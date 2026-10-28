"""Collector health, judged only from the records the collector writes.

    cd dashboard && .venv/bin/python -m unittest tests.test_health -q
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rc_dashboard.health import CollectorHealth  # noqa: E402

T0 = 1791518400.0


class Cfg:
    def get(self, k, d=None):
        return {"feeds.ebpf.max_age_s": 300}.get(k, d)


def meta(t, pid, rejected=None, sysctl=None, host="h1"):
    return {"event": "meta", "host": host, "ts_epoch": t, "collector_pid": pid,
            "kernel": "4.18", "bcc": "0.25", "features": ["io", "tcp_hdr"],
            "features_rejected": rejected or {},
            "sysctl": sysctl or {"kernel.sched_schedstats": "1"}}


def beat(t, pid, host="h1"):
    return {"event": "residency_totals", "host": host, "ts_epoch": t,
            "collector_pid": pid, "interval_s": 60.0}


def stop(t, pid, lost=0, seen=1000, reason="signal", host="h1"):
    return {"event": "stop", "host": host, "ts_epoch": t, "collector_pid": pid,
            "uptime_s": t - T0, "reason": reason, "records": 10, "truncated": 2,
            "perf_lost": {"events": lost}, "perf_seen": {"events": seen}}


def run(recs, now, files=None):
    h = CollectorHealth(Cfg())
    for r in recs:
        h.feed(r)
    return {x["host"]: x for x in h.report(files, now=now)}


class Verdicts(unittest.TestCase):

    def test_ok_with_a_recent_heartbeat_and_no_loss_reported_yet(self):
        r = run([meta(T0, 1), beat(T0 + 60, 1), beat(T0 + 120, 1)], now=T0 + 150)["h1"]
        self.assertEqual(r["verdict"], "ok")
        self.assertEqual(r["running"]["pid"], 1)
        self.assertIsNone(r["running"]["lost"])          # only a stop record says
        self.assertEqual(r["restarts"], 0)

    def test_stopped_cleanly_versus_gone_silent(self):
        recs = [meta(T0, 1), beat(T0 + 60, 1), stop(T0 + 61, 1, reason="signal")]
        r = run(recs, now=T0 + 3600)["h1"]
        self.assertEqual(r["verdict"], "stopped")
        self.assertIn("signal", r["reasons"][0])
        r = run([meta(T0, 1), beat(T0 + 60, 1)], now=T0 + 3600)["h1"]
        self.assertEqual(r["verdict"], "silent")

    def test_a_restart_reads_the_new_run_and_counts_the_old_runs_loss(self):
        recs = [meta(T0, 1), beat(T0 + 60, 1), stop(T0 + 100, 1, lost=5, seen=1000),
                meta(T0 + 110, 2), beat(T0 + 170, 2)]
        r = run(list(reversed(recs)), now=T0 + 200)["h1"]   # order does not matter
        self.assertEqual(r["restarts"], 1)
        self.assertEqual(r["running"]["pid"], 2)
        # 0.5% lost by the run before: past the warning line
        self.assertEqual(r["verdict"], "degraded")
        self.assertEqual(r["runs"][1]["lost_pct"], 0.5)

    def test_an_unloadable_probe_is_a_limit_and_a_sysctl_off_degrades(self):
        rej = {"tcp_accept": "BTF lacks required members",
               "tcp_basic": "superseded by tcp_hdr"}
        r = run([meta(T0, 1, rejected=rej), beat(T0 + 60, 1)], now=T0 + 90)["h1"]
        self.assertEqual(r["verdict"], "ok")
        self.assertEqual([u["feature"] for u in r["unavailable"]], ["tcp_accept"])
        self.assertTrue(r["limits"])
        r = run([meta(T0, 1, sysctl={"kernel.sched_schedstats": "0"}), beat(T0 + 60, 1)],
                now=T0 + 90)["h1"]
        self.assertEqual(r["verdict"], "degraded")

    def test_files_without_a_heartbeat_and_the_worst_host_first(self):
        files = {"h2": {"ebpf": {"files": 1, "bytes": 10, "last_write": T0}}}
        out = CollectorHealth(Cfg())
        out.feed(meta(T0, 1))
        out.feed(beat(T0 + 60, 1))
        rows = out.report(files, now=T0 + 90)
        self.assertEqual([(x["host"], x["verdict"]) for x in rows],
                         [("h2", "unknown"), ("h1", "ok")])


if __name__ == "__main__":
    unittest.main()
