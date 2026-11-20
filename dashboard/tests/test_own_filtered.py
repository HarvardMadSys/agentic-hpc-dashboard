"""A filtered view takes each process's own disk bytes as the unfiltered one does.

The collector's `io` on an exit holds every child the process reaped, which
each panel takes back off at the parent's exit (reducers/own_io.py). A
filtered view reads only what its filter keeps -- the scan skips the rest
before parsing it (`windows.ViewFilter`) -- so a child the filter leaves out
was never taken off its parent there: a `claude` that read 20 MB itself, under
which a `codex` it started read 100 MB, was 20 MB unfiltered and 120 MB under
`agent_type=claude_code`. The hour chain reads every record: it works each
exit's own figure out once and saves it beside the hour (hours.py, `Ledger`),
and a filtered build gives each exit of a closed hour that figure
(`hours.OwnFigures`), from a day-file or a sealed day's replica alike. The
hour still open is joined within the view, as before, until it closes: then
a held view is built again, and a saved build of it is not resumed
(`OpenHourTest`).

    cd dashboard && .venv/bin/python -m unittest tests.test_own_filtered -q
"""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from rc_dashboard import clock, history, hours, replica         # noqa: E402
from rc_dashboard.normalize import Normalizer                   # noqa: E402
from rc_dashboard.reducers import own_io                        # noqa: E402
from rc_dashboard.windows import HOURS_JOB, ViewFilter, view_key  # noqa: E402
from test_count_once import family, iob, person, raw            # noqa: E402
from test_hours import HOUR, NOW, WIN, HourCase                 # noqa: E402
from test_sessions import cfg                                   # noqa: E402

CLAUDE = ViewFilter(agent_types=["claude_code"])
ALICE = ViewFilter(users=["alice"])
CWD = "/home/alice/proj"
# claude's own disk read in every panel that sums one process's: the I/O
# panel's tools, its session, and where it worked
OWN = {"io_process": 20.0, "sessions": 20.0, "workspace": 20.0}


def nested(t, host="h1", gap=60.0):
    """alice's `claude` 200, which read 20 MB itself, and the `codex` 201 it
    started, which read 100 MB and exited `gap` s before it did: the kernel's
    figure on claude's exit is 120 MB, the codex's among it."""
    return [
        raw("exit", t, 201, 200, "codex", "codex exec fix the tests", agent_type="codex",
            agent_pid=201, tree_root=200, is_agent=True, io=iob(100.0, 100.0),
            duration_s=30.0, host=host),
        raw("exit", t + gap, 200, 50, "claude", "claude", agent_type="claude_code",
            agent_pid=200, tree_root=200, is_agent=True, io=iob(120.0, 130.0),
            duration_s=gap + 600.0, host=host),
    ]


def later(t, host="h2"):
    """bob's `ls`, a record past the family's hour, so that hour has closed."""
    return person("exit", t, 900, 1, "ls", "ls", "bob", io=iob(1.0, 1.0), host=host)


def tools(view):
    return {r["tool"]: r["rd_mb"] for r in view.payload["io_process"]["by_tool"]}


def claude_of(view, host="h1"):
    """claude's own disk read, in each panel that sums one process's."""
    rows = view.payload["trajectories"]["rows"]
    sess = [r["io_rd_mb"] for sid, r in rows.items()
            if sid.startswith(host + "|") and sid.endswith("apid:200")]
    ws = view.reducers["workspace"].keys[(host, "200")].dirs[CWD]
    return {"io_process": tools(view)["claude"], "sessions": sess[0], "workspace": ws[4]}


def series_sum(row):
    return round(sum(v for v in row if v is not None), 3)


class OwnCase(HourCase):
    """HourCase's feed root and pinned clock, its records one stream a host."""

    def write(self, recs, day=None):
        by_host = {}
        for r in recs:
            by_host.setdefault(r["host"], []).append(r)
        for host, rs in by_host.items():
            if day is None:
                self.append("%s.exits.jsonl" % host, rs)
            else:
                self.append("%s.exits.jsonl" % host, rs, day=day)

    def at(self, t):
        """The pinned clock, moved on to `t`."""
        self.clock.stop()
        self.clock = mock.patch("time.time", return_value=t)
        self.clock.start()

    def fresh(self, flt):
        """A build of the view at this moment, from nothing saved: a registry
        with no saved builds over the same hours."""
        return self.build(self.registry(self.cfg(cache_windows=False)), flt=flt)


class NestedAgentTest(OwnCase):
    """The review's case: a Claude whose figure holds a Codex it started."""

    def setUp(self):
        super().setUp()
        self.t = (int(NOW // HOUR) - 1) * HOUR + 600          # a closed hour
        self.write(nested(self.t) + [later(NOW - 30)])

    def test_the_unfiltered_view_takes_the_codex_off_the_claude(self):
        view = self.build(self.registry(self.cfg()))
        self.assertEqual(view.cache["source"], "hours")
        self.assertEqual(claude_of(view), OWN)
        self.assertEqual(tools(view), {"codex": 100.0, "claude": 20.0, "ls": 1.0})

    def test_a_filter_that_leaves_the_codex_out_takes_it_off_too(self):
        reg = self.registry(self.cfg())
        unfiltered = self.build(reg)
        view = self.build(reg, flt=CLAUDE)
        self.assertEqual(view.cache["source"], "scan")
        self.assertEqual(claude_of(view), claude_of(unfiltered))
        self.assertEqual(claude_of(view), OWN)
        p = view.payload
        # and every sum over what the filter keeps -- claude alone -- is its own
        self.assertEqual(tools(view), {"claude": 20.0})
        self.assertEqual(p["io_process"]["by_class"]["agent"]["rd_mb"], 20.0)
        self.assertEqual([(u["user"], u["rd_mb"]) for u in p["io_process"]["by_user"]],
                         [("alice", 20.0)])
        self.assertEqual(p["tool_mix"]["by_class"]["agent"]["io_mb"], 20.0)
        self.assertEqual(series_sum(p["resources"]["series"]["by_class"]["agent"]["rd_mb"]),
                         20.0)
        # and it says where they came from: the window's 3 closed hours, the edge's one
        self.assertIn("the window's 3 closed hour(s)", view.cache["own_io"])
        self.assertIn("(1 exit(s))", view.cache["own_io"])
        self.assertIsNone(unfiltered.cache["own_io"])

    def test_and_its_syscall_bytes(self):
        reg = self.registry(self.cfg())
        want = {r["tool"]: r["rchar_mb"] for r in self.build(reg).payload["io_process"]["by_tool"]}
        got = {r["tool"]: r["rchar_mb"]
               for r in self.build(reg, flt=CLAUDE).payload["io_process"]["by_tool"]}
        self.assertEqual((want["claude"], got["claude"]), (30.0, 30.0))

    def test_a_user_filter_that_keeps_both(self):
        view = self.build(self.registry(self.cfg()), flt=ALICE)
        self.assertEqual(claude_of(view), OWN)
        self.assertEqual(tools(view), {"codex": 100.0, "claude": 20.0})

    def test_a_filter_on_the_child_s_agent_type(self):
        view = self.build(self.registry(self.cfg()), flt=ViewFilter(agent_types=["codex"]))
        self.assertEqual(tools(view), {"codex": 100.0})

    def test_the_figures_are_saved_with_the_hour_by_who_ran_the_process(self):
        reg = self.registry(self.cfg())
        self.build(reg)
        h = int(self.t // HOUR)
        figs = reg.hours.figures(h)
        # the parent alone: the codex took nothing off, and is its `io` as it stands
        self.assertEqual(figs, {("h1", 200, self.t + 60): (20.0, 0.0, 30.0, 0.0)})
        self.assertEqual(reg.hours.figures(h, CLAUDE.holds), figs)
        self.assertEqual(reg.hours.figures(h, ViewFilter(users=["bob"]).holds), {})
        self.assertEqual(reg.hours.status()["figure_hours"], len(reg.hours.have))
        d = os.path.join(self.tmp, "cache", "hours")
        for n in os.listdir(d):
            if n.startswith("o"):
                self.assertEqual(os.stat(os.path.join(d, n)).st_mode & 0o777, 0o600)

    def test_a_sealed_day_read_from_its_replica(self):
        # the family's day-file sealed and converted (history.py): the filtered
        # scan reads it from its tables, and its exits take the hours' figures
        self.compress("h1.exits.jsonl")
        store = history.HistoryStore(self.cfg())
        self.assertEqual(store.run_once(), 1)
        replica.install(store.replica_for, store.replica_failed)
        self.addCleanup(replica.install, None)
        reg = self.registry(self.cfg())
        unfiltered = self.build(reg)
        view = self.build(reg, flt=CLAUDE)
        self.assertGreater(store.status()["lookups"]["replica"], 0)
        self.assertEqual(claude_of(view), claude_of(unfiltered))
        self.assertEqual(claude_of(view), OWN)

    def test_a_saved_build_resumed_joins_what_it_reads_until_its_hour_closes(self):
        c = self.cfg()
        self.build(self.registry(c), flt=CLAUDE)          # saved (`panels.cache_windows`)
        # Another family, in the hour then still open, read by a resume alone.
        self.write([dict(r, pid=r["pid"] + 100, ppid=r["ppid"] + 100 if r["ppid"] == 200
                         else r["ppid"], agent_pid=r["agent_pid"] + 100,
                         tree_root_pid=300, session_key="apid:%d" % (r["agent_pid"] + 100))
                    for r in nested(NOW + 100)])
        # Ten minutes on, that hour still open: a restart resumes, and joins
        # the new family within the view, over what its filter keeps, as a
        # build at that moment does -- its claude keeps its codex's 100 MB.
        self.at(NOW + 600)
        self.write([later(NOW + 570)])
        view = self.build(self.registry(c), flt=CLAUDE)
        self.assertEqual(view.cache["source"], "snapshot", view.cache["note"])
        self.assertEqual(tools(view), {"claude": 140.0})
        self.assertEqual(tools(self.fresh(CLAUDE)), {"claude": 140.0})
        # 41 minutes on, within the window's drift budget, that hour has
        # closed: a restart reads the window again rather than resume a copy
        # that joined it within itself, and both claudes take their own 20 MB.
        self.at(NOW + 2450)
        self.write([later(NOW + 2410)])
        view = self.build(self.registry(c), flt=CLAUDE)
        self.assertEqual(view.cache["source"], "scan")
        self.assertIn("has closed since", view.cache["note"])
        self.assertEqual(tools(view), {"claude": 40.0})


class LoginShellTest(OwnCase):
    """test_count_once's `family`: alice's login `bash` started `claude`, whose
    `zsh` ran a `cat` that read 1000 MB. Under `agent_type=none` -- what the
    collector did not tag, a person's own shell -- the agent's processes are
    left out, and bash's figure holds every one of them."""

    def test_a_person_s_shell_keeps_its_own_bytes_under_none(self):
        self.write([dict(r, host="h1") for r in family((int(NOW // HOUR) - 1) * HOUR + 600)]
                   + [later(NOW - 30)])
        reg = self.registry(self.cfg())
        want = tools(self.build(reg))
        self.assertEqual(want, {"cat": 1000.0, "claude": 5.0, "bash": 1.0, "zsh": 0.0,
                                "ls": 1.0})
        view = self.build(reg, flt=ViewFilter(agent_types=["none"]))
        self.assertEqual(tools(view), {"bash": 1.0, "ls": 1.0})
        self.assertEqual({u["user"]: u["rchar_mb"] for u in view.payload["io_process"]["by_user"]},
                         {"alice": 10.0, "bob": 1.0})


class SeamTest(OwnCase):
    """The codex exits in one hour, the claude in the next: the figure crosses
    the seam in the hours' carry, as their reducers' joins do."""

    def test_across_an_hour_seam(self):
        seam = (int(NOW // HOUR) - 1) * HOUR
        self.write(nested(seam - 30, gap=90.0) + [later(NOW - 30)])
        reg = self.registry(self.cfg())
        self.assertEqual(claude_of(self.build(reg)), OWN)
        self.assertEqual(claude_of(self.build(reg, flt=CLAUDE)), OWN)


class WindowEdgeTest(OwnCase):
    """The codex exits just before the window begins, the claude just after.
    The window's first hour is reduced whole, so its join knows the codex: a
    child read by no reader of the window is still taken off its parent, in
    the edge as in the hours after it, filtered or not."""

    def test_a_child_reaped_before_the_window_began(self):
        since = NOW - WIN * 60
        self.write(nested(since - 120, gap=180.0) + [later(NOW - 30)])
        reg = self.registry(self.cfg())
        unfiltered = self.build(reg)
        self.assertEqual(tools(unfiltered), {"claude": 20.0, "ls": 1.0})
        self.assertEqual(tools(self.build(reg, flt=CLAUDE)), {"claude": 20.0})


class OpenHourTest(OwnCase):
    """The hour still open has no figures yet: a filtered view joins its exits
    over what it keeps, as before, and says so. Built again once the hour has
    closed, it takes the hour's -- and it is built again then, held or saved:
    a held view at the next tick (`WindowRegistry.refresh`), and a saved copy is
    not resumed past it (`WindowRegistry._resume`), the review of #76."""

    def setUp(self):
        super().setUp()
        # claude's exit in the hour still open: 120 MB within the view, 20 once it closes
        self.write(nested(NOW - 900) + [later(NOW - 30)])

    def test_joined_within_the_view_until_the_hour_closes(self):
        reg = self.registry(self.cfg())
        view = self.build(reg, flt=CLAUDE)
        self.assertEqual(tools(view), {"claude": 120.0})
        self.assertIn("still open", view.cache["own_io"])
        # two hours on
        self.at(NOW + 2 * HOUR)
        self.write([later(NOW + 2 * HOUR - 30)])
        self.assertEqual(tools(self.build(reg, flt=CLAUDE)), {"claude": 20.0})

    def test_a_saved_build_is_not_resumed_once_that_hour_has_closed(self):
        c = self.cfg()
        self.assertEqual(tools(self.build(self.registry(c), flt=CLAUDE)), {"claude": 120.0})
        # 41 minutes on, within the window's drift budget, that hour has closed
        self.at(NOW + 2450)
        self.write([later(NOW + 2410)])
        view = self.build(self.registry(c), flt=CLAUDE)     # a restart
        self.assertEqual(view.cache["source"], "scan")
        self.assertIn("has closed since", view.cache["note"])
        self.assertEqual(claude_of(view), OWN)
        self.assertEqual(tools(view), tools(self.fresh(CLAUDE)))

    def test_a_restart_while_that_hour_is_open_resumes(self):
        c = self.cfg()
        self.build(self.registry(c), flt=CLAUDE)
        # ten minutes on, the hour still open
        self.at(NOW + 600)
        self.write([later(NOW + 570)])
        view = self.build(self.registry(c), flt=CLAUDE)
        self.assertEqual(view.cache["source"], "snapshot", view.cache["note"])
        self.assertEqual(view.own_open_hour, int(NOW // HOUR))
        # what a build at that moment holds: the hour is still joined within the view
        self.assertEqual(tools(view), {"claude": 120.0})
        self.assertEqual(tools(self.fresh(CLAUDE)), {"claude": 120.0})

    def test_a_held_view_is_built_again_once_that_hour_has_closed(self):
        reg = self.registry(self.cfg())
        everyone = self.build(reg)
        view = self.build(reg, flt=CLAUDE)
        h = int(NOW // HOUR)
        self.assertEqual((view.own_open_hour, everyone.own_open_hour), (h, None))
        reg._queue.clear()                  # what `request` queued, built above on this thread
        reg.refresh()                       # the hour still open: nothing to build
        self.assertEqual(list(reg._queue), [])
        # 41 minutes on, within the drift budget, that hour has closed: the tick
        # queues its reduction, then the filtered view, and not the unfiltered one
        self.at(NOW + 2450)
        self.write([later(NOW + 2410)])
        reg.refresh()
        self.assertEqual(list(reg._queue), [HOURS_JOB, view_key(WIN, CLAUDE)])
        while reg._queue:                   # the build worker
            reg._job(reg._queue.popleft())
        self.assertEqual((view.state, view.error), ("ready", None))
        self.assertEqual(claude_of(view), OWN)
        self.assertEqual(view.own_open_hour, h + 1)
        reg.refresh()                       # built in the hour now open: nothing more
        self.assertEqual(list(reg._queue), [])


class StragglerTest(OwnCase):
    """A family written late, for an hour already reduced: the hour after finds
    it and patches it in (`hours._patch`), joined among itself, and its figures
    with it."""

    def test_a_late_family_is_patched_into_its_hour_s_figures(self):
        # A file past its first 4 KB, so the next hour's read knows it for the
        # one the last read stopped in (`plan_span`) and finds what came late.
        self.write([person("exit", NOW - 6 * HOUR + 777 + 300 * i, 5000 + i, 1, "ls", "ls",
                           "bob", io=iob(1.0, 1.0)) for i in range(48)])
        reg = self.registry(self.cfg())
        self.build(reg, 300)
        late_t = NOW - 3 * HOUR + 120
        self.write(nested(late_t) + [later(NOW - 30, host="h1")])
        unfiltered = self.build(reg, 300)
        h = int(late_t // HOUR)
        self.assertEqual(reg.hours.meta(h)["stragglers"], 2)
        self.assertEqual(reg.hours.figures(h), {("h1", 200, late_t + 60): (20.0, 0.0, 30.0, 0.0)})
        self.assertEqual(tools(unfiltered)["claude"], 20.0)
        self.assertEqual(tools(self.build(reg, 300, CLAUDE)), {"claude": 20.0})


class NoHoursTest(OwnCase):
    """With no hours kept (`panels.merge_reduce` off) nothing has read every
    record: a filtered view joins what it keeps, as it always did, and says so."""

    def test_falls_back_to_the_join_and_says_why(self):
        self.write(nested((int(NOW // HOUR) - 1) * HOUR + 600) + [later(NOW - 30)])
        reg = self.registry(self.cfg(merge=False))
        self.assertEqual(tools(self.build(reg)), {"codex": 100.0, "claude": 20.0, "ls": 1.0})
        view = self.build(reg, flt=CLAUDE)
        self.assertEqual(tools(view), {"claude": 120.0})
        self.assertIn("panels.merge_reduce", view.cache["own_io"])
        # no hour it could take figures from: neither a held view nor a saved
        # one is built again for one closing
        self.assertIsNone(view.own_open_hour)


class BrokenHoursTest(OwnCase):
    """Hours kept, but they cannot be reduced (`HourStore.ensure` raises, as a
    full disk would have it): every exit is joined within the view, which says
    so, and the build still records the hour still open. So a held view is
    built again once each hour closes, to take the figures should the hours
    work again -- and not at every tick while they stay broken."""

    def test_built_again_once_an_hour_closes_not_every_tick(self):
        self.write(nested(NOW - 900) + [later(NOW - 30)])
        reg = self.registry(self.cfg())
        broken = mock.patch.object(reg.hours, "ensure",
                                   side_effect=OSError("No space left on device"))
        broken.start()
        self.addCleanup(broken.stop)
        view = self.build(reg, flt=CLAUDE)
        self.assertIn("could not be reduced", view.cache["own_io"])
        self.assertEqual(tools(view), {"claude": 120.0})
        h = int(NOW // HOUR)
        self.assertEqual(view.own_open_hour, h)
        reg._queue.clear()                  # what `request` queued, built above on this thread
        reg.refresh()
        self.assertEqual(list(reg._queue), [])
        # 41 minutes on, that hour has closed: built again once
        self.at(NOW + 2450)
        self.write([later(NOW + 2410)])
        reg.refresh()
        self.assertEqual(list(reg._queue), [view_key(WIN, CLAUDE)])
        reg._job(reg._queue.popleft())
        self.assertEqual((view.state, view.error), ("ready", None))
        self.assertEqual(view.own_open_hour, h + 1)
        self.assertEqual(tools(view), {"claude": 120.0})   # the store still broken
        # and then not again, tick after tick, until the next hour closes
        for _ in range(3):
            reg.refresh()
            self.assertEqual(list(reg._queue), [])


class NodeSplitTest(OwnCase):
    """`ingest.workers` >= 2, each node's hour in reducer groups: the figures each
    node's units make are the ones one pass makes."""

    def setUp(self):
        super().setUp()
        self.t = (int(NOW // HOUR) - 1) * HOUR + 600
        self.write(nested(self.t, "h1") + nested(self.t + 5, "h2") + [later(NOW - 30, "h3")])
        # the workers pin their clock to the service's (`clock.install`)
        self.pin = mock.patch.dict(clock._state, {"pinned": True, "as_of": NOW})
        self.pin.start()

    def tearDown(self):
        self.pin.stop()
        super().tearDown()

    def built(self, workers, groups, tag):
        c = self.cfg()
        c.tree["ingest"]["workers"] = workers
        c.tree["ingest"]["reducer_groups"] = groups
        c.tree["cache"]["dir"] = os.path.join(self.tmp, "cache-%s" % tag)
        reg = self.registry(c)
        self.addCleanup(reg.stop)
        return reg, self.build(reg, flt=CLAUDE)

    def test_split_by_node_and_group_as_one_pass(self):
        one, v1 = self.built(1, 1, "one")
        split, v2 = self.built(4, 2, "split")
        self.assertGreater(split.hours.stats["node_tasks"], 0)
        h = int(self.t // HOUR)
        self.assertEqual(split.hours.figures(h), one.hours.figures(h))
        self.assertEqual(set(one.hours.figures(h)),
                         {("h1", 200, self.t + 60), ("h2", 200, self.t + 65)})
        for view in (v1, v2):
            # two claudes, one a node, each its own 20 MB
            self.assertEqual(tools(view), {"claude": 40.0})
            for host in ("h1", "h2"):
                self.assertEqual(dict(claude_of(view, host), io_process=20.0), OWN)


class JoinTest(unittest.TestCase):
    """The join itself: a record that carries its own figure is given it."""

    def exit(self, pid, ppid, t, dur, rd, **kw):
        return dict({"event": "exit", "host": "h1", "pid": pid, "ppid": ppid, "user": "bob",
                     "_ts_epoch": t, "duration_s": dur,
                     "io": {"rd_mb": rd, "wr_mb": 0.0, "scope": "process"}}, **kw)

    def test_a_stored_figure_is_given_and_the_join_holds_what_it_would_have(self):
        stored, derived = own_io.OwnIo(own_io.DISK), own_io.OwnIo(own_io.DISK)
        for j, own in ((stored, {"rd_mb": 7.0, "wr_mb": 0.0}), (derived, None)):
            j.take(self.exit(301, 300, 1000.0, 1.0, 100.0))
            parent = self.exit(300, 1, 1010.0, 60.0, 120.0)
            if own is not None:
                parent[own_io.OWN] = own
            got = j.take(parent)
            self.assertEqual(got, (7.0, 0.0) if own is not None else (20.0, 0.0))
            # what it held for the parent is gone, its whole figure held for its own
            self.assertNotIn(("h1", 300), j.reaped)
            self.assertEqual(j.reaped[("h1", 1)][0][own_io._G_VALS:], [120.0, 0.0])

    def test_the_ledger_puts_every_exit_s_on_it_and_keeps_the_parents(self):
        led = own_io.Ledger(part=hours.own_part)
        norm = Normalizer(cfg())
        recs = [norm.normalize(dict(r)) for r in nested(1_000_000.0)]
        for r in recs:
            led.feed(r)
        self.assertEqual([r[own_io.OWN]["rd_mb"] for r in recs], [100.0, 20.0])
        self.assertEqual(led.take_figures(),
                         {("alice", "claude_code"): {("h1", 200, 1_000_060.0):
                                                     (20.0, 0.0, 30.0, 0.0)}})
        self.assertEqual(led.take_figures(), {})

    def test_a_filter_holds_the_parts_of_what_it_keeps(self):
        self.assertTrue(CLAUDE.holds(("alice", "claude_code")))
        self.assertFalse(CLAUDE.holds(("alice", "codex")))
        self.assertTrue(ALICE.holds(("alice", "none")))
        self.assertFalse(ALICE.holds(("bob", "claude_code")))
        both = ViewFilter(users=["alice"], agent_types=["none"])
        self.assertTrue(both.holds(("alice", "none")))
        self.assertFalse(both.holds(("alice", "codex")))
        # a part is the record's own: the filter's predicate keeps its records
        norm = Normalizer(cfg())
        for r in nested(1_000_000.0) + [later(1_000_100.0)]:
            n = norm.normalize(dict(r))
            for f in (CLAUDE, ALICE, both):
                self.assertEqual(f.holds(hours.own_part(n)), f.match(n))


if __name__ == "__main__":
    unittest.main()
