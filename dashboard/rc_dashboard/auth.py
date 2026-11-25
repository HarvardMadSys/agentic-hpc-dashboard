"""Sign-in for the whole service: the page, its assets, every /api route, and /ws.

Two ways in, either or both: the site's single sign-on, through a proxy in
front of the service (`Proxy`, the last item below), and one password, shared
by everyone who reads this deployment. There are no accounts. The service
keeps only a salted scrypt hash of the password: in a file of its own,
`server.auth.password_hash_file` (RC_DASH_PASSWORD_HASH_FILE), or given
outright as `server.auth.password_hash` (RC_DASH_PASSWORD_HASH); never both.
With no password and no proxy sign-in, `serve` refuses to start unless told
to serve open.

* **The hash file** holds the hash on one line (quotes around it, blank lines
  and `#` lines are passed over). It is read again when it changes -- checked
  on every sign-in attempt (the form, and each request with a Basic header), and
  at most once a second on any other request --
  and on SIGHUP, so a new password (`rc-dashboard new-password --install FILE`)
  needs no restart. A changed hash makes a new cookie key, so everyone signs in
  again. A file that cannot be read, or holds no hash, leaves the hash in use
  as it is (and says so on stderr) until it is fixed; at start it stops the
  service, as a malformed hash does.

* **The form.** A reader who has not signed in gets a small form, with a 401,
  at whatever URL they asked for, so a link to a tab still lands on that tab.
  The form posts to /login, which checks the password and sends the browser
  back with a cookie. A request under /api, or any method but GET and HEAD,
  gets a 401 JSON body instead. A websocket is refused before it is accepted,
  which uvicorn answers with a 403.
* **The cookie.** `rc_dash_<port>`, so two instances on one host name
  (127.0.0.1:8085 and :8086) keep separate ones. It holds an expiry and an HMAC
  of it, and lasts `server.auth.session_days`. It is HttpOnly and SameSite=Lax,
  and Secure when the request came over https: cloudflared says so in
  X-Forwarded-Proto, which uvicorn trusts from 127.0.0.1. The HMAC key comes
  from a random key in <cache.dir>/session.key, made once with mode 0600, and
  from the password hash, so a new password or a deleted key file signs
  everyone out.
* **Scripts.** HTTP Basic auth works too, with any user name: `curl
  --netrc-file`. The service never asks for it with a WWW-Authenticate header,
  or the browser would put its own dialog over the page's background requests.
* **Cost.** scrypt at n=2**15, r=8: 32 MiB and about 70 ms here. One check runs
  at a time, off the event loop, and a wrong password waits a second more. A
  Basic header that passed once is remembered, so a script's later requests
  cost an HMAC, not a scrypt.
* **Single sign-on** (`server.auth.proxy_header`, RC_DASH_PROXY_HEADER): a
  reverse proxy -- oauth2-proxy, Apache's mod_auth_openidc, Open OnDemand --
  signs the reader in with the site's own sign-on and names them in that
  header. It is believed only when the request also carries the proxy's
  secret, once, in X-RC-Proxy-Secret (SECRET_HEADER), the one in
  `server.auth.proxy_secret_file`: an address alone is no proof, since every
  account on the dashboard's host connects from 127.0.0.1 and could name any
  user. And only when the connection's own address is in
  `server.auth.proxy_trusted` (127.0.0.1 and ::1 unless set), never an address
  a header gives (`Peer`), and only when it names one user, once, in at most
  MAX_PROXY_USER characters, none of them a control character; with
  `server.auth.proxy_groups`, the header `server.auth.proxy_groups_header`
  names must list one of those groups. From any other address the header is
  as if it were not there. A request the proxy does not sign in falls back to
  the password, if there is one: the cookie, the Basic header and the form,
  as above. With none it gets a 401 saying the page is behind the site's
  sign-on, or a 403 for a user in none of the groups, in plain text; a
  websocket is refused, as above.

`rc-dashboard new-password` makes a password, writes it and a netrc to files only
their owner can read, and writes the hash to a hash file (`--install`) or prints
it. Nothing here prints, logs, or sends the password itself.
"""
import asyncio
import base64
import binascii
import hashlib
import hmac
import html
import ipaddress
import os
import re
import secrets
import string
import sys
import tempfile
import time
import unicodedata
import urllib.parse
from collections import namedtuple

from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response

# string.punctuation without what breaks a shell line, a netrc, or a copy out
# of a terminal: quotes, backslash, #, $, !, &, |, ;, <, >, and parentheses.
# With both cases and the digits that is 80 characters, so 16 of them carry
# 16 * log2(80) = 101 bits.
SYMBOLS = "%*+,-./:=?@[]^_{}~"
CLASSES = (string.ascii_lowercase, string.ascii_uppercase, string.digits, SYMBOLS)
ALPHABET = "".join(CLASSES)

SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 15, 8, 1
# A stored hash names its own cost. Past this much memory it is a typo, not a
# choice, and every sign-in would allocate it.
MAX_SCRYPT_BYTES = 1 << 30
MAX_PASSWORD = 1024             # longer is not a password anyone typed
MAX_FORM = 8192                 # the sign-in form's body
MAX_SESSION_DAYS = 365

LOGIN, LOGOUT = "/login", "/logout"
KEY_FILE = "session.key"
DEFAULT_DIR = "~/.config/rc-dashboard"
# `rc-dashboard init`'s hash file, beside the password it makes.
DEFAULT_HASH_FILE = "~/.config/rc-dashboard/password.hash"
MAX_HASH_FILE = 4096            # more is not a file holding one hash
HASH_FILE_MODE = 0o640          # the service's group may read it (deploy/)
CHECK_EVERY_S = 1.0             # how often a request but a sign-in looks at the file
NO_STORE = {"cache-control": "no-store"}
PAGE_HEADERS = {"cache-control": "no-store", "x-frame-options": "DENY"}


# ----------------------------------------------------------- the password
def generate_password(length=16):
    """`length` characters from ALPHABET, drawn with `secrets`, at least one
    from each class. A draw that misses a class is drawn again whole, so every
    password that has all four is equally likely."""
    if length < len(CLASSES):
        raise ValueError("a password needs at least %d characters" % len(CLASSES))
    while True:
        pw = "".join(secrets.choice(ALPHABET) for _ in range(length))
        if all(any(c in cls for c in pw) for cls in CLASSES):
            return pw


def _b64(b):
    return base64.b64encode(b).decode("ascii")


def _scrypt(password, salt, n, r, p, dklen):
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p,
                          maxmem=128 * r * (n + p + 2) + (1 << 20), dklen=dklen)


def hash_password(password, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P):
    """`scrypt$n$r$p$salt$key`, salt and key in base64: what the config keeps
    in place of the password."""
    salt = secrets.token_bytes(16)
    return "scrypt$%d$%d$%d$%s$%s" % (n, r, p, _b64(salt), _b64(_scrypt(password, salt, n, r, p, 32)))


def parse_hash(stored):
    """`(n, r, p, salt, key)` from a stored hash, or ValueError saying what is
    wrong with it."""
    parts = str(stored or "").split("$")
    if len(parts) != 6 or parts[0] != "scrypt":
        raise ValueError("not a scrypt$n$r$p$salt$key hash")
    try:
        n, r, p = (int(x) for x in parts[1:4])
        salt = base64.b64decode(parts[4], validate=True)
        key = base64.b64decode(parts[5], validate=True)
    except (ValueError, binascii.Error):
        raise ValueError("a field of the hash does not parse")
    if n < 2 or n & (n - 1) or r < 1 or not 1 <= p <= 16:
        raise ValueError("scrypt's n must be a power of 2, r at least 1, and p 1 to 16")
    if 128 * r * (n + p + 2) > MAX_SCRYPT_BYTES:
        raise ValueError("this scrypt cost needs more than 1 GiB per sign-in")
    if len(salt) < 8 or len(key) < 16:
        raise ValueError("the salt or the key is too short")
    return n, r, p, salt, key


def verify_password(password, stored):
    """True when `password` is the one `stored` was made from. The comparison
    takes the same time either way, and a malformed hash never matches."""
    try:
        n, r, p, salt, key = parse_hash(stored)
    except ValueError:
        return False
    if not isinstance(password, str) or len(password) > MAX_PASSWORD:
        return False
    try:
        got = _scrypt(password, salt, n, r, p, len(key))
    except (ValueError, MemoryError):
        return False
    return hmac.compare_digest(got, key)


def wall():
    """The real time. clock.py pins `time.time` to a capture's end on a frozen
    instance, and a cookie must still run out there."""
    return time.clock_gettime(time.CLOCK_REALTIME)


def read_hash_file(path):
    """The hash a hash file holds: its one line, without the whitespace or the
    quotes around it (blank lines and `#` lines passed over). ValueError, saying
    what is wrong, when it holds no hash, more than one line, or a hash that does
    not parse; OSError when it cannot be read."""
    with open(path, encoding="utf-8", errors="replace") as fh:
        text = fh.read(MAX_HASH_FILE + 1)
    if len(text) > MAX_HASH_FILE:
        raise ValueError("%s is over %d bytes; a hash file holds one hash" % (path, MAX_HASH_FILE))
    lines = [ln.strip() for ln in text.splitlines()]
    lines = [ln for ln in lines if ln and not ln.startswith("#")]
    if not lines:
        raise ValueError("%s holds no hash" % path)
    if len(lines) > 1:
        raise ValueError("%s holds %d lines; a hash file holds the hash on one line"
                         % (path, len(lines)))
    stored = lines[0]
    if len(stored) >= 2 and stored[0] == stored[-1] and stored[0] in "\"'":
        stored = stored[1:-1].strip()
    try:
        parse_hash(stored)
    except ValueError as e:
        raise ValueError("%s: %s" % (path, e))
    return stored


def _file_id(path):
    """What says a file was replaced or changed: its inode, mtime and size."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_dev, st.st_ino, st.st_mtime_ns, st.st_size)


def _say(msg):
    try:
        sys.stderr.write("sign-in: %s\n" % msg)
        sys.stderr.flush()
    except (OSError, ValueError):
        pass


def load_session_key(cache_dir):
    """32 random bytes, kept as hex in <cache_dir>/session.key and made once
    with mode 0600. Deleting the file signs everyone out."""
    d = cache_dir or "."
    path = os.path.join(d, KEY_FILE)
    try:
        with open(path) as fh:
            got = fh.read().strip()
    except FileNotFoundError:
        got = None
    if got is not None:
        try:
            key = bytes.fromhex(got)
        except ValueError:
            key = b""
        if len(key) < 16:
            raise SystemExit("%s is not a session key. Delete it and a new one is made; "
                             "everyone then signs in again." % path)
        return key
    os.makedirs(d, exist_ok=True)
    key = secrets.token_bytes(32)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:             # another instance won the race: use theirs
        return load_session_key(cache_dir)
    with os.fdopen(fd, "w") as fh:
        fh.write(key.hex() + "\n")
    return key


def read_cookie(header, name):
    """One cookie's value out of a Cookie header, or None. Read by hand:
    http.cookies gives up on the whole header over one odd cookie beside ours."""
    for part in (header or "").split(";"):
        k, sep, v = part.strip().partition("=")
        if sep and k == name:
            return v.strip()
    return None


def safe_next(raw):
    """Where to send a reader once they have signed in: a path on this site,
    or "/". Never another site (`//host`, a scheme, or a backslash, which a
    browser reads as a slash), never a header break, and never the sign-in or
    sign-out URL itself."""
    if not isinstance(raw, str) or not raw.startswith("/") or raw.startswith("//") \
            or "\\" in raw or not raw.isascii() or any(c < " " or c == "\x7f" for c in raw):
        return "/"
    if raw.split("?", 1)[0].split("#", 1)[0] in (LOGIN, LOGOUT):
        return "/"
    return raw


# --------------------------------------------------------------- the gate
class Gate:
    """Who may come in: the password check, the session cookie, and the Basic
    header for scripts. With `hash_file`, the hash is that file's, read again
    when it changes (`refresh`, `reload`)."""

    def __init__(self, stored, key, cookie_name, lifetime_s, now=None, fail_delay_s=1.0,
                 hash_file=None, check_every_s=CHECK_EVERY_S):
        parse_hash(stored)
        self._session_key = key
        self.cookie_name = cookie_name
        self.lifetime_s = int(lifetime_s)
        self.now = now or wall
        self.fail_delay_s = fail_delay_s
        self.hash_file = hash_file
        self.check_every_s = check_every_s
        self._file = None
        self._checked = time.monotonic()
        self.reloads = 0                # times a changed hash was taken up
        self.reload_error = None        # why the file could not be read, last time
        self._lock = self._loop = None
        self._use(stored)
        if hash_file:
            # Read again, its identity first (`reload`): `stored` was read before the
            # session key was loaded, and the file's identity taken after it paired a
            # file replaced in between with the old hash, so no later look saw a
            # change, and the revoked password stayed good until a SIGHUP or a restart.
            self.reload()

    def _key_for(self, stored):
        # Per deployment and per password: a cookie made under another key
        # file or another hash is not valid here.
        return hmac.new(self._session_key, b"rc-dashboard session\0" + stored.encode(),
                        hashlib.sha256).digest()

    def _use(self, stored):
        self.stored = stored
        self.key = self._key_for(stored)
        self._passed = set()            # Basic headers that passed once, as HMACs

    # -- the hash file
    def refresh(self, throttle=True):
        """Read the hash file again if it changed since it was read (by its
        inode, mtime and size): at most once every `check_every_s` with
        `throttle`, else now. Returns whether the hash changed."""
        if not self.hash_file:
            return False
        now = time.monotonic()
        if throttle and now - self._checked < self.check_every_s:
            return False
        self._checked = now
        if _file_id(self.hash_file) == self._file:
            return False
        return self.reload()

    def reload(self):
        """Read the hash file now (SIGHUP, or a change `refresh` saw). A new
        hash makes a new cookie key, so everyone signs in again. A file that
        cannot be read, or holds no hash, leaves the hash as it is, says why on
        stderr (once per reason), and is tried again at the next look. Returns
        whether the hash changed."""
        if not self.hash_file:
            return False
        ident = _file_id(self.hash_file)
        try:
            stored = read_hash_file(self.hash_file)
        except OSError as e:
            why = "%s: %s" % (self.hash_file, e.strerror or e)
        except ValueError as e:
            why = str(e)
        else:
            why = None
        if why is not None:
            if why != self.reload_error:
                _say("the hash file could not be read, so the password stays as it was: %s"
                     % why)
            self.reload_error = why
            return False
        self._file, self.reload_error = ident, None
        if stored == self.stored:
            return False
        self._use(stored)
        self.reloads += 1
        _say("a new password hash from %s: everyone signs in again" % self.hash_file)
        return True

    # -- the cookie
    def _mac(self, exp, key=None):
        d = hmac.new(key or self.key, b"%d" % exp, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(d).decode("ascii").rstrip("=")

    def token(self, key=None):
        """A cookie's token, signed with `key`: by default the key of the hash in use."""
        exp = int(self.now()) + self.lifetime_s
        return "%d.%s" % (exp, self._mac(exp, key))

    def valid(self, tok):
        """True for a token this gate made that has not run out. One made for
        a longer lifetime than the one set now is cut to it."""
        if not isinstance(tok, str):
            return False
        exp, sep, mac = tok.partition(".")
        if not sep or not (exp.isascii() and exp.isdigit()) or len(exp) > 12:
            return False
        exp = int(exp)
        if not hmac.compare_digest(mac.encode(), self._mac(exp).encode()):
            return False
        now = self.now()
        return now < exp <= now + self.lifetime_s + 60

    def _cookie(self, value, max_age, secure):
        c = "%s=%s; Path=/; Max-Age=%d; HttpOnly; SameSite=Lax" % (self.cookie_name, value, max_age)
        return c + "; Secure" if secure else c

    def set_cookie(self, tok, secure):
        return self._cookie(tok, self.lifetime_s, secure)

    def clear_cookie(self, secure):
        return self._cookie("", 0, secure)

    # -- the password
    def _lock_here(self):
        loop = asyncio.get_running_loop()
        if self._loop is not loop:
            self._lock, self._loop = asyncio.Lock(), loop
        return self._lock

    async def _opens(self, password):
        """The hash `password` was checked against and opens, or None. scrypt, one
        check at a time and off the event loop. A wrong password waits `fail_delay_s`
        more, so guessing is slow as well as hopeless. The hash file is looked at
        first, every time. A hash replaced while scrypt ran (SIGHUP, or a change
        another request saw) is a no: the answer was for a password that may no
        longer open the page, and whoever gave it signs in again."""
        self.refresh(throttle=False)
        opened = None
        if password and len(password) <= MAX_PASSWORD:
            async with self._lock_here():
                stored = self.stored
                if (await asyncio.to_thread(verify_password, password, stored)
                        and stored == self.stored):
                    opened = stored
        if opened is None and self.fail_delay_s:
            await asyncio.sleep(self.fail_delay_s)
        return opened

    async def check_password(self, password):
        """Whether `password` opens the page (`_opens`)."""
        return await self._opens(password) is not None

    async def sign_in(self, password):
        """The cookie token `password` earns, or None. Signed with the key of the hash
        it was checked against: a hash replaced at any moment after the check leaves a
        token the gate refuses, never a session a revoked password opened."""
        stored = await self._opens(password)
        return None if stored is None else self.token(self._key_for(stored))

    async def check_basic(self, value):
        """`Authorization: Basic <value>`, with the password after any user name.
        Each is a sign-in, so the hash file is looked at first, every time."""
        self.refresh(throttle=False)
        seen = hmac.new(self.key, value.encode("latin-1", "replace"), hashlib.sha256).digest()
        if seen in self._passed:
            return True
        try:
            raw = base64.b64decode(value, validate=True).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return False
        _, sep, password = raw.partition(":")
        if not sep or not await self.check_password(password):
            return False
        if len(self._passed) >= 64:
            self._passed.clear()
        self._passed.add(seen)
        return True

    async def allowed(self, scope):
        """The request carries a valid cookie or the password."""
        self.refresh()
        cookies, basic = [], None
        for k, v in scope.get("headers") or ():
            if k == b"cookie":
                cookies.append(v.decode("latin-1"))
            elif k == b"authorization":
                a = v.decode("latin-1")
                if a[:6].lower() == "basic ":
                    basic = a[6:].strip()
        if cookies and self.valid(read_cookie("; ".join(cookies), self.cookie_name)):
            return True
        return bool(basic) and await self.check_basic(basic)


def password_settings(cfg):
    """`(hash, hash file)` as the config sets them, each "" when unset. Both set
    stops the service: which password opens the page must not be a guess."""
    stored = str(cfg.get("server.auth.password_hash") or "").strip()
    path = str(cfg.get("server.auth.password_hash_file") or "").strip()
    if stored and path:
        origin = getattr(cfg, "origin", lambda k: "?")
        raise SystemExit(
            "server.auth.password_hash (set by %s) and server.auth.password_hash_file (set by "
            "%s) are both set, and only one may say which password opens the page. Keep the "
            "file (RC_DASH_PASSWORD_HASH_FILE), which a new password replaces without a "
            "restart, and drop the hash (RC_DASH_PASSWORD_HASH); or keep the hash alone."
            % (origin("server.auth.password_hash"), origin("server.auth.password_hash_file")))
    return stored, os.path.expanduser(path) if path else ""


def has_password(cfg):
    """Whether the config gives the service a password, inline or in a file."""
    stored, path = password_settings(cfg)
    return bool(stored or path)


def from_config(cfg):
    """The service's gate, or None when no password is set. A hash that does
    not parse, a hash file that cannot be read or holds none, both a hash and a
    hash file, or a lifetime that is not a sane number of days, stops the
    service: starting open, or locked for good, would be worse."""
    stored, path = password_settings(cfg)
    if not stored and not path:
        return None
    if path:
        try:
            stored = read_hash_file(path)
        except OSError as e:
            raise SystemExit("server.auth.password_hash_file %s: %s. Make it with "
                             "`rc-dashboard new-password --install %s`." % (
                                 path, e.strerror or e, path))
        except ValueError as e:
            raise SystemExit("server.auth.password_hash_file: %s. Make it again with "
                             "`rc-dashboard new-password --install %s --force`." % (e, path))
    else:
        try:
            parse_hash(stored)
        except ValueError as e:
            raise SystemExit("server.auth.password_hash: %s. Make one with "
                             "`rc-dashboard new-password`." % e)
    days = cfg.get("server.auth.session_days", 30)
    try:
        lifetime = int(float(days) * 86400)
    except (TypeError, ValueError, OverflowError):
        lifetime = 0
    if not 0 < lifetime <= MAX_SESSION_DAYS * 86400:
        raise SystemExit("server.auth.session_days: %r is not a number of days above 0 "
                         "and at most %d" % (days, MAX_SESSION_DAYS))
    return Gate(stored, load_session_key(cfg.get("cache.dir") or "."),
                cookie_name="rc_dash_%s" % cfg.get("server.port"), lifetime_s=lifetime,
                hash_file=path or None)


# --------------------------------------------------------- single sign-on
MAX_PROXY_USER = 256            # characters: longer is not a name a proxy sets
MAX_PROXY_GROUPS = 1 << 16      # bytes of the groups header
# An HTTP header's name: RFC 9110's token.
HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
SECRET_HEADER_NAME = "x-rc-proxy-secret"   # the proxy's secret's header (`Proxy.signs_in`)
# Headers that say something other than who signed in, set by the client or the
# HTTP layer, or by uvicorn's and a proxy's own forwarding: none is the proxy's
# sign-in header.
NOT_PROXY_HEADERS = frozenset((
    "host", "cookie", "authorization", "origin", "referer", "user-agent", "content-type",
    "content-length", "forwarded", "x-forwarded-for", "x-forwarded-host",
    "x-forwarded-proto", "x-real-ip", SECRET_HEADER_NAME))
# What parts one group from the next, in the groups header and in RC_DASH_PROXY_GROUPS.
GROUP_SEP = re.compile(r"[\s,]+")
# What no name holds: a control or format character (a bidi override among them),
# or a line or paragraph separator.
NOT_IN_NAMES = ("Cc", "Cf", "Zl", "Zp")
_ASCII_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
PEER = "rc_dashboard.peer"      # the scope's key for the connection's own address (`Peer`)
# The header the proxy puts its secret in, on every request it passes on, and the
# shortest secret taken: one a guess cannot reach.
SECRET_HEADER = SECRET_HEADER_NAME.encode("latin-1")
MIN_SECRET = 32
MAX_SECRET_FILE = 4096
# What a secret is made of: visible ASCII, which every proxy writes into a header as is.
SECRET_CHARS = re.compile(r"[\x21-\x7e]+")
READER = "rc_dashboard.reader"  # and for how the reader got in (`AuthMiddleware`)
SAY_EVERY_S = 60.0              # how often a header from an address not trusted is said

# What the proxy said of a request (`Proxy.signs_in`): `why` is None when it
# signed `user` in.
Signed = namedtuple("Signed", "user why")


def _odd(text):
    """Whether `text` holds a character no name holds (NOT_IN_NAMES)."""
    if text.isascii():
        return _ASCII_CONTROL.search(text) is not None
    return any(unicodedata.category(c) in NOT_IN_NAMES for c in text)


def _header_text(raw):
    """A header's value as text, UTF-8 or else Latin-1 (HTTP's own), without the
    spaces and tabs around it."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    return text.strip(" \t")


def shown(name):
    """A name as the service sends it out: with what reads as a credential
    masked (redact.py), as everything else it sends is."""
    from . import redact
    return redact.redact(name) if name else name


class Proxy:
    """The site's single sign-on, through a reverse proxy in front of the
    service (the module's docstring). `trusted` holds ip_network objects."""

    def __init__(self, header, trusted, groups_header="", groups=(), logout_url="",
                 secret=b""):
        if len(secret) < MIN_SECRET:
            raise ValueError("the proxy's secret is under %d characters" % MIN_SECRET)
        self.header = header
        self.secret = secret
        self.trusted = tuple(trusted)
        self.groups_header = groups_header
        self.groups = frozenset(groups)
        self.logout_url = logout_url
        self._user = header.lower().encode("latin-1")
        self._groups = groups_header.lower().encode("latin-1") if groups_header else None
        self._said = None               # when a header from an untrusted address was said
        self._said_secret = None        # when a user named without the secret was said

    def trusts(self, addr):
        """Whether `addr`, the address a connection came from, is in `trusted`.
        An IPv4 address an IPv6 socket reports (::ffff:a.b.c.d) is that IPv4
        address; anything that is not an address is not trusted."""
        if not isinstance(addr, str):
            return False
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            return False
        if ip.version == 6 and ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        return any(ip in net for net in self.trusted)

    def signs_in(self, scope):
        """`Signed(user, why)`: `why` is None when the proxy signed this request
        in as `user`. Else why not: "peer", the connection is not from a trusted
        address, so its headers are as if absent; "missing", no user named;
        "bad", a name not taken -- empty, longer than MAX_PROXY_USER, holding a
        control character, or the header twice, since one may be the client's;
        "group", `user` is in none of `groups`; "secret", the request does not
        carry the proxy's secret once (SECRET_HEADER), so its headers are as if
        absent. The connection's address is the one `Peer` kept: never a
        header's. Header names match in any case."""
        peer = scope.get(PEER)
        addr = peer[0] if peer else None
        users, groups, secrets_ = [], [], []
        for k, v in scope.get("headers") or ():
            k = k.lower()
            if k == self._user:
                users.append(v)
            elif k == self._groups:
                groups.append(v)
            elif k == SECRET_HEADER:
                secrets_.append(v)
        if not self.trusts(addr):
            if users:
                self._untrusted(addr)
            return Signed(None, "peer")
        # compare_digest in full either way: how long a wrong secret took says nothing
        if len(secrets_) != 1 or not hmac.compare_digest(secrets_[0].strip(b" \t"),
                                                          self.secret):
            if users:
                self._no_secret(addr)
            return Signed(None, "secret")
        if not users:
            return Signed(None, "missing")
        user = ""
        if len(users) == 1 and len(users[0]) <= 4 * MAX_PROXY_USER:
            user = _header_text(users[0])
        if not user or len(user) > MAX_PROXY_USER or _odd(user):
            return Signed(None, "bad")
        if self.groups and not self.in_group(groups):
            return Signed(user, "group")
        return Signed(user, None)

    def in_group(self, values):
        """Whether the groups header -- `values`, each copy of it -- lists one of
        `groups`, exactly as written. It must come once, since a second copy may
        be the client's, and hold no control character."""
        if len(values) != 1 or len(values[0]) > MAX_PROXY_GROUPS:
            return False
        names = [g for g in GROUP_SEP.split(_header_text(values[0])) if g]
        if _odd("".join(names)):
            return False
        return not self.groups.isdisjoint(names)

    def _untrusted(self, addr):
        """Say, at most once a minute, that a request from an address not in
        `trusted` named a user, which was passed over: what a proxy connecting
        from an address no one listed looks like from here."""
        now = time.monotonic()
        if self._said is not None and now - self._said < SAY_EVERY_S:
            return
        self._said = now
        _say("a request from %s named a user in %s, which was passed over: that address is "
             "not in server.auth.proxy_trusted (said at most once a minute)"
             % (addr or "an unknown address", self.header))

    def _no_secret(self, addr):
        """Say, at most once a minute, that a request named a user without the
        proxy's secret, which was passed over: another account on this host, or
        a proxy not set to send it."""
        now = time.monotonic()
        if self._said_secret is not None and now - self._said_secret < SAY_EVERY_S:
            return
        self._said_secret = now
        _say("a request from %s named a user in %s without the proxy's secret in "
             "X-RC-Proxy-Secret, so it was passed over: the proxy must send it on every "
             "request (said at most once a minute)" % (addr or "an unknown address", self.header))

    def describe(self):
        """Who is believed, and of what, for the start-up log."""
        who = ", ".join(str(n.network_address) if n.num_addresses == 1 else str(n)
                        for n in self.trusted)
        groups = ("a user in %s, by %s" % (", ".join(sorted(self.groups)), self.groups_header)
                  if self.groups else "any user it names")
        return "the user %s names, sent from %s with the proxy's secret; %s; %s" % (
            self.header, who, groups,
            "Sign out goes to %s" % self.logout_url if self.logout_url else "no Sign out")


def has_proxy(cfg):
    """Whether the config turns proxy sign-in on (`server.auth.proxy_header`)."""
    return bool(str(cfg.get("server.auth.proxy_header") or "").strip())


def _logout_ok(url):
    """A path on this site (not `//host`, nor a backslash, which a browser reads
    as a slash), or an http(s) URL with a host; never a space or a control
    character."""
    if "\\" in url or any(c.isspace() for c in url) or _odd(url):
        return False
    if url.startswith("/"):
        return not url.startswith("//")
    parts = urllib.parse.urlsplit(url)
    return parts.scheme in ("http", "https") and bool(parts.netloc)


def proxy_from_config(cfg):
    """The proxy sign-in, or None when `server.auth.proxy_header` is empty. A
    setting it could not work with stops the service, saying why: a header name
    that is not one, or says something else (NOT_PROXY_HEADERS); no trusted
    address, or one that is not an address or a network (no name, since a name
    is only as good as DNS); a group with a space or a comma in it, or groups
    and no header to read them from; a sign-out URL that is neither a path here
    nor an http(s) URL, or that holds what reads as a credential (redact.py):
    every reader is sent it. Each of its other settings, given while it is off,
    is named in a warning."""
    key = "server.auth."
    origin = getattr(cfg, "origin", lambda k: "?")
    header = str(cfg.get(key + "proxy_header") or "").strip()
    gheader = str(cfg.get(key + "proxy_groups_header") or "").strip()
    groups = [str(g) for g in cfg.get(key + "proxy_groups") or ()]
    logout = str(cfg.get(key + "proxy_logout_url") or "").strip()
    if not header:
        for name, val in (("proxy_groups_header", gheader), ("proxy_groups", groups),
                          ("proxy_logout_url", logout)):
            if val:
                _say("%s%s is set (by %s), but %sproxy_header is not, so proxy sign-in is off "
                     "and it is ignored" % (key, name, origin(key + name), key))
        return None
    for name, val in (("proxy_header", header), ("proxy_groups_header", gheader)):
        if val and not HEADER_NAME.fullmatch(val):
            raise SystemExit("%s%s: %r is not the name of a header (set by %s)" % (
                key, name, val, origin(key + name)))
        if val.lower() in NOT_PROXY_HEADERS:
            raise SystemExit(
                "%s%s: %s says something other than who signed in, which a client or the "
                "HTTP layer sets, so the proxy's sign-in cannot ride in it (set by %s). Name "
                "the header the proxy sets for it, such as X-Forwarded-User or "
                "X-Forwarded-Groups." % (key, name, val, origin(key + name)))
    if gheader.lower() == header.lower():
        raise SystemExit("%sproxy_groups_header and %sproxy_header both name %s: the groups "
                         "are a header of their own" % (key, key, header))
    for g in groups:
        if not g or GROUP_SEP.search(g) or _odd(g):
            raise SystemExit("%sproxy_groups: %r is not the name of a group: no space, comma "
                             "or control character, which part one group from the next "
                             "(set by %s)" % (key, g, origin(key + "proxy_groups")))
    if groups and not gheader:
        raise SystemExit(
            "%sproxy_groups is set (by %s), but %sproxy_groups_header is not, so no one could "
            "be found in a group: name the header the proxy lists the user's groups in "
            "(RC_DASH_PROXY_GROUPS_HEADER), or leave proxy_groups empty to take any user it "
            "names" % (key, origin(key + "proxy_groups"), key))
    trusted = []
    for entry in cfg.get(key + "proxy_trusted") or ():
        e = str(entry).strip()
        try:
            trusted.append(ipaddress.ip_network(e))
        except ValueError:
            why = "is not an address or a network (10.0.0.0/8): name the proxy's own address"
            if e == "*":
                why = "would believe the user any connection names: name the proxy's own address"
            else:
                try:
                    why = "has bits set past its prefix: the network is %s" % (
                        ipaddress.ip_network(e, strict=False))
                except ValueError:
                    pass
            raise SystemExit("%sproxy_trusted: %r %s (set by %s)" % (
                key, e, why, origin(key + "proxy_trusted")))
    if not trusted:
        raise SystemExit(
            "%sproxy_trusted is empty (set by %s), so the proxy could sign no one in: name the "
            "address the proxy connects from (127.0.0.1 when it runs on this host)"
            % (key, origin(key + "proxy_trusted")))
    if logout and not _logout_ok(logout):
        raise SystemExit("%sproxy_logout_url: %r is neither a path on this site (/oauth2/sign_out) "
                         "nor an http(s) URL (set by %s)" % (
                             key, logout, origin(key + "proxy_logout_url")))
    if logout and shown(logout) != logout:
        raise SystemExit(
            "%sproxy_logout_url holds what reads as a credential, and every reader is sent it "
            "(set by %s): give the proxy's sign-out URL without one" % (
                key, origin(key + "proxy_logout_url")))
    secret = read_proxy_secret(cfg.get(key + "proxy_secret_file") or "",
                               origin(key + "proxy_secret_file"))
    return Proxy(header, trusted, gheader, groups, logout, secret)


MAKE_SECRET = ("python3 -c 'import secrets; print(secrets.token_urlsafe(48))' > FILE, "
               "with umask 077, readable by the service's account alone")


def read_proxy_secret(path, origin="?"):
    """The proxy's secret, from `server.auth.proxy_secret_file`: its one line, as
    bytes. Stops the service, saying why and how to make one, when it is not set,
    cannot be read, is readable by every account on this host (which could then
    name any user), or is too short or holds a character a header would not
    carry as it is."""
    key = "server.auth.proxy_secret_file"
    if not path:
        raise SystemExit(
            "%s is not set: proxy sign-in believes the user a request names only with the "
            "secret the proxy adds to it in X-RC-Proxy-Secret, since every account that can "
            "reach the port -- on this host, all of them, through 127.0.0.1 -- could send any "
            "header. Make one: %s; name it here (RC_DASH_PROXY_SECRET_FILE); and have the "
            "proxy send it on every request (dashboard/README.md, \"Single sign-on through a "
            "proxy\")" % (key, MAKE_SECRET))
    try:
        st = os.stat(path)
        with open(path, "rb") as fh:
            raw = fh.read(MAX_SECRET_FILE + 1)
    except OSError as e:
        raise SystemExit("%s: %s: %s (set by %s)" % (key, path, e.strerror or e, origin))
    if st.st_mode & 0o004:
        raise SystemExit("%s: %s is readable by every account on this host, any of which "
                         "could then name any user: chmod o-r it (set by %s)" % (key, path, origin))
    lines = [ln.strip() for ln in raw[:MAX_SECRET_FILE].splitlines()]
    lines = [ln for ln in lines if ln and not ln.startswith(b"#")]
    if len(raw) > MAX_SECRET_FILE or len(lines) != 1:
        raise SystemExit("%s: %s must hold the secret on one line (set by %s)" % (key, path, origin))
    secret = lines[0]
    if len(secret) < MIN_SECRET or not SECRET_CHARS.fullmatch(secret.decode("latin-1")):
        raise SystemExit(
            "%s: the secret in %s must be %d characters or more, letters, digits and "
            "punctuation, no space: make one with %s (set by %s)"
            % (key, path, MIN_SECRET, MAKE_SECRET, origin))
    return secret


def forwarded_allow_ips():
    """The addresses uvicorn takes X-Forwarded-Proto and X-Forwarded-For from:
    FORWARDED_ALLOW_IPS, as uvicorn reads it, else 127.0.0.1 and ::1."""
    return os.environ.get("FORWARDED_ALLOW_IPS", "127.0.0.1,::1")


class Peer:
    """ASGI, outside everything else: the address the connection came from, kept
    in the scope under PEER before anything reads a forwarded header, and then
    the forwarded headers read as uvicorn reads them -- X-Forwarded-Proto into
    the scheme, so a cookie is Secure behind cloudflared, and X-Forwarded-For
    into `client`, which the access log names -- from the addresses
    `forwarded_allow_ips` names. `serve` runs uvicorn with its own reading off
    (app.SERVER_OPTIONS): it rewrote `client` before the app saw the request, so
    the address the proxy sign-in trusts would have been a header's."""

    def __init__(self, app, trusted_hosts=None):
        from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware
        self.app = app
        self.forwarded = ProxyHeadersMiddleware(app, trusted_hosts=trusted_hosts
                                                or forwarded_allow_ips())

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            scope.setdefault(PEER, scope.get("client"))
        await self.forwarded(scope, receive, send)


def reader(scope, proxy=None):
    """How the reader of this request got in, for /api/config's `_meta.auth`:
    `method`, proxy, password or open; `user`, the name the proxy gave, else
    null; `logout_url`, where Sign out goes when the proxy signed them in, else
    null (with the password, Sign out is the form's own)."""
    got = scope.get(READER) or {"method": "open", "user": None}
    by_proxy = got["method"] == "proxy"
    return {"method": got["method"], "user": shown(got["user"]) if by_proxy else None,
            "logout_url": (proxy.logout_url or None) if by_proxy and proxy else None}


# --------------------------------------------------------------- the page
# Light only and in the page's own palette (web/src/styles.css), with nothing
# loaded from anywhere: the bundle's assets wait behind this form.
_PAGE = string.Template("""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>$title</title>
<style>
:root { color-scheme: light; --crimson: #a51c30; --crimson-ink: #8a1728; --page: #f7f4f4;
  --surface: #fdfbfb; --ink: #1e1e1e; --ink-2: #4e4548; --muted: #8a7d80; --rule: #e4dcdc;
  --edge: #cfc4c5; }
* { box-sizing: border-box; }
body { margin: 0; min-height: 100vh; display: grid; place-items: center; padding: 16px;
  background: var(--page); color: var(--ink); -webkit-font-smoothing: antialiased;
  font: 14px/1.5 'IBM Plex Sans', system-ui, -apple-system, 'Segoe UI', sans-serif; }
form { width: 100%; max-width: 340px; padding: 22px 24px 24px; background: var(--surface);
  border: 1px solid var(--rule); border-radius: 10px;
  box-shadow: 0 1px 2px rgba(30, 30, 30, 0.04), 0 4px 10px -6px rgba(30, 30, 30, 0.06); }
.mark { display: flex; align-items: center; gap: 10px; font-size: 15px; font-weight: 600;
  letter-spacing: -0.015em; }
.bmark { width: 30px; height: 30px; border-radius: 7px; display: grid; place-items: center;
  flex: none; color: #fff; background: var(--crimson); }
.site { margin: 6px 0 20px; font-size: 11px; color: var(--muted);
  font-family: 'IBM Plex Mono', ui-monospace, SFMono-Regular, Menlo, monospace; }
label { display: block; margin-bottom: 4px; font-size: 12px; color: var(--ink-2); }
input[type=password] { display: block; width: 100%; padding: 8px 10px; font: inherit;
  color: var(--ink); background: #fff; border: 1px solid var(--edge); border-radius: 6px; }
input[type=password]:focus { outline: 2px solid var(--crimson); outline-offset: -1px; }
button { display: block; width: 100%; margin-top: 14px; padding: 9px 10px; font: inherit;
  font-weight: 600; color: #fff; background: var(--crimson); border: 0; border-radius: 7px;
  cursor: pointer; }
button:hover, button:focus-visible { background: var(--crimson-ink); }
button:focus-visible { outline: 2px solid var(--crimson); outline-offset: 2px; }
.err { margin: 12px 0 0; font-size: 12.5px; color: var(--crimson-ink); }
</style>
</head>
<body>
<form method="post" action="/login">
<div class="mark"><span class="bmark"><svg width="30" height="30" viewBox="0 0 32 32" fill="currentColor" aria-hidden="true" focusable="false"><path d="M5.7 24.1a8 8 0 0 1 16 0z"/><path d="M14.4 17.9L18.4 13.9" stroke="currentColor" stroke-width="3.2" stroke-linecap="round"/><rect x="4.7" y="23.9" width="18" height="2.6" rx="1.3"/><path d="M23.4 4.6C23.96 8.34 23.96 8.34 27.7 8.9C23.96 9.46 23.96 9.46 23.4 13.2C22.84 9.46 22.84 9.46 19.1 8.9C22.84 8.34 22.84 8.34 23.4 4.6Z"/></svg></span>Agent Observatory</div>
<div class="site">$site</div>
<label for="password">Password</label>
<input id="password" type="password" name="password" autocomplete="current-password" required autofocus>
<input type="hidden" name="next" value="$next">
<button type="submit">Sign in</button>
$error</form>
</body>
</html>
""")


def site_label(cfg):
    """What the sign-in form calls this deployment: `site.name`, or this host's
    name when that is empty."""
    from . import about
    return str(cfg.get("site.name") or "").strip() or about.host() or ""


def login_page(site, nxt, error=None):
    return _PAGE.substitute(
        title=html.escape("Sign in · %s" % site if site else "Sign in"),
        site=html.escape(site or ""),
        next=html.escape(nxt, quote=True),
        error='<p class="err" role="alert">%s</p>\n' % html.escape(error) if error else "")


def _see_other(location, cookie=None):
    headers = {"location": location, "cache-control": "no-store"}
    if cookie:
        headers["set-cookie"] = cookie
    return Response(status_code=303, headers=headers)


def _here(scope):
    """The URL a request asked for, as a path to come back to."""
    raw = scope.get("raw_path") or urllib.parse.quote(scope.get("path") or "/").encode()
    q = scope.get("query_string") or b""
    return safe_next(raw.decode("latin-1") + ("?" + q.decode("latin-1") if q else ""))


async def _read_body(receive, limit):
    """The request's body, or None past `limit` bytes or when the client left."""
    chunks, size = [], 0
    while True:
        msg = await receive()
        if msg["type"] != "http.request":
            return None
        chunk = msg.get("body", b"")
        size += len(chunk)
        if size > limit:
            return None
        chunks.append(chunk)
        if not msg.get("more_body"):
            return b"".join(chunks)


def _form(body):
    try:
        fields = urllib.parse.parse_qs(body.decode("utf-8", "replace"),
                                       keep_blank_values=True, max_num_fields=8)
    except ValueError:
        return {}
    return {k: v[0] for k, v in fields.items()}


def _is_api(path):
    return path == "/api" or path.startswith("/api/")


# What a request the proxy did not sign in is told when there is no password to
# fall back to (`AuthMiddleware.no_password`): plain text, never a list of groups.
BEHIND_SSO = "This dashboard is behind the site's single sign-on: open it through the site's sign-in.\n"
NOT_IN_GROUP = "Signed in as %s, who is not in a group allowed to read this dashboard.\n"
TEXT_HEADERS = {"cache-control": "no-store", "x-content-type-options": "nosniff"}


class AuthMiddleware:
    """ASGI: nothing reaches the app unless the proxy signed the reader in or
    they gave the password (see the module's docstring). Added outside every
    route and the static mount, so a route added later is covered without
    anyone remembering to be. `gate` is the password's and `proxy` the single
    sign-on's: either may be None, not both. What let a request in is kept in
    its scope under READER, for /api/config (`reader`)."""

    def __init__(self, app, gate=None, site="", proxy=None):
        if gate is None and proxy is None:
            raise ValueError("a sign-in needs a password, a proxy, or both")
        self.app = app
        self.gate = gate
        self.site = site
        self.proxy = proxy

    def page(self, nxt, status=200, error=None):
        return HTMLResponse(login_page(self.site, nxt, error), status_code=status,
                            headers=PAGE_HEADERS)

    async def __call__(self, scope, receive, send):
        kind = scope["type"]
        if kind not in ("http", "websocket"):           # lifespan
            await self.app(scope, receive, send)
            return
        path = scope.get("path") or "/"
        signed = self.proxy.signs_in(scope) if self.proxy is not None else None
        if self.proxy is not None:
            # The proxy's secret goes no further than here: nothing past the
            # sign-in reads it, so nothing there can log it or send it back.
            scope["headers"] = [(k, v) for k, v in scope.get("headers") or ()
                                if k.lower() != SECRET_HEADER]
        if self.gate is not None and kind == "http" and path == LOGIN:
            await self.login(scope, receive, send, signed)
        elif self.gate is not None and kind == "http" and path == LOGOUT:
            await self.logout(scope, receive, send)
        elif signed is not None and signed.why is None:
            scope[READER] = {"method": "proxy", "user": signed.user}
            await self.app(scope, receive, send)
        elif self.gate is not None and await self.gate.allowed(scope):
            scope[READER] = {"method": "password", "user": None}
            await self.app(scope, receive, send)
        elif kind == "websocket":
            await receive()                              # websocket.connect
            await send({"type": "websocket.close", "code": 1008})
        elif self.gate is None:
            await self.no_password(signed)(scope, receive, send)
        elif scope.get("method") in ("GET", "HEAD") and not _is_api(path):
            await self.page(_here(scope), status=401)(scope, receive, send)
        else:
            await JSONResponse({"detail": "this dashboard needs its password: sign in at /login"},
                               status_code=401, headers=NO_STORE)(scope, receive, send)

    def no_password(self, signed):
        """A request the proxy did not sign in, with no password to fall back to:
        a 403 for a user in none of the groups, else a 401, either way saying
        why in plain text, and never which groups would do."""
        if signed is not None and signed.why == "group":
            return PlainTextResponse(NOT_IN_GROUP % shown(signed.user), status_code=403,
                                     headers=TEXT_HEADERS)
        return PlainTextResponse(BEHIND_SSO, status_code=401, headers=TEXT_HEADERS)

    async def login(self, scope, receive, send, signed=None):
        method = scope.get("method")
        if method in ("GET", "HEAD"):
            q = urllib.parse.parse_qs((scope.get("query_string") or b"").decode("latin-1"))
            nxt = safe_next((q.get("next") or ["/"])[0])
            inside = (signed is not None and signed.why is None) or await self.gate.allowed(scope)
            resp = _see_other(nxt) if inside else self.page(nxt)
        elif method == "POST":
            body = await _read_body(receive, MAX_FORM)
            if body is None:
                resp = JSONResponse({"detail": "too large for a sign-in form"},
                                    status_code=413, headers=NO_STORE)
            else:
                form = _form(body)
                nxt = safe_next(form.get("next", "/"))
                tok = await self.gate.sign_in(form.get("password", ""))
                if tok is not None:
                    secure = scope.get("scheme") == "https"
                    resp = _see_other(nxt, self.gate.set_cookie(tok, secure))
                else:
                    resp = self.page(nxt, status=401, error="Wrong password.")
        else:
            resp = JSONResponse({"detail": "sign in with GET or POST"}, status_code=405,
                                headers=dict(NO_STORE, allow="GET, HEAD, POST"))
        await resp(scope, receive, send)

    async def logout(self, scope, receive, send):
        if scope.get("method") != "POST":
            resp = JSONResponse({"detail": "sign out with a POST"}, status_code=405,
                                headers=dict(NO_STORE, allow="POST"))
        else:
            resp = _see_other(LOGIN, self.gate.clear_cookie(scope.get("scheme") == "https"))
        await resp(scope, receive, send)


# ------------------------------------------------------- a new password
def _write_private(path, text):
    """`text` to `path` with mode 0600, replacing it in one step."""
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp-")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _stage_hash_file(path, stored):
    """`stored` written, mode 0640, beside hash file `path` under a name of its
    own, with the replaced file's owner and group where this account may keep
    them; `_place` renames it into place. Folders made on the way are 0750."""
    parse_hash(stored)
    d = os.path.dirname(path)
    os.makedirs(d, mode=0o750, exist_ok=True)
    try:
        before = os.stat(path)
    except FileNotFoundError:
        before = None
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(stored + "\n")
        os.chmod(tmp, HASH_FILE_MODE)
        if before is not None:
            try:
                os.chown(tmp, before.st_uid if os.geteuid() == 0 else -1, before.st_gid)
            except OSError:
                pass
    except BaseException:
        _unlink(tmp)
        raise
    return tmp


def _unlink(path):
    try:
        os.unlink(path)
    except OSError:
        pass


def write_hash_file(path, stored):
    """`stored` to hash file `path`, mode 0640 (the service's group may read it),
    replaced in one step so the service never reads it half written."""
    path = os.path.abspath(os.path.expanduser(path))
    tmp = _stage_hash_file(path, stored)
    try:
        os.replace(tmp, path)
    except BaseException:
        _unlink(tmp)
        raise
    return path


def new_password(directory=None, hosts=(), force=False, length=16, hash_file=None):
    """Make a password and write it to `<directory>/password`, with a netrc for
    curl beside it that names 127.0.0.1, localhost, and `hosts`. Both are mode
    0600; a directory made here is 0700. With `hash_file`, its hash goes there
    too (`write_hash_file`). Returns `(password file, hash)`. Refuses to replace
    any of them unless `force`."""
    d = os.path.abspath(os.path.expanduser(directory or DEFAULT_DIR))
    names = ["127.0.0.1", "localhost"] + [h for h in hosts if h not in ("127.0.0.1", "localhost")]
    for h in names:
        if not h or any(c.isspace() for c in h):
            raise ValueError("not a host name: %r" % h)
    files = {"password": os.path.join(d, "password"), "netrc": os.path.join(d, "netrc")}
    if hash_file:
        files["hash"] = os.path.abspath(os.path.expanduser(hash_file))
    there = [p for p in files.values() if os.path.exists(p)]
    if there and not force:
        raise FileExistsError("%s already exists; pass --force to replace it, which signs "
                              "everyone out once the service has the new hash" % there[0])
    pw = generate_password(length)
    stored = hash_password(pw)
    # The hash is written first, beside its file, so a hash file that cannot be
    # written stops this before the password is replaced; it goes into place
    # last, once the password it is the hash of is safely written.
    os.makedirs(d, mode=0o700, exist_ok=True)      # before the hash file's, which may be in it
    staged = _stage_hash_file(files["hash"], stored) if hash_file else None
    try:
        _write_private(files["password"], pw + "\n")
        _write_private(files["netrc"], "".join("machine %s login rc password %s\n" % (h, pw)
                                               for h in names))
        if staged:
            os.replace(staged, files["hash"])
            staged = None
    finally:
        if staged:
            _unlink(staged)
    return files["password"], stored
