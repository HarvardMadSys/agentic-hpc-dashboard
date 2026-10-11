"""I/O reducer tests: reads and writes are reported, and ranked, separately.

    cd dashboard && .venv/bin/python -m unittest discover -s tests -q
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rc_dashboard.reducers.io import IoReducer     # noqa: E402


class Cfg:
    def __init__(self, tree):
        self.tree = tree

    def get(self, dotted, default=None):
        return self.tree.get(dotted, default)


def exit_rec(tool, user, rchar=0.0, rd=0.0, wchar=0.0, wr=0.0, tx=None, rx=None):
    return {"event": "exit", "_a3": "agent", "_eff": tool, "user": user,
            "io": {"rchar_mb": rchar, "rd_mb": rd, "wchar_mb": wchar, "wr_mb": wr,
                   "scope": "proc"},
            "net_tx_bytes": tx, "net_rx_bytes": rx, "net_calls": None}


# a, b, c read; w only writes. With top_n=2, c is third on every read counter and
# last on every write counter, so it is the one row that should be cut.
ROWS = [
    ("a", 100.0, 50.0, 1.0, 0.2),
    ("b", 90.0, 40.0, 2.0, 0.1),
    ("c", 80.0, 30.0, 0.0, 0.0),
    ("w", 0.0, 0.0, 1000.0, 500.0),
]


def reduce(rows=ROWS, top_n=2, **net):
    r = IoReducer(Cfg({"reducers.io": {"top_n": top_n}}), ["agent"])
    for name, rchar, rd, wchar, wr in rows:
        r.feed(exit_rec(name, name, rchar, rd, wchar, wr, **net))
    return r.result()


class RankingTest(unittest.TestCase):

    def test_a_write_heavy_tool_survives_the_top_n_cut(self):
        tools = {t["tool"] for t in reduce()["by_tool"]}
        self.assertEqual(tools, {"a", "b", "w"})

    def test_a_write_heavy_user_survives_the_top_n_cut(self):
        users = {u["user"] for u in reduce()["by_user"]}
        self.assertEqual(users, {"a", "b", "w"})


class RowShapeTest(unittest.TestCase):

    def test_tool_rows_carry_read_and_write_on_both_layers(self):
        w = next(t for t in reduce()["by_tool"] if t["tool"] == "w")
        self.assertEqual((w["rd_mb"], w["rchar_mb"], w["wr_mb"], w["wchar_mb"]),
                         (0.0, 0.0, 500.0, 1000.0))

    def test_tool_rows_carry_their_event_count_and_coverage(self):
        r = IoReducer(Cfg({}), ["agent"])
        r.feed(exit_rec("git", "u", rchar=4.0, rd=1.0))
        rec = exit_rec("git", "u")
        rec["io"] = None
        r.feed(rec)
        git = r.result()["by_tool"][0]
        self.assertEqual((git["events"], git["io_coverage_pct"], git["cache_hit_pct"]),
                         (2, 50.0, 75.0))

    def test_user_rows_carry_both_layers_and_both_network_directions(self):
        a = next(u for u in reduce(tx=2e6, rx=3e6)["by_user"] if u["user"] == "a")
        self.assertEqual((a["rd_mb"], a["rchar_mb"], a["wr_mb"], a["wchar_mb"]),
                         (50.0, 100.0, 0.2, 1.0))
        self.assertEqual((a["net_tx_mb"], a["net_rx_mb"]), (2.0, 3.0))


class DistributionTest(unittest.TestCase):

    def test_the_distributions_cover_disk_writes_too(self):
        q = reduce()["by_class"]["agent"]["quants"]
        self.assertEqual(list(q), ["rchar_mb", "rd_mb", "wchar_mb", "wr_mb"])
        self.assertEqual(q["wr_mb"]["max"], 500.0)


if __name__ == "__main__":
    unittest.main()
