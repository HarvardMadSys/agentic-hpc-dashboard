"""Page-wide user and agent-type filters: a filter is a view, keyed like a window.

Same scratch-feed fixture as test_window_cache. Builds run on the calling
thread, so each test sees exactly the builds it asks for.

    cd dashboard && .venv/bin/python -m unittest discover -s tests -q
"""
import os
import sys
import threading
import unittest
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from starlette.datastructures import QueryParams   # noqa: E402

from rc_dashboard import app as appmod            # noqa: E402
from rc_dashboard import snapshots                 # noqa: E402
from rc_dashboard.aggregator import Aggregator     # noqa: E402
from rc_dashboard.export import make_predicate, parse_filters   # noqa: E402
from rc_dashboard.normalize import Normalizer      # noqa: E402
from rc_dashboard.reducers.facets import TOP_USERS, FacetReducer   # noqa: E402
from rc_dashboard.windows import (NO_FILTER, PanelView, ViewFilter,   # noqa: E402
                                  WindowRegistry, view_key)
from test_ingest import DAY, line                  # noqa: E402
from test_window_cache import WIN, CacheCase, skipped_rec, varied_exit   # noqa: E402

# (user, agent type) per exit, cycling. `glen` runs Claude Code and a plain
# shell, `ana` Codex and VS Code, so a user and an agent type cut differently.
WHO = [("glen", "claude_code"), ("ana", "codex"), ("glen", None),
       ("ana", "vscode"), ("bo", "claude_code")]


def who_exit(epoch, i, host="h1"):
    r = varied_exit(epoch, i, host)
    u, at = WHO[i % len(WHO)]
    r.update(user=u, agent_type=at,
             actor3="ide" if at == "vscode" else "agent" if at else "human")
    return r


class Req:
    """What the route reads off a request: its query string, as Starlette parses it."""

    def __init__(self, qs):
        self.query_params = QueryParams(qs)


class FilterCase(CacheCase):

    def feed_who(self, start_min, n, host="h1", only=None, root=None):
        recs = []
        for i in range(n):
            t = self.now - 60 * (start_min - i)
            r = who_exit(t, i, host)
            if only is None or only(r):
                recs.append(r)
            recs.append(skipped_rec(t + 1, host))
        path = os.path.join(root or self.root, DAY, "%s.exits.jsonl" % host)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "ab") as fh:
            for r in recs:
                fh.write(line(r))

    def build(self, reg, minutes=WIN, flt=None):
        view = reg.request(minutes, flt)
        reg._build(view)
        self.assertEqual((view.state, view.error), ("ready", None))
        return view

    def calls(self, view):
        return sum(v["calls"] for v in view.payload["tool_mix"]["by_class"].values())


class ViewFilterTest(unittest.TestCase):

    def test_an_empty_filter_is_no_filter_and_keys_the_view_by_minutes_alone(self):
        self.assertFalse(NO_FILTER)
        self.assertFalse(ViewFilter([], ["", None]))
        self.assertEqual(view_key(1440), 1440)
        self.assertEqual(view_key(1440, ViewFilter()), 1440)
        self.assertEqual(view_key(1440, ViewFilter(["b", "a", "a"], ["codex"])),
                         (1440, (("a", "b"), ("codex",))))

    def test_every_spelling_of_untagged_is_one_value_and_matches_a_null_type(self):
        f = ViewFilter(agent_types=["untagged", "None", "none"])
        self.assertEqual(f.agent_types, ("none",))
        self.assertTrue(f.match({"agent_type": None}))
        self.assertTrue(f.match({"agent_type": ""}))
        self.assertTrue(f.match({}))
        self.assertFalse(f.match({"agent_type": "codex"}))

    def test_users_and_types_and_together_and_values_or_within(self):
        f = ViewFilter(["glen", "ana"], ["claude_code", "none"])
        self.assertTrue(f.match({"user": "glen", "agent_type": "claude_code"}))
        self.assertTrue(f.match({"user": "ana", "agent_type": None}))
        self.assertFalse(f.match({"user": "ana", "agent_type": "codex"}))
        self.assertFalse(f.match({"user": "bo", "agent_type": "claude_code"}))
        # A host-wide tick has neither, so no filtered view holds it.
        self.assertFalse(ViewFilter(["glen"]).match({"event": "residency_totals"}))

    def test_the_byte_check_never_misses_a_line_the_predicate_keeps(self):
        recs = [who_exit(1.7e9 + i, i) for i in range(25)]
        for f in (ViewFilter(["glen"]), ViewFilter(agent_types=["codex", "vscode"]),
                  ViewFilter(["ana"], ["vscode"]), ViewFilter(["glen"], ["none"])):
            self.assertIsNotNone(f.line_match)
            for r in recs:
                if f.match(r):
                    self.assertIsNotNone(f.line_match(line(r)), (f.key, r["user"]))
        # Nothing it can search for safely: no pre-check, the predicate decides.
        self.assertIsNone(ViewFilter(agent_types=["none"]).line_match)
        self.assertIsNone(ViewFilter(['we"ird']).line_match)

    def test_params_take_the_events_vocabulary_repeated_or_comma_separated(self):
        f = ViewFilter.from_params(appmod.query_lists(
            Req("user=glen&user=ana,bo&agent_type=codex&agent_type=untagged&window_min=60")))
        self.assertEqual(f.users, ("ana", "bo", "glen"))
        self.assertEqual(f.agent_types, ("codex", "none"))
        self.assertFalse(ViewFilter.from_params(appmod.query_lists(Req("window_min=60"))))
        too_many = "&".join("user=u%d" % i for i in range(65))
        with self.assertRaises(Exception) as cm:
            appmod.view_filter(Req(too_many))
        self.assertEqual(getattr(cm.exception, "status_code", None), 400)

    def test_repeated_event_filters_keep_every_value(self):
        # `class=agent&class=human` is what the page sends; the old dict()
        # kept only `human`.
        f = parse_filters(appmod.query_lists(Req("class=agent&class=human&agent_type=none")))
        self.assertEqual(f["class"], {"agent", "human"})
        ok = make_predicate(f)
        self.assertTrue(ok({"_a3": "human", "agent_type": None}))
        self.assertFalse(ok({"_a3": "human", "agent_type": "codex"}))


class FilteredBuildTest(FilterCase):

    def test_unfiltered_is_keyed_saved_and_counted_exactly_as_before(self):
        self.feed_who(120, 50)
        reg = self.registry()
        view = self.build(reg)
        self.assertEqual(list(reg.views), [WIN])
        self.assertIsNone(view.status()["filter"])
        self.assertEqual(self.calls(view), 50)
        names = os.listdir(os.path.join(self.tmp, "cache", "windows"))
        self.assertEqual([n.split(".")[0] for n in names], ["w%d" % WIN])

    def test_a_filtered_view_is_the_view_of_a_feed_holding_only_those_records(self):
        glen = ViewFilter(["glen"])
        self.feed_who(120, 50)
        other = os.path.join(self.tmp, "only_glen")
        self.feed_who(120, 50, only=glen.match, root=other)
        filtered = self.build(self.registry(), flt=glen)
        self.assertEqual(self.calls(filtered), 20)

        cfg = self.cfg()
        reg = WindowRegistry(cfg, list(cfg.get("filters.classes")) + ["unlabeled"],
                             lambda: [other], threading.RLock())
        alone = self.build(reg)
        self.assertEqual(filtered.payload, alone.payload)

    def test_an_agent_type_filter_counts_only_those_types(self):
        self.feed_who(120, 50)
        v = self.build(self.registry(), flt=ViewFilter(agent_types=["claude_code", "none"]))
        self.assertEqual(self.calls(v), 30)
        fac = v.payload["facets"]
        self.assertEqual(sorted(t["agent_type"] for t in fac["agent_types"]),
                         ["claude_code", "none"])

    def test_the_live_feed_applies_the_same_predicate(self):
        self.feed_who(120, 10)
        reg = self.registry()
        glen = self.build(reg, flt=ViewFilter(["glen"]))
        everyone = self.build(reg)
        norm = Normalizer(self.cfg())
        for i in range(5):
            reg.feed(norm.normalize(who_exit(self.now - 30 + i, i)))
        self.assertEqual(everyone.live_records, 5)
        self.assertEqual(glen.live_records, 2)        # WHO[0] and WHO[2]
        glen.materialize()
        self.assertEqual(self.calls(glen), 4 + 2)

    def test_filtered_and_unfiltered_builds_never_resume_each_other(self):
        glen = ViewFilter(["glen"])
        self.feed_who(150, 50)
        self.build(self.registry())
        self.build(self.registry(), flt=glen)
        d = os.path.join(self.tmp, "cache", "windows")
        stems = sorted(n.split(".")[0] for n in os.listdir(d))
        self.assertEqual(stems, ["w%d" % WIN, "w%d-%s" % (WIN, glen.slug())])

        self.feed_who(40, 10)
        again = self.build(self.registry(), flt=glen)
        self.assertEqual(again.cache["source"], "snapshot", again.cache["note"])
        self.assertEqual(again.progress["records"], 4)

        # With its own copy gone it scans -- it never picks up the unfiltered one.
        for n in os.listdir(d):
            if n.startswith("w%d-" % WIN):
                os.unlink(os.path.join(d, n))
        fresh = self.build(self.registry(), flt=glen)
        self.assertEqual(fresh.cache["source"], "scan")
        self.assertIn("no saved build", fresh.cache["note"])
        self.assertEqual(fresh.payload, again.payload)

    def test_a_saved_copy_for_another_filter_is_refused_by_its_header(self):
        self.feed_who(150, 20)
        self.build(self.registry(), flt=ViewFilter(["glen"]))
        store = snapshots.SnapshotStore.from_config(self.cfg())
        meta, _state, why = store.load(WIN, [self.root], lambda m: None,
                                       flt=ViewFilter(["glen"]))
        self.assertIsNotNone(meta, why)
        self.assertEqual(meta["filter"], {"users": ["glen"], "agent_types": []})
        # The same file under another filter's name must not load.
        src = store.path(WIN, [self.root], ViewFilter(["glen"]))[0]
        dst = store.path(WIN, [self.root], ViewFilter(["ana"]))[0]
        os.rename(src, dst)
        meta, _state, why = store.load(WIN, [self.root], lambda m: None,
                                       flt=ViewFilter(["ana"]))
        self.assertIsNone(meta)
        self.assertIn("different user or agent-type filter", why)


class EvictionTest(FilterCase):

    def ready(self, reg):
        for v in reg.views.values():
            v.state = "ready"

    def test_a_filtered_view_is_evicted_before_any_unfiltered_one(self):
        reg = self.registry()
        reg.max_views = 3
        reg.request(WIN)
        reg.request(60)
        reg.request(WIN, ViewFilter(["glen"]))
        reg.request(60)                       # 60 is now the most recent
        self.ready(reg)
        reg.request(360)
        self.assertEqual(sorted(k for k in reg.views if isinstance(k, int)),
                         [60, 360, WIN])
        self.assertEqual(len(reg.views), 3)

    def test_a_filter_never_evicts_the_default_window(self):
        reg = self.registry()
        reg.max_views = 2
        default = self.cfg().get("live.window_min")
        reg.request(default)
        reg.request(60)                       # the default is now least recent
        self.ready(reg)
        reg.request(default, ViewFilter(["glen"]))
        self.assertIn(default, reg.views)
        self.assertNotIn(60, reg.views)

    def test_a_queued_filtered_view_gives_way_to_the_next_one(self):
        reg = self.registry()
        reg.max_views = 2
        reg.request(WIN)
        self.ready(reg)
        a = ViewFilter(["glen"])
        b = ViewFilter(["glen", "ana"])
        reg.request(WIN, a)                   # queued, nothing read yet
        reg.request(WIN, b)
        self.assertEqual(list(reg.views), [WIN, view_key(WIN, b)])
        self.assertEqual(list(reg._queue), [WIN, view_key(WIN, b)])
        st = reg.status()
        self.assertEqual(st["queued"], [WIN])
        self.assertEqual(st["queued_filtered"],
                         [{"minutes": WIN, "users": ["ana", "glen"], "agent_types": []}])

    def test_unfiltered_snapshots_outlive_filtered_ones_when_pruning(self):
        self.feed_who(150, 20)
        cfg = self.cfg()
        reg = self.registry(cfg)
        reg.store.keep = 2
        self.build(reg, 60)
        self.build(reg, WIN)
        self.build(reg, WIN, ViewFilter(["glen"]))     # newest, and pruned first
        d = os.path.join(self.tmp, "cache", "windows")
        self.assertEqual(sorted(n.split(".")[0] for n in os.listdir(d)),
                         ["w1440", "w60"])


class FacetsTest(FilterCase):

    def test_facets_count_exits_by_user_with_their_class_mix(self):
        r = FacetReducer(None, ["agent", "human", "ide", "unlabeled"])
        norm = Normalizer(self.cfg())
        for i in range(10):
            r.feed(norm.normalize(who_exit(self.now - 100 + i, i)))
        out = r.result()
        glen = next(u for u in out["users"] if u["user"] == "glen")
        self.assertEqual(glen, {"user": "glen", "events": 4,
                                "by_class": {"agent": 2, "human": 2}})
        self.assertEqual(out["users"][0]["user"], "ana")      # 4 each: by name
        types = {t["agent_type"]: t for t in out["agent_types"]}
        self.assertEqual(types["vscode"]["by_class"], {"ide": 2})
        self.assertEqual(types["none"]["events"], 2)

    def test_users_past_the_top_are_counted_not_listed(self):
        r = FacetReducer(None, [])
        for i in range(TOP_USERS + 7):
            r.feed({"user": "u%03d" % i, "_a3": "human", "agent_type": None})
        out = r.result()
        self.assertEqual((out["users_shown"], out["users_total"]), (TOP_USERS, TOP_USERS + 7))

    def test_panels_carry_the_unfiltered_facets_and_state_their_filter(self):
        self.feed_who(120, 20)
        agg = Aggregator(self.cfg())
        reg = agg.panels_by_window
        reg._build(reg.request(WIN))
        out = agg.panels(WIN)
        self.assertEqual(out["filter"]["active"], False)
        self.assertEqual(out["facets"]["users_total"], 3)
        self.assertNotIn("facets", out["panels"])

        glen = ViewFilter(["glen"])
        first = agg.panels(WIN, glen)                   # queued: building path
        self.assertEqual(first["window_status"]["state"], "queued")
        self.assertEqual(first["window_status"]["filter"], glen.as_dict())
        self.assertEqual(first["panels"]["tool_mix"]["_window"]["has_payload"], False)
        reg._build(reg.peek(WIN, glen))
        out = agg.panels(WIN, glen)
        self.assertEqual(out["filter"]["users"], ["glen"])
        self.assertTrue(out["filter"]["not_narrowed"])
        # Everyone's choices, not just glen's.
        self.assertEqual(out["facets"]["users_total"], 3)
        self.assertEqual(out["panels"]["tool_mix"]["_filter"], glen.as_dict())
        traj = agg.trajectories(WIN, glen)
        self.assertEqual(traj["filter"]["users"], ["glen"])
        self.assertEqual(traj["_window"]["filter"], glen.as_dict())


class RouteTest(FilterCase):

    def route(self, app, path):
        return next(r.endpoint for r in app.routes if getattr(r, "path", None) == path)

    def test_the_panels_route_builds_the_view_its_params_name(self):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)   # on_event
            app = appmod.create_app(self.cfg())
        out = self.route(app, "/api/panels")(
            request=Req("window_min=60&user=glen&agent_type=untagged"), window_min=60)
        self.assertEqual(out["filter"]["users"], ["glen"])
        self.assertEqual(out["filter"]["agent_types"], ["none"])
        self.assertEqual(out["window_status"]["filter"],
                         {"users": ["glen"], "agent_types": ["none"]})
        self.assertIn((60, (("glen",), ("none",))), app.state.agg.panels_by_window.views)
        out = self.route(app, "/api/trajectories")(
            request=Req("user=ana"), rank="events", window_min=60)
        self.assertEqual(out["filter"]["users"], ["ana"])
        out = self.route(app, "/api/panels")(request=Req(""), window_min=None)
        self.assertFalse(out["filter"]["active"])


if __name__ == "__main__":
    unittest.main()
