"""Shared coding-agent / IDE / sandbox classification for the collectors.

VENDORED COPY — see ../VENDORED.md. Upstream is collect/common/agent_classify.py; keep the two in sync.
`ebpfm.sh check` diffs them whenever it can see the repo.

Single source of truth for the login-node agent collectors
(`../agents/agent_census.py`, `../agents/proc_trace.py`). Deliberately small and
dependency-free so it can be imported by a path-invoked collector script.

`../common/snapshot.py` keeps its own inline copy on purpose (it is a standalone,
import-free node probe); `../../analyze/agent_lib.py` keeps a richer offline copy.
Keep all of these in sync — order matters: an agent running inside an IDE host
(e.g. Claude Code under .vscode-server) must be tagged as the agent, and bwrap
(the sandbox wrapper) stays last so a Cursor/agent sandbox is told from a generic
probe.

LOCAL CHANGE (see ../VENDORED.md): classify() reads the program a command line
runs, not every word of it. Matched anywhere, the patterns below tagged a human's
`grep claude notes.txt`, `vim ~/.claude/CLAUDE.md` or `git clone
https://github.com/openai/codex`, and the tracer made each one an agent root of
its own, an agent session per command.
"""
import re

# The agents' own programs: their names, and the places they are installed. Read
# against the program words of a command line (_program_words), one word at a time.
# A project's .claude/worktrees/ holds the project's programs, not Claude's.
_AGENT_PATTERNS = [
    ('claude_code', re.compile(r'anthropic\.claude-code|native-binary/claude'
                               r'|/\.claude/(?!worktrees/)|claude-agent-sdk'
                               r'|(?:^|/|\s)claude(?:\s|$)'
                               r'|/claude/versions/|@anthropic-ai/claude-code'
                               r'|claude-code/cli\.[cm]?js$', re.I)),
    ('codex',       re.compile(r'@openai/codex|openai\.chatgpt|codex[ -]app-server'
                               r'|/\.codex/|codex-linux-sandbox|codex-bin|codex-homes'
                               r'|(?:^|/|\s)codex(?:\s|$|@)', re.I)),
    ('copilot',     re.compile(r'github\.copilot|/copilot(?:\s|$)', re.I)),
    ('cursor',      re.compile(r'\.cursor-server|\.cursor/', re.I)),
    ('windsurf',    re.compile(r'\.windsurf-server|\.codeium/windsurf', re.I)),
    ('vscode',      re.compile(r'\.vscode-server|\.vscode/cli', re.I)),
    ('bwrap',       re.compile(r'(?:^|/)bwrap(?:\s|$)')),
]

# Runtime artifacts: what only an agent's runtime writes on a command line, read
# anywhere on it, in the order above. Cursor's and Windsurf's sandbox and terminal
# markers; and Claude Code's tool-call shell, a shell that sources a snapshot of
# the user's own (`zsh -c source ~/.claude/shell-snapshots/snapshot-zsh-1.sh &&
# eval '...'`, also behind the sandbox runtime's `exec 3<&- ...;`).
_MARKERS = {
    'cursor':   re.compile(r'CURSOR_SANDBOX', re.I),
    'windsurf': re.compile(r'windsurf-terminal|WINDSURF_STATE', re.I),
}
_TOOL_SHELL = re.compile(r'/\.claude/shell-snapshots/')
_SHELLS = frozenset(('sh', 'bash', 'zsh', 'dash', 'ksh', 'fish', 'ash'))

# Programs that run the word after them as the program: (options that take a
# value, words before that program). `env FOO=1 claude`, `nohup codex`,
# `timeout 600 claude -p ...`, and `exec claude` in a shell's -c command.
_WRAPPERS = {
    'env': (('-u', '--unset', '-C', '--chdir'), 0),
    'exec': (('-a',), 0),
    'nohup': ((), 0),
    'setsid': ((), 0),
    'nice': (('-n', '--adjustment'), 0),
    'timeout': (('-s', '--signal', '-k', '--kill-after'), 1),
}
# Package runners, and the subcommand that makes one: what they run is a package
# (`npx -y @anthropic-ai/claude-code`, `npm exec @openai/codex`, `bunx`, `pnpm dlx`).
# Run through its #! line, npx is node's script.
_RUNNERS = {'npx': (), 'npx-cli': (), 'pnpx': (), 'bunx': (),
            'npm': ('exec', 'x'), 'npm-cli': ('exec', 'x'), 'pnpm': ('exec', 'dlx'),
            'yarn': ('exec', 'dlx'), 'bun': ('x',)}
_RUNNER_VALUES = ('-p', '--package', '-c', '--call', '--cache')
# Interpreters, whose script is the program: a JavaScript host runs an agent's own
# launcher (`node .../claude-code/cli.js`); a shell or Python, a wrapper or a test
# double named after one. A shell's -c command starts at a program of its own.
_INTERPRETER = re.compile(r'^(?:node|nodejs|bun|deno|python(?:\d+(?:\.\d+)*)?|pypy3?)$')
_INTERPRETER_VALUES = ('-r', '--require', '--import', '--loader', '--experimental-loader',
                       '--title', '-W', '-X')
_SHELL_VALUES = ('-o', '-O', '--rcfile', '--init-file')
_INLINE = ('-c', '-e', '--eval', '-p', '--print', '-m')     # code or a module, no script
_ASSIGN = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*=')

# bwrap is the sandbox wrapper, not a session — collectors summarise it separately.
SANDBOX_TYPES = {'bwrap'}

# --- 3-way actor class -------------------------------------------------------
# MUST stay in sync with the canonical `analyze/agent_lib.py: actor3()`, which is
# the source of truth for the label set. Collectors cannot import from analyze/
# (they are SSH-deployed and run standalone against collect/ only), so the logic
# is mirrored here rather than shared.
#
# The label is 'human-vscode', NOT 'vscode': the repo migrated away from 'vscode'
# on 2026-07-28 and datasets are physically Hive-partitioned by
# {agent, human, human-vscode} (agent_lib.PHYSICAL_CLASSES). Emitting 'vscode'
# would silently miss every downstream filter written against the canonical name.
REAL_AGENT_TYPES = frozenset({'claude_code', 'codex', 'cursor', 'copilot', 'windsurf'})


def is_agent_side(agent_type):
    """True if agent_type belongs on the AGENT side: a real coding agent, or the
    `bwrap` sandbox those agents run tool-commands inside (agent infrastructure,
    never a human tty)."""
    return agent_type in REAL_AGENT_TYPES or agent_type == 'bwrap'


def actor3(actor, agent_type):
    """Canonical 3-way class — mirrors analyze/agent_lib.actor3() exactly.

      'agent'        — a real coding agent (or its bwrap sandbox) is responsible
      'human-vscode' — VS Code Remote IDE: a human in an editor
      'human'        — bare-shell / other human activity

    A real agent under .vscode-server (e.g. Claude Code) is already tagged
    claude_code/codex/cursor by classify(), not vscode, so it stays 'agent'.
    None stays None (unlabeled)."""
    if actor == 'agent':
        if is_agent_side(agent_type):
            return 'agent'
        if agent_type == 'vscode':
            return 'human-vscode'
        return 'human'
    return actor    # 'human' or None

LABELS = {
    'claude_code': 'Claude Code',
    'codex':       'OpenAI Codex',
    'copilot':     'GitHub Copilot',
    'cursor':      'Cursor IDE',
    'windsurf':    'Windsurf',
    'vscode':      'VS Code Remote',
    'bwrap':       'bwrap sandbox',
}

# Autonomous / unsupervised-execution flags that bypass human approval & sandboxes.
_AUTONOMOUS = re.compile(
    r'--dangerously-skip-permissions'
    r'|--dangerously-bypass-approvals-and-sandbox'
    r'|danger-full-access'
    r'|--ask-for-approval\s+never'
    r'|--yolo'
    r'|--sandbox\s+danger', re.I)


def _program_name(word):
    """A word's program name: its last path part, less a JavaScript extension."""
    name = word.rsplit('/', 1)[-1]
    for ext in ('.js', '.mjs', '.cjs'):
        if name.endswith(ext):
            return name[:-len(ext)]
    return name


def _past_options(words, i, values):
    """The index of the first word from i on that is neither an option nor the
    value of one."""
    while i < len(words) and len(words[i]) > 1 and words[i][0] == '-':
        if words[i] == '--':
            return i + 1
        i += 2 if words[i] in values else 1
    return i


def _program_words(words):
    """The words of a command line that name the program it runs: argv[0]; past
    a wrapper or a VAR=value, the word run; a package runner's package; an
    interpreter's script; the first word of a shell's -c command. Every word of
    a bwrap's, which is tagged by what it confines, as every command line was."""
    out = []
    i, n = 0, len(words)
    while i < n:
        word = words[i]
        if _ASSIGN.match(word):
            i += 1
            continue
        out.append(word)
        name = _program_name(word)
        if name == 'bwrap':
            return words
        if name in _WRAPPERS:
            values, before = _WRAPPERS[name]
            i = _past_options(words, i + 1, values) + before
        elif name in _RUNNERS and (not _RUNNERS[name]
                                   or (i + 1 < n and words[i + 1] in _RUNNERS[name])):
            i = _past_options(words, i + (2 if _RUNNERS[name] else 1), _RUNNER_VALUES)
        elif name in _SHELLS or _INTERPRETER.match(name):
            shell = name in _SHELLS
            values = _SHELL_VALUES if shell else _INTERPRETER_VALUES
            i += 2 if name in ('bun', 'deno') and words[i + 1:i + 2] == ['run'] else 1
            while i < n and len(words[i]) > 1 and words[i][0] == '-':
                opt = words[i]
                if opt == '--':
                    i += 1
                    break
                if not shell and opt in _INLINE:
                    return out
                i += 2 if opt in values else 1
            # the next word is the script, or for a shell's -c the command's first
            # word: a program in its turn (`node /usr/bin/npx`, `sh -c exec claude`)
        else:
            break
    return out


def classify(args):
    """Return the agent category key for a command line, or None.

    Only the program counts (_program_words): `grep claude notes.txt`,
    `cat ~/.claude/settings.json` and `pip install claude-agent-sdk` run no
    agent. So do the runtime artifacts (_MARKERS, _TOOL_SHELL)."""
    if not args:
        return None
    words = _program_words(args.split())
    # claude_code comes first in _AGENT_PATTERNS, so its tool-call shell can be
    # tried before them
    if _TOOL_SHELL.search(args) and any(_program_name(w) in _SHELLS for w in words):
        return 'claude_code'
    for name, rx in _AGENT_PATTERNS:
        if any(rx.search(w) for w in words):
            return name
        marker = _MARKERS.get(name)
        if marker is not None and marker.search(args):
            return name
    return None


# The approval-bypass flags split two ways. Some only skip the human approval
# prompt; some ALSO turn the sandbox off. They are different facts about a run
# and the dashboard needs them apart, but `is_autonomous` keeps using the union
# so every existing finding stays comparable.
_APPROVAL_ONLY = re.compile(
    r'--dangerously-skip-permissions'
    r'|--ask-for-approval\s+never'
    r'|--yolo'
    r'|--permission-mode\s+bypassPermissions', re.I)

# These imply BOTH bypassed approval and a disabled sandbox.
_APPROVAL_AND_SANDBOX = re.compile(
    r'--dangerously-bypass-approvals-and-sandbox'
    r'|danger-full-access'
    r'|--sandbox\s+danger', re.I)


def approval_mode_of(args):
    """('bypassed'|None, sandbox_disabled: bool) from a command line.

    Split out of is_autonomous so "runs unattended" and "runs unconfined" can be
    reported independently -- a run can be either without being the other."""
    if not args:
        return None, False
    if _APPROVAL_AND_SANDBOX.search(args):
        return 'bypassed', True
    if _APPROVAL_ONLY.search(args):
        return 'bypassed', False
    return None, False


def is_autonomous(args):
    """True if the command line carries an approval-bypass / autonomous flag."""
    return bool(args and _AUTONOMOUS.search(args))
