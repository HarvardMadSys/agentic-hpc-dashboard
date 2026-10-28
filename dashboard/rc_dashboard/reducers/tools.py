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

# The key prefix of an agent type's own counters, beside its class's.
TYPE = "agent:"


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
        # plus written, network bytes, and peak memory (summed: a kind of
        # tool's share of every process's peak, and for a tool a mean over the
        # processes that reported one, and its max).
        #
        # Every counter is keyed by a class, and an agent's process ALSO by
        # its agent type ("agent:codex"), so the Agents tab shows one type's
        # tools without mixing in another's (its "nothing mixes types").
        self.b_io = collections.defaultdict(collections.Counter)
        self.b_net = collections.defaultdict(collections.Counter)
        self.b_mem = collections.defaultdict(collections.Counter)
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

        b = bucket(ec)
        t = ec or "(empty)"
        io = rec.get("io") or {}
        disk = (io.get("rd_mb") or 0.0) + (io.get("wr_mb") or 0.0)
        net = ((rec.get("net_tx_bytes") or 0) + (rec.get("net_rx_bytes") or 0)) / 1e6
        rss = rec.get("peak_rss_mb")
        atype = rec.get("agent_type")
        for k in (cls, TYPE + atype) if cls == "agent" and atype else (cls,):
            self.b_calls[k][b] += 1
            self.b_cpu[k][b] += cpu
            self.t_calls[k][t] += 1
            self.t_cpu[k][t] += cpu
            if disk:
                self.b_io[k][b] += disk
                self.t_io[k][t] += disk
            if net:
                self.b_net[k][b] += net
                self.t_net[k][t] += net
            if rss is not None:
                self.b_mem[k][b] += rss
                self.t_mem[k][t] += rss
                self.t_mem_n[k][t] += 1
                mx = self.t_mem_max[k]
                if rss > mx.get(t, -1.0):
                    mx[t] = rss
        at = (cls, atype or "")
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
                             (self.b_io, o.b_io), (self.b_net, o.b_net), (self.b_mem, o.b_mem),
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

    def _rows(self, k):
        """One class's (or agent type's) work by kind of tool, by every resource."""
        tot = {f: sum(c[k].values()) for f, c in (("calls", self.b_calls), ("cpu", self.b_cpu),
                                                  ("io", self.b_io), ("net", self.b_net),
                                                  ("mem", self.b_mem))}

        def share(c, f, b):
            return pct(c[k].get(b, 0.0), tot[f]) if tot[f] else None

        return {
            "calls": tot["calls"], "cpu_s": round(tot["cpu"], 2),
            "io_mb": round(tot["io"], 2), "net_mb": round(tot["net"], 2),
            "mem_mb": round(tot["mem"], 1),
            "rows": [{"b": b,
                      "calls": self.b_calls[k].get(b, 0),
                      "calls_pct": pct(self.b_calls[k].get(b, 0), tot["calls"]),
                      "cpu_s": round(self.b_cpu[k].get(b, 0.0), 2),
                      "cpu_pct": pct(self.b_cpu[k].get(b, 0.0), tot["cpu"]),
                      "io_mb": round(self.b_io[k].get(b, 0.0), 2),
                      "io_pct": share(self.b_io, "io", b),
                      "net_mb": round(self.b_net[k].get(b, 0.0), 2),
                      "net_pct": share(self.b_net, "net", b),
                      # the sum of each process's peak: a kind of tool's share
                      # of the memory its processes held at their highest
                      "mem_mb": round(self.b_mem[k].get(b, 0.0), 1),
                      "mem_pct": share(self.b_mem, "mem", b)}
                     for b in BUCKETS],
        }

    def _head(self, k):
        calls = sum(self.t_calls[k].values())
        cpu = sum(self.t_cpu[k].values())
        return [{"tool": t, "calls": n, "calls_pct": pct(n, calls),
                 "cpu_pct": pct(self.t_cpu[k].get(t, 0.0), cpu)}
                for t, n in self.t_calls[k].most_common(self.head_n)]

    def _tools(self, k):
        """The top tools by every resource, as one list: the union of the head_n
        busiest by processes, CPU, disk, network, and memory, each row carrying
        all of them, so the page sorts by whichever it is asked."""
        calls = sum(self.t_calls[k].values())
        cpu = sum(self.t_cpu[k].values())
        io = sum(self.t_io[k].values())
        net = sum(self.t_net[k].values())
        mem_mean = {t: self.t_mem[k][t] / n for t, n in self.t_mem_n[k].items() if n}
        pick = set()
        for counter in (self.t_calls[k], self.t_cpu[k], self.t_io[k], self.t_net[k]):
            pick.update(t for t, _v in counter.most_common(self.head_n))
        pick.update(sorted(mem_mean, key=lambda t: -mem_mean[t])[:self.head_n])
        return [{
            "tool": t, "bucket": bucket(t if t != "(empty)" else ""),
            "calls": self.t_calls[k].get(t, 0),
            "calls_pct": pct(self.t_calls[k].get(t, 0), calls),
            "cpu_s": round(self.t_cpu[k].get(t, 0.0), 2),
            "cpu_pct": pct(self.t_cpu[k].get(t, 0.0), cpu) if cpu else None,
            "io_mb": round(self.t_io[k].get(t, 0.0), 2),
            "io_pct": pct(self.t_io[k].get(t, 0.0), io) if io else None,
            "net_mb": round(self.t_net[k].get(t, 0.0), 2),
            "net_pct": pct(self.t_net[k].get(t, 0.0), net) if net else None,
            "mem_mean_mb": round(mem_mean[t], 1) if t in mem_mean else None,
            "mem_max_mb": round(self.t_mem_max[k][t], 1) if t in self.t_mem_max[k] else None,
        } for t in pick]

    def result(self):
        units = list(self.b_calls)
        classes = [k for k in units if not k.startswith(TYPE)]
        types = [k for k in units if k.startswith(TYPE)]
        return {
            "buckets": list(BUCKETS),
            "by_class": {k: self._rows(k) for k in classes},
            "tools": {k: self._tools(k) for k in classes},
            # the same three for each agent type's processes alone
            "by_agent_type": {k[len(TYPE):]: {"by": self._rows(k), "tools": self._tools(k),
                                             "head": self._head(k),
                                             "n_tools": len(self.t_calls[k])}
                              for k in types},
            # class -> agent type -> share of the class's calls and CPU; an
            # untagged process (a person's shell) is the type ""
            "by_type": self._by_type(),
            "head": {k: self._head(k) for k in classes},
            # how many different tools each class ran: the head shows only the top
            "n_tools": {k: len(self.t_calls[k]) for k in classes},
            "dedup": {"dropped": self.dropped,
                      "dropped_pct": pct(self.dropped, self.total),
                      "index_evicted": self.evicted,
                      "rule": "resolved shell dropped iff its captured direct child "
                              "(same session, ppid==shell.pid) resolves to the same token"},
            "cpu_basis": "self cpu_s, each process once; child_cpu_s NEVER added "
                         "(analyze/common.py:364 -- the naive sum inflates login CPU "
                         "1.83x, unevenly by class)",
        }
