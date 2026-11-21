# What each figure computes

This page lists every chart, table, plate, pill, ranking and list that the dashboard page draws,
and the operation on the data that produces each one: which records it reads, by which read path,
which reducer folds them, which method renders its payload, what that costs, and where the code
is. It is written for the dashboard's maintainers and describes `develop` at `0a9eb37`
(2026-11-17).

The machinery this page refers to (the live tier, window builds and their saved copies, the
hourly merge-reduce, and the history store of Parquet replicas) is explained in
[`dashboard/README.md`](../../dashboard/README.md). This page links to it rather than repeating
it. What each tab is for is in [`dashboard/web/README.md`](../../dashboard/web/README.md#tabs).

How to read this page:

- **Code pointers** are `file:function`. Service code is under `dashboard/rc_dashboard/` and page
  code is under `dashboard/web/src/`. The prefixes are left off.
- **Figures** are numbered by tab, in page order (`LV-7` is the Live tab's seventh figure). Each
  tab has a table listing the figure, its read path, the payload it draws and its code. A note per
  figure follows, covering what it shows, the records and fields it reads, the computation, and
  the cost.
- **Cost** is given per record (what one record costs a reducer as it is fed) and per read (what
  it costs to make the figure's payload), along with the bounds that apply. A bound is the default
  in `config.py:defaults` or in the module constant named. A number is one that the repo's docs or
  code comments measured, and the note says where. "Not measured" means there is no such number.
- **Records** are the eBPF tier's unless a note says otherwise. On every path, a record is parsed
  (`fastjson.loads`) and normalized (`normalize.py:Normalizer.normalize`) before a reducer sees
  it. The normalizer derives the keys the reducers read: `_a3` (the class), `_command`, `_eff`
  (the effective command), `_purpose`, `_sid`, `_kt` and `_kv` (the session key), `_role`,
  `_sandbox`, `_approval`, `_surface`, `_ts_epoch`. Parsing and normalizing cost about 25 µs per
  record (`proctree.py`, `DEFAULTS`).

## Where the records come from

- **The eBPF tier** (`feeds.ebpf`): `exit`, `truncated`, `residency`, `residency_totals`, `conn`,
  `netio`, `submit`, `meta`, `stop`. Every figure except those on the Feeds tab reads only this
  tier.
- **The node tier** (`feeds.ebpf_node`): one snapshot of a host per line. It is read and reduced
  (path NODE) and served as `panels.node`, but no figure draws it
  ([Served, not drawn](#served-not-drawn)).
- **Slurm**: no figure reads the scheduler's accounting feeds (`feeds.sacct`, `feeds.sacctmgr`),
  and nothing the service runs imports `sacct.py`. The Slurm figures read the eBPF tier instead:
  its `submit` records (one per `sbatch`, `srun` or `salloc`) and the `exit` records of the
  scheduler commands run on the login nodes (`squeue`, `sacct`, `scontrol`, `scancel`, `srun`, ...).
- **The domains and user CSVs** (`feeds.domains`, `feeds.user_actor`): these are resolved and
  listed on the Feeds tab (FD-2), but no figure reads them.

On FAS RC's eight login nodes on 2026-11-15, the eBPF tier wrote 21.6 M records: 17.1 M `exit`,
2.1 M `conn`, 1.7 M `netio`, 0.66 M `residency`, 18 k `submit` and 11.5 k `residency_totals`
([docs/feed-parquet](../feed-parquet/README.md#what-the-fas-feed-looks-like)). When this page
says "a big feed", it means that scale.

## Read paths

| path | what reads the records | when | sealed days from the Parquet replicas |
|---|---|---|---|
| LIVE | the live tier, `reducers/live.py:LiveReducer`, and the collectors' health, `health.py:CollectorHealth`, fed by the tail and the startup backfill | the tail every `ingest.poll_ms` (1 s); the backfill once per start | no |
| BUILD | a window view's reducers, `windows.py:PanelView`: built once per window and filter, then fed live | a build on first request, at start for the default and prewarm windows, on drift; its payload made again every rebuild tick | merge: no. scan: only the time search that places the window's start. filtered: yes |
| INSPECT | the session inspector's re-read of one session, `proctree.py:inspect` | on opening a session | yes for a session keyed on pids; no for a terminal's or a login scope's |
| EVENTS | the events scan, `export.py:scan`, behind `/api/events` and `/api/export` | per request | yes when the filter names `user`, `host`, `agent_type`, `exit_code`, `signal` or `session_key` |
| IOCMDS | the lookup of the commands behind a clicked I/O number, `iocmds.py:IoCommands` | per click, as a polled job | yes when the click names a user, a host, a directory or agent pids |
| NODE | the node tier's newest snapshots, `aggregator.py:Aggregator.read_node_tier` | every rebuild tick, and at start | no: the node tier is never sealed |
| FEEDS | the feed reports, `feeds.py:resolve` | per `/api/feeds` request, and every 30 s for the `feeds` frame | no |
| CONFIG | the resolved configuration, `/api/config` | once per page load | no records read |
| ASK | an agent CLI run over a snapshot of BUILD and LIVE payloads, `ask.py` | per question | through BUILD |

### LIVE: the live tier

See [The time window](../../dashboard/README.md#the-time-window) and
[Ingestion, precisely](../../dashboard/README.md#ingestion-precisely). One `LiveReducer`, sized at
`live.retention_min` (7 d), holds:

- per-minute bins by class (`buckets.py:RollingBuckets`). Any window is an exact slice of these
  bins, and 7 d holds 10,080 of them;
- a per-account roster (`roster.py:UserRoster`): 10-minute bins for the last day and hourly bins
  beyond that. Each bin is a flat list per account of (class and agent type, processes, CPU,
  bytes);
- the newest `residency_totals` of each host, the newest `residency` tick of each agent or editor
  tree, and the agent session roots;
- bounded tails: 500 exits (`live.event_tail`), 50 submissions (`live.submit_tail`) and 100 exits
  per account (`live.user_tail`), plus every submission in retention (`submits_in_window`).

Every second, the tail reads what the collectors appended (`Aggregator.poll_once`). On every
start, the backfill reads `min(live.backfill_hours, retention)` of history, newest first
(`Aggregator.backfill_now`). The tail's records also go to each held view (BUILD). The backfill's
records do not, because a view reads its own window from the files. `CollectorHealth` is fed from
both, in the same dispatch as the live tier. The live tier is served by `/api/live`, by the
websocket's `live` frame, and by `/api/live/users` and `/api/live/user`. There is one `live`
frame per distinct window in use; it is made after each poll that brought records and reused for
a second (`Aggregator.live_frame`). Without a socket, the page polls `/api/live` every 15 s
([Live transport](../../dashboard/web/README.md#live-transport)). The page's user or agent-type
filter does not narrow anything here (`windows.py:ViewFilter.NOT_NARROWED`).

Replicas: no. The backfill finds its start in each file with a time search of the `.gz` and reads
the JSONL. The tail reads today's plain files.

Measured: a week of one login node (4 GB, 2.4 M records) backfills in 35-45 s (README).
Positioning a week of 8 login nodes took 4 minutes on rtx6000b, because it inflates each sealed
day's `.gz` to build its seek index (`aggregator.py:Aggregator.backfill_now`). With the backfill
holding the lock for 256 records at a time, `/api/live` stayed at p90 ~10 ms while a build ran
(`aggregator.py`, `BACKFILL_BATCH`).

### BUILD: a window view

See [The time window](../../dashboard/README.md#the-time-window) and
[Hourly merge-reduce](../../dashboard/README.md#hourly-merge-reduce). A view is one set of the
historical reducers for one window and one page filter (`windows.py:PanelView.fresh_reducers`).
Views are kept in an LRU of `panels.max_windows` (5) and built one at a time on one worker thread
(`WindowRegistry._build`). The default window (24 h) and the prewarm windows (1 h, 6 h, 12 h;
`panels.prewarm_min`) are built at start, once the backfill has found its place in every file.
After it is built, a view is fed every live record, so each live record is reduced once per held
view.

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

Their union (`windows.EVENTS`) is every record kind except `meta` and `stop`, so a build parses
nearly every line it reads. The byte check on the event kind (`export.py:event_filter`) skips
little.

Two rules hold for every sum below. First, a process is counted once, at its `exit`. A
`truncated` record is a process that was still running when a collector stopped, carrying its
whole life's counters so far. Its exit after the restart carries all of them again, so a
`truncated` record may name a running root but never adds to a sum. Second, disk and syscall
bytes are each process's own. The collector's `io` is `task->signal->ioac`, which the kernel also
credits with every child the process reaped. So each reducer that sums bytes holds a join
(`reducers/own_io.py:OwnIo`) that subtracts the children's figures at the parent's exit. The join
is carried across hours, as the sessions' join always was, and holds at most
`reducers.own_io.max_parents` parents. A dropped parent is counted (`own_io.evicted`) and reported
on stderr. Parents held for an agent runtime, or for whatever started one, are dropped last,
unless such parents make up more than half the cap.

A view is built one of three ways:

- **merge**: an unfiltered view with a closed hour inside it (`hours.py:build`). The records from
  the window's start to its first whole hour are read. Every closed hour is merged from its saved
  state, and a whole epoch day is merged from one saved state (`HourStore.load_part`, each
  reducer's `merge`). The join state that the last hour ended with is installed, and the open
  hour is read. An hour that is not saved yet is reduced first, in order (`HourStore.ensure`), and
  this happens only once ever. Replicas: no. The edge, the hours and the tail read the JSONL and
  search the `.gz` by time. In a run of hours, a reader of a sealed file is handed the span that
  another reader of the same file inflated last. These spans are kept for every file an hour
  reads, up to 64 files (`gz.shared_spans`, `hours.shared_files`), so an hour's search, its read
  and the next hour's search do not each inflate the file again. When there is a worker for every
  unit of the node split, each unit's hours run in one worker (`hours.UnitPool`). That worker
  keeps at most 4 such spans for one hour and indexes only its own node's files.
- **scan**: an unfiltered view with no closed hour inside it, or any view with
  `panels.merge_reduce` off (`WindowRegistry._build`, `export.py:window_plan`, `window_scan`). It
  reads each day-file's in-window byte range and k-way merges them, oldest first. The 1 h view is
  always scanned, because an hour closes `panels.hour_grace_s` (300 s) after it ends, so a
  60-minute window never contains a closed hour. A scan resumes from a saved build when one fits
  (`snapshots.py`, `WindowRegistry._resume`), and then reads only what was written since.
  Replicas: only for the time search that finds the window's start in a sealed file
  (`export.py:_place`). The read itself is JSONL.
- **filtered**: a view narrowed by the page's user or agent-type filter. It is always scanned,
  never merged, and it is saved and resumed like a scan. Replicas: yes. The filter is a `where`
  (`ViewFilter.where`, `where.py`) that a sealed file's replica tests on its columns before it
  builds a record. In a JSONL file, a byte check (`ViewFilter.line_match`) skips the lines the
  filter rejects before they are parsed.

A view's payload (each reducer's `result()`, with credentials masked) is made after its build and
again on every rebuild tick (`rebuild.interval_s`, 900 s; `WindowRegistry.refresh`). If a view's
drift has passed `panels.max_drift_pct` (25%) of its window, the rebuild tick builds the view
again instead: by a merge where the view can be merged, otherwise by a scan, and never by
resuming a saved build. So most BUILD figures change every 15 minutes, not with each record. The
page re-reads `/api/panels` on the `rebuilt` frame and when a build finishes. Two payloads are
composed on read instead. `trajectories` is made again at most every
`reducers.trajectories.live_s` (5 s) if the view has ingested records since
(`Aggregator.trajectories`, `PanelView.materialize_key`). The Users tab is built from the view's
reducers (`portraits.py`) and held for up to 60 s (`Aggregator._held`). Both run under the
aggregator's lock, which the tail and every reader share. While a view builds, it shows the
panels that the last view of its window saved (`payloads.py`).

Measured on one login node's feed (README): merged, a 24 h window builds in ~2-3 s and a 7 d
window in ~8 s, compared with ~15 s and ~160 s scanned. Filling the hours the first time costs
about as much as one scan. Resumed from a saved build, a 24 h window takes ~0.2 s instead of ~7 s,
and a 7 d window ~1.4 s instead of ~42 s. A held 7 d window of a busy node takes up to ~1 GB,
mostly the trajectories' session keys (~6 KB each, ~950 MB at the 150,000-key cap). A week of
saved hours takes ~38 MB on disk, and a saved build ~12 MB per day of window.

### INSPECT: the session inspector's re-read

See [Sessions: tasks, phases, and processes](../../dashboard/README.md#sessions-tasks-phases-and-processes).
`/api/trajectory` (`proctree.py:serve`, `inspect`, `_inspect`) re-reads one row's records from the
day-files of the row's host, starting at the session's first record, for exactly the collector
keys that the row folds. It uses the unfiltered view of the window, whatever the page's filter is.
A session keyed on agent pids is found by searching the bytes for its keys (`key_scan`, over a
memory map, 32 MB at a time). In a sealed file with a replica, it is found by testing the
replica's `session_key` and `agent_pid` columns (`key_where`). A terminal's or a login scope's key
is searched for in the inflated `.gz`.

The records a search finds are built and normalized in `inspect_workers` worker processes (8, but
no more than the number of CPUs the service may use; `keysearch.py:stream`). Each worker handles
one part of a day-file: a replica's row groups, or two of a plain file's 32 MB chunks. Each record
comes back holding only the 28 fields the tree reads (`keysearch.py:INSPECT_KEYS`). The service's
process replays the parts in file order, using the same counters as a single-process read, so
every bound below stops at the same record. It then feeds the records to the tree. A read that
stops early cancels the parts that are still outstanding. A sealed `.gz` with no usable replica
is still searched in the service's process, as before. That covers every sealed `.gz` for a
terminal's or a login scope's key. The row shown beside the tree (SE-4) takes the agent's other
days from the window's last fold, so opening a session folds only the keys that share its row's
prefix. See the inspector under [API](../../dashboard/README.md#api), "How a read is made".

Bounds, each reported in the response: `inspect_max_records` 150,000 (600,000 newest first),
`inspect_max_bytes` 6 GiB, `inspect_hold` 20,000 processes itemised, `inspect_per_parent` 2,000
childless processes of one command per parent, and `inspect_nodes` 6,000 entries served. One read
runs at a time. A built tree is served again for 600 s (16 trees, 96 MB in all), keyed on the
view's install time. While someone has opened a session in the last 30 minutes and no window is
building, a background thread keeps the top 3 sessions of the `recent` ranking read. It gives way
to a reader (`_warm_loop`).

Measured: searching for the keys read one 11,000-line Claude session from a 627 MB day-file in
0.8 s, compared with 2.9 s line by line (`proctree.py:key_scan`). In FAS's busiest sealed day-file
(17.4 GB inflated), a session's key search took 30-43 s from the `.gz` and 2.8 s from its replica
([History in Parquet](../../dashboard/README.md#history-in-parquet)). Building the records in 8
processes brought a 150,000-record read of a FAS agent session (boslogin06, 2026-11-16, with its
fold stale, as on a live window) from 9.9 s down to 2.5-2.7 s. A newest-first read of 149,573
records (boslogin05) went from 12.1 s to 5.3-5.5 s, and most of that time is now spent by the
tree keeping the newest records.

### EVENTS: the events scan

`export.py:scan` plans every day-file under the roots and reads each one from its near time bound
(newest first means reading backwards from `to`). It checks the event kind on the bytes, then
parses, normalizes and tests each record (`make_predicate`), and k-way merges the files' streams
on time. Paging is by offset, so a deep page walks through the earlier matches again. The first
time a `.gz` with no replica is read, it is inflated whole so it can be indexed. Replicas: when
the filter names one of the record's own fields (`where_of`: `user`, `host`, `agent_type`,
`exit_code`, `signal`, `session_key`). Measured: finding one user's exits on one sealed day, with
the week's 128 day-files planned, took 222-444 s from the `.gz` and 13.6-15.4 s from replicas
([History in Parquet](../../dashboard/README.md#history-in-parquet)).

### IOCMDS: the commands behind an I/O number

`iocmds.py:IoCommands`: a click on an I/O number (`components/IoCmds.tsx:IoNum`) starts a job that
reads the window's `exit` records again (`export.py:window_plan`, then `_window_stream` per file,
one file after another). The job works out each process's own bytes by taking off what it reaped
(`OwnIo`, over every record the read parses), keeps the records that pass the click's filters
(the filters of `/api/events`, plus agent pids, a host and a directory), and groups them by
command line. Two jobs can read at once. Six finished jobs are kept, and each answers the same
question again for 120 s on a live feed, or indefinitely on a pinned clock. A click carries its
figure's filters. Where the figure names no filters of its own, the click carries the page's user
and agent-type filter instead (`components/IoCmds.tsx:params`). Replicas: when the lookup names a
user, a host, a directory or agent pids (`prefilter_where`). A lookup narrowed only by class,
tool, kind of tool, agent type or time reads the JSONL. Measured (`iocmds.py`): about 3 s for a
day of rtx6000a and about a minute for a day of the FAS RC capture (6.2 GB of gzip, 2.8 M exits).

### NODE, FEEDS, CONFIG and ASK

- **NODE**: `Aggregator.read_node_tier` reads the last `feeds.ebpf_node.snapshots_per_host` (2)
  lines of each node-tier file, without streaming the rest (`aggregator.py:last_lines`), into
  `reducers/resources.py:NodeReducer`, on every rebuild tick and at start.
- **FEEDS**: `feeds.py:resolve` globs each feed's paths and reads the newest file's last record
  (`newest_record_ts`); `Aggregator.refresh_feeds` adds the eBPF tier's counters, the backfill's
  state and the history store's (`history.py:HistoryStore.status`). `/api/collectors` adds the
  health the live tier folded (`CollectorHealth.report`).
- **CONFIG**: `/api/config` serves the resolved configuration with each key's origin.
- **ASK** (off unless `ask.enabled` is set, and refused while the service has no password; a
  refused question reads nothing): `Aggregator.ask` builds a snapshot from the window's
  `/api/panels` payload, the live payload's `now`, `users`, `hosts` and `window`, and the ranked
  sessions. Alternatively, the snapshot is one panel's keys, or one session's process tree read
  by INSPECT. It compacts the snapshot to `ask.context_kb` (120 KB). To do that,
  `ask.py:snapshot` cuts the largest section's lists and strings a step at a time and drops bulky
  keys such as series and tails (`DROP_KEYS`). It then runs the configured agent CLI (`codex exec`
  by default) as the service's user, `ask.max_concurrent` (1) at a time, for at most
  `ask.timeout_s` (240 s). The CLI runs with a short environment (`ask.AGENT_ENV`) in its own
  directory under the system's temporary directory.

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
- **Records:** none of its own. It uses each required feed's status and newest record (FEEDS), the
  live frame's `clock`, and the transport (`api/useLive.ts:useLive`).
- **Computation:** `headline` takes the worst status among the required feeds. A feed whose
  `lag_s` exceeds its `max_age_s` counts as `stale`. `pillOf` turns this into words, putting a
  problem with the data ahead of a problem with the transport.
- **Cost:** a FEEDS report.

#### SH-2 `anonymized` pill

- **Shows:** that names leave the service as aliases
  ([Anonymized names](../../dashboard/README.md#anonymized-names)).
- **Records, computation, cost:** none; `/api/config` says `anonymize.enabled`.

#### SH-3 `fake data` pill and note

- **Shows:** that some host's records were written by the demo, and which hosts.
- **Records:** `meta`, `stop` and `residency_totals` carrying `"synthetic": true`.
- **Computation:** `CollectorHealth.feed` marks each host with a record that carries the key, and
  `Aggregator.refresh_feeds` lists those hosts.
- **Cost:** O(1) per record, in the live dispatch.

#### SH-4 newest record's age

- **Shows:** how old the newest record of the freshest required feed is.
- **Records:** the last record of the newest file of each feed (`ts`).
- **Computation:** `newest_record_ts` reads the newest file's tail, extending the read until it
  holds a whole line. `lag_s` is the current clock minus that record's time. The page shows the
  smallest `lag_s` among the required feeds.
- **Cost:** one tail read per feed per report. On a capture, the newest file is a `.gz`, so the
  read needs the file's inflater index, which is built once per process.

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
- **Computation:** `presets` gives the configured presets that retention can serve. `resolve`
  gives the clamp and its note. `PanelView.status` gives the state, the progress (by bytes, or by
  hour-reads when merging), the drift and the cache. These are sent as `/api/windows`, and as a
  `windows` frame when a material field changes (checked every 2 s, `app.py:windows_loop`).
  `retained_minutes` comes from the oldest live bin (`RollingBuckets.oldest_epoch`), and the
  backfill's progress from the live frame's `backfill`.
- **Cost:** a status per held view per frame.

#### SH-8 user and agent-type choices

- **Shows:** the 50 users with the most exits in the window and every agent type, each with its
  class mix, to narrow the page by.
- **Records:** `exit`: `user`, `agent_type`, `_a3`.
- **Computation:** `FacetReducer.feed` counts exits per user and per agent type, by class, and
  `result` ranks them. The choices are served only from the window's unfiltered view
  (`Aggregator.facets`) and never get a build of their own, so they stay in place while a filtered
  view builds.
- **Cost:** two dict updates per record; the user map holds every user; one sort per read.

#### SH-9 the rail's foot

- **Shows:** whether updates stream or are polled, when the last arrived, the window and the
  retention held, when the panels were built and whether from a saved build (`· restored`), and the
  page's own build stamp.
- **Computation:** `full_built_at` is the view's `built_at`, or the node tier's if the view has
  none. `restored` means the view's `cache.source` is `snapshot`.

#### SH-10 classes shown

- **Shows:** how many of the three class chips are on. This comes from the URL's state, not from
  any data.

#### SH-11 the commands behind an I/O number

- **Shows:** every command line behind the clicked number, each listed once, with its processes,
  disk bytes read and written, bytes requested through `read()` and `write()`, bytes sent and
  received, CPU, users and time. Rows are sorted by the quantity clicked, 200 rows per page (up to
  1,000), with totals and how far the read got.
- **Records:** `exit`: `args` (else `_command`), `_eff`, `io.rd_mb`, `io.wr_mb`,
  `io.rchar_mb`, `io.wchar_mb` (each process's own: `pid`, `ppid`, `host`, `io.scope` for the
  join), `net_tx_bytes`, `net_rx_bytes`, `cpu_s`, `user`, `_ts_epoch`, and
  the fields the click filters on (`_a3`, `agent_type`, `_eff` or its bucket, `session_key` and
  `agent_pid`, `host`, `cwd`).
- **Computation:** IOCMDS. Each record gets a byte check (the event kind, then user, agent pids,
  directory, host), a parse and `iocmds.py:predicate`, and is then added to its command line's
  group (up to 1,000,000 groups; past that, only the totals are kept). For a blast-radius click,
  the agent pids are widened to every tool-call key that the window's process index walks up to
  them (`agent_keys`). The page polls every 600 ms, and `Job.page` sorts the groups for each page.
- **Cost:** one read of the window's bytes per lookup, however much it matches (IOCMDS above).

## Live

The figures at the top of the tab, and the tables that refer to now, come from the live tier. The
plates and charts that refer to the window come from the window's reducers.

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
  (`RollingBuckets.add`). `result` reads the minute before the current one
  (`RollingBuckets.last_complete_bin`). A minute with no records is null for every class. In a
  minute that has records, a class with none is 0. The page sums the classes.
- **Cost:** one dict increment per record, and one bin per frame. A record stamped more than
  `live.clock_skew_s` (120 s) in the future is dropped and counted.

#### LV-2 sessions · last 10 min

- **Shows:** by class, the agent and IDE sessions whose process tree sent a `residency` tick in
  the last 10 minutes, and the human sessions with an exit in that time. Below that, it shows the
  processes running (summed from the hosts' newest ticks) and the live agent sessions launched
  with a bypass flag and in auto mode.
- **Records:** `residency` (`tree_root_pid` or `agent_pid`, `host`, `_a3`, `_ts_epoch`);
  `residency_totals` (`_by_class`, the collector's `by_actor3` under the dashboard's classes:
  `n_procs`); a human `exit` (`host`, `_kt`, `_kv`); the agent roots, any record of a process whose
  role is `agent` with `pid == agent_pid` (`_approval`).
- **Computation:** `_residency` keeps the newest tick per (host, tree root), `_totals` keeps the
  newest totals per host, `_exit` keeps the newest human exit per login scope, terminal or
  account, and `_root` keeps each root's newest record. `result` counts the trees that ticked
  within `LiveReducer.LIVE_S` (600 s), by the tick's class. It adds the human sessions whose
  newest exit is that recent, sums the hosts' per-class `n_procs`, and counts the roots seen
  within 600 s by approval. The plate's memory clause expects `now.rss_gb` to be a single number,
  but the service sends a per-class map, so the memory clause is never shown.
- **Cost:** O(1) per record, and one pass per frame over the held trees, hosts and roots. The
  window does not cut it, because it is a snapshot of now.

#### LV-3 active users

- **Shows:** the distinct accounts that ran something in the window or have a session running now,
  per class, and how many have an agent running.
- **Records:** `exit`: `user`, `_a3`, `agent_type`, `cpu_s`, `io.rd_mb`, `io.wr_mb` (each
  process's own, via `LiveReducer._own_io`; the tail subtracts a parent's reaped children at the
  parent's exit, while the backfill, reading newest first, meets the parent first and subtracts
  each child from it as the child arrives), `net_tx_bytes`, `net_rx_bytes`, `_ts_epoch`; and
  `residency` for what is running now.
- **Computation:** `UserRoster.add` puts each exit in its account's slot of a 10-minute bin (an
  hourly bin if it is more than a day old). `UserRoster.totals` sums the window's closed bins and
  then adds the open bin. The closed-bin sum is cached per window and made again when a bin
  closes, when the window's edge crosses a bin, or at most every 15 s for a late record.
  `LiveReducer.users` counts the accounts over the totals and the live trees (`_running`). A
  window's edge falls on a bin boundary: 10 minutes inside the last day, an hour beyond it.
- **Cost:** per record, a walk of the account's slot; per frame, one pass over the accounts.
  Measured: the roster's first design held 640 MB and took up to 600 ms per frame at 2,000 users
  (`roster.py`). The current design has not been measured.

#### LV-4 agent processes exited

- **Shows:** the agent processes that exited in the window, the other classes beside them, the
  agent share of all processes, and the agent types on hover. The share is out of every class
  counted, including `unlabeled`, whatever the class chips show (`lib/shares.ts`).
- **Records:** `exit`: `_a3`, `agent_type`, `_eff`, `_command`, `_sid`, `pid`, `ppid`,
  `_shell_resolved`, `cpu_s`.
- **Computation:** `ToolMixReducer.feed` counts each exit under its class, and also counts an
  agent's exit under its agent type. The exception is the shell-wrapper dedup: a shell is not
  counted if its effective command was resolved from its argv and its captured direct child, in
  the same session, resolved to the same tool. The dedup uses a `(session, parent pid)` index of
  the children's tools. A child exits before the shell that waited on it, so one pass in time
  order is exact. `_rows` totals each class's `calls`, and `_by_type` splits a class by agent type.
- **Cost:** a few counter adds and one index lookup per record. The index holds
  `reducers.tool_mix.dedup_sessions_max` (200,000) parents, dropping the oldest first, and is
  carried from hour to hour when merging. Per read, sums over the 12 kinds of tool.

#### LV-5 agent sessions

- **Shows:** the window's agent sessions (leaving out one-off probes), the other classes beside
  them, and how many agent sessions were launched with a bypass flag, how many in auto mode, and
  how many ran tool calls none of which was sandboxed.
- **Records:** for sessions, `exit` (`_sid`, `_kt`, `_kv`, `_a3`, `_ts_epoch`) and each root's own
  record (role `agent`, `pid == agent_pid`). For the launch line, the same as IN-2 and IN-4.
- **Computation:** `TrajectoryReducer.summary` counts each folded session whose last record is in
  the window. For agents, it also requires that the session's root was seen (`rooted`). An
  agent-class key with no root is a probe and is counted separately. The launch line comes from
  `SandboxReducer.result`: sessions by approval at launch and by the verdict of their tool calls
  (`session_state`).
- **Cost:** the summary walks every session of the window each time `trajectories` is made (SE-2).
  The sandbox reducer walks every tool call of the window each time its payload is made (IN-2).

#### LV-6 risky agent actions

- **Shows:** the processes whose command line matched a dangerous-command rule plus the credentials
  found on command lines, by class; under it each count, and the share of agent tool calls that ran
  sandboxed and how many sandboxes failed to start.
- **Records:** `exit`: `_eff` or `_command`, `args`, `_a3`, `user`, `host`,
  `agent_type`, `cpu_s`, `exit_code`, `signal`.
- **Computation:** `RiskReducer.feed` tests `"<tool> <args>"` against every rule that a section
  shows (`_matches`: a literal gate first, then the regex, with the verdict cached per tool and
  argv). The plate sums the rule rows' `classes` over the commands and credentials sections. The
  sandbox line is the same as IN-1.
- **Cost:** one cache lookup per record and, on a miss, one regex search per rule whose gate
  passes. The cache holds 50,000 command lines and is cleared when full. Before the cache existed,
  matching took half of a window build: 2.5 M regex searches for a 6 h window (`risk.py`).

#### LV-7 processes exited per minute

- **Shows:** the processes that exited each minute of the window, a line per class; a minute with no
  record is a hole in the line.
- **Records:** `exit`: `_a3`, `_ts_epoch`.
- **Computation:** as LV-1. `RollingBuckets.series` gives a row per minute of the window, oldest
  first, sliced from the same bins whatever the window. A row is made again only if a record has
  landed in its bin since it was last made (`_rows`, keyed on each bin's version).
- **Cost:** per frame, one pass over the window's bins (10,080 at 7 d), most of them cached rows.
  The page builds its points in one pass.

#### LV-8 who uses what

- **Shows:** each class's share of the window in eleven units side by side: processes, CPU time,
  disk read, disk written, network sent, network received, peak memory and sessions on the login
  nodes, and jobs, GPUs and GPU-hours asked of Slurm.
- **Records:** those of the payloads it reads: `exit` (processes, CPU; LV-4), `exit`
  `io` (each process's own) and network bytes (`IoReducer`), `residency_totals` (memory; LV-10),
  sessions (LV-5), `submit` (`gpus`, `array`, `time_limit_s`; SL-3).
- **Computation:** none of its own. `OverviewTab` reads `tool_mix.by_class[c]` `calls` and `cpu_s`;
  `io_process.by_class[c]` `rd_mb`, `wr_mb` and the totals of the `net_tx_mb`/`net_rx_mb`
  accumulators; the highest bin of `resources.series.resident[c].rss_mb` (with the largest process
  on hover, from `resources.by_class[c].top`); `trajectories.summary[c].sessions`; and
  `slurm.by_class[c]` `jobs`, `gpus`, `gpu_h`. A cell is the class's share of its row, out of
  every class that the row's panel counted (including `unlabeled`), not just the classes shown,
  so a share does not change with the class chips. If a counted class has no reading for the row
  (null, as with the network bytes of a class where none of the processes carried them), the row
  gets no share. It shows a dash and says why (`lib/shares.ts`). The processes and CPU rows come
  from the tool mix, after the shell-wrapper dedup. A byte cell opens IOCMDS for its class.
- **Cost:** the cost of its reducers.

#### LV-9 CPU seconds per minute

- **Shows:** CPU seconds per minute by class over the window, summed by exit time: a process's
  whole-life CPU lands in the bin it exited in.
- **Records:** `exit`: `_a3`, `_ts_epoch`, `cpu_s`.
- **Computation:** `ResourceReducer._series_add` counts the record in its bin by class and adds
  each of its ten series fields (CPU, runqueue wait, D-state wait, major faults, four `io` byte
  counts, network sent and received). It also counts the records that had each field, so a bin
  where every record lacked the field is null. `series` lays the bins on one axis, with null where
  a class has no record, and the page divides by the bin's length in minutes. The bin width comes
  from `resources.py:bin_for`: the first of 1, 2, 5, 10, 15, 30 minutes, 1, 2, 3, 6, 12, 24 hours
  that keeps the window within 240 bins (per minute for 1 h, per 10 minutes for 24 h, per hour for
  7 d). A merged window folds the hours' 60 s bins into its own width (`ResourceReducer.merge`).
- **Cost:** about ten dict updates per record; per read, one pass over at most 240 bins (960 are
  held, `SERIES_SPAN_CAP`, for a window that drifted or a skewed clock).

#### LV-10 memory, and peak and average

- **Shows:** resident memory by class over the window, sampled while processes ran; a table of each
  class's peak, when, and average.
- **Records:** `residency_totals`: `host`, `_ts_epoch`, `_by_class` (`rss_mb`).
- **Computation:** `ResourceReducer._tick` sums each host's per-class `rss_mb` over its ticks in a
  bin. `series` takes each host's mean over its ticks and sums the hosts. The page finds the peak
  and the mean of the bins. The chart is empty in a view filtered by user, because a tick names no
  user (`ViewFilter.NOT_NARROWED`).
- **Cost:** a few adds per tick; per read, bins times hosts.

#### LV-11 each class's share of the processes exited

- **Shows:** per bin, each class's share of the processes that exited.
- **Records:** as LV-9 (`n`, the records in a bin).
- **Computation:** in the page. A class's count is divided by the count over every class, not only
  the classes shown, so a share does not change with the class chips.

#### LV-12 each class's share of the CPU time

- **Shows:** per bin, each class's share of the CPU seconds; computed as LV-11 from LV-9's series.

#### LV-13 runqueue wait, and run and wait

- **Shows:** seconds spent waiting for a CPU per minute, by class; a table of each class's run time,
  wait time and their ratio.
- **Records:** `exit`: `sched_wait_s`, `sched_run_s`, `cpu_s`.
- **Computation:** the series is made as in LV-9. The table uses an accumulator per class
  (`reducers/__init__.py:Acc`: sum, present count, coverage) of `sched_run_s` and `sched_wait_s`,
  plus `schedstat_ratio`, the median of `cpu_s / sched_run_s` over the processes with more than
  1 s of CPU.
- **Cost:** the ratio keeps one float per such process per class, with no bound, and sorts them at
  each read.

#### LV-14 D-state by tool, and every tool that stalled

- **Shows:** seconds in uninterruptible sleep per minute, a line for each of the six tools that
  stalled longest and one for the rest; a table, titled "every tool that stalled", of the 20 that
  stalled longest (processes, time, stalls, longest).
- **Records:** `exit`: `dstate_wait_s`, `dstate_episodes`, `dstate_max_s`, `_eff` or
  `_command`.
- **Computation:** `_series_add` adds each record's `dstate_wait_s` to its bin by tool. `feed`
  counts each tool's stalled processes, stall time, stalls and longest stall. `result` ranks the
  tools by time (20 in the table, 6 drawn). `series` draws 0 for a tool in a bin where D-state was
  measured, and null where it was not. It covers every class; the class chips do not narrow it.
- **Cost:** one dict add per stalled record, and one sort over the tools per read.

#### LV-15 major page faults

- **Shows:** major page faults per minute by class: `maj_flt` summed by exit time, as LV-9.

#### LV-16 disk read, disk written

- **Shows:** per class and per minute, the bytes that reached the disk (solid) against the bytes
  requested through `read()` or `write()` (dashed). The gap between them is the page cache. A
  point opens the commands of that class in that bin (IOCMDS).
- **Records:** `exit`: `io.rd_mb`, `io.rchar_mb`, `io.wr_mb`, `io.wchar_mb`, each process's own
  (`ResourceReducer`'s `OwnIo`). These are null for another user's process, because the collector
  could not read its `/proc/<pid>/io`.
- **Computation:** as LV-9.

#### LV-17 network

- **Shows:** per class, bytes sent (solid) and received (dashed) per minute by the processes that
  exited; a point opens IOCMDS.
- **Records:** `exit`: `net_tx_bytes`, `net_rx_bytes` (null where the collector kept no
  count).
- **Computation:** as LV-9, in MB.

#### LV-18 Slurm queue checks, Slurm jobs submitted

- **Shows:** per class, the scheduler queries and the jobs submitted per bin. These charts are
  offered only when the window holds Slurm records.
- **Records:** `submit` (an outcome of `job`); `exit` of a scheduler query command
  (`tool_buckets.SLURM_Q`: `squeue`, `sacct`, `scontrol`, `sinfo`, ...).
- **Computation:** `SlurmReducer._bin` counts each job and each query in its bin by class, using
  the same bin width as the resources charts. `_series` makes a bin null (a gap) when no class has
  a Slurm record in it. When another class has records in the bin, a class with none gets 0.
- **Cost:** O(1) per record; per read, one pass over the bins.

#### LV-19 active users: running now

- **Shows:** per agent and IDE type, how many accounts have such a session running now; a chip
  narrows the table to them.
- **Records:** `residency` (`user`, `_a3`, `agent_type`, `_ts_epoch`).
- **Computation:** `LiveReducer._running` groups the trees that ticked within 600 s by account,
  class and type, and `users` counts the accounts per type. Under a page filter, the page counts
  the rows it kept instead.

#### LV-20 active users: one row per account

- **Shows:** a row per account: agents and IDEs running now by type, processes running, processes
  exited in the window as a bar by class, CPU time, disk read and written, network sent and
  received, GPU-hours asked, last active. The columns are the reader's (defaults `ui.user_columns`).
- **Records:** as LV-3, and `submit` (`gpus`, `time_limit_s`, `user`, `_ts_epoch`) for GPU-hours.
- **Computation:** `LiveReducer.users` orders the accounts: those with agents running now first,
  then by most processes, then by name. It makes full rows for the first `live.user_rows` (100).
  A full row (`_row`) holds the roster's (class, agent type) slots decoded, the trees running now,
  their census's `n_procs`, and GPUs times time limit over the window's submissions that named
  both. When the frame does not hold every account, a search, a running-now chip or a page filter
  asks `/api/live/users`, which searches every account. The page applies the user and agent-type
  filter row by row (`pageMatch`). A byte cell opens IOCMDS for the account.
- **Cost:** per frame, a pass over every account and 100 full rows.

#### LV-21 active users: an account opened

- **Shows:** the account's sessions running now (agent or IDE, version, surface, launch badges,
  running time, processes, memory now, peak and average over the session's census ticks, CPU, and
  the agents running inside it with their own launch details); what the account ran in the window
  by class and agent type, with agents started by another agent shown separately; and its last
  10, 25 or 100 exited processes, however long ago, on any host.
- **Records:** as LV-2 and LV-3; the account's own exit tail.
- **Computation:** `/api/live/user` calls `LiveReducer.user_detail`, which returns the account's
  row, its trees that ticked within 600 s with the agent roots inside each (`_inner`,
  `_fronted`), and its own tail (`user_tails`, newest first, 100 deep, masked when served).
  "Started by" is computed in the page from the window's agent trees (`cascades.trees`, BUILD):
  per child agent type, the processes of the agents that a parent agent type started
  (`nestedAgents`).
- **Cost:** per open, a roster read and a pass over the live trees and roots. It is fetched again
  when the requested count or the window changes.

#### LV-22 active users: every process

- **Shows:** the newest exited processes of every user on every host, newest first (the last 10, 25
  or 100), narrowed by the search, a host, the page filter and the class chips.
- **Records:** `exit`: time, host, user, class, agent type, command and argv, purpose,
  depth, pid, duration, CPU, peak memory, disk bytes (its own, as LV-3), network bytes, exit
  code, signal, sandbox,
  approval, session key.
- **Computation:** `LiveReducer._exit` keeps a deque of `live.event_tail` (500) rows, newest first.
  `result` cuts it to the window and masks each row's argv once (`_served`). The page filters and
  sorts the rows.
- **Cost:** one row dict per record, which is not made during the backfill once the tails are
  full. This is a tail, not an aggregate, so a wider window does not make it any deeper.

#### LV-23 login nodes right now

- **Shows:** per login node: sessions, processes running (split by class or by agent type), memory,
  processes in D-state, users, external TCP sockets.
- **Records:** `residency_totals`: `host`, `trees`, `_by_class` (`n_procs`, `threads`, `rss_mb`,
  `d_state`, `users`), `by_agent_type`, `_tcp_by_class` (`open`, `ext`, `estab`); a host with only
  `residency` gets a row that says so.
- **Computation:** `_totals` keeps each host's newest tick, and `result` sums the per-class fields
  per host. `users` adds up the collector's per-class counts, so one person in two classes counts
  twice. `load₁` and `CPU` would come from the node tier, but the node tier is not joined here, so
  they are always null. The page drops any column with no value.
- **Cost:** per frame, one pass over the hosts.

## Users

See [Users: a portrait of each user](../../dashboard/README.md#users-a-portrait-of-each-user).
Everything on the tab is composed on read (BUILD on read) by `portraits.py`, for the page's
filter. It uses the view's `trajectories`, `sandbox` and `slurm` reducers, plus the live tier's
agent roots for what is running now (`Aggregator._running_fn`). While the tab is shown,
`/api/users` and `/api/user` are read again every 15 s.

A session here is one agent run: its rows across several days taken together, keyed by (host,
agent pid, user) (`portraits.agent_sessions`, `trajectories._agent_key`), with a root, and
belonging to its user. Common cost: `Aggregator._held` keeps a view's inputs, its roster, and each
portrait opened. The inputs (`portraits.inputs`) are the sandbox's tool-call verdict per agent
session, which walks every tool call of the window, and `slurm_by_user`, which folds every Slurm
key. `_held` makes them again, under the aggregator's lock, once the view has ingested records
and 5 s have passed, or after 60 s. A roster groups every folded trajectory row by agent and walks
each session's call log. Measured on 500 synthetic sessions with 100,000 calls: a roster takes
about 0.3 s, and a portrait read from the roster's inputs about 20 ms (README).

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

- **Shows:** users with an agent session in the window, and how many have one running now; agent
  sessions, with the median time a session was open and the median number of tool calls; the
  share of sessions launched with a bypass flag, out of those whose launch is known; and the share
  driven from a script, out of those whose surface is known.
- **Records:** the trajectories' agent sessions (`exit`: `_sid`, `_kt`, `_kv`, `user` or `uid`,
  `_ts_epoch`; the call log from `args`), each root's own record (`_approval`, `_surface`), and
  the live tier's roots.
- **Computation:** `roster` builds every user's row (US-3). `_fleet` counts users and sessions,
  works out the medians (`_q`, by sorting), and totals approval and surface. A session's surface
  and approval are taken from its root's own record (`TrajectoryReducer.surface_of`,
  `approval_of`).

#### US-2 trait chips

- **Shows:** each trait held in the window and how many users hold it; a chip narrows the roster.
- **Computation:** `traits` runs per user, and each trait is one rule (`portraits.RULES`) applied
  to the row's numbers. `_fleet` counts them.

#### US-3 roster

- **Shows:** a row per user: sessions, sessions by agent type, where they are driven from, the
  bypass share, tool calls with a bar by category, the typical session's length, the most open at
  once, active minutes by hour and when last active, traits; a line under each on how they leverage
  agents.
- **Records:** as US-1, plus each session's call log (a call's category, start and tag set; only
  Claude Code's and Codex's are read), the sandbox's tool-call verdicts, and the Slurm keys.
- **Computation:** `_user_row` runs per user. It counts sessions by agent type, surface, approval
  and tool-call verdict; counts calls (null when no session's calls are read); counts categories
  over the call logs; counts active minutes (the distinct minutes in which a call began) by local
  hour (`_Clock`); finds `peak_open` with a sweep over the sessions' first and last records
  (`_peak_open`); and finds the 5 most frequent projects (`TrajectoryReducer.project_of`), the
  traits, and `leverage` by `RULES`. Rows are sorted by sessions, then by calls.
- **Cost:** one pass over every session's call log per roster.

#### US-4 figures

- **Shows:** each user drawn from their row, with a part of the figure for each number, and the
  part's numbers and rule on hover.
- **Computation:** in the page, from US-3's row. The only numbers the page adds are drawing caps
  (six robots, five sheets, ten lights).

#### US-5 portrait: head

- **Shows:** the user's figure, how they leverage agents point by point with the window's medians
  beside, their trait badges with each rule, and totals: sessions, tool calls, active minutes,
  processes, CPU, disk read and written, network sent and received, peak memory.
- **Records:** as US-3, for one user. The footprint comes from the trajectory rows (`n`, CPU, each
  process's own disk bytes, network bytes, the largest peak memory).
- **Computation:** `portrait` makes the user's row with a weekday grid (`_user_row(grid=True)`) and
  compares it with the roster's `fleet`. That is the unfiltered view's fleet when that view is
  held, otherwise the filtered view's (`fleet_of`).

#### US-6 portrait: how they launch agents

- **Shows:** four share rows (agent, driven from, approval at launch, tool calls sandboxed). Each
  row shows the user's share of the known values, with the window's share as a tick. Values that
  carry no information are counted underneath.
- **Computation:** the row's counters compared with those of `fleet`, divided in the page over the
  known states.

#### US-7 portrait: rhythm

- **Shows:** the typical and longest session, the most sessions open at once, and active minutes,
  each with the window's median; active minutes by hour of day, and by weekday and hour for a
  window of two days or more; and each session across the window from its first record to its
  last, packed into rows.
- **Computation:** the row's `wall_p50_s`, `wall_max_s`, `peak_open`, `active_min`, `hours` and
  `grid`. The timeline shows the 60 newest sessions (`SESSIONS_SHOWN`), which the page packs into
  at most 12 rows.

#### US-8 portrait: what their agents do

- **Shows:** counts of tool calls by category, with the window's share as a tick; time in each
  phase, with idle time shown separately; and the commands their agents ran most.
- **Records:** the call logs, and the phase blocks of the user's newest sessions.
- **Computation:** `row.cats`; `phases.time_s`, summed over the phase blocks of the newest
  `PHASE_SESSIONS` (80) sessions whose calls are read (`TrajectoryReducer.phase_block`, as in
  SE-3); and the 8 most frequent masked previews among the user's calls.
- **Cost:** up to 80 phase blocks per portrait, each made as SE-3 says and kept in the reducer's
  block cache (4,096 blocks).

#### US-9 portrait: projects and tasks

- **Shows:** the projects their sessions worked in; tasks and lone stretches, and how many of them
  are labeled in the agent's own words (commit, pull request, log); and the newest 40 tasks.
- **Computation:** `row.projects` (the top 5 and a total), and `tasks` over the same 80 blocks.

#### US-10 portrait: at the scheduler

- **Shows:** what the user's agent sessions asked of Slurm: jobs, refusals, queue checks and checks
  per job, polling sessions and their hours, the longest polling stretch, the `--parsable` share,
  cancels, GPU-hours.
- **Records:** as SL-2.
- **Computation:** `slurm_by_user` folds the Slurm reducer's agent keys into sessions
  (`SlurmReducer._sessions`) and reads each session's polling with `SlurmReducer._polls`. Only
  agent sessions are included.

#### US-11 portrait: sessions

- **Shows:** the newest 60 sessions, each with its start, agent, surface, launch badges, category
  and headline, how long it was open, tool calls, tasks and how many are quoted, a bar of time by
  phase, CPU, and a link to its process tree.
- **Computation:** each session's sums (`portraits._session`) and, where its calls are read, its
  block's `headline`, `category`, tasks and phase times. The 60 sessions served are among the 80
  whose blocks US-8 reads.

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
- **Records:** the node records, meaning any record (`exit`, `truncated`, `residency`, `conn`,
  `netio`, `submit`) of a process whose role is `agent`, `infra` or `client` (`pid`, `ppid`,
  `_role`, `agent_type`, `user`, `_command`, `args`, `cwd`, `tree_root_pid`, `_approval`,
  `_approval_detail`, `_surface`, `duration_s`); the trajectories' folded sessions; and the
  workspace's views (AG-4).
- **Computation:** in the page (`typeStats`), over the agent nodes of the trees the service sent.
  A node's processes are its folded trajectory session's `n` (`tool_calls` on the node), and its
  CPU is that session's CPU.
- **Cost:** counted over the trees sent (at most `reducers.cascades.top_trees`, 1,000, deepest
  first). The strip says so when the list is cut.

#### AG-2 agents and their subagents

- **Shows:** every tree with more than one agent, and every agent on its own, grouped by user or as
  one list, running ones first. Three or more alike agents (same user, type and start folder) fold
  into one row. It shows marks B (bypass flag), A (auto mode), U (tool calls not sandboxed), F
  (sandbox failed to start) and O (wrote outside its start folder), each with a count, which act
  as filters. It also shows the agents that ran nothing and a count of the idle `claude daemon`
  spares.
- **Records:** as AG-1, and the sandbox's tool-call verdicts.
- **Computation:** `CascadeReducer.feed` keeps a node per (host, pid) and a census of exits by
  (agent type, role). `result` takes the nodes last seen in the window; adds agents known only
  from their tool calls; turns
  unclaimed `bg-spare` sessions into infrastructure (`_idle`); finds each node's parent with
  `_parent` (the nearest node up the process index, at most 64 hops, never linking two different
  users; for a `bg-pty-host`, its user's only daemon; otherwise the collector's tree root); cuts
  cycles; and makes a Codex TUI or a Node launcher over the same agent a client. It walks each
  tree from its root (`_tree`), recording each agent's level, fan-out, folded session
  (`traj.agent_sessions`), start folder, workspace summary, and the verdict of its tool calls
  (`sb.agent_calls`, `session_state`). The trees with agents are sorted by agent depth, agents,
  tool calls and recency, and cut to `top_trees`.
- **Cost:** per record, a dict per node (at most `max_nodes`, 20,000; the rest are counted) and a
  census counter. Per read, made each rebuild tick: a parent walk per node, the trajectories'
  fold, the workspace's fold and a view per agent, and a walk of every sandbox tool call. Not
  measured.

#### AG-3 an agent tree

- **Shows:** the picked tree's agents, and the infrastructure and clients they run under, as boxes
  with launch marks, version, surface, and how each parent was found.
- **Computation:** a tree sends at most `max_nodes_shown` (60) nodes. `_pick` chooses them: the
  tree's shape first, then, one parent at a time, each parent's busiest agents that started no
  other agent. What the remaining agents did is counted on their parent as `folded`.
  Infrastructure and clients with no agent below them are counted (`hidden_nodes`) but not sent.
  The page lays out the tree (`TreeDiagram.layoutTree`).

#### AG-4 blast radius: summary

- **Shows:** for the picked agent, alone or with its subagents: its start folder, its processes and
  the share of them that ran inside that folder, directories, the files its commands mention
  (inferred, split into read, write, delete and other), writes outside the start folder, and the
  top-level places outside it that the agent reached.
- **Records:** `exit` of agent-class processes keyed on an agent pid (`_kt` `apid`):
  `cwd`, `cwd_source`, `io.rchar_mb`, `io.wchar_mb`, `io.rd_mb`, `io.wr_mb` (each process's own,
  over these records: `WorkspaceReducer`'s `OwnIo`), `args`, `argv_source`,
  `fork_only`, `_role`, `user`, `_ts_epoch`, `tree_root_pid`.
- **Computation:** `WorkspaceReducer.feed` records, per collector key, the processes per `cwd` with
  their syscall and disk bytes (measured), and the paths each command line names, sorted by what
  the command does with them (inferred; each distinct argv is parsed once by a regex tokenizer,
  `workspace.py:plan`). At read, `fold` merges the keys into their agent through the process
  index. `view` then collapses directories deeper than `collapse_depth` (4) below the start
  folder or a top-level root, folds a directory's quiet subdirectories, flags writes and deletes
  named outside the start folder, and groups the outside directories by location. The start
  folder is the agent's own cwd, or its first call's directory off `/`.
- **Cost:** measured in a 24 h build of rtx6000a's 2026-10-10 (1.89 M records, ~90 s): 2.4 s in
  `feed`, about 1.4 µs per record (`workspace.py`). Bounds: `max_keys` 150,000 (at the cap, keys
  last seen before the window are dropped first), `max_dirs` 300 per key, `max_dir_entries`
  400,000, 64 names per directory and 32 per command line, with each excess counted. Views are
  served for the 300 busiest agents (`max_agents`) and for every agent that wrote outside its
  start folder.

#### AG-5 blast radius: written or deleted outside its start folder

- **Shows:** the paths a command named for writing or deleting outside the start folder, grouped
  by where they landed: another user's home and system paths first, the agent's own scratch last.
- **Computation:** `view` keeps at most 400 paths per view and serves 16, counting the rest
  (`flags_more`). The page shows every loud one and the first three quiet ones.

#### AG-6 blast radius: directories

- **Shows:** each directory: processes that ran there (measured), bytes requested through `read()`
  and `write()` there (measured per process, credited where it ran), the files its commands mention
  (inferred), when it was seen.
- **Computation:** `view`'s rows: the 40 busiest (`max_dirs_shown`) and every flagged one. A byte
  cell opens IOCMDS for the agent's pids (and its subagents'), host and directory. The replicas
  can answer that lookup.

#### AG-7 how deep agents nest; fan-out

- **Shows:** trees by the largest number of agents on one root-to-leaf path; and the agents (and
  tree roots) that started an agent, by how many agents they started directly, in power-of-two
  bins.
- **Computation:** `result` counts every tree with agents in the window, whether it was sent or
  not. `_tree` records a fan-out for each agent or root that has an agent directly below it. The
  page bins the fan-out.

#### AG-8 tool mix

- **Shows:** the 12 kinds of tool (`tool_buckets.BUCKETS`), by class (a row adds to 100%) or by kind
  (a column adds to 100%), weighted by one resource at a time: processes, CPU, disk I/O, network,
  memory (each process's peak, summed).
- **Records:** `exit`: `_eff` (its kind by `tool_buckets.bucket`), `_a3`, `agent_type`, `cpu_s`,
  `io.rd_mb`, `io.wr_mb`, `net_tx_bytes`, `net_rx_bytes`, `peak_rss_mb`, and the dedup's fields
  (LV-4).
- **Computation:** `ToolMixReducer.feed` counts per (class, kind) and per (class, tool), and also
  counts an agent's exits per (agent type, ...), after the shell-wrapper dedup. Disk I/O is each
  process's own (its `OwnIo` is fed every exit, including a shell that the dedup drops). `_rows`
  gives each kind's counts, sums and shares per class. A page filter to one agent type uses that
  type's own counters (`by_agent_type`). The page computes a class's share of a row out of every
  class counted, not just the classes shown (`lib/shares.ts`), and the total beside it is that
  whole.
- **Cost:** as LV-4. A byte cell opens IOCMDS by kind and class, which reads from the JSONL unless
  the page is filtered by user.

#### AG-9 top tools

- **Shows:** per class, the 12 tools with the most of one resource: processes, CPU, mean peak memory,
  disk I/O, network.
- **Computation:** `_tools` serves the union of the 12 busiest tools by processes, CPU, disk,
  network and mean peak memory, with every measure on each row. The page sorts the rows and cuts
  them to 12. For a payload without `tools`, the page draws `head` (processes and CPU shares)
  instead.
- **Cost:** per read, `most_common` over every tool token of a class. The per-class tool maps have
  no bound.

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

- **Shows:** agent sessions in the window (with users, the busiest user's share of agent
  processes, probes set aside, and terminal or script against app); the median processes per
  agent session (with p90 and the maximum); the median session length (with the longest); and the
  share of agent processes that only wait. The other classes appear beside each.
- **Records:** `exit` (`_sid`, `_kt`, `_kv`, `_ts_epoch`, `user`, `_purpose`), and each root's own
  record (`_surface`).
- **Computation:** `TrajectoryReducer.summary` runs over every folded session whose last record is
  in the window. Per class, it counts the rooted sessions and the probes, processes per session,
  and wall time (first to last record in the window), with exact quantiles found by sorting. It
  also counts waiting processes (those whose purpose is `poll` or `slurm_mon`), processes per user,
  and surfaces.
- **Cost:** one pass and two sorts over every session per read. It is made together with SE-2.

#### SE-2 sessions, ranked

- **Shows:** the window's sessions ranked by one of seven measures: recency, processes, CPU, peak
  memory, disk I/O, network, or distinct tools. Each row shows its key and date, surface, category,
  a live badge with the processes running now, user, tool calls (for an agent whose commands are
  read) or processes, CPU, length, and the quantity it is ranked by. When opened from LV-20, it
  shows only that user's sessions.
- **Records:** `exit`: `_sid`, `_kt`, `_kv`, `pid`, `ppid`, `cpu_s`, `_eff`, `_purpose`,
  `peak_rss_mb`, `io` (`rd_mb`, `wr_mb`, `scope`), `net_tx_bytes`, `net_rx_bytes`, `user` or `uid`,
  `tree_root_pid`, `_sandbox`, `_sandbox_ancestry`, `cwd`, `args`, `duration_s`; each root's own
  record (`_approval`, `_surface`); the process index; for running, the live tier's roots and tree
  ticks.
- **Computation:** for each exit, `TrajectoryReducer.feed` updates a `Session` per collector key
  (host, day, class, key). A `Session` holds counts, CPU, run-length chains of tools and of
  purposes (400 runs), drift arrays, each process's own disk bytes (`own_io`: its figure minus what
  its reaped children carried), and, for an agent session, its call log (`_phase`, bottom up). At
  read (`result`), sessions idle for longer than the larger of 2 h and the window are dropped.
  Every key is folded into its agent through the process index (`folded`, `_fold_all`; cached
  until a record arrives, an eviction happens or the index changes). Each rank is
  `heapq.nlargest(15)` over the window's sessions (recency ranks rooted sessions only). Each listed
  session gets a row (`_row`) with its phase block (SE-3). `Aggregator._running_rows` marks the
  newest row of each agent whose root's records arrived in the last 600 s, adding its tree's
  census.
- **Cost:** per record, a few adds, a run-length push, the drift arrays and, for an agent, the
  phase join (pending entries up to `pend_max`, 50,000). Per read, under the aggregator's lock:
  the fold of every held key if anything changed ("half a second to most of one" at 24 h on
  rtx6000a, per `proctree.py:lookup`), seven partial sorts over the sessions, and up to 105 rows.
  While the list is shown, the page reads it again every 5 s, and the service remakes the payload
  at most that often. Memory: `max_sessions` (150,000) keys per view, about 6 KB each. Past that
  cap, whole trees are dropped, least recently active first (`_shed`).

#### SE-3 a row's phase band, task line and category chip

- **Shows:** for an agent whose commands are read (Claude Code, Codex): a band of its phases, a tick
  where each task or lone stretch begins, a line naming its tasks (or the order of its phases), the
  category chip (the use that accounts for half its work), and, at a running session's live edge,
  what the census shows running. For any other row, it shows the purpose of each process, run by
  run.
- **Records:** the session's call log (the agent's direct children, each a call named by
  `phase_rules`, with its command text, start, end and what ran under it), its pending entries,
  and, for the live edge, `residency` `by_command`.
- **Computation:** `_phases` and `_build_block`. The agent's day rows are folded into one block
  (`PhaseLog.fold`, at most `max_calls`, 20,000, calls), and the open calls are added from the
  pending join (`_open_calls`). The block then goes through `phase_segment.prepared`,
  `phase_tasks.task_spans` (tasks), `phase_segment.segment` (phases, cut at the tasks'
  boundaries), `phase_tasks.name_tasks` (labels, taken from the agent's own words or the names in
  its commands, and the headline) and `phase_summary.summarize` (category, uses and summary: the
  work weighed to the last phase's own calls, `own_t1`, and a call's time past its phase credited
  to its own use, `ran_on`). Blocks are cached per row (`_phase_cache`,
  4,096 blocks) and made again when the row's calls, records or pending entries change. For a
  session that is still working, that means on each read. `running_now` compares the tree's newest
  census with its baseline, which is the smallest count each command had over the session's ticks.
- **Cost:** per record, a tool call is masked and its names are read once, when it is logged
  (`TrajectoryReducer._call`; `phase_names.tagset` took about 200 µs per call on an earlier
  sample, `phase_names.py`). Per block, measured at the 20,000-call cap on a shared host
  (2026-11-03; `phase_segment.py`, `phase_tasks.py`): `segment` took 0.06-0.10 s, or 1.66-1.84 s
  with half the calls hung and an idle gap after every other call; `task_spans` took 0.06-0.59 s;
  and `name_tasks` took 0.04-0.07 s. Making a block for every session of a week on eight login
  nodes takes minutes (README), which is why the plates do not count categories.

#### SE-4 inspector: facts; purpose ribbon and tool tokens

- **Shows:** at the head of the inspector: the row's first and last record, its length, its tool
  calls (where the agent's commands are read), processes, CPU, distinct tools, pieces, sandbox and
  approval. "Pieces" means the keys folded and how the least certain one was attributed. It is
  shown only where there is more than one key, or where the least certain key was placed by a
  weaker rule than the agent itself or an ancestor. At the head of the Processes part, it shows
  the purpose ribbon and tool tokens, and each run opens the tree at its first record.
- **Computation:** SE-2's row (`tool_rle` cut to 60 runs, `purpose_rle` to 80, with their start
  times). The inspector's own copy is made under the lock by `_inspect`. When the window's fold is
  stale, its `lookup` folds only the keys that share the row's prefix. The agent's other days come
  from the window's last fold as it stands (`TrajectoryReducer._agent_days(fresh=False)`), not
  from a fresh fold of every session.

#### SE-5 inspector: what was read, and its limits

- **Shows:** the records read and their span; the bytes re-read, from how many day-files, and how
  long it took; parents with no exit in the read; whether the tree came from the cache; whether
  the read stopped at its record or byte budget, with a button to continue from there; processes
  counted under their parent; entries past the row budget; and parents placed on weaker evidence
  than their own `ppid`.
- **Computation:** `_inspect`'s `scan` (`bytes`, `parsed`, `skipped`, `method`, `stopped`,
  `stopped_at`), `bounds` and `links`.

#### SE-6 inspector: headline, category, summary

- **Shows:** the project and the names that recur across the tasks; what kind of session it was;
  and a one- to three-sentence description of the session, built from the block's own numbers.
- **Computation:** made with the block (SE-3).

#### SE-7 inspector: navigator

- **Shows:** the session's whole span, marking the stretch this read covers; process starts per
  time bin, stacked by purpose on the read's axis, with the task lane above and the phases tinted
  behind; the agent's housekeeping as ticks; failures as ticks; and quick jumps to the busiest
  minute, the first tool call, the longest process and each failure.
- **Records:** every record the read matched, drawn or only counted: start (`_ts_epoch` less
  `duration_s`), `_purpose`, `_role`, `duration_s`, `exit_code`, `signal`.
- **Computation:** `TreeBuilder._note` notes every matched record before any budget applies.
  `TreeBuilder.nav` bins the starts at the finest width in `BIN_WIDTHS` that keeps the read within
  2,400 bins. It also finds the busiest clock minute, the first tool call, the longest process
  other than the root, and the failures (2,000 listed, the rest counted). Tasks and phases come
  from the row's block.
- **Cost:** part of the inspector's read.

#### SE-8 inspector: task list, or phase list

- **Shows:** each task, which opens into its phases, and each phase, which opens into the commands
  that ran most in it (`top_commands`, 3). Under a closed task, its phases appear in order on one
  line, with each phase's chip and the length of any phase that takes up a quarter of the task
  (`sequenceParts`). Lone stretches and cutting gaps get lines of their own. A block with no task
  lists only its phases.
- **Computation:** the block's `tasks` and `list` (SE-3).

#### SE-9 inspector: process tree

- **Shows:** every process the read itemised, under its parent, with its start, duration, own and
  subtree CPU, disk and network bytes, exit or signal, and a bar on the read's time axis.
  Consecutive siblings of the same shape are folded into counted groups, weaker parent links are
  tagged, and processes that were counted rather than drawn appear as `⋯` rows. Find, "hide
  routine" and "hide typos" work in the page. The pop-out draws the same read on its own canvas.
- **Records:** `exit`, `truncated` and `residency` whose `_sid` is one of the row's keys: `pid`,
  `ppid`, `_command`, `args` (masked, then cut to 220 characters), `cwd`, `_ts_epoch`, `duration_s`,
  `cpu_s`, `io`, `net_tx_bytes`, `net_rx_bytes`, `exit_code`, `signal`, `_role`, `depth`, `_kv`;
  parents with no record from the window's process index and the cascades' nodes.
- **Computation:** INSPECT. `TreeBuilder.feed` lets each process claim the children waiting for it
  (a parent exits after its children). It takes each process's own disk and syscall bytes (its
  `OwnIo`, over the records the read takes). For a `truncated` record, which was running at a
  collector stop, that means its figure minus what its exited children had carried so far. As a
  result, a subtree's bytes count each process's own bytes once. `feed` itemises up to
  `inspect_hold` processes and up to `inspect_per_parent` childless processes of one command under
  one parent, and counts the rest under their parent (when reading newest first, it keeps the
  newest instead). `resolve` attaches what is left by the index, the collector's depth or the
  fold, and draws parents that have no record (at most 2,000). `render` folds runs of siblings
  (motifs of up to 4) and serves `inspect_nodes` entries, breadth first. `_typos` marks what a
  person mistyped at a terminal.
- **Cost:** the cost of INSPECT: one read at a time, up to 150,000 records or 6 GiB. Building a
  record costs about 44 µs on FAS, in the `inspect_workers` processes, and feeding it to the tree
  about 6 µs, in the service's own process. A 150,000-record read takes 2.5-2.7 s. Trees are
  cached for 600 s.

#### SE-10 inspector: map of the whole session

- **Shows:** under the session's gist, before any process is read, the tasks as a lane of blocks
  above the phases as a band, on the session's own clock. The clock runs from the session's first
  record (or its first phase, if that begins earlier) to the end of its last phase. For a session
  running now, it runs on to now, and the live edge ends it. Where it fits, a task is numbered and
  labelled the same way as in the list below it. A phase is tinted the way the navigator tints it,
  with a rule in its full colour along its foot, and is named where its name fits. A guess is
  hatched, and idle time is a dashed outline. Clicking a task or phase selects it, just as clicking
  its row in the list does.
- **Computation:** none of its own. It draws the row's block (SE-3) on the same axis as the row's
  band (`bandAxis`), so it covers the whole session no matter how far the read reached. SE-7
  cannot do that.
- **Cost:** none on the service.

## Login nodes › Resources

This section covers the Resources sub-tab's own panels, then the I/O panels that it draws with the
Risks tab's code (`tabs/RiskIoTab.tsx:RiskIoTab`, `part="io"`).

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
- **Computation:** each value goes into a `Quant`, a DDSketch of every value with 1% relative error.
  It uses logarithmic buckets, and two sketches merge by adding counts, so a merged window gives
  the same result as one pass. `n`, `min`, `max` and `mean` are exact. A null is counted for
  coverage. A field that is exactly 0 everywhere is flagged `all_zero` and drawn as not measured.
  The on-CPU share is `sched_run_s / duration_s`. A process whose run time exceeds its lifetime by
  more than 5% is set aside and counted. `top` keeps the process behind each max.
- **Cost:** five sketch adds per record, bucketed 4,096 values at a time; per read, the buckets are
  sorted once per measure. The sketch's size grows with the range of the values, not with how
  many there are.

#### RE-2 faults, delays, and D-state

- **Shows:** per class, the average per process and the total of: minor and major page faults
  (major ones in reaped children too), voluntary and involuntary context switches, block-I/O,
  swap-in and memory-reclaim delays, and D-state (processes that stalled, time, stalls, the longest),
  each with its coverage. Values longer than the process's lifetime are left out and counted.
- **Records:** `exit`: `min_flt`, `maj_flt`, `cmaj_flt`, `nvcsw`, `nivcsw`,
  `blkio_wait_s`, `swapin_wait_s`, `freepages_wait_s`, `dstate_wait_s`, `dstate_episodes`,
  `dstate_max_s`, `dstate_src`, and `_impossible` (the normalizer's list of waits it voided).
- **Computation:** an `Acc` (sum, present count, records seen) per field and class; the page divides.

#### RE-3 worst individual processes

- **Shows:** for each of three measures (longest D-state wait, most major faults, longest runqueue
  wait), up to 12 processes, grouped by user.
- **Computation:** `_keep_worst` appends a row when the value is at least the list's floor. When the
  list reaches 240 rows, it is sorted and cut to 24, and the floor is raised. `result` serves 12.
  Merging cuts the list again by the same rule.
- **Cost:** a row is made only for a value above the floor, so few records make one.

#### RE-4 disk read by tool, RE-5 disk written by tool

- **Shows:** the 14 tools that read the most from disk (or, for RE-5, wrote the most to it), as
  bars, with the bytes requested and the cache hit on hover. It covers every class; the class chips
  do not narrow it.
- **Records:** `exit`: `_eff` or `_command`, `io.rd_mb`, `io.rchar_mb`, `io.wr_mb`,
  `io.wchar_mb`, and `pid`, `ppid`, `host`, `io.scope` for the join.
- **Computation:** `IoReducer.feed` sums each tool's four byte counters, using each process's own
  bytes (its `OwnIo` takes back the part of a parent's figure that came from the children it
  reaped). It also counts the tool's processes and those with an `io` block. `result` serves the
  union of the top `reducers.io.top_n` (20) tools on each counter (`_heaviest`, up to 80 rows). The
  page ranks them by disk bytes and draws 14. A bar opens IOCMDS for the tool, which reads from the
  JSONL unless the page is filtered by user.
- **Cost:** a few adds per record; the tool map has no bound; per read, four `nlargest` passes.

#### RE-6 network bytes, and per class

- **Shows:** per class, bytes sent and received, and the network calls, with coverage; a cell opens
  IOCMDS.
- **Records:** `exit`: `net_tx_bytes`, `net_rx_bytes`, `net_calls`, each null when the
  collector's netbytes block was dropped.
- **Computation:** an `Acc` per class and counter.

#### RE-7 network endpoints

- **Shows:** the top 10 remote endpoints by send and receive calls, or by bytes, for the classes
  shown, with each row split by class. Each endpoint is named by reverse DNS, or failing that by a
  command line, or failing that by its address. It also shows the share of the bars' bytes whose
  endpoint is known ([Network endpoints](../../dashboard/README.md#network-endpoints)).
- **Records:** `exit`: `net_endpoints` (schema 7): per endpoint `addr`, `inbound`,
  `calls`, `tx_bytes`, `rx_bytes`, `proto`, `port`, `host`, `external`, `provider`; and its
  `unattributed`, `overflow`, `unaccounted` losses.
- **Computation:** `IoReducer._feed_endpoints` adds each address's calls and bytes per class, and
  every address is kept. On every `/api/panels` read, `endpoints.serve` names each address
  (lookups run on background threads and are kept in `<cache.dir>/rdns.json`) and groups the
  addresses that share a name. For every combination of the classes present, it keeps the union
  of the top 10 by calls and by bytes (`fold`). The page ranks the combination it shows.
- **Cost:** per record, a dict update per endpoint; per `/api/panels` read, a pass over every
  address for every class combination (up to 15). While names are pending, the page reads
  `/api/panels` again after 3, 6, 12, 24 and 48 s. Not measured.

#### RE-8 I/O by user

- **Shows:** per user, with the class of their newest record: processes, disk and requested bytes
  read and written, network bytes received and sent, and I/O coverage. A cell opens IOCMDS for the
  user, and the replicas can answer that lookup.
- **Computation:** `IoReducer.feed` per user, using each process's own bytes (RE-4). `result`
  serves the union of the top 20 users on each of the four I/O counters. The class chips keep or
  hide a user based on that one class.

#### RE-9 I/O distributions

- **Shows:** per class, quantiles of each process's own bytes read requested, read from disk,
  written requested and written to disk (RE-4's join).
- **Computation:** a `Quant` per counter and class (the same sketch as RE-1). A record with no `io`
  block is counted as not measured.

## Login nodes › Risks

See [Risk rules differ by cluster](../../dashboard/README.md#risk-rules-differ-by-cluster). If the
cluster's profile leaves a section out, that section is neither computed nor drawn.

| | figure | path | payload | code (service · page) |
|---|---|---|---|---|
| RI-1 | a plate per section | BUILD | `risk.sections`, `login_compute.by_class`, `dangerous.rows`, `secrets.rows` | `reducers/risk.py:RiskReducer.result` · `tabs/RiskIoTab.tsx:RiskIoTab` |
| RI-2 | compute on a login node: bars, per tool, heaviest processes | BUILD | `risk.login_compute` | `RiskReducer.feed`, `result` · `tabs/RiskIoTab.tsx:RiskIoTab`, `InstanceTable` |
| RI-3 | commands that should not run here | BUILD | `risk.dangerous` | `RiskReducer.feed`, `_matches`, `result` · `tabs/RiskIoTab.tsx:RiskIoTab`, `WhoChips`, `InstanceTable` |
| RI-4 | credentials on a command line | BUILD | `risk.secrets` | `RiskReducer.feed`, `_matches`, `result` · `tabs/RiskIoTab.tsx:RiskIoTab` |
| RI-5 | rules in effect | BUILD (config only) | `risk.rules` | `risk.py:ruleset`, `RuleSet.summary` · `tabs/RiskIoTab.tsx:RulesInEffect` |

#### RI-1 a plate per section

- **Shows:** for the compute section, the agents' CPU-hours in builds and interpreters, with the
  other classes beside them, the agent users, and all classes' share of the CPU. For a commands
  section, the runs matching its rules by class, and the two commands agents ran most. For
  credentials, the matches by class and the number of command lines scanned.
- **Computation:** the page sums the payloads of RI-2 to RI-4 per class over the section's rules.

#### RI-2 compute on a login node

- **Shows:** CPU spent in the tools of the compute kinds (build, interpreter) and the transfer kind
  (`net/data`), by tool, with processes, users and hosts; CPU-hours per class; and the 24 heaviest
  processes.
- **Records:** `exit`: `_eff` or `_command` (its kind), `cpu_s`, `user`, `host`, `_a3`.
- **Computation:** `RiskReducer.feed` adds each exit of a compute or transfer kind
  (`risk.login_compute`) to its tool. A compute exit also joins a heaviest list (cut to 200 when
  it reaches 400) and is added to its class's CPU. `result` ranks the tools by CPU (25 rows) and
  gives the compute CPU's share of all CPU.
- **Cost:** a kind lookup and a few adds per record.

#### RI-3 commands that should not run here

- **Shows:** per rule: the command, severity, runs, users, who ran it by agent type, the share of
  runs with an exit code that exited 0, and CPU. A row opens its first 12 runs.
- **Computation:** per matching rule (using LV-6's match): runs, CPU, users, hosts, classes,
  (class, agent type) counts, the first 12 runs, the runs with an exit code, and those that exited
  0. Rows are sorted by severity, users and runs.
- **Cost:** as LV-6.

#### RI-4 credentials on a command line

- **Shows:** per pattern: severity, matches, users, tools, and who ran it. A row opens its first 12
  matches with pid, user and tool, but never the command line. It also shows the number of command
  lines scanned.
- **Computation:** the credential patterns run on `args` alone and are case-sensitive. The matched
  text is never kept; a match keeps its identity fields but not the argv.

#### RI-5 rules in effect

- **Shows:** every rule, with its resolved pattern, severity, source (built in, changed, added) and
  section; and the rules that are switched off, in no section, invalid or unknown.
- **Computation:** `RuleSet.summary`, built once per distinct `risk` configuration (`ruleset`). No
  records.

## Login nodes › Slurm

See [Slurm: how sessions use the scheduler](../../dashboard/README.md#slurm-how-sessions-use-the-scheduler).
This sub-tab is gone when `slurm.enabled` is false. Requests are read from argv, so a `#SBATCH` line
is not seen.

| | figure | path | payload | code (service · page) |
|---|---|---|---|---|
| SL-1 | plates: agent jobs submitted, queue checks per job, agent sessions polling, submissions refused | BUILD | `slurm.by_class`, `slurm.rejections` | `reducers/slurm.py:SlurmReducer.result` · `tabs/SlurmTab.tsx:SlurmTab` |
| SL-2 | sessions at the scheduler | BUILD | `slurm.sessions`, `sessions_total` | `SlurmReducer.feed`, `_submit`, `_sessions`, `_polls`, `result` · `tabs/SlurmTab.tsx:Sessions`, `SessionDetail` |
| SL-3 | how each class uses the scheduler | BUILD | `slurm.by_class`; `tool_mix.by_class[c].rows` | `SlurmReducer.result` · `tabs/SlurmTab.tsx:ByClass` |
| SL-4 | submissions the scheduler refused | BUILD | `slurm.rejections` | `SlurmReducer._submit`, `slurm.py:reason` · `tabs/SlurmTab.tsx:SlurmTab` |
| SL-5 | latest submissions | LIVE | `submits` | `reducers/live.py:LiveReducer._submit` · `tabs/SlurmTab.tsx:Latest` |

#### SL-1 plates

- **Shows:** jobs agents submitted (with agent sessions and the busiest minute), agent queue checks
  per job, agent sessions that polled the queue (with hours spent polling), and agent submissions
  refused (with their share and the commonest reason). The other classes appear beside each.
- **Computation:** `slurm.by_class` (SL-3) and the agent's first refusal reason (SL-4).

#### SL-2 sessions at the scheduler

- **Shows:** per agent session (its collector keys folded into the agent), or per person or IDE and
  user: jobs, refusals, the most in one minute, distinct commands with numbers masked, sbatch
  flags, queue checks, checks per job, and the longest polling stretch. When a row is opened, it
  also shows what came back, the tools, queue checks by command, polls, partitions, cancels,
  `srun`/`salloc` runs, the share with the request on the command line, GPUs and GPU-hours,
  refusal reasons, and the latest command.
- **Records:** `submit`: `tool`, `args`, `job_id` or `job_ids`, `exit_code`, `captured_tail`,
  `req_src`, `partition`, `time_limit_s`, `mem`, `gpus`, `array`, `user`, `_a3`, `agent_type`,
  `_kt`, `_kv`, `tree_root_pid`, `host`, `_ts_epoch`; `exit` of the query commands,
  `scancel`, `srun` and `salloc` (`_command`, `_ts_epoch`, `exit_code`, `signal`).
- **Computation:** `SlurmReducer.feed` and `_submit` work per key. An agent's key is (host, agent
  pid, tree root), and anyone else's is (host, class, user). Per key they record the outcome
  (`outcome`: job, dry run, step, refused, no id), flags by regex, whether the request was on the
  command line, the command's shape (a blake2b of `args` with digits masked), submissions per
  minute, partitions, GPUs times array tasks, GPU-hours where GPUs and a time limit were both on
  the command line, and each query's time. At read, `_sessions` folds the agent keys into their
  owner through the process index. `_polls` joins a session's queries that fall within 2 s of each
  other into a poll, and polls within 3 minutes of each other into a stretch. A stretch of 10
  minutes or more counts as polling. Sessions are sorted by queries plus 10 times jobs and
  refusals plus runs, and 200 are served.
- **Cost:** a few counters per record. The bounds are 50,000 keys (the rest are counted), and per
  key 50,000 query times and 2,000 shapes. Per read, one sort of each session's query times.

#### SL-3 how each class uses the scheduler

- **Shows:** per class: jobs, sessions and users, queue checks and checks per job, sessions polling
  and the hours of it, the share of the class's processes that are scheduler commands, the share of
  submissions with each flag and with the request on the command line, dry runs, refusals and their
  share, the busiest minute, `srun`/`salloc` runs and failures, cancels, GPU-hours, and the time
  limit asked (median, p90).
- **Computation:** `result` sums over the sessions per class. The time limit is a `Quant` per
  class. The page computes the scheduler share as the tool mix's `slurm-submit` and `slurm-query`
  kinds divided by the class's processes (AG-8).

#### SL-4 submissions the scheduler refused

- **Shows:** per class, tool and reason: how many times it happened, and one example (user, time,
  and the command on hover).
- **Computation:** a refused submission (no job id, a non-zero exit, and a scheduler error line in
  `captured_tail`) is counted by (class, tool, reason). The reason is that error line with numbers
  masked, cut to 160 characters (`reason`). The 20 commonest are served.

#### SL-5 latest submissions

- **Shows:** the newest submissions in the window: when, user, class or agent type, the job id or
  what came back (refused with its reason, dry run, step), the partition, the time and GPUs asked
  for (or "in the script"), and the command.
- **Records:** `submit`, as SL-2.
- **Computation:** `LiveReducer._submit` keeps the 50 newest (`live.submit_tail`), each one judged
  by `slurm.outcome` and masked. `result` cuts them to the window.

## Ask LLM

| | figure | path | payload | code (service · page) |
|---|---|---|---|---|
| AL-1 | suggestions for the operator (cards) | ASK over BUILD and LIVE | `/api/ask/{job}` `suggestions` | `aggregator.py:Aggregator.ask`; `ask.py:snapshot`, `parse_suggestions`, `Asker.submit` · `tabs/ChatTab.tsx:SuggestionList` |
| AL-2 | marked done (list) | none: the service's own file | `/api/suggestions/done` | `ask.py:Asker.done_list`, `mark_done` · `tabs/ChatTab.tsx:ChatTab` |
| AL-3 | ask about the whole window (answers with their sources) | ASK over BUILD and LIVE | `/api/ask/{job}` `answer`, citations | `Aggregator.ask`; `ask.py:snapshot`, `Asker` · `tabs/ChatTab.tsx:ChatTab`, `components/Ask.tsx`, `components/Cite.tsx:Sources` |

#### AL-1 suggestions for the operator

- **Shows:** cards, most urgent first, each with what to do, the numbers behind it with their
  sources, and links to the accounts named. It runs only when the button is pressed.
- **Records:** none are read for it; it uses the window's payloads.
- **Computation:** `Aggregator.ask` with `context.suggest`. The context holds the `/api/panels`
  payload for the page's window and filter (refused while the window is still being read), the
  live payload's `now`, `users`, `hosts` and `window`, the trajectories' summary and the 15
  sessions with the most processes, the cascades with a session hint on each agent, the accounts
  the risk rows name (`risk_who`), and the suggestions marked done. It is compacted to 120 KB by
  cutting the largest section's lists and strings a step at a time (`STEPS`). Command lines are
  dropped unless `ask.include_commands` is set, and every object gets a citation ref. The agent is
  asked one fixed question. The answer is JSON, which is checked and cut to 8 suggestions
  (`parse_suggestions`).
- **Cost:** one run of the agent CLI, at most 240 s, one at a time. Not measured.

#### AL-2 marked done

- **Shows:** the suggestions that anyone marked done. They stay marked for everyone until someone
  undoes them, and they are left out of the next pass.
- **Computation:** `Asker.mark_done` copies the suggestion from its own job into
  `<cache.dir>/ask/done.json` (200 kept). No records.

#### AL-3 ask about the whole window

- **Shows:** the answer to a question about the window on screen, with its figures linked to the
  rows they came from. The last turns of the conversation are sent with each question.
- **Computation:** as AL-1, but without the fixed question, and with the page's filter left out
  unless the question is about one panel. A panel's own "ask" button, on any tab, sends only that
  panel's keys (`Aggregator._ask_panel`). The inspector's button sends a digest of the session's
  process tree, read by INSPECT (`Aggregator._ask_session`, `ask.py:session_digest`).
- **Cost:** as AL-1.

## Search & export

This tab is hidden on `develop` (`App.tsx`, `HIDDEN_TABS`). Its code is kept, and `/api/events`
and `/api/export` are still served.

| | figure | path | payload | code (service · page) |
|---|---|---|---|---|
| EX-1 | records, filtered (table) | EVENTS | `/api/events` `rows`, `next_cursor`, `total_scanned`, `event_filter_defaulted` | `export.py:scan`, `make_predicate`; `app.py:api_events` · `components/EventsExplorer.tsx:EventsExplorer`, `tabs/LiveTab.tsx:EventTable` |
| EX-2 | download | EVENTS | `/api/export`: JSONL with a `_meta` header and a `_trailer` | `export.py:scan`, `meta_header`; `app.py:api_export` · `components/EventsExplorer.tsx:EventsExplorer` |

#### EX-1 records, filtered

- **Shows:** the records matching the filters, newest first, 200 per page (5,000 at most), grouped
  by user (class, commands, CPU, failed, latest), with each group opening into rows.
- **Records:** any kind. `event` defaults to `exit`, and the response says when it did. The fields
  the filters name: time, `_a3`, `agent_type`, `user`, `host`, `_eff`, `_purpose`, bucket,
  `session_key`, `_sandbox`, `_approval`, `depth`, `cpu_s`, `duration_s`, `exit_code`, `signal`,
  and `q`, a substring of the masked command line.
- **Computation:** EVENTS. When names are anonymized, `q` is matched against the aliased text,
  which costs a rewrite of each candidate (`app.py:search`).
- **Cost:** per page, bounded by the matches it needs and the files' time ranges. A deep page walks
  through the earlier matches again. Measured: see EVENTS above.

#### EX-2 download

- **Shows:** the same records as a JSONL file: the collector's fields plus the derived `_` keys,
  credentials masked.
- **Computation:** the same predicate, with every match streamed, 500 lines per chunk, up to
  `export.max_rows` (5,000,000). The trailer gives the row count and whether the cap cut the
  export short.
- **Cost:** a read of every planned file's range, bounded only by the cap.

## Internal

This tab holds the panels that are not settled yet, namely the sandbox panels. See
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
`signal`, `dstate_wait_s`, `is_agent`, `agent_pid`, `tree_root_pid`, `agent_type`, `user`. For
each agent root's launch, the tab also reads the root's own record of any kind, including
`truncated`, `residency`, `conn` and `netio` (`_approval`, `_approval_detail`).

#### IN-1 plates

- **Shows:** the share of agent tool calls that ran sandboxed, out of those whose outcome is known
  (isolated, not isolated, failed); agent sandboxes that failed to start, out of the wrapper calls;
  and the share of agent sessions launched with a bypass flag, out of those whose launch flags are
  readable.
- **Computation:** IN-2's, IN-5's and IN-4's counts, divided in the page over the known states.

#### IN-2 tool calls: did they run sandboxed?

- **Shows:** agent tool calls by outcome, by class; the sessions that made tool calls, grouped by
  user, with approval, calls, the share sandboxed and failed starts (40 served, busiest first);
  and the sessions that made none, folded together.
- **Computation:** `SandboxReducer._calls` reads an agent's exits bottom up. A child reports to its
  parent's slot (`below[(host, ppid)]`: whether a payload ran, was isolated, or was unknown). A
  wrapper or a Claude Code Bash tool shell becomes a tool call with an outcome: isolated, not
  isolated, failed (the wrapper exited non-zero with no payload), no payload, or unknown. A
  wrapper absorbs the calls below it, so nested wrappers fold into the outermost one. At read,
  `_sessions` takes the calls that were not absorbed and groups them by the session that owns them
  (found through the process index), alongside every agent root. `session_state` then judges each session by its
  calls.
- **Cost:** a few counters per record. For an agent's exit, there is also a slot (at most 200,000
  parents waiting, with the overflow counted) and a call per tool call, kept for the window with
  no cap. `_sessions` walks every call each time the payload is made, and the cascades (AG-2) and
  the Users tab (`portraits.inputs`) walk them again (`agent_calls`). The slots and their calls
  are carried from hour to hour when merging.

#### IN-3 sandboxed, per process and per session

- **Shows:** per class, the processes in each of the four namespace states, and the sessions with
  each verdict (sandboxed, partly, failed, not sandboxed, unknown, no calls). As in IN-2 and IN-4,
  a state that the axis can judge is shown as a share of the class's judged items. The rest
  (exited before it was checked, unknown, no calls, not an agent session, can't tell) are shown
  as counts, with the coverage under the class (`AxisBlock`, `lib/sandbox.ts:shareOfKnown`). This
  is the same share that a plate above prints.
- **Computation:** a counter per class of each counted record's `_sandbox`, and the session
  verdicts from IN-2.

#### IN-4 approval at launch

- **Shows:** per class, sessions and processes by approval at launch; and the launch flags as
  written, per agent type and reading, with their sessions.
- **Computation:** each root's verdict comes from its own record (`roots`, which keeps the
  newest). Sessions are those of IN-2. Per process, there is a counter of each counted record's
  `_approval`. `launch_modes` counts the rooted sessions by (agent type, approval, flags).

#### IN-5 sandbox wrapper calls, by outcome

- **Shows:** the calls per wrapper (`bwrap`, `codex-linux-sandbox`), agent type and outcome.
- **Computation:** `_sessions` counts the wrapper calls that no other wrapper absorbed, by wrapper,
  agent type and outcome.

#### IN-6 processes by sandbox wrapper, kind and sandbox state

- **Shows:** per (wrapper above it, wrapper or process, sandbox state): processes, their D-state wait
  and its coverage, and a plain reading of what the row means.
- **Computation:** a counter per key, with summed `dstate_wait_s` and a present count. `_reading`
  writes the description for each row.

#### IN-7 when each process was checked

- **Shows:** each process's namespace answer, by when it was read: at the process's start, in a
  census, copied from its parent, or after it exited.
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
  heartbeat's age, running since, restarts, events lost in the last finished run, reasons and
  kernel limits. When a row is opened, it also shows every run (started, stopped, ran for, why,
  records, events lost, cut short, pid and kernel), the probes loaded and not loaded, the sysctls,
  the files of each tier, and the heartbeats read.
- **Records:** `meta`, `stop`, `residency_totals`: `collector_pid`, `_ts_epoch`, `interval_s`;
  `meta`'s `kernel`, `bcc`, `features`, `features_rejected`, `sysctl`, `euid`, `schema_version`;
  `stop`'s `reason`, `uptime_s`, `records`, `truncated`, `perf_lost`, `perf_seen`. Also the FEEDS
  report's file list (size, mtime, host) of the eBPF and node tiers.
- **Computation:** `CollectorHealth.feed` runs in the live dispatch, so it covers the backfill's
  span and the tail. It keeps up to 20 runs per host (by collector pid) and the host's newest
  heartbeat. `report` judges each host against `feeds.ebpf.max_age_s` (300 s) and a loss of 0.1%
  of kernel events (`LOST_WARN_PCT`), and lists the worst first.
- **Cost:** O(1) per `meta`, `stop` or heartbeat; per read, hosts times runs. While the tab is
  shown, it is read again every 20 s.

#### FD-2 feeds, and the history store

- **Shows:** for every feed (eBPF, node tier, `sacct`, `sacctmgr`, domains CSV, user CSV): status,
  the newest record and its age against the feed's limit, where it was read from and where else
  the service looked for it, files, bytes, days and hosts. For the eBPF tier, it also shows the
  history store's replicas and their size, the conversion's progress, and how many selective reads
  the replicas answered ([History in Parquet](../../dashboard/README.md#history-in-parquet)).
- **Records:** for the eBPF and node tiers, the newest file's last record. For the other feeds, no
  records, only their files' names, sizes and times.
- **Computation:** `resolve`. The dated tiers glob `feeds.<feed>.days` dated directories (8 for
  the eBPF tier, 2 for the node tier) and read the newest file's last record
  (`newest_record_ts`). `sacct` and `sacctmgr` glob their patterns, and the CSVs stat their file.
  `refresh_feeds` adds the eBPF tier's record counts, schema versions, exclusions, backfill, tail
  counters, synthetic hosts and the store's status.
- **Cost:** per read, a glob, a stat per file and a tail read per dated feed (SH-4).

#### FD-3 startup read

- **Shows:** the backfill's state, and the lines that did not parse. The state is one of: finding
  its place in each file; reading, with a percentage and the records so far; done (records, bytes,
  time, how far back the history reaches); skipped; or failed.
- **Computation:** `Aggregator.backfill`, updated one batch at a time. `retained_from` is the
  oldest live bin.

#### FD-4 configuration

- **Shows:** every resolved setting, its value, and where it came from (flag, environment, file,
  default). The password hash is hidden.
- **Computation:** `config.py:Config.as_dict(with_origins=True)`. No records.

## Served, not drawn

This section lists what the service computes and serves but no figure on `develop` draws. Each of
these still costs its reducer's work, and some of them reach Ask LLM's snapshot or a panel's basis
note.

- **`panels.node`** (NODE, `reducers/resources.py:NodeReducer.result`): per host, the load, memory,
  CPU idle and iowait, process states, responsiveness probes, logged-in users and open files; per
  user, the cgroup CPU used between the two newest snapshots, its quota, throttling, memory and
  pids; and NFS mounts and disk space. It is made every rebuild tick and served in every
  `/api/panels` response. The login nodes table's `load₁` and `CPU` columns (LV-23) read the live
  tier's host rows instead, where those values are always null.
- **`workspace`**: the workspace reducer's own payload is its buffer counters. The per-agent views
  that the page draws are in `cascades.workspace` (AG-4).
- **`cascades.edges`, `edge_via`, `role_census`**: who starts whom, how each parent was found, and
  every classifier-tagged process counted by agent type and role.
- **`trajectories`' `drift`** (cwd drift and the purpose mix's Jensen-Shannon divergence, per row),
  `dominant_user_share_pct`, `by_key_type`, `by_class_counts`, `fold` and `buffer`.
- **`sandbox.agent_only` and `sandbox_detail`**: only in IN-6's basis note.
- **The I/O plates and the "Requested bytes against disk bytes" panel** of `RiskIoTab`: the
  Resources tab passes `plates={false}` and `series={false}`, so they are not drawn. Their numbers
  appear in LV-8's rows and LV-16's charts.

## What is slow

These are the expensive operations, most expensive first. The ranking is based on what the repo
measured and, where nothing was measured, on the work the code does.

1. **Reading a window from the day-files.** A scan, or the first reduction of a window's hours,
   parses every line in the window except `meta` and `stop` (`windows.EVENTS`). That costs about
   25 µs per record through json and the normalizer, and each record is then fed to about eleven
   reducers. Measured on one login node: ~15 s for 24 h and ~160 s for 7 d when scanned (README).
   FAS's eight nodes write 21.6 M records a day. Merged windows pay this cost only once per hour,
   ever, but some builds still read the window: the first fill of the hours, the 1 h view
   (always scanned), every view filtered by user or agent type (always scanned, though the byte
   check and the replicas skip what the filter rejects), and the drift rebuilds of those last two.
   Every BUILD figure waits on this read, and the page shows the saved panels in the meantime.
2. **Re-reading a window per click or query.** IOCMDS (SH-11, and every I/O number) reads the
   window's bytes for each new lookup. That takes about a minute per day of the FAS capture, read
   from the JSONL unless the lookup names a user, a host, a directory or agent pids. EVENTS reads
   the planned files' ranges from the JSONL unless the filter names a column that a replica can
   test, and it inflates each `.gz` whole to index it. One user's exits over a week took
   222-444 s from the `.gz` and 13.6-15.4 s from replicas. An export streams every match in its
   range, up to 5,000,000 rows.
3. **The backfill.** Finding the read's starting place in every file inflates every sealed `.gz`
   to index it. That took 4 minutes for a week of 8 login nodes. The reading itself takes 35-45 s
   per node-week. Window builds wait for the placing to finish, and the inflater indexes are
   rebuilt on every restart.
4. **The session inspector's read** (SE-5, SE-7, SE-9): up to 150,000 records (600,000 newest
   first) or 6 GiB per open, one read at a time, so a second reader has to wait. Its records are
   built in 8 processes. On FAS, a 150,000-record read takes 2.5-2.7 s, and a newest-first read of
   the same size takes 5.3-5.5 s, most of it spent by the tree keeping the newest records. A sealed
   `.gz` with no replica, and every sealed day of a terminal's or a login scope's session, is still
   searched in the service's process. That takes 30-43 s for a session in FAS's busiest sealed
   day-file, compared with 2.8 s from its replica in one process.
5. **Folding and ranking the sessions on read** (SE-1 to SE-3, LV-5, US-*, AG-2). Whenever a
   record has arrived since the last fold, the fold of every held key runs under the aggregator's
   lock, which the tail and every reader share. The fold takes "half a second to most of one" at
   24 h on rtx6000a, and the Sessions list asks for it every 5 s. A running session's phase block
   is made again on each read: `segment` takes up to 1.66-1.84 s for one session at the 20,000-call
   cap in its worst shape. The cascades (each rebuild tick) and the Users roster (every 5-60 s)
   read the same fold, and make it again if a record has arrived since.
6. **The Users tab's composition** (US-*): a roster walks every session's call log, and its inputs
   walk every tool call of the window. A portrait makes up to 80 phase blocks. A roster took about
   0.3 s on 500 synthetic sessions with 100,000 calls. This has not been measured on a real feed.
7. **Memory, and feeding every view.** Every live record is reduced once per held view (5 by
   default, plus filtered ones). The trajectories hold ~6 KB per collector key, up to 150,000 keys,
   which is ~950 MB per view. A held 7 d window of a busy node takes up to ~1 GB. These have no
   bound within the window: the sandbox's tool calls, the tool mix's and I/O's per-tool and
   per-user maps, the resources' CPU-to-run ratios, the endpoints' addresses, and the live tier's
   submissions.
8. **Making and serving the panels.** Every held view's payload is made again on each rebuild
   tick, one reducer per lock hold. Each `/api/panels` read copies the payload, and names and ranks
   the endpoints for every class combination (RE-7). A panels payload of 1.8 MB held the event loop
   for a quarter of a second, before serializing was moved to the request's thread (`app.py`). It
   has not been measured since.
9. **The live frame** (LV-1 to LV-3, LV-7, LV-19 to LV-23): one frame per distinct window for each
   poll that brought records. Making it is cheap, because a bin's rows are cached until a record
   lands in the bin, and the roster sums the closed bins only once. But it holds the aggregator's
   lock while it makes its payload.
