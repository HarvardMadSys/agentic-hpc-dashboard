"""The page stays answerable: what moved off the event loop and out of the lock serves
exactly what it served before.

* `jsonout` writes what FastAPI's `JSONResponse` wrote after `jsonable_encoder`,
  and `dumps_str` what `json.dumps(default=str)` wrote;
* the anonymizer's memo of rewrites gives every string the rewrite a fresh one
  would, before and after a name is learned or forgotten;
* a poll dispatched a slice per lock hold feeds the same records, in the same
  order, as one hold; positioning files side by side hands the tail the same
  offsets and plans the same read-back as one after another; a file unchanged
  on disk is not opened again; the node tier's last lines are `readlines()`'s;
* a backfill normalized on its own normalizer counts what the shared one would;
* a live frame is reused only while nothing new was dispatched, and only frames
  that can still be reused are kept;
* a static asset is never gzipped on the thread that serves it, and goes out
  gzipped once a worker has compressed it.

    cd dashboard && .venv/bin/python -m unittest tests.test_responsiveness -q
"""
import collections
import datetime
import gzip
import json
import os
import random
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi.encoders import jsonable_encoder      # noqa: E402
from fastapi.responses import JSONResponse         # noqa: E402

from rc_dashboard import aggregator as aggmod       # noqa: E402
from rc_dashboard import app as appmod              # noqa: E402
from rc_dashboard import anonymize, gz, jsonout     # noqa: E402
from rc_dashboard import tail                       # noqa: E402
from rc_dashboard.aggregator import Aggregator      # noqa: E402
from test_ingest import NONE, FeedCase, exit_rec, line, totals_rec   # noqa: E402


def old_json(obj):
    """What a route returning `obj` used to send."""
    return JSONResponse(jsonable_encoder(obj)).body


PAYLOADS = [
    {"a": 1, "b": [1, 2, {"c": None, "d": True, "e": False}], "s": "x/y\n\"q\" é ✓  "},
    {1: "int key", None: "none key", 2.5: "float key", True: "bool key"},
    {"t": (1, "two", (3,)), "nested": {"rows": [{"user": "u", "n": 0}] * 3}},
    {"set": {"only"}, "fs": frozenset(["one"]), "dq": collections.deque([3, 1, 2])},
    collections.OrderedDict([("z", 1), ("a", 2)]),
    collections.Counter({"x": 3, "y": 1}),
    collections.defaultdict(list, {"k": [1]}),
    {"big": 2 ** 70, "neg": -(2 ** 65)},
    {"when": datetime.datetime(2026, 11, 15, 18, 0, 1, 250), "day": datetime.date(2026, 11, 15)},
    [],
    {},
    "plain",
]


class JsonOutTest(unittest.TestCase):

    def test_bytes_mean_what_jsonresponse_meant(self):
        for p in PAYLOADS:
            with self.subTest(p=repr(p)[:60]):
                self.assertEqual(json.loads(jsonout.dumps(p)), json.loads(old_json(p)))

    def test_bytes_are_jsonresponses_when_no_float_is_spelled(self):
        for p in PAYLOADS:
            if isinstance(p, dict) and any(isinstance(k, float) for k in p):
                continue
            with self.subTest(p=repr(p)[:60]):
                self.assertEqual(jsonout.dumps(p), old_json(p))

    def test_floats_parse_to_the_same_number(self):
        vals = [0.1, 1e-07, 2.5e-05, 1e16, 1.5e300, -0.0, 1 / 3, 123456789.125]
        self.assertEqual(json.loads(jsonout.dumps(vals)), json.loads(old_json(vals)))

    def test_dumps_str_is_json_dumps_with_default_str(self):
        for p in PAYLOADS + [{"obj": object.__new__(object).__class__}]:
            with self.subTest(p=repr(p)[:60]):
                try:
                    want = json.loads(json.dumps(p, default=str))
                except TypeError:
                    continue            # keys json.dumps itself refuses
                self.assertEqual(json.loads(jsonout.dumps_str(p)), want)

    def test_frames_are_the_old_frames(self):
        p = {"window": {"minutes": 60}, "rows": [{"user": "a", "set": {"x"}}]}
        self.assertEqual(json.loads(jsonout.frame("live", p)),
                         json.loads(json.dumps({"type": "live", "payload": p}, default=str)))

    def test_gzip_only_for_a_reader_that_takes_it_and_a_body_worth_it(self):
        class Req:
            def __init__(self, ae):
                self.headers = {"accept-encoding": ae} if ae is not None else {}
        big = {"rows": [{"user": "u%d" % i, "n": i} for i in range(500)]}
        r = jsonout.response(big, Req("gzip, deflate, br"))
        self.assertEqual(r.headers["content-encoding"], "gzip")
        self.assertEqual(json.loads(gzip.decompress(r.body)), json.loads(old_json(big)))
        for ae in (None, "identity", "br", "gzip;q=0x"):
            r = jsonout.response(big, Req(ae))
            if ae == "gzip;q=0x":
                continue                # malformed but names gzip: either answer is fine
            self.assertNotIn("content-encoding", r.headers)
            self.assertEqual(r.body, jsonout.dumps(big))
        small = jsonout.response({"ok": True}, Req("gzip"))
        self.assertNotIn("content-encoding", small.headers)

    def test_a_zero_quality_refuses_gzip(self):
        # q=0 is a refusal (RFC 9110 12.5.3): such a reader gets the body as it is
        for ae in ("gzip;q=0, identity;q=1", "gzip; q=0", "GZIP;Q=0.0", "x-gzip;q=0",
                   "*;q=1, gzip;q=0", "gzip;q=0, *", "*;q=0", "deflate, br"):
            self.assertFalse(jsonout.gzip_ok(ae), ae)
        for ae in ("gzip", "gzip;q=0.5, identity", "x-gzip", "*", "identity;q=0, *;q=0.1",
                   "br;q=1.0, gzip;q=0.8, *;q=0.1"):
            self.assertTrue(jsonout.gzip_ok(ae), ae)

        class Req:
            headers = {"accept-encoding": "gzip;q=0, identity;q=1"}
        big = {"rows": [{"user": "u%d" % i, "n": i} for i in range(500)]}
        r = jsonout.response(big, Req())
        self.assertNotIn("content-encoding", r.headers)
        self.assertEqual(r.body, jsonout.dumps(big))


class AnonMemoTest(unittest.TestCase):

    STRINGS = ["/home/alice/proj/x.py", "alice ran it", "bob.smith and bob-smith",
               "carol_lab results", "/n/holylabs/LABS/kempner_grads/Lab/alice/run",
               "ssh me@fas.harvard.edu", "/tmp/claude-1234/x", "user-1234.slice",
               "nothing here", "sbatch -A carol_lab job.sh", "x" * 150 + " alic"]

    def fresh(self, names=("alice", "bob.smith")):
        return anonymize.Anonymizer("salt", known_users=names)

    def test_a_memoized_rewrite_is_the_rewrite(self):
        a = self.fresh()
        for _ in range(3):              # the second and third reads come from the memo
            for s in self.STRINGS:
                self.assertEqual(a._rw(s), a._rewrite(s), s)

    def test_a_name_learned_later_is_masked_in_a_string_seen_before(self):
        a = self.fresh(("alice",))
        before = a._rw("carol ran it")
        a.learn_user("carol")
        after = a._rw("carol ran it")
        self.assertNotEqual(before, after)
        self.assertEqual(after, self.fresh(("alice", "carol"))._rw("carol ran it"))

    def test_a_system_account_drops_the_memo(self):
        a = self.fresh(("alice",))
        s = "/n/home08/svcacct/run.sh"
        masked = a._rw(s)
        a.learn_user("svcacct", uid=42)     # system: its home is no longer masked
        self.assertEqual(a._rw(s), a._rewrite(s))
        self.assertNotEqual(masked, a._rw(s))

    def test_payloads_match_a_memo_less_anonymizer(self):
        rnd = random.Random(7)
        words = ["alice", "bob.smith", "carol", "dave_lab", "/home/erin/x", "plain", "git",
                 "LABS/kempner_grads/Lab/frank", "me@fas.harvard.edu", "uid=1234"]
        memo = self.fresh(())
        for i in range(40):
            p = {"user": rnd.choice(["alice", "carol", "gina"]),
                 "rows": [{"args": " ".join(rnd.choice(words) for _ in range(5)),
                           rnd.choice(words): rnd.randint(0, 9)} for _ in range(5)]}
            ref = self.fresh(())
            # the reference has learned exactly what the memoized one has so far
            for n in list(memo.users):
                ref.learn_user(n)
            for lab in list(memo.labs):
                ref.learn_lab(lab)
            with mock.patch.object(anonymize.Anonymizer, "_rw", anonymize.Anonymizer._rewrite):
                want = ref.payload(json.loads(json.dumps(p)))
            self.assertEqual(memo.payload(json.loads(json.dumps(p))), want, i)


class PollTest(FeedCase):

    def burst(self, n=1000):
        return [exit_rec(self.now - 30 + i * 0.01, pid=5000 + i,
                         actor3="agent" if i % 3 else "human") for i in range(n)]

    def test_a_poll_held_a_slice_at_a_time_feeds_what_one_hold_fed(self):
        self.append("h1.exits.jsonl", self.thirty_exits())
        a, b = Aggregator(self.cfg()), Aggregator(self.cfg())
        for agg in (a, b):
            agg.backfill_now()
        self.append("h1.exits.jsonl", self.burst())
        self.append("h1.snapshot.jsonl", [totals_rec(self.now - 5, 7)])
        n = a.poll_once()
        with b.lock:                    # the old way: one hold, the generator inside it
            m = b.consume(b.source.poll())
        self.assertEqual(n, m)
        self.assertEqual(n, 1001)
        pa, pb = a.live_payload(1440), b.live_payload(1440)
        for k in ("now", "events", "rate", "users", "hosts"):
            self.assertEqual(pa[k], pb[k], k)
        self.assertEqual(a.norm.stats["schema_versions"], b.norm.stats["schema_versions"])

    def test_an_unchanged_file_is_not_opened_again(self):
        self.append("h1.exits.jsonl", self.thirty_exits())
        agg = Aggregator(self.cfg())
        agg.backfill_now()
        agg.poll_once()
        opened = []
        real = gz.open_feed

        def counting(path, *a, **k):
            opened.append(os.path.basename(path))
            return real(path, *a, **k)
        with mock.patch.object(tail.gz, "open_feed", counting):
            self.assertEqual(agg.poll_once(), 0)
            self.assertEqual(opened, [])
            self.append("h1.exits.jsonl", [exit_rec(self.now - 1, pid=9)])
            self.assertEqual(agg.poll_once(), 1)
            self.assertEqual(opened, ["h1.exits.jsonl"])

    def test_a_partial_line_is_read_again_until_it_is_whole(self):
        agg = Aggregator(self.cfg(backfill_hours=0))
        agg.backfill_now()
        rec = line(exit_rec(self.now - 1, pid=77))
        self.append("h1.exits.jsonl", [], raw=rec[:20])
        self.assertEqual(agg.poll_once(), 0)
        self.assertEqual(agg.poll_once(), 0)
        with open(self.path("h1.exits.jsonl"), "ab") as fh:
            fh.write(rec[20:])
        self.assertEqual(agg.poll_once(), 1)

    def test_positioning_side_by_side_plans_what_one_file_at_a_time_planned(self):
        for h in range(6):
            self.append("h%d.exits.jsonl" % h, self.thirty_exits())
            self.append("h%d.snapshot.jsonl" % h, [totals_rec(self.now - 60 * k, k)
                                                   for k in range(20, 0, -1)])
        self.compress("h0.exits.jsonl")
        self.compress("h1.snapshot.jsonl")
        since = self.now - 3600
        got = {}
        for workers in (1, 4):
            with mock.patch.object(tail, "POSITION_WORKERS", workers):
                src = tail.FileTailSource([self.root], days=8)
                got[workers] = (src.position(since), src.state)
        self.assertEqual(got[1][0], got[4][0])
        self.assertEqual(got[1][1], got[4][1])
        self.assertTrue(got[4][0])

    def test_a_failed_file_stops_positioning_where_one_pass_would(self):
        for h in range(4):
            self.append("h%d.exits.jsonl" % h, self.thirty_exits())
        real = tail.seek_to_epoch

        def boom(fh, since, end):
            if os.path.basename(getattr(fh, "name", "")) == "h2.exits.jsonl":
                raise RuntimeError("bad file")
            return real(fh, since, end)
        states = {}
        for workers in (1, 4):
            src = tail.FileTailSource([self.root], days=8)
            with mock.patch.object(tail, "POSITION_WORKERS", workers), \
                    mock.patch.object(tail, "seek_to_epoch", boom):
                with self.assertRaises(RuntimeError):
                    src.position(self.now - 3600)
            states[workers] = sorted(os.path.basename(p) for p in src.state)
        self.assertEqual(states[1], states[4])
        self.assertEqual(states[4], ["h0.exits.jsonl", "h1.exits.jsonl"])


class LastLinesTest(FeedCase):

    def check(self, raw, n, compressed=False):
        p = self.path("node.jsonl")
        with open(p, "wb") as fh:
            fh.write(raw)
        if compressed:
            self.compress("node.jsonl")
            p += ".gz"
        with gz.open_text(p) as fh:
            want = fh.readlines()[-n:]
        for window in (8, 64, 1 << 20):
            self.assertEqual(aggmod.last_lines(p, n, window=window), want, (raw[:40], n, window))

    def test_last_lines_are_readlines(self):
        big = b"".join(b'{"i": %d, "pad": "%s"}\n' % (i, b"x" * (i * 37 % 200)) for i in range(50))
        for raw in (big, big + b'{"partial": ', b"", b"one\n", b"one", b"\n\n", b"a\nb"):
            for n in (1, 2, 5, 80):
                self.check(raw, n)
        self.check(big, 2, compressed=True)


class BackfillTest(FeedCase):

    def test_the_backfills_own_normalizer_counts_what_the_shared_one_would(self):
        recs = self.thirty_exits()
        recs[3] = dict(recs[3], schema_version=5)
        self.append("h1.exits.jsonl", recs)
        agg = Aggregator(self.cfg())
        agg.backfill_now()
        rep = agg.refresh_feeds()["ebpf"]
        self.assertEqual(rep["schema_versions"], {6: 29, 5: 1})
        self.assertEqual(agg.backfill["records"], 30)
        self.assertEqual(agg.live_payload(1440)["now"]["events"], dict(NONE, agent=20, human=10))


class LiveFrameTest(FeedCase):

    def test_a_frame_is_reused_only_while_nothing_new_was_dispatched(self):
        self.append("h1.exits.jsonl", self.thirty_exits())
        agg = Aggregator(self.cfg())
        agg.backfill_now()
        f1 = agg.live_frame(1440)
        self.assertIs(agg.live_frame(1440), f1)
        self.assertEqual(json.loads(f1)["payload"]["now"]["events"],
                         dict(NONE, agent=20, human=10))
        self.append("h1.exits.jsonl", [exit_rec(self.now - 1, pid=9)])
        agg.poll_once()
        f2 = agg.live_frame(1440)
        self.assertEqual(json.loads(f2)["payload"]["now"]["events"],
                         dict(NONE, agent=21, human=10))
        self.assertIsNot(agg.live_frame(1440, max_age_s=0), f2)

    def test_only_frames_that_can_be_reused_are_kept(self):
        self.append("h1.exits.jsonl", self.thirty_exits())
        agg = Aggregator(self.cfg())
        agg.backfill_now()
        # a reader cycling through windows: the most recent FRAMES_MAX are held
        for m in range(1, 60):
            agg.live_frame(m, max_age_s=3600)
        self.assertEqual(list(agg._frames), list(range(60 - Aggregator.FRAMES_MAX, 60)))
        # a frame past its age is never served again, so the next one drops it
        agg.live_frame(1440, max_age_s=0)
        self.assertEqual(list(agg._frames), [1440])


class CachedStaticTest(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.dir, "assets"))
        self.data = b"console.log('the page');\n" * 400
        with open(os.path.join(self.dir, "assets", "index-0a1b2c.js"), "wb") as fh:
            fh.write(self.data)
        with open(os.path.join(self.dir, "index.html"), "wb") as fh:
            fh.write(b"<!doctype html><title>page</title>")
        appmod.CachedStatic._gz.clear()
        self.static = appmod.CachedStatic(directory=self.dir, html=True)

    def tearDown(self):
        for t in threading.enumerate():
            if t.name == "static-gzip":
                t.join(30)
        shutil.rmtree(self.dir, ignore_errors=True)
        appmod.CachedStatic._gz.clear()

    def get(self, rel, accept="gzip, deflate, br"):
        full = os.path.join(self.dir, rel)
        headers = [(b"accept-encoding", accept.encode())] if accept else []
        scope = {"type": "http", "method": "GET", "path": "/" + rel, "headers": headers}
        return self.static.file_response(full, os.stat(full), scope)

    def test_an_asset_is_never_gzipped_on_the_serving_thread(self):
        threads = []
        real = gzip.compress

        def spy(*a, **k):
            threads.append(threading.current_thread())
            return real(*a, **k)
        with mock.patch.object(appmod.gzip, "compress", spy):
            first = self.get("assets/index-0a1b2c.js")
            for t in threading.enumerate():
                if t.name == "static-gzip":
                    t.join(30)
        # the first reader got the file as it is, while a worker compressed it
        self.assertNotIn("content-encoding", first.headers)
        self.assertEqual(first.headers["vary"], "Accept-Encoding")
        self.assertEqual(len(threads), 1)
        self.assertIsNot(threads[0], threading.current_thread())
        second = self.get("assets/index-0a1b2c.js")
        self.assertEqual(second.headers["content-encoding"], "gzip")
        self.assertEqual(gzip.decompress(second.body), self.data)
        self.assertEqual(second.headers["cache-control"], "public, max-age=31536000, immutable")

    def test_warm_compresses_every_asset_before_a_reader_asks(self):
        self.assertEqual(self.static.warm(), 1)
        with mock.patch.object(appmod.gzip, "compress", side_effect=AssertionError):
            r = self.get("assets/index-0a1b2c.js")
            plain = self.get("assets/index-0a1b2c.js", accept="")
        self.assertEqual(gzip.decompress(r.body), self.data)
        self.assertNotIn("content-encoding", plain.headers)
        self.assertEqual(plain.headers["vary"], "Accept-Encoding")

    def test_a_reader_that_refuses_gzip_gets_the_asset_as_it_is(self):
        self.static.warm()
        r = self.get("assets/index-0a1b2c.js", accept="gzip;q=0, identity;q=1")
        self.assertNotIn("content-encoding", r.headers)
        self.assertEqual(r.headers["vary"], "Accept-Encoding")

    def test_a_changed_asset_is_not_served_from_its_old_bytes(self):
        self.static.warm()
        p = os.path.join(self.dir, "assets", "index-0a1b2c.js")
        with open(p, "ab") as fh:
            fh.write(b"// more\n")
        r = self.get("assets/index-0a1b2c.js")
        self.assertNotIn("content-encoding", r.headers)

    def test_the_page_itself_is_revalidated(self):
        self.assertEqual(self.get("index.html").headers["cache-control"], "no-cache")


if __name__ == "__main__":
    unittest.main()
