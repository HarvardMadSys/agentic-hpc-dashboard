"""Requirement 6: top trajectories in the window.

A trajectory is the ordered chain of tool calls in one session -- the repo's own
definition (docs/TRAJECTORIES.md, analyze/extract_trajectories.py).  Sessionizing
follows analyze/extract_trajectories.py:278-294 exactly: `apid` from the agent
pid, else `sess` from the cgroup's session-N.scope, else `tty` as user@tty, else
`usr`.  `usr` is counted but NEVER ranked -- "everything that user did today" is
not a trajectory.

An `apid` key is then FOLDED into the agent it belongs to. The collector keys a
process on its nearest classifier-tagged ancestor, and most tagged processes are
not agents (roles.py): every Claude Code Bash tool call runs in a tagged
`zsh -c source ~/.claude/shell-snapshots/...`, so one Claude session arrived as
one key per tool call -- 204 keys for one tree on madsys-gpu1 on 2026-10-06, and
383 for one Codex tree. The fold walks each key up the window's ProcIndex to the
nearest `agent`-role process, at result time, because a helper's parent is only
known once the helper's own record arrives, after its children's. Every row
says how many keys it folds and how the least certain was attributed.

This works at a 24h window because the state is incrementally composable:
records arrive in time order, so each session's counters and its run-length
encoded chain are appended in place.  Memory is O(sessions x chain cap), bounded
by evicting idle sessions.

"Top" is FOUR rankings, not one.  A single ranking is dominated by one poll loop
and one mega-user -- CLAUDE.md records one user at ~81% of agent submissions --
so each list ships with `dominant_user_share_pct`, letting the panel state its
own concentration instead of implying a fleet-wide pattern.

Each row also carries `drift`: two process-level proxies for an agent leaving the
task it started on. Neither sees the model's context; both are labelled proxies.

* cwd drift -- the share of the session's processes whose cwd lies outside the
  agent's launch directory (the agent process's own cwd, else the first call's).
  The collector reads cwd at exec for tagged processes (each tool-call shell) and
  inherits it otherwise, so a `cd` inside one shell command is not seen.
* tool-mix drift -- Jensen-Shannon divergence (base 2, 0..1) between the purpose
  mix of the first and the last third of the session's processes. Null below
  MIN_DRIFT_N processes: a third of a handful is noise, not a finding.
"""
import collections
import copy
import itertools
import math
import time
from array import array

from ..procindex import VIA

from . import pct

RANKS = ("events", "cpu_s", "distinct_tools", "chain_runs")
RANK_MEANING = {
    "events": "most tool calls",
    "cpu_s": "most self CPU",
    "distinct_tools": "most distinct effective_comm tokens",
    "chain_runs": "longest run-length-compressed chain",
}
MAX_SEQ_RUNS = 400          # matches extract_trajectories.MAX_SEQ_RUNS
MIN_DRIFT_N = 30            # processes; below this the thirds are noise
DRIFT_CAP = 100000          # per-session processes kept for drift; past it, flagged
MAX_CWDS = 200000           # distinct cwds interned per window; past it, unmeasured


class Session:
    __slots__ = ("sid", "kt", "kv", "host", "user", "cls", "agent_type", "n", "cpu",
                 "tools", "chain", "purpose", "first", "last", "rss", "rd", "wr",
                 "sandbox", "approval", "ancestry", "autonomous", "truncated", "no_ts",
                 "users", "pids", "tree_root", "chain_t", "purpose_t",
                 "ev_t", "ev_p", "ev_c", "drift_cut")

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
        # When each run began, parallel to the two chains: a folded session
        # interleaves its keys' runs by these.
        self.chain_t = []
        self.purpose_t = []
        self.tree_root = rec.get("tree_root_pid")
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
        # One entry per process, in arrival order, for drift: its time, its
        # purpose id and its cwd id (0 = not measured), interned by the reducer.
        self.ev_t = array("d")
        self.ev_p = array("H")
        self.ev_c = array("I")
        self.drift_cut = False

    def add(self, rec, now):
        e = rec.get("_ts_epoch")
        if e is None:
            self.no_ts = True          # counted, never silently stamped as "now"
        self.n += 1
        self.cpu += rec.get("cpu_s") or 0.0
        if len(self.pids) < 8 and rec.get("pid") is not None:
            self.pids.append(rec["pid"])
        t = rec.get("_eff") or rec.get("_command") or "(empty)"
        self.tools.add(t)
        _rle_push(self.chain, self.chain_t, t, e, MAX_SEQ_RUNS, self)
        _rle_push(self.purpose, self.purpose_t, rec.get("_purpose") or "other", e,
                  MAX_SEQ_RUNS, None)
        if self.tree_root is None and rec.get("tree_root_pid") is not None:
            self.tree_root = rec["tree_root_pid"]
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


def _rle_push(seq, times, tok, t, cap, owner):
    if seq and seq[-1][0] == tok:
        seq[-1][1] += 1
        return
    if len(seq) >= cap:
        if owner is not None:
            owner.truncated = True
        return
    seq.append([tok, 1])
    times.append(t)


def _interleave(parts, attr, tattr, cap):
    """Merge several sessions' run-length chains by when each run began.

    Runs re-coalesce where neighbours now match. Order is by run START, so two
    keys running at once (parallel tool calls) interleave at run grain, not
    per call. Returns (chain, times, truncated).
    """
    runs = []
    for p in parts:
        for (tok, n), t in zip(getattr(p, attr), getattr(p, tattr)):
            runs.append((float("inf") if t is None else t, tok, n))
    runs.sort(key=lambda r: r[0])
    seq, times, cut = [], [], False
    for t, tok, n in runs:
        if seq and seq[-1][0] == tok:
            seq[-1][1] += n
        elif len(seq) >= cap:
            cut = True
        else:
            seq.append([tok, n])
            times.append(None if t == float("inf") else t)
    return seq, times, cut


def fold(parts, sid, kv):
    """One Session from the keys that resolve to the same agent.

    `parts[0]` is the owner's own key when it has one: its identity (user,
    agent type, sandbox verdict) wins, since the key process is the agent.
    """
    m = copy.copy(parts[0])
    m.sid, m.kv = sid, kv
    m.n = sum(p.n for p in parts)
    m.cpu = sum(p.cpu for p in parts)
    m.tools = set().union(*(p.tools for p in parts))
    m.chain, m.chain_t, cut = _interleave(parts, "chain", "chain_t", MAX_SEQ_RUNS)
    m.purpose, m.purpose_t, _ = _interleave(parts, "purpose", "purpose_t", MAX_SEQ_RUNS)
    m.truncated = cut or any(p.truncated for p in parts)
    firsts = [p.first for p in parts if p.first is not None]
    lasts = [p.last for p in parts if p.last is not None]
    m.first = min(firsts) if firsts else None
    m.last = max(lasts) if lasts else None
    m.rss = max(p.rss for p in parts)
    m.rd = sum(p.rd for p in parts)
    m.wr = sum(p.wr for p in parts)
    m.autonomous = sum(p.autonomous for p in parts)
    m.no_ts = any(p.no_ts for p in parts)
    by_time = sorted(parts, key=lambda p: p.first if p.first is not None else float("inf"))
    for attr, unknown in (("sandbox", "unknown"), ("approval", "unknown"),
                          ("ancestry", None), ("agent_type", None),
                          ("user", None), ("host", None), ("tree_root", None)):
        if getattr(m, attr) == unknown:
            v = next((getattr(p, attr) for p in by_time if getattr(p, attr) != unknown),
                     unknown)
            setattr(m, attr, v)
    m.users = {}
    for p in parts:
        for u, c in p.users.items():
            m.users[u] = m.users.get(u, 0) + c
    m.pids = [pid for p in parts for pid in p.pids][:8]
    ev = sorted(itertools.chain.from_iterable(zip(p.ev_t, p.ev_p, p.ev_c) for p in parts),
                key=lambda x: x[0])
    m.ev_t = array("d", (x[0] for x in ev))
    m.ev_p = array("H", (x[1] for x in ev))
    m.ev_c = array("I", (x[2] for x in ev))
    m.drift_cut = any(p.drift_cut for p in parts)
    return m


def jsd(a, b):
    """Jensen-Shannon divergence, base 2, between two count maps: 0 same, 1 disjoint."""
    na, nb = sum(a.values()), sum(b.values())
    out = 0.0
    for k in set(a) | set(b):
        p, q = a.get(k, 0) / na, b.get(k, 0) / nb
        m = (p + q) / 2
        if p:
            out += 0.5 * p * math.log2(p / m)
        if q:
            out += 0.5 * q * math.log2(q / m)
    return out


def under(path, anchor):
    """`path` is `anchor` or below it -- `/a/proj-old` is NOT under `/a/proj`."""
    return path == anchor or path.startswith(anchor.rstrip("/") + "/")


class TrajectoryReducer:
    EVENTS = ("exit",)
    KEY = "trajectories"
    # Derived from the sessions and the index on demand; not worth saving.
    SNAPSHOT_SKIP = ("_fold_cache",)

    def __init__(self, cfg, classes, window_s=None, procs=None):
        self.cfg = cfg
        # The window's ProcIndex: what each key's process is. Without one the
        # keys are ranked as the collector wrote them.
        self.procs = procs
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
        self.fed = 0
        self._fold_cache = (None, None)
        # Interned per window, so a session's drift arrays hold small ints.
        self._purpose_id, self._purposes = {}, []
        self._cwd_id, self._cwds = {None: 0}, [None]
        self.cwd_overflow = 0
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
        self.fed += 1
        self._track(s, rec)

    def _track(self, s, rec):
        if len(s.ev_p) >= DRIFT_CAP:
            s.drift_cut = True
            return
        e = rec.get("_ts_epoch")
        s.ev_t.append(e if e is not None else (s.ev_t[-1] if s.ev_t else 0.0))
        p = rec.get("_purpose") or "other"
        pid = self._purpose_id.get(p)
        if pid is None:
            pid = self._purpose_id[p] = len(self._purposes)
            self._purposes.append(p)
        s.ev_p.append(pid)
        c = rec.get("cwd") or None
        cid = self._cwd_id.get(c)
        if cid is None:
            if len(self._cwds) >= MAX_CWDS:
                self.cwd_overflow += 1          # reported in `fold`, read as unmeasured
                cid = 0
            else:
                cid = self._cwd_id[c] = len(self._cwds)
                self._cwds.append(c)
        s.ev_c.append(cid)

    def drift(self, s, anchor=None):
        """The two drift proxies for one session (see the module docstring).

        `anchor` is the agent's launch directory when the caller knows it and it
        is not `/`; the first measured call's cwd otherwise, and
        `cwd_anchor_src` says which.
        """
        n = len(s.ev_p)
        d = {"n": n, "capped": s.drift_cut, "min_n": MIN_DRIFT_N,
             "mix_jsd": None, "mix_first": None, "mix_last": None}
        if n >= MIN_DRIFT_N:
            k = n // 3
            a = collections.Counter(s.ev_p[:k])
            b = collections.Counter(s.ev_p[n - k:])
            d["mix_jsd"] = round(jsd(a, b), 3)
            d["mix_first"] = [[self._purposes[i], pct(c, k)] for i, c in a.most_common(3)]
            d["mix_last"] = [[self._purposes[i], pct(c, k)] for i, c in b.most_common(3)]
        cw = [c for c in s.ev_c if c]
        d["cwd_coverage_pct"] = pct(len(cw), n) if n else None
        if not cw:
            d.update(cwd_anchor=None, cwd_anchor_src=None, cwd_outside_pct=None,
                     cwd_distinct=0, cwd_top=[], cwd_first=None, cwd_last=None)
            return d
        names = self._cwds
        if anchor == "/":
            # IDE-hosted agents run at `/`, under which every path is inside:
            # 0% by construction is not a measurement, so anchor on the work.
            anchor = None
        src = "agent" if anchor else "first_call"
        anchor = anchor or next((names[c] for c in cw if names[c] != "/"), "/")
        cnt = collections.Counter(cw)
        outside = sum(k for c, k in cnt.items() if not under(names[c], anchor))
        d.update(cwd_anchor=anchor, cwd_anchor_src=src,
                 cwd_outside_pct=pct(outside, len(cw)), cwd_distinct=len(cnt),
                 cwd_top=[[names[c], k] for c, k in cnt.most_common(5)],
                 cwd_first=names[cw[0]], cwd_last=names[cw[-1]])
        return d

    def anchor_of(self, s):
        """The agent process's own cwd, when the session is keyed on one."""
        if self.procs is None or s.kt != "apid":
            return None
        try:
            e = self.procs.get(s.host, int(s.kv))
        except (TypeError, ValueError):
            return None
        return e[3] if e else None

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

    def folded(self):
        """`_fold_all` over the held sessions, cached until something changes."""
        key = (self.fed, self.evicted, self.procs.version if self.procs else None)
        if self._fold_cache[0] != key:
            self._fold_cache = (key, self._fold_all(self.sessions.values()))
        return self._fold_cache[1]

    def agent_sessions(self, since=None):
        """(host, agent pid) -> (session, owner_via, n_keys), one per agent.

        Rows are split by day and class (the sid carries both); an agent is
        not, so its rows are folded again here. Sessions last active before
        `since` are left out.
        """
        by = {}
        for s, via, k in self.folded()[0]:
            if s.kt != "apid" or (since is not None and (s.last or 0) < since):
                continue
            try:
                pid = int(s.kv)
            except (TypeError, ValueError):
                continue
            by.setdefault((s.host, pid), []).append((s, via, k))
        out = {}
        for key, items in by.items():
            if len(items) == 1:
                out[key] = items[0]
                continue
            parts = sorted((i[0] for i in items),
                           key=lambda p: p.first if p.first is not None else float("inf"))
            vs = [i[1] for i in items if i[1]]
            out[key] = (fold(parts, parts[0].sid, parts[0].kv),
                        max(vs, key=VIA.index) if vs else None, sum(i[2] for i in items))
        return out

    def _fold_all(self, sessions):
        """Resolve every apid key to its agent and merge the keys that share one.

        Returns ([(session, owner_via, n_keys)], per-key via counts). Builds new
        objects for merged sessions: the held ones keep ingesting after this.
        """
        groups = {}
        vias = collections.Counter()
        for s in sessions:
            target, via = s.sid, None
            if s.kt == "apid" and self.procs is not None:
                try:
                    pid = int(s.kv)
                except (TypeError, ValueError):
                    pid = None
                if pid is not None:
                    owner, via = self.procs.owner(s.host, pid, s.tree_root)
                    vias[via] += 1
                    if owner != pid:
                        target = s.sid[:s.sid.rfind(":") + 1] + str(owner)
            groups.setdefault(target, []).append((s, via))
        out = []
        for target, members in groups.items():
            members.sort(key=lambda m: m[0].sid != target)    # the owner's own key first
            parts = [m[0] for m in members]
            if len(parts) == 1 and parts[0].sid == target:
                s = parts[0]
            else:
                s = fold(parts, target, target.rsplit(":", 1)[1])
            # Report how the FOLDED keys got here, at their least certain: a row
            # assembled partly by fallback must not read as fully resolved.
            vs = [v for p, v in members if p.sid != target] or [v for _, v in members]
            vs = [v for v in vs if v is not None]
            out.append((s, max(vs, key=VIA.index) if vs else None, len(parts)))
        return out, vias

    def _row(self, s, via=None, n_keys=1):
        chain = s.chain[:self.max_runs]
        return {
            "sid": s.sid, "key_type": s.kt, "host": s.host, "user": s.user,
            # how many collector session keys this row folds together, and how
            # the least certain of them was attributed (procindex.VIA)
            "fragments": n_keys, "owner_via": via,
            "drift": self.drift(s, self.anchor_of(s)),
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
        folded, vias = self.folded()
        meta = {s.sid: (via, k) for s, via, k in folded}
        live = [s for s, _, _ in folded
                if (s.last or 0) >= cutoff and s.kt in self.rankable]
        ranks, rows, dom = {}, {}, {}
        keyf = {"events": lambda s: s.n, "cpu_s": lambda s: s.cpu,
                "distinct_tools": lambda s: len(s.tools),
                "chain_runs": lambda s: len(s.chain)}
        for r in RANKS:
            top = sorted(live, key=keyf[r], reverse=True)[:self.top_n]
            ranks[r] = [s.sid for s in top]
            for s in top:
                rows.setdefault(s.sid, self._row(s, *meta[s.sid]))
            if top:
                byu = collections.Counter()
                for s in top:
                    byu[s.user] += keyf[r](s)
                tot = sum(byu.values())
                dom[r] = pct(byu.most_common(1)[0][1], tot) if tot else 0.0
            else:
                dom[r] = None
        by_cls = collections.Counter(s.cls for s, _, _ in folded)
        return {
            "window": {"minutes": self.window_s // 60,
                       "from": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(cutoff)),
                       "to": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))},
            "rank_by": "events", "rank_meaning": RANK_MEANING,
            "ranks": ranks, "rows": rows,
            "by_class_counts": dict(by_cls), "by_key_type": dict(self.by_kt),
            "fold": {"raw_sessions": len(self.sessions), "sessions": len(folded),
                     "by_via": dict(vias), "cwd_overflow": self.cwd_overflow,
                     "index": self.procs.result() if self.procs is not None else None},
            "dominant_user_share_pct": dom,
            "buffer": {"sessions": len(self.sessions), "evicted": self.evicted},
            "basis": {
                "sessionize": "session_key='apid:<agent_pid>'; absent -> sess (cgroup "
                              "session-(\\d+)\\.scope) -> tty as user@tty -> usr "
                              "(analyze/extract_trajectories.py:278-294)",
                "fold": "an apid key whose process is not an agent (a tool-call shell, "
                        "a sandbox wrapper, an embedded rg, a probe) is folded into "
                        "the nearest agent above it; failing that, into the tree's "
                        "top-most agent (owner_via=tree_root). A key whose process "
                        "was never seen is left alone (owner_via=unknown). Folded "
                        "chains interleave by run start time.",
                "rle": "run-length compression over effective_comm. True run count is "
                       "chain_runs; the displayed chain is capped at max_runs_shown.",
                "excluded_key_types": ["usr"],
                "excluded_why": "a usr-keyed group is 'everything that user did', not a "
                                "trajectory; counted in by_key_type, never ranked",
            },
        }
