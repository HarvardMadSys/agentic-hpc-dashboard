"""Each process counted once: at its exit, and with its own disk bytes.

Two ways the per-class totals counted one process's resources twice.

* A process alive across a collector stop. At each stop the collector writes a
  `truncated` record for every live process it tracks, with its whole life's
  CPU and I/O so far; after the restart it tracks the process again, and its
  exit carries its whole life's totals again. Summed as both came, one `vim`
  that ran 3605 CPU-s and read 505 MB was 7205 CPU-s and 1005 MB, two
  processes. A sum takes the exit only.
* Disk I/O summed at every ancestor. The collector's `io` is the kernel's
  `task->ioac + task->signal->ioac`, and the kernel adds into `signal->ioac`
  every child the process reaped. A `cat` that read 1000 MB under `zsh` under
  `claude`, started from a login `bash`, was 4011 MB of its user's: 3005 MB of
  agent reads and 1006 MB of human ones, for 1005 and 1. Every sum takes each
  process's own figure (reducers/own_io.py), as the sessions always did.

    cd dashboard && .venv/bin/python -m unittest tests.test_count_once -q
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from rc_dashboard import config as configmod                   # noqa: E402
from rc_dashboard import hours, iocmds                         # noqa: E402
from rc_dashboard.export import parse_filters                  # noqa: E402
from rc_dashboard.normalize import Normalizer                  # noqa: E402
from rc_dashboard.proctree import TreeBuilder                  # noqa: E402
from rc_dashboard.reducers import live as livemod              # noqa: E402
from rc_dashboard.reducers import own_io                       # noqa: E402
from rc_dashboard.reducers.live import LiveReducer             # noqa: E402
from rc_dashboard.reducers.trajectories import TrajectoryReducer   # noqa: E402
from rc_dashboard.windows import PanelView                     # noqa: E402
from test_ingest import FeedCase                               # noqa: E402
from test_merge import (HOUR, NOW, diff, hour_states, materialize,   # noqa: E402
                        merged, normalized, one_pass)
from test_sessions import cfg                                  # noqa: E402

CLASSES = ["agent", "human", "ide"]
TS = "%Y-%m-%d %H:%M:%S"


def raw(event, t, pid, ppid, command, args, user="alice", cls="agent",
        agent_type="claude_code", agent_pid=None, tree_root=None, is_agent=False,
        host="h1", **kw):
    """A collector record (schema 8): an agent's process when `agent_pid` is
    given, else a person's, in a terminal."""
    r = {"schema_version": 8, "event": event, "ts_epoch": t,
         "ts": time.strftime(TS, time.localtime(t)), "host": host, "user": user,
         "uid": 1000, "pid": pid, "ppid": ppid, "command": command, "args": args,
         "actor3": cls, "agent_type": agent_type, "is_agent": is_agent,
         "agent_pid": agent_pid, "tree_root_pid": tree_root, "cpu_s": 0.1,
         "duration_s": 1.0, "exit_code": 0, "signal": None,
         "sandbox": "unsandboxed", "sandbox_src": "proc", "cwd": "/home/%s/proj" % user,
         "tty": None if agent_pid is not None else "pts/1"}
    if agent_pid is not None:
        r["session_key"] = "apid:%s" % agent_pid
        r["depth"] = 0 if pid == agent_pid else 1
    if event == "truncated":
        r.update(exit_code=None, duration_src="proc")
    r.update(kw)
    return r


def person(event, t, pid, ppid, command, args, user, **kw):
    return raw(event, t, pid, ppid, command, args, user=user, cls="human", agent_type=None,
               **kw)


def window(recs, minutes=1440, c=None):
    """`recs` normalised and fed to a window's reducer set, as a build feeds it.
    Returns the view, with its panels materialised."""
    c = c or cfg()
    view = PanelView(c, CLASSES + [c.get("filters.unlabeled_class")], minutes)
    view.reducers = view.fresh_reducers()
    view.dispatch = PanelView.build_dispatch(view.reducers)
    norm = Normalizer(c)
    for r in recs:
        n = norm.normalize(dict(r))
        if n is not None:
            view.feed(n)
    view.materialize()
    return view


def live(recs, older=False, c=None):
    """The live tier fed `recs` in the order given; `older` as the backfill feeds."""
    c = c or cfg()
    lv = LiveReducer(c, CLASSES)
    norm = Normalizer(c)
    for r in recs:
        n = norm.normalize(dict(r))
        if n is not None:
            lv.feed(n, older=older)
    return lv


def series_sum(row):
    return round(sum(v for v in row if v is not None), 3)


class TruncatedThenExitTest(unittest.TestCase):
    """One `vim`, alive across a collector stop: `truncated` with 3600 CPU-s and
    500 MB read, then its exit with 3605 CPU-s and 505 MB read. One process,
    3605 CPU-s, 505 MB."""

    @classmethod
    def setUpClass(cls):
        cls.now = time.time()
        t = cls.now - 900
        trunc = {"rd_mb": 500.0, "wr_mb": 100.0, "rchar_mb": 800.0, "wchar_mb": 200.0,
                 "scope": "proc"}
        done = {"rd_mb": 505.0, "wr_mb": 100.0, "rchar_mb": 810.0, "wchar_mb": 200.0,
                "scope": "process"}
        cls.recs = [
            person("truncated", t, 300, 250, "vim", "vim notes.txt", "carol", cpu_s=3600.0,
                   duration_s=86400.0, tty="pts/3", io=trunc),
            person("exit", t + 300, 300, 250, "vim", "vim notes.txt", "carol", cpu_s=3605.0,
                   duration_s=86700.0, tty="pts/3", io=done),
        ]
        cls.view = window(cls.recs)
        cls.p = cls.view.payload

    def test_disk_io_by_class_counts_one_process(self):
        h = self.p["io_process"]["by_class"]["human"]
        self.assertEqual(h["events"], 1)
        self.assertEqual((h["rd_mb"], h["wr_mb"], h["rchar_mb"], h["wchar_mb"]),
                         (505.0, 100.0, 810.0, 200.0))
        self.assertEqual(h["quants"]["rd_mb"]["n"], 1)
        self.assertEqual([(u["user"], u["events"], u["rd_mb"])
                          for u in self.p["io_process"]["by_user"]], [("carol", 1, 505.0)])

    def test_resources_count_its_cpu_once(self):
        r = self.p["resources"]
        h = r["by_class"]["human"]
        self.assertEqual((h["events"], h["cpu_s"]), (1, 3605.0))
        self.assertEqual(h["quants"]["cpu_s"]["n"], 1)
        s = r["series"]["by_class"]["human"]
        self.assertEqual(series_sum(s["n"]), 1)
        self.assertEqual(series_sum(s["cpu_s"]), 3605.0)
        self.assertEqual(series_sum(s["rd_mb"]), 505.0)

    def test_risk_totals_and_sandbox_count_it_once(self):
        self.assertEqual(self.p["risk"]["totals"], {"events": 1, "cpu_s": 3605.0})
        self.assertEqual(self.p["sandbox"]["n_events"], {"human": 1})

    def test_the_sessions_already_took_its_exit_alone(self):
        rows = list(self.p["trajectories"]["rows"].values())
        self.assertEqual([(r["n_events"], r["cpu_s"], r["io_rd_mb"]) for r in rows],
                         [(1, 3605.0, 505.0)])
        self.assertEqual(self.p["tool_mix"]["by_class"]["human"]["calls"], 1)

    def test_the_live_tier_counts_it_once(self):
        lv = live(self.recs)
        now = lv.result(now=self.now, window_s=3600)["now"]
        self.assertEqual((now["events"]["human"], now["cpu_s"]["human"]), (1, 3605.0))
        rows = lv.users(now=self.now, window_s=3600)["rows"]
        self.assertEqual([(u["user"], u["n"], u["cpu_s"], u["rd_mb"]) for u in rows],
                         [("carol", 1, 3605.0, 505.0)])
        # the tail lists exits: a process running at a stop has not exited
        self.assertEqual([(e["pid"], e["cpu_s"]) for e in lv.event_tail], [(300, 3605.0)])

    def test_the_backfill_counts_it_once(self):
        lv = live(reversed(self.recs), older=True)
        now = lv.result(now=self.now, window_s=3600)["now"]
        self.assertEqual((now["events"]["human"], now["cpu_s"]["human"]), (1, 3605.0))


class AgentAliveAcrossAStopTest(unittest.TestCase):
    """An agent and an interactive `srun`, each alive across a stop."""

    @classmethod
    def setUpClass(cls):
        t = time.time() - 900
        kw = dict(agent_pid=100, tree_root=100)
        cls.view = window([
            raw("truncated", t, 100, 50, "claude", "claude", is_agent=True, cpu_s=50.0, **kw),
            raw("truncated", t, 101, 100, "zsh", "zsh -c make", cpu_s=1.0, **kw),
            person("truncated", t, 700, 650, "srun", "srun --pty bash", "erin", cpu_s=2.0),
            raw("exit", t + 60, 101, 100, "zsh", "zsh -c make", cpu_s=1.5, **kw),
            raw("exit", t + 90, 100, 50, "claude", "claude", is_agent=True, cpu_s=60.0, **kw),
            person("exit", t + 120, 700, 650, "srun", "srun --pty bash", "erin", cpu_s=2.5),
        ])
        cls.p = cls.view.payload

    def test_the_role_census_counts_each_exit_once(self):
        census = {(r["agent_type"], r["role"]): (r["n"], r["cpu_s"])
                  for r in self.p["cascades"]["role_census"]}
        self.assertEqual(census[("claude_code", "agent")], (1, 60.0))

    def test_the_workspace_counts_each_process_once(self):
        ws = self.view.reducers["workspace"]
        self.assertEqual(sum(st.procs for st in ws.keys.values()), 2)
        self.assertEqual(ws.fed, 2)

    def test_slurm_counts_one_run(self):
        self.assertEqual(self.p["slurm"]["by_class"]["human"]["runs"], 1)

    def test_a_truncated_record_still_names_the_running_root(self):
        # identity is not a sum: the sandbox and the process index still read it
        t = time.time() - 60
        v = window([raw("truncated", t, 100, 50, "claude", "claude --dangerously-skip-permissions",
                        is_agent=True, agent_pid=100, tree_root=100)])
        self.assertIn(("h1", 100), v.reducers["sandbox"].roots)
        self.assertEqual(v.payload["sandbox"]["n_events"], {})


def iob(rd, rc):
    return {"rd_mb": rd, "wr_mb": 0.0, "rchar_mb": rc, "wchar_mb": 0.0, "scope": "process"}


def family(t, shell_at=1.0):
    """`claude` 100 > `zsh` 110 > `cat` 111, which reads 1000 MB; `claude` reads 5
    MB itself, and was started from alice's login `bash` 50, which read 1 MB of
    its own. Each figure is the kernel's: its own, and every child it reaped.
    Own disk reads: cat 1000, zsh 0, claude 5 (agent 1005), bash 1 (human 1);
    own syscall reads: 1000, 0.5, 299.5 (agent 1300), 10 (human 10)."""
    kw = dict(agent_pid=100, tree_root=100)
    return [
        raw("exit", t, 111, 110, "cat", "cat big.bin", io=iob(1000.0, 1000.0), depth=2, **kw),
        raw("exit", t + shell_at, 110, 100, "zsh", "zsh -c cat big.bin",
            io=iob(1000.0, 1000.5), duration_s=shell_at + 1.0, **kw),
        raw("exit", t + 100, 100, 50, "claude", "claude", is_agent=True,
            io=iob(1005.0, 1300.0), duration_s=400.0, **kw),
        person("exit", t + 150, 50, 1, "bash", "-bash", "alice", io=iob(1006.0, 1310.0),
               duration_s=3600.0),
    ]


def rd_of(lv, now):
    return {u["user"]: u["rd_mb"] for u in lv.users(now=now, window_s=3600)["rows"]}


class ReapedChildrenTest(unittest.TestCase):
    """Each panel that sums disk bytes, over `family`."""

    @classmethod
    def setUpClass(cls):
        cls.now = time.time()
        cls.recs = family(cls.now - 800)
        cls.view = window(cls.recs)
        cls.p = cls.view.payload

    def test_the_io_panel_takes_each_process_s_own_bytes(self):
        b = self.p["io_process"]["by_class"]
        self.assertEqual((b["agent"]["rd_mb"], b["human"]["rd_mb"]), (1005.0, 1.0))
        self.assertEqual((b["agent"]["rchar_mb"], b["human"]["rchar_mb"]), (1300.0, 10.0))
        self.assertEqual(b["agent"]["quants"]["rd_mb"]["max"], 1000.0)
        self.assertEqual([(u["user"], u["rd_mb"]) for u in self.p["io_process"]["by_user"]],
                         [("alice", 1006.0)])
        self.assertEqual({t["tool"]: t["rd_mb"] for t in self.p["io_process"]["by_tool"]},
                         {"cat": 1000.0, "zsh": 0.0, "claude": 5.0, "bash": 1.0})

    def test_the_tool_mix_takes_each_process_s_own_bytes(self):
        a = self.p["tool_mix"]["by_class"]["agent"]
        self.assertEqual(a["io_mb"], 1005.0)
        self.assertEqual({r["b"]: r["io_mb"] for r in a["rows"] if r["io_mb"]},
                         {"runtime": 5.0, "files": 1000.0})

    def test_the_resource_series_takes_each_process_s_own_bytes(self):
        s = self.p["resources"]["series"]["by_class"]
        self.assertEqual((series_sum(s["agent"]["rd_mb"]), series_sum(s["human"]["rd_mb"])),
                         (1005.0, 1.0))
        self.assertEqual(series_sum(s["agent"]["rchar_mb"]), 1300.0)

    def test_the_workspace_puts_each_process_s_own_bytes_where_it_ran(self):
        d = self.view.reducers["workspace"].keys[("h1", "100")].dirs["/home/alice/proj"]
        self.assertEqual((d[0], d[2], d[4]), (3, 1300.0, 1005.0))

    def test_the_sessions_are_unchanged(self):
        rows = sorted((r["actor3"], r["io_rd_mb"])
                      for r in self.p["trajectories"]["rows"].values())
        self.assertEqual(rows, [("agent", 1005.0), ("human", 1.0)])

    def test_the_inspector_sums_a_subtree_once(self):
        b = TreeBuilder()
        norm = Normalizer(cfg())
        for r in self.recs[:3]:
            b.feed(norm.normalize(dict(r)))
        root = b.resolve(100, (100,), lambda pid: None)
        tree, _ = b.render(root, 100)
        zsh = tree["kids"][0]
        cat = zsh["kids"][0]
        self.assertEqual((tree["io"]["rd_mb"], tree["sub_io"]["rd_mb"]), (5.0, 1005.0))
        self.assertEqual((zsh["io"]["rd_mb"], zsh["sub_io"]["rd_mb"]), (0.0, 1000.0))
        self.assertEqual((cat["io"]["rd_mb"], cat.get("sub_io")), (1000.0, None))


class LiveOwnIoTest(unittest.TestCase):
    """The live tier's roster and tails, in each order it is fed in: the tail
    oldest first, the backfill newest first, and the two meeting at the point
    the tail began, where one process can be in either."""

    def setUp(self):
        self.now = time.time()
        self.recs = family(self.now - 800)
        self.norm = Normalizer(cfg())
        self.lv = LiveReducer(cfg(), CLASSES)

    def feed(self, recs, older):
        for r in recs:
            self.lv.feed(self.norm.normalize(dict(r)), older=older)

    def check(self):
        self.assertEqual(rd_of(self.lv, self.now), {"alice": 1006.0})
        rows = {e["pid"]: e["rd_mb"] for e in self.lv.event_tail}
        self.assertEqual(rows, {111: 1000.0, 110: 0.0, 100: 5.0, 50: 1.0})
        by = self.lv.users(now=self.now, window_s=3600)["rows"][0]["by_class"]
        self.assertEqual((by["agent"]["rd_mb"], by["human"]["rd_mb"]), (1005.0, 1.0))
        self.assertEqual(by["agent"]["n"], 3)

    def test_the_tail_oldest_first(self):
        self.feed(self.recs, older=False)
        self.check()

    def test_the_backfill_newest_first(self):
        self.feed(reversed(self.recs), older=True)
        self.check()

    def test_children_backfilled_before_their_parent_s_exit_is_tailed(self):
        self.feed(reversed(self.recs[:2]), older=True)
        self.feed(self.recs[2:], older=False)
        self.check()

    def test_a_parent_tailed_before_its_children_are_backfilled(self):
        self.feed(self.recs[2:], older=False)
        self.feed(reversed(self.recs[:2]), older=True)
        self.check()
        self.assertEqual(self.lv.result(now=self.now)["own_io"]["dropped"], 0)

    def test_a_child_that_outlived_its_parent_is_not_taken_off_it(self):
        t = self.now - 600
        recs = [person("exit", t, 200, 1, "bash", "bash run.sh", "bob", io=iob(7.0, 7.0),
                       duration_s=60.0),
                # started by it, ended after it: never reaped, so not in its figure
                person("exit", t + 50, 201, 200, "sleep", "sleep 100", "bob",
                       io=iob(3.0, 3.0), duration_s=100.0)]
        for older, order in ((False, recs), (True, recs[::-1])):
            with self.subTest(older=older):
                self.lv, self.norm = LiveReducer(cfg(), CLASSES), Normalizer(cfg())
                self.feed(order, older)
                self.assertEqual(rd_of(self.lv, self.now), {"bob": 10.0})
                # nor held for a later holder of its pid
                self.assertNotIn(("h1", 200), self.lv.io_given.reaped)


class HourSeamTest(unittest.TestCase):
    """`cat` exits in one hour and the shell that reaped it in the next: the
    join is carried, as the sessions' always was."""

    def setUp(self):
        seam = (int(NOW // HOUR) - 1) * HOUR
        self.recs = family(seam - 30, shell_at=60.0)

    def test_every_panel_merged_from_hours_is_the_one_pass(self):
        want = materialize(one_pass(normalized(self.recs)))
        saved, carry, fresh = hour_states(normalized(self.recs))
        self.assertGreaterEqual(len(saved), 2)
        got = materialize(merged(saved, fresh, carry))
        self.assertEqual(want["io_process"]["by_class"]["agent"]["rd_mb"], 1005.0)
        self.assertEqual(want["tool_mix"]["by_class"]["agent"]["io_mb"], 1005.0)
        for key in ("io_process", "tool_mix", "resources", "trajectories"):
            with self.subTest(panel=key):
                self.assertEqual(diff(want[key], got[key]), [])

    def test_without_the_carry_the_shell_would_count_its_child_again(self):
        # each hour reduced on its own: the shell never hears of `cat`
        recs = normalized(self.recs)
        before, _, fresh = hour_states(recs[:1])
        after, carry, _ = hour_states(recs[1:])
        cold = materialize(merged(before + after, fresh, carry))
        self.assertEqual(cold["io_process"]["by_class"]["agent"]["rd_mb"], 2005.0)

    def test_the_join_s_carry_is_aged_and_capped_with_the_rest(self):
        for key in ("io_process", "tool_mix", "resources", "workspace"):
            with self.subTest(reducer=key):
                r = PanelView(cfg(), CLASSES, 60).fresh_reducers()[key]
                self.assertIn("reaped", r.CARRY_AGED)
                self.assertIn("reaped", r.carry_caps())
                self.assertIn("reaped", r.carry())
        self.assertEqual(TrajectoryReducer.CARRY_AGED[0], "reaped")


class PidReuseTest(unittest.TestCase):
    """A child that outlives its parent keeps naming it (the collector holds the
    parent a process was forked by), so its exit leaves its bytes under a pid
    that is free, and a later process may take that pid. alice's `bash` 500
    exits; its `rsync` 501, started under it, runs on and exits with 100 MB;
    then 500 is another process's, which reads 20 MB, 5 of them through its
    own child 502. Nothing it reaped is the rsync's."""

    def setUp(self):
        self.now = time.time()

    def recs(self, user, child=True):
        t = self.now - 1200
        out = [person("exit", t, 500, 1, "bash", "bash sync.sh", "alice", io=iob(7.0, 7.0),
                      duration_s=60.0),
               person("exit", t + 60, 501, 500, "rsync", "rsync -a src/ dst/", "alice",
                      io=iob(100.0, 100.0), duration_s=65.0)]
        if child:
            out.append(person("exit", t + 650, 502, 500, "cat", "cat f", user,
                              io=iob(5.0, 5.0), duration_s=40.0))
        out.append(person("exit", t + 700, 500, 1, "python", "python job.py", user,
                          io=iob(25.0 if child else 20.0, 25.0 if child else 20.0),
                          duration_s=100.0))
        return out

    def by_user(self, recs):
        return {u["user"]: u["rd_mb"]
                for u in window(recs).payload["io_process"]["by_user"]}

    def test_another_user_s_process_on_the_pid_keeps_its_bytes(self):
        self.assertEqual(self.by_user(self.recs("bob", child=False)),
                         {"alice": 107.0, "bob": 20.0})

    def test_a_later_process_of_the_same_user_keeps_its_bytes(self):
        # started ten minutes after the rsync did: none of what is held is its
        self.assertEqual(self.by_user(self.recs("alice", child=False)), {"alice": 127.0})

    def test_what_it_reaped_itself_is_still_taken_off(self):
        self.assertEqual(self.by_user(self.recs("bob")), {"alice": 107.0, "bob": 25.0})
        rows = {(r["actor3"], r["user"]): r["io_rd_mb"]
                for r in window(self.recs("bob")).payload["trajectories"]["rows"].values()}
        self.assertEqual(rows[("human", "bob")], 25.0)

    def test_the_live_tier_in_either_order(self):
        for older in (False, True):
            with self.subTest(older=older):
                lv, norm = LiveReducer(cfg(), CLASSES), Normalizer(cfg())
                recs = self.recs("bob")
                for r in (reversed(recs) if older else recs):
                    lv.feed(norm.normalize(dict(r)), older=older)
                self.assertEqual(rd_of(lv, self.now), {"alice": 107.0, "bob": 25.0})
                self.assertEqual({(e["pid"], e["user"]): e["rd_mb"] for e in lv.event_tail},
                                 {(500, "alice"): 7.0, (501, "alice"): 100.0,
                                  (502, "bob"): 5.0, (500, "bob"): 20.0})

    def live_rd(self, recs, older):
        lv, norm = LiveReducer(cfg(), CLASSES), Normalizer(cfg())
        for r in (reversed(recs) if older else recs):
            lv.feed(norm.normalize(dict(r)), older=older)
        return rd_of(lv, self.now)

    def test_the_same_user_s_next_holder_takes_off_what_it_reaped_and_no_more(self):
        # the rsync's bytes and the cat's wait under 500 for one user: the cat
        # began under the new holder, the rsync ten minutes before it
        recs = self.recs("alice")
        self.assertEqual(self.by_user(recs), {"alice": 132.0})
        for older in (False, True):
            with self.subTest(older=older):
                self.assertEqual(self.live_rd(recs, older), {"alice": 132.0})

    def test_an_orphan_still_running_under_the_next_holder_is_not_taken_off_it(self):
        # the rsync runs on until the new holder of 500 is running, and exits
        # before it: by its exit it is inside that holder's life, by its start not
        t = self.now - 1200
        recs = [person("exit", t, 500, 1, "bash", "bash sync.sh", "alice", io=iob(7.0, 7.0),
                       duration_s=60.0),
                person("exit", t + 700, 501, 500, "rsync", "rsync -a src/ dst/", "alice",
                       io=iob(100.0, 100.0), duration_s=705.0),
                person("exit", t + 800, 500, 1, "python", "python job.py", "alice",
                       io=iob(20.0, 20.0), duration_s=200.0)]
        self.assertEqual(self.by_user(recs), {"alice": 127.0})
        for older in (False, True):
            with self.subTest(older=older):
                self.assertEqual(self.live_rd(recs, older), {"alice": 127.0})

    def test_a_pid_holds_a_few_entries_and_the_closest_merge(self):
        # an earlier holder's orphan, then twelve children of the next holder ten
        # minutes apart: held to PER_PID entries, the orphan's still its own
        def ex(t, pid, ppid, dur, rd, user="alice"):
            return {"event": "exit", "host": "h1", "pid": pid, "ppid": ppid, "user": user,
                    "_ts_epoch": t, "duration_s": dur, "io": iob(rd, rd)}
        t = self.now - 20000
        j = own_io.OwnIo()
        j.take(ex(t + 60, 501, 500, 65.0, 100.0))            # began at t - 5
        n = own_io.PER_PID + 4
        for k in range(n):
            j.take(ex(t + 1000 + 600 * k + 30, 600 + k, 500, 30.0, 1.0))
        held = j.reaped[("h1", 500)]
        self.assertEqual(len(held), own_io.PER_PID)
        self.assertEqual(min(g[own_io._G_START] for g in held), t - 5)
        own = j.take(ex(t + 1000 + 600 * n, 500, 1, 700.0 + 600 * n, n + 2.0))
        self.assertEqual(own[0], 2.0)                         # its children off, not the rsync
        self.assertEqual((j.rejected, j.evicted), (1, 0))

    def test_a_child_forked_before_its_parent_s_last_exec_is_still_its(self):
        # `bash -lc` reads a profile, then execs the command: the record's start
        # is that exec, 30 s after the profile's own child began
        t = self.now - 600
        recs = [person("exit", t, 601, 600, "lmod", "lmod load x", "carol",
                       io=iob(3.0, 3.0), duration_s=2.0),
                person("exit", t + 300, 600, 1, "python", "python run.py", "carol",
                       io=iob(10.0, 10.0), duration_s=272.0)]
        self.assertEqual(self.by_user(recs), {"carol": 10.0})

    def launcher(self, before):
        """carol's `python` 600, its last exec at t + 28, exits at t + 300 with
        10 MB read; its child `lmod` 601, which read 3 MB, began `before`
        seconds ahead of that exec."""
        t = self.now - 600
        return [person("exit", t + 30 - before, 601, 600, "lmod", "lmod load x", "carol",
                       io=iob(3.0, 3.0), duration_s=2.0),
                person("exit", t + 300, 600, 1, "python", "python run.py", "carol",
                       io=iob(10.0, 10.0), duration_s=272.0)]

    def test_a_child_begun_just_inside_the_slack_is_taken_off(self):
        recs = self.launcher(own_io.START_SLACK_S - 1)
        p = window(recs).payload
        self.assertEqual({u["user"]: u["rd_mb"] for u in p["io_process"]["by_user"]},
                         {"carol": 10.0})
        self.assertEqual(p["io_process"]["own_io"]["rejected"], 0)
        for older in (False, True):
            with self.subTest(older=older):
                self.assertEqual(self.live_rd(recs, older), {"carol": 10.0})

    def test_a_child_begun_just_past_it_is_refused_and_counted(self):
        # A launcher that forks, runs on over a minute and then execs: the
        # record has no fork time apart from the exec, so the child is refused,
        # and its bytes count twice, at its exit and in the launcher's figure
        # (own_io.py, a known limit). The refusal is the signal.
        recs = self.launcher(own_io.START_SLACK_S + 1)
        p = window(recs).payload
        self.assertEqual({u["user"]: u["rd_mb"] for u in p["io_process"]["by_user"]},
                         {"carol": 13.0})
        for k in ("io_process", "tool_mix", "resources"):
            with self.subTest(panel=k):
                self.assertEqual(p[k]["own_io"]["rejected"], 1)
        self.assertEqual(p["trajectories"]["buffer"]["reaped_rejected"], 1)
        for older, signal in ((False, "rejected"), (True, "waiting")):
            with self.subTest(older=older):
                lv = live(recs, older=older)
                self.assertEqual(rd_of(lv, self.now), {"carol": 13.0})
                self.assertEqual(lv.result()["own_io"][signal], 1)


def shells(t, host="h1", base=0, n=5):
    """`n` tool-call shells, each the parent of one `cat` that read 1 MB: every
    shell's figure is its cat's, and its own is 0."""
    out = []
    for i in range(n):
        out.append(raw("exit", t + i, 1000 + base + i, 900 + base + i, "cat", "cat f",
                       host=host, agent_pid=900 + base + i, tree_root=100,
                       io=iob(1.0, 1.0)))
    return out + [raw("exit", t + 10 + i, 900 + base + i, None, "zsh", "zsh -c cat f",
                      host=host, agent_pid=900 + base + i, tree_root=100, is_agent=True,
                      io=iob(1.0, 1.0)) for i in range(n)]


JOINS = ("io_process", "tool_mix", "resources", "workspace", "trajectories")


def join_of(red):
    return red.own


def capped(n):
    """The default config with every join held to `n` parents."""
    c = cfg()
    c.set("reducers.own_io.max_parents", n, "test")
    return c


class JoinBoundsTest(unittest.TestCase):
    """What the join drops is counted, never silent: past a reducer's own cap,
    and past one pass's where a window puts the nodes' joins together. The cap
    (`reducers.own_io.max_parents`) set to 3 for both. What a join says on
    stderr when it drops one is caught, from a clean slate."""

    def setUp(self):
        self.err = self.enterContext(contextlib.redirect_stderr(io.StringIO()))
        self.enterContext(mock.patch.dict(own_io._SAID, clear=True))

    def said(self):
        return self.err.getvalue().splitlines()

    def test_the_reducer_s_own_cap(self):
        v = window(shells(time.time() - 900), c=capped(3))
        # five parents waiting, three held: the first two dropped, and their
        # cats' bytes count again at their shells' exits
        for k in JOINS:
            with self.subTest(reducer=k):
                self.assertEqual(join_of(v.reducers[k]).evicted, 2)
        # said once a panel, naming it and the setting
        self.assertEqual(sorted(ln.split()[2] for ln in self.said()), sorted(JOINS))
        for ln in self.said():
            self.assertIn("at reducers.own_io.max_parents (3), 1 so far", ln)
        p = v.payload
        self.assertEqual(p["io_process"]["by_class"]["agent"]["rd_mb"], 7.0)
        self.assertEqual(p["io_process"]["own_io"]["evicted"], 2)
        self.assertEqual(p["tool_mix"]["own_io"]["evicted"], 2)
        self.assertEqual(p["resources"]["own_io"]["evicted"], 2)
        self.assertEqual(p["workspace"]["own_io"]["evicted"], 2)
        self.assertEqual(p["trajectories"]["buffer"]["reaped_evicted"], 2)

    def test_one_pass_s_cap_where_a_window_puts_the_nodes_joins_together(self):
        c = capped(3)
        fresh = lambda: PanelView(c, CLASSES, 1440).fresh_reducers()   # noqa: E731
        kinds = {k: type(r) for k, r in fresh().items()}
        t = time.time() - 900
        carries, ages = {}, {}
        for n, host in enumerate(("h1", "h2")):
            red = hours.prepare(fresh())
            # each node: two shells' cats exited, the shells not yet
            recs = normalized(shells(t, host=host, base=10 * n, n=2)[:2])
            hours.feed_all(red, ((r["_ts_epoch"], r) for r in recs))
            carries[host], ages[host] = hours.age(kinds, hours.take_carry(red), {},
                                                  int(t // HOUR))
        acc = fresh()
        got = hours.combine_carries(carries, ages, acc)
        for k in JOINS:
            with self.subTest(reducer=k):
                self.assertEqual(len(got[k]["reaped"]), 3)
                self.assertEqual(join_of(acc[k]).evicted, 1)
        self.assertEqual(sorted(ln.split()[2] for ln in self.said()), sorted(JOINS))
        hours.install(acc, got)
        self.assertEqual(acc["io_process"].result()["own_io"]["evicted"], 1)

    def test_a_join_that_drops_a_parent_says_so_then_at_most_hourly(self):
        clock = [5000.0]
        self.enterContext(mock.patch.object(own_io, "_now", lambda: clock[0]))

        def ex(t, pid, ppid):
            return {"event": "exit", "host": "h1", "pid": pid, "ppid": ppid, "user": "alice",
                    "_ts_epoch": t, "duration_s": 1.0, "io": iob(1.0, 1.0)}
        t = time.time() - 600
        j = own_io.OwnIo(own_io.DISK, cap=1, name="io_process")
        for k in range(3):                      # two parents dropped: said once
            j.take(ex(t + k, 100 + k, 10 + k))
        clock[0] += own_io.SAY_EVERY_S - 1       # within the hour: not again
        j.take(ex(t + 3, 103, 13))
        clock[0] += 1                            # an hour on: said again
        j.take(ex(t + 4, 104, 14))
        self.assertEqual(j.evicted, 4)
        said = self.said()
        self.assertEqual(len(said), 2)
        self.assertTrue(said[0].startswith("warning: own_io: io_process dropped 1 parent "))
        self.assertIn("at reducers.own_io.max_parents (1), 1 so far", said[0])
        self.assertIn("(1), 4 so far", said[1])
        # another panel's join says so on its own clock
        o = own_io.OwnIo(own_io.DISK, cap=1, name="tool_mix")
        o.take(ex(t, 100, 10))
        o.take(ex(t + 1, 101, 11))
        self.assertEqual([ln.split()[2] for ln in self.said()],
                         ["io_process", "io_process", "tool_mix"])

    def test_the_parent_given_to_least_recently_goes_first(self):
        # an agent at work, a shell that has stopped reaping, then a third
        # parent past a cap of two: the shell goes, and the agent's exit still
        # takes both its tool calls off
        def ex(t, pid, ppid, rd):
            return {"event": "exit", "host": "h1", "pid": pid, "ppid": ppid, "user": "alice",
                    "_ts_epoch": t, "duration_s": 1.0, "io": iob(rd, rd)}
        t = time.time() - 600
        j = own_io.OwnIo(own_io.DISK, cap=2)
        j.take(ex(t, 11, 10, 1.0))
        j.take(ex(t + 1, 21, 20, 1.0))
        j.take(ex(t + 2, 12, 10, 1.0))
        j.take(ex(t + 3, 31, 30, 1.0))
        self.assertEqual(list(j.reaped), [("h1", 10), ("h1", 30)])
        self.assertEqual(j.evicted, 1)
        self.assertEqual(j.take(ex(t + 4, 10, 1, 5.0)), (3.0, 0.0))

    def test_a_pid_crowded_past_its_entries_is_not_the_cap(self):
        # nine users' children under root's cron 1: a pid holds PER_PID groups
        # and no two of one user merge, so the earliest begun is dropped --
        # counted apart, not said, and not in the cap's count the warning gives
        def ex(t, pid, ppid, user):
            return {"event": "exit", "host": "h1", "pid": pid, "ppid": ppid, "user": user,
                    "_ts_epoch": t, "duration_s": 1.0, "io": iob(1.0, 1.0)}
        t = time.time() - 600
        j = own_io.OwnIo(own_io.DISK, cap=2, name="io_process")
        for k in range(own_io.PER_PID + 1):
            j.take(ex(t + k, 100 + k, 1, "user%d" % k))
        self.assertEqual((j.crowded, j.evicted, self.said()), (1, 0, []))
        j.take(ex(t + 20, 200, 2, "alice"))
        j.take(ex(t + 21, 300, 3, "alice"))
        self.assertEqual(j.status(), {"evicted": 1, "rejected": 0, "clamped": 0, "crowded": 1})
        self.assertEqual(len(self.said()), 1)
        self.assertIn("at reducers.own_io.max_parents (2), 1 so far", self.said()[0])

    def test_an_agent_runtime_and_what_started_it_are_passed_over_at_the_cap(self):
        # claude 100, started from alice's login bash 50, makes a tool call and
        # sits idle while three other parents reap past a cap of two: they go,
        # it stays; and so, once it has exited, does the bash
        def ex(t, pid, ppid, rd, dur=1.0, root=None):
            return {"event": "exit", "host": "h1", "pid": pid, "ppid": ppid, "user": "alice",
                    "_ts_epoch": t, "duration_s": dur, "io": iob(rd, rd),
                    "agent_pid": pid if root else None, "tree_root_pid": root}
        t = time.time() - 600
        j = own_io.OwnIo(own_io.DISK, cap=2)
        j.take(ex(t, 110, 100, 3.0, root=100))
        for k in range(3):
            j.take(ex(t + 1 + k, 201 + k, 200 + k, 1.0))
        self.assertIn(("h1", 100), j.reaped)
        self.assertEqual(j.take(ex(t + 10, 100, 50, 5.0, dur=20.0, root=100)), (2.0, 0.0))
        for k in range(3):
            j.take(ex(t + 11 + k, 301 + k, 300 + k, 1.0))
        self.assertIn(("h1", 50), j.reaped)
        self.assertEqual(j.take(ex(t + 20, 50, 1, 6.0, dur=3600.0)), (1.0, 0.0))
        self.assertEqual(j.evicted, 5)

    def test_so_are_they_where_a_window_puts_the_nodes_joins_together(self):
        # h1 holds claude 100's tool call from the hour before; h2 two shells
        # of another agent, this hour: past one pass's cap of two, a shell goes
        c = capped(2)
        fresh = lambda: PanelView(c, CLASSES, 1440).fresh_reducers()   # noqa: E731
        kinds = {k: type(r) for k, r in fresh().items()}
        t = time.time() - 900
        h = int(t // HOUR)
        by_node = {
            "h1": (h - 1, [raw("exit", t - HOUR, 110, 100, "zsh", "zsh -c ls", host="h1",
                               agent_pid=100, tree_root=100, io=iob(3.0, 3.0))]),
            "h2": (h, [raw("exit", t + k, 311 + 10 * k, 310 + 10 * k, "cat", "cat f",
                           host="h2", agent_pid=900, tree_root=900, depth=2,
                           io=iob(1.0, 1.0)) for k in range(2)])}
        carries, ages = {}, {}
        for host, (hour, recs) in by_node.items():
            red = hours.prepare(fresh())
            recs = normalized(recs)
            hours.feed_all(red, ((r["_ts_epoch"], r) for r in recs))
            carries[host], ages[host] = hours.age(kinds, hours.take_carry(red), {}, hour)
        acc = fresh()
        got = hours.combine_carries(carries, ages, acc)
        for k in JOINS:
            with self.subTest(reducer=k):
                self.assertEqual(list(got[k]["reaped"]), [("h1", 100), ("h2", 320)])
                self.assertEqual(join_of(acc[k]).evicted, 1)

    def test_and_so_is_an_agent_runtime_s_note_in_the_live_tier_s_backfill(self):
        # newest first: claude 100 is noted, then three of bob's processes past
        # a cap of two notes; its tool call, met last, still finds it
        self.enterContext(mock.patch.object(livemod, "BOOKED_MAX", 2))
        now = time.time()
        t = now - 900
        recs = [raw("exit", t + 10, 110, 100, "zsh", "zsh -c ls", agent_pid=100,
                    tree_root=100, io=iob(3.0, 3.0), duration_s=5.0)]
        recs += [person("exit", t + 20 + k, 400 + k, 300 + k, "sleep", "sleep 1", "bob",
                        io=iob(1.0, 1.0)) for k in range(3)]
        recs.append(raw("exit", t + 100, 100, 50, "claude", "claude", is_agent=True,
                        agent_pid=100, tree_root=100, io=iob(5.0, 5.0), duration_s=200.0))
        lv = live(list(reversed(recs)), older=True)
        # bob's three notes and the tool call's own dropped in turn, not claude's
        self.assertEqual(lv.io_dropped, 3)
        self.assertEqual(rd_of(lv, now)["alice"], 5.0)

    def test_runtimes_idle_past_any_look_ahead_still_keep_their_place(self):
        # seventy agent runtimes, each holding a tool call, then idle -- the least
        # recently given of all -- and eighty plain parents given to after them:
        # one more plain parent past the cap drops the oldest plain one, never a
        # runtime; with only runtimes held, the least recently given goes
        n = 70

        def ex(t, pid, ppid, root=None):
            return {"event": "exit", "host": "h1", "pid": pid, "ppid": ppid, "user": "alice",
                    "_ts_epoch": t, "duration_s": 1.0, "io": iob(1.0, 1.0),
                    "tree_root_pid": root}
        t = time.time() - 600
        j = own_io.OwnIo(own_io.DISK, cap=150)
        for k in range(n):
            j.take(ex(t, 10000 + k, 1000 + k, root=1000 + k))
        for k in range(81):
            j.take(ex(t + 1, 20000 + k, 2000 + k))
        self.assertEqual((j.evicted, ("h1", 2000) in j.reaped), (1, False))
        self.assertEqual([k for k in range(n) if ("h1", 1000 + k) not in j.reaped], [])
        r = own_io.OwnIo(own_io.DISK, cap=n)
        for k in range(n + 1):
            r.take(ex(t + k, 10000 + k, 1000 + k, root=1000 + k))
        self.assertEqual((len(r.reaped), ("h1", 1000) in r.reaped, r.evicted), (n, False, 1))

    def test_past_half_the_cap_runtimes_give_way(self):
        # runtimes that never exit -- a login shell the feed never sees end --
        # would crowd every other parent out: past half the cap the least
        # recently given of them goes first
        def ex(t, pid, ppid, root=None):
            return {"event": "exit", "host": "h1", "pid": pid, "ppid": ppid, "user": "alice",
                    "_ts_epoch": t, "duration_s": 1.0, "io": iob(1.0, 1.0),
                    "tree_root_pid": root}
        t = time.time() - 600
        j = own_io.OwnIo(own_io.DISK, cap=10)
        for k in range(6):
            j.take(ex(t, 10000 + k, 1000 + k, root=1000 + k))
        for k in range(5):
            j.take(ex(t + 1 + k, 20000 + k, 2000 + k))
        self.assertEqual((("h1", 1000) in j.reaped, ("h1", 2000) in j.reaped), (False, True))
        j.take(ex(t + 9, 20009, 2009))
        self.assertEqual((("h1", 1001) in j.reaped, ("h1", 2000) in j.reaped), (True, False))

    def test_and_so_do_runtimes_notes_in_the_live_tier_s_backfill(self):
        # newest first: seventy agent runtimes' exits noted, then eighty-one of
        # bob's processes past BOOKED_MAX: bob's oldest note goes, every
        # runtime's stays
        n = 70
        self.enterContext(mock.patch.object(livemod, "BOOKED_MAX", 150))
        t = time.time() - 900
        recs = [raw("exit", t + 100 - 0.01 * k, 1000 + k, 50, "claude", "claude",
                    is_agent=True, agent_pid=1000 + k, tree_root=1000 + k,
                    io=iob(5.0, 5.0), duration_s=200.0) for k in range(n)]
        recs += [person("exit", t + 20 - 0.01 * k, 400 + k, 300 + k, "sleep", "sleep 1", "bob",
                        io=iob(1.0, 1.0)) for k in range(81)]
        lv = live(recs, older=True)
        self.assertEqual((lv.io_dropped, ("h1", 400) in lv.io_booked), (1, False))
        self.assertEqual([k for k in range(n) if ("h1", 1000 + k) not in lv.io_booked], [])

    def test_every_join_is_held_to_the_setting(self):
        self.assertEqual(own_io.cap_of(cfg()), own_io.REAPED_MAX)
        self.assertEqual(own_io.OwnIo().cap, own_io.REAPED_MAX)
        c = capped(7)
        red = PanelView(c, CLASSES, 60).fresh_reducers()
        for k in JOINS:
            with self.subTest(reducer=k):
                self.assertEqual((join_of(red[k]).cap, join_of(red[k]).name), (7, k))
                self.assertEqual(red[k].carry_caps()["reaped"], 7)
        lv = LiveReducer(c, CLASSES)
        self.assertEqual([(j.cap, j.name) for j in (lv.io_join, lv.io_given)],
                         [(7, "live"), (7, "live")])
        own = TreeBuilder(own_cap=own_io.cap_of(c)).own
        self.assertEqual((own.cap, own.name), (7, "inspector"))


class SettingTest(unittest.TestCase):
    """`reducers.own_io.max_parents` is a count of one or more: 0 is neither
    "unbounded" nor "no join", and the service refuses it at start rather than
    run with the default."""

    def load(self, value):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "dashboard.config.json")
            with open(path, "w") as fh:
                json.dump({"reducers": {"own_io": {"max_parents": value}}}, fh)
            return configmod.load(None, path)

    def test_below_one_or_not_a_count_is_refused_at_start(self):
        for bad in (0, -1, 1.5, True, "600"):
            with self.subTest(value=bad):
                with self.assertRaises(SystemExit) as cm:
                    self.load(bad)
                self.assertIn("reducers.own_io.max_parents: %r is not a count of parents, "
                              "1 or more" % (bad,), str(cm.exception))

    def test_null_is_the_default_and_one_is_one(self):
        self.assertEqual(self.load(None).get("reducers.own_io.max_parents"), own_io.REAPED_MAX)
        self.assertEqual(own_io.cap_of(self.load(1)), 1)

    def test_a_join_refuses_it_too(self):
        c = cfg()
        c.set("reducers.own_io.max_parents", 0, "test")
        with self.assertRaises(ValueError):
            own_io.cap_of(c)
        with self.assertRaises(ValueError):
            own_io.OwnIo(cap=0)
        c.set("reducers.own_io.max_parents", None, "test")
        self.assertEqual(own_io.cap_of(c), own_io.REAPED_MAX)


class ClampTest(unittest.TestCase):
    """A figure short of what its children carried shows as 0, never below, and
    is counted; short by the collector's rounding alone, it is not."""

    def setUp(self):
        t = time.time() - 900
        kw = dict(agent_pid=100, tree_root=100)
        self.recs = [
            raw("exit", t, 111, 110, "cat", "cat big", io=iob(10.0, 10.0), **kw),
            raw("exit", t + 1, 110, 100, "zsh", "zsh -c cat big", io=iob(4.0, 10.5), **kw),
            raw("exit", t + 2, 121, 120, "cat", "cat f", io=iob(2.0, 2.0), **kw),
            raw("exit", t + 3, 120, 100, "zsh", "zsh -c cat f", io=iob(1.996, 2.0), **kw)]

    def test_the_panels(self):
        p = window(self.recs).payload
        self.assertEqual(p["io_process"]["by_class"]["agent"]["rd_mb"], 12.0)
        for k in ("io_process", "tool_mix", "resources", "workspace"):
            with self.subTest(panel=k):
                self.assertEqual(p[k]["own_io"]["clamped"], 1)
        self.assertEqual(p["trajectories"]["buffer"]["reaped_clamped"], 1)

    def test_the_live_tier_in_either_order(self):
        for older in (False, True):
            with self.subTest(older=older):
                lv, norm = LiveReducer(cfg(), CLASSES), Normalizer(cfg())
                for r in (reversed(self.recs) if older else self.recs):
                    lv.feed(norm.normalize(dict(r)), older=older)
                self.assertEqual(lv.result()["own_io"]["clamped"], 1)
                self.assertEqual({e["pid"]: e["rd_mb"] for e in lv.event_tail},
                                 {111: 10.0, 110: 0.0, 121: 2.0, 120: 0.0})


class LookupTest(FeedCase):
    """The commands behind a number: the same own bytes, and exits only."""

    def lookup(self, c):
        t = self.now - 600
        recs = family(t) + [
            person("truncated", t + 200, 300, 250, "vim", "vim notes.txt", "alice",
                   io=dict(iob(50.0, 50.0), scope="proc"), cpu_s=40.0)]
        self.append("h1.jsonl", recs)
        io = iocmds.IoCommands(c, lambda: [self.root])
        f = parse_filters({})
        f.pop("q", None)
        f["event"] = None
        j = io.start(f, 60)
        for _ in range(200):
            if j.state in ("done", "error"):
                break
            time.sleep(0.02)
        self.assertEqual(j.state, "done", j.error)
        return j

    def test_its_join_is_held_to_the_setting(self):
        c = self.cfg()
        c.set("reducers.own_io.max_parents", 7, "test")
        with mock.patch.object(iocmds, "OwnIo", side_effect=own_io.OwnIo) as made:
            self.lookup(c)
        self.assertEqual([(k.get("cap"), k.get("name")) for _a, k in made.call_args_list],
                         [(7, "lookup")])

    def test_a_lookup_sums_what_the_panels_sum(self):
        j = self.lookup(self.cfg())
        self.assertEqual((j.totals["n"], j.totals["rd_mb"], j.totals["rchar_mb"]),
                         (4, 1006.0, 1310.0))
        rows = {r["cmd"]: r["rd_mb"] for r in j.page("rd_mb", True, 0, 100)}
        self.assertEqual(rows, {"cat big.bin": 1000.0, "zsh -c cat big.bin": 0.0,
                                "claude": 5.0, "-bash": 1.0})


if __name__ == "__main__":
    unittest.main()
