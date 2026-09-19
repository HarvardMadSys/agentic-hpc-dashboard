"""Filtered raw-event query and export.

The filter predicate is built ONCE and used by both `/api/events` and
`/api/export`, so the download is exactly the view rather than an approximation
of it.  End users have no access to the raw data mount, so this is their only
route to the underlying rows -- which is why the export is the normalized record
(derived `_` keys are additive; the collector's own fields are untouched) and why
the first line is a `_meta` header naming the filter set, the window, the feed
paths and whether the row cap truncated the result.  A reader can always tell
what they actually got.

Scanning re-reads the day-files rather than serving from memory: a 24-hour window
of raw events is on the order of 10-20 GB, so it is never buffered.
"""
import glob
import heapq
import json
import os
import time


LIST_FIELDS = ("class", "agent_type", "user", "host", "tool", "purpose", "bucket",
               "sandbox", "approval", "exit_code", "signal", "event")
SCALARS = ("from", "to", "session_key", "depth_min", "depth_max", "cpu_min",
           "duration_min", "q", "rd_min", "wr_min")


def parse_filters(params):
    """params: mapping name -> list[str] (repeatable keys allowed)."""
    def many(k):
        v = params.get(k)
        if v is None:
            return None
        if isinstance(v, (list, tuple)):
            out = [x for item in v for x in str(item).split(",") if x]
        else:
            out = [x for x in str(v).split(",") if x]
        return set(out) or None

    def one(k, cast=str):
        v = params.get(k)
        if isinstance(v, (list, tuple)):
            v = v[0] if v else None
        if v in (None, ""):
            return None
        try:
            return cast(v)
        except (TypeError, ValueError):
            return None

    f = {k: many(k) for k in LIST_FIELDS}
    f["from"] = one("from")
    f["to"] = one("to")
    f["session_key"] = one("session_key")
    f["depth_min"] = one("depth_min", int)
    f["depth_max"] = one("depth_max", int)
    f["cpu_min"] = one("cpu_min", float)
    f["duration_min"] = one("duration_min", float)
    f["rd_min"] = one("rd_min", float)
    f["wr_min"] = one("wr_min", float)
    q = one("q")
    f["q"] = q.lower() if q else None
    return f


def _epoch_bound(s):
    if not s:
        return None
    try:
        return float(s)
    except (TypeError, ValueError):
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return time.mktime(time.strptime(str(s)[:19], fmt))
        except ValueError:
            continue
    return None


def make_predicate(f):
    lo, hi = _epoch_bound(f.get("from")), _epoch_bound(f.get("to"))

    def ok(rec):
        if f.get("event") and rec.get("event") not in f["event"]:
            return False
        if lo is not None or hi is not None:
            e = rec.get("_ts_epoch")
            if e is None:
                return False
            if lo is not None and e < lo:
                return False
            if hi is not None and e > hi:
                return False
        if f.get("class") and rec.get("_a3") not in f["class"]:
            return False
        for key, field in (("agent_type", "agent_type"), ("user", "user"),
                           ("host", "host"), ("tool", "_eff"),
                           ("purpose", "_purpose"), ("sandbox", "_sandbox"),
                           ("approval", "_approval")):
            want = f.get(key)
            if want and str(rec.get(field)) not in want:
                return False
        if f.get("exit_code") and str(rec.get("exit_code")) not in f["exit_code"]:
            return False
        if f.get("signal") and str(rec.get("signal")) not in f["signal"]:
            return False
        if f.get("session_key") and rec.get("session_key") != f["session_key"]:
            return False
        d = rec.get("depth")
        if f.get("depth_min") is not None and (d is None or d < f["depth_min"]):
            return False
        if f.get("depth_max") is not None and (d is None or d > f["depth_max"]):
            return False
        if f.get("cpu_min") is not None and (rec.get("cpu_s") or 0) < f["cpu_min"]:
            return False
        if f.get("duration_min") is not None and (rec.get("duration_s") or 0) < f["duration_min"]:
            return False
        io = rec.get("io") or {}
        if f.get("rd_min") is not None and (io.get("rd_mb") or 0) < f["rd_min"]:
            return False
        if f.get("wr_min") is not None and (io.get("wr_mb") or 0) < f["wr_min"]:
            return False
        if f.get("q"):
            hay = "%s %s" % (rec.get("comm") or "", rec.get("args") or "")
            if f["q"] not in hay.lower():
                return False
        if f.get("bucket"):
            from .reducers.tools import bucket
            if bucket(rec.get("_eff") or "") not in f["bucket"]:
                return False
        return True
    return ok


def iter_lines_reverse(path, block=1 << 20):
    """Yield complete lines from the END of a file backwards, bounded memory.

    A day-file is gigabytes, so it cannot be read into memory and reversed. This
    walks backwards one block at a time and reverses within the block, holding at
    most `block` bytes plus one straddling line.

    The first element of a split block is the fragment whose start lies in the
    PREVIOUS (earlier) block, so it is carried rather than yielded -- except at
    byte 0, where it is a complete line and is yielded last.
    """
    try:
        fh = open(path, "rb")
    except OSError:
        return
    with fh:
        fh.seek(0, os.SEEK_END)
        pos = fh.tell()
        carry = b""
        while pos > 0:
            size = min(block, pos)
            pos -= size
            fh.seek(pos)
            buf = fh.read(size) + carry
            parts = buf.split(b"\n")
            carry = parts.pop(0)
            for line in reversed(parts):
                if line.strip():
                    yield line
        if carry.strip():
            yield carry


def iter_lines_forward(path):
    try:
        fh = open(path, errors="replace")
    except OSError:
        return
    with fh:
        for line in fh:
            if line.strip():
                yield line


def day_files(roots, days=0):
    out = []
    for root in roots:
        if not os.path.isdir(root):
            continue
        dates = sorted(d for d in os.listdir(root)
                       if d.startswith("20") and os.path.isdir(os.path.join(root, d)))
        if days:
            dates = dates[-days:]
        for d in dates:
            out += sorted(glob.glob(os.path.join(root, d, "*.jsonl")))
        out += sorted(glob.glob(os.path.join(root, "*.jsonl")))
    return sorted(set(out))


NO_TS = float("-inf")


def _matching(path, norm, pred, newest_first, stats):
    """One file's matching records as `(ts, rec)`, in time order.

    A day-file is appended as events occur, so reading it backwards yields
    descending timestamps and forwards yields ascending -- which is exactly the
    precondition `heapq.merge` needs.
    """
    lines = iter_lines_reverse(path) if newest_first else iter_lines_forward(path)
    for line in lines:
        try:
            raw = json.loads(line)
        except ValueError:
            continue
        stats["scanned"] += 1
        rec = norm.normalize(raw)
        if rec is None or not pred(rec):
            continue
        ts = rec.get("_ts_epoch")
        yield (NO_TS if ts is None else ts), rec


def scan(roots, norm, pred, limit=None, offset=0, days=0, newest_first=True):
    """Yield `(rec, scanned, matched)` for records matching `pred`, in time order.

    `newest_first` is the default because this feeds a log view and the newest
    matches are the ones a reader wants. It is also much cheaper for the common
    case: the first page comes off the TAIL of each day-file, so a filtered query
    does not walk a multi-GB file from its oldest record forward before it can
    show anything.

    The per-file streams are k-way merged on timestamp rather than concatenated.
    Concatenation would give "all of host B, then all of host A", which looks
    ordered per host and is wrong globally -- on a 7-node login fleet that is not
    a reverse-chronological log, it is seven of them interleaved by filename.
    `heapq.merge` holds one record per open file, so the cost is one block read
    per file to seed it and bounded memory thereafter.

    Pagination is offset-based, matching the cursor contract the viewer already
    uses. A deep page re-walks the earlier matches; acceptable for a log a human
    scrolls, and the early return keeps a shallow page cheap.

    A record with no resolvable timestamp sorts last under `newest_first` rather
    than being dropped -- it is still a match, just not placeable in time.
    """
    stats = {"scanned": 0}
    streams = [_matching(p, norm, pred, newest_first, stats)
               for p in day_files(roots, days)]
    matched = 0
    for _ts, rec in heapq.merge(*streams, key=lambda kr: kr[0],
                                reverse=newest_first):
        matched += 1
        if matched <= offset:
            continue
        yield rec, stats["scanned"], matched
        if limit and matched - offset >= limit:
            return


def meta_header(f, reports, cfg, matched, truncated, elapsed):
    return {"_meta": {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "filters": {k: (sorted(v) if isinstance(v, set) else v)
                    for k, v in f.items() if v is not None},
        "rows": matched,
        "truncated": truncated,
        "max_rows": cfg.get("export.max_rows"),
        "feed_paths": reports["ebpf"]["resolved"] or reports["ebpf"]["configured"],
        "feed_status": reports["ebpf"]["status"],
        "schema_versions": reports["ebpf"].get("schema_versions") or {},
        "elapsed_s": round(elapsed, 2),
        "note": "Records are the collector's own, plus additive `_`-prefixed derived "
                "keys (_a3, _eff, _purpose, _sandbox, _approval, _sid, _ts_epoch). "
                "Original fields are unmodified.",
    }}
