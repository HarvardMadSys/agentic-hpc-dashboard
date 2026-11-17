"""history: the store of sealed day-files' Parquet tables, and its replicas.

    cd dashboard && uv run python -m unittest tests.test_history -q
"""
import gzip
import json
import os
import shutil
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rc_dashboard import feedparquet as fp      # noqa: E402
from rc_dashboard import history, replica        # noqa: E402

from tests.test_feedparquet import full          # noqa: E402

HOST = "login01.example.org"


class Cfg:
    def __init__(self, d):
        self.d = d

    def get(self, key, default=None):
        return self.d.get(key, default)


class HistoryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="history-")
        self.root = os.path.join(self.tmp, "feed")
        self.dir = os.path.join(self.tmp, "store")
        self.cfg = Cfg({"feeds.ebpf.roots": [self.root], "cache.dir": self.tmp,
                        "history": {"dir": self.dir, "workers": 1}})

    def tearDown(self):
        replica.install(None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, day, stream, recs, sealed=True):
        d = os.path.join(self.root, day)
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, "%s.%s.jsonl" % (HOST, stream)) + (".gz" if sealed else "")
        data = "".join(json.dumps(r) + "\n" for r in recs).encode()
        if sealed:
            with gzip.open(path, "wb") as fh:
                fh.write(data)
        else:
            with open(path, "wb") as fh:
                fh.write(data)
        return path

    def test_sealed_files_only_and_replicas_after(self):
        old = self.write("2026-11-13", "exits", [full("exit", i) for i in range(3)])
        snap = self.write("2026-11-13", "snapshot", [full("conn", 1), full("residency", 2)])
        live = self.write("2026-11-14", "exits", [full("exit", 9)], sealed=False)
        st = history.HistoryStore(self.cfg)
        self.assertIsNone(st.replica_for(old))
        todo, prune = st.discover()
        self.assertEqual(sorted(p for _d, p, _o in todo), sorted([old, snap]))
        self.assertEqual(prune, [])
        self.assertEqual(st.run_once(), 2)
        rep = st.replica_for(old)
        self.assertIsInstance(rep, replica.Replica)
        self.assertEqual([r["pid"] for _o, _n, r in rep.records()],
                         [full("exit", i)["pid"] for i in range(3)])
        self.assertEqual(len(list(st.replica_for(snap).records())), 2)
        self.assertIsNone(st.replica_for(live))
        self.assertEqual(st.run_once(), 0, "nothing new")
        out = st.roots[os.path.abspath(self.root)]
        self.assertEqual(stat.S_IMODE(os.stat(out).st_mode), 0o700)
        with open(os.path.join(out, history.MARKER)) as fh:
            self.assertEqual(json.load(fh)["realpath"], os.path.realpath(self.root))
        s = st.status()
        self.assertEqual((s["replicas"], s["converted"], s["state"]), (2, 2, "idle"))

    def test_a_replaced_file_is_stale_until_converted_again(self):
        p = self.write("2026-11-13", "exits", [full("exit", 0)])
        st = history.HistoryStore(self.cfg)
        st.run_once()
        self.assertIsNotNone(st.replica_for(p))
        time.sleep(0.01)
        self.write("2026-11-13", "exits", [full("exit", 0), full("exit", 1)])
        self.assertIsNone(st.replica_for(p))
        self.assertEqual(st.run_once(), 1)
        self.assertEqual(len(list(st.replica_for(p).records())), 2)

    def test_a_gone_source_takes_its_replica_and_days_bound(self):
        a = self.write("2026-11-12", "exits", [full("exit", 0)])
        b = self.write("2026-11-13", "exits", [full("exit", 1)])
        st = history.HistoryStore(self.cfg)
        st.run_once()
        out = st.roots[os.path.abspath(self.root)]
        os.unlink(a)
        st.run_once()
        self.assertIsNone(fp.read_manifest(out, "2026-11-12", HOST + ".exits"))
        self.assertFalse(os.path.exists(os.path.join(out, "exit", "day=2026-11-12")))
        self.assertIsNotNone(st.replica_for(b))
        self.write("2026-11-11", "exits", [full("exit", 2)])
        cfg = Cfg(dict(self.cfg.d, history={"dir": self.dir, "workers": 1, "days": 1}))
        st2 = history.HistoryStore(cfg)
        todo, _prune = st2.discover()
        self.assertEqual([d for d, _p, _o in todo], [])
        self.assertIsNotNone(st2.replica_for(b))

    def test_a_store_root_of_another_source_is_refused(self):
        self.write("2026-11-13", "exits", [full("exit", 0)])
        st = history.HistoryStore(self.cfg)
        out = st.roots[os.path.abspath(self.root)]
        os.makedirs(out)
        with open(os.path.join(out, history.MARKER), "w") as fh:
            json.dump({"realpath": "/somewhere/else"}, fh)
        with self.assertRaises(RuntimeError):
            st.discover()

    def test_off_reads_nothing_from_the_store(self):
        p = self.write("2026-11-13", "exits", [full("exit", 0)])
        cfg = Cfg(dict(self.cfg.d, history={"dir": self.dir, "enabled": False}))
        st = history.HistoryStore(cfg)
        self.assertEqual(st.run_once(), 0)
        self.assertIsNone(st.replica_for(p))
        self.assertEqual(st.status()["state"], "off")

    def test_installed_lookup(self):
        p = self.write("2026-11-13", "exits", [full("exit", 0)])
        st = history.HistoryStore(self.cfg)
        st.run_once()
        self.assertIsNone(replica.for_path(p))
        replica.install(st.replica_for)
        self.assertIsNotNone(replica.for_path(p))


if __name__ == "__main__":
    unittest.main()
