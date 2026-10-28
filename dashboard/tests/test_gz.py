"""Compressed day-files (`gz`), the dated-dir walk, and the pinned clock.

A `.jsonl.gz` must read exactly as the `.jsonl` inside it does: the same bytes
at the same offsets, so every byte-addressed reader -- the window plan's binary
search, the backwards read-back, a saved window's resume, the tail -- gives the
same answer for either.

    cd dashboard && .venv/bin/python -m unittest tests.test_gz -q
"""
import gzip
import os
import random
import sys
import time
import unittest
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from rc_dashboard import clock, gz                      # noqa: E402
from rc_dashboard.export import (dated_dirs, feed_files, iter_lines_reverse,  # noqa: E402
                                 make_predicate, parse_filters, resume_plan, scan,
                                 seek_to_epoch, window_plan, window_scan)
from rc_dashboard.feeds import host_of                  # noqa: E402
from rc_dashboard.normalize import Normalizer           # noqa: E402
from rc_dashboard.tail import FileTailSource            # noqa: E402
from rc_dashboard.windows import EVENTS                 # noqa: E402
from test_ingest import DAY, FeedCase, exit_rec, line   # noqa: E402


def payload(n, seed):
    r = random.Random(seed)
    return b"".join(b'{"ts_epoch": %d, "event": "exit", "pad": "%s"}\n'
                    % (i, b"x" * r.randrange(0, 300)) for i in range(n))


class SmallSpans(unittest.TestCase):
    """Spans, inflate steps and reads far smaller than a line, so every
    boundary -- checkpoint, member, read -- falls mid-line many times over."""

    def setUp(self):
        self.saved = gz.SPAN, gz._STEP, gz._CHUNK
        gz.SPAN, gz._STEP, gz._CHUNK = 997, 311, 101
        gz._indexes.clear()
        import tempfile
        self.dir = tempfile.mkdtemp(prefix="rc_gz_")

    def tearDown(self):
        gz.SPAN, gz._STEP, gz._CHUNK = self.saved
        gz._indexes.clear()
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)

    def check(self, name, plain, blob, note=None):
        p = os.path.join(self.dir, name + ".jsonl.gz")
        with open(p, "wb") as fh:
            fh.write(blob)
        rnd = random.Random(name)
        with gz.open_feed(p) as fh:
            self.assertEqual(gz.fstat(fh).st_size, len(plain))
            self.assertEqual(fh.read(), plain)
            for _ in range(200):
                a, n = rnd.randrange(0, len(plain) + 5), rnd.randrange(0, 4000)
                fh.seek(a)
                self.assertEqual(fh.read(n), plain[a:a + n], (name, a, n))
            fh.seek(0)
            self.assertEqual(list(fh), plain.splitlines(True))
        self.assertEqual(list(reversed(list(iter_lines_reverse(p)))),
                         [x for x in plain.split(b"\n") if x.strip()])
        idx = gz._indexes[p]
        if note is None:
            self.assertIsNone(idx.note)
        else:
            self.assertIn(note, idx.note or "")

    def test_single_member(self):
        a = payload(400, 1)
        self.check("single", a, gzip.compress(a))

    def test_concatenated_members(self):
        # CPython can leave a finished member's leftover in BOTH unconsumed_tail
        # and unused_data; counting it twice put every offset after the first
        # member 41 bytes early.
        a, b = payload(400, 1), payload(300, 2)
        self.check("multi", a + b + a,
                   gzip.compress(a) + gzip.compress(b, 1) + gzip.compress(a, 9))

    def test_empty_member_and_empty_file(self):
        a = payload(50, 3)
        self.check("emptymem", a, gzip.compress(a) + gzip.compress(b""))
        self.check("empty", b"", gzip.compress(b""))

    def test_trailing_bytes_are_reported_not_read(self):
        a = payload(50, 4)
        self.check("trailing", a, gzip.compress(a) + b"\0" * 37, "not gzip data")

    def test_truncated_stream_reads_as_far_as_it_goes(self):
        full = gzip.compress(payload(400, 5))
        cut = full[:len(full) * 2 // 3]
        part = zlib.decompressobj(31).decompress(cut)
        self.check("truncated", part, cut, "without its trailer")


class CompressedFeed(FeedCase):
    """The same records, plain in one root and compressed in another."""

    def setUp(self):
        super().setUp()
        gz._indexes.clear()
        self.saved = gz.SPAN
        gz.SPAN = 4096                      # many checkpoints in a small file
        recs = [exit_rec(self.now - 7200 + 10 * i, pid=100 + i,
                         actor3=("agent", "human", "human-vscode")[i % 3])
                for i in range(600)]
        self.append("h1.exits.jsonl", recs)
        self.zroot = os.path.join(self.tmp, "zipped")
        os.makedirs(os.path.join(self.zroot, DAY))
        with open(self.path("h1.exits.jsonl"), "rb") as src, \
                gzip.open(os.path.join(self.zroot, DAY, "h1.exits.jsonl.gz"), "wb") as dst:
            dst.write(src.read())

    def tearDown(self):
        gz.SPAN = self.saved
        gz._indexes.clear()
        super().tearDown()

    def test_window_scan_reads_the_same_records(self):
        since = self.now - 3600
        out = []
        for root in (self.root, self.zroot):
            plan, total = window_plan([root], since)
            recs = [(r["pid"], r["_ts_epoch"]) for r, _ in
                    window_scan(plan, Normalizer(self.cfg()), since, events=EVENTS)]
            out.append((plan[0]["start"], total, recs))
        self.assertEqual(out[0], out[1])
        self.assertTrue(out[0][2])

    def test_seek_and_resume_are_by_uncompressed_offset(self):
        plain = self.path("h1.exits.jsonl")
        zp = os.path.join(self.zroot, DAY, "h1.exits.jsonl.gz")
        since = self.now - 1800
        offs = []
        for p in (plain, zp):
            with gz.open_feed(p) as fh:
                offs.append(seek_to_epoch(fh, since, gz.fstat(fh).st_size))
        self.assertEqual(offs[0], offs[1])
        # what a saved build records, and resuming from it
        plan, _ = window_plan([self.zroot], since)
        list(window_scan(plan, Normalizer(self.cfg()), since, events=EVENTS))
        saved = [{k: e[k] for k in ("path", "dev", "ino", "head_n", "head", "end")}
                 for e in plan]
        self.assertEqual(saved[0]["end"], os.path.getsize(plain))
        got = resume_plan([self.zroot], since, saved)
        self.assertIsNotNone(got)
        self.assertEqual(got[1], 0)          # nothing written since: nothing to read

    def test_tail_positions_a_compressed_file_at_its_uncompressed_end(self):
        plans = []
        for root in (self.root, self.zroot):
            src = FileTailSource([root])
            plans.append([(e["start"], e["end"]) for e in src.position(self.now - 3600)])
            self.assertEqual(list(src.poll()), [])   # handed over at the end
        self.assertEqual(plans[0], plans[1])

    def test_plain_file_wins_over_its_gz_twin(self):
        d = os.path.join(self.root, DAY)
        with gzip.open(os.path.join(d, "h1.exits.jsonl.gz"), "wb") as fh:
            fh.write(b"")
        self.assertEqual(feed_files(d), [self.path("h1.exits.jsonl")])

    def test_host_of_a_compressed_file(self):
        self.assertEqual(host_of("/x/2026-10-09/boslogin06.rc.fas.harvard.edu.exits.jsonl.gz"),
                         "boslogin06.rc.fas.harvard.edu")

    def test_empty_date_dirs_do_not_count_as_days(self):
        for d in ("2099-01-01", "2099-01-02"):
            os.makedirs(os.path.join(self.zroot, d))
        self.assertEqual([d for d, _ in dated_dirs(self.zroot, 1)], [DAY])
        # a 1 h window plans 2 days; two empty newer dirs once left it none
        plan, _ = window_plan([self.zroot], self.now - 3600, 2)
        self.assertEqual(len(plan), 1)


class BoundedScan(CompressedFeed):
    """`/api/events` and `/api/export` read no further than their window."""

    def setUp(self):
        super().setUp()
        old = os.path.join(self.zroot, "2000-01-01")
        os.makedirs(old)
        with gzip.open(os.path.join(old, "h1.exits.jsonl.gz"), "wb") as fh:
            for i in range(3000):
                fh.write(line(exit_rec(946700000 + i, pid=i)))

    def run_scan(self, newest_first):
        f = parse_filters({"from": [str(self.now - 3600)], "event": ["exit"]})
        return list(scan([self.zroot], Normalizer(self.cfg()), make_predicate(f),
                         limit=5, newest_first=newest_first))

    def test_a_day_outside_the_window_is_not_read_to_its_end(self):
        for newest_first in (True, False):
            got = self.run_scan(newest_first)
            self.assertEqual(len(got), 5)
            # every page waited on the whole old day before the bound
            self.assertLess(got[-1][1], 50, newest_first)
            ts = [r["_ts_epoch"] for r, _, _ in got]
            self.assertTrue(all(t >= self.now - 3600 for t in ts))
            self.assertEqual(ts, sorted(ts, reverse=newest_first))


class PinnedClock(CompressedFeed):

    def setUp(self):
        super().setUp()
        self.time, self.time_ns = time.time, time.time_ns
        self.state = dict(clock._state)

    def tearDown(self):
        time.time, time.time_ns = self.time, self.time_ns
        clock._state.clear()
        clock._state.update(self.state)
        super().tearDown()

    def cfg_as_of(self, v):
        cfg = self.cfg()
        cfg.tree["clock"] = {"as_of": v}
        return cfg

    def test_auto_pins_one_second_after_the_newest_record_and_freezes(self):
        newest = self.now - 7200 + 10 * 599
        st = clock.install(self.cfg_as_of("auto"), [self.zroot])
        self.assertTrue(st["pinned"])
        self.assertAlmostEqual(time.time(), newest + 1, places=3)
        a = time.time()
        time.sleep(0.01)
        self.assertEqual(time.time(), a)     # frozen, so a restart resumes its builds
        self.assertEqual(time.time_ns(), int(a * 1e9))

    def test_wall_clock_is_untouched(self):
        st = clock.install(self.cfg_as_of(""), [self.zroot])
        self.assertFalse(st["pinned"])
        self.assertIs(time.time, self.time)

    def test_explicit_time_and_bad_value(self):
        clock.install(self.cfg_as_of("2026-10-09 12:00:00"), [self.zroot])
        self.assertEqual(time.time(), time.mktime(time.strptime("2026-10-09 12:00:00",
                                                                "%Y-%m-%d %H:%M:%S")))
        with self.assertRaises(SystemExit):
            clock.resolve(self.cfg_as_of("yesterday"), [self.zroot])


if __name__ == "__main__":
    unittest.main()
