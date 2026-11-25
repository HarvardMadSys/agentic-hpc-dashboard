"""Sign-in by the site's single sign-on, through a reverse proxy in front of the
service (rc_dashboard/auth.py: `Proxy`, `Peer`, and `AuthMiddleware` with them).

The proxy names the signed-in user in a header, believed only with the proxy's
secret (X-RC-Proxy-Secret) and only from a connection whose own address is
trusted. The helpers send the secret unless a test says not to. The first classes drive the settings, the check
and the middleware with hand-built requests, from any address, IPv6 among them
(this host may have none). The last start the real app on a free local port as
`serve` serves it (app.SERVER_OPTIONS) and send from 127.0.0.1 and 127.0.0.2,
so what uvicorn itself does with X-Forwarded-For is part of what is tested.

    cd dashboard && .venv/bin/python -m unittest tests.test_proxy_signin -q
"""
import asyncio
import base64
import http.client
import io
import ipaddress
import json
import os
import re
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
import warnings
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import uvicorn                                       # noqa: E402
from fastapi.routing import APIRoute                 # noqa: E402
from websockets.exceptions import InvalidStatus      # noqa: E402
from websockets.sync.client import connect           # noqa: E402

from rc_dashboard import app as appmod               # noqa: E402
from rc_dashboard import auth                        # noqa: E402
from rc_dashboard import config as configmod         # noqa: E402
from rc_dashboard import hours, snapshots            # noqa: E402

FAST = {"n": 16, "r": 1, "p": 1}
USER = ("X-Forwarded-User", "alice")
# The proxy's secret, as the helpers write it to a file and send it.
SECRET = b"the-proxys-secret_0123456789-abcdefghijklmnop"
SECRET_H = ("X-RC-Proxy-Secret", SECRET.decode())


def tmpdir(case):
    d = tempfile.mkdtemp(prefix="rc-proxy-")
    case.addCleanup(shutil.rmtree, d, True)
    return d


def secret_file(case, text=SECRET.decode() + "\n", mode=0o600, d=None):
    path = os.path.join(d or tmpdir(case), "proxy.secret")
    with open(path, "w") as fh:
        fh.write(text)
    os.chmod(path, mode)
    return path


def with_secret(case, auth_keys, d=None):
    """`auth_keys`, with a secret file of SECRET where they turn the proxy on and
    name none."""
    keys = dict(auth_keys)
    if str(keys.get("proxy_header") or "").strip() and "proxy_secret_file" not in keys:
        keys["proxy_secret_file"] = secret_file(case, d=d)
    return keys


def cfg_with(case, **auth_keys):
    tree = configmod.defaults()
    tree["cache"]["dir"] = os.path.join(tmpdir(case), "cache")
    tree["server"]["auth"].update(with_secret(case, auth_keys))
    return configmod.Config(tree, {}, None)


def proxy(header="X-Forwarded-User", trusted=("127.0.0.1", "::1"), groups_header="",
          groups=(), logout_url="", secret=SECRET):
    return auth.Proxy(header, [ipaddress.ip_network(t) for t in trusted], groups_header,
                      groups, logout_url, secret)


def scope_of(peer, headers=(), kind="http", path="/api/health", method="GET", secret=True):
    """A request as uvicorn hands it over from `peer`, with the connection's own
    address kept as `Peer` keeps it, and the proxy's secret unless `secret` is
    false. Header names go as written: uvicorn sends them in lower case, and a
    name in another case must match all the same."""
    client = (peer, 50000) if peer else None
    hs = [(k.encode("latin-1"), v if isinstance(v, bytes) else v.encode("utf-8"))
          for k, v in headers]
    if secret:
        hs.append((b"x-rc-proxy-secret", SECRET))
    return {"type": kind, "path": path, "method": method, "headers": hs, "query_string": b"",
            "scheme": "http", "client": client, auth.PEER: client}


class Settings(unittest.TestCase):
    """`server.auth.proxy_*`: what each takes, and what stops the service."""

    def test_off_by_default_and_trusting_this_host_alone(self):
        d = configmod.defaults()["server"]["auth"]
        self.assertEqual({k: v for k, v in d.items() if k.startswith("proxy_")},
                         {"proxy_header": "", "proxy_groups_header": "", "proxy_groups": [],
                          "proxy_trusted": ["127.0.0.1", "::1"], "proxy_logout_url": "",
                          "proxy_secret_file": ""})
        self.assertIsNone(auth.proxy_from_config(cfg_with(self)))
        self.assertFalse(auth.has_proxy(cfg_with(self)))
        self.assertFalse(auth.has_proxy(cfg_with(self, proxy_header="  ")))

    def test_each_variable_sets_its_key(self):
        path = os.path.join(tmpdir(self), "empty.json")
        with open(path, "w") as fh:
            fh.write("{}")
        env = {k: v for k, v in os.environ.items() if not k.startswith("RC_DASH_")}
        env.update({"RC_DASH_PROXY_HEADER": "X-Forwarded-User",
                    "RC_DASH_PROXY_GROUPS_HEADER": "X-Forwarded-Groups",
                    "RC_DASH_PROXY_GROUPS": "dash-readers, hpc-staff ops",
                    "RC_DASH_PROXY_TRUSTED": "10.0.0.5,fd00::/8",
                    "RC_DASH_PROXY_LOGOUT_URL": "/oauth2/sign_out",
                    "RC_DASH_PROXY_SECRET_FILE": secret_file(self)})
        with mock.patch.dict(os.environ, env, clear=True):
            cfg = configmod.load(None, path)
        want = {"proxy_header": ("X-Forwarded-User", "RC_DASH_PROXY_HEADER"),
                "proxy_groups_header": ("X-Forwarded-Groups", "RC_DASH_PROXY_GROUPS_HEADER"),
                "proxy_groups": (["dash-readers", "hpc-staff", "ops"], "RC_DASH_PROXY_GROUPS"),
                "proxy_trusted": (["10.0.0.5", "fd00::/8"], "RC_DASH_PROXY_TRUSTED"),
                "proxy_logout_url": ("/oauth2/sign_out", "RC_DASH_PROXY_LOGOUT_URL"),
                "proxy_secret_file": (env["RC_DASH_PROXY_SECRET_FILE"],
                                      "RC_DASH_PROXY_SECRET_FILE")}
        for key, (val, var) in want.items():
            with self.subTest(key=key):
                self.assertEqual(cfg.get("server.auth." + key), val)
                self.assertEqual(cfg.origin("server.auth." + key), "env:" + var)
                self.assertIn("%s = " % key, configmod.reference_toml())
                self.assertIn("# " + var, configmod.reference_toml())
        p = auth.proxy_from_config(cfg)
        self.assertEqual((p.header, p.groups_header, p.groups, p.logout_url),
                         ("X-Forwarded-User", "X-Forwarded-Groups",
                          frozenset(("dash-readers", "hpc-staff", "ops")), "/oauth2/sign_out"))
        self.assertEqual([str(n) for n in p.trusted], ["10.0.0.5/32", "fd00::/8"])

    def test_the_config_file_takes_text_and_lists_only(self):
        d = tmpdir(self)
        for key, val, words in (("proxy_trusted", "127.0.0.1", "is not a list of names"),
                                ("proxy_groups", "a,b", "is not a list of names"),
                                ("proxy_trusted", [1], "is not a list of names"),
                                ("proxy_header", 5, "is not text"),
                                ("proxy_logout_url", ["/x"], "is not text")):
            with self.subTest(key=key, val=val):
                path = os.path.join(d, "c.json")
                with open(path, "w") as fh:
                    json.dump({"server": {"auth": {key: val}}}, fh)
                with self.assertRaises(SystemExit) as cm:
                    configmod.load(None, path)
                self.assertIn("server.auth." + key, str(cm.exception))
                self.assertIn(words, str(cm.exception))

    def test_a_setting_it_could_not_work_with_stops_the_service(self):
        on = {"proxy_header": "X-Forwarded-User"}
        grouped = dict(on, proxy_groups_header="X-Forwarded-Groups")
        for keys, words in (
                ({"proxy_header": "X Forwarded User"}, "is not the name of a header"),
                ({"proxy_header": "X-User:"}, "is not the name of a header"),
                ({"proxy_header": "Authorization"}, "says something other than who signed in"),
                ({"proxy_header": "x-forwarded-for"}, "says something other than who signed in"),
                ({"proxy_header": "Host"}, "says something other than who signed in"),
                (dict(on, proxy_groups_header="Cookie"), "says something other than"),
                (dict(on, proxy_groups_header="x-forwarded-USER"), "both name"),
                (dict(grouped, proxy_groups=["dash readers"]), "is not the name of a group"),
                (dict(grouped, proxy_groups=["a,b"]), "is not the name of a group"),
                (dict(grouped, proxy_groups=[""]), "is not the name of a group"),
                (dict(on, proxy_groups=["dash-readers"]), "proxy_groups_header is not"),
                (dict(on, proxy_trusted=[]), "proxy_trusted is empty"),
                (dict(on, proxy_trusted=["*"]), "would believe the user any connection names"),
                (dict(on, proxy_trusted=["localhost"]), "is not an address or a network"),
                (dict(on, proxy_trusted=["127.0.0.1", "10.0.0.5/8"]),
                 "the network is 10.0.0.0/8"),
                (dict(on, proxy_logout_url="javascript:alert(1)"), "neither a path on this site"),
                (dict(on, proxy_logout_url="//evil.example/"), "neither a path on this site"),
                (dict(on, proxy_logout_url="/\\evil.example"), "neither a path on this site"),
                (dict(on, proxy_logout_url="/sign out"), "neither a path on this site"),
                (dict(on, proxy_logout_url="ftp://idp.example.org/"), "neither a path"),
                (dict(on, proxy_logout_url="https://"), "neither a path on this site"),
                (dict(on, proxy_logout_url="https://rc:hunter2-x@idp.example.org/logout"),
                 "holds what reads as a credential"),
                (dict(on, proxy_logout_url="https://idp.example.org/logout?access_token="
                                           "abcdef0123456789abcdef"),
                 "holds what reads as a credential")):
            with self.subTest(keys=keys):
                with self.assertRaises(SystemExit) as cm:
                    auth.proxy_from_config(cfg_with(self, **keys))
                self.assertIn(words, str(cm.exception))

    def test_what_it_takes(self):
        p = auth.proxy_from_config(cfg_with(
            self, proxy_header=" X-Forwarded-User ", proxy_groups_header="X-Forwarded-Groups",
            proxy_groups=["dash-readers", "HPC_Staff"],
            proxy_trusted=["10.0.0.0/8", " ::1 ", "192.0.2.7"],
            proxy_logout_url="https://idp.example.org/logout?client_id=dash"))
        self.assertEqual(p.header, "X-Forwarded-User")
        self.assertEqual(p.groups, frozenset(("dash-readers", "HPC_Staff")))
        self.assertEqual([str(n) for n in p.trusted], ["10.0.0.0/8", "::1/128", "192.0.2.7/32"])
        self.assertEqual(p.describe(), "the user X-Forwarded-User names, sent from 10.0.0.0/8, ::1, "
                                       "192.0.2.7 with the proxy's secret; a user in HPC_Staff, "
                                       "dash-readers, by "
                                       "X-Forwarded-Groups; Sign out goes to "
                                       "https://idp.example.org/logout?client_id=dash")
        for url in ("/oauth2/sign_out", "/redirect_uri?logout=https%3A%2F%2Fdash.example.org%2F",
                    "http://idp.example.org/logout", "/logout"):
            with self.subTest(url=url):
                self.assertEqual(auth.proxy_from_config(cfg_with(
                    self, proxy_header="Remote-User", proxy_logout_url=url)).logout_url, url)
        self.assertIn("no Sign out", auth.proxy_from_config(
            cfg_with(self, proxy_header="Remote-User")).describe())

    def test_its_settings_while_it_is_off_are_named(self):
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertIsNone(auth.proxy_from_config(cfg_with(
                self, proxy_groups=["dash-readers"], proxy_groups_header="X-Forwarded-Groups",
                proxy_logout_url="/oauth2/sign_out")))
        for key in ("proxy_groups", "proxy_groups_header", "proxy_logout_url"):
            self.assertIn("server.auth.%s is set" % key, err.getvalue())
        self.assertIn("proxy sign-in is off", err.getvalue())

    def test_neither_saved_state_fingerprint_moves(self):
        """`server` is in neither fingerprint, so turning single sign-on on,
        changing it, or off is a restart, never a rebuild."""
        a = cfg_with(self)
        b = cfg_with(self, proxy_header="X-Forwarded-User",
                     proxy_groups_header="X-Forwarded-Groups", proxy_groups=["dash-readers"],
                     proxy_trusted=["10.0.0.0/8"], proxy_logout_url="/oauth2/sign_out")
        b.tree["cache"]["dir"] = a.tree["cache"]["dir"]
        for fp in (snapshots.fingerprint, hours.fingerprint):
            with self.subTest(fp=fp.__module__):
                self.assertEqual(fp("code", a, ["/r"]), fp("code", b, ["/r"]))
                # not because the tree is ignored whole: a setting a reduction reads moves it
                c = cfg_with(self)
                c.tree["cache"]["dir"] = a.tree["cache"]["dir"]
                c.tree["live"]["bin_s"] = a.tree["live"]["bin_s"] + 1
                self.assertNotEqual(fp("code", a, ["/r"]), fp("code", c, ["/r"]))
        for key in ("server",):
            self.assertIn(key, hours.CONFIG_IGNORED)


class Trust(unittest.TestCase):
    """`Proxy.signs_in`: whom the header is believed from, and what it must hold."""

    def test_from_a_trusted_address_the_proxy_names_the_user(self):
        for peer in ("127.0.0.1", "::1"):
            with self.subTest(peer=peer):
                self.assertEqual(proxy().signs_in(scope_of(peer, [USER])), ("alice", None))

    def test_from_any_other_address_the_header_is_as_if_absent(self):
        p = proxy()
        with redirect_stderr(io.StringIO()):
            for peer in ("127.0.0.2", "10.0.0.1", "::2", "192.0.2.1"):
                with self.subTest(peer=peer):
                    self.assertEqual(p.signs_in(scope_of(peer, [USER])), (None, "peer"))
                    self.assertEqual(p.signs_in(scope_of(peer)), (None, "peer"))

    def test_without_the_connections_own_address_nothing_is_believed(self):
        p = proxy()
        s = scope_of("127.0.0.1", [USER])
        del s[auth.PEER]                       # a request `Peer` did not see: `client` is no proof
        with redirect_stderr(io.StringIO()):
            self.assertEqual(p.signs_in(s), (None, "peer"))
            for peer in (None, "", "testclient", "/run/rc.sock", "localhost"):
                with self.subTest(peer=peer):
                    s = scope_of("127.0.0.1", [USER])
                    s[auth.PEER] = (peer, 0) if peer is not None else None
                    self.assertEqual(p.signs_in(s), (None, "peer"))

    def test_networks_and_ipv6(self):
        p = proxy(trusted=("10.0.0.0/8", "fd00::/8", "192.0.2.7", "2001:db8::1"))
        with redirect_stderr(io.StringIO()):
            for peer, ok in (("10.1.2.3", True), ("10.255.255.255", True), ("11.0.0.1", False),
                             ("fd12:3456::5", True), ("fe80::1", False), ("192.0.2.7", True),
                             ("192.0.2.8", False), ("2001:db8::1", True),
                             ("2001:0db8:0000::0001", True), ("2001:db8::2", False),
                             ("127.0.0.1", False), ("::1", False)):
                with self.subTest(peer=peer):
                    self.assertEqual(p.signs_in(scope_of(peer, [USER])).why is None, ok)

    def test_an_ipv4_address_on_an_ipv6_socket_is_that_address(self):
        # a dual-stack listener reports 127.0.0.1 as ::ffff:127.0.0.1
        p = proxy(trusted=("127.0.0.1", "10.0.0.0/8"))
        with redirect_stderr(io.StringIO()):
            for peer, ok in (("::ffff:127.0.0.1", True), ("::ffff:10.9.8.7", True),
                             ("::ffff:192.0.2.1", False)):
                with self.subTest(peer=peer):
                    self.assertEqual(p.signs_in(scope_of(peer, [USER])).why is None, ok)

    def test_a_forwarded_address_is_never_the_connections(self):
        p = proxy(trusted=("127.0.0.1",))
        fwd = [("X-Forwarded-For", "127.0.0.1"), ("X-Real-IP", "127.0.0.1"),
               ("Forwarded", "for=127.0.0.1"), ("X-Forwarded-Host", "127.0.0.1")]
        with redirect_stderr(io.StringIO()):
            self.assertEqual(p.signs_in(scope_of("192.0.2.1", [USER] + fwd)), (None, "peer"))
        # and from the proxy, the client's address it adds is no matter
        self.assertEqual(p.signs_in(scope_of("127.0.0.1", [USER, ("X-Forwarded-For",
                                                                  "203.0.113.9")])),
                         ("alice", None))
        # what uvicorn would have made of X-Forwarded-For (`client`) is not read
        s = scope_of("192.0.2.1", [USER])
        s["client"] = ("127.0.0.1", 0)
        with redirect_stderr(io.StringIO()):
            self.assertEqual(p.signs_in(s), (None, "peer"))

    def test_header_names_match_in_any_case(self):
        for configured in ("X-Forwarded-User", "x-forwarded-user", "X-FORWARDED-USER"):
            for sent in ("x-forwarded-user", "X-Forwarded-User", "X-FORWARDED-USER"):
                with self.subTest(configured=configured, sent=sent):
                    self.assertEqual(proxy(header=configured).signs_in(
                        scope_of("127.0.0.1", [(sent, "alice")])), ("alice", None))
        p = proxy(groups_header="X-Forwarded-Groups", groups=("dash-readers",))
        self.assertEqual(p.signs_in(scope_of("127.0.0.1", [
            ("x-forwarded-user", "alice"), ("X-FORWARDED-GROUPS", "dash-readers")])),
            ("alice", None))
        # not a name like it: `_` is not `-` (where CGI would read them alike)
        self.assertEqual(proxy().signs_in(scope_of("127.0.0.1", [("X_Forwarded_User", "alice")])),
                         (None, "missing"))

    def test_no_name_is_missing(self):
        self.assertEqual(proxy().signs_in(scope_of("127.0.0.1", [("Remote-User", "alice")])),
                         (None, "missing"))

    def test_an_empty_oversized_or_odd_name_is_refused(self):
        p = proxy()
        top = auth.MAX_PROXY_USER
        for value in ("", " ", "\t", "x" * (top + 1), "é" * (top + 1), "al\x00ice", "alice\x7f",
                      "al\tice", "al\x1bice", "\x85alice", "‮alice", "ali​ce",
                      "a b", b"x" * (4 * top + 1)):
            with self.subTest(value=value[:12]):
                self.assertEqual(p.signs_in(scope_of("127.0.0.1", [("X-Forwarded-User", value)])),
                                 (None, "bad"))
        for value, name in (("x" * top, "x" * top), (" alice ", "alice"), ("Jane Doe", "Jane Doe"),
                            ("alice@example.org", "alice@example.org"),
                            ("CN=Jane,O=Example", "CN=Jane,O=Example")):
            with self.subTest(value=value[:12]):
                self.assertEqual(p.signs_in(scope_of("127.0.0.1", [("X-Forwarded-User", value)])),
                                 (name, None))

    def test_the_header_twice_is_refused(self):
        # a proxy that adds its own beside the client's: one of them is the client's
        two = [("X-Forwarded-User", "alice"), ("X-Forwarded-User", "mallory")]
        self.assertEqual(proxy().signs_in(scope_of("127.0.0.1", two)), (None, "bad"))
        same = [USER, USER]
        self.assertEqual(proxy().signs_in(scope_of("127.0.0.1", same)), (None, "bad"))

    def test_a_name_in_utf8_or_latin1(self):
        p = proxy()
        for raw in ("josé".encode("utf-8"), "josé".encode("latin-1")):
            with self.subTest(raw=raw):
                self.assertEqual(p.signs_in(scope_of("127.0.0.1", [("X-Forwarded-User", raw)])),
                                 ("josé", None))
        # Latin-1's C1 controls are controls
        self.assertEqual(p.signs_in(scope_of("127.0.0.1", [("X-Forwarded-User", b"a\x9bb")])),
                         (None, "bad"))

    def test_groups(self):
        p = proxy(groups_header="X-Forwarded-Groups", groups=("dash-readers", "hpc-staff"))

        def groups(*values):
            return p.signs_in(scope_of("127.0.0.1", [USER] + [("X-Forwarded-Groups", v)
                                                              for v in values]))
        for value in ("dash-readers", "staff,dash-readers", "staff dash-readers",
                      " staff ,\tdash-readers ", "hpc-staff", "a,,b,hpc-staff,"):
            with self.subTest(value=value):
                self.assertEqual(groups(value), ("alice", None))
        for values in ((), ("staff",), ("",), ("Dash-Readers",), ("dash-readers-old",),
                       ("dash",), ("staff", "dash-readers"), ("dash-readers", "dash-readers"),
                       ("dash-readers,a\x00b",), ("dash-readers ‮",),
                       ("dash-readers," + "g" * auth.MAX_PROXY_GROUPS,)):
            with self.subTest(values=[v[:30] for v in values]):
                self.assertEqual(groups(*values), ("alice", "group"))
        # the user is checked before the groups: no name, no group check
        self.assertEqual(p.signs_in(scope_of("127.0.0.1", [("X-Forwarded-Groups",
                                                            "dash-readers")])),
                         (None, "missing"))

    def test_an_empty_allow_list_takes_any_user_the_proxy_names(self):
        p = proxy(groups_header="X-Forwarded-Groups")
        for extra in ([], [("X-Forwarded-Groups", "staff")], [("X-Forwarded-Groups", "a"),
                                                             ("X-Forwarded-Groups", "b")]):
            with self.subTest(extra=extra):
                self.assertEqual(p.signs_in(scope_of("127.0.0.1", [USER] + extra)),
                                 ("alice", None))

    def test_a_header_from_an_untrusted_address_is_said_once_a_minute_never_its_value(self):
        p = proxy()
        err = io.StringIO()
        with redirect_stderr(err):
            for _ in range(3):
                p.signs_in(scope_of("192.0.2.1", [("X-Forwarded-User", "secret-value")]))
            p.signs_in(scope_of("192.0.2.9"))          # no header: nothing to say
        self.assertEqual(err.getvalue().count("\n"), 1, err.getvalue())
        self.assertIn("192.0.2.1", err.getvalue())
        self.assertIn("X-Forwarded-User", err.getvalue())
        self.assertIn("server.auth.proxy_trusted", err.getvalue())
        self.assertNotIn("secret-value", err.getvalue())
        p._said -= auth.SAY_EVERY_S + 1
        with redirect_stderr(err):
            p.signs_in(scope_of("192.0.2.2", [USER]))
        self.assertIn("192.0.2.2", err.getvalue())


class Secret(unittest.TestCase):
    """The proxy's secret: without it, once and as written, a named user is passed
    over, since every account on the dashboard's host connects from 127.0.0.1."""

    def test_without_it_a_named_user_is_passed_over_and_said(self):
        p = proxy()
        wrong = ("X-RC-Proxy-Secret", SECRET.decode()[:-1] + "x")
        err = io.StringIO()
        with redirect_stderr(err):
            for headers in ([USER], [USER, wrong], [USER, SECRET_H, SECRET_H],
                            [USER, ("X-RC-Proxy-Secret", "")]):
                with self.subTest(headers=headers):
                    self.assertEqual(p.signs_in(scope_of("127.0.0.1", headers, secret=False)),
                                     (None, "secret"))
        self.assertEqual(err.getvalue().count("without the proxy's secret"), 1)   # once a minute
        # its header's name in any case, and the blanks around its value, are as HTTP has them
        self.assertEqual(p.signs_in(scope_of("127.0.0.1", [USER, ("x-RC-proxy-SECRET",
                                                                  " %s " % SECRET.decode())],
                                             secret=False)), ("alice", None))

    def test_with_it_and_no_user_the_password_decides(self):
        self.assertEqual(proxy().signs_in(scope_of("127.0.0.1")), (None, "missing"))

    def test_it_goes_no_further_than_the_sign_in(self):
        seen = {}

        async def app(scope, receive, send):
            seen["headers"] = [k.lower() for k, _v in scope["headers"]]

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(msg):
            pass

        asyncio.run(auth.AuthMiddleware(app, proxy=proxy())(
            scope_of("127.0.0.1", [USER]), receive, send))
        self.assertIn(b"x-forwarded-user", seen["headers"])
        self.assertNotIn(b"x-rc-proxy-secret", seen["headers"])

    def test_its_file_is_checked(self):
        d = tmpdir(self)
        with self.assertRaises(SystemExit) as cm:
            auth.read_proxy_secret("")
        self.assertIn("proxy_secret_file is not set", str(cm.exception))
        self.assertIn("secrets.token_urlsafe", str(cm.exception))
        for text, mode, words in (
                (None, 0o600, "No such file"),
                (SECRET.decode(), 0o644, "readable by every account"),
                ("short-secret", 0o600, "32 characters or more"),
                (SECRET.decode() + " more", 0o600, "32 characters or more"),
                (SECRET.decode() + "\n" + SECRET.decode(), 0o600, "on one line"),
                ("", 0o600, "on one line")):
            with self.subTest(text=text, mode=oct(mode)):
                path = os.path.join(d, "s%d" % len(os.listdir(d)))
                if text is not None:
                    secret_file(self, text, mode, d=d)
                    os.rename(os.path.join(d, "proxy.secret"), path)
                with self.assertRaises(SystemExit) as cm:
                    auth.read_proxy_secret(path, "file:x")
                self.assertIn(words, str(cm.exception))
        path = secret_file(self, "# the proxy's\n\n  %s  \n" % SECRET.decode(), d=d)
        self.assertEqual(auth.read_proxy_secret(path), SECRET)

    def test_its_header_is_no_users_header(self):
        for key in ("proxy_header", "proxy_groups_header"):
            keys = {"proxy_header": "X-Forwarded-User", key: "X-RC-Proxy-Secret"}
            with self.subTest(key=key), self.assertRaises(SystemExit) as cm:
                auth.proxy_from_config(cfg_with(self, **keys))
            self.assertIn("says something other than who signed in", str(cm.exception))

    def test_a_proxy_without_one_does_not_start(self):
        with self.assertRaises(SystemExit) as cm:
            auth.proxy_from_config(cfg_with(self, proxy_header="X-Forwarded-User",
                                            proxy_secret_file=""))
        self.assertIn("proxy_secret_file is not set", str(cm.exception))


class Result:
    """What AuthMiddleware did with one request (`through`)."""

    def __init__(self, sent):
        first = sent[0]
        self.reader = first.get("reader")
        self.kind = first["type"]
        self.status = first.get("status")
        self.headers = {k.decode(): v.decode() for k, v in first.get("headers") or ()}
        self.body = b"".join(m.get("body", b"") for m in sent[1:]
                             if m["type"] == "http.response.body")


def through(scope, **kwargs):
    sent = []

    async def receive():
        return {"type": "websocket.connect"} if scope["type"] == "websocket" else \
            {"type": "http.request", "body": b"", "more_body": False}

    async def send(msg):
        sent.append(msg)

    async def app(scope, receive, send):
        sent.append({"type": "app", "reader": scope.get(auth.READER)})

    with redirect_stderr(io.StringIO()):
        asyncio.run(auth.AuthMiddleware(app, **kwargs)(scope, receive, send))
    return Result(sent)


class Middleware(unittest.TestCase):
    """`AuthMiddleware` with the proxy: alone, and beside the password."""

    def gate(self):
        self.stored = auth.hash_password("pw", **FAST)
        return auth.Gate(self.stored, auth.load_session_key(tmpdir(self)),
                         cookie_name="rc_dash_test", lifetime_s=3600, fail_delay_s=0)

    def test_alone_it_admits_the_user_it_names_and_says_how(self):
        for kind, path in (("http", "/api/health"), ("http", "/"), ("http", "/assets/app.js"),
                           ("http", "/metrics"), ("websocket", "/ws")):
            with self.subTest(path=path):
                r = through(scope_of("127.0.0.1", [USER], kind, path), proxy=proxy())
                self.assertEqual(r.kind, "app")
                self.assertEqual(r.reader, {"method": "proxy", "user": "alice"})

    def test_alone_a_request_it_did_not_sign_in_gets_a_401_in_plain_text(self):
        p = proxy()
        for peer, headers in (("127.0.0.1", []), ("127.0.0.1", [("X-Forwarded-User", "")]),
                              ("127.0.0.1", [("Remote-User", "alice")]),
                              ("192.0.2.1", [USER]), ("192.0.2.1", [])):
            for path, method in (("/api/health", "GET"), ("/", "GET"), ("/assets/app.js", "GET"),
                                 ("/metrics", "GET"), ("/api/ask", "POST"), ("/login", "GET"),
                                 ("/login", "POST"), ("/logout", "POST")):
                with self.subTest(peer=peer, headers=headers, path=path, method=method):
                    r = through(scope_of(peer, headers, path=path, method=method), proxy=p)
                    self.assertEqual(r.status, 401)
                    self.assertTrue(r.headers["content-type"].startswith("text/plain"))
                    self.assertEqual(r.headers["cache-control"], "no-store")
                    self.assertEqual(r.headers["x-content-type-options"], "nosniff")
                    self.assertEqual(r.body.decode(), auth.BEHIND_SSO)
                    self.assertNotIn("www-authenticate", r.headers)

    def test_alone_a_user_in_no_allowed_group_gets_a_403_naming_no_group(self):
        p = proxy(groups_header="X-Forwarded-Groups", groups=("dash-readers", "hpc-staff"))
        r = through(scope_of("127.0.0.1", [USER, ("X-Forwarded-Groups", "staff")]), proxy=p)
        self.assertEqual(r.status, 403)
        self.assertTrue(r.headers["content-type"].startswith("text/plain"))
        self.assertIn("Signed in as alice", r.body.decode())
        self.assertIn("not in a group allowed", r.body.decode())
        for g in ("dash-readers", "hpc-staff", "staff"):
            self.assertNotIn(g, r.body.decode())
        r = through(scope_of("127.0.0.1", [USER, ("X-Forwarded-Groups", "hpc-staff")]), proxy=p)
        self.assertEqual(r.kind, "app")

    def test_alone_a_websocket_it_did_not_sign_in_is_refused_before_it_opens(self):
        p = proxy(groups_header="X-Forwarded-Groups", groups=("dash-readers",))
        for peer, headers in (("127.0.0.1", []), ("192.0.2.1", [USER]), ("127.0.0.1", [USER])):
            with self.subTest(peer=peer, headers=headers):
                r = through(scope_of(peer, headers, "websocket", "/ws"), proxy=p)
                self.assertEqual(r.kind, "websocket.close")

    def test_beside_the_password_a_request_it_did_not_sign_in_falls_back(self):
        g = self.gate()
        p = proxy(groups_header="X-Forwarded-Groups", groups=("dash-readers",))
        for peer, headers in (("192.0.2.1", [USER]), ("127.0.0.1", []),
                              ("127.0.0.1", [USER, ("X-Forwarded-Groups", "staff")])):
            with self.subTest(peer=peer, headers=headers):
                r = through(scope_of(peer, headers, path="/", method="GET"), gate=g, proxy=p)
                self.assertEqual(r.status, 401)
                self.assertIn(b'type="password" name="password"', r.body)    # the form
                r = through(scope_of(peer, headers), gate=g, proxy=p)
                self.assertEqual(r.status, 401)
                self.assertIn(b"needs its password", r.body)                 # JSON, as ever
                self.assertEqual(through(scope_of(peer, headers, "websocket", "/ws"), gate=g,
                                         proxy=p).kind, "websocket.close")
                # and the password opens it (header names in lower case, as uvicorn sends them)
                tok = "rc_dash_test=" + g.token()
                r = through(scope_of(peer, headers + [("cookie", tok)]), gate=g, proxy=p)
                self.assertEqual((r.kind, r.reader), ("app", {"method": "password",
                                                              "user": None}))
                basic = "Basic " + base64.b64encode(b"rc:pw").decode()
                r = through(scope_of(peer, headers + [("authorization", basic)]), gate=g, proxy=p)
                self.assertEqual(r.kind, "app")

    def test_beside_the_password_the_proxys_user_goes_in_and_the_login_url_on(self):
        g = self.gate()
        r = through(scope_of("127.0.0.1", [USER]), gate=g, proxy=proxy())
        self.assertEqual((r.kind, r.reader), ("app", {"method": "proxy", "user": "alice"}))
        s = scope_of("127.0.0.1", [USER], path="/login")
        s["query_string"] = b"next=/%3Ftab%3Drisk"
        r = through(s, gate=g, proxy=proxy())
        self.assertEqual((r.status, r.headers["location"]), (303, "/?tab=risk"))
        # not signed in by it, the form
        s = scope_of("192.0.2.1", [USER], path="/login")
        self.assertEqual(through(s, gate=g, proxy=proxy()).status, 200)

    def test_neither_way_in_is_no_sign_in(self):
        with self.assertRaises(ValueError):
            auth.AuthMiddleware(None)


class PeerKept(unittest.TestCase):
    """`Peer`: the connection's own address is kept before a forwarded header is read."""

    def run_peer(self, client, headers, trusted=None, kept=None):
        seen = {}

        async def app(scope, receive, send):
            seen.update(scope)

        scope = {"type": "http", "path": "/", "method": "GET", "scheme": "http",
                 "client": client, "headers": [(k.lower().encode(), v.encode())
                                               for k, v in headers]}
        if kept is not None:
            scope[auth.PEER] = kept
        asyncio.run(auth.Peer(app, trusted)(scope, None, None))
        return seen

    def test_kept_then_the_forwarded_headers_read_as_uvicorn_reads_them(self):
        fwd = [("X-Forwarded-For", "203.0.113.9"), ("X-Forwarded-Proto", "https")]
        seen = self.run_peer(("127.0.0.1", 41000), fwd)
        self.assertEqual(seen[auth.PEER], ("127.0.0.1", 41000))
        self.assertEqual(seen["client"][0], "203.0.113.9")          # the access log's
        self.assertEqual(seen["scheme"], "https")                   # a Secure cookie
        # from an address uvicorn does not take them from, neither is read
        seen = self.run_peer(("192.0.2.1", 41000), fwd)
        self.assertEqual((seen[auth.PEER], seen["client"], seen["scheme"]),
                         (("192.0.2.1", 41000), ("192.0.2.1", 41000), "http"))

    def test_kept_once(self):
        seen = self.run_peer(("127.0.0.1", 1), [], kept=("192.0.2.1", 2))
        self.assertEqual(seen[auth.PEER], ("192.0.2.1", 2))

    def test_the_addresses_uvicorn_takes_them_from(self):
        with mock.patch.dict(os.environ, {"FORWARDED_ALLOW_IPS": "192.0.2.1"}):
            self.assertEqual(auth.forwarded_allow_ips(), "192.0.2.1")
            seen = self.run_peer(("192.0.2.1", 1), [("X-Forwarded-Proto", "https")])
        self.assertEqual(seen["scheme"], "https")
        env = {k: v for k, v in os.environ.items() if k != "FORWARDED_ALLOW_IPS"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(auth.forwarded_allow_ips(), "127.0.0.1,::1")

    def test_served_as_serve_serves_it(self):
        self.assertIs(appmod.SERVER_OPTIONS["proxy_headers"], False)


def app_with(case, anonymize=False, **auth_keys):
    """The app, not served: its own stack of middleware, driven by `ask`."""
    tmp = tmpdir(case)
    tree = configmod.defaults()
    tree["cache"]["dir"] = os.path.join(tmp, "cache")
    tree["cache"]["state_dir"] = os.path.join(tmp, "state")
    tree["feeds"]["ebpf"]["roots"] = [os.path.join(tmp, "ebpf")]
    tree["feeds"]["ebpf_node"]["roots"] = [os.path.join(tmp, "node")]
    tree["server"]["static_dir"] = os.path.join(tmp, "no-ui")
    tree["panels"]["prewarm_min"] = []
    tree["server"]["auth"].update(with_secret(case, auth_keys, d=tmp))
    if anonymize:
        tree["anonymize"]["enabled"] = True
        tree["anonymize"]["salt"] = "a-salt-sixteen-bytes-or-more"
    with warnings.catch_warnings(), redirect_stderr(io.StringIO()):
        warnings.simplefilter("ignore", DeprecationWarning)
        return appmod.create_app(configmod.Config(tree, {}, None))


def ask(app, path, peer=("127.0.0.1", 50000), headers=(), secret=True):
    """(status, body) of one GET through the whole app, from `peer`, as uvicorn
    would hand it over with its own forwarded-header reading off; with the
    proxy's secret unless `secret` is false."""
    hs = [(b"host", b"127.0.0.1:8080")] + [(k.lower().encode(), v.encode()) for k, v in headers]
    if secret:
        hs.append((b"x-rc-proxy-secret", SECRET))
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET",
             "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": b"",
             "root_path": "", "headers": hs, "client": peer, "server": ("127.0.0.1", 8080)}
    msgs = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(m):
        msgs.append(m)

    with redirect_stderr(io.StringIO()):
        asyncio.run(app(scope, receive, send))
    start = next(m for m in msgs if m["type"] == "http.response.start")
    return start["status"], b"".join(m.get("body", b"") for m in msgs
                                     if m["type"] == "http.response.body")


def auth_leaf(body):
    return json.loads(body)["_meta"]["auth"]


class WholeApp(unittest.TestCase):
    """The app's own middleware, from any address: IPv6, and IPv4 on an IPv6 socket."""

    def test_by_default_this_hosts_own_addresses_are_trusted(self):
        app = app_with(self, proxy_header="X-Forwarded-User")
        self.assertIsNone(app.state.auth)
        self.assertIsNotNone(app.state.proxy)
        for peer, want in ((("127.0.0.1", 1), 200), (("::1", 1), 200),
                           (("::ffff:127.0.0.1", 1), 200), (("127.0.0.2", 1), 401),
                           (("::2", 1), 401), (("10.0.0.1", 1), 401), (None, 401)):
            with self.subTest(peer=peer):
                status, body = ask(app, "/api/config", peer, [USER])
                self.assertEqual(status, want)
                if want == 200:
                    self.assertEqual(auth_leaf(body), {
                        "_value": {"method": "proxy", "user": "alice", "logout_url": None},
                        "_origin": "request"})

    def test_a_forwarded_address_from_this_host_is_not_the_proxys(self):
        # 127.0.0.1 is where uvicorn takes X-Forwarded-For from (auth.Peer), and the
        # proxy is trusted at 192.0.2.10 alone: a local process naming it is not it
        app = app_with(self, proxy_header="X-Forwarded-User", proxy_trusted=["192.0.2.10"])
        status, _ = ask(app, "/api/health", ("127.0.0.1", 1),
                        [USER, ("X-Forwarded-For", "192.0.2.10")])
        self.assertEqual(status, 401)
        status, _ = ask(app, "/api/health", ("192.0.2.10", 1),
                        [USER, ("X-Forwarded-For", "198.51.100.7")])
        self.assertEqual(status, 200)

    def test_the_middleware_in_place(self):
        app = app_with(self, proxy_header="X-Forwarded-User")
        stack = [m.cls for m in app.user_middleware]
        self.assertIs(stack[0], auth.Peer)                       # outside everything
        self.assertIn(auth.AuthMiddleware, stack)
        open_app = app_with(self)
        self.assertIsNone(open_app.state.proxy)
        self.assertFalse([m for m in open_app.user_middleware if m.cls is auth.AuthMiddleware])
        self.assertIs(open_app.user_middleware[0].cls, auth.Peer)

    def test_an_open_page_says_so(self):
        status, body = ask(app_with(self), "/api/config")
        self.assertEqual((status, auth_leaf(body)), (200, {
            "_value": {"method": "open", "user": None, "logout_url": None},
            "_origin": "request"}))

    def test_sign_out_goes_where_the_proxy_says(self):
        app = app_with(self, proxy_header="X-Forwarded-User",
                       proxy_logout_url="/oauth2/sign_out")
        status, body = ask(app, "/api/config", headers=[USER])
        self.assertEqual(auth_leaf(body)["_value"]["logout_url"], "/oauth2/sign_out")
        tree = json.loads(body)
        self.assertEqual(tree["server"]["auth"]["proxy_logout_url"]["_value"],
                         "/oauth2/sign_out")

    def test_names_masked_the_readers_own_name_is_kept_and_teaches_nothing(self):
        app = app_with(self, anonymize=True, proxy_header="X-Forwarded-User")
        status, body = ask(app, "/api/config", headers=[("X-Forwarded-User", "jdoe")])
        self.assertEqual(status, 200)
        self.assertEqual(auth_leaf(body)["_value"],
                         {"method": "proxy", "user": "jdoe", "logout_url": None})
        self.assertNotIn("jdoe", app.state.agg.anon.users)

    def test_what_reads_as_a_credential_in_a_name_is_masked_on_the_way_out(self):
        jwt = ("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0."
               "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c")
        self.assertNotEqual(auth.shown(jwt), jwt)
        for name in ("alice", "alice@example.org", "Jane Doe", "CN=Jane,O=Example"):
            self.assertEqual(auth.shown(name), name)
        got = auth.reader({auth.READER: {"method": "proxy", "user": jwt}}, proxy())
        self.assertNotIn(jwt, json.dumps(got))
        self.assertEqual(auth.reader({auth.READER: {"method": "password", "user": None}},
                                     proxy(logout_url="/x")),
                         {"method": "password", "user": None, "logout_url": None})


class Served(unittest.TestCase):
    """The real app on a free local port, served as `serve` serves it
    (app.SERVER_OPTIONS), over an empty feed and a stand-in bundle."""

    AUTH = {}

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="rc-proxy-app-")
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
        tree["server"]["auth"].update(cls.AUTH)
        if cls.AUTH.get("proxy_header"):
            path = os.path.join(cls.tmp, "proxy.secret")
            with open(path, "w") as fh:
                fh.write(SECRET.decode() + "\n")
            os.chmod(path, 0o600)
            tree["server"]["auth"]["proxy_secret_file"] = path
        with warnings.catch_warnings(), redirect_stderr(io.StringIO()):
            warnings.simplefilter("ignore", DeprecationWarning)    # on_event
            cls.app = appmod.create_app(configmod.Config(tree, {}, None))
        if cls.app.state.auth is not None:
            cls.app.state.auth.fail_delay_s = 0
        cls.server = uvicorn.Server(uvicorn.Config(
            cls.app, host="127.0.0.1", port=cls.port, log_level="error",
            **appmod.SERVER_OPTIONS))
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

    def req(self, method, path, headers=(), src="127.0.0.1", body=None, host=None, secret=True):
        """(status, headers, body) of one request sent from address `src`, with
        the proxy's secret unless `secret` is false. `headers` is a list of
        pairs, so a header may go twice."""
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30,
                                       source_address=(src, 0))
        try:
            c.putrequest(method, path, skip_host=host is not None, skip_accept_encoding=True)
            if host is not None:
                c.putheader("Host", host)
            for k, v in list(headers) + ([SECRET_H] if secret else []):
                c.putheader(k, v)
            if body is not None:
                c.putheader("Content-Length", str(len(body)))
            c.endheaders(body)
            r = c.getresponse()
            return r.status, {k.lower(): v for k, v in r.getheaders()}, r.read()
        finally:
            c.close()

    def ws(self, headers=(), src="127.0.0.1", secret=True):
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=20,
                                        source_address=(src, 0))
        return connect("ws://127.0.0.1:%d/ws" % self.port, sock=sock, open_timeout=20,
                       additional_headers=list(headers) + ([SECRET_H] if secret else []))

    def refused_ws(self, headers=(), src="127.0.0.1", secret=True):
        with self.assertRaises(InvalidStatus) as cm:
            with self.ws(headers, src, secret):
                pass
        return cm.exception.response.status_code

    def every_route(self):
        """Every API route the app has, the page, an asset, /metrics and the
        sign-in URLs, as (method, path) with any {param} filled."""
        out = []
        for r in self.app.routes:
            if isinstance(r, APIRoute):
                out += [(m, re.sub(r"\{[^}]+\}", "x", r.path)) for m in sorted(r.methods)]
        self.assertIn(("GET", "/metrics"), out)
        self.assertGreater(len(out), 15)
        return out + [("GET", "/"), ("GET", "/index.html"), ("GET", "/assets/app.js"),
                      ("GET", "/favicon.ico"), ("HEAD", "/"), ("POST", "/"), ("GET", "/login"),
                      ("POST", "/login"), ("POST", "/logout")]


SIGNED = [("X-Forwarded-User", "alice"), ("X-Forwarded-Groups", "staff, dash-readers")]


class SignOnAlone(Served):
    """No password: the proxy, trusted at 127.0.0.1, is the one way in."""

    AUTH = {"proxy_header": "X-Forwarded-User", "proxy_groups_header": "X-Forwarded-Groups",
            "proxy_groups": ["dash-readers"], "proxy_trusted": ["127.0.0.1"],
            "proxy_logout_url": "/oauth2/sign_out"}

    def test_nothing_answers_a_request_the_proxy_did_not_sign_in(self):
        with redirect_stderr(io.StringIO()):
            for headers, src in (([], "127.0.0.1"), (SIGNED, "127.0.0.2")):
                for method, path in self.every_route():
                    with self.subTest(src=src, method=method, path=path):
                        body = b"{}" if method in ("POST", "PUT", "PATCH") else None
                        hs = list(headers) + ([("Content-Type", "application/json")]
                                              if body else [])
                        status, h, got = self.req(method, path, hs, src, body)
                        self.assertEqual(status, 401)
                        self.assertTrue(h["content-type"].startswith("text/plain"))
                        self.assertEqual(h.get("cache-control"), "no-store")
                        self.assertNotIn("set-cookie", h)
                        if method != "HEAD":
                            self.assertIn(b"single sign-on", got)
                        self.assertNotIn(b"the page", got)
                        self.assertNotIn(b"the bundle", got)

    def test_a_user_named_without_the_secret_is_refused(self):
        """Every account on this host connects from 127.0.0.1: without the proxy's
        secret, the user it names is no one."""
        with redirect_stderr(io.StringIO()):
            status, _, body = self.req("GET", "/api/health", SIGNED, secret=False)
            self.assertEqual(status, 401)
            self.assertIn(b"single sign-on", body)
            self.assertEqual(self.refused_ws(SIGNED, secret=False), 403)

    def test_its_reader_reads_the_page_its_assets_the_api_and_metrics(self):
        for path, want in (("/", b"the page"), ("/assets/app.js", b"the bundle"),
                           ("/api/health", b'"ok"'), ("/metrics", b"rc_dashboard_")):
            with self.subTest(path=path):
                status, _, body = self.req("GET", path, SIGNED)
                self.assertEqual(status, 200)
                self.assertIn(want, body)

    def test_the_websocket(self):
        with redirect_stderr(io.StringIO()):
            for headers, src in (([], "127.0.0.1"), (SIGNED, "127.0.0.2"),
                                 (SIGNED[:1], "127.0.0.1")):
                with self.subTest(headers=headers, src=src):
                    self.assertEqual(self.refused_ws(headers, src), 403)
        with self.ws(SIGNED) as w:
            self.assertEqual(json.loads(w.recv(timeout=20))["type"], "live")

    def test_a_forwarded_address_is_never_where_the_request_came_from(self):
        with redirect_stderr(io.StringIO()):
            for fwd in (("X-Forwarded-For", "127.0.0.1"), ("Forwarded", "for=127.0.0.1"),
                        ("X-Real-IP", "127.0.0.1")):
                with self.subTest(fwd=fwd):
                    self.assertEqual(self.req("GET", "/api/health", SIGNED + [fwd],
                                              "127.0.0.2")[0], 401)
        # the client's address Apache, or nginx, adds is no matter from the proxy
        self.assertEqual(self.req("GET", "/api/health",
                                  SIGNED + [("X-Forwarded-For", "203.0.113.9")])[0], 200)

    def test_groups(self):
        for groups in ([], [("X-Forwarded-Groups", "staff")],
                       [("X-Forwarded-Groups", "Dash-Readers")],
                       [("X-Forwarded-Groups", "staff"), ("X-Forwarded-Groups", "dash-readers")]):
            with self.subTest(groups=groups):
                status, h, body = self.req("GET", "/api/health", SIGNED[:1] + groups)
                self.assertEqual(status, 403)
                self.assertTrue(h["content-type"].startswith("text/plain"))
                self.assertIn(b"Signed in as alice", body)
                self.assertNotIn(b"dash-readers", body)
        for groups in ("dash-readers", "staff,dash-readers", "staff dash-readers"):
            with self.subTest(groups=groups):
                self.assertEqual(self.req("GET", "/api/health", SIGNED[:1] + [
                    ("X-Forwarded-Groups", groups)])[0], 200)

    def test_an_empty_oversized_or_doubled_name(self):
        groups = SIGNED[1:]
        for user in ("", " ", "x" * (auth.MAX_PROXY_USER + 1)):
            with self.subTest(user=user[:10]):
                self.assertEqual(self.req("GET", "/api/health", [("X-Forwarded-User", user)]
                                          + groups)[0], 401)
        self.assertEqual(self.req("GET", "/api/health", [
            ("X-Forwarded-User", "alice"), ("X-Forwarded-User", "mallory")] + groups)[0], 401)
        self.assertEqual(self.req("GET", "/api/health", [
            ("X-Forwarded-User", "x" * auth.MAX_PROXY_USER)] + groups)[0], 200)

    def test_header_names_match_in_any_case(self):
        hs = [("X-FORWARDED-USER", "alice"), ("x-forwarded-groups", "dash-readers")]
        self.assertEqual(self.req("GET", "/api/health", hs)[0], 200)

    def test_the_page_is_told_who_signed_in_and_where_sign_out_goes(self):
        status, _, body = self.req("GET", "/api/config", SIGNED)
        self.assertEqual(status, 200)
        self.assertEqual(auth_leaf(body), {
            "_value": {"method": "proxy", "user": "alice", "logout_url": "/oauth2/sign_out"},
            "_origin": "request"})
        self.assertEqual(json.loads(body)["server"]["auth"]["proxy_header"]["_value"],
                         "X-Forwarded-User")

    def test_the_host_is_checked(self):
        # a page that points its own name at this host could send the header itself
        status, _, body = self.req("GET", "/api/health", SIGNED, host="evil.example:%d" % self.port)
        self.assertEqual(status, 403)
        self.assertIn("signs readers in through a proxy", json.loads(body)["detail"])


class SignOnAndPassword(Served):
    """The password beside the proxy, which is trusted at 127.0.0.2 (and a network
    no test sends from): a request from 127.0.0.1 is one the proxy did not send."""

    PASSWORD = "Test-pw:1{x}%"
    STORED = auth.hash_password(PASSWORD, **FAST)
    AUTH = {"proxy_header": "Remote-User", "proxy_trusted": ["127.0.0.2", "203.0.113.0/24"],
            "password_hash": STORED}
    USER = [("Remote-User", "alice")]

    def basic(self):
        return [("Authorization",
                 "Basic " + base64.b64encode(("rc:" + self.PASSWORD).encode()).decode())]

    def sign_in(self, headers=(), src="127.0.0.1"):
        body = urllib.parse.urlencode({"password": self.PASSWORD, "next": "/"}).encode()
        return self.req("POST", "/login", [("Content-Type", "application/x-www-form-urlencoded")]
                        + list(headers), src, body)

    def test_the_proxys_reader_goes_in(self):
        status, _, body = self.req("GET", "/api/config", self.USER, "127.0.0.2")
        self.assertEqual(status, 200)
        self.assertEqual(auth_leaf(body)["_value"],
                         {"method": "proxy", "user": "alice", "logout_url": None})

    def test_a_request_the_proxy_did_not_send_falls_back_to_the_password(self):
        with redirect_stderr(io.StringIO()):
            status, h, body = self.req("GET", "/?tab=risk", self.USER)
            self.assertEqual(status, 401)
            self.assertIn(b'type="password" name="password"', body)
            status, h, body = self.req("GET", "/api/health", self.USER)
            self.assertEqual(status, 401)
            self.assertIn("password", json.loads(body)["detail"])
            # an address a forwarded header gives is not where the request came from
            for fwd in ("127.0.0.2", "203.0.113.5"):
                with self.subTest(fwd=fwd):
                    self.assertEqual(self.req("GET", "/api/health", self.USER + [
                        ("X-Forwarded-For", fwd)])[0], 401)
        # the password opens it, as it always did, and the page is told so
        status, _, body = self.req("GET", "/api/config", self.USER + self.basic())
        self.assertEqual(status, 200)
        self.assertEqual(auth_leaf(body)["_value"],
                         {"method": "password", "user": None, "logout_url": None})
        status, h, _ = self.sign_in()
        self.assertEqual(status, 303)
        jar = [("Cookie", h["set-cookie"].split(";")[0])]
        self.assertEqual(self.req("GET", "/", jar)[0], 200)

    def test_the_websocket(self):
        with redirect_stderr(io.StringIO()):
            self.assertEqual(self.refused_ws(self.USER), 403)
        with self.ws(self.USER, "127.0.0.2") as w:
            self.assertEqual(json.loads(w.recv(timeout=20))["type"], "live")

    def test_signed_in_by_the_proxy_the_login_url_goes_on(self):
        status, h, _ = self.req("GET", "/login?next=/%3Ftab%3Drisk", self.USER, "127.0.0.2")
        self.assertEqual((status, h["location"]), (303, "/?tab=risk"))

    def test_a_cookie_is_secure_behind_a_tunnel_as_it_was(self):
        # served without uvicorn's own forwarded-header reading (SERVER_OPTIONS), the
        # app reads X-Forwarded-Proto itself, from where uvicorn did (auth.Peer)
        status, h, _ = self.sign_in([("X-Forwarded-Proto", "https")])
        self.assertEqual(status, 303)
        self.assertIn("Secure", [a.strip() for a in h["set-cookie"].split(";")])
        status, h, _ = self.sign_in([("X-Forwarded-Proto", "https")], "127.0.0.2")
        self.assertEqual(status, 303)
        self.assertNotIn("Secure", [a.strip() for a in h["set-cookie"].split(";")])

    def test_the_host_is_checked_with_the_password_too(self):
        for headers, src in ((self.USER, "127.0.0.2"), (self.basic(), "127.0.0.1")):
            with self.subTest(src=src):
                status, _, body = self.req("GET", "/api/health", headers, src,
                                           host="evil.example")
                self.assertEqual(status, 403)
                self.assertIn("signs readers in through a proxy", json.loads(body)["detail"])


if __name__ == "__main__":
    unittest.main()
