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

from . import Reducer, add_counts, pct
from ..tool_buckets import BUCKETS, bucket


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
        # (class, agent_type) -> calls / CPU: which agent or editor the calls
        # came from. Flat Counters, so a saved window pickles them as they are.
        self.at_calls = collections.Counter()
        self.at_cpu = collections.Counter()
        self.child_tok = {}            # (sid, ppid) -> {child effective_comm}
        self.dropped = 0
        self.total = 0
        self.evicted = 0

    def feed(self, rec):
        sid = rec.get("_sid")
        ec = rec.get("_eff") or ""
        comm = rec.get("_command") or ""
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
        self.b_calls[cls][b] += 1
        self.b_cpu[cls][b] += cpu
        self.t_calls[cls][ec or "(empty)"] += 1
        self.t_cpu[cls][ec or "(empty)"] += cpu
        at = (cls, rec.get("agent_type") or "")
        self.at_calls[at] += 1
        self.at_cpu[at] += cpu

    # ------------------------------------------------------------- merge
    # A window is merged from saved hour states (`hours`). The dedup index is
    # the one thing here that crosses an hour: a shell that exits in the next
    # hour looks up the child it waited on in this one. So it is CARRIED from
    # each hour into the next one's reduction -- the dedup decides exactly as
    # one pass would -- and is not part of the saved hour, which holds only
    # the counts that hour produced.
    CARRY_AGED = ("child_tok",)

    def carry(self):
        return {"child_tok": self.child_tok}

    def seed(self, c):
        self.child_tok = c["child_tok"]

    def strip(self):
        self.child_tok = {}

    def merge(self, o):
        """Fold a LATER state's counts into this one. `o` is consumed."""
        for mine, theirs in ((self.b_calls, o.b_calls), (self.b_cpu, o.b_cpu),
                             (self.t_calls, o.t_calls), (self.t_cpu, o.t_cpu)):
            for cls, c in theirs.items():
                add_counts(mine[cls], c)
        add_counts(self.at_calls, o.at_calls)
        add_counts(self.at_cpu, o.at_cpu)
        self.dropped += o.dropped
        self.total += o.total
        self.evicted += o.evicted

    def _by_type(self):
        calls = collections.Counter()
        cpu = collections.Counter()
        for (cls, _at), n in self.at_calls.items():
            calls[cls] += n
        for (cls, _at), v in self.at_cpu.items():
            cpu[cls] += v
        out = {}
        for (cls, at), n in self.at_calls.most_common():
            out.setdefault(cls, {})[at] = {
                "calls": n, "calls_pct": pct(n, calls[cls]),
                "cpu_s": round(self.at_cpu[(cls, at)], 2),
                "cpu_pct": pct(self.at_cpu[(cls, at)], cpu[cls]) if cpu[cls] else None,
            }
        return out

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
            # class -> agent type -> share of the class's calls and CPU; an
            # untagged process (a person's shell) is the type ""
            "by_type": self._by_type(),
            "head": head,
            # how many different tools each class ran: the head shows only the top
            "n_tools": {c: len(v) for c, v in self.t_calls.items()},
            "dedup": {"dropped": self.dropped,
                      "dropped_pct": pct(self.dropped, self.total),
                      "index_evicted": self.evicted,
                      "rule": "resolved shell dropped iff its captured direct child "
                              "(same session, ppid==shell.pid) resolves to the same token"},
            "cpu_basis": "self cpu_s, each process once; child_cpu_s NEVER added "
                         "(analyze/common.py:364 -- the naive sum inflates login CPU "
                         "1.83x, unevenly by class)",
        }
