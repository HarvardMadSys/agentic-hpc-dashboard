"""Streaming reducers: one pass over the records, dispatched by event type.

A multi-GB day-file must be read once, so the reducers share a single pass --
but each owns its own state and result shape, so the six panel requirements are
separately testable.  `EVENTS` is the subscription: a residency reducer never
sees 20M exit records, and the live reducer never sees `meta`.

Two conventions every reducer obeys:

* **Null is not zero.** A nullable field is accumulated as (sum, n_present) and
  reported with a `coverage_pct`; it is never imputed to 0, because the collector
  emits null precisely when a BPF block was unavailable.
* **Count and cost, side by side.** Any per-category result carries both an
  event-weighted and a CPU-weighted share. Showing one alone is the specific
  error this research most wants to prevent.
"""
import collections
import math


class Reducer:
    EVENTS = ()
    KEY = None

    def __init__(self, cfg, classes):
        self.cfg = cfg
        self.classes = list(classes)

    def feed(self, rec):
        raise NotImplementedError

    def result(self):
        raise NotImplementedError


class Acc:
    """(sum, n_present) for a nullable field -- never imputes a zero."""

    __slots__ = ("total", "n", "seen")

    def __init__(self):
        self.total = 0.0
        self.n = 0
        self.seen = 0

    def add(self, v):
        self.seen += 1
        if v is None:
            return
        self.total += v
        self.n += 1

    def merge(self, o):
        self.total += o.total
        self.n += o.n
        self.seen += o.seen

    def as_dict(self):
        return {"total": round(self.total, 3), "n": self.n,
                "coverage_pct": round(100.0 * self.n / self.seen, 2) if self.seen else None,
                "mean": round(self.total / self.n, 4) if self.n else None}


class Quant:
    """Quantiles from a mergeable sketch of EVERY value, to within REL_ERR.

    A window is merged from saved per-hour states (`hours`), and a capped list
    of sampled values does not merge: the first 200 000 values of each hour
    are not the first 200 000 of the day. So each value lands in a
    logarithmic bucket (DDSketch: Masson, Rim and Lee, VLDB 2019) whose width
    is a fixed share of its magnitude, and two sketches merge by adding their
    bucket counts -- a merged sketch is the very sketch one pass would have
    built. A quantile read from it is within REL_ERR of the true value, at any
    rank, for any number of values; `n`, `max` and `mean` are exact, and
    nothing is sampled any more, which the payload says (`sampled` false,
    `rel_err`).
    """

    __slots__ = ("pos", "neg", "zero", "seen", "n_null", "n_zero", "n", "total",
                 "lo", "hi", "buf")

    REL_ERR = 0.01
    _GAMMA = (1 + REL_ERR) / (1 - REL_ERR)
    _INV_LG = 1.0 / math.log(_GAMMA)
    # Below this a value is read as 0: a 1 ns wait is not a different
    # distribution from none, and it keeps the bucket range finite.
    _TINY = 1e-9
    # Values are bucketed a batch at a time, in one tight loop: `add` runs for
    # nine fields of every exit, and a log per call there was most of its cost.
    _BATCH = 4096

    def __init__(self, cap=None):
        # `cap` was the sample size of the capped list this replaced; a sketch
        # keeps every value, so it is accepted and ignored.
        self.pos = {}           # bucket index -> count, values > _TINY
        self.neg = {}           # the same for values < -_TINY, by magnitude
        self.zero = 0           # |value| <= _TINY
        self.seen = 0
        self.n_null = 0
        self.n_zero = 0         # exactly 0, for the not-measured check
        self.n = 0
        self.total = 0.0
        self.lo = None
        self.hi = None
        self.buf = []           # values not yet bucketed

    def add(self, v):
        self.seen += 1
        if v is None:
            self.n_null += 1
            return
        buf = self.buf
        buf.append(v)
        if len(buf) >= self._BATCH:
            self._flush()

    def _flush(self):
        buf = self.buf
        if not buf:
            return
        self.n += len(buf)
        self.total += sum(buf)
        hi, lo = max(buf), min(buf)
        if self.hi is None or hi > self.hi:
            self.hi = hi
        if self.lo is None or lo < self.lo:
            self.lo = lo
        tiny, idx = self._TINY, self._index
        up = [v for v in buf if v > tiny]
        down = [-v for v in buf if v < -tiny]
        for mine, vals in ((self.pos, up), (self.neg, down)):
            if vals:
                for i, c in collections.Counter(idx(vals)).items():
                    mine[i] = mine.get(i, 0) + c
        zero = len(buf) - len(up) - len(down)
        nz = buf.count(0) if zero else 0
        self.zero += zero
        self.n_zero += nz
        self.buf = []

    # Saved bucketed: a pending batch would otherwise be pickled value by value.
    def __getstate__(self):
        self._flush()
        return {k: getattr(self, k) for k in self.__slots__}

    def __setstate__(self, st):
        for k in self.__slots__:
            setattr(self, k, st.get(k, [] if k == "buf" else None))

    def merge(self, o):
        self._flush()
        o._flush()
        for mine, theirs in ((self.pos, o.pos), (self.neg, o.neg)):
            for i, c in theirs.items():
                mine[i] = mine.get(i, 0) + c
        self.zero += o.zero
        self.seen += o.seen
        self.n_null += o.n_null
        self.n_zero += o.n_zero
        self.n += o.n
        self.total += o.total
        if o.hi is not None and (self.hi is None or o.hi > self.hi):
            self.hi = o.hi
        if o.lo is not None and (self.lo is None or o.lo < self.lo):
            self.lo = o.lo

    @classmethod
    def _index(cls, vals):
        """Each value's bucket, ceil(log_gamma(v)), in C-level maps."""
        return map(math.ceil, map(cls._INV_LG.__mul__, map(math.log, vals)))

    def _value(self, i):
        return 2.0 * self._GAMMA ** i / (self._GAMMA + 1.0)

    def _ranks(self, ranks):
        """The value at each 0-based rank (ascending, sorted), clamped to the
        exact min and max."""
        out, k, c = [], 0, 0
        buckets = [(-self._value(i), n) for i, n in sorted(self.neg.items(), reverse=True)]
        buckets.append((0.0, self.zero))
        buckets += [(self._value(i), n) for i, n in sorted(self.pos.items())]
        for v, n in buckets:
            c += n
            while k < len(ranks) and ranks[k] < c:
                out.append(min(self.hi, max(self.lo, v)))
                k += 1
            if k == len(ranks):
                break
        out += [self.hi] * (len(ranks) - len(out))
        return out

    def as_dict(self):
        self._flush()
        n = self.n
        if not n:
            return {"n": 0, "coverage_pct": 0.0 if self.seen else None, "sampled": False}
        ps = (.10, .25, .50, .75, .90, .99)
        # the rank the capped list read, `int(p * (n - 1))`, over every value
        qs = self._ranks([int(p * (n - 1)) for p in ps])
        # A field that is PRESENT on every record and exactly 0 on every record is
        # almost never a real distribution -- it is a counter the collector could
        # not populate, reported as 0 instead of null. Charting p50=0 would assert
        # a measurement that was never made, so the flag lets the UI say
        # "not measured" instead. (Observed for real: peak_rss_mb is structurally
        # 0 at the sched_process_exit tracepoint, because the kernel runs
        # exit_mm() -- clearing task->mm -- before the tracepoint fires.)
        all_zero = self.n_zero == n
        out = {"n": n}
        out.update(("p%d" % round(p * 100), round(q, 4)) for p, q in zip(ps, qs))
        out.update({
            "max": round(self.hi, 4),
            "mean": round(self.total / n, 4),
            "sampled": False, "sample_n": n, "rel_err": self.REL_ERR,
            "zero_pct": round(100.0 * self.n_zero / n, 2),
            "all_zero": all_zero,
            "suspect": ("every value is exactly 0 -- treat as not measured, "
                        "not as a measured zero" if all_zero else None),
            "coverage_pct": round(100.0 * n / self.seen, 2) if self.seen else None})
        return out


def add_counts(dst, src):
    """Add `src`'s counts into `dst` key by key; new keys land in `src`'s order,
    as they would have arrived."""
    for k, v in src.items():
        dst[k] = dst.get(k, 0) + v
    return dst


def min_of(a, b):
    return b if a is None else a if b is None else min(a, b)


def max_of(a, b):
    return b if a is None else a if b is None else max(a, b)


# Per-row instance samples are capped everywhere: the aggregate answers "how
# much", the sample answers "who exactly", and an unbounded list would make the
# payload scale with the feed.
INSTANCE_CAP = 12


def who(rec, with_args=True):
    """Who ran it -- pid, user, actor, tool, and the outcome.

    `with_args=False` for any panel where the argv is itself the finding that
    must not be reproduced (the secrets panel).
    """
    w = {"pid": rec.get("pid"), "ppid": rec.get("ppid"),
         "user": rec.get("user"), "actor3": rec.get("_a3"),
         "agent_type": rec.get("agent_type"), "host": rec.get("host"),
         "ts": rec.get("ts"), "tool": rec.get("_eff") or rec.get("_command"),
         "session_key": rec.get("session_key"),
         "cpu_s": rec.get("cpu_s"), "duration_s": rec.get("duration_s"),
         "exit_code": rec.get("exit_code"), "signal": rec.get("signal")}
    if with_args:
        w["args"] = (rec.get("args") or "")[:220]
    return w


def top_by(bucket, key, cap=INSTANCE_CAP, trim_at=None):
    """Keep a bounded list of the heaviest entries without holding them all."""
    trim_at = trim_at or cap * 20
    if len(bucket) > trim_at:
        bucket.sort(key=key)
        del bucket[cap * 2:]
    return bucket


def pct(part, whole):
    return round(100.0 * part / whole, 2) if whole else 0.0
