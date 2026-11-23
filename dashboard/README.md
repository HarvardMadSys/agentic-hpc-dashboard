# Live agent-behaviour dashboard

A FastAPI service that tails the [`ebpfm`](../collector) collector's JSONL day-files (a sealed day
is also kept as Parquet: [History in Parquet](#history-in-parquet)), keeps rolling aggregates over
a **selectable** window, and pushes them to a React page over a WebSocket. Every number on the page
is measured: a feed with no data names the path it expected, and a metric the collector could not
populate reads **not measured**, never `0`. [docs/figures](../docs/figures/README.md) says what each
figure on the page computes, from which records, by which read path, and at what cost.

## Run it

```bash
cd dashboard && uv sync --frozen
uv run --frozen python -m rc_dashboard --check-feeds   # what resolved, what didn't, and why
(cd web && npm ci && npm run build)                    # builds web/dist/, which the service serves
uv run --frozen python -m rc_dashboard new-password    # once: prints the hash for dashboard.config.json
uv run --frozen python -m rc_dashboard serve --port 8080
```

`serve` starts only behind a password ([Password](#password)): put the hash `new-password` prints
into `dashboard.config.json` as `"server": {"auth": {"password_hash": "..."}}`. Without one it says
so and exits, because on a login node every user can reach a port on 127.0.0.1. `--open`
(`RC_DASH_OPEN=1`) serves without a password, for a machine only you can log in to.

Off-cluster, `--check-feeds` exits 1, because the eBPF feed is `required` and absent: point it at a
collector root.

```bash
RC_DASH_EBPF_ROOTS=/var/log/ebpfm RC_DASH_NODE_ROOTS=/var/log/ebpfm-login \
  uv run --frozen python -m rc_dashboard serve
```

Reach a remote instance over a tunnel: `ssh -L 8080:127.0.0.1:8080 <host>`. With no cluster,
[Demo mode](#demo-mode) runs the same service over a synthetic feed it writes itself.

`./run.sh` runs the service with hot reload: `dev` (the default) through Vite's dev server on
`RC_DASH_UI_PORT` (5173), `build` serving the built page, rebuilt on every edit, `api` the backend
alone. Its wrappers run it in `build` mode: `run-dev.sh` (:8086) and `run-fasrc.sh` (:8085, a capture
under a pinned clock) with one site's feed roots, risk profile and site name, which need a password
hash and serve names as they are (`RC_DASH_ANONYMIZE=0` unless set), and `run-demo.sh` (:8087).

## Configuration

Each key resolves **separately**: **CLI flag > `RC_DASH_*` env var > config file > built-in
default**, and a flag you don't pass never outranks the file. Copy
[`dashboard.config.example.json`](dashboard.config.example.json) to `dashboard.config.json`
(gitignored; `//`-prefixed keys are comments). `--print-config` prints the resolved tree with each
key's `_origin`.

- Path defaults are **candidate lists**, and every candidate that exists is read: `--print-config`
  lists those (`_existing`), and `--check-feeds` prints the first, or how many it tried when none
  exists.
- **A mistake stops the service rather than loosening it.** A config file named by `--config` or
  `RC_DASH_CONFIG` must exist. A yes/no setting given as text (an `RC_DASH_*` variable, or a string
  in the file) is `1`/`0`, `true`/`false`, `yes`/`no` or `on`/`off`, in any case; anything else
  (`RC_DASH_ASK=n`) stops it. An unknown key under `server`, `anonymize` or `ask`, or a top-level
  key one slip from one of them (`anonymise`), stops it and names the nearest key; an unknown key
  anywhere else is a warning.
- Retired feeds' settings (`feeds.sacct`, `feeds.sacctmgr`, `feeds.domains`, `feeds.user_actor`,
  `RC_DASH_SACCT_DIR` and the other `config.RETIRED_ENV` variables, `--sacct-dir`, `--jobs-csv`)
  are named in a warning and ignored, never refused. So are the sections of removed features
  (`config.RETIRED_SECTIONS`: `annotate`, the tool-call purposes a model wrote; `export`, the
  Search & export tab's download) and every variable that set one (`RC_DASH_ANNOTATE`,
  `RC_DASH_ANNOTATE_*`, `RC_DASH_EXPORT_MAX_ROWS`), which `/api/config` never shows.
- `history` and `ingest` are under [History in Parquet](#history-in-parquet) and
  [Hourly merge-reduce](#hourly-merge-reduce).
- `filters.drop_global` (`RC_DASH_DROP_USERS`, comma-separated) drops every record of the accounts
  it names; `filters.drop_agent` (`RC_DASH_DROP_AGENT_USERS`) only their records classed agent,
  such as a site's monitoring account's. Neither names anyone by default (`run.sh` sets both for
  its clusters). `/api/feeds` counts what was dropped (`excluded.users`).
- `site.name` (`RC_DASH_SITE_NAME`) is shown under the sidebar title and in the browser tab, so two
  instances open side by side are not mistaken for each other.
- `slurm.enabled` (`RC_DASH_SLURM=0`) turns the HPC tab off and skips reducing it, for a cluster
  without Slurm; *Who uses what*'s units asked of Slurm go with it.

## The two feeds

| feed | what it carries | default candidates |
|---|---|---|
| `ebpf` (**required**) | the root tier: per-process exit, residency, tcp/conn, submit | `$EBPFM_LOG_ROOT`, `$EBPFM_OUTPUT_DIR`, `$RC_MEASUREMENT_ROOT/ebpfm/logs`, `log/ebpfm`, `log/ebpfm/logs`, `/var/log/ebpfm`, `~/.ebpfm/ebpf` |
| `ebpf_node` | the unprivileged node tier: load, memory, NFS, per-user cgroup | `$EBPFM_NODE_LOG_ROOT`, `$EBPFM_NODE_OUTPUT_DIR`, `$RC_MEASUREMENT_ROOT/ebpfm/node/logs`, `log/ebpfm_node`, `/var/log/ebpfm-login`, `~/.ebpfm/login`, `log/login` |

A relative candidate is under the repository's root. `RC_DASH_EBPF_ROOTS` (`--ebpf-root`) and
`RC_DASH_NODE_ROOTS` (`--node-root`) replace the lists. A root collector on a host with a writable
`/scratch` writes to `/scratch/ebpfm{,-login}`, which is not a candidate, so name it there. Since
every candidate that exists is read, `EBPFM_LOG_ROOT` or `EBPFM_OUTPUT_DIR` set where the service
starts adds a root.

Under a root, a day is a directory of one file per host and stream:
`<YYYY-MM-DD>/<host>.exits.jsonl[.gz]` and `<host>.snapshot.jsonl[.gz]` for the eBPF tier,
`<YYYY-MM-DD>/<host>.jsonl` for the node tier. A `.gz` is a day the collector has sealed; it never
seals the node tier's files.

### History in Parquet

The collector seals a day by gzipping its files once the day is over (`x.jsonl` to `x.jsonl.gz`);
a sealed file never changes again. The service also keeps each sealed eBPF-tier file as Parquet
(`history.py`): `feedparquet`'s typed table for each record kind
([docs/feed-parquet](../docs/feed-parquet/README.md)), every row carrying `_off`, its line's offset
in the uncompressed file, and a manifest of what else a reader might ask of the file (its size,
where its last complete line ends, its first 4 KB, the lines that are not records). From these a
replica (`replica.py`) answers the readers' questions with the file's own offsets:

| read of a sealed day-file | from its replica? |
|---|---|
| a filtered read: a view of some users or agent types, the commands behind a clicked I/O figure, a session's process tree | yes: the filter, as a `where` (`where.py`), is tested on the columns, and only the records that pass are built |
| a time search: where a window's read, a resumed read or a filtered read starts | yes, with nothing inflated (the backfill and an hour's read search the `.gz`, whose seek index the read that follows needs anyway) |
| a read of everything in a range: the backfill, an hour's reduction, an unfiltered window | no: orjson builds a record from a line faster than Arrow does |

A file is read as JSONL while it has no current replica: not converted yet, no longer the file its
manifest names (by name, size, mtime and inode), holding a line a JSONL reader would read
differently (a last line with no newline, a line that is JSON but not an object), or with a table
missing or resized (it is then converted again). A replica that fails during a read is dropped, and
the read continues from the file where it left off; the error shows in the store's `errors`. No read
waits for the store.

**The same answers.** A replica returns the same records in the same order, with the same keys and
types at the same offsets, and a time search or a filtered read makes the same probes, stops at the
same record and counts the same things. So switching the store on or off, or moving it, changes no
answer and rebuilds no saved window or hour; `tests/e2e_history.py` serves one feed with the store
off and then on and diffs every API and saved state.

**The store.** `history.dir` holds one root per source root, made 0700 because it holds every
user's command lines:

```
<dir>/ebpf/<root basename>-<sha1 of its real path, 10 hex>/
    SOURCE.json                                the source root it mirrors
    <table>/day=<D>/<host>.<stream>.parquet    the records, in file order, with _off
    _manifest/day=<D>/<host>.<stream>.json     the rest of what the file could be asked
```

It is a Hive-partitioned dataset you can query yourself, e.g. in DuckDB:
`read_parquet('<root>/exit/*/*.parquet', hive_partitioning = true)`. Row groups are 128 Ki rows: an
hour of a busy login node's exits fills one or two. `history.workers` background processes convert
every sealed file without a current replica, biggest first, at startup and then every minute; a
replica whose source is gone is removed, and with `history.days` set, so is any replica older than
the newest `history.days` dated dirs.

**What it costs and what it buys**, measured on FAS RC's mirror: 8 login nodes, the 7 sealed days
2026-11-10 to 2026-11-16 (112 day-files, 18.4 GB of `.gz`, 182 M records). Converting them took 12
minutes in 8 processes of up to 0.8 GB each, and made 6.2 GB of Parquet, 34 % of the `.gz`. Each
read ran in a fresh process and gave the same answers both ways:

| | from the `.gz` | from the replicas |
|---|---:|---:|
| a session's key search through the busiest day-file (17.4 GB inflated) | 30-43 s | 2.8 s |
| a time search in that file: the first, then each after | 12.3 s, then 0.1 s | 0.17 s, then 0.06 ms |

From the `.gz`, every file a read places is inflated in full to build its seek index. A replica
builds its own, about 30 bytes a line (250 MB for the busiest
file), and indexes are kept for the next read up to 1 GiB and 64 replicas in all.

| setting | env | default | |
|---|---|---|---|
| `history.enabled` | `RC_DASH_HISTORY` | `true` | keep and read replicas; off reads every day-file as JSONL, and leaves the store on disk |
| `history.dir` | `RC_DASH_HISTORY_DIR` | `<cache.dir>/history` | where the store lives |
| `history.workers` | `RC_DASH_HISTORY_WORKERS` | `0` | conversion processes: 0 is up to 8, as the CPUs the service may run on allow |
| `history.days` | `RC_DASH_HISTORY_DAYS` | `0` | the newest dated dirs kept: 0 is every one the source holds |

The eBPF tier's entry in `/api/feeds` carries `history`: `enabled`, `state` (`off`, `idle`,
`converting`, or `error` with a `note`), `dir`, `workers`, `days`, `source_roots`, `store_roots`,
`replicas` (sealed files with a current replica), `files` and `bytes` (its Parquet), and, since
startup, `converted`, `lookups` (sealed files a reader asked for, answered from a `replica` or read
as `jsonl`), `held` (replicas held between reads, and their indexes' `bytes`), `swept` (a killed
conversion's leftovers, removed), `read_failures`, `progress` (`todo`, `done`, `failed`, `records`,
`bytes_in`, `elapsed_s`) and the latest `errors`. `--check-feeds` says how many sealed files have a
current replica and how much is left to convert.

A binary `.ebr` day-file under a root is skipped. To read an EBR-only capture, convert it with the
last `ebr.py`: `git show 2f16731:collector/lib/ebr.py > ebr.py && python3 ebr.py cat x.ebr > x.jsonl`.

## Demo mode

```bash
cd dashboard && uv sync --frozen && (cd web && npm ci && npm run build)
uv run --frozen python -m rc_dashboard demo   # http://127.0.0.1:8080
./run-demo.sh                                 # the same with hot reload, on :8087
```

For showing the dashboard where there is no cluster. `demo` writes a few days of collector records
for fictional login nodes and 28 fictional people (`rc_dashboard/demo/`) into `.cache/demo/feed`,
as JSONL in the collector's own form, keeps appending to them in real time, and starts the unchanged
service over them, so every tab has the kind of data it is meant to show and the Live tab moves.
Costs are drawn from ranges that look like a login node's, and model nothing.

**Nothing in the demo was measured, and the page says so.** Every record carries
`"synthetic": true`, a key no collector writes. The feeds report lists the hosts whose records carry
it (`synthetic_hosts`); while any does, the header shows *fake data* and the Feeds tab marks each
such host. The site is named *demo · fake data*, and its description, which the Ask LLM agent is
given, says no record was measured. The mark travels with the records, so a service pointed at a
demo feed by hand, or a copy of one, still shows it. Use a demo to show the dashboard, never to
report on one.

**What it overrides**, whatever the shell or the config file says (`run.sh` exports a cluster's
feed roots, which a demo must never read): both feed roots and `feeds.ebpf.days`, `cache.dir` (so
no real deployment's saved builds are evicted), `clock.as_of` (the wall clock),
`live.retention_min`, `live.backfill_hours` and `live.window_min` (fitted to the history written),
`panels.preprocess` and `panels.preprocess_wait_s` (every window built before the port opens),
`site.name` and `site.description`, `server.rdns.enabled` (off: no DNS is asked about made-up
addresses) and `server.auth.open` (no password unless one is set). Each reads `demo` as its origin
in `--print-config`. `--ebpf-root`, `--node-root` and `--as-of` are refused.

| flag | env | default | |
|---|---|---|---|
| `--demo-days` | `RC_DASH_DEMO_DAYS` | 3 | history written before serving, 1..14 |
| `--demo-hosts` | `RC_DASH_DEMO_HOSTS` | 2 | login nodes, 1..6 |
| `--demo-seed` | `RC_DASH_DEMO_SEED` | `demo` | another seed is another set of sessions |
| `--demo-dir` | `RC_DASH_DEMO_DIR` | `.cache/demo` | the feed, the saved builds, the stamp |
| `--demo-fresh` | | | discard the feed and write it again |
| `--demo-write-only` | | | write the history and exit |

The feed is a function of the seed, the host count and the clock, so a restart resumes it: it
truncates whatever a stopped run wrote past its stamp (`demo.json`) and writes on from there, nothing
twice and nothing missed. It starts over when the stamp is from another seed, host count or format,
or is older than the history asked for. Only one demo writes a directory at a time (`.demo.lock`).
Three days of two hosts is 84 MB of JSONL, written in about 2 s.
[`docker/demo/Dockerfile`](../docker/demo/Dockerfile) packages the demo as an image (two days of
history, Ask LLM off, the people's working day in New York time), which
[`deploy/cloudflare`](../deploy/cloudflare/README.md) runs at <https://dashboard-demo.agentic-system.org>.

## A capture from another cluster

A feed collected elsewhere and copied here differs from a live one in two ways, and the service
handles both without changing a record.

**It is compressed.** A `<host>.<stream>.jsonl.gz` day-file is read in place (`gz.py`), and once
converted its replica answers the selective reads. Every reader addresses a file by byte offset,
which gzip does not offer, so the first open inflates the file once and keeps a copy of the inflater
every 8 MB of output: reaching any offset then inflates at most 8 MB, and offsets are into the
uncompressed stream, so a byte range or a saved build means the same thing in either form. On
boslogin06's 2026-10-08..09 (330 MB -> 7.9 GB) indexing takes ~6 s and holds ~32 MB, in the
process, so each restart indexes again. A plain file wins over its `.gz` copy. A date dir holding no
day-file is not counted as a day (a copied capture makes its dirs ahead of its files).

**It is old.** On the wall clock every window would end in days of nothing. `clock.as_of`
(`RC_DASH_AS_OF`, `--as-of`) pins the service's present: `auto` is one second after the newest
record, or give a local time. The pinned clock is **frozen** (`clock.py` says why). Every live frame
carries `clock`, the header reads *frozen capture · as of ...* instead of *live*, and the page takes
"now" from the server, so every "ago" agrees with the windows. Every restart builds the same windows over the same bytes, which makes a pinned capture a
test of every tab on another cluster's data; with no capture at hand, the bench
([bench/README.md](bench/README.md)) writes a synthetic feed at fleet density, times the service
over it, and compares every response the page reads before and after a change.

## Is each collector OK?

The **Feeds** tab starts with one row per host and a verdict an admin can act on, judged only from
what the collector wrote (`health.py`): a `meta` record when it starts (kernel, the probes that
loaded and those that did not, the sysctls they need), a `residency_totals` heartbeat every minute,
and a `stop` record when it exits cleanly (why, how long it ran, kernel events lost versus seen).
The dashboard never contacts a collector, so a copied capture is judged exactly like a live feed.

| verdict | means |
|---|---|
| OK | a heartbeat within `feeds.ebpf.max_age_s` (5 min) |
| degraded | running, but losing data it could have: a sysctl a probe needs is off, or its last run lost more than 0.1% of kernel events |
| stopped | no recent heartbeat and a stop record after the last one; its reason is shown |
| silent | no recent heartbeat and no stop record: killed, crashed, or the host is down |
| no heartbeat | files for the host, but no heartbeat in what was read |

A probe the kernel cannot load (`tcp_accept` without BTF on 4.18) is listed as a **limit** and does
not change the verdict, since it holds until the kernel changes. A collector writes its lost-event
count only when it stops, so the run in progress reads "reported when it stops", never 0. Below the
collectors, an optional data source nobody set up reads "not set up", not red.

## The time window

The page asks for a window as `?win=<minutes>` ([web/README.md](web/README.md#the-window-control));
without one, the service's `live.window_min` applies.

**Retention and window are different numbers.** `live.retention_min` (default 7 d) is how much the
process *holds*, and the largest span the picker reaches. `live.window_min` (default 24 h) is only
the view a fresh page opens on. Raising the view costs nothing; raising retention costs memory and
a longer backfill.

At ~1.5 KB per exit record a login fleet writes 10–20 GB a day, so raw events are never held in
memory. A window change is served one of three ways:

| path | how a window change is served | storage |
|---|---|---|
| rolling aggregates | **sliced**: exact, instant, no re-read | per-minute buckets, one slot per minute of retention (7 d ≈ 10 080 slots × a few counters) |
| historical reducers | **rebuilt** from the rows | quantiles, CCDFs, Gini, the dedup index and the trajectory chains carry no time index |
| event stream | cut to the window | a tail of `live.event_tail` (100), **not** an aggregate: a wider window does not lengthen it |

Bins are fixed-width and aligned to the wall clock, so the 1 h view is exactly the last 60 rows of
the 7 d view, and the headline under a chart is the sum of exactly the bins it drew. A window
**wider than retention is clamped, and the page says so**: drawing bins the process never held
would be the same error as imputing a zero.

A rebuild costs as much as the *window*, not the *day*: a day-file is in time order, so
`export.seek_to_epoch` binary-searches for the window's first byte (a 1 h view of a 15 GB day-file
takes ~20 probes and reads one hour), and files wholly older than the window are not opened. Records
are fed **oldest first**, which correctness needs: the shell-wrapper dedup expects a child's exit
before its shell's, and a trajectory's run-length chain is appended in place. A rebuild reports
three things instead of assuming them:

- **Progress.** A wide window takes minutes to build, so its panels show a *third* kind of nothing,
  with a real percentage: neither `none` (which would claim a measurement) nor the empty state
  (which would blame the feed).
- **Drift.** A built view keeps ingesting, so it holds more than its window. The excess is
  published in seconds, and the view re-scans at `panels.max_drift_pct` (25%) of the window, so 1 h
  refreshes often and cheaply and 7 d rarely. A view filtered by user or agent type also re-scans
  once the hour still open at its build has closed ("Disk I/O is each process's own").
- **The seam.** A build reads the day-files while the tail advances through them, so a live record
  is skipped only if the build already read its line or it is older than the window's start
  (`live_skipped`: `already_read`, `before_window`), and rows that overflow the replay buffer are
  published as `replay_dropped`.

Trajectory sessions are never evicted inside the window (`max(reducers.trajectories.idle_evict_s,
window)`), so `reducers.trajectories.max_sessions` (150 000 keys per held window, ~6 KB each, ~950
MB) is the memory bound: past it whole sessions go, least recently active first. A schema-7 feed
keys each tool call apart, and a FAS login node makes 45 000 keys on a median day and up to 105 000,
so a 24 h view holds a busy day whole and a 7 d view of one reaches the cap. Lower it if you raise
retention on a busy fleet, but not below ~50 000: on boslogin06 the sessions active in the last four
hours alone held 52 840 keys.

Built windows are kept in an LRU of `panels.max_windows` (5) and built one at a time on one worker,
so two builds never compete for the page cache; readers of one window share its build and its
WebSocket frame. The default window starts building when the service starts.

**Every build is saved, and resumed instead of repeated.** At the end of each scan the reducers are
pickled to `<cache.dir>/windows/` with how far into each day-file the scan read: `(st_dev, st_ino)`,
a digest of the first 4 KB, and the offset past the last complete line. A restart, or a return to a
window the LRU evicted, loads that and reads on from those offsets. On one login node's feed a 24 h
window goes from ~7 s to ~0.2 s and a 7 d window from ~42 s to ~1.4 s, plus the catch-up. A snapshot
is ~12 MB per day of window; `panels.cache_keep` (8) bounds how many are kept. A resumed view
reproduces a fresh scan's panels exactly (`tests/test_window_cache.py`) and keeps its original
`built_at`. A snapshot is ignored, and the window scanned afresh, unless all of these hold
(`cache.note` says which failed):

- the same code and config: a fingerprint of the reducers, the normaliser, the scan and every config
  key outside `server`, `anonymize`, `ui`, `ask`, `history` and `rollup`, so a changed way of
  counting never resumes counts made the old way;
- the same files: each one it read still there with the same inode *and* first 4 KB, at least as
  long, and a line ending at the saved offset (a day gzipped since is read on from its `.gz`, the
  same bytes at the same offsets);
- built within the window's drift budget;
- for a view filtered by user or agent type, the hour open at its build not closed since
  (`own_open_hour`).

Pickle runs code for whoever can write the file, so the directory is created 0700, files 0600, and a
symlink or a file another user owns or can write is never loaded. Each view's panels, credentials
masked, are saved as well (`<cache.dir>/payloads/`, `payloads.py`), so a view created after a
restart, or for a window the LRU dropped, shows them at once with their age and a "rebuilding" pill
(`cache.restored`) until its own build swaps fresh ones in; they are shown only for the same code,
config, window and filter within the drift budget (else `cache.panels_note` says why), and no build
reads them. `panels.cache_windows=false` (`RC_DASH_CACHE_WINDOWS=0`) turns all of it off.

### Hourly merge-reduce

With `panels.merge_reduce` on (the default; `RC_DASH_MERGE_REDUCE=0` turns it off), each record is
parsed and reduced **once**, into the state of its epoch hour, saved under `<cache.dir>/hours/`
(`hours.py`). An unfiltered window is then its records up to the first whole hour, read as before
(so its start is exact); every closed hour inside it, merged from the saved states (a whole epoch
day from one saved merge of its 24 hours, `panels.day_cache`); and the open hour, read as before.
Every window reducer's `merge(other)` folds a later state in exactly as feeding it the later records
would, so a merged window is the window one pass builds (`tests/test_merge.py`,
`tests/test_hours.py`). The quantile panels are a mergeable sketch: `n`, `max` and `mean` exact, each
quantile within 1% (`rel_err`), `sampled` false.

Joins that cross an hour (the shell-wrapper dedup, the process index's parent requests, a sandbox
wrapper absorbing its payload) are carried from each hour into the next, so hours are reduced in
order. A carried entry untouched for 24 hours is aged out, the one place a merged 7 d window differs
from one pass over 7 days. An hour closes once the newest record is `panels.hour_grace_s` (300 s)
past its end; a record that arrives later is merged into its hour's saved state, not lost.

On one login node's feed a 24 h window then builds in ~2-3 s and a 7 d one in ~8 s after a restart,
a window switch or a drift refresh (against ~15 s and ~160 s scanned); filling the hours the first
time costs about what one scan does. A week of hours is ~38 MB on disk. A view filtered to users or
agent types is still scanned, but takes its exits' own disk bytes from the hours, which save them by
user and agent type (`o<hour>`; "Disk I/O is each process's own").

In a run of hours, a reader of a sealed `.gz` is handed the 8 MB span another reader of the same file
inflated last (`gz.shared_spans`), so an hour's time search, its read and the next hour's search do
not each inflate it again; past 64 files (`hours.SHARED_FILES_MAX`, 768 MB at most) nothing is
shared. On a 6-node bench feed whose files grow 1 to 3 MB an hour, a 24 h window's hours inflated 132
spans instead of 455 and were reduced in 22.8 s instead of 26.3 s; a FAS login node's file grows ~140
MB an hour, so there it saves about one span per file per hour.

**Reducing by node, in parallel.** A reducer is pure Python and holds the GIL, so the service's
threads share one core: one pass over an hour of the eight FAS login nodes (1.45 M records, 2.7 GB of
JSONL) takes ~95 s of it, under 10% of that decoding. With `ingest.workers`
(`RC_DASH_INGEST_WORKERS`, 1 by default) at 2 or more, each node's share of an hour is reduced in its
own worker process, at most that many at once, and the saved hour is the node states merged in node
order; 0 means one worker per CPU the service may use. A process, its session, its tree and its
wrapper are all on one node, so the joins stay exact. With `ingest.reducer_groups`
(`RC_DASH_INGEST_REDUCER_GROUPS`, 1 or 2, 2 by default), each node's hour is reduced in two
processes, one per group of reducers that share no state (`hours.reducer_groups`): the session reducers and the
process index they fold through (`_procs`, `trajectories`, `workspace`, `sandbox`, `slurm`,
`cascades`), and the counting panels (`resources`, `io_process`, `tool_mix`, `risk`, `facets`). A
node is split only when there are at least as many workers as nodes. Measured on rtx6000b (32 cores,
shared with other users' load) over the eight nodes' feed of 2026-11-16, with Python 3.14 (5-10%
faster than 3.12, with identical panels):

| workers (reducer groups) | 1 | 4 | 8 (1) | 8 (2) | 12 (2) | 16 (2) |
|---|---|---|---|---|---|---|
| 15:00, a balanced hour (1.45 M records) | 90 s | 28 s | 22 s | 18 s | **15 s** (6.2x) | 15 s |
| 11:00-14:00, boslogin07 at 2 GB/h (4.94 M) | 367 s | 120 s | 119 s | 111 s | **92 s** (4.0x) | 101 s |

The balanced hour is bound by boslogin06 and the merge into the hour (~4 s an hour in the service's
own process), the spike hours by boslogin07, whose records cost more to read than to reduce. Two
workers per node (one per node and group) is the most that helps; it is `hpc/deploy`'s default
(`INGEST_WORKERS` empty in `site.env`). Each is ~0.5 GB (16 of them ~7.4 GB, plus ~1.7 GB in the
service while it merges), counted against the service's memory cap. With a worker for every unit,
each unit keeps to its own worker (`hours.UnitPool`), unlike the shared pool the table was measured
with. Threads would not do instead: on the free-threaded 3.14t each thread is ~18% slower, and orjson
has no free-threaded build.

Counts, sums, sets, quantile sketches and every panel are the same for any worker count from 2 up;
merging nodes' states differs from one pass only where `merge` keeps one state's value (a "newest
record's" field, a home directory learned from one node's argv, rows that tie at a cap, the last
digit of a float sum). The split is part of the hours' fingerprint but the count is not, so going
from 1 worker to more, or back, reduces the hours again. A worker runs the service's own code or
nothing: after a deploy since the service started, the nodes are reduced in the service's own
process until it restarts (`hours.spawn_identity`; `node_note` in `hours.status` says so).

**Preprocessing everything.** By default only the windows readers ask for (and the cheap
`panels.prewarm_min` ones) are built, so the first switch to a wide window waits while the page says
*merging*. With `panels.preprocess` (`RC_DASH_PREPROCESS=1`) the service reduces every closed hour in
retention at start-up, merges its whole days, and builds and holds **every** preset window
(`max_windows` raised to fit). With `panels.preprocess_wait_s` (`RC_DASH_PREPROCESS_WAIT_S`) above 0,
it does so before opening its port, up to that deadline. It is off by default because a held 7 d
window on a busy node can take ~1 GB.

## API

| endpoint | purpose |
|---|---|
| `GET /api/config` | resolved config, `_origin` per key |
| `GET /api/feeds` | per feed: configured vs resolved paths, status, lag, rows, notice, backfill; the eBPF tier's `history` ([History in Parquet](#history-in-parquet)) |
| `GET /api/collectors` | per host, whether its collector is OK: verdict and reasons, heartbeat age, every run and how it ended, probes that could not load ([Is each collector OK?](#is-each-collector-ok)) |
| `GET /api/windows` | offered windows, the retention ceiling, and every held window's build state |
| `GET /api/panels?window_min=&user=&agent_type=` | historical aggregates for that window + its build state; `user` / `agent_type` (repeatable, `none` = untagged) narrow it to a view of its own, built and saved like a window; `filter` names it, `facets` lists the window's users and agent types |
| `GET /api/live?window_min=` | rolling window: per-minute bins, hosts, the user roster, event tail, submits |
| `GET /api/live/users?window_min=&q=&limit=&running=` | the user roster past the busiest rows the live frame carries; `q` searches it (the aliases, when names are anonymized), `running=agent:<type>` keeps the accounts with such a session up |
| `GET /api/live/user?name=&window_min=&n=` | one account: the sessions it has running and its last `n` (25) exited processes |
| `GET /api/trend?days=&by=` | the Trend tab: the roll-up table's last `days` (1..366, 365 by default) local days in `by` buckets (`day`, `week`, `month`): each class's processes, CPU, distinct users and submissions, the agent types and the hosts, and the table's own state ([Trend](#trend-up-to-a-year)) |
| `GET /api/trajectories?rank=&window_min=&user=&agent_type=` | ranked sessions in that window, narrowed the same way; an agent's row carries its tasks and phases ([below](#sessions-tasks-phases-and-processes)) |
| `GET /api/trajectory?sid=&window_min=&from=&to=&order=` | one session as a **process tree**, re-read on demand, `oldest` or `newest` first (below) |
| `GET /api/users?window_min=&user=&agent_type=` | every user with an agent session in the window, the window's totals (`fleet`), the agent sessions of no named user (`unattributed`), and the traits' rules ([below](#users-a-portrait-of-each-user)) |
| `GET /api/user?name=&window_min=` | one user's portrait; `name` takes the alias when names are anonymized |
| `GET /api/io/commands?<filters>&window_min=&apid=&host=&cwd=` | starts a lookup of every command behind an I/O figure, over the window's records (`iocmds.py`); `apid` and `host` name the agents as the page drew them, `cwd` a directory |
| `GET /api/io/commands/{job}?offset=&limit=&sort=&dir=` | that lookup's state and, once done, a page of its rows (200, by `rd_mb`, largest first, unless asked otherwise) |
| `WS /ws` | push: `live`, `feeds`, `rebuilt`, `backfill`, `windows`; accepts `{"type":"window","minutes":N}`. Each reader has a sender of its own holding the newest frame of each kind, so a slow reader holds up neither the tail nor the other readers; one that takes no frame for 5 s is dropped (close 1008), and past 64 readers one is turned away (close 1013: the page retries, then polls) |
| `GET /api/health` | `ok` false, and `why`, when the service is not doing its work: the tail has not moved (no poll ended, no batch handed over) for 5 poll intervals (5 s at least); a background loop failed its last 3 rounds in a row; or a view could not take a record in 3 of the tail's batches in a row. One error alone flips nothing. Also `ingest`, each loop's last error and `streak` (`loops`), the readers (`websocket`), the backfill, the windows, the garbage collector's collections and how often the heap's free pages were handed back (`gc`: `trims`, `last_trim_ms`), `anonymize`'s counts and the memos. A loop's or a view's error is also logged to stderr when it changes, and once a minute while it repeats |
| `GET /login`, `POST /login` | the sign-in form, and signing in: a session cookie, then back to `next` ([Password](#password)) |
| `POST /logout` | signing out: the cookie cleared, then the form |

With a password set, every route above asks for it. `POST /api/ask`, `GET /api/ask`,
`GET /api/ask/{job}` and `/api/suggestions/done` (`GET`, `POST`, and `DELETE
/api/suggestions/done/{id}`) are [Ask LLM](#ask-llm)'s. Every `/api` request but a GET, HEAD or
OPTIONS takes `Content-Type: application/json` only (a 415 otherwise, `guard.JsonOnly`), which
another site's page cannot send without the browser asking first; a body past 1 MB is a 413, and a
malformed one a 400. The page's routes for panels, sessions and users (`/api/panels`,
`/api/trajectories`, `/api/trajectory`, `/api/users`, `/api/user`) leave out the method notes,
`notes`, `basis` and every `*_basis` key (`jsonout.strip_basis`): the page shows none of them, and
Ask LLM reads them where the service holds them. `window_min` is optional everywhere: omitted, it is `live.window_min`. A value
above retention is clamped, and the response carries `requested`, `clamped` and a `note`.

`/api/trajectory` (`proctree.py`) is the inspector behind a clicked session row. No reducer holds
per-process rows (one day's busiest session on rtx6000a ran 1.59 M processes), so it re-reads the
window's day-files for exactly the records the row folds. Parents come from each record's own
`ppid`; one placed on weaker evidence (the process index, the collector's `depth`, the fold) says so
in `link`. Consecutive siblings of one shape fold into counted groups. Its bounds, under
`reducers.trajectories`, are each reported in the response, never applied silently: the read stops
at `inspect_max_records` (150 000) or `inspect_max_bytes` (6 GiB) and says where (`scan.stopped_at`;
`from=` continues from it); at most `inspect_hold` (20 000) processes, and `inspect_per_parent`
(2 000) childless ones of one command per parent, are itemised, the rest counted under their
parent; `inspect_nodes` (6 000) rows are served, breadth first. Every bound keeps the session's
**oldest** end; `order=newest` keeps the newest instead, reading on to the session's end, up to
`inspect_newest_records` (600 000), with the same totals. Command lines are masked (`redact.py`),
then cut to 220 characters. One read runs at a time, and its tree is served again for ten minutes
while the window is the same build.

**How a read is made.** The records are built and normalised in
`reducers.trajectories.inspect_workers` worker processes (8, and no more than the CPUs the service
may use; 1 builds them in the service's own process), each taking a part of a day-file, and replayed
in file order with the one-process read's counters, so every bound stops a read at the same record
either way (`tests/test_keysearch.py`). What a read costs is in
[INSPECT](../docs/figures/README.md#inspect-the-session-inspectors-re-read).

The `<filters>` of `/api/io/commands` are the one filter grammar (`export.parse_filters`):
`from to class[] agent_type[] user[] host[] tool[] purpose[] bucket[] session_key sandbox[]
approval[] depth_min depth_max cpu_min duration_min rd_min wr_min exit_code[] signal[] outcome[]`;
`rd_min` and `wr_min` are MB read and written, at least; `outcome` is how a process ended, in the
words of [Commands that failed](#commands-that-failed): `ok`, `nonzero`, `answer`, `signal`, `pipe`
or `none`.

**Credentials never leave the service.** Tokens, passwords, keys and signed URLs on a command line
become `[REDACTED]` on every way out (built panels, the live frame, the session inspector, an I/O
lookup's commands); the flag or variable name is kept, so a reader sees
what was there. Records stay raw in memory, where the secrets, sandbox and approval detectors read
them. The rules (`redact.py`, matching the collector's word for word) are a deny-list, so a secret in
a shape they do not know is shown as it is (`collector/README.md` lists the shapes). Each reads a
text in linear time; a text past 100,000 characters is cut there and marked `[CUT]`.

## Conventions the panels obey

These rules protect the integrity of the research; each exists because this project made that
mistake once.

- **Three classes, never two:** `agent` / `human` / `ide` (+ `unlabeled`). `ide` is any editor a
  person drives: the VS Code, Cursor and Windsurf remote-server trees the collector tags (it calls
  the first `human-vscode` and counts the other two as agents), and other editors' servers, known by
  their executable path (`normalize.ide_class`). An agent CLI started in an IDE's terminal or by its
  extension keeps its agent type and stays `agent` (`roles.py`). Folding the IDE class into "agent"
  produced a GPU near-parity claim that was later retracted. `actor3` is derived (`normalize.py`),
  never approximated from the binary `actor`, which would silently empty the IDE class.
- **Count and cost, side by side:** every categorical panel ships an event-weighted and a
  CPU-weighted chart, each on its own single axis, never a dual axis: `poll/probe` is ~28% of an
  agent's tool calls and ~0% of its CPU.
- **Null is not zero:** a dropped BPF block emits `null`. Nullable fields carry `coverage_pct` and
  are never imputed; a field present but always exactly zero is flagged `all_zero` and renders as
  **not measured**.
- **Zero is not missing:** a present feed with no matching rows says `none`, while an absent feed
  names its configured path.
- **`sandbox` and `approval` are independent axes.** `--dangerously-skip-permissions` bypasses
  approval but leaves the sandbox; `--dangerously-bypass-approvals-and-sandbox` disables both.
- **`sandbox` is namespace isolation, and labelled so.** The collector compares `/proc/<pid>/ns/*`
  with pid 1's, so Landlock and seccomp-only sandboxes are invisible to it, and a container, an
  apptainer user namespace or a `PrivateTmp=` unit counts as sandboxed. It has **four** states.
  `denied`, shown as "exited before it was checked", means the namespace links were already gone
  when read (every such record on rtx6000a lived under 6 ms); it is never folded into
  `unsandboxed`. Every share is over the known verdicts, with coverage beside it.
- **A session is judged by its tool calls.** Claude Code and Codex wrap each tool call but never
  themselves, so the session root's own verdict says nothing about confinement. A tool call is a
  sandbox wrapper invocation (bwrap, codex-linux-sandbox; nested ones folded into the outermost) or
  a Claude Code Bash shell no wrapper encloses, and what ran under the wrapper decides it:
  `isolated`, `not_isolated`, `failed` (the wrapper exited non-zero and no payload ran: the sandbox
  did not come up, as for all 612 codex-linux-sandbox runs on rtx6000a), `no_payload` or `unknown`.
- **Approval is read from the session root's own argv, and claims no more than that.** Only a
  process whose role is `agent` (not an IDE's) carries a verdict: `bypassed` (a bypass flag at
  launch), `auto` (`--permission-mode auto`), `supervised`, shown as "no bypass flag at launch"
  (settings files, codex `config.toml` and a mid-session switch are invisible), `unobservable`
  (`codex app-server` takes its policy per thread), `na` (a tool shell, probe, IDE helper or human
  process) and `unknown` (a child; its verdict is on the root).
- **Where a session is driven from is read from the same argv, and `unknown` is never guessed.**
  Every agent session root gets one of five surfaces (`normalize.surface_of`, whose `SURFACES` lists
  the evidence). CLI: `terminal` (the agent's own interactive UI, `claude daemon` background sessions
  included) and `script` (`claude -p`, the Agent SDK, `codex exec`, an MCP server). App: `desktop`
  (the Claude desktop app's Code tab or the Codex app, over SSH), `extension` (an IDE extension's
  bundled binary) and `web` (Remote Control). The mode is checked before the path, so the extension's
  binary run by hand as `codex exec` is a `script`. A Codex app-server that serves whichever client
  attaches is `unknown` unless its terminal UI started it. A host (the desktop app's server,
  `claude rc`, `claude daemon`) is never a session, but a row keyed on one says where its sessions
  are driven from.
- **CPU is `sum(cpu_s)`, self time only, counted once per process.** `child_cpu_s` is never added:
  upstream (`analyze/common.py`) the naive sum inflates login CPU 1.83×, *unevenly by class*, which
  would invert an agent-vs-human comparison.
- **A process is counted once, at its exit.** At each stop the collector writes a `truncated` record
  for every tracked process still running, with its CPU and I/O so far, and the process's exit after
  the restart carries all of it again. So every count and sum uses `exit` records only; a
  `truncated` record still identifies a running agent root, and the inspector draws it as alive at
  the collector's stop.
- **Disk I/O is each process's own.** The collector's `io` is `task->signal->ioac`, which the kernel
  also credits with the I/O of every child the process reaped, so a shell's figure holds its
  commands' and an agent's its tool calls'. Every figure that sums disk or syscall bytes (the I/O
  panel, the tool mix, the I/O series, the blast radius, the sessions, the live roster, the
  inspector's subtree totals, the commands behind a number) takes each process's figure less what
  its exited children carried (`reducers/own_io.py`, which documents the join), so a command's bytes
  count once. The hours, which read every record ("Hourly merge-reduce"), work the figure out once,
  and a filtered view, which never reads what its filter leaves out, takes each closed hour's exits'
  figures from them: a process reads the same in any view. Only in the hour still open, and in what
  a filtered view is fed live, does the view take off just the children it read (`cache.own_io`
  says from which hour), until that hour closes and the next rebuild tick (`rebuild.interval_s`, 15
  minutes) builds the view again: about one filtered scan an hour per held filtered view. With
  `panels.merge_reduce` off, every view joins only what it reads.
  What the record cannot show is counted. A pid can be reused, so a process takes off only its own
  user's children (root aside) that began no more than a minute before its recorded start (its last
  exec): a
  launcher that execs over a minute after forking is refused its child, whose bytes then count twice
  (`rejected`; one child in 2.7 M on four million records). A parent that reaped none of its
  children (its `child_cpu_s` is 0) takes none off (`unreaped`), one that reaped only some comes out
  short, and a figure short of its children's shows as 0, never below (`clamped`). Each join holds
  `reducers.own_io.max_parents` parents (2000; below 1 the service refuses to start); a parent
  dropped past it is counted (`evicted`) and said on stderr at most hourly, its children's bytes
  then counted again at its exit. A service holds some 36 joins: at one entry a parent (~0.5 KB)
  2000 is ~40 MB in all, at worst (eight, ~2.5 KB) 164 MB. The repo's streams hold under 500
  parents at once; a busy login node may need more: raise it while `evicted` grows, at 19 to 80 KB
  for each one added.
- **"Top" is seven rankings, not one**: `events`, `cpu_s`, `peak_rss_mb`, `io_mb`, `net_mb`,
  `distinct_tools` and `recent`, because a single ranking is dominated by one poll loop and one
  mega-user. `recent` lists real sessions only, so a status-bar
  probe, or a tool call whose shell has not exited yet, cannot hold the top spot. The list is
  re-ranked from the live feed when read (`reducers.trajectories.live_s`, 5 s).
- **Scope is stated:** the eBPF tier runs on login nodes, so its resource figures describe
  login-node use, not cluster use.

## Risk rules differ by cluster

What counts as risky is up to each site: `nvidia-smi` is pointless on a login node with no GPU and
sensible on a GPU dev box, and compute node names, tools and token formats differ everywhere. The
rules are built in (`reducers/risk.py`) and changed per cluster in the config's `risk` section, or in
a **profile** merged over it: `RC_DASH_RISK_PROFILE=risk-profiles/gpu-dev.json`. The profile's
content is part of the resolved config, so a changed rule re-scans the saved windows instead of
resuming counts made under the old one. A secret pattern a cluster adds is also masked everywhere a
command line is shown (`redact.extend`). A rule that does not compile is left out and named on the
Risk tab, whose **Rules in effect** lists every rule and where it came from.
[`risk-profiles/README.md`](risk-profiles/README.md) documents every key, every built-in rule and
the two shipped profiles, and [`example.json`](risk-profiles/example.json) uses every key.

## Anonymized names

`anonymize.enabled` (`RC_DASH_ANONYMIZE=1`) replaces every username, lab or Slurm account name,
email address and numeric user ID in everything the service sends out (every HTTP response and
websocket frame, and the Ask snapshot) with a stable alias: `user-7f3a9c1e`,
`lab-1b2c3d4e`, `[email]`, `[uid]`. It is off by default, so a deployment for the cluster's own staff
shows real names. The header says *anonymized* while it is on.

- **Stable, not reversible.** An alias is 8 hex characters of an HMAC keyed with a secret salt:
  `anonymize.salt` (16 bytes or more, or the service stops), or else `<cache.dir>/anonymize.salt`,
  generated once with mode 0600. An account has the same alias in every tab, window and restart, and
  without the salt neither a list of candidate names nor sending them to the service reverses one.
  Changing the salt reshuffles every alias. `/api/config` and `--print-config` never show it. A salt
  file that is empty, not hex or too short stops the service: delete it, and the next start makes a
  new salt, with new aliases.
- **What counts as a name.** Every account the live roster holds or a `user` field names, and the
  accounts the config's filters name; any segment of a home or lab path, masked in place even for
  someone who never ran anything here (`/n/home08/<name>/`, `/n/holylabs/<lab>/Lab/<name>/`, Claude
  Code's `-n-home08-<name>-` spelling of a folder, `~<name>/`); and labs, from a Slurm `--account` or
  `-A`, from `<x>_lab`, and from lab paths. Names match as whole words, a dotted name hyphenated too,
  and a name clipped off the end of a long command line by its prefix. The patterns are
  `anonymize.home_patterns` and `anonymize.lab_patterns`; the defaults (`anonymize.HOME_PATTERNS`)
  know FAS's layout. System accounts (uid below 1000), `anonymize.keep` (`root`), the class names
  and the agent types are never aliased.
- **Where.** At the exits, after the reducers, like `redact`. Reducers and saved state keep raw
  names, and `anonymize` is in neither saved-state fingerprint, so switching it needs a restart,
  never a rebuild. If anonymizing fails, the response is a 500 with no body, never the raw payload.
- **Requests.** A real name sent in place of an alias finds nothing, so the service cannot be asked
  whether someone is here, or which alias is theirs. A `user` filter, the user drill-down (`name`)
  and a session id (`sid`, whose `tty:` and `usr:` keys name an account) take an alias; anything else
  maps to `user-unknown`, which matches no record. The roster's search (`q`) and the `host`,
  `tool` and `cwd` filters match the text as the reader sees it. What a response echoes from a request (a
  view's filter and status, an I/O lookup's query, a session id, an Ask
  question and answer, the suggestions marked done) goes back exactly as sent (`anonymize.Verbatim`).
- **Ask is refused while names are anonymized**, unless `ask.unmasked_answers` is set: its answer is
  shown as the agent writes it, and the agent can read whatever the service's account can, raw feeds
  included. The suggestions marked done (`/api/suggestions/done`) are refused too, deleting one
  included; to clear them, edit or remove `<cache.dir>/ask/done.json` by hand.
- **Not covered.** A person's name in free text that no rule ties to an account (a commit message, a
  folder named after someone), hostnames, partitions and job ids; and an account named in another's
  command line, outside a home path, before any response has named it. In `/api/health`, `anonymize` counts the names it knows, the collisions (two names, one
  alias), and the accounts left visible because their name is a reserved word, never a name.

`tests/test_anonymize_leak.py` reads every route and the websocket over a feed full of names and
finds no name in any byte; its `ReaderTextTest` sends a real name in every query, path and body
parameter of every route and finds neither that name's alias nor any of that account's rows.

## Password

`server.auth.password_hash` (`RC_DASH_PASSWORD_HASH`) puts the whole service behind one password:
the page, its assets, every `/api` route (Ask LLM included) and the websocket. It is empty by
default, and then `serve` refuses to start, because on a shared host "bound to 127.0.0.1" means
"readable by every local user". `server.auth.open` (`--open`, `RC_DASH_OPEN=1`) serves without a
password anyway, to anyone who reaches the port. A [demo](#demo-mode) is open unless a password is
set.

```bash
cd dashboard && .venv/bin/python -m rc_dashboard new-password --netrc-host dash.example.org
```

generates a 16-character password with `secrets` (both cases, digits and 18 symbols, at least one of
each: about 101 bits), writes it to `~/.config/rc-dashboard/password` with a netrc for curl beside
it, both mode 0600, and prints only the salted scrypt hash. Put the hash in `dashboard.config.json`,
then restart:

```json
"server": { "auth": { "password_hash": "scrypt$32768$8$1$..." } }
```

- **Browsers** get a sign-in form, with a 401, at whatever URL they asked for, and land back on it.
  The session is a cookie that lasts `server.auth.session_days` (30): HttpOnly, SameSite=Lax, Secure
  when the request came over https (a proxy such as cloudflared reports this in
  `X-Forwarded-Proto`), and named `rc_dash_<port>`, so two instances on one host keep separate
  cookies. *Sign out*, at the bottom of the sidebar, ends it.
- **Scripts** send the password as HTTP Basic auth, with any user name:
  `curl --netrc-file ~/.config/rc-dashboard/netrc http://127.0.0.1:8080/api/health`, so it never
  appears on a command line, where `ps` would show it.
- **What the service keeps.** Only the hash. A password is checked with scrypt, one check at a time,
  in constant time; a wrong one waits an extra second. The cookie holds an expiry and its HMAC, keyed
  from `<cache.dir>/session.key` (created once, mode 0600) and the hash, so a new password or a
  deleted key file signs everyone out. `/api/config` and `--print-config` show the hash as
  `(hidden)`.
- **A restart, not a rebuild.** `server` is in neither saved-state fingerprint, so setting, changing
  or clearing the password needs only a restart. A malformed hash stops the service at startup
  instead of letting it serve without a password.

**Who may connect** (`guard.py`), with or without a password:

- **A websocket from another page's origin is refused** with a 403: any whose `Origin` names a host
  and port other than the request's `Host`, a loopback origin on another port included. A browser
  lets any page open a websocket to 127.0.0.1, and from another port of the same host (a dev server,
  a notebook, another tunnel) it sends the reader's cookie too, since every port of a host is the
  same site. A script sends no `Origin` and is let through. A proxy that rewrites the Host to
  `127.0.0.1:<port>` needs its public origin in `server.allowed_origins` (`RC_DASH_ALLOWED_ORIGINS`,
  comma-separated: `https://dash.example.org`). Vite's dev server is another origin too:
  `server.dev_origins` (`RC_DASH_DEV_ORIGINS`), which `run.sh` sets, adds to `allowed_origins`
  ([web/README.md](web/README.md)).
- **With no password, a request under a host name that is not this host's is refused** (a 403). Its
  `Host` must be a loopback name, the bind host (`server.host`), or one of `server.allowed_hosts`
  (`RC_DASH_ALLOWED_HOSTS`; `*` for any); otherwise a page on another site could point its own name
  at 127.0.0.1 and read every route (DNS rebinding). With a password the cookie already prevents
  that, so the check is off. The demo image answers under any name, its data being made up.

`tests/test_auth.py` reads every route, an asset and the websocket with and without the password;
`tests/test_guard.py` sends the handshakes a browser, a script and Vite's proxy send.

## Ask LLM

The Ask LLM tab, and the *ask* button on a panel or a session, run an agent CLI on this host
(`ask.py`): by default `codex exec --sandbox read-only`, as the service's account with its Codex
login, given the question and a compact, masked snapshot of the window. The snapshot leaves the
host, for the agent's provider.

**The agent can read whatever the service's account can read.** Its sandbox is read-only, not
confined: the collector's files, the service's cache (the anonymize salt and the session key among
them) and that account's home are within its reach, and its question is text a reader wrote. Run
the service as a dedicated account that holds no other credentials, not as a person's. So:

- **Off by default.** `ask.enabled` (`RC_DASH_ASK=1`) turns it on.
- **Never without a password.** While `server.auth.password_hash` is empty, Ask is refused even when
  turned on: `GET /api/ask` says why, and a question gets a 400 and reads nothing.
- **A short environment.** The agent gets only what a program needs to run, find its login and reach
  its provider (`ask.AGENT_ENV`, `ask.AGENT_ENV_PREFIXES`): `PATH`, `HOME`, `USER`, `LOGNAME`,
  `SHELL`, `LANG`, `LANGUAGE`, `LC_*`, `TZ`, `TMPDIR`, `TERM`, `LD_LIBRARY_PATH`,
  `XDG_RUNTIME_DIR`, `XDG_CONFIG_HOME`, `XDG_CACHE_HOME`, `XDG_DATA_HOME`, `XDG_STATE_HOME` and
  `DBUS_SESSION_BUS_ADDRESS` (for `systemd-run --user`, as `hpc/deploy` runs it), the proxy and CA
  variables, `CODEX_HOME`, `OPENAI_*`, `ANTHROPIC_*` and `CLAUDE_*`. Anything else it needs, such as
  a provider reached through a cloud, is named in `ask.env_passthrough`
  (`RC_DASH_ASK_ENV_PASSTHROUGH`, comma-separated): variable names, or a family's prefix of three
  characters or more and then `_*` (`["AWS_*", "GOOGLE_APPLICATION_CREDENTIALS", "CLOUD_ML_REGION",
  "AZURE_*"]`); a shorter prefix, or one without the `_` (`S*`), is refused at start. The service's
  own `RC_DASH_*` settings, its password hash among them, are never passed, and a failing agent's
  error says it ran with only part of the environment. **The proxy variables are passed as they
  are** (`HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY`, `NO_PROXY` and their lower-case forms), so a user
  and password in a proxy URL (`http://user:pass@proxy:3128`) can be read by an agent whose prompt
  holds other users' command lines: for the service's account, prefer a proxy that needs none.
- **A directory of its own**, made 0700 in the system's temporary directory for each question and
  removed after, not under `cache.dir`, so the salt, the session key and the saved builds are not its
  neighbours.
- **A short question**: at most 4 000 characters (a 400 past that); the prompt carries the last three
  earlier turns, cut to 600 and 1 500 characters a side.

`ask.agent` picks a preset (`codex`); `ask.command` gives the argv outright, with `{workdir}`,
`{answer_file}` and `{model}` filled in and the prompt on stdin. A command, or the instructions in
`ask.system_prompt`, may hold a provider's key, so `/api/config` and `--print-config` show both as
`(hidden)` when set, and `GET /api/ask` names only the program a command runs (`claude` for
`env ANTHROPIC_API_KEY=... claude -p`). `tests/test_ask.py` checks the refusals, the environment and
the directory.

## Network endpoints

The Network bytes panel (Resources tab, I/O section) lists the ten remote endpoints the processes
made the most send and receive calls to, or with a switch moved the most bytes to and from, each
row split by class; each class's bytes sent and received in all are rows of Who uses what, on
Live. It is counted from the collector's `net_endpoints` block (schema 7) at each exit, so an
older capture shows no list.

- **One row per name.** The service names each address when the panel is read (`endpoints.py`): by
  reverse DNS from this host if it can, else by a name a command line gave for it (the collector's
  `host`), else as the address. Addresses that share a name are one row; inbound endpoints, whose
  address is a client's, are kept apart from outbound ones. A name's hover gives its addresses,
  ports, and where the name came from.
- **Lookups never hold up a request.** Background threads look up the addresses, busiest first, keep
  the answers in `<cache.dir>/rdns.json` (mode 0600), and look a name up again after a week, a "no
  name" after a day and a failed lookup after ten minutes, serving the old answer meanwhile. They use
  this host's resolver, so a private address in another site's capture is named as this host names
  it.
- **What it cannot show is said**: the note under the list gives the share of the classes' bytes
  whose endpoint is known, the bytes a block held with no address, and the processes that moved
  bytes with no endpoint block.
- **A restart, not a rebuild.** `server.rdns.enabled` (`RC_DASH_RDNS`, on by default) turns the
  lookups off, and names then come only from command lines; `server.rdns.workers` (4) sets how many
  run at once. A name is applied when the panel is read, never saved, so neither setting affects a
  saved build.

## Login node health

The node tier covers what the eBPF tier cannot see, because none of it is a process: the whole
node's load and memory, how quickly it responds, its NFS mounts, its local disks and each account's
cgroup. The `node` panel (`reducers/resources.py`, `NodeReducer`) holds the newest two snapshots per
host, drawn first on the Resources tab as **Login node health**: one table at a time (nodes, NFS
mounts, local disks, accounts, stuck processes), each choice with its count and how many items in it
have a problem.

- **The service sets the thresholds.** A local filesystem at or above `node.disk_full_pct` (90) of
  its space or `node.inode_full_pct` (90) of its file slots is `full`, on Needs attention too.
- **An account held back** is one whose cgroup's `throttled_usec` grew between the two snapshots:
  the kernel paused it at its CPU limit. Its CPU is a rate over the same interval, in cores
  (`cpu_cores`): null with one snapshot, and `counter_reset` (no rate, which is not idle) when the
  cgroup was recreated between them.
- **Stuck processes** are the snapshot's `d_state_procs`, up to 20 per host with the rest counted:
  pid, account, wait channel, the kernel stack's top frames where readable, and the command line,
  masked (`redact.py`). The eBPF tier's `d_stack_top` (what its tracked processes were blocked in)
  is on the live frame's host rows, and the hover on the D-state count shows both.

### Over the window

**Login node response**, on Resources under the window's CPU chart and zoomed with it, plots four
node tier numbers per host over the window: the time to start a process and to look up an account
(the collector's own probes), the slowest NFS mount's read round trip, and the load. A spike there,
under the agents' CPU above, is what "do agents make the node slow for people?" looks like.

- **Read on its own thread** (`nodehist.py`), so nothing waits for it; the panel shows how far the
  first read of retention has got. It reads `feeds.ebpf_node.series_days` day-dirs (0: enough for
  retention, whatever `feeds.ebpf_node.days` says).
- **No resolution the samples never had.** A bin is the window's own width or twice the snapshot
  interval (`EBPFM_NODE_INTERVAL`, 300 s), whichever is wider, so a hole always means a missing
  snapshot. A latency bin shows the slowest probe in it, and a load bin its mean.
- **Hosts are not classes**, so their lines use hues no class uses. Past five hosts, the five that
  stand out most are named, and the rest are one grey line, the slowest of them in each bin.

## Trend: up to a year

**Trend** is a tab of its own: each class's processes, CPU, users and Slurm submissions per day,
week or month, over up to a year -- 30, 90, 180 or 365 days -- with the users of each agent type
and each login node's processes beside them, over what the **roll-up table** holds, which outlives
retention and the day-files (`rc_dashboard/rollup.py`). It is the one place for per-day use: the
charts on Live and Resources cover the selected window only.

**The table is filled as records are ingested.** Every exit and submit the live tier takes -- the
tail's and the backfill's -- is summed into its local day beside it (`Aggregator.dispatch`), so it
costs no read of its own, and every minute the days that changed are written to SQLite, at
`rollup.dir` (`<cache.dir>/rollup/rollup.sqlite` unless set):

| table | one row per | holds |
|---|---|---|
| `day` | local day | its span and zone, `minutes` it could hold and `held` (minutes with any exit), `records`, the first and last record's time, `complete`, `source` (`ingest` or `files`) and when it was written |
| `day_class` | day, host, class, agent type | `processes` that exited, `cpu_s` and `cpu_n` (the processes that reported a CPU figure: a dropped BPF block writes null, never 0), `submits` |
| `day_user` | day, account, class, agent type | the same per account, so a week's or a month's users are distinct accounts counted over it, never a sum of days |

It is plain SQLite, for a query of your own:
`sqlite3 rollup.sqlite "SELECT date, SUM(processes) FROM day_class WHERE class='agent' GROUP BY date"`.

**A row is a function of the collector's files, never of when the service ran.** The backfill reads
retention again on every start, so the days it reads are recounted from the files, not added to
the table's. A day is written only once the service has read all of it -- from the backfill's start
once the backfill is done; until then the table keeps what an earlier run wrote -- `complete` once it
ended 15 minutes ago (`GRACE_S`), and today as it stands, written again as it grows. A day the
read began inside is written only where the table holds no whole copy of it and fewer of its
records, and never as complete. A record that lands on a day already written (a host's stream
catching up) writes it again. And a recount never replaces a complete day that holds more records
than it read: records only ever add to a day, so fewer means a day-file the startup could not open
(the tail reads it once it can) or day-files cleaned away inside retention, and the table's copy is
the better one (`kept_better` in the response). Days are the service's local days, cut at local
midnight.

**On the page.** A stretch the table holds no record of is a hole in the line, and nothing before
its first day is drawn. Processes and CPU are a mean per day over each point's days with data, so
the week still going sits on the scale of a whole one; users are distinct over the whole point,
and a point that is partial says so in the table below the charts. It is
everyone: neither the window nor the user and agent-type filter narrows it.

**Older days, by hand.** The table starts the first time the service runs with it, from what the
backfill reads (a week). Days still on disk before that are filled with

```bash
uv run python -m rc_dashboard rollup --rollup-days 365     # --force reads again days it holds whole
```

which reads every date dir near a day the table lacks, oldest first, through the service's own
normalizer, and writes each day once the dirs either side of it are read too (a record lands in
the collector's day's dir, which near midnight need not be the service's day). It never writes
today, and writes a day partial, to be read again by the next run, while it is inside its 15
minutes of grace or while a day-file near it could not be read whole. That is a full parse of every exit and submit, in one process: 14 us a record on the demo
feed (118 k records in 1.7 s), so some 40 s a node-day at FAS's 2.7 M exits if its records cost the
same -- not measured there -- and a year of eight nodes a day and more. So it is a command, run once,
not something the service starts by itself.

| setting | env | default | |
|---|---|---|---|
| `rollup.enabled` | `RC_DASH_ROLLUP` | `true` | sum and write the days; off, the Trend tab says so |
| `rollup.dir` | `RC_DASH_ROLLUP_DIR` | `<cache.dir>/rollup` | where the table lives. It is the only copy of a day whose day-files are gone, so a directory outside the cache is safer |

Neither is in what a saved window or hour is keyed on, and a demo writes only its own table.
The table keeps account names (0700, as the other caches); what `/api/trend` serves is counts.

## Commands that failed

**Commands that failed**, on the Agents tab under the tool panels, shows how the Tool mix's processes
ended (the same processes, after its shell dedup), per class and agent type, by tool and by kind of
tool ([AG-10](../docs/figures/README.md#ag-10-commands-that-failed)).

- **A failure is a non-zero exit or a stop by a signal**, except an exit 1 from a program whose 1 is
  an answer (`EXIT_ANSWERS`, which the inspector's "hide typos" shares: the grep family, rg, ag,
  diff, cmp) and SIGPIPE, which `command | head` gets once head has its lines. Both are counted
  apart, as is a record with no wait status. A rate is over the processes whose ending is known.
- **The collector does not fold signals into exit codes**: its `exit_code` is null exactly when a
  signal ended the process. Each row lists its commonest exit codes and signals, and explains the
  conventional ones (127, command not found; SIGKILL, killed outright).
- **Each count opens its commands** through `/api/io/commands` with `outcome=nonzero&outcome=signal`,
  by the same verdict rule (`export.make_predicate`). The lookup reads records before the dedup, so a
  shell that ran a failing command is listed beside it.

## Sessions: tasks, phases, and processes

Every session's row says what it was doing at three levels, which the page calls **Tasks**,
**Phases** and **Processes**: an agent's from the tool calls it made, and a session no agent ran (a
person's login, an editor's terminal) from the commands its terminals ran.

| level | what it is | built by |
|---|---|---|
| Tasks | stretches of the session that were about one thing, each labeled for what it was for | `reducers/phase_tasks.py`, from the names and the agent's own words `reducers/phase_names.py` reads out of each command |
| Phases | what the session spent its time on inside a task: exploring, editing, building, testing, running, Slurm jobs, waiting, git, and idle | `reducers/phase_segment.py`, from the calls `reducers/phase_rules.py` names |
| Processes | every process the session ran, in its tree | `/api/trajectory` (`proctree.py`) |

Every row carries the first two as `phases`: the `list` of phases, the `tasks` above them, a
`headline`, `n_calls`, the session's `category`, `uses` and `summary`, and `driver`, whose calls they
are (`agent` or `terminal`). Those modules document every rule; how the page draws the levels is in
[`web/README.md`](web/README.md#sessions-tasks-phases-and-processes).

**What a call is.** An agent's tool calls are its runtime's direct children, read by Claude Code's
and Codex's own rules, and for any other agent by Codex's; each agent's own chores are housekeeping.
A terminal's calls are the jobs its shell ran, known by each process's group and session (`pgrp`,
`sid`): a process that leads its own group in a session it does not lead, with everything under it,
or a command that leads a session of its own (`ssh host cmd`). The shell itself, what it runs in its
own group (its prompt's `git rev-parse`, a typed `module load`) and a terminal multiplexer are
housekeeping, and a record whose group and session were not kept is counted (`phase_ungrouped`). A
call is recorded when it ends, so a running session whose first call has not ended shows its live
edge alone, which says what the per-minute census shows running, never `no tool calls`.

**What it is read from**: the commands the session ran and the words in them, and nothing else, as
the basis line over the inspector's chart says. Reading and editing inside an agent, its thinking,
and a log entry it writes with its own Write or Edit tool leave no command, so none of them is seen,
nor is what a person does in an editor's window. No model writes a label, and no transcript, prompt
or file on disk is read. The cutting and naming are heuristics over command text, tuned on seven
hand-labeled sessions of boslogin06 and rtx6000a with `rc_dashboard/phase_report.py`, which reads a
day of a feed as a window build does and prints its rows' phases, tasks, categories and summaries.

**Where a task begins.** A task is cut at a gap of `gap_s` or more in which the agent waited on
nothing (a call from an earlier step that is still running has been moved past, and holds no gap
open; the gap names up to three such calls, `scratchpad/linktest still running since 13:05:44`);
where a name new to the session starts to recur (in `new_steps` later steps within `new_window_s`);
and after a commit or pull request that closes a burst of git, when the names before and after it
overlap by less than `close_overlap`. A piece shorter than `min_s`, or with fewer than `min_calls`
calls, merges into its more similar neighbour (never across a cutting gap, and not if it holds the
agent's own words), and each local day is merged down toward `cap` tasks. A few calls hours away from
other work (at most `lone_calls`, within `lone_s`) form a **lone stretch**, which is no task. A row is
cut again each time it is built, so a task holding a running call is open. A session's call log holds
`phases.max_calls` calls; spans past the first dropped call are marked incomplete.

**How a task is labeled**, from one of two sources, and the page shows which:

- **The agent's own words**, when a command in the task holds any: a pull request's title (`gh pr
  create` or `edit` with `--title`), else a commit's subject (`git commit -m`, or `-F -` with a
  heredoc), else the title of an entry the agent writes with `cat`, `tee`, `echo` or `printf` into a
  Markdown file named as a log, plan, notes or the like. Among several, the last pull request or
  commit counts, since it closes the work, and the first log entry, since it usually states the goal.
  Only text in the command counts (`git commit -F msg.txt` has none), masked like every command.
- **Otherwise, names**: a verb for the category holding most of the task's time (`Edit and test`,
  `Run on Slurm`, `Wait on`, ...) and the one or two names most specific to the task, such as a file
  run, tested, written or read, a folder, a Slurm job, a pull request, a branch or a package. Text a
  reader would not recognize (an option's value, a Slurm query's arguments, Claude Code's own scratch
  and project folders) is no name, and a name that every span of the session holds, or that is the
  user's, names no task.

The headline is the project, then up to `headline_names` names that two or more tasks hold:
`KempnerForge · PRs #229, #233 · video_io.py`. The project is the folder the agent was launched in,
unless that is a home folder or named for a person (as `anonymize.py` finds names); then it is the
folder its commands change into most.

**What a plain reading of the commands would get wrong.** A program held in a variable is read as
the program it names (`PY=../venv/bin/python; $PY -m pytest ...`; `$PY` or `$PYTHON` as Python;
nothing else is inferred). The Codex desktop app's reads of its own plugins' icons are housekeeping,
and a `cat` with no recorded argv (schema 7) is the app's if it began within
`reducers.trajectories.phases.app_read_s` (2 s) of one of the app's reads; on boslogin06 on
2026-10-09 that took 230 of the day's 8,434 Codex calls. A call the agent has moved past holds no
idle time however long it runs: on the lane, time goes to what the agent started most recently, a gap
that begins while it runs lists it (`alive`), and once it ends the time it ran past its phase is
reported there (`train.py kept running for 2.7 h`); the last phase ends when the last thing the agent
started does. Each phase says where its own calls ended (`own_t1`) and how long they, or what they
left running, went on past it, by category (`ran_on`). A session across midnight is one block: each
day's row (its session id includes the day) serves the same block, so midnight splits no task or
phase; `days` lists the rows, `sid` on a task or phase names the row that holds its start, and a
row's own `n_calls` counts its day only.

**What kind of session it was** (`reducers/phase_summary.py`). The `category` is the use that holds
half or more of the session's work, as the Users tab counts calls: `explore` (read and search code
and files), `develop` (change files from the shell, and test), `build` (build and install software),
`run` (run programs), `hpc` (Slurm) or `git`; `mixed` when none does, `wait` when the session only
waited, `unknown` when its unrecorded commands hold half of its work plus their own time or it did
nothing else, and `none` with no tool call. The work is the phases' time, less idle gaps, waits and
unrecorded commands, the last phase counted to `own_t1`, weighed by time, not calls, which would let
a Slurm session's polls or one long `python` run lose to the quick reads around them; a wait on Slurm
counts as Slurm work. A call's `ran_on` counts to its own use, so a call hung for 14 h beside a minute
of edits makes a session of running, and the work can be longer than the session. `uses` gives each
use's share, largest first. The `summary` is one to three sentences built from the block's own
numbers, never by a model, such as `Mostly ran jobs on Slurm (82% of 5.1 h of work), then explored
code and files (12%).`, `564 tool calls in 7 tasks, 5 named in the agent's own words; 61 Slurm job
names, 74 scripts run.` and `Waited 4.7 h on Slurm, polling every 2 min.`

On the eight FAS RC login nodes on 2026-11-12, 2026-11-13 and 2026-11-15, 16,630 sessions had an
agent at their root, and 75% of them made no tool call. Of the 4,096 that made one, 33% were
`explore`, 29% `run`, 20% `hpc`, 6% `mixed`, 5% `develop`, 4% `unknown`, 1% `wait`, and under 1%
each `build` and `git`; counting calls instead of time would have changed about a third of their
categories, mostly Slurm sessions and long runs. One known misreading: in 114 sessions on 2026-11-15
a user's Codex desktop app ran only a `mkdir -p` and a `chmod 700` of a folder under `/var/tmp` that
is the user's setup, not Codex's, so they read as `develop`.

The plates over every session in the window do not count categories: a category needs a session's
block, and building a block for every session of a week on eight login nodes takes minutes, where the
plates are read again every few seconds.

**Config.** `reducers.trajectories.tasks` sets the thresholds below (`phase_tasks.DEFAULTS`, which a
test holds equal), `reducers.trajectories.phases` the phases' (`phase_segment.py`), and
`reducers.trajectories.summary` the category's and summary's: `mostly` (0.5), the share a use needs
for the session to be put under it, and `also` (0.1), the share a second use, or waiting, needs to be
named. All three are in the saved builds' fingerprint, so a changed value builds them again, though
only the phase thresholds read as records arrive (`max_calls`, `hk_bin_cap`, `pend_max`, `idle_s`,
`app_read_s`) need it: every task and summary threshold is read when a row is built, and no saved
state holds a task or a category.

| key | default | meaning |
|---|---|---|
| `gap_s` | 3600 | a gap this many seconds long, with nothing the agent waits on, cuts a task |
| `lone_calls` | 8 | the most calls a lone stretch holds |
| `lone_s` | 7200 | the window, in seconds, within which all of a lone stretch's calls must start |
| `new_steps` | 5 | the minimum number of later steps of the stretch a new name must turn up in to begin a task |
| `new_window_s` | 1800 | the seconds after the name first occurs by which the `new_steps`-th of those steps must start |
| `close_overlap` | 0.5 | a closing commit or pull request cuts when the names before and after it overlap by less than this (the share of all their names that appear in both) |
| `close_series_s` | 300 | bursts of git that each start this many seconds or fewer after the end of the one before produce one cut, after the last |
| `min_s` | 600 | a task shorter than this merges into a neighbor |
| `min_calls` | 5 | a task with fewer calls than this merges into a neighbor |
| `cap` | 10 | the number of tasks a local day is merged down toward |
| `headline_names` | 3 | the names the headline lists after the project |

## Agents: subagents and blast radius

The **Agents** tab (`?tab=agents`, or the old `?tab=cascades`) answers "which agents started which
other agents, and where did each one work?". A node is a process whose role (`roles.py`) is `agent`,
`infra` or `client`; tool-call shells, sandboxes, probes and helpers are steps between nodes. A node's
parent is the nearest node above it in the process tree; if there is none, the user's only daemon (for
a `bg-pty-host` orphaned to init, labelled as inferred), and otherwise the tree root. An agent started
beneath another agent, through any number of steps, is its **subagent**. Two outcomes are kept apart:
**nested**, an agent with subagents, and **hosted**, a daemon, Claude Desktop's remote server or
`claude rc` running sessions side by side. A Codex TUI whose direct child is `codex app-server` is
that session's front-end, not a second agent.

`claude daemon` is the root of its own tree. The `claude` process that started it (`--spawned-by`)
is recorded on its node (`spawned_by`) but is never its parent, because every background session the
user starts later, from any terminal, goes through the daemon. A session its `bg-pty-host` runs is a
`terminal` **background session**. A spare it started ahead of time (`claude bg-spare`) that nobody
claimed ran nothing but itself, so it is the daemon's (`infra`), not an agent, and is counted
(`idle_spares`); a claimed spare is the agent it became.

Every tree is sent, up to `reducers.cascades.top_trees` (1000), deepest first; past the cap, the list
and the type strip say how many were left out. A blast radius is served for the
`reducers.workspace.max_agents` (300) busiest agents and for every agent that wrote outside its start
folder; picking another says so.

What it cannot see: Claude Code's Task-tool subagents and workflow agents run inside their parent's
process, so only agents that are processes of their own appear, and an in-process subagent's commands
count as its parent's. On madsys-gpu1 over 2026-10-01..07 that came to 49 agent trees, 4 hosted and
none nested.

**Workspace & blast radius** (`reducers/workspace.py`, composed per agent by the cascades reducer)
shows where each agent worked, relative to its **home**: the agent process's own cwd, or, when that
is `/` (IDE-hosted), its first call's directory below the root, labelled as such. The collector
records no file paths, so three kinds of evidence are kept apart on the page and in the notes Ask
LLM reads (`cascades.workspace.basis`):

| reading | kind | what it is |
|---|---|---|
| ran in | **measured** | processes whose cwd the collector recorded as that directory. It reads cwd at exec for some processes and copies the parent's at fork for the rest, which is before a shell's own `cd`, and before the chdir that Claude Code's Bash tool makes into the directory its last call left it in (on rtx6000a, 1 173 of 1 180 tool-call shells on 2026-10-11 had a copied cwd). Each view says how much was read (`cwd_read_pct`) |
| bytes | **measured, per process** | rchar/wchar (syscall, pipes and terminals included) and read_bytes/write_bytes (disk) of the processes that ran there, credited to where they ran, not to where the bytes went |
| named | **inferred** | distinct paths a command line names, classified by the command: read (`cat`, `grep`, `ls`, a script run, `<`), write (`>`/`>>`, a `cp`/`mv`/`rsync` destination, `tee`, `touch`, `mkdir`, `sed -i`, `tar -x`, `git clone`, `wget -O`, `curl -o`), delete (`rm`, `rmdir`, `git rm`, `find -delete`), and anything else path-like as named. Naming a path is not proof of touching it |

Exact file-level reads and writes would need a collector probe (openat / unlink / rename tracepoints)
that does not exist, and Claude Code's Read, Edit and Write tools and Codex's apply_patch run inside
the agent's own process, leaving no command line at all.

Directories inside the home are relative (`./src`); outside it, `~` is the user's home and `~bob`
another user's. Directories more than `collapse_depth` (4) levels below the home or a top-level root
(`/tmp`, `/scratch`, `~/.cache`, `/n/<fs>`, another user's home) are folded into that ancestor, as are
a directory's quieter subdirectories beyond the first six, and every fold is counted. A **write or
delete named outside the home is flagged**, labelled by where it landed (another user's home and
system paths first, the agent's own `/tmp` scratch last). The toggle **this agent only / with its
subagents** adds every agent beneath it, judged against the outer agent's home. A relative path
resolves against the process's cwd, never against `/` and never by climbing out of a copied cwd with
`..` (`workspace.py` says why). Served paths are masked (`redact`), and no raw argv is served. The
caps on what is kept, and the cost, are in [AG-4](../docs/figures/README.md#ag-4-blast-radius-summary).

## Users: a portrait of each user

The **Users** tab (`?tab=users`, with a portrait opened as `&portrait=<user>`) shows how each person
works with coding agents: which agents they use, how and from where they launch them, what the agents'
commands do, how long a session runs and how many run at once, at what time of day, what they work on,
and what they ask of the scheduler. Each of their sessions opens its process tree under the portrait
(`&traj=<sid>`): the inspector the Agents tab opens beside its list of every session, read the same
way (`/api/trajectory`).

Nothing here is reduced or saved. `portraits.py` composes the tab when it is read, from the reducers a
window view already holds (the trajectories, the sandbox and Slurm) and from the live tier for what is
running now, so it can never disagree with the tabs those reducers fill. It is in neither fingerprint,
and changing it rebuilds nothing.

| part | from | what it says |
|---|---|---|
| launch | trajectories (`surface_of`, `approval_of`), sandbox (`agent_calls`) | agent types; where each session is driven from; approval at launch; whether its tool calls ran sandboxed, as the Security tab counts them |
| rhythm | trajectories (each row's first and last record, each call's start) | sessions, how long each was open, the most open at once, and the minutes in which a call began, by local hour and weekday |
| work | each session's call log; its phase block, for one user's portrait | tool calls by category (`phase_rules.CATS`), counted; time in each phase; the agent's own words in a command (a commit message, a pull request's title, a log entry); the project and the tasks |
| footprint | trajectories | processes, CPU, disk and network bytes, peak memory, summed as a row of the Agents tab's list of sessions sums them |
| scheduler | Slurm (`_sessions`, `_polls`) | jobs, refusals, queue checks and polling loops of the user's agent sessions, folded as the HPC tab folds them |

- **A session is one agent run.** The rows the Agents tab's list of sessions holds for one agent on two
  days are one session here, as its phase block is. Only rooted sessions count, as on that list's plates, so
  a status bar probing `codex --version` every minute is not a session. A session belongs to the
  account its records name first; one whose records name no user and no uid gets no one's row, and is
  counted under the table (`unattributed`).
- **Every agent's calls are counted**: Claude Code's and Codex's by rules of their own, any other
  agent's by the general rule (`portraits.BASIS["calls"]`). A null count reads *not read*, never 0.
- **A share is taken over the known values**, with the rest counted beside it: a Codex app server's
  approval is unobservable, and a session whose own record was not seen has no surface.
- **How they leverage agents** puts the same numbers into words (`portraits.leverage`, by `RULES`):
  how most sessions are driven, the freedom the agents start with, how many run at once, what their
  actions are for (`portraits.USES`), the waiting, the scheduler, what the agents leave behind in
  their own words, what they work on, and when; a sentence under each user in the roster, and point by
  point at the top of their portrait.
- **A figure of each user** draws the same row (`web/src/components/PortraitFigure.tsx`), an element
  per number; the page adds no number but its drawing caps, each noted where it applies.
  `npm run check:figures` checks every fixture user's figure against their row.
- **Traits** are plain statements ("launches with a bypass flag", "keeps several sessions open at
  once"), each one rule over the numbers above (`portraits.RULES`, `portraits.TRAITS`), served with the
  traits and shown on hover with the numbers that met it. No trait is a judgement or a model's, and a
  user may have none.
- **One user's portrait reads the phase blocks** of their newest `PHASE_SESSIONS` (80) sessions, and
  lists at most 60 sessions and 40 tasks, the rest counted. Under a user filter, the portrait is
  compared against all of the window's users when that view is held (`fleet_of`).

Cost: the roster walks every folded session and its call log once, and the sandbox's verdicts walk
every tool call of the window, which is most of the cost. The aggregator holds each view's inputs
(`portraits.Context`), its roster and each open portrait, and rebuilds them once the view has ingested
new records and `reducers.trajectories.live_s` has passed. On 500 synthetic sessions with 100 000
calls, the roster took about 0.3 s, and a portrait read from the roster's inputs about 20 ms.

## Slurm: how sessions use the scheduler

"An agent submitted job N" says nothing about agent behaviour; how agents drive the scheduler does. On
boslogin06's 2026-10-09 agents made 16.6 scheduler queries per job against 0.2 for people, 41 agent
sessions polled the queue for 10 minutes or more (94.9 h of it; one Claude session every 4.9 s for 4.9
h), one session submitted 112 jobs in a minute, and 79% of agent submissions used `--parsable`
(people: 0.2%): a program reading the job id. The **HPC** tab (`?tab=slurm` or `?tab=hpc`; hidden by
`slurm.enabled: false`) shows, from the window's `slurm` reducer (`reducers/slurm.py`; how each part
is computed is in [HPC](../docs/figures/README.md#hpc)):

- **abnormal activity**: every session that broke a rule, each rule the session's own count against a
  threshold in `reducers.slurm.abnormal` (each a count of 1 or more, seconds for the two polling
  bounds, or the service refuses to start):

  | rule | breaks it | setting (default) |
  |---|---|---|
  | polling the queue fast | its longest stretch of polls each at most `poll_gap_s` after the last lasts `poll_min_s` or more | `poll_gap_s` (30 s), `poll_min_s` (1800 s) |
  | a burst of queue checks | the most queue checks in any 60 s | `query_burst` (30) |
  | a burst of submissions | the most jobs submitted in any 60 s (an array is one submission) | `submit_burst` (20) |
  | refused again and again | submissions the scheduler refused | `refused` (3) |
  | cancelling again and again | `scancel` calls | `cancels` (10) |
  | runs failing again and again | `srun`/`salloc` runs that ended non-zero or on a signal | `failed_runs` (5) |

  The polling loop below (polls at most 3 min apart for 10 min or more) is how an agent waits for its
  own job, and flags nothing; fast polling is the alarm. "In any 60 s" is a sliding window, not a
  clock minute. The page words the rules from the thresholds the service serves, and holds none of
  its own;
- **per session**: an agent's collector keys folded into the agent, as the list of sessions folds them;
  people and IDEs one row per user: jobs, refusals, the busiest minute, distinct command lines (numbers
  masked: one script resubmitted is one), the flags it submits with, queries, its longest **polling
  loop** (queries within 2 s are one poll, polls within 3 min one loop, 10 min or more is polling),
  queries per job, cancels, GPU-hours asked;
- **per class**, the same as rates and shares side by side;
- **what came back** from each submission: a job id, a dry run (`--test-only` makes no job), a step
  into an allocation already held (`srun --jobid`/`--overlap`), or a **refusal**: no id, a non-zero
  exit and the scheduler's own error line, which is the reason shown (`QOSMaxSubmitJobPerUserLimit`, a
  partition rule). An srun or salloc is refused only when it cannot get the allocation, not when its
  program fails. The count is a lower bound: a submission keeps only its last 300 characters, so a
  refusal line whose start was cut off (a reason of about 200 characters) is not counted;
- queries and jobs per time bin, by class; a bin with no Slurm record of any class is a gap, not 0.

Requests are read from argv, so a `#SBATCH` line in the script is not seen: a job whose directives
carry the request reports `null` for `partition`, `gpus` and `array` (`req_src` says which), and
GPU-hours count only jobs with GPUs and a time limit on the command line, as the tab says.

## Known collector limitations the page surfaces

- **`peak_rss_mb` is 0 on every exit of older captures**: the kernel clears `task->mm` before the
  `sched_process_exit` tracepoint fires, so a collector that read `mm->hiwater_rss` never got a value
  (it now reads `task->signal->maxrss`). A field that is 0 on every record of a feed renders **not
  measured** rather than as a measured zero (`all_zero`).
- **Through schema 7, the collector's session key split a session per tool call**, every Claude Code
  Bash shell included; schema 8 starts a session only at an agent runtime. For older captures the
  dashboard decides what each tagged process is (`roles.py`) and folds each key into the agent above
  it (`procindex.py`); rows say how many keys they fold and how the least certain was attributed. On
  madsys-gpu1 a 24 h window went from 3 281 keys to 636 sessions.
- **No scheduler census or GPU duty.** Running jobs and GPUs held need a `slurm_jobs` census and
  `dcgm`, which are not feeds here, and the live payload names them under `unavailable`. The HPC tab
  shows the window's latest submissions from eBPF `submit` records instead: per submission and
  attributed to an actor, which is better evidence than a census count.

## Layout

| path | role |
|---|---|
| `rc_dashboard/__main__.py` | the CLI: `serve` (the default), `demo`, `check-feeds`, `print-config` (also as `--check-feeds`, `--print-config`) and `new-password` (`--force` to replace a password, `--dir`, `--netrc-host`; [Password](#password)) |
| `rc_dashboard/config.py` | per-key precedence, candidate probing, `--print-config` |
| `rc_dashboard/feeds.py` | resolve and report every feed |
| `rc_dashboard/feedfile.py` | the collector's day-files: listing them (a plain file before its `.gz`), the time searches (`seek_to_epoch`, `end_at`), where the last complete line ends, the head digest |
| `rc_dashboard/fastjson.py` | the one JSON parser for every line read off the collector's files |
| `rc_dashboard/history.py` | the history store: every sealed day-file kept as Parquet, converted in the background ([History in Parquet](#history-in-parquet)) |
| `rc_dashboard/keysearch.py` | the inspector's key search, its records built in worker processes and replayed with the one-process read's counters |
| `rc_dashboard/replica.py` | a sealed day-file read from its Parquet tables as the file itself: its offsets, time searches and records |
| `rc_dashboard/where.py` | the filters a replica rejects records by before it builds them |
| `rc_dashboard/feedparquet.py` | the Parquet schema, the converter and its verifier ([docs/feed-parquet](../docs/feed-parquet/README.md)) |
| `rc_dashboard/tail.py` | the file tail, and the newest-first read-back that refills retention on every start |
| `rc_dashboard/aggregator.py` | one streaming pass, many reducers: the live feed's dispatch by event type, and the background tasks that feed it |
| `rc_dashboard/roster.py` | the live tier's per-user roster: who ran what, cheap at any number of users |
| `rc_dashboard/gz.py` | a `.jsonl.gz` day-file read in place, seekable by uncompressed offset through a per-process inflater index |
| `rc_dashboard/health.py` | each collector's verdict, from its own meta / heartbeat / stop records |
| `rc_dashboard/clock.py` | the service's present; `clock.as_of` pins (and freezes) it for a captured feed |
| `rc_dashboard/normalize.py` | schema contract, `actor3`, sandbox/approval, surface, session keys, null gating |
| `rc_dashboard/roles.py` | what a classifier-tagged process is: agent, tool-shell, probe, sandbox, infra, ... |
| `rc_dashboard/procindex.py` | per-window index of tagged processes; folds session keys into their agent |
| `rc_dashboard/buckets.py` | rolling per-minute aggregates; exact sub-window slicing |
| `rc_dashboard/rollup.py` | the roll-up table: each local day's sums, fed beside the live tier as records are ingested and written to SQLite as days end; the Trend tab's `/api/trend`, and the `rollup` command that fills older days from the day-files ([Trend](#trend-up-to-a-year)) |
| `rc_dashboard/nodehist.py` | the node tier over time, read on its own thread and binned to a window ([Over the window](#over-the-window)) |
| `rc_dashboard/reducers/` | tools, io, resources, risk, sandbox, trajectories, cascades (agent trees), workspace (where each agent worked), slurm (sessions at the scheduler), facets (the page filters' choices), own_io (each process's own disk bytes), live |
| `rc_dashboard/reducers/phase_*.py` | a session's calls named (`phase_rules`) and logged (`phase_log`), cut into phases (`phase_segment`) and tasks (`phase_tasks`), the names and the agent's own words the tasks are labeled from (`phase_names`), and the session's kind of work and summary (`phase_summary`) |
| `rc_dashboard/tool_buckets.py` | the kinds of tool a command is bucketed into, vendored from the measurement repo's `analyze/tool_bucket_lib.py` |
| `rc_dashboard/phase_report.py` | the report the phase and task thresholds are tuned against |
| `rc_dashboard/portraits.py` | the Users tab: each user's portrait, composed from a view's reducers when read; the traits and their rules |
| `rc_dashboard/windows.py` | window clamp rule, per-window reducer sets, the LRU + build worker |
| `rc_dashboard/hours.py` | hourly merge-reduce, and the node split's worker processes ([Hourly merge-reduce](#hourly-merge-reduce)) |
| `rc_dashboard/snapshots.py` | saved window builds: fingerprint, capture/restore, the 0600 store |
| `rc_dashboard/payloads.py` | each window's panels as last served, saved so a new view can show them at once |
| `rc_dashboard/proctree.py` | one session as a process tree, read on demand: what the session inspector draws |
| `rc_dashboard/iocmds.py` | every command behind an I/O figure, looked up when it is clicked |
| `rc_dashboard/endpoints.py` | the far ends of the network bytes, named and ranked when the panel is read ([Network endpoints](#network-endpoints)) |
| `rc_dashboard/export.py` | the shared filter grammar, the windowed re-scan |
| `rc_dashboard/app.py` | FastAPI: REST, WebSocket, static mount |
| `rc_dashboard/auth.py` | sign-in: one password, kept as a salted scrypt hash ([Password](#password)) |
| `rc_dashboard/guard.py` | who may talk to the service at all: a websocket's Origin, and every request's Host while it has no password |
| `rc_dashboard/redact.py` | credentials masked in command lines before anything is served |
| `rc_dashboard/anonymize.py` | stable aliases for the names in everything the service sends out ([Anonymized names](#anonymized-names)) |
| `rc_dashboard/jsonout.py` | JSON out: serialized with orjson on the request's worker thread, gzipped when the reader takes it; the method notes left out of what the page reads (`strip_basis`) |
| `rc_dashboard/gcwatch.py` | how long Python's garbage collector holds the process, and keeping it short; handing the heap's free pages back to the system (`malloc_trim`) |
| `rc_dashboard/ask.py` | a question about the window on screen, answered by an agent CLI on this host ([Ask LLM](#ask-llm)) |
| `rc_dashboard/demo/` | demo mode: the synthetic feed (`world` plans and builds it, `cast` the people, `sessions`/`agents` what each runs, `host` each node's own records, `records` the records as the collector writes them, `feed` the files), and the overrides that point the service at it |
| `web/` | React + Vite + TS; `dist/` is what the service serves |
| `bench/` | a synthetic feed at fleet density, timers, a profiler, and the guard that the main served answers stayed the same ([bench/README.md](bench/README.md)) |

## Ingestion, precisely

**What the page holds is a function of the collector's files and the clock, never of when this
service was running.** Nothing in the live tier is restored from a previous run (windows resume from
saved builds only when provably a prefix of the same files: *The time window*). On every start the
service hands each day-file to the tail at the end of its last complete line, then reads the history
behind that point back into the live tier: `min(live.backfill_hours, live.retention_min)` of it,
placed by the same binary search a window rebuild uses.

The read-back runs **newest first**, k-way merged across hosts, while the tail is already polling; a
poll that lands before the hand-off reads nothing, so no record is counted twice. The page is live
within a second and fills from now backwards, always contiguously, so the picker's `filled 5h of 7d`
stays true next to `reading history · N%`. The read takes the aggregation lock a small batch at a
time, so neither the page nor the tail waits behind it. A week of one login node's feed (4 GB, 2.4 M
records) takes 35–45 s. Bins are sums and take records in any order; the "latest" state (each host's
plate, the event and submission tails) takes an older record only if nothing newer holds the slot.

The collector (`DailyWriter.write()`, `collector/ebpf_trace.py`) flushes once per poll round, which
is not atomic with respect to a reader, so the tail yields only complete lines and **advances its
stored offset only that far**; trailing bytes are read again next cycle. It never yields a truncated
JSON line and never loses a record, and a line still being written belongs to the tail, never to the
read-back.

**One bad line stops nothing.** Every reader of the day-files counts a line that is JSON but not an
object (`0`, `null`, `[]`) with the lines that do not parse (`fastjson.record`). A record the
normalizer cannot read (a field of a type no collector writes, a list `args` or `event`, a number for
`ts`) is skipped and counted, and so is a record that a held view cannot take. `/api/feeds` reports
them in the eBPF tier's `tail`: `parse_errors`, `malformed`, `feed_errors`.

Offsets are keyed on `(st_dev, st_ino)`, not path, so a recreated file is a new inode rather than a
file silently seeked past its end. A file first seen after startup (a new date directory, a new
host) is read from byte 0, at most 64 MB per poll.
