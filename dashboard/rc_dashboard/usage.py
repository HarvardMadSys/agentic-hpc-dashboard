"""Which tabs readers open, counted on this host and kept there: what a site's own use
says about which parts of the page earn their place (`rc-dashboard usage`).

The page posts, at most once a minute and when it is put away, how many times each tab
was opened since its last post: POST /api/usage, {"views": {"tab:live": 2, ...}}. A view
is one of the page's own tabs (`tab:<key>`, TABS), never what a reader typed or a name
the data holds: a key that is not one of them is dropped, not kept, so nothing a client
sends is written but a count. No user, address, window, filter or time of day is kept,
only a count a day. The counts are
kept per day (UTC) in <cache.dir>/usage/<YYYY-MM-DD>.json, and a day older than
KEEP_DAYS is removed. `ui.count_views` (RC_DASH_COUNT_VIEWS=0) turns it off: the page
sends nothing, and the route takes nothing. A day is the wall clock's (`clock.wall`), even
while `clock.as_of` pins the feed's present: a reader opens a tab today.
"""
import datetime
import json
import os
import re
import sys
import threading

from . import clock

# A view: a tab's key, as the page writes it in `?tab=`.
VIEW = re.compile(r"tab:[a-z][a-z0-9_-]{0,31}\Z")
# The page's tabs (web/src/App.tsx, TABS; tests/test_usage.py holds the two equal): the
# only views kept.
TABS = frozenset(("summary", "live", "users", "agents", "sessions", "security", "risk",
                  "resources", "slurm", "trend", "feeds", "internal"))
MAX_VIEWS = 64          # views in one post
MAX_COUNT = 1000        # opens of one view in one post
MAX_BODY = 16384        # bytes of a post
KEEP_DAYS = 400
DAY_FILE = re.compile(r"(\d{4}-\d{2}-\d{2})\.json\Z")


class UsageError(ValueError):
    """A post that is not counts of views (`parse`), saying why."""


def parse(body):
    """The counts a post carries of the page's tabs, as {view: count}, a view that is
    no tab of the page's dropped; UsageError for a post that is not counts. Its words
    never repeat what was sent."""
    views = body.get("views") if isinstance(body, dict) else None
    if not isinstance(views, dict) or not views:
        raise UsageError('expected {"views": {"tab:<key>": <count>, ...}}')
    if len(views) > MAX_VIEWS:
        raise UsageError("at most %d views in one post" % MAX_VIEWS)
    out = {}
    for k, v in views.items():
        if isinstance(v, bool) or not isinstance(v, int) or not 1 <= v <= MAX_COUNT:
            raise UsageError("a count is a whole number from 1 to %d" % MAX_COUNT)
        if isinstance(k, str) and VIEW.match(k) and k[4:] in TABS:
            out[k] = v
    return out


def _day(now=None):
    return datetime.datetime.fromtimestamp(clock.wall() if now is None else now,
                                           datetime.timezone.utc).strftime("%Y-%m-%d")


def _read(path):
    """A day's counts, {} for a day with none; a file that is not counts is said once
    and read as none, so the day starts again rather than the route failing."""
    try:
        with open(path) as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        sys.stderr.write("usage: %s is not counts (%s); that day starts again\n" % (path, e))
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items()
            if isinstance(k, str) and VIEW.match(k) and k[4:] in TABS
            and isinstance(v, int) and v > 0}


class Store:
    """The counts in `folder`, one file a day. `add` is called from the service's
    worker threads, so it takes a lock; a file is replaced whole, never left half
    written."""

    def __init__(self, folder):
        self.folder = folder
        self._lock = threading.Lock()
        self._pruned = None             # the day the old files were last looked for
        self._failed = False            # a write that failed has been said

    def add(self, counts, now=None):
        """Add `counts` to today's. A count is worth losing, not a failed request: a
        folder that cannot be written (read-only, full, refused) loses this post's
        counts, says so once, and leaves no file of its own behind. True when kept."""
        day = _day(now)
        path = os.path.join(self.folder, day + ".json")
        tmp = "%s.%d.tmp" % (path, os.getpid())
        with self._lock:
            try:
                os.makedirs(self.folder, mode=0o700, exist_ok=True)
                have = _read(path)
                for k, v in counts.items():
                    have[k] = have.get(k, 0) + v
                with open(tmp, "w") as fh:
                    json.dump(have, fh, sort_keys=True)
                os.replace(tmp, path)
            except OSError as e:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                if not self._failed:
                    self._failed = True
                    sys.stderr.write("usage: tab counts cannot be kept in %s (%s); they are "
                                     "dropped until it can be written\n"
                                     % (self.folder, e.strerror or e))
                return False
            if self._failed:
                self._failed = False
                sys.stderr.write("usage: tab counts are kept in %s again\n" % self.folder)
            if self._pruned != day:
                self._pruned = day
                self._prune(day)
            return True

    def _prune(self, today):
        """Each day's file older than KEEP_DAYS, removed."""
        cut = (datetime.date.fromisoformat(today)
               - datetime.timedelta(days=KEEP_DAYS)).isoformat()
        try:
            names = os.listdir(self.folder)
        except OSError:
            return
        for name in names:
            m = DAY_FILE.match(name)
            if m and m.group(1) < cut:
                try:
                    os.unlink(os.path.join(self.folder, name))
                except OSError:
                    pass

    def totals(self, days, now=None):
        """{view: (opens, the last day it was opened)} over the last `days` days,
        today among them."""
        today = datetime.date.fromisoformat(_day(now))
        first = (today - datetime.timedelta(days=max(days, 1) - 1)).isoformat()
        out = {}
        try:
            names = sorted(os.listdir(self.folder))
        except FileNotFoundError:
            return out
        for name in names:
            m = DAY_FILE.match(name)
            if not m or m.group(1) < first or m.group(1) > today.isoformat():
                continue
            for k, v in _read(os.path.join(self.folder, name)).items():
                n, _last = out.get(k, (0, None))
                out[k] = (n + v, m.group(1))
        return out


def folder(cfg):
    """<cache.dir>/usage, the working directory standing in for an unset cache.dir as
    it does for the session key and the salt."""
    return os.path.join(cfg.get("cache.dir") or ".", "usage")


def report(cfg, days, out=None, now=None):
    """`rc-dashboard usage`: each tab and how often it was opened in the last `days`
    days, most opened first. Exit 0."""
    out = out or sys.stdout
    where = folder(cfg)
    totals = Store(where).totals(days, now)
    on = cfg.get("ui.count_views") is not False
    if not totals:
        out.write("no tab opened in the last %d days, as far as %s says%s\n"
                  % (days, where, "" if on else "; counting is off (ui.count_views)"))
        return 0
    out.write("tabs opened in the last %d days, as this dashboard's page counted them (%s):\n"
              % (days, where))
    width = max(len(k) - 4 for k in totals)
    for k, (n, last) in sorted(totals.items(), key=lambda kv: (-kv[1][0], kv[0])):
        out.write("  %-*s  %6d   last opened %s\n" % (width, k[4:], n, last))
    out.write("A tab no one opened is not listed. Nothing else is kept: no user, window or "
              "filter.%s\n" % ("" if on else " Counting is off now (ui.count_views)."))
    return 0
