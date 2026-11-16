"""feedparquet: the feed's records into Parquet tables, and back exactly.

Every record kind, built key by key from the module's own key lists, must come back
the same -- type for type, in the collector's key order -- and so must every way a
record can fail to fit a column: another type, out of range, text that is not
UTF-8, a time in another format, a nested block of another shape, a key no table
names, a key it lacks. Then the layout on disk (paths, modes, the manifest, a run
that finds nothing changed), and the two input formats giving the same tables.

    cd dashboard && uv run --extra parquet python -m unittest tests.test_feedparquet -q
"""
import json
import math
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    import pyarrow  # noqa: F401
    import pyarrow.parquet as pq
except ImportError:          # pragma: no cover - the service does not need pyarrow
    pq = None

from rc_dashboard import feedparquet as fp      # noqa: E402

DAY = "2026-11-14"
HOST = "login01.example.org"
T0 = 1_794_632_420.5718088


def sample(kind, i=0):
    """A value of `kind`, different for each `i`."""
    if kind is fp.STR:
        return "s%d" % i
    if kind is fp.I32:
        return 300 + i
    if kind is fp.I64:
        return (1 << 40) + i
    if kind is fp.F64:
        return 0.25 + i
    if kind is fp.BOOL:
        return bool(i % 2)
    if kind is fp.LOCAL:
        return "2026-11-14 00:00:%02d" % (i % 60)
    if kind is fp.JSON:
        return {"k": [1, 2.5, None, "v", True], "n": i}
    if kind is fp.NULL:
        return None
    if isinstance(kind, fp.List):
        return [sample(kind.item, i), sample(kind.item, i + 1)]
    if isinstance(kind, fp.Map):
        return {"a%d" % i: sample(kind.value, i), "b": sample(kind.value, i + 1)}
    if isinstance(kind, fp.Struct):
        return {n: sample(k, i) for n, k in kind.fields if n not in kind.optional}
    raise TypeError(kind)


def full(event, i=0):
    """A record of `event` with every key it always has, in the collector's order."""
    rec = {}
    for k, kind, opt in fp.EVENT_TABLE[event].keys[event]:
        if not opt:
            rec[k] = sample(kind, i)
    rec["event"] = event
    rec["host"] = HOST
    rec["ts_epoch"] = T0 + i
    return rec


@unittest.skipIf(pq is None, "pyarrow is not installed (uv sync --extra parquet)")
class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="feedparquet-")
        self.root = os.path.join(self.tmp, "feed")
        self.out = os.path.join(self.tmp, "pq")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_day(self, recs, name=HOST + ".exits.jsonl", day=DAY):
        d = os.path.join(self.root, day)
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, name)
        with open(path, "w") as fh:
            for r in recs:
                fh.write(json.dumps(r) + "\n")    # NaN, a lone surrogate: as json writes them
        return path

    def roundtrip(self, recs):
        """(the records read back, in the order given; the manifest)."""
        path = self.write_day(recs)
        man = fp.convert_host_day(DAY, HOST, [path], self.out)
        its = {name: fp.records(fp.out_path(self.out, name, DAY, HOST))
               for name in man["records"]}
        got = [next(its[(fp.table_of(r) or fp.TABLES["other"]).name]) for r in recs]
        for name, it in its.items():
            self.assertIsNone(next(it, None), "%s has rows left over" % name)
        return got, man

    def column(self, table, name):
        t = pq.read_table(fp.out_path(self.out, table, DAY, HOST), columns=[name])
        return t.column(name).to_pylist()

    def assertSameRecords(self, recs, got, order=True):
        for want, have in zip(recs, got):
            self.assertTrue(fp.same(want, have), "\nwant %r\nhave %r" % (want, have))
            if order:
                self.assertTrue(fp.same_order(want, have),
                                "\nwant %r\nhave %r" % (list(want), list(have)))


class RoundTrip(Base):
    def test_every_record_kind(self):
        recs = [full(ev, i) for i, ev in enumerate(sorted(fp.EVENT_TABLE))]
        got, man = self.roundtrip(recs)
        self.assertSameRecords(recs, got)
        self.assertEqual(man["extra_rows"], {}, "a record of the collector's own shape "
                         "must fit its columns")
        self.assertEqual(sorted(man["records"]), sorted({t.name for t in fp.EVENT_TABLE.values()}))
        for name in man["records"]:
            self.assertEqual(set(self.column(name, "_absent")), {None})

    def test_values_keep_their_columns_types(self):
        rec = full("exit")
        self.roundtrip([rec])
        self.assertEqual(self.column("exit", "pid"), [rec["pid"]])
        self.assertEqual(self.column("exit", "cpu_s"), [rec["cpu_s"]])
        self.assertEqual(str(pq.read_schema(fp.out_path(self.out, "exit", DAY, HOST))
                             .field("ts").type), "timestamp[ms]")
        self.assertEqual(self.column("exit", "ts")[0].strftime(fp.TS_FORMAT), rec["ts"])
        io = self.column("exit", "io")[0]
        self.assertEqual(io, rec["io"])

    def test_truncated_rows_share_the_exit_table(self):
        recs = [full("exit", 0), full("truncated", 1), full("exit", 2)]
        got, _man = self.roundtrip(recs)
        self.assertSameRecords(recs, got)
        self.assertEqual(self.column("exit", "event"), ["exit", "truncated", "exit"])
        self.assertEqual(self.column("exit", "rss_mb"), [None, recs[1]["rss_mb"], None])
        self.assertEqual(self.column("exit", "_absent"), [None, None, None])

    def test_a_schema7_record_says_what_it_lacks(self):
        old = full("exit", 0)
        del old["args_redacted"]
        recs = [old, full("exit", 1)]
        got, _man = self.roundtrip(recs)
        self.assertSameRecords(recs, got)
        self.assertNotIn("args_redacted", got[0])
        self.assertEqual(got[1]["args_redacted"], recs[1]["args_redacted"])
        self.assertEqual(self.column("exit", "args_redacted"), [None, recs[1]["args_redacted"]])
        self.assertEqual(self.column("exit", "_absent"), [["args_redacted"], None])

    def test_keys_written_only_when_they_hold_something(self):
        with_over = full("netio", 0)
        with_over["overflow"] = {"n": 3, "tx_bytes": 1, "rx_bytes": 2, "calls": 4}
        null_over = full("netio", 1)
        null_over["overflow"] = None               # never written so: kept in _extra
        exit_over = full("exit", 2)
        exit_over["net_endpoints"]["overflow"] = {"n": 1, "tx_bytes": 1, "rx_bytes": 1,
                                                  "calls": 1}
        exit_over["conns"]["peers_overflow"] = 7
        recs = [full("netio", 3), with_over, null_over, exit_over]
        got, man = self.roundtrip(recs)
        self.assertSameRecords(recs, got)
        self.assertEqual(self.column("netio", "_absent"), [None, None, None])
        self.assertEqual(self.column("netio", "_extra"), [None, None, '{"overflow":null}'])
        self.assertEqual(man["extra_rows"], {"netio": 1})

    def test_floats_as_written(self):
        rec = full("exit")
        rec["cpu_s"] = float("nan")
        rec["duration_s"] = float("inf")
        rec["sched_run_s"] = -0.0
        rec["cpu_pct"] = 1e-300
        got, man = self.roundtrip([rec])
        self.assertSameRecords([rec], got)
        self.assertTrue(math.isnan(got[0]["cpu_s"]))
        self.assertEqual(math.copysign(1, got[0]["sched_run_s"]), -1)
        self.assertEqual(man["extra_rows"], {})

    def test_maps_lists_and_json_blocks(self):
        meta = full("meta")
        meta["sysctl"] = {"kernel.sched_schedstats": "1", "kernel.task_delayacct": None}
        meta["features_rejected"] = {}
        meta["features"] = []
        meta["config"] = {"eph_range": [32768, 60999], "residency_s": 60.0, "x": None}
        meta["btf_offs"] = None
        res = full("residency")
        res["by_command"] = {"claude": 1, "2.1.322": 3, "?": 2}      # order is the count's
        res["d_stack"] = None
        res["top_procs"] = []
        recs = [meta, res]
        got, man = self.roundtrip(recs)
        self.assertSameRecords(recs, got)
        self.assertEqual(man["extra_rows"], {})


class Misfits(Base):
    """A value its column cannot hold exactly goes to `_extra`, alone: the row's
    other columns, and the other rows, keep theirs."""

    CASES = [
        ("cpu_s", 1),                       # an int where a float is written: 1 is not 1.0
        ("pid", 1.5),                       # pyarrow would truncate it to 1
        ("pid", True),                      # a bool is not an int
        ("pid", "300"),
        ("pid", 1 << 31),                   # past int32
        ("net_tx_bytes", 1 << 63),          # past int64, as a u64 counter can be
        ("net_rx_bytes", (1 << 64) - 1),
        ("args", "printf '\ud800'"),        # a lone surrogate: not UTF-8
        ("ts", "2026-11-14T00:00:20"),      # another format
        ("start", "2026-1-4 0:0:2"),        # parses, but would not read back the same
        ("start", "2026-11-14 24:00:00"),
        ("io", {"rd_mb": 1.0}),             # a block of another shape
        ("io", {"rd_mb": 1.0, "wr_mb": 2.0, "rchar_mb": 3.0, "wchar_mb": 4.0,
                "scope": "process", "new": 1}),
        ("net_endpoints", {"n": 1, "in_series": False, "unattributed": None,   # port: past int32
                           "endpoints": [{"proto": "tcp", "addr": "10.0.0.1", "port": 1 << 40,
                                          "host": None, "inbound": False, "external": False,
                                          "provider": None, "tx_bytes": 1, "rx_bytes": 2,
                                          "calls": 3}]}),
        ("conns", {"n": 1}),
        ("samples", [1, 2]),                # a key always null until it is not
        ("state_last", 5),
        ("args_redacted", ["prompt", 3]),
    ]

    def test_each_misfit_reads_back(self):
        recs = []
        for i, (k, v) in enumerate(self.CASES):
            recs.append(full("exit", 2 * i))
            r = full("exit", 2 * i + 1)
            r[k] = v
            recs.append(r)
        got, man = self.roundtrip(recs)
        self.assertSameRecords(recs, got)
        self.assertEqual(man["extra_rows"], {"exit": len(self.CASES)})
        extras = self.column("exit", "_extra")
        for i, (k, _v) in enumerate(self.CASES):
            self.assertIsNone(extras[2 * i], "row %d is whole" % (2 * i))
            self.assertEqual(list(json.loads(extras[2 * i + 1])), [k])
        pids = self.column("exit", "pid")
        for i, (k, _v) in enumerate(self.CASES):
            if k != "pid":
                self.assertEqual(pids[2 * i + 1], recs[2 * i + 1]["pid"])

    def test_records_as_the_dashboard_reads_them(self):
        """Past u64 orjson reads an integer as a float, and so does every reader
        of the feed (`fastjson`); the copy keeps what they read, in `_extra` here."""
        rec = full("exit")
        rec["net_tx_bytes"] = 1 << 64
        got, man = self.roundtrip([rec])
        read = fp.fastjson.loads(json.dumps(rec))
        self.assertTrue(fp.same(read, got[0]))
        self.assertEqual(type(got[0]["net_tx_bytes"]), type(read["net_tx_bytes"]))
        self.assertEqual(man["extra_rows"], {"exit": 1})

    def test_json_columns_take_any_value_but_surrogates(self):
        meta = full("meta", 0)
        meta["config"] = {"a": "\udcff"}
        meta2 = full("meta", 1)
        meta2["output"] = ["x", 1, {"y": None}]
        got, man = self.roundtrip([meta, meta2])
        self.assertSameRecords([meta, meta2], got)
        self.assertEqual(man["extra_rows"], {"meta": 1})

    def test_unknown_keys_and_events(self):
        v9 = full("exit", 0)
        v9["pk"] = "205008.135929015"          # schema 9's process key
        v9["ppk"] = None
        session = {"event": "session", "ts_epoch": T0, "host": HOST, "schema_version": 9,
                   "sess": "642bf1d3f9e5c153", "root_pk": "205005.135928230"}
        node = {"timestamp": "2026-11-14T00:00:00", "hostname": HOST, "load": [0.1, 0.2]}
        weird = {"event": ["not", "a", "name"], "ts_epoch": "soon"}
        recs = [v9, session, node, weird]
        got, man = self.roundtrip(recs)
        self.assertSameRecords(recs, got, order=False)
        self.assertEqual(list(got[0])[-2:], ["pk", "ppk"])
        self.assertEqual(man["records"], {"exit": 1, "other": 3})
        self.assertEqual(self.column("other", "event"), ["session", None, None])


class Layout(Base):
    def test_paths_modes_manifest(self):
        self.write_day([full("exit", 0), full("exit", 1)])
        self.write_day([full("conn", 2), full("residency_totals", 3)],
                       name=HOST + ".snapshot.jsonl")
        self.write_day([full("exit", 4)], day="2026-11-15")
        rc = fp.main(["convert", "--out", self.out, "--jobs", "1", self.root])
        self.assertEqual(rc, 0)
        for table, day in (("exit", DAY), ("conn", DAY), ("residency_totals", DAY),
                           ("exit", "2026-11-15")):
            p = fp.out_path(self.out, table, day, HOST)
            self.assertTrue(os.path.exists(p), p)
            self.assertEqual(os.stat(p).st_mode & 0o007, 0, "no access for other")
            footer = json.loads(pq.read_schema(p).metadata[fp.META_KEY])
            self.assertEqual((footer["table"], footer["host"], footer["day"]), (table, HOST, day))
        man = fp.read_manifest(self.out, DAY, HOST)
        self.assertEqual(man["records"], {"exit": 2, "conn": 1, "residency_totals": 1})
        self.assertEqual([s["name"] for s in man["sources"]],
                         [HOST + ".exits.jsonl", HOST + ".snapshot.jsonl"])
        self.assertEqual(fp.main(["verify", "--out", self.out, "--jobs", "1", self.root]), 0)

    def test_unchanged_sources_are_not_converted_again(self):
        path = self.write_day([full("exit", 0), full("conn", 1)])
        fp.main(["convert", "--out", self.out, "--jobs", "1", self.root])
        mp = fp.manifest_path(self.out, DAY, HOST)
        first = os.stat(mp).st_mtime_ns
        fp.main(["convert", "--out", self.out, "--jobs", "1", self.root])
        self.assertEqual(os.stat(mp).st_mtime_ns, first)
        # the day-file changes: converted again, and a table it no longer has goes
        time.sleep(0.01)
        with open(path, "w") as fh:
            fh.write(json.dumps(full("exit", 5)) + "\n")
        fp.main(["convert", "--out", self.out, "--jobs", "1", self.root])
        self.assertNotEqual(os.stat(mp).st_mtime_ns, first)
        self.assertEqual(fp.read_manifest(self.out, DAY, HOST)["records"], {"exit": 1})
        self.assertFalse(os.path.exists(fp.out_path(self.out, "conn", DAY, HOST)))

    def test_today_waits_unless_asked(self):
        today = time.strftime("%Y-%m-%d")
        self.write_day([full("exit", 0)], day=today)
        self.assertEqual(fp.host_days([self.root]), [])
        self.assertEqual([d for d, _h, _p in fp.host_days([self.root], include_today=True)],
                         [today])

    def test_a_bad_host_day_does_not_stop_the_others(self):
        self.write_day([full("exit", 0)])
        with open(os.path.join(self.root, DAY, "other.example.org.exits.jsonl.gz"), "wb") as fh:
            fh.write(b"\x1f\x8b\x08\x00 not gzip at all")
        self.assertEqual(fp.main(["convert", "--out", self.out, "--jobs", "1", self.root]), 1)
        self.assertTrue(os.path.exists(fp.out_path(self.out, "exit", DAY, HOST)))
        self.assertIsNone(fp.read_manifest(self.out, DAY, "other.example.org"))
        left = [n for _d, _s, ns in os.walk(self.out) for n in ns if ".tmp-" in n]
        self.assertEqual(left, [])

    def test_a_torn_line_is_counted_not_kept(self):
        path = self.write_day([full("exit", 0)])
        with open(path, "a") as fh:
            fh.write('{"event": "exit", "ts_ep')
        man = fp.convert_host_day(DAY, HOST, [path], self.out)
        self.assertEqual((man["records"], man["malformed"]), ({"exit": 1}, 1))


class Formats(Base):
    def test_jsonl_and_ebr_twins_give_the_same_tables(self):
        from rc_dashboard.demo.feed import Feed
        from rc_dashboard.demo.world import World
        end = float(int(time.time()) // 3600 * 3600) - 86400
        t = end - 2 * 3600
        w, f = World("demo", 2, t), Feed(self.root, fmt="both")
        while t < end:
            f.append(w.records(t, t + 3600))
            t += 3600
        f.close()
        outs = {}
        for prefer in ("jsonl", "ebr"):
            out = outs[prefer] = os.path.join(self.tmp, "pq-" + prefer)
            todo = fp.host_days([f.ebpf], include_today=True, prefer=prefer)
            self.assertTrue(todo)
            for day, host, paths in todo:
                self.assertTrue(all(fp.feedfile.format_of(p) == prefer for p in paths))
                fp.convert_host_day(day, host, paths, out)
                rep = fp.verify_host_day(day, host, paths, out)
                self.assertEqual((rep["differ"], rep["rows_left_over"]), (0, {}), rep)
        names = sorted(os.path.relpath(os.path.join(d, n), outs["jsonl"])
                       for d, _s, ns in os.walk(outs["jsonl"]) for n in ns
                       if n.endswith(".parquet"))
        self.assertTrue(names)
        for rel in names:
            a = pq.read_table(os.path.join(outs["jsonl"], rel))
            b = pq.read_table(os.path.join(outs["ebr"], rel))
            self.assertTrue(a.equals(b), rel)


if __name__ == "__main__":
    unittest.main()
