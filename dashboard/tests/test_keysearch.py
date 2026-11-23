"""keysearch: the session inspector's key search with its records built in workers.

The fanned-out search must be the one-process search, exactly: for a plain
day-file and for a sealed one's replica, over parts a few lines long, from the
file's start or a line inside it, bounded at either end or not at all, the same
records in the same order, the same counters (`bytes`, `scanned`, `skipped`,
`parse_errors`, `no_ts`) whenever a record is handed on and at the end, and the
same stop past `until`. And the tree built from the records it sends back
(`INSPECT_KEYS`) must be the tree built from whole records.

    cd dashboard && uv run python -m unittest tests.test_keysearch -q
"""
import bisect
import gzip
import json
import os
import random
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rc_dashboard import config as configmod           # noqa: E402
from rc_dashboard import feedparquet as fp             # noqa: E402
from rc_dashboard import keysearch, proctree           # noqa: E402
from rc_dashboard import replica as rp                 # noqa: E402
from rc_dashboard.normalize import Normalizer          # noqa: E402

from tests.test_feedparquet import full                # noqa: E402
from tests.test_replica import DAY, T0                 # noqa: E402

PIDS = (101, 202, 303, 404)


def lines(seed, n=900):
    """A session's lines among others': every kind the search meets, out of time
    order a little, blank and torn lines, records timed by `ts` alone."""
    rng = random.Random(seed)
    out = []
    t = T0
    for i in range(n):
        t += rng.uniform(0, 20)
        r = rng.random()
        if r < 0.03:
            out.append("")
            continue
        if r < 0.05:
            out.append('{"event": "exit", "ts_epoch": %d' % i)          # torn, no key
            continue
        if r < 0.06:
            out.append('{"event": "exit", "agent_pid": 101, "ts_epoch": %d' % i)
            continue
        rec = full(rng.choice(("exit", "exit", "exit", "residency", "conn", "netio",
                               "truncated")), i)
        pid = rng.choice(PIDS)
        rec["agent_pid"] = pid
        rec["session_key"] = rng.choice(("claude:%d" % pid, str(pid), "s%d" % i))
        rec["ts_epoch"] = t - (rng.uniform(0, 90) if rng.random() < 0.05 else 0)
        if rng.random() < 0.03:
            del rec["ts_epoch"]
        if rng.random() < 0.05:
            rec.pop("duration_s", None)                # timed from `start` instead
        out.append(json.dumps(rec))
    return out


class KeySearchTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="keysearch-")
        d = os.path.join(cls.tmp, "feed", DAY)
        os.makedirs(d)
        text = "".join(x + "\n" for x in lines(11)).encode()
        cls.plain = os.path.join(d, "h1.exits.jsonl")
        with open(cls.plain, "wb") as fh:
            fh.write(text)
        cls.sealed = os.path.join(d, "h2.exits.jsonl.gz")
        with gzip.open(cls.sealed, "wb") as fh:
            fh.write(text)
        out = os.path.join(cls.tmp, "pq")
        man = fp.convert_source(DAY, cls.sealed, out, row_group_rows=50)
        cls.rep = rp.Replica(out, man)
        cls.cfg = configmod.Config(configmod.defaults(), {}, None)
        cls.starts = [i for i, _ in enumerate(text) if i == 0 or text[i - 1:i] == b"\n"]

    @classmethod
    def tearDownClass(cls):
        keysearch.shutdown()
        rp.install(None)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def read(self, path, start, since, until, workers, members):
        stats_out = []
        entry = {"path": path, "start": start}
        seen = []
        for rec, st in proctree.key_scan([entry], Normalizer(self.cfg), since, until,
                                         proctree.key_patterns(members),
                                         where=proctree.key_where(members),
                                         workers=workers, cfg=self.cfg):
            seen.append((keysearch.compact(rec), {k: st[k] for k in (
                "bytes", "scanned", "skipped", "parse_errors", "no_ts")}))
            stats_out = st
        seen.append(("end", {k: stats_out.get(k) for k in (
            "bytes", "scanned", "skipped", "parse_errors", "no_ts")} if stats_out else None))
        return seen

    def test_the_parallel_search_is_the_one_process_search(self):
        rp.install(lambda p: self.rep if p == self.sealed else None)
        members = {"h|%s|agent|apid:101" % DAY, "h|%s|agent|apid:303" % DAY}
        span = 900 * 10
        found = 0
        with mock.patch.object(proctree, "CHUNK", 700), \
                mock.patch.object(keysearch, "PLAIN_CHUNKS", 2), \
                mock.patch.object(keysearch, "PRIME", 1), \
                mock.patch.object(keysearch, "AHEAD", 0):
            for path in (self.plain, self.sealed):
                for since, until in ((T0 - 1, None), (T0 + span * 0.2, T0 + span * 0.6),
                                     (T0 - 1, T0 + span * 0.3), (T0 + span * 0.5, None),
                                     (T0 + 10 ** 6, None), (T0 - 99, T0 - 9)):
                    for start in (0, self.starts[len(self.starts) // 3]):
                        one = self.read(path, start, since, until, 1, members)
                        many = self.read(path, start, since, until, 3, members)
                        self.assertEqual(many, one, (os.path.basename(path), since, until,
                                                     start))
                        found += len(one) - 1
        self.assertGreater(found, 300)

    def test_a_read_stopped_early_is_cancelled(self):
        """A read the inspector stops (its budget, a reader gone elsewhere) drops
        what its workers have left: a queued part returns at once, a running one
        within CHECK_EVERY records, so the next read does not wait behind it."""
        members = {"h|%s|agent|apid:101" % DAY, "h|%s|agent|apid:303" % DAY}
        with mock.patch.object(proctree, "CHUNK", 700):
            it = proctree.key_scan([{"path": self.plain, "start": 0}], Normalizer(self.cfg),
                                   T0 - 1, None, proctree.key_patterns(members),
                                   where=proctree.key_where(members), workers=3, cfg=self.cfg)
            for _ in zip(range(5), it):
                pass
            read = keysearch._pool["seq"]
            self.assertLess(keysearch._pool["cancel"].value, read)
            it.close()
        self.assertGreaterEqual(keysearch._pool["cancel"].value, read)
        # a part of a cancelled read, queued or running, gives up
        job = {"kind": "plain", "path": self.plain, "size": os.path.getsize(self.plain),
               "chunks": [(0, os.path.getsize(self.plain))], "pats": proctree.key_patterns(
                   members), "events": proctree.EVENTS, "cfg": keysearch.cfg_spec(self.cfg),
               "since": T0 - 1, "until": None, "read": read}
        flag = mock.Mock(value=read - 1)
        with mock.patch.dict(keysearch._W, {"cancel": flag}):
            self.assertIsNotNone(keysearch.part(job), "a live read is read")
            flag.value = read
            self.assertIsNone(keysearch.part(job), "a cancelled one is not")

    def test_a_replica_its_workers_cannot_read_is_read_here_from_the_gz(self):
        """A part whose replica raises in a worker -- a table gone under it --
        does not end the read: the rest is the one-process read of the `.gz`,
        and the store is told (`replica.failed`)."""
        members = {"h|%s|agent|apid:101" % DAY, "h|%s|agent|apid:303" % DAY}
        rp.install(None)
        want = self.read(self.sealed, 0, T0 - 1, None, 1, members)
        rep = rp.Replica(self.rep.out, self.rep.man)
        # This process reads its tables; the copy a worker builds names none.
        rep.man = dict(rep.man, stem="gone")
        failures = []
        rp.install(lambda p: rep if p == self.sealed else None,
                   lambda p, r, e: failures.append(p))
        self.addCleanup(rp.install, None)
        with mock.patch.object(proctree, "CHUNK", 700), \
                mock.patch.object(keysearch, "PRIME", 1), \
                mock.patch.object(keysearch, "AHEAD", 0):
            got = self.read(self.sealed, 0, T0 - 1, None, 3, members)
        self.assertGreater(len(want), 100)
        self.assertEqual([r for r, _st in got[:-1]], [r for r, _st in want[:-1]])
        self.assertEqual(failures, [self.sealed])

    def test_after_a_part_fails_the_gz_goes_on_from_the_last_record_handed_on(self):
        """`_or_file` once some parts were handed on: the `.gz` from the line
        after the last of them, each record once."""
        members = {"h|%s|agent|apid:101" % DAY, "h|%s|agent|apid:303" % DAY}
        pats, where = proctree.key_patterns(members), proctree.key_where(members)
        ev, kinds = proctree.event_filter(proctree.EVENTS), frozenset(proctree.EVENTS)

        def stats():
            return {"scanned": 0, "skipped": 0, "bytes": 0, "parse_errors": 0, "no_ts": 0}
        want = [(ts, keysearch.compact(r)) for ts, r in proctree._key_stream_gz(
            {"path": self.sealed, "start": 0}, Normalizer(self.cfg), T0 - 1, None, stats(),
            pats, ev)]
        self.assertGreater(len(want), 100)
        failures = []
        rp.install(None, lambda p, r, e: failures.append(p))
        self.addCleanup(rp.install, None)
        for fail_at in (1, 40, len(want) - 1):
            got_next, seen = {}, {}
            healthy = proctree._key_stream_replica(
                {"path": self.sealed, "start": 0}, self.rep, Normalizer(self.cfg), T0 - 1,
                None, stats(), kinds, where, got_next)

            def replayed():
                # the parts' records, as `_replay` hands them on, then a part raises
                for i, (ts, rec) in enumerate(healthy):
                    if i == fail_at:
                        raise OSError("a part's table cut short")
                    # the record's own line: the one before the line after it
                    seen["off"] = self.starts[bisect.bisect_left(self.starts,
                                                                 got_next["next"]) - 1]
                    yield ts, rec
            got = [(ts, keysearch.compact(r)) for ts, r in keysearch._or_file(
                {"path": self.sealed, "start": 0}, self.rep, replayed(), seen,
                keysearch.cfg_spec(self.cfg), T0 - 1, None, stats(), pats, proctree.EVENTS)]
            self.assertEqual(got, want, fail_at)
        self.assertEqual(failures, [self.sealed] * 3)

    def test_the_tree_from_the_records_sent_back_is_the_whole_records_tree(self):
        members = {"h|%s|agent|apid:101" % DAY, "h|%s|agent|apid:202" % DAY}
        lim = proctree._limits(self.cfg)
        recs = [rec for rec, _st in proctree.key_scan(
            [{"path": self.plain, "start": 0}], Normalizer(self.cfg), T0 - 1, None,
            proctree.key_patterns(members), where=proctree.key_where(members))]
        self.assertGreater(len(recs), 100)
        trees = []
        for feed in (recs, [keysearch.compact(r) for r in recs]):
            for newest in (False, True):
                b = proctree.TreeBuilder(lim["inspect_hold"], lim["inspect_per_parent"],
                                         newest=newest)
                for r in feed:
                    b.feed(r)
                root = b.resolve(101, {101, 202}, lambda pid: None)
                tree, rs = b.render(root, lim["inspect_nodes"])
                trees.append(json.dumps([tree, rs, b.nav(101, T0, b.last)], sort_keys=True,
                                        default=str))
        self.assertEqual(trees[2], trees[0])
        self.assertEqual(trees[3], trees[1])

    def family(self):
        """An agent's session in which the own-I/O join (reducers/own_io.py) has
        work to do, `io` read at process scope: an earlier holder of pid 500 left
        a child that outlived it (40 MB), and pid 500 again, a tool call, says it
        reaped no child (`child_cpu_s` 0), so those 40 MB stay the orphan's; the
        agent takes back what its two tool calls carried (60 and 25 MB)."""
        norm = Normalizer(self.cfg)

        def exit_(i, pid, ppid, start, end, rd_mb, child_cpu_s, command):
            rec = full("exit", i)
            rec.update(pid=pid, ppid=ppid, tree_root_pid=101, agent_pid=101, user="ann",
                       start=time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(T0 + start)),
                       duration_s=end - start, ts_epoch=T0 + end, child_cpu_s=child_cpu_s,
                       session_key="claude:101", agent_type="claude-code",
                       command=command, args=command,
                       io={"scope": "process", "rd_mb": rd_mb, "wr_mb": 0.0,
                           "rchar_mb": rd_mb, "wchar_mb": 0.0})
            return norm.normalize(rec)
        return [exit_(1, 777, 500, 9, 12, 40.0, 0.0, "sleep"),
                exit_(2, 500, 101, 10, 20, 60.0, 0.0, "grep"),
                exit_(3, 600, 101, 30, 40, 25.0, 0.0, "cat"),
                exit_(4, 101, 1, 0, 100, 200.0, 1.5, "claude")]

    def tree_of(self, feed, root, members, newest=False):
        lim = proctree._limits(self.cfg)
        b = proctree.TreeBuilder(lim["inspect_hold"], lim["inspect_per_parent"], newest=newest)
        for r in feed:
            b.feed(r)
        tree, rs = b.render(b.resolve(root, members, lambda pid: None), lim["inspect_nodes"])
        return json.dumps([tree, rs, b.nav(root, T0, b.last)], sort_keys=True, default=str)

    def test_a_processs_own_io_is_the_same_from_the_records_sent_back(self):
        """The join holds what a parent takes back by `host` and pid, and an exit
        whose `child_cpu_s` is 0 takes back none: from a part's records it takes
        back what it takes from whole ones, and so in a read whose files went
        partly to workers and partly not (a sealed day's replica, and a day with
        no replica read here)."""
        recs = self.family()
        whole = self.tree_of(recs, 101, {101})

        def own(tree):
            out, todo = {}, [json.loads(tree)[0]]
            while todo:
                node = todo.pop()
                if node.get("pid") is not None:
                    out[node["pid"]] = node["io"]["rd_mb"]
                todo.extend(node.get("kids") or ())
            return out
        # the join has work in this session: the tool call keeps what it read, and
        # the agent's own is its figure less what its two tool calls carried
        self.assertEqual(own(whole)[500], 60.0)
        self.assertEqual(own(whole)[101], 115.0)
        sent_back = [keysearch.compact(r) for r in recs]
        self.assertEqual(self.tree_of(sent_back, 101, {101}), whole)
        mixed = [keysearch.compact(r) if i % 2 else r for i, r in enumerate(recs)]
        self.assertEqual(self.tree_of(mixed, 101, {101}), whole)

    def test_the_tree_asks_for_no_key_a_part_leaves_out(self):
        """Every key the tree reads of a record that holds it is one a part sends
        back: one left out reads None from a part's record and its value from a
        whole one, and the two trees differ, where a test of whole against sent
        back may not reach the code that reads it."""

        class Asked(dict):
            """A record that remembers the keys it was asked for."""
            __slots__ = ("asked",)

            def __init__(self, rec):
                super().__init__(rec)
                self.asked = set()

            def get(self, k, *default):
                self.asked.add(k)
                return super().get(k, *default)

            def __getitem__(self, k):
                self.asked.add(k)
                return super().__getitem__(k)

            def __contains__(self, k):
                self.asked.add(k)
                return super().__contains__(k)

        members = {"h|%s|agent|apid:101" % DAY, "h|%s|agent|apid:202" % DAY}
        scanned = [rec for rec, _st in proctree.key_scan(
            [{"path": self.plain, "start": 0}], Normalizer(self.cfg), T0 - 1, None,
            proctree.key_patterns(members), where=proctree.key_where(members))]
        asked, held = set(), set()
        for recs, root, pids in ((scanned, 101, {101, 202}), (self.family(), 101, {101})):
            for newest in (False, True):
                feed = [Asked(r) for r in recs]
                self.tree_of(feed, root, pids, newest=newest)
                for r in feed:
                    asked |= r.asked
                    held.update(r)
        # the join's own reads were reached, as the fixture means them to be
        self.assertTrue({"host", "child_cpu_s", "tree_root_pid"} <= asked)
        self.assertEqual(sorted((asked & held) - set(keysearch.INSPECT_KEYS)), [])


class _Stop(Exception):
    pass


class IdleTest(unittest.TestCase):
    """The workers, a few hundred MB, are let go once nobody has read for IDLE_S --
    on a pinned clock too: `clock.as_of` freezes time.time for the whole process."""

    class Pool:
        """An executor as the reaper sees it."""
        _broken = False

        def __init__(self):
            self.down = False

        def shutdown(self, wait=True, cancel_futures=False):
            self.down = True

    def reap(self, idle_s, wait):
        """One look of the reaper's, `wait` seconds after a read, with time.time
        frozen: whether it let the workers go."""
        pool = self.Pool()
        looks = []
        me = threading.current_thread()

        class Clock:
            """keysearch's `time`, whose reaper looks once, at once, then stops (in
            this thread: a reaper an earlier test started sleeps as it did)."""
            def __getattr__(self, name):
                return getattr(time, name)

            def sleep(self, s):
                if threading.current_thread() is not me:
                    return time.sleep(s)
                looks.append(s)
                if len(looks) > 1:
                    raise _Stop
        with mock.patch.dict(keysearch._pool, {"ex": pool, "n": 2, "busy": 0,
                                               "reaper": "running"}), \
                mock.patch.object(keysearch, "IDLE_S", idle_s), \
                mock.patch("time.time", return_value=time.time()):
            self.assertIs(keysearch._executor(2), pool)          # a read takes the pool
            time.sleep(wait)
            with mock.patch.object(keysearch, "time", Clock()), self.assertRaises(_Stop):
                keysearch._reap()
            self.assertEqual(pool.down, keysearch._pool["ex"] is None)
            return pool.down

    def test_idle_workers_are_let_go_on_a_pinned_clock(self):
        self.assertTrue(self.reap(idle_s=0.05, wait=0.2))

    def test_workers_read_from_lately_are_kept(self):
        self.assertFalse(self.reap(idle_s=60, wait=0))


if __name__ == "__main__":
    unittest.main()
