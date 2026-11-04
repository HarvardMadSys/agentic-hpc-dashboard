# `dashboard/web` — live agent-behaviour dashboard (frontend)

React + Vite + TypeScript SPA for the live HPC agent-behaviour dashboard. It reads the
`rc_dashboard` HTTP/WS API on the **same origin** and builds to `dist/`, which the Python
backend serves as static files.

```
src/api/        the contract: types, fetch client, hooks, the live websocket
src/lib/        classes, purposes, formatters, URL/filter state, panel lookup, the words and geometry of phases and tasks
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
sandbox slice that `exited before read` (key `denied`), a session whose every sandbox
attempt failed, and a trajectory ranking dominated by one user. To see the
`<EmptyState>` path, set any rendered panel's `_present` to `false` in `mock/api.json` and
reload — the middleware re-reads the file on every request.

The fixture's agent rows carry the tasks the page has to word: quoted from a commit, from a log entry, and
from a pull request with a number and one without; built from names; a lone stretch; a gap that names a call
still running and one that names none; a task still open at the live edge; a task under a call log that was
cut; and one agent over two day rows, which serve one block. `npm run check:phases` reads every task and
phase of every row of it.

The fixture's sessions sit at its own `now_epoch`, so every live frame the mock sends
carries that as the service's clock (`clock.now`) and the page's "now" follows it: a
running session's live edge is minutes long, not the days between the fixture and today.
It stands still, like a capture's clock. `RC_MOCK_PINNED=1 npm run dev` also says it is
pinned (`clock.pinned`), as a frozen capture does, to see what the page says then.

The inspector's read (`/api/trajectory`) is one hand-built tree with its navigator (process starts
per bin, the landmarks, the failures), laid on the row's own span as the service reads a session: from
the first record, or from `?from=` where the inspector pages, for as long as the tree runs, so a long
session stops at its budget with the rest unread. The row's phases, the chart's process starts, and the
session track then agree about when it ran, and the phases behind the bars can be clicked through pages.
This is done in `mock/plugin.ts`, so a change to it needs the dev server restarted; `api.json` is re-read.

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

1. **Three classes, never two** — `agent` / `human` / `ide` (+ `unlabeled`).
   The list is closed in `src/lib/classes.ts` and labelled `agent` / `human` / `IDE`, always
   in full, never abbreviated. Folding the IDE class into "agent" is what produced the retracted GPU
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

- **Both grains, where both exist.** The sandbox tab shows per-event *and* per-session (the
  session judged by its tool calls); the
  overview shows the same fleet in eight units, because the class ranking is not stable
  across them.
- **`denied` is not `unsandboxed`.** Four sandbox states. `denied`, labelled "exited before
  read", is the namespace links being gone when the collector looked (it is root; these are
  few-ms processes), so it gets its own hue *and* a hatch (`src/components/Defs.tsx`). Every
  share is a state over the KNOWN verdicts with coverage beside it (`src/lib/sandbox.ts`), so
  neither it nor `unknown` sits silently in a denominator.

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
| **Resources** | `resources` | time series over the window (`series`): exits, CPU, runqueue wait, D-state by tool and major faults by exit time, resident memory sampled from residency ticks; run vs wait accumulators with coverage, quantile spine, delay accounting shown as `not measured` |
| **Risk** (`?tab=risk`) | `risk` | dangerous commands on a login node, credentials on command lines (counted, never shown), and build / interpreter CPU on the login node |
| **I/O** (`?tab=io`) | `io_process`, `resources` | syscall bytes vs disk bytes (the gap is the page cache), network accumulators, reads and writes by tool (each ranked on its own counter), per-user read/write/network, I/O distributions |
| **Sandbox** | `sandbox` | namespace isolation × approval at launch as independent axes; tool-call outcomes per session (isolated / not / **sandbox attempt failed**), wrapper calls by outcome, launch flags as written, the `ancestry_matrix` by wrapper vs process with `reading` surfaced |
| **Trajectories** | `/api/trajectories?rank=&window_min=` | rank switcher that only **reorders** (events and cpu columns always visible), a ribbon (the purpose of each process, or for an agent whose commands are read its phases, with a line naming its tasks: [below](#trajectories-tasks-phases-and-processes)) + tool tokens, `dominant_user_share_pct`; a **keys** column for how many collector session keys each row folds and how they were attributed. Clicking a row opens the **inspector** (`/api/trajectory`, `?traj=` in the URL): the row's ribbon and tokens over its process tree -- indentation guides, collapsible subtrees, start / duration / own and subtree CPU / exit or signal per process, a timeline bar on the read's own axis, repeated siblings folded into counted groups, weaker parent links and processes with no exit tagged, and every read bound stated above the tree. **oldest first / newest first** (or the table's `started` header) asks the service for the other end: newest first, the newest processes are the ones drawn in full and the oldest the ones counted |
| **Agents** | `cascades` | agent and subagent behaviour (`?tab=agents`, `?tab=cascades` still opens it): agent trees deepest first (agent / infra / client nodes, how each parent was found, each agent's home, directories and outside-home writes), agents with **subagents** kept apart from **hosted** sessions (a daemon or server running them); **Workspace & blast radius** per agent with a *this agent only / with its subagents* toggle -- where its processes **ran** (measured), the **bytes** they moved (measured per process), the paths its command lines **named** (inferred, drawn dashed), writes and deletes outside the home flagged by where they landed; depth and fan-out histograms, who-starts-whom, and the count-and-cost census of everything the classifier tagged |
| **Feeds** | `/api/feeds`, `/api/config` | tier, configured **vs** resolved path, status pill, lag vs `max_age_s`, rows, notice |

### Trajectories: tasks, phases, and processes

A session of an agent whose commands are read (Claude Code and Codex) is drawn at three levels, which the
page names **Tasks**, **Phases**, and **Processes** (`LEVELS`, `src/lib/phases.ts`). The service builds them
and holds every threshold (`dashboard/README.md`, *Trajectories: tasks, phases, and processes*); the page has
none of its own. The words and the geometry for them are in `src/lib/phases.ts` and `src/lib/tasks.ts`, so the
table, the chart, and the list say one thing.

- **The table row** (`src/tabs/TrajectoriesTab.tsx`, `src/components/PhaseBand.tsx`) draws the session's
  phases as a thin band, with a tick across it where each task or lone stretch after the first span begins.
  The tick is a 1 px line at the later span's first call, which across a cutting gap is the end of the gap.
  Under the band a line names the tasks, the first two in order and a count of the rest:
  `“Fix the loader” → Run train.py · config/base.yaml → 3 more`. It is two lines at most, with an ellipsis,
  and its hover has the whole of it. A row whose block has no task keeps the order its phases came in,
  `explore → edit → test`, in the same line, and has no tick.
- **The inspector** (`src/tabs/TrajectoryInspector.tsx`) shows the session's headline, then a chart with a
  **Tasks** lane above the **Phases**, which are tinted behind the **Processes**, the bars. A gutter at the
  chart's left captions the three. Under the chart is a list (`src/tabs/TaskList.tsx`): a task opens into
  its phases, a phase into the commands that ran most in it, and a button from there to its calls in the
  tree. A click on a task, in the lane or in the list, zooms the chart to it. A block with no tasks has the
  phase list alone (`src/tabs/PhaseList.tsx`).
- **A label says where it came from.** The agent's own words are a quote, in quotation marks, with their
  source (`commit`, `PR #235`, or `log`) where there is room for it. A label built from the names in the
  task's commands has neither, so it is never taken for something the agent wrote. The basis line over the
  chart reads `tasks · read from the commands the agent ran and the words it wrote in them`.
- **A lone stretch**, a few calls hours from other work, is no task. It has no number, its block in the lane
  is a dashed outline, the list gives it a line of its own (`3 calls at 10:41:24 · read src/app.py`), and
  neither the numbering nor the row's line counts it. The row's band does tick where it begins, as it does
  where a task does. A cutting gap is a line of its own too (`2.1 h with no new calls`), and it names the
  first of the calls the agent moved past that were still running, by the name the service gives it, else
  by its command (`· scratchpad/linktest still running since 13:05:44`).
- **The Codex app's reads of its own plugins** are housekeeping, so they are counted in the line under the
  chart (`housekeeping, not the model: …`, as the `app assets` shape) and are no call in any phase.
- **A session across midnight** is a row to a day, and each row serves the one block, so each draws the
  whole session's band, line, and list. A task or a phase picked in the list that the other day's row holds
  opens that row and goes there (`jumpTo` in the tab), whenever the window holds that row: its last record,
  as the block's `days` gives it, is at or after the window's start. The table need not list it. Where the
  window does not hold it, the pick is made on the open row.

### Live transport

`src/api/useLive.ts` prefers the websocket and falls back to polling `GET /api/live` every
15 s after four failed socket attempts. The transport is always named in the rail
(`transport ws` / `polling` / `down`), because a frozen chart that looks live is worse than
a chart labelled `polling`. A `rebuilt` frame triggers a re-read of `/api/panels`; a
`feeds` frame supersedes the REST feeds snapshot.

### Filters, URL state and export

Every filter — plus the active tab, the trajectory rank and the inspected trajectory (`traj`,
`tfrom` when a read was continued past its budget, and `torder=newest` when it is read newest
first) — lives in the query string, so
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
