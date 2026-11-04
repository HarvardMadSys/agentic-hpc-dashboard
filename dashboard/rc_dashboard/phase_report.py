"""A report of agents' phases and tasks over real days: the check the phase and task heuristics are
tuned against.

    cd dashboard && uv run python -m rc_dashboard.phase_report \\
        --roots /scratch/marthen/fas-rc-ebpfm/data/boslogin06 --day 2026-10-28 --tree 3580508

It is not part of a window build, so it is not in `hours.CODE_FILES`: a change here never discards
a saved hour. It reads one local day of a feed, or `--days` of them from `--day`, as a build reads
a window (`export.window_plan` and `export.window_scan`: every file's records merged by time, the
snapshot ticks beside the exits), and feeds the process index and the trajectories reducer that
shares it, as `PanelView.fresh_reducers` builds them. A reducer error is counted, not swallowed.
Every folded row is then built as the page gets it (`TrajectoryReducer._row`), and it prints:

  * every row of an agent pid given with --apid, or in a tree given with --tree (its root's pid,
    `tree_root_pid`; a tree can hold several agents): its user, agent type, collector keys, tool
    calls, housekeeping, and phase list, and then its tasks: how many, the headline, and a line for
    each (`T<i>`, its span and length, and its label, in quotes with where the agent's own words
    came from when it has them), with the gap before it and what was still running through the
    gap, and a lone stretch (a few calls hours from other work) as a line of its own
  * the calls by kind (`bash`, `search`, `wrapper`, `shell`, `bare`) and agent type, with the
    commonest shapes of each, so a shape filed in the wrong place shows
  * a summary over the rows whose calls are recognized (Claude Code and Codex): phases per active
    hour (median, p90, max), the share of rows at a handful or fewer, phases per row, tasks per
    session-day (median, p90, max, over the rows with two active hours or more), the share of tasks
    named by the agent's words, the lone stretches, the open phases, housekeeping by shape, the
    joins' bounds, and the solid editing phases that hold no edit call, which must be none

An active hour is a clock hour in which at least one of the row's calls started, a call still open
included. Idle phases are not counted as phases. A quantile is the nearest rank. Each call is
placed in the last phase that began at or before it, which is how the segmenter cuts them, and the
calls placed must match each phase's own count: a mismatch is reported.

A row is one local day, and an agent that works past midnight has a row for each. The rows of one
session serve one phase block, cut from the calls of all of them (`TrajectoryReducer.phase_block`),
so the report reads each row as the block gives it. A phase and a task say which day's row holds
them (`sid`): a row prints and counts the phases of its own day, its own calls, and the tasks that
begin in its day, which are the session-day it counts. A task is not cut at midnight, so the tasks
of a session are printed once, under the first of its rows that is printed, and the others say they
share them. The calls a block is cut from are read again here as the reducer reads them
(`session_calls`), and the calls placed must match each phase's count there too.

The days are local time, midnight to midnight; the directory of each under each root
(`<root>/<day>/`, the layout of a captured feed) is read, and the next day's for the records
written after midnight.
"""
import argparse
import bisect
import collections
import datetime
import functools
import json
import math
import os
import sys
import time

from . import config as configmod
from .export import window_plan, window_scan
from .normalize import Normalizer, canonical_class
from .procindex import ProcIndex
from .redact import redact
from .reducers.phase_log import CAT, FLAGS, OPEN, P_TEXT, S, PhaseLog
from .reducers.phase_rules import (_SETUP, PHASE_AGENTS, _argv, _first_word, call_text,
                                   is_fs_helper, shell_text, split_simple)
from .reducers.phase_segment import NEVER_EXITED
from .reducers import trajectories as trajmod
from .reducers.trajectories import TrajectoryReducer

HANDFUL = 6                 # phases per active hour the spec calls a handful
TASK_HOURS = 2              # rows with this many active hours (`row_stats`) count for tasks a day
SHAPE_CHARS = 60            # characters of a masked command shown as a shape
LABEL_CHARS = 200           # characters of a task's label or a headline shown
DAY_SESSIONS = 10 ** 7      # raw sessions one pass over a day holds (`reduce_day`)
EXAMPLES = 4                # shapes per kind shown with their first argv
KINDS = ("bash", "search", "wrapper", "shell", "bare")
MINUTE = "%H:%M"            # the clock of a task line, as `clock` takes it


# ------------------------------------------------------------------ pure parts
def day_bounds(day, days=1):
    """`(since, until)`: the epoch seconds of the local midnight that starts `day` (YYYY-MM-DD)
    and of the one that ends the `days`th day from it, as `export.window_scan` takes them."""
    d = datetime.date.fromisoformat(day)
    end = d + datetime.timedelta(days=days)
    return (time.mktime(d.timetuple()), time.mktime(end.timetuple()))


def day_range(day, days=1):
    """The days a report reads, as its title says them: `2026-10-28`, or `2026-10-28 to
    2026-10-29`."""
    if days == 1:
        return day
    last = datetime.date.fromisoformat(day) + datetime.timedelta(days=days - 1)
    return "%s to %s" % (day, last.isoformat())


def day_dirs(roots, day, days=1):
    """The directories that hold the records of `days` days from `day`: `<root>/<day>` for each of
    them the root has, and the directory of the day after the last, where records written after
    midnight land, for each root laid out by date. A root with none of those days is read as it
    is when it holds its day-files itself, and is no part of the read when it is laid out by
    date."""
    first = datetime.date.fromisoformat(day)
    names = [(first + datetime.timedelta(days=k)).isoformat() for k in range(days + 1)]
    out = []
    for root in roots:
        held = [os.path.join(root, n) for n in names[:-1] if os.path.isdir(os.path.join(root, n))]
        if held:
            after = os.path.join(root, names[-1])
            out += held + ([after] if os.path.isdir(after) else [])
        elif os.path.isdir(root) and not any(n.startswith("20") for n in os.listdir(root)):
            out.append(root)
    return out


def positive_int(text):
    """An argparse type: a whole number of at least 1."""
    try:
        n = int(text)
    except ValueError:
        n = 0
    if n < 1:
        raise argparse.ArgumentTypeError("%r is not a whole number of at least 1" % text)
    return n


def quantile(vals, q):
    """The nearest-rank `q` quantile of `vals` (0 < q <= 1), None for none."""
    if not vals:
        return None
    v = sorted(vals)
    return v[max(0, math.ceil(q * len(v)) - 1)]


def place(phases, calls):
    """Each call's phase: the index in `phases` of the last phase other than idle that began at
    or before it (half a millisecond allowed for the block's rounding), or None. Returns the
    indexes, in the order of `calls`."""
    keep = [i for i, p in enumerate(phases) if p["kind"] != "idle"]
    t0s = [phases[i]["t0"] for i in keep]
    out = []
    for c in calls:
        k = bisect.bisect_right(t0s, c[S] + 0.0005) - 1
        out.append(keep[k] if k >= 0 else None)
    return out


def belongs(item, sid):
    """Whether a phase or a task of a block is the row `sid`'s. A block of one day is its row's
    whole, and its items have no `sid`. The block of an agent that worked on more than one day
    is shared by the rows of those days (`days`), and an item says which of them holds it."""
    return item.get("sid") in (None, sid)


def of_day(items, sid):
    """The phases or the tasks `items` of a block that are the row `sid`'s (`belongs`)."""
    return [x for x in items if belongs(x, sid)]


def row_stats(block, calls, sid=None, own=None):
    """What the summary reads of one row: its phases (not idle), the clock hours its calls
    started in, phases per such hour, the solid editing phases that hold no edit call, and the
    phases whose own call count differs from the calls placed in them.

    `calls` are the ones the block's phases are cut from. Of a block shared by the day rows of
    one session, the row `sid` counts the phases that are its own (`belongs`), and its own calls,
    `own` (by default `calls`), for the hours and for the calls placed in no phase, so that
    across the rows each phase and each call is counted once."""
    phases = block.get("list") or []
    mine = [i for i, p in enumerate(phases) if belongs(p, sid)]
    work = [i for i in mine if phases[i]["kind"] != "idle"]
    own = calls if own is None else own
    hours = {int(c[S] // 3600) for c in own}
    at = place(phases, calls)
    held = collections.Counter(i for i in at if i is not None)
    edits = {i for i, c in zip(at, calls) if i is not None and c[CAT] == "edit"}
    no_edit = [i for i in mine if phases[i]["kind"] == "edit" and i not in edits]
    off = [i for i in mine
           if phases[i]["kind"] not in ("idle", "edit_guess") and held.get(i, 0) != phases[i]["n"]]
    off += [i for i in mine if phases[i]["kind"] in ("idle", "edit_guess") and held.get(i)]
    placed = at if own is calls else place(phases, own)
    return {"phases": len(work), "active_hours": len(hours),
            "per_hour": (len(work) / len(hours)) if hours else None,
            "open": sum(1 for i in mine if phases[i].get("open")),
            "no_edit": no_edit, "unplaced": sum(1 for i in placed if i is None),
            "off": sorted(off)}


def span(sec):
    """A duration as a reader reads it: 35s, 4m08s, 2h40m."""
    if sec is None:
        return "?"
    sec = int(round(sec))
    if sec < 60:
        return "%ds" % sec
    if sec < 3600:
        return "%dm%02ds" % divmod(sec, 60)
    return "%dh%02dm" % (sec // 3600, sec % 3600 // 60)


def clock(t, day=None, fmt="%H:%M:%S"):
    """A local time, with its date when it is not on `day`."""
    lt = time.localtime(t)
    s = time.strftime(fmt, lt)
    return s if day is None or time.strftime("%Y-%m-%d", lt) == day else \
        time.strftime("%m-%d ", lt) + s


def cut(text, n=SHAPE_CHARS):
    """`text` masked, its whitespace collapsed, and cut to `n` characters."""
    s = " ".join(redact(text or "").split())
    return s if len(s) <= n else s[:n - 1] + "…"


def plural(n, one, many=None):
    """`n` and its noun: `1 task`, `12 tasks`, `1 lone stretch`, `2 lone stretches`."""
    return "%d %s" % (n, one if n == 1 else many or one + "s")


def row_tag(row, days=1):
    """A row's short name in the summary: its collector key (`apid:1234`), and its day when the
    report reads more than one, since an agent that worked on two has a row for each."""
    parts = row["sid"].split("|")
    return "%s %s" % (parts[1], parts[-1]) if days > 1 and len(parts) > 2 else parts[-1]


@functools.lru_cache(maxsize=65536)
def _program(text):
    """The first program a shell text runs, past `cd`, `export`, and the like; None for none."""
    try:
        cmds = split_simple(text[:2000])
    except Exception:           # a text the splitter cannot read is shown by its first word
        cmds = [text.split()]
    for words in cmds:
        for w in words:
            if "=" in w.split("/", 1)[0] and not w.startswith(("-", "=")):
                continue        # an assignment before the program
            name = os.path.basename(w)
            if name in _SETUP:
                break
            return name or None
    return None


def call_shape(rec, kind, kids_text=None):
    """A call's shape for the census: a bare command's program (the fs helper as such), a search's
    name, a shell call's first program, and a wrapper's payload's first program."""
    command, args, argv0 = _argv(rec)
    if kind == "bare":
        return "codex fs helper" if is_fs_helper(rec) else (os.path.basename(command) or "?")
    if kind == "search":
        return argv0 or os.path.basename(command) or "?"
    if kind == "wrapper":
        if not kids_text:
            return "(no payload)"
        text = shell_text(None, kids_text)
        return _program(text) if text else os.path.basename(_first_word(kids_text)) or "?"
    text = call_text(rec, kind)
    return (_program(text) or "(setup only)") if text else "(no command)"


# ------------------------------------------------------------------ the read
class CountedReducer(TrajectoryReducer):
    """The trajectories reducer, counting each call it holds by kind and shape as it holds it."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.by_kind = collections.Counter()      # (agent type, kind) -> calls
        self.shapes = collections.Counter()       # (agent type, kind, shape) -> calls
        self.example = {}                         # (agent type, kind, shape) -> its first argv
        self.bare_argv = collections.Counter()    # (agent type, masked argv) of a bare call

    def _call(self, s, rec, kind, t0, end, kids):
        at = rec.get("agent_type")
        at = at if at in PHASE_AGENTS else s.agent_type
        key = (at, kind, call_shape(rec, kind, kids[P_TEXT] if kids else None))
        self.by_kind[(at, kind)] += 1
        self.shapes[key] += 1
        if key not in self.example:
            self.example[key] = cut(rec.get("args") or (kids[P_TEXT] if kids else "") or "", 160)
        if kind == "bare":
            self.bare_argv[(at, cut(rec.get("args") or ""))] += 1
        super()._call(s, rec, kind, t0, end, kids)


def classes_of(cfg):
    """The classes a window's reducers are built with, as the aggregator lists them."""
    return [canonical_class(c) for c in cfg.get("filters.classes") or []] + \
        [cfg.get("filters.unlabeled_class", "unlabeled")]


def reduce_day(cfg, dirs, since, until, progress=None):
    """Read `[since, until)` of the day-files under `dirs` into a process index and the
    trajectories reducer sharing it. Returns `(reducer, read stats)`.

    The reducer holds every session of the day: past `max_sessions` it evicts by the wall clock
    (`TrajectoryReducer._evict`), which for a past day is every session it holds, and schema 7
    keys each tool call apart (76,011 raw sessions on boslogin06 on 2026-10-28). A window is
    reduced hour by hour and merged, so this pass is not that, and the report says so."""
    procs = ProcIndex(cfg)
    traj = CountedReducer(cfg, classes_of(cfg), window_s=int(until - since) + 1, procs=procs)
    configured = traj.max_sessions
    traj.max_sessions = max(configured, DAY_SESSIONS)
    red = {ProcIndex.KEY: procs, traj.KEY: traj}
    dispatch = {}
    for r in red.values():
        for ev in r.EVENTS:
            dispatch.setdefault(ev, []).append(r)
    plan, total = window_plan(dirs, since, 0)
    t0 = time.time()
    n, errors, stats = 0, collections.Counter(), {}
    for rec, stats in window_scan(plan, Normalizer(cfg), since, until - 1e-6,
                                  events=set(dispatch)):
        for r in dispatch.get(rec.get("event"), ()):
            try:
                r.feed(rec)
            except Exception as e:  # the service swallows it; a report counts it
                errors["%s: %s" % (type(r).__name__, type(e).__name__)] += 1
        n += 1
        if progress is not None and n % 500000 == 0:
            progress("%s records, %.0f%% of %.0f MB, %.0f s" % (
                "{:,}".format(n), 100.0 * stats["bytes"] / max(total, 1), total / 1e6,
                time.time() - t0))
    return traj, {"records": n, "files": [e["path"] for e in plan], "bytes": stats.get("bytes", 0),
                  "parse_errors": stats.get("parse_errors", 0), "no_ts": stats.get("no_ts", 0),
                  "errors": dict(errors), "seconds": round(time.time() - t0, 1),
                  "max_sessions": configured}


def _key_pid(sid):
    """The pid a collector key names (`host|day|class|apid:1234` -> 1234), or None."""
    try:
        return int(sid.rsplit(":", 1)[1])
    except (IndexError, ValueError):
        return None


class Calls(collections.namedtuple("Calls", "block own hidden")):
    """The calls of one row, as the report reads them. `block` are the ones its block's phases are
    cut from: the row's own for a row of one day, and the session's for a row of an agent that
    worked on more than one day. `own` are the ones the row's own records hold, which `n_calls`
    counts (the same list as `block` for a row of one day), and `hidden` is how many of its
    commands ran under a process that never exited and are no call."""
    __slots__ = ()


def session_calls(traj, days):
    """`{row sid: Calls}` for the day rows `days` (`[{sid, first, last}]`, as a block lists them)
    that share one block. The block is cut from the logs of all of them folded, and from the open
    calls among the entries still pending under the collector keys of each, read as of the
    session's last record: a call is where it began, whichever row its record is in. This reads
    them as `TrajectoryReducer._build_block` does, which a test of the report holds it to."""
    parts = []
    for d in days:
        s, _via, _n, mem = traj.lookup(d["sid"])
        parts.append((s, traj._pending_of(mem)))
    log = PhaseLog.fold([s.phase for s, _entries in parts], traj.phase_params["max_calls"])
    last = max(s.last for s, _entries in parts)
    block, own = list(log.calls), {}
    for s, entries in parts:
        opened, hidden = traj._open_calls(parts[0][0].agent_type, last, entries)
        block += opened
        own[s.sid] = (list(s.phase.calls) + opened, hidden)
    return {sid: Calls(block, calls, hidden) for sid, (calls, hidden) in own.items()}


def rows_of(traj):
    """Every folded row, as the page gets it, with what the summary reads:
    `[(session, row, member sids, calls)]`, `calls` being the row's `Calls`."""
    folded = traj.folded()[0]
    members = traj._fold_cache[2] or {}
    sessions, out = {}, []
    for s, via, k in folded:
        mem = members.get(s.sid) or [s.sid]
        row = traj._row(s, via, k)
        block = row["phases"]
        if "list" not in block:
            held = Calls([], [], 0)
        elif "days" in block:
            key = tuple(d["sid"] for d in block["days"])        # read once for its day rows
            if key not in sessions:
                sessions[key] = session_calls(traj, block["days"])
            held = sessions[key][s.sid]
        else:
            calls, hidden = traj.phase_calls(s, mem)
            held = Calls(calls, calls, hidden)
        out.append((s, row, mem, held))
    return out


def matches(s, mem, apids, trees):
    """Why a row is asked for: its agent pid or one of its keys' is in `apids`, or its tree root
    is in `trees`; None when it is not."""
    why = []
    if apids:
        if s.kt == "apid" and _key_pid(s.sid) in apids:
            why.append("apid %s" % _key_pid(s.sid))
        else:
            hit = sorted({_key_pid(m) for m in mem} & apids)
            if hit:
                why.append("holds key %s" % ", ".join(map(str, hit)))
    if trees and s.tree_root in trees:
        why.append("tree %s" % s.tree_root)
    return why or None


# ------------------------------------------------------------------ the print
def _phase_line(p, day, calls_by_cat):
    kind = p["kind"]
    name = {"edit_guess": "edit?", "loop": "edit–test loop ×%s" % p.get("rounds")}.get(kind, kind)
    bits = ["%s–%s" % (clock(p["t0"], day), clock(p["t1"], day)),
            span(p["t1"] - p["t0"]).rjust(6), (name + (" (open)" if p.get("open") else "")).ljust(22)]
    if kind == "idle":
        return "  ".join(bits)
    if kind == "edit_guess":
        return "  ".join(bits + ["guess: %s" % p.get("guess")])
    bits.append("%d calls" % p["n"])
    if calls_by_cat:
        bits.append("[%s]" % " ".join("%s %d" % kv for kv in calls_by_cat.most_common()))
    if p.get("mix"):
        bits.append("mix " + ", ".join("%s %d" % (c, n) for c, n in p["mix"]))
    if kind == "wait":
        bits.append("waits on %s%s" % (p.get("waits_on"), "" if p.get("every_s") is None else
                                      " · every %s, %s times" % (span(p["every_s"]), p["times"])))
    if p.get("top"):
        bits.append("top: " + " | ".join("%s ×%d" % (cut(t, 50), n) for t, n in p["top"]))
    if p.get("slices"):
        bits.append("%d guessed slice(s)" % len(p["slices"]))
    if p.get("ran_past"):
        bits.append("ran past: " + ", ".join("%s %s" % (cut(t, 30), span(d))
                                             for t, d in p["ran_past"][:2]))
    return "  ".join(bits)


def _label(t):
    """A task's label as the report shows it, masked and on one line: a built label as it is, and
    the agent's own words in quotes with where they came from, `[commit]`, `[pr #235]`, or
    `[log]`."""
    text = cut(t["label"], LABEL_CHARS)
    if t.get("source") is None:
        return text
    return '"%s" [%s%s]' % (text, t["source"], " " + t["ref"] if t.get("ref") else "")


def task_line(i, t, day):
    """`  T<i> HH:MM–HH:MM  <dur>  <kind>  <label>`: task `i` of a block's list, its span, its
    length, and its label (`_label`). The clock carries its date when it is not on `day`."""
    return "  T%d %s–%s  %s  %s  %s" % (
        i, clock(t["t0"], day, MINUTE), clock(t["t1"], day, MINUTE), span(t["t1"] - t["t0"]),
        t["kind"], _label(t))


def gap_line(gap, day):
    """`  gap <dur>`, and ` · <name> still running since HH:MM` for each call that was running as
    the gap began, the earliest first, as the page words it: the name the service gives the call,
    else its preview, else `a call`. A call still open has no preview, as in a phase."""
    bits = ["  gap " + span(gap["s"])]
    for preview, since, name in gap["alive"]:
        bits.append("%s still running since %s" % (
            cut(name or preview or "a call"), clock(since, day, MINUTE)))
    return " · ".join(bits)


def lone_line(t, day):
    """`  <n> call(s) at HH:MM · <label>`: a lone stretch, as the page words it."""
    return "  %s at %s · %s" % (plural(t["n"], "call"), clock(t["t0"], day, MINUTE), _label(t))


def task_lines(tasks, day):
    """The lines of a block's `tasks`, in time order: each task or lone stretch, with the gap before
    it, when a gap cut there, on a line above it. A task is numbered by its place in the list, which
    a lone stretch takes a place in too: `T2` is `tasks[2]`, and a phase's `task` is that index."""
    out = []
    for i, t in enumerate(tasks):
        if t.get("gap"):
            out.append(gap_line(t["gap"], day))
        out.append(lone_line(t, day) if t["kind"] == "lone" else task_line(i, t, day))
    return out


def task_header(block):
    """`  <n> tasks, <m> lone stretches · headline "<headline>"`, for a block. The block of an agent
    that worked on more than one day says how many tasks each day holds, which is what the summary
    counts for a session-day: a task is the day it begins in. A block with none says so."""
    tasks = block.get("tasks") or []
    if not tasks:
        return "  no tasks"
    kinds = collections.Counter(t["kind"] for t in tasks)
    line = "  " + plural(kinds["task"], "task")
    if kinds["lone"]:
        line += ", " + plural(kinds["lone"], "lone stretch", "lone stretches")
    days = block.get("days")
    if days:
        per_day = collections.Counter(t["sid"] for t in tasks if t["kind"] == "task")
        line += " over %d days (%s)" % (len(days), ", ".join(
            "%d on %s" % (per_day[d["sid"]], d["sid"].split("|")[1]) for d in days))
    if block.get("headline"):
        line += ' · headline "%s"' % cut(block["headline"], LABEL_CHARS)
    return line


def print_row(out, s, row, mem, calls, why, day, traj, shared=None):
    """One row asked for. `shared` is the sid of the row that has printed the tasks of the block
    this row shares with it, when one has: they are the session's, and are printed once."""
    ph = row["phases"]
    print("-- %s · %s · agent pid %s · tree %s · %s · %s · %s" % (
        "; ".join(why), row["sid"], row["agent_pid"], s.tree_root, row["user"],
        row["agent_type"], "session" if traj.rooted(s) else "probe (no agent process at its root)"),
        file=out)
    print("   %d collector key(s): %s%s" % (
        len(mem), ", ".join(m.rsplit("|", 1)[-1] for m in mem[:4]),
        " (+%d more)" % (len(mem) - 4) if len(mem) > 4 else ""), file=out)
    print("   records %s · first %s · last %s · n_calls %s" % (
        "{:,}".format(row["n_events"]), clock(row["first"], day) if row["first"] else "?",
        clock(row["last"], day) if row["last"] else "?", row["n_calls"]), file=out)
    if "list" not in ph:
        print("   phases: %s" % ph.get("unavailable"), file=out)
        return
    shapes = sorted(s.phase.hk.items(), key=lambda kv: -kv[1])[:8]
    print("   housekeeping %s (%s)%s" % (
        "{:,}".format(sum(s.phase.hk.values()) + calls.hidden),
        ", ".join("%s %s" % (k, "{:,}".format(v)) for k, v in shapes),
        " · %s under a process that never exited" % "{:,}".format(calls.hidden)
        if calls.hidden else ""), file=out)
    cats = collections.Counter(c[CAT] for c in calls.own)
    st = row_stats(ph, calls.block, row["sid"], calls.own)
    print("   calls by category: %s · open %d · cut %s" % (
        ", ".join("%s %d" % kv for kv in cats.most_common()) or "none",
        sum(1 for c in calls.own if c[FLAGS] & OPEN), ph["cut"]), file=out)
    print("   %d phases (not idle) over %d active hour(s): %s per active hour" % (
        st["phases"], st["active_hours"],
        "%.1f" % st["per_hour"] if st["per_hour"] is not None else "-"), file=out)
    at = place(ph["list"], calls.block)
    by_phase = collections.defaultdict(collections.Counter)
    for i, c in zip(at, calls.block):
        if i is not None:
            by_phase[i][c[CAT]] += 1
    for i, p in enumerate(ph["list"]):
        if belongs(p, row["sid"]):
            print("     " + _phase_line(p, day, by_phase.get(i)), file=out)
    if st["no_edit"] or st["off"] or st["unplaced"]:
        print("   !! solid edits without an edit call %s · phases whose count differs %s · "
              "calls placed in no phase %d" % (st["no_edit"], st["off"], st["unplaced"]), file=out)
    if shared is not None:
        print("  (same session as %s)" % shared, file=out)
        return
    print(task_header(ph), file=out)
    for line in task_lines(ph.get("tasks") or [], day):
        print(line, file=out)


def _pct(a, b):
    return "%.0f%%" % (100.0 * a / b) if b else "-"


def report(out, traj, read, day, apids=(), trees=(), top=15, days=1):
    """Print the report of the `days` days from `day`, as `reduce_day` read them, to `out`; returns
    the summary as a dict."""
    apids, trees = set(apids), set(trees)
    rows = rows_of(traj)
    print("phase report · %s · %s records from %s · %.0f MB · read in %s s" % (
        day_range(day, days), "{:,}".format(read["records"]), ", ".join(read["files"]),
        read["bytes"] / 1e6, read["seconds"]), file=out)
    print("thresholds: %s · tasks: %s" % (json.dumps(traj.phase_params, sort_keys=True),
                                          json.dumps(traj.task_params, sort_keys=True)), file=out)
    print("raw sessions: %s, held in one pass (max_sessions %s is lifted: a window is reduced "
          "hour by hour, and merging past it evicts what was last seen over half a window "
          "before its clock)" % ("{:,}".format(len(traj.sessions) + traj.evicted),
                                 "{:,}".format(read.get("max_sessions", traj.max_sessions))),
          file=out)
    if read["errors"] or read["parse_errors"]:
        print("!! reducer errors %s · parse errors %d" % (read["errors"], read["parse_errors"]),
              file=out)

    # -- the rows asked for
    if apids or trees:
        asked = [(s, row, mem, calls, matches(s, mem, apids, trees))
                 for s, row, mem, calls in rows]
        asked = [a for a in asked if a[4]]
        asked.sort(key=lambda a: (a[0].tree_root or 0, a[1]["sid"]))
        print("\n== rows asked for: %d" % len(asked), file=out)
        found, printed = set(), {}      # and the row that printed the tasks of each shared block
        for s, row, mem, calls, why in asked:
            session = tuple(d["sid"] for d in row["phases"].get("days") or ())
            print_row(out, s, row, mem, calls, why, day, traj, printed.get(session))
            if session:
                printed.setdefault(session, row["sid"])
            found.update(_key_pid(m) for m in mem)
            found.update((s.tree_root, _key_pid(s.sid)))
        missing = sorted((apids | trees) - found)
        if missing:
            print("!! no row for: %s" % ", ".join(map(str, missing)), file=out)

    # -- calls by kind
    print("\n== calls by kind (direct children of an agent read as tool calls)", file=out)
    for at in sorted({k[0] for k in traj.by_kind}, key=str):
        print("%s: %s" % (at, " · ".join("%s %s" % (k, "{:,}".format(traj.by_kind[(at, k)]))
                                         for k in KINDS if traj.by_kind[(at, k)])), file=out)
        for k in KINDS:
            got = [(sh, n) for (a, kk, sh), n in traj.shapes.items() if a == at and kk == k]
            if not got:
                continue
            got.sort(key=lambda x: (-x[1], str(x[0])))
            print("  %s by shape: %s" % (k, " · ".join(
                "%s %s" % (sh, "{:,}".format(n)) for sh, n in got[:top])), file=out)
            for sh, n in got[:EXAMPLES]:
                print("    e.g. %s: %s" % (sh, traj.example.get((at, k, sh), "")), file=out)
        argv = sorted(((a, n) for (aa, a), n in traj.bare_argv.items() if aa == at),
                      key=lambda x: (-x[1], x[0]))
        if argv:
            print("  bare by argv: %s" % " · ".join("%s ×%s" % (a, "{:,}".format(n))
                                                   for a, n in argv[:top]), file=out)

    # -- the summary
    sup = [(s, row, mem, calls) for s, row, mem, calls in rows if "list" in row["phases"]]
    unavail = collections.Counter(row["agent_type"] for s, row, mem, calls in rows
                                  if s.cls == "agent" and "list" not in row["phases"])
    stats = [(s, row, row_stats(row["phases"], calls.block, row["sid"], calls.own))
             for s, row, mem, calls in sup]
    real = [x for x in stats if traj.rooted(x[0])]
    print("\n== summary over the rows whose calls are recognized (%s)" %
          ", ".join(sorted(PHASE_AGENTS)), file=out)
    print("rows: %d: %d sessions (an agent process at the root) and %d probes (none, as the "
          "page counts them); calls %s" % (
              len(sup), len(real), len(sup) - len(real),
              "{:,}".format(sum(row["n_calls"] for _s, row, _m, _c in sup))), file=out)
    print("agent rows whose calls are not recognized: %s" % (
        ", ".join("%s %d" % (k, v) for k, v in unavail.most_common()) or "none"), file=out)
    dist = _distribution(out, "sessions", real, days=days)
    _distribution(out, "every row, probes too", stats, brief=True, days=days)

    opens = [(s, row, p) for s, row, _m, _c in sup
             for p in of_day(row["phases"]["list"], row["sid"]) if p.get("open")]
    print("open phases: %d in %d rows" % (len(opens), len({row["sid"] for _s, row, _p in opens})),
          file=out)
    for s, row, p in sorted(opens, key=lambda x: -(x[2]["t1"] - x[2]["t0"]))[:top]:
        print("  %s %s %s–%s %s %s" % (
            row_tag(row, days), p["kind"], clock(p["t0"], day), clock(p["t1"], day),
            span(p["t1"] - p["t0"]), " | ".join(cut(t, 50) for t, _n in p.get("top") or [])),
            file=out)

    hk = collections.Counter()
    for s, _row, _m, calls in sup:
        hk.update(s.phase.hk)
        hk[NEVER_EXITED] += calls.hidden
    hk = +hk
    print("housekeeping: %s commands; by shape (top 20): %s" % (
        "{:,}".format(sum(hk.values())), " · ".join(
            "%s %s" % (k, "{:,}".format(v)) for k, v in hk.most_common(20))), file=out)

    no_edit = [(row["sid"], i) for _s, row, st in stats for i in st["no_edit"]]
    off = [(row["sid"], i) for _s, row, st in stats for i in st["off"]]
    unplaced = sum(st["unplaced"] for _s, _row, st in stats)
    kinds = collections.Counter(p["kind"] for _s, row, _m, _c in sup
                                for p in of_day(row["phases"]["list"], row["sid"]))
    print("phases by kind: %s" % ", ".join("%s %d" % kv for kv in kinds.most_common()), file=out)
    # each task is counted by the row of the day it begins in, so a session is counted once; a lone
    # stretch is no task, and is counted apart
    tasks = [t for _s, row, _m, _c in sup for t in day_tasks(row)]
    words = collections.Counter(t["source"] for t in tasks if t["source"])
    by_source = sorted(words.items(), key=lambda kv: (-kv[1], kv[0]))
    print("tasks named by the agent's words: %d of %d (%s)%s" % (
        sum(words.values()), len(tasks), _pct(sum(words.values()), len(tasks)),
        ": " + ", ".join("%s %d" % kv for kv in by_source) if words else ""), file=out)
    lone = [row["sid"] for _s, row, _m, _c in sup
            for t in of_day(row["phases"].get("tasks") or [], row["sid"]) if t["kind"] == "lone"]
    print("lone stretches: %d in %s" % (len(lone), plural(len(set(lone)), "row")), file=out)
    print("solid edits without an edit call: %d%s" % (
        len(no_edit), " %s" % no_edit[:10] if no_edit else ""), file=out)
    print("phases whose call count differs from the calls placed in them: %d%s; calls placed in "
          "no phase: %d" % (len(off), " %s" % off[:10] if off else "", unplaced), file=out)
    print("pending join at the day's end: %s entries (cap %s), %s evicted, %s refused as an "
          "earlier process's (%s records); call index %s, %s evicted; past-due dropped %s; "
          "reparented %s; held by a root to its exit %s; preview overflow %s; phase_tag_overflow "
          "%s; sessions evicted %s"
          % tuple("{:,}".format(v) for v in (
              len(traj.pend.entries), traj.pend.cap, traj.pend.evicted, traj.pend.refused,
              traj.pend.refused_records, len(traj.calls_at), traj.calls_at_evicted,
              traj.past_due_evicted, traj.phase_reparented, traj.phase_unread,
              traj.preview_overflow, traj.tag_overflow, traj.evicted)), file=out)
    print("late children: where each process went, %s noted (cap %s), %s dropped; placed where "
          "their parent went %s; placed nowhere %s; under a record with no time %s; pushed with "
          "no start to check %s; pushed past %s parents up %s; entries started under a parent "
          "whose note may have been dropped, at most %s" % tuple("{:,}".format(v) for v in (
              len(traj.exited), trajmod.EXITED_MAX, traj.exited_evicted, traj.late_routed,
              traj.late_unplaced, traj.phase_untimed, traj.late_no_start, trajmod.LATE_HOPS,
              traj.late_hops_out, traj.late_maybe_evicted)), file=out)
    return dict(dist, rows=len(sup), sessions=len(real), open=len(opens),
                no_edit=len(no_edit), off=len(off), pending=len(traj.pend.entries),
                pending_evicted=traj.pend.evicted, tasks=len(tasks), named=sum(words.values()),
                lone=len(lone), tag_overflow=traj.tag_overflow)


def day_tasks(row):
    """The tasks of a row's own day, not the lone stretches (`belongs`): a task is the day it
    begins in, and the session-day is what the cap on tasks is for."""
    return [t for t in of_day(row["phases"].get("tasks") or [], row["sid"]) if t["kind"] == "task"]


def _distribution(out, label, stats, brief=False, days=1):
    """Print phases per active hour and per row, and tasks per session-day, over `stats`
    ([(session, row, row_stats)]); returns them. Rows with no call have no active hour and are
    counted apart. Tasks are counted over the rows with `TASK_HOURS` active hours or more, since a
    short row has few of them however it is cut."""
    busy = [x for x in stats if x[2]["per_hour"] is not None]
    per_hour = [x[2]["per_hour"] for x in busy]
    per_row = [x[2]["phases"] for x in busy]
    small = sum(1 for v in per_hour if v <= HANDFUL)
    worst = max(busy, key=lambda x: x[2]["per_hour"], default=None)
    held = [(len(day_tasks(x[1])), x) for x in busy if x[2]["active_hours"] >= TASK_HOURS]
    per_day = [n for n, _x in held]
    most = max(held, key=lambda h: h[0], default=None)
    print("-- %s: %d rows, %d with calls, %d with none (housekeeping only)" % (
        label, len(stats), len(busy), len(stats) - len(busy)), file=out)
    print("   phases per active hour: median %s, p90 %s, max %s%s; at %d or fewer: %d of %d (%s)" % (
        _num(quantile(per_hour, 0.5)), _num(quantile(per_hour, 0.9)),
        _num(max(per_hour, default=None)),
        " (%s, %d phases over %d h)" % (row_tag(worst[1], days), worst[2]["phases"],
                                        worst[2]["active_hours"]) if worst else "",
        HANDFUL, small, len(per_hour), _pct(small, len(per_hour))), file=out)
    print("   phases per row: median %s, p90 %s, max %s" % (
        quantile(per_row, 0.5), quantile(per_row, 0.9), max(per_row, default=None)), file=out)
    print("   tasks per session-day: median %s, p90 %s, max %s%s; rows with %d or more active "
          "hours: %d of %d" % (
              _whole(quantile(per_day, 0.5)), _whole(quantile(per_day, 0.9)),
              _whole(max(per_day, default=None)),
              " (%s, %s over %d h)" % (row_tag(most[1][1], days), plural(most[0], "task"),
                                       most[1][2]["active_hours"]) if most else "",
              TASK_HOURS, len(per_day), len(busy)), file=out)
    if not brief:
        hist = collections.Counter(min(int(v), 20) for v in per_hour)
        print("   rows by phases per active hour (floor): %s" % " ".join(
            "%s%s:%d" % (k, "+" if k == 20 else "", hist[k]) for k in sorted(hist)), file=out)
        top_rows = sorted(busy, key=lambda x: -x[2]["phases"])[:10]
        print("   most phases: %s" % "; ".join("%s %d (%.1f/h, %d calls)" % (
            row_tag(x[1], days), x[2]["phases"], x[2]["per_hour"], x[1]["n_calls"])
            for x in top_rows), file=out)
    return {"per_hour": {"median": quantile(per_hour, 0.5), "p90": quantile(per_hour, 0.9),
                         "max": max(per_hour, default=None)},
            "per_row": {"median": quantile(per_row, 0.5), "p90": quantile(per_row, 0.9),
                        "max": max(per_row, default=None)},
            "tasks_per_day": {"median": quantile(per_day, 0.5), "p90": quantile(per_day, 0.9),
                              "max": max(per_day, default=None), "rows": len(per_day)},
            "handful_share": (small / len(per_hour)) if per_hour else None,
            "rows_with_calls": len(busy)}


def _num(v):
    return "-" if v is None else "%.1f" % v


def _whole(v):
    return "-" if v is None else "%d" % v


def build_parser():
    ap = argparse.ArgumentParser(prog="python -m rc_dashboard.phase_report",
                                 description="Agents' phases and tasks over real days.")
    ap.add_argument("--roots", nargs="+", required=True,
                    help="feed roots, laid out <root>/<YYYY-MM-DD>/<host>.*")
    ap.add_argument("--day", required=True, help="the first local day, YYYY-MM-DD")
    ap.add_argument("--days", type=positive_int, default=1,
                    help="how many local days to read, from --day (default 1)")
    ap.add_argument("--apid", nargs="*", type=int, default=[],
                    help="print the rows of these agent pids")
    ap.add_argument("--tree", nargs="*", type=int, default=[],
                    help="print the rows in these trees (their root's pid)")
    ap.add_argument("--top", type=int, default=15, help="shapes and open phases listed")
    ap.add_argument("--config", default=None)
    return ap


def main(argv=None, out=None):
    args = build_parser().parse_args(argv)
    out = out or sys.stdout
    cfg = configmod.load(None, args.config)
    since, until = day_bounds(args.day, args.days)
    dirs = day_dirs(args.roots, args.day, args.days)
    if not dirs:
        print("no directory for %s under %s" % (day_range(args.day, args.days),
                                                ", ".join(args.roots)), file=sys.stderr)
        return 2
    traj, read = reduce_day(cfg, dirs, since, until,
                            progress=lambda m: print("  " + m, file=sys.stderr, flush=True))
    report(out, traj, read, args.day, args.apid, args.tree, args.top, args.days)
    return 0


if __name__ == "__main__":
    sys.exit(main())
