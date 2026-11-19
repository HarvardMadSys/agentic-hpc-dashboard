"""The node tier over time (nodehist.py), the live tier day by day
(LiveReducer.daily), and how the tool mix's processes ended (ToolMixReducer
`exits`).

* a bin no snapshot landed in is a hole, a probe a snapshot did not report is
  None, and an NFS mount list with no I/O is no round trip, not one of 0;
* a bin is at least twice the snapshot interval, a latency is the bin's worst
  and a level its mean;
* a second pass reads only what was appended, a line still being written waits
  for its newline, and a day sealed into its `.gz` adds nothing it held;
* a day the live tier held no minute of has null counts, a class with nothing
  in a held day is 0, and an account counts once a day in each class;
* an exit 1 from grep is an answer, SIGPIPE is not a failure, and a merged
  state counts the endings one pass counts.

    cd dashboard && .venv/bin/python -m unittest tests.test_nodehist -q
"""
import gzip
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rc_dashboard import config as configmod                  # noqa: E402
from rc_dashboard.nodehist import NodeHistory, point           # noqa: E402
from rc_dashboard.reducers.live import LiveReducer            # noqa: E402
from rc_dashboard.reducers.tools import ToolMixReducer        # noqa: E402

CLASSES = ["agent", "human", "ide", "unlabeled"]


def cfg(**over):
    c = configmod.Config(configmod.defaults(), {}, None)
    for k, v in over.items():
        node = c.tree
        parts = k.split(".")
        for p in parts[:-1]:
            node = node.setdefault(p, {})
        node[parts[-1]] = v
    return c


def snap(host, e, fork=2.0, load=1.0, nfs=None, resp=True):
    rec = {"hostname": host, "ts_epoch": e,
           "timestamp": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(e)),
           "uptime": {"load_1": load},
           "memory": {"mem": {"total_gb": 100.0, "used_gb": 25.0}},
           "cpu": [{"cpu": "all", "iowait": 0.5}, {"cpu": "0", "iowait": 9.0}],
           "d_state_procs": [],
           "nfs_io": nfs if nfs is not None else [
               {"mountpoint": "/n/home", "rd_rtt_ms": 1.5, "wr_rtt_ms": 6.0,
                "rd_retrans": 0, "wr_retrans": 1},
               {"mountpoint": "/n/scratch", "rd_rtt_ms": 4.0, "wr_rtt_ms": 2.0,
                "rd_retrans": 2, "wr_retrans": 0}]}
    if resp:
        rec["responsiveness"] = {"fork_exec_ms": fork, "stat_local_ms": 0.02,
                                 "getent_ms": 5.0}
    return rec


class PointTest(unittest.TestCase):

    def test_a_snapshot_keeps_the_worst_mount_and_the_all_cores_row(self):
        p = dict(zip(("fork", "stat", "getent", "load", "iowait", "mem", "dstate",
                      "rd", "wr", "retr"), point(snap("h", 0.0))))
        self.assertEqual((p["fork"], p["load"], p["iowait"], p["mem"], p["dstate"]),
                         (2.0, 1.0, 0.5, 25.0, 0))
        self.assertEqual((p["rd"], p["wr"], p["retr"]), (4.0, 6.0, 3))

    def test_missing_is_none_not_zero(self):
        p = point(snap("h", 0.0, nfs=[], resp=False))
        self.assertEqual(p[0:3], (None, None, None))
        self.assertEqual(p[7:10], (None, None, None))
        rec = snap("h", 0.0)
        del rec["d_state_procs"]
        self.assertIsNone(point(rec)[6])


class NodeHistoryTest(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.now = time.time()
        self.day = time.strftime("%Y-%m-%d", time.localtime(self.now))
        os.makedirs(os.path.join(self.root, self.day))
        self.path = os.path.join(self.root, self.day, "login1.jsonl")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def write(self, recs, mode="a", newline=True):
        with open(self.path, mode) as f:
            for i, r in enumerate(recs):
                f.write(json.dumps(r))
                if newline or i < len(recs) - 1:
                    f.write("\n")

    def hist(self):
        return NodeHistory(cfg(**{"feeds.ebpf_node.roots": [self.root]}))

    def test_bins_holes_worst_and_mean(self):
        t0 = (int(self.now // 3600) - 2) * 3600
        # every 300 s for two hours, but none in the half hour from t0 + 3600
        recs = [snap("login1", t0 + i * 300 + 7, fork=1.0 + i, load=float(i))
                for i in range(24) if not 12 <= i < 18]
        self.write(recs, "w")
        h = self.hist()
        h.scan()
        s = h.series(3 * 3600, now=t0 + 2 * 3600 + 60)
        self.assertEqual(s["interval_s"], 300.0)
        self.assertEqual(s["bin_s"], 600)          # twice the interval, past 3 h's own
        row = s["hosts"]["login1"]
        i0 = s["t"].index(t0)
        # the bin t0..t0+600 holds snapshots 0 and 1: the worst probe, the mean load
        self.assertEqual(row["fork_exec_ms"][i0], 2.0)
        self.assertEqual(row["load_1"][i0], 0.5)
        self.assertEqual(row["snapshots"][i0], 2)
        hole = s["t"].index(t0 + 3600)
        for b in range(hole, hole + 3):
            self.assertIsNone(row["fork_exec_ms"][b])
            self.assertIsNone(row["snapshots"][b])
        self.assertIsNotNone(row["fork_exec_ms"][hole + 3])
        # before the first snapshot: a hole, not a zero
        self.assertIsNone(row["load_1"][0])

    def test_a_second_pass_reads_only_what_was_appended(self):
        t0 = self.now - 3600
        self.write([snap("login1", t0 + i * 300) for i in range(4)], "w")
        h = self.hist()
        h.scan()
        self.assertEqual(h.read["lines"], 4)
        # a line still being written: no newline yet, so not read
        self.write([snap("login1", t0 + 1200)], newline=False)
        h.scan()
        self.assertEqual(h.read["lines"], 4)
        with open(self.path, "a") as f:
            f.write("\n")
        self.write([snap("login1", t0 + 1500)])
        h.scan()
        self.assertEqual(h.read["lines"], 6)
        self.assertEqual(len(h.points["login1"]), 6)

    def test_a_sealed_day_adds_nothing_it_held(self):
        t0 = self.now - 3600
        self.write([snap("login1", t0 + i * 300) for i in range(5)], "w")
        h = self.hist()
        h.scan()
        with open(self.path, "rb") as src, gzip.open(self.path + ".gz", "wb") as dst:
            dst.write(src.read())
        os.unlink(self.path)
        h.scan()
        self.assertEqual(len(h.points["login1"]), 5)
        self.assertEqual(list(h.offsets), [self.path + ".gz"])
        lines = h.read["lines"]
        h.scan()                                    # read whole: not again
        self.assertEqual(h.read["lines"], lines)

    def test_older_than_retention_is_not_held(self):
        h = NodeHistory(cfg(**{"feeds.ebpf_node.roots": [self.root],
                               "live.retention_min": 60}))
        self.write([snap("login1", self.now - 6 * 3600), snap("login1", self.now - 60)], "w")
        h.scan()
        self.assertEqual(len(h.points["login1"]), 1)


def exit_rec(e, cls="agent", user="ann", atype="claude_code", cpu=1.0, **kw):
    r = {"event": "exit", "_a3": cls, "agent_type": atype if cls == "agent" else None,
         "_ts_epoch": e, "user": user, "cpu_s": cpu, "pid": int(e) % 100000, "host": "h"}
    r.update(kw)
    return r


class DailyTest(unittest.TestCase):

    def test_days_held_quiet_and_unheld(self):
        r = LiveReducer(cfg(), CLASSES)
        now = time.time()
        # an hour ago and on: whatever the hour, every record lands in one day
        t0 = int((now - 3600) // 60) * 60
        mid = time.mktime(time.localtime(t0)[:3] + (0, 0, 0, 0, 0, -1))
        t0 = max(t0, mid + 60)
        for k in range(6):
            r.feed(exit_rec(t0 + k * 60, user="ann" if k % 2 else "bob"))
        r.feed(exit_rec(t0 + 6 * 60, cls="human", user="ann", cpu=2.0))
        days = {d["date"]: d for d in r.daily(now)["days"]}
        self.assertTrue(days[time.strftime("%Y-%m-%d", time.localtime(now))]["today"])
        day = days[time.strftime("%Y-%m-%d", time.localtime(t0))]
        self.assertEqual(day["held"], 7)
        self.assertEqual(day["by_class"]["agent"], {"n": 6, "cpu_s": 6.0, "users": 2})
        self.assertEqual(day["by_class"]["human"], {"n": 1, "cpu_s": 2.0, "users": 1})
        # held, and the class ran nothing: a measured 0
        self.assertEqual(day["by_class"]["ide"], {"n": 0, "cpu_s": 0.0, "users": 0})
        self.assertEqual(day["types"]["claude_code"]["users"], 2)
        before = days[time.strftime("%Y-%m-%d", time.localtime(mid - 3600))]
        # not one minute held: unknown, not quiet
        self.assertEqual(before["held"], 0)
        self.assertFalse(before["today"])
        self.assertIsNone(before["by_class"]["agent"]["n"])
        self.assertEqual(before["types"], {})

    def test_a_pinned_clock_does_not_keep_a_reading_for_good(self):
        # clock.as_of freezes time.time(): a reading taken before the backfill
        # read anything must still give way once the records are in
        from unittest import mock
        r = LiveReducer(cfg(), CLASSES)
        now = time.time()
        t0 = int((now - 600) // 60) * 60
        day = time.strftime("%Y-%m-%d", time.localtime(t0))
        empty = {d["date"]: d for d in r.daily(now)["days"]}
        self.assertEqual(empty.get(day, {}).get("held", 0), 0)
        r.feed(exit_rec(t0, user="ann"))
        # within the reuse span, by both clocks: the same reading
        self.assertIs(r.daily(now), r.daily(now))
        with mock.patch("time.monotonic", return_value=time.monotonic() + LiveReducer.DAILY_REUSE_S + 1):
            days = {d["date"]: d for d in r.daily(now)["days"]}
        self.assertEqual(days[day]["held"], 1)


def tool_rec(tool, code=0, sig=None, cls="agent", pid=1):
    return {"event": "exit", "_a3": cls, "agent_type": "codex" if cls == "agent" else None,
            "_eff": tool, "_command": tool, "pid": pid, "ppid": 0, "_sid": "s",
            "cpu_s": 0.5, "exit_code": code, "signal": sig}


class ExitsTest(unittest.TestCase):
    RECS = [tool_rec("grep"), tool_rec("grep", 1), tool_rec("grep", 2),
            tool_rec("pytest", 1), tool_rec("pytest", None, 9), tool_rec("yes", None, 13),
            tool_rec("nvidia-smi", 127), tool_rec("make", 2, cls="human")]

    def test_verdicts(self):
        r = ToolMixReducer(cfg(), CLASSES)
        for rec in self.RECS:
            r.feed(rec)
        ex = r.result()["exits"]
        a = ex["by_class"]["agent"]
        self.assertEqual((a["calls"], a["nonzero"], a["answer"], a["signal"], a["pipe"], a["none"]),
                         (7, 3, 1, 1, 1, 0))
        self.assertEqual(sorted(a["codes"]), [[1, 1], [2, 1], [127, 1]])
        self.assertEqual(a["signals"], [[9, 1]])
        tools = {t["tool"]: t for t in a["tools"]}
        self.assertEqual((tools["grep"]["nonzero"], tools["grep"]["answer"]), (1, 1))
        self.assertNotIn("yes", tools)            # SIGPIPE alone: not a failing tool
        self.assertEqual(ex["by_agent_type"]["codex"]["nonzero"], 3)
        self.assertEqual(ex["by_class"]["human"]["nonzero"], 1)

    def test_a_merge_counts_what_one_pass_counts(self):
        one = ToolMixReducer(cfg(), CLASSES)
        a, b = ToolMixReducer(cfg(), CLASSES), ToolMixReducer(cfg(), CLASSES)
        for i, rec in enumerate(self.RECS):
            one.feed(rec)
            (a if i < 4 else b).feed(rec)
        a.merge(b)
        self.assertEqual(json.dumps(a.result()["exits"], sort_keys=True),
                         json.dumps(one.result()["exits"], sort_keys=True))


if __name__ == "__main__":
    unittest.main()
