"""Requirement 5: what a process actually costs, and where it waits.

Scope is LOGIN NODES -- the eBPF tier runs there -- so this is login-node
resource use, not cluster resource use, and the payload says so rather than
leaving a reader to assume.

CPU is `sum(cpu_s)`, self, once per process.  `child_cpu_s` is the kernel's
cumulative reaped-descendant CPU and is therefore re-counted at every ancestor
level; analyze/common.py:364 measures the naive `cpu_s + child_cpu_s` sum
inflating login CPU 1.83x pooled and UNEVENLY by class (agent 1.57x, human
2.03x, human-vscode 2.38x), which would invert an agent-vs-human comparison.
`schedstat_ratio` (cpu_s / sched_run_s, median) is emitted so any future
regression in that basis is visible on the dashboard itself.

`series` puts the same quantities on a time axis, in fixed-width wall-clock
bins sized to the window (`bin_for`). Two kinds, kept apart because they mean
different things:

* **By exit time** -- events, CPU, runqueue wait, D-state, major faults. A process's
  whole-life counters land in the bin it exited in, so a six-hour agent drops
  six hours of CPU into one bin. Exact for the short tool calls that are nearly
  every record, lumpy for long-lived roots, and the payload says so.
* **Sampled** -- resident memory, live processes and D-state processes per
  class, from `residency_totals`, which every host emits each residency tick.
  This is state at a moment, not a sum at exit, so it is averaged over a host's
  ticks in a bin and then added across hosts.
"""
import collections
import time

from . import INSTANCE_CAP, Acc, Quant, add_counts, pct, top_by, who

FAULT_FIELDS = ("min_flt", "maj_flt", "cmaj_flt", "nvcsw", "nivcsw")
DELAY_FIELDS = ("blkio_wait_s", "swapin_wait_s", "freepages_wait_s")
NFS_IO_FIELDS = ("ops_s", "rd_kb_s", "rd_rtt_ms", "rd_exe_ms", "rd_retrans",
                 "wr_kb_s", "wr_rtt_ms", "wr_exe_ms", "wr_retrans")

# Per-bin sums by exit time. Each is nullable, so each keeps its own present
# count: a bin whose records all lacked schedstat is null, not 0 s of wait.
SERIES_FIELDS = ("cpu_s", "sched_wait_s", "dstate_wait_s", "maj_flt")
# The same, from the record's `io` block: bytes through read()/write() and bytes
# that reached the disk. Null for another user's process (/proc/<pid>/io is
# theirs), so a bin can have calls and no I/O measured.
IO_SERIES_FIELDS = ("rchar_mb", "rd_mb", "wchar_mb", "wr_mb")
# Network bytes sent and received, from the record's own TCP accounting, in MB
# (Live's picker: "every time series on Live", roadmap note 23). Null where the
# collector kept none, like the rest.
NET_SERIES_FIELDS = (("net_tx_mb", "net_tx_bytes"), ("net_rx_mb", "net_rx_bytes"))
# Per-class state from a residency tick, `residency_totals.by_actor3`.
TICK_FIELDS = ("rss_mb", "n_procs", "d_state")
# Round wall-clock widths; the first that keeps a window under SERIES_MAX_BINS
# wins, so 1 h is per minute, 24 h per 10 min and 7 d per hour -- a few hundred
# points at any window rather than ten thousand at the widest.
BIN_STEPS_S = (60, 120, 300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200, 86400)
SERIES_MAX_BINS = 240
# Tools drawn as their own line in the stall series; the rest are one line.
STALL_SERIES_TOOLS = 6
# A record stamped further ahead of the clock than this is not binned: one host
# with a wild clock would otherwise stretch the time axis to its future.
SERIES_SKEW_S = 300
# The axis is the bins held, which is the window plus its drift. A clock far
# behind can still reach back further, so past this many bins only the newest
# are drawn and the rest are counted.
SERIES_SPAN_CAP = 4 * SERIES_MAX_BINS


def bin_for(window_s):
    return next((b for b in BIN_STEPS_S if window_s / b <= SERIES_MAX_BINS),
                BIN_STEPS_S[-1])


class ResourceReducer:
    EVENTS = ("exit", "truncated", "residency_totals")
    KEY = "resources"

    def __init__(self, cfg, classes, window_s=86400):
        self.cfg = cfg
        self.classes = list(classes)
        self.bin_s = bin_for(window_s)
        # bin -> class -> {"n", "sum": {field: total}, "has": {field: present}}
        self.bins = {}
        # bin -> tool -> D-state seconds; only stalled records, so it stays sparse
        self.stall_bins = {}
        # bin -> host -> {"ticks": k, "cls": {class -> {field: sum over ticks}}}
        self.tick_bins = {}
        self.series_skipped = {"no_ts": 0, "future": 0}
        # waits longer than the process lived, set to None by normalize
        self.impossible = collections.defaultdict(collections.Counter)
        self.cls = collections.defaultdict(lambda: {
            "events": 0,
            "cpu_s": 0.0, "sched_run": Acc(), "sched_wait": Acc(),
            "q_cpu": Quant(), "q_rss": Quant(), "q_wait": Quant(), "q_dur": Quant(),
            "q_oncpu": Quant(),
            # the process behind each distribution's max (roadmap note 27):
            # metric -> [value, who]
            "top": {},
            # processes whose run time is longer than their lifetime as
            # measured, left out of the on-CPU share and counted (note 28)
            "oncpu_aside": 0,
            "faults": {k: Acc() for k in FAULT_FIELDS},
            "delays": {k: Acc() for k in DELAY_FIELDS},
            "dstate": {"events": 0, "wait": Acc(), "episodes": Acc(), "max_s": 0.0,
                       "src": collections.Counter(), "n_with": 0},
            "ratio": [],
        })
        self.stall_by_tool = collections.defaultdict(
            lambda: {"n": 0, "wait_s": 0.0, "episodes": 0, "max_s": 0.0})
        # "which tool stalls" does not tell you whose process to go look at, so
        # keep the worst individual offenders on each axis as well.
        self.worst_stall = []      # by dstate_wait_s
        self.worst_fault = []      # by maj_flt (the ones that actually hit disk)
        self.worst_wait = []       # by sched_wait_s (runqueue starvation)

    def _bin(self, rec):
        """The bin a record lands in, or None when it has no usable time."""
        e = rec.get("_ts_epoch")
        if e is None:
            self.series_skipped["no_ts"] += 1
            return None
        if e > time.time() + SERIES_SKEW_S:
            self.series_skipped["future"] += 1
            return None
        return int(e // self.bin_s)

    def _tick(self, rec):
        b = self._bin(rec)
        if b is None:
            return
        h = self.tick_bins.setdefault(b, {}).setdefault(
            rec.get("host") or "?", {"ticks": 0, "cls": {}})
        h["ticks"] += 1
        # `_by_class`: the tick's totals under this dashboard's classes (normalize)
        for cls, d in (rec.get("_by_class") or rec.get("by_actor3") or {}).items():
            slot = h["cls"].setdefault(cls, {})
            for k in TICK_FIELDS:
                v = (d or {}).get(k)
                if isinstance(v, (int, float)):
                    slot[k] = slot.get(k, 0) + v

    def _series_add(self, rec):
        b = self._bin(rec)
        if b is None:
            return
        slot = self.bins.setdefault(b, {}).setdefault(
            rec["_a3"], {"n": 0, "sum": {}, "has": {}})
        slot["n"] += 1
        io = rec.get("io") or {}
        for k, v in [(k, rec.get(k)) for k in SERIES_FIELDS] + \
                [(k, io.get(k)) for k in IO_SERIES_FIELDS] + \
                [(k, rec[f] / 1e6 if isinstance(rec.get(f), (int, float)) else None)
                 for k, f in NET_SERIES_FIELDS]:
            if v is None:
                continue
            slot["sum"][k] = slot["sum"].get(k, 0) + v
            slot["has"][k] = slot["has"].get(k, 0) + 1
        dw = rec.get("dstate_wait_s")
        if dw:
            tool = rec.get("_eff") or rec.get("_command") or "(empty)"
            s = self.stall_bins.setdefault(b, {})
            s[tool] = s.get(tool, 0) + dw

    def feed(self, rec):
        if rec.get("event") == "residency_totals":
            self._tick(rec)
            return
        self._series_add(rec)
        for k in rec.get("_impossible") or ():
            self.impossible[rec["_a3"]][k] += 1
        c = self.cls[rec["_a3"]]
        c["events"] += 1
        cpu = rec.get("cpu_s")
        dur = rec.get("duration_s")
        run = rec.get("sched_run_s")
        if cpu is not None:
            c["cpu_s"] += cpu
        c["sched_run"].add(run)
        c["sched_wait"].add(rec.get("sched_wait_s"))
        c["q_cpu"].add(cpu)
        c["q_rss"].add(rec.get("peak_rss_mb") if rec.get("peak_rss_mb") is not None
                       else rec.get("rss_mb"))
        rss = rec.get("peak_rss_mb") if rec.get("peak_rss_mb") is not None else rec.get("rss_mb")
        c["q_wait"].add(rec.get("sched_wait_s"))
        c["q_dur"].add(dur)
        top = c["top"]
        for metric, v in (("cpu_s", cpu), ("peak_rss_mb", rss),
                          ("sched_wait_s", rec.get("sched_wait_s")), ("duration_s", dur)):
            if v is not None and (metric not in top or v > top[metric][0]):
                top[metric] = [v, who(rec)]
        # The on-CPU share is run time over lifetime. A lifetime measured from
        # exec (`duration_src` exec) leaves out the work a process did before
        # it exec'd -- the shell code of a fork, before the command it runs --
        # which its run time counts. On rtx6000a one exit in five ran longer
        # than it "lived" that way; capped at 100% they read as busy CPUs and
        # lifted the median. They are left out, and counted, instead.
        if dur and dur > 0 and run is not None:
            if run > dur * 1.05 + 0.001:
                c["oncpu_aside"] += 1
            else:
                c["q_oncpu"].add(min(1.0, run / dur))
        if cpu and cpu > 1.0 and run:
            c["ratio"].append(cpu / run)
        for k in FAULT_FIELDS:
            c["faults"][k].add(rec.get(k))
        mf = rec.get("maj_flt")
        if mf:
            w = who(rec)
            for k in FAULT_FIELDS:
                w[k] = rec.get(k)
            self.worst_fault.append(w)
            top_by(self.worst_fault, lambda x: -(x.get("maj_flt") or 0))
        sw = rec.get("sched_wait_s")
        if sw:
            w = who(rec)
            w["sched_wait_s"] = sw
            w["sched_run_s"] = rec.get("sched_run_s")
            self.worst_wait.append(w)
            top_by(self.worst_wait, lambda x: -(x.get("sched_wait_s") or 0))
        for k in DELAY_FIELDS:
            c["delays"][k].add(rec.get(k))

        d = c["dstate"]
        dw = rec.get("dstate_wait_s")
        d["wait"].add(dw)
        d["episodes"].add(rec.get("dstate_episodes"))
        if dw is not None:
            d["n_with"] += 1
            if dw > 0:
                d["events"] += 1
                tool = rec.get("_eff") or rec.get("_command") or "(empty)"
                s = self.stall_by_tool[tool]
                s["n"] += 1
                s["wait_s"] += dw
                s["episodes"] += rec.get("dstate_episodes") or 0
                s["max_s"] = max(s["max_s"], rec.get("dstate_max_s") or 0.0)
                w = who(rec)
                w["dstate_wait_s"] = dw
                w["dstate_episodes"] = rec.get("dstate_episodes")
                w["dstate_max_s"] = rec.get("dstate_max_s")
                w["dstate_src"] = rec.get("dstate_src")
                self.worst_stall.append(w)
                top_by(self.worst_stall, lambda x: -(x.get("dstate_wait_s") or 0))
        if rec.get("dstate_max_s") is not None:
            d["max_s"] = max(d["max_s"], rec["dstate_max_s"])
        if rec.get("dstate_src"):
            d["src"][rec["dstate_src"]] += 1

    # ----------------------------------------------------------------- merge
    def merge(self, o):
        """Fold a LATER state into this one. `o` is consumed.

        An hour state bins at a finer width than any window (`hours`), and
        every width here is a whole number of minutes on the epoch, so each of
        its bins falls inside exactly one of this state's: the coarser series
        is the one a single pass at this width would have binned. A bin no
        record reached is still absent, so a gap stays a gap.
        """
        if self.bin_s % o.bin_s:
            raise ValueError("cannot merge %d s bins into %d s bins" % (o.bin_s, self.bin_s))
        f = self.bin_s // o.bin_s
        for b, row in o.bins.items():
            dst = self.bins.setdefault(b // f, {})
            for cls, slot in row.items():
                d = dst.get(cls)
                if d is None:
                    dst[cls] = slot
                    continue
                d["n"] += slot["n"]
                add_counts(d["sum"], slot["sum"])
                add_counts(d["has"], slot["has"])
        for b, tools in o.stall_bins.items():
            add_counts(self.stall_bins.setdefault(b // f, {}), tools)
        for b, hosts in o.tick_bins.items():
            dst = self.tick_bins.setdefault(b // f, {})
            for host, h in hosts.items():
                d = dst.get(host)
                if d is None:
                    dst[host] = h
                    continue
                d["ticks"] += h["ticks"]
                for cls, fields in h["cls"].items():
                    add_counts(d["cls"].setdefault(cls, {}), fields)
        add_counts(self.series_skipped, o.series_skipped)
        for cls, c in o.impossible.items():
            add_counts(self.impossible[cls], c)
        for cls, c in o.cls.items():
            m = self.cls[cls]
            m["events"] += c["events"]
            m["cpu_s"] += c["cpu_s"]
            for k in ("sched_run", "sched_wait"):
                m[k].merge(c[k])
            for k in ("q_cpu", "q_rss", "q_wait", "q_dur", "q_oncpu"):
                m[k].merge(c[k])
            for metric, t in c.get("top", {}).items():
                if metric not in m["top"] or t[0] > m["top"][metric][0]:
                    m["top"][metric] = t
            m["oncpu_aside"] += c.get("oncpu_aside", 0)
            for grp in ("faults", "delays"):
                for k, a in c[grp].items():
                    m[grp][k].merge(a)
            d, od = m["dstate"], c["dstate"]
            d["events"] += od["events"]
            d["n_with"] += od["n_with"]
            d["wait"].merge(od["wait"])
            d["episodes"].merge(od["episodes"])
            d["max_s"] = max(d["max_s"], od["max_s"])
            add_counts(d["src"], od["src"])
            m["ratio"].extend(c["ratio"])
        for tool, v in o.stall_by_tool.items():
            m = self.stall_by_tool[tool]
            m["n"] += v["n"]
            m["wait_s"] += v["wait_s"]
            m["episodes"] += v["episodes"]
            m["max_s"] = max(m["max_s"], v["max_s"])
        # The heaviest processes: a later state's after this one's, re-cut by
        # `feed`'s own rule, so the served top is the one a single pass keeps.
        for attr, field in (("worst_stall", "dstate_wait_s"), ("worst_fault", "maj_flt"),
                            ("worst_wait", "sched_wait_s")):
            lst = getattr(self, attr)
            lst.extend(getattr(o, attr))
            top_by(lst, lambda x, f=field: -(x.get(f) or 0))

    def result(self):
        by_class = {}
        for cls, c in self.cls.items():
            ratios = sorted(c["ratio"])
            by_class[cls] = {
                "events": c["events"],
                "cpu_s": round(c["cpu_s"], 2),
                "sched_run_s": c["sched_run"].as_dict(),
                "sched_wait_s": c["sched_wait"].as_dict(),
                "schedstat_ratio": (round(ratios[len(ratios) // 2], 3) if ratios else None),
                "quants": {"cpu_s": c["q_cpu"].as_dict(),
                           "peak_rss_mb": c["q_rss"].as_dict(),
                           "sched_wait_s": c["q_wait"].as_dict(),
                           "duration_s": c["q_dur"].as_dict(),
                           "on_cpu_frac": c["q_oncpu"].as_dict()},
                # the process behind each max: which one, whose, and when
                "top": {m: dict(t[1], value=round(t[0], 4)) for m, t in c["top"].items()},
                "oncpu_set_aside": c["oncpu_aside"],
                "faults": {k: v.as_dict() for k, v in c["faults"].items()},
                "delays": {k: v.as_dict() for k, v in c["delays"].items()},
                "dstate": {"events_with_stall": c["dstate"]["events"],
                           "wait_s": c["dstate"]["wait"].as_dict(),
                           "episodes": c["dstate"]["episodes"].as_dict(),
                           "max_s": round(c["dstate"]["max_s"], 2),
                           "src": dict(c["dstate"]["src"]),
                           "coverage_pct": pct(c["dstate"]["n_with"], c["events"])},
            }
        stalls = sorted(self.stall_by_tool.items(), key=lambda kv: -kv[1]["wait_s"])[:20]
        series = self.series([k for k, _v in stalls[:STALL_SERIES_TOOLS]])
        self.worst_stall.sort(key=lambda x: -(x.get("dstate_wait_s") or 0))
        self.worst_fault.sort(key=lambda x: -(x.get("maj_flt") or 0))
        self.worst_wait.sort(key=lambda x: -(x.get("sched_wait_s") or 0))
        return {
            "by_class": by_class,
            "stall_by_tool": [dict(tool=k, wait_s=round(v["wait_s"], 2), n=v["n"],
                                   episodes=v["episodes"], max_s=round(v["max_s"], 2))
                              for k, v in stalls],
            # pid/user/actor for the worst individual process on each axis
            "worst_stall": self.worst_stall[:INSTANCE_CAP],
            "worst_fault": self.worst_fault[:INSTANCE_CAP],
            "worst_wait": self.worst_wait[:INSTANCE_CAP],
            "scope": "login nodes only -- the eBPF tier runs on login nodes, so this is "
                     "login-node resource use, not cluster resource use",
            "cpu_basis": "sum(cpu_s), self, once per process; child_cpu_s NEVER added",
            "series": series,
            "impossible_waits": {c: dict(v) for c, v in self.impossible.items()},
            "impossible_basis": "a wait longer than the process lived is not a "
                                "measurement (it is the kernel's `now - 0` for an "
                                "unstamped wait start, ~ the host's uptime); such "
                                "values are treated as not measured and counted here",
        }

    def series(self, stall_tools):
        """Every quantity above, per bin, oldest first, on one shared axis.

        A bin a class has no record in is null -- a gap, never a measured 0 --
        and so is a bin whose records all lacked the field. The stall lines
        are the one place a 0 is real: in a bin where D-state was measured, a
        tool with no stalled record stalled for 0 s.
        """
        held = set(self.bins) | set(self.tick_bins)
        out = {"bin_s": self.bin_s, "t": [], "by_class": {}, "stall_by_tool": {},
               "stall_other": [], "resident": {}, "resident_hosts": [],
               "skipped": dict(self.series_skipped, clipped_bins=0)}
        if not held:
            return out
        lo, hi = min(held), max(held)
        if hi - lo + 1 > SERIES_SPAN_CAP:
            out["skipped"]["clipped_bins"] = sum(1 for b in held if b <= hi - SERIES_SPAN_CAP)
            lo = hi - SERIES_SPAN_CAP + 1
        idx = range(lo, hi + 1)
        out["t"] = [b * self.bin_s for b in idx]

        exit_cls = sorted({c for b in idx for c in self.bins.get(b, ())})
        fields = SERIES_FIELDS + IO_SERIES_FIELDS + tuple(k for k, _f in NET_SERIES_FIELDS)
        for c in exit_cls:
            row = {"n": []}
            row.update((k, []) for k in fields)
            for b in idx:
                s = self.bins.get(b, {}).get(c)
                row["n"].append(s["n"] if s else None)
                for k in fields:
                    row[k].append(round(s["sum"][k], 3) if s and s["has"].get(k) else None)
            out["by_class"][c] = row

        measured = [any(s["has"].get("dstate_wait_s")
                        for s in self.bins.get(b, {}).values()) for b in idx]
        for tool in stall_tools:
            out["stall_by_tool"][tool] = [
                round(self.stall_bins.get(b, {}).get(tool, 0), 3) if m else None
                for b, m in zip(idx, measured)]
        named = set(stall_tools)
        out["stall_other"] = [
            round(sum(v for t, v in self.stall_bins.get(b, {}).items() if t not in named), 3)
            if m else None for b, m in zip(idx, measured)]

        # Per host, the mean over its ticks in the bin; then summed over hosts.
        # A class missing from a tick had no live process at that tick, which
        # the tick did measure, so it counts as 0 there; a bin no host ticked
        # in is null.
        tick_cls = sorted({c for b in idx for h in self.tick_bins.get(b, {}).values()
                           for c in h["cls"]})
        for c in tick_cls:
            out["resident"][c] = {k: [] for k in TICK_FIELDS}
        for b in idx:
            hosts = self.tick_bins.get(b)
            out["resident_hosts"].append(len(hosts) if hosts else None)
            for c in tick_cls:
                for k in TICK_FIELDS:
                    out["resident"][c][k].append(
                        round(sum(h["cls"].get(c, {}).get(k, 0) / h["ticks"]
                                  for h in hosts.values()), 2) if hosts else None)
        return out


class NodeReducer:
    """The unprivileged node tier: load, memory, filesystem, NFS, per-user cgroup.

    Field names are the real `collect/common/snapshot.py` schema (37 top-level
    keys), not a guess: load lives under `uptime`, memory under `memory.mem` in
    GB, the host under `hostname`, and an NFS mount names itself `mountpoint`.

    `per_user_cgroup.cpu_usage_usec` is CUMULATIVE since boot, so a per-user CPU
    RATE exists only as a delta between two consecutive snapshots.  One snapshot
    alone cannot produce it, and reporting the cumulative figure as a rate would
    overstate by however long the node has been up -- so `cpu_delta_s` is None on
    the first snapshot rather than wrong.
    """

    KEY = "node"

    def __init__(self, cfg, classes):
        self.cfg = cfg
        self.keep = int(cfg.get("feeds.ebpf_node.snapshots_per_host", 2) or 2)
        self.snaps = collections.defaultdict(list)

    def feed_snapshot(self, rec):
        h = rec.get("hostname") or rec.get("host") or "?"
        self.snaps[h].append(rec)
        self.snaps[h] = self.snaps[h][-self.keep:]

    @staticmethod
    def _gap(prev, cur):
        from datetime import datetime as _dt
        try:
            a = _dt.strptime((prev.get("timestamp") or "")[:19], "%Y-%m-%d %H:%M:%S")
            b = _dt.strptime((cur.get("timestamp") or "")[:19], "%Y-%m-%d %H:%M:%S")
            return (b - a).total_seconds()
        except Exception:
            return None

    def result(self):
        hosts, mounts, disks = [], [], []
        for h, snaps in sorted(self.snaps.items()):
            cur = snaps[-1]
            prev = snaps[-2] if len(snaps) > 1 else None
            up = cur.get("uptime") or {}
            mem = ((cur.get("memory") or {}).get("mem") or {})
            ps = cur.get("process_states") or {}
            pstat = cur.get("proc_stat") or {}
            resp = cur.get("responsiveness") or {}
            cpu_all = next((c for c in (cur.get("cpu") or []) if c.get("cpu") == "all"), {})
            total_gb = mem.get("total_gb")
            used_gb = mem.get("used_gb")
            row = {
                "host": h, "ts": cur.get("timestamp"),
                "load_1": up.get("load_1"), "load_5": up.get("load_5"),
                "load_15": up.get("load_15"),
                "mem_total_gb": total_gb, "mem_used_gb": used_gb,
                "mem_used_pct": (round(100.0 * used_gb / total_gb, 1)
                                 if total_gb and used_gb is not None else None),
                "mem_available_gb": mem.get("available_gb"),
                "cpu_idle_pct": cpu_all.get("idle"),
                "cpu_iowait_pct": cpu_all.get("iowait"),
                "procs_running": pstat.get("procs_running"),
                "procs_blocked": pstat.get("procs_blocked"),
                "btime": pstat.get("btime"),
                "d_state_procs": len(cur.get("d_state_procs") or []),
                "zombie": ps.get("zombie"), "blocked_io": ps.get("blocked_io"),
                "sleeping": ps.get("sleeping"), "running": ps.get("running"),
                "fork_exec_ms": resp.get("fork_exec_ms"),
                "stat_local_ms": resp.get("stat_local_ms"),
                "getent_ms": resp.get("getent_ms"),
                "logged_in_users": len(cur.get("logged_in_users") or []),
                "fds_allocated": (cur.get("system_fds") or {}).get("allocated"),
                "snapshots": len(snaps),
                "per_user": [],
            }
            dt = self._gap(prev, cur) if prev else None
            pu_prev = {r.get("user"): r for r in ((prev or {}).get("per_user_cgroup") or [])}
            for r in (cur.get("per_user_cgroup") or []):
                user = r.get("user")
                p = pu_prev.get(user)
                delta = thr = None
                reset = False
                if p and dt and dt > 0:
                    a, b = p.get("cpu_usage_usec"), r.get("cpu_usage_usec")
                    if a is not None and b is not None:
                        # A cumulative counter cannot decrease: a negative delta
                        # means the cgroup was recreated (the user's last session
                        # ended and a new one started) and the counter restarted
                        # from zero. The elapsed CPU is then unknowable from these
                        # two samples, so it stays null and is flagged -- clamping
                        # to 0 would read as "this user was idle", which is a
                        # different and false claim.
                        if b >= a:
                            delta = round((b - a) / 1e6, 3)
                        else:
                            reset = True
                    ta, tb = p.get("throttled_usec"), r.get("throttled_usec")
                    if ta is not None and tb is not None and tb >= ta:
                        thr = round((tb - ta) / 1e6, 3)
                quota = r.get("cpu_max_quota_usec")
                period = r.get("cpu_max_period_usec")
                row["per_user"].append({
                    "user": user, "uid": r.get("uid"),
                    "cpu_delta_s": delta, "gap_s": dt, "counter_reset": reset,
                    "cpu_quota_cores": (round(quota / period, 2)
                                        if quota and period else None),
                    "throttled_delta_s": thr,
                    "mem_gb": (round(r["mem_current_bytes"] / 1073741824.0, 3)
                               if r.get("mem_current_bytes") is not None else None),
                    "pids": r.get("pids_current"), "pids_max": r.get("pids_max"),
                })
            row["per_user"].sort(key=lambda x: -(x["cpu_delta_s"] or x["mem_gb"] or 0))
            row["per_user_resets"] = sum(1 for u in row["per_user"] if u["counter_reset"])
            hosts.append(row)
            # Two sources per mount. `nfs_io` (nfsiostat) has the interval rates
            # and lists only mounts with I/O in that interval; `nfs_mountstats`
            # has cumulative-since-mount counters and omits an op whose count is
            # 0. A mount absent from either keeps that source's fields null.
            io = {m.get("mountpoint"): m for m in (cur.get("nfs_io") or [])}
            ms = {m.get("mountpoint"): m for m in (cur.get("nfs_mountstats") or [])}
            for mp in list(ms) + [mp for mp in io if mp not in ms]:
                i, s = io.get(mp, {}), ms.get(mp, {})
                mounts.append({"host": h, "mount": mp,
                               "fstype": s.get("fstype"), "bad_xid": s.get("bad_xid"),
                               "backlog_u": s.get("backlog_u"),
                               "rd_ops_total": (s.get("read") or {}).get("ops"),
                               "wr_ops_total": (s.get("write") or {}).get("ops"),
                               **{k: i.get(k) for k in NFS_IO_FIELDS}})
            for d in (cur.get("disk_space") or []):
                disks.append({"host": h, **d})
        return {
            "hosts": hosts, "nfs": mounts, "disk": disks,
            "cpu_delta_basis": "per_user_cgroup.cpu_usage_usec is CUMULATIVE; cpu_delta_s "
                               "is the difference between the two newest snapshots over "
                               "their gap (gap_s). One snapshot alone cannot yield a rate, "
                               "so it is null rather than wrong. A negative delta means "
                               "the cgroup was recreated and the counter reset; that is "
                               "reported as counter_reset with a null rate, never clamped "
                               "to 0 (which would read as 'idle').",
            "privilege": "cgroup v2 stats are readable by the slice owner and root; other "
                         "users' slices may be restricted when running unprivileged, so "
                         "per_user can under-count users.",
        }
