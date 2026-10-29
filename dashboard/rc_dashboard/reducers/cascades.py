"""Agents and their subagents: which agents started which, through what, and
where each one worked.

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

An agent started beneath another agent is that agent's SUBAGENT. An agent's
numbers -- tool calls, CPU -- are its folded trajectory session's
(trajectories.py), so the two tabs cannot disagree about a session; where it
worked -- its home, the directories it ran in and the paths its commands named
-- is the workspace reducer's (workspace.py), folded the same way and composed
here, because only this reducer knows which agents are whose subagents.
"""
import collections
import json
import re
import time

from ..procindex import MAX_HOPS, users_conflict
from . import add_counts, max_of, min_of, pct
from ..redact import redact
from .live import agent_version
from .sandbox import session_state
from .workspace import tilde

NODE_ROLES = ("agent", "infra", "client")
_NO_PARENT = (None, 0, 1)
_SPAWNED_BY = re.compile(r"--spawned-by\s+(\{[^}]*\})")
_DAEMON = re.compile(r"(?:^|\s)daemon\s+run(?:\s|$)")
_PTY_HOST = re.compile(r"(?:^|\s)bg-pty-host(?:\s|$)")
_APP_SERVER = re.compile(r"(?:^|\s)app-server(?:\s|$)")
_SCRIPT_HOSTS = frozenset({"node", "nodejs", "bun", "deno"})
WORKSPACE_BASIS = {
    "ran_in": "MEASURED: processes whose working directory is that directory. The "
              "collector reads cwd at exec for agent-tagged processes and processes "
              "with a terminal, and copies the parent's at fork otherwise -- before a "
              "shell's own `cd` and before the chdir an agent's Bash tool makes, so "
              "`cd sub && make` is credited to the shell's directory. cwd_read_pct "
              "says how much was read; a relative path is never placed by climbing "
              "out of a copied cwd",
    "bytes": "MEASURED per process, not per file: the read/write syscall bytes "
             "(rchar/wchar, which include pipes, terminals and sockets) and the disk "
             "bytes (read_bytes/write_bytes) of the processes that ran in a directory, "
             "credited to where they RAN, not to where the bytes went",
    "named": "INFERRED from command lines: the paths a command names, as distinct "
             "paths per directory, classified by what the command does with them -- "
             "read (cat, grep, ls, a script run), write (> and >>, a cp/mv/rsync "
             "destination, tee, touch, mkdir, sed -i, tar -x), delete (rm, rmdir, "
             "git rm, find -delete) -- and anything else path-like as named. Naming "
             "a path is not proof of touching it: the command may fail, the path may "
             "be a pattern",
    "not_seen": "exact file-level reads and writes would need a collector probe "
                "(openat/unlink/rename tracepoints) that is not built. Claude Code's "
                "Read, Edit and Write tools and Codex's apply_patch run inside the "
                "agent's own process and leave no command line, so their file "
                "access is not here",
    "home": "the agent process's own cwd; an agent started at `/` (IDE-hosted) is "
            "anchored on its first call's directory off the root, labelled so",
    "outside": "a write or delete NAMED outside the home is flagged: the agent "
               "changed, or tried to change, something beyond the directory it was "
               "started in. /tmp, caches and the agent's own state directory are "
               "flagged too, and labelled by kind",
    "collapse": "directories deeper than collapse_depth levels below the home, or "
                "below a top-level root (/tmp, /scratch, ~/.cache, another user's "
                "home), are folded into that ancestor and counted as `deeper`",
    "subagents": "with subagents adds every agent started beneath this one (see "
                 "Agent trees), judged against THIS agent's home",
    "scope": "exit and truncated records of agent-class processes that carry a "
             "session key, folded into their agent through the process index as "
             "trajectory rows are; a fork that never exec'd inherits its parent's "
             "argv and is not read twice; ~ is the user's home as their agent's own "
             "paths name it, else /home/<user>",
}
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

    def __init__(self, cfg, classes, window_s=None, procs=None, traj=None, ws=None, sb=None):
        self.cfg = cfg
        r = cfg.get("reducers.cascades", {}) or {}
        self.max_nodes = int(r.get("max_nodes", 20000))
        self.top_n = int(r.get("top_trees", 25))
        self.max_shown = int(r.get("max_nodes_shown", 60))
        self.window_s = window_s or int(cfg.get("live.window_min", 1440)) * 60
        self.procs = procs
        self.traj = traj
        self.ws = ws
        self.sb = sb
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
                # Masked BEFORE the cut: the payload is masked again on the
                # way out, but a token cut in half here would no longer look
                # like one. Once per node, so it costs nothing per record.
                "command": rec.get("_command"), "args": redact(args)[:200],
                "cwd": rec.get("cwd"), "tree_root": rec.get("tree_root_pid"),
                "spawned_by": spawned_by_pid(args) if role == "infra" else None,
                "daemon": bool(role == "infra" and _DAEMON.search(args)),
                "pty_host": bool(role == "infra" and _PTY_HOST.search(args)),
                "first": e, "last": e, "start": None, "exited": False,
                "duration_s": None, "implied": False, "frontend": False,
                "approval": None, "approval_detail": None, "version": None,
                "surface": None, "surface_detail": None,
            }
        if role == "agent":
            _launch(n, rec.get("_approval"), rec.get("_approval_detail"),
                    agent_version(rec.get("_command"), rec.get("args")),
                    rec.get("_surface"), rec.get("_surface_detail"))
        elif n["surface"] is None and rec.get("_surface") is not None:
            # a host: where the sessions it holds are driven from
            n["surface"], n["surface_detail"] = rec["_surface"], rec.get("_surface_detail")
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

    # --------------------------------------------------------------- merge
    def merge(self, o):
        """Fold a LATER state into this one. `o` is consumed.

        A node keeps what it was first seen with and takes what `feed` would
        have filled in from the later records: a parent, cwd, user or tree root
        it lacked, the widened first/last and start, and its exit."""
        for key, n in o.nodes.items():
            cur = self.nodes.get(key)
            if cur is None:
                if len(self.nodes) >= self.max_nodes:
                    self.dropped += 1            # counted, never silent
                    continue
                self.nodes[key] = n
                continue
            for f, pick in (("first", min_of), ("last", max_of), ("start", min_of)):
                cur[f] = pick(cur[f], n[f])
            if cur["ppid"] in _NO_PARENT and n["ppid"] not in _NO_PARENT:
                cur["ppid"] = n["ppid"]
            for f in ("cwd", "user", "tree_root"):
                if cur[f] is None and n[f] is not None:
                    cur[f] = n[f]
            _launch(cur, n["approval"], n["approval_detail"], n["version"],
                    n["surface"], n["surface_detail"])
            if n["exited"] or n["duration_s"] is not None:
                cur["exited"], cur["duration_s"] = n["exited"], n["duration_s"]
        self.dropped += o.dropped
        add_counts(self.census, o.census)
        add_counts(self.census_cpu, o.census_cpu)

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
                    "duration_s": None, "implied": True, "frontend": False,
                    "approval": None, "approval_detail": None, "version": None,
                    "surface": None, "surface_detail": None}

        daemons = collections.defaultdict(list)
        for (host, pid), n in live.items():
            if n["daemon"]:
                daemons[(host, n["user"])].append(pid)

        for n in live.values():
            n["parent"], n["via"], n["path"] = self._parent(n, live, daemons)
        self._break_cycles(live)
        # Codex's TUI starts `codex app-server` as its direct child and the
        # app-server runs the session: one session in two processes, not an
        # agent under an agent. So does npm's `node /usr/local/bin/codex`, which
        # only execs the native binary (rtx6000a, 2026-10-11: two such pairs,
        # read as two agents with a subagent each). The parent is re-labelled a
        # client (front-end).
        for n in live.values():
            if n["role"] != "agent" or n["via"] != "ancestor" or n["path"]:
                continue
            p = live[(n["host"], n["parent"])]
            if p["role"] == "agent" and p["agent_type"] == n["agent_type"] and (
                    _APP_SERVER.search(n["args"] or "")
                    or (p["command"] or "") in _SCRIPT_HOSTS):
                p["role"], p["frontend"] = "client", True
                # the session's launch is the front-end's: `codex --yolo` starts
                # an app-server whose own argv names no approval
                if n["approval"] in (None, "unknown", "unobservable"):
                    n["approval"], n["approval_detail"] = p["approval"], p["approval_detail"]
                n["version"] = n["version"] or p["version"]
                # and so is its surface: a daemon names no client of its own
                if n["surface"] in (None, "unknown") and p["surface"] is not None:
                    n["surface"], n["surface_detail"] = p["surface"], p["surface_detail"]

        kids = collections.defaultdict(list)
        for (host, pid), n in live.items():
            if n["parent"] is not None:
                kids[(host, n["parent"])].append((host, pid))
        for v in kids.values():
            v.sort(key=lambda k: live[k]["start"] or live[k]["first"] or 0)

        # Where each agent worked: its keys folded through the same process
        # index, and its home -- its own cwd, or its first call's off `/`.
        folded = self.ws.fold(cutoff) if self.ws is not None else {}
        homes = {key: _home(n, folded.get(key)) for key, n in live.items()
                 if n["role"] == "agent"}
        views = {}
        for key, (home, _src) in homes.items():
            if key in folded:
                views[key] = self.ws.view([folded[key]], home, live[key]["user"])
        # Whether each agent's tool calls ran in a sandbox: the Sandbox tab's
        # verdict for the session the agent owns, read from the same reducer.
        boxed = self.sb.agent_calls() if self.sb is not None else {}

        trees, edges, vias = [], collections.Counter(), collections.Counter()
        depth_hist, fan_hist = collections.Counter(), collections.Counter()
        for key, n in live.items():
            if n["parent"] is not None:
                p = live[(n["host"], n["parent"])]
                edges[(_label(p), _label(n))] += 1
                vias[n["via"]] += 1
                continue
            trees.append(self._tree(key, live, kids, sessions, homes, views, boxed))
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
            "workspace": self._workspace(live, kids, folded, homes, views),
            "basis": {
                "node": "a process whose role is agent, infra or client (roles.py); "
                        "tool-call shells, sandboxes, probes and helpers are steps "
                        "between nodes, never nodes",
                "agent_depth": "most agents on any root-to-leaf path; 1 = no agent "
                               "under another agent",
                "fanout": "agents directly below an agent (or a tree's root) with no "
                          "other agent between",
                "via": VIA_MEANING,
                "frontend": "a codex TUI whose direct child is `codex app-server`, or a "
                            "node launcher whose direct child is the same agent's native "
                            "binary, is that session's front-end: shown as a client, not "
                            "as an agent with a subagent",
                "hidden_nodes": "infra and clients with no agent beneath them are "
                                "counted per tree, not listed",
                "subagent": "an agent started, through any number of steps, beneath "
                            "another agent",
                "invisible": "in-process subagents (Claude Code's Task tool, workflow "
                             "agents) share their parent's process and do not appear; "
                             "their commands are counted in their parent's",
                "home": "an agent's own cwd, the directory it was started in; one "
                        "started at `/` (IDE-hosted) is anchored on its first call's "
                        "directory off the root instead",
                "dir_vs_parent": "where an agent's home sits relative to the home of "
                                 "the agent above it: same, inside, above or outside",
            },
        }

    def _workspace(self, live, kids, folded, homes, views):
        """Per agent: where it worked, alone and with its subagents."""
        if self.ws is None:
            return None
        ws = self.ws
        owners = set(folded)

        def below(key):
            """Every agent beneath `key`, through infra and clients too."""
            out, stack = [], list(kids.get(key, ()))
            while stack:
                k = stack.pop()
                if live[k]["role"] == "agent":
                    out.append(k)
                stack.extend(kids.get(k, ()))
            return out

        def up(key):
            """(the nearest agent above, the root of the tree)."""
            n, agent, seen = live[key], None, {key}
            while n["parent"] is not None:
                pk = (n["host"], n["parent"])
                if pk in seen or pk not in live:
                    break
                seen.add(pk)
                n = live[pk]
                if agent is None and n["role"] == "agent":
                    agent = n["pid"]
            return agent, n["pid"]

        rows = []
        for key, (home, src) in homes.items():
            subs = below(key)
            mine = [folded[k] for k in subs if k in folded]
            if key not in views and not mine:
                continue
            n = live[key]
            parent_agent, root = up(key)
            tree = None
            if mine:
                own = [folded[key]] if key in folded else []
                tree = ws.view(own + mine, home, n["user"])
            uhome = ws.home_of(n["user"])
            rows.append({
                "host": n["host"], "pid": n["pid"], "agent_type": n["agent_type"],
                "user": n["user"], "tree_root": root, "parent_agent": parent_agent,
                "subagents": [k[1] for k in subs],
                "home": redact(home) if home else None, "home_src": src,
                "home_shown": redact(tilde(home, uhome)) if home else None,
                "self": views.get(key), "tree": tree,
                "_rank": ((tree or views.get(key))["summary"]["procs"]),
            })
        rows.sort(key=lambda r: (-r["_rank"], r["host"] or "", r["pid"]))
        for r in rows:
            del r["_rank"]

        def wrote(r):
            return bool(r["self"] and r["self"]["summary"]["outside_writes"])
        # The busiest are served; an agent that wrote outside its home is
        # served however quiet, because that is the reading the panel is for.
        served = rows[:ws.max_agents] + [r for r in rows[ws.max_agents:] if wrote(r)]
        unowned = sum(a.procs for k, a in folded.items()
                      if k not in live or live[k]["role"] != "agent")
        # each agent judged on its own processes: a subagent's writes count once,
        # against the subagent, not again against every agent above it
        flagged = sum(1 for r in rows if wrote(r))

        def reached(r, kind):
            """It ran in, or named a path in, a place of this kind outside its home."""
            return bool(r["self"] and any(g["kind"] == kind
                                          for g in r["self"]["summary"]["roots"]))
        return {
            "agents": served,
            "summary": {
                "agents": len(rows), "shown": len(served),
                "with_subagents": sum(1 for r in rows if r["subagents"]),
                "with_outside_writes": flagged,
                "other_homes": sum(1 for r in rows if reached(r, "other user's home")),
                "shared_roots": sum(1 for r in rows if reached(r, "shared storage")),
                "unowned_procs": unowned,
                "agents_without_records": sum(1 for k in homes if k not in owners),
                "collapse_depth": ws.depth, "rows_cap": ws.max_rows,
            },
            "buffer": ws.result(),
            "basis": WORKSPACE_BASIS,
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

    def _tree(self, root_key, live, kids, sessions, homes, views, boxed):
        nodes, fanouts = [], []
        totals = {"tool_calls": 0, "cpu_s": 0.0}

        def visit(key, depth, level, anchor_up):
            n = live[key]
            is_agent = n["role"] == "agent"
            level = level + 1 if is_agent else level
            s = sessions.get(key)
            sess = s[0] if s else None
            home, home_src = homes.get(key, (None, None))
            # a node that is not an agent has no home of its own; its cwd, off `/`,
            # is what an agent below it is compared with
            anchor = home if is_agent else (n["cwd"] if n["cwd"] != "/" else None)
            v = views.get(key)
            calls = boxed.get(key) if is_agent else None
            out = _node_out(n)
            out.update(depth=depth, agent_level=level if is_agent else None,
                       tool_calls=sess.n if sess else None,
                       cpu_s=round(sess.cpu, 3) if sess else None,
                       session_keys=s[2] if s else None, session_via=s[1] if s else None,
                       home=home, home_src=home_src,
                       workspace=({"dirs": v["summary"]["dirs"],
                                   "inside_pct": v["summary"]["inside_pct"],
                                   "outside_writes": v["summary"]["outside_writes"]}
                                  if v else None),
                       cwd_vs_parent_agent=(_rel(anchor, anchor_up)
                                            if is_agent and anchor_up else None),
                       sandbox=session_state(calls) if calls else None,
                       sandbox_calls=calls or None)
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
            "nodes": _pick(nodes, self.max_shown) if len(nodes) > self.max_shown else nodes,
            "hidden_nodes": hidden,
            "nodes_truncated": len(nodes) > self.max_shown,
            "_fanouts": fanouts,
        }


def _busiest(x):
    """Sort key, busiest first: processes, then writes outside home, then CPU."""
    return (-(x.get("tool_calls") or 0),
            -((x.get("workspace") or {}).get("outside_writes") or 0),
            -(x.get("cpu_s") or 0))


def _pick(nodes, cap):
    """The `cap` nodes of a bigger tree that are sent, and what the rest did.

    The root, and every node an agent hangs from, come first: they are the
    tree's shape. Then each parent's agents that started none, busiest first,
    a parent at a time in turn -- so a Codex with 211 subagents sends its
    busiest, not its first 58 in the order they were walked. What a parent's
    unsent agents did is counted on the parent's node (`folded`), so the page
    can say so rather than show 58 as if they were all. The sent nodes keep
    the order they were walked in, a parent before its children."""
    by_pid = {x["pid"]: x for x in nodes}
    root = nodes[0]

    def agent_above(x):
        p = by_pid.get(x.get("parent"))
        while p is not None and p is not root and p["role"] != "agent":
            p = by_pid.get(p.get("parent"))
        return p if p is not None else root

    shape = [root] + [x for x in nodes[1:] if x["role"] != "agent" or x.get("fanout")]
    groups = {}
    for x in nodes[1:]:
        if x["role"] == "agent" and not x.get("fanout"):
            groups.setdefault(agent_above(x)["pid"], []).append(x)
    for g in groups.values():
        g.sort(key=_busiest)
    keep = {id(x) for x in shape[:cap]}
    room, i = cap - len(keep), 0
    while room > 0 and any(i < len(g) for g in groups.values()):
        for g in groups.values():
            if i < len(g) and room > 0:
                keep.add(id(g[i]))
                room -= 1
        i += 1
    for pid, g in groups.items():
        left = [x for x in g if id(x) not in keep]
        if left:
            calls = [x.get("tool_calls") or 0 for x in left]
            by_pid[pid]["folded"] = {
                "agents": len(left),
                "ran_nothing": sum(1 for c in calls if c == 0),
                "one": sum(1 for c in calls if c == 1),
                "processes": sum(calls),
                "outside": sum(1 for x in left if (x.get("workspace") or {}).get("outside_writes")),
            }
    return [x for x in nodes if id(x) in keep]


def _launch(n, approval, detail, version, surface=None, surface_detail=None):
    """Take what an agent's own record says of its launch: the approval its
    flags set, the version its program's path names, and where it is driven
    from (normalize.surface_of). A bypass read off any record stands -- the
    collector reads the uncapped argv, an emitted one may be cut -- and
    otherwise the first verdict read is kept."""
    if approval not in (None, "unknown") and (
            n["approval"] in (None, "unknown") or approval == "bypassed"):
        n["approval"], n["approval_detail"] = approval, detail
    if n["version"] is None:
        n["version"] = version
    if surface is not None and n["surface"] in (None, "unknown"):
        n["surface"], n["surface_detail"] = surface, surface_detail


def _home(n, acc):
    """(home, how it was found) for an agent node: its own cwd unless that is
    `/`, else the first directory off `/` any of its calls ran in."""
    if n["cwd"] and n["cwd"] != "/":
        return n["cwd"], "agent"
    if acc is not None and acc.cwd0:
        return acc.cwd0, "first_call"
    return None, None


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
        "approval": n["approval"], "approval_detail": n["approval_detail"],
        "version": n["version"],
        # an agent whose own record was never read cannot say where it was
        # driven from; a host says it for its sessions, a client has none
        "surface": n["surface"] or ("unknown" if n["role"] == "agent" else None),
        "surface_detail": n["surface_detail"],
    }
