"""Stable aliases for the names in everything this service sends out.

With `anonymize.enabled` set, no response, websocket frame, export line or
Ask-LLM snapshot carries a real username, a lab or Slurm account name, an email
address or a numeric user ID. Each name becomes an alias cut from an HMAC under
a secret salt -- `user-` or `lab-` and 8 hex characters -- so a reader can still
follow one person across tabs, windows and restarts, and nobody without the
salt can turn an alias back into a name.

WHERE it runs is the design, as with `redact`: at the exits, after the
reducers. Reducers and their saved state keep raw names -- the detectors, the
user filter and the dedup need them -- so switching this on or off is a
restart, never a rebuild. `app.py` sends every JSON response through
`AnonJSONResponse` and calls `payload` itself for the exits that bypass it: the
websocket frames, the export stream and the gzipped trajectory body.

What counts as a name:

* every account in the live roster, every value of a `user` key, and the
  accounts the config names (`filters.drop_global`, `filters.drop_agent`);
* a path segment the home patterns capture (`/n/home08/<name>/`), known or not
  -- which is how a person who never ran anything on the node, but whose files
  an agent read, is caught;
* labs: `--account=X` or `-A X` on a Slurm command line, `<x>_lab` tokens, and
  the `LABS/<lab>/` and `netscratch/<lab>/` segments of those paths.

Names are matched as whole words -- a run of letters, digits and underscores,
or, for a name with a dot or a hyphen, that name between non-alphanumerics --
so `glenn` is not `glen`. A name cut off at the end of a long string (reducers
clip command lines at 160-400 characters) is matched by its prefix.

Requests are anonymous too: `dealias_user` maps an alias back to its account,
and anything that is not a known alias maps to `UNKNOWN`, which matches no
record. A real name typed into a filter finds nothing, so the page cannot be
used to ask whether someone is here.
"""
import bisect
import hashlib
import hmac
import os
import re
import secrets
import threading
import time

UNKNOWN = "user-unknown"          # what an unknown alias, or a real name, maps to
AMBIGUOUS = "user-?"              # a clipped name more than one account starts with
EMAIL_MASK = "[email]"
UID_MASK = "[uid]"

# Words a payload uses as keys or values that an account could plausibly be
# named. Never aliased, so a site with an account called `data` keeps a page
# that works; /api/health counts the accounts this leaves visible.
CLASSES = ("agent", "human", "ide", "unlabeled", "human-vscode")
AGENT_TYPES = ("claude", "claude_code", "codex", "cursor", "vscode", "copilot", "windsurf",
               "continue", "aider", "gemini", "opencode", "goose", "amp", "cline", "zed",
               "qwen", "kiro", "sandbox", "wrapper")
STRUCTURAL = ("all", "admin", "any", "data", "default", "email", "events", "guest", "host",
              "hosts", "info", "lab", "label", "live", "name", "none", "now", "other", "panels",
              "root", "rows", "self", "status", "test", "total", "uid", "unknown", "user", "users",
              "window")

# Home directories and lab trees, as regexes with named groups: `name` is an
# account, `lab` a lab or Slurm account. Overridable in `anonymize.home_patterns`.
HOME_PATTERNS = (
    r"(?<![\w.-])/home/(?P<name>[A-Za-z0-9._-]+)",
    r"/n/home\d+/(?P<name>[A-Za-z0-9._-]+)",
    # FAS lab trees: /n/holylabs/<lab>/Lab/<person>, /n/netscratch/<lab>/Everyone/<person>,
    # /n/holylfs05/LABS/<lab>/Lab/<person>. A project folder in a person's place
    # is masked too, which costs a little readability and leaks nothing.
    r"/(?:LABS|holylabs|netscratch)/(?P<lab>[^/\s\"']+)/(?:Lab|Users|Everyone)/(?P<name>[^/\s\"']+)",
    r"/n/lab_storage/(?P<lab>[^/\s\"']+)/(?P<name>[^/\s\"']+)",
    r"/(?:Users|Everyone)/(?P<name>[^/\s\"']+)",
    # Claude Code names a project folder after its path with every `/`, `_`
    # and `.` turned into `-` (`-home-glen-workspace`), so the same homes and
    # lab trees, in that spelling. Labs there are matched by their known names.
    r"(?<![A-Za-z0-9])-n-home\d+-(?P<name>[A-Za-z0-9]+)",
    r"(?<![A-Za-z0-9])-home-(?P<name>[A-Za-z0-9]+)",
    r"-(?:Lab|Users|Everyone)-(?P<name>[A-Za-z0-9]+)",
    r"-n-lab-storage-[a-z0-9-]*?-lab-(?P<name>[A-Za-z0-9]+)",
)
# A Slurm account on a Slurm command line, and FAS's `<pi>_lab` naming.
# Overridable in `anonymize.lab_patterns`. `-A` alone is also `ls -A` and
# `ssh -A`, hence the anchor on the command.
LAB_PATTERNS = (
    r"(?:sbatch|srun|salloc|sacct|squeue|sshare|sreport|scontrol)\b[^\n]*?"
    r"(?:--account[= ]|(?<!\S)-A ?)(?P<lab>[A-Za-z0-9_]+)",
    r"\b(?P<lab>[a-z0-9]+(?:_[a-z0-9]+)*_lab)\b",
)

# Cheap substring gates, as in `redact`: most strings pass none, and a pattern
# only runs on a string its gate admits.
_PATTERN_GATES = (("home", "LABS", "holylabs", "netscratch", "lab_storage", "lab-storage",
                   "Users", "Everyone", "-Lab-"),
                  ("sbatch", "srun", "salloc", "sacct", "squeue", "sshare", "sreport",
                   "scontrol", "_lab"))

_EMAIL = re.compile(r"([A-Za-z0-9._%+-]+)@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# (gate, pattern): the number in each is the account's uid.
_UIDS = (
    ("claude-", re.compile(r"(/tmp/claude-)\d+")),
    (".slice", re.compile(r"(user-)\d+(?=\.slice)")),
    (".service", re.compile(r"(user@)\d+(?=\.service)")),
    ("/run/user/", re.compile(r"(/run/user/)\d+")),
    ("uid=", re.compile(r"(\buid=)\d+")),
)
_TOKEN = re.compile(r"[A-Za-z0-9_]+")
_TAIL = re.compile(r"(?<![A-Za-z0-9])([A-Za-z0-9]{3,})$")
_ALIAS = re.compile(r"(?:user|lab)-[0-9a-f]{8}")
_NAME_OK = re.compile(r"(?=[^A-Za-z]*[A-Za-z])[A-Za-z0-9_][A-Za-z0-9._-]{1,63}")
CLIPPED_MIN = 100                 # the shortest string a clipped name is looked for in
ROSTER_EVERY_S = 2.0


class Anonymizer:
    """The registry of names seen, and the rewrite every exit applies.

    One per service, shared by the request threads, the live-frame executor and
    the Ask thread: learning a name takes the lock, rewriting reads plain dicts.
    """

    def __init__(self, salt, *, labs=True, emails=True, uids=True, keep=("root",),
                 home_patterns=HOME_PATTERNS, lab_patterns=LAB_PATTERNS,
                 known_users=(), roster=None, salt_source="config"):
        self._salt = salt if isinstance(salt, bytes) else str(salt).encode()
        self.salt_source = salt_source
        self.mask_labs, self.mask_emails, self.mask_uids = bool(labs), bool(emails), bool(uids)
        self.keep = frozenset(keep or ())
        self.reserved = frozenset(CLASSES + AGENT_TYPES + STRUCTURAL) | self.keep
        self._home = [re.compile(p) for p in home_patterns or ()]
        self._labp = [re.compile(p) for p in lab_patterns or ()] if self.mask_labs else []
        self._roster = roster
        self._roster_n = -1
        self._roster_at = 0.0
        self._lock = threading.RLock()
        self.users = {}           # account -> alias
        self.labs = {}            # lab -> alias
        self.back = {}            # alias -> account
        self.system = set()       # accounts seen with a uid below 1000: never aliased
        self.collisions = set()   # aliases two accounts share (counted, not resolved)
        self.reserved_hits = set()
        self.generation = 0
        self._special = None      # (regex, variant -> alias) for names with . or -
        self._sorted = None       # every account, sorted, for the clipped-name lookup
        self.last_ms = {}
        for u in known_users or ():
            self.learn_user(u)

    # ----------------------------------------------------------- aliases
    def _mac(self, kind, name):
        return hmac.new(self._salt, ("%s:%s" % (kind, name)).encode(),
                        hashlib.sha256).hexdigest()[:8]

    def user_alias(self, name):
        """The alias of one account, learning it if new; reserved names stay."""
        a = self.users.get(name)
        if a is None:
            self.learn_user(name)
            a = self.users.get(name)
        return a or name

    # ----------------------------------------------------------- learning
    def learn_user(self, name, uid=None):
        if not isinstance(name, str) or not _NAME_OK.fullmatch(name) or _ALIAS.fullmatch(name):
            return
        with self._lock:
            if isinstance(uid, int) and 0 <= uid < 1000:
                self.system.add(name)
                self._forget(name)
                return
            if name in self.users or name in self.system or name in self.keep:
                return
            if name in self.reserved:
                self.reserved_hits.add(name)
                return
            a = "user-" + self._mac("user", name)
            if self.back.get(a, name) != name:
                self.collisions.add(a)
            self.users[name] = a
            self.back.setdefault(a, name)
            # an account is never also a lab: `LABS/xlin/` where xlin logs in
            self.labs.pop(name, None)
            self.generation += 1
            self._special = self._sorted = None

    def learn_lab(self, name):
        if not self.mask_labs or not isinstance(name, str) or not _NAME_OK.fullmatch(name) \
                or _ALIAS.fullmatch(name) or len(name) < 2:
            return
        with self._lock:
            if name in self.labs or name in self.users or name in self.reserved \
                    or name in self.system:
                return
            self.labs[name] = "lab-" + self._mac("lab", name)
            self.generation += 1
            self._special = None

    def _forget(self, name):
        a = self.users.pop(name, None)
        if a and self.back.get(a) == name:
            del self.back[a]
        if a:
            self.generation += 1
            self._special = self._sorted = None

    def refresh(self, force=False):
        """Learn every account the live roster holds, at most every 2 s."""
        if self._roster is None:
            return
        now = time.monotonic()
        if not force and now - self._roster_at < ROSTER_EVERY_S:
            return
        self._roster_at = now
        try:
            names = list(self._roster())
        except Exception:
            return
        if len(names) != self._roster_n or force:
            self._roster_n = len(names)
            for n in names:
                if n not in self.users:
                    self.learn_user(n)

    def learn_text(self, s):
        """Learn the labs the patterns capture in `s`. Names they capture are
        masked where they stand (`_in_place`), not learned."""
        if not s or not isinstance(s, str):
            return
        for gate, pats in ((_PATTERN_GATES[0], self._home), (_PATTERN_GATES[1], self._labp)):
            if not pats or not any(g in s for g in gate):
                continue
            for rx in pats:
                for m in rx.finditer(s):
                    lab = m.groupdict().get("lab")
                    if lab:
                        self.learn_lab(lab)

    # ----------------------------------------------------------- rewriting
    def _special_names(self):
        """One regex for every name a plain word match cannot see: accounts with
        a `.` or `-` in them, and the `-` spelling of accounts and labs with a
        `.` or `_` (Claude Code's folder names). Longest first."""
        sp = self._special
        if sp is None:
            with self._lock:
                variants = {}
                for names in (self.users, self.labs):
                    for n, a in names.items():
                        if "." in n or "-" in n:
                            variants[n] = a
                        if "." in n or "_" in n:
                            variants.setdefault(re.sub(r"[._]", "-", n), a)
                rx = None
                if variants:
                    alt = "|".join(re.escape(v) for v in sorted(variants, key=len, reverse=True))
                    rx = re.compile(r"(?<![A-Za-z0-9])(" + alt + r")(?![A-Za-z0-9])")
                sp = self._special = (rx, variants)
        return sp

    def _sorted_names(self):
        s = self._sorted
        if s is None:
            with self._lock:
                s = self._sorted = sorted(self.users)
        return s

    def _groups(self, m):
        """The match, with its `name` and `lab` groups swapped for aliases."""
        text, base = m.group(0), m.start()
        spans = []
        for g in ("name", "lab"):
            try:
                v = m.group(g)
            except IndexError:
                continue
            if not v:
                continue
            if g == "name":
                a = self.users.get(v)
                if a is None and m.end(g) == len(m.string) and len(m.string) >= CLIPPED_MIN:
                    a = self._prefix_alias(v)
                a = a or self._in_place(v)
            else:
                a = self.labs.get(v) or self.users.get(v)
                if a is None and self.mask_labs:
                    self.learn_lab(v)
                    a = self.labs.get(v)
            if a:
                spans.append((m.start(g) - base, m.end(g) - base, a))
        for s, e, a in sorted(spans, reverse=True):
            text = text[:s] + a + text[e:]
        return text

    def _in_place(self, name):
        """The alias of a name a path pattern captured, WITHOUT learning it.

        `/n/home08/bowen/` is someone's home whether or not bowen ever ran
        anything here, so the segment is always masked. But a pattern can also
        catch an ordinary word (`/home/logs`), and a learned name is masked
        everywhere it appears as a word -- so only accounts the roster or a
        `user` field vouches for are learned."""
        if not _NAME_OK.fullmatch(name) or _ALIAS.fullmatch(name) or name in self.keep \
                or name in self.reserved or name in self.system:
            return None
        return "user-" + self._mac("user", name)

    def _token(self, m):
        t = m.group(0)
        a = self.labs.get(t) or self.users.get(t)
        if a:
            return a
        if "_" in t:
            parts = t.split("_")
            hit = False
            for i, p in enumerate(parts):
                a = self.users.get(p)
                if a:
                    parts[i], hit = a, True
            if hit:
                return "_".join(parts)
        return t

    def _rewrite(self, s):
        """Every mask, in an order where each step only sees what the earlier
        ones left: uids before emails (`user@2004.service` is no address);
        known names before the path patterns, so a pattern only ever meets a
        name nobody vouched for -- and masks it where it stands."""
        if not s:
            return s
        orig_len = len(s)
        if self.mask_uids:
            for gate, rx in _UIDS:
                if gate in s:
                    s = rx.sub(lambda m: m.group(1) + UID_MASK, s)
        if self.mask_emails and "@" in s:
            s = _EMAIL.sub(lambda m: m.group(0) if m.group(1).lower() == "git" else EMAIL_MASK, s)
        rx, variants = self._special_names()
        if rx is not None and ("." in s or "-" in s):
            s = rx.sub(lambda m: variants[m.group(1)], s)
        s = _TOKEN.sub(self._token, s)
        if orig_len >= CLIPPED_MIN:
            s = self._clipped(s)
        for gate, pats in ((_PATTERN_GATES[0], self._home), (_PATTERN_GATES[1], self._labp)):
            if pats and any(g in s for g in gate):
                for rx in pats:
                    s = rx.sub(self._groups, s)
        return s

    def _prefix_alias(self, t):
        """The alias of the one account `t` begins, `AMBIGUOUS` when several
        do, None when none does."""
        if len(t) < 3:
            return None
        names = self._sorted_names()
        i = bisect.bisect_left(names, t)
        hits = []
        while i < len(names) and names[i].startswith(t) and len(hits) < 2:
            hits.append(names[i])
            i += 1
        if not hits:
            return None
        return self.users[hits[0]] if len(hits) == 1 else AMBIGUOUS

    def _clipped(self, s):
        m = _TAIL.search(s)
        if not m:
            return s
        t = m.group(1)
        if t in self.users or t in self.labs or s[:m.start()].endswith(("user-", "lab-")):
            return s
        a = self._prefix_alias(t)
        return s[:m.start()] + a if a else s

    def text(self, s):
        """`s` with every name masked; learns what its patterns capture first."""
        if not s or not isinstance(s, str):
            return s
        self.learn_text(s)
        return self._rewrite(s)

    # ----------------------------------------------------------- payloads
    def _collect(self, obj):
        stack = [obj]
        while stack:
            o = stack.pop()
            if isinstance(o, dict):
                u = o.get("user")
                if isinstance(u, str):
                    uid = o.get("uid")
                    self.learn_user(u, uid if isinstance(uid, int) else None)
                us = o.get("users")
                if isinstance(us, (list, tuple)):
                    for x in us:
                        if isinstance(x, str):
                            self.learn_user(x)
                for k, v in o.items():
                    if isinstance(k, str) and ("/" in k or "_lab" in k):
                        self.learn_text(k)
                    if isinstance(v, str):
                        self.learn_text(v)
                    elif isinstance(v, (dict, list, tuple, set, frozenset)):
                        stack.append(v)
            elif isinstance(o, (list, tuple, set, frozenset)):
                for v in o:
                    if isinstance(v, str):
                        self.learn_text(v)
                    elif isinstance(v, (dict, list, tuple, set, frozenset)):
                        stack.append(v)

    def _key(self, k):
        """A dict key: an exact account or lab is aliased; a key that is a path
        or an address is rewritten like any string; an identifier-shaped key is
        left alone, which keeps `rows` and `by_class` what the page reads."""
        if not isinstance(k, str):
            return k
        a = self.users.get(k) or (self.labs.get(k) if self.mask_labs else None)
        if a:
            return a
        if k.isidentifier():
            return k
        return self._rewrite(k)

    def _walk(self, o):
        if isinstance(o, str):
            return self._rewrite(o)
        if isinstance(o, dict):
            out = {}
            for k, v in o.items():
                if self.mask_uids and k in ("uid", "uids"):
                    out[k] = None
                else:
                    out[self._key(k)] = self._walk(v)
            return out
        if isinstance(o, list):
            return [self._walk(v) for v in o]
        if isinstance(o, tuple):
            return tuple(self._walk(v) for v in o)
        if isinstance(o, (set, frozenset)):
            return [self._walk(v) for v in o]
        return o

    def payload(self, obj, label=None):
        """A deep copy of a JSON-shaped object with every name masked. Two
        passes: the first learns every name the object holds, so a name in one
        string is masked even when the string that introduces it comes later."""
        t0 = time.perf_counter()
        self.refresh()
        self._collect(obj)
        out = self._walk(obj)
        if label:
            self.last_ms[label] = round(1000 * (time.perf_counter() - t0), 1)
        return out

    def record(self, rec):
        """One raw record, for /api/events and /api/export."""
        return self.payload(rec)

    # ----------------------------------------------------------- requests
    def dealias_user(self, v):
        """The account behind an alias; `UNKNOWN` for anything else -- a real
        name included, so a filter cannot be used to probe for one."""
        if not isinstance(v, str) or not v:
            return v
        n = self.back.get(v)
        if n is None and _ALIAS.fullmatch(v):
            self.refresh(force=True)
            n = self.back.get(v)
        return n if n is not None else UNKNOWN

    def status(self):
        """For /api/health: counts only, never a name."""
        return {"enabled": True, "salt_source": self.salt_source,
                "users_known": len(self.users), "labs_known": len(self.labs),
                "system_accounts": len(self.system),
                "alias_collisions": len(self.collisions),
                "reserved_collisions": len(self.reserved_hits),
                "last_pass_ms": dict(self.last_ms)}


# ---------------------------------------------------------------- set-up
def load_salt(cfg):
    """`(salt bytes, source)`. `anonymize.salt` when set; else the hex in
    `<cache.dir>/anonymize.salt`, made once (mode 0600) when missing. A salt
    file that exists but cannot be read is an error: serving with no salt, or a
    new one, would hand out aliases nobody asked for."""
    s = cfg.get("anonymize.salt") or ""
    if s:
        return str(s).encode(), "config"
    d = cfg.get("cache.dir") or "."
    path = os.path.join(d, "anonymize.salt")
    try:
        with open(path) as fh:
            got = fh.read().strip()
        if got:
            return bytes.fromhex(got), "file"
    except FileNotFoundError:
        pass
    os.makedirs(d, exist_ok=True)
    salt = secrets.token_bytes(32)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:           # another instance won the race: use theirs
        return load_salt(cfg)
    with os.fdopen(fd, "w") as fh:
        fh.write(salt.hex() + "\n")
    return salt, "generated"


def from_config(cfg, roster=None):
    """The service's anonymizer, or None when `anonymize.enabled` is off."""
    if not cfg.get("anonymize.enabled"):
        return None
    salt, source = load_salt(cfg)
    known = list(cfg.get("filters.drop_global") or ()) + list(cfg.get("filters.drop_agent") or ())
    return Anonymizer(
        salt,
        labs=cfg.get("anonymize.labs", True),
        emails=cfg.get("anonymize.emails", True),
        uids=cfg.get("anonymize.uids", True),
        keep=cfg.get("anonymize.keep") or ("root",),
        home_patterns=cfg.get("anonymize.home_patterns") or HOME_PATTERNS,
        lab_patterns=cfg.get("anonymize.lab_patterns") or LAB_PATTERNS,
        known_users=known, roster=roster, salt_source=source)
