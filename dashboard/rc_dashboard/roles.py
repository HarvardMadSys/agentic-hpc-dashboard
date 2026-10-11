"""What a process the collector tagged as an agent actually is.

The collector's classifier (`collector/lib/agent_classify.py`) answers "which
agent's tree is this?" from argv, and it matches any path under `~/.claude/`,
`~/.codex/` or `~/.vscode-server/`. That is right for the tree, and wrong for
the process: `resolve()` makes every argv match its own agent root (`is_agent`,
`depth` 0, `agent_pid` = itself), so a process that only *mentions* an agent
path becomes the `agent_pid`, and so the `session_key`, of everything under it.

Across two weeks of madsys-gpu1 (2026-09-20 .. 10-07) the tagged processes are
mostly not agents:

* every Claude Code Bash tool call, `zsh -c source ~/.claude/shell-snapshots/...`
* the native binary re-executing itself as `rg`, `ugrep` or `bfs` for Grep/Glob
* `codex-linux-sandbox` and `bwrap`, one per sandboxed Codex tool call
* `--version` and `auth status` probes, run by IDE extensions every few minutes
* Codex's self-update script (`mkdir`, `find`, `ln`, `mv` under `~/.codex/`)
* VS Code's server, its extension hosts and their helper scripts

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
  helper         tagged only because its argv mentions an agent path

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

_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "fish"})
_TOOL_SHELL = re.compile(r"/\.claude/shell-snapshots/")

_PROBE_FLAGS = frozenset({"--version", "-v", "-V", "--help", "-h", "--update"})
_PROBE_SUB = frozenset({"doctor", "update", "upgrade", "install", "auth", "config",
                        "completion", "setup-token", "migrate-installer", "plugin",
                        "login", "logout", "version", "help", "features"})
_CLIENT_SUB = frozenset({"attach", "agents"})
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


def role_of(agent_type, command, args):
    """One of ROLES for a process the collector tagged as an agent root."""
    command = command or ""
    toks = (args or "").split()
    argv0 = _base(toks[0]) if toks else ""

    if agent_type == "bwrap" or command.startswith(_SANDBOX_PREFIX) \
            or argv0 in ("bwrap", "codex-linux-sandbox"):
        return "sandbox"
    if agent_type in _IDE_TYPES or ".cursor-server" in (args or "") \
            or ".windsurf-server" in (args or ""):
        return "ide" if (_IDE_EXE.match(command) or _IDE_SERVER.search(args or "")) \
            else "helper"
    if command.startswith(_INFRA_PREFIX):
        return "infra"
    if toks and "/.claude/remote/srv/" in toks[0]:
        # Claude Desktop's remote server: it launches `ccd-cli` sessions over SSH
        return "probe" if _PROBE_FLAGS.intersection(toks[1:]) else "infra"

    i = _exe_index(command, toks)
    if i is None:
        if command in _SHELLS and _TOOL_SHELL.search(args or ""):
            return "tool-shell"
        return "helper"
    if i == 0 and argv0 in _EMBEDDED:
        return "embedded-tool"
    rest = toks[i + 1:]
    if _PROBE_FLAGS.intersection(rest):
        return "probe"
    pos = _positionals(rest)
    sub = pos[0] if pos else None
    if sub == "app-server":
        nxt = pos[1] if len(pos) > 1 else None
        return {"proxy": "client", "daemon": "infra"}.get(nxt, "agent")
    if sub == "mcp":
        return "agent" if len(pos) > 1 and pos[1] == "serve" else "probe"
    if sub in _PROBE_SUB:
        return "probe"
    if sub in _CLIENT_SUB:
        return "client"
    if sub in _INFRA_SUB:
        return "infra"
    return "agent"


def spawned_by_agent(role, command):
    """True for a process its agent starts directly: its parent IS the agent."""
    return role == "tool-shell" or \
        (role == "sandbox" and (command or "").startswith("codex-linux-san"))
