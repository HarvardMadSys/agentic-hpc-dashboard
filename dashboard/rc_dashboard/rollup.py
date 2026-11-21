"""The roll-up table: each local day's processes, CPU, users and job submissions,
by class, agent type and host, kept on disk past retention -- what the Trend tab
draws, up to a year back.

The live tier holds `live.retention_min` (7 days) and the day-files hold what the
collector, or whoever cleans its directory, kept. Neither answers "is agent use
growing over the term?". This does, from a table filled **as records are
ingested**: every exit and submit the live tier takes -- the tail's and the
backfill's -- is also summed into its local day here (`Rollup.feed`, beside
`LiveReducer.feed` in `Aggregator.dispatch`), so it costs no read of its own,
and the days are written to SQLite (`RollupStore`) as they end.

**What a row means is a function of the collector's files, never of when the
service ran.** The backfill reads retention again on every start, so a day the
service holds in memory is recounted from the files, not added to what the
table had. A day is written only when the service read **all** of it:

* `covered_from` is where the service's read of the files starts -- the
  backfill's `since` once the backfill is done (the tail's start if it failed or
  was skipped). Until then nothing is written, and the table keeps what an
  earlier run wrote.
* a day that began before `covered_from` (`cut`) is written only where the
  table holds no complete copy of it and holds fewer of its records than this
  run read -- the first run's oldest day, or a demo's -- and never `complete`:
  the table's copy, read whole by an earlier run, is the better one.
* a day that ended `GRACE_S` ago is written `complete`; today, and any day not
  yet past its grace, is written as it stands, `complete = 0`, and written again
  as it grows. A record for a complete day that arrives late (a host's stream
  catching up) marks it dirty and it is written again, so the table converges on
  what the files hold.
* a recount never replaces a complete day holding more records than it read:
  late records only add to a day, so fewer means a day-file the startup could
  not open (the tail reads it once it can) or day-files cleaned away inside
  retention, and the table's copy is the better one (`kept_better`).

A day held in memory is let go once it is retention behind and written; a
record older than the days let go (`floor`) is counted (`late_dropped`) and
not summed, since it would land on a day the service no longer holds whole.

**What is counted.** Per day, class (`_a3`), agent type and host: processes that
exited, the CPU seconds they reported (`cpu_n`: how many reported one -- a
dropped BPF block writes null, never 0), and `submit` records. Per day, user,
class and agent type, the same, so a week's or a month's distinct users are
counted from the accounts themselves, never summed from the days. Per day, the
minutes the collector wrote any exit in (`held`), against the minutes of it the
table could hold (`minutes`): a day no minute of was held has no rows and reads
"no data", never as a quiet day. Days are the service's local days (`tz`),
cut at local midnight, so a day with a clock change is 23 or 25 hours long.

Names stay in the table (it is made 0700, as the other caches are); what is
served is counts, so nothing here needs aliasing on the way out.

Older days, from day-files still on disk that the live tier never held, are
filled by hand: `python -m rc_dashboard rollup --rollup-days 365` reads them,
oldest first, through the same normalizer (`build_from_files`).
"""
import bisect
import datetime
import os
import sqlite3
import sys
import threading
import time
import zlib

FORMAT = 1
FILE = "rollup.sqlite"
# A day is complete once it ended this long ago: a record lands in the files
# up to a poll round and a clock skew behind its time.
GRACE_S = 900
# The most days a trend is asked for: a year, and the day it began on.
MAX_DAYS = 366
# How often the service writes what changed, at most (`Rollup.flush`), and how
# old the table may be when a trend is read before it writes first.
FLUSH_S = 60

# A bucket, as SQL over `date` (YYYY-MM-DD) and as Python over a date. Weeks start
# on Monday: SQLite's `weekday 1` moves forward to a Monday, so six days back first.
BUCKETS = {
    "day": "date",
    "week": "date(date, '-6 days', 'weekday 1')",
    "month": "substr(date, 1, 7) || '-01'",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS day (
    date TEXT PRIMARY KEY,
    day_from REAL NOT NULL, day_to REAL NOT NULL, tz TEXT,
    minutes INTEGER NOT NULL, held INTEGER NOT NULL, records INTEGER NOT NULL,
    first_ts REAL, last_ts REAL,
    complete INTEGER NOT NULL, source TEXT, written_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS day_class (
    date TEXT NOT NULL, host TEXT NOT NULL, class TEXT NOT NULL, agent_type TEXT NOT NULL,
    processes INTEGER NOT NULL, cpu_s REAL NOT NULL, cpu_n INTEGER NOT NULL,
    submits INTEGER NOT NULL,
    PRIMARY KEY (date, host, class, agent_type));
CREATE TABLE IF NOT EXISTS day_user (
    date TEXT NOT NULL, user TEXT NOT NULL, class TEXT NOT NULL, agent_type TEXT NOT NULL,
    processes INTEGER NOT NULL, cpu_s REAL NOT NULL, submits INTEGER NOT NULL,
    PRIMARY KEY (date, user, class, agent_type));
"""


def enabled(cfg):
    return bool(cfg.get("rollup.enabled", True))


def path_of(cfg):
    """The table's file: `rollup.dir`, `<cache.dir>/rollup` unless set."""
    d = cfg.get("rollup.dir") or os.path.join(cfg.get("cache.dir") or ".", "rollup")
    return os.path.join(os.path.abspath(d), FILE)


def bucket_of(date, by):
    """`date` (a datetime.date) as the first day of its bucket, as BUCKETS has it."""
    if by == "week":
        return date - datetime.timedelta(days=date.weekday())
    if by == "month":
        return date.replace(day=1)
    return date


def midnight(date):
    """Local midnight starting `date`, as an epoch: mktime places it, so a day
    with a clock change is 23 or 25 hours long."""
    return time.mktime((date.year, date.month, date.day, 0, 0, 0, 0, 0, -1))


class Calendar:
    """Local days by epoch, each placed once: a record's day is a bisect, not a
    `localtime` call."""

    def __init__(self):
        self.starts = []         # day_from, sorted
        self.days = []           # (date string, day_from, day_to), in the same order

    def of(self, epoch):
        i = bisect.bisect_right(self.starts, epoch) - 1
        if 0 <= i and epoch < self.days[i][2]:
            return self.days[i]
        t = time.localtime(epoch)
        d = datetime.date(t.tm_year, t.tm_mon, t.tm_mday)
        a, b = midnight(d), midnight(d + datetime.timedelta(days=1))
        day = (d.isoformat(), a, b)
        j = bisect.bisect_left(self.starts, a)
        self.starts.insert(j, a)
        self.days.insert(j, day)
        return day


class Day:
    """One local day's sums, as the ingest left them."""
    __slots__ = ("date", "day_from", "day_to", "by", "users", "minutes", "records",
                 "first", "last", "dirty", "written_complete")

    def __init__(self, date, day_from, day_to):
        self.date, self.day_from, self.day_to = date, day_from, day_to
        self.by = {}             # (host, class, agent_type) -> [processes, cpu_s, cpu_n, submits]
        self.users = {}          # (user, class, agent_type) -> [processes, cpu_s, submits]
        self.minutes = set()     # epoch minutes any exit was in
        self.records = 0
        self.first = self.last = None
        self.dirty = True
        self.written_complete = False

    def feed(self, rec, epoch, ev):
        host = rec.get("host") or ""
        cls = rec.get("_a3") or ""
        atype = rec.get("agent_type") or ""
        acc = self.by.get((host, cls, atype))
        if acc is None:
            acc = self.by[(host, cls, atype)] = [0, 0.0, 0, 0]
        user = rec.get("user")
        u = None
        if user:
            u = self.users.get((user, cls, atype))
            if u is None:
                u = self.users[(user, cls, atype)] = [0, 0.0, 0]
        if ev == "exit":
            cpu = rec.get("cpu_s")
            acc[0] += 1
            if isinstance(cpu, (int, float)) and not isinstance(cpu, bool):
                acc[1] += cpu
                acc[2] += 1
            if u is not None:
                u[0] += 1
                if isinstance(cpu, (int, float)) and not isinstance(cpu, bool):
                    u[1] += cpu
            self.minutes.add(int(epoch // 60))
        else:
            acc[3] += 1
            if u is not None:
                u[2] += 1
        self.records += 1
        if self.first is None or epoch < self.first:
            self.first = epoch
        if self.last is None or epoch > self.last:
            self.last = epoch
        self.dirty = True

    def snapshot(self, now, complete):
        """What `RollupStore.write` takes: plain data, copied, so it is written
        outside the lock the ingest holds."""
        end = min(self.day_to, now)
        return {"date": self.date, "day_from": self.day_from, "day_to": self.day_to,
                "tz": time.strftime("%Z", time.localtime(self.day_from + 1)),
                "minutes": max(0, int((end - self.day_from) // 60)),
                "held": len(self.minutes), "records": self.records,
                "first": self.first, "last": self.last, "complete": complete,
                "by": [(k[0], k[1], k[2], v[0], round(v[1], 3), v[2], v[3])
                       for k, v in self.by.items()],
                "users": [(k[0], k[1], k[2], v[0], round(v[1], 3), v[2])
                          for k, v in self.users.items()]}


class Accumulator:
    """Records summed into their local days, in memory."""
    EVENTS = ("exit", "submit")

    def __init__(self):
        self.cal = Calendar()
        self.days = {}           # date -> Day
        self.floor = None        # records before this epoch are not summed
        self.late_dropped = 0

    def feed(self, rec):
        ev = rec.get("event")
        if ev not in self.EVENTS:
            return
        epoch = rec.get("_ts_epoch")
        if not isinstance(epoch, (int, float)) or isinstance(epoch, bool):
            return
        if self.floor is not None and epoch < self.floor:
            self.late_dropped += 1
            return
        date, a, b = self.cal.of(epoch)
        day = self.days.get(date)
        if day is None:
            day = self.days[date] = Day(date, a, b)
        day.feed(rec, epoch, ev)


class RollupStore:
    """The table on disk: a SQLite file, 0700 directory, 0600 file."""

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.version = 0         # bumped by every write, so a cached trend knows
        self._cache = {}
        d = os.path.dirname(path)
        os.makedirs(d, mode=0o700, exist_ok=True)
        try:
            os.chmod(d, 0o700)
        except OSError:
            pass
        with self._connect() as db:
            db.executescript(SCHEMA)
            row = db.execute("SELECT value FROM meta WHERE key = 'format'").fetchone()
            if row is None:
                db.execute("INSERT INTO meta VALUES ('format', ?)", (str(FORMAT),))
            elif row[0] != str(FORMAT):
                raise RuntimeError("%s is roll-up format %s, not %d: move it aside"
                                   % (path, row[0], FORMAT))
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.execute("PRAGMA journal_mode = WAL")
        db.execute("PRAGMA synchronous = NORMAL")
        return _Tx(db)

    def write(self, snaps, source="ingest", now=None):
        """Replace each day's rows with `snaps` (Day.snapshot), in one transaction."""
        if not snaps:
            return 0
        now = time.time() if now is None else now
        with self.lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for s in snaps:
                d = s["date"]
                db.execute("DELETE FROM day_class WHERE date = ?", (d,))
                db.execute("DELETE FROM day_user WHERE date = ?", (d,))
                db.execute("INSERT OR REPLACE INTO day VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                           (d, s["day_from"], s["day_to"], s["tz"], s["minutes"], s["held"],
                            s["records"], s["first"], s["last"], 1 if s["complete"] else 0,
                            source, now))
                db.executemany("INSERT INTO day_class VALUES (?,?,?,?,?,?,?,?)",
                               [(d,) + r for r in s["by"]])
                db.executemany("INSERT INTO day_user VALUES (?,?,?,?,?,?,?)",
                               [(d,) + r for r in s["users"]])
            db.execute("COMMIT")
            self.version += 1
            self._cache.clear()
        return len(snaps)

    def held(self, dates):
        """{date: (complete, records)} for the `dates` the table holds."""
        if not dates:
            return {}
        with self._connect() as db:
            return {r[0]: (r[1], r[2]) for r in db.execute(
                "SELECT date, complete, records FROM day WHERE date IN (%s)"
                % ",".join("?" * len(dates)), list(dates))}

    def complete_dates(self):
        with self._connect() as db:
            return {r[0] for r in db.execute("SELECT date FROM day WHERE complete = 1")}

    def summary(self):
        with self._connect() as db:
            n, lo, hi, done, at = db.execute(
                "SELECT COUNT(*), MIN(date), MAX(date), SUM(complete), MAX(written_at) "
                "FROM day").fetchone()
        return {"days": n or 0, "oldest": lo, "newest": hi, "complete": done or 0,
                "written_at": at}

    # ---------------------------------------------------------------- trend
    def trend(self, days, by, today):
        """`days` local days up to `today` (a datetime.date), in buckets of `by`."""
        key = (days, by, today, self.version)
        hit = self._cache.get(key)
        if hit is not None:
            return hit
        body = self._trend(days, by, today)
        if len(self._cache) > 16:
            self._cache.clear()
        self._cache[key] = body
        return body

    def _trend(self, days, by, today):
        expr = BUCKETS[by]
        lo = today - datetime.timedelta(days=days - 1)
        rng = (lo.isoformat(), today.isoformat())
        # every bucket in the range, with or without rows: a bucket the table
        # holds nothing of is a hole, not a quiet stretch
        buckets = {}
        d = lo
        while d <= today:
            b = bucket_of(d, by).isoformat()
            bk = buckets.get(b)
            if bk is None:
                end = bucket_of(d, by)
                end = (end + datetime.timedelta(days=7) if by == "week" else
                       (end.replace(day=28) + datetime.timedelta(days=4)).replace(day=1)
                       if by == "month" else end + datetime.timedelta(days=1))
                bk = buckets[b] = {
                    "key": b, "from": d.isoformat(), "to": d.isoformat(),
                    "t": midnight(d), "t_end": midnight(end),
                    "cut": bucket_of(d, by) < d,   # the range starts inside it
                    "days": 0, "data_days": 0, "complete_days": 0, "today": False,
                    "minutes": 0, "held": 0, "records": 0,
                    "by_class": {}, "types": {}, "users": None, "hosts": {}}
            bk["to"] = d.isoformat()
            bk["days"] += 1
            bk["today"] = bk["today"] or d == today
            d += datetime.timedelta(days=1)
        with self._connect() as db:
            for row in db.execute(
                    "SELECT %s AS b, complete, minutes, held, records FROM day "
                    "WHERE date BETWEEN ? AND ?" % expr, rng):
                bk = buckets.get(row[0])
                if bk is None:
                    continue
                bk["minutes"] += row[2]
                bk["held"] += row[3]
                bk["records"] += row[4]
                if row[4]:
                    bk["data_days"] += 1
                if row[1]:
                    bk["complete_days"] += 1
            for b, cls, atype, n, cpu, cpu_n, sub in db.execute(
                    "SELECT %s AS b, class, agent_type, SUM(processes), SUM(cpu_s), "
                    "SUM(cpu_n), SUM(submits) FROM day_class WHERE date BETWEEN ? AND ? "
                    "GROUP BY b, class, agent_type" % expr, rng):
                bk = buckets.get(b)
                if bk is None:
                    continue
                acc = bk["by_class"].setdefault(cls, {"n": 0, "cpu_s": 0.0, "cpu_n": 0,
                                                      "submits": 0, "users": 0})
                acc["n"] += n
                acc["cpu_s"] += cpu
                acc["cpu_n"] += cpu_n
                acc["submits"] += sub
                if cls == "agent" and atype:
                    ty = bk["types"].setdefault(atype, {"n": 0, "cpu_s": 0.0, "users": 0})
                    ty["n"] += n
                    ty["cpu_s"] += cpu
            for b, cls, users in db.execute(
                    "SELECT %s AS b, class, COUNT(DISTINCT user) FROM day_user "
                    "WHERE date BETWEEN ? AND ? GROUP BY b, class" % expr, rng):
                bk = buckets.get(b)
                if bk is not None and cls in bk["by_class"]:
                    bk["by_class"][cls]["users"] = users
            for b, atype, users in db.execute(
                    "SELECT %s AS b, agent_type, COUNT(DISTINCT user) FROM day_user "
                    "WHERE date BETWEEN ? AND ? AND class = 'agent' AND agent_type != '' "
                    "GROUP BY b, agent_type" % expr, rng):
                bk = buckets.get(b)
                if bk is not None and atype in bk["types"]:
                    bk["types"][atype]["users"] = users
            for b, users in db.execute(
                    "SELECT %s AS b, COUNT(DISTINCT user) FROM day_user "
                    "WHERE date BETWEEN ? AND ? GROUP BY b" % expr, rng):
                if b in buckets:
                    buckets[b]["users"] = users
            for b, host, n, cpu in db.execute(
                    "SELECT %s AS b, host, SUM(processes), SUM(cpu_s) FROM day_class "
                    "WHERE date BETWEEN ? AND ? GROUP BY b, host" % expr, rng):
                if b in buckets:
                    buckets[b]["hosts"][host] = {"n": n, "cpu_s": round(cpu, 1)}
            summary = db.execute(
                "SELECT COUNT(*), MIN(date), MAX(date), SUM(complete), MAX(written_at) "
                "FROM day").fetchone()
        out = []
        for bk in buckets.values():
            have = bk["data_days"] > 0
            if not have:
                # a stretch the table holds no record of: null, never 0
                bk["by_class"], bk["types"], bk["users"], bk["hosts"] = {}, {}, None, {}
            for acc in bk["by_class"].values():
                acc["cpu_s"] = round(acc["cpu_s"], 1)
            for ty in bk["types"].values():
                ty["cpu_s"] = round(ty["cpu_s"], 1)
            bk["types"] = dict(sorted(bk["types"].items(), key=lambda kv: -kv[1]["users"]))
            bk["partial"] = bk["cut"] or bk["complete_days"] < bk["days"]
            out.append(bk)
        return {"buckets": out, "by": by, "days": days,
                "from": lo.isoformat(), "to": today.isoformat(),
                "table": {"days": summary[0] or 0, "oldest": summary[1],
                          "newest": summary[2], "complete": summary[3] or 0,
                          "written_at": summary[4]}}


class _Tx:
    """A connection as a context manager that closes it (sqlite3's own commits only)."""

    def __init__(self, db):
        self.db = db

    def __enter__(self):
        return self.db

    def __exit__(self, *exc):
        if exc[0] is not None and self.db.in_transaction:
            self.db.execute("ROLLBACK")
        self.db.close()
        return False


class Rollup:
    """The service's roll-up: the ingest's sums in memory, and the table they are
    written to. `feed` runs under the aggregator's lock; `flush` takes it only to
    copy what changed."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.retention_s = int(cfg.get("live.retention_min", 10080)) * 60
        self.acc = Accumulator()
        self.covered_from = None
        self.error = None
        self.written = 0         # day rows written since the service started
        self.kept_better = 0     # writes passed over for a better copy the table held
        self.flushed_at = None
        self.path = path_of(cfg)
        self._flush_lock = threading.Lock()
        try:
            self.store = RollupStore(self.path)
        except Exception as e:                  # the page says so; ingest goes on
            self.store = None
            self.error = "%s: %s" % (type(e).__name__, e)

    def feed(self, rec):
        self.acc.feed(rec)

    def covered(self, since):
        """The files are read whole from `since` (an epoch) on. Under the lock."""
        self.covered_from = since

    def flush(self, lock, now=None, force=False):
        """Write every day that changed and the service read whole. Returns the
        days written."""
        if self.store is None or self.covered_from is None:
            return 0
        with self._flush_lock:
            now = time.time() if now is None else now
            if not force and self.flushed_at is not None and now - self.flushed_at < FLUSH_S:
                return 0
            snaps, done = [], []
            with lock:
                for day in list(self.acc.days.values()):
                    cut = day.day_from < self.covered_from
                    complete = not cut and now >= day.day_to + GRACE_S
                    if day.dirty or (complete and not day.written_complete):
                        snaps.append(day.snapshot(now, complete))
                        done.append((day, complete))
                        day.dirty = False
            try:
                # Never over a better copy. A cut day only where the table has
                # no whole copy and fewer of its records; any day never over a
                # whole copy holding more records than this run read -- a file
                # the startup could not open (the tail reads it once it can,
                # and the day is written then), or day-files cleaned away
                # inside retention. Late records only ever add to a day.
                had = self.store.held([s["date"] for s in snaps])
                cut = {s["date"] for s, (day, _c) in zip(snaps, done)
                       if day.day_from < self.covered_from}

                def worse(s):
                    h = had.get(s["date"])
                    if h is None:
                        return False
                    if s["date"] in cut:
                        return bool(h[0]) or h[1] >= s["records"]
                    return bool(h[0]) and h[1] > s["records"]
                kept = [s["date"] for s in snaps if worse(s)]
                snaps = [s for s in snaps if s["date"] not in kept]
                self.kept_better += len(kept)
                n = self.store.write(snaps, now=now)
            except Exception as e:
                with lock:
                    for day, _ in done:
                        day.dirty = True        # tried again at the next flush
                self.error = "%s: %s" % (type(e).__name__, e)
                return 0
            with lock:
                for day, complete in done:
                    if complete:
                        day.written_complete = True
                # A day retention behind, written, is let go -- only now that its
                # write went through: let go before, a failed write would leave
                # its newest records in a Day nothing holds. What lands on it
                # later is counted, not summed.
                keep_from = now - self.retention_s
                for date, day in list(self.acc.days.items()):
                    if day.day_to < keep_from and not day.dirty and \
                            (day.written_complete or day.day_from < self.covered_from):
                        del self.acc.days[date]
                        self.acc.floor = max(self.acc.floor or 0, day.day_to)
            self.error = None
            self.written += n
            self.flushed_at = now
            return n

    def trend(self, lock, days=365, by="day", now=None):
        now = time.time() if now is None else now
        days = max(1, min(MAX_DAYS, int(days)))
        if by not in BUCKETS:
            raise ValueError("by is day, week or month, not %r" % by)
        body = {"enabled": True, "path": self.path, "grace_s": GRACE_S}
        if self.store is None:
            return dict(body, buckets=[], error=self.error, by=by, days=days)
        self.flush(lock, now)
        t = time.localtime(now)
        out = dict(self.store.trend(days, by, datetime.date(t.tm_year, t.tm_mon, t.tm_mday)))
        out.update(body)
        out.update(self.status(lock))
        out["tz"] = time.strftime("%Z", t)
        out["basis"] = ("every exit and Slurm submission the service ingested, summed into "
                        "its local day as it was read (rollup.py); a day is written once the "
                        "service has read all of it, complete once it ended %d min ago. Users "
                        "are distinct accounts over the whole bucket." % (GRACE_S // 60))
        return out

    def status(self, lock):
        with lock:
            held = len(self.acc.days)
            late = self.acc.late_dropped
        return {"state": ("error" if self.error else "reading" if self.covered_from is None
                          else "ok"),
                "error": self.error, "covered_from": self.covered_from,
                "flushed_at": self.flushed_at, "written": self.written,
                "kept_better": self.kept_better,
                "held_days": held, "late_dropped": late}


# ------------------------------------------------------------ older days, by hand
def build_from_files(cfg, days=365, force=False, out=sys.stderr):
    """Fill the table with the days of the last `days` the day-files on disk still
    hold and the table has no complete copy of (all of them with `force`), read
    oldest first through the service's normalizer. A day is written only once the
    date dirs either side of it are read too, since a record lands in the dir of
    the collector's day, which near midnight need not be the service's."""
    from . import fastjson
    from . import feeds as feedmod
    from .feedfile import dated_dirs
    from .gz import open_feed
    from .normalize import Normalizer

    store = RollupStore(path_of(cfg))
    roots = feedmod.resolve(cfg)["ebpf"]["resolved"] or []
    now = time.time()
    t = time.localtime(now)
    today = datetime.date(t.tm_year, t.tm_mon, t.tm_mday)
    lo = today - datetime.timedelta(days=max(1, int(days)) - 1)
    have = set() if force else store.complete_dates()
    want = set()
    d = lo
    while d < today:
        if d.isoformat() not in have:
            want.add(d.isoformat())
        d += datetime.timedelta(days=1)
    by_date = {}
    for root in roots:
        for date, files in dated_dirs(root, 0):
            by_date.setdefault(date, []).extend(files)

    def near(date):
        x = datetime.date.fromisoformat(date)
        return any((x + datetime.timedelta(days=k)).isoformat() in want for k in (-1, 0, 1))
    dirs = sorted(dd for dd in by_date if near(dd))
    if not dirs:
        print("rollup: nothing to read (%d day(s) wanted, %d dated dir(s) on disk)"
              % (len(want), len(by_date)), file=out)
        return 0
    norm = Normalizer(cfg)
    acc = Accumulator()
    written = 0
    read_dirs = set()
    # dirs with a day-file that could not be read whole: a day whose records
    # could be in one is written, but never as complete, so a later run reads
    # it again
    failed = set()

    def whole(date):
        x = datetime.date.fromisoformat(date)
        return not any((x + datetime.timedelta(days=k)).isoformat() in failed
                       for k in (-1, 0, 1))

    def ready(date):
        x = datetime.date.fromisoformat(date)
        return all((x + datetime.timedelta(days=k)).isoformat() in read_dirs
                   or (x + datetime.timedelta(days=k)).isoformat() not in by_date
                   for k in (-1, 0, 1))

    def write_ready(final=False):
        nonlocal written
        snaps = []
        for date in sorted(acc.days):
            if date in want and (final or ready(date)):
                day = acc.days.pop(date)
                # whole only once its grace is past too: a record may land up
                # to GRACE_S after its day ends, and a later run skips a whole day
                snaps.append(day.snapshot(now, whole(date) and now >= day.day_to + GRACE_S))
            elif date not in want and (final or ready(date)):
                acc.days.pop(date)
        written += store.write(snaps, source="files", now=now)
        for s in snaps:
            print("rollup: %s  %d records, %d of %d minutes held%s"
                  % (s["date"], s["records"], s["held"], s["minutes"],
                     "" if s["complete"] else
                     ", partial: a day-file near it was not read whole, or it ended under "
                     "%d min ago" % (GRACE_S // 60)),
                  file=out)

    for date in dirs:
        t0 = time.monotonic()
        n = 0
        for path in sorted(by_date[date]):
            try:
                with open_feed(path) as fh:
                    for line in fh:
                        if b'"exit"' not in line and b'"submit"' not in line:
                            continue
                        try:
                            raw = fastjson.record(line)
                        except ValueError:
                            continue
                        rec = norm.normalize(raw)
                        if rec is not None:
                            acc.feed(rec)
                            n += 1
            except (OSError, EOFError, zlib.error) as e:
                # unreadable, or a .gz cut short or damaged part way
                print("rollup: %s: %s" % (path, e), file=out)
                failed.add(date)
        read_dirs.add(date)
        print("rollup: read %s, %d records in %.0f s" % (date, n, time.monotonic() - t0),
              file=out)
        write_ready()
    write_ready(final=True)
    return written
