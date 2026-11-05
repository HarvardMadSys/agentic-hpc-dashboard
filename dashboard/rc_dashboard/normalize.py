"""Turn a raw collector record into the shape the reducers consume.

The contract is SCHEMA_VERSION 8.  A thin shim keeps the pre-v4 capture usable as
an offline test input, but the shim's job is to make older records *honest*, not
to pretend they are current: a field the old collector could not measure must
arrive as None, never as 0.

Schema 6 removed the `tcp` and `accept` events; a closed connection now arrives
as the `conns` block on the owning process's `exit`/`truncated` record. Nothing
here reads either event, so the gates below are all `sv >= 5` / `sv < 4` and a v6
record needs no shim -- but a capture that mixes 5 and 6 will have connection
data in two different shapes, which `schema_versions` is what makes visible.

Schema 7 adds `net_endpoints` (bytes per endpoint) to `exit`/`truncated`, a
`netio` event (the same per tick), `proto` on `conn`, and `collector_pid` in the
envelope (the collector's own pid, for dropping its own process tree), and drops
the envelope's constant `source`/`collector`, `cwd_source` and `env_flags`. Nothing here reads
any of them. It also renames `comm` to `command`, whole rather than cut at 15
characters; `_command` below is the one key readers use, taking whichever the
record has. It also widens `net_tx_bytes` and
`net_rx_bytes` to include IPv6 UDP, which the v6 collector did not count: across
a 6/7 boundary those totals are not like for like, and `schema_versions` again is
what shows the boundary is there.

Schema 8 renames nothing this module reads, but changes what a session is (it
also masks credentials and agent prompts in `args`). Through 7, every process
whose own argv the classifier tagged was its own agent root, so Claude Code's shell
for each Bash tool call, its Grep re-exec, a Codex sandbox and a `--version` probe
each had `is_agent` true, `depth` 0, and its own `agent_pid`, `session_key` and
`session_uuid`: one session per tool call. From 8, inside an agent tree only an
agent runtime is a root (the collector's `agent_role()`). Every other tagged process
carries its parent's tree, at `depth` + 1, and its tree's `agent_type`, so its
`actor3` can change too. Outside any tree a tagged process is still its own root.
`tree_root_pid` and `actor` do not change. Nothing here branches on the version: a
v8 key is already a session, and a capture that spans 7 and 8 mixes the two grains,
which `schema_versions` makes visible.

Three rules this module exists to enforce:

1. `actor3` is derived, never approximated.  `r.get("actor3") or r.get("actor")`
   yields the BINARY actor on an older record, which silently empties the
   `human-vscode` class -- a class-accounting bug in a three-class dashboard. It
   also swallows the deliberate explicit `actor3: null` on an untracked-pid
   `conn` record.  Unlabelled becomes its own class, never `human`.

2. Null is not zero.  `null_if_off` (ebpf_trace.py:1075) emits None for every
   field whose BPF block was dropped, but the pre-v4 collector wrote 0 in some of
   those slots.  A 0 there means "not measured", so it is gated back to None and
   counted, rather than averaged in as a real zero.

3. `sandbox` and `approval_mode` are separate axes.  Schema 5 carries both; for
   an older record they are simply unknown.  `sandbox` means NAMESPACE ISOLATION
   (the collector's /proc/<pid>/ns/* against pid 1's) and has FOUR states,
   because "could not read" must not be reported as "unsandboxed".  The ebpf tier
   runs as root, so that state is not ptrace-gating: across 2026-09-21..25 every
   one of 163 such records lived under 6 ms -- the process had exited before the
   read.  It is still not "not isolated", and it is labelled "exited before read".

4. Approval is a property of the agent SESSION, read off the argv of the
   process that runs it.  Only a process whose role (roles.py) is `agent` gets a
   verdict; a tool shell, a probe or an IDE helper is `na`, and an untagged child
   is `unknown` -- its session's verdict lives on the session root's record.
   Labelling every tagged process "supervised" made 9,341 of 9,370 "agent trees"
   on rtx6000a supervised `codex --version` polls.

5. Where a session is driven from -- its launch SURFACE -- is read off that same
   process's argv, and is `unknown` where the argv does not say: a Codex
   app-server daemon serves whichever client attaches to it.
"""
import functools
import os
import re
import sys
from datetime import datetime

from .roles import role_of, subcommand

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SESSION_SCOPE_RE = re.compile(r"session-([0-9]+)\.scope")
TS_FORMAT = "%Y-%m-%d %H:%M:%S"


def epoch_of(rec):
    """Epoch seconds for a record, or None.  Never guesses."""
    e = rec.get("ts_epoch")
    if isinstance(e, (int, float)):
        return float(e)
    ts = rec.get("ts") or rec.get("timestamp")
    if not ts:
        return None
    try:
        return datetime.strptime(str(ts)[:19], TS_FORMAT).timestamp()
    except ValueError:
        return None

# Fields the v4+ collector emits as null-when-absent but pre-v4 wrote as 0.
# On a pre-v4 record these are forced to None and counted in excluded_pre_v4.
NULLABLE_V4_ONLY = (
    "threads", "cmaj_flt", "child_cpu_s", "sched_wait_s", "blkio_wait_s",
    "swapin_wait_s", "freepages_wait_s", "dstate_wait_s", "dstate_episodes",
    "dstate_max_s", "net_tx_bytes", "net_rx_bytes", "net_calls",
    "min_flt", "nvcsw", "nivcsw", "pgrp", "sid",
)
# Never let a reducer see these: permanent null placeholders in v4+.
ALWAYS_NULL = ("samples", "state_last")

# The collector observing its own instrumentation. node_snapshot.py:1162-1165
# shells out to mpstat/iostat/sar/nfsiostat every tick and queries nvidia-smi,
# and ebpfm.sh drives it -- so without this the dashboard reports its own probes
# as fleet activity, and the Risk panel accuses the node tier of running
# `nvidia-smi` on a login node. The repo already excludes the monitoring
# operator globally (`drop_global`) for exactly this reason; this is the same
# rule applied to the instrument rather than its owner.
#
# Every drop is COUNTED and reported, never silent: an exclusion you cannot see
# is indistinguishable from data that was never there.
# Matched against `args` only -- not comm+args, which doubles the token and
# breaks a leading anchor.
#
# These are the collector's EXACT invocations, copied from
# collector/node_snapshot.py:1162-1176. Matching the precise flag sets
# rather than a loose `nvidia-smi --query-` matters: a user legitimately running
# `nvidia-smi --query-gpu=name` must still be counted, and only the snapshot's
# own verbatim calls excluded. Nobody types these by hand.
SELF_OBSERVATION = re.compile(
    r"node_snapshot\.py"
    r"|ebpfm\.sh\b"
    r"|nvidia-smi\s+--query-gpu=index,name,temperature\.gpu"
    r"|nvidia-smi\s+--query-compute-apps=gpu_uuid,pid,process_name"
    r"|nvidia-smi\s+pmon\s+-c\s+1\s*$"
    r"|\bmpstat\s+-P\s+ALL\s+1\s+1\s*$"
    r"|\biostat\s+-xz\s+1\s+2\s*$"
    r"|\bsar\s+-n\s+DEV\s+1\s+2\s*$"
    r"|\bnfsiostat\s+5\s+2\s*$"
    r"|\bvmstat\s+1\s+1\s*$"
)

# Records that carry a process's identity block.
IDENTITY_EVENTS = ("exit", "truncated", "residency", "conn", "netio", "submit")

SANDBOX_STATES = ("sandboxed", "unsandboxed", "denied", "unknown")
# The keys are what /api/events and /api/export filter on, so a key keeps its
# meaning and only its label moves: `supervised` is shown as "no bypass flag at
# launch", because that is all argv can say -- ~/.claude/settings.json, a
# `--settings` file, codex's config.toml and a mid-session mode switch are all
# invisible to it.
#   bypassed      a bypass flag at launch
#   auto          `--permission-mode auto`: approvals made by a classifier, not
#                 by a human and not skipped -- neither of the other two
#   supervised    an agent launched with no bypass flag
#   unobservable  `codex app-server`: the policy arrives per thread over JSON-RPC
#   na            tagged, but not an agent session (tool shell, probe, IDE, ...)
#                 or not an agent at all
#   unknown       an agent-tree record whose session verdict is on its root
APPROVAL_STATES = ("bypassed", "auto", "supervised", "unobservable", "na", "unknown")
# Where an agent session is driven from, in two groups. CLI:
#   terminal   a person at the agent's own interactive UI, in a terminal, tmux,
#              an IDE's terminal, or a `claude daemon` background session
#   script     a program drives it: `claude -p`, the Agent SDK's stream-json,
#              `codex exec`, an MCP server
# and app:
#   desktop    the Claude desktop app's Code tab (`ccd-cli`) or the Codex app,
#              each over SSH
#   extension  an IDE extension's own bundled binary
#   web        Remote Control, from a browser or a phone
#   unknown    the argv does not say: a Codex app-server daemon serves whichever
#              client attaches to it, and one on stdio was started by a program
#              whose own record is not there
SURFACES = ("terminal", "script", "desktop", "extension", "web", "unknown")


# ------------------------------------------------------------ optional repo libs
def _load_helpers():
    """agent_lib / a purpose taxonomy, each optional with a working fallback.

    `purpose_lib` is the declared source of truth but imports matplotlib at module
    level, and it exports `classify_comm` -- NOT `purpose_of`, which is why the
    retired builder's probe for `purpose_of` never matched it and the taxonomy
    silently fell through to deep_traj_lib's 12 purposes (no `guard`, no `shell`).
    Probe the real name first.
    """
    sys.path.insert(0, os.path.join(REPO, "analyze"))
    eff = actor3 = autonomous = None
    shell_comms = frozenset()
    try:
        from agent_lib import (effective_comm as eff, actor3 as actor3,   # noqa: F811
                               is_autonomous as autonomous, SHELL_COMMS as shell_comms)
    except Exception:
        pass
    purpose_of, purpose_src, purposes = None, "builtin", None
    for mod, attr in (("purpose_lib", "classify_comm"), ("deep_traj_lib", "purpose_of")):
        try:
            m = __import__(mod)
            fn = getattr(m, attr, None)
            if fn:
                purpose_of, purpose_src = fn, mod
                purposes = getattr(m, "PURPOSES", None)
                break
        except Exception:
            continue
    if eff is None:
        eff = lambda comm, args: comm                                    # noqa: E731
    if actor3 is None:
        actor3 = _fallback_actor3
    # `autonomous` is ALWAYS the approval reading, whatever agent_lib offers: the
    # collector's own `is_autonomous` lacks `--permission-mode bypassPermissions`,
    # the only bypass on rtx6000a, so `"autonomous": true` occurs 0 times in 28
    # days of records that carry `approval_mode: "bypassed"` 30k times.
    autonomous = lambda a: bypass_flag_of(a) is not None                  # noqa: E731
    if purpose_of is None:
        purpose_of = lambda c: _BUILTIN_PURPOSE.get(c or "", "other")    # noqa: E731
    return eff, actor3, autonomous, shell_comms, purpose_of, purpose_src, purposes


_REAL_AGENTS = frozenset({"claude_code", "codex", "copilot"})

# The IDE class: an editor a person drives, whatever AI features it carries.
# The collector tags the VS Code, Cursor and Windsurf remote-server trees by
# agent type, but it counts Cursor and Windsurf as `agent`, and it calls the VS
# Code class `human-vscode`. Both are settled here, once, so every reader sees
# one class under one name. An agent CLI started in an IDE's terminal (Claude
# Code, Codex) is tagged by its own agent type, so it stays `agent`.
IDE = "ide"
IDE_AGENT_TYPES = frozenset({"vscode", "cursor", "windsurf"})
LEGACY_CLASS = {"human-vscode": IDE}
# Other editors' remote servers, which the collector does not tag. Recognised
# only by the process's OWN executable path -- a person typing
# `ls ~/.zed_server` is not an IDE -- so their child processes stay `human`.
_OTHER_IDE = re.compile(
    r"^\S*?(?:/\.(?:vscode|vscodium|cursor|windsurf|positron|trae|kiro)-server/"
    r"|/remote-dev-server(?:\.sh)?(?:\s|$)|/JetBrains/RemoteDev/|/\.zed_server/|/code-server/)")


# Per-process waits, each bounded by the process's own lifetime.
WAIT_FIELDS = ("blkio_wait_s", "swapin_wait_s", "freepages_wait_s", "dstate_wait_s",
               "sched_wait_s")
WAIT_SLACK_S = 1.0               # clock granularity between the two readings
MAX_WAIT_NO_DUR_S = 7 * 86400    # no duration: nothing waits longer than a week


def canonical_class(c):
    """A class name as this dashboard spells it (`human-vscode` is `ide`)."""
    return LEGACY_CLASS.get(c, c)


def ide_class(a3, agent_type, args):
    """`a3` with every IDE process moved into the IDE class."""
    if agent_type in IDE_AGENT_TYPES:
        return IDE
    a3 = canonical_class(a3)
    if a3 == "human" and args and _OTHER_IDE.search(args):
        return IDE
    return a3


# The IDEs the collector counts in its `agent` totals rather than as a class.
_AGENT_COUNTED_IDES = IDE_AGENT_TYPES - {"vscode"}
_TICK_SUMS = ("n_procs", "threads", "rss_mb", "d_state")


def class_totals(by_actor3, by_agent_type):
    """A residency tick's per-class totals, under this dashboard's classes.

    `human-vscode` becomes `ide`, and the Cursor and Windsurf processes the
    collector folded into `agent` are moved across using its per-agent-type
    breakdown, for every count that adds up. Users do not -- one person can run
    both -- so the agent user count is left as the upper bound it already was.
    """
    out = {}
    for cls, d in (by_actor3 or {}).items():
        tgt = out.setdefault(canonical_class(cls), {})
        for k, v in (d or {}).items():
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                tgt[k] = tgt.get(k, 0) + v
            else:
                tgt.setdefault(k, v)
    for at in _AGENT_COUNTED_IDES & set(by_agent_type or {}):
        d = by_agent_type[at] or {}
        agent, ide = out.setdefault("agent", {}), out.setdefault(IDE, {})
        for k in _TICK_SUMS + ("users",):
            v = d.get(k)
            if not isinstance(v, (int, float)) or not v:
                continue
            ide[k] = (ide.get(k) or 0) + v
            if k != "users":
                agent[k] = max(0, (agent.get(k) or 0) - v)
    return out


def _fallback_actor3(actor, agent_type):
    """Mirror of agent_lib.actor3 -- must NOT degrade to the binary `actor`."""
    if actor == "agent":
        if agent_type in _REAL_AGENTS or agent_type == "bwrap":
            return "agent"
        if agent_type in IDE_AGENT_TYPES:
            return IDE
        return "human"
    return actor


# Launch flags, matched as whole argv tokens (`--x v` or `--x=v`) and only ever
# against the argv of a process whose role is `agent`. A substring anywhere in
# any argv made a tool call that merely greps for `danger-full-access` a bypass.
_TOK = r"(?:^|\s)"
_END = r"(?=\s|$)"
_BYPASS = re.compile(
    _TOK + r"(--dangerously-skip-permissions)" + _END
    + r"|" + _TOK + r"(--dangerously-bypass-approvals-and-sandbox)" + _END
    + r"|" + _TOK + r"(--yolo)" + _END
    + r"|" + _TOK + r"(--permission-mode[ =]bypassPermissions)" + _END
    + r"|" + _TOK + r"((?:--ask-for-approval|-a)[ =]never)" + _END
    + r"|(approval_policy\W{0,3}=\W{0,3}never)"
    + r"|" + _TOK + r"((?:--sandbox|-s)[ =]danger-full-access)" + _END
    + r"|(sandbox_mode\W{0,3}=\W{0,3}danger-full-access)", re.I)
_PERMISSION_MODE = re.compile(_TOK + r"--permission-mode[ =]([A-Za-z]+)" + _END)
_MAY_BYPASS = re.compile(_TOK + r"--allow-dangerously-skip-permissions" + _END)
_APP_SERVER = re.compile(_TOK + r"app-server" + _END)
# What an older record's argv says about a sandbox it ASKED for. Intent is not
# outcome: on rtx6000a all 612 `codex-linux-sandbox` runs exited 1 with no
# payload, so "asked for a sandbox" must never be read as "was isolated".
_SANDBOX_INTENT = re.compile(
    r"codex-linux-sandbox|CURSOR_SANDBOX|--dangerously-bypass-approvals-and-sandbox"
    r"|" + _TOK + r"(?:--sandbox|-s)[ =](?:read-only|workspace-write|danger-full-access)"
    + _END + r"|sandbox_mode\W{0,3}=", re.I)


@functools.lru_cache(maxsize=16384)
def bypass_flag_of(args):
    """The bypass flag on an agent's command line, as written, or None.

    Cached: an agent's own command line recurs on every process it spawns that
    inherits it, and this is the most expensive pattern the normaliser runs."""
    m = _BYPASS.search(args or "")
    if m is None:
        return None
    return next(g for g in m.groups() if g).strip()

_BUILTIN_PURPOSE = {}
for _p, _cs in {
    "poll": "ps pgrep pidof top free uptime nvidia-smi date hostname id whoami printenv nproc w who tput",
    "slurm_mon": "squeue sacct sstat sinfo scontrol sprio watch",
    "slurm_sub": "sbatch salloc srun scancel",
    "build": "make cmake ninja gcc g++ cc1plus nvcc ld cargo",
    "git": "git git-remote-https gitstatusd gh",
    "data": "rsync wget curl tar gzip cp mv scp zstd",
    "compute": "python python3 R Rscript julia matlab jupyter",
    "runtime": "node claude codex cursor bwrap electron esbuild",
    "editor": "vim nvim emacs nano code",
    "filesearch": ("grep rg find ls cat sed awk wc sort jq base64 head tail stat "
                   "mkdir rm xargs cut tr getopt getconf infocmp lsb_release ssh uname"),
}.items():
    for _c in _cs.split():
        _BUILTIN_PURPOSE[_c] = _p


def schema_of(rec):
    """Declared schema version; absent means the pre-v4 tracer or the poller."""
    sv = rec.get("schema_version")
    if isinstance(sv, int):
        return sv
    return 3


def session_key_of(rec):
    """(key_type, key_value), exactly as analyze/extract_trajectories.py:278-294.

    First key that applies wins.  `usr` is the last resort and is deliberately
    NOT a trajectory -- it means "everything that user did today" -- so the
    trajectory reducer counts it but never ranks it.
    """
    sk = rec.get("session_key")
    if sk:
        return "apid", sk.split(":", 1)[-1]
    ap = rec.get("agent_pid")
    if ap is not None:
        return "apid", str(ap)
    m = SESSION_SCOPE_RE.search(rec.get("cgroup") or "")
    if m:
        return "sess", m.group(1)
    tty = rec.get("tty")
    if tty and tty not in ("?", "-"):
        return "tty", "%s@%s" % (rec.get("user") or "?", tty)
    return "usr", rec.get("user") or "?"


def sandbox_of(rec, sv, role=None):
    """(state, source).  Four states -- `denied` is NOT `unsandboxed`.

    Schema 5 decides this in the collector by comparing /proc/<pid>/ns/* against
    pid 1's: it is NAMESPACE ISOLATION and nothing else.  Seccomp and
    capabilities are deliberately NOT part of the verdict (they live in
    sandbox_detail) -- systemd's SystemCallFilter= puts a filter on ordinary
    daemons -- and Landlock has no /proc exposure at all, so a Landlock- or
    seccomp-only sandbox reads as not isolated, while a container, an apptainer
    user namespace or a PrivateTmp= unit reads as isolated.

    For an older record we do not know.  Its argv can say a sandbox was ASKED
    for (or turned off), and that is kept as the source `argv-intent` of an
    `unknown` -- never as a verdict -- and only for the agent's own argv.
    """
    if sv >= 5 and "sandbox" in rec:
        v = rec.get("sandbox")
        src = rec.get("sandbox_src")
        if v in ("sandboxed", "unsandboxed"):
            return v, src
        return ("denied" if src == "denied" else "unknown"), src
    if role == "agent" and _SANDBOX_INTENT.search(rec.get("args") or ""):
        return "unknown", "argv-intent"
    return "unknown", None


def approval_of(rec, role, a3=None):
    """(state, source, detail) -- see APPROVAL_STATES.

    Only the session's own process (role `agent`) carries a verdict.  The
    collector's `approval_mode` is taken as well as this module's reading: it
    classified the UNCAPPED argv, and the emitted one may be cut.  `detail` is
    the launch mode as written, so the panel can show what "no bypass flag"
    covers (`permission-mode=default`, `=acceptEdits`, nothing at all ...).
    """
    # An IDE's own processes are never a session, even where roles.py calls one
    # `agent` (a Cursor or Windsurf tree, which the collector counts as an agent
    # but the dashboard files under `ide`).
    if role == "agent" and a3 not in ("ide", "human"):
        args = rec.get("args") or ""
        flag = bypass_flag_of(args)
        pm = _PERMISSION_MODE.search(args)
        app = rec.get("agent_type") == "codex" and _APP_SERVER.search(args) is not None
        parts = []
        if flag:
            parts.append(flag.replace(" ", "=").replace("\"", "").replace("'", ""))
        elif pm:
            parts.append("permission-mode=" + pm.group(1))
        if app:
            parts.append("app-server")
        if _MAY_BYPASS.search(args):
            parts.append("may-switch-to-bypass")
        detail = " ".join(parts) or "no launch flag"
        if flag or rec.get("approval_mode") == "bypassed":
            return "bypassed", rec.get("approval_src") or "argv", detail
        if pm and pm.group(1).lower() == "auto":
            return "auto", "argv", detail
        if app:
            return "unobservable", None, detail
        return "supervised", "argv", detail
    if role is not None:
        return "na", None, None
    if a3 in ("human", "ide"):
        return "na", None, None
    return "unknown", None, None


# A surface's evidence, all of it the agent's own record. A path names the
# program a client installs and runs: the Claude desktop app's `ccd-cli`, an IDE
# extension's bundled binary. But where a binary is installed does not say who
# runs it -- two users run the VS Code extension's `codex exec` from a terminal --
# so a mode (`exec`, `-p`, the interactive UI) decides first, and the extension
# is the one running its binary only in its own shapes: Claude's stream-json,
# Codex's app-server, or no mode at all and no terminal. The Codex app starts its
# remote app-server through the login shell's `codex`, so only its flags mark it:
# a unix socket, code mode.
_CCD_CLI = "/.claude/remote/ccd-cli/"
_EXTENSION = re.compile(r"/\.([a-z]+)(?:-server(?:-insiders)?)?/extensions/"
                        r"(?:anthropic\.claude-code|openai\.chatgpt)-")
_IDE_NAMES = {"vscode": "VS Code", "cursor": "Cursor", "windsurf": "Windsurf",
              "positron": "Positron"}
_SDK_URL = re.compile(_TOK + r"--sdk-url(?=[\s=]|$)")
_PRINT = re.compile(_TOK + r"(?:-p|--print)" + _END)
_STREAM_JSON = re.compile(_TOK + r"--(?:input|output)-format[ =]stream-json" + _END)
_AGENT_SDK = "/claude-agent-sdk"
_REMOTE_CONTROL = re.compile(_TOK + r"--remote-control" + _END)
_LISTEN_UNIX = re.compile(_TOK + r"--listen[ =]unix://")
_CODE_MODE_HOST = re.compile(r"features\.code_mode_host=true")
_EXPERIMENTAL_JSON = re.compile(_TOK + r"--experimental-json" + _END)


def surface_of(rec, role, a3=None):
    """(surface, detail) -- see SURFACES -- for an agent session's own process,
    or for a process hosting sessions whose argv names their client.

    Only those say, as for approval: a tool shell, a child, a client and an
    IDE's own process get (None, None). `detail` names the client, or the mode.
    """
    if a3 in ("ide", "human"):
        return None, None
    command = rec.get("_command") or rec.get("command") or rec.get("comm")
    if role == "infra":
        return _host_surface(rec.get("agent_type"), command, rec.get("args") or "")
    if role != "agent":
        return None, None
    return _surface(rec.get("agent_type"), command, rec.get("args") or "",
                    rec.get("tty") not in (None, "", "?", "-"))


@functools.lru_cache(maxsize=1024)
def _host_surface(agent_type, command, args):
    """A host is not a session, but its sessions are driven from where it says:
    a trajectory row keyed on the desktop app's server held thousands of its
    processes on rtx6000a."""
    if "/.claude/remote/srv/" in args:
        return "desktop", "Claude desktop app"
    sub = subcommand(command, args)
    if agent_type == "codex":
        if sub == "remote-control":
            return "web", "Codex Remote Control"
        if sub == "app-server":          # `app-server daemon`: the shared daemon's keeper
            return "unknown", "shared app server"
        return None, None
    if sub in ("rc", "remote-control"):
        return "web", "Claude Remote Control"
    if sub in ("daemon", "bg-pty-host"):
        return "terminal", "background session"
    return None, None


@functools.lru_cache(maxsize=4096)
def _surface(agent_type, command, args, tty):
    """Cached: an agent's own argv recurs on every tick its session sends."""
    m = _EXTENSION.search(args)
    ide = _IDE_NAMES.get(m.group(1), m.group(1)) if m else None
    sub = subcommand(command, args)
    if agent_type == "codex":
        if sub == "exec":
            return "script", "Codex SDK" if _EXPERIMENTAL_JSON.search(args) else "codex exec"
        if sub in ("mcp-server", "mcp"):
            return "script", "MCP server"
        if sub == "app-server":
            if ide:
                return "extension", ide
            if _REMOTE_CONTROL.search(args):
                return "web", "Codex Remote Control"
            if not _LISTEN_UNIX.search(args):
                return "unknown", "app server"
            if _CODE_MODE_HOST.search(args):
                return "desktop", "Codex app"
            return "unknown", "shared app server"
    elif _CCD_CLI in args:
        return "desktop", "Claude desktop app"
    elif _SDK_URL.search(args):
        return "web", "Claude Remote Control"
    elif sub == "mcp":
        return "script", "MCP server"
    elif _STREAM_JSON.search(args) or _AGENT_SDK in args:
        return ("extension", ide) if ide else ("script", "Agent SDK")
    elif _PRINT.search(args):
        return "script", "claude -p" if agent_type == "claude_code" else "-p"
    elif sub == "bg-spare":
        return "terminal", "background session"
    if ide and not tty:
        return "extension", ide
    return "terminal", "interactive"


class Normalizer:
    """Stateful because the exclusion filters and the counters are per-run."""

    def __init__(self, cfg):
        (self.effective_comm, self.actor3, self.is_autonomous, self.shell_comms,
         self.purpose_of, self.purpose_src, self.purposes) = _load_helpers()
        f = cfg.get("filters", {})
        self.drop_global = set(f.get("drop_global") or [])
        self.drop_agent = set(f.get("drop_agent") or [])
        self.unlabeled = f.get("unlabeled_class", "unlabeled")
        self.drop_self = f.get("drop_self_observation", True)
        self.stats = {
            "schema_versions": {}, "excluded_pre_v4": {}, "dropped_users": 0,
            "parse_errors": 0, "records": 0, "skew_dropped": 0,
            "dropped_self_observation": 0, "impossible_wait": {},
            "purpose_src": self.purpose_src,
        }
        self._selfobs = {}       # argv -> self-observation verdict; a cache

    def __call__(self, rec):
        return self.normalize(rec)

    def normalize(self, rec):
        """Returns the record with derived keys added, or None if filtered out.

        Derived keys are additive and prefixed `_`, so the raw record is still
        byte-faithful in memory, where the detectors need its argv as written.
        Credentials are masked on the way OUT, never here (`redact`).
        """
        sv = schema_of(rec)
        s = self.stats
        s["schema_versions"][sv] = s["schema_versions"].get(sv, 0) + 1
        s["records"] += 1

        user = rec.get("user")
        ev = rec.get("event")

        # class first: it gates the agent-only exclusion
        if "actor3" in rec:
            a3 = rec.get("actor3")           # v4+: authoritative, incl. explicit null
        else:
            a3 = self.actor3(rec.get("actor"), rec.get("agent_type"))
        a3 = ide_class(a3 or self.unlabeled, rec.get("agent_type"), rec.get("args"))
        if ev == "residency_totals":
            rec["_by_class"] = class_totals(rec.get("by_actor3"), rec.get("by_agent_type"))
            tcp = rec.get("tcp_open_ext_by_actor3")
            rec["_tcp_by_class"] = (None if tcp is None else
                                    {canonical_class(k): v for k, v in tcp.items()})

        if user and (user in self.drop_global
                     or (a3 == "agent" and user in self.drop_agent)):
            s["dropped_users"] += 1
            return None

        if self.drop_self and ev in ("exit", "truncated"):
            if self._self_observed(rec.get("args") or ""):
                s["dropped_self_observation"] += 1
                return None

        rec["_sv"] = sv
        rec["_a3"] = a3
        # v7 names the executable `command`, whole; v6 and older had `comm`, cut
        # at 15 characters. Readers use this and never either raw key.
        rec["_command"] = rec.get("command") or rec.get("comm")

        # What a classifier-tagged process actually is (see roles.py). Only a
        # tagged process gets one, so a missing `_role` means "not tagged".
        tagged = rec.get("is_agent")
        if tagged is None:          # pre-v4: the collector's own rule, agent_pid == pid
            tagged = rec.get("agent_pid") is not None and rec.get("agent_pid") == rec.get("pid")
        if tagged and ev in IDENTITY_EVENTS:
            rec["_role"] = role_of(rec.get("agent_type"), rec["_command"], rec.get("args"))

        if ev in ("exit", "truncated"):
            self._gate_nulls(rec, sv)
            self._gate_impossible_waits(rec)
            comm = rec["_command"] or ""
            args = rec.get("args") or ""
            ec = self.effective_comm(comm, args)
            rec["_eff"] = ec
            rec["_purpose"] = self.purpose_of(ec)
            rec["_shell_resolved"] = bool(comm in self.shell_comms and ec != comm)
            # pre-v4 emitted cpu_pct on a 0-duration process; recompute, never trust
            d = rec.get("duration_s")
            cpu = rec.get("cpu_s")
            rec["_cpu_pct"] = (100.0 * cpu / d) if (d and d >= 0.01 and cpu) else None

        if ev in ("exit", "truncated", "submit", "residency"):
            kt, kv = session_key_of(rec)
            rec["_kt"], rec["_kv"] = kt, kv
            rec["_sid"] = "%s|%s|%s|%s:%s" % (rec.get("host"), (rec.get("ts") or "")[:10],
                                              a3, kt, kv)
            sb, sbsrc = sandbox_of(rec, sv, rec.get("_role"))
            rec["_sandbox"], rec["_sandbox_src"] = sb, sbsrc
            rec["_sandbox_ancestry"] = rec.get("sandbox_ancestry")
        if ev in IDENTITY_EVENTS:
            # Every identity block, the tick records included: a running
            # session's root is often only ever seen in its own `conn` records
            # (a VS Code-launched Claude is not its residency tree's root).
            ap, apsrc, apdet = approval_of(rec, rec.get("_role"), a3)
            rec["_approval"], rec["_approval_src"] = ap, apsrc
            rec["_approval_detail"] = apdet
            rec["_autonomous"] = ap == "bypassed"
            rec["_surface"], rec["_surface_detail"] = surface_of(rec, rec.get("_role"), a3)

        # Time is resolved HERE, once, so no downstream consumer invents one.
        # `ts_epoch` is schema 5; an older record only has the local-naive `ts`
        # string, and a record with neither must stay None -- falling back to
        # "now" would stamp a week-old capture as live, which is precisely the
        # fabrication this rewrite exists to remove.
        rec["_ts_epoch"] = epoch_of(rec)
        return rec

    def _self_observed(self, args):
        """SELF_OBSERVATION, remembered per command line: ~90% of a login node's
        command lines are repeats, and this ran on every exit."""
        hit = self._selfobs.get(args)
        if hit is None:
            hit = SELF_OBSERVATION.search(args) is not None
            if len(self._selfobs) >= 50000:
                self._selfobs.clear()
            self._selfobs[args] = hit
        return hit

    def _gate_impossible_waits(self, rec):
        """A wait longer than the process existed was never measured.

        Seen for real on rtx6000a (kernel 6.17): about one block-I/O delay in
        150 is the host's UPTIME -- 3,393,948 s on a 1.3 s process -- the
        kernel adding `now - 0` for a wait whose start it never stamped. Three
        such records outweighed every real block-I/O wait of the day a million
        to one. So the value becomes None, like any unmeasured field, and the
        record is marked so the panels can say how many they set aside.
        """
        dur = rec.get("duration_s")
        limit = dur + WAIT_SLACK_S if isinstance(dur, (int, float)) else MAX_WAIT_NO_DUR_S
        bad = None
        for k in WAIT_FIELDS:
            v = rec.get(k)
            if isinstance(v, (int, float)) and v > limit:
                rec[k] = None
                bad = (bad or []) + [k]
                ex = self.stats["impossible_wait"]
                ex[k] = ex.get(k, 0) + 1
        if bad:
            rec["_impossible"] = bad

    def _gate_nulls(self, rec, sv):
        """A pre-v4 0 in a v4-only slot means 'block absent', not 'measured zero'."""
        if sv < 4:
            ex = self.stats["excluded_pre_v4"]
            for k in NULLABLE_V4_ONLY:
                if rec.get(k) is not None:
                    ex[k] = ex.get(k, 0) + 1
                rec[k] = None
        for k in ALWAYS_NULL:
            rec[k] = None
        io = rec.get("io")
        if isinstance(io, dict) and not io.get("scope"):
            # absent pre-v4; assuming "process" would overstate (vs leader accounting)
            io["scope"] = "unknown"
