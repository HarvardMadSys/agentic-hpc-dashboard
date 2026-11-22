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

from unittest import mock                              # noqa: E402

from rc_dashboard import config as configmod           # noqa: E402
from rc_dashboard import fastjson, feedfile, gz        # noqa: E402
from rc_dashboard import feedparquet as fp             # noqa: E402
from rc_dashboard import proctree                      # noqa: E402
from rc_dashboard import replica as rp                 # noqa: E402
from rc_dashboard.normalize import Normalizer          # noqa: E402
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
            fh.seek(0)
            data = fh.read()
        for x in range(0, size + 1):
            # what the key search's chunks and end come to on the inflated bytes
            nxt = data.find(b"\n", x - 1) + 1 if x else 0
            self.assertEqual(rep.next_boundary(x), nxt if nxt or not x else size, x)
            self.assertEqual(rep.prev_boundary(x), data.rfind(b"\n", 0, x) + 1, x)
        with gz.open_feed(path) as fh:
            for n in (0, 1, 100, 4096):
                self.assertEqual(rep.head_digest(n), feedfile.head_digest(fh, n))

    def test_a_key_search_reads_and_counts_as_the_gz_search(self):
        """proctree's key search of a replica (`_key_stream_replica`) against the
        same search of the `.gz` (`_key_stream_gz`): the same records, and the
        same `bytes`, `scanned` and `skipped` whenever a record is handed on --
        what `inspect_max_bytes` is checked against -- over chunks a few lines
        long, from the file's start or a line inside it, bounded at either end
        or not at all.

        Not a line that does not parse: the replica keeps where it is, not what
        it says, so it cannot tell whether a key is in it. The `.gz` search
        counts one a key's pattern hits as parsed (and a parse error); the
        replica's does not. Its bytes are counted alike."""
        rng = random.Random(5)
        lines = []
        t = T0
        for i in range(500):
            t += rng.uniform(0, 20)
            r = rng.random()
            if r < 0.03:
                lines.append("")
                continue
            if r < 0.05:
                lines.append('{"event": "exit", "ts_epoch": %d' % i)     # torn, no key
                continue
            rec = full(rng.choice(("exit", "exit", "residency", "conn", "netio",
                                   "truncated")), i)
            pid = rng.choice((101, 202, 303, 404))
            rec["agent_pid"] = pid
            rec["session_key"] = rng.choice(("claude:%d" % pid, str(pid), "s%d" % i))
            rec["ts_epoch"] = t
            lines.append(json.dumps(rec))
        path, rep = self.make(lines)
        cfg = configmod.Config(configmod.defaults(), {}, None)
        members = ["h1|%s|agent|apid:101" % DAY, "h1|%s|agent|apid:303" % DAY]
        pats, where = proctree.key_patterns(members), proctree.key_where(members)
        ev = proctree.event_filter(proctree.EVENTS)
        kinds = frozenset(proctree.EVENTS)
        starts = [o for o, _n, _l in self.jsonl(path)]
        found = 0
        with mock.patch.object(proctree, "CHUNK", 700):
            for since, until in ((T0 - 1, None), (T0 + 1500, T0 + 4000),
                                 (T0 - 1, T0 + 2500), (T0 + 4000, None),
                                 (T0 + 99999, None), (T0 - 99, T0 - 9)):
                for start in (0, starts[len(starts) // 3], starts[-1]):
                    got = {}
                    for name in ("gz", "replica"):
                        st = {"scanned": 0, "skipped": 0, "bytes": 0, "parse_errors": 0,
                              "no_ts": 0}
                        entry = {"path": path, "start": start}
                        if name == "gz":
                            it = proctree._key_stream_gz(entry, Normalizer(cfg), since,
                                                         until, st, pats, ev)
                        else:
                            it = proctree._key_stream_replica(entry, rep, Normalizer(cfg),
                                                              since, until, st, kinds, where)
                        seen = [(ts, rec, st["bytes"], st["scanned"], st["skipped"])
                                for ts, rec in it]
                        seen.append(("end", st["bytes"], st["scanned"], st["skipped"],
                                     st["no_ts"]))
                        got[name] = seen
                    self.assertEqual(got["replica"], got["gz"], (since, until, start))
                    found += len(got["gz"]) - 1
        self.assertGreater(found, 100, "the reads found the keys' records")

    def broken(self, path, rep, after):
        """`rep` installed for `path`, its ranged reads raising once `after`
        records are handed on, as a table damaged part way through does; and
        the list each `replica.failed` lands in."""
        real = rep.records

        def records(*a, **kw):
            for i, item in enumerate(real(*a, **kw)):
                if i >= after:
                    raise OSError("a table cut short under the read")
                yield item
        rep.records = records
        failures = []
        rp.install(lambda p: rep if p == path else None,
                   lambda p, r, e: failures.append((p, r, str(e))))
        self.addCleanup(rp.install, None)
        return failures

    def test_a_build_its_replica_fails_part_way_goes_on_from_the_file(self):
        """A table that raises in the middle of a read -- damaged with its size
        intact, or gone since the replica was taken -- costs the read nothing:
        it goes on from the file, from where the replica left it, the records
        the same as a read of the file alone, each once; and the store is told.
        Before, it raised out of the window build, on every retry."""
        from rc_dashboard import export
        path, rep = self.make(feed_lines(13, n=600))
        cfg = configmod.Config(configmod.defaults(), {}, None)
        rp.install(None)                    # what to compare with: the file read alone

        # a filtered view's build (`export.window_scan`)
        def window():
            entry = {"path": path, "start": 0}
            got = [(r["pid"], r["_ts_epoch"]) for r, _st in export.window_scan(
                [entry], Normalizer(cfg), T0 - 1, events={"exit", "truncated", "residency"},
                where={"user": frozenset({"ann"})})
                if r.get("user") == "ann"]          # the view's own test (`windows._build`)
            return got, entry["end"]
        want = window()
        self.assertGreater(len(want[0]), 20)
        for after in (0, 1, 7, len(want[0]) // 2):
            failures = self.broken(path, rp.Replica(self.out, rep.man), after)
            self.assertEqual(window(), want, after)
            self.assertEqual([p for p, _r, _e in failures], [path], after)

    def test_a_key_search_its_replica_fails_part_way_goes_on_in_the_gz(self):
        """The inspector's key search in this process (`proctree._key_stream`):
        the replica raising part way, the rest is the `.gz` search's."""
        rng = random.Random(14)
        lines = []
        for i in range(400):
            rec = full(rng.choice(("exit", "exit", "residency", "conn")), i)
            pid = rng.choice((101, 202, 303))
            rec["agent_pid"] = pid
            rec["session_key"] = rng.choice(("claude:%d" % pid, str(pid)))
            rec["ts_epoch"] = T0 + 10 * i
            lines.append(json.dumps(rec))
        path, rep = self.make(lines)
        cfg = configmod.Config(configmod.defaults(), {}, None)
        members = ["h1|%s|agent|apid:101" % DAY, "h1|%s|agent|apid:303" % DAY]
        pats, where = proctree.key_patterns(members), proctree.key_where(members)
        ev, kinds = proctree.event_filter(proctree.EVENTS), frozenset(proctree.EVENTS)

        def read(stream):
            st = {"scanned": 0, "skipped": 0, "bytes": 0, "parse_errors": 0, "no_ts": 0}
            return [(ts, r["pid"], r["_ts_epoch"]) for ts, r in stream(st)]
        want = read(lambda st: proctree._key_stream_gz(
            {"path": path, "start": 0}, Normalizer(cfg), T0 - 1, None, st, pats, ev))
        self.assertGreater(len(want), 100)
        for after in (0, 2, len(want) // 2):
            failures = self.broken(path, rp.Replica(self.out, rep.man), after)
            got = read(lambda st: proctree._key_stream(
                {"path": path, "start": 0}, Normalizer(cfg), T0 - 1, None, st, pats, ev,
                kinds, where))
            self.assertEqual(got, want, after)
            self.assertEqual(len(failures), 1)

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
