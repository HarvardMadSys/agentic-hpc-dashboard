"""The session inspector's key search, its records built in worker processes.

A key search (`proctree.key_scan`) reads one session's records from each of its
day-files: the lines its keys' patterns hit in a plain file, the rows its keys'
`where` passes in a sealed file's replica (`replica.py`). Building those records
and normalising them is most of a large session's read -- 44 us a record on FAS,
against 6 us for the tree they feed -- so it is done in worker processes, a part
of a file each, and a record comes back holding only what the tree and the
inspector read (`INSPECT_KEYS`).

The read is the one-process read's, exactly. A part is a run of the file the
search would read anyway: a replica's row groups, or a plain file's CHUNKs, each
searched as the one-process read searches it. For every record it hands on, it
reports the line's offset and what the counters (`skipped`, `scanned`,
`parse_errors`, `no_ts`) gained before it, and where it stopped: the first record
past `until`, after which the one-process read returns. This process replays the
parts in file order, counting `bytes` a CHUNK at a time from those offsets as the
one-process read counts them, so the same records come out in the same order with
the same counters at each one, and `inspect_max_bytes` and the record budget stop
a read at the same record (tests/test_keysearch.py).

Scheduling. A file's first PRIME parts are handed out when the read is planned,
so the merge across files has every file's first record to start from; the file
being read keeps its workers plus AHEAD more in flight, and a file the merge has
not reached yet waits with what it was primed with. A read that stops early --
its budget, or a reader opening another session -- is cancelled (`cancel`): its
queued parts are dropped, and a running one notices within a few hundred records
and gives up, so the next read does not wait behind it.

A sealed file with no replica is read in this process as before: a `.gz` cannot
be read from the middle without inflating it up to there.
"""
import atexit
import collections
import concurrent.futures
import hashlib
import json
import mmap
import multiprocessing
import os
import threading
import time

from . import gz, replica
from . import fastjson

# What the tree (`proctree.TreeBuilder`) and the inspector's loop read of a
# record: a part sends back these keys alone. tests/test_keysearch.py checks the
# trees built from them against the trees built from whole records.
INSPECT_KEYS = ("event", "pid", "ppid", "_ts_epoch", "_sid", "_command", "_eff", "_role",
                "_purpose", "_kv", "_a3", "args", "cwd", "depth", "user", "agent_type",
                "start", "duration_s", "cpu_s", "exit_code", "signal", "core_dumped",
                "argv_source", "duration_src", "io", "net_tx_bytes", "net_rx_bytes",
                "age_s")
PLAIN_CHUNKS = 2         # a plain file's CHUNKs to a part
PRIME = 2                # parts handed out for each file when the read is planned
AHEAD = 2                # parts in flight past the workers, for the file being read
CHECK_EVERY = 256        # records between a part's looks at whether its read was cancelled
IDLE_S = 300             # seconds unused after which the workers are let go


def compact(rec):
    return {k: rec[k] for k in INSPECT_KEYS if k in rec}


# ---------------------------------------------------------------- the workers
_W = {"norm": {}, "reps": collections.OrderedDict(), "cancel": None}


def _init_worker(cancel):
    _W["cancel"] = cancel


def _normalizer(cfg):
    digest, tree = cfg
    got = _W["norm"].get(digest)
    if got is None:
        from .config import Config
        from .normalize import Normalizer
        got = _W["norm"][digest] = Normalizer(Config(tree, {}, None))
    return got


def _replica(spec):
    out, man = spec
    key = (out, man["day"], man["stem"], json.dumps(man["source"], sort_keys=True))
    got = _W["reps"].get(key)
    if got is None:
        got = _W["reps"][key] = replica.Replica(out, man)
        while len(_W["reps"]) > 8:
            _W["reps"].popitem(last=False)
    else:
        _W["reps"].move_to_end(key)
    return got


def part(job):
    """One part of one file's key search, in a worker: `(items, tail, stop)`, or
    None when its read was cancelled.

    `items` are `(offset, ts, record, gained)` for the records the one-process
    read hands on, `gained` the counters' gain since the record before;
    `tail` the gain after the last; `stop` the offset of the record past
    `until` where the read returns, or None."""
    from .proctree import _hits
    from .export import event_filter
    flag, read = _W["cancel"], job.get("read")

    def cancelled():
        return flag is not None and read is not None and flag.value >= read
    if cancelled():
        return None
    norm = _normalizer(job["cfg"])
    since, until = job["since"], job["until"]
    items = []
    d = [0, 0, 0, 0]                    # skipped, scanned, parse_errors, no_ts
    seen = [0]

    def take(off, raw):
        """None to go on; else the offset the read stops at."""
        d[1] += 1
        rec = norm.normalize(raw)
        if rec is None:
            return None
        ts = rec.get("_ts_epoch")
        if ts is None:
            d[3] += 1
            return None
        if ts < since:
            return None
        if until is not None and ts > until:
            return off
        items.append((off, ts, compact(rec), tuple(d)))
        d[:] = [0, 0, 0, 0]
        return None

    def gone():
        seen[0] += 1
        return not seen[0] % CHECK_EVERY and cancelled()

    if job["kind"] == "replica":
        rep = _replica(job["rep"])
        kinds = job["kinds"]
        u0, u1 = job["range"]
        for off, _n, raw in rep.records(u0, u1, where=job["where"]):
            if gone():
                return None
            if kinds is not None and raw.get("event") not in kinds:
                d[0] += 1
                continue
            hit = take(off, raw)
            if hit is not None:
                return items, tuple(d), hit
        return items, tuple(d), None
    pats = job["pats"]
    ev = event_filter(job["events"])
    with open(job["path"], "rb") as fh:
        mm = mmap.mmap(fh.fileno(), job["size"], access=mmap.ACCESS_READ)
        try:
            for pos, stop in job["chunks"]:
                if cancelled():
                    return None
                for ls in _hits(mm, pats, pos, stop):
                    if gone():
                        return None
                    line = mm[ls:mm.find(b"\n", ls, stop)]
                    if ev is not None and ev(line) is None:
                        d[0] += 1
                        continue
                    try:
                        raw = fastjson.loads(line)
                    except ValueError:
                        d[1] += 1
                        d[2] += 1
                        continue
                    hit = take(ls, raw)
                    if hit is not None:
                        return items, tuple(d), hit
        finally:
            mm.close()
    return items, tuple(d), None


# ---------------------------------------------------------------- the pool
_pool = {"ex": None, "n": 0, "cancel": None, "seq": 0, "dropped": 0, "used": 0.0,
         "busy": 0, "reaper": None}
_pool_lock = threading.Lock()


def _executor(n):
    with _pool_lock:
        ex = _pool["ex"]
        if ex is not None and (_pool["n"] != n or getattr(ex, "_broken", False)):
            ex.shutdown(wait=False, cancel_futures=True)
            ex = None
        if ex is None:
            ctx = multiprocessing.get_context("spawn")
            cancel = ctx.Value("q", 0, lock=False)
            ex = _pool["ex"] = concurrent.futures.ProcessPoolExecutor(
                n, mp_context=ctx, initializer=_init_worker, initargs=(cancel,))
            _pool["n"], _pool["cancel"] = n, cancel
            cancel.value = _pool["dropped"]      # what was cancelled stays so
        _pool["used"] = time.time()
        if _pool["reaper"] is None:
            _pool["reaper"] = threading.Thread(target=_reap, name="keysearch-reaper",
                                               daemon=True)
            _pool["reaper"].start()
            atexit.register(shutdown)       # before the interpreter tears its modules down
        return ex


def _reap():
    """Let the workers go once nobody has read for IDLE_S: they hold a few hundred MB."""
    while True:
        time.sleep(30)
        with _pool_lock:
            ex = _pool["ex"]
            if ex is not None and not _pool["busy"] and time.time() - _pool["used"] > IDLE_S:
                ex.shutdown(wait=False, cancel_futures=True)
                _pool["ex"] = None


def shutdown():
    with _pool_lock:
        ex, _pool["ex"] = _pool["ex"], None
    if ex is not None:
        ex.shutdown(wait=False, cancel_futures=True)


def workers_for(n):
    """At most `n` workers, and no more than the CPUs this process may use; 1 or
    fewer reads in this process."""
    from .history import usable_cpus
    return min(n, usable_cpus())


def next_read():
    """A new read's id: `cancel` with it drops what is left of that read."""
    with _pool_lock:
        _pool["seq"] += 1
        return _pool["seq"]


def cancel(read):
    """Drop what is left of read `read` (and of every earlier one): its queued
    parts return at once, and a running one within CHECK_EVERY records."""
    with _pool_lock:
        _pool["dropped"] = max(_pool["dropped"], read)
        flag = _pool["cancel"]
        if flag is not None and flag.value < read:
            flag.value = read


def cfg_spec(cfg):
    """The config as a worker takes it: a digest it caches its normaliser by, and
    the tree itself (pickled, so its sets and tuples stay what they are)."""
    tree = cfg.as_dict()
    blob = json.dumps(tree, sort_keys=True, default=str)
    return (hashlib.sha1(blob.encode()).hexdigest(), tree)


# ---------------------------------------------------------------- the reader
def stream(entry, cfg, since, until, stats, pats, events, kinds, where, workers, read):
    """One file's key search, its parts built by `workers` processes, as
    `(ts, rec)` ascending; None when this file is read in this process
    (`proctree._key_stream`): a `.gz` with no replica, or no key patterns for a
    plain file. Its first parts are handed out now (PRIME)."""
    from . import proctree
    path = entry["path"]
    rep = replica.for_path(path) if where is not None else None
    if rep is not None:
        size = rep.size
        start = entry["start"]
        if size <= start:
            return iter(())
        end = rep.end_at(until + proctree.UNTIL_SLACK, size) if until is not None else size
        end = rep.prev_boundary(max(start, end))
        if end <= start:
            return iter(())

        def chunk(pos):
            return min(rep.next_boundary(min(pos + proctree.CHUNK, end)), end)
        base = {"kind": "replica", "rep": (rep.out, rep.man), "kinds": kinds,
                "where": where}
        jobs = [dict(base, range=r) for r in _replica_ranges(rep, start, end, kinds)]
    elif gz.is_gz(path) or pats is None:
        return None
    else:
        try:
            fh = open(path, "rb")
        except OSError:
            return iter(())
        with fh:
            size = os.fstat(fh.fileno()).st_size
            start = entry["start"]
            if size <= start:
                return iter(())
            end = proctree._end_at(fh, until + proctree.UNTIL_SLACK, size) \
                if until is not None else size
            mm = mmap.mmap(fh.fileno(), size, access=mmap.ACCESS_READ)
            try:
                end = mm.rfind(b"\n", start, max(start, end)) + 1
                chunks = []
                pos = start
                while pos < end:
                    stop = mm.find(b"\n", min(pos + proctree.CHUNK, end) - 1, end) + 1 or end
                    chunks.append((pos, stop))
                    pos = stop
            finally:
                mm.close()
        if not chunks:
            return iter(())
        stops = [s for _p, s in chunks]

        def chunk(pos, _stops=stops):
            # the stop of the chunk that begins at `pos`, as the read computed it
            return _stops[_index(_stops, pos)]
        base = {"kind": "plain", "path": path, "size": size, "pats": pats, "events": events}
        jobs = [dict(base, chunks=chunks[i:i + PLAIN_CHUNKS])
                for i in range(0, len(chunks), PLAIN_CHUNKS)]
    common = {"cfg": cfg, "since": since, "until": until, "read": read}
    todo = iter([dict(j, **common) for j in jobs])
    ex = _executor(workers)
    primed = collections.deque()
    for job in todo:
        primed.append(ex.submit(part, job))
        if len(primed) >= PRIME:
            break
    return _replay(ex, todo, primed, workers, stats, start, end, chunk)


def _index(stops, pos):
    """The chunk that begins at `pos`: the first stop past it."""
    lo, hi = 0, len(stops)
    while lo < hi:
        mid = (lo + hi) // 2
        if stops[mid] <= pos:
            lo = mid + 1
        else:
            hi = mid
    return lo


def _replica_ranges(rep, start, end, kinds):
    """[start, end) cut at the row groups of the tables the read takes: a part
    each, so no part decodes another's rows."""
    cuts = set()
    for t, p in rep.tables:
        evs = {e for e in t.events if e is not None}
        if kinds is not None and evs and not (evs & kinds):
            continue
        offs = rep.offs(t.name, p)
        rep._file(t.name, p)
        for f in rep._rg[t.name][1:-1]:
            o = int(offs[f])
            if start < o < end:
                cuts.add(o)
    bounds = [start] + sorted(cuts) + [end]
    return [(a, b) for a, b in zip(bounds, bounds[1:]) if a < b]


def _replay(ex, todo, ahead, workers, stats, start, end, chunk):
    keys = ("skipped", "scanned", "parse_errors", "no_ts")
    with _pool_lock:
        _pool["busy"] += 1

    def submit():
        # the file being read keeps its workers busy, and AHEAD more
        for job in todo:
            ahead.append(ex.submit(part, job))
            if len(ahead) >= workers + AHEAD:
                return

    def gain(d):
        for k, v in zip(keys, d):
            if v:
                stats[k] += v

    pos, stop = start, chunk(start)
    stats["bytes"] += stop - pos
    pending = [0, 0, 0, 0]
    try:
        submit()
        while ahead:
            got = ahead.popleft().result()
            _pool["used"] = time.time()
            if got is None:             # its read was cancelled: nothing more is read
                return
            submit()
            items, tail, halt = got
            for off, ts, rec, d in items:
                while off >= stop:
                    pos, stop = stop, chunk(stop)
                    stats["bytes"] += stop - pos
                gain([a + b for a, b in zip(pending, d)])
                pending = [0, 0, 0, 0]
                yield ts, rec
            pending = [a + b for a, b in zip(pending, tail)]
            if halt is not None:
                while halt >= stop:
                    pos, stop = stop, chunk(stop)
                    stats["bytes"] += stop - pos
                gain(pending)
                return
        gain(pending)
        while stop < end:
            pos, stop = stop, chunk(stop)
            stats["bytes"] += stop - pos
    finally:
        for f in ahead:
            f.cancel()
        with _pool_lock:
            _pool["busy"] -= 1
            _pool["used"] = time.time()
