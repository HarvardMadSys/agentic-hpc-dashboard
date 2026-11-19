"""A receiver with `keep_gz`: sealed days stay in the copy as gzip files, the way the
collector leaves them on its own disk, and a checked `.gz` in the copy counts as held,
so no sender sends that day again."""
import errno
import os
import shutil
import stat
import sys
import tempfile
import time
import unittest
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
FLEET = os.path.dirname(HERE)
sys.path.insert(0, FLEET)
sys.path.insert(0, HERE)

import fleet_stream as fs                                     # noqa: E402
from fakecollector import FakeCollector                       # noqa: E402
from test_receiver import HOST, Base, Rig, read, source_tree  # noqa: E402
from test_safety import ReceiverCase, wait_for                # noqa: E402

EXITS = "nodeA.cluster.example.org.exits.jsonl"
SNAP = "nodeA.cluster.example.org.snapshot.jsonl"


def names_in(mdir):
    """{day: sorted file names} of a host's copy."""
    return {d: sorted(os.listdir(os.path.join(mdir, d))) for d in sorted(os.listdir(mdir))}


def staged(basedir):
    return [n for _, _, files in os.walk(os.path.join(basedir, "staging")) for n in files]


class KeepGz(Base):
    def setUp(self):
        self.rig = Rig(keep_gz=True)

    def wait_same(self, timeout=15.0):
        """The copy holds the source's plain bytes, whichever form each file has."""
        end = time.time() + timeout
        while time.time() < end and source_tree(self.rig.mdir) != source_tree(self.rig.src):
            time.sleep(0.05)
        self.assertEqual(source_tree(self.rig.mdir), source_tree(self.rig.src))

    def sealed_day(self):
        """2026-11-10 sealed and gzipped at the source, 2026-11-11 live."""
        c = FakeCollector(self.rig.src, day="2026-11-10")
        c.append("exits", b'{"x":1}\n' * 5000)
        c.append("snapshot", b'{"s":1}\n' * 50)
        c.roll("2026-11-11", gzip=True)
        c.append("exits", b'{"y":1}\n')
        return c

    def both_verified(self, c, day):
        return lambda: (c.key("exits", day) in self.rig.mirror.verified
                        and c.key("snapshot", day) in self.rig.mirror.verified)

    def test_fetched_day_stays_gz(self):
        """A sealed day the copy lacks crosses as the collector's `.gz` and stays one."""
        c = self.sealed_day()
        src_gz = read(os.path.join(self.rig.src, "2026-11-10", EXITS + ".gz"))
        self.rig.start()
        self.wait_for(self.both_verified(c, "2026-11-10"), what="the sealed day verified")
        self.wait_same()
        self.assertEqual(names_in(self.rig.mdir)["2026-11-10"], [EXITS + ".gz", SNAP + ".gz"])
        self.assertEqual(read(os.path.join(self.rig.mdir, "2026-11-10", EXITS + ".gz")), src_gz)
        self.assertEqual(staged(self.rig.basedir), [])
        for f in names_in(self.rig.mdir)["2026-11-10"]:
            mode = os.stat(os.path.join(self.rig.mdir, "2026-11-10", f)).st_mode
            self.assertEqual(stat.S_IMODE(mode), 0o600, f)

    def test_sealed_live_day_is_gzipped_in_the_copy(self):
        """The live day streams plain. Once the source's trailer checks out, the copy
        gzips it and drops the plain file, as the collector does at the source; no
        `.gz` crosses the network for it."""
        c = FakeCollector(self.rig.src, day="2026-11-10")
        c.append("exits", b'{"d":1}\n' * 3000)
        self.rig.start()
        self.wait_same()
        c.roll("2026-11-11", gzip=True)
        c.append("exits", b'{"n":1}\n')
        self.wait_for(self.both_verified(c, "2026-11-10"), what="the old day verified")
        self.wait_same()
        days = names_in(self.rig.mdir)
        self.assertEqual(days["2026-11-10"], [EXITS + ".gz", SNAP + ".gz"])
        self.assertEqual(days["2026-11-11"], [EXITS, SNAP])
        self.assertEqual(self.rig.counters.total(HOST, "gz"), 0)
        self.assertEqual(staged(self.rig.basedir), [])

    def test_a_gz_in_the_copy_counts_as_held(self):
        """After a restart the hello lists the kept days as verified, so the sender
        sends none of them again."""
        c = self.sealed_day()
        self.rig.start()
        self.wait_for(self.both_verified(c, "2026-11-10"), what="the sealed day verified")
        self.wait_same()
        self.rig.halt()
        gz_before = self.rig.counters.total(HOST, "gz")
        have = fs.Mirror(self.rig.base, HOST).inventory()
        self.assertEqual(have[c.key("exits", "2026-11-10")], {"verified": True})
        self.assertNotIn(c.key("exits", "2026-11-10") + ".gz", have)
        c.append("exits", b'{"z":1}\n' * 10)
        self.rig.start()
        self.wait_same()
        self.assertEqual(self.rig.counters.total(HOST, "gz"), gz_before)

    def test_an_unlisted_gz_in_the_copy_is_fetched_again(self):
        """A `.gz` the ledger does not list (the receiver stopped between putting it in
        place and listing it) is not held: the day is fetched again over it."""
        c = self.sealed_day()
        day = os.path.join(self.rig.mdir, "2026-11-10")
        os.makedirs(day, 0o700)
        os.chmod(os.path.dirname(self.rig.mdir), 0o700)
        os.chmod(self.rig.mdir, 0o700)
        with open(os.path.join(day, EXITS + ".gz"), "wb") as fh:
            fh.write(b"not what the source holds")
        self.rig.start()
        self.wait_for(self.both_verified(c, "2026-11-10"), what="the day fetched again")
        self.wait_same()
        self.assertEqual(read(os.path.join(day, EXITS + ".gz")),
                         read(os.path.join(self.rig.src, "2026-11-10", EXITS + ".gz")))

    def test_a_damaged_copy_is_dropped_and_the_day_kept_as_gz(self):
        """The trailer check fails on a damaged plain copy: it is dropped, the day comes
        again as the collector's `.gz`, and that stays in the copy."""
        c = FakeCollector(self.rig.src, day="2026-11-10")
        c.append("exits", b'{"k":1}\n' * 200)
        old_key = c.key("exits")
        c.roll("2026-11-11", gzip=False)
        self.rig.start()
        self.wait_same()
        with open(os.path.join(self.rig.mdir, old_key), "r+b") as fh:
            fh.seek(3)
            fh.write(b"X")
        c.gzip_day("2026-11-10")
        self.wait_for(lambda: old_key in self.rig.mirror.verified, what="the refetched day verified")
        self.wait_same()
        self.assertIn(EXITS + ".gz", names_in(self.rig.mdir)["2026-11-10"])
        self.assertNotIn(EXITS, names_in(self.rig.mdir)["2026-11-10"])
        self.assertGreater(self.rig.counters.total(HOST, "resent"), 0)


class TestNoSpaceWhileGzipping(unittest.TestCase):
    def setUp(self):
        self.basedir = tempfile.mkdtemp(prefix="fleet-base-")
        os.chmod(self.basedir, 0o700)
        self.base = fs.SafeBase.open(self.basedir)

    def tearDown(self):
        self.base.close()
        shutil.rmtree(self.basedir, ignore_errors=True)

    def test_the_checked_plain_copy_stays(self):
        """ENOSPC while the copy gzips a sealed day: the partial `.gz` goes, the plain
        copy stays as it was, and the day is not listed, so the next connection
        checks it again."""
        def write(fd, data):
            raise OSError(errno.ENOSPC, "No space left on device")
        m = fs.Mirror(self.base, HOST, write=write, keep_gz=True)
        body = b'{"z":1}\n' * 1000
        rel = m.rel("2026-11-10", EXITS)
        self.base.makedirs("mirror/%s/2026-11-10" % HOST)
        fd = self.base.open_new(rel)
        os.write(fd, body)
        os.close(fd)
        h = {"t": "trailer", "path": "2026-11-10/" + EXITS,
             "crc": zlib.crc32(body) & 0xffffffff, "isize": len(body)}
        with self.assertRaises(fs.DiskFull):
            m.apply(h, b"", None)
        self.assertEqual(self.base.read_all(rel), body)
        self.assertEqual(self.base.listdir("mirror/%s/2026-11-10" % HOST), [EXITS])
        self.assertEqual(staged(self.basedir), [])
        self.assertNotIn("2026-11-10/" + EXITS, m.verified)


class TestConfig(ReceiverCase):
    def test_keep_gz_comes_from_the_config(self):
        c = FakeCollector(self.src, day="2026-11-10")
        c.append("exits", b'{"c":1}\n' * 100)
        c.roll("2026-11-11", gzip=True)
        c.append("exits", b'{"c":2}\n')
        self.start(cfg_extra={"keep_gz": True})
        day = os.path.join(self.mdir, "2026-11-10")
        self.assertTrue(wait_for(lambda: os.path.isdir(day)
                                 and sorted(os.listdir(day)) == [EXITS + ".gz", SNAP + ".gz"]))


if __name__ == "__main__":
    unittest.main()
