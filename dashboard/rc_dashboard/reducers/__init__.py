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

    def as_dict(self):
        return {"total": round(self.total, 3), "n": self.n,
                "coverage_pct": round(100.0 * self.n / self.seen, 2) if self.seen else None,
                "mean": round(self.total / self.n, 4) if self.n else None}


class Quant:
    """Reservoir-free exact quantiles over a capped sample.

    Capped because a 24h window can hold millions of values and the panel only
    needs a distribution shape; the cap is reported so a reader knows whether the
    quantiles are exact or sampled.
    """

    __slots__ = ("vals", "cap", "seen", "n_null", "n_zero")

    def __init__(self, cap=200000):
        self.vals = []
        self.cap = cap
        self.seen = 0
        self.n_null = 0
        self.n_zero = 0

    def add(self, v):
        self.seen += 1
        if v is None:
            self.n_null += 1
            return
        if v == 0:
            self.n_zero += 1
        if len(self.vals) < self.cap:
            self.vals.append(v)

    def as_dict(self):
        if not self.vals:
            return {"n": 0, "coverage_pct": 0.0 if self.seen else None, "sampled": False}
        v = sorted(self.vals)
        n = len(v)

        def q(p):
            return round(v[min(n - 1, int(p * (n - 1)))], 4)
        measured = self.seen - self.n_null
        # A field that is PRESENT on every record and exactly 0 on every record is
        # almost never a real distribution -- it is a counter the collector could
        # not populate, reported as 0 instead of null. Charting p50=0 would assert
        # a measurement that was never made, so the flag lets the UI say
        # "not measured" instead. (Observed for real: peak_rss_mb is structurally
        # 0 at the sched_process_exit tracepoint, because the kernel runs
        # exit_mm() -- clearing task->mm -- before the tracepoint fires.)
        all_zero = measured > 0 and self.n_zero == measured
        return {"n": measured, "p10": q(.10), "p25": q(.25), "p50": q(.50),
                "p75": q(.75), "p90": q(.90), "p99": q(.99),
                "max": round(v[-1], 4),
                "mean": round(sum(v) / n, 4),
                "sampled": n < measured, "sample_n": n,
                "zero_pct": round(100.0 * self.n_zero / measured, 2) if measured else None,
                "all_zero": all_zero,
                "suspect": ("every value is exactly 0 -- treat as not measured, "
                            "not as a measured zero" if all_zero else None),
                "coverage_pct": round(100.0 * measured / self.seen, 2) if self.seen else None}


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
         "ts": rec.get("ts"), "tool": rec.get("_eff") or rec.get("comm"),
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
