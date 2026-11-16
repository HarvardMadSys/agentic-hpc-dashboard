"""Ingest tests: what the page can show must depend on the collector's files alone.

Runs anywhere: no collector, no cluster, no network. Each test writes a small
feed into a scratch directory laid out the way the collector lays it out --
`<root>/<date>/<host>.{exits,snapshot}.jsonl` -- and drives the Aggregator
through the calls `app.py` makes.

`FeedCase` writes either format the collector does: JSONL, or EBR binary
records in its EBR twin (`ebr_twins`), which runs every test of a case again on
`.ebr` day-files. A test about JSONL's own bytes is marked `@jsonl_only(why)`.

    cd dashboard && .venv/bin/python -m unittest discover -s tests -q
"""
import gzip
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rc_dashboard import config as configmod      # noqa: E402
from rc_dashboard import ebr                      # noqa: E402
from rc_dashboard.aggregator import Aggregator     # noqa: E402
from rc_dashboard import tail                     # noqa: E402

TS = "%Y-%m-%d %H:%M:%S"
DAY = "2026-09-25"
NONE = {"agent": 0, "human": 0, "ide": 0, "unlabeled": 0}


def stamp(epoch):
    return time.strftime(TS, time.localtime(epoch))


def exit_rec(epoch, pid, actor3="agent", host="h1"):
    return {"schema_version": 6, "event": "exit", "ts": stamp(epoch),
            "ts_epoch": epoch, "host": host, "user": "alice", "actor3": actor3,
            "agent_type": "claude_code" if actor3 == "agent" else None,
            "comm": "git", "args": "git status", "pid": pid, "ppid": 1,
            "cpu_s": 0.5, "duration_s": 1.0}


def totals_rec(epoch, n_procs, host="h1"):
    return {"schema_version": 6, "event": "residency_totals", "ts": stamp(epoch),
            "ts_epoch": epoch, "host": host,
            "by_actor3": {"agent": {"n_procs": n_procs, "threads": n_procs,
                                    "rss_mb": 100.0, "d_state": 0, "users": 1}},
            "by_agent_type": {}, "trees": 1, "tracked_live": n_procs}


def line(rec):
    return json.dumps(rec).encode() + b"\n"


def ebr_name(name):
    """A JSONL day-file's name, as its EBR twin's: `x.jsonl[.gz]` is `x.ebr[.gz]`."""
    for suf in (".jsonl", ".jsonl.gz"):
        if name.endswith(suf):
            return name[:-len(suf)] + suf.replace(".jsonl", ".ebr")
    return name


# Records per EBR frame a FeedCase writes: a poll round of a busy host. One
# append is still one flush (one os.write), its frames cut at this many; a test
# that wants a frame spanning hours says so (test_formats). And a segment every
# 16 KiB, not every 2 MiB: a test's day-file is small, and its reads should
# still cross segments.
FRAME_RECORDS = 16
RESET_BYTES = 16 << 10


def ebr_rounds(rounds, frame_records=FRAME_RECORDS):
    """The bytes a collector's EBR writer appends for each poll round, as one
    writer would write them in turn: the first opens a segment, and each is a
    flush of frames. Appended to a day-file at a frame's end, they read as
    written."""
    out, bio = [], io.BytesIO()
    w = ebr.Writer(bio, max_frame_records=frame_records, reset_bytes=RESET_BYTES)
    for recs in rounds:
        at = bio.tell()
        for r in recs:
            w.write(r)
        w.flush()
        out.append(bio.getvalue()[at:])
    w.close()
    return out


def jsonl_only(why):
    """A test about JSONL's own bytes (a line's regex, a memory map): the EBR
    twin of its case (`ebr_twins`) skips it, saying why."""
    def mark(fn):
        fn._jsonl_only = why
        return fn
    return mark


def ebr_twins(ns):
    """Add to the module namespace `ns` an EBR twin of every FeedCase subclass
    defined there: `<Case>Ebr`, every one of its tests again on `.ebr`
    day-files (`FeedCase.FORMAT`). Called at the end of the module."""
    for name, cls in list(ns.items()):
        if not (isinstance(cls, type) and issubclass(cls, FeedCase) and cls is not FeedCase
                and cls.__module__ == ns["__name__"] and cls.FORMAT == "jsonl" and cls.TWIN):
            continue
        body = {"FORMAT": "ebr", "__module__": ns["__name__"],
                "__qualname__": name + "Ebr"}
        for attr in dir(cls):
            fn = getattr(cls, attr, None)
            why = getattr(fn, "_jsonl_only", None)
            if attr.startswith("test") and why:
                body[attr] = unittest.skip("JSONL only: %s" % why)(fn)
        ns[name + "Ebr"] = type(name + "Ebr", (cls,), body)


class FeedCase(unittest.TestCase):
    """A scratch collector root, and a config that reads only from it.

    `FORMAT` is the format it writes its day-files in: `jsonl`, or `ebr` in a
    case's EBR twin (`ebr_twins`). Every name is given as the JSONL one and
    mapped (`fname`), so a test reads the same in either."""

    FORMAT = "jsonl"
    FRAME_RECORDS = FRAME_RECORDS
    TWIN = True             # False: `ebr_twins` makes none (a case that writes both itself)

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rc_ingest_")
        self.root = os.path.join(self.tmp, "ebpf")
        os.makedirs(os.path.join(self.root, DAY))
        self.now = time.time()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def fname(self, name):
        """`name`, a JSONL day-file's, in this case's format."""
        return ebr_name(name) if self.FORMAT == "ebr" else name

    def path(self, name, day=DAY):
        return os.path.join(self.root, day, self.fname(name))

    def append(self, name, recs, raw=b"", day=DAY):
        """One poll round: `recs` appended as the collector writes them. EBR:
        one `open_append`, the records, one flush -- a frame. `raw` is bytes
        written after them (a round cut short: see `rounds`)."""
        os.makedirs(os.path.join(self.root, day), exist_ok=True)
        if self.FORMAT == "ebr":
            if recs:
                w = ebr.Writer.open_append(self.path(name, day),
                                           max_frame_records=self.FRAME_RECORDS,
                                           reset_bytes=RESET_BYTES)
                try:
                    for r in recs:
                        w.write(r)
                    w.flush()
                finally:
                    w.close()
            if raw:
                self.write_raw(name, raw, day)
            return
        with open(self.path(name, day), "ab") as fh:
            for r in recs:
                fh.write(line(r))
            fh.write(raw)

    def rounds(self, rounds):
        """The bytes each of `rounds` (lists of records) adds to a day-file in
        this case's format, written one after another: for writing a round
        only part way (`write_raw`), as a reader can find one mid-write."""
        if self.FORMAT == "ebr":
            return ebr_rounds(rounds, self.FRAME_RECORDS)
        return [b"".join(line(r) for r in recs) for recs in rounds]

    def write_raw(self, name, data, day=DAY):
        os.makedirs(os.path.join(self.root, day), exist_ok=True)
        with open(self.path(name, day), "ab") as fh:
            fh.write(data)

    def records(self, name, day=DAY):
        """Every record of a day-file, in file order."""
        p = self.path(name, day)
        if self.FORMAT == "ebr":
            return [r for _p, r in ebr.iter_records(p)]
        opener = gzip.open if p.endswith(".gz") else open
        with opener(p, "rb") as fh:
            return [json.loads(x) for x in fh if x.strip()]

    def replace_bytes(self, name, old, new, day=DAY):
        """The day-file deleted and written again with the first `old` in its
        records' JSON made `new`: the same records but for that, in the same
        rounds (EBR: the same frames)."""
        p = self.path(name, day)
        if self.FORMAT == "jsonl":
            with open(p, "rb") as fh:
                data = fh.read()
            os.unlink(p)
            with open(p, "wb") as fh:
                fh.write(data.replace(old, new, 1))
            return
        with open(p, "rb") as fh:
            data = fh.read()
        dec, groups, done = ebr.Decoder(), [], False
        for f in ebr.frames(data):
            recs = []
            for _pos, r in dec.records(data, f):
                text = json.dumps(r).encode()
                if not done and old in text:
                    r, done = json.loads(text.replace(old, new, 1)), True
                recs.append(r)
            if f.kind == ebr.DATA:
                groups.append(recs)
        os.unlink(p)
        w = ebr.Writer(p, max_frame_records=self.FRAME_RECORDS, reset_bytes=RESET_BYTES)
        for recs in groups:
            for r in recs:
                w.write(r)
            w.flush()
        w.close()

    def write_gz(self, name, dest_dir, day=DAY):
        """A gzip-compressed copy of day-file `name` written into `dest_dir`,
        under its compressed name, as a capture copied in from elsewhere."""
        dst = os.path.join(dest_dir, self.fname(name) + ".gz")
        with open(self.path(name, day), "rb") as src, gzip.open(dst, "wb") as out:
            shutil.copyfileobj(src, out)
        return dst

    def compress(self, name, day=DAY):
        """What the collector does to a sealed day (`_compress_day`, `gzip -f`):
        the `.gz` written beside the file, then the file unlinked -- the same
        bytes under a new name and a new inode. For either format."""
        p = self.path(name, day)
        with open(p, "rb") as src, gzip.open(p + ".gz", "wb") as dst:
            shutil.copyfileobj(src, dst)
        shutil.copystat(p, p + ".gz")
        os.unlink(p)

    def cfg(self, **live):
        tree = configmod.defaults()
        tree["feeds"]["ebpf"]["roots"] = [self.root]
        # A twin of a stream in the other format is never written here; read
        # this case's own all the same.
        tree["feeds"]["ebpf"]["prefer"] = self.FORMAT
        # The node tier is not under test: point it at nothing.
        tree["feeds"]["ebpf_node"]["roots"] = [os.path.join(self.tmp, "node")]
        # Where the retired offset store lived. Scratch, so a regression that
        # brings it back is caught by these tests instead of writing into the
        # repo tree.
        tree["cache"]["state_dir"] = os.path.join(self.tmp, "state")
        # Saved windows too: scratch, never the repo tree, and never another
        # test's.
        tree["cache"]["dir"] = os.path.join(self.tmp, "cache")
        # Windows built by scanning, which is what these tests are about;
        # test_hours.py builds them from saved hours.
        tree["panels"]["merge_reduce"] = False
        tree["live"].update(live)
        return configmod.Config(tree, {}, None)

    def thirty_exits(self):
        """One exit every 5 minutes over the last 150 minutes, 20 agent + 10 human.

        Oldest first, the order the collector appends in: exit `i` is
        `5 * i + 1` minutes old.
        """
        return [exit_rec(self.now - 60 * (5 * i + 1), pid=1000 + i,
                         actor3="agent" if i % 3 else "human")
                for i in reversed(range(30))]

    @staticmethod
    def events(agg):
        return agg.live_payload(1440)["now"]["events"]


class RestartTest(FeedCase):

    def test_a_restart_serves_the_history_the_previous_run_already_read(self):
        # The reported bug: offsets survived a restart while the bins they fed
        # did not, so a second process resumed at EOF and showed nothing older
        # than its own start.
        self.append("h1.exits.jsonl", self.thirty_exits())
        cfg = self.cfg()

        first = Aggregator(cfg)
        first.backfill_now()
        first.poll_once()
        self.assertEqual(self.events(first), dict(NONE, agent=20, human=10))

        second = Aggregator(cfg)
        second.backfill_now()
        second.poll_once()
        self.assertEqual(self.events(second), dict(NONE, agent=20, human=10))

    def test_a_poll_that_runs_before_positioning_reads_nothing(self):
        # app.py polls while the backfill runs. A file the tail has not been
        # handed yet would be read from byte 0 -- and then read again by the
        # backfill, counting the whole history twice.
        self.append("h1.exits.jsonl", self.thirty_exits())
        agg = Aggregator(self.cfg())

        self.assertEqual(agg.poll_once(), 0)
        agg.backfill_now()
        agg.poll_once()
        self.assertEqual(self.events(agg), dict(NONE, agent=20, human=10))


class PositioningTest(FeedCase):

    def test_the_page_is_served_while_the_tail_is_positioned(self):
        # Positioning opens every file and, for a sealed day's .gz, builds its
        # seek index: on rtx6000b on 2026-11-13, 7 days of 8 nodes (128 files)
        # took 4 minutes. Held under the aggregator's lock, which the window
        # registry shares, it kept every read of the views and of the live tier
        # waiting -- and the event loop reads both -- so the page answered
        # nothing until it ended.
        self.append("h1.exits.jsonl", self.thirty_exits())
        agg = Aggregator(self.cfg())
        inside, release = threading.Event(), threading.Event()
        position = agg.source.position

        def slow(since):
            inside.set()
            release.wait(30)
            return position(since)
        agg.source.position = slow
        fill = threading.Thread(target=agg.backfill_now)
        fill.start()
        try:
            self.assertTrue(inside.wait(10))
            read = threading.Thread(target=lambda: (agg.panels_by_window.status(),
                                                    agg.live_payload(1440)))
            read.start()
            read.join(5)
            self.assertFalse(read.is_alive(), "reads waited for the positioning")
            # The page can now ask during those minutes, and is told what runs.
            self.assertEqual(agg.backfill["state"], "positioning")
            # The tail still reads nothing until every file is handed to it.
            self.assertEqual(agg.poll_once(), 0)
        finally:
            release.set()
            fill.join(30)
        self.assertEqual(agg.backfill["state"], "done")
        agg.poll_once()
        self.assertEqual(self.events(agg), dict(NONE, agent=20, human=10))


    def test_window_builds_wait_until_the_tail_is_positioned(self):
        # A build is pure Python and keeps the GIL; positioning inflates .gz
        # files in short C calls, and each must win the GIL back. Beside a build
        # it got 5% of a core on rtx6000b (2026-11-13), and the live tail, which
        # reads nothing until positioning ends, stood still as long. Positioning
        # takes minutes; the builds start when it ends.
        self.append("h1.exits.jsonl", self.thirty_exits())
        agg = Aggregator(self.cfg())
        inside, release = threading.Event(), threading.Event()
        position = agg.source.position

        def slow(since):
            inside.set()
            release.wait(30)
            return position(since)
        agg.source.position = slow
        agg.start()
        fill = threading.Thread(target=agg.backfill_now)
        fill.start()
        try:
            self.assertTrue(inside.wait(10))
            agg.panels(1440)                        # the page asks for its window
            time.sleep(1)
            held = {w["minutes"]: w for w in agg.panels_by_window.status()["held"]}
            self.assertEqual(held[1440]["state"], "queued")
        finally:
            release.set()
            fill.join(30)
        try:
            deadline = time.time() + 20
            while time.time() < deadline:
                held = {w["minutes"]: w for w in agg.panels_by_window.status()["held"]}
                if held[1440]["state"] in ("ready", "error"):
                    break
                time.sleep(0.05)
            self.assertEqual((held[1440]["state"], held[1440]["error"]), ("ready", None))
        finally:
            agg.stop()


class HandOffTest(FeedCase):

    def test_the_read_back_and_the_tail_split_each_file_at_the_positioning_point(self):
        # Three complete lines and the first half of a fourth, as the collector
        # leaves a file mid-write. The half line belongs to the tail: it is not
        # parseable yet, and positioning past it would lose it once complete.
        # (EBR: three records in a frame, and the first 20 bytes of the next.)
        t = self.now - 600
        recs = [exit_rec(t + 60 * i, pid=i) for i in range(1, 6)]
        first, fourth, fifth = self.rounds([recs[:3], recs[3:4], recs[4:5]])
        self.write_raw("h1.exits.jsonl", first + fourth[:20])

        src = tail.FileTailSource([self.root], days=8)
        plan = src.position(since=self.now - 3600)
        # Everything after this point is the tail's, including records written
        # while the read-back is still running.
        self.write_raw("h1.exits.jsonl", fourth[20:] + fifth)

        stats = {"bytes": 0, "parse_errors": 0}
        self.assertEqual([raw["pid"] for _ts, raw in tail.read_back(plan, stats)],
                         [3, 2, 1])
        self.assertEqual([raw["pid"] for raw in src.poll()], [4, 5])
        self.assertEqual([raw["pid"] for raw in src.poll()], [])


class BackfillTest(FeedCase):

    def test_the_backfill_leaves_each_hosts_newest_state_and_a_time_ordered_tail(self):
        # Hosts interleave in time. The event tail must interleave with them,
        # and each host's plate must show its NEWEST tick -- whichever order
        # the files are read in.
        m = 60
        for host, base in (("h1", 100), ("h2", 200)):
            self.append("%s.snapshot.jsonl" % host,
                        [totals_rec(self.now - 50 * m, 1, host),
                         totals_rec(self.now - 30 * m, 2, host),
                         totals_rec(self.now - 10 * m, 3, host)])
        self.append("h1.exits.jsonl", [exit_rec(self.now - 40 * m, 101, host="h1"),
                                       exit_rec(self.now - 20 * m, 102, host="h1"),
                                       exit_rec(self.now - 5 * m, 103, host="h1")])
        self.append("h2.exits.jsonl", [exit_rec(self.now - 35 * m, 201, host="h2"),
                                       exit_rec(self.now - 15 * m, 202, host="h2"),
                                       exit_rec(self.now - 2 * m, 203, host="h2")])
        agg = Aggregator(self.cfg())
        agg.backfill_now()

        live = agg.live_payload(1440)
        self.assertEqual([e["pid"] for e in live["events"]],
                         [203, 103, 202, 102, 201, 101])
        self.assertEqual([(h["host"], h["ts"], h["procs"]) for h in live["hosts"]],
                         [("h1", stamp(self.now - 10 * m), 3),
                          ("h2", stamp(self.now - 10 * m), 3)])

    def test_a_failing_node_tier_does_not_stall_the_backfill(self):
        # The node tier is read first so its panel is not blank for the length
        # of the backfill. A snapshot line that is valid JSON but not an object
        # makes that read raise -- which must not leave the eBPF history unread
        # and the page saying "reading history" forever.
        self.append("h1.exits.jsonl", self.thirty_exits())
        node_day = os.path.join(self.tmp, "node", DAY)
        os.makedirs(node_day)
        with open(os.path.join(node_day, "n1.jsonl"), "w") as fh:
            fh.write("[1, 2]\n")
        agg = Aggregator(self.cfg())
        agg.backfill_now()
        self.assertEqual((agg.backfill["state"], self.events(agg)),
                         ("done", dict(NONE, agent=20, human=10)))

    def test_backfill_hours_bounds_the_startup_read_by_time(self):
        # Exits at 1, 6, 11, ... 146 minutes ago: twelve of them inside the
        # last hour (i = 0..11), the rest older.
        self.append("h1.exits.jsonl", self.thirty_exits())
        agg = Aggregator(self.cfg(backfill_hours=1))
        agg.backfill_now()
        # i % 3 == 0 is human: i = 0, 3, 6, 9 -> 4 human, 8 agent.
        self.assertEqual(self.events(agg), dict(NONE, agent=8, human=4))

    def test_backfill_hours_zero_starts_empty_and_fills_forward(self):
        self.append("h1.exits.jsonl", self.thirty_exits())
        agg = Aggregator(self.cfg(backfill_hours=0))
        agg.backfill_now()
        self.assertEqual(self.events(agg), NONE)

        self.append("h1.exits.jsonl", [exit_rec(time.time() - 1, pid=9)])
        agg.poll_once()
        self.assertEqual(self.events(agg), dict(NONE, agent=1))


class ConcurrencyTest(FeedCase):

    def test_the_page_can_read_while_the_backfill_runs(self):
        # The point of reading history concurrently: the tail and every API read
        # get the lock between backfill batches, instead of queueing behind the
        # whole read. A lock that is released and immediately re-taken by the
        # thread still holding the GIL is never handed over.
        import threading
        t = self.now - 20000
        self.append("h1.exits.jsonl", [exit_rec(t + 0.5 * i, pid=i) for i in range(30000)])
        agg = Aggregator(self.cfg())
        fill = threading.Thread(target=agg.backfill_now)
        fill.start()
        try:
            deadline = time.time() + 20
            while agg.backfill.get("records", 0) == 0 and fill.is_alive() \
                    and time.time() < deadline:
                time.sleep(0.001)
            self.assertTrue(fill.is_alive(), "backfill ended before a read was tried")
            agg.live_payload(1440)
            self.assertTrue(fill.is_alive(),
                            "a read queued behind the whole backfill instead of a batch")
        finally:
            fill.join()


class PanelsTest(FeedCase):

    def test_the_default_window_builds_its_panels_over_the_whole_feed(self):
        self.append("h1.exits.jsonl", self.thirty_exits())
        agg = Aggregator(self.cfg())
        agg.start()
        try:
            agg.backfill_now()
            agg.panels(1440)
            deadline = time.time() + 20
            while time.time() < deadline:
                # status(), not panels(): asking again re-queues a failed build,
                # and the failure is what this has to be able to see.
                held = {w["minutes"]: w for w in agg.panels_by_window.status()["held"]}
                if held[1440]["state"] in ("ready", "error"):
                    break
                time.sleep(0.05)
            self.assertEqual((held[1440]["state"], held[1440]["error"]), ("ready", None))
            by_class = agg.panels(1440)["panels"]["tool_mix"]["by_class"]
            self.assertEqual((by_class["agent"]["calls"], by_class["human"]["calls"]),
                             (20, 10))
        finally:
            agg.stop()


ebr_twins(globals())


if __name__ == "__main__":
    unittest.main()
