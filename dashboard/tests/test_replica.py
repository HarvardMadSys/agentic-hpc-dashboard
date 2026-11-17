"""replica: a sealed day-file's Parquet tables read as the file itself.

Every answer a replica gives is checked against the JSONL file it was made from,
through `feedfile`'s own functions: the time searches probe for probe, ranged reads
forward and newest first record for record (offsets, byte counts, key order and
types), the kinds and `where` filters, `is_boundary` at every offset, the head
digest, and the count of lines that do not parse. The files are built to hold what
a reader must step around: blank and malformed lines, records out of time order,
records timed only by their `ts` string or not at all, an int `ts_epoch` (kept in
`_extra`), and both streams' kinds.

    cd dashboard && uv run python -m unittest tests.test_replica -q
"""
import gzip
import json
import os
import random
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rc_dashboard import fastjson, feedfile, gz        # noqa: E402
from rc_dashboard import feedparquet as fp             # noqa: E402
from rc_dashboard import replica as rp                 # noqa: E402
from rc_dashboard.where import Prefix                  # noqa: E402

from tests.test_feedparquet import full                # noqa: E402

DAY = "2026-11-14"
T0 = 1_794_632_420.0
KINDS = ("exit", "truncated", "conn", "netio", "residency", "residency_totals", "submit")


def feed_lines(seed, n=400):
    """Lines of a day-file as a collector (and a careless editor) might leave it."""
    rng = random.Random(seed)
    lines = []
    t = T0
    for i in range(n):
        t += rng.uniform(0, 30)
        r = rng.random()
        if r < 0.03:
            lines.append("")
            continue
        if r < 0.05:
            lines.append('{"event": "exit", "ts_epoch": %d' % i)     # torn mid-file
            continue
        rec = full(rng.choice(KINDS), i)
        rec["user"] = rng.choice(["ann", "bob", "cy"]) if "user" in rec else rec.get("user")
        ts = t - (rng.uniform(0, 0.25) if rng.random() < 0.3 else 0)  # the 250 ms hold
        rec["ts_epoch"] = ts
        q = rng.random()
        if q < 0.05:
            del rec["ts_epoch"]                    # timed by its `ts` string only
        elif q < 0.07:
            del rec["ts_epoch"]
            rec.pop("ts", None)                    # not timed at all
        elif q < 0.09:
            rec["ts_epoch"] = int(ts)              # an int: kept in _extra
        lines.append(json.dumps(rec))
    return lines


class ReplicaTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="replica-")
        self.root = os.path.join(self.tmp, "feed")
        self.out = os.path.join(self.tmp, "pq")
        os.makedirs(os.path.join(self.root, DAY))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make(self, lines, name="h1.exits.jsonl.gz"):
        path = os.path.join(self.root, DAY, name)
        with gzip.open(path, "wb") as fh:
            fh.write("".join(x + "\n" for x in lines).encode())
        man = fp.convert_source(DAY, path, self.out)
        self.assertTrue(rp.exact(man))
        return path, rp.Replica(self.out, man)

    def jsonl(self, path):
        """[(off, nbytes, line)] of every line of the file."""
        out, off = [], 0
        with gz.open_feed(path) as fh:
            for line in fh:
                out.append((off, len(line), line))
                off += len(line)
        return out

    def test_time_searches_probe_for_probe(self):
        for seed in range(6):
            lines = feed_lines(seed)
            path, rep = self.make(lines)
            times = sorted(r["ts_epoch"] for r in map(json.loads, filter(_rec, lines))
                           if isinstance(r.get("ts_epoch"), float))
            probes = [T0 - 10, T0 + 1e7] + times[::7] + [x + 1e-6 for x in times[::11]]
            with gz.open_feed(path) as fh:
                size = gz.fstat(fh).st_size
                self.assertEqual(rep.size, size)
                for s in (size, rep.complete_end, size // 3, size // 2 + 17):
                    for t in probes:
                        self.assertEqual(rep.seek_to_epoch(t, s),
                                         feedfile.seek_to_epoch(fh, t, s), (seed, s, t))
                        self.assertEqual(rep.end_at(t, s),
                                         feedfile.end_at_jsonl(fh, t, s), (seed, s, t))
                for pl in (1, 3):
                    for t in probes[::5]:
                        self.assertEqual(rep.seek_to_epoch(t, size, pl),
                                         feedfile.seek_to_epoch(fh, t, size, probe_lines=pl))

    def test_ranges_forward_and_newest_first(self):
        lines = feed_lines(7)
        path, rep = self.make(lines)
        whole = self.jsonl(path)
        recs = [(o, n, fastjson.loads(ln)) for o, n, ln in whole if _rec(ln.decode())]
        bounds = [o for o, _n, _l in whole] + [rep.complete_end]
        rng = random.Random(1)
        cases = [(0, None), (0, rep.complete_end)] + \
            [tuple(sorted(rng.sample(bounds, 2))) for _ in range(40)]
        for start, end in cases:
            e = rep.complete_end if end is None else end
            want = [x for x in recs if start <= x[0] < e]
            got = list(rep.records(start, end))
            self.assertEqual([(o, n) for o, n, _r in got], [(o, n) for o, n, _r in want])
            for (_o, _n, a), (_o2, _n2, b) in zip(want, got):
                self.assertTrue(fp.same(a, b) and fp.same_order(a, b), (a, b))
            back = list(rep.records(start, end, reverse=True))
            self.assertEqual([o for o, _n, _r in back], [o for o, _n, _r in reversed(want)])
            bad = sum(1 for o, _n, ln in whole if start <= o < e
                      and ln.strip() and not _rec(ln.decode()))
            self.assertEqual(rep.bad_in(start, e), bad)

    def test_kinds_and_where_leave_out_only_what_cannot_match(self):
        lines = feed_lines(8)
        snap = feed_lines(9)
        for name, ls in (("h1.exits.jsonl.gz", lines), ("h1.snapshot.jsonl.gz", snap)):
            path, rep = self.make(ls, name)
            recs = [(o, fastjson.loads(ln)) for o, _n, ln in self.jsonl(path)
                    if _rec(ln.decode())]
            for kinds in ({"exit"}, {"truncated", "conn"}, {"residency"}, {"meta"}):
                for where in (None, {"user": {"ann"}}, [{"user": {"bob"}}, {"pid": {"301"}}],
                              {"cwd": Prefix("s1")}, {"ts_epoch": lambda v: v is not None}):
                    want = [o for o, r in recs if r.get("event") in kinds and
                            (where is None or _match(where, r))]
                    got = [o for o, _n, _r in rep.records(kinds=kinds, where=where)]
                    self.assertEqual(got, want, (name, kinds, where))

    def test_boundaries_and_head(self):
        lines = feed_lines(10, n=60)
        path, rep = self.make(lines)
        with gz.open_feed(path) as fh:
            size = gz.fstat(fh).st_size
            for off in range(0, size + 3):
                self.assertEqual(rep.is_boundary(off), feedfile.is_boundary(fh, off), off)
            for n in (0, 1, 100, 4096):
                self.assertEqual(rep.head_digest(n), feedfile.head_digest(fh, n))

    def test_what_a_replica_cannot_stand_in_for(self):
        path = os.path.join(self.root, DAY, "h2.exits.jsonl.gz")
        with gzip.open(path, "wb") as fh:
            fh.write((json.dumps(full("exit")) + '\n{"event": "ex').encode())
        self.assertFalse(rp.exact(fp.convert_source(DAY, path, self.out)))
        with gzip.open(path, "wb") as fh:
            fh.write((json.dumps(full("exit")) + "\n[1, 2]\n").encode())
        self.assertFalse(rp.exact(fp.convert_source(DAY, path, self.out)))


def _rec(line):
    try:
        return isinstance(json.loads(line), dict)
    except ValueError:
        return False


def _match(where, rec):
    from rc_dashboard.where import match
    return match(where, rec)


if __name__ == "__main__":
    unittest.main()
