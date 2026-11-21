# Live agent-behaviour dashboard

A FastAPI service that tails the [`ebpfm`](../collector) collector's JSONL day-files (a sealed day
is also kept as Parquet; see [History in Parquet](#history-in-parquet)), keeps rolling aggregates over a **selectable** window, and
pushes them to a React page over a WebSocket. Every number on the page is measured. A feed with
no data names the path it expected instead of showing a plausible figure, and a metric the
collector could not populate reads **not measured** rather than `0`. For each figure on the page,
[docs/figures](../docs/figures/README.md) explains what it computes, from which records, by which
read path, and at what cost.

This service replaces a static-HTML demo, which has been removed from the tree but can be recovered
from git history under `dashboard/archive/`. That demo generated most of its own numbers (`rc_synth.py`),
and its viewer made up new events between polls. Nothing here does either: `grep -r Math.random rc_dashboard web/src` returns
nothing, and CI can assert that (`npm run check:no-random`).

## Run it

```bash
cd dashboard && uv sync
uv run python -m rc_dashboard --check-feeds     # what resolved, what didn't, and why
cd web && npm ci && npm run build && cd ..      # builds web/dist/, which the service serves
uv run python -m rc_dashboard new-password      # once: prints the hash for dashboard.config.json
uv run python -m rc_dashboard serve --port 8080
```

`serve` only starts behind a password ([Password](#password)). Put the hash that `new-password`
prints into `dashboard.config.json` as `"server": {"auth": {"password_hash": "..."}}`. Without
a hash, `serve` says so and exits, because on a login node every user can reach a port on 127.0.0.1.
`--open` (`RC_DASH_OPEN=1`) serves without a password, for a machine only you can log in to.

Off-cluster, `--check-feeds` exits 1 because the eBPF feed is `required` and absent. That is
intended, so point it at a collector root first.

```bash
RC_DASH_EBPF_ROOTS=/var/log/ebpfm \
RC_DASH_NODE_ROOTS=/var/log/ebpfm-login \
uv run python -m rc_dashboard serve
```

Reach a remote instance over a tunnel: `ssh -L 8080:127.0.0.1:8080 <host>`.

No cluster at hand? [Demo mode](#demo-mode) runs the same service over a synthetic feed it
writes itself: `uv run python -m rc_dashboard demo`.

## Configuration

Every source is configurable. Each key is resolved **separately**, in the order
**CLI flag > `RC_DASH_*` env var > config file > built-in default**. A flag you don't pass
never outranks the file.

- Copy [`dashboard.config.example.json`](dashboard.config.example.json) to
  `dashboard.config.json` (gitignored) and edit. `//`-prefixed keys are comments, stripped on
  load, so the example file is itself a legal config.
- `--print-config` prints the resolved tree with a `_origin` per key, so "why is it reading
  *that*" is never a guess.
- Path defaults are **candidate lists**, probed against the filesystem, and every candidate
  that exists is read. `--print-config` lists the ones that exist (`_existing`);
  `--check-feeds` prints the first, or how many it tried when none does.
- **A mistake stops the service rather than loosening it.** A config file named by `--config`
  or `RC_DASH_CONFIG` must exist. A yes/no setting, as an `RC_DASH_*` variable or as a string
  in the file, is `1`/`0`, `true`/`false`, `yes`/`no` or `on`/`off` in any case; anything else
  (`RC_DASH_ASK=n`) stops it. An unknown key under `server`, `anonymize` or `ask`, or a
  top-level key one slip from one of them (`anonymise`), stops it and names the nearest key
  there is. An unknown key anywhere else, such as an old file's `feeds.ebpf.prefer` from the
  EBR format, is a warning.
- **The feeds since removed are named and ignored, never refused.** Nothing read the
  scheduler's accounting or the domain and user CSVs, so their feeds are gone. A unit, wrapper
  or config file written for them still starts, with a warning for each setting it gives:
  `feeds.sacct`, `feeds.sacctmgr`, `feeds.domains` or `feeds.user_actor` in the file, which is
  dropped from the resolved config; `RC_DASH_SACCT_DIR` and the other variables
  (`config.RETIRED_ENV`); or `--sacct-dir` and `--jobs-csv`.
- `history` (`RC_DASH_HISTORY`, `RC_DASH_HISTORY_DIR`, `_WORKERS`, `_DAYS`) keeps every
  sealed day-file as Parquet too, made in the background, and answers a selective read of one
  from that copy ([History in Parquet](#history-in-parquet)); `RC_DASH_HISTORY=0` reads every
  day-file as JSONL. Switching it changes no answer, and rebuilds no saved window.
- `ingest.workers` (`RC_DASH_INGEST_WORKERS`, 1 by default) and `ingest.reducer_groups`
  (`RC_DASH_INGEST_REDUCER_GROUPS`, 2) reduce the hours in worker processes, each taking one
  node's share of an hour for one group of reducers. 0 workers means one per CPU the service may
  use ([Hourly merge-reduce](#hourly-merge-reduce)). Going from 1 worker to more, or back,
  reduces the hours again from scratch. Neither the count nor the groups change any saved hour.
- `filters.drop_global` (`RC_DASH_DROP_USERS`, comma-separated) drops every record of the
  accounts it names. `filters.drop_agent` (`RC_DASH_DROP_AGENT_USERS`) drops only the records
  classed as agent, for example from the account a site's monitoring runs as. Neither names
  anyone by default. `run.sh` sets the lists for these clusters (not for the demo). The ebpf
  feed's report counts what was dropped (`excluded.users` in `/api/feeds`).

The config is JSON rather than TOML because the rest of the repo uses JSON (every collector record
is a JSON line, and the retired builder's contract was `data.json`), and
`--print-config` has to round-trip it.

## The two feeds

| feed | what it carries | default candidates |
|---|---|---|
| `ebpf` (**required**) | the root tier: per-process exit, residency, tcp/conn, submit | `$EBPFM_LOG_ROOT`, `$EBPFM_OUTPUT_DIR`, `$RC_MEASUREMENT_ROOT/ebpfm/logs`, `log/ebpfm`, `log/ebpfm/logs`, `/var/log/ebpfm`, `~/.ebpfm/ebpf` |
| `ebpf_node` | the unprivileged node tier: load, memory, NFS, per-user cgroup | `$EBPFM_NODE_LOG_ROOT`, `$EBPFM_NODE_OUTPUT_DIR`, `$RC_MEASUREMENT_ROOT/ebpfm/node/logs`, `log/ebpfm_node`, `/var/log/ebpfm-login`, `~/.ebpfm/login`, `log/login` |

A relative candidate is under the repository's root. `/var/log/ebpfm{,-login}` and
`~/.ebpfm/{ebpf,login}` are where the collector writes as root and as a user; as root on a
host with a writable `/scratch` it writes to `/scratch/ebpfm{,-login}`, which is not a
candidate, so name it in `RC_DASH_EBPF_ROOTS` / `RC_DASH_NODE_ROOTS`.

Under a root, a day is a directory of one file per host and stream:
`<YYYY-MM-DD>/<host>.exits.jsonl[.gz]` and `<host>.snapshot.jsonl[.gz]` for the eBPF tier,
`<YYYY-MM-DD>/<host>.jsonl` for the node tier. A `.gz` file is a day the collector has
sealed. The collector never seals the node tier's files.

`log/login` is last but load-bearing: `node_snapshot.py` is a vendored copy of
`collect/common/snapshot.py`, so the login feed that poller writes is schema-compatible, and is
read where the collector's node tier is not deployed.

The readers have a variable of their own, `EBPFM_LOG_ROOT`, first in the list, following
`collect/healthcheck.py`: an operator shell that repoints the collector's `EBPFM_OUTPUT_DIR`
is not meant to repoint the readers too. That variable is still a candidate, though, and every
candidate that exists is read, so one set where the service starts adds a root.
`RC_DASH_EBPF_ROOTS` (`--ebpf-root`) replaces the list.

### History in Parquet

The collector seals a day by gzipping its files once the day is over (`x.jsonl` to
`x.jsonl.gz`). A sealed file never changes again. The service also keeps each sealed file as Parquet
(`history`, on by default; `rc_dashboard/history.py`). That copy holds `feedparquet`'s typed table
for each record kind ([The feed as Parquet tables](#the-feed-as-parquet-tables)), where every row
carries `_off`, the offset of its line in the uncompressed file. It also holds a manifest of the
other facts a reader might need about the file: its size and where its last complete line ends,
its first 4 KB, and the location of each line that is not a record. From these, a replica
(`rc_dashboard/replica.py`) answers every question the readers ask of a day-file, using the
file's own offsets. These are the reads that use a replica:

| read of a sealed day-file | from its replica? |
|---|---|
| a filtered read: a view of some users or agent types, an events query or export by `user`, `host`, `agent_type`, `exit_code`, `signal` or `session_key`, the commands behind a clicked I/O figure, a session's process tree | yes: the filter, as a `where` (`where.py`), is tested on the columns, and only the records that pass it are built |
| a search by time: where a window's read, a resumed read, or a filtered read starts in the file | yes: the same probes as in the file, with nothing inflated (the backfill and an hour's read search the `.gz`, because the read that follows needs its seek index anyway) |
| a read of everything in a range: the backfill, an hour's reduction, an unfiltered window or export | no: orjson turns a line into a record faster than Arrow builds one, so these read the JSONL |

A file is read as JSONL if it has no replica yet, either because it hasn't been converted or
because it is no longer the file its manifest names (by name, size, mtime and inode). The same
happens if the file holds a line that a JSONL reader would read differently: a last line with no
newline, or a line that is JSON but not an object. It also happens if the manifest names a table
that is missing or no longer has the byte size it was written with (for example, the store was
partly cleaned up, or the machine crashed before the data reached the disk); in that case the
file is converted again. If a read still finds a replica broken (say, a table damaged without
its size changing), the replica is dropped. That read continues from the file, at the point
where the replica left off, and the file is read as JSONL until it is converted again. The error
shows up in the store's `errors` (the Feeds tab). Previously, a replica was trusted on its
manifest alone. If its table was deleted, a window's plan skipped the sealed day as if it held
nothing in the window, and if the table was truncated, every build of the window failed. No read
ever waits for the store.

**The same answers.** A replica reads exactly as its file does. It returns the same records in
the same order, with the same keys and types, at the same offsets. A time search makes the same
probes. A filtered read stops at the same record a line-by-line read would, and counts the same
things (an events query's `scanned`, and the session inspector's `bytes`, which its budget is
checked against). So a position, a saved build or an hour's span means the same thing whichever
source a reader took it from, and switching the store on or off, or moving it, rebuilds no saved
window or hour. `tests/test_replica.py` and `tests/test_where.py` check each answer against the
JSONL. `tests/e2e_history.py` serves one feed with the store off and then on, and diffs every API
and every saved state. With `--restart --seal-between`, it also resumes saved builds into a day
that was sealed between the two runs, through the replica on one side and the `.gz` on the other.

**The store.** `history.dir` (`RC_DASH_HISTORY_DIR`; `<cache.dir>/history` unless set) holds one
root for each source root. Like the other caches it is created with mode 0700, because it holds
every user's command lines:

```
<dir>/ebpf/<root basename>-<sha1 of its real path, 10 hex>/
    SOURCE.json                                the source root it mirrors
    <table>/day=<D>/<host>.<stream>.parquet    the records, in file order, with _off
    _manifest/day=<D>/<host>.<stream>.json     the rest of what the file could be asked
```

The store is also a Hive-partitioned dataset, so you can run your own queries on it, for example
`read_parquet('<root>/exit/*/*.parquet', hive_partitioning = true)` in DuckDB. Row groups are
128 Ki rows, so an hour of a busy login node's exits fills one or two.

**Conversion.** In the background, `history.workers` processes (`RC_DASH_HISTORY_WORKERS`) convert
every sealed file that has no current replica, biggest first. The default of 0 means up to 8
processes, as far as the CPUs the service may run on allow. Conversion runs at startup and then
every minute as days are sealed. Each table is flushed to disk before it is renamed into place,
and the manifest, which names the tables, is written last. A replica whose source is gone is
removed. When `history.days` is set, so is any replica older than the newest `history.days`
dated dirs. The node tier is never sealed, so it is never converted.

**What it costs and what it buys**, measured on FAS RC's mirror: 8 login nodes, the 7 sealed days
2026-11-10 to 2026-11-16 (112 day-files, 18.4 GB of `.gz`, 182 M records). Converting them took
12 minutes in 8 processes of up to 0.8 GB each, and produced 6.2 GB of Parquet, 34 % of the size
of the `.gz`. Each read below ran in a fresh process and gave the same answers both ways:

| | from the `.gz` | from the replicas |
|---|---:|---:|
| one user's exits on one sealed day, 500 rows, either order (the week's 128 day-files planned, as `/api/events` plans them) | 222-444 s | 13.6-15.4 s |
| a session's key search through the busiest day-file (17.4 GB inflated) | 30-43 s | 2.8 s |
| a time search in that file: the first, then each after | 12.3 s, then 0.1 s | 0.17 s, then 0.06 ms |

Reading from the `.gz` means inflating every file the plan places, in full, to learn its size and
build its seek index. For the events query that was the whole week, 367 GB. Reads from a replica
build their own index instead, about 30 bytes per line (250 MB for the busiest file). These
indexes are kept for the next read, up to 1 GiB and 64 replicas in total, and the least recently
used is dropped first.

| setting | env | default | |
|---|---|---|---|
| `history.enabled` | `RC_DASH_HISTORY` | `true` | keep and read replicas; `0`/`false`/`no`/`off` reads every day-file as JSONL, and leaves the store on disk |
| `history.dir` | `RC_DASH_HISTORY_DIR` | `<cache.dir>/history` | where the store lives |
| `history.workers` | `RC_DASH_HISTORY_WORKERS` | `0` | conversion processes: 0 is up to 8, as the CPUs the service may run on allow |
| `history.days` | `RC_DASH_HISTORY_DAYS` | `0` | the newest dated dirs kept: 0 is every one the source holds |

The eBPF tier's entry in `/api/feeds` carries `history`: `enabled` and `state` (`off`, `idle`,
`converting`, or `error` with a `note`), `dir`, `workers` and `days`, `source_roots` and
`store_roots`, `replicas` (sealed files with a current replica), `files` and `bytes` (its Parquet
files), `converted` (since the service started), `lookups` (sealed files a reader has asked for
since startup, either answered from a `replica` or read as `jsonl` because there was none), `held`
(the replicas held between reads, and the `bytes` their indexes take), `swept` (half-written files
left by a killed conversion and removed at startup), `read_failures` (reads where a replica failed
since startup, each finished from the file), `progress` (the current round: `todo`, `done`,
`failed`, `records`, `bytes_in`, `elapsed_s`) and the latest `errors`.
`--check-feeds` says how many sealed files have a current replica and how much is left to
convert.

**EBR.** The collector's binary day-files (`.ebr`), which this service read until 2026-11, are
no longer written or read, and a `.ebr` file under a root is skipped. A capture that exists only
as EBR can be read again after converting it to JSONL with the last version of `ebr.py`:
`git show 2f16731:collector/lib/ebr.py > ebr.py && python3 ebr.py cat x.ebr > x.jsonl`.

## Two deployments on this host

| script | port | data | clock | risk profile | Slurm |
|---|---|---|---|---|---|
| `run-dev.sh` | 8086 | this dev cluster's live feed, `/scratch/marthen/ebpfm/{ebpf,login}` (rtx6000a) | wall | `gpu-dev.json` | off |
| `run-fasrc.sh` | 8085 | FAS RC capture, `/scratch/marthen/fas-rc-test-data` (boslogin06, 2026-10-08..09, `.jsonl.gz`) | pinned to the capture's end | `fasrc.json` | on |
| `run-demo.sh` | 8087 | a synthetic feed it writes itself, `.cache/demo/feed` ([Demo mode](#demo-mode)) | wall | built-in rules | on |

All three hot-reload as `./run.sh build`, from the same tree. `run-dev.sh` and `run-fasrc.sh`
serve names as they are (`RC_DASH_ANONYMIZE=0` unless set; `1` aliases every name,
[Anonymized names](#anonymized-names)), to readers who sign in. `run-dev.sh` and
`run-fasrc.sh` (and `run.sh` itself) start only with a password hash in
`dashboard.config.json` ([Password](#password)); without one the backend prints how to make
one and exits. `run-demo.sh` needs none. `site.name` (`RC_DASH_SITE_NAME`) is
shown under the sidebar title and in the browser tab, so a frozen capture and a live feed open
side by side are never mistaken for each other. `slurm.enabled` (`RC_DASH_SLURM=0`) turns the
HPC tab off and skips reducing it, for a cluster without Slurm; the Overview's "asked of Slurm"
rows go with it.

The FAS RC `sacct` export (`/scratch/marthen/agentic-hpc-measurement/raw_data/sacct`, one parquet
file a day, 2026-08-23..09-30) is not read. Those days overlap neither deployment's eBPF data, so
none of the job ids the collector saw can be joined to them.

## Demo mode

```bash
cd dashboard && uv sync && (cd web && npm ci && npm run build)
uv run python -m rc_dashboard demo        # http://127.0.0.1:8080
./run-demo.sh                             # the same with hot reload, on :8087
```

Demo mode is for showing the dashboard where there is no cluster: on a laptop, in a talk, or to
a reviewer. `demo` writes a few days of collector records for fictional login nodes into
`.cache/demo/feed`, as JSONL in the same form a collector writes. It keeps appending to them in
real time and starts the unchanged service over them (`rc_dashboard/demo/`). The tail, the
backfill, every reducer, the saved builds and the websocket all run as they do on a cluster. The
page is not fed from a fixture, so what it shows is what the service makes of the records, and
the Live tab moves.

**Nothing in the demo was measured, and the page says so everywhere.** Every record carries
`"synthetic": true`, a key no collector writes. `health.py` notes each host whose records
carry it, the feeds report lists those hosts (`synthetic_hosts`), and the header shows
*fake data* beside the live pill whenever any host's records carry it. The Feeds tab marks each
such host. The site is named *demo · fake data*, and its description, which the Ask LLM
agent is given, says no record was measured. Because the mark travels with the records, a
service pointed at a demo feed by hand, or an export of a demo feed, still shows it. Use a demo
to show the dashboard, never to report on one.

**What it writes.** Two hosts, `demo-login01` and `-02`, and 28 people: twelve with a specific
role to play (`demo/cast.py`) and a crowd. Their sessions are built from the argv shapes and the
schema-8 rules that the collector and the reducers document, so each tab has the kind of data it
is meant to show:

| tab | what the demo puts there |
|---|---|
| Live, Resources | a working day and a quieter night: VS Code left connected, agents run from cron, an agent watching a job overnight |
| Agents | Claude Code in a terminal, `claude -p` and `codex exec` from scripts and from other agents' tool calls (nested), the desktop app's server and Remote Control hosting sessions, VS Code's Claude extension, the Codex app; writes outside the start folder for the blast radius |
| Sessions | sessions cut into tasks labeled in the agent's own words (commit subjects, a pull request's title, a `PROGRESS.md` entry), phases from explore to an edit-test loop, a Slurm job polled to its end |
| Risks | a year regridded on the login node, `pip`/`conda` installs, builds, `ssh` to compute nodes, `nvidia-smi`, `rm -rf` on shared scratch, credentials on command lines (masked, as schema 8 writes them) |
| HPC | agents submitting with `--parsable` and polling the queue, which the abnormal activity lists; people submitting arrays and checking now and then; refusals with the scheduler's own words |
| Security | Codex calls under `codex-linux-sandbox` (isolated, Landlock-only helpers, sandboxes that failed to start), Claude calls under `bwrap`; sessions launched with a bypass flag and in auto mode; and, checked here, the credentials Risks lists and the writes outside the start folder the Agents tab draws |
| Feeds | per host: a heartbeat a minute, a nightly collector restart with its lost-event count, a kernel limit on one host |

Costs are drawn for each command from ranges that look like a login node's. They make the
panels' shapes recognisable, but they don't model anything.

**What it overrides**, whatever the shell or the config file says -- `run.sh` exports the
cluster's feed roots, and a demo must never read them: both feed roots (the demo's own), the other feeds (a path in the
demo dir that does not exist, so they read *not set up*), `cache.dir` (so no real deployment's
saved builds are evicted), `clock.as_of` (the wall clock), `feeds.ebpf.days`,
`live.retention_min`, `live.backfill_hours` and `live.window_min` (fitted to the history
written), `panels.preprocess` and `panels.preprocess_wait_s` (every window built before the
port opens, so a visitor never lands on a *merging* bar), `site.name`/`site.description`,
`server.rdns.enabled` (off: no DNS is asked about made-up addresses), and `server.auth.open`
(a demo serves its made-up data with no password unless one is set). Each reads `demo` as its origin in `--print-config`.
`--ebpf-root`, `--node-root` and `--as-of` are refused rather than ignored.

| flag | env | default | |
|---|---|---|---|
| `--demo-days` | `RC_DASH_DEMO_DAYS` | 3 | history written before serving, 1..14 |
| `--demo-hosts` | `RC_DASH_DEMO_HOSTS` | 2 | login nodes, 1..6 |
| `--demo-seed` | `RC_DASH_DEMO_SEED` | `demo` | another seed is another set of sessions |
| `--demo-dir` | `RC_DASH_DEMO_DIR` | `.cache/demo` | the feed, the saved builds, the stamp |
| `--demo-fresh` | | | discard the feed and write it again |
| `--demo-write-only` | | | write the history and exit |

**Hosting it.** [`docker/demo/Dockerfile`](../docker/demo/Dockerfile) packages the demo as one
image (`docker run -p 8080:8080`), with two days of history, Ask LLM off, and the people's
working day in New York time. [`deploy/cloudflare`](../deploy/cloudflare/README.md) puts
that image behind a Cloudflare Worker at <https://dashboard-demo.agentic-system.org>: one
shared instance that sleeps when nobody is looking and says it is starting while it wakes.
The real service over live collectors, behind a password, on a host of your own, is one
of [`deploy`](../deploy/README.md)'s two modes -- one server that runs the collector and
the dashboard, or a fleet of collectors that the dashboard's host pulls from with rsync --
or, over a cluster's login nodes, [`hpc/deploy`](../hpc/deploy/README.md).

**Restarts resume the demo.** The feed is a function of the seed, the host count and the clock.
The same span produces the same bytes, however it is cut into windows (`demo/world.py`;
`tests/test_demo.py` checks this). `demo.json` records how far the feed was written. On a
restart (and `run.sh` restarts on every save), the demo truncates anything a stopped round wrote
past that point and continues writing from there, so nothing is written twice and nothing is
missed. The feed starts over if the stamp is from another seed, host count or format (an older
demo's EBR), or is older than the history asked for.
Only one demo writes to a directory at a time (`.demo.lock`), just as only one collector writes a
host. Three days of two hosts is 84 MB of JSONL, written in about 2 s.

## A capture from another cluster

A feed collected elsewhere and copied here differs from a live one in two ways, and the service
handles both without modifying any record. (`run-fasrc.sh` serves boslogin06's capture on
`:8085`, with the same hot reload as `./run.sh build`.)

**It is compressed.** A day-file may be `<host>.<stream>.jsonl.gz`, which is read in place
(`rc_dashboard/gz.py`). Once the history store has converted it, its replica answers the
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
Durations are not measured on that clock: a build's elapsed time, the inspector's read time
and the workers' idle time are timed on `time.monotonic()`, which the pin leaves alone.

### Testing against a capture

The pinned view is deterministic: every restart builds the same windows over the same bytes. That
makes a capture useful as a test, at four levels:

| level | what it checks | state |
|---|---|---|
| the pinned view (`run-fasrc.sh`) | every tab on another cluster's data: its users, its kernel's feature set (4.18: no node tier, no `dstate_task`, no `tcp_accept`) rendering *not measured*, never 0 | running |
| scale | index, backfill and window-build cost at that cluster's volume (boslogin06 writes ~2.7 M exit records a day, about four times rtx6000a) | measured on the instance (`/api/feeds` `compressed`, `/api/windows`) |
| cross-check | a few totals counted straight from the `.gz` by an independent script (exits per class per hour, distinct users, agent sessions), compared against `/api/live` and `/api/panels` at the pinned clock | proposed |
| replay | the live path that the frozen view never runs (the tail following appends, WebSocket pushes, drift re-builds, the build/tail seam), exercised by writing the capture into a plain staging root at N x speed while the service's clock runs from the replay's start | proposed |

With no capture at hand, the bench ([bench/README.md](bench/README.md)) writes a synthetic feed at
fleet density, times the service over it, and compares every response the page reads before and
after a change.

## The feed as Parquet tables

`rc_dashboard/feedparquet.py` writes day-files as Parquet. It writes a typed table for each record
kind (`exit`, `conn`, `netio`, `residency`, `residency_totals`, `submit`, `meta`, `stop`) and one
file per source day-file and table. Every row carries its line's offset, and every record can be
read back exactly. The history store runs it on every sealed day-file ([History in
Parquet](#history-in-parquet)). To run it by hand on a capture of your own:

```bash
uv run python -m rc_dashboard.feedparquet convert --out /path/to/pq ROOT...
uv run python -m rc_dashboard.feedparquet verify  --out /path/to/pq ROOT...
```

[docs/feed-parquet](../docs/feed-parquet/README.md) describes the schema and the choices behind it,
and what it measured on FAS's feed (a third of the size of the sealed `.gz`, with every record of
a week identical).

## Is each collector OK?

The **Feeds** tab starts with one row per host and a verdict an admin can act on. The verdict is
judged only from what the collector wrote (`rc_dashboard/health.py`). The dashboard never
contacts a collector, so a capture copied from another cluster is judged exactly like a live
feed. The collector writes a `meta` record when it starts (pid, kernel, the probes that
loaded and the ones that did not, the sysctls they need), a `residency_totals` heartbeat
every minute, and a `stop` record when it exits cleanly (why, how long it ran, and kernel
events lost versus seen).

| verdict | means |
|---|---|
| OK | a heartbeat within `feeds.ebpf.max_age_s` (5 min) |
| degraded | running, but losing data it could have: a sysctl a probe needs is off, or its last run lost more than 0.1% of kernel events |
| stopped | no recent heartbeat and a stop record after the last one; its reason is shown |
| silent | no recent heartbeat and no stop record: killed, crashed, or the host is down |
| no heartbeat | files for the host, but no heartbeat in what was read |

A probe the kernel cannot load (boslogin06's `tcp_accept`: no BTF on 4.18) is listed as a
**limit** beside the verdict but does not change it, because it holds for every run until the
kernel changes. A running collector only writes its lost-event count when it stops, so the run
in progress shows "reported when it stops", never 0. Below the collectors, the tab shows the data
sources (an optional source that nobody set up reads "not set up", not red), the startup read,
and the resolved configuration.

## The time window

You choose the window on the page, from presets or a custom entry (`90m`, `4h`, `2d`). It is
kept in the query string next to the class filter, so a pasted link restores the span as well as
the view. The parameter is `?win=<minutes>`. If you omit it, the service's own `live.window_min`
applies, which is why a bookmark made before this control existed still opens on 24 h.

**Retention and window are different numbers.** `live.retention_min` (default 7 d) is how much
the process *holds*, and it is the largest span the picker can reach. `live.window_min` (default
24 h) is only the view a fresh page opens on. Raising the view costs nothing. Raising retention
costs memory and a longer backfill.

At ~1.5 KB per exit record, a login fleet produces 10–20 GB/day, so raw events are never kept in
memory. Instead there are four bounded paths:

| path | how a window change is served | storage |
|---|---|---|
| rolling aggregates | **sliced**: exact, instant, no re-read | per-minute buckets, one slot per minute of retention. 7 d ≈ 10 080 slots × a few counters. |
| historical reducers | **rebuilt** from the rows | quantiles, CCDFs, Gini, the dedup index and the trajectory chains carry no time index |
| event stream | cut to the window | a bounded tail of `live.event_tail` (500). It is a tail, **not** an aggregate, so a wider window does not make it longer |
| search / export | `from`/`to` pinned to the window | on-demand re-scan, streamed, never buffered |

Slicing is exact because the bins are fixed-width and aligned to the wall clock. The 1-hour view
is exactly the last 60 rows of the 7-day view, down to the record, and the headline under a chart
is the sum of exactly the bins the chart drew. A window **wider than retention is clamped, and the
page says so**. Drawing bins the process never held would be the same error as imputing a zero.

A rebuild costs as much as the *window*, not the whole *day*. A day-file is appended in time
order, so it is sorted by timestamp, and `export.seek_to_epoch` binary-searches for the first
byte inside the window. (In a sealed file's replica it searches the stored times, with nothing
inflated.) So a 1-hour view of a 15 GB day-file takes ~20 probes and reads one hour of rows.
Files entirely older than the window are skipped without being opened. The rebuild feeds records
**oldest first**. This is required for correctness, not just a preference: the tool-mix
shell-wrapper dedup relies on a child's exit arriving before the exit of the shell that waited on
it, and a trajectory's run-length-encoded chain is appended in place.

A rebuild reports three things instead of assuming them:

- **Progress.** A wide window takes minutes to build, so panels render a *third* kind of nothing,
  with a real percentage. It is neither `none` (which would claim a measurement) nor the empty
  state (which would blame the feed).
- **Drift.** A built view keeps ingesting, so it holds more than its own window. The excess is
  published in seconds and the view re-scans at `panels.max_drift_pct` (25%) of the window —
  proportional, so 1 h refreshes often and cheaply and 7 d rarely and expensively. With the
  hours kept, a view filtered by user or agent type also re-scans once the hour still open as
  it was built has closed, whatever its drift, so its exits' own disk bytes are the hours'
  ("Disk I/O is each process's own").
- **The seam.** A build reads the day-files while the tailer is also advancing through them.
  Rows that land in that seam are buffered and replayed. If the buffer overflows, the count is
  published as `replay_dropped` instead of silently going missing. The seam is drawn per file,
  in bytes. The build records where it stopped reading each day-file, the tail reports where each
  record's line ends, and a live record is skipped only if the build already read that line.
  The seam used to be a timestamp (the newest record the build read, from any host). As a result,
  if a host's stream lagged when the view was built, its catch-up was dropped from the view until
  the next re-scan (6 h for the 24 h view), even though the live tier counted it. A live record
  older than the window's start is left out, just as a scan would leave it out. The status counts
  both cases (`live_skipped`: `already_read`, `before_window`).
  A day-file can be truncated in place (with the same inode), after which the tail reads it again
  from the start. If the build read the file before the truncation, the file drops out of the
  seam and its records are counted as the tail reads them, the same way the live tier counts
  them. The build detects this when the file is now shorter than what the build read, starts
  differently, or (for a plain file) holds different bytes in the 4 KB before where the build
  stopped. If the build read the file after the truncation (which the tail only notices at its
  next poll), the file keeps its seam. The file is read for this check once, outside the lock the readers
  wait on. A `.gz` is checked by its first bytes only, because a sealed day is not truncated in
  place and reading past its head would mean inflating it.

One existing inconsistency had to be fixed for the window control to mean anything. Trajectory
sessions were evicted after `reducers.trajectories.idle_evict_s` (2 h), while the panel's own
`window` block claimed 1440 minutes, so "top trajectories in the window" actually ranked
sessions active in the last two hours. Eviction now never reaches inside the window
(`max(idle_evict_s, window)`). That makes the label true, and it makes
`reducers.trajectories.max_sessions` (150 000) the real memory bound. Consider lowering it if you
raise retention on a busy fleet, because it applies to each held window. Past the cap, whole
sessions are evicted, least recently active first, each judged by its own newest record. Here a
session means its process tree. Schema 7 gives each tool call its own key, so a session that
works all day has hundreds of keys, and a FAS login node produces 45 000 keys on a median day and
up to 105 000. Each held key costs about 6 KB, so the cap comes to about 950 MB per window. A 7 d
view of a busy node reaches it, while a 24 h view of one holds the whole day. (A cap under 50 000
would not have worked with any eviction order: on boslogin06 on 2026-10-28, the sessions active
in the last four hours alone held 52 840 keys. Eviction also used to go key by key and by wall
clock. A window merged from its hours oldest first hit the cap while every session looked idle,
and kept only the afternoon of each long session.)

Built windows are kept in an LRU of `panels.max_windows` (5), and builds are serialised on one
worker. Two concurrent rebuilds would compete for the same page cache and each would make the
other look slow. Ten readers on the same window cost one build, because the WebSocket sends one
frame per *distinct* window rather than one per client. The default window starts building when
the service starts, not on the first page load.

Two things keep a build from costing more than it needs to:

- **Only the records the panels read are parsed.** Each reducer names the event kinds it reads
  (`EVENTS`). A scan skips every other line (`meta` and `stop`, and any kind no reducer knows)
  using a byte match before `json.loads`. The match cannot miss a record, because a JSON
  writer emits `"event": "exit"`, give or take whitespace, and a false hit only costs a
  parse. Filters on a view's users and agent types, on the inspector's session keys, and on the
  user, host and directory of the commands looked up behind a clicked I/O figure (`iocmds.py`)
  are byte checks too. In a sealed file's replica they become a `where` (`where.py`) tested on the
  replica's columns, so a record they reject is never built.
- **Every build is saved, and resumed instead of repeated.** At the end of each scan, the
  reducers are pickled to `<cache.dir>/windows/` together with exactly how far into each day-file
  the scan read: `(st_dev, st_ino)`, a digest of the first 4 KB, and the offset just past the last
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
  config key outside `server`, `anonymize`, `ui`, `ask` and `history`, none of which changes
  what a saved state holds) — changing how a record is counted must never resume a reduction
  counted the old way;
- the same files: every one it read still there, the same inode *and* the same first 4 KB (a
  file deleted and rewritten can get its inode number back), at least as long, and a line ending
  at the saved offset. A day the collector has gzipped since (`x.jsonl` to `x.jsonl.gz`) is the
  same bytes at the same offsets under a new name and inode, so it is read on from its `.gz` (or
  its replica, which answers the same checks), known by its first 4 KB alone;
- built within the window's drift budget. An older one would be re-scanned the moment it caught
  up, so catching it up would only add to the cost;
- for a view filtered by user or agent type, the hour still open when it was built not closed
  since. Such a build joins that hour's exits within itself, over what its filter keeps, and once
  the hour has closed a build gives them the figures the hours worked out over every record
  instead ("Disk I/O is each process's own"); a copy that only reads on from where it stopped
  would keep its own, so that hour is saved with it (`own_open_hour`).

A drift re-build always scans: it exists to move the window's start forward, which an older copy
cannot do. So does a filtered view's once the hour still open as it was built has closed. Pickle
runs code for whoever can write the file, so the directory is created 0700, files 0600, and a
symlink or a file another user owns or can write is never loaded.

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

With `panels.merge_reduce` on (the default; `RC_DASH_MERGE_REDUCE=0` turns it off), each record is
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
disk. A view filtered to users or agent types is still scanned, but its exits take their own
disk bytes from the hours, which its build reduces first when they are not yet (see "Disk I/O
is each process's own"): each hour also saves the own figures of the exits it took a child off
(`o<hour>`), kept by user and agent type so a filtered build reads only its own. On the bench
feed (a day of 8 nodes, 0.9 M records) they are 0.25 MB beside 3.3 MB of hour states, and
working them out adds under 3% to reducing an hour.

On a sealed day, an hour's time search and its read each open the `.gz`, and the next hour's
search lands near where that read ended. Each open used to inflate the same 8 MB span again. Now,
in a run of hours, a reader is handed the span that another reader of the same file inflated last
(`gz.shared_spans`). One span is held for each file an hour reads (every host and stream, on the
two days either side of midnight, and never fewer than 16 files, `hours.shared_files`) for as long
as the run lasts, at 8 to 12 MB each. On 8 nodes that is 32 spans at most, which is what the
hour's own readers (two spans each) hold while it reads anyway. An hour searches every file before
it reads any, so a block that held fewer files than that would lose each file's span to the
searches that follow. Past 64 files (`SHARED_FILES_MAX`, 16 nodes, 768 MB at most), the run opens
no block and shares nothing, rather than holding a block that grows with the fleet. A node-split
worker holds at most 4 (`hours.NODE_SHARED_FILES`), for one node-hour. How much this saves depends
on how an hour of a file compares in size with a span. On a 6-node bench feed
(`bench/amplify.py --days 2 --hosts 3 --host-clones 2 --density 8
--gzip-sealed`), whose files grow 1 to 3 MB an hour, a 24 h window's hours inflated 132 spans
instead of 455, and were reduced in 22.8 s instead of 26.3 s, holding at most 152 MB. A FAS login
node's file grows about 140 MB (17 spans) an hour, and the records between where one hour's read
stops and the next one's starts (twice `SCAN_SLACK_S`, 240 s) cover a span or two, so there it
saves about one span per file per hour.

**Reducing by node, in parallel (`ingest.workers`).** A reducer is pure Python and holds the
GIL, so the service's threads share one core. One pass over an hour of the eight FAS login nodes
(1.45 M records, 2.7 GB of JSONL) takes ~95 s of that core, and decoding the records is under 10%
of that time. The reducers take the rest, whatever the format. With `ingest.workers` at 2 or more
(`RC_DASH_INGEST_WORKERS`), each node's share of an hour is reduced in its own worker process,
with at most that many running at once, and the saved hour is the node states merged in node
order. A value of 0 means one worker per CPU the service may use. The joins stay exact, because a
process, its session, its tree and its wrapper are all on one node. Each node carries its own join
state from hour to hour (a chain holds one per node), and a window installs them as one. A node
runs at most four hours ahead of another, so a quiet node does not wait for a busy one.

**And by reducer group (`ingest.reducer_groups`, 2 by default).** A node's hours are chained, so
each node runs in one process at a time and the busiest node sets the pace. With 2 groups, each
node's hour is reduced in two processes, one for each group of reducers that share no state
(`hours.reducer_groups`). One group is the session reducers and the process index they fold
through (`_procs`, `trajectories`, `workspace`, `sandbox`, `slurm`, `cascades`). The other is the
counting panels (`resources`, `io_process`, `tool_mix`, `risk`, `facets`). No reducer writes into
the records it reads, so each group reads the node's records itself, in order, and the two states
combined equal the node's single-process state. The split changes no number and no saved hour
(`NodeSplitTest`, and every panel of a real hour). The second read costs CPU, so a node is only
split when there are at least as many workers as nodes. Measured on rtx6000b (32 cores, shared
with other users' load) over the eight nodes' real feed of 2026-11-16, with Python 3.14:

| workers (reducer groups) | 1 | 4 | 8 (1) | 8 (2) | 12 (2) | 16 (2) |
|---|---|---|---|---|---|---|
| 15:00, a balanced hour (1.45 M records) | 90 s | 28 s | 22 s | 18 s | **15 s** (6.2x) | 15 s |
| 11:00-14:00, boslogin07 at 2 GB/h (4.94 M) | 367 s | 120 s | 119 s | 111 s | **92 s** (4.0x) | 101 s |

The balanced hour is bound by boslogin06 and the merge into the hour (~4 s an hour in the
service's own process); the spike hours by boslogin07, whose records cost more to read than to
reduce, so its second read buys less. Two workers per node -- one per node and group -- is the
most that helps; each is ~0.5 GB (16 of them ~7.4 GB, plus ~1.7 GB in the service while it
merges), and they count against the service's memory cap. The FAS deployment uses that
(`INGEST_WORKERS` in `hpc/deploy/site.env`, empty for two per node). The table was measured
with one pool that every worker took any unit's next hour from; with a worker for every unit, as
in the 16 (2) column, each unit now keeps to its own (`hours.UnitPool`, below), which this feed
has not been measured with.

**Python 3.14.** The same reductions run 5-10% faster on CPython 3.14 than on 3.12, and every
panel is identical (compared field by field on a real slice). The test suite passes on both.
CPython's experimental JIT (`PYTHON_JIT=1`) was slower here, and so was PyPy 3.11, whose panels
also differ in float rounding. The free-threaded build (3.14t) does run one node's groups as
threads in parallel with exact results, but each thread is ~18% slower and orjson has no
free-threaded build, so the dashboard uses the processes described above.

Merging two nodes' states for the same hour can give a different result from one pass only where
`merge` takes a value from one state instead of adding the two. That covers a "newest record's"
field (a user's class in the I/O panel), a home directory learned from one node's argv (which no
longer places another node's paths), the rows that tie at a cap (the 200 Slurm sessions), and
float sums added in a different order (the last digit). The "first N" samples (the risk panel's
instances, a Slurm rejection's example) keep the earliest in time. Counts, sums, sets and the
quantile sketches are the same, and so is every panel for any worker count from 2 up
(`tests/test_hours.py`, `NodeSplitTest`). The join caps (`max_parents` and the like) apply per
node, so fewer pending joins are evicted. A window treats the nodes' join states as if they came
from one pass, because its tail and live feed are one pass over every node
(`hours.combine_carries`). Each capped map is interleaved by the hour in which each key was last
touched, keeping each node's own order, and then trimmed to one pass's cap by dropping the
oldest. The split is part of the hours' fingerprint but the count is not, so going from 1 worker
to more, or back, reduces the hours again, while changing the count does not. The workers are
spawned once and kept (`hours.status` reports `node_workers`, `node_cpu_by` per node, and
`node_merge_s`), and they count against the service's memory cap. With a worker for every unit
(the fleet's two per node), each unit's hours are always reduced in the same worker
(`hours.UnitPool`). So a worker opens only its own node's files, and builds and holds the gzip
index only for its own node's sealed files. With fewer workers than units, the workers share one
pool, and each ends up indexing every node's files. (On the bench feed above, 12 workers for its
12 units used to build 280 indexes and now build 48, and spent 44 s of CPU on a 24 h window's
hours where they now spend 26 s.) A worker runs the service's own code and Python, or nothing
at all. A spawned process starts from `sys.executable` and the service's `sys.path` as they are
at spawn time. So a deploy since the service started (a new release in the location the service
imports from, a recreated venv, an upgraded package) would make the worker reduce with different
code (`hours.spawn_identity`). The service checks for this before it starts a pool, and each
worker checks when it starts. After such a deploy, the nodes are reduced in the service's own
process, on one core and into the same hours, until the service restarts (`node_note` says so).
The fleet's install restarts the dashboard whenever it recreates the venv.

**Preprocessing everything.** By default, only the windows a reader asks for (and the cheap
`panels.prewarm_min` ones) are built, and only the hours inside them are reduced. So the first
switch to a wide window still reduces and merges while the page says *merging*. With
`panels.preprocess` on (`RC_DASH_PREPROCESS=1`), the service instead reduces every closed hour in
retention at start-up and merges its whole days, then builds **every** preset window and holds
them all (`max_windows` is raised to fit). With `panels.preprocess_wait_s` (`RC_DASH_PREPROCESS_WAIT_S`)
above 0, it does all this before it opens its port (uvicorn only binds once start-up returns), so
the first page finds every window ready. If that deadline passes, it starts listening anyway and
the builds continue. Preprocessing is off by default because a held 7 d window on a busy node can
take up to ~1 GB. The demo turns both settings on.

## API

| endpoint | purpose |
|---|---|
| `GET /api/config` | resolved config, `_origin` per key |
| `GET /api/feeds` | per feed: configured vs resolved paths, status, lag, rows, notice, backfill; for the eBPF tier, `history`: the store of its sealed days as Parquet, how far it has converted them and how many reads it answered ([History in Parquet](#history-in-parquet)) |
| `GET /api/collectors` | per host, whether its collector is OK: verdict and reasons, heartbeat age, the run in progress, every run and how it ended (events lost), probes that could not load (`health.py`) |
| `GET /api/windows` | offered windows, the retention ceiling, and every held window's build state |
| `GET /api/panels?window_min=&user=&agent_type=` | historical aggregates for that window + its build state; `user` / `agent_type` (repeatable, `none` = untagged) narrow it to a view of its own, built and saved like a window; `filter` names it, `facets` lists the window's users and agent types |
| `GET /api/live?window_min=` | rolling window: per-minute bins, plates, hosts, event tail, submits |
| `GET /api/live/users?window_min=&q=&limit=&running=` | the user roster past the busiest rows the live frame carries; `q` searches it (the aliases, when names are anonymized), `running=agent:<type>` keeps the accounts with such a session up |
| `GET /api/live/user?name=&window_min=&n=` | one account opened up: the sessions it has running and its last `n` (25) exited processes, however long ago |
| `GET /api/trajectories?rank=&window_min=&user=&agent_type=` | ranked sessions in that window, narrowed the same way; an agent's row carries its tasks and phases ([below](#sessions-tasks-phases-and-processes)) |
| `GET /api/trajectory?sid=&window_min=&from=&to=&order=` | one session as a **process tree**, re-read on demand, `oldest` or `newest` first (below) |
| `GET /api/annotate` | the model that labels tool calls with their purpose: provider, model, endpoint, whether it can run here ([below](#tool-call-purposes-from-a-model)) |
| `POST /api/annotate` `{sid, window_min, from, to, order, run}` | one session's tool calls and the purposes already known; `run` starts a job asking the model about the rest |
| `GET /api/annotate/{job}` | that job's progress and its answers so far |
| `GET /api/users?window_min=&user=&agent_type=` | every user with an agent session in the window, one row each, the window's totals (`fleet`), the agent sessions of no named user (`unattributed`), and the traits' rules ([below](#users-a-portrait-of-each-user)) |
| `GET /api/user?name=&window_min=` | one user's portrait: their row read against the window's users, rhythm by hour, calls and time by phase, projects and tasks, the scheduler, and their sessions; `name` takes the alias when names are anonymized |
| `GET /api/events?<filters>` | paged event query |
| `GET /api/export?<filters>` | **streaming filtered raw JSONL** |
| `GET /api/io/commands?<filters>&window_min=&apid=&host=&cwd=` | starts a lookup of every command behind an I/O figure, over the window's records (`iocmds.py`); `apid` and `host` name the agents as the page drew them, `cwd` a directory |
| `GET /api/io/commands/{job}?offset=&limit=&sort=&dir=` | that lookup's state and, once it is done, a page of its rows (200, by `rd_mb`, largest first, unless asked otherwise) |
| `WS /ws` | push: `live`, `feeds`, `rebuilt`, `backfill`, `windows`; accepts `{"type":"window","minutes":N}`. Each reader has a sender of its own holding the newest frame of each kind, so a reader that stops reading holds up neither the tail nor the other readers (one did, for as long as it stayed); one that takes no frame for 5 s is dropped (close 1008), and past 64 readers one is turned away (close 1013: the page retries, then polls) |
| `GET /api/health` | `ok` false, and `why`, when the service is not doing its work: the tail has not moved -- no poll ended, no batch handed over -- for 5 poll intervals (5 s at least); a background loop failed its last 3 rounds in a row (the ingest loop's 3 polls, the windows loop's 6 s, the feeds loop's 90 s, the rebuild loop's 3 rebuild intervals); or a view could not take a record in 3 of the tail's batches in a row. One error alone flips nothing: a round that goes through, or a batch every view takes, ends the streak. It said `ok` with a `last_poll` two minutes old, and whatever the loops and the views met. `ingest` (`age_s`, `stale_after_s`, `feed_errors`, `feed_error_streak`, `feed_error`), each loop's last error, how many and its `streak` (`loops`), the readers (`websocket`: `readers`, `dropped`, `refused`, `last_drop`), the backfill, the windows, the memos. A loop's error and a view's are also logged to stderr, when they change and once a minute while they repeat |
| `GET /login`, `POST /login` | the sign-in form, and signing in: a session cookie, then back to `next` ([Password](#password)) |
| `POST /logout` | signing out: the cookie cleared, then the form |

With a password set, every route above asks for it ([Password](#password)). `POST /api/ask`,
`GET /api/ask`, `GET /api/ask/{job}` and `/api/suggestions/done` (`GET`, `POST`, and `DELETE
/api/suggestions/done/{id}`) are [Ask LLM](#ask-llm)'s; every route under `/api` that is not a
GET or HEAD takes a JSON body only (a 415 otherwise).

`window_min` is optional everywhere. Omitting it means `live.window_min`, never an error, so a
script or bookmark that predates the control keeps the view it had. A value above retention is
clamped, and the response carries `requested`, `clamped` and a `note` explaining why.

`/api/trajectory` (`rc_dashboard/proctree.py`) is the inspector behind a clicked trajectory row.
No reducer holds per-process rows (one day's busiest session on rtx6000a ran 1.59 M processes),
so the inspector re-reads the window's day-files for exactly the records the row folds (their
`_sid`). It uses the same normaliser scan as a window build, with a byte pre-check on
`session_key`, so the rest of the file is skipped before parsing. (In a sealed file's replica, the
pre-check is a test of its `session_key` and `agent_pid` columns before a record is built.)
Parents come from each record's own `ppid`. A parent placed on weaker evidence (the process index,
the collector's `depth`, the fold) says so in `link`, and a parent with no exit record in the read
is drawn without one. Consecutive siblings of the same shape fold into counted groups. Every bound
is reported in the response, never applied silently. The read stops at
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
`from to event[] class[] agent_type[] user[] host[] tool[] purpose[] bucket[] session_key
sandbox approval depth_min depth_max cpu_min duration_min rd_min wr_min exit_code signal
outcome[] q`. `event` is `exit` unless one is given (an empty `event=` widens it to every kind,
and `/api/events` says `event_filter_defaulted` when it applied the default); `rd_min` and
`wr_min` are MB read and written, at least. `outcome` is
how a process ended, in the words of [Commands that failed](#commands-that-failed): `ok`,
`nonzero`, `answer`, `signal`, `pipe` or `none`.

The export's first line is a `_meta` header (the filters, `from` and `to` among them, the feed paths and their status, schema versions) and its last is a `_trailer` (row count, whether the cap truncated it).
Body rows are the
collector's own fields plus added `_`-prefixed derived keys. The original fields are unmodified,
except for credentials.

**Credentials never leave the service.** Tokens, passwords, keys and signed URLs on a command
line are replaced by `[REDACTED]` on every way out of the service (built panels, the live frame,
`/api/events`, `/api/export`, and the `q` search, which matches the masked text). The flag or
variable name is kept, so a reader can see what was there. Records stay raw in memory, because
the secrets, sandbox and approval detectors need to read them. The rules and their measured cost
are in `rc_dashboard/redact.py`, and they match the collector's word for word. They are a
deny-list, so a secret in a shape the rules do not know is shown as it is (`collector/README.md`
lists the shapes). Each rule reads a text in linear time. A text longer than 100,000 characters
(the same limit up to which the phase rules read a command) is cut there and marked `[CUT]`.

## Conventions the panels obey

These rules protect the integrity of the research. Each one exists because this project has
already made that mistake once.

- **Three classes, never two:** `agent` / `human` / `ide` (+ `unlabeled`). `ide` is any
  editor a person drives. That covers the VS Code, Cursor and Windsurf remote-server trees the
  collector tags (it calls the first `human-vscode` and counts the other two as agents), plus
  other editors' servers, recognised by their own executable path (`normalize.ide_class`). An
  agent CLI started in an IDE terminal, or by an IDE's extension, keeps its own agent type and
  stays `agent`, because its role is read from that type and not from the IDE's directory in its
  path (`roles.py`). Folding the IDE
  class into "agent" is what produced a GPU near-parity claim that was later retracted. `actor3`
  is *derived* via `agent_lib.actor3`. It is never approximated from the binary `actor`, which
  would silently empty the IDE class.
- **Count and cost, side by side:** every categorical panel ships an event-weighted chart and a
  CPU-weighted chart, each on its own single axis. Never a dual axis. The reason is easy to see:
  `poll/probe` is ~28% of an agent's tool calls and ~0% of its CPU.
- **Null is not zero:** a dropped BPF block emits `null`. Nullable fields carry
  `coverage_pct` and are never imputed. A field that is present but always exactly zero is
  flagged `all_zero` and renders as **not measured**.
- **Zero is not missing:** a feed that is present but has no matching rows says `none`, while an
  absent feed names its configured path. These are different states and render differently.
- **`sandbox` and `approval` are independent axes.** `--dangerously-skip-permissions` bypasses
  approval but leaves the sandbox intact. `--dangerously-bypass-approvals-and-sandbox`
  disables both.
- **`sandbox` is namespace isolation, and it is labelled that way.** The collector compares
  `/proc/<pid>/ns/*` with pid 1's. Landlock and seccomp-only sandboxes are invisible to this
  check, and a container, an apptainer user namespace or a `PrivateTmp=` unit counts as
  sandboxed. The key `sandboxed` is shown as "sandboxed", and its glossary entry explains how to
  read it. It has **four** states. `denied`,
  shown as "exited before it was checked", means the namespace links were already gone when read
  (the ebpf tier runs as root, and every such record on rtx6000a lived under 6 ms). It is never
  folded into `unsandboxed`. Every share is taken over the KNOWN verdicts, with coverage shown
  beside it.
- **A session is judged by its tool calls.** Claude Code and Codex wrap each tool call but never
  themselves, so the session root's own verdict says nothing about confinement. A tool call is
  either a sandbox wrapper invocation (bwrap / codex-linux-sandbox, with nested ones folded into
  the outermost) or a Claude Code Bash shell that no wrapper encloses. Its outcome is `isolated`,
  `not_isolated`, `failed` (the wrapper exited non-zero and no payload ran, meaning the sandbox
  was tried but did not come up; this was true of all 612 codex-linux-sandbox runs on rtx6000a),
  `no_payload` or `unknown`. A wrapper's own record is read before it unshares, so it always reads
  "not isolated", and it is not a "capability probe". What ran under the wrapper decides the call.
- **Approval is read from the session root's own argv, and claims no more than that.** Only a
  process whose role is `agent` (and not an IDE's) carries a verdict. The verdict comes from exit
  records and from the process's residency / conn / netio identity blocks, because a running
  Claude launched from VS Code is only ever seen in its own `conn` records. States: `bypassed` (a
  bypass flag at launch), `auto` (`--permission-mode auto`), `supervised`, shown as "no bypass
  flag at launch" (settings files, codex `config.toml` and a mid-session switch are invisible),
  `unobservable` (`codex app-server` takes its policy per thread), `na` (a tool shell, probe,
  IDE helper or human process) and `unknown` (a child; its verdict is on the root).
- **Where a session is driven from is read from the same argv, and `unknown` is never
  guessed.** `normalize.surface_of` gives every agent session root one of five surfaces in
  two groups. CLI: `terminal` (the agent's own interactive UI, including `claude daemon`
  background sessions) and `script` (`claude -p`, the Agent SDK's stream-json, `codex exec`,
  an MCP server). App: `desktop` (`~/.claude/remote/ccd-cli/`, the Claude desktop app's Code
  tab over SSH; `codex -c features.code_mode_host=true app-server --listen unix://`, the
  Codex app's Remote SSH), `extension` (an IDE extension's bundled binary under
  `.vscode-server/extensions/` and similar paths) and `web` (Remote Control: `--sdk-url`,
  `app-server --remote-control`). The mode is checked before the path. The extension only runs
  its binary as Claude's stream-json, as Codex's `app-server`, or with no mode and no terminal,
  so a `codex exec` of the extension's binary run by hand from a terminal is a `script`. A Codex
  `--managed-daemon` app-server serves whichever
  client attaches, so its argv names no surface. It takes the surface of its terminal UI when
  that UI started it, the same way it takes its approval (`cascades.py`), and is otherwise
  `unknown`. A host (the desktop app's `~/.claude/remote/srv/` server, `claude rc`,
  `claude daemon`) is never a session, but a row or box keyed on a host says where its sessions
  are driven from. The surface is shown as a chip on agent boxes, session rows and live sessions,
  and as the terminal-or-script / app split on the agent sessions plate over the Agents tab's list of
  sessions. There is no filter on it
  yet.
- **CPU is `sum(cpu_s)`, self time only, counted once per process.** `child_cpu_s` is never
  added. `analyze/common.py:364` measures that the naive sum inflates login CPU 1.83× and does so
  *unevenly by class*, which would invert an agent-vs-human comparison. `schedstat_ratio` is
  emitted so that a regression in that basis is visible on the page.
- **A process is counted once, at its exit.** At each stop, the collector writes a `truncated`
  record for every tracked process that is still running, with its CPU and I/O over its whole
  life so far. That process's exit record after the restart carries all of it again. So every
  count and sum uses `exit` records only. A `truncated` record still identifies a running agent
  root (its launch, its approval), and the session inspector draws it as alive at the
  collector's stop, but it adds to no total.
- **Disk I/O is each process's own.** The collector's `io` is `task->signal->ioac`, which the
  kernel also credits with the I/O of every child the process reaped. So a shell's figure includes
  its commands' I/O, and an agent's figure includes the I/O of every one of its tool calls. Every
  figure that sums disk or syscall bytes (the
  I/O panel, the tool mix, the I/O series, the blast radius, the sessions, the live roster and its
  rows, the inspector's subtree totals, the commands behind a number -- takes each process's
  figure less what its exited children carried (`reducers/own_io.py`: one join per reducer,
  carried across hours like the other joins; the live tier's backfill, which reads newest first,
  takes each child off the parent it met first). So a command's bytes count once, not once per
  process above it. A join can take off only a child whose exit it read, and a filtered view
  never reads what its filter leaves out: a `claude` that read 20 MB itself, under which a
  `codex` it started read 100 MB, was 20 MB unfiltered and 120 MB under
  `agent_type=claude_code`. So the figure is worked out once, by the one pass that reads every
  record: the hours (`hours.py`, "Hourly merge-reduce") put each exit's own figure on it before
  their reducers read it, with one more join (`Ledger`), and save the parents' beside each hour,
  kept by the process's user and agent type. A filtered view's build gives every exit of a
  closed hour in its window that figure, from a day-file or a sealed day's replica alike,
  loading only the figures of the users and agent types it keeps; a window's first part-hour
  does too, so a child reaped before the window began is taken off as in the hours after it.
  Then a process reads the same in any view. A child is still not taken off where no such pass
  has read it: in the hour still open and in what a filtered view is fed live, the view takes
  off only the children it read, until that hour has closed (its status says from which hour,
  `cache.own_io`). A held filtered view is then built again at the next rebuild tick
  (`rebuild.interval_s`, 15 minutes), whatever its drift: about one filtered scan an hour for
  each filtered view held, where drift alone would build a 24 h view again every 6 h and a 7 d
  one every 42 h (on the bench's six-node feed, `bench/README.md`, a 24 h filtered view is
  built again in about 3 to 7 s, a 48 h one in 4 to 16 s). And a build of it saved before that hour
  closed is not resumed after: a restart reads the window again (`cache.note` says why). With
  no hours kept (`panels.merge_reduce` off) every view joins what it reads, as before; and the
  inspector and the commands behind a number join what their own reads parse, as before. Nor is
  one its parent cannot have reaped: a child that outlives its
  parent still names it, and a later process may get that pid, so a process takes off only its
  own user's children (root aside) that began no more than a minute before it did (its recorded
  start is its last exec); the rest were counted at their own exits and are dropped, counted.
  The record has no fork time apart from that exec, so a launcher that forks a child and execs
  over a minute later is refused it: that child's bytes count twice, and the refusal is counted
  in `rejected` (on four million records, one child in 2.7 M began a minute to an hour before
  its parent's recorded start). Nor has the record a reaping event: a child that ended before
  its parent but was never waited for by it (the parent never calls wait(), or ignores SIGCHLD)
  is not in the parent's figure. A parent that reaped none at all says so -- its `child_cpu_s`,
  the CPU the kernel credits a reaper with in the same step as the bytes, is 0 -- and takes none
  off, counted apart (`unreaped`); one that reaped some of its children and not others still has
  them all taken off, and comes out short by the rest. A figure short of what its children
  carried shows as 0, never below, and is counted too. Each join holds at most
  `reducers.own_io.max_parents` parents (2000; below 1 the service refuses to start), the one
  given to least recently dropped first,
  counted (`evicted`) and said on stderr (the first time, then at most hourly a panel in each
  process, so each of the hours' workers says it too), its children's bytes then counted again
  at its exit. What is held for an agent runtime, or for the process that started one, goes
  only after every other parent, unless such parents hold over half the cap: an agent sits idle
  as long as its reader does while other parents reap, and its tool calls' bytes are the most a
  parent holds, but a login shell the feed never sees exit would otherwise crowd the rest out.
  A pid whose children's groups pass PER_PID (eight) with no two of one user to merge drops the
  earliest begun, counted apart (`crowded`): the cap does not bear on it. A service holds some
  36 joins (five in each held window, six carried in the hours' chain, the live tier's two, an
  inspector read's and a lookup's): at one entry a parent (~0.5 KB) 2000 is ~40 MB in all, at
  the demo's 2.8 ~71 MB, at worst (eight, ~2.5 KB) 164 MB; the live tier's notes add at most
  ~10 MB. The repo's streams hold under 500 parents at once, and the bench's feed, 32 times the
  demo's records, drops none at 2000. A busy login node may need more -- over one real day an
  uncapped join of agent children alone ended holding 177,411 parents, most of them stale
  (`reducers/phase_log.py`): raise it while `evicted` grows, at 19 to 80 KB for each one added.
- **"Top" is seven rankings, not one** — `events`, `cpu_s`, `peak_rss_mb`, `io_mb`, `net_mb`,
  `distinct_tools` and `recent`, each with `dominant_user_share_pct`, because a single ranking
  is dominated by one poll loop and one mega-user. `recent` lists real sessions only, because
  otherwise a status-bar probe, or a tool call whose shell has not exited yet, would hold the
  top spot. The list is re-ranked from the live feed when read (`reducers.trajectories.live_s`,
  5 s), and the page re-reads it at that interval while it is on screen.
- **Scope is stated:** the eBPF tier runs on login nodes, so its resource figures describe
  login-node use, not cluster use.

## Risk rules differ by cluster

What counts as risky is up to each site. Running nvidia-smi is pointless on a login node with no
GPU and sensible on a GPU dev box, compute nodes are named differently everywhere, and each site
has its own tools and token formats. The rules are built in (`reducers/risk.py`, which documents
every key) and can be changed per cluster in the config's `risk` section, or in a **profile**
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

`{{name}}` in a pattern refers to a `vars` entry. A changed pattern, an added rule, or a pattern
changed by a variable runs without the built-in rule's pre-check (a cheap literal test that, if
stale, would cause missed matches). A rule that does not compile is left out and named on the Risk
tab; it is never fatal. A secret pattern that a cluster adds is also **masked** everywhere the page
shows a command line (`redact.extend`), so a credential the panel counts is never shown elsewhere.
The profile's content is part of the resolved config, so changing a rule re-scans the saved
windows instead of resuming counts made under the old rule. The Risk tab's **Rules in effect**
lists every rule, its pattern, and whether it is built in, changed, added or switched off.
The tab itself is modular. `risk.sections` lists its sections in order (compute on a login
node, groups of command rules, credentials), so a cluster can drop the ones it has no use for (an
unlisted section is not computed) or split rules into sections of its own.
Two profiles are shipped: `risk-profiles/fasrc.json` (FASRC login nodes; `run-fasrc.sh` uses it)
and `gpu-dev.json` (a GPU box that is also the login node). **[`risk-profiles/README.md`](risk-profiles/README.md)**
documents every key and every built-in rule, and [`example.json`](risk-profiles/example.json) uses
every key, with annotations.

## Anonymized names

`anonymize.enabled` (`RC_DASH_ANONYMIZE=1`) replaces every username, lab or Slurm account name,
email address, and numeric user ID in everything the service sends out (every HTTP response,
websocket frame and export line, and the Ask-LLM snapshot) with a stable alias: `user-7f3a9c1e`,
`lab-1b2c3d4e`, `[email]`, and `[uid]`. It is off by default, so a deployment for the cluster's
own staff shows real names. `run-dev.sh` and `run-fasrc.sh` leave it off (user, 2026-10-13), and
the tunnel only serves their pages to readers who sign in ([Password](#password)). The header says
*anonymized* while it is on.

- **Stable, not reversible.** An alias is 8 hex characters of an HMAC keyed with a secret salt:
  `anonymize.salt`, or otherwise `<cache.dir>/anonymize.salt`, generated once with mode 0600. An
  account has the same alias in every tab and window and across restarts. Without the salt, a list
  of candidate names cannot be used to reverse an alias, and neither can sending those names to
  the service (**Requests**, below). Changing the salt reshuffles every alias.
  Neither `/api/config` nor `--print-config` shows the salt. A configured salt shorter than 16
  bytes stops the service at startup instead of being used, and a longer one gives every name
  a new alias. The salt file is written in full and then linked into place, so a crash cannot
  leave it half-written. (On a file system without hard links, it is created in place, so a
  crash can leave it half-written there. Also, if two instances start there at the same moment
  for the first time, one of them can stop; when restarted, it reads the other's salt.) A salt
  file that is empty, not hex, or too short (because it was edited by hand, a crash happened
  there, or an older version wrote it) also stops the service. Delete it, and the next start
  creates a new salt, with new aliases.
- **What counts as a name.**
  - Every account the live roster holds or a `user` field names, plus the accounts named in the
    config's filters.
  - Any segment of a home or lab path, masked in place even for someone who never ran
    anything here: `/n/home08/<name>/`, `/n/holylabs/<lab>/Lab/<name>/`, Claude Code's
    `-n-home08-<name>-` spelling of a folder, and `~<name>/`, which is how the Agents tab's
    workspace writes another user's home (`anonymize.TILDE_HOME`, applied regardless of the home
    patterns).
  - Labs, from a Slurm `--account` or `-A`, from `<x>_lab`, and from lab paths.

  Names match as whole words. A dotted name also matches in its hyphenated spelling, and a name
  clipped off the end of a long command line matches by its prefix. The patterns are
  `anonymize.home_patterns` and `anonymize.lab_patterns`; the defaults
  (`anonymize.HOME_PATTERNS`) know FAS's layout. System accounts (uid below 1000),
  `anonymize.keep` (`root`), the class names, and the agent types are never aliased.
- **Where.** At the exits, after the reducers, like `redact`: the default response class, plus
  the websocket frames, the export stream, and the gzipped trajectory body. Reducers and saved
  state keep raw names, and `anonymize` is left out of both saved-state fingerprints, so
  switching it on or off needs a restart, never a rebuild. If anonymizing fails, the response is
  a 500 with no body, never the raw payload.
- **Requests.** The page sends aliases, and a real name sent in their place finds nothing, so the
  service cannot be used to ask whether someone is here, or which alias is theirs.
  - A `user` filter, the user drill-down (`name`), and a session id (`sid`, whose `tty:` and
    `usr:` keys name an account) take an alias. Anything else maps to `user-unknown`, which
    matches no record.
  - Both searches (`q`), and the `host`, `tool` and directory (`cwd`) filters, match the text
    as the reader sees it. Each record's account is learned before its text is masked, so a
    real name matches nothing, even in records that no response has named yet. An alias inside a
    path or a word is mapped back to its account only to find the records to test.
  - Whatever a response echoes from a request goes back exactly as it was sent, without passing
    through the rewrite (`anonymize.Verbatim`). That covers the export header's filters, a view's
    filter block and window status, an I/O lookup's query, a session id, and an Ask question,
    its title and classes, the answer, and the suggestions marked done. When echoes were
    rewritten, the service translated any name it was sent: `host=alice,zed9` came back as
    alice's alias, and `zed9`, which is not an account, came back unchanged.
  - For the same reason, Ask is refused while names are anonymized, unless `ask.unmasked_answers`
    is set. Its answer is shown as the agent writes it, and the agent can read whatever the
    service's account can, including the raw feeds, so a real name it read there would be shown.
    The suggestions marked done (`/api/suggestions/done`) are refused too, because they keep
    Ask's answers. That includes deleting one, so while names are anonymized the page can neither
    show nor clear them. To clear them, edit or remove `<cache.dir>/ask/done.json` by hand. With
    `ask.unmasked_answers` set, a suggestion kept from before names were masked, or under another
    salt, contains the names from the data it was written from, so it goes through the masking
    as that data would. The salt survives a restart (see the salt above), so only a new salt
    (after deleting its file) sends every kept suggestion through the masking again.
- **Not covered.** A person's name in free text that no rule ties to an account (a commit
  message, a folder named after someone), hostnames, partitions, and job ids. Also not covered is
  a name the masking cannot see yet: an account named in another account's command line, outside
  a home path, before any response has named that account. A search for that name finds the
  line, and if the same scan of `/api/events` read that account's own records, the line is served
  with the alias there. In `/api/health`, `anonymize` counts the names it knows, the
  collisions (two names, one alias), and the accounts left visible because their name is a
  reserved word. It never shows a name.

`tests/test_anonymize_leak.py` starts the app over a feed full of names, then reads every route
and the websocket. No byte may contain a name. Its `ReaderTextTest` instead sends a real name in
every query, path and body parameter of every route, and no answer may contain that name's alias
or any of that account's rows.

## Password

`server.auth.password_hash` (`RC_DASH_PASSWORD_HASH`) puts the whole service behind one
password: the page, its assets, every `/api` route (Ask LLM included), and the websocket. It is
empty by default, and in that case `serve` refuses to start, because on a shared host "bound to
127.0.0.1" means "readable by every local user". `server.auth.open` (`--open`, `RC_DASH_OPEN=1`)
serves without a password anyway, to anyone who reaches the port. A [demo](#demo-mode) is open
unless a password is set.

```bash
cd dashboard && .venv/bin/python -m rc_dashboard new-password --netrc-host dash.example.org
```

generates a 16-character password with `secrets`, using upper and lower case letters, digits,
and 18 symbols, with at least one of each class: about 101 bits. It writes the password to
`~/.config/rc-dashboard/password` and a netrc for curl beside it, both mode 0600, and prints
only the salted scrypt hash. Put the hash in the gitignored `dashboard.config.json`, then
restart:

```json
"server": { "auth": { "password_hash": "scrypt$32768$8$1$..." } }
```

- **Browsers** get a sign-in form, with a 401, at whatever URL they asked for, and land back
  on that URL once signed in. The session is a cookie that lasts `server.auth.session_days` (30).
  It is HttpOnly and SameSite=Lax, Secure when the request came over https (a proxy such as
  cloudflared reports this in `X-Forwarded-Proto`), and named `rc_dash_<port>`, so two instances
  on one host keep separate cookies. *Sign out*, at the bottom of the sidebar, ends the session. A
  page whose session expires goes back to the form on its next request.
- **Scripts** send the password as HTTP Basic auth, with any user name:
  `curl --netrc-file ~/.config/rc-dashboard/netrc http://127.0.0.1:8080/api/health`. The
  password then never appears on a command line, where `ps` would show it to other users.
- **What the service keeps.** Only the hash. A password is checked with scrypt, one check at a
  time, and compared in constant time. A wrong password waits an extra second. The cookie holds an
  expiry and its HMAC, keyed with a key derived from `<cache.dir>/session.key` (created once, mode
  0600) and the hash, so a new password or a deleted key file signs everyone out. `/api/config`
  and `--print-config` show the hash as `(hidden)`.
- **A restart, not a rebuild.** `server` is left out of both saved-state fingerprints, so
  setting, changing, or clearing the password only needs a restart. A malformed hash stops the
  service at startup instead of letting it serve without a password.

`tests/test_auth.py` starts the app with a password, then reads every route, an asset, and the
websocket, with and without the password.

**Who may connect** (`rc_dashboard/guard.py`), with or without a password:

- **A websocket from any other page's origin is refused** with a 403 before it is accepted.
  That means any websocket whose `Origin` names a host and port other than the request's `Host`,
  including a loopback origin on another port. A browser lets any page open a websocket to
  127.0.0.1, and if the page is on another port of the same host (a dev server, a notebook,
  another tunnel), the browser sends the reader's cookie too, because the cookie is SameSite=Lax
  and every port of a host counts as the same site. A script sends no `Origin` and is let through.
  A proxy that rewrites the Host to `127.0.0.1:<port>`
  needs its public origin in `server.allowed_origins` (`RC_DASH_ALLOWED_ORIGINS`,
  comma-separated: `https://dash.example.org`). The page served by Vite's dev server (`./run.sh`
  dev mode, on `RC_DASH_UI_PORT`, 5173) is on another origin too, so `run.sh` lists it, as
  `http://127.0.0.1:5173` and `http://localhost:5173`, in `server.dev_origins`
  (`RC_DASH_DEV_ORIGINS`), which adds to `allowed_origins` instead of replacing them. A Vite
  dev server started any other way needs its origin listed the same way
  ([web/README.md](web/README.md)).
- **With no password, a request under a host name that is not this host's is refused** (a 403).
  Its `Host` must be a loopback name, the bind host (`server.host`), or one of
  `server.allowed_hosts` (`RC_DASH_ALLOWED_HOSTS`; `*` for any). Otherwise a page on another
  site could point its own name at 127.0.0.1 and read every route as if it were its own (DNS
  rebinding). With a password, the cookie already prevents that, so the check is off, and a
  tunnel that keeps its public name in the Host needs no list. The demo image answers under any
  name (`docker/demo/Dockerfile`), because its data is made up.

`tests/test_guard.py` sends the handshakes a browser, a script and Vite's proxy send, and
starts the app open and behind a password.

## Ask LLM

The Ask LLM tab, and the *ask* button on a panel or a session, run an agent CLI on this host
(`rc_dashboard/ask.py`). By default this is `codex exec --sandbox read-only`, run as the service's
account with its Codex login, and given the question plus a compact, masked snapshot of the
window. The snapshot leaves the host, because it is sent to the agent's provider.

**The agent can read whatever the service's account can read.** Its sandbox is read-only, not
confined. The collector's files, the service's cache (including the anonymize salt and the
session key), and that account's home are all within its reach, and the question it is given is
text written by a reader. Run the service as a dedicated account that holds no other
credentials, not as a person's account. For these reasons:

- **Off by default.** `ask.enabled` (`RC_DASH_ASK=1`) turns it on.
- **Never without a password.** While `server.auth.password_hash` is empty, Ask is refused,
  even when turned on. `GET /api/ask` says why, and a question gets a 400 that says so. A refused
  question reads nothing.
- **A short environment.** The agent gets what a program needs to run, to find its login and
  to reach its provider through the site's proxy, and nothing else (`ask.AGENT_ENV` and
  `ask.AGENT_ENV_PREFIXES`): `PATH`, `HOME`, `USER`, `LOGNAME`, `SHELL`, `LANG`, `LANGUAGE`,
  `LC_*`, `TZ`, `TMPDIR`, `TERM`, `LD_LIBRARY_PATH`, `XDG_RUNTIME_DIR`, `XDG_CONFIG_HOME`,
  `XDG_CACHE_HOME`, `XDG_DATA_HOME`, `XDG_STATE_HOME` and `DBUS_SESSION_BUS_ADDRESS` (which
  `systemd-run --user` needs, as `hpc/deploy` runs it), the proxy and CA variables,
  `CODEX_HOME`, `OPENAI_*`, `ANTHROPIC_*` and `CLAUDE_*`. Anything else it needs, such as a
  provider reached through a cloud, is named in `ask.env_passthrough`
  (`RC_DASH_ASK_ENV_PASSTHROUGH`, comma-separated): variable names, or a family's prefix,
  three characters or more and then `_*` -- `["AWS_*", "GOOGLE_APPLICATION_CREDENTIALS",
  "CLOUD_ML_REGION", "AZURE_*"]` as the provider needs. A shorter prefix,
  or one without the `_` (`S*`), would take in whatever else begins so, and is refused at
  start; such a variable is named whole. The service's own `RC_DASH_*` settings, its password
  hash among them, are never passed. An agent that fails says so, since a credential left out
  fails it without a word about the environment.
  **The proxy variables are passed as they are**: `HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY`,
  `NO_PROXY` and their lower-case forms, so the agent reaches its provider through the site's
  proxy. A proxy URL often carries a user and password (`http://user:pass@proxy:3128`), and
  then the agent can read them -- an agent whose prompt holds other users' command lines. For
  the service's account, prefer a proxy that needs no password in the URL.
- **A directory of its own**, made 0700 in the system's temporary directory for each question
  and removed after, not under `cache.dir`, so the salt, the session key and the saved builds
  are not its neighbours.
- **A short question.** At most 4 000 characters (a 400 past that); the prompt carries the last
  three earlier turns, cut to 600 and 1 500 characters a side.
- **JSON only.** Every `/api` request but a GET, HEAD or OPTIONS (`POST /api/ask`, `POST
  /api/suggestions/done`, `DELETE /api/suggestions/done/{id}`, `POST /api/annotate`) takes
  `Content-Type: application/json` (a 415 otherwise, `guard.py:JsonOnly`), which another site's page cannot send
  without the browser asking first; a body past 1 MB is a 413, and a malformed one a 400.

`ask.agent` picks a preset (`codex`); `ask.command` gives the argv outright, `{workdir}`,
`{answer_file}` and `{model}` filled in, the prompt on stdin. A command, or the instructions in
`ask.system_prompt`, may hold a provider's key, so `/api/config` and `--print-config` show both
as `(hidden)` when set, and `GET /api/ask` names only the program a command runs (`claude` for
`env ANTHROPIC_API_KEY=... claude -p`). `tests/test_ask.py` checks the
refusals, the environment and the directory, and `tests/test_guard.py` checks the routes.

## Network endpoints

The Network bytes panel, in the I/O section of the Resources tab (under *Login nodes* in the
rail), lists beside its per-class table the ten remote endpoints the processes made the most send
and receive calls to. A switch ranks them by bytes sent and received instead. Each row splits its
calls or bytes by class, and the list follows the who filter and the class choice like the table.
It is counted from the collector's `net_endpoints` block (schema 7) as each process exited, so a
capture from an older collector shows no list. Demo mode turns the lookups below off, so its
made-up endpoints keep the host names its records give.

- **One row per name.** The I/O reducer adds up the calls and bytes of every remote address
  (`reducers/io.py`), and the service names each address when the panel is read
  (`endpoints.py`). It uses a reverse-DNS lookup from this host if it can, otherwise a name that
  a process's command line gave for the address (the collector's `host`), and otherwise the
  address itself. Addresses that share a name are one row. Inbound endpoints, whose address is a
  client's, are kept separate from outbound ones. Hover over a name to see its addresses, its
  ports, and where the name came from.
- **Lookups never hold up a request.** A few background threads look up the addresses, busiest
  first, and keep the answers in `<cache.dir>/rdns.json` (mode 0600), so a restart doesn't repeat
  any lookups. A name is looked up again after a week, a "no name" result after a day, and a
  failed lookup after ten minutes, and the old answer is served in the meantime. While names are
  pending, the page re-reads the panels a few times. The lookups use this host's resolver, so a
  private address in another site's capture is named however this host's resolver names it.
- **Exact for any mix of classes.** The service sends the top ten by calls and by bytes for
  every mix of classes, so the page can rank the classes it shows and still hold their whole top
  ten.
- **What it cannot show is said.** The note under the list gives the share of the table's bytes
  whose endpoint is known, the bytes a block held with no address, and the processes that moved
  bytes with no endpoint block at all.
- **A restart, not a rebuild.** `server.rdns.enabled` (`RC_DASH_RDNS`, on by default) turns the
  lookups off, and names then come only from command lines. `server.rdns.workers` (4) sets how
  many lookups run at once. A name is applied when the panel is read and is never saved in a
  reducer's state, so neither setting affects a saved build.

`tests/test_endpoints.py` covers the names, the lookups, the grouping, the ranking, and the
served payload, and `tests/test_io.py` covers the reducer.

## Login node health

The node tier covers what the eBPF tier cannot see, because none of it is a process: the load and
memory of the whole node, how quickly it responds, its NFS mounts, its local disks, and each
account's cgroup. The `node` panel (`reducers/resources.py`, `NodeReducer`) holds the newest two
snapshots per host, and the Resources tab draws it first, as **Login node health**. It shows one
table at a time (nodes, NFS mounts, local disks, accounts, stuck processes), and each choice shows
its count and how many items in it have a problem.

- **The service sets the thresholds.** A local filesystem at or above `node.disk_full_pct` (90) of
  its space or `node.inode_full_pct` (90) of its file slots is `full`, and the panel says which
  threshold applied in `thresholds`. Needs attention shows the same flag.
- **An account held back** is one whose cgroup's `throttled_usec` grew between the two snapshots,
  meaning the kernel paused it at its CPU limit. Its CPU is a rate over the same interval, in cores
  in use (`cpu_cores`). It is null with only one snapshot, and `counter_reset` (no rate, which is
  not the same as idle) when the cgroup was recreated in between.
- **Stuck processes** are the snapshot's `d_state_procs`, up to 20 per host with the rest counted.
  Each shows the pid, account, wait channel, the kernel stack's top frames where the collector
  could read them, and the command line. The command line is masked here (`redact.py`) because
  this panel is served as built, not through a window's redaction. The eBPF tier's `d_stack_top`
  (what its tracked processes were blocked in) is carried on the live frame's host rows, and the
  hover on the D-state count shows both.

### Over the window

**Login node response**, on Live under the window's CPU chart and zoomed with it, plots four of
the node tier's numbers per host over the window: the time to start a process and to look up an
account (the collector's own probes), the slowest NFS mount's read round trip, and the load. A
spike there, lined up under the agents' CPU in the chart above, is what "do agents make the node
slow for people?" looks like.

- **Read on its own thread** (`nodehist.py`). A snapshot is ~200 KB and arrives every
  `EBPFM_NODE_INTERVAL` (300 s), which is ~60 MB per host-day. So the first read of retention runs
  alongside the service, 4 MB at a time, and nothing waits for it; the panel shows how far it has
  read. After that it reads only what was appended, tracked by inode and offset. When a day is
  sealed into its `.gz`, it maps onto the snapshots already held. The thread keeps ten numbers per
  snapshot for the whole retention, and reads `feeds.ebpf_node.series_days` day-dirs (0 means
  enough for retention, regardless of `feeds.ebpf_node.days`).
- **No resolution the samples never had.** A bin is the window's own width or twice the snapshot
  interval, whichever is wider, so a snapshot that drifts across a bin edge leaves no hole. A hole
  always means a missing snapshot. A latency bin shows the slowest probe in it, and a load bin its
  mean.
- **Hosts are not classes**, so their lines use hues that no class uses. With more than five
  hosts, the five that stand out most are named on every chart, and the rest are drawn as one grey
  line showing the slowest of them in each bin.

`tests/test_nodehist.py` covers the binning, the holes, the incremental read and the sealed day.

## Day by day

**Day by day**, on Live under *Who uses what*, shows each class's processes, CPU and users per
local day over everything the live tier holds (its retention, not the window). It is computed
from the tier's own minute bins and roster (`LiveReducer.daily`) and served in `/api/panels` as
`trend`. The user and agent-type filters do not narrow it, and the panel says so when one is set.
Like every categorical panel, it shows count and cost side by side, either as numbers or as each
class's share of the day.

- **Three kinds of day.** A day the collector wrote records in has its numbers, plus `held` (the
  minutes of that day that held a record) and `minutes` (the minutes the tier could hold). A day
  it wrote nothing in has null counts and no bars, and is not shown as a quiet day. Today is still
  in progress, and the oldest day starts where retention does (`cut`).
- **Users come from the roster**: an account counts once per day in each class it ran something
  in. The roster's bins are hours past the newest day, so a day boundary that isn't on the hour
  (a +5:30 zone) falls with the hour it lands in.

## Commands that failed

**Commands that failed**, on the Agents tab under the tool panels, shows how the Tool mix's
processes ended (`tool_mix.exits`, `reducers/tools.py`). It covers the same processes, after the
Tool mix's shell dedup, per class and agent type, by tool and by kind of tool.

- **A failure is a non-zero exit or a stop by a signal**, with two exceptions that are not really
  failures: an exit 1 from a program where 1 is an answer (`EXIT_ANSWERS`: grep, rg, ag, diff, cmp,
  and the grep family; shared with the inspector's "hide typos"), and SIGPIPE, which
  `command | head` sends once head has its lines. Both are counted separately, as is a record with
  no wait status at all. A rate is taken over the processes whose ending is known.
- **The collector does not fold signals into exit codes**: its `exit_code` is null exactly when a
  signal ended the process. The panel lists each row's most common exit codes and signals, and
  explains the conventional ones (127, command not found; SIGKILL, killed outright).
- **Each count opens its commands** through the `/api/io/commands` lookup with
  `outcome=nonzero&outcome=signal`, which uses the same verdict rule the reducer counts with
  (`export.make_predicate`). The lookup reads records before the dedup, so a shell that ran a
  failing command is listed next to it.

## Sessions: tasks, phases, and processes

Every session's row says what it was doing at three levels, which the page calls **Tasks**, **Phases**, and
**Processes**: an agent's, by the tool calls it made, and a session no agent ran (a person's login, an editor's
terminal), by the commands its terminals ran.

| level | what it is | built by |
|---|---|---|
| Tasks | stretches of the session that were about one thing, each labeled for what it was for | `reducers/phase_tasks.py` cuts and labels them, from the names and the agent's own words that `reducers/phase_names.py` reads out of each command |
| Phases | what the session spent its time on inside a task: exploring, editing, building, testing, running, Slurm jobs, waiting, git, and idle | `reducers/phase_segment.py`, from the calls `reducers/phase_rules.py` names |
| Processes | every process the session ran, in its tree | `/api/trajectory` (`proctree.py`) |

Every row carries the first two as `phases`: the `list` of phases, the `tasks` above them (each phase says
which one it belongs to), a `headline` (the project, then the names that recur across the tasks, as
`KempnerForge · PRs #229, #233 · video_io.py`), and `n_calls`; what kind of session it was, a `category`
with the `uses` of its work it rests on and a `summary` (*What kind of session it was*, below); and
`driver`, whose calls they are: `agent` or `terminal`. How the page draws the levels is in
[`web/README.md`](web/README.md#sessions-tasks-phases-and-processes).

**Which processes are calls** (`reducers/phase_rules.py`):

- **An agent's tool calls** are its runtime's direct children. Claude Code and Codex have rules of their own
  (their Bash tool's shell, Claude's Grep and Glob, Codex's shell and bare commands, each agent's own chores
  counted as housekeeping). Any other agent -- Copilot, a Cursor or Windsurf agent, a sandbox the collector
  keyed on its own, a type it learns later -- is read by the general rule, Codex's with only the chores any
  agent runs: its own executable run as a probe, client, server, or helper, a login shell's startup helpers,
  and `git --version`. An editor's server (VS Code, Cursor, Windsurf) runs nothing for anyone directly: its
  direct children are the editor's own, and its terminals are read as any terminal is.
- **A terminal's commands.** A shell a person types into runs each command line as a job, a process group of
  its own, in the session the shell leads (sshd, a tmux pane, and an editor's terminal host each start it so).
  The collector records each process's group and session (`pgrp`, `sid`), so a session no agent ran is read
  from them a record at a time: a process that leads its own group in a session it does not lead is a call,
  with everything under it, and so is a later stage of its pipeline and a command that leads a session of its
  own (`ssh host cmd`, `setsid cmd`). The shell itself is never a call (it ran the whole time), and what it
  runs in its own group -- its startup files' helpers, its prompt's `git rev-parse`, what a typed `module load`
  or `conda activate` runs -- is its housekeeping, as are a terminal multiplexer and the daemons that lead a
  session after a fork. A record whose group and session were not kept (a collector without the feature) is
  not read, and counted (`phase_ungrouped`).
- **A call still running** is read from what its children did while it runs, as the module docstring of
  `reducers/trajectories.py` says; in a session no agent ran, only what runs inside a command (a job, or the
  command that leads a session) can be one, never what waits under the shell itself or an editor's server.

A session running now whose first call has not ended has no call to cut phases from: a call is recorded when it
ends. Its row and its inspector draw its live edge alone, from its first record to now, and say what the
per-minute census shows running in the summary's place, never `no tool calls`.

All of it is read from the commands the session ran and the words written in them, and from nothing else,
as the basis line over the inspector's chart says. Reading and editing inside an agent, its thinking, and
a log entry it writes with its own Write or Edit tool leave no command, so none of them is seen, and nor is
what a person reads or edits in an editor's window. No model
writes a label, and no transcript, prompt, or file on disk is read. The cutting and the naming are
heuristics over command text, and their thresholds were tuned on seven hand-labeled sessions of boslogin06
and rtx6000a. `rc_dashboard/phase_report.py` reads a day of a feed as a window build does, prints the
phases and tasks of the agents it is asked for, and summarizes every row, which is what the thresholds are
tuned against.

**Where a task begins.** Calls that start within `phases.step_s` of a step's first call are one step, and
every cut falls between two steps. A task is cut

- at a gap of `gap_s` or more in which the agent was waiting on nothing. The agent is waiting on the calls of
  the step it just made, and on any process that a finished call left running (`nohup job &`) while that
  process runs. A call from an earlier step that is still running when a later step begins is one the agent
  has moved past, so it holds nothing open. The service lists up to three such calls with the gap, each under
  the name the gap line uses for it: the script it ran, or else what it tested, the job it submitted, or the
  folder it made, as a label would show them. For a still-running call with none of these, it uses the
  programs seen under it (`pgrep and sleep`). The page names the first one, as in
  `scratchpad/linktest still running since 13:05:44`, or by its command if it has no name;
- where a name new to the session starts to recur: a step holds a name that first occurs in that step, and
  the name turns up again in at least `new_steps` later steps of the stretch, with the `new_steps`-th of them
  within `new_window_s` of the first. A search term or a folder changed into does not count as such a name,
  and starts no task;
- after a commit or a pull request that ends a burst of git (git calls and nothing else, each within two
  minutes of the one before), when the names in the `new_window_s` before the burst and the names after it
  overlap by less than `close_overlap`. When bursts each start within `close_series_s` of the end of the one
  before, they produce only one cut, after the last.

Polls and bursts are folded before names are counted. A command on a timer counts once, and so does a
command run again within three minutes, so a job polled a hundred times is named once. A piece shorter than
`min_s`, or with fewer than `min_calls` calls, is then merged into whichever neighbor it is more similar to,
unless it contains the agent's own words. It is never merged across a cutting gap. Each local day is merged
down toward `cap` tasks, starting with the most similar neighbors. A few calls that are hours away from other
work (at most `lone_calls`, all within `lone_s`, with a cutting gap or the session's edge on each side) form
a **lone stretch**. A lone stretch is not a task, is never merged into one, and does not count toward the
cap. Every session with calls has at least one task.

A row is cut and named again each time it is built, so a task that contains a still-running call is open,
and is named from the calls it has so far. A session's call log holds `phases.max_calls` calls. Beyond that,
calls are counted but placed in no phase. The last span (which may be a lone stretch), and any span that ends
after the first dropped call, are marked as incomplete.

**How a task is labeled.** There are two sources, and the page shows which one a label came from.

- **The agent's own words**, when a command in the task contains any. These are the title of a pull request
  (`gh pr create` or `gh pr edit` with `--title`), the subject of a commit (`git commit -m`, or `-F -` with
  a heredoc), or the title of an entry the agent writes to a Markdown file whose name has `log`, `plan`,
  `notes`, `handoff`, `progress`, `changelog`, or similar as a word of its own. A word here is a part of the
  name split on `_`, `-`, `.`, and camel case, so `CHAT_LOG.md` and `progressNotes.md` are logs and
  `methodology.md` is not. An entry is the text that a `cat` or `tee` heredoc, an
  `echo`, or a `printf` writes, and its title is its first heading or bold line, or else its first line that
  contains a word. A pull request's title takes precedence over a commit's subject, and a commit's subject over
  a log entry. Among several pull requests or commits, the last one counts, since it closes the work. Among
  several log entries, the first one counts, since it usually states the goal. Only text in the command counts,
  so `git commit -F msg.txt` has none, and the words are masked like every command (`redact.py`). The page shows
  them as a quote, in quotation marks, with their source (`commit`, `PR #235`, or `log`).
- **Otherwise, names.** The label is a verb for the category that holds most of the task's time (`Read`,
  `Edit`, `Edit and test`, `Test`, `Build`, `Install`, `Run`, `Run on Slurm`, `Wait on`, `Commit`, or
  `Search for`), followed by the one or two names most specific to the task. A name is a file a command runs,
  tests, writes, or reads, a folder it creates or changes into, a Slurm job, a pull request, a branch, a
  worktree, a package it installs, a name the agent gave its own scratch, what it searched for, or another
  argument that reads as a name (`marinQC51`). Some text names nothing a reader would recognize, so it is not
  a name. That includes the value of an option known to take a value (the partition of `srun -p kempner_h100`),
  any arguments given to Slurm's queue and accounting commands (`squeue -u xq404`), the operands of a shell test,
  which are a loop's stop condition rather than its work (`[[ "$s" =~ ^(COMPLETED|NODE_FAIL) ]]`), Claude Code's
  id for a subagent's worktree (`agent-a69e2723ecfaa3a16`), Claude Code's own folders in its temporary folder
  (`/tmp/claude-<uid>/<project>/<session>/scratchpad`), and what Claude Code keeps in its home:
  `~/.claude/projects`, a project's folder in it, a session's transcript `<uuid>.jsonl` and the folder
  `<uuid>/` next to it, and the project's `memory` folder and its index `MEMORY.md`. A relative path is
  resolved in the folder the command changed into, so `MEMORY.md` after `cd ~/.claude/projects/x/memory` is
  the index. Whatever the agent creates inside these folders, such as a script in its scratchpad or a note in
  `memory`, is named as usual. A name's score depends on how many of the task's steps contain it, on how few
  of the session's spans (tasks and lone stretches alike) contain it, and on its kind. It scores more where
  it first occurs. The objects a task works on count in full, a name the agent gave its own scratch
  0.8, a file that is only read 0.6, another argument that reads as a name 0.5, a folder changed into 0.1,
  and a search term 0.05 (`phase_tasks.WEIGHTS`). In a session of two or more spans, a name that every span
  contains, lone stretches included, scores nothing. With only one span, every name is in every span and
  still scores. A name that is the session's user names no task. The page shows a built label without
  quotation marks or a source, so it is never mistaken for something the agent wrote.

The headline is the project, followed by up to `headline_names` names that two or more tasks contain,
excluding the session's user. The project is the folder the agent was launched in. If that is a home folder
or is named for a person, the project is instead the folder, not named for a person, that the agent's
commands change into most often. A folder is named for a person when its name, or a part of it split on
`_`, `-`, and `.`, is the user's name, or is the name of a home or lab folder that its path runs through
(`/n/netscratch/alpha_lab/Lab/jdoe`, `…/Lab/netscratch_rroe`), as `anonymize.py` finds them.

**Four things a plain reading of the commands gets wrong**, which the rules account for:

- **A program held in a variable** is read as the program it names. An assignment earlier in the call
  supplies it (`PY=../venv/bin/python; $PY -m pytest tests/x.py`, `S='sbatch --parsable'; $S job.sh`).
  Failing that, a variable named for Python (`$PY`, `$PYTHON`), or one whose first argument is `-m`, `-c`,
  `-`, or a `.py` file, is treated as Python and read by the Python rules: `-m pytest` is a test, a heredoc
  follows the heredoc rules, and a script is read by its name (`$PY check_env.py` is a test, `$PY train.py`
  a run). Nothing else is inferred, and `$CMD --flag x` stays unknown.
- **The Codex desktop app's reads of its own plugins** are housekeeping, not the model's calls. The app
  `cat`s the icons of the plugins it lists, hundreds per minute, each one file in an `assets/` folder under
  `.codex/.tmp/plugins/`, `.codex/.tmp/bundled-marketplaces/`, or `.codex/plugins/cache/`. Only the app's own
  argv counts, so a model's `rg`, `sed`, or `cat` of a plugin's skill is still a call. A `cat` whose argv was
  not recorded (schema 7) names no file, so it is judged by when it ran. It belongs to the app if it began
  within 2 s (`reducers.trajectories.phases.app_read_s`) of one of the app's reads in the same row, before or
  after, and is a call otherwise. On boslogin06 on 2026-10-09, this rule took 230 of the day's 8,434 Codex
  calls, 226 of them from one row. A wider window would catch the model's own reads, which can come as soon
  as 15 s after a burst.
- **A call the agent has moved past holds no idle time.** Only what the agent is still waiting on holds a gap
  open (see above). A call from an earlier step that is still running when a later step begins holds no idle
  gap and no guess, however long it runs, so a call that hangs for hours does not block the gaps after it. On
  the lane, time goes to whatever the agent started most recently, so the hung call does not take the time of
  the work that follows it. A gap that begins while the call runs lists it (`alive`). Once the call ends, the
  time it ran past the phase it began in is reported in that phase (`train.py kept running for 2.7 h`), the
  same way a background job's is. The session's last phase ends at the latest end of all the session's calls,
  or of a process one of them left running, whichever is later, so the lane does not end while anything the
  agent started is still running. That describes the lane, not the work. Each phase also says where its own
  calls ended (`own_t1`), and how long its calls, or what they left running, went on past it, by the call's
  category (`ran_on`). The summary weighs the work using those values (below).
- **A session across midnight is one block.** A row covers one local day, because its session id includes the
  day, so an agent that works past midnight has a row for each day. Each of those rows serves the same block,
  cut from the calls of all of them, so midnight splits no task or phase, and either row opens the whole
  session. `days` lists the rows (`{sid, first, last}`), and `sid` on a task or a phase says which row holds
  it: the row whose span (first record to last) contains its start, or else the nearest row. A row's own
  `n_calls` still counts only its own day, and the block's `n_calls` counts the whole session.

**What kind of session it was.** Each block describes this in one word and in a few sentences
(`reducers/phase_summary.py`), so a list of sessions can be read, and counted, by what they were for. The
`category` is one of the uses by which the Users tab counts a user's calls: `explore` (read and search code
and files), `develop` (change files from the shell, and test), `build` (build and install software), `run`
(run programs), `hpc` (Slurm), or `git`. A session is put under the use that holds half or more of its work,
and is `mixed` when no use does. A session's work is its phases' time, excluding idle gaps, waiting, and
commands that were not recorded, with the last phase counted only to the end of its own calls (`own_t1`). The
time a call, or a process it left running, went on past its phase counts toward the call's own use
(`ran_on`). So a call that hung for 14 h next to a minute of edits makes a session of running, not of
changing files (it used to read "Mostly changed files from the shell (99% of
14 h of work)"). A training run left going for 3 h while the agent read its log is 3 h of running alongside
the reading, so the work can be longer than the session. A wait on Slurm counts as Slurm work, as it does for a task's verb, because a job is the scheduler's work whether it is
being submitted or waited on. Any other wait (on a process, a file, or a fixed sleep) belongs to no use and is
reported separately. The uses are weighed by time, as the band draws them, not by calls. Otherwise a Slurm
session's polls of the queue, or one long `python` run, would be outnumbered by the quick reads around them. A
block whose phases have no length at all (a call or two that began and ended within one second) is weighed by
its calls. A session that only waited is `wait`. A session is `unknown` if its unrecorded commands hold half of
its work plus their own time, or if it did nothing else. A session that made no tool call is `none`. `uses`
gives each use's share of the work, largest first.

The `summary` is one to three sentences, each built from the block's own numbers and none written by a model.
They say what the work was and its mix (`Mostly ran jobs on Slurm (82% of 5.1 h of work), then explored code and files
(12%).`), the tool calls and tasks with the distinct names they held (`564 tool calls in 7 tasks, 5 named in
the agent's own words; 61 Slurm job names, 74 scripts run.`), and how long the agent waited and on what, when
that is at least a tenth of its time excluding idle gaps (`Waited 4.7 h on Slurm, polling every 2 min.`). A
Slurm job is named once per call, by the name it was given (`--job-name`, `-J`), or else by its script's name.
Develop work is described by what the session contained: `edited and tested code` with both an edit and a
test, `changed files from the
shell` with no test, `tested code` with no edit -- a test's time past its phase (`ran_on`) counts as
testing, whichever phase the test started in. When the work is longer than the time it was done in (because a
call that ran on counts alongside the phases after it), the summary gives that time too: `Mostly ran programs (51% of 5.9 h of work in 3.0 h)`.

On the eight FAS RC login nodes (`boslogin05`–`08`, `holylogin05`–`08`) on 2026-11-12, 2026-11-13, and
2026-11-15, 16,630 sessions had an agent at their root. 12,534 of them (75%) made no tool call. Half of those
lasted under a second and ran only the agent's own commands (`rg`, `git`), and an agent that only reads and
edits with its own tools leaves no command either. Of the 4,096 that made a tool call, 33% were `explore`, 29% `run`, 20%
`hpc`, 6% `mixed`, 5% `develop`, 4% `unknown`, 1% `wait`, and under 1% each `build` and `git`. One use held
half of the work in 94% of the sessions that did any work, and counting calls instead would have given a
different category for about a third of them, mostly Slurm sessions and long runs. A fifth of the summaries
say the agent waited. Claude Code's sessions were 27% `hpc` and Codex's 4%, while 42% of Codex's were `run`.
There is one known misreading: 114 sessions of one user's Codex desktop app on 2026-11-15 ran two calls each,
a `mkdir -p` and a `chmod 700` of a folder of its own under `/var/tmp`, and they read as `develop` (`Only changed
files from the shell. 2 tool calls.`), as their tasks read `Edit`. The folder is part of that user's
setup, not Codex's, so it is not treated as housekeeping. `rc_dashboard/phase_report.py` prints each row's category and summary, and counts a day's
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
bound the call log and join a call's children to it (`max_calls`, `hk_bin_cap`, `pend_max` and
`idle_s`), and `app_read_s`, which decides as a `cat` arrives whether it is a call or the Codex
app's own read.
The rest, every task and summary threshold among them, are read when a row is built: the reducer cuts and
names a row each time it serves it, with the thresholds it runs with, and no saved state holds a task or a
category.

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

## Tool-call purposes from a model

The rule-based `purpose` (git, build, compute, ...) says what kind of program a call ran, not
why it ran. The session inspector's **✦ annotate purposes** asks a language model instead. The
model says what each of the session's tool calls was for, in a few words ("check which CUDA
version the node has"), and picks one of a fixed set of kinds (explore, edit, build, test, run,
debug, hpc, git, data, env, monitor, other). This is the only place where a model writes onto a
row, so its answer is kept visibly separate from everything measured: a dashed, purple ✦ tag
beside the command in the table, a ✦ beside the box's name in the tree, and the model named in
every hover. The answers are never counted anywhere.

It is **off by default**, because it sends command lines off the host. What is sent is exactly
what the page shows: each distinct tool call's command line (what a Claude Code Bash call or a
Codex sandbox ran), masked by `redact.py` and, with [anonymized names](#anonymized-names) on,
by the anonymizer, plus the folder it ran in. The service takes these from its own read of
the session. The page posts only the session's name, so the endpoint cannot be used to send a
model anything else. The answers also come back through the anonymizer, because an answer kept
from a run without anonymization, or one written by an agent on this host that read the raw
feeds, may contain a name. No text written by the reader reaches the model here, so that rewrite
never translates anything the reader chose. The model is asked about the first
`annotate.max_calls` (400) distinct calls, in batches of
`annotate.batch_size` (40) per request.

Each answer is kept in `<cache.dir>/annotate/purposes.jsonl`, keyed by the provider and model,
the prompt it was asked with, and the command line. So a command asked about once is known to
every reader of every session that runs it, and opening a session shows what is already known
immediately, without asking anything. A different model or a changed prompt (a new built-in
version, or an edit to `annotate.system_prompt` or its file) asks again from scratch. A call that
the model's answer leaves out stays unlabelled and is counted as unlabelled; nothing is filled
in for it.

Set it up under `annotate` (`dashboard.config.example.json` lists every key):

| `provider` | what it calls | the key |
|---|---|---|
| `anthropic` (default) | the Claude Messages API; `model` defaults to `claude-opus-5-5`, `effort` (`low`) is sent as `output_config.effort`; `base_url` for a gateway | `ANTHROPIC_API_KEY` |
| `openai` | any OpenAI-compatible `/chat/completions`: OpenAI, or with `base_url` vLLM, Ollama, LiteLLM, OpenRouter, ...; `model` is required | `OPENAI_API_KEY`; none for a local `base_url` |
| `codex`, `claude` | the Codex or Claude Code CLI on this host, as the service's user, with that user's login | the CLI's own |
| `ask` | whatever agent the chat (`ask`) runs | the agent's own |
| `command` | any command: the prompt on stdin, a JSON answer on stdout or in `{answer_file}` | -- |

A CLI provider (`codex`, `claude`, `ask`, `command`) runs an agent on this host over other
users' command lines, so it follows [Ask LLM](#ask-llm)'s rules. It is refused while the service
has no password, and it runs with only part of the service's environment (`ask.env_passthrough`
adds more) in its own directory inside the system's temporary directory. An HTTP provider runs
nothing here. The model gets the masked text the page shows, with no tools, and its answer is
only read as a label and a kind, so it is not refused when there is no password.

The key is read from the variable that `api_key_env` names (if empty, the one in the table above),
otherwise from `api_key_file`, and otherwise from `api_key`. `/api/config` and `--print-config`
only show whether `api_key` is set, as they do for `command` and `system_prompt`. The HTTP providers use Python's standard library, so `HTTPS_PROXY` is honoured.
`auto: true` asks as soon as a session opens, instead of waiting for the button. The environment can set
`RC_DASH_ANNOTATE=1`, `RC_DASH_ANNOTATE_PROVIDER`, `_MODEL`, `_BASE_URL`, `_KEY_ENV`,
`_KEY_FILE` and `_CMD`. For example, a local model:

```json
"annotate": { "enabled": true, "provider": "openai", "model": "qwen2.5-coder:7b",
              "base_url": "http://127.0.0.1:11434/v1" }
```

## Agents: subagents and blast radius

The **Agents** tab (`?tab=agents`; the old `?tab=cascades` still opens it) answers "which
agents started which other agents, and where did each one work?". A node is a process whose
role (`roles.py`) is `agent`, `infra` or `client`. Tool-call shells, sandboxes, probes and
helpers are steps between nodes. A node's parent is the nearest node above it in the process
tree. If there is none, it is the user's only daemon (for a `bg-pty-host` orphaned to init),
labelled as inferred, and otherwise the tree root. An agent started beneath another agent,
through any number of steps, is that agent's **subagent**. Two outcomes are kept separate:
**nested**, an agent with subagents, and **hosted**, a daemon, Claude Desktop's remote server or
`claude rc` running sessions side by side. A Codex TUI whose direct child is `codex app-server`
is shown as that session's front-end, not as a second agent.

`claude daemon` is the root of its own tree, hosting its background sessions side by side. It is
started, detached, by whichever `claude` process needs it first, and its `--spawned-by {"pid": N}`
names that process. But the daemon belongs to the user, and every background session the user
starts later, from any terminal, goes through it. When the daemon was hung under the process that
started it, as it used to be, each of those sessions was drawn as a subagent of that one session.
The process that started the daemon is recorded on its node (`spawned_by`, in its box's tooltip),
but is never treated as its parent. Neither is the ppid that a detached daemon keeps until its
starter exits. A session run by the daemon's `bg-pty-host` shows as a `terminal` **background
session**. The daemon also starts sessions ahead of time, each a `claude bg-spare` waiting to be
claimed, so the next background session starts immediately. A spare that nobody claimed ran no
process except its own, so it belongs to the daemon (`infra`) and is not an agent. When spares were
drawn as agents, each one was an agent that ran nothing. Idle spares are counted per tree and in
the summary (`idle_spares`). A spare that ran anything was claimed, and is shown as the agent it
became.

Every tree is sent, up to `reducers.cascades.top_trees` (1000), deepest first. The cap used to be
25, and with 25 trees of subagents in a window, no session that started no other session was sent
at all. Past the cap, the list says how many trees were left out, and the type strip says that it
counts only the trees sent. A blast radius is served for the `reducers.workspace.max_agents` (300;
was 30) busiest agents, and for every agent that wrote outside its start folder. Picking an agent
beyond that limit says so, and an agent that ran nothing says that instead.

What it cannot see: Claude Code's Task-tool subagents and workflow agents run inside their
parent's process. Only agents that are processes of their own appear, and an in-process
subagent's commands are counted as its parent's. On madsys-gpu1 over 2026-10-01..07, that came
to 49 agent trees, 4 hosted and none nested.

**Workspace & blast radius** (`reducers/workspace.py`, composed per agent by the cascades
reducer) shows where each agent worked, relative to its **home**. The home is the agent process's
own cwd, or, when that is `/` (IDE-hosted), its first call's directory below the root, labelled
as such. The collector records no file paths, so three kinds of evidence are kept separate on the
page and in the payload's `basis`:

| reading | kind | what it is |
|---|---|---|
| ran in | **measured** | processes whose cwd the collector recorded as that directory. It reads cwd at exec for some processes and copies the parent's at fork for the rest, which is before a shell's own `cd`, and before the chdir that Claude Code's Bash tool makes into the directory its last call left it in (on rtx6000a, 1 173 of 1 180 tool-call shells on 2026-10-11 had a copied cwd). Each view says how much was read (`cwd_read_pct`) |
| bytes | **measured, per process** | rchar/wchar (syscall, pipes and terminals included) and read_bytes/write_bytes (disk) of the processes that ran there, credited to where they ran, not to where the bytes went |
| named | **inferred** | distinct paths a command line names, classified by the command: read (`cat`, `grep`, `ls`, a script run, `<`), write (`>`/`>>`, a `cp`/`mv`/`rsync` destination, `tee`, `touch`, `mkdir`, `sed -i`, `tar -x`, `git clone`, `wget -O`, `curl -o`), delete (`rm`, `rmdir`, `git rm`, `find -delete`), and anything else path-like as named. Naming a path is not proof of touching it |

Exact file-level reads and writes would need a collector probe (openat / unlink / rename
tracepoints) that has not been built. Claude Code's Read, Edit and Write tools and Codex's
apply_patch also run inside the agent's own process and leave no command line at all.

Directories inside the home are shown as relative paths (`./src`). Directories outside it use `~`
for the user's home and `~bob` for another user's. Directories more than `collapse_depth` (4)
levels below the home or a top-level root (`/tmp`, `/scratch`, `~/.cache`, `/n/<fs>`, another
user's home) are folded into that ancestor, as are a directory's quieter subdirectories beyond
the first six, and every fold is counted.
A **write or delete named outside the home is flagged** and labelled by where it landed
(another user's home and system paths first, the agent's own `/tmp` scratch last). The
toggle **this agent only / with its subagents** adds every agent beneath it, judged against
the outer agent's home. Claude Code's tool calls are read through their `eval '<cmd>'`
(`proctree.ran`, the inspector's own reader), so the snapshot each call sources and the cwd
file it writes are never counted, and a fork that inherited its parent's argv is not read twice.
A relative path is resolved against the process's cwd, never against `/`. (An agent's process is
at `/` when it inherited an IDE's or a daemon's directory, and `ls src` from there does not mean
`/src`.) A relative path is also never resolved by climbing out of a copied cwd with `..`.
Otherwise `./bin/cachesim ../data/x`, with a cwd copied from an agent in `/home/marthen`, would
read as `/home/data`, which is another user's home.

Cost and bounds: each distinct argv is parsed once (with a regex tokenizer, never `shlex`), and
the per-record path is a few dict lookups. On rtx6000a's 2026-10-10 (1.89 M records, a ~90 s 24 h
build), `feed` took 2.4 s, ~1.4 µs per record, and the saved window grew by 2.4 MB. The state is
one entry per collector session key, capped at `reducers.workspace.max_keys` (150 000) keys,
`max_dirs` (300) directories per key, `max_dir_entries` (400 000) overall, 64 named paths per
directory, and 32 per command line. Past any cap, the excess is counted but not kept. At the key
cap, a new key first evicts the keys last seen before the window began. The service only walks
the keys for this once the oldest key kept by the previous walk has left the window, so a cap
full of keys inside the window costs one walk, not one per record. Served paths are masked
(`redact`), and no raw argv is served.

The **drift** proxies (`cwd_outside_pct`, `mix_jsd`) are read from the session rows, which
serve them (`trajectories`' `drift`). The Agents page's blast radius no longer shows them, and
neither does its list of sessions.

## Users: a portrait of each user

The **Users** tab (`?tab=users`, with a portrait opened as `&portrait=<user>`) shows how each person works
with coding agents: which agents they use, how and from where they launch them, what the agents' commands do,
how long a session runs and how many run at once, what time of day, what they work on, and what they ask of
the scheduler. Each of their sessions opens its process tree under the portrait (`&traj=<sid>`): the
inspector the Agents tab opens beside its list of every session, read the same way (`/api/trajectory`).

Nothing here is reduced or saved. `rc_dashboard/portraits.py` composes the tab when it is read, from the
reducers a window view already holds (the trajectories, the sandbox and Slurm) and from the live tier for what
is running now, so it can never disagree with the tabs those reducers fill. It holds no state that a saved
window or hour must keep. It is not in either fingerprint (`snapshots.CODE_FILES`, `hours.CODE_FILES`), no
reducer imports it, and changing it rebuilds nothing.

| part | from | what it says |
|---|---|---|
| launch | trajectories (`surface_of`, `approval_of`), sandbox (`agent_calls`) | agent types; where each session is driven from; approval at launch; whether its tool calls ran sandboxed, as the Security tab counts them |
| rhythm | trajectories (each row's first and last record, each call's start) | sessions, how long each was open, the most open at once, and the minutes in which a call began, by local hour and weekday |
| work | each session's call log; its phase block, for one user's portrait | tool calls by category (`phase_rules.CATS`), counted; time in each phase; the agent's own words in a command (a commit message, a pull request's title, a log entry); the project and the tasks |
| footprint | trajectories | processes, CPU, disk and network bytes, peak memory, summed as a row of the Agents tab's list of sessions sums them |
| scheduler | Slurm (`_sessions`, `_polls`) | jobs, refusals, queue checks and polling loops of the user's agent sessions, folded as the HPC tab folds them |

- **A session is one agent run.** The rows the Agents tab's list of sessions holds for one agent on two days
  are one session here, just as the agent's phase block already is. Only rooted sessions count, as on that
  list's plates, so a status bar probing `codex --version` every minute is not a session. A session belongs to the
  account its records name first. A session whose records name no user and no uid (the trajectories reducer
  falls back to `uid:N` where there is a uid) gets no one's row. The roster counts it separately
  (`unattributed`) and says so under the table, and the Agents tab's list of sessions lists it with the rest.
- **Unread is not none.** An agent whose commands the service does not read (anything other than Claude Code
  and Codex) has null calls, shown as *not read*, never 0.
- **A share is taken over the known values**, with the rest counted next to it. For example, a Codex app
  server's approval is unobservable, and a session whose own record was not seen has no surface.
- **How they leverage agents** combines the same numbers (`portraits.leverage`): how most of the user's
  sessions are driven (a terminal, scripts, an IDE, the desktop app or the web, or else several of these), the
  freedom the agents start with, how many run at once, what the agents' actions are for (`portraits.USES`:
  their calls grouped into exploring, editing and testing, building, running programs, Slurm and git, leaving
  out waiting and unrecorded calls, and naming the uses that hold most of them), how much time is spent
  waiting, the scheduler, what the agents leave behind in their own words, what they work on, and when. The
  service decides each part using `RULES`, and the page puts it into words: one sentence under each user in
  the roster, and the same sentence, point by point, at the top of their portrait.
- **A figure of each user** draws the same row (`web/src/components/PortraitFigure.tsx`). It shows a person at
  a desk, drawn the same for everyone (no one's picture is stored, and the names are aliases), surrounded by
  one element per number: a robot for each session open at once, coloured by agent, with its antenna lit while
  it runs; the laptop's emblem for where most sessions are driven from, with the person leaning back when that
  is scripts; a padlock for launches that bypassed approval, a shield for auto mode, and capsules for the
  sandboxed trait; a shelf showing what the actions are for, with each icon's area matching its share; a
  24-hour clock of active minutes; the moon for the overnight trait; papers for what the agents wrote; a mug or
  a stopwatch for long or short sessions; an hourglass for polling; and a server with a light for each Slurm
  job. Hovering over an element shows its numbers and the rule behind it, and a key explains what each one
  means. The only numbers the page adds are drawing caps (six robots, five sheets, ten lights), each noted
  where it applies. A call count that the service could not compute is drawn as unread, never as an empty
  shelf. The figure is the first thing in the portrait, and the roster shows everyone's figures side by side
  (*figures*). `npm run check:figures` renders every fixture user's figure and checks it against their row.
- **Traits** are plain statements ("launches with a bypass flag", "keeps several sessions open at once"),
  each derived from one rule over the numbers above (`portraits.RULES`, `portraits.TRAITS`). The rules are
  served with the traits and shown on hover, and each trait carries the numbers that met its rule. No trait is
  a judgement, none is made by a model, and a user may have none.
- **The work is read from commands**, like everything in the session inspector. An agent's own Read and Edit
  tools, and its thinking, leave no command, so an agent that edits only through its own tools shows no
  editing.
- **One user's portrait reads the phase blocks** of their newest `PHASE_SESSIONS` (80) sessions (the same
  blocks, from the same cache, that the rows of the Agents tab's list of sessions show), and lists at most 60
  sessions and 40 tasks; the rest are
  counted. Under a user filter, the portrait is compared against all of the window's users when that view is
  held (`fleet_of`).

Cost: the roster walks every folded session and its call log once, and the sandbox's verdicts walk every
tool call of the window, which is most of the cost. The aggregator holds each view's inputs
(`portraits.Context`), its roster and each open portrait, and rebuilds them once the view has ingested new
records and `reducers.trajectories.live_s` has passed, just as the ranked sessions are re-ranked. On 500
synthetic sessions with 100 000 calls, the roster took about 0.3 s, and a portrait read from the roster's
inputs took about 20 ms.

## Slurm: how sessions use the scheduler

"An agent submitted job N" says nothing about agent behaviour; how agents drive the
scheduler does. On boslogin06's 2026-10-09 agents made 16.6 scheduler queries per job
against 0.2 for people, 41 agent sessions polled the queue for 10 minutes or more (94.9 h
of it; one Claude session every 4.9 s for 4.9 h), one session submitted 112 jobs in a
minute, and 79% of agent submissions used `--parsable` (people: 0.2%) -- a program reading
the job id. The **HPC** tab (`?tab=slurm`, or `?tab=hpc`; an entry of its own in the rail,
hidden by `slurm.enabled: false`) is built on that, from the window's `slurm` reducer
(`reducers/slurm.py`):

- **abnormal activity** -- every session that broke a rule, each rule the session's own count
  against a threshold in `reducers.slurm.abnormal`:

  | rule | breaks it | setting (default) |
  |---|---|---|
  | polling the queue fast | its longest stretch of polls each at most `poll_gap_s` after the last lasts `poll_min_s` or more | `poll_gap_s` (30 s), `poll_min_s` (1800 s) |
  | a burst of queue checks | the most queue checks in any 60 s | `query_burst` (30) |
  | a burst of submissions | the most jobs submitted in any 60 s (an array is one submission) | `submit_burst` (20) |
  | refused again and again | submissions the scheduler refused | `refused` (3) |
  | cancelling again and again | `scancel` calls | `cancels` (10) |
  | runs failing again and again | `srun`/`salloc` runs that ended non-zero or on a signal | `failed_runs` (5) |

  The polling loop below -- polls at most 3 min apart for 10 min or more -- is how an agent
  waits for its own job, and it describes a session without flagging it; fast polling, every
  30 s or less for half an hour by default, is the alarm (one Claude session polled every 5 s
  for 5 h). "In any 60 s" is a sliding window, not a clock minute, which would call 29 checks
  before a minute turns and 29 after it 29. Each setting a count of 1 or more (seconds for the
  two polling bounds), or the service refuses to start. The service serves the rules with
  their thresholds, so the page words them and holds none of its own; each class counts its
  sessions per rule, zero included, and the page's count of flagged sessions is those totals;
  and every session that broke a rule is served past the busiest 200 however quiet it was, up
  to 500 more, the rest counted. A session's busiest minute of queue checks, or of jobs, is a
  lower bound for a key past 50,000 of them, whose later ones have no time;

- **per session** -- an agent's collector keys folded into the agent, as the list of sessions folds
  them; people and IDEs one row per user: jobs, refusals, the busiest minute, distinct
  command lines (numbers masked: one script resubmitted is one), the flags it submits with,
  queries, its longest **polling loop** (queries within 2 s are one poll, polls within 3 min
  one loop, 10 min or more counts as polling), queries per job, cancels, GPU-hours asked;
- **per class**, the same as rates and shares side by side;
- **what came back** from each submission: a job id, a dry run (`--test-only` makes no job),
  a step into an allocation already held (`srun --jobid`/`--overlap`), or a **refusal** --
  no id, a non-zero exit and the scheduler's own error line, which is the reason shown
  (`QOSMaxSubmitJobPerUserLimit`, a partition rule). An `srun` whose program ran and failed
  is not a refusal, and neither is srun's own line reporting that (`task 0: Exited with exit code 1`).
  An srun or salloc only counts as refused when it cannot get the allocation. The refusal count
  is a lower bound. A submission keeps only its last 300 characters, so a refusal line whose start
  was cut off (a reason of about 200 characters, since Slurm prints that line last) is not counted;
- queries and jobs per time bin, by class. A bin with no Slurm record of any class is shown as a
  gap, not 0.

Requests are read from argv, so a `#SBATCH` line in the script is not seen. As a result,
GPU-hours only count jobs with GPUs and a time limit on the command line, and the tab says how
many jobs that was.

## Known collector limitations the page surfaces

- **`peak_rss_mb` is 0 on every exit in older captures**: the kernel runs `exit_mm()` (clearing
  `task->mm`) before the `sched_process_exit` tracepoint fires, so a collector that read
  `mm->hiwater_rss` never got a value. The collector now reads `task->signal->maxrss`, which
  survives `exit_mm()`; a field that is 0 on every record of a feed renders **not measured**
  rather than as a measured zero (`reducers/__init__.py`, `all_zero`).
- **Through schema 7 the collector's session key split one session per tool call.** It keyed
  a process on its nearest classifier-tagged ancestor, and the classifier tagged anything
  whose argv mentioned `~/.claude/`, `~/.codex/` or `~/.vscode-server/` -- including the shell
  every Claude Code Bash tool call runs in -- until it read the program alone (collector
  README, *Agent tags*). Schema 8 starts a session only at an agent runtime. For the older
  captures the dashboard decides what each tagged process really is (`roles.py`) and folds
  each key into the agent above it (`procindex.py`); trajectory rows say how many keys they
  fold and how the least certain one was attributed, and the Sandbox tab attributes tool
  calls to sessions through the same fold. On madsys-gpu1 a 24 h window went from 3 281 keys
  to 636 sessions, one VS Code server alone from 688.
- **`submit` request fields are argv-only.** A job whose `#SBATCH` directives carry the
  request reports `null` for `partition`/`gpus`/`array`; `req_src` says which. Recovering them
  needs a `slurm_jobs` census join, which is not a configured feed.
- **Scheduler and GPU panels are absent, not zero.** `running_agent_jobs` and GPU duty need
  the census and `dcgm`. The HPC tab shows the *latest submissions* in the window from eBPF
  `submit` records instead — per-submission and actor-attributed, which is better evidence
  than a census count.

## Layout

| path | role |
|---|---|
| `rc_dashboard/__main__.py` | the CLI: `serve` (the default), `demo`, `check-feeds`, `print-config` (also as `--check-feeds`, `--print-config`) and `new-password` (`--force` to replace a password, `--dir`, `--netrc-host`; [Password](#password)) |
| `rc_dashboard/config.py` | per-key precedence, candidate probing, `--print-config` |
| `rc_dashboard/feeds.py` | resolve + report every feed; ports `healthcheck.check_target` / `last_timestamp` |
| `rc_dashboard/feedfile.py` | the collector's day-files: listing them (a plain file before its `.gz`), the binary searches by time (`seek_to_epoch`, `end_at`), where the last complete line ends, the head digest |
| `rc_dashboard/fastjson.py` | the one JSON parser for every line read off the collector's files |
| `rc_dashboard/history.py` | the history store: every sealed day-file kept as Parquet, converted in the background ([History in Parquet](#history-in-parquet)) |
| `rc_dashboard/keysearch.py` | the session inspector's key search with its records built in worker processes, replayed with the one-process read's counters |
| `rc_dashboard/replica.py` | a sealed day-file read from its Parquet tables as the file itself: its offsets, time searches and records |
| `rc_dashboard/where.py` | the filters a replica rejects records by before it builds them |
| `rc_dashboard/feedparquet.py` | the Parquet schema, the converter and its verifier ([The feed as Parquet tables](#the-feed-as-parquet-tables)) |
| `rc_dashboard/tail.py` | `IngestSource`; the file tail, and the newest-first read-back that refills retention on every start (a push source drops in here) |
| `rc_dashboard/aggregator.py` | one streaming pass, many reducers: the live feed's dispatch by event type, and the background tasks that feed it |
| `rc_dashboard/roster.py` | the live tier's per-user roster: who ran what, cheap at any number of users |
| `rc_dashboard/gz.py` | a `.jsonl.gz` day-file read in place, seekable by uncompressed offset through a per-process inflater index |
| `rc_dashboard/health.py` | each collector's verdict, from its own meta / heartbeat / stop records |
| `rc_dashboard/clock.py` | the service's present; `clock.as_of` pins (and freezes) it for a captured feed |
| `rc_dashboard/normalize.py` | schema contract, `actor3`, sandbox/approval, session keys, null gating |
| `rc_dashboard/roles.py` | what a classifier-tagged process is: agent, tool-shell, probe, sandbox, infra, ... |
| `rc_dashboard/procindex.py` | per-window index of tagged processes; folds session keys into their agent |
| `rc_dashboard/buckets.py` | rolling per-minute aggregates; exact sub-window slicing |
| `rc_dashboard/nodehist.py` | the node tier over time: every snapshot's probes, load and NFS round trips, read incrementally on its own thread and binned to a window ([Login node health](#login-node-health)) |
| `rc_dashboard/reducers/` | tools, io, resources, risk, sandbox, trajectories, cascades (agent trees), workspace (where each agent worked), slurm (sessions at the scheduler), facets (the page filters' choices), own_io (each process's own disk bytes, its reaped children's taken off), live |
| `rc_dashboard/reducers/phase_*.py` | an agent's tool calls named (`phase_rules`) and logged (`phase_log`), cut into phases (`phase_segment`) and into tasks (`phase_tasks`), the names and the agent's own words the tasks are labeled from (`phase_names`), and a session at a glance, its kind of work and a few sentences (`phase_summary`) |
| `rc_dashboard/tool_buckets.py` | the kinds of tool a command is bucketed into, vendored from the measurement repo's `analyze/tool_bucket_lib.py` |
| `rc_dashboard/phase_report.py` | the report the phase and task thresholds are tuned against |
| `rc_dashboard/portraits.py` | the Users tab: each user's portrait, composed from a view's trajectories, sandbox and Slurm reducers when read; the traits and their rules |
| `rc_dashboard/windows.py` | window clamp rule, per-window reducer sets, the LRU + build worker |
| `rc_dashboard/hours.py` | hourly merge-reduce: each record reduced once into its hour's saved state, a window merged from them, and the node split's worker processes ([Hourly merge-reduce](#hourly-merge-reduce)) |
| `rc_dashboard/snapshots.py` | saved window builds: fingerprint, capture/restore, the 0600 store |
| `rc_dashboard/payloads.py` | each window's panels as last served, saved so a new view can show them at once |
| `rc_dashboard/proctree.py` | one session as a process tree, read on demand: what the session inspector draws |
| `rc_dashboard/annotate.py` | what each tool call was for, in a model's words: the inspector's "annotate purposes" |
| `rc_dashboard/iocmds.py` | every command behind an I/O figure, looked up when it is clicked |
| `rc_dashboard/endpoints.py` | the far ends of the network bytes, named and ranked when the panel is read ([Network endpoints](#network-endpoints)) |
| `rc_dashboard/export.py` | shared filter grammar, streaming export, the windowed re-scan (over `feedfile.py`, whose names it re-exports) |
| `rc_dashboard/app.py` | FastAPI: REST, WebSocket, static mount |
| `rc_dashboard/auth.py` | sign-in for the whole service: one password, kept as a salted scrypt hash ([Password](#password)) |
| `rc_dashboard/guard.py` | who may talk to the service at all: a websocket's Origin, and every request's Host while it has no password |
| `rc_dashboard/redact.py` | credentials masked in command lines before anything is served |
| `rc_dashboard/anonymize.py` | stable aliases for the names in everything the service sends out ([Anonymized names](#anonymized-names)) |
| `rc_dashboard/jsonout.py` | JSON out: serialized with orjson on the request's worker thread, gzipped when the reader takes it |
| `rc_dashboard/gcwatch.py` | how long Python's garbage collector holds the process, and keeping it short |
| `rc_dashboard/ask.py` | a question about the window on screen, answered by an agent CLI on this host ([Ask LLM](#ask-llm)) |
| `rc_dashboard/demo/` | demo mode: the synthetic feed (`world` plans and builds it, `cast` the people, `sessions`/`agents` what each runs, `host` each node's own records, `records` the records themselves as the collector writes them, `feed` the files), and the overrides that point the service at it |
| `web/` | React + Vite + TS; `dist/` is what the service serves |
| `bench/` | a synthetic feed at fleet density, timers, a profiler, and the guard that the main served answers stayed the same ([bench/README.md](bench/README.md)) |

## Ingestion, precisely

**What the page holds is a function of the collector's files and the clock — never of when this
service was running.** Nothing in the live tier is restored from a previous run (the historical
windows resume from saved builds, but only from ones that are provably a prefix of the same files —
see *The time window*). On every start the service hands
each day-file to the tail at the end of its last complete line, then reads the history
behind that point back into the live tier: `min(live.backfill_hours, live.retention_min)` of it,
located by the same timestamp binary search the window rebuild uses. Offsets used to be
persisted while the bins they fed were not, so a restart resumed at EOF with empty bins and the
page showed only what the collector wrote after the service came back — and `run.sh` restarts
the service on every save.

The read-back runs **newest first**, k-way merged across hosts, while the tail is already
polling. A poll that lands before the hand-off reads nothing, so no record is counted twice. The
page is live within a second, and you can watch the chart fill from now backwards. The filled span
is always contiguous, so the picker's `filled 5h of 7d` stays accurate next to `reading history · N%`.
The read takes the aggregation lock one small batch at a time, so neither the page nor the tail
has to wait behind it. A week of one login node's feed (4 GB, 2.4 M records) takes 35–45 s here.
Bins are sums and accept records in any order. The "latest" state (each host's plate, the event
and submission tails) only accepts an older record if nothing newer has already claimed the slot.

`DailyWriter.write()` (`collector/ebpf_trace.py`) buffers and flushes once per poll round, which
is not atomic with respect to a concurrent reader. So the tailer reads to EOF, finds the last
newline, yields only complete lines, and **advances its stored offset only that far**. Any
trailing bytes are re-read on the next cycle. This is idempotent, never yields a truncated JSON
line, and never loses a record. The startup hand-off follows the
same rule, so a line that is still being written belongs to the tail, never to the read-back.

**One bad line stops nothing.** Every reader of the day-files counts a line that is JSON but not
an object (`0`, `null`, `[]`) along with the lines that do not parse (`fastjson.record`). A record
the normalizer cannot read (a field of a type no collector writes, a list `args` or `event`, a
number for `ts`) is skipped and counted, and so is a record that a held view cannot take.
`/api/feeds` reports how many in the eBPF tier's `tail`: `parse_errors`, `malformed`,
`feed_errors`. A single `null` used to make every poll raise an exception. The tail's offsets
stayed put, the other hosts were never read, and the batch already handed over was counted again
at every poll. A `[]` already in a file stopped the backfill, and such a record made every build
of a window that contained it fail.

Offsets are keyed on `(st_dev, st_ino)`, not path, so a recreated file is detected as a new inode
instead of being silently seeked past its end. A file first seen after startup (a new date
directory, a new host) is read from byte 0, at most 64 MB per poll.
