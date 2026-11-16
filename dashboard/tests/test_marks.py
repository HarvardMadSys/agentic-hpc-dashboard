"""MARK frames (`ebr.MARK`) change nothing a reader reads.

A converted day-file (ebrview.py) carries advisory MARK frames between its
DATA frames: no records, NaN time bounds, kinds 0. Every reader must pass them
as it passes a SEGMENT: the tail and its backfill, the event scan both ways,
the window scan, an hour's read, the seeks by time and from the end, and the
clock. Here the same records are written four ways -- JSONL; EBR a frame per
poll round, as the collector writes it; that EBR with MARK frames after every
round, in runs longer than a seek's probe walks and one before any record; and
the converter's own output of the JSONL, a MARK every few KB -- and each reader
must read the same records from every one of them.

    cd dashboard && uv run python -m unittest tests.test_marks -q
"""
import gzip
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from rc_dashboard import clock, ebr, ebrview, feedfile, gz         # noqa: E402
from rc_dashboard.export import (make_predicate, parse_filters, scan,  # noqa: E402
                                 window_plan, window_scan)
from rc_dashboard.feedfile import day_files                        # noqa: E402
from rc_dashboard.hours import HOUR, new_stats, read_span          # noqa: E402
from rc_dashboard.normalize import Normalizer, epoch_of            # noqa: E402
from rc_dashboard.tail import FileTailSource, read_back            # noqa: E402
from rc_dashboard.windows import EVENTS                            # noqa: E402
from test_formats import plain_cfg, varied, write_rounds       # noqa: E402
from test_ingest import DAY, RESET_BYTES                           # noqa: E402

FORMATS = ("jsonl", "ebr", "marked", "converted")
PER_ROUND = 3
RUN_EVERY = 7       # every 7th round is followed by a run of MARKs ...
RUN = 20            # ... longer than a seek's probe walks (feedfile.PROBE_FRAMES)


def write_marked(path, recs, per_round=PER_ROUND, start=0):
    """`recs` a round at a time, each round's frames followed by a MARK in the
    same write; every RUN_EVERY-th round by a run of RUN of them; and the file
    begun with a MARK before any record."""
    if not os.path.exists(path):
        with ebr.Writer.open_append(path, reset_bytes=RESET_BYTES) as w:
            w.mark({"note": "before any record"})
    for k, i in enumerate(range(0, len(recs), per_round), start):
        with ebr.Writer.open_append(path, max_frame_records=16, reset_bytes=RESET_BYTES) as w:
            for r in recs[i:i + per_round]:
                w.write(r)
            w.mark({"round": k})
            if k % RUN_EVERY == 3:
                for j in range(RUN):
                    w.mark({"round": k, "run": j})


def rounds_of(recs):
    return [recs[i:i + PER_ROUND] for i in range(0, len(recs), PER_ROUND)]


class MarkRoots(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rc_marks_")
        self.roots = {f: os.path.join(self.tmp, f) for f in FORMATS}
        for r in self.roots.values():
            os.makedirs(os.path.join(r, DAY))
        self.cfg = plain_cfg()
        self.now = time.time()
        self.t0 = self.now - 3 * 3600
        self.rounds = {}
        # the converter's MARKs a few KB apart, not 64 MB, read a few KB at a time
        for name, value in (("BULK_MARK_BYTES", 4096), ("CHUNK", 2048)):
            p = mock.patch.object(ebrview, name, value)
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def path(self, fmt, name, day=DAY):
        stem = name[:-len(".jsonl")]
        return os.path.join(self.roots[fmt], day, stem + (".jsonl" if fmt == "jsonl"
                                                          else ".ebr"))

    def write(self, name, recs):
        """`recs` appended to `name` in every format (the converted one by
        converting what the JSONL holds now)."""
        k = self.rounds.get(name, 0)
        write_rounds(self.path("jsonl", name), recs, "jsonl", PER_ROUND)
        write_rounds(self.path("ebr", name), recs, "ebr", PER_ROUND)
        write_marked(self.path("marked", name), recs, start=k)
        self.rounds[name] = k + len(rounds_of(recs))
        ebrview.convert(self.path("converted", name), self.path("jsonl", name))

    def feed(self):
        self.write("h1.exits.jsonl", varied(self.t0, 400, seed=1, host="h1"))
        self.write("h2.snapshot.jsonl", varied(self.t0 + 7, 200, seed=2, host="h2",
                                               step=50.0))

    def prefer(self, fmt):
        return "jsonl" if fmt == "jsonl" else "ebr"

    def each(self, read):
        """`read(fmt)` of every format; all equal to the plain EBR one's."""
        want = read("ebr")
        self.assertTrue(want)
        for fmt in FORMATS:
            with self.subTest(fmt=fmt):
                self.assertEqual(read(fmt), want)
        return want

    def count_marks(self, fmt, name):
        return sum(1 for f in ebr.frames(self.path(fmt, name)) if f.kind == ebr.MARK)


def key(r):
    return (r.get("host"), r.get("pid"), r.get("event"), r.get("ts_epoch"))


class FilesTest(MarkRoots):

    def test_the_files_hold_marks_and_the_same_records(self):
        self.feed()
        for name in ("h1.exits.jsonl", "h2.snapshot.jsonl"):
            self.assertGreater(self.count_marks("marked", name), 50)
            self.assertGreater(self.count_marks("converted", name), 2)
            self.assertEqual(self.count_marks("ebr", name), 0)
            recs = [r for _p, r in ebr.iter_records(self.path("ebr", name))]
            for fmt in ("marked", "converted"):
                got = [r for _p, r in ebr.iter_records(self.path(fmt, name))]
                self.assertEqual(got, recs, fmt)
            marks = [f for f in ebr.frames(self.path("marked", name)) if f.kind == ebr.MARK]
            self.assertTrue(all(f.n_records == 0 and f.ts_min != f.ts_min and not f.kinds
                                for f in marks))


class TailTest(MarkRoots):

    def test_the_backfill_and_the_tail_read_alike(self):
        self.feed()
        sources, backfilled = {}, {}
        since = self.t0 + 3600
        for fmt in FORMATS:
            src = FileTailSource([self.roots[fmt]], days=3, prefer=self.prefer(fmt))
            plan = src.position(since)
            stats = {"bytes": 0, "parse_errors": 0}
            backfilled[fmt] = [key(raw) for _ts, raw in read_back(plan, stats)]
            self.assertEqual(stats["parse_errors"], 0, fmt)
            sources[fmt] = src
        self.each(lambda fmt: backfilled[fmt])
        # then the collector writes on, and every format's tail reads it once
        more = varied(self.now - 600, 30, seed=7, host="h1", step=15.0)
        self.write("h1.exits.jsonl", more)
        got = {fmt: [key(r) for r in sources[fmt].poll()] for fmt in FORMATS}
        want = self.each(lambda fmt: got[fmt])
        self.assertEqual(want, [key(r) for r in more])
        for fmt in FORMATS:
            self.assertEqual(list(sources[fmt].poll()), [], fmt)
            self.assertEqual(sources[fmt].stats["parse_errors"], 0, fmt)
            self.assertEqual(sources[fmt].stats["corrupt_frames"], 0, fmt)


class ScanTest(MarkRoots):

    def run_scan(self, fmt, params, newest_first, limit=None):
        f = parse_filters({k: [v] for k, v in params.items()})
        return [key(rec) for rec, _s, _m in scan([self.roots[fmt]], Normalizer(self.cfg),
                                                 make_predicate(f), limit=limit,
                                                 newest_first=newest_first,
                                                 prefer=self.prefer(fmt))]

    def test_the_event_scan_both_ways(self):
        self.feed()
        t = self.t0
        for params in ({}, {"event": "exit"}, {"user": "alice"}, {"event": "conn,residency"},
                       {"from": str(t + 3600), "to": str(t + 5400)}, {"to": str(t + 2000)},
                       {"from": str(t + 7000), "user": "bob"}):
            for newest in (True, False):
                with self.subTest(params=params, newest_first=newest):
                    self.each(lambda fmt: self.run_scan(fmt, params, newest))
                    self.each(lambda fmt: self.run_scan(fmt, params, newest, limit=7))


class WindowTest(MarkRoots):

    def test_the_window_scan(self):
        self.feed()
        for since, until in ((self.t0 - 10, None), (self.t0 + 4000, None),
                             (self.t0 + 1000, self.t0 + 5000)):
            def read(fmt):
                plan, _ = window_plan([self.roots[fmt]], since, prefer=self.prefer(fmt))
                return [key(r) for r, _st in window_scan(plan, Normalizer(self.cfg), since,
                                                         until, events=EVENTS)]
            with self.subTest(since=since - self.t0, until=until):
                self.each(read)


class HourReadTest(MarkRoots):

    def test_an_hours_read(self):
        self.feed()
        h0 = int((self.t0 + 1800) // HOUR)
        for h in (h0, h0 + 1):
            def read(fmt):
                files = day_files([self.roots[fmt]], 3, self.prefer(fmt))
                stream, _plan, stats = read_span(files, Normalizer(self.cfg), h * HOUR,
                                                 (h + 1) * HOUR, EVENTS, stats=new_stats())
                out = [key(r) for _ts, r in stream]
                self.assertEqual(stats["parse_errors"], 0)
                return out
            with self.subTest(hour=h - h0):
                self.each(read)


class SeekTest(MarkRoots):

    def test_the_seeks_by_time_and_from_the_end(self):
        self.feed()
        name = "h1.exits.jsonl"
        for t in (self.t0 - 100, self.t0 + 10, self.t0 + 2222, self.t0 + 5000,
                  self.t0 + 7990, self.t0 + 99999):
            def seek(fmt):
                p = self.path(fmt, name)
                with gz.open_feed(p) as fh:
                    size = gz.fstat(fh).st_size
                    start = feedfile.seek_to_epoch(fh, t, size, "ebr")
                    end = feedfile.end_at(fh, t, size, "ebr")
                fwd = [key(r) for _pos, r in feedfile.records_forward(p, start)]
                upto = [key(r) for _pos, r in feedfile.records_forward(p, 0, end)]
                # what each must hold, whatever its frames: every record from
                # `t` on, and every one up to it
                return ([k for k in fwd if k[3] >= t], [k for k in upto if k[3] <= t])

            def ebr_only(fmt):
                return seek(fmt) if fmt != "jsonl" else seek("ebr")
            with self.subTest(t=t - self.t0):
                want = ebr_only("ebr")
                for fmt in ("marked", "converted"):
                    self.assertEqual(ebr_only(fmt), want, fmt)

        def ends(fmt):
            if fmt == "jsonl":
                fmt = "ebr"
            p = self.path(fmt, name)
            return (feedfile.newest(p, epoch_of), feedfile.newest(p, epoch_of, 4096),
                    [key(r) for r in feedfile.last_records(p, 5)],
                    [key(r) for r in feedfile.last_records(p, 1)])
        self.each(ends)

    def test_a_seek_in_a_file_that_ends_in_marks(self):
        self.feed()
        name = "h1.exits.jsonl"
        for fmt in ("marked", "converted"):
            with ebr.Writer.open_append(self.path(fmt, name)) as w:
                for j in range(3 * RUN):
                    w.mark({"tail": j})

        t = feedfile.newest(self.path("ebr", name), epoch_of) - 45

        def last(fmt):
            p = self.path("ebr" if fmt == "jsonl" else fmt, name)
            with gz.open_feed(p) as fh:
                size = gz.fstat(fh).st_size
                at = feedfile.seek_to_epoch(fh, t, size, "ebr")
                end = feedfile.end_at(fh, t, size, "ebr")
            return (feedfile.newest(p, epoch_of), [key(r) for r in feedfile.last_records(p, 3)],
                    [k for k in (key(r) for _pos, r in feedfile.records_forward(p, at))
                     if k[3] >= t],
                    [k for k in (key(r) for _pos, r in feedfile.records_forward(p, 0, end))
                     if k[3] <= t])
        got = self.each(last)
        self.assertTrue(got[2])

    def test_sealed_copies_seek_as_their_plain_files(self):
        self.feed()
        name = "h1.exits.jsonl"
        t = self.t0 + 4000
        for fmt in ("marked", "converted"):
            p = self.path(fmt, name)
            with gz.open_feed(p) as fh:
                size = gz.fstat(fh).st_size
                plain = (feedfile.seek_to_epoch(fh, t, size, "ebr"),
                         feedfile.end_at(fh, t, size, "ebr"))
            with open(p, "rb") as src, gzip.open(p + ".gz", "wb") as dst:
                shutil.copyfileobj(src, dst)
            os.unlink(p)
            with gz.open_feed(p + ".gz") as fh:
                zipped = (feedfile.seek_to_epoch(fh, t, size, "ebr"),
                          feedfile.end_at(fh, t, size, "ebr"))
            self.assertEqual(plain, zipped, fmt)


class ClockTest(MarkRoots):

    def test_the_newest_record(self):
        self.feed()
        got = {fmt: clock.newest_record([self.roots[fmt]], self.prefer(fmt))[0]
               for fmt in FORMATS}
        self.each(lambda fmt: got[fmt])


if __name__ == "__main__":
    unittest.main()
