"""Masking credentials in command lines before anything is served.

A process's argv is the most useful thing this dashboard shows and the most
dangerous: people put tokens on command lines (`--hf-token ...`,
`OPENAI_API_KEY=... python`, `curl -H "Authorization: Bearer ..."`,
`git clone https://user:pass@...`), and the collector records argv whole.

WHERE it happens is the design. Not at ingest: the detectors read raw argv --
the secrets panel has to see a token to count it, and the sandbox, approval,
role and self-observation rules read flags out of it -- so the records stay raw
in memory and in the saved window state. Instead every exit to the outside
world goes through here:

* every built window's payload, once, when it is materialised (`payload`);
* the live frame's event tail (`reducers/live.py`);
* raw records from `/api/events` and `/api/export` (`record`), and the `q`
  search over them, which matches redacted text so it cannot be used to probe
  for a secret one prefix at a time.

Doing it at the exit also keeps it cheap: a few hundred strings per payload,
rather than a regex pass over every one of millions of records during a
week-long backfill.

What is masked is the VALUE, never the structure around it: the flag, the
variable name, the header name and the URL's user stay, so a reader can still
tell what kind of thing was there and that it was removed. Anything the
secrets panel (`reducers/risk.py`, SECRETS) flags must be masked here; the
tests hold that.
"""
import functools
import re

MASK = "[REDACTED]"

# A value that only refers to a secret is not one: `--token $HF_TOKEN`. Nor is
# a kwarg in an inline script that reads one, `token=os.environ["HF_TOKEN"]`,
# or is a placeholder, `password=None)`; `password=None-of-yours` still is.
_NOT_A_REF = r"(?!\$|\"\$|'\$)"
_NOT_CODE = (r"(?!(?:os\.)?(?:environ\b|getenv\()|Path\("
             r"|(?:None|True|False|%s|\{\})(?=[\s)\],;}\"']|$))")
_VALUE = r"(\\?\"[^\"]*\"|\\?'[^']*'|[^\s\"']+)"

# Names that hold a credential, as an env var, a kwarg or a query key. Matched
# on the END of the name: `HF_TOKEN`, `db_password`, `AWS_SECRET_ACCESS_KEY`,
# Spark's `fs.s3a.secret.key`. `max_tokens` ends in an S and is left alone;
# `PWD` alone is the cwd. An env var's name, in capitals, may also end in _KEY,
# PASS or _AUTH -- `KAGGLE_KEY`, `SSHPASS`, `REDISCLI_AUTH` -- where in lower
# case the same ending is a setting's (`sort_key=`, `bypass=`). `PASSWD_FILE`
# names where a password is kept, not the password.
_SECRET_NAME = (r"[A-Za-z0-9_]*(?:TOKEN|SECRET|PASSWORD|PASSWD|PASSPHRASE|_PASS|_PWD|"
                r"(?:API|ACCESS|SECRET|PRIVATE|AUTH)[._]?KEY|CREDENTIALS?"
                r"|(?-i:_KEY|(?<!BY)PASS|_AUTH))")

_NAME_WORDS = ("token", "secret", "pass", "_pwd", "key", "credential", "_auth")

# Where a program's name begins: the line's start, a space or a path's slash,
# or a quote, a backtick, ; | & ( or = just before it -- `bash -c 'cd
# /x;mysql ...'`, and Claude Code's tool calls, `eval 'mysql ...'`.
_CMD_START = r"(?:^|[\s/'\"`;|&(=])"

# Every rule reads a text in time linear in its length; past TEXT_MAX
# characters a text is cut -- at the last space before the limit, so no word is
# written in part -- and CUT is written after what was read. The rest is
# neither read nor written: a command line can be as long as the kernel
# allows, and every one is masked.
TEXT_MAX = 16384
CUT = "[CUT]"


def cut_text(text, limit=TEXT_MAX):
    """(`text` cut to at most `limit` characters at a space, tab or newline,
    whether it was cut). CUT is the caller's to write after the masked text."""
    if len(text) <= limit:
        return text, False
    k = max(text.rfind(" ", 0, limit + 1), text.rfind("\t", 0, limit + 1),
            text.rfind("\n", 0, limit + 1))
    return text[:max(k, 0)], True


def _each(rx, sub):
    """A substitution that keeps what a rule's first group matched and applies
    `rx` -> `sub` to what its second did: each value in one command's span."""
    return lambda m: m.group(1) + rx.sub(sub, m.group(2))


def _until(start, chars=r"[^\n]"):
    """What lies between a program and an option of its: read lazily, however
    far, but only up to where the rule's own `start` next begins a command,
    whose own match reads on from there. So each part of a line is read once,
    and no option is out of reach: a bound on how far to read would leave a
    password past it written as it was."""
    return r"(?:(?!" + start + r")" + chars + r")*?"


# A command's span for the rules that read every value in it: to the end of
# the command, a quoted string whole, `;` in it and all.
_SPAN = r"((?:\"[^\"]*(?:\"|\Z)|'[^']*(?:'|\Z)|[^\n;&|<>\"'])*)"
_DB = _CMD_START + r"(?:mysql|mysqldump|mysqladmin|mariadb|mongo|mongosh)\b"
_REDIS = _CMD_START + r"redis-cli\b"
_LOGIN = (_CMD_START + r"(?:(?:docker|podman|nerdctl|buildah|skopeo|oras|helm|apptainer"
          r"|singularity)(?:\s+(?:registry|remote))?\s+login|az\s+login|twine\s+(?:upload|register)"
          r"|mongo(?:sh|dump|restore|export|import|stat|top|files)?)\b")


# A container's `--user 1000:1000` is a uid and a gid, not a user and password:
# docker's, podman's, a wrapper of theirs (`podman-hpc`, a test's
# `fake_podman.py`), lines continued with a backslash and all. The rule matches
# such a command whole, from the program's name to the command's end, and masks
# what in it is not a uid:gid; so each part of a line is read once, however
# many `-u` it holds.
_UID_GID = re.compile(r"\d+:\d+\Z")
_CONTAINER_RUN = (r"(?:^|[\s/;&|(_-])(?:docker|podman|nerdctl|buildah|enroot|kubectl|"
                  r"crictl|ctr)\b(?:\\\n|[^;&|\n])*")
_USER_PASSWORD = re.compile(
    r"((?:^|\s)(?:-u|--user|--proxy-user)(?:=|\s*)[\"']?)(?![+\[])([^\s:\"']+:)(?!\$)"
    r"([^\s\"']+)")


def _user_password(m):
    """`-u user:` kept and the password masked; in a container's command, all
    but a uid:gid."""
    if m.group(1) is None:
        return _USER_PASSWORD.sub(_not_a_uid_gid, m.group(0))
    return m.group(1) + m.group(2) + MASK


def _not_a_uid_gid(m):
    if _UID_GID.match(m.group(2) + m.group(3)):
        return m.group(0)
    return m.group(1) + m.group(2) + MASK


# A word on a joined line, where a backslash escapes the character after it:
# `Authorization:\ Bearer\ KEY` is one word, as the shell reads it.
_WORD = r"(?:\\.|[^\s\"'\\])+"

# A credential header's name: an API key, a token (`X-Auth-Token`,
# `X-Vault-Token`, GitLab's `PRIVATE-TOKEN`), a secret, a session's cookie, an
# API gateway's subscription key. An `X-` is read only where a name begins, so
# a run of them is read once, not once for each.
_HEADER_NAME = (r"(?:(?<![A-Za-z-])X-[A-Za-z-]*)?(?:Api[-_]?Key|Auth-?Token|Access-?Token"
                r"|Private-?Token|(?<=-)Token|Secret|Cookie|Subscription-Key)")


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
    # a scheme it knows. `Bearer $TOKEN` names a secret without holding one.
    # Any other scheme (`ApiKey`, `Negotiate`, AWS's) cannot be told from a
    # token, so it is masked with what follows it: to the closing quote of a
    # quoted header; else its first two words -- but an option or a URL as the
    # second, or a reference (`ApiKey $KEY`) -- and each word after a comma
    # (`Credential=..., Signature=...`).
    ("auth_header", (("authorization",),), re.compile(
        r"(([\"'])?(?:Proxy-)?Authorization\s*:\s*((?:Bearer|Basic|Token|Digest|Bot)\s+)?)"
        + _NOT_A_REF + r"(?!(?:Bearer|Basic|Token|Digest|Bot)\s)"
        r"(?(3)|(?!\[|" + _WORD + r"\s+[\"']?\$))"
        r"((?(2)[^\s\"'\\][^\"'\\]*|" + _WORD
        + r"(?(3)|(?:\s+(?![-;&|<>]|\S*://)" + _WORD + r")?(?:(?<=,)\s+" + _WORD + r")*)))",
        re.I),
     lambda m: m.group(1) + MASK),
    # Nor is a shell default that is empty or another variable a header --
    # `${OPENAI_API_KEY:-}`, `${OPENAI_API_KEY:-$KEY}`, `${OPENAI_API_KEY:?unset}`
    # -- while a default written out is the key itself. A cookie's pairs go on
    # after a `;`.
    ("key_header", ((":",), ("api-key", "apikey", "api_key", "-token", "secret", "cookie",
                             "subscription-key")), re.compile(
        r"(" + _HEADER_NAME + r"\s*:(?![-=+]?\}|[-=+][\"']?\$|\?)\s*)" + _NOT_A_REF
        + r"([^\s\"']+(?:(?<=;)\s+[^\s\"']+)*)", re.I),
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
        r"(?:" + _CONTAINER_RUN + r")|" + _USER_PASSWORD.pattern),
     _user_password),
    # --token X, --hf-token=X, --api_key X, --password X, --secret X ... A flag
    # starts an argument: the `-credential` inside `git-credential` is not one.
    ("secret_flag", (("-",), _NAME_WORDS), re.compile(
        r"((?<![^\s\"'=])--?[A-Za-z0-9_-]*(?:token|secret|password|passwd|passphrase|api[-_]?key|"
        r"access[-_]?key|private[-_]?key|auth[-_]?key|credentials?)(?:=|\s+))" + _NOT_A_REF
        + r"(?!-)" + _VALUE, re.I),
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
    # {"X-Api-Key"="..."}`. The value is masked to its closing quote, but for a
    # scheme it knows.
    ("header_json", (("\"", "'"), ("authorization", "api-key", "apikey", "api_key", "-token",
                                   "secret", "cookie", "subscription-key")), re.compile(
        r"(\\?[\"'](?:(?:Proxy-)?Authorization|" + _HEADER_NAME + r")\\?[\"']\s*[:=]\s*\\?[\"']"
        r"(?:(?:Bearer|Basic|Token|Digest|Bot)\s+)?)(?!\$)(?!(?:Bearer|Basic|Token|Digest|Bot)\s)"
        r"([^\s\"'\\][^\"'\\]*)", re.I),
     lambda m: m.group(1) + MASK),
    # A credential flag and its value as items of a JSON list: an MCP server's
    # `"args": ["--api-key", "..."]`.
    ("secret_flag_json", (("\"", "'"), _NAME_WORDS), re.compile(
        r"(\\?[\"']--?[A-Za-z0-9_-]*(?:token|secret|password|passwd|passphrase|api[-_]?key|"
        r"access[-_]?key|private[-_]?key|auth[-_]?key|credentials?)\\?[\"']\s*,\s*\\?[\"'])"
        r"(?!\$)([^\"'\\]+)", re.I),
     lambda m: m.group(1) + MASK),
    # ?access_token=...&key=...: query-string credentials, including signed URLs
    # and EOS's `?authz=`.
    ("query_secret", (("?", "&"), ("=",)), re.compile(
        r"([?&](?:access_token|refresh_token|id_token|token|authz|api_?key|key|secret|password|"
        r"sig|signature|X-Amz-Signature|X-Amz-Credential|X-Goog-Signature)=)([^&\s\"'#]+)", re.I),
     lambda m: m.group(1) + MASK),
    # mysql -pSECRET (no space), redis-cli -a SECRET, sshpass -p SECRET. Not psql:
    # its -p is the port. The option is looked for however far it is (_until).
    ("db_password", (("mysql", "mariadb", "mongo"), ("-p",)), re.compile(
        r"(" + _DB + _until(_DB) + r"\s-p)(?=\S)([^\s\"']+)"),
     lambda m: m.group(1) + MASK),
    ("short_password", (("redis-cli", "sshpass"),), re.compile(
        r"(" + _CMD_START + r"(?:redis-cli\b" + _until(_REDIS) + r"\s-a|sshpass\s+-p)\s*)"
        + _NOT_A_REF + r"([^\s\"']+)"),
     lambda m: m.group(1) + MASK),
    # A login's -p, spaced or not: a registry login's (docker, podman,
    # apptainer, singularity ...), az login's, twine upload's, the mongo
    # tools'. Not ssh's -p, a port, nor docker run's, a port mapping.
    ("login_password", (("login", "twine", "mongo"), ("-p",)), re.compile(
        r"(" + _LOGIN + _until(_LOGIN, r"[^\n;&|]") + r"\s-p(?:=|\s+)?)" + _NOT_A_REF
        + r"(?!-)" + _VALUE),
     lambda m: m.group(1) + MASK),
    # A key given as a command's own argument: `wandb login KEY` -- every word
    # after `login` but an option, a URL after `--host` and all.
    ("login_key", (("wandb",), ("login",)), re.compile(
        r"(" + _CMD_START + r"wandb\s+login\b)" + _SPAN),
     _each(re.compile(r"(\s+)" + _NOT_A_REF + r"(?!-)" + _VALUE), lambda m: m.group(1) + MASK)),
    # A credential-named setting's value: `aws configure set
    # aws_secret_access_key VALUE`, `rclone config create REMOTE s3
    # secret_access_key VALUE pass VALUE ...`.
    ("config_secret", (("aws", "rclone"), ("config",)), re.compile(
        r"(" + _CMD_START + r"(?:aws\s+configure\s+set|rclone\s+config\s+(?:create|update))\b)"
        + _SPAN),
     _each(re.compile(r"(\s(?:[\w-]+\.)*(?:" + _SECRET_NAME + r"|pass|key)\s+)" + _NOT_A_REF
                      + r"(?!-)" + _VALUE, re.I), lambda m: m.group(1) + MASK)),
    # Token shapes that are secrets wherever they appear, flag or not. A JWT
    # starts a word: after a hyphen, `eyJ` is the middle of one.
    ("token_shape", (("sk-", "ghp_", "gho_", "ghu_", "ghs_", "ghr_", "github_pat_", "glpat-",
                      "hf_", "akia", "asia", "xox", "aiza", "npm_", "eyj", "zteos64", "nvapi-",
                      "gsk_", "r8_", "pplx-", "xai-", "ya29.", "k_live_", "k_test_", "sg.",
                      "_v1_", "hvs.", "hvb.", "hvr.", "pypi-age"),), re.compile(
        r"\b(?:sk-(?:ant-|proj-)?[A-Za-z0-9_\-]{20,}"
        r"|zteos64:[A-Za-z0-9+/=_%\-]{20,}"
        r"|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}"
        r"|glpat-[A-Za-z0-9_\-]{20,}"
        r"|hf_[A-Za-z0-9]{30,}"
        r"|(?:AKIA|ASIA)[0-9A-Z]{16}"
        r"|xox[abprs]-[A-Za-z0-9-]{10,}"
        r"|AIza[0-9A-Za-z_\-]{35}"
        r"|npm_[A-Za-z0-9]{36}"
        r"|nvapi-[A-Za-z0-9_\-]{20,}"                  # NVIDIA
        r"|gsk_[A-Za-z0-9]{20,}"                       # Groq
        r"|r8_[A-Za-z0-9]{30,}"                        # Replicate
        r"|pplx-[A-Za-z0-9]{20,}"                      # Perplexity
        r"|xai-[A-Za-z0-9]{20,}"                       # xAI
        r"|ya29\.[A-Za-z0-9_\-]{20,}"                  # a Google OAuth access token
        r"|[rs]k_(?:live|test)_[A-Za-z0-9]{20,}"       # Stripe
        r"|SG\.[A-Za-z0-9_\-]{16,}\.[A-Za-z0-9_\-]{16,}"   # SendGrid
        r"|do[por]_v1_[a-f0-9]{40,}"                   # DigitalOcean
        r"|hv[sbr]\.[A-Za-z0-9_\-]{20,}"               # a Vault token
        r"|pypi-AgE[A-Za-z0-9_\-]{40,}"                # PyPI
        r"|(?<![\w-])eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,})"),
     lambda m: MASK),
)

# Secret patterns a cluster added to its risk rules (`reducers/risk.py`,
# `ruleset`): whatever the credentials panel counts, the rest of the page must
# mask, and a site's own token format is not in the generic rules above. The
# whole match is masked. Process-wide, registered once at start-up.
_EXTRA = []


# The `cache_clear` of every memo elsewhere whose answers are built from masked
# text (`phase_names.tagset`): what was masked changes when the rules grow, so
# `extend` drops them with its own. Registered by the memo's module, which
# imports this one -- never the other way round.
_DEPENDENT = []


def masked_by_rules(cache_clear):
    """Register a memo built from `redact`'s output; `extend` clears it."""
    _DEPENDENT.append(cache_clear)
    return cache_clear


def extend(patterns):
    """Also mask every match of these regexes (idempotent)."""
    have = {rx.pattern for rx in _EXTRA}
    grew = False
    for p in patterns:
        if p and p not in have:
            _EXTRA.append(re.compile(p))
            have.add(p)
            grew = True
    if grew:                                    # masked under the old rules
        _redact_memo.cache_clear()
        for clear in _DEPENDENT:
            clear()


# Fields of a record or payload that carry argv or free text from a process.
TEXT_KEYS = frozenset(("args", "argv", "cmdline", "captured_tail", "submit_line"))


# A command line is masked wherever it is read -- every tool call reduced, every
# panel materialized, every frame -- and the same ones recur. Memoized up to a
# length and a count, both small: the repeats are short commands, a long one is
# nearly always unique (a heredoc, a prompt), and the memo holds its text and
# its masked copy for as long as it stays -- MEMO_MAX x 2 x MEMO_LEN characters
# at most, ~33 MB. `extend` drops it, since what is masked changes with the rules.
MEMO_LEN = 1024
MEMO_MAX = 16384


def redact(text):
    """`text` with every credential value replaced by MASK. MASK itself matches
    no rule, so applying it twice is harmless."""
    if not text or not isinstance(text, str):
        return text
    if len(text) > MEMO_LEN:
        return _redact(text)
    return _redact_memo(text)


@functools.lru_cache(maxsize=MEMO_MAX)
def _redact_memo(text):
    return _redact(text)


# The most of a text masked here, and so served. The collector masks every exec on one thread and
# cuts at TEXT_MAX; the page reads a command as far as the phase rules do (`phase_rules._MAX_TEXT`,
# 100,000 characters: the longest argv seen was 58 k, from a collector with no cut), so it masks
# that far. Past it a text is cut at a space, and CUT stands where the rest was.
READ_MAX = 100000


def _redact(text):
    text, cut = cut_text(text, READ_MAX)
    low = text.lower()
    for _name, gate, rx, sub in RULES:
        if all(any(w in low for w in group) for group in gate):
            text = rx.sub(sub, text)
            low = text.lower()
    for rx in _EXTRA:
        text = rx.sub(MASK, text)
    if cut:
        return text + " " + CUT if text else CUT
    return text


def record(rec):
    """A shallow copy of a raw record with its text fields redacted.

    A copy, because the record may still be in use by a reducer or a scan that
    needs the raw argv."""
    out = dict(rec)
    for k in TEXT_KEYS:
        v = out.get(k)
        if isinstance(v, str):
            out[k] = redact(v)
        elif isinstance(v, list):
            out[k] = [redact(x) if isinstance(x, str) else x for x in v]
    return out


def _text(v):
    if isinstance(v, str):
        return redact(v)
    if isinstance(v, list):
        return [redact(x) if isinstance(x, str) else payload(x) for x in v]
    return payload(v)


def payload(obj):
    """A deep copy of a served payload with every TEXT_KEYS string redacted,
    wherever it sits. Walks the structure once; leaves everything else as is."""
    if isinstance(obj, dict):
        return {k: (_text(v) if k in TEXT_KEYS else payload(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [payload(v) for v in obj]
    if isinstance(obj, tuple):
        return tuple(payload(v) for v in obj)
    return obj
