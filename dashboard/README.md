# Live agent-behaviour dashboard

A FastAPI service that tails the [`ebpfm`](../collector) collector's JSONL day-files (a sealed day's
kept as Parquet too: [History in Parquet](#history-in-parquet)), keeps rolling aggregates over a **selectable** window, and
pushes them to a React page over a WebSocket. Every number on the page is measured: a feed with
no data names the path it expected instead of showing a plausible figure, and a metric the
collector could not populate reads **not measured** rather than `0`. What each figure on the page
computes, from which records, by which read path and at what cost, is in
[docs/figures](../docs/figures/README.md).

This replaces a static-HTML demo, retired from the tree and recoverable in git history
under `dashboard/archive/`. That demo generated most of its own numbers (`rc_synth.py`),
and its viewer manufactured new events between polls; nothing here does either. `grep -r Math.random rc_dashboard web/src` returns
nothing, by design and by CI-able assertion (`npm run check:no-random`).

## Run it

```bash
cd dashboard && uv sync
uv run python -m rc_dashboard --check-feeds     # what resolved, what didn't, and why
cd web && npm ci && npm run build && cd ..      # builds web/dist/, which the service serves
uv run python -m rc_dashboard serve --port 8080
```

Off-cluster, `--check-feeds` exits 1 because the eBPF feed is `required` and absent. That is
the point: point it at a collector root first.

```bash
RC_DASH_EBPF_ROOTS=/var/log/ebpfm \
RC_DASH_NODE_ROOTS=/var/log/ebpfm-login \
uv run python -m rc_dashboard serve
```

Reach a remote instance over a tunnel: `ssh -L 8080:127.0.0.1:8080 <host>`.

No cluster at hand? [Demo mode](#demo-mode) runs the same service over a synthetic feed it
writes itself: `uv run python -m rc_dashboard demo`.

## Configuration

Every source is configurable, resolved **per key** in the order
**CLI flag > `RC_DASH_*` env var > config file > built-in default**. A flag you don't pass
never outranks the file.

- Copy [`dashboard.config.example.json`](dashboard.config.example.json) to
  `dashboard.config.json` (gitignored) and edit. `//`-prefixed keys are comments, stripped on
  load, so the example file is itself a legal config.
- `--print-config` prints the resolved tree with a `_origin` per key, so "why is it reading
  *that*" is never a guess.
- Path defaults are **candidate lists**, probed against the filesystem. `--check-feeds` shows
  every candidate and which one won.
- `history` (`RC_DASH_HISTORY`, `RC_DASH_HISTORY_DIR`, `_WORKERS`, `_DAYS`) keeps every
  sealed day-file as Parquet too, made in the background, and answers a selective read of one
  from that copy ([History in Parquet](#history-in-parquet)); `RC_DASH_HISTORY=0` reads every
  day-file as JSONL. Switching it changes no answer, and rebuilds no saved window.
- `ingest.workers` (`RC_DASH_INGEST_WORKERS`, 1 by default) and `ingest.reducer_groups`
  (`RC_DASH_INGEST_REDUCER_GROUPS`, 2) reduce the hours in worker processes, a node's share
  of an hour and a group of reducers each; 0 workers is one per CPU the service may use
  ([Hourly merge-reduce](#hourly-merge-reduce)). Going from 1 worker to more, or back,
  reduces the hours afresh; the count and the groups change no saved hour.

JSON rather than TOML: the format is shared with the rest of the repo (every collector's record
is a JSON line; the retired builder's contract was `data.json`) and
`--print-config` has to round-trip it.

## The two feeds

| feed | what it carries | default candidates |
|---|---|---|
| `ebpf` (**required**) | the root tier: per-process exit, residency, tcp/conn, submit | `$EBPFM_LOG_ROOT`, `$EBPFM_OUTPUT_DIR`, `$RC_MEASUREMENT_ROOT/ebpfm/logs`, `log/ebpfm`, `/var/log/ebpfm` |
| `ebpf_node` | the unprivileged node tier: load, memory, NFS, per-user cgroup | `$EBPFM_NODE_LOG_ROOT`, `…/ebpfm/node/logs`, `/var/log/ebpfm-login`, `log/login` |

Under a root, a day is a directory of one file per host and stream:
`<YYYY-MM-DD>/<host>.exits.jsonl[.gz]` and `<host>.snapshot.jsonl[.gz]` for the eBPF tier,
`<YYYY-MM-DD>/<host>.jsonl` for the node tier. A `.gz` is a day the collector has
sealed; it never seals the node tier's.

`log/login` is last but load-bearing: `node_snapshot.py` is a vendored copy of
`collect/common/snapshot.py`, so the existing login feed is schema-compatible and works before
`ebpfm --node` is deployed anywhere.

Reading the collector's output uses a **separate** env var from the collector's own
`EBPFM_OUTPUT_DIR`, following `collect/healthcheck.py`: sharing one would let an operator shell
silently repoint the readers along with the writer.

### History in Parquet

The collector seals a day by gzipping its files once the day is over (`x.jsonl` to
`x.jsonl.gz`), and a sealed file never changes again. The service keeps each one as Parquet too
(`history`, on by default; `rc_dashboard/history.py`): `feedparquet`'s typed table per record
kind ([The feed as Parquet tables](#the-feed-as-parquet-tables)), every row carrying `_off`, the
offset of its line in the uncompressed file, and a manifest of what else the file could be asked
-- its size and complete end, its first 4 KB, where each line that is not a record lies. A
replica (`rc_dashboard/replica.py`) answers from those every question the readers ask of a
day-file, at the file's own offsets. Which reads take it:

| read of a sealed day-file | from its replica? |
|---|---|
| a filtered one: a view of some users or agent types, an events query or export by `user`, `host`, `agent_type`, `exit_code`, `signal` or `session_key`, the commands behind a clicked I/O figure, a session's process tree | yes: the filter, as a `where` (`where.py`), is tested on the columns, and only the records it passes are built |
| a search by time: where a window's read or a resumed one starts in it, and a filtered one's | yes: the same probes as in the file, with nothing inflated (the backfill and an hour's read search the `.gz`, whose seek index the read that follows needs anyway) |
| a read of everything in a range: the backfill, an hour's reduction, an unfiltered window or export | no: orjson makes a record of a line faster than Arrow builds one, so these read the JSONL |

A file with no replica yet -- not converted, or no longer the file its manifest names (its name,
size, mtime and inode) -- is read as JSONL, and so is one holding a line a JSONL reader would
read differently: a last line with no newline, or a line that is JSON but not an object. Nothing
waits for the store.

**The same answers.** A replica reads as its file does: the same records in the same order, key
for key and type for type, at the same offsets; a time search probe for probe; and a filtered
read stops at the record a line read stops at and counts what it counts (an events query's
`scanned`, and the session inspector's `bytes`, which its budget is checked against). So a
position, a saved build or an hour's span means the same thing whichever of the two a reader
took it from, and switching the store on or off, or moving it, rebuilds no saved window or hour.
`tests/test_replica.py` and `tests/test_where.py` check each answer against the JSONL, and
`tests/e2e_history.py` serves one feed with the store off and on and diffs every API and every
saved state -- with `--restart --seal-between`, resuming saved builds into a day sealed between
the two runs, through the replica on one side and the `.gz` on the other.

**The store.** `history.dir` (`RC_DASH_HISTORY_DIR`; `<cache.dir>/history` unless set) holds a
root per source root, made 0700 as the other caches are, since it holds every user's command
lines:

```
<dir>/ebpf/<root basename>-<sha1 of its real path, 10 hex>/
    SOURCE.json                                the source root it mirrors
    <table>/day=<D>/<host>.<stream>.parquet    the records, in file order, with _off
    _manifest/day=<D>/<host>.<stream>.json     the rest of what the file could be asked
```

It is a Hive-partitioned dataset as well, for a query of your own:
`read_parquet('<root>/exit/*/*.parquet', hive_partitioning = true)` in DuckDB. Row groups are
128 Ki rows: an hour of a busy login node's exits is one or two.

**Conversion.** In the background, `history.workers` processes (`RC_DASH_HISTORY_WORKERS`; 0, the
default, is up to 8, as the CPUs the service may run on allow) convert every sealed file with no
current replica, the biggest first, at startup and then every minute as days are sealed. A
replica whose source is gone is removed, and so is one older than the newest `history.days` dated
dirs when that is set. The node tier is never sealed, so it is never converted.

**What it costs and buys**, on FAS RC's mirror: 8 login nodes, the 7 sealed days 2026-11-10 to
2026-11-16 (112 day-files, 18.4 GB of `.gz`, 182 M records). Converting them took 12 minutes in 8
processes of up to 0.8 GB each, into 6.2 GB of Parquet, 34 % of the `.gz`. Each read below ran
in a fresh process, with the same answers both ways:

| | from the `.gz` | from the replicas |
|---|---:|---:|
| one user's exits on one sealed day, 500 rows, either order (the week's 128 day-files planned, as `/api/events` plans them) | 222-444 s | 13.6-15.4 s |
| a session's key search through the busiest day-file (17.4 GB inflated) | 30-43 s | 2.8 s |
| a time search in that file: the first, then each after | 12.3 s, then 0.1 s | 0.17 s, then 0.06 ms |

The `.gz` side inflates each file it places, whole, to learn its size and build its seek index:
the whole week, 367 GB, for the events query. A replica's reads build their own index, about 30
bytes a line (250 MB for the busiest file), kept for the next read up to 1 GiB and 64 replicas in
all, the least recently used let go first.

| setting | env | default | |
|---|---|---|---|
| `history.enabled` | `RC_DASH_HISTORY` | `true` | keep and read replicas; `0`/`false`/`no`/`off` reads every day-file as JSONL, and leaves the store on disk |
| `history.dir` | `RC_DASH_HISTORY_DIR` | `<cache.dir>/history` | where the store lives |
| `history.workers` | `RC_DASH_HISTORY_WORKERS` | `0` | conversion processes: 0 is up to 8, as the CPUs the service may run on allow |
| `history.days` | `RC_DASH_HISTORY_DAYS` | `0` | the newest dated dirs kept: 0 is every one the source holds |

The eBPF tier's entry in `/api/feeds` carries `history`: `enabled` and `state` (`off`, `idle`,
`converting`, or `error` with a `note`), `dir`, `workers` and `days`, `source_roots` and
`store_roots`, `replicas` (sealed files with a current replica), `files` and `bytes` (its Parquet
files), `converted` (since the service started), `lookups` (sealed files a reader asked for since
then: answered from a `replica`, or read as `jsonl` for want of one), `held` (the replicas held
between reads, and the `bytes` their indexes take), `swept` (half-written files a killed
conversion left, removed at startup), `progress` (the current round: `todo`, `done`, `failed`,
`records`, `bytes_in`, `elapsed_s`) and the latest `errors`.
`--check-feeds` says how many sealed files have a current replica and how much is left to
convert.

**EBR.** The collector's binary day-files (`.ebr`), which this service read until 2026-11, are
no longer written or read; a `.ebr` file under a root is passed over. A capture that exists only
as EBR reads again once converted to JSONL, by the last `ebr.py` there was:
`git show 2f16731:collector/lib/ebr.py > ebr.py && python3 ebr.py cat x.ebr > x.jsonl`.

## Two deployments on this host

| script | port | data | clock | risk profile | Slurm |
|---|---|---|---|---|---|
| `run-dev.sh` | 8086 | this dev cluster's live feed, `/scratch/marthen/ebpfm/{ebpf,login}` (rtx6000a) | wall | `gpu-dev.json` | off |
| `run-fasrc.sh` | 8085 | FAS RC capture, `/scratch/marthen/fas-rc-test-data` (boslogin06, 2026-10-08..09, `.jsonl.gz`) | pinned to the capture's end | `fasrc.json` | on |
| `run-demo.sh` | 8087 | a synthetic feed it writes itself, `.cache/demo/feed` ([Demo mode](#demo-mode)) | wall | built-in rules | on |

Both hot-reload as `./run.sh build`, from the same tree, and both serve names as aliases
(`RC_DASH_ANONYMIZE=1`, [Anonymized names](#anonymized-names)). `site.name` (`RC_DASH_SITE_NAME`) is
shown under the sidebar title and in the browser tab, so a frozen capture and a live feed open
side by side are never mistaken for each other. `slurm.enabled` (`RC_DASH_SLURM=0`) turns the
Slurm tab off and skips reducing it, for a cluster without Slurm; the Overview's "asked of Slurm"
rows go with it.

The FAS RC `sacct` export (`/scratch/marthen/agentic-hpc-measurement/raw_data/sacct`, one parquet
file a day, 2026-08-23..09-30) is not read: those days overlap neither deployment's eBPF data, so no job
id the collector saw can be joined to them.

## Demo mode

```bash
cd dashboard && uv sync && (cd web && npm ci && npm run build)
uv run python -m rc_dashboard demo        # http://127.0.0.1:8080
./run-demo.sh                             # the same with hot reload, on :8087
```

For showing the dashboard where there is no cluster: a laptop, a talk, a reviewer. `demo`
writes a few days of collector records for fictional login nodes into `.cache/demo/feed`, as
JSONL as a collector writes it, keeps appending to them in real time, and starts the
service, unchanged, over them (`rc_dashboard/demo/`). The tail, the backfill, every reducer,
the saved builds and the websocket run as they do on a cluster; the page is not fed from a
fixture, so what it shows is what the service makes of the records, and the Live tab moves.

**Nothing in it was measured, and it says so everywhere.** Every record carries
`"synthetic": true`, a key no collector writes. `health.py` notes each host whose records
carry it, the feeds report lists them (`synthetic_hosts`), and the header shows
*fake data* beside the live pill whenever any host's do; the Feeds tab marks each such
host. The site is named *demo · fake data*, and its description, which the Ask LLM
agent is given, says no record was measured. Because the mark travels with the records, a
service pointed at a demo feed by hand, or an export of one, still says it. Use a demo to
show the dashboard, never to report on one.

**What it writes.** Two hosts, `demo-login01` and `-02`, and 28 people: twelve with a part
to play (`demo/cast.py`) and a crowd. Their sessions are built from the argv shapes and the
schema-8 rules the collector and the reducers document, so each tab has what it is for:

| tab | what the demo puts there |
|---|---|
| Live, Resources | a working day and a quieter night: VS Code left connected, agents run from cron, an agent watching a job overnight |
| Agents | Claude Code in a terminal, `claude -p` and `codex exec` from scripts and from other agents' tool calls (nested), the desktop app's server and Remote Control hosting sessions, VS Code's Claude extension, the Codex app; writes outside the start folder for the blast radius |
| Sessions | sessions cut into tasks labeled in the agent's own words (commit subjects, a pull request's title, a `PROGRESS.md` entry), phases from explore to an edit-test loop, a Slurm job polled to its end |
| Risks | a year regridded on the login node, `pip`/`conda` installs, builds, `ssh` to compute nodes, `nvidia-smi`, `rm -rf` on shared scratch, credentials on command lines (masked, as schema 8 writes them) |
| Slurm | agents submitting with `--parsable` and polling the queue; people submitting arrays and checking now and then; refusals with the scheduler's own words |
| Internal (Sandbox) | Codex calls under `codex-linux-sandbox` (isolated, Landlock-only helpers, sandboxes that failed to start), Claude calls under `bwrap` |
| Feeds | per host: a heartbeat a minute, a nightly collector restart with its lost-event count, a kernel limit on one host |

Costs are drawn per command from ranges that look like a login node's; they make the
panels' shapes recognisable and are a model of nothing.

**What it overrides**, whatever the shell or the config file says -- `run.sh` exports the
cluster's feed roots, and a demo must never read them: both feed roots (the demo's own), the other feeds (a path in the
demo dir that does not exist, so they read *not set up*), `cache.dir` (so no real deployment's
saved builds are evicted), `clock.as_of` (the wall clock), `live.retention_min` and
`live.backfill_hours` (the history written), `panels.preprocess` and `panels.preprocess_wait_s`
(every window built before the port opens, so a visitor never lands on a *merging* bar), and
`site.name`/`site.description`. Each reads `demo` as its origin in `--print-config`.
`--ebpf-root`, `--node-root` and `--as-of` are refused rather than ignored.

| flag | env | default | |
|---|---|---|---|
| `--demo-days` | `RC_DASH_DEMO_DAYS` | 3 | history written before serving, 1..14 |
| `--demo-hosts` | `RC_DASH_DEMO_HOSTS` | 2 | login nodes, 1..6 |
| `--demo-seed` | `RC_DASH_DEMO_SEED` | `demo` | another seed is another set of sessions |
| `--demo-dir` | `RC_DASH_DEMO_DIR` | `.cache/demo` | the feed, the saved builds, the stamp |
| `--demo-fresh` | | | discard the feed and write it again |
| `--demo-write-only` | | | write the history and exit |

**Hosting it.** [`docker/demo/Dockerfile`](../docker/demo/Dockerfile) is the demo in one
image (`docker run -p 8080:8080`), with two days of history, Ask LLM off, and the people's
working day in New York time. [`deploy/cloudflare`](../deploy/cloudflare/README.md) puts
that image behind a Cloudflare Worker at <https://dashboard-demo.agentic-system.org>: one
shared instance that sleeps when nobody is looking and says it is starting while it wakes.
The real service over live collectors, behind a password, on a host of your own, is
[`deploy/fleet`](../deploy/fleet/README.md).

**Restarts resume it.** The feed is a function of the seed, the host count and the clock:
the same span cut into windows anywhere is the same bytes (`demo/world.py`;
`tests/test_demo.py` checks it). `demo.json` stamps how far the feed was written,
so a restart -- `run.sh` restarts on every save -- cuts whatever a stopped round left past that,
then writes on from it; nothing is written twice and nothing is missed. A stamp from another
seed, host count or format (an older demo's EBR), or one older than the history asked for,
starts the feed afresh.
One demo writes a directory at a time (`.demo.lock`), as one collector writes a host. Three days
of two hosts is 84 MB of JSONL, written in about 2 s.

## A capture from another cluster

A feed collected elsewhere and copied here -- `run-fasrc.sh` serves boslogin06's, on `:8085`
with the same hot reload as `./run.sh build` -- differs from a live one in two ways, and the
service handles both without touching a record.

**It is compressed.** A day-file may be `<host>.<stream>.jsonl.gz`, read in place
(`rc_dashboard/gz.py`), and once the history store has converted it, its replica answers the
selective reads ([History in Parquet](#history-in-parquet)). Every reader here
addresses a file by byte offset (the window plan's binary search, the read-back that walks from
the end, a saved window's resume, the tail),
and gzip offers none of that, so the first open inflates the file once and keeps a copy of the
inflater every 8 MB of output. Reaching any offset then inflates at most 8 MB, and offsets are
into the uncompressed stream, so a byte range or a saved build means the same thing for either
form. On boslogin06's 2026-10-08..09 (330 MB -> 7.9 GB) indexing takes ~6 s and holds ~32 MB;
it lives in the process, because an inflater cannot be pickled, so each restart indexes again.
A plain file wins over its `.gz` copy, concatenated members are followed, and a stream cut
short is read as far as it goes. A date dir holding no day-file is passed over rather than
counted as a day: a copied capture has dirs made ahead of the files, and counting them left
the 1 h view of the capture nothing to read.

**It is old.** On the wall clock every window would end in days of nothing. `clock.as_of`
(`RC_DASH_AS_OF`, `--as-of`) pins the service's present: `auto` is one second after the newest
record, or give a local time. The pinned clock is **frozen** (`rc_dashboard/clock.py` says why:
a running one would date every saved build in the future after a hot-reload restart, and
re-scan instead of resuming). Every live frame carries `clock`, the header reads *frozen
capture · as of ...* instead of *live*, and the page takes "now" from the server, not the
browser, so the events query, the export bounds and every "ago" agree with the windows.
Durations the service measures with that clock read 0 s.

### Testing against a capture

The pinned view is deterministic: the same windows over the same bytes on every restart. That
is what makes a capture useful as a test, at four levels:

| level | what it checks | state |
|---|---|---|
| the pinned view (`run-fasrc.sh`) | every tab on another cluster's data: its users, its kernel's feature set (4.18: no node tier, no `dstate_task`, no `tcp_accept`) rendering *not measured*, never 0 | running |
| scale | index, backfill and window-build cost at that cluster's volume -- boslogin06 writes ~2.7 M exit records a day, about four times rtx6000a | measured on the instance (`/api/feeds` `compressed`, `/api/windows`) |
| cross-check | a few totals counted straight from the `.gz` by an independent script (exits per class per hour, distinct users, agent sessions) against `/api/live` and `/api/panels` at the pinned clock | proposed |
| replay | the live path the frozen view never runs -- the tail following appends, WebSocket pushes, drift re-builds, the build/tail seam -- by writing the capture into a plain staging root at N x speed while the service's clock runs from the replay's start | proposed |

## The feed as Parquet tables

`rc_dashboard/feedparquet.py` writes day-files as Parquet: a typed table per record kind (`exit`,
`conn`, `netio`, `residency`, `residency_totals`, `submit`, `meta`, `stop`), a file per source
day-file and table, every row carrying its line's offset, from which every record reads back
exactly. The history store runs it on every sealed day-file ([History in
Parquet](#history-in-parquet)); by hand, for a capture of your own:

```bash
uv run python -m rc_dashboard.feedparquet convert --out /path/to/pq ROOT...
uv run python -m rc_dashboard.feedparquet verify  --out /path/to/pq ROOT...
```

The schema, its choices and what it measured on FAS's feed (a third of the sealed `.gz`, every
record of a week the same) are in [docs/feed-parquet](../docs/feed-parquet/README.md).

## Is each collector OK?

The **Feeds** tab leads with one row per host and a verdict an admin can act on, judged
only from what the collector wrote (`rc_dashboard/health.py`) -- the dashboard never
contacts a collector, so a capture copied from another cluster is judged exactly as a live
feed. The collector writes a `meta` record when it starts (pid, kernel, the probes that
loaded and the ones that did not, the sysctls they need), a `residency_totals` heartbeat
every minute, and a `stop` record when it exits cleanly (why, how long it ran, and kernel
events lost against seen).

| verdict | means |
|---|---|
| OK | a heartbeat within `feeds.ebpf.max_age_s` (5 min) |
| degraded | running, but losing data it could have: a sysctl a probe needs is off, or its last run lost more than 0.1% of kernel events |
| stopped | no recent heartbeat and a stop record after the last one; its reason is shown |
| silent | no recent heartbeat and no stop record: killed, crashed, or the host is down |
| no heartbeat | files for the host, but no heartbeat in what was read |

A probe the kernel cannot load (boslogin06's `tcp_accept`: no BTF on 4.18) is listed as a
**limit** beside the verdict but does not change it -- it is true of every run until the
kernel changes. A running collector's lost-event count is written only at stop, so the run
in progress shows "reported when it stops", never 0. Below the collectors: the data sources
(an optional one nobody set up reads "not set up", not red), the startup read, and the
resolved configuration.

## The time window

The window is chosen on the page — presets plus a custom entry (`90m`, `4h`, `2d`) — and it
lives in the query string next to the class filter, so a pasted link restores the span as
well as the view. It is `?win=<minutes>`; omit it and the service's own `live.window_min`
applies, which is why a bookmark made before this control existed still opens on 24 h.

**Retention and window are different numbers.** `live.retention_min` (default 7 d) is what
the process *holds* and the ceiling the picker can reach; `live.window_min` (default 24 h) is
only the view a fresh page opens on. Raising the view is free. Raising retention costs memory
and a longer backfill.

At ~1.5 KB per exit record a login fleet produces 10–20 GB/day, so raw events never sit in
memory. Four bounded paths:

| path | how a window change is served | storage |
|---|---|---|
| rolling aggregates | **sliced** — exact, instant, no re-read | per-minute buckets, one slot per minute of retention. 7 d ≈ 10 080 slots × a few counters. |
| historical reducers | **rebuilt** from the rows | quantiles, CCDFs, Gini, the dedup index and the trajectory chains carry no time index |
| event stream | cut to the window | a bounded tail of `live.event_tail` (500) — a tail, **not** an aggregate, so a wider window does not deepen it |
| search / export | `from`/`to` pinned to the window | on-demand re-scan, streamed, never buffered |

Slicing is exact because the bins are fixed-width and wall-clock aligned: the 1-hour view is
the last 60 rows of the 7-day view, to the record, and the headline under a chart is the sum
of exactly the bins the chart drew. A window **wider than retention is clamped and says so** —
drawing bins the process never held is the same error as imputing a zero.

Rebuilding costs the *window*, not the *day*. A day-file is appended in time order, so it is
sorted by timestamp and `export.seek_to_epoch` binary-searches the first in-window byte (in a
sealed file's replica, by its times, with nothing inflated): a 1-hour view of a 15 GB
day-file reads ~20 probes and one hour of rows. Files entirely older than the window are
skipped without being opened. The rebuild feeds records **oldest first**,
which is a correctness requirement and not a preference — the tool-mix shell-wrapper dedup
relies on a child's exit arriving before the shell that waited on it, and a trajectory's
run-length-encoded chain is appended in place.

Three things a rebuild reports rather than assumes:

- **Progress.** A wide window takes minutes, so panels render a *third* kind of nothing —
  neither `none` (which would assert a measurement) nor the empty state (which would blame
  the feed) — with a real percentage.
- **Drift.** A built view keeps ingesting, so it holds more than its own window. The excess is
  published in seconds and the view re-scans at `panels.max_drift_pct` (25%) of the window —
  proportional, so 1 h refreshes often and cheaply and 7 d rarely and expensively.
- **The seam.** A build reads the day-files while the tailer is also advancing through them.
  Rows landing in that seam are buffered and replayed; if the buffer overflows the count is
  published as `replay_dropped` rather than quietly going missing.

One pre-existing inconsistency had to be fixed for the control to mean anything: trajectory
sessions were evicted at `reducers.trajectories.idle_evict_s` (2 h) while the panel's own
`window` block claimed 1440 minutes, so "top trajectories in the window" actually ranked
sessions active in the last two hours. Eviction now never reaches inside the window
(`max(idle_evict_s, window)`), which makes the label true and makes
`reducers.trajectories.max_sessions` (150 000) the real memory bound — worth lowering if you
raise retention on a busy fleet, since it applies per held window. Past it, whole sessions go,
the least recently active first, each judged by its own newest record. A session there is its
process tree: schema 7 keys each tool call apart, so a session at work all day is hundreds of
keys, and a FAS login node makes 45 000 keys on a median day and up to 105 000. A key held
costs about 6 KB, so the cap is about 950 MB a window, which a 7 d view of a busy node
reaches; a 24 h view of one holds its day whole. (Under 50 000 no order could: on boslogin06
on 2026-10-28 the sessions active in the last four hours alone held 52 840 keys. And it used
to go key by key and against the clock: a window merged from its hours oldest first met the
cap with every session looking idle, and kept only each long session's afternoon.)

Built windows are an LRU of `panels.max_windows` (5), and builds are serialised on one worker:
two concurrent rebuilds would contend for the same page cache and each make the other look
slow. Ten readers on the same window cost one build, because the WebSocket sends one frame per
*distinct* window rather than one per client. The default window starts building with the
service, not with the first page load.

Two things keep a build from costing more than it must:

- **Only the records the panels read are parsed.** Each reducer names the event kinds it reads
  (`EVENTS`), and a scan passes over every other line -- `meta` and `stop`, and a kind no
  reducer knows -- on a byte match before `json.loads`. The match cannot miss a record -- a JSON
  writer emits `"event": "exit"`, give or take whitespace -- and a false hit only costs a
  parse. A view's users and agent types, the inspector's session keys, and the user,
  host and directory of the commands looked up behind a clicked I/O figure (`iocmds.py`) are
  byte checks too; in a sealed file's replica they are a `where` (`where.py`) tested on its
  columns, so a record they reject is never built.
- **Every build is saved, and resumed rather than repeated.** At the end of each scan the
  reducers are pickled to `<cache.dir>/windows/` together with exactly how far into each day-file
  the scan read: `(st_dev, st_ino)`, a digest of the first 4 KB, and the offset past the last
  complete line. A restart (and `run.sh` restarts on every save), or a return to a
  window the LRU evicted, loads that and reads each file on from its offset — only what the
  collector wrote since. On one login node's feed a 24 h window goes from ~7 s to ~0.2 s and a
  7 d window from ~42 s to ~1.4 s, plus the catch-up. A snapshot is ~12 MB per day of window;
  `panels.cache_keep` (8) bounds how many are kept.

A resumed view holds the records a service that had never stopped would hold, and reproduces a
fresh scan's panels exactly — checked on real data, and in `tests/test_window_cache.py`. It keeps
its original `built_at`, so its age and its drift read as they would had nothing restarted; the
catch-up *is* drift, and re-scans at the same budget. The snapshot is ignored, and the window
scanned from scratch, unless all of these hold — and the window status's `cache.note` says which
one failed:

- the same code and config (a fingerprint of the reducers, the normaliser, the scan and every
  config key outside `server`) — changing how a record is counted must never resume a reduction
  counted the old way;
- the same files: every one it read still there, the same inode *and* the same first 4 KB (a
  file deleted and rewritten can get its inode number back), at least as long, and a line ending
  at the saved offset. A day the collector has gzipped since (`x.jsonl` to `x.jsonl.gz`) is the
  same bytes at the same offsets under a new name and inode, so it is read on from its `.gz` (or
  its replica, which answers the same checks), known by its first 4 KB alone;
- built within the window's drift budget. An older one would be re-scanned the moment it caught
  up, so catching it up would only add to the cost.

A drift re-build always scans: it exists to move the window's start forward, which an older copy
cannot do. Pickle runs code for whoever can write the file, so the directory is created 0700,
files 0600, and a symlink or a file another user owns or can write is never loaded.

**The panels are saved too** (`payloads.py`). What a view materialises -- every reducer's
`result()`, credentials masked -- is written to `<cache.dir>/payloads/` after each build and each
refresh tick, with the span it covers. A view the registry creates (after a restart, or for a
window the LRU dropped) shows those panels at once, as a drift re-build shows its previous
reduction: their own `built_at`, coverage and drift, and the "rebuilding" pill, whose tip says
when they were saved (`cache.restored` in the window status). Its own build then swaps fresh panels
in. Saved panels are shown only for the same code and config (what a saved build and a saved hour
are keyed on), the same window and filter, and within the drift budget; otherwise
`cache.panels_note` says why not. They are a picture of what the page showed, never an input: no
build reads them. A day's panels on the two-host demo feed are ~0.4 MB, 0.12 MB compressed, and
load in ~5 ms.

`panels.cache_windows=false` (`RC_DASH_CACHE_WINDOWS=0`) turns all of it off.

### Hourly merge-reduce

With `panels.merge_reduce` on (the default; `RC_DASH_MERGE_REDUCE=0` turns it off), a record is
parsed and reduced **once**, into the state of the epoch hour it belongs to, and that state is
saved under `<cache.dir>/hours/` (`hours.py`). An unfiltered window is then the records from its
start to the first whole hour, read as before, so the start is exact rather than snapped to an
hour; every closed hour inside it, merged from their saved states (whole epoch days from one saved
merge of their 24 hours, `panels.day_cache`); and the hour still open, read as before. The view
is live-fed as always. Every window reducer has `merge(other)`, which folds a later state into its
own exactly as feeding it the later records would have, so a merged window is the window one pass
builds (`tests/test_merge.py`, `tests/test_hours.py`; checked on the real feed). The quantile panels
are now a mergeable sketch of every value rather than a capped sample: `n`, `max` and `mean` exact,
each quantile within 1% (`rel_err`), `sampled` false.

Joins that cross an hour -- the shell-wrapper dedup, the process index's parent requests, a
sandbox wrapper absorbing its payload, the keys the workspace has admitted -- are carried from
each hour into the next one's reduction, so the hours are reduced in order, and a merged window
takes the join state its last hour ended with. That carried state is aged out after 24 hours
untouched, which is the one place a merged 7 d window differs from one pass over 7 days: it no
longer holds week-old pending requests, which on rtx6000a made one pass refuse a few legitimate
parents whose pid an older process had used. An hour closes once the newest record is
`panels.hour_grace_s` (300 s) past its end; a record that arrives for a closed hour is found by the
next hour's read and merged into its hour's saved state, not lost. On one login node's feed a 24 h window then builds in ~2-3 s and a 7 d one in
~8 s after a restart, a window switch or a drift refresh (against ~15 s and ~160 s scanned);
filling the hours the first time costs about what one scan does. A week of hours is ~38 MB on
disk. A view filtered to users or agent types is still scanned.

**Reducing by node, in parallel (`ingest.workers`).** A reducer is pure Python and holds the
GIL, so the service's threads share one core: one pass over an hour of the eight FAS login nodes
(1.45 M records, 2.7 GB of JSONL) takes ~95 s of it, and decoding the records is under 10% of that
-- the reducers are the rest, whatever the format. With `ingest.workers` at 2 or more
(`RC_DASH_INGEST_WORKERS`) each node's share of an hour is reduced in a worker process of its own,
at most that many at once, and the hour saved is the node states merged in node order; 0 is one
per CPU the service may use. The joins stay exact, since a process, its session, its tree and its
wrapper are all on one node: each node carries its own join state from hour to hour (a chain
holds one per node), and a window installs them as one. Nodes run ahead of one another by at most
four hours, so a quiet node does not wait on a busy one.

**And by reducer group (`ingest.reducer_groups`, 2 by default).** A node's hours are chained, so
one node is one process at a time and the busiest node sets the pace. With 2, each node's hour is
reduced in two processes, one per group of reducers that share nothing (`hours.reducer_groups`):
the session reducers and the process index they fold through (`_procs`, `trajectories`,
`workspace`, `sandbox`, `slurm`, `cascades`), and the counting panels (`resources`, `io_process`,
`tool_mix`, `risk`, `facets`). No reducer writes into the records it reads, so each group reads
the node's records itself, in order, and the two states put together are the node's one-process
state: the split changes no number and no saved hour (`NodeSplitTest`, and every panel of a real
hour). The second read costs CPU, so a node is split only when there are at least as many
workers as nodes. Measured on rtx6000b (32 cores, other users' load beside it) over the eight
nodes' real feed of 2026-11-16, Python 3.14:

| workers (reducer groups) | 1 | 4 | 8 (1) | 8 (2) | 12 (2) | 16 (2) |
|---|---|---|---|---|---|---|
| 15:00, a balanced hour (1.45 M records) | 90 s | 28 s | 22 s | 18 s | **15 s** (6.2x) | 15 s |
| 11:00-14:00, boslogin07 at 2 GB/h (4.94 M) | 367 s | 120 s | 119 s | 111 s | **92 s** (4.0x) | 101 s |

The balanced hour is bound by boslogin06 and the merge into the hour (~4 s an hour in the
service's own process); the spike hours by boslogin07, whose records cost more to read than to
reduce, so its second read buys less. Two workers per node -- one per node and group -- is the
most that helps; each is ~0.5 GB (16 of them ~7.4 GB, plus ~1.7 GB in the service while it
merges), and they count against the service's memory cap. The FAS deployment uses that
(`INGEST_WORKERS` in `deploy/fleet/site.env`, empty for two per node).

**Python 3.14.** The same reductions run 5-10% faster on CPython 3.14 than on 3.12, and every
panel is the same (compared field by field on a real slice); the test suite passes on both. Its
experimental JIT (`PYTHON_JIT=1`) was slower here, and so was PyPy 3.11, whose panels also
differ in float rounding. The free-threaded build (3.14t) does run one node's groups as threads
in parallel, exactly, but each thread is ~18% slower and orjson has no free-threaded build, so
the processes above are what the dashboard uses.

What a merge of two nodes' states of the same hour can take differently from one pass is what
`merge` takes from one state rather than adding: a "newest record's" field (a user's class in the
I/O panel), a home learned from one node's argv no longer places another node's paths, the rows
that tie at a cap (the 200 Slurm sessions), and float sums in another order (the last digit). The
"first N" samples (the risk panel's instances, a Slurm rejection's example) keep the earliest in
time. Counts, sums, sets and the quantile sketches are the same, and so is every panel for any
worker count from 2 up (`tests/test_hours.py`, `NodeSplitTest`). The join caps (`max_parents` and
the like) hold per node, so fewer pending joins are evicted. A window takes the nodes' join
states as one pass's, since its tail and live feed are one pass over every node
(`hours.combine_carries`): each capped map is interleaved by the hour each key was last
touched in, each node's own order kept, and held to one pass's cap, its oldest dropped. The
split is in the hours'
fingerprint and the count is not: going from 1 to more, or back, reduces the hours afresh;
changing the count does not. The workers are spawned once and kept (`hours.status` reports
`node_workers`, `node_cpu_by` per node, `node_merge_s`), and they count against the service's
memory cap. A worker runs the service's own code and Python, or nothing: a process spawned
starts from `sys.executable` and the service's `sys.path` as they are then, so a deploy since
the start (another release where the service imports from, a venv made again, a package
upgraded) would have it reduce with other code (`hours.spawn_identity`). The service checks
before it starts a pool, and each worker on its own start; after such a deploy the nodes are
reduced in the service's own process, on one core and into the same hours, until it restarts
(`node_note` says so). The fleet's install restarts the dashboard when it makes the venv again.

**Preprocessing everything.** By default only the windows a reader asks for (and the cheap
`panels.prewarm_min` ones) are built, and only the hours inside them are reduced, so the first
switch to a wide window still reduces and merges while the page says *merging*. With
`panels.preprocess` on (`RC_DASH_PREPROCESS=1`) the service instead, at start-up, reduces every
closed hour in retention and merges its whole days, then builds **every** preset window and holds
them all (`max_windows` is raised to fit). With `panels.preprocess_wait_s` (`RC_DASH_PREPROCESS_WAIT_S`)
above 0 it does that before it opens its port -- uvicorn binds only once start-up returns -- so
the first page finds every window ready; past that deadline it listens anyway and the builds carry
on. Off by default because a held 7 d window on a busy node is up to ~1 GB; the demo turns both on.

## API

| endpoint | purpose |
|---|---|
| `GET /api/config` | resolved config, `_origin` per key |
| `GET /api/feeds` | per feed: configured vs resolved paths, status, lag, rows, notice, backfill; for the eBPF tier, `history`: the store of its sealed days as Parquet, how far it has converted them and how many reads it answered ([History in Parquet](#history-in-parquet)) |
| `GET /api/collectors` | per host: is its collector OK -- verdict and reasons, heartbeat age, the run in progress, every run and how it ended (events lost), probes that could not load (`health.py`) |
| `GET /api/windows` | offered windows, the retention ceiling, and every held window's build state |
| `GET /api/panels?window_min=&user=&agent_type=` | historical aggregates for that window + its build state; `user` / `agent_type` (repeatable, `none` = untagged) narrow it to a view of its own, built and saved like a window; `filter` names it, `facets` lists the window's users and agent types |
| `GET /api/live?window_min=` | rolling window: per-minute bins, plates, hosts, event tail, submits |
| `GET /api/trajectories?rank=&window_min=&user=&agent_type=` | ranked sessions in that window, narrowed the same way; an agent's row carries its tasks and phases ([below](#sessions-tasks-phases-and-processes)) |
| `GET /api/trajectory?sid=&window_min=&from=&order=` | one session as a **process tree**, re-read on demand, `oldest` or `newest` first (below) |
| `GET /api/users?window_min=&user=&agent_type=` | every user with an agent session in the window, one row each, the window's totals (`fleet`), the agent sessions of no named user (`unattributed`), and the traits' rules ([below](#users-a-portrait-of-each-user)) |
| `GET /api/user?name=&window_min=` | one user's portrait: their row read against the window's users, rhythm by hour, calls and time by phase, projects and tasks, the scheduler, and their sessions; `name` takes the alias when names are anonymized |
| `GET /api/events?<filters>` | paged event query |
| `GET /api/export?<filters>` | **streaming filtered raw JSONL** |
| `WS /ws` | push: `live`, `feeds`, `rebuilt`, `backfill`, `windows`; accepts `{"type":"window","minutes":N}` |
| `GET /login`, `POST /login` | the sign-in form, and signing in: a session cookie, then back to `next` ([Password](#password)) |
| `POST /logout` | signing out: the cookie cleared, then the form |

With a password set, every route above asks for it ([Password](#password)).

`window_min` is optional everywhere and omitting it is `live.window_min`, never an error, so a
script or bookmark that predates the control keeps the view it had. A value above retention is
clamped, and the response carries `requested`, `clamped` and a `note` saying why.

`/api/trajectory` (`rc_dashboard/proctree.py`) is the inspector behind a clicked trajectory row.
No reducer holds per-process rows -- one day's busiest session on rtx6000a ran 1.59 M processes --
so it re-reads the window's day-files for exactly the records the row folds (their `_sid`), through
the same normaliser scan a window build uses, with a byte pre-check on `session_key` (in a sealed
file's replica, a test of its `session_key` and `agent_pid` columns before a record is built) so
the rest of the file is
skipped before parsing. Parents come from each record's own `ppid`; a parent placed on
weaker evidence (the process index, the collector's `depth`, the fold) says so in `link`, and one
with no exit record in the read is drawn without one. Consecutive siblings of one shape fold into
counted groups. Every bound is in the response, never applied quietly: the read stops at
`reducers.trajectories.inspect_max_records` (150 000) or `inspect_max_bytes` and says where
(`scan.stopped_at`; `from=` continues from it), at most `inspect_hold` (20 000) processes and
`inspect_per_parent` (2 000) childless ones of one command per parent are itemised and the rest
counted under their parent, and `inspect_nodes` (6 000) rows are served breadth first with the rest
counted. Command lines are masked (`redact.py`) and then cut to 220 characters. One read runs at
a time, and its tree is served again for ten minutes (`CACHE_TTL`) while the window is the same
build.

**How a read is made.** Most of a large session's read is building its records and normalising
them -- 44 us a record on FAS, against 6 us for the tree they feed -- so that is done in
`reducers.trajectories.inspect_workers` worker processes (8, and no more than the CPUs the
service may use; 1 builds them in the service's own process), a part of each day-file each: a
replica's row groups, or a plain file's 32 MB chunks (`keysearch.py`). A record comes back holding
only the fields the tree reads. The parts are replayed in file order with the counters the
one-process read keeps, so every bound above stops a read at the same record either way
(`tests/test_keysearch.py`); a read that stops early is cancelled in its workers, so the next one
does not wait behind it. A sealed day-file with no replica is read in the service's own process,
as before. And a row's summary takes the agent's other days from the ranking's last fold rather
than folding every session of the window again under the service's lock. On FAS's boslogin06,
2026-11-16, read as a live window is (its fold stale), a 150 000-record read went from 9.9 s to
2.5-2.7 s, and a 3 121-key agent's from 13.2 s to 2.2-2.6 s; newest first, 149 573 records on
boslogin05 from 12.1 s to 5.3-5.5 s, the tree's own keeping of the newest now the most of it.

Each of those bounds keeps one end of the session and counts the other, and by default all of
them keep the **oldest**, the order the records arrive in. `order=newest` moves every one of them
rather than sorting the result. Siblings are listed latest start first, and the rows are served
breadth first in that order, so the row budget cuts the deep end of the oldest subtrees. A process
that finds the hold full, or its command's quota under one parent, is drawn anyway, and the oldest
drawn one is counted under its parent in its place. The read goes on to the session's end, up to
`inspect_newest_records` (600 000): however much it reads, it keeps only the newest
`inspect_hold` drawn, so that budget bounds time alone. Totals are the same in either order.

Filters are shared by `/api/events` and `/api/export`, so **the download is exactly the view**:
`from to class[] agent_type[] user[] host[] tool[] purpose[] bucket[] session_key sandbox
approval depth_min depth_max cpu_min duration_min exit_code signal outcome[] q`. `outcome` is
how a process ended, in the words of [Commands that failed](#commands-that-failed): `ok`,
`nonzero`, `answer`, `signal`, `pipe` or `none`.

The export's first line is a `_meta` header (filters, window, feed paths, schema versions) and its last is a `_trailer` (row count, whether the cap truncated it).
Body rows are the
collector's own fields plus additive `_`-prefixed derived keys — originals unmodified,
except credentials.

**Credentials never leave the service.** Tokens, passwords, keys and signed URLs on a command
line are replaced by `[REDACTED]` on every way out (built panels, the live frame,
`/api/events`, `/api/export`, and the `q` search, which matches the masked text), keeping the
flag or variable name so a reader can see what was there. Records stay raw in memory, because
the secrets, sandbox and approval detectors need to read them. The rules and their measured cost
are in `rc_dashboard/redact.py`.

## Conventions the panels obey

These are research-integrity rules, not preferences. Each one is a mistake this project has
already made once.

- **Three classes, never two** — `agent` / `human` / `ide` (+ `unlabeled`). `ide` is any
  editor a person drives: the VS Code, Cursor and Windsurf remote-server trees the collector
  tags (it calls the first `human-vscode` and counts the other two as agents), plus other
  editors' servers recognised by their own executable path (`normalize.ide_class`). An agent
  CLI started in an IDE terminal keeps its own agent type and stays `agent`. Folding the IDE
  class into "agent" is what produced a retracted GPU near-parity claim. `actor3` is
  *derived* via `agent_lib.actor3`, never approximated from the binary `actor`, which would
  silently empty the IDE class.
- **Count and cost, side by side** — every categorical panel ships an event-weighted and a
  CPU-weighted chart on separate single axes. Never a dual axis. You can see why immediately:
  `poll/probe` is ~28% of an agent's tool calls and ~0% of its CPU.
- **Null is not zero** — a dropped BPF block emits `null`. Nullable fields carry
  `coverage_pct` and are never imputed. A field present-but-always-exactly-zero is flagged
  `all_zero` and renders **not measured**.
- **Zero is not missing** — a present feed with no matching rows says `none`; an absent feed
  names its configured path. Different states, different renderings.
- **`sandbox` and `approval` are independent axes.** `--dangerously-skip-permissions` bypasses
  approval but leaves the sandbox intact; `--dangerously-bypass-approvals-and-sandbox`
  disables both.
- **`sandbox` is namespace isolation, and it is labelled so.** The collector compares
  `/proc/<pid>/ns/*` with pid 1's: Landlock and seccomp-only sandboxes are invisible to it,
  and a container, an apptainer user namespace or a `PrivateTmp=` unit counts as sandboxed.
  The key `sandboxed` is shown as "sandboxed", and the glossary's line for it says how it is
  read. It has **four** states — `denied`,
  shown as "exited before it was checked", is the namespace links being gone when read (the ebpf tier
  is root; every such record on rtx6000a lived under 6 ms) and is never folded into
  `unsandboxed`. Every share is taken over the KNOWN verdicts, with coverage beside it.
- **A session is judged by its tool calls.** Claude Code and Codex wrap each tool call and
  never themselves, so the session root's own verdict says nothing about confinement. A tool
  call is a sandbox wrapper invocation (bwrap / codex-linux-sandbox, nested ones folded into
  the outermost) or a Claude Code Bash shell no wrapper encloses; its outcome is `isolated`,
  `not_isolated`, `failed` (the wrapper exited non-zero and no payload ran — tried, and the
  sandbox did not come up: all 612 codex-linux-sandbox runs on rtx6000a), `no_payload` or
  `unknown`. A wrapper's own record is read before it unshares, so it always reads "not
  isolated" and is not a "capability probe"; what ran under it decides the call.
- **Approval is read off the session root's own argv, and says no more than that.** Only a
  process whose role is `agent` (and not an IDE's) carries a verdict, from exit records and
  from its residency / conn / netio identity blocks — a running VS Code-launched Claude is
  only ever seen in its own `conn` records. States: `bypassed` (a bypass flag at launch),
  `auto` (`--permission-mode auto`), `supervised` shown as "no bypass flag at launch"
  (settings files, codex `config.toml` and a mid-session switch are invisible),
  `unobservable` (`codex app-server` takes its policy per thread), `na` (a tool shell, probe,
  IDE helper or human process) and `unknown` (a child: its verdict is on the root).
- **Where a session is driven from is read off the same argv, and `unknown` is never
  guessed.** `normalize.surface_of` gives every agent session root one of five surfaces in
  two groups. CLI: `terminal` (the agent's own interactive UI, `claude daemon` background
  sessions included) and `script` (`claude -p`, the Agent SDK's stream-json, `codex exec`,
  an MCP server). App: `desktop` (`~/.claude/remote/ccd-cli/`, the Claude desktop app's Code
  tab over SSH; `codex -c features.code_mode_host=true app-server --listen unix://`, the
  Codex app's Remote SSH), `extension` (an IDE extension's bundled binary under
  `.vscode-server/extensions/` and the like) and `web` (Remote Control: `--sdk-url`,
  `app-server --remote-control`). A mode decides before a path: the extension runs its
  binary only as Claude's stream-json, Codex's `app-server`, or with no mode and no
  terminal, so its `codex exec` run by hand from a terminal is a `script`. A Codex
  `--managed-daemon` app-server serves whichever
  client attaches, so its argv names none: it takes its terminal UI's surface when that UI
  started it, as it takes its approval (`cascades.py`), and is otherwise `unknown`. A host
  (the desktop app's `~/.claude/remote/srv/` server, `claude rc`, `claude daemon`) is never a
  session, but a row or a box keyed on one says where its sessions are driven from. Shown as
  a chip on agent boxes, session rows and live sessions, and as the terminal-or-script / app
  split on the Sessions tab's plate; there is no filter on it yet.
- **CPU is `sum(cpu_s)`, self, once per process.** `child_cpu_s` is never added:
  `analyze/common.py:364` measures the naive sum inflating login CPU 1.83× and *unevenly by
  class*, which would invert an agent-vs-human comparison. `schedstat_ratio` is emitted so a
  regression in that basis is visible on the page.
- **A process is counted once, at its exit.** At each stop the collector writes a `truncated`
  record for every process it tracks that is still running, with its whole life's CPU and I/O so
  far, and that process's exit after the restart carries them all again. So every count and sum
  takes `exit` records only. A `truncated` one still names a running agent root (its launch, its
  approval), and the session inspector draws it as alive at the collector's stop, but it adds to
  no total.
- **Disk I/O is each process's own.** The collector's `io` is `task->signal->ioac`, which the
  kernel also credits with every child the process reaped, so a shell's figure holds its
  commands' and an agent's every tool call's. Every figure that sums disk or syscall bytes -- the
  I/O panel, the tool mix, the I/O series, the blast radius, the sessions, the live roster and its
  rows, the inspector's subtree totals, the commands behind a number -- takes each process's
  figure less what its exited children carried (`reducers/own_io.py`: one join per reducer,
  carried across hours like the other joins; the live tier's backfill, which reads newest first,
  takes each child off the parent it met first). So a command's bytes count once, not once per
  process above it. A child whose exit was not read is not taken off: one outside the window, or
  one a filter leaves out. Nor is one its parent cannot have reaped: a child that outlives its
  parent still names it, and a later process may get that pid, so a process takes off only its
  own user's children (root aside) that began no more than a minute before it did (its recorded
  start is its last exec); the rest were counted at their own exits and are dropped, counted.
  The record has no fork time apart from that exec, so a launcher that forks a child and execs
  over a minute later is refused it: that child's bytes count twice, and the refusal is counted
  in `rejected` (on four million records, one child in 2.7 M began a minute to an hour before
  its parent's recorded start). A figure short of what its children carried shows as 0, never
  below, and is counted too. Each join holds at most `reducers.own_io.max_parents` parents
  (600), the one given to least recently dropped first, counted (`evicted`) and said on stderr
  (the first time, then at most hourly a panel), its children's bytes then counted again at its
  exit. A service holds some 35 joins (five in each held window, five carried in the hours'
  chain, the live tier's two, an inspector read's and a lookup's) at up to 2.5 KB a parent, so
  600 is at worst ~48 MB; the live tier's notes add at most ~10 MB. The repo's streams hold
  under 500 parents at once and lose nothing at 100; the bench's feed, 32 times the demo's
  records, sums 1.9% more than an uncapped join at 600 and the same from 800. A busy login node
  needs more: raise it while `evicted` grows, at up to 80 KB for each one added.
- **"Top" is seven rankings, not one** — `events`, `cpu_s`, `peak_rss_mb`, `io_mb`, `net_mb`,
  `distinct_tools` and `recent`, each with `dominant_user_share_pct`, because a single ranking
  is dominated by one poll loop and one mega-user. `recent` lists real sessions only: a
  status-bar probe, or a tool call whose shell has not exited yet, would otherwise hold the
  top. The list is re-ranked from the live feed when read (`reducers.trajectories.live_s`,
  5 s), and the page re-reads it that often while it is on screen.
- **Scope is stated** — the eBPF tier runs on login nodes, so its resource figures are
  login-node use, not cluster use.

## Risk rules differ by cluster

What counts as risky is a site's call: nvidia-smi is a pointless question on a GPU-less login
node and a sensible one on a GPU dev box, compute nodes are named differently everywhere, and a
site has its own tools and token formats. The rules are built in (`reducers/risk.py`, which
documents every key) and changed per cluster in the config's `risk` section, or in a **profile**
file merged over it: `RC_DASH_RISK_PROFILE=risk-profiles/gpu-dev.json`.

```json
{
  "vars": {"compute_hosts": "holy|bos|node|gpu|c\\d{3,}"},
  "dangerous": {
    "disable": ["nvidia_smi_login", "nvcc_login"],
    "override": {"pip_install": {"severity": "low"}},
    "add": [{"id": "rclone_sync", "label": "rclone sync on login", "severity": "med",
             "pattern": "(?:^|/)rclone\\b[^\\n]*\\bsync\\b"}]
  },
  "secrets": {"add": [{"id": "site_token", "label": "site token", "severity": "HIGH",
                       "pattern": "\\bRCT-[A-Z0-9]{12}\\b"}]}
}
```

`{{name}}` in a pattern is a `vars` entry. A changed pattern, an added rule, or a variable that
changes a pattern runs without the built-in's pre-check (a cheap literal test that a stale one
would turn into missed matches). A rule that does not compile is left out and named on the Risk
tab, never fatal. A secret pattern a cluster adds is also **masked** everywhere the page shows a
command line (`redact.extend`), so a credential the panel counts is never shown elsewhere. The
profile's content is part of the resolved config, so changing a rule re-scans the saved windows
instead of resuming counts made by the old one. The Risk tab's **Rules in effect** lists every
rule, its pattern, and whether it is built in, changed, added or switched off.
The tab itself is modular: `risk.sections` lists its sections in order -- compute on a login
node, groups of command rules, credentials -- so a cluster drops the ones it has no use for (an
unlisted section is not computed) or splits rules into sections of its own.
`risk-profiles/fasrc.json` (FASRC login nodes; `run-fasrc.sh` uses it) and `gpu-dev.json`
(a GPU box that is also the login node) are shipped; **[`risk-profiles/README.md`](risk-profiles/README.md)**
documents every key and every built-in rule, and [`example.json`](risk-profiles/example.json) uses
every key, annotated.

## Anonymized names

`anonymize.enabled` (`RC_DASH_ANONYMIZE=1`) replaces every username, lab or Slurm account name,
email address, and numeric user ID in what the service sends out -- every HTTP response,
websocket frame, export line, and the Ask-LLM snapshot -- with a stable alias: `user-7f3a9c1e`,
`lab-1b2c3d4e`, `[email]`, and `[uid]`. It is off by default, so a deployment for the cluster's
own staff shows real names. `run-dev.sh` and `run-fasrc.sh` leave it off (user, 2026-10-13), and
the tunnel serves their pages only to readers who sign in ([Password](#password)). The header says
*anonymized* while it is on.

- **Stable, not reversible.** An alias is 8 hex characters of an HMAC under a secret salt:
  `anonymize.salt`, or else `<cache.dir>/anonymize.salt`, generated once with mode 0600. One
  account reads the same in every tab, window, and restart, and without the salt a list of
  candidate names does not turn an alias back. Changing the salt reshuffles every alias.
  Neither `/api/config` nor `--print-config` shows it.
- **What is a name.**
  - Every account the live roster holds or a `user` field names, plus the accounts the
    config's filters name.
  - Any segment of a home or lab path, masked where it stands even for someone who never ran
    anything here: `/n/home08/<name>/`, `/n/holylabs/<lab>/Lab/<name>/`, Claude Code's
    `-n-home08-<name>-` spelling of a folder, and `~<name>/`, as the Agents tab's workspace
    writes another user's home (`anonymize.TILDE_HOME`, applied whatever the home patterns say).
  - Labs, from a Slurm `--account` or `-A`, from `<x>_lab`, and from lab paths.

  Names match as whole words, a dotted one also in its hyphen spelling, and a name clipped off
  the end of a long command line matches by its prefix. The patterns are
  `anonymize.home_patterns` and `anonymize.lab_patterns`; the defaults
  (`anonymize.HOME_PATTERNS`) know FAS's layout. System accounts (uid below 1000),
  `anonymize.keep` (`root`), the class names, and the agent types are never aliased.
- **Where.** At the exits, after the reducers, like `redact`: the default response class, plus
  the websocket frames, the export stream, and the gzipped trajectory body. Reducers and saved
  state keep raw names, and `anonymize` is left out of both saved-state fingerprints, so
  switching it is a restart, never a rebuild. Anonymizing that fails is a 500 with no body,
  never the raw payload.
- **Requests.** A `user` filter, the user drill-down, and both searches take aliases. A real
  name matches nothing, so the page cannot be used to ask whether someone is here.
- **Not covered.** A person's name in free text that no rule ties to an account (a commit
  message, a folder named after someone), hostnames, partitions, and job ids. In `/api/health`,
  `anonymize` counts the names it knows, the collisions (two names, one alias), and the
  accounts left visible because their name is a reserved word. It never shows a name.

`tests/test_anonymize_leak.py` starts the app over a feed full of names, then reads every route
and the websocket. No byte may carry one.

## Password

`server.auth.password_hash` (`RC_DASH_PASSWORD_HASH`) puts the whole service behind one
password: the page, its assets, every `/api` route (Ask LLM included), and the websocket. It is
empty by default, which leaves the page open to anyone who reaches the port.

```bash
cd dashboard && .venv/bin/python -m rc_dashboard new-password --netrc-host dash.example.org
```

makes a 16-character password with `secrets`, from upper and lower case, digits, and 18
symbols, with each class present: about 101 bits. It writes the password to
`~/.config/rc-dashboard/password` and a netrc for curl beside it, both mode 0600, and prints
only the salted scrypt hash. Put the hash in the gitignored `dashboard.config.json`, then
restart:

```json
"server": { "auth": { "password_hash": "scrypt$32768$8$1$..." } }
```

- **Browsers** get a sign-in form, with a 401, at whatever URL they asked for, and land back
  on it once signed in. The session is a cookie that lasts `server.auth.session_days` (30). It
  is HttpOnly and SameSite=Lax, Secure when the request came over https (a proxy such as
  cloudflared says so in `X-Forwarded-Proto`), and named `rc_dash_<port>`, so two instances on
  one host keep separate ones. *Sign out*, at the foot of the sidebar, ends it. A page whose
  session runs out goes back to the form on its next request.
- **Scripts** send the password as HTTP Basic auth, with any user name:
  `curl --netrc-file ~/.config/rc-dashboard/netrc http://127.0.0.1:8085/api/health`. The
  password then never appears on a command line, where `ps` would show it to other users.
- **What the service keeps.** Only the hash. A password is checked with scrypt, one check at a
  time, compared in constant time, and a wrong one waits a second more. The cookie is an expiry
  and its HMAC, under a key drawn from `<cache.dir>/session.key` (made once, mode 0600) and the
  hash, so a new password or a deleted key file signs everyone out. `/api/config` and
  `--print-config` show the hash as `(hidden)`.
- **A restart, not a rebuild.** `server` is left out of both saved-state fingerprints, so
  setting, changing, or clearing the password costs a restart. A malformed hash stops the
  service at start rather than serving it open.

`tests/test_auth.py` starts the app with a password, then reads every route, an asset, and the
websocket, with and without it.

## Network endpoints

The Network bytes panel, in the I/O section of the Resources tab (under *Login nodes* in the
rail), lists beside its bars the ten remote endpoints the processes made the most send and
receive calls to. A switch ranks them by bytes sent and received instead. Each row splits its
calls or bytes by class, and the list follows the who filter and the class choice like the bars.
It is counted from the collector's `net_endpoints` block (schema 7) as each process exited, so a
capture from an older collector shows no list. Demo mode turns the lookups below off, so its
made-up endpoints keep the host names its records give.

- **One row per name.** The I/O reducer adds up the calls and bytes of every remote address
  (`reducers/io.py`), and the service names each address when the panel is read
  (`endpoints.py`): by a reverse-DNS lookup from this host, else by a name a process's command
  line gave for it (the collector's `host`), else by the address itself. Addresses with one name
  are one row. Inbound endpoints, whose address is a client's, stay apart from outbound ones.
  Hover a name for its addresses, its ports, and where the name came from.
- **Lookups never hold up a request.** A few background threads look the addresses up, the
  busiest first, and keep the answers in `<cache.dir>/rdns.json` (mode 0600), so a restart asks
  nothing again. A name is asked for again after a week, "no name" after a day, and a lookup
  that failed after ten minutes, with the old answer served meanwhile. While names are pending,
  the page reads the panels again a few times. The lookups are this host's: a private address
  in another site's capture is named as this host's resolver names it.
- **Exact for any mix of classes.** The service sends the top ten by calls and by bytes for
  every mix of classes, so the page can rank the classes it shows and still hold their whole top
  ten.
- **What it cannot show is said.** The note under the list gives the share of the bars' bytes
  whose endpoint is known, the bytes a block held with no address, and the processes that moved
  bytes with no endpoint block at all.
- **A restart, not a rebuild.** `server.rdns.enabled` (`RC_DASH_RDNS`, on by default) turns the
  lookups off, and names then come only from command lines. `server.rdns.workers` (4) sets how
  many run at once. A name is applied when the panel is read and never saved in a reducer's
  state, so neither setting touches a saved build.

`tests/test_endpoints.py` covers the names, the lookups, the grouping, the ranking, and the
served payload, and `tests/test_io.py` covers the reducer.

## Login node health

The node tier is what the eBPF tier cannot see, because none of it is a process: the load and
memory of the whole node, how quickly it answers, its NFS mounts, its local disks, and each
account's cgroup. The `node` panel (`reducers/resources.py`, `NodeReducer`) is its newest two
snapshots per host, and the Resources tab draws it first, as **Login node health**: one table at a
time -- nodes, NFS mounts, local disks, accounts, stuck processes -- each choice with its count and
the count of what is wrong in it.

- **The service holds the lines.** A local filesystem at or past `node.disk_full_pct` (90) of its
  space or `node.inode_full_pct` (90) of its file slots is `full`, and the panel says which line in
  `thresholds`. Needs attention draws the same flag.
- **An account held back** is one whose cgroup's `throttled_usec` grew between the two snapshots:
  the kernel paused it at its CPU limit. Its CPU is a rate over the same gap, as cores in use
  (`cpu_cores`), null with one snapshot, and `counter_reset` (no rate, not an idle one) when the
  cgroup was made again in between.
- **Stuck processes** are the snapshot's `d_state_procs`, up to 20 per host with the rest counted:
  pid, account, the wait channel, the kernel stack's top frames where the collector could read it,
  and the command line, masked here (`redact.py`) because this panel is served as built, not
  through a window's redaction. The eBPF tier's `d_stack_top` -- what its tracked processes were
  blocked in -- rides on the live frame's host rows, and the D-state count's hover shows both.

### Over the window

**Login node response**, on Live under the window's CPU chart and zoomed with it, draws four of
the node tier's numbers per host over the window: the time to start a process and to look up an
account (the collector's own probes), the slowest NFS mount's read round trip, and the load. A
spike there under the agents' CPU above it is what "do agents make the node slow for people?"
looks like.

- **Read on its own thread** (`nodehist.py`). A snapshot is ~200 KB and comes every
  `EBPFM_NODE_INTERVAL` (300 s), ~60 MB a host-day, so the first read of retention runs beside the
  service, a 4 MB chunk at a time, and nothing waits for it: the panel says how far it has read.
  After that it reads only what was appended, by inode and offset; a day sealed into its `.gz`
  lands on the snapshots it already holds. It keeps ten numbers a snapshot for retention, and reads
  `feeds.ebpf_node.series_days` day-dirs (0: enough for retention, whatever `feeds.ebpf_node.days`).
- **No resolution the samples never had.** A bin is the window's own width or twice the interval
  the snapshots come at, whichever is wider, so a snapshot that drifts over a bin edge leaves no
  hole; a hole is a missing snapshot. A latency bin is the slowest probe in it, a load bin its mean.
- **Hosts are not classes**, so their lines take hues no class owns. Past five, the five that stand
  out most are named on every chart and the rest are one grey line, the slowest of them per bin.

`tests/test_nodehist.py` covers the binning, the holes, the incremental read and the sealed day.

## Day by day

**Day by day**, on Live under *Who uses what*, is each class's processes, CPU and users per local
day over everything the live tier holds -- retention, not the window -- from its own minute bins
and roster (`LiveReducer.daily`), served in `/api/panels` as `trend`. It is not narrowed by the user
or agent-type filter, and says so when one is set. Count and cost side by side, as every
categorical panel, read as numbers or as each class's share of the day.

- **Three kinds of day.** A day the collector wrote in has its numbers and `held`, the minutes of
  it that held a record, against `minutes`, those the tier could hold; a day it wrote nothing in
  has null counts and no bars, not a quiet day; today is still going, and the oldest day starts
  where retention does (`cut`).
- **Users are the roster's**: an account counts once a day in each class it ran something in. Its
  bins are hours past the newest day, so a day boundary off the hour (a +5:30 zone) lands with the
  hour it falls in.

## Commands that failed

**Commands that failed**, on the Agents tab under the tool panels, is how the Tool mix's processes
ended (`tool_mix.exits`, `reducers/tools.py`): the same processes, after its shell dedup, per class
and agent type, by tool and by kind of tool.

- **A failure is a non-zero exit or a stop by a signal**, with two kept out because they are not
  failures: an exit 1 from a program whose 1 is an answer (`EXIT_ANSWERS`: grep, rg, ag, diff, cmp,
  and the grep family; shared with the inspector's "hide typos"), and SIGPIPE, which
  `command | head` sends once head has its lines. Both are counted apart, as is a record with no
  wait status at all; a rate is over the processes whose ending is known.
- **The collector does not fold signals into codes**: its `exit_code` is null exactly when a signal
  ended the process. The panel lists each row's commonest exit codes and signals, and says what
  the conventional ones mean (127, command not found; SIGKILL, killed outright).
- **Each count opens its commands**: the `/api/io/commands` lookup with `outcome=nonzero&outcome=signal`,
  the same verdict rule the reducer counts with (`export.make_predicate`). The lookup reads records
  before the dedup, so a shell that ran a failing command is listed beside it.

## Sessions: tasks, phases, and processes

An agent session's row says what the agent was doing at three levels, which the page calls **Tasks**,
**Phases**, and **Processes**.

| level | what it is | built by |
|---|---|---|
| Tasks | stretches of the session that were about one thing, each labeled for what it was for | `reducers/phase_tasks.py` cuts and labels them, from the names and the agent's own words that `reducers/phase_names.py` reads out of each command |
| Phases | what the agent spent its time on inside a task: exploring, editing, building, testing, running, Slurm jobs, waiting, git, and idle | `reducers/phase_segment.py`, from the tool calls `reducers/phase_rules.py` names |
| Processes | every process the session ran, in its tree | `/api/trajectory` (`proctree.py`) |

A row of Claude Code or Codex carries the first two as `phases`: the `list` of phases, the `tasks` above
them (each phase says which one it belongs to), a `headline` (the project, then the names that recur
across the tasks, as `KempnerForge · PRs #229, #233 · video_io.py`), and `n_calls`; and what kind of session
it was, a `category` with the `uses` of its work it rests on and a `summary` (*What kind of session it
was*, below). A row of any other
agent type, a person, or an IDE carries `{unavailable}` and a null `n_calls`: not recognized is not none.
How the page draws the levels is in [`web/README.md`](web/README.md#sessions-tasks-phases-and-processes).

All of it is read from the commands the agent ran and the words it wrote in them, and from nothing else,
as the basis line over the inspector's chart says. Reading and editing inside the agent, its thinking, and
a log entry it writes with its own Write or Edit tool leave no command, so none of them is seen. No model
writes a label, and no transcript, prompt, or file on disk is read. The cutting and the naming are
heuristics over command text, and their thresholds were tuned on seven hand-labeled sessions of boslogin06
and rtx6000a. `rc_dashboard/phase_report.py` reads a day of a feed as a window build does, prints the
phases and tasks of the agents it is asked for, and summarizes every row, which is what the thresholds are
tuned against.

**Where a task begins.** Calls that start within `phases.step_s` of a step's first call are one step, and
every cut falls between two steps. A task is cut

- at a gap of `gap_s` or more in which the agent waited on nothing. The agent waits on the calls of the step
  it just made, and on a process a finished call left running (`nohup job &`) while that runs. A call from
  an earlier step that is still running when a later step begins is one it moved past, and holds nothing
  open. The service lists such calls with the gap, three at most, each with the name the gap line calls it
  by: the script it ran, else what it tested, the job it submitted, or the folder it made, as a label shows
  them, and for a call still running with none of these the programs seen under it (`pgrep and sleep`).
  The page names the first: `scratchpad/linktest still running since 13:05:44`, and by its command where it
  has no name;
- where a name new to the session starts to recur: a step holds a name that first occurs in it, and the
  name turns up again in at least `new_steps` later steps of the stretch, the `new_steps`-th of them within
  `new_window_s` of the first. A search term and a folder changed into are no object, and begin no task;
- after a commit or a pull request that closes a burst of git (git calls and nothing else, each within two
  minutes of the one before), when the names in the `new_window_s` before the burst and after it overlap by
  less than `close_overlap`. Bursts that start within `close_series_s` of the end of the one before close
  once, after the last.

Polls and bursts are folded before names are counted: a command on a timer counts once, and so does one run
again within three minutes, so a job polled a hundred times is named once. A piece shorter than `min_s`, or
with fewer than `min_calls` calls, then merges into the neighbor it is more alike, unless it holds the
agent's own words, and never across a cutting gap. A local day merges down toward `cap` tasks, its most
alike neighbors first. A few calls hours from other work (at most `lone_calls`, all within `lone_s`, with a
cutting gap on each side or at the session's edge) are a **lone stretch**: no task, never merged into one,
and not counted toward the cap. Every session with calls has at least one task.

A row is cut and named again each time it is built, so a task that holds a call still running is open, and
is named from the calls it has so far. A session's call log holds `phases.max_calls` calls. Past that, calls
are counted and placed in no phase, and the last span, a lone stretch if that is what it is, and any span
that ends after the first call dropped, say they are incomplete.

**How a task is labeled.** There are two sources, and the page shows which one a label came from.

- **The agent's own words**, when a command in the task holds any: the title of a pull request
  (`gh pr create` or `gh pr edit` with `--title`), the subject of a commit (`git commit -m`, or `-F -` with
  a heredoc), or the title of an entry it writes to a Markdown file whose name has `log`, `plan`, `notes`,
  `handoff`, `progress`, `changelog`, or the like as a word of its own: a part of the name split on `_`,
  `-`, `.`, and camel case, so `CHAT_LOG.md` and `progressNotes.md` are logs and `methodology.md` is not.
  An entry is the text that a `cat` or `tee` heredoc, an
  `echo`, or a `printf` writes, and its title is its first heading or bold line, else its first line with a
  word in it. A pull request's title wins over a commit's subject, and a commit's over a log entry. Of
  several pull requests or commits the last counts, since it closes the work, and of several log entries the
  first, since it usually states the goal. Only text in the command counts, so `git commit -F msg.txt` has
  none, and the words are masked as every command is (`redact.py`). The page shows them as a quote: in
  quotation marks, with their source (`commit`, `PR #235`, or `log`).
- **Otherwise, names.** The label is a verb for the category that holds most of the task's time (`Read`,
  `Edit`, `Edit and test`, `Test`, `Build`, `Install`, `Run`, `Run on Slurm`, `Wait on`, `Commit`, or
  `Search for`), then the one or two names most specific to the task. A name is a file a command runs,
  tests, writes, or reads, a folder it makes or changes into, a Slurm job, a pull request, a branch, a
  worktree, a package it installs, a name it gave its own scratch, what it searched for, or another
  argument that reads as a name (`marinQC51`). Some text names nothing a reader knows and is no name: the
  value of an option that the reader knows takes one (the partition of `srun -p kempner_h100`), anything
  Slurm's queue and accounting commands are given (`squeue -u xq404`), the operands of a shell test, which
  are a loop's stop condition and not its work (`[[ "$s" =~ ^(COMPLETED|NODE_FAIL) ]]`), Claude Code's id
  for a subagent's worktree (`agent-a69e2723ecfaa3a16`), Claude Code's own folders in its temporary folder
  (`/tmp/claude-<uid>/<project>/<session>/scratchpad`), and what it keeps in its home: `~/.claude/projects`,
  a project's folder in it, a session's transcript `<uuid>.jsonl` and the folder `<uuid>/` beside it, and
  the project's `memory` folder and its index `MEMORY.md`. A relative path is read in the folder the
  command changed into, so `MEMORY.md` after `cd ~/.claude/projects/x/memory` is the index. What the agent
  makes inside these folders, a script in its scratchpad or a note in `memory`, is named as usual. A name
  scores by the steps of the task that hold it and how few of the session's spans, tasks and lone stretches
  alike, hold it, more where it first occurs, and by its kind: the objects a task works on count in full, a
  name the agent gave its own scratch 0.8, a file only read 0.6, another argument that reads as a name 0.5,
  a folder changed into 0.1, and a search term 0.05 (`phase_tasks.WEIGHTS`). In a session of two spans or
  more, a name that every span holds, lone stretches included, scores nothing. With one span, every name is
  in every span and still scores. A name that is the session's user names no task. The page shows a built
  label with no quotation marks and no source, so it is never taken for something the agent wrote.

The headline is the project, then up to `headline_names` names that two tasks or more hold, but for the
session's user. The project is the folder the agent was launched in or, when that is a home folder or is
named for a person, the folder its commands change into most that is not. A folder is named for a person
when its name, or a part of it split on `_`, `-`, and `.`, is the user's, or is the name of a home or lab
folder its path runs through (`/n/netscratch/alpha_lab/Lab/jdoe`, `…/Lab/netscratch_rroe`), as
`anonymize.py` finds them.

**Four things a plain reading of the commands gets wrong**, which the rules account for:

- **A program held in a variable** reads as the program it names. The assignment earlier in the call gives
  it (`PY=../venv/bin/python; $PY -m pytest tests/x.py`, `S='sbatch --parsable'; $S job.sh`). Failing that,
  a variable named for Python (`$PY`, `$PYTHON`) or one whose first argument is `-m`, `-c`, `-`, or a `.py`
  file is Python, read by the Python rules: `-m pytest` is a test, a heredoc follows the heredoc rules, and a
  script is read by its name (`$PY check_env.py` is a test, `$PY train.py` a run). Nothing else is read, and
  `$CMD --flag x` stays unknown.
- **The Codex desktop app's reads of its own plugins** are housekeeping, not the model's calls. The app
  `cat`s the icons of the plugins it lists, hundreds in a minute: one file in an `assets/` folder under
  `.codex/.tmp/plugins/`, `.codex/.tmp/bundled-marketplaces/`, or `.codex/plugins/cache/`. Only the app's own
  argv counts, so a model's `rg`, `sed`, or `cat` of a plugin's skill stays a call. A `cat` whose argv was
  not recorded (schema 7) names no file, so it is read by when it ran: it is the app's when it began within
  2 s (`reducers.trajectories.phases.app_read_s`) of one of the app's reads in the same row, before it or
  after, and a call otherwise. On boslogin06 on 2026-10-09 that took 230 of the day's 8,434 Codex calls,
  226 of them from one row. A wider window would take the model's own reads, which come as soon as 15 s
  after a burst.
- **A call the agent moved past holds no idle.** Only what the agent is still waiting on holds a gap open
  (above). A call from an earlier step that is still running when a later step begins holds no idle gap and
  no guess, however long it runs, so one that hangs for hours does not hold off the gaps after it. Time goes
  to what the agent started most recently, so it does not take the time of the work that follows it. A gap
  that begins while it runs lists it (`alive`), and once it ends, its time past the phase it began in is
  said in that phase (`train.py kept running for 2.7 h`), as a background job's is. The session's last phase
  ends at the latest end of all the session's calls, or of a process one of them left running, whichever is
  later, so the lane does not end while anything the agent started still runs.
- **A session across midnight is one block.** A row is one local day, since its session id carries the day,
  so an agent that works past midnight has a row for each. Every one of them serves the same block, cut from
  the calls of all of them, so midnight cuts no task or phase and either row opens the whole session. `days`
  lists the rows (`{sid, first, last}`), and `sid` on a task or a phase says which row holds it: the row
  whose span, first record to last, holds its start, else the nearest. A row's own `n_calls` stays its day's,
  and the block's counts the session's.

**What kind of session it was.** Each block says it in a word and in a few sentences
(`reducers/phase_summary.py`), so a list of sessions can be read, and counted, by what they were for. The
`category` is one of the uses the Users tab counts a user's calls by: `explore` (read and search code and
files), `develop` (change files from the shell, and test), `build` (build and install software), `run` (run
programs), `hpc` (Slurm), or `git`. A session is put under the use that holds half of its work or more, and
is `mixed` when none does. Its work is its phases' time but idle gaps, waiting, and commands not recorded. A
wait on Slurm is Slurm work, as a task's verb counts it, since a job is the scheduler's work whether it is
being submitted or waited on; any other wait (on a process, a file, or a fixed sleep) is no use, and is said
apart. The uses are weighed by time, as the band draws them, and not by calls: a Slurm session's polls of
the queue, or one long `python` run, would otherwise be outnumbered by the quick reads around them. A block
whose phases have no length at all (a call or two that began and ended in one second) is weighed by its
calls. A session that only waited is `wait`; one whose commands not recorded hold half of its work and
themselves, or that did nothing else, is `unknown`; and one that made no tool call is `none`. `uses` gives
each use's share of the work, the most first.

The `summary` is one to three sentences, each built from the block's own numbers and none from a model: what
the work was and its mix (`Mostly ran jobs on Slurm (82% of 5.1 h of work), then explored code and files
(12%).`), the tool calls and tasks with the distinct names they held (`564 tool calls in 7 tasks, 5 named in
the agent's own words; 61 Slurm job names, 74 scripts run.`), and how long the agent waited and on what, when
that is a tenth of its time but idle gaps or more (`Waited 4.7 h on Slurm, polling every 2 min.`). A Slurm job
is named once by a call: by the name it was given (`--job-name`, `-J`), else by its script's.

On the eight FAS RC login nodes (`boslogin05`–`08`, `holylogin05`–`08`) on 2026-11-12, 2026-11-13, and
2026-11-15, 16,630 sessions had an agent at their root. 12,534 of them (75%) made no tool call: half of them
lasted under a second and ran only the agent's own commands (`rg`, `git`), and an agent that only reads and
edits with its own tools leaves no command either. Of the 4,096 that made one, 33% were `explore`, 29% `run`, 20%
`hpc`, 6% `mixed`, 5% `develop`, 4% `unknown`, 1% `wait`, and under 1% each `build` and `git`. One use held
half of the work in 94% of the sessions that did any, and a count of calls would have named a different
category for about a third of them, most of them Slurm sessions and long runs. A fifth of the summaries say
the agent waited. Claude Code's sessions were 27% `hpc` and Codex's 4%; Codex's were 42% `run`. One
misreading is known: 114 sessions of one user's Codex desktop app on 2026-11-15 ran two calls each, a
`mkdir -p` and a `chmod 700` of a folder of its own under `/var/tmp`, and read as `develop` (`Only changed
files from the shell. 2 tool calls.`), as their tasks read `Edit`. The folder is that user's setup, not
Codex's, so it is not read as housekeeping. `rc_dashboard/phase_report.py` prints each row's category and summary, and counts a day's
sessions by category, each once.

The page shows the category as a chip on the row and in the Users tab's list of a user's sessions, and the
summary under the headline in the inspector (`web/README.md`). The plates over every session in the window
do not count categories: a category needs a session's block, and building a block for every session of a
week on eight login nodes takes minutes, where the plates are read again every few seconds.

**Config.** `reducers.trajectories.tasks` sets the thresholds. It equals `phase_tasks.DEFAULTS`, where each
one is documented, and a test holds the two equal. `reducers.trajectories.phases`, beside it, does the same
for the fine phases (`reducers/phase_segment.py`), and `reducers.trajectories.summary` for a block's
category and summary (`reducers/phase_summary.py`): `mostly` (0.5), the share of the work a use must hold
for the session to be put under it, and `also` (0.1), the share a second use, or waiting, needs before the
summary names it. Like the rest of `reducers`, all three are part of the config a
saved window or hour is fingerprinted by, so a changed value builds them again rather than resuming them
(*The time window*). A saved state needs that only for the phase thresholds read as records arrive, which
bound the call log and join a call's children to it (`max_calls`, `hk_bin_cap`, `pend_max`, and `idle_s`).
The rest, every task and summary threshold among them, are read when a row is built: the reducer cuts and
names a row each time it serves it, with the thresholds it runs with, and no saved state holds a task or a
category.

| key | default | meaning |
|---|---|---|
| `gap_s` | 3600 | a gap this many seconds long, with nothing the agent waits on, cuts a task |
| `lone_calls` | 8 | the most calls a lone stretch holds |
| `lone_s` | 7200 | the seconds its calls must all start within |
| `new_steps` | 5 | the later steps of the stretch a new name must turn up in, at least, to begin a task |
| `new_window_s` | 1800 | the seconds after it first occurs by which the `new_steps`-th of those steps must start |
| `close_overlap` | 0.5 | a closing commit or pull request cuts when the names around it overlap by less than this: the share of all their names that are in both |
| `close_series_s` | 300 | bursts of git that start this many seconds or fewer after the end of the one before close once, after the last |
| `min_s` | 600 | a task shorter than this merges into a neighbor |
| `min_calls` | 5 | a task with fewer calls than this merges into a neighbor |
| `cap` | 10 | the number of tasks a local day is merged down toward |
| `headline_names` | 3 | the names the headline lists after the project |

## Agents: subagents and blast radius

The **Agents** tab (`?tab=agents`; the old `?tab=cascades` still opens it) answers "which
agents started which other agents, and where did each one work". A node is a process whose
role (`roles.py`) is `agent`, `infra` or `client`; tool-call shells, sandboxes, probes and
helpers are steps between nodes. A node's parent is the nearest node above it in the process
tree, else (for a `bg-pty-host` orphaned to init) its user's only daemon, labelled as
inferred, else the tree root. An agent started, through any number of steps, beneath another
agent is that agent's **subagent**. Two outcomes are kept apart: **nested**, an agent with
subagents, and **hosted**, a daemon, Claude Desktop's remote server or `claude rc` running
sessions side by side. A Codex TUI whose direct child is `codex app-server` is shown as that
session's front-end, not as a second agent.

`claude daemon` roots a tree of its own, hosting its background sessions side by side. It is
started, detached, by whichever `claude` first needs it, and its `--spawned-by {"pid": N}`
names that process -- but it is its user's, and every background session the user starts
later, from any terminal, goes through it. Hung under the process that started it, as it
was, each of those sessions was drawn as that one session's subagent. Who started it is kept
on its node (`spawned_by`, in its box's tooltip), and is never its parent, nor is the ppid a
detached daemon keeps until its starter exits. A session its `bg-pty-host` runs shows as a
`terminal` **background session**. The daemon also keeps sessions started ahead of need, each
a `claude bg-spare` waiting to be claimed, so the next background session starts at once:
one nobody claimed ran no process but its own, and is the daemon's (`infra`), not an agent
-- drawn as one, each was an agent that ran nothing. Idle spares are counted per tree and in
the summary (`idle_spares`); a spare that ran anything was claimed, and is the agent it
became.

Every tree is sent, up to `reducers.cascades.top_trees` (1000), the deepest first. It was 25,
and with 25 trees of subagents in a window no session that started no other was sent at all;
past the cap the list says how many trees were not, and the type strip that it counts the
trees sent. A blast radius is served for the `reducers.workspace.max_agents` (300; was 30)
busiest agents, and for every agent that wrote outside its start folder; one past that says
so when picked, and an agent that ran nothing says that instead.

What it cannot see: Claude Code's Task-tool subagents and workflow agents run inside their
parent's process. Only agents that are processes of their own appear; an in-process
subagent's commands are counted in its parent's. On madsys-gpu1 over 2026-10-01..07 that was
49 agent trees, 4 hosted and none nested.

**Workspace & blast radius** (`reducers/workspace.py`, composed per agent by the cascades
reducer) is where each agent worked, from its **home** -- the agent process's own cwd, or,
when that is `/` (IDE-hosted), its first call's directory off the root, labelled so. The
collector records no file paths, so three kinds of evidence are kept apart on the page and
in the payload's `basis`:

| reading | kind | what it is |
|---|---|---|
| ran in | **measured** | processes whose cwd the collector recorded as that directory. It reads cwd at exec for some processes and copies the parent's at fork for the rest -- before a shell's own `cd`, and before the chdir Claude Code's Bash tool makes into the directory its last call left it in (on rtx6000a 1 173 of 1 180 tool-call shells on 2026-10-11 were copied). Each view says how much was read (`cwd_read_pct`) |
| bytes | **measured, per process** | rchar/wchar (syscall, pipes and terminals included) and read_bytes/write_bytes (disk) of the processes that ran there -- credited to where they ran, not to where the bytes went |
| named | **inferred** | distinct paths a command line names, classified by the command: read (`cat`, `grep`, `ls`, a script run, `<`), write (`>`/`>>`, a `cp`/`mv`/`rsync` destination, `tee`, `touch`, `mkdir`, `sed -i`, `tar -x`, `git clone`, `wget -O`, `curl -o`), delete (`rm`, `rmdir`, `git rm`, `find -delete`), and anything else path-like as named. Naming a path is not proof of touching it |

Exact file-level reads and writes would need a collector probe (openat / unlink / rename
tracepoints) that is not built, and Claude Code's Read, Edit and Write tools and Codex's
apply_patch run inside the agent's own process and leave no command line at all.

Directories inside the home are shown relative (`./src`), outside it with `~` for the user's
home and `~bob` for another's; deeper than `collapse_depth` (4) levels below the home or a
top-level root (`/tmp`, `/scratch`, `~/.cache`, `/n/<fs>`, another user's home) they fold into
that ancestor, as do a directory's quieter subdirectories past six, and every fold is counted.
A **write or delete named outside the home is flagged** and labelled by where it landed
(another user's home and system paths first, the agent's own `/tmp` scratch last). The
toggle **this agent only / with its subagents** adds every agent beneath it, judged against
the outer agent's home. Claude Code's tool calls are read through their `eval '<cmd>'`
(`proctree.ran`, the inspector's own reader), so the snapshot each call sources and the cwd
file it writes are never counted; a fork that inherited its parent's argv is not read twice. A
relative path is placed against the process's cwd, never against `/` (an agent's process is at
`/` when it inherited an IDE's or a daemon's directory, and `ls src` from there is not `/src`),
and never by climbing out of a copied cwd with `..`: `./bin/cachesim ../data/x` with a cwd
copied from an agent in `/home/marthen` would otherwise read as `/home/data`, another user's
home.

Cost and bounds: each distinct argv is parsed once (a regex tokenizer, never `shlex`) and the
per-record path is a few dict lookups. On rtx6000a's 2026-10-10 (1.89 M records, a ~90 s 24 h
build) `feed` took 2.4 s, ~1.4 µs a record, and the saved window grew by 2.4 MB. State is one
entry per collector session key, capped at `reducers.workspace.max_keys` (150 000) keys,
`max_dirs` (300) directories per key, `max_dir_entries` (400 000) overall, 64 named paths per
directory and 32 per command line; past any cap the excess is counted, not kept. At the key
cap, a new key first drops the keys last seen before the window began. The keys are walked
for that only once the oldest key the previous walk kept has left the window, so a cap full of
keys inside the window costs one walk, not one per record. Served paths are masked
(`redact`) and no raw argv is served.

The **drift** proxies (`cwd_outside_pct`, `mix_jsd`) are the session rows' reading and stay
on the Sessions tab; the Agents page no longer carries them.

## Users: a portrait of each user

The **Users** tab (`?tab=users`, a portrait open as `&portrait=<user>`) says how each person works with
coding agents: which agents, launched how and from where, what the agents' commands do, how long a session
runs and how many run at once, when in the day, what they work on, and what they ask of the scheduler.

Nothing here is reduced or saved. `rc_dashboard/portraits.py` composes it, when it is read, from the reducers
a window view already holds -- the trajectories, the sandbox and Slurm -- and from the live tier for what is
running now, so it can never disagree with the tabs those reducers fill. It holds no state a saved window or
hour must keep: it is not in either fingerprint (`snapshots.CODE_FILES`, `hours.CODE_FILES`), no reducer
imports it, and changing it rebuilds nothing.

| part | from | what it says |
|---|---|---|
| launch | trajectories (`surface_of`, `approval_of`), sandbox (`agent_calls`) | agent types; where each session is driven from; approval at launch; whether its tool calls ran sandboxed, as the Internal tab counts them |
| rhythm | trajectories (each row's first and last record, each call's start) | sessions, how long each was open, the most open at once, and the minutes in which a call began, by local hour and weekday |
| work | each session's call log; its phase block, for one user's portrait | tool calls by category (`phase_rules.CATS`), counted; time in each phase; the agent's own words in a command (a commit message, a pull request's title, a log entry); the project and the tasks |
| footprint | trajectories | processes, CPU, disk and network bytes, peak memory, summed as a Sessions row sums them |
| scheduler | Slurm (`_sessions`, `_polls`) | jobs, refusals, queue checks and polling loops of the user's agent sessions, folded as the Slurm tab folds them |

- **A session is one agent run.** The rows the Sessions tab lists for one agent on two days are one session
  here, as the agent's phase block already is. Only rooted sessions count, as the Sessions tab's plates count
  them, so a status bar probing `codex --version` every minute is none. A session belongs to the account its
  records name first. One whose records name no user and no uid (the trajectories reducer falls back to
  `uid:N` where there is one) is no one's row: the roster counts it apart (`unattributed`) and says so under
  the table, and the Sessions tab lists it with the rest.
- **Unread is not none.** An agent whose commands the service does not read (anything but Claude Code and
  Codex) has calls of null, shown as *not read*, never 0.
- **A share is over the known values**, with the rest counted beside it: a Codex app server's approval is
  unobservable, and a session whose own record was not seen has no surface.
- **How they leverage agents** puts the same numbers together (`portraits.leverage`): the way most of their
  sessions are driven (a terminal, scripts, an IDE, the desktop app or the web, else several places), the
  freedom the agents start with, how many run at once, what the agents' actions are for (`portraits.USES`:
  their calls grouped into exploring, editing and testing, building, running programs, Slurm and git, with
  waiting and unrecorded calls left out, and the uses that hold most of them named), how much is waiting, the
  scheduler, what the agents leave behind in their own words, what they work on, and when. The service decides
  each part by `RULES`, and the page words it: one sentence under each user in the roster, and that sentence,
  point by point, at the head of their portrait.
- **A figure of each user** draws the same row (`web/src/components/PortraitFigure.tsx`): a person at a desk,
  drawn the same for everyone (no one's picture is held, and the names are aliases), and round them a part for
  each number: a robot for each session open at once, coloured by agent, its antenna lit while it runs; the
  laptop's emblem for where most sessions are driven from, the person leaning back when that is scripts; a
  padlock for launches that bypassed approval, a shield for auto mode, capsules for the sandboxed trait; a shelf
  of what the actions are for, each icon's area its share; a 24-hour clock of active minutes; the moon for the
  overnight trait; papers for what the agents wrote; a mug or a stopwatch for long or short sessions; an
  hourglass for polling; a server with a light for each Slurm job. Each part says its numbers and the rule
  behind it on hover, and a key says what each one means. The only numbers the page adds are drawing caps (six
  robots, five sheets, ten lights), each said where it applies; a calls count the service could not make is
  drawn as unread, never as an empty shelf. It leads the portrait, and the roster shows everyone's side by side
  (*figures*). `npm run check:figures` renders every fixture user's figure and checks it against their row.
- **Traits** are plain statements ("launches with a bypass flag", "keeps several sessions open at once"),
  each from one rule over the numbers above (`portraits.RULES`, `portraits.TRAITS`). The rules are served with
  them and shown on hover; each trait carries the numbers that met its rule. None is a judgement, none is made
  by a model, and a user can hold none.
- **The work is read from commands**, as everything on the Sessions tab is: an agent's own Read and Edit
  tools, and its thinking, leave no command. An agent that edits only through its own tools shows no editing.
- **One user's portrait reads the phase blocks** of their newest `PHASE_SESSIONS` (80) sessions, the same
  blocks the Sessions tab's rows show and cache, and lists at most 60 sessions and 40 tasks; the rest are counted.
  Under a user filter it is read against the whole window's users when that view is held (`fleet_of`).

Cost: the roster walks every folded session and its call log once; the sandbox's verdicts walk every tool call
of the window, which is most of it. The aggregator holds each view's inputs (`portraits.Context`), its roster
and each open portrait, and builds them again once the view has ingested and `reducers.trajectories.live_s`
has passed, as the ranked sessions are re-ranked. On 500 synthetic sessions with 100 000 calls the roster took
about 0.3 s, and a portrait read from the roster's inputs about 20 ms.

## Slurm: how sessions use the scheduler

"An agent submitted job N" says nothing about agent behaviour; how agents drive the
scheduler does. On boslogin06's 2026-10-09 agents made 16.6 scheduler queries per job
against 0.2 for people, 41 agent sessions polled the queue for 10 minutes or more (94.9 h
of it; one Claude session every 4.9 s for 4.9 h), one session submitted 112 jobs in a
minute, and 79% of agent submissions used `--parsable` (people: 0.2%) -- a program reading
the job id. The **Slurm** tab (`?tab=slurm`, hidden by `slurm.enabled: false`) is built on
that, from the window's `slurm` reducer (`reducers/slurm.py`):

- **per session** -- an agent's collector keys folded into the agent, as the Sessions tab folds
  them; people and IDEs one row per user: jobs, refusals, the busiest minute, distinct
  command lines (numbers masked: one script resubmitted is one), the flags it submits with,
  queries, its longest **polling loop** (queries within 2 s are one poll, polls within 3 min
  one loop, 10 min or more counts as polling), queries per job, cancels, GPU-hours asked;
- **per class**, the same as rates and shares side by side;
- **what came back** from each submission: a job id, a dry run (`--test-only` makes no job),
  a step into an allocation already held (`srun --jobid`/`--overlap`), or a **refusal** --
  no id, a non-zero exit and the scheduler's own error line, which is the reason shown
  (`QOSMaxSubmitJobPerUserLimit`, a partition rule). An `srun` whose program ran and failed
  is not a refusal;
- queries and jobs per time bin, by class. A bin with no Slurm record of any class is a
  gap, not 0.

Requests are read off argv: a `#SBATCH` line in the script is not seen, so GPU-hours count
only jobs with GPUs and a time limit on the command line, and say how many those were.

## Known collector limitations the page surfaces

- **`peak_rss_mb` is always 0** on kernel 6.8: the kernel runs `exit_mm()` (clearing
  `task->mm`) before the `sched_process_exit` tracepoint fires, so the `if (mm)` guard at
  `ebpf_trace.py:834` never assigns. Reading `task->signal->maxrss` instead survives
  `exit_mm()` (verified). Until then the metric renders **not measured**.
- **The collector's session key splits one session per tool call.** It keys a process on
  its nearest classifier-tagged ancestor, and the classifier tags anything whose argv
  mentions `~/.claude/`, `~/.codex/` or `~/.vscode-server/` -- including the shell every
  Claude Code Bash tool call runs in. The dashboard decides what each tagged process
  really is (`roles.py`) and folds each key into the agent above it (`procindex.py`);
  trajectory rows say how many keys they fold and how the least certain one was
  attributed, and the Sandbox tab attributes tool calls to sessions through the same fold. On madsys-gpu1 a
  24 h window went from 3 281 keys to 636 sessions, one VS Code server alone from 688.
- **The node tier daemon needs an absolute path.** `ebpfm.sh:466` re-execs `"$0"`, so
  `bash ebpfm.sh start` leaves the nohup'd child unable to find itself. Invoke it by absolute
  path.
- **`submit` request fields are argv-only.** A job whose `#SBATCH` directives carry the
  request reports `null` for `partition`/`gpus`/`array`; `req_src` says which. Recovering them
  needs a `slurm_jobs` census join, which is not a configured feed.
- **Scheduler and GPU panels are absent, not zero.** `running_agent_jobs` and GPU duty need
  the census and `dcgm`. The Live tab shows *agent job submissions in the window* from eBPF
  `submit` records instead — per-submission and actor-attributed, which is better evidence
  than a census count.

## Layout

| path | role |
|---|---|
| `rc_dashboard/config.py` | per-key precedence, candidate probing, `--print-config` |
| `rc_dashboard/feeds.py` | resolve + report every feed; ports `healthcheck.check_target` / `last_timestamp` |
| `rc_dashboard/feedfile.py` | the collector's day-files: listing them (a plain file before its `.gz`), the binary searches by time (`seek_to_epoch`, `end_at`), where the last complete line ends, the head digest |
| `rc_dashboard/history.py` | the history store: every sealed day-file kept as Parquet, converted in the background ([History in Parquet](#history-in-parquet)) |
| `rc_dashboard/keysearch.py` | the session inspector's key search with its records built in worker processes, replayed with the one-process read's counters |
| `rc_dashboard/replica.py` | a sealed day-file read from its Parquet tables as the file itself: its offsets, time searches and records |
| `rc_dashboard/where.py` | the filters a replica rejects records by before it builds them |
| `rc_dashboard/feedparquet.py` | the Parquet schema, the converter and its verifier ([The feed as Parquet tables](#the-feed-as-parquet-tables)) |
| `rc_dashboard/tail.py` | `IngestSource`; the file tail, and the newest-first read-back that refills retention on every start (a push source drops in here) |
| `rc_dashboard/gz.py` | a `.jsonl.gz` day-file read in place, seekable by uncompressed offset through a per-process inflater index |
| `rc_dashboard/health.py` | each collector's verdict, from its own meta / heartbeat / stop records |
| `rc_dashboard/clock.py` | the service's present; `clock.as_of` pins (and freezes) it for a captured feed |
| `rc_dashboard/normalize.py` | schema contract, `actor3`, sandbox/approval, session keys, null gating |
| `rc_dashboard/roles.py` | what a classifier-tagged process is: agent, tool-shell, probe, sandbox, infra, ... |
| `rc_dashboard/procindex.py` | per-window index of tagged processes; folds session keys into their agent |
| `rc_dashboard/buckets.py` | rolling per-minute aggregates; exact sub-window slicing |
| `rc_dashboard/nodehist.py` | the node tier over time: every snapshot's probes, load and NFS round trips, read incrementally on its own thread and binned to a window ([Login node health](#login-node-health)) |
| `rc_dashboard/reducers/` | tools, io, resources, sandbox, trajectories, cascades (agent trees), workspace (where each agent worked), slurm (sessions at the scheduler), live, node |
| `rc_dashboard/reducers/phase_*.py` | an agent's tool calls named (`phase_rules`) and logged (`phase_log`), cut into phases (`phase_segment`) and into tasks (`phase_tasks`), and the names and the agent's own words the tasks are labeled from (`phase_names`) |
| `rc_dashboard/phase_report.py` | the report the phase and task thresholds are tuned against |
| `rc_dashboard/portraits.py` | the Users tab: each user's portrait, composed from a view's trajectories, sandbox and Slurm reducers when read; the traits and their rules |
| `rc_dashboard/windows.py` | window clamp rule, per-window reducer sets, the LRU + build worker |
| `rc_dashboard/snapshots.py` | saved window builds: fingerprint, capture/restore, the 0600 store |
| `rc_dashboard/sacct.py` | `collect/slurm_query.bash` exports → per-job rows, generation-merged |
| `rc_dashboard/export.py` | shared filter grammar, streaming export, the windowed re-scan (over `feedfile.py`, whose names it re-exports) |
| `rc_dashboard/app.py` | FastAPI: REST, WebSocket, static mount |
| `rc_dashboard/demo/` | demo mode: the synthetic feed (`world` plans and builds it, `cast` the people, `sessions`/`agents` what each runs, `host` each node's own records, `feed` the files), and the overrides that point the service at it |
| `web/` | React + Vite + TS; `dist/` is what the service serves |

## Ingestion, precisely

**What the page holds is a function of the collector's files and the clock — never of when this
service was running.** Nothing in the live tier is restored from a previous run (the historical
windows resume from saved builds, but only from ones that are provably a prefix of the same files —
see *The time window*). On every start the service hands
each day-file to the tail at the end of its last complete line or frame, then reads the history
behind that point back into the live tier: `min(live.backfill_hours, live.retention_min)` of it,
located by the same timestamp binary search the window rebuild uses. Offsets used to be
persisted while the bins they fed were not, so a restart resumed at EOF with empty bins and the
page showed only what the collector wrote after the service came back — and `run.sh` restarts
the service on every save.

The read-back runs **newest first**, k-way merged across hosts, while the tail is already
polling; a poll that lands before the hand-off reads nothing, so no record is counted twice. The
page is live within a second and watches the chart fill from now backwards. The filled span is
always contiguous, so the picker's `filled 5h of 7d` stays true beside `reading history · N%`.
The read takes the aggregation lock one small batch at a time, so neither the page nor the tail
queues behind it. A week of one login node's feed (4 GB, 2.4 M records) takes 35–45 s here. Bins
are sums and take records in any order; the "latest" state — each host's plate, the event and
submission tails — only takes an older record where nothing newer has claimed the slot.

`DailyWriter.write()` (`collector/ebpf_trace.py`) buffers and flushes once per poll round, which
is not atomic against a concurrent reader. So the tailer reads to EOF, finds the last newline,
yields only complete lines, and **advances its stored offset only that far** — trailing bytes are
re-read next cycle. Idempotent, never a truncated JSON line, never a lost record. The startup hand-off obeys the
same rule, so a line still being written belongs to the tail, never to the read-back.

Offsets are keyed on `(st_dev, st_ino)`, not path, so a recreated file is detected as a new inode
rather than silently seeked past its end. A file first seen after startup — a new date
directory, a new host — is read from byte 0, at most 64 MB per poll.
