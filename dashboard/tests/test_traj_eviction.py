"""Trajectory sessions past `reducers.trajectories.max_sessions`: the least
recently active go, each whole, and the rest stay whole.

Schema 7 keys a process on its nearest classifier-tagged ancestor, so every
Claude Code Bash tool call is a key of its own (test_sessions.py), and a busy
login node makes more keys in a day than the cap: boslogin06 made 74,640 on
2026-10-28, against the default 50,000. Past the cap, the reducer evicted
every key last seen more than half a window before the service's present. A
window is merged from its hours oldest first, and a frozen capture's present
is the window's end, so when the cap was reached in the morning every key held
looked idle and all of them went. The 24 h view of that day kept the afternoon
of each long session: 178 of apid 2342259's 1,749 keys.

Here a past day is built as the frozen FAS instance serves one, the clock
pinned to the window's end and the cap lowered, by one pass and by merging
saved hours.

    cd dashboard && .venv/bin/python -m unittest discover -s tests -q
"""
import copy
import os
import pickle
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from rc_dashboard import config as configmod, hours, snapshots   # noqa: E402
from rc_dashboard.normalize import Normalizer                     # noqa: E402
from rc_dashboard.windows import PanelView                        # noqa: E402
from test_sessions import HOME, SNAP, rec                         # noqa: E402

HOUR = 3600
PROBE = HOME + "/.codex/packages/app-server-daemon/current/bin/codex --version"
# Local midnight three days back, so every record shares one date (a sid
# carries its day), and END, the clock, pinned at the window's end.
_D = time.localtime(time.time() - 3 * 86400)
DAY0 = time.mktime((_D.tm_year, _D.tm_mon, _D.tm_mday, 0, 0, 0, 0, 0, -1))
END = DAY0 + 20 * HOUR
CAP = 100


def cfg(cap):
    tree = configmod.defaults()
    tree["reducers"]["trajectories"]["max_sessions"] = cap
    return configmod.Config(tree, {}, None)


def classes(c):
    return list(c.get("filters.classes")) + [c.get("filters.unlabeled_class")]


def calls(root, t, until, every, pid):
    """An agent at `root` making a Bash tool call every `every` s from `t`:
    the command, then the snapshot shell it ran in -- a key of its own."""
    out = [rec("residency", t - 30, root, 50, "claude", "claude",
               agent_pid=root, tree_root=root, is_agent=True)]
    while t < until:
        out += [rec("exit", t, pid + 1, pid, "git", "git status",
                    agent_pid=pid, tree_root=root),
                rec("exit", t + 1, pid, root, "zsh", SNAP + "'git status'",
                    agent_pid=pid, tree_root=root, is_agent=True)]
        t += every
        pid += 2
    return out


def day(long_every=20 * 60):
    """A session at work all day, one done by mid-morning, one at work only in
    the last two hours, and a status-bar script probing codex through the
    morning, every probe a key and a tree of its own: 219 keys in all."""
    recs = calls(100, DAY0 + 60, END - 60, long_every, 10000)
    recs += calls(400, DAY0 + 2 * HOUR, DAY0 + 2 * HOUR + 300, 60, 40000)
    recs += calls(300, END - 100 * 60, END - 10 * 60, 20 * 60, 30000)
    recs += [rec("exit", DAY0 + HOUR + 150 * i, 50000 + i, 1, "codex", PROBE,
                 agent_pid=50000 + i, tree_root=50000 + i, is_agent=True,
                 agent_type="codex") for i in range(150)]
    recs.sort(key=lambda r: r["ts_epoch"])
    return recs


def exits_by_tree(raw):
    out = {}
    for r in raw:
        if r["event"] == "exit":
            out[r["tree_root_pid"]] = out.get(r["tree_root_pid"], 0) + 1
    return out


def normalized(c, raw):
    norm = Normalizer(c)
    return [n for n in (norm.normalize(r) for r in copy.deepcopy(raw)) if n is not None]


def feed(red, recs):
    hours.feed_all(red, ((r["_ts_epoch"], r) for r in recs))


def one_pass(c, recs):
    view = PanelView(c, classes(c), 1440)
    view.reducers = view.fresh_reducers()
    feed(view.reducers, recs)
    return view


def from_hours(c, recs):
    """Each hour reduced and saved as the hour store does it, then merged in
    order into a 24 h window (hours.build)."""
    fresh = lambda: PanelView(c, classes(c), 10080).fresh_reducers()   # noqa: E731
    by_hour = {}
    for r in recs:
        by_hour.setdefault(int(r["_ts_epoch"] // HOUR), []).append(r)
    view = PanelView(c, classes(c), 1440)
    acc = view.reducers = view.fresh_reducers()
    carry = None
    for h in sorted(by_hour):
        red = hours.prepare(fresh())
        if carry is not None:
            hours.seed(red, carry)
        feed(red, by_hour[h])
        carry = pickle.loads(pickle.dumps(hours.take_carry(red)))
        hours.strip(red)
        hours.merge_into(acc, snapshots.restore(fresh(), pickle.loads(
            snapshots.capture(red))))
    hours.install(acc, carry)
    return view


def agents(view):
    """agent pid -> its row's calls, every agent the window holds."""
    traj = view.reducers["trajectories"]
    return {pid: s.n for (_, pid, _), (s, _, _) in traj.agent_sessions().items()}


class PastTheCapTest(unittest.TestCase):
    """The cap reached in the morning of a day served after it, as on the
    frozen FAS instance (the clock pinned) and as when a past day is read
    against the wall clock."""

    def build(self, how, pinned, cap=CAP, raw=None):
        c = cfg(cap)
        recs = normalized(c, raw or day())
        if not pinned:
            return how(c, recs)
        with mock.patch("time.time", return_value=END):
            return how(c, recs)

    def each(self):
        """The day built every way: (what, view)."""
        return [("%s, %s clock" % (how.__name__, "pinned" if pinned else "wall"),
                 self.build(how, pinned))
                for how in (one_pass, from_hours) for pinned in (True, False)]

    def test_the_day_is_past_the_cap(self):
        for what, view in self.each():
            with self.subTest(what):
                traj = view.reducers["trajectories"]
                self.assertGreater(traj.evicted, 0)
                self.assertLessEqual(len(traj.sessions), CAP)

    def test_every_session_active_in_the_last_hours_is_whole(self):
        want = exits_by_tree(day())
        for what, view in self.each():
            with self.subTest(what):
                held = agents(view)
                for root in (100, 300):
                    self.assertEqual(held.get(root), want[root], root)

    def test_a_session_evicted_goes_whole(self):
        # whatever the cap took, it took a whole session: no row is part of one
        want = exits_by_tree(day())
        for what, view in self.each():
            with self.subTest(what):
                for pid, n in agents(view).items():
                    self.assertEqual(n, want[pid], pid)

    def test_the_least_recently_active_go_first(self):
        # the session done by mid-morning and the earliest probes, not the
        # session still at work
        for what, view in self.each():
            with self.subTest(what):
                held = agents(view)
                self.assertNotIn(400, held)
                self.assertNotIn(50000, held)
                self.assertIn(50149, held)

    def test_the_row_served_counts_every_call(self):
        # the panel's own row, where 2342259's folded 178 of its 1,749 keys
        view = self.build(from_hours, True)
        with mock.patch("time.time", return_value=END):
            t = view.materialize()["trajectories"]
        top = t["rows"][t["ranks"]["events"][0]]
        self.assertEqual((top["agent_pid"], top["n_events"]),
                         ("100", exits_by_tree(day())[100]))

    def test_a_session_going_on_at_the_cap_is_as_recent_as_its_new_call(self):
        # The oldest session held makes a call in the hour that crosses the
        # cap, after a probe there: the probe's key comes first, so a merge
        # that made room key by key would drop the session before its call
        # came in, and a pass that judged it before its call would too.
        raw = calls(100, DAY0 + HOUR, DAY0 + HOUR + 1, 60, 10000)
        raw += [rec("exit", DAY0 + 2 * HOUR + 60 * i, 50000 + i, 1, "codex", PROBE,
                    agent_pid=50000 + i, tree_root=50000 + i, is_agent=True,
                    agent_type="codex") for i in range(8)]
        raw += [rec("exit", DAY0 + 3 * HOUR + 10, 50100, 1, "codex", PROBE,
                    agent_pid=50100, tree_root=50100, is_agent=True, agent_type="codex")]
        raw += calls(100, DAY0 + 3 * HOUR + 60, DAY0 + 3 * HOUR + 61, 60, 10002)[1:]
        for how in (one_pass, from_hours):
            with self.subTest(build=how.__name__):
                view = self.build(how, True, cap=10, raw=raw)
                self.assertGreater(view.reducers["trajectories"].evicted, 0)
                held = agents(view)
                self.assertEqual(held.get(100), 4)
                self.assertNotIn(50000, held)

    def test_a_session_bigger_than_the_cap_keeps_its_newest_calls(self):
        raw = calls(100, DAY0 + 60, END - 60, 20 * 60, 10000)
        last = max(r["ts_epoch"] for r in raw)
        for how in (one_pass, from_hours):
            with self.subTest(build=how.__name__):
                traj = self.build(how, True, cap=20, raw=raw).reducers["trajectories"]
                self.assertLessEqual(len(traj.sessions), 20)
                (s, _, _), = traj.agent_sessions().values()
                self.assertEqual(s.last, last)
                self.assertGreater(s.first, DAY0 + 60)
                self.assertEqual(s.n, 2 * len(traj.sessions))


if __name__ == "__main__":
    unittest.main()
