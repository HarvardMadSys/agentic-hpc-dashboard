"""Resources time series: what each bin holds, and what an empty bin says.

    cd dashboard && .venv/bin/python -m unittest discover -s tests -q
"""
import pickle
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rc_dashboard import snapshots                                  # noqa: E402
from rc_dashboard.reducers.resources import (                       # noqa: E402
    STALL_SERIES_TOOLS, ResourceReducer, bin_for)


class Cfg:
    def get(self, dotted, default=None):
        return default


# Hour-aligned and a day back, so every bin width divides it and nothing here
# is "in the future" to the skew guard.
T0 = (int(time.time()) // 3600 - 24) * 3600


def exit_rec(at, cls="agent", tool="python", cpu=1.0, wait=0.1, dstate=0.0, maj=0,
             host="h1"):
    return {"event": "exit", "_a3": cls, "_eff": tool, "_ts_epoch": at, "host": host,
            "cpu_s": cpu, "sched_run_s": cpu, "sched_wait_s": wait,
            "dstate_wait_s": dstate, "maj_flt": maj}


def tick(at, host="h1", **by):
    return {"event": "residency_totals", "_a3": "unlabeled", "_ts_epoch": at,
            "host": host, "by_actor3": by}


def reduce(recs, window_s=3600):
    r = ResourceReducer(Cfg(), ["agent", "human", "unlabeled"], window_s=window_s)
    for rec in recs:
        r.feed(rec)
    return r.result()


class BinWidthTest(unittest.TestCase):

    def test_the_width_keeps_every_window_to_a_few_hundred_points(self):
        self.assertEqual(bin_for(3600), 60)
        self.assertEqual(bin_for(86400), 600)
        self.assertEqual(bin_for(7 * 86400), 3600)
        self.assertEqual(bin_for(300), 60)


class ExitSeriesTest(unittest.TestCase):

    def test_a_bin_with_no_record_is_a_gap_not_a_zero(self):
        s = reduce([exit_rec(T0), exit_rec(T0 + 120)])["series"]
        self.assertEqual(s["t"], [T0, T0 + 60, T0 + 120])
        self.assertEqual(s["by_class"]["agent"]["n"], [1, None, 1])
        self.assertEqual(s["by_class"]["agent"]["cpu_s"], [1.0, None, 1.0])

    def test_a_bin_whose_records_all_lack_a_field_is_null_for_it(self):
        s = reduce([exit_rec(T0, wait=None), exit_rec(T0 + 60)])["series"]
        self.assertEqual(s["by_class"]["agent"]["sched_wait_s"], [None, 0.1])
        self.assertEqual(s["by_class"]["agent"]["n"], [1, 1])

    def test_the_bins_add_up_to_the_class_total(self):
        recs = [exit_rec(T0 + 37 * i, cpu=0.5 + i) for i in range(40)]
        res = reduce(recs)
        cpu = [v for v in res["series"]["by_class"]["agent"]["cpu_s"] if v is not None]
        self.assertAlmostEqual(sum(cpu), res["by_class"]["agent"]["cpu_s"], places=2)

    def test_a_record_far_in_the_future_is_counted_not_binned(self):
        s = reduce([exit_rec(T0), exit_rec(time.time() + 86400)])["series"]
        self.assertEqual(s["t"], [T0])
        self.assertEqual(s["skipped"]["future"], 1)

    def test_a_record_with_no_time_is_counted_not_binned(self):
        s = reduce([exit_rec(T0), exit_rec(None)])["series"]
        self.assertEqual(s["by_class"]["agent"]["n"], [1])
        self.assertEqual(s["skipped"]["no_ts"], 1)


class IoSeriesTest(unittest.TestCase):

    def test_bytes_land_in_their_bin_and_unmeasured_io_is_a_gap(self):
        a = exit_rec(T0)
        a["io"] = {"rchar_mb": 5.0, "rd_mb": 1.0, "wchar_mb": 2.0, "wr_mb": 0.5}
        b = exit_rec(T0 + 60)
        b["io"] = {"rchar_mb": None, "rd_mb": None, "wchar_mb": None, "wr_mb": None}
        s = reduce([a, b])["series"]["by_class"]["agent"]
        self.assertEqual(s["rd_mb"], [1.0, None])
        self.assertEqual(s["rchar_mb"], [5.0, None])
        self.assertEqual(s["n"], [1, 1])


class StallSeriesTest(unittest.TestCase):

    def test_a_tool_that_did_not_stall_in_a_measured_bin_reads_zero(self):
        recs = [exit_rec(T0, tool="ls", dstate=2.0),
                exit_rec(T0 + 60, tool="cat", dstate=0.0),
                exit_rec(T0 + 120, tool="cat", dstate=None)]
        s = reduce(recs)["series"]
        self.assertEqual(s["stall_by_tool"]["ls"], [2.0, 0, None])

    def test_tools_past_the_named_lines_are_one_other_line(self):
        tools = ["t%d" % i for i in range(STALL_SERIES_TOOLS + 2)]
        recs = [exit_rec(T0, tool=t, dstate=10.0 - i) for i, t in enumerate(tools)]
        s = reduce(recs)["series"]
        self.assertEqual(list(s["stall_by_tool"]), tools[:STALL_SERIES_TOOLS])
        rest = sum(10.0 - i for i in range(STALL_SERIES_TOOLS, len(tools)))
        self.assertEqual(s["stall_other"], [rest])


class ResidentSeriesTest(unittest.TestCase):

    def test_hosts_are_averaged_over_their_ticks_then_added(self):
        recs = [tick(T0, "h1", agent={"rss_mb": 100, "n_procs": 4, "d_state": 0}),
                tick(T0 + 30, "h1", agent={"rss_mb": 300, "n_procs": 6, "d_state": 1}),
                tick(T0 + 10, "h2", agent={"rss_mb": 50, "n_procs": 1, "d_state": 0})]
        s = reduce(recs)["series"]
        self.assertEqual(s["resident"]["agent"]["rss_mb"], [250.0])
        self.assertEqual(s["resident"]["agent"]["n_procs"], [6.0])
        self.assertEqual(s["resident_hosts"], [2])

    def test_a_class_missing_from_a_tick_had_none_running(self):
        recs = [tick(T0, agent={"rss_mb": 10}, human={"rss_mb": 20}),
                tick(T0 + 60, agent={"rss_mb": 10}),
                tick(T0 + 180, agent={"rss_mb": 10})]
        s = reduce(recs)["series"]
        self.assertEqual(s["resident"]["human"]["rss_mb"], [20.0, 0.0, None, 0.0])
        self.assertEqual(s["resident_hosts"], [1, 1, None, 1])

    def test_a_tick_is_not_a_process_event(self):
        res = reduce([tick(T0, agent={"rss_mb": 10}), exit_rec(T0)])
        self.assertEqual(set(res["by_class"]), {"agent"})
        self.assertEqual(res["by_class"]["agent"]["events"], 1)


class ImpossibleWaitTest(unittest.TestCase):

    def test_a_wait_longer_than_the_process_lived_is_not_measured(self):
        from rc_dashboard import config as configmod
        from rc_dashboard.normalize import Normalizer
        norm = Normalizer(configmod.Config(configmod.defaults(), {}, None))
        raw = {"schema_version": 7, "event": "exit", "ts_epoch": T0, "host": "h1",
               "user": "u", "pid": 5, "ppid": 1, "command": "cat", "args": "cat f",
               "actor3": "human", "duration_s": 1.26,
               "blkio_wait_s": 3393948.66, "dstate_wait_s": 0.4}   # ~ the host's uptime
        rec = norm.normalize(raw)
        self.assertIsNone(rec["blkio_wait_s"])
        self.assertEqual(rec["dstate_wait_s"], 0.4)              # a real one stays
        r = ResourceReducer(Cfg(), ["human"], window_s=3600)
        r.feed(rec)
        out = r.result()
        self.assertEqual(out["impossible_waits"], {"human": {"blkio_wait_s": 1}})
        self.assertEqual(out["by_class"]["human"]["delays"]["blkio_wait_s"]["n"], 0)


class SnapshotTest(unittest.TestCase):

    def test_the_series_state_survives_a_save_and_resume(self):
        r = ResourceReducer(Cfg(), ["agent"], window_s=86400)
        for rec in (exit_rec(T0, dstate=1.0), tick(T0, agent={"rss_mb": 5})):
            r.feed(rec)
        fresh = {"resources": ResourceReducer(Cfg(), ["agent"], window_s=86400)}
        snapshots.restore(fresh, pickle.loads(snapshots.capture({"resources": r})))
        self.assertEqual(fresh["resources"].result()["series"], r.result()["series"])


class MaxProcessTest(unittest.TestCase):
    """Roadmap notes 27 and 28: a max names its process, and an on-CPU share
    that cannot be real is set aside and counted, not capped at 100%."""

    def recs(self):
        a = exit_rec(T0 + 60, cpu=2.0)
        a.update(pid=11, user="ann", duration_s=10.0, peak_rss_mb=50.0, args="python a.py")
        b = exit_rec(T0 + 120, cpu=9.0)
        b.update(pid=22, user="bob", duration_s=20.0, peak_rss_mb=900.0, args="python b.py")
        # ran 5 s but "lived" 0.1 s: a lifetime counted from exec
        c = exit_rec(T0 + 180, cpu=5.0)
        c.update(pid=33, user="cy", duration_s=0.1, peak_rss_mb=10.0, args="sh -c x")
        return [a, b, c]

    def test_each_max_names_the_process_behind_it(self):
        top = reduce(self.recs())["by_class"]["agent"]["top"]
        self.assertEqual((top["cpu_s"]["value"], top["cpu_s"]["pid"], top["cpu_s"]["user"]), (9.0, 22, "bob"))
        self.assertEqual((top["peak_rss_mb"]["value"], top["peak_rss_mb"]["pid"]), (900.0, 22))
        self.assertEqual(top["duration_s"]["pid"], 22)

    def test_a_run_longer_than_the_measured_lifetime_is_set_aside(self):
        a = reduce(self.recs())["by_class"]["agent"]
        self.assertEqual(a["oncpu_set_aside"], 1)
        self.assertEqual(a["quants"]["on_cpu_frac"]["n"], 2)
        self.assertLess(a["quants"]["on_cpu_frac"]["max"], 1.0)

    def test_two_merged_states_keep_the_larger_max_and_add_the_set_aside(self):
        rs = []
        for part in (self.recs()[:1], self.recs()[1:]):
            r = ResourceReducer(Cfg(), ["agent", "human", "unlabeled"], window_s=3600)
            for rec in part:
                r.feed(rec)
            rs.append(r)
        rs[0].merge(rs[1])
        a = rs[0].result()["by_class"]["agent"]
        self.assertEqual(a["top"]["cpu_s"]["pid"], 22)
        self.assertEqual(a["oncpu_set_aside"], 1)


if __name__ == "__main__":
    unittest.main()
