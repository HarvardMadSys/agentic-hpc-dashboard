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
parent's own is its figure less that, never below 0. A child its parent never
waited for exits after it and is never taken off. Only a `process` figure
includes children: a `leader` one is the leader thread's alone (the collector
could not read `signal`), and is its own as it stands. Only an exit joins: a
`truncated` record is a process still running at a collector stop, which no
parent has reaped.

Each reducer that sums bytes holds its own join, over the fields it sums (the
record's names: `rd_mb`, `wr_mb`, `rchar_mb`, `wchar_mb`), and feeds it every
exit it reads, in order. A field the record lacks is None in what `take` gives
back -- not measured, never 0 -- and hands nothing up.

The join crosses hours: a shell that exits in the next hour gives back what the
children it reaped in this one carried. So `reaped` is CARRIED into each hour's
reduction and is not part of the saved hour (`hours`): a reducer's `carry`,
`seed`, `strip` and `track` hand it on, its CARRY_AGED ages it with the rest of
its join state, and its `carry_caps` hold it to REAPED_MAX parents, the oldest
first out, counted (`evicted`).
"""
import collections

from ..procindex import pop_oldest

FIELDS = ("rd_mb", "wr_mb", "rchar_mb", "wchar_mb")
DISK = ("rd_mb", "wr_mb")
REAPED_MAX = 200000         # parents with exited children held for their own exit


class OwnIo:
    """One reducer's join (the module docstring): `take` every exit, in order."""

    __slots__ = ("fields", "reaped", "evicted", "touched")

    def __init__(self, fields=FIELDS):
        self.fields = tuple(fields)
        # (host, pid) -> [MB per field] its exited children carried, for its own
        # exit to give back
        self.reaped = collections.OrderedDict()
        self.evicted = 0
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
        host = rec.get("host")
        kids = self.reaped.pop((host, rec.get("pid")), None)
        ppid = rec.get("ppid")
        if ppid is not None and any(vals):
            key = (host, ppid)
            acc = self.reaped.get(key)
            if acc is None:
                if len(self.reaped) >= REAPED_MAX:
                    pop_oldest(self.reaped)
                    self.evicted += 1
                acc = self.reaped[key] = [0.0] * len(vals)
            for i, v in enumerate(vals):
                if v:
                    acc[i] += v
            if self.touched is not None:
                self.touched.add(key)
        if kids:
            vals = tuple(v if v is None else max(0.0, v - k) for v, k in zip(vals, kids))
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
        kids = self.reaped.get((rec.get("host"), rec.get("pid")))
        if kids:
            vals = tuple(v if v is None else max(0.0, v - k) for v, k in zip(vals, kids))
        return vals

    # ------------------------------------------- carried across hours (hours.py)
    def track(self):
        self.touched = set()

    def carry(self, c):
        """`c`, a reducer's carry, with this join's state in it."""
        c["reaped"] = self.reaped
        c.setdefault("touched", {})["reaped"] = self.touched or ()
        return c

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

    def status(self):
        """What a panel says of its join: parents dropped at the cap, whose
        reaped children's bytes then count again at their exit."""
        return {"evicted": self.evicted}
