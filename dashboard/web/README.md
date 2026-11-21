# `dashboard/web` — live agent-behaviour dashboard (frontend)

React + Vite + TypeScript SPA for the live HPC agent-behaviour dashboard. It reads the
`rc_dashboard` HTTP/WS API on the **same origin** and builds to `dist/`, which the Python
backend serves as static files.

```
src/api/        the contract: types, fetch client, hooks, the live websocket
src/lib/        classes, purposes, formatters, URL/filter state, panel lookup, the sticky control bar, the words and geometry of phases and tasks
src/charts/     hand-rolled SVG primitives (lines, bars, stacks, ribbon)
src/components/ Panel/EmptyState, CountCost, Plate, Coverage, ClassFilter, EventsExplorer, PortraitFigure, Icon, CommandPalette
src/tabs/       one module per tab
mock/           dev-only fixture + Vite middleware (NOT bundled, NOT in src/)
```

## Dev

```bash
npm ci
npm run dev          # http://localhost:5173 — the page's main routes and /ws answered from mock/api.json
npm run dev:real     # /api, /ws, /login, and /logout proxied to a running backend
```

`npm run dev` needs **no backend**: a Vite middleware (`mock/plugin.ts`) answers the routes
the tabs are drawn from (`/api/config`, `feeds`, `windows`, `panels`, `live`, `trajectories`,
`trajectory`, `users`, `user`, `events` and `export`) and performs a real websocket handshake
on `/ws`, from the static fixture `mock/api.json`. Any other route is a 404 there (the
collectors' health, Active users' searches and accounts, the commands behind an I/O number,
Ask and annotate), so what reads it says it was not returned; `dev:real` has them all. The
app is unaware of it — `src/api/client.ts` has no mock branch, so the
code path exercised in dev is the production path, over HTTP, with real status codes.

For `dev:real`:

```bash
RC_BACKEND=http://127.0.0.1:8080 npm run dev:real   # the backend's port; unset, :8787
# or
npm run dev -- --mode real          # same thing
RC_MOCK=0 npm run dev               # same thing
```

The backend refuses a websocket opened by a page on any origin but its own, another port of
the same host included (`rc_dashboard/guard.py`), and Vite's page is on its own port. `../run.sh`
names it to the backend for you; a backend started any other way needs it named, as the
browser will write it: `RC_DASH_DEV_ORIGINS=http://127.0.0.1:5173,http://localhost:5173`
(`server.dev_origins`, added to `server.allowed_origins`), or the page's `/ws` gets a 403 and
the Live tab does not move.

Regenerate the fixture after changing its shape (deterministic — no RNG, byte-stable):

```bash
TZ=America/New_York npm run fixture      # node mock/gen_fixture.mjs -> mock/api.json
```

The live tab's clock strings (the rate's minute labels, and the `ts` of its events and submissions) are written
in the generator's local time, and the committed file was made in `America/New_York`: regenerated in another
zone, those strings change, and so do Day by day's (`trend`), whose days are cut at local midnight (their
dates, minutes and counts); nothing else does. The Users
section (`users`) is the fixture's own trajectory rows composed as `rc_dashboard/portraits.py` composes the
service's, with its hours in a fixed EDT, so it is the same in any zone.

The fixture deliberately contains the awkward states the UI has to get right: a `missing`
feed, a `stale` feed whose `lag_s` exceeds its own `max_age_s`, an `empty` feed, `null`
rate bins (including a 23-minute outage), accumulators with `n: 0, coverage_pct: 0`, a
sandbox slice that `exited before it was checked` (key `denied`), a session whose every sandbox
attempt failed, and a trajectory ranking dominated by one user. To see the
`<EmptyState>` path, set any rendered panel's `_present` to `false` in `mock/api.json` and
reload — the middleware re-reads the file on every request. Delete a panel's key instead to
see `<NotReturned>`; the fixture already leaves out the Slurm and risk-rules panels. Give a
panel an `_error` string to see `<NotReduced>`, a panel whose reducer raised.

The node tier, the day-by-day panel and the exit verdicts have theirs: a node whose probes are
ten times the rest, an NFS hiccup in the same bins as the resources series' stall, a host whose
snapshots stop for 40 minutes (a hole), a full `/tmp` and a `/var` out of file slots, an account
held back at its CPU limit and one whose cgroup was made again, processes stuck in NFS waits, a
day the collector wrote nothing in, the oldest day cut where retention starts, a test runner
failing most of its runs, a command not found, a timeout's SIGKILL, grep's exit-1 answers and
`| head`'s SIGPIPE. With no Slurm or risk panel, Needs attention names both as not checked.

The fixture's agent rows carry the tasks the page has to word: quoted from a commit, from a log entry, and
from a pull request with a number and one without; built from names; a lone stretch; a gap that names a call
still running and one that names none; a task still open at the live edge; a task under a call log that was
cut; and one agent over two day rows, which serve one block. `npm run check:phases` reads every task and
phase of every row of it, and `npm run check:figures` draws every user's portrait figure and checks each
part against their row.

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
npm run build:watch  # vite build --watch: dist/ rebuilt on every edit, no type check (../run.sh build)
npm run typecheck    # tsc -b, no emit
npm run check        # every check below: check:no-random, then each mock/check_*.mjs
npm run check:no-random # no Math.random() anywhere in src/ (rule 5)
npm run check:phases  # node --test: every task and phase of every fixture row is worded and drawn
npm run check:figures # node --test: every fixture user's portrait figure agrees with their row
npm run check:charts # node --test: the chart tick and width rules in src/charts/scale.ts
npm run check:css    # node --test: every stylesheet closes its blocks, no rule nested in a rule
npm run check:shares # node --test: a class's share is out of every class counted, whatever the chips show
npm run check:panels # node --test: a panel not reduced, waited for or building is never "none" or 0
npm run check:live   # node --test: the live socket is retried for good, polled meanwhile, replaced when quiet
npm run check:lookup # node --test: no outside string reads a table's prototype; a part that throws says so
npm run check:procexit # node --test: a run's box says how it ended as the process table does
npm run check:security # node --test: a security check with no panel behind it says "not checked", never none; every link lands
npm run check:hpc    # node --test: abnormal Slurm activity is worded from the service's thresholds, and never none of a panel not judged
npm run check:io     # node --test: the per-tool disk chart keeps the heaviest on each counter, a 0 as 0, one scale
npm run preview      # serve dist/ locally (no API; use the backend for that)
```

Output as of 2026-11-20:

| file | raw | gzip |
|---|---|---|
| `dist/index.html` | 1.2 kB | 0.6 kB |
| `dist/assets/index-*.css` | 89.1 kB | 17.1 kB |
| `dist/assets/index-*.js` (app) | 287.3 kB | 95.4 kB |
| `dist/assets/react-*.js` (runtime) | 218.8 kB | 68.3 kB |
| a chunk per tab loaded on demand (`UsersTab-*`, `ResourcesTab-*`, `AgentsTab-*`, ...), and the ones they share (`scale-*`, `format-*`, `TreePopout-*`) | 4 to 62 kB each | 2 to 19 kB |

Assets are emitted with a **relative** `base` (`./`), so `dist/` works mounted at `/` or
under a sub-path. The API is addressed absolutely at `/api` and the socket at `/ws`;
override with `VITE_API_BASE` / `VITE_WS_URL` at build time if the backend is elsewhere.

Every build stamps the bundle with where it came from, and the foot of the rail shows it:
`commit 0d516df` (HEAD when it was built, with `+ edits` when anything under `web/`
differed from it) and `built 2026-11-15 14:02` (local time; the full timestamp is on
hover). The stamp is `virtual:build-info`, written by the plugin in `vite.config.ts` and
re-read on each build rather than fixed in a `define`, so the one `vite build --watch`
that `./run.sh build` keeps alive stamps every rebuild with its own time. The dev server
builds nothing and says so (`built — (dev server)`); its commit is re-read on every page
load. Built without git or outside a checkout, the commit reads `—`, not a guess.

## Dependencies

Runtime: **`react`, `react-dom`.** That is all the bundle holds. The typeface, IBM Plex Sans
and Mono, comes from Google Fonts (`index.html`); where that is out of reach the browser's own
fonts stand in.

Dev: `vite`, `@vitejs/plugin-react`, `typescript`, `@types/{node,react,react-dom}`.

There is **no charting library**. Every chart is hand-rolled SVG: the primitives in `src/charts/`
(`primitives.tsx`, `TimeChart.tsx`), and the few figures that draw their own (`PhaseBand`,
`SessionMap`, `TrajectoryNav`, `PortraitFigure`, `TreeDiagram`), ported from the retired demo template (git history: `dashboard/archive/`), for two
reasons that are constraints here rather than preferences: there is no code path that can introduce a second
value axis, and the colours are the CSS custom properties verbatim, so no library theme can
substitute a hue. There is no table library either — the event explorer pages on the
backend's `next_cursor`. Nor an icon library: `src/components/Icon.tsx` draws the page's
few icons as paths, in the text colour, so no icon is ever a series colour.

## The rules this code enforces

These come from the retired demo's README (git history: `dashboard/archive/README.md`).
They are research-integrity constraints; breaking one is a reporting error, not a styling error.

1. **Three classes, never two** — `agent` / `human` / `ide` (+ `unlabeled`).
   The list is closed in `src/lib/classes.ts` and labelled `agent` / `human` / `IDE`, always
   in full, never abbreviated. Folding the IDE class into "agent" is what produced the retracted GPU
   near-parity claim. `<Plate>` prints its per-class row from `by`; only a plate about agents
   alone, on Users and on Security's sandbox panels, leaves it out.
2. **Count and cost side by side** — every categorical panel goes through
   `<CountCost>`, which renders an event-weighted chart and a CPU-weighted chart, each on
   its own single axis. `GroupBars`/`Lines` take one `ylabel` and have no second-axis
   option, so a dual axis is not expressible.
3. **Colour** — Harvard palette, light only, tokens copied from the archived template.
   Crimson `#A51C30` (`--s1`) is `agent`'s among the classes; outside them, the `wait` phase
   (`src/lib/phases.ts`) wears it too. Eight series slots plus one grey
   tail (`--sg`); the purpose table in `src/lib/purposes.ts` draws the service's purposes as
   three kinds (slurm, build & compute, files & data) in three of those slots, and folds
   everything else (git, polling, the agent's runtime, editors, ...) into the grey
   `other` rather than inventing a hue. A run, a process and a tooltip still name the
   purpose itself; only the swatch is its kind's. A series that is not a
   class — a tool, or a direction such as sent and received — takes its hue from
   `TOOL_COL` (`src/charts/TimeChart.tsx`), never a class's.
4. **Zero is not missing** — `_present === false` renders the shared `<EmptyState>`:
   the panel frame, the title, the feed name, and one line per `_paths` entry, verbatim
   (`no data from /var/log/ebpfm`). A feed that IS present but has no rows renders
   `<NoRows>` — the word `none`. The two look nothing alike, because they are not the
   same fact. A panel the backend did not return — an answer without its key, or a
   request that failed — renders `<NotReturned>`: one dashed line reading `not returned`, the
   reason, and the feed it reads. It wears no feed status and names no path, because no
   feed reported anything; whether the feed is healthy is the Feeds tab's to say.
5. **Nothing fabricated** — there is no `Math.random()` anywhere in `src/`
   (`npm run check:no-random` asserts it), and nothing interpolates between frames. A
   `null` rate bin arrives as `null` and `Lines` emits a separate subpath around it, so a
   minute with no record is a **hole in the line** — never a 0, never a straight line
   drawn across the hole. The dev fixture lives in `mock/`, outside `src/`, so it cannot
   reach the production bundle. The service's demo mode (`rc_dashboard/demo`) feeds it
   records that were never measured, and the page says so on every tab: the feeds report
   lists the hosts whose records carry `"synthetic": true` (`synthetic_hosts`), and while
   any does, every tab opens with a *Fake data* note, the header shows a dashed **fake data**
   pill beside the live one, and the Feeds tab tags each such host.
6. **Coverage is part of the number** — `<Coverage>`, `<AccumCell>` and `<QuantCells>`
   print `coverage_pct` next to every value that carries one. An accumulator or quantile
   with `n: 0` renders the words **`not measured`**, never `0`.
7. **Data only** — a panel carries a title, its feed, and the chart or table. Every
   `notes` / `basis` / `cpu_basis` / `scope` / `dedup.rule` string goes into `<Basis>`, a
   collapsed `basis` affordance at the foot of the panel, or into a `data-tip` tooltip.
8. **Down to a phone** — at 900px and under (`src/lib/useNarrow.ts`) the rail becomes a
   drawer, opened from a top bar that names the tab, and every grid collapses to one column.
   Wide tables scroll inside their own `.scroller`, the widest with their label column held
   in view (`pin`); the page body never scrolls horizontally, down to 320px, and a table's
   edge fades where there is more to scroll to (`src/lib/scrollEdges.ts`), so a cut-off
   column does not pass for the last one. A panel's controls drop under its title and its
   switches wrap. A time series is drawn at its container's width, at 1:1 (`<Fluid>`,
   `src/charts/primitives.tsx`), and in the narrow layout so is every other chart in a box
   narrower than it (`useFit`), so a 10px label stays 10px instead of printing at 3px; on a
   narrow plot the ticks thin out (`tickTarget`) rather than overprint. On a touch screen a
   tap shows a `data-tip`, controls are a fingertip high, and form fields are 16px so iOS
   does not zoom into them. Above 900px, with a mouse, the page is as it was.
9. **No outside string reads a prototype** — a key that comes from the URL, the service or
   the collector indexes only a table made by `table()` (`src/lib/lookup.ts`), which has no
   prototype, or is read with `own()`: `?atype=__proto__` once handed the filter's label
   `Object.prototype`, which React cannot draw, and the page went white.
10. **A part that throws says so** — one boundary around the page (`src/main.tsx`), one per
   tab (`TabBoundary`, `src/lib/lazyTab.tsx`), one per panel's body (`<Panel>`), each from
   `src/components/Boundary.tsx`: "this tab could not be drawn: … · reload" in the frame of
   the missing family, and the rest of the page stays. A tab's code that would not load (a
   deploy replaced it under an open page) is one such failure. A panel handed new data tries
   again.

Two more that the payloads force:

- **Both grains, where both exist.** The Security tab's sandbox panels show per-process *and* per-session (the
  session judged by its tool calls); Live's *Who uses what* shows the same fleet in eight
  units on the login nodes, and three more asked of Slurm where there is a Slurm panel, because
  the class ranking is not stable across them.
- **`denied` is not `unsandboxed`.** Four sandbox states. `denied`, labelled "exited before
  it was checked", is the namespace links being gone when the collector looked (it is root; these are
  few-ms processes), so it gets its own hue *and* a hatch (`src/components/Defs.tsx`). Every
  share is a state over the KNOWN states with coverage beside it (`src/lib/sandbox.ts`), so
  neither it nor `unknown` sits silently in a denominator.

## Words

One word for each thing, on every tab. A label says the word in the first column; the words
in the last are not used for that thing anywhere on the page. `src/lib/glossary.ts` holds
the same table, and the line each word shows on hover (its `?`), where the precise reading
is: what is measured, and what it cannot see.

| say | for | not |
|---|---|---|
| class: **agent**, **human**, **IDE** | who ran it | "who ran it" as a heading, people, editor |
| **agent**, **agent type** | an AI coding agent (one running copy of one, as the Agents tab counts them), and which it is: Claude Code, Codex, … | a run of one: that is a session |
| **session** | one run of an agent or an IDE and everything it started, or one person's terminal login | trajectory, tree, run |
| **process tree** | a session's processes, each under the one that started it | trajectory |
| **process** | one program that ran; the collector records it when it exits | event, exits (as a count) |
| **tool call** | a command an agent issued itself; what that command started counts as processes | call, command of its own |
| **login node** | a machine the collector watches, in the site's own word (`ui.node_noun`, `src/lib/nodes.ts`) | host, machine |
| **start folder** | the directory an agent was started in | home: that is a user's home folder |
| **driven from** | a terminal, a script, an IDE extension, the desktop app, the web or a phone, or unknown | launcher, surface, the CLI/app prefix |
| **approval at launch** | bypass flag, auto mode, no bypass flag, can't tell | approval bypassed, not observable |
| **sandboxed** | ran in its own namespaces | namespace-isolated, isolated, unsandboxed |
| **sandbox failed to start** | a wrapper ran, and the command it wraps did not | attempt failed (no payload ran) |
| **wrapped command** | what a sandbox wrapper was asked to run | payload |
| **memory**, **peak memory** | resident memory (RSS), and the most of it held at once | resident, memory in use |
| **disk read**, **disk written** | bytes that came from or reached the disk | rd, wr, `rd_mb`, `wr_mb` |
| **read requested**, **write requested** | bytes through `read()` and `write()`, the page cache included | syscall bytes, rchar, wchar |
| **sent**, **received** | network bytes | tx, rx, egress, ingress |
| **median** | the middle value | p50 |
| **pieces** | how many collector keys a session's row was joined from | keys |
| **feed** | a stream of records the page reads: process tracer, node metrics, job accounting, … | data source, tier, provenance |
| **fake data** | the demo's made-up records | synthetic data |

The tabs are Live, Users, Agents, **Sessions**, Security, Login nodes (Resources, **Risks**), **HPC**,
Ask LLM, Internal and Feeds. A tab's URL key (`?tab=trajectories`, `?tab=risk`, `?tab=slurm`), an API field and a
filter key keep their old names, so links, citations and exports are unchanged: only what the
page says moved. A panel whose title changed keeps its old anchor (`id=`), which
`src/lib/cites.ts` points at.

## The window control

`src/components/WindowPicker.tsx`, in one control bar with the class filter and the user and
agent-type dropdowns — both answer "what is on screen", and a reader checking one should not
have to hunt for the other. The bar sticks to the top of the page as a tab scrolls
(`src/lib/sticky.ts`), so the answer is still in view at the foot of a long tab. It publishes
its height as `--controls-h`, which is the page's `scroll-padding-top`, so nothing the browser
scrolls to (a control reached with Tab, a panel picked in the section list, the trajectory
inspector) lands under it, and which the Agents tab's sticky list and detail stick below. On a
phone (640px and below) it scrolls with the page. It is URL-backed as `?win=<minutes>`, like
every other view knob, and it is the only control of the window: the Live tab's headline plates
count it too. It offers the service's floor first (`min_minutes`, 5 minutes) when the presets
start above it -- read from `/api/windows`, whose websocket `windows` frame does not carry it,
and not offered until the service has said it -- and the "go to" palette lists the same spans.
Besides those it takes a typed span (`90m`, `4h`, `2d`, `1w`, or bare minutes; cleared, the
service's default), and beside it says what the window is doing:
`filled 5h of 7d`, `reading history · N%` while the start's read-back runs, `rebuilding` or
`catching up` with its percentage, `… failed to build — pick it again to retry`, and `+Nm past
the window` while a built view drifts.

Three rules it follows, all of them the same rule this page follows everywhere:

- **Presets come from the service**, filtered server-side against retention, and so does the
  floor, so the picker cannot offer a span the process does not hold.
- **A clamp is shown, never silent.** Ask for 30 days against 7 days of retention and the
  control reads `asked 30d · showing 7d` with the reason on hover.
- **The kinds of nothing stay apart.** A panel whose window is still being reduced
  renders `<Building>` — a solid frame and a percentage — not `none` (which would assert a
  measurement) and not the empty state (which would blame the feed). One the backend did
  not return renders `<NotReturned>` (rule 4), never the empty state. One whose reducer
  raised arrives as its envelope with `_error` and nothing else (`windows.materialize`): it
  renders `<NotReduced>`, "the service could not reduce this panel" and what it said, and
  `pick` hands it on as not present, so a plate reads a dash rather than a 0. A tab that
  says `none` of a panel says it only of a body the service reduced (`hasBody`): while the
  panel is waited for, building, or not reduced, the panel says which.

Plate labels read the window off the **panel's own envelope** (`winLabel`), not off the
picker: during a rebuild those genuinely differ, and the plate is the one that must be right.

## The rail and "go to"

The rail lists the tabs in their reading order under two words, Monitor and More, each with
its icon, and the open tab's sections under it. Resources and Risks share one entry, named
in the site's own word for its hosts (`ui.node_noun`, "Login nodes"; their figures are
login-node use, not the cluster's), with a sub-tab each along the foot of the head. A sub-tab
keeps its own key (`?tab=resources`, `risk`), so links and citations land where they did, and
the entry reopens the sub-tab last used. HPC, the scheduler, was the third sub-tab and is an
entry of its own: what a session asks of the cluster is not login-node use. It keeps the key
`slurm`, and `?tab=hpc` opens it too. Overview's one table is a
section of Live (*Who uses what*), and `?tab=overview` lands on it. The sandbox panels moved from
Internal to Security, and `?tab=sandbox`, which opened Internal, opens Security. The section under the top of
the screen is marked as the page scrolls — on a page too short to scroll, the first — and one
picked from the list or from go-to stays marked while it is on screen, even near the foot of a
page where it cannot reach the top. It folds to the icons alone
(`[`, or the button at its foot), which this browser remembers (`localStorage`, a per-viewer
convenience: storage that is absent or refuses just leaves it unfolded). Its foot says how
the page gets its data: the transport as a light, the last update, the window, and when the
panels were built.

**Go to** (`src/components/CommandPalette.tsx`), opened with Ctrl+K or Cmd+K anywhere, or `/`
outside a field: type to reach a tab, a section of the open tab, a window preset, or an
action (refresh, show all three classes, fold the rail). It offers only what the page already
holds, so it can never name a tab, section or window that is not there.

## Keyboard

Every control is a tab stop with a visible focus ring (controls restyled from `all: unset`
restate it at the foot of `src/styles.css`). A sortable header (`src/components/SortTh.tsx`)
sorts with Enter or Space, and a control's `data-tip` shows under it when it is reached with
Tab (`src/components/Tooltip.tsx`), since the caveat about how a number is counted lives there.
The tip is visual only: its layer is `aria-hidden`, and no control points at it with
`aria-describedby`, so a screen reader does not announce it.

| key | does |
|---|---|
| Ctrl+K, Cmd+K | opens or closes **go to**, anywhere |
| `/` | opens **go to**, outside a field |
| `[` | folds or unfolds the rail, outside a field |
| ↑ ↓ (or Tab, Shift+Tab), Enter, Esc | in **go to**: move, go, close (focus returns where it was) |
| ← →, Enter or Space, + −, 0 or Esc | on the inspector's chart and its purpose ribbon: step, open what is under the cursor, zoom, reset |
| ↑ ↓ | in the Agents tab's list: the previous or next agent |
| Esc | closes the rail's drawer below 900px |

## Tabs

| tab | source | notes |
|---|---|---|
| **Live** | `WS /ws`, `GET /api/live`, `/api/panels?window_min=`, its `node` and `trend` | *Needs attention* first: what the service already flags -- a feed past its own max age, risk-rule hits by agents, credentials on command lines, sessions polling the queue, refused submissions, full local disks, accounts held back at a CPU limit, NFS retransmissions -- each with its per-class split and a link to its panel, and every source it could not check named, never read as an all-clear (`src/tabs/Attention.tsx`); then one row of plates over the window (agent processes exited, agent sessions, risky agent actions), labeled with the window their panels were built over; the per-minute rate chart over the **selected window** with real gaps, *Who uses what* (each class's share of the window in eight units on the login nodes and three asked of Slurm, the old Overview tab: each unit's whole as one stacked bar, split by class, with each class's share and amount written out in its own column beside it, so a part too thin to see on the bar is still read there; a class the chips hide stays in the bar as a neutral part, and a unit with no known whole draws no bar but the reason (`src/components/SplitBar.tsx`); `?tab=overview` lands on it); *Day by day*, each class per day over retention (`trend`, not the window, and not narrowed by the filter, which it says), count and cost side by side, a day with no record a gap (`src/tabs/DayByDay.tsx`); *Over the window* (CPU and memory; each class's share of them is *Who uses what*'s); *Login node response*, the node tier's probes, slowest NFS round trip and load per host over the window, zoomed with the CPU chart (`node.series`, `src/tabs/NodeResponse.tsx`); *More over time* (runqueue wait, D-state, major faults, disk, network, Slurm checks and jobs); *Active users*, running now, one row per account, an account opened, and every process as it exits (`src/tabs/LiveUsers.tsx`); and the login nodes right now, with a per-class split |
| **Users** | `/api/users`, `/api/user?name=` | a portrait of each user from how they use coding agents (`src/tabs/UsersTab.tsx`, words in `src/lib/portraits.ts`): a roster, one row per user with an agent session in the window -- agents, where sessions are driven from, launched with a bypass flag, tool calls by what they do, a typical session, how many at once, active minutes by hour, and traits, with a line under each saying in one sentence how they leverage agents -- filterable by trait, and shown as a table or as **figures**, each user drawn from their numbers (`src/components/PortraitFigure.tsx`: a part for each number, its numbers and rule on hover, a key beside it); a row or a figure opens the user's **portrait** (`?portrait=`): their figure, how they leverage agents, the sentence and then point by point with the numbers each rests on and the window's medians beside them, the traits with the rule behind each, launch shares with the window's share as a tick on each bar, the hours and the sessions over the window, calls counted beside time by phase, projects and tasks in the agents' own words, the scheduler, and every session, each a link to its process tree. The service holds every threshold (`rc_dashboard/portraits.py`); the page only words what it counted |
| **Agents** | `cascades`, `tool_mix` | agent and subagent behaviour (`?tab=agents`; `?tab=cascades`, and `?tab=sessions` from the old Sessions & tools tab, still open it): every agent tree, listed by user (the default) or as one list, the latest first (agent / infra / client nodes; `claude daemon`'s idle spares are infra, counted not drawn; how each parent was found, each agent's start folder, directories and writes outside it), agents with **subagents** kept apart from **hosted** sessions (a daemon or server running them); **Workspace & blast radius** per agent with a *this agent only / with its subagents* toggle -- where its processes **ran** (measured), the **bytes** they moved (measured per process), the paths its command lines **named** (inferred, drawn dashed), writes and deletes outside its start folder flagged by where they landed; depth and fan-out histograms, who-starts-whom, and the count-and-cost census of everything the classifier tagged; then the **Tool mix** and **Top tools** (the old Sessions & tools tab: the 12-way bucket mix, counted then costed, by class or by kind of tool; per-class head); *Commands that failed* (`tool_mix.exits`): how the tool mix's processes ended, per class, agent type and tool -- a failure is a non-zero exit or a signal, past grep's exit-1 answer and SIGPIPE -- with each row's commonest exit codes and signals, and each count opening the commands behind it (`outcome` filter; `src/tabs/ExitsPanel.tsx`) |
| **Sessions** (`?tab=trajectories`) | `/api/trajectories?rank=&window_min=` | rank switcher that only **reorders**; each row counts its tool calls where the agent's commands are read (its processes on hover), else its processes, and draws a ribbon (the purpose of each process, or for an agent whose commands are read its phases, with a line naming its tasks: [below](#sessions-tasks-phases-and-processes)); a plate gives the busiest user's share of agent processes (`summary.agent.top_user_share_pct`). Clicking a row opens the **inspector** (`/api/trajectory`, `?traj=` in the URL), which reads top down: who ran the session and what it came to, its headline and gist, a **pieces** count for how many collector session keys the row joins and how they were attributed, then **What it did** (for an agent whose commands are read: a map of its tasks over its phases on the whole session's clock, and the list of tasks, each with its phases named under it), then **Processes**: the purpose ribbon and tool tokens, what the read covers, the chart of the read, and the process tree -- indentation guides, collapsible subtrees, start / duration / own and subtree CPU / exit or signal per process, a timeline bar on the read's own axis, repeated siblings folded into counted groups, weaker parent links and processes with no exit tagged, and every read bound stated above the tree. **oldest first / newest first** (or the table's `started` header) asks the service for the other end: newest first, the newest processes are the ones drawn in full and the oldest the ones counted |
| **Security** (`?tab=security`; `?tab=sandbox` opens it too) | `sandbox`, `risk`, `cascades` | how the agents were launched, how their tool calls were confined, what they exposed, and where they wrote outside their start folder (`src/tabs/SecurityTab.tsx`). *Security checks* first: every check with what it found -- launch (a bypass flag, auto mode), confinement (tool calls not sandboxed, sandboxes that failed to start), exposure (each credentials section of the site's risk rules, the agent's count with the other classes beside it; the values are never shown, and the list stays on Risks) and reach (agents that wrote outside their start folder, agents that reached another user's home) -- each with its share of what its axis could judge and a link to the panel that shows it. Like *Needs attention* it holds no threshold of its own, and three kinds of nothing stay three: a check made that found none is a ring, one not made (its panel waited for, still being read, not reduced or not returned, or the agent class filtered out) a dashed ring and the reason, never none. `sandbox` and `approval` stay independent axes: each check is one question, and none is crossed with another into a score. Then the **sandbox** panels, which were Internal's: sandboxed × approval at launch as independent axes; tool-call outcomes per session (sandboxed / not / **sandbox failed to start**), wrapper calls by outcome, launch flags as written, processes by sandbox wrapper, kind and sandbox state (`ancestry_matrix`) with `reading` surfaced, under their old titles and anchors. Then *Writes outside the start folder*: every agent that wrote or deleted outside the folder it was started in (the service serves each one however quiet, so the list is whole), by the kind of place the paths are in, the most alarming first (`src/lib/places.ts`, one ranking with the Agents tab's flags), then by user with each agent's paths; the paths are read from the text of its commands, not measured |
| **Login nodes › Resources** (`?tab=resources`; `?tab=io` opens it too) | `node`, `resources`, `io_process` | *Login node health* first, the node tier's newest snapshot of each host, one table at a time (nodes: load, CPU, memory, the collector's probes; NFS mounts; local disks, full past the service's line; accounts against their CPU limit; stuck processes and what they wait in; `src/tabs/NodeHealth.tsx`); then what an operator acts on: the worst individual processes (D-state wait, major faults, runqueue wait, each with its pid and command line), disk read and written by tool (a row per tool, a bar for each on one scale; the 14 heaviest on each counter), network bytes and the endpoints they went to, per-user read/write/network. The per-class tables behind them are on **Internal** |
| **Login nodes › Risks** (`?tab=risk`) | `risk` | dangerous commands on a login node, credentials on command lines (counted, never shown), and build / interpreter CPU on the login node |
| **HPC** (`?tab=slurm`; `?tab=hpc` opens it too) | `slurm`, `submits` (`/api/live`) | the Slurm scheduler, an entry of its own in the rail (it was a sub-tab of Login nodes). *Abnormal Slurm activity* first, under the plates: the service's rules, each worded from the threshold it sends with it (`reducers.slurm.abnormal`) -- polling the queue fast (every 30 s or less for half an hour; the Slurm tab's polling loop, how an agent waits for its job, is no alarm), a burst of queue checks or of submissions in any 60 s, refusals, `scancel` calls or failed `srun`/`salloc` runs again and again -- with each class's count of the sessions that broke it, counted over every session the service judged, those it did not send included, then every session that broke one, with its own numbers as the evidence (`polled the queue every 5.0 s for 5.0 h`), opened into its detail. The service judges every session and serves every one that broke a rule however quiet; the page holds no threshold, and says none only of a panel the service reduced and judged. Then sessions at the scheduler (with a *broke a rule* filter), how each class uses it, the submissions it refused, the latest submissions; gone where `slurm.enabled` is false |
| **Ask LLM** (`?tab=chat`) | `/api/ask`, `/api/ask/{job}`, `/api/suggestions/done` | ask the configured agent about the window on screen, or about the whole window, its answer's citations linking to the panels it read; *Suggestions for the operator* as cards, one marked done kept for every reader until undone; off unless the site turned `ask` on (`src/tabs/ChatTab.tsx`) |
| **Internal** | `resources`, `io_process` | panels not settled yet, and the detail behind the operator tabs: per-process distributions (quantile spine), faults / delays / D-state accounting shown as `not measured` where unread, requested bytes against disk bytes per class (the gap is the page cache; the same bytes over time are Live's), I/O distributions. They keep their titles, so a citation of one lands on it (`src/lib/cites.ts`). The sandbox panels were here until their takeaway settled; they are on **Security** now |
| **Feeds** | `/api/feeds`, `/api/collectors` (every 20 s), `/api/config` | *Collectors*, the collector on each login node and how its runs ended; then *Feeds*, each by its plain name and its key: configured **vs** resolved path, status pill, newest record against its own limit (`max_age_s`), files, notice; *Startup read*, how far the read-back of history has got; *Configuration*, the resolved settings and where each came from |

### Sessions: tasks, phases, and processes

Every session is drawn at three levels, which the page names **Tasks**, **Phases**, and **Processes**
(`LEVELS`, `src/lib/phases.ts`): an agent's from its tool calls, and a session no agent ran (a person's login,
an editor's terminal) from the commands its terminals ran, which its block says (`driver`: `agent` or
`terminal`). The service builds them and holds every threshold (`dashboard/README.md`, *Sessions: tasks,
phases, and processes*); the page has none of its own. The words and the geometry for them are in
`src/lib/phases.ts` and `src/lib/tasks.ts`, so the table, the chart, and the list say one thing, and say it in
the driver's words: a count, a phase's detail, a task's line, and the housekeeping line say `tool calls` for an
agent and `commands` for a terminal (`callWord`, `driverOf`), an idle phase is the user's turn or the agent
thinking for one and reading, thinking, or away for the other, and the basis line is the block's own
(`tasksBasis`). A row a service that read agents alone sent `{unavailable}` still says so under an agent
(`phasesUnread`).

- **The table row** (`src/tabs/TrajectoriesTab.tsx`, `src/components/PhaseBand.tsx`) draws the session's
  phases as a thin band, with a tick across it where each task or lone stretch after the first span begins.
  The tick is a 1 px line at the later span's first call, which across a cutting gap is the end of the gap.
  Under the band a line names the tasks, the first two in order and a count of the rest:
  `“Fix the loader” → Run train.py · config/base.yaml → 3 more`. It is two lines at most, with an ellipsis,
  and its hover has the whole of it. A row whose block has no task keeps the order its phases came in,
  `explore → edit → test`, in the same line, and has no tick. A session running now whose first call has not
  ended has no phase yet (`runningUnread`): its band is its live edge alone, from its first record to now, its
  line is the edge's name (`running now · python, srun`), and it has no category chip, since `no tool calls`
  would be wrong while its calls run.
- **What kind of session it was** is a chip on the row, beside where it was driven from
  (`src/components/CategoryChip.tsx`): the use that holds half its work (`explore`, `edit & test`, `build`,
  `run`, `Slurm`, `git`), `mixed`, `waiting`, or what it had none of (`not recorded`, `no tool calls` or, for
  a session no agent ran, `no commands`, both dashed, as everything unread is). Its dot is the colour of the phases its use counts, so the chip and the
  band beside it agree. Its hover says how the category is decided, the shares of the work, and the
  session's summary. The words are `CATEGORIES` in `src/lib/phases.ts`; the service decides the category.
- **The inspector** (`src/tabs/TrajectoryInspector.tsx`) reads top down, the session's story before the
  processes that back it. First who ran it and what it came to (when, how long, its tool calls or commands,
  processes, CPU, how it was launched), the session's headline, and the category chip with the summary, the
  session in a few sentences that the service builds from the block's own numbers (`Mostly ran jobs on Slurm
  (94% of 5.1 h of work). 564 tool calls in 7 tasks, …`). A session running now with no call ended says what
  the census shows running in the summary's place (`runningUnreadText`), and its *What it did* is its live
  edge alone. Then two parts, each under a heading:
  - **What it did**: a map of the whole session (`src/tabs/SessionMap.tsx`), its **Tasks** as a lane of
    numbered blocks over its **Phases** as a band, on the session's own clock, drawn from the row's block
    before any process is read. The chart below is the read's, and a long session's read stops at its
    budget, so that chart can hold the first hour of a ten-hour session; the map holds all of it. Under
    the map is the list (`src/tabs/TaskList.tsx`): each closed task names its phases in order with the
    chip of each (`explore 60 min → edit → test → git`), a task opens into its phases, a phase into the
    commands that ran most in it, and a button from there to its calls in the tree. A block with no tasks
    has the phase list alone (`src/tabs/PhaseList.tsx`). An agent of a type whose commands are not read
    says so here instead.
  - **Processes**: the purpose ribbon and the tool tokens (they count processes, so a poll loop is most of
    the ribbon however little of the work it was), what the read covers and its limits, then the chart of
    the read, with a **Tasks** lane above the **Phases**, which are tinted behind the **Processes**, the
    bars, and under it the tree with its own controls (tree or table, oldest or newest first, expand,
    collapse, pop out) on the line above its find bar.

  A click on a task or a phase, in the map, the chart's lane, or the list, picks it: the chart zooms to it
  and the tree opens at its first call, reading the page that holds it if the read does not.
- **✦ annotate purposes** (`src/components/Annotate.tsx`), in the bar over the tree, when the site
  turned `annotate` on: a language model's reading of what each tool call was for. The inspector shows
  what is already known as soon as the tree is in, and asks the model about the rest on the button. A
  purpose is a dashed purple ✦ tag beside the command in the table and a ✦ beside a box's name in the
  tree, and every hover names the model and says it is not measured. The answers reach the two views
  through `LlmPurposes` (`src/lib/annotate.ts`), keyed by the command line exactly as the tree serves
  it; nothing else on the page reads them ([the service's side](../README.md#tool-call-purposes-from-a-model)).
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
  chart (`housekeeping, not the model: …`, as the `app assets` shape) and are no call in any phase. So is a
  `cat` with no argv that ran within 2 s of one of them.
- **A session across midnight** is a row to a day, and each row serves the one block, so each draws the
  whole session's band, line, and list. A task or a phase picked in the list that the other day's row holds
  opens that row and goes there (`jumpTo` in the tab), whenever the window holds that row: its last record,
  as the block's `days` gives it, is at or after the window's start. The table need not list it. Where the
  window does not hold it, the pick is made on the open row.

### Live transport

`src/api/useLive.ts` prefers the websocket. After five failed socket attempts in a row it
also polls, every 15 s, what the socket would have sent: `GET /api/live`, and `/api/windows`
and `/api/feeds` on the same beat, so a build that finishes still reaches the panels
(`buildNonce`, from the ready windows) and the feeds report stays current. It never gives up
on the socket: it tries again after a wait that doubles from 1 s to 30 s, each wait spread
over its upper half by a hash of when the page opened (`src/lib/backoff.ts`; no random
number), and at once when the browser comes back online or the tab is shown again. Only a
socket that has stayed up for 75 s resets the wait and ends the polling, not its opening nor
its first frames, which the service sends as soon as it connects: a proxy that accepts the
upgrade and drops it soon after would otherwise be knocked on every second, and the polling
stopped each time. A socket not open within 15 s (a proxy that takes the connection and never
answers the upgrade), or that says nothing for 75 s once open -- the service sends a `feeds`
frame every 30 s -- is closed and replaced, and the page stops saying `live`. The transport is always named, at the rail's foot
(`updates streaming` / `every 15 s` / `connecting…` / `disconnected`) and in the header's pill
(`live` / `refreshing every 15 s` / `connecting…` / `disconnected · retrying`), because a frozen
chart that looks live is worse than one labelled as polled. A `rebuilt` frame triggers a re-read of `/api/panels`; a
`feeds` frame supersedes the REST feeds snapshot. The header's refresh asks for all of it
again: the live frame, the windows, the feeds, the panels and the sessions.

### Filters, URL state and export

Every filter — plus the active tab, the window (`win`), the trajectory rank and the inspected
trajectory (`traj`, `tfrom` when a read was continued past its budget, and `torder=newest` when
it is read newest first), the user whose sessions Sessions opens on (`tuser`, from Active
users), and the portrait open on the Users tab (`portrait`) — lives in the query string, so
a reload or a pasted link restores the view. `src/lib/filters.ts` owns the one
serialisation: `apiQuery()` builds the string sent to **both** `/api/events` and
`/api/export`, so the download is exactly the view by construction rather than by
convention. The row count next to the Download button is the user's confirmation of that.
Search & export, the tab that draws that table and its button (`?tab=events`), is hidden for
now (`HIDDEN_TABS`, `src/App.tsx`): a link to it opens Live.

Page-wide params: `class`, `user` and `atype`, each repeatable (`agent_type` is still read, as
old links wrote it). Search & export's own: `from`, `to`, `host`, `tool`, `purpose`, `bucket`,
`session_key`, `sandbox`, `approval`, `depth_min`, `depth_max`, `cpu_min`, `duration_min`,
`exit_code`, `signal`, `q`.

## Known gaps

- Search & export's event table, hidden for now, is **paged** (`next_cursor`), not virtualised. Fine for the backend's
  page size; a 10⁵-row single view would need windowing.
- `/api/panels` payloads that this UI does not render (anything beyond `tool_mix`,
  `io_process`, `resources`, `sandbox`, `cascades`, `node`, `trend`, `risk` and `slurm`) are
  ignored rather than shown generically; Sessions reads `/api/trajectories`, not the panels'
  `trajectories`.
- The `purposes` list returned by `/api/panels` is not consulted — the colour table of
  three kinds and the grey tail is fixed in `src/lib/purposes.ts` so the palette cannot grow
  a hue at runtime. A new purpose folds into the grey `other` until the table names its kind.
