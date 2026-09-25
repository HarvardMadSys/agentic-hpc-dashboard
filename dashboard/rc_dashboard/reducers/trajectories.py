"""Requirement 6: top trajectories in the window.

A trajectory is the ordered chain of tool calls in one session -- the repo's own
definition (docs/TRAJECTORIES.md, analyze/extract_trajectories.py).  Sessionizing
follows analyze/extract_trajectories.py:278-294 exactly: `apid` from the agent
pid, else `sess` from the cgroup's session-N.scope, else `tty` as user@tty, else
`usr`.  `usr` is counted but NEVER ranked -- "everything that user did today" is
not a trajectory.

This works at a 24h window because the state is incrementally composable:
records arrive in time order, so each session's counters and its run-length
encoded chain are appended in place.  Memory is O(sessions x chain cap), bounded
by evicting idle sessions.

"Top" is FOUR rankings, not one.  A single ranking is dominated by one poll loop
and one mega-user -- CLAUDE.md records one user at ~81% of agent submissions --
so each list ships with `dominant_user_share_pct`, letting the panel state its
own concentration instead of implying a fleet-wide pattern.
"""
import collections
import time

from . import pct

RANKS = ("events", "cpu_s", "distinct_tools", "chain_runs")
RANK_MEANING = {
    "events": "most tool calls",
    "cpu_s": "most self CPU",
    "distinct_tools": "most distinct effective_comm tokens",
    "chain_runs": "longest run-length-compressed chain",
}
MAX_SEQ_RUNS = 400          # matches extract_trajectories.MAX_SEQ_RUNS


class Session:
    __slots__ = ("sid", "kt", "kv", "host", "user", "cls", "agent_type", "n", "cpu",
                 "tools", "chain", "purpose", "first", "last", "rss", "rd", "wr",
                 "sandbox", "approval", "ancestry", "autonomous", "truncated", "no_ts",
                 "users", "pids")

    def __init__(self, rec):
        self.sid = rec["_sid"]
        self.kt = rec["_kt"]
        self.kv = rec["_kv"]
        self.host = rec.get("host")
        self.user = rec.get("user")
        self.cls = rec["_a3"]
        self.agent_type = rec.get("agent_type")
        self.n = 0
        self.cpu = 0.0
        self.tools = set()
        self.chain = []          # [[tool, runlength], ...]
        self.purpose = []        # [[purpose, runlength], ...]
        self.first = None
        self.last = None
        self.rss = 0.0
        self.rd = 0.0
        self.wr = 0.0
        self.sandbox = "unknown"
        self.approval = "unknown"
        self.ancestry = None
        self.autonomous = 0
        self.truncated = False
        self.no_ts = False
        self.users = {}
        self.pids = []

    def add(self, rec, now):
        e = rec.get("_ts_epoch")
        if e is None:
            self.no_ts = True          # counted, never silently stamped as "now"
        self.n += 1
        self.cpu += rec.get("cpu_s") or 0.0
        if len(self.pids) < 8 and rec.get("pid") is not None:
            self.pids.append(rec["pid"])
        t = rec.get("_eff") or rec.get("comm") or "(empty)"
        self.tools.add(t)
        _rle_push(self.chain, t, MAX_SEQ_RUNS, self)
        _rle_push(self.purpose, rec.get("_purpose") or "other", MAX_SEQ_RUNS, None)
        if e is not None:
            self.first = e if self.first is None else min(self.first, e)
            self.last = e if self.last is None else max(self.last, e)
        r = rec.get("peak_rss_mb")
        if r:
            self.rss = max(self.rss, r)
        io = rec.get("io")
        if isinstance(io, dict):
            self.rd += io.get("rd_mb") or 0.0
            self.wr += io.get("wr_mb") or 0.0
        if rec.get("autonomous"):
            self.autonomous += 1
        if self.sandbox == "unknown" and rec.get("_sandbox") != "unknown":
            self.sandbox = rec.get("_sandbox")
        if self.approval == "unknown" and rec.get("_approval") != "unknown":
            self.approval = rec.get("_approval")
        if not self.ancestry and rec.get("_sandbox_ancestry"):
            self.ancestry = rec["_sandbox_ancestry"]
        if self.agent_type is None and rec.get("agent_type"):
            self.agent_type = rec["agent_type"]
        # The session is keyed on the agent pid, and the FIRST record for it may
        # be one the collector saw without a resolvable user (a seeded process
        # predating the collector, or a root helper). Backfill from any later
        # record rather than leaving the row's owner blank -- "which session" is
        # not useful without "whose".
        # A uid with no passwd entry resolves to no `user`, and "uid 1234" is
        # still someone you can go and find -- so fall back rather than blank.
        ident = rec.get("user") or (
            "uid:%s" % rec["uid"] if rec.get("uid") is not None else None)
        if not self.user and ident:
            self.user = ident
        if self.host is None and rec.get("host"):
            self.host = rec["host"]
        if ident:
            self.users[ident] = self.users.get(ident, 0) + 1


def _rle_push(seq, tok, cap, owner):
    if seq and seq[-1][0] == tok:
        seq[-1][1] += 1
        return
    if len(seq) >= cap:
        if owner is not None:
            owner.truncated = True
        return
    seq.append([tok, 1])


class TrajectoryReducer:
    EVENTS = ("exit",)
    KEY = "trajectories"

    def __init__(self, cfg, classes, window_s=None):
        self.cfg = cfg
        r = cfg.get("reducers.trajectories", {}) or {}
        self.top_n = r.get("top_n", 15)
        self.max_runs = r.get("max_runs_shown", 60)
        self.purpose_cap = r.get("purpose_seq_cap", 80)
        self.idle_evict = r.get("idle_evict_s", 7200)
        self.max_sessions = r.get("max_sessions", 50000)
        self.rankable = set(r.get("include_key_types", ["apid", "sess", "tty"]))
        self.sessions = {}
        self.by_kt = collections.Counter()
        self.evicted = 0
        # The window is the VIEW's, not a fixed config value: one reducer instance
        # exists per selected window, each ranking the sessions that window holds.
        self.window_s = window_s or int(cfg.get("live.window_min", 1440)) * 60

    def feed(self, rec):
        sid = rec.get("_sid")
        if not sid:
            return
        now = time.time()
        s = self.sessions.get(sid)
        if s is None:
            if len(self.sessions) >= self.max_sessions:
                self._evict(now, force=True)
            s = self.sessions[sid] = Session(rec)
            self.by_kt[rec["_kt"]] += 1
        s.add(rec, now)

    def _evict(self, now, force=False):
        # Idle eviction must never reach INSIDE the window, or the panel would
        # rank "sessions active in the last idle_evict_s" while claiming to rank
        # the window -- at the 2 h default and a 24 h window it already silently
        # did. Memory stays bounded by `max_sessions`, which force-evicts.
        keep_s = max(self.idle_evict, self.window_s)
        cutoff = now - (keep_s if not force else keep_s / 2)
        dead = [k for k, v in self.sessions.items() if (v.last or 0) < cutoff]
        if force and not dead:
            dead = sorted(self.sessions, key=lambda k: self.sessions[k].last or 0)
            dead = dead[:max(1, len(dead) // 10)]
        for k in dead:
            del self.sessions[k]
            self.evicted += 1

    def _row(self, s):
        chain = s.chain[:self.max_runs]
        return {
            "sid": s.sid, "key_type": s.kt, "host": s.host, "user": s.user,
            "actor3": s.cls, "agent_type": s.agent_type,
            "n_events": s.n, "cpu_s": round(s.cpu, 3),
            # a session can span users (a root helper inside a user's tree), so
            # name the dominant one and carry the rest
            "users": sorted(s.users, key=lambda u: -s.users[u]),
            "n_users": len(s.users), "pids": s.pids,
            "agent_pid": (s.kv if s.kt == "apid" else None),
            "distinct_tools": len(s.tools), "chain_runs": len(s.chain),
            "wall_s": round((s.last - s.first), 1) if s.first and s.last else None,
            "peak_rss_mb": round(s.rss, 1),
            "io_rd_mb": round(s.rd, 3), "io_wr_mb": round(s.wr, 3),
            "sandbox": s.sandbox, "approval": s.approval,
            "sandbox_ancestry": s.ancestry, "autonomous": s.autonomous,
            "tool_rle": chain, "chain_truncated": s.truncated or len(s.chain) > self.max_runs,
            "purpose_rle": s.purpose[:self.purpose_cap],
        }

    def result(self, now=None):
        now = now or time.time()
        self._evict(now)
        cutoff = now - self.window_s
        live = [s for s in self.sessions.values()
                if (s.last or 0) >= cutoff and s.kt in self.rankable]
        ranks, rows, dom = {}, {}, {}
        keyf = {"events": lambda s: s.n, "cpu_s": lambda s: s.cpu,
                "distinct_tools": lambda s: len(s.tools),
                "chain_runs": lambda s: len(s.chain)}
        for r in RANKS:
            top = sorted(live, key=keyf[r], reverse=True)[:self.top_n]
            ranks[r] = [s.sid for s in top]
            for s in top:
                rows.setdefault(s.sid, self._row(s))
            if top:
                byu = collections.Counter()
                for s in top:
                    byu[s.user] += keyf[r](s)
                tot = sum(byu.values())
                dom[r] = pct(byu.most_common(1)[0][1], tot) if tot else 0.0
            else:
                dom[r] = None
        by_cls = collections.Counter(s.cls for s in self.sessions.values())
        return {
            "window": {"minutes": self.window_s // 60,
                       "from": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(cutoff)),
                       "to": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))},
            "rank_by": "events", "rank_meaning": RANK_MEANING,
            "ranks": ranks, "rows": rows,
            "by_class_counts": dict(by_cls), "by_key_type": dict(self.by_kt),
            "dominant_user_share_pct": dom,
            "buffer": {"sessions": len(self.sessions), "evicted": self.evicted},
            "basis": {
                "sessionize": "session_key='apid:<agent_pid>'; absent -> sess (cgroup "
                              "session-(\\d+)\\.scope) -> tty as user@tty -> usr "
                              "(analyze/extract_trajectories.py:278-294)",
                "rle": "run-length compression over effective_comm. True run count is "
                       "chain_runs; the displayed chain is capped at max_runs_shown.",
                "excluded_key_types": ["usr"],
                "excluded_why": "a usr-keyed group is 'everything that user did', not a "
                                "trajectory; counted in by_key_type, never ranked",
            },
        }
