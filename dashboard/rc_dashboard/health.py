"""Is each collector OK? Answered from what the collector wrote, and nothing else.

The dashboard and the collector share no channel but the JSONL on disk, and this
keeps it that way: no process is probed, no socket opened, no pid looked up --
a collector on another cluster, read from a copied capture, is judged exactly
as one on this host. What the collector writes about itself is enough:

* `meta`, once per start: its pid, kernel, bcc, the probes that loaded, the ones
  that did not and why, the sysctls they depend on;
* `residency_totals`, every `interval_s` (60 s): the heartbeat;
* `stop`, once per clean exit: why it stopped, how long it ran, and how many
  events the kernel handed it against how many it lost (`perf_lost`).

So per host: the run in progress, every run in the window and how each ended,
and a verdict an admin can act on --

  ok        a heartbeat within `feeds.ebpf.max_age_s`
  degraded  running, but losing data it could have: a sysctl a probe depends on
            is switched off, or its last run lost more than `LOST_WARN_PCT` of
            the kernel's events
  stopped   no recent heartbeat, and a `stop` record after the last one: it was
            stopped (the reason is its own)
  silent    no recent heartbeat and no `stop` after it: killed, crashed, or the
            host is down -- a collector that exits cleanly always writes one
  unknown   files for the host, but no heartbeat in what was read

A probe the kernel cannot load (a missing BTF member, an old kernel) is a
LIMIT, listed beside the verdict but not degrading it: it is true of every run
on that host until the kernel changes, and a verdict that is always amber is
one nobody reads. A probe superseded by a better one is not listed at all.

A running collector's lost-event count is only written when it stops, so for
the run in progress it is "reported at stop", never 0.
"""
import time

LOST_WARN_PCT = 0.1
MAX_RUNS = 20
# What a probe that did not load leaves unmeasured, in the page's words. A
# feature not listed here is shown with the collector's own reason only.
IMPACT = {
    "tcp_accept": "inbound TCP connections (accept) are not seen",
    "dstate_task": "D-state totals come from the per-event counter instead",
    "dstate_stat": "per-process D-state (stall) time is not measured",
    "argv_user": "command lines are read from the kernel copy, capped",
    "argv_kernel": "command lines are not captured",
    "netbytes": "per-process network bytes are not measured",
    "netpeer_hdr": "network bytes by endpoint are not measured",
    "submit": "Slurm submissions are not recorded",
    "io": "per-process disk I/O is not measured",
    "delay": "scheduler, block-I/O and swap delays are not measured",
    "sched": "run-queue wait is not measured",
    "cgid": "cgroup (session scope) is not recorded",
    "tty": "terminals are not recorded",
}
# sysctls a probe needs, and what is lost without them
SYSCTL_NEEDS = {
    "kernel.sched_schedstats": ("1", "D-state (stall) time reads 0"),
}


def _superseded(why):
    w = (why or "").lower()
    return w.startswith("not needed") or w.startswith("superseded")


class _Run:
    __slots__ = ("pid", "started", "meta", "stopped", "stop", "last_beat")

    def __init__(self, pid):
        self.pid = pid
        self.started = None
        self.meta = None
        self.stopped = None
        self.stop = None
        self.last_beat = None


class _Host:
    __slots__ = ("runs", "last_beat", "beats", "interval_s", "beat_pid")

    def __init__(self):
        self.runs = {}            # collector_pid -> _Run
        self.last_beat = None
        self.beats = 0
        self.interval_s = None
        self.beat_pid = None


class CollectorHealth:
    """Fed every `meta`, `stop` and `residency_totals` record, in any order."""

    EVENTS = ("meta", "stop", "residency_totals")

    def __init__(self, cfg):
        self.cfg = cfg
        self.hosts = {}

    def _run(self, h, rec):
        pid = rec.get("collector_pid")
        r = h.runs.get(pid)
        if r is None:
            r = h.runs[pid] = _Run(pid)
            if len(h.runs) > MAX_RUNS:          # keep the newest runs
                oldest = min(h.runs.values(), key=lambda x: x.started or x.last_beat or 0)
                del h.runs[oldest.pid]
        return r

    def feed(self, rec):
        ev = rec.get("event")
        host = rec.get("host")
        ts = rec.get("_ts_epoch") or rec.get("ts_epoch")
        if host is None or ev not in self.EVENTS:
            return
        h = self.hosts.setdefault(host, _Host())
        r = self._run(h, rec)
        if ev == "meta":
            if r.started is None or (ts is not None and ts < r.started):
                r.started, r.meta = ts, rec
        elif ev == "stop":
            r.stopped, r.stop = ts, rec
        else:
            h.beats += 1
            if ts is not None and (r.last_beat is None or ts > r.last_beat):
                r.last_beat = ts
            if ts is not None and (h.last_beat is None or ts > h.last_beat):
                h.last_beat, h.interval_s, h.beat_pid = ts, rec.get("interval_s"), r.pid

    # ---------------------------------------------------------------- report
    @staticmethod
    def _run_row(r):
        s = r.stop or {}
        lost, seen = s.get("perf_lost"), s.get("perf_seen")
        n_lost = sum(lost.values()) if isinstance(lost, dict) else None
        n_seen = sum(seen.values()) if isinstance(seen, dict) else None
        m = r.meta or {}
        return {
            "pid": r.pid, "started": r.started, "stopped": r.stopped,
            "last_beat": r.last_beat,
            "ran_s": s.get("uptime_s"),
            "stop_reason": s.get("reason"),
            "records": s.get("records"), "truncated": s.get("truncated"),
            "lost": n_lost, "seen": n_seen,
            "lost_pct": (round(100.0 * n_lost / n_seen, 4) if n_lost is not None and n_seen else None),
            "lost_by_buffer": lost if isinstance(lost, dict) else None,
            "kernel": m.get("kernel"), "bcc": m.get("bcc"),
            "schema_version": m.get("schema_version") or s.get("schema_version"),
        }

    def report(self, files_by_host=None, now=None):
        """One entry per host, worst first."""
        now = now or time.time()
        max_age = float(self.cfg.get("feeds.ebpf.max_age_s", 300) or 300)
        files_by_host = files_by_host or {}
        out = []
        for host in sorted(set(self.hosts) | set(files_by_host)):
            h = self.hosts.get(host) or _Host()
            runs = sorted(h.runs.values(), key=lambda r: r.started or r.last_beat or 0)
            rows = [self._run_row(r) for r in runs]
            cur = next((r for r in reversed(runs) if r.meta is not None), runs[-1] if runs else None)
            age = (now - h.last_beat) if h.last_beat is not None else None
            reasons, verdict = [], "ok"

            if age is None:
                verdict = "unknown"
                reasons.append("no heartbeat (residency_totals) in the files read")
            elif age > max_age:
                last = next((r for r in runs if r.pid == h.beat_pid), None)
                if last is not None and last.stopped is not None and last.stopped >= h.last_beat - 1:
                    verdict = "stopped"
                    reasons.append("stopped %s (%s)" % (
                        time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(last.stopped)),
                        (last.stop or {}).get("reason") or "no reason given"))
                else:
                    verdict = "silent"
                    reasons.append("no heartbeat for %s and no stop record: killed, crashed, "
                                   "or the host is down" % _age(age))

            unavailable, sysctl_off = [], []
            if cur is not None and cur.meta is not None:
                m = cur.meta
                for feat, why in sorted((m.get("features_rejected") or {}).items()):
                    if not _superseded(why):
                        unavailable.append({"feature": feat, "why": why,
                                            "impact": IMPACT.get(feat)})
                sys_ = m.get("sysctl") or {}
                for k, (want, impact) in SYSCTL_NEEDS.items():
                    if k in sys_ and str(sys_.get(k)) != want:
                        sysctl_off.append({"sysctl": k, "value": sys_.get(k), "want": want,
                                           "impact": impact})
            last_done = next((r for r in reversed(rows) if r["stopped"] is not None), None)
            limits = ["%s did not load: %s" % (u["feature"], u["impact"] or u["why"])
                      for u in unavailable]
            if verdict == "ok":
                if sysctl_off:
                    verdict = "degraded"
                    reasons += ["%s=%s: %s" % (x["sysctl"], x["value"], x["impact"])
                                for x in sysctl_off]
                if last_done and last_done["lost_pct"] is not None \
                        and last_done["lost_pct"] > LOST_WARN_PCT:
                    verdict = "degraded"
                    reasons.append("its last run lost %.2f%% of kernel events"
                                   % last_done["lost_pct"])
            f = files_by_host.get(host) or {}
            out.append({
                "host": host, "verdict": verdict, "reasons": reasons, "limits": limits,
                "heartbeat": {"last": h.last_beat, "age_s": round(age, 1) if age is not None else None,
                              "interval_s": h.interval_s, "seen": h.beats, "max_age_s": max_age},
                "running": (self._run_row(cur) if cur is not None and verdict in
                            ("ok", "degraded") else None),
                "features": sorted((cur.meta or {}).get("features") or []) if cur else [],
                "unavailable": unavailable, "sysctl_off": sysctl_off,
                "sysctl": (cur.meta or {}).get("sysctl") if cur else None,
                "euid": (cur.meta or {}).get("euid") if cur else None,
                "runs": rows[::-1],
                "restarts": max(0, sum(1 for r in rows if r["started"] is not None) - 1),
                "files": f,
            })
        rank = {"silent": 0, "stopped": 1, "unknown": 2, "degraded": 3, "ok": 4}
        out.sort(key=lambda x: (rank.get(x["verdict"], 9), x["host"]))
        return out


def _age(s):
    s = int(s)
    if s < 120:
        return "%d s" % s
    if s < 7200:
        return "%d min" % (s // 60)
    if s < 172800:
        return "%.1f h" % (s / 3600.0)
    return "%.1f d" % (s / 86400.0)
