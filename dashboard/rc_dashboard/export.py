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
import hashlib
import heapq
import json
import os
import re
import time

from . import gz, fastjson
from .normalize import canonical_class, epoch_of
from .redact import redact


LIST_FIELDS = ("class", "agent_type", "user", "host", "tool", "purpose", "bucket",
               "sandbox", "approval", "exit_code", "signal", "event")
SCALARS = ("from", "to", "session_key", "depth_min", "depth_max", "cpu_min",
           "duration_min", "q", "rd_min", "wr_min")

# A process the collector did not tag with an agent type -- a person's own
# shell, an editor it does not recognise -- has none, and `none` is how a filter
# asks for those. `str(None)` made it `None`, which nobody would think to type,
# and an empty value cannot be used: it is dropped as "no filter".
UNTAGGED = "none"
_UNTAGGED_ALIASES = frozenset({"none", "None", "null", "untagged"})


def agent_type_token(v):
    """A record's agent type as a filter value: the collector's own name, or `none`."""
    return UNTAGGED if v in (None, "") else str(v)


def agent_type_want(values):
    """Requested agent types, with every spelling of "untagged" made `none`."""
    return {UNTAGGED if v in _UNTAGGED_ALIASES else v for v in values}


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
    if f.get("class"):                   # old links say class=human-vscode
        f["class"] = {canonical_class(c) for c in f["class"]}
    if f.get("agent_type"):
        f["agent_type"] = agent_type_want(f["agent_type"])
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
        if f.get("agent_type") and agent_type_token(rec.get("agent_type")) \
                not in f["agent_type"]:
            return False
        for key, field in (("user", "user"),
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
            # Matched against what would be SHOWN: searching raw argv would let
            # `q=sk-a`, `q=sk-ab`, ... read a masked token back one character
            # at a time from the hit counts.
            hay = "%s %s" % (rec.get("_command") or "", redact(rec.get("args") or ""))
            if f["q"] not in hay.lower():
                return False
        if f.get("bucket"):
            from .reducers.tools import bucket
            if bucket(rec.get("_eff") or "") not in f["bucket"]:
                return False
        return True
    # The time bounds, for `scan` to stop at: a day-file is in time order, so
    # past the window's far edge nothing in it can match.
    ok.bounds = (lo, hi)
    # and the event types, for its byte-level pre-check (`event_filter`)
    ok.events = f.get("event")
    return ok


def iter_lines_reverse(path, block=1 << 20, start=0, end=None):
    """Yield complete lines from the END of a file backwards, bounded memory.

    A day-file is gigabytes, so it cannot be read into memory and reversed. This
    walks backwards one block at a time and reverses within the block, holding at
    most `block` bytes plus one straddling line.

    The first element of a split block is the fragment whose start lies in the
    PREVIOUS (earlier) block, so it is carried rather than yielded -- except at
    `start`, where it is a complete line and is yielded last.

    `[start, end)` narrows the walk to a byte range whose bounds are line
    boundaries; the default is the whole file as it stands when opened.
    """
    try:
        fh = gz.open_feed(path)
    except OSError:
        return
    with fh:
        if end is None:
            fh.seek(0, os.SEEK_END)
            end = fh.tell()
        pos = end
        carry = b""
        while pos > start:
            size = min(block, pos - start)
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


def iter_lines_forward(path, start=0):
    """Yield complete lines from byte `start` on, as bytes."""
    try:
        fh = gz.open_feed(path)
    except OSError:
        return
    with fh:
        fh.seek(start)
        for line in fh:
            if line.strip():
                yield line


def feed_files(d):
    """A directory's day-files, plain or gzip-compressed (`gz`).

    A plain file wins over its `.gz` twin: both exist while one is being
    compressed into the other, and the plain one is the complete copy.
    """
    found = set(glob.glob(os.path.join(d, "*.jsonl")))
    found.update(p for p in glob.glob(os.path.join(d, "*.jsonl" + gz.SUFFIX))
                 if gz.plain_name(p) not in found)
    return sorted(found)


def dated_dirs(root, days=0):
    """`[(date, files)]` for the newest `days` date dirs under `root` that hold
    a day-file, oldest first; every one of them for 0.

    An empty date dir is passed over, not counted. A capture copied in from
    another cluster has dated dirs made ahead of the files that fill them, and
    counting those pushed the days that do hold records out of every window:
    the 1 h view of such a capture read nothing at all.
    """
    if not os.path.isdir(root):
        return []
    dates = sorted((d for d in os.listdir(root)
                    if d.startswith("20") and os.path.isdir(os.path.join(root, d))),
                   reverse=True)
    out = []
    for d in dates:
        files = feed_files(os.path.join(root, d))
        if files:
            out.append((d, files))
            if days and len(out) >= days:
                break
    return out[::-1]


def day_files(roots, days=0):
    out = []
    for root in roots:
        for _d, files in dated_dirs(root, days):
            out += files
        out += feed_files(root)
    return sorted(set(out))


NO_TS = float("-inf")
# How far out of time order a day-file's lines can be: the collector writes a
# poll round at a time. A scan reads this far past a bound before it stops.
SCAN_SLACK_S = 120


def _matching(path, norm, pred, newest_first, stats):
    """One file's matching records as `(ts, rec)`, in time order.

    A day-file is appended as events occur, so reading it backwards yields
    descending timestamps and forwards yields ascending -- which is exactly the
    precondition `heapq.merge` needs.

    The same order bounds the read by the predicate's `from`/`to`: it starts at
    the near edge (`seek_to_epoch`) and stops past the far one. It once read
    every day-file whole, and `heapq.merge` cannot yield until each stream has,
    so a day wholly outside the window -- 1.7 GB of a capture's first day under
    a 24 h view of its second -- held every page of results until it was parsed
    to the end, to match nothing.
    """
    lo, hi = getattr(pred, "bounds", (None, None))
    lo = None if lo is None else lo - SCAN_SLACK_S
    hi = None if hi is None else hi + SCAN_SLACK_S
    edge = hi if newest_first else lo
    at = None
    if edge is not None:
        try:
            with gz.open_feed(path) as fh:
                at = seek_to_epoch(fh, edge, gz.fstat(fh).st_size)
        except OSError:
            return
    if newest_first:
        lines = iter_lines_reverse(path, end=at)
    else:
        lines = iter_lines_forward(path, start=at or 0)
    # Lines of another event type are dropped before they are parsed, as a
    # window build drops them: a snapshot stream holds no `exit` at all, and
    # parsing every line of it to learn that cost most of an exits query.
    wanted = event_filter(getattr(pred, "events", None))
    for line in lines:
        if wanted is not None and wanted(line) is None:
            continue
        try:
            raw = fastjson.loads(line)
        except ValueError:
            continue
        stats["scanned"] += 1
        ts = epoch_of(raw)
        if ts is not None and (lo if newest_first else hi) is not None:
            if (ts < lo) if newest_first else (ts > hi):
                return
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


# ------------------------------------------------------- windowed re-scan
#
# The historical reducers (quantiles, CCDFs, Gini, the dedup index) are not
# resumable from a byte offset and hold no time index, so a window change cannot
# be served by slicing them the way the live buckets are sliced -- they have to
# be rebuilt from the rows.  Rebuilding must cost the WINDOW, not the DAY, or
# narrowing to the last hour would read the same 10-20 GB as asking for all of
# it, and nobody would use the control.
#
# A day-file is appended as events occur, so it is sorted by timestamp: the
# first in-window byte is findable by binary search over offsets.  A 1-hour view
# of a 15 GB day-file then reads ~20 probes and one hour of rows.


def seek_to_epoch(fh, since, size, probe_lines=64):
    """Offset of the first line in `fh` whose timestamp is >= `since`.

    `size` bytes is the search space; the return is `size` when the file holds
    nothing in range, which lets the caller skip it without opening a stream.

    A probe that lands on lines carrying no resolvable timestamp searches
    EARLIER rather than guessing.  That reads more than strictly necessary,
    which is the right way to be wrong here: the alternative is starting the
    rebuild after rows that belonged in the window.
    """
    lo, hi, best = 0, size, size
    while lo < hi:
        mid = (lo + hi) // 2
        off, ts = _probe(fh, mid, probe_lines)
        if ts is None:
            hi = mid                      # unplaceable: widen backwards
        elif ts >= since:
            best = min(best, off)
            hi = mid
        else:
            lo = max(off, mid) + 1        # off >= mid, so this always advances
    return best


def _probe(fh, pos, limit):
    """Align to the next whole line at/after `pos`; return (offset, ts|None)."""
    fh.seek(pos)
    if pos:
        fh.readline()                     # drop the line we landed inside
    off = fh.tell()
    for _ in range(limit):
        line = fh.readline()
        if not line:
            return off, None
        if not line.strip():
            continue
        try:
            ts = epoch_of(fastjson.loads(line))
        except ValueError:
            continue
        if ts is not None:
            return off, ts
    return off, None


def window_plan(roots, since, days=0):
    """Per-file byte range a window touches, plus the total bytes to read.

    Built up front rather than discovered while streaming, so the rebuild can
    report a real percentage instead of a spinner -- and so the cost of a window
    is answerable BEFORE committing to it.
    """
    plan = []
    for p in day_files(roots, days):
        try:
            with gz.open_feed(p) as fh:
                size = gz.fstat(fh).st_size
                start = seek_to_epoch(fh, since, size)
        except OSError:
            continue
        if start < size:
            plan.append({"path": p, "start": start, "size": size})
    return plan, sum(e["size"] - e["start"] for e in plan)


def resume_plan(roots, since, files, days=0):
    """The byte ranges a saved window still has to read, or None.

    `files` is what the saved scan read: per file its path, `(dev, ino)` and the
    offset just past the last complete line it consumed. Each of those files is
    read on from that offset -- the records before it are already in the saved
    reducers -- and any file the scan never read is planned from `since`, as a
    fresh build would plan it.

    None when the saved scan no longer describes the files: one has vanished, is
    a different inode, starts with different bytes (a file deleted and written
    again can get its inode number back), is shorter than the offset, or has no
    line ending there.
    The saved reducers then hold records that are not on disk in that form, and
    resuming them would serve numbers the files cannot reproduce.
    """
    known = {f["path"]: f for f in files}
    plan = []
    for p, f in known.items():
        try:
            with gz.open_feed(p) as fh:
                st = gz.fstat(fh)
                if (st.st_dev, st.st_ino) != (f["dev"], f["ino"]) \
                        or st.st_size < f["end"] \
                        or head_digest(fh, f["head_n"]) != f["head"]:
                    return None
                if f["end"]:
                    fh.seek(f["end"] - 1)
                    if fh.read(1) != b"\n":
                        return None
        except OSError:
            return None
        plan.append({"path": p, "start": f["end"], "size": st.st_size})
    for p in day_files(roots, days):
        if p in known:
            continue
        try:
            with gz.open_feed(p) as fh:
                size = gz.fstat(fh).st_size
                start = seek_to_epoch(fh, since, size)
        except OSError:
            continue
        if start < size:
            plan.append({"path": p, "start": start, "size": size})
    plan.sort(key=lambda e: e["path"])
    return plan, sum(max(0, e["size"] - e["start"]) for e in plan)


HEAD_BYTES = 4096


def head_digest(fh, n):
    """Digest of a file's first `n` bytes: its identity beyond the inode number."""
    fh.seek(0)
    return hashlib.sha1(fh.read(n)).hexdigest()


def event_filter(events):
    """A byte-level pre-check for lines whose `event` is one of `events`.

    Run before `json.loads`, so a scan feeding reducers that only read `exit`
    skips parsing the residency, connection and socket records around them --
    most of a day-file's lines. Never a false negative: the collector writes
    `"event": "<name>"` and any JSON writer must, give or take whitespace. A false
    positive (the same text inside some nested object) only costs a parse, since
    dispatch is still by the parsed record's own `event`.
    """
    if not events:
        return None
    names = b"|".join(re.escape(e.encode()) for e in sorted(events))
    return re.compile(rb'"event"\s*:\s*"(?:' + names + rb')"').search


def _window_stream(entry, norm, since, until, stats, wanted=None):
    """One file's in-window records as `(ts, rec)`, ascending.

    Records in `entry` how far it got, for a saved window to resume from: the
    file's `(dev, ino)`, a digest of its head, and `end`, the offset just past the
    last COMPLETE line.
    A trailing line with no newline is still being written; it is left for the
    tail, and for the next resume, rather than lost as a parse error.
    """
    try:
        fh = gz.open_feed(entry["path"])
    except OSError:
        return
    with fh:
        st = gz.fstat(fh)
        entry["dev"], entry["ino"] = st.st_dev, st.st_ino
        entry["head_n"] = min(HEAD_BYTES, st.st_size)
        entry["head"] = head_digest(fh, entry["head_n"])
        pos = entry["end"] = entry["start"]
        fh.seek(pos)
        for line in fh:
            if not line.endswith(b"\n"):
                return
            pos += len(line)
            entry["end"] = pos
            stats["bytes"] += len(line)
            if not line.strip():
                continue
            if wanted is not None and wanted(line) is None:
                stats["skipped"] += 1
                continue
            stats["scanned"] += 1
            try:
                raw = fastjson.loads(line)
            except ValueError:
                stats["parse_errors"] += 1
                continue
            rec = norm.normalize(raw)
            if rec is None:
                continue
            ts = rec.get("_ts_epoch")
            if ts is None:
                # A record that cannot be placed in time cannot be attributed to
                # a window.  Counted, never folded into one silently.
                stats["no_ts"] += 1
                continue
            if ts < since:
                continue
            if until is not None and ts > until:
                return                    # sorted: nothing later qualifies
            yield ts, rec


def window_scan(plan, norm, since, until=None, events=None, match=None):
    """Yield `(rec, stats)` for every in-window record, OLDEST FIRST.

    Ascending order is a correctness requirement, not a preference: the tool-mix
    shell-wrapper dedup relies on a child's exit arriving before the shell that
    waited on it, and a trajectory's run-length-encoded chain is appended in
    place.  Fed newest-first, both would be wrong rather than merely unsorted.

    Per-file streams are k-way merged on timestamp for the same reason `scan`
    merges: concatenating gives all of host B then all of host A, which is not
    one ordered stream, it is seven of them end to end.

    `events`, when given, is the only event types yielded; the rest are skipped
    before they are parsed (`event_filter`). `match`, when given, is a second
    byte-level pre-check of the same kind -- a callable on the raw line, None to
    skip it -- for a caller after a handful of records in a busy file (one
    session's, `proctree.key_matcher`). It must never miss a line the caller
    wants; a false hit only costs a parse, the caller still filters the record.
    """
    stats = {"scanned": 0, "skipped": 0, "bytes": 0, "parse_errors": 0,
             "no_ts": 0, "files": len(plan)}
    wanted = event_filter(events)
    if match is not None:
        by_event = wanted

        def wanted(line):
            return match(line) if by_event is None or by_event(line) is not None else None
    streams = [_window_stream(e, norm, since, until, stats, wanted) for e in plan]
    for _ts, rec in heapq.merge(*streams, key=lambda kr: kr[0]):
        yield rec, stats


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
                "Original fields are unmodified, except that credential values in "
                "args / captured_tail are replaced by [REDACTED] (rc_dashboard/redact.py).",
    }}
