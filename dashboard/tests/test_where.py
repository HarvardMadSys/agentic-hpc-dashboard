"""where: the prefilters a sealed file's replica rejects records by.

A selective read of a sealed day-file -- a filtered view, an events query, the
commands behind a figure, a session's tree -- hands the replica (`replica.py`) a
`where` (`where.py`) built from the same inputs as the predicate that decides
each record, and the replica drops what the `where` rejects before the predicate
sees it. So every `where` must pass every record its predicate keeps, whatever a
record holds: a key it lacks, a null, an empty string, a value of another type.
And the replica, which tests a `where` on its columns, must pass exactly what
`where.match` passes. Both are checked on random records.

    cd dashboard && uv run python -m unittest tests.test_where -q
"""
import copy
import gzip
import json
import os
import random
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rc_dashboard import config as configmod                      # noqa: E402
from rc_dashboard import feedparquet as fp                        # noqa: E402
from rc_dashboard import iocmds, proctree, replica                # noqa: E402
from rc_dashboard.export import and_where, make_predicate, parse_filters    # noqa: E402
from rc_dashboard.normalize import Normalizer                     # noqa: E402
from rc_dashboard.where import Prefix, match                      # noqa: E402
from rc_dashboard.windows import ViewFilter                       # noqa: E402

T0 = 1_790_000_000.0


def stamp(epoch):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(epoch))


class WherePropertyTest(unittest.TestCase):
    """Every prefilter passes every record the predicate it stands in for keeps
    (`where.match` is its meaning), on random records; and a replica filters
    exactly as `where.match` does."""

    N = 2500
    MISSING = object()

    def setUp(self):
        self.rng = random.Random(20261116)
        self.norm = Normalizer(configmod.Config(configmod.defaults(), {}, None))
        self.recs = [self.record() for _ in range(self.N)]

    def pick(self, *vals):
        return self.rng.choice(vals)

    def record(self):
        t = T0 + self.rng.uniform(0, 7200)
        r = {"schema_version": 8, "ts": stamp(t), "ts_epoch": t,
             "pid": self.rng.randrange(9999), "ppid": 1, "args": "git status", "comm": "git"}
        for k, vals in (
                ("event", ("exit", "exit", "truncated", "conn", "residency", "submit",
                           self.MISSING)),
                ("user", ("alice", "bob", "", None, self.MISSING, "carol")),
                ("agent_type", ("claude_code", "codex", "none", "None", "", None, self.MISSING)),
                ("actor3", ("agent", "human", None, self.MISSING)),
                ("host", ("h1", "h2", "", None, self.MISSING)),
                ("session_key", ("apid:7", "apid:8", "claude:7", "7", "", None, self.MISSING,
                                 "x:y:7", "apid:70")),
                ("agent_pid", (7, 8, "7", 70, None, self.MISSING)),
                ("cwd", ("/home/a/proj", "/home/a/proj/sub", "/home/a/project2", "/", "",
                         None, self.MISSING)),
                ("exit_code", (0, 1, "1", None, self.MISSING)),
                ("signal", (None, 9, "9", self.MISSING)),
                ("cgroup", ("/user.slice/session-7.scope", None, self.MISSING)),
                ("tty", ("pts/5", None, self.MISSING))):
            v = self.pick(*vals)
            if v is not self.MISSING:
                r[k] = v
        return r

    def normalized(self, raw):
        return self.norm.normalize(copy.deepcopy(raw))

    def check(self, keep, where, kinds=None, what=""):
        hits = 0
        for raw in self.recs:
            rec = self.normalized(raw)
            if rec is None or not keep(rec):
                continue
            hits += 1
            self.assertTrue(match(where, raw), (what, raw))
            if kinds is not None:
                self.assertIn(raw.get("event"), kinds, (what, raw))
        return hits

    def test_a_view_filter(self):
        hits = 0
        for users, types in ((["alice"], []), ([], ["none"]), (["bob", ""], ["codex"]),
                             ([], ["claude_code", "none", "None"]), (["carol"], ["none"])):
            flt = ViewFilter(users, types)
            hits += self.check(flt.match, flt.where, what=(users, types))
        self.assertGreater(hits, 100)

    def test_the_event_filters(self):
        hits = 0
        for params in ({"user": ["alice"]}, {"host": ["h1"], "event": ["exit"]},
                       {"exit_code": ["1"], "signal": ["9"]}, {"session_key": ["apid:7"]},
                       {"agent_type": ["none"]}, {"agent_type": ["codex", "untagged"]},
                       {"user": ["bob"], "host": ["h2"], "event": ["exit", "truncated"],
                        "agent_type": ["claude_code"]}, {"class": ["human"]}):
            pred = make_predicate(parse_filters(params))
            self.assertIsNotNone(pred.where if set(params) - {"event", "class"} else True)
            hits += self.check(pred, pred.where, pred.events, what=params)
        self.assertGreater(hits, 100)

    def test_a_blast_radius_lookup(self):
        hits = 0
        for f, apid, host, cwd in (({}, ["7"], "h1", "/home/a/proj"),
                                   ({"user": {"alice"}}, ["7", "70"], None, None),
                                   ({}, None, "h2", "/home/a/proj/"), ({}, ["8"], None, "/"),
                                   ({"user": {"bob", ""}}, None, None, "/home/a")):
            ap = frozenset(apid) if apid else None
            ok = iocmds.predicate(dict(f), ap, host, cwd)
            hits += self.check(ok, iocmds.prefilter_where(f, ap, host, cwd),
                               what=(f, apid, host, cwd))
        self.assertGreater(hits, 50)

    def test_a_sessions_keys(self):
        # keys on a pid: a key the patterns cannot spell has no prefilter at all
        sids = sorted({r["_sid"] for r in (self.normalized(x) for x in self.recs)
                       if r is not None and r.get("_kt") == "apid"
                       and str(r.get("_kv")).isdigit()})
        self.assertTrue(sids)
        hits = 0
        for k in range(1, len(sids) + 1, max(1, len(sids) // 6)):
            members = set(sids[:k])
            where = proctree.key_where(members)
            self.assertIsNotNone(where)
            hits += self.check(lambda rec: rec.get("_sid") in members, where, what=members)
        self.assertGreater(hits, 100)

    def test_a_replica_filters_as_match(self):
        """A replica tests a `where` on its columns before it builds a record
        (`replica.records`): a value as its column holds it, one of another type
        kept beside the columns (`_extra`), a key a record lacks (`_absent`). It
        passes exactly the records `match` passes, of the kinds asked for, in
        either order."""
        tmp = tempfile.mkdtemp(prefix="where-")
        try:
            day = time.strftime("%Y-%m-%d", time.localtime(T0))
            os.makedirs(os.path.join(tmp, "feed", day))
            path = os.path.join(tmp, "feed", day, "h1.exits.jsonl.gz")
            with gzip.open(path, "wb") as fh:
                for r in self.recs:
                    fh.write((json.dumps(r) + "\n").encode())
            out = os.path.join(tmp, "pq")
            man = fp.convert_source(day, path, out, row_group_rows=700)
            self.assertTrue(replica.exact(man))
            rep = replica.Replica(out, man)
            every = [(o, r) for o, _n, r in rep.records()]
            self.assertEqual([r for _o, r in every], json.loads(json.dumps(self.recs)))
            pred = make_predicate(parse_filters({"user": ["bob"], "agent_type": ["none"]}))
            wheres = [ViewFilter(["alice"], ["none"]).where,
                      ViewFilter([], ["codex", "none"]).where,
                      pred.where,
                      and_where(pred.where, "ts_epoch",
                                lambda v: not isinstance(v, (int, float)) or v < T0 + 3600),
                      make_predicate(parse_filters({"exit_code": ["1"],
                                                    "signal": ["9"]})).where,
                      iocmds.prefilter_where({}, frozenset({"7"}), "h1", "/home/a/proj"),
                      {"cwd": Prefix("/home/a/proj")},
                      proctree.key_where({"h1|x|agent|apid:7"}),
                      proctree.key_where({"h1|x|agent|apid:7", "h1|x|agent|apid:70"})]
            for where in wheres:
                for kinds in (None, frozenset({"exit", "truncated"}), frozenset({"conn"})):
                    for reverse in (False, True):
                        got = [(o, r) for o, _n, r in rep.records(reverse=reverse,
                                                                   kinds=kinds, where=where)]
                        want = [(o, r) for o, r in every if match(where, r)
                                and (kinds is None or r.get("event") in kinds)]
                        if reverse:
                            want.reverse()
                        self.assertEqual(got, want, (where, kinds, reverse))
                        self.assertTrue(want or kinds == frozenset({"conn"}), where)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
