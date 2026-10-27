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
           model, subcommand and session names stay; each run of prompt text
           becomes one PROMPT_MASK.

What is masked is the VALUE, never the structure around it (the dashboard's
rule): the flag, the variable name and the subcommand stay, so a reader can
still tell what kind of thing was there and that it was removed. In a shell
command string only the masked words change; the rest is written as it was.

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

# A value that only refers to a secret is not one: `--token $HF_TOKEN`. Nor is
# a kwarg in an inline script that reads one: `token=os.environ["HF_TOKEN"]`.
_NOT_A_REF = r"(?!\$|\"\$|'\$)"
_NOT_CODE = r"(?!os\.|environ\b|getenv\(|Path\(|None\b|True\b|False\b|%s|\{\})"
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
    # Credential headers, inside -H "..." or anywhere: keep the header name and
    # scheme. `Bearer $TOKEN` names a secret without holding one.
    ("auth_header", (("authorization",),), re.compile(
        r"((?:Proxy-)?Authorization\s*:\s*(?:Bearer|Basic|Token|Digest|Bot)?\s*)" + _NOT_A_REF
        + r"(?!(?:Bearer|Basic|Token|Digest|Bot)\s)([^\s\"']+)", re.I),
     lambda m: m.group(1) + MASK),
    # Nor is the `:-` of a shell default a header: `${OPENAI_API_KEY:-}`.
    ("key_header", ((":",), ("api-key", "apikey", "api_key", "auth-token", "access-token",
                             "private-token", "secret")), re.compile(
        r"((?:X-[A-Za-z-]*)?(?:Api[-_]?Key|Auth-?Token|Access-?Token|Private-?Token|Secret)"
        r"\s*:(?![-+=?])\s*)" + _NOT_A_REF + r"([^\s\"']+)", re.I),
     lambda m: m.group(1) + MASK),
    # scheme://user:password@host -- keep the user; a long user alone is a token.
    # Anchored on the literal `://`, not on the scheme, which would be tried at
    # every character of the line.
    ("url_password", (("://",), ("@",)), re.compile(r"(://[^/\s:@\"']+:)([^@\s/\"']+)(@)"),
     lambda m: m.group(1) + MASK + m.group(3)),
    ("url_token_user", (("://",), ("@",)), re.compile(r"(://)([^/\s:@\"']{20,})(@)"),
     lambda m: m.group(1) + MASK + m.group(3)),
    # curl/wget -u user:password, --user user:password. Not a container's
    # `--user 1000:1000`, nor `date -u +%H:%M`.
    ("user_password", (("-u", "--user", "--proxy-user"), (":",)), re.compile(
        r"((?:^|\s)(?:-u|--user|--proxy-user)\s*)(?!\d+:\d+(?![^\s\"']))"
        r"([\w.@-][^\s:\"']*:)([^\s\"']+)"),
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
        r"(\b" + _SECRET_NAME + r"=)" + _NOT_A_REF + _NOT_CODE + _VALUE, re.I),
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
# left once the rest is accounted for. Its options, by kind:
#   prompt    the value is prompt text: masked
#   text      the value is free text that can carry instructions (a config
#             override, agent definitions, a cloud task's description): masked
#             when it reads as prose or is a JSON object
#   value     the value is a name, model, path, id, number or label: kept, spaces
#             and all (a session's name is not a prompt)
#   optional  a value only when the next argument is not an option (commander's
#             `[name]`)
#   many      one value or more (`--add-dir A B`): the arguments up to the next
#             option, while none of them reads as prose
#   flag      no value
#   stop      nothing after it is a prompt (Claude Code's own daemon modes)
# and its other arguments:
#   pos       a bare positional at the top level is prompt text
#   subs      first-positional subcommands: 'prompt' (prompt text follows),
#             'quiet' (no prompt anywhere after it), 'model' (one model name,
#             then prompt text); or (kind, {its own subcommands})
#   need      only a CLI when one of these subcommands follows: a generic name
# The claude and codex tables follow their own --help (Claude Code 2.1.309,
# codex-cli 0.162.1), plus the modes Claude Code's daemon starts it in.
#
# An argument no table accounts for is judged by its shape (_reads_as_text): one
# with whitespace in it, or a bare word, is text, masked wherever a prompt can
# stand; a path, URL, number, id, hyphenated name or key=value is kept. An option
# the table does not know takes the next argument as its value unless that one
# is prose, so the failure mode of an unknown option is one word too little.

def _cli(pos=True, prompt=(), text=(), value=(), optional=(), many=(), flags=(), stop=(),
         subs=None, need=None):
    kinds = {}
    for kind, names in (('flag', flags), ('value', value), ('optional', optional),
                        ('many', many), ('text', text), ('prompt', prompt), ('stop', stop)):
        kinds.update(dict.fromkeys(names, kind))
    return {'pos': pos, 'kinds': kinds,
            'takes': frozenset(k for k, v in kinds.items() if v not in ('flag', 'stop')),
            'subs': dict(subs or {}), 'need': frozenset(need or ())}


_CLAUDE_QUIET = ('agents', 'attach', 'auth', 'auto-mode', 'doctor', 'gateway', 'import',
                 'install', 'logs', 'mcp', 'plugin', 'plugins', 'purge', 'respawn', 'rm',
                 'setup-token', 'stop', 'kill', 'ultrareview', 'update', 'upgrade',
                 'remote-control', 'rc', 'config', 'migrate-installer', 'login', 'logout',
                 'serve', 'setup', 'daemon', 'bg-spare', 'bg-pty-host')
_CODEX_QUIET = ('agents', 'login', 'logout', 'mcp', 'mcp-server', 'plugin', 'app-server',
                'remote-control', 'completion', 'update', 'doctor', 'sandbox', 'debug', 'apply',
                'a', 'archive', 'delete', 'migrate-rollouts', 'unarchive', 'exec-server',
                'features', 'help', 'proto', 'responses-api-proxy', 'stdio-to-uds')
_CODEX_EXEC = ('prompt', {'resume': 'prompt', 'fork': 'prompt', 'review': 'prompt',
                          'help': 'quiet'})
CLIS = {
    'claude': _cli(
        prompt=('--append-system-prompt', '--system-prompt'),
        text=('--agents', '--cloud'),
        flags=('--allow-dangerously-skip-permissions', '--ax-screen-reader', '--bg',
               '--background', '--bare', '--brief', '--chrome', '--no-chrome', '-c',
               '--continue', '--dangerously-skip-permissions', '--desktop',
               '--disable-slash-commands', '--exclude-dynamic-system-prompt-sections',
               '--fork-session', '--forward-subagent-text', '-h', '--help', '--ide',
               '--include-hook-events', '--include-partial-messages',
               '--no-session-persistence', '-p', '--print', '--replay-user-messages',
               '--restricted', '--safe-mode', '--strict-mcp-config', '--tmux', '--verbose',
               '-v', '--version', '--debug-to-stderr', '--enable-auth-status',
               '--reply-on-resume', '--mcp-debug'),
        value=('--agent', '--autocompact', '--debug-file', '--effort', '--environment',
               '--fallback-model', '--input-format', '--json-schema', '--max-budget-usd',
               '--model', '-n', '--name', '--output-format', '--permission-mode',
               '--permission-prompts', '--plugin-dir', '--plugin-url',
               '--remote-control-session-name-prefix', '--session-id', '--setting-sources',
               '--settings', '--system-prompt-snapshot', '--system-prompt-file',
               '--append-system-prompt-file', '--permission-prompt-tool', '--max-turns',
               '--max-thinking-tokens', '--inherit-permission-mode', '--sdk-url',
               '--resume-session-at'),
        optional=('-r', '--resume', '--remote-control', '-d', '--debug', '--from-pr',
                  '--prompt-suggestions', '--teleport', '-w', '--worktree'),
        many=('--add-dir', '--allowedTools', '--allowed-tools', '--betas', '--disallowedTools',
              '--disallowed-tools', '--file', '--mcp-config', '--tools'),
        stop=('--bg-spare', '--bg-pty-host', '--preload'),
        subs=dict.fromkeys(_CLAUDE_QUIET, 'quiet')),
    'codex': _cli(
        prompt=('--message',),
        text=('-c', '--config'),
        flags=('--strict-config', '--oss', '--approve-for-me',
               '--dangerously-bypass-approvals-and-sandbox', '--dangerously-bypass-hook-trust',
               '--worktree', '--search', '--no-alt-screen', '--no-daemon',
               '--skip-git-repo-check', '--ephemeral', '--ignore-user-config', '--ignore-rules',
               '--json', '--last', '--all', '--include-non-interactive', '--uncommitted',
               '--full-auto', '--yolo', '--include-plan-tool', '-h', '--help', '-V',
               '--version'),
        value=('-m', '--model', '--enable', '--disable', '--remote', '--remote-auth-token-env',
               '--local-provider', '-p', '--profile', '-s', '--sandbox', '-C', '--cd',
               '--add-dir', '-a', '--ask-for-approval', '--thread-source',
               '--cyber-access-program', '--output-schema', '--color', '-o',
               '--output-last-message', '--base', '--commit', '--title', '--thread', '--env',
               '--attempts', '--branch'),
        many=('-i', '--image'),
        subs=dict(dict.fromkeys(_CODEX_QUIET, 'quiet'),
                  exec=_CODEX_EXEC, e=_CODEX_EXEC, review='prompt', resume='prompt',
                  fork='prompt', queue='prompt', cloud=('quiet', {'exec': 'prompt'}))),
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
    # its launcher runs node with its own index.js first; a worker serves the IDE
    'cursor-agent': _cli(
        flags=('--use-system-ca', '-p', '--print', '-f', '--force', '--verbose'),
        value=('-m', '--model', '--output-format', '-a', '--api-key', '--endpoint',
               '--worker-dir', '--name', '--label', '--worker-api-socket'),
        optional=('--resume',),
        subs=dict({'resume': 'prompt'},
                  **dict.fromkeys(('login', 'logout', 'status', 'update', 'upgrade', 'mcp', 'ls',
                                   'create-chat', 'install-shell-integration',
                                   'uninstall-shell-integration', 'worker', 'about', 'models',
                                   'help'), 'quiet'))),
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
    'screen': (frozenset(('-S', '-c', '-p', '-X', '-T', '-e', '-h', '-s', '-t',
                          '-Logfile')), 0),
    # `flock FILE cmd ...`; its `-c CMD` form is a command string (_TAKES_C)
    'flock': (frozenset(('-w', '--timeout', '-E', '--conflict-exit-code')), 1),
    'numactl': (frozenset(('-N', '--cpunodebind', '-m', '--membind', '-C', '--physcpubind',
                           '-i', '--interleave', '-p', '--preferred')), 0),
    'systemd-run': (frozenset(('-u', '--unit', '-p', '--property', '--slice', '-E', '--setenv',
                               '--uid', '--gid', '-M', '--machine', '--description',
                               '--working-directory', '--nice', '-H', '--host',
                               '--on-active', '--on-calendar', '--service-type')), 0),
    'nsenter': (frozenset(('-t', '--target', '-S', '--setuid', '-G', '--setgid', '-w', '--wd',
                           '-r', '--root')), 0),
    'unshare': (frozenset(('--map-user', '--map-group', '--propagation', '--setgroups')), 0),
    'unbuffer': (frozenset(), 0),
}
# a program and subcommand that run the command after them, (options, positionals)
_CONTAINER = frozenset(('-B', '--bind', '--mount', '--env', '--env-file', '-H', '--home',
                        '--pwd', '-W', '--workdir', '-o', '--overlay', '-S', '--scratch',
                        '--security', '--network', '--network-args', '--app', '--fusemount',
                        '--cwd'))
_ENVRUN = frozenset(('-n', '--name', '-p', '--prefix', '--cwd', '-e', '--environment',
                     '--manifest-path'))
_SUB_WRAPPERS = {
    ('apptainer', 'exec'): (_CONTAINER, 1), ('singularity', 'exec'): (_CONTAINER, 1),
    ('conda', 'run'): (_ENVRUN, 0), ('mamba', 'run'): (_ENVRUN, 0),
    ('micromamba', 'run'): (_ENVRUN, 0), ('pixi', 'run'): (_ENVRUN, 0),
    ('poetry', 'run'): (frozenset(), 0), ('pipenv', 'run'): (frozenset(), 0),
    ('pdm', 'run'): (frozenset(), 0), ('hatch', 'run'): (frozenset(), 0),
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


def _is_option(t):
    """An option, not text that starts with a dash: an option has no spaces
    (before any `=`), while a prompt written as a list (`- rotate the key`) does."""
    return (len(t) > 1 and t[0] == '-' and not t[1:2].isdigit()
            and not _PROSE.search(t.partition('=')[0]))


def _skip_options(toks, i, value_opts):
    """Index of the first argument at or after `i` that is not an option, nor
    the value of one. A cluster of short options (`-dmS name`) takes a value
    when its last letter does."""
    n = len(toks)
    while i < n:
        t = toks[i]
        if t == '--':
            return i + 1
        if not _is_option(t):
            return i
        if '=' in t:
            i += 1
            continue
        takes = t in value_opts or (t[1] != '-' and len(t) > 2 and '-' + t[-1] in value_opts)
        i += 2 if takes else 1
    return i


def _past_wrapper(toks, i):
    """Index of the command the wrapper at toks[i] runs (`timeout 600 X`,
    `apptainer exec img X`), or None when toks[i] is not a wrapper."""
    n, base = len(toks), _basename(toks[i])
    if base in _WRAPPERS:
        if base == 'flock' and any(t in ('-c', '--command') or t.startswith('--command=')
                                   for t in toks[i + 1:]):
            return None                       # a command string: _command_strings()
        opts, npos = _WRAPPERS[base]
        j = _skip_options(toks, i + 1, opts)
        if base == 'env':
            while j < n and _ENV_ASSIGN.match(toks[j]):
                j += 1
        return j + npos
    if i + 1 < n and (base, toks[i + 1]) in _SUB_WRAPPERS:
        opts, npos = _SUB_WRAPPERS[(base, toks[i + 1])]
        return _skip_options(toks, i + 2, opts) + npos
    return None


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
                j = _skip_options(toks, i + 1, spec['takes'])
                if j >= n or toks[j] not in spec['need']:
                    return None, None
            return i, name
        base = _basename(t)
        j = _past_wrapper(toks, i)
        if j is not None:
            i = j
            continue
        if _INTERPRETERS.match(base):
            j = i + 1
            while j < n and _is_option(toks[j]):
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
_ID = re.compile(r'(?:[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-'
                 r'[0-9a-fA-F]{12}|[0-9a-fA-F]{16,})\Z')
# What an argument that is not text looks like: a path, URL or slash command; a
# number, version, size or time; letters with a digit among them (an id, `v2`);
# a name joined by - _ . : @ or +, or in camelCase (`bg-spare`, `acceptEdits`);
# key=value; an npm scope; a run of hex; stdin's `-`; an empty argument.
_MACHINE = re.compile(
    r'(?:|-'
    r'|[0-9a-fA-F]{16,}'
    r'|\S*/\S*'
    r'|[-+]?\d\S*'
    r'|[^\W_]*\d[^\W_]*'
    r'|[^\W_]+(?:[-_.:@+][^\W_]+)+[-_.:]?'
    r'|[a-z]+(?:[A-Z][a-z0-9]*)+'
    r'|[\w.-]+=\S*'
    r'|@[\w.-]+'
    r')\Z')
# Small words a sentence has and a subcommand's arguments do not.
_SMALL_WORDS = frozenset((
    'a', 'an', 'the', 'to', 'of', 'and', 'or', 'but', 'in', 'on', 'at', 'by', 'for', 'with',
    'from', 'into', 'about', 'is', 'are', 'was', 'be', 'it', 'its', 'this', 'that', 'these',
    'those', 'my', 'your', 'our', 'me', 'you', 'we', 'i', 'please', 'can', 'could',
    'should', 'would', 'will', 'do', 'does', 'not', 'no', 'all', 'any', 'how', 'what', 'why',
    'when'))


def _reads_as_text(t):
    """Whether an argument reads as words rather than as a name, path, number
    or id: whitespace in it, or a bare word."""
    return bool(_PROSE.search(t)) or not _MACHINE.match(t)


def _text_value(v):
    """A `text` option's value that may carry instructions: prose, or a JSON
    object (agent definitions)."""
    return bool(_PROSE.search(v)) or v.lstrip()[:1] == '{'


def _sentence_follows(spec, toks, j):
    """Split on spaces, whether toks[j:] go on like a sentence: four words in a
    row, or two with a small word (`the`, `to`, `please`) among them. How
    `claude update the README` is told from `claude auth status --json`."""
    kinds = spec['kinds']
    words, small, k, n = 0, False, j, len(toks)
    while k < n:
        t = toks[k]
        if _is_option(t):
            words, small = 0, False
            name, eq, _v = t.partition('=')
            k += 2 if not eq and kinds.get(name) not in (None, 'flag', 'stop') else 1
            continue
        if _reads_as_text(t):
            words += 1
            small = small or t.lower().rstrip('.,;:!?') in _SMALL_WORDS
            if words >= 4 or (words >= 2 and small):
                return True
        else:
            words, small = 0, False
        k += 1
    return False


def _walk(spec, toks, exact=True):
    """Which of an agent CLI's arguments are prompt text: a list as long as
    `toks` holding None (kept), PROMPT_MASK, or the argument's new text
    (`--prompt=[REDACTED-PROMPT]`); and whether anything was masked.

    A run of positionals is masked whole when any of it reads as text, but for
    a session id ahead of it: a prompt given as several arguments keeps no path
    or name of its own. `exact` says the tokens are the real arguments. A line
    split on spaces is not: a prompt's words are separate tokens there, so a
    bare word is never taken for an unknown option's value, and a quiet
    subcommand counts only when no sentence follows it."""
    n = len(toks)
    res = [None] * n
    if exact and any(t in _NO_PROMPT_FLAGS for t in toks):
        return res, False                   # `codex exec --help`: a usage screen
    kinds = spec['kinds']
    pos_prompt, model_next = spec['pos'], False
    subs, parent = spec['subs'], None       # what the next positional may be
    run = []                                # the positionals since the last option

    def end_run():
        if any(_reads_as_text(toks[k]) for k in run):
            lead = 0
            while lead < len(run) and _ID.match(toks[run[lead]]):
                lead += 1                   # the session a prompt resumes: `resume ID ...`
            for k in run[lead:]:
                res[k] = PROMPT_MASK
        del run[:]

    def positional(k, t):
        if pos_prompt:
            run.append(k)
        elif _PROSE.search(t):
            res[k] = PROMPT_MASK            # text with spaces is prompt-like wherever it is

    i = 0
    while i < n:
        t = toks[i]
        if t == '--':
            end_run()
            for k in range(i + 1, n):
                positional(k, toks[k])
            break
        if _is_option(t):
            end_run()
            name, eq, val = t.partition('=')
            kind = kinds.get(name)
            if kind == 'stop':
                break
            if eq:
                if val and (kind == 'prompt' or (kind == 'text' and _text_value(val))
                            or (kind is None and _PROSE.search(val))):
                    res[i] = name + '=' + PROMPT_MASK
                i += 1
                continue
            if kind == 'flag' or i + 1 >= n:
                i += 1
                continue
            v = toks[i + 1]
            if kind is None:
                # An option this table does not know, then one argument: far
                # likelier its value (`--effort max`) than a prompt, unless it is
                # prose -- or, split on spaces, a word that may begin one. After a
                # short option only a name or number is taken: those are more
                # often switches.
                if not (_is_option(v) or v == '--' or _PROSE.search(v)
                        or ((not exact or name[1] != '-') and _reads_as_text(v))):
                    i += 2
                else:
                    i += 1
                continue
            if kind in ('optional', 'text') and (_is_option(v) or v == '--'):
                i += 1
                continue
            if kind == 'prompt' or (kind == 'text' and _text_value(v)):
                res[i + 1] = PROMPT_MASK
            i += 2
            if kind == 'many':
                while (i < n and not _is_option(toks[i]) and toks[i] != '--'
                       and not (_PROSE.search(toks[i]) if exact else _reads_as_text(toks[i]))):
                    i += 1
            continue
        # a positional
        sub = None
        if subs is not None:
            if not _PROSE.search(t):
                sub = subs.get(t)
            if sub is None:
                if parent == 'quiet':
                    break                   # a quiet subcommand's own argument
                if parent is None and '/' in t and not _PROSE.search(t):
                    i += 1                  # the launcher's own script (cursor-agent's
                    continue                # index.js); the subcommand may follow it
                subs, parent = None, None
        if sub is not None:
            kind, nested = sub if isinstance(sub, tuple) else (sub, None)
            if kind == 'quiet' and not nested:
                if exact or not _sentence_follows(spec, toks, i + 1):
                    break                   # no prompt anywhere after it
                # split on spaces: a prompt whose first word is a subcommand's name
                pos_prompt, subs = True, None
                run.append(i)
                i += 1
                continue
            if kind in ('prompt', 'model'):
                pos_prompt = True
            model_next = kind == 'model'
            subs, parent = nested, (kind if nested else None)
            i += 1
            continue
        if model_next:
            model_next = False
            if not _PROSE.search(t):
                i += 1
                continue
        positional(i, t)
        i += 1
    end_run()
    return res, any(r is not None for r in res)


_PLAIN = re.compile(r'[\w@%+=:,./\[\]-]*\Z')


def _quote(t):
    """`t` as a shell word: as it is when it needs no quotes (a mask needs
    none), else in single quotes."""
    return t if _PLAIN.match(t) else shlex.quote(t)


def _requote(raw, new):
    """`new`, written where the shell word `raw` stood: inside raw's own quotes
    when raw is one quoted string, else quoted only if it has to be."""
    if len(raw) > 1 and raw[0] == raw[-1] == "'" and "'" not in new:
        return "'" + new + "'"
    if len(raw) > 1 and raw[0] == raw[-1] == '"':
        return '"' + new.replace('\\', '\\\\').replace('"', '\\"') + '"'
    return _quote(new)


_OP_CHARS = frozenset('();<>|&`')


def _shell_tokens(s):
    """shell_split(), each token with where it stands in `s`: [(text, is
    operator, start, end)]."""
    out, cur = [], []
    start = [None]                        # where the word being read began
    i, n = 0, len(s)

    def flush(end):
        if start[0] is not None:
            out.append((''.join(cur), False, start[0], end))
            del cur[:]
            start[0] = None
    while i < n:
        c = s[i]
        if c == '\\':
            if i + 1 < n and s[i + 1] != '\n':
                if start[0] is None:
                    start[0] = i
                cur.append(s[i + 1])
            i += 2                                # a backslash-newline joins lines
        elif c == "'":
            j = s.find("'", i + 1)
            if j < 0:
                raise ValueError('unterminated quote')
            if start[0] is None:
                start[0] = i
            cur.append(s[i + 1:j])
            i = j + 1
        elif c == '"':
            if start[0] is None:
                start[0] = i
            i += 1
            while True:
                if i >= n:
                    raise ValueError('unterminated quote')
                ch = s[i]
                if ch == '"':
                    i += 1
                    break
                if ch == '\\' and i + 1 < n and s[i + 1] in '"\\$`\n':
                    if s[i + 1] != '\n':
                        cur.append(s[i + 1])
                    i += 2
                    continue
                cur.append(ch)
                i += 1
        elif c in ' \t\r':
            flush(i)
            i += 1
        elif c == '\n':
            flush(i)
            out.append(('\n', True, i, i + 1))
            i += 1
        elif c == '#' and start[0] is None:
            j = s.find('\n', i)
            i = n if j < 0 else j
        elif c in _OP_CHARS:
            flush(i)
            j = i
            while j < n and s[j] in _OP_CHARS:
                j += 1
            out.append((s[i:j], True, i, j))
            i = j
        else:
            if start[0] is None:
                start[0] = i
            cur.append(c)
            i += 1
    flush(min(i, n))
    return out


def shell_split(s):
    """[(text, is_operator)] for a shell command string, split as sh splits it:
    single and double quotes and backslashes as sh reads them; ; & | ( ) < >
    and backticks as operators wherever they stand, glued to a word or not; a
    newline as an operator too, since it ends a command; # starting a comment.
    Raises ValueError on an unterminated quote.

    Not shlex: before Python 3.8 its punctuation_chars do not split a word they
    are glued to under whitespace_split, so on EL8's 3.6 `cd /p; claude -p "x"`
    was one command, `cd`'s, and the prompt went out unmasked."""
    return [(t, op) for t, op, _a, _b in _shell_tokens(s)]


def _redact_shell(s, depth, exact=True):
    """A shell command string with every agent prompt in it masked; (str,
    masked?). Only the masked words change: the rest of the string, its quoting
    and spacing included, is written as it was."""
    if not _CLI_WORD.search(s):
        return s, False
    try:
        toks = _shell_tokens(s)
    except ValueError:
        # Not shell a lexer can follow -- a command line the kernel cut short, or
        # a heredoc of Python. An agent run at a command position in it is
        # masked from the agent's name on, since nothing after can be told apart
        # from prompt text; a name anywhere else (`--planner qwen`) is just text.
        m = _CLI_AT_COMMAND.search(s)
        if m is None:
            return s, False
        return s[:m.end(1)] + ' ' + PROMPT_MASK, True
    edits, cmd = [], []
    for t in toks + [None]:
        if t is not None and not t[1]:
            cmd.append(t)
            continue
        if cmd:
            res, _f = _mask_argv([w[0] for w in cmd], depth, exact)
            k = 0
            while k < len(cmd):
                if res[k] is None:
                    k += 1
                elif res[k] == PROMPT_MASK:
                    e = k
                    while e + 1 < len(cmd) and res[e + 1] == PROMPT_MASK:
                        e += 1
                    edits.append((cmd[k][2], cmd[e][3], PROMPT_MASK))
                    k = e + 1
                else:
                    a, b = cmd[k][2], cmd[k][3]
                    edits.append((a, b, _requote(s[a:b], res[k])))
                    k += 1
            cmd = []
    if not edits:
        return s, False
    out, at = [], 0
    for a, b, text in edits:
        out.append(s[at:a])
        out.append(text)
        at = b
    out.append(s[at:])
    return ''.join(out), True


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
        j = _past_wrapper(toks, i)
        if j is None:
            return i, via_ssh
        via_ssh = via_ssh or _basename(t) == 'ssh'
        i = j
    return None, via_ssh


# programs whose arguments are commands to run, each as one string
_RUNS_STRINGS = frozenset(('eval', 'watch', 'tmux', 'screen'))
_TAKES_C = frozenset(('su', 'runuser', 'script', 'flock'))   # -c CMD / --command=CMD


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
            if _is_option(t) and not t.startswith('--'):
                has_c = has_c or 'c' in t[1:]
                continue
            if _is_option(t):
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


def _mask_argv(argv, depth=0, exact=True):
    """Per argument, what masking `argv` changes (see _walk), and whether it
    changed anything: the agent run itself, then every command string an
    argument carries -- `bash -c '...'`, `sbatch --wrap='...'`, `tmux new
    'claude ...'` -- parsed as a shell would and handled the same way, three
    levels deep."""
    n = len(argv)
    res = [None] * n
    i, name = find_cli(argv)
    if name is not None:
        res[i + 1:] = _walk(CLIS[name], argv[i + 1:], exact)[0]
    if depth < 3:
        cur = [argv[j] if res[j] is None else res[j] for j in range(n)]
        for j, head in _command_strings(cur):
            if res[j] is not None or not argv[j].startswith(head):
                continue
            red, f = _redact_shell(argv[j][len(head):], depth + 1, exact)
            if f:
                res[j] = head + red
    return res, any(r is not None for r in res)


def redact_prompt_argv(argv, depth=0, exact=True):
    """(argv with every agent prompt in it masked, masked?): each run of
    masked arguments is one PROMPT_MASK."""
    res, fired = _mask_argv(argv, depth, exact)
    out = []
    for t, r in zip(argv, res):
        if r is None:
            out.append(t)
        elif r != PROMPT_MASK or not out or out[-1] != PROMPT_MASK:
            out.append(r)
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
# the setting, and the entry points: the tracer's two and the node tier's
# ---------------------------------------------------------------------------

BOTH = frozenset(('keys', 'prompts'))
_KIND_NAMES = {'key': 'keys', 'keys': 'keys', 'credential': 'keys', 'credentials': 'keys',
               'secret': 'keys', 'secrets': 'keys', 'prompt': 'prompts', 'prompts': 'prompts'}


def parse_kinds(value):
    """EBPFM_REDACT -> (the kinds masked, a complaint or None). Unset or empty
    means both; '0', 'none' or 'off' alone means neither. A value this cannot
    read also means both: a typo (`prompt` for `prompts`) must not be how keys
    get written out."""
    if value is None or not value.strip():
        return BOTH, None
    toks = {k.strip().lower() for k in value.split(',') if k.strip()}
    if toks <= {'0', 'none', 'off'}:
        return frozenset(), None
    bad = sorted(toks - set(_KIND_NAMES))
    if bad:
        return BOTH, ('EBPFM_REDACT=%r: cannot read %s; masking keys and prompts'
                      % (value, ', '.join(bad)))
    return frozenset(_KIND_NAMES[k] for k in toks), None


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


_CONTROL = re.compile(r'[\x00-\x1f\x7f]')
_SPELLING = re.compile(r'[\x00-\x20\x7f?]+')


def _proc_argv(pid, line):
    """pid's arguments from /proc/<pid>/cmdline when they join to `line` -- the
    same process, still running the same command -- else None. ps prints a
    newline or a tab inside an argument as a space or a ?, so neither counts."""
    try:
        with open('/proc/%s/cmdline' % pid, 'rb') as f:
            raw = f.read().rstrip(b'\0')
    except Exception:
        return None
    if not raw:
        return None
    argv = [a.decode('utf-8', 'replace') for a in raw.split(b'\0')]
    if _SPELLING.sub('', ' '.join(argv)) != _SPELLING.sub('', line):
        return None
    return argv


def redact_ps_line(line, pid, keys=True, prompts=True):
    """The same for the line `ps` printed for `pid`, as the node tier has it:
    masked from the process's own arguments while /proc still has them -- ps
    joins them with spaces, and a prompt is one argument, spaces and all, as is
    a session's name beside it -- else from the joined line."""
    argv = _proc_argv(pid, line) if line and prompts and needs_argv(line) else None
    if argv is None:
        return redact_line(line, keys, prompts)
    red, what = redact_argv(argv, keys, prompts)
    return _CONTROL.sub(' ', red), what
