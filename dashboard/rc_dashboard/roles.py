"""What a process the collector tagged as an agent actually is.

The collector's classifier (`collector/lib/agent_classify.py`) answers "which
agent's tree is this?" from argv, and until it read the program alone it matched
any path under `~/.claude/`, `~/.codex/` or `~/.vscode-server/`. That is right for
the tree, and wrong for the process: through schema 7 `resolve()` made every argv
match its own agent root (`is_agent`, `depth` 0, `agent_pid` = itself), so a
process that only *mentions* an agent path became the `agent_pid`, and so the
`session_key`, of everything under it.

Across two weeks of madsys-gpu1 (2026-09-20 .. 10-07) the tagged processes are
mostly not agents:

* every Claude Code Bash tool call, `zsh -c source ~/.claude/shell-snapshots/...`
* the native binary re-executing itself as `rg`, `ugrep` or `bfs` for Grep/Glob
* `codex-linux-sandbox` and `bwrap`, one per sandboxed Codex tool call
* `--version` and `auth status` probes, run by IDE extensions every few minutes
* Codex's self-update script (`mkdir`, `find`, `ln`, `mv` under `~/.codex/`)
* VS Code's server, its extension hosts and their helper scripts

The FAS login nodes (2026-10-20 .. 10-28) add the agent binary run as a helper of
its own, chosen by its first argument: Codex's sandboxed filesystem helper
(`codex --codex-run-as-fs-helper`, 15 945 processes from 63 users, each inside a
tool call's `codex-linux-sandbox`), and the Claude binary in VS Code's Claude
extension run as the Chrome MCP server (`claude --claude-in-chrome-mcp`, started
by the extension host itself).

So the role is decided here, from the process's own argv, and only `agent` is a
session. Roles, in the order they are tested:

  sandbox        the per-call confinement wrapper (codex-linux-sandbox, bwrap)
  ide            an IDE server process (VS Code / Cursor / Windsurf server, extension host)
  embedded-tool  the agent binary run under another argv[0] (Claude's rg/ugrep/bfs)
  probe          a one-shot invocation that is not a session (--version, auth, doctor)
  client         attaches to a session it does not run (claude attach, codex app-server proxy)
  infra          hosts sessions without being one (claude daemon, bg-pty-host, claude rc,
                 Claude Desktop's remote server)
  agent          an agent session runtime: the thing that talks to the model and runs tools
  tool-shell     the shell a Claude Code Bash tool call runs in
  helper         tagged only because its argv mentions an agent path, or the agent
                 binary run as a helper of its own (Codex's fs helper, Claude's
                 Chrome MCP server)

`tool-shell` and `codex-linux-sandbox` are started by their agent directly,
never through anything in between, so `spawned_by_agent` lets a caller take the
parent of one of them as the agent even when nothing else in the window says
what that parent is. `embedded-tool` is NOT: Claude's Grep tool runs it from the
agent, but the shell snapshot routes a Bash call's `grep` to the same binary, so
it also runs under a tool call's shell or a subshell that shell forked -- read as
three nested agents on madsys-gpu1 on 2026-10-10 before this was taken out.

This is a heuristic over argv, calibrated on the shapes above, and it is stated
as one: callers publish the role census so a misfiled shape is visible.
"""
import functools
import os
import re

ROLES = ("agent", "client", "infra", "probe", "embedded-tool", "sandbox", "ide",
         "tool-shell", "helper")

# The agent executables themselves. Claude's native binary is installed as its
# version (`~/.local/share/claude/versions/2.1.291`, `~/.claude/remote/ccd-cli/2.1.286`),
# so the kernel's comm for it is a version string.
_AGENT_EXES = frozenset({"claude", "codex", "cursor-agent", "copilot", "windsurf"})
_VERSION_EXE = re.compile(r"^\d+\.\d+\.\d+")
_SCRIPT_HOSTS = frozenset({"node", "bun", "deno"})
_SCRIPT = re.compile(r"claude-code/cli\.[cm]?js$|@anthropic-ai/claude-code"
                     r"|@openai/codex/bin/codex(?:\.[cm]?js)?$")

# argv[0] names the native Claude binary takes when it runs its bundled search
_EMBEDDED = frozenset({"rg", "ugrep", "bfs"})

# The kernel cuts comm at 15 characters, so schema <= 6 records carry
# `codex-linux-san` and `codex-code-mode`: match the prefix.
_SANDBOX_PREFIX = ("codex-linux-san", "bwrap")
_INFRA_PREFIX = ("codex-code-mode",)

_IDE_SERVER = re.compile(
    r"bootstrap-fork|server-main\.js|--type=extensionHost|--connection-token"
    r"|\.(?:vscode|cursor|windsurf)-server/cli/servers/[^ ]*/server/(?:node|bin/code-server)(?:\s|$)")
_IDE_EXE = re.compile(r"^code-[0-9a-f]{6,}")
_IDE_TYPES = frozenset({"vscode"})
# Cursor's and Windsurf's servers carry their IDE's own type, which their AI
# features' processes share (a CURSOR_SANDBOX wrapper, `cursor-agent`): for those
# two types the server's directory tells the IDE's processes apart. The collector
# tags an agent by its own type wherever it is installed, so Claude Code or Codex
# from one of these IDEs' extensions (`~/.cursor-server/extensions/
# anthropic.claude-code-*/...`) is that agent, as it is from VS Code's.
_IDE_DIR_TYPES = frozenset({"cursor", "windsurf"})
_IDE_DIRS = (".cursor-server", ".windsurf-server")

_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "fish"})
_TOOL_SHELL = re.compile(r"/\.claude/shell-snapshots/")

# The agent binary run as a helper of its own, which the binary picks from its
# first argument alone: Codex's `--codex-run-as-*` (fs-helper, apply-patch,
# process-setup), and the servers Claude's binary runs in place of a session --
# the Chrome extension's MCP server, its native host, and an eval's mock MCP
# server (2.1.309's entry point tests `process.argv[2]` for each).
_HELPER_FLAGS = frozenset({"--claude-in-chrome-mcp", "--chrome-native-host",
                           "--eval-mock-server"})
_HELPER_PREFIX = ("--codex-run-as-",)

_PROBE_FLAGS = frozenset({"--version", "-v", "-V", "--help", "-h", "--update"})
_PROBE_SUB = frozenset({"doctor", "update", "upgrade", "install", "auth", "config",
                        "completion", "setup-token", "migrate-installer", "plugin",
                        "login", "logout", "version", "help", "features", "debug"})
_PROBE_SUB_PREFIX = ("migrate-",)       # codex migrate-nfs-db, migrate-rollouts
# `codex queue --thread X --message M` posts into a thread the app-server holds and
# exits: 531 on the FAS login nodes in 2026-11-09 .. 16, every one counted as an agent
_CLIENT_SUB = frozenset({"attach", "agents", "queue"})
# `codex sandbox -C DIR -P agent -- CMD` runs one command under Codex's sandbox
_SANDBOX_SUB = frozenset({"sandbox"})
# `rc`/`remote-control` is the Remote Control server: it starts a
# `--print --sdk-url` worker per remote session and runs no tool calls itself
# (none in 7 days on madsys-gpu1, against 30 409 for its worker). The `--rc`
# FLAG is an interactive session with Remote Control on, and stays an agent.
_INFRA_SUB = frozenset({"daemon", "bg-pty-host", "rc", "remote-control"})

# Flags that take the next token as their value, so it is not mistaken for a
# subcommand (`codex -c features.code_mode_host=true app-server`).
_VALUE_FLAGS = frozenset({
    "-c", "--config", "-m", "--model", "--session-id", "-r", "--resume",
    "--output-format", "--input-format", "--effort", "--sdk-url", "--permission-mode",
    "--add-dir", "--json-path", "--log-file", "--origin", "--spawned-by", "-C", "--cd",
    "-s", "--sandbox", "-a", "--ask-for-approval", "-p", "--profile", "-i", "--image",
    "--agent", "--settings", "--mcp-config", "--append-system-prompt", "--system-prompt",
    "--allowedTools", "--disallowedTools", "--fallback-model", "--max-turns",
    "--bg-spare", "--bg-pty-host", "--socket", "--listen",
})


def _base(tok):
    b = os.path.basename(tok)
    for ext in (".js", ".mjs", ".cjs"):
        if b.endswith(ext):
            return b[:-len(ext)]
    return b


def _positionals(rest):
    """Non-flag tokens, skipping the values of the flags that take one."""
    out, skip = [], False
    for tok in rest:
        if skip:
            skip = False
            continue
        if tok.startswith("-"):
            skip = "=" not in tok and tok in _VALUE_FLAGS
            continue
        out.append(tok)
    return out


def _exe_index(command, toks):
    """Index in argv of the agent executable, or None if this is not one."""
    if command in _AGENT_EXES or _VERSION_EXE.match(command):
        return 0
    if command in _SCRIPT_HOSTS:
        for i, tok in enumerate(toks[1:], 1):
            if tok.startswith("-"):
                continue
            if _base(tok) in _AGENT_EXES or _SCRIPT.search(tok):
                return i
            return None             # the first script argument is something else
    return None


# A script the kernel ran through its #! line takes the script's name as its comm,
# and its argv starts with the interpreter: `/usr/bin/env node .../bin/codex exec`,
# `/bin/bash .../stub/claude -p`. A JavaScript host runs the agent's own launcher,
# whose subcommand follows the script. A shell or Python runs a wrapper or a test
# double named after the agent, never the session itself -- the session, if the
# script starts one, is its child: 2,658 such scripts were counted as agents on the
# FAS login nodes in 2026-11-09 .. 16, most of them one project's test suite.
_PYTHON = re.compile(r"^python(?:\d+(?:\.\d+)*)?$")
_SCRIPT_REACH = 4          # tokens an interpreter and its options take before the script


def _interpreter(toks):
    """The program a #! line runs: argv[0], or for `env`, the first word after its
    flags and assignments."""
    b = _base(toks[0]) if toks else ""
    if b != "env":
        return b
    for tok in toks[1:]:
        if tok.startswith("-") or "=" in tok:
            continue
        return _base(tok)
    return ""


def _agent_index(command, toks):
    """(index in argv of the agent executable or None, wrapped): `wrapped` when that
    executable is a script a shell or Python ran -- named after the agent, not it."""
    if command in _AGENT_EXES and len(toks) > 1:
        interp = _interpreter(toks)
        if interp in _SHELLS or interp in _SCRIPT_HOSTS or _PYTHON.match(interp):
            for i, tok in enumerate(toks[1:_SCRIPT_REACH + 1], 1):
                if tok == "-c":
                    break           # a command string, not a script
                if _base(tok) == command:
                    return i, interp not in _SCRIPT_HOSTS
    return _exe_index(command, toks), False


# Memoized: a pure function of three strings, asked for every tagged record a
# window reduces, and a session's records repeat their command lines. The
# distinct ones are few -- an agent root's argv per session -- so the memo is
# small in count (ROLE_MEMO_MAX x ROLE_MEMO_LEN characters, ~16 MB, at most)
# but keeps a long argv: a prompt on the command line recurs with its session.
# An argv past ROLE_MEMO_LEN is worked out each time rather than kept.
ROLE_MEMO_LEN = 4096
ROLE_MEMO_MAX = 4096


def role_of(agent_type, command, args):
    """One of ROLES for a process the collector tagged as an agent root."""
    if args is not None and len(args) > ROLE_MEMO_LEN:
        return _role_of(agent_type, command, args)
    return _role_memo(agent_type, command, args)


@functools.lru_cache(maxsize=ROLE_MEMO_MAX)
def _role_memo(agent_type, command, args):
    return _role_of(agent_type, command, args)


def _role_of(agent_type, command, args):
    command = command or ""
    toks = (args or "").split()
    argv0 = _base(toks[0]) if toks else ""

    if agent_type == "bwrap" or command.startswith(_SANDBOX_PREFIX) \
            or argv0 in ("bwrap", "codex-linux-sandbox"):
        return "sandbox"
    if agent_type in _IDE_TYPES or (agent_type in _IDE_DIR_TYPES
                                    and any(d in (args or "") for d in _IDE_DIRS)):
        return "ide" if (_IDE_EXE.match(command) or _IDE_SERVER.search(args or "")) \
            else "helper"
    if command.startswith(_INFRA_PREFIX):
        return "infra"
    if toks and "/.claude/remote/srv/" in toks[0]:
        # Claude Desktop's remote server: it launches `ccd-cli` sessions over SSH
        return "probe" if _PROBE_FLAGS.intersection(toks[1:]) else "infra"

    i, wrapped = _agent_index(command, toks)
    if wrapped:
        return "helper"
    if i is None:
        if command in _SHELLS and _TOOL_SHELL.search(args or ""):
            return "tool-shell"
        return "helper"
    if i == 0 and argv0 in _EMBEDDED:
        return "embedded-tool"
    rest = toks[i + 1:]
    if rest and (rest[0] in _HELPER_FLAGS or rest[0].startswith(_HELPER_PREFIX)):
        return "helper"
    if _PROBE_FLAGS.intersection(rest):
        return "probe"
    pos = _positionals(rest)
    sub = pos[0] if pos else None
    if sub == "app-server":
        nxt = pos[1] if len(pos) > 1 else None
        return {"proxy": "client", "daemon": "infra"}.get(nxt, "agent")
    if sub == "mcp":
        return "agent" if len(pos) > 1 and pos[1] == "serve" else "probe"
    if sub in _PROBE_SUB or (sub or "").startswith(_PROBE_SUB_PREFIX):
        return "probe"
    if sub in _CLIENT_SUB:
        return "client"
    if sub in _SANDBOX_SUB:
        return "sandbox"
    if sub in _INFRA_SUB:
        return "infra"
    return "agent"


# An agent's version, where the name or path of its program says it: Claude
# Code's native installs (`~/.local/share/claude/versions/2.1.291`,
# `~/.claude/remote/ccd-cli/2.1.295-8b2928c...`, a process named
# `2.1.295-8b2928c`) and the Codex app-server daemon
# (`.../releases/0.162.1-x86_64.../bin/codex`). A plain `claude` or `codex`, or
# Codex run through npm, names none, and gets none rather than a guess. Here
# and not in the live tier, which keeps it too: the cascades count with it,
# and an hour's fingerprint holds this module, not the live tier's.
_VERSION_NAME = re.compile(r"^v?(\d+\.\d+\.\d+)(?:[-+][0-9A-Za-z.]+)?$")
_VERSION_PATH = re.compile(r"/(?:versions|ccd-cli|releases)/v?(\d+\.\d+\.\d+)")


def agent_version(comm, args):
    m = _VERSION_NAME.match(comm or "")
    if m:
        return m.group(1)
    m = _VERSION_PATH.search(args or "")
    return m.group(1) if m else None


# Claude Code's sandbox runtime starts a Bash call's shell inside bwrap, in two
# stages: a shell behind a proxy prologue (`_srt_http=$!`), which runs the seccomp
# shim (`ARGV0=apply-seccomp /proc/self/fd/3`), which starts the call's shell
# (`bash -c exec 3<&- || exit 126; source ...`). Each stage's parent is the sandbox,
# not the agent, and exits before its child, so the index never holds it. Taken for
# the agent, it made each sandboxed call an implied agent -- 981 in one FAS tree on
# 2026-11-13. Without the link the call folds into its tree root, the agent.
_SANDBOX_RUNTIME = re.compile(r"\b_srt_(?:http|socks)=|\bSRT_ALLOW_NESTED_USERNS="
                              r"|\bARGV0=apply-seccomp\b|\s-c\s+exec 3<&- \|\| exit 126;")


def spawned_by_agent(role, command, args=None):
    """True for a process its agent starts directly: its parent IS the agent."""
    if role == "tool-shell":
        return not (args and _SANDBOX_RUNTIME.search(args))
    return role == "sandbox" and (command or "").startswith("codex-linux-san")


def js_launcher(command, args):
    """True for an agent's own JavaScript launcher, which only starts the agent's
    native binary: `node .../bin/codex`, or npm's `codex` run through its #! line as
    `/usr/bin/env node .../bin/codex` (comm `codex`)."""
    command = command or ""
    toks = (args or "").split()
    if command in _SCRIPT_HOSTS:
        return _exe_index(command, toks) is not None
    i, wrapped = _agent_index(command, toks)
    return bool(i) and not wrapped


def subcommand(command, args):
    """The agent executable's first positional argument (`exec`, `app-server`,
    `mcp-server`), or None: there is none, or this is not an agent executable.
    Read as `role_of` reads it, so a word of a prompt is not one."""
    toks = (args or "").split()
    # a script the kernel ran through its #! line: read past its interpreter, a
    # wrapper's too -- what it was asked for, whatever runs the session
    i, _wrapped = _agent_index(command or "", toks)
    if i is None:
        return None
    pos = _positionals(toks[i + 1:])
    return pos[0] if pos else None
