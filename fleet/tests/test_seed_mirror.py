"""seed_mirror.py and pull-gz.sh: a copy built ahead of time from pulled `.gz` days,
which a real sender then leaves alone, sending only the live day's plain bytes."""
import fnmatch
import gzip
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
FLEET = os.path.dirname(HERE)
sys.path.insert(0, FLEET)
sys.path.insert(0, HERE)

import fleet_stream as fs                                         # noqa: E402
import seed_mirror as sm                                          # noqa: E402
from fakecollector import FakeCollector                           # noqa: E402
from test_receiver import HOST, Rig, mirror_tree, read, source_tree   # noqa: E402

SEED = os.path.join(FLEET, "seed_mirror.py")
PULL = os.path.join(FLEET, "pull-gz.sh")
EXITS = "nodeA.cluster.example.org.exits.jsonl"
SNAP = "nodeA.cluster.example.org.snapshot.jsonl"


def pull_patterns():
    """The file patterns pull-gz.sh's rsync takes: its `--include='<pattern>'`s but the
    one for folders, `*/`."""
    with open(PULL) as fh:
        return [p for p in re.findall(r"--include='([^']*)'", fh.read()) if p != "*/"]


def pull_like_pull_gz(src, pull, host=HOST):
    """What pull-gz.sh's `rsync -rlt --include='*/' <its patterns> --exclude='*'` brings
    from a node's /scratch/ebpfm: its sealed days only, under `<pull>/<host>/`."""
    pats = pull_patterns()
    for day in sorted(os.listdir(src)):
        for name in sorted(os.listdir(os.path.join(src, day))):
            if any(fnmatch.fnmatchcase(name, p) for p in pats):
                os.makedirs(os.path.join(pull, host, day), exist_ok=True)
                shutil.copy2(os.path.join(src, day, name), os.path.join(pull, host, day, name))


class Seeded(unittest.TestCase):
    """Two sealed days and a live one at the source, the sealed ones pulled."""

    def setUp(self):
        self.rig = Rig(days=3)
        self.pull = tempfile.mkdtemp(prefix="fleet-pull-")
        c = FakeCollector(self.rig.src, day="2026-11-09")
        c.append("exits", b'{"d":9}\n' * 5000)
        c.append("snapshot", b'{"s":9}\n' * 50)
        c.roll("2026-11-10")
        c.append("exits", b'{"d":10}\n' * 5000)
        c.append("snapshot", b'{"s":10}\n' * 50)
        c.roll("2026-11-11")
        c.append("exits", b'{"live":1}\n' * 300)
        self.c = c
        pull_like_pull_gz(self.rig.src, self.pull)

    def tearDown(self):
        self.rig.cleanup()
        shutil.rmtree(self.pull, ignore_errors=True)

    def seed(self, *extra):
        return subprocess.run([sys.executable, SEED, self.pull, self.rig.basedir] + list(extra),
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              universal_newlines=True)

    def sealed(self):
        """The sealed days' plain bytes, as the copy must hold them."""
        return {k: v for k, v in source_tree(self.rig.src).items() if not k.startswith("2026-11-11/")}

    def ledger(self):
        with open(os.path.join(self.rig.basedir, "state", HOST + ".verified.json")) as fh:
            return json.load(fh)

    def wait_same(self, timeout=15.0):
        """The copy holds the source's plain bytes, whichever form each file has."""
        end = time.time() + timeout
        while time.time() < end and source_tree(self.rig.mdir) != source_tree(self.rig.src):
            time.sleep(0.05)
        self.assertEqual(source_tree(self.rig.mdir), source_tree(self.rig.src))

    def private_files(self):
        for root, dirs, files in os.walk(self.rig.basedir):
            for d in dirs:
                self.assertEqual(stat.S_IMODE(os.stat(os.path.join(root, d)).st_mode), 0o700, d)
            for f in files:
                self.assertEqual(stat.S_IMODE(os.stat(os.path.join(root, f)).st_mode), 0o600, f)


class TestSeed(Seeded):
    def test_receiver_streams_only_the_live_day(self):
        """The point of seeding: no `.gz` crosses, only the live day's plain bytes."""
        p = self.seed()
        self.assertEqual(p.returncode, 0, p.stdout)
        self.rig.start()
        self.wait_same()
        self.assertEqual(self.rig.counters.total(HOST, "gz"), 0)
        live = len(self.c.read("exits")) + len(self.c.read("snapshot"))
        self.assertEqual(self.rig.counters.total(HOST, "plain"), live)

    def test_copy_holds_the_pulled_gz(self):
        """Each sealed day goes into the copy as the pulled `.gz`, byte for byte, as a
        `keep_gz` receiver keeps a day it fetches."""
        p = self.seed()
        self.assertEqual(p.returncode, 0, p.stdout)
        self.assertEqual(source_tree(self.rig.mdir), self.sealed())
        self.assertEqual(self.ledger(), sorted(self.sealed()))
        self.assertEqual(sorted(mirror_tree(self.rig.mdir)), sorted(k + ".gz" for k in self.sealed()))
        for key in self.sealed():
            self.assertEqual(read(os.path.join(self.rig.mdir, key + ".gz")),
                             read(os.path.join(self.pull, HOST, key + ".gz")))
        self.private_files()

    def test_inflate_writes_plain_files(self):
        """`--inflate` builds the copy a receiver without `keep_gz` makes."""
        p = self.seed("--inflate")
        self.assertEqual(p.returncode, 0, p.stdout)
        self.assertEqual(mirror_tree(self.rig.mdir), self.sealed())
        self.assertEqual(self.ledger(), sorted(self.sealed()))
        self.private_files()

    def test_a_seeded_gz_replaces_a_partial_plain_copy(self):
        """A day a receiver began as plain while it was live, and the pull now holds
        sealed: the `.gz` goes in and the partial plain file goes, or the dashboard,
        which reads a plain file before its `.gz` twin, would read the partial one."""
        day = os.path.join(self.rig.mdir, "2026-11-10")
        os.makedirs(day, 0o700)
        os.chmod(os.path.dirname(self.rig.mdir), 0o700)
        os.chmod(self.rig.mdir, 0o700)
        with open(os.path.join(day, EXITS), "wb") as fh:
            fh.write(b'{"d":10}\n' * 10)
        p = self.seed()
        self.assertEqual(p.returncode, 0, p.stdout)
        self.assertEqual(sorted(os.listdir(day)), [EXITS + ".gz", SNAP + ".gz"])
        self.assertEqual(source_tree(self.rig.mdir), self.sealed())

    def test_corrupt_gz_is_left_for_the_stream(self):
        """A `.gz` cut short (pulled while gzip wrote it) is not in the copy, so the
        sender sends that one day itself."""
        bad = os.path.join(self.pull, HOST, "2026-11-10", "nodeA.cluster.example.org.exits.jsonl.gz")
        with open(bad, "r+b") as fh:
            fh.truncate(os.path.getsize(bad) // 2)
        p = self.seed()
        self.assertEqual(p.returncode, 1, p.stdout)
        self.assertIn("2026-11-10/nodeA.cluster.example.org.exits.jsonl", p.stdout)
        self.assertNotIn("2026-11-10/nodeA.cluster.example.org.exits.jsonl", self.ledger())
        self.assertNotIn("2026-11-10/nodeA.cluster.example.org.exits.jsonl", mirror_tree(self.rig.mdir))
        self.assertNotIn("2026-11-10/nodeA.cluster.example.org.exits.jsonl.gz", mirror_tree(self.rig.mdir))
        self.assertEqual([n for _, _, f in os.walk(os.path.join(self.rig.basedir, "staging")) for n in f], [])
        self.assertEqual(len(self.ledger()), 3)
        self.rig.start()
        self.wait_same()
        self.assertGreater(self.rig.counters.total(HOST, "gz"), 0)

    def test_rerun_keeps_what_is_there_and_adds_new_days(self):
        self.assertEqual(self.seed().returncode, 0)
        old = os.path.join(self.rig.mdir, "2026-11-09", "nodeA.cluster.example.org.exits.jsonl.gz")
        before = os.stat(old)
        self.c.append("snapshot", b'{"s":11}\n')
        self.c.roll("2026-11-12")
        pull_like_pull_gz(self.rig.src, self.pull)
        p = self.seed()
        self.assertEqual(p.returncode, 0, p.stdout)
        after = os.stat(old)
        self.assertEqual((before.st_ino, before.st_mtime), (after.st_ino, after.st_mtime))
        self.assertIn("2026-11-11/nodeA.cluster.example.org.exits.jsonl", self.ledger())
        self.assertIn("4 already in the copy", p.stdout)

    def test_says_how_many_days_the_receiver_must_keep(self):
        """The receiver keeps its `days` newest day folders: fewer drops seeded days."""
        p = self.seed()
        self.assertIn('"days" to at least 3', p.stdout)
        self.assertIn('"keep_gz": true', p.stdout)
        self.assertNotIn("keep_gz", self.seed("--inflate").stdout)

    def test_refuses_a_base_others_can_open(self):
        os.chmod(self.rig.basedir, 0o755)
        p = self.seed()
        self.assertEqual(p.returncode, 2, p.stdout)
        self.assertEqual(os.listdir(self.rig.basedir), [])

    def test_refuses_while_a_receiver_runs_on_the_base(self):
        os.makedirs(os.path.join(self.rig.basedir, "state"), mode=0o700)
        with open(os.path.join(self.rig.basedir, "state", "status.json"), "w") as fh:
            json.dump({"at_epoch": time.time()}, fh)
        p = self.seed()
        self.assertEqual(p.returncode, 2, p.stdout)
        self.assertIn("receiver", p.stdout)
        self.assertFalse(os.path.exists(os.path.join(self.rig.basedir, "mirror")))

    def test_makes_a_missing_base_private(self):
        base = os.path.join(self.rig.basedir, "new")
        p = subprocess.run([sys.executable, SEED, self.pull, base], stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, universal_newlines=True)
        self.assertEqual(p.returncode, 0, p.stdout)
        self.assertEqual(stat.S_IMODE(os.stat(base).st_mode), 0o700)

    def test_newest_days_first_and_stops_above_the_free_space_floor(self):
        """The receiver pauses every link below max(300 GB, 10%) free. Seeding stops
        before that, newest days first, so what is left for the stream is the oldest."""
        calls = []

        def statvfs():
            calls.append(1)
            free = 10**13 if len(calls) <= 2 else fs.FREE_MIN_BYTES - 1
            return free, 10**13
        out = []
        rc = sm.seed(self.pull, self.rig.basedir, statvfs=statvfs, say=out.append)
        self.assertEqual(rc, 1)
        self.assertEqual(self.ledger(), ["2026-11-10/nodeA.cluster.example.org.exits.jsonl",
                                         "2026-11-10/nodeA.cluster.example.org.snapshot.jsonl"])
        self.assertTrue(any("free" in line for line in out), out)

    def test_a_lower_floor_for_a_smaller_disk(self):
        """200 GB free of 1 TB is under the default floor (300 GB) but over the
        floor of a receiver whose config sets free_min_gb 50 (then 10%, 100 GB)."""
        def small():
            return 200e9, 1e12
        out = []
        self.assertEqual(sm.seed(self.pull, self.rig.basedir, statvfs=small, say=out.append), 1)
        self.assertFalse(os.path.exists(os.path.join(self.rig.basedir, "state", HOST + ".verified.json")))
        rc = sm.seed(self.pull, self.rig.basedir, statvfs=small, say=out.append, free_min_gb=50)
        self.assertEqual(rc, 0, out)
        self.assertEqual(len(self.ledger()), 4)


class TestPull(unittest.TestCase):
    """pull-gz.sh with rsync replaced by a stand-in that logs its arguments."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pull-gz-")
        self.bin = os.path.join(self.tmp, "bin")
        os.mkdir(self.bin)
        self.log = os.path.join(self.tmp, "calls.log")
        p = os.path.join(self.bin, "rsync")
        with open(p, "w") as fh:
            fh.write('#!/bin/bash\nprintf "%s\\n" "$*" >> "$STUB_LOG"\nexit "${STUB_RC:-0}"\n')
        os.chmod(p, 0o700)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def pull(self, dest, **env):
        e = dict(os.environ, PATH=self.bin + ":" + os.environ["PATH"], STUB_LOG=self.log)
        for k in ("SOURCE_USER", "SOURCE_NODES", "SOURCE_DOMAIN", "SOURCE_ROOT"):
            e.pop(k, None)
        e.update(env)
        return subprocess.run(["bash", PULL, dest], env=e, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, universal_newlines=True)

    def calls(self):
        with open(self.log) as fh:
            return fh.read().splitlines()

    def test_pulls_each_nodes_sealed_days(self):
        dest = os.path.join(self.tmp, "pull")
        p = self.pull(dest, SOURCE_USER="alice", SOURCE_NODES="nodeA nodeB",
                      SOURCE_DOMAIN="cluster.example.org")
        self.assertEqual(p.returncode, 0, p.stdout)
        calls = self.calls()
        self.assertEqual(len(calls), 2)
        for host, call in zip(("nodeA", "nodeB"), calls):
            self.assertIn("--include=*/ --include=*.jsonl.gz --exclude=*", call)
            self.assertIn("--prune-empty-dirs", call)
            self.assertTrue(call.endswith("alice@%s.cluster.example.org:/scratch/ebpfm/ %s/%s/"
                                          % (host, dest, host)), call)
        self.assertEqual(stat.S_IMODE(os.stat(dest).st_mode), 0o700)
        self.assertIn("seed_mirror.py", p.stdout)

    def test_no_domain_and_another_root(self):
        """With no SOURCE_DOMAIN a node is reached by its name as it is (an ssh alias,
        or a name the resolver completes), and SOURCE_ROOT moves the folder read."""
        dest = os.path.join(self.tmp, "pull")
        p = self.pull(dest, SOURCE_USER="alice", SOURCE_NODES="nodeA", SOURCE_ROOT="/var/log/ebpfm")
        self.assertEqual(p.returncode, 0, p.stdout)
        self.assertTrue(self.calls()[0].endswith("alice@nodeA:/var/log/ebpfm/ %s/nodeA/" % dest),
                        self.calls()[0])

    def test_needs_the_account_and_the_nodes(self):
        for env in ({}, {"SOURCE_USER": "alice"}, {"SOURCE_NODES": "nodeA"}):
            p = self.pull(os.path.join(self.tmp, "pull"), **env)
            self.assertEqual(p.returncode, 2, p.stdout)
            self.assertIn("SOURCE_USER", p.stdout)
            self.assertIn("SOURCE_NODES", p.stdout)
            self.assertFalse(os.path.exists(self.log))

    def test_a_failed_node_is_reported_and_the_rest_pulled(self):
        p = self.pull(os.path.join(self.tmp, "pull"), SOURCE_USER="alice", SOURCE_NODES="nodeA nodeB",
                      STUB_RC="12")
        self.assertEqual(p.returncode, 1, p.stdout)
        self.assertEqual(len(self.calls()), 2)
        self.assertIn("nodeA: FAILED", p.stdout)


if __name__ == "__main__":
    unittest.main()
