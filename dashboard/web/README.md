# `dashboard/web` — live agent-behaviour dashboard (frontend)

React + Vite + TypeScript SPA for the live HPC agent-behaviour dashboard. It reads the
`rc_dashboard` HTTP/WS API on the **same origin** and builds to `dist/`, which the Python
backend serves as static files.

```
src/api/        the contract: types, fetch client, hooks, the live websocket
src/lib/        classes, purposes, formatters, URL/filter state, panel lookup
src/charts/     hand-rolled SVG primitives (lines, bars, stacks, ribbon)
src/components/ Panel/EmptyState, CountCost, Plate, Coverage, ClassFilter, EventsExplorer
src/tabs/       one module per tab
mock/           dev-only fixture + Vite middleware (NOT bundled, NOT in src/)
```

## Dev

```bash
npm install
npm run dev          # http://localhost:5173 — /api/* and /ws answered from mock/api.json
npm run dev:real     # /api and /ws proxied to a running backend
```

`npm run dev` needs **no backend**: a Vite middleware (`mock/plugin.ts`) answers every
`/api/*` route and performs a real websocket handshake on `/ws` from the static fixture
`mock/api.json`. The app is unaware of it — `src/api/client.ts` has no mock branch, so the
code path exercised in dev is the production path, over HTTP, with real status codes.

For `dev:real`:

```bash
RC_BACKEND=http://127.0.0.1:8787 npm run dev:real
# or
npm run dev -- --mode real          # same thing
RC_MOCK=0 npm run dev               # same thing
```

Regenerate the fixture after changing its shape (deterministic — no RNG, byte-stable):

```bash
npm run fixture      # node mock/gen_fixture.mjs -> mock/api.json
```

The fixture deliberately contains the awkward states the UI has to get right: a `missing`
feed, a `stale` feed whose `lag_s` exceeds its own `max_age_s`, an `empty` feed, `null`
rate bins (including a 23-minute outage), accumulators with `n: 0, coverage_pct: 0`, a
`denied` sandbox slice, and a trajectory ranking dominated by one user. To see the
`<EmptyState>` path, set any rendered panel's `_present` to `false` in `mock/api.json` and
reload — the middleware re-reads the file on every request.

## Build

```bash
npm run build        # tsc -b && vite build  ->  dist/
npm run typecheck    # tsc -b, no emit
npm run preview      # serve dist/ locally (no API; use the backend for that)
```

Output as of the last build:

| file | raw | gzip |
|---|---|---|
| `dist/index.html` | 0.90 kB | 0.51 kB |
| `dist/assets/index-*.css` | 12.7 kB | 3.3 kB |
| `dist/assets/index-*.js` (app) | 77.3 kB | 20.7 kB |
| `dist/assets/react-*.js` (runtime) | 218.9 kB | 68.3 kB |

Assets are emitted with a **relative** `base` (`./`), so `dist/` works mounted at `/` or
under a sub-path. The API is addressed absolutely at `/api` and the socket at `/ws`;
override with `VITE_API_BASE` / `VITE_WS_URL` at build time if the backend is elsewhere.

## Dependencies

Runtime: **`react`, `react-dom`.** That is all.

Dev: `vite`, `@vitejs/plugin-react`, `typescript`, `@types/{node,react,react-dom}`.

There is **no charting library**. Every chart is hand-rolled SVG in `src/charts/primitives.tsx`,
ported from the retired demo template (git history: `dashboard/archive/`), for two
reasons that are constraints here rather than preferences: there is no code path that can introduce a second
value axis, and the colours are the CSS custom properties verbatim, so no library theme can
substitute a hue. There is no table library either — the event explorer pages on the
backend's `next_cursor`.

## The rules this code enforces

These come from the retired demo's README (git history: `dashboard/archive/README.md`).
They are research-integrity constraints; breaking one is a reporting error, not a styling error.

1. **Three classes, never two** — `agent` / `human-vscode` / `human` (+ `unlabeled`).
   The list is closed in `src/lib/classes.ts` and labelled `agent` / `human in VS Code` /
   `human (shell)`. Folding VS Code into "agent" is what produced the retracted GPU
   near-parity claim. `<Plate>` **requires** a per-class breakdown, so a headline number
   cannot ship without its split.
2. **Count and cost side by side** — every categorical panel goes through
   `<CountCost>`, which renders an event-weighted chart and a CPU-weighted chart, each on
   its own single axis. `GroupBars`/`Lines` take one `ylabel` and have no second-axis
   option, so a dual axis is not expressible.
3. **Colour** — Harvard palette, light only, tokens copied from the archived template.
   Crimson `#A51C30` (`--s1`) is reserved for `agent`. Eight series slots plus one grey
   tail (`--sg`); the purpose table in `src/lib/purposes.ts` folds everything outside its
   eight slots into `other` rather than inventing a ninth hue.
4. **Zero is not missing** — `_present === false` renders the shared `<EmptyState>`:
   the panel frame, the title, the feed name, and one line per `_paths` entry, verbatim
   (`no data from /var/log/ebpfm`). A feed that IS present but has no rows renders
   `<NoRows>` — the word `none`. The two look nothing alike, because they are not the
   same fact.
5. **Nothing fabricated** — there is no `Math.random()` anywhere in `src/`
   (`npm run check:no-random` asserts it), and nothing interpolates between frames. A
   `null` rate bin arrives as `null` and `Lines` emits a separate subpath around it, so a
   minute with no record is a **hole in the line** — never a 0, never a straight line
   drawn across the hole. The dev fixture lives in `mock/`, outside `src/`, so it cannot
   reach the production bundle.
6. **Coverage is part of the number** — `<Coverage>`, `<AccumCell>` and `<QuantCells>`
   print `coverage_pct` next to every value that carries one. An accumulator or quantile
   with `n: 0` renders the words **`not measured`**, never `0`.
7. **Data only** — a panel carries a title, its feed, and the chart or table. Every
   `notes` / `basis` / `cpu_basis` / `scope` / `dedup.rule` string goes into `<Basis>`, a
   collapsed `basis` affordance at the foot of the panel, or into a `data-tip` tooltip.
8. **Responsive to ~900px** — below 900px the rail becomes a horizontal nav and the grid
   collapses to one column. Wide tables scroll inside their own `.scroller`; the page body
   never scrolls horizontally.

Two more that the payloads force:

- **Both grains, where both exist.** The sandbox tab shows per-event *and* per-tree; the
  overview shows the same fleet in eight units, because the class ranking is not stable
  across them.
- **`denied` is not `unsandboxed`.** Four sandbox states. `denied` is the collector being
  unable to read the field (it is unprivileged), so it gets its own hue *and* a hatch
  (`src/components/Defs.tsx`) and is excluded from the sandboxed numerator.

## The window control

`src/components/WindowPicker.tsx`, directly above the class filter — both answer "what is on
screen", and a reader checking one should not have to hunt for the other. It is URL-backed as
`?win=<minutes>`, like every other view knob.

Three rules it follows, all of them the same rule this page follows everywhere:

- **Presets come from the service**, filtered server-side against retention, so the picker
  cannot offer a span the process does not hold.
- **A clamp is shown, never silent.** Ask for 30 days against 7 days of retention and the
  control reads `asked 30d · showing 7d` with the reason on hover.
- **The three kinds of nothing stay three.** A panel whose window is still being reduced
  renders `<Building>` — a solid frame and a percentage — not `none` (which would assert a
  measurement) and not the empty state (which would blame the feed).

Plate labels read the window off the **panel's own envelope** (`winLabel`), not off the
picker: during a rebuild those genuinely differ, and the plate is the one that must be right.

## Tabs

| tab | source | notes |
|---|---|---|
| **Live** | `WS /ws`, `GET /api/live` | three-class stat tiles, per-minute rate chart over the **selected window** with real gaps, per-host table with a per-class split, bounded event stream, submissions |
| **Overview** | `/api/panels?window_min=` | share of calls vs share of CPU; the same fleet in eight units |
| **Sessions & tools** | `tool_mix` | the 12-way bucket mix, counted then costed; per-class head; shell kinds |
| **Resources** | `resources` | run vs wait accumulators with coverage, quantile spine, stall by tool, delay accounting shown as `not measured` |
| **Risk & I/O** | `io_process` | syscall bytes vs disk bytes (the gap is the page cache), network accumulators, per-tool/per-user — plus the **event bench** (filter bar → `/api/events` → `/api/export`) |
| **Sandbox** | `sandbox` | sandbox × approval as independent axes, two grains, the `ancestry_matrix` with `reading` surfaced |
| **Trajectories** | `/api/trajectories?rank=&window_min=` | rank switcher that only **reorders** (events and cpu columns always visible), purpose ribbon + tool tokens, `dominant_user_share_pct` |
| **Feeds** | `/api/feeds`, `/api/config` | tier, configured **vs** resolved path, status pill, lag vs `max_age_s`, rows, notice |

### Live transport

`src/api/useLive.ts` prefers the websocket and falls back to polling `GET /api/live` every
15 s after four failed socket attempts. The transport is always named in the rail
(`transport ws` / `polling` / `down`), because a frozen chart that looks live is worse than
a chart labelled `polling`. A `rebuilt` frame triggers a re-read of `/api/panels`; a
`feeds` frame supersedes the REST feeds snapshot.

### Filters, URL state and export

Every filter — plus the active tab and the trajectory rank — lives in the query string, so
a reload or a pasted link restores the view. `src/lib/filters.ts` owns the one
serialisation: `apiQuery()` builds the string sent to **both** `/api/events` and
`/api/export`, so the download is exactly the view by construction rather than by
convention. The row count next to the Download button is the user's confirmation of that.

Supported params: `from`, `to`, `class` (repeatable), `agent_type`, `user`, `host`, `tool`,
`purpose`, `bucket`, `session_key`, `sandbox`, `approval`, `depth_min`, `depth_max`,
`cpu_min`, `duration_min`, `exit_code`, `signal`, `q`.

## Known gaps

- The event table is **paged** (`next_cursor`), not virtualised. Fine for the backend's
  page size; a 10⁵-row single view would need windowing.
- `/api/panels` payloads that this UI does not yet render (anything beyond `tool_mix`,
  `io_process`, `resources`, `sandbox`, `trajectories`) are ignored rather than shown
  generically.
- The `purposes` list returned by `/api/panels` is not consulted — the eight-slot colour
  table is fixed in `src/lib/purposes.ts` so the palette cannot grow a ninth hue at
  runtime. A new purpose folds into the grey `other`.
