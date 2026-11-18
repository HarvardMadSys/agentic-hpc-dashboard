"""Each process counted once, at its exit.

How the per-class totals counted one process's resources twice:

* A process alive across a collector stop. At each stop the collector writes a
  `truncated` record for every live process it tracks, with its whole life's
  CPU and I/O so far; after the restart it tracks the process again, and its
  exit carries its whole life's totals again. Summed as both came, one `vim`
  that ran 3605 CPU-s and read 505 MB was 7205 CPU-s and 1005 MB, two
  processes. A sum takes the exit only.

    cd dashboard && .venv/bin/python -m unittest tests.test_count_once -q
"""
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from rc_dashboard.normalize import Normalizer                  # noqa: E402
from rc_dashboard.reducers.live import LiveReducer             # noqa: E402
from rc_dashboard.windows import PanelView                     # noqa: E402
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


if __name__ == "__main__":
    unittest.main()
