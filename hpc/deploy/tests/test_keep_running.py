"""keep-running.sh and tmux-session.sh, run for real with systemd-run, systemctl,
check-local.sh, and tmux replaced by stand-ins that log their calls. The stand-in
systemd-run runs the command after its `--`, as the real one does inside its scope."""
import fcntl
import os
import shutil
import signal
import stat
import subprocess
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
KIT = os.path.dirname(HERE)
SCRIPT = os.path.join(KIT, "keep-running.sh")
SESSION = os.path.join(KIT, "tmux-session.sh")
HASH = "scrypt$16384$8$1$c2FsdA$aGFzaA=="

STUBS = {
    "systemd-run": r'''#!/bin/bash
echo "systemd-run $*" >> "$STUB_LOG"
for a in "$@"; do case $a in --unit=*) echo $$ > "$STUB_SCOPES/${a#--unit=}.scope" ;; esac; done
while [ $# -gt 0 ] && [ "$1" != "--" ]; do shift; done
shift
exec "$@"
''',
    "systemctl": r'''#!/bin/bash
echo "systemctl $*" >> "$STUB_LOG"
unit=${@: -1}
f=$STUB_SCOPES/$unit
alive() { [ -f "$f" ] && kill -0 "$(cat "$f")" 2>/dev/null; }
case " $* " in
  *" is-active "*)
    [[ " ${STUB_ACTIVE:-} " == *" $unit "* ]] && exit 0
    alive && exit 0
    exit 3 ;;
  *" stop "*)
    if alive; then
      pid=$(cat "$f")
      kill -TERM "$pid"
      ( for _ in $(seq 10); do kill -0 "$pid" 2>/dev/null || exit 0; sleep 0.2; done
        kill -KILL "$pid" ) > /dev/null 2>&1 &
      [[ " $* " == *" --no-block "* ]] || wait
    fi ;;
esac
exit 0
''',
    "check-local.sh": r'''#!/bin/bash
echo "check $*" >> "$STUB_LOG"
var=STUB_CHECK_${1#--}
var=${var//-/_}
rc=${!var:-0}
[ "$rc" = 0 ] || echo "the check $1 says no" >&2
exit "$rc"
''',
    "tmux": r'''#!/bin/bash
for a in "$@"; do printf '%s\n' "$a"; done >> "$STUB_LOG"
''',
}

LOGS = {"stream": "rc-fleet-stream.log", "dashboard": "rc-dashboard.log"}
CHILD_WAITS = 'trap "echo got-term; exit 0" TERM; echo ready; while :; do sleep 0.1; done'
CHILD_STUCK = 'trap "echo ignoring-term" TERM; echo ready; while :; do sleep 0.1; done'


class Rig(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="keep-running-")
        os.chmod(self.tmp, 0o700)
        self.tmpd = os.path.join(self.tmp, "dash")
        self.home = os.path.join(self.tmp, "home")
        self.root = os.path.join(self.home, "rc-dashboard")
        self.etc = os.path.join(self.root, "etc")
        self.code = os.path.join(self.root, "current", "dashboard")
        for d in (self.tmpd, os.path.join(self.tmpd, "logs"), os.path.join(self.tmpd, "data"),
                  self.etc, self.code):
            os.makedirs(d, 0o700)
        with open(os.path.join(self.etc, "auth.env"), "w") as fh:
            fh.write("# set by hand\nRC_DASH_PASSWORD_HASH=%s\n" % HASH)
        self.bin = os.path.join(self.tmp, "bin")
        os.mkdir(self.bin, 0o700)
        for name, body in STUBS.items():
            p = os.path.join(self.bin, name)
            with open(p, "w") as fh:
                fh.write(body)
            os.chmod(p, 0o700)
        self.calls_file = os.path.join(self.tmp, "calls.log")
        self.scopes = os.path.join(self.tmp, "scopes")
        os.mkdir(self.scopes, 0o700)
        self.procs = []

    def tearDown(self):
        # A failed test can leave the loop, its log viewer, and its process running:
        # the loop's process group, then each fake scope's process.
        for p in self.procs:
            if p.poll() is None:
                try:
                    os.killpg(p.pid, signal.SIGKILL)
                except OSError:
                    pass
            try:
                p.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        for f in os.listdir(self.scopes):
            try:
                with open(os.path.join(self.scopes, f)) as fh:
                    os.kill(int(fh.read()), signal.SIGKILL)
            except (OSError, ValueError):
                pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def env(self, cmd, pause="0", **extra):
        e = dict(os.environ, PATH=self.bin + ":" + os.environ["PATH"], HOME=self.home,
                 RC_DASH_ROOT=self.root, RC_DASH_RUN=self.tmpd, RC_FLEET_ETC=self.etc,
                 RC_KEEP_CHECK=os.path.join(self.bin, "check-local.sh"),
                 RC_KEEP_CMD=cmd, RC_KEEP_PAUSE=pause, STUB_LOG=self.calls_file,
                 STUB_SCOPES=self.scopes)
        for k in ("RC_DASH_PATH", "RC_DASH_MEMORY_MAX"):
            e.pop(k, None)
        for k, v in extra.items():      # None takes a variable out
            if v is None:
                e.pop(k, None)
            else:
                e[k] = v
        return e

    def start(self, name, cmd, **kw):
        p = subprocess.Popen(["bash", SCRIPT, name], env=self.env(cmd, **kw),
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             universal_newlines=True, start_new_session=True)
        self.procs.append(p)
        return p

    def run_loop(self, name, cmd, timeout=30, **kw):
        p = self.start(name, cmd, **kw)
        out, _ = p.communicate(timeout=timeout)
        return p.returncode, out

    def calls(self):
        if not os.path.exists(self.calls_file):
            return ""
        with open(self.calls_file) as fh:
            return fh.read()

    def runs(self):
        return [l for l in self.calls().splitlines() if l.startswith("systemd-run ")]

    def log_path(self, name):
        return os.path.join(self.tmpd, "logs", LOGS[name])

    def log(self, name):
        p = self.log_path(name)
        if not os.path.exists(p):
            return ""
        with open(p) as fh:
            return fh.read()

    def wait_for(self, name, text, count=1, timeout=15):
        end = time.time() + timeout
        while time.time() < end:
            if self.log(name).count(text) >= count:
                return
            time.sleep(0.05)
        self.fail("%r did not appear %d time(s) in the %s log:\n%s" % (text, count, name, self.log(name)))


class TestFolders(Rig):
    def test_refuses_to_start_without_the_deployment_folder(self):
        """There is no default folder, in /tmp or in the home: the unit names it."""
        p = subprocess.run(["bash", SCRIPT, "dashboard"], env=self.env("echo no", RC_DASH_ROOT=None),
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           universal_newlines=True, timeout=30)
        self.assertEqual(p.returncode, 2)
        self.assertIn("RC_DASH_ROOT", p.stdout)
        self.assertEqual(self.calls(), "", "no check and no start")

    def test_logs_go_to_the_deployment_folder(self):
        """Without a run folder of its own, everything is in RC_DASH_ROOT."""
        os.mkdir(os.path.join(self.root, "logs"), 0o700)
        self.run_loop("stream", "echo in-the-root; exit 3", RC_DASH_RUN=None)
        with open(os.path.join(self.root, "logs", "rc-fleet-stream.log")) as fh:
            self.assertIn("in-the-root", fh.read())
        self.assertEqual(os.listdir(os.path.join(self.tmpd, "logs")), [])

    def test_dashboard_cap_comes_from_the_unit(self):
        self.run_loop("dashboard", "exit 0", RC_DASH_MEMORY_MAX="32G")
        runs = self.runs()
        self.assertTrue(runs)
        for r in runs:
            self.assertIn(" -p MemoryMax=32G -- ", r)


class TestStarts(Rig):
    def test_dashboard_runs_in_a_capped_scope_until_the_start_limit(self):
        """Four starts in six hours and the loop stops, as the old unit's start limit did:
        each start refills the history. Without RC_DASH_MEMORY_MAX the cap is 7G."""
        code, out = self.run_loop("dashboard", "echo child-ran")
        runs = self.runs()
        self.assertEqual(len(runs), 4, self.calls())
        for r in runs:
            self.assertIn("systemd-run --user --scope --quiet --collect --unit=rc-dashboard ", r)
            self.assertIn(" -p MemoryMax=7G -- ", r)
            self.assertNotIn("MemoryHigh", r)
        log = self.log("dashboard")
        self.assertEqual(log.count("child-ran"), 4)
        self.assertIn("started 4 times in 360 min", log)
        self.assertNotEqual(code, 0)

    def test_stream_runs_in_its_own_scope_and_stops_on_exit_3(self):
        """Exit 3 is the stream's unsafe-folder exit, which the old unit did not restart on."""
        code, out = self.run_loop("stream", "echo unsafe; exit 3")
        runs = self.runs()
        self.assertEqual(len(runs), 1, self.calls())
        self.assertIn("--unit=rc-fleet-stream ", runs[0])
        self.assertIn(" -p MemoryMax=512M -- ", runs[0])
        self.assertIn("exit 3", self.log("stream"))

    def test_stream_starts_again_after_a_crash(self):
        code, out = self.run_loop("stream", "exit 1")
        self.assertEqual(len(self.runs()), 5, self.calls())
        self.assertIn("started 5 times in 10 min", self.log("stream"))

    def test_each_start_runs_the_start_checks(self):
        self.run_loop("stream", "exit 3")
        self.run_loop("dashboard", "exit 0")
        checks = [l for l in self.calls().splitlines() if l.startswith("check ")]
        self.assertIn("check --base-ok", checks)
        self.assertIn("check --port-free", checks)
        self.assertIn("check --auth-set", checks)
        stream_part = self.calls().split("--unit=rc-fleet-stream")[0]
        self.assertNotIn("--port-free", stream_part)

    def test_trip_stops_before_any_start(self):
        code, out = self.run_loop("dashboard", "echo no", STUB_CHECK_base_ok="11")
        self.assertEqual(self.runs(), [])
        self.assertIn("--base-ok failed (exit 11)", self.log("dashboard"))
        self.assertEqual(self.calls().count("check --base-ok"), 1)

    def test_unsafe_folder_stops_without_writing_there(self):
        code, out = self.run_loop("dashboard", "echo no", STUB_CHECK_dirs_only="13")
        self.assertEqual(self.runs(), [])
        self.assertIn("stopped", out)
        self.assertFalse(os.path.exists(self.log_path("dashboard")))

    def test_taken_port_is_tried_until_the_start_limit(self):
        code, out = self.run_loop("dashboard", "echo no", STUB_CHECK_port_free="14")
        self.assertEqual(self.runs(), [])
        self.assertEqual(self.calls().count("check --port-free"), 4)
        self.assertIn("started 4 times in 360 min", self.log("dashboard"))

    def test_leftover_scope_is_stopped_before_the_checks(self):
        """A dashboard left by a loop that was killed still holds the port: stopping it
        must come before --port-free, or every start fails on the port."""
        self.run_loop("dashboard", "exit 0", STUB_ACTIVE="rc-dashboard.scope")
        calls = self.calls()
        self.assertIn("systemctl --user stop rc-dashboard.scope", calls)
        self.assertLess(calls.index("systemctl --user stop rc-dashboard.scope"),
                        calls.index("check --port-free"))

    def test_one_loop_per_process(self):
        """The lock is the log itself, which the process keeps open: no lock file of
        its own is written."""
        with open(self.log_path("dashboard"), "a") as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            code, out = self.run_loop("dashboard", "echo no")
        self.assertEqual(self.runs(), [])
        self.assertNotEqual(code, 0)
        self.assertIn("already runs", self.log("dashboard"))
        self.assertEqual(sorted(os.listdir(self.tmpd)), ["data", "logs"])

    def test_trip_marker_is_checked_just_before_the_start(self):
        """The stream can trip between the start checks and the start."""
        open(os.path.join(self.tmpd, "TRIPPED"), "w").close()
        code, out = self.run_loop("dashboard", "echo no")
        self.assertEqual(self.runs(), [])
        self.assertNotEqual(code, 0)
        self.assertIn("TRIPPED", self.log("dashboard"))
        self.assertEqual(self.calls().count("check --port-free"), 1)

    def test_log_is_private(self):
        self.run_loop("stream", "exit 3")
        self.assertEqual(stat.S_IMODE(os.stat(self.log_path("stream")).st_mode), 0o600)


class TestSettings(Rig):
    REPORT = ('echo "cfg=$RC_DASH_CONFIG|arena=$MALLOC_ARENA_MAX|hash=${RC_DASH_PASSWORD_HASH:-none}'
              '|path=$PATH|cwd=$(pwd)|umask=$(umask)|core=$(ulimit -c)|unbuffered=$PYTHONUNBUFFERED'
              '|sid=$(ps -o sid= -p $$ | tr -d " ")|pid=$$"; exit 3')

    def report(self, name):
        line = [l for l in self.log(name).splitlines() if l.startswith("cfg=")][0]
        return dict(kv.split("=", 1) for kv in line.split("|"))

    def test_dashboard_gets_its_settings(self):
        path = self.bin + ":/opt/codex/bin:/usr/local/bin:/usr/bin:/bin"
        self.run_loop("dashboard", self.REPORT, RC_DASH_PATH=path)
        r = self.report("dashboard")
        self.assertEqual(r["cfg"], os.path.join(self.etc, "dashboard.config.json"))
        self.assertEqual(r["arena"], "2")
        self.assertEqual(r["hash"], HASH)
        self.assertEqual(r["path"], path)
        self.assertEqual(r["cwd"], self.code)
        self.assertEqual((r["umask"], r["core"], r["unbuffered"]), ("0077", "0", "1"))

    def test_child_has_a_session_of_its_own(self):
        """Ctrl-C and the hang-up when tmux goes away reach the loop, never the process."""
        self.run_loop("stream", self.REPORT)
        r = self.report("stream")
        self.assertEqual(r["sid"], r["pid"])

    def test_stream_never_sees_the_password_hash(self):
        self.run_loop("stream", self.REPORT)
        r = self.report("stream")
        self.assertEqual(r["hash"], "none")
        self.assertEqual(r["cwd"], self.home)


class TestSignals(Rig):
    def test_term_stops_the_process_and_ends_the_loop(self):
        p = self.start("dashboard", CHILD_WAITS)
        self.wait_for("dashboard", "ready")
        p.send_signal(signal.SIGTERM)
        p.communicate(timeout=20)
        self.assertIn("got-term", self.log("dashboard"))
        self.assertEqual(len(self.runs()), 1)

    def test_hangup_stops_the_process_and_ends_the_loop(self):
        """tmux going away hangs up the window: the loop stops its process first."""
        p = self.start("stream", CHILD_WAITS)
        self.wait_for("stream", "ready")
        p.send_signal(signal.SIGHUP)
        p.communicate(timeout=20)
        self.assertIn("got-term", self.log("stream"))
        self.assertEqual(len(self.runs()), 1)

    def test_ctrl_c_restarts_the_process(self):
        p = self.start("dashboard", CHILD_WAITS)
        self.wait_for("dashboard", "ready")
        p.send_signal(signal.SIGINT)
        self.wait_for("dashboard", "ready", count=2)
        p.send_signal(signal.SIGTERM)
        p.communicate(timeout=20)
        self.assertEqual(self.log("dashboard").count("got-term"), 2)
        self.assertEqual(len(self.runs()), 2)

    def test_stuck_process_is_killed(self):
        """A process that ignores SIGTERM (a stalled dashboard) is killed when its scope
        stops, so Ctrl-C, or the session ending, never waits on it for ever."""
        p = self.start("dashboard", CHILD_STUCK)
        self.wait_for("dashboard", "ready")
        p.send_signal(signal.SIGINT)
        self.wait_for("dashboard", "ready", count=2)
        self.assertIn("exit 137", self.log("dashboard"))
        p.send_signal(signal.SIGTERM)
        p.communicate(timeout=20)
        self.assertEqual(len(self.runs()), 2)
        self.assertIn("systemctl --user stop --no-block rc-dashboard.scope", self.calls())

    def test_ctrl_backslash_is_like_ctrl_c(self):
        """SIGQUIT must not kill the loop and leave its process running alone."""
        p = self.start("stream", CHILD_WAITS)
        self.wait_for("stream", "ready")
        p.send_signal(signal.SIGQUIT)
        self.wait_for("stream", "ready", count=2)
        p.send_signal(signal.SIGTERM)
        p.communicate(timeout=20)
        self.assertEqual(self.log("stream").count("got-term"), 2)
        self.assertEqual(len(self.runs()), 2)

    def test_ctrl_c_in_the_pause_ends_the_loop(self):
        p = self.start("dashboard", "exit 1", pause="30")
        self.wait_for("dashboard", "starts again in 30 s")
        p.send_signal(signal.SIGINT)
        t0 = time.time()
        p.communicate(timeout=10)
        self.assertLess(time.time() - t0, 5)
        self.assertEqual(len(self.runs()), 1)
        self.assertIn("stays stopped", self.log("dashboard"))


class TestSession(Rig):
    def test_session_has_both_windows_and_keeps_dead_ones(self):
        p = subprocess.run(["bash", SESSION], env=self.env("unused"),
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        args = self.calls().splitlines()
        keep = os.path.join(KIT, "keep-running.sh")
        self.assertEqual(args, [
            "-L", "rc-dashboard", "-f", "/dev/null",
            "new-session", "-d", "-s", "rc-dashboard", "-n", "stream", "exec %s stream" % keep, ";",
            "set-window-option", "-g", "remain-on-exit", "on", ";",
            "new-window", "-d", "-t", "rc-dashboard:", "-n", "dashboard", "exec %s dashboard" % keep])


if __name__ == "__main__":
    unittest.main()
