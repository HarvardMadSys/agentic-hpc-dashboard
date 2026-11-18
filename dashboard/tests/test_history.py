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
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rc_dashboard import config as configmod    # noqa: E402
from rc_dashboard import feedparquet as fp      # noqa: E402
from rc_dashboard import history, hours, replica, snapshots     # noqa: E402

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

    def test_a_killed_conversions_leftovers_are_swept(self):
        """What a conversion killed mid-file left (`.tmp-<pid>`) goes when the store
        next starts, once its process is gone; a live process's stays."""
        p = self.write("2026-11-13", "exits", [full("exit", 0)])
        st = history.HistoryStore(self.cfg)
        st.run_once()
        out = st.roots[os.path.abspath(self.root)]
        target = fp.out_path(out, "exit", "2026-11-13", HOST + ".exits")
        dead = os.fork()
        if dead == 0:
            os._exit(0)
        os.waitpid(dead, 0)
        gone = target + ".tmp-%d" % dead
        live = target + ".tmp-%d" % os.getpid()
        for x in (gone, live):
            with open(x, "wb") as fh:
                fh.write(b"half a file")
        again = history.HistoryStore(self.cfg)
        again.discover()
        self.assertFalse(os.path.exists(gone))
        self.assertTrue(os.path.exists(live))
        self.assertEqual(again.status()["swept"], 1)
        self.assertIsNotNone(again.replica_for(p))

    def test_replicas_are_held_by_memory_and_count(self):
        """A replica's indexes are let go, least recently used first, once the held
        ones pass REPLICA_BYTES or REPLICAS_MAX; the one just asked for stays."""
        paths = [self.write("2026-11-1%d" % d, "exits", [full("exit", i) for i in range(50)])
                 for d in (1, 2, 3)]
        st = history.HistoryStore(self.cfg)
        st.run_once()
        for p in paths:
            rep = st.replica_for(p)
            rep.seek_to_epoch(0)                   # builds its index
            self.assertGreater(rep.nbytes(), 0)
        self.assertEqual(len(st._cache), 3)
        with mock.patch.object(history, "REPLICA_BYTES", 1):
            st.replica_for(paths[0])
            self.assertEqual(list(st._cache), [paths[0]])
        with mock.patch.object(history, "REPLICAS_MAX", 2):
            for p in paths:
                st.replica_for(p)
            self.assertEqual(list(st._cache), paths[1:])
        self.assertEqual(st.status()["held"]["replicas"], 2)

    def test_a_replica_whose_table_is_gone_or_cut_short_stands_in_for_nothing(self):
        """The review's r3: with its table deleted, the manifest still read
        "current" and the replica was taken -- and a window's plan then left
        the sealed day out, as if it held nothing in the window. Cut short or
        emptied, the read raised on every retry. Each is no replica now, and
        the next round converts the file again."""
        p = self.write("2026-11-13", "exits", [full("exit", i) for i in range(50)])
        st = history.HistoryStore(self.cfg)
        self.assertEqual(st.run_once(), 1)
        table = fp.out_path(st.roots[os.path.abspath(self.root)], "exit", "2026-11-13",
                            HOST + ".exits")
        with open(table, "rb") as fh:
            whole = fh.read()
        for cut in (None, len(whole) // 2, 0):
            with self.subTest(cut=cut):
                st._cache.clear()                   # as at a start: nothing taken yet
                if cut is None:
                    os.unlink(table)
                else:
                    with open(table, "wb") as fh:
                        fh.write(whole[:cut])
                self.assertIsNone(st.replica_for(p))
                todo, _prune = st.discover()
                self.assertEqual([x for _d, x, _o in todo], [p])
                self.assertEqual(st.run_once(), 1)
                self.assertIsInstance(st.replica_for(p), replica.Replica)
                with open(table, "rb") as fh:
                    whole = fh.read()

    def test_a_replica_a_read_finds_broken_is_let_go_and_said_so(self):
        """A table damaged under a replica already taken, its size intact: the
        read goes on from the file (here a window's plan, which raised), the
        store lets the replica go and keeps the error the Feeds tab shows, and
        the next round converts the file again."""
        from rc_dashboard import export
        p = self.write("2026-11-13", "exits", [full("exit", i) for i in range(50)])
        st = history.HistoryStore(self.cfg)
        st.run_once()
        replica.install(st.replica_for, st.replica_failed)      # as `start` does
        self.assertIsInstance(replica.for_path(p), replica.Replica)
        table = fp.out_path(st.roots[os.path.abspath(self.root)], "exit", "2026-11-13",
                            HOST + ".exits")
        size = os.path.getsize(table)
        with open(table, "r+b") as fh:
            fh.write(b"\0" * size)
        plan, _total = export.window_plan([self.root], 0)
        self.assertEqual([(e["path"], e["start"]) for e in plan], [(p, 0)])
        s = st.status()
        self.assertEqual(s["read_failures"], 1)
        self.assertEqual([e["file"] for e in s["errors"]], [p])
        self.assertIn("read as JSONL", s["errors"][0]["error"])
        self.assertIsNone(st.replica_for(p))
        todo, _prune = st.discover()
        self.assertEqual([x for _d, x, _o in todo], [p])
        self.assertEqual(st.run_once(), 1)
        self.assertIsInstance(st.replica_for(p), replica.Replica)

    def test_lookups_are_counted(self):
        """What /api/feeds says of the store's use: a sealed file asked for is
        answered from its replica, or left to the JSONL; any other file is no
        lookup of the store's."""
        p = self.write("2026-11-13", "exits", [full("exit", 0)])
        live = self.write("2026-11-14", "exits", [full("exit", 1)], sealed=False)
        st = history.HistoryStore(self.cfg)
        self.assertIsNone(st.replica_for(p))
        st.run_once()
        self.assertIsNotNone(st.replica_for(p))
        self.assertIsNotNone(st.replica_for(p))
        self.assertIsNone(st.replica_for(live))
        self.assertEqual(st.status()["lookups"], {"replica": 2, "jsonl": 1})


class FingerprintTest(unittest.TestCase):
    """The store changes no answer, so no saved build is fingerprinted with it:
    switching it on or off, or moving it, is a restart, not a rebuild."""

    def test_no_saved_window_or_hour_is_rebuilt_for_a_history_setting(self):
        a = configmod.Config(configmod.defaults(), {}, None)
        tree = configmod.defaults()
        tree["history"].update(enabled=False, dir="/elsewhere", workers=3, days=7)
        b = configmod.Config(tree, {}, None)
        for f in (snapshots.fingerprint, hours.fingerprint):
            self.assertEqual(f("code", a, ["/r"]), f("code", b, ["/r"]), f.__module__)


if __name__ == "__main__":
    unittest.main()
