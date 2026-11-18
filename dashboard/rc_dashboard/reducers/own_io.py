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
process may take. A child cannot begin before its parent did, and an earlier
holder's orphan began before that holder exited, so before the next holder
began. So what is held for a pid is kept by the children's user and when they
began (`_entry_for`), and a process takes off only what it can have reaped:
not another user's children (root aside, `users_conflict`), and not children
that began before it did. Its recorded start is its last exec, which can come
after children it forked first -- a login shell's profile, a script's setup
before `exec` -- so a child may begin up to START_SLACK_S before it; a pid
comes round again only once the kernel has handed out every other one,
minutes at the least on a busy node. What a process does not take is
dropped, and counted (`rejected`): those bytes were counted at the outliving
child's own exit, and the process that reaped it (init, or a subreaper) never
names them. Left ambiguous: an orphan that began within START_SLACK_S of the
next holder's start is taken off that holder, and past PER_PID entries for
one pid the closest two merge, so an orphan whose start is nearer a holder's
child than any two of its children are to each other goes with them.

A known limit: the record has no fork time apart from that exec (the
collector writes the exec's when it saw one, the fork's only when it did not:
`ebpf_trace._duration_and_start`, `duration_src`). So a launcher that forks a
child, runs on for over START_SLACK_S and only then execs -- a wrapper that
starts a helper early and `exec`s the real program late -- is refused that
child: its bytes stay in the launcher's figure as well as at the child's own
exit, and the refusal is counted in `rejected`, which is the signal (the live
tier's backfill, which meets the launcher first, holds the child as
`waiting`). phase_log.py measured the gap on four million records: of 2.7 M
children, 0.62% began a second to a minute before their parent's recorded
start, and one a minute to an hour before.

Each reducer that sums bytes holds its own join, over the fields it sums (the
record's names: `rd_mb`, `wr_mb`, `rchar_mb`, `wchar_mb`), and feeds it every
exit it reads, in order. A field the record lacks is None in what `take` gives
back -- not measured, never 0 -- and hands nothing up.

The join crosses hours: a shell that exits in the next hour gives back what the
children it reaped in this one carried. So `reaped` is CARRIED into each hour's
reduction and is not part of the saved hour (`hours`): a reducer's `carry`,
`seed`, `strip` and `track` hand it on, its CARRY_AGED ages it with the rest of
its join state, and its `carry_caps` hold it to the join's cap. Every parent
dropped is counted (`evicted`): at the reducer's own cap, and at one pass's
when a window puts the nodes' joins together (`carry_trimmed`); its children's
bytes then count again at its exit. And said on stderr, naming the panel and
the setting, the first time and then at most hourly (`_say`). So is a figure
short of what its children carried counted (`clamped`): shown as 0, never
below.

The cap is a setting, `reducers.own_io.max_parents` (`cap_of`; REAPED_MAX
unless set), because a service holds some 35 joins at once -- five in each
held window, five carried in the hours' chain, the live tier's two, an
inspector read's and a lookup's -- and each holds its parents apart. A parent
costs about 0.5 KB with one entry and up to 2.5 KB with PER_PID: the default
is some 39 MB over all of them at one entry a parent, 159 MB at worst
(README, "Disk I/O is each process's own"). The parent given to least
recently goes first: a dead one whose orphans' bytes wait under its pid, or
one that has stopped reaping, before one at work. Passed over, looking
RUNTIME_PASS deep, is what is held for an agent runtime or for the process
that started one (`_runtime`, by the collector's `tree_root_pid`): an agent
sits idle as long as its reader does while other parents reap, and its tool
calls' bytes, or a whole session's, are the most a parent holds. The hours'
chain still ages it out after `hours.CARRY_KEEP_H` untouched.
"""
import collections
import itertools
import sys
import time

from ..procindex import pop_oldest, users_conflict

FIELDS = ("rd_mb", "wr_mb", "rchar_mb", "wchar_mb")
DISK = ("rd_mb", "wr_mb")
REAPED_MAX = 2000           # parents held for their own exit, unless the setting says (`cap_of`)
START_SLACK_S = 60.0        # how long before a process's recorded start its child may begin
# A figure short of what its children carried by more than the collector's
# rounding (MB to three places, per child) is counted when it is shown as 0.
CLAMP_TOL_MB = 0.01

# What is held for a pid: its children's figures, an entry per user and stretch
# of starts no wider than START_SLACK_S (one with no start known apart), each
# [user, earliest start (None: none known), MB per field...]; at most PER_PID
# of them, the two of one user whose starts are closest merged past it.
_G_USER, _G_START, _G_VALS = 0, 1, 2
PER_PID = 8

# What is held for an agent runtime or what started one (`_runtime`) is passed
# over at the cap, looking this deep for another to drop (`pop_least_recent`).
RUNTIME_PASS = 64


class AgentHeld(list):
    """What is held for a pid that is an agent runtime, or started one."""
    __slots__ = ()


def _runtime(rec):
    """Whether `rec`'s parent is an agent runtime (`rec` one of its tool calls)
    or started one (`rec` the runtime itself): the root of an agent's tree,
    `tree_root_pid`, which every schema puts on each process in it, where
    schema 7 gave each tool call an `agent_pid` of its own."""
    r = rec.get("tree_root_pid")
    return r is not None and (rec.get("ppid") == r or rec.get("pid") == r)


def pop_least_recent(d):
    """Drop the least recently given of `d`, an OrderedDict kept in that order,
    that is not held for an agent runtime, looking RUNTIME_PASS deep; past
    that, the least recently given of all."""
    for k, v in itertools.islice(d.items(), RUNTIME_PASS):
        if type(v) is not AgentHeld:
            del d[k]
            return
    pop_oldest(d)

# A join that drops a parent at its cap says so on stderr, naming its panel and
# the setting: the first time, then at most once in SAY_EVERY_S for that panel
# (`OwnIo._say`). Otherwise only the panel's `own_io.evicted` shows it.
SAY_EVERY_S = 3600.0
_SAID = {}                  # panel -> when it last said so (`_now`)


def _now():
    return time.monotonic()


def cap_of(cfg):
    """The parents one join holds: `reducers.own_io.max_parents`, REAPED_MAX
    when unset or null. Below 1 is refused, here as at config load: 0 is
    neither "unbounded" nor "no join"."""
    r = (cfg.get("reducers.own_io", {}) if cfg is not None else None) or {}
    v = r.get("max_parents")
    return REAPED_MAX if v is None else _count(v)


def _count(v):
    if type(v) is not int or v < 1:
        raise ValueError("reducers.own_io.max_parents: %r is not a count of parents, "
                         "1 or more" % (v,))
    return v


def _entry_for(held, user, st):
    """The entry a child of `user` begun at `st` joins: its user's whose
    earliest start is closest to it and within START_SLACK_S, or None."""
    best, gap = None, None
    for g in held:
        if g[_G_USER] != user:
            continue
        gs = g[_G_START]
        if st is None or gs is None:
            if st is None and gs is None:
                return g
            continue
        d = abs(gs - st)
        if d <= START_SLACK_S and (gap is None or d < gap):
            best, gap = g, d
    return best


def _merge_closest(held):
    """Merge the two entries of one user whose starts are closest: an earlier
    holder's orphan began minutes before the next holder's children at the
    least, and one holder's children often seconds apart. False when no two
    entries can merge."""
    pair, gap = None, None
    for i, a in enumerate(held):
        for b in held[i + 1:]:
            if a[_G_USER] != b[_G_USER] or a[_G_START] is None or b[_G_START] is None:
                continue
            d = abs(a[_G_START] - b[_G_START])
            if gap is None or d < gap:
                pair, gap = (a, b), d
    if pair is None:
        return False
    a, b = pair
    a[_G_START] = min(a[_G_START], b[_G_START])
    for i in range(_G_VALS, len(a)):
        a[i] += b[i]
    _drop(held, b)
    return True


def _drop(held, g):
    """`g` out of `held`, by identity: two entries can be equal lists."""
    del held[next(i for i, x in enumerate(held) if x is g)]


def _entry_age(g):
    """For dropping one entry when none can merge: the earliest-begun first."""
    return (g[_G_START] is not None, g[_G_START] or 0.0)


def _start(rec):
    """When an exited process began, by its record, or None."""
    e, d = rec.get("_ts_epoch"), rec.get("duration_s")
    if isinstance(e, (int, float)) and isinstance(d, (int, float)) and d >= 0:
        return e - d
    return None


class OwnIo:
    """One reducer's join (the module docstring): `take` every exit, in order."""

    __slots__ = ("fields", "cap", "name", "reaped", "evicted", "rejected", "clamped",
                 "touched")

    def __init__(self, fields=FIELDS, cap=None, name="own_io"):
        self.fields = tuple(fields)
        self.cap = REAPED_MAX if cap is None else _count(cap)
        self.name = name            # the panel it serves, for what it says (`_say`)
        # (host, pid) -> what its exited children carried, for its own exit to
        # give back (_G_* entries), the one given to least recently first
        self.reaped = collections.OrderedDict()
        self.evicted = 0            # parents dropped at either cap (`carry_trimmed`)
        self.rejected = 0           # groups a process could not have reaped: dropped
        self.clamped = 0            # processes whose figure fell short of their children's
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
        return self.less(vals, got) if got else vals

    def less(self, vals, got, count=True):
        """`vals` less what the children carried (`got`), field by field: never
        below 0, and a figure short by more than rounding is counted."""
        out, short = [], False
        for v, k in zip(vals, got):
            if v is None:
                out.append(None)
                continue
            short = short or k - v > CLAMP_TOL_MB
            out.append(max(0.0, v - k))
        if short and count:
            self.clamped += 1
        return tuple(out)

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
        return self.less(vals, got, count=False) if got else vals

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
        held = self.reaped.get(key)
        if held is None:
            if len(self.reaped) >= self.cap:
                pop_least_recent(self.reaped)
                self.evicted += 1
                self._say(1)
            held = self.reaped[key] = AgentHeld() if _runtime(rec) else []
        else:
            self.reaped.move_to_end(key)
            if type(held) is list and _runtime(rec):
                held = self.reaped[key] = AgentHeld(held)
        user, st = rec.get("user") or None, _start(rec)
        g = _entry_for(held, user, st)
        if g is None:
            g = [user, st] + [0.0] * len(vals)
            held.append(g)
        elif st is not None and st < g[_G_START]:
            g[_G_START] = st
        for i, v in enumerate(vals, _G_VALS):
            if v:
                g[i] += v
        if len(held) > PER_PID and not _merge_closest(held):
            _drop(held, min(held, key=_entry_age))
            self.evicted += 1
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
        return {"reaped": self.cap}

    def carry_trimmed(self, c, dropped):
        """The parents `hours.combine_carries` dropped past that cap, putting the
        nodes' joins together for a window: what is held for an agent runtime
        is put back in place of the oldest of the rest, and the dropped are
        counted and said, as at the reducer's own cap."""
        gone = dropped.get("reaped") or ()
        d = c.get("reaped")
        back = [kv for kv in gone if type(kv[1]) is AgentHeld]
        if back and isinstance(d, collections.OrderedDict):
            out = [k for k, v in d.items() if type(v) is not AgentHeld][:len(back)]
            for k in out:
                del d[k]
            for k, v in reversed(back[len(back) - len(out):]):
                d[k] = v
                d.move_to_end(k, last=False)
        self.evicted += len(gone)
        if gone:
            self._say(len(gone))

    def _say(self, n):
        """Say on stderr that this join dropped `n` parents at its cap: the first
        time, then at most once in SAY_EVERY_S for its panel."""
        now = _now()
        last = _SAID.get(self.name)
        if last is not None and now - last < SAY_EVERY_S:
            return
        _SAID[self.name] = now
        sys.stderr.write(
            "warning: own_io: %s dropped %d parent%s held for %s own exit, at "
            "reducers.own_io.max_parents (%d), %d so far: what %s reaped children carried "
            "counts again at that exit. Raise the setting while this recurs (said at most "
            "hourly)\n" % (self.name, n, "" if n == 1 else "s", "its" if n == 1 else "their",
                           self.cap, self.evicted, "its" if n == 1 else "their"))
        sys.stderr.flush()

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
        self.clamped += o.clamped

    def status(self):
        """What a panel says of its join: parents dropped at a cap, whose reaped
        children's bytes then count again at their exit; groups of children a
        process could not have reaped, not taken off it; and processes whose
        figure fell short of what their children carried, shown as 0."""
        return {"evicted": self.evicted, "rejected": self.rejected, "clamped": self.clamped}
