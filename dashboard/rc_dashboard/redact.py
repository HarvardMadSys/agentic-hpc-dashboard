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
import re

MASK = "[REDACTED]"

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

# Secret patterns a cluster added to its risk rules (`reducers/risk.py`,
# `ruleset`): whatever the credentials panel counts, the rest of the page must
# mask, and a site's own token format is not in the generic rules above. The
# whole match is masked. Process-wide, registered once at start-up.
_EXTRA = []


def extend(patterns):
    """Also mask every match of these regexes (idempotent)."""
    have = {rx.pattern for rx in _EXTRA}
    for p in patterns:
        if p and p not in have:
            _EXTRA.append(re.compile(p))
            have.add(p)


# Fields of a record or payload that carry argv or free text from a process.
TEXT_KEYS = frozenset(("args", "argv", "cmdline", "captured_tail", "submit_line"))


def redact(text):
    """`text` with every credential value replaced by MASK. MASK itself matches
    no rule, so applying it twice is harmless."""
    if not text or not isinstance(text, str):
        return text
    low = text.lower()
    for _name, gate, rx, sub in RULES:
        if all(any(w in low for w in group) for group in gate):
            text = rx.sub(sub, text)
            low = text.lower()
    for rx in _EXTRA:
        text = rx.sub(MASK, text)
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
