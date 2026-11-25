# `dashboard/web` — live agent-behaviour dashboard (frontend)

The dashboard's page, in React, Vite and TypeScript. It reads the `rc_dashboard` API on the
**same origin** and builds to `dist/`, which the service serves.

```
src/api/        the contract: types, fetch client, hooks, the live websocket
src/lib/        classes, purposes, formatters, URL/filter state, panel lookup, the sticky control bar, the words and geometry of phases and tasks
src/charts/     hand-rolled SVG primitives (lines, bars, ribbon)
src/components/ Panel/EmptyState, Plate, Coverage, ClassFilter, PortraitFigure, Icon, CommandPalette
src/tabs/       the tabs and their parts
mock/           dev-only fixture + Vite middleware (NOT bundled, NOT in src/)
```

## Dev

```bash
npm ci
npm run dev                                         # http://localhost:5173, answered from mock/api.json
RC_BACKEND=http://127.0.0.1:8080 npm run dev:real   # /api, /ws, /login, /logout proxied to a backend (unset: :8787)
```

`npm run dev` needs no backend: `mock/plugin.ts` answers `/ws` and the routes the tabs are drawn
from (`/api/config`, `feeds`, `status`, `windows`, `panels`, `trend`, `live`, `trajectories`,
`trajectory`, `users`, `user`) out of `mock/api.json`, which it re-reads on every request. Any other
route (the collectors' health, Active users' searches and accounts, the commands behind an I/O
number, Ask) is a 404 there, so what reads it says it was not returned.
`src/api/client.ts` has no mock branch: dev runs the production code path.

The backend refuses a websocket from any origin but its own (`rc_dashboard/guard.py`), and Vite
serves the page on its own port. `../run.sh` names Vite's origins to the backend; a backend
started any other way needs `RC_DASH_DEV_ORIGINS=http://127.0.0.1:5173,http://localhost:5173`
(`server.dev_origins`), or `/ws` gets a 403 and the Live tab does not update.

`mock/api.json` is generated: edit `mock/gen_fixture.mjs` and run
`TZ=America/New_York npm run fixture`. The output is deterministic, but the Live tab's clock
strings and the Trend tab's days are in the generator's local time, so another zone changes them.
The fixture holds the awkward states the page has to get right, each listed in
`mock/gen_fixture.mjs` where it is made: among them `/api/status` with one collector degraded,
which Summary's first card names. To see `<EmptyState>`, set a panel's `_present` to
`false`; `<NotReturned>`, delete its key (the fixture has no Slurm or risk panel, so Needs
attention lists both as not checked); `<NotReduced>`, give it an `_error` string.

The mock's live frames carry the fixture's `now_epoch` as the service's clock (`clock.now`),
standing still, so a running session's live edge is minutes long; `RC_MOCK_PINNED=1 npm run dev`
also marks it pinned (`clock.pinned`), as a frozen capture does. `/api/trajectory` serves the
fixture's one hand-built tree for every row, laid over the row's own span.

## Build

```bash
npm run build        # tsc -b && vite build  ->  dist/
npm run build:watch  # vite build --watch: dist/ rebuilt on every edit, no type check (../run.sh build)
npm run typecheck    # tsc -b, no emit
npm run check        # check:no-random, then every mock/check_*.mjs (by glob) under one node --test
npm run check:no-random # no Math.random() anywhere in src/ (rule 5)
npm run check:phases  # every task and phase of every fixture row is worded and drawn
npm run check:figures # every fixture user's portrait figure agrees with their row
npm run check:charts # the chart tick and width rules in src/charts/scale.ts; the rate chart draws at every window
npm run check:css    # every stylesheet closes its blocks, no rule nested in a rule
npm run check:shares # a class's share is out of every class counted, whatever the chips show
npm run check:panels # a panel not reduced, waited for or building is never "none" or 0
npm run check:live   # the live socket is retried for good, polled meanwhile, replaced when quiet
npm run check:lookup # no outside string reads a table's prototype; a part that throws says so
npm run check:procexit # a run's box says how it ended as the process table does
npm run check:security # a security check with no panel behind it says "not checked", never none; every link lands
npm run check:hpc    # abnormal Slurm activity is worded from the service's thresholds, and never none of a panel not judged
npm run check:io     # the per-tool disk chart keeps the heaviest on each counter, a 0 as 0, one scale
npm run check:tabs   # seven stops in the rail, Summary first; Sessions a sub-tab of Agents; a user's open under their portrait; every link to one lands
npm run check:summary # Summary's four answers from the fixture, in eight numbers at most; "not measured", never 0; every link lands; the bare address opens it
npm run check:internal # a per-process distribution of all 0s reads "not measured", never 0; a measured 0 stays 0
npm run check:ux     # no data says so (the setup screen); nothing unmeasured reads 0; one verdict per rule; one time zone; class words
npm run preview      # serve dist/ locally (no API)
```

On 2026-11-21 the app's chunk was 199 kB (64 kB gzipped), React's runtime a chunk of its own
(219 kB, 68 kB), the CSS 97 kB (19 kB), and each tab a chunk loaded on demand (0.5 to 70 kB), the
session inspector (`OpenSession-*`) one the Agents and Users tabs share.
Assets use a relative `base` (`./`), so `dist/` works at `/` or under a sub-path; the API is
addressed at `/api` and the socket at `/ws`, overridden at build time by `VITE_API_BASE` /
`VITE_WS_URL`. The hover of the rail's status line gives the bundle's stamp from `virtual:build-info`
(`vite.config.ts`, `src/lib/buildStamp.ts`): `commit 0d516df` (`+ edits` if anything under `web/` differed from HEAD, `—`
without git) and `built 2026-11-15 14:02`, or `built — (dev server)`.

## Dependencies

Runtime: **`react` and `react-dom`**, nothing else. The typeface, IBM Plex Sans and Mono, is
served with the page (`src/fonts/`: the files and Unicode ranges Google Fonts served, under the
SIL Open Font License, `OFL.txt`), with the browser's fonts as fallback. The page asks no other
host for anything. There is no charting,
table or icon library. Every chart is hand-rolled SVG (`src/charts/`, and `PhaseBand`,
`TrajectoryNav`, `PortraitFigure` and `TreeDiagram`, which draw their own), so no
code path can add a second value axis and no library theme can substitute a hue for the CSS
custom properties. `src/components/Icon.tsx` draws the icons as paths in the text colour, so no
icon is ever a series colour.

## The rules this code enforces

Research-integrity constraints: breaking one is a reporting error, not a styling error.

1. **Three classes, never two**: `agent` / `human` / `ide` (+ `unlabeled`), a closed list in
   `src/lib/classes.ts`, labelled `agent` / `human` / `IDE` in full. `<Plate>` prints its
   per-class row from `by`; only a plate about agents alone (on Users) leaves it out.
2. **Count and cost side by side**: a categorical panel shows how many (processes) and what they
   cost (CPU) as two readings, each on its own single axis. `GroupBars` and `Lines`
   take one `ylabel`, so a dual axis is not expressible.
3. **Colour**: Harvard palette, light only. Crimson `#A51C30` (`--s1`) is `agent`'s among the
   classes; outside them the `wait` phase wears it too (`src/lib/phases.ts`). Eight series slots
   and a grey tail (`--sg`): `src/lib/purposes.ts` draws the service's purposes as three kinds
   (slurm, build & compute, files & data) and folds the rest, a new purpose included, into the
   grey `other` rather than invent a hue. A series that is not a class (a tool, sent and received) takes its hue
   from `TOOL_COL` (`src/charts/TimeChart.tsx`). **A class is never told by colour alone**: to a
   red-green colour-blind reader crimson and green are one brown, so each class swatch on a plate
   carries its word (`components/Plate.tsx`), a class line carries its word at its end
   (`LineSeries.tag`), and a class's colour as text uses `CLS_INK`, whose human green is darkened
   to WCAG AA (`--s3-ink`).
4. **Zero is not missing**, and each kind of nothing looks like itself. `_present === false`
   renders `<EmptyState>`: the panel's frame, title and feed, and each `_paths` entry verbatim
   (`no data from /var/log/ebpfm`). A present feed with no rows renders `<NoRows>`, the word
   `none`. A panel the backend did not return (no key, or a failed request) renders
   `<NotReturned>`: the reason and the feed, with no path, since no feed reported anything. A
   reducer that raised sends its envelope with `_error` alone (`windows.materialize`), which
   renders `<NotReduced>`. A window still being reduced renders `<Building>`, a frame and a
   percentage. `pick` hands a missing or errored panel on as not present, so a plate reads a
   dash, and a tab says `none` only of a body the service reduced (`hasBody`), and a plate's
   note says *not checked*, with why, of a panel not present, never `0`. When **no feed holds a
   record at all**, the tabs give way to one setup screen (`components/SetupScreen.tsx`): where the
   service looked, folder by folder (`/api/feeds` `candidates`), and the three steps that fix it.
   Feeds stays, and so does Trend: its roll-up table holds days whose files are gone (expired, or
   not yet copied back after a restore), and it says so itself when it holds none;
   the head's pill reads `no data` and the rail's live light goes out. Needs attention is green
   only for a full check: with a source not checked it reads *nothing flagged in what was checked*.
5. **Nothing fabricated**: no `Math.random()` in `src/` (`npm run check:no-random`), and nothing
   interpolates between frames. A `null` rate bin stays `null`, and `Lines` draws a separate
   subpath around it: a hole, never a 0 or a line across. The fixture lives in `mock/`, outside
   the bundle. While any host's records carry `"synthetic": true` (the demo's:
   `synthetic_hosts`), every tab opens with a *Fake data* note and the header shows a dashed
   **fake data** pill.
6. **Coverage is part of the number**: `<Coverage>`, `<AccumCell>` and `<QuantCells>` print
   `coverage_pct` beside every value that has one, and an accumulator or quantile with `n: 0`
   reads **`not measured`**, never `0`, as does a quantile the service flags `all_zero` (every
   value exactly 0: a counter the collector could not fill), in Internal's distributions too.
7. **Data only**: a panel holds its title, its feed and the chart or table, and no method notes:
   what a reader needs to read a number goes in its `data-tip`. The service does not send the page
   its `notes`, `basis` and `*_basis` texts ([API](../README.md#api)); Ask LLM reads them. Every
   panel's ⓘ opens **How to read** (`components/About.tsx`), a drawer of the explanations its
   labels hold -- its tips, gathered from the panel as drawn, not the per-row and per-point ones --
   so they can be read, scrolled and reached by keyboard and screen reader; nothing is written
   twice. A panel that draws a table also offers **copy CSV** (`components/CopyCsv.tsx`): the
   tables as drawn, from the page's own DOM, at no cost to the service.
8. **Down to a phone**: at 900px and under (`src/lib/useNarrow.ts`) the rail is a drawer and
   every grid one column. The page never scrolls sideways, down to 320px: wide tables scroll in
   their own `.scroller`, fading at an edge with more (`src/lib/scrollEdges.ts`). Charts are
   drawn at their box's width (`<Fluid>`, `useFit`, `src/charts/primitives.tsx`), so a 10px label
   stays 10px. On touch, a tap shows a `data-tip`, and form fields are 16px so iOS does not zoom.
9. **No outside string reads a prototype**: a key from the URL, the service or the collector
   indexes only a table made by `table()` (`src/lib/lookup.ts`), which has no prototype, or is
   read with `own()` (`?atype=__proto__` once blanked the page).
10. **A part that throws says so**: a boundary around the page (`src/main.tsx`), each tab
    (`TabBoundary`, `src/lib/lazyTab.tsx`) and each panel's body (`<Panel>`), all from
    `src/components/Boundary.tsx`, says what failed in that part's frame (a tab whose code a
    deploy replaced under an open page among them), and the rest of the page stays; a panel
    handed new data tries again.

Two more that the payloads force:

- **Both grains, where both exist.** Security's sandbox panels show per-process *and*
  per-session (judged by its tool calls); *Who uses what* shows eight units on the login nodes
  and three asked of Slurm, because the class ranking is not stable across them.
- **`denied` is not `unsandboxed`.** `denied`, labelled "exited before it was checked", has its
  own hue and a hatch (`src/components/Defs.tsx`), and every share is over the known states with
  coverage beside it (`src/lib/sandbox.ts`), so neither `denied` nor `unknown` sits silently in a
  denominator. `sandbox` and `approval` stay independent axes, never crossed into a score.

## Words

Each thing has one word, on every tab: labels use the word in the first column, and never a word
in the last column for that thing. `src/lib/glossary.ts` holds the same table, and the line each
word shows on hover (the word is underlined, dotted): what is measured, and what the measurement cannot see.

| say | for | not |
|---|---|---|
| class: **agent**, **human**, **IDE** | who ran it | "who ran it" as a heading, people, editor |
| **agent**, **agent type** | an AI coding agent (one running copy of one, as the Agents tab counts them), and which it is: Claude Code, Codex, … | a run of one: that is a session |
| **session** | one run of an agent or an IDE and everything it started, or one person's terminal login | trajectory, tree, run |
| **process tree** | a session's processes, each under the one that started it | trajectory |
| **process** | one program that ran; the collector records it when it exits | exits (as a count) |
| **event** | the record of a process's exit, as the collector writes it; a count of events counts processes that exited | exits |
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

When a word changes, the tab's URL key, the API field and the filter key keep the old name
(`risk` is Site rules, `slurm` HPC, `resources` Login nodes), so links and citations keep
working; a panel whose title changed keeps its old anchor (`id=`), which `src/lib/cites.ts`
points at. Sessions is a sub-tab of the Agents entry (`sessions`): a link to the old Sessions tab
(`?tab=trajectories`, with its `traj`, `tuser`, `rank` and `torder`), or to the Agents tab with a
session open or one user's list (`?tab=agents&traj=…`, `&tuser=…`), opens it there. Trend (`src/tabs/TrendTab.tsx`, `/api/trend`) is the one tab that
reads neither the window nor the filter: each class's use over up to a year, from the roll-up
table the service writes as it ingests (`rc_dashboard/rollup.py`), so the window picker is not
shown on it.

## The window control

`src/components/WindowPicker.tsx`, in one control bar with the class filter and the user and
agent-type dropdowns: everything that says what is on screen. The bar sticks to the top as a tab
scrolls (`src/lib/sticky.ts`; on a phone, 640px and below, it is one row that scrolls sideways,
short enough to stick there too) and publishes its height as `--controls-h`, the page's
`scroll-padding-top`, so nothing the browser scrolls to lands under it.

The window is `?win=<minutes>`, and this is its only control: Live's plates count it too. It
offers the service's presets, filtered against retention, after the service's floor
(`min_minutes`, 5 minutes, which only `/api/windows` carries) when the presets start above it;
"go to" lists the same spans. It also takes a typed span (`90m`, `4h`, `2d`, `1w`, bare minutes;
cleared, the service's default). Beside it, the window's state: `filled 5h of 7d`, `reading
history · N%`, `rebuilding` or `catching up` with a percentage, `… failed to build — pick it
again to retry`, `+Nm past the window`. A clamp is shown, never silent: 30 days asked against 7
days of retention reads `asked 30d · showing 7d`, with the reason on hover. A plate names its
window from its panel's own envelope (`winLabel`), not from the picker: during a rebuild the two
differ.

## The rail and "go to"

The rail holds seven stops, each answering one question, in an operator's reading order:
**Summary** (the four answers a reader opens the page for, [below](#summary-the-first-screen)),
**Live** (the window now; Overview's table is its section, *Who uses what*), **Users**, **Agents** (›
Agents, Sessions), **Risk** (› Security, Site rules), **Usage** (› Login nodes, HPC, Trend), and,
after a rule, **System** (› Feeds, and Internal only where `ui.show_internal` lists it). An
entry's sub-tabs run along the foot of the head; a sub-tab keeps its own key (`?tab=resources`,
`risk`, `slurm`, …), so links and citations land where they did, and the entry reopens the
sub-tab last used. An entry left with one sub-tab takes that tab's name (System is *Feeds* where
Internal is not listed), and HPC goes where `slurm.enabled` is false. Login nodes is named in the
site's own word for its hosts (`ui.node_noun`): its figures are login-node use, not the
cluster's. A tab's sections are not listed under it: go-to reaches each one, read off the page
itself (`src/components/SectionNav.tsx`). The rail folds to the icons alone (`[`, or the button
at its foot), which this browser remembers (`localStorage`, a per-viewer convenience: storage
that is absent or refuses just leaves it unfolded). Its foot says how the page gets its data in
one line, the transport as a light and the last update; the window and how much of it the
service holds, when the panels were built (and whether they were restored from a saved build),
and which code the page is are that line's hover.

**Ask LLM** is not a stop: it asks about the window on screen, so the head's **Ask** button
opens it in a drawer beside whatever tab is open (`components/AskDrawer.tsx`), and only where a
site turned it on (`ask.enabled`); `?tab=chat` opens Live with it open.

### Summary: the first screen

The bare address opens **Summary** (`?tab=summary`, `src/tabs/SummaryTab.tsx`): four cards, each
a question a reader opens the dashboard to ask, answered in at most two numbers, each number with
its words beside it and a link to the tab or section that breaks it down. Eight numbers in all,
never more (`mock/check_summary.mjs` counts them). Nothing on it is reduced: each number is one
the service already sends, read as the panel it comes from reads it, so the two cannot disagree;
its one request of its own is `/api/status`, asked when it opens and once a minute while it is on
screen. The rest it takes from what the shell already holds: the panels, the sessions.

| card | its numbers | from | links to |
|---|---|---|---|
| **Is collection healthy?** | login nodes whose collector is OK, of every one `/api/status` lists; the level (`working`, `something to look at`, `data not arriving`) in its pill; the others named, with their verdicts | `/api/status`: the verdicts the Feeds tab's *Collectors* draws (`rc_dashboard/health.py`), rolled up by `status.py` | System › Feeds, *Collectors* |
| **Who runs agents, and how much?** | users with an agent session in the window; agent sessions; each class's sessions side by side, agent, human and IDE each with its word, the counts on hover | `/api/trajectories`' `summary`, as Live's sessions plate reads it | Users; Agents › Sessions |
| **Is anything risky?** | agents' commands this site's rules say should not run here; credentials on agents' command lines (counted, never shown); `flagged` or `nothing flagged` | `panels.risk`, summed as Live's risky-actions plate sums it | Risk › Site rules, on the first section with an agent's hit |
| **Is a login node struggling?** | the busiest login node's CPU busy share at its newest snapshot, named; the problems the service's own lines flag (a full local filesystem, an account held back at its CPU limit, an NFS mount retransmitting), as *Needs attention* counts them | `panels.node` | Usage › Login nodes, *Login node health* |

A number nobody measured reads **not measured**, never 0, with why beside it (`waiting for
/api/status`, `not checked: no risk panel was sent`); with the agent class filtered out, its
numbers read **not shown**. A card's verdict is a word in its pill, beside the colour. With no
data at all, the setup screen takes Summary over as it takes every tab. Live is one click away,
in the rail and at the screen's foot.

A pane beside a list never stands empty: with nothing picked, Agents shows its list's first
agent and Sessions its first session, held while it stays listed (the list is re-read every few
seconds, and a new row at its top must not swap the tree under the reader); a pick is the
reader's, and only it goes in the URL.

The head above the panels is one line: the tab's name, with what the tab answers on its hover,
and one pill saying how current the data is (`live`, or what is wrong: `data delayed`,
`no data yet`, `no data`, …; `frozen capture · as of …` on a pinned clock). What the pill
stands on is its hover: the newest record's age, each feed past its own limit and by how much,
and the login nodes that reported. `anonymized` and `fake data` sit beside it when they apply,
then Ask (where it is on), the **time zone** every time on the page is in, and the refresh.
Below 900px the top bar names the tab, so the head shows the pills alone.

**One time zone.** Every time on the page is in the dashboard service's zone, which
`/api/config` names (`_meta.tz`); the head says which (`EST`, or `UTC−05:00`). The service
formats some times itself, in its local zone, and the page formats the rest from epochs through
`src/lib/zone.ts`, so the two agree on one screen; drawn in the browser's zone they disagreed as
soon as reader and cluster were in different zones. A service that does not say leaves the
browser's zone, and the pill says that instead. A time axis ticks that zone's round wall-clock
times: across daylight saving it has no tick in the hour the clock springs over, and one per time
in the hour it falls back over (`mock/check_charts.mjs`).

**Go to** (`src/components/CommandPalette.tsx`), opened with Ctrl+K or Cmd+K anywhere, or `/`
outside a field: type to reach a tab, a section of the open tab, a window preset, or an
action (refresh, show all three classes, fold the rail); and, once a word is typed, who and
what the window holds: a **user**'s portrait, a **login node**'s health, a **session**, or the
page narrowed to one user or agent type. It offers only what the page already holds, so it can
never name a tab, section, window, user or session that is not there.

## Keyboard

Every control is a tab stop with a visible focus ring (controls restyled from `all: unset`
restate it at the foot of `src/styles.css`). A sortable header (`src/components/SortTh.tsx`)
sorts with Enter or Space, and a control's `data-tip` shows under it when it is reached with
Tab (`src/components/Tooltip.tsx`), since the caveat about how a number is counted lives there.
The tip is visual only: its layer is `aria-hidden`, and no control points at it with
`aria-describedby`, so a screen reader does not announce it. Where a tip holds facts the page
shows nowhere beside it -- the head's status pill and the rail's status line -- the element is a
tab stop too, so its tip shows on focus, and carries the tip's words as text a screen reader
reads (`vh`); so does the line under each tab's name, which is its name's tip.

| key | does |
|---|---|
| Ctrl+K, Cmd+K | opens or closes **go to**, anywhere |
| `/` | opens **go to**, outside a field |
| `[` | folds or unfolds the rail, outside a field |
| ↑ ↓ (or Tab, Shift+Tab), Enter, Esc | in **go to**: move, go, close (focus returns where it was) |
| ← →, Enter or Space, + −, 0 or Esc | on the inspector's chart and its purpose ribbon: step, open what is under the cursor, zoom, reset |
| ↑ ↓ | in the Agents tab's list of agents: the previous or next agent |
| Esc | closes the rail's drawer below 900px |

## Tabs

How each figure is computed is in [`docs/figures`](../../docs/figures/README.md).

| tab | source | holds |
|---|---|---|
| **Summary** (`?tab=summary`; the bare address) | `GET /api/status`; the panels and sessions the shell holds | four questions, at most two numbers each ([above](#summary-the-first-screen)): is collection healthy, who runs agents and how much, is anything risky, is a login node struggling; each card links to where it is broken down |
| **Live** | `WS /ws`, `GET /api/live`, `/api/panels?window_min=` | *Needs attention* (`src/tabs/Attention.tsx`): what the service already flags, each linking to its panel, and every source it could not check named, never read as an all-clear; one row of plates over the window; the rate of processes exited, per minute in a window of up to an hour and per 5 minutes in a longer one (`src/lib/rate.ts`), the one time series the tab keeps; *Who uses what* (`src/tabs/OverviewTab.tsx`; `?tab=overview` lands on it); *Active users* (`src/tabs/LiveUsers.tsx`); the login nodes right now. The window's other series are on Resources, and per-day use is Trend's |
| **Usage › Trend** (`?tab=trend`) | `/api/trend?days=&by=` | each class's use over the long run, never the window and not narrowed by the filter, so the window picker is not shown: *Over the year*, from the roll-up table the service writes as it ingests (`rc_dashboard/rollup.py`) -- processes and CPU a day, users, users of each agent -- by day, week or month over 30 days to a year, a stretch with no record a hole; and *Each week* (or day, or month), the same as a table. The one place for per-day use (`src/tabs/TrendTab.tsx`) |
| **Users** | `/api/users`, `/api/user?name=` | every user with an agent session in the window (`src/tabs/UsersTab.tsx`), one row each, opening the user's **portrait** (`?portrait=`): their figure, drawn from their numbers (`src/components/PortraitFigure.tsx`), how they leverage agents, the numbers and rules behind it, and every session, each opening its process tree under the portrait (`?traj=`), whose *all their sessions →* opens the Sessions tab's list narrowed to them (`?tuser=`). The service holds every threshold (`rc_dashboard/portraits.py`); the page words what it counted (`src/lib/portraits.ts`) |
| **Agents › Agents** (`?tab=agents`; `?tab=cascades` opens it too) | `cascades`, `tool_mix` | every agent tree, by user or as one list (`src/tabs/AgentsTab.tsx`), agents with **subagents** kept apart from **hosted** sessions; per agent, **Workspace & blast radius**, alone or with its subagents: where its processes ran and the bytes they moved (measured), the paths its command lines named (inferred, drawn dashed), writes and deletes outside its start folder; depth and fan-out; **Tool mix** and **Top tools** (`src/tabs/ToolPanels.tsx`); *Commands that failed* (`src/tabs/ExitsPanel.tsx`) |
| **Agents › Sessions** (`?tab=sessions`; `?tab=trajectories` opens it too) | `/api/trajectories?rank=&window_min=` | the Sessions sub-tab (`src/tabs/TrajectoriesTab.tsx`), re-read every 5 s while it is open, its first session open beside the list until one is picked: sessions ranked by a switcher that only reorders, each row a band of its phases and a line naming its tasks ([below](#sessions-tasks-phases-and-processes)); a row opens the **inspector** (`/api/trajectory`, `?traj=`; `src/tabs/OpenSession.tsx`, which the Users tab opens too): who ran the session and what it came to, **What it did**, then **Processes**, read oldest or newest first, with every bound of the read stated |
| **Risk › Security** (`?tab=security`; `?tab=sandbox` opens it too) | `sandbox`, `risk`, `cascades` | *Security checks* (`src/tabs/SecurityTab.tsx`) on launch, confinement, exposure and reach, each with its share of what its axis could judge and a link to its panel, and no threshold of its own: a check that found none is a ring, one not made (its panel not in, or the agent class filtered out) a dashed ring and the reason. Then the **sandbox** panels (`src/tabs/SandboxTab.tsx`), and *Writes outside the start folder*, by kind of place, the most alarming first (`src/lib/places.ts`), then by user; those paths are read from command text, not measured |
| **Usage › Login nodes** (`?tab=resources`; `?tab=io` opens it too) | `node`, `resources`, `io_process` | *Login node health* (`src/tabs/NodeHealth.tsx`), the node tier's newest snapshot of each host; then the window over time, on one zoom (`src/tabs/LiveCharts.tsx`): *Over the window* (CPU and memory), *Login node response* (`node.series`, `src/tabs/NodeResponse.tsx`) and *More over time* (runqueue wait, D-state, major faults, disk, network, Slurm checks and jobs, one at a time); then the worst individual processes; disk read and written by tool, on one scale; where the network bytes went (*Network bytes*, its endpoints); I/O per user. The per-class tables behind them are on **Internal** |
| **Risk › Site rules** (`?tab=risk`) | `risk` | the sections the site's [risk profile](../risk-profiles/README.md) lays out, by default build and interpreter CPU on the login node, commands that should not run there, and credentials on command lines (counted, never shown) |
| **Usage › HPC** (`?tab=slurm`; `?tab=hpc` opens it too) | `slurm`, `submits` (`/api/live`) | *Abnormal Slurm activity* (`src/tabs/SlurmTab.tsx`), each rule worded from the threshold the service sends with it, each class's count of the sessions that broke it, then every such session with its own numbers as evidence; then sessions at the scheduler, how each class uses it, refused submissions, the latest submissions; its two series, queue checks and jobs submitted, are in Resources' *More over time*. Gone where `slurm.enabled` is false |
| **Ask LLM** (a drawer: the head's Ask button; `?tab=chat` opens it) | `/api/ask`, `/api/ask/{job}`, `/api/suggestions/done` | a question about the window, its answer's citations linking to the panels it read (`src/lib/cites.ts`); *Suggestions for the operator*, one marked done kept for every reader; off unless the site turned `ask` on (`src/tabs/ChatTab.tsx`) |
| **System › Internal** (`?tab=internal`; in the rail where `ui.show_internal`) | `resources`, `io_process` | panels not settled yet, and the detail behind the operator tabs: per-process distributions, faults, delays and D-state (`not measured` where unread), requested against disk bytes, I/O distributions |
| **System › Feeds** (`?tab=feeds`) | `/api/feeds`, `/api/collectors` (every 20 s), `/api/config` | *Collectors*, each login node's collector and how its runs ended; *Feeds*, each one's configured **vs** resolved path, status, newest record against its own `max_age_s`, files and notice; *Startup read*; *Configuration*, each resolved setting and where it came from |

### Sessions: tasks, phases, and processes

The page draws every session at three levels, **Tasks**, **Phases** and **Processes** (`LEVELS`,
`src/lib/phases.ts`): an agent's from its tool calls, and a session no agent ran (a person's
login, an editor's terminal) from its terminals' commands (`driver`: `agent` or `terminal`).
The service builds them and holds every threshold
([`dashboard/README.md`](../README.md#sessions-tasks-phases-and-processes)); the words and
geometry are in `src/lib/phases.ts` and `src/lib/tasks.ts`, so the row, the chart and the list
say one thing, in the driver's words (`tool calls` or `commands`: `callWord`, `driverOf`).

- **The row** (`src/tabs/TrajectoriesTab.tsx`, `src/components/PhaseBand.tsx`) draws the phases
  as a thin band, ticked where each task or lone stretch after the first begins, and under it a
  line of the tasks, two lines at most (`“Fix the loader” → Run train.py · config/base.yaml → 3
  more`), or with no tasks the phases in order (`explore → edit → test`). A chip
  (`src/components/CategoryChip.tsx`, words in `CATEGORIES`) says what kind of session it was, as
  the service decided; its dot is the colour of the phases its use counts.
- **The inspector** (`src/tabs/TrajectoryInspector.tsx`) reads top down: who ran the session and
  what it came to, its headline, chip and summary; **What it did**, the task list
  (`src/tabs/TaskList.tsx`; with no tasks, `src/tabs/PhaseList.tsx`), drawn from the row's block;
  then **Processes**, what the read covers, its chart and the tree. The read stops at its budget,
  so the chart can hold the first hour of a ten-hour session while the list holds all of it. A
  click on a task or a phase, in the chart or the list, zooms the chart to it and opens the tree
  at its first call.
- **A label says where it came from.** The agent's own words are a quote, in quotation marks,
  with their source (`commit`, `PR #235` or `log`); a label built from the names in the task's
  commands has neither, so it is never taken for something the agent wrote.
- **A lone stretch**, a few calls hours from other work, is no task: no number, a dashed block in
  the lane, a line of its own in the list, not counted in the row's line. A cutting gap is a line
  of its own too, naming the first call the agent moved past that was still running.
- **A session running now with no call ended** has no phase yet (`runningUnread`): its band and
  phase list are its live edge alone, its line and summary say what the census shows running, and it has
  no chip, since `no tool calls` would be wrong.
- **A session across midnight** is a row a day, each serving the one block. A task or phase the
  other day's row holds opens that row (`jumpTo`) when the window holds it; otherwise the pick is
  made on the open row.

### Live transport

`src/api/useLive.ts` prefers the websocket and never gives up on it: it tries again after a wait
that doubles from 1 s to 30 s, spread by a hash of when the page opened (`src/lib/backoff.ts`; no
random number), and at once when the browser comes back online or the tab is shown again. After
five failed attempts in a row it also polls every 15 s for what the socket would have sent
(`/api/live`, `/api/windows`, `/api/feeds`), so a finished build still reaches the panels. Only a
socket up for 75 s resets the wait and ends the polling: a proxy that accepts the upgrade and
drops it soon after would otherwise be knocked on every second. A socket not open within 15 s, or
silent for 75 s once open (the service sends `feeds` every 30 s), is closed and replaced. The
transport is always named, at the rail's foot (`streaming` / `every 15 s` / `connecting…` /
`disconnected`, then how long ago the last update came) and in the header's pill (`live` /
`refreshing every 15 s` / `connecting…` / `disconnected · retrying`): a frozen chart that looks
live is worse than one labelled as polled. A `rebuilt` frame re-reads `/api/panels`, and a `feeds` frame supersedes the
REST feeds report. The header's refresh asks for everything again: the live frame, the windows,
the feeds, the panels and the sessions.

### Filters and URL state

Every filter and view lives in the query string, so a reload or a pasted link restores the view:
the tab, the window (`win`), the session rank, the inspected session (`traj`, on the Agents tab or
under a portrait, closed by going to another tab; `tfrom` once a read was continued past its budget,
`torder=newest`), the user the Agents tab's list of sessions is narrowed to (`tuser`), and the open
portrait (`portrait`). The page-wide filters are `class`, `user` and `atype`, each repeatable
(`agent_type` is still read). `src/lib/filters.ts` owns the one serialisation (`urlQuery`), which
always writes the tab. The bare address, with no key at all, opens Summary, the first tab. A link
with no `tab` but the page's other keys (`class`, which every link the page writes carries, `win`,
`user`, …) was written while Live was the first tab, and opens Live, as it always did (`parseUrl`).
A tab that is not there, such as the Search & export tab, since removed (`?tab=events`), opens
Summary.

## Known gaps

- `/api/panels` payloads this page does not render (any beyond `tool_mix`, `io_process`,
  `resources`, `sandbox`, `cascades`, `node`, `trend`, `risk` and `slurm`) are ignored, not shown
  generically; sessions are read from `/api/trajectories`, not the panels' `trajectories`.
