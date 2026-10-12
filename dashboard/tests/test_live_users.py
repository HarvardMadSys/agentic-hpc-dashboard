"""Live user roster: one row per account, however many classes it shows up in.

    cd dashboard && .venv/bin/python -m unittest discover -s tests -q
"""
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rc_dashboard.reducers.live import LiveReducer                  # noqa: E402


class Cfg:
    def get(self, dotted, default=None):
        return {"live.user_rows": 2}.get(dotted, default)


CLASSES = ["agent", "human", "ide"]


def exit_rec(at, user, cls="agent", agent_type="claude_code", cpu=1.0):
    return {"event": "exit", "_a3": cls, "_ts_epoch": at, "host": "h1",
            "user": user, "agent_type": agent_type, "cpu_s": cpu,
            "args": f"run --token=sk-ant-{'x' * 30}"}


def tree(at, user, pid, cls="agent", agent_type="claude_code"):
    return {"event": "residency", "_a3": cls, "_ts_epoch": at, "host": "h1",
            "user": user, "agent_type": agent_type, "tree_root_pid": pid,
            "n_procs": 5}


class UserRosterTest(unittest.TestCase):
    def setUp(self):
        self.now = time.time()
        self.r = LiveReducer(Cfg(), CLASSES)
        for rec in [
            exit_rec(self.now - 60, "ann"),
            exit_rec(self.now - 60, "ann", agent_type="codex"),
            exit_rec(self.now - 60, "ann", cls="human", agent_type=None),
            exit_rec(self.now - 120, "bob", cls="ide", agent_type="vscode"),
            exit_rec(self.now - 120, "cy", cls="human", agent_type=None),
            exit_rec(self.now - 30, "cy", cls="human", agent_type=None),
            tree(self.now - 30, "ann", 10),
            tree(self.now - 30, "ann", 11, agent_type="codex"),
            tree(self.now - 30, "bob", 12, cls="ide", agent_type="vscode"),
            tree(self.now - 3600, "dee", 13),          # stale: not running now
        ]:
            self.r.feed(rec)

    def test_one_row_per_account_across_classes(self):
        u = self.r.users(now=self.now, window_s=3600, limit=10)
        self.assertEqual(u["total"], 3)              # ann, bob, cy; not dee
        ann = next(r for r in u["rows"] if r["user"] == "ann")
        self.assertEqual(ann["n"], 3)
        self.assertEqual(set(ann["by_class"]), {"agent", "human"})
        self.assertEqual(ann["types"]["agent"], {"claude_code": 1, "codex": 1})
        self.assertEqual(ann["live"]["agent"], {"claude_code": 1, "codex": 1})

    def test_by_class_counts_people_not_sessions(self):
        u = self.r.users(now=self.now, window_s=3600)
        self.assertEqual(u["by_class"], {"agent": 1, "human": 2, "ide": 1})
        self.assertEqual(u["with_live_agent"], 1)

    def test_running_agents_sort_first_and_rows_are_capped(self):
        u = self.r.users(now=self.now, window_s=3600)
        self.assertEqual([r["user"] for r in u["rows"]], ["ann", "cy"])
        self.assertEqual(u["rows_total"], 3)

    def test_search_reaches_past_the_cap(self):
        u = self.r.users(now=self.now, window_s=3600, q="BO")
        self.assertEqual([r["user"] for r in u["rows"]], ["bob"])

    def test_running_filter_reaches_every_account(self):
        u = self.r.users(now=self.now, window_s=3600, running="ide:vscode")
        self.assertEqual([r["user"] for r in u["rows"]], ["bob"])

    def test_running_by_type_counts_people(self):
        u = self.r.users(now=self.now, window_s=3600)
        self.assertEqual(u["running_by_type"],
                         {"agent": {"claude_code": 1, "codex": 1}, "ide": {"vscode": 1}})

    def test_frame_carries_the_roster(self):
        out = self.r.result(now=self.now, window_s=3600)
        self.assertEqual(out["users"]["total"], 3)

    def test_detail_lists_sessions_and_masks_commands(self):
        d = self.r.user_detail("ann", now=self.now, window_s=3600)
        self.assertEqual(len(d["sessions"]), 2)
        self.assertEqual(len(d["recent"]), 3)
        for e in d["recent"]:
            self.assertNotIn("sk-ant-", e["args"])
            self.assertNotIn("_argv", e)
        self.assertEqual(d["row"]["user"], "ann")

    def test_unknown_user_has_no_row(self):
        d = self.r.user_detail("zed", now=self.now, window_s=3600)
        self.assertIsNone(d["row"])
        self.assertEqual(d["sessions"], [])


if __name__ == "__main__":
    unittest.main()
