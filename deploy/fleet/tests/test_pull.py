"""pull.py, run for real: rsync copies from stand-in nodes, each a folder here, through an
ssh stand-in that runs the node's side in that folder (or under `rrsync -ro`, as a
restricted key would)."""
import fcntl
import gzip
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
KIT = os.path.dirname(HERE)
SCRIPT = os.path.join(KIT, "pull.py")
sys.path.insert(0, KIT)
import pull                                                         # noqa: E402

HAVE_RSYNC = shutil.which("rsync") is not None
HAVE_RRSYNC = shutil.which("rrsync") is not None

# ssh as rsync runs it: options, then [user@]host, then the command. The node's side runs
# in $FAKE_NODES/<host>; a host with no folder there is a node that does not answer.
SSH = r'''#!/bin/bash
host=
while [ $# -gt 0 ]; do
  case $1 in
    -i|-o|-p|-l|-F|-S|-E|-c|-m|-b|-J|-O) shift 2 ;;
    -*) shift ;;
    *) host=${1#*@}; shift; break ;;
  esac
done
echo "$host" >> "$FAKE_NODES/.calls"
cd "$FAKE_NODES/$host" 2>/dev/null || { echo "ssh: connect to host $host: refused" >&2; exit 255; }
if [ -n "${FAKE_RRSYNC:-}" ]; then
  SSH_ORIGINAL_COMMAND="$*" exec rrsync -ro "$FAKE_NODES/$host"
fi
exec sh -c "$*"
'''


@unittest.skipUnless(HAVE_RSYNC, "rsync is not installed")
class Rig(unittest.TestCase):
    HOSTS = ("n1", "n2")

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pull-")
        self.nodes = os.path.join(self.tmp, "nodes")
        self.mirror = os.path.join(self.tmp, "var", "mirror")
        os.makedirs(self.nodes)
        os.makedirs(os.path.dirname(self.mirror))
        for h in self.HOSTS:
            os.makedirs(os.path.join(self.nodes, h, "ebpfm"))
        self.ssh = os.path.join(self.tmp, "ssh")
        with open(self.ssh, "w") as fh:
            fh.write(SSH)
        os.chmod(self.ssh, 0o755)
        self.conf = os.path.join(self.tmp, "fleet.env")
        self.settings()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def settings(self, **over):
        vals = {"FLEET_HOSTS": " ".join(self.HOSTS), "FLEET_SSH": self.ssh, "FLEET_SSH_KEY": "",
                "FLEET_EBPF_SOURCE": "ebpfm", "FLEET_NODE_SOURCE": "ebpfm-login",
                "FLEET_MIRROR": self.mirror, "FLEET_FREE_MIN_GB": "0"}
        vals.update(over)
        with open(self.conf, "w") as fh:
            for k, v in vals.items():
                fh.write('%s="%s"\n' % (k, v))

    def run_pull(self, command="once", code=0, rrsync=False):
        env = dict(os.environ, FAKE_NODES=self.nodes)
        if rrsync:
            env["FAKE_RRSYNC"] = "1"
        p = subprocess.run([sys.executable, SCRIPT, "--config", self.conf, command], env=env,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           universal_newlines=True, timeout=120)
        self.assertEqual(p.returncode, code, p.stdout + p.stderr)
        return p.stdout + p.stderr

    # the node's side
    def src(self, host, rel, tier="ebpfm"):
        return os.path.join(self.nodes, host, tier, rel)

    def write(self, host, rel, data, tier="ebpfm", mode="ab"):
        p = self.src(host, rel, tier)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, mode) as fh:
            fh.write(data)

    def seal(self, host, rel):
        """What the collector's `gzip -f` leaves: the whole .gz, and no plain file."""
        p = self.src(host, rel)
        with open(p, "rb") as fh, gzip.open(p + ".gz", "wb") as out:
            out.write(fh.read())
        os.unlink(p)

    # the copy
    def dst(self, host, rel, tier="ebpf"):
        return os.path.join(self.mirror, tier, host, rel)

    def read(self, path):
        opener = gzip.open if path.endswith(".gz") else open
        with opener(path, "rb") as fh:
            return fh.read()


def lines(start, n):
    return b"".join(b'{"n":%d,"pad":"%s"}\n' % (i, b"x" * 40) for i in range(start, start + n))


class TestCopy(Rig):
    def test_the_copy_has_the_collectors_layout(self):
        self.write("n1", "2026-11-20/n1.exits.jsonl", lines(0, 50))
        self.seal("n1", "2026-11-20/n1.exits.jsonl")
        self.write("n1", "2026-11-21/n1.exits.jsonl", lines(50, 5))
        self.write("n1", "2026-11-21/n1.snapshot.jsonl", lines(0, 3))
        self.write("n2", "2026-11-21/n2.exits.jsonl", lines(0, 7))
        self.run_pull()
        self.assertEqual(self.read(self.dst("n1", "2026-11-20/n1.exits.jsonl.gz")), lines(0, 50))
        self.assertFalse(os.path.exists(self.dst("n1", "2026-11-20/n1.exits.jsonl")))
        self.assertEqual(self.read(self.dst("n1", "2026-11-21/n1.exits.jsonl")), lines(50, 5))
        self.assertEqual(self.read(self.dst("n1", "2026-11-21/n1.snapshot.jsonl")), lines(0, 3))
        self.assertEqual(self.read(self.dst("n2", "2026-11-21/n2.exits.jsonl")), lines(0, 7))

    def test_a_growing_file_keeps_its_inode(self):
        """The dashboard follows a file by (st_dev, st_ino): a new inode on every pass
        would have it read the whole day again."""
        rel = "2026-11-21/n1.exits.jsonl"
        self.write("n1", rel, lines(0, 100))
        self.run_pull()
        ino = os.stat(self.dst("n1", rel)).st_ino
        for i in range(3):
            self.write("n1", rel, lines(100 + 10 * i, 10))
            self.run_pull()
            self.assertEqual(os.stat(self.dst("n1", rel)).st_ino, ino)
        self.assertEqual(self.read(self.dst("n1", rel)), lines(0, 130))

    def test_a_line_cut_short_is_finished_by_the_next_pass(self):
        rel = "2026-11-21/n1.exits.jsonl"
        self.write("n1", rel, b'{"a":1}\n{"a":')
        self.run_pull()
        self.assertEqual(self.read(self.dst("n1", rel)), b'{"a":1}\n{"a":')
        self.write("n1", rel, b'2}\n')
        self.run_pull()
        self.assertEqual(self.read(self.dst("n1", rel)), b'{"a":1}\n{"a":2}\n')

    def test_a_gz_waits_until_gzip_is_done_with_it(self):
        """While gzip runs on the node both files are there and the .gz is short. It is
        not copied until the plain file is gone: a .gz in the copy is always whole."""
        rel = "2026-11-20/n1.exits.jsonl"
        self.write("n1", rel, lines(0, 2000))
        whole = gzip.compress(lines(0, 2000))
        self.write("n1", rel + ".gz", whole[:len(whole) // 2], mode="wb")
        self.run_pull()
        self.assertEqual(self.read(self.dst("n1", rel)), lines(0, 2000))
        self.assertFalse(os.path.exists(self.dst("n1", rel + ".gz")))
        self.write("n1", rel + ".gz", whole, mode="wb")
        os.unlink(self.src("n1", rel))
        self.run_pull()
        self.assertEqual(self.read(self.dst("n1", rel + ".gz")), lines(0, 2000))
        self.assertFalse(os.path.exists(self.dst("n1", rel)))

    def test_a_day_sealed_between_passes_is_read_whole(self):
        """The copy's plain file stopped short at the last pass; the node wrote more, then
        sealed the day. The plain copy goes, and the .gz holds every line."""
        rel = "2026-11-20/n1.exits.jsonl"
        self.write("n1", rel, lines(0, 100))
        self.run_pull()
        self.write("n1", rel, lines(100, 25))
        self.seal("n1", rel)
        out = self.run_pull()
        self.assertFalse(os.path.exists(self.dst("n1", rel)))
        self.assertEqual(self.read(self.dst("n1", rel + ".gz")), lines(0, 125))
        self.assertIn("sealed", out)

    def test_a_gz_that_does_not_inflate_keeps_the_plain_copy(self):
        rel = "2026-11-20/n1.exits.jsonl"
        self.write("n1", rel, lines(0, 100))
        self.run_pull()
        bad = bytearray(gzip.compress(lines(0, 120)))
        bad[-8] ^= 0xFF                             # the CRC
        self.write("n1", rel + ".gz", bytes(bad), mode="wb")
        os.unlink(self.src("n1", rel))
        out = self.run_pull()
        self.assertEqual(self.read(self.dst("n1", rel)), lines(0, 100))
        self.assertIn("does not inflate whole", out)
        self.run_pull()
        self.assertEqual(self.read(self.dst("n1", rel)), lines(0, 100))

    def test_a_gz_that_did_not_inflate_is_not_tried_again_until_it_changes(self):
        """A loop's passes: an unchanged .gz is not inflated again, nor reported again."""
        rel = "2026-11-20/n1.exits.jsonl"
        self.write("n1", rel, lines(0, 100))
        self.run_pull()
        self.write("n1", rel + ".gz", gzip.compress(lines(0, 120))[:-1], mode="wb")
        os.unlink(self.src("n1", rel))
        os.environ["FAKE_NODES"] = self.nodes
        self.addCleanup(os.environ.pop, "FAKE_NODES", None)
        calls, said = [], []
        real, real_say = pull.gz_complete, pull.say
        pull.gz_complete = lambda path: calls.append(path) or real(path)
        pull.say = said.append
        self.addCleanup(setattr, pull, "gz_complete", real)
        self.addCleanup(setattr, pull, "say", real_say)
        p = pull.Puller(pull.load(self.conf), "n1")
        for _ in range(3):
            self.assertTrue(p.pull()["ok"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(sum("does not inflate whole" in s for s in said), 1)
        self.assertEqual(self.read(self.dst("n1", rel)), lines(0, 100))
        self.write("n1", rel + ".gz", gzip.compress(lines(0, 120)), mode="wb")
        self.assertTrue(p.pull()["ok"])
        self.assertEqual(len(calls), 2)
        self.assertFalse(os.path.exists(self.dst("n1", rel)))
        self.assertEqual(self.read(self.dst("n1", rel + ".gz")), lines(0, 120))

    def test_a_file_replaced_on_the_node_is_fetched_again(self):
        rel = "2026-11-21/n1.exits.jsonl"
        self.write("n1", rel, lines(0, 100))
        self.run_pull()
        ino = os.stat(self.dst("n1", rel)).st_ino
        self.write("n1", rel, lines(500, 3), mode="wb")
        out = self.run_pull()
        self.assertEqual(self.read(self.dst("n1", rel)), lines(500, 3))
        self.assertNotEqual(os.stat(self.dst("n1", rel)).st_ino, ino)
        self.assertIn("replaced there", out)

    def test_only_the_newest_days(self):
        days = ["2026-11-%02d" % d for d in range(10, 22)]
        for d in days:
            self.write("n1", "%s/n1.exits.jsonl" % d, lines(0, 2))
        self.settings(FLEET_DAYS="3")
        self.run_pull()
        self.assertEqual(sorted(os.listdir(os.path.join(self.mirror, "ebpf", "n1"))), days[-3:])
        self.settings(FLEET_DAYS="0")
        self.run_pull()
        self.assertEqual(sorted(os.listdir(os.path.join(self.mirror, "ebpf", "n1"))), days)

    def test_nothing_but_day_files(self):
        """No log, no feature cache, no link, nothing outside a dated folder."""
        self.write("n1", "2026-11-21/n1.exits.jsonl", lines(0, 2))
        self.write("n1", "ebpfm_n1.log", b"log\n")
        self.write("n1", ".features_6.8.json", b"{}\n")
        self.write("n1", "2026-11-21/notes.txt", b"x\n")
        self.write("n1", "2026-11-21/sub/n1.exits.jsonl", lines(0, 2))
        self.write("n1", "old/n1.exits.jsonl", lines(0, 2))
        secret = os.path.join(self.tmp, "secret.jsonl")
        with open(secret, "w") as fh:
            fh.write("secret\n")
        os.symlink(secret, self.src("n1", "2026-11-21/n1.link.jsonl"))
        os.symlink(os.path.dirname(secret), self.src("n1", "2026-11-22"))
        self.run_pull()
        got = []
        for top, _dirs, files in os.walk(os.path.join(self.mirror, "ebpf")):
            got += [os.path.relpath(os.path.join(top, f), self.mirror) for f in files]
        self.assertEqual(got, ["ebpf/n1/2026-11-21/n1.exits.jsonl"])

    def test_the_node_tier(self):
        self.write("n1", "2026-11-21/n1.exits.jsonl", lines(0, 2))
        self.write("n1", "2026-11-21/n1.jsonl", lines(0, 4), tier="ebpfm-login")
        self.run_pull()
        self.assertEqual(self.read(self.dst("n1", "2026-11-21/n1.jsonl", tier="node")), lines(0, 4))

    def test_the_copy_is_private(self):
        self.write("n1", "2026-11-21/n1.exits.jsonl", lines(0, 2))
        os.chmod(self.src("n1", "2026-11-21/n1.exits.jsonl"), 0o644)
        self.run_pull()
        for top, dirs, files in os.walk(self.mirror):
            for d in dirs:
                self.assertEqual(stat.S_IMODE(os.stat(os.path.join(top, d)).st_mode), 0o700, d)
            for f in files:
                p = os.path.join(top, f)
                if not stat.S_ISSOCK(os.lstat(p).st_mode):
                    self.assertEqual(stat.S_IMODE(os.stat(p).st_mode) & 0o077, 0, p)

    def test_keep_days_removes_the_oldest_folders(self):
        days = ["2026-11-%02d" % d for d in range(15, 22)]
        for d in days:
            self.write("n1", "%s/n1.exits.jsonl" % d, lines(0, 2))
        self.settings(FLEET_KEEP_DAYS="2")
        self.run_pull()
        self.assertEqual(sorted(os.listdir(os.path.join(self.mirror, "ebpf", "n1"))), days[-2:])


class TestRun(Rig):
    def test_a_node_that_does_not_answer_fails_alone(self):
        self.write("n1", "2026-11-21/n1.exits.jsonl", lines(0, 2))
        shutil.rmtree(os.path.join(self.nodes, "n2"))
        out = self.run_pull(code=1)
        self.assertIn("n2: FAILED", out)
        self.assertIn("refused", out)
        self.assertTrue(os.path.exists(self.dst("n1", "2026-11-21/n1.exits.jsonl")))
        status = self.run_pull("status", code=1)
        self.assertRegex(status, r"n1\s+ok")
        self.assertRegex(status, r"n2\s+FAILED")

    def test_a_second_pull_does_not_start(self):
        self.run_pull()
        with open(os.path.join(self.mirror, ".state", "pull.lock"), "a") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            out = self.run_pull(code=3)
        self.assertIn("another pull.py", out)

    def test_paused_when_the_disk_is_full(self):
        self.write("n1", "2026-11-21/n1.exits.jsonl", lines(0, 2))
        self.settings(FLEET_FREE_MIN_GB="1e12")
        out = self.run_pull(code=1)
        self.assertIn("paused", out)
        self.assertFalse(os.path.exists(self.dst("n1", "2026-11-21/n1.exits.jsonl")))

    def test_loop_stops_on_sigterm(self):
        self.write("n1", "2026-11-21/n1.exits.jsonl", lines(0, 2))
        self.settings(FLEET_INTERVAL_S="5")
        env = dict(os.environ, FAKE_NODES=self.nodes)
        p = subprocess.Popen([sys.executable, SCRIPT, "--config", self.conf, "loop"], env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        try:
            deadline = time.time() + 30
            while not os.path.exists(self.dst("n1", "2026-11-21/n1.exits.jsonl")):
                self.assertLess(time.time(), deadline, "the loop pulled nothing")
                time.sleep(0.2)
            p.send_signal(signal.SIGTERM)
            self.assertEqual(p.wait(timeout=20), 0)
        finally:
            if p.poll() is None:
                p.kill()
            p.stdout.close()

    def test_roots(self):
        out = self.run_pull("roots")
        self.assertEqual(out.splitlines(), [
            "RC_DASH_EBPF_ROOTS=%s/ebpf/n1:%s/ebpf/n2" % (self.mirror, self.mirror),
            "RC_DASH_NODE_ROOTS=%s/node/n1:%s/node/n2" % (self.mirror, self.mirror)])

    def test_without_the_node_tier_its_root_is_named_missing(self):
        """Empty, the dashboard would look in its default places, this host's own
        /var/log/ebpfm-login among them."""
        self.settings(FLEET_NODE_SOURCE="")
        out = self.run_pull("roots")
        self.assertIn("RC_DASH_NODE_ROOTS=%s/node-tier-not-pulled" % self.mirror, out)
        self.write("n1", "2026-11-21/n1.jsonl", lines(0, 4), tier="ebpfm-login")
        self.run_pull()
        self.assertFalse(os.path.exists(os.path.join(self.mirror, "node")))

    @unittest.skipUnless(HAVE_RRSYNC, "rrsync is not installed")
    def test_through_a_key_restricted_to_rrsync(self):
        """The key the README recommends runs `rrsync -ro`, which refuses any rsync
        option it does not allow and any path outside its folder."""
        rel = "2026-11-20/n1.exits.jsonl"
        self.write("n1", rel, lines(0, 100))
        self.write("n2", "2026-11-21/n2.exits.jsonl", lines(0, 3))
        self.run_pull(rrsync=True)
        self.write("n1", rel, lines(100, 5))
        self.seal("n1", rel)
        self.run_pull(rrsync=True)
        self.assertEqual(self.read(self.dst("n1", rel + ".gz")), lines(0, 105))
        self.assertFalse(os.path.exists(self.dst("n1", rel)))
        self.assertEqual(self.read(self.dst("n2", "2026-11-21/n2.exits.jsonl")), lines(0, 3))


class TestSettings(unittest.TestCase):
    def load(self, text):
        with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as fh:
            fh.write(text)
        try:
            return pull.load(fh.name)
        finally:
            os.unlink(fh.name)

    def test_targets(self):
        s = self.load('FLEET_HOSTS="a b=admin@10.0.0.2 c=alias"\nFLEET_USER=pull\n'
                      "FLEET_DOMAIN=example.org\n")
        self.assertEqual(s.hosts, {"a": "pull@a.example.org", "b": "admin@10.0.0.2",
                                   "c": "pull@alias"})

    def test_refused(self):
        for text, why in (("FLEET_HOSTS=\n", "names no node"),
                          ("FLEET_HOSTS='a a'\n", "twice"),
                          ("FLEET_HOSTS=../x\n", "not a node name"),
                          ("FLEET_HOSTS=a\nFLEET_HOST=b\n", "unknown setting"),
                          ("FLEET_HOSTS=a\nFLEET_DAYS=x\n", "whole number"),
                          ("FLEET_HOSTS=a\nFLEET_EBPF_SOURCE=\n", "eBPF tier"),
                          ("FLEET_HOSTS=a\nFLEET_MIRROR='/a b'\n", "no spaces"),
                          ("FLEET_HOSTS=a\nnot a setting\n", "KEY=VALUE")):
            with self.assertRaises(pull.ConfigError) as cm:
                self.load(text)
            self.assertIn(why, str(cm.exception), text)

    def test_the_ssh_command_shares_one_connection_where_it_fits(self):
        s = self.load("FLEET_HOSTS=a\nFLEET_MIRROR=/var/lib/rc-dashboard/mirror\n")
        self.assertIn("ControlPath=/var/lib/rc-dashboard/mirror/.state/cm-%C", s.ssh())
        s = self.load("FLEET_HOSTS=a\nFLEET_MIRROR=/%s\n" % ("x" * 120))
        self.assertNotIn("ControlPath", s.ssh())


class TestParts(unittest.TestCase):
    def test_listing(self):
        text = ("drwxr-x---          4,096 2026/11/21 10:00:00 .\n"
                "drwxr-x---          4,096 2026/11/21 10:00:00 2026-11-21\n"
                "-rw-r-----  1,234,567,890 2026/11/21 10:00:00 2026-11-21/h.exits.jsonl\n"
                "-rw-r-----             12 2026/11/21 10:00:00 2026-11-20/h.exits.jsonl.gz\n"
                "lrwxrwxrwx             11 2026/11/21 10:00:00 2026-11-21/l.jsonl\n"
                "-rw-r-----             12 2026/11/21 10:00:00 2026-11-21/a\\#012b.jsonl\n"
                "-rw-r-----             12 2026/11/21 10:00:00 x/h.jsonl\n")
        self.assertEqual(pull.parse_listing(text), {"2026-11-21/h.exits.jsonl": 1234567890,
                                                    "2026-11-20/h.exits.jsonl.gz": 12})

    def test_gz_complete(self):
        tmp = tempfile.mkdtemp(prefix="gz-")
        self.addCleanup(shutil.rmtree, tmp)

        def check(name, data):
            p = os.path.join(tmp, name)
            with open(p, "wb") as fh:
                fh.write(data)
            return pull.gz_complete(p)

        one = gzip.compress(lines(0, 3000))
        two = gzip.compress(lines(0, 10)) + gzip.compress(lines(10, 10))
        self.assertTrue(check("whole", one))
        self.assertTrue(check("members", two))
        self.assertTrue(check("tiny", gzip.compress(b"")))
        self.assertFalse(check("empty", b""))
        self.assertFalse(check("short", one[:-1]))
        self.assertFalse(check("half", one[:len(one) // 2]))
        self.assertFalse(check("second-short", two[:-3]))
        self.assertFalse(check("garbage", one + b"\0\0\0\0"))
        bad = bytearray(one)
        bad[-5] ^= 1                                # the length
        self.assertFalse(check("length", bytes(bad)))
        self.assertFalse(check("plain", lines(0, 3)))
        # an output far past the read size: a member ends deep inside a read
        big = zlib.compressobj(9, zlib.DEFLATED, 31)
        data = big.compress(b"\0" * (64 << 20)) + big.flush()
        self.assertTrue(check("deep", data))

    def test_plan(self):
        remote = {"2026-11-20/h.exits.jsonl.gz": 50,      # sealed, the copy has its plain
                  "2026-11-21/h.exits.jsonl": 100,        # live, the copy is behind
                  "2026-11-21/h.exits.jsonl.gz": 10,      # gzip still running: wait
                  "2026-11-21/h.snapshot.jsonl": 5,       # shorter on the node: replaced
                  "2026-11-19/h.exits.jsonl.gz": 7}       # sealed, the copy has it whole
        local = {"2026-11-20/h.exits.jsonl": 400,
                 "2026-11-21/h.exits.jsonl": 60,
                 "2026-11-21/h.snapshot.jsonl": 9,
                 "2026-11-19/h.exits.jsonl.gz": 7,
                 "2026-11-18/h.exits.jsonl": 3,           # gone from the node, no .gz: kept
                 "2026-11-17/h.exits.jsonl": 3,           # gone from the node, its .gz held
                 "2026-11-17/h.exits.jsonl.gz": 2}
        p = pull.plan(remote, local, 0)
        self.assertEqual(p["append"], ["2026-11-21/h.exits.jsonl", "2026-11-21/h.snapshot.jsonl"])
        self.assertEqual(p["replace"], ["2026-11-21/h.snapshot.jsonl"])
        self.assertEqual(p["fetch"], ["2026-11-20/h.exits.jsonl.gz"])
        self.assertEqual(p["seal"], ["2026-11-17/h.exits.jsonl", "2026-11-20/h.exits.jsonl"])
        p = pull.plan(remote, local, 1)                     # only 2026-11-21 is in the window
        self.assertEqual(p["fetch"], [])
        self.assertEqual(p["seal"], ["2026-11-17/h.exits.jsonl"])


if __name__ == "__main__":
    unittest.main()
