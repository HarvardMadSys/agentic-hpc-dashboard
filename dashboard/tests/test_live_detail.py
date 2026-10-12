"""A user opened up: running sessions' memory and version, the last N exited
processes however old, and each account's I/O, running processes, and GPU ask.

    cd dashboard && .venv/bin/python -m unittest discover -s tests -q
"""
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rc_dashboard.reducers.live import LiveReducer, agent_version   # noqa: E402

CLASSES = ["agent", "human", "ide"]


class Cfg:
    def __init__(self, **kw):
        self.kw = kw

    def get(self, dotted, default=None):
        return self.kw.get(dotted, default)


def exit_rec(at, user, cls="agent", agent_type="claude_code", cpu=1.0, rd=None, wr=None,
             tx=None, rx=None, host="h1", args="ls", code=0):
    r = {"event": "exit", "_a3": cls, "_ts_epoch": at, "ts": "t", "host": host,
         "user": user, "agent_type": agent_type, "cpu_s": cpu, "args": args,
         "exit_code": code}
    if rd is not None or wr is not None:
        r["io"] = {"rd_mb": rd, "wr_mb": wr}
    if tx is not None:
        r["net_tx_bytes"] = tx
    if rx is not None:
        r["net_rx_bytes"] = rx
    return r


def tick(at, user, pid, rss, uuid="u1", comm="claude", args="claude", n=3, sid="h1|d|agent|apid:1"):
    return {"event": "residency", "_a3": "agent", "_ts_epoch": at, "host": "h1",
            "user": user, "agent_type": "claude_code", "tree_root_pid": pid,
            "n_procs": n, "rss_mb": rss, "session_uuid": uuid, "comm": comm, "args": args,
            "_sid": sid}


class VersionTest(unittest.TestCase):

    def test_a_version_is_read_where_the_program_names_it_and_nowhere_else(self):
        self.assertEqual(agent_version("2.1.295-8b2928c", ""), "2.1.295")
        self.assertEqual(agent_version("claude", "/home/u/.local/share/claude/versions/2.1.291 --print"), "2.1.291")
        self.assertEqual(agent_version("2.1.29", "/home/u/.claude/remote/ccd-cli/2.1.295-8b2928c330c4 --x"), "2.1.29")
        self.assertEqual(agent_version("codex", "/h/.codex/packages/app-server-daemon/releases/0.162.1-x86_64-unknown-linux-musl/bin/codex app-server"), "0.162.1")
        self.assertIsNone(agent_version("claude", "claude --rc"))
        self.assertIsNone(agent_version("node", "node /h/.nvm/versions/node/v22.22.0/bin/codex --version"))


class SessionMemoryTest(unittest.TestCase):

    def setUp(self):
        self.now = time.time()
        self.r = LiveReducer(Cfg(), CLASSES)

    def session(self):
        return self.r.user_detail("ann", now=self.now, window_s=3600)["sessions"][0]

    def test_peak_and_mean_cover_every_tick_not_just_the_newest(self):
        for i, rss in enumerate((100.0, 400.0, 200.0)):
            self.r.feed(tick(self.now - 300 + 60 * i, "ann", 7, rss))
        s = self.session()
        self.assertEqual((s["rss_mb"], s["rss_peak_mb"], s["rss_mean_mb"]), (200.0, 400.0, 233.3))
        self.assertEqual(s["rss_ticks"], 3)

    def test_an_older_tick_read_after_a_newer_one_still_counts(self):
        # the backfill reads newest first
        self.r.feed(tick(self.now - 60, "ann", 7, 100.0))
        self.r.feed(tick(self.now - 120, "ann", 7, 900.0), older=True)
        s = self.session()
        self.assertEqual((s["rss_mb"], s["rss_peak_mb"]), (100.0, 900.0))

    def test_a_reused_pid_starts_a_fresh_history(self):
        self.r.feed(tick(self.now - 120, "ann", 7, 900.0, uuid="old"))
        self.r.feed(tick(self.now - 60, "ann", 7, 50.0, uuid="new"))
        self.assertEqual(self.session()["rss_peak_mb"], 50.0)

    def test_a_session_names_its_trajectory_and_version(self):
        self.r.feed(tick(self.now - 60, "ann", 7, 10.0, comm="2.1.295-8b2928c",
                         sid="h1|2026-10-12|agent|apid:7"))
        s = self.session()
        self.assertEqual((s["sid"], s["version"]), ("h1|2026-10-12|agent|apid:7", "2.1.295"))


class RecentTest(unittest.TestCase):

    def test_the_last_n_are_kept_per_user_however_old_and_however_busy_the_node(self):
        now = time.time()
        r = LiveReducer(Cfg(**{"live.event_tail": 5, "live.user_tail": 50}), CLASSES)
        for i in range(10):
            r.feed(exit_rec(now - 7200 + i, "quiet", args="echo %d" % i))
        for i in range(100):                       # a busy neighbour floods the fleet tail
            r.feed(exit_rec(now - 60 + i * 0.1, "busy"))
        d = r.user_detail("quiet", now=now, window_s=600, recent=4)
        self.assertEqual([c["args"] for c in d["recent"]], ["echo 9", "echo 8", "echo 7", "echo 6"])
        self.assertEqual(d["recent_held"], 10)
        self.assertEqual(len(r.user_detail("busy", now=now, recent=1000)["recent"]), 50)

    def test_rows_carry_host_io_and_the_exit_code(self):
        now = time.time()
        r = LiveReducer(Cfg(), CLASSES)
        r.feed(exit_rec(now - 5, "ann", rd=1.5, wr=0.5, tx=2e6, rx=1e6, host="h9", code=2))
        c = r.user_detail("ann", now=now)["recent"][0]
        self.assertEqual((c["host"], c["rd_mb"], c["wr_mb"], c["net_tx_mb"], c["net_rx_mb"], c["exit_code"]),
                         ("h9", 1.5, 0.5, 2.0, 1.0, 2))


class RowTest(unittest.TestCase):

    def setUp(self):
        self.now = time.time()
        self.r = LiveReducer(Cfg(), CLASSES)
        for rec in [
            exit_rec(self.now - 60, "ann", rd=10.0, wr=1.0, tx=3e6, rx=1e6),
            exit_rec(self.now - 60, "ann", agent_type="codex", rd=2.0, cpu=4.0),
            exit_rec(self.now - 60, "ann", cls="human", agent_type=None, wr=5.0),
            tick(self.now - 30, "ann", 7, 10.0, n=4),
        ]:
            self.r.feed(rec)
        self.r.feed({"event": "submit", "_a3": "agent", "_ts_epoch": self.now - 100, "ts": "t",
                     "user": "ann", "gpus": 2, "time_limit_s": 7200, "args": "sbatch x"})
        self.r.feed({"event": "submit", "_a3": "agent", "_ts_epoch": self.now - 100, "ts": "t",
                     "user": "ann", "gpus": 1, "time_limit_s": None, "args": "sbatch y"})

    def row(self):
        return next(x for x in self.r.users(now=self.now, window_s=3600)["rows"] if x["user"] == "ann")

    def test_an_account_carries_its_io_running_processes_and_gpu_ask(self):
        a = self.row()
        self.assertEqual((a["rd_mb"], a["wr_mb"], a["net_mb"]), (12.0, 6.0, 4.0))
        self.assertEqual(a["running"], {"agent": 4})
        self.assertEqual(a["gpu_h"], 4.0)            # 2 GPUs x 2 h; the one with no limit is not guessed

    def test_each_agent_type_has_its_own_row_under_its_class(self):
        d = self.row()["type_detail"]["agent"]
        self.assertEqual(d["claude_code"]["n"], 1)
        self.assertEqual((d["codex"]["n"], d["codex"]["cpu_s"], d["codex"]["rd_mb"]), (1, 4.0, 2.0))


if __name__ == "__main__":
    unittest.main()
