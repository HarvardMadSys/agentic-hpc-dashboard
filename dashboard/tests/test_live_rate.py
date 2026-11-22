"""Live rate: a quiet class in a minute the collector wrote is a measured 0;
only a minute with no record at all is a hole.

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
        return default


CLASSES = ["agent", "human", "ide", "unlabeled"]


def exit_rec(at, cls):
    return {"event": "exit", "_a3": cls, "_ts_epoch": at, "host": "h1",
            "user": "ann", "agent_type": "claude_code" if cls == "agent" else None,
            "cpu_s": 0.1, "args": "true"}


class LiveRateTest(unittest.TestCase):
    def setUp(self):
        self.now = time.time()
        m = int(self.now // 60) * 60             # start of the minute in progress
        self.last_full, self.empty, self.older = m - 60, m - 120, m - 180
        self.r = LiveReducer(Cfg(), CLASSES)

    def feed(self, minute, cls, n):
        for _ in range(n):
            self.r.feed(exit_rec(minute + 30, cls))

    def rows(self):
        out = self.r.result(now=self.now, window_s=600)
        return {row["epoch"]: row for row in out["rate"]}

    def test_quiet_class_in_a_minute_with_records_reads_zero(self):
        self.feed(self.last_full, "agent", 3)
        self.feed(self.older, "human", 2)
        rate = self.rows()
        self.assertEqual(rate[self.last_full]["agent"], 3)
        self.assertEqual(rate[self.last_full]["human"], 0)
        self.assertEqual(rate[self.last_full]["ide"], 0)
        self.assertEqual(rate[self.older]["human"], 2)
        self.assertEqual(rate[self.older]["agent"], 0)

    def test_minute_with_no_record_is_still_a_hole(self):
        self.feed(self.last_full, "agent", 3)
        self.feed(self.older, "human", 2)
        rate = self.rows()
        self.assertEqual({c: rate[self.empty][c] for c in CLASSES},
                         dict.fromkeys(CLASSES))


if __name__ == "__main__":
    unittest.main()
