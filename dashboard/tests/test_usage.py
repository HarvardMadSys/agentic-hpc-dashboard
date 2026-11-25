"""Which tabs readers open, counted on this host (rc_dashboard/usage.py): what a post
must be, how the counts are kept a day at a time and for how long, `rc-dashboard usage`,
and POST /api/usage on the real app -- a 204, nothing kept with ui.count_views off, and
behind the password like every route.

    cd dashboard && .venv/bin/python -m unittest tests.test_usage -q
"""
import base64
import datetime
import http.client
import io
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
import warnings
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import uvicorn                                       # noqa: E402

from rc_dashboard import __main__ as cli             # noqa: E402
from rc_dashboard import app as appmod               # noqa: E402
from rc_dashboard import auth                        # noqa: E402
from rc_dashboard import config as configmod         # noqa: E402
from rc_dashboard import hours, snapshots            # noqa: E402
from rc_dashboard import usage                       # noqa: E402

DAY = 86400.0
NOW = datetime.datetime(2026, 11, 20, 12, tzinfo=datetime.timezone.utc).timestamp()


class Parse(unittest.TestCase):
    def test_counts_of_tabs(self):
        self.assertEqual(usage.parse({"views": {"tab:live": 2, "tab:risk": 1000}}),
                         {"tab:live": 2, "tab:risk": 1000})

    def test_a_view_that_is_no_tab_of_the_pages_is_dropped(self):
        """A key a client wrote -- a name among them -- is never kept: only counts of
        the page's own tabs are."""
        self.assertEqual(usage.parse({"views": {"tab:live": 1, "tab:alice": 4, "live": 1,
                                                "tab:Live": 1, "tab:../x": 1, "x" * 40: 1}}),
                         {"tab:live": 1})
        self.assertEqual(usage.parse({"views": {"tab:dave": 1}}), {})

    def test_the_tabs_are_the_pages(self):
        """usage.TABS is the page's TABS (App.tsx), but Ask's, which is no tab one can
        open: a tab the page gains is counted only once it is listed here too."""
        app = (Path(__file__).resolve().parents[1] / "web" / "src" / "App.tsx").read_text()
        block = app[app.index("const TABS"):]
        block = block[:block.index("];")]
        page = set(re.findall(r"\{ k: '([a-z0-9_-]+)'", block)) - {"chat"}
        self.assertTrue(page)
        self.assertLessEqual(page, usage.TABS)
        self.assertLessEqual(usage.TABS - page, {"summary"})   # the Summary tab, its own PR

    def test_anything_else_is_refused_and_never_repeated(self):
        many = {"views": {"tab:t%d" % i: 1 for i in range(usage.MAX_VIEWS + 1)}}
        for body, words in ((None, "expected"), ([], "expected"), ({}, "expected"),
                            ({"views": {}}, "expected"), ({"views": []}, "expected"),
                            ({"views": "dave"}, "expected"),
                            (many, "at most"),
                            ({"views": {"tab:live": "dave"}}, "whole number"),
                            ({"views": {"tab:live": 0}}, "whole number"),
                            ({"views": {"tab:live": -1}}, "whole number"),
                            ({"views": {"tab:live": 1001}}, "whole number"),
                            ({"views": {"tab:live": 1.5}}, "whole number"),
                            ({"views": {"tab:live": True}}, "whole number"),
                            ({"views": {"tab:live": "2"}}, "whole number")):
            with self.subTest(body=body if body is not many else "too many"):
                with self.assertRaises(usage.UsageError) as cm:
                    usage.parse(body)
                self.assertIn(words, str(cm.exception))
                self.assertNotIn("dave", str(cm.exception))


class Store(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rc-usage-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.dir = os.path.join(self.tmp, "usage")
        self.store = usage.Store(self.dir)

    def test_a_file_holding_what_is_no_tab_is_read_without_it(self):
        os.makedirs(self.dir)
        with open(os.path.join(self.dir, "2026-11-20.json"), "w") as fh:
            json.dump({"tab:live": 2, "tab:alice": 3, "tab:risk": "x"}, fh)
        self.assertEqual(self.store.totals(1, NOW), {"tab:live": (2, "2026-11-20")})

    def test_a_count_a_day_and_nothing_else(self):
        self.store.add({"tab:live": 2, "tab:risk": 1}, NOW)
        self.store.add({"tab:live": 3}, NOW + 60)
        self.store.add({"tab:live": 1}, NOW + DAY)
        self.assertEqual(sorted(os.listdir(self.dir)), ["2026-11-20.json", "2026-11-21.json"])
        with open(os.path.join(self.dir, "2026-11-20.json")) as fh:
            self.assertEqual(json.load(fh), {"tab:live": 5, "tab:risk": 1})
        self.assertEqual(oct(os.stat(self.dir).st_mode & 0o777), "0o700")
        self.assertEqual(self.store.totals(30, NOW + DAY),
                         {"tab:live": (6, "2026-11-21"), "tab:risk": (1, "2026-11-20")})
        self.assertEqual(self.store.totals(1, NOW + DAY), {"tab:live": (1, "2026-11-21")})

    def test_days_past_keeping_are_removed(self):
        old = NOW - (usage.KEEP_DAYS + 1) * DAY
        self.store.add({"tab:live": 1}, old)
        self.store.add({"tab:live": 1}, NOW - usage.KEEP_DAYS * DAY)
        self.store.add({"tab:live": 1}, NOW)
        days = sorted(os.listdir(self.dir))
        self.assertEqual(len(days), 2)
        self.assertNotIn(datetime.datetime.fromtimestamp(old, datetime.timezone.utc)
                         .strftime("%Y-%m-%d.json"), days)

    def test_a_day_that_is_not_counts_starts_again(self):
        os.makedirs(self.dir)
        with open(os.path.join(self.dir, "2026-11-20.json"), "w") as fh:
            fh.write("{not json")
        err = io.StringIO()
        with redirect_stderr(err):
            self.store.add({"tab:live": 1}, NOW)
        self.assertIn("is not counts", err.getvalue())
        self.assertEqual(self.store.totals(1, NOW), {"tab:live": (1, "2026-11-20")})

    def test_no_counts_yet(self):
        self.assertEqual(self.store.totals(30, NOW), {})


class Report(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rc-usage-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        tree = configmod.defaults()
        tree["cache"]["dir"] = self.tmp
        self.cfg = configmod.Config(tree, {}, None)

    def run_report(self, days=30):
        out = io.StringIO()
        self.assertEqual(usage.report(self.cfg, days, out, NOW), 0)
        return out.getvalue()

    def test_most_opened_first_and_what_is_not_kept(self):
        s = usage.Store(usage.folder(self.cfg))
        s.add({"tab:live": 3, "tab:feeds": 1}, NOW - DAY)
        s.add({"tab:summary": 9}, NOW)
        out = self.run_report()
        lines = [ln.split() for ln in out.splitlines()[1:-1]]
        self.assertEqual([ln[:2] for ln in lines], [["summary", "9"], ["live", "3"], ["feeds", "1"]])
        self.assertIn("last opened 2026-11-19", out)
        self.assertIn("no user, window or filter", out)

    def test_nothing_counted_says_where_it_looked(self):
        self.assertIn("no tab opened in the last 30 days", self.run_report())
        self.cfg.set("ui.count_views", False, "test")
        self.assertIn("counting is off", self.run_report())

    def test_the_command(self):
        usage.Store(usage.folder(self.cfg)).add({"tab:live": 2})
        path = os.path.join(self.tmp, "c.json")
        with open(path, "w") as fh:
            json.dump({"cache": {"dir": self.tmp}}, fh)
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(cli.main(["usage", "--config", path, "--days", "7"]), 0)
        self.assertIn("in the last 7 days", out.getvalue())
        self.assertRegex(out.getvalue(), r"live\s+2")
        with redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(["usage", "--config", path, "--days", "0"]), 2)


class Settings(unittest.TestCase):
    def test_on_by_default_and_no_fingerprint_moves(self):
        self.assertIs(configmod.defaults()["ui"]["count_views"], True)
        self.assertEqual(configmod.ENV_MAP["ui.count_views"][0], "RC_DASH_COUNT_VIEWS")
        self.assertIn("ui", hours.CONFIG_IGNORED)
        a = configmod.Config(configmod.defaults(), {}, None)
        tree = configmod.defaults()
        tree["ui"]["count_views"] = False
        b = configmod.Config(tree, {}, None)
        for fp in (snapshots.fingerprint, hours.fingerprint):
            with self.subTest(fp=fp.__module__):
                self.assertEqual(fp("code", a, ["/r"]), fp("code", b, ["/r"]))


class Live(unittest.TestCase):
    """POST /api/usage on the real app, over an empty feed, on a free port."""

    PASSWORD = None
    COUNT = True

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="rc-usage-app-")
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
        tree["ui"]["count_views"] = cls.COUNT
        if cls.PASSWORD:
            tree["server"]["auth"]["password_hash"] = auth.hash_password(cls.PASSWORD, n=16, r=1, p=1)
        cls.cfg = configmod.Config(tree, {}, None)
        with warnings.catch_warnings(), redirect_stderr(io.StringIO()):
            warnings.simplefilter("ignore", DeprecationWarning)    # on_event
            cls.app = appmod.create_app(cls.cfg)
        if cls.app.state.auth is not None:
            cls.app.state.auth.fail_delay_s = 0
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

    def post(self, body, ctype="application/json", password=None):
        h = {"Content-Type": ctype} if ctype else {}
        if password:
            h["Authorization"] = "Basic " + base64.b64encode(
                ("x:" + password).encode()).decode()
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            c.request("POST", "/api/usage", body=body if isinstance(body, bytes)
                      else json.dumps(body).encode(), headers=h)
            r = c.getresponse()
            return r.status, r.read()
        finally:
            c.close()

    def kept(self):
        return usage.Store(usage.folder(self.cfg)).totals(2)


class CountsKept(Live):
    def test_a_post_is_counted(self):
        self.assertEqual(self.post({"views": {"tab:live": 2}})[0], 204)
        self.assertEqual(self.post({"views": {"tab:live": 1, "tab:summary": 1}})[0], 204)
        got = self.kept()
        self.assertEqual((got["tab:live"][0], got["tab:summary"][0]), (3, 1))

    def test_a_name_is_never_kept_or_repeated(self):
        status, text = self.post({"views": {"tab:dave": 3}})
        self.assertEqual((status, text), (204, b""))
        self.assertNotIn("tab:dave", self.kept())
        status, text = self.post({"views": {"tab:live": "dave"}})
        self.assertEqual(status, 400)
        self.assertNotIn(b"dave", text)

    def test_what_is_not_counts_is_refused(self):
        for body, status in (({"views": {"tab:live": True}}, 400), (b"{not json", 400),
                             ({"views": {}}, 400), (b"x" * (usage.MAX_BODY + 1), 413)):
            with self.subTest(body=str(body)[:40]):
                status_, text = self.post(body)
                self.assertEqual(status_, status, text)
        # a form or plain text is never taken (guard.JsonOnly), so another site's page
        # cannot post here without the browser asking first
        self.assertEqual(self.post({"views": {"tab:live": 1}}, ctype="text/plain")[0], 415)


class CountingOff(Live):
    COUNT = False

    def test_taken_and_not_kept(self):
        self.assertEqual(self.post({"views": {"tab:live": 2}})[0], 204)
        self.assertEqual(self.kept(), {})
        self.assertFalse(os.path.exists(usage.folder(self.cfg)))


class BehindThePassword(Live):
    PASSWORD = "a-password-for-the-tests"

    def test_like_every_route(self):
        self.assertEqual(self.post({"views": {"tab:live": 1}})[0], 401)
        self.assertEqual(self.kept(), {})
        self.assertEqual(self.post({"views": {"tab:live": 1}}, password=self.PASSWORD)[0], 204)
        self.assertEqual(self.kept()["tab:live"][0], 1)


if __name__ == "__main__":
    unittest.main()
