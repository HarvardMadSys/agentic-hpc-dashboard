# What each figure computes

Every chart, table, plate, pill, ranking and list the dashboard page draws, and the operation on
the data that produces it: which records it reads, by which read path, which reducer folds them
and which method renders its payload, what that costs, and where the code is. It is written for
the dashboard's maintainers and describes `develop` at `ce38ed3` (2026-11-20).

The machinery named here (the live tier, window builds and their saved copies, the hourly
merge-reduce, the history store) is explained in [`dashboard/README.md`](../../dashboard/README.md),
which also says what each panel means. What each tab is for is in
[`dashboard/web/README.md`](../../dashboard/web/README.md#tabs).

- **Code pointers** are `file:function`, under `dashboard/rc_dashboard/` (service) or
  `dashboard/web/src/` (page), the prefix left off.
- **Figures** are numbered by tab, in page order (`LV-7` is the Live tab's seventh). Each tab has a
  table of its figures (read path, payload, code), then a note per figure: what it shows, the
  records and fields it reads, the computation, the cost.
- **Cost** is per record (as a reducer is fed) and per read (making the payload), with the bounds
  that apply: a setting's default (`config.py:defaults`, else the module that reads it) or the
  module constant named. A measured number says where it was measured; "not measured" means there
  is none.
- **Records** are the eBPF tier's unless a note says otherwise. Every path parses
  (`fastjson.loads`) and normalizes (`normalize.py:Normalizer.normalize`) a record before a reducer
  sees it, about 25 µs a record (`proctree.py`, `DEFAULTS`). The normalizer derives the keys the
  reducers read: `_a3` (the class), `_command`, `_eff` (the effective command), `_purpose`, `_sid`,
  `_kt` and `_kv` (the session key), `_role`, `_sandbox`, `_approval`, `_surface`, `_ts_epoch`.

## Where the records come from

- **The eBPF tier** (`feeds.ebpf`): `exit`, `truncated`, `residency`, `residency_totals`, `conn`,
  `netio`, `submit`, `meta`, `stop`. Every figure reads only this tier, but for the Feeds tab's and
  the three that read the node tier (LV-1, LV-10, RE-1).
- **The node tier** (`feeds.ebpf_node`): a host's snapshot a line. Its newest snapshots (path NODE)
  are served as `panels.node`, which RE-1 draws and LV-1 reads; its history over the window (path
  NODEHIST), as `panels.node.series`, which LV-10 draws.
- **Slurm**: no scheduler accounting (`sacct`, `sacctmgr`) is read. The Slurm figures read the
  eBPF tier's `submit` records (one per `sbatch`, `srun` or `salloc`) and the `exit` records of
  scheduler commands run on the login nodes (`squeue`, `sacct`, `scontrol`, `scancel`, ...).

Scale: FAS RC's eight login nodes wrote 21.6 M eBPF records on 2026-11-15: 17.1 M `exit`, 2.1 M
`conn`, 1.7 M `netio`, 0.66 M `residency`, 18 k `submit`, 11.5 k `residency_totals`
([docs/feed-parquet](../feed-parquet/README.md#what-the-fas-feed-looks-like)).

## Read paths

| path | what reads the records | when | sealed days from the Parquet replicas |
|---|---|---|---|
| LIVE | the live tier, `reducers/live.py:LiveReducer`, and the collectors' health, `health.py:CollectorHealth`, fed by the tail and the startup backfill | the tail every `ingest.poll_ms` (1 s); the backfill once per start | no |
| BUILD | a window view's reducers, `windows.py:PanelView`: built once per window and filter, then fed live | built on first request, at start for the default and prewarm windows, and on drift; its payload made again every rebuild tick | merge: no. scan: only the time search that places the window's start. filtered: yes |
| INSPECT | the session inspector's re-read of one session, `proctree.py:inspect` | on opening a session | yes for a session keyed on pids; no for a terminal's or a login scope's |
| EVENTS | the events scan, `export.py:scan`, behind `/api/events` and `/api/export` | per request | yes when the filter names `user`, `host`, `agent_type`, `exit_code`, `signal` or `session_key` |
| IOCMDS | the lookup of the commands behind a clicked I/O number, `iocmds.py:IoCommands` | per click, as a polled job | yes when the click names a user, a host, a directory or agent pids |
| NODE | the node tier's newest snapshots, `aggregator.py:Aggregator.read_node_tier` | at start and every rebuild tick | no: the node tier is never sealed |
| NODEHIST | the node tier over time, `nodehist.py:NodeHistory`, on a thread of its own | a pass every 60 s (`IDLE_S`), 4 MB a read (`CHUNK`); binned to the window on every `/api/panels` read | no: the node tier is never sealed |
| FEEDS | the feed reports, `feeds.py:resolve` | per `/api/feeds` request, and every 30 s for the `feeds` frame | no |
| CONFIG | the resolved configuration, `/api/config` | once per page load | no records read |
| ASK | an agent CLI run over a snapshot of BUILD and LIVE payloads, `ask.py` | per question | through BUILD |

### LIVE: the live tier

One `LiveReducer`, sized at `live.retention_min` (7 d), holds
([The time window](../../dashboard/README.md#the-time-window),
[Ingestion, precisely](../../dashboard/README.md#ingestion-precisely)):

- per-minute bins by class (`buckets.py:RollingBuckets`), of which any window is an exact slice
  (10,080 bins at 7 d);
- a per-account roster (`roster.py:UserRoster`): 10-minute bins for the last day and hourly bins
  beyond, each a flat list per account of (class and agent type, processes, CPU, bytes);
- each host's newest `residency_totals`, each agent or editor tree's newest `residency` tick, and
  the agent session roots;
- tails of 500 exits (`live.event_tail`), 50 submissions (`live.submit_tail`) and 100 exits per
  account (`live.user_tail`), and every submission in retention (`submits_in_window`).

The tail feeds it every second (`Aggregator.poll_once`); on every start the backfill reads
`min(live.backfill_hours, retention)` of history into it, newest first (`Aggregator.backfill_now`).
The tail's records also go to each held view (BUILD); the backfill's do not, since a view reads its
own window from the files. `CollectorHealth` is fed in the same dispatch. The tier is served by
`/api/live`, `/api/live/users`, `/api/live/user` and the websocket's `live` frame: one frame per
distinct window, made after each poll that brought records and reused for a second
(`Aggregator.live_frame`). Without a socket the page polls `/api/live` every 15 s
([Live transport](../../dashboard/web/README.md#live-transport)). The service does not narrow the
tier by the page's user or agent-type filter (`windows.py:ViewFilter.NOT_NARROWED`).

Replicas: no. The backfill places its start in each file by a time search of the `.gz` and reads
the JSONL; the tail reads today's plain files.

Measured: a week of one login node (4 GB, 2.4 M records) backfills in 35-45 s (README). Placing
the read in a week of 8 login nodes took 4 minutes on rtx6000b, as it inflates each sealed day's
`.gz` to index it (`aggregator.py:Aggregator.backfill_now`). Holding the lock for 256 records at a
time (`BACKFILL_BATCH`) kept `/api/live` at p90 ~10 ms while a build ran (`aggregator.py`).

### BUILD: a window view

A view is one set of the historical reducers for one window and one page filter
(`windows.py:PanelView.fresh_reducers`). Views are kept in an LRU of `panels.max_windows` (5) and
built one at a time on one worker thread (`WindowRegistry._build`). The default window (24 h) and
the prewarm windows (`panels.prewarm_min`: 1 h, 6 h, 12 h) are built at start, once the backfill
has placed its read in every file. A built view is fed every live record, so each live record is
reduced once per held view. How views are built, saved and resumed:
[The time window](../../dashboard/README.md#the-time-window) and
[Hourly merge-reduce](../../dashboard/README.md#hourly-merge-reduce).

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
every line it reads: the byte check on the event kind (`export.py:event_filter`) skips little.

Every sum below counts a process once, at its `exit` (a `truncated` record may name a running root,
never add to a sum), and takes disk and syscall bytes as each process's own: each reducer that sums
bytes takes a parent's reaped children back off at its exit (`reducers/own_io.py:OwnIo`, at most
`reducers.own_io.max_parents` parents held, the rest counted in `own_io.evicted`). The hours work
that figure out once (`own_io.py:Ledger`, the record's `_own`), and a read that misses exits puts it
back on those of the closed hours it reads (`hours.py:OwnFigures`)
([Conventions the panels obey](../../dashboard/README.md#conventions-the-panels-obey)).

A view is built one of three ways:

- **merge**: an unfiltered view with a closed hour inside it (`hours.py:build`): the edge up to
  its first whole hour and the open hour are read, the closed hours merged from their saved states
  (`HourStore.load_part`; an hour not yet saved is reduced first, once ever, `HourStore.ensure`).
  Replicas: no.
- **scan**: an unfiltered view with no closed hour inside it, or any view with
  `panels.merge_reduce` off (`export.py:window_plan`, `window_scan`): each day-file's in-window byte
  range, k-way merged oldest first. The 1 h view is always scanned: an hour closes
  `panels.hour_grace_s` (300 s) after it ends, so 60 minutes never hold a closed one. A scan resumes
  from a saved build when one fits (`snapshots.py`, `WindowRegistry._resume`). Replicas: only for
  the time search that places the window's start in a sealed file (`export.py:_place`).
- **filtered**: a view narrowed by the page's user or agent-type filter, always scanned, saved and
  resumed like a scan. Replicas: yes: the filter is a `where` (`ViewFilter.where`, `where.py`) a
  replica tests on its columns before it builds a record, and in a JSONL file a byte check
  (`ViewFilter.line_match`) before a line is parsed. Its exits of closed hours take the hours' own
  figures (`WindowRegistry._own_figures`); those of the open hour, or fed live, are joined over what
  the filter keeps (`cache.own_io`) until that hour closes, and the view is then built again at the
  next rebuild tick (`PanelView.own_open_hour`), never resumed from a build saved before.

A view's payload (each reducer's `result()`, credentials masked) is made after its build and on
every rebuild tick (`rebuild.interval_s`, 900 s; `WindowRegistry.refresh`). The tick instead builds
the view again once its drift passes `panels.max_drift_pct` (25%) of its window, or once a filtered
view's open hour has closed: by a merge where it can, else a scan, never a resume. So most BUILD
figures move every 15 minutes; the page re-reads `/api/panels` on the `rebuilt` frame and when a
build lands, and while a view builds it shows the panels its window last saved (`payloads.py`).
Four are composed on read instead:

- `trajectories`, made again at most every `reducers.trajectories.live_s` (5 s) once the view has
  ingested (`Aggregator.trajectories`, `PanelView.materialize_key`);
- the Users tab, from the view's reducers (`portraits.py`), held up to 60 s (`Aggregator._held`);
- `trend` (LV-7), from the live tier on each `/api/panels` read, reused for 20 s
  (`LiveReducer.DAILY_REUSE_S`);
- `node.series` (LV-10), binned from NODEHIST's snapshots on each read.

The first two run under the aggregator's lock, which the tail and every reader share.

Measured on one login node's feed (README): merged, a 24 h window builds in ~2-3 s and a 7 d one in
~8 s, against ~15 s and ~160 s scanned, and filling the hours the first time costs about one scan;
resumed from a saved build, ~0.2 s and ~1.4 s against ~7 s and ~42 s. A held 7 d window of a busy
node is up to ~1 GB, most of it the trajectories' session keys (~6 KB each, ~950 MB at the
150,000-key cap). Saved hours take ~38 MB a week on disk, a saved build ~12 MB per day of window.

### INSPECT: the session inspector's re-read

`/api/trajectory` (`proctree.py:serve`, `inspect`, `_inspect`) re-reads one row's records from the
day-files of the row's host, from the session's first record, for exactly the collector keys the
row folds, through the window's unfiltered view whatever the page's filter. A session keyed on
agent pids is found by searching the bytes for its keys (`key_scan`, over a memory map, 32 MB at a
time), or in a sealed file's replica by testing its `session_key` and `agent_pid` columns
(`key_where`); a terminal's or a login scope's key is searched for in the inflated `.gz`. Except in
a sealed `.gz` with no usable replica, which the service's own process searches, the records found
are built in `inspect_workers` processes (8, at most the CPUs the service may use;
`keysearch.py:stream`), each holding only the 28 fields the tree reads (`keysearch.py:INSPECT_KEYS`)
([API](../../dashboard/README.md#api), "How a read is made").

Bounds, each reported in the response: `inspect_max_records` 150,000 (600,000 newest first),
`inspect_max_bytes` 6 GiB, `inspect_hold` 20,000 processes itemised, `inspect_per_parent` 2,000
childless processes of one command per parent, and `inspect_nodes` 6,000 entries served. One read
runs at a time. A built tree is served again for 600 s (16 trees, 96 MB in all), keyed on the
view's install time. While a session has been opened in the last 30 minutes and no window is
building, a background thread keeps the top 3 sessions of the `recent` ranking read, giving way to
a reader (`_warm_loop`).

Measured: the key search read one 11,000-line Claude session from a 627 MB day-file in 0.8 s,
against 2.9 s line by line (`proctree.py:key_scan`). In FAS's busiest sealed day-file (17.4 GB
inflated) it took 30-43 s from the `.gz` and 2.8 s from its replica
([History in Parquet](../../dashboard/README.md#history-in-parquet)). In 8 processes, a
150,000-record read of a FAS agent session (boslogin06, 2026-11-16, its fold stale as on a live
window) takes 2.5-2.7 s against 9.9 s in one, and a newest-first read of 149,573 records
(boslogin05) 5.3-5.5 s against 12.1 s, most of it the tree keeping the newest.

### EVENTS: the events scan

`export.py:scan` plans every day-file under the roots and reads each one from its near time bound
(newest first reads backwards from `to`): it checks the event kind on the bytes, parses, normalizes
and tests each record (`make_predicate`), and k-way merges the files' streams on time. Paging is by
offset, so a deep page reads the earlier matches again. A `.gz` with no replica is inflated whole
the first time it is read, to index it. Replicas: when the filter names one of the record's own
fields (`where_of`: `user`, `host`, `agent_type`, `exit_code`, `signal`, `session_key`). Measured:
finding one user's exits on one sealed day, with the week's 128 day-files planned, took 222-444 s
from the `.gz` and 13.6-15.4 s from replicas
([History in Parquet](../../dashboard/README.md#history-in-parquet)).

### IOCMDS: the commands behind an I/O number

A click on an I/O number (`components/IoCmds.tsx:IoNum`) starts a job (`iocmds.py:IoCommands`) that
reads the window's `exit` records again, one file after another (`export.py:window_plan`, then
`_window_stream`), takes each process's reaped children off its bytes (`OwnIo`, over every record
it parses), keeps the records that pass the click's filters (`/api/events`' own, plus agent pids, a
host and a directory) and groups them by command line. A click carries its figure's filters, else
the page's user and agent-type filter (`components/IoCmds.tsx:params`). Two jobs read at once; six
finished ones are kept, each answering the same question again for 120 s on a live feed, or
indefinitely on a pinned clock. Replicas: when the lookup names a user, a host, a directory or agent
pids (`prefilter_where`); one narrowed only by class, tool, kind of tool, agent type or time reads
the JSONL. Measured (`iocmds.py`): about 3 s for a day of rtx6000a, about a minute for a day of the
FAS RC capture (6.2 GB of gzip, 2.8 M exits).

### NODE, FEEDS, CONFIG and ASK

- **NODE**: `Aggregator.read_node_tier` reads the last `feeds.ebpf_node.snapshots_per_host` (2)
  lines of each node-tier file, without reading the rest (`aggregator.py:last_lines`), into
  `reducers/resources.py:NodeReducer`.
- **FEEDS**: `feeds.py:resolve` globs each feed's paths and reads the newest file's last record
  (`newest_record_ts`); `Aggregator.refresh_feeds` adds the eBPF tier's counters, the backfill's
  state and the history store's (`history.py:HistoryStore.status`). `/api/collectors` adds the
  health the live tier folded (`CollectorHealth.report`).
- **CONFIG**: `/api/config` serves the resolved configuration with each key's origin.
- **ASK** (off unless `ask.enabled`; refused, reading nothing, while the service has no password,
  or while names are anonymized unless `ask.unmasked_answers` is set): `Aggregator.ask` builds a
  snapshot of the window's `/api/panels` payload, the live payload's `now`, `users`, `hosts` and
  `window`, and the ranked sessions -- or one panel's keys, or one session's process tree read by
  INSPECT -- and compacts it to `ask.context_kb` (120 KB): `ask.py:snapshot` cuts the largest
  section's lists and strings a step at a time (`STEPS`) and drops bulky keys such as series and
  tails (`DROP_KEYS`). It runs the configured agent CLI (`codex exec` by default) as the service's
  user, `ask.max_concurrent` (1) at a time, for at most `ask.timeout_s` (240 s), with a short
  environment (`ask.AGENT_ENV`) in a temporary directory of its own.

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

- **Shows:** `live`, what is wrong with the data, or how updates arrive.
- **Records:** none of its own: each required feed's status and newest record (FEEDS), the live
  frame's `clock`, the transport (`api/useLive.ts:useLive`).
- **Computation:** `headline` takes the worst status of the required feeds, one whose `lag_s` passes
  its `max_age_s` counting as `stale`; `pillOf` words it, the data's problem before the transport's;
  on a pinned clock, `frozen capture` and its `as of`.
- **Cost:** a FEEDS report.

#### SH-2 `anonymized` pill

- **Shows:** that names leave as aliases ([Anonymized
  names](../../dashboard/README.md#anonymized-names)).
- **Records, computation, cost:** none; `/api/config` says `anonymize.enabled`.

#### SH-3 `fake data` pill and note

- **Shows:** that some hosts' records were written by the demo, and which.
- **Records:** `meta`, `stop` and `residency_totals` carrying `"synthetic": true`.
- **Computation:** `CollectorHealth.feed` marks a host with such a record;
  `Aggregator.refresh_feeds` lists those hosts.
- **Cost:** O(1) per record, in the live dispatch.

#### SH-4 newest record's age

- **Shows:** the age of the newest record of the freshest required feed.
- **Records:** the last record (`ts`) of each feed's newest file.
- **Computation:** `newest_record_ts` reads the newest file's tail, widening the read until it holds
  a whole line; `lag_s` is the clock minus that record's time; the page shows the smallest.
- **Cost:** one tail read per feed per report; a capture's newest file is a `.gz`, read after its
  inflater index is built, once per process.

#### SH-5 feeds delayed

- **Shows:** how many required feeds are older than their own limit, each on hover.
- **Computation:** `lag_s` (SH-4) against `feeds.<feed>.max_age_s` (300 s for the eBPF tier, 900 s
  for the node tier).

#### SH-6 login nodes reporting

- **Shows:** how many login nodes reported, named on hover.
- **Records, computation, cost:** the live frame's `hosts`, as LV-21.

#### SH-7 window picker and its state

- **Shows:** the windows offered, the clamp, the build state, drift, and the backfill's progress
  ([The window control](../../dashboard/web/README.md#the-window-control)).
- **Records:** none of its own.
- **Computation:** `presets` (the configured presets retention can serve), `MIN_WINDOW_MIN` (the
  5-minute floor, offered when the presets start above it; `min_minutes` on `/api/windows` only),
  `resolve` (the clamp and its note), `PanelView.status` (state, progress by bytes or, merging, by
  hour-reads, drift, shown once past half its budget, cache), sent as `/api/windows` and as a
  `windows` frame when a material field changes (checked every 2 s, `app.py:windows_loop`);
  `retained_minutes` from the oldest live bin (`RollingBuckets.oldest_epoch`); the live frame's
  `backfill`.
- **Cost:** a status per held view per frame.

#### SH-8 user and agent-type choices

- **Shows:** the 50 users with the most exits and every agent type, each with its class mix.
- **Records:** `exit`: `user`, `agent_type`, `_a3`.
- **Computation:** `FacetReducer.feed` counts exits per user and per agent type, by class; `result`
  ranks them. Served only from the window's unfiltered view (`Aggregator.facets`), never a build of
  their own, so they stay put while a filtered view builds.
- **Cost:** two dict updates per record; the user map holds every user; one sort per read.

#### SH-9 the rail's foot

- **Shows:** the transport, the last update, the window, and when the panels were built.
- **Computation:** `full_built_at` is the view's `built_at`, else the node tier's; `restored` means
  the view's `cache.source` is `snapshot`.

#### SH-10 classes shown

- **Shows:** how many of the three class chips are on, from the URL's state, not from any data.

#### SH-11 the commands behind an I/O number

- **Shows:** every command line behind the clicked number, with its processes, bytes and CPU.
- **Records:** `exit`: `args` (else `_command`), `_eff`, `io.rd_mb`, `io.wr_mb`, `io.rchar_mb`,
  `io.wchar_mb` (each process's own: `pid`, `ppid`, `host`, `io.scope` for the join),
  `net_tx_bytes`, `net_rx_bytes`, `cpu_s`, `user`, `_ts_epoch`, and the fields the click filters on
  (`_a3`, `agent_type`, `_eff` or its bucket, `session_key` and `agent_pid`, `host`, `cwd`, and for
  an `outcome`, `exit_code` and `signal`).
- **Computation:** IOCMDS. Per record, a byte check (the event kind, then user, agent pids,
  directory, host), a parse, `iocmds.py:predicate`, and an add into its command line's group (up to
  1,000,000 groups, then the totals only). A blast-radius click's agent pids are widened to every
  tool-call key the window's process index walks up to them (`agent_keys`). The page polls every 600
  ms; `Job.page` sorts the groups for each page of 200 rows (up to 1,000).
- **Cost:** a read of the window's bytes per lookup, whatever it matches (IOCMDS).

## Live

| | figure | path | payload | code (service · page) |
|---|---|---|---|---|
| LV-1 | list: needs attention | FEEDS, BUILD, NODE | the `feeds` report; `risk`, `slurm`, `node` | (no reducer of its own) · `tabs/Attention.tsx:Attention` |
| LV-2 | plate: agent processes exited | BUILD | `tool_mix.by_class[c].calls`, `tool_mix.by_type` | `reducers/tools.py:ToolMixReducer._rows`, `_by_type` · `components/WindowPlates.tsx:WindowPlates` |
| LV-3 | plate: agent sessions | BUILD (on read) | `trajectories.summary`; `sandbox.approval_by_class_sessions`, `sandbox.by_class_sessions` | `TrajectoryReducer.summary`; `SandboxReducer.result` · `components/WindowPlates.tsx:WindowPlates` |
| LV-4 | plate: risky agent actions | BUILD | `risk.dangerous.rows`, `risk.secrets.rows`; `sandbox.tool_calls` | `reducers/risk.py:RiskReducer.result` · `components/WindowPlates.tsx:WindowPlates` |
| LV-5 | chart: processes exited per minute | LIVE | `rate` | `buckets.py:RollingBuckets.series` · `tabs/LiveTab.tsx:LiveTab`, `charts/primitives.tsx:Lines` |
| LV-6 | table: who uses what | BUILD | `tool_mix`, `io_process`, `resources`, `trajectories.summary`, `slurm` | (no reducer of its own) · `tabs/OverviewTab.tsx:OverviewTab` |
| LV-7 | table: day by day | LIVE, over retention; composed on read | `trend.days` | `reducers/live.py:LiveReducer.daily` · `tabs/DayByDay.tsx:DayByDay` |
| LV-8 | chart: CPU seconds per minute | BUILD | `resources.series.by_class[c].cpu_s` | `ResourceReducer._series_add`, `series` · `tabs/LiveCharts.tsx:WindowCharts`, `charts/TimeChart.tsx:TimeChart` |
| LV-9 | chart: memory; table: peak and average | BUILD | `resources.series.resident[c].rss_mb` | `ResourceReducer._tick`, `series` · `tabs/LiveCharts.tsx:WindowCharts` |
| LV-10 | charts: login node response | NODEHIST | `node.series` | `nodehist.py:NodeHistory.series` · `tabs/NodeResponse.tsx:NodeResponse` |
| LV-11 | chart: runqueue wait; table: run and wait | BUILD | `series.by_class[c].sched_wait_s`; `by_class[c].sched_run_s`, `sched_wait_s`, `schedstat_ratio` | `ResourceReducer.feed`, `series`, `result` · `tabs/LiveCharts.tsx:WindowCharts` |
| LV-12 | chart: D-state by tool; table: every tool that stalled | BUILD | `series.stall_by_tool`, `series.stall_other`; `stall_by_tool` | `ResourceReducer._series_add`, `result` · `tabs/LiveCharts.tsx:WindowCharts` |
| LV-13 | chart: major page faults | BUILD | `series.by_class[c].maj_flt` | `ResourceReducer.series` · `tabs/LiveCharts.tsx:WindowCharts` |
| LV-14 | charts: disk read, disk written | BUILD; a point opens IOCMDS | `series.by_class[c]`: `rd_mb`, `rchar_mb`, `wr_mb`, `wchar_mb` | `ResourceReducer.series` · `tabs/LiveCharts.tsx:WindowCharts` |
| LV-15 | chart: network | BUILD; a point opens IOCMDS | `series.by_class[c]`: `net_tx_mb`, `net_rx_mb` | `ResourceReducer.series` · `tabs/LiveCharts.tsx:WindowCharts` |
| LV-16 | charts: Slurm queue checks, Slurm jobs submitted | BUILD | `slurm.series.by_class[c]`: `queries`, `jobs` | `reducers/slurm.py:SlurmReducer._bin`, `_series` · `tabs/LiveCharts.tsx:WindowCharts` |
| LV-17 | active users: running now (chips) | LIVE | `users.running_by_type` | `LiveReducer.users`, `_running` · `tabs/LiveUsers.tsx:Summary` |
| LV-18 | active users: one row per account (table) | LIVE; `/api/live/users` past the frame | `users.rows`, `users.rows_total` | `LiveReducer.users`, `_row` · `tabs/LiveUsers.tsx:UsersPanel` |
| LV-19 | active users: an account opened (three boxes) | LIVE (`/api/live/user`); BUILD for "started by" | `row`, `sessions`, `recent`; `cascades.trees` | `LiveReducer.user_detail` · `tabs/LiveUsers.tsx:Detail`, `nestedAgents` |
| LV-20 | active users: every process (exit stream) | LIVE | `events` | `LiveReducer._exit`, `result` · `tabs/LiveUsers.tsx:Stream` |
| LV-21 | table: login nodes right now | LIVE | `hosts` | `LiveReducer._totals`, `result` · `tabs/LiveTab.tsx:LiveTab`, `HostBreakdown` |

#### LV-1 needs attention

- **Shows:** what the service already flagged, a line per rule, each linking to its panel.
- **Records:** none of its own: the `feeds` report (FEEDS) and the window's `risk`, `slurm` and
  `node` panels (BUILD, NODE).
- **Computation:** in the page, with no threshold of its own: each line (a stale feed, risk hits,
  polling sessions, refusals, a full filesystem, a throttled account, NFS retransmissions) is a rule
  the service or another panel applies, so they cannot disagree; the agent class's count first. A
  source with nothing to flag is *checked*; an absent one is *not checked* and named, never an
  all-clear; with the agent class hidden, the agent rules say they were not applied.
- **Cost:** none past its payloads'.

#### LV-2 agent processes exited

- **Shows:** the agent processes that exited, the other classes beside them, the agent share.
- **Records:** `exit`: `_a3`, `agent_type`, `_eff`, `_command`, `_sid`, `pid`, `ppid`,
  `_shell_resolved`, `cpu_s`.
- **Computation:** `ToolMixReducer.feed` counts each exit under its class, an agent's also under its
  agent type, but for the shell-wrapper dedup: a shell whose effective command was resolved from its
  argv is dropped when its captured direct child, in the same session, resolved to the same tool (a
  `(session, parent pid)` index of the children's tools; a child exits before the shell that waited
  on it, so one pass in time order is exact). `_rows` totals each class's `calls`; `_by_type` splits
  a class by agent type. Every share on the page (`lib/shares.ts`) is out of every class the panel
  counted, `unlabeled` included, whatever the class chips show; a counted class whose value is null
  leaves the whole unknown, and the share is a dash that says why.
- **Cost:** a few counter adds and one index lookup per record; the index holds
  `reducers.tool_mix.dedup_sessions_max` (200,000) parents, oldest dropped first, carried from hour
  to hour when merging. Per read, sums over the 12 kinds of tool.

#### LV-3 agent sessions

- **Shows:** agent sessions (probes left out), and how they were launched and confined.
- **Records:** for sessions, `exit` (`_sid`, `_kt`, `_kv`, `_a3`, `_ts_epoch`) and each root's own
  record (role `agent`, `pid == agent_pid`); for the launch line, as SC-3 and SC-5.
- **Computation:** `TrajectoryReducer.summary` counts each folded session whose last record is in
  the window and, for agents, whose root was seen (`rooted`; an agent-class key with no root is a
  probe, counted apart). The launch line is `SandboxReducer.result`'s sessions by approval at launch
  and by their tool calls' verdict (`session_state`).
- **Cost:** the summary walks every session of the window each time `trajectories` is made (SE-2);
  the sandbox walks every tool call of the window each time its payload is made (SC-3).

#### LV-4 risky agent actions

- **Shows:** dangerous-command and credential matches by class; SC-2's sandbox line.
- **Records:** `exit`: `_eff` or `_command`, `args`, `_a3`, `user`, `host`, `agent_type`, `cpu_s`,
  `exit_code`, `signal`.
- **Computation:** `RiskReducer.feed` tests `"<tool> <args>"` against every rule a section shows
  (`_matches`: a literal gate, then the regex; the verdict cached per tool and argv); the plate sums
  the rule rows' `classes` over the commands and credentials sections. The sandbox line is SC-2's.
- **Cost:** a cache lookup a record and, on a miss, one regex search per rule whose gate passes. The
  cache holds 50,000 command lines and is cleared when full; uncached, matching was half of a window
  build (`risk.py`).

#### LV-5 processes exited per minute

- **Shows:** processes exited per minute of the window, a line per class.
- **Records:** `exit`: `_a3`, `_ts_epoch`.
- **Computation:** `LiveReducer._exit` adds each exit to its minute's bin by class
  (`RollingBuckets.add`). A minute no record landed in is null for every class; in a minute with
  records, a class with none is 0. `RollingBuckets.series` gives a row per minute, oldest first, made
  again only when a record landed in its bin since (`_rows`, keyed on each bin's version).
- **Cost:** a dict increment a record; per frame, one pass over the window's bins (10,080 at 7 d),
  most of them cached rows. A record stamped more than `live.clock_skew_s` (120 s) ahead is dropped
  and counted.

#### LV-6 who uses what

- **Shows:** each class's share of the window in eleven units, processes to GPU-hours asked.
- **Records:** those of its payloads: `exit` (processes, CPU; LV-2), each process's own `io` bytes
  and network bytes (`IoReducer`), `residency_totals` (memory; LV-9), sessions (LV-3), `submit`
  (`gpus`, `array`, `time_limit_s`; SL-4).
- **Computation:** none of its own. `OverviewTab` reads `tool_mix.by_class[c]` `calls` and `cpu_s`
  (after the shell-wrapper dedup), `io_process.by_class[c]` `rd_mb`, `wr_mb` and the
  `net_tx_mb`/`net_rx_mb` accumulators' totals, the highest bin of
  `resources.series.resident[c].rss_mb` (the largest process on hover, `resources.by_class[c].top`),
  `trajectories.summary[c].sessions`, and `slurm.by_class[c]` `jobs`, `gpus`, `gpu_h`. Shares follow
  LV-2's rule: a class none of whose processes carried network bytes leaves that row no share. A
  byte cell opens IOCMDS for its class.
- **Cost:** its reducers'.

#### LV-7 day by day

- **Shows:** each class's processes, CPU and users per local day, over retention
  ([Day by day](../../dashboard/README.md#day-by-day)).
- **Records:** the live tier's minute bins (`exit`: `_a3`, `cpu_s`) and its roster (`user`).
- **Computation:** `LiveReducer.daily` sums the bins of each local day and counts an account once a
  day in each class it ran something in. A day the collector wrote in has its numbers and `held`,
  the minutes that held a record, against `minutes`; a day it wrote nothing in has null counts, not
  zeros; the oldest day starts where retention does (`cut`). Not narrowed by the page filter.
- **Cost:** per read, the bins of retention per class; reused for `DAILY_REUSE_S` (20 s).

#### LV-8 CPU seconds per minute

- **Shows:** CPU seconds per minute by class, a process's whole-life CPU in its exit's bin.
- **Records:** `exit`: `_a3`, `_ts_epoch`, `cpu_s`.
- **Computation:** `ResourceReducer._series_add` counts the record in its bin by class and adds each
  of its ten series fields (CPU, runqueue wait, D-state wait, major faults, four `io` byte counts,
  network sent and received), counting the records that had each, so a bin where every record lacked
  the field is null. `series` lays the bins on one axis, null where a class has no record; the page
  divides by the bin's minutes. The width (`resources.py:bin_for`) is the first of 1, 2, 5, 10, 15,
  30 minutes, 1, 2, 3, 6, 12, 24 hours that keeps the window within 240 bins; a merged window folds
  the hours' 60 s bins into it (`ResourceReducer.merge`).
- **Cost:** about ten dict updates per record; per read, one pass over at most 240 bins (960 held,
  `SERIES_SPAN_CAP`, for a window that drifted or a skewed clock).

#### LV-9 memory, and peak and average

- **Shows:** resident memory by class over the window; each class's peak and average.
- **Records:** `residency_totals`: `host`, `_ts_epoch`, `_by_class` (`rss_mb`).
- **Computation:** `ResourceReducer._tick` sums each host's per-class `rss_mb` over its ticks in a
  bin; `series` takes each host's mean and sums the hosts; the page finds the peak and the mean.
  Empty in a filtered view: a tick names no user or agent type (`ViewFilter.NOT_NARROWED`).
- **Cost:** a few adds a tick; per read, bins times hosts.

#### LV-10 login node response

- **Shows:** per host, start and account-lookup times, the slowest NFS read, the load
  ([Over the window](../../dashboard/README.md#over-the-window)).
- **Records:** the node tier's snapshots: ten numbers of each (`nodehist.py:FIELDS`).
- **Computation:** `NodeHistory.series` bins them to the window: a bin is the window's own width or
  twice the snapshot interval, whichever is wider; a latency bin is the slowest probe in it, a load
  bin its mean; a missing snapshot is a hole. Past five hosts, the page names the five that stand out
  most and draws the rest as one grey line.
- **Cost:** NODEHIST reads the node tier's day-files once, then only what was appended; per
  `/api/panels` read, the binning of the window's snapshots.

#### LV-11 runqueue wait, and run and wait

- **Shows:** runqueue wait per minute by class; run and wait time, CPU-to-run ratio.
- **Records:** `exit`: `sched_wait_s`, `sched_run_s`, `cpu_s`.
- **Computation:** the series as LV-8; the table is an accumulator per class
  (`reducers/__init__.py:Acc`: sum, present count, coverage) of `sched_run_s` and `sched_wait_s`, and
  `schedstat_ratio`, the median of `cpu_s / sched_run_s` over the processes with more than 1 s of CPU.
- **Cost:** the ratio keeps one float per such process per class, unbounded, sorted at each read.

#### LV-12 D-state by tool, and every tool that stalled

- **Shows:** D-state seconds per minute for the six tools that stalled most; the top 20.
- **Records:** `exit`: `dstate_wait_s`, `dstate_episodes`, `dstate_max_s`, `_eff` or `_command`.
- **Computation:** `_series_add` adds each record's `dstate_wait_s` to its bin by tool; `feed` counts
  each tool's stalled processes, stall time, stalls and longest stall; `result` ranks the tools by
  time. `series` draws 0 for a tool in a bin where D-state was measured, null where it was not. Every
  class; the class chips do not narrow it.
- **Cost:** one dict add per stalled record; one sort over the tools per read.

#### LV-13 major page faults

- **Shows:** major page faults per minute by class: `maj_flt` summed by exit time, as LV-8.

#### LV-14 disk read, disk written

- **Shows:** per class and minute, bytes that reached the disk against those requested.
- **Records:** `exit`: `io.rd_mb`, `io.rchar_mb`, `io.wr_mb`, `io.wchar_mb`, each process's own
  (BUILD); null for another user's process, whose `/proc/<pid>/io` the collector could not read.
- **Computation:** as LV-8.

#### LV-15 network

- **Shows:** per class, bytes sent and received per minute.
- **Records:** `exit`: `net_tx_bytes`, `net_rx_bytes` (null where the collector kept no count).
- **Computation:** as LV-8, in MB.

#### LV-16 Slurm queue checks, Slurm jobs submitted

- **Shows:** per class, scheduler queries and jobs submitted per bin.
- **Records:** `submit` (an outcome of `job`); `exit` of a scheduler query command
  (`tool_buckets.SLURM_Q`: `squeue`, `sacct`, `scontrol`, `sinfo`, ...).
- **Computation:** `SlurmReducer._bin` counts each job and query in its bin by class, at LV-8's
  width. `_series` makes a bin null (a gap) when no class has a Slurm record in it; when another class
  has, a class with none gets 0.
- **Cost:** O(1) per record; per read, one pass over the bins.

#### LV-17 active users: running now

- **Shows:** per agent and IDE type, the accounts with such a session running now.
- **Records:** `residency` (`user`, `_a3`, `agent_type`, `_ts_epoch`).
- **Computation:** `LiveReducer._running` groups the trees that ticked within 600 s by account, class
  and type; `users` counts the accounts per type. Under a page filter, the page counts the rows it
  kept.

#### LV-18 active users: one row per account

- **Shows:** a row per account: running now, exited by class, CPU, bytes, GPU-hours.
- **Records:** `exit`: `user`, `_a3`, `agent_type`, `cpu_s`, `io.rd_mb`, `io.wr_mb` (each process's
  own, `LiveReducer._own_io`: the tail takes a parent's reaped children off at its exit; the backfill,
  newest first, meets the parent first and takes each child off as it comes), `net_tx_bytes`,
  `net_rx_bytes`, `_ts_epoch`; `residency` for what runs now; `submit` (`gpus`, `time_limit_s`,
  `user`, `_ts_epoch`) for GPU-hours.
- **Computation:** `UserRoster.add` puts each exit in its account's slot of a 10-minute bin (hourly
  past a day); `UserRoster.totals` sums the bins the window overlaps (the closed ones cached per
  window, made again when a bin closes, the window's edge crosses one, or at most every 15 s for a
  late record) and adds the open bin. The edge lands on a bin boundary, so a row counts up to one
  bin more than the window (a 5-minute window reads 5 to 15 minutes). `LiveReducer.users` orders the
  accounts (agents running now, then most processes, then name) and makes full rows for the first
  `live.user_rows` (100) (`_row`: the slots decoded, the trees running now and their census's
  `n_procs`, GPUs times time limit over the window's submissions that named both). A search, a
  running-now chip or a page filter, when the frame does not hold every account, asks
  `/api/live/users`, which searches them all; the page filters rows by user and agent type
  (`pageMatch`). Columns default to `ui.user_columns`. A byte cell opens IOCMDS for the account.
- **Cost:** per record, a walk of the account's slot; per frame, a pass over every account and 100
  full rows. Not measured.

#### LV-19 active users: an account opened

- **Shows:** the account's sessions running now, what it ran in the window, its last exits.
- **Records:** `residency` (`tree_root_pid` or `agent_pid`, `host`, `user`, `_a3`, `agent_type`,
  `_ts_epoch`) for the sessions running now; the agent roots, any record of a process whose role is
  `agent` with `pid == agent_pid` (`_approval`), for the launch badges; the roster's records, as
  LV-18; the account's own exit tail.
- **Computation:** `/api/live/user` calls `LiveReducer.user_detail`: the account's row, its trees
  ticking within 600 s with the agent roots inside each (`_inner`, `_fronted`), and its own tail
  (`user_tails`, newest first, 100 deep, masked when served). "Started by" is the page's, from the
  window's agent trees (`cascades.trees`, BUILD): per child agent type, the processes of the agents a
  parent agent type started (`nestedAgents`).
- **Cost:** per open, a roster read and a pass over the live trees and roots; fetched again when the
  count asked for or the window changes.

#### LV-20 active users: every process

- **Shows:** the newest exited processes of every user and host (the last 10, 25 or 100).
- **Records:** `exit`: time, host, user, class, agent type, command and argv, purpose, depth, pid,
  duration, CPU, peak memory, disk bytes (its own, as LV-18), network bytes, exit code, signal,
  sandbox, approval, session key.
- **Computation:** `LiveReducer._exit` keeps a deque of `live.event_tail` (500) rows, newest first;
  `result` cuts it to the window and masks each row's argv once (`_served`); the page filters and
  sorts.
- **Cost:** one row dict per record, not made during the backfill once the tails are full. A tail,
  not an aggregate: a wider window does not make it deeper.

#### LV-21 login nodes right now

- **Shows:** per login node: sessions, processes, memory, D-state, users, external TCP.
- **Records:** `residency_totals`: `host`, `trees`, `_by_class` (`n_procs`, `threads`, `rss_mb`,
  `d_state`, `users`), `by_agent_type`, `_tcp_by_class` (`open`, `ext`, `estab`); a host with only
  `residency` gets a row that says so.
- **Computation:** `_totals` keeps each host's newest tick; `result` sums the per-class fields per
  host; `users` adds the collector's per-class counts, so one person in two classes counts twice.
- **Cost:** per frame, one pass over the hosts.

## Users

See [Users: a portrait of each user](../../dashboard/README.md#users-a-portrait-of-each-user).
The tab is composed on read (BUILD on read) by `portraits.py`, for the page's filter, from the
view's `trajectories`, `sandbox` and `slurm` reducers and the live tier's agent roots for what is
running now (`Aggregator._running_fn`); while it is shown, `/api/users` and `/api/user` are read
again every 15 s. A session is one agent run: its rows across days, keyed by (host, agent pid,
user) (`portraits.agent_sessions`, `trajectories._agent_key`), with a root.

Common cost: `Aggregator._held` keeps a view's inputs (`portraits.inputs`: the sandbox's tool-call
verdict per agent session, which walks every tool call of the window, and `slurm_by_user`, which
folds every Slurm key), its roster and each portrait opened, and makes them again, under the
aggregator's lock, once the view has ingested and 5 s have passed (the roster and portraits also
after 60 s). A roster groups every folded trajectory row by agent and walks each session's call
log. Measured on 500 synthetic sessions with 100,000 calls: a roster about 0.3 s, a portrait read
from the roster's inputs about 20 ms (README).

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

- **Shows:** agent users (and running now), sessions with median length and calls, bypass and script
  shares.
- **Records:** the trajectories' agent sessions (`exit`: `_sid`, `_kt`, `_kv`, `user` or `uid`,
  `_ts_epoch`; the call log from `args`), each root's own record (`_approval`, `_surface`), and the
  live tier's roots.
- **Computation:** `roster` builds every user's row (US-3); `_fleet` counts users and sessions,
  takes the medians (`_q`, by sorting) and totals approval and surface, each session's taken from
  its root's own record (`TrajectoryReducer.surface_of`, `approval_of`).

#### US-2 trait chips

- **Shows:** each trait held in the window and how many users hold it; a chip narrows the roster.
- **Computation:** `traits` runs per user, each trait one rule (`portraits.RULES`) applied to the
  row's numbers; `_fleet` counts them.

#### US-3 roster

- **Shows:** a row per user: sessions, launch, calls by category, rhythm, traits, how they leverage
  agents.
- **Records:** as US-1, plus each session's call log (a call's category, start and tag set), the
  sandbox's tool-call verdicts, and the Slurm keys.
- **Computation:** `_user_row` runs per user. It counts sessions by agent type, surface, approval and
  tool-call verdict; calls (null when no session's calls are recognized); categories over the call
  logs; active minutes (the distinct minutes in which a call began) by local hour (`_Clock`); finds
  `peak_open` by a sweep over the sessions' first and last records (`_peak_open`), the 5 most
  frequent projects (`TrajectoryReducer.project_of`), the traits, and `leverage`, by `RULES`. Rows
  are sorted by sessions, then calls.
- **Cost:** one pass over every session's call log per roster.

#### US-4 figures

- **Shows:** each user drawn from their row, a part of the figure per number.
- **Computation:** in the page, from US-3's row; the only numbers it adds are drawing caps (six
  robots, five sheets, ten lights).

#### US-5 portrait: head

- **Shows:** the user's figure, leverage, traits, and totals, against the window's medians.
- **Records:** as US-3, for one user; the footprint from the trajectory rows (`n`, CPU, each
  process's own disk bytes, network bytes, the largest peak memory).
- **Computation:** `portrait` makes the user's row with a weekday grid (`_user_row(grid=True)`) and
  compares it with the roster's `fleet`: the unfiltered view's when that view is held, else the
  filtered view's (`fleet_of`).

#### US-6 portrait: how they launch agents

- **Shows:** four share rows (agent, driven from, approval, sandboxed) against the window's.
- **Computation:** the row's counters against `fleet`'s, divided in the page over the known states.

#### US-7 portrait: rhythm

- **Shows:** session lengths, the most open at once, active minutes by hour, a session timeline.
- **Computation:** the row's `wall_p50_s`, `wall_max_s`, `peak_open`, `active_min`, `hours` and
  `grid`; the timeline shows the 60 newest sessions (`SESSIONS_SHOWN`), packed by the page into at
  most 12 rows.

#### US-8 portrait: what their agents do

- **Shows:** tool calls by category, time by phase, and the commands their agents ran most.
- **Records:** the call logs, and the phase blocks of the user's newest sessions.
- **Computation:** `row.cats`; `phases.time_s`, summed over the phase blocks of the newest
  `PHASE_SESSIONS` (80) sessions (`TrajectoryReducer.phase_block`, SE-3); and the 8 most frequent
  masked previews among the user's calls.
- **Cost:** up to 80 phase blocks per portrait, each made as SE-3 says and kept in the reducer's
  block cache (4,096 blocks).

#### US-9 portrait: projects and tasks

- **Shows:** the projects their sessions worked in, their tasks, and the newest 40 tasks.
- **Computation:** `row.projects` (the top 5 and a total), and `tasks` over US-8's 80 blocks.

#### US-10 portrait: at the scheduler

- **Shows:** what the user's agent sessions asked of Slurm, as SL-3 counts it.
- **Records:** as SL-3.
- **Computation:** `slurm_by_user` folds the Slurm reducer's agent keys into sessions
  (`SlurmReducer._sessions`) and reads each one's polling with `SlurmReducer._polls`; agent sessions
  only.

#### US-11 portrait: sessions

- **Shows:** the newest 60 sessions, each with its launch, category, phases, calls and CPU.
- **Computation:** each session's sums (`portraits._session`) and, where it has a block, the
  block's `headline`, `category`, tasks and phase times; the 60 are among US-8's 80.

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
| AG-10 | commands that failed | BUILD; a count opens IOCMDS | `tool_mix.exits` | `reducers/tools.py:ToolMixReducer._exits` · `tabs/ExitsPanel.tsx:ExitsPanel` |

#### AG-1 agents by type

- **Shows:** per agent type: agents, their processes and CPU, nesting, writes outside, other homes.
- **Records:** the node records, any record (`exit`, `truncated`, `residency`, `conn`, `netio`,
  `submit`) of a process whose role is `agent`, `infra` or `client` (`pid`, `ppid`, `_role`,
  `agent_type`, `user`, `_command`, `args`, `cwd`, `tree_root_pid`, `_approval`,
  `_approval_detail`, `_surface`, `duration_s`); the trajectories' folded sessions; the workspace's
  views (AG-4).
- **Computation:** in the page (`typeStats`), over the agent nodes of the trees sent: a node's
  processes are its folded session's `n` (`tool_calls` on the node), its CPU that session's.
- **Cost:** counted over the trees sent (at most `reducers.cascades.top_trees`, 1,000, deepest
  first); the strip says so when the list is cut.

#### AG-2 agents and their subagents

- **Shows:** every tree with more than one agent and every lone agent, with launch and reach marks.
- **Records:** as AG-1, and the sandbox's tool-call verdicts.
- **Computation:** `CascadeReducer.feed` keeps a node per (host, pid) and a census of exits by
  (agent type, role). `result` takes the nodes last seen in the window; adds agents known only from
  their tool calls; turns unclaimed `bg-spare` sessions into infrastructure (`_idle`, counted in
  `idle_spares`); finds each node's parent (`_parent`: the nearest node up the process index, at
  most 64 hops, never linking two users; for a `bg-pty-host`, its user's only daemon; else the
  collector's tree root); cuts cycles; and makes a Codex TUI or a Node launcher over the same agent
  a client. `_tree` walks each tree from its root, recording each agent's level, fan-out, folded
  session (`traj.agent_sessions`), start folder, workspace summary and tool-call verdict
  (`sb.agent_calls`, `session_state`). The trees with agents are sorted by agent depth, agents, tool
  calls and recency, and cut to `top_trees`. The page folds three or more alike agents (same user,
  type and start folder) into one row, and marks B (bypass flag), A (auto mode), U (tool calls not
  sandboxed), F (sandbox failed to start) and O (wrote outside its start folder).
- **Cost:** per record, a dict per node (at most `max_nodes`, 20,000, the rest counted) and a census
  counter. Per read, each rebuild tick: a parent walk per node, the trajectories' fold, the
  workspace's fold and a view per agent, and a walk of every sandbox tool call. Not measured.

#### AG-3 an agent tree

- **Shows:** the picked tree's agents, and the infrastructure and clients they run under.
- **Computation:** a tree sends at most `max_nodes_shown` (60) nodes, chosen by `_pick`: the tree's
  shape first, then, a parent at a time, each parent's busiest agents that started no other agent;
  what the rest did is counted on their parent as `folded`. Infrastructure and clients with no agent
  below them are counted (`hidden_nodes`), not sent. The page lays the tree out
  (`TreeDiagram.tsx:layoutTree`).

#### AG-4 blast radius: summary

- **Shows:** where the picked agent, alone or with its subagents, worked, against its start folder.
- **Records:** `exit` of agent-class processes keyed on an agent pid (`_kt` `apid`): `cwd`,
  `cwd_source`, `io.rchar_mb`, `io.wchar_mb`, `io.rd_mb`, `io.wr_mb` (each process's own, BUILD),
  `args`, `argv_source`, `fork_only`, `_role`, `user`, `_ts_epoch`, `tree_root_pid`.
- **Computation:** `WorkspaceReducer.feed` records, per collector key, the processes per `cwd` with
  their syscall and disk bytes (measured), and the paths each command line names, sorted by what the
  command does with them (inferred; each distinct argv parsed once by a regex tokenizer,
  `workspace.py:plan`). At read, `fold` merges the keys into their agent through the process index,
  and `view` collapses directories deeper than `collapse_depth` (4) below the start folder or a
  top-level root, folds a directory's quiet subdirectories, flags writes and deletes named outside
  the start folder, and groups the outside directories by location. The start folder is the agent's
  own cwd, or its first call's directory off `/`.
- **Cost:** measured in a 24 h build of rtx6000a's 2026-10-10 (1.89 M records, ~90 s): 2.4 s in
  `feed`, about 1.4 µs a record (`workspace.py`). Bounds: `max_keys` 150,000 (at the cap, keys last
  seen before the window go first), `max_dirs` 300 per key, `max_dir_entries` 400,000, 64 names per
  directory and 32 per command line, each excess counted. Views are served for the 300 busiest
  agents (`max_agents`) and every agent that wrote outside its start folder.

#### AG-5 blast radius: written or deleted outside its start folder

- **Shows:** the paths named for writing or deleting outside the start folder, by where they landed.
- **Computation:** `view` keeps at most 400 paths per view, another user's home and system paths
  first, the agent's own scratch last, and serves 16, counting the rest (`flags_more`); the page
  shows every loud one and the first three quiet ones.

#### AG-6 blast radius: directories

- **Shows:** each directory: processes and bytes there (measured), files named (inferred).
- **Computation:** `view`'s rows: the 40 busiest (`max_dirs_shown`) and every flagged one. A byte
  cell opens IOCMDS for the agent's pids (and its subagents'), host and directory, a lookup the
  replicas can answer.

#### AG-7 how deep agents nest; fan-out

- **Shows:** trees by their deepest agent path; agents by how many agents they started.
- **Computation:** `result` counts every tree with agents in the window, sent or not; `_tree` records
  a fan-out for each agent or root with an agent directly below it; the page bins the fan-out.

#### AG-8 tool mix

- **Shows:** the 12 kinds of tool by class or by kind, weighted by one resource at a time.
- **Records:** `exit`: `_eff` (its kind by `tool_buckets.bucket`), `_a3`, `agent_type`, `cpu_s`,
  `io.rd_mb`, `io.wr_mb`, `net_tx_bytes`, `net_rx_bytes`, `peak_rss_mb`, and the dedup's fields
  (LV-2).
- **Computation:** `ToolMixReducer.feed` counts per (class, kind of `tool_buckets.BUCKETS`) and per
  (class, tool), an agent's also per agent type, after the shell-wrapper dedup (LV-2); disk I/O is
  each process's own (its `OwnIo` is fed every exit, a shell the dedup drops too). `_rows` gives
  each kind's counts, sums and shares per class; a page filter to one agent type uses that type's
  counters (`by_agent_type`). The page's shares follow LV-2's rule, with that whole as the total
  beside them.
- **Cost:** as LV-2. A byte cell opens IOCMDS by kind and class, read from the JSONL unless the page
  is filtered by user.

#### AG-9 top tools

- **Shows:** per class, the 12 tools with the most of one resource.
- **Computation:** `_tools` serves the union of the 12 busiest tools by each of those measures,
  every measure on each row; the page sorts the rows and cuts them to 12. For a payload without
  `tools`, the page draws `head` (processes and CPU shares) instead.
- **Cost:** per read, `most_common` over every tool token of a class; the per-class tool maps are
  unbounded.

#### AG-10 commands that failed

- **Shows:** how the tool mix's processes ended, by tool or kind
  ([Commands that failed](../../dashboard/README.md#commands-that-failed)).
- **Records:** AG-8's, after its shell dedup: `exit`: `exit_code`, `signal`, `_command`.
- **Computation:** `ToolMixReducer._exits` counts a non-zero exit or a stop by a signal as a
  failure, and keeps apart an exit 1 from a program whose 1 is an answer (`EXIT_ANSWERS`: grep, rg,
  diff, cmp, ...), a SIGPIPE, and a record with no wait status; a rate is over the processes whose
  ending is known. A count opens IOCMDS with `outcome=nonzero&outcome=signal`.
- **Cost:** a few adds a record, with the tool mix's.

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

- **Shows:** agent sessions, processes per session, session length, the share spent waiting.
- **Records:** `exit` (`_sid`, `_kt`, `_kv`, `_ts_epoch`, `user`, `_purpose`), and each root's own
  record (`_surface`).
- **Computation:** `TrajectoryReducer.summary` runs over every folded session whose last record is in
  the window. Per class it counts the rooted sessions and the probes, processes per session and wall
  time (first to last record in the window), with exact quantiles by sorting, and the waiting
  processes (purpose `poll` or `slurm_mon`), processes per user, and surfaces.
- **Cost:** one pass and two sorts over every session per read, made with SE-2.

#### SE-2 sessions, ranked

- **Shows:** the window's sessions ranked by one of seven measures, a row each.
- **Records:** `exit`: `_sid`, `_kt`, `_kv`, `pid`, `ppid`, `cpu_s`, `_eff`, `_purpose`,
  `peak_rss_mb`, `io` (`rd_mb`, `wr_mb`, `scope`), `net_tx_bytes`, `net_rx_bytes`, `user` or `uid`,
  `tree_root_pid`, `_sandbox`, `_sandbox_ancestry`, `cwd`, `args`, `duration_s`; each root's own
  record (`_approval`, `_surface`); the process index; for running, the live tier's roots and tree
  ticks.
- **Computation:** `TrajectoryReducer.feed` per exit: a `Session` per collector key (host, day,
  class, key) with counts, CPU, run-length chains of tools and of purposes (400 runs), drift arrays,
  each process's own disk bytes (BUILD), and its call log (`_phase`, bottom up): an agent's direct
  children, or, in a session no agent runs, the jobs its terminals' shells ran, known by each
  record's `pgrp` and `sid`. At read (`result`), sessions idle past the longer of 2 h and the window
  are dropped; every key is folded into its agent through the process index (`folded`, `_fold_all`,
  cached until a record, an eviction or the index changes); each rank is `heapq.nlargest(15)` over
  the window's sessions (recency over rooted ones only); each listed session gets a row (`_row`)
  with its phase block (SE-3). `Aggregator._running_rows` marks the newest row of each agent whose
  root's records arrived in the last 600 s, with its tree's census.
- **Cost:** per record, a few adds, a run-length push, the drift arrays and the phase join (pending
  entries up to `pend_max`, 50,000; a record of a session no agent runs whose groups were not kept
  is counted, `phase_ungrouped`, and joins nothing). Per read, under the aggregator's lock: the fold
  of every held key when anything changed ("half a second to most of one" at 24 h on rtx6000a,
  `proctree.py:lookup`), seven partial sorts over the sessions, and up to 105 rows. The page reads
  again every 5 s while the list is shown, and the service makes the payload at most that often.
  Memory: `max_sessions` (150,000) keys per view, about 6 KB each; past it whole trees go, the least
  recently active first (`_shed`).

#### SE-3 a row's phase band, task line and category chip

- **Shows:** for every row, its phases as a band, its tasks, its category chip, its live edge.
- **Records:** the session's call log (an agent's direct children, or a terminal's jobs, each a call
  named by `phase_rules`, with its command text, start, end and what ran under it), its pending
  entries, and, for the live edge, `residency` `by_command`.
- **Computation:** every session has one: an agent's calls named by its own rules (Claude Code,
  Codex) or the general rule, a session no agent ran by its terminals' jobs (`driver` `terminal`);
  one running now with no call ended yet has only its live edge. `_phases` and `_build_block`: an
  agent's day rows are folded into one block (`PhaseLog.fold`, at most `max_calls`, 20,000, calls),
  the open calls added from the pending join (`_open_calls`), then `phase_segment.prepared`,
  `phase_tasks.task_spans` (tasks), `phase_segment.segment` (phases, cut at the tasks' boundaries),
  `phase_tasks.name_tasks` (labels, from the agent's own words or the names in its commands, and the
  headline) and `phase_summary.summarize` (category, uses, summary: the work weighed to the last
  phase's own calls, `own_t1`, and a call's time past its phase to its own use, `ran_on`). Cached
  per row (`_phase_cache`, 4,096 blocks) and made again when the row's calls, records or pending
  entries change, so on each read of a session still at work. `running_now` reads the tree's newest
  census against its baseline, the least each command had over the session's ticks.
- **Cost:** per record, a tool call is masked and its names read once, as it is logged
  (`TrajectoryReducer._call`; `phase_names.tagset` took about 200 µs a call on an earlier sample,
  `phase_names.py`). Per block, at the 20,000-call cap on a shared host (2026-11-03;
  `phase_segment.py`, `phase_tasks.py`): `segment` 0.06-0.10 s, and 1.66-1.84 s with half the calls
  hung and an idle gap after each other one; `task_spans` 0.06-0.59 s; `name_tasks` 0.04-0.07 s. A
  block for every session of a week on eight login nodes takes minutes (README), so the plates count
  no category.

#### SE-4 inspector: facts; purpose ribbon and tool tokens

- **Shows:** the row's facts at the inspector's head; its purpose ribbon and tool tokens.
- **Computation:** SE-2's row (`tool_rle` cut to 60 runs, `purpose_rle` to 80, with their start
  times), the inspector's copy made under the lock by `_inspect`. When the window's fold is stale,
  its `lookup` folds only the keys that share the row's prefix, and takes the agent's other days
  from the window's last fold as it stands (`TrajectoryReducer._agent_days(fresh=False)`), not from
  a fresh fold of every session. A run is coloured by its purpose's kind (`lib/purposes.ts`: slurm,
  build & compute, files & data, and a grey `other`).

#### SE-5 inspector: what was read, and its limits

- **Shows:** what the read covered, what it cost, and every bound it met.
- **Computation:** `_inspect`'s `scan` (`bytes`, `parsed`, `skipped`, `method`, `stopped`,
  `stopped_at`, from which `from=` continues the read), `bounds` and `links`.

#### SE-6 inspector: headline, category, summary

- **Shows:** the project and recurring names, the session's category, a short summary.
- **Computation:** made with the block (SE-3).

#### SE-7 inspector: navigator

- **Shows:** the read's process starts per time bin by purpose kind, with tasks, phases and jumps.
- **Records:** every record the read matched, drawn or only counted: start (`_ts_epoch` less
  `duration_s`), `_purpose`, `_role`, `duration_s`, `exit_code`, `signal`.
- **Computation:** `TreeBuilder._note` notes every matched record before any budget applies.
  `TreeBuilder.nav` bins the starts at the finest width in `BIN_WIDTHS` that keeps the read within
  2,400 bins, and finds the busiest clock minute, the first tool call, the longest process other
  than the root, and the failures (2,000 listed, the rest counted). Tasks and phases come from the
  row's block.
- **Cost:** part of the inspector's read.

#### SE-8 inspector: task list, or phase list

- **Shows:** the tasks, each opening into its phases, and each phase's commonest commands.
- **Computation:** the block's `tasks` and `list` (SE-3), each phase with its `top_commands` (3)
  commands; the page lays a closed task's phases on one line, a phase's length shown where it takes
  a quarter of the task (`sequenceParts`).

#### SE-9 inspector: process tree

- **Shows:** every process the read itemised, under its parent, with its own and subtree figures.
- **Records:** `exit`, `truncated` and `residency` whose `_sid` is one of the row's keys: `pid`,
  `ppid`, `_command`, `args` (masked, then cut to 220 characters), `cwd`, `_ts_epoch`, `duration_s`,
  `cpu_s`, `io`, `net_tx_bytes`, `net_rx_bytes`, `exit_code`, `signal`, `_role`, `depth`, `_kv`;
  parents with no record from the window's process index and the cascades' nodes.
- **Computation:** INSPECT. `TreeBuilder.feed` lets each process claim the children waiting for it
  (a parent exits after its children) and takes each process's own disk and syscall bytes (its
  `OwnIo`, over the records the read takes; for a `truncated` record, its figure less what its
  exited children carried so far), so a subtree's bytes count each process once. `feed` itemises up
  to `inspect_hold` processes and `inspect_per_parent` childless processes of one command under one
  parent, and counts the rest under their parent (newest first, it keeps the newest instead).
  `resolve` attaches what is left by the index, the collector's depth or the fold, and draws parents
  that have no record (at most 2,000). `render` folds runs of siblings (motifs of up to 4) and
  serves `inspect_nodes` entries, breadth first. `_typos` marks what a person mistyped at a
  terminal, for the page's "hide typos"; "annotate purposes" (off unless `annotate.enabled`) labels
  each tool call with a model's purpose from `/api/annotate` (`annotate.py`).
- **Cost:** INSPECT's: one read at a time, up to 150,000 records or 6 GiB. Building a record costs
  about 44 µs on FAS, in the `inspect_workers` processes, and feeding it to the tree about 6 µs, in
  the service's own process. Trees are cached for 600 s.

#### SE-10 inspector: map of the whole session

- **Shows:** the whole session's tasks over its phases, on its own clock, before any read.
- **Computation:** none of its own: it draws the row's block (SE-3) on the row band's axis
  (`bandAxis`: from the first record, or the first phase if earlier, to the last phase's end, or the
  live edge), so it covers the whole session however far the read reached, which SE-7 cannot.
- **Cost:** none on the service.

## Security

The checks (SC-1, `tabs/SecurityTab.tsx`), the sandbox panels (SC-2 to SC-8, `tabs/SandboxTab.tsx`)
and the writes outside the start folder (SC-9); credentials on command lines are checked here and
listed on Risks (RI-4). What `sandboxed` and `approval` mean, and why a session is judged by its
tool calls: [Conventions the panels obey](../../dashboard/README.md#conventions-the-panels-obey).

| | figure | path | payload | code (service · page) |
|---|---|---|---|---|
| SC-1 | list: security checks | BUILD | `sandbox`, `risk.sections`, `risk.secrets`, `cascades.workspace` | (no reducer of its own) · `tabs/SecurityTab.tsx:securityChecks`, `SecurityChecks` |
| SC-2 | plates: agent tool calls sandboxed, sandboxes failed to start, sessions launched with a bypass flag | BUILD | `sandbox.tool_calls`, `attempts`, `approval_by_class_sessions` | `reducers/sandbox.py:SandboxReducer.result` · `tabs/SandboxTab.tsx:SandboxTab` |
| SC-3 | tool calls: did they run sandboxed? (outcomes; sessions by user) | BUILD | `sandbox.tool_calls`, `sessions`, `sessions_total` | `SandboxReducer._calls`, `_sessions`, `result`; `sandbox.py:session_state` · `tabs/SandboxTab.tsx:AxisBlock`, `SessionTable` |
| SC-4 | sandboxed, per process and per session | BUILD | `sandbox.by_class_events`, `by_class_sessions` | `SandboxReducer.feed`, `result` · `tabs/SandboxTab.tsx:AxisBlock` |
| SC-5 | approval at launch, per session and per process; launch flags as written | BUILD | `sandbox.approval_by_class_sessions`, `approval_by_class_events`, `launch_modes` | `SandboxReducer.feed`, `result` · `tabs/SandboxTab.tsx:AxisBlock`, `SandboxTab` |
| SC-6 | sandbox wrapper calls, by outcome | BUILD | `sandbox.attempts` | `SandboxReducer._sessions` · `tabs/SandboxTab.tsx:SandboxTab` |
| SC-7 | processes by sandbox wrapper, kind and sandbox state | BUILD | `sandbox.ancestry_matrix` | `SandboxReducer.feed`, `result`; `sandbox.py:_reading` · `tabs/SandboxTab.tsx:SandboxTab` |
| SC-8 | when each process was checked | BUILD | `sandbox.sandbox_src` | `SandboxReducer.feed` · `tabs/SandboxTab.tsx:SandboxTab` |
| SC-9 | writes outside the start folder: by kind of place; by user | BUILD | `cascades.workspace.agents[].self`: `summary.outside`, `summary.roots`, `flags` | `reducers/workspace.py:WorkspaceReducer.view`; `CascadeReducer._workspace` · `tabs/SecurityTab.tsx:OutsideWrites`, `writersOf`, `placesOf` |

Records for the sandbox panels, SC-2 to SC-8: `exit` (each counted once): `_sandbox` (four states),
`_sandbox_src`, `sandbox_detail`, `_sandbox_ancestry`, `_approval`, `_command` (a wrapper is `bwrap`
or `codex-linux-san…`), `_role`, `pid`, `ppid`, `host`, `_ts_epoch`, `duration_s`, `exit_code`,
`signal`, `dstate_wait_s`, `is_agent`, `agent_pid`, `tree_root_pid`, `agent_type`, `user`; and each
agent root's own record of any kind, `truncated`, `residency`, `conn` and `netio` among them, for its
launch (`_approval`, `_approval_detail`).

#### SC-1 security checks

- **Shows:** every security check under the axis it answers, with what it found and its share.
- **Records:** none of its own: the window's `sandbox`, `risk` and `cascades` panels (BUILD).
- **Computation:** in the page (`securityChecks`), with no threshold of its own: each line is a state
  the service assigns or a rule it applies, so the list and its panels cannot disagree. Launch is
  SC-5's agent sessions by approval at launch, a bypass flag and auto mode each a share of those
  whose launch flags are readable; confinement is SC-3's agent tool calls not sandboxed, with the
  share sandboxed as SC-2 prints it, and the sandboxes that failed to start, of SC-6's wrapper calls;
  exposure is RI-4's matches summed over each credentials section, per class; reach is
  `cascades.workspace.summary`'s `with_outside_writes` and `other_homes`, with SC-9's kinds of place.
  A check whose panel has no body (waited for, still being read, not reduced, not returned), whose
  site counts no credential rule, or with the agent class hidden is *not checked*, and says why:
  never none, never 0.
- **Cost:** none past its payloads'.

#### SC-2 plates

- **Shows:** agent tool calls sandboxed, sandboxes failed, sessions with a bypass flag, as shares.
- **Computation:** SC-3's, SC-6's and SC-5's counts, divided in the page over the known states.

#### SC-3 tool calls: did they run sandboxed?

- **Shows:** agent tool calls by outcome, by class; the sessions that made them, by user.
- **Computation:** `SandboxReducer._calls` reads an agent's exits bottom up: a child reports to its
  parent's slot (`below[(host, ppid)]`: whether a payload ran, was isolated, was unknown); a wrapper
  or a Claude Code Bash tool shell becomes a tool call with its outcome (isolated, not isolated,
  failed -- the wrapper exited non-zero with no payload --, no payload, unknown), and a wrapper
  absorbs the calls below it, so nested wrappers fold into the outermost. At read, `_sessions` groups
  the calls not absorbed by the session that owns them (through the process index) beside every
  agent root, and `session_state` judges each session by its calls.
- **Cost:** a few counters a record, and for an agent's exit a slot (at most 200,000 parents waiting,
  the overflow counted) and a call per tool call, kept for the window with no cap. `_sessions` walks
  every call each time the payload is made, and the cascades (AG-2) and the Users tab
  (`portraits.inputs`) walk them again (`agent_calls`). The slots and their calls are carried from
  hour to hour when merging.

#### SC-4 sandboxed, per process and per session

- **Shows:** per class, processes by namespace state and sessions by verdict.
- **Computation:** a counter per class of each counted record's `_sandbox`; the session verdicts of
  SC-3. As in SC-3 and SC-5, a state the axis can judge is a share of the class's judged ones, and the
  rest (exited before it was checked, unknown, no calls, not an agent session, can't tell) are
  counts, with the coverage under the class (`AxisBlock`, `lib/sandbox.ts:shareOfKnown`).

#### SC-5 approval at launch

- **Shows:** per class, sessions and processes by approval at launch; launch flags as written.
- **Computation:** each root's verdict is its own record's (`roots`, the newest kept); sessions are
  SC-3's; per process, a counter of each counted record's `_approval`; `launch_modes` counts the
  rooted sessions by (agent type, approval, flags).

#### SC-6 sandbox wrapper calls, by outcome

- **Shows:** per wrapper (`bwrap`, `codex-linux-sandbox`), agent type and outcome, the calls.
- **Computation:** `_sessions` counts the wrapper calls not absorbed by another, by those three.

#### SC-7 processes by sandbox wrapper, kind and sandbox state

- **Shows:** per wrapper above, process kind and sandbox state: processes and D-state wait.
- **Computation:** a counter per key with summed `dstate_wait_s` and a present count; `_reading`
  words each row.

#### SC-8 when each process was checked

- **Shows:** each process's namespace answer by when it was read.
- **Computation:** a counter of `_sandbox_src`.

#### SC-9 writes outside the start folder

- **Shows:** the agents that wrote or deleted outside their start folder, by place and by user.
- **Records:** AG-4's and AG-5's: the paths an agent's commands name, read from the text of its
  command lines, not measured.
- **Computation:** in the page, from `cascades.workspace.agents[].self`, each agent judged on its own
  processes as `with_outside_writes` counts it. An agent is listed when its `summary.outside_writes`
  is above 0, and the service serves every such agent however quiet (`CascadeReducer._workspace`), so
  the list is whole. The kinds of place are the `summary.roots` it wrote or deleted in, ranked as the
  Agents tab ranks its flags (`lib/places.ts`); its paths are its `flags` (16 served, the rest
  counted in `flags_more`), in the service's order.
- **Cost:** none past AG-4's.

## Login nodes › Resources

Login node health, the worst individual processes, then the I/O panels, drawn by Risks' code
(`tabs/RiskIoTab.tsx:RiskIoTab`, `part="io"`, `io="main"`). The per-process tables behind them are
the Internal tab's (IN-1 to IN-4).

| | figure | path | payload | code (service · page) |
|---|---|---|---|---|
| RE-1 | login node health: nodes, NFS mounts, local disks, accounts, stuck processes | NODE | `node` | `reducers/resources.py:NodeReducer.result` · `tabs/NodeHealth.tsx:NodeHealth` |
| RE-2 | worst individual processes | BUILD | `resources.worst_stall`, `worst_fault`, `worst_wait` | `ResourceReducer._keep_worst` · `tabs/ResourcesTab.tsx:WorstPanel`, `tabs/RiskIoTab.tsx:InstanceTable` |
| RE-3 | disk read and written by tool | BUILD; a bar opens IOCMDS | `io_process.by_tool` | `reducers/io.py:IoReducer.feed`, `io.py:_heaviest` · `tabs/RiskIoTab.tsx:toolBars`, `charts/primitives.tsx:HGroupBars` |
| RE-4 | network bytes per class (table) | BUILD; a cell opens IOCMDS | `io_process.by_class[c]`: `net_tx_mb`, `net_rx_mb`, `net_calls` | `IoReducer.feed` · `tabs/RiskIoTab.tsx:RiskIoTab` |
| RE-5 | network endpoints | BUILD, named on read | `io_process.endpoints` | `IoReducer._feed_endpoints`, `_endpoint_rows`; `endpoints.py:serve`, `fold` · `tabs/NetEndpoints.tsx:NetEndpoints` |
| RE-6 | I/O by user | BUILD; a cell opens IOCMDS | `io_process.by_user` | `IoReducer.feed`, `io.py:_heaviest` · `tabs/RiskIoTab.tsx:RiskIoTab` |

#### RE-1 login node health

- **Shows:** one table at a time: nodes, NFS mounts, local disks, accounts, stuck processes
  ([Login node health](../../dashboard/README.md#login-node-health)).
- **Records:** the node tier's newest two snapshots a host (NODE).
- **Computation:** `NodeReducer.result`: per host the load, memory, CPU, process states,
  responsiveness probes, users and open files; per account the cgroup's CPU as a rate between the
  two snapshots and whether the kernel held it back (`throttled_usec` grew); a local filesystem at
  `node.disk_full_pct` or `node.inode_full_pct` (90) is `full`; the stuck processes, up to 20 a host,
  masked (`redact.py`).
- **Cost:** two snapshots a host, read every rebuild tick; nothing more per read.

#### RE-2 worst individual processes

- **Shows:** for three measures, up to 12 worst processes each, grouped by user.
- **Computation:** `_keep_worst` appends a row when the value is at least the list's floor; past 240
  rows the list is sorted, cut to 24, and the floor raised; `result` serves 12. Merging cuts the list
  again by the same rule.
- **Cost:** a row is made only for a value above the floor, so few records make one.

#### RE-3 disk read and written by tool

- **Shows:** per tool, disk read and written as two bars, for the 14 heaviest on each.
- **Records:** `exit`: `_eff` or `_command`, `io.rd_mb`, `io.rchar_mb`, `io.wr_mb`, `io.wchar_mb`,
  and `pid`, `ppid`, `host`, `io.scope` for the join.
- **Computation:** `IoReducer.feed` sums each tool's four byte counters, each process's own (BUILD),
  its processes, and those with an `io` block; `result` serves the union of the top
  `reducers.io.top_n` (20) tools on each counter (`_heaviest`, up to 80 rows). The page keeps the 14
  heaviest on disk read and the 14 on disk written, up to 28 rows, ordered by the two together; a
  kept tool's 0 on the other counter is drawn as 0. Every class: the class chips do not narrow it. A
  bar opens IOCMDS for the tool, read from the JSONL unless the page is filtered by user.
- **Cost:** a few adds a record; the tool map is unbounded; per read, four `nlargest` passes.

#### RE-4 network bytes per class

- **Shows:** per class, network bytes sent and received and network calls, with coverage.
- **Records:** `exit`: `net_tx_bytes`, `net_rx_bytes`, `net_calls`, each null when the collector's
  netbytes block was dropped.
- **Computation:** an `Acc` per class and counter.

#### RE-5 network endpoints

- **Shows:** the top 10 remote endpoints by calls or bytes, split by class
  ([Network endpoints](../../dashboard/README.md#network-endpoints)).
- **Records:** `exit`: `net_endpoints` (schema 7): per endpoint `addr`, `inbound`, `calls`,
  `tx_bytes`, `rx_bytes`, `proto`, `port`, `host`, `external`, `provider`; and its `unattributed`,
  `overflow`, `unaccounted` losses.
- **Computation:** `IoReducer._feed_endpoints` adds each address's calls and bytes per class, every
  address kept. On every `/api/panels` read, `endpoints.serve` names each address (lookups run on
  background threads and are kept in `<cache.dir>/rdns.json`) and groups the addresses that share a
  name; for every combination of the classes present it keeps the union of the top 10 by calls and
  by bytes (`fold`). The page ranks the combination it shows, and gives the share of RE-4's bytes
  whose endpoint is known.
- **Cost:** per record, a dict update per endpoint; per `/api/panels` read, a pass over every address
  for every class combination (up to 15). While names are pending, the page reads `/api/panels`
  again after 3, 6, 12, 24 and 48 s. Not measured.

#### RE-6 I/O by user

- **Shows:** per user, processes, disk and requested bytes, network bytes, I/O coverage.
- **Computation:** `IoReducer.feed` per user, each process's own bytes; `result` serves the union of
  the top 20 users on each of the four I/O counters. A user's class is that of their newest record,
  which the class chips keep the user by.

## Login nodes › Risks

See [Risk rules differ by cluster](../../dashboard/README.md#risk-rules-differ-by-cluster). A section
the cluster's profile leaves out is neither computed nor drawn.

| | figure | path | payload | code (service · page) |
|---|---|---|---|---|
| RI-1 | a plate per section | BUILD | `risk.sections`, `login_compute.by_class`, `dangerous.rows`, `secrets.rows` | `reducers/risk.py:RiskReducer.result` · `tabs/RiskIoTab.tsx:RiskIoTab` |
| RI-2 | compute on a login node: bars, per tool, heaviest processes | BUILD | `risk.login_compute` | `RiskReducer.feed`, `result` · `tabs/RiskIoTab.tsx:RiskIoTab`, `InstanceTable` |
| RI-3 | commands that should not run here | BUILD | `risk.dangerous` | `RiskReducer.feed`, `_matches`, `result` · `tabs/RiskIoTab.tsx:RiskIoTab`, `WhoChips`, `InstanceTable` |
| RI-4 | credentials on a command line | BUILD | `risk.secrets` | `RiskReducer.feed`, `_matches`, `result` · `tabs/RiskIoTab.tsx:RiskIoTab` |
| RI-5 | rules in effect | BUILD (config only) | `risk.rules` | `risk.py:ruleset`, `RuleSet.summary` · `tabs/RiskIoTab.tsx:RulesInEffect` |

#### RI-1 a plate per section

- **Shows:** a plate per risk section: CPU-hours, matching runs, or credential matches, by class.
- **Computation:** the page sums RI-2 to RI-4's payloads per class over the section's rules.

#### RI-2 compute on a login node

- **Shows:** CPU in compute and transfer tools, by tool and class; the 24 heaviest processes.
- **Records:** `exit`: `_eff` or `_command` (its kind), `cpu_s`, `user`, `host`, `_a3`.
- **Computation:** `RiskReducer.feed` adds each exit of a compute (build, interpreter) or transfer
  (`net/data`) kind (`risk.login_compute`) to its tool; a compute exit also joins a heaviest list
  (cut to 200 when it reaches 400) and is added to its class's CPU. `result` ranks the tools by CPU
  (25 rows) and gives the compute CPU's share of all CPU.
- **Cost:** a kind lookup and a few adds per record.

#### RI-3 commands that should not run here

- **Shows:** per dangerous-command rule: runs, users, who ran it, the share that exited 0, CPU.
- **Computation:** per matching rule (LV-4's match): runs, CPU, users, hosts, classes, (class, agent
  type) counts, the first 12 runs, and the runs with an exit code and those that exited 0; rows by
  severity, users, runs.
- **Cost:** as LV-4.

#### RI-4 credentials on a command line

- **Shows:** per credential pattern: matches, users, tools, who ran it; never the command line.
- **Computation:** the credential patterns run on `args` alone, case-sensitive. The matched text is
  never kept; a match keeps its identity fields but not the argv.

#### RI-5 rules in effect

- **Shows:** every rule with its pattern, severity, source and section; the rules not in effect.
- **Computation:** `RuleSet.summary`, built once per distinct `risk` configuration (`ruleset`). No
  records.

## HPC

The scheduler's own entry in the rail (`?tab=slurm`, or `?tab=hpc`), gone when `slurm.enabled` is
false ([Slurm: how sessions use the scheduler](../../dashboard/README.md#slurm-how-sessions-use-the-scheduler)).
Requests are read off argv: a `#SBATCH` line is not seen.

| | figure | path | payload | code (service · page) |
|---|---|---|---|---|
| SL-1 | plates: agent jobs submitted, queue checks per job, agent sessions polling, submissions refused | BUILD | `slurm.by_class`, `slurm.rejections` | `reducers/slurm.py:SlurmReducer.result` · `tabs/SlurmTab.tsx:SlurmTab` |
| SL-2 | abnormal Slurm activity: the rules, each class's count; the sessions that broke one | BUILD | `slurm.abnormal`, `sessions[].abnormal`, `by_class[c].abnormal_by` | `SlurmReducer._abnormal`, `abnormal_rules`, `result` · `tabs/SlurmTab.tsx:Abnormal`, `ruleWords`, `evidence` |
| SL-3 | sessions at the scheduler | BUILD | `slurm.sessions`, `sessions_total` | `SlurmReducer.feed`, `_submit`, `_sessions`, `_polls`, `result` · `tabs/SlurmTab.tsx:Sessions`, `SessionDetail` |
| SL-4 | how each class uses the scheduler | BUILD | `slurm.by_class`; `tool_mix.by_class[c].rows` | `SlurmReducer.result` · `tabs/SlurmTab.tsx:ByClass` |
| SL-5 | submissions the scheduler refused | BUILD | `slurm.rejections` | `SlurmReducer._submit`, `slurm.py:reason` · `tabs/SlurmTab.tsx:SlurmTab` |
| SL-6 | latest submissions | LIVE | `submits` | `reducers/live.py:LiveReducer._submit` · `tabs/SlurmTab.tsx:Latest` |

#### SL-1 plates

- **Shows:** agent jobs, queue checks per job, polling sessions, refusals; other classes beside.
- **Computation:** `slurm.by_class` (SL-4) and the agent's first refusal reason (SL-5).

#### SL-2 abnormal Slurm activity

- **Shows:** the abnormal-activity rules, each class's count, and every session that broke one.
- **Records:** SL-3's.
- **Computation:** `SlurmReducer.result` judges each session's row (`_abnormal`) against
  `reducers.slurm.abnormal`. Fast polling is the longest stretch of its polls each at most
  `poll_gap_s` (30 s) after the last (`_stretch`) lasting `poll_min_s` (30 min) or more; SL-3's own
  polling loop, polls up to 3 minutes apart, breaks nothing. Each other rule is a count against a
  threshold: the most queue checks, and the most jobs, in any 60 s (`_in_a_minute`: a sliding
  half-open window over the times SL-3 keeps, so a lower bound past 50,000 a key), submissions
  refused, `scancel` calls, and `srun`/`salloc` runs that ended non-zero or on a signal. Each class
  counts its sessions per rule, zero included, and the title's count is those totals, so it holds a
  session whose row is not sent: every session that broke a rule is served past the busiest 200, up
  to 500 more, the rest counted (`abnormal.served`). The page holds no threshold: it words the rules
  from `abnormal.rules`.
- **Cost:** a pass over each session's row at read: its query and job times, sorted once, walked by
  two pointers for the minutes and once more for the fast stretch.

#### SL-3 sessions at the scheduler

- **Shows:** per session at the scheduler: jobs, refusals, queue checks and polling, with its
  detail.
- **Records:** `submit`: `tool`, `args`, `job_id` or `job_ids`, `exit_code`, `captured_tail`,
  `req_src`, `partition`, `time_limit_s`, `mem`, `gpus`, `array`, `user`, `_a3`, `agent_type`, `_kt`,
  `_kv`, `tree_root_pid`, `host`, `_ts_epoch`; `exit` of the query commands, `scancel`, `srun` and
  `salloc` (`_command`, `_ts_epoch`, `exit_code`, `signal`).
- **Computation:** `SlurmReducer.feed` and `_submit` per key (an agent's (host, agent pid, tree
  root); anyone else's (host, class, user)): the outcome (`outcome`: job, dry run, step, refused, no
  id), flags by regex, whether the request was on the command line, the command's shape (a blake2b
  of `args` with digits masked), each job's time, partitions, GPUs times array tasks, GPU-hours where
  GPUs and a time limit were both on the command line, and each query's time. At read, `_sessions`
  folds the agent keys into their owner through the process index, and `_polls` joins a session's
  queries within 2 s into a poll, polls within 3 minutes into a stretch, and calls a stretch of 10
  minutes or more polling. Sessions are sorted by queries plus 10 times jobs and refusals, plus
  runs; the busiest 200 are served, then every other one that broke a rule (SL-2).
- **Cost:** a few counters a record; bounds of 50,000 keys (the rest counted), and per key 50,000
  query times, 50,000 job times and 2,000 shapes. Per read, a sort of each session's query times.

#### SL-4 how each class uses the scheduler

- **Shows:** per class, SL-3's counts summed, and the time limit asked.
- **Computation:** `result` sums over the sessions per class; the time limit is a `Quant` per class.
  The page takes the scheduler share as the tool mix's `slurm-submit` and `slurm-query` kinds over
  the class's processes (AG-8).

#### SL-5 submissions the scheduler refused

- **Shows:** per class, tool and reason, the refused submissions, with an example.
- **Computation:** a refused submission (no job id, a non-zero exit, and a scheduler error line in
  `captured_tail`) is counted by (class, tool, reason), the reason being that line with numbers
  masked, cut to 160 characters (`reason`). The 20 commonest are served.

#### SL-6 latest submissions

- **Shows:** the newest submissions in the window, with what came back and what was asked.
- **Records:** `submit`, as SL-3.
- **Computation:** `LiveReducer._submit` keeps the 50 newest (`live.submit_tail`), each judged by
  `slurm.outcome` and masked; `result` cuts them to the window.

## Ask LLM

| | figure | path | payload | code (service · page) |
|---|---|---|---|---|
| AL-1 | suggestions for the operator (cards) | ASK over BUILD and LIVE | `/api/ask/{job}` `suggestions` | `aggregator.py:Aggregator.ask`; `ask.py:snapshot`, `parse_suggestions`, `Asker.submit` · `tabs/ChatTab.tsx:SuggestionList` |
| AL-2 | marked done (list) | none: the service's own file | `/api/suggestions/done` | `ask.py:Asker.done_list`, `mark_done` · `tabs/ChatTab.tsx:ChatTab` |
| AL-3 | ask about the whole window (answers with their sources) | ASK over BUILD and LIVE | `/api/ask/{job}` `answer`, citations | `Aggregator.ask`; `ask.py:snapshot`, `Asker` · `tabs/ChatTab.tsx:ChatTab`, `components/Ask.tsx`, `components/Cite.tsx:Sources` |

#### AL-1 suggestions for the operator

- **Shows:** suggestion cards for the operator, each with its numbers and their sources.
- **Records:** none read for it: the window's payloads.
- **Computation:** `Aggregator.ask` with `context.suggest`: the ASK snapshot for the page's window
  and filter (refused while the window is still being read), with the trajectories' summary and the
  15 sessions with the most processes, the cascades with a session hint on each agent, the accounts
  the risk rows name (`risk_who`), and the suggestions marked done. Command lines are dropped unless
  `ask.include_commands` is set, and every object gets a citation ref. The agent is asked one fixed
  question; its JSON answer is checked and cut to 8 suggestions (`parse_suggestions`).
- **Cost:** one run of the agent CLI, at most 240 s, one at a time. Not measured.

#### AL-2 marked done

- **Shows:** the suggestions anyone marked done.
- **Computation:** `Asker.mark_done` copies the suggestion from its job into
  `<cache.dir>/ask/done.json` (200 kept). No records.

#### AL-3 ask about the whole window

- **Shows:** the answer to a question about the window, its figures linked to their rows.
- **Computation:** as AL-1, without the fixed question, with the conversation's last turns, and with
  the page's filter left out unless the question is about one panel. A panel's own "ask" button, on
  any tab, sends only that panel's keys (`Aggregator._ask_panel`); the inspector's sends a digest of
  the session's process tree, read by INSPECT (`Aggregator._ask_session`, `ask.py:session_digest`).
- **Cost:** as AL-1.

## Search & export

Hidden (`App.tsx`, `HIDDEN_TABS`); its code is kept, and `/api/events` and `/api/export` are
served.

| | figure | path | payload | code (service · page) |
|---|---|---|---|---|
| EX-1 | records, filtered (table) | EVENTS | `/api/events` `rows`, `next_cursor`, `total_scanned`, `event_filter_defaulted` | `export.py:scan`, `make_predicate`; `app.py:api_events` · `components/EventsExplorer.tsx:EventsExplorer`, `tabs/LiveTab.tsx:EventTable` |
| EX-2 | download | EVENTS | `/api/export`: JSONL with a `_meta` header and a `_trailer` | `export.py:scan`, `meta_header`; `app.py:api_export` · `components/EventsExplorer.tsx:EventsExplorer` |

#### EX-1 records, filtered

- **Shows:** the records matching the filters, newest first, 200 a page (5,000 at most), by user.
- **Records:** any kind; `event` defaults to `exit`, and the response says when it did. The fields
  the filters name: time, `_a3`, `agent_type`, `user`, `host`, `_eff`, `_purpose`, bucket,
  `session_key`, `_sandbox`, `_approval`, `depth`, `cpu_s`, `duration_s`, `exit_code`, `signal`, and
  `q`, a substring of the masked command line.
- **Computation:** EVENTS. When names are anonymized, `q` is matched against the aliased text, at
  the cost of rewriting each candidate (`app.py:search`).
- **Cost:** per page, bounded by the matches it needs and the files' time ranges (EVENTS).

#### EX-2 download

- **Shows:** the same records as a JSONL file, with the derived `_` keys, credentials masked.
- **Computation:** the same predicate, every match streamed, 500 lines a chunk, up to
  `export.max_rows` (5,000,000); the trailer gives the row count and whether the cap cut it short.
- **Cost:** a read of every planned file's range, bounded only by the cap.

## Internal

The per-process tables of the resource and I/O reducers (IN-1 to IN-4), drawn through
`ResourcesTab part="internal"` and `RiskIoTab io="internal"` (`tabs/InternalTab.tsx`).

| | figure | path | payload | code (service · page) |
|---|---|---|---|---|
| IN-1 | per-process distributions | BUILD | `resources.by_class[c].quants`, `top`, `oncpu_set_aside` | `reducers/resources.py:ResourceReducer.feed`, `result`; `reducers/__init__.py:Quant` · `tabs/ResourcesTab.tsx:DistTable` |
| IN-2 | faults, delays, and D-state | BUILD | `resources.by_class[c].faults`, `delays`, `dstate`; `impossible_waits` | `ResourceReducer.feed`; `reducers/__init__.py:Acc` · `tabs/ResourcesTab.tsx:FaultTable` |
| IN-3 | requested bytes against disk bytes, per class | BUILD | `io_process.by_class[c]` | `IoReducer.feed`, `result` · `tabs/RiskIoTab.tsx:RiskIoTab` |
| IN-4 | I/O distributions | BUILD | `io_process.by_class[c].quants` | `IoReducer.feed`; `reducers/__init__.py:Quant` · `tabs/RiskIoTab.tsx:RiskIoTab`, `components/Coverage.tsx:QuantCells` |

#### IN-1 per-process distributions

- **Shows:** per class, mean and quantiles of CPU time, lifetime, on-CPU share, runqueue wait, peak
  memory.
- **Records:** `exit`: `cpu_s`, `duration_s`, `sched_run_s`, `sched_wait_s`, `peak_rss_mb` (else
  `rss_mb`), and the identity fields of the process behind each max (`reducers/__init__.py:who`).
- **Computation:** each value goes into a `Quant`, a DDSketch of every value with 1% relative error
  (logarithmic buckets; two merge by adding counts, so a merged window equals one pass); `n`, `min`,
  `max` and `mean` exact; a null counted for coverage; a field exactly 0 everywhere flagged
  `all_zero` and drawn as not measured. The on-CPU share is `sched_run_s / duration_s`; a process
  whose run time passes its lifetime by more than 5% is set aside and counted. `top` keeps the
  process behind each max.
- **Cost:** five sketch adds a record, bucketed 4,096 values at a time; per read, the buckets sorted
  once per measure. The sketch grows with the values' range, not their number.

#### IN-2 faults, delays, and D-state

- **Shows:** per class, faults, context switches, delays and D-state, average and total.
- **Records:** `exit`: `min_flt`, `maj_flt`, `cmaj_flt`, `nvcsw`, `nivcsw`, `blkio_wait_s`,
  `swapin_wait_s`, `freepages_wait_s`, `dstate_wait_s`, `dstate_episodes`, `dstate_max_s`,
  `dstate_src`, and `_impossible` (the normalizer's list of waits it voided).
- **Computation:** an `Acc` (sum, present count, records seen) per field and class; the page divides.

#### IN-3 requested bytes against disk bytes

- **Shows:** per class, bytes requested against disk bytes, and what the page cache served.
- **Records:** as RE-3 (each process's own bytes).
- **Computation:** none of its own: `IoReducer`'s per-class sums, the cache served being
  `max(0, rchar - rd)` and the hit its share of `rchar`.
- **Cost:** its reducer's.

#### IN-4 I/O distributions

- **Shows:** per class, quantiles of each process's own bytes on four counters.
- **Computation:** a `Quant` per counter and class (IN-1's sketch); a record with no `io` block is
  counted as not measured.

## Feeds

See [Is each collector OK?](../../dashboard/README.md#is-each-collector-ok). No window and no filter.

| | figure | path | payload | code (service · page) |
|---|---|---|---|---|
| FD-1 | collectors: one row per login node, opened into its runs | LIVE (health), FEEDS (files) | `/api/collectors` `hosts`, `tiers` | `health.py:CollectorHealth.feed`, `report`; `aggregator.py:Aggregator.collectors` · `tabs/FeedsTab.tsx:Collectors`, `HostDetail`, `Runs` |
| FD-2 | feeds, and the history store | FEEDS | `/api/feeds` | `feeds.py:resolve`, `resolve_dated`; `Aggregator.refresh_feeds`; `history.py:HistoryStore.status` · `tabs/FeedsTab.tsx:Sources`, `Where`, `History` |
| FD-3 | startup read | LIVE | `backfill` | `Aggregator.backfill_now` · `tabs/FeedsTab.tsx:Backfill` |
| FD-4 | configuration | CONFIG | `/api/config` | `config.py:Config.as_dict` · `tabs/FeedsTab.tsx:FeedsTab` |

#### FD-1 collectors

- **Shows:** per login node, the collector's verdict (OK, degraded, stopped, silent, no heartbeat),
  why, and its runs.
- **Records:** `meta`, `stop`, `residency_totals`: `collector_pid`, `_ts_epoch`, `interval_s`;
  `meta`'s `kernel`, `bcc`, `features`, `features_rejected`, `sysctl`, `euid`, `schema_version`;
  `stop`'s `reason`, `uptime_s`, `records`, `truncated`, `perf_lost`, `perf_seen`; and the FEEDS
  report's file list (size, mtime, host) of both tiers.
- **Computation:** `CollectorHealth.feed` runs in the live dispatch, so it covers the backfill's span
  and the tail, keeping up to 20 runs per host (by collector pid) and the host's newest heartbeat.
  `report` judges each host against `feeds.ebpf.max_age_s` (300 s) and a loss of 0.1% of kernel
  events (`LOST_WARN_PCT`), and lists the worst first.
- **Cost:** O(1) per `meta`, `stop` or heartbeat; per read, hosts times runs; read again every 20 s
  while the tab is shown.

#### FD-2 feeds, and the history store

- **Shows:** both feeds' status, newest record and files; the history store
  ([History in Parquet](../../dashboard/README.md#history-in-parquet)).
- **Records:** each feed's newest file's last record.
- **Computation:** `resolve` globs `feeds.<feed>.days` dated directories (8 for the eBPF tier, 2 for
  the node tier) and reads the newest file's last record (`newest_record_ts`); `refresh_feeds` adds
  the eBPF tier's records counted, schema versions, exclusions, backfill, tail counters, synthetic
  hosts and the store's status.
- **Cost:** per read, a glob, a stat per file and a tail read per feed (SH-4).

#### FD-3 startup read

- **Shows:** the backfill's state, and the lines that did not parse.
- **Computation:** `Aggregator.backfill`, updated one batch at a time; `retained_from` is the oldest
  live bin.

#### FD-4 configuration

- **Shows:** every resolved setting, its value and its origin; the password hash hidden.
- **Computation:** `config.py:Config.as_dict(with_origins=True)`. No records.

## Served, not drawn

What the service computes and serves but no figure draws: each costs its reducer's work, and some
reach Ask LLM's snapshot or a panel's basis note.

- **`resources.series.by_class[c].n`**: each class's processes exited per bin; the count is LV-5's,
  per minute, and each class's share of the window LV-6's.
- **`load_1` and `cpu_pct` of the live frame's host rows**: always null, being the node tier's
  (`LiveReducer`), so LV-21 has no column for them; a node's own load and CPU are RE-1's.
- **`workspace`**: the workspace reducer's own payload is its buffer counters; the per-agent views
  the page draws are in `cascades.workspace` (AG-4).
- **`cascades.edges`, `edge_via`, `role_census`**: who starts whom, how each parent was found, and
  every classifier-tagged process counted by agent type and role.
- **`trajectories`' `drift`** (cwd drift and the purpose mix's Jensen-Shannon divergence, per row),
  `dominant_user_share_pct`, `by_key_type`, `by_class_counts`, `fold` and `buffer`.
- **`sandbox.agent_only` and `sandbox_detail`**: only in SC-7's basis note.
- **The live frame's `now` block, and the roster's counts** (`users.total`, `users.by_class`,
  `users.with_live_agent`): the last full minute's exits by class, the sessions and processes running
  now, the live agent sessions by launch approval, and the accounts active in the window; in Ask
  LLM's snapshot (`live_now`).

## What is slow

The expensive operations, most expensive first, ranked by what the repo measured (the numbers are
with the read paths and figures above) and, where nothing was measured, by the work the code does.

1. **Reading a window from the day-files** (BUILD): a scan, or the first reduction of a window's
   hours, parses every line but `meta` and `stop` and feeds it to about eleven reducers; merged
   windows pay it once per hour, but the first fill of the hours, the 1 h view, every filtered view
   and their drift rebuilds still read the window, and every BUILD figure waits on it.
2. **Re-reading a window per click or query** (IOCMDS, EVENTS): from the JSONL unless a replica can
   test the filter, a `.gz` with no replica inflated whole to index it; an export streams every match
   in its range, up to 5,000,000 rows.
3. **The backfill** (LIVE): placing the read inflates every sealed `.gz` to index it, window builds
   wait for it, and the indexes are rebuilt on every restart.
4. **The session inspector's read** (INSPECT; SE-5, SE-7, SE-9): one at a time, so a second reader
   waits; a sealed `.gz` with no replica is searched in the service's own process.
5. **Folding and ranking the sessions on read** (SE-1 to SE-3, LV-3, US-*, AG-2): the fold of every
   held key runs under the aggregator's lock whenever a record arrived since the last, the Sessions
   list asks every 5 s, and a running session's phase block is made again on each read; the cascades
   and the Users roster read the same fold.
6. **The Users tab's composition** (US-*): a roster walks every session's call log, its inputs every
   tool call of the window, and a portrait makes up to 80 phase blocks; not measured on a real feed.
7. **Memory, and feeding every view**: every live record is reduced once per held view (at most
   `panels.max_windows`, 5, filtered ones included). Unbounded within the window: the sandbox's tool
   calls, the tool mix's and I/O's per-tool and per-user maps, the resources' CPU-to-run ratios, the
   endpoints' addresses, the live tier's submissions.
8. **Making and serving the panels**: every held view's payload is made again each rebuild tick, a
   reducer per lock hold; each `/api/panels` read copies the payload and names and ranks the
   endpoints for every class mix (RE-5). Not measured.
9. **The live frame** (LV-5, LV-17 to LV-21): one per distinct window per poll that brought records,
   cheap by design (cached bin rows, closed roster bins summed once), but made under the
   aggregator's lock.
