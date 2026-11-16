"""The day-file layer (`feedfile`): names, the twin rule, and EBR's frames.

A collector writing both formats leaves `x.jsonl` and `x.ebr` with the same
records; the dashboard reads exactly one, the one `feedfile.choose` names, and
never switches a live day from one to the other. These tests write the files
the collector writes, in the order it writes them, and check each row of the
rule; then the EBR primitives the readers use -- the time searches, the
boundaries, and reading frames from either end -- against brute force.

    cd dashboard && uv run python -m unittest tests.test_feedfile -q
"""
import gzip
import json
import os
import random
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rc_dashboard import config as configmod      # noqa: E402
from rc_dashboard import ebr, feedfile, gz        # noqa: E402

T0 = 1_790_000_000.0


def rec(t, i, event="exit", **kw):
    r = {"schema_version": 8, "event": event, "ts_epoch": t, "host": "h1", "pid": i,
         "user": "u%d" % (i % 3), "args": "cmd %d" % i}
    r.update(kw)
    return r


def jsonl_write(path, recs, raw=b""):
    with open(path, "ab") as fh:
        for r in recs:
            fh.write(json.dumps(r).encode() + b"\n")
        fh.write(raw)


def ebr_write(path, recs, **kw):
    w = ebr.Writer.open_append(path, **kw)
    try:
        for r in recs:
            w.write(r)
        w.flush()
    finally:
        w.close()


def gzip_file(path):
    with open(path, "rb") as src, gzip.open(path + ".gz", "wb") as dst:
        shutil.copyfileobj(src, dst)
    os.unlink(path)
    return path + ".gz"


def forget():
    """A restart: no choice and no first record remembered."""
    feedfile._choices.clear()
    feedfile._firsts.clear()


class TmpCase(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="rc_feedfile_")
        forget()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)
        forget()

    def p(self, name):
        return os.path.join(self.dir, name)


class NamesTest(unittest.TestCase):

    def test_split_name(self):
        self.assertEqual(feedfile.split_name("/a/2026-01-01/h.exits.jsonl.gz"),
                         ("h.exits", "jsonl", True))
        self.assertEqual(feedfile.split_name("h.snapshot.ebr"), ("h.snapshot", "ebr", False))
        self.assertEqual(feedfile.split_name("/x/n1.ebr.gz"), ("n1", "ebr", True))
        self.assertEqual(feedfile.split_name("notes.txt"), ("notes.txt", None, False))
        self.assertEqual(feedfile.format_of("x.ebr.gz"), "ebr")
        self.assertEqual(feedfile.format_of("x.jsonl"), "jsonl")

    def test_host_of_strips_either_format_and_only_a_known_stream(self):
        for name, host in (("h1.exits.jsonl", "h1"), ("h1.exits.ebr", "h1"),
                           ("h1.snapshot.ebr.gz", "h1"), ("n1.ebr", "n1"),
                           ("n1.jsonl.gz", "n1"), ("login.rc.fas.exits.ebr", "login.rc.fas"),
                           ("boslogin06.rc.fas.harvard.edu.snapshot.ebr.gz",
                            "boslogin06.rc.fas.harvard.edu"),
                           ("login.rc.fas.ebr", "login.rc.fas")):
            self.assertEqual(feedfile.host_of("/r/2026-01-01/" + name), host, name)

    def test_twin_names(self):
        self.assertEqual(feedfile.twin_names("/d/h.exits.jsonl.gz"),
                         ["/d/h.exits.ebr", "/d/h.exits.ebr.gz"])
        self.assertEqual(feedfile.twin_names("/d/n1.ebr"), ["/d/n1.jsonl", "/d/n1.jsonl.gz"])

    def test_prefer_of(self):
        tree = configmod.defaults()
        cfg = configmod.Config(tree, {}, None)
        self.assertEqual(feedfile.prefer_of(cfg, "ebpf"), "jsonl")
        tree["feeds"]["ebpf"]["prefer"] = " EBR "
        self.assertEqual(feedfile.prefer_of(cfg, "ebpf"), "ebr")
        tree["feeds"]["ebpf_node"]["prefer"] = "xml"
        self.assertEqual(feedfile.prefer_of(cfg, "ebpf_node"), "jsonl")
        self.assertEqual(feedfile.prefer_of(None), "jsonl")


class TwinRuleTest(TmpCase):
    """Every row of the rule (`feedfile` docstring)."""

    def choose(self, prefer, now=None):
        files = feedfile.feed_files(self.dir, prefer, now=now)
        self.assertLessEqual(len(files), 1)
        return os.path.basename(files[0]) if files else None

    def reason(self, prefer, now=None):
        stem = "h.exits"
        cands = {feedfile.format_of(p): p for p in
                 (self.p(n) for n in os.listdir(self.dir)) if not feedfile.split_name(p)[2]}
        return feedfile.choose(self.dir, stem, cands, prefer, now)[1]

    def test_one_file_is_read_whatever_it_holds(self):
        open(self.p("h.exits.ebr"), "wb").close()
        self.assertEqual(self.choose("jsonl"), "h.exits.ebr")
        os.unlink(self.p("h.exits.ebr"))
        jsonl_write(self.p("h.exits.jsonl"), [rec(T0, 1)])
        self.assertEqual(self.choose("ebr"), "h.exits.jsonl")

    def test_neither_twin_with_a_record_is_held(self):
        open(self.p("h.exits.ebr"), "wb").close()
        open(self.p("h.exits.jsonl"), "wb").close()
        for prefer in ("jsonl", "ebr"):
            notes = []
            self.assertEqual(feedfile.feed_files(self.dir, prefer, report=notes), [])
            self.assertEqual([(n["stream"], n["read"]) for n in notes], [("h.exits", None)])
            self.assertIn("neither", notes[0]["reason"])

    def test_a_line_or_frame_still_being_written_is_no_record_yet(self):
        frame = ebr_bytes([rec(T0, 1)])
        with open(self.p("h.exits.ebr"), "wb") as fh:
            fh.write(frame[:-3])
        jsonl_write(self.p("h.exits.jsonl"), [], raw=json.dumps(rec(T0, 1)).encode())
        self.assertIsNone(self.choose("jsonl", now=T0 + 10))
        self.assertIsNone(self.choose("ebr", now=T0 + 10))

    def test_the_preferred_twin_with_a_record_is_read(self):
        recs = [rec(T0 + i, i) for i in range(5)]
        jsonl_write(self.p("h.exits.jsonl"), recs)
        ebr_write(self.p("h.exits.ebr"), recs)
        self.assertEqual(self.choose("jsonl"), "h.exits.jsonl")
        self.assertEqual(self.choose("ebr"), "h.exits.ebr")
        self.assertIsNone(self.reason("ebr"))

    def test_the_preferred_twin_is_read_while_the_other_has_none(self):
        jsonl_write(self.p("h.exits.jsonl"), [rec(T0, 1)])
        open(self.p("h.exits.ebr"), "wb").close()
        self.assertEqual(self.choose("jsonl", now=T0 + 1), "h.exits.jsonl")
        for n in os.listdir(self.dir):
            os.unlink(self.p(n))
        forget()
        ebr_write(self.p("h.exits.ebr"), [rec(T0, 1)])
        open(self.p("h.exits.jsonl"), "wb").close()
        self.assertEqual(self.choose("ebr", now=T0 + 1), "h.exits.ebr")

    def test_a_preferred_twin_started_mid_day_reads_the_other(self):
        jsonl_write(self.p("h.exits.jsonl"), [rec(T0 + i, i) for i in range(10)])
        ebr_write(self.p("h.exits.ebr"), [rec(T0 + 3600, 99)])
        self.assertEqual(self.choose("ebr"), "h.exits.jsonl")
        why = self.reason("ebr")
        self.assertIn("mid-day", why)
        self.assertIn("60 min", why)
        # and the other way round
        forget()
        self.assertEqual(self.choose("jsonl"), "h.exits.jsonl")

    def test_within_a_second_is_the_same_start(self):
        jsonl_write(self.p("h.exits.jsonl"), [rec(T0, 1)])
        ebr_write(self.p("h.exits.ebr"), [rec(T0 + 0.9, 2)])
        self.assertEqual(self.choose("ebr"), "h.exits.ebr")
        forget()
        os.unlink(self.p("h.exits.ebr"))
        ebr_write(self.p("h.exits.ebr"), [rec(T0 + 1.5, 2)])
        self.assertEqual(self.choose("ebr"), "h.exits.jsonl")

    def test_only_the_other_with_a_record_is_held_then_read(self):
        jsonl_write(self.p("h.exits.jsonl"), [rec(T0, 1)])
        open(self.p("h.exits.ebr"), "wb").close()
        notes = []
        self.assertEqual(feedfile.feed_files(self.dir, "ebr", now=T0 + 299, report=notes), [])
        self.assertIn("held", notes[0]["reason"])
        self.assertEqual(self.choose("ebr", now=T0 + 300), "h.exits.jsonl")
        self.assertIn("300 s", self.reason("ebr", now=T0 + 300))

    def test_compressed_twins_and_a_plain_file_over_its_gz(self):
        recs = [rec(T0 + i, i) for i in range(5)]
        jsonl_write(self.p("h.exits.jsonl"), recs)
        ebr_write(self.p("h.exits.ebr"), recs)
        gzip_file(self.p("h.exits.jsonl"))
        gzip_file(self.p("h.exits.ebr"))
        self.assertEqual(self.choose("ebr"), "h.exits.ebr.gz")
        self.assertEqual(self.choose("jsonl"), "h.exits.jsonl.gz")
        # a plain file beside its .gz (being compressed) is the one read
        ebr_write(self.p("h.exits.ebr"), recs)
        self.assertEqual(self.choose("ebr"), "h.exits.ebr")
        # the first record of a .gz is streamed from its head: no seek index built
        gz._indexes.clear()
        forget()
        self.assertEqual(feedfile.first_record(self.p("h.exits.jsonl.gz")), (True, T0))
        self.assertEqual(feedfile.first_record(self.p("h.exits.ebr.gz")), (True, T0))
        self.assertEqual(len(gz._indexes), 0)


def ebr_bytes(recs, **kw):
    import io
    bio = io.BytesIO()
    w = ebr.Writer(bio, **kw)
    for r in recs:
        w.write(r)
    w.close()
    return bio.getvalue()


class BothWriterTest(TmpCase):
    """The collector in `both` mode (`DailyWriter`): every sink opened at the
    day's start, then each poll round written to the JSONL twin, then the EBR
    one. A reader listing at any point never takes the other twin and later
    the preferred one -- nor ever changes a choice made."""

    def run_day(self, prefer, ebr_late_s=0.0, rounds=6, gap=60.0):
        j, e = self.p("h.exits.jsonl"), self.p("h.exits.ebr")
        open(j, "wb").close()
        open(e, "wb").close()
        seen = []

        def look(now):
            files = feedfile.feed_files(self.dir, prefer, now=now)
            seen.append(feedfile.format_of(files[0]) if files else None)

        look(T0)
        pending = []
        for k in range(rounds):
            t = T0 + k * gap
            recs = [rec(t + i, 100 * k + i) for i in range(3)]
            jsonl_write(j, recs)
            look(t + 0.01)
            pending.append(recs)
            if t >= T0 + ebr_late_s:       # an EBR writer that fell behind catches up
                for r in pending:
                    ebr_write(e, r)
                pending = []
            look(t + 0.02)
        return seen

    def check(self, seen, prefer):
        other = "ebr" if prefer == "jsonl" else "jsonl"
        made = [s for s in seen if s is not None]
        self.assertTrue(made, seen)
        self.assertEqual(len(set(made)), 1, "a choice changed: %s" % seen)
        if other in seen:
            self.assertNotIn(prefer, seen[seen.index(other):], seen)

    def test_the_preferred_twin_is_read_from_the_first_round(self):
        for prefer in ("jsonl", "ebr"):
            with self.subTest(prefer=prefer):
                forget()
                for n in os.listdir(self.dir):
                    os.unlink(self.p(n))
                seen = self.run_day(prefer)
                self.check(seen, prefer)
                self.assertEqual(seen[-1], prefer)

    def test_an_ebr_writer_behind_by_more_than_the_hold_is_never_switched_to(self):
        seen = self.run_day("ebr", ebr_late_s=6 * 60.0, rounds=8, gap=60.0)
        self.check(seen, "ebr")
        self.assertEqual(seen[-1], "jsonl")
        # held while the preferred twin could still be coming, then the other
        self.assertIsNone(seen[1])

    def test_an_ebr_writer_behind_within_the_hold_is_waited_for(self):
        seen = self.run_day("ebr", ebr_late_s=2 * 60.0, rounds=5, gap=60.0)
        self.check(seen, "ebr")
        self.assertEqual(seen[-1], "ebr")
        self.assertNotIn("jsonl", seen)


class MemoTest(TmpCase):

    def test_a_restart_and_a_compression_make_the_same_choice(self):
        jsonl_write(self.p("h.exits.jsonl"), [rec(T0 + i, i) for i in range(10)])
        ebr_write(self.p("h.exits.ebr"), [rec(T0 + 3600 + i, i) for i in range(3)])
        first = feedfile.feed_files(self.dir, "ebr")
        forget()
        self.assertEqual(feedfile.feed_files(self.dir, "ebr"), first)
        gzip_file(self.p("h.exits.jsonl"))
        gzip_file(self.p("h.exits.ebr"))
        self.assertEqual(feedfile.feed_files(self.dir, "ebr"), [first[0] + ".gz"])

    def test_a_choice_made_is_kept_for_the_same_files(self):
        # read from the preferred twin while the other had no record; the other
        # then shows a first record far older (a twin filled in after the fact)
        jsonl_write(self.p("h.exits.jsonl"), [rec(T0 + 7200, 1)])
        open(self.p("h.exits.ebr"), "wb").close()
        self.assertEqual(os.path.basename(feedfile.feed_files(self.dir, "jsonl")[0]),
                         "h.exits.jsonl")
        ebr_write(self.p("h.exits.ebr"), [rec(T0, 1)])
        self.assertEqual(os.path.basename(feedfile.feed_files(self.dir, "jsonl")[0]),
                         "h.exits.jsonl")

    def test_chosen_for(self):
        jsonl_write(self.p("h.exits.jsonl"), [rec(T0, 1)])
        ebr_write(self.p("h.exits.ebr"), [rec(T0, 1)])
        self.assertEqual(feedfile.chosen_for(self.p("h.exits.jsonl"), "ebr"),
                         self.p("h.exits.ebr"))
        self.assertEqual(feedfile.chosen_for(self.p("h.exits.ebr.gz"), "jsonl"),
                         self.p("h.exits.jsonl"))
        self.assertIsNone(feedfile.chosen_for(self.p("other.exits.jsonl"), "jsonl"))


class DatedDirsTest(TmpCase):

    def test_a_held_stem_counts_as_a_day(self):
        old = os.path.join(self.dir, "2026-01-01")
        new = os.path.join(self.dir, "2026-01-02")
        os.makedirs(old)
        os.makedirs(new)
        jsonl_write(os.path.join(old, "h.exits.jsonl"), [rec(T0, 1)])
        open(os.path.join(new, "h.exits.jsonl"), "wb").close()
        open(os.path.join(new, "h.exits.ebr"), "wb").close()
        notes = []
        got = feedfile.dated_dirs(self.dir, 1, "ebr", report=notes)
        self.assertEqual(got, [("2026-01-02", [])])
        self.assertEqual([(n["day"], n["read"]) for n in notes], [("2026-01-02", None)])
        self.assertEqual([d for d, _ in feedfile.dated_dirs(self.dir, 0, "ebr")],
                         ["2026-01-01", "2026-01-02"])
        # an empty date dir still is not one
        os.makedirs(os.path.join(self.dir, "2026-01-03"))
        self.assertEqual(feedfile.dated_dirs(self.dir, 1, "ebr"), [("2026-01-02", [])])

    def test_day_files_lists_one_file_per_stream(self):
        d = os.path.join(self.dir, "2026-01-01")
        os.makedirs(d)
        for host in ("h1", "h2"):
            for stream in ("exits", "snapshot"):
                recs = [rec(T0 + i, i) for i in range(3)]
                jsonl_write(os.path.join(d, "%s.%s.jsonl" % (host, stream)), recs)
                ebr_write(os.path.join(d, "%s.%s.ebr" % (host, stream)), recs)
        for prefer in ("jsonl", "ebr"):
            got = feedfile.day_files([self.dir], 0, prefer)
            self.assertEqual(len(got), 4)
            self.assertEqual({feedfile.format_of(p) for p in got}, {prefer})


class EbrPrimitivesTest(TmpCase):
    """The time searches and boundaries of an EBR day-file, against brute force."""

    def setUp(self):
        super().setUp()
        self.path = self.p("h.exits.ebr")
        # rounds 30 s apart, each one frame of 7 records, a little out of order
        # within the round; small segments, so a seek crosses many of them
        self.recs = []
        for k in range(120):
            t = T0 + 30 * k
            rr = [rec(t + (i * 7) % 5, 10 * k + i) for i in range(7)]
            ebr_write(self.path, rr, reset_bytes=4096)
            self.recs += rr
        self.size = os.path.getsize(self.path)
        self.frames = [f for f in ebr.frames(self.path) if f.kind == ebr.DATA]

    def seek(self, since, path=None, fmt="ebr"):
        with gz.open_feed(path or self.path) as fh:
            return feedfile.seek_to_epoch(fh, since, gz.fstat(fh).st_size, fmt)

    def end(self, t, path=None):
        with gz.open_feed(path or self.path) as fh:
            return feedfile.end_at(fh, t, gz.fstat(fh).st_size, "ebr")

    def test_the_start_bound_is_the_first_frame_reaching_since(self):
        for since in (T0 - 100, T0, T0 + 31, T0 + 1000.5, T0 + 3569, T0 + 3575, T0 + 9999):
            got = self.seek(since)
            want = next((f.off for f in self.frames if f.ts_max >= since), self.size)
            # the search lands on the frame, or on the SEGMENT just before it
            self.assertLessEqual(got, want, since)
            self.assertTrue(all(f.ts_max < since for f in self.frames if f.off < got), since)
            with gz.open_feed(self.path) as fh:
                self.assertTrue(feedfile.is_boundary(fh, got, "ebr"), since)

    def test_the_end_bound_is_the_first_frame_entirely_after_t(self):
        for t in (T0 - 100, T0, T0 + 31, T0 + 1000.5, T0 + 3569, T0 + 9999):
            got = self.end(t)
            want = next((f.off for f in self.frames if f.ts_min > t), self.size)
            self.assertLessEqual(got, want, t)
            self.assertTrue(all(f.ts_min <= t for f in self.frames if f.end <= got), t)
            # nothing at or before t lies past it
            self.assertTrue(all(f.ts_min > t for f in self.frames if f.off >= got), t)

    def test_the_same_answers_from_its_gz(self):
        plain = [self.seek(T0 + 1000), self.end(T0 + 1000)]
        zp = gzip_file(self.path)
        self.assertEqual([self.seek(T0 + 1000, zp), self.end(T0 + 1000, zp)], plain)

    def test_a_frame_its_header_cannot_place_stays_inside_both_bounds(self):
        # One record with no `ts_epoch` makes a frame's bounds (-inf, +inf). A
        # probe walks past it to a frame it can place, as one walks past lines
        # with no time; the bounds keep the frame inside the read either way.
        p = self.p("u.exits.ebr")
        for k in range(40):
            rr = [rec(T0 + 30 * k + i, i) for i in range(3)]
            if k == 25:
                rr.append({"event": "exit", "pid": 1})          # no ts_epoch
            ebr_write(p, rr)
        frames = [f for f in ebr.frames(p) if f.kind == ebr.DATA]
        odd = frames[25]
        self.assertEqual((odd.ts_min, odd.ts_max), (float("-inf"), float("inf")))
        self.assertLessEqual(self.seek(T0 + 30 * 25 + 1, p), odd.off)
        self.assertGreaterEqual(self.end(T0 + 30 * 25 + 1, p), odd.end)

    def test_records_from_either_end(self):
        since = T0 + 1000
        start = self.seek(since)
        fwd = [r["pid"] for _p, r in feedfile.records_forward(self.path, start)]
        every = [r["pid"] for r in self.recs]
        self.assertEqual(fwd, every[len(every) - len(fwd):])
        back = [r["pid"] for _p, r in feedfile.records_reverse(self.path, self.size, start)]
        self.assertEqual(back, fwd[::-1])
        self.assertEqual([r["pid"] for r in feedfile.last_records(self.path, 3)], every[-3:])
        self.assertEqual(feedfile.newest(self.path, lambda r: r["pid"] if r["pid"] % 10 == 2
                                         else None), 1192)
        kinds = [r["pid"] for _p, r in feedfile.records_forward(
            self.path, start, kinds={"exit"}, where={"user": {"u1"}})]
        self.assertEqual(kinds, [p for p in fwd if p % 3 == 1])

    def test_a_pending_tail_and_a_corrupt_region_near_the_end(self):
        frame = ebr_bytes([rec(T0 + 9000, 1)])
        with open(self.path, "ab") as fh:
            fh.write(frame[:30])                                    # still being written
        size = os.path.getsize(self.path)
        with gz.open_feed(self.path) as fh:
            self.assertEqual(feedfile.complete_end(fh, size, fmt="ebr"), self.size)
            self.assertFalse(feedfile.is_boundary(fh, size, "ebr"))
        # bytes not yet visible (zeros), then a whole frame: before the hole
        # with settle, after the frame without
        with open(self.path, "ab") as fh:
            fh.write(b"\0" * 100 + frame)
        size = os.path.getsize(self.path)
        with gz.open_feed(self.path) as fh:
            self.assertEqual(feedfile.complete_end(fh, size, fmt="ebr", settle=feedfile.SETTLE),
                             self.size)
            self.assertEqual(feedfile.complete_end(fh, size, fmt="ebr"), size)

    def test_the_settled_end_is_the_frame_by_frame_walks(self):
        # the walk settled_end did frame by frame before ebr.verify_back checked
        # the frames in one loop first: the same end, whatever the damage
        def walked(src, size, settle):
            end = ebr.complete_end(src, size)
            if not settle or not end:
                return end
            cut = end
            for f in ebr.frames_reverse(src, end, verify=True):
                if f.kind == ebr.BAD:
                    cut = f.off
                if f.off <= end - settle:
                    break
            return cut
        with open(self.path, "rb") as fh:
            data = fh.read()
        rng = random.Random(11)
        bad = self.p("bad.exits.ebr")
        for trial in range(120):
            b = bytearray(data)
            at = len(b) - rng.randrange(1, 6000)
            if trial % 3 == 0:
                b[at:at + 100] = bytes(100)[:len(b[at:at + 100])]      # not yet visible
            else:
                b[at] ^= 1 + rng.randrange(255)
            with open(bad, "wb") as fh:
                fh.write(bytes(b))
            for settle in (0, 512, 4096, feedfile.SETTLE):
                self.assertEqual(feedfile.settled_end(bad, len(b), settle),
                                 walked(bad, len(b), settle), (trial, settle))

    def test_reading_back_a_file_that_is_gone(self):
        self.assertEqual(list(feedfile.records_reverse(self.p("gone.exits.ebr"), 100)), [])
        zp = gzip_file(self.path)
        got = [r["pid"] for _p, r in feedfile.records_reverse(zp, self.size)]
        self.assertEqual(got, [r["pid"] for r in self.recs][::-1])

    def test_a_decoder_at_a_place_that_is_no_frames_end(self):
        f = self.frames[40]
        dec = feedfile.decoder_at(self.path, f.off + 3)         # inside a frame
        out = []
        for g in ebr.frames(self.path, ebr.resync(self.path, f.off + 3)):
            out += [r["pid"] for _p, r in dec.records(self.path, g)]
        # nothing until the next SEGMENT, everything after it
        nxt = next(g for g in ebr.frames(self.path, f.end) if g.kind == ebr.SEGMENT)
        after = [r["pid"] for _p, r in feedfile.records_forward(self.path, nxt.off)]
        self.assertEqual(out, after)
        self.assertGreater(feedfile.errors(dec.stats), 0)
        good = feedfile.decoder_at(self.path, f.off)
        self.assertEqual(feedfile.errors(good.stats), 0)

    def test_first_record(self):
        self.assertEqual(feedfile.first_record(self.path), (True, T0))
        empty = self.p("e.exits.ebr")
        open(empty, "wb").close()
        self.assertEqual(feedfile.first_record(empty), (False, None))
        self.assertEqual(feedfile.first_record(self.p("missing.ebr")), (False, None))


if __name__ == "__main__":
    unittest.main()
