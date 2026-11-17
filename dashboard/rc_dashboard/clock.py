"""The service's present, and pinning it to the end of a captured feed.

Everything here that asks what time it is -- the live tier's newest bin, a
window's start, a build's drift and age, a feed's lag -- calls `time.time()`.
On a live feed that is right by definition. On a capture copied in from another
cluster it is not: its newest record is days old, every window ends in a
stretch of nothing, and the default 24 h view is blank.

`clock.as_of` (`RC_DASH_AS_OF`, `--as-of`) moves the service's present rather
than touching a record:

  ""                      the wall clock: the default, and the only right
                          setting for a live feed
  "auto"                  one second after the newest record in the ebpf
                          feed's newest day
  "YYYY-MM-DD HH:MM:SS"   that local time, as the collector stamps records

A pinned clock is FROZEN, not started at `as_of` and left to run. A running one
would move the windows off the end of the capture a minute per minute, and
`run.sh` restarts the service on every save: each restart would pin it back,
which dates every saved window build in the future (`windows._resume`) and
re-scans the window instead of resuming it. Frozen, every restart computes the
same windows over the same bytes, so a saved build resumes, and the page is
the same page each time it is opened -- which is what a test needs. The cost:
durations the service measures with this clock read 0 s (a build's elapsed,
the inspector's read time), and its caches never age out, which over a capture
that cannot change is correct.

It is applied by replacing `time.time` and `time.time_ns` for the whole
process rather than by threading a clock through every call site: there are
~50 across the reducers, the window registry and the API, and one missed would
mix two presents on one page. `time.monotonic` is untouched, so sleeps,
timeouts and the event loop run in real time. `datetime.now()` and an
argument-less `time.strftime()` read the C clock and are not covered; nothing
that bounds a window uses them. Every live frame carries `clock`, the page
says the clock is pinned, and the page's own "now" follows the server's.

An explicit `as_of` before the newest record is a clock, not a filter: the live
tier drops what comes after it as clock skew, while a window build still reads
it. Pin at or after the end of the capture.
"""
import os
import time

from . import gz
from .export import dated_dirs
from .normalize import epoch_of

_wall = time.time
_state = {"pinned": False, "as_of": None, "source": "wall clock"}


def newest_record(roots):
    """`(epoch, path)` of the newest record in the newest day of each root.

    Read from each file's end; a compressed file is indexed to get there, which
    the tail would do moments later anyway.
    """
    import json
    best = (None, None)
    for root in roots:
        days = dated_dirs(root, 1)
        for path in (days[-1][1] if days else []):
            try:
                with gz.open_feed(path) as fh:
                    size = gz.fstat(fh).st_size
                    fh.seek(max(0, size - (4 << 20)))
                    tail = fh.read()
            except OSError:
                continue
            for line in reversed(tail.split(b"\n")):
                if not line.strip():
                    continue
                try:
                    ts = epoch_of(json.loads(line))
                except ValueError:
                    continue
                if ts is not None:
                    if best[0] is None or ts > best[0]:
                        best = (ts, path)
                    break
    return best


def resolve(cfg, roots):
    """`(epoch, source)` the clock is pinned to, or `(None, "wall clock")`."""
    raw = str(cfg.get("clock.as_of") or "").strip()
    if not raw:
        return None, "wall clock"
    if raw == "auto":
        ts, path = newest_record(roots)
        if ts is None:
            raise SystemExit("clock.as_of=auto: no record with a timestamp in the "
                             "newest day of %s" % (", ".join(roots) or "(no ebpf root)"))
        return ts + 1, "auto: 1 s after the newest record, in %s" % os.path.basename(path)
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return time.mktime(time.strptime(raw, fmt)), "set: %s" % raw
        except ValueError:
            continue
    try:
        return float(raw), "set: epoch %s" % raw
    except ValueError:
        raise SystemExit("clock.as_of: %r is not 'auto', a local time "
                         "(YYYY-MM-DD HH:MM:SS) or an epoch" % raw)


def install(cfg, roots):
    """Pin this process's clock per `clock.as_of`. A no-op on the wall clock."""
    as_of, source = resolve(cfg, roots)
    if as_of is None:
        return status()
    ns = int(as_of * 1e9)
    time.time = lambda: as_of
    time.time_ns = lambda: ns
    _state.update(pinned=True, as_of=as_of, source=source)
    return status()


def status():
    """What every live frame says about the present it was computed at."""
    now = time.time()
    return {"pinned": _state["pinned"],
            "now": round(now, 3),
            "as_of": (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(_state["as_of"]))
                      if _state["as_of"] is not None else None),
            "source": _state["source"],
            "behind_wall_s": (round(_wall() - now, 1) if _state["pinned"] else 0.0)}
