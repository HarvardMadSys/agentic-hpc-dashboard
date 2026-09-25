"""Rolling per-minute aggregates over the live window.

A multi-day window cannot hold raw events: at ~1.5 KB per exit record and a
real-node exec rate one to two orders of magnitude below the collector's 830/s
benchmark, a login fleet produces on the order of 10-20 GB/day.  So the charts
read BUCKETS -- one slot per minute x a handful of counters, tens of KB per day
retained -- while the raw events survive only as a bounded tail, and anything
needing real rows re-scans the day-files on demand.

RETENTION and VIEW are different numbers.  The buckets are sized once, at
`window_s`, and that is what the process holds; a reader then asks for any
sub-window of it, which is an exact slice because the bins are fixed-width and
wall-clock aligned.  A view WIDER than retention is not silently served -- the
caller clamps and says so -- because inventing bins the process never held is
the same class of mistake as imputing a zero.

Time comes from `ts_epoch` (schema 5).  For an older record we fall back to
parsing the local-naive `ts`, and a record from the near future is clamped into
the newest bin rather than dropped: a host with a skewed clock would otherwise
silently vanish from the rate chart.  Anything beyond the tolerance is counted.
"""
import time

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

    def _span(self, now, window_s=None):
        """The half-open bin range [start, end] a view covers.

        ONE definition, used by `series`, `totals` and `covers` alike: the
        headline number under a chart is the sum of exactly the bins the chart
        drew, never a separately-derived figure that could drift from it.
        """
        end = self._index(now)
        n = self.n if window_s is None else max(1, int(window_s) // self.bin_s)
        return end - min(n, self.n) + 1, end

    def covers(self, now=None, window_s=None):
        """What a view actually spans, in epoch seconds -- start of the oldest
        bin to the end of the newest, so `from`/`to` line up with the chart."""
        now = now or time.time()
        start, end = self._span(now, window_s)
        return start * self.bin_s, (end + 1) * self.bin_s

    def oldest_epoch(self):
        """Start of the oldest bin holding data, or None when nothing landed.

        This is what the retained window really reaches back to, which after a
        cold start is younger than the configured retention.  Reporting it is
        how a short backfill reads as a gap instead of as a quiet fleet.
        """
        return min(self.bins) * self.bin_s if self.bins else None

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

    def series(self, classes, counter="n", now=None, window_s=None):
        """One row per bin, oldest first, aligned to wall-clock minutes.

        A bin with no records is `None`, never 0 -- the chart must show a gap,
        not a measured zero.  A narrower `window_s` slices the same bins rather
        than re-deriving them: the 1-hour view is the last 60 rows of the 7-day
        view, to the record.

        The label carries the date once the view is wider than a day: `14:03`
        alone is ambiguous across a multi-day window, and an axis that repeats
        the same seven ticks is worse than no tick at all.
        """
        now = now or time.time()
        start, end = self._span(now, window_s)
        fmt = "%H:%M" if (end - start + 1) * self.bin_s <= 86400 else "%m-%d %H:%M"
        rows = []
        for idx in range(start, end + 1):
            slot = self.bins.get(idx)
            row = {"t": time.strftime(fmt, time.localtime(idx * self.bin_s)),
                   "epoch": idx * self.bin_s}
            for c in classes:
                row[c] = (slot.get(c, {}).get(counter) if slot else None)
            rows.append(row)
        return rows

    def totals(self, classes=None, now=None, window_s=None):
        """Summed counters over the SAME bins `series` would draw."""
        start, end = self._span(now or time.time(), window_s)
        out = {}
        for idx, slot in self.bins.items():
            if idx < start or idx > end:
                continue
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
