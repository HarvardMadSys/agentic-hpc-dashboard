# EBR out, Parquet in: Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Remove EBR from the collector, fleet and dashboard; keep JSONL as the one live format and
give the dashboard a Parquet history store of sealed day-files that its analytical readers use.

**Architecture:** The collector writes JSONL only (as FAS already does). The dashboard reads the
collector's JSONL directly (the path `RC_DASH_CONVERT=0` takes today) and keeps, per sealed
`<day>/<host>.<stream>.jsonl.gz`, a Parquet **replica**: `rc_dashboard/feedparquet.py`'s exact
encoding, one file per table, each row carrying `_off`, the byte offset of its line in the
uncompressed source. With the source's line starts, size, complete end and first 4 KB in its
manifest, a replica answers every question a reader asks of the JSONL file -- a time seek, a
ranged read forward or backward, `is_boundary`, the head digest -- with the same offsets and
the same records. So positions, saved window builds, hour spans and the tail never change
meaning, a reader can switch between a file and its replica at any time, and the API's answers
are identical with the store on or off (the e2e test). Conversion runs in a background process
pool, never blocking startup; a file without a current replica is read as JSONL.

**Tech stack:** Python 3.12, pyarrow (now a required dependency), orjson, unittest.

**Why (measured on a FAS host-day, boslogin05 2026-11-15, 1.81 M records):** the EBR view costs
25-30 us/record to build and 52 GB of disk for a week of 8 hosts (the Parquet copy: 5.7 GB);
reading a sealed `.jsonl.gz` costs ~15 us/record (gunzip 3 + orjson 12), the replica ~8 us
building full records column-wise. The follow-ups that make restarts fast (stored normalized
columns, per-reducer invalidation, per-host parallelism, vectorized reducers) build on this
store; they are not in this plan.

---

## Store layout

```
<history.dir>/                         default <cache.dir>/history; dirs 0700, files 0600
  <tier>/<root-basename>-<sha1(realpath)[:10]>/
    SOURCE.json                        the source root it mirrors (as the EBR view had)
    <table>/day=<D>/<host>.<stream>.parquet     rows of one source file, in file order
    _manifest/day=<D>/<host>.<stream>.json      the source: name, size, mtime_ns, ino, usize,
                                                complete_end, head (4 KB, base64), nonrec
                                                [[off, "blank"|"bad"], ...], tables {t: rows},
                                                records, parquet_version
```

`feedparquet` moves to this per-source layout (PARQUET_VERSION 2) for its CLI too, so the
dashboard's store is also the analysis dataset (`read_parquet('<store>/exit/*/*.parquet')`).

## Tasks

### Task 1: feedparquet v2 -- per-source files, `_off`, source metadata
**Files:** modify `dashboard/rc_dashboard/feedparquet.py`; test `dashboard/tests/test_feedparquet.py`.
1. Test: converting a day-file with blank and malformed lines yields per-table files named
   `<host>.<stream>.parquet`, every row's `_off` equal to its line's offset, a manifest with
   `usize`, `complete_end`, `head`, `nonrec`; `records()` round-trips; `_off` is not part of a
   rebuilt record.
2. Implement: `convert_source(path, out_root)` (one source file); `Source` yields `(off, rec)`
   and records non-record lines; `Builder` takes `_off`; schema gains `_off` (int64, delta).
   `convert_host_day` becomes a loop over sources; CLI and verify follow.
3. Run `python -m unittest tests.test_feedparquet`; commit.

### Task 2: `replica.py` -- a sealed file's replica, position-exact
**Files:** create `dashboard/rc_dashboard/replica.py`; test `dashboard/tests/test_replica.py`.
API (all offsets are the uncompressed source's):
- `Replica.open(manifest_path)`; `.size` (usize), `.complete_end`, `.head(n)`;
- `.seek_to_epoch(since, size=None, probe_lines=64)`, `.end_at(t, size=None)`: the JSONL
  searches' exact answers (`feedfile._probe` semantics: next whole line after `pos`, first
  timestamped record among the next `probe_lines` lines, blank and bad lines counted);
- `.is_boundary(off)`; `.records(start=0, end=None, reverse=False, kinds=None, where=None)`
  yielding `(off, rec)` exactly as `fastjson.loads` of the lines in `[start, end)` would,
  merged across tables by `_off`, `where` with `ebr.match_where`'s semantics, plus
  `.bad_in(start, end)` (malformed lines in a range, for `parse_errors`).
Tests: random feeds (blank/bad lines, out-of-order times, records with no time, several kinds),
every probe position against `feedfile.seek_to_epoch`/`end_at_jsonl` on the JSONL, every range
forward and reverse against the lines, kinds/where against post-filtering. Commit.

### Task 3: `history.py` -- the store, its conversions, its status
**Files:** create `dashboard/rc_dashboard/history.py`; modify `config.py`, `app.py`,
`aggregator.py` (status in `/api/feeds`), `__main__.py` (`--check-feeds` line); test
`dashboard/tests/test_history.py`.
- Settings `history.enabled` (`RC_DASH_HISTORY`, default on), `history.dir`
  (`RC_DASH_HISTORY_DIR`), `history.workers` (`RC_DASH_HISTORY_WORKERS`, 0 = min(8, CPUs the
  service may use)), `history.days` (`RC_DASH_HISTORY_DAYS`, 0 = every sealed day).
- `HistoryStore.discover()` lists sealed `*.jsonl.gz` under the eBPF roots, compares with
  manifests; `run()` converts the missing or stale ones biggest first in a process pool, in a
  background thread; `prune()` drops replicas whose source is gone; `replica_for(path)` returns
  a current `Replica` or None. Module-level `install()/replica_for()` for the readers.
- App: started at startup without blocking; re-discovered every 60 s.
Tests: discovery, staleness (a replaced `.gz`), pruning, `replica_for` before/after, modes.

### Task 4: readers use replicas for sealed files
**Files:** `export.py` (`window_plan`, `resume_plan`, `_window_stream`, `_matching`), `hours.py`
(`plan_span`, `_stream`), `tail.py` (`position`, `_read_back_one`), `proctree.py`
(`_key_stream`, `_key_stream_gz`); tests in the existing suites plus
`dashboard/tests/test_replica_readers.py` (each reader, replica vs JSONL, same output).

### Task 5: EBR out of the dashboard
Delete `ebr.py`, `ebrview.py` and every EBR branch (`feedfile`, `tail`, `export`, `hours`,
`proctree`, `iocmds`, `feeds`, `clock`, `aggregator`), the twin rule and `prefer`,
`ingest.convert`, the 503 while converting, the demo's EBR writing (`--demo-format`), EBR tests
and benches; `CODE_FILES` lose `ebr.py` and gain `replica.py`/`feedparquet.py`. pyarrow moves to
the main dependencies. Full suite green.

### Task 6: e2e -- the store on and off give the same API
`dashboard/tests/e2e_history.py` (from `e2e_formats.py`): a demo feed in JSONL with sealed
days, served twice (history off / on, conversion finished), every API diffed exactly; `--restart`
resumes saved builds across the switch.

### Task 7: EBR out of the collector and fleet (parallel, subagent)
`collector/lib/ebr.py`, `EBPFM_FORMAT`/`EBPFM_EBR_*`, the twins in `DailyWriter`, the node
tier's `ebr.py append`, `ebpfm.sh` verbs (`compare`, `verify`, `bench`, `ebr`) and checks,
fleet's `.ebr` carrying, packaging, tests and docs. `meta.output`/`stop.output` keep their
shape with JSONL only (`"ebr": null`), so no schema bump.

### Task 8: docs and measurements
Dashboard README ("History in Parquet" replaces "Two formats" and "Reading EBR only"),
collector README, `docs/feed-parquet` (per-source layout), root README. Measure on the FAS
mirror: store size and conversion time, replica vs `.jsonl.gz` read speed for the backfill and
an hour's span.

### Task 9: PR into develop, merged when green (the user asked for the merge).
