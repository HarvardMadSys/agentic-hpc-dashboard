"""The roll-up table the Trend tab reads (rollup.py).

* the ingest writes a day once the service read all of it -- complete once it
  ended GRACE_S ago, today as it stands -- and its counts are the live tier's;
* a restart recounts the days it reads again from the files: the table holds the
  same rows, never twice the counts;
* a day the read began inside (`cut`) never overwrites the table's whole copy,
  and is written, partial, only where the table has none;
* a late record for a written day is written again;
* a week's or a month's users are distinct accounts over it, a stretch with no
  record is null, not 0;
* the days on disk the live tier never held are filled by hand
  (`build_from_files`) into the same rows the ingest writes;
* `rollup.enabled` off is no table at all, and a demo writes only its own.

    cd dashboard && .venv/bin/python -m unittest tests.test_rollup -q
"""
import datetime
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rc_dashboard import config as configmod      # noqa: E402
from rc_dashboard import gz as gzmod              # noqa: E402
from rc_dashboard import rollup                   # noqa: E402
from rc_dashboard.aggregator import Aggregator     # noqa: E402

TS = "%Y-%m-%d %H:%M:%S"
DAY_S = 86400
_TZ = None


def setUpModule():
    # Days are local days: one zone for every test, so a date is a date.
    global _TZ
    _TZ = os.environ.get("TZ")
    os.environ["TZ"] = "UTC"
    time.tzset()


def tearDownModule():
    if _TZ is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = _TZ
    time.tzset()


def exit_rec(epoch, pid, user="alice", actor3="agent", host="h1", cpu=0.5):
    return {"schema_version": 8, "event": "exit", "ts": time.strftime(TS, time.localtime(epoch)),
            "ts_epoch": epoch, "host": host, "user": user, "actor3": actor3,
            "agent_type": "claude_code" if actor3 == "agent" else None,
            "comm": "git", "args": "git status", "pid": pid, "ppid": 1,
            "cpu_s": cpu, "duration_s": 1.0}


def submit_rec(epoch, user="alice", host="h1"):
    return {"schema_version": 8, "event": "submit", "ts": time.strftime(TS, time.localtime(epoch)),
            "ts_epoch": epoch, "host": host, "user": user, "actor3": "agent",
            "agent_type": "claude_code", "comm": "sbatch", "args": "sbatch job.sh",
            "pid": 77, "ppid": 1}


def midnight_before(epoch):
    t = time.localtime(epoch)
    return rollup.midnight(datetime.date(t.tm_year, t.tm_mon, t.tm_mday))


def date_of(epoch):
    return time.strftime("%Y-%m-%d", time.localtime(epoch))


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rc_rollup_")
        self.root = os.path.join(self.tmp, "ebpf")
        self.now = time.time()
        self.today = midnight_before(self.now)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def cfg(self, cache="cache", **over):
        tree = configmod.defaults()
        tree["feeds"]["ebpf"]["roots"] = [self.root]
        tree["feeds"]["ebpf_node"]["roots"] = [os.path.join(self.tmp, "node")]
        tree["cache"]["dir"] = os.path.join(self.tmp, cache)
        tree["cache"]["state_dir"] = os.path.join(self.tmp, "state")
        tree["panels"]["merge_reduce"] = False
        for k, v in over.items():
            node = tree
            parts = k.split(".")
            for p in parts[:-1]:
                node = node.setdefault(p, {})
            node[parts[-1]] = v
        return configmod.Config(tree, {}, None)

    def write(self, recs, stream="exits", host="h1"):
        """Each record into the date dir of its own day, as the collector files it."""
        for r in recs:
            d = os.path.join(self.root, date_of(r["ts_epoch"]))
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "%s.%s.jsonl" % (host, stream)), "ab") as fh:
                fh.write(json.dumps(r).encode() + b"\n")

    def feed(self, spread=False):
        """Days -4, -3 and -2 at noon, each with alice and bob, and today so far;
        with `spread`, an exit every 10 minutes of day -4 besides, so a read
        begun inside it misses some."""
        recs, pid = [], 100
        if spread:
            base = self.today - 4 * DAY_S
            recs += [exit_rec(base + 600 * k + 5, 3000 + k, user="erin") for k in range(144)]
        for back in (4, 3, 2):
            noon = self.today - back * DAY_S + 12 * 3600
            for i, user in enumerate(("alice", "bob", "alice")):
                pid += 1
                recs.append(exit_rec(noon + 60 * i, pid, user=user))
            pid += 1
            recs.append(exit_rec(noon + 600, pid, user="carol", actor3="human", cpu=None))
        recs.append(exit_rec(max(self.today + 1, self.now - 60), 999, user="alice"))
        self.write(sorted(recs, key=lambda r: r["ts_epoch"]))
        self.write([submit_rec(self.today - 3 * DAY_S + 13 * 3600)], stream="snapshot")

    def run_service(self, cfg):
        agg = Aggregator(cfg)
        agg.backfill_now()
        agg.rollup.flush(agg.lock, force=True)
        return agg

    @staticmethod
    def rows(agg_or_path):
        path = agg_or_path if isinstance(agg_or_path, str) else agg_or_path.rollup.path
        db = sqlite3.connect(path)
        try:
            return {
                "day": sorted(db.execute("SELECT date, held, records, complete FROM day")),
                "class": sorted(db.execute(
                    "SELECT date, host, class, agent_type, processes, cpu_s, cpu_n, submits "
                    "FROM day_class")),
                "user": sorted(db.execute(
                    "SELECT date, user, class, agent_type, processes, cpu_s, submits "
                    "FROM day_user")),
            }
        finally:
            db.close()


class IngestTest(Case):

    def test_whole_days_are_written_complete_and_today_as_it_stands(self):
        self.feed()
        agg = self.run_service(self.cfg())
        days = {d: (held, recs, done) for d, held, recs, done in self.rows(agg)["day"]}
        for back in (4, 2):
            self.assertEqual(days[date_of(self.today - back * DAY_S)], (4, 4, 1))
        # the day with a submission: a record, and no minute of exits more
        self.assertEqual(days[date_of(self.today - 3 * DAY_S)], (4, 5, 1))
        self.assertEqual(days[date_of(self.now)][1:], (1, 0))
        cls = {(d, c): (n, cpu, cpu_n, sub) for d, _h, c, _t, n, cpu, cpu_n, sub
               in self.rows(agg)["class"]}
        d3 = date_of(self.today - 3 * DAY_S)
        self.assertEqual(cls[(d3, "agent")], (3, 1.5, 3, 1))
        # a null CPU is not a 0: counted, but not as a reading
        self.assertEqual(cls[(d3, "human")], (1, 0.0, 0, 0))

    def test_a_restart_recounts_rather_than_adds(self):
        self.feed()
        first = self.rows(self.run_service(self.cfg()))
        second = self.rows(self.run_service(self.cfg()))
        self.assertEqual(first["class"], second["class"])
        self.assertEqual(first["user"], second["user"])
        self.assertEqual([r[:3] for r in first["day"]], [r[:3] for r in second["day"]])

    def test_nothing_is_written_before_the_read_of_the_history_is_done(self):
        self.feed()
        agg = Aggregator(self.cfg())
        self.assertEqual(agg.rollup.flush(agg.lock, force=True), 0)
        self.assertEqual(self.rows(agg)["day"], [])
        self.assertEqual(agg.rollup.status(agg.lock)["state"], "reading")

    def test_a_late_record_for_a_written_day_writes_it_again(self):
        self.feed()
        agg = self.run_service(self.cfg())
        d2 = date_of(self.today - 2 * DAY_S)
        self.write([exit_rec(self.today - 2 * DAY_S + 20 * 3600, 5000, user="dave")])
        agg.poll_once()
        agg.rollup.flush(agg.lock, force=True)
        days = {d: recs for d, _h, recs, _c in self.rows(agg)["day"]}
        self.assertEqual(days[d2], 5)
        self.assertIn((d2, "dave", "agent", "claude_code", 1, 0.5, 0), self.rows(agg)["user"])

    def test_a_failed_write_of_a_day_retention_behind_is_tried_again(self):
        # A late record lands on a written day that is past retention by the
        # flush that writes it again. That write fails: the day must still be
        # held, and written at the next flush, never let go with its record.
        self.feed()
        agg = self.run_service(self.cfg())
        d4 = date_of(self.today - 4 * DAY_S)
        self.write([exit_rec(self.today - 4 * DAY_S + 20 * 3600, 5001, user="dave")])
        agg.poll_once()
        later = self.now + 10 * DAY_S              # day -4 is retention behind
        with mock.patch.object(agg.rollup.store, "write", side_effect=OSError("disk full")):
            self.assertEqual(agg.rollup.flush(agg.lock, now=later, force=True), 0)
        self.assertIn(d4, agg.rollup.acc.days)
        self.assertEqual(agg.rollup.status(agg.lock)["state"], "error")
        agg.rollup.flush(agg.lock, now=later, force=True)
        days = {d: recs for d, _h, recs, _c in self.rows(agg)["day"]}
        self.assertEqual(days[d4], 5)
        self.assertNotIn(d4, agg.rollup.acc.days)   # let go once written

    def test_a_file_the_startup_cannot_open_never_shrinks_a_whole_day(self):
        self.feed()
        d3 = date_of(self.today - 3 * DAY_S)
        self.write([exit_rec(self.today - 3 * DAY_S + 15 * 3600, 6000, host="h2")], host="h2")
        whole = self.rows(self.run_service(self.cfg()))
        bad = os.path.join(self.root, d3, "h2.exits.jsonl")
        real = gzmod.open_feed

        def refuse(path, *a, **k):
            if path == bad:
                raise PermissionError(13, "Permission denied", path)
            return real(path, *a, **k)
        with mock.patch("rc_dashboard.gz.open_feed", side_effect=refuse):
            agg = self.run_service(self.cfg())
        self.assertEqual(self.rows(agg)["day"], whole["day"])
        self.assertEqual(self.rows(agg)["class"], whole["class"])
        self.assertGreater(agg.rollup.status(agg.lock)["kept_better"], 0)

    def test_a_better_copy_written_by_another_process_meanwhile_is_kept(self):
        # the `rollup` command commits a whole day just before the flush writes
        self.feed()
        agg = self.run_service(self.cfg())
        d2 = date_of(self.today - 2 * DAY_S)
        self.write([exit_rec(self.today - 2 * DAY_S + 20 * 3600, 5002, user="dave")])
        agg.poll_once()                      # day -2 dirty: 5 records
        other = rollup.RollupStore(agg.rollup.path)
        big = rollup.Day(d2, self.today - 2 * DAY_S, self.today - DAY_S)
        for k in range(9):
            big.feed({"event": "exit", "host": "h1", "_a3": "agent", "agent_type": "codex",
                      "user": "erin", "cpu_s": 1.0}, self.today - 2 * DAY_S + 60 * k, "exit")
        real = agg.rollup.store._connect

        raced = []

        def racing():
            # once, as the flush opens the connection it writes through: after
            # anything it judged before writing, before its transaction
            f, calls = sys._getframe(1), set()
            while f is not None:              # past mock's own frames
                calls.add(f.f_code.co_name)
                f = f.f_back
            if not raced and "write" in calls:
                raced.append(1)
                other.write([big.snapshot(self.now, True)], source="files")
            return real()
        with mock.patch.object(agg.rollup.store, "_connect", side_effect=racing):
            agg.rollup.flush(agg.lock, force=True)
        days = {d: recs for d, _h, recs, _c in self.rows(agg)["day"]}
        self.assertEqual(days[d2], 9)
        self.assertGreater(agg.rollup.status(agg.lock)["kept_better"], 0)

    def test_a_day_the_read_began_inside_never_replaces_the_whole_copy(self):
        self.feed(spread=True)
        whole = self.rows(self.run_service(self.cfg()))
        d4 = date_of(self.today - 4 * DAY_S)
        # read from between 12:00 and 13:00 of day -4: its morning is left behind
        hours = int((self.now - (self.today - 4 * DAY_S + 12 * 3600)) / 3600.0)
        cut = self.run_service(self.cfg(**{"live.backfill_hours": hours}))
        self.assertLess(cut.rollup.covered_from, self.today - 3 * DAY_S)
        self.assertEqual(self.rows(cut)["day"], whole["day"])
        self.assertEqual([r for r in self.rows(cut)["class"] if r[0] == d4],
                         [r for r in whole["class"] if r[0] == d4])

    def test_a_day_the_read_began_inside_is_written_partial_where_there_is_none(self):
        self.feed(spread=True)
        hours = int((self.now - (self.today - 4 * DAY_S + 12 * 3600)) / 3600.0)
        agg = self.run_service(self.cfg(cache="fresh", **{"live.backfill_hours": hours}))
        days = {d: (recs, done) for d, _h, recs, done in self.rows(agg)["day"]}
        d4 = date_of(self.today - 4 * DAY_S)
        self.assertIn(d4, days)
        self.assertEqual(days[d4][1], 0)
        self.assertLess(days[d4][0], 144 + 4)
        self.assertEqual(days[date_of(self.today - 3 * DAY_S)][1], 1)

    def test_off_is_no_table(self):
        self.feed()
        agg = Aggregator(self.cfg(**{"rollup.enabled": False}))
        agg.backfill_now()
        self.assertIsNone(agg.rollup)
        body = agg.trend(30, "week")
        self.assertFalse(body["enabled"])
        self.assertFalse(os.path.exists(rollup.path_of(self.cfg(**{"rollup.enabled": False}))))


class TrendTest(Case):
    """Buckets, from rows written straight to the store."""

    def snap(self, date, users, cls="agent", complete=True):
        a = rollup.midnight(date)
        day = rollup.Day(date.isoformat(), a, rollup.midnight(date + datetime.timedelta(days=1)))
        for i, u in enumerate(users):
            day.feed({"event": "exit", "host": "h1", "_a3": cls, "agent_type": "codex",
                      "user": u, "cpu_s": 2.0}, a + 3600 + 60 * i, "exit")
        return day.snapshot(a + 2 * DAY_S, complete)

    def test_users_over_a_week_or_month_are_distinct_accounts(self):
        store = rollup.RollupStore(os.path.join(self.tmp, "t", rollup.FILE))
        mon = datetime.date(2026, 11, 2)                   # a Monday
        store.write([self.snap(mon, ["alice", "bob"]),
                     self.snap(mon + datetime.timedelta(days=1), ["alice"]),
                     self.snap(mon + datetime.timedelta(days=8), ["carol"])])
        today = mon + datetime.timedelta(days=13)
        week = {b["key"]: b for b in store.trend(14, "week", today)["buckets"]}
        self.assertEqual(sorted(week), ["2026-11-02", "2026-11-09"])
        w1 = week["2026-11-02"]
        self.assertEqual((w1["users"], w1["by_class"]["agent"]["users"],
                          w1["by_class"]["agent"]["n"], w1["data_days"], w1["days"]),
                         (2, 2, 3, 2, 7))
        self.assertEqual(w1["types"]["codex"], {"n": 3, "cpu_s": 6.0, "users": 2})
        days = {b["key"]: b for b in store.trend(14, "day", today)["buckets"]}
        self.assertEqual([days[k]["users"] for k in ("2026-11-02", "2026-11-03")], [2, 1])
        month = store.trend(14, "month", today)["buckets"]
        self.assertEqual([(b["key"], b["users"]) for b in month], [("2026-11-01", 3)])
        self.assertTrue(month[0]["cut"])

    def test_a_stretch_with_no_record_is_null_not_zero(self):
        store = rollup.RollupStore(os.path.join(self.tmp, "t", rollup.FILE))
        mon = datetime.date(2026, 11, 2)
        store.write([self.snap(mon, ["alice"])])
        days = {b["key"]: b for b in store.trend(3, "day", mon + datetime.timedelta(days=2))["buckets"]}
        empty = days["2026-11-03"]
        self.assertEqual((empty["users"], empty["by_class"], empty["data_days"]), (None, {}, 0))
        self.assertTrue(empty["partial"])

    def test_a_partial_day_makes_its_bucket_partial(self):
        store = rollup.RollupStore(os.path.join(self.tmp, "t", rollup.FILE))
        mon = datetime.date(2026, 11, 2)
        store.write([self.snap(mon + datetime.timedelta(days=k), ["alice"], complete=k != 6)
                     for k in range(7)])
        (w,) = store.trend(7, "week", mon + datetime.timedelta(days=6))["buckets"]
        self.assertEqual((w["complete_days"], w["partial"], w["today"]), (6, True, True))

    def test_a_write_from_another_process_is_seen(self):
        # the `rollup` command writes the table through a store of its own
        path = os.path.join(self.tmp, "t", rollup.FILE)
        served, other = rollup.RollupStore(path), rollup.RollupStore(path)
        mon = datetime.date(2026, 11, 2)
        self.assertEqual(served.trend(3, "day", mon)["buckets"][-1]["data_days"], 0)
        other.write([self.snap(mon, ["alice"])])
        self.assertEqual(served.trend(3, "day", mon)["buckets"][-1]["data_days"], 1)

    def test_a_day_not_yet_whole_is_measured_to_now(self):
        # written at 00:02, read at noon with nothing written since
        store = rollup.RollupStore(os.path.join(self.tmp, "t", rollup.FILE))
        mon = datetime.date(2026, 11, 2)
        a = rollup.midnight(mon)
        day = rollup.Day(mon.isoformat(), a, rollup.midnight(mon + datetime.timedelta(days=1)))
        day.feed({"event": "exit", "host": "h1", "_a3": "agent", "agent_type": "codex",
                  "user": "alice", "cpu_s": 1.0}, a + 60, "exit")
        store.write([day.snapshot(a + 120, False)])
        (b,) = store.trend(1, "day", mon, now=a + 12 * 3600)["buckets"]
        self.assertEqual((b["minutes"], b["held"]), (720, 1))

    def test_a_trend_is_read_from_one_snapshot(self):
        # another writer commits between the trend's first query and the rest
        path = os.path.join(self.tmp, "t", rollup.FILE)
        served, other = rollup.RollupStore(path), rollup.RollupStore(path)
        mon = datetime.date(2026, 11, 2)
        served.write([self.snap(mon, ["alice"])])
        real = served._connect

        class Racing:
            def __init__(self, db):
                self.db, self.raced = db, False

            def __getattr__(self, name):
                return getattr(self.db, name)

            def execute(self, sql, *a):
                cur = self.db.execute(sql, *a)
                if not self.raced and "FROM day WHERE" in sql:
                    rows = cur.fetchall()
                    self.raced = True
                    other.write([self_.snap(mon, ["alice", "bob", "carol"])])
                    return iter(rows)
                return cur

        self_ = self

        def connect():
            tx = real()
            tx.db = Racing(tx.db)
            return tx
        # the cache stamp is read through `_connect` too: race only the trend's
        with mock.patch.object(served, "_connect", side_effect=[real(), connect()]):
            (b,) = served.trend(1, "day", mon, now=rollup.midnight(mon) + 2 * DAY_S)["buckets"]
        self.assertEqual((b["records"], b["by_class"]["agent"]["n"], b["users"]), (1, 1, 1))

    def test_a_table_of_another_format_is_refused(self):
        path = os.path.join(self.tmp, "t", rollup.FILE)
        rollup.RollupStore(path)
        db = sqlite3.connect(path)
        db.execute("UPDATE meta SET value = '99' WHERE key = 'format'")
        db.commit()
        db.close()
        with self.assertRaises(RuntimeError):
            rollup.RollupStore(path)


class FromFilesTest(Case):

    def test_the_days_on_disk_fill_the_rows_the_ingest_writes(self):
        self.feed()
        ingest = self.rows(self.run_service(self.cfg()))
        cfg = self.cfg(cache="by-hand")
        with open(os.devnull, "w") as quiet:
            n = rollup.build_from_files(cfg, days=30, out=quiet)
        self.assertEqual(n, 3)
        hand = self.rows(rollup.path_of(cfg))
        whole = {date_of(self.today - k * DAY_S) for k in (2, 3, 4)}
        self.assertEqual([r for r in hand["class"] if r[0] in whole],
                         [r for r in ingest["class"] if r[0] in whole])
        self.assertEqual([r for r in hand["user"] if r[0] in whole],
                         [r for r in ingest["user"] if r[0] in whole])
        # today is the service's, never written by hand
        self.assertNotIn(date_of(self.now), {r[0] for r in hand["day"]})
        # and a second run reads nothing it holds whole
        with open(os.devnull, "w") as quiet:
            self.assertEqual(rollup.build_from_files(cfg, days=30, out=quiet), 0)

    def test_a_root_listed_twice_is_read_once(self):
        self.feed()
        ingest = self.rows(self.run_service(self.cfg()))
        cfg = self.cfg(cache="twice", **{"feeds.ebpf.roots": [self.root, self.root]})
        with open(os.devnull, "w") as quiet:
            rollup.build_from_files(cfg, days=30, out=quiet)
        d3 = date_of(self.today - 3 * DAY_S)
        self.assertEqual([r for r in self.rows(rollup.path_of(cfg))["class"] if r[0] == d3],
                         [r for r in ingest["class"] if r[0] == d3])

    def test_a_day_within_its_grace_is_written_partial(self):
        # run at 00:01: yesterday is over but a record of it may yet land
        self.feed()
        y = self.today - DAY_S
        self.write([exit_rec(y + 23 * 3600, 7000)])
        cfg = self.cfg(cache="by-hand")
        at = self.today + 60
        with open(os.devnull, "w") as quiet, \
                mock.patch.object(rollup.time, "time", return_value=at):
            rollup.build_from_files(cfg, days=30, out=quiet)
        days = {d: done for d, _h, _r, done in self.rows(rollup.path_of(cfg))["day"]}
        self.assertEqual(days[date_of(y)], 0)
        self.assertEqual(days[date_of(self.today - 2 * DAY_S)], 1)

    def test_a_day_file_that_cannot_be_read_leaves_its_days_partial(self):
        self.feed()
        d3 = date_of(self.today - 3 * DAY_S)
        self.write([exit_rec(self.today - 3 * DAY_S + 15 * 3600, 6000, host="h2")], host="h2")
        bad = os.path.join(self.root, d3, "h2.exits.jsonl")
        cfg = self.cfg(cache="by-hand")
        real = gzmod.open_feed

        def refuse(path, *a, **k):
            if path == bad:
                raise PermissionError(13, "Permission denied", path)
            return real(path, *a, **k)
        with open(os.devnull, "w") as quiet, \
                mock.patch("rc_dashboard.gz.open_feed", side_effect=refuse):
            rollup.build_from_files(cfg, days=30, out=quiet)
        days = {d: (recs, done) for d, _h, recs, done in self.rows(rollup.path_of(cfg))["day"]}
        # the dir it is in, and the days either side whose records could be in it
        for back in (4, 3, 2):
            self.assertEqual(days[date_of(self.today - back * DAY_S)][1], 0, back)
        self.assertEqual(days[d3][0], 5)
        # readable again, the next run reads those days again, whole
        with open(os.devnull, "w") as quiet:
            self.assertEqual(rollup.build_from_files(cfg, days=30, out=quiet), 3)
        days = {d: (recs, done) for d, _h, recs, done in self.rows(rollup.path_of(cfg))["day"]}
        self.assertEqual(days[d3], (6, 1))


class ClockTest(Case):

    def test_the_rollup_command_takes_the_pinned_clock(self):
        # a capture two years old: only the pinned present finds its days
        from rc_dashboard import __main__ as cli
        old = self.today - 730 * DAY_S
        self.write([exit_rec(old - k * DAY_S + 12 * 3600, 100 + k) for k in (3, 2, 1)])
        self.write([exit_rec(old + 12 * 3600, 99)])
        cache = os.path.join(self.tmp, "pinned")
        env = {"RC_DASH_EBPF_ROOTS": self.root, "RC_DASH_CACHE_DIR": cache,
               "RC_DASH_NODE_ROOTS": os.path.join(self.tmp, "node"), "RC_DASH_AS_OF": "auto"}
        was_time, was_ns = time.time, time.time_ns
        try:
            with mock.patch.dict(os.environ, env), open(os.devnull, "w") as quiet, \
                    mock.patch("sys.stderr", quiet):
                self.assertEqual(cli.main(["rollup", "--rollup-days", "30"]), 0)
        finally:
            time.time, time.time_ns = was_time, was_ns
        days = self.rows(os.path.join(cache, "rollup", rollup.FILE))["day"]
        self.assertEqual([d for d, _h, _r, _c in days],
                         [date_of(old - k * DAY_S) for k in (3, 2, 1)])


class ConfigTest(unittest.TestCase):

    def test_the_switch_and_the_directory(self):
        with mock.patch.dict(os.environ, {"RC_DASH_ROLLUP": "0", "RC_DASH_ROLLUP_DIR": "/x/roll"}):
            cfg = configmod.load(None, None)
        self.assertFalse(rollup.enabled(cfg))
        self.assertEqual(rollup.path_of(cfg), os.path.join("/x/roll", rollup.FILE))
        with mock.patch.dict(os.environ, {"RC_DASH_ROLLUP": "maybe"}), \
                self.assertRaises(SystemExit):
            configmod.load(None, None)

    def test_the_table_is_not_in_what_a_saved_state_is_keyed_on(self):
        from rc_dashboard import hours, snapshots
        a = configmod.Config(configmod.defaults(), {}, None)
        b = configmod.Config(configmod.defaults(), {}, None)
        b.tree["rollup"] = {"enabled": False, "dir": "/elsewhere"}
        self.assertEqual(snapshots.fingerprint("c", a, []), snapshots.fingerprint("c", b, []))
        self.assertEqual(hours.fingerprint("c", a, []), hours.fingerprint("c", b, []))

    def test_a_demo_writes_only_its_own_table(self):
        from rc_dashboard import demo
        cfg = configmod.Config(configmod.defaults(), {}, None)
        cfg.tree["rollup"]["dir"] = "/real/rollup"
        tmp = tempfile.mkdtemp(prefix="rc_rollup_demo_")
        try:
            demo.configure(cfg, {"dir": tmp, "days": 1, "hosts": 1, "seed": "x"})
            self.assertTrue(rollup.path_of(cfg).startswith(os.path.abspath(tmp)))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
