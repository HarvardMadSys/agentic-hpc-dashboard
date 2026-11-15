"""Sign-in for the whole service: the page, its assets, every /api route, and /ws.

One password, shared by everyone who reads this deployment. There are no
accounts. The service keeps only a salted scrypt hash of it, at
`server.auth.password_hash` (or RC_DASH_PASSWORD_HASH). An empty value leaves
the service open, as it was before this module.

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

`python -m rc_dashboard new-password` makes a password, writes it and a netrc
to files only their owner can read, and prints the hash to put in the config.
Nothing here prints, logs, or sends the password itself.
"""
import asyncio
import base64
import binascii
import hashlib
import hmac
import html
import os
import secrets
import string
import tempfile
import time
import urllib.parse

from starlette.responses import HTMLResponse, JSONResponse, Response

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
    header for scripts."""

    def __init__(self, stored, key, cookie_name, lifetime_s, now=None, fail_delay_s=1.0):
        parse_hash(stored)
        self.stored = stored
        # Per deployment and per password: a cookie made under another key
        # file or another hash is not valid here.
        self.key = hmac.new(key, b"rc-dashboard session\0" + stored.encode(),
                            hashlib.sha256).digest()
        self.cookie_name = cookie_name
        self.lifetime_s = int(lifetime_s)
        self.now = now or wall
        self.fail_delay_s = fail_delay_s
        self._passed = set()            # Basic headers that passed once, as HMACs
        self._lock = self._loop = None

    # -- the cookie
    def _mac(self, exp):
        d = hmac.new(self.key, b"%d" % exp, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(d).decode("ascii").rstrip("=")

    def token(self):
        exp = int(self.now()) + self.lifetime_s
        return "%d.%s" % (exp, self._mac(exp))

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

    async def check_password(self, password):
        """scrypt, one check at a time and off the event loop. A wrong password
        waits `fail_delay_s` more, so guessing is slow as well as hopeless."""
        ok = False
        if password and len(password) <= MAX_PASSWORD:
            async with self._lock_here():
                ok = await asyncio.to_thread(verify_password, password, self.stored)
        if not ok and self.fail_delay_s:
            await asyncio.sleep(self.fail_delay_s)
        return ok

    async def check_basic(self, value):
        """`Authorization: Basic <value>`, with the password after any user name."""
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


def from_config(cfg):
    """The service's gate, or None when no password is set. A hash that does
    not parse, or a lifetime that is not a sane number of days, stops the
    service: starting open, or locked for good, would be worse."""
    stored = str(cfg.get("server.auth.password_hash") or "").strip()
    if not stored:
        return None
    try:
        parse_hash(stored)
    except ValueError as e:
        raise SystemExit("server.auth.password_hash: %s. Make one with "
                         "`python -m rc_dashboard new-password`." % e)
    days = cfg.get("server.auth.session_days", 30)
    try:
        lifetime = int(float(days) * 86400)
    except (TypeError, ValueError, OverflowError):
        lifetime = 0
    if not 0 < lifetime <= MAX_SESSION_DAYS * 86400:
        raise SystemExit("server.auth.session_days: %r is not a number of days above 0 "
                         "and at most %d" % (days, MAX_SESSION_DAYS))
    return Gate(stored, load_session_key(cfg.get("cache.dir") or "."),
                cookie_name="rc_dash_%s" % cfg.get("server.port"), lifetime_s=lifetime)


# --------------------------------------------------------------- the page
# Light only and in the page's own palette (web/src/styles.css), with nothing
# loaded from anywhere: the bundle's assets wait behind this form.
_PAGE = string.Template("""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>Sign in · $site</title>
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
.bmark { width: 30px; height: 30px; border-radius: 8px; display: grid; place-items: center;
  flex: none; color: #fff; background: var(--crimson);
  box-shadow: 0 1px 0 rgba(255, 255, 255, 0.25) inset, 0 2px 6px rgba(165, 28, 48, 0.28); }
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
<div class="mark"><span class="bmark"><svg width="15" height="15" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false"><rect x="4" y="6.5" width="12" height="9" rx="2.4"/><path d="M10 6.5V3.8M2.6 10v2.5M17.4 10v2.5"/><circle cx="10" cy="3.2" r="0.9" fill="currentColor" stroke="none"/><circle cx="7.6" cy="11" r="1" fill="currentColor" stroke="none"/><circle cx="12.4" cy="11" r="1" fill="currentColor" stroke="none"/></svg></span>Agent Behaviour</div>
<div class="site">$site</div>
<label for="password">Password</label>
<input id="password" type="password" name="password" autocomplete="current-password" required autofocus>
<input type="hidden" name="next" value="$next">
<button type="submit">Sign in</button>
$error</form>
</body>
</html>
""")


def login_page(site, nxt, error=None):
    return _PAGE.substitute(
        site=html.escape(site or "rc_measurement"),
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


class AuthMiddleware:
    """ASGI: nothing reaches the app without the password (see the module's
    docstring). Added outside every route and the static mount, so a route
    added later is covered without anyone remembering to be."""

    def __init__(self, app, gate, site=""):
        self.app = app
        self.gate = gate
        self.site = site

    def page(self, nxt, status=200, error=None):
        return HTMLResponse(login_page(self.site, nxt, error), status_code=status,
                            headers=PAGE_HEADERS)

    async def __call__(self, scope, receive, send):
        kind = scope["type"]
        if kind not in ("http", "websocket"):           # lifespan
            await self.app(scope, receive, send)
            return
        path = scope.get("path") or "/"
        if kind == "http" and path == LOGIN:
            await self.login(scope, receive, send)
        elif kind == "http" and path == LOGOUT:
            await self.logout(scope, receive, send)
        elif await self.gate.allowed(scope):
            await self.app(scope, receive, send)
        elif kind == "websocket":
            await receive()                              # websocket.connect
            await send({"type": "websocket.close", "code": 1008})
        elif scope.get("method") in ("GET", "HEAD") and not _is_api(path):
            await self.page(_here(scope), status=401)(scope, receive, send)
        else:
            await JSONResponse({"detail": "this dashboard needs its password: sign in at /login"},
                               status_code=401, headers=NO_STORE)(scope, receive, send)

    async def login(self, scope, receive, send):
        method = scope.get("method")
        if method in ("GET", "HEAD"):
            q = urllib.parse.parse_qs((scope.get("query_string") or b"").decode("latin-1"))
            nxt = safe_next((q.get("next") or ["/"])[0])
            resp = _see_other(nxt) if await self.gate.allowed(scope) else self.page(nxt)
        elif method == "POST":
            body = await _read_body(receive, MAX_FORM)
            if body is None:
                resp = JSONResponse({"detail": "too large for a sign-in form"},
                                    status_code=413, headers=NO_STORE)
            else:
                form = _form(body)
                nxt = safe_next(form.get("next", "/"))
                if await self.gate.check_password(form.get("password", "")):
                    secure = scope.get("scheme") == "https"
                    resp = _see_other(nxt, self.gate.set_cookie(self.gate.token(), secure))
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


def new_password(directory=None, hosts=(), force=False, length=16):
    """Make a password and write it to `<directory>/password`, with a netrc for
    curl beside it that names 127.0.0.1, localhost, and `hosts`. Both are mode
    0600; a directory made here is 0700. Returns `(password file, hash)`.
    Refuses to replace either file unless `force`."""
    d = os.path.abspath(os.path.expanduser(directory or DEFAULT_DIR))
    names = ["127.0.0.1", "localhost"] + [h for h in hosts if h not in ("127.0.0.1", "localhost")]
    for h in names:
        if not h or any(c.isspace() for c in h):
            raise ValueError("not a host name: %r" % h)
    files = {"password": os.path.join(d, "password"), "netrc": os.path.join(d, "netrc")}
    there = [p for p in files.values() if os.path.exists(p)]
    if there and not force:
        raise FileExistsError("%s already exists; pass --force to replace it, which signs "
                              "everyone out once the new hash is in the config" % there[0])
    os.makedirs(d, mode=0o700, exist_ok=True)
    pw = generate_password(length)
    _write_private(files["password"], pw + "\n")
    _write_private(files["netrc"], "".join("machine %s login rc password %s\n" % (h, pw)
                                           for h in names))
    return files["password"], hash_password(pw)
