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
child than any two of its children are to each other goes with them. When no
two can merge (each another user's) the earliest begun is dropped, counted
apart from the cap's (`crowded`): raising the cap does nothing for it.

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

Nor has the record a reaping event: exit order stands in for one. A child
that ends before its parent but is never waited for by it -- the parent never
calls wait(), so the child is a zombie until the parent's own exit and init
(or a subreaper) reaps it, or the parent ignores SIGCHLD and the kernel reaps
it unasked -- never reaches the parent's `signal->ioac`. The kernel says so on
the parent's own exit: the `wait_task_zombie` that adds a child's I/O to its
reaper's also adds its CPU to the reaper's `cutime`/`cstime`. The eBPF
collector, the only one that writes schema 4 or later, reads the two in ns
at `sched_process_exit` and writes their sum on the `exit` record as
`child_cpu_s`, to 0.1 ms (`ebpf_trace.py`), or null when its `signal` block
is off (`null_if_off`). An exit whose `child_cpu_s` is 0 reaped no child that
ran an exec -- one costs far more than 0.05 ms; a bare fork may not, and does
next to no I/O -- so it takes none off (`reaped_none`): what is held for its
pid is dropped and counted apart (`unreaped`), not with the start rule's
refusals (`rejected`). A coarser figure would not do: /proc's, in clock
ticks, reads 0 for a parent of a few short children. The pre-v4 tracer and
the /proc poller wrote 0 there for "not measured", which normalize.py makes
None (`NULLABLE_V4_ONLY`), and the collector's own /proc reads go on
`truncated` records, which `reaped_none` passes over; a producer that wrote
an exit's from ticks would have this drop children it did reap. One that
reaped some of its children and not others still has them all taken off --
the record does not say which -- and comes out short by the ones it did not
reap: seen where that passes its whole figure (`clamped`), not otherwise.

Each reducer that sums bytes holds its own join, over the fields it sums (the
record's names: `rd_mb`, `wr_mb`, `rchar_mb`, `wchar_mb`), and feeds it every
exit it reads, in order. A field the record lacks is None in what `take` gives
back -- not measured, never 0 -- and hands nothing up.

A join takes off only the children it read, and a filtered view reads only
the records its filter keeps: the scan skips the rest before parsing them
(`windows.ViewFilter`). So the figure is worked out ONCE, by the pass that
reads every record -- the hour chain (hours.py), which reduces each closed
hour over every node in order -- with one more join, the `Ledger`, which puts
each exit's own figure on the record (`OWN`) before any reducer reads it, and
keeps the hour's that are not the record's own `io`: a parent's, less what its
children carried. A reader that does not read every exit -- a filtered view's
build, a window's edge -- puts them back on the records of the hours they
cover (`hours.OwnFigures`), and `take` gives a record that carries one that
figure. The rest -- an exit of the hour still open, a record fed live, a feed
with no hour chain -- the join works out as above, from what it read.

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
unless set), because a service holds some 36 joins at once -- five in each
held window, six carried in the hours' chain (the reducers' and the
`Ledger`'s), the live tier's two, an inspector read's and a lookup's -- and
each holds its parents apart. A parent
costs about 0.5 KB with one entry and up to 2.5 KB with PER_PID: the default
is some 40 MB over all of them at one entry a parent, 164 MB at worst
(README, "Disk I/O is each process's own"). The parent given to least
recently goes first: a dead one whose orphans' bytes wait under its pid, or
one that has stopped reaping, before one at work. What is held for an agent
runtime, or for the process that started one (`_runtime`, by the collector's
`tree_root_pid`), is kept apart and goes before the rest only once such
parents hold over half the cap (`Held`): an agent sits idle as long as its
reader does while other parents reap, and its tool calls' bytes, or a whole
session's, are the most a parent holds; the half is for the ones that never
exit, a login shell that outlives the feed, which would otherwise crowd
every other parent out. Carried, the two are one map, the runtimes last, and
a pass's cap keeps the same share of it (`carry_trimmed`), ranking each kind
by its own hours (`carry_tier`). The hours' chain still ages one out after
`hours.CARRY_KEEP_H` untouched.
"""
import collections
import itertools
import sys
import time

from ..procindex import pop_oldest, users_conflict

FIELDS = ("rd_mb", "wr_mb", "rchar_mb", "wchar_mb")
DISK = ("rd_mb", "wr_mb")
# The key an exit's own figure is put on the record under: field -> MB, None
# where unmeasured, as the record's `io` has them (`Ledger`, hours.OwnFigures).
OWN = "_own"
# The Ledger's key in an hour's reducer set (hours.py), which no reducer's is.
LEDGER = "_own_ledger"
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


def runtime_share(cap):
    """How much of a cap of `cap` runtimes keep while other parents want it:
    half. With fewer others they keep the rest of it, and with none, all."""
    return cap // 2


def reaped_none(rec):
    """Whether the exit `rec` says it reaped no child: its `child_cpu_s`, the
    kernel's `cutime + cstime`, measured and 0 (the module docstring). Only an
    exit's says so: a `truncated` record's is read from /proc in clock ticks.
    None -- the collector's `signal` feature off, or a record from before v4,
    whose 0 placeholder normalize.py makes None -- says nothing."""
    c = rec.get("child_cpu_s")
    return rec.get("event") == "exit" and type(c) in (int, float) and c == 0


class Held:
    """Parents held for their own exit, least recently given first, as two
    OrderedDicts: the rest, and agent runtimes' (an `AgentHeld` value), which
    go first only past their share (`pop_least_recent`): O(1), whatever the
    mix. The dict calls the joins make, on either. Carried as one OrderedDict,
    the runtimes last (`carried`); `Held(items)` splits one again."""

    __slots__ = ("rest", "agents")

    def __init__(self, items=()):
        self.rest = collections.OrderedDict()
        self.agents = collections.OrderedDict()
        for k, v in items:
            self[k] = v

    def _tier(self, key):
        return self.agents if key in self.agents else self.rest

    def __len__(self):
        return len(self.rest) + len(self.agents)

    def __contains__(self, key):
        return key in self.rest or key in self.agents

    def __iter__(self):
        return itertools.chain(self.rest, self.agents)

    def items(self):
        return itertools.chain(self.rest.items(), self.agents.items())

    def get(self, key, default=None):
        return self._tier(key).get(key, default)

    def __getitem__(self, key):
        return self._tier(key)[key]

    def __setitem__(self, key, v):
        """Set `key`, moving it to the other map when its kind changed."""
        tier, other = ((self.agents, self.rest) if type(v) is AgentHeld
                       else (self.rest, self.agents))
        other.pop(key, None)
        tier[key] = v

    def __delitem__(self, key):
        del self._tier(key)[key]

    def pop(self, key, default=None):
        return self._tier(key).pop(key, default)

    def move_to_end(self, key):
        self._tier(key).move_to_end(key)

    def pop_least_recent(self, share):
        """One past the cap, the one just given to included: drop the least
        recently given of the rest, or of the runtimes' when none of the rest is
        left or they hold more than `share`. What stays is what
        `OwnIo.carry_trimmed` keeps of the same parents."""
        pop_oldest(self.agents if not self.rest or len(self.agents) > share else self.rest)

    def carried(self):
        out = collections.OrderedDict(self.rest)
        out.update(self.agents)
        return out

# A join that drops a parent at its cap says so on stderr, naming its panel and
# the setting: the first time, then at most once in SAY_EVERY_S for that panel
# (`OwnIo._say`). Otherwise only the panel's `own_io.evicted` shows it.
SAY_EVERY_S = 3600.0
_SAID = {}                  # panel -> when it last said so (`_now`), in this process


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
                 "crowded", "unreaped", "touched")

    def __init__(self, fields=FIELDS, cap=None, name="own_io"):
        self.fields = tuple(fields)
        self.cap = REAPED_MAX if cap is None else _count(cap)
        self.name = name            # the panel it serves, for what it says (`_say`)
        # (host, pid) -> what its exited children carried, for its own exit to
        # give back (_G_* entries), the one given to least recently first
        self.reaped = Held()
        self.evicted = 0            # parents dropped at either cap (`carry_trimmed`)
        self.rejected = 0           # groups a process could not have reaped: dropped
        self.clamped = 0            # processes whose figure fell short of their children's
        self.crowded = 0            # groups dropped from a pid past PER_PID
        self.unreaped = 0           # groups held for an exit that says it reaped none
        self.touched = None         # the keys touched since `track`, for the ageing

    def take(self, rec):
        """`rec`'s own figures, one per field (None where it carries none), or
        None when it carries no `io` block. Its whole figure goes to its parent.

        One the pass that read every record worked out and put on it (`OWN`)
        is the figure given back. The join still takes what it holds for the
        pid, counting what it refuses, and hands the whole figure up, as it
        would have: what it holds for the records that come without one -- the
        hour still open, the live feed -- is what it was."""
        io = rec.get("io")
        if not isinstance(io, dict):
            return None
        vals = tuple(io.get(f) for f in self.fields)
        if io.get("scope") != "process":
            return vals
        got = self.pop_reaped(rec)
        self.give(rec, vals)
        own = self.less(vals, got) if got else vals
        known = rec.get(OWN)
        return own if known is None else tuple(known.get(f) for f in self.fields)

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
            # counted in before the cap is applied: a runtime past a cap of 1
            # held by another parent is itself the one that goes
            held = self.reaped[key] = AgentHeld() if _runtime(rec) else []
            if len(self.reaped) > self.cap:
                self.reaped.pop_least_recent(runtime_share(self.cap))
                self.evicted += 1
                self._say(1)
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
            self.crowded += 1
        if self.touched is not None:
            self.touched.add(key)

    def pop_reaped(self, rec):
        """What is held for `rec`'s pid that it can have reaped, summed per field
        (None when nothing), the rest dropped (the module docstring). An exit
        that says it reaped no child takes none (`reaped_none`): another
        process, or the kernel, reaped what is held for its pid."""
        groups = self.reaped.pop((rec.get("host"), rec.get("pid")), None)
        if not groups:
            return None
        if reaped_none(rec):
            self.unreaped += len(groups)
            return None
        return self._reaped_by(rec, groups)

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
        """`c`, a reducer's carry, with this join's state in it: one OrderedDict,
        as `hours` ages, combines and saves it."""
        c["reaped"] = self.reaped.carried()
        c.setdefault("touched", {})["reaped"] = self.touched or ()
        return c

    def carry_caps(self):
        """What one pass holds `reaped` to (`hours.combine_carries`)."""
        return {"reaped": self.cap}

    def carry_tier(self, field, value):
        """The run of a carried map `value` is ranked in (`hours.combine_carries`):
        a runtime's apart from the rest, so each kind's age is its own and not
        that of the last parent before it in its node's map."""
        return field == "reaped" and type(value) is AgentHeld

    def carry_trimmed(self, c, dropped):
        """The parents `hours.combine_carries` dropped past that cap, putting the
        nodes' joins together for a window. The cut went by age alone, so what
        stays is chosen again from all it saw by the share rule of the
        reducer's own cap (`Held.pop_least_recent`): runtimes up to
        `runtime_share`, or more where fewer others fill the cap, the others
        the rest, the newest of each kind by its own hours (`carry_tier`), the
        runtimes last. For one node, in the order it gave them, that is what
        its own cap would have kept; across nodes, ranked by the hour each was
        last touched in, it can differ within an hour. The older past that
        stay dropped, counted and said as at the reducer's own cap."""
        gone = dropped.get("reaped") or ()
        d = c.get("reaped")
        if gone and isinstance(d, collections.OrderedDict):
            every = list(gone) + list(d.items())         # least recently given first
            rt = [kv for kv in every if type(kv[1]) is AgentHeld]
            rest = [kv for kv in every if type(kv[1]) is not AgentHeld]
            cap = len(d)
            n_rt = min(len(rt), max(runtime_share(cap), cap - len(rest)))
            d.clear()
            d.update(rest[len(rest) - (cap - n_rt):])
            d.update(rt[len(rt) - n_rt:])
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
        self.reaped = Held((c.get("reaped") or {}).items())

    def strip(self):
        self.reaped = Held()
        self.touched = None

    def merge(self, o):
        """A LATER state's counts: the map itself is carried, never merged."""
        self.evicted += o.evicted
        self.rejected += o.rejected
        self.clamped += o.clamped
        self.crowded += o.crowded
        self.unreaped += o.unreaped

    def status(self):
        """What a panel says of its join: parents dropped at a cap, whose reaped
        children's bytes then count again at their exit; groups of children a
        process could not have reaped, not taken off it; processes whose figure
        fell short of what their children carried, shown as 0; groups
        dropped from a pid holding PER_PID others' already, which also count
        again at its exit; and groups held for a process whose exit says it
        reaped no child (`reaped_none`), not taken off it."""
        return {"evicted": self.evicted, "rejected": self.rejected, "clamped": self.clamped,
                "crowded": self.crowded, "unreaped": self.unreaped}


def figure_key(rec):
    """What names an exit among an hour's figures: its host, pid and time. Two
    exits of one pid on one host are minutes apart at the least (a pid comes
    round again only once every other one has been handed out), and the
    collector's `ts_epoch` is to the microsecond."""
    return rec.get("host"), rec.get("pid"), rec.get("_ts_epoch")


class Ledger:
    """The join of the pass that reads every record, once: the hour chain's
    (hours.py), every exit of a node in order, carried hour to hour.

    It works each exit's own figure out over every field, puts it on the record
    (`OWN`) before any reducer reads it -- it is first in the hour's reducer
    set, under LEDGER -- so every panel the hour holds takes the same one, and
    keeps the hour's that differ from the record's own `io`: a parent's, less
    what its reaped children carried. Kept by `part(rec)` (the record's user
    and agent type, hours.own_part), so a filtered view loads the figures of the
    records it keeps and no more (`hours.OwnFigures`). An exit not kept took
    nothing off: its own figure is its `io` as it stands.

    Its join is carried, aged and capped as a reducer's is (`CARRY_AGED`,
    `carry_caps`, ...): the hour chain does the same with it as with theirs. It
    is never part of a saved hour: hours.py takes it out of the set first."""

    EVENTS = ("exit",)
    CARRY_AGED = ("reaped",)

    def __init__(self, cap=None, part=None):
        self.join = OwnIo(FIELDS, cap, "own figures")
        self.part = part
        self.figures = {}       # part -> {figure_key: own figure, in FIELDS order}
        self.kept = 0

    def feed(self, rec):
        own = self.join.take(rec)
        if own is None:
            return
        rec[OWN] = dict(zip(FIELDS, own))
        io = rec["io"]
        key = figure_key(rec)
        if key[2] is None or own == tuple(io.get(f) for f in FIELDS):
            return
        part = self.part(rec) if self.part is not None else None
        got = self.figures.get(part)
        if got is None:
            got = self.figures[part] = {}
        got[key] = own
        self.kept += 1

    def take_figures(self):
        """The figures kept since the last call, by part; none kept after it."""
        out, self.figures = self.figures, {}
        return out

    def track(self):
        self.join.track()

    def carry(self):
        return self.join.carry({})

    def carry_caps(self):
        return self.join.carry_caps()

    def carry_trimmed(self, c, dropped):
        self.join.carry_trimmed(c, dropped)

    def carry_tier(self, field, value):
        return self.join.carry_tier(field, value)

    def seed(self, c):
        self.join.seed(c)

    def strip(self):
        self.join.strip()
