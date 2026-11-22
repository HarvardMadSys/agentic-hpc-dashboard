"""The node tier over time: a few numbers from every snapshot, held for retention.

The node panel (`reducers.resources.NodeReducer`) reads the newest two
snapshots per host: right now, and the gap a CPU rate needs. "Do agents make
the node slow for people?" needs the same probes over the window, beside the
agents' CPU. So this follows every node-tier day-file the way a tail does,
keeps from each snapshot only the handful of numbers drawn over time
(`FIELDS`), and serves them binned to a window (`series`).

What it costs, and why it is its own thread:

* A snapshot is ~200 KB of JSON, and the collector writes one per host every
  `EBPFM_NODE_INTERVAL` (300 s): ~60 MB a host-day. The first read covers
  retention -- a week of a fleet is gigabytes -- so it runs on a thread of its
  own, started with the service, a chunk at a time and yielding between them.
  Nothing waits for it, and the series says how far it has got (`state`).
* After that it reads only what was appended: each file's offset is kept, by
  inode, as the tail keeps them. A day-file sealed into its `.gz` is a new path
  whose snapshots are already held: read once more, they land on the same
  (host, epoch) key and change nothing.

Two rules the rest of the service keeps:

* **Null is not zero.** A snapshot without a probe holds None for it, and a bin
  no snapshot of a host landed in is a hole in that host's line.
* **No resolution the samples never had.** A bin is at least twice the
  interval the snapshots arrive at, so a snapshot that drifts across a bin
  edge does not open a hole that would read as an outage.
"""
import bisect
import math
import os
import statistics
import threading
import time

from . import fastjson, gz
from .feedfile import dated_dirs, host_of
from .reducers.resources import BIN_STEPS_S, _local_epoch, bin_for

# What each snapshot keeps -- the four probes Live draws (NodeResponse.tsx) --
# and how a bin of them is read: a latency is the bin's worst, the slowest probe
# a person could have met in it, and a level its mean.
FIELDS = (
    ("fork_exec_ms", "max"),    # responsiveness: start a process
    ("getent_ms", "max"),       # responsiveness: look an account up
    ("load_1", "mean"),         # load average over a minute
    ("nfs_rd_rtt_ms", "max"),   # the slowest NFS mount's read round trip
)
KEYS = tuple(k for k, _how in FIELDS)

# Bytes read per step, between which the thread yields.
CHUNK = 4 << 20
# How long the thread rests between passes over the files.
IDLE_S = 60
# Gaps between a host's snapshots read for its interval: the newest this many.
GAPS = 64


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def point(rec):
    """A snapshot's FIELDS, in order, None where it did not report one."""
    up = rec.get("uptime") or {}
    resp = rec.get("responsiveness") or {}
    # nfsiostat lists only the mounts that did I/O in its interval: none listed
    # is no round trip measured, not a round trip of 0
    rtts = [v for v in (_num(m.get("rd_rtt_ms")) for m in (rec.get("nfs_io") or [])
                        if isinstance(m, dict)) if v is not None]
    return (
        _num(resp.get("fork_exec_ms")), _num(resp.get("getent_ms")),
        _num(up.get("load_1")), max(rtts) if rtts else None,
    )


def epoch_of(rec):
    e = rec.get("ts_epoch")
    if isinstance(e, (int, float)) and not isinstance(e, bool):
        return float(e)
    return _local_epoch(rec.get("timestamp"))


class NodeHistory:
    def __init__(self, cfg):
        self.cfg = cfg
        self.retention_s = int(cfg.get("live.retention_min", 10080)) * 60
        # Day-dirs read: enough for retention, whatever the node panel's own
        # `days` (which only needs the newest snapshots).
        self.days = int(cfg.get("feeds.ebpf_node.series_days") or 0) or \
            int(math.ceil(self.retention_s / 86400.0)) + 1
        self.lock = threading.Lock()
        self.points = {}          # host -> {epoch: point}
        self.version = 0          # bumped by every point added or pruned
        self.offsets = {}         # path -> (inode ident, offset read to, read whole)
        self.state = "idle"       # idle -> reading -> ready
        self.read = {"files": 0, "lines": 0, "bad": 0, "bytes": 0}
        self.error = None
        self.passes = 0
        self._sorted = (None, {})
        self._stop = threading.Event()
        self._thread = None

    # ------------------------------------------------------------ the thread
    def start(self):
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="nodehist", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _run(self):
        while not self._stop.is_set():
            try:
                self.scan()
                self.error = None
            except Exception as e:           # a bad pass must not end the thread
                self.error = str(e)
            self._stop.wait(IDLE_S)

    # ------------------------------------------------------------- reading
    def files(self):
        out = set()
        for root in self.cfg.get("feeds.ebpf_node.roots") or []:
            for _date, found in dated_dirs(root, self.days):
                out.update(found)
        return sorted(out)

    def scan(self):
        """One pass: every file in the day-dirs held, from where it was left."""
        if self.state == "idle":
            self.state = "reading"
        files = self.files()
        keep = set(files)
        for p in [p for p in self.offsets if p not in keep]:
            del self.offsets[p]
        for path in files:
            if self._stop.is_set():
                return
            self._read(path)
        self.prune()
        self.passes += 1
        self.state = "ready"

    def _read(self, path):
        try:
            st = os.stat(path)
        except OSError:
            return
        ident = (st.st_dev, st.st_ino)
        sealed = gz.is_gz(path)
        prev = self.offsets.get(path)
        off = 0
        if prev is not None and prev[0] == ident:
            if sealed and prev[2]:
                return                        # a sealed day, read whole already
            if not sealed:
                if st.st_size == prev[1]:
                    return                    # nothing appended
                off = prev[1] if st.st_size > prev[1] else 0   # rewritten: again
        pos, rest = off, b""
        try:
            with gz.open_feed(path) as fh:
                if off:
                    fh.seek(off)
                while not self._stop.is_set():
                    chunk = fh.read(CHUNK)
                    if not chunk:
                        break
                    self.read["bytes"] += len(chunk)
                    lines = (rest + chunk).split(b"\n")
                    rest = lines.pop()        # a line still being written
                    for ln in lines:
                        pos += len(ln) + 1
                        self._line(ln, path)
                    time.sleep(0)             # hand the GIL over between chunks
        except (OSError, EOFError, ValueError):
            self.read["bad"] += 1
            return
        self.read["files"] += prev is None
        # a sealed file's last line has its newline; a growing one's tail is
        # read again next pass, from the start of the line
        self.offsets[path] = (ident, pos, sealed and not rest)

    def _line(self, ln, path):
        if not ln.strip():
            return
        try:
            rec = fastjson.loads(ln)
        except fastjson.DecodeError:
            self.read["bad"] += 1
            return
        if not isinstance(rec, dict):
            self.read["bad"] += 1
            return
        e = epoch_of(rec)
        if e is None or e < time.time() - self.retention_s - 3600:
            return
        host = rec.get("hostname") or rec.get("host") or host_of(path)
        pt = point(rec)
        with self.lock:
            self.points.setdefault(host, {})[round(e, 3)] = pt
            self.version += 1
        self.read["lines"] += 1

    def prune(self, now=None):
        cutoff = (now or time.time()) - self.retention_s - 3600
        with self.lock:
            for h in list(self.points):
                held = self.points[h]
                old = [e for e in held if e < cutoff]
                for e in old:
                    del held[e]
                if old:
                    self.version += 1
                if not held:
                    del self.points[h]

    # ------------------------------------------------------------- serving
    def _held(self):
        """{host: (epochs, points)}, sorted, rebuilt only when a point changed."""
        with self.lock:
            ver, cached = self._sorted
            if ver == self.version:
                return cached
            out = {}
            for h, held in self.points.items():
                es = sorted(held)
                out[h] = (es, [held[e] for e in es])
            self._sorted = (self.version, out)
            return out

    @staticmethod
    def interval(held):
        """The usual gap between a host's snapshots, in seconds, over every host:
        the median of the newest GAPS gaps of each. None before two snapshots."""
        gaps = []
        for es, _pts in held.values():
            tail = es[-(GAPS + 1):]
            gaps += [b - a for a, b in zip(tail, tail[1:]) if b > a]
        return round(statistics.median(gaps), 1) if gaps else None

    def series(self, window_s, now=None):
        """Every host's FIELDS over the window, on bins of the window's own
        width or of twice the snapshot interval, whichever is wider."""
        now = now or time.time()
        held = self._held()
        every = self.interval(held)
        want = max(bin_for(window_s), 2 * every if every else 0)
        bin_s = next((b for b in BIN_STEPS_S if b >= want), BIN_STEPS_S[-1])
        lo, hi = int((now - window_s) // bin_s), int(now // bin_s)
        n = hi - lo + 1
        hosts = {}
        oldest = None
        for h, (es, pts) in sorted(held.items()):
            if es and (oldest is None or es[0] < oldest):
                oldest = es[0]
            i0 = bisect.bisect_left(es, lo * bin_s)
            i1 = bisect.bisect_left(es, (hi + 1) * bin_s)
            if i0 >= i1:
                continue
            cells = [[[] for _ in range(n)] for _ in FIELDS]
            count = [0] * n
            for e, pt in zip(es[i0:i1], pts[i0:i1]):
                b = int(e // bin_s) - lo
                count[b] += 1
                for j, v in enumerate(pt):
                    if v is not None:
                        cells[j][b].append(v)
            row = {}
            for j, (k, how) in enumerate(FIELDS):
                if how == "mean":
                    row[k] = [round(sum(c) / len(c), 2) if c else None for c in cells[j]]
                else:
                    row[k] = [max(c) if c else None for c in cells[j]]
            # snapshots per bin: none is a hole, and says so on hover
            row["snapshots"] = [c or None for c in count]
            hosts[h] = row
        return {
            "bin_s": bin_s, "t": [b * bin_s for b in range(lo, hi + 1)],
            "interval_s": every, "fields": dict(FIELDS), "hosts": hosts,
            "state": self.state, "passes": self.passes, "error": self.error,
            "read": dict(self.read), "held_from": oldest, "days": self.days,
        }
