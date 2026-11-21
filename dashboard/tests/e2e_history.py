#!/usr/bin/env python3
"""End to end: one feed served with the history store off and on, every API diffed.

    cd dashboard && uv run python tests/e2e_history.py [--days 2] [--hosts 2]
                        [--scan] [--legacy] [--restart [--seal-between] [--grow SECONDS]]
                        [--keep] [--max-diffs 20] [--timeout 900]

The collector seals a day by gzipping its day-files, and the history store
(`history`, rc_dashboard/history.py) keeps each sealed one as Parquet: a reader
that reads a sealed file selectively -- one user's events, one session's tree,
the commands behind a figure -- or searches it by time takes the records from its
replica (replica.py) instead of inflating the `.gz`. A replica must answer
exactly what its file would, offsets and all, or a page would change with the
store. So this:

1. writes a demo feed (`demo --demo-write-only`) and seals the eBPF tier's days
   before the newest as `.gz`, as the collector does at rollover;
2. starts two `serve` processes, identical but for `history.enabled` (off, on):
   each with its own cache dir (the store is under it) and port, the clock pinned
   to the stamp's `written_to`, Ask, reverse DNS and the Slurm/user/domain inputs
   off, retention and backfill sized to the feed, and the same hash seed (set
   order is per process);
3. waits for each one's backfill, for the on service's store to hold a current
   replica of every sealed file, and for every window to be built: 60 min, 24 h,
   the whole feed, and two filtered views of the 24 h window (the busiest user;
   the processes no agent type was tagged on, `none`);
4. GETs the same requests from both -- /api/panels, /api/live, /api/trajectories
   and /api/users at every window, the filtered views, /api/trajectory and
   /api/user for a few sessions and a user, /api/live/users and /api/live/user,
   /api/events (newest and oldest first, a second page, every event kind, a
   user's and an agent type's, both orders), /api/export in both orders,
   /api/io/commands, /api/collectors and /api/feeds -- and diffs the JSON,
   type- and key-order-exact;
5. checks the on service answered from the replicas: every sealed file a reader
   asked for once the store was complete was answered from its replica
   (`history.lookups`), and the off service's store was asked nothing.

Only what depends on the store by construction is ignored (`IGNORE`, each with
its reason): timings, and the feeds report's account of the store. The first
`--max-diffs` differing paths are printed, and how many differing values each
IGNORE entry stood in for.

After each run the two caches are compared too: every saved window build, hour
and day state and saved panels must be equal by value, where each stopped in
each day-file (`files`) included -- an offset a replica gave must be its file's
own, or a resumed build would read on from somewhere else.

`--scan` turns hourly merge-reduce off (RC_DASH_MERGE_REDUCE=0): every window is
one scan. `--legacy` makes the feed a capture from before `ts_epoch` (times from
the `ts` strings), which a replica's time search must place as its file's does.
`--restart` then stops both services and starts them again over the saved builds
and the store, checks every view was resumed (`cache.source` "snapshot", or
"hours" from saved hour states with none of them reduced again) rather than read
again, and diffs again: the on service reads the replicas from its first read
on, a resumed build's checks of a sealed file (its size, its head, the line
boundary it stopped at) included. `--seal-between` leaves the day before the
newest plain at the first start and seals it between the two runs, the store
converting it before the restart: each resumed build takes up its plain file's
offsets in the `.gz`, through the replica on the one side and the `.gz` seek
index on the other. `--grow SECONDS` has the demo write that much more of the
feed before the restart, so each resumed build reads on (both clocks then pinned
by `auto`). `--keep` keeps the temp dir: the feed, both caches (the store in the
on one's) and both services' logs.

Exit status: 0 when every response is identical, 1 on any difference, 2 when
the run itself could not be made (a service that would not start, a timeout).

Named so `unittest discover` (`test*.py`) passes over it: it starts two services
and takes a minute or more. The standard library only, beside rc_dashboard's own
history store for `--seal-between`.
"""
import argparse
import atexit
import fnmatch
import itertools
import json
import math
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
DASH = os.path.dirname(HERE)
sys.path.insert(0, DASH)

LABELS = ("off", "on")
WINDOWS_FIXED = (60, 1440)
# The most --grow may add: a saved build older than its window's drift budget
# (panels.max_drift_pct, 25% of 60 min) is scanned again, not resumed.
GROW_MAX = 899

# What is ignored, and why: (path pattern, endpoints it applies to, reason).
# A path is the response's keys joined by "/", with every list index written
# "#"; patterns are fnmatch's, so "*" spans levels. Endpoints are the labels'
# first word ("panels", "feeds", ...), or "*" for all. Nothing that describes
# the records belongs here: every entry is true of the run's clock, or of the
# store itself, on in one service only.
IGNORE = (
    # -- timings
    ("*elapsed_s", "*", "a duration, timed on the monotonic clock"),
    ("*behind_wall_s", "*", "the wall clock less the pinned one: when the request ran"),
    ("_meta/generated_at", "export", "the export header's wall-clock stamp"),
    ("*/compressed/index_s", "feeds", "how long indexing the .gz files took"),
    # -- the store's own account: on in one service only
    ("ebpf/history", "feeds", "the history store's own account: on in one service only"),
    # -- not the store's, the run's: a lookup is named by a uuid4
    ("id", "iocmds", "a lookup's id, drawn at random by each service"),
)


class Fatal(Exception):
    """The run could not be made: exit 2, not a difference."""


def log(msg=""):
    sys.stdout.write(msg + "\n")
    sys.stdout.flush()


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def clean_env():
    """This process's environment without a service setting the shell may carry."""
    return {k: v for k, v in os.environ.items()
            if not k.startswith(("RC_DASH_", "EBPFM_")) and k != "RC_MEASUREMENT_ROOT"}


# ------------------------------------------------------------------ the feed
def write_feed(base, days, hosts, conf):
    """`demo --demo-write-only` into `base`; the stamp."""
    env = clean_env()
    env["RC_DASH_CONFIG"] = conf
    cmd = [sys.executable, "-m", "rc_dashboard", "demo", "--demo-dir", base,
           "--demo-days", str(days), "--demo-hosts", str(hosts), "--demo-write-only"]
    t = time.monotonic()
    got = subprocess.run(cmd, cwd=DASH, env=env, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT)
    if got.returncode != 0:
        raise Fatal("the demo could not write the feed:\n%s"
                    % got.stdout.decode("utf-8", "replace")[-4000:])
    with open(os.path.join(base, "demo.json")) as fh:
        stamp = json.load(fh)
    if not isinstance(stamp.get("written_to"), (int, float)):
        raise Fatal("the demo's stamp says no `written_to`: %r" % (stamp,))
    return stamp, time.monotonic() - t


def grow_feed(base, days, hosts, until):
    """Have the demo write the feed on to `until`, as it resumes after a restart:
    the files are appended to, nothing is rewritten. Returns the new stamp."""
    import contextlib
    import io
    from rc_dashboard import demo as demomod
    ns = argparse.Namespace(demo_dir=base, demo_days=days, demo_hosts=hosts,
                            demo_seed=demomod.DEFAULTS["seed"], demo_fresh=False)
    said = io.StringIO()
    with contextlib.redirect_stderr(said):
        d = demomod.Demo(demomod.options(ns))
        try:
            d.prepare(now=until)
        finally:
            d.stop()
    if "resuming" not in said.getvalue():
        raise Fatal("the demo wrote a new feed rather than continuing it:\n%s" % said.getvalue())
    with open(os.path.join(base, "demo.json")) as fh:
        return json.load(fh)


def legacy_feed(feed):
    """Make the feed a capture from before `ts_epoch`: every record keeps only its
    `ts` (or `timestamp`) string, read to the second (`normalize.epoch_of`), and
    every seek places a line by parsing it. Rewritten line by line (`json.dumps`,
    as the collector writes). Returns `(files, records)`."""
    files = records = 0
    for tier in ("ebpf", "node"):
        for dp, _dirs, names in sorted(os.walk(os.path.join(feed, tier))):
            for name in sorted(names):
                if not name.endswith(".jsonl"):
                    continue
                src = os.path.join(dp, name)
                tmp = os.path.join(dp, ".%s.tmp" % name)
                with open(src, "rb") as fi, open(tmp, "w") as fo:
                    for line in fi:
                        if line.strip():
                            rec = json.loads(line)
                            rec.pop("ts_epoch", None)
                            fo.write(json.dumps(rec) + "\n")
                            records += 1
                os.replace(tmp, src)
                files += 1
    return files, records


def ebpf_days(feed):
    root = os.path.join(feed, "ebpf")
    return sorted(d for d in os.listdir(root) if d.startswith("20"))


def seal_days(feed, keep=1):
    """gzip every eBPF-tier day-file of every day before the newest `keep`, as the
    collector seals a day at rollover (`_compress_day`): `x.jsonl.gz` written
    whole, then `x.jsonl` removed. The node tier is never compressed. Returns the
    number of files sealed."""
    import gzip
    root = os.path.join(feed, "ebpf")
    n = 0
    for day in ebpf_days(feed)[:-keep]:
        d = os.path.join(root, day)
        for name in sorted(os.listdir(d)):
            if not name.endswith(".jsonl"):
                continue
            src = os.path.join(d, name)
            tmp = os.path.join(d, ".%s.gz.tmp" % name)
            with open(src, "rb") as fi, open(tmp, "wb") as raw, \
                    gzip.GzipFile(filename=name, mode="wb", fileobj=raw, mtime=0) as fo:
                shutil.copyfileobj(fi, fo, 1 << 20)
            os.replace(tmp, src + ".gz")
            os.unlink(src)
            n += 1
    return n


def sealed_files(feed):
    """Every sealed eBPF-tier day-file: what the store keeps a replica of."""
    root = os.path.join(feed, "ebpf")
    return sorted(os.path.join(root, day, n) for day in ebpf_days(feed)
                  for n in os.listdir(os.path.join(root, day)) if n.endswith(".jsonl.gz"))


def feed_records(feed):
    """How many records the feed holds, both tiers."""
    import gzip
    n = 0
    for tier in ("ebpf", "node"):
        for dp, _dirs, names in sorted(os.walk(os.path.join(feed, tier))):
            for name in sorted(names):
                if name.endswith((".jsonl", ".jsonl.gz")):
                    p = os.path.join(dp, name)
                    with (gzip.open(p, "rb") if p.endswith(".gz") else open(p, "rb")) as fh:
                        n += sum(1 for line in fh if line.strip())
    return n


def preconvert(roots, cache):
    """The on service's store brought up to date in this process, as the service
    would in the background: a restart then finds every sealed file's replica
    before its first read. Returns how many files were converted."""
    from rc_dashboard import history

    class Cfg(dict):
        def get(self, key, default=None):
            return dict.get(self, key, default)
    st = history.HistoryStore(Cfg({"feeds.ebpf.roots": roots, "cache.dir": cache,
                                   "history": {"workers": 2}}))
    n = st.run_once()
    errors = st.status()["errors"]
    if errors:
        raise Fatal("the store could not convert a sealed file: %r" % (errors,))
    return n


# ---------------------------------------------------------------- a service
class Service:
    """`python -m rc_dashboard serve`, configured by RC_DASH_* alone."""

    def __init__(self, name, env, cache, logdir):
        self.name = name
        self.base_env = env
        self.cache = cache
        self.log_path = os.path.join(logdir, "serve-%s.log" % name)
        self.port = None
        self.proc = None
        self._log = None

    def start(self):
        self.port = free_port()
        env = dict(self.base_env, RC_DASH_PORT=str(self.port), RC_DASH_CACHE_DIR=self.cache)
        self._log = open(self.log_path, "ab")
        self._log.write(("\n==== start %s on :%d ====\n" % (self.name, self.port)).encode())
        self._log.flush()
        self.proc = subprocess.Popen([sys.executable, "-m", "rc_dashboard", "serve"],
                                     cwd=DASH, env=env, stdout=self._log,
                                     stderr=subprocess.STDOUT, start_new_session=True)
        return self

    def stop(self):
        p, self.proc = self.proc, None
        if p is not None and p.poll() is None:
            try:
                os.killpg(p.pid, signal.SIGINT)
                p.wait(30)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    os.killpg(p.pid, signal.SIGKILL)
                except OSError:
                    pass
                p.wait()
        if self._log is not None:
            self._log.close()
            self._log = None

    def alive(self):
        if self.proc is None or self.proc.poll() is not None:
            raise Fatal("the %s service exited (%s); its log ends:\n%s" % (
                self.name, None if self.proc is None else self.proc.returncode,
                self.log_tail()))

    def log_tail(self, n=40):
        try:
            with open(self.log_path, "rb") as fh:
                return b"\n".join(fh.read().splitlines()[-n:]).decode("utf-8", "replace")
        except OSError:
            return "(no log)"

    def url(self, path):
        return "http://127.0.0.1:%d%s" % (self.port, path)

    def get(self, path, timeout=600):
        with urllib.request.urlopen(self.url(path), timeout=timeout) as r:
            return r.read()

    def json(self, path, timeout=600):
        try:
            return json.loads(self.get(path, timeout))
        except urllib.error.HTTPError as e:
            raise Fatal("%s %s -> HTTP %d: %s" % (self.name, path, e.code, e.read()[:300]))

    def lines(self, path, timeout=600):
        """A streamed body, a line at a time."""
        try:
            r = urllib.request.urlopen(self.url(path), timeout=timeout)
        except urllib.error.HTTPError as e:
            raise Fatal("%s %s -> HTTP %d: %s" % (self.name, path, e.code, e.read()[:300]))
        try:
            for line in r:
                yield line
        finally:
            r.close()

    def wait(self, pred, what, timeout):
        end = time.monotonic() + timeout
        while True:
            self.alive()
            try:
                if pred():
                    return
            except (urllib.error.URLError, ConnectionError, socket.timeout, OSError, ValueError):
                pass
            if time.monotonic() > end:
                raise Fatal("%s service: timed out after %d s waiting for %s; its log ends:\n%s"
                            % (self.name, timeout, what, self.log_tail()))
            time.sleep(0.25)

    def view(self, minutes, flt=None):
        for w in self.json("/api/windows", 60).get("held") or []:
            if w.get("minutes") == minutes and (w.get("filter") or None) == (flt or None):
                return w
        return None

    def store(self):
        """The history store's report (/api/feeds, `ebpf.history`)."""
        return (self.json("/api/feeds", 60).get("ebpf") or {}).get("history") or {}


def stop_all(services):
    for s in services:
        s.stop()


def query(params):
    return urllib.parse.urlencode(params, doseq=True)


def flt_of(params):
    """The `filter` a view's status names for these query params, or None."""
    users = sorted(params.get("user", []))
    types = sorted(params.get("agent_type", []))
    return {"users": users, "agent_types": types} if users or types else None


def wait_backfill(services, timeout):
    for s in services:
        def backfilled(s=s):
            b = s.json("/api/health", 60).get("backfill") or {}
            if b.get("state") == "error":
                raise Fatal("%s service: backfill failed: %s" % (s.name, b.get("error")))
            return b.get("state") in ("done", "skipped")
        s.wait(backfilled, "the backfill", timeout)


def wait_store(s, sealed, timeout):
    """The service's store holds a current replica of each of the `sealed` files
    and is idle. Returns its report."""
    def complete():
        h = s.store()
        if h.get("state") == "error" or h.get("errors") \
                or (h.get("progress") or {}).get("failed"):
            raise Fatal("%s service: the history store failed: %s %r" % (
                s.name, h.get("note"), h.get("errors")))
        return h.get("state") == "idle" and h.get("replicas") == sealed
    s.wait(complete, "the history store to hold %d replica(s)" % sealed, timeout)
    return s.store()


def wait_views(services, views, timeout):
    """Every view in `views`, (minutes, params), built. A view not held is asked
    for (`/api/panels`), which queues its build; one whose build failed fails
    the run."""
    for minutes, params in views:
        for s in services:
            asked = [0.0]

            def ready(s=s, minutes=minutes, params=params, asked=asked):
                v = s.view(minutes, flt_of(params))
                if v is None:
                    if time.monotonic() - asked[0] > 5:
                        s.json("/api/panels?" + query(dict(params, window_min=minutes)), 60)
                        asked[0] = time.monotonic()
                    return False
                if v.get("state") == "error":
                    raise Fatal("%s service: the %s view failed to build: %s"
                                % (s.name, label(minutes, params), v.get("error")))
                return v.get("state") == "ready"
            s.wait(ready, "the %s view" % label(minutes, params), timeout)


def label(minutes, params=None):
    return "%d min%s" % (minutes, (" " + query(params)) if params else "")


# ------------------------------------------------------------------- diffing
class Norm:
    """What makes two responses comparable without hiding a record: either
    cache dir is `<cache>` (a saved build names it; the store is under it)."""

    def __init__(self, caches):
        self.caches = sorted(caches, key=len, reverse=True)

    def text(self, s):
        for c in self.caches:
            if c in s:
                s = s.replace(c, "<cache>")
        return s


# How many differing values each IGNORE entry stood in for, over the run: what
# the summary prints, so an entry that hides more than it says is seen.
SUPPRESSED = {}


def ignored(kind, path):
    """The IGNORE entry that covers `path` of a `kind` response, or None."""
    for rule in IGNORE:
        pat, where, _why = rule
        if (where == "*" or where == kind) and fnmatch.fnmatchcase(path, pat):
            return rule
    return None


def skip(rule, a, b, kind, norm):
    """Leave out an ignored value, counting it when the two differed."""
    if diff(a, b, kind, norm, ("#",), check=False):
        SUPPRESSED[rule] = SUPPRESSED.get(rule, 0) + 1


def diff(a, b, kind, norm, path=(), out=None, check=True):
    """Every path where `a` and `b` differ, as `(path, a, b)`: type-exact (an
    int is not a float), lists in order, and a dict's keys in order too -- one
    built from records in another order is a reader's difference as surely as
    a list is. `check=False` applies no IGNORE entry (a value one covers)."""
    out = [] if out is None else out
    key = "/".join("#" if isinstance(p, int) else str(p) for p in path)
    if path and check:
        rule = ignored(kind, key)
        if rule is not None:
            skip(rule, a, b, kind, norm)
            return out
    shown = "/".join("[%d]" % p if isinstance(p, int) else str(p) for p in path) or "/"
    if isinstance(a, str) and isinstance(b, str):
        if norm.text(a) != norm.text(b):
            out.append((shown, a, b))
    elif type(a) is not type(b):
        out.append((shown, a, b))
    elif isinstance(a, dict):
        both = [k for k in a if k in b]
        if both != [k for k in b if k in a]:
            out.append((shown + " (key order)", both, [k for k in b if k in a]))
        for k in list(a) + [k for k in b if k not in a]:
            sub = "/".join("#" if isinstance(p, int) else str(p) for p in path + (k,))
            rule = ignored(kind, sub) if check else None
            if rule is not None:
                skip(rule, a.get(k, "<absent>"), b.get(k, "<absent>"), kind, norm)
                continue
            if k not in b or k not in a:
                out.append(("%s/%s" % ("" if shown == "/" else shown, k),
                            a.get(k, "<absent>"), b.get(k, "<absent>")))
            else:
                diff(a[k], b[k], kind, norm, path + (k,), out, check)
    elif isinstance(a, list):
        if len(a) != len(b):
            out.append((shown + " (length)", len(a), len(b)))
        for i, (x, y) in enumerate(zip(a, b)):
            diff(x, y, kind, norm, path + (i,), out, check)
    elif isinstance(a, float):
        if not ((a != a and b != b) or a == b):
            out.append((shown, a, b))
    elif a != b:
        out.append((shown, a, b))
    return out


def short(v, n=110):
    s = json.dumps(v, default=str) if not isinstance(v, str) else repr(v)
    return s if len(s) <= n else s[:n - 3] + "..."


class Report:
    def __init__(self, max_diffs):
        self.max = max_diffs
        self.first = []
        self.total = 0
        self.requests = 0
        self.differing = []

    def add(self, name, found, note=""):
        self.requests += 1
        if found:
            self.differing.append(name)
            self.total += len(found)
            for p, a, b in found:
                if len(self.first) < self.max:
                    self.first.append((name, p, a, b))
        log("  %-5s %s%s" % ("DIFF" if found else "ok", name,
                             (" (%d path(s))" % len(found) if found else "") + note))

    def print(self, phase):
        if not self.total:
            log("%s: %d request(s), every response identical" % (phase, self.requests))
            return
        log("%s: %d of %d request(s) differ, %d differing path(s); the first %d:" % (
            phase, len(self.differing), self.requests, self.total, len(self.first)))
        for name, p, a, b in self.first:
            log("  %s  %s" % (name, p))
            log("      %-6s %s" % (LABELS[0] + ":", short(a)))
            log("      %-6s %s" % (LABELS[1] + ":", short(b)))


def kind_of(path):
    """The IGNORE endpoint label for a request path."""
    p = path.split("?", 1)[0]
    if p.startswith("/api/io/commands"):
        return "iocmds"
    name = p.rsplit("/", 1)[-1]
    if name == "events" and any(k in path for k in ("user=", "agent_type=", "host=")):
        return "events-filtered"
    return name


def compare_json(services, path, report, norm):
    a, b = (s.json(path) for s in services)
    report.add(path, diff(a, b, kind_of(path), norm))
    return a


def compare_stream(services, path, report, norm):
    """A streamed export, a line at a time: both bodies in step, never held whole."""
    kind = kind_of(path)
    found = []
    n = 0
    for n, (la, lb) in enumerate(itertools.zip_longest(*(s.lines(path) for s in services)), 1):
        if la is None or lb is None:
            found.append(("[%d]" % (n - 1), short(la), short(lb)))
            break
        a, b = json.loads(la), json.loads(lb)
        for p, x, y in diff(a, b, kind, norm):
            found.append(("[%d]%s" % (n - 1, p if p.startswith("/") else "/" + p), x, y))
            if len(found) > 1000:
                break
        if len(found) > 1000:
            break
    report.add(path, found, " (%d line(s))" % n)


def iocmds(services, params, report, norm, timeout):
    """`/api/io/commands`: start the lookup on both, wait for it, diff its pages."""
    path = "/api/io/commands?" + query(params)
    done = []
    for s in services:
        jid = s.json(path)["id"]
        s.wait(lambda s=s, jid=jid: s.json("/api/io/commands/%s" % jid, 60)["state"]
               in ("done", "error"), "the I/O lookup %s" % path, timeout)
        done.append(jid)
    for sort in ("rd_mb", "cmd"):
        got = [s.json("/api/io/commands/%s?limit=1000&sort=%s" % (jid, sort))
               for s, jid in zip(services, done)]
        name = "%s [sort=%s]" % (path, sort)
        report.add(name, diff(got[0], got[1], "iocmds", norm))


# ----------------------------------------------------------------- the run
def busiest_user(s):
    """The user with the most exits in the 24 h window, as `s` counts them: the
    one the filtered views, a portrait, the events and the I/O lookup ask for."""
    users = (s.json("/api/panels?window_min=1440").get("facets") or {}).get("users") or []
    if not users:
        raise Fatal("the 24 h window names no user: nothing to filter by")
    return users[0]["user"]


def views_of(user, full):
    """Every view the run waits for and diffs: (minutes, query params)."""
    return [(m, {}) for m in WINDOWS_FIXED + (full,)] + [
        (1440, {"user": [user]}), (1440, {"agent_type": ["none"]})]


def compare_all(services, user, full, report, norm, timeout):
    windows = WINDOWS_FIXED + (full,)
    for m in windows:
        compare_json(services, "/api/panels?window_min=%d" % m, report, norm)
        compare_json(services, "/api/live?window_min=%d" % m, report, norm)
        compare_json(services, "/api/trajectories?window_min=%d" % m, report, norm)
        compare_json(services, "/api/users?window_min=%d" % m, report, norm)
    for params in ({"user": [user]}, {"agent_type": ["none"]}):
        q = query(dict(params, window_min=1440))
        compare_json(services, "/api/panels?" + q, report, norm)
        compare_json(services, "/api/trajectories?" + q, report, norm)
        compare_json(services, "/api/users?" + q, report, norm)
    for m in (1440, full):
        compare_json(services, "/api/user?" + query({"name": user, "window_min": m}),
                     report, norm)
    # the live tier past what /api/live carries: every account, and one account's
    # newest exited processes (the read-back's order, ties and all)
    compare_json(services, "/api/live/users?" + query({"window_min": full, "limit": 5000}),
                 report, norm)
    compare_json(services, "/api/live/user?" + query({"name": user, "window_min": full,
                                                      "n": 1000}), report, norm)
    # A few sessions as process trees, each of them a key search through every
    # day-file of the window: the busiest agents and a terminal, both orders, at
    # 24 h and over the whole feed (its sealed days).
    tr = services[0].json("/api/trajectories?rank=events&window_min=%d" % full)
    rows = tr.get("rows") or {}
    ranked = sorted(rows, key=lambda k: (-((rows[k] or {}).get("n_events") or 0), k))
    sids, kinds = [], set()
    for sid in ranked:
        r = rows[sid] or {}
        kind = (r.get("actor3"), r.get("agent_type"), r.get("key_type"))
        if kind not in kinds:
            kinds.add(kind)
            sids.append(sid)
        if len(sids) >= 4:
            break
    for sid in sids:
        for m in (1440, full):
            for order in ("oldest", "newest"):
                compare_json(services, "/api/trajectory?" + query(
                    {"sid": sid, "window_min": m, "order": order}), report, norm)
    every = "exit,truncated,residency,residency_totals,conn,netio,submit,meta,stop"
    for params in ({"limit": 500},
                   {"limit": 500, "order": "asc"},
                   {"limit": 500, "cursor": 500},
                   {"limit": 500, "event": every},
                   {"limit": 500, "user": user},
                   {"limit": 500, "user": user, "order": "asc"},
                   {"limit": 500, "user": user, "cursor": 500},
                   {"limit": 500, "user": user, "event": "exit", "order": "asc"},
                   {"limit": 500, "agent_type": "none"},
                   {"limit": 500, "agent_type": "none", "order": "asc"}):
        compare_json(services, "/api/events?" + query(params), report, norm)
    compare_stream(services, "/api/export?order=asc", report, norm)
    compare_stream(services, "/api/export", report, norm)
    for m in (1440, full):
        iocmds(services, {"window_min": m, "user": user}, report, norm, timeout)
    compare_json(services, "/api/collectors", report, norm)
    compare_json(services, "/api/feeds", report, norm)


def check_lookups(services, before):
    """The off service's store was asked nothing; the on service's answered from
    its replicas every sealed file asked for since `before` (its `lookups` when
    its store was complete), and some were asked. Returns `(said, problems)`."""
    off, on = (s.store() for s in services)
    bad = []
    lo = off.get("lookups") or {}
    if off.get("state") != "off" or any(lo.values()):
        bad.append("off service: its store %s, lookups %r" % (off.get("state"), lo))
    ln = on.get("lookups") or {}
    since = {k: (ln.get(k) or 0) - (before.get(k) or 0) for k in ("replica", "jsonl")}
    if since["jsonl"] or not since["replica"]:
        bad.append("on service: since its store was complete, %d sealed file(s) read from "
                   "a replica and %d as JSONL" % (since["replica"], since["jsonl"]))
    said = "%d lookup(s) answered from a replica since the store was complete (%d before " \
        "it was, %d of them as JSONL)" % (since["replica"],
                                          (before.get("replica") or 0)
                                          + (before.get("jsonl") or 0),
                                          before.get("jsonl") or 0)
    return said, bad


# ------------------------------------------------------------ saved states
def _plain(o):
    """A saved state as plain comparable data: an object as its class and
    fields, a set sorted, a NaN equal to itself."""
    if isinstance(o, float):
        return ("NaN",) if o != o else o
    if isinstance(o, dict):
        return {_key(k): _plain(v) for k, v in o.items()}
    if isinstance(o, list):
        return [_plain(x) for x in o]
    if isinstance(o, tuple):
        return tuple(_plain(x) for x in o)
    if isinstance(o, (set, frozenset)):
        return ("set", sorted(json.dumps(_plain(x), sort_keys=True, default=repr) for x in o))
    if hasattr(o, "__dict__") and not isinstance(o, type):
        return (type(o).__name__, _plain(vars(o)))
    slots = getattr(type(o), "__slots__", None)
    if slots and not isinstance(o, type):
        return (type(o).__name__, {k: _plain(getattr(o, k, None)) for k in slots})
    return o


def _key(k):
    return json.dumps(_plain(k), default=repr) if isinstance(k, tuple) else k


def _cache_file(path):
    """`(meta, state)` of a saved window build, hour state or saved panels. An
    hour's own figures (`o<hour>`) are written packed, each part compressed on
    its own (`hours.HourStore._save_figures`): their state is every part's."""
    import pickle
    import zlib
    with open(path, "rb") as fh:
        meta = pickle.load(fh)
        if os.sep + "windows" + os.sep in path:
            return meta, pickle.load(fh)
        if meta.get("kind") == "figures":
            return meta, {p: pickle.loads(zlib.decompress(b))
                          for p, b in pickle.loads(fh.read()).items()}
        return meta, pickle.loads(zlib.decompress(fh.read()))


def code_digest():
    """The dashboard's code as it is on disk: a service started after it changes
    runs other code, and saves its builds under another fingerprint."""
    import hashlib
    h = hashlib.sha1()
    for dp, dirs, files in sorted(os.walk(os.path.join(DASH, "rc_dashboard"))):
        dirs.sort()
        for name in sorted(files):
            if name.endswith(".py"):
                with open(os.path.join(dp, name), "rb") as fh:
                    h.update(name.encode() + b"\0" + fh.read())
    return h.hexdigest()


def compare_caches(caches, report, norm):
    """The two services' saved states -- every window build, hour and day state
    and saved panels -- are equal by value, the day-files each read and where it
    stopped (`files`) included. Each is named by a fingerprint of the config,
    which the store is not part of (`snapshots.fingerprint`), and of its cache
    dir, which is: that alone may differ. A state the API does not show yet
    would show the next time it is merged or resumed."""
    by = []
    for f in LABELS:
        got, fps = {}, set()
        for sub in ("windows", "hours", "payloads"):
            d = os.path.join(caches[f], sub)
            for name in sorted(os.listdir(d)) if os.path.isdir(d) else ():
                parts = name.split(".")
                if len(parts) == 3 and not name.startswith("."):
                    got["%s/%s.%s" % (sub, parts[0], parts[2])] = os.path.join(d, name)
                    fps.add((sub, parts[1]))
        if len(fps) != len({sub for sub, _fp in fps}):
            raise Fatal("the %s cache holds states saved under more than one fingerprint "
                        "(%s): the code or the config changed during the run" % (
                            f, ", ".join("%s %s" % x for x in sorted(fps))))
        by.append(got)
    found = []
    for k in sorted(set(by[0]) | set(by[1])):
        if k not in by[0] or k not in by[1]:
            found.append((k, "saved" if k in by[0] else "<absent>",
                          "saved" if k in by[1] else "<absent>"))
            continue
        try:
            (ma, sa), (mb, sb) = (_cache_file(x[k]) for x in by)
        except Exception as e:
            raise Fatal("a saved state could not be read (%s): %s" % (k, e))
        for m in (ma, mb):
            m.pop("fingerprint", None)
        found += [("%s %s%s" % (k, part, p), x, y) for part, a, b in (
            ("meta", ma, mb), ("state", sa, sb))
            for p, x, y in diff(_plain(a), _plain(b), "state", norm)]
    report.add("saved states (%d file(s) each)" % len(by[0]), found)


def resumed(services, views, scan, grown=0):
    """After a restart: every view resumed from what the first run saved. A
    scanned window comes back from its saved build ("snapshot"), read on from
    where it stopped; with merge-reduce, a window with whole hours inside it is
    merged from the saved hour states ("hours"), and none of them is reduced
    again -- only an hour the feed's growth (`grown` seconds) closed. A
    filtered view whose open hour that growth closed is read again ("scan"):
    it joined that hour's exits within itself, and a build now takes the
    figures the hours worked out there (`WindowRegistry._resume`)."""
    bad = []
    new_hours = int(math.ceil(grown / 3600.0)) + 1 if grown else 0
    for s in services:
        st = s.json("/api/windows", 60)
        for minutes, params in views:
            v = s.view(minutes, flt_of(params))
            src = ((v or {}).get("cache") or {}).get("source")
            note = ((v or {}).get("cache") or {}).get("note")
            ok = src == "snapshot" or (src == "hours" and not scan) or (
                src == "scan" and bool(grown and params) and "has closed since" in (note or ""))
            log("  %-5s %-4s %-34s cache.source=%s  %s" % (
                "ok" if ok else "FAIL", s.name, label(minutes, params), src, note or ""))
            if not ok:
                bad.append("%s %s: cache.source=%s (%s)" % (s.name, label(minutes, params),
                                                           src, note))
        hours = st.get("hours")
        if hours is not None:
            n = hours.get("hours_reduced") or 0
            log("  %-5s %-4s hour states reduced since the restart: %d (at most %d new)" % (
                "ok" if n <= new_hours else "FAIL", s.name, n, new_hours))
            if n > new_hours:
                bad.append("%s: %d hour state(s) reduced after the restart, where at most %d "
                           "closed since" % (s.name, n, new_hours))
    return bad


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Serve one demo feed with the history store off and on and diff "
                    "every API.")
    ap.add_argument("--days", type=int, default=2, help="days of demo history (2; 1..14)")
    ap.add_argument("--hosts", type=int, default=2, help="demo login nodes (2; 1..6)")
    ap.add_argument("--scan", action="store_true",
                    help="hourly merge-reduce off (RC_DASH_MERGE_REDUCE=0)")
    ap.add_argument("--legacy", action="store_true",
                    help="drop ts_epoch from every record (times from the `ts` strings)")
    ap.add_argument("--restart", action="store_true",
                    help="then restart both services, check the saved builds were "
                         "resumed, and diff again")
    ap.add_argument("--seal-between", action="store_true",
                    help="with --restart: leave the day before the newest plain at the "
                         "first start, and seal it (and convert it) before the restart")
    ap.add_argument("--grow", type=int, default=0, metavar="SECONDS",
                    help="with --restart: have the demo write SECONDS more of the feed "
                         "before the restart (1..%d, inside the 60 min window's drift "
                         "budget), so the resumed builds read on; both clocks are then "
                         "pinned by `auto`" % (GROW_MAX,))
    ap.add_argument("--keep", action="store_true", help="keep the temp dir")
    ap.add_argument("--max-diffs", type=int, default=20,
                    help="differing paths printed (20)")
    ap.add_argument("--timeout", type=int, default=900,
                    help="seconds to wait for a backfill, the store or a build (900)")
    a = ap.parse_args(argv)
    days = max(1, min(14, a.days))
    hosts = max(1, min(6, a.hosts))
    if a.grow and not (a.restart and 0 < a.grow <= GROW_MAX):
        ap.error("--grow takes --restart and 1..%d seconds" % GROW_MAX)
    if a.grow and a.legacy:
        ap.error("--grow continues the feed as the demo writes it, with ts_epoch: "
                 "not with --legacy")
    if a.seal_between and not a.restart:
        ap.error("--seal-between seals a day between the two runs: it takes --restart")

    tmp = tempfile.mkdtemp(prefix="rc-e2e-history-")
    services = []

    def cleanup():
        stop_all(services)
        if a.keep:
            log("kept %s" % tmp)
        else:
            shutil.rmtree(tmp, ignore_errors=True)
    atexit.register(cleanup)
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, lambda *_: sys.exit(2))

    try:
        return run(a, tmp, days, hosts, services)
    except Fatal as e:
        log("ERROR: %s" % e)
        return 2
    except KeyboardInterrupt:
        log("interrupted")
        return 2


def run(a, tmp, days, hosts, services):
    t0 = time.monotonic()
    code = code_digest()

    def same_code():
        if code_digest() != code:
            raise Fatal("rc_dashboard's code changed on disk during the run, so its two "
                        "phases ran different code; run it again")
    base = os.path.join(tmp, "demo")
    conf = os.path.join(tmp, "config.json")
    with open(conf, "w") as fh:
        fh.write("{}\n")            # no dashboard.config.json of this checkout's
    log("e2e history: %d day(s), %d host(s), %s, the history store off and on, in %s" % (
        days, hosts, "one scan per window (merge-reduce off)" if a.scan
        else "hourly merge-reduce", tmp))

    stamp, took = write_feed(base, days, hosts, conf)
    feed = os.path.join(base, "feed")
    as_of = stamp["written_to"]
    span_min = int(math.ceil((as_of - stamp["start"]) / 60.0))
    full = span_min + 60            # the whole feed, and an hour before it
    log("feed: written in %.1f s, %s .. %s (%d min), %.1f MB" % (
        took, time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stamp["start"])),
        time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(as_of)), span_min,
        sum(os.path.getsize(os.path.join(dp, f)) for dp, _d, fs in os.walk(feed) for f in fs)
        / 1e6))
    if a.legacy:
        n, recs = legacy_feed(feed)
        log("feed made a legacy capture: ts_epoch dropped from %d record(s) of %d file(s)"
            % (recs, n))
    sealed = seal_days(feed, 2 if a.seal_between else 1)
    if not sealed:
        raise Fatal("the feed has no day to seal before %s: give it more --days"
                    % ("the newest two" if a.seal_between else "the newest"))
    records = feed_records(feed)
    log("feed: %d record(s), %d eBPF day-file(s) of %s sealed as .gz" % (
        records, sealed, ", ".join(ebpf_days(feed)[:-(2 if a.seal_between else 1)])))

    none = os.path.join(tmp, "not-set-up")
    env = clean_env()
    env.update({
        "RC_DASH_CONFIG": conf,
        "RC_DASH_EBPF_ROOTS": os.path.join(feed, "ebpf"),
        "RC_DASH_NODE_ROOTS": os.path.join(feed, "node"),
        "RC_DASH_EBPF_DAYS": str(days + 2),
        # the stamp's `written_to`; with --grow, `auto` (the newest record's
        # second after), the one pinned clock that can move between two runs
        # without changing the config every saved build is fingerprinted with
        "RC_DASH_AS_OF": "auto" if a.grow else repr(float(as_of)),
        "RC_DASH_HOST": "127.0.0.1",
        "RC_DASH_RETENTION_MIN": str(full),
        "RC_DASH_BACKFILL_HOURS": str(int(math.ceil(full / 60.0))),
        "RC_DASH_PREWARM": "60,%d" % full,
        "RC_DASH_MAX_WINDOWS": "8",
        "RC_DASH_MERGE_REDUCE": "0" if a.scan else "1",
        "RC_DASH_ASK": "0",
        # no password: two services on 127.0.0.1 over a demo feed
        "RC_DASH_OPEN": "1",
        # names come from the records' own host names, as in a demo: a lookup's
        # answer depends on when it landed, which no store decides
        "RC_DASH_RDNS": "0",
        "RC_DASH_STATIC_DIR": none,
        # set iteration order is drawn per process; fixed, two services that
        # build the same state serve it in the same order
        "PYTHONHASHSEED": "0",
        # the store under each service's cache dir; two workers, as a small host
        "RC_DASH_HISTORY_WORKERS": "2",
    })
    caches = {f: os.path.join(tmp, "cache-%s" % f) for f in LABELS}
    services.append(Service("off", dict(env, RC_DASH_HISTORY="0"), caches["off"], tmp))
    services.append(Service("on", dict(env, RC_DASH_HISTORY="1"), caches["on"], tmp))
    norm = Norm(caches.values())

    failed = False
    for phase in (("first start", "restart") if a.restart else ("first start",)):
        log("")
        log("== %s ==" % phase)
        if phase == "restart" and (a.seal_between or a.grow):
            said = []
            if a.seal_between:
                n = seal_days(feed, 1)
                done = preconvert([os.path.join(feed, "ebpf")], caches["on"])
                said.append("sealed %d day-file(s) of %s, and the store converted %d" % (
                    n, ebpf_days(feed)[-2], done))
            if a.grow:
                stamp = grow_feed(base, days, hosts, stamp["written_to"] + a.grow)
                said.append("grown by %d s to %s" % (a.grow, time.strftime(
                    "%Y-%m-%d %H:%M:%S", time.localtime(stamp["written_to"]))))
            n = feed_records(feed)
            log("feed %s: %d record(s)%s" % ("; ".join(said), n,
                                              " (%+d)" % (n - records) if a.grow else ""))
            records = n
        n_sealed = len(sealed_files(feed))
        t = time.monotonic()
        same_code()
        for s in services:
            s.start()
        wait_backfill(services, a.timeout)
        h = wait_store(services[1], n_sealed, a.timeout)
        before = dict(h.get("lookups") or {})
        if phase == "restart":
            # the store was complete before the service started: every lookup counts
            before = {}
        log("store: %d replica(s) of %d sealed file(s), %d Parquet file(s), %.1f MB, "
            "%d converted by the service" % (
                h.get("replicas") or 0, n_sealed, h.get("files") or 0,
                (h.get("bytes") or 0) / 1e6, h.get("converted") or 0))
        wait_views(services, [(m, {}) for m in WINDOWS_FIXED + (full,)], a.timeout)
        user = busiest_user(services[0])
        views = views_of(user, full)
        wait_views(services, views, a.timeout)
        log("ready in %.1f s: backfill done, views %s" % (
            time.monotonic() - t, ", ".join(label(m, p) for m, p in views)))
        if phase == "restart":
            bad = resumed(services, views, a.scan, a.grow)
            if bad:
                failed = True
                log("not resumed: " + "; ".join(bad))
        # The feed report an export's header carries is the one last made
        # (`refresh_feeds`, on /api/feeds): made anew on both, once both have
        # read their history, rather than whenever this run happened to ask one.
        for s in services:
            s.json("/api/feeds", 60)
        report = Report(a.max_diffs)
        compare_all(services, user, full, report, norm, a.timeout)
        said, bad = check_lookups(services, before)
        if bad:
            failed = True
            log("NOT read from the replicas: " + "; ".join(bad))
        else:
            log("store: %s" % said)
        stop_all(services)
        same_code()
        compare_caches(caches, report, norm)
        report.print(phase)
        failed = failed or bool(report.total)
    log("")
    log("ignored as store-dependent by construction (IGNORE), values that differed:")
    for rule in IGNORE:
        if SUPPRESSED.get(rule):
            log("  %6d  %-28s %-16s %s" % (SUPPRESSED[rule], rule[0], rule[1], rule[2]))
    quiet = sum(1 for r in IGNORE if not SUPPRESSED.get(r))
    log("  (%d other entr%s stood in for no difference this run)" % (
        quiet, "y" if quiet == 1 else "ies"))
    log("")
    log("%s in %.0f s" % ("DIFFERENT" if failed else "IDENTICAL", time.monotonic() - t0))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
