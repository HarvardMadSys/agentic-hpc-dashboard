"""Tasks: the stretches of work in a session, cut from the tool calls it made.

The phases of a session (`phase_segment`) say how the agent spent its time: reading, editing,
testing, running, waiting. They do not say what the time was for, and a long session has dozens of
them. A task is a stretch of the session that was about one thing. `task_spans` cuts the calls into
tasks: it says which calls make up each task, and does not name them. `name_tasks` then names them,
and gives the session a headline. Both are pure functions of the calls as `phase_segment.prepared`
reads them and of each call's tag set, the names it worked on and the agent's own words in it
(`phase_names`). The same calls always give the same spans and the same names, and none of what
they are given is changed, but for the phases that `name_tasks` marks with their task. The
thresholds are DEFAULTS, tuned on seven hand-labeled sessions of boslogin06 and rtx6000a
(`phase_report`, 2026-11-03); the reducer passes the configured ones.

The rules of the cut, in the order they apply. A span is a task or a lone stretch. The spans are in
time order and hold every call once.

  Steps. Calls that start within `step_s` of a step's first call are one step, as the phases read
  them. Every cut falls between two steps.

  Gaps. The agent is waiting on the calls of the step it just made, and on a process a finished
  call left behind (`nohup job &`) while that runs. Holding ends at the later of the step's calls'
  ends and the `past` of any call that started at or before the step. A call from an earlier step
  that is still running when a later step begins is one the agent moved past, and holds nothing
  (the user's ruling, 2026-11-02, as for the idle gap of a phase). The gap is from where holding
  ends to the start of the next step, or from the step's own start when that is later. A gap of
  `gap_s` or more cuts. It lists the calls from earlier steps that are still running where holding
  ends (`alive`), the earliest started first, three at most.

  Stretches. The calls between two cutting gaps are a stretch. A stretch of at most `lone_calls`
  calls, all started within `lone_s`, is lone: a few calls hours from other work, set apart. It is
  no task, and nothing merges into it. A session whose stretches would all be lone has no other
  work for them to be hours from, so none is lone and each is a task. A session of one stretch is
  the plainest case. So every session with calls has at least one task.

  Names. The names of a step are the `(kind, name)` pairs of its calls' tag sets, with polls and
  bursts folded. Of the calls in a stretch that are one command on a timer (a polling group), the
  first adds its names and the rest add none, so a job polled a hundred times is named once. And a
  burst of one command counts once: a call whose normalized command (`h`) was counted within
  SAME_COMMAND_S before it, in the stretch, adds no names, so twenty subagents reading one file in
  a minute read it once. The window runs from the call that counted, so a command run every minute
  for an hour counts once every few minutes. A name is the pair, so a file that is read and then
  written is two names.

  New objects. A step that holds a name new to the session cuts before itself when the name recurs:
  in at least `new_steps` later steps of the stretch, the last within `new_window_s` of the step. A
  name is new where it first occurs in the session, in any stretch. A `grep` or `cd` name is no
  object, and no cut goes before the first step of a stretch.

  Closing. A step that holds the agent's words from a commit or a PR closes the burst of git it is
  in: the steps of git calls and nothing else, each within BURST_S of the one before. A commit in a
  command that does more (a test and a commit in one line) is no git step, and closes nothing. A
  burst ends at the latest end among its calls, which can be a call of an earlier step that outlasts
  its last, as a commit's hooks outlast a `git status` beside it. Bursts that each start within
  `close_series_s` of the end of the one before are a series, and a series closes once, after its
  last burst: commits made minutes apart are the commits of one piece of work. A burst on its own is
  a series of one. The cut goes after the series when the names in the `new_window_s` before its
  first burst and the `new_window_s` after its last share less than `close_overlap`. That is the
  Jaccard index of the two sets: the share of the names in either that are in both. Two sets with no
  names do not differ, and a set with names and one without share none. So a series that opens a
  stretch, with nothing before it, cuts whenever names follow it, and its commits stand as a piece
  of their own. They close work from before the gap, and joining them to the work after would label
  that work with a stale message.

  Merging. A stretch's pieces are taken in order. A piece shorter than `min_s`, or with fewer than
  `min_calls` calls, goes into the neighbour inside its stretch that it is more alike, by the
  Jaccard index of their names and the earlier on a tie, unless it holds words of the agent's (a
  commit, a PR, or a log). A piece's length is a span's (Times). The piece that took one in is
  taken next, so a run of small pieces goes together. Nothing merges across a cutting gap: a stretch
  of one piece keeps it, however short.

  Cap. The tasks of a local day merge down toward `cap`. A piece is the day its first call starts
  on (its `t0`). While a day holds more than `cap` pieces of tasks, the two neighbours of that day
  with the highest Jaccard index of names merge: two pieces that follow each other inside one
  stretch, the later of that day and the earlier of that day or the day before. The merged piece
  keeps the earlier's `t0`, so the merge lowers that day's count and leaves the day before's as it
  was, and a task can stay whole across midnight. On a tie, a pair whose pieces both start on that
  day goes before a pair across midnight, so that when nothing is alike a merge keeps the day's
  boundary, and then the earlier pair goes first. So the cap merges nothing across a cutting gap,
  and each day's budget is its own: a day with no piece of a task, as one with only lone calls,
  has none to lend another. A lone stretch is no piece. When the day has no such pair left it
  stops, over the cap. A local day is the process's own (`time.localtime`), the day that the date
  in a row's session id names.

  Times. A span begins at its first call's start. It ends where the next span of its stretch
  begins. The last span of a stretch ends where holding ended before the cutting gap that follows.
  The last span of the session ends at the latest end, or `past`, among its calls that the agent did
  not move past.

The rules of the naming. `name_tasks` takes the spans, the phase block cut at them (`segment` with
`cuts`), and the session's project, and gives each span a task. It marks each phase of the block
with the task it belongs to (`task`).

  Phases. A phase belongs to the task whose cut interval it lies in, by position: the phases before
  the first cut are the first task's, and those from one cut to the next are the next task's. A
  phase that holds calls is its first call's task, since none crosses a cut. One that holds none, an
  idle gap or a long guess, is the earlier task's: the idle gap of a cutting gap lies between that
  task's end and the next one's start, so times cannot place it. The tasks tile the phases in
  order, and `phases` is each one's `[first, end)` range. The last phase can end after the last
  task does, when a call the agent moved past outlives the session. It is still that task's, and
  the task ends where it did.

  Words. A task that holds the agent's own words is labeled with them, and `source` and `ref` say
  where they came from. A PR title wins over a commit subject, and a commit subject over a log
  entry. Of the PR titles in a task, or of the commit subjects, the last counts, since it closes
  the work. Of the log entries the first does, since it usually states the goal.

  Scores. Otherwise the label is built from the names of the task. Its names are the ones its
  calls hold, with polls and bursts folded as for the cuts (Names), counted in steps. A name scores
  the steps of the task that hold it, times log(1 + spans / spans that hold it), so that a name
  rare in the session beats one that is everywhere. The spans are every span, tasks and lone
  stretches alike.
  The score is multiplied by 1.5 in the task where the name first occurs, and by the weight of its
  kind (WEIGHTS): the objects a task works on count in full, a name the agent gave its own scratch
  0.8, a file only read 0.6, a recurring word 0.5, a folder it changed into 0.1, and what it
  searched for 0.05, so that the last two name a task only when little else does. In a session of
  two spans or more, a name that every span holds scores 0 and names none of them. With one span
  every name is in every span, and none would name it. A name that is the session's user, which
  the reducer passes in masked as the names are, names no span at all. The best two names that
  read differently label the task, for a file that is read and then written is two names and one
  `x.py`. Two names of one file show once too: a name that is the last path component of one
  before it, as the job `refresh.sbatch` is of the script `analysis/refresh.sbatch`, or a scratch
  name that is its last component without the extension, as `q` is of `scratchpad/q.py`, and the
  other way round. Two files of one basename in two folders are two names. A tie goes to the name
  that occurs first in the task.

  Verbs. The label begins with a verb for the category that holds the most time inside the task,
  and on a tie the one with more calls, then the one that comes first. A phase's time is cut to the
  task, so that a last phase that runs past it does not decide, and idle is left out. Categories
  that share a verb share their time: a loop counts as a test, a guessed edit as an edit, and a wait
  on Slurm as a Slurm run (VERBS). A test is "Edit and test" when the task holds an edit call, a
  guessed edit, or a long guess, and a build is "Install" when the task names a package. A task
  whose names are all search terms is a search, "Search for".

  Labels. A built label is the verb, a name, and " · " and a second name when there is one. A file
  or folder reads as `phase_names` kept it, by its last two path components, and a PR as `PR #235`.
  A task that names nothing is the verb and the project, or the verb alone when the session has no
  project. A task of unrecorded commands that names nothing says "Commands not recorded".

  Fields. A task is open when it holds a call still running. When the log cut calls (`cut_at`, the
  earliest start among the ones it dropped), a task is incomplete if it ends after `cut_at`, or if
  it is the last span: the dropped calls start at or after `cut_at`, so they may continue the last
  span though it ends before. A lone stretch at the end is the last span too. Its `gap` is the
  cutting gap before it, with the calls still running as it began. An open call has no preview
  there, as in a phase. Each call has the name the gap line calls it by. It is the first name of
  the first of these kinds that its tag set holds: a script it ran, a test, a job, and a folder it
  made (ALIVE_KINDS), shown as in a label. The hung call of rtx6000a on 2026-11-01 made a folder
  and changed into it, and ran no script, so it is `scratchpad/linktest`. An open call with none of
  these is named by the programs seen under it, joined as the page joins them for an open phase
  (`pgrep and sleep`), and a call with neither has none.

  Headline. The project, then up to `headline_names` names that two tasks or more hold, the most
  widely held first and on a tie the one that occurs first, joined by " · ". A lone stretch counts
  for none. A search term and a folder changed into are no object (NOT_AN_OBJECT) and are left
  out. On boslogin06 on 2026-10-09 they were 46 of the 108 names the headlines of its 1,233 agent
  rows would have listed (11 search terms, such as `RUNNING|PENDING`, and 35 folders). The
  session's user is left out (Scores), and so is a name that reads like the project or like one
  before it, and a name of the same file as one before it (Scores). The PRs among the rest are one,
  "PRs #229, #233". It is None when there is neither a project nor such a name.

Cost: the steps, the names, the fold of polls and bursts, and the new-object cuts are linear in
the calls. A series of closing bursts re-reads the steps within `new_window_s` on each side of
it, so the closing cuts take the number of series times the steps in two windows. A cutting gap
scans the calls before it for the ones still running, and stops at three, so when fewer than
three are running the gaps take their number times the calls. The merge reads each piece once.
The cap scans the pieces for each merge, so it is quadratic in the pieces it starts with, and it
is most of the time where many pieces go to it. At the call cap, 20,000 calls, on a shared host,
`task_spans` took (two runs of five, 2026-11-03):
- 0.06 to 0.09 s over a session of one project, one with 5,000 polls on a timer, and one with a
  commit every six calls 20 s apart, whose commits are one series
- 0.53 to 0.59 s with a commit every six calls 60 s apart: every commit closes, and the 3,333
  pieces merge down to 150, ten on each of the 15 local days the calls start on. It is the worst
  of these shapes
- 0.43 to 0.57 s with 2,857 pieces on one local day, each cut by a new file and holding a log
  entry, so none merges for being short: the cap merges them down to 10
- 0.11 to 0.13 s with a hung call alive through 200 gaps
- 0.25 to 0.27 s with a commit every ten calls, two seconds apart: the 2,000 pieces each hold a
  commit and go to the cap, which leaves ten on each of the two local days the calls start on

`segment` took 0.06 to 0.10 s over the same calls, and more where many idle gaps each list many
calls still running (its own cost paragraph).

`name_tasks` is linear in the calls and the names they hold. It cuts the steps again (`_steps`,
about half of its time), reads each call's names once, and scores the names of each span. At the
call cap it took 0.04 to 0.07 s over the same sessions (2026-11-03).
"""
import datetime
import math
import os
import time
from bisect import bisect_left

from .phase_log import E, FLAGS, H, OPEN, PAST, PREV, S, TAGS
from .phase_names import SOURCES
from .phase_segment import ALIVE_SHOWN, _preview, _seen, _steps
from .phase_segment import DEFAULTS as PHASE_DEFAULTS

DEFAULTS = {
    "gap_s": 3600,          # a gap this long with nothing the agent waits on cuts a task
    "lone_calls": 8,        # a stretch between cutting gaps with at most this many calls,
    "lone_s": 7200,         #   all started within this, is set apart
    "new_steps": 5,         # a name new to the session that recurs in this many steps,
    "new_window_s": 1800,   #   within this after it first occurs, cuts where it first occurs
    "close_overlap": 0.5,   # a closing commit cuts when the names around it share less than this
    "close_series_s": 300,  #   and commits this close to each other close once, after the last
    "min_s": 600,           # a task shorter than this,
    "min_calls": 5,         #   or with fewer calls, merges into a neighbour unless it holds words
    "cap": 10,              # a local day's pieces of tasks merge down toward this many
    "headline_names": 3,    # names the headline lists after the project
}

BURST_S = 120                               # git steps this close to one another are one burst
SAME_COMMAND_S = 180                        # a command run again this soon after it counted
_ONE_DAY = datetime.timedelta(days=1)       # a cap's pair may begin on the day before (`_cap`)
CLOSING = frozenset({"commit", "pr"})       # the sources of the words that close a piece of work
NOT_AN_OBJECT = frozenset({"grep", "cd"})   # the kinds of name that never begin a task

# Naming. A name's weight, by its kind, in its score (`_ranked`): the objects a task works on count
# in full, a name the agent gave its own scratch a little less, a file only read less again, then a
# recurring word. A folder it changed into, and least of all what it searched for, count so little
# that they name a task only when little else does: a session's commands `cd` into one folder at
# nearly every step, and a score counts steps, so at 0.3 that folder led labels such as "Wait on
# analysis · update_final" (rtx6000a, 2026-11-01).
WEIGHTS = {"run": 1.0, "test": 1.0, "write": 1.0, "job": 1.0, "pr": 1.0, "worktree": 1.0,
           "branch": 1.0, "dir": 1.0, "pkg": 1.0, "scratch": 0.8, "read": 0.6, "word": 0.5,
           "cd": 0.1, "grep": 0.05}
NEW_IN_TASK = 1.5       # a name that first occurs in a task counts this much more there
NAMES_SHOWN = 2         # names in a label, and in a task's `names`
# The kinds of name a gap line can call a call still running by, the first it holds winning
# (`_alive_name`): what it ran, tested, or submitted, and then a folder it made.
ALIVE_KINDS = ("run", "test", "job", "dir")

# The verb of a label, by the category that holds the most of the task's time (`_category`).
VERBS = {"test": "Test", "slurm": "Run on Slurm", "run": "Run", "build": "Build", "edit": "Edit",
         "git": "Commit", "wait": "Wait on", "explore": "Read", "unknown": "Run"}
_COUNTS_AS = {"loop": "test", "edit_guess": "edit"}     # phases of these kinds count as these
EDIT_AND_TEST = "Edit and test"         # a test, in a task that edits
INSTALL = "Install"                     # a build, in a task that names a package
SEARCH = "Search for"                   # a task whose names are all search terms
# The label of a task of unrecorded commands that names nothing: a phase of them is called so.
NOT_RECORDED = "Commands not recorded"


class Span:
    """A task or a lone stretch: the calls `lo` to `hi` of the sorted calls, from `t0` to `t1`.
    `kind` is `"task"` or `"lone"`. `gap` is None, or `(seconds, alive)` for the cutting gap just
    before it: how long it was, and the indices of the calls still running as it began."""
    __slots__ = ("kind", "lo", "hi", "t0", "t1", "gap")

    def __init__(self, kind, lo, hi, t0, t1, gap=None):
        self.kind, self.lo, self.hi, self.t0, self.t1, self.gap = kind, lo, hi, t0, t1, gap

    def __repr__(self):
        return "Span(%r, calls %d:%d, %.1f to %.1f, gap=%r)" % (
            self.kind, self.lo, self.hi, self.t0, self.t1, self.gap)


class _Stretch:
    """The calls between two cutting gaps: steps `a` to `b` of the session's steps, calls `lo` to
    `hi`. `gap` is the cutting gap before it (`Span.gap`), None for the first. `hold` is where
    holding ended before the gap after it, None for the last. `pieces` are what it is cut into."""
    __slots__ = ("a", "b", "lo", "hi", "gap", "hold", "lone", "pieces")


class _Piece:
    """A run of steps `a` to `b` in a stretch, calls `lo` to `hi`, from `t0`. `names` is the set of
    names its steps hold, and `words` is whether any call of it holds the agent's words."""
    __slots__ = ("a", "b", "lo", "hi", "t0", "names", "words")


def task_spans(prep, tagsets, params, step_s):
    """The spans of one session, in time order, covering every call once.

    `prep` is `phase_segment.prepared(calls, previews, params)`; `tagsets` is the reducer's table
    of tag sets by id, `(names, words)`, which each call names by its TAGS; `params` holds
    DEFAULTS' keys; `step_s` is the phases' own, which says what one step is. An OPEN call is read
    as any other is, by its first child's tag set. None of them is changed."""
    cs, cats, poll = prep
    if not cs:
        return []
    steps = _steps(cs, cats, step_s)
    stretches = _stretches(cs, steps, params)
    seen = set()                            # the names the session held before this stretch
    for st in stretches:
        names = _step_names(cs, steps, poll, tagsets, st.a, st.b)
        if st.lone:
            st.pieces = [_piece(cs, steps, st, 0, st.b - st.a)]
        else:
            words, closes, git = _step_flags(cs, cats, tagsets, steps, st)
            cuts = _new_object_cuts(steps, st, names, seen, params)
            cuts |= _closing_cuts(steps, st, names, closes, git, params)
            edges = [0, *sorted(cuts), st.b - st.a]
            pieces = [_piece(cs, steps, st, x, y, names, words)
                      for x, y in zip(edges, edges[1:])]
            st.pieces = _merge_short(cs, steps, st, pieces, params)
        for ns in names:
            seen |= ns                      # what the stretch held, the session holds from here
    _cap(stretches, params["cap"])
    return _spans(cs, steps, stretches)


def _stretches(cs, steps, params):
    """The runs of steps between cutting gaps, as `_Stretch`es in time order, each marked lone or
    not. The gap after step k is measured from where holding ends: the later of the step's calls'
    ends and any `past` of a call that started at or before it. A session whose stretches are all
    small enough to be lone has no other work for them to be hours from, so none is lone."""
    gap_s, out, a, gap, past = params["gap_s"], [], 0, None, None
    for k in range(len(steps) - 1):
        st = steps[k]
        for i in range(st.lo, st.hi):
            p = cs[i][PAST]
            if p is not None and (past is None or p > past):
                past = p
        hold = st.end if past is None else max(st.end, past)
        seconds = steps[k + 1].first - max(st.first, hold)
        if seconds >= gap_s:
            out.append(_stretch(cs, steps, a, k + 1, gap, hold, params))
            gap = (seconds, _alive(cs, st.lo, hold))
            a = k + 1
    out.append(_stretch(cs, steps, a, len(steps), gap, None, params))
    if all(st.lone for st in out):
        for st in out:
            st.lone = False
    return out


def _stretch(cs, steps, a, b, gap, hold, params):
    """The stretch of steps `a` to `b`, after the cutting gap `gap` and before the one that ends
    holding at `hold` (None for the last). It is lone if it is small: at most `lone_calls` calls,
    all started within `lone_s`. `_stretches` says whether a small one stays lone."""
    st = _Stretch()
    st.a, st.b, st.gap, st.hold, st.pieces = a, b, gap, hold, []
    st.lo, st.hi = steps[a].lo, steps[b - 1].hi
    st.lone = (st.hi - st.lo <= params["lone_calls"]
               and cs[st.hi - 1][S] - cs[st.lo][S] <= params["lone_s"])
    return st


def _alive(cs, hi, hold):
    """The calls among the first `hi` that are still running at `hold`, the earliest started
    first, ALIVE_SHOWN at most."""
    out = []
    for i in range(hi):
        if cs[i][E] > hold:
            out.append(i)
            if len(out) == ALIVE_SHOWN:
                break
    return out


def _folded_calls(cs, steps, poll, a, b):
    """The calls of steps `a` to `b`, one stretch, that add their names, as `(step, call)` in order,
    with polls and bursts folded: of the calls of one polling group in the stretch the first adds
    its names, and the rest add none; and a call whose command (`h`) was counted within
    SAME_COMMAND_S before it, in the stretch, adds none, the window running from that counted
    call. It is the one place calls are folded, for the cuts and the names."""
    folded, counted = set(), {}
    for k in range(a, b):
        for i in range(steps[k].lo, steps[k].hi):
            g = poll.get(i)
            if g is not None:
                if g in folded:
                    continue
                folded.add(g)
            h = cs[i][H]
            if h:
                since = counted.get(h)
                if since is not None and cs[i][S] - since <= SAME_COMMAND_S:
                    continue
                counted[h] = cs[i][S]
            yield k, i


def _step_names(cs, steps, poll, tagsets, a, b):
    """The names each of steps `a` to `b`, one stretch, holds, as a list of sets, with polls and
    bursts of one command folded (`_folded_calls`)."""
    out = [set() for _ in range(a, b)]
    for k, i in _folded_calls(cs, steps, poll, a, b):
        out[k - a].update(tagsets[cs[i][TAGS]][0])
    return out


def _step_flags(cs, cats, tagsets, steps, st):
    """Three lists, one entry for each step of the stretch: whether any of its calls holds the
    agent's words, whether any holds the words of a commit or a PR, and whether every call of it is
    a git call."""
    words, closes, git = [], [], []
    for k in range(st.a, st.b):
        w = c = False
        g = True
        for i in range(steps[k].lo, steps[k].hi):
            held = tagsets[cs[i][TAGS]][1]
            if held is not None:
                w = True
                if held[0] in CLOSING:
                    c = True
            if cats[i] != "git":
                g = False
        words.append(w)
        closes.append(c)
        git.append(g)
    return words, closes, git


def _new_object_cuts(steps, st, names, seen, params):
    """The steps of the stretch to cut before, as indices into `names`, for each name that is new to
    the session where it first occurs there and recurs: in `new_steps` later steps of the stretch,
    the last within `new_window_s` of the first. `seen` holds the names the session held before the
    stretch. It is not changed."""
    first = [steps[k].first for k in range(st.a, st.b)]
    hits = {}                       # each name of the stretch: the steps that hold it, in order
    for j, ns in enumerate(names):
        for n in ns:
            hits.setdefault(n, []).append(j)
    more, window = params["new_steps"], params["new_window_s"]
    return {at[0] for n, at in hits.items()
            if at[0] and n not in seen and n[0] not in NOT_AN_OBJECT
            and len(at) > more and first[at[more]] - first[at[0]] <= window}


def _closing_cuts(steps, st, names, closes, git, params):
    """The steps of the stretch to cut before, as indices into `names`: the one after each series
    of git bursts that a commit or a PR closes, when the names in the `new_window_s` before the
    series and after it differ. A burst is the steps of git calls and nothing else, each within
    BURST_S of the one before, and a step of it holds the words. It ends at the latest end among
    its calls, so a call that outlasts its last step counts. A series is the bursts that each start
    within `close_series_s` of the end of the one before, and it closes after its last."""
    first = [steps[k].first for k in range(st.a, st.b)]
    window, m, j, bursts = params["new_window_s"], len(names), 0, []
    while j < m:
        if not (git[j] and closes[j]):
            j += 1
            continue
        lo = hi = j
        while lo and git[lo - 1] and first[lo] - first[lo - 1] <= BURST_S:
            lo -= 1
        while hi + 1 < m and git[hi + 1] and first[hi + 1] - first[hi] <= BURST_S:
            hi += 1
        bursts.append((lo, hi))
        j = hi + 1
    ends = [max(steps[st.a + k].end for k in range(lo, hi + 1)) for lo, hi in bursts]
    cuts, n = set(), 0
    while n < len(bursts):
        lo, hi = bursts[n]
        while (n + 1 < len(bursts)
               and first[bursts[n + 1][0]] - ends[n] <= params["close_series_s"]):
            n += 1                          # the next burst is of the same series
            hi = bursts[n][1]
        n += 1
        if hi + 1 < m:
            before, after = set(), set()
            for i in range(lo - 1, -1, -1):
                if first[lo] - first[i] > window:
                    break
                before |= names[i]
            for i in range(hi + 1, m):
                if first[i] - first[hi] > window:
                    break
                after |= names[i]
            if _jaccard(before, after) < params["close_overlap"]:
                cuts.add(hi + 1)
    return cuts


def _jaccard(a, b):
    """How alike two sets of names are: the share of the names in either that are in both. Two
    empty sets do not differ, and are alike."""
    if not a and not b:
        return 1.0
    both = len(a & b)
    return both / (len(a) + len(b) - both)


def _piece(cs, steps, st, x, y, names=None, words=None):
    """The piece of the stretch's steps `x` to `y`, counted from its first."""
    p = _Piece()
    p.a, p.b = st.a + x, st.a + y
    p.lo, p.hi = steps[p.a].lo, steps[p.b - 1].hi
    p.t0 = cs[p.lo][S]
    p.names = set().union(*names[x:y]) if names else set()
    p.words = any(words[x:y]) if words else False
    return p


def _join(a, b):
    """The piece made of `a` and `b`, which is right after it. It is `a`, taken over: the names go
    into the larger set."""
    if len(a.names) < len(b.names):
        a.names, b.names = b.names, a.names
    a.names |= b.names
    a.b, a.hi = b.b, b.hi
    a.words = a.words or b.words
    return a


def _last_end(cs, steps, a, b):
    """The latest end, or `past` of a process left behind, among the calls of steps `a` to `b` that
    the agent did not move past: a call still running when the next step began is one it did."""
    latest = None
    for k in range(a, b):
        later = steps[k + 1].first if k + 1 < len(steps) else None
        for i in range(steps[k].lo, steps[k].hi):
            c = cs[i]
            if later is not None and c[E] > later:
                continue
            end = c[E] if c[PAST] is None else max(c[E], c[PAST])
            if latest is None or end > latest:
                latest = end
    return latest


def _end(cs, steps, st, piece, after):
    """Where `piece` ends, with `after` the piece that follows it in its stretch, None for the last:
    where the next begins, else where holding ended before the stretch's gap, else (the session's
    last piece) the end of what the agent did not move past."""
    if after is not None:
        return after.t0
    if st.hold is not None:
        return st.hold
    return _last_end(cs, steps, piece.a, piece.b)


def _merge_short(cs, steps, st, pieces, params):
    """The pieces of a stretch with the short ones merged, each into the neighbour it is more alike,
    the earlier on a tie, unless it holds the agent's words. One pass, in order, does what merging
    until nothing is left to merge does: a piece that goes into the one after it is looked at again
    as the larger piece it has become, and a piece that is left behind only grows, so it cannot
    become short."""
    pieces, out, i = list(pieces), [], 0
    while i < len(pieces):
        cur = pieces[i]
        before = out[-1] if out else None
        after = pieces[i + 1] if i + 1 < len(pieces) else None
        short = (_end(cs, steps, st, cur, after) - cur.t0 < params["min_s"]
                 or cur.hi - cur.lo < params["min_calls"])
        i += 1
        if not short or cur.words or (before is None and after is None):
            out.append(cur)
        elif after is None or (before is not None and _jaccard(cur.names, before.names)
                               >= _jaccard(cur.names, after.names)):
            _join(before, cur)
        else:
            pieces[i] = _join(cur, after)
    return out


def _cap(stretches, cap):
    """While a local day holds more than `cap` pieces of tasks, merge the two neighbours of that
    day with the highest Jaccard index of names, until it holds `cap` or has no such pair left. On
    a tie, a pair whose pieces both start on that day goes before a pair across midnight, and then
    the earlier pair goes first. A piece is the day its first call starts on (its `t0`). Two
    neighbours are of a day when the later is, inside one stretch, and the earlier is of that day
    or the day before. A merged piece keeps the earlier's `t0`, so a merge lowers the count of
    the later piece's day and leaves the earlier's as it was, and a task can stay whole across
    midnight. A lone stretch is no piece."""
    flat = [(si, p) for si, st in enumerate(stretches) if not st.lone for p in st.pieces]
    days = [datetime.date(*time.localtime(p.t0)[:3]) for _, p in flat]
    held = {}
    for day in days:
        held[day] = held.get(day, 0) + 1
    if all(n <= cap for n in held.values()):
        return

    def alike(k):
        """How alike pieces `k` and `k + 1` are, or -1 when they may not merge."""
        day = days[k + 1]
        if (flat[k][0] != flat[k + 1][0] or held[day] <= cap
                or days[k] not in (day, day - _ONE_DAY)):
            return -1.0
        return _jaccard(flat[k][1].names, flat[k + 1][1].names)

    def rank(k):
        """Which pair merges first: the most alike and, on a tie, a pair of one day before a pair
        across midnight. `max` takes the earlier of pairs that rank the same."""
        return pairs[k], days[k] == days[k + 1]

    pairs = [alike(k) for k in range(len(flat) - 1)]
    while True:
        k = max(range(len(pairs)), key=rank, default=None)
        if k is None or pairs[k] < 0:
            break
        day = days[k + 1]               # the later piece's day, which the merge lowers
        flat[k] = (flat[k][0], _join(flat[k][1], flat[k + 1][1]))
        del flat[k + 1], days[k + 1], pairs[k]
        held[day] -= 1
        if held[day] <= cap:            # the day is done: no pair of it merges any more
            pairs = [-1.0 if days[i + 1] == day else pairs[i] for i in range(len(pairs))]
        for i in (k - 1, k):
            if 0 <= i < len(pairs):
                pairs[i] = alike(i)
    for st in stretches:
        if not st.lone:
            st.pieces = []
    for si, p in flat:
        stretches[si].pieces.append(p)


def _spans(cs, steps, stretches):
    """The spans, in time order: one for each piece of a task stretch and one for a lone stretch.
    A span ends where the next piece of its stretch begins, else where its stretch does (`_end`),
    and the first of a stretch carries the cutting gap before it."""
    out = []
    for st in stretches:
        for k, p in enumerate(st.pieces):
            after = st.pieces[k + 1] if k + 1 < len(st.pieces) else None
            out.append(Span("lone" if st.lone else "task", p.lo, p.hi, p.t0,
                            _end(cs, steps, st, p, after), st.gap if k == 0 else None))
    return out


def name_tasks(spans, block, prep, tagsets, previews, params, project, cut_at,
               step_s=PHASE_DEFAULTS["step_s"], user=None):
    """`(tasks, headline)` for the spans of one session: a dict for each span, and the session's
    headline, a string or None.

    `spans` are `task_spans`' for `prep`, the same `phase_segment.prepared(...)` it was given;
    `block` is `phase_segment.segment(..., prepared=prep, cuts=...)` cut at them, and each of its
    phases gains `task`, the index of the span it belongs to. `tagsets` and `previews` are the
    tables the calls name. `params` holds DEFAULTS' keys. `project` is the session's project, or
    None. `cut_at` is the log's, when it cut the calls (the block's `cut_at`), else None. `step_s`
    is the one the spans were cut with, which says what a step is. `user` is the session's user,
    masked as the names are, or None: a name that is the user names no task and is no part of the
    headline. Nothing but the block's phases is changed.

    A task is `{kind, t0, t1, n, label, source, ref, names, phases, open, incomplete, gap, sid}`:
    `kind` is the span's, `n` its calls, `source` and `ref` where the agent's own words in `label`
    came from, else None and "", `names` the names the label is built from (shown as in it, at most
    NAMES_SHOWN), `phases` the `[first, end)` range of the block's list, `open` whether a call is
    still running in it, `incomplete` whether `cut_at` is set and it is the last span or ends after
    `cut_at`, `gap` the cutting gap before it as `{s, alive: [[preview or None, since, name or
    None], ...]}`, and `sid` None, for the caller that joins the day rows of one session. The
    rules are in the module's docstring."""
    cs, cats, poll = prep
    phases = block["list"]
    owners = _owners(phases, spans)
    for p, j in zip(phases, owners):
        p["task"] = j
    ranges = _ranges(owners, len(spans))
    held = _held(cs, cats, poll, tagsets, spans, step_s)
    with_name, first_in = _spread(held)
    tasks = []
    for j, x in enumerate(spans):
        mine = phases[slice(*ranges[j])]
        t0, t1 = round(x.t0, 3), round(x.t1, 3)
        shown = _shown(_ranked(j, held, with_name, first_in, user))
        words = _words(cs, tagsets, x)
        if words is None:
            source, ref = None, ""
            label = _label(_dominant(mine, t0, t1), _edits(cs, cats, x, mine),
                           any(kind == "pkg" for kind, _ in held[j]), shown, project)
        else:
            source, label, ref = words
        tasks.append({
            "kind": x.kind, "t0": t0, "t1": t1, "n": x.hi - x.lo, "label": label, "source": source,
            "ref": ref, "names": [text for _, text in shown], "phases": list(ranges[j]),
            "open": any(cs[i][FLAGS] & OPEN for i in range(x.lo, x.hi)),
            "incomplete": cut_at is not None and (j == len(spans) - 1 or x.t1 > cut_at),
            "gap": _gap(x, cs, previews, tagsets), "sid": None})
    return tasks, _headline(project, held, spans, params["headline_names"], user)


def _owners(phases, spans):
    """The span each phase of the block belongs to, by position: the cut interval it lies in (the
    phases before the first cut are the first span's, those between the k-th cut and the next the
    next one's). Phases that hold calls partition them in order, and none crosses a cut, so such
    a phase is its first call's span, and one that holds none (an idle gap, a long guess) lies
    before the cut it is at, and is the span before it. Times could not say which: the idle gap
    of a cutting gap lies between the earlier span's end and the later one's start."""
    if not spans:
        return []
    out, j, c = [], 0, 0
    for p in phases:
        if p["n"]:
            while j + 1 < len(spans) and spans[j].hi <= c:
                j += 1
            c += p["n"]
        out.append(j)
    return out


def _ranges(owners, count):
    """The `[first, end)` range of the phases that `owners` gives each of `count` spans, which they
    tile in order; a span that has none gets an empty range where it would be."""
    out, i = [], 0
    for j in range(count):
        first = i
        while i < len(owners) and owners[i] == j:
            i += 1
        out.append([first, i])
    return out


def _held(cs, cats, poll, tagsets, spans, step_s):
    """For each span, the names its calls hold, as `{name: steps}` in the order they first occur:
    how many of the span's steps hold the name. A name is `(kind, text)`. Polls and bursts are
    folded as `task_spans` folds them (`_folded_calls`), over each stretch (the spans from one
    with a cutting gap to the next): a poll after the first of its group in a stretch adds no
    names, wherever in the stretch it is, and neither does a run of a command that counted within
    SAME_COMMAND_S before it, though the two are in two spans.
    A step belongs to the span that holds its first call, which is the one that holds all of it for
    the `step_s` the spans were cut with."""
    steps = _steps(cs, cats, step_s)
    starts = [st.lo for st in steps]
    bounds = [(bisect_left(starts, x.lo), bisect_left(starts, x.hi)) for x in spans]
    owner = [0] * len(steps)
    for j, (a, b) in enumerate(bounds):
        owner[a:b] = [j] * (b - a)
    held, last = [{} for _ in spans], [{} for _ in spans]       # name -> steps, and its latest step
    j = 0
    while j < len(spans):
        end = j + 1
        while end < len(spans) and spans[end].gap is None:
            end += 1
        for k, i in _folded_calls(cs, steps, poll, bounds[j][0], bounds[end - 1][1]):
            counts, latest = held[owner[k]], last[owner[k]]
            for name in tagsets[cs[i][TAGS]][0]:
                if latest.get(name) != k:
                    latest[name] = k
                    counts[name] = counts.get(name, 0) + 1
        j = end
    return held


def _spread(held):
    """How the names lie over the spans, tasks and lone stretches alike: `(with_name, first_in)`,
    for each name how many spans hold it and the first that does."""
    with_name, first_in = {}, {}
    for j, names in enumerate(held):
        for name in names:
            with_name[name] = with_name.get(name, 0) + 1
            first_in.setdefault(name, j)
    return with_name, first_in


def _ranked(j, held, with_name, first_in, user=None):
    """The names that can name span `j`, the best first. A name scores the steps of the span that
    hold it, times log(1 + spans / spans that hold it), so that a name rare in the session beats
    one that is everywhere; times NEW_IN_TASK when it first occurs in the span, and times its
    kind's weight. A name in every span scores 0 and names none, when there are two spans or more:
    with one, every name is in every span, and none would name it. A name that is the session's
    `user` names none either. A tie keeps the order the names first occur in the span."""
    count, scored = len(held), []
    for name, steps in held[j].items():
        spans_with = with_name[name]
        if (count > 1 and spans_with == count) or (user is not None and name[1] == user):
            continue
        score = steps * math.log(1 + count / spans_with) * WEIGHTS[name[0]]
        scored.append((score * NEW_IN_TASK if first_in[name] == j else score, name))
    scored.sort(key=lambda s: -s[0])
    return [name for _, name in scored]


def _display(kind, name):
    """How a name reads in a label. A file or folder is already its last two path components, as
    `phase_names` reads it, so only a PR changes: `#235` is `PR #235`."""
    return "PR " + name if kind == "pr" else name


def _one_file(a, b):
    """Whether two names, `(kind, text)` as they read, are one file under two names: one text is
    the other's last path component (the job an sbatch script is named, `refresh.sbatch`, beside
    `analysis/refresh.sbatch`), or one is a scratch name and the other's last component without
    its extension (`q` beside `scratchpad/q.py`), which is how `phase_names` makes a scratch name.
    Two files of one basename in two folders are two files."""
    for (kind, short), (_, long) in ((a, b), (b, a)):
        last = long.rsplit("/", 1)[-1]
        if "/" in long and short == last:
            return True
        if kind == "scratch" and short != last and short == os.path.splitext(last)[0]:
            return True
    return False


def _shown(ranked):
    """The names a label shows, as `(kind, text)`: the best NAMES_SHOWN that read differently, for
    a file that is read and then written is two names, and one `x.py`. A name that is one file
    with a name shown before it (`_one_file`) is not shown either."""
    out, seen = [], set()
    for kind, name in ranked:
        text = _display(kind, name)
        if text not in seen and not any(_one_file((kind, text), shown) for shown in out):
            seen.add(text)
            out.append((kind, text))
            if len(out) == NAMES_SHOWN:
                break
    return out


def _words(cs, tagsets, x):
    """The agent's words that name span `x`, as `(source, line, ref)`, or None. The first of
    `phase_names.SOURCES` that the span holds wins, a PR title over a commit subject over a log
    entry. Of the PRs or the commits the last counts, for it closes the work, and of the log
    entries the first, for it usually states the goal."""
    found = {}
    for i in range(x.lo, x.hi):
        held = tagsets[cs[i][TAGS]][1]
        if held is not None and (held[0] in CLOSING or held[0] not in found):
            found[held[0]] = held
    return next((found[s] for s in SOURCES if s in found), None)


def _category(phase):
    """The category a phase's time counts toward: those that share a verb count together. A loop is
    a test, a guessed edit an edit, and a wait on Slurm a Slurm run."""
    kind = phase["kind"]
    if kind == "wait" and phase["waits_on"] == "slurm":
        return "slurm"
    return _COUNTS_AS.get(kind, kind)


def _dominant(mine, t0, t1):
    """The category that holds the most time of the phases `mine` inside `t0` to `t1`, idle left
    out, and on a tie the one with more calls, then the one that comes first. A phase's time is
    cut to the task: the last phase can run on past it, when a call the agent moved past outlives
    the session. "unknown" when none holds any."""
    held = {}
    for p in mine:
        if p["kind"] != "idle":
            got = held.setdefault(_category(p), [0.0, 0])
            got[0] += max(0.0, min(p["t1"], t1) - max(p["t0"], t0))
            got[1] += p["n"]
    return max(held, key=held.get) if held else "unknown"


def _edits(cs, cats, x, mine):
    """Whether span `x` holds edit time: a call that edits, whichever phase took it in, or a guess
    that the agent was editing, a phase of its own or a slice of another."""
    return (any(cats[i] == "edit" for i in range(x.lo, x.hi))
            or any(p["kind"] == "edit_guess" or p["slices"] for p in mine))


def _label(category, edits, packages, shown, project):
    """A built label: the verb for `category`, then the names `shown`, else the project, else the
    verb alone. A test is "Edit and test" when the task `edits`, a build is "Install" when the task
    names a package (`packages`), and a task whose names are all search terms is a search. A task of
    unrecorded commands that names nothing says so, as a phase of them does, and is no "Run" of the
    project."""
    if not shown and category == "unknown":
        return NOT_RECORDED
    if shown and all(kind == "grep" for kind, _ in shown):
        verb = SEARCH
    elif category == "test" and edits:
        verb = EDIT_AND_TEST
    elif category == "build" and packages:
        verb = INSTALL
    else:
        verb = VERBS[category]
    names = " · ".join(text for _, text in shown) or project
    return "%s %s" % (verb, names) if names else verb


def _gap(x, cs, previews, tagsets):
    """Span `x`'s cutting gap as `{s, alive}`, or None. `alive` is `[preview, since, name]` for each
    call still running as it began: the preview of an open call is None, as in a phase (R65), and
    the name is the one the gap line says (`_alive_name`)."""
    if x.gap is None:
        return None
    seconds, alive = x.gap
    return {"s": round(seconds, 3),
            "alive": [[None if cs[i][FLAGS] & OPEN else _preview(previews, cs[i][PREV]),
                       round(cs[i][S], 3), _alive_name(cs[i], tagsets)] for i in alive]}


def _alive_name(c, tagsets):
    """What a gap line calls a call still running, or None. It is the call's first name of the
    first kind in ALIVE_KINDS that its tag set holds (a script it ran, then a test, a job, and a
    folder it made), as a label shows it. Else, for an open call, it is the programs seen under it
    (SEEN), joined as the page joins them for an open phase: `pgrep and sleep`."""
    names = tagsets[c[TAGS]][0]
    for kind in ALIVE_KINDS:
        name = next((text for k, text in names if k == kind), None)
        if name is not None:
            return _display(kind, name)
    seen = [p for p in (" ".join(p.split()) for p in _seen(c)) if p]
    if len(seen) < 3:
        return " and ".join(seen) or None
    return "%s, and %s" % (", ".join(seen[:-1]), seen[-1])


def _headline(project, held, spans, limit, user=None):
    """The session's headline: the project, then up to `limit` names that two tasks or more hold,
    the most widely held first and on a tie the one that first occurs earlier, joined by ` · `.
    A lone stretch is no task and counts for none. A search term and a folder changed into are no
    object (NOT_AN_OBJECT), and are left out: a pattern such as `RUNNING|PENDING` is no headline.
    So is the session's `user`, a name that reads like the project or one before it, or one that is
    one file with one before it (`_one_file`), and the PRs among the rest are one, `PRs #a, #b`,
    where the first is. None when it would be empty."""
    holding, order = {}, {}
    for x, names in zip(spans, held):
        for name in names:
            order.setdefault(name, len(order))
            if x.kind == "task":
                holding[name] = holding.get(name, 0) + 1
    recurring = sorted((n for n, tasks in holding.items()
                        if tasks >= 2 and n[0] not in NOT_AN_OBJECT
                        and (user is None or n[1] != user)),
                       key=lambda n: (-holding[n], order[n]))
    chosen, seen = [], {project}
    for kind, name in recurring:
        if len(chosen) == limit:
            break
        text = _display(kind, name)
        if text not in seen and not any(_one_file((kind, text), (k, _display(k, n)))
                                        for k, n in chosen):
            seen.add(text)
            chosen.append((kind, name))
    prs = [name for kind, name in chosen if kind == "pr"]
    parts = [project] if project else []
    for kind, name in chosen:
        if kind != "pr":
            parts.append(name)
        elif name == prs[0]:
            parts.append(("PRs " if len(prs) > 1 else "PR ") + ", ".join(prs))
    return " · ".join(parts) or None
