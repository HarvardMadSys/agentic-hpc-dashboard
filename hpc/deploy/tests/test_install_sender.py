"""install-sender.sh, run for real against a temporary folder: the guard that each source
node runs for the copy key's forced command, under its own name and the earlier stream's."""
import os
import shutil
import stat
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(os.path.dirname(HERE), "install-sender.sh")


class TestInstallSender(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="install-sender-")
        self.dir = os.path.join(self.tmp, "rc-fleet-sender")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def ship(self, sha, body):
        """What ship-sender.sh copies before it runs the script: DIR/.incoming-SHA."""
        os.makedirs(self.dir, exist_ok=True)
        with open(os.path.join(self.dir, ".incoming-" + sha), "w") as fh:
            fh.write(body)

    def install(self, sha, code=0):
        with open(SCRIPT) as fh:
            p = subprocess.run(["bash", "-s", "--", sha, self.dir], stdin=fh,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               universal_newlines=True)
        self.assertEqual(p.returncode, code, p.stderr)
        return p

    def current(self):
        with open(os.path.join(self.dir, "current", "rsync_guard.py")) as fh:
            return fh.read()

    def test_installs_and_points_current_at_it(self):
        self.ship("aaaaaaaaaaaa", "one")
        self.install("aaaaaaaaaaaa")
        self.assertEqual(os.readlink(os.path.join(self.dir, "current")), "aaaaaaaaaaaa")
        self.assertEqual(self.current(), "one")
        self.assertEqual(stat.S_IMODE(os.stat(self.dir).st_mode), 0o700)
        f = os.path.join(self.dir, "aaaaaaaaaaaa", "rsync_guard.py")
        self.assertEqual(stat.S_IMODE(os.stat(f).st_mode), 0o600)
        # a key from the earlier stream runs `current/fleet_stream.py send --root ROOT`
        old = os.path.join(self.dir, "current", "fleet_stream.py")
        self.assertEqual(os.readlink(old), "rsync_guard.py")
        with open(old) as fh:
            self.assertEqual(fh.read(), "one")
        self.assertEqual(sorted(os.listdir(self.dir)), ["aaaaaaaaaaaa", "current"])

    def test_keeps_the_one_before_and_removes_older_ones(self):
        """Senders that run have their code loaded; the one before stays for a rollback."""
        for sha, body in (("aaaaaaaaaaaa", "one"), ("bbbbbbbbbbbb", "two"), ("cccccccccccc", "three")):
            self.ship(sha, body)
            self.install(sha)
        self.assertEqual(self.current(), "three")
        self.assertEqual(sorted(os.listdir(self.dir)), ["bbbbbbbbbbbb", "cccccccccccc", "current"])

    def test_same_sha_again_keeps_the_installed_file(self):
        self.ship("aaaaaaaaaaaa", "one")
        self.install("aaaaaaaaaaaa")
        self.ship("aaaaaaaaaaaa", "changed")
        self.install("aaaaaaaaaaaa")
        self.assertEqual(self.current(), "one")
        self.assertFalse(os.path.exists(os.path.join(self.dir, ".incoming-aaaaaaaaaaaa")))

    def test_the_current_release_again_keeps_the_rollback(self):
        """In a home the nodes share, every node after the first finds the release
        current already; so does a deploy of the same commit. The release kept for a
        rollback must survive both."""
        for sha, body in (("aaaaaaaaaaaa", "one"), ("bbbbbbbbbbbb", "two")):
            self.ship(sha, body)
            self.install(sha)
        for _ in range(2):
            self.ship("bbbbbbbbbbbb", "two")
            p = self.install("bbbbbbbbbbbb")
            self.assertIn("already current", p.stdout)
        self.assertEqual(sorted(os.listdir(self.dir)), ["aaaaaaaaaaaa", "bbbbbbbbbbbb", "current"])
        self.assertEqual(self.current(), "two")

    def test_nothing_shipped_changes_nothing(self):
        self.ship("aaaaaaaaaaaa", "one")
        self.install("aaaaaaaaaaaa")
        p = self.install("bbbbbbbbbbbb", code=1)
        self.assertIn(".incoming-bbbbbbbbbbbb", p.stderr)
        self.assertEqual(os.readlink(os.path.join(self.dir, "current")), "aaaaaaaaaaaa")

    def test_an_empty_file_is_refused(self):
        self.ship("aaaaaaaaaaaa", "")
        self.install("aaaaaaaaaaaa", code=1)
        self.assertFalse(os.path.exists(os.path.join(self.dir, "current")))


if __name__ == "__main__":
    unittest.main()
