"""One trajectory as a process tree: what the Trajectories inspector draws.

A ranked row is a run-length summary, and that is all a reducer can afford to
hold: the busiest session of one day on rtx6000a ran 1.59 M processes. So the
tree is not held anywhere. It is re-read on demand, for one session, from the
window's day-files, through the same Normalizer-based scan a window build uses
(`export.window_scan`), and every bound on that read is reported in the
response -- a tree that stopped early says where, and a process that was
counted but not itemised is still counted.

Which records are the session's is the fold's answer, not a new one: a row
folds collector session keys into the agent they resolve to (trajectories.py),
and the inspector reads exactly the records whose `_sid` is one of those keys.
The day-file is searched for those keys as bytes first (`key_scan`), so only
the session's own lines are ever parsed: a small session in a multi-GB
day-file costs a search, not a parse, and a big one is not slowed by the
other sessions' lines around it.

Parent links come from the records' own `ppid` wherever they can: a parent's
exit is written after its children's, so each exit claims the children already
waiting for it. What is left is placed on weaker evidence, and each process
says which (`link`):

  ppid    its own record names this parent
  index   its parent left no record in the read; the window's ProcIndex or the
          agent's residency ticks say who that parent is
  depth   the collector's `depth` below the session key puts it there; any
          levels in between were never recorded (`gap`)
  fold    attached to the agent because the fold put its key there -- nothing
          read links it to a process

A process the records name as a parent but that left no exit record in the read
-- the agent still running, a shell that exited after the span -- is drawn from
what the index and the ticks know, and says it has no exit (`ev` null). A
process whose record names init outlived its parent and was reparented; it is
placed under its session key by depth and flagged `orphan`.

Readability is the other half. A Claude Code session is hundreds of tool calls;
a poll loop is the same `pgrep` a thousand times. Consecutive siblings of the
same SHAPE -- the command, and the commands it ran -- fold into one group:
`pgrep x120`, or `pgrep > sleep x461` for an alternating loop. A group carries
its members' totals, their exit codes, their distinct command lines, and the
first few members in full.

Every command line served from here is masked (`redact.redact`) BEFORE it is
cut to its preview length: masking a token needs the text around it, and a cut
taken first could leave half a token that no rule recognises any more.

The page navigates the tree by time, so the read also keeps what it needs for
that from EVERY record it matched, itemised or only counted (`nav`): process
starts per time bin by purpose, the busiest minute, the first tool call, the
longest process and the failures. A navigator drawn from the served rows alone
would show the itemised few and hide the counted many.

Which end of a long session is drawn is the reader's choice (`order`). Each of
the read's budgets keeps one end and counts the other -- the records read, the
processes itemised, the entries served -- and by default all three keep the
OLDEST, because that is the order the records arrive in. Newest first moves all
three. The read goes on to the session's end: the hold keeps no more drawn
however much is read, so its record budget bounds time alone. A process that
finds the hold full, or its command's quota under one parent, is made room for
by counting the oldest drawn one under that one's parent, rather than being
counted itself. And siblings are listed, grouped, and served newest first, so
the entry budget cuts the deep end of the oldest subtrees. Sorting the served
rows alone would put at the top the very processes the budgets had counted
rather than drawn.
"""
import collections
import gzip
import heapq
import json
import math
import mmap
import os
import re
import threading
import time
from array import array

from . import feeds as feedmod, fastjson
from . import gz
from .export import _window_stream, event_filter, window_plan, window_scan
from .normalize import Normalizer, epoch_of
from .procindex import VIA
from .redact import redact
from .reducers.trajectories import fold as fold_sessions

NO_PARENT = (None, 0, 1)
EVENTS = ("exit", "truncated", "residency")
PREVIEW = 220            # characters of a command line shown, as reducers.who()
RAW_KEEP = 1024          # characters held per command line until it is masked
MAX_PERIOD = 4           # the longest repeating motif looked for among siblings
MIN_REPS = 3             # a run shorter than this reads fine as plain rows
GROUP_ITEMS = 6          # members of a group itemised in full
SAMPLES = 6              # distinct command lines listed per group
CMD_KEEP = 12            # distinct commands counted per not-itemised bucket
TICK_KEEP = 5000         # processes whose residency ticks are remembered
MAX_STUBS = 2000         # parents drawn without a record of their own
# Built trees are kept and served again: going back to a session is the common
# move, and a re-read is seconds. Bounded three ways -- age, count, bytes -- and
# keyed on the window's install time, so a rebuilt window is a fresh read.
CACHE_TTL = 600          # seconds a built tree is served again
CACHE_KEEP = 16          # trees held
CACHE_BYTES = 96 << 20   # their serialised size, in all
CHUNK = 32 << 20         # bytes of day-file searched for the keys at a time
UNTIL_SLACK = 60         # seconds read past `until`, for lines written out of order
# The navigator: process starts binned at the finest of these widths (seconds)
# that keeps the read's span within DENSITY_BINS bins.
BIN_WIDTHS = (0.1, 0.2, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600,
              7200, 21600, 43200, 86400)
DENSITY_BINS = 2400
FAIL_KEEP = 2000         # failures listed by time; past it, counted
TOOL_ROLES = ("tool-shell", "sandbox", "embedded-tool")
ORDERS = ("oldest", "newest")   # how siblings are listed, and which end is drawn
COMPACT_SLACK = 4096     # stale entries a newest-first queue holds before a sweep
WARM_TOP = 3             # sessions of the default ranking kept read in the background
WARM_EVERY = 30          # seconds between the warmer's looks

DEFAULTS = {
    # Matched exit records read before the scan stops and says where. At ~25 us a
    # record through json + the normaliser this is several seconds of work.
    "inspect_max_records": 150000,
    # The same for a newest-first read, which reads on to the session's end and
    # keeps only the newest `inspect_hold` drawn: memory is bounded however
    # much it reads, so this bounds time alone -- tens of seconds at the top.
    "inspect_newest_records": 600000,
    # Bytes of day-file read before the scan stops, likewise.
    "inspect_max_bytes": 6 << 30,
    # Processes itemised; past it a process with nothing below it is counted
    # under its parent instead (`k: "agg"`), and a parent at 1.5x it too.
    "inspect_hold": 20000,
    # Childless processes of one command itemised under any one parent. A poll
    # loop is the same command a hundred thousand times, and a group shows six
    # of them anyway: itemising the rest would spend the whole hold budget on
    # one loop and leave every later tool call counted rather than drawn. Per
    # command, so an agent's own ~2 000 `git status` polls a day do not push
    # its other direct children out of the drawing.
    "inspect_per_parent": 2000,
    # Entries in the served tree, allotted breadth first, so the top of the tree
    # is whole and what is cut is the deep end (`k: "more"`). ~400 bytes each
    # as JSON, a tenth of that gzipped (`response`).
    "inspect_nodes": 6000,
}

# Claude Code runs every Bash tool call as `zsh -c source <snapshot> && ...
# && eval '<the command>' < /dev/null && pwd -P >| <cwd file>`; the first 300
# characters are the same boilerplate every time, the command is inside eval.
_EVAL = re.compile(r"\beval '((?:[^']|'\\'')*)'")


def ran(role, args):
    """What a wrapper ran, when its argv is mostly the wrapper, else None.

    Not only the tagged tool shell: a subshell it forks inherits its argv
    (procindex.py), so it carries the same snapshot boilerplate.
    """
    if not args:
        return None
    if role == "tool-shell" or "/.claude/shell-snapshots/" in args:
        m = _EVAL.search(args)
        if m:
            return m.group(1).replace("'\\''", "'")
    if role == "sandbox":
        i = args.rfind(" -- ")
        if i >= 0:
            return args[i + 4:]
    return None


def shown(text, memo=None):
    """A command line as served: masked whole, then cut.

    `memo` is one render's: a tool call's command line recurs hundreds of times
    in a session, and masking is most of a render's time. Held for that render
    only, never across requests -- its keys are the unmasked lines.
    """
    if not text:
        return None
    if memo is not None:
        hit = memo.get(text)
        if hit is not None:
            return hit
    out = redact(text)
    out = out if len(out) <= PREVIEW else out[:PREVIEW - 1] + "…"
    if memo is not None:
        memo[text] = out
    return out


def _r(v, nd=3):
    return round(v, nd) if isinstance(v, (int, float)) else None


def _add(a, b):
    """Sum where None means unmeasured, not zero."""
    return b if a is None else a if b is None else a + b


# A process's I/O, from its exit record: disk bytes, syscall bytes (pipes and
# terminals included) and network bytes, in MB. None when the record carries
# none of them -- unmeasured, as `cpu` None is -- and summed up a subtree the
# way `sub_cpu` is, so a session's tree says which commands did the I/O.
IO_KEYS = ("rd_mb", "wr_mb", "rchar_mb", "wchar_mb", "tx_mb", "rx_mb")


def _io_of(rec):
    io = rec.get("io") or {}
    tx, rx = rec.get("net_tx_bytes"), rec.get("net_rx_bytes")
    if io.get("rchar_mb") is None and io.get("rd_mb") is None and tx is None and rx is None:
        return None
    return (io.get("rd_mb") or 0.0, io.get("wr_mb") or 0.0, io.get("rchar_mb") or 0.0,
            io.get("wchar_mb") or 0.0, (tx or 0) / 1e6, (rx or 0) / 1e6)


def _io_add(a, b):
    return b if a is None else a if b is None else tuple(x + y for x, y in zip(a, b))


def _io_out(v):
    """Served only where something moved: a row of zeros says nothing."""
    if v is None or not any(v):
        return None
    return {k: round(x, 3) for k, x in zip(IO_KEYS, v)}


class Agg:
    """Processes counted under one parent but not itemised."""

    __slots__ = ("n", "cpu", "cpu_nm", "io", "t0", "t1", "cmds", "kv", "depth")

    def __init__(self, kv=None, depth=None):
        self.n, self.cpu, self.cpu_nm = 0, None, 0
        self.io = None
        self.t0 = self.t1 = None
        self.cmds = collections.Counter()
        self.kv, self.depth = kv, depth

    def _span(self, t0, t1):
        for t in (t0, t1):
            if t is not None:
                self.t0 = t if self.t0 is None else min(self.t0, t)
                self.t1 = t if self.t1 is None else max(self.t1, t)

    def _cmd(self, cmd, n):
        cmd = cmd or "?"
        self.cmds[cmd if cmd in self.cmds or len(self.cmds) < CMD_KEEP else "(other)"] += n

    def add(self, cmd, cpu, t0, t1, io=None):
        """One process's own record."""
        self.n += 1
        if cpu is None:
            self.cpu_nm += 1
        else:
            self.cpu = _add(self.cpu, cpu)
        self.io = _io_add(self.io, io)
        self._span(t0, t1)
        self._cmd(cmd, 1)

    def take(self, p):
        """An itemised subtree's totals, as one more counted sibling."""
        self.n += p.n
        self.cpu = _add(self.cpu, p.sub_cpu)
        self.cpu_nm += p.cpu_nm
        self.io = _io_add(self.io, p.sub_io)
        self._span(p.t0, p.t1)
        self._cmd(p.label, 1)

    def merge(self, o):
        self.n += o.n
        self.cpu = _add(self.cpu, o.cpu)
        self.cpu_nm += o.cpu_nm
        self.io = _io_add(self.io, o.io)
        self._span(o.t0, o.t1)
        for c, k in o.cmds.items():
            self._cmd(c, k)

    def absorb(self, p):
        """A whole itemised subtree, counted here instead. Returns how many."""
        n, stack = 0, [p]
        while stack:
            q = stack.pop()
            if q.ev is not None:
                self.add(q.cmd, q.cpu, q.start, q.end, q.io)
                n += 1
            if q.agg is not None:
                self.merge(q.agg)
            stack.extend(q.kids)
        return n

    def as_dict(self, why):
        return {"k": "agg", "why": why, "n": self.n, "sub_cpu": _r(self.cpu),
                "cpu_nm": self.cpu_nm or None, "sub_io": _io_out(self.io),
                "start": _r(self.t0), "end": _r(self.t1),
                "cmds": [[c, k] for c, k in self.cmds.most_common()]}


class Proc:
    """One process: from its exit record, or a stub for a parent with none."""

    __slots__ = ("pid", "ppid", "cmd", "eff", "args", "ran", "purpose", "role", "start",
                 "end", "dur", "cpu", "code", "sig", "core", "ev", "cwd", "kv", "depth",
                 "a3", "atype", "user", "kids", "agg", "up", "link", "gap", "orphan",
                 "hint", "n", "sub_cpu", "cpu_nm", "t0", "t1", "sig_id", "label",
                 "io", "sub_io", "gone", "leaf")

    def __init__(self, pid=None):
        self.pid = pid
        self.ppid = self.cmd = self.eff = self.args = self.ran = self.purpose = None
        self.role = self.start = self.end = self.dur = self.cpu = self.code = None
        self.sig = self.ev = self.cwd = self.kv = self.depth = self.a3 = None
        self.atype = self.user = self.agg = self.up = self.link = self.gap = None
        # `gone`: counted since, under its parent. `leaf`: itemised childless in
        # a newest-first read, so the first to be counted when room is needed.
        self.core = self.orphan = self.gone = self.leaf = False
        self.kids = []
        self.hint = None
        self.n, self.sub_cpu, self.cpu_nm = 0, None, 0
        self.io = self.sub_io = None
        self.t0 = self.t1 = None
        self.sig_id, self.label = 0, None

    @classmethod
    def from_record(cls, rec, start, ts):
        p = cls(rec.get("pid"))
        p.ppid = rec.get("ppid")
        p.cmd = rec.get("_command")
        eff = rec.get("_eff")
        p.eff = eff if eff and eff != p.cmd else None
        p.role = rec.get("_role")
        args = rec.get("args") or ""
        w = ran(p.role, args)
        p.args = args[:RAW_KEEP] or None
        p.ran = w[:RAW_KEEP] if w else None
        p.purpose = rec.get("_purpose")
        p.start, p.end = start, ts
        p.dur, p.cpu = rec.get("duration_s"), rec.get("cpu_s")
        p.io = _io_of(rec)
        p.code, p.sig = rec.get("exit_code"), rec.get("signal")
        p.core = bool(rec.get("core_dumped"))
        p.ev = rec.get("event")
        p.cwd, p.kv = rec.get("cwd"), rec.get("_kv")
        d = rec.get("depth")
        p.depth = d if isinstance(d, int) else None
        p.a3, p.atype, p.user = rec.get("_a3"), rec.get("agent_type"), rec.get("user")
        return p

    @classmethod
    def stub(cls, pid, info):
        p = cls(pid)
        p.ppid = info.get("ppid")
        p.cmd = info.get("cmd")
        p.role = info.get("role")
        args = info.get("args") or ""
        w = ran(p.role, args)
        p.args = args[:RAW_KEEP] or None
        p.ran = w[:RAW_KEEP] if w else None
        p.start = info.get("start")
        # No exit: `end` is when it was last seen alive, and `ev` stays None.
        p.end = info.get("last")
        p.cwd, p.user = info.get("cwd"), info.get("user")
        p.a3, p.atype = info.get("a3"), info.get("atype")
        p.kv, p.depth = info.get("kv"), info.get("depth")
        return p


def _alive_at(p, t):
    """Was `p` running at `t` (give or take the clock's second)?"""
    if p.start is None or t is None:
        return False
    return p.start - 1.0 <= t <= (p.end if p.end is not None else t) + 1.0


def _started(rec, ts):
    d = rec.get("duration_s")
    if isinstance(ts, (int, float)) and isinstance(d, (int, float)):
        return ts - d
    return epoch_of({"ts": rec.get("start")}) if rec.get("start") else None


class TreeBuilder:
    """Fed one session's records oldest first; builds its process tree.

    Bounded: at most `hold` processes are itemised (`hard` counting the parents
    kept past it for structure), and what arrives after is counted under its
    parent. Every such count is published, never dropped.

    `newest`: the same bounds keep the newest instead. A process that finds no
    room is drawn anyway, and the oldest childless one drawn is counted under
    its parent in its place (`_room`), so the drawn processes are the newest
    `hold` -- and of one command under one parent, the newest `per_parent`.
    """

    def __init__(self, hold=DEFAULTS["inspect_hold"],
                 per_parent=DEFAULTS["inspect_per_parent"], newest=False):
        self.hold = max(1, int(hold))
        self.hard = self.hold + self.hold // 2
        self.per_parent = max(1, int(per_parent))
        self.newest = bool(newest)
        # Newest first: the childless processes drawn, oldest first, in all and
        # per (ppid, command). An entry counted since is skipped when reached,
        # and swept out when they outnumber the drawn (`_compact`).
        self.fifo = collections.deque()
        self.fifo_k = {}
        self.live_leaves = 0
        # (ppid, command) -> childless itemised; newest first, those still drawn
        self.leaves = collections.Counter()
        self.pending = {}           # pid -> [Proc] whose record names it as parent
        self.pending_agg = {}       # pid -> Agg, the same past the hold budget
        self.orphans = []           # [Proc] whose record names no usable parent
        self.orphan_agg = {}        # session-key pid -> Agg of the same
        self.loose = None           # Agg past every budget: placed under the agent
        self.closed = {}            # pid -> the latest Proc that exited with it
        self.ticks = {}             # pid -> what its residency ticks said
        self.stubs = {}
        self.matched = self.itemised = self.absorbed = self.aggregated = 0
        self.ticks_seen = 0
        self.first = self.last = None
        # The navigator's view of every matched record (`nav`): its start and
        # purpose id, and the few records it can jump to.
        self.nav_t = array("d")
        self.nav_p = array("B")
        self.purposes, self._pid = [], {}
        self.no_start = 0
        self.first_tool = self.first_any = None
        self.longest = []           # the few longest, longest first
        self.fails, self.n_fails = [], 0

    # ----------------------------------------------------------------- feed
    def feed(self, rec):
        ev = rec.get("event")
        if ev == "residency":
            self._tick(rec)
            return
        if ev not in ("exit", "truncated"):
            return
        self.matched += 1
        ts = rec.get("_ts_epoch")
        if ts is not None:
            self.first = ts if self.first is None else min(self.first, ts)
            self.last = ts if self.last is None else max(self.last, ts)
        pid = rec.get("pid")
        st = _started(rec, ts)
        self._note(rec, pid, st, ts)
        if self.newest:
            # Room is made before this process claims what waited for it: the
            # one counted to make it may be one of those, or the parent this
            # one names, and is counted where it stands before it is claimed.
            waited = bool(self.pending.get(pid)) or pid in self.pending_agg
            self._room(not waited, (rec.get("ppid"), rec.get("_command")))
        kids = self.pending.pop(pid, None) if pid is not None else None
        if kids and st is not None:
            # A child that started before this process did had an earlier
            # holder of the pid for a parent: pids are reused, and a busy
            # session can wrap them.
            mine = [k for k in kids if k.start is None or k.start >= st - 1.0]
            if len(mine) != len(kids):
                held = set(map(id, mine))
                self.orphans.extend(k for k in kids if id(k) not in held)
            kids = mine
        agg = self.pending_agg.pop(pid, None) if pid is not None else None
        ppid = rec.get("ppid")
        # Its parent's exit may already be in: a child the collector still names
        # after its parent exited, or one written a poll round late. The pid
        # alone could be a reused one, so it must have been alive when this
        # process started.
        par = self.closed.get(ppid) if ppid not in NO_PARENT else None
        if par is not None and not _alive_at(par, st):
            par = None
        leaf = not kids and agg is None
        lk = (ppid, rec.get("_command"))
        # Newest first, the processes counted since leave their room behind.
        drawn = self.itemised - self.absorbed if self.newest else self.itemised
        if (drawn >= (self.hold if leaf else self.hard)
                or (leaf and self.leaves[lk] >= self.per_parent)):
            self._count(rec, ppid, st, ts, kids, agg, par)
            return
        p = Proc.from_record(rec, st, ts)
        self.itemised += 1
        if leaf:
            self.leaves[lk] += 1
            if self.newest:
                p.leaf = True
                self.live_leaves += 1
                self.fifo.append(p)
                self.fifo_k.setdefault(lk, collections.deque()).append(p)
        for k in kids or ():
            k.up, k.link = p, "ppid"
        p.kids = kids or []
        p.agg = agg
        if pid is not None:
            self.closed[pid] = p
        if par is not None:
            p.up, p.link = par, "ppid"
            par.kids.append(p)
        elif ppid in NO_PARENT:
            p.orphan = True
            self.orphans.append(p)
        else:
            self.pending.setdefault(ppid, []).append(p)

    def _note(self, rec, pid, st, ts):
        """What the navigator needs of one record, before any budget applies."""
        if st is None:
            self.no_start += 1
            return
        p = rec.get("_purpose") or "other"
        i = self._pid.get(p)
        if i is None:
            if len(self.purposes) >= 255:      # the vocabulary is ~15; never in practice
                p = "other"
                i = self._pid.get(p)
            if i is None:
                i = self._pid[p] = len(self.purposes)
                self.purposes.append(p)
        self.nav_t.append(st)
        self.nav_p.append(i)
        mark = (st, ts, pid, rec.get("_command"))
        role = rec.get("_role")
        if role in TOOL_ROLES:
            if self.first_tool is None or st < self.first_tool[0]:
                self.first_tool = mark
        elif role != "agent" and (self.first_any is None or st < self.first_any[0]):
            self.first_any = mark
        if rec.get("event") != "exit":
            return
        d = rec.get("duration_s")
        if isinstance(d, (int, float)) and (len(self.longest) < 4 or d > self.longest[-1][0]):
            self.longest.append((d,) + mark)
            self.longest.sort(key=lambda x: -x[0])
            del self.longest[4:]
        code, sig = rec.get("exit_code"), rec.get("signal")
        if sig is not None or (code is not None and code != 0):
            self.n_fails += 1
            if len(self.fails) < FAIL_KEEP:
                self.fails.append(mark + (code, sig))

    def nav(self, owner, lo, hi):
        """The navigator: where in the read processes started, and what to jump to.

        From every matched record, itemised or only counted, so the strip is
        the read's activity and not the served rows'. Starts are binned on the
        read's own axis `[lo, hi]` at a round width, aligned to the clock, so a
        60 s bin is a clock minute; a start before `lo` (a process already
        running when the read began) is counted in `before`, not piled into the
        first bin. The session root's own record is never "the longest
        process": it is the session.
        """
        out = {"starts": len(self.nav_t), "no_start": self.no_start,
               "purposes": list(self.purposes)}
        dens = busiest = None
        if self.nav_t and lo is not None and hi is not None and hi >= lo:
            span = max(hi - lo, 1e-3)
            w = next((b for b in BIN_WIDTHS if span / b <= DENSITY_BINS), BIN_WIDTHS[-1])
            o = math.floor(lo / w) * w
            n = int((hi - o) // w) + 1
            cnt, per_min = collections.Counter(), collections.Counter()
            before = 0
            for t, p in zip(self.nav_t, self.nav_p):
                if t < lo - 1.0:
                    before += 1
                    continue
                cnt[(min(n - 1, max(0, int((t - o) // w))), p)] += 1
                per_min[int(t // 60)] += 1
            dens = {"lo": round(o, 3), "w": w, "n": n, "before": before,
                    "bins": [[i, p, c] for (i, p), c in sorted(cnt.items())]}
            if per_min:
                m, c = max(per_min.items(), key=lambda kv: (kv[1], -kv[0]))
                busiest = {"t": m * 60, "n": c}
        out["density"] = dens
        out["busiest"] = busiest

        def mark(m, **extra):
            return dict({"t": _r(m[0]), "end": _r(m[1]), "pid": m[2], "cmd": m[3]}, **extra)

        tool = self.first_tool or self.first_any
        out["first_tool"] = mark(tool, via="tool" if tool is self.first_tool else "first") \
            if tool else None
        top = next((x for x in self.longest if x[3] is None or x[3] != owner), None)
        out["longest"] = mark(top[1:], dur=_r(top[0])) if top else None
        fails = sorted(self.fails, key=lambda f: (f[0], f[2] or 0))
        out["failures"] = {"n": self.n_fails, "listed": len(fails),
                           "items": [[_r(f[0]), _r(f[1]), f[2], f[3], f[4], f[5]]
                                     for f in fails]}
        return out

    def _count(self, rec, ppid, st, ts, kids, agg, par=None):
        """Past the hold budget: this process, and what waited on it, as a count."""
        self.aggregated += 1
        if par is not None:
            if par.agg is None:
                par.agg = Agg(rec.get("_kv"), rec.get("depth"))
            a = par.agg
        else:
            table, key = ((self.orphan_agg, rec.get("_kv")) if ppid in NO_PARENT
                          else (self.pending_agg, ppid))
            a = self._agg_at(table, key, rec.get("_kv"), rec.get("depth"))
        a.add(rec.get("_command"), rec.get("cpu_s"), st, ts, _io_of(rec))
        for k in kids or ():
            self.absorbed += a.absorb(k)
            self._drop(k)
        if agg is not None:
            a.merge(agg)

    def _agg_at(self, table, key, kv, depth):
        """The count waiting under `key` in `table`, begun if there is none."""
        a = table.get(key)
        if a is None:
            if len(self.pending_agg) + len(self.orphan_agg) >= self.hold:
                a = self.loose = self.loose or Agg()
            else:
                a = table[key] = Agg(kv, depth)
        return a

    def _drop(self, p):
        """`p` and everything under it, gone from the drawing into a count."""
        stack = [p]
        while stack:
            q = stack.pop()
            q.gone = True
            # No later child may hang from it.
            if self.closed.get(q.pid) is q:
                del self.closed[q.pid]
            if q.leaf:
                q.leaf = False
                self.live_leaves -= 1
                lk = (q.ppid, q.cmd)
                self.leaves[lk] -= 1
                if self.leaves[lk] <= 0:
                    del self.leaves[lk]
            stack.extend(q.kids)

    def _room(self, leaf, lk):
        """Newest first: count the oldest drawn processes until one more fits.

        A childless one past its command's quota under its parent takes the
        place of the oldest of that command there; any other, of the oldest
        childless one drawn at all.
        """
        if leaf and self.leaves[lk] >= self.per_parent:
            self._demote(self.fifo_k.get(lk))
        while self.itemised - self.absorbed >= (self.hold if leaf else self.hard):
            if not self._demote(self.fifo):
                break
        if len(self.fifo) > 2 * self.live_leaves + COMPACT_SLACK:
            self._compact()

    def _demote(self, q):
        """Count the oldest process still drawn in queue `q` under its parent.

        Wherever it waits -- under a parent that exited, for a parent still to
        exit, or with no parent to wait for -- its count goes where a process
        arriving past the hold there would have gone (`_count`). False when
        there is none left to count.
        """
        while q:
            p = q.popleft()
            if p.gone:
                continue
            mine = self.fifo_k.get((p.ppid, p.cmd))
            if q is self.fifo and mine is not None:
                # The oldest drawn of all is the oldest of its command too: what
                # is ahead of it there was counted already.
                while mine and (mine[0] is p or mine[0].gone):
                    mine.popleft()
            if mine is not None and not mine:
                del self.fifo_k[(p.ppid, p.cmd)]
            if p.up is not None:
                p.up.kids.remove(p)
                if p.up.agg is None:
                    p.up.agg = Agg(p.kv, p.depth)
                a = p.up.agg
            else:
                waiting = self.pending.get(p.ppid)
                if waiting is not None and p in waiting:
                    waiting.remove(p)
                    if not waiting:
                        del self.pending[p.ppid]
                    a = self._agg_at(self.pending_agg, p.ppid, p.kv, p.depth)
                else:
                    self.orphans.remove(p)
                    a = self._agg_at(self.orphan_agg, p.kv, p.kv, p.depth)
            self.absorbed += a.absorb(p)
            self._drop(p)
            return True
        return False

    def _compact(self):
        """Sweep out of the newest-first queues the entries counted since."""
        self.fifo = collections.deque(p for p in self.fifo if not p.gone)
        for lk, q in list(self.fifo_k.items()):
            left = [p for p in q if not p.gone]
            if left:
                self.fifo_k[lk] = collections.deque(left)
            else:
                del self.fifo_k[lk]

    def _tick(self, rec):
        pid = rec.get("pid")
        if pid is None:
            return
        self.ticks_seen += 1
        ts = rec.get("_ts_epoch")
        t = self.ticks.get(pid)
        if t is None:
            if len(self.ticks) >= TICK_KEEP:
                return
            age = rec.get("age_s")
            d = rec.get("depth")
            t = self.ticks[pid] = {
                "ppid": rec.get("ppid"), "cmd": rec.get("_command"),
                "args": (rec.get("args") or "")[:RAW_KEEP] or None,
                "role": rec.get("_role"), "cwd": rec.get("cwd"), "user": rec.get("user"),
                "a3": rec.get("_a3"), "atype": rec.get("agent_type"),
                "kv": rec.get("_kv"), "depth": d if isinstance(d, int) else None,
                "start": (ts - age if isinstance(ts, (int, float))
                          and isinstance(age, (int, float)) else None),
                "last": ts}
        elif ts is not None:
            t["last"] = ts if t["last"] is None else max(t["last"], ts)

    # -------------------------------------------------------------- resolve
    def resolve(self, owner, keys, index):
        """Give every held process a parent, and the session one root.

        `owner` is the agent's pid (None for a session not keyed on one), `keys`
        the session-key pids the row folds, `index(pid)` what the window knows
        of a pid that left no record here (see `inspect`). Returns the root.
        """
        keys = set(keys)
        place = collections.deque()

        def stub(pid, hint=None):
            s = self.stubs.get(pid)
            if s is not None:
                return s
            if len(self.stubs) >= MAX_STUBS:
                return None
            info = dict(self.ticks.get(pid) or {})
            for k, v in (index(pid) or {}).items():
                if info.get(k) is None:
                    info[k] = v
            s = self.stubs[pid] = Proc.stub(pid, info)
            s.hint = hint
            if s is not root:
                place.append(s)
            return s

        root = None
        if owner is not None:
            c = self.closed.get(owner)
            root = c if c is not None and c.kv == str(owner) else None
            if root is None:
                root = stub(owner)
        if root is None:
            # A session keyed on a cgroup or a tty has no agent process: a
            # virtual root holds it, and says it is one.
            root = Proc(None)
            root.cmd = "(session)"

        def attach(child, parent, link, gap=None):
            if child is root:
                return
            if parent is None or parent is child:
                parent, link, gap = root, "fold", None
            a = parent
            while a is not None:
                if a is child:          # a reused pid made it its own ancestor
                    parent, link, gap = root, "fold", None
                    break
                a = a.up
            child.up, child.link, child.gap = parent, link, gap
            parent.kids.append(child)

        def belongs(pid, deep):
            """Is a pid this session's records name as a parent in the session?"""
            if owner is None:
                return True
            if pid == owner or pid in keys:
                return True
            info = index(pid)
            if info and info.get("owner") is not None:
                return info["owner"] == owner
            # Below its session key a child's parent is the key or under it.
            return deep

        def key_node(kv):
            try:
                k = int(kv)
            except (TypeError, ValueError):
                return None
            if k == owner:
                return root
            if k in keys:
                return self.stubs.get(k) or self.closed.get(k) or stub(k)
            return None

        def adopt(p):
            """A process whose record names no usable parent: by its key."""
            if p.depth is not None and p.depth >= 1 and str(p.pid) != p.kv:
                kn = key_node(p.kv)
                if kn is not None:
                    attach(p, kn, "depth", (p.depth - 1) or None)
                    return
            attach(p, root, "fold")

        def hint_of(kids, agg=None):
            deep = [k for k in kids if k.depth is not None and k.depth >= 1]
            if deep:
                k = min(deep, key=lambda q: q.depth)
                return (k.kv, k.depth)
            if agg is not None and agg.depth is not None and agg.depth >= 1:
                return (agg.kv, agg.depth)
            return None

        for pid, kids in list(self.pending.items()):
            kids = [k for k in kids if k is not root]
            if not kids:
                continue
            h = hint_of(kids)
            s = (root if pid == owner else
                 stub(pid, h) if belongs(pid, h is not None) else None)
            for k in kids:
                if s is not None:
                    attach(k, s, "ppid")
                elif k.depth:
                    adopt(k)
                else:
                    attach(k, root, "fold")
        self.pending = {}
        for p in self.orphans:
            if p is not root:
                adopt(p)
        self.orphans = []
        for pid, a in self.pending_agg.items():
            h = hint_of((), a)
            s = (root if pid == owner else
                 stub(pid, h) if belongs(pid, h is not None) else None)
            if s is None:
                s = key_node(a.kv) if a.depth else None
            _merge_agg(s or root, a)
        for kv, a in self.orphan_agg.items():
            _merge_agg(key_node(kv) or root, a)
        if self.loose is not None:
            _merge_agg(root, self.loose)

        while place:
            s = place.popleft()
            if s.up is not None or s is root:
                continue
            q = s.ppid
            if q not in NO_PARENT and (q == owner or q in keys or q in self.stubs
                                       or q in self.closed or belongs(q, False)):
                par = root if q == owner else (self.stubs.get(q) or self.closed.get(q)
                                               or stub(q))
                attach(s, par, "index")
                continue
            if s.hint is not None:
                kv, d = s.hint
                kn = key_node(kv)
                if kn is not None and kn is not s and d >= 2:
                    attach(s, kn, "depth", (d - 2) or None)
                    continue
            attach(s, root, "fold")
        return root

    # --------------------------------------------------------------- render
    def render(self, root, budget):
        """The tree as served: grouped, totalled, and cut to `budget` entries.

        Served in the order the siblings are listed, so newest first the cut
        falls on the deep end of the oldest subtrees.
        """
        _finish(root, self.newest)
        stats = {"shown": 0, "elided": 0, "links": collections.Counter()}
        budget = max(1, int(budget))
        memo = {}
        out = _proc_dict(root, None, memo)
        stats["shown"] += 1
        left = [budget - 1]
        queue = collections.deque([(root, out)])
        while queue:
            node, d = queue.popleft()
            entries = _entries(node, self.newest)
            kids = []
            for i, e in enumerate(entries):
                if left[0] <= 0:
                    kids.append(_elided(entries[i:], stats))
                    break
                left[0] -= 1
                stats["shown"] += 1
                if isinstance(e, Proc):
                    o = _proc_dict(e, node, memo)
                    stats["links"][e.link] += 1
                    kids.append(o)
                    if e.kids or e.agg is not None:
                        queue.append((e, o))
                elif isinstance(e, Agg):
                    kids.append(e.as_dict("hold"))
                else:
                    g = _group_dict(e, node, memo)
                    kids.append(g)
                    items = []
                    for m in e[0][:GROUP_ITEMS]:
                        if left[0] <= 0:
                            break
                        left[0] -= 1
                        stats["shown"] += 1
                        stats["links"][m.link] += 1
                        o = _proc_dict(m, node, memo)
                        items.append(o)
                        if m.kids or m.agg is not None:
                            queue.append((m, o))
                    g["items"] = items
                    g["more"] = len(e[0]) - len(items)
            if kids:
                d["kids"] = kids
        return out, stats


def _merge_agg(node, a):
    if node.agg is None:
        node.agg = Agg(a.kv, a.depth)
    node.agg.merge(a)


def _finish(root, newest=False):
    """Totals, start order (newest first, if asked), labels and shape
    signatures, bottom up. A process with no start time is listed last."""
    order, stack = [], [root]
    while stack:
        n = stack.pop()
        order.append(n)
        stack.extend(n.kids)
    shapes = {}
    unique = 0
    sign = -1 if newest else 1
    for n in reversed(order):
        n.kids.sort(key=lambda k: (k.t0 is None, sign * (k.t0 or 0.0), sign * (k.pid or 0)))
        own = n.ev is not None
        n.n = (1 if own else 0) + sum(k.n for k in n.kids) + (n.agg.n if n.agg else 0)
        cpu = n.cpu if own else None
        nm = 1 if own and n.cpu is None else 0
        io = n.io if own else None
        for k in n.kids:
            cpu = _add(cpu, k.sub_cpu)
            nm += k.cpu_nm
            io = _io_add(io, k.sub_io)
        if n.agg is not None:
            cpu = _add(cpu, n.agg.cpu)
            nm += n.agg.cpu_nm
            io = _io_add(io, n.agg.io)
        n.sub_cpu, n.cpu_nm, n.sub_io = cpu, nm, io
        t0s = [t for t in [n.start] + [k.t0 for k in n.kids]
               + [n.agg.t0 if n.agg else None] if t is not None]
        t1s = [t for t in [n.end] + [k.t1 for k in n.kids]
               + [n.agg.t1 if n.agg else None] if t is not None]
        n.t0 = n.start if n.start is not None else (min(t0s) if t0s else None)
        n.t1 = max(t1s) if t1s else None
        cmds = {k.cmd for k in n.kids}
        n.label = (n.cmd or "?") + (" › " + (n.kids[0].cmd or "?")
                                    if n.kids and len(cmds) == 1 else "")
        if not own or n.role == "agent":
            # Never folded into a group: a process with no record of its own, or
            # an agent, must each stay a row.
            unique -= 1
            n.sig_id = unique
            continue
        # The shape is the command and the commands it ran, one level down and
        # as a set: what a pipeline's members start in which order is a race,
        # and `lsb_release -a` run 746 times must read as one run of 746, not
        # as 746 rows that differ in which `cut` won. How it was placed is part
        # of it, so a group never mixes a measured parent link with an
        # inferred one.
        ran_cmds = {k.cmd for k in n.kids}
        if n.agg is not None:
            ran_cmds.update(n.agg.cmds)
        n.sig_id = shapes.setdefault(
            (n.cmd, n.eff, n.role, n.link, n.gap, n.orphan, frozenset(ran_cmds)),
            len(shapes) + 1)


def runs(sigs):
    """Cover `sigs` with repeats: [(start, period, reps)], left to right.

    At each position the motif of up to MAX_PERIOD siblings that repeats over
    the most siblings wins, so `pgrep sleep pgrep sleep ...` is one run of
    period 2 rather than a thousand rows. Negative signatures never repeat.
    """
    out, i, n = [], 0, len(sigs)
    while i < n:
        best = (1, 1, 1)
        for p in range(1, MAX_PERIOD + 1):
            if i + p * MIN_REPS > n:
                break
            motif = sigs[i:i + p]
            if min(motif) < 0 or (p > 1 and motif.count(motif[0]) == p):
                continue
            reps = 1
            while sigs[i + reps * p:i + (reps + 1) * p] == motif:
                reps += 1
            if reps >= MIN_REPS and reps * p > best[0]:
                best = (reps * p, p, reps)
        out.append((i, best[1], best[2]))
        i += best[0]
    return out


def _entries(node, newest=False):
    """A node's children as served: processes, groups, and its counted rest."""
    kids = node.kids
    out = []
    for i, p, reps in runs([k.sig_id for k in kids]):
        if reps == 1:
            out.append(kids[i])
        else:
            out.append((kids[i:i + p * reps], p, reps))
    a = node.agg
    if a is not None:
        # In time order with its siblings, by when the first counted one began,
        # rather than trailing them all: it happened somewhere, and where is
        # part of the trajectory. Newest first, the same place seen from the
        # other end.
        at = len(out)
        if a.t0 is not None:
            for j, e in enumerate(out):
                t = e.t0 if isinstance(e, Proc) else e[0][0].t0
                if t is not None and (t < a.t0 if newest else t > a.t0):
                    at = j
                    break
        out.insert(at, a)
    return out


def _proc_dict(n, parent, memo=None):
    d = {"k": "p", "pid": n.pid, "cmd": n.cmd,
         "start": _r(n.start), "end": _r(n.end), "dur": _r(n.dur, 4), "cpu": _r(n.cpu),
         "n": n.n, "sub_cpu": _r(n.sub_cpu), "ev": n.ev, "link": n.link}
    # A tool call's argv is ~300 characters of the same snapshot boilerplate
    # before the command it ran; with that command extracted, the argv preview
    # would be the boilerplate alone.
    for k, v in (("ppid", n.ppid), ("eff", n.eff),
                 ("label", n.label if n.label != n.cmd else None),
                 ("args", None if n.ran else shown(n.args, memo)),
                 ("cmdline", shown(n.ran, memo)), ("purpose", n.purpose), ("role", n.role),
                 ("exit_code", n.code), ("signal", n.sig), ("gap", n.gap),
                 ("cpu_nm", n.cpu_nm or None), ("io", _io_out(n.io)),
                 ("sub_io", _io_out(n.sub_io) if n.sub_io != n.io else None)):
        if v is not None:
            d[k] = v
    if n.core:
        d["core"] = True
    if n.orphan:
        d["orphan"] = True
    # Only where it changes: a tool call's own cwd is the drift signal, and
    # repeating the parent's on every row would bury it.
    if n.cwd and (parent is None or n.cwd != parent.cwd):
        d["cwd"] = n.cwd
    for k, v, pv in (("actor3", n.a3, parent.a3 if parent else None),
                     ("agent_type", n.atype, parent.atype if parent else None),
                     ("user", n.user, parent.user if parent else None)):
        if v is not None and (parent is None or v != pv):
            d[k] = v
    return d


def _group_dict(e, parent, memo=None):
    members, p, reps = e
    exits, signals = collections.Counter(), collections.Counter()
    samples = collections.Counter()
    alive = 0
    cpu, nm, t0, t1, dmax = None, 0, None, None, None
    for m in members:
        if m.ev == "truncated":
            alive += 1
        elif m.sig is not None:
            signals[str(m.sig)] += 1
        elif m.code is not None:
            exits[str(m.code)] += 1
        cpu = _add(cpu, m.cpu)
        nm += m.cpu is None
        if m.t0 is not None:
            t0 = m.t0 if t0 is None else min(t0, m.t0)
        if m.t1 is not None:
            t1 = m.t1 if t1 is None else max(t1, m.t1)
        if m.dur is not None:
            dmax = m.dur if dmax is None else max(dmax, m.dur)
        samples[("cmdline", m.ran) if m.ran else ("args", m.args)] += 1
    sub_cpu = sub_io = None
    for m in members:
        sub_cpu = _add(sub_cpu, m.sub_cpu)
        sub_io = _io_add(sub_io, m.sub_io)
    motif = [m.label for m in members[:p]]
    # When the last member started: with the itemised first few, it bounds when
    # the members that are not drawn started, which is what the page's
    # navigator needs to land a time on this group.
    last = max((m.start for m in members if m.start is not None), default=None)
    return {"k": "g", "motif": motif, "period": p, "reps": reps, "count": len(members),
            "n": sum(m.n for m in members), "start": _r(t0), "end": _r(t1),
            "last": _r(last),
            "cpu": _r(cpu), "cpu_nm": nm or None, "sub_cpu": _r(sub_cpu),
            "sub_io": _io_out(sub_io),
            "dur_max": _r(dmax, 4), "exits": dict(exits), "signals": dict(signals),
            "alive": alive or None,
            "distinct": len(samples),
            "samples": [{key: shown(text, memo), "n": k}
                        for (key, text), k in samples.most_common(SAMPLES) if text]}


def _elided(entries, stats):
    """The siblings past the entry budget, as one counted row.

    `count` is how many siblings it stands for, `n` the processes in them and
    below them, `cmds` the siblings by label.
    """
    a = Agg()
    count = 0
    for e in entries:
        if isinstance(e, Proc):
            a.take(e)
            count += 1
        elif isinstance(e, Agg):
            a.merge(e)
            count += e.n
        else:
            for m in e[0]:
                a.take(m)
                count += 1
    stats["elided"] += a.n
    d = a.as_dict("budget")
    d.update(k="more", count=count)
    return d


# ------------------------------------------------------------------ the read

def _key_values(members):
    """The pids of these session keys as digit strings, or None when one of
    them is not keyed on a pid (a cgroup scope, a tty): no byte search then."""
    kvs = set()
    for m in members:
        kt, _, kv = m.rpartition("|")[2].partition(":")
        if kt != "apid" or not kv.isdigit():
            return None
        kvs.add(kv)
    return kvs or None


def _trie(words):
    """A regex alternation of digit strings, factored as a trie.

    A Claude session folds one key per tool call -- 672 of them for one session
    on rtx6000a on 2026-10-11 -- and a flat `a|b|c|...` has the regex engine try
    them one after another at every candidate; factored, a candidate costs the
    length of one key. `12` and `123` are both kept: `12(?:3)?`, and whatever
    follows the alternation decides.
    """
    root = {}
    for w in words:
        n = root
        for ch in w:
            n = n.setdefault(ch, {})
        n[""] = None

    def emit(n):
        alts = [ch + emit(n[ch]) for ch in sorted(k for k in n if k)]
        if not alts:
            return ""
        body = alts[0] if len(alts) == 1 else "(?:" + "|".join(alts) + ")"
        if "" in n:
            return "(?:" + body + ")?" if len(alts) == 1 else body + "?"
        return body
    return emit(root).encode()


def key_patterns(members):
    """(session_key, agent_pid) patterns for these keys' lines, or None.

    Never a false negative, by the normaliser's own rule (`session_key_of`): an
    apid key is `session_key` after its first colon, else `agent_pid`, so a
    line is the session's only if one of the two names one of its keys. A false
    hit -- another host's same pid, an agent_pid beside some other session_key
    -- only costs a parse; the `_sid` decides.
    """
    kvs = _key_values(members)
    if kvs is None:
        return None
    alt = _trie(sorted(kvs))
    return (re.compile(rb'"session_key"\s*:\s*"(?:[^":]*:)?' + alt + rb'"'),
            re.compile(rb'"agent_pid"\s*:\s*"?' + alt + rb'(?![0-9])'))


def key_matcher(members):
    """The same test on one line, or None when there is no byte check."""
    pats = key_patterns(members)
    if pats is None:
        return None
    sk, ap = pats
    return lambda line: sk.search(line) or ap.search(line)


def _end_at(fh, t, size, probe_lines=64):
    """An offset past which every line is later than `t`, or `size`.

    `export.seek_to_epoch`'s binary search with the opposite caution: a probe
    that cannot be placed in time searches LATER, because an end bound read too
    far only costs a search, and one cut too early would lose records.
    """
    lo, hi, best = 0, size, size
    while lo < hi:
        mid = (lo + hi) // 2
        fh.seek(mid)
        if mid:
            fh.readline()
        off, ts = fh.tell(), None
        for _ in range(probe_lines):
            line = fh.readline()
            if not line:
                break
            try:
                ts = epoch_of(fastjson.loads(line)) if line.strip() else None
            except ValueError:
                ts = None
            if ts is not None:
                break
        if ts is not None and ts > t:
            best = min(best, off)
            hi = mid
        else:
            lo = max(off, mid) + 1
    return best


def _hits(mm, pats, pos, stop):
    """Start offsets of the lines in `[pos, stop)` either pattern finds.

    A function of its own so the match objects, which hold the map, are gone
    when it returns: a map cannot be closed while one is alive.
    """
    out = set()
    for rx in pats:
        for m in rx.finditer(mm, pos, stop):
            out.add(mm.rfind(b"\n", pos, m.start()) + 1 or pos)
    return sorted(out)


def _key_stream(entry, norm, since, until, stats, pats, ev):
    """One file's records on the keys, as `(ts, rec)`, ascending."""
    if gz.is_gz(entry["path"]):
        # A compressed file cannot be mapped: its lines are read in order and
        # the key test runs on each before it is parsed, as `window_scan` runs
        # its `match`.
        def wanted(line):
            if ev is not None and ev(line) is None:
                return None
            return line if any(rx.search(line) for rx in pats) else None
        yield from _window_stream(entry, norm, since, until, stats, wanted)
        return
    try:
        fh = open(entry["path"], "rb")
    except OSError:
        return
    with fh:
        size = os.fstat(fh.fileno()).st_size
        start = entry["start"]
        if size <= start:
            return
        end = _end_at(fh, until + UNTIL_SLACK, size) if until is not None else size
        mm = mmap.mmap(fh.fileno(), size, access=mmap.ACCESS_READ)
        try:
            # A trailing line with no newline is still being written: left for
            # the next read, as `window_scan` leaves it.
            end = mm.rfind(b"\n", start, max(start, end)) + 1
            pos = start
            while pos < end:
                # A chunk ends at a line end, so no line is split between two.
                stop = mm.find(b"\n", min(pos + CHUNK, end) - 1, end) + 1 or end
                stats["bytes"] += stop - pos
                for ls in _hits(mm, pats, pos, stop):
                    line = mm[ls:mm.find(b"\n", ls, stop)]
                    if ev is not None and ev(line) is None:
                        stats["skipped"] += 1
                        continue
                    stats["scanned"] += 1
                    try:
                        raw = fastjson.loads(line)
                    except ValueError:
                        stats["parse_errors"] += 1
                        continue
                    rec = norm.normalize(raw)
                    if rec is None:
                        continue
                    ts = rec.get("_ts_epoch")
                    if ts is None:
                        stats["no_ts"] += 1
                        continue
                    if ts < since:
                        continue
                    if until is not None and ts > until:
                        return                # sorted: nothing later qualifies
                    yield ts, rec
                pos = stop
        finally:
            mm.close()


def key_scan(plan, norm, since, until, pats, events=EVENTS):
    """Yield `(rec, stats)` for the records on these session keys, OLDEST FIRST.

    `window_scan` for one session: the same byte ranges, the same per-file
    streams merged on timestamp, the same rule for a half-written last line --
    but each file is searched for the keys (`key_patterns`) by the regex engine
    over a memory map, a chunk at a time, and only the lines it hits are
    parsed. The per-line loop it replaces spent most of a read in Python on
    every other session's lines: 2.9 s for one Claude session's 11 000 lines in
    rtx6000a's 627 MB day-file of 2026-10-11, against 0.8 s searched.

    `skipped` here counts the lines a key hit that are not one of `events`;
    the lines no key hit are never visited, so they are not counted at all.
    """
    stats = {"scanned": 0, "skipped": 0, "bytes": 0, "parse_errors": 0,
             "no_ts": 0, "files": len(plan), "method": "key search"}
    ev = event_filter(events)
    streams = [_key_stream(e, norm, since, until, stats, pats, ev) for e in plan]
    for _ts, rec in heapq.merge(*streams, key=lambda kr: kr[0]):
        yield rec, stats


def _day_end(sid):
    """The end of the local day a row's sid names: its records stop there."""
    try:
        day = sid.split("|")[1]
        return time.mktime(time.strptime(day, "%Y-%m-%d")) + 86400
    except (IndexError, ValueError):
        return None


def lookup(traj, sid):
    """`traj.lookup(sid)`, folding only the keys that resolve to this row.

    The reducer folds every key in the window at once, and keeps the result
    only until the next record arrives -- on a live window, every few seconds.
    At 24 h on rtx6000a that fold is half a second to most of one, spent under
    the service's lock, for one row. A key resolves to this row only if its sid
    shares the row's `host|day|class|apid:` prefix, so only those are resolved
    and folded, by the reducer's own rule (`_fold_all`); a fold already cached
    is used as it is.
    """
    procs = traj.procs
    if procs is None or traj._fold_cache[0] == (traj.fed, traj.evicted, procs.version):
        return traj.lookup(sid)
    cut = sid.rfind(":") + 1
    head, tail = sid[:cut], sid[cut:]
    if not head.endswith("|apid:"):
        # A cgroup scope or a tty is never folded: the row is its own key.
        s = traj.sessions.get(sid)
        return (s, None, 1, [sid]) if s is not None else None
    members = []
    for s in list(traj.sessions.values()):
        if not s.sid.startswith(head) or s.sid.rfind(":") + 1 != cut:
            continue
        target, via = s.sid, None
        if s.kt == "apid":
            try:
                pid = int(s.kv)
            except (TypeError, ValueError):
                pid = None
            if pid is not None:
                owner, via = procs.owner(s.host, pid, s.tree_root)
                if owner != pid:
                    target = head + str(owner)
        if target == sid:
            members.append((s, via))
    if not members:
        return None
    members.sort(key=lambda m: m[0].sid != sid)       # the owner's own key first
    parts = [m[0] for m in members]
    s = parts[0] if len(parts) == 1 and parts[0].sid == sid else \
        fold_sessions(parts, sid, tail)
    vs = [v for p, v in members if p.sid != sid] or [v for _, v in members]
    vs = [v for v in vs if v is not None]
    return s, (max(vs, key=VIA.index) if vs else None), len(parts), [p.sid for p in parts]


_scan_lock = threading.Lock()
_cache_lock = threading.Lock()
_cache = collections.OrderedDict()      # key -> (expires, body, serialised bytes)
_waiting = [0]                          # readers queued for the scan lock
_warm = {"thread": None, "last_open": 0.0}


class _GiveWay(Exception):
    """A background read stepping aside for a reader who opened a session."""


def _limits(cfg):
    r = cfg.get("reducers.trajectories", {}) or {}
    return {k: int(r.get(k, v) or v) for k, v in DEFAULTS.items()}


def _cached(key, now):
    with _cache_lock:
        hit = _cache.get(key)
        if hit is None:
            return None
        if hit[0] <= now:
            del _cache[key]
            return None
        _cache.move_to_end(key)
        return dict(hit[1], cached=True)


def _remember(key, body):
    """Keep a built tree, least recently opened out first, within the bounds."""
    size = len(json.dumps(body, default=str, separators=(",", ":")))
    with _cache_lock:
        _cache[key] = (time.time() + CACHE_TTL, body, size)
        _cache.move_to_end(key)
        while len(_cache) > 1 and (len(_cache) > CACHE_KEEP or
                                   sum(v[2] for v in _cache.values()) > CACHE_BYTES):
            _cache.popitem(last=False)


def inspect(cfg, view, lock, roots, sid, frm=None, to=None, now=None, background=False,
            order="oldest"):
    """The process tree of row `sid` in `view`'s window.

    `frm`/`to` (epoch seconds) narrow the read inside the session's span: the
    page uses `frm` to continue past a read that stopped at its budget. One
    read runs at a time -- a second request waits rather than doubling the I/O.
    A built tree is served again for CACHE_TTL seconds, the last CACHE_KEEP of
    them, until the window is rebuilt (`installed_at` is in the key).

    `order` is one of ORDERS, anything else the first: which end of the
    session the tree lists first, and draws in full (see the module's notes).

    `background` is the warmer's read (`_warm_loop`): it never waits for the
    scan, gives way mid-read to anyone who opens a session, and returns None
    when it did.
    """
    now = now or time.time()
    order = order if order in ORDERS else ORDERS[0]
    key = (sid, view.minutes, view.installed_at, frm, to, order)
    hit = _cached(key, now)
    if hit is not None:
        return hit
    if background:
        if _waiting[0] or not _scan_lock.acquire(blocking=False):
            return None
    else:
        with _cache_lock:
            _waiting[0] += 1
        try:
            _scan_lock.acquire()
        finally:
            with _cache_lock:
                _waiting[0] -= 1
    try:
        hit = _cached(key, time.time())
        if hit is not None:
            return hit
        try:
            body = _inspect(cfg, view, lock, roots, sid, frm, to,
                            give_way=(lambda: _waiting[0] > 0) if background else None,
                            order=order)
        except _GiveWay:
            return None
        if body.get("found"):
            _remember(key, body)
        return dict(body, cached=False)
    finally:
        _scan_lock.release()


def _inspect(cfg, view, lock, roots, sid, frm, to, give_way=None, order="oldest"):
    lim = _limits(cfg)
    newest = order == "newest"
    # Newest first reads on to the session's end: see `inspect_newest_records`.
    max_records = lim["inspect_newest_records" if newest else "inspect_max_records"]
    with lock:
        status = view.status()
        red = view.reducers or {}
        traj = red.get("trajectories")
        if traj is None:
            # The window is still being reduced: the page shows <Building>.
            return {"sid": sid, "found": None, "_window": status}
        hit = lookup(traj, sid)
        if hit is None:
            return {"sid": sid, "found": False, "_window": status,
                    "why": "no session with this id in the %s window" % status["label"]}
        s, via, nkeys, members = hit
        row = traj._row(s, via, nkeys)
        host, first, last = s.host, s.first, s.last
        owner = int(s.kv) if s.kt == "apid" and str(s.kv).isdigit() else None
        keys = set()
        for m in members:
            kt, _, kv = m.rpartition("|")[2].partition(":")
            if kt == "apid" and kv.isdigit():
                keys.add(int(kv))

    t0 = time.time()
    if first is None:
        return {"sid": sid, "found": False, "_window": status, "row": row,
                "why": "the session's records carry no timestamp, so they cannot "
                       "be placed in a read"}
    since = max(first, frm) if frm else first
    until = _day_end(sid)
    if last is not None and last < t0 - 300:
        until = min(until, last + 1) if until else last + 1
    if to:
        until = min(until, to) if until else to
    plan, total = window_plan(roots, since, int(max(0, t0 - since) // 86400) + 2)
    # The sid names one host, and a day-file is named for its host: reading
    # the other hosts' files would only find other processes with the same pids.
    mine = [e for e in plan if host and e["path"].rsplit("/", 1)[-1].startswith(host + ".")]
    if mine:
        plan, total = mine, sum(e["size"] - e["start"] for e in mine)

    b = TreeBuilder(lim["inspect_hold"], lim["inspect_per_parent"], newest=newest)
    norm = Normalizer(cfg)
    want = set(members)
    stopped, stopped_at, stats = None, None, {}
    pats = key_patterns(members)
    records = (key_scan(plan, norm, since, until, pats) if pats is not None else
               window_scan(plan, norm, since, until, events=EVENTS))
    if give_way is not None and give_way():
        raise _GiveWay
    for rec, stats in records:
        if rec.get("_sid") not in want:
            continue
        b.feed(rec)
        if give_way is not None and not b.matched & 255 and give_way():
            raise _GiveWay
        if b.matched >= max_records:
            stopped, stopped_at = "records", rec.get("_ts_epoch")
            break
        if stats["bytes"] >= lim["inspect_max_bytes"]:
            stopped, stopped_at = "bytes", rec.get("_ts_epoch")
            break
    elapsed = time.time() - t0

    with lock:
        procs = red.get("_procs")
        casc = red.get("cascades")

        def index(pid):
            out = {}
            if procs is not None:
                e = procs.get(host, pid)
                if e is not None:
                    out.update(ppid=e[0], role=e[1], cmd=e[2], cwd=e[3], user=e[4])
                    o, how = procs.owner(host, pid)
                    if how != "unknown":
                        out["owner"] = o
            n = casc.nodes.get((host, pid)) if casc is not None else None
            if n is not None:
                for k, v in (("args", n.get("args")), ("start", n.get("start")),
                             ("atype", n.get("agent_type")), ("cmd", n.get("command")),
                             ("cwd", n.get("cwd")), ("ppid", n.get("ppid")),
                             ("last", n.get("last"))):
                    if out.get(k) is None and v is not None:
                        out[k] = v
            return out or None

        root = b.resolve(owner, keys, index)
    tree, rs = b.render(root, lim["inspect_nodes"])

    # The timeline spans what was read, not the agent's whole life: an agent
    # started two days ago would otherwise squeeze the session into a sliver.
    # A process that started before it is drawn running off the left edge.
    lo = since if b.first is None else min(since, b.first)
    return {
        "sid": sid, "found": True, "row": row, "host": host, "owner_pid": owner,
        "keys": len(members), "order": order,
        "span": {"first": first, "last": last},
        "scan": {"from": since, "to": until, "files": len(plan),
                 "bytes_planned": total, "bytes": stats.get("bytes", 0),
                 "parsed": stats.get("scanned", 0), "skipped": stats.get("skipped", 0),
                 "method": stats.get("method", "line scan"),
                 "records": b.matched, "ticks": b.ticks_seen,
                 "first": b.first, "last": b.last,
                 "stopped": stopped, "stopped_at": stopped_at,
                 "elapsed_s": round(elapsed, 2),
                 "computed_at": round(time.time(), 3)},
        "bounds": {"max_records": max_records,
                   "max_bytes": lim["inspect_max_bytes"],
                   "hold": b.hold, "per_parent": b.per_parent,
                   "nodes": lim["inspect_nodes"],
                   "itemised": b.itemised - b.absorbed,
                   "counted": b.aggregated + b.absorbed,
                   "shown_entries": rs["shown"], "elided": rs["elided"],
                   "stubs": len(b.stubs)},
        "links": {k: v for k, v in rs["links"].items() if k},
        "timeline": {"lo": lo, "hi": b.last},
        "nav": b.nav(owner, lo, b.last),
        "tree": tree,
        "basis": {
            "records": "the exit records whose _sid is one of the session keys this "
                       "row folds, re-read from the day-files through the normaliser; "
                       "residency ticks of those keys describe processes still running",
            "parent": "a record's own ppid where its parent exited later in the read; "
                      "else the window's process index (link=index), the collector's "
                      "depth below the session key (link=depth, gap = levels never "
                      "recorded), or the fold alone (link=fold)",
            "groups": "consecutive siblings of the same shape (the command, and the "
                      "set of commands it ran) fold into one group, up to a "
                      "%d-sibling repeating motif; "
                      "the first %d members are itemised" % (MAX_PERIOD, GROUP_ITEMS),
            "cpu": "cpu is the process's own cpu_s; sub_cpu adds everything below "
                   "it. child_cpu_s is never added. null is unmeasured, not 0",
            "bounds": "at most %d exit records or %d GB read; %d processes "
                      "itemised, %d childless ones of one command per parent (the "
                      "rest counted under their parent, k=agg); %d entries served "
                      "breadth first (the rest counted, k=more)"
                      % (max_records, lim["inspect_max_bytes"] >> 30,
                         b.hold, b.per_parent, lim["inspect_nodes"]),
            "order": ("newest first: siblings by latest start first. The read goes "
                      "on to the session's end and every budget keeps the newest, so "
                      "the oldest processes are the ones counted (k=agg) or past the "
                      "served entries (k=more)" if newest else
                      "oldest first: siblings by start. Every budget keeps the "
                      "oldest, so the newest processes are the ones counted (k=agg) "
                      "or past the served entries (k=more)"),
            "args": "masked, then cut to %d characters" % PREVIEW,
            "nav": "from every record the read matched, itemised or counted: process "
                   "starts binned by purpose on the read's axis (a start before it "
                   "is counted in `before`), the busiest clock minute by starts, the "
                   "first tool call (a tool-call shell, sandbox wrapper or embedded "
                   "tool; failing one, the first process), the longest process other "
                   "than the session root, and every exit with a non-zero code or a "
                   "signal, the first %d listed by start" % FAIL_KEEP,
        },
        "_window": status,
    }


def response(body, accept_encoding):
    """The body as JSON, gzipped when the client takes it.

    A tree of a few thousand rows is a few MB of repetitive JSON, and the page is
    usually read over an ssh tunnel: compressed it is a tenth of that.
    """
    raw = json.dumps(body, default=str, separators=(",", ":")).encode()
    if "gzip" in (accept_encoding or "") and len(raw) > 4096:
        return gzip.compress(raw, compresslevel=5), {"Content-Encoding": "gzip",
                                                     "Vary": "Accept-Encoding"}
    return raw, {"Vary": "Accept-Encoding"}


def serve(agg, sid, window_min=None, frm=None, to=None, order="oldest"):
    """`/api/trajectory`: `inspect` over the aggregator's view of that window."""
    _warm["last_open"] = time.time()
    if _warm["thread"] is None:
        _warm["thread"] = threading.Thread(target=_warm_loop, args=(agg,),
                                           name="proctree-warm", daemon=True)
        _warm["thread"].start()
    win = agg.window(window_min)
    view = agg.panels_by_window.request(win["minutes"])
    body = inspect(agg.cfg, view, agg.lock, agg.roots(), sid, frm, to, order=order)
    body["window"] = win
    body.update(feedmod.panel_meta(agg.reports["ebpf"]))
    return body


WARM_IDLE = 1800         # seconds without an open after which the warmer rests


def _warm_loop(agg):
    """Keep the default ranking's top sessions read, so opening one is a cache hit.

    Started by the first open, so a service nobody inspects never reads for
    it, and resting once nobody has opened a session for WARM_IDLE. One thread;
    it reads only while no window is queued or being built -- a build is the
    work every panel waits for -- and its reads give way, mid-read, to anyone
    who opens a session (`inspect(background=True)`). What it builds is an
    ordinary cache entry, keyed on the window's install time like any other,
    so after a rebuild it reads the new top sessions again.
    """
    def busy(reg):
        return any(v.state in ("building", "queued") for v in list(reg.views.values()))

    while not getattr(agg, "stopping", False):
        time.sleep(WARM_EVERY)
        if time.time() - _warm["last_open"] > WARM_IDLE:
            continue
        try:
            reg = agg.panels_by_window
            minutes = agg.window(None)["minutes"]
            with agg.lock:
                if busy(reg):
                    continue
                view = reg.views.get(minutes)
                tj = ((view.payload or {}).get("trajectories")
                      if view is not None and view.state == "ready" else None)
                sids = list(((tj or {}).get("ranks") or {}).get("events") or [])[:WARM_TOP]
            for sid in sids:
                if _waiting[0] or getattr(agg, "stopping", False):
                    break
                with agg.lock:
                    if busy(reg) or view.state != "ready":
                        break
                inspect(agg.cfg, view, agg.lock, agg.roots(), sid, background=True)
        except Exception:
            # An optimisation: whatever goes wrong here, the open it was for
            # simply reads, as it would have without it.
            continue
