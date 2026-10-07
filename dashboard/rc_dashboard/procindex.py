"""Who each classifier-tagged process was and who started it, for one window.

A session key is the pid of the nearest process the collector's classifier
tagged (`agent_pid`), and most tagged processes are not agents -- see `roles`.
Folding a key back into its session means walking up from that process to the
nearest one whose role is `agent`, and the record that names the key cannot do
that: its own fields give the tagged ancestor and the top of the tree, not the
chain between. This index holds the chain's links.

Only tagged processes are held (`_role` is set by the normaliser on those
alone), which keeps it to a few thousand entries a day on a login node rather
than one per exit. An untagged process between a helper and its agent ends the
walk, and the caller falls back to the tree root -- which is exactly the agent
in the common case, a session not nested under any other.

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
        # (host, pid) -> [ppid, role, spawned_by_agent, command, cwd]
        self.entries = {}
        self.evicted = 0
        # Bumped on every change, so a consumer can cache what it derived.
        self.version = 0

    def feed(self, rec):
        role = rec.get("_role")
        pid = rec.get("pid")
        if role is None or pid is None:
            return
        key = (rec.get("host"), pid)
        e = self.entries.get(key)
        if e is not None:
            if e[0] in _NO_PARENT and rec.get("ppid") not in _NO_PARENT:
                e[0] = rec.get("ppid")
                self.version += 1
            if e[4] is None and rec.get("cwd"):
                e[4] = rec["cwd"]
                self.version += 1
            return
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
                             rec.get("_command"), rec.get("cwd")]
        self.version += 1

    def role(self, host, pid):
        e = self.entries.get((host, pid))
        return e[1] if e else None

    def get(self, host, pid):
        """(ppid, role, command, cwd), or None if the process was never seen."""
        e = self.entries.get((host, pid))
        return (e[0], e[1], e[3], e[4]) if e else None

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
        return {"entries": len(self.entries), "cap": self.cap, "evicted": self.evicted}
