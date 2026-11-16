"""Demo mode: a synthetic feed the real service reads, and that never passes for a measurement.

* the feed is a function of seed, hosts and clock: any cut of a span into windows writes the
  same records -- JSONL: the same bytes; EBR: the same decoded records -- so a restarted demo
  resumes exactly, even after a round cut off mid-write (a torn line, a torn frame);
* it is written as EBR (the default), JSONL, or both as twins that decode alike, each laid
  out as the collector lays it out; another format is another feed, written afresh;
* every record says `synthetic`, every file is in time order, and a child's exit comes
  before its parent's -- what the tail, the backfill and the reducers rely on;
* the demo reads only its own feed, whatever the shell exports, and the twin it writes;
* one session of every kind, through the reducers a window build uses, fills what each tab
  needs: tasks quoted from commits, every launch surface and approval state, sandboxed calls
  isolated and failed, nested and hosted agents, Slurm polling, compute on the login node;
* the collectors' report says which hosts are synthetic, which is what the header shows.

    cd dashboard && uv run python -m unittest tests.test_demo -v
"""
import glob
import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from rc_dashboard import config as configmod            # noqa: E402
from rc_dashboard import demo as demomod                 # noqa: E402
from rc_dashboard import ebr                             # noqa: E402
from rc_dashboard.__main__ import build_parser           # noqa: E402
from rc_dashboard.__main__ import main as cli            # noqa: E402
from rc_dashboard.demo import agents, cast               # noqa: E402
from rc_dashboard.demo import feed as feedmod            # noqa: E402
from rc_dashboard.demo.world import (PID_BASE, PID_BLOCK, Spec, World,  # noqa: E402
                                     day_of)
from rc_dashboard.health import CollectorHealth          # noqa: E402
from test_sessions import reduce                         # noqa: E402

# The suffixes each --demo-format writes.
EXTS = {"jsonl": ("jsonl",), "ebr": ("ebr",), "both": ("jsonl", "ebr")}
# What a decoder counts of a file that does not decode whole.
DAMAGE = ("bad_records", "bad_frames", "lost_frames", "lost_records", "unsupported_frames")


def opts(d, **kw):
    o = dict(demomod.DEFAULTS, dir=d, days=1, hosts=2, fresh=False)
    o.update(kw)
    return o


def day_files(d):
    """Every day-file under the feed, either format: {path relative to it: path}."""
    return {os.path.relpath(p, d): p
            for p in sorted(glob.glob(os.path.join(d, "feed", "*", "*", "*")))
            if p.endswith(feedmod.SUFFIXES)}


def feed_records(d):
    """Every record of every day-file under the feed, by path relative to it: JSONL
    line by line, EBR through the codec -- which must decode the whole file, with no
    torn tail and nothing corrupt. Finding no file at all fails: a glob for one
    suffix would pass vacuously over a feed written in the other."""
    out = {}
    for rel, p in day_files(d).items():
        if p.endswith(".ebr"):
            st = {}
            out[rel] = [rec for _pos, rec in ebr.iter_records(p, stats=st)]
            damage = {k: st[k] for k in DAMAGE if st.get(k)}
            if damage or ebr.complete_end(p) != os.path.getsize(p):
                raise AssertionError("%s does not decode whole: %s" % (rel, damage or "a torn tail"))
        else:
            with open(p, "rb") as fh:
                out[rel] = [json.loads(ln) for ln in fh.read().splitlines()]
    if not out:
        raise AssertionError("no day-files under %s" % d)
    return out


def exact(recs):
    """Records as text, which tells 1, 1.0 and true apart and keeps key order: what
    `==` on two dicts does not."""
    return [json.dumps(r) for r in recs]


def exact_feed(d):
    return {rel: exact(recs) for rel, recs in feed_records(d).items()}


def jsonl_bytes(d):
    """Every JSONL day-file's bytes, by path relative to the feed."""
    out = {}
    for rel, p in day_files(d).items():
        if p.endswith(".jsonl"):
            with open(p, "rb") as fh:
                out[rel] = fh.read()
    return out


def segment_of(path):
    """What the first SEGMENT of an EBR file says: its writer's meta among the rest."""
    dec = ebr.Decoder()
    first = next(iter(ebr.frames(path)))
    dec.skip(first, path)
    return first.kind, dec.segment


class FeedTest(unittest.TestCase):
    """What the feed is, whatever its format."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rc_demo_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_windows_cut_anywhere_give_the_same_bytes(self):
        now = time.time()
        start = float(int(now - 86400) // 60 * 60)
        whole = World("demo", 2, start).records(start, now)
        w = World("demo", 2, start)
        cut, t = [], start
        for step in (0.7, 59.3, 3600, 1.1, 7200, 13.0, 40000, 2.0):
            cut += w.records(t, t + step)
            t += step
        cut += w.records(t, now)
        self.assertGreater(len(whole), 5000)
        self.assertEqual(whole, cut)

    def test_another_seed_is_another_set_of_sessions(self):
        now = time.time()
        start = float(int(now - 43200) // 60 * 60)
        a = World("demo", 1, start).records(start, now)
        b = World("other", 1, start).records(start, now)
        self.assertNotEqual([x[4] for x in a], [x[4] for x in b])

    def test_another_format_is_written_afresh(self):
        """The stamp names the format the feed was written in. A demo asked for
        another, or one finding a stamp from before there were two (a JSONL-only
        demo's, which names none), starts the feed again rather than resume
        files it would not write."""
        d = os.path.join(self.tmp, "demo")
        with redirect_stderr(io.StringIO()):
            demomod.Demo(opts(d, hosts=1, format="jsonl")).prepare().stop()
        self.assertTrue(all(k.endswith(".jsonl") for k in feed_records(d)))
        err = io.StringIO()
        with redirect_stderr(err):
            demomod.Demo(opts(d, hosts=1, format="ebr")).prepare().stop()
        self.assertIn("starting a new feed (the feed there has another format (jsonl, not ebr))",
                      err.getvalue())
        self.assertTrue(all(k.endswith(".ebr") for k in feed_records(d)))
        feed = demomod.Feed(d)
        st = feed.load_state()
        self.assertEqual(st["format"], "ebr")
        del st["format"]
        feed.save_state(st)
        err = io.StringIO()
        with redirect_stderr(err):
            demomod.Demo(opts(d, hosts=1, format="ebr")).prepare().stop()
        self.assertIn("another format (jsonl, not ebr)", err.getvalue())
        # the same format, though, resumes
        err = io.StringIO()
        with redirect_stderr(err):
            demomod.Demo(opts(d, hosts=1, format="ebr")).prepare().stop()
        self.assertIn("resuming the ebr feed", err.getvalue())

    def test_an_ebr_file_keeps_its_writer_and_ends_a_frame_with_each_round(self):
        """Rounds a few seconds apart, as the live writer appends them: each file is
        opened (Writer.open_append) once, not once a round, and every frame holds
        at most a minute of one round -- a reader never sees one span two."""
        d = os.path.join(self.tmp, "live")
        now = time.time()
        start = float(int(now - 1500) // 60 * 60)
        world = World("demo", 1, start)
        feed = demomod.Feed(d, "ebr")
        rounds, t = [], start
        with mock.patch.object(ebr.Writer, "open_append", wraps=ebr.Writer.open_append) as opened:
            while t < now:
                t1 = min(now, t + 7.3)
                items = world.records(t, t1)
                feed.append(items)
                rounds.append((t1, items))
                t = t1
        feed.close()
        files = list(day_files(d).values())
        self.assertGreaterEqual(len(files), 3)
        self.assertEqual(opened.call_count, len(files))
        frames = 0
        for p in files:
            for f in ebr.frames(p):
                if f.kind != ebr.DATA:
                    continue
                frames += 1
                self.assertLess(f.ts_max - f.ts_min, 60.01, p)
                # (a submit is stamped a millisecond before its place in the round)
                self.assertFalse([e for e, _ in rounds if f.ts_min < e - 0.01 and f.ts_max > e + 0.01],
                                 (p, f))
        # a frame for each minute of each round a file has records in, and no more
        self.assertEqual(frames, sum(
            len({(feed.path(x[1], x[0], x[2], x[3], "ebr"), x[0] // 60) for x in items})
            for _end, items in rounds))
        # what was written is what the generator gave, record for record
        got = {}
        for x in World("demo", 1, start).records(start, now):
            got.setdefault(feed.path(x[1], x[0], x[2], x[3], "ebr"), []).append(json.loads(x[4]))
        self.assertEqual({os.path.relpath(p, d): exact(r) for p, r in got.items()}, exact_feed(d))

    def test_a_users_cgroup_cpu_counter_only_grows_and_at_a_believable_rate(self):
        """The node reducer reads the difference of two snapshots as CPU used between
        them: a counter that jumps or falls would show a spike or a false reset."""
        now = time.time()
        w = World("demo", 2, now - 1.5 * 86400)
        prev, rates = {}, []
        for x in w.records(w.start, now):
            if x[1] != "node":
                continue
            r = json.loads(x[4])
            for u in r["per_user_cgroup"]:
                k = (r["hostname"], u["user"])
                if k in prev:
                    t0, c0 = prev[k]
                    self.assertGreaterEqual(u["cpu_usage_usec"], c0, k)
                    rates.append((u["cpu_usage_usec"] - c0) / 1e6 / (r["ts_epoch"] - t0))
                prev[k] = (r["ts_epoch"], u["cpu_usage_usec"])
        self.assertTrue(rates)
        self.assertLess(max(rates), 4.0)

    def test_one_writer_per_feed(self):
        d = os.path.join(self.tmp, "demo")
        with redirect_stderr(io.StringIO()):
            first = demomod.Demo(opts(d)).prepare()
            try:
                with self.assertRaises(SystemExit) as cm:
                    demomod.Demo(opts(d)).prepare()
            finally:
                first.stop()
            demomod.Demo(opts(d)).prepare().stop()        # free again once it stops
        self.assertIn("another demo", str(cm.exception))


class EbrCutTest(unittest.TestCase):
    """`_cut_file_ebr`, on files no demo round writes: what a restart cuts back to."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rc_demo_cut_")
        self.recs = [{"ts_epoch": 1000.0 + i, "event": "exit", "host": "h", "n": i}
                     for i in range(10)]

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def ns(self, p):
        return [rec["n"] for _pos, rec in ebr.iter_records(p)]

    def test_frames_past_the_watermark_go_with_the_segment_before_them(self):
        p = os.path.join(self.tmp, "h.exits.ebr")
        with ebr.Writer(p, meta={"host": "h", "stream": "exits"}) as w:
            for rec in self.recs[:4]:
                w.write(rec)
        kept = os.path.getsize(p)
        # a later run's segment, all past the watermark, and a frame of it torn
        with ebr.Writer(p, meta={"host": "h", "stream": "exits"}) as w:
            for rec in self.recs[4:7]:
                w.write(rec)
            w.flush()
            for rec in self.recs[7:]:
                w.write(rec)
        os.truncate(p, os.path.getsize(p) - 5)
        self.assertEqual(feedmod._cut_file_ebr(p, 1003.5), 1)
        self.assertEqual(os.path.getsize(p), kept)
        self.assertEqual(self.ns(p), [0, 1, 2, 3])
        self.assertEqual(feedmod._cut_file_ebr(p, 1003.5), 0)   # nothing more to cut

    def test_a_torn_frame_alone_is_cut(self):
        p = os.path.join(self.tmp, "h.ebr")
        with ebr.Writer(p) as w:
            for rec in self.recs[:3]:
                w.write(rec)
            w.flush()
            for rec in self.recs[3:5]:
                w.write(rec)
        os.truncate(p, os.path.getsize(p) - 1)
        self.assertEqual(feedmod._cut_file_ebr(p, 2000.0), 1)
        self.assertEqual(self.ns(p), [0, 1, 2])
        self.assertEqual(ebr.complete_end(p), os.path.getsize(p))

    def test_a_frame_across_the_watermark_is_cut_inside(self):
        """No round writes one, but a frame holding records on both sides of the
        stamp is decoded, cut, and its records before the stamp written again."""
        p = os.path.join(self.tmp, "h.exits.ebr")
        with ebr.Writer(p, meta={"host": "h", "stream": "exits", "writer": "test"}) as w:
            for rec in self.recs[:4]:
                w.write(rec)
            w.flush()
            for rec in self.recs[4:]:        # one frame, 1004..1009
                w.write(rec)
        self.assertEqual(feedmod._cut_file_ebr(p, 1006.5), 1)
        self.assertEqual(self.ns(p), [0, 1, 2, 3, 4, 5, 6])
        self.assertEqual(ebr.complete_end(p), os.path.getsize(p))
        # one segment still, which a writer resumes
        self.assertEqual(sum(f.kind == ebr.SEGMENT for f in ebr.frames(p)), 1)
        with ebr.Writer.open_append(p) as w:
            w.write(self.recs[7])
        self.assertEqual(self.ns(p), list(range(8)))
        self.assertEqual(sum(f.kind == ebr.SEGMENT for f in ebr.frames(p)), 1)

    def test_an_empty_or_missing_file_is_left_alone(self):
        p = os.path.join(self.tmp, "h.ebr")
        open(p, "wb").close()
        self.assertEqual(feedmod._cut_file_ebr(p, 1000.0), 0)
        self.assertEqual(feedmod._cut_file_ebr(p + ".missing", 1000.0), 0)


class FormatCase:
    """The feed tests that depend on the format the demo writes, run once per format
    by the TestCases below."""
    FORMAT = None

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rc_demo_%s_" % self.FORMAT)
        self.exts = EXTS[self.FORMAT]

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def opts(self, d, **kw):
        return opts(d, format=self.FORMAT, **kw)

    def test_the_feed_is_laid_out_and_marked_as_the_collector_would(self):
        d = os.path.join(self.tmp, "demo")
        with redirect_stderr(io.StringIO()):
            demomod.Demo(self.opts(d)).prepare().stop()
        files = feed_records(d)
        self.assertEqual({k.rsplit(".", 1)[1] for k in files}, set(self.exts))
        for ext in self.exts:
            self.assertTrue(any(k.startswith("feed/ebpf/") and k.endswith(".exits." + ext)
                                for k in files), ext)
            self.assertTrue(any(k.startswith("feed/ebpf/") and k.endswith(".snapshot." + ext)
                                for k in files), ext)
            self.assertTrue(any(k.startswith("feed/node/") and k.endswith("." + ext)
                                for k in files), ext)
        events = set()
        exits = {}
        for path, recs in files.items():
            day = path.split("/")[2]
            self.assertTrue(recs, path)
            last = -1.0
            for r in recs:
                self.assertIs(r.get("synthetic"), True, path)
                ts = r["ts_epoch"]
                self.assertGreaterEqual(ts, last, "%s is not in time order" % path)
                last = ts
                self.assertEqual((r.get("ts") or r.get("timestamp"))[:10], day, path)
                if "/node/" not in path:
                    self.assertEqual(r["schema_version"], 8)
                    events.add(r["event"])
                if r.get("event") == "exit":
                    exits[(r["host"], r["pid"], ts)] = r
        self.assertTrue({"meta", "exit", "residency", "residency_totals", "conn", "netio",
                         "submit"} <= events, events)
        # a child's exit is written before its parent's, and its life lies inside the parent's
        by_pid = {}
        for (host, pid, ts), r in exits.items():
            by_pid.setdefault((host, pid), []).append(r)
        late = 0
        for r in exits.values():
            for par in by_pid.get((r["host"], r["ppid"]), ()):
                p0 = par["ts_epoch"] - par["duration_s"]
                if p0 <= r["ts_epoch"] - r["duration_s"] + 1 and r["ts_epoch"] <= par["ts_epoch"] + 1:
                    late += r["ts_epoch"] > par["ts_epoch"]
        self.assertEqual(late, 0)
        # an EBR file says who wrote it, and holds a minute of records a frame
        for rel, p in day_files(d).items():
            if not p.endswith(".ebr"):
                continue
            kind, seg = segment_of(p)
            self.assertEqual(kind, ebr.SEGMENT, rel)
            host, _, stream = os.path.basename(p)[:-len(".ebr")].partition(".")
            self.assertEqual((seg["host"], seg["stream"], seg["writer"], seg["synthetic"]),
                             (host, stream or "node", "rc_dashboard.demo", True), rel)
            for f in ebr.frames(p):
                if f.kind == ebr.DATA:
                    self.assertLess(f.ts_max - f.ts_min, 60.01, rel)

    def test_a_restart_resumes_without_a_gap_or_a_repeat(self):
        d = os.path.join(self.tmp, "demo")
        t1 = time.time() - 7200
        with redirect_stderr(io.StringIO()):
            first = demomod.Demo(self.opts(d)).prepare(now=t1)
        start = first.state["start"]
        first.stop()
        # a round cut off mid-write: whole records past the stamp, then half of one --
        # in JSONL a torn line, in EBR a whole frame and a torn one
        # The demo's people keep a working day, so how many exits follow t1 depends
        # on the hour the test runs at: widen the span until there are ten. Every
        # record past the stamp is cut on restart, however far past it lies.
        past, hi = [], t1 + 600
        while len(past) < 10 and hi <= t1 + 86400:
            past = [x for x in World("demo", 2, start).records(t1, hi)
                    if x[3] == "exits" and json.loads(x[4])["ts_epoch"] >= t1]
            hi += 3600
        self.assertGreaterEqual(len(past), 10)
        for ext in self.exts:
            exits = sorted(glob.glob(os.path.join(d, "feed", "ebpf", "*", "*.exits." + ext)))[-1]
            if ext == "jsonl":
                with open(exits, "ab") as fh:
                    fh.write(b"".join(x[4] for x in past[:5]))
                    fh.write(b'{"ts_epoch": %f, "event": "exi' % (t1 + 700))
                continue
            w = ebr.Writer.open_append(exits)
            for x in past[:5]:
                w.write(json.loads(x[4]))
            w.flush()
            for x in past[5:10]:
                w.write(json.loads(x[4]))
            w.close()
            os.truncate(exits, os.path.getsize(exits) - 9)
            self.assertLess(ebr.complete_end(exits), os.path.getsize(exits))
        t2 = time.time()
        err = io.StringIO()
        with redirect_stderr(err):
            demomod.Demo(self.opts(d)).prepare(now=t2).stop()
        self.assertIn("(cut %d file(s) back to it)" % len(self.exts), err.getvalue())
        # the same as one writer that never stopped: the same records (and in JSONL,
        # the same bytes) as one append of the whole span
        ref = os.path.join(self.tmp, "ref")
        feed = demomod.Feed(ref, self.FORMAT)
        feed.append(World("demo", 2, start).records(start, t2))
        feed.close()
        self.assertEqual(exact_feed(d), exact_feed(ref))
        self.assertEqual(jsonl_bytes(d), jsonl_bytes(ref))

    def test_a_feed_from_another_seed_or_host_count_is_written_afresh(self):
        d = os.path.join(self.tmp, "demo")
        with redirect_stderr(io.StringIO()):
            demomod.Demo(self.opts(d)).prepare().stop()
        self.assertTrue(any("demo-login02" in k for k in feed_records(d)))
        err = io.StringIO()
        with redirect_stderr(err):
            demomod.Demo(self.opts(d, hosts=1)).prepare().stop()
        self.assertIn("starting a new feed (the feed there has another host count)", err.getvalue())
        self.assertFalse(any("demo-login02" in k for k in feed_records(d)))
        err = io.StringIO()
        with redirect_stderr(err):
            demomod.Demo(self.opts(d, hosts=1, seed="other")).prepare().stop()
        self.assertIn("starting a new feed (the feed there has another seed)", err.getvalue())

    def test_a_longer_history_than_the_feed_holds_is_written_afresh(self):
        """Resuming a 1-day feed for 2 days would leave the wider windows half empty:
        records cannot be put before a file's first one, so the feed starts again."""
        d = os.path.join(self.tmp, "demo")
        with redirect_stderr(io.StringIO()):
            demomod.Demo(self.opts(d, days=1, hosts=1)).prepare().stop()
        err = io.StringIO()
        with redirect_stderr(err):
            longer = demomod.Demo(self.opts(d, days=2, hosts=1)).prepare()
            longer.stop()
        self.assertIn("covers less than the 2 days", err.getvalue())
        self.assertLessEqual(longer.state["start"], time.time() - 2 * 86400 + 120)
        # a shorter history than the feed holds is resumed, not rewritten
        err = io.StringIO()
        with redirect_stderr(err):
            demomod.Demo(self.opts(d, days=1, hosts=1)).prepare().stop()
        self.assertIn("resuming the %s feed" % self.FORMAT, err.getvalue())
        self.assertTrue(feed_records(d))


class JsonlFeedTest(FormatCase, unittest.TestCase):
    FORMAT = "jsonl"


class EbrFeedTest(FormatCase, unittest.TestCase):
    FORMAT = "ebr"


class BothFeedTest(FormatCase, unittest.TestCase):
    FORMAT = "both"

    def test_the_twins_decode_alike(self):
        """Every stream is written twice, and its EBR twin decodes to `json.loads` of
        each line of its JSONL one: the same records in the same order, with the same
        types and the same key order -- through the history's hour-long rounds and
        the live writer's two-second ones."""
        d = os.path.join(self.tmp, "demo")
        t1 = time.time() - 300
        with redirect_stderr(io.StringIO()):
            demo = demomod.Demo(self.opts(d)).prepare(now=t1)
            try:
                while demo.state["written_to"] < t1 + 240:
                    demo._write(demo.state["written_to"], demo.state["written_to"] + demomod.TICK_S)
            finally:
                demo.stop()
        files = day_files(d)
        stems = {rel.rsplit(".", 1)[0] for rel in files}
        self.assertEqual(set(files), {s + ext for s in stems for ext in (".jsonl", ".ebr")})
        for s in sorted(stems):
            with open(files[s + ".jsonl"], "rb") as fh:
                want = [json.loads(ln) for ln in fh.read().splitlines()]
            got = [rec for _pos, rec in ebr.iter_records(files[s + ".ebr"])]
            self.assertTrue(want, s)
            self.assertEqual(exact(got), exact(want), s)


class ConfigTest(unittest.TestCase):
    def test_the_demo_reads_its_own_feed_whatever_the_shell_says(self):
        env = {"RC_DASH_EBPF_ROOTS": "/var/log/ebpfm", "RC_DASH_NODE_ROOTS": "/var/log/ebpfm-login",
               "RC_DASH_SACCT_DIR": "/n/sacct", "RC_DASH_AS_OF": "auto",
               "RC_DASH_SITE_NAME": "FAS RC", "RC_DASH_CACHE_DIR": "/srv/cache",
               "RC_DASH_EBPF_PREFER": "jsonl", "RC_DASH_NODE_PREFER": "jsonl"}
        with mock.patch.dict(os.environ, env):
            cfg = configmod.load(None, None)
            o = opts("/tmp/some-demo", days=2)
            demomod.configure(cfg, o)
        feed = demomod.Feed(o["dir"])
        self.assertEqual(cfg.get("feeds.ebpf.roots"), [feed.ebpf])
        self.assertEqual(cfg.get("feeds.ebpf_node.roots"), [feed.node])
        self.assertTrue(cfg.get("feeds.sacct.dir").startswith(feed.base))
        self.assertEqual(cfg.get("clock.as_of"), "")
        self.assertEqual(cfg.get("cache.dir"), feed.cache)
        self.assertEqual(cfg.get("site.name"), demomod.SITE_NAME)
        self.assertEqual(cfg.get("live.retention_min"), 2 * 1440)
        # The endpoint list names the made-up addresses by the records' own host
        # names: a demo asks no DNS server (endpoints.py).
        self.assertIs(cfg.get("server.rdns.enabled"), False)
        self.assertEqual(cfg.origin("feeds.ebpf.roots"), "demo")
        # and the page's config says where each came from
        tree = cfg.as_dict(with_origins=True)
        self.assertEqual(tree["feeds"]["ebpf"]["roots"]["_origin"], "demo")
        # it reads the twin it writes: EBR, unless it writes only JSONL
        for fmt, prefer in (("ebr", "ebr"), ("both", "ebr"), ("jsonl", "jsonl")):
            with mock.patch.dict(os.environ, env):
                cfg = demomod.configure(configmod.load(None, None), opts(o["dir"], format=fmt))
            for key in ("feeds.ebpf.prefer", "feeds.ebpf_node.prefer"):
                self.assertEqual((cfg.get(key), cfg.origin(key)), (prefer, "demo"), (fmt, key))

    def test_the_format_is_ebr_unless_a_flag_or_the_environment_says(self):
        def fmt(*argv, **env):
            clean = {k: v for k, v in os.environ.items() if k != "RC_DASH_DEMO_FORMAT"}
            clean.update(env)
            with mock.patch.dict(os.environ, clean, clear=True):
                return demomod.options(build_parser().parse_args(["demo"] + list(argv)))["format"]
        self.assertEqual(fmt(), "ebr")
        self.assertEqual(fmt(RC_DASH_DEMO_FORMAT="both"), "both")
        self.assertEqual(fmt(RC_DASH_DEMO_FORMAT=" JSONL "), "jsonl")
        self.assertEqual(fmt("--demo-format", "jsonl", RC_DASH_DEMO_FORMAT="both"), "jsonl")
        with self.assertRaises(SystemExit) as cm:
            fmt(RC_DASH_DEMO_FORMAT="parquet")
        self.assertIn("RC_DASH_DEMO_FORMAT", str(cm.exception))
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            build_parser().parse_args(["demo", "--demo-format", "parquet"])

    def test_a_real_root_is_refused_not_dropped(self):
        err = io.StringIO()
        with redirect_stderr(err):
            code = cli(["demo", "--ebpf-root", "/var/log/ebpfm"])
        self.assertEqual(code, 2)
        self.assertIn("--ebpf-root", err.getvalue())

    def test_print_config_writes_nothing(self):
        d = tempfile.mkdtemp(prefix="rc_demo_cfg_")
        try:
            for argv, prefer in (([], "ebr"), (["--demo-format", "jsonl"], "jsonl")):
                out = io.StringIO()
                with mock.patch("sys.stdout", out), mock.patch.dict(os.environ):
                    os.environ.pop("RC_DASH_DEMO_FORMAT", None)
                    self.assertEqual(cli(["demo", "--print-config", "--demo-dir", d] + argv), 0)
                tree = json.loads(out.getvalue())
                self.assertEqual(tree["site"]["name"]["_value"], demomod.SITE_NAME)
                self.assertEqual(tree["feeds"]["ebpf"]["prefer"], {"_value": prefer, "_origin": "demo"})
                self.assertEqual(tree["feeds"]["ebpf_node"]["prefer"]["_value"], prefer)
                self.assertEqual(os.listdir(d), [])
        finally:
            shutil.rmtree(d, ignore_errors=True)


class HealthTest(unittest.TestCase):
    def test_the_report_says_which_hosts_are_synthetic(self):
        h = CollectorHealth(configmod.Config(configmod.defaults(), {}, None))
        now = time.time()
        for host, mark in (("demo-login01", True), ("login07", None)):
            rec = {"event": "residency_totals", "host": host, "ts_epoch": now - 30,
                   "collector_pid": 7, "interval_s": 60.0}
            if mark:
                rec["synthetic"] = True
            h.feed(rec)
        self.assertEqual(h.synthetic_hosts(), ["demo-login01"])
        rep = {x["host"]: x for x in h.report(now=now)}
        self.assertTrue(rep["demo-login01"]["synthetic"])
        self.assertFalse(rep["login07"]["synthetic"])


def person(user):
    return next(p for p in cast.PEOPLE if p["user"] == user)


class EveryKindTest(unittest.TestCase):
    """One session of each kind, reduced as a window build reduces a capture."""

    KINDS = [("ada", "claude_nested", 3.0), ("dorothy", "claude_terminal", 2.5),
             ("alan", "codex_tui", 2.5), ("emmy", "claude_desktop", 2.5),
             ("edsger", "vscode_claude", 4.0), ("frances", "claude_rc", 2.5),
             ("kat", "codex_exec", 0.3), ("kat", "claude_print", 0.2), ("kat", "claude_nightly", 2.0),
             ("leslie", "codex_app", 2.0), ("grace", "claude_terminal", 3.0),
             ("barbara", "human_shell", 3.0), ("liam", "claude_babysit", 5.0),
             ("john", "vscode", 4.0), ("tim", "human_shell", 2.0)]

    @classmethod
    def setUpClass(cls):
        now = time.time()
        start = now - 23 * 3600
        world = World("test", 1, start - 3600)
        recs = []
        for i, (user, kind, hours) in enumerate(cls.KINDS):
            s0 = start + i * 1500
            spec = Spec(world.hosts[0], person(user), kind, s0, min(now - 60, s0 + hours * 3600),
                        "test|%s|%d" % (kind, i), day_of(s0))
            spec.idx, spec.key = i, "%s|%s|%03d" % (world.hosts[0], day_of(s0), i)
            spec.pid_base = PID_BASE + i * PID_BLOCK
            out, _census = agents.build(spec, world)
            recs += [(t, seq, i, json.loads(ln)) for t, seq, _tier, _stream, ln in out]
        recs.sort(key=lambda r: (r[0], r[2], r[1]))
        cls.recs = [r[3] for r in recs]
        cls.out = reduce(cls.recs)

    def rows(self):
        t = self.out["trajectories"]
        return [t["rows"][sid] for sid in {s for v in t["ranks"].values() for s in v}]

    def test_every_record_is_marked(self):
        self.assertTrue(all(r.get("synthetic") is True for r in self.recs))

    def test_tasks_are_labeled_in_the_agents_own_words(self):
        sources = {tk.get("source") for r in self.rows()
                   for tk in ((r.get("phases") or {}).get("tasks") or [])}
        self.assertIn("commit", sources)
        kinds = {p["kind"] for r in self.rows() for p in ((r.get("phases") or {}).get("list") or [])}
        # a commit and its push are a burst of two calls, folded into the phase beside it
        self.assertTrue({"explore", "test", "slurm", "wait"} <= kinds, kinds)

    def test_every_surface_and_approval_state(self):
        rows = [r for r in self.rows() if r.get("actor3") == "agent"]
        self.assertTrue({"terminal", "script", "desktop", "extension", "web"}
                        <= {r.get("surface") for r in rows}, {r.get("surface") for r in rows})
        approvals = self.out["sandbox"]["approval_by_class_sessions"]["agent"]
        self.assertTrue({"bypassed", "auto", "supervised", "unobservable"} <= set(approvals), approvals)
        self.assertTrue({"agent", "human", "ide"} <= {r.get("actor3") for r in self.rows()})

    def test_sandboxed_calls_isolated_and_failed(self):
        calls = self.out["sandbox"]["tool_calls"]["agent"]
        self.assertGreater(calls.get("isolated", 0), 0, calls)
        self.assertGreater(calls.get("failed", 0), 0, calls)
        wrappers = {(a["wrapper"], a["outcome"]) for a in self.out["sandbox"]["attempts"]}
        self.assertIn(("bwrap", "isolated"), wrappers)
        self.assertIn(("codex-linux-sandbox", "isolated"), wrappers)

    def test_nested_and_hosted_agents(self):
        summary = self.out["cascades"]["summary"]
        self.assertGreaterEqual(summary["nested"], 1, summary)
        self.assertGreaterEqual(summary["hosted"], 1, summary)

    def test_agents_poll_the_scheduler(self):
        agent = self.out["slurm"]["by_class"]["agent"]
        self.assertGreater(agent["jobs"], 0)
        self.assertGreaterEqual(agent["polling_sessions"], 1)
        self.assertEqual(agent["flags_pct"]["parsable"], 100.0)

    def test_risk_has_compute_commands_and_credentials(self):
        risk = self.out["risk"]
        compute = {r["tool"]: r for r in risk["login_compute"]["rows"]}
        self.assertGreater(compute["python3"]["cpu_s"], 1000)
        ids = {r["id"] for r in risk["dangerous"]["rows"]}
        self.assertTrue({"pip_install", "conda_install", "build_login"} <= ids, ids)
        self.assertTrue(risk["secrets"]["rows"])


if __name__ == "__main__":
    unittest.main()
