"""Records as the collector writes them (collector/ebpf_trace.py), field for field.

`envelope` is `_envelope`, `Proc.identity` is `_base_identity` (the same keys,
in the same order), and `exit_record` is `_exit_record` plus the `conns` and
`net_endpoints` blocks `_write_exit` adds. Every record also carries
`"synthetic": true`, which no collector writes: it is how the service, and
anyone reading an export, can tell a demo record from a measured one.

The costs a process is given (`costs`) are drawn per command from ranges that
look like a login node's, log-uniformly: they make the panels' shapes
recognisable, and they are not a model of anything.
"""
import json
import math
import time

SCHEMA = 8
TS = "%Y-%m-%d %H:%M:%S"


def stamp(epoch):
    return time.strftime(TS, time.localtime(epoch))


try:                                    # a dashboard dependency, and 5x json's speed
    import orjson

    def line(rec):
        return orjson.dumps(rec) + b"\n"
except ImportError:                     # pragma: no cover
    def line(rec):
        return (json.dumps(rec, separators=(",", ":"), ensure_ascii=False) + "\n").encode()


def envelope(event, t, host, collector_pid):
    return {"ts": stamp(t), "ts_epoch": round(t, 6), "host": host, "event": event,
            "collector_pid": collector_pid, "schema_version": SCHEMA, "synthetic": True}


def logu(rng, lo, hi):
    """Log-uniform in [lo, hi]: most draws small, a few large, as costs are."""
    if lo <= 0 or hi <= lo:
        return lo
    return math.exp(rng.uniform(math.log(lo), math.log(hi)))


# effective command -> cpu_s, duration_s, peak RSS MB, disk read MB, disk write MB,
# threads. Ranges; `costs` draws within them. A duration a session already fixed
# (a poll, a job the agent waited on) overrides the range.
PROFILE = {
    "bash": ((0.002, 0.03), (0.01, 0.4), (3, 6), (0, 0.2), (0, 0.02), 1),
    "zsh": ((0.004, 0.04), (0.02, 0.5), (4, 8), (0, 0.3), (0, 0.02), 1),
    "sh": ((0.001, 0.01), (0.005, 0.2), (1.5, 3), (0, 0.05), (0, 0.01), 1),
    "rg": ((0.02, 0.6), (0.02, 0.9), (8, 40), (0.5, 30), (0, 0), 8),
    "grep": ((0.002, 0.08), (0.003, 0.1), (2, 4), (0.05, 5), (0, 0), 1),
    "find": ((0.01, 0.4), (0.02, 1.5), (3, 6), (0.2, 20), (0, 0), 1),
    "ls": ((0.001, 0.01), (0.002, 0.05), (2, 3), (0, 0.2), (0, 0), 1),
    "cat": ((0.001, 0.005), (0.002, 0.03), (1, 2), (0.01, 2), (0, 0), 1),
    "head": ((0.001, 0.003), (0.002, 0.02), (1, 2), (0.01, 0.5), (0, 0), 1),
    "tail": ((0.001, 0.004), (0.002, 0.05), (1, 2), (0.01, 2), (0, 0), 1),
    "sed": ((0.001, 0.01), (0.002, 0.04), (1.5, 3), (0.01, 1), (0, 0.5), 1),
    "wc": ((0.001, 0.01), (0.002, 0.03), (1, 2), (0.01, 3), (0, 0), 1),
    "git": ((0.005, 0.25), (0.01, 0.8), (6, 30), (0.1, 15), (0, 2), 1),
    "gh": ((0.05, 0.4), (0.4, 2.5), (25, 60), (0.1, 2), (0, 0.1), 8),
    "python3": ((0.2, 8.0), (0.4, 14.0), (40, 450), (2, 80), (0.1, 20), 4),
    "pytest": ((1.5, 25.0), (2.5, 40.0), (120, 650), (10, 120), (0.5, 30), 6),
    "pip": ((2.0, 25.0), (6.0, 70.0), (90, 260), (20, 300), (40, 900), 4),
    "make": ((0.02, 0.4), (3.0, 90.0), (6, 20), (0.5, 10), (0, 1), 1),
    "cc1plus": ((0.8, 9.0), (0.9, 10.0), (180, 900), (2, 30), (0.5, 15), 1),
    "ld": ((0.3, 3.0), (0.4, 4.0), (120, 700), (20, 200), (10, 120), 1),
    "cmake": ((0.3, 2.5), (0.6, 6.0), (20, 60), (1, 20), (0.2, 5), 2),
    "ctest": ((0.5, 12.0), (1.0, 20.0), (20, 200), (2, 50), (0.1, 10), 2),
    "cargo": ((0.5, 6.0), (3.0, 120.0), (60, 300), (5, 80), (5, 200), 8),
    "rustc": ((2.0, 40.0), (2.5, 60.0), (300, 1800), (10, 120), (5, 160), 8),
    "julia": ((3.0, 60.0), (5.0, 90.0), (300, 1200), (20, 200), (0.5, 40), 8),
    "Rscript": ((0.5, 20.0), (1.0, 40.0), (120, 900), (5, 300), (0.2, 50), 2),
    "snakemake": ((1.0, 8.0), (3.0, 60.0), (90, 300), (2, 30), (0.5, 10), 4),
    "samtools": ((2.0, 40.0), (3.0, 60.0), (20, 200), (200, 4000), (100, 2000), 4),
    "sbatch": ((0.01, 0.06), (0.08, 0.6), (8, 14), (0.05, 0.5), (0, 0.01), 1),
    "squeue": ((0.01, 0.05), (0.05, 0.6), (8, 14), (0.02, 0.3), (0, 0), 1),
    "sacct": ((0.02, 0.15), (0.1, 1.5), (10, 20), (0.05, 1), (0, 0), 1),
    "scontrol": ((0.01, 0.05), (0.05, 0.4), (8, 14), (0.02, 0.3), (0, 0), 1),
    "sinfo": ((0.01, 0.05), (0.05, 0.5), (8, 14), (0.02, 0.3), (0, 0), 1),
    "scancel": ((0.005, 0.03), (0.05, 0.3), (6, 10), (0.01, 0.1), (0, 0), 1),
    "srun": ((0.05, 0.4), (2.0, 600.0), (12, 30), (0.1, 2), (0, 0.1), 4),
    "sleep": ((0.0005, 0.002), (5.0, 60.0), (1, 2), (0, 0), (0, 0), 1),
    "vim": ((0.05, 4.0), (30.0, 2400.0), (8, 40), (0.1, 5), (0.01, 2), 1),
    "less": ((0.005, 0.1), (5.0, 300.0), (2, 6), (0.05, 20), (0, 0), 1),
    "rsync": ((1.0, 40.0), (20.0, 900.0), (10, 60), (200, 9000), (200, 9000), 1),
    "tar": ((0.5, 30.0), (5.0, 300.0), (3, 10), (100, 5000), (100, 5000), 1),
    "gzip": ((1.0, 60.0), (2.0, 90.0), (2, 4), (50, 2000), (20, 800), 1),
    "ssh": ((0.01, 0.5), (1.0, 900.0), (6, 12), (0.05, 1), (0, 0.1), 1),
    "nvidia-smi": ((0.02, 0.15), (0.1, 0.8), (20, 40), (0.1, 1), (0, 0), 2),
    "node": ((0.05, 3.0), (0.2, 20.0), (60, 400), (1, 40), (0.1, 5), 11),
    "conda": ((0.4, 3.0), (0.8, 6.0), (60, 200), (5, 60), (0, 2), 2),
    "ps": ((0.005, 0.05), (0.01, 0.1), (3, 5), (0.1, 1), (0, 0), 1),
    "uname": ((0.0005, 0.002), (0.001, 0.01), (1, 2), (0, 0), (0, 0), 1),
    "codex-linux-sandbox": ((0.002, 0.02), (0.01, 0.3), (4, 10), (0, 0.2), (0, 0), 1),
    "nbody": ((1.0, 12.0), (2.0, 60.0), (300, 2400), (5, 120), (20, 600), 8),
    "rm": ((0.001, 0.2), (0.002, 0.5), (1, 3), (0, 1), (0, 0), 1),
    "bwrap": ((0.002, 0.02), (0.01, 0.3), (2, 5), (0, 0.2), (0, 0), 1),
}
DEFAULT = ((0.002, 0.05), (0.005, 0.3), (2, 8), (0, 1), (0, 0.2), 1)


def costs(rng, cmd, duration=None, scale=1.0):
    """cpu_s, duration_s, rss_mb, rd_mb, wr_mb, threads for one run of `cmd`."""
    cpu, dur, rss, rd, wr, thr = PROFILE.get(cmd, DEFAULT)
    d = duration if duration is not None else logu(rng, *dur) * scale
    c = min(logu(rng, *cpu) * scale, d * max(1, thr) * 0.95) if d > 0 else 0.0
    return (round(c, 4), round(d, 4), round(logu(rng, *rss), 2),
            round(rng.uniform(*rd) * scale, 3), round(rng.uniform(*wr) * scale, 3), thr)


class Proc:
    """One process: who it is (the identity block) and what it cost."""

    __slots__ = ("pid", "ppid", "uid", "user", "tty", "command", "args", "cwd",
                 "actor", "actor3", "attribution", "agent_type", "agent_pid",
                 "session_uuid", "tree_root_pid", "depth", "is_agent", "autonomous",
                 "approval_mode", "approval_src", "sandbox", "sandbox_src",
                 "sandbox_detail", "sandbox_ancestry", "cgroup", "pgrp", "sid",
                 "start", "end", "cpu_s", "rss_mb", "rd_mb", "wr_mb", "threads",
                 "exit_code", "signal", "net", "conns", "children_cpu", "redacted")

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, None)
        self.exit_code, self.children_cpu = 0, 0.0
        for k, v in kw.items():
            setattr(self, k, v)

    def identity(self):
        ap = self.agent_pid
        return {
            "actor": self.actor, "actor3": self.actor3, "attribution": self.attribution,
            "pid": self.pid, "ppid": self.ppid, "uid": self.uid, "user": self.user,
            "tty": self.tty, "command": self.command, "args": self.args,
            # what the collector's masking replaced in `args` (schema 8), or None
            "args_redacted": self.redacted, "argv_source": "kernel" if self.args else None,
            "args_len": len(self.args) if self.args else None,
            "args_truncated": False if self.args else None,
            "cwd": self.cwd,
            "sandbox": self.sandbox, "sandbox_src": self.sandbox_src,
            "sandbox_detail": self.sandbox_detail, "sandbox_ancestry": self.sandbox_ancestry,
            "approval_mode": self.approval_mode, "approval_src": self.approval_src,
            "agent_type": self.agent_type, "agent_pid": ap,
            "session_key": ("apid:%s" % ap) if ap is not None else None,
            "session_uuid": self.session_uuid if ap is not None else None,
            "tree_root_pid": self.tree_root_pid if ap is not None else None,
            "depth": self.depth, "is_agent": bool(self.is_agent),
            "autonomous": self.autonomous if self.is_agent else None,
            "cgroup": self.cgroup,
        }


def exit_record(p, host, collector_pid, rng):
    """`_exit_record` for `p`, which ran [p.start, p.end] and spent p.cpu_s."""
    t = p.end
    dur = max(0.0, t - p.start)
    # on-CPU time within the wall time it ran: the panels set aside a record whose
    # run time exceeds its duration (normalize's checks)
    cpu = min(p.cpu_s or 0.0, dur * 0.97)
    run = cpu
    wait = round(cpu * rng.uniform(0.01, 0.12), 4)
    rd, wr = p.rd_mb or 0.0, p.wr_mb or 0.0
    net = p.net or {}
    rec = envelope("exit", t, host, collector_pid)
    rec.update(p.identity())
    rec.update({
        "fork_only": False,
        "start": stamp(p.start),
        "duration_s": round(dur, 4),
        "duration_src": "exec",
        "cpu_s": round(cpu, 4),
        "cpu_scope": "process",
        "child_cpu_s": round(p.children_cpu or 0.0, 4),
        "cpu_pct": round(100.0 * cpu / dur, 1) if dur >= 0.01 else None,
        "peak_rss_mb": p.rss_mb,
        "threads": p.threads or 1,
        "min_flt": int(200 + (p.rss_mb or 1) * rng.uniform(150, 260)),
        "maj_flt": int(rng.random() < 0.08) * rng.randint(1, 40),
        "cmaj_flt": 0,
        "nvcsw": int(2 + dur * rng.uniform(2, 40)),
        "nivcsw": int(cpu * rng.uniform(5, 60)),
        "sched_run_s": round(run, 4),
        "sched_wait_s": wait,
        "blkio_wait_s": round(min(dur, (rd + wr) * rng.uniform(0.0002, 0.003)), 4),
        "swapin_wait_s": 0.0,
        "freepages_wait_s": 0.0,
        "dstate_wait_s": round(min(dur, (rd + wr) * rng.uniform(0.0001, 0.002)), 4),
        "dstate_episodes": int((rd + wr) > 1) * rng.randint(1, 30),
        "dstate_max_s": round(min(dur, rng.uniform(0.0005, 0.05)) if (rd + wr) > 1 else 0.0, 4),
        "dstate_src": "task",
        "net_tx_bytes": int(net.get("tx", 0)),
        "net_rx_bytes": int(net.get("rx", 0)),
        "net_calls": int(net.get("calls", 0)),
        "pgrp": p.pgrp or p.pid,
        "sid": p.sid or p.pid,
        "exit_code": p.exit_code if p.signal is None else None,
        "signal": p.signal,
        "core_dumped": False,
        "io": {"rd_mb": round(rd, 3), "wr_mb": round(wr, 3),
               "rchar_mb": round(rd * rng.uniform(1.05, 1.8) + 0.004, 3),
               "wchar_mb": round(wr * rng.uniform(1.0, 1.3) + 0.001, 3),
               "scope": "process"},
        "samples": None, "state_last": None,
        "conns": p.conns,
        "net_endpoints": endpoints_block(net) if net else None,
    })
    return rec


def endpoints_block(net):
    eps = [dict(e) for e in net.get("endpoints", ())]
    return {"n": len(eps), "in_series": bool(net.get("in_series")), "endpoints": eps,
            "unattributed": None}


def endpoint(proto, addr, port, tx, rx, calls, provider=None, host=None, inbound=False,
             external=True):
    return {"proto": proto, "addr": addr, "port": port, "host": host, "inbound": inbound,
            "external": external, "provider": provider, "tx_bytes": int(tx),
            "rx_bytes": int(rx), "calls": int(calls)}
