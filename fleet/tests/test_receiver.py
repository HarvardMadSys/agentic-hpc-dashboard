"""The receiver end to end: a real sender process feeding a Link and its Mirror.

Each test writes a source tree with FakeCollector, runs a Link whose transport is
`fleet_stream.py send` over that tree (no ssh), and checks the copy under the base
folder against the source: plain bytes for plain files, inflated bytes for days the
source holds only as `.gz`.
"""
import gzip
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
FLEET = os.path.dirname(HERE)
sys.path.insert(0, FLEET)
sys.path.insert(0, HERE)

import fleet_stream as fs                      # noqa: E402
from fakecollector import FakeCollector        # noqa: E402

SCRIPT = os.path.join(FLEET, "fleet_stream.py")
HOST = "nodeA"


def read(p):
    with open(p, "rb") as fh:
        return fh.read()


def source_tree(src):
    """{<day>/<file>: plain bytes} of every `.jsonl` day-file: a plain file as it is, and
    a `.gz` whose plain file is gone inflated."""
    out = {}
    for day in sorted(os.listdir(src)):
        folder = os.path.join(src, day)
        for name in sorted(os.listdir(folder)):
            p = os.path.join(folder, name)
            if name.endswith(".jsonl"):
                out["%s/%s" % (day, name)] = read(p)
            elif name.endswith(".jsonl.gz") and not os.path.exists(p[:-3]):
                out["%s/%s" % (day, name[:-3])] = gzip.decompress(read(p))
    return out


def mirror_tree(mdir):
    out = {}
    if not os.path.isdir(mdir):
        return out
    for day in sorted(os.listdir(mdir)):
        for name in sorted(os.listdir(os.path.join(mdir, day))):
            out["%s/%s" % (day, name)] = read(os.path.join(mdir, day, name))
    return out


class Rig:
    """A source tree, a base folder, and one Link at a time between them."""

    def __init__(self, days=3, backoff=(0.2, 1.0), root=None, keep_gz=False):
        self.src = tempfile.mkdtemp(prefix="fleet-src-")
        self.basedir = tempfile.mkdtemp(prefix="fleet-base-")
        os.chmod(self.basedir, 0o700)
        self.base = fs.SafeBase.open(self.basedir)
        self.counters = fs.Counters()
        self.days, self.backoff, self.keep_gz = days, backoff, keep_gz
        self.root = root or self.src
        self.link = self.thread = None
        self.mirror = None

    @property
    def mdir(self):
        return os.path.join(self.basedir, "mirror", HOST)

    def argv(self):
        return [sys.executable, SCRIPT, "send", "--root", self.root,
                "--poll", "0.05", "--ping", "0.5", "--trailer-poll", "0.1"]

    def start(self, write=None):
        """A fresh Mirror and Link, as a restarted receiver would make."""
        kw = {"write": write} if write else {}
        if self.keep_gz:
            kw["keep_gz"] = True
        self.mirror = fs.Mirror(self.base, HOST, **kw)
        self.paused, self.stop = threading.Event(), threading.Event()
        self.link = fs.Link(HOST, self.argv(), self.mirror, self.days, self.counters,
                            self.paused, self.stop, backoff=self.backoff)
        self.thread = threading.Thread(target=self.link.run, daemon=True)
        self.thread.start()

    def halt(self):
        if self.link is not None:
            self.stop.set()
            self.link.close()
            self.thread.join(10)
            self.link = None

    def cleanup(self):
        self.halt()
        shutil.rmtree(self.src, ignore_errors=True)
        shutil.rmtree(self.basedir, ignore_errors=True)


class Base(unittest.TestCase):
    def setUp(self):
        self.rig = Rig()

    def tearDown(self):
        self.rig.cleanup()

    def wait_same(self, timeout=15.0):
        end = time.time() + timeout
        while time.time() < end:
            if mirror_tree(self.rig.mdir) == source_tree(self.rig.src):
                return
            time.sleep(0.05)
        self.assertEqual(sorted(mirror_tree(self.rig.mdir)), sorted(source_tree(self.rig.src)))
        self.assertEqual(mirror_tree(self.rig.mdir), source_tree(self.rig.src))

    def wait_for(self, pred, timeout=15.0, what="condition"):
        end = time.time() + timeout
        while time.time() < end:
            if pred():
                return
            time.sleep(0.05)
        self.fail("timed out waiting for %s" % what)


class TestReceiver(Base):
    def test_copy_equals_source_live(self):
        c = FakeCollector(self.rig.src)
        self.rig.start()
        for i in range(5):
            c.append("exits", b'{"e":%d}\n' % i)
            c.append("snapshot", b'{"s":%d}\n' % i)
            time.sleep(0.2)
        self.wait_same()

    def test_receiver_restart_resumes(self):
        c = FakeCollector(self.rig.src)
        c.append("exits", b'{"a":1}\n' * 1000)
        self.rig.start()
        self.wait_same()
        self.rig.halt()
        c.append("exits", b'{"b":2}\n' * 1000)
        c.append("snapshot", b'{"c":3}\n' * 10)
        self.rig.start()
        self.wait_same()
        total = sum(len(v) for v in source_tree(self.rig.src).values())
        self.assertEqual(self.rig.counters.total(HOST, "plain"), total)

    def test_sender_killed_reconnects(self):
        c = FakeCollector(self.rig.src)
        c.append("exits", b'{"a":1}\n')
        self.rig.start()
        self.wait_same()
        self.rig.link.proc.kill()
        c.append("exits", b'{"after":1}\n')
        self.wait_same()
        self.assertGreaterEqual(len(self.rig.link.attempts), 2)

    def test_down_across_midnight_plain_present(self):
        c = FakeCollector(self.rig.src, day="2026-11-10")
        c.append("exits", b'{"d":1}\n' * 100)
        self.rig.start()
        self.wait_same()
        self.rig.halt()
        c.append("exits", b'{"d":2}\n' * 100)
        old_key = c.key("exits")
        c.roll("2026-11-11", gzip="thread", delay=1.5)
        c.append("exits", b'{"n":1}\n')
        self.rig.start()
        self.wait_same()
        c.wait()
        self.wait_for(lambda: old_key in self.rig.mirror.verified, what="the old day verified")
        self.wait_same()

    def test_down_across_midnight_plain_gone(self):
        c = FakeCollector(self.rig.src, day="2026-11-10")
        c.append("exits", b'{"d":1}\n' * 100)
        self.rig.start()
        self.wait_same()
        self.rig.halt()
        c.append("exits", b'{"d":2}\n' * 100)
        old_key = c.key("exits")
        c.roll("2026-11-11", gzip=True)
        c.append("exits", b'{"n":1}\n')
        self.rig.start()
        self.wait_same()
        self.wait_for(lambda: old_key in self.rig.mirror.verified, what="the old day verified")

    def test_missing_day_staged_then_renamed(self):
        c = FakeCollector(self.rig.src, day="2026-11-10")
        c.append("exits", b'{"x":1}\n' * 500)
        c.roll("2026-11-11", gzip=True)
        c.append("exits", b'{"y":1}\n')
        self.rig.start()
        self.wait_same()
        names = [n for _, _, files in os.walk(self.rig.mdir) for n in files]
        self.assertTrue(names)
        self.assertTrue(all(n.endswith(".jsonl") for n in names), names)
        staged = [n for _, _, files in os.walk(os.path.join(self.rig.basedir, "staging")) for n in files]
        self.assertEqual(staged, [])

    def test_crc_mismatch_refetches(self):
        c = FakeCollector(self.rig.src, day="2026-11-10")
        body = b'{"k":1}\n' * 200
        c.append("exits", body)
        old_key = c.key("exits")
        c.roll("2026-11-11", gzip=False)
        self.rig.start()
        self.wait_same()
        copy = os.path.join(self.rig.mdir, old_key)
        with open(copy, "r+b") as fh:
            fh.seek(3)
            fh.write(b"X")
        c.gzip_day("2026-11-10")
        self.wait_for(lambda: old_key in self.rig.mirror.verified, what="the refetched day verified")
        self.wait_same()
        self.assertGreater(self.rig.counters.total(HOST, "resent"), 0)

    def test_traffic_lines(self):
        c = FakeCollector(self.rig.src)
        c.append("exits", b'{"t":1}\n' * 100)
        self.rig.start()
        self.wait_same()
        self.rig.counters.flush(self.rig.base)
        lines = [json.loads(x) for x in read(os.path.join(self.rig.basedir, "state", "traffic.jsonl")).splitlines()]
        mine = [x for x in lines if x["host"] == HOST]
        self.assertTrue(mine)
        for k in ("hour", "wire", "plain", "gz", "resent"):
            self.assertIn(k, mine[0])
        self.assertEqual(sum(x["plain"] for x in mine), 800)
        self.assertGreater(sum(x["wire"] for x in mine), 0)

    def test_reset_drops_copy(self):
        c = FakeCollector(self.rig.src)
        c.append("exits", b"AAAAAAAAAA\n")
        self.rig.start()
        self.wait_same()
        c.replace("exits", b"BB\n")
        self.wait_same()
        self.assertEqual(read(os.path.join(self.rig.mdir, c.key("exits"))), b"BB\n")


class TestReplacedWhileDown(Base):
    def test_replaced_while_down_resets(self):
        """The review's case: a new inode that grew past the copy while the link was down."""
        c = FakeCollector(self.rig.src)
        c.append("exits", b"A" * 999 + b"\n")
        self.rig.start()
        self.wait_same()
        self.rig.halt()
        c.replace("exits", b"B" * 1499 + b"\n")
        self.rig.start()
        self.wait_same()
        self.assertEqual(read(os.path.join(self.rig.mdir, c.key("exits"))), b"B" * 1499 + b"\n")

    def test_inventory_carries_head(self):
        c = FakeCollector(self.rig.src)
        c.append("exits", b"x" * 70000)
        self.rig.start()
        self.wait_same()
        have = self.rig.mirror.inventory(3)[c.key("exits")]
        self.assertEqual(have["head_n"], 65536)
        self.assertEqual(have["head"], zlib.crc32(b"x" * 65536) & 0xffffffff)


class TestBackoff(unittest.TestCase):
    def test_error_frame_backs_off(self):
        rig = Rig(backoff=(0.2, 1.0), root="/nonexistent/fleet-src")
        try:
            rig.start()
            end = time.time() + 10
            while time.time() < end and len(rig.link.attempts) < 2:
                time.sleep(0.05)
            self.assertGreaterEqual(len(rig.link.attempts), 2)
            self.assertIn("cannot read", rig.link.last_error or "")
            self.assertGreaterEqual(rig.link.attempts[1] - rig.link.attempts[0], 0.2)
            self.assertFalse(rig.link.status()["connected"])
            self.assertLessEqual(rig.link.status()["down_since"], time.time())
        finally:
            rig.cleanup()

    def test_ssh_error_reaches_last_error(self):
        rig = Rig(backoff=(0.2, 1.0))
        try:
            rig.argv = lambda: [sys.executable, "-c",
                                "import sys; sys.stderr.write('Host key verification failed.\\n'); sys.exit(255)"]
            rig.start()
            end = time.time() + 10
            while time.time() < end and "Host key" not in (rig.link.last_error or ""):
                time.sleep(0.05)
            self.assertIn("Host key verification failed", rig.link.last_error or "")
        finally:
            rig.cleanup()

    def test_connected_link_is_not_down(self):
        rig = Rig()
        try:
            FakeCollector(rig.src)
            rig.start()
            end = time.time() + 10
            while time.time() < end and not rig.link.status()["connected"]:
                time.sleep(0.05)
            st = rig.link.status()
            self.assertTrue(st["connected"])
            self.assertIsNone(st["down_since"])
        finally:
            rig.cleanup()


if __name__ == "__main__":
    unittest.main()
