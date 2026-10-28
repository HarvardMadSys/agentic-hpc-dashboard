"""The Trajectories plates: every session in the window, real sessions only.

    cd dashboard && .venv/bin/python -m unittest discover -s tests -q
"""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_sessions import HOME, claude_session, rec, reduce   # noqa: E402

PROBE = HOME + "/.codex/packages/app-server-daemon/current/bin/codex --version"


class SummaryTest(unittest.TestCase):

    def setUp(self):
        t0 = time.time() - 600
        recs = claude_session(t0) + [
            # a tool call that only polls the process table
            rec("exit", t0 + 12, 202, 200, "ps", "ps aux", agent_pid=200, tree_root=100),
        ]
        # a status-bar script probing codex every minute: no agent at its root
        for i in range(3):
            recs.append(rec("exit", t0 + 30 + 60 * i, 900 + i, 1, "codex", PROBE,
                            agent_pid=900 + i, tree_root=900 + i, is_agent=True,
                            agent_type="codex"))
        self.s = reduce(recs)["trajectories"]["summary"]["agent"]

    def test_a_probe_is_set_aside_not_counted_as_a_session(self):
        self.assertEqual(self.s["sessions"], 1)
        self.assertEqual(self.s["probes"], 3)

    def test_waiting_is_the_share_of_tool_calls_that_only_poll(self):
        self.assertGreater(self.s["wait_pct"], 0)
        self.assertAlmostEqual(self.s["wait_pct"], round(100.0 / self.s["tool_calls"], 2))

    def test_length_and_size_come_from_the_real_session(self):
        self.assertEqual(self.s["calls_p50"], self.s["tool_calls"])
        self.assertEqual(self.s["longest"]["user"], "alice")
        self.assertGreater(self.s["longest"]["wall_s"], 0)
        self.assertEqual(self.s["top_user_share_pct"], 100.0)


if __name__ == "__main__":
    unittest.main()
