# `dashboard/web` — live agent-behaviour dashboard (frontend)

This is the React + Vite + TypeScript SPA for the live HPC agent-behaviour dashboard. It reads the
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
npm install
npm run dev          # http://localhost:5173 — /api/* and /ws answered from mock/api.json
npm run dev:real     # /api, /ws, /login, and /logout proxied to a running backend
```

`npm run dev` needs **no backend**: a Vite middleware (`mock/plugin.ts`) answers every
`/api/*` route and performs a real websocket handshake on `/ws` from the static fixture
`mock/api.json`. The app doesn't know the middleware is there: `src/api/client.ts` has no
mock branch, so dev exercises the production code path, over HTTP, with real status codes.

For `dev:real`:

```bash
RC_BACKEND=http://127.0.0.1:8787 npm run dev:real
# or
npm run dev -- --mode real          # same thing
RC_MOCK=0 npm run dev               # same thing
```

The backend refuses a websocket opened by a page from any origin other than its own,
including another port on the same host (`rc_dashboard/guard.py`). Vite serves its page on
its own port, so the backend has to be told about that origin. `../run.sh` does this for
you. A backend started any other way needs the origin set explicitly, spelled the way the
browser will send it: `RC_DASH_DEV_ORIGINS=http://127.0.0.1:5173,http://localhost:5173`
(`server.dev_origins`, which is added to `server.allowed_origins`). Otherwise the page's
`/ws` gets a 403 and the Live tab doesn't update.

Regenerate the fixture after changing its shape. Generation is deterministic (no RNG) and
byte-stable:

```bash
TZ=America/New_York npm run fixture      # node mock/gen_fixture.mjs -> mock/api.json
```

The Live tab's clock strings (the rate chart's minute labels, and the `ts` of its events and submissions) are
written in the generator's local time. The committed file was generated in `America/New_York`. If you
regenerate it in another zone, those strings change and nothing else does. The Users section (`users`) is
built from the fixture's own trajectory rows, the same way `rc_dashboard/portraits.py` builds the service's.
Its hours are in a fixed EDT, so it comes out the same in any zone.

The fixture deliberately contains the awkward states the UI has to get right: a `missing`
feed, a `stale` feed whose `lag_s` exceeds its own `max_age_s`, an `empty` feed, `null`
rate bins (including a 23-minute outage), accumulators with `n: 0, coverage_pct: 0`, a
sandbox slice that `exited before it was checked` (key `denied`), a session in which every
sandbox attempt failed, and a trajectory ranking dominated by one user. To see the
`<EmptyState>` path, set any rendered panel's `_present` to `false` in `mock/api.json` and
reload. The middleware re-reads the file on every request. To see `<NotReturned>`, delete a
panel's key instead (the fixture already leaves out the Slurm and risk-rules panels). To see
`<NotReduced>`, which marks a panel whose reducer raised, give a panel an `_error` string.

The node tier, the day-by-day panel and the exit verdicts have awkward states of their own: a
node whose probes are ten times the rest, an NFS hiccup in the same bins as the resources
series' stall, a host whose snapshots stop for 40 minutes (a hole), a full `/tmp` and a `/var`
out of file slots, an account held back at its CPU limit and one whose cgroup was recreated,
processes stuck in NFS waits, a day the collector wrote nothing in, the oldest day cut off where
retention starts, a test runner failing most of its runs, a command not found, a timeout's
SIGKILL, grep's exit-1 answers and `| head`'s SIGPIPE. Since there is no Slurm or risk panel,
Needs attention lists both as not checked.

The fixture's agent rows carry each kind of task the page has to put into words: tasks quoted
from a commit, from a log entry, and from a pull request with a number and one without; tasks
built from names; a lone stretch; a gap that names a call still running and one that names none;
a task still open at the live edge; a task under a call log that was cut; and one agent spread
over two day rows, which serve one block. `npm run check:phases` reads every task and phase of
every row in the fixture, and `npm run check:figures` draws every user's portrait figure and
checks each part against that user's row.

The fixture's sessions sit at its own `now_epoch`, so every live frame the mock sends carries
that time as the service's clock (`clock.now`), and the page's "now" follows it. That way a
running session's live edge is minutes long, rather than spanning the days between the fixture
and today. The clock stands still, like a capture's clock. Run `RC_MOCK_PINNED=1 npm run dev`
to also mark the clock as pinned (`clock.pinned`), as a frozen capture does, and see what the
page says in that case.

The inspector's read (`/api/trajectory`) is one hand-built tree with its navigator (process
starts per bin, the landmarks, the failures). It is laid over the row's own span the same way
the service reads a session: from the first record (or from `?from=` when the inspector pages),
for as long as the tree runs. So a long session stops at its budget, with the rest unread. As a
result, the row's phases, the chart's process starts and the session track agree about when the
session ran, and the phases behind the bars can be clicked through across pages. This happens in
`mock/plugin.ts`, so changing it requires a dev server restart (`api.json`, by contrast, is
re-read on every request).

## Build

```bash
npm run build        # tsc -b && vite build  ->  dist/
npm run typecheck    # tsc -b, no emit
npm run check:charts # node --test: the chart tick and width rules in src/charts/scale.ts
npm run check:css    # node --test: every stylesheet closes its blocks, no rule nested in a rule
npm run check:shares # node --test: a class's share is out of every class counted, whatever the chips show
npm run check:panels # node --test: a panel not reduced, waited for or building is never "none" or 0
npm run check:live   # node --test: the live socket is retried for good, polled meanwhile, replaced when quiet
npm run check:lookup # node --test: no outside string reads a table's prototype; a part that throws says so
npm run check:procexit # node --test: a run's box says how it ended as the process table does
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
under a sub-path. The API is addressed by absolute path at `/api`, and the socket at `/ws`.
If the backend lives elsewhere, override them with `VITE_API_BASE` / `VITE_WS_URL` at build time.

Every build stamps the bundle with its origin, and the foot of the rail shows the stamp:
`commit 0d516df` (HEAD at build time, plus `+ edits` if anything under `web/` differed
from HEAD) and `built 2026-11-15 14:02` (local time; hover for the full timestamp). The
stamp comes from `virtual:build-info`, which the plugin in `vite.config.ts` writes. It is
re-read on each build instead of being fixed in a `define`, so the single `vite build --watch`
that `./run.sh build` keeps alive stamps each rebuild with that rebuild's time. The dev
server builds nothing and says so (`built — (dev server)`), and it re-reads the commit on
every page load. When the bundle is built without git or outside a checkout, the commit
reads `—` instead of a guess.

## Dependencies

Runtime: **`react`, `react-dom`.** That's all.

Dev: `vite`, `@vitejs/plugin-react`, `typescript`, `@types/{node,react,react-dom}`.

There is **no charting library**. Every chart is hand-rolled SVG in `src/charts/primitives.tsx`,
ported from the retired demo template (git history: `dashboard/archive/`). There are two
reasons, and both are constraints here rather than preferences. First, no code path can
introduce a second value axis. Second, the colours are the CSS custom properties used
verbatim, so no library theme can substitute a different hue. There is no table library
either; the event explorer pages using the backend's `next_cursor`. There is no icon library:
`src/components/Icon.tsx` draws the page's few icons as paths in the text colour, so an icon
is never drawn in a series colour.

## The rules this code enforces

These come from the retired demo's README (git history: `dashboard/archive/README.md`).
They are research-integrity constraints. Breaking one is a reporting error, not a styling error.

1. **Three classes, never two**: `agent` / `human` / `ide` (+ `unlabeled`).
   The list is closed in `src/lib/classes.ts` and labelled `agent` / `human` / `IDE`, always
   written in full, never abbreviated. Folding the IDE class into "agent" is what produced the
   retracted GPU near-parity claim. `<Plate>` **requires** a per-class breakdown, so a headline
   number cannot ship without its split.
2. **Count and cost side by side**: every categorical panel goes through `<CountCost>`,
   which renders an event-weighted chart and a CPU-weighted chart, each on its own single
   axis. `GroupBars`/`Lines` take one `ylabel` and have no second-axis option, so there is
   no way to express a dual axis.
3. **Colour**: the Harvard palette, light only, with tokens copied from the archived
   template. Crimson `#A51C30` (`--s1`) is reserved for `agent`. There are eight series slots
   plus one grey tail (`--sg`). The purpose table in `src/lib/purposes.ts` folds everything
   outside its eight slots into `other` instead of inventing a ninth hue. A series that is
   not a class (a tool, or a direction such as sent and received) takes its hue from
   `TOOL_COL` (`src/charts/TimeChart.tsx`), never from a class.
4. **Zero is not missing**: `_present === false` renders the shared `<EmptyState>`, which
   shows the panel frame, the title, the feed name, and one line per `_paths` entry, verbatim
   (`no data from /var/log/ebpfm`). A feed that IS present but has no rows renders
   `<NoRows>`, which is the word `none`. The two look nothing alike because they are
   different facts. A panel the backend did not return (a response without its key, or a
   request that failed) renders `<NotReturned>`: one dashed line reading `not returned`, the
   reason, and the feed the panel reads. It shows no feed status and names no path, because
   no feed reported anything. Whether the feed is healthy is for the Feeds tab to say.
5. **Nothing fabricated**: there is no `Math.random()` anywhere in `src/`
   (`npm run check:no-random` checks this), and nothing interpolates between frames. A
   `null` rate bin arrives as `null`, and `Lines` emits a separate subpath around it, so a
   minute with no record is a **hole in the line**. It is never a 0, and never a straight
   line drawn across the hole. The dev fixture lives in `mock/`, outside `src/`, so it cannot
   reach the production bundle. The service's demo mode (`rc_dashboard/demo`) feeds the page
   records that were never measured, and the page says so on every tab. The feeds report
   lists the hosts whose records carry `"synthetic": true` (`synthetic_hosts`). While any
   host does, every tab opens with a *Fake data* note, the header shows a dashed **fake data**
   pill beside the live one, and the Feeds tab tags each such host.
6. **Coverage is part of the number**: `<Coverage>`, `<AccumCell>` and `<QuantCells>`
   print `coverage_pct` next to every value that has one. An accumulator or quantile
   with `n: 0` renders the words **`not measured`**, never `0`.
7. **Data only**: a panel carries a title, its feed, and the chart or table. Every
   `notes` / `basis` / `cpu_basis` / `scope` / `dedup.rule` string goes into `<Basis>` (a
   collapsed `basis` affordance at the foot of the panel) or into a `data-tip` tooltip.
8. **Down to a phone**: at 900px and under (`src/lib/useNarrow.ts`), the rail becomes a
   drawer opened from a top bar that names the tab, and every grid collapses to one column.
   Wide tables scroll inside their own `.scroller`, and the widest keep their label column
   in view (`pin`). The page body never scrolls horizontally, down to 320px. A table's edge
   fades where there is more to scroll to (`src/lib/scrollEdges.ts`), so a cut-off column
   doesn't pass for the last one. A panel's controls drop below its title, and its switches
   wrap. A time series is drawn at its container's width, at 1:1 (`<Fluid>`,
   `src/charts/primitives.tsx`). In the narrow layout, so is every other chart whose box is
   narrower than the chart (`useFit`), so a 10px label stays 10px instead of printing at 3px.
   On a narrow plot, the ticks thin out (`tickTarget`) rather than overprint. On a touch
   screen, a tap shows a `data-tip`, controls are a fingertip high, and form fields are 16px
   so iOS does not zoom into them. Above 900px, with a mouse, the page is unchanged.
9. **No outside string reads a prototype**: a key that comes from the URL, the service or
   the collector either indexes a table made by `table()` (`src/lib/lookup.ts`), which has
   no prototype, or is read with `own()`. Once, `?atype=__proto__` handed the filter
   `Object.prototype` as its label, which React cannot draw, and the page went white.
10. **A part that throws says so**: there is one error boundary around the page
   (`src/main.tsx`), one per tab (`TabBoundary`, `src/lib/lazyTab.tsx`), and one per panel's
   body (`<Panel>`), each from `src/components/Boundary.tsx`. A failure shows
   "this tab could not be drawn: … · reload" in the frame of the missing family, and the
   rest of the page stays up. A tab whose code would not load (a deploy replaced it under an open page) is one
   such failure. A panel that is handed new data tries again.

Two more rules are forced by the payloads:

- **Both grains, where both exist.** The Internal tab's sandbox panels show per-process
  *and* per-session figures (a session is judged by its tool calls). Live's *Who uses what*
  shows the same fleet in eight units, because the class ranking is not stable across them.
- **`denied` is not `unsandboxed`.** There are four sandbox states. `denied`, labelled
  "exited before it was checked", means the namespace links were already gone when the
  collector looked (the collector runs as root; these are processes that last a few ms). So
  `denied` gets its own hue *and* a hatch (`src/components/Defs.tsx`). Every share is one
  state's share of the KNOWN states, with coverage beside it (`src/lib/sandbox.ts`), so
  neither `denied` nor `unknown` sits silently in a denominator.

## Words

Each thing has one word, on every tab. Labels use the word in the first column. The words in
the last column are not used for that thing anywhere on the page. `src/lib/glossary.ts` holds
the same table, plus the line each word shows on hover (its `?`). That line carries the
precise reading: what is measured, and what the measurement cannot see.

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

The tabs are Live, Users, Agents, **Sessions**, Login nodes (Resources, **Risks**, Slurm), Ask LLM,
Internal and Feeds. Tab URL keys (`?tab=trajectories`, `?tab=risk`), API fields and filter
keys keep their old names, so links, citations and exports are unchanged. Only the page's
wording moved. A panel whose title changed keeps its old anchor (`id=`), which
`src/lib/cites.ts` points at.

## The window control

The window picker (`src/components/WindowPicker.tsx`) sits in one control bar with the class
filter and the user and agent-type dropdowns. The window and the filters both answer "what is
on screen", so a reader checking one shouldn't have to hunt for the other. The bar sticks to
the top of the page as a tab scrolls (`src/lib/sticky.ts`), so the answer is still in view at
the foot of a long tab. The bar publishes its height as `--controls-h`. The page uses that as
its `scroll-padding-top`, so nothing the browser scrolls to (a control reached with Tab, a
panel picked in the section list, the trajectory inspector) ends up under the bar. The Agents
tab's sticky list and detail also stick below that height. On a phone (640px and below), the
bar scrolls with the page. The picker is backed by the URL as `?win=<minutes>`, like every
other view setting.

The picker follows three rules, each a case of the one rule this page follows everywhere:

- **Presets come from the service**, filtered server-side against retention, so the picker
  cannot offer a span the process does not hold.
- **A clamp is shown, never silent.** If you ask for 30 days with 7 days of retention, the
  control reads `asked 30d · showing 7d`, with the reason on hover.
- **The kinds of nothing stay apart.** A panel whose window is still being reduced renders
  `<Building>` (a solid frame and a percentage). It does not render `none`, which would
  claim a measurement, or the empty state, which would blame the feed. A panel the backend
  did not return renders `<NotReturned>` (rule 4), never the empty state. A panel whose
  reducer raised arrives as its envelope with `_error` and nothing else
  (`windows.materialize`). It renders `<NotReduced>`, which shows
  "the service could not reduce this panel" and what the service said, and `pick` passes it
  on as not present, so a plate reads a dash rather than a 0. A tab only says `none` of a panel when the service
  reduced a body for it (`hasBody`). While the panel is being waited for, building, or not
  reduced, the panel says which of those it is.

Plate labels take the window from the **panel's own envelope** (`winLabel`), not from the
picker. During a rebuild the two really do differ, and the plate is the one that must be right.

## The rail and "go to"

The rail lists the tabs in reading order under two words, Monitor and More, each with its
icon, and lists the open tab's sections under that tab. Resources, Risks and Slurm share one
entry, named with the site's own word for its hosts (`ui.node_noun`, "Login nodes"; their
figures are login-node use, not the cluster's). Each of the three has a sub-tab along the
foot of the head. A sub-tab keeps its own key (`?tab=resources`, `risk`, `slurm`), so links
and citations still land where they did, and the entry reopens the last-used sub-tab. The
old Overview tab's one table is now a section of Live (*Who uses what*), and
`?tab=overview` lands on it. As the page scrolls, the rail marks the section at
the top of the screen (or the first section, on a page too short to scroll). A section picked
from the list or from go-to stays marked while it is on screen, even near the bottom of a page
where it cannot reach the top. The rail folds down to its icons alone (`[`, or the button at
its foot), and this browser remembers that (`localStorage`, a per-viewer convenience; if
storage is absent or refuses, the rail just stays unfolded). The rail's foot says how the page
gets its data: the transport as a light, the last update, the window, and when the panels were
built.

**Go to** (`src/components/CommandPalette.tsx`) opens with Ctrl+K or Cmd+K anywhere, or with
`/` outside a field. Type to reach a tab, a section of the open tab, a window preset, or an
action (refresh, show all three classes, fold the rail). It offers only what the page already
holds, so it can never name a tab, section or window that is not there.

## Keyboard

Every control is a tab stop with a visible focus ring. (Controls restyled from `all: unset`
re-declare the ring at the foot of `src/styles.css`.) A sortable header
(`src/components/SortTh.tsx`) sorts with Enter or Space. A control's `data-tip` shows under it
when the control is reached with Tab (`src/components/Tooltip.tsx`), because that tip is where
the caveat about how a number is counted lives. The tip is visual only: its layer is
`aria-hidden`, and no control points at it with `aria-describedby`, so a screen reader does
not announce it.

| key | does |
|---|---|
| Ctrl+K, Cmd+K | opens or closes **go to**, anywhere |
| `/` | opens **go to**, outside a field |
| `[` | folds or unfolds the rail, outside a field |
| ↑ ↓ Enter, Esc | in **go to**: move, go, close (focus returns where it was) |
| Esc | closes the rail's drawer below 900px |

## Tabs

| tab | source | notes |
|---|---|---|
| **Live** | `WS /ws`, `GET /api/live`, `/api/panels?window_min=`, its `node` and `trend` | *Needs attention* first: what the service already flags (a feed past its own max age, risk-rule hits by agents, credentials on command lines, sessions polling the queue, refused submissions, full local disks, accounts held back at a CPU limit, NFS retransmissions), each with its per-class split and a link to its panel, and every source it could not check named, never read as an all-clear (`src/tabs/Attention.tsx`); then three-class stat tiles, per-minute rate chart over the **selected window** with real gaps, *Who uses what* (each class's share of the window in eight units, the old Overview tab; `?tab=overview` lands on it), per-login-node table with a per-class split, bounded event stream, submissions; *Day by day*, each class per day over retention (`trend`, not the window, and not narrowed by the filter, which it says), count and cost side by side, a day with no record a gap (`src/tabs/DayByDay.tsx`); and *Login node response*, the node tier's probes, slowest NFS round trip and load per host over the window, zoomed with the CPU chart (`node.series`, `src/tabs/NodeResponse.tsx`) |
| **Users** | `/api/users`, `/api/user?name=` | a portrait of each user from how they use coding agents (`src/tabs/UsersTab.tsx`, words in `src/lib/portraits.ts`): a roster, one row per user with an agent session in the window (agents, where sessions are driven from, launched with a bypass flag, tool calls by what they do, a typical session, how many at once, active minutes by hour, and traits, with a line under each saying in one sentence how they leverage agents), filterable by trait, and shown as a table or as **figures**, each user drawn from their numbers (`src/components/PortraitFigure.tsx`: a part for each number, its numbers and rule on hover, a key beside it); a row or a figure opens the user's **portrait** (`?portrait=`): their figure, how they leverage agents, the sentence and then point by point with the numbers each rests on and the window's medians beside them, the traits with the rule behind each, launch shares with the window's share as a tick on each bar, the hours and the sessions over the window, calls counted beside time by phase, projects and tasks in the agents' own words, the scheduler, and every session, each a link to its process tree. The service holds every threshold (`rc_dashboard/portraits.py`); the page only words what it counted |
| **Agents** | `cascades`, `tool_mix` | agent and subagent behaviour (`?tab=agents`, `?tab=cascades` still opens it): every agent tree, listed by user (the default) or as one list, the latest first (agent / infra / client nodes; `claude daemon`'s idle spares are infra, counted not drawn; how each parent was found, each agent's start folder, directories and writes outside it), agents with **subagents** kept apart from **hosted** sessions (a daemon or server running them); **Workspace & blast radius** per agent with a *this agent only / with its subagents* toggle: where its processes **ran** (measured), the **bytes** they moved (measured per process), the paths its command lines **named** (inferred, drawn dashed), writes and deletes outside its start folder flagged by where they landed; depth and fan-out histograms, who-starts-whom, and the count-and-cost census of everything the classifier tagged; then the **Tool mix** and **Top tools** (the old Sessions & tools tab: the 12-way bucket mix, counted then costed, by class or by kind of tool; per-class head); *Commands that failed* (`tool_mix.exits`): how the tool mix's processes ended, per class, agent type and tool (a failure is a non-zero exit or a signal, past grep's exit-1 answer and SIGPIPE), with each row's commonest exit codes and signals, and each count opening the commands behind it (`outcome` filter; `src/tabs/ExitsPanel.tsx`) |
| **Sessions** (`?tab=trajectories`) | `/api/trajectories?rank=&window_min=` | rank switcher that only **reorders** (processes and CPU always visible), a ribbon (the purpose of each process, or for an agent whose commands are read its phases, with a line naming its tasks: [below](#sessions-tasks-phases-and-processes)) + tool tokens, `dominant_user_share_pct`; a **pieces** count for how many collector session keys each row joins and how they were attributed. Clicking a row opens the **inspector** (`/api/trajectory`, `?traj=` in the URL), which reads top down: who ran the session and what it came to, its headline and gist, then **What it did** (for an agent whose commands are read: a map of its tasks over its phases on the whole session's clock, and the list of tasks, each with its phases named under it), then **Processes**: the purpose ribbon and tool tokens, what the read covers, the chart of the read, and the process tree (indentation guides, collapsible subtrees, start / duration / own and subtree CPU / exit or signal per process, a timeline bar on the read's own axis, repeated siblings folded into counted groups, weaker parent links and processes with no exit tagged, and every read bound stated above the tree). **oldest first / newest first** (or the table's `started` header) asks the service for the other end. Newest first, the newest processes are drawn in full and the oldest are only counted |
| **Login nodes › Resources** (`?tab=resources`; `?tab=io` opens it too) | `node`, `resources`, `io_process` | *Login node health* first, the node tier's newest snapshot of each host, one table at a time (nodes: load, CPU, memory, the collector's probes; NFS mounts; local disks, full past the service's line; accounts against their CPU limit; stuck processes and what they wait in; `src/tabs/NodeHealth.tsx`); then what an operator acts on: the worst individual processes (D-state wait, major faults, runqueue wait, each with its pid and command line), disk read and written by tool (each ranked on its own counter), network bytes and the endpoints they went to, per-user read/write/network. The per-class tables behind them are on **Internal** |
| **Login nodes › Risks** (`?tab=risk`) | `risk` | dangerous commands on a login node, credentials on command lines (counted, never shown), and build / interpreter CPU on the login node |
| **Login nodes › Slurm** (`?tab=slurm`) | `slurm`, `live_now.submits` | sessions at the scheduler, how each class uses it, the submissions it refused, the latest submissions; gone where `slurm.enabled` is false |
| **Internal** (`?tab=sandbox` opens it too) | `sandbox`, `resources`, `io_process` | panels not settled yet, and the detail behind the operator tabs. **Sandbox**: sandboxed × approval at launch as independent axes; tool-call outcomes per session (sandboxed / not / **sandbox failed to start**), wrapper calls by outcome, launch flags as written, processes by sandbox wrapper, kind and sandbox state (`ancestry_matrix`) with `reading` surfaced. **Resources and I/O**: per-process distributions (quantile spine), faults / delays / D-state accounting shown as `not measured` where unread, requested bytes against disk bytes (the gap is the page cache, with the per-class table), I/O distributions. They keep their titles, so a citation of one lands on it (`src/lib/cites.ts`) |
| **Feeds** | `/api/feeds`, `/api/config` | the collectors on each login node, then each feed by its plain name and its key: configured **vs** resolved path, status pill, newest record against its own limit (`max_age_s`), files, notice |

### Sessions: tasks, phases, and processes

A session of an agent whose commands are read (Claude Code and Codex) is drawn at three levels, which the
page names **Tasks**, **Phases**, and **Processes** (`LEVELS`, `src/lib/phases.ts`). The service builds the
levels and holds every threshold (`dashboard/README.md`, *Sessions: tasks, phases, and processes*). The page
has no thresholds of its own. The words and the geometry for the levels are in `src/lib/phases.ts` and
`src/lib/tasks.ts`, so the table, the chart, and the list all say the same thing.

- **The table row** (`src/tabs/TrajectoriesTab.tsx`, `src/components/PhaseBand.tsx`) draws the session's
  phases as a thin band, with a tick across the band where each task or lone stretch after the first span
  begins. The tick is a 1 px line at the first call of the later span; across a cutting gap, that is the
  end of the gap. Under the band, a line names the first two tasks in order and counts the rest:
  `“Fix the loader” → Run train.py · config/base.yaml → 3 more`. It wraps to two lines at most, cut off
  with an ellipsis, and its hover shows the whole text. A row whose block has no task shows its phases in
  the order they came, `explore → edit → test`, in the same line, and has no tick.
- **What kind of session it was** is shown as a chip on the row, beside where the session was driven from
  (`src/components/CategoryChip.tsx`). The chip names the use that holds half the session's work
  (`explore`, `edit & test`, `build`, `run`, `Slurm`, `git`), or `mixed`, or `waiting`, or what the session
  had none of (`not recorded`, `no tool calls`, both dashed, as everything unread is). The chip's dot is the
  colour of the phases its use counts, so the chip agrees with the band beside it. The chip's hover says how
  the category is decided, the shares of the work, and the session's summary. The words are `CATEGORIES` in
  `src/lib/phases.ts`, and the service decides the category.
- **The inspector** (`src/tabs/TrajectoryInspector.tsx`) reads top down: the session's story first, then
  the processes that back it. It starts with who ran the session and what it came to (when, how long, its
  tool calls, processes, CPU, how it was launched), the session's headline, and the category chip with the
  summary. The summary describes the session in a few sentences that the service builds from the block's
  own numbers (`Mostly ran jobs on Slurm (94% of 5.1 h of
  work). 564 tool calls in 7 tasks, …`). Then come two parts, each under a heading:
  - **What it did**: a map of the whole session (`src/tabs/SessionMap.tsx`). The map shows the session's
    **Tasks** as a lane of numbered blocks over its **Phases** as a band, on the session's own clock. It is
    drawn from the row's block before any process is read. The chart below it shows only the read, and a
    long session's read stops at its budget, so that chart may hold just the first hour of a ten-hour
    session while the map holds all of it. Under the map is the list (`src/tabs/TaskList.tsx`). Each closed
    task names its phases in order, each with its chip (`explore 60 min → edit → test → git`). A task opens
    into its phases, and a phase opens into the commands that ran most in it, with a button from there to
    its calls in the tree. A block with no tasks has only the phase list (`src/tabs/PhaseList.tsx`). For an
    agent of a type whose commands are not read, this part says so instead.
  - **Processes**: the purpose ribbon and the tool tokens (these count processes, so a poll loop takes up
    most of the ribbon however little of the work it was), what the read covers and its limits, and then
    the chart of the read. The chart has a **Tasks** lane above the **Phases**, which are tinted behind the
    **Processes** (the bars). Under the chart is the tree, with its own controls (tree or table, oldest or
    newest first, expand, collapse, pop out) on the line above its find bar.

  Clicking a task or a phase in the map, the chart's lane, or the list selects it. The chart zooms to it,
  and the tree opens at its first call, reading the page that holds that call if the current read does not.
- **✦ annotate purposes** (`src/components/Annotate.tsx`) appears in the bar over the tree when the site
  has turned `annotate` on. It shows a language model's reading of what each tool call was for. The
  inspector shows the purposes already known as soon as the tree is in, and asks the model about the rest
  when you press the button. A purpose appears as a dashed purple ✦ tag beside the command in the table,
  and as a ✦ beside a box's name in the tree. Every hover names the model and says the purpose is not
  measured. The answers reach the two views through `LlmPurposes` (`src/lib/annotate.ts`), keyed by the
  command line exactly as the tree serves it. Nothing else on the page reads them
  ([the service's side](../README.md#tool-call-purposes-from-a-model)).
- **A label says where it came from.** The agent's own words appear as a quote, in quotation marks, with
  their source (`commit`, `PR #235`, or `log`) where there is room for it. A label built from the names in
  the task's commands has neither quotation marks nor a source, so it is never taken for something the
  agent wrote. The basis line over the chart reads
  `tasks · read from the commands the agent ran and the words it wrote in them`.
- **A lone stretch** (a few calls, hours away from other work) is not a task. It has no number, its block in
  the lane is a dashed outline, the list gives it a line of its own (`3 calls at 10:41:24 · read src/app.py`),
  and neither the numbering nor the row's line counts it. The row's band does put a tick where it begins,
  as it does for a task. A cutting gap also gets a line of its own (`2.1 h with no new calls`). That line
  names the first of the calls that were still running when the agent moved past them, by the name the
  service gives that call, or else by its command (`· scratchpad/linktest still running since 13:05:44`).
- **The Codex app's reads of its own plugins** are housekeeping. They are counted in the line under the
  chart (`housekeeping, not the model: …`, as the `app assets` shape) and are not a call in any phase. The
  same goes for a `cat` with no argv that ran within 2 s of one of them.
- **A session across midnight** gets one row per day. Each row serves the same block, so each one draws the
  whole session's band, line, and list. Picking a task or a phase in the list that the other day's row
  holds opens that row and goes there (`jumpTo` in the tab), as long as the window holds that row, meaning
  its last record (as the block's `days` gives it) is at or after the window's start. The table need not
  list that row. If the window does not hold it, the pick is made on the open row.

### Live transport

`src/api/useLive.ts` prefers the websocket. After five failed socket attempts in a row, it
also polls every 15 s for what the socket would have sent: `GET /api/live`, plus `/api/windows`
and `/api/feeds` on the same schedule, so a build that finishes still reaches the panels
(`buildNonce`, from the ready windows) and the feeds report stays current. It never gives up
on the socket. It tries again after a wait that doubles from 1 s to 30 s, with each wait
spread over its upper half by a hash of when the page opened (`src/lib/backoff.ts`; no random
number). It also tries again at once when the browser comes back online or the tab is shown
again. The wait resets and the polling ends only once a socket has stayed up for 75 s. The
socket opening doesn't count, and neither do its first frames, which the service sends as soon
as it connects. Otherwise a proxy that accepts the upgrade and drops it soon after would be
knocked on every second, and the polling would stop each time. A socket that is not open within
15 s (a proxy that takes the connection and never answers the upgrade), or that says nothing for
75 s once open (the service sends a `feeds` frame every 30 s), is closed and replaced, and the
page stops saying `live`. The rail always names the transport (`transport ws` / `polling` /
`down`), because a frozen chart that looks live is worse than a chart labelled `polling`. A
`rebuilt` frame triggers a re-read of `/api/panels`, and a `feeds` frame replaces the REST feeds
snapshot. The header's refresh button asks for everything again: the live frame, the windows,
the feeds, the panels and the sessions.

### Filters, URL state and export

Every filter lives in the query string, along with the active tab, the trajectory rank, the
inspected trajectory (`traj`, plus `tfrom` when a read was continued past its budget and
`torder=newest` when it is read newest first), and the portrait open on the Users tab
(`portrait`). So a reload or a pasted link restores the view. `src/lib/filters.ts` owns the
single serialisation. `apiQuery()` builds the string sent to **both** `/api/events` and
`/api/export`, so the download matches the view exactly because both requests share one
query builder, not by convention. The row count next to the Download button lets the user confirm that.

Supported params: `from`, `to`, `class` (repeatable), `agent_type`, `user`, `host`, `tool`,
`purpose`, `bucket`, `session_key`, `sandbox`, `approval`, `depth_min`, `depth_max`,
`cpu_min`, `duration_min`, `exit_code`, `signal`, `q`.

## Known gaps

- The event table is **paged** (`next_cursor`), not virtualised. That's fine for the
  backend's page size, but a single view of 10⁵ rows would need windowing.
- `/api/panels` payloads that this UI does not yet render (anything beyond `tool_mix`,
  `io_process`, `resources`, `sandbox`, `trajectories`) are ignored rather than shown
  generically.
- The `purposes` list returned by `/api/panels` is not consulted. The eight-slot colour
  table is fixed in `src/lib/purposes.ts`, so the palette cannot grow a ninth hue at
  runtime. A new purpose folds into the grey `other`.
