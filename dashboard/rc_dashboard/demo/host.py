"""What a login node's collector writes about the node and about itself.

* `meta` when a collector run starts, `stop` when it ends: each host's
  collector restarts once a night (as a nightly package update would), so the
  Feeds tab has runs to list and a `meta` always falls inside the window the
  service reads back. A demo's restart writes no `truncated` records: those would
  put every live process's lifetime CPU into one minute, which is true of a real
  restart and only noise in a demo.
* `residency_totals`, the heartbeat, every minute: what was running, by class
  and agent type, counted from the sessions' own census (world.census), so the
  Live tab's plates agree with its session list.
* the node tier's snapshot every five minutes (load, memory, NFS, per-user
  cgroups), into its own root as `ebpfm.sh _node_tick` writes it.

Ticks land at a fixed second of each minute, per host, and the sessions' own
residency/conn/netio records use the same ticks (`ticks`), a little before the
heartbeat, as the collector writes them.
"""
import hashlib
import time

from .records import envelope, line, stamp

TICK_S = 60
NODE_S = 300
KERNELS = (
    ("4.18.0-553.22.1.el8_10.x86_64", "0.29.1",
     {"tcp_accept": "error: no BTF for struct sock on this kernel (CONFIG_DEBUG_INFO_BTF=n)"}),
    ("5.14.0-503.26.1.el9_5.x86_64", "0.30.0", {}),
)
FEATURES = ["argv_user", "cgid", "delay", "dstate_stat", "fork_raw", "io", "netbytes",
            "netbytes_page", "netbytes_udp6", "netpeer_btf", "pids_signal", "sched", "signal",
            "submit", "tcp_accept", "tcp_btf", "thread_acc", "tty"]
SUPERSEDED = {"argv_kernel": "superseded by argv_user", "pids_task": "superseded by pids_signal",
              "pids_kread": "superseded by pids_signal", "tcp_hdr": "superseded by tcp_btf",
              "tcp_basic": "superseded by tcp_btf", "netpeer_hdr": "superseded by netpeer_btf",
              "dstate_task": "not needed: dstate_stat loaded"}


# The cores a session keeps busy on average, by kind: what a user's cgroup
# counter grows by while one runs.
CORES = {"claude_terminal": 0.12, "claude_nested": 0.15, "claude_babysit": 0.03,
         "claude_nightly": 0.12, "claude_desktop": 0.1, "claude_rc": 0.08, "claude_print": 0.2,
         "codex_tui": 0.1, "codex_exec": 0.2, "codex_app": 0.08, "vscode": 0.06,
         "vscode_claude": 0.12, "vscode_parked": 0.02, "human_shell": 0.04}


def day_of_local(epoch):
    return time.strftime("%Y-%m-%d", time.localtime(epoch))


def _h(*parts):
    return int(hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:12], 16)


class HostFeed:
    def __init__(self, world, host, index):
        self.world, self.host, self.index = world, host, index
        self.seed = world.seed
        self.tick_off = 3.0 + (11.0 * index) % 50
        self.node_off = 17.0 + (37.0 * index) % 240
        self.boot = float(int(world.start) - 86400 * (11 + 4 * index) - 3721 * (index + 1))
        b = "%032x" % _h(self.seed, host, "boot")
        self.boot_id = "%s-%s-%s-%s-%s" % (b[:8], b[8:12], b[12:16], b[16:20], b[20:32])
        self.kernel, self.bcc, self.rejected = KERNELS[index % len(KERNELS)]
        self.ip = "10.31.%d.%d" % (4 + index, 17 + index * 3)
        self._cg_days, self._cg_done = {}, {}

    # ------------------------------------------------------------- runs
    def restart_on(self, day):
        """When this host's collector restarts on local `day`, or None."""
        y, m, d = (int(x) for x in day.split("-"))
        h = _h(self.seed, self.host, day, "restart")
        t = time.mktime((y, m, d, 4, 7 + h % 40, h % 60, 0, 0, -1))
        return float(t) if t > self.world.start + 600 else None

    def run_start(self, t):
        """The start of the collector run in progress at `t`."""
        from .world import day_of, midnight
        day = day_of(t)
        for back in range(0, 3):
            dd = day_of(midnight(day) - 86400 * back + 43200)
            r = self.restart_on(dd)
            if r is not None and r + 2.6 <= t:
                return r + 2.6
        return self.world.start

    def collector_pid(self, t):
        return 1200 + _h(self.seed, self.host, "pid", round(self.run_start(t), 1)) % 60000

    def ticks(self, t0, t1):
        """The heartbeat times in [t0, t1): a minute after a run starts, then every minute."""
        out = []
        m = int((t0 - self.tick_off) // TICK_S) * TICK_S + self.tick_off
        while m < t1:
            if m >= t0 and m >= self.run_start(m) + 50:
                out.append(m)
            m += TICK_S
        return out

    # ---------------------------------------------------------- records
    def between(self, t0, t1):
        from .world import days_between
        items = []
        key = "~%s" % self.host                 # after the sessions' records at one instant
        starts = []
        if t0 <= self.world.start < t1:
            starts.append(self.world.start)
        for day in days_between(t0 - 10, t1):
            r = self.restart_on(day)
            if r is not None:
                if t0 <= r < t1:
                    items.append(self._rec(r, key + "|0", self.stop(r)))
                if t0 <= r + 2.6 < t1:
                    starts.append(r + 2.6)
        for s in starts:
            items.append(self._rec(s, key + "|1", self.meta(s)))
        for t in self.ticks(t0 - 0.9, t1 - 0.9):
            # written last in its round, after the sessions' own tick records
            items.append(self._rec(t + 0.9, key + "|2", self.totals(t + 0.9)))
        n = int((t0 - self.node_off) // NODE_S) * NODE_S + self.node_off
        while n < t1:
            if n >= t0 and n >= self.world.start:
                rec = self.node(n)
                items.append((n, key + "|3", "node", self.host, None, line(rec)))
            n += NODE_S
        return items

    def _rec(self, t, order, rec):
        return (t, order, "ebpf", self.host, "snapshot", line(rec))

    def meta(self, t):
        rec = envelope("meta", t, self.host, self.collector_pid(t))
        rej = dict(SUPERSEDED)
        rej.update(self.rejected)
        feats = [f for f in FEATURES if f not in self.rejected]
        rec.update({
            "kernel": self.kernel, "bcc": self.bcc, "euid": 0,
            "tz": "%s%s" % (time.strftime("%Z", time.localtime(t)), time.strftime("%z", time.localtime(t))),
            "boot_id": self.boot_id, "features": feats, "features_rejected": rej,
            "cpu_scope": "process", "counters_scope": "process", "redact": ["keys", "prompts"],
            "argv_source_default": "kernel", "dstate_src_default": "stat", "btf_offs": None,
            "sysctl": {"kernel.sched_schedstats": "1", "kernel.task_delayacct": "1"},
            "cgroup": "/system.slice/ebpfm.service", "netlink_sockdiag": True, "unsafe_dirs": None,
            "config": {"residency_s": 60.0, "exit_hold_ms": 250, "reorder_ms": 20, "netio": True,
                       "flush_mode": "poll", "submit_comms": ["salloc", "sbatch", "srun"]},
            "seeded_pids": 900 + _h(self.host, t) % 1400,
        })
        return rec

    def stop(self, t):
        start = self.run_start(t - 1)
        up = t - start
        seen = int(up * (310 + 40 * self.index))
        rec = envelope("stop", t, self.host, self.collector_pid(t - 1))
        rec.update({
            "uptime_s": round(up, 1), "records": int(up * 9.3), "truncated": 0,
            "perf_lost": {"events": seen // 400000, "argv_events": 0, "write_events": 0,
                          "tcp_events": 0, "netp_new": 0},
            "perf_seen": {"events": seen, "argv_events": seen // 3, "write_events": seen // 900,
                          "tcp_events": seen // 40, "netp_new": seen // 100},
            "handler_errors": None, "proc_vanished": int(up / 7200), "proc_late_exits": 0,
            "flush_mode": "poll", "flushes": int(up * 2), "conns_dropped": 0,
            "conns_dropped_commands": None, "netio_records": int(up * 0.6),
            "netpeer_full": {"calls": 0, "tx_bytes": 0, "rx_bytes": 0},
            "netpeer_orphans": {"entries": int(up / 60), "tx_bytes": int(up * 3),
                                "rx_bytes": int(up * 17), "calls": int(up / 9)},
            "argv_hosts": {"resolved": int(up / 70), "failed": 0, "dropped": 0},
            "reason": "signal",
        })
        return rec

    def totals(self, t):
        """The heartbeat: what the sessions had running in this minute."""
        cen = self.world.census(self.host, int(t // 60) * 60)
        by_cls, by_type, trees = {}, {}, set()
        users_cls, users_type = {}, {}
        for cls, atype, user, n, thr, rss, ds in cen:
            c = by_cls.setdefault(cls, {"n_procs": 0, "threads": 0, "rss_mb": 0.0, "d_state": 0})
            c["n_procs"] += n
            c["threads"] += thr
            c["rss_mb"] += rss
            c["d_state"] += ds
            users_cls.setdefault(cls, set()).add(user)
            if atype:
                a = by_type.setdefault(atype, {"trees": 0, "n_procs": 0, "threads": 0, "rss_mb": 0.0,
                                               "cpu_s": 0.0, "d_state": 0, "autonomous": 0})
                a["n_procs"] += n
                a["threads"] += thr
                a["rss_mb"] += rss
                a["d_state"] += ds
                a["cpu_s"] += round(rss * 0.05, 2)
                users_type.setdefault(atype, set()).add(user)
                trees.add((atype, user))
        for cls, c in by_cls.items():
            c["rss_mb"] = round(c["rss_mb"], 1)
            c["users"] = len(users_cls[cls])
            c["states"] = {"S": max(0, c["n_procs"] - 1), "R": min(1, c["n_procs"])}
        for at, a in by_type.items():
            a["rss_mb"] = round(a["rss_mb"], 1)
            a["cpu_s"] = round(a["cpu_s"], 2)
            a["users"] = len(users_type[at])
            a["trees"] = sum(1 for x in trees if x[0] == at)
        live = sum(c["n_procs"] for c in by_cls.values())
        rec = envelope("residency_totals", t, self.host, self.collector_pid(t))
        up = t - self.boot
        rs = self.run_start(t)
        seen = int((t - rs) * (310 + 40 * self.index))
        rec.update({
            "interval_s": 60.0, "tracked_live": 640 + live + _h(self.host, int(t // 60)) % 90,
            "trees": len(trees), "conn_records": sum(1 for x in cen if x[0] == "agent"),
            "by_actor3": by_cls, "by_agent_type": by_type,
            "dropped": {"n": 20 + _h(self.host, t) % 30,
                        "by_command": {"sshd": 9, "unix_chkpwd": 4, "sadc": 3}},
            "tcp_open_ext_by_actor3": {cls: {"open": c["n_procs"] // 3 + 1, "ext": c["n_procs"] // 5,
                                             "estab": c["n_procs"] // 3 + 1}
                                       for cls, c in by_cls.items()},
            "conn_unattributed": 0, "conn_unmatched_births": 0,
            "fork_total": int(up * 13.7), "fork_rate": round(9.0 + live * 0.35, 1),
            "d_stack_top": None, "d_stacks_read": 0,
            "netio_records": len(trees), "netpeer_entries": 300 + live * 4,
            "netpeer_full": {"calls": 0, "tx_bytes": 0, "rx_bytes": 0},
            "netpeer_orphans": {"entries": 12, "tx_bytes": 4100, "rx_bytes": 22000, "calls": 19},
            "netpeer_kernel": {"calls": int(up * 2.1), "tx_bytes": int(up * 130),
                               "rx_bytes": int(up * 16000)},
            "perf_seen": {"events": seen, "argv_events": seen // 3, "write_events": seen // 900,
                          "tcp_events": seen // 40, "netp_new": seen // 100},
            "perf_lost": {"events": 0, "argv_events": 0, "write_events": 0, "tcp_events": 0,
                          "netp_new": 0},
            "handler_errors": None, "proc_entries": 700 + live, "proc_vanished": 0,
            "proc_late_exits": 0,
            "thread_acc": {"taken": int((t - rs) * 4), "late": 0, "missing": 0, "stale": 0,
                           "swept": 0, "full": 0},
        })
        return rec

    def cgroup_cpu_usec(self, user, t):
        """A user's cgroup CPU counter at `t`: CUMULATIVE, so it only ever grows, by
        what their sessions ran. The node reducer takes the difference of two
        snapshots as the CPU used between them, so the counter is an integral of
        activity -- a background trickle since boot, plus each session's cores for
        as long as it has run -- and a session starting or ending changes its slope,
        never its value."""
        from .world import MAX_SESSION_S, days_between
        key = (user, day_of_local(t))
        done = self._cg_done.get(key)
        if done is None:
            # every session planned before yesterday has ended by today (none runs
            # past MAX_SESSION_S), so those days count whole, once
            done = 0.0
            for day in days_between(self.world.start, t - 2 * 86400):
                if day < day_of_local(t - 86400):
                    done += self._cg_day(user, day)
            self._cg_done[key] = done
        live = 0.0
        for day in days_between(max(self.world.start, t - MAX_SESSION_S - 86400), t + 1):
            if day >= day_of_local(t - 86400):
                for s in self.world.plan(self.host, day):
                    if s.person["user"] == user and s.start < t:
                        live += CORES.get(s.kind, 0.1) * (min(t, s.end) - s.start)
        return int(((t - self.boot) * 0.003 + done + live) * 1e6)

    def _cg_day(self, user, day):
        key = (user, day)
        v = self._cg_days.get(key)
        if v is None:
            v = self._cg_days[key] = sum(CORES.get(s.kind, 0.1) * (s.end - s.start)
                                         for s in self.world.plan(self.host, day)
                                         if s.person["user"] == user)
        return v

    def node(self, t):
        """The node tier's snapshot, as `node_snapshot.py` prints it."""
        cen = self.world.census(self.host, int(t // 60) * 60)
        users = sorted({x[2] for x in cen})
        procs = sum(x[3] for x in cen)
        rss_gb = sum(x[5] for x in cen) / 1000.0
        hot = sum(1 for x in cen if x[0] == "agent")
        load = round(0.4 + 0.35 * hot + 0.05 * procs + (_h(self.host, t) % 40) / 100.0, 2)
        total = 256.0
        used = round(14.0 + rss_gb + (_h(self.host, int(t // 3600)) % 50) / 10.0, 1)
        idle = max(5.0, round(100.0 - load * 1.6 - 0.4, 2))
        per_user = []
        for u in users:
            uid = next((p["uid"] for p in _people() if p["user"] == u), 20000)
            mine = [x for x in cen if x[2] == u]
            per_user.append({
                "uid": uid, "user": u,
                "cpu_usage_usec": self.cgroup_cpu_usec(u, t),
                "nr_periods": 0, "nr_throttled": 0, "throttled_usec": 0,
                "cpu_max_quota_usec": None, "cpu_max_period_usec": 100000,
                "mem_current_bytes": int(sum(x[5] for x in mine) * 1048576 + 64 * 1048576),
                "pids_current": sum(x[3] for x in mine) + 2, "pids_max": 4096})
        mounts = (("/home", "nfs4"), ("/scratch", "nfs4"))
        return {
            "timestamp": stamp(t), "hostname": self.host, "schema_version": 1,
            "collector": {"uid": 0, "privileged": True, "args_maxlen": 256, "mode": "node"},
            "uptime": {"users": len(users), "load_1": load, "load_5": round(load * 0.93, 2),
                       "load_15": round(load * 0.88, 2)},
            "logged_in_users": [{"user": u, "tty": "pts/%d" % (i + 1), "from": "10.255.12.%d" % (i + 4),
                                 "login": "09:%02d" % (i * 7 % 60), "idle": "0.00s",
                                 "jcpu": "0.41s", "pcpu": "0.02s", "what": "-bash"}
                                for i, u in enumerate(users)],
            "process_states": {"running": max(1, int(load)), "blocked_io": 0,
                               "sleeping": 700 + procs, "zombie": 0, "stopped": 0},
            "memory": {"mem": {"total_gb": total, "used_gb": used, "free_gb": round(total - used - 60, 1),
                               "shared_gb": 1.8, "buff_cache_gb": 60.0,
                               "available_gb": round(total - used - 4, 1)},
                       "swap": {"total_gb": 4.3, "used_gb": 0.0, "free_gb": 4.3}},
            "cpu": [{"cpu": "all", "usr": round(load * 1.3, 2), "sys": round(load * 0.3, 2),
                     "iowait": 0.2, "irq": 0.0, "soft": 0.1, "idle": idle}],
            "proc_stat": {"ctxt": int((t - self.boot) * 21000), "btime": int(self.boot),
                          "processes": int((t - self.boot) * 13.7),
                          "procs_running": max(1, int(load)), "procs_blocked": 0},
            "nfs_io": [{"mountpoint": mp, "ops_s": round(5 + procs * 0.8, 1),
                        "rd_kb_s": round(100 + procs * 30.0, 1), "rd_rtt_ms": 1.3, "rd_exe_ms": 1.6,
                        "rd_retrans": 0, "wr_kb_s": round(20 + procs * 9.0, 1), "wr_rtt_ms": 5.9,
                        "wr_exe_ms": 7.2, "wr_retrans": 0} for mp, _ in mounts],
            "nfs_mountstats": [{"mountpoint": mp, "fstype": fs, "bad_xid": 0, "backlog_u": 0.0,
                                "read": {"ops": int((t - self.boot) * 40), "retrans": 0,
                                         "avg_queue_ms": 0.02, "avg_rtt_ms": 1.31, "avg_exe_ms": 1.52},
                                "write": {"ops": int((t - self.boot) * 9), "retrans": 0,
                                          "avg_queue_ms": 0.11, "avg_rtt_ms": 5.8, "avg_exe_ms": 7.1}}
                               for mp, fs in mounts],
            "disk_space": [{"mountpoint": "/", "fstype": "xfs", "total_gb": 107.3, "used_pct": 41.2,
                            "inode_used_pct": 3.1},
                           {"mountpoint": "/tmp", "fstype": "xfs", "total_gb": 429.0,
                            "used_pct": round(6.0 + procs * 0.05, 1), "inode_used_pct": 0.4}],
            "responsiveness": {"fork_exec_ms": round(1.4 + load * 0.1, 2), "stat_local_ms": 0.02,
                               "getent_ms": 4.8},
            "d_state_procs": [],
            "per_user_cgroup": per_user,
            "system_fds": {"allocated": 18000 + procs * 40, "free": 0, "max": 9223372036854775807},
            "ts_epoch": round(t, 3), "tz": time.strftime("%Z%z", time.localtime(t)),
            "redact": ["keys", "prompts"], "synthetic": True,
        }


def _people():
    from . import cast
    return cast.PEOPLE

