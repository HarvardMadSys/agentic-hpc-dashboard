"""EBR day-files travel as JSONL ones do.

The collector writes each stream as `<host>.<stream>.jsonl`, `.ebr` (its binary
records), or both, and gzips a sealed day's files in place. The stream copies bytes,
not records, so an `.ebr` file and its `.ebr.gz` go through the frames, checks, and
copies a `.jsonl` one does. These tests check the copy against the source byte for
byte, with real EBR bytes from the collector's own writer (collector/lib/ebr.py), and
read the copy back with the same module.
"""
import gzip
import os
import shutil
import sys
import tempfile
import time
import unittest
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
FLEET = os.path.dirname(HERE)
sys.path.insert(0, FLEET)
sys.path.insert(0, HERE)

import fleet_stream as fs                                                   # noqa: E402
from fakecollector import STREAMS, FakeCollector, ebr, ebr_bytes, records   # noqa: E402
from test_keep_gz import names_in, staged                                   # noqa: E402
from test_receiver import HOST, Base, Rig, read, source_tree                # noqa: E402

BOTH = ("jsonl", "ebr")
EXITS = "nodeA.cluster.example.org.exits.ebr"
SNAP = "nodeA.cluster.example.org.snapshot.ebr"
NO_EBR = "no collector/lib/ebr.py alongside fleet/"


def recs_of(path):
    """The records of a day-file, either format, plain or gzipped."""
    return [r for _, r in ebr.iter_records(path)]


@unittest.skipIf(ebr is None, NO_EBR)
class TestEbrStream(Base):
    """A receiver without keep_gz: the copy holds every file plain."""

    def test_an_ebr_day_in_two_writes_then_sealed(self):
        """Two writes of the collector's writer, the receiver restarted between them,
        then the day sealed and gzipped. The copy is the source byte for byte after each
        step, the second write crosses once, from where the copy ends, and the sealed
        day checks out against its `.ebr.gz` trailer."""
        c = FakeCollector(self.rig.src, day="2026-11-10", formats=("ebr",))
        key = c.key("exits", fmt="ebr")
        first, second = records(300), records(300, start=300)
        c.write_records("exits", first, "ebr")
        self.rig.start()
        self.wait_same()
        self.rig.halt()
        c.write_records("exits", second, "ebr")
        self.rig.start()
        self.wait_same()
        self.assertEqual(self.rig.counters.total(HOST, "plain"), len(c.read("exits", fmt="ebr")))
        c.roll("2026-11-11", gzip=True)
        self.wait_for(lambda: key in self.rig.mirror.verified, what="the sealed .ebr day verified")
        self.wait_same()
        self.assertEqual(recs_of(os.path.join(self.rig.mdir, key)), first + second)
        self.assertEqual(recs_of(c.path("exits", "2026-11-10", "ebr") + ".gz"), first + second)

    def test_a_frame_cut_across_two_writes(self):
        """A write the sender sees half done ends inside a frame. The copy then ends in
        that partial frame, as the source does, and reads as the source does: the frames
        before it, and nothing of it yet. The rest of the write completes it."""
        c = FakeCollector(self.rig.src, formats=("ebr",))
        first, second = records(250), records(250, start=250)
        blob = ebr_bytes([first, second], {"host": c.host, "stream": "exits"})
        cut = len(blob) - 100              # inside the second batch's frame
        c.append("exits", blob[:cut], fmt="ebr")
        self.rig.start()
        self.wait_same()
        copy = os.path.join(self.rig.mdir, c.key("exits", fmt="ebr"))
        self.assertLess(ebr.complete_end(copy), cut)
        self.assertEqual(recs_of(copy), first)
        c.append("exits", blob[cut:], fmt="ebr")
        self.wait_same()
        self.assertEqual(ebr.complete_end(copy), len(blob))
        self.assertEqual(recs_of(copy), first + second)

    def test_a_mixed_day_carries_both_twins(self):
        """EBPFM_FORMAT=both: each stream as a `.jsonl` and an `.ebr` twin. A sealed day
        the copy lacks crosses as the collector's `.gz` files; the live day streams both
        twins and, once sealed, checks both. The twins in the copy hold the same records."""
        c = FakeCollector(self.rig.src, day="2026-11-09", formats=BOTH)
        c.write_twins("exits", records(400))
        c.write_twins("snapshot", records(20, event="residency"))
        c.roll("2026-11-10", gzip=True)
        c.write_twins("exits", records(100))
        self.rig.start()
        self.wait_same()
        c.write_twins("exits", records(100, start=100))
        c.write_twins("snapshot", records(5, event="residency"))
        self.wait_same()
        days = ("2026-11-09", "2026-11-10")
        keys = [c.key(s, d, f) for d in days for s in STREAMS for f in BOTH]
        c.roll("2026-11-11", gzip=True)
        self.wait_for(lambda: all(k in self.rig.mirror.verified for k in keys),
                      what="both days' twins verified")
        self.wait_same()
        self.assertGreater(self.rig.counters.total(HOST, "gz"), 0)
        names = names_in(self.rig.mdir)
        for d in days:
            self.assertEqual(names[d], sorted(k.split("/", 1)[1] for k in keys if k.startswith(d)))
            for s in STREAMS:
                twins = [recs_of(os.path.join(self.rig.mdir, c.key(s, d, f))) for f in BOTH]
                self.assertTrue(twins[0])
                self.assertEqual(twins[0], twins[1])


@unittest.skipIf(ebr is None, NO_EBR)
class TestEbrKeepGz(Base):
    """A receiver with keep_gz: sealed days stay in the copy as `.gz`."""

    def setUp(self):
        self.rig = Rig(keep_gz=True)

    def wait_same(self, timeout=15.0):
        """The copy holds the source's plain bytes, whichever form each file has."""
        end = time.time() + timeout
        while time.time() < end and source_tree(self.rig.mdir) != source_tree(self.rig.src):
            time.sleep(0.05)
        self.assertEqual(source_tree(self.rig.mdir), source_tree(self.rig.src))

    def test_a_sealed_ebr_day_stays_the_collectors_gz(self):
        """A sealed day the copy lacks crosses as the collector's own `.ebr.gz`, resumed
        from the part a stopped fetch staged, and stays in the copy byte for byte. The
        live day's `.ebr` streams plain and, sealed and checked, is gzipped in the copy;
        no `.gz` crosses for it."""
        c = FakeCollector(self.rig.src, day="2026-11-09", formats=("ebr",))
        c.write_records("exits", records(3000), "ebr")
        c.write_records("snapshot", records(30, event="residency"), "ebr")
        c.roll("2026-11-10", gzip=True)
        c.write_records("exits", records(50), "ebr")
        exits_gz = read(c.path("exits", "2026-11-09", "ebr") + ".gz")
        snap_gz = read(c.path("snapshot", "2026-11-09", "ebr") + ".gz")
        half = len(exits_gz) // 2
        fd = self.rig.base.open_append("staging/%s/2026-11-09/%s.gz.part" % (HOST, EXITS))
        try:
            os.write(fd, exits_gz[:half])
        finally:
            os.close(fd)
        sealed = [c.key(s, "2026-11-09", "ebr") for s in STREAMS]
        self.rig.start()
        self.wait_for(lambda: all(k in self.rig.mirror.verified for k in sealed),
                      what="the sealed day verified")
        self.wait_same()
        day = os.path.join(self.rig.mdir, "2026-11-09")
        self.assertEqual(names_in(self.rig.mdir)["2026-11-09"], [EXITS + ".gz", SNAP + ".gz"])
        self.assertEqual(read(os.path.join(day, EXITS + ".gz")), exits_gz)
        self.assertEqual(read(os.path.join(day, SNAP + ".gz")), snap_gz)
        fetched = len(exits_gz) - half + len(snap_gz)
        self.assertEqual(self.rig.counters.total(HOST, "gz"), fetched)
        self.assertEqual(staged(self.rig.basedir), [])
        live = [c.key(s, fmt="ebr") for s in STREAMS]
        c.roll("2026-11-11", gzip=True)
        self.wait_for(lambda: all(k in self.rig.mirror.verified for k in live),
                      what="the live day verified")
        self.wait_same()
        self.assertEqual(names_in(self.rig.mdir)["2026-11-10"], [EXITS + ".gz", SNAP + ".gz"])
        self.assertEqual(self.rig.counters.total(HOST, "gz"), fetched)


@unittest.skipIf(ebr is None, NO_EBR)
class TestEbrNames(unittest.TestCase):
    """The receiver's names for EBR files: the paths it takes, and what its hello says
    the copy holds."""

    def setUp(self):
        self.basedir = tempfile.mkdtemp(prefix="fleet-base-")
        os.chmod(self.basedir, 0o700)
        self.base = fs.SafeBase.open(self.basedir)

    def tearDown(self):
        self.base.close()
        shutil.rmtree(self.basedir, ignore_errors=True)

    def put(self, rel, data):
        fd = self.base.open_append(rel)
        try:
            os.write(fd, data)
        finally:
            os.close(fd)

    def test_paths(self):
        for fmt in BOTH:
            name = "h.exits." + fmt
            self.assertEqual(fs.Mirror.parse("2026-11-10/" + name), ("2026-11-10", name, False))
            self.assertEqual(fs.Mirror.parse("2026-11-10/%s.gz" % name), ("2026-11-10", name, True))
        for bad in ("2026-11-10/h.exits.txt", "2026-11-10/h.exits.ebrx", "2026-11-10/h.exits.ebr.part",
                    "2026-11-10/h.exits.ebr.gz.gz", "2026-11-10/.ebr", "../2026-11-10/h.exits.ebr",
                    "h.exits.ebr"):
            with self.assertRaises(fs.ProtocolError):
                fs.Mirror.parse(bad)

    def test_inventory_lists_ebr_files_as_jsonl_ones(self):
        """The hello lists a plain `.ebr` by its size and head, a listed `.ebr.gz` as
        verified, and a staged `.ebr.gz.part` by its size, as it does `.jsonl` ones; any
        other name is left out."""
        m = fs.Mirror(self.base, HOST)
        bodies = {"jsonl": b'{"a":1}\n' * 10, "ebr": ebr_bytes([records(40)])}
        want = {}
        for fmt, body in sorted(bodies.items()):
            self.put(m.rel("2026-11-10", "h.exits." + fmt), body)
            self.put(m.rel("2026-11-09", "h.exits.%s.gz" % fmt), gzip.compress(body))
            m.mark_verified("2026-11-09/h.exits." + fmt)
            self.put(m.part("2026-11-10", "h.snapshot." + fmt), b"x" * 7)
            want["2026-11-10/h.exits." + fmt] = {
                "size": len(body), "head": zlib.crc32(body[:fs.HEAD]) & 0xffffffff,
                "head_n": min(len(body), fs.HEAD), "verified": False}
            want["2026-11-09/h.exits." + fmt] = {"verified": True}
            want["2026-11-10/h.snapshot.%s.gz" % fmt] = {"size": 7}
        self.put(m.rel("2026-11-10", "h.exits.txt"), b"not a day-file")
        self.put(m.part("2026-11-10", "h.snapshot.txt"), b"x")
        self.assertEqual(m.inventory(3), want)


if __name__ == "__main__":
    unittest.main()
