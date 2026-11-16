"""Saved panels: a view the registry creates shows the last ones its window served.

The scratch-feed fixtures of test_window_cache (scanned windows) and test_hours
(merged ones). Builds run on the calling thread (`WindowRegistry._build`), so
each test sees exactly one build and nothing races it, and a view asked for but
not built is what a page sees while the worker is still busy.

    cd dashboard && .venv/bin/python -m unittest tests.test_payload_cache -q
"""
import os
import pickle
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from rc_dashboard import payloads                  # noqa: E402
from rc_dashboard.aggregator import Aggregator     # noqa: E402
from rc_dashboard.normalize import Normalizer      # noqa: E402
from rc_dashboard.windows import ViewFilter        # noqa: E402
from test_hours import NOW, SINCE, HourCase, gen   # noqa: E402
from test_hours import WIN as HOUR_WIN             # noqa: E402
from test_window_cache import WIN, CacheCase, varied_exit   # noqa: E402


class PanelsCase(CacheCase):

    def panels_file(self, minutes=WIN):
        d = os.path.join(self.tmp, "cache", "payloads")
        names = [n for n in os.listdir(d) if n.startswith("p%d." % minutes)]
        self.assertEqual(len(names), 1, names)
        return os.path.join(d, names[0])

    def restart(self, cfg=None, flt=None):
        """The service after a restart: a new registry over the same cache
        directory, its window asked for and not built yet."""
        reg = self.registry(cfg)
        return reg, reg.request(WIN, flt)

    def assertNotShown(self, view, why):
        self.assertEqual(view.payload, {})
        self.assertFalse(view.status()["has_payload"])
        self.assertIsNone(view.cache["restored"])
        self.assertIn(why, view.cache["panels_note"])


class SaveTest(PanelsCase):

    def test_a_build_saves_the_panels_it_served_and_their_span(self):
        self.feed(150, 90)
        reg = self.registry()
        view = self.build(reg)
        meta, payload, why = reg.payloads.load(WIN, [self.root])
        self.assertIsNone(why)
        self.assertEqual(payload, view.payload)
        for k in payloads.SPAN:
            self.assertEqual(meta[k], getattr(view, k), k)
        self.assertEqual(meta["materialized_at"], view.materialized_at)
        self.assertEqual(os.stat(self.panels_file()).st_mode & 0o777, 0o600)
        self.assertIsNotNone(view.cache["panels_saved_at"])

    def test_a_refresh_saves_the_panels_it_materialised(self):
        self.feed(150, 90)
        reg = self.registry()
        view = self.build(reg)
        first = reg.payloads.load(WIN, [self.root])[0]["materialized_at"]
        reg.feed(Normalizer(self.cfg()).normalize(varied_exit(self.now - 5, 3)))
        with mock.patch("time.time", return_value=time.time() + 10):
            reg.refresh()
        meta, payload, _ = reg.payloads.load(WIN, [self.root])
        self.assertGreater(meta["materialized_at"], first)
        self.assertEqual(meta["live_records"], 1)
        self.assertEqual(payload, view.payload)

    def test_an_older_copy_never_replaces_a_newer_one(self):
        # A build finishing on the worker and a refresh tick can each save the
        # same window; whichever writes last must not be the older picture.
        self.feed(150, 90)
        reg = self.registry()
        view = self.build(reg)
        meta, _ = view.saved_panels()
        stale = dict(meta, materialized_at=meta["materialized_at"] - 60)
        self.assertIsNone(reg.payloads.save(WIN, [self.root], stale, {"tool_mix": "older"}))
        self.assertEqual(reg.payloads.load(WIN, [self.root])[1], view.payload)

    def test_off_with_the_saved_builds(self):
        cfg = self.cfg()
        cfg.tree["panels"]["cache_windows"] = False
        self.feed(150, 90)
        reg = self.registry(cfg)
        self.build(reg)
        self.assertIsNone(reg.payloads)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "cache", "payloads")))


class RestoreTest(PanelsCase):

    def test_a_restarted_service_shows_the_saved_panels_until_its_build_lands(self):
        self.feed(150, 90)
        first = self.build(self.registry())
        reg, view = self.restart()
        st = view.status()
        self.assertEqual(st["state"], "queued")
        self.assertTrue(st["has_payload"])
        self.assertEqual(view.payload, first.payload)
        # The saved reduction's own span and age, as a drift re-build shows its
        # previous one -- never the moment it was shown again.
        before = first.status()
        for k in ("built_at", "covers_from", "covers_to", "drift_s", "live_records"):
            self.assertEqual(st[k], before[k], k)
        self.assertIn("panels saved at", st["cache"]["restored"])
        # Not installed: the page re-fetches when the build's reducers are.
        self.assertIsNone(st["installed_at"])

        reg._build(view)
        self.assertEqual(view.state, "ready")
        self.assertIsNone(view.cache["restored"])
        self.assertIsNotNone(view.installed_at)

    def test_the_build_replaces_the_saved_panels_with_its_own(self):
        self.feed(150, 90)
        with mock.patch("time.time", return_value=self.now):
            self.build(self.registry())
            self.feed(55, 50)              # written while the service was down
            reg, view = self.restart()
            shown = view.payload
            reg._build(view)
        self.assertIsNot(view.payload, shown)
        self.assertNotEqual(view.payload["tool_mix"], shown["tool_mix"])
        self.assertEqual(view.progress["records"], 50)

    def test_a_window_the_lru_dropped_comes_back_with_its_panels(self):
        self.feed(150, 90)
        reg = self.registry()
        reg.max_views = 1
        first = self.build(reg)
        self.build(reg, 60)                # drops the day's view
        self.assertNotIn(WIN, reg.views)
        back = reg.request(WIN)
        self.assertIsNot(back, first)
        self.assertEqual(back.state, "queued")
        self.assertEqual(back.payload, first.payload)

    def test_the_api_serves_the_saved_panels_while_the_window_is_read(self):
        self.feed(150, 90)
        cfg = self.cfg()
        agg = Aggregator(cfg)
        agg.panels_by_window._build(agg.panels_by_window.request(WIN))
        want = agg.panels(WIN)["panels"]["tool_mix"]

        # A restart a minute and a half later. Not at the same instant: the feed's
        # status, which every panel body carries, is read when the service starts
        # (its `_notice` says how old the feed is), and a test that compared it
        # across the two failed whenever a second ticked between them.
        with mock.patch("time.time", return_value=time.time() + 90):
            again = Aggregator(cfg)
            got = again.panels(WIN)
        self.assertEqual(got["window_status"]["state"], "queued")
        body = got["panels"]["tool_mix"]
        self.assertTrue(body["_window"]["has_payload"])
        # The panel's own numbers are the saved ones; what starts with `_` (the
        # feed's status, the window's, the filter) is this service's, read now.
        mine = {k: v for k, v in body.items() if not k.startswith("_")}
        self.assertTrue(mine)
        self.assertEqual(mine, {k: v for k, v in want.items() if not k.startswith("_")})


    def test_what_is_built_from_reducers_reads_as_a_first_build_meanwhile(self):
        # The Users tab's roster and portraits come from the view's reducers, which
        # a view showing saved panels does not have yet: their status must say
        # "first build" (the page shows "reading"), never "has panels" over an empty
        # roster ("no user ran an agent session").
        self.feed(150, 90)
        cfg = self.cfg()
        agg = Aggregator(cfg)
        agg.panels_by_window._build(agg.panels_by_window.request(WIN))
        self.assertTrue(agg.users(WIN)["_window"]["has_payload"])

        again = Aggregator(cfg)            # a restart
        self.assertTrue(again.panels(WIN)["window_status"]["has_payload"])
        users = again.users(WIN)
        self.assertEqual(users["users"], [])
        self.assertFalse(users["_window"]["has_payload"])
        one = again.user("alice", WIN)
        self.assertIsNone(one["found"])
        self.assertFalse(one["_window"]["has_payload"])

        reg = again.panels_by_window
        reg._build(reg.request(WIN))
        self.assertTrue(again.users(WIN)["_window"]["has_payload"])


class RefusalTest(PanelsCase):

    def test_panels_from_other_code_or_config_are_not_shown(self):
        self.feed(150, 90)
        self.build(self.registry())
        cfg = self.cfg()
        cfg.tree["reducers"]["io"]["top_n"] = 5
        self.assertNotShown(self.restart(cfg)[1], "different code, config")

    def test_panels_older_than_the_drift_budget_are_not_shown(self):
        self.feed(150, 90)
        reg = self.registry()
        view = self.build(reg)
        meta, payload = view.saved_panels()
        # 24 h at 25% is a 6 h budget.
        reg.payloads.save(WIN, [self.root], dict(meta, built_at=time.time() - 7 * 3600,
                                                 materialized_at=time.time() + 1), payload)
        self.assertNotShown(self.restart()[1], "drift budget")

    def test_a_file_anyone_else_can_write_is_never_loaded(self):
        self.feed(150, 90)
        self.build(self.registry())
        os.chmod(self.panels_file(), 0o666)
        self.assertNotShown(self.restart()[1], "not private")

    def test_an_unreadable_file_is_discarded(self):
        self.feed(150, 90)
        self.build(self.registry())
        p = self.panels_file()
        with open(p, "rb") as fh:
            meta = pickle.load(fh)
        with open(p, "wb") as fh:
            pickle.dump(meta, fh)
            fh.write(b"not zlib")
        self.assertNotShown(self.restart()[1], "unreadable")
        self.assertFalse(os.path.exists(p))

    def test_a_filtered_view_never_shows_the_unfiltered_panels(self):
        self.feed(150, 90)
        self.build(self.registry())
        self.assertNotShown(self.restart(flt=ViewFilter(["alice"]))[1], "no saved panels")


class MergedPanelsTest(HourCase):

    def test_a_merged_window_saves_its_panels_and_a_new_registry_shows_them(self):
        self.write(gen(SINCE + 1, NOW - 30))
        cfg = self.cfg()
        view = self.build(self.registry(cfg))
        self.assertEqual(view.cache["source"], "hours")
        again = self.registry(cfg).request(HOUR_WIN)
        self.assertEqual(again.state, "queued")
        self.assertEqual(again.payload, view.payload)
        self.assertIn("panels saved at", again.cache["restored"])


if __name__ == "__main__":
    unittest.main()
