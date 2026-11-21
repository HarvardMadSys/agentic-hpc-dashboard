"""install-remote.sh, run for real against a temporary home and deployment folder, with
systemctl, loginctl, and the venv's python replaced by stand-ins that log their calls."""
import io
import os
import shutil
import stat
import subprocess
import tarfile
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(os.path.dirname(HERE), "install-remote.sh")
SHA = "0123456789ab"

STUB = """#!/bin/bash
echo "$(basename "$0") $*" >> "$STUB_LOG"
exit 0
"""


class Rig(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="install-remote-")
        self.home = os.path.join(self.tmp, "home")
        # The deployment's folder is named by deploy.sh, and is not in the home.
        self.r = os.path.join(self.tmp, "data", "rc-dashboard")
        self.bin = os.path.join(self.tmp, "bin")
        self.log = os.path.join(self.tmp, "calls.log")
        for d in (self.home, self.bin, os.path.join(self.r, "venv", "bin")):
            os.makedirs(d)
        for p in (os.path.join(self.bin, "systemctl"), os.path.join(self.bin, "loginctl"),
                  os.path.join(self.r, "venv", "bin", "python")):
            with open(p, "w") as fh:
                fh.write(STUB)
            os.chmod(p, 0o700)
        # The running session's windows: list-panes finds them unless STUB_WINDOWS=no.
        p = os.path.join(self.bin, "tmux")
        with open(p, "w") as fh:
            fh.write(STUB.replace("exit 0", '[ "${STUB_WINDOWS:-yes}" = yes ]'))
        os.chmod(p, 0o700)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def payload(self, sha=SHA, version="one", unit="[Unit]\n"):
        """What deploy.sh ships into DASH_ROOT/incoming/<sha>."""
        inc = os.path.join(self.r, "incoming", sha)
        os.makedirs(os.path.join(inc, "dist"))
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            for name, body in (("rc/dashboard/rc_dashboard/__init__.py", version),
                               ("rc/hpc/fleet/fleet_stream.py", version)):
                data = body.encode()
                info = tarfile.TarInfo(name)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        with open(os.path.join(inc, "release.tar"), "wb") as fh:
            fh.write(buf.getvalue())
        files = {"dist/index.html": version, "requirements.txt": "",
                 "stream.json": "{}", "dashboard.config.json": '{"v": "%s"}' % version}
        files["rc-tmux.service"] = unit
        for name, body in files.items():
            with open(os.path.join(inc, name), "w") as fh:
                fh.write(body)

    def install(self, sha=SHA, first="0", restart="0", windows="yes", root=None, code=0, py=None):
        env = dict(os.environ, HOME=self.home, PATH=self.bin + ":" + os.environ["PATH"],
                   STUB_LOG=self.log, STUB_WINDOWS=windows)
        args = [sha, first, restart] + ([] if root == "" else [root or self.r]) + ([py] if py else [])
        with open(SCRIPT) as fh:
            p = subprocess.run(["bash", "-s", "--"] + args, stdin=fh, env=env,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               universal_newlines=True)
        self.assertEqual(p.returncode, code, p.stderr)
        return p.stdout


class TestInstall(Rig):
    def test_needs_the_deployment_folder(self):
        """There is no default folder in the home."""
        self.payload()
        self.install(root="", code=2)
        self.assertFalse(os.path.exists(os.path.join(self.home, "rc-dashboard")))
        self.assertFalse(os.path.exists(os.path.join(self.r, "current")))

    def test_the_folder_is_private(self):
        self.payload()
        self.install()
        for d in (self.r, os.path.join(self.r, "releases"), os.path.join(self.r, "etc")):
            self.assertEqual(stat.S_IMODE(os.stat(d).st_mode), 0o700, d)

    def test_modes_are_left_alone_when_not_private(self):
        """PRIVATE_TREE=false: a deploy keeps the modes and ACLs the user set (an ACL that
        lets one more account read the copy, say). etc/, which holds the secrets, is still
        700."""
        self.payload()
        with open(os.path.join(self.r, "incoming", SHA, "stream.json"), "w") as fh:
            fh.write('{"private": false}')
        data = os.path.join(self.r, "data")
        os.mkdir(data)
        f = os.path.join(data, "shared.jsonl")
        open(f, "w").close()
        os.chmod(f, 0o640)
        os.chmod(data, 0o750)
        os.chmod(self.r, 0o710)
        self.install()
        self.assertEqual(stat.S_IMODE(os.stat(self.r).st_mode), 0o710)
        self.assertEqual(stat.S_IMODE(os.stat(data).st_mode), 0o750)
        self.assertEqual(stat.S_IMODE(os.stat(f).st_mode), 0o640)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.r, "etc")).st_mode), 0o700)

    def test_installs_release_and_settings(self):
        self.payload()
        self.install()
        self.assertEqual(os.readlink(os.path.join(self.r, "current")), "releases/" + SHA)
        rel = os.path.join(self.r, "releases", SHA)
        with open(os.path.join(rel, "dashboard", "rc_dashboard", "__init__.py")) as fh:
            self.assertEqual(fh.read(), "one")
        self.assertTrue(os.path.exists(os.path.join(rel, "dist", "index.html")))
        cfg = os.path.join(self.r, "etc", "dashboard.config.json")
        self.assertEqual(stat.S_IMODE(os.stat(cfg).st_mode), 0o600)
        self.assertFalse(os.path.exists(os.path.join(self.r, "incoming", SHA)))

    def test_same_release_again_keeps_the_running_folder(self):
        """A settings change ships the same sha again. A unit may be running from that
        release, so its folder must stay as it is; the new settings still install."""
        self.payload()
        self.install()
        rel = os.path.join(self.r, "releases", SHA)
        code = os.path.join(rel, "dashboard", "rc_dashboard", "__init__.py")
        # Held open, as a running unit holds its folder and code: a freed inode
        # cannot be handed straight back to a new folder while these are open.
        held = [os.open(rel, os.O_RDONLY), os.open(code, os.O_RDONLY)]
        try:
            self.payload(version="two")
            self.install()
            for fd, path in zip(held, (rel, code)):
                self.assertTrue(os.path.samestat(os.fstat(fd), os.stat(path)), path)
                self.assertGreater(os.fstat(fd).st_nlink, 0, path)
        finally:
            for fd in held:
                os.close(fd)
        with open(os.path.join(self.r, "etc", "dashboard.config.json")) as fh:
            self.assertEqual(fh.read(), '{"v": "two"}')
        self.assertFalse(os.path.exists(os.path.join(self.r, "incoming", SHA)))

    def test_incomplete_release_is_replaced(self):
        """A deploy cut off mid-extract leaves a folder without its marker; the next
        deploy of that sha installs it whole."""
        os.makedirs(os.path.join(self.r, "releases", SHA, "dashboard"))
        self.payload()
        self.install()
        code = os.path.join(self.r, "releases", SHA, "dashboard", "rc_dashboard", "__init__.py")
        with open(code) as fh:
            self.assertEqual(fh.read(), "one")

    def calls(self):
        with open(self.log) as fh:
            return fh.read()

    def test_only_the_tmux_unit_is_installed(self):
        self.payload()
        self.install()
        units = os.path.join(self.home, ".config", "systemd", "user")
        self.assertEqual(sorted(os.listdir(units)), ["rc-tmux.service"])
        self.assertIn("systemctl --user enable rc-tmux.service", self.calls())

    def test_the_venv_needs_python_3_11(self):
        """--first makes the venv with site.env's DASH_PYTHON, and refuses one older than
        the dashboard needs, before it touches the venv."""
        old = os.path.join(self.bin, "python3.9")
        with open(old, "w") as fh:
            fh.write('#!/bin/bash\necho "python3.9 $*" >> "$STUB_LOG"\nexit 1\n')
        os.chmod(old, 0o700)
        self.payload()
        p_before = os.path.join(self.r, "venv", "bin", "python")
        self.install(first="1", py=old, code=1)
        self.assertIn("python3.9 -c", self.calls())
        self.assertNotIn("python3.9 -m venv", self.calls())
        self.assertTrue(os.path.exists(p_before))
        self.assertFalse(os.path.exists(os.path.join(self.r, "current")))

    def test_a_new_venv_from_dash_python(self):
        new = os.path.join(self.bin, "python3.12")
        with open(new, "w") as fh:
            fh.write('#!/bin/bash\necho "python3.12 $*" >> "$STUB_LOG"\nexit 0\n')
        os.chmod(new, 0o700)
        self.payload()
        self.install(first="1", py=new)
        self.assertIn("python3.12 -m venv %s/venv" % self.r, self.calls())
        self.assertEqual(os.readlink(os.path.join(self.r, "current")), "releases/" + SHA)

    def python_stub(self, path, version):
        """A Python that says it is `version` when asked, makes a venv of itself as
        `-m venv` does, and logs every call."""
        with open(path, "w") as fh:
            fh.write('#!/bin/bash\necho "%s $*" >> "$STUB_LOG"\n'
                     '[ "$1" = -c ] && echo %s\n'
                     'if [ "$1" = -m ] && [ "$2" = venv ]; then mkdir -p "$3/bin"; '
                     'cp "$0" "$3/bin/python"; fi\nexit 0\n' % (os.path.basename(path), version))
        os.chmod(path, 0o700)

    def test_a_venv_of_another_python_is_made_again(self):
        """DASH_PYTHON moved to a newer Python: a deploy that is not the first one makes
        the venv again with it, keeps the old one beside it, and restarts the session,
        whose dashboard would otherwise start its worker processes from the new venv."""
        venv_py = os.path.join(self.r, "venv", "bin", "python")
        self.python_stub(venv_py, "3.12")
        new = os.path.join(self.bin, "python3.14")
        self.python_stub(new, "3.14")
        self.payload()
        out = self.install(py=new)
        self.assertIn("the venv is Python 3.12, DASH_PYTHON is 3.14: making it again", out)
        self.assertIn("python3.14 -m venv %s/venv" % self.r, self.calls())
        self.assertTrue(os.path.exists(os.path.join(self.r, "venv.old", "bin", "python")))
        self.assertIn("systemctl --user restart rc-tmux.service", self.calls())

    def test_a_venv_of_the_same_python_is_kept(self):
        venv_py = os.path.join(self.r, "venv", "bin", "python")
        self.python_stub(venv_py, "3.14")
        same = os.path.join(self.bin, "python3.14")
        self.python_stub(same, "3.14")
        self.payload()
        self.install(py=same)
        self.assertNotIn("-m venv", self.calls())
        self.assertFalse(os.path.exists(os.path.join(self.r, "venv.old")))
        self.assertNotIn("restart", self.calls())

    def test_a_venv_whose_python_does_not_answer_is_kept(self):
        """Its version unknown, the venv is not taken for another Python's: no venv is
        made, and nothing restarts."""
        new = os.path.join(self.bin, "python3.14")
        self.python_stub(new, "3.14")
        self.payload()
        self.install(py=new)
        self.assertNotIn("-m venv", self.calls())
        self.assertNotIn("restart", self.calls())
        self.assertFalse(os.path.exists(os.path.join(self.r, "venv.old")))

    def test_no_restart_without_the_flag(self):
        self.payload()
        self.install()
        calls = self.calls()
        self.assertIn("systemctl --user start rc-tmux.service", calls)
        self.assertNotIn("restart", calls)
        self.assertIn("enable-linger", calls)

    def test_session_without_its_windows_is_restarted(self):
        """A session without its two windows (a deploy that stopped half way, or a
        window someone closed): a plain start would do nothing."""
        self.payload()
        self.install(windows="no")
        self.assertIn("systemctl --user restart rc-tmux.service", self.calls())

    def test_a_unit_whose_kit_moved_restarts_the_session(self):
        """The session's windows run the kit's scripts by the path the unit named, through
        `current`. A release whose kit is in another folder (deploy/fleet became hpc/deploy)
        has no such path, so a window whose process ended could not start it again: a unit
        whose ExecStart changed restarts the session, with no --restart."""
        old = "[Service]\nExecStart=%s/current/deploy/fleet/tmux-session.sh\n" % self.r
        new = "[Service]\nExecStart=%s/current/hpc/deploy/tmux-session.sh\n" % self.r
        self.payload(unit=old)
        self.install()
        self.assertNotIn("restart", self.calls())
        self.payload(sha="ba9876543210", version="two", unit=new)
        out = self.install(sha="ba9876543210")
        self.assertIn("systemctl --user restart rc-tmux.service", self.calls())
        self.assertIn("ExecStart changed", out)

    def test_the_same_unit_again_keeps_the_session(self):
        unit = "[Service]\nExecStart=%s/current/hpc/deploy/tmux-session.sh\n" % self.r
        self.payload(unit=unit)
        self.install()
        self.payload(sha="ba9876543210", version="two", unit=unit)
        self.install(sha="ba9876543210")
        self.assertNotIn("restart", self.calls())

    def test_restart_flag_restarts_the_session(self):
        self.payload()
        self.install(restart="1")
        calls = self.calls()
        self.assertIn("systemctl --user restart rc-tmux.service", calls)
        self.assertNotIn("rc-dashboard.service", calls)


if __name__ == "__main__":
    unittest.main()
