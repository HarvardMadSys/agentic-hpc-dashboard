"""Each process's own disk I/O: its figure less what its reaped children carried.

The collector's `io` on an exit record is the kernel's `task->ioac +
task->signal->ioac` (`scope: process`), and the kernel adds into
`signal->ioac` the totals of every child the process reaped
(`wait_task_zombie`), and of its threads that died before it. So a shell's
figure holds its commands', an agent's every tool call's, and a login shell's
the agent it started: in the rtx6000a feed a parent's read+write covered its
exited children's in 132 of 133 families. Summed as they come, a command's
bytes count once per process above it: a `cat` that read 1000 MB under `zsh`
under `claude`, started from a login `bash`, was 4011 MB of its user's.

`OwnIo` is the join that takes them back off, one implementation for every
figure that sums disk bytes. A child exits before the parent that reaps it, so
by the parent's exit every child it reaped has handed its WHOLE figure -- its
own and what it reaped in turn -- to `reaped`, keyed (host, parent pid); the
parent's own is its figure less that, never below 0. Only a `process` figure
includes children: a `leader` one is the leader thread's alone (the collector
could not read `signal`), and is its own as it stands. Only an exit joins: a
`truncated` record is a process still running at a collector stop, which no
parent has reaped.

A child its parent never waited for -- one that outlived it -- exits after it,
and still names it: the collector keeps the parent a process was forked by
(`proc_add`). Its figure is left under a pid that is free, and that a later
process may take. So what is held for a pid is kept by the children's user,
with the earliest start among them, and a process takes off only what it can
have reaped: not another user's children (root aside, `users_conflict`), and
not children that began before it did. Its recorded start is its last exec,
which can come after children it forked first -- a login shell's profile, a
script's setup before `exec` -- so a child may begin up to START_SLACK_S
before it; a pid comes round again only once the kernel has handed out every
other one, minutes at the least on a busy node. What a process does not take
is dropped, and counted (`rejected`): those bytes were counted at the
outliving child's own exit, and the process that reaped it (init, or a
subreaper) never names them.

Each reducer that sums bytes holds its own join, over the fields it sums (the
record's names: `rd_mb`, `wr_mb`, `rchar_mb`, `wchar_mb`), and feeds it every
exit it reads, in order. A field the record lacks is None in what `take` gives
back -- not measured, never 0 -- and hands nothing up.

The join crosses hours: a shell that exits in the next hour gives back what the
children it reaped in this one carried. So `reaped` is CARRIED into each hour's
reduction and is not part of the saved hour (`hours`): a reducer's `carry`,
`seed`, `strip` and `track` hand it on, its CARRY_AGED ages it with the rest of
its join state, and its `carry_caps` hold it to REAPED_MAX parents, the oldest
first out. Every parent dropped is counted (`evicted`): at the reducer's own
cap, and at one pass's when a window puts the nodes' joins together
(`carry_trimmed`); its children's bytes then count again at its exit.
"""
import collections

from ..procindex import pop_oldest, users_conflict

FIELDS = ("rd_mb", "wr_mb", "rchar_mb", "wchar_mb")
DISK = ("rd_mb", "wr_mb")
REAPED_MAX = 200000         # parents with exited children held for their own exit
START_SLACK_S = 60.0        # how long before a process's recorded start its child may begin

# What is held for a pid: a group per user of its children, each
# [user, earliest start (None: none known), MB per field...].
_G_USER, _G_START, _G_VALS = 0, 1, 2


def _start(rec):
    """When an exited process began, by its record, or None."""
    e, d = rec.get("_ts_epoch"), rec.get("duration_s")
    if isinstance(e, (int, float)) and isinstance(d, (int, float)) and d >= 0:
        return e - d
    return None


class OwnIo:
    """One reducer's join (the module docstring): `take` every exit, in order."""

    __slots__ = ("fields", "reaped", "evicted", "rejected", "touched")

    def __init__(self, fields=FIELDS):
        self.fields = tuple(fields)
        # (host, pid) -> what its exited children carried, for its own exit to
        # give back: a group per user (_G_*)
        self.reaped = collections.OrderedDict()
        self.evicted = 0            # parents dropped at either cap (`carry_trimmed`)
        self.rejected = 0           # groups a process could not have reaped: dropped
        self.touched = None         # the keys touched since `track`, for the ageing

    def take(self, rec):
        """`rec`'s own figures, one per field (None where it carries none), or
        None when it carries no `io` block. Its whole figure goes to its parent."""
        io = rec.get("io")
        if not isinstance(io, dict):
            return None
        vals = tuple(io.get(f) for f in self.fields)
        if io.get("scope") != "process":
            return vals
        got = self.pop_reaped(rec)
        self.give(rec, vals)
        if got:
            vals = tuple(v if v is None else max(0.0, v - k) for v, k in zip(vals, got))
        return vals

    def peek(self, rec):
        """A process still running at a collector stop: its `truncated` figures
        (read from /proc, which holds the children it reaped so far, as an
        exit's does) less what those children carried. They stay held for its
        exit, and nothing is handed up: it has not exited. Only the inspector
        draws such a process (proctree.py); no sum takes one."""
        io = rec.get("io")
        if not isinstance(io, dict):
            return None
        vals = tuple(io.get(f) for f in self.fields)
        if io.get("scope") not in ("process", "proc"):
            return vals
        groups = self.reaped.get((rec.get("host"), rec.get("pid")))
        got = self._reaped_by(rec, groups, count=False) if groups else None
        if got:
            vals = tuple(v if v is None else max(0.0, v - k) for v, k in zip(vals, got))
        return vals

    def give(self, rec, vals=None):
        """Hand an exit's whole figure (`vals`, else its record's) to its parent's
        pid, with its user and start; a `leader` or unread one gives nothing."""
        ppid = rec.get("ppid")
        if vals is None:
            io = rec.get("io")
            if not isinstance(io, dict) or io.get("scope") != "process":
                return
            vals = tuple(io.get(f) for f in self.fields)
        if ppid is None or not any(vals):
            return
        key = (rec.get("host"), ppid)
        groups = self.reaped.get(key)
        if groups is None:
            if len(self.reaped) >= REAPED_MAX:
                pop_oldest(self.reaped)
                self.evicted += 1
            groups = self.reaped[key] = []
        user, st = rec.get("user") or None, _start(rec)
        for g in groups:
            if g[_G_USER] == user:
                break
        else:
            g = [user, st] + [0.0] * len(vals)
            groups.append(g)
        if st is not None and (g[_G_START] is None or st < g[_G_START]):
            g[_G_START] = st
        for i, v in enumerate(vals, _G_VALS):
            if v:
                g[i] += v
        if self.touched is not None:
            self.touched.add(key)

    def pop_reaped(self, rec):
        """What is held for `rec`'s pid that it can have reaped, summed per field
        (None when nothing), the rest dropped (the module docstring)."""
        groups = self.reaped.pop((rec.get("host"), rec.get("pid")), None)
        return self._reaped_by(rec, groups) if groups else None

    def forget_outlived(self, host, pid, end):
        """A process with this pid that ended at `end`, met after the children
        held for its pid (the live tier's backfill, newest first): those that
        began before it ended outlived it, and no one reaped them here."""
        groups = self.reaped.get((host, pid))
        if not groups or end is None:
            return
        keep = [g for g in groups if g[_G_START] is None or g[_G_START] > end + 1]
        self.rejected += len(groups) - len(keep)
        if keep:
            groups[:] = keep
        else:
            del self.reaped[(host, pid)]

    def _reaped_by(self, rec, groups, count=True):
        user, st = rec.get("user") or None, _start(rec)
        out = None
        for g in groups:
            if users_conflict(user, g[_G_USER]) or (
                    st is not None and g[_G_START] is not None
                    and g[_G_START] < st - START_SLACK_S):
                if count:
                    self.rejected += 1
                continue
            vals = g[_G_VALS:]
            out = vals if out is None else [a + b for a, b in zip(out, vals)]
        return out

    # ------------------------------------------- carried across hours (hours.py)
    def track(self):
        self.touched = set()

    def carry(self, c):
        """`c`, a reducer's carry, with this join's state in it."""
        c["reaped"] = self.reaped
        c.setdefault("touched", {})["reaped"] = self.touched or ()
        return c

    def carry_caps(self):
        """What one pass holds `reaped` to (`hours.combine_carries`)."""
        return {"reaped": REAPED_MAX}

    def carry_trimmed(self, c, dropped):
        """The parents `hours.combine_carries` dropped past that cap, putting the
        nodes' joins together for a window: counted, as the reducer's own cap
        counts what it drops."""
        self.evicted += len(dropped.get("reaped", ()))

    def seed(self, c):
        """Take the join's state from `c`, a reducer's carry."""
        x = c.get("reaped")
        self.reaped = (x if isinstance(x, collections.OrderedDict)
                       else collections.OrderedDict(x or ()))

    def strip(self):
        self.reaped = collections.OrderedDict()
        self.touched = None

    def merge(self, o):
        """A LATER state's counts: the map itself is carried, never merged."""
        self.evicted += o.evicted
        self.rejected += o.rejected

    def status(self):
        """What a panel says of its join: parents dropped at a cap, whose reaped
        children's bytes then count again at their exit; and groups of children a
        process could not have reaped, not taken off it."""
        return {"evicted": self.evicted, "rejected": self.rejected}
