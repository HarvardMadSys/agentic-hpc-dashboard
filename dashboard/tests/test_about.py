"""What is running and where (rc_dashboard/about.py): the version and its commit
-- a build stamp, else the checkout's HEAD, else none --, the service's time zone,
and the host's name, as `--version` and /api/config's `_meta` give them.

    cd dashboard && .venv/bin/python -m unittest tests.test_about -q
"""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rc_dashboard import __version__                # noqa: E402
from rc_dashboard import about                      # noqa: E402
from rc_dashboard import config as configmod        # noqa: E402


class Version(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rc-about-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        about.version.cache_clear()
        self.addCleanup(about.version.cache_clear)

    def stamp(self, text):
        p = os.path.join(self.tmp, "BUILD.json")
        with open(p, "w") as fh:
            fh.write(text)
        return mock.patch.object(about, "STAMP", p)

    def test_a_build_stamp_comes_first(self):
        with self.stamp('{"commit": "0123456789ABCDEF0123456789abcdef01234567"}'):
            v = about.version()
        self.assertEqual(v, {"version": __version__, "commit": "0123456", "origin": "build"})
        about.version.cache_clear()
        with self.stamp('{"commit": "abc1234"}'):
            self.assertEqual(about.version_line(), "rc-dashboard %s (abc1234)" % __version__)

    def test_a_stamp_that_is_no_commit_is_passed_over(self):
        with self.stamp('{"commit": "; rm -rf /"}'), \
                mock.patch.object(about, "_git", return_value=None):
            self.assertEqual(about.version(),
                             {"version": __version__, "commit": None, "origin": "package"})
        self.assertEqual(about.version_line(), "rc-dashboard %s" % __version__)

    def test_then_the_checkouts_head(self):
        with self.stamp("not json"), mock.patch.object(about, "_git", return_value="feedbee"):
            self.assertEqual(about.version()["origin"], "git")

    def test_git_only_for_this_repository(self):
        """A release unpacked under a home that is a git repository is not that repository's."""
        class P:
            returncode, stdout = 0, "/somewhere/else\nabc1234\n"
        with mock.patch.object(about.subprocess, "run", return_value=P()):
            self.assertIsNone(about._git())
        P.stdout = "%s\nabc1234\n" % about.REPO
        with mock.patch.object(about.subprocess, "run", return_value=P()):
            self.assertEqual(about._git(), "abc1234")
        with mock.patch.object(about.subprocess, "run", side_effect=OSError("no git")):
            self.assertIsNone(about._git())


class Zone(unittest.TestCase):
    def setUp(self):
        about.zone_name.cache_clear()
        self.addCleanup(about.zone_name.cache_clear)

    def test_from_tz(self):
        self.assertEqual(about.zone_name("America/New_York"), "America/New_York")
        self.assertEqual(about.zone_name(":Europe/Paris"), "Europe/Paris")
        self.assertEqual(about.zone_name("/usr/share/zoneinfo/Asia/Tokyo"), "Asia/Tokyo")
        self.assertEqual(about.zone_name(""), "UTC")
        self.assertIsNone(about.zone_name("EST+5EDT,M3.2.0,M11.1.0"))
        self.assertIsNone(about.zone_name("../../etc/passwd"))

    def test_from_etc_localtime(self):
        tmp = tempfile.mkdtemp(prefix="rc-tz-")
        self.addCleanup(shutil.rmtree, tmp, True)
        link = os.path.join(tmp, "localtime")
        os.symlink("../usr/share/zoneinfo/America/Chicago", link)
        self.assertEqual(about.zone_name(None, link), "America/Chicago")
        self.assertIsNone(about.zone_name(None, os.path.join(tmp, "none")))

    def test_the_leaf(self):
        self.addCleanup(time.tzset)
        with mock.patch.dict(os.environ, {"TZ": "America/New_York"}):
            time.tzset()
            winter = about.zone(1767225600)         # 2026-01-01 00:00 UTC
            summer = about.zone(1782864000)         # 2026-07-01 00:00 UTC
        self.assertEqual(winter, {"name": "America/New_York", "abbrev": "EST",
                                  "utc_offset_s": -18000})
        self.assertEqual(summer["abbrev"], "EDT")
        self.assertEqual(summer["utc_offset_s"], -14400)


class Meta(unittest.TestCase):
    def test_config_meta_leaves(self):
        meta = configmod.Config(configmod.defaults(), {}, None).as_dict(with_origins=True)["_meta"]
        self.assertEqual(set(meta), {"config_file", "python", "version", "tz", "host"})
        for leaf in meta.values():
            self.assertEqual(set(leaf), {"_value", "_origin"})
        self.assertEqual(set(meta["version"]["_value"]), {"version", "commit"})
        self.assertEqual(meta["host"]["_value"], about.host())
        json.dumps(meta)


if __name__ == "__main__":
    unittest.main()
