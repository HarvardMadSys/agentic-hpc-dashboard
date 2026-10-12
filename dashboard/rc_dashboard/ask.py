"""Chat with an LLM about the window on screen.

A question becomes one run of an agent CLI on this host, as the user the
service runs as -- by default `codex exec`, with that user's own Codex login.
The agent is given the question, the last few turns of the conversation, and
a compact snapshot of the window's panels; it is not given a shell to the
data. The snapshot is built from the same payload the page reads, masked by
`redact.payload` like everything else this service serves, pruned to a size
budget, and stripped of command lines unless `ask.include_commands` says
otherwise. It leaves the host: it is sent to whichever model the agent uses.

A question can also be about one thing on the page (roadmap notes 40 and 51):
one panel, whose snapshot is that panel's data and nothing else
(`aggregator.ask(context={"parts": ...})`), or one session, whose snapshot is
its process tree read into a digest (`session_digest`). Either way the data is
the service's own, built here -- never what the page posted -- and masked like
the rest.

Configurable under `ask` (config.py): `agent` picks a preset (`codex`), or
`command` gives the argv outright -- `{workdir}`, `{answer_file}` and `{model}`
are filled in, the prompt arrives on stdin, and a command with no
`{answer_file}` has its stdout taken as the answer. `max_concurrent` (1)
bounds how many agents run at once: on a small deployment a second question
waits rather than starting a second agent.
"""
import collections
import json
import os
import shlex
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import uuid

from . import redact

# Argv for each preset. The prompt is piped on stdin (`-`), the agent's last
# message is written to {answer_file}, and --json streams its events so the
# page can say what it is doing. read-only and --ephemeral: a question must
# not leave files, sessions or changes behind.
PRESETS = {
    "codex": ["codex", "exec", "--skip-git-repo-check", "--ephemeral",
              "--sandbox", "read-only", "--color", "never", "--json",
              "-C", "{workdir}", "-o", "{answer_file}", "{model_flag}", "-"],
}

# Bulky, per-row, or per-bin fields that cost the budget and answer little a
# question about the window needs; the summaries beside them stay.
DROP_KEYS = frozenset((
    "series", "stall_series", "rate", "bins", "t", "events", "instances",
    "top_procs", "nodes", "items", "chain", "purpose", "chain_t", "purpose_t",
    "tool_rle", "purpose_rle", "tool_t", "notes", "basis", "cpu_basis",
    "impossible_basis", "by_class_detail", "nav", "_paths", "_notice", "_window",
    "_feed", "_status", "_present", "_filter", "resident_hosts",
))
ARGV_KEYS = frozenset(("args", "argv", "cmd", "command_line", "_argv"))

DEFAULT_SYSTEM = """You are the assistant inside an agent-observability dashboard.
People ask what AI coding agents (Claude Code, Codex, ...), people working in a
terminal, and IDE sessions (VS Code, Cursor, ...) did on the machines this
dashboard watches -- which may be a cluster's login or compute nodes, a
workstation, or any other computer; the deployment line below says which, when
it is known. Do not assume a kind of machine it does not state, and judge what
counts as risky by the dashboard's own rules for this site (panels.risk), not
by general rules for any one kind of host. The dashboard's classes are:
agent = an AI coding agent and everything it ran; human = a person's own
terminal; ide = an editor session.

Answer from the snapshot below. Quote the numbers you use, with their units.
If the snapshot cannot answer the question, say so plainly and say what would.
Be concise: a few sentences or a short list, unless asked for more. Write only
the final answer: no working, no second thoughts, no self-corrections. Do not
run shell commands or read files -- everything you have is below."""

# How the snapshot is laid out. Kept apart from the system prompt: a site that
# writes its own prompt still wants the model to find its way round the data,
# which only this code can describe. `ask.snapshot_guide: false` drops it.
GUIDE = """Where to look in the snapshot:
- live_now.users.rows: one row per user -- n (processes run in the window),
  by_class, types (processes per agent type, per class), live (sessions
  running now, per agent type).
- facets: the top users and agent types by processes, with their class mix.
- panels.tool_mix: commands per class and per kind of tool; by_type, each
  agent type's share of calls and CPU.
- panels.risk: dangerous commands and credentials on command lines -- counts,
  users, and who ran them (types).
- panels.io_process: disk and network bytes per class, tool, and user.
- panels.resources: CPU, memory, and waits per class.
- panels.sandbox: whether agents' tool calls ran isolated, and how sessions
  were launched (approval).
- panels.cascades: agent trees and subagents; its workspace block is where
  agents worked and what they wrote outside their home.
- sessions: per-class session summaries and the busiest sessions.
- Lists cut short end with "... N more"."""


# One panel asked about on its own keeps the fields that say how its numbers
# are computed, and has room for longer lists: its budget is not shared.
LOCAL_KEEP = frozenset(("notes", "basis", "cpu_basis", "impossible_basis"))


def panel_guide(title, tab, parts):
    """The guide for a question about one panel: what the snapshot is, and
    that it is all there is."""
    where = '"%s"' % title if title else "one panel"
    if tab:
        where += " on the %s tab" % tab
    return ("""The snapshot is the data behind ONE panel the reader has open -- %s --
and nothing else from the dashboard: %s. Answer about that panel. When the
question needs data the panel does not carry, say so, and say which part of the
dashboard would answer it. Field names are the panel's own; a `basis` or `notes`
field, where there is one, says how its numbers are counted. `view` says which
window, filter, and classes the reader has on screen. Lists cut short end with
"... N more".""" % (where, ", ".join(parts) or "its data"))


SESSION_GUIDE = """The snapshot is ONE session the reader opened on the Trajectories tab -- an
agent (or a person's terminal, or an IDE) and every process it ran, re-read from
the collector's records -- and nothing else from the dashboard.
- session: whose, where, when (the host's local time), and its totals.
- tools: the commands it ran, in order, as runs (`git x25` is 25 in a row);
  purposes: the same sequence by the dashboard's kind of work, and each kind's
  share of the processes -- a count, not a share of the session's time.
- landmarks: the first tool call, the busiest minute, the longest process, and
  the failures (a non-zero exit or a signal), by command, with the first few.
- tree: the process tree as an indented outline, one line per process: its
  cost (dur = wall time, cpu = its own CPU time, sum cpu = with everything it
  started), how many processes it and everything below it make, and how it
  ended. A parent with many children lists them by command first, then opens
  the costliest few.
- read: how complete the read was; processes "counted" are in every total but
  not itemised in the tree."""


def _clock(t):
    return time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime(t)) if t else None


def _secs(v):
    if v is None:
        return None
    if v >= 3600:
        return "%.1f h" % (v / 3600)
    if v >= 60:
        return "%.1f min" % (v / 60)
    if v >= 1:
        return "%.1f s" % v
    return "%d ms" % round(v * 1000)


def _runs(rle, keep):
    """`[["git", 25], ["wc", 1]]` as `git x25 > wc`, the first `keep` runs."""
    rle = rle or []
    text = " > ".join("%s x%d" % (k, n) if n > 1 else str(k) for k, n in rle[:keep])
    return text + (" > ... %d more runs" % (len(rle) - keep) if len(rle) > keep else "")


def _ended(e):
    if e.get("signal") is not None:
        return "killed by signal %s" % e["signal"]
    if e.get("exit_code") is not None:
        return "exit %s" % e["exit_code"]
    return "running at the end of the read" if e.get("ev") == "truncated" else "no exit seen"


def _failed(e):
    return e.get("signal") is not None or e.get("exit_code") not in (None, 0)


def _name(e):
    k = e.get("k")
    if k == "g":
        return " > ".join(e.get("motif") or ["?"])
    if k == "agg":
        return "(counted)"
    if k == "more":
        return "(past the budget)"
    return e.get("label") or e.get("cmd") or "pid %s" % e.get("pid")


def _tree_line(e, with_argv, root=False):
    k = e.get("k")
    if k == "g":
        exits = e.get("exits") or {}
        bad = sum(n for c, n in exits.items() if c != "0") + sum((e.get("signals") or {}).values())
        return "%s x%d in a row (%d processes)%s%s" % (
            _name(e), e.get("reps") or e.get("count") or 0, e.get("n") or 0,
            ", sum cpu %s" % _secs(e["sub_cpu"]) if e.get("sub_cpu") else "",
            ", %d failed" % bad if bad else ", all exit 0" if exits else "")
    if k == "agg":
        cmds = ", ".join("%s x%d" % (c, n) for c, n in (e.get("cmds") or [])[:8])
        return "+%d more processes counted, not itemised%s%s" % (
            e.get("n") or 0, " (%s)" % cmds if cmds else "",
            ", sum cpu %s" % _secs(e["sub_cpu"]) if e.get("sub_cpu") else "")
    if k == "more":
        return "+%d more entries past the read's budget (%d processes)" % (
            e.get("count") or 0, e.get("n") or 0)
    name = _name(e)
    if e.get("eff") and e["eff"] != name:
        name += " (runs %s)" % e["eff"]
    tags = [t for t in ((e.get("agent_type") if root or e.get("role") == "agent" else None),
                        e.get("role")) if t]
    bits = ["dur %s" % _secs(e["dur"]) if e.get("dur") is not None else None,
            "cpu %s" % _secs(e["cpu"]) if e.get("cpu") is not None else None,
            "sum cpu %s" % _secs(e["sub_cpu"])
            if e.get("sub_cpu") and (e.get("cpu") or 0) * 1.05 < e["sub_cpu"] else None,
            "%d processes" % e["n"] if (e.get("n") or 0) > 1 else None,
            _ended(e)]
    line = "%s%s: %s" % (name, " [%s]" % ", ".join(tags) if tags else "",
                         ", ".join(b for b in bits if b))
    text = e.get("cmdline") or e.get("args")
    if with_argv and text:
        line += " | " + text[:160]
    return line


def _outline(root, with_argv, max_lines=300, max_depth=6, open_n=10):
    """The tree as indented lines. A parent with more children than it opens
    lists them by command first; then it opens the ones that started others,
    the costliest first, then the costliest of the rest -- `open_n` near the
    root, half that deeper, two of any one command -- in the order they
    started. What the read only counted is one line under its parent, never a
    census entry."""
    lines = []

    def emit(e, depth):
        if len(lines) >= max_lines:
            return
        pad = "  " * depth
        lines.append(pad + _tree_line(e, with_argv, root=depth == 0))
        kids = (e.get("kids") or []) if e.get("k") == "p" else []
        if not kids or depth >= max_depth:
            return
        summ = [c for c in kids if c.get("k") in ("agg", "more")]
        rest = [c for c in kids if c.get("k") not in ("agg", "more")]
        n_open = open_n if depth < 2 else max(3, open_n // 2)
        pick = rest
        if len(rest) > n_open:
            census = {}
            for c in rest:
                row = census.setdefault(_name(c), [0, 0, 0])   # runs, processes, failed
                row[0] += (c.get("count") or 1) if c.get("k") == "g" else 1
                row[1] += c.get("n") or 0
                row[2] += _failed(c) if c.get("k") == "p" else sum(
                    n for x, n in (c.get("exits") or {}).items() if x != "0")
            top = sorted(census.items(), key=lambda kv: -kv[1][1])[:12]
            lines.append(pad + "  %d children by command: %s%s" % (
                len(rest),
                "; ".join("%s x%d%s%s" % (nm, r[0],
                                          " (%d processes)" % r[1] if r[1] > r[0] else "",
                                          ", %d failed" % r[2] if r[2] else "")
                          for nm, r in top),
                "; ... %d more commands" % (len(census) - len(top)) if len(census) > len(top) else ""))
            ranked = sorted(rest, key=lambda c: (not (c.get("k") == "p" and (c.get("n") or 0) > 1),
                                                 -(c.get("sub_cpu") or 0), -(c.get("n") or 0)))
            # two of a command at most: ten copies of one helper's subtree say
            # less than the census line already has
            seen, pick = collections.Counter(), []
            for c in ranked:
                if len(pick) >= n_open:
                    break
                if seen[_name(c)] < 2:
                    pick.append(c)
                seen[_name(c)] += 1
            pick.sort(key=lambda c: c.get("start") or 0)
        for c in pick:
            emit(c, depth + 1)
        if len(pick) < len(rest) and len(lines) < max_lines:
            lines.append(pad + "  ... %d more children, in the census above" % (len(rest) - len(pick)))
        for c in summ:
            if len(lines) < max_lines:
                lines.append(pad + "  " + _tree_line(c, with_argv))

    emit(root, 0)
    if len(lines) >= max_lines:
        lines.append("... the outline stops at %d lines" % max_lines)
    return lines


def session_digest(body, with_argv=False, max_lines=300):
    """One session's process tree (proctree.inspect's body) as what a question
    about it needs: the totals, the sequence, the landmarks, and an outline of
    the tree -- a few tens of KB where the tree is a few MB."""
    row = body.get("row") or {}
    nav = body.get("nav") or {}
    scan = body.get("scan") or {}
    bounds = body.get("bounds") or {}
    keep = ("sid", "host", "actor3", "agent_type", "user", "n_users", "n_events", "cpu_s",
            "distinct_tools", "chain_runs", "peak_rss_mb", "io_rd_mb", "io_wr_mb",
            "sandbox", "approval", "autonomous", "drift")
    session = {k: row.get(k) for k in keep if row.get(k) is not None}
    session.update(first=_clock(row.get("first")), last=_clock(row.get("last")),
                   wall=_secs(row.get("wall_s")))
    shares = collections.Counter()
    for k, n in row.get("purpose_rle") or []:
        shares[k] += n
    total = sum(shares.values()) or 1
    fails = (nav.get("failures") or {})
    items = fails.get("items") or []
    by = collections.Counter()
    for _t0, _t1, _pid, cmd, code, sig in items:
        by["%s %s" % (cmd, "signal %s" % sig if sig is not None else "exit %s" % code)] += 1

    def mark(m, **kv):
        if not m:
            return None
        out = {"cmd": m.get("cmd"), "at": _clock(m.get("t"))}
        out.update({k: v for k, v in kv.items() if v is not None})
        return out

    longest = nav.get("longest")
    busiest = nav.get("busiest")
    return {
        "session": session,
        "tools": _runs(row.get("tool_rle"), 200)
                 + (" (the recorded sequence stops here)" if row.get("chain_truncated") else ""),
        "purposes": {"sequence": _runs(row.get("purpose_rle"), 80),
                     "share_pct": {k: round(100 * n / total, 1) for k, n in shares.most_common()}},
        "landmarks": {
            "first_tool_call": mark(nav.get("first_tool")),
            "busiest_minute": {"at": _clock(busiest["t"]), "process_starts": busiest["n"]}
                              if busiest else None,
            "longest_process": mark(longest, dur=_secs((longest or {}).get("dur"))),
            "failures": {"total": fails.get("n") or 0,
                         "by_command": ["%s x%d" % kv for kv in by.most_common(15)],
                         "first": ["%s %s %s" % (_clock(t0), cmd, "signal %s" % sig if sig is not None
                                                 else "exit %s" % code)
                                   for t0, _t1, _pid, cmd, code, sig in items[:8]]},
        },
        "tree": _outline(body.get("tree") or {}, with_argv, max_lines) if body.get("tree") else [],
        "read": {"records": scan.get("records"), "itemised": bounds.get("itemised"),
                 "counted": bounds.get("counted"),
                 "stopped": scan.get("stopped"),
                 "from": _clock(scan.get("first")), "to": _clock(scan.get("last"))},
    }


def _compact(obj, list_max, str_max, with_argv, drop=DROP_KEYS):
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in drop or (not with_argv and k in ARGV_KEYS):
                continue
            out[k] = _compact(v, list_max, str_max, with_argv, drop)
        return out
    if isinstance(obj, list):
        head = [_compact(v, list_max, str_max, with_argv, drop) for v in obj[:list_max]]
        if len(obj) > list_max:
            head.append("... %d more" % (len(obj) - list_max))
        return head
    if isinstance(obj, str):
        # every string, not only the command-line fields `redact.payload`
        # knows: the snapshot leaves the host
        obj = redact.redact(obj)
        return obj[:str_max] + "..." if len(obj) > str_max else obj
    if isinstance(obj, float):
        return round(obj, 3)
    return obj


STEPS = ((12, 200), (8, 160), (5, 120), (3, 80), (2, 60), (1, 40))
# One panel: its lists start long and its strings may carry a `basis` note.
LOCAL_STEPS = ((60, 400), (30, 300), (15, 200), (8, 160), (5, 120), (3, 80), (2, 60), (1, 40))
# One session: the digest's outline is a list of a few hundred short lines.
SESSION_STEPS = ((400, 300), (200, 240), (100, 200), (50, 160), (25, 120), (12, 80))


def snapshot(panels, live=None, budget_kb=120, with_argv=False, extra=None,
             steps=STEPS, keep=()):
    """The window as compact JSON text, inside `budget_kb`.

    Each section is compacted on its own, and while the whole is over budget
    the LARGEST section is cut down a step (12 rows per list, then 8, 5, ...):
    one sprawling panel -- the agent trees run to 60 KB -- gives up its tails
    before any other panel loses rows, and no panel is dropped whole.

    `steps` and `keep` are for a question about one panel or one session,
    whose budget is not shared: longer lists, and the fields in `keep` (the
    panel's own notes on how it counts) are not dropped."""
    drop = DROP_KEYS - frozenset(keep)
    sections = {"panels." + k: v for k, v in (panels or {}).items()}
    if live:
        sections["live_now"] = {k: live.get(k) for k in ("now", "users", "hosts", "window")
                                if live.get(k) is not None}
    for k, v in (extra or {}).items():
        sections[k] = v
    step = {k: 0 for k in sections}
    part = {k: json.dumps(_compact(v, *steps[0], with_argv, drop), separators=(",", ":"),
                          default=str) for k, v in sections.items()}
    while sum(len(t) for t in part.values()) > budget_kb * 1024:
        k = max((k for k in part if step[k] < len(steps) - 1),
                key=lambda k: len(part[k]), default=None)
        if k is None:
            break
        step[k] += 1
        part[k] = json.dumps(_compact(sections[k], *steps[step[k]], with_argv, drop),
                             separators=(",", ":"), default=str)
    out = {"panels": {}}
    for k, t in part.items():
        if k.startswith("panels."):
            out["panels"][k[len("panels."):]] = json.loads(t)
        else:
            out[k] = json.loads(t)
    return json.dumps(out, separators=(",", ":"))[:budget_kb * 1024]


def deployment(cfg, live=None):
    """One line on what is being watched: the site's own description if it
    has one, its name, and the hosts reporting -- never a guess at their role."""
    hosts = [h.get("host") for h in ((live or {}).get("hosts") or []) if h.get("host")]
    parts = [cfg.get("site.description") or "", ("site: %s" % cfg.get("site.name"))
             if cfg.get("site.name") else ""]
    if hosts:
        parts.append("%d host%s reporting: %s" % (len(hosts), "" if len(hosts) == 1 else "s",
                                                 ", ".join(hosts[:12])))
    return "; ".join(p for p in parts if p) or "not described"


def system_text(cfg, about="not described", window_label=""):
    """The answerer's instructions: `ask.system_prompt_file` (read on every
    question, so it can be tuned without a restart), else `ask.system_prompt`,
    else the built-in one. `{deployment}`, `{site}` and `{window}` are filled
    in; any other braces are left alone."""
    path = cfg.get("ask.system_prompt_file") or ""
    if path:
        try:
            with open(os.path.expanduser(path), encoding="utf-8") as fh:
                text = fh.read()
        except OSError as e:
            raise ValueError("ask.system_prompt_file %s: %s" % (path, e.strerror or e))
    else:
        text = cfg.get("ask.system_prompt") or DEFAULT_SYSTEM
    return (text.replace("{deployment}", about)
                .replace("{site}", cfg.get("site.name") or "this site")
                .replace("{window}", window_label or "")).strip()


def prompt(question, snap, window_label, history=(), about="not described",
           system=DEFAULT_SYSTEM, guide=True, guide_text=None):
    """`guide_text` replaces the whole-window GUIDE for a question about one
    panel or one session: the snapshot is laid out differently, and saying so
    is what keeps the model from looking for panels that are not there."""
    turns = ""
    for h in list(history)[-3:]:
        q, a = (h.get("q") or "").strip(), (h.get("a") or "").strip()
        if q and a:
            turns += "\nEarlier question: %s\nYour earlier answer: %s\n" % (q[:600], a[:1500])
    head = system + ("\n\n" + (guide_text or GUIDE) if guide else "")
    return ("%s\n\nDeployment: %s.\nWindow: %s.%s\n\nQuestion: %s\n\n<snapshot>\n%s\n</snapshot>\n"
            % (head, about, window_label, turns, question.strip(), snap))


def argv_for(cfg, workdir, answer_file):
    """The command to run, from `ask.command` or the `ask.agent` preset."""
    cmd = cfg.get("ask.command") or PRESETS.get(cfg.get("ask.agent", "codex"))
    if not cmd:
        raise ValueError("ask.agent %r is not a preset and ask.command is not set"
                         % cfg.get("ask.agent"))
    if isinstance(cmd, str):
        cmd = shlex.split(cmd)
    model = cfg.get("ask.model") or ""
    out = []
    for a in cmd:
        if a == "{model_flag}":
            out += ["-m", model] if model else []
            continue
        out.append(a.replace("{workdir}", workdir).replace("{answer_file}", answer_file)
                    .replace("{model}", model))
    return out


def describe(cfg):
    """What the page shows about the agent, and whether it can run here."""
    try:
        argv = argv_for(cfg, "<workdir>", "<answer>")
        exe = shutil.which(argv[0]) if argv else None
        err = None if exe else "%s was not found on the service's PATH" % (argv[0] if argv else "?")
    except ValueError as e:
        argv, exe, err = [], None, str(e)
    return {"enabled": bool(cfg.get("ask.enabled", True)),
            "agent": cfg.get("ask.agent", "codex") if not cfg.get("ask.command") else "custom",
            "model": cfg.get("ask.model") or None,
            "command": " ".join(argv[:3]) + (" ..." if len(argv) > 3 else ""),
            "available": bool(exe) and not err, "error": err,
            "system_prompt": ("file" if cfg.get("ask.system_prompt_file")
                              else "config" if cfg.get("ask.system_prompt") else "built-in"),
            "timeout_s": int(cfg.get("ask.timeout_s", 240))}


def _progress_of(line):
    """A short note from one line of the agent's --json event stream."""
    try:
        e = json.loads(line)
    except ValueError:
        return None, None
    item = e.get("item") if isinstance(e.get("item"), dict) else {}
    kind = item.get("type")
    if e.get("type") == "item.completed" and kind == "agent_message":
        return "writing the answer", item.get("text")
    if kind == "reasoning":
        return "thinking", None
    if kind == "command_execution":
        return "tried to run a command", None
    if e.get("type") == "turn.failed":
        return "failed: %s" % ((e.get("error") or {}).get("message") or "unknown"), None
    return None, None


class Asker:
    """Runs questions, `max_concurrent` at a time, and keeps the last few."""

    KEEP = 50

    def __init__(self, cfg):
        self.cfg = cfg
        self.jobs = collections.OrderedDict()
        self.lock = threading.Lock()
        self.slots = threading.BoundedSemaphore(max(1, int(cfg.get("ask.max_concurrent", 1))))

    def submit(self, question, panels, live, window_label, history=(), extra=None,
               scope=None):
        """`scope`, for a question about one panel or one session: `about`
        (what the page shows it as), `guide` (replaces GUIDE), and the
        snapshot's `steps` and `keep`."""
        scope = scope or {}
        info = describe(self.cfg)
        if not info["enabled"]:
            raise ValueError("chat is turned off (ask.enabled)")
        if not info["available"]:
            raise ValueError(info["error"] or "the agent cannot run here")
        question = (question or "").strip()
        if not question:
            raise ValueError("ask a question")
        system_text(self.cfg)            # an unreadable prompt file fails here
        snap = snapshot(panels, live, int(self.cfg.get("ask.context_kb", 120)),
                        bool(self.cfg.get("ask.include_commands", False)), extra,
                        scope.get("steps") or STEPS, scope.get("keep") or ())
        job = {"id": uuid.uuid4().hex[:12], "state": "queued", "question": question[:2000],
               "window": window_label, "about": scope.get("about"),
               "agent": info["agent"], "model": info["model"],
               "submitted": time.time(), "started": None, "finished": None,
               "progress": "waiting for a free agent", "answer": None, "error": None,
               "snapshot_kb": round(len(snap) / 1024, 1)}
        with self.lock:
            self.jobs[job["id"]] = job
            while len(self.jobs) > self.KEEP:
                self.jobs.popitem(last=False)
        about = deployment(self.cfg, live)
        text = prompt(question, snap, window_label, history, about,
                      system_text(self.cfg, about, window_label),
                      bool(self.cfg.get("ask.snapshot_guide", True)), scope.get("guide"))
        threading.Thread(target=self._run, args=(job, text), daemon=True,
                         name="ask-%s" % job["id"]).start()
        return self.view(job["id"])

    def view(self, jid):
        with self.lock:
            j = self.jobs.get(jid)
            if j is None:
                return None
            out = dict(j)
        end = out["finished"] or time.time()
        out["elapsed_s"] = round(end - (out["started"] or out["submitted"]), 1)
        return out

    def _set(self, job, **kv):
        with self.lock:
            job.update(kv)

    def _run(self, job, text):
        with self.slots:
            self._set(job, state="running", started=time.time(), progress="starting the agent")
            base = os.path.join(self.cfg.get("cache.dir") or tempfile.gettempdir(), "ask")
            os.makedirs(base, mode=0o700, exist_ok=True)
            work = tempfile.mkdtemp(prefix="q-", dir=base)
            answer_file = os.path.join(work, "answer.md")
            timeout = int(self.cfg.get("ask.timeout_s", 240))
            try:
                argv = argv_for(self.cfg, work, answer_file)
                uses_file = any("{answer_file}" in str(a) for a in
                                (self.cfg.get("ask.command") or
                                 PRESETS.get(self.cfg.get("ask.agent", "codex")) or []))
                # Its own session, so a timeout can stop the agent and anything
                # it started, not only the first process.
                proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, cwd=work, text=True,
                                        start_new_session=True)
                killer = threading.Timer(timeout, lambda: _kill(proc))
                killer.start()
                last, out_lines = None, []
                try:
                    proc.stdin.write(text)
                    proc.stdin.close()
                    for line in proc.stdout:
                        out_lines.append(line)
                        note, msg = _progress_of(line)
                        if msg:
                            last = msg
                        if note:
                            self._set(job, progress=note)
                    err = proc.stderr.read()
                    code = proc.wait()
                finally:
                    killer.cancel()
                    for fh in (proc.stdout, proc.stderr):
                        fh.close()
                answer = None
                if uses_file and os.path.exists(answer_file):
                    with open(answer_file, encoding="utf-8", errors="replace") as fh:
                        answer = fh.read().strip()
                if not answer:
                    answer = last or ("" if uses_file else "".join(out_lines).strip())
                if code != 0 and not answer:
                    timed_out = code in (-signal.SIGKILL, -signal.SIGTERM)
                    raise RuntimeError("the agent timed out after %d s" % timeout if timed_out
                                       else "the agent exited %d: %s" % (code, (err or "").strip()[-400:]))
                self._set(job, state="done", answer=redact.redact(answer) if answer else "(no answer)",
                          progress=None, finished=time.time())
            except Exception as e:
                self._set(job, state="error", error=str(e), progress=None, finished=time.time())
            finally:
                shutil.rmtree(work, ignore_errors=True)


def _kill(proc):
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        time.sleep(2)
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
