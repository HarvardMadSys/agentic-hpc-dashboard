"""Masking credentials and coding-agent prompts in the command lines the
collector writes.

Two kinds, both on by default (EBPFM_REDACT, see ebpf_trace.py):

  keys     credentials -- API keys, tokens, passwords, private keys, an
           Authorization header, a URL's password. The rules (RULES below) are
           the dashboard's, copied from dashboard/rc_dashboard/redact.py because
           this folder runs standalone; a test holds the two copies equal
           whenever both are present. Same mask, so the dashboard's own pass
           over a collector-masked line changes nothing.
  prompts  the free text a coding agent is handed on its command line:
           `claude -p "..."`, `codex exec "..."`, `gemini -p "..."` and the
           like, run directly, through node/npx/uvx, under a wrapper
           (timeout, nohup, env, srun, ssh, ...) or inside a shell command
           string (`bash -c '...'`, `sbatch --wrap=...`). The agent's flags,
           model and subcommand stay; each run of prompt text becomes one
           PROMPT_MASK.

What is masked is the VALUE, never the structure around it (the dashboard's
rule): the flag, the variable name and the subcommand stay, so a reader can
still tell what kind of thing was there and that it was removed.

The collector masks what it WRITES. Its in-memory argv stays raw: agent
classification, the approval flags and the endpoint names are read from it.

Python 3.6 compatible (EL8): no walrus, no shlex.join.
"""
import re
import shlex

MASK = "[REDACTED]"
PROMPT_MASK = "[REDACTED-PROMPT]"

# ---------------------------------------------------------------------------
# credentials: dashboard/rc_dashboard/redact.py, verbatim from here to RULES' end
# ---------------------------------------------------------------------------

# A value that only refers to a secret is not one: `--token $HF_TOKEN`.
_NOT_A_REF = r"(?!\$|\"\$|'\$)"
_VALUE = r"(\"[^\"]*\"|'[^']*'|[^\s\"']+)"

# Names that hold a credential, as an env var, a kwarg or a query key. Matched
# on the END of the name: `HF_TOKEN`, `db_password`, `AWS_SECRET_ACCESS_KEY`.
# `max_tokens` ends in an S and is left alone; `PWD` alone is the cwd.
_SECRET_NAME = (r"[A-Za-z0-9_]*(?:TOKEN|SECRET|PASSWORD|PASSWD|PASSPHRASE|_PASS|_PWD|"
                r"API_?KEY|ACCESS_?KEY|SECRET_?KEY|PRIVATE_?KEY|AUTH_?KEY|CREDENTIALS?)")

_NAME_WORDS = ("token", "secret", "password", "passwd", "passphrase", "_pass", "_pwd", "key",
               "credential")

# (name, gate, pattern, substitution). The gate is cheap substring tests on the
# lowercased text -- every group must have one hit -- and the pattern only runs
# when it passes. Most command lines pass no gate at all, which is what keeps a
# pass over a live tail or a full export cheap: measured on a login node's real
# exits, the patterns alone cost ~150 us a line, the gates a few.
RULES = (
    # A PEM private key pasted inline: the whole block, header to footer.
    ("private_key", (("private key",),), re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)", re.S),
     lambda m: MASK),
    # Credential headers, inside -H "..." or anywhere: keep the header name and scheme.
    ("auth_header", (("authorization",),), re.compile(
        r"((?:Proxy-)?Authorization\s*:\s*(?:Bearer|Basic|Token|Digest|Bot)?\s*)([^\s\"']+)", re.I),
     lambda m: m.group(1) + MASK),
    ("key_header", ((":",), ("api-key", "apikey", "api_key", "auth-token", "access-token",
                             "private-token", "secret")), re.compile(
        r"((?:X-[A-Za-z-]*)?(?:Api[-_]?Key|Auth-?Token|Access-?Token|Private-?Token|Secret)\s*:\s*)"
        r"([^\s\"']+)", re.I),
     lambda m: m.group(1) + MASK),
    # scheme://user:password@host -- keep the user; a long user alone is a token.
    # Anchored on the literal `://`, not on the scheme, which would be tried at
    # every character of the line.
    ("url_password", (("://",), ("@",)), re.compile(r"(://[^/\s:@\"']+:)([^@\s/\"']+)(@)"),
     lambda m: m.group(1) + MASK + m.group(3)),
    ("url_token_user", (("://",), ("@",)), re.compile(r"(://)([^/\s:@\"']{20,})(@)"),
     lambda m: m.group(1) + MASK + m.group(3)),
    # curl/wget -u user:password, --user user:password
    ("user_password", (("-u", "--user", "--proxy-user"), (":",)), re.compile(
        r"((?:^|\s)(?:-u|--user|--proxy-user)\s*)([^\s:\"']+:)([^\s\"']+)"),
     lambda m: m.group(1) + m.group(2) + MASK),
    # --token X, --hf-token=X, --api-key X, --password X, --secret X ... A flag
    # starts an argument: the `-credential` inside `git-credential` is not one.
    ("secret_flag", (("-",), _NAME_WORDS), re.compile(
        r"((?<![^\s\"'=])--?[A-Za-z0-9-]*(?:token|secret|password|passwd|passphrase|api-?key|"
        r"apikey|access-?key|private-?key|auth-?key|credentials?)(?:=|\s+))" + _NOT_A_REF + r"(?!-)"
        + _VALUE, re.I),
     lambda m: m.group(1) + MASK),
    # NAME=value for a credential-named variable: env prefix, export, kwarg. No
    # spaces around `=` -- that is how a shell or a kwarg writes it, while
    # `token = tokens[i]` inside an inline script is code, not a credential.
    ("secret_assign", (("=",), _NAME_WORDS), re.compile(
        r"(\b" + _SECRET_NAME + r"=)" + _NOT_A_REF + _VALUE, re.I),
     lambda m: m.group(1) + MASK),
    # The same names as JSON keys: curl -d '{"password": "..."}'.
    ("secret_json", ((":",), _NAME_WORDS), re.compile(
        r"([\"']" + _SECRET_NAME + r"[\"']\s*:\s*)(\"[^\"]*\"|'[^']*')", re.I),
     lambda m: m.group(1) + MASK),
    # ?access_token=...&key=...: query-string credentials, including signed URLs.
    ("query_secret", (("?", "&"), ("=",)), re.compile(
        r"([?&](?:access_token|refresh_token|id_token|token|api_?key|key|secret|password|"
        r"sig|signature|X-Amz-Signature|X-Amz-Credential|X-Goog-Signature)=)([^&\s\"'#]+)", re.I),
     lambda m: m.group(1) + MASK),
    # mysql -pSECRET (no space), redis-cli -a SECRET, sshpass -p SECRET. Not psql:
    # its -p is the port.
    ("db_password", (("mysql", "mariadb", "mongo"), ("-p",)), re.compile(
        r"((?:^|[\s/])(?:mysql|mysqldump|mysqladmin|mariadb|mongo|mongosh)\b[^\n]*?\s-p)(?=\S)"
        r"([^\s\"']+)"),
     lambda m: m.group(1) + MASK),
    ("short_password", (("redis-cli", "sshpass"),), re.compile(
        r"((?:^|[\s/])(?:redis-cli\b[^\n]*?\s-a|sshpass\s+-p)\s*)" + _NOT_A_REF + r"([^\s\"']+)"),
     lambda m: m.group(1) + MASK),
    # Token shapes that are secrets wherever they appear, flag or not.
    ("token_shape", (("sk-", "ghp_", "gho_", "ghu_", "ghs_", "ghr_", "github_pat_", "glpat-",
                      "hf_", "akia", "asia", "xox", "aiza", "npm_", "eyj"),), re.compile(
        r"\b(?:sk-(?:ant-|proj-)?[A-Za-z0-9_\-]{20,}"
        r"|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}"
        r"|glpat-[A-Za-z0-9_\-]{20,}"
        r"|hf_[A-Za-z0-9]{30,}"
        r"|(?:AKIA|ASIA)[0-9A-Z]{16}"
        r"|xox[abprs]-[A-Za-z0-9-]{10,}"
        r"|AIza[0-9A-Za-z_\-]{35}"
        r"|npm_[A-Za-z0-9]{36}"
        r"|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,})"),
     lambda m: MASK),
)


def redact_keys(text):
    """(`text` with every credential value masked, names of the rules that
    masked something). MASK itself matches no rule, so a second pass is a no-op."""
    if not text:
        return text, []
    fired = []
    low = text.lower()
    for name, gate, rx, sub in RULES:
        if all(any(w in low for w in group) for group in gate):
            new = rx.sub(sub, text)
            if new != text:
                fired.append(name)
                text = new
                low = text.lower()
    return text, fired


# ---------------------------------------------------------------------------
# prompts
# ---------------------------------------------------------------------------
#
# A CLI is described by what its arguments ARE, because the prompt is whatever is
# left once the rest is accounted for:
#   pos     a bare positional at the top level is prompt text
#   prompt  flags whose value is prompt text
#   value   flags that take a value which is not (a model, a path, an id) -- a
#           value is still masked if it reads as prose (whitespace, or long),
#           unless it is JSON, which a prompt never is
#   free    value flags whose values have spaces by nature (tool lists, a
#           settings path): kept whatever they look like
#   subs    first-positional subcommands: 'prompt' (prompt text follows), 'quiet'
#           (no prompt anywhere after it), 'model' (one model name, then prompt
#           text), 'files' (positionals are paths)
#   flags   long flags known to take NO value (the next token is judged alone)
#   need    only a CLI when one of these subcommands follows: a generic name
# An unknown long flag followed by one short word keeps it as its value; any
# other token after an unknown flag is judged on its own, so the failure mode of
# a flag this table does not know is masking too much, or one word too little.

def _cli(pos=True, prompt=(), value=(), free=(), flags=(), subs=None, need=None):
    return {'pos': pos, 'prompt': frozenset(prompt), 'value': frozenset(value) | frozenset(free),
            'free': frozenset(free), 'flags': frozenset(flags), 'subs': dict(subs or {}),
            'need': frozenset(need or ())}


_CLAUDE_QUIET = ('mcp', 'config', 'update', 'doctor', 'install', 'migrate-installer',
                 'setup-token', 'plugin', 'plugins', 'auth', 'login', 'logout', 'agents')
CLIS = {
    'claude': _cli(
        prompt=('--append-system-prompt', '--system-prompt', '--agents'),
        flags=('--print', '--continue', '--verbose', '--debug', '--dangerously-skip-permissions',
               '--allow-dangerously-skip-permissions', '--ide', '--strict-mcp-config',
               '--include-partial-messages', '--replay-user-messages', '--fork-session',
               '--version', '--help'),
        value=('--model', '--fallback-model', '--output-format', '--input-format',
               '--permission-mode', '--session-id', '-r', '--resume', '--max-turns',
               '--plugin-dir', '--permission-prompt-tool', '--system-prompt-file',
               '--append-system-prompt-file', '--max-budget-usd', '--debug-file', '--effort'),
        free=('--allowedTools', '--allowed-tools', '--disallowedTools', '--disallowed-tools',
              '--tools', '--mcp-config', '--settings', '--add-dir', '--setting-sources',
              '--json-schema', '--betas'),
        subs=dict.fromkeys(_CLAUDE_QUIET, 'quiet')),
    'codex': _cli(
        flags=('--full-auto', '--dangerously-bypass-approvals-and-sandbox', '--oss', '--json',
               '--search', '--skip-git-repo-check', '--last', '--include-plan-tool',
               '--version', '--help'),
        value=('-m', '--model', '-c', '--config', '-p', '--profile', '-s', '--sandbox',
               '-a', '--ask-for-approval', '-C', '--cd', '-i', '--image', '-o',
               '--output-last-message', '--output-schema', '--color', '--enable', '--disable',
               '--add-dir', '--local-provider'),
        subs=dict(dict.fromkeys(('exec', 'e', 'resume'), 'prompt'),
                  **dict.fromkeys(('login', 'logout', 'mcp', 'mcp-server', 'app-server',
                                   'proto', 'completion', 'debug', 'apply', 'a', 'cloud',
                                   'sandbox', 'features', 'help'), 'quiet'))),
    'gemini': _cli(
        prompt=('-p', '--prompt', '-i', '--prompt-interactive'),
        flags=('--yolo', '--sandbox', '--debug', '--all-files', '--show-memory-usage',
               '--checkpointing', '--screen-reader', '--version', '--help'),
        value=('-m', '--model', '--approval-mode', '--include-directories', '-e',
               '--extensions', '--allowed-mcp-server-names', '--allowed-tools', '-o',
               '--output-format', '--telemetry-target', '--telemetry-otlp-endpoint',
               '--proxy'),
        subs=dict.fromkeys(('mcp', 'extensions', 'auth'), 'quiet')),
    'qwen': _cli(
        prompt=('-p', '--prompt', '-i', '--prompt-interactive'),
        value=('-m', '--model', '--approval-mode', '--include-directories', '-e',
               '--extensions', '-o', '--output-format', '--proxy'),
        subs=dict.fromkeys(('mcp', 'extensions'), 'quiet')),
    # aider's positionals are the files to edit
    'aider': _cli(pos=False, prompt=('-m', '--message', '--msg'),
                  value=('--model', '--edit-format', '--chat-mode', '--message-file', '-f',
                         '--read', '--file', '--weak-model', '--editor-model', '--map-tokens',
                         '--env-file', '--config', '-c')),
    # `opencode [project]` names a directory; `opencode run ...` takes the prompt
    'opencode': _cli(pos=False,
                     value=('-m', '--model', '--agent', '-s', '--session', '-f', '--format',
                            '--port', '--hostname', '--title', '--attach', '--log-level'),
                     subs=dict({'run': 'prompt'},
                               **dict.fromkeys(('auth', 'mcp', 'serve', 'web', 'upgrade',
                                                'models', 'agent', 'github', 'export',
                                                'import', 'stats'), 'quiet'))),
    'cursor-agent': _cli(
        value=('-m', '--model', '--output-format', '--resume', '-a'),
        subs=dict.fromkeys(('login', 'logout', 'status', 'update', 'upgrade', 'mcp', 'ls',
                            'create-chat', 'install-shell-integration',
                            'uninstall-shell-integration'), 'quiet')),
    'goose': _cli(pos=False, prompt=('-t', '--text'),
                  value=('-i', '--instructions', '-n', '--name', '-r', '--resume',
                         '--recipe', '--provider', '--model', '--max-turns'),
                  subs=dict({'run': 'prompt'},
                            **dict.fromkeys(('configure', 'info', 'mcp', 'update', 'bench',
                                             'web', 'recipe', 'schedule', 'project',
                                             'projects'), 'quiet'))),
    'amp': _cli(prompt=('-x', '--execute'), value=('--settings-file', '--log-level',
                                                    '--log-file', '-m', '--mode'),
                subs=dict.fromkeys(('login', 'logout', 'mcp', 'tools', 'permissions',
                                    'update', 'doctor', 'threads'), 'quiet')),
    'crush': _cli(pos=False, value=('-c', '--cwd', '-D', '--data-dir', '-m', '--model'),
                  subs=dict({'run': 'prompt'},
                            **dict.fromkeys(('logs', 'update-providers', 'schema'), 'quiet'))),
    'copilot': _cli(prompt=('-p', '--prompt'),
                    value=('--model', '--allow-tool', '--deny-tool', '--add-dir',
                           '--log-dir', '--log-level', '--resume', '--agent'),
                    subs=dict.fromkeys(('help', 'version', 'login', 'logout'), 'quiet')),
    'cn': _cli(prompt=('-p', '--print'), value=('--config', '--model', '--rule', '--mcp',
                                                '--org', '--resume', '--format')),
    'droid': _cli(value=('-m', '--model', '-o', '--output-format', '--cwd', '--auto',
                         '-r', '--reasoning-effort', '-s', '--session-id', '-f', '--file'),
                  subs={'exec': 'prompt'}),
    'llm': _cli(prompt=('-s', '--system'),
                value=('-m', '--model', '-t', '--template', '-a', '--attachment', '--at',
                       '-f', '--fragment', '--sf', '--system-fragment', '-T', '--tool',
                       '--cid', '--conversation-id', '--key', '-d', '--database'),
                subs=dict({'prompt': 'prompt', 'chat': 'prompt', 'cmd': 'prompt'},
                          **dict.fromkeys(('keys', 'models', 'logs', 'install', 'uninstall',
                                           'plugins', 'templates', 'aliases', 'embed',
                                           'embed-multi', 'embed-models', 'similar',
                                           'collections', 'openai', 'schemas', 'tools',
                                           'fragments'), 'quiet'))),
    'sgpt': _cli(value=('--model', '--temperature', '--top-p', '--chat', '--repl', '--id',
                        '--role', '--show-chat', '--create-role', '--show-role')),
    # `ollama run MODEL [PROMPT]`; every other subcommand is admin
    'ollama': _cli(pos=False, subs=dict({'run': 'model'},
                                        **dict.fromkeys(('serve', 'create', 'show', 'stop',
                                                         'pull', 'push', 'list', 'ls', 'ps',
                                                         'cp', 'rm', 'help'), 'quiet'))),
    # generic names: a CLI only when the subcommand says so
    'q': _cli(pos=False, subs={'chat': 'prompt', 'translate': 'prompt'},
              need=('chat', 'translate')),
    'kiro-cli': _cli(pos=False, subs={'chat': 'prompt', 'translate': 'prompt'},
                     need=('chat', 'translate')),
}

_BY_BASENAME = {name: name for name in CLIS}
_BY_BASENAME.update({'codex.js': 'codex', 'claude.js': 'claude', 'gemini.js': 'gemini',
                     'qwen-code': 'qwen', 'opencode-ai': 'opencode'})
# The entry scripts an interpreter or a package runner names. Matched against
# the script argument only, never a whole command line, and never a helper of
# the same package: codex-linux-sandbox's arguments are the sandboxed tool
# command, not a prompt.
_BY_PATH = (
    ('claude', re.compile(r'@anthropic-ai/claude-code/cli\.js$|/claude/versions/[^/]+$'
                          r'|native-binary/claude$')),
    ('codex', re.compile(r'@openai/codex/bin/codex(?:\.js)?$')),
    ('gemini', re.compile(r'@google/gemini-cli/(?:[^/]+/)*(?:index|gemini)\.js$')),
    ('qwen', re.compile(r'@qwen-code/qwen-code/(?:[^/]+/)*(?:index|cli|qwen)\.js$')),
    ('copilot', re.compile(r'@github/copilot/(?:[^/]+/)*(?:index|npm-loader)\.js$')),
    ('amp', re.compile(r'@sourcegraph/amp/(?:[^/]+/)*(?:main|amp)\.js$')),
    ('opencode', re.compile(r'opencode-ai/bin/opencode(?:\.js)?$')),
)
# what a package runner (npx/uvx/...) is asked to run
_PACKAGES = {'@anthropic-ai/claude-code': 'claude', '@openai/codex': 'codex',
             '@google/gemini-cli': 'gemini', '@qwen-code/qwen-code': 'qwen',
             'opencode-ai': 'opencode', '@sourcegraph/amp': 'amp',
             '@github/copilot': 'copilot', 'aider-chat': 'aider', 'llm': 'llm',
             'shell-gpt': 'sgpt', '@continuedev/cli': 'cn', 'crush': 'crush'}
# `python -m <module>`
_MODULES = {'aider': 'aider', 'llm': 'llm', 'sgpt': 'sgpt'}

# A cheap gate: a command line naming none of these words cannot hold a prompt
# this module masks. `.claude/` paths do not pass it (the dot), so Claude Code's
# own tool commands, which mention ~/.claude on every call, stay cheap.
_CLI_WORD = re.compile(
    r'(?<![\w.\-])(?:' + '|'.join(sorted((re.escape(n) for n in set(_BY_BASENAME) | set(_PACKAGES)
                                          | {'claude-code', 'gemini-cli', 'qwen-code'}),
                                         key=len, reverse=True))
    + r')(?![\w\-])')

# The same names where a command starts -- the start, or after ; & | ( ` $( or
# a newline -- past env assignments and the commonest wrappers. Used only on a
# string the lexer could not split.
_GENERIC = ('q', 'kiro-cli')
_CLI_AT_COMMAND = re.compile(
    r'(?:^|[;&|\n(`]|\$\()\s*(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)*'
    r'(?:(?:sudo|nohup|exec|time|env|setsid|timeout\s+\S+|nice\s+-n\s*\S+)\s+)*'
    r'(?:\S*/)?('
    + '|'.join(sorted((re.escape(n) for n in _BY_BASENAME if n not in _GENERIC),
                      key=len, reverse=True))
    + r'|(?:q|kiro-cli)(?=\s+(?:chat|translate)\b))(?=\s|$|[;&|)])')

# ---- command positions: what runs a command it was handed -------------------
# wrapper: (options that take a value, positionals before the command)
_SLURM_VALUE = frozenset((
    '-A', '-c', '-C', '-d', '-D', '-e', '-G', '-i', '-J', '-L', '-m', '-n', '-N', '-o',
    '-p', '-q', '-r', '-t', '-T', '-w', '-W', '-x', '-B', '--account', '--partition',
    '--time', '--job-name', '--nodes', '--ntasks', '--cpus-per-task', '--mem', '--gres',
    '--qos', '--constraint', '--output', '--error', '--chdir', '--dependency'))
_WRAPPERS = {
    'timeout': (frozenset(('-s', '--signal', '-k', '--kill-after')), 1),
    'nice': (frozenset(('-n', '--adjustment')), 0),
    'ionice': (frozenset(('-c', '--class', '-n', '--classdata')), 0),
    'nohup': (frozenset(), 0), 'setsid': (frozenset(), 0), 'exec': (frozenset(('-a',)), 0),
    'time': (frozenset(('-f', '--format', '-o', '--output')), 0),
    'stdbuf': (frozenset(('-i', '-o', '-e')), 0),
    'chrt': (frozenset(), 1), 'taskset': (frozenset(), 1),
    'env': (frozenset(('-u', '--unset', '-C', '--chdir')), 0),
    'sudo': (frozenset(('-u', '--user', '-g', '--group', '-C', '-D', '--chdir', '-h',
                        '--host', '-p', '--prompt', '-r', '--role', '-t', '--type', '-T',
                        '-U')), 0),
    'runuser': (frozenset(('-u', '--user', '-g', '--group', '-G')), 0),
    'srun': (_SLURM_VALUE, 0), 'salloc': (_SLURM_VALUE, 0),
    'xargs': (frozenset(('-a', '-d', '-E', '-I', '-L', '-n', '-P', '-s', '--arg-file',
                         '--delimiter', '--max-args', '--max-procs', '--max-chars')), 0),
    'ssh': (frozenset(('-b', '-c', '-D', '-E', '-e', '-F', '-I', '-i', '-J', '-L', '-l',
                       '-m', '-O', '-o', '-p', '-Q', '-R', '-S', '-W', '-w', '-B')), 1),
    'strace': (frozenset(('-e', '-o', '-p', '-s', '-u', '-E')), 0),
    'ltrace': (frozenset(('-e', '-o', '-p', '-s', '-u')), 0),
}
_INTERPRETERS = re.compile(r'^(?:node|nodejs|bun|deno|python(?:\d+(?:\.\d+)?)?|pypy3?)$')
_INTERP_VALUE = frozenset(('-r', '--require', '--import', '--loader', '--experimental-loader',
                           '-W', '-X', '--inspect-port', '--title', '--env-file'))
_RUNNERS = {'npx': frozenset(('-p', '--package', '--cache')), 'pnpx': frozenset(),
            'bunx': frozenset(), 'uvx': frozenset(('--from', '--with', '-p', '--python',
                                                   '--index-url', '--extra-index-url'))}
# `uv run`, `uv tool run`, `pnpm dlx`, `pnpm exec`, `yarn dlx`, `bun x`
_RUNNER_SUBS = {'uv': ('run', 'tool'), 'pnpm': ('dlx', 'exec'), 'yarn': ('dlx', 'exec'),
                'bun': ('x',)}
_SHELLS = frozenset(('sh', 'bash', 'zsh', 'dash', 'ksh', 'mksh', 'fish', 'ash', 'busybox'))
_ENV_ASSIGN = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*=')
_PROSE = re.compile(r'\s')


def _basename(tok):
    return tok.rsplit('/', 1)[-1]


def cli_of(tok):
    """The agent CLI `tok` runs, as a program or an interpreter's script, or None."""
    name = _BY_BASENAME.get(_basename(tok))
    if name is not None:
        return name
    if '/' in tok:
        for name, rx in _BY_PATH:
            if rx.search(tok):
                return name
    return None


def _package_cli(spec):
    """npx's `@scope/pkg@version` -> the CLI it is, or None."""
    if spec.startswith('@'):
        parts = spec.split('@')          # ['', 'scope/pkg', 'version']
        pkg = '@' + parts[1] if len(parts) > 1 else spec
    else:
        pkg = spec.split('@', 1)[0]
    pkg = pkg.split('==', 1)[0]           # uvx pkg==1.2
    return _PACKAGES.get(pkg)


def _skip_options(toks, i, value_opts):
    """Index of the first argument at or after `i` that is not an option, nor
    the value of one."""
    n = len(toks)
    while i < n:
        t = toks[i]
        if t == '--':
            return i + 1
        if not t.startswith('-') or t == '-':
            return i
        name = t.split('=', 1)[0]
        i += 2 if (name in value_opts and '=' not in t) else 1
    return i


def find_cli(toks):
    """(index of the agent CLI's program token, its name) when `toks` runs one
    -- directly or through wrappers, an interpreter, or a package runner -- else
    (None, None). Only command positions are tried, so `grep claude notes.txt`
    is not mistaken for an agent run."""
    i, n = 0, len(toks)
    while i < n:
        t = toks[i]
        if _ENV_ASSIGN.match(t):
            i += 1
            continue
        name = cli_of(t)
        if name is not None:
            spec = CLIS[name]
            if spec['need']:
                j = _skip_options(toks, i + 1, spec['value'])
                if j >= n or toks[j] not in spec['need']:
                    return None, None
            return i, name
        base = _basename(t)
        if base in _WRAPPERS:
            opts, npos = _WRAPPERS[base]
            i = _skip_options(toks, i + 1, opts)
            if base == 'env':
                while i < n and _ENV_ASSIGN.match(toks[i]):
                    i += 1
            i += npos
            continue
        if _INTERPRETERS.match(base):
            j = i + 1
            while j < n and toks[j].startswith('-') and toks[j] != '-':
                o = toks[j]
                if o in ('-e', '--eval', '-p', '--print', '-c'):
                    return None, None           # inline code, not a script
                if o == '-m' and j + 1 < n:
                    mod = _MODULES.get(toks[j + 1])
                    return (j + 1, mod) if mod else (None, None)
                j += 2 if (o.split('=', 1)[0] in _INTERP_VALUE and '=' not in o) else 1
            if j < n:
                name = cli_of(toks[j])
                if name is not None:
                    return j, name
            return None, None
        if base in _RUNNERS:
            j = _skip_options(toks, i + 1, _RUNNERS[base])
            if j < n:
                name = _package_cli(toks[j])
                if name is not None:
                    return j, name
                i = j                           # `npx -p pkg claude ...`
                continue
            return None, None
        if base in _RUNNER_SUBS and i + 1 < n and toks[i + 1] in _RUNNER_SUBS[base]:
            j = i + 2
            if base == 'uv' and toks[i + 1] == 'tool' and j < n and toks[j] == 'run':
                j += 1
            j = _skip_options(toks, j, _RUNNERS['uvx'])
            if j < n:
                name = _package_cli(toks[j]) or cli_of(toks[j])
                if name is not None:
                    return j, name
            return None, None
        return None, None
    return None, None


_NO_PROMPT_FLAGS = frozenset(('--help', '-h', '--version', '-V', '-v'))
# a session or thread id: a UUID, or a long run of hex
_ID = re.compile(r'^(?:[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-'
                 r'[0-9a-fA-F]{12}|[0-9a-fA-F]{16,})$')


def _walk(spec, toks, exact=True):
    """An agent CLI's arguments with its prompt text masked; (tokens, masked?).

    `exact` says the tokens are the real arguments. A line split on spaces is
    not: a prompt's words are separate tokens there, so nothing that would keep
    one of them -- a lone path, an id, a word after an unknown flag -- is kept."""
    if any(t in _NO_PROMPT_FLAGS for t in toks if not _PROSE.search(t)):
        return list(toks), False            # `codex exec --help`: a usage screen
    out, fired = [], False
    pos_prompt, model_next, first = spec['pos'], False, True
    i, n = 0, len(toks)

    def put_mask():
        if not out or out[-1] != PROMPT_MASK:
            out.append(PROMPT_MASK)

    while i < n:
        t = toks[i]
        if t == '--':
            out.append(t)
            if i + 1 < n:
                if pos_prompt:
                    put_mask()
                    fired = True
                else:
                    out.extend(toks[i + 1:])
            break
        if t.startswith('-') and len(t) > 1 and not t[1:2].isdigit():
            name, eq, val = t.partition('=')
            if eq:
                if val and (name in spec['prompt']
                            or (name not in spec['free'] and _prose_value(val))):
                    out.append(name + '=' + PROMPT_MASK)
                    fired = True
                else:
                    out.append(t)
                i += 1
                continue
            out.append(t)
            if i + 1 < n and (name in spec['prompt'] or name in spec['value']):
                v = toks[i + 1]
                if name in spec['prompt'] or (name not in spec['free'] and _prose_value(v)):
                    out.append(PROMPT_MASK)
                    fired = True
                else:
                    out.append(v)
                i += 2
                continue
            if (exact and i + 1 < n and name.startswith('--') and name not in spec['flags']
                    and _word_value(toks[i + 1])):
                # A long flag this table does not know, then one short word: far
                # likelier its value (`--effort max`) than a one-word prompt.
                out.append(toks[i + 1])
                i += 2
                continue
            i += 1
            continue
        if first:
            first = False
            sub = spec['subs'].get(t)
            if sub is not None:
                out.append(t)
                if sub == 'quiet':
                    out.extend(toks[i + 1:])
                    break
                pos_prompt = sub in ('prompt', 'model')
                model_next = sub == 'model'
                i += 1
                continue
        if model_next:
            out.append(t)
            model_next = False
        elif exact and pos_prompt and (('/' in t and not _PROSE.search(t)) or _ID.match(t)):
            # one path, no spaces: a script or a directory (cursor-agent's
            # launcher hands itself its index.js); or a session id. Not text.
            out.append(t)
        elif pos_prompt:
            put_mask()
            fired = True
        else:
            out.append(t)
        i += 1
    return out, fired


def _word_value(v):
    """One short word that is not itself a flag: how a flag's value looks."""
    return (bool(v) and not v.startswith('-') and len(v) <= 64
            and not _PROSE.search(v))


def _prose_value(v):
    """A flag value that reads as text rather than as a name, path, number or
    JSON (an MCP config, a schema) -- whose secrets the credential rules mask."""
    if v[:1] in ('{', '['):
        return False
    return bool(_PROSE.search(v)) or len(v) > 256


def _quote(t):
    if t in (MASK, PROMPT_MASK):
        return t
    return shlex.quote(t)


_OPERATOR = re.compile(r'^[();<>|&\n]+$')


def _redact_shell(s, depth, exact=True):
    """A shell command string with every agent prompt in it masked; (str, masked?)."""
    if not _CLI_WORD.search(s):
        return s, False
    try:
        lex = shlex.shlex(s, posix=True, punctuation_chars='();<>|&\n')
        lex.whitespace = ' \t\r'           # a newline ends a command, as `;` does
        lex.whitespace_split = True
        toks = list(lex)
    except ValueError:
        # Not shell a lexer can follow -- a command line the kernel cut short, or
        # a heredoc of Python. An agent run at a command position in it is
        # masked from the agent's name on, since nothing after can be told apart
        # from prompt text; a name anywhere else (`--planner qwen`) is just text.
        m = _CLI_AT_COMMAND.search(s)
        if m is None:
            return s, False
        return s[:m.end(1)] + ' ' + PROMPT_MASK, True
    out, cmd, fired = [], [], False
    for t in toks + [None]:
        if t is None or _OPERATOR.match(t):
            if cmd:
                red, f = redact_prompt_argv(cmd, depth, exact)
                fired = fired or f
                out.extend(_quote(x) for x in red)
                cmd = []
            if t is not None:
                out.append(t)
        else:
            cmd.append(t)
    if not fired:
        return s, False
    return ' '.join(out), True


def _program_at(toks):
    """(index of the program `toks` runs once wrappers and env assignments are
    passed, whether ssh was one of them) -- `timeout 60 nohup bash ...` -> bash.
    The index is None when nothing is left."""
    i, n, via_ssh = 0, len(toks), False
    while i < n:
        t = toks[i]
        if _ENV_ASSIGN.match(t):
            i += 1
            continue
        base = _basename(t)
        if base not in _WRAPPERS:
            return i, via_ssh
        via_ssh = via_ssh or base == 'ssh'
        opts, npos = _WRAPPERS[base]
        i = _skip_options(toks, i + 1, opts)
        if base == 'env':
            while i < n and _ENV_ASSIGN.match(toks[i]):
                i += 1
        i += npos
    return None, via_ssh


# programs whose arguments are commands to run, each as one string
_RUNS_STRINGS = frozenset(('eval', 'watch', 'tmux', 'screen'))
_TAKES_C = frozenset(('su', 'runuser', 'script'))       # -c CMD / --command=CMD


def _command_strings(toks):
    """[(index, prefix)] of the arguments of `toks` that are themselves shell
    command lines: a shell's -c string, su/script -c, sbatch --wrap, what
    eval/watch/tmux/screen run, and an ssh remote command given as one string.
    Only those: an ordinary argument with spaces in it -- `git commit -m "claude
    fixed it"` -- is text, and parsing it as a command would mask it."""
    out, n = [], len(toks)
    for j, t in enumerate(toks):
        if t.startswith('--wrap=') and _PROSE.search(t):
            out.append((j, '--wrap='))
        elif t == '--wrap' and j + 1 < n:
            out.append((j + 1, ''))
    k, via_ssh = _program_at(toks)
    if k is None:
        return out
    base = _basename(toks[k])
    if _PROSE.search(toks[k]):
        if via_ssh:
            out.append((k, ''))                  # ssh host 'cmd ...'
    elif base in _SHELLS:
        # -c anywhere among the options, then the first argument is the command
        # (`bash -c -l "..."` is how Claude Code runs its tools)
        has_c = False
        for j in range(k + 1, n):
            t = toks[j]
            if t.startswith('-') and not t.startswith('--') and len(t) > 1:
                has_c = has_c or 'c' in t[1:]
                continue
            if t.startswith('--'):
                continue
            if has_c:
                out.append((j, ''))
            break
    elif base in _TAKES_C:
        for j in range(k + 1, n):
            t = toks[j]
            if t in ('-c', '--command') and j + 1 < n:
                out.append((j + 1, ''))
                break
            if t.startswith('--command='):
                out.append((j, '--command='))
                break
    elif base in _RUNS_STRINGS:
        out.extend((j, '') for j in range(k + 1, n) if _PROSE.search(toks[j]))
    return out


def redact_prompt_argv(argv, depth=0, exact=True):
    """(argv with every agent prompt in it masked, masked?).

    The agent run itself, then every command string an argument carries --
    `bash -c '...'`, `sbatch --wrap='...'`, `tmux new 'claude ...'` -- parsed as
    a shell would and handled the same way, three levels deep."""
    out = list(argv)
    fired = False
    i, name = find_cli(out)
    if name is not None:
        tail, fired = _walk(CLIS[name], out[i + 1:], exact)
        out = out[:i + 1] + tail
    if depth < 3:
        for j, head in _command_strings(out):
            t = out[j]
            if t in (MASK, PROMPT_MASK) or not t.startswith(head):
                continue
            red, f = _redact_shell(t[len(head):], depth + 1, exact)
            if f:
                out[j] = head + red
                fired = True
    return out, fired


def _shell_command_index(toks):
    """Where a shell's `-c` command string starts in a space-split line, or None."""
    if not toks or _basename(toks[0]) not in _SHELLS:
        return None
    for j in range(1, len(toks)):
        t = toks[j]
        if not t.startswith('-'):
            return None
        if t.startswith('-') and not t.startswith('--') and 'c' in t[1:]:
            return j + 1 if j + 1 < len(toks) else None
    return None


# ---------------------------------------------------------------------------
# the two entry points the collector uses
# ---------------------------------------------------------------------------

def needs_argv(line):
    """Whether masking `line` needs its argument boundaries: it may run an agent."""
    return bool(line) and bool(_CLI_WORD.search(line))


def redact_argv(argv, keys=True, prompts=True, exact=True):
    """(command line as the collector writes it, sorted names of what was
    masked, or None) from a command line's separate arguments."""
    what = []
    if prompts:
        argv, fired = redact_prompt_argv(argv, 0, exact)
        if fired:
            what.append('prompt')
    line = ' '.join(argv).strip()
    if keys:
        line, names = redact_keys(line)
        what.extend(names)
    return line, (sorted(set(what)) or None)


def redact_line(line, keys=True, prompts=True):
    """The same from a joined command line, whose argument boundaries are lost:
    a prompt's words are then separate tokens, which the masking collapses back
    into one, and a shell's `-c` string is taken to run to the end of the line."""
    if not line:
        return line, None
    if prompts and _CLI_WORD.search(line):
        toks = line.split(' ')
        k = _shell_command_index(toks)
        if k is not None:
            toks = toks[:k] + [' '.join(toks[k:])]
        return redact_argv(toks, keys, prompts, exact=False)
    if keys:
        line, names = redact_keys(line)
        return line, (sorted(set(names)) or None)
    return line, None
