#!/usr/bin/env python3
"""End to end: one feed written in both formats, served twice, every API diffed.

    cd dashboard && uv run python tests/e2e_formats.py [--days 2] [--hosts 2]
                        [--scan] [--gzip] [--legacy] [--restart [--grow SECONDS]]
                        [--convert [--convert-feed jsonl|both]]
                        [--keep] [--max-diffs 20] [--timeout 900]

A collector on EBPFM_FORMAT=both writes each stream twice, as twins
(`<host>.<stream>.jsonl` and `.ebr`), and the service reads exactly one of them,
the one `feeds.ebpf.prefer` / `feeds.ebpf_node.prefer` names. The same records
must make the same page whichever twin is read. So this:

1. writes a demo feed in both formats (`demo --demo-format both --demo-write-only`);
2. checks every twin pair decodes alike: `ebr.iter_records` of the EBR twin
   against `json.loads` of each line of the JSONL one, type- and key-order-exact;
3. starts two `serve` processes, identical but for `prefer` (jsonl, ebr): each
   with its own cache dir and port, the clock pinned to the stamp's `written_to`,
   Ask, reverse DNS and the Slurm/user/domain inputs off, retention and backfill
   sized to the feed, and the same hash seed (set order is per process);
4. waits for each one's backfill and for every window to be built: 60 min, 24 h,
   the whole feed, and two filtered views of the 24 h window (the busiest user;
   the processes no agent type was tagged on, `none`);
5. GETs the same requests from both -- /api/panels, /api/live, /api/trajectories
   and /api/users at every window, the filtered views, /api/trajectory and
   /api/user for a few sessions and a user, /api/live/users and /api/live/user,
   /api/events (newest and oldest first, a second page, every event kind, a
   user's), /api/export in both orders, /api/io/commands, /api/collectors and
   /api/feeds -- and diffs the JSON.

Only what depends on the format by construction is ignored (`IGNORE`, each with
its reason): byte counts and byte progress, timings, the feeds report's account
of the twins, and the scan counts of reads a filter narrows. A day-file's name,
in a path or in a sentence, is compared with its format suffix written
`.<fmt>`, and the two cache dirs as one, rather than ignored. The first
`--max-diffs` differing paths are printed, and how many differing values each
IGNORE entry stood in for.

After each run the two caches are compared too: every saved window build, hour
and day state and saved panels must be equal by value, the day-files each read
and where it stopped aside -- state the API does not show yet would show when it
is next merged or resumed.

`--scan` turns hourly merge-reduce off (RC_DASH_MERGE_REDUCE=0): every window is
one scan. `--restart` then stops both services and starts them again over the
saved builds, checks every view was resumed (`cache.source` "snapshot", or
"hours" from saved hour states with none of them reduced again) rather than read
again, and diffs again. Over an unchanged feed and clock a resumed build has
nothing left to read; `--grow SECONDS` has the demo write that much more of the
feed first, as it resumes after a restart (an EBR file's last segment
continued), so each resumed build reads on from where it stopped -- and both
runs pin the clock with `auto` instead, because a pinned epoch is part of the
config every saved build is fingerprinted with. `--gzip` seals every day
before the newest as the collector does at rollover (`x.jsonl` -> `x.jsonl.gz`,
`x.ebr` -> `x.ebr.gz`), so every read goes through the `.gz` seek index too:
before the first start, or with `--restart` between the two runs, when each
resumed build must take up its plain files' offsets in their `.gz`. `--legacy`
makes the feed a capture from before `ts_epoch` (times from the `ts` strings),
its EBR twins converted by `ebr.py convert`: frames cut by size, spanning
hours, that no header places in time. `--keep` keeps the temp dir: the feed,
both caches and both services' logs.

Every variant above serves the twins as they are, with the startup conversion
off (RC_DASH_CONVERT=0): it is the JSONL reader against the EBR one.

`--convert` checks the startup conversion instead (`ingest.convert`,
rc_dashboard/ebrview.py): the demo writes a JSONL-only feed (`--demo-format
jsonl`), and it is served once with the conversion off, reading the JSONL, and
once with it on, reading nothing but the EBR view it converts at startup -- every
API diffed as above, a view file's path compared as its source's. `--restart`
then starts both again: the conversion resumes from the MARKs it wrote and must
leave every view file as it was, and change no answer; `--gzip` seals the days
before the newest (before the first start, or between the runs: the view finishes
each from its .gz). `--convert-feed both` serves a feed in both formats instead,
the conversion on (its streams linked, not converted) against `prefer=ebr` with it
off.

Exit status: 0 when every response is identical, 1 on any difference, 2 when
the run itself could not be made (a service that would not start, a timeout).

Named so `unittest discover` (`test*.py`) passes over it: it starts two services
and takes a minute or more. The standard library only, beside rc_dashboard's own
`ebr` for step 2.
"""
import argparse
import atexit
import fnmatch
import itertools
import json
import math
import os
import re
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

from rc_dashboard import ebr      # noqa: E402

FORMATS = ("jsonl", "ebr")
WINDOWS_FIXED = (60, 1440)
# The most --grow may add: a saved build older than its window's drift budget
# (panels.max_drift_pct, 25% of 60 min) is scanned again, not resumed.
GROW_MAX = 899

# What is ignored, and why: (path pattern, endpoints it applies to, reason).
# A path is the response's keys joined by "/", with every list index written
# "#"; patterns are fnmatch's, so "*" spans levels. Endpoints are the labels'
# first word ("panels", "feeds", ...), or "*" for all. Nothing that describes
# the records belongs here: every entry is true of the twin read, its bytes,
# or the clock the run took.
IGNORE = (
    # -- timings
    ("*elapsed_s", "*", "a duration: the pinned clock's (0) or a monotonic one"),
    ("*behind_wall_s", "*", "the wall clock less the pinned one: when the request ran"),
    ("_meta/generated_at", "export", "the export header's wall-clock stamp"),
    # -- byte counts and byte progress: the twin read is another size
    ("*progress/bytes", "*", "bytes a build has read, of the twin it reads"),
    ("*progress/total_bytes", "*", "bytes a build plans to read, of the twin it reads"),
    ("*backfill/bytes", "*", "bytes the backfill has read, of the twin it reads"),
    ("*backfill/total_bytes", "*", "bytes the backfill plans to read, of the twin it reads"),
    ("hosts/#/files/*/bytes", "collectors", "a host's bytes on disk: the twins' own"),
    ("scan/bytes", "trajectory", "bytes the inspector read, of the twin it reads"),
    ("scan/bytes_planned", "trajectory", "bytes the inspector planned, of the twin it reads"),
    ("bytes", "iocmds", "bytes the lookup read, of the twin it reads"),
    ("total_bytes", "iocmds", "bytes the lookup planned, of the twin it reads"),
    # -- the twin's own file, not its records
    ("hosts/#/files/*/last_write", "collectors", "when the newest twin read was last written"),
    ("_meta/feed_formats", "export", "the feeds report's account of the twins"),
) + tuple(
    (pat % tier, "feeds", why) for tier in ("ebpf", "ebpf_node") for pat, why in (
        ("%s/bytes", "the tier's bytes on disk: the twins' own"),
        ("%s/files/#/size", "a day-file's size on disk: the twin's own"),
        ("%s/files/#/mtime", "when the twin read was last written"),
        ("%s/newest_mtime", "when the newest twin read was last written"),
        # the .gz seek index (gz.status): the inflated twin's size, and so the
        # index's, and how long it took; how many files it indexed is compared
        ("%s/compressed/uncompressed_bytes", "the inflated size of the .gz twins read"),
        ("%s/compressed/index_bytes", "the seek index's memory: one point per span inflated"),
        ("%s/compressed/index_s", "how long indexing the .gz twins took"),
        # the feeds report's account of the twins
        ("%s/prefer", "the twin each service is told to read"),
        ("%s/formats", "the formats read and left unread"),
        ("%s/twins", "which twin of each stream is read"),
        ("%s/held", "the streams held for a twin, by format"),
        ("%s/files/#/format", "the format of the twin read"),
    )) + (
    # -- the scan counts of a read a filter narrows: a JSONL line is judged by a
    #    byte pattern before it is parsed, an EBR record by its fields before it
    #    is built, and the two pass different false hits on to be counted
    ("total_scanned", "events-filtered", "records a filtered query looked at"),
    ("scan/parsed", "trajectory", "records the inspector's key search looked at"),
    ("scan/skipped", "trajectory", "records the inspector's key search passed over"),
    ("scanned", "iocmds", "records a filtered lookup looked at"),
    # -- not the format's, the run's: a lookup is named by a uuid4
    ("id", "iocmds", "a lookup's id, drawn at random by each service"),
)


# With `--convert`, also: what says HOW the feed is read -- the EBR view or the
# day-files as they are -- and nothing about the records read.
CONVERT_IGNORE = tuple(
    (pat % tier, "feeds", why) for tier in ("ebpf", "ebpf_node") for pat, why in (
        ("%s/convert", "the startup conversion's own account: on in one service only"),
        ("%s/source_roots", "the collector's roots, beside the view's: one service only"),
        ("%s/origin", "who set the roots read: the conversion, or the environment"),
        ("%s/compressed", "a .gz is read as it is, or through its plain EBR copy"),
        ("%s/files/#/compressed", "a .gz is read as it is, or through its plain EBR copy"),
    ))


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
def write_feed(base, days, hosts, conf, fmt="both"):
    """`demo --demo-format <fmt> --demo-write-only` into `base`; the stamp."""
    env = clean_env()
    env["RC_DASH_CONFIG"] = conf
    cmd = [sys.executable, "-m", "rc_dashboard", "demo", "--demo-dir", base,
           "--demo-days", str(days), "--demo-hosts", str(hosts),
           "--demo-format", fmt, "--demo-write-only"]
    t = time.monotonic()
    got = subprocess.run(cmd, cwd=DASH, env=env, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT)
    if got.returncode != 0:
        raise Fatal("the demo could not write the feed:\n%s"
                    % got.stdout.decode("utf-8", "replace")[-4000:])
    with open(os.path.join(base, "demo.json")) as fh:
        stamp = json.load(fh)
    if stamp.get("format", "jsonl") != fmt or not isinstance(stamp.get("written_to"),
                                                              (int, float)):
        raise Fatal("the demo's stamp is not a %s feed's: %r" % (fmt, stamp))
    return stamp, time.monotonic() - t


def grow_feed(base, days, hosts, until, fmt="both"):
    """Have the demo write the feed on to `until`, as it resumes after a restart:
    the files are appended to (an EBR file's last segment continued,
    `ebr.Writer.open_append`), nothing is rewritten. Returns the new stamp."""
    import contextlib
    import io
    from rc_dashboard import demo as demomod
    ns = argparse.Namespace(demo_dir=base, demo_days=days, demo_hosts=hosts,
                            demo_seed=demomod.DEFAULTS["seed"], demo_format=fmt,
                            demo_fresh=False)
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


def same(a, b, path=""):
    """The first path where `a` and `b` differ, type- and key-order-exact (1, 1.0
    and true apart, -0.0 and 0.0 apart, NaN equal to NaN); None when they do not."""
    if type(a) is not type(b):
        return "%s: %s %r != %s %r" % (path or "/", type(a).__name__, a, type(b).__name__, b)
    if isinstance(a, dict):
        if list(a) != list(b):
            return "%s: keys %r != %r" % (path or "/", list(a), list(b))
        for k in a:
            d = same(a[k], b[k], "%s/%s" % (path, k))
            if d:
                return d
        return None
    if isinstance(a, list):
        if len(a) != len(b):
            return "%s: %d items != %d" % (path or "/", len(a), len(b))
        for i, (x, y) in enumerate(zip(a, b)):
            d = same(x, y, "%s[%d]" % (path, i))
            if d:
                return d
        return None
    if isinstance(a, float):
        if (a != a and b != b) or (a == b and math.copysign(1.0, a) == math.copysign(1.0, b)):
            return None
        return "%s: %r != %r" % (path or "/", a, b)
    return None if a == b else "%s: %r != %r" % (path or "/", a, b)


def legacy_feed(feed):
    """Make the feed a capture from before `ts_epoch`: every record keeps only its
    `ts` (or `timestamp`) string, read to the second (`normalize.epoch_of`). The
    JSONL twin is rewritten line by line (`json.dumps`, as the collector writes)
    and the EBR twin converted from it by `ebr.py convert`, whose frames, with no
    `ts_epoch` to cut a time grid by, are cut by size alone and carry no times in
    their headers: every seek places them by their records (`feedfile._probe_ebr`),
    and a frame spans hours. Returns `(files, frames, records)`."""
    tool = os.path.join(DASH, "rc_dashboard", "ebr.py")
    files = frames = records = 0
    for tier in ("ebpf", "node"):
        for dp, _dirs, names in sorted(os.walk(os.path.join(feed, tier))):
            for name in sorted(names):
                if not name.endswith(".jsonl"):
                    continue
                src = os.path.join(dp, name)
                stem = name[:-len(".jsonl")]
                tmp = os.path.join(dp, ".%s.jsonl.tmp" % stem)
                with open(src, "rb") as fi, open(tmp, "w") as fo:
                    for line in fi:
                        if line.strip():
                            rec = json.loads(line)
                            rec.pop("ts_epoch", None)
                            fo.write(json.dumps(rec) + "\n")
                os.replace(tmp, src)
                etmp = os.path.join(dp, ".%s.ebr.tmp" % stem)
                got = subprocess.run([sys.executable, tool, "convert", src, etmp],
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
                if got.returncode != 0:
                    raise Fatal("ebr.py convert %s: %s" % (src, got.stdout.decode()[-2000:]))
                os.replace(etmp, os.path.join(dp, stem + ".ebr"))
                for f in ebr.frames(os.path.join(dp, stem + ".ebr")):
                    if f.kind == ebr.DATA:
                        if f.ts_min - f.ts_min == 0.0:
                            raise Fatal("%s: a converted frame is placed by its header" % src)
                        frames += 1
                        records += f.n_records
                files += 1
    return files, frames, records


def seal_days(feed):
    """gzip every eBPF-tier day-file of every day before the newest, both twins,
    as the collector seals a day at rollover (`_compress_day`): `x.gz` written
    whole, then `x` removed. The node tier is never compressed. Returns the
    number of files sealed."""
    import gzip
    root = os.path.join(feed, "ebpf")
    days = sorted(d for d in os.listdir(root) if d.startswith("20"))
    n = 0
    for day in days[:-1]:
        d = os.path.join(root, day)
        for name in sorted(os.listdir(d)):
            if not name.endswith((".jsonl", ".ebr")):
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


def jsonl_feed_records(feed):
    """Every record of a JSONL-only feed, both tiers."""
    for tier in ("ebpf", "node"):
        for dp, _dirs, names in sorted(os.walk(os.path.join(feed, tier))):
            for name in sorted(names):
                if name.endswith((".jsonl", ".jsonl.gz")):
                    for r in jsonl_records(os.path.join(dp, name)):
                        yield r


def jsonl_records(path):
    import gzip
    with (gzip.open(path, "rb") if path.endswith(".gz") else open(path, "rb")) as fh:
        for line in fh:
            if line.strip():
                yield json.loads(line)


def check_twins(feed):
    """Every stream under the feed's two tiers is a twin pair, and the EBR twin
    decodes to exactly the JSONL twin's records, one by one, in order. Returns
    `(pairs, records, problems)`."""
    pairs = records = 0
    bad = []
    for tier in ("ebpf", "node"):
        root = os.path.join(feed, tier)
        for day in sorted(os.listdir(root)):
            d = os.path.join(root, day)
            by = {}
            for name in sorted(os.listdir(d)):
                m = re.match(r"^([^.].*)\.(jsonl|ebr)(\.gz)?$", name)
                if m:
                    slot = by.setdefault(m.group(1), {})
                    if m.group(2) in slot:
                        bad.append("%s/%s/%s: both %s and its .gz" % (
                            tier, day, m.group(1), m.group(2)))
                    slot[m.group(2)] = os.path.join(d, name)
            for stem, files in sorted(by.items()):
                where = "%s/%s/%s" % (tier, day, stem)
                if set(files) != set(FORMATS):
                    bad.append("%s: only %s" % (where, ", ".join(sorted(files))))
                    continue
                st = {}
                n = 0
                got = (rec for _pos, rec in ebr.iter_records(files["ebr"], stats=st))
                for n, (want, have) in enumerate(itertools.zip_longest(
                        jsonl_records(files["jsonl"]), got, fillvalue=StopIteration), 1):
                    d = same(have, want, "[%d]" % (n - 1))
                    if d:
                        bad.append("%s: EBR != JSONL at %s" % (where, d[:300]))
                        break
                errs = {k: v for k, v in st.items() if v and k in (
                    "bad_records", "lost_records", "bad_frames", "unsupported_frames")}
                if errs:
                    bad.append("%s: the EBR twin decodes with errors %r" % (where, errs))
                if not n:
                    bad.append("%s: the twins hold no record" % where)
                pairs += 1
                records += n
    if not pairs:
        bad.append("no twin pair under %s" % feed)
    return pairs, records, bad


# ---------------------------------------------------------------- a service
class Service:
    """`python -m rc_dashboard serve`, configured by RC_DASH_* alone."""

    def __init__(self, fmt, env, cache, logdir):
        self.fmt = fmt
        self.base_env = env
        self.cache = cache
        self.log_path = os.path.join(logdir, "serve-%s.log" % fmt)
        self.port = None
        self.proc = None
        self._log = None

    def start(self):
        self.port = free_port()
        env = dict(self.base_env, RC_DASH_PORT=str(self.port), RC_DASH_CACHE_DIR=self.cache)
        self._log = open(self.log_path, "ab")
        self._log.write(("\n==== start %s on :%d ====\n" % (self.fmt, self.port)).encode())
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
                self.fmt, None if self.proc is None else self.proc.returncode,
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
            raise Fatal("%s %s -> HTTP %d: %s" % (self.fmt, path, e.code, e.read()[:300]))

    def lines(self, path, timeout=600):
        """A streamed body, a line at a time."""
        try:
            r = urllib.request.urlopen(self.url(path), timeout=timeout)
        except urllib.error.HTTPError as e:
            raise Fatal("%s %s -> HTTP %d: %s" % (self.fmt, path, e.code, e.read()[:300]))
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
                            % (self.fmt, timeout, what, self.log_tail()))
            time.sleep(0.25)

    def view(self, minutes, flt=None):
        for w in self.json("/api/windows", 60).get("held") or []:
            if w.get("minutes") == minutes and (w.get("filter") or None) == (flt or None):
                return w
        return None


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


def wait_ready(services, views, timeout):
    """Every service's backfill done and every view in `views` built.

    `views`: (minutes, params). A view not held is asked for (`/api/panels`),
    which queues its build; one whose build failed fails the run."""
    for s in services:
        def backfilled(s=s):
            b = s.json("/api/health", 60).get("backfill") or {}
            if b.get("state") == "error":
                raise Fatal("%s service: backfill failed: %s" % (s.fmt, b.get("error")))
            return b.get("state") in ("done", "skipped")
        s.wait(backfilled, "the backfill", timeout)
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
                                % (s.fmt, label(minutes, params), v.get("error")))
                return v.get("state") == "ready"
            s.wait(ready, "the %s view" % label(minutes, params), timeout)


def label(minutes, params=None):
    return "%d min%s" % (minutes, (" " + query(params)) if params else "")


# ------------------------------------------------------------------- diffing
class Norm:
    """What makes two responses comparable without hiding a record: a day-file
    name's format suffix is written `.<fmt>` (`h.exits.jsonl` and `h.exits.ebr`
    are one stream's twins; a path, or a name in a sentence such as the `auto`
    clock's source), and either cache dir is `<cache>`."""

    SUFFIX = re.compile(r"(?<=[\w-])\.(?:jsonl|ebr)(?=(?:\.gz)?(?![\w.]))")

    SEALED = re.compile(r"\.<fmt>\.gz(?![\w.])")

    def __init__(self, caches):
        self.caches = sorted(caches, key=len, reverse=True)
        self.views = []         # (view root, its source root): `--convert`
        self.sealed = False     # `--convert`: a sealed x.jsonl.gz is converted to a plain x.ebr

    def text(self, s):
        if ".jsonl" in s or ".ebr" in s:
            s = self.SUFFIX.sub(".<fmt>", s)
            if self.sealed:
                s = self.SEALED.sub(".<fmt>", s)
        for v, src in self.views:
            if v in s:
                s = s.replace(v, src)
        for c in self.caches:
            if c in s:
                s = s.replace(c, "<cache>")
        return s


# How many differing values each IGNORE entry stood in for, over the run: what
# the summary prints, so an entry that hides more than it says is seen.
SUPPRESSED = {}


# The entries this run applies: IGNORE, and with --convert CONVERT_IGNORE too.
RULES = list(IGNORE)


def ignored(kind, path):
    """The IGNORE entry that covers `path` of a `kind` response, or None."""
    for rule in RULES:
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
    def __init__(self, max_diffs, labels=FORMATS):
        self.labels = labels
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
            log("      %-6s %s" % (self.labels[0] + ":", short(a)))
            log("      %-6s %s" % (self.labels[1] + ":", short(b)))


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
    compare_json(services, "/api/user?" + query({"name": user, "window_min": 1440}),
                 report, norm)
    # the live tier past what /api/live carries: every account, and one account's
    # newest exited processes (the read-back's order, ties and all)
    compare_json(services, "/api/live/users?" + query({"window_min": full, "limit": 5000}),
                 report, norm)
    compare_json(services, "/api/live/user?" + query({"name": user, "window_min": full,
                                                      "n": 1000}), report, norm)
    # A few sessions as process trees: the busiest agents and a terminal, both orders.
    tr = services[0].json("/api/trajectories?rank=events&window_min=1440")
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
        for order in ("oldest", "newest"):
            compare_json(services, "/api/trajectory?" + query(
                {"sid": sid, "window_min": 1440, "order": order}), report, norm)
    for path in ("/api/events?limit=500",
                 "/api/events?limit=500&order=asc",
                 "/api/events?limit=500&cursor=500",
                 "/api/events?limit=500&event=exit,truncated,residency,residency_totals,"
                 "conn,netio,submit,meta,stop",
                 "/api/events?" + query({"limit": 500, "user": user})):
        compare_json(services, path, report, norm)
    compare_stream(services, "/api/export?order=asc", report, norm)
    compare_stream(services, "/api/export", report, norm)
    iocmds(services, {"window_min": 1440, "user": user}, report, norm, timeout)
    compare_json(services, "/api/collectors", report, norm)
    compare_json(services, "/api/feeds", report, norm)


def twins_read(services):
    """Each service read every stream from its own twin, and only from it: else
    two identical answers could be one twin read twice."""
    bad, said = [], []
    for s in services:
        rep = s.json("/api/feeds")
        other = next(f for f in FORMATS if f != s.fmt)
        for tier in ("ebpf", "ebpf_node"):
            r = rep.get(tier) or {}
            fm = r.get("formats") or {}
            read, unread = fm.get("read") or {}, fm.get("unread") or {}
            off = [t for t in r.get("twins") or () if t.get("read") != s.fmt or t.get("reason")]
            if set(read) != {s.fmt} or set(unread) != {other} or read[s.fmt] != unread[other] \
                    or r.get("held") or r.get("format_notice") or off \
                    or len(r.get("twins") or ()) != read[s.fmt] or r.get("prefer") != s.fmt:
                bad.append("%s service, %s tier: prefer %s, read %r, unread %r, held %r, "
                           "notice %r, %d twin(s) read otherwise" % (
                               s.fmt, tier, r.get("prefer"), read, unread, r.get("held"),
                               r.get("format_notice"), len(off)))
            else:
                said.append("%s %s %d" % (s.fmt, tier, read[s.fmt]))
    return said, bad


def converted_read(services, linked):
    """The service with the conversion off read the day-files as they are; the
    one with it on read only its EBR view, every stream converted (a JSONL feed)
    or linked (`linked`: a feed in both formats), the conversion live and
    clean. Returns `(said, problems)`."""
    bad, said = [], []
    off, on = services
    for tier in ("ebpf", "ebpf_node"):
        a = off.json("/api/feeds").get(tier) or {}
        b = on.json("/api/feeds").get(tier) or {}
        c = b.get("convert") or {}
        n = sum((a.get("formats") or {}).get("read", {}).values())
        read = (b.get("formats") or {}).get("read") or {}
        counts = c.get("counts") or {}
        want = {"converted": 0, "linked": n, "held": 0} if linked else \
            {"converted": n, "linked": 0, "held": 0}
        files = [f for f in b.get("files") or () if not f.get("held")]
        if (a.get("convert") or {}).get("state") != "off" or c.get("state") != "live" \
                or read != {"ebr": n} or counts != want or c.get("errors") \
                or c.get("lag_bytes") or not n \
                or any(not f["path"].startswith(tuple(c.get("view_roots") or ["?"]))
                       for f in files):
            bad.append("%s: off read %r; on read %r through the view, convert %r" % (
                tier, (a.get("formats") or {}).get("read"), read,
                {k: c.get(k) for k in ("state", "counts", "errors", "lag_bytes")}))
        else:
            said.append("%s %d %s" % (tier, n, "linked" if linked else "converted"))
    return said, bad


def view_files(service):
    """`{path: (inode, size)}` of every file of the service's EBR view."""
    out = {}
    for tier in ("ebpf", "ebpf_node"):
        c = service.json("/api/feeds").get(tier, {}).get("convert") or {}
        for root in c.get("view_roots") or ():
            for dp, _dirs, names in os.walk(root):
                for n in names:
                    p = os.path.join(dp, n)
                    st = os.lstat(p)
                    out[p] = (st.st_ino, st.st_size, os.path.islink(p))
    return out


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
    """`(meta, state)` of a saved window build, hour state or saved panels."""
    import pickle
    import zlib
    with open(path, "rb") as fh:
        meta = pickle.load(fh)
        if os.sep + "windows" + os.sep in path:
            return meta, pickle.load(fh)
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


def compare_caches(caches, report, norm, labels=FORMATS):
    """The two services' saved states -- every window build, hour and day state
    and saved panels -- are equal by value. Each names the day-files it read and
    where it stopped (`files`), and is named by a fingerprint of a config that
    differs in `prefer`; nothing else may differ. A state the API does not show
    yet would show the next time it is merged or resumed."""
    by = []
    for f in labels:
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
            for x in ("fingerprint", "files"):
                m.pop(x, None)
        found += [("%s %s%s" % (k, part, p), x, y) for part, a, b in (
            ("meta", ma, mb), ("state", sa, sb))
            for p, x, y in diff(_plain(a), _plain(b), "state", norm)]
    report.add("saved states (%d file(s) each)" % len(by[0]), found)


def resumed(services, views, scan, grown=0):
    """After a restart: every view resumed from what the first run saved. A
    scanned window comes back from its saved build ("snapshot"), read on from
    where it stopped; with merge-reduce, a window with whole hours inside it is
    merged from the saved hour states ("hours"), and none of them is reduced
    again -- only an hour the feed's growth (`grown` seconds) closed."""
    bad = []
    new_hours = int(math.ceil(grown / 3600.0)) + 1 if grown else 0
    for s in services:
        st = s.json("/api/windows", 60)
        for minutes, params in views:
            v = s.view(minutes, flt_of(params))
            src = ((v or {}).get("cache") or {}).get("source")
            note = ((v or {}).get("cache") or {}).get("note")
            ok = src == "snapshot" or (src == "hours" and not scan)
            log("  %-5s %-5s %-34s cache.source=%s  %s" % (
                "ok" if ok else "FAIL", s.fmt, label(minutes, params), src, note or ""))
            if not ok:
                bad.append("%s %s: cache.source=%s (%s)" % (s.fmt, label(minutes, params),
                                                           src, note))
        hours = st.get("hours")
        if hours is not None:
            n = hours.get("hours_reduced") or 0
            log("  %-5s %-5s hour states reduced since the restart: %d (at most %d new)" % (
                "ok" if n <= new_hours else "FAIL", s.fmt, n, new_hours))
            if n > new_hours:
                bad.append("%s: %d hour state(s) reduced after the restart, where at most %d "
                           "closed since" % (s.fmt, n, new_hours))
    return bad


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Serve one demo feed from its JSONL and its EBR twins and diff every API.")
    ap.add_argument("--days", type=int, default=2, help="days of demo history (2; 1..14)")
    ap.add_argument("--hosts", type=int, default=2, help="demo login nodes (2; 1..6)")
    ap.add_argument("--scan", action="store_true",
                    help="hourly merge-reduce off (RC_DASH_MERGE_REDUCE=0)")
    ap.add_argument("--restart", action="store_true",
                    help="then restart both services, check the saved builds were "
                         "resumed, and diff again")
    ap.add_argument("--grow", type=int, default=0, metavar="SECONDS",
                    help="with --restart: have the demo write SECONDS more of the feed "
                         "before the restart (1..%d, inside the 60 min window's drift "
                         "budget), so the resumed builds read on; both clocks are then "
                         "pinned by `auto`" % (GROW_MAX,))
    ap.add_argument("--gzip", action="store_true",
                    help="seal every day before the newest as .gz, both twins, as the "
                         "collector does at rollover: before the first start, or with "
                         "--restart between the two runs (resumed from the .gz)")
    ap.add_argument("--legacy", action="store_true",
                    help="drop ts_epoch from every record (times from the `ts` strings) "
                         "and convert each JSONL twin to its EBR twin with ebr.py convert: "
                         "frames cut by size, with no times in their headers")
    ap.add_argument("--convert", action="store_true",
                    help="the startup conversion instead: a JSONL-only feed served with "
                         "it off and with it on (RC_DASH_CONVERT), every API diffed")
    ap.add_argument("--convert-feed", choices=("jsonl", "both"), default="jsonl",
                    help="with --convert: the feed's format (jsonl); both: the conversion "
                         "on (linking) against prefer=ebr with it off")
    ap.add_argument("--keep", action="store_true", help="keep the temp dir")
    ap.add_argument("--max-diffs", type=int, default=20,
                    help="differing paths printed (20)")
    ap.add_argument("--timeout", type=int, default=900,
                    help="seconds to wait for a backfill or a build (900)")
    a = ap.parse_args(argv)
    days = max(1, min(14, a.days))
    hosts = max(1, min(6, a.hosts))
    if a.grow and not (a.restart and 0 < a.grow <= GROW_MAX):
        ap.error("--grow takes --restart and 1..%d seconds" % GROW_MAX)
    if a.grow and a.legacy:
        ap.error("--grow continues the feed as the demo writes it, with ts_epoch: "
                 "not with --legacy")
    if a.convert and a.legacy:
        ap.error("--legacy makes the EBR twins of a both-format feed: not with --convert")

    tmp = tempfile.mkdtemp(prefix="rc-e2e-formats-")
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


def change_feed(a, base, feed, days, hosts, stamp, records, seal=False, grow=0,
                fmt="both"):
    """Seal the days before the newest (`seal_days`) and/or have the demo write
    `grow` seconds more (`grow_feed`), then check every twin pair again.
    Returns `(stamp, records)`, or None when a pair no longer decodes alike."""
    said = []
    if seal:
        said.append("sealed %d day-file(s) before the newest day as .gz" % seal_days(feed))
    if grow:
        stamp = grow_feed(base, days, hosts, stamp["written_to"] + grow, fmt)
        said.append("grown by %d s to %s" % (grow, time.strftime(
            "%Y-%m-%d %H:%M:%S", time.localtime(stamp["written_to"]))))
    if fmt != "both":
        n = sum(1 for _r in jsonl_feed_records(feed))
        log("feed %s: %d record(s)%s" % ("; ".join(said), n,
                                          " (%+d)" % (n - records) if grow else ""))
        return stamp, n
    pairs, n, bad = check_twins(feed)
    if bad:
        log("twins: %d of %d pair(s) do not decode alike once %s:" % (
            len(bad), pairs, " and ".join(said)))
        for line in bad[:a.max_diffs]:
            log("  " + line)
        return None
    log("feed %s: %d record(s)%s, every twin pair still alike" % (
        "; ".join(said), n, " (%+d)" % (n - records) if grow else ""))
    return stamp, n


def run(a, tmp, days, hosts, services):
    t0 = time.monotonic()
    code = code_digest()
    # --convert: the feed the demo writes, and the two services' names
    fmt = a.convert_feed if a.convert else "both"
    labels = ("off", "on") if a.convert else FORMATS
    if a.convert:
        RULES.extend(CONVERT_IGNORE)

    def same_code():
        if code_digest() != code:
            raise Fatal("rc_dashboard's code changed on disk during the run, so its two "
                        "phases ran different code; run it again")
    base = os.path.join(tmp, "demo")
    conf = os.path.join(tmp, "config.json")
    with open(conf, "w") as fh:
        fh.write("{}\n")            # no dashboard.config.json of this checkout's
    log("e2e formats: %d day(s), %d host(s), %s, %s, in %s" % (
        days, hosts, "one scan per window (merge-reduce off)" if a.scan
        else "hourly merge-reduce",
        ("the startup conversion off and on, over a %s feed" % fmt) if a.convert
        else "the JSONL twins against the EBR ones, no conversion", tmp))

    stamp, took = write_feed(base, days, hosts, conf, fmt)
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
        n, frames, recs = legacy_feed(feed)
        log("feed made a legacy capture: ts_epoch dropped from every record, %d EBR "
            "twin(s) converted by ebr.py convert into %d frame(s) (%.0f records each) "
            "no header places in time" % (n, frames, recs / max(1, frames)))
    if fmt == "both":
        pairs, records, bad = check_twins(feed)
        if bad:
            log("twins: %d of %d pair(s) do not decode alike:" % (len(bad), pairs))
            for line in bad[:a.max_diffs]:
                log("  " + line)
            return 1
        log("twins: %d pair(s), %d record(s), each EBR twin decodes to its JSONL twin "
            "exactly" % (pairs, records))
    else:
        records = sum(1 for _r in jsonl_feed_records(feed))
        log("feed: %d record(s), JSONL only" % records)
    if a.gzip and not a.restart:
        got = change_feed(a, base, feed, days, hosts, stamp, records, seal=True, fmt=fmt)
        if got is None:
            return 1
        stamp, records = got

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
        # names come from the records' own host names, as in a demo: a lookup's
        # answer depends on when it landed, which no format decides
        "RC_DASH_RDNS": "0",
        "RC_DASH_SACCT_DIR": none,
        "RC_DASH_SACCTMGR_DIR": none,
        "RC_DASH_DOMAINS_CSV": os.path.join(none, "map_user.csv"),
        "RC_DASH_USER_CSV": os.path.join(none, "user.csv"),
        "RC_DASH_STATIC_DIR": none,
        # set iteration order is drawn per process; fixed, two services that
        # build the same state serve it in the same order
        "PYTHONHASHSEED": "0",
        # the day-files read as they are, unless the conversion is what is checked
        "RC_DASH_CONVERT": "0",
    })
    caches = {f: os.path.join(tmp, "cache-%s" % f) for f in labels}
    if a.convert:
        # off: the day-files as they are (JSONL, or prefer=ebr over twins); on:
        # only the EBR view, converted (or linked) at startup into its cache
        off = "ebr" if fmt == "both" else "jsonl"
        services.append(Service("off", dict(env, RC_DASH_EBPF_PREFER=off,
                                            RC_DASH_NODE_PREFER=off), caches["off"], tmp))
        services.append(Service("on", dict(env, RC_DASH_CONVERT="1"), caches["on"], tmp))
    else:
        for f in FORMATS:
            services.append(Service(f, dict(env, RC_DASH_EBPF_PREFER=f, RC_DASH_NODE_PREFER=f),
                                    caches[f], tmp))
    norm = Norm(caches.values())
    # the view's copy of a sealed day is a plain file: one stream, either way
    norm.sealed = a.convert and fmt == "jsonl"

    failed = False
    before = None
    for phase in (("first start", "restart") if a.restart else ("first start",)):
        log("")
        log("== %s ==" % phase)
        if phase == "restart" and (a.grow or a.gzip):
            got = change_feed(a, base, feed, days, hosts, stamp, records, seal=a.gzip,
                              grow=a.grow, fmt=fmt)
            if got is None:
                return 1
            stamp, records = got
        t = time.monotonic()
        same_code()
        for s in services:
            s.start()
        wait_ready(services, [(m, {}) for m in WINDOWS_FIXED + (full,)], a.timeout)
        user = busiest_user(services[0])
        views = views_of(user, full)
        wait_ready(services, views, a.timeout)
        log("ready in %.1f s: backfill done, views %s" % (
            time.monotonic() - t, ", ".join(label(m, p) for m, p in views)))
        if a.convert:
            failed = check_converted(services, fmt, phase, a, before, norm) or failed
        else:
            said, bad = twins_read(services)
            if bad:
                failed = True
                log("NOT each service its own twin: " + "; ".join(bad))
            else:
                log("twins read: each service only its own, of every stream (%s)"
                    % ", ".join(said))
        if phase == "restart":
            bad = resumed(services, views, a.scan, a.grow)
            if bad:
                failed = True
                log("not resumed: " + "; ".join(bad))
        report = Report(a.max_diffs, labels)
        compare_all(services, user, full, report, norm, a.timeout)
        if a.convert:
            before = view_files(services[1])
        stop_all(services)
        same_code()
        compare_caches(caches, report, norm, labels)
        report.print(phase)
        failed = failed or bool(report.total)
    log("")
    log("ignored as format-dependent by construction (IGNORE), values that differed:")
    for rule in RULES:
        if SUPPRESSED.get(rule):
            log("  %6d  %-28s %-16s %s" % (SUPPRESSED[rule], rule[0], rule[1], rule[2]))
    log("  (%d other entr%s stood in for no difference this run)" % (
        sum(1 for r in RULES if not SUPPRESSED.get(r)),
        "y" if sum(1 for r in RULES if not SUPPRESSED.get(r)) == 1 else "ies"))
    log("")
    log("%s in %.0f s" % ("DIFFERENT" if failed else "IDENTICAL", time.monotonic() - t0))
    return 1 if failed else 0


def check_converted(services, fmt, phase, a, before, norm):
    """--convert: what each service read (`converted_read`), the view's paths
    made comparable as their sources' (`Norm.views`), and after a restart, that
    the conversion resumed from its MARKs: nothing converted again, and every
    view file the same file it was -- appended to only when a day was sealed
    between the runs (`--gzip`). Returns whether anything is wrong."""
    wrong = False
    said, bad = converted_read(services, linked=fmt == "both")
    if bad:
        wrong = True
        log("NOT read as it should be: " + "; ".join(bad))
    else:
        log("read: the service with the conversion off reads the day-files as they are; "
            "with it on, only its EBR view (%s)" % ", ".join(said))
    feeds = services[1].json("/api/feeds")
    norm.views = []
    for tier in ("ebpf", "ebpf_node"):
        c = feeds.get(tier, {}).get("convert") or {}
        norm.views += list(zip(c.get("view_roots") or (), c.get("source_roots") or ()))
        cu = c.get("catch_up") or {}
        if tier == "ebpf":
            log("conversion: %d file(s) converted at startup, %s record(s) in %.2f s with "
                "%d worker(s)%s; view %.1f MB of %.1f MB of JSONL" % (
                    cu.get("files") or 0, cu.get("records") or 0, cu.get("elapsed_s") or 0,
                    cu.get("workers") or 0,
                    (", %d records/s" % cu["records_per_s"]) if cu.get("records_per_s")
                    else "", sum((feeds[t].get("convert") or {}).get("bytes_out") or 0
                                 for t in ("ebpf", "ebpf_node")) / 1e6,
                    sum((feeds[t].get("convert") or {}).get("bytes_in") or 0
                        for t in ("ebpf", "ebpf_node")) / 1e6))
    norm.views.sort(key=lambda vs: len(vs[0]), reverse=True)
    if phase == "restart" and before is not None:
        now = view_files(services[1])
        cu = (feeds.get("ebpf", {}).get("convert") or {}).get("catch_up") or {}
        moved = sorted(p for p in set(before) | set(now)
                       if before.get(p, (None,))[0] != now.get(p, (None,))[0]
                       or now.get(p, (0, 0))[1] < before.get(p, (0, 0))[1])
        grown = sorted(p for p in now if p in before and now[p][1] > before[p][1])
        if moved or (grown and not a.gzip) or (cu.get("records") and not a.gzip):
            wrong = True
            log("NOT resumed from the MARKs: %d view file(s) replaced or gone (%s), %d "
                "appended to, %s record(s) converted again" % (
                    len(moved), ", ".join(os.path.basename(p) for p in moved[:4]),
                    len(grown), cu.get("records")))
        else:
            log("resumed from the MARKs: every view file the same file%s; %s record(s) "
                "converted at the restart" % (
                    (", %d sealed and appended to" % len(grown)) if grown else
                    ", untouched", cu.get("records") or 0))
    return wrong


if __name__ == "__main__":
    sys.exit(main())
