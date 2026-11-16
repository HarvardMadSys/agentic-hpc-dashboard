"""How long Python's garbage collector holds the process, and keeping it short.

A collection runs on whichever thread happens to allocate -- the event loop's
included -- and holds the GIL throughout. The youngest generations are cheap; a
full one walks every tracked object the process holds, and a window's reducers
are millions of them, so on a busy feed a full collection held everything for
seconds (#28's commit measured up to 2.5 s each, during merges).

`install()` adds a callback that times every collection, by generation, for
`/api/health` -- counts and milliseconds, nothing else. `settle()` moves what is
alive now into the permanent generation (`gc.freeze`), so the next full
collections walk only what was made since: called once the startup backfill
has filled the live tier and whenever a window's reducers are swapped in, the
two moments the heap takes on most of what it will keep. Objects freed by
reference counting go as before; the one thing freezing changes is that a
reference cycle among frozen objects is not collected, so `settle()` unfreezes
and collects everything again at most every `FULL_EVERY_S`, off the request
path (the rebuild tick), which bounds what such a cycle could hold.
"""
import gc
import threading
import time

FULL_EVERY_S = 3600.0

_lock = threading.Lock()
_state = {"installed": False, "t0": None, "frozen_at": None, "full_at": None,
          "settles": 0, "full_collects": 0, "last_full_ms": None}
_gens = {g: {"n": 0, "total_ms": 0.0, "max_ms": 0.0} for g in (0, 1, 2)}


def _callback(phase, info):
    if phase == "start":
        _state["t0"] = time.perf_counter()
        return
    t0 = _state["t0"]
    if t0 is None:
        return
    ms = 1000.0 * (time.perf_counter() - t0)
    g = _gens.get(info.get("generation"))
    if g is not None:
        g["n"] += 1
        g["total_ms"] += ms
        if ms > g["max_ms"]:
            g["max_ms"] = ms


def install():
    """Time every collection from now on (once per process)."""
    with _lock:
        if not _state["installed"]:
            gc.callbacks.append(_callback)
            _state["installed"] = True


def settle(full_every_s=FULL_EVERY_S):
    """Freeze what is alive now; collect all of it again first when the last full
    collection is older than `full_every_s`. Returns what it did."""
    now = time.monotonic()
    with _lock:
        # Nothing is frozen before the first call, so it has nothing to bound: it
        # freezes, and starts the clock the full collections run on.
        if _state["full_at"] is None:
            _state["full_at"] = now
        full = now - _state["full_at"] >= full_every_s
        if full:
            t = time.perf_counter()
            gc.unfreeze()
            gc.collect()
            _state["last_full_ms"] = round(1000.0 * (time.perf_counter() - t), 1)
            _state["full_at"] = now
            _state["full_collects"] += 1
        gc.freeze()
        _state["frozen_at"] = now
        _state["settles"] += 1
    return "collected and froze" if full else "froze"


def status():
    """For /api/health: per generation, how many collections and how long."""
    return {"generations": {str(g): {"n": v["n"], "total_ms": round(v["total_ms"], 1),
                                     "max_ms": round(v["max_ms"], 1)}
                            for g, v in _gens.items()},
            "frozen": gc.get_freeze_count(), "settles": _state["settles"],
            "full_collects": _state["full_collects"], "last_full_ms": _state["last_full_ms"],
            "threshold": list(gc.get_threshold())}
