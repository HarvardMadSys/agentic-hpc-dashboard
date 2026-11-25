"""Compressed day-files (`gz`), the dated-dir walk, and the pinned clock.

A `.jsonl.gz` must read exactly as the `.jsonl` inside it does: the same bytes
at the same offsets, so every byte-addressed reader -- the window plan's binary
search, the backwards read-back, a saved window's resume, the tail -- gives the
same answer for either.

    cd dashboard && .venv/bin/python -m unittest tests.test_gz -q
"""
import contextlib
import gzip
import os
import random
import shutil
import sys
import time
import unittest
import zlib
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from rc_dashboard import clock, gz                      # noqa: E402
from rc_dashboard.aggregator import Aggregator          # noqa: E402
from rc_dashboard.export import (dated_dirs, iter_lines_reverse,  # noqa: E402
                                 resume_plan, seek_to_epoch, window_plan, window_scan)
from rc_dashboard.normalize import Normalizer           # noqa: E402
from rc_dashboard.tail import FileTailSource, read_back  # noqa: E402
from rc_dashboard.windows import EVENTS                 # noqa: E402
from test_ingest import DAY, NONE, FeedCase, exit_rec   # noqa: E402


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


class IndexCache(unittest.TestCase):
    """The process keeps an index for each compressed file it reads, up to `_KEEP`
    of them. It drops the index of a file that is gone, and of one no reader used for
    `_IDLE_S`: a copy that drops a day of `.gz` files each night (pull.py, as a node seals
    its days), and days that left the window, would otherwise keep theirs here."""

    def setUp(self):
        gz._indexes.clear()
        self.saved = gz._SWEEP_S
        gz._SWEEP_S = 0.0
        import tempfile
        self.dir = tempfile.mkdtemp(prefix="rc_gz_")

    def tearDown(self):
        gz._SWEEP_S = self.saved
        gz._indexes.clear()
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)

    def gz_file(self, name, seed):
        p = os.path.join(self.dir, name + ".jsonl.gz")
        with open(p, "wb") as fh:
            fh.write(gzip.compress(payload(50, seed)))
        return p

    def read(self, p):
        with gz.open_feed(p) as fh:
            return fh.read()

    def test_a_deleted_file_keeps_no_index(self):
        a, b = self.gz_file("a", 1), self.gz_file("b", 2)
        self.read(a)
        self.read(b)
        self.assertEqual(sorted(gz._indexes), [a, b])
        os.unlink(a)
        self.read(b)
        self.assertEqual(sorted(gz._indexes), [b])
        self.assertEqual(gz.status()["files"], 1)

    def test_an_index_no_reader_used_is_dropped(self):
        a, b = self.gz_file("a", 1), self.gz_file("b", 2)
        self.read(a)
        gz._indexes[a].used -= gz._IDLE_S + 1
        self.read(b)
        self.assertEqual(sorted(gz._indexes), [b])

    def test_opening_a_file_again_keeps_its_index(self):
        a, b = self.gz_file("a", 1), self.gz_file("b", 2)
        self.read(a)
        idx = gz._indexes[a]
        idx.used -= gz._IDLE_S + 1
        self.read(a)
        self.read(b)
        self.assertIs(gz._indexes.get(a), idx)

    def test_a_long_read_keeps_its_index(self):
        """Every span a reader inflates counts as a use."""
        a, b = self.gz_file("a", 1), self.gz_file("b", 2)
        saved = gz.SPAN, gz._STEP, gz._CHUNK
        gz.SPAN, gz._STEP, gz._CHUNK = 997, 311, 101     # many spans, as in SmallSpans
        try:
            gz._indexes.clear()
            with gz.open_feed(a, buffering=64) as fh:   # a small buffer: each
                fh.read(10)                             # read reaches the spans
                gz._indexes[a].used -= gz._IDLE_S + 1
                fh.seek(5000)
                fh.read(10)
                self.read(b)
                self.assertIn(a, gz._indexes)
        finally:
            gz.SPAN, gz._STEP, gz._CHUNK = saved


class SharedSpans(unittest.TestCase):
    """Inside `shared_spans` a reader is handed the span another reader of the same
    file inflated last: each reads what it would have read alone, a read after a
    search inflates the search's span no more, a read through a whole file holds
    its last span and none of those it went past, at most `n` files' are held, and
    none once the block ends."""

    def setUp(self):
        self.saved = gz.SPAN, gz._STEP, gz._CHUNK
        gz.SPAN, gz._STEP, gz._CHUNK = 997, 311, 101
        gz._indexes.clear()
        import tempfile
        self.dir = tempfile.mkdtemp(prefix="rc_gz_")
        self.plain = payload(400, 6)
        self.path = os.path.join(self.dir, "h1.exits.jsonl.gz")
        with open(self.path, "wb") as fh:
            fh.write(gzip.compress(self.plain))

    def tearDown(self):
        gz.SPAN, gz._STEP, gz._CHUNK = self.saved
        gz._indexes.clear()
        shutil.rmtree(self.dir, ignore_errors=True)

    def counted(self):
        """`Index.span` counting the spans it inflates, by checkpoint."""
        seen = []
        real = gz.Index.span

        def span(index, fh, i):
            seen.append(i)
            return real(index, fh, i)
        return seen, mock.patch.object(gz.Index, "span", span)

    def test_readers_inside_read_what_readers_outside_read(self):
        rnd = random.Random(8)
        with gz.shared_spans(4):
            a, b = gz.open_feed(self.path, buffering=64), gz.open_feed(self.path, buffering=64)
            with a, b:
                for _ in range(300):
                    fh = rnd.choice((a, b))
                    at, n = rnd.randrange(0, len(self.plain) + 5), rnd.randrange(0, 3000)
                    fh.seek(at)
                    self.assertEqual(fh.read(n), self.plain[at:at + n], (at, n))
            with gz.open_feed(self.path) as fh:
                self.assertEqual(list(fh), self.plain.splitlines(True))

    def test_a_read_after_a_search_is_handed_the_span_the_search_inflated(self):
        # As an hour is read: a time search ends in a span, and the read of what it
        # found starts there. Alone, the read inflates that span again.
        at = len(self.plain) // 2
        seen, patch = self.counted()
        with patch:
            with gz.open_feed(self.path) as fh:       # indexed, outside the block
                fh.read()
            for shared in (False, True):
                del seen[:]
                with gz.shared_spans(4) if shared else contextlib.nullcontext():
                    with gz.open_feed(self.path, buffering=64) as fh:   # the search
                        fh.seek(at)
                        fh.read(1)
                    with gz.open_feed(self.path, buffering=64) as fh:   # the read
                        fh.seek(at)
                        self.assertEqual(fh.read(), self.plain[at:])
                self.assertEqual(seen.count(seen[0]), 1 if shared else 2, shared)
                self.assertEqual(len(set(seen)), len(seen) - (0 if shared else 1))

    def test_a_read_through_a_file_holds_its_last_span_only(self):
        # A read goes on forward, so the spans it went past are not asked for
        # again: holding them made a FAS node-hour's block hold some 40.
        with gz.shared_spans(4) as held:
            with gz.open_feed(self.path) as fh:
                self.assertEqual(fh.read(), self.plain)
            idx = gz._indexes[self.path]
            self.assertGreater(len(idx.offs), 10)
            self.assertEqual(list(held.held), [idx])
            self.assertEqual(held.held[idx][0], len(idx.offs) - 1)

    def test_at_most_n_files_are_held_and_none_once_the_block_ends(self):
        paths = [self.path]
        for k in range(3):
            p = os.path.join(self.dir, "h%d.exits.jsonl.gz" % (k + 2))
            with open(p, "wb") as fh:
                fh.write(gzip.compress(payload(100, k)))
            paths.append(p)
        with gz.shared_spans(2) as held:
            for p in paths:
                with gz.open_feed(p) as fh:
                    fh.read()
            self.assertEqual(set(held.held), {gz._indexes[p] for p in paths[-2:]})
        self.assertEqual(len(held.held), 0)
        self.assertIsNone(getattr(gz._shared, "spans", None))

    def test_a_block_holding_fewer_files_than_are_searched_keeps_none(self):
        # An hour searches every file before it reads any (`hours.plan_span`): a
        # block one file short loses each span to the searches after it, and
        # every read inflates its span again -- what `hours.shared_files` sizes
        # the hourly reduction's block against.
        paths = [self.path]
        for k in range(5):
            p = os.path.join(self.dir, "h%d.snapshot.jsonl.gz" % k)
            with open(p, "wb") as fh:
                fh.write(gzip.compress(payload(200, k + 10)))
            paths.append(p)
        for p in paths:                               # indexed, outside the block
            with gz.open_feed(p) as fh:
                fh.read()

        def an_hour(n):
            seen, patch = self.counted()
            with patch, gz.shared_spans(n):
                for _search_then_read in range(2):
                    for p in paths:
                        with gz.open_feed(p, buffering=64) as fh:
                            fh.seek(300)
                            fh.read(1)
            return len(seen)
        self.assertEqual(an_hour(len(paths)), len(paths))
        self.assertEqual(an_hour(len(paths) - 1), 2 * len(paths))

    def test_a_block_inside_another_is_the_outer_one(self):
        with gz.shared_spans(5) as outer:
            with gz.shared_spans(2) as inner:
                self.assertIs(inner, outer)
            self.assertIs(gz._shared.spans, outer)  # still in force after the inner one
        self.assertIsNone(getattr(gz._shared, "spans", None))

    def test_another_thread_shares_nothing(self):
        import threading
        seen, patch = self.counted()
        got = {}
        with patch, gz.shared_spans(64):
            with gz.open_feed(self.path) as fh:
                fh.read()
            mine = len(seen)

            def other():
                got["spans"] = getattr(gz._shared, "spans", None)
                with gz.open_feed(self.path) as fh:
                    got["read"] = fh.read()
            t = threading.Thread(target=other)
            t.start()
            t.join()
        self.assertIsNone(got["spans"])
        self.assertEqual(got["read"], self.plain)
        self.assertEqual(len(seen), 2 * mine)         # it inflated every span itself


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
        self.write_gz("h1.exits.jsonl", os.path.join(self.zroot, DAY))

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

    def test_empty_date_dirs_do_not_count_as_days(self):
        for d in ("2099-01-01", "2099-01-02"):
            os.makedirs(os.path.join(self.zroot, d))
        self.assertEqual([d for d, _ in dated_dirs(self.zroot, 1)], [DAY])
        # a 1 h window plans 2 days; two empty newer dirs once left it none
        plan, _ = window_plan([self.zroot], self.now - 3600, 2)
        self.assertEqual(len(plan), 1)


class CompressedUnderTheTail(FeedCase):
    """A day the tail was following as plain JSONL, compressed in place at the
    rollover (`FeedCase.compress`). Where the tail stopped in the `.jsonl` is
    where it takes up the `.jsonl.gz`: no record read twice, none skipped."""

    def setUp(self):
        super().setUp()
        gz._indexes.clear()
        self.recs = [exit_rec(self.now - 600 + 60 * i, pid=i) for i in range(8)]

    def tearDown(self):
        gz._indexes.clear()
        super().tearDown()

    @staticmethod
    def pids(recs):
        return [r["pid"] for r in recs]

    def test_a_day_read_then_compressed_is_not_read_again(self):
        self.append("h1.exits.jsonl", self.recs[:5])
        src = FileTailSource([self.root])
        self.assertEqual(self.pids(src.poll()), [0, 1, 2, 3, 4])
        self.compress("h1.exits.jsonl")
        # a `.gz` the tail never followed as plain JSONL is new: read whole
        self.append("h2.exits.jsonl", [exit_rec(self.now - 30, pid=99, host="h2")])
        self.compress("h2.exits.jsonl")
        self.assertEqual(self.pids(src.poll()), [99])
        self.assertEqual(self.pids(src.poll()), [])

    def test_lines_written_just_before_the_compression_arrive_once(self):
        # The day's last poll round lands after the tail's last read -- half
        # of its first line was there already -- and the day is sealed and
        # compressed before the tail looks again.
        five, sixth, rest = self.rounds([self.recs[:5], self.recs[5:6], self.recs[6:]])
        self.write_raw("h1.exits.jsonl", five + sixth[:20])
        src = FileTailSource([self.root])
        self.assertEqual(self.pids(src.poll()), [0, 1, 2, 3, 4])
        self.write_raw("h1.exits.jsonl", sixth[20:] + rest)
        self.compress("h1.exits.jsonl")
        self.assertEqual(self.pids(src.poll()), [5, 6, 7])
        self.assertEqual(self.pids(src.poll()), [])

    def test_a_compression_while_a_read_is_handed_out_loses_no_offset(self):
        # gzip can finish between the tail's read of the plain file and its
        # noting how far that read got.
        self.append("h1.exits.jsonl", self.recs[:5])
        src = FileTailSource([self.root])
        poll = src.poll()
        got = [next(poll)["pid"]]
        self.compress("h1.exits.jsonl")
        got += self.pids(poll)
        self.assertEqual(got, [0, 1, 2, 3, 4])
        self.assertEqual(self.pids(src.poll()), [])

    def test_a_prune_between_the_compression_and_the_next_poll_keeps_the_offset(self):
        # `poll_once` polls, then prunes: a day compressed in between has left
        # the listing before the tail has met its `.gz`.
        self.append("h1.exits.jsonl", self.recs[:5])
        src = FileTailSource([self.root])
        list(src.poll())
        self.append("h1.exits.jsonl", self.recs[5:])
        self.compress("h1.exits.jsonl")
        src.prune_state()
        self.assertEqual(self.pids(src.poll()), [5, 6, 7])
        src.prune_state()                  # the plain name is followed no more
        self.assertEqual(list(src.state), [self.path("h1.exits.jsonl.gz")])

    def test_a_day_compressed_after_positioning_is_read_back_and_followed_once(self):
        # A start just after midnight: the tail is handed yesterday as plain
        # JSONL, and the compression lands before the read-back opens it.
        self.append("h1.exits.jsonl", self.recs[:5])
        src = FileTailSource([self.root])
        plan = src.position(self.now - 3600)
        self.append("h1.exits.jsonl", self.recs[5:])
        self.compress("h1.exits.jsonl")
        stats = {"bytes": 0, "parse_errors": 0}
        self.assertEqual(self.pids(raw for _ts, raw in read_back(plan, stats)),
                         [4, 3, 2, 1, 0])
        self.assertEqual(self.pids(src.poll()), [5, 6, 7])
        self.assertEqual(self.pids(src.poll()), [])

    def test_a_gz_shorter_than_where_the_tail_was_is_read_from_its_start(self):
        # Not the bytes the tail was reading: like a truncated file, read again.
        self.append("h1.exits.jsonl", self.recs[:5])
        src = FileTailSource([self.root])
        list(src.poll())
        os.unlink(self.path("h1.exits.jsonl"))
        self.append("h1.exits.jsonl", self.recs[6:])
        self.compress("h1.exits.jsonl")
        self.assertEqual(self.pids(src.poll()), [6, 7])
        self.assertEqual(src.stats["resets"], 1)

    def test_the_live_tier_counts_a_compressed_day_once(self):
        self.append("h1.exits.jsonl", self.thirty_exits())
        agg = Aggregator(self.cfg())
        agg.backfill_now()
        agg.poll_once()
        self.compress("h1.exits.jsonl")
        agg.poll_once()
        self.assertEqual(self.events(agg), dict(NONE, agent=20, human=10))


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
