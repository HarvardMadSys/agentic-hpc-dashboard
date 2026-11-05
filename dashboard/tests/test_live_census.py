"""What the census says is running at a session's live edge.

A command still running leaves no exit record until it ends, so a session in the middle of a long
`srun` has nothing new to show for it. The collector's per-minute census of each agent tree does:
`LiveReducer` keeps the newest one, with the least it has seen of each command over the session's
ticks (the agent and its helpers are up all the while), and `running_now` calls what is up beyond
that running. A tree with no census of its own (an agent a VS Code server started), or a census
older than the live window, is not known, and is never read as idle. Nor is nothing beyond a
baseline that did not begin with the session: a call already up in the earliest tick held is in it.

    cd dashboard && uv run python -m unittest discover -s tests -p 'test_live_census.py' -v
"""
import json
import sys
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_live_detail import Cfg                                      # noqa: E402
from rc_dashboard import proctree                                     # noqa: E402
from rc_dashboard.aggregator import Aggregator                        # noqa: E402
from rc_dashboard.reducers.live import LiveReducer, running_now       # noqa: E402

CLASSES = ["agent", "human", "ide"]


BEGAN_AT_0 = object()


def T(at, byc, uuid="u1", age_s=BEGAN_AT_0):   # a residency tick for tree (h1, 100)
    # `age_s` is its root's age: by default the tick's time since the session began, at time 0
    return {"event": "residency", "host": "h1", "tree_root_pid": 100, "agent_pid": 100, "session_uuid": uuid,
            "_ts_epoch": at, "by_command": byc, "n_procs": sum(byc.values()), "rss_mb": 10.0, "_a3": "agent",
            "age_s": at if age_s is BEGAN_AT_0 else age_s}


class CensusTest(unittest.TestCase):
    def setUp(self):
        self.lv = LiveReducer(Cfg(), ["agent", "human", "ide"])
    def feed(self, *ticks):
        for t in ticks: self.lv._residency(t)
        return self.lv.trees[("h1", 100)]
    def test_what_is_up_beyond_the_baseline_is_running(self):
        tr = self.feed(T(0, {"claude": 1, "npm exec @model": 2}), T(60, {"claude": 1, "npm exec @model": 2, "python": 1, "srun": 1}))
        now = running_now(tr, fresh=0)
        self.assertEqual((now["state"], dict(now["cmds"]), now["cat"]), ("running", {"python": 1, "srun": 1}, "slurm"))
    def test_only_the_baseline_up_is_idle(self):
        tr = self.feed(T(0, {"claude": 1, "npm exec @model": 2}), T(60, {"claude": 1, "npm exec @model": 2}))
        self.assertEqual(running_now(tr, fresh=0)["state"], "idle")
    def test_a_stale_or_missing_census_is_unknown(self):
        tr = self.feed(T(0, {"claude": 1}))
        self.assertEqual(running_now(tr, fresh=100), {"state": "unknown"})
        self.assertEqual(running_now(None, fresh=0), {"state": "unknown"})
    def test_a_new_session_on_the_same_pid_starts_a_fresh_baseline(self):
        tr = self.feed(T(0, {"claude": 1}), T(60, {"claude": 1, "python": 1}, uuid="u2"))
        self.assertEqual(running_now(tr, fresh=0)["state"], "idle")

    # What the census says, and when it was taken
    def test_the_answer_carries_the_time_of_the_census_it_read(self):
        tr = self.feed(T(0, {"claude": 1}), T(60, {"claude": 1, "python": 1}))
        self.assertEqual(running_now(tr, fresh=0)["at"], 60)
        tr = self.feed(T(120, {"claude": 1}))
        self.assertEqual(running_now(tr, fresh=0), {"state": "idle", "at": 120})

    def test_a_census_taken_at_the_cutoff_is_fresh_and_one_before_it_is_not(self):
        tr = self.feed(T(60, {"claude": 1}))
        self.assertEqual(running_now(tr, fresh=60)["state"], "idle")
        self.assertEqual(running_now(tr, fresh=60.5), {"state": "unknown"})

    def test_a_tick_with_no_time_is_never_fresh(self):
        # null is not zero: a missing time is not time 0, which any cutoff of 0 would call fresh
        # (it has an age, so that the missing time is the only reason it is not known)
        tr = self.feed(T(None, {"claude": 1}, age_s=0))
        self.assertEqual(running_now(tr, fresh=0), {"state": "unknown"})

    # The baseline: the least of each command over the session's ticks
    def test_what_counts_is_the_excess_over_the_least_ever_seen(self):
        tr = self.feed(T(0, {"claude": 1, "node": 2}), T(60, {"claude": 1, "node": 1}),
                       T(120, {"claude": 1, "node": 4}))
        self.assertEqual(running_now(tr, fresh=0)["cmds"], [["node", 3]])

    def test_a_command_missing_from_a_tick_is_not_baseline_afterwards(self):
        # absent is a count of 0, and the least of 1, 0 and 1 is 0: a `node` that comes back is work
        tr = self.feed(T(0, {"claude": 1, "node": 1}), T(60, {"claude": 1}),
                       T(120, {"claude": 1, "node": 1}))
        now = running_now(tr, fresh=0)
        self.assertEqual((now["state"], now["cmds"]), ("running", [["node", 1]]))

    def test_an_older_tick_of_the_same_session_read_later_still_counts(self):
        # the backfill reads newest first: the newest tick has python and srun up, and the one
        # before it shows they were not up at the start of the session
        self.lv._residency(T(120, {"claude": 1, "python": 1, "srun": 1}))
        self.lv._residency(T(0, {"claude": 1}), older=True)
        now = running_now(self.lv.trees[("h1", 100)], fresh=0)
        self.assertEqual((now["state"], dict(now["cmds"]), now["at"]),
                         ("running", {"python": 1, "srun": 1}, 120))

    def test_an_older_tick_of_an_earlier_session_is_not_this_sessions_baseline(self):
        self.lv._residency(T(120, {"claude": 1, "python": 1}, uuid="u2"))
        self.lv._residency(T(0, {"claude": 1}, uuid="u1"), older=True)
        self.assertEqual(running_now(self.lv.trees[("h1", 100)], fresh=0)["state"], "idle")

    def test_a_tick_with_no_command_census_is_not_known_and_leaves_the_baseline_alone(self):
        bare = T(60, {})
        del bare["by_command"]
        tr = self.feed(T(0, {"claude": 1}), bare)
        self.assertEqual(running_now(tr, fresh=0), {"state": "unknown"})
        # `claude` is still baseline: the bare tick said nothing about any command
        tr = self.feed(T(120, {"claude": 1}))
        self.assertEqual(running_now(tr, fresh=0), {"state": "idle", "at": 120})

    def test_a_census_that_is_not_a_map_of_counts_is_not_read(self):
        # an empty map is no tree: the root is always up in one that sent a tick. A bad record
        # is not an idle one.
        for bad in (["claude", "node"], "claude", {}, {"claude": "1", "node": None}):
            lv = LiveReducer(Cfg(), CLASSES)
            tick = T(0, {})
            tick["by_command"] = bad
            lv._residency(tick)
            self.assertEqual(running_now(lv.trees[("h1", 100)], fresh=0), {"state": "unknown"}, bad)

    def test_a_count_that_is_not_a_whole_positive_number_is_not_one(self):
        odd = {"claude": 1, "a": "2", "b": True, "c": 0, "d": -1, "e": 1.5, "f": None}
        first = T(0, {"claude": 1})
        first["by_command"] = odd
        # in the tick that makes the baseline, one is no baseline: b and d are work when they are up
        tr = self.feed(first, T(60, {"claude": 1, "b": 1, "d": 1}))
        self.assertEqual(running_now(tr, fresh=0)["cmds"], [["b", 1], ["d", 1]])
        # and in the newest tick, one is not work
        newest = T(120, {"claude": 1})
        newest["by_command"] = odd
        self.assertEqual(running_now(self.feed(newest), fresh=0), {"state": "idle", "at": 120})

    # Idle needs a baseline that began with the session. One formed from a tick taken later may
    # hold a call that was already up, and then nothing beyond it is not known to be nothing.
    def test_a_baseline_formed_an_hour_into_the_session_cannot_say_idle(self):
        # the earliest tick held is of a root an hour old: a call up then is in the baseline
        tr = self.feed(T(0, {"claude": 1, "srun": 1}, age_s=3600),
                       T(60, {"claude": 1, "srun": 1}, age_s=3660))
        self.assertEqual(running_now(tr, fresh=0), {"state": "unknown"})

    def test_the_same_census_reads_idle_when_the_baseline_began_with_the_session(self):
        tr = self.feed(T(0, {"claude": 1, "srun": 1}, age_s=60),
                       T(60, {"claude": 1, "srun": 1}, age_s=120))
        self.assertEqual(running_now(tr, fresh=0), {"state": "idle", "at": 60})

    def test_the_start_is_a_root_of_at_most_150_seconds(self):
        for age, state in ((0, "idle"), (150, "idle"), (150.1, "unknown"), (151, "unknown")):
            lv = LiveReducer(Cfg(), CLASSES)
            lv._residency(T(0, {"claude": 1}, age_s=age))
            self.assertEqual(running_now(lv.trees[("h1", 100)], fresh=0)["state"], state, age)

    def test_a_baseline_from_ticks_with_no_age_cannot_say_idle(self):
        # the collector sends a null age where it cannot read the root's start; null is not 0, and
        # an age that is not a number is none
        absent = T(0, {"claude": 1})
        del absent["age_s"]
        for tick in (T(0, {"claude": 1}, age_s=None), absent, T(0, {"claude": 1}, age_s="0"),
                     T(0, {"claude": 1}, age_s=True)):
            lv = LiveReducer(Cfg(), CLASSES)
            lv._residency(tick)
            self.assertEqual(running_now(lv.trees[("h1", 100)], fresh=0), {"state": "unknown"},
                             tick.get("age_s", "no age_s"))

    def test_a_new_session_restarts_the_start_age_with_the_baseline(self):
        # u1's baseline began with it; the first tick held of u2 is of a root an hour old
        tr = self.feed(T(0, {"claude": 1}, age_s=0), T(60, {"claude": 1}, uuid="u2", age_s=3600))
        self.assertEqual(running_now(tr, fresh=0), {"state": "unknown"})
        # and a session whose first tick held is its own start (u3) reads idle, whatever came before
        tr = self.feed(T(120, {"claude": 1}, uuid="u3", age_s=0))
        self.assertEqual(running_now(tr, fresh=0), {"state": "idle", "at": 120})

    def test_a_later_tick_does_not_age_the_baseline(self):
        tr = self.feed(T(0, {"claude": 1}, age_s=0), T(3600, {"claude": 1}, age_s=3600))
        self.assertEqual(running_now(tr, fresh=0), {"state": "idle", "at": 3600})

    def test_an_older_tick_read_later_can_give_the_baseline_its_start(self):
        # the backfill reads newest first: until it reaches the start, idle is not known
        def now():
            return running_now(self.lv.trees[("h1", 100)], fresh=0)
        self.lv._residency(T(3600, {"claude": 1}, age_s=3600))
        self.assertEqual(now(), {"state": "unknown"})
        self.lv._residency(T(1800, {"claude": 1}, age_s=1800), older=True)
        self.assertEqual(now(), {"state": "unknown"})
        self.lv._residency(T(0, {"claude": 1}, age_s=0), older=True)
        self.assertEqual(now(), {"state": "idle", "at": 3600})

    def test_an_older_tick_of_an_earlier_session_does_not_give_this_baseline_its_start(self):
        self.lv._residency(T(3600, {"claude": 1}, uuid="u2", age_s=3600))
        self.lv._residency(T(0, {"claude": 1}, uuid="u1", age_s=0), older=True)
        self.assertEqual(running_now(self.lv.trees[("h1", 100)], fresh=0), {"state": "unknown"})

    def test_a_tick_with_no_census_does_not_give_the_baseline_its_start(self):
        # nothing was seen of the commands at age 0, so the baseline began with the tick at an hour
        bare = T(0, {}, age_s=0)
        del bare["by_command"]
        tr = self.feed(bare, T(3600, {"claude": 1}, age_s=3600))
        self.assertEqual(running_now(tr, fresh=0), {"state": "unknown"})

    def test_a_call_beyond_a_late_baseline_is_still_running(self):
        # running is positive evidence, and does not depend on how the baseline began
        tr = self.feed(T(0, {"claude": 1}, age_s=3600), T(60, {"claude": 1, "srun": 1}, age_s=3660))
        now = running_now(tr, fresh=0)
        self.assertEqual((now["state"], now["cmds"], now["cat"]), ("running", [["srun", 1]], "slurm"))

    # What is listed: largest first, five at most, named by its category, masked
    def test_the_five_largest_are_listed_largest_first_and_a_tie_goes_by_name(self):
        up = {"w1": 1, "w2": 2, "w3": 3, "w4": 4, "w5": 5, "w6": 1, "w0": 1}
        tr = self.feed(T(0, {"claude": 1}), T(60, dict(up, claude=1)))
        self.assertEqual(running_now(tr, fresh=0)["cmds"],
                         [["w5", 5], ["w4", 4], ["w3", 3], ["w2", 2], ["w0", 1]])

    def test_the_category_is_the_first_in_rule_order_of_what_is_listed(self):
        for names, cat in ((("python", "sleep", "ls"), "run"), (("python", "pytest"), "test"),
                           (("srun", "python"), "slurm"), (("sleep", "ls"), "wait"),
                           (("ls", "bash"), "explore"), (("bash",), "unknown")):
            lv = LiveReducer(Cfg(), CLASSES)
            lv._residency(T(0, {"claude": 1}))
            lv._residency(T(60, {"claude": 1, **dict.fromkeys(names, 1)}))
            now = running_now(lv.trees[("h1", 100)], fresh=0)
            self.assertEqual((now["state"], now["cat"]), ("running", cat), names)

    def test_a_name_that_carries_a_credential_is_masked_before_it_is_shown(self):
        secret = "ghp_" + "Z" * 36
        tr = self.feed(T(0, {"claude": 1}), T(60, {"claude": 1, "tool --token " + secret: 1}))
        shown = running_now(tr, fresh=0)["cmds"]
        self.assertEqual(shown, [["tool --token [REDACTED]", 1]])
        self.assertNotIn("Z" * 20, json.dumps(running_now(tr, fresh=0)))

    def test_a_long_name_is_cut_after_it_is_masked(self):
        tr = self.feed(T(0, {"claude": 1}), T(60, {"claude": 1, "code-" + "e" * 80: 1}))
        (name, n), = running_now(tr, fresh=0)["cmds"]
        self.assertEqual((len(name), n), (40, 1))

    def test_the_users_session_list_does_not_carry_the_census(self):
        now = time.time()
        self.lv.feed(dict(T(now - 5, {"claude": 1, "python": 1}, age_s=0), user="ann"))
        s = self.lv.user_detail("ann", now=now, window_s=3600)["sessions"][0]
        self.assertEqual(s["n_procs"], 2)
        self.assertNotIn("by_command", s)
        self.assertNotIn("base", s)


class RunningRowsTest(unittest.TestCase):
    """`Aggregator._running_rows` puts the census on the row of a running agent."""
    NOW = 1_790_000_000.0

    def setUp(self):
        self.live = LiveReducer(Cfg(), CLASSES)

    def agent(self, pid, at, tree=None):
        # the live tier hears from an agent root: a record of its own
        self.live.feed({"event": "conn", "host": "h1", "user": "ann", "agent_type": "claude_code",
                        "pid": pid, "agent_pid": pid, "tree_root_pid": tree or pid,
                        "_role": "agent", "_a3": "agent", "_ts_epoch": at})

    def tick(self, root, at, byc, age_s=0):
        self.live.feed(dict(T(at, byc, age_s=age_s), tree_root_pid=root, agent_pid=root))

    def marked(self, rows):
        return Aggregator._running_rows(types.SimpleNamespace(live=self.live), rows, now=self.NOW)

    def test_the_row_of_an_agent_that_roots_its_tree_says_what_its_census_shows(self):
        self.agent(100, self.NOW - 10)
        self.tick(100, self.NOW - 70, {"claude": 1}, age_s=0)
        self.tick(100, self.NOW - 10, {"claude": 1, "srun": 1}, age_s=60)
        rows = {"d": {"host": "h1", "agent_pid": "100", "last": self.NOW - 30}}
        run = self.marked(rows)["d"]["running"]
        self.assertEqual(run["now"], {"state": "running", "cmds": [["srun", 1]], "cat": "slurm",
                                      "at": self.NOW - 10})
        self.assertEqual((run["n_procs"], run["census_at"]), (2, self.NOW - 10), "beside what it had")
        self.assertNotIn("running", rows["d"], "the cached row is untouched")

    def test_an_agent_with_only_its_baseline_up_reads_idle_when_that_began_with_the_session(self):
        self.agent(100, self.NOW - 10)
        self.tick(100, self.NOW - 70, {"claude": 1}, age_s=0)
        self.tick(100, self.NOW - 10, {"claude": 1}, age_s=60)
        rows = {"d": {"host": "h1", "agent_pid": "100", "last": self.NOW - 30}}
        self.assertEqual(self.marked(rows)["d"]["running"]["now"],
                         {"state": "idle", "at": self.NOW - 10})

    def test_the_same_census_is_not_known_when_the_baseline_began_mid_session(self):
        self.agent(100, self.NOW - 10)
        self.tick(100, self.NOW - 70, {"claude": 1}, age_s=3600)
        self.tick(100, self.NOW - 10, {"claude": 1}, age_s=3660)
        rows = {"d": {"host": "h1", "agent_pid": "100", "last": self.NOW - 30}}
        self.assertEqual(self.marked(rows)["d"]["running"]["now"], {"state": "unknown"})

    def test_an_agent_inside_another_programs_tree_has_no_census_of_its_own(self):
        # a VS Code server roots tree 100; its census is the server's, not this agent's
        self.agent(200, self.NOW - 10, tree=100)
        self.tick(100, self.NOW - 10, {"node": 3, "claude": 1, "python": 1})
        rows = {"d": {"host": "h1", "agent_pid": "200", "last": self.NOW - 30}}
        run = self.marked(rows)["d"]["running"]
        self.assertEqual(run["now"], {"state": "unknown"})
        self.assertIsNone(run["n_procs"])

    def test_a_census_older_than_the_live_window_is_not_known_yet(self):
        self.agent(100, self.NOW - 10)                        # the agent itself still speaks
        self.tick(100, self.NOW - LiveReducer.LIVE_S - 60, {"claude": 1})
        rows = {"d": {"host": "h1", "agent_pid": "100", "last": self.NOW - 30}}
        run = self.marked(rows)["d"]["running"]
        self.assertEqual(run["now"], {"state": "unknown"})
        self.assertIsNone(run["census_at"])

    def test_an_agent_that_is_not_running_gets_no_block_at_all(self):
        self.tick(100, self.NOW - 10, {"claude": 1})          # a census, but no live agent root
        rows = {"d": {"host": "h1", "agent_pid": "100", "last": self.NOW - 30}}
        self.assertNotIn("running", self.marked(rows)["d"])


class ServeTest(unittest.TestCase):
    """`proctree.serve` marks the inspector's row as the panel marks the ranked one."""
    NEW = "h1|2026-10-29|agent|apid:100"
    OLD = "h1|2026-10-28|agent|apid:100"

    def setUp(self):
        now = time.time()
        self.live = LiveReducer(Cfg(), CLASSES)
        self.live.feed({"event": "conn", "host": "h1", "user": "ann", "agent_type": "claude_code",
                        "pid": 100, "agent_pid": 100, "tree_root_pid": 100, "_role": "agent",
                        "_a3": "agent", "_ts_epoch": now - 10})
        self.live.feed(T(now - 70, {"claude": 1}, age_s=0))
        self.live.feed(T(now - 10, {"claude": 1, "python": 1}, age_s=60))
        # the panel's ranked rows: the agent's row for each day it ran, as `last` orders them
        self.ranked = {sid: {"sid": sid, "host": "h1", "agent_pid": "100", "last": last}
                       for sid, last in ((self.NEW, now - 30), (self.OLD, now - 90000))}
        agg = types.SimpleNamespace(
            cfg=Cfg(), lock=threading.RLock(), live=self.live, stopping=True, roots=lambda: [],
            window=lambda minutes: {"minutes": 1440, "label": "24 h"},
            reports={"ebpf": {"name": "ebpf", "status": "ok", "configured": [], "notice": None,
                              "env": None, "cli": None}})
        view = types.SimpleNamespace(payload={"trajectories": {"rows": self.ranked}})
        agg.panels_by_window = types.SimpleNamespace(request=lambda minutes, flt=None: view)
        agg._running_rows = types.MethodType(Aggregator._running_rows, agg)
        self.agg = agg

    def serve(self, sid, body=None):
        # what `inspect` returns: the cached body, copied one level down, its row shared
        self.cached = body if body is not None else {
            "sid": sid, "found": True, "row": dict(self.ranked[sid])}
        inspect = lambda *a, **k: dict(self.cached, cached=True)        # noqa: E731
        with mock.patch.object(proctree, "inspect", inspect), \
                mock.patch.dict(proctree._warm, {"thread": object()}):
            return proctree.serve(self.agg, sid)

    def test_a_running_agents_row_says_what_the_census_shows_and_the_cached_body_is_untouched(self):
        body = self.serve(self.NEW)
        now = body["row"]["running"]["now"]
        self.assertEqual((now["state"], now["cmds"], now["cat"]), ("running", [["python", 1]], "run"))
        self.assertNotIn("running", self.cached["row"])
        self.assertIsNot(body["row"], self.cached["row"])
        self.assertEqual(body["row"]["sid"], self.NEW)
        self.assertEqual(body["window"]["minutes"], 1440)

    def test_an_older_days_row_of_the_same_agent_is_not_marked(self):
        # the panel marks an agent's newest row only, and an inspector that says otherwise
        # would draw yesterday's lane running up to now
        body = self.serve(self.OLD)
        self.assertNotIn("running", body["row"])

    def test_a_row_is_not_marked_when_no_agent_of_it_is_running(self):
        self.live.roots.clear()
        self.assertNotIn("running", self.serve(self.NEW)["row"])

    def test_a_body_with_no_row_is_served_as_it_is(self):
        # the window is still being reduced, or the session is not in it
        body = self.serve(self.NEW, {"sid": self.NEW, "found": None})
        self.assertNotIn("row", body)
        self.assertIsNone(body["found"])


if __name__ == "__main__":
    unittest.main()
