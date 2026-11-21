# The feed as Parquet tables

**Status: the dashboard's history store.** `rc_dashboard.feedparquet` writes a typed,
columnar copy of the eBPF tier's day-files and reads every record back from it exactly.
The dashboard keeps such a copy of every sealed day-file and answers selective reads and time
searches of the file from it, at the file's own offsets
([History in Parquet](../../dashboard/README.md#history-in-parquet)). It is the columnar copy
of sealed days that [schema 9's proposal](../feed-format-v9/README.md) left open, built for the
feed as it is, with no change to the collector: schema 7 (FAS's login nodes) and schema 8 (this
repo's collector) both convert.

Every number here comes from the FAS mirror (8 login nodes, the 7 sealed days
2026-11-09 to 2026-11-15) unless noted otherwise. The data names real users and
commands, so this page gives aggregates only.

## Running it

The dashboard converts the sealed days it reads on its own (`history.py`). By hand, on a capture
of your own:

```bash
cd dashboard
uv run python -m rc_dashboard.feedparquet convert --out /path/to/pq ROOT...   # the roots feeds.ebpf.roots lists
uv run python -m rc_dashboard.feedparquet verify  --out /path/to/pq ROOT...   # every record, rebuilt and compared
uv run python -m rc_dashboard.feedparquet schema --markdown
```

- It reads what the dashboard reads (`feedfile`): `.jsonl`, plain or `.gz`, a plain file before
  its `.gz`. Today's day-files, still being written, are skipped unless `--include-today`.
- A day-file is skipped while its manifest is this version's, names the source as it is (name,
  size, mtime, inode), and every table it names is still the size it was written, so a cron job
  can run it every night; otherwise, or with `--force`, it is converted again. `--days N` and
  `--day D` narrow it, and `--jobs` sets the processes (one per day-file).
- Output is written beside its final name and renamed into place, files `0640` and directories
  `0750`, as the collector writes the day-files: the tables hold what those hold.

### Layout

```
pq/<table>/day=<YYYY-MM-DD>/<host>.<stream>.parquet
pq/_manifest/day=<YYYY-MM-DD>/<host>.<stream>.json   the source (name, size, mtime, inode);
                                                     its inflated size and complete end, its
                                                     first 4 KB, where each line that is not
                                                     a record lies (blank, bad, not an
                                                     object); records per table, seconds
```

Each table is a directory and `day` a Hive partition. A file holds one source day-file's records
for that table, so each sealed file's copy is made, checked and replaced on its own. Every row
carries `_off`, the byte offset of its line in the uncompressed day-file (delta-packed, under a
byte a row), so a row can be found in the file and a range of the file in the table: this is how
the dashboard reads a sealed file from its copy at the file's own offsets (`replica.py`). Every
file also carries its host (`host`) and provenance (the footer's `rcfeed` entry), so it stands
alone. A day leaves the copy as it leaves the feed: delete its directories.

```sql
-- DuckDB
SELECT agent_type, count(*), sum(cpu_s)
FROM read_parquet('pq/exit/*/*.parquet', hive_partitioning = true)
WHERE day >= '2026-11-12' AND actor3 = 'agent'
GROUP BY ALL;
```

```python
from rc_dashboard import feedparquet      # the records themselves, exactly as the day-file had them
for rec in feedparquet.records("pq/exit/day=2026-11-15/<host>.exits.parquet"):
    ...
```

## The schema

One table per record kind: `exit` (with `truncated`), `conn`, `netio`, `residency`,
`residency_totals`, `submit`, `meta`, `stop`, and `other` for an event kind no table names (a
pre-6 `tcp`, a future `session`). Records are routed by `event`, not by stream, so a capture from
before the exits/snapshot split reads the same. A table's columns are the keys of its records,
in the collector's order, with types taken from the code that writes them:

- **Integers** are int32 where the kernel bounds them (pids, ports, exit codes, signals,
  depths, a process's threads, `args_len`) and int64 everywhere else.
- **Floats** are float64, exactly what the collector wrote: it rounds at the source
  (`duration_s` to 4 places, `peak_rss_mb` to 2).
- **`ts` and `start`** are local, naive strings with one-second granularity, stored as
  timestamps with no zone (Parquet's `isAdjustedToUTC=false`), in `ms` because Parquet has no
  seconds unit. **`ts_epoch`** stays a float: it is the authoritative time, as in the feed.
- **Nested blocks keep their shape**: structs, lists of structs, and maps, in their own order,
  for objects whose keys are data. The blocks of `meta` and `stop` with no fixed shape
  (`config`, `btf_offs`, `output`, `unsafe_dirs`) are JSON text.
- **Null means what it means in the feed** (not measured, not applicable), never "this
  record had no such key": that is `_absent`.

### `_absent` and `_extra`

Two columns at the end of every table make the copy exact without a registry of past schemas:

- **`_absent`** lists the keys of the record's event that this record did not have, and is
  null when it had them all. A schema-7 record lacks `args_redacted`, so its `_absent` reads
  `["args_redacted"]`, and a reader can tell "no such field then" from "nothing was masked".
  For the few keys written only when they hold something (`netio.overflow`,
  `net_endpoints.overflow` and `.unaccounted`, `conns.peers_overflow`, and the demo's
  `synthetic`), absent and null mean the same, and nothing is recorded.
- **`_extra`** is a JSON object holding whatever a column cannot hold exactly: a key no table
  names (schema 9's `pk`, pre-7's `comm`), a value of another type than its column's (an int
  where a float is written, since `1` would read back as `1.0`), a value out of range, text that
  is not UTF-8, a time string in another format, or a nested block of another shape. It is null
  on every row that has none of these.

`feedparquet.records()` rebuilds a record from a row: the event's keys in order, minus those
`_absent` names, each from its column or from `_extra`, then any key in `_extra` that no table
names.

### Columns

Generated by `python -m rc_dashboard.feedparquet schema --markdown`.

#### Envelope: every table

| column | type |
|---|---|
| `ts` | `timestamp[ms] (local, no zone)` |
| `ts_epoch` | `double` |
| `host` | `string` |
| `event` | `string` |
| `collector_pid` | `int32` |
| `schema_version` | `int32` |
| `synthetic` | `bool` (written only when set) |

#### Identity: `exit`, `conn`, `netio`, `residency`, `submit`

| column | type |
|---|---|
| `actor` | `string` |
| `actor3` | `string` |
| `attribution` | `string` |
| `pid` | `int32` |
| `ppid` | `int32` |
| `uid` | `int64` |
| `user` | `string` |
| `tty` | `string` |
| `command` | `string` |
| `args` | `string` |
| `args_redacted` | `list<item: string>` |
| `argv_source` | `string` |
| `args_len` | `int32` |
| `args_truncated` | `bool` |
| `cwd` | `string` |
| `sandbox` | `string` |
| `sandbox_src` | `string` |
| `sandbox_detail` | `string` |
| `sandbox_ancestry` | `string` |
| `approval_mode` | `string` |
| `approval_src` | `string` |
| `agent_type` | `string` |
| `agent_pid` | `int32` |
| `session_key` | `string` |
| `session_uuid` | `string` |
| `tree_root_pid` | `int32` |
| `depth` | `int32` |
| `is_agent` | `bool` |
| `autonomous` | `bool` |
| `cgroup` | `string` |

#### `exit`

One row per process end: `exit` at exit, `truncated` for one still alive at the collector's stop (counters from /proc). Columns: the envelope and the identity, then

(`rss_mb` is a `truncated` row's; `fork_only`, `cpu_pct`, `peak_rss_mb`, `cmaj_flt`, `nvcsw`, `nivcsw`, `sched_*`, the delay waits, `pgrp` and `sid` are an `exit` row's)

| column | type |
|---|---|
| `fork_only` | `bool` |
| `start` | `timestamp[ms] (local, no zone)` |
| `duration_s` | `double` |
| `duration_src` | `string` |
| `cpu_s` | `double` |
| `cpu_scope` | `string` |
| `child_cpu_s` | `double` |
| `cpu_pct` | `double` |
| `peak_rss_mb` | `double` |
| `threads` | `int32` |
| `min_flt` | `int64` |
| `maj_flt` | `int64` |
| `cmaj_flt` | `int64` |
| `nvcsw` | `int64` |
| `nivcsw` | `int64` |
| `sched_run_s` | `double` |
| `sched_wait_s` | `double` |
| `blkio_wait_s` | `double` |
| `swapin_wait_s` | `double` |
| `freepages_wait_s` | `double` |
| `dstate_wait_s` | `double` |
| `dstate_episodes` | `int64` |
| `dstate_max_s` | `double` |
| `dstate_src` | `string` |
| `net_tx_bytes` | `int64` |
| `net_rx_bytes` | `int64` |
| `net_calls` | `int64` |
| `pgrp` | `int32` |
| `sid` | `int32` |
| `exit_code` | `int32` |
| `signal` | `int32` |
| `core_dumped` | `bool` |
| `io` | `struct<rd_mb: double, wr_mb: double, rchar_mb: double, wchar_mb: double, scope: string>` |
| `samples` | always null: no column |
| `state_last` | `string` |
| `conns` | `struct<n: int64, accepted: int64, established: int64, failed: int64, external: int64, rx_bytes: int64, tx_bytes: int64, conn_s_total: double, connect_ms_p50: double, connect_ms_max: double, providers: map<string, int64>, peers: list<item: struct<daddr: string, dport: int32, n: int64, external: bool, provider: string, inbound: int64, failed: int64, rx_bytes: int64, tx_bytes: int64>>, peers_total: int64, peers_overflow: int64>` |
| `net_endpoints` | `struct<n: int64, in_series: bool, endpoints: list<item: struct<proto: string, addr: string, port: int32, host: string, inbound: bool, external: bool, provider: string, tx_bytes: int64, rx_bytes: int64, calls: int64>>, unattributed: struct<tx_bytes: int64, rx_bytes: int64, calls: int64>, overflow: struct<n: int64, tx_bytes: int64, rx_bytes: int64, calls: int64>, unaccounted: struct<tx_bytes: int64, rx_bytes: int64>>` |
| `rss_mb` | `double` |

#### `conn`

One row per standing external socket per residency tick. Columns: the envelope and the identity, then

| column | type |
|---|---|
| `family` | `string` |
| `proto` | `string` |
| `saddr` | `string` |
| `sport` | `int32` |
| `daddr` | `string` |
| `dport` | `int32` |
| `external` | `bool` |
| `provider` | `string` |
| `inbound` | `bool` |
| `age_s` | `double` |
| `established` | `bool` |
| `stats_src` | `string` |
| `state` | `string` |
| `rtt_ms` | `double` |
| `retrans` | `int64` |
| `rx_bytes` | `int64` |
| `tx_bytes` | `int64` |
| `idle_send_s` | `double` |
| `idle_recv_s` | `double` |
| `segs_in` | `int64` |
| `segs_out` | `int64` |
| `rqueue` | `int64` |
| `wqueue` | `int64` |

#### `netio`

One row per process that moved bytes, per tick: bytes per endpoint since its previous netio. Columns: the envelope and the identity, then

| column | type |
|---|---|
| `reason` | `string` |
| `interval_s` | `double` |
| `endpoints` | `list<item: struct<proto: string, addr: string, port: int32, host: string, inbound: bool, external: bool, provider: string, tx_bytes: int64, rx_bytes: int64, calls: int64>>` |
| `unattributed` | `struct<tx_bytes: int64, rx_bytes: int64, calls: int64>` |
| `overflow` | `struct<n: int64, tx_bytes: int64, rx_bytes: int64, calls: int64>` (written only when set) |

#### `residency`

One row per live agent tree per tick. Columns: the envelope and the identity, then

| column | type |
|---|---|
| `root_alive` | `bool` |
| `age_s` | `double` |
| `headless` | `bool` |
| `n_procs` | `int64` |
| `threads` | `int64` |
| `rss_mb` | `double` |
| `cpu_s` | `double` |
| `d_state` | `int64` |
| `max_depth` | `int32` |
| `agent_types` | `list<item: string>` |
| `by_command` | `map<string, int64>` |
| `d_stack` | `map<string, int64>` |
| `state_counts` | `map<string, int64>` |
| `top_procs` | `list<item: struct<pid: int32, command: string, rss_mb: double, cpu_s: double, cpu_delta_s: double, state: string, threads: int32>>` |
| `tcp_open_ext` | `int64` |
| `tcp_open` | `int64` |

#### `residency_totals`

One row per host per tick: census, loss and health counters. Columns: the envelope, then

| column | type |
|---|---|
| `interval_s` | `double` |
| `tracked_live` | `int64` |
| `trees` | `int64` |
| `conn_records` | `int64` |
| `by_actor3` | `map<string, struct<n_procs: int64, threads: int64, rss_mb: double, d_state: int64, users: int64, states: map<string, int64>>>` |
| `by_agent_type` | `map<string, struct<trees: int64, n_procs: int64, threads: int64, rss_mb: double, cpu_s: double, d_state: int64, users: int64, autonomous: int64>>` |
| `dropped` | `struct<n: int64, by_command: map<string, int64>>` |
| `tcp_open_ext_by_actor3` | `map<string, struct<open: int64, ext: int64, estab: int64>>` |
| `conn_unattributed` | `int64` |
| `conn_unmatched_births` | `int64` |
| `fork_total` | `int64` |
| `fork_rate` | `double` |
| `d_stack_top` | `map<string, int64>` |
| `d_stacks_read` | `int64` |
| `netio_records` | `int64` |
| `netpeer_entries` | `int64` |
| `netpeer_full` | `struct<calls: int64, tx_bytes: int64, rx_bytes: int64>` |
| `netpeer_orphans` | `struct<entries: int64, tx_bytes: int64, rx_bytes: int64, calls: int64>` |
| `netpeer_kernel` | `struct<calls: int64, tx_bytes: int64, rx_bytes: int64>` |
| `perf_seen` | `map<string, int64>` |
| `perf_lost` | `map<string, int64>` |
| `handler_errors` | `map<string, int64>` |
| `proc_entries` | `int64` |
| `proc_vanished` | `int64` |
| `proc_late_exits` | `int64` |
| `thread_acc` | `struct<taken: int64, late: int64, missing: int64, stale: int64, swept: int64, full: int64>` |

#### `submit`

One row per sbatch/salloc/srun: the request and the job id. Columns: the envelope and the identity, then

| column | type |
|---|---|
| `partition` | `string` |
| `gpus` | `int64` |
| `array` | `string` |
| `time_limit_s` | `int64` |
| `mem` | `string` |
| `cpus_per_task` | `int64` |
| `req_src` | `string` |
| `tool` | `string` |
| `job_id` | `int64` |
| `job_ids` | `list<item: int64>` |
| `work_dir` | `string` |
| `exit_code` | `int32` |
| `signal` | `int32` |
| `start` | `timestamp[ms] (local, no zone)` |
| `duration_s` | `double` |
| `captured_bytes` | `int64` |
| `captured_tail` | `string` |

#### `meta`

One row per collector start: kernel, features, settings. Columns: the envelope, then

| column | type |
|---|---|
| `kernel` | `string` |
| `bcc` | `string` |
| `euid` | `int64` |
| `tz` | `string` |
| `boot_id` | `string` |
| `features` | `list<item: string>` |
| `features_rejected` | `map<string, string>` |
| `cpu_scope` | `string` |
| `counters_scope` | `string` |
| `redact` | `list<item: string>` |
| `argv_source_default` | `string` |
| `dstate_src_default` | `string` |
| `btf_offs` | `string (JSON text)` |
| `sysctl` | `map<string, string>` |
| `cgroup` | `string` |
| `netlink_sockdiag` | `bool` |
| `unsafe_dirs` | `string (JSON text)` |
| `config` | `string (JSON text)` |
| `seeded_pids` | `int64` |
| `output` | `string (JSON text)` |

#### `stop`

One row per collector stop: the run's totals. Columns: the envelope, then

| column | type |
|---|---|
| `uptime_s` | `double` |
| `records` | `int64` |
| `truncated` | `int64` |
| `perf_lost` | `map<string, int64>` |
| `perf_seen` | `map<string, int64>` |
| `handler_errors` | `map<string, int64>` |
| `proc_vanished` | `int64` |
| `proc_late_exits` | `int64` |
| `flush_mode` | `string` |
| `flushes` | `int64` |
| `conns_dropped` | `int64` |
| `conns_dropped_commands` | `map<string, int64>` |
| `netio_records` | `int64` |
| `netpeer_full` | `struct<calls: int64, tx_bytes: int64, rx_bytes: int64>` |
| `netpeer_orphans` | `struct<entries: int64, tx_bytes: int64, rx_bytes: int64, calls: int64>` |
| `argv_hosts` | `struct<resolved: int64, failed: int64, dropped: int64>` |
| `reason` | `string` |
| `output` | `string (JSON text)` |

#### `other`

An event kind no other table names: the envelope, the rest in _extra. Columns: the envelope, then

(nothing else: every other key goes to `_extra`)

#### Every table ends with

| column | type |
|---|---|
| `_off` | `int64` |
| `_absent` | `list<string>` |
| `_extra` | `string (JSON object)` |

## What the FAS feed looks like

From profiling every field of every record of 2026-11-15 (8 hosts, 21.6 M records,
45.1 GB of JSON):

| event | records | bytes per record (JSON) |
|---|---:|---:|
| `exit` | 17,142,727 | 2,215 |
| `conn` | 2,138,107 | 1,495 |
| `netio` | 1,675,063 | 1,640 |
| `residency` | 660,883 | 1,693 |
| `submit` | 18,316 | 1,742 |
| `residency_totals` | 11,515 | 1,970 |

- **Every host writes schema 7**, and every event has exactly one key order. In the 7
  days there is one `meta` record (a collector restart) and no `stop` or `truncated`
  record. Schema 8's shapes come from the demo generator, whose output the tests convert
  too.
- **No field mixes ints and floats**, and no line fails to parse. The only keys ever
  absent are ones written only when set.
- **Some columns are constant on FAS** (`pgrp` and `sid` 0, `swapin_wait_s` 0.0,
  `fork_only` false, `conns.accepted` 0, `cpu_scope` `process`, `dstate_src` `stat`).
  Dictionary pages make them free, and they vary elsewhere, so they stay.
- **Ranges that set types**: pids reach 4,194,303, a socket's `rx_bytes` 1.66 TB, `args`
  119,710 characters (schema 7 does not cap it), `args_len` 412,675 and `min_flt` 704 M, and
  endpoint lists reach the cap of 256 entries.
- **Values to know about**: `duration_s` can be negative (-0.0083); `conn.age_s` is sometimes
  an epoch, 1.79e9 (schema 7's wall-clock `age_s` on EL8's Python 3.6); `netio.interval_s` and
  `residency.age_s` reach -3,600 s. The copy keeps all of these as written.

## Measured

### As the dashboard's store (version 2)

The dashboard's history store (`history.py`: a file per day-file, 128 Ki-row groups,
8 processes) converted the 7 sealed days 2026-11-10 to 2026-11-16 of the same 8 hosts
(112 day-files, 18.39 GB of `.gz`, 182,151,599 records) in 701 s, 260 k records a second.
The result was 6.22 GB in 337 files, 33.8 % of the `.gz`. A process held 0.66 to 0.80 GB
resident (measured in a running service mid-conversion).

The reads the dashboard makes from the store, each in a fresh process, against the same reads
of the `.gz`, with the same answers both ways:

| | from the `.gz` | from the replicas |
|---|---:|---:|
| one user's exits on one sealed day, 500 rows, either order (the week's 128 day-files planned, as `/api/events` plans them) | 222-444 s | 13.6-15.4 s |
| a session's key search through the busiest day-file (17.4 GB inflated) | 30-43 s | 2.8 s |
| a time search in that file: the first, then each after | 12.3 s, then 0.1 s | 0.17 s, then 0.06 ms |

The `.gz` side inflates each file it places in full, to learn its size and build its
seek index. For the events query, that is the whole week: 367 GB.

### Version 1

All 56 sealed host-days of the mirror (8 hosts, 2026-11-09 to 2026-11-15, 179,448,351
records), converted with a file per host-day and 1 Mi-row groups.

#### Size

| | `.jsonl.gz` (the sealed feed) | Parquet | |
|---|---:|---:|---:|
| exits stream, to `exit` | 13.24 GB | 5.09 GB | 38.5 % |
| snapshot stream, to the other tables | 5.07 GB | 1.02 GB | 20.0 % |
| **all** | **18.32 GB** | **6.11 GB** | **33.4 %** |

Compared with the JSON itself, 2026-11-15 is 45.1 GB as written, 2.19 GB gzipped and
694 MB as Parquet, 1.5 % of the JSON. Every day's Parquet lands between 31.7 % and 34.0 % of
its gzip.

| table | rows | Parquet | per row |
|---|---:|---:|---:|
| `exit` | 144,517,876 | 5.09 GB | 35 B |
| `conn` | 15,839,600 | 481 MB | 30 B |
| `netio` | 13,463,228 | 334 MB | 25 B |
| `residency` | 5,397,194 | 174 MB | 32 B |
| `submit` | 149,849 | 12.7 MB | 85 B |
| `residency_totals` | 80,603 | 13.7 MB | 170 B |
| `meta` | 1 | 13 KB | |

Where the bytes go, on boslogin06's 2026-11-15 (2.0 M exits):

- **`exit`**: `net_endpoints` 16 % (schema 7 still counts the NFS client's RPCs as the
  calling process's traffic, so most exits list NFS servers), `args` 12 %, `ts_epoch`
  7 %, `conns` 6 %, `ppid` 5.5 %, `duration_s` 4 %, `pid` 4 %. The 30-column identity
  block is 36 % of the file, 22 points of it `args`, `pid` and `ppid`, which are specific to
  each process; the other 27 columns, half of an `exit` line in JSON, take 15 % here.
- **`netio`**: the endpoint lists 55 %; identity 35 % (15 % without `args`/`pid`/`ppid`).
- **`conn`**: its socket counters, each 5-11 %; identity 24 %.
- **`residency`**: `top_procs` 49 %, `age_s` 14 %, `cpu_s` 12 %; identity 6 %.

#### Converting

Conversion costs 31 CPU-µs per record: parsing the JSON 12, building rows 8.5, building
Arrow arrays 8-9.5, gunzip 3, writing 2.5 (300,000 `exit` records, Python 3.12, orjson
3.13, pyarrow 26). The week took 7.4 minutes with 16 processes (`--jobs 16`), one
host-day per process, with the largest process using 3.6 GB.

#### Exact

`verify` read every record of the 56 host-days from both its day-file and its table, and
compared the two: all 179,448,351 were the same, type for type, each in its own key order, with
0 rows needing `_extra` and 0 malformed lines. The run took 12.8 minutes with 16 processes. A
second conversion gave the same 337 files, table for table. The synthetic schema-8 feed
(`docs/feed-format-v9/prototype.py demo --truncated`, with `truncated`, `meta` and `stop`
records) also reads back the same, but for one block's key order (Caveats).

#### Asking it

DuckDB 2.0 (16 threads) over the tables, against the way the analysis scripts read the feed
(orjson over the `.gz` in 8 processes), the page cache warm for both:

| question | DuckDB on Parquet | orjson on `.jsonl.gz` |
|---|---:|---:|
| exits by `actor3` and `agent_type`, one day (17.1 M rows) | 0.03 s | 28.1 s |
| CPU seconds and network bytes by `agent_type`, one day | 0.03 s | |
| `netio` bytes by provider, one day (`UNNEST(endpoints)`) | 0.07 s | |
| exits by day and `actor3`, the week (144.5 M rows) | 0.10 s | |
| p99 `duration_s` of agent tool calls by `agent_type`, the week | 0.8 s | |

The first two queries read two or three columns, where a JSON reader parses every key of every
line.

## Choices

- **A table per record kind, not per stream.** The snapshot stream mixes seven kinds with
  little in common; a table each gives every table a fixed set of columns and none null by
  construction. `exit` and `truncated` share one, as they share the exits stream: both end a
  process's life, and `event` tells them apart.
- **Identity stays on every row.** Schema 9 moves the 30-key identity block into a `procs`
  stream because JSON repeats it on every tick. In columns most of that repetition is gone,
  since dictionary pages hold each distinct `cwd` or `session_uuid` once per row group: apart
  from `args`, `pid` and `ppid`, what is left is 6 % of `residency`, 15 % of `netio` and 19 % of
  `conn`, about 150 MB of the week's 6.1 GB. In return every table reads on its own, with no
  join and no process key (which schemas 7 and 8 do not have) to put a process back together.
- **Nested blocks stay nested.** Endpoints, peers and top processes are lists of structs
  inside their row, not child tables, so a row is still a record. `UNNEST` (DuckDB),
  `explode` (Polars) or `list_flatten` (pyarrow) gives the child table when you want it.
- **Exact, then convenient.** Every column holds what the collector wrote, in its type: nothing
  is derived, renamed or converted to other units, so a question about the data is a question
  about the feed. Schema 9's cleanups (bytes instead of MB, an epoch `start`) belong in the
  collector, and would reach this copy on their own.
- **Rows in file order.** File order is time order to within the collector's 250 ms exit
  hold, so row-group statistics on `ts_epoch` can skip by time, and the day-file's order
  is kept without a sequence column.
- **zstd 9, 1 Mi-row groups, dictionary pages, with `pid`/`ppid` delta-packed and
  `ts_epoch` byte-stream-split** in the tables keyed by process. The dashboard's store writes
  128 Ki-row groups instead: an hour of a busy node's exits fills one or two, so a read of an
  hour's span decodes no more than that. Measured on one FAS host-day: zstd 9 is 5 % smaller
  than zstd 3, and zstd 19 another 10 % smaller at eight times the encoding time
  (`--zstd-level 19` for an archive). Groups of 128 Ki or 256 Ki rows cost 5-8 %, and a larger
  dictionary page limit 9 %. The three encodings save 10.5 % of `exit` and 4 % of `netio` and
  `residency`; they are left off `conn`, where they cost 1 % because its sockets repeat with
  the same pid every tick.

## Caveats

- **Integers past 64 bits**: the day-files are read as the dashboard reads them
  (`fastjson`), and orjson reads such an integer as a float, so the copy has the float
  (in `_extra`). The collector writes no such value.
- **Key order**: a record comes back in its event's order, and a key only `_extra` holds comes
  back last. The demo generator writes `by_agent_type`'s `users` and `autonomous` the other way
  round from the collector; they come back in the collector's order.
- **What is not kept**: a line that does not parse or is not a JSON object (counted in the
  manifest as `malformed`, as every reader of the feed counts it), and a blank line (not
  counted); `nonrec` keeps where each lies and what it was, so every offset stays the file's. A
  last line with no newline, the torn end of a live file, ends the read at `partial`. The
  dashboard stands a copy in for its file only when the file has neither such a last line nor a
  line that is JSON but not an object.
- **The node tier** is not covered: its 36-key snapshots have no `event` and would land
  in `other`.

## Open questions

1. **Keep or replace the `.gz`.** The copy is exact, so it could replace the sealed JSONL at
   rest, at a third of the size; the cautious order is to keep both until `verify` has run
   clean for a while.
2. **Whole-range reads.** The dashboard's backfill and hour reductions read the JSONL, because
   building a record from a row in Arrow costs more than orjson's parse of a line. Reducers that
   read columns instead of records would take those reads from the copy too.
3. **A node-tier table**, and whether to add `pk`/`ppk` columns when schema 9 lands (until
   then, schema 9's keys arrive in `_extra`, losslessly).
