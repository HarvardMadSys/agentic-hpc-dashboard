"""Sign-in (rc_dashboard/auth.py): the password, its hash, the session cookie,
and every route of the real app asking for it.

The last class starts the app on a free local port, as test_anonymize_leak
does, over an empty feed and a stand-in bundle. It reads the page, an asset,
every API route, and the websocket, with and without the password.

    cd dashboard && .venv/bin/python -m unittest tests.test_auth -q
"""
import asyncio
import base64
import http.client
import io
import json
import math
import netrc
import os
import re
import shutil
import socket
import stat
import string
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
import warnings
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import uvicorn                                       # noqa: E402
from fastapi.routing import APIRoute                 # noqa: E402
from websockets.exceptions import InvalidStatus      # noqa: E402
from websockets.sync.client import connect           # noqa: E402

from rc_dashboard import __main__ as cli             # noqa: E402
from rc_dashboard import app as appmod               # noqa: E402
from rc_dashboard import auth                        # noqa: E402
from rc_dashboard import config as configmod         # noqa: E402
from rc_dashboard import hours, snapshots            # noqa: E402

# Cheap scrypt for every test that is not about its cost. A hash carries its
# own parameters, so the service checks it with these.
FAST = {"n": 16, "r": 1, "p": 1}

# What a password must never hold: each one breaks a shell line, a netrc, or a
# copy out of a terminal.
AWKWARD = set(" \t\n\"'`\\#$!&|;<>()")


def tmpdir(case):
    d = tempfile.mkdtemp(prefix="rc-auth-")
    case.addCleanup(shutil.rmtree, d, True)
    return d


class Password(unittest.TestCase):
    def test_sixteen_characters_with_every_class(self):
        for _ in range(300):
            pw = auth.generate_password()
            self.assertEqual(len(pw), 16)
            for cls in (string.ascii_lowercase, string.ascii_uppercase, string.digits,
                        auth.SYMBOLS):
                self.assertTrue(any(c in cls for c in pw), (pw, cls))
            self.assertTrue(set(pw) <= set(auth.ALPHABET), pw)

    def test_about_a_hundred_bits(self):
        self.assertGreaterEqual(16 * math.log2(len(auth.ALPHABET)), 100)

    def test_no_awkward_characters(self):
        self.assertFalse(set(auth.ALPHABET) & AWKWARD)
        self.assertEqual(len(set(auth.ALPHABET)), len(auth.ALPHABET))

    def test_drawn_afresh_each_time(self):
        self.assertEqual(len({auth.generate_password() for _ in range(200)}), 200)

    def test_longer_on_request(self):
        self.assertEqual(len(auth.generate_password(24)), 24)
        with self.assertRaises(ValueError):
            auth.generate_password(3)


class Hash(unittest.TestCase):
    def test_round_trip(self):
        h = auth.hash_password("Correct-horse:1", **FAST)
        self.assertTrue(auth.verify_password("Correct-horse:1", h))
        self.assertFalse(auth.verify_password("Correct-horse:2", h))
        self.assertFalse(auth.verify_password("", h))

    def test_salted(self):
        self.assertNotEqual(auth.hash_password("x", **FAST), auth.hash_password("x", **FAST))

    def test_default_cost(self):
        h = auth.hash_password("pw")
        self.assertTrue(h.startswith("scrypt$32768$8$1$"), h)
        self.assertTrue(auth.verify_password("pw", h))

    def test_never_holds_the_password(self):
        self.assertNotIn("Visible-123%", auth.hash_password("Visible-123%", **FAST))

    def test_malformed_hashes_refuse(self):
        good = auth.hash_password("pw", **FAST).split("$")
        for bad in ("", "scrypt", "scrypt$16$1$1$AAAA",
                    "bcrypt$" + "$".join(good[1:]),
                    "$".join(good[:1] + ["x"] + good[2:]),
                    "$".join(good[:1] + ["15"] + good[2:]),          # n not a power of 2
                    "$".join(good[:1] + [str(2 ** 30)] + good[2:]),  # n past the cap
                    "$".join(good[:4] + ["!!!!"] + good[5:]),
                    "$".join(good[:5] + [""])):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    auth.parse_hash(bad)
                self.assertFalse(auth.verify_password("pw", bad))


class Session(unittest.TestCase):
    def setUp(self):
        self.tmp = tmpdir(self)
        self.stored = auth.hash_password("pw", **FAST)

    def gate(self, stored=None, now=None):
        return auth.Gate(stored or self.stored, auth.load_session_key(self.tmp),
                         cookie_name="rc_dash_test", lifetime_s=3600,
                         now=now or (lambda: 1000.0))

    def test_the_key_file_is_private_and_kept(self):
        key = auth.load_session_key(self.tmp)
        path = os.path.join(self.tmp, "session.key")
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(auth.load_session_key(self.tmp), key)
        self.assertEqual(len(key), 32)

    def test_a_token_lasts_its_lifetime(self):
        t = [1000.0]
        g = self.gate(now=lambda: t[0])
        tok = g.token()
        self.assertTrue(g.valid(tok))
        t[0] = 1000.0 + 3599
        self.assertTrue(g.valid(tok))
        t[0] = 1000.0 + 3600
        self.assertFalse(g.valid(tok))

    def test_tampered_tokens_fail(self):
        g = self.gate()
        tok = g.token()
        exp, _, mac = tok.partition(".")
        flipped = mac[:-1] + ("A" if mac[-1] != "A" else "B")
        for bad in (None, "", ".", exp, exp + ".", "." + mac, "x" + tok, tok + "x",
                    "%d.%s" % (int(exp) + 10 ** 6, mac), exp + "." + flipped,
                    "１２." + mac, "-1." + mac):
            with self.subTest(bad=bad):
                self.assertFalse(g.valid(bad))

    def test_a_new_password_signs_everyone_out(self):
        tok = self.gate().token()
        # the same password, hashed again: a new salt, so a new hash
        self.assertFalse(self.gate(stored=auth.hash_password("pw", **FAST)).valid(tok))

    def test_a_new_key_file_signs_everyone_out(self):
        tok = self.gate().token()
        os.remove(os.path.join(self.tmp, "session.key"))
        self.assertFalse(self.gate().valid(tok))

    def test_the_wall_clock_even_when_pinned(self):
        # clock.py pins time.time to a capture's end on a frozen instance; a
        # cookie must still run out by the real clock.
        real = time.time()
        saved = time.time
        time.time = lambda: 0.0
        try:
            self.assertGreater(auth.wall(), real - 60)
        finally:
            time.time = saved

    def test_cookie_attributes(self):
        g = self.gate()
        attrs = [a.strip() for a in g.set_cookie("TOKEN", secure=False).split(";")]
        self.assertEqual(attrs[0], "rc_dash_test=TOKEN")
        for a in ("Path=/", "Max-Age=3600", "HttpOnly", "SameSite=Lax"):
            self.assertIn(a, attrs)
        self.assertNotIn("Secure", attrs)
        self.assertIn("Secure", [a.strip() for a in g.set_cookie("T", secure=True).split(";")])
        gone = [a.strip() for a in g.clear_cookie(secure=False).split(";")]
        self.assertEqual(gone[0], "rc_dash_test=")
        self.assertIn("Max-Age=0", gone)

    def test_reads_its_cookie_among_others(self):
        g = self.gate()
        tok = g.token()
        self.assertEqual(auth.read_cookie("a=1; rc_dash_test=%s; b=2" % tok, "rc_dash_test"), tok)
        self.assertEqual(auth.read_cookie("rc_dash_test2=x;rc_dash_test=%s" % tok, "rc_dash_test"), tok)
        self.assertIsNone(auth.read_cookie("a=1; b", "rc_dash_test"))


class Next(unittest.TestCase):
    def test_only_paths_on_this_site(self):
        for good in ("/", "/?tab=risk", "/x/y?a=1&b=%2F#c"):
            self.assertEqual(auth.safe_next(good), good)
        for bad in (None, "", "risk", "//evil.example", "///evil.example", "/\\evil.example",
                    "https://evil.example/", "javascript:alert(1)", "/\r\nSet-Cookie: x=1",
                    "/café", "/login", "/login?next=/", "/logout"):
            with self.subTest(bad=bad):
                self.assertEqual(auth.safe_next(bad), "/")


class ConfigKeys(unittest.TestCase):
    def cfg(self, **auth_keys):
        tree = configmod.defaults()
        tree["cache"]["dir"] = tmpdir(self)
        tree["server"]["auth"].update(auth_keys)
        return configmod.Config(tree, {}, None)

    def test_no_password_and_not_open_by_default(self):
        # An app built with no hash has no gate; `serve` refuses to start so
        # unless `open` is set (test_serve.py).
        # Nor single sign-on: no header named (tests/test_proxy_signin.py).
        self.assertEqual(configmod.defaults()["server"]["auth"],
                         {"password_hash": "", "password_hash_file": "", "session_days": 30,
                          "open": False, "proxy_header": "", "proxy_groups_header": "",
                          "proxy_groups": [], "proxy_trusted": ["127.0.0.1", "::1"],
                          "proxy_logout_url": "", "proxy_secret_file": ""})
        self.assertIsNone(auth.from_config(self.cfg()))
        self.assertIsNone(auth.proxy_from_config(self.cfg()))

    def test_env_sets_the_hash(self):
        stored = auth.hash_password("pw", **FAST)
        path = os.path.join(tmpdir(self), "empty.json")
        with open(path, "w") as fh:
            fh.write("{}")
        saved = os.environ.get("RC_DASH_PASSWORD_HASH")
        os.environ["RC_DASH_PASSWORD_HASH"] = stored
        try:
            cfg = configmod.load(None, path)
        finally:
            if saved is None:
                del os.environ["RC_DASH_PASSWORD_HASH"]
            else:
                os.environ["RC_DASH_PASSWORD_HASH"] = saved
        self.assertEqual(cfg.get("server.auth.password_hash"), stored)
        self.assertEqual(cfg.origin("server.auth.password_hash"), "env:RC_DASH_PASSWORD_HASH")

    def test_config_route_hides_the_hash(self):
        stored = auth.hash_password("pw", **FAST)
        tree = self.cfg(password_hash=stored).as_dict(with_origins=True)
        self.assertEqual(tree["server"]["auth"]["password_hash"]["_value"], "(hidden)")
        self.assertNotIn(stored, json.dumps(tree))
        # unset stays empty, so the page can tell the two apart
        tree = self.cfg().as_dict(with_origins=True)
        self.assertEqual(tree["server"]["auth"]["password_hash"]["_value"], "")

    def test_neither_saved_state_fingerprint_moves(self):
        # a password set, changed, or cleared is a restart, never a rebuild
        a = self.cfg()
        b = self.cfg(password_hash=auth.hash_password("pw", **FAST), session_days=7)
        b.tree["cache"]["dir"] = a.tree["cache"]["dir"]
        self.assertEqual(snapshots.fingerprint("code", a, ["/r"]),
                         snapshots.fingerprint("code", b, ["/r"]))
        self.assertEqual(hours.fingerprint("code", a, ["/r"]),
                         hours.fingerprint("code", b, ["/r"]))

    def test_a_malformed_hash_stops_the_service(self):
        with self.assertRaises(SystemExit) as cm:
            auth.from_config(self.cfg(password_hash="hunter2"))
        self.assertIn("new-password", str(cm.exception))

    def test_session_days_must_be_positive(self):
        stored = auth.hash_password("pw", **FAST)
        for bad in (0, -1, "x", None):
            with self.subTest(bad=bad):
                with self.assertRaises(SystemExit):
                    auth.from_config(self.cfg(password_hash=stored, session_days=bad))
        g = auth.from_config(self.cfg(password_hash=stored, session_days=0.5))
        self.assertEqual(g.lifetime_s, 43200)


class NewPassword(unittest.TestCase):
    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["new-password", *argv])
        return code, out.getvalue(), err.getvalue()

    def test_writes_private_files_and_prints_only_the_hash(self):
        d = os.path.join(tmpdir(self), "rc-dashboard")
        code, out, err = self.run_cli("--dir", d, "--netrc-host", "dash.example.org")
        self.assertEqual(code, 0, err)
        with open(os.path.join(d, "password")) as fh:
            text = fh.read()
        self.assertTrue(text.endswith("\n"))
        pw = text[:-1]
        self.assertEqual(len(pw), 16)
        self.assertNotIn(pw, out + err)
        self.assertEqual(stat.S_IMODE(os.stat(d).st_mode), 0o700)
        for name in ("password", "netrc"):
            self.assertEqual(stat.S_IMODE(os.stat(os.path.join(d, name)).st_mode), 0o600)
        hashes = [w.strip('"') for w in out.split() if w.strip('"').startswith("scrypt$")]
        self.assertEqual(len(hashes), 1, out)
        self.assertTrue(auth.verify_password(pw, hashes[0]))
        # curl reads the netrc; Python's reader agrees on every host
        n = netrc.netrc(os.path.join(d, "netrc"))
        for host in ("127.0.0.1", "localhost", "dash.example.org"):
            self.assertEqual(n.authenticators(host), ("rc", "", pw))

    def test_refuses_to_overwrite_without_force(self):
        d = tmpdir(self)
        self.assertEqual(self.run_cli("--dir", d)[0], 0)
        with open(os.path.join(d, "password")) as fh:
            before = fh.read()
        code, out, err = self.run_cli("--dir", d)
        self.assertNotEqual(code, 0)
        self.assertIn("--force", err)
        with open(os.path.join(d, "password")) as fh:
            self.assertEqual(fh.read(), before)
        self.assertEqual(self.run_cli("--dir", d, "--force")[0], 0)
        with open(os.path.join(d, "password")) as fh:
            self.assertNotEqual(fh.read(), before)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(d, "password")).st_mode), 0o600)


class EveryRoute(unittest.TestCase):
    """The real app with a password set: nothing answers until it is given."""

    PASSWORD = "Test-pw:1{x}%"     # a few of the symbols the generator draws from

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="rc-auth-app-")
        ui = os.path.join(cls.tmp, "ui")
        os.makedirs(os.path.join(ui, "assets"))
        os.makedirs(os.path.join(cls.tmp, "ebpf"))
        with open(os.path.join(ui, "index.html"), "w") as fh:
            fh.write("<!doctype html><title>the page</title>")
        with open(os.path.join(ui, "assets", "app.js"), "w") as fh:
            fh.write("console.log('the bundle')")
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            cls.port = s.getsockname()[1]
        tree = configmod.defaults()
        tree["feeds"]["ebpf"]["roots"] = [os.path.join(cls.tmp, "ebpf")]
        tree["feeds"]["ebpf_node"]["roots"] = [os.path.join(cls.tmp, "node")]
        tree["cache"]["dir"] = os.path.join(cls.tmp, "cache")
        tree["cache"]["state_dir"] = os.path.join(cls.tmp, "state")
        tree["server"]["static_dir"] = ui
        tree["server"]["port"] = cls.port
        tree["panels"]["prewarm_min"] = []
        cls.stored = auth.hash_password(cls.PASSWORD, **FAST)
        tree["server"]["auth"]["password_hash"] = cls.stored
        cfg = configmod.Config(tree, {}, None)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)    # on_event
            cls.app = appmod.create_app(cfg)
        cls.app.state.auth.fail_delay_s = 0
        cls.server = uvicorn.Server(uvicorn.Config(
            cls.app, host="127.0.0.1", port=cls.port, log_level="error"))
        cls.thread = threading.Thread(target=cls.server.run, daemon=True)
        cls.thread.start()
        deadline = time.time() + 60
        while not cls.server.started and time.time() < deadline:
            time.sleep(0.05)
        if not cls.server.started:
            raise AssertionError("the app never started")

    @classmethod
    def tearDownClass(cls):
        cls.server.should_exit = True
        cls.thread.join(timeout=20)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def req(self, method, path, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            c.request(method, path, body=body, headers=headers or {})
            r = c.getresponse()
            return r.status, {k.lower(): v for k, v in r.getheaders()}, r.read()
        finally:
            c.close()

    def sign_in(self, password=None, nxt="/", headers=None):
        body = urllib.parse.urlencode({"password": self.PASSWORD if password is None else password,
                                       "next": nxt})
        h = {"content-type": "application/x-www-form-urlencoded"}
        h.update(headers or {})
        return self.req("POST", "/login", body, h)

    def cookie(self):
        status, h, _ = self.sign_in()
        self.assertEqual(status, 303)
        return h["set-cookie"].split(";")[0]

    def api_routes(self):
        """Every API route the app has, as (method, path) with any {param} filled."""
        out = []
        for r in self.app.routes:
            if isinstance(r, APIRoute):
                path = re.sub(r"\{[^}]+\}", "x", r.path)
                out += [(m, path) for m in sorted(r.methods)]
        return out

    def test_every_route_wants_the_password(self):
        routes = self.api_routes()
        self.assertGreater(len(routes), 15)
        # and what no route takes, which is told to sign in first, never a 405 or a 415
        pages = [("GET", "/"), ("GET", "/index.html"), ("GET", "/assets/app.js"),
                 ("GET", "/favicon.ico"), ("HEAD", "/"), ("GET", "/ws"), ("POST", "/"),
                 ("POST", "/api/panels"), ("DELETE", "/api/panels")]
        for method, path in routes + pages:
            with self.subTest(method=method, path=path):
                body = b"{}" if method in ("POST", "PUT", "PATCH") else None
                status, h, got = self.req(method, path, body,
                                          {"content-type": "application/json"} if body else {})
                self.assertEqual(status, 401)
                self.assertEqual(h.get("cache-control"), "no-store")
                self.assertNotIn("set-cookie", h)
                self.assertNotIn(b"the page", got)
                self.assertNotIn(b"the bundle", got)

    def test_the_page_answers_with_the_form(self):
        status, h, body = self.req("GET", "/?tab=risk&cls=agent")
        self.assertEqual(status, 401)
        self.assertTrue(h["content-type"].startswith("text/html"))
        self.assertEqual(h.get("x-frame-options"), "DENY")
        page = body.decode()
        self.assertIn('<form method="post" action="/login">', page)
        self.assertIn('type="password" name="password"', page)
        self.assertIn('name="next" value="/?tab=risk&amp;cls=agent"', page)

    def test_the_api_answers_with_json(self):
        status, h, body = self.req("GET", "/api/health")
        self.assertEqual(status, 401)
        self.assertEqual(h["content-type"], "application/json")
        self.assertIn("password", json.loads(body)["detail"])

    def test_a_wrong_password(self):
        for wrong in ("wrong", "", self.PASSWORD + " "):
            with self.subTest(wrong=wrong):
                status, h, body = self.sign_in(wrong)
                self.assertEqual(status, 401)
                self.assertNotIn("set-cookie", h)
                self.assertIn(b"Wrong password.", body)

    def test_signed_in_reads_everything(self):
        status, h, _ = self.sign_in(nxt="/?tab=risk")
        self.assertEqual(status, 303)
        self.assertEqual(h["location"], "/?tab=risk")
        attrs = [a.strip() for a in h["set-cookie"].split(";")]
        self.assertTrue(attrs[0].startswith("rc_dash_%d=" % self.port), attrs)
        for a in ("HttpOnly", "SameSite=Lax", "Path=/", "Max-Age=%d" % (30 * 86400)):
            self.assertIn(a, attrs)
        self.assertNotIn("Secure", attrs)           # plain http on this host
        jar = {"cookie": attrs[0]}
        status, _, body = self.req("GET", "/", headers=jar)
        self.assertEqual((status, b"the page" in body), (200, True))
        status, _, body = self.req("GET", "/assets/app.js", headers=jar)
        self.assertEqual((status, b"the bundle" in body), (200, True))
        status, _, body = self.req("GET", "/api/health", headers=jar)
        self.assertEqual((status, json.loads(body)["ok"]), (200, True))
        status, _, body = self.req("GET", "/api/config", headers=jar)
        self.assertEqual(status, 200)
        tree = json.loads(body)
        self.assertEqual(tree["server"]["auth"]["password_hash"]["_value"], "(hidden)")
        self.assertNotIn(self.stored.encode(), body)

    def test_its_cookie_among_others(self):
        jar = {"cookie": "__cf_bm=abc; %s; theme=light" % self.cookie()}
        self.assertEqual(self.req("GET", "/api/health", headers=jar)[0], 200)

    def test_secure_through_the_tunnel(self):
        # cloudflared says https in X-Forwarded-Proto, from 127.0.0.1, which
        # uvicorn trusts by default
        status, h, _ = self.sign_in(headers={"x-forwarded-proto": "https"})
        self.assertEqual(status, 303)
        self.assertIn("Secure", [a.strip() for a in h["set-cookie"].split(";")])

    def test_the_websocket(self):
        url = "ws://127.0.0.1:%d/ws" % self.port
        with self.assertRaises(InvalidStatus) as cm:
            with connect(url, open_timeout=20):
                pass
        self.assertEqual(cm.exception.response.status_code, 403)
        with connect(url, open_timeout=20, additional_headers={"Cookie": self.cookie()}) as ws:
            self.assertEqual(json.loads(ws.recv(timeout=20))["type"], "live")

    def test_basic_auth_for_scripts(self):
        def basic(raw):
            return {"authorization": "Basic " + base64.b64encode(raw.encode()).decode()}
        for user in ("rc", "", "anyone"):
            with self.subTest(user=user):
                h = basic("%s:%s" % (user, self.PASSWORD))
                self.assertEqual(self.req("GET", "/api/health", headers=h)[0], 200)
                self.assertEqual(self.req("GET", "/", headers=h)[0], 200)
        for h in (basic("rc:wrong"), basic(self.PASSWORD), {"authorization": "Basic !!!"},
                  {"authorization": "Bearer " + self.PASSWORD}):
            with self.subTest(h=h):
                self.assertEqual(self.req("GET", "/api/health", headers=h)[0], 401)

    def test_never_sent_to_another_site(self):
        for nxt in ("//evil.example/", "https://evil.example/", "/\\evil.example"):
            with self.subTest(nxt=nxt):
                status, h, _ = self.sign_in(nxt=nxt)
                self.assertEqual((status, h["location"]), (303, "/"))

    def test_sign_out(self):
        status, h, _ = self.req("POST", "/logout", headers={"cookie": self.cookie()})
        self.assertEqual((status, h["location"]), (303, "/login"))
        attrs = [a.strip() for a in h["set-cookie"].split(";")]
        self.assertEqual(attrs[0], "rc_dash_%d=" % self.port)
        self.assertIn("Max-Age=0", attrs)
        self.assertEqual(self.req("GET", "/logout")[0], 405)

    def test_the_login_url(self):
        status, h, body = self.req("GET", "/login?next=/%3Ftab%3Drisk")
        self.assertEqual(status, 200)
        self.assertIn(b'name="next" value="/?tab=risk"', body)
        status, h, _ = self.req("GET", "/login?next=/%3Ftab%3Drisk",
                                headers={"cookie": self.cookie()})
        self.assertEqual((status, h["location"]), (303, "/?tab=risk"))

    def test_a_huge_login_body(self):
        status, h, _ = self.req("POST", "/login", b"password=" + b"x" * 20000,
                                {"content-type": "application/x-www-form-urlencoded"})
        self.assertEqual(status, 413)
        self.assertNotIn("set-cookie", h)


class Open(unittest.TestCase):
    def test_no_hash_no_gate(self):
        tree = configmod.defaults()
        tmp = tmpdir(self)
        tree["cache"]["dir"] = os.path.join(tmp, "cache")
        tree["cache"]["state_dir"] = os.path.join(tmp, "state")
        tree["feeds"]["ebpf"]["roots"] = [os.path.join(tmp, "ebpf")]
        tree["server"]["static_dir"] = os.path.join(tmp, "no-ui")
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            app = appmod.create_app(configmod.Config(tree, {}, None))
        self.assertIsNone(app.state.auth)
        self.assertFalse([m for m in app.user_middleware if m.cls is auth.AuthMiddleware])


if __name__ == "__main__":
    unittest.main()


class HashFile(unittest.TestCase):
    """The hash in a file of its own, read again when it changes."""

    def setUp(self):
        self.tmp = tmpdir(self)
        self.path = os.path.join(self.tmp, "etc", "password.hash")
        self.stored = auth.hash_password("pw", **FAST)
        auth.write_hash_file(self.path, self.stored)

    def cfg(self, **auth_keys):
        tree = configmod.defaults()
        tree["cache"]["dir"] = os.path.join(self.tmp, "cache")
        tree["server"]["auth"].update(auth_keys)
        return configmod.Config(tree, {}, None)

    def test_the_file_written_and_read(self):
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o640)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(self.path)).st_mode) & 0o027, 0)
        self.assertEqual(auth.read_hash_file(self.path), self.stored)
        for text in ('"%s"\n', "'%s'", "# made by new-password\n\n  %s  \n\n"):
            with open(self.path, "w") as fh:
                fh.write(text % self.stored)
            self.assertEqual(auth.read_hash_file(self.path), self.stored)
        for text, why in (("", "holds no hash"), ("%s\n%s\n", "on one line"),
                          ("hunter2", "not a scrypt")):
            with open(self.path, "w") as fh:
                fh.write(text % ((self.stored,) * text.count("%s")))
            with self.assertRaises(ValueError) as cm:
                auth.read_hash_file(self.path)
            self.assertIn(why, str(cm.exception))

    def test_the_gate_takes_it(self):
        g = auth.from_config(self.cfg(password_hash_file=self.path))
        self.assertEqual((g.stored, g.hash_file), (self.stored, self.path))
        self.assertTrue(auth.has_password(self.cfg(password_hash_file=self.path)))

    def test_missing_or_malformed_stops_the_service(self):
        with self.assertRaises(SystemExit) as cm:
            auth.from_config(self.cfg(password_hash_file=os.path.join(self.tmp, "none")))
        self.assertIn("new-password --install", str(cm.exception))
        with open(self.path, "w") as fh:
            fh.write("hunter2\n")
        with self.assertRaises(SystemExit) as cm:
            auth.from_config(self.cfg(password_hash_file=self.path))
        self.assertIn("password_hash_file", str(cm.exception))

    def test_both_set_is_refused_saying_why(self):
        with self.assertRaises(SystemExit) as cm:
            auth.from_config(self.cfg(password_hash_file=self.path, password_hash=self.stored))
        msg = str(cm.exception)
        self.assertIn("are both set", msg)
        self.assertIn("RC_DASH_PASSWORD_HASH_FILE", msg)

    def test_a_changed_file_is_a_new_key_and_a_broken_one_keeps_the_old(self):
        g = auth.from_config(self.cfg(password_hash_file=self.path))
        tok = g.token()
        key = g.key
        self.assertFalse(g.refresh(throttle=False))          # unchanged
        new = auth.hash_password("pw2", **FAST)
        auth.write_hash_file(self.path, new)
        self.assertFalse(g.refresh())                        # throttled: a second has not passed
        self.assertTrue(g.refresh(throttle=False))
        self.assertEqual((g.stored, g.reloads), (new, 1))
        self.assertNotEqual(g.key, key)
        self.assertFalse(g.valid(tok))                       # everyone signs in again
        err = io.StringIO()
        with open(self.path, "w") as fh:
            fh.write("garbage\n")
        with redirect_stderr(err):
            self.assertFalse(g.refresh(throttle=False))
            self.assertFalse(g.reload())                     # SIGHUP: the same
        self.assertEqual(g.stored, new)
        self.assertEqual(err.getvalue().count("stays as it was"), 1)    # said once
        os.unlink(self.path)
        with redirect_stderr(io.StringIO()):
            self.assertFalse(g.reload())
        self.assertIn("No such file", g.reload_error)
        auth.write_hash_file(self.path, self.stored)
        self.assertTrue(g.reload())
        self.assertIsNone(g.reload_error)

    def test_a_file_replaced_while_the_service_starts_is_taken_up(self):
        """The hash was read (`from_config`), the session key loaded, and only then the
        file's identity taken (`Gate`): a file replaced in between paired the old hash
        with the new file, so no later look saw a change, and the revoked password let
        everyone in, and the new one no one, until a SIGHUP or a restart."""
        new = auth.hash_password("pw2", **FAST)
        real = auth.load_session_key

        def rotating(d):                 # a rotation while the service starts
            auth.write_hash_file(self.path, new)
            return real(d)

        with mock.patch.object(auth, "load_session_key", rotating), \
                redirect_stderr(io.StringIO()):
            g = auth.from_config(self.cfg(password_hash_file=self.path))
            g.refresh(throttle=False)
        g.fail_delay_s = 0
        self.assertEqual(g.stored, new)
        run = asyncio.new_event_loop().run_until_complete
        self.assertTrue(run(g.check_password("pw2")))
        self.assertFalse(run(g.check_password("pw")))

    def test_a_check_across_a_new_hash_opens_nothing(self):
        """A sign-in whose scrypt ran while the hash was replaced (SIGHUP, or a change
        another request saw) once passed against the old hash, and its cookie was signed
        with the new hash's key: a revoked password opened a session that outlived the
        rotation. The check now says no, and a cookie is signed with the key of the hash
        it was checked against, so one made across a rotation is refused."""
        g = auth.from_config(self.cfg(password_hash_file=self.path))
        g.fail_delay_s = 0
        new = auth.hash_password("pw2", **FAST)
        real = auth.verify_password

        def rotating(password, stored):     # the old hash checked, then replaced
            ok = real(password, stored)
            auth.write_hash_file(self.path, new)
            g.reload()
            return ok
        run = asyncio.new_event_loop().run_until_complete
        auth.verify_password = rotating
        try:
            self.assertIsNone(run(g.sign_in("pw")))
        finally:
            auth.verify_password = real
        self.assertEqual(g.stored, new)
        auth.write_hash_file(self.path, self.stored)
        self.assertTrue(g.reload())
        auth.verify_password = rotating
        try:
            self.assertFalse(run(g.check_password("pw")), "nor through Basic")
        finally:
            auth.verify_password = real
        # a token signed for the hash checked is refused once another is in use
        tok = run(g.sign_in("pw2"))
        self.assertTrue(g.valid(tok))
        old = g.key
        auth.write_hash_file(self.path, self.stored)
        self.assertTrue(g.reload())
        self.assertFalse(g.valid(tok))
        self.assertFalse(g.valid(g.token(old)))

    def test_a_password_check_looks_at_the_file_first(self):
        g = auth.from_config(self.cfg(password_hash_file=self.path))
        g.fail_delay_s = 0
        auth.write_hash_file(self.path, auth.hash_password("pw2", **FAST))
        run = asyncio.new_event_loop().run_until_complete
        self.assertFalse(run(g.check_password("pw")))
        self.assertTrue(run(g.check_password("pw2")))


class NewPasswordInstall(unittest.TestCase):
    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["new-password", *argv])
        return code, out.getvalue(), err.getvalue()

    def test_install_writes_the_hash_file_and_names_it(self):
        d = tmpdir(self)
        hf = os.path.join(d, "etc", "rc", "password.hash")
        code, out, err = self.run_cli("--dir", os.path.join(d, "pw"), "--install", hf)
        self.assertEqual(code, 0, err)
        with open(os.path.join(d, "pw", "password")) as fh:
            pw = fh.read().strip()
        self.assertTrue(auth.verify_password(pw, auth.read_hash_file(hf)))
        self.assertEqual(stat.S_IMODE(os.stat(hf).st_mode), 0o640)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(d, "pw")).st_mode), 0o700)
        for what in ("server.auth.password_hash_file", "RC_DASH_PASSWORD_HASH_FILE", hf):
            self.assertIn(what, out)
        self.assertNotIn(pw, out + err)
        # a second run replaces neither without --force
        code, _, err = self.run_cli("--dir", os.path.join(d, "pw"), "--install", hf)
        self.assertEqual(code, 1)
        self.assertIn("--force", err)
        self.assertTrue(auth.verify_password(pw, auth.read_hash_file(hf)))
        self.assertEqual(self.run_cli("--dir", os.path.join(d, "pw"), "--install", hf,
                                      "--force")[0], 0)
        self.assertFalse(auth.verify_password(pw, auth.read_hash_file(hf)))

    def test_a_hash_file_that_cannot_be_written_replaces_no_password(self):
        d = tmpdir(self)
        pwdir = os.path.join(d, "pw")
        self.assertEqual(self.run_cli("--dir", pwdir)[0], 0)
        with open(os.path.join(pwdir, "password")) as fh:
            before = fh.read()
        blocker = os.path.join(d, "file")
        open(blocker, "w").close()
        code, _, err = self.run_cli("--dir", pwdir, "--force", "--install",
                                    os.path.join(blocker, "password.hash"))
        self.assertEqual(code, 1)
        with open(os.path.join(pwdir, "password")) as fh:
            self.assertEqual(fh.read(), before)

    def test_without_install_the_hash_file_is_named_first(self):
        code, out, _ = self.run_cli("--dir", os.path.join(tmpdir(self), "pw"))
        self.assertEqual(code, 0)
        self.assertLess(out.index("password_hash_file"), out.index("server.auth.password_hash "))
        self.assertIn("RC_DASH_PASSWORD_HASH_FILE", out)


class SiteLabel(unittest.TestCase):
    def test_the_form_names_the_site_or_the_host_never_rc_measurement(self):
        from rc_dashboard import about
        tree = configmod.defaults()
        self.assertEqual(auth.site_label(configmod.Config(tree, {}, None)), about.host())
        tree["site"]["name"] = "cluster X"
        self.assertEqual(auth.site_label(configmod.Config(tree, {}, None)), "cluster X")
        page = auth.login_page("", "/")
        self.assertNotIn("rc_measurement", page)
        self.assertIn("<title>Sign in</title>", page)
        self.assertIn("<title>Sign in · h1</title>", auth.login_page("h1", "/"))
