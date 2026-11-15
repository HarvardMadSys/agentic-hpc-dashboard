"""check-local.sh's three start checks, run for real against temporary paths."""
import json
import os
import shutil
import socket
import stat
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(os.path.dirname(HERE), "check-local.sh")


class TestStartChecks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="check-local-")
        self.tmpd = os.path.join(self.tmp, "dash")
        self.root = os.path.join(self.tmp, "root")
        self.etc = os.path.join(self.tmp, "etc")
        os.mkdir(self.etc, 0o700)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_check(self, flag, **env):
        e = dict(os.environ, RC_DASH_ROOT=self.root, RC_DASH_RUN=self.tmpd, RC_FLEET_ETC=self.etc)
        for k, v in env.items():        # None takes a variable out
            if v is None:
                e.pop(k, None)
            else:
                e[k] = v
        return subprocess.run(["bash", SCRIPT, flag], env=e, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, universal_newlines=True).returncode

    def test_needs_the_deployment_folder(self):
        """There is no default folder, in /tmp or in the home."""
        for flag in ("--dirs-only", "--base-ok", "--auth-set", "--port-free"):
            self.assertEqual(self.run_check(flag, RC_DASH_ROOT=None), 2, flag)

    def test_dirs_only_makes_the_folders_in_the_deployment_folder(self):
        """Without a run folder of its own, data, cache, and logs go in RC_DASH_ROOT."""
        self.assertEqual(self.run_check("--dirs-only", RC_DASH_RUN=None), 0)
        for d in (self.root, os.path.join(self.root, "cache"), os.path.join(self.root, "logs"),
                  os.path.join(self.root, "data")):
            self.assertEqual(stat.S_IMODE(os.stat(d).st_mode), 0o700, d)
        self.assertFalse(os.path.exists(self.tmpd))

    def test_dirs_only_accepts_a_folder_another_check_made_at_the_same_time(self):
        """Both windows' loops run --dirs-only when the session starts. On a new folder,
        one of them loses the race to make cache/ and logs/; its mkdir fails although
        the folder is there, private, and this account's. That is not an unsafe
        folder, and the loop must not stop at its first start for it. Here mkdir makes
        the folder, then fails, as the loser's does."""
        stubs = os.path.join(self.tmp, "bin")
        os.mkdir(stubs, 0o700)
        with open(os.path.join(stubs, "mkdir"), "w") as fh:
            fh.write('#!/bin/bash\n/bin/mkdir "$@" && exit 1\n')
        os.chmod(os.path.join(stubs, "mkdir"), 0o700)
        self.assertEqual(self.run_check("--dirs-only", PATH=stubs + ":" + os.environ["PATH"]), 0)
        for d in (self.tmpd, os.path.join(self.tmpd, "cache"), os.path.join(self.tmpd, "logs"),
                  os.path.join(self.tmpd, "data")):
            self.assertEqual(stat.S_IMODE(os.stat(d).st_mode), 0o700, d)

    def test_dirs_only_creates_private_dirs(self):
        self.assertEqual(self.run_check("--dirs-only"), 0)
        for d in (self.tmpd, os.path.join(self.tmpd, "cache"), os.path.join(self.tmpd, "logs"),
                  os.path.join(self.tmpd, "data")):
            self.assertEqual(stat.S_IMODE(os.stat(d).st_mode), 0o700, d)

    def test_dirs_only_refuses_open_folder(self):
        os.mkdir(self.tmpd)
        os.chmod(self.tmpd, 0o755)
        self.assertEqual(self.run_check("--dirs-only"), 13)

    def test_dirs_only_refuses_symlink(self):
        real = os.path.join(self.tmp, "real")
        os.mkdir(real, 0o700)
        os.symlink(real, self.tmpd)
        self.assertEqual(self.run_check("--dirs-only"), 13)

    def not_private(self):
        """site.env's PRIVATE_TREE=false, as deploy.sh renders it into etc/stream.json."""
        with open(os.path.join(self.etc, "stream.json"), "w") as fh:
            json.dump({"private": False}, fh)

    def test_open_folders_pass_when_not_private(self):
        """PRIVATE_TREE=false (a host only the team can log in to): modes and ACLs are the user's
        business, so a folder that another account may read is no reason to stop."""
        self.not_private()
        for d in (self.tmpd, os.path.join(self.tmpd, "cache"), os.path.join(self.tmpd, "logs"),
                  os.path.join(self.tmpd, "data")):
            os.mkdir(d)
            os.chmod(d, 0o750)
        self.assertEqual(self.run_check("--dirs-only"), 0)
        self.assertEqual(self.run_check("--base-ok"), 0)

    def test_a_symlink_is_still_refused_when_not_private(self):
        self.not_private()
        real = os.path.join(self.tmp, "real")
        os.mkdir(real, 0o750)
        os.symlink(real, self.tmpd)
        self.assertEqual(self.run_check("--dirs-only"), 13)

    def test_auth_set(self):
        self.assertEqual(self.run_check("--auth-set"), 15)
        p = os.path.join(self.etc, "auth.env")
        with open(p, "w") as fh:
            fh.write("RC_DASH_PASSWORD_HASH=\n")
        self.assertEqual(self.run_check("--auth-set"), 15)
        with open(p, "w") as fh:
            fh.write("RC_DASH_PASSWORD_HASH=scrypt$16384$8$1$c2FsdA$aGFzaA\n")
        self.assertEqual(self.run_check("--auth-set"), 0)

    def test_port_free(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        with open(os.path.join(self.etc, "dashboard.config.json"), "w") as fh:
            json.dump({"server": {"port": port}}, fh)
        try:
            s.listen(1)
            self.assertEqual(self.run_check("--port-free"), 14)
        finally:
            s.close()
        self.assertEqual(self.run_check("--port-free"), 0)


if __name__ == "__main__":
    unittest.main()
