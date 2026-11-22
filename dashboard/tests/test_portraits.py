"""The Users tab's portraits: one row per user, composed from the reducers a window
already holds (rc_dashboard/portraits.py).

Runs anywhere: the records are built here and fed through the same reducer set a
window build uses, as test_sessions does.

    cd dashboard && .venv/bin/python -m unittest tests.test_portraits -q
"""
import collections
import json
import os
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from rc_dashboard import portraits                       # noqa: E402
from rc_dashboard import windows as winmod               # noqa: E402
from rc_dashboard.aggregator import Aggregator           # noqa: E402
from rc_dashboard.normalize import Normalizer            # noqa: E402
from rc_dashboard.windows import PanelView               # noqa: E402
from test_sessions import cfg, rec                       # noqa: E402

PROBE = "/home/%s/.codex/packages/app-server-daemon/current/bin/codex --version"


def agent(user, root, t0, cmds, *, launch="claude", agent_type="claude_code", gap=20.0,
          host="h1"):
    """One agent session: its root's own tick, then one Bash tool call per command,
    `gap` seconds apart, each in a snapshot shell as Claude Code runs them. Every
    pid is counted up from `root`, so no two sessions built here share one."""
    home = "/home/%s" % user
    snap = "/bin/zsh -c source %s/.claude/shell-snapshots/snapshot-zsh-1.sh && eval " % home
    kw = {"user": user, "agent_type": agent_type, "host": host, "cwd": home + "/proj"}
    out = [rec("residency", t0, root, 50, launch.split()[0], launch,
               agent_pid=root, tree_root=root, is_agent=True, **kw)]
    for i, cmd in enumerate(cmds):
        sh, t = root + 1 + 2 * i, t0 + gap * (i + 1)
        out.append(rec("exit", t, sh + 1, sh, cmd.split()[0], cmd,
                       agent_pid=sh, tree_root=root, **kw))
        out.append(rec("exit", t + 0.5, sh, root, "zsh", snap + "'%s'" % cmd,
                       agent_pid=sh, tree_root=root, is_agent=True, **kw))
    if not cmds:
        # a session leaves an exit record or it is no session: one search of its own
        out.append(rec("exit", t0 + gap, root + 1, root, "rg", "rg --files " + home,
                       agent_pid=root, tree_root=root, **kw))
    return out


def build(recs, minutes=1440):
    """The window's reducers, fed as `WindowRegistry._build` feeds them."""
    c = cfg()
    classes = list(c.get("filters.classes")) + [c.get("filters.unlabeled_class")]
    view = PanelView(c, classes, minutes)
    reducers = view.fresh_reducers()
    dispatch = PanelView.build_dispatch(reducers)
    norm = Normalizer(c)
    for raw in sorted(recs, key=lambda r: r["ts_epoch"]):
        r = norm.normalize(raw)
        if r is None:
            continue
        for red in dispatch.get(r.get("event"), ()):
            red.feed(r)
    return reducers


def rows_of(ros):
    return {r["user"]: r for r in ros["users"]}


class RosterTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        t0 = time.time() - 6 * 3600
        recs = (agent("alice", 1000, t0, ["ls -la", "cat src/app.py", "pytest -q tests/test_a.py"])
                + agent("alice", 2000, t0 + 3600, ["git status", 'git commit -m "Fix the loader"'],
                        launch="claude --dangerously-skip-permissions")
                + agent("bob", 3000, t0 + 600, ["ls"], launch="claude -p summarise this")
                + agent("carol", 4000, t0 + 900, [], launch="codex", agent_type="codex")
                + agent("carol", 5000, t0 + 1200, ["rg -n foo src"]))
        # a status bar probing codex every minute: no agent at its root, so no session
        for i in range(3):
            recs.append(rec("exit", t0 + 60 * i, 9000 + i, 1, "codex", PROBE % "dan",
                            agent_pid=9000 + i, tree_root=9000 + i, is_agent=True,
                            agent_type="codex", user="dan"))
        cls.reducers = build(recs)
        cls.ros = portraits.roster(portraits.Context(cls.reducers))
        cls.rows = rows_of(cls.ros)

    def test_one_row_per_user_and_a_probe_is_no_session(self):
        self.assertEqual(set(self.rows), {"alice", "bob", "carol"})
        self.assertEqual(self.rows["alice"]["sessions"], 2)
        self.assertEqual(self.ros["fleet"]["users"], 3)
        self.assertEqual(self.ros["fleet"]["sessions"], 5)

    def test_launch_and_surface_are_each_sessions_own(self):
        a, b = self.rows["alice"], self.rows["bob"]
        self.assertEqual(a["approval"], {"supervised": 1, "bypassed": 1})
        self.assertEqual(a["surfaces"], {"terminal": 2})
        self.assertEqual(b["surfaces"], {"script": 1})
        self.assertEqual(self.rows["carol"]["agent_types"], {"codex": 1, "claude_code": 1})

    def test_calls_are_counted_by_category_with_the_agents_own_words(self):
        a = self.rows["alice"]
        self.assertEqual(a["calls"], 5)
        self.assertEqual(sum(a["cats"].values()), 5)
        self.assertEqual(a["cats"].get("test"), 1)
        self.assertEqual(a["words"], {"commit": 1})

    def test_traits_carry_the_numbers_that_met_their_rule(self):
        ids = {t["id"]: t for t in self.rows["alice"]["traits"]}
        self.assertEqual(ids["bypass"]["detail"], "1 of 2 sessions")
        self.assertEqual(ids["bypass"]["tone"], "crit")
        self.assertIn("committer", ids)
        self.assertIn("interactive", ids)
        self.assertEqual([t["id"] for t in self.rows["bob"]["traits"]], ["scripted"])
        self.assertIn("multi_agent", {t["id"] for t in self.rows["carol"]["traits"]})
        # every trait a row holds is in the catalogue, with its rule
        rules = {t["id"]: t["rule"] for t in self.ros["traits"]}
        for r in self.ros["users"]:
            for t in r["traits"]:
                self.assertTrue(rules[t["id"]])

    def test_each_row_says_how_its_user_leverages_agents(self):
        a = self.rows["alice"]["leverage"]
        self.assertEqual(a["mode"]["id"], "terminal")
        self.assertEqual(a["autonomy"], {"id": "bypass", "bypassed": 1, "auto": 0, "of": 2})
        self.assertEqual(a["uses"], [], "five calls are too few to say what for")
        self.assertEqual(self.rows["bob"]["leverage"]["mode"]["id"], "script")
        self.assertEqual(self.rows["alice"]["leverage"]["project"]["name"], "proj")

    def test_the_busiest_user_is_first(self):
        self.assertEqual(self.ros["users"][0]["user"], "alice")

    def test_a_session_of_any_agent_type_has_its_calls_counted(self):
        # Copilot's direct children are read by the general rule, as Codex's are
        t0 = time.time() - 3600
        red = build(agent("erin", 6000, t0, ["ls"], launch="copilot", agent_type="copilot"))
        row = rows_of(portraits.roster(portraits.Context(red)))["erin"]
        self.assertEqual(row["sessions"], 1)
        self.assertEqual(row["calls"], 1)
        self.assertEqual(row["calls_sessions"], 1)
        self.assertEqual(row["cats"], {"explore": 1})

    def test_nothing_before_the_view_is_built(self):
        ctx = portraits.Context({})
        self.assertFalse(ctx.ready())
        self.assertIsNone(portraits.roster(ctx))
        self.assertIsNone(portraits.portrait(ctx, "alice"))


class RhythmTest(unittest.TestCase):

    def test_sessions_open_at_once(self):
        t0 = time.time() - 4 * 3600
        cmds = ["ls"] * 4
        together = build(agent("ann", 1000, t0, cmds, gap=300)
                         + agent("ann", 2000, t0 + 60, cmds, gap=300)
                         + agent("ann", 3000, t0 + 120, cmds, gap=300))
        row = rows_of(portraits.roster(portraits.Context(together)))["ann"]
        self.assertEqual(row["peak_open"], 3)
        self.assertIn("parallel", {t["id"] for t in row["traits"]})
        apart = build(agent("ann", 1000, t0, ["ls"]) + agent("ann", 2000, t0 + 3600, ["ls"]))
        self.assertEqual(rows_of(portraits.roster(portraits.Context(apart)))["ann"]["peak_open"], 1)

    def test_active_minutes_by_local_hour(self):
        t0 = time.time() - 3 * 3600
        row = rows_of(portraits.roster(portraits.Context(
            build(agent("ann", 1000, t0, ["ls"] * 6, gap=30)))))["ann"]
        self.assertEqual(len(row["hours"]), 24)
        self.assertEqual(sum(row["hours"]), row["active_min"])
        self.assertGreaterEqual(row["active_min"], 3)

    def test_an_agent_across_midnight_is_one_session(self):
        lt = time.localtime()
        midnight = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
        if time.time() - midnight < 3600:
            midnight -= 86400            # the calls must be in the past
        red = build(agent("ann", 1000, midnight - 900, ["ls"] * 4, gap=600), minutes=2880)
        traj = red["trajectories"]
        days = {s.sid for s, _v, _k in traj.folded()[0] if s.user == "ann"}
        self.assertEqual(len(days), 2, "one row a day, as the Trajectories tab lists them")
        row = rows_of(portraits.roster(portraits.Context(red)))["ann"]
        self.assertEqual(row["sessions"], 1)
        self.assertEqual(row["calls"], 4)
        self.assertGreater(row["wall_s"], 1800)

    def test_running_now_is_the_live_tiers(self):
        t0 = time.time() - 600
        red = build(agent("ann", 1000, t0, ["ls"]) + agent("ann", 2000, t0, ["ls"]))
        ctx = portraits.Context(red, running=lambda host, pid, user: pid == 2000)
        row = rows_of(portraits.roster(ctx))["ann"]
        self.assertEqual(row["running"], 1)
        p = portraits.portrait(ctx, "ann")
        self.assertEqual({s["pid"]: s["running"] for s in p["sessions"]}, {1000: False, 2000: True})


class PidReuseTest(unittest.TestCase):
    """A pid is reused: the agent running now under one may be another user's."""

    def test_only_the_user_whose_agent_runs_under_the_pid_reads_running(self):
        lt = time.localtime()
        midnight = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
        if time.time() - midnight < 1800:
            midnight -= 86400            # both sessions in the past, a day apart
        # alice's agent had pid 1000 yesterday; bob's has it today, and runs now
        red = build(agent("alice", 1000, midnight - 7200, ["ls"])
                    + agent("bob", 1000, midnight + 600, ["ls"]), minutes=2880)
        agg = Aggregator.__new__(Aggregator)

        class Live:
            LIVE_S = 600
            roots = {("h1", 1000): {"epoch": time.time() - 30, "user": "bob"}}
        agg.live = Live
        rows = rows_of(portraits.roster(portraits.Context(red, running=agg._running_fn(time.time()))))
        self.assertEqual((rows["alice"]["running"], rows["bob"]["running"]), (0, 1))
        # a root whose user was not read is no other user's
        Live.roots[("h1", 1000)]["user"] = None
        rows = rows_of(portraits.roster(portraits.Context(red, running=agg._running_fn(time.time()))))
        self.assertEqual((rows["alice"]["running"], rows["bob"]["running"]), (1, 1))


class SlurmTest(unittest.TestCase):

    def test_the_users_agent_sessions_at_the_scheduler(self):
        t0 = time.time() - 2 * 3600
        # a job, then squeue every minute for 15 min: polling. Each is a Bash tool
        # call, so its keys fold into the agent through the snapshot shell.
        recs = agent("ann", 1000, t0, ["sbatch --parsable job.sh"] + ["squeue -u ann"] * 16, gap=60)
        recs.append(rec("submit", t0 + 60, 1002, 1001, "sbatch", "sbatch --parsable job.sh",
                        agent_pid=1001, tree_root=1000, user="ann", tool="sbatch", job_id=5001,
                        exit_code=0))
        row = rows_of(portraits.roster(portraits.Context(build(recs))))["ann"]
        sl = row["slurm"]
        self.assertEqual(sl["jobs"], 1)
        self.assertEqual(sl["queries"], 16)
        self.assertEqual(sl["queries_per_job"], 16.0)
        self.assertEqual(sl["polling_sessions"], 1)
        self.assertGreaterEqual(sl["polling_s"], 900)
        self.assertEqual(sl["parsable_pct"], 100.0)
        ids = {t["id"] for t in row["traits"]}
        self.assertIn("slurm", ids)
        self.assertIn("poller", ids)


class PortraitTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        t0 = time.time() - 5 * 3600
        cls.reducers = build(
            agent("alice", 1000, t0, ["ls -la", "cat src/app.py", "pytest -q tests/test_a.py"])
            + agent("alice", 2000, t0 + 3600, ["git status", 'git commit -m "Fix the loader"'])
            + agent("bob", 3000, t0, ["ls"]))
        cls.ctx = portraits.Context(cls.reducers)
        cls.ros = portraits.roster(cls.ctx)
        cls.p = portraits.portrait(cls.ctx, "alice", cls.ros["fleet"])

    def test_found_with_its_row_and_the_window_to_read_it_against(self):
        self.assertTrue(self.p["found"])
        self.assertEqual(self.p["row"]["sessions"], 2)
        self.assertEqual(self.p["fleet"]["users"], 2)
        self.assertEqual(len(self.p["row"]["grid"]), 7)
        self.assertEqual(sum(map(sum, self.p["row"]["grid"])), self.p["row"]["active_min"])

    def test_the_phases_and_tasks_are_the_trajectories_own(self):
        ph = self.p["phases"]
        self.assertEqual(ph["sessions"], 2)
        self.assertTrue(ph["time_s"])
        traj = self.reducers["trajectories"]
        served = traj.result()["rows"]
        mine = [r for r in served.values() if r["user"] == "alice"]
        want = sum(len([t for t in r["phases"]["tasks"] if t["kind"] == "task"]) for r in mine)
        self.assertEqual(self.p["tasks"]["n"], want)
        quoted = [t for t in self.p["tasks"]["recent"] if t["source"]]
        self.assertEqual([(t["label"], t["source"]) for t in quoted], [("Fix the loader", "commit")])

    def test_sessions_newest_first_and_open_by_their_trajectory_row(self):
        ss = self.p["sessions"]
        self.assertEqual([s["pid"] for s in ss], [2000, 1000])
        rows = self.reducers["trajectories"].result()["rows"]
        for s in ss:
            self.assertIn(s["sid"], rows)
            self.assertFalse(any(k.startswith("_") for k in s))

    def test_a_session_says_what_kind_it_was_as_its_row_does(self):
        rows = self.reducers["trajectories"].result()["rows"]
        for s in self.p["sessions"]:
            block = rows[s["sid"]]["phases"]
            self.assertEqual((s["category"], s["uses"], s["summary"]),
                             (block["category"], block["uses"], block["summary"]))
        # two reads before one quick test: time goes to what the agent started most recently
        self.assertEqual({s["pid"]: s["category"] for s in self.p["sessions"]},
                         {1000: "explore", 2000: "git"})
        self.assertIn("category", self.p["basis"])

    def test_the_commands_their_agents_ran_most(self):
        cmds = dict((c, n) for c, n in self.p["commands"])
        self.assertEqual(cmds.get("pytest -q tests/test_a.py"), 1)
        self.assertEqual(sum(cmds.values()), 5)

    def test_someone_with_no_agent_session_is_not_found(self):
        p = portraits.portrait(self.ctx, "nobody")
        self.assertFalse(p["found"])
        self.assertNotIn("row", p)


class HeldTest(unittest.TestCase):
    """What the aggregator keeps between reads holds no reducer, so it keeps no
    window's state alive once the registry drops or rebuilds the window."""

    def test_what_a_caller_may_keep_is_plain_data(self):
        t0 = time.time() - 3600
        red = build(agent("ann", 1000, t0, ["ls", "squeue -u ann"]) + agent("bob", 2000, t0, ["ls"]))
        got = portraits.inputs(red)
        self.assertEqual(set(got), {"boxed", "slurm"})
        json.dumps({repr(k): v for k, v in got["boxed"].items()})
        json.dumps(got["slurm"])
        ctx = portraits.Context(red, **got)
        ros = portraits.roster(ctx)
        json.dumps(ros)
        json.dumps(portraits.portrait(ctx, "ann", ros["fleet"]))
        # and the inputs, handed in, read as the context would have read them itself
        self.assertEqual(ros, portraits.roster(portraits.Context(red, now=ctx.now)))


class LeverageTest(unittest.TestCase):
    """The summary of how a user leverages their agents, part by part, on rows made by hand."""

    def row(self, **kw):
        r = {"sessions": 4, "surfaces": {}, "approval": {}, "cats": {}, "calls": 0,
             "peak_open": 1, "projects": [], "hours": [0] * 24, "active_min": 0}
        r.update(kw)
        return portraits.leverage(r)

    def test_the_way_most_sessions_are_driven(self):
        self.assertEqual(self.row(surfaces={"terminal": 3, "script": 1})["mode"],
                         {"id": "terminal", "n": 3, "of": 4})
        # the desktop app and the web are one way to a reader; an unknown launcher is not counted
        self.assertEqual(self.row(surfaces={"desktop": 1, "web": 1, "terminal": 1, "unknown": 1})["mode"],
                         {"id": "app", "n": 2, "of": 3})
        self.assertEqual(self.row(surfaces={"terminal": 1, "script": 1, "extension": 1,
                                            "desktop": 1})["mode"]["id"], "mixed")
        self.assertEqual(self.row(surfaces={"unknown": 4})["mode"]["id"], "unknown")

    def test_the_freedom_the_agents_start_with(self):
        a = self.row(approval={"bypassed": 2, "supervised": 2, "unobservable": 3})["autonomy"]
        self.assertEqual(a, {"id": "bypass", "bypassed": 2, "auto": 0, "of": 4})
        self.assertEqual(self.row(approval={"auto": 3, "supervised": 1})["autonomy"]["id"], "auto")
        self.assertEqual(self.row(approval={"bypassed": 1, "supervised": 3})["autonomy"]["id"], "no_flag")
        self.assertIsNone(self.row(approval={"unknown": 4})["autonomy"])

    def test_what_the_actions_are_for_with_waiting_said_apart(self):
        got = self.row(calls=100, cats={"explore": 30, "edit": 10, "test": 20, "wait": 40})
        self.assertEqual(got["actions"], 60)
        # a tie keeps the order of USES
        self.assertEqual([(u["id"], u["n"], u["pct"]) for u in got["uses"]],
                         [("explore", 30, 50.0), ("develop", 30, 50.0)])
        self.assertEqual(got["mostly"], 1)
        self.assertEqual(got["waiting"], {"n": 40, "of": 100, "pct": 40.0})

    def test_mostly_one_two_or_a_mix(self):
        self.assertEqual(self.row(calls=90, cats={"explore": 30, "run": 30, "git": 30})["mostly"], 2)
        mix = self.row(calls=100, cats={"explore": 20, "run": 20, "git": 20, "build": 20, "slurm": 20})
        self.assertEqual(mix["mostly"], 0)
        self.assertEqual(len(mix["uses"]), 5)

    def test_too_few_actions_or_none_read_say_no_use(self):
        self.assertEqual(self.row(calls=10, cats={"explore": 10})["uses"], [])
        unread = self.row(calls=None)
        self.assertEqual((unread["uses"], unread["mostly"], unread["waiting"]), ([], 0, None))

    def test_what_they_work_on_and_when(self):
        hours = [0] * 24
        hours[10], hours[3] = 50, 20
        got = self.row(projects=[["proj", 3], ["dash", 1]], hours=hours, active_min=70, peak_open=3)
        self.assertEqual(got["project"], {"name": "proj", "n": 3, "of": 4, "mostly": True})
        self.assertEqual(got["when"], {"busiest": 10, "minutes": 50, "night_pct": 28.6})
        self.assertEqual(got["parallel"], 3)
        few = self.row(projects=[["proj", 1], ["dash", 1]], hours=hours, active_min=30)
        self.assertFalse(few["project"]["mostly"])
        self.assertIsNone(few["when"]["night_pct"])          # under night_min active minutes
        self.assertIsNone(few["parallel"])
        self.assertIsNone(self.row()["when"])


class _QuietView:
    """The parts of a PanelView the aggregator's `_held` reads, for a view that
    ingests nothing: a quiet feed, or a filter whose users went quiet."""
    minutes, flt = 1440, winmod.NO_FILTER
    reducers = {"trajectories": object()}
    installed_at, live_records = 1.0, 0


class HeldAgeTest(unittest.TestCase):
    """A roster or a portrait holds what time alone changes (running now, the
    window's edge), so it is built again after a while even when its view has
    ingested nothing; the sandbox and Slurm inputs change with records only."""

    def test_a_roster_is_built_again_on_a_quiet_feed_and_its_inputs_are_not(self):
        agg = Aggregator.__new__(Aggregator)
        agg.cfg, agg._portraits = cfg(), collections.OrderedDict()
        built = collections.Counter()
        clock = [1000.0]

        def read(what, max_age=None):
            return agg._held(_QuietView, what, lambda: built.update([what]) or {"at": clock[0]},
                             max_age)

        cap = Aggregator.PORTRAITS_MAX_AGE_S
        with mock.patch.object(time, "monotonic", lambda: clock[0]):
            read("roster", cap)
            read("inputs")
            clock[0] += cap / 2
            read("roster", cap)
            self.assertEqual(built["roster"], 1, "young: held")
            clock[0] += cap
            self.assertEqual(read("roster", cap), {"at": clock[0]}, "old: built again")
            clock[0] += 86400
            read("inputs")
        self.assertEqual(built, {"roster": 2, "inputs": 1})


class TraitRulesTest(unittest.TestCase):
    """The rules alone, on rows made by hand."""

    def row(self, **kw):
        r = {"sessions": 4, "surfaces": {}, "approval": {}, "sandbox": {}, "agent_types": {},
             "peak_open": 1, "wall_p50_s": 600.0, "cats": {}, "words": {}, "slurm": None,
             "active_min": 0, "hours": [0] * 24}
        r.update(kw)
        return {t["id"]: t["detail"] for t in portraits.traits(r)}

    def test_a_share_is_over_the_known_values_only(self):
        # two bypassed of two whose launch is known; the app server's says nothing
        got = self.row(approval={"bypassed": 2, "unobservable": 2})
        self.assertEqual(got["bypass"], "2 of 2 sessions")
        self.assertNotIn("bypass", self.row(approval={"bypassed": 1, "supervised": 2}))
        self.assertIn("bypass", self.row(approval={"bypassed": 2, "supervised": 2}))

    def test_a_share_of_calls_needs_enough_calls(self):
        few = self.row(cats={"test": 10})
        self.assertNotIn("tester", few)
        many = self.row(cats={"test": 10, "explore": 30})
        self.assertEqual(many["tester"], "25% of 40 calls")

    def test_the_numbers_read_as_a_page_writes_them(self):
        self.assertEqual(self.row(cats={"test": 300, "explore": 900})["tester"], "25% of 1,200 calls")
        self.assertEqual(self.row(peak_open=4)["parallel"], "up to 4")
        self.assertEqual(self.row(slurm={"jobs": 1})["slurm"], "1 job submitted")
        self.assertEqual(self.row(slurm={"jobs": 1200})["slurm"], "1,200 jobs submitted")

    def test_long_and_short(self):
        self.assertEqual(self.row(wall_p50_s=3 * 3600.0)["long"], "median 3.0 h")
        self.assertIn("short", self.row(sessions=12, wall_p50_s=300.0))
        self.assertNotIn("short", self.row(sessions=9, wall_p50_s=300.0))

    def test_overnight_needs_enough_active_minutes(self):
        hours = [0] * 24
        hours[2], hours[14] = 30, 60
        self.assertEqual(self.row(active_min=90, hours=hours)["overnight"],
                         "33% of 90 active minutes")
        hours[2] = 10
        self.assertNotIn("overnight", self.row(active_min=50, hours=hours))

    def test_the_catalogue_names_every_rule(self):
        ids = [t["id"] for t in portraits.catalogue()]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(all(t["rule"] and t["label"] for t in portraits.catalogue()))


class _Zone:
    """Run under the time zone `tz`, and put the process's own back after."""

    def __init__(self, tz):
        self.tz = tz

    def __enter__(self):
        self.was = os.environ.get("TZ")
        os.environ["TZ"] = self.tz
        time.tzset()

    def __exit__(self, *exc):
        if self.was is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = self.was
        time.tzset()


class LocalHourTest(unittest.TestCase):
    """Active minutes go to their local hour, minute by minute, where the offset is
    not whole hours too: a UTC hour there holds two local hours."""

    ZONES = ("America/New_York", "Asia/Kolkata", "Asia/Kathmandu", "America/St_Johns",
             "Australia/Adelaide")

    def test_every_minute_of_a_day_is_in_its_own_local_hour(self):
        day0 = (int(time.time()) // 86400 - 2) * 1440        # an epoch minute two days back
        for tz in self.ZONES:
            with self.subTest(tz=tz), _Zone(tz):
                clock = portraits._Clock()
                wrong = [m for m in range(day0, day0 + 1440)
                         if clock.at(m) != tuple(time.localtime(m * 60)[i] for i in (6, 3))]
                self.assertEqual(wrong, [], "%d minutes in another local hour" % len(wrong))

    def test_a_call_past_the_half_hour_counts_in_its_local_hour(self):
        with _Zone("Asia/Kolkata"):
            hour = (int(time.time()) // 3600 - 3) * 3600     # a UTC hour in the window, past
            t0 = hour + 45 * 60                              # every call 45 minutes into it
            local = time.localtime(t0)
            self.assertEqual(local.tm_min, 15, "45 past in UTC is a quarter past in Kolkata")
            row = rows_of(portraits.roster(portraits.Context(
                build(agent("ann", 1000, t0, ["ls"] * 2, gap=10)))))["ann"]
            self.assertEqual([h for h, n in enumerate(row["hours"]) if n], [local.tm_hour])


class UnattributedTest(unittest.TestCase):
    """A session whose records name no user and no uid is no one's row."""

    def test_counted_apart_and_never_a_user_of_no_name(self):
        t0 = time.time() - 3600
        recs = agent("ann", 1000, t0, ["ls"] * 3) + agent(None, 5000, t0, ["ls"] * 3)
        for r in recs:
            r.pop("uid", None)
        ros = portraits.roster(portraits.Context(build(recs)))
        self.assertEqual([r["user"] for r in ros["users"]], ["ann"])
        self.assertEqual(ros["unattributed"], 1)
        self.assertEqual((ros["fleet"]["users"], ros["fleet"]["sessions"]), (1, 1))

    def test_a_uid_with_no_name_is_still_someone(self):
        t0 = time.time() - 3600
        recs = agent(None, 5000, t0, ["ls"] * 3)
        for r in recs:
            r["uid"] = 4242
        ros = portraits.roster(portraits.Context(build(recs)))
        self.assertEqual([r["user"] for r in ros["users"]], ["uid:4242"])
        self.assertEqual(ros["unattributed"], 0)


if __name__ == "__main__":
    unittest.main()
