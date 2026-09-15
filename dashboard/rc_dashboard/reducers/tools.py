"""Requirement 3: a TOOL distribution, not a head of literal command lines.

Two levels, because they answer different questions: the 12-way bucket gives the
shape of the work, the `effective_comm` token names the tool.  Both ship
count-weighted AND CPU-weighted -- polling is roughly a third of an agent's
process exits and a fiftieth of its CPU, so either weighting alone misleads.

The shell-wrapper dedup matters more than it looks.  When an agent runs
`bash -lc "make ..."`, the tracer captures BOTH the shell (whose args resolve to
`make` via effective_comm) and the child `make`: ~8% of agent events are such
duplicates.  analyze/tool_calls_distribution.py:206-227 drops the resolved-shell
event iff its own captured direct child resolves to the same token.  That is a
two-pass rule over an in-memory table; the streaming version here is EXACT for
the case that actually produces the duplicates, because a shell that ran a
command waits for it, so the child's exit is emitted before the shell's.  When
the child was not captured the shell is kept, which is the original's own
conservative rule.
"""
import collections
import os
import sys

from . import Reducer, pct

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "..", "analyze"))
try:
    from tool_bucket_lib import bucket, BUCKETS, shellenv_sub
except Exception:                                    # keep the service running
    BUCKETS = ("other",)
    def bucket(ec):                                  # noqa: E301
        return "other"
    def shellenv_sub(cm, ec, a):                     # noqa: E301
        return "other"


class ToolMixReducer(Reducer):
    EVENTS = ("exit",)
    KEY = "tool_mix"

    def __init__(self, cfg, classes):
        super().__init__(cfg, classes)
        r = cfg.get("reducers.tool_mix", {}) or {}
        self.head_n = r.get("head_n", 12)
        self.dedup_max = r.get("dedup_sessions_max", 200000)
        self.b_calls = collections.defaultdict(collections.Counter)
        self.b_cpu = collections.defaultdict(collections.Counter)
        self.t_calls = collections.defaultdict(collections.Counter)
        self.t_cpu = collections.defaultdict(collections.Counter)
        self.shell_kinds = collections.defaultdict(collections.Counter)
        self.child_tok = {}            # (sid, ppid) -> {child effective_comm}
        self.dropped = 0
        self.total = 0
        self.evicted = 0

    def feed(self, rec):
        sid = rec.get("_sid")
        ec = rec.get("_eff") or ""
        comm = rec.get("comm") or ""
        pid, ppid = rec.get("pid"), rec.get("ppid")
        cls = rec["_a3"]
        cpu = rec.get("cpu_s") or 0.0

        # remember what this process resolved to, for its parent's later exit
        if ppid is not None and ec:
            key = (sid, ppid)
            s = self.child_tok.get(key)
            if s is None:
                if len(self.child_tok) >= self.dedup_max:
                    self.child_tok.pop(next(iter(self.child_tok)), None)
                    self.evicted += 1
                s = self.child_tok[key] = set()
            s.add(ec)

        self.total += 1
        resolved_shell = comm != ec and bool(rec.get("_shell_resolved"))
        if resolved_shell and ec in self.child_tok.get((sid, pid), ()):
            self.dropped += 1
            return                      # its child was captured: count the child

        if rec.get("_shell_resolved") or comm == ec:
            pass
        b = bucket(ec)
        if b == "shell/env-init":
            self.shell_kinds[cls][shellenv_sub(comm, ec, rec.get("args") or "")] += 1
        self.b_calls[cls][b] += 1
        self.b_cpu[cls][b] += cpu
        self.t_calls[cls][ec or "(empty)"] += 1
        self.t_cpu[cls][ec or "(empty)"] += cpu

    def result(self):
        by_class = {}
        for cls in self.b_calls:
            calls = sum(self.b_calls[cls].values())
            cpu = sum(self.b_cpu[cls].values())
            by_class[cls] = {
                "calls": calls, "cpu_s": round(cpu, 2),
                "rows": [{"b": b,
                          "calls": self.b_calls[cls].get(b, 0),
                          "calls_pct": pct(self.b_calls[cls].get(b, 0), calls),
                          "cpu_s": round(self.b_cpu[cls].get(b, 0.0), 2),
                          "cpu_pct": pct(self.b_cpu[cls].get(b, 0.0), cpu)}
                         for b in BUCKETS],
            }
        head = {}
        for cls in self.t_calls:
            calls = sum(self.t_calls[cls].values())
            cpu = sum(self.t_cpu[cls].values())
            head[cls] = [{"tool": t, "calls": n, "calls_pct": pct(n, calls),
                          "cpu_pct": pct(self.t_cpu[cls].get(t, 0.0), cpu)}
                         for t, n in self.t_calls[cls].most_common(self.head_n)]
        return {
            "buckets": list(BUCKETS),
            "by_class": by_class,
            "head": head,
            "shell_kinds": {c: dict(v) for c, v in self.shell_kinds.items()},
            "dedup": {"dropped": self.dropped,
                      "dropped_pct": pct(self.dropped, self.total),
                      "index_evicted": self.evicted,
                      "rule": "resolved shell dropped iff its captured direct child "
                              "(same session, ppid==shell.pid) resolves to the same token"},
            "cpu_basis": "self cpu_s, each process once; child_cpu_s NEVER added "
                         "(analyze/common.py:364 -- the naive sum inflates login CPU "
                         "1.83x, unevenly by class)",
        }
