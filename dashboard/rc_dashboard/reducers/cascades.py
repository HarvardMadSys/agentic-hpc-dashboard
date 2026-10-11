"""Cascades: which agents started which other agents, and through what.

A node is a process whose role (roles.py) is `agent`, `infra` or `client` --
the things that run sessions, host them, or attach to them. Everything else the
classifier tags (tool-call shells, sandboxes, probes, helpers) is a step on the
way between nodes, never a node: counting those as agents is how the
collector's own figure reaches ~8 000 "nested agents" a day on madsys-gpu1,
when two weeks there hold no `claude -p` or `codex exec` launched from a tool
call at all.

What this can see is PROCESSES. Claude Code's Task-tool subagents and workflow
agents run inside their parent's process and are invisible here; only agents
that are processes of their own appear. On madsys-gpu1 those are Remote Control
workers under `claude rc`, Claude Desktop `ccd-cli` sessions under the remote
`server`, and background agents a session starts through `claude daemon run`.

A node's parent is found, in order:

  ancestor          the nearest node above it in the process tree, walking the
                    window's ProcIndex; `path` lists what it passed through
  spawned_by        the daemon's own `--spawned-by {"pid": N}`: it is started
                    detached, so the process tree does not say who asked for it
  same_user_daemon  a `bg-pty-host` orphaned to init, attached to its user's only
                    daemon on that host. Inferred, and labelled so
  tree_root         the top-most agent the collector recorded for the tree

An agent's numbers -- tool calls, CPU, drift -- are its folded trajectory
session's (trajectories.py), so the two tabs cannot disagree about a session.
"""
import collections
import json
import re
import time

from ..procindex import MAX_HOPS, users_conflict
from . import pct

NODE_ROLES = ("agent", "infra", "client")
_NO_PARENT = (None, 0, 1)
_SPAWNED_BY = re.compile(r"--spawned-by\s+(\{[^}]*\})")
_DAEMON = re.compile(r"(?:^|\s)daemon\s+run(?:\s|$)")
_PTY_HOST = re.compile(r"(?:^|\s)bg-pty-host(?:\s|$)")
_APP_SERVER = re.compile(r"(?:^|\s)app-server(?:\s|$)")
VIA_MEANING = {
    "ancestor": "nearest node above it in the process tree",
    "spawned_by": "named by the daemon's own --spawned-by argument",
    "same_user_daemon": "a bg-pty-host orphaned to init, attached to its user's only "
                        "daemon on the host (inferred)",
    "tree_root": "the top-most agent the collector recorded for the tree",
}


def spawned_by_pid(args):
    """The pid in `claude daemon run --spawned-by {...}`, or None."""
    m = _SPAWNED_BY.search(args or "")
    if not m:
        return None
    try:
        pid = json.loads(m.group(1)).get("pid")
    except (ValueError, AttributeError):
        return None
    return pid if isinstance(pid, int) else None


class CascadeReducer:
    EVENTS = ("exit", "truncated", "residency", "conn", "netio", "submit")
    KEY = "cascades"

    def __init__(self, cfg, classes, window_s=None, procs=None, traj=None):
        self.cfg = cfg
        r = cfg.get("reducers.cascades", {}) or {}
        self.max_nodes = int(r.get("max_nodes", 20000))
        self.top_n = int(r.get("top_trees", 25))
        self.max_shown = int(r.get("max_nodes_shown", 60))
        self.window_s = window_s or int(cfg.get("live.window_min", 1440)) * 60
        self.procs = procs
        self.traj = traj
        self.nodes = {}                          # (host, pid) -> dict
        self.dropped = 0
        self.census = collections.Counter()      # (agent_type, role) -> exits
        self.census_cpu = collections.Counter()

    def feed(self, rec):
        role = rec.get("_role")
        if role is None:
            return
        if rec.get("event") in ("exit", "truncated"):
            k = (rec.get("agent_type"), role)
            self.census[k] += 1
            self.census_cpu[k] += rec.get("cpu_s") or 0.0
        if role not in NODE_ROLES or rec.get("pid") is None:
            return
        e = rec.get("_ts_epoch")
        key = (rec.get("host"), rec["pid"])
        n = self.nodes.get(key)
        if n is None:
            if len(self.nodes) >= self.max_nodes:
                self.dropped += 1                # counted, never silent
                return
            args = rec.get("args") or ""
            n = self.nodes[key] = {
                "host": key[0], "pid": key[1], "ppid": rec.get("ppid"), "role": role,
                "agent_type": rec.get("agent_type"), "user": rec.get("user"),
                "command": rec.get("_command"), "args": args[:200],
                "cwd": rec.get("cwd"), "tree_root": rec.get("tree_root_pid"),
                "spawned_by": spawned_by_pid(args) if role == "infra" else None,
                "daemon": bool(role == "infra" and _DAEMON.search(args)),
                "pty_host": bool(role == "infra" and _PTY_HOST.search(args)),
                "first": e, "last": e, "start": None, "exited": False,
                "duration_s": None, "implied": False, "frontend": False,
            }
        if e is not None:
            n["first"] = e if n["first"] is None else min(n["first"], e)
            n["last"] = e if n["last"] is None else max(n["last"], e)
        if n["ppid"] in _NO_PARENT and rec.get("ppid") not in _NO_PARENT:
            n["ppid"] = rec.get("ppid")
        for f, v in (("cwd", rec.get("cwd")), ("user", rec.get("user")),
                     ("tree_root", rec.get("tree_root_pid"))):
            if n[f] is None and v is not None:
                n[f] = v
        # when it started: an exit knows its duration, a tick its age
        age = rec.get("duration_s") if rec.get("event") in ("exit", "truncated") \
            else rec.get("age_s")
        if e is not None and isinstance(age, (int, float)):
            st = e - age
            n["start"] = st if n["start"] is None else min(n["start"], st)
        if rec.get("event") in ("exit", "truncated"):
            n["exited"] = rec.get("event") == "exit"
            n["duration_s"] = rec.get("duration_s")

    # ------------------------------------------------------------- resolve
    def _parent(self, n, live, daemons):
        host = n["host"]
        p, path, seen = n["ppid"], [], {n["pid"]}
        for _ in range(MAX_HOPS):
            if p in _NO_PARENT or p in seen:
                break
            seen.add(p)
            if (host, p) in live:
                if users_conflict(n["user"], live[(host, p)]["user"]):
                    break               # a reused pid, not this process's parent
                return p, "ancestor", path
            e = self.procs.get(host, p) if self.procs is not None else None
            if e is None or users_conflict(n["user"], e[4]):
                break
            path.append(e[2] or "?")
            p = e[0]
        sb = n["spawned_by"]
        if sb is not None and (host, sb) in live:
            return sb, "spawned_by", []
        if n["pty_host"] and n["ppid"] in _NO_PARENT:
            mine = daemons.get((host, n["user"]), [])
            if len(mine) == 1:
                return mine[0], "same_user_daemon", []
        tr = n["tree_root"]
        if tr is not None and tr != n["pid"] and (host, tr) in live:
            return tr, "tree_root", []
        return None, None, []

    def result(self, now=None):
        now = now or time.time()
        cutoff = now - self.window_s
        live = {k: dict(n) for k, n in self.nodes.items() if (n["last"] or 0) >= cutoff}
        sessions = self.traj.agent_sessions(since=cutoff) if self.traj is not None else {}

        # An agent whose own process left no record -- a Claude Desktop ccd-cli
        # session talks to the API over the server's bridge, so it holds no
        # socket and never appears in a `conn` -- is still known from its tool
        # calls: a tool-call shell's parent IS its agent (owner_via=parent).
        for (host, pid), (s, via, _) in sessions.items():
            if via == "parent" and (host, pid) not in live:
                live[(host, pid)] = {
                    "host": host, "pid": pid, "ppid": None, "role": "agent",
                    "agent_type": s.agent_type, "user": s.user, "command": None,
                    "args": None, "cwd": None, "tree_root": s.tree_root,
                    "spawned_by": None, "daemon": False, "pty_host": False,
                    "first": s.first, "last": s.last, "start": None, "exited": False,
                    "duration_s": None, "implied": True, "frontend": False}

        daemons = collections.defaultdict(list)
        for (host, pid), n in live.items():
            if n["daemon"]:
                daemons[(host, n["user"])].append(pid)

        for n in live.values():
            n["parent"], n["via"], n["path"] = self._parent(n, live, daemons)
        self._break_cycles(live)
        # Codex's TUI starts `codex app-server` as its direct child and the
        # app-server runs the session: one session in two processes, not an
        # agent under an agent. The parent is re-labelled a client (front-end).
        for n in live.values():
            if n["role"] == "agent" and n["via"] == "ancestor" and not n["path"] \
                    and _APP_SERVER.search(n["args"] or ""):
                p = live[(n["host"], n["parent"])]
                if p["role"] == "agent" and p["agent_type"] == n["agent_type"]:
                    p["role"], p["frontend"] = "client", True

        kids = collections.defaultdict(list)
        for (host, pid), n in live.items():
            if n["parent"] is not None:
                kids[(host, n["parent"])].append((host, pid))
        for v in kids.values():
            v.sort(key=lambda k: live[k]["start"] or live[k]["first"] or 0)

        trees, edges, vias = [], collections.Counter(), collections.Counter()
        depth_hist, fan_hist = collections.Counter(), collections.Counter()
        for key, n in live.items():
            if n["parent"] is not None:
                p = live[(n["host"], n["parent"])]
                edges[(_label(p), _label(n))] += 1
                vias[n["via"]] += 1
                continue
            trees.append(self._tree(key, live, kids, sessions))
        for t in trees:
            if t["n_agents"]:
                depth_hist[str(t["agent_depth"])] += 1
            for f in t["_fanouts"]:
                fan_hist[str(f)] += 1

        with_agents = [t for t in trees if t["n_agents"]]
        with_agents.sort(key=lambda t: (-t["agent_depth"], -t["n_agents"],
                                        -t["tool_calls"], -(t["root"]["last_epoch"] or 0)))
        for t in with_agents:
            del t["_fanouts"]
        agents = [n for n in live.values() if n["role"] == "agent"]
        nested = [t for t in with_agents if t["agent_depth"] >= 2]
        return {
            "window": {"minutes": self.window_s // 60,
                       "from": _ts(cutoff), "to": _ts(now)},
            "summary": {
                "trees": len(with_agents),
                "nested": len(nested),
                # sessions started by infrastructure (a daemon, a remote server,
                # `claude rc`): many agents, none of them inside another
                "hosted": sum(1 for t in with_agents if t["root"]["role"] == "infra"),
                "agents": len(agents),
                "agents_nested": sum(t["agents_nested"] for t in with_agents),
                "agents_implied": sum(1 for n in agents if n["implied"]),
                "max_agent_depth": max((t["agent_depth"] for t in with_agents), default=None),
                "max_fanout": max((t["max_fanout"] for t in with_agents), default=None),
                "trees_without_agents": len(trees) - len(with_agents),
            },
            "depth_hist": dict(depth_hist),
            "fanout_hist": dict(fan_hist),
            "edges": [{"parent": p, "child": c, "n": k}
                      for (p, c), k in edges.most_common()],
            "edge_via": dict(vias),
            "trees": with_agents[:self.top_n],
            "role_census": [
                {"agent_type": at, "role": role, "n": k,
                 "cpu_s": round(self.census_cpu[(at, role)], 2),
                 "pct": pct(k, sum(self.census.values()))}
                for (at, role), k in self.census.most_common()],
            "buffer": {"nodes": len(self.nodes), "cap": self.max_nodes,
                       "dropped": self.dropped},
            "basis": {
                "node": "a process whose role is agent, infra or client (roles.py); "
                        "tool-call shells, sandboxes, probes and helpers are steps "
                        "between nodes, never nodes",
                "agent_depth": "most agents on any root-to-leaf path; 1 = no agent "
                               "under another agent",
                "fanout": "agents directly below an agent (or a tree's root) with no "
                          "other agent between",
                "via": VIA_MEANING,
                "frontend": "a codex TUI whose direct child is `codex app-server` is that "
                            "session's front-end: shown as a client, not a second agent",
                "hidden_nodes": "infra and clients with no agent beneath them are "
                                "counted per tree, not listed",
                "invisible": "in-process subagents (Claude Code's Task tool, workflow "
                             "agents) share their parent's process and do not appear",
                "drift": "per agent, from its folded trajectory session: see the "
                         "trajectories basis",
            },
        }

    @staticmethod
    def _break_cycles(live):
        """A spawned_by or tree_root link can close a loop; cut it where it closes."""
        for key, n in live.items():
            seen, cur = {key}, n
            while cur["parent"] is not None:
                pk = (cur["host"], cur["parent"])
                if pk in seen:
                    n["parent"], n["via"], n["path"] = None, None, []
                    break
                seen.add(pk)
                cur = live[pk]

    def _tree(self, root_key, live, kids, sessions):
        nodes, fanouts = [], []
        totals = {"tool_calls": 0, "cpu_s": 0.0}

        def visit(key, depth, level, anchor_up):
            n = live[key]
            is_agent = n["role"] == "agent"
            level = level + 1 if is_agent else level
            s = sessions.get(key)
            sess = s[0] if s else None
            drift = self.traj.drift(sess, n["cwd"]) if (sess is not None and self.traj) else None
            # the anchor drift was measured from, which skips a `/` launch dir
            anchor = (drift or {}).get("cwd_anchor") or (n["cwd"] if n["cwd"] != "/" else None)
            out = _node_out(n)
            out.update(depth=depth, agent_level=level if is_agent else None,
                       tool_calls=sess.n if sess else None,
                       cpu_s=round(sess.cpu, 3) if sess else None,
                       session_keys=s[2] if s else None, session_via=s[1] if s else None,
                       drift=drift, cwd_anchor=anchor,
                       cwd_vs_parent_agent=(_rel(anchor, anchor_up)
                                            if is_agent and anchor_up else None))
            if sess:
                totals["tool_calls"] += sess.n
                totals["cpu_s"] += sess.cpu
            idx = len(nodes)
            nodes.append(out)
            below = 0
            for k in kids.get(key, ()):
                below += visit(k, depth + 1, level, anchor if is_agent else anchor_up)
            # agents directly below with no other agent between. Only an agent's
            # or the root's is a fan-out: an infra node in a chain would count
            # the same agent again at every hop.
            nodes[idx]["fanout"] = below
            if below and (is_agent or depth == 0):
                fanouts.append(below)
            return 1 if is_agent else below

        visit(root_key, 0, 0, None)
        # Infrastructure and clients with no agent beneath them -- the Codex
        # daemon's update loop, an idle `claude attach` -- are counted, not listed.
        hidden = sum(1 for x in nodes[1:] if x["role"] != "agent" and not x["fanout"])
        nodes = [nodes[0]] + [x for x in nodes[1:] if x["role"] == "agent" or x["fanout"]]
        agents = [x for x in nodes if x["role"] == "agent"]
        root = nodes[0]
        return {
            "root": root, "host": root["host"],
            "users": sorted({x["user"] for x in nodes if x["user"]}),
            "agent_depth": max((x["agent_level"] for x in agents), default=0),
            "max_fanout": max(fanouts, default=0),
            "n_nodes": len(nodes), "n_agents": len(agents),
            "agents_nested": sum(1 for x in agents if x["agent_level"] >= 2),
            "agent_types": sorted({x["agent_type"] for x in agents if x["agent_type"]}),
            "tool_calls": totals["tool_calls"], "cpu_s": round(totals["cpu_s"], 3),
            "nodes": nodes[:self.max_shown], "hidden_nodes": hidden,
            "nodes_truncated": len(nodes) > self.max_shown,
            "_fanouts": fanouts,
        }


def _label(n):
    return "%s %s" % (n.get("agent_type") or "?", n["role"])


def _rel(path, anchor):
    """Where an agent's directory sits relative to the agent above it."""
    if path is None or anchor is None:
        return None
    if path == anchor:
        return "same"
    if path.startswith(anchor.rstrip("/") + "/"):
        return "inside"
    if anchor.startswith(path.rstrip("/") + "/"):
        return "above"
    return "outside"


def _ts(e):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(e)) if e else None


def _node_out(n):
    return {
        "pid": n["pid"], "ppid": n["ppid"], "host": n["host"], "role": n["role"],
        "agent_type": n["agent_type"], "user": n["user"], "command": n["command"],
        "args": n["args"], "cwd": n["cwd"], "implied": n["implied"],
        "frontend": n["frontend"],
        "parent": n["parent"], "via": n["via"], "path": n["path"],
        "spawned_by": n["spawned_by"],
        "started": _ts(n["start"]), "first_seen": _ts(n["first"]),
        "last_seen": _ts(n["last"]), "last_epoch": n["last"],
        "exited": n["exited"], "duration_s": n["duration_s"],
    }
