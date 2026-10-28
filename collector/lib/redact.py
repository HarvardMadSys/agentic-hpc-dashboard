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
command string only the masked words change, and a comment beside a masked
prompt; the rest is written as it was, unless a credential in it can only be
read with its quoting taken off (_redact_shell).

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
# a kwarg in an inline script that reads one, `token=os.environ["HF_TOKEN"]`,
# or is a placeholder, `password=None)`; `password=None-of-yours` still is.
_NOT_A_REF = r"(?!\$|\"\$|'\$)"
_NOT_CODE = (r"(?!(?:os\.)?(?:environ\b|getenv\()|Path\("
             r"|(?:None|True|False|%s|\{\})(?=[\s)\],;}\"']|$))")
_VALUE = r"(\\?\"[^\"]*\"|\\?'[^']*'|[^\s\"']+)"

# Names that hold a credential, as an env var, a kwarg or a query key. Matched
# on the END of the name: `HF_TOKEN`, `db_password`, `AWS_SECRET_ACCESS_KEY`.
# `max_tokens` ends in an S and is left alone; `PWD` alone is the cwd.
_SECRET_NAME = (r"[A-Za-z0-9_]*(?:TOKEN|SECRET|PASSWORD|PASSWD|PASSPHRASE|_PASS|_PWD|"
                r"API_?KEY|ACCESS_?KEY|SECRET_?KEY|PRIVATE_?KEY|AUTH_?KEY|CREDENTIALS?)")

_NAME_WORDS = ("token", "secret", "password", "passwd", "passphrase", "_pass", "_pwd", "key",
               "credential")

# A container's `--user 1000:1000` is a uid and a gid, not a user and password:
# docker's, podman's, a wrapper of theirs (`podman-hpc`, a test's
# `fake_podman.py`), lines continued with a backslash and all.
_UID_GID = re.compile(r"\d+:\d+\Z")
_CONTAINER_RUN = re.compile(r"(?:^|[\s/;&|(_-])(?:docker|podman|nerdctl|buildah|enroot|kubectl|"
                            r"crictl|ctr)\b(?:\\\n|[^;&|\n])*\Z")


def _user_password(m):
    """`-u user:` kept and the password masked, but for a container's uid:gid."""
    if _UID_GID.match(m.group(2) + m.group(3)) and _CONTAINER_RUN.search(m.string, 0, m.start()):
        return m.group(0)
    return m.group(1) + m.group(2) + MASK


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
    # Nor is a shell default that is empty or another variable a header --
    # `${OPENAI_API_KEY:-}`, `${OPENAI_API_KEY:-$KEY}`, `${OPENAI_API_KEY:?unset}`
    # -- while a default written out is the key itself.
    ("key_header", ((":",), ("api-key", "apikey", "api_key", "auth-token", "access-token",
                             "private-token", "secret")), re.compile(
        r"((?:X-[A-Za-z-]*)?(?:Api[-_]?Key|Auth-?Token|Access-?Token|Private-?Token|Secret)"
        r"\s*:(?![-=+]?\}|[-=+][\"']?\$|\?)\s*)" + _NOT_A_REF + r"([^\s\"']+)", re.I),
     lambda m: m.group(1) + MASK),
    # scheme://user:password@host -- keep the user; a long user alone is a token.
    # Anchored on the literal `://`, not on the scheme, which would be tried at
    # every character of the line.
    ("url_password", (("://",), ("@",)), re.compile(r"(://[^/\s:@\"']+:)([^@\s/\"']+)(@)"),
     lambda m: m.group(1) + MASK + m.group(3)),
    ("url_token_user", (("://",), ("@",)), re.compile(r"(://)([^/\s:@\"']{20,})(@)"),
     lambda m: m.group(1) + MASK + m.group(3)),
    # curl/wget -u user:password, --user=user:password, quoted or not, `$USER:`
    # too. Not `date -u +%H:%M`, numpy's `-u[:, 0]`, a container's `--user
    # 1000:1000` (_user_password), nor a password that is a variable's.
    ("user_password", (("-u", "--user", "--proxy-user"), (":",)), re.compile(
        r"((?:^|\s)(?:-u|--user|--proxy-user)(?:=|\s*)[\"']?)(?![+\[])([^\s:\"']+:)(?!\$)"
        r"([^\s\"']+)"),
     _user_password),
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
    # Credential headers as JSON or TOML keys, escaped or not: an MCP server's
    # `"headers": {"Authorization": "Bearer ..."}`, codex's `http_headers=
    # {"X-Api-Key"="..."}`.
    ("header_json", (("\"", "'"), ("authorization", "api-key", "apikey", "api_key", "auth-token",
                                   "access-token", "private-token")), re.compile(
        r"(\\?[\"'](?:(?:Proxy-)?Authorization\\?[\"']\s*[:=]\s*\\?[\"']"
        r"(?:(?:Bearer|Basic|Token|Digest|Bot)\s+)?"
        r"|(?:X-[A-Za-z-]*)?(?:Api[-_]?Key|Auth-?Token|Access-?Token|Private-?Token)"
        r"\\?[\"']\s*[:=]\s*\\?[\"']))(?!\$)([^\s\"'\\]+)", re.I),
     lambda m: m.group(1) + MASK),
    # A credential flag and its value as items of a JSON list: an MCP server's
    # `"args": ["--api-key", "..."]`.
    ("secret_flag_json", (("\"", "'"), _NAME_WORDS), re.compile(
        r"(\\?[\"']--?[A-Za-z0-9-]*(?:token|secret|password|passwd|passphrase|api-?key|apikey|"
        r"access-?key|private-?key|auth-?key|credentials?)\\?[\"']\s*,\s*\\?[\"'])(?!\$)"
        r"([^\"'\\]+)", re.I),
     lambda m: m.group(1) + MASK),
    # ?access_token=...&key=...: query-string credentials, including signed URLs
    # and EOS's `?authz=`.
    ("query_secret", (("?", "&"), ("=",)), re.compile(
        r"([?&](?:access_token|refresh_token|id_token|token|authz|api_?key|key|secret|password|"
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
                      "hf_", "akia", "asia", "xox", "aiza", "npm_", "eyj", "zteos64"),), re.compile(
        r"\b(?:sk-(?:ant-|proj-)?[A-Za-z0-9_\-]{20,}"
        r"|zteos64:[A-Za-z0-9+/=_%\-]{20,}"
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
#   value     the value is a name, model, path, id, number or label: kept, unless
#             it reads as words where a name should stand -- a sentence after
#             codex's -p, which is --profile, is a prompt typed from habit
#   optional  a value only when the next argument is not an option (commander's
#             `[name]`)
#   many      one value or more (`--add-dir A B`): the arguments up to the next
#             option, while none of them reads as prose
#   flag      no value
#   stop      nothing after it is a prompt (Claude Code's own daemon modes)
# and its other arguments:
#   pos       a bare positional at the top level is prompt text
#   subs      first-positional subcommands: 'prompt' (prompt text follows),
#             'quiet' (no prompt after it: names, commands), 'admin' (no prompt
#             and no word of its own after it), 'model' (one name -- a model, a
#             session -- then prompt text); or (kind, {its own subcommands})
#   need      only a CLI when one of these subcommands follows: a generic name
#   names     value options that name a session: kept whatever they read like
#   prints    flags that ask for print mode, which takes a prompt
#   hosts     a mode whose arguments after `--` are a command line of their own
# The claude and codex tables follow their own --help (Claude Code 2.1.309,
# codex-cli 0.162.1), plus the modes Claude Code's daemon starts it in and the
# prompt options its binary has but does not list.
#
# An argument no table accounts for is judged by its shape (_reads_as_text): one
# with whitespace in it, a bare word, or text in a script other than Latin is
# text; a path, URL, number, id, hyphenated name or key=value is not. Where a
# prompt can stand, only a path, a session id and `-` are kept (_prompt_word). An
# option the table does not know takes the next argument as its value unless
# that one is text, so the failure mode of an unknown option is one word too
# little.

def _cli(pos=True, prompt=(), text=(), value=(), optional=(), many=(), flags=(), stop=(),
         subs=None, need=None, names=(), prints=(), hosts=()):
    kinds = {}
    for kind, opts in (('flag', flags), ('value', value), ('optional', optional),
                       ('many', many), ('text', text), ('prompt', prompt), ('stop', stop)):
        kinds.update(dict.fromkeys(opts, kind))
    return {'pos': pos, 'kinds': kinds,
            'takes': frozenset(k for k, v in kinds.items() if v not in ('flag', 'stop')),
            'subs': dict(subs or {}), 'need': frozenset(need or ()), 'names': frozenset(names),
            'prints': frozenset(prints), 'hosts': frozenset(hosts)}


def _words(*names):
    """A subcommand's own subcommands, none of which takes a prompt."""
    return dict.fromkeys(names, 'quiet')


# Claude Code's subcommands, 2.1.309's and older ones' (`config`, `setup`,
# `login`, ...), none of which takes a prompt: an 'admin' one has no word of
# its own after it, a 'quiet' one a name (`attach <id|name>`), and the rest
# their own subcommands.
_CLAUDE_SUBS = dict.fromkeys(('agents', 'doctor', 'gateway', 'purge', 'setup-token', 'update',
                              'upgrade', 'remote-control', 'rc', 'migrate-installer', 'login',
                              'logout', 'serve', 'setup', 'bg-spare', 'bg-pty-host'), 'admin')
_CLAUDE_SUBS.update(dict.fromkeys(('attach', 'logs', 'stop', 'kill', 'rm', 'respawn', 'import',
                                   'ultrareview'), 'quiet'))
_PLUGIN = ('quiet', _words('configure', 'details', 'disable', 'enable', 'eval', 'help', 'init',
                           'new', 'install', 'i', 'list', 'marketplace', 'prune', 'autoremove',
                           'tag', 'test', 'uninstall', 'remove', 'update', 'validate'))
_CLAUDE_SUBS.update({
    'auth': ('quiet', _words('help', 'login', 'logout', 'status')),
    'auto-mode': ('quiet', _words('config', 'critique', 'defaults', 'help', 'reset')),
    'mcp': ('quiet', _words('add', 'add-from-claude-desktop', 'add-json', 'get', 'help', 'list',
                            'login', 'logout', 'remove', 'reset-project-choices', 'serve')),
    'plugin': _PLUGIN, 'plugins': _PLUGIN,
    'daemon': ('quiet', _words('run', 'start', 'status', 'logs', 'uninstall', 'stop')),
    'install': ('quiet', _words('stable', 'latest')),
    'config': ('quiet', _words('get', 'set', 'remove', 'rm', 'list', 'ls', 'add'))})
# `resume` and `fork` take a session first, an id or a thread's name
_CODEX_EXEC = ('prompt', {'resume': 'model', 'fork': 'model', 'review': 'prompt',
                          'help': 'quiet'})
_CODEX_SUBS = dict.fromkeys(('agents', 'logout', 'mcp-server', 'update', 'doctor', 'apply', 'a',
                             'migrate-rollouts', 'proto', 'responses-api-proxy', 'stdio-to-uds'),
                            'admin')
_CODEX_SUBS.update(dict.fromkeys(('archive', 'delete', 'unarchive', 'help', 'sandbox'), 'quiet'))
_CODEX_SUBS.update({
    'login': ('quiet', _words('status', 'help')),
    'mcp': ('quiet', _words('list', 'get', 'add', 'remove', 'login', 'logout', 'help')),
    'plugin': ('quiet', _words('add', 'list', 'marketplace', 'remove', 'help')),
    'app-server': ('quiet', _words('daemon', 'proxy', 'generate-ts', 'generate-json-schema',
                                   'help')),
    'remote-control': ('quiet', _words('start', 'stop', 'pair', 'help')),
    'completion': ('quiet', _words('bash', 'zsh', 'fish', 'powershell', 'elvish')),
    'debug': ('quiet', _words('models', 'app-server', 'prompt-input', 'help')),
    'exec-server': ('quiet', _words('forward', 'help')),
    'features': ('quiet', _words('list', 'enable', 'disable', 'help')),
    'cloud': ('quiet', dict(_words('status', 'list', 'apply', 'diff', 'help'), exec='prompt')),
    'exec': _CODEX_EXEC, 'e': _CODEX_EXEC, 'review': 'prompt', 'resume': 'model',
    'fork': 'model', 'queue': 'prompt'})
CLIS = {
    'claude': _cli(
        prompt=('--append-system-prompt', '--system-prompt', '--append-subagent-system-prompt',
                '--plan-mode-instructions', '--prefill', '--prefill-b64'),
        text=('--agents', '--cloud', '--remote'),
        flags=('--allow-dangerously-skip-permissions', '--ax-screen-reader', '--bg',
               '--background', '--bare', '--brief', '--chrome', '--no-chrome', '-c',
               '--continue', '--dangerously-skip-permissions', '--desktop',
               '--disable-slash-commands', '--exclude-dynamic-system-prompt-sections',
               '--fork-session', '--forward-subagent-text', '-h', '--help', '--ide',
               '--include-hook-events', '--include-partial-messages',
               '--no-session-persistence', '-p', '--print', '--replay-user-messages',
               '--restricted', '--safe-mode', '--strict-mcp-config', '--tmux', '--verbose',
               '-v', '--version', '--debug-to-stderr', '--enable-auth-status',
               '--reply-on-resume', '--mcp-debug', '--deep-link-origin'),
        value=('--agent', '--autocompact', '--debug-file', '--effort', '--environment',
               '--fallback-model', '--input-format', '--json-schema', '--max-budget-usd',
               '--model', '-n', '--name', '--output-format', '--permission-mode',
               '--permission-prompts', '--plugin-dir', '--plugin-url',
               '--remote-control-session-name-prefix', '--session-id', '--setting-sources',
               '--settings', '--system-prompt-snapshot', '--system-prompt-file',
               '--append-system-prompt-file', '--permission-prompt-tool', '--max-turns',
               '--max-thinking-tokens', '--inherit-permission-mode', '--sdk-url',
               '--resume-session-at', '--append-subagent-system-prompt-file'),
        optional=('-r', '--resume', '--remote-control', '-d', '--debug', '--from-pr',
                  '--prompt-suggestions', '--teleport', '-w', '--worktree'),
        many=('--add-dir', '--allowedTools', '--allowed-tools', '--betas', '--disallowedTools',
              '--disallowed-tools', '--file', '--mcp-config', '--tools'),
        stop=('--bg-spare', '--bg-pty-host', '--preload'),
        subs=_CLAUDE_SUBS,
        names=('-n', '--name', '-r', '--resume', '--remote-control',
               '--remote-control-session-name-prefix'),
        prints=('-p', '--print'), hosts=('bg-pty-host', '--bg-pty-host')),
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
        subs=_CODEX_SUBS, names=('--title',)),
    # the Codex plugin for Claude Code, run by node: `codex-companion.mjs task
    # --model M --effort E "<prompt>"`
    'codex-companion': _cli(pos=False, value=('-m', '--model', '--effort', '--cwd'),
                            subs={'task': 'prompt'}),
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
    'opencode': _cli(pos=False, names=('-s', '--session', '--title'),
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
        optional=('--resume',), names=('--name', '--label', '--resume'),
        prints=('-p', '--print'),
        subs=dict({'resume': 'prompt'},
                  **dict.fromkeys(('login', 'logout', 'status', 'update', 'upgrade', 'mcp', 'ls',
                                   'create-chat', 'install-shell-integration',
                                   'uninstall-shell-integration', 'worker', 'about', 'models',
                                   'help'), 'quiet'))),
    'goose': _cli(pos=False, prompt=('-t', '--text'), names=('-n', '--name', '-r', '--resume'),
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
    'copilot': _cli(prompt=('-p', '--prompt'), names=('--resume',),
                    value=('--model', '--allow-tool', '--deny-tool', '--add-dir',
                           '--log-dir', '--log-level', '--resume', '--agent'),
                    subs=dict.fromkeys(('help', 'version', 'login', 'logout'), 'quiet')),
    'cn': _cli(prompt=('-p', '--print'), value=('--config', '--model', '--rule', '--mcp',
                                                '--org', '--resume', '--format'),
               names=('--resume',)),
    'droid': _cli(value=('-m', '--model', '-o', '--output-format', '--cwd', '--auto',
                         '-r', '--reasoning-effort', '-s', '--session-id', '-f', '--file'),
                  subs={'exec': 'prompt'}),
    'llm': _cli(prompt=('-s', '--system'),
                value=('-m', '--model', '-t', '--template', '-a', '--attachment', '--at',
                       '-f', '--fragment', '--sf', '--system-fragment', '-T', '--tool',
                       '--cid', '--conversation-id', '--key', '-d', '--database'),
                names=('--cid', '--conversation-id'),
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
                     'qwen-code': 'qwen', 'opencode-ai': 'opencode', 'codex.node-native': 'codex',
                     'codex-companion.mjs': 'codex-companion'})
# Claude Code's native binary under the names it is installed as: the npm
# package's `claude.exe`, and with its version on (`claude.exe.2.1.281`,
# `claude-2.1.295`).
_CLAUDE_BINARY_NAME = r'claude(?:\.exe)?(?:[.-]\d+(?:\.\d+)*)?'
_CLAUDE_BINARY = re.compile(_CLAUDE_BINARY_NAME + r'\Z')
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
# this module masks. `.claude/` paths do not pass it (the dot), nor the
# `/tmp/claude-7cb5-cwd` file Claude Code's own tool commands write, so those,
# which mention ~/.claude on every call, stay cheap; `claude-2.1.295` does.
_CLI_WORD = re.compile(
    r'(?<![\w.\-])(?:' + _CLAUDE_BINARY_NAME + '|'
    + '|'.join(sorted((re.escape(n) for n in set(_BY_BASENAME) | set(_PACKAGES)
                       | {'claude-code', 'gemini-cli', 'qwen-code'}), key=len, reverse=True))
    + r')(?![\w\-])')
# a backslash escape, as $'...' writes a newline (`$'cd x\nclaude -p ...'`),
# however many times over it is escaped
_ESCAPE = re.compile(r'\\+[ntrvfabeE]?')
_QUOTING = re.compile(r'[\'"\\]')


def _names_cli(text):
    """The gate: whether `text` names an agent, read as a shell reads it: an
    escape (`\\nclaude`) as the space it stands for, and a name quoted in
    pieces (`clau'd'e`) whole."""
    if _CLI_WORD.search(text):
        return True
    if '\\' not in text and "'" not in text and '"' not in text:
        return False
    return bool(_CLI_WORD.search(_ESCAPE.sub(' ', text))
                or _CLI_WORD.search(_QUOTING.sub('', text)))

# The same names where a command starts -- the start, or after ; & | ( { ` $(
# or a newline, and a reserved word (`do`, `then`) -- past env assignments and
# the commonest wrappers. Used only on a string the lexer could not split.
_GENERIC = ('q', 'kiro-cli')
_CLI_AT_COMMAND = re.compile(
    r'(?:^|[;&|\n({`]|\$\()\s*(?:(?:do|then|else|elif|if|while|until|!)\s+)*'
    r'(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)*'
    r'(?:(?:sudo|nohup|exec|time|env|setsid|timeout\s+\S+|nice\s+-n\s*\S+)\s+)*'
    r'(?:\S*/)?('
    + '|'.join(sorted((re.escape(n) for n in _BY_BASENAME if n not in _GENERIC),
                      key=len, reverse=True))
    + r'|' + _CLAUDE_BINARY_NAME
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
_SHELL_VALUE = frozenset(('-o', '-O', '--rcfile', '--init-file'))
_ENV_ASSIGN = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*=')
_PROSE = re.compile(r'\s')
_FOREIGN = re.compile(r'[^\x00-\x7f]')
# where a path or a URL starts
_PATHLIKE = re.compile(r'(?:~?/|\.\.?/|[A-Za-z][\w+.-]*://)\S*\Z')


def _basename(tok):
    return tok.rsplit('/', 1)[-1]


def cli_of(tok):
    """The agent CLI `tok` runs, as a program or an interpreter's script, or None."""
    base = _basename(tok)
    name = _BY_BASENAME.get(base)
    if name is not None:
        return name
    if _CLAUDE_BINARY.match(base):
        return 'claude'
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
        if base in _SHELLS:
            # `bash SCRIPT ...`: a wrapper script, which is how the kernel sees a
            # `#!/bin/bash` one run. A -c string is _command_strings()' to read.
            j = i + 1
            while j < n and _is_option(toks[j]):
                o = toks[j]
                if not o.startswith('--') and 'c' in o[1:]:
                    return None, None
                j += 2 if o in _SHELL_VALUE else 1
            if j < n:
                name = cli_of(toks[j])
                if name is not None:
                    return j, name
            return None, None
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


def _foreign_text(t):
    """Text in a script other than Latin, outside a path or URL. Chinese and
    Japanese are written without spaces, so a prompt in them can be one word
    with a digit in it (`修复第3个测试`), which would read as an id."""
    return bool(_FOREIGN.search(t)) and not _PATHLIKE.match(t)


def _reads_as_text(t):
    """Whether an argument reads as words rather than as a name, path, number
    or id: whitespace in it, a bare word, or text in a script other than Latin."""
    return bool(_PROSE.search(t)) or _foreign_text(t) or not _MACHINE.match(t)


def _prompt_word(t):
    """Whether an argument where a prompt can stand is prompt text: anything but
    a path, URL or slash command, a session id, stdin's `-` or an empty one.
    A version, a snake_case word or a number may be the whole of a prompt."""
    if _PROSE.search(t) or _foreign_text(t):
        return True
    return not (t in ('', '-') or '/' in t or _ID.match(t))


def _text_value(v):
    """A `text` option's value that may carry instructions: prose, a JSON
    object (agent definitions), or text in a script other than Latin."""
    return bool(_PROSE.search(v)) or v.lstrip()[:1] == '{' or _foreign_text(v)


def _wordy(v):
    """A `value` option's value that reads as words where a name, number or
    path should stand: a prompt typed after the wrong flag. JSON (settings, a
    schema) is not words."""
    return v.lstrip()[:1] not in ('{', '[') and (bool(_PROSE.search(v)) or _foreign_text(v))


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


_BARE = {'kinds': {}}


def _says(t):
    """Whether one argument is a sentence rather than a name: four words, or
    two with a small word among them (`focus on the auth changes`, not `Remote
    Dev (FASRC)`), or text in a script other than Latin. JSON is a config."""
    if t.lstrip()[:1] in ('{', '['):
        return False
    return _foreign_text(t) or (bool(_PROSE.search(t)) and _sentence_follows(_BARE, t.split(), 0))


def _a_prompt_begins(spec, kind, nested, toks, i, exact, printed):
    """Whether toks[i], the name of a subcommand that takes no prompt, is the
    first word of a prompt instead: a sentence follows it (`claude update the
    README`). Split on spaces, where a prompt's words are separate, so does
    print mode, which asks for a prompt (`claude -p update deps`), and a word
    the subcommand does not take (`claude install pytorch nightly`, `codex a
    quick fix`); a name, path, id or version it may. What follows a `--` is
    another command's (`claude mcp add srv -- npx ...`, a host mode's claude)."""
    end = toks.index('--', i + 1) if '--' in toks[i + 1:] else len(toks)
    if _sentence_follows(spec, toks[:end], i + 1):
        return True
    if exact:
        return False
    if printed:
        return True
    nxt = toks[i + 1] if i + 1 < len(toks) else None
    if nxt is None or _is_option(nxt) or nxt == '--' or not _reads_as_text(nxt):
        return False
    if nested is not None:
        return nxt not in nested
    return kind == 'admin'


def _mask_sayings(spec, toks, i, res):
    """After a subcommand that takes no prompt, from toks[i] on, with the real
    arguments in hand: what is said in a sentence is still masked (`claude
    ultrareview 'focus on the auth changes'`); names, paths and ids stay, and a
    session's name whatever it reads like."""
    n = len(toks)
    while i < n:
        t = toks[i]
        if _is_option(t):
            name, eq, val = t.partition('=')
            if name in spec['names']:
                i += 1 if eq else 2
                continue
            if eq and _says(val):
                res[i] = name + '=' + PROMPT_MASK
        elif _says(t):
            res[i] = PROMPT_MASK
        i += 1


def _walk(spec, toks, exact=True):
    """Which of an agent CLI's arguments are prompt text: a list as long as
    `toks` holding None (kept), PROMPT_MASK, or the argument's new text
    (`--prompt=[REDACTED-PROMPT]`); and whether anything was masked.

    A run of positionals is masked whole unless every one of them is a path, a
    session id or `-`, but for a session id ahead of it: a prompt given as
    several arguments keeps no path or name of its own. `exact` says the tokens
    are the real arguments. A line split on spaces is not: a prompt's words are
    separate tokens there, so a bare word is never taken for an unknown option's
    value, nor anything once a prompt has begun (`-k name` inside one), a
    subcommand's name counts only when what follows is that subcommand's, and a
    second value of `--add-dir A B` only when no prompt's words follow it."""
    n = len(toks)
    res = [None] * n
    if exact and any(t in _NO_PROMPT_FLAGS for t in toks):
        return res, False                   # `codex exec --help`: a usage screen
    kinds, names = spec['kinds'], spec['names']
    pos_prompt, model_next, printed, said = spec['pos'], False, False, False
    subs, parent = spec['subs'], None       # what the next positional may be
    run = []                                # the positionals since the last option

    def end_run():
        # Split on spaces, a run may be the rest of a session's name (`--name
        # paper co-reader`): only what reads as text masks it there.
        masked = any((_prompt_word if exact else _reads_as_text)(toks[k]) for k in run)
        if masked:
            lead = 0
            while lead < len(run) and _ID.match(toks[run[lead]]):
                lead += 1                   # the session a prompt resumes: `resume ID ...`
            for k in run[lead:]:
                res[k] = PROMPT_MASK
        del run[:]
        return masked

    def positional(k, t):
        if pos_prompt:
            run.append(k)
        elif _PROSE.search(t) or _foreign_text(t):
            res[k] = PROMPT_MASK            # text is prompt-like wherever it is

    i = 0
    while i < n:
        t = toks[i]
        if t == '--':
            end_run()
            for k in range(i + 1, n):
                positional(k, toks[k])
            break
        if _is_option(t):
            said = end_run() or said
            name, eq, val = t.partition('=')
            kind = kinds.get(name)
            if kind == 'stop':
                if exact:
                    _mask_sayings(spec, toks, i + 1, res)
                break
            if eq:
                if val and (kind == 'prompt' or (kind == 'text' and _text_value(val))
                            or (kind in ('value', 'optional') and name not in names
                                and _wordy(val))
                            or (kind is None and (_PROSE.search(val) or _foreign_text(val)))):
                    res[i] = name + '=' + PROMPT_MASK
                i += 1
                continue
            if kind == 'flag' or i + 1 >= n:
                printed = printed or name in spec['prints']
                i += 1
                continue
            v = toks[i + 1]
            if kind is None:
                # An option this table does not know, then one argument: far
                # likelier its value (`--effort max`) than a prompt, unless it is
                # text -- or, split on spaces, a word that may begin one, or any
                # word once a prompt has begun. After a short option only a name
                # or number is taken: those are more often switches.
                if not (_is_option(v) or v == '--' or _PROSE.search(v) or _foreign_text(v)
                        or (not exact and said)
                        or ((not exact or name[1] != '-') and _reads_as_text(v))):
                    i += 2
                else:
                    i += 1
                continue
            if kind in ('optional', 'text') and (_is_option(v) or v == '--'):
                i += 1
                continue
            if (kind == 'prompt' or (kind == 'text' and _text_value(v))
                    or (kind in ('value', 'optional') and name not in names and _wordy(v))):
                res[i + 1] = PROMPT_MASK
            i += 2
            if kind == 'many':
                first = i
                while (i < n and not _is_option(toks[i]) and toks[i] != '--'
                       and not (_PROSE.search(toks[i]) or _foreign_text(toks[i]) if exact
                                else _reads_as_text(toks[i]))):
                    i += 1
                if not exact and first < i < n and not _is_option(toks[i]) and toks[i] != '--':
                    i = first               # split on spaces, they may be a prompt's words
                    subs, parent = None, None
            continue
        # a positional
        sub = None
        if subs is not None:
            if not _PROSE.search(t):
                sub = subs.get(t)
            if sub is None:
                if parent == 'quiet':
                    if exact:
                        _mask_sayings(spec, toks, i, res)
                    break                   # a quiet subcommand's own argument
                if parent is None and '/' in t and not (_PROSE.search(t) or _foreign_text(t)):
                    i += 1                  # the launcher's own script (cursor-agent's
                    continue                # index.js); the subcommand may follow it
                subs, parent = None, None
        if sub is not None:
            kind, nested = sub if isinstance(sub, tuple) else (sub, None)
            if kind in ('quiet', 'admin'):
                if _a_prompt_begins(spec, kind, nested, toks, i, exact, printed):
                    # a prompt whose first word is a subcommand's name
                    pos_prompt, subs, parent = True, None, None
                    run.append(i)
                    i += 1
                    continue
                if nested:
                    subs, parent = nested, 'quiet'
                    i += 1
                    continue
                if exact:
                    _mask_sayings(spec, toks, i + 1, res)
                break                       # no prompt anywhere after it
            if kind in ('prompt', 'model'):
                pos_prompt = True
            model_next = kind == 'model'
            subs, parent = nested, (kind if nested else None)
            i += 1
            continue
        if model_next:
            model_next = False
            if not (_PROSE.search(t) or _foreign_text(t)):
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
_ANSI_C = {'n': '\n', 't': '\t', 'r': '\r', 'a': '\a', 'b': '\b', 'e': '\x1b', 'E': '\x1b',
           'f': '\f', 'v': '\v'}


def _heredoc_delim(s, j):
    """(delimiter, tabs stripped?) of the heredoc whose `<<` ends just before
    s[j], quotes taken off the delimiter, or None."""
    n = len(s)
    strip = s[j:j + 1] == '-'
    j += strip
    while j < n and s[j] in ' \t':
        j += 1
    word = []
    while j < n and s[j] not in ' \t\n;&|<>()':
        c = s[j]
        if c in '\'"':
            k = s.find(c, j + 1)
            if k < 0:
                return None
            word.append(s[j + 1:k])
            j = k + 1
        elif c == '\\' and j + 1 < n:
            word.append(s[j + 1])
            j += 2
        else:
            word.append(c)
            j += 1
    return (''.join(word), strip) if word else None


def _heredoc_bodies(s, i, pending, spans):
    """Where the line after the heredocs' bodies begins, the bodies starting at
    s[i] and each running to its delimiter's own line: data, whatever quotes
    are in it (`Don't forget`). (operator, start, end) of each into `spans`."""
    n = len(s)
    for k, (delim, strip, op) in enumerate(pending):
        start = i
        while True:
            nl = s.find('\n', i)
            end = n if nl < 0 else nl
            line = s[i:end]
            matched = (line.lstrip('\t') if strip else line) == delim
            if matched or nl < 0:
                spans.append((op, start, i if matched else n))
                i = end if k == len(pending) - 1 else min(end + 1, n)
                break
            i = nl + 1
    return i


def _quoted_end(s, i):
    """Index just past the quoted string or command substitution that starts at
    s[i] -- '...', $'...', "...", $(...) or `...` -- read as sh reads it: the
    $(...) and `...` inside "..." have quotes of their own, and a heredoc's
    body inside $(...) none."""
    n, c = len(s), s[i]
    if c == "'":
        j = s.find("'", i + 1)
        if j < 0:
            raise ValueError('unterminated quote')
        return j + 1
    if c in '$`' and s[i + 1:i + 2] != '(':
        j = i + (2 if c == '$' else 1)          # $'...' or `...`: backslashes escape
        close = "'" if c == '$' else '`'
        while j < n and s[j] != close:
            j += 2 if s[j] == '\\' else 1
        if j >= n:
            raise ValueError('unterminated quote')
        return j + 1
    if c == '"':
        j = i + 1
        while j < n:
            ch = s[j]
            if ch == '"':
                return j + 1
            if ch == '\\':
                j += 2
            elif ch == '`' or (ch == '$' and s[j + 1:j + 2] == '('):
                j = _quoted_end(s, j)
            else:
                j += 1
        raise ValueError('unterminated quote')
    depth, j, pending = 0, i + 1, []            # $(...): its parentheses counted
    while j < n:
        ch = s[j]
        if ch == '\\':
            j += 2
        elif ch in '\'"`' or (ch == '$' and s[j + 1:j + 2] == "'"):
            j = _quoted_end(s, j)
        elif s.startswith('<<', j) and not s.startswith('<<<', j):
            d = _heredoc_delim(s, j + 2)
            if d:
                pending.append(d + (j,))
            j += 2
        elif ch == '\n' and pending:
            j = _heredoc_bodies(s, j + 1, pending, [])
            pending = []
        else:
            j += 1
            depth += {'(': 1, ')': -1}.get(ch, 0)
            if depth == 0:
                return j
    raise ValueError('unterminated command substitution')


def _shell_tokens(s, comments=None, substs=None, heredocs=None):
    """shell_split(), each token with where it stands in `s`: [(text, is
    operator, start, end)]. Where each comment stands, from its `#` to the end
    of its line, goes into `comments` when that is a list; where the command
    inside each `"...$(...)..."` stands, into `substs`; and each heredoc's
    body, which is data and no token, into `heredocs` as (where its `<<`
    stands, start, end)."""
    out, cur, pending = [], [], []
    start = [None]                        # where the word being read began
    i, n = 0, len(s)
    if heredocs is None:
        heredocs = []

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
        elif c == '$' and s[i + 1:i + 2] == "'":
            # $'...', as printf %q writes a prompt with a newline in it:
            # backslash escapes inside, `\'` among them
            if start[0] is None:
                start[0] = i
            j = i + 2
            while True:
                if j >= n:
                    raise ValueError('unterminated quote')
                ch = s[j]
                if ch == "'":
                    break
                if ch == '\\' and j + 1 < n:
                    cur.append(_ANSI_C.get(s[j + 1], s[j + 1]))
                    j += 2
                    continue
                cur.append(ch)
                j += 1
            i = j + 1
        elif c == '$' and s[i + 1:i + 2] == '"':
            if start[0] is None:
                start[0] = i
            i += 1                                # $"...": a translated "..."
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
                if ch == '`' or (ch == '$' and s[i + 1:i + 2] == '('):
                    j = _quoted_end(s, i)         # a command inside, quotes and all
                    if ch == '$' and substs is not None:
                        substs.append((i + 2, j - 1))
                    cur.append(s[i:j])
                    i = j
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
            if pending:
                i = _heredoc_bodies(s, i, pending, heredocs)
                del pending[:]
        elif c == '#' and start[0] is None:
            j = s.find('\n', i)
            j = n if j < 0 else j
            if comments is not None:
                comments.append((i, j))
            i = j
        elif c in _OP_CHARS:
            flush(i)
            j = i
            while j < n and s[j] in _OP_CHARS:
                j += 1
            out.append((s[i:j], True, i, j))
            if '<<' in s[i:j] and '<<<' not in s[i:j]:
                d = _heredoc_delim(s, j)        # its body begins on the next line
                if d:
                    pending.append(d + (i,))
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


def _key_values(text):
    """The values the credential rules would mask in `text`, each as found."""
    found, low = [], text.lower()
    for _name, gate, rx, sub in RULES:
        if all(any(w in low for w in group) for group in gate):
            for m in rx.finditer(text):
                whole, rep = m.group(0), sub(m)
                p = 0
                while p < min(len(whole), len(rep)) and whole[p] == rep[p]:
                    p += 1
                q = 0
                while q < min(len(whole), len(rep)) - p and whole[-1 - q] == rep[-1 - q]:
                    q += 1
                found.append(whole[p:len(whole) - q].strip('\'"\\'))
    return [v for v in found if len(v) >= 4]


def _redact_shell(s, depth, exact=True):
    """A shell command string with every agent prompt in it masked; (str, a
    prompt masked?). Only the masked words change -- the rest of the string,
    its quoting and spacing included, is written as it was -- but for a comment
    beside a masked prompt, which goes with it, and for a credential the rules
    cannot read through the string's quoting (`-H 'X-Api-Key: '...`, an
    escaped quote, eval's '"'"'): then the string rebuilt from its words is
    written, where they can."""
    if not _names_cli(s):
        return s, False
    comments, substs, heredocs = [], [], []
    try:
        toks = _shell_tokens(s, comments, substs, heredocs)
    except ValueError:
        # Not shell a lexer can follow -- a command line the kernel cut short, or
        # a heredoc of Python. An agent run at a command position in it is
        # masked from the agent's name on, since nothing after can be told apart
        # from prompt text; a name anywhere else (`--planner qwen`) is just text.
        m = _CLI_AT_COMMAND.search(s)
        if m is None:
            return s, False
        return s[:m.end(1)] + ' ' + PROMPT_MASK, True
    bodies = dict((op, (a, b)) for op, a, b in heredocs)
    edits, cmd = [], []
    for k, t in enumerate(toks + [None]):
        if t is not None and not t[1]:
            nxt = toks[k + 1] if k + 1 < len(toks) else None
            if (nxt is not None and nxt[1] and nxt[2] == t[3] and nxt[0][:1] in '<>'
                    and s[t[2]:t[3]].isdigit()):
                continue                    # `2>&1`: a file descriptor, no argument
            # `do claude ...`: a command after a reserved word
            if cmd or not (t[0] in _LEADING_WORDS and s[t[2]:t[3]] == t[0]):
                cmd.append(t)
            continue
        if cmd and t is not None and t[2] in bodies:
            a, b = bodies[t[2]]
            if s[a:b].strip() and find_cli([w[0] for w in cmd])[1] is not None:
                edits.append((a, b, PROMPT_MASK + '\n'))    # `claude -p <<'EOF'`: its prompt
        if cmd:
            argv = [w[0] for w in cmd]
            res, _f = _mask_argv(argv, depth, exact)
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
    for a, b in substs:
        # `out="$(claude -p ...)"`: a command inside a quoted word, unless the
        # word itself went
        if not any(x <= a and b <= y for x, y, _t in edits):
            red = _redact_string(s[a:b], depth, exact)
            if red != s[a:b]:
                edits.append((a, b, red))
    fired = any(PROMPT_MASK in e[2] for e in edits)
    for a, b in comments:
        # A comment beside a masked prompt may be the prompt's own words, which
        # a quoting this lexer misread turned into one; so may one with an agent
        # run in it. Neither is written out.
        if s[a + 1:b].strip() and (fired or _CLI_AT_COMMAND.search(s[a + 1:b])):
            edits.append((a, b, '# ' + PROMPT_MASK))
            fired = True
    out, at = [], 0
    for a, b, text in sorted(edits):
        out.append(s[at:a])
        out.append(text)
        at = b
    out.append(s[at:])
    out = ''.join(out)
    if "'" in s or '"' in s or '\\' in s:
        try:
            plain = ' '.join(t[0] for t in _shell_tokens(out))   # its words, masks and all
        except ValueError:
            plain = ''
        shown = redact_keys(out)[0]
        if any(v in shown for v in _key_values(plain)):
            return plain, fired
    return out, fired


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


# reserved words and builtins a command can follow: `for ...; do claude ...`
_LEADING_WORDS = frozenset(('do', 'then', 'else', 'elif', 'if', 'while', 'until', '{', '!',
                            'command', 'builtin'))
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


def _inner_command(spec, toks, i):
    """Where the command line a host mode runs begins, in toks from i on --
    Claude Code's `claude bg-pty-host --bg-pty-host SOCK COLS ROWS -- claude
    ...` -- or None."""
    host = False
    for j in range(i, len(toks)):
        if toks[j] in spec['hosts']:
            host = True
        elif toks[j] == '--':
            return j + 1 if host and j + 1 < len(toks) else None
    return None


def _render(argv, res):
    """argv with each argument's masking applied, a run of masks as one."""
    out = []
    for t, r in zip(argv, res):
        if r is None:
            out.append(t)
        elif r != PROMPT_MASK or not out or not out[-1].endswith(PROMPT_MASK):
            out.append(r)
    return out


def _mask_argv(argv, depth=0, exact=True):
    """Per argument, what masking `argv` changes (see _walk), and whether a
    prompt was masked: the agent run itself, the command line a host mode of
    it runs, then every command string an argument carries -- `bash -c '...'`,
    `sbatch --wrap='...'`, `tmux new 'claude ...'` -- parsed as a shell would
    and handled the same way, three levels deep."""
    n = len(argv)
    if exact and depth == 0 and n and _PROSE.search(argv[0]):
        # A process started under a name with spaces in it -- Claude Code's own
        # `claude bg-pty-host`, or `exec -a "..."` -- is read as those words.
        words = argv[0].split()
        res, fired = _mask_argv(words + list(argv[1:]), depth, exact)
        k = len(words)
        first = ' '.join(_render(words, res[:k])) if any(r is not None for r in res[:k]) else None
        return [first] + res[k:], fired
    res = [None] * n
    i, name = find_cli(argv)
    if name is not None:
        spec = CLIS[name]
        res[i + 1:] = _walk(spec, argv[i + 1:], exact)[0]
        k = _inner_command(spec, argv, i + 1)
        if k is not None:
            inner = _mask_argv(argv[k:], depth, exact)[0]
            res[k:] = [r if r is not None else res[k + j] for j, r in enumerate(inner)]
    cur = [argv[j] if res[j] is None else res[j] for j in range(n)]
    for j, head in _command_strings(cur):
        if res[j] is not None or not argv[j].startswith(head):
            continue
        part = argv[j][len(head):]
        red = _redact_string(part, depth, exact)
        if red != part:
            res[j] = head + red
    return res, any(r is not None and PROMPT_MASK in r for r in res)


# How many command strings deep are read. Claude Code's own tool calls are two
# (`bash -c` and `eval`), and one of them running `tmux new "bash -lc '...'"`
# four.
_MAX_DEPTH = 6


def _redact_string(s, depth, exact=True):
    """A command string found at `depth` with its prompts masked, _MAX_DEPTH
    levels deep; deeper than that, one naming an agent is masked from the name
    on, since nothing after it can be told apart from a prompt."""
    if depth < _MAX_DEPTH:
        return _redact_shell(s, depth + 1, exact)[0]
    for m in _CLI_WORD.finditer(s):
        if m.group(0) not in _GENERIC:
            return s[:m.end()] + ' ' + PROMPT_MASK if s[m.end():].strip() else s
    return PROMPT_MASK if _names_cli(s) and not _CLI_WORD.search(s) else s


def redact_prompt_argv(argv, depth=0, exact=True):
    """(argv with every agent prompt in it masked, masked?): each run of
    masked arguments is one PROMPT_MASK."""
    res, fired = _mask_argv(argv, depth, exact)
    return _render(argv, res), fired


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
    return bool(line) and _names_cli(line)


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
    if prompts and _names_cli(line):
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
