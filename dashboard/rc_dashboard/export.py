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
import heapq
import json
import os
import re
import time

from . import gz, fastjson, replica
# The day-file layer that was defined here lives in feedfile.py; every name of
# it is imported back, so `export.<name>` still works for its callers.
from .feedfile import (HEAD_BYTES, chosen_for, day_files, head_digest, is_boundary,
                       iter_lines_forward, iter_lines_reverse, seek_to_epoch, stem_of)
from .feedfile import (_MEMO_FILES, _MEMO_PROBES, _identity, _memo, _memo_lock,  # noqa: F401
                       _memo_of, _probe, _probe_read, _remember, dated_dirs, feed_files)
from .normalize import canonical_class, epoch_of
from .redact import redact
from .reducers.tools import verdict


LIST_FIELDS = ("class", "agent_type", "user", "host", "tool", "purpose", "bucket",
               "sandbox", "approval", "exit_code", "signal", "event", "outcome")
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


def agent_type_where(want):
    """`agent_type_token(v) in want` as a `where` set test (`str(v) in S`): `none`
    is also the record whose agent type is null or empty. A superset, as every
    `where` must be: it may let a record through the token rejects, never the
    other way round."""
    out = set(want)
    if UNTAGGED in out:
        out |= {"None", ""}
    return frozenset(out)


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
        # how it ended, as the tool mix's exit verdicts word it (`ok` for an
        # exit 0): a click on a failure count finds the processes it counted
        if f.get("outcome") and (verdict(rec, rec.get("_eff")) or "ok") not in f["outcome"]:
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
    # and the record's own fields it tests, for a sealed file's replica to
    # reject records by before it builds them (`where_of`)
    ok.where = where_of(f)
    return ok


def where_of(f):
    """The filters of `make_predicate` that test a record's own fields, as a
    `where` (`where.py`): each one AND the others, by the predicate's own rule
    (`str(value) in` the wanted set). None when there is none. Never stricter
    than the predicate -- a record it keeps always passes -- since a sealed
    file's replica drops what this rejects before the predicate sees it."""
    w = {}
    for key in ("user", "host", "exit_code", "signal"):
        if f.get(key):
            w[key] = frozenset(f[key])
    if f.get("session_key"):
        w["session_key"] = frozenset([f["session_key"]])
    if f.get("agent_type"):
        w["agent_type"] = agent_type_where(f["agent_type"])
    return w or None


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
    where = getattr(pred, "where", None)
    rep = replica.for_path(path) if where is not None else None
    at = None
    if rep is not None:
        resume = {}
        try:
            yield from _matching_replica(rep, norm, pred, newest_first, stats, lo, hi, where,
                                         resume)
            return
        except Exception as e:
            # The replica stopped answering part way: the rest of the read from
            # the file, past the last record it read (`replica.failed`).
            replica.failed(path, rep, e)
            at = resume.get("at")
    edge = hi if newest_first else lo
    if at is None and edge is not None:
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
            raw = fastjson.record(line)
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


def _matching_replica(rep, norm, pred, newest_first, stats, lo, hi, where, resume=None):
    """`_matching` over a sealed file's replica, for a predicate that tests the
    records' own fields: the same read -- from the same edge (the replica's time
    search answers as the file's), stopping at the same record past the far
    bound, `scanned` counting the same records by each one handed on
    (`Replica.line_read`) -- with only the records the predicate's event types
    and fields could keep built (`replica.records`), the same rule deciding
    each. `resume["at"]` keeps where a read of the file would go on from: past
    the last record read, in the read's direction."""
    edge = hi if newest_first else lo
    at = None if edge is None else rep.seek_to_epoch(edge)
    kinds = getattr(pred, "events", None)
    if at is None:
        at = rep.complete_end if newest_first else 0
    start, end, count, total = rep.line_read(at, kinds, lo if newest_first else hi,
                                             reverse=newest_first)
    resume = {} if resume is None else resume
    done = 0
    for off, _n, raw in rep.records(start, end, reverse=newest_first, kinds=kinds,
                                    where=where):
        resume["at"] = off if newest_first else off + _n
        rec = norm.normalize(raw)
        if rec is None or not pred(rec):
            continue
        got = count(off)
        stats["scanned"] += got - done
        done = got
        ts = rec.get("_ts_epoch")
        yield (NO_TS if ts is None else ts), rec
    stats["scanned"] += total - done


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


def _place(p, since):
    """`(start, size)` of day-file `p` for a read from epoch `since`: its first
    in-range offset and its size. A sealed file's replica answers both as the
    file would, without the `.gz` being inflated to search it. One that raises
    -- a table gone or damaged -- is let go (`replica.failed`) and the file
    answers: its error is not the file's, and must not leave the file out of a
    plan as if it held nothing in the window."""
    rep = replica.for_path(p)
    if rep is not None:
        try:
            return rep.seek_to_epoch(since), rep.size
        except Exception as e:
            replica.failed(p, rep, e)
    with gz.open_feed(p) as fh:
        size = gz.fstat(fh).st_size
        return seek_to_epoch(fh, since, size), size


def window_plan(roots, since, days=0, paths=None):
    """Per-file byte range a window touches, plus the total bytes to read.

    Built up front rather than discovered while streaming, so the rebuild can
    report a real percentage instead of a spinner -- and so the cost of a window
    is answerable BEFORE committing to it. `paths`: these of the day-files only.
    """
    plan = []
    for p in (day_files(roots, days) if paths is None else paths):
        try:
            start, size = _place(p, since)
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

    A plain file compressed since (the collector's nightly `x.jsonl` ->
    `x.jsonl.gz`) is the same bytes at the same offsets under a new name and
    inode: it is read on from its `.gz`, known by its head alone, and its stream
    is not planned a second time. A sealed file's replica answers for it.

    None when the saved scan no longer describes the files: one has vanished, is
    a different inode, starts with different bytes (a file deleted and written
    again can get its inode number back), is shorter than the offset, or has no
    line ending there. The saved reducers then hold records that are not on disk
    in that form, and resuming them would serve numbers the files cannot
    reproduce.
    """
    known = {f["path"]: f for f in files}
    plan, planned = [], set()
    for p, f in known.items():
        q = p
        if not gz.is_gz(p) and not os.path.exists(p) and os.path.exists(p + gz.SUFFIX):
            q = p + gz.SUFFIX
        if chosen_for(q) is None:
            return None
        try:
            st = os.stat(q)
            if q == p and (st.st_dev, st.st_ino) != (f["dev"], f["ino"]):
                return None
            rep = replica.for_path(q)
            if rep is not None:
                try:
                    size = rep.size
                    same = size >= f["end"] and rep.head_digest(f["head_n"]) == f["head"] \
                        and rep.is_boundary(f["end"])
                except Exception as e:      # the replica, not the file: ask the file
                    replica.failed(q, rep, e)
                    rep = None
                else:
                    if not same:
                        return None
            if rep is None:
                with gz.open_feed(q) as fh:
                    size = gz.fstat(fh).st_size
                    if size < f["end"] or head_digest(fh, f["head_n"]) != f["head"] \
                            or not is_boundary(fh, f["end"]):
                        return None
        except OSError:
            return None
        plan.append({"path": q, "start": f["end"], "size": size})
        planned.add(stem_of(q))
    for p in day_files(roots, days):
        if p in known or stem_of(p) in planned:
            continue
        try:
            start, size = _place(p, since)
        except OSError:
            continue
        if start < size:
            plan.append({"path": p, "start": start, "size": size})
    plan.sort(key=lambda e: e["path"])
    return plan, sum(max(0, e["size"] - e["start"]) for e in plan)


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


# A filtered scan's block: the regex engine looks through this much of a
# day-file at a time for the lines a filter could want (`_found`).
BLOCK = 8 << 20


# A line a line-by-line read passes over without counting it: nothing but
# whitespace (`not line.strip()`).
_BLANK = re.compile(rb"^[ \t\r\x0b\x0c]*\n", re.M)
# Where such a line could begin: a byte after a newline that is whitespace or a
# newline itself. `_BLANK` tries a match at every line's start, and through a
# block of a day-file that took ten times as long as looking for this.
_BLANK_AFTER = re.compile(rb"\n[ \t\r\x0b\x0c\n]")
_BLANK_FIRST = frozenset(b" \t\r\x0b\x0c\n")


def _passed(body, a, b):
    """How many of the whole lines in `body[a:b]` a line-by-line read counts as
    skipped: all but the blank ones. `a` is a line's start."""
    n = body.count(b"\n", a, b)
    if not n:
        return 0
    # A collector writes no blank line, so the blank ones are counted only
    # where one could be: at `a`, or after a newline that is followed by
    # whitespace or another newline. Anywhere else, every line is counted.
    if body[a] not in _BLANK_FIRST and _BLANK_AFTER.search(body, a, b) is None:
        return n
    return n - len(_BLANK.findall(body, a, b))


def _found(fh, pos, find, stats, entry, block=None):
    """The complete lines from `pos` on that `find` hits, for a scan after a few.

    The regex engine looks through a block of the day-file at a time, and only
    the lines it hits are handed on -- a narrow filter wants a few percent of a
    busy file's lines, and each of the rest cost a Python loop's turn. What a
    line-by-line read keeps up to date is kept the same: `entry["end"]` just
    past the line handed on (or the last whole line read), `bytes` read through
    it, and the lines passed over counted as skipped (a blank one not, as
    there). A line still being written is left, as there. `find` never matches across a line's end: a
    record is one line."""
    block = block or BLOCK
    rest = b""
    while True:
        chunk = fh.read(block)
        buf = rest + chunk if rest else chunk
        cut = buf.rfind(b"\n") + 1
        if cut == 0:
            if not chunk:
                return
            rest = buf
            continue
        body, rest = buf[:cut], buf[cut:]
        done = 0                                    # bytes of `body` accounted for
        for m in find.finditer(body):
            ls = body.rfind(b"\n", 0, m.start()) + 1
            if ls < done:
                continue                            # another hit in the line just handed on
            le = body.find(b"\n", m.start()) + 1
            stats["skipped"] += _passed(body, done, ls)
            stats["bytes"] += le - done
            entry["end"] = pos + le
            done = le
            yield body[ls:le]
        stats["skipped"] += _passed(body, done, cut)
        stats["bytes"] += cut - done
        entry["end"] = pos + cut
        pos += cut
        if not chunk:
            return


def _window_stream(entry, norm, since, until, stats, wanted=None, find=None, kinds=None,
                   where=None):
    """One file's in-window records as `(ts, rec)`, ascending.

    Records in `entry` how far it got, for a saved window to resume from: the
    file's `(dev, ino)`, a digest of its head, and `end`, the offset just past the
    last COMPLETE line.
    A trailing line with no newline is still being written; it is left for the
    tail, and for the next resume, rather than lost as a parse error.

    A sealed file read with a `where` -- one session's, one user's, the commands
    behind one figure -- is read from its replica when it has one
    (`_window_stream_replica`), `kinds` and `where` deciding there which records
    are built, where `wanted` and `find` are a line's byte checks here. Should the
    replica raise, the read goes on here, from where it left off.
    """
    rep = replica.for_path(entry["path"]) if where is not None else None
    start = entry["start"]
    if rep is not None:
        try:
            yield from _window_stream_replica(entry, rep, norm, since, until, stats, kinds,
                                              where)
            return
        except Exception as e:
            # The replica stopped answering part way: the rest of the read from
            # the file, from the end of the last record it read (`replica.failed`).
            replica.failed(entry["path"], rep, e)
            start = entry.get("end", start)
    try:
        fh = gz.open_feed(entry["path"])
    except OSError:
        return
    with fh:
        st = gz.fstat(fh)
        entry["dev"], entry["ino"] = st.st_dev, st.st_ino
        entry["head_n"] = min(HEAD_BYTES, st.st_size)
        entry["head"] = head_digest(fh, entry["head_n"])
        pos = entry["end"] = start
        fh.seek(pos)
        # `find`: only the lines it hits come through (`_found`), each still
        # judged by `wanted` below, with the reading's end and bytes kept there.
        lines = fh if find is None else _found(fh, pos, find, stats, entry)
        for line in lines:
            if find is None:
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
                raw = fastjson.record(line)
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


def _window_stream_replica(entry, rep, norm, since, until, stats, kinds, where):
    """`_window_stream` over a sealed file's replica: the same records, offsets and
    `end` as the line read, with only the records `kinds` and `where` could keep
    built (`replica.records`); the others counted as skipped, as the byte checks
    count theirs, and the lines that do not parse as parse errors."""
    try:
        st = os.stat(entry["path"])
    except OSError:
        return
    entry["dev"], entry["ino"] = st.st_dev, st.st_ino
    entry["head_n"] = min(HEAD_BYTES, rep.size)
    entry["head"] = rep.head_digest(entry["head_n"])
    start = last = entry["end"] = entry["start"]
    built = 0
    try:
        for off, n, raw in rep.records(start, None, kinds=kinds, where=where):
            stats["bytes"] += off + n - last
            last = entry["end"] = off + n
            built += 1
            stats["scanned"] += 1
            rec = norm.normalize(raw)
            if rec is None:
                continue
            ts = rec.get("_ts_epoch")
            if ts is None:
                stats["no_ts"] += 1
                continue
            if ts < since:
                continue
            if until is not None and ts > until:
                return                    # sorted: nothing later qualifies
            yield ts, rec
        stats["bytes"] += rep.complete_end - last
        last = entry["end"] = rep.complete_end
    finally:
        stats["skipped"] += rep.record_count(start, last) - built
        stats["parse_errors"] += rep.bad_in(start, last)


def window_scan(plan, norm, since, until=None, events=None, match=None, find=None,
                where=None, stats=None):
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
    `find`, with `match`, is a compiled pattern every line `match` passes holds:
    the scan then looks for the lines by it a block at a time (`_found`).

    A sealed file's replica does both: `events` are its `kinds`, and `where`
    (`where.py`, e.g. `windows.ViewFilter.where`) the structured form of
    `match`, built from the same inputs, never stricter.

    `stats`, when given, is the dict the counts are kept in, so a caller has
    them once the scan is done, whether or not a record came through.
    """
    stats = {} if stats is None else stats
    stats.update({"scanned": 0, "skipped": 0, "bytes": 0, "parse_errors": 0,
                  "no_ts": 0, "files": len(plan)})
    wanted = event_filter(events)
    if match is not None:
        by_event = wanted

        def wanted(line):
            return match(line) if by_event is None or by_event(line) is not None else None
    if match is None:
        find = None
    kinds = frozenset(events) if events else None
    streams = [_window_stream(e, norm, since, until, stats, wanted, find, kinds, where)
               for e in plan]
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
                "keys (_a3, _eff, _purpose, _sandbox, _approval, _surface, _sid, _ts_epoch). "
                "Original fields are unmodified, except that credential values in "
                "args / captured_tail are replaced by [REDACTED] (rc_dashboard/redact.py).",
    }}
