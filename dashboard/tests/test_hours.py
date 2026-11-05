"""The hour store: windows merged from saved hours, against windows scanned.

A scratch collector root as in test_ingest, the feed shaped as in test_merge,
and a pinned clock (`time.time` patched, as `clock.as_of` pins it) so a
merged and a scanned build see the same window. Builds run on the calling
thread (`WindowRegistry._build`), as in test_window_cache.

    cd dashboard && .venv/bin/python -m unittest discover -s tests -q
"""
import json
import os
import pickle
import shutil
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from rc_dashboard import hours                     # noqa: E402
from rc_dashboard.windows import HOURS_JOB, ViewFilter, WindowRegistry   # noqa: E402
from test_ingest import DAY, FeedCase             # noqa: E402
import test_merge as TM                           # noqa: E402

HOUR = 3600
NOW = (int(time.time()) // HOUR) * HOUR + 1500.0   # 25 minutes into an hour
WIN = 200                                          # minutes: since is mid-hour
SINCE = NOW - WIN * 60


def gen(t0, t1, seed=7):
    """test_merge's stream, between t0 and t1."""
    bounds = [h * HOUR for h in range(int(t0 // HOUR) + 1, int(t1 // HOUR) + 1)]
    with mock.patch.multiple(TM, T0=t0, T1=t1, BOUNDS=bounds):
        return TM.stream(seed)


class HourCase(FeedCase):

    def setUp(self):
        super().setUp()
        self.clock = mock.patch("time.time", return_value=NOW)
        self.clock.start()

    def tearDown(self):
        self.clock.stop()
        super().tearDown()

    def write(self, recs, day=DAY):
        by_host = {}
        for r in recs:
            by_host.setdefault(r["host"], []).append(r)
        for host, rs in by_host.items():
            self.append("%s.jsonl" % host, rs, day=day)

    def cfg(self, merge=True, **panels):
        c = super().cfg()
        c.tree["panels"].update(merge_reduce=merge, hour_pause_s=0, **panels)
        return c

    def registry(self, cfg):
        classes = list(cfg.get("filters.classes")) + ["unlabeled"]
        return WindowRegistry(cfg, classes, lambda: [self.root], threading.RLock())

    def build(self, reg, minutes=WIN, flt=None):
        view = reg.request(minutes, flt)
        reg._build(view)
        self.assertEqual((view.state, view.error), ("ready", None))
        return view

    @staticmethod
    def payload(view):
        return pickle.loads(pickle.dumps(view.payload))


class MergedWindowTest(HourCase):

    def setUp(self):
        super().setUp()
        # From the window's start on. A record from earlier in the window's
        # first hour is reduced into that hour, and so is context the hours
        # after it are joined with -- a merged window knows a little more at
        # its start than one pass that began there (test_edge_context).
        self.write(gen(SINCE + 1, NOW - 30))

    def test_a_merged_window_is_the_scanned_window(self):
        want = self.payload(self.build(self.registry(self.cfg(merge=False))))
        view = self.build(self.registry(self.cfg()))
        self.assertEqual(view.cache["source"], "hours")
        self.assertEqual(view.progress["mode"], "merge")
        got = self.payload(view)
        for key in sorted(want):
            with self.subTest(reducer=key):
                self.assertEqual(TM.diff(want[key], got[key]), [])

    def test_switching_windows_and_restarting_reduce_no_hour_twice(self):
        cfg = self.cfg()
        reg = self.registry(cfg)
        self.build(reg)
        first = reg.hours.stats["hours_reduced"]
        self.assertGreaterEqual(first, 3)
        self.build(reg, 120)
        self.build(reg, WIN - 20)
        self.assertEqual(reg.hours.stats["hours_reduced"], first)
        # a restart: a new registry over the same saved hours
        again = self.registry(cfg)
        view = self.build(again)
        self.assertEqual(again.hours.stats["hours_reduced"], 0)
        self.assertIn("reducing 0 new hour", view.cache["note"])

    def test_a_drifted_window_is_merged_again_not_read_again(self):
        reg = self.registry(self.cfg())
        view = self.build(reg)
        n = reg.hours.stats["hours_reduced"]
        reg._build(view)
        self.assertEqual(view.state, "ready")
        self.assertEqual(reg.hours.stats["hours_reduced"], n)
        self.assertEqual(view.cache["source"], "hours")

    def test_other_reducer_config_never_merges_these_hours(self):
        self.build(self.registry(self.cfg()))
        cfg = self.cfg()
        cfg.tree["reducers"]["tool_mix"]["head_n"] = 5
        reg = self.registry(cfg)
        self.build(reg)
        self.assertGreater(reg.hours.stats["hours_reduced"], 0)

    def test_a_panels_knob_or_another_feeds_path_does_not_discard_the_hours(self):
        self.build(self.registry(self.cfg()))
        cfg = self.cfg(max_drift_pct=40, prewarm_min=[60])
        cfg.tree["feeds"]["sacct"]["dir"] = "/elsewhere"
        reg = self.registry(cfg)
        self.build(reg)
        self.assertEqual(reg.hours.stats["hours_reduced"], 0)

    def test_a_filtered_view_is_still_scanned(self):
        reg = self.registry(self.cfg())
        view = self.build(reg, flt=ViewFilter(users=["bob"]))
        self.assertEqual(view.cache["source"], "scan")

    def test_a_window_with_no_closed_hour_is_scanned(self):
        reg = self.registry(self.cfg())
        view = self.build(reg, 10)
        self.assertEqual(view.cache["source"], "scan")

    def test_a_reducer_without_merge_falls_back_to_a_scan(self):
        from rc_dashboard.reducers.facets import FacetReducer
        with mock.patch.object(FacetReducer, "merge", None):
            view = self.build(self.registry(self.cfg()))
        self.assertEqual(view.cache["source"], "scan")

    def test_hour_files_are_private_and_compressed(self):
        reg = self.registry(self.cfg())
        self.build(reg)
        d = os.path.join(self.tmp, "cache", "hours")
        self.assertEqual(os.stat(d).st_mode & 0o777, 0o700)
        names = [n for n in os.listdir(d) if n.startswith("h")]
        self.assertTrue(names)
        for n in names:
            self.assertEqual(os.stat(os.path.join(d, n)).st_mode & 0o777, 0o600)
        # a file anyone else can write is never loaded: it is reduced again
        h = int(names[0][1:].split(".")[0])
        os.chmod(os.path.join(d, names[0]), 0o666)
        self.assertIsNone(reg.hours.load("h", h))


class LateRecordTest(HourCase):

    def test_a_record_for_a_closed_hour_is_merged_into_it(self):
        # Hours are reduced through the newest closed one; then a host writes a
        # record dated two hours back, and the feed moves on an hour.
        early = gen(NOW - 6 * HOUR + 777, NOW - 2 * HOUR)
        self.write(early)
        reg = self.registry(self.cfg())
        self.assertEqual(self.build(reg, 300).cache["source"], "hours")
        late_t = NOW - 3 * HOUR + 120
        late = TM.plain("exit", late_t, 999999, 1, "pip", "pip install late",
                        user="erin")
        self.write([late] + gen(NOW - 2 * HOUR + 1, NOW - 30, seed=9))
        view = self.build(reg, 300)
        h = int(late_t // HOUR)
        self.assertEqual(reg.hours.meta(h)["stragglers"], 1)
        got = self.payload(view)
        self.assertIn("erin", [u["user"] for u in got["facets"]["users"]])
        # an hour read again meets the same late record, and does not take it twice
        store = reg.hours
        nxt = h + 1
        store._drop("h", nxt)
        store.ensure([self.root], nxt, nxt + 1)
        self.assertEqual(store.meta(h)["stragglers"], 1)
        view = self.build(reg, 300)
        got = self.payload(view)
        # and the window holds what a scan of the same files holds
        want = self.payload(self.build(self.registry(self.cfg(merge=False)), 300))
        for key in ("facets", "io_process", "risk", "resources", "tool_mix"):
            with self.subTest(reducer=key):
                self.assertEqual(TM.diff(want[key], got[key]), [])


class CompressedDayTest(HourCase):
    """At the rollover the collector compresses the day that closed in place
    (`FeedCase.compress`): the same bytes under a new name and a new inode. An
    hour read after that must still know where the hour before it stopped."""

    NEXT = "2026-09-26"

    def compress_day(self):
        for name in os.listdir(os.path.join(self.root, DAY)):
            self.compress(name)

    def test_a_day_compressed_between_two_hours_is_counted_once(self):
        # Hours reduced through the newest closed one from the plain files;
        # then midnight: the day is compressed, the next day's files go on.
        self.write(gen(NOW - 6 * HOUR + 777, NOW - 2 * HOUR))
        reg = self.registry(self.cfg())
        self.assertEqual(self.build(reg, 300).cache["source"], "hours")
        last = reg.hours.chain[0]
        self.compress_day()
        self.write(gen(NOW - 2 * HOUR + 1, NOW - 30, seed=9), day=self.NEXT)
        view = self.build(reg, 300)
        self.assertGreater(reg.hours.chain[0], last)
        # The next hour's read starts SCAN_SLACK_S early, in records the hour
        # before already took: they are not late arrivals for it.
        self.assertEqual(reg.hours.meta(last)["stragglers"], 0)
        got = self.payload(view)
        want = self.payload(self.build(self.registry(self.cfg(merge=False)), 300))
        # Two batches from one generator: these two join across their seam
        # differently in a merge and in a scan with no compression at all.
        for key in sorted(set(want) - {"_procs", "trajectories"}):
            with self.subTest(reducer=key):
                self.assertEqual(TM.diff(want[key], got[key]), [])

    def test_a_late_record_met_again_after_its_day_was_compressed_is_taken_once(self):
        # LateRecordTest's late record, merged into its hour from the plain
        # file; then the day is compressed and the hour after read again.
        self.write(gen(NOW - 6 * HOUR + 777, NOW - 2 * HOUR))
        reg = self.registry(self.cfg())
        self.build(reg, 300)
        late_t = NOW - 3 * HOUR + 120
        late = TM.plain("exit", late_t, 999999, 1, "pip", "pip install late",
                        user="erin")
        self.write([late] + gen(NOW - 2 * HOUR + 1, NOW - 30, seed=9))
        self.build(reg, 300)
        store, h = reg.hours, int(late_t // HOUR)
        self.assertEqual(store.meta(h)["stragglers"], 1)
        self.compress_day()
        store._drop("h", h + 1)
        store.ensure([self.root], h + 1, h + 2)
        self.assertEqual(store.meta(h)["stragglers"], 1)


class AdvanceTest(HourCase):

    def test_hours_that_close_later_are_reduced_behind_the_builds(self):
        self.write(gen(NOW - 6 * HOUR + 777, NOW - 2 * HOUR))
        reg = self.registry(self.cfg())
        self.build(reg, 300)
        store = reg.hours
        last, done = store.chain[0], store.stats["hours_reduced"]
        reg.refresh()
        self.assertNotIn(HOURS_JOB, reg._queue)      # nothing new has closed
        self.write(gen(NOW - 2 * HOUR + 1, NOW - 30, seed=5))
        store.saw(NOW - 30)
        reg.refresh()
        self.assertIn(HOURS_JOB, reg._queue)
        # the worker runs it, between window builds
        with reg._cv:
            reg._queue.clear()
        reg._enqueue(HOURS_JOB)
        reg.start()
        try:
            deadline = time.monotonic() + 30
            while store.chain[0] != last + 2 and time.monotonic() < deadline:
                time.sleep(0.05)
        finally:
            reg.stop()
        self.assertEqual(store.chain[0], last + 2)
        self.assertEqual(store.stats["hours_reduced"], done + 2)
        st = reg.status()                               # never listed as a window
        self.assertNotIn(HOURS_JOB, st["queued"])
        self.assertEqual(st["queued_filtered"], [])
        view = self.build(reg, 300)
        self.assertIn("reducing 0 new hour", view.cache["note"])


class InterruptedBuildTest(HourCase):
    """R67: a build stopped after it saved an hour is resumed as the build that was never stopped
    would have made the window. Every hour a run saves also saves the join state it carries out of
    that hour (its chain), so the resume starts at the last saved hour, from that hour's chain,
    and reduces nothing twice."""

    def setUp(self):
        super().setUp()
        self.write(gen(SINCE + 1, NOW - 30))

    def wipe(self):
        for d in ("hours", "windows"):
            shutil.rmtree(os.path.join(self.tmp, "cache", d), ignore_errors=True)

    def stop_after(self, k):
        """A patch that stops every build at its `k`th saved hour, as a killed service would, and
        the hours each build saved."""
        real, saved = hours.HourStore._save_hour, []

        def save_then_stop(store, hour, red, meta):
            size = real(store, hour, red, meta)
            saved.append(hour)
            if len(saved) % k == 0:
                raise RuntimeError("stopped")
            return size
        return mock.patch.object(hours.HourStore, "_save_hour", save_then_stop), saved

    def chains(self):
        d = os.path.join(self.tmp, "cache", "hours")
        return sorted(int(n[1:].split(".")[0]) for n in os.listdir(d)
                      if n[0] == "c" and n[1:2].isdigit())

    def same(self, want, view):
        self.assertEqual(view.cache["source"], "hours")
        got = self.payload(view)
        for key in sorted(want):
            with self.subTest(reducer=key):
                self.assertEqual(TM.diff(want[key], got[key]), [])

    def test_a_build_interrupted_after_an_hour_is_resumed_from_that_hours_chain(self):
        want = self.payload(self.build(self.registry(self.cfg(merge=False))))
        cfg = self.cfg()
        clean = self.registry(cfg)
        self.assertEqual(TM.diff(want["trajectories"], self.payload(self.build(clean))["trajectories"]), [])
        whole = clean.hours.stats["hours_reduced"]          # what a build never stopped reduces
        self.assertGreaterEqual(whole, 3)
        newest = clean.hours.chain[0]
        self.assertEqual(self.chains(), [newest])           # a completed run keeps its last chain only
        for stop_after in (1, 2):
            with self.subTest(stop_after=stop_after):
                self.wipe()
                patch, saved = self.stop_after(stop_after)
                with patch:
                    self.build(self.registry(cfg))          # the build falls back to a scan
                self.assertEqual(len(saved), stop_after)
                self.assertEqual(self.chains()[-1], saved[-1])  # the last saved hour's chain is saved
                again = self.registry(cfg)                  # a restart, same code and config
                view = self.build(again)
                # from the last saved hour's chain: no hour reduced twice
                self.assertEqual(again.hours.stats["hours_reduced"], whole - stop_after)
                self.same(want, view)
                self.assertEqual(self.chains(), [newest])

    def test_hours_saved_with_no_chain_are_reduced_again_from_where_their_run_began(self):
        # As the code before per-hour chains left them: `_began`'s walk back is the fallback
        want = self.payload(self.build(self.registry(self.cfg(merge=False))))
        cfg = self.cfg()
        clean = self.registry(cfg)
        self.build(clean)
        whole = clean.hours.stats["hours_reduced"]
        self.wipe()
        patch, saved = self.stop_after(2)
        with patch, mock.patch.object(hours.HourStore, "_save_chain", lambda *a, **k: 0):
            self.build(self.registry(cfg))
        self.assertEqual(len(saved), 2)
        self.assertEqual(self.chains(), [])
        again = self.registry(cfg)
        view = self.build(again)
        self.assertEqual(again.hours.stats["hours_reduced"], whole)
        self.same(want, view)

    def test_a_run_before_the_chain_stopped_is_resumed_from_its_last_hour(self):
        # (b): the window widened past the saved hours (a narrower window saved the newest), and
        # the build stopped within the run before them: mid-run, and after its last hour, before
        # the saved hour after it is reduced again with the run's join state
        want = self.payload(self.build(self.registry(self.cfg(merge=False))))
        cfg = self.cfg()
        h_hi = self.registry(cfg).hours.closed_end(NOW, [self.root])
        for stop_after in (1, 2):
            with self.subTest(stop_after=stop_after):
                self.wipe()
                self.registry(cfg).hours.ensure([self.root], h_hi - 1, h_hi)
                self.assertEqual(self.chains(), [h_hi - 1])
                patch, saved = self.stop_after(stop_after)
                with patch:
                    self.build(self.registry(cfg))
                self.assertEqual(saved, [h_hi - 3, h_hi - 2][:stop_after])
                again = self.registry(cfg)
                view = self.build(again)
                # the rest of the run, and the hour after it once more: nothing else twice
                self.assertEqual(again.hours.stats["hours_reduced"], 3 - stop_after)
                self.same(want, view)
                self.assertEqual(self.chains(), [h_hi - 1])

    def test_a_gap_fill_stopped_past_max_gap_h_is_resumed_from_its_last_hour(self):
        # (c): a run carrying on from the chain, stopped after more saved hours than MAX_GAP_H
        want = self.payload(self.build(self.registry(self.cfg(merge=False))))
        cfg = self.cfg()
        h_hi = self.registry(cfg).hours.closed_end(NOW, [self.root])
        with mock.patch.object(hours, "MAX_GAP_H", 0):
            self.registry(cfg).hours.ensure([self.root], h_hi - 3, h_hi - 2)
            patch, saved = self.stop_after(1)
            with patch:
                self.build(self.registry(cfg))
            self.assertEqual(saved, [h_hi - 2])
            again = self.registry(cfg)
            view = self.build(again)
            self.assertEqual(again.hours.stats["hours_reduced"], 1)
            self.same(want, view)

    def test_a_build_stopped_after_every_new_hour_still_reaches_the_windows_end(self):
        # Progress: each restart saves one more hour, however often the service is stopped
        want = self.payload(self.build(self.registry(self.cfg(merge=False))))
        cfg = self.cfg()
        h_hi = self.registry(cfg).hours.closed_end(NOW, [self.root])
        patch, saved = self.stop_after(1)
        with patch:
            for _ in range(6):
                reg = self.registry(cfg)
                if reg.hours.missing([self.root], h_hi - 3, h_hi) == 0:
                    break
                self.build(reg)
        self.assertEqual(saved, [h_hi - 3, h_hi - 2, h_hi - 1])    # each hour once, in order
        again = self.registry(cfg)
        view = self.build(again)
        self.assertEqual(again.hours.stats["hours_reduced"], 0)
        self.same(want, view)


class ClosingTest(HourCase):

    def test_an_hour_closes_only_once_the_feed_is_past_its_grace(self):
        reg = self.registry(self.cfg(hour_grace_s=300))
        store = reg.hours
        h = int(NOW // HOUR)                # the open hour, 25 min in
        store.saw(h * HOUR + 200)
        self.assertEqual(store.closed_end(NOW, [self.root]), h - 1)
        store.saw(h * HOUR + 301)
        self.assertEqual(store.closed_end(NOW, [self.root]), h)
        # a host whose clock runs ahead closes nothing early
        store.saw(NOW + 7200)
        self.assertEqual(store.closed_end(NOW, [self.root]), h)

    def test_the_grace_is_never_less_than_the_skew_the_live_tier_allows(self):
        c = self.cfg(hour_grace_s=10)
        c.tree["live"]["clock_skew_s"] = 200
        self.assertEqual(self.registry(c).hours.grace, 200 + hours.SCAN_SLACK_S)


class DayStateTest(HourCase):

    def test_a_week_merges_whole_days_as_one_state_each_and_says_the_same(self):
        recs = []
        t = NOW - 50 * HOUR
        k = 0
        while t < NOW - 60:
            recs.append(TM.plain("exit", t, 100000 + k, 1, "git", "git status",
                                 user=("bob", "carol")[k % 2], **TM.cost(TM.random.Random(k))))
            k += 1
            t += 97
        self.write(recs)
        on = self.registry(self.cfg(day_cache=True))
        got = self.payload(self.build(on, 2880))
        self.assertGreaterEqual(on.hours.stats["days_built"], 1)
        off = self.registry(self.cfg(day_cache=False))
        want = self.payload(self.build(off, 2880))
        self.assertEqual(TM.diff(want, got), [])
        scan = self.payload(self.build(self.registry(self.cfg(merge=False)), 2880))
        self.assertEqual(TM.diff(scan, got), [])


class PruneTest(HourCase):

    def test_hours_past_retention_and_other_generations_are_pruned(self):
        self.write(gen(NOW - 3 * HOUR, NOW - 30))
        reg = self.registry(self.cfg())
        self.build(reg, 120)
        store = reg.hours
        d = store.dir
        old = int(NOW // HOUR) - store.retention_h - 30
        store._write(store._path("h", old), {"kind": "hour"}, b"")
        for fp in ("aaaaaaaaaaaaaaaa", "bbbbbbbbbbbbbbbb"):
            p = os.path.join(d, "h1.%s.hr" % fp)
            with open(p, "wb") as fh:
                fh.write(b"x")
            os.utime(p, (NOW - 100 if fp[0] == "a" else NOW - 10,) * 2)
        store.prune(NOW)
        names = os.listdir(d)
        self.assertNotIn(os.path.basename(store._path("h", old)), names)
        # the newest other generation is kept: another process may share the dir
        self.assertIn("h1.bbbbbbbbbbbbbbbb.hr", names)
        self.assertNotIn("h1.aaaaaaaaaaaaaaaa.hr", names)


class StatusTest(HourCase):

    def test_the_registry_reports_the_store(self):
        self.write(gen(NOW - 3 * HOUR, NOW - 30))
        reg = self.registry(self.cfg())
        self.build(reg, 120)
        st = reg.status()["hours"]
        self.assertGreater(st["hours"], 0)
        json.dumps(st)
        self.assertIsNone(self.registry(self.cfg(merge=False)).status()["hours"])


if __name__ == "__main__":
    unittest.main()


class EdgeContextTest(HourCase):

    def test_records_before_the_window_only_inform_its_joins(self):
        # The window's first hour is reduced whole, so what came before the
        # window's start in that hour seeds the joins of the hours after it.
        # Every count of the window's own records is still exact.
        self.write(gen(NOW - 5 * HOUR + 777, NOW - 30))
        want = self.payload(self.build(self.registry(self.cfg(merge=False))))
        got = self.payload(self.build(self.registry(self.cfg())))
        for key in ("facets", "io_process", "risk", "resources", "slurm"):
            with self.subTest(reducer=key):
                self.assertEqual(TM.diff(want[key], got[key]), [])
        self.assertEqual(want["sandbox"]["n_events"], got["sandbox"]["n_events"])
        self.assertEqual(want["tool_mix"]["dedup"]["rule"], got["tool_mix"]["dedup"]["rule"])
