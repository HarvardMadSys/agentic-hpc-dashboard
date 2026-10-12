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
        # What else each kind of tool and each tool cost, so the tables can be
        # read by any one resource (roadmap notes 24 and 25): disk bytes read
        # plus written, network bytes, and peak memory (summed for a mean over
        # the processes that reported one, and its max).
        self.b_io = collections.defaultdict(collections.Counter)
        self.b_net = collections.defaultdict(collections.Counter)
        self.t_io = collections.defaultdict(collections.Counter)
        self.t_net = collections.defaultdict(collections.Counter)
        self.t_mem = collections.defaultdict(collections.Counter)
        self.t_mem_n = collections.defaultdict(collections.Counter)
        self.t_mem_max = collections.defaultdict(dict)
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
        t = ec or "(empty)"
        self.b_calls[cls][b] += 1
        self.b_cpu[cls][b] += cpu
        self.t_calls[cls][t] += 1
        self.t_cpu[cls][t] += cpu
        io = rec.get("io") or {}
        disk = (io.get("rd_mb") or 0.0) + (io.get("wr_mb") or 0.0)
        net = ((rec.get("net_tx_bytes") or 0) + (rec.get("net_rx_bytes") or 0)) / 1e6
        if disk:
            self.b_io[cls][b] += disk
            self.t_io[cls][t] += disk
        if net:
            self.b_net[cls][b] += net
            self.t_net[cls][t] += net
        rss = rec.get("peak_rss_mb")
        if rss is not None:
            self.t_mem[cls][t] += rss
            self.t_mem_n[cls][t] += 1
            mx = self.t_mem_max[cls]
            if rss > mx.get(t, -1.0):
                mx[t] = rss
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
                             (self.t_calls, o.t_calls), (self.t_cpu, o.t_cpu),
                             (self.b_io, o.b_io), (self.b_net, o.b_net),
                             (self.t_io, o.t_io), (self.t_net, o.t_net),
                             (self.t_mem, o.t_mem), (self.t_mem_n, o.t_mem_n)):
            for cls, c in theirs.items():
                add_counts(mine[cls], c)
        for cls, c in o.t_mem_max.items():
            mx = self.t_mem_max[cls]
            for t, v in c.items():
                if v > mx.get(t, -1.0):
                    mx[t] = v
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
            io = sum(self.b_io[cls].values())
            net = sum(self.b_net[cls].values())
            by_class[cls] = {
                "calls": calls, "cpu_s": round(cpu, 2),
                "io_mb": round(io, 2), "net_mb": round(net, 2),
                "rows": [{"b": b,
                          "calls": self.b_calls[cls].get(b, 0),
                          "calls_pct": pct(self.b_calls[cls].get(b, 0), calls),
                          "cpu_s": round(self.b_cpu[cls].get(b, 0.0), 2),
                          "cpu_pct": pct(self.b_cpu[cls].get(b, 0.0), cpu),
                          "io_mb": round(self.b_io[cls].get(b, 0.0), 2),
                          "io_pct": pct(self.b_io[cls].get(b, 0.0), io) if io else None,
                          "net_mb": round(self.b_net[cls].get(b, 0.0), 2),
                          "net_pct": pct(self.b_net[cls].get(b, 0.0), net) if net else None}
                         for b in BUCKETS],
            }
        head = {}
        for cls in self.t_calls:
            calls = sum(self.t_calls[cls].values())
            cpu = sum(self.t_cpu[cls].values())
            head[cls] = [{"tool": t, "calls": n, "calls_pct": pct(n, calls),
                          "cpu_pct": pct(self.t_cpu[cls].get(t, 0.0), cpu)}
                         for t, n in self.t_calls[cls].most_common(self.head_n)]
        # Each class's top tools by every resource, as one list: the union of
        # the head_n busiest by processes, CPU, disk, network, and memory, each
        # row carrying all of them, so the page sorts by whichever it is asked.
        tools = {}
        for cls in self.t_calls:
            calls = sum(self.t_calls[cls].values())
            cpu = sum(self.t_cpu[cls].values())
            io = sum(self.t_io[cls].values())
            net = sum(self.t_net[cls].values())
            mem_mean = {t: self.t_mem[cls][t] / n for t, n in self.t_mem_n[cls].items() if n}
            pick = set()
            for counter in (self.t_calls[cls], self.t_cpu[cls], self.t_io[cls], self.t_net[cls]):
                pick.update(t for t, _v in counter.most_common(self.head_n))
            pick.update(sorted(mem_mean, key=lambda t: -mem_mean[t])[:self.head_n])
            tools[cls] = [{
                "tool": t, "bucket": bucket(t if t != "(empty)" else ""),
                "calls": self.t_calls[cls].get(t, 0),
                "calls_pct": pct(self.t_calls[cls].get(t, 0), calls),
                "cpu_s": round(self.t_cpu[cls].get(t, 0.0), 2),
                "cpu_pct": pct(self.t_cpu[cls].get(t, 0.0), cpu) if cpu else None,
                "io_mb": round(self.t_io[cls].get(t, 0.0), 2),
                "io_pct": pct(self.t_io[cls].get(t, 0.0), io) if io else None,
                "net_mb": round(self.t_net[cls].get(t, 0.0), 2),
                "net_pct": pct(self.t_net[cls].get(t, 0.0), net) if net else None,
                "mem_mean_mb": round(mem_mean[t], 1) if t in mem_mean else None,
                "mem_max_mb": round(self.t_mem_max[cls][t], 1) if t in self.t_mem_max[cls] else None,
            } for t in pick]
        return {
            "buckets": list(BUCKETS),
            "by_class": by_class,
            "tools": tools,
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
