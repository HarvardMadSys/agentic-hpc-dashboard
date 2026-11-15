"""How each user works with coding agents: one portrait per account.

The Users tab reads it (`/api/users`, `/api/user`). Nothing here is reduced or
saved. It is composed, when it is read, from the reducers a window view already
holds -- the trajectories (every agent session in the window: its launch, its
calls, its phases and tasks), the sandbox (whether its tool calls ran isolated)
and Slurm (what it asked of the scheduler) -- and from the live tier, for what
is running now. So it keeps no state a saved window or hour must hold, a change
to it rebuilds nothing (it is not in `snapshots.CODE_FILES`, and no reducer
imports it), and it cannot disagree with the tabs those reducers fill.

A session here is one agent run. The Trajectories tab lists an agent that worked
past midnight as one row a day; here those rows are one session, as the agent's
phase block already treats them. Only rooted sessions count, as the Trajectories
plates count them: an agent process was seen at the root, so a status bar
probing `codex --version` every minute is no session. A session belongs to its
user, the account its records name first (`Session.user`).

A portrait has five parts, each read from one place:

  launch      the agent types; where each session is driven from (terminal,
              script, desktop app, IDE extension, web: `surface_of`); approval
              at launch, off the agent's own argv (bypassed, auto, no bypass
              flag); and the Sandbox tab's verdict on its tool calls
  rhythm      how many sessions, how long each was open (its first record to
              its last), how many were open at once, and the minutes in which a
              call began, by local hour and weekday
  work        tool calls by category (`phase_rules.CATS`), counted from each
              session's call log; the agent's own words in them (a commit
              message, a pull request's title, a log entry); the project each
              session worked in (`project_of`); and in one user's portrait, the
              time in each phase and the tasks, from the very phase blocks the
              Trajectories rows show
  footprint   processes, CPU, disk and network bytes and peak memory, summed as
              the Trajectories rows sum them
  scheduler   jobs, refusals, queue checks and polling loops of the user's agent
              sessions, folded as the Slurm tab folds them

Traits are plain statements about how a user's agents were used in the window
("launches with approval bypassed", "keeps several sessions open at once"). Each
comes from one rule over the numbers above, and the rule is served beside it
(`TRAITS`, `RULES`), so the page never shows a trait without saying why. None is
a judgement, none is made by a model, and a user can hold none.

The summary of how a user leverages their agents (`leverage`) puts the same
numbers together: the way most of their sessions are driven, the freedom the
agents start with, how many run at once, what the agents' actions are for
(`USES`), how much of it is waiting, what they work on, and when. The service
decides each part by RULES; the page words it (lib/portraits.ts), as a line
under each user in the roster and as the head of their portrait.

Like everything on the Trajectories tab, the work is read from commands: an edit
an agent makes with its own Edit tool, a file it reads with Read, and its
thinking leave no command. So the work mix counts what ran as a process, and an
agent that edits only through its own tools shows no editing.
"""
import collections
import math
import time
from operator import itemgetter

from .reducers import pct
from .reducers.phase_log import CAT, PREV, S, TAGS
from .reducers.phase_rules import CATS, PHASE_AGENTS
from .reducers.sandbox import session_state
from .reducers.slurm import SlurmReducer
from .reducers.trajectories import _agent_key

# What a trait needs (TRAITS). A share of sessions is over the sessions whose
# value is known; a share of calls is over the calls the logs hold, and only
# once they hold `min_calls`: a share of a handful of calls is noise.
RULES = {
    "interactive": 0.6,     # sessions driven from the agent's own terminal UI
    "scripted": 0.5,        # sessions driven from a script: claude -p, codex exec, an SDK
    "app": 0.5,             # sessions driven from a desktop app, an IDE extension, or the web
    "bypass": 0.5,          # sessions started with a bypass flag, of those whose launch is known
    "auto": 0.5,            # sessions started in auto mode, likewise
    "sandboxed": 0.5,       # sessions some or all of whose tool calls ran isolated, of those judged
    "parallel": 3,          # sessions open at one moment
    "long_s": 7200,         # a median session open this long or longer
    "short_n": 10,          # this many sessions or more,
    "short_s": 600,         # with a median this short or shorter
    "explore": 0.6,         # calls that read and search
    "test": 0.15,           # calls that test
    "build": 0.15,          # calls that build or install
    "slurm": 0.10,          # calls that are Slurm commands
    "wait": 0.25,           # calls that only wait
    "night": 0.25,          # active minutes between 00:00 and 06:00, local time,
    "night_min": 60,        # once there are this many active minutes
    "min_calls": 20,        # calls a share of calls needs
    # the summary of how a user leverages their agents (`leverage`)
    "mode": 0.5,            # a way of driving agents names the summary at this share of the sessions
    #                         whose launcher is known; under it, "several places"
    "mostly": 0.5,          # a use, or the top two together, is "mostly" at this share of the actions
    "project": 0.5,         # a project is "mostly" what they work on at this share of the sessions
}

# What a user's agents are used for, as the summary names a use: the call
# categories grouped, in the order a tie is listed. Waiting (sleep, ps, pgrep,
# a squeue loop) and calls whose command was not recorded are no use of their
# own: they are left out of the actions, and waiting is said apart.
USES = (
    ("explore", ("explore",)),      # read and search code and files: ls, cat, rg, grep, sed -n
    ("develop", ("edit", "test")),  # change files from the shell, and test them
    ("build", ("build",)),          # compile, build and install software
    ("run", ("run",)),              # run programs: a script, a training run
    ("hpc", ("slurm",)),            # the scheduler: sbatch, srun, squeue, ...
    ("git", ("git",)),              # git and gh
)
# The ways of driving agents the summary names, from the surfaces (`surface_of`):
# an app is the desktop app or the web, one way to a reader.
_MODES = (("terminal", ("terminal",)), ("script", ("script",)),
          ("extension", ("extension",)), ("app", ("desktop", "web")))

# The traits, in the order a portrait lists them: id, what it says, its tone
# (how the page colours it: crit, warn, ok, or plain), and its rule in words.
TRAITS = (
    ("interactive", "works in the agent's own terminal", "",
     "%d%% or more of the sessions are driven from a terminal: the agent's own interactive "
     "screen" % (100 * RULES["interactive"])),
    ("scripted", "runs agents from scripts", "",
     "%d%% or more of the sessions are driven from a script: claude -p, codex exec, an SDK, "
     "or an MCP server, with no one at a terminal" % (100 * RULES["scripted"])),
    ("app", "drives agents from an app or an IDE", "",
     "%d%% or more of the sessions are driven from the desktop app, an IDE extension, or the "
     "web" % (100 * RULES["app"])),
    ("bypass", "launches with approval bypassed", "crit",
     "%d%% or more of the sessions whose launch is known were started with a bypass flag "
     "(--dangerously-skip-permissions, --yolo, ...)" % (100 * RULES["bypass"])),
    ("auto", "launches in auto mode", "warn",
     "%d%% or more of the sessions whose launch is known were started in auto mode"
     % (100 * RULES["auto"])),
    ("sandboxed", "tool calls run sandboxed", "ok",
     "%d%% or more of the sessions whose tool calls were judged had some or all of them "
     "namespace-isolated" % (100 * RULES["sandboxed"])),
    ("multi_agent", "uses more than one agent", "",
     "sessions of two agent types or more"),
    ("parallel", "keeps several sessions open at once", "",
     "%d sessions or more open at the same moment" % RULES["parallel"]),
    ("long", "long sessions", "",
     "the median session is open %d h or longer" % (RULES["long_s"] // 3600)),
    ("short", "many short sessions", "",
     "%d sessions or more, and the median one is open %d min or less"
     % (RULES["short_n"], RULES["short_s"] // 60)),
    ("explorer", "agents mostly read and search", "",
     "%d%% or more of the calls explore (ls, cat, rg, grep, find, ...), of %d calls or more; "
     "the agent's own Read tool runs no command and is not counted"
     % (100 * RULES["explore"], RULES["min_calls"])),
    ("tester", "agents test often", "",
     "%d%% or more of the calls run tests, of %d calls or more"
     % (100 * RULES["test"], RULES["min_calls"])),
    ("builder", "agents build often", "",
     "%d%% or more of the calls build, compile or install, of %d calls or more"
     % (100 * RULES["build"], RULES["min_calls"])),
    ("slurm", "agents drive Slurm", "",
     "an agent session submitted a job, or %d%% or more of the calls are Slurm commands"
     % (100 * RULES["slurm"])),
    ("poller", "agents poll", "warn",
     "an agent session polled the scheduler for 10 min or more, or %d%% or more of the calls "
     "only wait (sleep, ps, pgrep, squeue loops), of %d calls or more"
     % (100 * RULES["wait"], RULES["min_calls"])),
    ("committer", "agents commit their work", "",
     "an agent wrote a commit message or a pull request's title in a command"),
    ("overnight", "agents active overnight", "",
     "%d%% or more of the minutes in which a call began fall between 00:00 and 06:00, local "
     "time, of %d such minutes or more" % (100 * RULES["night"], RULES["night_min"])),
)
_TRAIT = {t[0]: t for t in TRAITS}

# What one user's portrait lists, at most: the rest are counted.
SESSIONS_SHOWN = 60         # sessions, the newest first
TASKS_SHOWN = 40            # tasks, the newest first
COMMANDS_SHOWN = 8          # the commands their agents ran most
PROJECTS_SHOWN = 5          # the projects their sessions worked in
# Phase blocks a portrait builds: the newest sessions'. A block is cut and named
# when it is read (phase_segment, phase_tasks), as a Trajectories row's is, and
# this bounds what one read of a heavy user costs; the rest are counted.
PHASE_SESSIONS = 80

# The surfaces and the approval states a share is over: `unknown` (and an app
# server's `unobservable`) say nothing, so they are counted but not shared.
_APP = ("desktop", "extension", "web")
_APPROVAL_KNOWN = ("bypassed", "auto", "supervised")
_SANDBOX_KNOWN = ("sandboxed", "partial", "failed", "unsandboxed")
_NIGHT = range(0, 6)

BASIS = {
    "session": "one agent run: an agent process seen at the root of its tree, with every collector "
               "key folded into it (as Trajectories), and its rows on several days together. It "
               "belongs to the account its records name first",
    "open": "a session is open from its first record to its last; 'open at once' counts the "
            "sessions open at the same moment",
    "active_minutes": "the minutes in which one of the user's agents began a tool call, local time",
    "calls": "tool calls the agent issued directly, read from its commands (phase_rules) and counted "
             "from each session's call log; calls still running are not in it. Claude Code and Codex "
             "only: another agent's calls are not recognized, which is not none",
    "words": "the agent's own words in a command: a commit message (git commit -m), a pull request's "
             "title (gh pr create/edit --title), or an entry written to a log or plan file",
    "phases": "time in each phase, from the same phase blocks the Trajectories rows show, for the "
              "newest %d sessions" % PHASE_SESSIONS,
    "project": "the folder the agent was started in, or when that is a home folder or named for a "
               "person, the folder its commands change into most (as a task's headline)",
    "sandbox": "the Sandbox tab's verdict on each session's tool calls",
    "slurm": "the user's agent sessions as the Slurm tab folds them",
    "traits": "plain statements, each from one rule over these numbers; the rules are served "
              "beside them, and none is a judgement",
    "leverage": "how a user leverages their agents, from the same numbers: the way most of their "
                "sessions are driven, the freedom they start with, how many run at once, and what the "
                "agents' actions are for -- their calls by use, waiting and unrecorded calls left out",
}


def _q(vals, p):
    """The value at rank `p` of `vals`, as the Trajectories plates read one."""
    if not vals:
        return None
    v = sorted(vals)
    return v[min(len(v) - 1, int(p * (len(v) - 1)))]


def _r(v, d=1):
    return None if v is None else round(v, d)


def agent_sessions(traj, now):
    """`{(host, agent pid, user): [rows]}`: every rooted agent row of the window,
    the rows of one agent on several days together, in time order."""
    cutoff = now - traj.window_s
    out = {}
    for s, _via, _k in traj.folded()[0]:
        if s.cls != "agent" or (s.last or 0) < cutoff or s.kt not in traj.rankable \
                or not traj.rooted(s):
            continue
        key = _agent_key(s)
        if key is not None:
            out.setdefault(key, []).append(s)
    for rows in out.values():
        rows.sort(key=lambda r: (r.first if r.first is not None else math.inf, r.sid))
    return out


def _session(traj, key, rows, boxed, running):
    """One agent run, summed over its rows. Internal fields start with `_`."""
    host, pid, user = key
    head = rows[0]
    agent_type = next((r.agent_type for r in rows if r.agent_type), None)
    surface, detail = traj.surface_of(head)
    firsts = [r.first for r in rows if r.first is not None]
    lasts = [r.last for r in rows if r.last is not None]
    first = min(firsts) if firsts else None
    last = max(lasts) if lasts else None
    read = agent_type in PHASE_AGENTS
    calls = [c for r in rows for c in r.phase.calls] if read else []
    calls.sort(key=itemgetter(S))
    tagsets = traj._tagsets
    cats = collections.Counter()
    minutes = set()
    words = set()
    for c in calls:
        cats[c[CAT]] += 1
        minutes.add(int(c[S] // 60))
        t = c[TAGS]
        w = tagsets[t][1] if 0 < t < len(tagsets) else None
        if w:
            words.add((w[0], w[1]))
    boxes = boxed.get((host, pid))
    return {
        "_rows": rows, "_calls": calls, "_cats": cats, "_minutes": minutes, "_words": words,
        "user": user, "host": host, "pid": pid, "agent_type": agent_type,
        "surface": surface, "surface_detail": detail,
        "approval": traj.approval_of(head),
        # no tool call the sandbox reads (a session that made none): its own state
        "sandbox": session_state(collections.Counter(boxes)) if boxes else "no_calls",
        "first": first, "last": last,
        "wall_s": (last - first) if first is not None and last is not None else None,
        "calls": sum(r.phase.n for r in rows) if read else None,
        "processes": sum(r.n for r in rows),
        "cpu_s": sum(r.cpu for r in rows),
        "rd_mb": sum(r.rd for r in rows), "wr_mb": sum(r.wr for r in rows),
        "net_tx_mb": sum(r.tx for r in rows), "net_rx_mb": sum(r.rx for r in rows),
        "peak_rss_mb": max((r.rss for r in rows), default=0.0),
        "running": bool(running and running(host, pid)),
        # the newest day's row: what the Trajectories tab opens it by
        "sid": rows[-1].sid,
        "project": traj.project_of(head, calls),
    }


def _peak_open(sessions):
    """The most sessions open at one moment. A start and an end at the same
    instant are the same session's, so starts are counted first."""
    ev = []
    for s in sessions:
        if s["first"] is not None and s["last"] is not None:
            ev.append((s["first"], -1))
            ev.append((s["last"], 1))
    ev.sort()
    cur = peak = 0
    for _t, d in ev:
        cur -= d
        peak = max(peak, cur)
    return peak


class _Clock:
    """Local (weekday, hour) of an epoch minute, one `localtime` per hour."""

    def __init__(self):
        self.held = {}

    def at(self, minute):
        h = minute // 60
        got = self.held.get(h)
        if got is None:
            lt = time.localtime(h * 3600)
            got = self.held[h] = (lt.tm_wday, lt.tm_hour)
        return got


def slurm_by_user(slurm):
    """`{user: ...}`: what each user's agent sessions asked of the scheduler,
    folded through the process index as the Slurm tab folds them. Only agent
    sessions; a person's own submissions are the Slurm tab's."""
    out = {}
    if slurm is None:
        return out
    # The reducer's own fold and poll reading, so the two cannot differ. Its
    # result() serves the busiest sessions only, and a portrait needs them all.
    for sk, g in slurm._sessions().items():
        ks = g["keys"]
        if sk[0] != "a" or ks[0].cls != "agent":
            continue
        user = next((k.user for k in ks if k.user), None)
        if not user:
            continue
        o = collections.Counter()
        ts, n = [], collections.Counter()
        gpu_h = 0.0
        for k in ks:
            o.update(k.out)
            ts.extend(k.q_ts)
            n["q"] += k.q
            n["cancels"] += k.cancels
            n["parsable"] += k.flags.get("parsable", 0)
            n["gpu_h_known"] += k.gpu_h_known
            gpu_h += k.gpu_h
        if not (n["q"] or n["cancels"] or sum(o.values())):
            continue
        loop = SlurmReducer._polls(ts)["loop"]
        d = out.setdefault(user, {"sessions": 0, "jobs": 0, "rejected": 0, "asked": 0,
                                  "queries": 0, "cancels": 0, "parsable": 0,
                                  "polling_sessions": 0, "polling_s": 0.0, "longest_loop": None,
                                  "gpu_h": 0.0, "gpu_h_known": 0})
        d["sessions"] += 1
        d["jobs"] += o["job"]
        d["rejected"] += o["rejected"]
        d["asked"] += o["job"] + o["rejected"] + o["no_id"]
        d["queries"] += n["q"]
        d["cancels"] += n["cancels"]
        d["parsable"] += n["parsable"]
        d["gpu_h"] += gpu_h
        d["gpu_h_known"] += n["gpu_h_known"]
        if loop and loop["polling"]:
            d["polling_sessions"] += 1
            d["polling_s"] += loop["span_s"]
        if loop and (d["longest_loop"] is None or loop["span_s"] > d["longest_loop"]["span_s"]):
            d["longest_loop"] = loop
    for d in out.values():
        d["queries_per_job"] = round(d["queries"] / d["jobs"], 1) if d["jobs"] else None
        d["parsable_pct"] = pct(d["parsable"], d["asked"]) if d["asked"] else None
        d["gpu_h"] = round(d["gpu_h"], 1) if d["gpu_h_known"] else None
        d["polling_s"] = round(d["polling_s"], 1)
    return out


def _of(k, n, noun="sessions"):
    return "%d of %d %s" % (k, n, noun)


def traits(row):
    """The traits a user's row holds, in TRAITS' order, each with the numbers
    that met its rule."""
    got = {}
    n = row["sessions"]
    surf = row["surfaces"]
    known = n - surf.get("unknown", 0)
    if known:
        if surf.get("terminal", 0) >= RULES["interactive"] * known:
            got["interactive"] = _of(surf.get("terminal", 0), known)
        if surf.get("script", 0) >= RULES["scripted"] * known:
            got["scripted"] = _of(surf.get("script", 0), known)
        app = sum(surf.get(s, 0) for s in _APP)
        if app >= RULES["app"] * known:
            got["app"] = _of(app, known)
    ap = row["approval"]
    known = sum(ap.get(a, 0) for a in _APPROVAL_KNOWN)
    if known:
        if ap.get("bypassed", 0) >= RULES["bypass"] * known:
            got["bypass"] = _of(ap.get("bypassed", 0), known)
        if ap.get("auto", 0) >= RULES["auto"] * known:
            got["auto"] = _of(ap.get("auto", 0), known)
    sb = row["sandbox"]
    known = sum(sb.get(s, 0) for s in _SANDBOX_KNOWN)
    iso = sb.get("sandboxed", 0) + sb.get("partial", 0)
    if known and iso >= RULES["sandboxed"] * known:
        got["sandboxed"] = _of(iso, known)
    types = [t for t in row["agent_types"] if t]
    if len(types) >= 2:
        got["multi_agent"] = "%d agent types" % len(types)
    if row["peak_open"] >= RULES["parallel"]:
        got["parallel"] = "up to %d" % row["peak_open"]
    w = row["wall_p50_s"]
    if w is not None and w >= RULES["long_s"]:
        got["long"] = "median %.1f h" % (w / 3600.0)
    if n >= RULES["short_n"] and w is not None and w <= RULES["short_s"]:
        got["short"] = "%s sessions, median %.0f min" % (format(n, ","), w / 60.0)
    cats = row["cats"]
    logged = sum(cats.values())
    share = (lambda c: cats.get(c, 0) / logged) if logged >= RULES["min_calls"] else (lambda c: 0.0)
    calls_of = lambda c: "%s%% of %s calls" % (_pct_txt(100.0 * share(c)), format(logged, ","))  # noqa: E731
    if share("explore") >= RULES["explore"]:
        got["explorer"] = calls_of("explore")
    if share("test") >= RULES["test"]:
        got["tester"] = calls_of("test")
    if share("build") >= RULES["build"]:
        got["builder"] = calls_of("build")
    sl = row.get("slurm") or {}
    if sl.get("jobs"):
        got["slurm"] = "%s job%s submitted" % (format(sl["jobs"], ","), "" if sl["jobs"] == 1 else "s")
    elif share("slurm") >= RULES["slurm"]:
        got["slurm"] = calls_of("slurm")
    if sl.get("polling_sessions"):
        got["poller"] = "%d session%s polled the queue for %.1f h" % (
            sl["polling_sessions"], "" if sl["polling_sessions"] == 1 else "s",
            sl["polling_s"] / 3600.0)
    elif share("wait") >= RULES["wait"]:
        got["poller"] = calls_of("wait")
    words = row["words"]
    if words.get("commit") or words.get("pr"):
        bits = []
        if words.get("commit"):
            bits.append("%d commit message%s" % (words["commit"], "" if words["commit"] == 1 else "s"))
        if words.get("pr"):
            bits.append("%d pull request%s" % (words["pr"], "" if words["pr"] == 1 else "s"))
        got["committer"] = ", ".join(bits)
    act = row["active_min"]
    if act >= RULES["night_min"]:
        night = sum(row["hours"][h] for h in _NIGHT)
        if night >= RULES["night"] * act:
            got["overnight"] = "%s%% of %s active minutes" % (_pct_txt(100.0 * night / act),
                                                              format(act, ","))
    return [{"id": t[0], "label": t[1], "tone": t[2], "detail": got[t[0]]}
            for t in TRAITS if t[0] in got]


def leverage(row):
    """How a user leverages their agents, as data the page words into its summary
    (lib/portraits.ts): every part from a row's own numbers, by RULES, and None
    where the numbers do not say it.

      mode       the way most of their sessions are driven (`_MODES`), at
                 RULES["mode"] of those whose launcher is known; `mixed` under
                 it, `unknown` with none known
      autonomy   the freedom the agents start with: `bypass` or `auto` at
                 RULES["bypass"] / RULES["auto"] of the known launches, else
                 `no_flag` (no bypass flag at launch: settings files, and a
                 switch made mid-session, are not seen), with the counts
      parallel   the most open at once, at RULES["parallel"] or more
      uses       what the agents' actions are for (`USES`), the most first,
                 over `actions`, the calls that do something; none under
                 RULES["min_calls"] actions, or where no call is read.
                 `mostly` is how many uses from the top hold RULES["mostly"]
                 of the actions: 1, 2, or 0 for a mix
      waiting    the calls that only wait, at RULES["wait"] of the calls
      project    what most of their sessions work on, `mostly` at
                 RULES["project"] of the sessions
      when       the busiest local hour, and the share of active minutes
                 between 00:00 and 06:00 once there are RULES["night_min"]
    """
    n = row["sessions"]
    surf = row["surfaces"]
    known = n - surf.get("unknown", 0)
    if known:
        # the first of a tie, in _MODES' order
        top, k = max(((m, sum(surf.get(x, 0) for x in xs)) for m, xs in _MODES),
                     key=lambda mk: mk[1])
        mode = {"id": top if k >= RULES["mode"] * known else "mixed", "n": k, "of": known}
    else:
        mode = {"id": "unknown", "n": 0, "of": n}
    ap = row["approval"]
    known = sum(ap.get(a, 0) for a in _APPROVAL_KNOWN)
    autonomy = None
    if known:
        b, a = ap.get("bypassed", 0), ap.get("auto", 0)
        kind = "bypass" if b >= RULES["bypass"] * known else \
            "auto" if a >= RULES["auto"] * known else "no_flag"
        autonomy = {"id": kind, "bypassed": b, "auto": a, "of": known}
    cats = row["cats"]
    actions = sum(cats.get(c, 0) for _u, cs in USES for c in cs)
    uses, mostly = [], 0
    if row["calls"] is not None and actions >= RULES["min_calls"]:
        got = [(u, sum(cats.get(c, 0) for c in cs)) for u, cs in USES]
        uses = [{"id": u, "n": k, "pct": round(100.0 * k / actions, 1)}
                for u, k in sorted((x for x in got if x[1]), key=lambda x: -x[1])]
        if uses[0]["n"] >= RULES["mostly"] * actions:
            mostly = 1
        elif len(uses) > 1 and uses[0]["n"] + uses[1]["n"] >= RULES["mostly"] * actions:
            mostly = 2
    logged = sum(cats.values())
    waiting = None
    if logged >= RULES["min_calls"] and cats.get("wait", 0) >= RULES["wait"] * logged:
        waiting = {"n": cats["wait"], "of": logged, "pct": round(100.0 * cats["wait"] / logged, 1)}
    project = None
    if row["projects"]:
        name, k = row["projects"][0]
        project = {"name": name, "n": k, "of": n, "mostly": k >= RULES["project"] * n}
    hours, act = row["hours"], row["active_min"]
    when = None
    if act:
        h = max(range(24), key=lambda i: (hours[i], -i))    # the earliest of a tie
        when = {"busiest": h, "minutes": hours[h],
                "night_pct": round(100.0 * sum(hours[i] for i in _NIGHT) / act, 1)
                if act >= RULES["night_min"] else None}
    return {"mode": mode, "autonomy": autonomy,
            "parallel": row["peak_open"] if row["peak_open"] >= RULES["parallel"] else None,
            "uses": uses, "actions": actions, "mostly": mostly, "waiting": waiting,
            "project": project, "when": when}


def _pct_txt(p):
    return "%d" % round(p) if p >= 10 else "%.1f" % p


def _user_row(user, sessions, slurm, clock, grid=False):
    """One user's summary over their sessions; the roster's row, and the head of
    their portrait (`grid` adds the weekday-by-hour minutes)."""
    n = len(sessions)
    read = [s for s in sessions if s["calls"] is not None]
    cats = collections.Counter()
    words = set()
    minutes = set()
    for s in sessions:
        cats.update(s["_cats"])
        words |= s["_words"]
        minutes |= s["_minutes"]
    hours = [0] * 24
    cells = [[0] * 24 for _ in range(7)] if grid else None
    for m in minutes:
        wd, h = clock.at(m)
        hours[h] += 1
        if cells is not None:
            cells[wd][h] += 1
    walls = [s["wall_s"] for s in sessions if s["wall_s"] is not None]
    firsts = [s["first"] for s in sessions if s["first"] is not None]
    lasts = [s["last"] for s in sessions if s["last"] is not None]
    projects = collections.Counter(s["project"] for s in sessions if s["project"])
    row = {
        "user": user,
        "sessions": n,
        "running": sum(1 for s in sessions if s["running"]),
        "hosts": sorted({s["host"] for s in sessions if s["host"]}),
        "agent_types": dict(collections.Counter(s["agent_type"] or "" for s in sessions)
                            .most_common()),
        "surfaces": dict(collections.Counter(s["surface"] or "unknown" for s in sessions)
                         .most_common()),
        "approval": dict(collections.Counter(s["approval"] or "unknown" for s in sessions)
                         .most_common()),
        "sandbox": dict(collections.Counter(s["sandbox"] for s in sessions).most_common()),
        # null: no session's calls are recognized, which is not none
        "calls": sum(s["calls"] for s in read) if read else None,
        "calls_sessions": len(read),
        "calls_p50": _q([s["calls"] for s in read], .5),
        "cats": {c: cats[c] for c in CATS if cats.get(c)},
        "words": dict(collections.Counter(src for src, _line in words)),
        "processes": sum(s["processes"] for s in sessions),
        "cpu_s": round(sum(s["cpu_s"] for s in sessions), 3),
        "rd_mb": round(sum(s["rd_mb"] for s in sessions), 3),
        "wr_mb": round(sum(s["wr_mb"] for s in sessions), 3),
        "net_tx_mb": round(sum(s["net_tx_mb"] for s in sessions), 3),
        "net_rx_mb": round(sum(s["net_rx_mb"] for s in sessions), 3),
        "peak_rss_mb": round(max((s["peak_rss_mb"] for s in sessions), default=0.0), 1),
        "wall_s": _r(sum(walls)) if walls else None,
        "wall_p50_s": _r(_q(walls, .5)),
        "wall_max_s": _r(max(walls)) if walls else None,
        "first": min(firsts) if firsts else None,
        "last": max(lasts) if lasts else None,
        "active_min": len(minutes),
        "hours": hours,
        "peak_open": _peak_open(sessions),
        "projects": [[p, k] for p, k in projects.most_common(PROJECTS_SHOWN)],
        "projects_total": len(projects),
        "slurm": slurm,
    }
    if cells is not None:
        row["grid"] = cells
    row["traits"] = traits(row)
    row["leverage"] = leverage(row)
    return row


def _fleet(rows, sessions):
    """The window's agent users and sessions, for a portrait to be read against."""
    tot = {k: collections.Counter() for k in ("agent_types", "surfaces", "approval", "sandbox",
                                               "cats", "words")}
    for r in rows:
        for k, c in tot.items():
            c.update(r[k])
    walls = [s["wall_s"] for s in sessions if s["wall_s"] is not None]
    calls = [s["calls"] for s in sessions if s["calls"] is not None]
    return {
        "users": len(rows), "sessions": len(sessions),
        "sessions_per_user_p50": _q([r["sessions"] for r in rows], .5),
        "wall_p50_s": _r(_q(walls, .5)),
        "calls_p50": _q(calls, .5),
        "active_min_p50": _q([r["active_min"] for r in rows if r["active_min"]], .5),
        "peak_open_p50": _q([r["peak_open"] for r in rows], .5),
        "running": sum(r["running"] for r in rows),
        **{k: dict(c.most_common()) for k, c in tot.items()},
        "traits": dict(collections.Counter(t["id"] for r in rows for t in r["traits"])),
    }


def _window(traj, now):
    cutoff = now - traj.window_s
    return {"minutes": traj.window_s // 60,
            "from": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(cutoff)),
            "to": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))}


def catalogue():
    return [{"id": t[0], "label": t[1], "tone": t[2], "rule": t[3]} for t in TRAITS]


def inputs(reducers):
    """What a read takes from the sandbox and Slurm reducers, as plain data:
    `{"boxed": {(host, agent pid): tool-call outcomes}, "slurm": {user: ...}}`.
    The sandbox's verdicts walk every tool call of the window
    (`SandboxReducer.agent_calls`), most of what a read costs, so a caller may
    keep these for as long as the view has not changed and hand them to each
    `Context` (`Aggregator._held`). They hold no reducer, so keeping them keeps
    no window's state alive after the window is gone."""
    sb = reducers.get("sandbox")
    return {"boxed": sb.agent_calls() if sb is not None else {},
            "slurm": slurm_by_user(reducers.get("slurm"))}


class Context:
    """One read of one view, under the lock its reducers are fed under: the agent
    sessions by user, the sandbox's verdict on each session's tool calls, and
    each user's Slurm sessions, each gathered when first asked for. A context
    references the reducers, so it lives for one read and is never kept; what
    is worth keeping between reads is `inputs`, which it takes as `boxed` and
    `slurm`.

    `running(host, pid)` says whether an agent is running now (the live tier);
    None says nothing is."""

    def __init__(self, reducers, now=None, running=None, boxed=None, slurm=None):
        self.now = now or time.time()
        self.traj = reducers.get("trajectories")
        self.running = running
        self._sb = reducers.get("sandbox")
        self._slurm_red = reducers.get("slurm")
        self._boxed, self._slurm = boxed, slurm
        self._users = {}

    def ready(self):
        """False before the view is built: there is nothing to read yet."""
        return self.traj is not None

    @property
    def boxed(self):
        if self._boxed is None:
            self._boxed = self._sb.agent_calls() if self._sb is not None else {}
        return self._boxed

    @property
    def slurm(self):
        if self._slurm is None:
            self._slurm = slurm_by_user(self._slurm_red)
        return self._slurm

    def by_user(self, only=None):
        """`{user: [session]}`: every user with an agent session in the window,
        or `only` that one, whose sessions are all a portrait builds."""
        if only not in self._users:
            users = collections.defaultdict(list)
            for key, rows in agent_sessions(self.traj, self.now).items():
                if only is None or key[2] == only:
                    users[key[2]].append(_session(self.traj, key, rows, self.boxed, self.running))
            self._users[only] = dict(users)
        return self._users[only]


def roster(ctx):
    """Every user with an agent session in the window, one row each, the busiest
    first, and the window's totals (`fleet`). None before the view is built."""
    if not ctx.ready():
        return None
    by_user = ctx.by_user()
    clock = _Clock()
    rows = [_user_row(u, ss, ctx.slurm.get(u), clock) for u, ss in by_user.items()]
    rows.sort(key=lambda r: (-r["sessions"], -(r["calls"] or 0), str(r["user"])))
    every = [s for ss in by_user.values() for s in ss]
    return {"window": _window(ctx.traj, ctx.now), "users": rows, "fleet": _fleet(rows, every),
            "traits": catalogue(), "rules": dict(RULES), "basis": BASIS}


def _members(traj, s):
    """The collector keys row `s` folds, as a Trajectories row reads them."""
    held = traj._fold_cache[2]
    return (held.get(s.sid) if held else None) or [s.sid]


def portrait(ctx, user, fleet=None):
    """One user's portrait: their row, read against the window's `fleet`, and
    what only one user's read affords -- the time in each phase and the tasks
    from their sessions' phase blocks, their sessions, and the commands their
    agents ran most. `found` is False for a user with no agent session in the
    window; None before the view is built."""
    if not ctx.ready():
        return None
    traj = ctx.traj
    out = {"user": user, "window": _window(traj, ctx.now), "fleet": fleet,
           "traits": catalogue(), "rules": dict(RULES), "basis": BASIS}
    sessions = sorted(ctx.by_user(only=user).get(user) or [], key=lambda s: (s["last"] or 0),
                      reverse=True)
    if not sessions:
        out["found"] = False
        return out
    row = _user_row(user, sessions, ctx.slurm.get(user), _Clock(), grid=True)
    phase_s, phase_n = collections.Counter(), collections.Counter()
    quoted, tasks, n_tasks, n_lone = collections.Counter(), [], 0, 0
    blocks = 0
    served = []
    for i, s in enumerate(sessions):
        block = None
        if i < PHASE_SESSIONS and s["agent_type"] in PHASE_AGENTS:
            head = s["_rows"][0]
            block = traj.phase_block(head, _members(traj, head))
            if not isinstance(block, dict) or "list" not in block:
                block = None
        mine = collections.Counter()
        n_own, q_own = 0, 0
        if block is not None:
            blocks += 1
            for ph in block["list"]:
                t0, t1 = ph.get("t0"), ph.get("t1")
                dt = max(0.0, t1 - t0) if t0 is not None and t1 is not None else 0.0
                mine[ph["kind"]] += dt
                phase_n[ph["kind"]] += ph.get("n") or 0
            for t in block.get("tasks") or []:
                if t.get("kind") == "lone":
                    n_lone += 1
                    continue
                n_own += 1
                if t.get("source"):
                    q_own += 1
                    quoted[t["source"]] += 1
                tasks.append({"label": t.get("label"), "source": t.get("source"),
                              "ref": t.get("ref") or "", "names": t.get("names") or [],
                              "t0": t.get("t0"), "t1": t.get("t1"), "n": t.get("n"),
                              "open": bool(t.get("open")), "sid": t.get("sid") or s["sid"],
                              "agent_type": s["agent_type"], "project": s["project"]})
            phase_s.update(mine)
            n_tasks += n_own
        if len(served) < SESSIONS_SHOWN:
            served.append({
                **{k: v for k, v in s.items() if not k.startswith("_")},
                "wall_s": _r(s["wall_s"]), "cpu_s": round(s["cpu_s"], 3),
                "rd_mb": round(s["rd_mb"], 3), "wr_mb": round(s["wr_mb"], 3),
                "net_tx_mb": round(s["net_tx_mb"], 3), "net_rx_mb": round(s["net_rx_mb"], 3),
                "peak_rss_mb": round(s["peak_rss_mb"], 1),
                "sids": [r.sid for r in s["_rows"]],
                "headline": block.get("headline") if block is not None else None,
                "tasks": n_own if block is not None else None,
                "quoted": q_own if block is not None else None,
                "phase_s": {k: round(v, 1) for k, v in mine.most_common()} if block is not None
                else None,
                "cats": {c: s["_cats"][c] for c in CATS if s["_cats"].get(c)},
            })
    tasks.sort(key=lambda t: t["t0"] or 0, reverse=True)
    previews = traj._previews
    cmds = collections.Counter(c[PREV] for s in sessions for c in s["_calls"] if c[PREV])
    out.update({
        "found": True,
        "row": row,
        "phases": {"time_s": {k: round(v, 1) for k, v in phase_s.most_common()},
                   "calls": dict(phase_n.most_common()),
                   "sessions": blocks,
                   "of": sum(1 for s in sessions if s["agent_type"] in PHASE_AGENTS)},
        "tasks": {"n": n_tasks, "lone": n_lone, "quoted": dict(quoted),
                  "recent": tasks[:TASKS_SHOWN], "listed_of": len(tasks)},
        "sessions": served,
        "sessions_total": len(sessions),
        "commands": [[previews[i], k] for i, k in cmds.most_common(COMMANDS_SHOWN)
                     if 0 < i < len(previews)],
        "tz": time.strftime("%Z", time.localtime(ctx.now)),
    })
    return out
