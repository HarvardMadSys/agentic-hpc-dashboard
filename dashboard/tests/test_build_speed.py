"""Builds got cheaper, and every number they make is the number they made.

Each speed-up of the build, the hourly reduction, the inspector's read and the
live tier is checked here against the path it replaced:

* a filtered scan that finds its lines a block at a time (`export._found`) yields
  the records, the resume offset and the bytes the line-by-line scan did, and
  leaves out of its skipped count the blank lines matching everywhere did;
* a memoized probe or head digest answers as a fresh read, and never for a file
  that has changed since;
* `heapq.nlargest` ranks the sessions `sorted(...)[:n]` ranked, ties included;
* eviction at a cap drops the keys a plain dict dropped, in the same order;
* a worst-offender list skipped below its floor serves the top it served;
* the live series' cached rows, and the backfill rows the tails would drop;
* the memoized `bucket`, `role_of`, `redact` (dropped on `extend`) and `tagset`;
* materializing a reducer per hold makes the payload `materialize()` made, and
  never swaps in one read from reducers a build has replaced since;
* a `Normalizer` no longer grows `sys.path`.

    cd dashboard && .venv/bin/python -m unittest tests.test_build_speed -q
"""
import collections
import gzip
import heapq
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
sys.path.insert(0, str(Path(__file__).resolve().parent))

from rc_dashboard import config as configmod       # noqa: E402
from rc_dashboard import export, normalize, procindex, redact, roles, tool_buckets  # noqa: E402
from rc_dashboard.buckets import RollingBuckets     # noqa: E402
from rc_dashboard.normalize import Normalizer       # noqa: E402
from rc_dashboard.reducers import INSTANCE_CAP, top_by   # noqa: E402
from rc_dashboard.reducers import phase_names       # noqa: E402
from rc_dashboard.reducers.resources import ResourceReducer   # noqa: E402
from rc_dashboard.windows import ViewFilter         # noqa: E402

TS = "%Y-%m-%d %H:%M:%S"


def rec(t, pid, user="alice", agent_type="claude_code", event="exit", host="h1", **kw):
    r = {"schema_version": 7, "event": event, "ts": time.strftime(TS, time.localtime(t)),
         "ts_epoch": t, "host": host, "user": user, "actor3": "agent" if agent_type else "human",
         "agent_type": agent_type, "pid": pid, "ppid": 1, "comm": "git", "args": "git status",
         "cpu_s": 0.1, "duration_s": 1.0, "session_key": "apid:%d" % (pid % 7 + 100)}
    r.update(kw)
    return r


class Scratch(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="rc_speed_")
        self.t0 = time.time() - 3600

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def write(self, name, recs, raw=b"", compact=False):
        p = os.path.join(self.dir, name)
        sep = (",", ":") if compact else None
        with open(p, "wb") as fh:
            for r in recs:
                fh.write(json.dumps(r, separators=sep).encode() + b"\n")
            fh.write(raw)
        return p

    def gzip(self, p):
        with open(p, "rb") as src, gzip.open(p + ".gz", "wb") as dst:
            shutil.copyfileobj(src, dst)
        os.unlink(p)
        return p + ".gz"


class FoundTest(Scratch):
    """`window_scan(match=, find=)` against `window_scan(match=)`."""

    def recs(self, n=600):
        rnd = random.Random(3)
        out = []
        for i in range(n):
            user = rnd.choice(["alice", "bob", "carol", "dave"])
            at = rnd.choice(["claude_code", "codex", None])
            ev = rnd.choice(["exit", "exit", "residency", "conn"])
            out.append(rec(self.t0 + i, 1000 + i, user=user, agent_type=at, event=ev,
                           args="x" * rnd.randint(0, 900)))
        return out

    def scan(self, path, flt, find, until=None, block=None):
        cfg = configmod.Config(configmod.defaults(), {}, None)
        entry = {"path": path, "start": 0, "size": os.path.getsize(path)}
        norm = Normalizer(cfg)
        events = ("exit", "truncated", "residency")
        with mock.patch.object(export, "BLOCK", block or export.BLOCK):
            got = [(r["_ts_epoch"], r["pid"]) for r, _st in export.window_scan(
                [entry], norm, self.t0 - 10, until, events=events,
                match=flt.line_match, find=flt.line_find if find else None)]
        return got, entry["end"], entry

    def check(self, path, flt, until=None):
        line, end_l, e_l = self.scan(path, flt, False, until)
        for block in (None, 512, 4096):
            found, end_f, e_f = self.scan(path, flt, True, until, block)
            self.assertEqual(found, line, block)
            self.assertEqual(end_f, end_l, block)
        self.assertTrue(line)
        return line

    def test_a_user_and_an_agent_type_filter_find_what_the_line_scan_reads(self):
        p = self.write("h1.exits.jsonl", self.recs())
        for flt in (ViewFilter(["bob"]), ViewFilter(agent_types=["codex"]),
                    ViewFilter(["carol", "dave"], ["claude_code"])):
            self.check(p, flt)

    def test_a_half_written_last_line_and_a_compressed_file(self):
        p = self.write("h1.exits.jsonl", self.recs(), raw=json.dumps(
            rec(self.t0 + 999, 9, user="bob")).encode()[:-5])
        want = self.check(p, ViewFilter(["bob"]))
        self.assertNotIn(9, [pid for _, pid in want])
        g = self.gzip(p)
        self.assertEqual(self.check(g, ViewFilter(["bob"])), want)

    def test_lines_passed_over_are_counted_as_the_line_scan_counts_them(self):
        p = self.write("h1.exits.jsonl", self.recs())
        with open(p, "rb") as fh:
            lines = fh.read().splitlines(keepends=True)
        with open(p, "wb") as fh:                    # blank and whitespace-only lines among them
            for i, line in enumerate(lines):
                fh.write(line)
                if i % 7 == 0:
                    fh.write(b"\n" if i % 2 else b" \t\r\n")
        flt = ViewFilter(["bob"])
        cfg = configmod.Config(configmod.defaults(), {}, None)

        def counters(find, block=None):
            entry = {"path": p, "start": 0, "size": os.path.getsize(p)}
            st = None
            with mock.patch.object(export, "BLOCK", block or export.BLOCK):
                for _rec, st in export.window_scan(
                        [entry], Normalizer(cfg), self.t0 - 10, events=("exit", "residency"),
                        match=flt.line_match, find=flt.line_find if find else None):
                    pass
            return dict(st), entry["end"]
        want = counters(False)
        self.assertTrue(want[0]["skipped"])
        for block in (None, 512, 4096):
            self.assertEqual(counters(True, block), want, block)

    def test_lines_passed_over_leave_out_the_blank_ones_the_regex_left_out(self):
        # `_passed` counts blank lines only where one could begin; it must
        # leave out what matching `_BLANK` across the whole range left out.
        rnd = random.Random(5)
        parts = [b'{"event": "exit"}', b"", b" ", b"\t", b"\r", b"\x0b", b"\x0c", b" {}", b"x"]
        for _ in range(400):
            body = b"".join(b"".join(rnd.choice(parts) for _ in range(rnd.randint(0, 3)))
                            + rnd.choice([b"\n", b"\r\n"]) for _ in range(rnd.randint(0, 10)))
            starts = [0] + [i + 1 for i, c in enumerate(body) if c == 10]
            for i, a in enumerate(starts):
                for b in starts[i:]:
                    n = body.count(b"\n", a, b)
                    want = n - len(export._BLANK.findall(body, a, b)) if n else 0
                    self.assertEqual(export._passed(body, a, b), want, (body, a, b))

    def test_until_stops_where_the_line_scan_stops(self):
        p = self.write("h1.exits.jsonl", self.recs(), compact=True)
        got = self.check(p, ViewFilter(["alice"]), until=self.t0 + 200)
        self.assertTrue(all(t <= self.t0 + 200 for t, _ in got))


class MemoTest(Scratch):

    def test_a_memoized_probe_answers_as_a_fresh_read_and_not_for_a_changed_file(self):
        recs = [rec(self.t0 + i, i) for i in range(500)]
        p = self.write("h1.exits.jsonl", recs)
        for since in (self.t0 + 7, self.t0 + 250, self.t0 + 499, self.t0 + 900):
            with open(p, "rb") as fh:
                export._memo.clear()
                fresh = export.seek_to_epoch(fh, since, os.path.getsize(p))
                again = export.seek_to_epoch(fh, since, os.path.getsize(p))
            self.assertEqual(again, fresh)
        with open(p, "rb") as fh:
            before = export.seek_to_epoch(fh, self.t0 + 600, os.path.getsize(p))
            digest = export.head_digest(fh, 4096)
        self.assertEqual(before, os.path.getsize(p))       # nothing that late yet
        with open(p, "ab") as fh:                           # the file grows
            for i in range(500, 700):
                fh.write(json.dumps(rec(self.t0 + i, i)).encode() + b"\n")
        with open(p, "rb") as fh:
            after = export.seek_to_epoch(fh, self.t0 + 600, os.path.getsize(p))
            self.assertEqual(export.head_digest(fh, 4096), digest)
        self.assertLess(after, os.path.getsize(p))


class RankTest(unittest.TestCase):

    def test_nlargest_is_sorted_reverse_cut_ties_in_order(self):
        rnd = random.Random(5)
        items = [(rnd.randint(0, 6), i) for i in range(500)]
        for n in (0, 1, 15, 499, 500, 900):
            self.assertEqual(heapq.nlargest(n, items, key=lambda x: x[0]),
                             sorted(items, key=lambda x: x[0], reverse=True)[:n])


class EvictTest(unittest.TestCase):

    def test_an_ordered_dict_drops_what_a_dict_dropped(self):
        rnd = random.Random(9)
        a, b = {}, collections.OrderedDict()
        for i in range(5000):
            k = rnd.randint(0, 900)
            for d in (a, b):
                if k not in d and len(d) >= 300:
                    procindex.pop_oldest(d)
                d.setdefault(k, i)
        self.assertEqual(list(a.items()), list(b.items()))

    def test_the_process_index_keeps_what_it_kept(self):
        cfg = configmod.Config(configmod.defaults(), {}, None)
        cfg.set("reducers.sessions.proc_index_max", 50, "test")
        idx = procindex.ProcIndex(cfg)
        ref = {}
        for i in range(400):
            r = {"pid": i, "ppid": i - 1, "host": "h", "_role": "agent", "_command": "x",
                 "user": "u", "event": "exit"}
            idx.feed(r)
        for i in range(350, 400):
            ref[("h", i)] = True
        self.assertEqual(list(idx.entries), list(ref))
        self.assertEqual(idx.evicted, 350)


class FloorTest(unittest.TestCase):

    def test_the_served_worst_lists_are_the_lists_without_a_floor(self):
        cfg = configmod.Config(configmod.defaults(), {}, None)
        rnd = random.Random(11)
        recs = []
        for i in range(3000):
            recs.append({"event": "exit", "_a3": rnd.choice(["agent", "human"]),
                         "_ts_epoch": time.time() - 3000 + i, "pid": i, "user": "u",
                         "sched_wait_s": rnd.choice([0, 0.001, 0.5, rnd.random() * 10]),
                         "maj_flt": rnd.choice([0, 0, 1, rnd.randint(1, 50)]),
                         "dstate_wait_s": rnd.choice([0, 0, rnd.random()]),
                         "cpu_s": 0.1, "duration_s": 1.0, "sched_run_s": 0.1})
        fast = ResourceReducer(cfg, ["agent", "human"])
        slow = ResourceReducer(cfg, ["agent", "human"])
        with mock.patch.object(ResourceReducer, "_keep_worst",
                               lambda self, attr, field, w: (getattr(self, attr).append(w),
                                                             top_by(getattr(self, attr),
                                                                    lambda x: -(x.get(field) or 0)))):
            for r in recs:
                slow.feed(dict(r))
        for r in recs:
            fast.feed(dict(r))
        a, b = fast.result(), slow.result()
        for k in ("worst_stall", "worst_fault", "worst_wait"):
            self.assertEqual(a[k], b[k], k)
            self.assertEqual(len(a[k]), INSTANCE_CAP)


class LiveTest(unittest.TestCase):

    def test_cached_rows_are_the_rows_and_move_when_a_record_lands(self):
        b = RollingBuckets(window_s=3600, bin_s=60)
        now = time.time()
        for i in range(0, 3000, 7):
            b.add({"_ts_epoch": now - i}, "agent", cpu_s=0.5)
        first = b.series(["agent", "human"], "n", now=now)
        again = b.series(["agent", "human"], "n", now=now)
        self.assertEqual(first, again)
        b.add({"_ts_epoch": now - 5}, "human", cpu_s=1.0)
        moved = b.series(["agent", "human"], "n", now=now)
        fresh = RollingBuckets(window_s=3600, bin_s=60)
        fresh.bins = {k: {c: dict(v) for c, v in s.items()} for k, s in b.bins.items()}
        self.assertEqual(moved, fresh.series(["agent", "human"], "n", now=now))
        self.assertNotEqual(moved, first)

    def test_backfill_rows_the_tails_would_drop_are_not_made(self):
        from rc_dashboard.reducers import live as livemod
        cfg = configmod.Config(configmod.defaults(), {}, None)
        cfg.set("live.event_tail", 20, "test")
        a = livemod.LiveReducer(cfg, ["agent", "human", "ide", "unlabeled"])
        b = livemod.LiveReducer(cfg, ["agent", "human", "ide", "unlabeled"])
        norm = Normalizer(cfg)
        now = time.time()
        raws = [rec(now - i, 2000 + i, user="u%d" % (i % 3)) for i in range(400)]
        for r in raws:
            a.feed(norm.normalize(dict(r)), older=True)
        with mock.patch.object(livemod, "_full", lambda tail: False):
            for r in raws:
                b.feed(norm.normalize(dict(r)), older=True)
        self.assertEqual(list(a.event_tail), list(b.event_tail))
        self.assertEqual({u: list(t) for u, t in a.user_tails.items()},
                         {u: list(t) for u, t in b.user_tails.items()})


class PureMemoTest(unittest.TestCase):

    def test_memoized_functions_answer_as_their_bodies(self):
        for t in ("git", "python3.11", "squeue", "  'node' ", "", None, "srun", "codex-linux"):
            self.assertEqual(tool_buckets.bucket(t), tool_buckets.bucket.__wrapped__(t))
        for args in ("node /x/claude -p", "bash -c 'ls'", "codex exec x", None, ""):
            for at, cmd in (("claude_code", "node"), (None, "bash"), ("codex", "codex")):
                self.assertEqual(roles.role_of(at, cmd, args), roles._role_of(at, cmd, args))
        for text in ("git commit -m 'fix the parser'", "gh pr create --title 'X'", "", "ls"):
            self.assertEqual(phase_names.tagset(text), phase_names.tagset.__wrapped__(text))

    def test_redact_forgets_what_it_masked_when_the_rules_grow(self):
        text = "see ticket SITE-AAAAAAAAAAAA now"
        before = redact.redact(text)
        saved = list(redact._EXTRA)
        try:
            redact.extend([r"SITE-[A-Z]{12}"])
            self.assertNotEqual(redact.redact(text), before)
            self.assertIn(redact.MASK, redact.redact(text))
        finally:
            redact._EXTRA[:] = saved
            redact._redact_memo.cache_clear()
        self.assertEqual(redact.redact(text), before)

    def test_tagset_forgets_what_it_masked_when_the_rules_grow(self):
        text = "git commit -m 'rotate SITE-BBBBBBBBBBBB today'"
        before = phase_names.tagset(text)
        self.assertNotIn(redact.MASK, json.dumps(before))
        saved = list(redact._EXTRA)
        try:
            redact.extend([r"SITE-[A-Z]{12}"])
            after = phase_names.tagset(text)
            self.assertIn(redact.MASK, json.dumps(after))
            self.assertEqual(after, phase_names.tagset.__wrapped__(text))
        finally:
            redact._EXTRA[:] = saved
            redact._redact_memo.cache_clear()
            phase_names.tagset.cache_clear()
        self.assertEqual(phase_names.tagset(text), before)

    def test_the_memos_keep_short_texts_and_a_bounded_count(self):
        redact._redact_memo.cache_clear()
        phase_names.tagset.cache_clear()
        roles._role_memo.cache_clear()
        long = "echo " + "x" * 2000
        for fn in (redact.redact, phase_names.tagset):
            fn(long)
            fn("ls -la /tmp")
        self.assertEqual(redact._redact_memo.cache_info().currsize, 1)
        self.assertEqual(phase_names.tagset.cache_info().currsize, 1)
        self.assertEqual(redact._redact_memo.cache_info().maxsize, redact.MEMO_MAX)
        self.assertEqual(phase_names.tagset.cache_info().maxsize, 16384)
        # an agent root's argv recurs with its session, long or not: kept up to 4 KB
        roles.role_of("claude_code", "node", "node /x/claude -p " + "y" * 3000)
        roles.role_of("claude_code", "node", "node /x/claude -p " + "y" * 5000)
        self.assertEqual(roles._role_memo.cache_info().currsize, 1)
        self.assertEqual(roles._role_memo.cache_info().maxsize, roles.ROLE_MEMO_MAX)

    def test_health_reports_each_memo_against_its_bound(self):
        from rc_dashboard import app as appmod
        st = appmod.memo_status()
        self.assertEqual(set(st), {"redact", "role_of", "tagset", "tool_bucket", "category_of",
                                   "category_of_argv"})
        for v in st.values():
            self.assertEqual(set(v), {"hits", "misses", "size", "max"})
            self.assertLessEqual(v["size"], v["max"])

    def test_a_normalizer_no_longer_grows_sys_path(self):
        cfg = configmod.Config(configmod.defaults(), {}, None)
        Normalizer(cfg)
        n = len(sys.path)
        for _ in range(50):
            Normalizer(cfg)
        self.assertEqual(len(sys.path), n)


class MaterializeTest(unittest.TestCase):

    def view(self):
        from rc_dashboard.windows import PanelView
        cfg = configmod.Config(configmod.defaults(), {}, None)
        v = PanelView(cfg, ["agent", "human", "ide", "unlabeled"], 60)
        v.reducers = v.fresh_reducers()
        norm = Normalizer(cfg)
        now = time.time()
        for i in range(300):
            r = norm.normalize(rec(now - 1000 + i, 3000 + i, user="u%d" % (i % 4)))
            for red in v.build_dispatch(v.reducers).get(r.get("event"), ()):
                red.feed(r)
        return v

    def test_stepwise_is_the_payload_materialize_makes(self):
        v = self.view()
        lock = threading.RLock()
        whole = v.materialize()
        step = v.materialize_stepwise(lock)
        self.assertEqual(json.loads(json.dumps(step, default=str)),
                         json.loads(json.dumps(whole, default=str)))

    def test_a_payload_read_from_replaced_reducers_is_not_swapped_in(self):
        v = self.view()
        v.payload = {"mine": 1}
        lock = threading.RLock()
        first = next(iter(v.reducers))
        real = v.reducers[first].result

        def swap():
            v.reducers = dict(v.reducers)               # a build installed new ones
            return real()
        with mock.patch.object(v.reducers[first], "result", swap):
            v.materialize_stepwise(lock)
        self.assertEqual(v.payload, {"mine": 1})


if __name__ == "__main__":
    unittest.main()
