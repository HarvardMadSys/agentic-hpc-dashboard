"""Phases: what an agent was doing, read from the tool calls it made.

A session holds hundreds of tool calls, and a reader opening it wants a handful of labeled
stretches: exploring, editing, building, testing, running, Slurm jobs, waiting, git, and idle.
`segment` reads them from the calls a session's `PhaseLog` holds, named by `phase_rules`. It is a
pure function: a row is built again every few seconds from a log that goes on ingesting, so it
changes neither the log, nor the list of calls, nor the previews, and the same calls always give
the same phases. It reads only the commands the agent ran: reading and editing inside the agent, and
its thinking, leave no command. The block's `basis` also names the words the agent wrote in them,
and its `v` is 2: the reducer adds `tasks` and a `headline` to the block a row serves
(`phase_tasks`), and names them from those words. `segment` itself gives neither.

The rules, in the order they apply. The thresholds are DEFAULTS, tuned on two real days (below
them); the reducer passes the configured ones.

  Polling. One command, the same normalized text (`H`), started `poll_min` times or more at a
  near-regular pace is polling, and its calls are waiting whatever they are themselves: `squeue`
  every two minutes is waiting on Slurm. Near-regular is the population stdev of the gaps between
  its starts at most `poll_cv` of their mean, and at most `poll_cv_strict` for a test or a run:
  an agent re-runs a test after editing, at the uneven pace of its own pauses, which is an
  edit-test loop and not a wait, and the two rules would otherwise claim the same calls. An
  unknown call is never polling: an unknown is never read into a phase.

  Steps. Calls that start within `step_s` of a step's first call are that step, and guesses and
  loops read it as the first of CATS any of them has: Codex starts `git diff --check` in the
  same second as a test, and the pause before that second is the pause before the test.

  Idle. A step that starts `idle_s` or more after the step before it has ended follows an idle
  gap, from that end to its start: nothing the agent was waiting on was running, and no call
  started. It is the user's turn or the agent thinking, never a phase of work, and nothing merges
  into it. Only what the agent is still waiting on holds a gap (the user's ruling, 2026-11-02):
  the calls of the step just before it, however long one runs. Calls that start within `step_s`
  of a step's first call are that step, so a long call in a parallel batch still holds it. A call
  an earlier step started, still running when a later step begins, is one the agent moved past,
  and holds no gap and no guess. On rtx6000a on 2026-11-01 a `linktest` call hung for 50,533 s
  (pid 1538974); counted as running, it held off every gap in those 14 hours, one of 2 h 8 min
  among them. The gap lists the calls running as it begins (`alive`), three at most, the earliest
  started first; one may end inside the gap, and one still running (OPEN) is listed with no text,
  as in Spans. A process left running in the background (`past`) is not a call, so it does not
  keep a session from idling. It keeps the lane from ending, as a call the agent moved past does
  (Spans).

  Guesses. Claude Code and Codex edit files inside the agent, which leaves no command. A pause of
  `guess_min_s` or more (and under `idle_s`) before a test or a run, with no edit seen since the
  last test or run, is a guess that the agent was editing, made in exactly two places: between
  two runs of one command with only reading and git between them ("a pause between two runs of
  pytest -q"), and after reading ("a pause after reading, before the test run"). A guess is
  never called editing. It is a phase of its own, `edit_guess`, or a slice of the phase that
  takes it in, and either way it carries its reason in words (`guess`). An `edit` phase always
  holds a call whose category is edit.

  Loops. A test or a run re-run twice or more, each time after an edit, seen or guessed, with
  only reading and git besides, is one edit-test loop of that command, and its guessed edits are
  its `slices`. Two seen edits in a round are still one round. `rounds` counts every run of the
  command inside it, a run absorbed into it too: "N counts the runs". Two loops are one only
  when they repeat one command: three runs of `pytest a` and then three of `pytest b` are two.

  Absorption. Neighbours of one kind merge, idle never, and loops only of one command. From here
  a run of calls is named for its dominant category (as below), and neighbours of one kind merge
  again whenever one changes. Then, until no run is left that can be, the smallest run (by
  calls, then duration, then the earliest) with fewer than `absorb_calls` calls and shorter than
  `absorb_s`, never idle and never a loop, is absorbed. A guess becomes a slice of the call run
  after it (of the one before it, when none follows). A call run merges only into neighbours that
  hold calls: into both when they are of one kind, else into the longer, and with neither it
  stays, so a lone call is never swallowed by a guess or an idle gap. A run's duration here is
  its calls' own, from the first start to the last end: the time after it is not yet its. A run
  absorbed takes the kind of the one that took it in, and is not named again by its calls: on
  boslogin06 on 2026-10-28 four quick reads beside two loops that polled all day (apid 114602)
  outnumbered them, and named the day exploring. That kind is a run's base (R50), and its name
  unless the base is `unknown` while it holds a known call: then it is named for the known kind
  with the most calls, a tie going to the kind seen first, again at every join (R66, `_named`).
  "Commands not recorded" names a phase of unknown calls alone.

  Cuts. A caller that has cut the session into tasks gives the times it cut at (`cuts`, in any
  order), and no phase holds calls from both sides of one. A call that starts at a cut is after
  it. A step ends at a cut, however soon the next call starts. A cut is a run with no calls and
  no length, and it is left out of the phases. It ends a loop, nothing merges into it or across
  it, and it is never absorbed: a run beside a cut can join only the neighbour on its other
  side, and stays when it has none. The phase before a cut ends where the next begins, as it
  does anywhere (Spans). The pause before the first call after a cut belongs to the calls before
  it: an idle gap there is as it would be with no cut, and a guess there is a slice of the run
  before it, or a phase of its own when it is long. A cut with no call on one side changes
  nothing. The block's `cut`, below, is another thing: the calls the log dropped.

  Spans. Time goes to what the agent started most recently. A phase runs from its first start
  (its first slice's, when a guess came before its calls) to the next phase's, so a call left
  running in the background does not split the phases of the calls started meanwhile. The last
  phase ends when the last call does, or the last process one of them left behind, whichever
  phase it began in: the lane does not end while anything the agent started still runs. A call
  that ran past its phase is listed in it with how long (`ran_past`), as "train.py kept running
  for 2h 40m": its own end past the phase's, by more than PAST_SLACK_S (a second, the stamps'
  noise, as `past` itself is), or a process it left behind past its own end, the longer of the
  two. A call still running (OPEN) is never listed there, nor in `top`: its text is its first
  child's, no command of its own, so the phase says it is still running and lists the programs
  seen under it instead (`seen`, R65).

  Names. A phase is named for its dominant category, by calls, ties broken by summed duration
  and then by rule order, when its run is first formed; loop, edit_guess, and idle keep their own,
  and a run absorbed takes its absorber's (Absorption). When its own kind holds under half its
  calls the mix is listed, its own kind first. A wait says what it waits on: from the polling
  command with the most calls in it, with how often it ran and how many times in this phase, and
  the command that pace is named for (`every_cmd`), else from a wait call's own text (one still
  running first), with no pace. `top` lists the phase's own kind first (R70).

The block also carries the counts around the phases. `n_calls` is every call seen: the log holds
the first `max_calls` and counts the rest as `cut`, the earliest starting at `cut_at`, so the
phases are complete before it. `housekeeping` is what the agent ran that the model did not ask
for: how many, the top shapes, the minutes they began in, and `cut`, the ones counted but placed
in no minute (no time, a minute past `hk_bin_cap`, or under a process that never exited, which
the caller counts in `extra_hk`), so `n` is always the bins' sum plus `cut`. `never_exited` is how
many of `n` were under a process that never exited: an int, always there, 0 when none. They are not
known to be the agent's own, so a reader sets them apart from the rest, and `shapes` holds only the
top five, which may not include their shape.

Cost: O(n log n) in the calls, but for the calls the idle gaps list. The absorb pass takes its
smallest candidate from a heap, a run's calls are a range of the sorted list, and slices join
smaller into larger. The idle pass keeps the calls a later step has begun over in a heap by their
end: each enters it once and leaves it at most once. But each idle gap scans that heap whole for
the calls still running as it begins (`alive`), so the gaps take their number times the calls
still running. At the call cap, 20,000 calls, with half of them hung and an idle gap after each
of the others, `segment` took 1.66 to 1.84 s on a shared host, against 0.06 to 0.10 s over the
sessions `phase_tasks` times (2026-11-03). The cuts are sorted once, and a step looks up the next
cut by bisection.
"""
import heapq
import itertools
import math
from bisect import bisect_right
from operator import itemgetter

from .phase_log import CAT, E, FLAGS, H, OPEN, PAST, PAST_SLACK_S, PREV, S, SEEN
from .phase_rules import CATS, waits_on

# The thresholds (`reducers.trajectories.phases` in config, which the reducer passes). The absorb
# pair was tuned on two real days (boslogin06 2026-10-28, rtx6000a 2026-10-29) from 2 calls and
# 60 s: the sessions' phases per active hour went from a p90 of 11 and 20 to 4.3 and 6, and from a
# max of 60 and 30 to 14 and 8 (`phase_report`). A stretch shorter than an idle gap, with fewer
# than six calls, is not a phase of its own, so every guess, a pause shorter than an idle gap, is
# a slice of a phase: of the one after it, or of the one before it where a task's cut comes
# between them (Cuts).
DEFAULTS = {
    "idle_s": 300,              # a gap this long with no call starting or waited on is idle
    "guess_min_s": 20,          # the shortest pause before a test or a run read as editing
    "absorb_calls": 6,          # a run with fewer calls than this,
    "absorb_s": 300,            # and shorter than this, is absorbed into a neighbour
    "poll_min": 5,              # starts of one command before it can be polling
    "poll_cv": 0.25,            # polling: its gaps' stdev over their mean is at most this
    "poll_cv_strict": 0.10,     # the same, for a test or a run
    "step_s": 1.0,              # calls this close to a step's first call are that step
    "top_commands": 3,          # command previews listed per phase
    "max_calls": 20000,         # calls a session's log holds; the rest are counted (`cut`)
    "hk_bin_cap": 10080,        # minutes of housekeeping a session places in time: a week
    "pend_max": 50000,          # parents the pending join holds, at about 1 KB each
}

# What the block says it is read from. The phases are cut from the commands alone; the words the
# agent wrote in them name the tasks above the phases (`phase_tasks`), which a row's block carries.
BASIS = "read from the commands the agent ran and the words it wrote in them"
HK_SHAPES = 5               # housekeeping shapes listed
# The housekeeping shape of commands under a process that never exited, which the caller counts in
# `extra_hk`: they are not known to be the agent's own, so the block counts them apart (R43).
NEVER_EXITED = "under a process that never exited"
# Programs a phase says were seen under its calls still running (R65), as an open call carries them.
SEEN_SHOWN = 5
# Calls an idle gap says were still running as it began (`alive`), the earliest started first.
ALIVE_SHOWN = 3

_RANK = {c: i for i, c in enumerate(CATS)}
_RUNS = frozenset({"test", "run"})          # what a guess comes before, and a loop repeats
_READING = frozenset({"explore", "git"})    # what may lie between two runs of one command
_NO_CALLS = frozenset({"idle", "edit_guess", "cut"})    # the runs that hold no calls


def _rank(cat):
    return _RANK.get(cat, len(_RANK))


def _preview(previews, i):
    """The masked preview with id `i`; None for none."""
    return previews[i] if i and i < len(previews) else None


def _seen(c):
    """The programs seen under an open call (SEEN), or () for a call that has none."""
    return c[SEEN] if len(c) > SEEN and c[SEEN] else ()


def _open(c):
    return bool(c[FLAGS] & OPEN)


class _Run:
    """A stretch being cut into phases: a step, then a run of calls, a loop, a guess, an idle gap,
    or a cut. One that holds calls holds a range of the sorted calls, `lo` to `hi`, and per
    category how many and their summed duration. An idle gap holds, as `alive`, the indices of the
    calls still running as it begins. A cut is a marker where the caller cut the session: it holds
    no calls, begins and ends at the cut (`t0` and `t1`), and is no phase."""
    __slots__ = ("kind", "base", "lo", "hi", "first", "end", "counts", "t0", "t1", "reason",
                 "slices", "alive", "h", "pv", "idx", "left", "right", "ver", "dead")

    def __init__(self, kind, t0=None, t1=None, reason=None):
        self.kind = kind
        self.base = kind        # the kind it keeps when it takes a run in (R50); `kind` is its name
        self.lo = self.hi = 0
        self.first = None       # its calls' earliest start
        self.end = None         # its calls' latest end
        self.counts = {}        # category -> [calls, summed duration, the first's index]
        self.t0, self.t1 = t0, t1           # a guess's or an idle gap's own bounds
        self.reason = reason    # why a guess reads as editing
        self.slices = []        # (t0, t1, reason): the guesses it took in, in any order
        self.alive = ()         # an idle gap's: calls running as it begins, earliest first
        self.h = self.pv = 0    # a step's command and preview, from the call naming it; a loop's
        #                         command is `h` too
        self.idx = 0            # its place among the first runs: a tie goes to the earliest
        self.left = self.right = None
        self.ver = 0            # bumped when it changes, so a stale heap entry is passed over
        self.dead = False


def _holds(r):
    """True for a run that holds calls: not idle, not a guess, not a cut, and there."""
    return r is not None and r.kind not in _NO_CALLS


def _key(r):
    """What two neighbours must share to be one: their kind, and for a loop its command."""
    return ("loop", r.h) if r.kind == "loop" else r.kind


def _n(r):
    return 0 if r.kind in _NO_CALLS else r.hi - r.lo


def _dur(r):
    if r.kind in _NO_CALLS:
        return r.t1 - r.t0
    return max(0.0, r.end - r.first)


def _dominant(counts):
    """The category with the most calls, then the most time, then the first in rule order."""
    return min(counts.items(), key=lambda kv: (-kv[1][0], -kv[1][1], _rank(kv[0])))[0]


def _named(counts, kind):
    """`kind`, unless it is `unknown` and the run holds a call of a known kind: then the known
    kind with the most calls, a tie going to the one seen first (R66). A phase made only of
    unknown calls reads "commands not recorded"; one holding recorded calls is named for them, and
    its mix says the rest."""
    if kind != "unknown":
        return kind
    known = [kv for kv in counts.items() if kv[0] != "unknown"]
    return min(known, key=lambda kv: (-kv[1][0], kv[1][2]))[0] if known else kind


def _count(counts, cat, n, d, first):
    cur = counts.get(cat)
    if cur is None:
        counts[cat] = [n, d, first]
    else:
        cur[0] += n
        cur[1] += d
        cur[2] = min(cur[2], first)


def segment(calls, log, previews, params, guess=True, extra_hk=None, prepared=None, cuts=()):
    """The phase block for one session.

    `calls` are call tuples as `phase_log` lays them out, in any order; `log` is the session's
    PhaseLog, read for its housekeeping and its cut; `previews` are masked command texts by id,
    0 being none; `params` holds DEFAULTS' keys. With `guess` False no editing is guessed.
    `extra_hk` adds housekeeping commands by shape to the block's counts, and to nothing else.
    `prepared` is what `prepared(calls, previews, params)` returned, for a caller that has read the
    same calls with it already; the block is the same with it and without. `cuts` are the times the
    caller cut the session at, the start of each task after the first (Cuts): no phase holds calls
    from both sides of one. `segment` sorts them, so they may come in any order and repeat. None
    of them is changed.
    """
    cs, cats, poll = _prepared(calls, previews, params) if prepared is None else prepared
    cuts = sorted(cuts)
    items = _pauses(_steps(cs, cats, params["step_s"], cuts), cs, previews, params, guess, cuts)
    head = _absorb(_runs(_loops(items)), params)
    return {
        "v": 2, "basis": BASIS,
        "n_calls": len(cs) + log.cut, "cut": log.cut,
        "cut_at": None if log.cut_at is None else round(log.cut_at, 3),
        "housekeeping": _housekeeping(log, extra_hk),
        "list": _phases(head, cs, cats, previews, poll, params["top_commands"]),
    }


def prepared(calls, previews, params):
    """What `segment` reads first, as `(cs, cats, poll)`, for a caller that cuts the same calls
    another way too (the tasks do).

    `cs` is `calls` sorted by start. `cats` is the category of each, with every call of a command
    on a timer marked as waiting (`_polling`). `poll` maps the index in `cs` of each such call to
    its polling group: the tuple `(waits_on, mean gap, calls, command, index of its first call)`,
    hashable and compared by value, never an int id. Two calls are one command on a timer when
    their `poll` values are equal, which is how a caller folds polls. It is read once and given to
    both: `segment` and the task cutter change none of it, and nothing given here is changed."""
    cs = sorted(calls, key=itemgetter(S))
    cats = [c[CAT] for c in cs]
    return cs, cats, _polling(cs, cats, previews, params)


_prepared = prepared            # `segment` has a parameter of the same name


def _polling(cs, cats, previews, p):
    """Mark the calls of every command on a timer as waiting, in `cats`. Returns the group of each
    such call, by its index in `cs`: (what it waits on, its mean gap, its calls, the command it
    names, the index of its first call). That last orders two groups, the earlier first, and tells
    them apart. What a group waits on is read from its first text, an open call's included
    (R60); the command it names is a finished call's, else the program run most under one still
    running (R65, its `seen` first): an open call's text is its first child's, no command of its own."""
    by_h = {}
    for i, c in enumerate(cs):
        if c[H] and c[CAT] != "unknown":
            by_h.setdefault(c[H], []).append(i)
    poll = {}
    for idx in by_h.values():
        if len(idx) < max(p["poll_min"], 2):
            continue
        gaps = [cs[b][S] - cs[a][S] for a, b in zip(idx, idx[1:])]
        mean = sum(gaps) / len(gaps)
        if mean <= 0:
            continue
        sd = math.sqrt(sum((g - mean) ** 2 for g in gaps) / len(gaps))
        cat = min((cs[i][CAT] for i in idx), key=_rank)
        if sd > mean * (p["poll_cv_strict"] if cat in _RUNS else p["poll_cv"]):
            continue
        text = next((t for t in (_preview(previews, cs[i][PREV]) for i in idx) if t), None)
        cmd = next((t for t in (_preview(previews, cs[i][PREV]) for i in idx
                                if not _open(cs[i])) if t), None)
        if cmd is None:
            cmd = next((s[0] for s in (_seen(cs[i]) for i in idx) if s), None)
        group = (waits_on(text, cat), mean, len(idx), cmd, idx[0])
        for i in idx:
            cats[i] = "wait"
            poll[i] = group
    return poll


def _next_cut(cuts, t):
    """The first of `cuts`, which are sorted, that is later than `t`; infinity when none is. A call
    that starts at a cut is after it, so a cut at `t` itself is not one this finds."""
    i = bisect_right(cuts, t)
    return cuts[i] if i < len(cuts) else math.inf


def _steps(cs, cats, step_s, cuts=()):
    """Calls that start within `step_s` of a step's first call, as one run each, named for the
    highest category in rule order and for the command of the first call that has it. A step ends
    at a cut, so none holds calls from both sides of one; `cuts` is a sorted sequence of times."""
    out, i, n = [], 0, len(cs)
    while i < n:
        t, j = cs[i][S], i + 1
        edge = _next_cut(cuts, t)
        while j < n and cs[j][S] - t <= step_s and cs[j][S] < edge:
            j += 1
        k = min(range(i, j), key=lambda x: _rank(cats[x]))
        st = _Run(cats[k])
        st.h, st.pv = cs[k][H], cs[k][PREV]
        st.lo, st.hi, st.first = i, j, t
        st.end = max(cs[x][E] for x in range(i, j))
        for x in range(i, j):
            _count(st.counts, cats[x], 1, max(0.0, cs[x][E] - cs[x][S]), x)
        out.append(st)
        i = j
    return out


def _pauses(steps, cs, previews, p, guess, cuts=()):
    """The steps, with an idle gap before each that follows `idle_s` of nothing the agent waits on,
    and a guess before each test or run that follows a pause read as editing.

    Only what the agent is still waiting on holds a gap: the calls of the step just before it,
    which count as one. A call an earlier step started and a later one began over is one the agent
    moved past; it holds neither a gap nor a guess, and an idle gap lists up to ALIVE_SHOWN of them
    that run as it begins (`alive`, as indices into `cs`).

    A cut, one of `cuts` (a sorted sequence), that lies between two steps is a run put just before
    the later step, after any idle gap or guess there: the pause belongs to the calls before the
    cut. The rules for idle and for guesses read across a cut as they would with none."""
    out = []
    busy = None             # the latest end of the last step's own calls
    edited = False          # an edit step since the last test or run step, or the start
    last = None             # the latest step that is neither reading nor git
    moved = []              # (end, index) of calls the agent moved past that may still run
    for k, st in enumerate(steps):
        if busy is not None:
            gap = st.first - busy
            if gap >= p["idle_s"]:
                idle = _Run("idle", busy, st.first)
                idle.alive = heapq.nsmallest(ALIVE_SHOWN, (i for end, i in moved if end > busy))
                out.append(idle)
            elif guess and st.kind in _RUNS and gap >= p["guess_min_s"] and not edited:
                why = None
                if last is not None and last.kind in _RUNS and st.h and last.h == st.h:
                    why = "a pause between two runs of %s" % (
                        _preview(previews, st.pv) or "the same command")
                elif steps[k - 1].kind == "explore":
                    why = "a pause after reading, before the %s" % (
                        "test run" if st.kind == "test" else "run")
                if why:
                    out.append(_Run("edit_guess", busy, st.first, why))
            # This step begins over the one before it, so that one's calls are moved past if they
            # still run. A call that ended by this step's start ended before any later gap begins,
            # since steps only start later: it leaves.
            for i in range(steps[k - 1].lo, steps[k - 1].hi):
                heapq.heappush(moved, (cs[i][E], i))
            while moved and moved[0][0] <= st.first:
                heapq.heappop(moved)
            edge = _next_cut(cuts, steps[k - 1].first)
            if edge <= st.first:        # a cut lies between the two steps
                out.append(_Run("cut", edge, edge))
        out.append(st)
        busy = st.end
        if st.kind in _RUNS:
            edited = False
        elif st.kind == "edit":
            edited = True
        if st.kind not in _READING:
            last = st
    return out


def _loops(items):
    """Each maximal edit-test loop in `items` as one run. A cut ends a loop, as an idle gap does."""
    out, i, n = [], 0, len(items)
    while i < n:
        it = items[i]
        if it.kind in _RUNS and it.h:
            last, rounds = _loop_end(items, i)
            if rounds >= 2:
                out.append(_loop(items[i:last + 1]))
                i = last + 1
                continue
        out.append(it)
        i += 1
    return out


def _loop_end(items, i):
    """The last run of `items[i]`'s command in the loop it would start, and the rounds before it
    that each hold an edit, seen or guessed, among reading and git."""
    h, last, rounds, edited = items[i].h, i, 0, False
    for k in range(i + 1, len(items)):
        it = items[k]
        if it.kind in ("edit", "edit_guess"):
            edited = True
        elif it.kind in _RUNS and it.h == h and edited:
            last, rounds, edited = k, rounds + 1, False
        elif it.kind not in _READING:
            break
    return last, rounds


def _loop(parts):
    r = _Run("loop")
    r.h = parts[0].h
    steps = [it for it in parts if it.kind != "edit_guess"]
    r.lo, r.hi, r.first = steps[0].lo, steps[-1].hi, steps[0].first
    r.end = max(st.end for st in steps)
    for st in steps:
        for cat, (n, d, first) in st.counts.items():
            _count(r.counts, cat, n, d, first)
    r.slices = [(g.t0, g.t1, g.reason) for g in parts if g.kind == "edit_guess"]
    return r


def _take(a, b):
    """Add to `a` the calls of `b`, the run right after it."""
    a.hi = b.hi
    a.end = max(a.end, b.end)
    for cat, (n, d, first) in b.counts.items():
        _count(a.counts, cat, n, d, first)
    if len(a.slices) < len(b.slices):
        a.slices, b.slices = b.slices, a.slices
    a.slices.extend(b.slices)
    a.ver += 1


def _runs(items):
    """Neighbours of one kind merged, loops only of one command; idle, guesses, and cuts never
    are, so nothing merges across a cut."""
    out = []
    for it in items:
        if out and _holds(it) and _key(it) == _key(out[-1]):
            _take(out[-1], it)
        else:
            out.append(it)
    return out


def _unlink(r):
    if r.left is not None:
        r.left.right = r.right
    if r.right is not None:
        r.right.left = r.left
    r.dead = True


def _join(a, b, keep):
    """Merge `b`, the run right after `a`, into `a`, which keeps from then on the kind of `keep`,
    the run that took the other in (R50); returns `a`. That kind is its base: when it is `unknown`
    the run is named for the known kind it holds the most calls of (`_named`, R66), as many as it
    holds now. A loop stays a loop of its own command: two loops are only ever joined when they
    share it."""
    base = keep.base
    _take(a, b)
    if b.kind == "loop" and a.kind != "loop":
        a.h = b.h
    a.base = base
    a.kind = _named(a.counts, base)
    _unlink(b)
    return a


def _settle(r):
    """Merge `r` with its neighbours while one is of its kind; returns what it became."""
    while True:
        if _holds(r.left) and _key(r.left) == _key(r):
            r = _join(r.left, r, r)
        elif _holds(r.right) and _key(r.right) == _key(r):
            r = _join(r, r.right, r)
        else:
            return r


def _longer(a, b):
    """True when `a` is the longer of two runs, by duration, then calls; a tie goes to `a`."""
    return (_dur(a), _n(a)) >= (_dur(b), _n(b))


def _absorb(runs, p):
    """Absorb the small runs, smallest first, until none is left that can be. Returns the first
    run of what is left, linked by `right`. A cut is never absorbed and holds no calls, so a run
    beside one has only its other neighbour to join; the cuts are taken out at the end."""
    for k, r in enumerate(runs):
        r.idx = k
        r.left = runs[k - 1] if k else None
        r.right = runs[k + 1] if k + 1 < len(runs) else None
    for r in runs:                      # from here a run of calls is named as it will be
        if _holds(r) and r.kind != "loop":
            r.base = _dominant(r.counts)
            r.kind = _named(r.counts, r.base)
    for r in runs:
        if not r.dead and _holds(r):
            _settle(r)
    most, short = p["absorb_calls"], p["absorb_s"]
    heap, tick = [], itertools.count()

    def offer(r):
        n, d = _n(r), _dur(r)
        if r.kind not in ("idle", "loop", "cut") and n < most and d < short:
            heapq.heappush(heap, (n, d, r.idx, next(tick), r.ver, r))

    for r in runs:
        if not r.dead:
            offer(r)
    while heap:
        *_, ver, r = heapq.heappop(heap)
        if r.dead or r.ver != ver:
            continue
        if r.kind == "edit_guess":
            to = r.right if _holds(r.right) else r.left if _holds(r.left) else None
            if to is None:
                continue                # no call run beside it: it stays
            to.slices.append((r.t0, r.t1, r.reason))
            a, b = r.left, r.right
            _unlink(r)
            if _holds(a) and _holds(b) and _key(a) == _key(b):
                offer(_settle(a))       # its neighbours meet
            continue
        a = r.left if _holds(r.left) else None
        b = r.right if _holds(r.right) else None
        if a is not None and b is not None and _key(a) == _key(b):
            m = _join(_join(a, r, a), b, a)
        elif a is not None and (b is None or _longer(a, b)):
            m = _join(a, r, a)
        elif b is not None:
            m = _join(r, b, b)
        else:
            continue                    # no neighbour holds calls: it stays
        offer(_settle(m))
    for r in runs:
        if r.kind == "cut":             # it has kept its two sides apart: it is no phase
            _unlink(r)
    return next((r for r in runs if not r.dead), None)


def _phases(head, cs, cats, previews, poll, top_n):
    runs = []
    r = head
    while r is not None:
        runs.append(r)
        r = r.right
    starts = [r.t0 if r.kind in _NO_CALLS else min([r.first] + [s[0] for s in r.slices])
              for r in runs]
    # The lane ends when the last call does, or the last process one left behind, whichever
    # phase it began in: until then something the agent started is still running.
    last = max((c[E] if c[PAST] is None else max(c[E], c[PAST]) for c in cs), default=None)
    out = []
    for k, r in enumerate(runs):
        if r.kind == "idle":
            t1 = r.t1
        elif k + 1 < len(runs):
            t1 = starts[k + 1]
        else:
            t1 = max(last, starts[k])
        out.append(_phase(r, starts[k], t1, cs, cats, previews, poll, top_n))
    return out


def _ran_past(c, t1):
    """How long call `c` ran past its phase, which ended at `t1`: its own end past `t1` (by more
    than PAST_SLACK_S, the stamps' noise, as for `past`), or a process it left behind past its own
    end, whichever is longer. None when neither."""
    over = [] if c[PAST] is None else [c[PAST] - c[E]]
    if c[E] - t1 > PAST_SLACK_S:
        over.append(c[E] - t1)
    return max(over) if over else None


def _phase(r, t0, t1, cs, cats, previews, poll, top_n):
    """One phase's dict. A call still running is counted in `n` and its kind, and is never named
    by its first child's text (R65): not in `top`, never in `ran_past` (the phase says it is still
    running instead), and `seen` lists the programs seen under the open calls, up to SEEN_SHOWN
    names (None in a phase with none). `every_cmd` is the command a wait's pace is named for.
    An idle phase lists the calls running as it began (`alive`): `[preview, since]` for each, the
    earliest start first. An open call's preview is None, as it is never named by its first
    child's text. Every other phase, and an idle one with nothing running, has None."""
    p = {"kind": r.kind, "t0": round(t0, 3), "t1": round(t1, 3), "n": _n(r),
         "mix": [], "top": [], "guess": None, "slices": None, "rounds": None,
         "waits_on": None, "every_s": None, "times": None, "every_cmd": None, "open": False,
         "seen": None, "ran_past": None, "alive": None}
    if r.kind == "idle":
        p["alive"] = [[None if _open(cs[i]) else _preview(previews, cs[i][PREV]),
                       round(cs[i][S], 3)] for i in r.alive] or None
        return p
    if r.kind == "edit_guess":
        p["guess"] = r.reason
        return p
    calls = cs[r.lo:r.hi]
    ranked = sorted(r.counts.items(), key=lambda kv: (-kv[1][0], -kv[1][1], _rank(kv[0])))
    own = r.counts.get(r.kind)
    if own is not None:
        # its own kind first: a run another took in is of the kind of the one that took it
        ranked = [(r.kind, own)] + [kv for kv in ranked if kv[0] != r.kind]
    if ranked[0][1][0] * 2 < len(calls):
        p["mix"] = [[cat, v[0]] for cat, v in ranked[:3]]
    # `top`: the previews of the phase's own kind first (a loop's is the test it repeats), then by
    # count, then the earlier (R70): with every preview distinct, a wait's top three were the
    # `git add` and `git commit` before its polls
    mine = "test" if r.kind == "loop" else r.kind
    shown, first, own_kind = {}, {}, set()
    for k, c in enumerate(calls):
        pv = c[PREV]
        if _open(c) or _preview(previews, pv) is None:
            continue
        shown[pv] = shown.get(pv, 0) + 1
        first.setdefault(pv, k)
        if cats[r.lo + k] == mine:
            own_kind.add(pv)
    best = sorted(shown, key=lambda pv: (pv not in own_kind, -shown[pv], first[pv]))[:top_n]
    p["top"] = [[previews[pv], shown[pv]] for pv in best]
    if r.slices:
        sl = sorted(r.slices, key=itemgetter(0, 1))
        p["slices"] = [[round(a, 3), round(b, 3)] for a, b, _why in sl]
        p["guess"] = "; ".join(dict.fromkeys(why for _a, _b, why in sl))
    if r.kind == "loop":
        p["rounds"] = sum(1 for c in calls if c[H] == r.h)      # every run, absorbed ones too
    if r.kind == "wait":
        p["waits_on"], p["every_s"], p["times"], p["every_cmd"] = _waits(r, cs, cats, previews,
                                                                         poll)
    opened = [c for c in calls if _open(c)]
    p["open"] = bool(opened)
    if opened:
        names = dict.fromkeys(name for c in opened for name in _seen(c))
        p["seen"] = list(names)[:SEEN_SHOWN]
    ran = []
    for c in calls:
        s = None if _open(c) else _ran_past(c, t1)
        if s is not None:
            ran.append([_preview(previews, c[PREV]), round(s, 1)])
    p["ran_past"] = ran or None
    return p


def _waits(r, cs, cats, previews, poll):
    """What a wait phase waits on, every how many seconds, how many times, and the command that
    pace is named for: from the polling command with the most calls in it, else from its wait
    calls' own text, one still running first, with no pace."""
    by = {}
    for i in range(r.lo, r.hi):
        g = poll.get(i)
        if g is not None:
            by[g] = by.get(g, 0) + 1
    if by:
        g = min(by, key=lambda g: (-by[g], g[4]))       # the most calls here, then the earlier
        on, mean, _n_all, cmd, _first = g
        return on, round(mean, 1), by[g], cmd
    own = [i for i in range(r.lo, r.hi) if cats[i] == "wait"]
    pick = [i for i in own if cs[i][FLAGS] & OPEN] or own
    texts = {}
    for i in pick:
        texts[cs[i][PREV]] = texts.get(cs[i][PREV], 0) + 1
    tally = {}
    for pv, n in texts.items():
        on = waits_on(_preview(previews, pv), "wait")
        tally[on] = tally.get(on, 0) + n
    return (max(tally, key=tally.get) if tally else None), None, None, None


def _housekeeping(log, extra_hk):
    shapes = dict(log.hk)
    extra = never = 0
    for shape, n in (extra_hk or {}).items():
        if n > 0:
            shapes[shape] = shapes.get(shape, 0) + n
            extra += n
            if shape == NEVER_EXITED:
                never += n
    top = sorted(shapes.items(), key=lambda kv: (-kv[1], kv[0]))[:HK_SHAPES]
    return {"n": sum(shapes.values()),
            "shapes": [[shape, n] for shape, n in top],
            "bins": [[t, n] for t, n in sorted(log.hk_bins.items())],
            "cut": log.hk_cut + extra,
            "never_exited": never}
