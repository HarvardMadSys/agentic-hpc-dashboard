#!/usr/bin/env python3
"""Store spike: could one columnar store replace the dashboard's cache layers?

An experiment, not a feature: docs/design/store-spike.md has the method and the
result. It computes one group of panels -- the Resources reducer's payload, the
`resources` key of `/api/panels`: the window's time series by class (CPU,
runqueue wait, major faults, disk and network bytes, resident memory, D-state by
tool), and its per-class totals, distributions and worst processes -- three ways
over one feed, and diffs them:

  sql      DuckDB over the history store's Parquet replicas of the sealed days
           and the newest day's plain JSONL, every rule restated in SQL;
  service  the dashboard itself, `rc-dashboard serve` under a pinned clock: cold
           (an empty cache) and warm (restarted over what the cold run saved);
  reducer  the service's own ResourceReducer alone, fed by its own window scan:
           what this one family costs in Python, without the ten other reducers
           a service build runs.

Run it from dashboard/, with DuckDB as a throwaway dependency, which leaves
uv.lock alone:

    cd dashboard && uv run --frozen --with duckdb python tools/store_spike.py run \\
        [--days 8] [--hosts 6] [--perturb] [--work DIR] [--keep]

`run` does everything, in order:

1. writes a demo feed (`demo --demo-write-only`, as tests/e2e_history.py does),
   optionally perturbs it (`--perturb`, below), seals every eBPF day but the
   newest as `.gz` and converts the sealed days with the history store, so the
   sealed days have Parquet replicas and the newest day is plain JSONL;
2. evaluates the SQL for each window (60, 1440 and 10080 minutes, at the
   service's bin widths), each in a fresh process: cold (a new connection),
   then warm (the same query again), with the process's peak RSS
   (`resource.getrusage`) and DuckDB's own peak buffer memory (its JSON
   profile);
3. feeds the ResourceReducer alone for each window, in a fresh process;
4. serves the feed once per window, with an empty cache that builds only that
   window, then restarts it over its saved state, and reads `/api/panels`,
   `/api/windows` and the process's RSS (`/proc/<pid>/status`) each time; then
   once more with all three windows in one cache, the default 24 h first, as a
   deployment holds them;
5. diffs the panels, value by value.

It prints the feed (records and sizes), a table of time, memory and disk per
path and window, and the diff: every value compared, how many were equal, how
many differed only in the last rounded digit at a rounding tie (float sums in
another order), and every other difference, the first few named. Everything is
written to `<work>/results/` (`summary.json`, one file per evaluation). Exit
status 0 when the SQL matches the service everywhere but in such ties, 1
otherwise, 2 when the run could not be made.

The rules are restated in SQL, not imported: classes (`normalize.py`), the
null gating and impossible waits, the bins (`resources.bin_for`), gaps kept as
nulls, the own-I/O join (`reducers/own_io.py`) with the hour chain's reach and
ageing (`hours.py`), the quantile sketch (`reducers.Quant`). Their constants and
regular expressions are read from the service's modules, so the two cannot
drift; credentials are masked by `redact.py` on the rows a query returns, as on
every way out of the service. What SQL does not reproduce is listed in the
write-up.

`--perturb` rewrites a deterministic share of the demo's records before they
are sealed, to exercise rules the demo never triggers: fields not measured
(null), a missing `io` block, an int where the replica's column is a float (it
then lives in `_extra`), an impossible wait, a self-observation record, an
editor known only by its path, an `actor3` absent or null, a `leader`-scoped
`io`, and Cursor processes in the residency ticks. Every record still says
`synthetic`.

The internal commands `sql` and `reducer` are the measured evaluations, run by
`run` in processes of their own; `--help` on each lists their options.
"""
import argparse
import gzip
import json
import math
import os
import resource
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
DASH = os.path.dirname(HERE)
sys.path.insert(0, DASH)
sys.path.insert(0, os.path.join(DASH, "tests"))

WINDOWS = (60, 1440, 10080)


def log(msg=""):
    sys.stdout.write(msg + "\n")
    sys.stdout.flush()


class Fatal(Exception):
    """The run could not be made: exit 2."""


# ---------------------------------------------------------------- measuring
def maxrss():
    """This process's peak RSS in bytes (Linux reports KiB)."""
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024


def proc_mem(pid):
    """A process's VmRSS and VmHWM (peak), and its children's VmRSS, in bytes."""
    def status(p):
        out = {}
        try:
            with open("/proc/%d/status" % p) as fh:
                for line in fh:
                    if line.startswith(("VmRSS:", "VmHWM:")):
                        k, v = line.split(":", 1)
                        out[k] = int(v.split()[0]) * 1024
        except OSError:
            pass
        return out
    me = status(pid)
    kids = 0
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        try:
            with open("/proc/%s/stat" % d) as fh:
                ppid = int(fh.read().rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            continue
        if ppid == pid:
            kids += status(int(d)).get("VmRSS", 0)
    return {"rss": me.get("VmRSS"), "hwm": me.get("VmHWM"), "children_rss": kids}


def du(path):
    """Apparent bytes and files under `path`."""
    n = files = 0
    if os.path.isfile(path):
        return {"bytes": os.path.getsize(path), "files": 1}
    for dp, _dirs, names in os.walk(path):
        for name in names:
            try:
                n += os.lstat(os.path.join(dp, name)).st_size
                files += 1
            except OSError:
                pass
    return {"bytes": n, "files": files}


def mb(n):
    return "-" if n is None else "%.1f MB" % (n / 1e6)


def secs(s):
    return "-" if s is None else ("%.2f s" % s if s < 100 else "%.0f s" % s)


# ---------------------------------------------------------------- the feed
def paths(work):
    return {"work": work, "conf": os.path.join(work, "config.json"),
            "demo": os.path.join(work, "demo"),
            "feed": os.path.join(work, "demo", "feed"),
            "ebpf": os.path.join(work, "demo", "feed", "ebpf"),
            "node": os.path.join(work, "demo", "feed", "node"),
            "store": os.path.join(work, "store"),
            "history": os.path.join(work, "store", "history"),
            "results": os.path.join(work, "results"),
            "service": os.path.join(work, "service")}


def perturb(feed):
    """Rewrite a deterministic share of the eBPF tier's records (module docstring).
    Before sealing; line by line, keeping each file's order. Returns the counts."""
    said = {}

    def hit(what):
        said[what] = said.get(what, 0) + 1

    for dp, _dirs, names in sorted(os.walk(os.path.join(feed, "ebpf"))):
        for name in sorted(names):
            if not name.endswith(".jsonl"):
                continue
            src = os.path.join(dp, name)
            tmp = os.path.join(dp, ".%s.tmp" % name)
            with open(src, "rb") as fi, open(tmp, "w") as fo:
                for line in fi:
                    if not line.strip():
                        continue
                    rec = json.loads(line)
                    ev = rec.get("event")
                    if ev == "exit":
                        _perturb_exit(rec, hit)
                    elif ev == "residency_totals":
                        _perturb_tick(rec, hit)
                    fo.write(json.dumps(rec, separators=(",", ":")) + "\n")
            os.replace(tmp, src)
    return said


def _perturb_exit(rec, hit):
    p = rec.get("pid")
    if not isinstance(p, int):
        return
    io = rec.get("io") if isinstance(rec.get("io"), dict) else None
    if p % 23 == 0:
        rec["sched_wait_s"] = rec["sched_run_s"] = None
        hit("schedstat not measured")
    if p % 29 == 0:
        rec["net_tx_bytes"] = rec["net_rx_bytes"] = None
        hit("network not measured")
    if p % 31 == 0:
        rec["io"] = None
        hit("no io block")
    elif p % 53 == 0 and io is not None:
        io["rd_mb"] = io["wr_mb"] = None
        hit("disk bytes not measured, requested bytes kept")
    elif p % 73 == 0 and io is not None:
        io["scope"] = "leader"
        hit("io of the leader thread alone")
    if p % 37 == 0:
        rec["cpu_s"] = None
        hit("cpu not measured")
    elif p % 79 == 0 and isinstance(rec.get("cpu_s"), float):
        rec["cpu_s"] = int(rec["cpu_s"])
        hit("cpu_s an int (the replica keeps it in _extra)")
    if p % 41 == 0:
        rec["maj_flt"] = None
        hit("major faults not measured")
    if p % 43 == 0 and isinstance(rec.get("duration_s"), float):
        rec["duration_s"] = int(round(rec["duration_s"]))
        hit("duration_s an int (the replica keeps it in _extra)")
    if p % 47 == 0:
        rec["blkio_wait_s"] = 3393948.0
        hit("impossible block-I/O wait (the host's uptime)")
    if p % 59 == 0 and rec.get("actor3") == "human":
        rec["args"] = "/home/u/.cursor-server/bin/0123abcd/node " + (rec.get("args") or "")
        hit("an editor known by its path")
    if p % 61 == 0:
        rec["args"] = "python3 /opt/ebpfm/node_snapshot.py --once"
        hit("self-observation (dropped)")
    if p % 67 == 0:
        rec.pop("actor3", None)
        hit("actor3 absent (derived from actor and agent_type)")
    elif p % 71 == 0:
        rec["actor3"] = None
        hit("actor3 null (unlabeled)")
    if p % 83 == 0:
        rec["child_cpu_s"] = None
        hit("child_cpu_s not measured")


def _perturb_tick(rec, hit):
    t = int(rec.get("ts_epoch") or 0)
    by3 = rec.get("by_actor3")
    if t % 7 == 0 and isinstance(by3, dict) and isinstance((by3.get("agent") or {}).get("rss_mb"), float):
        at = rec.setdefault("by_agent_type", {}) or {}
        rec["by_agent_type"] = at
        at["cursor"] = {"trees": 1, "n_procs": 1, "threads": 4,
                        "rss_mb": round(by3["agent"]["rss_mb"] * 0.25, 1),
                        "cpu_s": 1.0, "d_state": 0, "autonomous": 0, "users": 1}
        hit("tick: a Cursor tree (its memory moved from agent to ide)")
    if t % 11 == 0 and isinstance(by3, dict) and isinstance(by3.get("human"), dict):
        by3["human"]["rss_mb"] = int(by3["human"].get("rss_mb") or 0)
        hit("tick: an int rss_mb (the replica keeps the map in _extra)")


def feed_stats(p):
    """Records and bytes of the feed and the store."""
    out = {"ebpf": {"sealed_gz": 0, "sealed_inflated": 0, "plain": 0, "files_sealed": 0,
                    "files_plain": 0, "records": {}, "records_sealed": 0, "records_plain": 0},
           "node": du(p["node"])}
    for dp, _dirs, names in sorted(os.walk(p["ebpf"])):
        for name in sorted(names):
            path = os.path.join(dp, name)
            sealed = name.endswith(".jsonl.gz")
            if not (sealed or name.endswith(".jsonl")):
                continue
            e = out["ebpf"]
            e["sealed_gz" if sealed else "plain"] += os.path.getsize(path)
            e["files_sealed" if sealed else "files_plain"] += 1
            with (gzip.open(path, "rb") if sealed else open(path, "rb")) as fh:
                for line in fh:
                    if sealed:
                        e["sealed_inflated"] += len(line)
                    if not line.strip():
                        continue
                    ev = json.loads(line).get("event")
                    e["records"][ev] = e["records"].get(ev, 0) + 1
                    e["records_sealed" if sealed else "records_plain"] += 1
    node_records = 0
    for dp, _dirs, names in os.walk(p["node"]):
        for name in names:
            if name.endswith((".jsonl", ".jsonl.gz")):
                path = os.path.join(dp, name)
                with (gzip.open(path, "rb") if name.endswith(".gz") else open(path, "rb")) as fh:
                    node_records += sum(1 for line in fh if line.strip())
    out["node"]["records"] = node_records
    store = {}
    for root in store_roots(p):
        for table in sorted(os.listdir(root)):
            d = os.path.join(root, table)
            if os.path.isdir(d):
                got = du(d)
                store[table] = got
    out["store"] = {"tables": store,
                    "bytes": sum(v["bytes"] for v in store.values()),
                    "read_by_spike": sum(store.get(t, {}).get("bytes", 0)
                                         for t in ("exit", "residency_totals"))}
    return out


def store_roots(p):
    base = os.path.join(p["history"], "ebpf")
    return sorted(os.path.join(base, d) for d in os.listdir(base)) if os.path.isdir(base) else []


def make_feed(p, days, hosts, do_perturb):
    import e2e_history as e2e
    os.makedirs(p["work"], exist_ok=True)
    with open(p["conf"], "w") as fh:
        fh.write("{}\n")            # no dashboard.config.json of this checkout's
    stamp, took = e2e.write_feed(p["demo"], days, hosts, p["conf"])
    log("feed: %d day(s), %d host(s) written in %.1f s, %s .. %s" % (
        days, hosts, took, time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stamp["start"])),
        time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stamp["written_to"]))))
    said = None
    if do_perturb:
        said = perturb(p["feed"])
        log("feed perturbed: %s" % ", ".join("%s %d" % kv for kv in sorted(said.items())))
    sealed = e2e.seal_days(p["feed"], 1)
    t = time.monotonic()
    converted = e2e.preconvert([p["ebpf"]], p["store"])
    took_store = time.monotonic() - t
    log("store: %d sealed day-file(s), %d converted to Parquet in %.1f s" % (
        sealed, converted, took_store))
    meta = {"days": days, "hosts": hosts, "stamp": stamp, "write_s": took,
            "perturbed": said, "sealed": sealed, "converted": converted,
            "convert_s": took_store, "tz": time.strftime("%Z"),
            "newest_day": e2e.ebpf_days(p["feed"])[-1]}
    meta["stats"] = feed_stats(p)
    os.makedirs(p["results"], exist_ok=True)
    with open(os.path.join(p["results"], "feed.json"), "w") as fh:
        json.dump(meta, fh, indent=1)
    return meta


def load_feed_meta(p):
    with open(os.path.join(p["results"], "feed.json")) as fh:
        return json.load(fh)


# ============================================================ the SQL path
# What a window asks of the service, worked out as the service works it out.
def window_params(window_min, now, newest):
    """The window's bounds, bin width and how its own-I/O join reaches back, as
    the service builds it from an empty cache (hours.build, windows._build)."""
    from rc_dashboard.reducers.resources import bin_for
    from rc_dashboard.hours import HOUR
    grace = 300                       # panels.hour_grace_s's default
    window_s = window_min * 60
    since = now - window_s
    h_lo = int(math.ceil(since / HOUR))
    h_hi = int((min(now, newest) - grace) // HOUR)    # HourStore.closed_end
    merged = h_hi > h_lo
    return {"window_min": window_min, "window_s": window_s, "now": now, "since": since,
            "bin_s": bin_for(window_s), "h_lo": h_lo, "h_hi": h_hi,
            "path": "merge" if merged else "scan",
            # merge: the hour chain starts afresh at the edge's own hour
            # (hours.build's h_from), and every exit's own figure is the chain's
            "chain_from": (h_lo - 1) * HOUR if merged else None}


def lit(v):
    """A Python value as a DuckDB literal; a float exactly (repr round-trips)."""
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return "CAST(%r AS DOUBLE)" % v
    return "'" + str(v).replace("'", "''") + "'"


def lits(vals):
    return "[" + ", ".join(lit(v) for v in vals) + "]" if vals else "[]::VARCHAR[]"


def current_replica(path, store_root):
    """`{table: parquet path}` of a sealed day-file's replica when it is current
    (the manifest names this file as it is, every table it lists is there at
    the size it was written), else None: the file is then read as JSONL."""
    day = os.path.basename(os.path.dirname(path))
    name = os.path.basename(path)
    stem = name[:-len(".jsonl.gz")]
    try:
        with open(os.path.join(store_root, "_manifest", "day=" + day, stem + ".json")) as fh:
            man = json.load(fh)
        st = os.stat(path)
    except (OSError, ValueError):
        return None
    src = man.get("source") or {}
    if (src.get("name"), src.get("size"), src.get("mtime_ns"), src.get("ino")) != \
            (name, st.st_size, st.st_mtime_ns, st.st_ino):
        return None
    out = {}
    for table, nbytes in (man.get("bytes") or {}).items():
        q = os.path.join(store_root, table, "day=" + day, stem + ".parquet")
        if not os.path.exists(q) or os.path.getsize(q) != nbytes:
            return None
        out[table] = q
    return out


def sources(p, first_day):
    """Every day-file from `first_day` on, in the service's order (`day_files`,
    sorted by path: its rank breaks ties as the service's k-way merge does),
    each read from its current replica or as JSONL."""
    from rc_dashboard.feedfile import day_files
    roots = store_roots(p)
    root = roots[0] if roots else None
    out = []
    for rank, path in enumerate(day_files([p["ebpf"]], 0)):
        day = os.path.basename(os.path.dirname(path))
        if day < first_day:
            continue
        rep = current_replica(path, root) if root and path.endswith(".jsonl.gz") else None
        out.append({"rank": rank, "path": path, "day": day, "replica": rep})
    return out


# The exit record's fields the panels read, each with its kind: f a float,
# i an integer, s a string (`ts`, a local time string, as the record has it).
EXIT_FIELDS = (
    ("ts_epoch", "f"), ("ts", "s"), ("host", "s"), ("event", "s"), ("schema_version", "i"),
    ("pid", "i"), ("ppid", "i"), ("user", "s"), ("actor3", "s"), ("actor", "s"),
    ("agent_type", "s"), ("args", "s"), ("command", "s"), ("comm", "s"), ("session_key", "s"),
    ("exit_code", "i"), ("signal", "i"), ("duration_s", "f"), ("child_cpu_s", "f"),
    ("cpu_s", "f"), ("sched_run_s", "f"), ("sched_wait_s", "f"), ("blkio_wait_s", "f"),
    ("swapin_wait_s", "f"), ("freepages_wait_s", "f"), ("dstate_wait_s", "f"),
    ("dstate_episodes", "i"), ("dstate_max_s", "f"), ("dstate_src", "s"), ("min_flt", "i"),
    ("maj_flt", "i"), ("cmaj_flt", "i"), ("nvcsw", "i"), ("nivcsw", "i"),
    ("net_tx_bytes", "f"), ("net_rx_bytes", "f"), ("peak_rss_mb", "f"), ("rss_mb", "f"))
IO_FIELDS = (("rd", "rd_mb"), ("wr", "wr_mb"), ("rch", "rchar_mb"), ("wch", "wchar_mb"))
SQLTYPE = {"f": "DOUBLE", "i": "BIGINT", "s": "VARCHAR"}
NUMERIC_JSON = "('DOUBLE', 'UBIGINT', 'BIGINT')"


def _pq_select(files, cols, srcmap, kind):
    """The replicas' rows of one table (`kind`: exit or tick) as the common
    columns. A value its column could not hold exactly is in `_extra`
    (feedparquet), so each field is its column, else `_extra`'s."""
    def x(name, k):
        col = 'p."%s"' % name if name in cols else None
        if name == "ts":
            return ("coalesce(json_extract_string(p._extra, '$.ts'), "
                    "strftime(p.ts, '%Y-%m-%d %H:%M:%S'))" if col else
                    "json_extract_string(p._extra, '$.ts')")
        ex = ("json_extract_string(p._extra, '$.%s')" % name if k == "s" else
              "TRY_CAST(json_extract(p._extra, '$.%s') AS %s)" % (name, SQLTYPE[k]))
        return "coalesce(CAST(%s AS %s), %s)" % (col, SQLTYPE[k], ex) if col else ex
    srcs = ", ".join("(%s, %d)" % (lit(f), r) for f, r in srcmap)
    head = ("SELECT m.src, p._off AS ord, %s FROM read_parquet(%s, filename = true, "
            "union_by_name = true) p JOIN (VALUES %s) m(file, src) ON m.file = p.filename"
            % ("%s", lits(files), srcs))
    if kind == "tick":
        cols_sql = ", ".join([
            "%s AS ts_epoch" % x("ts_epoch", "f"), "%s AS ts" % x("ts", "s"),
            "%s AS host" % x("host", "s"), "%s AS event" % x("event", "s"),
            "coalesce(to_json(p.by_actor3), json_extract(p._extra, '$.by_actor3')) AS a3map",
            "coalesce(to_json(p.by_agent_type), json_extract(p._extra, '$.by_agent_type')) AS atmap"])
        return head % cols_sql
    parts = ['%s AS "%s"' % (x(name, k), name) for name, k in EXIT_FIELDS]
    # a key the record did not have is listed in `_absent`; a null one is not
    parts.append("NOT coalesce(list_contains(p._absent, 'actor3'), false) AS actor3_present")
    parts.append("(p.io IS NOT NULL OR json_type(p._extra, '$.io') = 'OBJECT') AS io_dict")
    parts.append("coalesce(p.io.scope, json_extract_string(p._extra, '$.io.scope')) AS io_scope")
    for short, f in IO_FIELDS:
        parts.append("coalesce(p.io.%s, TRY_CAST(json_extract(p._extra, '$.io.%s') AS DOUBLE)) AS %s"
                     % (f, f, short))
    return head % ", ".join(parts)


def _js_select(path, rank, kind):
    """One plain (or unreplicated) day-file's records as the common columns.
    Every key is read with its JSON type: an absent key (`actor3`) is told from
    a null one, and a value of another type than the reducers add is not one."""
    events = ("exit",) if kind == "exit" else ("residency_totals",)
    pre = "regexp_matches(json::VARCHAR, '\"event\"\\s*:\\s*\"(%s)\"')" % "|".join(events)
    if kind == "tick":
        keys = ["$.ts_epoch", "$.ts", "$.host", "$.event", "$.by_actor3", "$.by_agent_type"]
    else:
        keys = ["$.%s" % name for name, _k in EXIT_FIELDS] + ["$.io"]
    inner = ("SELECT %d AS src, ordinality AS ord, json_extract(json, %s) AS x, "
             "json_extract_string(json, %s) AS s FROM read_json_objects(%s, "
             "format = 'newline_delimited', ignore_errors = true) WITH ORDINALITY "
             "WHERE json_type(json) = 'OBJECT' AND %s"
             % (rank, lits(keys), lits(keys), lit(path), pre))

    def val(i, k):
        if k == "f":
            return "CASE WHEN json_type(x[%d]) IN %s THEN TRY_CAST(x[%d] AS DOUBLE) END" % (
                i, NUMERIC_JSON, i)
        if k == "i":
            return ("CASE WHEN json_type(x[%d]) IN ('UBIGINT', 'BIGINT') "
                    "THEN TRY_CAST(x[%d] AS BIGINT) END" % (i, i))
        return "CASE WHEN json_type(x[%d]) = 'VARCHAR' THEN s[%d] END" % (i, i)
    if kind == "tick":
        cols = ["%s AS ts_epoch" % val(1, "f"), "%s AS ts" % val(2, "s"),
                "%s AS host" % val(3, "s"), "%s AS event" % val(4, "s"),
                "CASE WHEN json_type(x[5]) = 'OBJECT' THEN x[5] END AS a3map",
                "CASE WHEN json_type(x[6]) = 'OBJECT' THEN x[6] END AS atmap"]
        return "SELECT src, ord, %s FROM (%s)" % (", ".join(cols), inner)
    cols = ['%s AS "%s"' % (val(i + 1, k), name) for i, (name, k) in enumerate(EXIT_FIELDS)]
    a3 = 1 + [n for n, _k in EXIT_FIELDS].index("actor3")
    io = len(EXIT_FIELDS) + 1
    cols.append("x[%d] IS NOT NULL AS actor3_present" % a3)
    cols.append("json_type(x[%d]) = 'OBJECT' AS io_dict" % io)
    cols.append("CASE WHEN json_type(x[%d], '$.scope') = 'VARCHAR' "
                "THEN json_extract_string(x[%d], '$.scope') END AS io_scope" % (io, io))
    for short, f in IO_FIELDS:
        cols.append("CASE WHEN json_type(x[%d], '$.%s') IN %s THEN TRY_CAST(json_extract(x[%d], "
                    "'$.%s') AS DOUBLE) END AS %s" % (io, f, NUMERIC_JSON, io, f, short))
    return "SELECT src, ord, %s FROM (%s)" % (", ".join(cols), inner)


def source_sql(con, srcs, table, kind):
    """The UNION ALL of every source's rows of one record kind."""
    parts = []
    pq = [(s["replica"][table], s["rank"]) for s in srcs
          if s["replica"] is not None and table in s["replica"]]
    if pq:
        files = [f for f, _r in pq]
        cols = {r[0] for r in con.execute(
            "DESCRIBE SELECT * FROM read_parquet(%s, union_by_name = true)" % lits(files)).fetchall()}
        parts.append(_pq_select(files, cols, pq, kind))
    for s in srcs:
        if s["replica"] is None:
            parts.append(_js_select(s["path"], s["rank"], kind))
    if not parts:
        raise Fatal("no day-file to read")
    return " UNION ALL ".join("(%s)" % q for q in parts)


def sql_rules():
    """The constants and patterns the SQL restates, from the service's modules."""
    from rc_dashboard import normalize as nz
    from rc_dashboard.reducers import INSTANCE_CAP, Quant
    from rc_dashboard.reducers import own_io, resources as rs
    from rc_dashboard.hours import CARRY_KEEP_H, HOUR
    return {
        "self_obs": nz.SELF_OBSERVATION.pattern, "other_ide": nz._OTHER_IDE.pattern,
        "ide_types": sorted(nz.IDE_AGENT_TYPES), "real_agents": sorted(nz._REAL_AGENTS),
        "legacy": nz.LEGACY_CLASS, "v4_only": nz.NULLABLE_V4_ONLY, "waits": nz.WAIT_FIELDS,
        "wait_slack": float(nz.WAIT_SLACK_S), "wait_no_dur": float(nz.MAX_WAIT_NO_DUR_S),
        "counted_ides": sorted(nz._AGENT_COUNTED_IDES),
        "start_slack": float(own_io.START_SLACK_S), "clamp_tol": float(own_io.CLAMP_TOL_MB),
        "per_pid": own_io.PER_PID, "reaped_max": own_io.REAPED_MAX,
        "keep_h": CARRY_KEEP_H, "hour": HOUR, "skew": float(rs.SERIES_SKEW_S),
        "span_cap": rs.SERIES_SPAN_CAP, "stall_tools": rs.STALL_SERIES_TOOLS,
        "instance_cap": INSTANCE_CAP, "fault_fields": rs.FAULT_FIELDS,
        "delay_fields": rs.DELAY_FIELDS, "class_fields": rs.CLASS_SERIES_FIELDS,
        "io_series": rs.IO_SERIES_FIELDS, "net_series": rs.NET_SERIES_FIELDS,
        "gamma": Quant._GAMMA, "inv_lg": Quant._INV_LG, "tiny": Quant._TINY,
        "rel_err": Quant.REL_ERR, "unlabeled": "unlabeled",
        "scope": "login nodes only -- the eBPF tier runs on login nodes, so this is "
                 "login-node resource use, not cluster resource use"}


def build_sql(con, srcs, w, join_from, chain, rules, tz, drop_global=(), drop_agent=()):
    """Every statement of one window's evaluation, in order: `(name, sql)`.
    `join_from`: where the own-I/O join starts reading; `chain`: whether the hour
    chain ages its carried state (merge) or not (scan)."""
    R = rules
    W = lambda name: R["waits"].count(name) > 0     # noqa: E731
    exit_src = source_sql(con, srcs, "exit", "exit")
    tick_src = source_sql(con, srcs, "residency_totals", "tick")
    v4 = [n for n, _k in EXIT_FIELDS if n in R["v4_only"]]
    gated = []
    for name, _k in EXIT_FIELDS:
        e = '"%s"' % name
        if name in v4:
            e = "CASE WHEN sv < 4 THEN NULL ELSE %s END" % name
        gated.append('%s AS "%s"' % (e, name))
    lim = ("CASE WHEN duration_s IS NOT NULL THEN duration_s + %s ELSE %s END"
           % (lit(R["wait_slack"]), lit(R["wait_no_dur"])))
    waits = []
    for name, _k in EXIT_FIELDS:
        if W(name):
            waits.append("coalesce(%s > (%s), false) AS imp_%s" % (name, lim, name))
    keep = [n for n, _k in EXIT_FIELDS if not W(n)]
    stmts = []
    # -- 1. exits, normalized (normalize.py): time, null gating, impossible waits,
    #    the self-observation and account drops, and the class.
    stmts.append(("exits", """
CREATE OR REPLACE TEMP TABLE ex AS
WITH raw AS ({exit_src}),
t AS (
  SELECT * EXCLUDE (ts_epoch, schema_version, command, comm),
    coalesce(ts_epoch, epoch(timezone({tz}, TRY_STRPTIME(left(ts, 19), '%Y-%m-%d %H:%M:%S')))) AS ts_e,
    coalesce(schema_version, 3) AS sv,
    CASE WHEN command IS NOT NULL AND command <> '' THEN command ELSE comm END AS command
  FROM raw WHERE event = 'exit'),
g AS (SELECT src, ord, ts_e, sv, command, actor3_present, io_dict, io_scope, rd, wr, rch, wch,
             {gated_no_cmd} FROM t),
w AS (SELECT * EXCLUDE ({wait_names}), {waits},
             {wait_vals} FROM g),
c AS (
  SELECT *,
    CASE WHEN agent_type IN {ide_types} THEN 'ide'
         WHEN a3_1 IN {legacy_keys} THEN 'ide'
         WHEN a3_1 = 'human' AND args IS NOT NULL AND args <> ''
              AND regexp_matches(args, {other_ide}) THEN 'ide'
         ELSE a3_1 END AS a3
  FROM (
    SELECT *,
      coalesce(nullif(CASE WHEN actor3_present THEN actor3
                           WHEN actor = 'agent' THEN
                             CASE WHEN agent_type IN {real_agents} OR agent_type = 'bwrap' THEN 'agent'
                                  WHEN agent_type IN {ide_types} THEN 'ide' ELSE 'human' END
                           ELSE actor END, ''), {unlabeled}) AS a3_1
    FROM w))
SELECT src, ord, ts_e AS ts, ts AS ts_str, host, pid, ppid, "user" AS usr, a3, agent_type,
       args, command AS tool, session_key, exit_code, signal, duration_s, child_cpu_s, cpu_s,
       sched_run_s, sched_wait_s, blkio_wait_s, swapin_wait_s, freepages_wait_s,
       dstate_wait_s, dstate_episodes, dstate_max_s, dstate_src, min_flt, maj_flt, cmaj_flt,
       nvcsw, nivcsw, net_tx_bytes, net_rx_bytes, peak_rss_mb, rss_mb,
       io_dict, io_scope, rd, wr, rch, wch, {imp_names}
FROM c
WHERE ts_e IS NOT NULL AND ts_e >= {join_from}
  AND NOT coalesce(regexp_matches(coalesce(args, ''), {self_obs}), false)
  AND NOT ("user" IS NOT NULL AND "user" <> '' AND ("user" IN (SELECT unnest({drop_global}))
           OR (a3 = 'agent' AND "user" IN (SELECT unnest({drop_agent})))))
""".format(exit_src=exit_src, tz=lit(tz),
           gated_no_cmd=", ".join(g for g in gated if not g.endswith(
               (' AS "ts_epoch"', ' AS "schema_version"', ' AS "command"', ' AS "comm"'))),
           wait_names=", ".join(n for n, _k in EXIT_FIELDS if W(n)),
           waits=", ".join(waits),
           wait_vals=", ".join("CASE WHEN coalesce(%s > (%s), false) THEN NULL ELSE %s END AS %s"
                               % (n, lim, n, n) for n, _k in EXIT_FIELDS if W(n)),
           ide_types="(%s)" % ", ".join(lit(x) for x in R["ide_types"]),
           legacy_keys="(%s)" % ", ".join(lit(x) for x in R["legacy"]),
           other_ide=lit(R["other_ide"]),
           real_agents="(%s)" % ", ".join(lit(x) for x in R["real_agents"]),
           unlabeled=lit(R["unlabeled"]),
           imp_names=", ".join("imp_" + n for n, _k in EXIT_FIELDS if W(n)),
           join_from=lit(float(join_from)), self_obs=lit(R["self_obs"]),
           drop_global=lits(list(drop_global)), drop_agent=lits(list(drop_agent)))))
    del keep
    # -- 2. the own-I/O join (reducers/own_io.py). Exits in the order the
    #    service feeds them (time, then file, then line), per host.
    stmts.append(("join: order", """
CREATE OR REPLACE TEMP TABLE j AS
SELECT *, CASE WHEN duration_s IS NOT NULL AND duration_s >= 0 THEN ts - duration_s END AS st,
       CAST(floor(ts / {hour}) AS BIGINT) AS hr,
       row_number() OVER (PARTITION BY host ORDER BY ts, src, ord) AS seq
FROM ex""".format(hour=R["hour"])))
    # A process-scoped exit takes back what was held for its pid since that
    # pid's previous such exit, and hands its whole figure to its parent.
    stmts.append(("join: pops and gives", """
CREATE OR REPLACE TEMP TABLE pops AS
SELECT host, pid, seq, hr, usr, st, child_cpu_s,
       coalesce(lag(seq) OVER (PARTITION BY host, pid ORDER BY seq), 0) AS prev_seq
FROM j WHERE io_dict AND io_scope = 'process';
CREATE OR REPLACE TEMP TABLE gives AS
SELECT host, ppid, seq, hr, usr, st, rd, wr, rch, wch
FROM j WHERE io_dict AND io_scope = 'process' AND ppid IS NOT NULL
  AND (coalesce(rd, 0) <> 0 OR coalesce(wr, 0) <> 0 OR coalesce(rch, 0) <> 0
       OR coalesce(wch, 0) <> 0)"""))
    # Each give is held for the parent's next pop. The hour chain drops held
    # state untouched for CARRY_KEEP_H at the end of each closed hour; a give
    # survives to the pop unless an event after it comes more than keep_h + 1
    # hours later and the hour that drop falls at end of was reduced.
    age = ("(next_hr > ghr + {k1} AND ghr + {k1} <= {last})".format(
        k1=R["keep_h"] + 1, last=w["h_hi"] - 1) if chain else "false")
    stmts.append(("join: candidates", """
CREATE OR REPLACE TEMP TABLE cand AS
SELECT *, bool_or(brk) OVER (PARTITION BY host, pseq ORDER BY gseq
                             ROWS BETWEEN CURRENT ROW AND UNBOUNDED FOLLOWING) AS cut
FROM (
  SELECT *, {age} AS brk FROM (
    SELECT p.host, p.seq AS pseq, p.hr AS phr, p.usr AS pusr, p.st AS pst, p.child_cpu_s AS pcc,
           g.seq AS gseq, g.hr AS ghr, g.usr AS gusr, g.st AS gst, g.rd, g.wr, g.rch, g.wch,
           coalesce(lead(g.hr) OVER (PARTITION BY p.host, p.seq ORDER BY g.seq), p.hr) AS next_hr
    FROM pops p JOIN gives g
      ON g.host IS NOT DISTINCT FROM p.host AND g.ppid = p.pid
     AND g.seq > p.prev_seq AND g.seq < p.seq))""".format(age=age)))
    conflict = ("(pusr IS NOT NULL AND pusr <> '' AND gusr IS NOT NULL AND gusr <> '' "
                "AND pusr <> gusr AND pusr <> 'root' AND gusr <> 'root')")
    early = "(pst IS NOT NULL AND gst IS NOT NULL AND gst < pst - %s)" % lit(R["start_slack"])
    stmts.append(("join: taken", """
CREATE OR REPLACE TEMP TABLE taken AS
SELECT host, pseq,
       count(*) AS n_cand, count(*) FILTER (WHERE ok) AS n_ok,
       count(*) FILTER (WHERE cut) AS n_aged, count(*) FILTER (WHERE NOT cut AND {conflict}) AS n_user,
       count(*) FILTER (WHERE NOT cut AND NOT {conflict} AND {early}) AS n_early,
       bool_or(rnone) AS reaped_none,
       fsum(rd) FILTER (WHERE ok) AS t_rd, fsum(wr) FILTER (WHERE ok) AS t_wr,
       fsum(rch) FILTER (WHERE ok) AS t_rch, fsum(wch) FILTER (WHERE ok) AS t_wch
FROM (SELECT *, (pcc IS NOT NULL AND pcc = 0) AS rnone,
             NOT cut AND NOT {conflict} AND NOT {early} AND NOT (pcc IS NOT NULL AND pcc = 0) AS ok
      FROM cand)
GROUP BY host, pseq""".format(conflict=conflict, early=early)))

    def own(short):
        return ("CASE WHEN NOT j.io_dict THEN NULL WHEN j.{s} IS NULL THEN NULL "
                "WHEN j.io_scope IS DISTINCT FROM 'process' OR coalesce(t.n_ok, 0) = 0 THEN j.{s} "
                "ELSE greatest(0.0, j.{s} - coalesce(t.t_{s}, 0.0)) END AS own_{s}".format(s=short))
    short_of = {"rd_mb": "rd", "wr_mb": "wr", "rchar_mb": "rch", "wchar_mb": "wch"}
    stmts.append(("window: exits", """
CREATE OR REPLACE TEMP TABLE wx AS
SELECT j.*, {own},
       j.ts > {now} + {skew} AS future,
       CAST(floor(j.ts / {bin}) AS BIGINT) AS b,
       coalesce(t.n_ok, 0) > 0 AND (
         (j.rd IS NOT NULL AND coalesce(t.t_rd, 0) - j.rd > {tol}) OR
         (j.wr IS NOT NULL AND coalesce(t.t_wr, 0) - j.wr > {tol}) OR
         (j.rch IS NOT NULL AND coalesce(t.t_rch, 0) - j.rch > {tol}) OR
         (j.wch IS NOT NULL AND coalesce(t.t_wch, 0) - j.wch > {tol})) AS clamped
FROM j LEFT JOIN taken t ON t.host IS NOT DISTINCT FROM j.host AND t.pseq = j.seq
WHERE j.ts >= {since}""".format(
        own=", ".join(own(s) for s, _f in IO_FIELDS), now=lit(float(w["now"])),
        skew=lit(R["skew"]), bin=w["bin_s"], tol=lit(R["clamp_tol"]),
        since=lit(float(w["since"])))))
    # -- 3. the residency ticks, each host's per-class memory (class_totals)
    stmts.append(("ticks", """
CREATE OR REPLACE TEMP TABLE tk AS
SELECT src, ord, coalesce(nullif(host, ''), '?') AS host, ts_e AS ts,
       CAST(floor(ts_e / {bin}) AS BIGINT) AS b, a3map, atmap
FROM (SELECT *, coalesce(ts_epoch, epoch(timezone({tz}, TRY_STRPTIME(left(ts, 19),
             '%Y-%m-%d %H:%M:%S')))) AS ts_e FROM ({tick_src}) q)
WHERE event = 'residency_totals' AND ts_e IS NOT NULL AND ts_e >= {since}""".format(
        bin=w["bin_s"], tz=lit(tz), tick_src=tick_src, since=lit(float(w["since"])))))
    stmts.append(("ticks: classes", """
CREATE OR REPLACE TEMP TABLE tkc AS
WITH e AS (
  SELECT t.src, t.ord, t.host, t.b, u.e.key AS k, u.e.value AS v
  FROM tk t, unnest(map_entries(TRY_CAST(t.a3map AS MAP(VARCHAR, JSON)))) u(e)),
c AS (
  SELECT src, ord, host, b, CASE WHEN k IN {legacy} THEN 'ide' ELSE k END AS cls,
         fsum(CASE WHEN json_type(v) = 'OBJECT' AND json_type(v, '$.rss_mb') IN {num}
                   THEN TRY_CAST(json_extract(v, '$.rss_mb') AS DOUBLE) END) AS rss
  FROM e GROUP BY ALL),
m AS (
  SELECT t.src, t.ord, t.host, t.b, count(*) AS n_ide_types,
         fsum(CASE WHEN json_type(u.e.value, '$.rss_mb') IN {num}
                    AND TRY_CAST(json_extract(u.e.value, '$.rss_mb') AS DOUBLE) <> 0
                   THEN TRY_CAST(json_extract(u.e.value, '$.rss_mb') AS DOUBLE) END) AS mv
  FROM tk t, unnest(map_entries(TRY_CAST(t.atmap AS MAP(VARCHAR, JSON)))) u(e)
  WHERE u.e.key IN {counted} GROUP BY ALL),
both_ AS (
  SELECT coalesce(c.src, x.src) AS src, coalesce(c.ord, x.ord) AS ord, coalesce(c.host, x.host) AS host,
         coalesce(c.b, x.b) AS b, coalesce(c.cls, x.cls) AS cls, c.rss, m.mv
  FROM c FULL JOIN (SELECT m.src, m.ord, m.host, m.b, u.cls FROM m, (VALUES ('agent'), ('ide')) u(cls)) x
    ON x.src = c.src AND x.ord = c.ord AND x.cls = c.cls
  LEFT JOIN m ON m.src = coalesce(c.src, x.src) AND m.ord = coalesce(c.ord, x.ord))
SELECT src, ord, host, b, cls,
       CASE WHEN mv IS NULL THEN rss
            WHEN cls = 'agent' THEN greatest(0.0, coalesce(rss, 0.0) - mv)
            WHEN cls = 'ide' THEN coalesce(rss, 0.0) + mv
            ELSE rss END AS rss
FROM both_""".format(legacy="(%s)" % ", ".join(lit(x) for x in R["legacy"]), num=NUMERIC_JSON,
                     counted="(%s)" % ", ".join(lit(x) for x in R["counted_ides"]))))
    # -- 4. what the panels show, aggregated
    io_sql = ", ".join("count(own_{s}) AS n_{f}, fsum(own_{s}) AS s_{f}".format(s=short_of[f], f=f)
                       for f in R["io_series"])
    net_sql = ", ".join("count({c}) AS n_{f}, fsum({c} / 1e6) AS s_{f}".format(c=c, f=f)
                        for f, c in R["net_series"])
    stmts.append(("q: series by class", """
SELECT b, a3, count(cpu_s) AS n_cpu_s, fsum(cpu_s) AS s_cpu_s,
       count(sched_wait_s) AS n_sched_wait_s, fsum(sched_wait_s) AS s_sched_wait_s,
       count(dstate_wait_s) AS n_dstate_wait_s,
       count(maj_flt) AS n_maj_flt, sum(maj_flt) AS s_maj_flt, {io}, {net}
FROM wx WHERE NOT future GROUP BY b, a3 ORDER BY b, a3""".format(io=io_sql, net=net_sql)))
    stmts.append(("q: stall bins", """
SELECT b, coalesce(nullif(tool, ''), '(empty)') AS tool, fsum(dstate_wait_s) AS s
FROM wx WHERE NOT future AND dstate_wait_s IS NOT NULL AND dstate_wait_s <> 0
GROUP BY ALL ORDER BY ALL"""))
    stmts.append(("q: tick bins", """
SELECT b, count(*) AS ticks, count(DISTINCT host) AS hosts FROM tk
WHERE ts <= {now} + {skew} GROUP BY b ORDER BY b""".format(now=lit(float(w["now"])), skew=lit(R["skew"]))))
    stmts.append(("q: resident", """
WITH hb AS (SELECT b, host, count(*) AS n FROM tk WHERE ts <= {now} + {skew} GROUP BY ALL),
hc AS (SELECT tkc.b, tkc.host, tkc.cls, fsum(tkc.rss) AS s FROM tkc
       JOIN tk ON tk.src = tkc.src AND tk.ord = tkc.ord WHERE tk.ts <= {now} + {skew} GROUP BY ALL)
SELECT hb.b, cl.cls, fsum(coalesce(hc.s, 0.0) / hb.n) AS v, bool_or(hc.cls IS NOT NULL) AS present
FROM hb CROSS JOIN (SELECT DISTINCT cls FROM hc) cl
LEFT JOIN hc ON hc.b = hb.b AND hc.host = hb.host AND hc.cls = cl.cls
GROUP BY hb.b, cl.cls ORDER BY hb.b, cl.cls""".format(now=lit(float(w["now"])), skew=lit(R["skew"]))))
    stmts.append(("q: skipped", """
SELECT (SELECT count(*) FROM wx WHERE future) + (SELECT count(*) FROM tk WHERE ts > {now} + {skew}) AS future,
       (SELECT count(*) FROM wx) AS exits, (SELECT count(*) FROM tk) AS ticks,
       (SELECT count(*) FROM j) AS joined""".format(now=lit(float(w["now"])), skew=lit(R["skew"]))))
    acc = []
    for f in ("sched_run_s", "sched_wait_s") + R["fault_fields"] + R["delay_fields"] + \
            ("dstate_wait_s", "dstate_episodes"):
        acc.append("fsum({f}) AS s_{f}, count({f}) AS n_{f}".format(f=f))
    stmts.append(("q: by class", """
SELECT a3, count(*) AS events, fsum(cpu_s) AS cpu_sum, {acc},
       count(*) FILTER (WHERE dstate_wait_s > 0) AS dstate_events,
       greatest(0.0, coalesce(max(dstate_max_s), 0.0)) AS dstate_max,
       count(*) FILTER (WHERE duration_s > 0 AND sched_run_s IS NOT NULL
                        AND sched_run_s > duration_s * {f105} + {f001}) AS aside
FROM wx GROUP BY a3 ORDER BY a3""".format(acc=", ".join(acc), f105=lit(1.05), f001=lit(0.001))))
    stmts.append(("q: dstate src", """
SELECT a3, dstate_src, count(*) AS n FROM wx WHERE dstate_src IS NOT NULL AND dstate_src <> ''
GROUP BY ALL ORDER BY ALL"""))
    stmts.append(("q: impossible", " UNION ALL ".join(
        "SELECT a3, %s AS f, count(*) AS n FROM wx WHERE imp_%s GROUP BY a3" % (lit(n), n)
        for n, _k in EXIT_FIELDS if W(n))))
    # The quantile sketch (reducers.Quant, DDSketch): log buckets of every
    # value, then the value at each rank read off the cumulative counts.
    oncpu = ("duration_s > 0 AND sched_run_s IS NOT NULL AND NOT (sched_run_s > duration_s * %s + %s)"
             % (lit(1.05), lit(0.001)))
    stmts.append(("quants: values", """
CREATE OR REPLACE TEMP TABLE qv AS
SELECT a3, 'cpu_s' AS m, cpu_s AS v FROM wx
UNION ALL SELECT a3, 'peak_rss_mb', coalesce(peak_rss_mb, rss_mb) FROM wx
UNION ALL SELECT a3, 'sched_wait_s', sched_wait_s FROM wx
UNION ALL SELECT a3, 'duration_s', duration_s FROM wx
UNION ALL SELECT a3, 'on_cpu_frac', least(1.0, sched_run_s / duration_s) FROM wx WHERE {oncpu}
""".format(oncpu=oncpu)))
    stmts.append(("q: quant stats", """
SELECT a3, m, count(*) AS seen, count(v) AS n, fsum(v) AS total, min(v) AS lo, max(v) AS hi,
       count(*) FILTER (WHERE v = 0) AS n_zero
FROM qv GROUP BY ALL ORDER BY ALL"""))
    stmts.append(("q: quant ranks", """
WITH st AS (SELECT a3, m, count(v) AS n, min(v) AS lo, max(v) AS hi FROM qv GROUP BY ALL),
bk AS (
  SELECT a3, m, sg, i, count(*) AS c,
         CASE sg WHEN 1 THEN 2.0 * pow({g}, i) / ({g} + 1.0)
                 WHEN -1 THEN -(2.0 * pow({g}, i) / ({g} + 1.0)) ELSE 0.0 END AS val
  FROM (SELECT a3, m, CASE WHEN v > {tiny} THEN 1 WHEN v < -{tiny} THEN -1 ELSE 0 END AS sg,
               CASE WHEN v > {tiny} THEN CAST(ceil({ilg} * ln(v)) AS BIGINT)
                    WHEN v < -{tiny} THEN CAST(ceil({ilg} * ln(-v)) AS BIGINT) ELSE 0 END AS i
        FROM qv WHERE v IS NOT NULL)
  GROUP BY ALL),
cum AS (
  SELECT *, sum(c) OVER (PARTITION BY a3, m ORDER BY sg, CASE WHEN sg = -1 THEN -i ELSE i END
                         ROWS UNBOUNDED PRECEDING) AS cu,
         row_number() OVER (PARTITION BY a3, m ORDER BY sg, CASE WHEN sg = -1 THEN -i ELSE i END) AS pos
  FROM bk),
rk AS (SELECT st.a3, st.m, ps.k, CAST(trunc(ps.p * (st.n - 1)) AS BIGINT) AS r, st.lo, st.hi
       FROM st, (VALUES ('p1', {p01}), ('p10', {p10}), ('p25', {p25}), ('p50', {p50}),
                        ('p75', {p75}), ('p90', {p90}), ('p99', {p99})) ps(k, p) WHERE st.n > 0)
SELECT rk.a3, rk.m, rk.k, least(min(rk.hi), greatest(min(rk.lo), arg_min(cum.val, cum.pos))) AS q
FROM rk JOIN cum ON cum.a3 = rk.a3 AND cum.m = rk.m AND cum.cu > rk.r
GROUP BY ALL ORDER BY ALL""".format(
        g=lit(R["gamma"]), tiny=lit(R["tiny"]), ilg=lit(R["inv_lg"]), p01=lit(.01), p10=lit(.10),
        p25=lit(.25), p50=lit(.50), p75=lit(.75), p90=lit(.90), p99=lit(.99))))
    who = ("pid, ppid, usr, a3, agent_type, host, ts_str, tool, session_key, cpu_s, duration_s, "
           "exit_code, signal, args")
    stmts.append(("q: top", """
SELECT * FROM (
  SELECT m, v, {who}, row_number() OVER (PARTITION BY a3, m ORDER BY v DESC, ts, src, ord) AS rn
  FROM (SELECT 'cpu_s' AS m, cpu_s AS v, * FROM wx
        UNION ALL SELECT 'peak_rss_mb', coalesce(peak_rss_mb, rss_mb), * FROM wx
        UNION ALL SELECT 'sched_wait_s', sched_wait_s, * FROM wx
        UNION ALL SELECT 'duration_s', duration_s, * FROM wx) WHERE v IS NOT NULL)
WHERE rn = 1 ORDER BY a3, m""".format(who=who)))
    stmts.append(("q: stall by tool", """
SELECT coalesce(nullif(tool, ''), '(empty)') AS tool, fsum(dstate_wait_s) AS wait_s, count(*) AS n,
       sum(coalesce(dstate_episodes, 0)) AS episodes, greatest(0.0, max(coalesce(dstate_max_s, 0.0))) AS max_s,
       min(struct_pack(ts := ts, src := src, ord := ord)) AS first
FROM wx WHERE dstate_wait_s > 0 GROUP BY 1 ORDER BY wait_s DESC, first LIMIT 20"""))
    lim12 = R["instance_cap"]
    stmts.append(("q: worst stall", """
SELECT {who}, dstate_wait_s, dstate_episodes, dstate_max_s, dstate_src FROM wx
WHERE dstate_wait_s > 0 ORDER BY dstate_wait_s DESC, ts, src, ord LIMIT {n}""".format(who=who, n=lim12)))
    stmts.append(("q: worst fault", """
SELECT {who}, {ff} FROM wx WHERE maj_flt > 0 ORDER BY maj_flt DESC, ts, src, ord LIMIT {n}""".format(
        who=who, ff=", ".join(R["fault_fields"]), n=lim12)))
    stmts.append(("q: worst wait", """
SELECT {who}, sched_wait_s, sched_run_s FROM wx WHERE sched_wait_s > 0
ORDER BY sched_wait_s DESC, ts, src, ord LIMIT {n}""".format(who=who, n=lim12)))
    # -- 5. what the join did, for the write-up: the rules SQL leaves out (the
    #    parent cap, crowding past PER_PID) cannot have acted while these are 0.
    stmts.append(("q: join diagnostics", """
SELECT (SELECT count(*) FROM pops) AS pops, (SELECT count(*) FROM gives) AS gives,
       coalesce(sum(n_cand), 0) AS candidates, coalesce(sum(n_ok), 0) AS taken,
       coalesce(sum(n_aged), 0) AS aged_out, coalesce(sum(n_user), 0) AS other_user,
       coalesce(sum(n_early), 0) AS began_before,
       coalesce(sum(n_cand) FILTER (WHERE reaped_none), 0) AS held_for_reaped_none,
       coalesce(max(n_cand), 0) AS max_candidates_per_pop,
       count(*) FILTER (WHERE n_cand > {per_pid}) AS pops_past_per_pid,
       (SELECT count(*) FROM wx WHERE clamped) AS clamped_in_window
FROM taken""".format(per_pid=R["per_pid"])))
    stmts.append(("q: held parents", """
WITH runs AS (
  SELECT g.host, g.ppid, min(g.ts) AS t0, min(p.ts) AS t1
  FROM (SELECT gv.*, j.ts FROM gives gv JOIN j ON j.host IS NOT DISTINCT FROM gv.host AND j.seq = gv.seq) g
  ASOF LEFT JOIN (SELECT pp.*, j.ts FROM pops pp JOIN j ON j.host IS NOT DISTINCT FROM pp.host
                  AND j.seq = pp.seq) p
    ON g.host = p.host AND g.ppid = p.pid AND g.seq < p.seq
  GROUP BY g.host, g.ppid, p.seq),
ev AS (SELECT t0 AS t, 1 AS d FROM runs UNION ALL SELECT t1, -1 FROM runs WHERE t1 IS NOT NULL)
SELECT coalesce(max(held), 0) AS held_max,
       (SELECT count(*) FROM runs WHERE t1 IS NULL) AS never_popped
FROM (SELECT sum(d) OVER (ORDER BY t, d ROWS UNBOUNDED PRECEDING) AS held FROM ev)"""))
    return stmts


def run_statements(con, stmts, profile_path=None):
    """Execute every statement; the queries' rows by name, each statement's
    time, and the largest peak buffer memory DuckDB's profile reported."""
    out, times, peak = {}, {}, 0
    for name, sql in stmts:
        t = time.perf_counter()
        for part in [s for s in sql.split(";\n") if s.strip()]:
            cur = con.execute(part)
            if name.startswith("q:"):
                cols = [d[0] for d in cur.description]
                out[name[3:]] = [dict(zip(cols, r)) for r in cur.fetchall()]
            if profile_path and os.path.exists(profile_path):
                try:
                    with open(profile_path) as fh:
                        prof = json.load(fh)
                    peak = max(peak, int((prof.get("system") or {}).get("peak_buffer_memory") or 0))
                except (OSError, ValueError):
                    pass
        times[name] = time.perf_counter() - t
    return out, times, peak


# ------------------------------------------------- the payload, assembled
def _round(digits, rounding):
    """`round(x, digits)` for the payload, or the raw value with its digits
    for the comparison (`rounding` False)."""
    def f(x):
        if x is None:
            return None
        return round(x, digits) if rounding else ("raw", x, digits)
    return f


def assemble(res, w, rules, rounding=True):
    """The Resources payload from the queries' rows, as ResourceReducer.result()
    lays it out (less `own_io`, the join's own counters, and the `*_basis`
    notes /api/panels leaves out)."""
    from rc_dashboard import redact
    R = rules
    r2, r3, r4 = (_round(d, rounding) for d in (2, 3, 4))

    def acc(total, n, seen):
        return {"total": r3(total if total is not None else 0.0), "n": n,
                "coverage_pct": r2(100.0 * n / seen) if seen else None,
                "mean": r4((total or 0.0) / n) if n else None}

    def who(row):
        return {"pid": row["pid"], "ppid": row["ppid"], "user": row["usr"], "actor3": row["a3"],
                "agent_type": row["agent_type"], "host": row["host"], "ts": row["ts_str"],
                "tool": row["tool"], "session_key": row["session_key"], "cpu_s": row["cpu_s"],
                "duration_s": row["duration_s"], "exit_code": row["exit_code"],
                "signal": row["signal"], "args": redact.redact((row["args"] or "")[:220])}

    qstats = {(r["a3"], r["m"]): r for r in res["quant stats"]}
    qranks = {}
    for r in res["quant ranks"]:
        qranks.setdefault((r["a3"], r["m"]), {})[r["k"]] = r["q"]

    def quant(a3, m):
        st = qstats.get((a3, m))
        if st is None or not st["n"]:
            seen = st["seen"] if st else 0
            return {"n": 0, "coverage_pct": 0.0 if seen else None, "sampled": False}
        n = st["n"]
        out = {"n": n}
        for k in ("p1", "p10", "p25", "p50", "p75", "p90", "p99"):
            out[k] = r4(qranks[(a3, m)][k])
        all_zero = st["n_zero"] == n
        out.update({"min": r4(st["lo"]), "max": r4(st["hi"]), "mean": r4(st["total"] / n),
                    "sampled": False, "sample_n": n, "rel_err": R["rel_err"],
                    "zero_pct": r2(100.0 * st["n_zero"] / n), "all_zero": all_zero,
                    "suspect": ("every value is exactly 0 -- treat as not measured, "
                                "not as a measured zero" if all_zero else None),
                    "coverage_pct": r2(100.0 * n / st["seen"]) if st["seen"] else None})
        return out

    tops = {}
    for r in res["top"]:
        tops.setdefault(r["a3"], {})[r["m"]] = dict(who(r), value=r4(r["v"]))
    srcs = {}
    for r in res["dstate src"]:
        srcs.setdefault(r["a3"], {})[r["dstate_src"]] = r["n"]
    imp = {}
    for r in res["impossible"]:
        imp.setdefault(r["a3"], {})[r["f"]] = r["n"]
    by_class = {}
    for r in res["by class"]:
        a3, ev = r["a3"], r["events"]

        def a(f, r=r, ev=ev):
            return acc(r["s_" + f], r["n_" + f], ev)
        by_class[a3] = {
            "events": ev, "cpu_s": r2(r["cpu_sum"] if r["cpu_sum"] is not None else 0.0),
            "sched_run_s": a("sched_run_s"), "sched_wait_s": a("sched_wait_s"),
            "quants": {m: quant(a3, m) for m in ("cpu_s", "peak_rss_mb", "sched_wait_s",
                                                  "duration_s", "on_cpu_frac")},
            "top": tops.get(a3, {}), "oncpu_set_aside": r["aside"],
            "faults": {k: a(k) for k in R["fault_fields"]},
            "delays": {k: a(k) for k in R["delay_fields"]},
            "dstate": {"events_with_stall": r["dstate_events"], "wait_s": a("dstate_wait_s"),
                       "episodes": a("dstate_episodes"), "max_s": r2(r["dstate_max"]),
                       "src": srcs.get(a3, {}),
                       "coverage_pct": r2(100.0 * r["n_dstate_wait_s"] / ev) if ev else 0.0}}
    stalls = [dict(tool=r["tool"], wait_s=r2(r["wait_s"]), n=r["n"], episodes=r["episodes"],
                   max_s=r2(r["max_s"])) for r in res["stall by tool"]]
    worst_stall = [dict(who(r), dstate_wait_s=r["dstate_wait_s"],
                        dstate_episodes=r["dstate_episodes"], dstate_max_s=r["dstate_max_s"],
                        dstate_src=r["dstate_src"]) for r in res["worst stall"]]
    worst_fault = [dict(who(r), **{k: r[k] for k in R["fault_fields"]}) for r in res["worst fault"]]
    worst_wait = [dict(who(r), sched_wait_s=r["sched_wait_s"], sched_run_s=r["sched_run_s"])
                  for r in res["worst wait"]]
    return {"by_class": by_class, "stall_by_tool": stalls, "worst_stall": worst_stall,
            "worst_fault": worst_fault, "worst_wait": worst_wait, "scope": R["scope"],
            "series": _series(res, w, R, [s["tool"] for s in stalls][:R["stall_tools"]],
                              r2, r3),
            "impossible_waits": imp}


def _series(res, w, R, stall_tools, r2, r3):
    """ResourceReducer.series: every bin from the first held to the last, each
    class's value null where it had no record (a gap, never a 0)."""
    bin_s = w["bin_s"]
    skipped = res["skipped"][0]
    out = {"bin_s": bin_s, "t": [], "by_class": {}, "stall_by_tool": {}, "stall_other": [],
           "resident": {}, "skipped": {"no_ts": 0, "future": skipped["future"],
                                       "clipped_bins": 0}}
    rows = res["series by class"]
    ticks = {r["b"] for r in res["tick bins"]}
    held = {r["b"] for r in rows} | ticks
    if not held:
        return out
    lo, hi = min(held), max(held)
    if hi - lo + 1 > R["span_cap"]:
        out["skipped"]["clipped_bins"] = sum(1 for b in held if b <= hi - R["span_cap"])
        lo = hi - R["span_cap"] + 1
    idx = range(lo, hi + 1)
    out["t"] = [b * bin_s for b in idx]
    by = {}
    for r in rows:
        by.setdefault(r["b"], {})[r["a3"]] = r
    fields = R["class_fields"] + R["io_series"] + tuple(k for k, _f in R["net_series"])
    for c in sorted({c for b in idx for c in by.get(b, ())}):
        row = {k: [] for k in fields}
        for b in idx:
            s = by.get(b, {}).get(c)
            for k in fields:
                row[k].append(r3(s["s_" + k]) if s and s["n_" + k] else None)
        out["by_class"][c] = row
    measured = [any(s["n_dstate_wait_s"] for s in by.get(b, {}).values()) for b in idx]
    stall = {}
    for r in res["stall bins"]:
        stall.setdefault(r["b"], {})[r["tool"]] = r["s"]
    for tool in stall_tools:
        out["stall_by_tool"][tool] = [r3(stall.get(b, {}).get(tool, 0)) if m else None
                                      for b, m in zip(idx, measured)]
    named = set(stall_tools)
    out["stall_other"] = [r3(sum(v for t, v in stall.get(b, {}).items() if t not in named))
                          if m else None for b, m in zip(idx, measured)]
    # Per host, the mean over its ticks in the bin, summed over hosts; a class a
    # tick did not list counts 0 there, and a bin no host ticked in is null.
    res_by, present = {}, {}
    for r in res["resident"]:
        res_by.setdefault(r["cls"], {})[r["b"]] = r["v"]
        if r["present"]:
            present.setdefault(r["cls"], set()).add(r["b"])
    for c in sorted(cl for cl, bins in present.items() if any(lo <= b <= hi for b in bins)):
        out["resident"][c] = {"rss_mb": [r2(res_by[c].get(b, 0.0)) if b in ticks else None
                                         for b in idx]}
    return out


# -------------------------------------------------------------- `sql` command
def evaluate(con, p, w, mode, rules, tz, profile_path=None):
    """One window, one join reach: the payloads (rounded, and raw for the
    comparison), the rows the join diagnostics hold, and the times."""
    if mode == "window":
        join_from, chain = w["since"], False
    elif mode == "chain":
        join_from, chain = w["chain_from"], True
    else:                                   # all: the whole store, never aged
        join_from, chain = -1e18, False
    first_day = time.strftime("%Y-%m-%d", time.localtime(max(0.0, min(join_from, w["since"]) - 86400)))
    t0 = time.perf_counter()
    srcs = sources(p, first_day if join_from > 0 else "0000")
    stmts = build_sql(con, srcs, w, join_from, chain, rules, tz)
    res, times, peak = run_statements(con, stmts, profile_path)
    t1 = time.perf_counter()
    payload = assemble(res, w, rules, rounding=True)
    raw = assemble(res, w, rules, rounding=False)
    t2 = time.perf_counter()
    return {"payload": payload, "raw": raw, "join": res["join diagnostics"][0],
            "held": res["held parents"][0], "counts": res["skipped"][0],
            "files": {"replica": sum(1 for s in srcs if s["replica"]),
                      "jsonl": sum(1 for s in srcs if not s["replica"])},
            "seconds": t2 - t0, "sql_s": t1 - t0, "assemble_s": t2 - t1,
            "statements_s": times, "duckdb_peak_buffer": peak}


def cmd_sql(a):
    """Evaluate one window in this fresh process, cold then warm; write JSON."""
    rss0 = maxrss()
    import duckdb
    from rc_dashboard import redact  # noqa: F401  (loaded before the baseline is read)
    rules = sql_rules()
    p = paths(a.work)
    meta = load_feed_meta(p)
    now = meta["stamp"]["written_to"]
    con = duckdb.connect()
    con.execute("SET threads = %d" % a.threads)
    prof = os.path.join(tempfile.mkdtemp(prefix="spike-prof-"), "profile.json")
    con.execute("PRAGMA enable_profiling = 'json'")
    con.execute("PRAGMA profiling_output = %s" % lit(prof))
    # every statement, the CREATE TABLE ... AS that do the work included
    con.execute("SET profiling_coverage = 'ALL'")
    newest = newest_record(con, p, meta)
    w = window_params(a.window, now, newest)
    mode = a.mode or ("chain" if w["path"] == "merge" else "window")
    if mode == "chain" and w["chain_from"] is None:
        raise Fatal("the %d min window has no closed hour: it is a scan, not a merge" % a.window)
    rss_ready = maxrss()
    runs = []
    for phase in ("cold", "warm"):
        got = evaluate(con, p, w, mode, rules, local_zone(), prof)
        got["phase"] = phase
        got["maxrss"] = maxrss()
        mem = con.execute("SELECT sum(memory_usage_bytes) FROM duckdb_memory()").fetchone()[0]
        got["duckdb_memory_after"] = int(mem or 0)
        runs.append(got)
    out = {"window": w, "mode": mode, "threads": a.threads, "maxrss_start": rss0,
           "maxrss_ready": rss_ready, "duckdb": duckdb.__version__,
           "runs": [{k: v for k, v in r.items() if k not in ("payload", "raw")} for r in runs],
           "payload": runs[0]["payload"], "raw": runs[0]["raw"],
           "warm_same": runs[0]["payload"] == runs[1]["payload"]}
    with open(a.out, "w") as fh:
        json.dump(out, fh, default=str)


def local_zone():
    """The IANA name of this process's zone, which a record's local `ts` string
    is read in when it has no `ts_epoch` (normalize.epoch_of): TZ, else what
    /etc/localtime links to, else UTC."""
    tz = os.environ.get("TZ", "").lstrip(":")
    if tz:
        return tz
    try:
        target = os.path.realpath("/etc/localtime")
        if "zoneinfo/" in target:
            return target.split("zoneinfo/", 1)[1]
    except OSError:
        pass
    return "UTC"


def newest_record(con, p, meta):
    """The newest record's time in the newest day, as HourStore.closed_end asks
    the files (clock.newest_record): what closes the hours."""
    day = meta["newest_day"]
    files = [os.path.join(p["ebpf"], day, n) for n in sorted(os.listdir(os.path.join(p["ebpf"], day)))
             if n.endswith((".jsonl", ".jsonl.gz"))]
    best = None
    for f in files:
        got = con.execute("SELECT max(TRY_CAST(json_extract(json, '$.ts_epoch') AS DOUBLE)) FROM "
                          "read_json_objects(%s, format = 'newline_delimited', ignore_errors = true)"
                          % lit(f)).fetchone()[0]
        if got is not None and (best is None or got > best):
            best = got
    return best


# ---------------------------------------------------------- `reducer` command
def cmd_reducer(a):
    """The service's ResourceReducer alone, over its own window scan (no merge):
    one family's cost in Python. Cold then warm, in this fresh process."""
    rss0 = maxrss()
    p = paths(a.work)
    meta = load_feed_meta(p)
    os.environ.update({"RC_DASH_CONFIG": p["conf"], "RC_DASH_EBPF_ROOTS": p["ebpf"],
                       "RC_DASH_NODE_ROOTS": p["node"],
                       "RC_DASH_AS_OF": repr(float(meta["stamp"]["written_to"]))})
    from rc_dashboard import clock, config, jsonout, redact
    from rc_dashboard.export import window_plan, window_scan
    from rc_dashboard.normalize import Normalizer
    from rc_dashboard.reducers.resources import ResourceReducer
    cfg = config.load()
    clock.install(cfg, [p["ebpf"]])
    window_s = a.window * 60
    runs = []
    for phase in ("cold", "warm"):
        t = time.perf_counter()
        since = time.time() - window_s
        red = ResourceReducer(cfg, ["agent", "human", "ide", "unlabeled"], window_s=window_s)
        norm = Normalizer(cfg)
        plan, total = window_plan([p["ebpf"]], since, int(window_s // 86400) + 2)
        n = 0
        for rec, _stats in window_scan(plan, norm, since, events=ResourceReducer.EVENTS):
            red.feed(rec)
            n += 1
        payload = jsonout.strip_basis(redact.payload(red.result()))
        runs.append({"phase": phase, "seconds": time.perf_counter() - t, "records": n,
                     "bytes_planned": total, "maxrss": maxrss()})
    with open(a.out, "w") as fh:
        json.dump({"window_min": a.window, "maxrss_start": rss0, "runs": runs,
                   "payload": payload}, fh, default=str)


# ------------------------------------------------------------- the service
def service_env(p, meta, minutes_default, prewarm):
    import e2e_history as e2e
    as_of = meta["stamp"]["written_to"]
    span_min = int(math.ceil((as_of - meta["stamp"]["start"]) / 60.0))
    full = span_min + 60
    env = e2e.clean_env()
    env.update({
        "RC_DASH_CONFIG": p["conf"], "RC_DASH_EBPF_ROOTS": p["ebpf"], "RC_DASH_NODE_ROOTS": p["node"],
        "RC_DASH_EBPF_DAYS": str(meta["days"] + 2), "RC_DASH_AS_OF": repr(float(as_of)),
        "RC_DASH_HOST": "127.0.0.1", "RC_DASH_RETENTION_MIN": str(full),
        "RC_DASH_BACKFILL_HOURS": str(int(math.ceil(full / 60.0))),
        "RC_DASH_LIVE_WINDOW_MIN": str(minutes_default),
        "RC_DASH_PREWARM": ",".join(str(m) for m in prewarm),
        "RC_DASH_MAX_WINDOWS": "8", "RC_DASH_MERGE_REDUCE": "1", "RC_DASH_ASK": "0",
        "RC_DASH_OPEN": "1", "RC_DASH_RDNS": "0",
        "RC_DASH_STATIC_DIR": os.path.join(p["work"], "not-set-up"),
        "PYTHONHASHSEED": "0",
        # the store converted once, before: every sealed file's replica is current
        "RC_DASH_HISTORY": "1", "RC_DASH_HISTORY_DIR": p["history"],
        "RC_DASH_HISTORY_WORKERS": "2"})
    return env


def resources_of(resp):
    body = (resp.get("panels") or {}).get("resources") or {}
    return {k: v for k, v in body.items() if not k.startswith("_")}, body.get("_window") or {}


def service_run(p, meta, label, windows, timeout):
    """One cache, `windows[0]` the default: cold, then warm over what it saved."""
    import e2e_history as e2e
    base = os.path.join(p["service"], label)
    cache = os.path.join(base, "cache")
    shutil.rmtree(base, ignore_errors=True)
    os.makedirs(base)
    svc = e2e.Service(label, service_env(p, meta, windows[0], windows[1:]), cache, base)
    out = {"label": label, "windows": list(windows), "phases": {}}
    try:
        for phase in ("cold", "warm"):
            t0 = time.monotonic()
            svc.start()
            e2e.wait_backfill([svc], timeout)
            t_backfill = time.monotonic() - t0
            first_panels = {}
            for m in windows:
                def has_panels(m=m):
                    body, _st = resources_of(svc.json("/api/panels?window_min=%d" % m, 60))
                    return "series" in body
                svc.wait(has_panels, "the %d min panels" % m, timeout)
                first_panels[m] = time.monotonic() - t0
            e2e.wait_views([svc], [(m, {}) for m in windows], timeout)
            t_ready = time.monotonic() - t0
            mem = proc_mem(svc.proc.pid)
            st = svc.json("/api/windows", 60)
            views = {}
            for v in st.get("held") or []:
                if v.get("filter"):
                    continue
                views[v["minutes"]] = {"state": v.get("state"),
                                       "elapsed_s": (v.get("progress") or {}).get("elapsed_s"),
                                       "mode": (v.get("progress") or {}).get("mode"),
                                       "records": (v.get("progress") or {}).get("records"),
                                       "source": (v.get("cache") or {}).get("source"),
                                       "note": (v.get("cache") or {}).get("note")}
            panels = {}
            for m in windows:
                body, wst = resources_of(svc.json("/api/panels?window_min=%d" % m, 120))
                panels[m] = body
            mem_after = proc_mem(svc.proc.pid)
            out["phases"][phase] = {"to_backfill_s": t_backfill, "to_first_panels_s": first_panels,
                                    "to_ready_s": t_ready, "mem_ready": mem, "mem_after": mem_after,
                                    "views": views, "hours": st.get("hours"), "panels": panels}
            svc.stop()
    finally:
        svc.stop()
    out["cache"] = {sub: du(os.path.join(cache, sub)) for sub in sorted(os.listdir(cache))
                    if os.path.isdir(os.path.join(cache, sub))} if os.path.isdir(cache) else {}
    out["cache_total"] = du(cache) if os.path.isdir(cache) else {"bytes": 0, "files": 0}
    return out


# --------------------------------------------------------------- the diff
def numeric(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def compare(svc, sql, raw, path=(), out=None):
    """Every value of the service's payload against the SQL's: `(kind, path,
    service, sql)`. kinds: `equal`, `tie` (the last rounded digit, the SQL's
    unrounded value at a rounding tie: float sums in another order), and the
    differences: `value`, `null` (one side null), `missing` (a key or list
    item one side lacks), `type`. Dicts are compared by key, in any order;
    numbers by value, an int equal to a float."""
    out = [] if out is None else out
    where = "/".join(str(x) for x in path) or "/"
    if isinstance(svc, dict) and isinstance(sql, dict):
        for k in list(svc) + [k for k in sql if k not in svc]:
            if k not in sql or k not in svc:
                out.append(("missing", where + "/" + str(k), svc.get(k, "<absent>"),
                            sql.get(k, "<absent>")))
                continue
            compare(svc[k], sql[k], raw.get(k) if isinstance(raw, dict) else None,
                    path + (k,), out)
        return out
    if isinstance(svc, list) and isinstance(sql, list):
        if len(svc) != len(sql):
            out.append(("missing", where + " (length)", len(svc), len(sql)))
        rl = raw if isinstance(raw, list) else [None] * len(sql)
        for i, (x, y) in enumerate(zip(svc, sql)):
            compare(x, y, rl[i] if i < len(rl) else None, path + (i,), out)
        return out
    if svc is None or sql is None:
        out.append(("equal" if svc is None and sql is None else "null", where, svc, sql))
        return out
    if numeric(svc) and numeric(sql):
        if svc == sql:
            out.append(("equal", where, svc, sql))
            return out
        if isinstance(raw, (list, tuple)) and len(raw) == 3 and raw[0] == "raw":
            _tag, x, d = raw
            if abs(x - svc) <= 0.5 * 10 ** -d * (1 + 1e-6) + 1e-12:
                out.append(("tie", where, svc, sql))
                return out
        out.append(("value", where, svc, sql))
        return out
    if type(svc) is not type(sql) and not (numeric(svc) and numeric(sql)):
        out.append(("type", where, svc, sql))
        return out
    out.append(("equal" if svc == sql else "value", where, svc, sql))
    return out


def summarize(found):
    kinds = {}
    for k, *_rest in found:
        kinds[k] = kinds.get(k, 0) + 1
    return kinds


def where_kind(path):
    """Which part of the payload a path is in, for the summary."""
    parts = path.split("/")
    if parts[0] == "series" and len(parts) > 1:
        return "series/" + parts[1]
    if parts[0] == "by_class" and len(parts) > 2:
        return "by_class/" + parts[2]
    return parts[0]


# -------------------------------------------------------------- the run
def sub(args, timeout):
    t = time.monotonic()
    got = subprocess.run([sys.executable, os.path.abspath(__file__)] + args, cwd=DASH,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
    if got.returncode != 0:
        raise Fatal("%s failed:\n%s" % (" ".join(args[:3]), got.stdout.decode("utf-8", "replace")[-4000:]))
    return time.monotonic() - t


def cmd_run(a):
    work = a.work or tempfile.mkdtemp(prefix="rc-store-spike-")
    p = paths(work)
    log("store spike in %s" % work)
    if a.reuse and os.path.exists(os.path.join(p["results"], "feed.json")):
        meta = load_feed_meta(p)
        log("feed: reused (%d day(s), %d host(s))" % (meta["days"], meta["hosts"]))
    else:
        if os.path.exists(p["demo"]):
            raise Fatal("%s already holds a feed: give --reuse, or another --work" % work)
        meta = make_feed(p, a.days, a.hosts, a.perturb)
    os.makedirs(p["results"], exist_ok=True)
    windows = [int(x) for x in a.windows.split(",")]
    threads = os.cpu_count() or 1
    summary = {"feed": meta, "windows": windows, "sql": {}, "reducer": {}, "service": {},
               "diff": {}}
    # -- the SQL, each window in a process of its own, cold then warm
    for m in windows:
        for t in sorted({threads, 1}, reverse=True):
            outp = os.path.join(p["results"], "sql-%d-t%d.json" % (m, t))
            sub(["sql", "--work", work, "--window", str(m), "--threads", str(t), "--out", outp],
                a.timeout)
            with open(outp) as fh:
                got = json.load(fh)
            summary["sql"]["%d/t%d" % (m, t)] = got
            r = got["runs"]
            log("sql %5d min, %d thread(s), %s join: cold %s, warm %s, peak RSS %s (%s at start), "
                "DuckDB peak buffer %s" % (m, t, got["mode"], secs(r[0]["seconds"]), secs(r[1]["seconds"]),
                                           mb(r[1]["maxrss"]), mb(got["maxrss_ready"]),
                                           mb(max(x["duckdb_peak_buffer"] for x in r))))
        # the window's join read as a scan reads it, against the reducer alone
        if summary["sql"]["%d/t%d" % (m, threads)]["mode"] != "window":
            outp = os.path.join(p["results"], "sql-%d-window.json" % m)
            sub(["sql", "--work", work, "--window", str(m), "--threads", str(threads),
                 "--mode", "window", "--out", outp], a.timeout)
            with open(outp) as fh:
                summary["sql"]["%d/window" % m] = json.load(fh)
        outp = os.path.join(p["results"], "sql-%d-all.json" % m)
        sub(["sql", "--work", work, "--window", str(m), "--threads", str(threads),
             "--mode", "all", "--out", outp], a.timeout)
        with open(outp) as fh:
            summary["sql"]["%d/all" % m] = json.load(fh)
    # -- the reducer alone
    if not a.skip_reducer:
        for m in windows:
            outp = os.path.join(p["results"], "reducer-%d.json" % m)
            sub(["reducer", "--work", work, "--window", str(m), "--out", outp], a.timeout)
            with open(outp) as fh:
                got = json.load(fh)
            summary["reducer"][str(m)] = got
            r = got["runs"]
            log("reducer %5d min: cold %s, warm %s, %d records, peak RSS %s" % (
                m, secs(r[0]["seconds"]), secs(r[1]["seconds"]), r[0]["records"], mb(r[1]["maxrss"])))
    # -- the service: each window alone in an empty cache, then all three
    if not a.skip_service:
        runs = [("w%d" % m, [m]) for m in windows]
        if len(windows) > 1:
            dflt = 1440 if 1440 in windows else windows[0]
            runs.append(("all", [dflt] + [m for m in windows if m != dflt]))
        for label, ws in runs:
            log("service %s: windows %s, cold then warm" % (label, ", ".join(map(str, ws))))
            got = service_run(p, meta, label, ws, a.timeout)
            summary["service"][label] = got
            for phase in ("cold", "warm"):
                ph = got["phases"][phase]
                log("  %-4s ready in %s (backfill %s); builds %s; RSS %s, peak %s" % (
                    phase, secs(ph["to_ready_s"]), secs(ph["to_backfill_s"]),
                    ", ".join("%d min %s %s" % (m, ph["views"].get(m, {}).get("source"),
                                                secs(ph["views"].get(m, {}).get("elapsed_s")))
                              for m in ws),
                    mb(ph["mem_ready"]["rss"]), mb(ph["mem_ready"]["hwm"])))
            log("  cache: %s" % ", ".join("%s %s" % (k, mb(v["bytes"])) for k, v in got["cache"].items()))
            with open(os.path.join(p["results"], "service-%s.json" % label), "w") as fh:
                json.dump(got, fh, default=str)
    rc = diff_all(summary, windows, threads)
    with open(os.path.join(p["results"], "summary.json"), "w") as fh:
        json.dump({k: v for k, v in summary.items()}, fh, default=str)
    report(summary, windows, threads, p)
    if not a.keep and not a.work:
        shutil.rmtree(work, ignore_errors=True)
    return rc


def diff_all(summary, windows, threads):
    """Every comparison the write-up reports; 0 when SQL matches the service."""
    rc = 0
    norm = lambda o: json.loads(json.dumps(o, default=str))          # noqa: E731

    def panel(run, phase, m):
        got = run["phases"][phase]["panels"]
        return norm(got.get(m) if m in got else got.get(str(m)))
    for m in windows:
        sq = summary["sql"]["%d/t%d" % (m, threads)]
        d = summary["diff"][str(m)] = {}
        svc = summary["service"].get("w%d" % m)
        if svc:
            cold = panel(svc, "cold", m)
            own = cold.pop("own_io", None)
            found = compare(cold, norm(sq["payload"]), norm(sq["raw"]))
            d["sql_vs_service"] = {"kinds": summarize(found), "own_io": own,
                                   "first": [f for f in found if f[0] not in ("equal", "tie")][:25],
                                   "ties": [f for f in found if f[0] == "tie"][:10],
                                   "by_part": by_part(found)}
            if any(k not in ("equal", "tie") for k, *_r in found):
                rc = 1
            warm = panel(svc, "warm", m)
            warm.pop("own_io", None)
            found = compare(cold, warm, None)
            d["service_warm_vs_cold"] = {"kinds": summarize(found),
                                         "first": [f for f in found if f[0] != "equal"][:10]}
        allrun = summary["service"].get("all")
        if allrun and svc:
            both = panel(allrun, "cold", m)
            both.pop("own_io", None)
            found = compare(cold, both, None)
            d["service_one_cache_vs_alone"] = {"kinds": summarize(found), "by_part": by_part(found),
                                               "first": [f for f in found if f[0] != "equal"][:10]}
        red = summary["reducer"].get(str(m))
        scan = summary["sql"].get("%d/window" % m) or (sq if sq["mode"] == "window" else None)
        if red and scan:
            rp = norm(red["payload"])
            rp.pop("own_io", None)
            found = compare(rp, norm(scan["payload"]), norm(scan["raw"]))
            d["sql_scan_vs_reducer"] = {"kinds": summarize(found), "by_part": by_part(found),
                                        "first": [f for f in found if f[0] not in ("equal", "tie")][:10]}
        alls = summary["sql"].get("%d/all" % m)
        if alls:
            found = compare(norm(sq["payload"]), norm(alls["payload"]), None)
            d["sql_all_vs_service_reach"] = {"kinds": summarize(found), "by_part": by_part(found)}
    return rc


def by_part(found):
    out = {}
    for k, where, *_r in found:
        part = where_kind(where)
        out.setdefault(part, {})
        out[part][k] = out[part].get(k, 0) + 1
    return out


def report(summary, windows, threads, p):
    meta = summary["feed"]
    st = meta["stats"]
    e = st["ebpf"]
    log("")
    log("== the feed ==")
    log("%d day(s) x %d host(s)%s; eBPF records %d (%d sealed, %d plain), node tier %d" % (
        meta["days"], meta["hosts"], " (perturbed)" if meta.get("perturbed") else "",
        e["records_sealed"] + e["records_plain"], e["records_sealed"], e["records_plain"],
        st["node"]["records"]))
    log("by event: %s" % ", ".join("%s %d" % kv for kv in sorted(e["records"].items(),
                                                               key=lambda kv: -kv[1])))
    log("eBPF day-files: sealed %s gz (%s inflated), plain %s; Parquet store %s (%s in the two "
        "tables read here); node tier %s" % (
            mb(e["sealed_gz"]), mb(e["sealed_inflated"]), mb(e["plain"]), mb(st["store"]["bytes"]),
            mb(st["store"]["read_by_spike"]), mb(st["node"]["bytes"])))
    log("")
    log("== time and memory, per window ==")
    log("| window | path | service cold build | service warm | service RSS cold / warm (peak) | "
        "reducer alone cold / warm (peak RSS) | SQL cold / warm, %d threads | SQL 1 thread cold | "
        "SQL peak RSS | DuckDB peak buffer |" % threads)
    log("|---|---|---|---|---|---|---|---|---|---|")
    for m in windows:
        sq = summary["sql"]["%d/t%d" % (m, threads)]
        s1 = summary["sql"].get("%d/t1" % m) or sq
        svc = summary["service"].get("w%d" % m, {}).get("phases", {})
        red = summary["reducer"].get(str(m), {}).get("runs") or [{}, {}]
        c, wm = svc.get("cold", {}), svc.get("warm", {})
        cv = (c.get("views") or {}).get(str(m)) or (c.get("views") or {}).get(m) or {}
        wv = (wm.get("views") or {}).get(str(m)) or (wm.get("views") or {}).get(m) or {}
        log("| %s | %s | %s (ready %s) | %s %s (ready %s) | %s / %s (%s / %s) | %s / %s (%s) | "
            "%s / %s | %s | %s | %s |" % (
                m, sq["window"]["path"], secs(cv.get("elapsed_s")), secs(c.get("to_ready_s")),
                secs(wv.get("elapsed_s")), wv.get("source") or "", secs(wm.get("to_ready_s")),
                mb((c.get("mem_ready") or {}).get("rss")), mb((wm.get("mem_ready") or {}).get("rss")),
                mb((c.get("mem_ready") or {}).get("hwm")), mb((wm.get("mem_ready") or {}).get("hwm")),
                secs(red[0].get("seconds")), secs(red[1].get("seconds")), mb(red[1].get("maxrss")),
                secs(sq["runs"][0]["seconds"]), secs(sq["runs"][1]["seconds"]),
                secs(s1["runs"][0]["seconds"]), mb(sq["runs"][1]["maxrss"]),
                mb(max(r["duckdb_peak_buffer"] for r in sq["runs"]))))
    log("")
    log("== disk ==")
    for label, got in summary["service"].items():
        log("service %-5s cache %s: %s" % (label, mb(got["cache_total"]["bytes"]), ", ".join(
            "%s %s (%d files)" % (k, mb(v["bytes"]), v["files"]) for k, v in got["cache"].items())))
    log("store (shared by both paths): %s" % ", ".join(
        "%s %s" % (k, mb(v["bytes"])) for k, v in st["store"]["tables"].items()))
    log("")
    log("== diff ==")
    for m in windows:
        d = summary["diff"].get(str(m), {})
        for name, got in d.items():
            log("%5d min %-28s %s" % (m, name, json.dumps(got.get("kinds"))))
            for f in got.get("first") or []:
                log("        %s" % json.dumps(f, default=str)[:300])


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    cmds = ap.add_subparsers(dest="cmd", required=True)
    r = cmds.add_parser("run", help="the whole experiment (module docstring)")
    r.add_argument("--days", type=int, default=8, help="days of demo history (8: a week and the "
                   "day before it, so the 7 d window's joins reach back past its start)")
    r.add_argument("--hosts", type=int, default=6, help="demo login nodes (6, the most)")
    r.add_argument("--perturb", action="store_true", help="exercise the rules the demo never "
                   "triggers (module docstring)")
    r.add_argument("--work", help="where everything goes (a new temp dir, removed unless --keep)")
    r.add_argument("--keep", action="store_true", help="keep the temp dir")
    r.add_argument("--reuse", action="store_true", help="reuse the feed already in --work")
    r.add_argument("--windows", default=",".join(map(str, WINDOWS)), help="minutes (60,1440,10080)")
    r.add_argument("--skip-service", action="store_true")
    r.add_argument("--skip-reducer", action="store_true")
    r.add_argument("--timeout", type=int, default=1800, help="seconds per wait (1800)")
    s = cmds.add_parser("sql", help="internal: one window's SQL, cold then warm, in this process")
    s.add_argument("--work", required=True)
    s.add_argument("--window", type=int, required=True)
    s.add_argument("--threads", type=int, default=os.cpu_count() or 1)
    s.add_argument("--mode", choices=("window", "chain", "all"),
                   help="how far back the own-I/O join reads: the window (a scan), the hour chain "
                        "(a merge, the default where the service merges), or the whole store")
    s.add_argument("--out", required=True)
    d = cmds.add_parser("reducer", help="internal: the ResourceReducer alone, in this process")
    d.add_argument("--work", required=True)
    d.add_argument("--window", type=int, required=True)
    d.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    try:
        if a.cmd == "run":
            return cmd_run(a)
        if a.cmd == "sql":
            cmd_sql(a)
            return 0
        cmd_reducer(a)
        return 0
    except Fatal as e:
        log("ERROR: %s" % e)
        return 2
    except urllib.error.URLError as e:
        log("ERROR: %s" % e)
        return 2


if __name__ == "__main__":
    sys.exit(main())
