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

from . import EXIT_ANSWERS, SIGPIPE, Reducer, add_counts, pct
from .own_io import DISK, OwnIo, cap_of
from ..procindex import pop_oldest
from ..tool_buckets import BUCKETS, bucket

# The key prefix of an agent type's own counters, beside its class's.
TYPE = "agent:"

# How each counted process ended, apart from a plain exit 0 (`verdict`). Only
# these are kept -- an exit 0 is the rest of a tool's calls -- so the maps stay
# as sparse as the failures are.
#   nonzero  a non-zero exit, other than an answer
#   answer   exit 1 from a program whose 1 is an answer (EXIT_ANSWERS)
#   signal   ended by a signal, other than SIGPIPE
#   pipe     ended by SIGPIPE: what read its output had gone (`| head`)
#   none     no verdict on the record
VERDICTS = ("nonzero", "answer", "signal", "pipe", "none")
# The exit codes and signals listed per class or agent type, the most first.
CODES_N = 8


def verdict(rec, tool):
    """How a process ended: one of VERDICTS, or None for a plain exit 0.

    The collector's `exit_code` is null exactly when a signal ended the process
    (`signal` set) -- the shell's 128+N is not applied -- and both are null on a
    record with no wait status."""
    sig, code = rec.get("signal"), rec.get("exit_code")
    if sig is not None:
        return "pipe" if sig == SIGPIPE else "signal"
    if code is None:
        return "none"
    if code == 0:
        return None
    return "answer" if code == 1 and tool in EXIT_ANSWERS else "nonzero"


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
        # plus written, each process's own (its reaped children's taken off:
        # own_io.py, `own`), network bytes, and peak memory (summed: a kind of
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
        # How the counted processes ended (VERDICTS), per class and agent type,
        # by tool and by kind of tool; and which exit codes and signals the
        # failures were. Same population as the calls: after the dedup.
        self.x_tool = {v: collections.defaultdict(collections.Counter) for v in VERDICTS}
        self.x_bucket = {v: collections.defaultdict(collections.Counter) for v in VERDICTS}
        self.x_codes = collections.defaultdict(collections.Counter)
        self.x_signals = collections.defaultdict(collections.Counter)
        self.child_tok = collections.OrderedDict()   # (sid, ppid) -> {child effective_comm}
        self.dropped = 0
        self.total = 0
        self.evicted = 0
        self.own = OwnIo(DISK, cap_of(cfg), "tool_mix")

    def feed(self, rec):
        sid = rec.get("_sid")
        ec = rec.get("_eff") or ""
        comm = rec.get("_command") or ""
        pid, ppid = rec.get("pid"), rec.get("ppid")
        cls = rec["_a3"]
        cpu = rec.get("cpu_s") or 0.0
        # before the dedup: a shell it drops still gives its children back
        own = self.own.take(rec)

        # remember what this process resolved to, for its parent's later exit
        if ppid is not None and ec:
            key = (sid, ppid)
            s = self.child_tok.get(key)
            if s is None:
                if len(self.child_tok) >= self.dedup_max:
                    pop_oldest(self.child_tok)
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
        disk = (own[0] or 0.0) + (own[1] or 0.0) if own else 0.0
        net = ((rec.get("net_tx_bytes") or 0) + (rec.get("net_rx_bytes") or 0)) / 1e6
        rss = rec.get("peak_rss_mb")
        atype = rec.get("agent_type")
        how = verdict(rec, ec)
        for k in (cls, TYPE + atype) if cls == "agent" and atype else (cls,):
            if how is not None:
                self.x_tool[how][k][t] += 1
                self.x_bucket[how][k][b] += 1
                if how == "nonzero":
                    self.x_codes[k][rec.get("exit_code")] += 1
                elif how == "signal":
                    self.x_signals[k][rec.get("signal")] += 1
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
    # A window is merged from saved hour states (`hours`). Two things here cross
    # an hour: the dedup index -- a shell that exits in the next hour looks up
    # the child it waited on in this one -- and the own-I/O join, where it
    # gives back what that child carried. So both are CARRIED from each hour
    # into the next one's reduction -- the dedup decides exactly as one pass
    # would -- and are not part of the saved hour, which holds only the counts
    # that hour produced.
    CARRY_AGED = ("child_tok", "reaped")

    def track(self):
        self.own.track()

    def carry(self):
        return self.own.carry({"child_tok": self.child_tok})

    def carry_caps(self):
        """What one pass holds the carried map to (`hours.combine_carries`)."""
        return dict(self.own.carry_caps(), child_tok=self.dedup_max)

    def carry_trimmed(self, c, dropped):
        self.own.carry_trimmed(c, dropped)

    def seed(self, c):
        self.child_tok = c["child_tok"]
        self.own.seed(c)

    def strip(self):
        self.child_tok = collections.OrderedDict()
        self.own.strip()

    def merge(self, o):
        """Fold a LATER state's counts into this one. `o` is consumed."""
        self.own.merge(o.own)
        for mine, theirs in ((self.b_calls, o.b_calls), (self.b_cpu, o.b_cpu),
                             (self.t_calls, o.t_calls), (self.t_cpu, o.t_cpu),
                             (self.b_io, o.b_io), (self.b_net, o.b_net), (self.b_mem, o.b_mem),
                             (self.t_io, o.t_io), (self.t_net, o.t_net),
                             (self.t_mem, o.t_mem), (self.t_mem_n, o.t_mem_n),
                             (self.x_codes, o.x_codes), (self.x_signals, o.x_signals),
                             *((self.x_tool[v], o.x_tool[v]) for v in VERDICTS),
                             *((self.x_bucket[v], o.x_bucket[v]) for v in VERDICTS)):
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

    def _exits(self, k):
        """How one class's (or agent type's) processes ended: the totals, the
        exit codes and signals of the failures, and per tool and kind of tool
        the calls beside each verdict. The tools listed are the union of the
        head_n with the most of each failing verdict, each row carrying all of
        them; a tool that never failed is the rest of `calls`."""
        def counts(store, key):
            return {v: store[v][k].get(key, 0) for v in VERDICTS}

        total = {v: sum(self.x_tool[v][k].values()) for v in VERDICTS}
        pick = set()
        for v in ("nonzero", "signal"):
            pick.update(t for t, _n in self.x_tool[v][k].most_common(self.head_n))
        return {
            "calls": sum(self.t_calls[k].values()),
            **total,
            "codes": [[c, n] for c, n in self.x_codes[k].most_common(CODES_N)],
            "signals": [[g, n] for g, n in self.x_signals[k].most_common(CODES_N)],
            "tools": sorted(({"tool": t, "bucket": bucket(t if t != "(empty)" else ""),
                              "calls": self.t_calls[k].get(t, 0), **counts(self.x_tool, t)}
                             for t in pick),
                            key=lambda r: (-(r["nonzero"] + r["signal"]), r["tool"])),
            "buckets": [{"b": b, "calls": self.b_calls[k].get(b, 0), **counts(self.x_bucket, b)}
                        for b in BUCKETS if self.b_calls[k].get(b)],
        }

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
            # how the processes ended, per class and per agent type (`_exits`)
            "exits": {
                "by_class": {k: self._exits(k) for k in classes},
                "by_agent_type": {k[len(TYPE):]: self._exits(k) for k in types},
                "answers": sorted(EXIT_ANSWERS),
                "basis": "a process's own wait status, after the shell dedup above. "
                         "exit_code is null exactly when a signal ended it; the "
                         "shell's 128+N is not applied. An exit 1 from %s is an "
                         "answer (no match, files differ), not a failure; SIGPIPE "
                         "is a reader that went first (`| head`), not a failure."
                         % ", ".join(sorted(EXIT_ANSWERS)),
            },
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
            "io_basis": "disk bytes read and written, each process's own: the "
                        "collector's figure also holds every child the process "
                        "reaped, taken back off at its exit",
            "own_io": self.own.status(),
        }
