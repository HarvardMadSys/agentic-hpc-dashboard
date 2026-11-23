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

    def test_a_failed_refetch_keeps_the_old_inode_held(self):
        """A replaced file's old copy is held aside until its replacement exists: freed any
        earlier, its inode number could go to the replacement, and the dashboard, which
        knows a file by (st_dev, st_ino), would read that at the old copy's offset."""
        rel = "2026-11-21/n1.exits.jsonl"
        self.write("n1", rel, lines(0, 100))
        self.run_pull()
        old = os.stat(self.dst("n1", rel)).st_ino
        self.write("n1", rel, lines(500, 3), mode="wb")
        os.environ["FAKE_NODES"] = self.nodes
        self.addCleanup(os.environ.pop, "FAKE_NODES", None)
        real_say, real_copy = pull.say, pull.Puller.copy
        pull.say = lambda msg: None
        self.addCleanup(setattr, pull, "say", real_say)
        self.addCleanup(setattr, pull.Puller, "copy", real_copy)

        def dropped(self_, source, root, files, append):
            raise pull.PullError("the link dropped")
        pull.Puller.copy = dropped
        p = pull.Puller(pull.load(self.conf), "n1")
        self.assertFalse(p.pull()["ok"])
        aside = os.path.join(self.mirror, "ebpf", "n1", "2026-11-21", ".n1.exits.jsonl.replaced")
        self.assertEqual(os.stat(aside).st_ino, old, "the old inode is still held")
        self.assertFalse(os.path.exists(self.dst("n1", rel)))
        pull.Puller.copy = real_copy
        self.assertTrue(p.pull()["ok"])
        self.assertEqual(self.read(self.dst("n1", rel)), lines(500, 3))
        self.assertNotEqual(os.stat(self.dst("n1", rel)).st_ino, old)
        self.assertFalse(os.path.exists(aside))

    def test_leftovers_of_a_killed_pass_go_once_their_file_is_there(self):
        """A pass killed between moving a copy aside and removing it leaves `.<name>.replaced`
        behind: it goes once its file, or that file's sealed .gz, is in the copy."""
        self.write("n1", "2026-11-21/n1.exits.jsonl", lines(0, 2))
        self.write("n1", "2026-11-20/n1.exits.jsonl", lines(0, 2))
        self.seal("n1", "2026-11-20/n1.exits.jsonl")
        self.run_pull()
        day = os.path.join(self.mirror, "ebpf", "n1")
        kept = os.path.join(day, "2026-11-19", ".n1.exits.jsonl.replaced")
        for p in (os.path.join(day, "2026-11-21", ".n1.exits.jsonl.replaced"),
                  os.path.join(day, "2026-11-20", ".n1.exits.jsonl.replaced"), kept):
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "wb") as fh:
                fh.write(b"old\n")
        self.run_pull()
        self.assertEqual(sorted(n for d in os.listdir(day) for n in os.listdir(os.path.join(day, d))
                                if n.endswith(".replaced")), [".n1.exits.jsonl.replaced"])
        self.assertTrue(os.path.exists(kept), "no file of its own yet: still held")

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
        """Retention lowered after a copy built up: the folders past the newest KEEP_DAYS go,
        and the next pass leaves them gone (the window copies no more than they keep)."""
        days = ["2026-11-%02d" % d for d in range(15, 22)]
        for d in days:
            self.write("n1", "%s/n1.exits.jsonl" % d, lines(0, 2))
        self.settings(FLEET_DAYS="0")
        self.run_pull()
        self.settings(FLEET_DAYS="2", FLEET_KEEP_DAYS="2")
        self.run_pull()
        self.assertEqual(sorted(os.listdir(os.path.join(self.mirror, "ebpf", "n1"))), days[-2:])
        out = self.run_pull()
        self.assertNotIn("removed", out)
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
            "RC_DASH_EBPF_ROOTS=%s/ebpf/*" % self.mirror,
            "RC_DASH_NODE_ROOTS=%s/node/*" % self.mirror,
            "RC_DASH_TRANSPORT_FILES=%s/.state/*.json" % self.mirror])

    def test_without_the_node_tier_its_root_still_globs(self):
        """No node's node tier is pulled: the glob stays, matching nothing, so a node
        added later with one is found at the dashboard's next rescan (add-node writes no
        paths.env). Matching nothing, it is a missing root, never the dashboard's default
        places -- this host's own /var/log/ebpfm-login among them."""
        self.settings(FLEET_NODE_SOURCE="")
        out = self.run_pull("roots")
        self.assertIn("RC_DASH_NODE_ROOTS=%s/node/*\n" % self.mirror, out)
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
                          ("FLEET_HOSTS=a\nnot a setting\n", "KEY=VALUE"),
                          # the window would copy again what keep-days removes, every pass
                          ("FLEET_HOSTS=a\nFLEET_KEEP_DAYS=3\n", "FLEET_KEEP_DAYS=3"),
                          ("FLEET_HOSTS=a\nFLEET_DAYS=0\nFLEET_KEEP_DAYS=3\n", "FLEET_KEEP_DAYS=3")):
            with self.assertRaises(pull.ConfigError) as cm:
                self.load(text)
            self.assertIn(why, str(cm.exception), text)

    def test_the_ssh_command_shares_one_connection_where_it_fits(self):
        s = self.load("FLEET_HOSTS=a\nFLEET_MIRROR=/var/lib/rc-dashboard/mirror\n")
        self.assertIn("ControlPath=/var/lib/rc-dashboard/mirror/.state/cm-%C", s.ssh())
        s = self.load("FLEET_HOSTS=a\nFLEET_MIRROR=/%s\n" % ("x" * 120))
        self.assertNotIn("ControlPath", s.ssh())
        # ssh binds the master at <ControlPath>.<16 random characters>, %C is 40, and a unix
        # socket's path holds 107: 50 characters of `<state>/cm-` at most
        s = self.load("FLEET_HOSTS=a\nFLEET_MIRROR=/%s\n" % ("x" * 38))
        self.assertEqual(len(os.path.join(s.state, "cm-")), 50)
        self.assertIn("ControlPath", s.ssh())
        s = self.load("FLEET_HOSTS=a\nFLEET_MIRROR=/%s\n" % ("x" * 39))
        self.assertNotIn("ControlPath", s.ssh())


RSYNC_STUB = """#!/bin/bash
# rsync as a node that cannot be reached: log the source asked for, and fail.
for a in "$@"; do case $a in *:*/) echo "$a" >> "$RSYNC_LOG" ;; esac; done
echo "rsync: connection unexpectedly closed" >&2
exit 12
"""


class StubRig(unittest.TestCase):
    """pull.py with a stand-in rsync that fails at once: what a pass asks for, and what
    the loop and `status` make of it, with no rsync installed."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pull-stub-")
        self.mirror = os.path.join(self.tmp, "mirror")
        self.rsync = os.path.join(self.tmp, "rsync")
        with open(self.rsync, "w") as fh:
            fh.write(RSYNC_STUB)
        os.chmod(self.rsync, 0o755)
        self.rsync_log = os.path.join(self.tmp, "rsync.log")
        self.conf = os.path.join(self.tmp, "fleet.env")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def settings(self, hosts, **over):
        vals = {"FLEET_HOSTS": hosts, "FLEET_RSYNC": self.rsync, "FLEET_SSH_KEY": "",
                "FLEET_EBPF_SOURCE": "ebpfm", "FLEET_NODE_SOURCE": "ebpfm-login",
                "FLEET_MIRROR": self.mirror, "FLEET_FREE_MIN_GB": "0", "FLEET_INTERVAL_S": "5"}
        vals.update(over)
        with open(self.conf, "w") as fh:
            for k, v in vals.items():
                fh.write('%s="%s"\n' % (k, v))

    def run_pull(self, *args, code=0):
        p = subprocess.run([sys.executable, SCRIPT, "--config", self.conf] + list(args),
                           env=dict(os.environ, RSYNC_LOG=self.rsync_log),
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           universal_newlines=True, timeout=60)
        self.assertEqual(p.returncode, code, p.stdout + p.stderr)
        return p.stdout + p.stderr

    def asked(self):
        if not os.path.exists(self.rsync_log):
            return []
        with open(self.rsync_log) as fh:
            return fh.read().split()

    def state(self, name, **st):
        d = os.path.join(self.mirror, ".state")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, name + ".json"), "w") as fh:
            json.dump(st, fh)


class TestNodesAndFolders(StubRig):
    def test_each_node_is_asked_for_its_own_folders(self):
        """A mixed fleet: an rrsync key's relative folders on one node, the RPM's full
        paths on another, no node tier on a third."""
        self.settings("a b=admin@10.0.0.2:/scratch/ebpfm:/scratch/ebpfm-login c:ebpfm:")
        self.run_pull("once", code=1)
        asked = self.asked()
        self.assertIn("a:ebpfm/", asked)
        self.assertIn("a:ebpfm-login/", asked[asked.index("a:ebpfm/"):] + ["a:ebpfm-login/"])
        self.assertIn("admin@10.0.0.2:/scratch/ebpfm/", asked)
        self.assertNotIn("admin@10.0.0.2:ebpfm/", asked)
        self.assertIn("c:ebpfm/", asked)
        self.assertNotIn("c:ebpfm-login/", asked)

    def test_a_node_is_one_line_to_add(self):
        with open(self.conf, "w") as fh:
            fh.write('FLEET_HOSTS=\nFLEET_HOSTS+=" a"\nFLEET_HOSTS+=" b=10.0.0.2:x"\n'
                     "FLEET_MIRROR=%s\n" % self.mirror)
        self.assertEqual(self.run_pull("check").strip(), "2 node(s): a b")

    def test_no_node_named_is_its_own_exit(self):
        """rc-pull waits for its nodes (4), apart from a wrong file (2)."""
        self.settings("")
        self.assertIn("names no node", self.run_pull("check", code=4))
        self.run_pull("roots", code=4)
        self.run_pull("loop", code=4)
        self.settings("a", FLEET_DAYS="x")
        self.run_pull("check", code=2)

    def test_roots_glob_over_the_copy(self):
        self.settings("a b:ebpfm:")
        out = self.run_pull("roots")
        self.assertIn("RC_DASH_EBPF_ROOTS=%s/ebpf/*\n" % self.mirror, out)
        self.assertIn("RC_DASH_NODE_ROOTS=%s/node/*\n" % self.mirror, out)
        self.assertIn("RC_DASH_TRANSPORT_FILES=%s/.state/*.json\n" % self.mirror, out)
        out = self.run_pull("roots", "--per-node")
        self.assertIn("RC_DASH_EBPF_ROOTS=%s/ebpf/a:%s/ebpf/b\n" % (self.mirror, self.mirror), out)
        self.assertIn("RC_DASH_NODE_ROOTS=%s/node/a\n" % self.mirror, out)
        self.settings("a:ebpfm: b:ebpfm:")
        self.assertIn("RC_DASH_NODE_ROOTS=%s/node/*\n" % self.mirror, self.run_pull("roots"))
        self.assertIn("RC_DASH_NODE_ROOTS=%s/node-tier-not-pulled\n" % self.mirror,
                      self.run_pull("roots", "--per-node"))


class TestStatus(StubRig):
    def test_a_node_with_no_good_pass_for_three_intervals_is_stale(self):
        """rc-pull stopped (a fleet.env typo ends it for good) left every node's last
        state `ok`, and status said ok, exit 0, for days."""
        now = time.time()
        self.settings("n1 n2 n3 n4", FLEET_INTERVAL_S="60")
        self.state("n1", ok=True, ok_epoch=now - 3 * 86400 - 7200, attempt_epoch=now - 3 * 86400,
                   tiers={"ebpf": {"bytes": 0}})
        self.state("n2", ok=True, ok_epoch=now - 30, attempt_epoch=now - 30,
                   tiers={"ebpf": {"bytes": 1234}})
        self.state("n3", ok=False, ok_epoch=now - 7200, attempt_epoch=now - 7000,
                   error="listing failed (rsync exit 255): Permission denied (publickey)")
        out = self.run_pull("status", code=1)
        self.assertRegex(out, r"n1\s+STALE\s+last ok 3d 2h ago\s+no pass for 3d 2h: is rc-pull running")
        self.assertRegex(out, r"n2\s+ok\s+last ok \d+s ago\s+1234 B appended in the last pass")
        self.assertRegex(out, r"n3\s+FAILED\s+last ok 2h 0m ago\s+listing failed .*Permission denied"
                              r".*last tried 1h 56m ago: is rc-pull running")
        self.assertRegex(out, r"n4\s+never\s+last ok never\s+no pass recorded")

    def test_stale_threshold(self):
        now = time.time()
        self.settings("n1", FLEET_INTERVAL_S="60")
        self.state("n1", ok=True, ok_epoch=now - 600, attempt_epoch=now - 600, tiers={})
        self.assertIn("STALE", self.run_pull("status", code=1))
        self.assertRegex(self.run_pull("status", "--stale", "15m"), r"n1\s+ok")
        self.run_pull("status", "--stale", "soon", code=2)

    def test_json(self):
        now = time.time()
        self.settings("n1 n2", FLEET_INTERVAL_S="60")
        self.state("n1", ok=True, ok_epoch=now - 10, attempt_epoch=now - 10, tiers={},
                   file_hosts=["login01"])
        doc = json.loads(self.run_pull("status", "--json", code=1))
        self.assertEqual(doc["stale_after_s"], 180)
        rows = {r["host"]: r for r in doc["nodes"]}
        self.assertEqual((rows["n1"]["state"], rows["n1"]["ok"], rows["n1"]["file_hosts"]),
                         ("ok", True, ["login01"]))
        self.assertEqual((rows["n2"]["state"], rows["n2"]["ok"]), ("never", False))

    def test_the_state_file_says_what_the_dashboard_needs(self):
        """The dashboard reads these files (RC_DASH_TRANSPORT_FILES): which copy, how
        often, and the pass."""
        self.settings("n1")
        self.run_pull("once", code=1)
        with open(os.path.join(self.mirror, ".state", "n1.json")) as fh:
            st = json.load(fh)
        self.assertEqual((st["host"], st["kind"], st["interval_s"], st["ok"]), ("n1", "rsync", 5, False))
        self.assertIn("rsync exit 12", st["error"])
        self.assertEqual(st["file_hosts"], [])
        for k in ("attempt_epoch", "ok_epoch", "target"):
            self.assertIn(k, st)

    def test_ages(self):
        self.assertEqual([pull.fmt_age(x) for x in (0, 42, 303, 11520, 266400)],
                         ["0s", "42s", "5m 3s", "3h 12m", "3d 2h"])


class TestReload(StubRig):
    def test_sighup_starts_and_stops_nodes(self):
        """A node added to fleet.env starts, and one taken out stops, with no restart of
        the loop: `systemctl reload rc-pull`."""
        self.settings("n1")
        p = subprocess.Popen([sys.executable, SCRIPT, "--config", self.conf, "loop"],
                             env=dict(os.environ, RSYNC_LOG=self.rsync_log),
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             universal_newlines=True)
        state_dir = os.path.join(self.mirror, ".state")

        def wait_for(path, newer_than=0):
            deadline = time.time() + 30
            while not (os.path.exists(path) and os.stat(path).st_mtime > newer_than):
                self.assertLess(time.time(), deadline, "%s never written" % path)
                time.sleep(0.1)

        try:
            wait_for(os.path.join(state_dir, "n1.json"))
            self.settings("n1 n2")
            p.send_signal(signal.SIGHUP)
            wait_for(os.path.join(state_dir, "n2.json"))
            self.settings("n2")
            p.send_signal(signal.SIGHUP)
            time.sleep(1.5)
            t_gone = os.stat(os.path.join(state_dir, "n1.json")).st_mtime
            wait_for(os.path.join(state_dir, "n2.json"), newer_than=time.time())
            self.assertEqual(os.stat(os.path.join(state_dir, "n1.json")).st_mtime, t_gone,
                             "n1 was taken out and still passes")
            # a file that does not load keeps every node as it was
            with open(self.conf, "a") as fh:
                fh.write("FLEET_HOST=typo\n")
            p.send_signal(signal.SIGHUP)
            time.sleep(1)
            self.assertIsNone(p.poll(), "a bad reload ended the loop")
            p.send_signal(signal.SIGTERM)
            out, _ = p.communicate(timeout=30)
        finally:
            if p.poll() is None:
                p.kill()
                p.communicate()
        self.assertEqual(p.returncode, 0, out)
        self.assertIn("1 node(s) | +n2", out.replace("reloaded %s: 2 node(s) | +n2" % self.conf,
                                                     "1 node(s) | +n2"))
        self.assertIn("-n1", out)
        self.assertIn("every node stays as it was", out)
        self.assertIn("did you mean FLEET_HOSTS", out)

    def test_sighup_with_no_node_left_retires_them_all(self):
        """The last node taken out is a reload like any other: it stops being pulled
        (kept as "a broken file", it went on being copied), and the loop stays up for
        the reload that names a node again."""
        self.settings("n1")
        p = subprocess.Popen([sys.executable, SCRIPT, "--config", self.conf, "loop"],
                             env=dict(os.environ, RSYNC_LOG=self.rsync_log),
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             universal_newlines=True)
        n1 = os.path.join(self.mirror, ".state", "n1.json")

        def wait_for(path, newer_than=0):
            deadline = time.time() + 30
            while not (os.path.exists(path) and os.stat(path).st_mtime > newer_than):
                self.assertLess(time.time(), deadline, "%s never written" % path)
                time.sleep(0.1)

        try:
            wait_for(n1)
            self.settings("")
            p.send_signal(signal.SIGHUP)
            time.sleep(1.5)
            t_gone = os.stat(n1).st_mtime
            time.sleep(5 + 2)   # one interval (the rig's FLEET_INTERVAL_S) and then some
            self.assertEqual(os.stat(n1).st_mtime, t_gone, "n1 was taken out and still passes")
            self.assertIsNone(p.poll(), "the loop ended with no node left")
            self.settings("n1")
            p.send_signal(signal.SIGHUP)
            wait_for(n1, newer_than=t_gone)
            p.send_signal(signal.SIGTERM)
            out, _ = p.communicate(timeout=30)
        finally:
            if p.poll() is None:
                p.kill()
                p.communicate()
        self.assertEqual(p.returncode, 0, out)
        self.assertIn("every node retired, and rc-pull waits for one to be named", out)


class TestAliases(unittest.TestCase):
    def load(self, text):
        with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as fh:
            fh.write(text)
        try:
            return pull.load(fh.name)
        finally:
            os.unlink(fh.name)

    def test_the_name_every_kit_uses(self):
        self.assertEqual(self.load("FLEET_HOSTS=a\nFREE_MIN_GB=5\n").free_min, 5 * (1 << 30))
        self.assertEqual(self.load("FLEET_HOSTS=a\nFREE_MIN_GB=5\nFLEET_FREE_MIN_GB=5\n").free_min,
                         5 * (1 << 30))
        with self.assertRaises(pull.ConfigError) as cm:
            self.load("FLEET_HOSTS=a\nFREE_MIN_GB=5\nFLEET_FREE_MIN_GB=7\n")
        self.assertIn("settle on one", str(cm.exception))

    def test_entries(self):
        s = self.load('FLEET_HOSTS="a b=10.0.0.2:/scratch/ebpfm c:ebpfm: d=root@[fe80::1]:x:y"\n'
                      "FLEET_USER=pull\n")
        self.assertEqual(s.hosts, {"a": "pull@a", "b": "pull@10.0.0.2", "c": "pull@c",
                                   "d": "root@[fe80::1]"})
        self.assertEqual(s.sources_for("a"), {"ebpf": "/var/log/ebpfm", "node": "/var/log/ebpfm-login"})
        self.assertEqual(s.sources_for("b"), {"ebpf": "/scratch/ebpfm", "node": "/var/log/ebpfm-login"})
        self.assertEqual(s.sources_for("c"), {"ebpf": "ebpfm"})
        self.assertEqual(s.sources_for("d"), {"ebpf": "x", "node": "y"})
        for text, why in (('FLEET_HOSTS="b=host:"\n', "no eBPF-tier folder"),
                          ('FLEET_HOSTS="b=host:-x"\n', "no spaces"),
                          ('FLEET_HOSTS="b=:x"\n', "not NAME"),
                          ('FLEET_HOSTS="b=h:x:y:z"\n', "not NAME")):
            with self.assertRaises(pull.ConfigError) as cm:
                self.load(text)
            self.assertIn(why, str(cm.exception), text)

    def test_no_node_is_its_own_error(self):
        with self.assertRaises(pull.NoNodes):
            self.load("FLEET_HOSTS=\n")


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
