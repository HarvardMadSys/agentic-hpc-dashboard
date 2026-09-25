"""Requirement 2: the live tier, all three actor classes, measured.

Everything here replaces a fabricated equivalent.  The retired builder's
`rc_synth.live()` hardcoded `running_agent_jobs: 1184`, `pending: 417`,
`agent_gpu_jobs: 263`, `agent_gpu_idle_now: 61`, and the viewer's `tick()`
manufactured new exit events and rate samples every three seconds.  None of that
survives: a bin with no records is `null`, and a quantity with no feed is absent
with a notice rather than plausible.

Three of those four plates came from the scheduler census and dcgm, which are not
configured feeds.  Rather than carry the numbers over or leave a placeholder, the
jobs plate is replaced by one built from eBPF `submit` records -- agent job
submissions in the window, which is per-submission AND actor-attributed, and so
is better evidence than a census count.
"""
import collections
import time

from ..buckets import RollingBuckets
from . import pct


def _sum_or_none(vals):
    """Sum what was reported; None when nothing was, so an absent counter never
    renders as a measured 0."""
    seen = [v for v in vals if v is not None]
    return sum(seen) if seen else None


def _conn_count(v, kind="ext"):
    """One class's standing-socket count from `tcp_open_ext_by_actor3`.

    Schema 5 reports a triple per class -- {open, ext, estab} -- separating all
    standing sockets from the externally-addressed ones from the established
    ones. A bare int is accepted too, so an older or narrower emitter still
    reads rather than raising. `None` stays None: zero standing sockets and
    "the collector did not report" are different facts.
    """
    if v is None:
        return None
    if isinstance(v, dict):
        return v.get(kind)
    if isinstance(v, (int, float)):
        return v
    return None


class LiveReducer:
    EVENTS = ("exit", "residency", "residency_totals", "submit", "truncated")
    KEY = "live"

    def __init__(self, cfg, classes):
        self.cfg = cfg
        self.classes = list(classes)
        # ONE instance, sized at RETENTION.  Every selected window is a slice of
        # these bins rather than its own accumulator: two accumulators over the
        # same stream are two chances to disagree about the same minute.
        self.retention_s = int(cfg.get("live.retention_min", 10080)) * 60
        self.bin_s = int(cfg.get("live.bin_s", 60))
        self.buckets = RollingBuckets(self.retention_s, self.bin_s,
                                      int(cfg.get("live.clock_skew_s", 120)))
        self.event_tail = collections.deque(maxlen=int(cfg.get("live.event_tail", 500)))
        self.submit_tail = collections.deque(maxlen=int(cfg.get("live.submit_tail", 50)))
        self.hosts = {}          # host -> newest residency_totals-derived rollup
        self.trees = {}          # (host, tree_root_pid) -> newest residency tick
        self.window_s = self.retention_s
        self.submits_in_window = collections.deque()
        self.conn_unmatched_births = None

    # ------------------------------------------------------------------ feed
    def feed(self, rec):
        ev = rec.get("event")
        if ev in ("exit", "truncated"):
            self._exit(rec)
        elif ev == "residency":
            self._residency(rec)
        elif ev == "residency_totals":
            self._totals(rec)
        elif ev == "submit":
            self._submit(rec)

    def _exit(self, rec):
        self.buckets.add(rec, rec["_a3"], cpu_s=rec.get("cpu_s"))
        io = rec.get("io") or {}
        self.event_tail.appendleft({
            "ts": rec.get("ts"), "epoch": rec.get("_ts_epoch"),
            "host": rec.get("host"), "user": rec.get("user"),
            "actor3": rec["_a3"], "agent_type": rec.get("agent_type"),
            "comm": rec.get("comm"), "tool": rec.get("_eff"),
            "args": (rec.get("args") or "")[:400],
            "purpose": rec.get("_purpose"), "depth": rec.get("depth"),
            "pid": rec.get("pid"),
            "duration_s": rec.get("duration_s"), "cpu_s": rec.get("cpu_s"),
            "peak_rss_mb": rec.get("peak_rss_mb"),
            "rd_mb": io.get("rd_mb"), "wr_mb": io.get("wr_mb"),
            "exit_code": rec.get("exit_code"), "signal": rec.get("signal"),
            "sandbox": rec.get("_sandbox"), "approval": rec.get("_approval"),
            "session_key": rec.get("session_key"),
        })

    def _residency(self, rec):
        key = (rec.get("host"), rec.get("tree_root_pid") or rec.get("agent_pid"))
        self.trees[key] = {
            "host": rec.get("host"), "agent_type": rec.get("agent_type"),
            "age_s": rec.get("age_s"), "n_procs": rec.get("n_procs"),
            "threads": rec.get("threads"), "rss_mb": rec.get("rss_mb"),
            "cpu_s": rec.get("cpu_s"), "d_state": rec.get("d_state"),
            "headless": rec.get("headless"), "max_depth": rec.get("max_depth"),
            "tcp_open_ext": rec.get("tcp_open_ext"),
            "state_counts": rec.get("state_counts"),
            "top_procs": rec.get("top_procs"),
            "sandbox": rec.get("_sandbox"), "approval": rec.get("_approval"),
            "epoch": rec.get("_ts_epoch"), "user": rec.get("user"),
            "actor3": rec.get("_a3"),
        }

    def _totals(self, rec):
        h = rec.get("host") or "?"
        self.hosts[h] = {
            "host": h, "ts": rec.get("ts"), "epoch": rec.get("_ts_epoch"),
            "by_actor3": rec.get("by_actor3") or {},
            "by_agent_type": rec.get("by_agent_type") or {},
            "trees": rec.get("trees"), "tracked_live": rec.get("tracked_live"),
            "conn_records": rec.get("conn_records"),
            "tcp_open_ext": (rec.get("tcp_open_ext_by_actor3") or {}),
            "fork_rate": rec.get("fork_rate"),
            "dropped": (rec.get("dropped") or {}).get("n"),
            "d_stack_top": rec.get("d_stack_top"),
            "conn_unmatched_births": rec.get("conn_unmatched_births"),
        }
        if rec.get("conn_unmatched_births") is not None:
            self.conn_unmatched_births = rec["conn_unmatched_births"]

    def _submit(self, rec):
        row = {
            "ts": rec.get("ts"), "epoch": rec.get("_ts_epoch"),
            "host": rec.get("host"), "user": rec.get("user"),
            "actor3": rec["_a3"], "agent_type": rec.get("agent_type"),
            "tool": rec.get("tool"), "job_id": rec.get("job_id"),
            "job_ids": rec.get("job_ids"),
            "work_dir": rec.get("work_dir"), "exit_code": rec.get("exit_code"),
            # schema 5 parses these from the tool's argv; null when the request
            # came from #SBATCH directives in the script instead
            "partition": rec.get("partition"), "gpus": rec.get("gpus"),
            "array": rec.get("array"), "time_limit_s": rec.get("time_limit_s"),
            "mem": rec.get("mem"), "cpus_per_task": rec.get("cpus_per_task"),
            "req_src": rec.get("req_src"),
        }
        self.submit_tail.appendleft(row)
        self.submits_in_window.append(row)

    # ---------------------------------------------------------------- result
    def result(self, now=None, window_s=None):
        """The live tier as seen through `window_s` (default: all of retention).

        Everything time-bounded here reads the SAME cutoff -- the rate series,
        its headline totals, the submissions and the event tail -- so a reader
        never sees a chart and a plate that disagree about what "in window"
        means.  The host and residency plates are deliberately NOT cut: they are
        a snapshot of what is running now, not an aggregate over the window, and
        narrowing the window does not make a running process stop running.
        """
        now = now or time.time()
        self.buckets.evict(now)
        # Prune to retention, then read through the view's own cutoff: the deque
        # is shared by every window, so it must hold the widest one.
        while (self.submits_in_window
               and (self.submits_in_window[0]["epoch"] or 0) < now - self.retention_s):
            self.submits_in_window.popleft()
        window_s = min(int(window_s or self.retention_s), self.retention_s)
        cutoff, _end = self.buckets.covers(now, window_s)

        rate = self.buckets.series(self.classes, "n", now, window_s)
        last = self.buckets.last_complete_bin(self.classes, now)
        totals = self.buckets.totals(now=now, window_s=window_s)

        # plates: all three classes, from residency_totals
        agg = collections.defaultdict(lambda: collections.Counter())
        for h in self.hosts.values():
            for cls, d in (h["by_actor3"] or {}).items():
                for k, v in (d or {}).items():
                    if isinstance(v, (int, float)):
                        agg[cls][k] += v
        # `residency` is emitted once per live AGENT tree, so a root count only
        # exists for the agent class; the other classes are absent rather than 0,
        # because the collector never claims to enumerate their roots.
        live_trees = sum(1 for t in self.trees.values()
                         if (t.get("epoch") or 0) >= now - 600)
        roots_by_cls = {}
        for t in self.trees.values():
            if (t.get("epoch") or 0) >= now - 600:
                c = t.get("actor3") or "agent"
                roots_by_cls[c] = roots_by_cls.get(c, 0) + 1
        autonomous = 0
        for h in self.hosts.values():
            for _t, d in (h.get("by_agent_type") or {}).items():
                autonomous += (d or {}).get("autonomous") or 0

        subs = [s for s in self.submits_in_window if (s["epoch"] or 0) >= cutoff]
        by_cls_subs = collections.Counter(s["actor3"] for s in subs)

        hosts = []
        for h, v in sorted(self.hosts.items()):
            by = v["by_actor3"] or {}
            ext = v.get("tcp_open_ext") or {}
            # Per-class breakdown is a plain {class: n_procs} map, and the flat
            # columns are the host-level totals across classes -- the host table
            # shows one row per host with a class split, not one row per pair.
            row = {
                "host": h, "ts": v["ts"],
                "by_class": {c: ((by.get(c) or {}).get("n_procs") or 0)
                             for c in self.classes if by.get(c)},
                "procs": sum((by.get(c) or {}).get("n_procs") or 0 for c in by),
                "threads": sum((by.get(c) or {}).get("threads") or 0 for c in by),
                "rss_gb": round(sum((by.get(c) or {}).get("rss_mb") or 0
                                    for c in by) / 1024.0, 2) or None,
                "d_state": sum((by.get(c) or {}).get("d_state") or 0 for c in by),
                "users": sum((by.get(c) or {}).get("users") or 0 for c in by),
                "tcp_open_ext": _sum_or_none(_conn_count(x) for x in ext.values()),
                "tcp_open": _sum_or_none(_conn_count(x, "open") for x in ext.values()),
                "tcp_estab": _sum_or_none(_conn_count(x, "estab") for x in ext.values()),
                "roots": v.get("trees"),
                "trees": v.get("trees"), "tracked_live": v.get("tracked_live"),
                "conn_records": v.get("conn_records"), "fork_rate": v.get("fork_rate"),
                "dropped": v.get("dropped"),
                # load and cpu_pct belong to the NODE tier; absent here rather
                # than zero, so the column reads as unreported not as idle
                "load_1": None, "cpu_pct": None,
                # by_actor3 carries procs/threads/rss/d_state/users/states; the
                # standing-socket triple arrives in a sibling field, so merge it
                # here rather than making the viewer join two maps.
                "by_class_detail": {
                    c: dict(
                        (by.get(c) or {}),
                        tcp_open_ext=_conn_count((ext or {}).get(c)),
                        tcp_open=_conn_count((ext or {}).get(c), "open"),
                        tcp_estab=_conn_count((ext or {}).get(c), "estab"),
                    ) for c in by
                },
            }
            hosts.append(row)
        # a host that reported residency ticks but no totals still gets a row
        seen = {r["host"] for r in hosts}
        for (host, _r), t in self.trees.items():
            if host and host not in seen:
                seen.add(host)
                hosts.append({"host": host, "ts": t.get("epoch"), "by_class": {},
                              "note": "residency only, no residency_totals tick"})

        retained = self.buckets.oldest_epoch()
        return {
            "window": {
                "minutes": window_s // 60, "bin_s": self.bin_s,
                "from": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(cutoff)),
                "to": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
                "retention_minutes": self.retention_s // 60,
                # How far the bins ACTUALLY reach back. After a cold start this
                # is younger than the window, and the page says so rather than
                # drawing empty bins that read as a quiet fleet.
                "retained_from": (time.strftime("%Y-%m-%d %H:%M:%S",
                                                time.localtime(retained))
                                  if retained else None),
                "retained_minutes": (int((now - retained) // 60)
                                     if retained else None),
            },
            "rate": rate,
            # Every per-class quantity is a plain {class: value} map under its own
            # bare name; a headline number is the map's sum, never a separate
            # figure that could drift from it.
            "now": {
                "exits_per_min": sum(last.values()),
                "exits_per_min_by_class": last,
                "events": {c: totals.get(c, {}).get("n", 0) for c in self.classes},
                "cpu_s": {c: round(totals.get(c, {}).get("cpu_s", 0.0), 1)
                          for c in self.classes},
                "procs": {c: agg[c].get("n_procs") for c in self.classes},
                "threads": {c: agg[c].get("threads") for c in self.classes},
                "rss_gb": {c: (round(agg[c]["rss_mb"] / 1024.0, 2)
                               if agg[c].get("rss_mb") else None)
                           for c in self.classes},
                "users": {c: agg[c].get("users") for c in self.classes},
                "d_state": {c: agg[c].get("d_state") for c in self.classes},
                "roots": {c: roots_by_cls.get(c) for c in self.classes},
                "live_agent_trees": live_trees,
                "autonomous_roots": autonomous or None,
                # replaces the census plate: real, in-scope, actor-attributed
                "submissions": dict(by_cls_subs),
                "submissions_window": len(subs),
                "submission_users": len({s["user"] for s in subs}) if subs else 0,
            },
            "hosts": hosts,
            # A bounded tail, cut to the window. The tail is `event_tail` deep
            # REGARDLESS of window -- it is a tail, not an aggregate -- so a wide
            # window does not deepen it; narrowing one only removes rows that
            # fell outside. `events_tail_depth` says which limit bit.
            "events": [e for e in self.event_tail if (e.get("epoch") or 0) >= cutoff],
            "events_tail_depth": self.event_tail.maxlen,
            "submits": [s for s in self.submit_tail if (s.get("epoch") or 0) >= cutoff],
            "skew_dropped": self.buckets.skew_dropped,
            "no_timestamp": self.buckets.no_ts,
            "conn_unmatched_births": self.conn_unmatched_births,
            "unavailable": {
                "running_jobs": "needs the slurm_jobs census feed (not configured)",
                "gpu_held": "needs dcgm / gpu_binding (not configured)",
            },
        }
