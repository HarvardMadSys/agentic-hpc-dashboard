"""Node-tier tests: NodeReducer reads the snapshot schema the collector writes.

Snapshots here are shaped like `collector/node_snapshot.py` output: `nfs_io`
is `parse_nfsiostat()` (flat per-interval rd_/wr_ rates, only mounts with I/O
in the interval), `nfs_mountstats` is `parse_mountstats()` (cumulative, nested
`read`/`write`, present only when that op count is non-zero).

    cd dashboard && .venv/bin/python -m unittest discover -s tests -q
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rc_dashboard import config as configmod                 # noqa: E402
from rc_dashboard.reducers.resources import NodeReducer      # noqa: E402

NFS_IO_FIELDS = ("ops_s", "rd_kb_s", "rd_rtt_ms", "rd_exe_ms", "rd_retrans",
                 "wr_kb_s", "wr_rtt_ms", "wr_exe_ms", "wr_retrans")
MOUNTSTATS_FIELDS = ("fstype", "bad_xid", "backlog_u", "rd_ops_total", "wr_ops_total")


def nfs_io(mp, **kw):
    rec = {"mountpoint": mp, "ops_s": 12.5,
           "rd_kb_s": 340.0, "rd_rtt_ms": 1.8, "rd_exe_ms": 2.1, "rd_retrans": 0,
           "wr_kb_s": 55.0, "wr_rtt_ms": 9.4, "wr_exe_ms": 11.0, "wr_retrans": 3}
    rec.update(kw)
    return rec


def mountstats(mp, read=None, write=None):
    rec = {"mountpoint": mp, "fstype": "nfs4", "bad_xid": 2, "backlog_u": 0.25}
    if read is not None:
        rec["read"] = {"ops": read, "retrans": 1, "avg_queue_ms": 0.1,
                       "avg_rtt_ms": 1.5, "avg_exe_ms": 1.9}
    if write is not None:
        rec["write"] = {"ops": write, "retrans": 4, "avg_queue_ms": 0.3,
                        "avg_rtt_ms": 8.0, "avg_exe_ms": 9.5}
    return rec


def snapshot(io, ms):
    return {"hostname": "login1", "timestamp": "2026-10-07 12:00:00",
            "nfs_io": io, "nfs_mountstats": ms}


class NfsRowsTest(unittest.TestCase):

    def rows(self, io, ms):
        r = NodeReducer(configmod.Config(configmod.defaults(), {}, None), [])
        r.feed_snapshot(snapshot(io, ms))
        return {m["mount"]: m for m in r.result()["nfs"]}

    def test_a_mount_in_both_sources_carries_read_and_write_separately(self):
        row = self.rows([nfs_io("/n/home")],
                        [mountstats("/n/home", read=1000, write=200)])["/n/home"]
        self.assertEqual(row["host"], "login1")
        self.assertEqual((row["rd_kb_s"], row["rd_rtt_ms"], row["rd_exe_ms"],
                          row["rd_retrans"]), (340.0, 1.8, 2.1, 0))
        self.assertEqual((row["wr_kb_s"], row["wr_rtt_ms"], row["wr_exe_ms"],
                          row["wr_retrans"]), (55.0, 9.4, 11.0, 3))
        self.assertEqual(row["ops_s"], 12.5)
        self.assertEqual((row["fstype"], row["bad_xid"], row["backlog_u"]),
                         ("nfs4", 2, 0.25))
        self.assertEqual((row["rd_ops_total"], row["wr_ops_total"]), (1000, 200))

    def test_a_mount_without_interval_io_keeps_its_rates_null(self):
        # nfsiostat drops a mount with no I/O in its interval; the reducer cannot
        # tell that from a failed nfsiostat, so the rates are unknown, not 0.
        row = self.rows([], [mountstats("/n/idle", read=5, write=7)])["/n/idle"]
        for k in NFS_IO_FIELDS:
            self.assertIsNone(row[k], k)
        self.assertEqual((row["rd_ops_total"], row["wr_ops_total"]), (5, 7))
        self.assertEqual(row["bad_xid"], 2)

    def test_a_mount_missing_from_mountstats_keeps_its_counters_null(self):
        row = self.rows([nfs_io("/n/scratch")], [])["/n/scratch"]
        for k in MOUNTSTATS_FIELDS:
            self.assertIsNone(row[k], k)
        self.assertEqual(row["wr_rtt_ms"], 9.4)

    def test_an_op_mountstats_omits_has_a_null_cumulative_count(self):
        row = self.rows([nfs_io("/n/ro")], [mountstats("/n/ro", read=40)])["/n/ro"]
        self.assertEqual(row["rd_ops_total"], 40)
        self.assertIsNone(row["wr_ops_total"])

    def test_each_mount_is_one_row(self):
        rows = self.rows([nfs_io("/n/a"), nfs_io("/n/b")],
                         [mountstats("/n/a", read=1), mountstats("/n/c", read=1)])
        self.assertEqual(sorted(rows), ["/n/a", "/n/b", "/n/c"])


if __name__ == "__main__":
    unittest.main()
