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
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from rc_dashboard import iocmds                                # noqa: E402
from rc_dashboard.export import parse_filters                  # noqa: E402
from rc_dashboard.normalize import Normalizer                  # noqa: E402
from rc_dashboard.proctree import TreeBuilder                  # noqa: E402
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


def window(recs, minutes=1440):
    """`recs` normalised and fed to a window's reducer set, as a build feeds it.
    Returns the view, with its panels materialised."""
    c = cfg()
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


def live(recs, older=False):
    """The live tier fed `recs` in the order given; `older` as the backfill feeds."""
    c = cfg()
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

    def test_a_child_forked_before_its_parent_s_last_exec_is_still_its(self):
        # `bash -lc` reads a profile, then execs the command: the record's start
        # is that exec, 30 s after the profile's own child began
        t = self.now - 600
        recs = [person("exit", t, 601, 600, "lmod", "lmod load x", "carol",
                       io=iob(3.0, 3.0), duration_s=2.0),
                person("exit", t + 300, 600, 1, "python", "python run.py", "carol",
                       io=iob(10.0, 10.0), duration_s=272.0)]
        self.assertEqual(self.by_user(recs), {"carol": 10.0})


class LookupTest(FeedCase):
    """The commands behind a number: the same own bytes, and exits only."""

    def test_a_lookup_sums_what_the_panels_sum(self):
        t = self.now - 600
        recs = family(t) + [
            person("truncated", t + 200, 300, 250, "vim", "vim notes.txt", "alice",
                   io=dict(iob(50.0, 50.0), scope="proc"), cpu_s=40.0)]
        self.append("h1.jsonl", recs)
        io = iocmds.IoCommands(self.cfg(), lambda: [self.root])
        f = parse_filters({})
        f.pop("q", None)
        f["event"] = None
        j = io.start(f, 60)
        for _ in range(200):
            if j.state in ("done", "error"):
                break
            time.sleep(0.02)
        self.assertEqual(j.state, "done", j.error)
        self.assertEqual((j.totals["n"], j.totals["rd_mb"], j.totals["rchar_mb"]),
                         (4, 1006.0, 1310.0))
        rows = {r["cmd"]: r["rd_mb"] for r in j.page("rd_mb", True, 0, 100)}
        self.assertEqual(rows, {"cat big.bin": 1000.0, "zsh -c cat big.bin": 0.0,
                                "claude": 5.0, "-bash": 1.0})


if __name__ == "__main__":
    unittest.main()
