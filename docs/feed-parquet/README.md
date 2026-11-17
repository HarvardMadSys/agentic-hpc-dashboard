# The feed as Parquet tables

**Status: a tool beside the feed.** `rc_dashboard.feedparquet` writes a typed, columnar
copy of the eBPF tier's day-files and reads every record back from it exactly. The
collector and the dashboard's read path do not change. This is the "columnar copy of
sealed days" that [schema 9's proposal](../feed-format-v9/README.md) left open (its §4.3
and open question 5), built for the feed as it is today: FAS's login nodes write schema
7, the collector in this repo writes 8, and both convert.

Every number here comes from the FAS mirror (8 login nodes, the 7 sealed days
2026-11-09 to 2026-11-15) unless it says otherwise. The data names real users and
commands, so this page gives aggregates only.

## In short

- **One table per record kind**: `exit` (with `truncated`), `conn`, `netio`, `residency`,
  `residency_totals`, `submit`, `meta`, `stop`. A table's columns are its records' keys,
  in the collector's order, typed from the code that writes them; nested blocks stay
  nested (structs, lists of structs, maps).
- **Lossless**: every one of a week's 179,448,351 records (8 hosts, 56 host-days) reads
  back the same as the day-file's, type for type and in its key order (`verify`); not
  one needed `_extra`.
- **Small**: a third of the sealed `.jsonl.gz` (6.11 GB for 18.32 GB over a week of 8
  hosts), 1.5 % of the JSON itself; an `exit` row is 35 bytes.
- **Fast to ask**: counting a day's 17 M exits by class takes DuckDB 0.03 s on the
  tables and 28 s as a parallel orjson scan of the `.gz`; a week's p99 tool-call
  duration per agent type, over 145 M exits, 0.8 s.

## Running it

pyarrow is an optional extra; the service never imports the module.

```bash
cd dashboard
uv run --extra parquet python -m rc_dashboard.feedparquet convert --out /path/to/pq \
    /scratch/marthen/rc-dashboard/data/mirror/*      # the roots feeds.ebpf.roots lists
uv run --extra parquet python -m rc_dashboard.feedparquet verify  --out /path/to/pq \
    /scratch/marthen/rc-dashboard/data/mirror/*      # every record, rebuilt and compared
uv run --extra parquet python -m rc_dashboard.feedparquet schema --markdown
```

- It reads what the dashboard reads: `.jsonl` or `.ebr`, plain or `.gz`, one twin per
  stream (`--prefer`), through `feedfile`.
- Today's day-files are skipped (they are still being written) unless `--include-today`.
- A host-day whose sources have not changed since its manifest is skipped, so a cron
  job can run it every night; `--force` converts again. `--days N` and `--day D` narrow
  it, `--jobs` sets the processes (one per host-day).
- Output is written beside its final name and renamed into place, files `0640` and
  directories `0750`, as the collector writes the day-files: the tables hold what those
  hold.

### Layout

```
pq/<table>/day=<YYYY-MM-DD>/<host>.parquet
pq/_manifest/day=<YYYY-MM-DD>/<host>.json     sources (name, size, mtime), records per table,
                                              malformed lines, rows with _extra, seconds
```

A table is a directory, `day` a Hive partition, and every file also carries its host
(the `host` column) and its provenance (the footer's `rcfeed` entry), so it stands
alone. A day leaves the copy the way it leaves the feed: its directories go.

```sql
-- DuckDB
SELECT agent_type, count(*), sum(cpu_s)
FROM read_parquet('pq/exit/*/*.parquet', hive_partitioning = true)
WHERE day >= '2026-11-12' AND actor3 = 'agent'
GROUP BY ALL;

SELECT e.provider, sum(e.tx_bytes + e.rx_bytes)
FROM read_parquet('pq/netio/*/*.parquet'), UNNEST(endpoints) AS t(e)
GROUP BY ALL;
```

```python
import polars as pl                       # or pyarrow.dataset.dataset("pq/exit", partitioning="hive")
pl.scan_parquet("pq/exit/**/*.parquet").group_by("actor3").len().collect()

from rc_dashboard import feedparquet      # the records themselves, exactly as the day-file had them
for rec in feedparquet.records("pq/exit/day=2026-11-15/<host>.parquet"):
    ...
```

## The schema

### Tables

| table | events | one row per | rows, 2026-11-15, 8 hosts |
|---|---|---|---:|
| `exit` | `exit`, `truncated` | process end; `truncated` for a process still alive at the collector's stop | 17,142,727 |
| `conn` | `conn` | standing external socket per residency tick | 2,138,107 |
| `netio` | `netio` | process that moved bytes, per tick | 1,675,063 |
| `residency` | `residency` | live agent tree per tick | 660,883 |
| `residency_totals` | `residency_totals` | host per tick | 11,515 |
| `submit` | `submit` | `sbatch`/`salloc`/`srun` | 18,316 |
| `meta` | `meta` | collector start | 0 (one in the 7 days) |
| `stop` | `stop` | collector stop | 0 |
| `other` | anything else | an event kind no table names (pre-6 `tcp`, a future `session`) | 0 |

Records are routed by `event`, not by stream, so a capture from before the
exits/snapshot split reads the same.

### Types

- **Integers** are int32 where the kernel bounds them (pids, ports, exit codes, signals,
  depths, a process's threads, `args_len`) and int64 everywhere else. On FAS pids reach
  4,194,303 and a socket's `rx_bytes` 1.66 TB.
- **Floats** are float64 and hold exactly what the collector wrote. It rounds at the
  source (`duration_s` to 4 places, `peak_rss_mb` to 2), and no field ever mixes ints and
  floats, so no column needs a wider or narrower type to be exact.
- **`ts` and `start`**, the local naive second-granular strings, are timestamps with no
  zone (Parquet's `isAdjustedToUTC=false`; `ms`, Parquet having no seconds unit).
  **`ts_epoch`** stays the float it is: it is the authoritative time, as in the feed.
- **Nested blocks keep their shape**: `io`, `conns`, `net_endpoints` are structs, endpoint
  and peer lists are lists of structs, and objects whose keys are data (`by_command`,
  `providers`, `state_counts`, `perf_seen`, ...) are `map<string, int64>`, in their own
  order. The blocks of `meta` and `stop` with no fixed shape (`config`, `btf_offs`,
  `output`, `unsafe_dirs`) are JSON text.
- **Null means what it means in the feed** (not measured, not applicable), never "this
  record had no such key": that is `_absent`.

### `_absent` and `_extra`

Two columns at the end of every table make the copy exact without a registry of past
schemas:

- **`_absent`** lists the keys of the record's event that the record did not have, and is
  null when it had them all. A schema-7 record lacks `args_redacted`, so it reads
  `["args_redacted"]`: a reader can tell "no such field then" from "nothing was masked".
  A few keys are written only when they hold something (`netio.overflow`,
  `net_endpoints.overflow` and `.unaccounted`, `conns.peers_overflow`, and the demo's
  `synthetic`); for those absent and null are one, and nothing is recorded.
- **`_extra`** is a JSON object of whatever a column cannot hold exactly: a key no table
  names (schema 9's `pk`, pre-7's `comm`), a value of another type than its column's (an
  int where a float is written, since `1` would read back `1.0`), one out of range, text
  that is not UTF-8, a time string in another format, a nested block of another shape.
  It is null on every row that has none, which on FAS is every row.

`feedparquet.records()` rebuilds a record from a row: the event's keys in order, less
those `_absent` names, each from its column or from `_extra`, then any key `_extra`
holds that no table names.

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
  days there is one `meta` record (a collector restart) and no `stop` or `truncated`;
  schema 8's shapes come from the demo generator, which the tests convert too.
- **No field mixes ints and floats**, and no line fails to parse. The only keys ever
  absent are the three written only when set.
- **Some columns are constant on FAS** (`pgrp` and `sid` 0, `swapin_wait_s` 0.0,
  `fork_only` false, `conns.accepted` 0, `cpu_scope` `process`, `dstate_src` `stat`):
  dictionary pages make them free, and they vary elsewhere, so they stay.
- **Ranges that set types**: `args` up to 119,710 characters (schema 7 does not cap it),
  `args_len` 412,675, `min_flt` 704 M, endpoint lists up to the cap of 256 entries.
- **Values to know about**: `duration_s` can be negative (-0.0083); `conn.age_s` on FAS
  is sometimes an epoch, 1.79e9 (schema 7's wall-clock `age_s` on EL8's Python 3.6,
  fixed in 8); `netio.interval_s` and `residency.age_s` reach -3,600 s. The copy keeps
  them as written.

## Measured

All 56 sealed host-days of the mirror (8 hosts, 2026-11-09 to 2026-11-15), 179,448,351
records, converted with the defaults.

### Size

| | `.jsonl.gz` (the sealed feed) | Parquet | |
|---|---:|---:|---:|
| exits stream, to `exit` | 13.24 GB | 5.09 GB | 38.5 % |
| snapshot stream, to the other tables | 5.07 GB | 1.02 GB | 20.0 % |
| **all** | **18.32 GB** | **6.11 GB** | **33.4 %** |

Against the JSON itself, 2026-11-15 is 45.1 GB as written, 2.19 GB gzipped and 694 MB
as Parquet: 1.5 %. Every day lands between 31.7 % and 34.0 % of its gzip.

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
  block is 36 % of the file, and 22 points of that are `args`, `pid` and `ppid`, which
  are each process's own; the other 27 columns, half of an `exit` line in JSON, take
  15 %.
- **`netio`**: the endpoint lists 55 %; identity 35 %, 15 % without `args`/`pid`/`ppid`.
- **`conn`**: its socket counters, each 5-11 %; identity 24 %.
- **`residency`**: `top_procs` 49 %, `age_s` 14 %, `cpu_s` 12 %; identity 6 %.

### Converting

31 CPU-µs per record: parsing the JSON 12, building rows 8.5, building Arrow arrays
8-9.5, gunzip 3, writing 2.5 (300,000 `exit` records, Python 3.12, orjson 3.13,
pyarrow 26). The week took 7.4 minutes with 16 processes (`--jobs 16`), one host-day
each, the largest using 3.6 GB.

### Exact

`verify` over the same 56 host-days read every record from its day-file and from its
table and compared them: 179,448,351 the same, type for type, and every one in its own
key order; 0 rows with `_extra`, 0 malformed lines. It took 12.8 minutes with 16
processes. A second conversion with the final code gave the same 337 files, table for
table, as the first.

The synthetic schema-8 feed (`docs/feed-format-v9/prototype.py demo --truncated`, with
`truncated`, `meta` and `stop` records) reads back the same too, with one difference in
key order only: the demo writes `by_agent_type`'s `users` and `autonomous` the other way
round from the collector.

### Asking it

DuckDB 2.0 (16 threads) over the tables, against the way the analysis scripts read the
feed today, orjson over the `.gz` in 8 processes; page cache warm for both:

| question | DuckDB on Parquet | orjson on `.jsonl.gz` |
|---|---:|---:|
| exits by `actor3` and `agent_type`, one day (17.1 M rows) | 0.03 s | 28.1 s |
| CPU seconds and network bytes by `agent_type`, one day | 0.03 s | |
| `netio` bytes by provider, one day (`UNNEST(endpoints)`) | 0.07 s | |
| exits by day and `actor3`, the week (144.5 M rows) | 0.10 s | |
| p99 `duration_s` of agent tool calls by `agent_type`, the week | 0.8 s | |

The first two read two or three columns of the 76; a JSON reader parses every key of
every line to get them.

## Choices

- **A table per record kind, not per stream.** The snapshot stream mixes six kinds with
  little in common; one table each gives every table a fixed set of columns and no
  column that is null by construction. `exit` and `truncated` share one, as they share
  the exits stream: both end a process's life, and `event` tells them apart.
- **Identity stays on every row.** Schema 9 moves the 30-key identity block into a
  `procs` stream because JSON repeats it on every tick. In columns most of that
  repetition is gone (dictionary pages hold each distinct `cwd` or `session_uuid` once
  per row group): beyond `args`, `pid` and `ppid`, what is left is 6 % of `residency`,
  15 % of `netio` and 19 % of `conn`, about 150 MB of the week's 6.1 GB. For that, every
  table reads
  alone, with no join and no process key, which schema 7 and 8 do not have, to put a
  process back together.
- **Nested blocks stay nested.** Endpoints, peers and top processes are lists of structs
  in their row, not child tables: a row is still a record, and `UNNEST` (DuckDB),
  `explode` (Polars) or `list_flatten` (pyarrow) gives the child table when it is wanted.
- **Exact, then convenient.** Every column holds what the collector wrote, in its type;
  nothing is derived, renamed or converted to other units here, so a question about the
  data is a question about the feed. Schema 9's cleanups (bytes not MB, an epoch
  `start`) belong in the collector, where they would reach this copy by themselves.
- **Rows in file order**, which is time order to within the collector's 250 ms exit
  hold: row-group statistics on `ts_epoch` skip by time, and the order of the day-file
  is kept without a sequence column.
- **zstd 9, 1 Mi-row groups, dictionary pages, with `pid`/`ppid` delta-packed and
  `ts_epoch` byte-stream-split** in the tables keyed by process. Measured on one FAS
  host-day: zstd 9 is 5 % smaller than zstd 3, and zstd 19 another 10 % at eight times
  the encoding time (`--zstd-level 19` for an archive); groups of 128 Ki or 256 Ki rows
  cost 5-8 %; a larger dictionary page limit costs 9 %; the three encodings save 10.5 %
  of `exit` and 4 % of `netio` and `residency`, and are left off `conn`, whose sockets
  repeat with the same pid every tick, where they cost 1 %.

## Caveats

- **Integers past 64 bits**: the day-files are read as the dashboard reads them
  (`fastjson`), and orjson reads such an integer as a float, so the copy has the float
  (in `_extra`). The collector writes no such value. `fastjson`'s docstring says orjson
  refuses them; orjson 3.13 does not.
- **Key order**: a record comes back in its event's order; a key `_extra` holds that no
  table names comes back last. On FAS every record comes back in its own order. The
  demo generator writes `by_agent_type`'s `users` and `autonomous` the other way round
  from the collector; those come back in the collector's order.
- **What is not kept**: a line that is not a JSON object (the torn end of a live file),
  counted in the manifest as `malformed`, as every reader of the feed counts it.
- **The node tier** is not covered: its 36-key snapshots have no `event` and would land
  in `other`. A table of its own is the next step if it is wanted.

## Open questions

1. **Where it runs.** The mirror host, nightly after the `.gz` lands, is the natural
   place; the skip-unchanged rule makes a nightly cron cheap.
2. **Keep or replace the `.gz`.** The copy is exact, so it could replace the sealed
   JSONL at rest, at a third of its size. Keeping both until `verify` has run clean
   for a while is the cautious order.
3. **A node-tier table**, and whether `pk`/`ppk` columns should be added when schema 9
   lands (until then its keys arrive in `_extra`, losslessly).
