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
"""
import re

_AGENT_PATTERNS = [
    ('claude_code', re.compile(r'anthropic\.claude-code|native-binary/claude'
                               r'|/\.claude/|claude-agent-sdk|(?:^|/|\s)claude(?:\s|$)', re.I)),
    ('codex',       re.compile(r'@openai/codex|openai\.chatgpt|codex[ -]app-server'
                               r'|/\.codex/|codex-linux-sandbox|codex-bin|codex-homes'
                               r'|(?:^|/|\s)codex(?:\s|$|@)', re.I)),
    ('copilot',     re.compile(r'github\.copilot|/copilot(?:\s|$)', re.I)),
    ('cursor',      re.compile(r'CURSOR_SANDBOX|\.cursor-server|\.cursor/', re.I)),
    ('windsurf',    re.compile(r'windsurf-terminal|WINDSURF_STATE|\.windsurf-server'
                               r'|\.codeium/windsurf', re.I)),
    ('vscode',      re.compile(r'\.vscode-server|\.vscode/cli', re.I)),
    ('bwrap',       re.compile(r'(?:^|/)bwrap(?:\s|$)')),
]

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


def classify(args):
    """Return the agent category key for a command line, or None."""
    if not args:
        return None
    for name, rx in _AGENT_PATTERNS:
        if rx.search(args):
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
