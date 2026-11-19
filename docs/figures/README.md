# What each figure computes

Every chart, table, plate, pill, ranking and list the dashboard page draws, and the operation on
the data that produces it: which records it reads, by which read path, which reducer folds them
and which method renders its payload, what that costs, and where the code is. It is written for
the dashboard's maintainers and describes `develop` at `0a9eb37` (2026-11-17).

The machinery this page refers to -- the live tier, window builds and their saved copies, the
hourly merge-reduce, the history store of Parquet replicas -- is explained in
[`dashboard/README.md`](../../dashboard/README.md), and linked here rather than repeated. What
each tab is for is in [`dashboard/web/README.md`](../../dashboard/web/README.md#tabs).

How to read it:

- **Code pointers** are `file:function`. Service code is under `dashboard/rc_dashboard/` and page
  code under `dashboard/web/src/`; the prefixes are left off.
- **Figures** are numbered by tab, in page order (`LV-7` is the Live tab's seventh). Each tab has
  a table -- the figure, its read path, the payload it draws, its code -- and then a note per
  figure: what it shows, the records and fields it reads, the computation, and the cost.
- **Cost** is said per record (what one record costs a reducer as it is fed) and per read (what
  making the figure's payload costs), with the bounds that apply. A bound is the default of
  `config.py:defaults` or of the module constant named. A number is one the repo's docs or code
  comments measured, and says where; "not measured" means there is none.
- **Records** are the eBPF tier's unless a note says otherwise. On every path a record is parsed
  (`fastjson.loads`) and normalized (`normalize.py:Normalizer.normalize`) before a reducer sees
  it. The normalizer derives the keys the reducers read: `_a3` (the class), `_command`, `_eff`
  (the effective command), `_purpose`, `_sid`, `_kt` and `_kv` (the session key), `_role`,
  `_sandbox`, `_approval`, `_surface`, `_ts_epoch`. Parsing and normalizing costs about 25 µs a
  record (`proctree.py`, `DEFAULTS`).

## Where the records come from

- **The eBPF tier** (`feeds.ebpf`): `exit`, `truncated`, `residency`, `residency_totals`, `conn`,
  `netio`, `submit`, `meta`, `stop`. Every figure but the Feeds tab's reads only this tier.
- **The node tier** (`feeds.ebpf_node`): one snapshot of a host a line. It is read and reduced
  (path NODE) and served as `panels.node`, and no figure draws it
  ([Served, not drawn](#served-not-drawn)).
- **Slurm**: no figure reads the scheduler's accounting feeds (`feeds.sacct`, `feeds.sacctmgr`),
  and `sacct.py` is imported by nothing the service runs. The Slurm figures read the eBPF tier: its
  `submit` records (one per `sbatch`, `srun` or `salloc`) and the `exit` records of the scheduler
  commands run on the login nodes (`squeue`, `sacct`, `scontrol`, `scancel`, `srun`, ...).
- **The domains and user CSVs** (`feeds.domains`, `feeds.user_actor`): resolved and listed on the
  Feeds tab (FD-2); no figure reads them.

On FAS RC's eight login nodes on 2026-11-15 the eBPF tier wrote 21.6 M records: 17.1 M `exit`,
2.1 M `conn`, 1.7 M `netio`, 0.66 M `residency`, 18 k `submit` and 11.5 k `residency_totals`
([docs/feed-parquet](../feed-parquet/README.md#what-the-fas-feed-looks-like)). That is the scale
"a big feed" means below.

## Read paths

| path | what reads the records | when | sealed days from the Parquet replicas |
|---|---|---|---|
| LIVE | the live tier, `reducers/live.py:LiveReducer`, and the collectors' health, `health.py:CollectorHealth`, fed by the tail and the startup backfill | the tail every `ingest.poll_ms` (1 s); the backfill once a start | no |
| BUILD | a window view's reducers, `windows.py:PanelView`: built once per window and filter, then fed live | a build on first request, at start for the default and prewarm windows, on drift; its payload made again every rebuild tick | merge: no. scan: only the time search that places the window's start. filtered: yes |
| INSPECT | the session inspector's re-read of one session, `proctree.py:inspect` | on opening a session | yes for a session keyed on pids; no for a terminal's or a login scope's |
| EVENTS | the events scan, `export.py:scan`, behind `/api/events` and `/api/export` | per request | yes when the filter names `user`, `host`, `agent_type`, `exit_code`, `signal` or `session_key` |
| IOCMDS | the lookup of the commands behind a clicked I/O number, `iocmds.py:IoCommands` | per click, as a polled job | yes when the click names a user, a host, a directory or agent pids |
| NODE | the node tier's newest snapshots, `aggregator.py:Aggregator.read_node_tier` | every rebuild tick, and at start | no: the node tier is never sealed |
| FEEDS | the feed reports, `feeds.py:resolve` | per `/api/feeds` request, and every 30 s for the `feeds` frame | no |
| CONFIG | the resolved configuration, `/api/config` | once a page load | no records read |
| ASK | an agent CLI run over a snapshot of BUILD and LIVE payloads, `ask.py` | per question | through BUILD |

### LIVE: the live tier

See [The time window](../../dashboard/README.md#the-time-window) and
[Ingestion, precisely](../../dashboard/README.md#ingestion-precisely). One `LiveReducer`, sized at
`live.retention_min` (7 d), holds:

- per-minute bins by class (`buckets.py:RollingBuckets`), of which any window is an exact slice:
  10,080 bins at 7 d;
- a per-account roster (`roster.py:UserRoster`): 10-minute bins for the last day and hourly ones
  beyond, each a flat list per account of (class and agent type, processes, CPU, bytes);
- the newest `residency_totals` of each host, the newest `residency` tick of each agent or editor
  tree, and the agent session roots;
- bounded tails: 500 exits (`live.event_tail`), 50 submissions (`live.submit_tail`), 100 exits per
  account (`live.user_tail`); and every submission in retention (`submits_in_window`).

The tail reads what the collectors appended, every second (`Aggregator.poll_once`); on every start
the backfill reads `min(live.backfill_hours, retention)` of history newest first
(`Aggregator.backfill_now`). The tail's records go to each held view too (BUILD); the
backfill's do not, since a view reads its own window from the files. `CollectorHealth` is fed in
the same dispatch as the live tier, from both. Served by `/api/live` and the websocket's `live`
frame -- one frame per distinct window in use, made after each poll that brought records and
reused for a second (`Aggregator.live_frame`) -- and by `/api/live/users` and `/api/live/user`.
Without a socket the page polls `/api/live` every 15 s
([Live transport](../../dashboard/web/README.md#live-transport)). Nothing here is narrowed by the
page's user or agent-type filter (`windows.py:ViewFilter.NOT_NARROWED`).

Replicas: no. The backfill places its start in each file by a time search of the `.gz` and reads
the JSONL; the tail reads today's plain files.

Measured: a week of one login node (4 GB, 2.4 M records) backfills in 35-45 s (README);
positioning a week of 8 login nodes, which inflates each sealed day's `.gz` for its seek index,
took 4 minutes on rtx6000b (`aggregator.py:Aggregator.backfill_now`); with the backfill holding
the lock 256 records at a time, `/api/live` stayed at p90 ~10 ms beside a build
(`aggregator.py`, `BACKFILL_BATCH`).

### BUILD: a window view

See [The time window](../../dashboard/README.md#the-time-window) and
[Hourly merge-reduce](../../dashboard/README.md#hourly-merge-reduce). A view is one set of the
historical reducers for one window and one page filter (`windows.py:PanelView.fresh_reducers`), in
an LRU of `panels.max_windows` (5) views, built one at a time on one worker thread
(`WindowRegistry._build`). The default window (24 h) and the prewarm windows (1 h, 6 h, 12 h;
`panels.prewarm_min`) build at start, once the backfill has placed itself in every file. After its
build a view is fed every live record, so a live record is reduced once per held view.

| payload key | reducer | subscribes to |
|---|---|---|
| `tool_mix` | `reducers/tools.py:ToolMixReducer` | `exit` |
| `io_process` | `reducers/io.py:IoReducer` | `exit` |
| `resources` | `reducers/resources.py:ResourceReducer` | `exit`, `residency_totals` |
| `risk` | `reducers/risk.py:RiskReducer` | `exit` |
| `sandbox` | `reducers/sandbox.py:SandboxReducer` | `exit`; `truncated`, `residency`, `conn`, `netio` for the roots' launch |
| `slurm` | `reducers/slurm.py:SlurmReducer`; absent with `slurm.enabled` off | `exit`, `submit` |
| `trajectories` | `reducers/trajectories.py:TrajectoryReducer` | `exit`; `residency`, `conn`, `netio` for the roots' launch (`truncated` is subscribed to and not counted) |
| `workspace` | `reducers/workspace.py:WorkspaceReducer` | `exit` |
| `cascades` | `reducers/cascades.py:CascadeReducer` | `exit` (the role census); `exit`, `truncated`, `residency`, `conn`, `netio`, `submit` for its nodes |
| `facets` | `reducers/facets.py:FacetReducer` | `exit` |
| `_procs` | `procindex.py:ProcIndex`, the process index the session reducers fold keys through | `exit`, `truncated`, `residency`, `conn`, `netio`, `submit` |

Their union (`windows.EVENTS`) is every record kind but `meta` and `stop`, so a build parses nearly
every line it reads; the byte check on the event kind (`export.py:event_filter`) skips little.

Two rules hold for every sum below. A process is counted once, at its `exit`: a `truncated` record
is a process still running at a collector stop, with its whole life's counters so far, and its exit
after the restart carries them all again, so it may name a running root, never add to a sum. And
disk and syscall bytes are each process's own: the collector's `io` is `task->signal->ioac`, which
the kernel also credits with every child the process reaped, so each reducer that sums bytes holds
a join (`reducers/own_io.py:OwnIo`) that takes the children's figures back off at the parent's
exit, carried across hours as the sessions' always was, and held to `reducers.own_io.max_parents`
parents (a parent dropped is counted, `own_io.evicted`, and said on stderr; what is held for an
agent runtime, or for what started one, goes last, unless such parents hold over half the cap).

A view is built one of three ways:

- **merge** -- an unfiltered view with a closed hour inside it (`hours.py:build`). The records from
  the window's start to its first whole hour are read; every closed hour is merged from its saved
  state, a whole epoch day from one saved state (`HourStore.load_part`, each reducer's `merge`);
  the join state the last hour ended with is installed; the open hour is read. An hour not saved
  yet is reduced first, in order (`HourStore.ensure`), once ever. Replicas: no -- the edge, the
  hours and the tail read the JSONL and search the `.gz` by time. In a run of hours, a reader of a
  sealed file is handed the span another reader of it inflated last, for the 16 files read last
  (`gz.shared_spans`), so an hour's search, its read and the next hour's search do not each
  inflate it again. With a worker for every unit of the node split, each unit's hours run in one
  worker (`hours.UnitPool`), which keeps at most 4 such spans for one hour and indexes only its
  own node's files.
- **scan** -- an unfiltered view with no closed hour inside it, or any view with
  `panels.merge_reduce` off (`WindowRegistry._build`, `export.py:window_plan`, `window_scan`): each
  day-file's in-window byte range, k-way merged oldest first. The 1 h view is always scanned: an
  hour closes `panels.hour_grace_s` (300 s) after it ends, so 60 minutes never hold a closed one. A
  scan resumes from a saved build when one fits (`snapshots.py`, `WindowRegistry._resume`), reading
  only what was written since. Replicas: only for the time search that places the window's start
  in a sealed file (`export.py:_place`); the read is JSONL.
- **filtered** -- a view narrowed by the page's user or agent-type filter: always scanned, never
  merged, saved and resumed like a scan. Replicas: yes. The filter is a `where`
  (`ViewFilter.where`, `where.py`) that a sealed file's replica tests on its columns before it
  builds a record; in a JSONL file a byte check (`ViewFilter.line_match`) skips the lines it
  rejects before they are parsed.

A view's payload (each reducer's `result()`, credentials masked) is made after its build and again
on every rebuild tick (`rebuild.interval_s`, 900 s; `WindowRegistry.refresh`), which instead builds
again a view whose drift passed `panels.max_drift_pct` (25%) of its window: by a merge where the
view can be merged, else by a scan, and never by resuming a saved build. So most BUILD
figures move every 15 minutes, not with each record; the page re-reads `/api/panels` on the
`rebuilt` frame and when a build lands. Two are composed on read instead: `trajectories` is made
again at most every `reducers.trajectories.live_s` (5 s) when the view has ingested since
(`Aggregator.trajectories`, `PanelView.materialize_key`), and the Users tab is built from the
view's reducers (`portraits.py`) and held up to 60 s (`Aggregator._held`). Both run under the
aggregator's lock, which the tail and every reader share. While a view builds it shows the panels
the last view of its window saved (`payloads.py`).

Measured on one login node's feed (README): merged, a 24 h window builds in ~2-3 s and a 7 d one
in ~8 s, against ~15 s and ~160 s scanned, and filling the hours the first time costs about what
one scan does; resumed from a saved build, a 24 h window takes ~0.2 s against ~7 s and a 7 d one
~1.4 s against ~42 s. A held 7 d window of a busy node is up to ~1 GB, most of it the trajectories'
session keys (~6 KB each, ~950 MB at the 150,000-key cap). A week of saved hours is ~38 MB on
disk, a saved build ~12 MB per day of window.

### INSPECT: the session inspector's re-read

See [Sessions: tasks, phases, and processes](../../dashboard/README.md#sessions-tasks-phases-and-processes).
`/api/trajectory` (`proctree.py:serve`, `inspect`, `_inspect`) re-reads one row's records from the
day-files of the row's host, from the session's first record, for exactly the collector keys the
row folds. It uses the unfiltered view of the window, whatever the page's filter. A session keyed
on agent pids is found by searching the bytes for its keys (`key_scan`, over a memory map 32 MB at
a time) and, in a sealed file with a replica, by testing the replica's `session_key` and
`agent_pid` columns (`key_where`); a terminal's or a login scope's key is searched for in the
inflated `.gz`.

The records a search finds are built and normalized in `inspect_workers` worker processes (8, no
more than the CPUs the service may use; `keysearch.py:stream`), a part of a day-file each: a
replica's row groups, or two of a plain file's 32 MB chunks. A record comes back holding only the
28 fields the tree reads (`keysearch.py:INSPECT_KEYS`). The service's process replays the parts in
file order with the one-process read's counters, so every bound below stops at the same record,
and feeds the tree; a read that stops early cancels the parts still out. A sealed `.gz` with no
usable replica -- every one, for a terminal's or a login scope's key -- is searched in the
service's process, as before. The row shown beside the tree (SE-4) takes the agent's other days
from the window's last fold, so opening a session folds only the keys sharing its row's prefix.
See the inspector under [API](../../dashboard/README.md#api), "How a read is made".

Bounds, each stated in the response: `inspect_max_records` 150,000 (600,000 newest first),
`inspect_max_bytes` 6 GiB, `inspect_hold` 20,000 processes itemised, `inspect_per_parent` 2,000
childless processes of one command per parent, `inspect_nodes` 6,000 entries served. One read runs
at a time; a built tree is served again for 600 s (16 trees, 96 MB in all), keyed on the view's
install time. A background thread keeps the top 3 sessions of the `recent` ranking read while
someone has opened a session in the last 30 minutes and no window is building, and gives way to a
reader (`_warm_loop`).

Measured: searching for the keys read one 11,000-line Claude session of a 627 MB day-file in 0.8 s
against 2.9 s line by line (`proctree.py:key_scan`); in FAS's busiest sealed day-file (17.4 GB
inflated) a session's key search took 30-43 s from the `.gz` and 2.8 s from its replica
([History in Parquet](../../dashboard/README.md#history-in-parquet)). With the records built in 8
processes, a 150,000-record read of a FAS agent session (boslogin06, 2026-11-16, its fold stale as
on a live window) went from 9.9 s to 2.5-2.7 s, and a newest-first read of 149,573 records
(boslogin05) from 12.1 s to 5.3-5.5 s, most of it now the tree keeping the newest.

### EVENTS: the events scan

`export.py:scan` plans every day-file under the roots, reads each from its near time bound (newest
first: backwards from `to`), checks the event kind on the bytes, parses, normalizes and tests each
record (`make_predicate`), and k-way merges the files' streams on time. Paging is by offset, so a
deep page walks the earlier matches again. A `.gz` read with no replica is inflated whole, the
first time, to be indexed. Replicas: when the filter names one of the record's own fields
(`where_of`: `user`, `host`, `agent_type`, `exit_code`, `signal`, `session_key`). Measured: one
user's exits on one sealed day, the week's 128 day-files planned, 222-444 s from the `.gz` and
13.6-15.4 s from replicas ([History in Parquet](../../dashboard/README.md#history-in-parquet)).

### IOCMDS: the commands behind an I/O number

`iocmds.py:IoCommands`: a click on an I/O number (`components/IoCmds.tsx:IoNum`) starts a job that
reads the window's `exit` records again (`export.py:window_plan`, then
`_window_stream` per file, one file after another), takes each one's own bytes off what it
reaped (`OwnIo`, over every record the read parses), keeps those the click's filters pass
(`/api/events`' own, plus agent pids, a host and a directory), and groups them by command line. Two
jobs read at once; six finished ones are kept, and answer the same question again for 120 s on a
live feed and for good on a pinned clock. A click carries its figure's filters, and the page's
user and agent-type filter where the figure names none of its own (`components/IoCmds.tsx:params`).
Replicas: when the lookup names a user, a host, a directory or agent pids (`prefilter_where`); one
narrowed only by class, tool, kind of tool, agent type or time reads the JSONL. Measured
(`iocmds.py`): about 3 s for a day of rtx6000a and about a minute for a day of the FAS RC capture
(6.2 GB of gzip, 2.8 M exits).

### NODE, FEEDS, CONFIG and ASK

- **NODE**: `Aggregator.read_node_tier` reads the last `feeds.ebpf_node.snapshots_per_host` (2)
  lines of each node-tier file, without streaming the rest (`aggregator.py:last_lines`), into
  `reducers/resources.py:NodeReducer`, on every rebuild tick and at start.
- **FEEDS**: `feeds.py:resolve` globs each feed's paths and reads the newest file's last record
  (`newest_record_ts`); `Aggregator.refresh_feeds` adds the eBPF tier's counters, the backfill's
  state and the history store's (`history.py:HistoryStore.status`). `/api/collectors` adds the
  health the live tier folded (`CollectorHealth.report`).
- **CONFIG**: `/api/config` serves the resolved configuration with each key's origin.
- **ASK** (off unless `ask.enabled`, and refused while the service has no password; a refused
  question reads nothing): `Aggregator.ask` builds a snapshot from the window's `/api/panels` payload, the live
  payload's `now`, `users`, `hosts` and `window`, and the ranked sessions -- or one panel's keys, or
  one session's process tree read by INSPECT -- compacts it to `ask.context_kb` (120 KB;
  `ask.py:snapshot` cuts the largest section's lists and strings a step at a time, and drops bulky
  keys such as series and tails, `DROP_KEYS`), and runs the configured agent CLI (`codex exec` by
  default) as the service's user, `ask.max_concurrent` (1) at a time, for at most `ask.timeout_s`
  (240 s), with a short environment (`ask.AGENT_ENV`) in a directory of its own under the system's
  temporary one.

## Every tab: the header, the rail and the controls

| | figure | path | payload | code (service · page) |
|---|---|---|---|---|
| SH-1 | status pill | FEEDS, LIVE | feeds' `status`, `lag_s`, `max_age_s`; live `clock` | `feeds.py:resolve_dated` · `App.tsx:headline`, `pillOf` |
| SH-2 | `anonymized` pill | CONFIG | `anonymize.enabled` | `config.py:Config.as_dict` · `App.tsx:App` |
| SH-3 | `fake data` pill and note | FEEDS (health from LIVE) | `synthetic_hosts` | `health.py:CollectorHealth.synthetic_hosts` · `App.tsx:App` |
| SH-4 | newest record's age | FEEDS | `lag_s` | `feeds.py:newest_record_ts` · `App.tsx:headline` |
| SH-5 | feeds delayed | FEEDS | `lag_s` against `max_age_s` | `feeds.py:resolve_dated` · `App.tsx:headline` |
| SH-6 | login nodes reporting | LIVE | live `hosts` | `reducers/live.py:LiveReducer.result` · `App.tsx:App` |
| SH-7 | window picker and its state | BUILD (status), LIVE | `/api/windows` and the `windows` frame; live `window`, `backfill` | `windows.py:presets`, `resolve`, `PanelView.status` · `components/WindowPicker.tsx:WindowPicker` |
| SH-8 | user and agent-type choices | BUILD, the unfiltered view | `facets` | `reducers/facets.py:FacetReducer.result` · `components/WhoFilter.tsx:WhoFilter` |
| SH-9 | the rail's foot | LIVE, BUILD | transport, last frame, `window`, `full_built_at`, `window_status.cache` | `aggregator.py:Aggregator.panels` · `App.tsx:App` |
| SH-10 | classes shown | none | the URL's `cls` | `App.tsx:App` |
| SH-11 | the commands behind an I/O number (drawer) | IOCMDS | job `totals`, `rows` | `iocmds.py:IoCommands._read`, `Job.page` · `components/IoCmds.tsx:IoCmdsDrawer` |

#### SH-1 status pill

- **Shows:** `live`, or what is wrong with the data (`data delayed`, `partial data`, `no data yet`,
  `collector not found`), or how updates arrive (`refreshing every 15 s`, `connecting…`,
  `disconnected`); on a pinned clock, `frozen capture` and its `as of`.
- **Records:** none of its own: each required feed's status and newest record (FEEDS), the live
  frame's `clock`, and the transport (`api/useLive.ts:useLive`).
- **Computation:** `headline` takes the worst status of the required feeds, a feed whose `lag_s`
  passes its `max_age_s` being `stale`; `pillOf` words it, the data's problem before the
  transport's.
- **Cost:** a FEEDS report.

#### SH-2 `anonymized` pill

- **Shows:** that names leave the service as aliases
  ([Anonymized names](../../dashboard/README.md#anonymized-names)).
- **Records, computation, cost:** none; `/api/config` says `anonymize.enabled`.

#### SH-3 `fake data` pill and note

- **Shows:** that some host's records were written by the demo, and which hosts.
- **Records:** `meta`, `stop` and `residency_totals` carrying `"synthetic": true`.
- **Computation:** `CollectorHealth.feed` marks a host whose record carries the key;
  `Aggregator.refresh_feeds` lists them.
- **Cost:** O(1) a record, in the live dispatch.

#### SH-4 newest record's age

- **Shows:** how old the newest record of the freshest required feed is.
- **Records:** the last record of the newest file of each feed (`ts`).
- **Computation:** `newest_record_ts` reads the newest file's tail, growing the read until it holds
  a whole line; `lag_s` is the clock less that record's time; the page shows the least over the
  required feeds.
- **Cost:** one tail read per feed per report. On a capture, whose newest file is a `.gz`, the read
  needs the file's inflater index, built once per process.

#### SH-5 feeds delayed

- **Shows:** how many required feeds are older than their own limit, each on hover.
- **Computation:** `lag_s` against `feeds.<feed>.max_age_s` (300 s for the eBPF tier, 900 s for the
  node tier), as SH-4 reads them.

#### SH-6 login nodes reporting

- **Shows:** how many login nodes reported, named on hover.
- **Records, computation, cost:** the live frame's `hosts`, as LV-23.

#### SH-7 window picker and its state

- **Shows:** the preset windows, a typed one, a clamp (`asked 30d · showing 7d`), how much of the
  window the live bins hold (`filled 5h of 7d`), the view's build state and percentage, its drift
  once past half its budget, and the backfill's progress
  ([The window control](../../dashboard/web/README.md#the-window-control)).
- **Records:** none of its own.
- **Computation:** `presets` (the configured presets that retention can serve), `resolve` (the
  clamp and its note), `PanelView.status` (state, progress by bytes or, merging, by hour-reads,
  drift, cache), sent as `/api/windows` and as a `windows` frame when a material field changes
  (checked every 2 s, `app.py:windows_loop`); `retained_minutes` from the oldest live bin
  (`RollingBuckets.oldest_epoch`); the live frame's `backfill`.
- **Cost:** a status per held view per frame.

#### SH-8 user and agent-type choices

- **Shows:** the 50 users with the most exits in the window and every agent type, each with its
  class mix, to narrow the page by.
- **Records:** `exit`: `user`, `agent_type`, `_a3`.
- **Computation:** `FacetReducer.feed` counts exits per user and per agent type, by class;
  `result` ranks them. Served from the unfiltered view of the window only (`Aggregator.facets`),
  never a build of its own, so the choices stay while a filtered view builds.
- **Cost:** two dict updates a record; the user map holds every user; a sort per read.

#### SH-9 the rail's foot

- **Shows:** whether updates stream or are polled, when the last arrived, the window and the
  retention held, when the panels were built and whether from a saved build (`· restored`), and the
  page's own build stamp.
- **Computation:** `full_built_at` is the view's `built_at`, else the node tier's; `restored` is the
  view's `cache.source` being `snapshot`.

#### SH-10 classes shown

- **Shows:** how many of the three class chips are on. The URL's state; no data.

#### SH-11 the commands behind an I/O number

- **Shows:** every command line behind the clicked number, each once, with its processes, disk
  bytes read and written, bytes requested through `read()` and `write()`, bytes sent and received,
  CPU, users and when; sorted by the quantity clicked, 200 rows a page (up to 1,000), with totals
  and how far the read got.
- **Records:** `exit`: `args` (else `_command`), `_eff`, `io.rd_mb`, `io.wr_mb`,
  `io.rchar_mb`, `io.wchar_mb` (each process's own: `pid`, `ppid`, `host`, `io.scope` for the
  join), `net_tx_bytes`, `net_rx_bytes`, `cpu_s`, `user`, `_ts_epoch`, and
  the fields the click filters on (`_a3`, `agent_type`, `_eff` or its bucket, `session_key` and
  `agent_pid`, `host`, `cwd`).
- **Computation:** IOCMDS. Per record a byte check (the event kind, then user, agent pids,
  directory, host), a parse, `iocmds.py:predicate`, and adds into its command line's group (up to
  1,000,000 groups; past that only the totals). A blast-radius click's agent pids are widened to
  every tool-call key the window's process index walks up to them (`agent_keys`). The page polls
  every 600 ms; `Job.page` sorts the groups for each page.
- **Cost:** a read of the window's bytes per lookup, whatever it matches (IOCMDS above).

## Live

The live tier at the top and in the tables that name now; the window's reducers in the plates and
charts that name the window.

| | figure | path | payload | code (service · page) |
|---|---|---|---|---|
| LV-1 | plate: processes exited / min | LIVE | `now.exits_per_min_by_class` | `reducers/live.py:LiveReducer.result` · `tabs/LiveTab.tsx:LiveTab` |
| LV-2 | plate: sessions · last 10 min | LIVE | `now.roots`, `now.procs`, `now.live_sessions_by_approval` | `LiveReducer.result` · `tabs/LiveTab.tsx:LiveTab` |
| LV-3 | plate: active users | LIVE | `users.total`, `users.by_class`, `users.with_live_agent` | `LiveReducer.users`; `roster.py:UserRoster.totals` · `tabs/LiveTab.tsx:LiveTab` |
| LV-4 | plate: agent processes exited | BUILD | `tool_mix.by_class[c].calls`, `tool_mix.by_type` | `reducers/tools.py:ToolMixReducer._rows`, `_by_type` · `components/WindowPlates.tsx:WindowPlates` |
| LV-5 | plate: agent sessions | BUILD (on read) | `trajectories.summary`; `sandbox.approval_by_class_sessions`, `sandbox.by_class_sessions` | `TrajectoryReducer.summary`; `SandboxReducer.result` · `components/WindowPlates.tsx:WindowPlates` |
| LV-6 | plate: risky agent actions | BUILD | `risk.dangerous.rows`, `risk.secrets.rows`; `sandbox.tool_calls` | `reducers/risk.py:RiskReducer.result` · `components/WindowPlates.tsx:WindowPlates` |
| LV-7 | chart: processes exited per minute | LIVE | `rate` | `buckets.py:RollingBuckets.series` · `tabs/LiveTab.tsx:LiveTab`, `charts/primitives.tsx:Lines` |
| LV-8 | table: who uses what | BUILD | `tool_mix`, `io_process`, `resources`, `trajectories.summary`, `slurm` | (no reducer of its own) · `tabs/OverviewTab.tsx:OverviewTab` |
| LV-9 | chart: CPU seconds per minute | BUILD | `resources.series.by_class[c].cpu_s` | `ResourceReducer._series_add`, `series` · `tabs/LiveCharts.tsx:WindowCharts`, `charts/TimeChart.tsx:TimeChart` |
| LV-10 | chart: memory; table: peak and average | BUILD | `resources.series.resident[c].rss_mb` | `ResourceReducer._tick`, `series` · `tabs/LiveCharts.tsx:WindowCharts` |
| LV-11 | chart: each class's share of the processes exited | BUILD | `resources.series.by_class[c].n` | `ResourceReducer.series` · `tabs/LiveCharts.tsx:WindowCharts` |
| LV-12 | chart: each class's share of the CPU time | BUILD | `resources.series.by_class[c].cpu_s` | `ResourceReducer.series` · `tabs/LiveCharts.tsx:WindowCharts` |
| LV-13 | chart: runqueue wait; table: run and wait | BUILD | `series.by_class[c].sched_wait_s`; `by_class[c].sched_run_s`, `sched_wait_s`, `schedstat_ratio` | `ResourceReducer.feed`, `series`, `result` · `tabs/LiveCharts.tsx:WindowCharts` |
| LV-14 | chart: D-state by tool; table: every tool that stalled | BUILD | `series.stall_by_tool`, `series.stall_other`; `stall_by_tool` | `ResourceReducer._series_add`, `result` · `tabs/LiveCharts.tsx:WindowCharts` |
| LV-15 | chart: major page faults | BUILD | `series.by_class[c].maj_flt` | `ResourceReducer.series` · `tabs/LiveCharts.tsx:WindowCharts` |
| LV-16 | charts: disk read, disk written | BUILD; a point opens IOCMDS | `series.by_class[c]`: `rd_mb`, `rchar_mb`, `wr_mb`, `wchar_mb` | `ResourceReducer.series` · `tabs/LiveCharts.tsx:WindowCharts` |
| LV-17 | chart: network | BUILD; a point opens IOCMDS | `series.by_class[c]`: `net_tx_mb`, `net_rx_mb` | `ResourceReducer.series` · `tabs/LiveCharts.tsx:WindowCharts` |
| LV-18 | charts: Slurm queue checks, Slurm jobs submitted | BUILD | `slurm.series.by_class[c]`: `queries`, `jobs` | `reducers/slurm.py:SlurmReducer._bin`, `_series` · `tabs/LiveCharts.tsx:WindowCharts` |
| LV-19 | active users: running now (chips) | LIVE | `users.running_by_type` | `LiveReducer.users`, `_running` · `tabs/LiveUsers.tsx:Summary` |
| LV-20 | active users: one row per account (table) | LIVE; `/api/live/users` past the frame | `users.rows`, `users.rows_total` | `LiveReducer.users`, `_row` · `tabs/LiveUsers.tsx:UsersPanel` |
| LV-21 | active users: an account opened (three boxes) | LIVE (`/api/live/user`); BUILD for "started by" | `row`, `sessions`, `recent`; `cascades.trees` | `LiveReducer.user_detail` · `tabs/LiveUsers.tsx:Detail`, `nestedAgents` |
| LV-22 | active users: every process (exit stream) | LIVE | `events` | `LiveReducer._exit`, `result` · `tabs/LiveUsers.tsx:Stream` |
| LV-23 | table: login nodes right now | LIVE | `hosts` | `LiveReducer._totals`, `result` · `tabs/LiveTab.tsx:LiveTab`, `HostBreakdown` |

#### LV-1 processes exited / min

- **Shows:** the processes that exited in the last finished minute, by class, and the agent share.
- **Records:** `exit`: `_a3`, `_ts_epoch`.
- **Computation:** `LiveReducer._exit` adds each exit to its minute's bin by class
  (`RollingBuckets.add`); `result` reads the minute before the current one
  (`RollingBuckets.last_complete_bin`). A minute no record landed in is null for every class; in a
  minute that holds records a class with none is 0. The page sums the classes.
- **Cost:** a dict increment a record; one bin per frame. A record stamped more than
  `live.clock_skew_s` (120 s) ahead is dropped and counted.

#### LV-2 sessions · last 10 min

- **Shows:** agent and IDE sessions whose process tree sent a `residency` tick in the last 10
  minutes, and human sessions with an exit in that time, by class; under it the processes running,
  summed from the hosts' newest ticks, and the live agent sessions launched with a bypass flag and
  in auto mode.
- **Records:** `residency` (`tree_root_pid` or `agent_pid`, `host`, `_a3`, `_ts_epoch`);
  `residency_totals` (`_by_class`, the collector's `by_actor3` under the dashboard's classes:
  `n_procs`); a human `exit` (`host`, `_kt`, `_kv`); the agent roots, any record of a process whose
  role is `agent` with `pid == agent_pid` (`_approval`).
- **Computation:** `_residency` keeps the newest tick per (host, tree root), `_totals` the newest
  totals per host, `_exit` the newest human exit per login scope, terminal or account, `_root`
  each root's newest record. `result` counts the trees ticking within `LiveReducer.LIVE_S` (600 s)
  by the tick's class, adds the human sessions when the newest exit is that recent, sums the hosts'
  per-class `n_procs`, and counts the roots seen within 600 s by approval. The plate's memory clause
  reads `now.rss_gb` as one number, and the service sends a per-class map, so it is never shown.
- **Cost:** O(1) a record; per frame one pass over the held trees, hosts and roots. Not cut by the
  window: it is a snapshot of now.

#### LV-3 active users

- **Shows:** the distinct accounts that ran something in the window or have a session running now,
  per class, and how many have an agent running.
- **Records:** `exit`: `user`, `_a3`, `agent_type`, `cpu_s`, `io.rd_mb`, `io.wr_mb` (each
  process's own, `LiveReducer._own_io`: the tail takes a parent's reaped children off at its exit;
  the backfill, newest first, meets the parent first and takes each child off it as it comes),
  `net_tx_bytes`, `net_rx_bytes`, `_ts_epoch`; `residency` for what runs now.
- **Computation:** `UserRoster.add` puts each exit in its account's slot of a 10-minute bin (an
  hourly one past a day); `UserRoster.totals` sums the window's closed bins, cached per window and
  made again when a bin closes, the window's edge crosses one, or at most every 15 s for a late
  record, then adds the open bin; `LiveReducer.users` counts the accounts over the totals and the
  live trees (`_running`). A window's edge lands on a bin boundary: 10 minutes inside the last day,
  an hour beyond.
- **Cost:** per record, a walk of the account's slot; per frame, one pass over the accounts.
  Measured: the roster's first design held 640 MB and took up to 600 ms a frame at 2,000 users
  (`roster.py`); the present one is not measured.

#### LV-4 agent processes exited

- **Shows:** the agent processes that exited in the window, the other classes beside them, the
  agent share of every process -- out of every class counted, `unlabeled` too, whatever the class
  chips show (`lib/shares.ts`) -- and the agent types on hover.
- **Records:** `exit`: `_a3`, `agent_type`, `_eff`, `_command`, `_sid`, `pid`, `ppid`,
  `_shell_resolved`, `cpu_s`.
- **Computation:** `ToolMixReducer.feed` counts an exit under its class, and an agent's also under
  its agent type, except a shell whose effective command was resolved from its argv and whose
  captured direct child, in the same session, resolved to the same tool: the shell-wrapper dedup,
  through a `(session, parent pid)` index of the children's tools. A child exits before the shell
  that waited on it, so one pass in time order is exact. `_rows` totals each class's `calls`;
  `_by_type` splits a class by agent type.
- **Cost:** a few counter adds and one index lookup a record; the index holds
  `reducers.tool_mix.dedup_sessions_max` (200,000) parents, the oldest dropped first, and is carried
  from hour to hour when merging. Per read, sums over the 12 kinds of tool.

#### LV-5 agent sessions

- **Shows:** the agent sessions of the window -- one-off probes left out -- the other classes beside
  them, and how many agent sessions launched with a bypass flag, in auto mode, and ran tool calls of
  which none was sandboxed.
- **Records:** for sessions, `exit` (`_sid`, `_kt`, `_kv`, `_a3`, `_ts_epoch`) and each root's own
  record (role `agent`, `pid == agent_pid`); for the launch line, as IN-2 and IN-4.
- **Computation:** `TrajectoryReducer.summary` counts each folded session whose last record is in
  the window and, for agents, whose root was seen (`rooted`; an agent-class key with no root is a
  probe, counted apart). The launch line is `SandboxReducer.result`'s sessions by approval at
  launch and by the verdict of their tool calls (`session_state`).
- **Cost:** the summary walks every session of the window each time `trajectories` is made (SE-2);
  the sandbox walks every tool call of the window each time its payload is made (IN-2).

#### LV-6 risky agent actions

- **Shows:** the processes whose command line matched a dangerous-command rule plus the credentials
  found on command lines, by class; under it each count, and the share of agent tool calls that ran
  sandboxed and how many sandboxes failed to start.
- **Records:** `exit`: `_eff` or `_command`, `args`, `_a3`, `user`, `host`,
  `agent_type`, `cpu_s`, `exit_code`, `signal`.
- **Computation:** `RiskReducer.feed` tests `"<tool> <args>"` against every rule a section shows
  (`_matches`: a literal gate first, then the regex; the verdict cached per tool and argv); the
  plate sums the rule rows' `classes` over the commands and credentials sections. The sandbox line
  is IN-1's.
- **Cost:** a cache lookup a record and, on a miss, one regex search per rule whose gate passes; the
  cache holds 50,000 command lines and is cleared when full. Before the cache, matching was half of
  a window build: 2.5 M regex searches for a 6 h window (`risk.py`).

#### LV-7 processes exited per minute

- **Shows:** the processes that exited each minute of the window, a line per class; a minute with no
  record is a hole in the line.
- **Records:** `exit`: `_a3`, `_ts_epoch`.
- **Computation:** as LV-1; `RollingBuckets.series` gives a row per minute of the window, oldest
  first, sliced from the same bins for any window. A row is made again only when a record landed in
  its bin since (`_rows`, keyed on each bin's version).
- **Cost:** per frame, one pass over the window's bins (10,080 at 7 d), most of them cached rows;
  the page builds its points in one pass.

#### LV-8 who uses what

- **Shows:** each class's share of the window in eleven units side by side: processes, CPU time,
  disk read, disk written, network sent, network received, peak memory and sessions on the login
  nodes, and jobs, GPUs and GPU-hours asked of Slurm.
- **Records:** those of the payloads it reads: `exit` (processes, CPU; LV-4), `exit`
  `io` (each process's own) and network bytes (`IoReducer`), `residency_totals` (memory; LV-10),
  sessions (LV-5), `submit` (`gpus`, `array`, `time_limit_s`; SL-3).
- **Computation:** none of its own. `OverviewTab` reads `tool_mix.by_class[c]` `calls` and `cpu_s`,
  `io_process.by_class[c]` `rd_mb`, `wr_mb` and the `net_tx_mb`/`net_rx_mb` accumulators' totals,
  the highest bin of `resources.series.resident[c].rss_mb` (the largest process on hover, from
  `resources.by_class[c].top`), `trajectories.summary[c].sessions`, and `slurm.by_class[c]` `jobs`,
  `gpus`, `gpu_h`; a cell is the class's share of its row over every class that row's panel counted,
  `unlabeled` too, not the classes shown, so a share does not move with the class chips; a class
  counted with no reading of the row (null, as the network bytes of a class none of whose processes
  carried them) leaves the row no share, a dash, and says why (`lib/shares.ts`). The processes and
  CPU rows are the tool mix's, after the shell-wrapper dedup. A byte cell opens IOCMDS for its
  class.
- **Cost:** its reducers'.

#### LV-9 CPU seconds per minute

- **Shows:** CPU seconds per minute by class over the window, summed by exit time: a process's
  whole-life CPU lands in the bin it exited in.
- **Records:** `exit`: `_a3`, `_ts_epoch`, `cpu_s`.
- **Computation:** `ResourceReducer._series_add` counts the record in its bin by class and adds each
  of its ten series fields (CPU, runqueue wait, D-state wait, major faults, four `io` byte counts,
  network sent and received), with a count of the records that had the field, so a bin whose
  records all lacked it is null; `series` lays the bins on one axis, null where a class has no
  record; the page divides by the bin's minutes. The width is `resources.py:bin_for`: the first of
  1, 2, 5, 10, 15, 30 minutes, 1, 2, 3, 6, 12, 24 hours that keeps the window within 240 bins (1 h
  per minute, 24 h per 10 minutes, 7 d per hour). A merged window folds the hours' 60 s bins into
  its own width (`ResourceReducer.merge`).
- **Cost:** about ten dict updates a record; per read one pass over at most 240 bins (960 held,
  `SERIES_SPAN_CAP`, for a window that drifted or a skewed clock).

#### LV-10 memory, and peak and average

- **Shows:** resident memory by class over the window, sampled while processes ran; a table of each
  class's peak, when, and average.
- **Records:** `residency_totals`: `host`, `_ts_epoch`, `_by_class` (`rss_mb`).
- **Computation:** `ResourceReducer._tick` sums each host's per-class `rss_mb` over its ticks in a
  bin; `series` takes each host's mean over its ticks and sums the hosts; the page finds the peak
  and the mean of the bins. Empty in a view filtered by user: a tick names no user
  (`ViewFilter.NOT_NARROWED`).
- **Cost:** a few adds a tick; per read, bins times hosts.

#### LV-11 each class's share of the processes exited

- **Shows:** per bin, each class's share of the processes that exited.
- **Records:** as LV-9 (`n`, the records in a bin).
- **Computation:** in the page: a class's count over every class's, not only the classes shown, so a
  share does not move with the class chips.

#### LV-12 each class's share of the CPU time

- **Shows:** per bin, each class's share of the CPU seconds; computed as LV-11 from LV-9's series.

#### LV-13 runqueue wait, and run and wait

- **Shows:** seconds spent waiting for a CPU per minute, by class; a table of each class's run time,
  wait time and their ratio.
- **Records:** `exit`: `sched_wait_s`, `sched_run_s`, `cpu_s`.
- **Computation:** the series as LV-9; the table is an accumulator per class
  (`reducers/__init__.py:Acc`: sum, present count, coverage) of `sched_run_s` and `sched_wait_s`,
  and `schedstat_ratio`, the median of `cpu_s / sched_run_s` over the processes with more than 1 s
  of CPU.
- **Cost:** the ratio keeps one float per such process per class, unbounded, sorted at each read.

#### LV-14 D-state by tool, and every tool that stalled

- **Shows:** seconds in uninterruptible sleep per minute, a line for each of the six tools that
  stalled longest and one for the rest; a table, titled "every tool that stalled", of the 20 that
  stalled longest (processes, time, stalls, longest).
- **Records:** `exit`: `dstate_wait_s`, `dstate_episodes`, `dstate_max_s`, `_eff` or
  `_command`.
- **Computation:** `_series_add` adds each record's `dstate_wait_s` to its bin by tool; `feed`
  counts each tool's stalled processes, time, stalls and longest; `result` ranks the tools by time
  (20 in the table, 6 drawn), and `series` draws 0 for a tool in a bin where D-state was measured
  and null where it was not. Over every class: the class chips do not narrow it.
- **Cost:** a dict add per stalled record; a sort over the tools per read.

#### LV-15 major page faults

- **Shows:** major page faults per minute by class: `maj_flt` summed by exit time, as LV-9.

#### LV-16 disk read, disk written

- **Shows:** per class, the bytes that reached the disk (solid) against the bytes requested through
  `read()` or `write()` (dashed), per minute; the gap is the page cache. A point opens the commands
  of that class in that bin (IOCMDS).
- **Records:** `exit`: `io.rd_mb`, `io.rchar_mb`, `io.wr_mb`, `io.wchar_mb`, each process's own
  (`ResourceReducer`'s `OwnIo`); null for another user's process, whose `/proc/<pid>/io` the
  collector could not read.
- **Computation:** as LV-9.

#### LV-17 network

- **Shows:** per class, bytes sent (solid) and received (dashed) per minute by the processes that
  exited; a point opens IOCMDS.
- **Records:** `exit`: `net_tx_bytes`, `net_rx_bytes` (null where the collector kept no
  count).
- **Computation:** as LV-9, in MB.

#### LV-18 Slurm queue checks, Slurm jobs submitted

- **Shows:** per class, the scheduler queries and the jobs submitted per bin; offered only when the
  window holds Slurm records.
- **Records:** `submit` (an outcome of `job`); `exit` of a scheduler query command
  (`tool_buckets.SLURM_Q`: `squeue`, `sacct`, `scontrol`, `sinfo`, ...).
- **Computation:** `SlurmReducer._bin` counts each job and each query in its bin by class, at the
  resources' width; `_series` makes a bin no class has a Slurm record in null (a gap) and a class
  with none in a bin another class has records in 0.
- **Cost:** O(1) a record; per read one pass over the bins.

#### LV-19 active users: running now

- **Shows:** per agent and IDE type, how many accounts have such a session running now; a chip
  narrows the table to them.
- **Records:** `residency` (`user`, `_a3`, `agent_type`, `_ts_epoch`).
- **Computation:** `LiveReducer._running` groups the trees ticking within 600 s by account, class and
  type; `users` counts the accounts per type. Under a page filter the page counts the rows it kept.

#### LV-20 active users: one row per account

- **Shows:** a row per account: agents and IDEs running now by type, processes running, processes
  exited in the window as a bar by class, CPU time, disk read and written, network sent and
  received, GPU-hours asked, last active. The columns are the reader's (defaults `ui.user_columns`).
- **Records:** as LV-3, and `submit` (`gpus`, `time_limit_s`, `user`, `_ts_epoch`) for GPU-hours.
- **Computation:** `LiveReducer.users` orders the accounts -- agents running now first, then the most
  processes, then the name -- and makes full rows (`_row`: the roster's (class, agent type) slots
  decoded, the trees running now, their census's `n_procs`, GPUs times time limit over the window's
  submissions that named both) for the first `live.user_rows` (100). A search, a running-now chip
  or a page filter, when the frame does not hold every account, asks `/api/live/users`, which
  searches every account. The page applies the user and agent-type filter row by row
  (`pageMatch`). A byte cell opens IOCMDS for the account.
- **Cost:** per frame a pass over every account and 100 full rows.

#### LV-21 active users: an account opened

- **Shows:** the account's sessions running now (agent or IDE, version, surface, launch badges,
  running for, processes, memory now, peak and average over its census ticks, CPU, and the agents
  running inside it with their own launch); what it ran in the window by class and agent type, with
  the agents that another agent started split out; and its last 10, 25 or 100 exited processes,
  however long ago, on any host.
- **Records:** as LV-2 and LV-3; the account's own exit tail.
- **Computation:** `/api/live/user` calls `LiveReducer.user_detail`: the account's row, its trees
  ticking within 600 s with the agent roots inside each (`_inner`, `_fronted`), and its own tail
  (`user_tails`, newest first, 100 deep, masked when served). "Started by" is computed in the page
  from the window's agent trees (`cascades.trees`, BUILD): per child agent type, the processes of
  the agents a parent agent type started (`nestedAgents`).
- **Cost:** per open, a roster read and a pass over the live trees and roots; fetched again when the
  count asked for or the window changes.

#### LV-22 active users: every process

- **Shows:** the newest exited processes of every user on every host, newest first (the last 10, 25
  or 100), narrowed by the search, a host, the page filter and the class chips.
- **Records:** `exit`: time, host, user, class, agent type, command and argv, purpose,
  depth, pid, duration, CPU, peak memory, disk bytes (its own, as LV-3), network bytes, exit
  code, signal, sandbox,
  approval, session key.
- **Computation:** `LiveReducer._exit` keeps a deque of `live.event_tail` (500) rows, newest first;
  `result` cuts it to the window and masks each row's argv once (`_served`). The page filters and
  sorts.
- **Cost:** a row dict a record, not made during the backfill once the tails are full. A tail, not
  an aggregate: a wider window does not deepen it.

#### LV-23 login nodes right now

- **Shows:** per login node: sessions, processes running (split by class or by agent type), memory,
  processes in D-state, users, external TCP sockets.
- **Records:** `residency_totals`: `host`, `trees`, `_by_class` (`n_procs`, `threads`, `rss_mb`,
  `d_state`, `users`), `by_agent_type`, `_tcp_by_class` (`open`, `ext`, `estab`); a host with only
  `residency` gets a row that says so.
- **Computation:** `_totals` keeps each host's newest tick; `result` sums the per-class fields per
  host. `users` adds the collector's per-class counts, so one person in two classes counts twice.
  `load₁` and `CPU` would come from the node tier, which is not joined here: they are always null,
  and the page drops a column with no value.
- **Cost:** per frame one pass over the hosts.

## Users

See [Users: a portrait of each user](../../dashboard/README.md#users-a-portrait-of-each-user).
Everything on the tab is composed on read (BUILD on read) from the view's `trajectories`, `sandbox`
and `slurm` reducers by `portraits.py`, for the page's filter, and from the live tier's agent roots
for what runs now (`Aggregator._running_fn`). `/api/users` and `/api/user` are read again every
15 s while the tab is shown.

A session here is one agent run: its rows on several days together, keyed (host, agent pid, user)
(`portraits.agent_sessions`, `trajectories._agent_key`), rooted, and its user's. Common cost:
`Aggregator._held` keeps a view's inputs (`portraits.inputs`: the sandbox's tool-call verdict per
agent session, which walks every tool call of the window, and `slurm_by_user`, which folds every
Slurm key), its roster, and each portrait opened, and makes them again once the view has ingested
and 5 s have passed, or after 60 s, under the aggregator's lock. A roster groups every folded
trajectory row by agent and walks each session's call log. Measured on 500 synthetic sessions with
100,000 calls: a roster about 0.3 s, a portrait read from the roster's inputs about 20 ms (README).

| | figure | path | payload | code (service · page) |
|---|---|---|---|---|
| US-1 | plates: agent users, agent sessions, launched with a bypass flag, driven from a script | BUILD on read | `/api/users` `fleet`; `users[].running` | `portraits.py:roster`, `_fleet` · `tabs/UsersTab.tsx:Plates` |
| US-2 | trait chips | BUILD on read | `fleet.traits` | `portraits.py:traits`, `_fleet` · `tabs/UsersTab.tsx:Roster` |
| US-3 | roster: a row per user, and how they leverage agents | BUILD on read | `users[]` | `portraits.py:_user_row`, `leverage` · `tabs/UsersTab.tsx:Roster` |
| US-4 | figures: each user drawn from their row | BUILD on read | `users[]` | `portraits.py:_user_row` · `components/PortraitFigure.tsx:PortraitFigure` |
| US-5 | portrait: head | BUILD on read | `/api/user` `row`, `fleet` | `portraits.py:portrait` · `tabs/UsersTab.tsx:Head`, `Leveraging` |
| US-6 | portrait: how they launch agents | BUILD on read | `row.agent_types`, `surfaces`, `approval`, `sandbox`; `fleet` | `portraits.py:_user_row` · `tabs/UsersTab.tsx:Launch`, `Shares` |
| US-7 | portrait: rhythm | BUILD on read | `row.hours`, `row.grid`, `row.peak_open`, `sessions` | `portraits.py:_user_row`, `_peak_open` · `tabs/UsersTab.tsx:Rhythm`, `HourBars`, `WeekGrid`, `Timeline` |
| US-8 | portrait: what their agents do | BUILD on read | `row.cats`, `phases`, `commands` | `portraits.py:portrait` · `tabs/UsersTab.tsx:Work` |
| US-9 | portrait: projects and tasks | BUILD on read | `row.projects`, `tasks` | `portraits.py:portrait`; `TrajectoryReducer.project_of` · `tabs/UsersTab.tsx:Tasks` |
| US-10 | portrait: at the scheduler | BUILD on read | `row.slurm` | `portraits.py:slurm_by_user` · `tabs/UsersTab.tsx:Scheduler` |
| US-11 | portrait: sessions | BUILD on read | `sessions` | `portraits.py:portrait` · `tabs/UsersTab.tsx:Sessions`, `PhaseBar` |

#### US-1 plates

- **Shows:** users with an agent session in the window and how many have one running now; agent
  sessions, the median time one was open and its median tool calls; the share of sessions launched
  with a bypass flag, of those whose launch is known; the share driven from a script, of those whose
  surface is known.
- **Records:** the trajectories' agent sessions (`exit`: `_sid`, `_kt`, `_kv`, `user` or `uid`,
  `_ts_epoch`; the call log from `args`), each root's own record (`_approval`, `_surface`), the live
  tier's roots.
- **Computation:** `roster` builds every user's row (US-3); `_fleet` counts users and sessions, the
  medians (`_q`, by sorting) and the totals of approval and surface. A session's surface and
  approval are its root's own (`TrajectoryReducer.surface_of`, `approval_of`).

#### US-2 trait chips

- **Shows:** each trait held in the window and how many users hold it; a chip narrows the roster.
- **Computation:** `traits` per user, each one rule (`portraits.RULES`) over the row's numbers;
  `_fleet` counts them.

#### US-3 roster

- **Shows:** a row per user: sessions, sessions by agent type, where they are driven from, the
  bypass share, tool calls with a bar by category, the typical session's length, the most open at
  once, active minutes by hour and when last active, traits; a line under each on how they leverage
  agents.
- **Records:** as US-1, plus each session's call log (a call's category, start and tag set; only
  Claude Code's and Codex's are read), the sandbox's tool-call verdicts, and the Slurm keys.
- **Computation:** `_user_row` per user: counters over sessions of agent type, surface, approval and
  tool-call verdict; calls (null when no session's calls are read); categories counted over the call
  logs; active minutes, the distinct minutes in which a call began, by local hour (`_Clock`);
  `peak_open` by a sweep over the sessions' first and last records (`_peak_open`); the 5 most
  frequent projects (`TrajectoryReducer.project_of`); traits; `leverage` by `RULES`. Rows sorted by
  sessions, then calls.
- **Cost:** a pass over every session's call log per roster.

#### US-4 figures

- **Shows:** each user drawn from their row: a part for each number, its numbers and rule on hover.
- **Computation:** in the page, from US-3's row; the only numbers the page adds are drawing caps (six
  robots, five sheets, ten lights).

#### US-5 portrait: head

- **Shows:** the user's figure, how they leverage agents point by point with the window's medians
  beside, their trait badges with each rule, and totals: sessions, tool calls, active minutes,
  processes, CPU, disk read and written, network sent and received, peak memory.
- **Records:** as US-3 for one user; the footprint is the trajectory rows' (`n`, CPU, each process's
  own disk bytes, network bytes, the largest peak memory).
- **Computation:** `portrait` makes the user's row with a weekday grid (`_user_row(grid=True)`) and
  reads it against the roster's `fleet`: the unfiltered view's when held, else the filter's
  (`fleet_of`).

#### US-6 portrait: how they launch agents

- **Shows:** four share rows -- agent, driven from, approval at launch, tool calls sandboxed -- each
  the user's share of the known values with the window's share as a tick, and the values that say
  nothing counted under them.
- **Computation:** the row's counters against `fleet`'s, divided in the page over the known states.

#### US-7 portrait: rhythm

- **Shows:** the typical and longest session, the most open at once and active minutes, each with
  the window's median; active minutes by hour of day; by weekday and hour for a window of two days
  or more; and each session over the window from its first record to its last, packed into rows.
- **Computation:** the row's `wall_p50_s`, `wall_max_s`, `peak_open`, `active_min`, `hours` and
  `grid`; the timeline is the 60 newest sessions (`SESSIONS_SHOWN`), packed in the page into at most
  12 rows.

#### US-8 portrait: what their agents do

- **Shows:** tool calls by category, counted, with the window's share as a tick; time in each phase,
  idle apart; the commands their agents ran most.
- **Records:** the call logs; the phase blocks of the user's newest sessions.
- **Computation:** `row.cats`; `phases.time_s`, summed over the phase blocks of the newest
  `PHASE_SESSIONS` (80) sessions whose calls are read (`TrajectoryReducer.phase_block`, as SE-3);
  the 8 most frequent masked previews over the user's calls.
- **Cost:** up to 80 phase blocks per portrait, each made as SE-3 says and kept in the reducer's
  block cache (4,096 blocks).

#### US-9 portrait: projects and tasks

- **Shows:** the projects their sessions worked in; tasks and lone stretches, how many are labeled in
  the agent's own words (commit, pull request, log), and the newest 40 tasks.
- **Computation:** `row.projects` (5 and a total); `tasks` over the same 80 blocks.

#### US-10 portrait: at the scheduler

- **Shows:** what the user's agent sessions asked of Slurm: jobs, refusals, queue checks and checks
  per job, polling sessions and their hours, the longest polling stretch, the `--parsable` share,
  cancels, GPU-hours.
- **Records:** as SL-2.
- **Computation:** `slurm_by_user` folds the Slurm reducer's agent keys into sessions
  (`SlurmReducer._sessions`) and reads each session's polling with `SlurmReducer._polls`; agent
  sessions only.

#### US-11 portrait: sessions

- **Shows:** the newest 60 sessions: start, agent, surface, launch badges, category and headline,
  how long open, tool calls, tasks and how many quoted, a bar of time by phase, CPU, and a link to
  its process tree.
- **Computation:** each session's sums (`portraits._session`), and, where its calls are read, its
  block's `headline`, `category`, tasks and phase times (the 60 served are among the 80 whose blocks
  US-8 reads).

## Agents

See [Agents: subagents and blast radius](../../dashboard/README.md#agents-subagents-and-blast-radius).

| | figure | path | payload | code (service · page) |
|---|---|---|---|---|
| AG-1 | agents by type (strip) | BUILD | `cascades.trees[].nodes`, `cascades.workspace.agents` | `reducers/cascades.py:CascadeReducer.result` · `tabs/AgentsTab.tsx:typeStats`, `TypeStrip` |
| AG-2 | agents and their subagents (list) | BUILD | `cascades.trees`, `cascades.summary` | `CascadeReducer.result`, `_parent`, `_tree` · `tabs/AgentsTab.tsx:AgentsTab`, `TreeRow`, `LoneRow`, `FoldRow`, `AgentFilter` |
| AG-3 | an agent tree (diagram) | BUILD | `cascades.trees[].nodes` | `CascadeReducer._tree`, `cascades.py:_pick` · `tabs/AgentsTab.tsx:TreeView`, `components/TreeDiagram.tsx:TreeDiagram` |
| AG-4 | blast radius: summary | BUILD | `cascades.workspace.agents[].self` or `.tree`: `summary` | `reducers/workspace.py:WorkspaceReducer.fold`, `view`; `CascadeReducer._workspace` · `tabs/AgentsTab.tsx:AgentBlast` |
| AG-5 | blast radius: written or deleted outside its start folder | BUILD | `.flags` | `WorkspaceReducer.view` · `tabs/AgentsTab.tsx:Flags` |
| AG-6 | blast radius: directories | BUILD; a byte cell opens IOCMDS | `.dirs` | `WorkspaceReducer.view` · `tabs/AgentsTab.tsx:DirTable` |
| AG-7 | how deep agents nest; fan-out (histograms) | BUILD | `cascades.depth_hist`, `cascades.fanout_hist` | `CascadeReducer.result` · `tabs/AgentsTab.tsx:AgentsTab`, `charts/primitives.tsx:GroupBars` |
| AG-8 | tool mix | BUILD; a byte cell opens IOCMDS | `tool_mix.buckets`, `tool_mix.by_class[c].rows`, `by_agent_type` | `reducers/tools.py:ToolMixReducer._rows` · `tabs/ToolPanels.tsx:ToolMixPanel` |
| AG-9 | top tools | BUILD; a disk or network bar opens IOCMDS | `tool_mix.tools`, `tool_mix.head` | `ToolMixReducer._tools`, `_head` · `tabs/ToolPanels.tsx:HeadPanel` |

#### AG-1 agents by type

- **Shows:** per agent type: agents, the processes and CPU of their sessions, how many were
  subagents, how many started one, how many wrote outside their start folder, how many reached
  another user's home.
- **Records:** the node records: any record (`exit`, `truncated`, `residency`, `conn`, `netio`,
  `submit`) of a process whose role is `agent`, `infra` or `client` (`pid`, `ppid`, `_role`,
  `agent_type`, `user`, `_command`, `args`, `cwd`, `tree_root_pid`, `_approval`,
  `_approval_detail`, `_surface`, `duration_s`); the trajectories' folded sessions; the workspace's
  views (AG-4).
- **Computation:** in the page (`typeStats`), over the agent nodes of the trees the service sent: a
  node's processes are its folded trajectory session's `n` (`tool_calls` on the node), its CPU the
  session's.
- **Cost:** counted over the trees sent (at most `reducers.cascades.top_trees`, 1,000, the deepest
  first), as the strip says when it is cut.

#### AG-2 agents and their subagents

- **Shows:** every tree with more than one agent, and every agent on its own -- three or more alike
  (user, type, start folder) fold into one row -- by user or as one list, running ones first; marks
  B (bypass flag), A (auto mode), U (tool calls not sandboxed), F (sandbox failed to start) and O
  (wrote outside its start folder), each with a count, as filters; the agents that ran nothing; the
  idle `claude daemon` spares counted.
- **Records:** as AG-1, and the sandbox's tool-call verdicts.
- **Computation:** `CascadeReducer.feed` keeps a node per (host, pid) and a census of exits by
  (agent type, role). `result`: the nodes last seen in the window; agents known only from their tool
  calls added; unclaimed `bg-spare` sessions made infrastructure (`_idle`); each node's parent found
  by `_parent` (the nearest node up the process index, at most 64 hops, refusing a link between two
  users; a `bg-pty-host` under its user's only daemon; else the collector's tree root); cycles cut; a
  Codex TUI or a Node launcher over the same agent made a client; each tree walked from its root
  (`_tree`) with each agent's level, fan-out, folded session (`traj.agent_sessions`), start folder
  and workspace summary, and the verdict of its tool calls (`sb.agent_calls`, `session_state`). The
  trees with agents are sorted by agent depth, agents, tool calls and recency, and cut to
  `top_trees`.
- **Cost:** a dict per node a record (at most `max_nodes`, 20,000, the rest counted) and a census
  counter. Per read, made each rebuild tick: a parent walk per node, the trajectories' fold, the
  workspace's fold and a view per agent, and a walk of every sandbox tool call. Not measured.

#### AG-3 an agent tree

- **Shows:** the picked tree's agents, and the infrastructure and clients they run under, as boxes
  with launch marks, version, surface, and how each parent was found.
- **Computation:** a tree sends at most `max_nodes_shown` (60) nodes (`_pick`: its shape first, then
  each parent's busiest agents that started none, a parent at a time; what the rest did counted on
  their parent as `folded`); infrastructure and clients with no agent below are counted
  (`hidden_nodes`), not sent. Laid out in the page (`TreeDiagram.layoutTree`).

#### AG-4 blast radius: summary

- **Shows:** for the picked agent, alone or with its subagents: its start folder, processes and the
  share that ran inside it, directories, the files its commands mention by read, write, delete and
  other (inferred), writes outside it, and the top-level places outside it reached.
- **Records:** `exit` of agent-class processes keyed on an agent pid (`_kt` `apid`):
  `cwd`, `cwd_source`, `io.rchar_mb`, `io.wchar_mb`, `io.rd_mb`, `io.wr_mb` (each process's own,
  over these records: `WorkspaceReducer`'s `OwnIo`), `args`, `argv_source`,
  `fork_only`, `_role`, `user`, `_ts_epoch`, `tree_root_pid`.
- **Computation:** `WorkspaceReducer.feed` per collector key: processes per `cwd` with their syscall
  and disk bytes (measured), and the paths each command line names, sorted by what the command does
  with them (inferred; each distinct argv parsed once by a regex tokenizer, `workspace.py:plan`). At
  read, `fold` merges the keys into their agent through the process index, and `view` collapses
  directories deeper than `collapse_depth` (4) below the start folder or a top-level root, folds a
  directory's quiet subdirectories, flags writes and deletes named outside the start folder, and
  groups the outside directories by where they are. The start folder is the agent's own cwd, or
  its first call's directory off `/`.
- **Cost:** measured in a 24 h build of rtx6000a's 2026-10-10 (1.89 M records, ~90 s): 2.4 s in
  `feed`, about 1.4 µs a record (`workspace.py`). Bounds: `max_keys` 150,000 (at the cap, keys last
  seen before the window go first), `max_dirs` 300 per key, `max_dir_entries` 400,000, 64 names per
  directory and 32 per command line, each excess counted; served for the 300 busiest agents
  (`max_agents`) and every agent that wrote outside.

#### AG-5 blast radius: written or deleted outside its start folder

- **Shows:** the paths a command named for writing or deleting outside the start folder, by where
  they landed, another user's home and system paths first, the agent's own scratch last.
- **Computation:** `view`: at most 400 paths kept per view and 16 served, the rest counted
  (`flags_more`); the page shows every loud one and the first three quiet ones.

#### AG-6 blast radius: directories

- **Shows:** each directory: processes that ran there (measured), bytes requested through `read()`
  and `write()` there (measured per process, credited where it ran), the files its commands mention
  (inferred), when it was seen.
- **Computation:** `view`'s rows, the 40 busiest (`max_dirs_shown`) and every flagged one. A byte
  cell opens IOCMDS for the agent's pids (and its subagents'), host and directory, a lookup the
  replicas can answer.

#### AG-7 how deep agents nest; fan-out

- **Shows:** trees by the most agents on one root-to-leaf path; the agents (and tree roots) that
  started an agent, by how many they started directly, in power-of-two bins.
- **Computation:** `result` counts every tree with agents in the window, sent or not (`_tree`
  records a fan-out for an agent or a root with an agent directly below it); the page bins the
  fan-out.

#### AG-8 tool mix

- **Shows:** the 12 kinds of tool (`tool_buckets.BUCKETS`), by class (a row adds to 100%) or by kind
  (a column adds to 100%), weighted by one resource at a time: processes, CPU, disk I/O, network,
  memory (each process's peak, summed).
- **Records:** `exit`: `_eff` (its kind by `tool_buckets.bucket`), `_a3`, `agent_type`, `cpu_s`,
  `io.rd_mb`, `io.wr_mb`, `net_tx_bytes`, `net_rx_bytes`, `peak_rss_mb`, and the dedup's fields
  (LV-4).
- **Computation:** `ToolMixReducer.feed` counts per (class, kind) and per (class, tool), and an
  agent's also per (agent type, ...), after the shell-wrapper dedup; disk I/O is each process's own
  (its `OwnIo`, fed every exit, a shell the dedup drops too); `_rows` gives each kind's
  counts, sums and shares per class; a page filter to one agent type uses that type's own counters
  (`by_agent_type`). The page computes a class's share of a row, out of every class counted, not
  the classes shown (`lib/shares.ts`); the total beside it is that whole.
- **Cost:** as LV-4. A byte cell opens IOCMDS by kind and class, read from the JSONL unless the
  page is filtered by user.

#### AG-9 top tools

- **Shows:** per class, the 12 tools with the most of one resource: processes, CPU, mean peak memory,
  disk I/O, network.
- **Computation:** `_tools` serves the union of the 12 busiest tools by processes, CPU, disk, network
  and mean peak memory, each row carrying every measure; the page sorts and cuts it to 12. `head`
  (processes and CPU shares) is drawn for a payload without `tools`.
- **Cost:** per read, `most_common` over every tool token of a class; the per-class tool maps are
  unbounded.

## Sessions

See [Sessions: tasks, phases, and processes](../../dashboard/README.md#sessions-tasks-phases-and-processes),
and for how the page draws them
[the web README's section](../../dashboard/web/README.md#sessions-tasks-phases-and-processes).

| | figure | path | payload | code (service · page) |
|---|---|---|---|---|
| SE-1 | plates: agent sessions, processes per session, session length, processes spent waiting | BUILD on read | `trajectories.summary` | `reducers/trajectories.py:TrajectoryReducer.summary` · `tabs/TrajectoriesTab.tsx:TrajectoriesTab` |
| SE-2 | sessions, ranked (list) | BUILD on read; LIVE for running | `trajectories.ranks`, `rows`; `rows[].running` | `TrajectoryReducer.feed`, `result`, `folded`, `_row`; `Aggregator._running_rows` · `tabs/TrajectoriesTab.tsx:SessionItem` |
| SE-3 | a row's phase band, task line and category chip; else its purpose ribbon | BUILD on read; LIVE for the live edge | `rows[].phases`, `purpose_rle`, `running.now` | `TrajectoryReducer._phases`, `_build_block`; `reducers/live.py:running_now` · `tabs/TrajectoriesTab.tsx:RowRibbon`, `components/PhaseBand.tsx:PhaseBand`, `components/CategoryChip.tsx:CategoryChip` |
| SE-4 | inspector: facts; purpose ribbon and tool tokens | BUILD | `row` | `TrajectoryReducer._row`; `proctree.py:lookup` · `tabs/TrajectoryInspector.tsx:Facts`, `ProcessMix`, `ToolTokens` |
| SE-5 | inspector: what was read, and its limits | INSPECT | `scan`, `bounds`, `links` | `proctree.py:_inspect` · `tabs/TrajectoryInspector.tsx:ReadLine` |
| SE-6 | inspector: headline, category, summary | BUILD | `row.phases.headline`, `category`, `summary` | `reducers/phase_tasks.py:name_tasks`, `reducers/phase_summary.py:summarize` · `tabs/TrajectoryInspector.tsx:TrajectoryInspector` |
| SE-7 | inspector: navigator | INSPECT; BUILD for tasks and phases | `nav`, `span`, `scan`; `row.phases` | `proctree.py:TreeBuilder._note`, `nav` · `tabs/TrajectoryNav.tsx:TrajectoryNav` |
| SE-8 | inspector: task list, or phase list | BUILD | `row.phases.tasks`, `list` | `phase_tasks.py:task_spans`, `name_tasks`; `phase_segment.py:segment` · `tabs/TaskList.tsx:TaskList`, `tabs/PhaseList.tsx:PhaseList` |
| SE-9 | inspector: process tree (tree or table) and its pop-out | INSPECT | `tree`, `timeline` | `proctree.py:TreeBuilder.feed`, `resolve`, `render` · `components/ProcDiagram.tsx:ProcDiagram`, `components/ProcTree.tsx:ProcTree`, `tabs/TreePopout.tsx` |
| SE-10 | inspector: map of the whole session, its tasks over its phases | BUILD; LIVE for the live edge | `row.phases`, `row.first`, `running.now` | `TrajectoryReducer._phases`, `_build_block`; `reducers/live.py:running_now` · `tabs/SessionMap.tsx:SessionMap`, `lib/phases.ts:bandAxis` |

#### SE-1 plates

- **Shows:** agent sessions in the window (users; the busiest user's share of agent processes;
  probes set aside; terminal or script against app), the median processes per agent session (p90,
  the most), the median session length (the longest), and the share of agent processes that only
  wait; the other classes beside each.
- **Records:** `exit` (`_sid`, `_kt`, `_kv`, `_ts_epoch`, `user`, `_purpose`); each root's own record
  (`_surface`).
- **Computation:** `TrajectoryReducer.summary` over every folded session whose last record is in the
  window: per class the rooted sessions and the probes, processes per session and wall time (first to
  last record in the window) with exact quantiles by sorting, waiting = processes whose purpose is
  `poll` or `slurm_mon`, processes per user, surfaces.
- **Cost:** a pass and two sorts over every session per read; made with SE-2.

#### SE-2 sessions, ranked

- **Shows:** the window's sessions ranked by one of seven measures -- recency, processes, CPU, peak
  memory, disk I/O, network, distinct tools; each row its key and date, surface, category, a live
  badge with the processes up now, user, tool calls (an agent whose commands are read) or processes,
  CPU, length, and the rank's own quantity. A user's sessions alone when opened from LV-20.
- **Records:** `exit`: `_sid`, `_kt`, `_kv`, `pid`, `ppid`, `cpu_s`, `_eff`, `_purpose`,
  `peak_rss_mb`, `io` (`rd_mb`, `wr_mb`, `scope`), `net_tx_bytes`, `net_rx_bytes`, `user` or `uid`,
  `tree_root_pid`, `_sandbox`, `_sandbox_ancestry`, `cwd`, `args`, `duration_s`; each root's own
  record (`_approval`, `_surface`); the process index; for running, the live tier's roots and tree
  ticks.
- **Computation:** `TrajectoryReducer.feed` per exit: a `Session` per collector key (host, day,
  class, key) with counts, CPU, run-length chains of tools and of purposes (400 runs), drift arrays,
  each process's own disk bytes (`own_io`: its figure less what its reaped children carried), and for
  an agent session its call log (`_phase`, bottom up). At read (`result`): sessions idle past the
  longer of 2 h and the window are dropped; every key is folded into its agent through the process
  index (`folded`, `_fold_all`, cached until a record, an eviction or the index changes); each rank
  is `heapq.nlargest(15)` over the window's sessions (recency over rooted ones only); each listed
  session gets a row (`_row`) with its phase block (SE-3). `Aggregator._running_rows` marks the
  newest row of each agent whose root's records arrived in the last 600 s, with its tree's census.
- **Cost:** per record a few adds, a run-length push, the drift arrays and, for an agent, the phase
  join (pending entries up to `pend_max`, 50,000). Per read, under the aggregator's lock: the fold of
  every held key when anything changed -- "half a second to most of one" at 24 h on rtx6000a
  (`proctree.py:lookup`) -- seven partial sorts over the sessions, and up to 105 rows. The page reads
  again every 5 s while the list is shown, and the service makes the payload again at most that
  often. Memory: `max_sessions` (150,000) keys per view, about 6 KB each; past it whole trees go,
  the least recently active first (`_shed`).

#### SE-3 a row's phase band, task line and category chip

- **Shows:** for an agent whose commands are read (Claude Code, Codex): a band of its phases, a tick
  where each task or lone stretch begins, a line naming its tasks (or the order of its phases), the
  category chip (the use that holds half its work), and at a running session's live edge what the
  census shows running; for any other row, the purpose of each process, run by run.
- **Records:** the session's call log (the agent's direct children, each a call named by
  `phase_rules`, with its command text, start, end and what ran under it), its pending entries, and,
  for the live edge, `residency` `by_command`.
- **Computation:** `_phases` and `_build_block`: the agent's day rows are folded into one block
  (`PhaseLog.fold`, at most `max_calls`, 20,000, calls), the open calls added from the pending join
  (`_open_calls`), then `phase_segment.prepared`, `phase_tasks.task_spans` (tasks),
  `phase_segment.segment` (phases, cut at the tasks' boundaries), `phase_tasks.name_tasks` (labels,
  from the agent's own words or the names in its commands, and the headline) and
  `phase_summary.summarize` (category, uses, summary: the work weighed to the last phase's own calls,
  `own_t1`, and a call's time past its phase to its own use, `ran_on`). Cached per row (`_phase_cache`, 4,096 blocks)
  and made again when the row's calls, records or pending entries change, so on each read of a
  session still at work. `running_now` reads the tree's newest census against its baseline, the
  least each command had over the session's ticks.
- **Cost:** per record, a tool call is masked and its names read once, as it is logged
  (`TrajectoryReducer._call`; `phase_names.tagset` took about 200 µs a call on an earlier sample,
  `phase_names.py`). Per block, measured at the 20,000-call cap on a shared host (2026-11-03;
  `phase_segment.py`, `phase_tasks.py`): `segment` 0.06-0.10 s, and 1.66-1.84 s with half the calls
  hung and an idle gap after each other one; `task_spans` 0.06-0.59 s; `name_tasks` 0.04-0.07 s. A
  block for every session of a week on eight login nodes takes minutes (README), which is why the
  plates count no category.

#### SE-4 inspector: facts; purpose ribbon and tool tokens

- **Shows:** at the head of the inspector, the row's first and last record, its length, its tool calls
  where the agent's commands are read, processes, CPU, distinct tools, pieces (keys folded and how the
  least certain was attributed: said only where there is more than one, or the least certain was placed
  by a weaker rule than the agent itself or an ancestor), sandbox and approval. At the head of its
  Processes part, its purpose ribbon and tool tokens, each run opening the tree at its first record.
- **Computation:** SE-2's row (`tool_rle` cut to 60 runs, `purpose_rle` to 80, with their start
  times). The inspector's own copy is made under the lock by `_inspect`, whose `lookup` folds only
  the keys sharing the row's prefix when the window's fold is stale; the agent's other days come
  from the window's last fold as it stands (`TrajectoryReducer._agent_days(fresh=False)`), not
  from a fold of every session made again.

#### SE-5 inspector: what was read, and its limits

- **Shows:** the records read and their span, bytes re-read from how many day-files and how long,
  parents with no exit in the read, whether the tree came from the cache; whether the read stopped at
  its record or byte budget, with a button to continue from there; processes counted under their
  parent, entries past the row budget, parents placed on weaker evidence than their own `ppid`.
- **Computation:** `_inspect`'s `scan` (`bytes`, `parsed`, `skipped`, `method`, `stopped`,
  `stopped_at`), `bounds` and `links`.

#### SE-6 inspector: headline, category, summary

- **Shows:** the project and the names that recur across the tasks; what kind of session it was; the
  session in one to three sentences built from the block's own numbers.
- **Computation:** made with the block (SE-3).

#### SE-7 inspector: navigator

- **Shows:** the session's whole span with the stretch this read covers; process starts per time bin
  stacked by purpose on the read's axis, with the task lane above and the phases tinted behind; the
  agent's housekeeping as ticks; failures as ticks; quick jumps to the busiest minute, the first tool
  call, the longest process and each failure.
- **Records:** every record the read matched, drawn or only counted: start (`_ts_epoch` less
  `duration_s`), `_purpose`, `_role`, `duration_s`, `exit_code`, `signal`.
- **Computation:** `TreeBuilder._note` notes every matched record before any budget applies;
  `TreeBuilder.nav` bins the starts at the finest width of `BIN_WIDTHS` that keeps the read within
  2,400 bins, and finds the busiest clock minute, the first tool call, the longest process other than
  the root, and the failures (2,000 listed, the rest counted). Tasks and phases are the row's block.
- **Cost:** part of the inspector's read.

#### SE-8 inspector: task list, or phase list

- **Shows:** each task, opened into its phases, each phase into the commands that ran most in it
  (`top_commands`, 3); under a closed task, its phases in order on one line, with the chip of each
  and the length of any that holds a quarter of the task (`sequenceParts`); lone stretches and
  cutting gaps as lines of their own. A block with no task lists its phases alone.
- **Computation:** the block's `tasks` and `list` (SE-3).

#### SE-9 inspector: process tree

- **Shows:** every process the read itemised, under its parent: start, duration, own and subtree CPU,
  disk and network bytes, exit or signal, a bar on the read's time axis; consecutive siblings of one
  shape folded into counted groups; weaker parent links tagged; processes counted rather than drawn
  as `⋯` rows. Find, "hide routine" and "hide typos" work in the page. The pop-out draws the same
  read on a canvas of its own.
- **Records:** `exit`, `truncated` and `residency` whose `_sid` is one of the row's keys: `pid`,
  `ppid`, `_command`, `args` (masked, then cut to 220 characters), `cwd`, `_ts_epoch`, `duration_s`,
  `cpu_s`, `io`, `net_tx_bytes`, `net_rx_bytes`, `exit_code`, `signal`, `_role`, `depth`, `_kv`;
  parents with no record from the window's process index and the cascades' nodes.
- **Computation:** INSPECT. `TreeBuilder.feed` lets each process claim the children waiting for it
  (a parent exits after its children), takes its own disk and syscall bytes (its `OwnIo`, over the
  records the read takes; a `truncated` one, running at a collector stop, less what its children
  that exited carried so far), so a subtree's bytes add each process's own once, itemises up to
  `inspect_hold` processes and
  `inspect_per_parent` childless ones of one command under one parent, and counts the rest under
  their parent (newest first, it keeps the newest instead); `resolve` attaches what is left by the
  index, the collector's depth or the fold, and draws parents with no record (at most 2,000);
  `render` folds runs of siblings (motifs of up to 4) and serves `inspect_nodes` entries breadth
  first; `_typos` marks what a person mistyped at a terminal.
- **Cost:** INSPECT's: one read at a time, up to 150,000 records or 6 GiB. Building a record costs
  about 44 µs on FAS, in the `inspect_workers` processes, and feeding it to the tree about 6 µs, in
  the service's own: a 150,000-record read 2.5-2.7 s. Cached 600 s.

#### SE-10 inspector: map of the whole session

- **Shows:** under the session's gist, before any process is read, the tasks as a lane of blocks over
  the phases as a band, on the session's own clock: from its first record, or its first phase where
  that begins earlier, to its last phase's end, and on to now for a session running now, where its
  live edge ends it. A task is numbered and labelled as the list under it has it, where that fits; a
  phase is tinted as the navigator tints it, with a rule of its full colour along its foot, and named
  where its name fits; a guess is hatched and idle is a dashed outline. A click picks the task or the
  phase as its row in the list does.
- **Computation:** none of its own: the row's block (SE-3) on the axis the row's band is drawn on
  (`bandAxis`), so it covers the whole session whatever the read reached, which SE-7 cannot.
- **Cost:** none on the service.

## Login nodes › Resources

The Resources sub-tab's own panels, then the I/O panels it shares with Risks' code
(`tabs/RiskIoTab.tsx:RiskIoTab`, `part="io"`).

| | figure | path | payload | code (service · page) |
|---|---|---|---|---|
| RE-1 | per-process distributions | BUILD | `resources.by_class[c].quants`, `top`, `oncpu_set_aside` | `reducers/resources.py:ResourceReducer.feed`, `result`; `reducers/__init__.py:Quant` · `tabs/ResourcesTab.tsx:DistTable` |
| RE-2 | faults, delays, and D-state | BUILD | `resources.by_class[c].faults`, `delays`, `dstate`; `impossible_waits` | `ResourceReducer.feed`; `reducers/__init__.py:Acc` · `tabs/ResourcesTab.tsx:FaultTable` |
| RE-3 | worst individual processes | BUILD | `resources.worst_stall`, `worst_fault`, `worst_wait` | `ResourceReducer._keep_worst` · `tabs/ResourcesTab.tsx:ResourcesTab`, `tabs/RiskIoTab.tsx:InstanceTable` |
| RE-4 | disk read by tool | BUILD; a bar opens IOCMDS | `io_process.by_tool` | `reducers/io.py:IoReducer.feed`, `io.py:_heaviest` · `tabs/RiskIoTab.tsx:toolBars`, `charts/primitives.tsx:HBars` |
| RE-5 | disk written by tool | BUILD; a bar opens IOCMDS | `io_process.by_tool` | as RE-4 |
| RE-6 | network bytes, and per class | BUILD; a cell opens IOCMDS | `io_process.by_class[c]`: `net_tx_mb`, `net_rx_mb`, `net_calls` | `IoReducer.feed` · `tabs/RiskIoTab.tsx:RiskIoTab`, `charts/primitives.tsx:GroupBars` |
| RE-7 | network endpoints | BUILD, named on read | `io_process.endpoints` | `IoReducer._feed_endpoints`, `_endpoint_rows`; `endpoints.py:serve`, `fold` · `tabs/NetEndpoints.tsx:NetEndpoints` |
| RE-8 | I/O by user | BUILD; a cell opens IOCMDS | `io_process.by_user` | `IoReducer.feed`, `io.py:_heaviest` · `tabs/RiskIoTab.tsx:RiskIoTab` |
| RE-9 | I/O distributions | BUILD | `io_process.by_class[c].quants` | `IoReducer.feed`; `Quant` · `tabs/RiskIoTab.tsx:RiskIoTab`, `components/Coverage.tsx:QuantCells` |

#### RE-1 per-process distributions

- **Shows:** per class, the mean, min, p1, p10, median, p90, p99 and max of five measures of a
  process: CPU time, lifetime, the share of its lifetime on a CPU, runqueue wait, peak memory; the
  max names its process on hover.
- **Records:** `exit`: `cpu_s`, `duration_s`, `sched_run_s`, `sched_wait_s`,
  `peak_rss_mb` (else `rss_mb`), and the identity fields of the process behind each max
  (`reducers/__init__.py:who`).
- **Computation:** each value goes into a `Quant`, a DDSketch of every value with 1% relative error
  (logarithmic buckets; two merge by adding counts, so a merged window equals one pass); `n`, `min`,
  `max` and `mean` exact; a null counted for coverage; a field exactly 0 everywhere flagged
  `all_zero` and drawn as not measured. The on-CPU share is `sched_run_s / duration_s`; a process
  whose run time passes its lifetime by more than 5% is set aside and counted. `top` keeps the
  process behind each max.
- **Cost:** five sketch adds a record, bucketed 4,096 values at a time; per read, the buckets sorted
  once per measure. The sketch's size grows with the values' range, not their number.

#### RE-2 faults, delays, and D-state

- **Shows:** per class, the average per process and the total of: minor and major page faults
  (major ones in reaped children too), voluntary and involuntary context switches, block-I/O,
  swap-in and memory-reclaim delays, and D-state (processes that stalled, time, stalls, the longest),
  each with its coverage; values longer than the process lived are left out and counted.
- **Records:** `exit`: `min_flt`, `maj_flt`, `cmaj_flt`, `nvcsw`, `nivcsw`,
  `blkio_wait_s`, `swapin_wait_s`, `freepages_wait_s`, `dstate_wait_s`, `dstate_episodes`,
  `dstate_max_s`, `dstate_src`, and `_impossible` (the normalizer's list of waits it voided).
- **Computation:** an `Acc` (sum, present count, records seen) per field and class; the page divides.

#### RE-3 worst individual processes

- **Shows:** up to 12 processes each with the longest D-state wait, the most major faults and the
  longest runqueue wait, grouped by user.
- **Computation:** `_keep_worst` appends a row when the value is at least the list's floor; at 240
  rows the list is sorted and cut to 24, and the floor raised; `result` serves 12. Merging re-cuts
  by the same rule.
- **Cost:** a row only for a value above the floor, so few records make one.

#### RE-4 disk read by tool, RE-5 disk written by tool

- **Shows:** the 14 tools that read the most from (wrote the most to) disk, as bars; the bytes
  requested and the cache hit on hover. Over every class: the class chips do not narrow it.
- **Records:** `exit`: `_eff` or `_command`, `io.rd_mb`, `io.rchar_mb`, `io.wr_mb`,
  `io.wchar_mb`, and `pid`, `ppid`, `host`, `io.scope` for the join.
- **Computation:** `IoReducer.feed` sums each tool's four byte counters, each process's own (its
  `OwnIo` takes back what a parent's figure holds of the children it reaped), its processes and
  those with an `io` block; `result` serves the union of the top `reducers.io.top_n` (20) tools on
  each counter
  (`_heaviest`, up to 80 rows); the page ranks by disk bytes and draws 14. A bar opens IOCMDS for the
  tool, read from the JSONL unless the page is filtered by user.
- **Cost:** a few adds a record; the tool map is unbounded; per read four `nlargest` passes.

#### RE-6 network bytes, and per class

- **Shows:** per class, bytes sent and received, and the network calls, with coverage; a cell opens
  IOCMDS.
- **Records:** `exit`: `net_tx_bytes`, `net_rx_bytes`, `net_calls`, each null when the
  collector's netbytes block was dropped.
- **Computation:** an `Acc` per class and counter.

#### RE-7 network endpoints

- **Shows:** the top 10 remote endpoints by send and receive calls, or by bytes, for the classes
  shown, each row split by class; a name by reverse DNS, else by a command line, else the address;
  the share of the bars' bytes whose endpoint is known
  ([Network endpoints](../../dashboard/README.md#network-endpoints)).
- **Records:** `exit`: `net_endpoints` (schema 7): per endpoint `addr`, `inbound`,
  `calls`, `tx_bytes`, `rx_bytes`, `proto`, `port`, `host`, `external`, `provider`; and its
  `unattributed`, `overflow`, `unaccounted` losses.
- **Computation:** `IoReducer._feed_endpoints` adds each address's calls and bytes per class; every
  address is kept. On every `/api/panels` read, `endpoints.serve` names each address (lookups run on
  background threads and are kept in `<cache.dir>/rdns.json`), groups the addresses of one name, and
  keeps the union of the top 10 by calls and by bytes for every mix of the present classes (`fold`);
  the page ranks the mix it shows.
- **Cost:** a dict update per endpoint a record; per `/api/panels` read, a pass over every address
  for every class mix (up to 15). While names are pending the page reads `/api/panels` again after
  3, 6, 12, 24 and 48 s. Not measured.

#### RE-8 I/O by user

- **Shows:** per user, with the class of their newest record: processes, disk and requested bytes
  read and written, network received and sent, I/O coverage; a cell opens IOCMDS for the user, a
  lookup the replicas can answer.
- **Computation:** `IoReducer.feed` per user, each process's own bytes (RE-4); `result` serves the
  union of the top 20 users on each of the four I/O counters. The class chips keep a user by that
  one class.

#### RE-9 I/O distributions

- **Shows:** per class, quantiles of each process's own bytes read requested, read from disk,
  written requested and written to disk (RE-4's join).
- **Computation:** a `Quant` per counter and class (RE-1's sketch); a record with no `io` block is
  counted as not measured.

## Login nodes › Risks

See [Risk rules differ by cluster](../../dashboard/README.md#risk-rules-differ-by-cluster). A
section the cluster's profile leaves out is neither computed nor drawn.

| | figure | path | payload | code (service · page) |
|---|---|---|---|---|
| RI-1 | a plate per section | BUILD | `risk.sections`, `login_compute.by_class`, `dangerous.rows`, `secrets.rows` | `reducers/risk.py:RiskReducer.result` · `tabs/RiskIoTab.tsx:RiskIoTab` |
| RI-2 | compute on a login node: bars, per tool, heaviest processes | BUILD | `risk.login_compute` | `RiskReducer.feed`, `result` · `tabs/RiskIoTab.tsx:RiskIoTab`, `InstanceTable` |
| RI-3 | commands that should not run here | BUILD | `risk.dangerous` | `RiskReducer.feed`, `_matches`, `result` · `tabs/RiskIoTab.tsx:RiskIoTab`, `WhoChips`, `InstanceTable` |
| RI-4 | credentials on a command line | BUILD | `risk.secrets` | `RiskReducer.feed`, `_matches`, `result` · `tabs/RiskIoTab.tsx:RiskIoTab` |
| RI-5 | rules in effect | BUILD (config only) | `risk.rules` | `risk.py:ruleset`, `RuleSet.summary` · `tabs/RiskIoTab.tsx:RulesInEffect` |

#### RI-1 a plate per section

- **Shows:** for the compute section, the agents' CPU-hours in builds and interpreters, the other
  classes beside, agent users and all classes' share of the CPU; for a commands section, the runs
  matching its rules by class and the two commands agents ran most; for credentials, the matches by
  class and the command lines scanned.
- **Computation:** RI-2 to RI-4's payloads, summed per class over the section's rules in the page.

#### RI-2 compute on a login node

- **Shows:** CPU in the tools of the compute kinds (build, interpreter) and of the transfer kind
  (`net/data`), by tool, with processes, users and hosts; per class CPU-hours; the 24 heaviest
  processes.
- **Records:** `exit`: `_eff` or `_command` (its kind), `cpu_s`, `user`, `host`, `_a3`.
- **Computation:** `RiskReducer.feed` adds each exit whose kind is a compute or transfer kind
  (`risk.login_compute`) to its tool; a compute exit also joins a heaviest list (cut to 200 at 400)
  and its class's CPU; `result` ranks the tools by CPU (25 rows) and gives the compute CPU's share of
  all CPU.
- **Cost:** a kind lookup and a few adds a record.

#### RI-3 commands that should not run here

- **Shows:** per rule: the command, severity, runs, users, who ran it by agent type, the share of
  runs with an exit code that exited 0, CPU; a row opens its first 12 runs.
- **Computation:** per matching rule (LV-6's match): runs, CPU, users, hosts, classes, (class, agent
  type) counts, the first 12 runs, and the runs with an exit code and those that exited 0; rows by
  severity, users, runs.
- **Cost:** as LV-6.

#### RI-4 credentials on a command line

- **Shows:** per pattern: severity, matches, users, tools, who ran it; a row opens its first 12
  matches with pid, user and tool and never the command line; the command lines scanned.
- **Computation:** the credential patterns run on `args` alone, case-sensitive, and the matched text
  is never kept; a match keeps its identity without argv.

#### RI-5 rules in effect

- **Shows:** every rule, its resolved pattern, severity, source (built in, changed, added) and
  section; those switched off, in no section, invalid or unknown.
- **Computation:** `RuleSet.summary`, built once per distinct `risk` configuration (`ruleset`). No
  records.

## Login nodes › Slurm

See [Slurm: how sessions use the scheduler](../../dashboard/README.md#slurm-how-sessions-use-the-scheduler).
Gone when `slurm.enabled` is false. Requests are read off argv: a `#SBATCH` line is not seen.

| | figure | path | payload | code (service · page) |
|---|---|---|---|---|
| SL-1 | plates: agent jobs submitted, queue checks per job, agent sessions polling, submissions refused | BUILD | `slurm.by_class`, `slurm.rejections` | `reducers/slurm.py:SlurmReducer.result` · `tabs/SlurmTab.tsx:SlurmTab` |
| SL-2 | sessions at the scheduler | BUILD | `slurm.sessions`, `sessions_total` | `SlurmReducer.feed`, `_submit`, `_sessions`, `_polls`, `result` · `tabs/SlurmTab.tsx:Sessions`, `SessionDetail` |
| SL-3 | how each class uses the scheduler | BUILD | `slurm.by_class`; `tool_mix.by_class[c].rows` | `SlurmReducer.result` · `tabs/SlurmTab.tsx:ByClass` |
| SL-4 | submissions the scheduler refused | BUILD | `slurm.rejections` | `SlurmReducer._submit`, `slurm.py:reason` · `tabs/SlurmTab.tsx:SlurmTab` |
| SL-5 | latest submissions | LIVE | `submits` | `reducers/live.py:LiveReducer._submit` · `tabs/SlurmTab.tsx:Latest` |

#### SL-1 plates

- **Shows:** jobs agents submitted (agent sessions, the busiest minute), agent queue checks per job,
  agent sessions that polled the queue (hours polling), agent submissions refused (share, the
  commonest reason); the other classes beside each.
- **Computation:** `slurm.by_class` (SL-3) and the agent's first refusal reason (SL-4).

#### SL-2 sessions at the scheduler

- **Shows:** per agent session -- its collector keys folded into the agent -- or per person or IDE
  and user: jobs, refusals, most in one minute, distinct commands with numbers masked, sbatch flags,
  queue checks, checks per job, its longest polling stretch; opened, what came back, the tools,
  queue checks by command, polls, partitions, cancels, `srun`/`salloc` runs, the share with the
  request on the command line, GPUs and GPU-hours, refusal reasons, the latest command.
- **Records:** `submit`: `tool`, `args`, `job_id` or `job_ids`, `exit_code`, `captured_tail`,
  `req_src`, `partition`, `time_limit_s`, `mem`, `gpus`, `array`, `user`, `_a3`, `agent_type`,
  `_kt`, `_kv`, `tree_root_pid`, `host`, `_ts_epoch`; `exit` of the query commands,
  `scancel`, `srun` and `salloc` (`_command`, `_ts_epoch`, `exit_code`, `signal`).
- **Computation:** `SlurmReducer.feed` and `_submit` per key (an agent's (host, agent pid, tree
  root); anyone else's (host, class, user)): the outcome (`outcome`: job, dry run, step, refused, no
  id), flags by regex, whether the request was on the command line, the command's shape (a blake2b
  of `args` with digits masked), submissions per minute, partitions, GPUs times array tasks,
  GPU-hours where GPUs and a time limit were both on the command line, and each query's time. At
  read, `_sessions` folds the agent keys into their owner through the process index, and `_polls`
  joins a session's queries within 2 s into a poll, polls within 3 minutes into a stretch, and
  calls a stretch of 10 minutes or more polling. Sessions sorted by queries plus 10 times jobs and
  refusals plus runs; 200 served.
- **Cost:** a few counters a record; bounds of 50,000 keys (the rest counted), and per key 50,000
  query times and 2,000 shapes. Per read, a sort of each session's query times.

#### SL-3 how each class uses the scheduler

- **Shows:** per class: jobs, sessions and users, queue checks and checks per job, sessions polling
  and the hours of it, the share of the class's processes that are scheduler commands, the share of
  submissions with each flag and with the request on the command line, dry runs, refusals and their
  share, the busiest minute, `srun`/`salloc` runs and failures, cancels, GPU-hours, and the time
  limit asked (median, p90).
- **Computation:** `result`'s per-class sums over the sessions; the time limit is a `Quant` per
  class; the scheduler share is the tool mix's `slurm-submit` and `slurm-query` kinds over the class's
  processes (AG-8), computed in the page.

#### SL-4 submissions the scheduler refused

- **Shows:** per class, tool and reason: how many times, and one example (user, time, command on
  hover).
- **Computation:** a refused submission (no job id, a non-zero exit, and a scheduler error line in
  `captured_tail`) is counted by (class, tool, reason), the reason being that line with numbers
  masked and cut to 160 characters (`reason`); the 20 commonest served.

#### SL-5 latest submissions

- **Shows:** the newest submissions in the window: when, user, class or agent type, the job id or what
  came back (refused with its reason, dry run, step), the partition, time and GPUs asked (or "in the
  script"), the command.
- **Records:** `submit`, as SL-2.
- **Computation:** `LiveReducer._submit` keeps the 50 newest (`live.submit_tail`), each judged by
  `slurm.outcome` and masked; `result` cuts them to the window.

## Ask LLM

| | figure | path | payload | code (service · page) |
|---|---|---|---|---|
| AL-1 | suggestions for the operator (cards) | ASK over BUILD and LIVE | `/api/ask/{job}` `suggestions` | `aggregator.py:Aggregator.ask`; `ask.py:snapshot`, `parse_suggestions`, `Asker.submit` · `tabs/ChatTab.tsx:SuggestionList` |
| AL-2 | marked done (list) | none: the service's own file | `/api/suggestions/done` | `ask.py:Asker.done_list`, `mark_done` · `tabs/ChatTab.tsx:ChatTab` |
| AL-3 | ask about the whole window (answers with their sources) | ASK over BUILD and LIVE | `/api/ask/{job}` `answer`, citations | `Aggregator.ask`; `ask.py:snapshot`, `Asker` · `tabs/ChatTab.tsx:ChatTab`, `components/Ask.tsx`, `components/Cite.tsx:Sources` |

#### AL-1 suggestions for the operator

- **Shows:** cards, the most urgent first: what to do, the numbers behind it with their sources, and
  links to the accounts named; run only when the button is pressed.
- **Records:** none read for it: the window's payloads.
- **Computation:** `Aggregator.ask` with `context.suggest`: the `/api/panels` payload for the page's
  window and filter (refused while the window is still being read), the live payload's `now`,
  `users`, `hosts` and `window`, the trajectories' summary and the 15 sessions with the most
  processes, the cascades with a session hint on each agent, who the risk rows name (`risk_who`) and
  the suggestions marked done; compacted to 120 KB by cutting the largest section's lists and
  strings a step at a time (`STEPS`), command lines dropped unless `ask.include_commands`, every
  object given a citation ref; one fixed question. The answer is JSON, checked and cut to 8
  suggestions (`parse_suggestions`).
- **Cost:** one run of the agent CLI, at most 240 s, one at a time. Not measured.

#### AL-2 marked done

- **Shows:** the suggestions anyone marked done, kept for everyone until undone and left out of the
  next pass.
- **Computation:** `Asker.mark_done` copies the suggestion from its own job into
  `<cache.dir>/ask/done.json` (200 kept). No records.

#### AL-3 ask about the whole window

- **Shows:** the answer to a question about the window on screen, its figures linked to the rows they
  came from; the last turns of the conversation go with each question.
- **Computation:** as AL-1, without the fixed question and with the page's filter left out unless the
  question is about one panel. A panel's own "ask" button, on any tab, sends only that panel's keys
  (`Aggregator._ask_panel`); the inspector's sends a digest of the session's process tree, read by
  INSPECT (`Aggregator._ask_session`, `ask.py:session_digest`).
- **Cost:** as AL-1.

## Search & export

Hidden on `develop` (`App.tsx`, `HIDDEN_TABS`); its code is kept and `/api/events` and `/api/export`
are served.

| | figure | path | payload | code (service · page) |
|---|---|---|---|---|
| EX-1 | records, filtered (table) | EVENTS | `/api/events` `rows`, `next_cursor`, `total_scanned`, `event_filter_defaulted` | `export.py:scan`, `make_predicate`; `app.py:api_events` · `components/EventsExplorer.tsx:EventsExplorer`, `tabs/LiveTab.tsx:EventTable` |
| EX-2 | download | EVENTS | `/api/export`: JSONL with a `_meta` header and a `_trailer` | `export.py:scan`, `meta_header`; `app.py:api_export` · `components/EventsExplorer.tsx:EventsExplorer` |

#### EX-1 records, filtered

- **Shows:** the records matching the filters, newest first, 200 a page (5,000 at most), grouped by
  user (class, commands, CPU, failed, latest) and opened into rows.
- **Records:** any kind; `event` defaults to `exit`, and the response says it did. The fields the
  filters name: time, `_a3`, `agent_type`, `user`, `host`, `_eff`, `_purpose`, bucket,
  `session_key`, `_sandbox`, `_approval`, `depth`, `cpu_s`, `duration_s`, `exit_code`, `signal`,
  and `q`, a substring of the masked command line.
- **Computation:** EVENTS. With names anonymized, `q` is matched against the aliased text, which
  costs a rewrite of each candidate (`app.py:search`).
- **Cost:** per page, bounded by the matches it needs and the files' time ranges; a deep page walks
  the earlier matches again. Measured: EVENTS above.

#### EX-2 download

- **Shows:** the same records as a JSONL file: the collector's fields plus the derived `_` keys,
  credentials masked.
- **Computation:** the same predicate, every match streamed, 500 lines a chunk, up to
  `export.max_rows` (5,000,000); the trailer gives the row count and whether the cap cut it.
- **Cost:** a read of every planned file's range, unbounded but by the cap.

## Internal

Panels not settled yet: the sandbox ones. See
[Conventions the panels obey](../../dashboard/README.md#conventions-the-panels-obey) for what
`sandboxed` and `approval` mean and why a session is judged by its tool calls.

| | figure | path | payload | code (service · page) |
|---|---|---|---|---|
| IN-1 | plates: agent tool calls sandboxed, sandboxes failed to start, sessions launched with a bypass flag | BUILD | `sandbox.tool_calls`, `attempts`, `approval_by_class_sessions` | `reducers/sandbox.py:SandboxReducer.result` · `tabs/SandboxTab.tsx:SandboxTab` |
| IN-2 | tool calls: did they run sandboxed? (outcomes; sessions by user) | BUILD | `sandbox.tool_calls`, `sessions`, `sessions_total` | `SandboxReducer._calls`, `_sessions`, `result`; `sandbox.py:session_state` · `tabs/SandboxTab.tsx:AxisBlock`, `SessionTable` |
| IN-3 | sandboxed, per process and per session | BUILD | `sandbox.by_class_events`, `by_class_sessions` | `SandboxReducer.feed`, `result` · `tabs/SandboxTab.tsx:AxisBlock` |
| IN-4 | approval at launch, per session and per process; launch flags as written | BUILD | `sandbox.approval_by_class_sessions`, `approval_by_class_events`, `launch_modes` | `SandboxReducer.feed`, `result` · `tabs/SandboxTab.tsx:AxisBlock`, `SandboxTab` |
| IN-5 | sandbox wrapper calls, by outcome | BUILD | `sandbox.attempts` | `SandboxReducer._sessions` · `tabs/SandboxTab.tsx:SandboxTab` |
| IN-6 | processes by sandbox wrapper, kind and sandbox state | BUILD | `sandbox.ancestry_matrix` | `SandboxReducer.feed`, `result`; `sandbox.py:_reading` · `tabs/SandboxTab.tsx:SandboxTab` |
| IN-7 | when each process was checked | BUILD | `sandbox.sandbox_src` | `SandboxReducer.feed` · `tabs/SandboxTab.tsx:SandboxTab` |

Records for the whole tab: `exit` (each counted once): `_sandbox` (four states), `_sandbox_src`,
`sandbox_detail`, `_sandbox_ancestry`, `_approval`, `_command` (a wrapper is `bwrap` or
`codex-linux-san…`), `_role`, `pid`, `ppid`, `host`, `_ts_epoch`, `duration_s`, `exit_code`,
`signal`, `dstate_wait_s`, `is_agent`, `agent_pid`, `tree_root_pid`, `agent_type`, `user`; and
each agent root's own record of any kind, `truncated`, `residency`, `conn` and `netio` among
them, for its launch (`_approval`, `_approval_detail`).

#### IN-1 plates

- **Shows:** the share of agent tool calls that ran sandboxed, of those whose outcome is known
  (isolated, not isolated, failed); agent sandboxes that failed to start, of the wrapper calls; the
  share of agent sessions launched with a bypass flag, of those whose launch flags are readable.
- **Computation:** IN-2's, IN-5's and IN-4's counts, divided in the page over the known states.

#### IN-2 tool calls: did they run sandboxed?

- **Shows:** agent tool calls by outcome, by class; the sessions that made tool calls, grouped by
  user, with approval, calls, the share sandboxed and failed starts (40 served, the busiest first);
  those that made none, folded.
- **Computation:** `SandboxReducer._calls` reads an agent's exits bottom up: a child reports to its
  parent's slot (`below[(host, ppid)]`: whether a payload ran, was isolated, was unknown); a wrapper
  or a Claude Code Bash tool shell becomes a tool call with its outcome (isolated, not isolated,
  failed -- the wrapper exited non-zero with no payload --, no payload, unknown), and a wrapper
  absorbs the calls below it, so nested wrappers fold into the outermost. At read, `_sessions`
  groups the calls not absorbed by the session that owns them (through the process index) beside
  every agent root, and `session_state` judges each session by its calls.
- **Cost:** a few counters a record, and for an agent's exit a slot (at most 200,000 parents waiting,
  the overflow counted) and a call per tool call, kept for the window with no cap. `_sessions` walks
  every call each time the payload is made, and the cascades (AG-2) and the Users tab
  (`portraits.inputs`) walk them again (`agent_calls`). The slots and their calls are carried from
  hour to hour when merging.

#### IN-3 sandboxed, per process and per session

- **Shows:** per class, the processes in each of the four namespace states, and the sessions in each
  verdict (sandboxed, partly, failed, not sandboxed, unknown, no calls). As in IN-2 and IN-4, a state
  the axis can judge is a share of the class's judged ones, and the rest (exited before it was checked,
  unknown, no calls, not an agent session, can't tell) are counts, with the coverage under the class
  (`AxisBlock`, `lib/sandbox.ts:shareOfKnown`): the share a plate above prints.
- **Computation:** a counter per class of each counted record's `_sandbox`; the session verdicts of
  IN-2.

#### IN-4 approval at launch

- **Shows:** per class, sessions and processes by approval at launch; the launch flags as written,
  per agent type and reading, with sessions.
- **Computation:** each root's verdict is its own record's (`roots`, the newest kept); sessions are
  IN-2's; per process, a counter of each counted record's `_approval`; `launch_modes` counts the
  rooted sessions by (agent type, approval, flags).

#### IN-5 sandbox wrapper calls, by outcome

- **Shows:** per wrapper (`bwrap`, `codex-linux-sandbox`), agent type and outcome, the calls.
- **Computation:** `_sessions` counts the wrapper calls not absorbed by another, by those three.

#### IN-6 processes by sandbox wrapper, kind and sandbox state

- **Shows:** per (wrapper above it, wrapper or process, sandbox state): processes, their D-state wait
  and its coverage, and a reading of what the row means.
- **Computation:** a counter per key with summed `dstate_wait_s` and a present count; `_reading`
  words each row.

#### IN-7 when each process was checked

- **Shows:** each process's namespace answer by when it was read: at its start, in a census, copied
  from its parent, or after it exited.
- **Computation:** a counter of `_sandbox_src`.

## Feeds

See [Is each collector OK?](../../dashboard/README.md#is-each-collector-ok). This tab has no window
and no filter.

| | figure | path | payload | code (service · page) |
|---|---|---|---|---|
| FD-1 | collectors: one row per login node, opened into its runs | LIVE (health), FEEDS (files) | `/api/collectors` `hosts`, `tiers` | `health.py:CollectorHealth.feed`, `report`; `aggregator.py:Aggregator.collectors` · `tabs/FeedsTab.tsx:Collectors`, `HostDetail`, `Runs` |
| FD-2 | feeds, and the history store | FEEDS | `/api/feeds` | `feeds.py:resolve`, `resolve_dated`, `resolve_globbed`, `resolve_file`; `Aggregator.refresh_feeds`; `history.py:HistoryStore.status` · `tabs/FeedsTab.tsx:Sources`, `Where`, `History` |
| FD-3 | startup read | LIVE | `backfill` | `Aggregator.backfill_now` · `tabs/FeedsTab.tsx:Backfill` |
| FD-4 | configuration | CONFIG | `/api/config` | `config.py:Config.as_dict` · `tabs/FeedsTab.tsx:FeedsTab` |

#### FD-1 collectors

- **Shows:** per login node: a verdict (OK, degraded, stopped, silent, no heartbeat), the last
  heartbeat's age, running since, restarts, events lost in the last finished run, reasons and kernel
  limits; opened, every run (started, stopped, ran for, why, records, events lost, cut short, pid and
  kernel), the probes loaded and not, the sysctls, the files of each tier, the heartbeats read.
- **Records:** `meta`, `stop`, `residency_totals`: `collector_pid`, `_ts_epoch`, `interval_s`;
  `meta`'s `kernel`, `bcc`, `features`, `features_rejected`, `sysctl`, `euid`, `schema_version`;
  `stop`'s `reason`, `uptime_s`, `records`, `truncated`, `perf_lost`, `perf_seen`. Also the FEEDS
  report's file list (size, mtime, host) of the eBPF and node tiers.
- **Computation:** `CollectorHealth.feed`, in the live dispatch, so over the backfill's span and the
  tail, keeps per host up to 20 runs (by collector pid) and its newest heartbeat; `report` judges each
  host against `feeds.ebpf.max_age_s` (300 s) and a loss of 0.1% of kernel events
  (`LOST_WARN_PCT`), worst first.
- **Cost:** O(1) per `meta`, `stop` or heartbeat; per read, hosts times runs. Read again every 20 s
  while shown.

#### FD-2 feeds, and the history store

- **Shows:** every feed -- eBPF, node tier, `sacct`, `sacctmgr`, domains CSV, user CSV --: status,
  newest record and its age against the feed's limit, where it was read from and where else it was
  looked for, files, bytes, days, hosts; for the eBPF tier, the history store's replicas and their
  size, the conversion's progress, and how many selective reads the replicas answered
  ([History in Parquet](../../dashboard/README.md#history-in-parquet)).
- **Records:** for the eBPF and node tiers, the newest file's last record; for the others, none, only
  their files' names, sizes and times.
- **Computation:** `resolve`: the dated tiers glob `feeds.<feed>.days` dated directories (8 for the
  eBPF tier, 2 for the node tier) and read the newest file's last record (`newest_record_ts`);
  `sacct` and `sacctmgr` glob their patterns; the CSVs stat their file. `refresh_feeds` adds the eBPF
  tier's records counted, schema versions, exclusions, backfill, tail counters, synthetic hosts and
  the store's status.
- **Cost:** per read a glob, a stat per file and a tail read per dated feed (SH-4).

#### FD-3 startup read

- **Shows:** the backfill's state -- placing itself in each file, reading with a percentage and the
  records so far, done (records, bytes, time, how far back the history reaches), skipped or failed --
  and the lines that did not parse.
- **Computation:** `Aggregator.backfill`, updated a batch at a time; `retained_from` is the oldest
  live bin.

#### FD-4 configuration

- **Shows:** every resolved setting, its value and where it came from (flag, environment, file,
  default); the password hash hidden.
- **Computation:** `config.py:Config.as_dict(with_origins=True)`. No records.

## Served, not drawn

What the service computes and serves that no figure on `develop` draws. Each still costs its
reducer's work, and some reach Ask LLM's snapshot or a panel's basis note.

- **`panels.node`** (NODE, `reducers/resources.py:NodeReducer.result`): per host the load, memory,
  CPU idle and iowait, process states, responsiveness probes, logged-in users and open files; per
  user the cgroup CPU used between the two newest snapshots, its quota, throttling, memory and pids;
  NFS mounts and disk space. Made every rebuild tick and served in every `/api/panels`. The login
  nodes table's `load₁` and `CPU` columns (LV-23) read the live tier's host rows instead, where they
  are always null.
- **`workspace`**: the workspace reducer's own payload is its buffer counters; the per-agent views
  the page draws are in `cascades.workspace` (AG-4).
- **`cascades.edges`, `edge_via`, `role_census`**: who starts whom, how each parent was found, and
  every classifier-tagged process counted by agent type and role.
- **`trajectories`' `drift`** (cwd drift and the purpose mix's Jensen-Shannon divergence, per row),
  `dominant_user_share_pct`, `by_key_type`, `by_class_counts`, `fold` and `buffer`.
- **`sandbox.agent_only` and `sandbox_detail`**: only in IN-6's basis note.
- **The I/O plates and the "Requested bytes against disk bytes" panel** of `RiskIoTab`: the
  Resources tab passes `plates={false}` and `series={false}`, so they are not drawn; their numbers
  are LV-8's rows and LV-16's charts.

## What is slow

The expensive operations, the most expensive first, by what the repo measured and, where it did not,
by the work the code does.

1. **Reading a window from the day-files.** A scan, or the first reduction of a window's hours,
   parses every line of the window but `meta` and `stop` (`windows.EVENTS`), at about 25 µs a record
   through json and the normalizer, and feeds each to about eleven reducers. Measured on one login
   node: ~15 s for 24 h and ~160 s for 7 d scanned (README); FAS's eight nodes write 21.6 M records a
   day. Merged windows pay it once per hour ever, but these builds still read the window: the first
   fill of the hours, the 1 h view (always scanned), every view filtered by user or agent type
   (always scanned, though the byte check and the replicas skip what the filter rejects), and the
   drift re-builds of those last two. Every BUILD figure waits on it; the page shows the saved
   panels meanwhile.
2. **Re-reading a window per click or query.** IOCMDS (SH-11, and every I/O number) reads the
   window's bytes for each new lookup: about a minute per day of the FAS capture, from the JSONL
   unless the lookup names a user, a host, a directory or agent pids. EVENTS reads the planned
   files' ranges from the JSONL unless the filter names a column a replica can test, inflating each
   `.gz` whole to index it: one user's exits over a week took 222-444 s from the `.gz` and
   13.6-15.4 s from replicas; an export streams every match in its range, up to 5,000,000 rows.
3. **The backfill.** Placing the read in every file inflates every sealed `.gz` to index it: 4
   minutes for a week of 8 login nodes; reading is 35-45 s per node-week. Window builds wait for the
   placing to finish, and the inflater indexes are rebuilt on every restart.
4. **The session inspector's read** (SE-5, SE-7, SE-9): up to 150,000 records (600,000 newest first)
   or 6 GiB per open, one at a time, so a second reader waits. Its records are built in 8
   processes: on FAS a 150,000-record read takes 2.5-2.7 s, and a newest-first one of as many
   5.3-5.5 s, most of it the tree keeping the newest. A sealed `.gz` with no replica, and every
   sealed day of a terminal's or a login scope's session, is still searched in the service's
   process: 30-43 s for a session in FAS's busiest sealed day-file, against 2.8 s from its replica
   in one process.
5. **Folding and ranking the sessions on read** (SE-1 to SE-3, LV-5, US-*, AG-2). The fold of every
   held key, "half a second to most of one" at 24 h on rtx6000a, runs under the aggregator's lock,
   which the tail and every reader share, whenever a record arrived since the last fold; the
   Sessions list asks for it every 5 s. A running session's phase block is made again on each read:
   `segment` up to 1.66-1.84 s for one session at the 20,000-call cap in its worst shape. The
   cascades (each rebuild tick) and the Users roster (every 5-60 s) read the same fold, and make it
   again when a record arrived since.
6. **The Users tab's composition** (US-*): a roster walks every session's call log, and its inputs
   walk every tool call of the window; a portrait makes up to 80 phase blocks. About 0.3 s a roster
   on 500 synthetic sessions with 100,000 calls; not measured on a real feed.
7. **Memory, and feeding every view.** Every live record is reduced once per held view (5 by
   default, plus filtered ones). The trajectories hold ~6 KB per collector key, up to 150,000 keys,
   ~950 MB a view; a held 7 d window of a busy node is up to ~1 GB. Unbounded within the window: the
   sandbox's tool calls, the tool mix's and I/O's per-tool and per-user maps, the resources'
   CPU-to-run ratios, the endpoints' addresses, the live tier's submissions.
8. **Making and serving the panels.** Every held view's payload is made again each rebuild tick, a
   reducer per lock hold. Each `/api/panels` read copies the payload and names and ranks the
   endpoints for every class mix (RE-7); a panels payload of 1.8 MB held the event loop for a quarter
   of a second before serializing moved to the request's thread (`app.py`). Not measured since.
9. **The live frame** (LV-1 to LV-3, LV-7, LV-19 to LV-23): one per distinct window per poll that
   brought records, cheap by design -- a bin's rows are cached until a record lands in it, and the
   roster sums closed bins once -- but it holds the aggregator's lock for its payload.
