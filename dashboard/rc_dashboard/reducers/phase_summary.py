"""A session at a glance: what kind of work it was, and the session in a few sentences.

A session's phases (`phase_segment`) say how the agent spent its time, its tasks (`phase_tasks`)
what each stretch of it was for, and its headline what it was about. None of them says what kind
of session it was, which a list of sessions, a filter, and a count over a day want. `summarize`
reads it from the block: a `category`, the `uses` it rests on, and a `summary`, a few sentences
built from the block's own numbers. It is a pure function of the block and the calls it was cut
from, as `phase_segment.prepared` sorts them, and of the tag sets they name (`phase_names`). It
changes none of them, and the same block always gives the same answer. Like the rest of the block it
is read from the commands the agent ran and from nothing else: the agent's own Read and Edit tools,
and its thinking, leave no command, so a session that only reads and edits with them has no tool
call.

The categories are the uses the Users tab counts a user's calls by (`portraits.USES`): `explore`
(read and search code and files), `develop` (change files from the shell, and test them), `build`
(build and install software), `run` (run programs), `hpc` (the scheduler: Slurm), and `git`. A
session is put under one when that use holds `mostly` of its work, or more, and is `mixed` when none
does. A session with no use is `wait` when it only waited, `unknown` when its commands were not
recorded, and `none` when it made no tool call at all.

  Work. The time of the block's phases, each from its start to the next one's, but idle gaps,
  waiting, and commands not recorded. A phase counts toward a use by its kind, as a task's verb
  counts it (`phase_tasks._category`): an edit, a guessed edit, a test, and an edit-test loop are
  `develop`, and a wait on Slurm is `hpc`, since a job is Slurm's work whether it is being submitted
  or waited on. Any other wait is the agent waiting on something it started (a process, a file, a
  fixed sleep), which is no use: it is said apart, in the summary, as `portraits.leverage` says a
  user's. So a session that submits a job and polls the queue for four hours is a Slurm session,
  and one that starts a script in the background and polls its log for four hours is whatever the
  rest of its work was.

  Time, not calls. Uses are weighed by the time their phases hold, as the band draws them and as a
  task's verb is chosen, and not by their calls as a user's uses are: a Slurm session's minute of
  `squeue` polls and a run's one long `python` call would otherwise be outnumbered by the quick
  reads around them. A block whose phases all have no length (a call or two that began and ended in
  the same second) is weighed by its calls instead. A tie goes to the use with more calls, then to
  the first of USES.

  Not recorded. A call whose command the collector did not record (`unknown`) is in no use. When
  such calls hold `mostly` of the time of the work and themselves, the session is `unknown`,
  whatever the rest is: most of it cannot be read.

The thresholds were read against the eight FAS RC login nodes (boslogin05-08, holylogin05-08) on
2026-11-12, 2026-11-13, and 2026-11-15 (`phase_report`): 16,630 sessions with an agent at their
root, 12,534 of them with no tool call. Of the 4,096 with one, 33% are `explore`, 29% `run`, 20%
`hpc`, 6% `mixed`, 5% `develop`, 4% `unknown`, 1% `wait`, and under 1% each `build` and `git`. At
`mostly` 0.5 a use holds the session in 94% of those that did any work, 87% at 0.6 and 98% at 0.4,
so `mixed` is the few that really are: their median is 126 calls and an hour of work. Weighed by
calls instead, about a third of the sessions would change category, most of them Slurm sessions
and long runs read as `mixed` or `explore`. One misreading is known: 114 sessions of one user's
Codex desktop app on 2026-11-15 hold two calls each, a `mkdir -p` and a `chmod 700` of a folder of
the user's own under `/var/tmp`, and are `develop`, as their tasks are `Edit`.

The summary is one to three sentences, each built from the block's own numbers and none from a
model: what the work was and its mix, the calls and tasks with what they named, and how long the
agent waited. A use is named beside the category only when it holds `also` of the work, and waiting
only when it holds `also` of the session's time but idle gaps. The work's length is said when it is
a second or more: a call or two of a few milliseconds is said by its calls alone. The names counted
are those the calls' tag sets hold, distinct, by kind (`phase_names`): a script run, a file written
or read, a test file, a Slurm job's name (the one it was given, else its script's: one a call), a
package or module, a search, a branch, and a pull request; and the commits whose subjects the agent
wrote in its commands. A call keeps at most `phase_names.MAX_NAMES` names, so a count is of the
names kept. Durations are written as the page writes them (`web/src/lib/format.ts`, `dur`).
"""
import collections
import decimal

from .phase_log import TAGS

USES = ("explore", "develop", "build", "run", "hpc", "git")     # as portraits.USES, a tie's order
MIXED, WAIT, UNKNOWN, NONE = "mixed", "wait", "unknown", "none"
CATEGORIES = USES + (MIXED, WAIT, UNKNOWN, NONE)

DEFAULTS = {
    "mostly": 0.5,      # the share of the work a use holds for the session to be put under it
    "also": 0.1,        # the share a second use, or waiting, needs for the summary to name it
}

# The use each phase kind counts toward. A wait on Slurm is `hpc` (`_use_of`); idle, any other
# wait, and commands not recorded count toward none.
USE_OF = {"explore": "explore", "edit": "develop", "edit_guess": "develop", "test": "develop",
          "loop": "develop", "build": "build", "run": "run", "slurm": "hpc", "git": "git"}
_TESTS = frozenset({"test", "loop"})        # the develop phases that test

# What the agent did, as a summary says it: the use, in the past tense.
DID = {"explore": "explored code and files", "develop": "edited and tested code",
       "build": "built and installed software", "run": "ran programs",
       "hpc": "ran jobs on Slurm", "git": "managed code in git"}
CHANGED = "changed files from the shell"    # develop with no test in it: edits alone
# What a wait waited on (`phase_rules.waits_on`), as the page's phase lines say it.
WAITS_ON = {"slurm": "Slurm", "process": "a process", "output": "a file or its output",
            "sleep": "a fixed sleep", "command": "something else"}
SOMETHING = "something"                     # a wait that says nothing of what it waited on

# The names a summary counts for each use, in the order it says them: (kind, one, many).
COUNTED = {
    "explore": (("read", "file read", "files read"), ("grep", "search", "searches")),
    "develop": (("write", "file written", "files written"), ("test", "test file", "test files")),
    "build": (("pkg", "package or module", "packages or modules"),
              ("run", "script run", "scripts run")),
    "run": (("run", "script run", "scripts run"), ("write", "file written", "files written")),
    "hpc": (("job", "Slurm job name", "Slurm job names"), ("run", "script run", "scripts run")),
    "git": (("branch", "branch", "branches"), ("pr", "pull request", "pull requests")),
}
COMMITS = ("develop", "git")                # the uses whose summary counts commits
COUNTS_SHOWN = 3                            # names counted in a summary, at most


def summarize(block, calls, tagsets, params=None):
    """`{category, uses, summary}` for a phase block. `block` is `phase_segment.segment`'s, with the
    `tasks` that `phase_tasks.name_tasks` gave it; `calls` are the calls it was cut from, as
    `phase_segment.prepared` sorts them (its `cs`); `tagsets` is the table their TAGS ids index;
    `params` holds DEFAULTS' keys, DEFAULTS when None.

    `category` is one of CATEGORIES. `uses` is `[[use, pct]]`, each use's share of the work in
    percent, rounded to one place, the most first and only those with a share: of the work's time,
    or of its calls when no work had a length. It is empty when the session did no work. `summary`
    is a string. The rules are in the module's docstring."""
    p = DEFAULTS if params is None else params
    w = _weigh(block["list"])
    n_calls = block.get("n_calls") or 0
    category, base, uses = _category(w, p["mostly"], n_calls)
    work = sum(base[u] for u in uses)
    named = _named(category, base, uses, work, p["also"])
    # a duration is said when the work took a second or more: `7 ms of work` says nothing
    timed = base is w.time and work >= 1.0
    return {"category": category,
            "uses": [[u, round(100.0 * base[u] / work, 1)] for u in uses],
            "summary": " ".join(s for s in (
                _what(category, w, timed, base, named, work, block),
                _counts(category, named, block, calls, tagsets),
                None if category == WAIT else _waited(w, p["also"])) if s)}


class _Weights:
    """A block's phases, weighed: by use, its work's seconds and calls; the seconds and calls of
    commands not recorded; the seconds of waiting by what it waited on, and the pace of the longest
    wait on each; the seconds of every phase but idle (`active`); and whether a develop phase
    tested."""
    __slots__ = ("time", "calls", "unk_s", "unk_n", "wait_s", "wait_n", "pace", "active",
                 "tested")

    def __init__(self):
        self.time, self.calls = collections.Counter(), collections.Counter()
        self.unk_s = self.active = 0.0
        self.unk_n = self.wait_n = 0
        self.wait_s, self.pace = collections.Counter(), {}
        self.tested = False


def _weigh(phases):
    w = _Weights()
    longest = {}
    for ph in phases:
        kind = ph["kind"]
        if kind == "idle":
            continue
        s = max(0.0, ph["t1"] - ph["t0"])
        n = ph["n"]
        w.active += s
        if kind == "wait":
            on = ph.get("waits_on")
            w.wait_s[on] += s
            w.wait_n += n
            if s > longest.get(on, -1.0):
                longest[on] = s
                w.pace[on] = ph.get("every_s")
        if kind == "unknown":
            w.unk_s += s
            w.unk_n += n
            continue
        use = _use_of(ph)
        if use is not None:
            w.time[use] += s
            w.calls[use] += n
            w.tested = w.tested or kind in _TESTS
    return w


def _use_of(ph):
    """The use a phase's time counts toward, or None."""
    if ph["kind"] == "wait":
        return "hpc" if ph.get("waits_on") == "slurm" else None
    return USE_OF.get(ph["kind"])


def _category(w, mostly, n_calls):
    """`(category, base, uses)`: the category, the amounts the uses are weighed by (seconds, or
    calls where no work and no command not recorded had a length), and the uses that have any of
    it, the most first."""
    by_time = sum(w.time.values()) + w.unk_s > 0
    base = w.time if by_time else w.calls
    unk = w.unk_s if by_time else w.unk_n
    uses = sorted((u for u in USES if base[u] > 0),
                  key=lambda u: (-base[u], -w.calls[u], USES.index(u)))
    work = sum(base[u] for u in uses)
    if not n_calls:
        return NONE, base, uses
    if unk > 0 and unk >= mostly * (work + unk):
        return UNKNOWN, base, uses
    if uses:
        return (uses[0] if base[uses[0]] >= mostly * work else MIXED), base, uses
    if w.wait_n or sum(w.wait_s.values()):
        return WAIT, base, uses
    return UNKNOWN, base, uses


def _named(category, base, uses, work, also):
    """The uses the summary names: the category's, then those that hold `also` of the work, three
    at most; for a mix, the two that hold the most and a third that holds `also`."""
    if category == MIXED:
        return uses[:2] + [u for u in uses[2:3] if base[u] >= also * work]
    if category in USES:
        return uses[:1] + [u for u in uses[1:3] if base[u] >= also * work]
    return []


def _what(category, w, timed, base, named, work, block):
    """The first sentence: what the work was, and its mix."""
    if category == NONE:
        hk = (block.get("housekeeping") or {}).get("n") or 0
        return "No tool calls, and %s of the agent's own." % _plural(hk, "command") \
            if hk else "No tool calls."
    if category == UNKNOWN:
        known = sum(w.time.values())
        if known and w.unk_s + known >= 1.0:
            return "Commands not recorded for %s of %s." % (_pct(w.unk_s, w.unk_s + known),
                                                            dur(w.unk_s + known))
        return "Commands not recorded."
    if category == WAIT:
        ons = _ranked_waits(w)
        if ons:
            return "Only waited: %s on %s." % (dur(w.wait_s[ons[0]]), _waits_on(ons[0]))
        return "Only waited."
    if category == MIXED:
        parts = ["%s (%s)" % (_did(u, w), _pct(base[u], work)) for u in named]
        return "Mixed work%s: %s." % (" (%s)" % dur(work) if timed else "", _and(parts))
    top = named[0]
    if base[top] >= 0.995 * work:
        return "Only %s%s." % (_did(top, w), " (%s of work)" % dur(work) if timed else "")
    of = "%s of %s of work" % (_pct(base[top], work), dur(work)) if timed else \
        "%s of its work" % _pct(base[top], work) if base is w.time else \
        "%s of its tool calls" % _pct(base[top], work)
    then = _and(["%s (%s)" % (_did(u, w), _pct(base[u], work)) for u in named[1:]])
    return "Mostly %s (%s)%s." % (_did(top, w), of, ", then " + then if then else "")


def _counts(category, named, block, calls, tagsets):
    """The second sentence: the calls, the tasks, and what the calls named for the uses the first
    sentence names. None for a session of no tool call."""
    n = block.get("n_calls") or 0
    if not n:
        return None
    tasks = [t for t in block.get("tasks") or () if t.get("kind") == "task"]
    head = _plural(n, "tool call")
    if len(tasks) > 1:
        head += " in %d tasks" % len(tasks)
        quoted = sum(1 for t in tasks if t.get("source"))
        if quoted == len(tasks):
            head += ", all named in the agent's own words"
        elif quoted:
            head += ", %d named in the agent's own words" % quoted
    said = []
    if named:
        names, commits = _names(calls, tagsets)
        for use in named:
            for kind, one, many in COUNTED[use]:
                k = len(names.get(kind, ()))
                if k:
                    said.append("%s %s" % (_fint(k), one if k == 1 else many))
            if use in COMMITS and commits:
                said.append(_plural(len(commits), "commit"))
        said = list(dict.fromkeys(said))[:COUNTS_SHOWN]
    return head + ("; " + ", ".join(said) if said else "") + "."


def _names(calls, tagsets):
    """`({kind: {name}}, {commit subject})`: the distinct names the calls hold, and the subjects of
    the commits they made. A Slurm job is named once by a call: by the name it was given
    (`--job-name`, `-J`), else by its script's. `phase_names` files an `sbatch` script's basename
    as a job too, so `sbatch --job-name=train job.sbatch` holds two job names for one job."""
    names, commits = {}, set()
    for c in calls:
        held, words = tagsets[c[TAGS]]
        scripts = {name.rsplit("/", 1)[-1] for kind, name in held if kind == "run"}
        jobs = [name for kind, name in held if kind == "job"]
        for name in [j for j in jobs if j not in scripts] or jobs:
            names.setdefault("job", set()).add(name)
        for kind, name in held:
            if kind != "job":
                names.setdefault(kind, set()).add(name)
        if words is not None and words[0] == "commit":
            commits.add(words[1])
    return names, commits


def _ranked_waits(w):
    """What the waits waited on, the longest first, a known target before an unknown one on a
    tie: only those that held time."""
    ons = sorted(w.wait_s, key=lambda k: (-w.wait_s[k], k is None, str(k)))
    return [on for on in ons if w.wait_s[on] > 0]


def _waited(w, also):
    """The third sentence: how long the agent waited, and on what, when it is `also` of the time
    of every phase but idle gaps; with the pace of the longest wait when there is one target."""
    total = sum(w.wait_s.values())
    if total <= 0 or total < also * w.active:
        return None
    ons = _ranked_waits(w)
    first = ons[0]
    text = "Waited %s on %s" % (dur(w.wait_s[first]), _waits_on(first))
    if len(ons) == 1:
        every = w.pace.get(first)
        if every:
            text += ", polling every %s" % _pace(every)
    else:
        rest = sum(w.wait_s[on] for on in ons[1:])
        text += " and %s on %s" % (dur(rest), _waits_on(ons[1]) if len(ons) == 2
                                   else "other things")
    return text + "."


def _did(use, w):
    return CHANGED if use == "develop" and not w.tested else DID[use]


def _waits_on(on):
    return WAITS_ON.get(on, SOMETHING)


def _pct(a, b):
    """A share as a summary says it: `94%`; `under 1%` for one that rounds to none, and `over 99%`
    for one that rounds to all but is not."""
    v = 100.0 * a / b if b else 0.0
    if v < 0.5:
        return "under 1%"
    if v >= 99.5 and a < b:
        return "over 99%"
    return "%s%%" % _fixed(v, 0)


def _and(parts):
    """`a`, `a and b`, `a, b, and c`: the serial comma, as the page joins a list."""
    if len(parts) < 3:
        return " and ".join(parts)
    return "%s, and %s" % (", ".join(parts[:-1]), parts[-1])


def _fint(n):
    return "{:,}".format(int(n))


def _plural(n, one, many=None):
    return "%s %s" % (_fint(n), one if n == 1 else (many or one + "s"))


def _fixed(v, places):
    """`v` to `places` decimals as JavaScript's `toFixed` writes it: the exact binary value
    rounded, a tie away from zero, so a duration reads here as it does on the page."""
    return str(decimal.Decimal(v).quantize(decimal.Decimal(1).scaleb(-places),
                                           rounding=decimal.ROUND_HALF_UP))


def dur(s):
    """A duration as the page writes one (`web/src/lib/format.ts`, `dur`): `850 ms`, `4.2 s`,
    `35 s`, `2.5 min`, `12 min`, `2.7 h`, `14 h`, `3.1 d`."""
    if s < 1:
        return "%s ms" % _fixed(s * 1000, 0)
    if s < 90:
        return "%s s" % _fixed(s, 1 if s < 10 else 0)
    if s < 5400:
        return "%s min" % _fixed(s / 60, 1 if s < 600 else 0)
    if s < 172800:
        return "%s h" % _fixed(s / 3600, 1 if s < 36000 else 0)
    return "%s d" % _fixed(s / 86400, 1)


def _pace(s):
    """A wait's pace as the page says one: `2 min`, `30 s`, never `2.0 min` (lib/phases.ts)."""
    return dur(s).replace(".0 ", " ")
