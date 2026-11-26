"""check-local.sh's hourly run and its --base-ok start check, with systemctl, loginctl,
ss, tmux, curl, and ps replaced by stand-ins that log their calls, and the copy's state
(`pull.py status --json`) by RC_PULL_CMD."""
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
KIT = os.path.dirname(HERE)
SCRIPT = os.path.join(KIT, "check-local.sh")
sys.path.insert(0, HERE)

from test_check_local import path_without   # noqa: E402

STUBS = {
    "systemctl": r'''#!/bin/bash
echo "systemctl $*" >> "$STUB_LOG"
args=" $* "
case "$args" in
  *" is-active "*) unit=${@: -1}; [[ " $STUB_ACTIVE " == *" $unit "* ]] && exit 0; exit 3 ;;
  *" show "*ControlGroup*) echo "/u/${@: -1}"; exit 0 ;;
  *" start "*) exit "${STUB_START_RC:-0}" ;;
esac
exit 0
''',
    "loginctl": r'''#!/bin/bash
echo "loginctl $*" >> "$STUB_LOG"
case " $* " in
  *" show-user "*) echo "${STUB_LINGER:-yes}" ;;
esac
exit 0
''',
    "ss": r'''#!/bin/bash
[ -n "${STUB_SS_FILE:-}" ] && [ -f "$STUB_SS_FILE" ] && cat "$STUB_SS_FILE"
exit 0
''',
    "tmux": r'''#!/bin/bash
echo "tmux $*" >> "$STUB_LOG"
target= prev=
for a in "$@"; do [ "$prev" = -t ] && target=$a; prev=$a; done
case " $* " in
  *" list-panes "*)
    w=${target#*:}
    for kv in ${STUB_PANES:-}; do
      [ "${kv%%=*}" = "$w" ] && { tr ',' '\n' <<< "${kv#*=}"; exit 0; }
    done
    echo "can't find window $w" >&2; exit 1 ;;
esac
exit 0
''',
    "curl": r'''#!/bin/bash
echo "curl $*" >> "$STUB_LOG"
# STUB_SWAP=FILE:PID: the scope's process is replaced while the first probe runs.
if [ -n "${STUB_SWAP:-}" ]; then echo "${STUB_SWAP#*:}" > "${STUB_SWAP%%:*}"; fi
printf '%s' "${STUB_HTTP:-401}"
[ "${STUB_HTTP:-401}" = 000 ] && exit 28
exit 0
''',
    "ps": r'''#!/bin/bash
echo "ps $*" >> "$STUB_LOG"
echo "  ${STUB_AGE:-7200}"
''',
}

ACTIVE = "rc-tmux.service rc-dashboard.scope rc-fleet-copy.scope"
# A hash of the right shape: scrypt$n$r$p$salt$key, an 8-byte salt and a 16-byte key.
HASH = "scrypt$16384$8$1$c2FsdHNhbHQ=$aGFzaGhhc2hoYXNoaGFzaA=="
OURS = 'LISTEN 0 128 127.0.0.1:28090 0.0.0.0:* users:(("python",pid=4242,fd=7))'


class Rig(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="check-full-")
        os.chmod(self.tmp, 0o700)
        self.tmpd = os.path.join(self.tmp, "tmp-dash")
        self.base = os.path.join(self.tmpd, "data")
        self.home = os.path.join(self.tmp, "home")
        self.etc = os.path.join(self.home, "rc-dashboard", "etc")
        for d in (self.tmpd, self.base, os.path.join(self.base, "mirror"),
                  os.path.join(self.base, "mirror", ".state"),
                  os.path.join(self.tmpd, "cache"), os.path.join(self.tmpd, "logs"),
                  self.home, os.path.join(self.home, "rc-dashboard"), self.etc):
            os.mkdir(d, 0o700)
        with open(os.path.join(self.etc, "auth.env"), "w") as fh:
            fh.write("RC_DASH_PASSWORD_HASH=%s\n" % HASH)
        os.chmod(os.path.join(self.etc, "auth.env"), 0o600)
        with open(os.path.join(self.etc, "dashboard.config.json"), "w") as fh:
            json.dump({"server": {"port": 28090}}, fh)
        os.chmod(os.path.join(self.etc, "dashboard.config.json"), 0o600)
        self.bin = os.path.join(self.tmp, "bin")
        os.mkdir(self.bin, 0o700)
        for name, body in STUBS.items():
            p = os.path.join(self.bin, name)
            with open(p, "w") as fh:
                fh.write(body)
            os.chmod(p, 0o700)
        # The dashboard's scope, as /sys/fs/cgroup shows it: its one process is 4242.
        self.cgroot = os.path.join(self.tmp, "cgroup")
        scope = os.path.join(self.cgroot, "u", "rc-dashboard.scope")
        os.makedirs(scope)
        with open(os.path.join(scope, "cgroup.procs"), "w") as fh:
            fh.write("4242\n")
        self.log = os.path.join(self.tmp, "calls.log")
        self.ss_file = os.path.join(self.tmp, "ss.out")
        self.pull_file = os.path.join(self.tmp, "pull.json")
        self.write_pull(self.node("node01"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    def node(host, state="ok", age=30.0, error=None):
        """One row of `pull.py status --json`: age is the last good pass's, in seconds."""
        return {"host": host, "kind": "rsync", "state": state, "ok": state == "ok",
                "last_ok_age_s": age, "last_attempt_age_s": 30.0, "error": error}

    def write_pull(self, *rows):
        with open(self.pull_file, "w") as fh:
            json.dump({"stale_after_s": 3600, "nodes": list(rows)}, fh)

    def listeners(self, *lines):
        with open(self.ss_file, "w") as fh:
            fh.write("".join(l + "\n" for l in lines))

    def run_check(self, *args, active=ACTIVE, linger="yes", panes="copy=0 dashboard=0",
                  http="401", age="7200", start_rc="0", swap="", **extra):
        env = dict(os.environ, PATH=self.bin + ":" + os.environ["PATH"], HOME=self.home,
                   RC_DASH_ROOT=os.path.join(self.home, "rc-dashboard"), RC_DASH_RUN=self.tmpd,
                   RC_FLEET_ETC=self.etc, RC_CGROUP_ROOT=self.cgroot,
                   RC_CHECK_RETRY_S="0", STUB_LOG=self.log, STUB_ACTIVE=active,
                   STUB_LINGER=linger, STUB_SS_FILE=self.ss_file, STUB_PANES=panes,
                   STUB_HTTP=http, STUB_AGE=age, STUB_START_RC=start_rc, STUB_SWAP=swap,
                   RC_PULL_CMD="cat %s" % self.pull_file)
        env.update(extra)
        p = subprocess.run(["bash", SCRIPT] + list(args), env=env, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, universal_newlines=True)
        calls = ""
        if os.path.exists(self.log):
            with open(self.log) as fh:
                calls = fh.read()
        return p.returncode, p.stdout.strip(), calls


class TestFullRun(Rig):
    def test_ok(self):
        self.listeners(OURS)
        code, out, calls = self.run_check()
        self.assertEqual((code, out), (0, "OK"))
        self.assertNotIn("respawn-pane", calls)
        self.assertNotIn("stop rc-dashboard.scope", calls)

    def test_turns_linger_on_even_when_the_session_runs(self):
        """After a reboot the check's own login starts the session; linger must still go on."""
        self.listeners(OURS)
        code, out, calls = self.run_check(linger="no")
        self.assertEqual(code, 10)
        self.assertIn("LINGER", out)
        self.assertIn("enable-linger", calls)

    def test_missing_data_is_recreated(self):
        """In /tmp the top folder is ours under the sticky bit, so a missing data/
        (a reimage, or the user's own rm) is made again and the copy refills it."""
        shutil.rmtree(self.base)
        code, out, calls = self.run_check(active="")
        self.assertNotIn("UNSAFE", out)
        self.assertEqual(stat.S_IMODE(os.stat(self.base).st_mode), 0o700)
        self.assertIn("systemctl --user start rc-tmux.service", calls)

    def test_open_top_folder_is_unsafe(self):
        os.chmod(self.tmpd, 0o755)
        code, out, calls = self.run_check(active="", panes="copy=1 dashboard=1")
        self.assertEqual(code, 13)
        self.assertIn("UNSAFE", out)
        self.assertNotIn("systemctl --user start", calls)
        self.assertNotIn("respawn-pane", calls)

    def test_foreign_listener_while_dashboard_down(self):
        self.listeners("LISTEN 0 128 127.0.0.1:28090 0.0.0.0:*")
        code, out, calls = self.run_check(active="rc-tmux.service rc-fleet-copy.scope",
                                          panes="copy=0 dashboard=1")
        self.assertEqual(code, 14)
        self.assertIn("PORT foreign", out)
        self.assertNotIn("respawn-pane -t rc-dashboard:dashboard", calls)

    def test_second_listener_is_foreign(self):
        self.listeners(OURS, "LISTEN 0 128 [::1]:28090 [::]:*")
        code, out, calls = self.run_check()
        self.assertEqual(code, 14)
        self.assertIn("PORT foreign", out)

    def test_listener_outside_the_scope_is_foreign(self):
        self.listeners('LISTEN 0 128 127.0.0.1:28090 0.0.0.0:* users:(("python",pid=777,fd=7))')
        code, out, calls = self.run_check()
        self.assertEqual(code, 14)
        self.assertIn("PORT foreign", out)

    def test_a_copy_failing_for_over_an_hour_alerts(self):
        self.listeners(OURS)
        self.write_pull(self.node("node02", "FAILED", age=7300.0,
                                  error="rsync ended with exit 255: Host key verification failed."),
                        self.node("node01"))
        code, out, calls = self.run_check()
        self.assertEqual(code, 16)
        self.assertIn("COPY node02 FAILED for 2.0 h", out)
        self.assertIn("Host key verification failed", out)
        self.assertNotIn("node01", out)

    def test_a_copy_failing_for_minutes_is_left_to_retry(self):
        """A pass fails now and then (a node rebooting); the copy tries again each minute."""
        self.listeners(OURS)
        self.write_pull(self.node("node01", "FAILED", age=600.0, error="ssh: connect timed out"))
        code, out, calls = self.run_check()
        self.assertEqual((code, out), (0, "OK"))

    def test_a_node_never_copied_alerts(self):
        """No pass at all, or none good yet: the node's files have never reached the
        dashboard, which a node's first failing passes say as well as a missing state file."""
        self.listeners(OURS)
        self.write_pull(self.node("node01", "never", age=None, error="no pass recorded"),
                        self.node("node02", "FAILED", age=None, error="Permission denied (publickey)."))
        code, out, calls = self.run_check()
        self.assertEqual(code, 16)
        self.assertIn("COPY node01 never copied: no pass recorded", out)
        self.assertIn("COPY node02 never copied: Permission denied (publickey).", out)

    def test_a_stale_copy_alerts(self):
        """Passes that stopped altogether (the copy's window ended, or every pass hangs)."""
        self.listeners(OURS)
        self.write_pull(self.node("node01", "STALE", age=7300.0,
                                  error="no pass for 2h 1m: is the copy running?"))
        code, out, calls = self.run_check()
        self.assertEqual(code, 16)
        self.assertIn("COPY node01 STALE for 2.0 h: no pass for 2h 1m", out)

    def test_a_state_that_cannot_be_read_is_said(self):
        self.listeners(OURS)
        with open(self.pull_file, "w") as fh:
            fh.write("pull.py: FLEET_HOSTS is empty\n")
        code, out, calls = self.run_check()
        self.assertEqual(code, 16)
        self.assertIn("COPY its state could not be read", out)

    def test_the_copys_own_status_is_asked(self):
        """Without RC_PULL_CMD: the release's pull.py, with the deployment's fleet.env, and
        an hour as its STALE line, which the check's own reading of FAILED matches."""
        fleet = os.path.join(self.home, "rc-dashboard")
        for d in ("current", "deploy", "fleet"):
            fleet = os.path.join(fleet, d)
            os.mkdir(fleet, 0o700)      # the privacy audit flags anything wider
        argv = os.path.join(self.tmp, "argv.json")
        with open(os.path.join(fleet, "pull.py"), "w") as fh:
            fh.write("import json, sys\njson.dump(sys.argv[1:], open(%r, 'w'))\n"
                     "print(json.dumps({'nodes': []}))\n" % argv)
        os.chmod(os.path.join(fleet, "pull.py"), 0o600)
        self.listeners(OURS)
        code, out, calls = self.run_check(RC_PULL_CMD="")
        self.assertEqual((code, out), (0, "OK"))
        with open(argv) as fh:
            self.assertEqual(json.load(fh), ["--config", os.path.join(self.etc, "fleet.env"),
                                             "status", "--stale", "3600", "--json"])

    def test_no_copy_state_yet_is_not_read(self):
        """A deployment whose copy has not run once has no state folder: nothing to say."""
        self.listeners(OURS)
        os.rmdir(os.path.join(self.base, "mirror", ".state"))
        with open(self.pull_file, "w") as fh:
            fh.write("not json")
        code, out, calls = self.run_check()
        self.assertEqual((code, out), (0, "OK"))


class TestAuth(Rig):
    def test_a_quoted_hash_is_the_hash(self):
        """deploy/README.md puts a hash in single quotes, and systemd takes them off:
        so do keep-running.sh and this check."""
        self.listeners(OURS)
        with open(os.path.join(self.etc, "auth.env"), "w") as fh:
            fh.write("RC_DASH_PASSWORD_HASH='%s'\n" % HASH)
        code, out, _ = self.run_check()
        self.assertEqual((code, out), (0, "OK"))

    def test_a_hash_that_does_not_parse_is_a_finding(self):
        """The dashboard would refuse it at every start, while the old check passed it."""
        self.listeners(OURS)
        with open(os.path.join(self.etc, "auth.env"), "w") as fh:
            fh.write("RC_DASH_PASSWORD_HASH=scrypt$x$y\n")
        code, out, _ = self.run_check()
        self.assertEqual(code, 15)
        self.assertIn("does not parse", out)
        self.assertNotIn("scrypt$x$y", out, "the hash itself is never printed")

    def test_the_hash_file_is_read(self):
        self.listeners(OURS)
        os.unlink(os.path.join(self.etc, "auth.env"))
        h = os.path.join(self.etc, "password.hash")
        with open(h, "w") as fh:
            fh.write(HASH + "\n")
        os.chmod(h, 0o600)          # as password.sh leaves it; the audit flags anything wider
        code, out, _ = self.run_check()
        self.assertEqual((code, out), (0, "OK"))


class TestStatus(Rig):
    """The dashboard's own verdict, `rc-dashboard status --json`, stood in for by
    RC_STATUS_CMD."""

    def status(self, doc, rc=0):
        p = os.path.join(self.tmp, "status.json")
        with open(p, "w") as fh:
            fh.write(doc if isinstance(doc, str) else json.dumps(doc))
        return {"RC_STATUS_CMD": "cat %s; exit %d" % (p, rc)}

    def test_ok_is_quiet(self):
        self.listeners(OURS)
        code, out, _ = self.run_check(**self.status({"level": "ok", "why": [], "hosts": []}))
        self.assertEqual((code, out), (0, "OK"))

    def test_warn_and_crit_are_findings(self):
        self.listeners(OURS)
        doc = {"level": "crit", "why": ["node02: silent for 2 h", "node03: copy failed"],
               "hosts": []}
        code, out, _ = self.run_check(**self.status(doc, rc=2))
        self.assertEqual(code, 17)
        self.assertIn("STATUS crit: node02: silent for 2 h; node03: copy failed", out)
        code, out, _ = self.run_check(**self.status(dict(doc, level="warn"), rc=1))
        self.assertEqual(code, 17)
        self.assertIn("STATUS warn:", out)

    def test_no_verdict_is_said(self):
        self.listeners(OURS)
        code, out, _ = self.run_check(**self.status("Traceback (most recent call last)", rc=1))
        self.assertEqual(code, 17)
        self.assertIn("gave no verdict (exit 1)", out)

    def test_a_release_without_status_is_left_alone(self):
        self.listeners(OURS)
        env = {"RC_STATUS_CMD": "echo \"rc-dashboard: error: argument command: invalid choice: "
                                "'status'\" >&2; exit 2"}
        code, out, _ = self.run_check(**env)
        self.assertEqual((code, out), (0, "OK"))

    def test_an_unsafe_folder_skips_the_status(self):
        os.chmod(self.tmpd, 0o755)
        code, out, _ = self.run_check(**self.status({"level": "crit", "why": ["x"]}, rc=2))
        self.assertEqual(code, 13)
        self.assertNotIn("STATUS", out)


class TestWithoutSs(Rig):
    """A host without ss: the port's listeners come from /proc/net/tcp. Real ones here, a
    socket this test holds on a free port, and every command but ss on PATH."""

    def setUp(self):
        super().setUp()
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.addCleanup(self.sock.close)
        with open(os.path.join(self.etc, "dashboard.config.json"), "w") as fh:
            json.dump({"server": {"port": self.sock.getsockname()[1]}}, fh)
        stubs = os.path.join(self.tmp, "bin-no-ss")
        os.mkdir(stubs, 0o700)
        for name in STUBS:
            if name != "ss":
                os.symlink(os.path.join(self.bin, name), os.path.join(stubs, name))
        self.path = stubs + ":" + path_without(os.path.join(self.tmp, "path"), "ss")

    def test_the_dashboards_own_listener(self):
        with open(os.path.join(self.cgroot, "u", "rc-dashboard.scope", "cgroup.procs"), "w") as fh:
            fh.write("%d\n" % os.getpid())
        code, out, _ = self.run_check(PATH=self.path)
        self.assertEqual((code, out), (0, "OK"))

    def test_a_listener_outside_the_scope_is_foreign(self):
        code, out, _ = self.run_check(PATH=self.path)      # the scope's one process is 4242
        self.assertEqual(code, 14)
        self.assertIn("PORT foreign", out)

    def test_listeners_that_cannot_be_listed_are_reported(self):
        """No ss and no /proc/net/tcp: not OK, and the dashboard's window is left alone,
        as it is beside a foreign listener."""
        empty = os.path.join(self.tmp, "net")
        os.mkdir(empty, 0o700)
        code, out, calls = self.run_check(panes="copy=0 dashboard=1", PATH=self.path,
                                          RC_PROC_NET=empty)
        self.assertEqual(code, 14)
        self.assertIn("PORT cannot tell", out)
        self.assertNotIn("respawn-pane -t rc-dashboard:dashboard", calls)


class TestSession(Rig):
    def test_starts_the_session_when_it_is_down(self):
        """The tmux session runs both processes, and a running tmux is what keeps some
        sites' cleanup scripts from turning linger off."""
        code, out, calls = self.run_check(active="rc-dashboard.scope rc-fleet-copy.scope")
        self.assertEqual(code, 10)
        self.assertIn("RESTARTED rc-tmux.service", out)
        self.assertIn("systemctl --user start rc-tmux.service", calls)
        self.assertNotIn("tmux -L", calls)

    def test_failed_session_start_is_reported(self):
        code, out, calls = self.run_check(active="", start_rc="1")
        self.assertEqual(code, 15)
        self.assertIn("START FAILED rc-tmux.service", out)

    def test_respawns_a_window_whose_loop_ended(self):
        code, out, calls = self.run_check(active="rc-tmux.service rc-fleet-copy.scope",
                                          panes="copy=0 dashboard=1")
        self.assertEqual(code, 10)
        self.assertIn("RESTARTED dashboard", out)
        self.assertIn("tmux -L rc-dashboard respawn-pane -t rc-dashboard:dashboard.0", calls)
        self.assertNotIn("respawn-pane -t rc-dashboard:copy", calls)

    def test_split_window_is_not_missing(self):
        """Someone split the window (Ctrl-b %): its first pane is the loop's."""
        self.listeners(OURS)
        code, out, calls = self.run_check(panes="copy=0 dashboard=0,0")
        self.assertEqual((code, out), (0, "OK"))
        self.assertNotIn("new-window", calls)

    def test_dead_loop_in_a_split_window_is_respawned(self):
        self.listeners(OURS)
        code, out, calls = self.run_check(panes="copy=0 dashboard=1,0")
        self.assertEqual(code, 10)
        self.assertIn("respawn-pane -t rc-dashboard:dashboard.0", calls)
        self.assertNotIn("new-window", calls)

    def test_makes_a_missing_window(self):
        self.listeners(OURS)
        code, out, calls = self.run_check(panes="dashboard=0")
        self.assertEqual(code, 10)
        self.assertIn("RESTARTED copy", out)
        made = [l for l in calls.splitlines() if " new-window " in l]
        self.assertEqual(len(made), 1, calls)
        self.assertIn("tmux -L rc-dashboard new-window -d -t rc-dashboard: -n copy ", made[0])
        self.assertTrue(made[0].endswith("exec %s/keep-running.sh copy" % KIT), made[0])

    def test_frozen_dashboard_is_restarted(self):
        """Listening but not answering, as a soft memory cap (memory.high) can stall it."""
        self.listeners(OURS)
        code, out, calls = self.run_check(http="000", age="7200")
        self.assertEqual(code, 10)
        self.assertIn("FROZEN", out)
        self.assertEqual(calls.count("curl "), 2)
        self.assertIn("--noproxy *", calls)
        self.assertIn("systemctl --user stop rc-dashboard.scope", calls)

    def test_young_dashboard_is_left_to_start(self):
        """A dashboard in its first minutes may not answer yet."""
        self.listeners(OURS)
        code, out, calls = self.run_check(http="000", age="300")
        self.assertEqual((code, out), (0, "OK"))
        self.assertNotIn("stop rc-dashboard.scope", calls)

    def test_dashboard_still_loading_is_left_alone(self):
        """Not listening yet after 30 min, on a busy core: still loading, not frozen."""
        code, out, calls = self.run_check(http="000", age="1800")
        self.assertEqual((code, out), (0, "OK"))
        self.assertNotIn("stop rc-dashboard.scope", calls)

    def test_dashboard_that_never_listens_is_restarted(self):
        code, out, calls = self.run_check(http="000", age="7300")
        self.assertEqual(code, 10)
        self.assertIn("FROZEN", out)
        self.assertIn("systemctl --user stop rc-dashboard.scope", calls)

    def test_replaced_dashboard_is_not_stopped(self):
        """The process ended between the probes and its loop started a new one."""
        self.listeners(OURS)
        procs = os.path.join(self.cgroot, "u", "rc-dashboard.scope", "cgroup.procs")
        code, out, calls = self.run_check(http="000", age="7200", swap=procs + ":5555")
        self.assertEqual((code, out), (0, "OK"))
        self.assertNotIn("stop rc-dashboard.scope", calls)

    def test_answer_without_a_password_counts(self):
        self.listeners(OURS)
        code, out, calls = self.run_check(http="401")
        self.assertEqual((code, out), (0, "OK"))
        self.assertEqual(calls.count("curl "), 1)
        self.assertNotIn("netrc", calls)


class TestNotPrivate(Rig):
    """site.env's PRIVATE_TREE=false (a host only the team can log in to), as deploy.sh
    renders it into etc/kit.env: no UNSAFE for open folders and no AUDIT, but a folder
    that is not a real folder is still unsafe."""

    def setUp(self):
        super().setUp()
        p = os.path.join(self.etc, "kit.env")
        with open(p, "w") as fh:
            fh.write("PRIVATE_TREE=no\n")
        os.chmod(p, 0o600)

    def test_open_folders_and_files_are_not_reported(self):
        self.listeners(OURS)
        os.chmod(self.tmpd, 0o710)
        os.chmod(self.base, 0o750)
        p = os.path.join(self.base, "mirror", "shared.json")
        with open(p, "w") as fh:
            fh.write("{}")
        os.chmod(p, 0o640)
        code, out, _ = self.run_check()
        self.assertEqual((code, out), (0, "OK"))

    def test_a_symlinked_folder_is_still_unsafe(self):
        self.listeners(OURS)
        real = os.path.join(self.tmp, "elsewhere")
        os.mkdir(real, 0o700)
        shutil.rmtree(self.base)
        os.symlink(real, self.base)
        code, out, _ = self.run_check()
        self.assertEqual(code, 13)
        self.assertIn("UNSAFE", out)


class TestBaseOk(Rig):
    def test_private_base_ok(self):
        self.assertEqual(self.run_check("--base-ok")[0], 0)

    def test_symlink_base_refuses(self):
        real = os.path.join(self.tmpd, "elsewhere")
        os.mkdir(real, 0o700)
        shutil.rmtree(self.base)
        os.symlink(real, self.base)
        self.assertEqual(self.run_check("--base-ok")[0], 13)

    def test_open_top_folder_refuses(self):
        os.chmod(self.tmpd, 0o755)
        self.assertEqual(self.run_check("--base-ok")[0], 13)

    def test_missing_base_refuses(self):
        shutil.rmtree(self.base)
        self.assertEqual(self.run_check("--base-ok")[0], 13)


if __name__ == "__main__":
    unittest.main()
