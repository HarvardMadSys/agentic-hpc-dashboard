#!/usr/bin/env python3
"""The dashboard's read paths timed on each twin of one feed: JSONL against EBR.

    cd dashboard && uv run python tests/bench_formats.py [--days 3] [--hosts 4]
                        [--reps 3] [--gzip] [--json] [--keep] [--dir DEMO_DIR]

Writes a demo feed in both formats (`demo --demo-format both --demo-write-only`,
or reads the one `--dir` names), then times, for each format, the reads a
service makes, in this process and on a warm page cache:

  backfill        `FileTailSource.position` over the whole feed, then
                  `tail.read_back` of all of it, newest first (what every start
                  reads into the live tier); raw records, nothing normalised
  window 24 h     `export.window_plan` + `window_scan(EVENTS)` of the last 24 h,
                  normalised (a window build's read, before its reducers)
  hour            `hours.read_span` of one whole hour, EVENTS, normalised (what
                  reducing an hour reads)
  scan user ...   `export.scan` of the busiest user's exits, newest first: the
                  first 500 (an /api/events page) and all of them (the export)
  key_scan        `proctree.key_scan` of the busiest agent session's records in
                  the last 24 h, on its host's files (the inspector's read)

Each is the best of `--reps` runs, with the day-file layer's memos (probes, heads,
twin choices, `.gz` indexes) cleared before every run, so a run pays for its
seeks as a first read does. Both formats must yield the same records (counted,
and their times summed); a row that does not says MISMATCH. Then the feed's
size on disk, per stream, plain and gzipped (level 6, as `gzip` seals a day),
and in bytes per record.

`--gzip` seals every day before the newest first (`x.jsonl` -> `x.jsonl.gz`,
`x.ebr` -> `x.ebr.gz`, as the collector does at rollover), so the reads of those
days go through the `.gz` seek index. `--json` prints the results as JSON
instead of the tables. Named so `unittest discover` passes over it.
"""
import argparse
import collections
import json
import math
import os
import shutil
import sys
import tempfile
import time
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
DASH = os.path.dirname(HERE)
sys.path.insert(0, DASH)
sys.path.insert(0, HERE)

from e2e_formats import Fatal, seal_days, write_feed   # noqa: E402

FORMATS = ("jsonl", "ebr")


def config(tmp):
    """The service's config with nothing from this shell or checkout: defaults."""
    conf = os.path.join(tmp, "config.json")
    with open(conf, "w") as fh:
        fh.write("{}\n")
    for k in [k for k in os.environ if k.startswith(("RC_DASH_", "EBPFM_"))]:
        del os.environ[k]
    os.environ["RC_DASH_CONFIG"] = conf
    from rc_dashboard import config as configmod
    return configmod.load(None, None)


def forget():
    """Drop what the day-file layer remembers between reads: a run then seeks
    and indexes as a first read does."""
    from rc_dashboard import feedfile, gz
    with feedfile._memo_lock:
        feedfile._memo.clear()
    with feedfile._twin_lock:
        feedfile._choices.clear()
        feedfile._firsts.clear()
    with gz._lock:
        gz._indexes.clear()


# ------------------------------------------------------------------ the reads
def op_backfill(env, fmt):
    from rc_dashboard.tail import FileTailSource, read_back
    src = FileTailSource(env["roots"], days=env["days"] + 2, prefer=fmt)
    t = time.perf_counter()
    plan = src.position(env["start"] - 60)
    t_pos = time.perf_counter() - t
    stats = {"bytes": 0, "parse_errors": 0}
    n, tsum = 0, 0.0
    for ts, _raw in read_back(plan, stats):
        n += 1
        tsum += ts if ts != float("inf") else 0.0
    return {"records": n, "check": tsum, "bytes": stats["bytes"], "position_s": t_pos,
            "errors": stats["parse_errors"]}


def op_window(env, fmt):
    from rc_dashboard.export import window_plan, window_scan
    from rc_dashboard.normalize import Normalizer
    from rc_dashboard.windows import EVENTS
    since = env["as_of"] - 86400
    plan, total = window_plan(env["roots"], since, 3, prefer=fmt)
    n, tsum, stats = 0, 0.0, {}
    for rec, stats in window_scan(plan, Normalizer(env["cfg"]), since, events=EVENTS):
        n += 1
        tsum += rec["_ts_epoch"]
    return {"records": n, "check": tsum, "bytes": stats.get("bytes", 0),
            "errors": stats.get("parse_errors", 0)}


def op_hour(env, fmt):
    from rc_dashboard.feedfile import day_files
    from rc_dashboard.hours import HOUR, new_stats, read_span
    from rc_dashboard.normalize import Normalizer
    from rc_dashboard.windows import EVENTS
    h = env["hour"]
    files = day_files(env["roots"], env["days"] + 2, fmt)
    stream, _plan, stats = read_span(files, Normalizer(env["cfg"]), h * HOUR, (h + 1) * HOUR,
                                     EVENTS, stats=new_stats())
    n, tsum = 0, 0.0
    for ts, _rec in stream:
        n += 1
        tsum += ts
    return {"records": n, "check": tsum, "bytes": stats["bytes"],
            "errors": stats["parse_errors"]}


def op_scan(limit):
    def run(env, fmt):
        from rc_dashboard.export import make_predicate, parse_filters, scan
        from rc_dashboard.normalize import Normalizer
        f = parse_filters({"user": [env["user"]]})
        f["event"] = {"exit"}           # /api/events' and /api/export's default
        n, tsum, scanned = 0, 0.0, 0
        for rec, scanned, _m in scan(env["roots"], Normalizer(env["cfg"]), make_predicate(f),
                                     limit=limit, newest_first=True, prefer=fmt):
            n += 1
            tsum += rec["_ts_epoch"]
        return {"records": n, "check": tsum, "scanned": scanned}
    return run


def op_key_scan(env, fmt):
    from rc_dashboard.export import window_plan
    from rc_dashboard.feedfile import day_files, host_of
    from rc_dashboard.normalize import Normalizer
    from rc_dashboard.proctree import key_patterns, key_scan, key_where
    host, pid = env["session"]
    members = {"%s|apid:%s" % (host, pid)}
    since, until = env["as_of"] - 86400, env["as_of"]
    mine = [p for p in day_files(env["roots"], 3, fmt) if host_of(p) == host]
    plan, _total = window_plan(env["roots"], since, 3, paths=mine)
    n, tsum, stats = 0, 0.0, {}
    for rec, stats in key_scan(plan, Normalizer(env["cfg"]), since, until,
                               key_patterns(members), where=key_where(members)):
        # the session's own, as the inspector keeps them by `_sid`: a key search
        # may pass another's (a pid named elsewhere), which each format reads apart
        if rec.get("_kt") != "apid" or rec.get("_kv") != str(pid) or rec.get("host") != host:
            continue
        n += 1
        tsum += rec["_ts_epoch"]
    return {"records": n, "check": tsum, "bytes": stats.get("bytes", 0),
            "scanned": stats.get("scanned", 0)}


OPS = (
    ("backfill", "backfill: position + read_back, whole feed", op_backfill),
    ("window_24h", "window_scan(EVENTS), 24 h", op_window),
    ("hour", "hours.read_span, one hour (EVENTS)", op_hour),
    ("scan_user_500", "scan, one user's exits, newest first, 500", op_scan(500)),
    ("scan_user_all", "scan, one user's exits, newest first, all", op_scan(None)),
    ("key_scan", "key_scan, one agent session, 24 h", op_key_scan),
)


def timed(fn, env, fmt, reps):
    best = None
    for _ in range(reps):
        forget()
        t = time.perf_counter()
        got = fn(env, fmt)
        got["s"] = time.perf_counter() - t
        if best is None or got["s"] < best["s"]:
            best = got
    return best


# ------------------------------------------------------------------ the feed
def survey(feed, as_of):
    """From the EBR exits (untimed): the busiest user over the feed, the busiest
    agent session of the last 24 h by its host and pid, and the busiest whole
    hour; and the records of every stream kind."""
    from rc_dashboard import ebr
    users, sessions, hours = collections.Counter(), collections.Counter(), collections.Counter()
    for dp, _dirs, files in os.walk(os.path.join(feed, "ebpf")):
        for name in files:
            if ".exits.ebr" not in name:
                continue
            for _pos, rec in ebr.iter_records(os.path.join(dp, name), kinds={"exit"}):
                ts = rec.get("ts_epoch")
                if rec.get("user"):
                    users[rec["user"]] += 1
                if isinstance(ts, (int, float)):
                    if as_of - 3600 * 24 < ts and rec.get("agent_pid") is not None:
                        sessions[(rec.get("host"), rec["agent_pid"])] += 1
                    if ts < as_of - 7200:
                        hours[int(ts // 3600)] += 1
    if not users or not sessions or not hours:
        raise Fatal("the feed has no exit records to survey")
    return users.most_common(1)[0][0], sessions.most_common(1)[0][0], \
        hours.most_common(1)[0][0]


def kind_of(name):
    for k in ("exits", "snapshot"):
        if ".%s." % k in name:
            return k
    return "node"


def sizes(feed):
    """Per stream kind and format: files, records, bytes plain and gzipped."""
    out = {}
    for tier in ("ebpf", "node"):
        for dp, _dirs, files in os.walk(os.path.join(feed, tier)):
            for name in sorted(files):
                fmt = "ebr" if ".ebr" in name else "jsonl" if ".jsonl" in name else None
                if fmt is None:
                    continue
                path = os.path.join(dp, name)
                k = out.setdefault(kind_of(name), {f: {"files": 0, "bytes": 0, "gzip_bytes": 0}
                                                   for f in FORMATS})[fmt]
                k["files"] += 1
                if name.endswith(".gz"):
                    raise Fatal("sizes are taken before any day is sealed")
                z = zlib.compressobj(6, zlib.DEFLATED, 31)
                with open(path, "rb") as fh:
                    while True:
                        buf = fh.read(1 << 20)
                        if not buf:
                            break
                        k["bytes"] += len(buf)
                        k["gzip_bytes"] += len(z.compress(buf))
                k["gzip_bytes"] += len(z.flush())
                if fmt == "jsonl":
                    with open(path, "rb") as fh:
                        k["records"] = k.get("records", 0) + sum(
                            1 for line in fh if line.strip())
    for k in out.values():
        k["ebr"]["records"] = k["jsonl"].get("records", 0)
    return out


# ------------------------------------------------------------------ the report
def table(res):
    w = sys.stdout.write
    m = res["meta"]
    w("bench formats: %d day(s), %d host(s), %s record(s)%s; best of %d, memos cleared; "
      "Python %s\n\n" % (m["days"], m["hosts"], "{:,}".format(m["records"]),
                         ", days before the newest sealed as .gz" if m["gzip"] else "",
                         m["reps"], m["python"]))
    w("%-46s %9s %9s %8s %10s %13s %s\n" % ("read", "jsonl s", "ebr s", "speedup",
                                           "records", "MB read j/e", ""))
    for key, label, _fn in OPS:
        r = res["reads"][key]
        j, e = r["jsonl"], r["ebr"]
        mb = ("%5.1f/%-5.1f" % (j["bytes"] / 1e6, e["bytes"] / 1e6)) if "bytes" in j else ""
        w("%-46s %9.3f %9.3f %7.2fx %10s %13s %s\n" % (
            label, j["s"], e["s"], j["s"] / e["s"] if e["s"] else float("inf"),
            "{:,}".format(j["records"]), mb, "" if r["same"] else "MISMATCH"))
        if key == "backfill":
            w("%-46s %9.3f %9.3f %7.2fx\n" % ("  of which position", j["position_s"],
                                             e["position_s"], j["position_s"] / e["position_s"]
                                             if e["position_s"] else float("inf")))
    w("\n%-9s %8s %10s %10s %10s %10s %8s %8s %8s %8s\n" % (
        "on disk", "records", "jsonl MB", ".gz MB", "ebr MB", ".gz MB", "ebr/jsonl",
        "gz e/j", "B/rec j", "B/rec e"))
    tot = {f: {"bytes": 0, "gzip_bytes": 0} for f in FORMATS}
    nrec = 0
    for kind in ("exits", "snapshot", "node", "total"):
        if kind == "total":
            s, n = tot, nrec
        else:
            s = res["sizes"].get(kind)
            if s is None:
                continue
            n = s["jsonl"].get("records", 0)
            nrec += n
            for f in FORMATS:
                for k in ("bytes", "gzip_bytes"):
                    tot[f][k] += s[f][k]
        j, e = s["jsonl"], s["ebr"]
        w("%-9s %8s %10.1f %10.1f %10.1f %10.1f %8.3f %8.3f %8.0f %8.0f\n" % (
            kind, "{:,}".format(n), j["bytes"] / 1e6, j["gzip_bytes"] / 1e6, e["bytes"] / 1e6,
            e["gzip_bytes"] / 1e6, e["bytes"] / j["bytes"], e["gzip_bytes"] / j["gzip_bytes"],
            j["bytes"] / max(1, n), e["bytes"] / max(1, n)))


def main(argv=None):
    ap = argparse.ArgumentParser(description="Time the dashboard's read paths on the JSONL "
                                             "and the EBR twins of one demo feed.")
    ap.add_argument("--days", type=int, default=3, help="days of demo history (3; 1..14)")
    ap.add_argument("--hosts", type=int, default=4, help="demo login nodes (4; 1..6)")
    ap.add_argument("--reps", type=int, default=3, help="runs of each read; the best counts (3)")
    ap.add_argument("--gzip", action="store_true",
                    help="seal every day before the newest as .gz first, both twins")
    ap.add_argument("--json", action="store_true", help="print the results as JSON")
    ap.add_argument("--dir", default=None,
                    help="a demo dir written with --demo-format both to read, not a new one")
    ap.add_argument("--keep", action="store_true", help="keep the temp dir")
    a = ap.parse_args(argv)
    days, hosts = max(1, min(14, a.days)), max(1, min(6, a.hosts))
    if a.gzip and a.dir:
        ap.error("--gzip seals the feed's files in place: not a feed --dir names")
    tmp = tempfile.mkdtemp(prefix="rc-bench-formats-")
    try:
        return run(a, tmp, days, hosts)
    except Fatal as e:
        sys.stderr.write("ERROR: %s\n" % e)
        return 2
    finally:
        if a.keep:
            sys.stderr.write("kept %s\n" % tmp)
        else:
            shutil.rmtree(tmp, ignore_errors=True)


def run(a, tmp, days, hosts):
    def say(msg):
        sys.stderr.write(msg + "\n")
        sys.stderr.flush()
    if a.dir:
        base = os.path.abspath(a.dir)
        with open(os.path.join(base, "demo.json")) as fh:
            stamp = json.load(fh)
        if stamp.get("format") != "both":
            raise Fatal("%s holds a %s feed, not both formats" % (base, stamp.get("format")))
        days, hosts = int(round((stamp["written_to"] - stamp["start"]) / 86400.0)), stamp["hosts"]
    else:
        base = os.path.join(tmp, "demo")
        conf = os.path.join(tmp, "demo-config.json")
        with open(conf, "w") as fh:
            fh.write("{}\n")
        say("writing a %d-day, %d-host demo feed in both formats ..." % (days, hosts))
        stamp, took = write_feed(base, days, hosts, conf)
        say("written in %.1f s" % took)
    feed = os.path.join(base, "feed")
    as_of = stamp["written_to"]
    say("measuring sizes ...")
    sz = sizes(feed)
    user, session, hour = survey(feed, as_of)
    if a.gzip:
        say("sealed %d day-file(s) before the newest day as .gz" % seal_days(feed))
    cfg = config(tmp)
    env = {"cfg": cfg, "roots": [os.path.join(feed, "ebpf")], "days": days, "as_of": as_of,
           "start": stamp["start"], "user": user, "session": session, "hour": hour}
    res = {"meta": {"days": days, "hosts": hosts, "reps": a.reps, "gzip": a.gzip,
                    "records": sum(k["jsonl"].get("records", 0) for k in sz.values()),
                    "python": sys.version.split()[0], "user": user,
                    "session": "%s apid:%s" % session,
                    "hour": time.strftime("%Y-%m-%d %H:00", time.localtime(hour * 3600))},
           "reads": {}, "sizes": sz}
    for key, label, fn in OPS:
        say("timing %s ..." % label)
        r = res["reads"][key] = {f: timed(fn, env, f, max(1, a.reps)) for f in FORMATS}
        j, e = r["jsonl"], r["ebr"]
        r["same"] = j["records"] == e["records"] and math.isclose(
            j["check"], e["check"], rel_tol=0, abs_tol=1e-3)
    if a.json:
        json.dump(res, sys.stdout, indent=1, default=str)
        sys.stdout.write("\n")
    else:
        table(res)
    return 0 if all(r["same"] for r in res["reads"].values()) else 1


if __name__ == "__main__":
    sys.exit(main())
