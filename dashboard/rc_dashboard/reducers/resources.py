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
"""
import collections

from . import INSTANCE_CAP, Acc, Quant, Reducer, pct, top_by, who

FAULT_FIELDS = ("min_flt", "maj_flt", "cmaj_flt", "nvcsw", "nivcsw")
DELAY_FIELDS = ("blkio_wait_s", "swapin_wait_s", "freepages_wait_s")


class ResourceReducer:
    EVENTS = ("exit", "truncated")
    KEY = "resources"

    def __init__(self, cfg, classes):
        self.cfg = cfg
        self.classes = list(classes)
        self.cls = collections.defaultdict(lambda: {
            "events": 0,
            "cpu_s": 0.0, "sched_run": Acc(), "sched_wait": Acc(),
            "q_cpu": Quant(), "q_rss": Quant(), "q_wait": Quant(), "q_dur": Quant(),
            "q_oncpu": Quant(),
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

    def feed(self, rec):
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
        c["q_wait"].add(rec.get("sched_wait_s"))
        c["q_dur"].add(dur)
        if dur and dur > 0 and run is not None:
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
                tool = rec.get("_eff") or rec.get("comm") or "(empty)"
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
        }


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
            for m in (cur.get("nfs_mountstats") or []):
                mounts.append({"host": h, "mount": m.get("mountpoint"),
                               "fstype": m.get("fstype"), "bad_xid": m.get("bad_xid"),
                               "backlog_u": m.get("backlog_u"),
                               "rd_rtt_ms": m.get("rd_rtt_ms"),
                               "wr_rtt_ms": m.get("wr_rtt_ms"),
                               "ops_s": m.get("ops_s")})
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
