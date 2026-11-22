"""Saved windows: a resumed view must be the view a fresh build would give.

Same scratch-feed fixture as test_ingest. Builds run on the calling thread
(`WindowRegistry._build`) rather than the worker, so each test sees exactly one
build and nothing races it.

    cd dashboard && .venv/bin/python -m unittest discover -s tests -q
"""
import ast
import glob
import os
import pickle
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from rc_dashboard import feedparquet as fp         # noqa: E402
from rc_dashboard import history, replica, snapshots, windows   # noqa: E402
from rc_dashboard.aggregator import Aggregator     # noqa: E402
from rc_dashboard.export import window_plan, window_scan   # noqa: E402
from rc_dashboard.normalize import Normalizer      # noqa: E402
from rc_dashboard.windows import EVENTS, ViewFilter, WindowRegistry    # noqa: E402
from test_ingest import FeedCase, exit_rec, line, stamp    # noqa: E402

WIN = 1440
# Enough variety that every reducer holds state worth getting wrong: a shell
# whose child the dedup must match, a compute tool, a dangerous command, and a
# second host interleaved in time.
TOOLS = [("bash", "bash -lc make all"), ("make", "make all"), ("git", "git status"),
         ("python3", "python3 train.py"), ("pip", "pip install torch"),
         ("rm", "rm -rf /scratch/old")]


def varied_exit(epoch, i, host="h1"):
    comm, args = TOOLS[i % len(TOOLS)]
    r = exit_rec(epoch, pid=5000 + i, actor3=("agent", "human", "human-vscode")[i % 3],
                 host=host)
    r.update(comm=comm, args=args, ppid=4000 + i // 3, cpu_s=0.1 * (i % 7),
             io={"rd_mb": 0.5, "wr_mb": 0.1, "rchar_mb": 2.0, "wchar_mb": 0.2,
                 "scope": "process"},
             sandbox="unsandboxed", sandbox_src="ns", session_key="claude:%d" % (i % 4))
    return r


def skipped_rec(epoch, host="h1"):
    """A record no view reads. Not `conn`: the process index and the cascades
    take a long-lived agent's identity from its connection ticks. Not
    `residency_totals` either: the resources series samples memory from it."""
    return {"schema_version": 6, "event": "stop", "ts": stamp(epoch),
            "ts_epoch": epoch, "host": host, "records": 0, "reason": "test"}


class CacheCase(FeedCase):

    def registry(self, cfg=None):
        cfg = cfg or self.cfg()
        classes = list(cfg.get("filters.classes")) + ["unlabeled"]
        return WindowRegistry(cfg, classes, lambda: [self.root], threading.RLock())

    def build(self, reg, minutes=WIN):
        view = reg.request(minutes)
        reg._build(view)
        self.assertEqual((view.state, view.error), ("ready", None))
        return view

    def feed(self, start_min, n, host="h1"):
        """`n` exits a minute apart, the oldest `start_min` minutes ago, with a
        record between each pair that the views must skip."""
        recs = []
        for i in range(n):
            t = self.now - 60 * (start_min - i)
            recs.append(varied_exit(t, i, host))
            recs.append(skipped_rec(t + 1, host))
        self.append("%s.exits.jsonl" % host, recs)

    def snapshot_file(self, minutes=WIN):
        d = os.path.join(self.tmp, "cache", "windows")
        names = [n for n in os.listdir(d) if n.startswith("w%d." % minutes)]
        self.assertEqual(len(names), 1, names)
        return os.path.join(d, names[0])


class ScanTest(CacheCase):

    def test_the_event_prefilter_yields_exactly_what_parsing_every_line_would(self):
        self.feed(120, 60)
        self.feed(110, 40, host="h2")
        since = self.now - 3600 * 24
        plan, _ = window_plan([self.root], since)
        everything = [r["pid"] for r, _ in window_scan(plan, Normalizer(self.cfg()), since)
                      if r["event"] in EVENTS]
        plan, _ = window_plan([self.root], since)
        filtered = [r["pid"] for r, st in
                    window_scan(plan, Normalizer(self.cfg()), since, events=EVENTS)]
        self.assertEqual(filtered, everything)
        self.assertEqual(len(filtered), 100)

    def test_a_half_written_last_line_is_left_unread_and_unclaimed(self):
        recs = [varied_exit(self.now - 600 + i, i) for i in range(3)]
        done, tail = self.rounds([recs, [varied_exit(self.now - 10, 9)]])
        self.write_raw("h1.exits.jsonl", done + tail[:25])
        since = self.now - 3600
        plan, _ = window_plan([self.root], since)
        got = [r["pid"] for r, _ in window_scan(plan, Normalizer(self.cfg()), since)]
        self.assertEqual(got, [5000, 5001, 5002])
        # The saved offset stops before the fragment, so a resume reads it whole.
        self.assertEqual(plan[0]["end"], os.path.getsize(self.path("h1.exits.jsonl")) - 25)

    def test_lines_and_records_no_reader_can_read_fail_no_build(self):
        # `null`, `[]` and `0` lines and an exit whose `args` is a list: a
        # window's build raised at each, on every retry.
        recs = [varied_exit(self.now - 3600 + 60 * i, i) for i in range(12)]
        bad = varied_exit(self.now - 3600 + 60 * 6, 90)
        bad["args"] = ["make", "all"]
        with mock.patch("time.time", return_value=self.now):
            self.write_raw("h1.exits.jsonl", b"".join(map(line, recs)))
            want = self.build(self.registry()).payload["tool_mix"]
            os.unlink(self.path("h1.exits.jsonl"))
            self.write_raw("h1.exits.jsonl", b"".join(map(line, recs[:6])) + b"null\n[]\n0\n"
                           + line(bad) + b"".join(map(line, recs[6:])))
            self.assertEqual(self.build(self.registry()).payload["tool_mix"], want)

    def test_on_a_pinned_clock_a_build_is_still_timed(self):
        # `clock.as_of` freezes time.time: a build's elapsed runs on a clock that moves
        self.feed(30, 10)
        scan = windows.window_scan

        def slow(*a, **kw):
            time.sleep(0.25)
            yield from scan(*a, **kw)
        reg = self.registry()
        with mock.patch("time.time", return_value=self.now), \
                mock.patch.object(windows, "window_scan", slow):
            view = self.build(reg)
        self.assertEqual(view.progress["records"], 10)
        self.assertGreaterEqual(view.progress["elapsed_s"], 0.2)


class PrewarmTest(CacheCase):

    def test_a_prewarmed_window_queues_behind_and_is_evicted_first(self):
        reg = self.registry()
        reg.max_views = 3
        mine = reg.request(WIN)                     # the reader's window
        reg.prewarm([60, 360, 720])                 # room for two of them
        self.assertEqual(list(reg._queue), [WIN, 60, 360])
        self.assertEqual(list(reg.views), [360, 60, WIN])   # least recent first
        for v in list(reg.views.values()):
            v.state = "ready"
        reg.request(4320)                           # the reader opens a wide one
        self.assertIn(WIN, reg.views)
        self.assertIs(reg.views[WIN], mine)
        self.assertNotIn(360, reg.views)            # a prewarm made the room

    def test_prewarm_never_evicts_what_a_reader_opened(self):
        reg = self.registry()
        reg.max_views = 2
        reg.request(WIN)
        reg.request(60)
        reg.prewarm([360])
        self.assertEqual(sorted(reg.views), [60, WIN])


class CapTest(CacheCase):
    """`panels.max_windows` holds at every build, not only when a window is asked for.

    A request evicts only a ready view, so a window asked for while another one
    builds leaves both held. On boslogin05 on 2026-11-12, with the cap at 1, a
    12-hour view asked for during the 24-hour view's first build stayed beside it
    all night, and each drift rebuild then built a third state beside the two."""

    def asked_during_a_build(self, reg):
        first = reg.request(WIN)
        first.state = "building"                    # the worker is reducing it
        second = reg.request(720)                   # nothing ready to evict yet
        self.assertEqual(sorted(reg.views), [720, WIN])
        first.state = "ready"                       # its build ends
        return first, second

    def test_a_window_asked_for_during_a_build_takes_its_place_at_its_own_build(self):
        reg = self.registry()
        reg.max_views = 1
        _, second = self.asked_during_a_build(reg)
        reg._build(second)                          # the worker takes the next one
        self.assertEqual(list(reg.views), [720])
        self.assertEqual(second.state, "ready")

    def test_the_room_is_made_before_the_build_reads(self):
        reg = self.registry()
        reg.max_views = 1
        _, second = self.asked_during_a_build(reg)
        held = []
        real = windows.window_plan

        def plan(*a, **k):
            held.append(sorted(reg.views))
            return real(*a, **k)
        with mock.patch.object(windows, "window_plan", plan):
            reg._build(second)
        self.assertEqual(held, [[720]])

    def test_a_rebuild_drops_a_view_held_past_the_cap(self):
        reg = self.registry()
        reg.max_views = 1
        first, second = self.asked_during_a_build(reg)
        second.state = "ready"                      # both built: one past the cap
        reg._build(first)                           # the 24-hour view's drift rebuild
        self.assertEqual(list(reg.views), [WIN])
        self.assertIs(reg.views[WIN], first)

    def test_a_build_within_the_cap_evicts_nothing(self):
        reg = self.registry()
        reg.max_views = 2
        day, half = reg.request(WIN), reg.request(720)
        day.state = half.state = "ready"
        reg._build(day)
        self.assertEqual(sorted(reg.views), [720, WIN])

    def test_a_filtered_build_never_evicts_the_default_window(self):
        reg = self.registry()
        reg.max_views = 1
        default = self.cfg().get("live.window_min")
        dflt = reg.request(default)
        dflt.state = "ready"
        flt = reg.request(default, ViewFilter(["glen"]))    # held beside it, by design
        reg._build(flt)
        self.assertIs(reg.views.get(default), dflt)


class ResumeTest(CacheCase):

    def test_a_resumed_window_matches_a_fresh_build_over_the_same_records(self):
        self.feed(150, 90)
        # Every build at one present, as a pinned clock runs the service
        # (clock.py): the trajectories and cascades payloads stamp their window
        # to the second, and a resumed build and a fresh one that straddled a
        # second differed there and nowhere else.
        with mock.patch("time.time", return_value=self.now):
            self.build(self.registry())

            # Written while the service was down: a resume reads only these.
            self.feed(55, 50)
            self.feed(50, 30, host="h2")
            resumed = self.build(self.registry())
            self.assertEqual(resumed.cache["source"], "snapshot", resumed.cache["note"])
            self.assertEqual(resumed.progress["records"], 80)

            os.unlink(self.snapshot_file())
            fresh = self.build(self.registry())
        self.assertEqual(fresh.cache["source"], "scan")
        self.assertEqual(resumed.payload, fresh.payload)

    def test_a_resumed_window_keeps_its_age_and_reports_the_catch_up_as_drift(self):
        self.feed(150, 90)
        # On the wall clock, not at one present: there a resume that re-stamped
        # `built_at` would match the first build's all the same.
        first = self.build(self.registry())
        self.feed(55, 50)
        resumed = self.build(self.registry())
        self.assertEqual(resumed.built_at, first.built_at)
        self.assertEqual(resumed.covers_to, first.covers_to)
        # The newest catch-up record is 6 minutes ago; the first build ended at
        # the one 61 minutes ago.
        self.assertAlmostEqual(resumed.drift_s(), 55 * 60, delta=1)
        self.assertEqual(resumed.status()["cache"]["source"], "snapshot")

    def test_resuming_twice_reads_each_record_once(self):
        self.feed(150, 90)
        with mock.patch("time.time", return_value=self.now):     # as in the first test
            self.build(self.registry())
            self.feed(55, 20)
            self.build(self.registry())
            self.feed(30, 20)
            again = self.build(self.registry())
            self.assertEqual(again.progress["records"], 20)

            os.unlink(self.snapshot_file())
            self.assertEqual(again.payload, self.build(self.registry()).payload)

    def test_a_live_record_the_resume_already_read_is_not_counted_again(self):
        self.feed(150, 90)
        self.build(self.registry())
        self.feed(55, 10)
        reg = self.registry()
        view = self.build(reg)
        before = view.payload["tool_mix"]
        # The tail hands over the newest record the catch-up already read.
        newest = varied_exit(self.now - 60 * 46, 9)
        reg.feed(Normalizer(self.cfg()).normalize(newest))
        view.materialize()
        self.assertEqual(view.payload["tool_mix"], before)


class RefusalTest(CacheCase):

    def assertRescanned(self, view, why):
        self.assertEqual(view.cache["source"], "scan")
        self.assertIn(why, view.cache["note"])

    def test_a_replaced_day_file_is_rescanned(self):
        self.feed(150, 90)
        self.build(self.registry())
        # Rewritten with one byte of its first record changed: the same length
        # and the same line endings, so only the content tells the files apart.
        # Deleted first, so the file system is free to hand the new file the old
        # inode number -- which is why the inode alone is not its identity.
        self.replace_bytes("h1.exits.jsonl", b'"alice"', b'"alicf"')
        self.assertRescanned(self.build(self.registry()), "changed since")

    def test_a_snapshot_older_than_the_drift_budget_is_rescanned(self):
        self.feed(150, 90)
        reg = self.registry()
        self.build(reg)
        p = self.snapshot_file()
        with open(p, "rb") as fh:
            meta = pickle.load(fh)
            blob = fh.read()
        # 24h at 25% is a 6h budget.
        reg.store.save(WIN, [self.root], dict(meta, built_at=time.time() - 7 * 3600), blob)
        self.assertRescanned(self.build(self.registry()), "drift budget")

    def test_a_config_change_is_rescanned(self):
        self.feed(150, 90)
        self.build(self.registry())
        cfg = self.cfg()
        cfg.tree["reducers"]["io"]["top_n"] = 5
        self.assertRescanned(self.build(self.registry(cfg)), "different code, config")

    def test_a_snapshot_anyone_else_can_write_is_never_loaded(self):
        self.feed(150, 90)
        self.build(self.registry())
        os.chmod(self.snapshot_file(), 0o666)
        self.assertRescanned(self.build(self.registry()), "not private")

    def test_an_unreadable_snapshot_is_discarded_and_rescanned(self):
        self.feed(150, 90)
        self.build(self.registry())
        p = self.snapshot_file()
        with open(p, "rb") as fh:
            meta = pickle.load(fh)
        with open(p, "wb") as fh:
            pickle.dump(meta, fh)
            fh.write(b"not a pickle")
        self.assertRescanned(self.build(self.registry()), "unreadable")

    def test_a_drift_rebuild_scans_rather_than_resuming(self):
        self.feed(150, 90)
        reg = self.registry()
        view = self.build(reg)
        self.assertIn("no saved build", view.cache["note"])
        reg._build(view)
        self.assertEqual((view.cache["source"], view.cache["note"]), ("scan", None))


class RoundTripTest(CacheCase):

    def test_reducers_that_share_state_still_share_it_after_a_restore(self):
        # The process index is shared by the reducers that fold session keys,
        # and the cascades read the trajectory reducer. Restored as separate
        # copies, a resumed view would feed one and read another.
        self.feed(150, 90)
        view = self.build(self.registry())
        r = snapshots.restore(view.fresh_reducers(),
                              pickle.loads(snapshots.capture(view.reducers)))
        self.assertIs(r["trajectories"].procs, r["_procs"])
        self.assertIs(r["sandbox"].procs, r["_procs"])
        self.assertIs(r["cascades"].procs, r["_procs"])
        self.assertIs(r["cascades"].traj, r["trajectories"])

    def test_every_reducer_comes_back_with_the_state_it_left_with(self):
        self.feed(150, 90)
        view = self.build(self.registry())
        restored = snapshots.restore(view.fresh_reducers(),
                                     pickle.loads(snapshots.capture(view.reducers)))
        # Both results at one present: the trajectories and cascades results
        # stamp their window to the second, and two reads can straddle one.
        with mock.patch("time.time", return_value=self.now):
            for key, r in view.reducers.items():
                self.assertEqual(restored[key].result(), r.result(), key)


class SeamTest(CacheCase):
    """The live feed and a view's build split each day-file where the build
    stopped reading it, byte for byte (`PanelView.seam`): the tail, positioned
    at start-up, hands the view every record, and the view counts those its
    build did not read -- whatever their time."""

    def aggregator(self):
        agg = Aggregator(self.cfg())
        agg.backfill_now()                  # the tail is handed each file at its end
        return agg

    def built(self, agg, minutes=WIN):
        reg = agg.panels_by_window
        view = reg.request(minutes)
        reg._build(view)
        self.assertEqual((view.state, view.error), ("ready", None))
        return view

    @staticmethod
    def calls(view):
        view.materialize()
        return view.payload["tool_mix"]["by_class"]["agent"]["calls"]

    def lagging(self):
        """Two hosts, ten exits each 30..21 minutes ago; then one more from
        host b 5 s ago. Host a's link is down: nothing from it since."""
        for h in ("a", "b"):
            self.append("%s.exits.jsonl" % h, [exit_rec(self.now - 1800 + 60 * i, 100 + i,
                                                         host=h) for i in range(10)])
        self.append("b.exits.jsonl", [exit_rec(self.now - 5, 999, host="b")])

    def catch_up(self):
        """Host a's link is back: its 20 exits of the last 20 minutes arrive,
        each older than host b's newest, which the build read."""
        self.append("a.exits.jsonl", [exit_rec(self.now - 1200 + 60 * i, 200 + i, host="a")
                                      for i in range(20)])

    def test_a_host_that_lagged_when_the_view_was_built_is_counted_as_it_catches_up(self):
        # The review's r2: the view stayed at 21 while the live tier and the
        # disk said 41, until the next re-scan -- 6 h for the 24-hour view.
        self.lagging()
        agg = self.aggregator()
        view = self.built(agg)
        self.assertEqual(self.calls(view), 21)
        self.catch_up()
        self.assertEqual(agg.poll_once(), 20)
        self.assertEqual(self.events(agg)["agent"], 41)          # the live tier
        self.assertEqual(self.calls(view), 41)                   # and the view
        st = view.status()
        self.assertEqual((st["live_records"], st["live_skipped"]),
                         (20, {"already_read": 0, "before_window": 0}))
        self.assertEqual(st["drift_s"], 0.0)       # all of it older than host b's newest

    def test_a_line_the_build_read_is_not_counted_again_when_the_tail_reaches_it(self):
        self.append("h1.exits.jsonl", [exit_rec(self.now - 600 + i, 100 + i) for i in range(10)])
        agg = self.aggregator()
        # Written after the tail was positioned and before the build: the build
        # reads them, and the tail hands them over after it.
        self.append("h1.exits.jsonl", [exit_rec(self.now - 300 + i, 300 + i) for i in range(5)])
        view = self.built(agg)
        self.assertEqual(self.calls(view), 15)
        # Written after the build: the tail's alone.
        self.append("h1.exits.jsonl", [exit_rec(self.now - 200 + i, 400 + i) for i in range(3)])
        self.assertEqual(agg.poll_once(), 8)
        self.assertEqual(self.calls(view), 18)
        st = view.status()
        self.assertEqual((st["live_records"], st["live_skipped"]["already_read"]), (3, 5))

    def test_the_seam_holds_where_the_day_is_compressed_under_the_tail(self):
        # The build read host a's plain file; the tail, behind in it, meets the
        # rest in the `.gz` the collector sealed the day into -- the same bytes
        # at the same offsets, under another name and inode.
        self.lagging()
        agg = self.aggregator()
        early = [exit_rec(self.now - 1250 + i, 150 + i, host="a") for i in range(3)]
        self.append("a.exits.jsonl", early)              # the build reads these
        view = self.built(agg)
        self.assertEqual(self.calls(view), 24)
        self.catch_up()                                  # these it never saw
        self.compress("a.exits.jsonl")
        self.assertEqual(agg.poll_once(), 23)
        self.assertEqual(self.calls(view), 44)
        st = view.status()
        self.assertEqual((st["live_records"], st["live_skipped"]["already_read"]), (20, 3))

    def test_the_seam_holds_where_the_day_is_compressed_under_the_build(self):
        # The other way round: the tail reads the plain file while the build
        # runs (its records held for replay), the collector then seals the day,
        # and the build lists and reads the `.gz` alone. Each held record was
        # counted twice: its file was not one the seam named.
        self.append("h1.exits.jsonl", [exit_rec(self.now - 900 + i, 100 + i) for i in range(10)])
        agg = self.aggregator()
        self.append("h1.exits.jsonl", [exit_rec(self.now - 600 + i, 300 + i) for i in range(5)])
        reg = agg.panels_by_window
        view = reg.request(WIN)
        real = windows.window_plan

        def plan(*a, **k):
            self.assertEqual(agg.poll_once(), 5)       # held: the build is under way
            self.compress("h1.exits.jsonl")
            return real(*a, **k)
        with mock.patch.object(windows, "window_plan", plan):
            reg._build(view)
        self.assertEqual((view.state, view.error), ("ready", None))
        self.assertEqual(self.calls(view), 15)
        self.assertEqual(view.status()["live_skipped"]["already_read"], 5)

    def cut_short(self, name, recs):
        """The day-file truncated and written again in place: its inode the same."""
        p = self.path(name)
        ino = os.stat(p).st_ino
        with open(p, "r+b") as fh:
            fh.truncate(0)
            fh.write(b"".join(map(line, recs)))
        self.assertEqual(os.stat(p).st_ino, ino)

    def test_a_file_cut_short_in_place_after_the_build_is_counted_as_the_tail_reads_it(self):
        # The tail goes back to its start; the view held it as the file its build
        # read, so each record ending before the build's end there was taken as
        # read -- dropped from the view until the file grew past it, or a rebuild.
        self.append("h1.exits.jsonl", [exit_rec(self.now - 900 + i, 100 + i) for i in range(20)])
        agg = self.aggregator()
        view = self.built(agg)
        self.assertEqual(self.calls(view), 20)
        self.cut_short("h1.exits.jsonl", [exit_rec(self.now - 60 + i, 500 + i) for i in range(3)])
        self.assertEqual(agg.poll_once(), 3)
        self.assertEqual(agg.source.stats["resets"], 1)
        self.assertEqual(self.calls(view), 23)          # as the live tier counts them
        self.assertEqual(self.events(agg)["agent"], 23)
        self.assertEqual(view.status()["live_skipped"]["already_read"], 0)

    def test_a_file_cut_short_while_the_build_reads_it_is_counted_as_the_tail_reads_it(self):
        # The same, told to a build under way in order with the records held for
        # it: the seam it installs does not hold the file either.
        self.append("h1.exits.jsonl", [exit_rec(self.now - 900 + i, 100 + i) for i in range(20)])
        agg = self.aggregator()
        reg = agg.panels_by_window
        view = reg.request(WIN)
        real = windows.window_scan

        def scan(*a, **k):
            for got in real(*a, **k):
                yield got
            # the build has read the file whole; then it is cut short, and the
            # tail reads it from its start again
            self.cut_short("h1.exits.jsonl", [exit_rec(self.now - 60 + i, 500 + i)
                                              for i in range(3)])
            self.assertEqual(agg.poll_once(), 3)
        with mock.patch.object(windows, "window_scan", scan):
            reg._build(view)
        self.assertEqual((view.state, view.error), ("ready", None))
        self.assertEqual(self.calls(view), 23)
        self.assertEqual(view.status()["live_skipped"]["already_read"], 0)

    def test_a_file_cut_short_before_the_build_read_it_is_not_counted_again(self):
        # Cut short before the build read it, the tail noticing only after: the
        # seam is the build's read of what the file holds now, and the tail's
        # read of it from its start again is what the build counted.
        self.append("h1.exits.jsonl", [exit_rec(self.now - 900 + i, 100 + i) for i in range(20)])
        agg = self.aggregator()
        self.cut_short("h1.exits.jsonl", [exit_rec(self.now - 60 + i, 500 + i) for i in range(3)])
        view = self.built(agg)
        self.assertEqual(self.calls(view), 3)
        self.assertEqual(agg.poll_once(), 3)
        self.assertEqual(agg.source.stats["resets"], 1)
        self.assertEqual(self.calls(view), 3)
        self.assertEqual(view.status()["live_skipped"]["already_read"], 3)

    def test_a_restart_held_before_the_build_read_the_file_is_not_counted_again(self):
        # The same, the tail noticing while the build is under way, before it
        # reads the file: the notice is held with the records, and the seam the
        # build installs is of what it read, which is what the file holds now.
        self.append("h1.exits.jsonl", [exit_rec(self.now - 900 + i, 100 + i) for i in range(20)])
        agg = self.aggregator()
        self.cut_short("h1.exits.jsonl", [exit_rec(self.now - 60 + i, 500 + i) for i in range(3)])
        reg = agg.panels_by_window
        view = reg.request(WIN)
        real = windows.window_plan

        def plan(*a, **k):
            self.assertEqual(agg.poll_once(), 3)       # held: the build is under way
            return real(*a, **k)
        with mock.patch.object(windows, "window_plan", plan):
            reg._build(view)
        self.assertEqual((view.state, view.error), ("ready", None))
        self.assertEqual(self.calls(view), 3)
        self.assertEqual(view.status()["live_skipped"]["already_read"], 3)

    def test_a_file_cut_and_grown_back_past_the_build_with_its_first_bytes_is_counted(self):
        # Cut short after the build and written again past where the build read
        # to, its first 4 KB as they were: the size and the head said "the file
        # the build read", so the tail's read of it from its start was skipped
        # as read up to the build's end -- though the bytes there are new. The
        # file was still in the seam: what the tail read past the build's end
        # was no record the view takes, and the tail noticed the cut because
        # the file was shorter than where it had read to.
        first = [exit_rec(self.now - 900 + i, 100 + i) for i in range(40)]
        self.append("h1.exits.jsonl", first)
        agg = self.aggregator()
        view = self.built(agg)
        end = os.path.getsize(self.path("h1.exits.jsonl"))
        self.assertGreater(len(b"".join(map(line, first[:20]))), 4096)
        self.append("h1.exits.jsonl", [skipped_rec(self.now - 500 + i) for i in range(40)])
        agg.poll_once()
        self.assertIn(self.path("h1.exits.jsonl"), view.seam)
        self.cut_short("h1.exits.jsonl", first[:20] + [exit_rec(self.now - 60 + i, 500 + i)
                                                       for i in range(30)])
        self.assertGreater(os.path.getsize(self.path("h1.exits.jsonl")), end)
        self.assertEqual(agg.poll_once(), 50)
        self.assertEqual(agg.source.stats["resets"], 1)
        self.assertEqual(self.calls(view), 90)          # as the live tier counts them
        self.assertEqual(self.events(agg)["agent"], 90)
        self.assertEqual(view.status()["live_skipped"]["already_read"], 0)

    def test_a_restart_reads_each_file_once_and_outside_the_lock(self):
        # What a restarted file holds now was read under the aggregator's lock,
        # by each view, while every reader waited.
        self.append("h1.exits.jsonl", [exit_rec(self.now - 900 + i, 100 + i) for i in range(20)])
        agg = self.aggregator()
        views = [self.built(agg), self.built(agg, 60)]
        self.cut_short("h1.exits.jsonl", [exit_rec(self.now - 60 + i, 500 + i) for i in range(3)])
        reads, real = [], windows.Look

        def look(*a, **k):
            reads.append(agg.lock._is_owned())
            return real(*a, **k)
        with mock.patch.object(windows, "Look", look):
            self.assertEqual(agg.poll_once(), 3)
        self.assertEqual(reads, [False])
        self.assertEqual([self.calls(v) for v in views], [23, 23])

    def building(self, agg, before_plan=None, after_scan=None, after_check=None):
        """A view built with `before_plan()` run as it plans its read,
        `after_scan()` once it has read every file, and `after_check()` once it
        has checked the restart notices held for it (`restarts_checked`): what
        happens to the files and the tail while the build is under way."""
        reg = agg.panels_by_window
        view = reg.request(WIN)
        plan, scan, check = windows.window_plan, windows.window_scan, reg.restarts_checked

        def planned(*a, **k):
            if before_plan:
                before_plan()
            return plan(*a, **k)

        def scanned(*a, **k):
            yield from scan(*a, **k)
            if after_scan:
                after_scan()

        def checked(*a, **k):
            out = check(*a, **k)
            if after_check:
                after_check()
            return out
        with mock.patch.object(windows, "window_plan", planned), \
                mock.patch.object(windows, "window_scan", scanned), \
                mock.patch.object(reg, "restarts_checked", checked):
            reg._build(view)
        self.assertEqual((view.state, view.error), ("ready", None))
        return view

    def test_a_file_replaced_while_the_build_runs_is_counted_once(self):
        # The review's case: the tail holds a record of the old file, then the
        # file is replaced (another inode) and the tail reads the new one from
        # its start; the build reads the new one. The old file's record, its
        # inode not the seam's, took the seam with it, and the new file's
        # records -- which the build had read -- were counted again: 9, not 5.
        self.append("h1.exits.jsonl", [exit_rec(self.now - 900 + i, 100 + i) for i in range(10)])
        agg = self.aggregator()
        p = self.path("h1.exits.jsonl")

        def replace():
            self.append("h1.exits.jsonl", [exit_rec(self.now - 600, 200)])
            self.assertEqual(agg.poll_once(), 1)
            with open(p + ".new", "wb") as fh:
                fh.write(b"".join(line(exit_rec(self.now - 60 + i, 500 + i)) for i in range(4)))
            os.replace(p + ".new", p)               # a new inode under the name
            self.assertEqual(agg.poll_once(), 4)
        view = self.building(agg, before_plan=replace)
        self.assertEqual(self.calls(view), 5)
        self.assertEqual(view.status()["live_skipped"]["already_read"], 4)

    def test_records_held_before_a_cut_the_build_read_are_not_counted_again(self):
        # Held before the file was cut, read by the build, which read the file
        # before the cut: they are what the seam is of, though the cut drops it.
        self.append("h1.exits.jsonl", [exit_rec(self.now - 900 + i, 100 + i) for i in range(20)])
        agg = self.aggregator()

        def more():
            self.append("h1.exits.jsonl", [exit_rec(self.now - 600 + i, 300 + i)
                                           for i in range(5)])
            self.assertEqual(agg.poll_once(), 5)

        def cut():
            self.cut_short("h1.exits.jsonl", [exit_rec(self.now - 60 + i, 500 + i)
                                              for i in range(3)])
            self.assertEqual(agg.poll_once(), 3)
        view = self.building(agg, before_plan=more, after_scan=cut)
        self.assertEqual(self.calls(view), 28)          # 25 read, then 3 new
        self.assertEqual(self.events(agg)["agent"], 28)
        self.assertEqual(view.status()["live_skipped"]["already_read"], 5)

    def test_records_held_before_a_cut_the_build_read_after_are_counted_once(self):
        # Held before the file was cut and gone from it after; the build read
        # what the cut left: they are another generation of the file than the
        # one the seam is of, and must not take the seam with them.
        self.append("h1.exits.jsonl", [exit_rec(self.now - 900 + i, 100 + i) for i in range(20)])
        agg = self.aggregator()

        def more_then_cut():
            self.append("h1.exits.jsonl", [exit_rec(self.now - 600 + i, 300 + i)
                                           for i in range(5)])
            self.assertEqual(agg.poll_once(), 5)
            self.cut_short("h1.exits.jsonl", [exit_rec(self.now - 60 + i, 500 + i)
                                              for i in range(3)])

        def noticed():
            self.assertEqual(agg.poll_once(), 3)
        view = self.building(agg, before_plan=more_then_cut, after_scan=noticed)
        self.assertEqual(self.calls(view), 8)           # the 3 it read, the 5 held
        self.assertEqual(view.status()["live_skipped"]["already_read"], 3)

    def test_a_file_cut_again_after_its_notices_were_checked_is_counted_once(self):
        # The review's case: the setup above, then the file cut again, to 2 new
        # records, after the notices held for the build were checked and before
        # it installs. The second notice took the first's decision: it kept a
        # seam that no longer describes the file, the 2 new records were skipped
        # against it, and the 3 the build read were fed as unread: 11, not 10.
        self.append("h1.exits.jsonl", [exit_rec(self.now - 900 + i, 100 + i) for i in range(20)])
        agg = self.aggregator()

        def more_then_cut():
            self.append("h1.exits.jsonl", [exit_rec(self.now - 600 + i, 300 + i)
                                           for i in range(5)])
            self.assertEqual(agg.poll_once(), 5)
            self.cut_short("h1.exits.jsonl", [exit_rec(self.now - 60 + i, 500 + i)
                                              for i in range(3)])

        def noticed():
            self.assertEqual(agg.poll_once(), 3)

        def cut_again():
            self.cut_short("h1.exits.jsonl", [exit_rec(self.now - 30 + i, 700 + i)
                                              for i in range(2)])
            self.assertEqual(agg.poll_once(), 2)
        view = self.building(agg, before_plan=more_then_cut, after_scan=noticed,
                             after_check=cut_again)
        self.assertEqual(self.calls(view), 10)          # the 3 read, the 5 held, the 2 new
        self.assertEqual(view.status()["live_skipped"]["already_read"], 3)

    def test_a_catch_up_from_before_the_window_is_left_out(self):
        # A scan now would leave out a record older than the window's start, so
        # the live feed does too, and says how many.
        self.append("h1.exits.jsonl", [exit_rec(self.now - 600 + i, 100 + i) for i in range(10)])
        agg = self.aggregator()
        view = self.built(agg, 60)
        self.append("h2.exits.jsonl", [exit_rec(self.now - 7200 + i, 500 + i, host="h2")
                                       for i in range(4)] + [exit_rec(self.now - 30, 600, host="h2")])
        self.assertEqual(agg.poll_once(), 5)
        self.assertEqual(self.calls(view), 11)
        self.assertEqual(view.status()["live_skipped"], {"already_read": 0, "before_window": 4})


class SealedDayTest(CacheCase):
    """A window reads a sealed day from its replica (`history.py`): one whose
    table is gone or damaged must not stand in for the day. The review's r3:
    a filtered 24 h view built `ready` with 5 agent calls where the window held
    105, or failed on every retry."""

    SEALED = "2026-09-24"

    def setUp(self):
        super().setUp()
        recs = []
        for i in range(100):                    # 20 h ago, alice's and bob's
            r = exit_rec(self.now - 20 * 3600 + i, 100 + i)
            r["user"] = "alice" if i % 2 else "bob"
            recs.append(r)
        self.append("h1.exits.jsonl", recs, day=self.SEALED)
        self.compress("h1.exits.jsonl", day=self.SEALED)
        self.append("h1.exits.jsonl", [exit_rec(self.now - 600 + i, 900 + i) for i in range(5)])
        self.store = history.HistoryStore(self.cfg())
        self.assertEqual(self.store.run_once(), 1)
        replica.install(self.store.replica_for, self.store.replica_failed)
        self.addCleanup(replica.install, None)
        self.table = fp.out_path(self.store.roots[os.path.abspath(self.root)], "exit",
                                 self.SEALED, "h1.exits")

    def calls(self):
        reg = self.registry()
        view = reg.request(WIN, ViewFilter(["alice", "bob"]))
        reg._build(view)
        self.assertEqual((view.state, view.error), ("ready", None))
        return view.payload["tool_mix"]["by_class"]["agent"]["calls"]

    def damage(self, cut):
        with open(self.table, "rb") as fh:
            whole = fh.read()
        if cut is None:
            os.unlink(self.table)
        else:
            with open(self.table, "wb") as fh:
                fh.write(whole[:cut])

    def test_the_whole_window_through_an_intact_replica(self):
        self.assertEqual(self.calls(), 105)
        self.assertGreater(self.store.status()["lookups"]["replica"], 0)

    def test_a_table_gone_is_read_around(self):
        self.damage(None)
        self.assertEqual(self.calls(), 105)

    def test_a_table_cut_short_is_read_around(self):
        self.damage(os.path.getsize(self.table) // 2)
        self.assertEqual(self.calls(), 105)

    def test_a_table_emptied_is_read_around(self):
        self.damage(0)
        self.assertEqual(self.calls(), 105)

    def test_a_table_damaged_under_a_replica_taken_already_is_read_around(self):
        gz_path = self.path("h1.exits.jsonl.gz", day=self.SEALED)
        self.assertIsNotNone(replica.for_path(gz_path))       # taken, and held
        size = os.path.getsize(self.table)
        with open(self.table, "r+b") as fh:
            fh.write(b"\0" * size)                           # its size intact
        self.assertEqual(self.calls(), 105)
        st = self.store.status()
        self.assertEqual([e["file"] for e in st["errors"]], [gz_path])
        self.assertEqual(st["read_failures"], 1)


def _imports(path):
    """The package modules one source file imports, anywhere in it."""
    here, out = os.path.dirname(path), set()
    for n in ast.walk(ast.parse(Path(path).read_text())):
        if not isinstance(n, ast.ImportFrom) or not n.level:
            continue
        base = here
        for _ in range(n.level - 1):
            base = os.path.dirname(base)
        mod = os.path.join(base, *n.module.split(".")) if n.module else base
        if os.path.exists(mod + ".py"):
            out.add(mod + ".py")
            continue
        for a in n.names:       # a package: the submodules named, or its __init__
            sub = os.path.join(mod, a.name + ".py")
            out.add(sub if os.path.exists(sub) else os.path.join(mod, "__init__.py"))
    return {os.path.normpath(p) for p in out if os.path.exists(p)}


def _reached(roots, stop):
    """Every package module `roots` import, in any number of steps, without
    walking on past a module in `stop`. Paths are the package's own."""
    pkg = snapshots.PKG
    seen, todo = set(), [os.path.join(pkg, r) for r in roots]
    while todo:
        p = todo.pop()
        if p in seen:
            continue
        seen.add(p)
        if os.path.relpath(p, pkg) not in stop:
            todo.extend(_imports(p))
    return {os.path.relpath(p, pkg) for p in seen}


def _reducers():
    return {"reducers/" + os.path.basename(p)
            for p in glob.glob(os.path.join(snapshots.PKG, "reducers", "*.py"))}


class FingerprintTest(unittest.TestCase):

    def test_every_module_a_saved_build_counts_with_is_in_its_fingerprint(self):
        # The hours are in: a filtered build's exits take the own figures they
        # worked out (`hours.OwnFigures`). Out on purpose: clock.py, which says
        # which hours are closed, feeds.py,
        # which finds the day-files whose roots the fingerprint holds,
        # anonymize.py, whose home patterns the trajectories read only to name
        # a task's project when a read builds a phase block (`_phase_cache`,
        # which SNAPSHOT_SKIP leaves unsaved), payloads.py, which saves the
        # panels a build served and nothing a build reads, and keysearch.py, the
        # session inspector's read fanned out over worker processes, which reads
        # what proctree's own read does and nothing any saved state holds.
        out = {"clock.py", "feeds.py", "anonymize.py", "payloads.py", "keysearch.py"}
        got = _reached(["normalize.py", "export.py", "windows.py", "snapshots.py",
                        "redact.py"] + sorted(_reducers()), out)
        self.assertEqual(got - set(snapshots.CODE_FILES) - _reducers() - out, set())

    def test_every_module_an_hour_counts_with_is_in_its_fingerprint(self):
        # The hours leave the live tier out of their fingerprint, so no reducer
        # an hour holds may reach it. Out on purpose: clock.py, which says which
        # hours are closed, feeds.py, which finds the day-files, and
        # anonymize.py and keysearch.py, as for a saved build.
        from rc_dashboard import hours
        held = _reducers() - {"reducers/live.py"}
        out = {"clock.py", "feeds.py", "anonymize.py", "keysearch.py"}
        got = _reached(["normalize.py", "export.py", "redact.py", "hours.py"] + sorted(held),
                       out)
        self.assertNotIn("reducers/live.py", got)
        self.assertEqual(got - set(hours.CODE_FILES) - held - out, set())

    def test_a_change_to_the_roles_is_a_change_of_code(self):
        # roles.py decides which processes are sessions, so a build saved under
        # the old roles must not resume under new ones. Only the hours held it
        # in their fingerprint, and a filtered view resumes a saved build,
        # never the hours.
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        pkg = os.path.join(tmp, "rc_dashboard")
        shutil.copytree(snapshots.PKG, pkg, ignore=shutil.ignore_patterns("__pycache__"))
        self.addCleanup(setattr, snapshots, "PKG", snapshots.PKG)
        snapshots.PKG = pkg
        before = snapshots.code_digest()
        with open(os.path.join(pkg, "roles.py"), "a") as fh:
            fh.write("\n# one more helper flag\n")
        self.assertNotEqual(snapshots.code_digest(), before)

    def test_what_decides_an_hour_decides_a_saved_build(self):
        # A saved build and a saved hour hold the state of the same normaliser
        # and reducers, so a module that changes what an hour means changes
        # what a saved build means. hours.py too: a filtered build's exits
        # took the own figures the hours worked out (`hours.OwnFigures`).
        from rc_dashboard import hours
        self.assertEqual(set(hours.CODE_FILES) - set(snapshots.CODE_FILES), set())


if __name__ == "__main__":
    unittest.main()
