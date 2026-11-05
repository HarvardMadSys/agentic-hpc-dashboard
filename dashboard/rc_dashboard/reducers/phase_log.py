"""What a session's tool calls were, and what ran under a process whose own record has not come yet.

Two plain data structures, no I/O, for the trajectories reducer. `phase_rules` says what a record
is; the segmenter reads what is held here and turns it into phases. Everything is a list, a dict, a
tuple, or a number, so a session and the join carried across hours pickle with the default protocol.
Times are epoch seconds and never None: a record with no time is the reducer's to place or to skip.
Only `pop`'s `start` and `add_housekeeping`'s `t` may be None, and are then not tested or not
binned.

PhaseLog: one session's calls and housekeeping, bounded.

  A call is a tuple `(start, end, cat, h, prev, flags, pid, past, tags)`, indexed by S, E, CAT, H,
  PREV, FLAGS, PID, PAST, TAGS: epoch seconds, one of `phase_rules.CATS`, the hash of the
  normalized command (0 for none), the id of its masked preview (0 for none), the flags OPEN (still
  running, seen through its children) and WROTE (Codex's fs helper wrote bytes), the pid of the
  process that ran it, `past`: the end of the last process that outlived the call, or None, and
  `tags`: the id of its tag set, the names it worked on and the agent's own words in it
  (`phase_names.tagset`; 0 for the empty set, which is also what a call whose set the reducer could
  not keep has). A call that starts a background job (`nohup python train.py &`) returns at once
  and the job runs for hours; that time belongs to the call, as `past`, and is not a call of its
  own. The ids of the preview and the tag set are the reducer's own, and it renumbers them when two
  hours merge. An OPEN call, which the reducer makes when a row is read and never logs, has a tenth
  field, SEEN: the programs seen under it. Its `h`, `prev`, and `tags` are its first child's text,
  which is no command of its own (R65): what it waits on is read from them, and nothing else.

  The log holds at most `cap` calls, the first it was shown, and counts the rest: `cut` is how many
  it dropped and `cut_at` the earliest start among them, so `n` is every call seen and the list is
  complete before `cut_at`. Nothing is dropped quietly. `merge` joins two hours of one session as
  one pass would have held them; `fold` joins the collector keys of one agent into a NEW log that
  shares no list with its parts, because they go on ingesting while a row is built from it.

  Housekeeping is what an agent runs that the model did not ask for. It is counted by shape (`hk`)
  and by the minute it began (`hk_bins`, at most `bin_cap` minutes). A command with no time, or in
  a minute past that bound, is in `hk` and in `hk_cut`: sum(hk) == sum(hk_bins) + hk_cut.

PendingJoin: what ran under a process that has not been recorded.

  A child's exit is recorded before its parent's, so when a process exits nobody knows yet whose
  call it belongs to. What it did is folded into an entry for its parent, `(host, ppid)`, and the
  parent's own record takes the entry (`pop`) when it arrives. An entry is a list indexed by
  P_FIRST (the earliest start), P_LAST (the latest end), P_N (records folded in), P_CATS (category
  to count), P_CMDS, P_TEXT (the first text any record gave, the entry's own first: a sandbox
  wrapper has none, and so a call takes its payload's), P_WROTE (any record wrote), P_SID (the
  session of its newest child, as a finished call is its own record's: a loop that runs past
  midnight is the new day's row's, R59), the stretch, P_SINCE and P_SINCE_N (`push`), and P_HELP:
  of each kept command's starts, how many were a login shell's startup helper (None for none), so
  `seen` names a command the call ran itself and no other (R72). A parent
  that never exits (an MCP
  server, a tool shell that outlived its agent) keeps its entry; the reducer reads those as open
  calls (`loop_like`, or two children or more in the stretch) or counts them, and the hours carry
  them (`entries` and `touched`, as `reaped` is carried).

  A loop is regular per command, not over all its children. A loop alternates commands, so the
  gaps between all its children's starts are not regular when the loop is. P_CMDS holds, for at
  most MAX_CMDS basenames, `[n, gap_mean, gap_m2, last_start, first_start]`: how many times the
  command started, the mean and the sum of squared deviations of the gaps between those starts
  (Welford's), and the latest and the earliest start. On boslogin06 (FAS RC, schema 7) on
  2026-10-28, in the first four million exit records (18.5 h), one parent has 124,798 agent
  children: `tail` and `df` each start every 1.0 s (gap stdev over mean 0.017 and 0.015), while
  the stdev of the gaps between all its children's starts is 2.17 times their mean. Another has
  `grep` every 5.0 s (0.008) and `sleep` every 5.2 s (0.203) beside `bash` starts that are not
  regular (0.998), so a loop is any one command that is, and not all of them.

  A start that joins an entry adds the gap from that command's latest start, however the two were
  recorded. An entry that joins another adds the gaps it holds, by Chan's parallel formulas, and
  the gap between the two runs, from the earlier's last start to the later's first: a helper that
  runs a burst of `sh` and `uname` once an hour is no loop polling every second, as it read when
  that gap was taken as the mean (R57; on boslogin06, `uname` "every 0.4 s" in apid 3380957).
  Runs that overlap leave no such gap, and it is taken as the mean, which moves neither the mean
  nor the spread; either way `n - 1` is the number of gaps held.

  A pid is reused. In the same four million records 27.7% carry a pid an earlier record had. Of
  2,741,198 children matched to a record of their ppid, 2.4% began over an hour before it, which
  no child can: they were children of an earlier process with that pid. So `pop` refuses, and
  drops, an entry that began more than START_SLACK_S before the process did. The slack is for the
  stamp. A record's time is when the collector handled the exit (`ebpf_trace._exit_record`) and
  its start is that less its duration, so a real child can seem to begin before its parent: 21.5%
  do, by under a second, and 0.62% by one second to a minute; one pair lay between a minute and
  an hour. A second would refuse that 0.62%, and each would lose what its children did, so
  START_SLACK_S is a minute. That lets in no stale entry in this slice: of 1,107,441 recurrences
  of a pid, two came within a second of the earlier process ending, none between a second and a
  minute, 51 between a minute and an hour, and the median was 5.1 hours.

  A process that ran past its call. A child recorded after a record of its ppid is not always
  that process's child. Of 1,004,636 such agent children in the same slice, 28.4% began no more
  than a second after the process ended (20.8% while it ran), 17 began a second to a minute after
  it, 449 a minute to an hour after, and 71.6% over an hour after. The last two groups are later
  processes that reused the pid (none recurred inside a minute, above). So `ran_past` takes a
  child into the call only if its `start`, when given, is no more than START_SLACK_S after the
  call ended. Of those children 28.4% ended within a second of the process, the order the file
  wrote two exits that came together, and 2 began while it ran and ended over an hour after it,
  so `past` is set only for one that ended more than PAST_SLACK_S after the call. Reparenting is
  the other limit. A job whose shell exited first reports ppid 1 or a subreaper
  (`ebpf_trace.proc_add`), as 2,505 of the slice's 3,388,082 agent records do (2,100 of them ran
  a minute or more), so it does not reach `ran_past` by its call's pid.

  `entries` is bounded at `cap`: past it the entry pushed to least recently is dropped and counted
  in `evicted`. It fills. Counting every agent child, 189,689 parents were still waiting for their
  own record when those 18.5 h ended, with 644,379 children under them, and the count was still
  rising. Most never pop: a child that outlives a parent that is no call (the stage of a pipeline
  that ends after the shell does) is pushed after its parent's record has gone by. Over the whole
  of 2026-10-28 an uncapped join ended with 177,411 entries, and at a cap of 50,000 dropped
  167,353. The oldest-inserted entry was the one dropped, and the oldest are the loops still
  running (a tool shell polling a log all day, pushed to every 15 to 30 s), so each restarted every
  few hours and the stretch before read idle; dropping the one pushed to least recently keeps them
  all, as uncapped, and drops the stale ones.
"""
import itertools
import math
from collections import OrderedDict
from operator import itemgetter

from . import add_counts

# A call: (start, end, cat, h, prev, flags, pid, past, tags)
S, E, CAT, H, PREV, FLAGS, PID, PAST, TAGS = range(9)
SEEN = 9                    # an OPEN call's: the programs seen under it
OPEN = 1                    # a flag: still running, seen through its children
WROTE = 2                   # a flag: Codex's fs helper wrote bytes

# A pending entry: [first, last, n, cats, cmds, text, wrote, sid, since, since_n, help]
P_FIRST, P_LAST, P_N, P_CATS, P_CMDS, P_TEXT, P_WROTE, P_SID, P_SINCE, P_SINCE_N, P_HELP = range(11)

MAX_CMDS = 5                # commands whose spacing an entry keeps; the others count in P_N only
START_SLACK_S = 60.0        # a child can seem to begin before its parent did, or after it ended
PAST_SLACK_S = 1.0          # a process that ended within this of its call's end did not outlive it


class PhaseLog:
    __slots__ = ("calls", "by_pid", "hk", "hk_bins", "hk_cut", "cut", "cut_at")

    def __init__(self):
        self.calls = []         # call tuples, in the order they were recorded
        self.by_pid = {}        # pid -> index in `calls`, for the held calls that have one
        self.hk = {}            # housekeeping shape -> commands
        self.hk_bins = {}       # minute (epoch seconds) -> housekeeping commands begun in it
        self.hk_cut = 0         # housekeeping commands in `hk` and in no bin
        self.cut = 0            # calls past the cap: counted, not held
        self.cut_at = None      # the earliest start among them

    @property
    def n(self):
        """Calls seen: the ones held and the ones the cap dropped."""
        return len(self.calls) + self.cut

    def _note_cut(self, n, at):
        """Count `n` calls the log does not hold, the earliest of which started at `at`."""
        self.cut += n
        if at is not None and (self.cut_at is None or at < self.cut_at):
            self.cut_at = at

    def add(self, call, cap):
        """Hold a call, or count it when `cap` calls are held."""
        if len(self.calls) >= cap:
            self._note_cut(1, call[S])
            return
        if call[PID] is not None:
            self.by_pid[call[PID]] = len(self.calls)       # a reused pid names the newest call
        self.calls.append(call)

    def ran_past(self, pid, end, start=None):
        """A process whose parent is `pid` exited at `end`, having begun at `start` (None if
        unknown). True when `pid` ran a held call and the process can be its child: it is part of
        that call, however long it ran. An `end` more than PAST_SLACK_S after the call's own, and
        later than any before it, becomes the call's `past`; every other field of the call stays,
        those after `past` too. False when no held call had that pid, or when `start` is more than
        START_SLACK_S after the call ended: a child is forked while its parent runs, so that is a
        later process that reused the pid."""
        i = self.by_pid.get(pid)
        if i is None:
            return False
        c = self.calls[i]
        if start is not None and start > c[E] + START_SLACK_S:
            return False
        if end > c[E] + PAST_SLACK_S and (c[PAST] is None or end > c[PAST]):
            self.calls[i] = c[:PAST] + (end,) + c[PAST + 1:]      # the fields after it stay
        return True

    def add_housekeeping(self, shape, t, bin_cap, n=1):
        """Count `n` housekeeping commands of `shape` that began at `t`, binned by the minute. A
        command with no time, or in a new minute past `bin_cap` minutes, is counted, not binned."""
        if n <= 0:
            return
        self.hk[shape] = self.hk.get(shape, 0) + n
        if t is not None:
            b = int(t // 60) * 60
            held = self.hk_bins.get(b)
            if held is not None:
                self.hk_bins[b] = held + n
                return
            if len(self.hk_bins) < bin_cap:
                self.hk_bins[b] = n
                return
        self.hk_cut += n

    def merge(self, later, cap):
        """Join the LATER log of the same session, as one pass over both would have held it: the
        first `cap` calls, and the rest counted. The bins are the union of both. `later` is
        consumed."""
        base = len(self.calls)
        held = later.calls[:max(0, cap - base)]
        dropped = later.calls[len(held):]
        self.calls.extend(held)
        for pid, i in later.by_pid.items():
            if i < len(held):
                self.by_pid[pid] = base + i
        starts = [c[S] for c in dropped]
        if later.cut_at is not None:
            starts.append(later.cut_at)
        self._note_cut(later.cut + len(dropped), min(starts) if starts else None)
        add_counts(self.hk, later.hk)
        add_counts(self.hk_bins, later.hk_bins)
        self.hk_cut += later.hk_cut

    @classmethod
    def fold(cls, parts, cap):
        """One log from the collector keys of one agent: a NEW object, sharing no list or dict with
        a part, which goes on ingesting. The keys' calls are interleaved by start, and the earliest
        `cap` are held."""
        out = cls()
        calls = sorted(itertools.chain.from_iterable(p.calls for p in parts), key=itemgetter(S))
        dropped = calls[cap:]
        del calls[cap:]
        out.calls = calls
        out.by_pid = {c[PID]: i for i, c in enumerate(calls) if c[PID] is not None}
        starts = [p.cut_at for p in parts if p.cut_at is not None]
        if dropped:
            starts.append(dropped[0][S])                   # they are in order of start
        out._note_cut(sum(p.cut for p in parts) + len(dropped), min(starts) if starts else None)
        for p in parts:
            add_counts(out.hk, p.hk)
            add_counts(out.hk_bins, p.hk_bins)
            out.hk_cut += p.hk_cut
        return out


def _fold_gaps(a, b):
    """Add the spacing stats `b` of one command to `a`'s, in place: `[n, gap_mean, gap_m2, last,
    first]`.

    One start joining (`b[0] == 1`) adds one gap, from `a`'s latest start, by Welford's step; the
    gap is taken as it falls, whichever recorded first (parallel children exit in another order than
    they began). Two runs of starts join by Chan's parallel formulas on the gaps each holds, `n - 1`
    apiece, and then the gap between them, from the last start of the earlier run to the first of
    the later, by Welford's step: bursts an hour apart are no loop polling every second (R57). Runs
    that overlap leave no gap between them, and it is taken as the mean, which changes neither the
    mean nor the sum of squares; either way `n - 1` is the number of gaps held.
    """
    na, nb = a[0], b[0]
    if nb == 1:
        gap = abs(b[3] - a[3])
        delta = gap - a[1]
        a[1] += delta / na                                  # `na` starts held, so `na` gaps now
        a[2] += delta * (gap - a[1])
    else:
        ga, gb = na - 1, nb - 1
        delta = b[1] - a[1]
        a[1] += delta * gb / (ga + gb)
        a[2] += b[2] + delta * delta * ga * gb / (ga + gb)
        bridge = b[4] - a[3] if b[4] >= a[3] else a[4] - b[3] if a[4] >= b[3] else None
        if bridge is not None:
            delta = bridge - a[1]
            a[1] += delta / (ga + gb + 1)
            a[2] += delta * (bridge - a[1])
    a[0] = na + nb
    a[3] = max(a[3], b[3])
    a[4] = min(a[4], b[4])


def fold_entry(into, item):
    """Fold the pending entry `item` into `into`, in place, and return `into`: the fold `push`
    applies. Counts add, the first start is the earliest and the last end the latest, the entry's
    own text stays and the first text it lacked is taken (so, to give one entry's text first, fold
    the other into it), a write is a write, and `item`'s commands join `into`'s while it holds
    fewer than MAX_CMDS. `into`'s session stays: the reducer folds what ran under a process into
    that process's own entry, the newer, and `push` gives an entry its newest child's. `item`'s
    lists are copied from, never shared. A command's helper starts (P_HELP) go where its starts
    go, and nowhere when they are not kept."""
    into[P_FIRST] = min(into[P_FIRST], item[P_FIRST])
    into[P_LAST] = max(into[P_LAST], item[P_LAST])
    into[P_N] += item[P_N]
    add_counts(into[P_CATS], item[P_CATS])
    cmds, helped = into[P_CMDS], item[P_HELP]
    for cmd, stats in item[P_CMDS].items():
        if cmd in cmds:
            _fold_gaps(cmds[cmd], stats)
        elif len(cmds) < MAX_CMDS:
            cmds[cmd] = list(stats)
        else:
            continue
        if helped and cmd in helped:
            if into[P_HELP] is None:
                into[P_HELP] = {}
            into[P_HELP][cmd] = into[P_HELP].get(cmd, 0) + helped[cmd]
    if into[P_TEXT] is None:
        into[P_TEXT] = item[P_TEXT]
    into[P_WROTE] = into[P_WROTE] or item[P_WROTE]
    if into[P_SID] is None:
        into[P_SID] = item[P_SID]
    return into


class PendingJoin:
    __slots__ = ("cap", "entries", "touched", "evicted", "idle_s", "refused", "refused_records")

    def __init__(self, cap, idle_s=None):
        self.cap = cap
        # (host, pid) -> what ran under that process, by its children; least recently pushed first.
        # An OrderedDict: a plain dict finds its first key past every slot deleted before it
        # (about 20 us at 50,000, against 0.3 us; 6f03948)
        self.entries = OrderedDict()
        self.touched = None     # the keys pushed since tracking began (a set), for the ageing
        self.evicted = 0        # entries dropped at the cap
        self.idle_s = idle_s    # a gap this long in what an entry's children did ends its stretch
        self.refused = 0        # entries `pop` dropped as an earlier process's, with that pid,
        self.refused_records = 0    # and the records they held

    @staticmethod
    def leaf(start, end, cat, cmd, text, wrote, sid, helper=False):
        """The entry for one record: its command's basename `cmd` (None for no command), the call's
        `text` (None when it has none of its own), whether it wrote, and whether it is a login
        shell's startup helper (`phase_rules.login_helper`), which is kept in its command's starts
        as any start is, and counted apart (P_HELP)."""
        return [start, end, 1, {cat: 1}, {cmd: [1, 0.0, 0.0, start, start]} if cmd else {}, text,
                bool(wrote), sid, start, 0, {cmd: 1} if helper and cmd else None]

    def push(self, host, ppid, item):
        """Fold `item`, what a process and its children did, into its parent's entry, which moves
        to the end: past `cap` the entry pushed to least recently is dropped. A parent with none
        yet takes `item` itself: the caller gives it up. The entry is filed under the session of
        `item`, its newest child (P_SID, R59).

        `item` is one child of the parent, busy from its first start to its last end, and the entry
        keeps its stretch: since when (P_SINCE) its children have been busy with no gap of `idle_s`
        or more between one's end and the next one's start, and how many children came in it
        (P_SINCE_N). A helper's bursts an hour apart are each a stretch of their own (R57)."""
        key = (host, ppid)
        if not isinstance(self.entries, OrderedDict):       # seeded from a carry that holds a dict
            self.entries = OrderedDict(self.entries)
        cur = self.entries.get(key)
        if cur is None:
            if self.entries and len(self.entries) >= self.cap:
                self.entries.popitem(last=False)
                self.evicted += 1
            item[P_SINCE], item[P_SINCE_N] = item[P_FIRST], 1
            self.entries[key] = item
        else:
            self.entries.move_to_end(key)
            gap = item[P_FIRST] - cur[P_LAST]
            fold_entry(cur, item)
            if item[P_SID] is not None:
                cur[P_SID] = item[P_SID]
            if self.idle_s is not None and gap >= self.idle_s:
                cur[P_SINCE], cur[P_SINCE_N] = item[P_FIRST], 1
            else:
                cur[P_SINCE] = min(cur[P_SINCE], item[P_FIRST])
                cur[P_SINCE_N] += 1
        if self.touched is not None:
            self.touched.add(key)

    def pop(self, host, pid, start):
        """Take the entry for a process whose own record has come, `start` being its start (None if
        unknown). An entry that began over START_SLACK_S before it is an earlier process's, one that
        had the same pid: it is dropped, counted with the records it held (`refused`), and None is
        returned."""
        e = self.entries.pop((host, pid), None)
        if e is not None and start is not None and e[P_FIRST] < start - START_SLACK_S:
            self.refused += 1
            self.refused_records += e[P_N]
            return None
        return e

    def for_sids(self, sids):
        """The entries of these sessions, themselves and not copies: read, never changed."""
        return [e for e in self.entries.values() if e[P_SID] in sids]

    @staticmethod
    def loop_like(entry, poll_min, poll_cv):
        """True when some command in `entry` started `poll_min` times or more with its gaps' stdev
        over their mean at most `poll_cv`: a loop polling at a steady pace."""
        for st in entry[P_CMDS].values():
            n, mean, m2 = st[0], st[1], st[2]
            if n < max(poll_min, 2) or mean <= 0:
                continue
            if math.sqrt(max(m2, 0.0) / (n - 1)) <= poll_cv * mean:
                return True
        return False
