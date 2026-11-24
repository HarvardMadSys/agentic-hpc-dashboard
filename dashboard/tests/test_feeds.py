"""Where the feeds are looked for (rc_dashboard/feeds.py): every candidate probed
-- found, empty, missing, denied --, glob roots matched again as folders appear,
a folder reached twice read once, and `check-feeds` naming every place it looked
without ever stopping on a folder it may not read.

    cd dashboard && .venv/bin/python -m unittest tests.test_feeds -q
"""
import errno
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rc_dashboard import __main__ as cli            # noqa: E402
from rc_dashboard import config as configmod        # noqa: E402
from rc_dashboard import feeds as feedmod           # noqa: E402
from rc_dashboard import history                    # noqa: E402

DAY = "2026-11-20"


class Cfg:
    def __init__(self, d):
        self.d = d

    def get(self, k, default=None):
        return self.d.get(k, default)

    def origin(self, k):
        return "test"


def day_file(root, host="h1", day=DAY, stream="exits"):
    d = os.path.join(root, day)
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, "%s.%s.jsonl" % (host, stream))
    with open(p, "w") as fh:
        fh.write(json.dumps({"ts": "%s 10:00:00" % day, "ts_epoch": 1.0, "host": host,
                             "event": "exit"}) + "\n")
    return p


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rc-feeds-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def path(self, *parts):
        return os.path.join(self.tmp, *parts)


class Probe(Case):
    def test_found_empty_missing(self):
        day_file(self.path("full"))
        os.makedirs(self.path("empty"))
        self.assertEqual(feedmod.probe(self.path("full"))[:2], ("found", None))
        self.assertEqual(len(feedmod.probe(self.path("full"))[2]), 1)
        self.assertEqual(feedmod.probe(self.path("empty"))[:2], ("empty", None))
        self.assertEqual(feedmod.probe(self.path("gone"))[0], "missing")
        open(self.path("file"), "w").close()
        self.assertEqual(feedmod.probe(self.path("file"))[:2], ("missing", "not a folder"))

    def test_a_folder_it_may_not_read_is_denied_with_its_owner_and_mode(self):
        d = self.path("locked")
        os.makedirs(d)
        real_access = os.access
        with mock.patch.object(feedmod.os, "access",
                               lambda p, m: False if p == d else real_access(p, m)):
            state, note, files = feedmod.probe(d)
        self.assertEqual((state, files), ("denied", []))
        self.assertRegex(note, r"^you: \S+; folder \S+:\S+ 0\d{3}$")

    def test_a_folder_below_one_it_may_not_enter_names_that_one(self):
        top = self.path("top")
        below = os.path.join(top, "ebpfm")
        os.makedirs(below)
        real_stat, real_access = os.stat, os.access

        def stat(p, *a, **kw):
            if p == below:
                raise PermissionError(errno.EACCES, "Permission denied", p)
            return real_stat(p, *a, **kw)
        with mock.patch.object(feedmod.os, "stat", stat), \
                mock.patch.object(feedmod.os, "access",
                                  lambda p, m: False if p == top else real_access(p, m)):
            state, note, _ = feedmod.probe(below)
        self.assertEqual(state, "denied")
        self.assertIn("folder %s " % top, note)

    @unittest.skipIf(os.geteuid() == 0, "root reads every folder")
    def test_for_real(self):
        d = self.path("locked")
        os.makedirs(d)
        os.chmod(d, 0o000)
        self.addCleanup(os.chmod, d, 0o700)
        self.assertEqual(feedmod.probe(d)[0], "denied")


class Candidates(Case):
    def test_a_pattern_is_a_candidate_per_folder_and_matched_again(self):
        day_file(self.path("mirror", "a"))
        pat = self.path("mirror", "*")
        got = feedmod.candidates([pat])
        self.assertEqual([(e["path"], e["state"]) for e in got],
                         [(self.path("mirror", "a"), "found")])
        self.assertEqual(got[0]["note"], "matches %s" % pat)
        os.makedirs(self.path("mirror", "b"))           # a node's folder made later
        got = feedmod.candidates([pat])
        self.assertEqual([e["path"] for e in got], [self.path("mirror", "a"), self.path("mirror", "b")])
        self.assertEqual(got[1]["state"], "empty")
        none = feedmod.candidates([self.path("nothing", "*")])
        self.assertEqual([(e["state"], e["note"]) for e in none],
                         [("missing", "a glob pattern that matches no folder")])

    def test_a_folder_named_with_pattern_characters_is_itself(self):
        day_file(self.path("logs[1]"))
        got = feedmod.candidates([self.path("logs[1]")])
        self.assertEqual([(e["path"], e["state"], e["note"]) for e in got],
                         [(self.path("logs[1]"), "found", None)])

    def test_a_folder_reached_twice_is_read_once(self):
        root = self.path("ebpfm")
        day_file(root)
        os.symlink(root, self.path("link"))
        cfg = Cfg({"feeds.ebpf.roots": [root, self.path("link"), root + "/"], "feeds.ebpf.days": 8})
        rep = feedmod.resolve_dated("ebpf", cfg)
        self.assertEqual(rep["resolved"], [root])
        self.assertEqual(rep["n_files"], 1)
        notes = [c["note"] for c in rep["candidates"][1:]]
        self.assertTrue(all("the same folder as %s, read once" % root in n for n in notes), notes)
        self.assertEqual(feedmod.resolved_roots(cfg, "ebpf"), [root])

    def test_the_report_carries_every_candidate_as_the_api_serves_it(self):
        day_file(self.path("a"))
        cfg = Cfg({"feeds.ebpf.roots": [self.path("gone"), self.path("a")], "feeds.ebpf.days": 8})
        rep = feedmod.resolve_dated("ebpf", cfg)
        self.assertEqual(rep["candidates"], [
            {"path": self.path("gone"), "state": "missing", "note": None},
            {"path": self.path("a"), "state": "found", "note": None}])
        self.assertEqual(rep["resolved"], [self.path("a")])
        self.assertEqual(rep["missing"], [self.path("gone")])

    def test_a_denied_root_is_not_read_and_the_notice_says_why(self):
        d = self.path("locked")
        os.makedirs(d)
        real_access = os.access
        cfg = Cfg({"feeds.ebpf.roots": [d], "feeds.ebpf.days": 8, "feeds.ebpf.required": True})
        with mock.patch.object(feedmod.os, "access",
                               lambda p, m: False if p == d else real_access(p, m)):
            reps = feedmod.resolve(cfg)
        rep = reps["ebpf"]
        self.assertEqual((rep["status"], rep["resolved"], rep["denied"]), ("missing", [], [d]))
        self.assertIn("permission denied in %s (you:" % d, rep["notice"])


class CheckFeeds(Case):
    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        env = {k: v for k, v in os.environ.items() if not k.startswith(("RC_DASH_", "EBPFM_"))}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(configmod, "HERE", self.tmp), \
                redirect_stdout(out), redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_one_line_per_candidate(self):
        day_file(self.path("a"))
        day_file(self.path("a"), host="h2")
        os.makedirs(self.path("b"))
        code, out, _ = self.run_cli(
            "check-feeds", "--ebpf-root", self.path("a"), "--ebpf-root", self.path("b"),
            "--ebpf-root", self.path("c"), "--node-root", self.path("n*"),
            "--config", self.cfg_file())
        self.assertEqual(code, 0)
        self.assertRegex(out, r"%s\s+found \(2 files\)" % self.path("a"))
        self.assertRegex(out, r"%s\s+empty" % self.path("b"))
        self.assertRegex(out, r"%s\s+missing\n" % self.path("c"))
        self.assertRegex(out, r"%s\s+missing; a glob pattern" % self.path("n*").replace("*", r"\*"))
        self.assertIn("ebpf: looked in 3 place(s)", out)

    def test_never_stops_on_a_root_it_may_not_read(self):
        day_file(self.path("a"))
        real = history.feedfile.dated_dirs

        def dated_dirs(root, days=0):
            raise PermissionError(errno.EACCES, "Permission denied", root)
        with mock.patch.object(history.feedfile, "dated_dirs", dated_dirs):
            code, out, err = self.run_cli("check-feeds", "--ebpf-root", self.path("a"),
                                          "--config", self.cfg_file())
        self.assertIn("cannot list %s (Permission denied)" % self.path("a"), out)
        self.assertIsNotNone(real)

    def cfg_file(self):
        p = self.path("c.json")
        with open(p, "w") as fh:
            json.dump({"cache": {"dir": self.path("cache")}}, fh)
        return p


class PrintConfig(Case):
    def test_existing_lists_a_patterns_matches(self):
        os.makedirs(self.path("m", "a"))
        os.makedirs(self.path("m", "b"))
        cfg = configmod.Config({"feeds": {"ebpf": {"roots": [self.path("m", "*")]}}}, {}, None)
        tree = cfg.as_dict(with_origins=True)
        self.assertEqual(tree["feeds"]["ebpf"]["roots"]["_existing"],
                         [self.path("m", "a"), self.path("m", "b")])


class Tail(Case):
    def test_the_tail_follows_a_folder_a_pattern_matches_later(self):
        from rc_dashboard.aggregator import Aggregator
        day_file(self.path("mirror", "a"))
        tree = configmod.defaults()
        tree["feeds"]["ebpf"]["roots"] = [self.path("mirror", "*")]
        tree["feeds"]["ebpf_node"]["roots"] = []
        tree["cache"]["dir"] = self.path("cache")
        tree["rollup"]["enabled"] = False
        agg = Aggregator(configmod.Config(tree, {}, None))
        self.addCleanup(agg.stop)
        self.assertEqual(agg.source.roots, [self.path("mirror", "a")])
        day_file(self.path("mirror", "b"), host="h2")
        agg.refresh_feeds()
        self.assertEqual(agg.source.roots, [self.path("mirror", "a"), self.path("mirror", "b")])
        self.assertEqual(len(agg.source.files()), 2)


if __name__ == "__main__":
    unittest.main()
