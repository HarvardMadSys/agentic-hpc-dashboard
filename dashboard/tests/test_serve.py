"""`serve` with no password and no single sign-on refuses to start unless told
to serve open (rc_dashboard/__main__.py): on a login node, an open page is every
user's. A demo serves made-up data, and is open unless a password is set. The
start-up log names each way in that is on.

The app and the server are stand-ins here: what is tested is whether the
command gets as far as serving.

    cd dashboard && .venv/bin/python -m unittest tests.test_serve -q
"""
import io
import json
import os
import shutil
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rc_dashboard import __main__ as cli            # noqa: E402
from rc_dashboard import auth                       # noqa: E402

HASH = auth.hash_password("pw", n=16, r=1, p=1)


class Exited(Exception):
    """os._exit, which `main` ends a served run with."""


def _exit(code):
    raise Exited(code)


class Serve(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rc-serve-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        env = mock.patch.dict(os.environ, {k: v for k, v in os.environ.items()
                                           if not k.startswith("RC_DASH_")}, clear=True)
        env.start()
        self.addCleanup(env.stop)

    def config(self, **server):
        """A config file with these server settings; one that turns the proxy on
        gets a secret file of its own unless it names one."""
        auth_ = server.get("auth") or {}
        if auth_.get("proxy_header") and "proxy_secret_file" not in auth_:
            secret = os.path.join(self.tmp, "proxy.secret")
            with open(secret, "w") as fh:
                fh.write("a-secret-for-the-serve-tests_0123456789abcdef\n")
            os.chmod(secret, 0o600)
            server = dict(server, auth=dict(auth_, proxy_secret_file=secret))
        tree = {"cache": {"dir": os.path.join(self.tmp, "cache")}, "server": server}
        path = os.path.join(self.tmp, "c.json")
        with open(path, "w") as fh:
            json.dump(tree, fh)
        return path

    def run_cli(self, *argv):
        """(exit code, stderr, whether it served)."""
        auth_ = types.SimpleNamespace(lifetime_s=86400)
        made = []

        def create_app(cfg):
            made.append(cfg)
            gated = bool(cfg.get("server.auth.password_hash"))
            return types.SimpleNamespace(state=types.SimpleNamespace(
                auth=auth_ if gated else None, proxy=auth.proxy_from_config(cfg)))

        out, err = io.StringIO(), io.StringIO()
        with mock.patch("rc_dashboard.app.create_app", create_app), \
                mock.patch("uvicorn.run") as run, mock.patch("os._exit", _exit), \
                redirect_stdout(out), redirect_stderr(err):
            try:
                code = cli.main(list(argv))
            except Exited as e:
                code = e.args[0]
        self.assertEqual(bool(made), run.called)
        self.served_with = run.call_args.kwargs if run.called else None
        return code, err.getvalue(), run.called

    def test_no_password_no_service(self):
        for argv in (["serve"], []):             # serve is the default command
            with self.subTest(argv=argv):
                code, err, served = self.run_cli(*argv, "--config", self.config(port=8124))
                self.assertFalse(served)
                self.assertEqual(code, 2)
                for what in ("refusing to serve", "port 8124", "new-password",
                             '"password_hash": "<hash>"', "RC_DASH_PASSWORD_HASH", "--open",
                             "RC_DASH_OPEN=1"):
                    self.assertIn(what, err)

    def test_a_password_serves(self):
        code, err, served = self.run_cli("serve", "--config",
                                         self.config(auth={"password_hash": HASH}))
        self.assertTrue(served)
        self.assertEqual(code, 0)
        self.assertIn("sign-in on", err)
        with mock.patch.dict(os.environ, {"RC_DASH_PASSWORD_HASH": HASH}):
            self.assertTrue(self.run_cli("serve", "--config", self.config())[2])

    def test_open_only_when_told(self):
        path = self.config()
        code, err, served = self.run_cli("serve", "--config", path, "--open")
        self.assertTrue(served)
        self.assertIn("sign-in off (server.auth.open, set by cli:--open)", err)
        for raw, want in (("1", True), ("yes", True), ("0", False), ("off", False)):
            with self.subTest(RC_DASH_OPEN=raw), mock.patch.dict(os.environ, {"RC_DASH_OPEN": raw}):
                self.assertIs(self.run_cli("serve", "--config", path)[2], want)
        self.assertTrue(self.run_cli("serve", "--config", self.config(auth={"open": True}))[2])
        self.assertFalse(self.run_cli("serve", "--config", self.config(auth={"open": False}))[2])

    def test_the_refusal_names_single_sign_on_too(self):
        code, err, served = self.run_cli("serve", "--config", self.config(port=8124))
        self.assertFalse(served)
        self.assertIn("refusing to serve: there is no password", err)
        for what in ("no single sign-on", "server.auth.proxy_header", "RC_DASH_PROXY_HEADER",
                     "Single sign-on through a proxy"):
            self.assertIn(what, err)

    def test_single_sign_on_alone_serves_and_says_so(self):
        code, err, served = self.run_cli("serve", "--config", self.config(
            auth={"proxy_header": "X-Forwarded-User", "proxy_logout_url": "/oauth2/sign_out"}))
        self.assertTrue(served)
        self.assertEqual(code, 0)
        self.assertIn("sign-in on: single sign-on through a proxy, the user X-Forwarded-User "
                      "names, sent from 127.0.0.1, ::1 with the proxy's secret; any user it "
                      "names; Sign out goes to "
                      "/oauth2/sign_out; no password, so a request the proxy did not sign in "
                      "is refused", err)
        self.assertNotIn("sign-in off", err)
        secret = os.path.join(self.tmp, "proxy.secret")       # the one written above
        with mock.patch.dict(os.environ, {"RC_DASH_PROXY_HEADER": "Remote-User",
                                          "RC_DASH_PROXY_SECRET_FILE": secret}):
            code, err, served = self.run_cli("serve", "--config", self.config())
        self.assertTrue(served)
        self.assertIn("the user Remote-User names", err)

    def test_both_ways_in_are_named(self):
        code, err, served = self.run_cli("serve", "--config", self.config(
            auth={"proxy_header": "X-Forwarded-User", "password_hash": HASH}))
        self.assertTrue(served)
        lines = [ln for ln in err.splitlines() if ln.startswith("sign-in on:")]
        self.assertEqual(len(lines), 2, err)
        self.assertIn("single sign-on through a proxy", lines[0])
        self.assertIn("server.auth.password_hash is set", lines[1])
        self.assertNotIn("no password", err)

    def test_uvicorn_leaves_the_connections_address_to_the_app(self):
        # its own reading of X-Forwarded-For would put a header's address where the
        # proxy sign-in reads the connection's (auth.Peer)
        self.run_cli("serve", "--config", self.config(auth={"password_hash": HASH}))
        self.assertIs(self.served_with["proxy_headers"], False)
        self.run_cli("serve", "--config", self.config(auth={"proxy_header": "X-Forwarded-User"}))
        self.assertIs(self.served_with["proxy_headers"], False)

    def test_a_proxy_sign_in_that_could_not_work_stops_it(self):
        with self.assertRaises(SystemExit) as cm:
            self.run_cli("serve", "--config", self.config(
                auth={"proxy_header": "X-Forwarded-User", "proxy_trusted": []}))
        self.assertIn("proxy_trusted is empty", str(cm.exception))

    def test_what_does_not_serve_still_answers(self):
        code, _, served = self.run_cli("print-config", "--config", self.config())
        self.assertEqual((code, served), (0, False))

    def test_a_demo_is_open(self):
        d = os.path.join(self.tmp, "demo")
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(cli.main(["demo", "--print-config", "--demo-dir", d]), 0)
        leaf = json.loads(out.getvalue())["server"]["auth"]["open"]
        self.assertEqual(leaf, {"_value": True, "_origin": "demo"})


if __name__ == "__main__":
    unittest.main()
