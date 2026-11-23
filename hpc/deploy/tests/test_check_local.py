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


def path_without(folder, *names):
    """`folder`, made, holding a link to every command on PATH but `names`: a PATH for
    a host without them (ss sits in /usr/bin beside bash, so PATH cannot just skip it)."""
    os.mkdir(folder, 0o700)
    for top in os.environ["PATH"].split(os.pathsep):
        if not os.path.isdir(top):
            continue
        for name in os.listdir(top):
            p = os.path.join(top, name)
            if (name not in names and not os.path.lexists(os.path.join(folder, name))
                    and os.path.isfile(p) and os.access(p, os.X_OK)):
                os.symlink(p, os.path.join(folder, name))
    return folder


def proc_net_row(n, addr, port, state, inode):
    """One line of /proc/net/tcp or tcp6: `addr` in its hex form, `state` 0A for LISTEN."""
    return ("%4d: %s:%04X %s:0000 %s 00000000:00000000 00:00000000 00000000  1000        0 %d 1 "
            "0000000000000000 100 0 0 10 0" % (n, addr, port, "0" * len(addr), state, inode))


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
        p = subprocess.run(["bash", SCRIPT, flag], env=e, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, universal_newlines=True)
        self.err = p.stderr
        return p.returncode

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

    def test_auth_set_reads_values_as_systemd_does(self):
        """One pair of matching quotes, and the blanks around the value, come off."""
        p = os.path.join(self.etc, "auth.env")
        for line in ("RC_DASH_PASSWORD_HASH='scrypt$16384$8$1$c2FsdA$aGFzaA'",
                     'RC_DASH_PASSWORD_HASH="scrypt$16384$8$1$c2FsdA$aGFzaA"',
                     "RC_DASH_PASSWORD_HASH=  scrypt$16384$8$1$c2FsdA$aGFzaA  "):
            with open(p, "w") as fh:
                fh.write(line + "\n")
            self.assertEqual(self.run_check("--auth-set"), 0, line)
        for line in ("RC_DASH_PASSWORD_HASH='scrypt$16384$8$1$c2FsdA$aGFzaA\"",
                     "RC_DASH_PASSWORD_HASH=scrypt$x$y", "RC_DASH_PASSWORD_HASH=''"):
            with open(p, "w") as fh:
                fh.write(line + "\n")
            self.assertEqual(self.run_check("--auth-set"), 15, line)
        self.assertNotIn("scrypt$x$y", self.err)

    def test_auth_set_takes_the_hash_file(self):
        """etc/password.hash, which password.sh writes and the dashboard reads again
        when it changes, or the file auth.env names."""
        h = os.path.join(self.etc, "password.hash")
        with open(h, "w") as fh:
            fh.write("'scrypt$16384$8$1$c2FsdA$aGFzaA'\n")
        self.assertEqual(self.run_check("--auth-set"), 0)
        with open(h, "w") as fh:
            fh.write("garbage\n")
        self.assertEqual(self.run_check("--auth-set"), 15)
        self.assertIn(h, self.err)
        other = os.path.join(self.tmp, "elsewhere.hash")
        with open(other, "w") as fh:
            fh.write("scrypt$16384$8$1$c2FsdA$aGFzaA\n")
        with open(os.path.join(self.etc, "auth.env"), "w") as fh:
            fh.write("RC_DASH_PASSWORD_HASH_FILE=%s\n" % other)
        self.assertEqual(self.run_check("--auth-set"), 0)

    def test_auth_set_asks_the_dashboards_parser_where_the_venv_is(self):
        """With a release and its venv, rc_dashboard.auth.parse_hash judges the hash,
        and its reason is the one given."""
        os.makedirs(os.path.join(self.root, "venv", "bin"))
        os.makedirs(os.path.join(self.root, "current", "dashboard"))
        py = os.path.join(self.root, "venv", "bin", "python")
        with open(py, "w") as fh:
            fh.write('#!/bin/bash\nread -r h\n[ "$h" = good ] && exit 0\necho "a field of the hash '
                     'does not parse"\nexit 1\n')
        os.chmod(py, 0o700)
        with open(os.path.join(self.etc, "auth.env"), "w") as fh:
            fh.write("RC_DASH_PASSWORD_HASH=scrypt$16384$8$1$c2FsdA$aGFzaA\n")
        self.assertEqual(self.run_check("--auth-set"), 15)
        self.assertIn("a field of the hash does not parse", self.err)
        with open(os.path.join(self.etc, "auth.env"), "w") as fh:
            fh.write("RC_DASH_PASSWORD_HASH=good\n")
        self.assertEqual(self.run_check("--auth-set"), 0)

    def port_free_while_listening(self, **env):
        """--port-free while a real socket listens on the dashboard's port, then once it
        has closed."""
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        with open(os.path.join(self.etc, "dashboard.config.json"), "w") as fh:
            json.dump({"server": {"port": port}}, fh)
        try:
            s.listen(1)
            taken = self.run_check("--port-free", **env)
        finally:
            s.close()
        return taken, self.run_check("--port-free", **env)

    def stub_ss(self, body):
        """A PATH whose ss is a script running `body`."""
        stubs = os.path.join(self.tmp, "bin")
        os.mkdir(stubs, 0o700)
        with open(os.path.join(stubs, "ss"), "w") as fh:
            fh.write("#!/bin/bash\n" + body)
        os.chmod(os.path.join(stubs, "ss"), 0o700)
        return stubs + ":" + os.environ["PATH"]

    def test_port_free(self):
        """With whatever this host has, ss or not."""
        self.assertEqual(self.port_free_while_listening(), (14, 0))

    def test_port_free_without_ss(self):
        """Without ss the port's listeners come from /proc/net/tcp: a port in use never
        reads free because the tool that would have shown it is missing."""
        path = path_without(os.path.join(self.tmp, "path"), "ss")
        self.assertEqual(self.port_free_while_listening(PATH=path), (14, 0))

    def test_port_free_when_ss_fails(self):
        """An ss that cannot read the sockets prints nothing and fails: no answer, so
        /proc/net/tcp gives one."""
        path = self.stub_ss('echo "Cannot open netlink socket: Permission denied" >&2\nexit 1\n')
        self.assertEqual(self.port_free_while_listening(PATH=path), (14, 0))

    def test_port_free_takes_what_ss_says(self):
        with open(os.path.join(self.etc, "dashboard.config.json"), "w") as fh:
            json.dump({"server": {"port": 28090}}, fh)
        out = os.path.join(self.tmp, "ss.out")
        with open(out, "w") as fh:
            fh.write('LISTEN 0 128 127.0.0.1:28090 0.0.0.0:* users:(("python",pid=777,fd=7))\n')
        path = self.stub_ss('cat "$STUB_SS"\n')
        self.assertEqual(self.run_check("--port-free", PATH=path, STUB_SS=out), 14)
        open(out, "w").close()
        self.assertEqual(self.run_check("--port-free", PATH=path, STUB_SS=out), 0)

    def test_port_free_reads_tcp_and_tcp6(self):
        """Without ss: a listener on the port in tcp6 alone is a listener; a connection
        on the port, or a listener on another port, is not; a host without IPv6 has no
        tcp6."""
        with open(os.path.join(self.etc, "dashboard.config.json"), "w") as fh:
            json.dump({"server": {"port": 28090}}, fh)
        net = os.path.join(self.tmp, "net")
        os.mkdir(net)
        path = path_without(os.path.join(self.tmp, "path"), "ss")

        def table(name, *rows):
            with open(os.path.join(net, name), "w") as fh:
                fh.write("  sl  local_address rem_address   st tx_queue rx_queue tr tm->when "
                         "retrnsmt   uid  timeout inode\n" + "".join(r + "\n" for r in rows))

        def check():
            return self.run_check("--port-free", PATH=path, RC_PROC_NET=net)

        table("tcp", proc_net_row(0, "0100007F", 28090, "01", 9001),
              proc_net_row(1, "0100007F", 28091, "0A", 9002))
        table("tcp6", proc_net_row(0, "00000000000000000000000001000000", 28090, "0A", 9003))
        self.assertEqual(check(), 14)
        table("tcp6")
        self.assertEqual(check(), 0)
        os.unlink(os.path.join(net, "tcp6"))
        self.assertEqual(check(), 0)

    def test_port_that_cannot_be_checked_is_not_free(self):
        """No ss, and no /proc/net/tcp to read: who listens is unknown, which is no
        reason to start the dashboard."""
        empty = os.path.join(self.tmp, "net")
        os.mkdir(empty)
        path = path_without(os.path.join(self.tmp, "path"), "ss")
        self.assertEqual(self.run_check("--port-free", PATH=path, RC_PROC_NET=empty), 14)
        self.assertIn("PORT cannot tell", self.err)


if __name__ == "__main__":
    unittest.main()
