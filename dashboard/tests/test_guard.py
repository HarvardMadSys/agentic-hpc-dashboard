"""Who may talk to the service (rc_dashboard/guard.py): a websocket opened by
another site's page is refused, and with no password, so is a request under a
name that is not this host's (DNS rebinding).

The first classes drive the middleware with handshakes as a browser, curl and
Vite's dev proxy send them; the last start the real app on a free local port,
as test_auth does, open and behind a password.

    cd dashboard && .venv/bin/python -m unittest tests.test_guard -q
"""
import asyncio
import base64
import http.client
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import uvicorn                                       # noqa: E402
from websockets.exceptions import InvalidStatus      # noqa: E402
from websockets.sync.client import connect           # noqa: E402

from rc_dashboard import app as appmod               # noqa: E402
from rc_dashboard import auth                        # noqa: E402
from rc_dashboard import config as configmod         # noqa: E402
from rc_dashboard.guard import SiteGuard, host_name  # noqa: E402

# Vite's dev server (dashboard/run.sh, web/vite.config.ts): the page is on
# 127.0.0.1:5173, and its /ws is proxied to the backend with `changeOrigin`, which
# rewrites the Host to the backend's and leaves the page's Origin as it was.
VITE = ("http://127.0.0.1:5173", "127.0.0.1:8124")
JSON = "application/json"


def call(guard, kind, host=None, origin=None, path="/api/health"):
    """What the guard did with one request: "app" when it let it through, else
    the HTTP status it answered with, or "closed" for a refused websocket."""
    headers = [(b"host", host.encode())] if host is not None else []
    if origin is not None:
        headers.append((b"origin", origin.encode()))
    scope = {"type": kind, "path": path, "method": "GET", "headers": headers,
             "query_string": b"", "scheme": "http"}
    sent = []

    async def receive():
        return {"type": "websocket.connect"} if kind == "websocket" else \
            {"type": "http.request", "body": b"", "more_body": False}

    async def send(msg):
        sent.append(msg)

    async def app(scope, receive, send):
        sent.append({"type": "app"})

    asyncio.run(SiteGuard(app, **guard)(scope, receive, send))
    first = sent[0]
    if first["type"] == "app":
        return "app"
    if first["type"] == "websocket.close":
        return "closed"
    return first["status"]


OPEN = {"check_host": True, "bind_host": "127.0.0.1"}
SIGNED = {"check_host": False, "bind_host": "127.0.0.1"}


class Origin(unittest.TestCase):
    def test_a_websocket_from_another_sites_page_is_refused(self):
        for guard in (OPEN, SIGNED):
            for origin in ("https://evil.example", "http://evil.example:8124",
                           "http://127.0.0.1.evil.example:8124", "null", "file://"):
                with self.subTest(guard=guard, origin=origin):
                    self.assertEqual(call(guard, "websocket", "127.0.0.1:8124", origin), "closed")

    def test_the_page_its_own_site_serves_and_a_script_pass(self):
        for host in ("127.0.0.1:8124", "localhost:8000", "dash.example.org"):
            with self.subTest(host=host):
                guard = dict(SIGNED)
                self.assertEqual(call(guard, "websocket", host, "http://%s" % host), "app")
                self.assertEqual(call(guard, "websocket", host, "https://%s/" % host.upper()), "app")
                self.assertEqual(call(guard, "websocket", host, None), "app")      # curl

    def test_vites_dev_proxy_connects_once_run_sh_names_it(self):
        named = ["http://127.0.0.1:5173", "http://localhost:5173"]       # as run.sh names them
        for guard in (OPEN, SIGNED):
            with self.subTest(guard=guard):
                self.assertEqual(call(guard, "websocket", VITE[1], VITE[0]), "closed")
                listed = dict(guard, allowed_origins=named)
                self.assertEqual(call(listed, "websocket", VITE[1], VITE[0]), "app")
                self.assertEqual(call(listed, "websocket", VITE[1], "http://localhost:5173"), "app")
                self.assertEqual(call(listed, "websocket", VITE[1], "http://localhost:5174"), "closed")

    def test_another_port_of_the_readers_own_host_is_another_site(self):
        # The cookie is SameSite=Lax, and a browser counts every port of a host as one
        # site: a dev server, a notebook or another tunnel on the reader's localhost
        # gets the cookie sent with its handshake. Its Origin is still not this one.
        for guard in (OPEN, SIGNED):
            for origin, host in (("http://localhost:8888", "localhost:8000"),
                                 ("http://127.0.0.1:9999", "127.0.0.1:8124"),
                                 ("http://[::1]:3000", "[::1]:8124")):
                with self.subTest(guard=guard, origin=origin):
                    self.assertEqual(call(guard, "websocket", host, origin), "closed")

    def test_a_listed_origin_passes_a_proxy_that_rewrites_the_host(self):
        guard = dict(SIGNED, allowed_origins=["https://Dash.example.org/", "ui.example.org:5173"])
        for origin in ("https://dash.example.org", "http://ui.example.org:5173"):
            with self.subTest(origin=origin):
                self.assertEqual(call(guard, "websocket", "127.0.0.1:8124", origin), "app")
        self.assertEqual(call(guard, "websocket", "127.0.0.1:8124", "https://evil.example"), "closed")

    def test_http_is_left_to_the_json_rule_and_the_password(self):
        self.assertEqual(call(SIGNED, "http", "127.0.0.1:8124", "https://evil.example"), "app")


class Host(unittest.TestCase):
    def test_with_no_password_a_rebound_name_is_refused(self):
        for kind in ("http", "websocket"):
            for host in ("evil.example:8124", "evil.example", "127.0.0.1.nip.io:8124",
                         "localhost.evil.example"):
                with self.subTest(kind=kind, host=host):
                    want = 403 if kind == "http" else "closed"
                    self.assertEqual(call(OPEN, kind, host), want)
                    # the page on the rebound name, opening its "own" websocket
                    self.assertEqual(call(OPEN, kind, host, "http://%s" % host), want)

    def test_loopback_names_the_bind_host_and_the_listed_ones_pass(self):
        guard = dict(OPEN, bind_host="10.0.0.5", allowed_hosts=["Dash.example.org:443"])
        for host in ("127.0.0.1:8124", "localhost:8124", "LOCALHOST", "[::1]:8124", "127.3.2.1",
                     "app.localhost:8124", "10.0.0.5:8124", "dash.example.org", "dash.example.org."):
            with self.subTest(host=host):
                self.assertEqual(call(guard, "http", host), "app")
        self.assertEqual(call(guard, "http", "10.0.0.6:8124"), 403)
        self.assertEqual(call(dict(OPEN, allowed_hosts=["*"]), "http", "evil.example"), "app")
        # HTTP/1.0 sends no Host, and no browser does that
        self.assertEqual(call(OPEN, "http", None), "app")

    def test_with_a_password_the_name_is_free(self):
        # a tunnel or a proxy may keep its public name in the Host (deploy/fleet)
        self.assertEqual(call(SIGNED, "http", "dash.example.org"), "app")
        self.assertEqual(call(SIGNED, "websocket", "dash.example.org", "https://dash.example.org"),
                         "app")

    def test_names(self):
        for raw, name in (("Example.org:8080", "example.org"), ("[::1]:8080", "::1"),
                          ("[::1]", "::1"), ("example.org.", "example.org"), ("", "")):
            self.assertEqual(host_name(raw), name)


class Live(unittest.TestCase):
    """The real app, over an empty feed, on a free port."""

    PASSWORD = None

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="rc-guard-")
        os.makedirs(os.path.join(cls.tmp, "ebpf"))
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            cls.port = s.getsockname()[1]
        tree = configmod.defaults()
        tree["feeds"]["ebpf"]["roots"] = [os.path.join(cls.tmp, "ebpf")]
        tree["feeds"]["ebpf_node"]["roots"] = [os.path.join(cls.tmp, "node")]
        tree["cache"]["dir"] = os.path.join(cls.tmp, "cache")
        tree["server"]["static_dir"] = os.path.join(cls.tmp, "no-ui")
        tree["server"]["port"] = cls.port
        tree["server"]["rdns"]["enabled"] = False
        tree["panels"]["prewarm_min"] = []
        # Ask on, with a stand-in agent: only the password decides whether it runs
        tree["ask"]["enabled"] = True
        tree["ask"]["command"] = [sys.executable, "-c", "import sys; sys.stdin.read(); print('ok')"]
        if cls.PASSWORD:
            tree["server"]["auth"]["password_hash"] = auth.hash_password(cls.PASSWORD, n=16, r=1, p=1)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)    # on_event
            cls.app = appmod.create_app(configmod.Config(tree, {}, None))
        cls.server = uvicorn.Server(uvicorn.Config(cls.app, host="127.0.0.1", port=cls.port,
                                                   log_level="error"))
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

    def headers(self):
        return {}

    def get(self, path, host):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            c.putrequest("GET", path, skip_host=True)
            c.putheader("Host", host)
            for k, v in self.headers().items():
                c.putheader(k, v)
            c.endheaders()
            r = c.getresponse()
            return r.status, r.read()
        finally:
            c.close()

    def req(self, method, path, body=None, ctype=None, origin=None):
        """(status, the body read as JSON)."""
        h = dict(self.headers())
        if ctype:
            h["Content-Type"] = ctype
        if origin:
            h["Origin"] = origin
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            c.request(method, path, body=body, headers=h)
            r = c.getresponse()
            return r.status, json.loads(r.read() or b"null")
        finally:
            c.close()

    def ws(self, origin=None):
        """The first frame's type, or the status the handshake was refused with."""
        try:
            with connect("ws://127.0.0.1:%d/ws" % self.port, origin=origin, open_timeout=20,
                         additional_headers=self.headers()) as ws:
                return json.loads(ws.recv(timeout=20))["type"]
        except InvalidStatus as e:
            return e.response.status_code


class LiveOpen(Live):
    def test_a_rebound_name_reads_nothing(self):
        status, body = self.get("/api/health", "evil.example:%d" % self.port)
        self.assertEqual(status, 403)
        self.assertIn("allowed_hosts", json.loads(body)["detail"])
        for host in ("127.0.0.1:%d" % self.port, "localhost:%d" % self.port):
            self.assertEqual(self.get("/api/health", host)[0], 200)

    def test_another_sites_page_gets_no_websocket(self):
        self.assertEqual(self.ws("https://evil.example"), 403)
        self.assertEqual(self.ws("http://127.0.0.1:%d" % self.port), "live")
        self.assertEqual(self.ws(VITE[0]), 403)              # another port, not named
        self.assertEqual(self.ws(), "live")                  # a script

    def test_another_origins_json_post_is_never_sent(self):
        # The browser asks first (a CORS preflight) before a JSON POST from another
        # origin, and is never told yes: the POST routes need no Origin check.
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            c.request("OPTIONS", "/api/ask", headers={
                "Origin": "http://localhost:8888", "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type"})
            r = c.getresponse()
            r.read()
        finally:
            c.close()
        self.assertGreaterEqual(r.status, 400)
        self.assertIsNone(r.getheader("access-control-allow-origin"))

    def test_ask_does_not_run_with_no_password(self):
        status, info = self.req("GET", "/api/ask")
        self.assertEqual(status, 200)
        self.assertTrue(info["enabled"])
        self.assertFalse(info["available"])
        self.assertIn("no password", info["error"])
        status, body = self.req("POST", "/api/ask", json.dumps({"question": "q"}), JSON)
        self.assertEqual(status, 400)
        self.assertIn("no password", body["detail"])

    def test_what_changes_something_takes_json_only(self):
        # what another site's page can send without the browser asking first
        for ctype in ("text/plain", "application/x-www-form-urlencoded", "multipart/form-data",
                      None):
            for path in ("/api/ask", "/api/suggestions/done"):
                with self.subTest(ctype=ctype, path=path):
                    status, _ = self.req("POST", path, '{"question": "q"}', ctype,
                                         origin="https://evil.example")
                    self.assertEqual(status, 415)
        self.assertEqual(self.req("DELETE", "/api/suggestions/done/x")[0], 415)
        self.assertEqual(self.req("DELETE", "/api/suggestions/done/x", ctype=JSON)[0], 404)
        self.assertEqual(self.req("POST", "/api/ask", '{"question": "q"}',
                                  "application/json; charset=utf-8")[0], 400)   # no password

    def test_a_malformed_body_is_a_400_never_a_500(self):
        for body in ("not json", "[1]", "{", '"q"', '{"question": 5}', '{"question": ["q"]}',
                     '{"history": "x"}', '{"history": [{"q": 1, "a": "x"}]}',
                     '{"window_min": "60"}', '{"window_min": 1e999}', '{"context": "risk"}',
                     "[" * 100000, b"\xff\xfe"):
            with self.subTest(body=str(body)[:40]):
                self.assertEqual(self.req("POST", "/api/ask", body, JSON)[0], 400)
        for body in ("[]", "nope"):
            for path in ("/api/suggestions/done", "/api/annotate"):
                with self.subTest(path=path, body=body):
                    self.assertEqual(self.req("POST", path, body, JSON)[0], 400)
        self.assertEqual(self.req("POST", "/api/ask", b"{" + b" " * (1 << 20) + b"}", JSON)[0], 413)


class LiveSigned(Live):
    PASSWORD = "Test-pw:1{x}%"

    def headers(self):
        return {"Authorization": "Basic " + base64.b64encode(
            ("rc:" + self.PASSWORD).encode()).decode()}

    def test_signed_in_under_any_name(self):
        self.assertEqual(self.get("/api/health", "dash.example.org")[0], 200)

    def test_another_sites_page_still_gets_no_websocket(self):
        self.assertEqual(self.ws("https://evil.example"), 403)
        # signed in (a cookie another port of this host would be sent too), all the same
        self.assertEqual(self.ws("http://127.0.0.1:%d" % (self.port + 1)), 403)
        self.assertEqual(self.ws("http://127.0.0.1:%d" % self.port), "live")

    def test_ask_runs_behind_the_password_and_takes_a_short_question(self):
        status, info = self.req("GET", "/api/ask")
        self.assertEqual((status, info["available"]), (200, True), info)
        status, job = self.req("POST", "/api/ask", json.dumps({"question": "q"}), JSON)
        self.assertEqual(status, 200, job)
        deadline = time.time() + 30
        while job["state"] not in ("done", "error") and time.time() < deadline:
            time.sleep(0.1)
            job = self.req("GET", "/api/ask/%s" % job["id"])[1]
        self.assertEqual((job["state"], job["answer"]), ("done", "ok"), job)
        status, body = self.req("POST", "/api/ask", json.dumps({"question": "x" * 4001}), JSON)
        self.assertEqual(status, 400)
        self.assertIn("at most 4000 characters", body["detail"])


@unittest.skipUnless(shutil.which("bash"), "run.sh is a bash script")
class DevScript(unittest.TestCase):
    """dashboard/run.sh names Vite's origins to the backend in dev mode, and
    only then, beside the configured ones. `uv` (which starts the backend),
    `npm` and `npx` are stand-ins: `uv` loads the config the backend would and
    writes down the origins the guard would let in (guard.allowed_origins)."""

    RUN = str(Path(__file__).resolve().parents[1] / "run.sh")
    PROBE = ("import json, os, sys\n"
             "sys.path.insert(0, %r)\n"
             "from rc_dashboard import config, guard\n"
             "print(json.dumps({'env': os.environ.get('RC_DASH_ALLOWED_ORIGINS'),\n"
             "                  'guard': guard.allowed_origins(config.load())}))\n"
             % str(Path(__file__).resolve().parents[1]))
    VITE = ["http://127.0.0.1:5173", "http://localhost:5173"]
    PROXY = {"server": {"allowed_origins": ["https://dash.example.org"]}}

    def origins(self, mode, config=None, **env):
        tmp = tempfile.mkdtemp(prefix="rc-run-sh-")
        self.addCleanup(shutil.rmtree, tmp, True)
        bin_dir, seen = os.path.join(tmp, "bin"), os.path.join(tmp, "seen")
        os.makedirs(bin_dir)
        probe, conf = os.path.join(tmp, "probe.py"), os.path.join(tmp, "config.json")
        with open(probe, "w") as fh:
            fh.write(self.PROBE)
        with open(conf, "w") as fh:
            json.dump(config or {}, fh)
        # npm waits for the backend to have started: run.sh stops both when one ends
        wait = "for i in $(seq 200); do [ -s %s ] && exit 0; sleep 0.05; done; exit 0" % seen
        for name, body in (("uv", "%s %s >> %s" % (sys.executable, probe, seen)),
                           ("npm", wait), ("npx", "exit 0")):
            path = os.path.join(bin_dir, name)
            with open(path, "w") as fh:
                fh.write("#!/bin/sh\n%s\n" % body)
            os.chmod(path, 0o755)
        dist = os.path.join(tmp, "dist")
        os.makedirs(dist)
        open(os.path.join(dist, "index.html"), "w").close()
        e = {k: v for k, v in os.environ.items() if not k.startswith("RC_DASH_")}
        e.update(PATH=bin_dir + os.pathsep + os.environ.get("PATH", ""), RC_DASH_STATIC_DIR=dist,
                 RC_DASH_CONFIG=conf)
        e.update(env)
        subprocess.run(["bash", self.RUN, mode], env=e, timeout=60, stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        with open(seen) as fh:
            got = {line for line in fh.read().splitlines() if line}
        self.assertEqual(len(got), 1, got)
        return json.loads(got.pop())

    def test_dev_mode_adds_vites_origins_to_the_configured_ones(self):
        got = self.origins("dev", self.PROXY)
        self.assertEqual(got["guard"], ["https://dash.example.org"] + self.VITE)
        self.assertIsNone(got["env"])                  # the config's list stands
        self.assertEqual(self.origins("dev")["guard"], self.VITE)
        self.assertEqual(self.origins("dev", RC_DASH_UI_PORT="6001")["guard"],
                         ["http://127.0.0.1:6001", "http://localhost:6001"])
        self.assertEqual(self.origins("dev", RC_DASH_UI_HOST="dev.example.org")["guard"],
                         self.VITE + ["http://dev.example.org:5173"])
        # what the shell named is kept, in either variable
        got = self.origins("dev", RC_DASH_ALLOWED_ORIGINS="https://a.example.org",
                           RC_DASH_DEV_ORIGINS="http://ui.example.org:5173")
        self.assertEqual(got["guard"], ["https://a.example.org", "http://ui.example.org:5173"]
                         + self.VITE)

    def test_without_vite_only_the_configured_ones(self):
        for mode in ("build", "api"):
            with self.subTest(mode=mode):
                self.assertEqual(self.origins(mode, self.PROXY)["guard"],
                                 ["https://dash.example.org"])


if __name__ == "__main__":
    unittest.main()
