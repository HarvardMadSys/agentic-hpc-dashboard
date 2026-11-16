"""The two day-file formats read as one: JSONL and EBR, apart and as twins.

A collector writing both formats (`EBPFM_FORMAT=both`) leaves `x.jsonl` and
`x.ebr` beside each other with the same records; every reader takes one of
them (`feedfile.choose`, `feeds.<tier>.prefer`) and counts each record once.
And a feed in either format alone must give the same answers: the same rows
for a query, the same records for a window, a tree, an hour. These tests write
the files the collector writes and read them through the readers the service
runs: the tail, the window scan, the hours, the event scan, the key scan, the
clock, the node tier and the feed report. The decoder's prefilters (`where`)
are checked against the predicates they stand in for, on random records.

    cd dashboard && uv run python -m unittest tests.test_formats -q
"""
import copy
import gzip
import io
import json
import os
import random
import shutil
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from rc_dashboard import config as configmod                       # noqa: E402
from rc_dashboard import (clock, ebr, export, feedfile, feeds, gz, hours,   # noqa: E402
                          iocmds, proctree, snapshots)
from rc_dashboard.aggregator import Aggregator                     # noqa: E402
from rc_dashboard.export import (make_predicate, parse_filters, scan,  # noqa: E402
                                 window_plan, window_scan)
from rc_dashboard.normalize import Normalizer                      # noqa: E402
from rc_dashboard.tail import FileTailSource, read_back            # noqa: E402
from rc_dashboard.windows import EVENTS, ViewFilter, WindowRegistry   # noqa: E402
from test_ingest import (DAY, NONE, RESET_BYTES, FeedCase, ebr_name, ebr_twins,  # noqa: E402
                         exit_rec, line, stamp)
import test_hours as TH                                            # noqa: E402
import test_merge as TM                                            # noqa: E402
import test_proctree as TP                                         # noqa: E402

T0 = 1_790_000_000.0


def plain_cfg():
    return configmod.Config(configmod.defaults(), {}, None)


def write_jsonl(path, recs):
    with open(path, "ab") as fh:
        for r in recs:
            fh.write(line(r))


def write_ebr(path, recs, frame_records=16, reset_bytes=RESET_BYTES):
    if not recs:
        return
    w = ebr.Writer.open_append(path, max_frame_records=frame_records, reset_bytes=reset_bytes)
    try:
        for r in recs:
            w.write(r)
        w.flush()
    finally:
        w.close()


def write_rounds(path, recs, fmt, per_round=3, frame_records=16):
    """`recs` appended a poll round (`per_round` records) at a time: one flush
    each, so an EBR file holds a frame per round."""
    for i in range(0, len(recs), per_round):
        if fmt == "ebr":
            write_ebr(path, recs[i:i + per_round], frame_records)
        else:
            write_jsonl(path, recs[i:i + per_round])


def gzip_in_place(path):
    with open(path, "rb") as src, gzip.open(path + ".gz", "wb") as dst:
        shutil.copyfileobj(src, dst)
    shutil.copystat(path, path + ".gz")
    os.unlink(path)
    return path + ".gz"


def varied(t0, n, seed=3, host="h1", step=20.0):
    """`n` records a few seconds out of order within their round, of every kind
    a filter can tell apart: users, agent types (a null one, an empty one, none
    at all), exit codes, signals, sessions, events, directories."""
    rng = random.Random(seed)
    out = []
    for i in range(n):
        t = t0 + step * i + rng.uniform(-6, 6)
        at = rng.choice(["claude_code", "codex", None, "", "absent"])
        r = {"schema_version": 8, "event": rng.choice(["exit"] * 5 + ["conn", "truncated",
                                                                       "residency"]),
             "ts": stamp(t), "ts_epoch": t, "host": host,
             "user": rng.choice(["alice", "bob", "carol"]),
             "actor3": "agent" if at not in (None, "", "absent") else "human",
             "agent_type": at, "comm": "git", "args": "git status %d" % i,
             "pid": 1000 + i, "ppid": 1, "cpu_s": 0.1, "duration_s": 1.0,
             "exit_code": rng.choice([0, 1, None]), "signal": rng.choice([None, None, 9]),
             "session_key": "apid:%d" % rng.choice([7, 8]), "agent_pid": rng.choice([7, 8]),
             "cwd": rng.choice(["/home/alice/proj", "/home/alice/proj/sub", "/tmp"])}
        if at == "absent":
            del r["agent_type"]
        out.append(r)
    return out


def rows(recs):
    return [(r["host"], r["pid"], r.get("_ts_epoch")) for r in recs]


class Roots(unittest.TestCase):
    """The same records in three roots: JSONL, EBR a frame per poll round, and
    EBR with one frame for every append (a frame spanning hours)."""

    FORMATS = ("jsonl", "ebr", "wide")

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rc_formats_")
        self.roots = {f: os.path.join(self.tmp, f) for f in self.FORMATS}
        for r in self.roots.values():
            os.makedirs(os.path.join(r, DAY))
        self.cfg = plain_cfg()
        self.now = time.time()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, name, recs, per_round=3):
        for fmt, root in self.roots.items():
            p = os.path.join(root, DAY, name)
            if fmt == "jsonl":
                write_rounds(p, recs, "jsonl", per_round)
            elif fmt == "ebr":
                write_rounds(ebr_name(p), recs, "ebr", per_round)
            else:
                write_ebr(ebr_name(p), recs, frame_records=1 << 20)


class ScanTest(Roots):
    """`/api/events` and `/api/export` (`export.scan`) over either format."""

    def setUp(self):
        super().setUp()
        self.t0 = self.now - 3 * 3600
        self.write("h1.exits.jsonl", varied(self.t0, 400, seed=1, host="h1"))
        self.write("h2.exits.jsonl", varied(self.t0 + 7, 300, seed=2, host="h2", step=27.0))

    def run_scan(self, fmt, params, newest_first, limit=None, offset=0):
        f = parse_filters({k: [v] if isinstance(v, str) else v for k, v in params.items()})
        return rows(rec for rec, _s, _m in scan([self.roots[fmt]], Normalizer(self.cfg),
                                                make_predicate(f), limit=limit, offset=offset,
                                                newest_first=newest_first,
                                                prefer="ebr" if fmt != "jsonl" else "jsonl"))

    def test_every_filter_both_orders_and_their_bounds_read_alike(self):
        t = self.t0
        cases = [{}, {"event": "exit"}, {"event": "conn,residency"}, {"user": "alice"},
                 {"user": "alice,bob", "event": "exit"}, {"agent_type": "none"},
                 {"agent_type": "codex,none"}, {"exit_code": "1"}, {"signal": "9"},
                 {"host": "h2"}, {"session_key": "apid:7"}, {"class": "human"},
                 {"from": str(t + 3600), "to": str(t + 5400)},
                 {"from": str(t + 100), "user": "carol", "event": "exit,truncated"},
                 {"to": str(t + 2000)}, {"from": str(t + 99999)}]
        for params in cases:
            for newest in (True, False):
                with self.subTest(params=params, newest_first=newest):
                    want = self.run_scan("jsonl", params, newest)
                    self.assertTrue(want or params.get("from") == str(t + 99999))
                    self.assertEqual(self.run_scan("ebr", params, newest), want)
                    self.assertEqual(self.run_scan("wide", params, newest), want)

    def test_pages_read_alike(self):
        for newest in (True, False):
            want = self.run_scan("jsonl", {"event": "exit"}, newest, limit=7, offset=11)
            self.assertEqual(len(want), 7)
            for fmt in ("ebr", "wide"):
                self.assertEqual(self.run_scan(fmt, {"event": "exit"}, newest, limit=7,
                                               offset=11), want)


class WindowTest(Roots):
    """A window build's read (`export.window_scan`), filtered or not."""

    def setUp(self):
        super().setUp()
        self.t0 = self.now - 3 * 3600
        self.write("h1.exits.jsonl", varied(self.t0, 400, seed=4, host="h1"))
        self.write("h2.snapshot.jsonl", varied(self.t0 + 3, 200, seed=5, host="h2",
                                               step=50.0))

    def read(self, fmt, since, until=None, flt=None, events=EVENTS):
        plan, total = window_plan([self.roots[fmt]], since,
                                  prefer="ebr" if fmt != "jsonl" else "jsonl")
        flt = flt or ViewFilter()
        got = [rec for rec, _st in window_scan(plan, Normalizer(self.cfg), since, until,
                                               events=events, match=flt.line_match,
                                               find=flt.line_find, where=flt.where)
               if not flt or flt.match(rec)]
        return rows(got), plan

    def test_a_window_reads_the_same_records(self):
        for since, until in ((self.t0 - 10, None), (self.t0 + 4000, None),
                             (self.t0 + 1000, self.t0 + 5000), (self.t0 + 9000, None)):
            want, _ = self.read("jsonl", since, until)
            for fmt in ("ebr", "wide"):
                with self.subTest(since=since - self.t0, until=until, fmt=fmt):
                    self.assertEqual(self.read(fmt, since, until)[0], want)

    def test_a_filtered_view_reads_the_same_records(self):
        since = self.t0 + 600
        for users, types in ((["alice"], []), ([], ["none"]), (["bob", "carol"], ["codex"]),
                             ([], ["claude_code", "none"]), (["alice"], ["none"])):
            flt = ViewFilter(users, types)
            want, _ = self.read("jsonl", since, flt=flt)
            self.assertTrue(want)
            for fmt in ("ebr", "wide"):
                with self.subTest(users=users, types=types, fmt=fmt):
                    self.assertEqual(self.read(fmt, since, flt=flt)[0], want)

    def test_a_saved_window_resumes_at_a_frame_boundary(self):
        since = self.t0 + 600
        _, plan = self.read("ebr", since)
        for e in plan:
            with gz.open_feed(e["path"]) as fh:
                self.assertTrue(feedfile.is_boundary(fh, e["end"], "ebr"))
            self.assertEqual(e["end"], os.path.getsize(e["path"]))
        saved = [{k: e[k] for k in ("path", "dev", "ino", "head_n", "head", "end")}
                 for e in plan]
        got = export.resume_plan([self.roots["ebr"]], since, saved, prefer="ebr")
        self.assertIsNotNone(got)
        self.assertEqual(got[1], 0)
        # an offset that is no frame's end is not resumed
        bad = [dict(saved[0], end=saved[0]["end"] - 1)] + saved[1:]
        self.assertIsNone(export.resume_plan([self.roots["ebr"]], since, bad, prefer="ebr"))


class TwinCase(FeedCase):
    """A collector writing both formats into one directory: each append goes to
    the JSONL twin and then, a frame per round, to the EBR twin."""

    TWIN = False

    def append(self, name, recs, raw=b"", day=DAY):
        assert not raw, "a twin round is written whole"
        os.makedirs(os.path.join(self.root, day), exist_ok=True)
        p = os.path.join(self.root, day, name)
        write_jsonl(p, recs)
        write_ebr(ebr_name(p), recs, self.FRAME_RECORDS)

    def cfg_prefer(self, prefer, **live):
        cfg = self.cfg(**live)
        cfg.tree["feeds"]["ebpf"]["prefer"] = prefer
        return cfg


class TwinsCountOnceTest(TwinCase):

    def test_the_tail_and_its_read_back_read_one_twin(self):
        recs = self.thirty_exits()
        for i in range(0, 30, 5):
            self.append("h1.exits.jsonl", recs[i:i + 5])
        agents = 20
        for prefer in ("jsonl", "ebr"):
            with self.subTest(prefer=prefer):
                agg = Aggregator(self.cfg_prefer(prefer))
                self.assertEqual([feedfile.format_of(p) for p in agg.source.files()], [prefer])
                agg.backfill_now()
                agg.poll_once()
                self.assertEqual(self.events(agg), dict(NONE, agent=agents, human=10))
                self.append("h1.exits.jsonl", [exit_rec(time.time() - 1, pid=7777 + agents)])
                agg.poll_once()
                agents += 1
                self.assertEqual(self.events(agg), dict(NONE, agent=agents, human=10))

    def test_a_window_reads_one_twin(self):
        self.append("h1.exits.jsonl", self.thirty_exits())
        self.append("h2.exits.jsonl", [exit_rec(self.now - 100, 9, host="h2")])
        since = self.now - 7200
        want = None
        for prefer in ("jsonl", "ebr"):
            plan, _ = window_plan([self.root], since, prefer=prefer)
            self.assertEqual({feedfile.format_of(e["path"]) for e in plan}, {prefer})
            got = rows(r for r, _ in window_scan(plan, Normalizer(self.cfg()), since,
                                                 events=EVENTS))
            self.assertEqual(len(got), 25)          # 24 of the thirty in 2 h, and h2's
            want = want or got
            self.assertEqual(got, want)


class TwinHoursTest(TH.HourCase):
    """Hours reduced from twins: once, from the twin each preference names."""

    TWIN = False

    def append(self, name, recs, raw=b"", day=DAY):
        TwinCase.append(self, name, recs, raw, day)

    def cfg_prefer(self, prefer, merge=True):
        cfg = self.cfg(merge=merge)
        cfg.tree["feeds"]["ebpf"]["prefer"] = prefer
        return cfg

    def same(self, want, view):
        self.assertEqual(view.cache["source"], "hours")
        got = self.payload(view)
        for key in sorted(want):
            with self.subTest(reducer=key):
                self.assertEqual(TM.diff(want[key], got[key]), [])

    def test_twins_reduce_into_hours_once_under_either_preference(self):
        self.write(TH.gen(TH.SINCE + 1, TH.NOW - 30))
        want = self.payload(self.build(self.registry(self.cfg_prefer("jsonl", merge=False))))
        self.assertEqual(TM.diff(want, self.payload(self.build(self.registry(
            self.cfg_prefer("ebr", merge=False))))), [])
        for prefer in ("jsonl", "ebr"):
            with self.subTest(prefer=prefer):
                self.same(want, self.build(self.registry(self.cfg_prefer(prefer))))

    def test_hours_go_on_from_the_other_twin_when_one_is_deleted(self):
        # Hours reduced from the JSONL twins; then the JSONL is deleted and the
        # day goes on in EBR alone. The hour after reads the EBR twin from a
        # seek: what the hour before took from the JSONL is not a straggler.
        self.write(TH.gen(TH.NOW - 6 * TH.HOUR + 777, TH.NOW - 2 * TH.HOUR))
        cfg = self.cfg_prefer("jsonl")
        reg = self.registry(cfg)
        self.assertEqual(self.build(reg, 300).cache["source"], "hours")
        last = reg.hours.chain[0]
        d = os.path.join(self.root, DAY)
        for n in os.listdir(d):
            if n.endswith(".jsonl"):
                os.unlink(os.path.join(d, n))
        later = TH.gen(TH.NOW - 2 * TH.HOUR + 1, TH.NOW - 30, seed=9)
        by_host = {}
        for r in later:
            by_host.setdefault(r["host"], []).append(r)
        for host, rs in by_host.items():
            write_ebr(os.path.join(d, "%s.ebr" % host), rs, self.FRAME_RECORDS)
        view = self.build(reg, 300)
        self.assertGreater(reg.hours.chain[0], last)
        self.assertEqual({reg.hours.meta(h)["stragglers"] for h in reg.hours.have}, {0})
        want = self.payload(self.build(self.registry(self.cfg_prefer("jsonl", merge=False)), 300))
        for key in sorted(set(want) - {"_procs", "trajectories"}):
            with self.subTest(reducer=key):
                self.assertEqual(TM.diff(want[key], self.payload(view)[key]), [])


class WideTwinHoursTest(TwinHoursTest):
    """The same with every append one EBR frame, hours wide: a frame is read
    whole by each hour it spans, and each takes its own records."""

    FRAME_RECORDS = 1 << 20


class MidDayTest(TwinCase):

    def setUp(self):
        super().setUp()
        recs = self.thirty_exits()
        p = os.path.join(self.root, DAY, "h1.exits.jsonl")
        write_jsonl(p, recs)
        # the collector went to both formats with exit 18 of 30: 90 min in
        write_ebr(ebr_name(p), recs[18:], self.FRAME_RECORDS)

    def test_a_twin_started_mid_day_is_read_from_the_other_and_said_so(self):
        cfg = self.cfg_prefer("ebr")
        agg = Aggregator(cfg)
        self.assertEqual([os.path.basename(p) for p in agg.source.files()], ["h1.exits.jsonl"])
        agg.backfill_now()
        agg.poll_once()
        self.assertEqual(self.events(agg), dict(NONE, agent=20, human=10))
        rep = feeds.resolve_dated("ebpf", cfg)
        self.assertEqual([(t["stream"], t["read"], t["unread"]) for t in rep["twins"]],
                         [("h1.exits", "jsonl", "ebr")])
        self.assertIn("mid-day", rep["twins"][0]["reason"])
        self.assertIn("not the preferred ebr", rep["format_notice"])


class PendingFrameTest(FeedCase):
    FORMAT = "ebr"

    def test_a_frame_still_being_written_is_held_then_read_once(self):
        recs = [exit_rec(self.now - 600 + 10 * i, pid=i) for i in range(9)]
        a, b, c = self.rounds([recs[:3], recs[3:6], recs[6:]])
        self.write_raw("h1.exits.jsonl", a)
        src = FileTailSource([self.root], prefer="ebr")
        self.assertEqual([r["pid"] for r in src.poll()], [0, 1, 2])
        for cut in (5, 44, len(b) - 1):
            with open(self.path("h1.exits.jsonl"), "rb+") as fh:
                fh.truncate(len(a))
            self.write_raw("h1.exits.jsonl", b[:cut])
            held = src.stats["partial_holds"]
            self.assertEqual(list(src.poll()), [], cut)
            self.assertEqual(src.stats["partial_holds"], held + 1)
        self.write_raw("h1.exits.jsonl", b[len(b) - 1:] + c)
        self.assertEqual([r["pid"] for r in src.poll()], [3, 4, 5, 6, 7, 8])
        self.assertEqual(list(src.poll()), [])
        self.assertEqual(src.stats["corrupt_frames"], 0)

    def test_the_decoder_is_kept_with_the_offset_and_moves_to_the_gz(self):
        recs = [exit_rec(self.now - 600 + 10 * i, pid=i) for i in range(6)]
        self.append("h1.exits.jsonl", recs[:3])
        src = FileTailSource([self.root], prefer="ebr")
        list(src.poll())
        st = src.state[self.path("h1.exits.jsonl")]
        self.assertEqual(st["dec_at"], st["offset"])
        with mock.patch.object(ebr.Decoder, "at", side_effect=AssertionError("replayed")):
            self.append("h1.exits.jsonl", recs[3:5])
            self.assertEqual([r["pid"] for r in src.poll()], [3, 4])
            self.append("h1.exits.jsonl", recs[5:])
            self.compress("h1.exits.jsonl")
            self.assertEqual([r["pid"] for r in src.poll()], [5])


class DeletedTwinTest(TwinCase):

    def test_a_deleted_twin_hands_its_stream_to_the_other_at_its_end(self):
        for prefer, other in (("jsonl", "ebr"), ("ebr", "jsonl")):
            with self.subTest(prefer=prefer):
                feedfile._choices.clear()
                shutil.rmtree(os.path.join(self.root, DAY))
                recs = [exit_rec(self.now - 600 + 10 * i, pid=i) for i in range(9)]
                self.append("h1.exits.jsonl", recs[:3])
                src = FileTailSource([self.root], prefer=prefer)
                self.assertEqual([r["pid"] for r in src.poll()], [0, 1, 2])
                self.append("h1.exits.jsonl", recs[3:6])
                mine = os.path.join(self.root, DAY, "h1.exits." + prefer)
                theirs = os.path.join(self.root, DAY, "h1.exits." + other)
                os.unlink(mine)
                src.prune_state()               # the twin's offset is kept for the switch
                # Taken up at its end: what it got since the last poll is the
                # most missed, and nothing it holds is read twice.
                self.assertEqual(list(src.poll()), [])
                self.assertEqual(src.stats["twin_switches"], 1)
                write = write_jsonl if other == "jsonl" else write_ebr
                write(theirs, recs[6:])
                self.assertEqual([r["pid"] for r in src.poll()], [6, 7, 8])
                src.prune_state()
                self.assertEqual(list(src.state), [theirs])


class ResumeAfterGzipTest(FeedCase):
    """A saved window over a plain day-file the collector has compressed since
    resumes from its `.gz`, in either format: the same bytes at the same offsets."""

    def registry(self, cfg=None):
        cfg = cfg or self.cfg()
        classes = list(cfg.get("filters.classes")) + ["unlabeled"]
        return WindowRegistry(cfg, classes, lambda: [self.root], threading.RLock())

    def build(self, reg, minutes=1440):
        view = reg.request(minutes)
        reg._build(view)
        self.assertEqual((view.state, view.error), ("ready", None))
        return view

    def test_a_compressed_day_resumes_and_is_not_read_again(self):
        self.append("h1.exits.jsonl", self.thirty_exits())
        self.append("h2.exits.jsonl", [exit_rec(self.now - 300, 5, host="h2")])
        with mock.patch("time.time", return_value=self.now):
            first = self.build(self.registry())
            self.compress("h1.exits.jsonl")
            again = self.build(self.registry())
            self.assertEqual(again.cache["source"], "snapshot", again.cache["note"])
            self.assertEqual(again.progress["records"], 0)
            self.assertEqual(again.payload, first.payload)
            # and it goes on from there: a record written to the other file since
            self.append("h2.exits.jsonl", [exit_rec(self.now - 200, 6, host="h2")])
            third = self.build(self.registry())
            self.assertEqual((third.cache["source"], third.progress["records"]),
                             ("snapshot", 1))


class TwinSwitchResumeTest(TwinCase):

    def test_a_saved_window_over_a_twin_no_longer_read_is_rescanned(self):
        self.append("h1.exits.jsonl", self.thirty_exits())
        cfg = self.cfg_prefer("jsonl")
        reg = ResumeAfterGzipTest.registry(self, cfg)
        ResumeAfterGzipTest.build(self, reg)
        os.unlink(os.path.join(self.root, DAY, "h1.exits.jsonl"))
        view = ResumeAfterGzipTest.build(self, ResumeAfterGzipTest.registry(self, cfg))
        self.assertEqual(view.cache["source"], "scan")
        self.assertIn("changed since", view.cache["note"])


class KeyScanTest(unittest.TestCase):
    """An EBR day-file's key scan (`proctree.key_scan`, its decoder's `where`)
    reads what a full scan of it reads, and what the JSONL key scan reads."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="rc_formats_key_")
        self.cfg = plain_cfg()
        t0 = time.time() - 7200
        if time.localtime(t0).tm_mday != time.localtime(t0 + 400).tm_mday:
            t0 -= 1200
        self.t0 = t0
        self.day = time.strftime("%Y-%m-%d", time.localtime(t0))
        recs = TP.KeyScanTest.recs(self)
        recs += [TP.raw("exit", t0 + 60 + i, 7100 + i, 1, "ls", "ls", agent_pid=None,
                        tree_root=None, actor="human", actor3="human", agent_type=None,
                        session_key=None, cgroup="/user.slice/user-1000.slice/session-7.scope")
                 for i in range(5)]
        recs.sort(key=lambda r: r["ts_epoch"])
        self.roots = {}
        for fmt in ("jsonl", "ebr"):
            root = os.path.join(self.dir, fmt)
            os.makedirs(os.path.join(root, self.day))
            write_rounds(os.path.join(root, self.day, "h1." + fmt), recs, fmt, per_round=4)
            self.roots[fmt] = root

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def sid(self, kv, kt="apid", a3="agent"):
        return "h1|%s|%s|%s:%s" % (self.day, a3, kt, kv)

    def read(self, fmt, members, how):
        plan, _ = window_plan([self.roots[fmt]], self.t0 - 10, 3,
                              prefer="ebr" if fmt == "ebr" else "jsonl")
        norm = Normalizer(self.cfg)
        if how == "keys":
            it = proctree.key_scan([dict(e) for e in plan], norm, self.t0 - 10, None,
                                   proctree.key_patterns(members),
                                   where=proctree.key_where(members))
        else:
            it = window_scan([dict(e) for e in plan], norm, self.t0 - 10, None,
                             events=proctree.EVENTS)
        return [(r["_ts_epoch"], r["pid"]) for r, _ in it if r.get("_sid") in members]

    def test_the_key_scans_read_what_a_full_scan_reads(self):
        for members in ({self.sid(12)}, {self.sid(12), self.sid(123)},
                        {self.sid(k) for k in range(130, 140)},
                        {self.sid(7, "sess", "human")}):
            with self.subTest(members=sorted(members)):
                want = self.read("jsonl", members, "keys")
                self.assertEqual(self.read("jsonl", members, "full"), want)
                self.assertEqual(self.read("ebr", members, "keys"), want)
                self.assertEqual(self.read("ebr", members, "full"), want)

    def test_a_key_scan_decodes_only_the_sessions_records(self):
        members = {self.sid(12)}
        plan, _ = window_plan([self.roots["ebr"]], self.t0 - 10, 3, prefer="ebr")
        it = proctree.key_scan(plan, Normalizer(self.cfg), self.t0 - 10, None,
                               proctree.key_patterns(members), where=proctree.key_where(members))
        got, st = [], None
        for r, st in it:
            got.append(r)
        self.assertEqual(len(got), 42)
        self.assertEqual(st["scanned"], 42, "the other agent's records are never built")
        self.assertIsNone(proctree.key_where({self.sid(7, "sess", "human")}))


class EbrInspectTest(TP.EndToEndTest):
    """The process tree of a session (proctree.inspect) read from EBR day-files:
    every end-to-end test of test_proctree, on the same records in frames."""

    def setUp(self):
        super().setUp()
        for d, _dirs, names in os.walk(self.dir):
            for n in names:
                if n.endswith(".jsonl"):
                    p = os.path.join(d, n)
                    with open(p, "rb") as fh:
                        recs = [json.loads(x) for x in fh if x.strip()]
                    write_rounds(ebr_name(p), recs, "ebr", per_round=2)
                    os.unlink(p)


class NewestTest(FeedCase):
    FORMAT = "ebr"

    def setUp(self):
        super().setUp()
        self.time, self.time_ns = time.time, time.time_ns
        self.state = dict(clock._state)
        self.recs = [exit_rec(self.now - 600 + 10 * i, pid=i) for i in range(20)]
        self.append("h1.exits.jsonl", self.recs[:10])
        self.append("h1.exits.jsonl", self.recs[10:])
        self.newest = self.recs[-1]["ts_epoch"]

    def tearDown(self):
        time.time, time.time_ns = self.time, self.time_ns
        clock._state.clear()
        clock._state.update(self.state)
        super().tearDown()

    def test_the_newest_record_of_an_ebr_file_and_of_its_gz(self):
        for compressed in (False, True):
            with self.subTest(compressed=compressed):
                if compressed:
                    self.compress("h1.exits.jsonl")
                ts, path = clock.newest_record([self.root], "ebr")
                self.assertEqual((ts, os.path.basename(path)),
                                 (self.newest, os.path.basename(self.path(
                                     "h1.exits.jsonl" + (".gz" if compressed else "")))))
                got = feeds.newest_record_ts(path)
                self.assertEqual(got.strftime(feeds.TS_FORMAT), stamp(self.newest))

    def test_auto_pins_the_clock_to_an_ebr_feed(self):
        cfg = self.cfg()
        cfg.tree["clock"] = {"as_of": "auto"}
        st = clock.install(cfg, [self.root])
        self.assertTrue(st["pinned"])
        self.assertAlmostEqual(time.time(), self.newest + 1, places=3)

    def test_an_hour_store_asks_the_ebr_files_how_far_the_feed_is(self):
        store = hours.HourStore(self.cfg(), ["agent"], dict, frozenset({"exit"}),
                                os.path.join(self.tmp, "h"))
        store.closed_end(self.newest + 3600, [self.root])
        self.assertEqual(store.newest_seen, self.newest)


class NodeTierTest(FeedCase):
    TWIN = False                # it writes the node tier's files in both formats itself

    def snapshots(self, host, n):
        return [{"ts": stamp(self.now - 600 + 60 * k), "ts_epoch": self.now - 600 + 60 * k,
                 "host": host, "seq": k, "per_user_cgroup": {"alice": {"cpu_usage_usec": k}}}
                for k in range(n)]

    def node_cfg(self, prefer):
        cfg = self.cfg()
        cfg.tree["feeds"]["ebpf_node"]["roots"] = [os.path.join(self.tmp, "node")]
        cfg.tree["feeds"]["ebpf_node"]["prefer"] = prefer
        return cfg

    def test_the_node_tier_reads_its_newest_snapshots_from_either_format(self):
        d = os.path.join(self.tmp, "node", DAY)
        os.makedirs(d)
        write_jsonl(os.path.join(d, "n1.jsonl"), self.snapshots("n1", 5))
        write_rounds(os.path.join(d, "n2.ebr"), self.snapshots("n2", 5), "ebr", per_round=1)
        # twins with no record yet: held, and nothing read
        open(os.path.join(d, "n3.jsonl"), "wb").close()
        open(os.path.join(d, "n3.ebr"), "wb").close()
        for prefer in ("jsonl", "ebr"):
            with self.subTest(prefer=prefer):
                agg = Aggregator(self.node_cfg(prefer))
                fed = []
                with mock.patch.object(agg.node, "feed_snapshot", fed.append):
                    self.assertEqual(agg.read_node_tier(), 4)
                self.assertEqual([(r["host"], r["seq"]) for r in fed],
                                 [("n1", 3), ("n1", 4), ("n2", 3), ("n2", 4)])
                rep = agg.reports["ebpf_node"]
                self.assertEqual(rep["hosts"], ["n1", "n2", "n3"])
                self.assertEqual(sorted((os.path.basename(f["path"]), f["held"])
                                        for f in rep["files"]),
                                 [("n1.jsonl", False), ("n2.ebr", False), ("n3.ebr", True),
                                  ("n3.jsonl", True)])


class ReportTest(TwinCase):
    """What /api/feeds and --check-feeds say about the formats."""

    def setUp(self):
        super().setUp()
        recs = self.thirty_exits()
        self.append("h1.exits.jsonl", recs)
        self.append("login.rc.fas.snapshot.jsonl", [totals_like(self.now - 60)])
        p = os.path.join(self.root, DAY, "h2.exits.jsonl")
        write_jsonl(p, recs)
        write_ebr(ebr_name(p), recs[20:])                 # started mid-day
        for n in ("h3.exits.jsonl", "h3.exits.ebr"):       # held
            open(os.path.join(self.root, DAY, n), "wb").close()

    def test_the_feed_report_names_the_formats_read_and_why(self):
        rep = feeds.resolve_dated("ebpf", self.cfg_prefer("ebr"))
        self.assertEqual(rep["prefer"], "ebr")
        self.assertEqual(rep["formats"], {"read": {"ebr": 2, "jsonl": 1},
                                          "unread": {"ebr": 1, "jsonl": 2}})
        self.assertEqual(rep["hosts"], ["h1", "h2", "h3", "login.rc.fas"])
        read = {os.path.basename(f["path"]): (f["format"], f["held"]) for f in rep["files"]}
        self.assertEqual(read["h1.exits.ebr"], ("ebr", False))
        self.assertEqual(read["h2.exits.jsonl"], ("jsonl", False))
        self.assertEqual(read["h3.exits.ebr"], ("ebr", True))
        self.assertNotIn("h1.exits.jsonl", read)
        self.assertEqual([(t["stream"], t["read"]) for t in rep["twins"]],
                         [("h1.exits", "ebr"), ("h2.exits", "jsonl"),
                          ("login.rc.fas.snapshot", "ebr")])
        self.assertEqual([(h["day"], h["stream"]) for h in rep["held"]], [(DAY, "h3.exits")])
        self.assertIn("h2.exits", rep["format_notice"])
        self.assertIsNone(feeds.resolve_dated("ebpf", self.cfg_prefer("jsonl"))["format_notice"])
        # the export header carries it, and --check-feeds prints it
        meta = export.meta_header({}, {"ebpf": rep}, self.cfg(), 0, False, 0.0)["_meta"]
        self.assertEqual(meta["feed_formats"]["prefer"], "ebr")
        self.assertEqual(meta["feed_formats"]["held"], 1)
        out = io.StringIO()
        with redirect_stdout(out):
            feeds.print_table({"ebpf": dict(rep, notice=None)})
        self.assertIn("formats ebpf      prefer ebr; read ebr 2, jsonl 1", out.getvalue())

    def test_a_twin_that_lags_the_other_is_reported(self):
        jp = os.path.join(self.root, DAY, "h1.exits.jsonl")
        os.utime(jp, (time.time() + 3600, time.time() + 3600))
        rep = feeds.resolve_dated("ebpf", self.cfg_prefer("ebr"))
        self.assertIn("lags", rep["format_notice"])
        self.assertIn("h1.exits", rep["format_notice"])

    def test_no_phantom_host_from_either_format(self):
        agg = Aggregator(self.cfg_prefer("ebr"))
        hosts = agg.collectors()["hosts"]
        names = hosts if isinstance(hosts, list) else list(hosts)
        flat = json.dumps(names)
        self.assertNotIn("h1.exits", flat)
        self.assertNotIn("login.rc\"", flat)
        self.assertEqual(sorted(feedfile.host_of(p) for p in agg.source.files()),
                         ["h1", "h2", "login.rc.fas"])


def totals_like(t):
    return {"schema_version": 8, "event": "residency_totals", "ts": stamp(t), "ts_epoch": t,
            "host": "login.rc.fas", "by_actor3": {}, "by_agent_type": {}}


class CorruptFrameTest(FeedCase):
    """A frame damaged in the middle of a day-file: the frames around it are
    read, its region and the rest of its segment are counted, nothing raises."""
    FORMAT = "ebr"

    def setUp(self):
        super().setUp()
        self.recs = [exit_rec(self.now - 3000 + 10 * i, pid=i) for i in range(120)]
        p = self.path("h1.exits.jsonl")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        rounds = chunks(self.recs, 4)
        for rr in rounds:                                   # a frame a round, small segments
            write_ebr(p, rr, frame_records=16, reset_bytes=4096)
        with open(p, "rb") as fh:
            data = bytearray(fh.read())
        frames = [f for f in ebr.frames(bytes(data)) if f.kind == ebr.DATA]
        self.assertEqual(len(frames), len(rounds))
        bad = frames[10]
        data[bad.off + 60] ^= 0xFF                          # a payload byte
        with open(p, "wb") as fh:
            fh.write(bytes(data))
        # what survives: every frame but the damaged one and the rest of its segment
        nxt = next(f for f in ebr.frames(bytes(data), bad.end) if f.kind == ebr.SEGMENT)
        self.assertLess(nxt.off, frames[-1].off)
        self.kept = [r["pid"] for f, rr in zip(frames, rounds)
                     if not (bad.off <= f.off < nxt.off) for r in rr]
        self.before = [r["pid"] for f, rr in zip(frames, rounds) if f.off < bad.off
                       for r in rr]

    def test_a_window_reads_around_it(self):
        with mock.patch.object(feedfile, "SETTLE", 256):
            plan, _ = window_plan([self.root], self.now - 4000, prefer="ebr")
            stats = None
            got = []
            for rec, stats in window_scan(plan, Normalizer(self.cfg()), self.now - 4000):
                got.append(rec["pid"])
        self.assertEqual(got, self.kept)
        self.assertGreaterEqual(stats["parse_errors"], 1)

    def test_the_tail_reads_around_it_and_counts_it(self):
        with mock.patch.object(feedfile, "SETTLE", 256):
            src = FileTailSource([self.root], prefer="ebr")
            got = [r["pid"] for r in src.poll()]
        self.assertEqual(got, self.kept)
        self.assertEqual(src.stats["corrupt_frames"], 1)

    def test_near_a_live_files_end_it_is_waited_for(self):
        # within SETTLE of the end it may be bytes not yet visible: read up to it
        src = FileTailSource([self.root], prefer="ebr")
        self.assertEqual([r["pid"] for r in src.poll()], self.before)
        self.assertEqual(src.stats["corrupt_frames"], 0)
        # sealed (.gz), it is read past
        self.compress("h1.exits.jsonl")
        plan, _ = window_plan([self.root], self.now - 4000, prefer="ebr")
        got = [r["pid"] for r, _ in window_scan(plan, Normalizer(self.cfg()), self.now - 4000)]
        self.assertEqual(got, self.kept)


def chunks(seq, n):
    return [seq[i:i + n] for i in range(0, len(seq), n)]


class WherePropertyTest(unittest.TestCase):
    """Every decoder prefilter passes every record the predicate it stands in
    for keeps (`ebr.match_where` is its semantics), on random records; and the
    decoder filters exactly as `match_where` does."""

    N = 2500

    def setUp(self):
        self.rng = random.Random(20261116)
        self.norm = Normalizer(plain_cfg())
        self.recs = [self.record() for _ in range(self.N)]

    MISSING = object()

    def pick(self, *vals):
        return self.rng.choice(vals)

    def record(self):
        t = T0 + self.rng.uniform(0, 7200)
        r = {"schema_version": 8, "ts": stamp(t), "ts_epoch": t, "pid": self.rng.randrange(9999),
             "ppid": 1, "args": "git status", "comm": "git"}
        for k, vals in (
                ("event", ("exit", "exit", "truncated", "conn", "residency", "submit",
                           self.MISSING)),
                ("user", ("alice", "bob", "", None, self.MISSING, "carol")),
                ("agent_type", ("claude_code", "codex", "none", "None", "", None, self.MISSING)),
                ("actor3", ("agent", "human", None, self.MISSING)),
                ("host", ("h1", "h2", "", None, self.MISSING)),
                ("session_key", ("apid:7", "apid:8", "claude:7", "7", "", None, self.MISSING,
                                 "x:y:7", "apid:70")),
                ("agent_pid", (7, 8, "7", 70, None, self.MISSING)),
                ("cwd", ("/home/a/proj", "/home/a/proj/sub", "/home/a/project2", "/", "",
                         None, self.MISSING)),
                ("exit_code", (0, 1, "1", None, self.MISSING)),
                ("signal", (None, 9, "9", self.MISSING)),
                ("cgroup", ("/user.slice/session-7.scope", None, self.MISSING)),
                ("tty", ("pts/5", None, self.MISSING))):
            v = self.pick(*vals)
            if v is not self.MISSING:
                r[k] = v
        return r

    def normalized(self, raw):
        return self.norm.normalize(copy.deepcopy(raw))

    def check(self, keep, where, kinds=None, what=""):
        hits = 0
        for raw in self.recs:
            rec = self.normalized(raw)
            if rec is None or not keep(rec):
                continue
            hits += 1
            self.assertTrue(ebr.match_where(where, raw), (what, raw))
            if kinds is not None:
                self.assertIn(raw.get("event"), kinds, (what, raw))
        return hits

    def test_a_view_filter(self):
        hits = 0
        for users, types in ((["alice"], []), ([], ["none"]), (["bob", ""], ["codex"]),
                             ([], ["claude_code", "none", "None"]), (["carol"], ["none"])):
            flt = ViewFilter(users, types)
            hits += self.check(flt.match, flt.where, what=(users, types))
        self.assertGreater(hits, 100)

    def test_the_event_filters(self):
        hits = 0
        for params in ({"user": ["alice"]}, {"host": ["h1"], "event": ["exit"]},
                       {"exit_code": ["1"], "signal": ["9"]}, {"session_key": ["apid:7"]},
                       {"agent_type": ["none"]}, {"agent_type": ["codex", "untagged"]},
                       {"user": ["bob"], "host": ["h2"], "event": ["exit", "truncated"],
                        "agent_type": ["claude_code"]}, {"class": ["human"]}):
            pred = make_predicate(parse_filters(params))
            self.assertIsNotNone(pred.where if set(params) - {"event", "class"} else True)
            hits += self.check(pred, pred.where, pred.events, what=params)
        self.assertGreater(hits, 100)

    def test_a_blast_radius_lookup(self):
        hits = 0
        for f, apid, host, cwd in (({}, ["7"], "h1", "/home/a/proj"),
                                   ({"user": {"alice"}}, ["7", "70"], None, None),
                                   ({}, None, "h2", "/home/a/proj/"), ({}, ["8"], None, "/"),
                                   ({"user": {"bob", ""}}, None, None, "/home/a")):
            ap = frozenset(apid) if apid else None
            ok = iocmds.predicate(dict(f), ap, host, cwd)
            hits += self.check(ok, iocmds.prefilter_where(f, ap, host, cwd),
                               what=(f, apid, host, cwd))
        self.assertGreater(hits, 50)

    def test_a_sessions_keys(self):
        # keys on a pid: a key the patterns cannot spell has no prefilter at all
        sids = sorted({r["_sid"] for r in (self.normalized(x) for x in self.recs)
                       if r is not None and r.get("_kt") == "apid"
                       and str(r.get("_kv")).isdigit()})
        self.assertTrue(sids)
        hits = 0
        for k in range(1, len(sids) + 1, max(1, len(sids) // 6)):
            members = set(sids[:k])
            where = proctree.key_where(members)
            self.assertIsNotNone(where)
            hits += self.check(lambda rec: rec.get("_sid") in members, where, what=members)
        self.assertGreater(hits, 100)

    def test_the_decoder_filters_as_match_where(self):
        bio = io.BytesIO()
        w = ebr.Writer(bio, max_frame_records=50)
        for r in self.recs:
            w.write(r)
        w.close()
        data = bio.getvalue()
        dec = ebr.Decoder()
        every = [(p, r) for f in ebr.frames(data) for p, r in dec.records(data, f)]
        self.assertEqual([r for _p, r in every], json.loads(json.dumps(self.recs)))
        wheres = [ViewFilter(["alice"], ["none"]).where,
                  make_predicate(parse_filters({"user": ["bob"], "agent_type": ["none"]})).where,
                  iocmds.prefilter_where({}, frozenset({"7"}), "h1", "/home/a/proj"),
                  proctree.key_where({"h1|x|agent|apid:7"})]
        for where in wheres:
            for kinds in (None, frozenset({"exit", "truncated"})):
                dec = ebr.Decoder()
                got = [(p, r) for f in ebr.frames(data) for p, r in
                       dec.records(data, f, kinds, where)]
                want = [(p, r) for p, r in every if ebr.match_where(where, r)
                        and (kinds is None or r.get("event") in kinds)]
                self.assertEqual(got, want)


class PreferConfigTest(unittest.TestCase):

    def load(self, **env):
        with mock.patch.dict(os.environ, env, clear=False):
            return configmod.load(None, config_path="/nonexistent/config.json")

    def test_prefer_is_validated_and_set_by_env(self):
        self.assertEqual(self.load().get("feeds.ebpf.prefer"), "jsonl")
        cfg = self.load(RC_DASH_EBPF_PREFER="EBR", RC_DASH_NODE_PREFER="ebr")
        self.assertEqual((cfg.get("feeds.ebpf.prefer"), cfg.get("feeds.ebpf_node.prefer")),
                         ("ebr", "ebr"))
        self.assertEqual(cfg.origin("feeds.ebpf.prefer"), "env:RC_DASH_EBPF_PREFER")
        with self.assertRaises(SystemExit) as cm:
            self.load(RC_DASH_EBPF_PREFER="xml")
        self.assertIn("feeds.ebpf.prefer", str(cm.exception))

    def test_the_example_config_names_both(self):
        with open(Path(__file__).resolve().parents[1] / "dashboard.config.example.json") as fh:
            tree = configmod.strip_comments(json.load(fh))
        self.assertEqual((tree["feeds"]["ebpf"]["prefer"], tree["feeds"]["ebpf_node"]["prefer"]),
                         ("jsonl", "jsonl"))

    def test_changing_it_is_a_new_fingerprint_for_saved_builds_and_hours(self):
        a, b, c = plain_cfg(), plain_cfg(), plain_cfg()
        b.tree["feeds"]["ebpf"]["prefer"] = "ebr"
        c.tree["feeds"]["ebpf_node"]["prefer"] = "ebr"
        roots = ["/r"]
        self.assertNotEqual(snapshots.fingerprint("x", a, roots),
                            snapshots.fingerprint("x", b, roots))
        self.assertNotEqual(hours.fingerprint("x", a, roots), hours.fingerprint("x", b, roots))
        # the node tier is never reduced into an hour
        self.assertEqual(hours.fingerprint("x", a, roots), hours.fingerprint("x", c, roots))

    def test_the_readers_take_it_from_the_config(self):
        tmp = tempfile.mkdtemp(prefix="rc_formats_cfg_")
        self.addCleanup(shutil.rmtree, tmp, True)
        cfg = plain_cfg()
        cfg.tree["feeds"]["ebpf"]["prefer"] = "ebr"
        cfg.tree["cache"]["dir"] = tmp
        reg = WindowRegistry(cfg, ["agent", "unlabeled"], lambda: [], threading.RLock())
        self.assertEqual((reg.prefer, reg.hours.prefer), ("ebr", "ebr"))
        self.assertEqual(iocmds.IoCommands(cfg, lambda: []).prefer, "ebr")


ebr_twins(globals())


if __name__ == "__main__":
    unittest.main()
