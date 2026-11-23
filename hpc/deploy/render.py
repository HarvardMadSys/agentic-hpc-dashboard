#!/usr/bin/env python3
"""Fill one of this kit's templates (*.in) from site.env.

    render.py [--strict] TEMPLATE SITE_ENV [KEY=VALUE ...] > OUT

Every `@KEY@` in the template is replaced by the value of KEY: from site.env, from
a KEY=VALUE argument (which wins, and is seen by later lines that use $KEY), or
from the values derived below. A placeholder left unfilled, or a value that fails
its check, exits 2 and names it. deploy.sh renders every template before it
ships anything, so a bad site.env stops a deploy before any remote command runs.

site.env is also read by bash (deploy.sh, ship-sender.sh, check.sh source it), so a
line the two would read differently is refused: a value with blanks and no quotes
(bash runs its second word as a command), or one with $( ) or backquotes. A key this
kit does not know is named, with the one it is nearest to, and left out. The names
every kit gives the same setting work here too: MEMORY_MAX (DASH_MEMORY_MAX),
yes/no as 1/0, true/false, yes/no or on/off. --strict (deploy.sh) also refuses the
example's own values (your-account, *.example.org), which name no real host.
"""
import difflib
import json
import os
import re
import sys

PLACEHOLDER = re.compile(r"@([A-Z][A-Z0-9_]*)@")
REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)")
PRESETS = [60, 360, 720, 1440, 4320, 10080]
SSH_OPTS = ["-T", "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-o", "Compression=no",
            "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=4",
            "-o", "ConnectTimeout=20", "-o", "StrictHostKeyChecking=accept-new",
            "-o", "LogLevel=ERROR"]
# Values that land inside JSON strings, unit lines, or remote commands: no quotes,
# backslashes, or spaces.
PLAIN = re.compile(r"^[A-Za-z0-9_./:+@-]*$")
REQUIRED = ("SOURCE_USER", "DASH_SSH", "DASH_NODE", "DASH_ROOT", "DASH_PORT")
OPTIONAL = ("SOURCE_DOMAIN", "SENDER_DIR", "DASH_PYTHON", "RISK_PROFILE", "CODEX")
# Every key site.env may set (site.env.example has them all), and the other names every
# kit gives the same setting.
KNOWN = ("SOURCE_USER", "SOURCE_NODES", "SOURCE_DOMAIN", "SOURCE_ROOT", "SENDER_DIR",
         "SHARED_HOME", "DASH_SSH", "DASH_NODE", "DASH_ROOT", "DASH_PORT", "DASH_PYTHON",
         "PRIVATE_TREE", "SITE_NAME", "SITE_DESCRIPTION", "SLURM", "NODE_NOUN", "RISK_PROFILE",
         "ANONYMIZE", "DROP_USERS", "DROP_AGENT_USERS", "CODEX", "HISTORY_DAYS", "MAX_WINDOWS",
         "PREWARM_MIN", "MAX_SESSIONS", "DAY_CACHE", "INGEST_WORKERS", "MEMORY_MAX",
         "FREE_MIN_GB", "SSH_KEY", "WORK_DIR", "NODE_MODULES", "SLACK_WEBHOOK_FILE", "KIT")
ALIASES = {"DASH_MEMORY_MAX": "MEMORY_MAX"}
# The example's own values, which name no real account or host (--strict).
PLACEHOLDER_VALUE = re.compile(r"(^|[@./])your-account([@./]|$)|(^|[.@])example\.(org|com|net)$")
YES, NO = ("1", "true", "yes", "on"), ("0", "false", "no", "off")
MEMORY = re.compile(r"^[1-9][0-9]*[KMGT]$")
SYSTEM_PATH = "/usr/local/bin:/usr/bin:/bin"
# This kit's folder, as a path in the repository (hpc/deploy): the release holds it
# there, so the unit and the hourly check find its scripts under DASH_ROOT/current.
KIT_DIR = os.path.dirname(os.path.abspath(__file__))
KIT = os.path.relpath(KIT_DIR, os.path.dirname(os.path.dirname(KIT_DIR)))


def parse_env(path, overrides, warn=None):
    """site.env as bash reads it, for the lines bash and this read alike; a line they
    would not is refused, naming it."""
    vals = {}

    def expand(v):
        return REF.sub(lambda m: vals.get(m.group(1) or m.group(2),
                                          os.environ.get(m.group(1) or m.group(2), "")), v)
    for n, line in enumerate(open(path), 1):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        if k in overrides:
            vals[k] = overrides[k]
            continue
        v = v.strip()
        quoted = len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'"
        if quoted:
            v = v[1:-1]
        elif re.search(r"\s", v):
            raise ValueError("%s:%d: %s's value has blanks and no quotes, so bash runs its second "
                             "word as a command: write %s=\"%s\"" % (path, n, k, k, v))
        if "$(" in v or "`" in v:
            raise ValueError("%s:%d: %s: no $( ) or backquotes: bash runs them when it reads "
                             "site.env, and this kit does not" % (path, n, k))
        vals[k] = expand(v) if not (quoted and line.split("=", 1)[1].strip()[0] == "'") else v
    for k, v in overrides.items():
        vals.setdefault(k, v)
    for alias, key in ALIASES.items():
        if alias in vals:
            if key in vals and vals[key] != vals[alias]:
                raise ValueError("%s=%r and %s=%r say two things; settle on one"
                                 % (key, vals[key], alias, vals[alias]))
            vals.setdefault(key, vals[alias])
    for k in sorted(vals):
        if k not in KNOWN and k not in ALIASES and k not in overrides and warn:
            near = difflib.get_close_matches(k, KNOWN, n=1, cutoff=0.75)
            warn("render.py: %s: %s is not a setting%s; it is left out"
                 % (path, k, " (did you mean %s?)" % near[0] if near else ""))
    return vals


def _number(v, key, default, kind=int):
    """`v[key]` as a number, or `default` when empty; a bad one names its key."""
    raw = v.get(key, "") or str(default)
    try:
        return kind(raw)
    except ValueError:
        raise ValueError("%s=%r is not %s" % (key, raw, "a whole number" if kind is int else "a number"))


def _flag(v, key, default):
    """A yes/no setting as JSON's true or false: 1/0, true/false, yes/no, on/off."""
    x = (v.get(key, "") or default).strip().lower()
    if x in YES:
        return "true"
    if x in NO:
        return "false"
    raise ValueError("%s=%r must be one of 1/0, true/false, yes/no, on/off" % (key, v.get(key)))


def _accounts(v, key):
    """The account names in `key`, separated by spaces or commas, each once."""
    out = []
    for name in v.get(key, "").replace(",", " ").split():
        if not PLAIN.match(name) or name.startswith("-"):
            raise ValueError("%s: %r is not an account name" % (key, name))
        if name not in out:
            out.append(name)
    return out


def derive(v):
    """The values the templates need beyond site.env's own. A history or memory key that
    site.env leaves out takes the dashboard's own default, as site.env.example has it."""
    for k in REQUIRED:
        if not v.get(k) or not PLAIN.match(v[k]):
            raise ValueError("%s=%r must be set, without quotes, spaces, or backslashes" % (k, v.get(k)))
    for k in OPTIONAL:
        if not PLAIN.match(v.get(k, "")):
            raise ValueError("%s=%r must have no quotes, spaces, or backslashes" % (k, v[k]))
    root = v["DASH_ROOT"].rstrip("/")
    if not root.startswith("/"):
        raise ValueError("DASH_ROOT=%r must be an absolute path" % v["DASH_ROOT"])
    for k in ("SENDER_DIR", "CODEX"):
        if v.get(k) and not v[k].startswith("/"):
            raise ValueError("%s=%r must be a full path, or empty" % (k, v[k]))
    sources = v.get("SOURCE_NODES", "").split()
    if not sources:
        raise ValueError("SOURCE_NODES must name at least one node")
    for h in sources:
        if not PLAIN.match(h) or h.startswith("-"):
            raise ValueError("SOURCE_NODES: %r is not a host name" % h)
    if len(set(sources)) != len(sources):
        raise ValueError("SOURCE_NODES names a node twice")
    if v.get("SHARED_HOME", "yes") not in ("yes", "no"):
        raise ValueError("SHARED_HOME=%r must be yes or no" % v["SHARED_HOME"])
    memory = v.get("MEMORY_MAX") or "32G"
    if not MEMORY.match(memory):
        raise ValueError("MEMORY_MAX=%r must be a size such as 7G or 32G" % memory)
    days = _number(v, "HISTORY_DAYS", 7)
    if days < 1:
        raise ValueError("HISTORY_DAYS must be at least 1")
    port = _number(v, "DASH_PORT", 8080)
    if not 1 <= port <= 65535:
        raise ValueError("DASH_PORT=%d is not a port (1..65535)" % port)
    # Two processes per node reduce the hours by default, one per reducer group: a node
    # and group is one process at a time, so more do nothing, and with fewer than one per
    # node the dashboard reduces each node's hour in one process.
    workers = v.get("INGEST_WORKERS") or str(2 * len(sources))
    if not workers.isdigit() or int(workers) > 256:
        raise ValueError("INGEST_WORKERS=%r must be a process count 0..256" % workers)
    free = _number(v, "FREE_MIN_GB", 300, float)
    if free < 0:
        raise ValueError("FREE_MIN_GB must not be negative")
    user, domain = v["SOURCE_USER"], v.get("SOURCE_DOMAIN", "")
    codex = v.get("CODEX", "")
    profile = v.get("RISK_PROFILE", "")
    if profile and not profile.startswith("/"):
        profile = "%s/current/dashboard/%s" % (root, profile)
    d = dict(v)
    d.update({
        "KIT": KIT,
        "DAYS": str(days + 1),
        "RETENTION_MIN": str(days * 1440),
        "BACKFILL_HOURS": str(days * 24),
        "PRESETS_JSON": json.dumps([m for m in PRESETS if m <= days * 1440]),
        "PREWARM_JSON": json.dumps(_minutes(v.get("PREWARM_MIN", "60 360 720"))),
        # Every node comes over the stream, into the copy in the deployment's folder: one
        # root per node, as a glob the dashboard expands again at every rescan, so a node
        # added to SOURCE_NODES needs no restart of it.
        "DASH_ROOT": root,
        "EBPF_ROOTS_JSON": json.dumps(["%s/data/mirror/*" % root]),
        "HOSTS_JSON": json.dumps({h: ["ssh"] + SSH_OPTS + ["-i", "%s/etc/rc_fleet_stream" % root,
                                                          "%s@%s" % (user, h + ("." + domain if domain else "")),
                                                          "send"]
                                  for h in sources}, sort_keys=True),
        "DAY_CACHE": _flag(v, "DAY_CACHE", "true"),
        "ANONYMIZE": _flag(v, "ANONYMIZE", "false"),
        "PRIVATE_TREE": _flag(v, "PRIVATE_TREE", "true"),
        # A cluster's login nodes run Slurm unless site.env says not.
        "SLURM": _flag(v, "SLURM", "true"),
        "NODE_NOUN_JSON": json.dumps(v.get("NODE_NOUN") or "login nodes"),
        "DASH_MEMORY_MAX": memory,
        "FREE_MIN_GB": json.dumps(int(free) if free.is_integer() else free),
        # Ask runs the Codex CLI when site.env names one; the command stays well formed
        # either way, so the page's settings show what it would run.
        "ASK_ENABLED": "true" if codex else "false",
        "CODEX_JSON": json.dumps(codex or "codex"),
        "DASH_PATH": (os.path.dirname(codex) + ":" if codex else "") + SYSTEM_PATH,
        "RISK_PROFILE_JSON": json.dumps(profile),
        # The stream's own account, and the ones site.env names.
        "DROP_GLOBAL_JSON": json.dumps([user] + [a for a in _accounts(v, "DROP_USERS") if a != user]),
        "DROP_AGENT_JSON": json.dumps(_accounts(v, "DROP_AGENT_USERS")),
        "SITE_NAME_JSON": json.dumps(v.get("SITE_NAME", "")),
        "SITE_DESCRIPTION_JSON": json.dumps(v.get("SITE_DESCRIPTION", "")),
        "MAX_WINDOWS": str(_number(v, "MAX_WINDOWS", 5)),
        "MAX_SESSIONS": str(_number(v, "MAX_SESSIONS", 150000)),
        "INGEST_WORKERS": str(int(workers)),
        "DASH_PORT": str(port),
    })
    return d


def _minutes(text):
    try:
        return [int(x) for x in text.replace(",", " ").split()]
    except ValueError:
        raise ValueError("PREWARM_MIN=%r is not a list of minutes, such as \"60 360 720\"" % text)


def strict(v):
    """deploy.sh's extra check: no value still the example's own."""
    for k in ("SOURCE_USER", "SOURCE_DOMAIN", "SENDER_DIR", "DASH_SSH", "DASH_ROOT"):
        if PLACEHOLDER_VALUE.search(v.get(k, "")) or "/your-account" in v.get(k, ""):
            raise ValueError("%s=%r is still site.env.example's value: put this site's in" % (k, v[k]))


def render(text, vals):
    missing = sorted(set(PLACEHOLDER.findall(text)) - set(vals))
    if missing:
        raise KeyError(", ".join(missing))
    return PLACEHOLDER.sub(lambda m: vals[m.group(1)], text)


def main(argv):
    is_strict = bool(argv) and argv[0] == "--strict"
    if is_strict:
        argv = argv[1:]
    if len(argv) < 2:
        sys.stderr.write(__doc__)
        return 2
    template, site = argv[0], argv[1]
    overrides = dict(a.split("=", 1) for a in argv[2:])
    try:
        vals = parse_env(site, overrides, warn=lambda m: sys.stderr.write(m + "\n"))
        if is_strict:
            strict(vals)
        out = render(open(template).read(), derive(vals))
    except KeyError as e:
        sys.stderr.write("render.py: %s: unfilled placeholder(s): %s\n" % (template, e.args[0]))
        return 2
    except (ValueError, OSError) as e:
        sys.stderr.write("render.py: %s\n" % e)
        return 2
    sys.stdout.write(out)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
