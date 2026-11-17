"""The day-file layer (`feedfile`): names, and which file of a stream is read.

A stream's day-file is `x.jsonl` while its day is live and `x.jsonl.gz` once
the collector has sealed it; both exist for the moment the compression takes,
and the plain one, the complete copy, is read then. These tests write the files
the collector writes and check what the listing (`feed_files`, `dated_dirs`,
`day_files`, `chosen_for`) reads of them.

    cd dashboard && uv run python -m unittest tests.test_feedfile -q
"""
import gzip
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rc_dashboard import feedfile                 # noqa: E402

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


def gzip_file(path):
    with open(path, "rb") as src, gzip.open(path + ".gz", "wb") as dst:
        shutil.copyfileobj(src, dst)
    os.unlink(path)
    return path + ".gz"


class TmpCase(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="rc_feedfile_")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def p(self, name):
        return os.path.join(self.dir, name)


class NamesTest(unittest.TestCase):

    def test_split_name(self):
        self.assertEqual(feedfile.split_name("/a/2026-01-01/h.exits.jsonl.gz"),
                         ("h.exits", "jsonl", True))
        self.assertEqual(feedfile.split_name("h.snapshot.jsonl"), ("h.snapshot", "jsonl", False))
        self.assertEqual(feedfile.split_name("/x/n1.jsonl.gz"), ("n1", "jsonl", True))
        self.assertEqual(feedfile.split_name("notes.txt"), ("notes.txt", None, False))
        self.assertEqual(feedfile.split_name("h.snapshot.ebr"), ("h.snapshot.ebr", None, False))

    def test_host_of_strips_only_a_known_stream(self):
        for name, host in (("h1.exits.jsonl", "h1"), ("h1.snapshot.jsonl.gz", "h1"),
                           ("n1.jsonl", "n1"), ("n1.jsonl.gz", "n1"),
                           ("login.rc.fas.exits.jsonl", "login.rc.fas"),
                           ("boslogin06.rc.fas.harvard.edu.snapshot.jsonl.gz",
                            "boslogin06.rc.fas.harvard.edu"),
                           ("login.rc.fas.jsonl", "login.rc.fas")):
            self.assertEqual(feedfile.host_of("/r/2026-01-01/" + name), host, name)


class ListingTest(TmpCase):

    def names(self):
        return [os.path.basename(p) for p in feedfile.feed_files(self.dir)]

    def test_a_streams_file_is_read_whatever_it_holds(self):
        # nothing yet, then a line still being written: the tail follows the
        # file from the start either way
        open(self.p("h.exits.jsonl"), "wb").close()
        self.assertEqual(self.names(), ["h.exits.jsonl"])
        jsonl_write(self.p("h.exits.jsonl"), [], raw=json.dumps(rec(T0, 1)).encode())
        self.assertEqual(self.names(), ["h.exits.jsonl"])

    def test_a_sealed_day_is_its_gz_and_a_plain_file_wins_over_its_gz(self):
        recs = [rec(T0 + i, i) for i in range(5)]
        jsonl_write(self.p("h.exits.jsonl"), recs)
        gzip_file(self.p("h.exits.jsonl"))
        self.assertEqual(self.names(), ["h.exits.jsonl.gz"])
        # a plain file beside its .gz (being compressed) is the one read
        jsonl_write(self.p("h.exits.jsonl"), recs)
        self.assertEqual(self.names(), ["h.exits.jsonl"])

    def test_only_day_files_are_listed(self):
        jsonl_write(self.p("h.exits.jsonl"), [rec(T0, 1)])
        for name in ("notes.txt", "h.exits.ebr", ".h.snapshot.jsonl"):
            open(self.p(name), "wb").close()
        self.assertEqual(self.names(), ["h.exits.jsonl"])

    def test_chosen_for(self):
        jsonl_write(self.p("h.exits.jsonl"), [rec(T0, 1)])
        self.assertEqual(feedfile.chosen_for(self.p("h.exits.jsonl")), self.p("h.exits.jsonl"))
        # while both exist, the plain file; once sealed, its .gz
        with gzip.open(self.p("h.exits.jsonl.gz"), "wb") as fh:
            fh.write(b"")
        self.assertEqual(feedfile.chosen_for(self.p("h.exits.jsonl.gz")), self.p("h.exits.jsonl"))
        os.unlink(self.p("h.exits.jsonl"))
        self.assertEqual(feedfile.chosen_for(self.p("h.exits.jsonl")),
                         self.p("h.exits.jsonl.gz"))
        self.assertIsNone(feedfile.chosen_for(self.p("other.exits.jsonl")))


class DatedDirsTest(TmpCase):

    def test_a_dir_of_empty_day_files_counts_as_a_day_and_an_empty_dir_does_not(self):
        old = os.path.join(self.dir, "2026-01-01")
        new = os.path.join(self.dir, "2026-01-02")
        os.makedirs(old)
        os.makedirs(new)
        jsonl_write(os.path.join(old, "h.exits.jsonl"), [rec(T0, 1)])
        open(os.path.join(new, "h.exits.jsonl"), "wb").close()
        self.assertEqual(feedfile.dated_dirs(self.dir, 1),
                         [("2026-01-02", [os.path.join(new, "h.exits.jsonl")])])
        self.assertEqual([d for d, _ in feedfile.dated_dirs(self.dir, 0)],
                         ["2026-01-01", "2026-01-02"])
        # an empty date dir is not one
        os.makedirs(os.path.join(self.dir, "2026-01-03"))
        self.assertEqual([d for d, _ in feedfile.dated_dirs(self.dir, 1)], ["2026-01-02"])

    def test_day_files_lists_one_file_per_stream(self):
        d = os.path.join(self.dir, "2026-01-01")
        os.makedirs(d)
        for host in ("h1", "h2"):
            for stream in ("exits", "snapshot"):
                jsonl_write(os.path.join(d, "%s.%s.jsonl" % (host, stream)),
                            [rec(T0 + i, i) for i in range(3)])
        # h1's streams are being compressed: both copies on disk
        for stream in ("exits", "snapshot"):
            p = os.path.join(d, "h1.%s.jsonl" % stream)
            with open(p, "rb") as src, gzip.open(p + ".gz", "wb") as dst:
                shutil.copyfileobj(src, dst)
        # h2's are sealed
        for stream in ("exits", "snapshot"):
            gzip_file(os.path.join(d, "h2.%s.jsonl" % stream))
        got = feedfile.day_files([self.dir], 0)
        self.assertEqual([os.path.relpath(p, self.dir) for p in got],
                         ["2026-01-01/h1.exits.jsonl", "2026-01-01/h1.snapshot.jsonl",
                          "2026-01-01/h2.exits.jsonl.gz", "2026-01-01/h2.snapshot.jsonl.gz"])


if __name__ == "__main__":
    unittest.main()
