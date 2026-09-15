"""Rolling per-minute aggregates over the live window.

A 24-hour window cannot hold raw events: at ~1.5 KB per exit record and a
real-node exec rate one to two orders of magnitude below the collector's 830/s
benchmark, a login fleet produces on the order of 10-20 GB/day.  So the charts
read BUCKETS -- 1440 slots x a handful of counters, tens of KB -- while the raw
events survive only as a bounded tail, and anything needing real rows re-scans
the day-files on demand.

Time comes from `ts_epoch` (schema 5).  For an older record we fall back to
parsing the local-naive `ts`, and a record from the near future is clamped into
the newest bin rather than dropped: a host with a skewed clock would otherwise
silently vanish from the rate chart.  Anything beyond the tolerance is counted.
"""
import time
from datetime import datetime

from .normalize import epoch_of as _normalize_epoch

TS_FORMAT = "%Y-%m-%d %H:%M:%S"


def _epoch_of(rec):
    """One definition, shared with normalize.epoch_of -- two would drift."""
    e = rec.get("_ts_epoch")
    if isinstance(e, (int, float)):
        return float(e)
    return _normalize_epoch(rec)


class RollingBuckets:
    """Fixed-width time bins over `window_s`, advanced lazily.

    Counters are per (bin, class).  `classes` is closed -- an unexpected class
    is still counted under its own key so nothing is silently folded, but the
    UI renders only the three physical classes plus `unlabeled`.
    """

    def __init__(self, window_s=86400, bin_s=60, skew_s=120):
        self.window_s = window_s
        self.bin_s = bin_s
        self.skew_s = skew_s
        self.n = max(1, window_s // bin_s)
        self.bins = {}          # bin_index -> {class -> {counter -> value}}
        self.newest = None      # newest bin index seen
        self.skew_dropped = 0
        self.no_ts = 0

    def _index(self, epoch):
        return int(epoch // self.bin_s)

    def add(self, rec, cls, **counters):
        e = _epoch_of(rec)
        if e is None:
            self.no_ts += 1
            return False
        now = time.time()
        if e > now + self.skew_s:
            self.skew_dropped += 1
            return False
        if e > now:
            e = now                      # small skew: clamp into the newest bin
        idx = self._index(e)
        cutoff = self._index(now) - self.n
        if idx < cutoff:
            return False                 # older than the window
        if self.newest is None or idx > self.newest:
            self.newest = idx
        slot = self.bins.setdefault(idx, {}).setdefault(cls, {})
        for k, v in counters.items():
            if v is None:
                continue                 # null is not zero: absent, not measured
            slot[k] = slot.get(k, 0) + v
        slot["n"] = slot.get("n", 0) + 1
        return True

    def evict(self, now=None):
        now = now or time.time()
        cutoff = self._index(now) - self.n
        for idx in [i for i in self.bins if i < cutoff]:
            del self.bins[idx]

    def series(self, classes, counter="n", now=None):
        """One row per bin, oldest first, aligned to wall-clock minutes.

        A bin with no records is `None`, never 0 -- the chart must show a gap,
        not a measured zero.
        """
        now = now or time.time()
        end = self._index(now)
        rows = []
        for idx in range(end - self.n + 1, end + 1):
            slot = self.bins.get(idx)
            row = {"t": time.strftime("%H:%M", time.localtime(idx * self.bin_s)),
                   "epoch": idx * self.bin_s}
            for c in classes:
                row[c] = (slot.get(c, {}).get(counter) if slot else None)
            rows.append(row)
        return rows

    def totals(self, classes=None):
        out = {}
        for slot in self.bins.values():
            for cls, counters in slot.items():
                if classes and cls not in classes:
                    continue
                d = out.setdefault(cls, {})
                for k, v in counters.items():
                    d[k] = d.get(k, 0) + v
        return out

    def last_complete_bin(self, classes, now=None):
        """The newest bin that is definitely finished -- never the in-progress one."""
        now = now or time.time()
        idx = self._index(now) - 1
        slot = self.bins.get(idx, {})
        return {c: slot.get(c, {}).get("n", 0) for c in classes}
