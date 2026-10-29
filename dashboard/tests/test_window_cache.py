"""Saved windows: a resumed view must be the view a fresh build would give.

Same scratch-feed fixture as test_ingest. Builds run on the calling thread
(`WindowRegistry._build`) rather than the worker, so each test sees exactly one
build and nothing races it.

    cd dashboard && .venv/bin/python -m unittest discover -s tests -q
"""
import os
import pickle
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from rc_dashboard import snapshots                 # noqa: E402
from rc_dashboard.export import window_plan, window_scan   # noqa: E402
from rc_dashboard.normalize import Normalizer      # noqa: E402
from rc_dashboard.windows import EVENTS, WindowRegistry    # noqa: E402
from test_ingest import FeedCase, exit_rec, line, stamp    # noqa: E402

WIN = 1440
# Enough variety that every reducer holds state worth getting wrong: a shell
# whose child the dedup must match, a compute tool, a dangerous command, and a
# second host interleaved in time.
TOOLS = [("bash", "bash -lc make all"), ("make", "make all"), ("git", "git status"),
         ("python3", "python3 train.py"), ("pip", "pip install torch"),
         ("rm", "rm -rf /scratch/old")]


def varied_exit(epoch, i, host="h1"):
    comm, args = TOOLS[i % len(TOOLS)]
    r = exit_rec(epoch, pid=5000 + i, actor3=("agent", "human", "human-vscode")[i % 3],
                 host=host)
    r.update(comm=comm, args=args, ppid=4000 + i // 3, cpu_s=0.1 * (i % 7),
             io={"rd_mb": 0.5, "wr_mb": 0.1, "rchar_mb": 2.0, "wchar_mb": 0.2,
                 "scope": "process"},
             sandbox="unsandboxed", sandbox_src="ns", session_key="claude:%d" % (i % 4))
    return r


def skipped_rec(epoch, host="h1"):
    """A record no view reads. Not `conn`: the process index and the cascades
    take a long-lived agent's identity from its connection ticks. Not
    `residency_totals` either: the resources series samples memory from it."""
    return {"schema_version": 6, "event": "stop", "ts": stamp(epoch),
            "ts_epoch": epoch, "host": host, "records": 0, "reason": "test"}


class CacheCase(FeedCase):

    def registry(self, cfg=None):
        cfg = cfg or self.cfg()
        classes = list(cfg.get("filters.classes")) + ["unlabeled"]
        return WindowRegistry(cfg, classes, lambda: [self.root], threading.RLock())

    def build(self, reg, minutes=WIN):
        view = reg.request(minutes)
        reg._build(view)
        self.assertEqual((view.state, view.error), ("ready", None))
        return view

    def feed(self, start_min, n, host="h1"):
        """`n` exits a minute apart, the oldest `start_min` minutes ago, with a
        record between each pair that the views must skip."""
        recs = []
        for i in range(n):
            t = self.now - 60 * (start_min - i)
            recs.append(varied_exit(t, i, host))
            recs.append(skipped_rec(t + 1, host))
        self.append("%s.exits.jsonl" % host, recs)

    def snapshot_file(self, minutes=WIN):
        d = os.path.join(self.tmp, "cache", "windows")
        names = [n for n in os.listdir(d) if n.startswith("w%d." % minutes)]
        self.assertEqual(len(names), 1, names)
        return os.path.join(d, names[0])


class ScanTest(CacheCase):

    def test_the_event_prefilter_yields_exactly_what_parsing_every_line_would(self):
        self.feed(120, 60)
        self.feed(110, 40, host="h2")
        since = self.now - 3600 * 24
        plan, _ = window_plan([self.root], since)
        everything = [r["pid"] for r, _ in window_scan(plan, Normalizer(self.cfg()), since)
                      if r["event"] in EVENTS]
        plan, _ = window_plan([self.root], since)
        filtered = [r["pid"] for r, st in
                    window_scan(plan, Normalizer(self.cfg()), since, events=EVENTS)]
        self.assertEqual(filtered, everything)
        self.assertEqual(len(filtered), 100)

    def test_a_half_written_last_line_is_left_unread_and_unclaimed(self):
        recs = [varied_exit(self.now - 600 + i, i) for i in range(3)]
        tail = line(varied_exit(self.now - 10, 9))
        self.append("h1.exits.jsonl", recs, raw=tail[:25])
        since = self.now - 3600
        plan, _ = window_plan([self.root], since)
        got = [r["pid"] for r, _ in window_scan(plan, Normalizer(self.cfg()), since)]
        self.assertEqual(got, [5000, 5001, 5002])
        # The saved offset stops before the fragment, so a resume reads it whole.
        self.assertEqual(plan[0]["end"], os.path.getsize(self.path("h1.exits.jsonl")) - 25)


class PrewarmTest(CacheCase):

    def test_a_prewarmed_window_queues_behind_and_is_evicted_first(self):
        reg = self.registry()
        reg.max_views = 3
        mine = reg.request(WIN)                     # the reader's window
        reg.prewarm([60, 360, 720])                 # room for two of them
        self.assertEqual(list(reg._queue), [WIN, 60, 360])
        self.assertEqual(list(reg.views), [360, 60, WIN])   # least recent first
        for v in list(reg.views.values()):
            v.state = "ready"
        reg.request(4320)                           # the reader opens a wide one
        self.assertIn(WIN, reg.views)
        self.assertIs(reg.views[WIN], mine)
        self.assertNotIn(360, reg.views)            # a prewarm made the room

    def test_prewarm_never_evicts_what_a_reader_opened(self):
        reg = self.registry()
        reg.max_views = 2
        reg.request(WIN)
        reg.request(60)
        reg.prewarm([360])
        self.assertEqual(sorted(reg.views), [60, WIN])


class ResumeTest(CacheCase):

    def test_a_resumed_window_matches_a_fresh_build_over_the_same_records(self):
        self.feed(150, 90)
        self.build(self.registry())

        # Written while the service was down: a resume reads only these.
        self.feed(55, 50)
        self.feed(50, 30, host="h2")
        resumed = self.build(self.registry())
        self.assertEqual(resumed.cache["source"], "snapshot", resumed.cache["note"])
        self.assertEqual(resumed.progress["records"], 80)

        os.unlink(self.snapshot_file())
        fresh = self.build(self.registry())
        self.assertEqual(fresh.cache["source"], "scan")
        self.assertEqual(resumed.payload, fresh.payload)

    def test_a_resumed_window_keeps_its_age_and_reports_the_catch_up_as_drift(self):
        self.feed(150, 90)
        first = self.build(self.registry())
        self.feed(55, 50)
        resumed = self.build(self.registry())
        self.assertEqual(resumed.built_at, first.built_at)
        self.assertEqual(resumed.covers_to, first.covers_to)
        # The newest catch-up record is 6 minutes ago; the first build ended at
        # the one 61 minutes ago.
        self.assertAlmostEqual(resumed.drift_s(), 55 * 60, delta=1)
        self.assertEqual(resumed.status()["cache"]["source"], "snapshot")

    def test_resuming_twice_reads_each_record_once(self):
        self.feed(150, 90)
        self.build(self.registry())
        self.feed(55, 20)
        self.build(self.registry())
        self.feed(30, 20)
        again = self.build(self.registry())
        self.assertEqual(again.progress["records"], 20)

        os.unlink(self.snapshot_file())
        self.assertEqual(again.payload, self.build(self.registry()).payload)

    def test_a_live_record_the_resume_already_read_is_not_counted_again(self):
        self.feed(150, 90)
        self.build(self.registry())
        self.feed(55, 10)
        reg = self.registry()
        view = self.build(reg)
        before = view.payload["tool_mix"]
        # The tail hands over the newest record the catch-up already read.
        newest = varied_exit(self.now - 60 * 46, 9)
        reg.feed(Normalizer(self.cfg()).normalize(newest))
        view.materialize()
        self.assertEqual(view.payload["tool_mix"], before)


class RefusalTest(CacheCase):

    def assertRescanned(self, view, why):
        self.assertEqual(view.cache["source"], "scan")
        self.assertIn(why, view.cache["note"])

    def test_a_replaced_day_file_is_rescanned(self):
        self.feed(150, 90)
        self.build(self.registry())
        # Rewritten with one byte of its first record changed: the same length
        # and the same line endings, so only the content tells the files apart.
        # Deleted first, so the file system is free to hand the new file the old
        # inode number -- which is why the inode alone is not its identity.
        p = self.path("h1.exits.jsonl")
        with open(p, "rb") as fh:
            data = fh.read()
        os.unlink(p)
        with open(p, "wb") as fh:
            fh.write(data.replace(b'"alice"', b'"alicf"', 1))
        self.assertRescanned(self.build(self.registry()), "changed since")

    def test_a_snapshot_older_than_the_drift_budget_is_rescanned(self):
        self.feed(150, 90)
        reg = self.registry()
        self.build(reg)
        p = self.snapshot_file()
        with open(p, "rb") as fh:
            meta = pickle.load(fh)
            blob = fh.read()
        # 24h at 25% is a 6h budget.
        reg.store.save(WIN, [self.root], dict(meta, built_at=time.time() - 7 * 3600), blob)
        self.assertRescanned(self.build(self.registry()), "drift budget")

    def test_a_config_change_is_rescanned(self):
        self.feed(150, 90)
        self.build(self.registry())
        cfg = self.cfg()
        cfg.tree["reducers"]["io"]["top_n"] = 5
        self.assertRescanned(self.build(self.registry(cfg)), "different code, config")

    def test_a_snapshot_anyone_else_can_write_is_never_loaded(self):
        self.feed(150, 90)
        self.build(self.registry())
        os.chmod(self.snapshot_file(), 0o666)
        self.assertRescanned(self.build(self.registry()), "not private")

    def test_an_unreadable_snapshot_is_discarded_and_rescanned(self):
        self.feed(150, 90)
        self.build(self.registry())
        p = self.snapshot_file()
        with open(p, "rb") as fh:
            meta = pickle.load(fh)
        with open(p, "wb") as fh:
            pickle.dump(meta, fh)
            fh.write(b"not a pickle")
        self.assertRescanned(self.build(self.registry()), "unreadable")

    def test_a_drift_rebuild_scans_rather_than_resuming(self):
        self.feed(150, 90)
        reg = self.registry()
        view = self.build(reg)
        self.assertIn("no saved build", view.cache["note"])
        reg._build(view)
        self.assertEqual((view.cache["source"], view.cache["note"]), ("scan", None))


class RoundTripTest(CacheCase):

    def test_reducers_that_share_state_still_share_it_after_a_restore(self):
        # The process index is shared by the reducers that fold session keys,
        # and the cascades read the trajectory reducer. Restored as separate
        # copies, a resumed view would feed one and read another.
        self.feed(150, 90)
        view = self.build(self.registry())
        r = snapshots.restore(view.fresh_reducers(),
                              pickle.loads(snapshots.capture(view.reducers)))
        self.assertIs(r["trajectories"].procs, r["_procs"])
        self.assertIs(r["sandbox"].procs, r["_procs"])
        self.assertIs(r["cascades"].procs, r["_procs"])
        self.assertIs(r["cascades"].traj, r["trajectories"])

    def test_every_reducer_comes_back_with_the_state_it_left_with(self):
        self.feed(150, 90)
        view = self.build(self.registry())
        restored = snapshots.restore(view.fresh_reducers(),
                                     pickle.loads(snapshots.capture(view.reducers)))
        for key, r in view.reducers.items():
            self.assertEqual(restored[key].result(), r.result(), key)


class FingerprintTest(unittest.TestCase):

    def test_a_change_to_the_roles_is_a_change_of_code(self):
        # roles.py decides which processes are sessions, so a build saved under
        # the old roles must not resume under new ones. Only the hours held it
        # in their fingerprint, and a filtered view resumes a saved build,
        # never the hours.
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        pkg = os.path.join(tmp, "rc_dashboard")
        shutil.copytree(snapshots.PKG, pkg, ignore=shutil.ignore_patterns("__pycache__"))
        self.addCleanup(setattr, snapshots, "PKG", snapshots.PKG)
        snapshots.PKG = pkg
        before = snapshots.code_digest()
        with open(os.path.join(pkg, "roles.py"), "a") as fh:
            fh.write("\n# one more helper flag\n")
        self.assertNotEqual(snapshots.code_digest(), before)

    def test_what_decides_an_hour_decides_a_saved_build(self):
        # A saved build and a saved hour hold the state of the same normaliser
        # and reducers, so a module that changes what an hour means changes
        # what a saved build means. hours.py itself decides only the hours.
        from rc_dashboard import hours
        self.assertEqual(set(hours.CODE_FILES) - set(snapshots.CODE_FILES), {"hours.py"})


if __name__ == "__main__":
    unittest.main()
