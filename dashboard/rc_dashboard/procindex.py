"""Who each classifier-tagged process was and who started it, for one window.

A session key is the pid of the nearest process the collector's classifier
tagged (`agent_pid`), and most tagged processes are not agents -- see `roles`.
Folding a key back into its session means walking up from that process to the
nearest one whose role is `agent`, and the record that names the key cannot do
that: its own fields give the tagged ancestor and the top of the tree, not the
chain between. This index holds the chain's links.

Tagged processes are held (`_role` is set by the normaliser on those alone),
plus each untagged process a held one names as its parent: the links between a
tagged process and its agent, such as the subshell a tool call's shell forks
(its argv is inherited, so it is never tagged). A child's record arrives before
its parent's, so asking for the parent when the child is seen is enough, and the
ask climbs one link per record. That keeps it to a few thousand entries a day
on a login node rather than one per exit. A link still missing at result time
ends the walk, and the caller falls back to the tree root -- which is exactly
the agent in the common case, a session not nested under any other.

Pids are reused -- madsys-gpu1's wrapped on 2026-10-09 -- so a link is refused
when it would join two users' processes (root aside), and an untagged parent is
refused when it started after the child that asked for it. Without both, an
unrelated `cut` that inherited a dead parent's pid made glen's codex the parent
of haoran's Claude Desktop server.

The parent kept is the FIRST one seen. A process whose parent exits is
reparented to init (or a subreaper), and its later tick records say so; the
process that started it is the one that matters here.
"""
from .roles import spawned_by_agent

MAX_HOPS = 64
_NO_PARENT = (None, 0, 1)

# How a session key was resolved, most certain first. A folded row reports the
# least certain of its keys, so a guess is never presented as a measurement.
VIA = ("self", "ancestor", "parent", "tree_root", "root", "unknown")


class ProcIndex:
    EVENTS = ("exit", "truncated", "residency", "conn", "netio", "submit")
    KEY = "_procs"

    def __init__(self, cfg, classes=None):
        self.cap = int(cfg.get("reducers.sessions.proc_index_max", 200000) or 200000)
        # (host, pid) -> [ppid, role, spawned_by_agent, command, cwd, user]
        self.entries = {}
        self.evicted = 0
        # (host, pid) -> [earliest start, user] of the held children naming it
        # as parent. A process reusing the pid later, or as someone else, is
        # refused.
        self.wanted = {}
        self.wanted_full = 0
        # Bumped on every change, so a consumer can cache what it derived.
        self.version = 0

    def feed(self, rec):
        pid = rec.get("pid")
        if pid is None:
            return
        host = rec.get("host")
        role = rec.get("_role")
        key = (host, pid)
        e = self.entries.get(key)
        if e is not None:
            if e[0] in _NO_PARENT and rec.get("ppid") not in _NO_PARENT:
                e[0] = rec.get("ppid")
                self.version += 1
            if e[4] is None and rec.get("cwd"):
                e[4] = rec["cwd"]
                self.version += 1
            return
        want = self.wanted.pop(key, None)
        if role is None:
            if want is None:
                return                  # untagged, and no held process's parent
            st = _start(rec)
            if (st is not None and st > want[0] + 1.0) or users_conflict(
                    rec.get("user"), want[1]):
                return                  # the pid, reused after or by someone else
        if len(self.entries) >= self.cap:
            # Oldest first: dict order is insertion order. Counted, never silent.
            self.entries.pop(next(iter(self.entries)))
            self.evicted += 1
        # cwd is the FIRST one seen: an agent's launch directory. The collector
        # re-reads a tree root's cwd every tick, and Claude Code never chdirs
        # (it tracks its working directory itself), but a later record of a
        # process that did would otherwise move the anchor drift is measured from.
        self.entries[key] = [rec.get("ppid"), role,
                             spawned_by_agent(role, rec.get("_command")),
                             rec.get("_command"), rec.get("cwd"), rec.get("user")]
        self.version += 1
        ppid = rec.get("ppid")
        pk = (host, ppid)
        if ppid not in _NO_PARENT and pk not in self.entries:
            st = _start(rec)
            st = float("inf") if st is None else st
            if pk in self.wanted:
                self.wanted[pk][0] = min(self.wanted[pk][0], st)
            elif len(self.wanted) < self.cap:
                self.wanted[pk] = [st, rec.get("user")]
            else:
                self.wanted_full += 1   # counted, never silent

    def role(self, host, pid):
        e = self.entries.get((host, pid))
        return e[1] if e else None

    def get(self, host, pid):
        """(ppid, role, command, cwd, user), or None if the process was never seen."""
        e = self.entries.get((host, pid))
        return (e[0], e[1], e[3], e[4], e[5]) if e else None

    def owner(self, host, pid, tree_root=None):
        """(owner_pid, via) for the session keyed on `pid`.

        `self`      pid is itself an agent
        `ancestor`  walked up to the nearest agent
        `parent`    the walk reached a process the index never saw, directly
                    above one its agent starts itself (roles.spawned_by_agent),
                    so that process is the agent
        `tree_root` the walk left what the index knows; the top-most agent
                    ancestor the collector recorded is used instead
        `root`      not an agent, with nothing above it to fold into
        `unknown`   pid was never seen, so nothing says what it is -- it may
                    still be running -- and it is left as its own session
        """
        e = self.entries.get((host, pid))
        if e is None:
            return pid, "unknown"
        if e[1] == "agent":
            return pid, "self"
        seen = {pid}
        below, p = e, e[0]
        for _ in range(MAX_HOPS):
            if p in _NO_PARENT or p in seen:
                break
            seen.add(p)
            a = self.entries.get((host, p))
            if a is not None and users_conflict(e[5], a[5]):
                break                   # a reused pid, not this process's parent
            if a is None:
                if below[2]:
                    return p, "parent"
                break
            if a[1] == "agent":
                return p, "ancestor"
            below, p = a, a[0]
        if tree_root is not None and tree_root != pid:
            return tree_root, "tree_root"
        return pid, "root"

    def result(self):
        return {"entries": len(self.entries), "cap": self.cap, "evicted": self.evicted,
                "wanted": len(self.wanted), "wanted_full": self.wanted_full}


def users_conflict(a, b):
    """Two known, different, non-root users: one cannot have started the other."""
    return bool(a and b and a != b and "root" not in (a, b))


def _start(rec):
    """When the process started: an exit's duration or a residency tick's age.

    A `conn` record's `age_s` is no use here: it is the socket's age.
    """
    ev = rec.get("event")
    e = rec.get("_ts_epoch")
    d = rec.get("duration_s") if ev in ("exit", "truncated") else \
        rec.get("age_s") if ev == "residency" else None
    if isinstance(e, (int, float)) and isinstance(d, (int, float)):
        return e - d
    return None
