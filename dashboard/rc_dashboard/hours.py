"""Hourly merge-reduce: every record is reduced once, and a window is a merge.

A window build used to re-read its whole window: a 24 h view of one login node
is ~250 000 records and ~12 s, a 7 d view a minute or more, and every window,
every drift re-scan and every restart after a code change paid it again for
the same records. Here each record is parsed and reduced ONCE, into the state
of the epoch hour it belongs to, and saved. A window is then

    an exact EDGE   the records from the window's start to the first whole
                    hour, read as before -- the start stays where it was asked
                    for, not snapped to an hour
  + the HOURS       every closed hour inside it, merged from their saved states
                    (whole days from a saved merge of their 24 hours)
  + the TAIL        the records of the hour still open, read as before
  + the live feed   as it always was (`windows.PanelView.feed`)

and a window switch, a drift refresh or a restart costs the edge and the tail
plus one merge per saved state, not a re-read of the window.

**Merging.** Every window reducer has `merge(other)`, which folds a LATER
state into its own exactly as feeding it the later records would have:
counts and sums add, sets union, first-seen fields keep the earlier value, the
"first N instances" lists keep the earlier ones, the "heaviest N" lists are
re-cut by their own rule, keyed maps merge key by key, and run-length chains
join at the seam. The quantile panels are the one place a merge is not
bit-exact against a single pass: they are a sketch (`reducers.Quant`) whose
merge IS exact, read to within 1%.

**Carrying.** Some state is a join between records rather than a count: the
dedup index a shell looks its child up in, the process index a child asks its
parent through, the sandbox wrapper that absorbs the payload it waited on.
Those joins cross hours, so the hours are reduced IN ORDER, each seeded with
the join state the previous hour ended with (`carry`), and each saves only
what its own records produced (`strip`). A window merged from them gets the
join state of its last hour (`install`), so its tail and its live feed join as
one pass would. The carried state is aged: what was last touched more than
`CARRY_KEEP_H` ago is dropped, so a service that runs for a month carries
about a day of it, not a month.

**When an hour closes.** Once the newest record seen is past its end by
`panels.hour_grace_s` -- the collector writes a poll round at a time and hosts'
clocks disagree, so not at the stroke of the hour. A record that arrives for
an hour already closed (a host that fell behind, a file appended late) is not
lost: the next hour's read finds it past where the previous hour stopped
reading that file, and it is reduced on its own and merged into its hour's
saved state, which says so (`stragglers`). Its joins with the rest of that
hour are the one thing such a record loses.

**Saved.** Under `<cache.dir>/hours/`, one file per hour (`h<epoch hour>`),
per merged day (`d<epoch day>`), and per hour a run may start after, the join
state that hour ends with (its chain, `c<epoch hour>`). Every hour reduced
saves its chain first, then itself, so a build stopped after any hour resumes
there; a chain is dropped once no run can start from it. Each is named
by a fingerprint of the code and config that decide what a reduction means --
a state reduced by other code is never merged, and is pruned (all but the
newest other generation, which another process may share the directory with). A pickle header
then the zlib-compressed state, written atomically, 0600 in a 0700 directory,
and never loaded unless private to this uid, as `snapshots` does it. Hours
older than retention are pruned.

Nothing here runs in parallel: the hours are reduced one at a time on the
registry's single build thread, which yields between hours
(`panels.hour_pause_s`), so a deployment with one core keeps its live tier.
"""
import collections
import glob
import hashlib
import heapq
import json
import math
import os
import pickle
import sys
import time
import zlib
from operator import itemgetter

from . import fastjson, gz, snapshots
from .export import (HEAD_BYTES, SCAN_SLACK_S, day_files, event_filter, head_digest,
                     seek_to_epoch)
from .normalize import Normalizer

FORMAT = 1
HOUR = 3600
# Every window's bin width is a whole number of these, so an hour's series
# folds into any window's exactly (resources.ResourceReducer.merge).
HOUR_BIN_S = 60
# Join state older than this is not carried into the next hour. A join waits
# on a parent's exit, and a tool call's parent exits within seconds of it;
# a day is the window most views are, and keeps the carried state a day's.
CARRY_KEEP_H = 24
# A gap this small between the newest reduced hour and the next one needed is
# filled in, so the carried join state reaches it; a wider one starts afresh.
MAX_GAP_H = 24
# Records found for an hour already closed, held per reduction for its patch.
STRAGGLER_CAP = 100000

PKG = os.path.dirname(os.path.abspath(__file__))
# Everything that decides what an hour's state means. `live.py` is not here:
# the live tier is not reduced into hours, and a change to it must not discard
# a week of them -- so no reducer an hour holds may import it (the cascades
# take `agent_version` from roles.py). Nor clock.py, which says which hours are
# closed, or feeds.py, which finds the day-files. test_window_cache checks that
# every module an hour imports is one or the other.
CODE_FILES = ("normalize.py", "export.py", "redact.py", "procindex.py", "roles.py",
              "tool_buckets.py", "proctree.py", "fastjson.py", "snapshots.py", "hours.py",
              "gz.py")
# Config that changes nothing a reduction holds: the server, the window
# registry's own knobs, the live tier's views and backfill, the clock, the
# masking of names on the way out (anonymize.py), the questions asked of a
# built view (ask.py), and the other feeds and the paths (the eBPF roots are
# fingerprinted as resolved) -- so a checkout moved, a node-tier path changed,
# anonymization switched, or another agent set to answer, keeps its hours.
CONFIG_IGNORED = ("server", "panels", "ingest", "rebuild", "export", "clock", "anonymize", "ui",
                  "ask")
LIVE_IGNORED = ("window_min", "window_presets_min", "event_tail", "submit_tail",
                "backfill_hours")


def code_digest():
    h = hashlib.sha256(b"hours %d python %d.%d\n" % ((FORMAT,) + sys.version_info[:2]))
    paths = [os.path.join(PKG, f) for f in CODE_FILES]
    paths += sorted(p for p in glob.glob(os.path.join(PKG, "reducers", "*.py"))
                    if os.path.basename(p) != "live.py")
    for name in snapshots.HELPER_MODULES:
        f = getattr(sys.modules.get(name), "__file__", None)
        if f:
            paths.append(f)
    for p in paths:
        h.update(os.path.basename(p).encode() + b"\0")
        try:
            with open(p, "rb") as fh:
                h.update(fh.read())
        except OSError:
            h.update(b"(missing)")
    return h.hexdigest()


def fingerprint(code, cfg, roots):
    tree = {k: v for k, v in cfg.as_dict().items() if k not in CONFIG_IGNORED}
    tree["live"] = {k: v for k, v in (tree.get("live") or {}).items()
                    if k not in LIVE_IGNORED}
    tree["feeds"] = {"ebpf": {k: v for k, v in ((tree.get("feeds") or {}).get("ebpf")
                                                or {}).items() if k != "roots"}}
    tree["cache"] = {k: v for k, v in (tree.get("cache") or {}).items()
                     if k not in ("dir", "state_dir")}
    blob = json.dumps([code, tree, list(roots)], sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


# ------------------------------------------------------------ the reducer set
def prepare(red):
    """Make a fresh reducer set an hour's: series at HOUR_BIN_S, and the
    process index noting what each hour touched (procindex.ProcIndex.track)."""
    for r in red.values():
        if hasattr(r, "bin_s"):
            r.bin_s = HOUR_BIN_S
        if hasattr(r, "track"):
            r.track()
    return red


def seed(red, carry):
    for k, c in (carry or {}).items():
        if c is not None and k in red:
            red[k].seed(c)


def take_carry(red):
    return {k: r.carry() for k, r in red.items() if hasattr(r, "carry")}


def strip(red):
    for r in red.values():
        if hasattr(r, "strip"):
            r.strip()


def mergeable(red):
    """The reducers that cannot be merged; empty when the set can be."""
    return sorted(k for k, r in red.items() if not callable(getattr(r, "merge", None)))


def merge_into(acc, part):
    """Fold the LATER reducer set `part` into `acc`, key by key. Shared objects
    (the process index the session reducers hold, the trajectory and workspace
    reducers the cascades read) are each merged once, as their own key, and
    `acc`'s links between them are untouched. `part` is consumed."""
    for k, r in acc.items():
        r.merge(part[k])


def install(acc, carry):
    """Give a merged set the join state its last hour ended with."""
    for k, c in (carry or {}).items():
        r = acc.get(k)
        if r is None or c is None:
            continue
        if hasattr(r, "install"):
            r.install(c)
        else:
            r.seed(c)


def age(red_cls, carry, ages, hour, keep_h=None):
    """Drop carried join state not touched for `keep_h` hours.

    `ages[(reducer, field)]` maps each key to the last hour it was new or
    touched in; it travels with the carry, so the ageing survives a restart.
    """
    cutoff = hour - (CARRY_KEEP_H if keep_h is None else keep_h)
    for k, c in carry.items():
        if not c:
            continue
        touched = c.pop("touched", None) or {}
        for field in getattr(red_cls.get(k), "CARRY_AGED", ()):
            d = c.get(field)
            if not isinstance(d, dict):
                continue
            a = ages.setdefault("%s.%s" % (k, field), {})
            for key in d:
                if key not in a:
                    a[key] = hour
            for key in touched.get(field, ()):
                if key in d:
                    a[key] = hour
            if len(a) > len(d):
                for key in [key for key in a if key not in d]:
                    del a[key]
            for key in [key for key, h in a.items() if h < cutoff]:
                del a[key]
                d.pop(key, None)
        done = getattr(red_cls.get(k), "carry_done", None)
        if done is not None:
            done(c)
    return carry, ages


# ------------------------------------------------------------------ reading
def plan_span(files, lo, prev=None):
    """Per file, where a read of records from `lo` on starts.

    `SCAN_SLACK_S` before `lo`, since a day-file is only in time order to
    within a poll round; and never later than where the previous hour's read
    of the same file stopped (`prev`), so every byte of a file is read by some
    hour -- the records a host wrote late are found, not skipped over.

    A day the collector compressed since that read (`x.jsonl` is now
    `x.jsonl.gz`) is the same bytes under a new name and inode, at the same
    uncompressed offsets: it is matched to the read of its plain name by its
    head alone. Unmatched, the slack before `lo` -- records the previous hour
    already took -- would be taken again as late arrivals for it.
    """
    out = []
    for p in files:
        try:
            with gz.open_feed(p) as fh:
                st = gz.fstat(fh)
                size = st.st_size
                n = min(HEAD_BYTES, size)
                e = {"path": p, "dev": st.st_dev, "ino": st.st_ino, "head_n": n,
                     "head": head_digest(fh, n), "size": size}
                start = seek_to_epoch(fh, lo - SCAN_SLACK_S, size)
        except OSError:
            continue
        pv, same = (prev or {}).get(p), ("dev", "ino", "head_n", "head")
        if pv is None and gz.is_gz(p):
            pv, same = (prev or {}).get(gz.plain_name(p)), ("head_n", "head")
        if pv and all(pv[k] == e[k] for k in same) and pv["stop"] <= size:
            e["prev_stop"] = pv["stop"]
            start = min(start, pv["stop"])
        e["start"] = e["stop"] = start
        out.append(e)
    return out


def _stream(e, norm, lo, hi, wanted, stats, strays):
    """One file's records with `lo <= ts < hi`, as `(ts, rec)`, in file order.

    Stops at the first record more than SCAN_SLACK_S past `hi` and records in
    `e["stop"]` where: the next hour's read starts there at the latest. A record
    from before `lo` that the previous hour's read never reached is a
    straggler, and goes to `strays` when given.
    """
    if e["start"] >= e["size"]:
        return
    try:
        fh = gz.open_feed(e["path"])
    except OSError:
        return
    prev_stop = e.get("prev_stop")
    with fh:
        pos = e["start"]
        fh.seek(pos)
        for line in fh:
            if not line.endswith(b"\n"):
                break                       # still being written: the next read's
            off = pos
            pos += len(line)
            stats["bytes"] += len(line)
            if wanted is not None and wanted(line) is None:
                continue
            try:
                raw = fastjson.loads(line)
            except ValueError:
                stats["parse_errors"] += 1
                continue
            rec = norm.normalize(raw)
            if rec is None:
                continue
            ts = rec.get("_ts_epoch")
            if ts is None:
                stats["no_ts"] += 1
                continue
            if hi is not None and ts >= hi:
                if ts > hi + SCAN_SLACK_S:
                    e["stop"] = off
                    return
                continue
            if ts >= lo:
                stats["records"] += 1
                yield ts, rec
            elif strays is not None and (prev_stop is None or off >= prev_stop):
                if len(strays) < STRAGGLER_CAP:
                    strays.append((ts, rec, e["path"], off))
                else:
                    stats["strays_dropped"] += 1
        e["stop"] = pos


def read_span(files, norm, lo, hi, events, prev=None, strays=None, stats=None):
    """Every record with `lo <= ts < hi` (`hi` None: to the end), oldest first,
    k-way merged across files as `export.window_scan` merges them. Returns the
    stream and the per-file plan, whose `stop`s are final once it is spent."""
    stats = stats if stats is not None else new_stats()
    plan = plan_span(files, lo, prev)
    wanted = event_filter(events)
    streams = [_stream(e, norm, lo, hi, wanted, stats, strays) for e in plan]
    return heapq.merge(*streams, key=itemgetter(0)), plan, stats


def new_stats():
    return {"records": 0, "bytes": 0, "parse_errors": 0, "no_ts": 0,
            "strays_dropped": 0}


def feed_all(red, stream):
    """Dispatch a stream into a reducer set; one bad record never stops it."""
    dispatch = {}
    for r in red.values():
        for ev in getattr(r, "EVENTS", ()):
            dispatch.setdefault(ev, []).append(r)
    newest = None
    n = 0
    for ts, rec in stream:
        for r in dispatch.get(rec.get("event"), ()):
            try:
                r.feed(rec)
            except Exception:
                pass
        if newest is None or ts > newest:
            newest = ts
        n += 1
    return n, newest


# -------------------------------------------------------------------- store
class HourStore:
    """Saved hour and day states for one feed, reduced in order on demand.

    Used from the registry's build thread only; `saw` is the one call from
    the ingest thread, and it only raises a number.
    """

    def __init__(self, cfg, classes, fresh, events, directory):
        self.cfg = cfg
        self.classes = list(classes)
        self.fresh = fresh              # () -> a window's reducer set
        self.events = events
        self.dir = directory
        self.code = code_digest()
        skew = int(cfg.get("live.clock_skew_s", 120) or 0)
        # Never less than the clock skew the live tier allows plus a poll
        # round's disorder: an hour closed sooner would turn on-time records
        # into stragglers.
        self.grace = max(int(cfg.get("panels.hour_grace_s", 300) or 0),
                         skew + SCAN_SLACK_S)
        self.day_cache = bool(cfg.get("panels.day_cache", True))
        self.pause = float(cfg.get("panels.hour_pause_s", 0.02) or 0)
        self.retention_h = int(math.ceil(max(5, int(cfg.get("live.retention_min", 10080)))
                                          / 60.0))
        self.newest_seen = None
        self.fp = None
        self.roots = None
        self.have = set()               # hours saved under the current fingerprint
        self.days = set()
        self.chains = set()             # hours whose chain is saved
        self.chain = None               # (hour, carry, ages): the newest chain
        self.stats = {"hours_reduced": 0, "records": 0, "bytes": 0, "stragglers": 0,
                      "stragglers_lost": 0, "patched_hours": 0, "reduce_s": 0.0,
                      "merged_states": 0, "days_built": 0, "pruned": 0, "chains_saved": 0,
                      "chain_bytes": 0}
        self._cls = {k: type(r) for k, r in fresh().items()}

    @classmethod
    def from_config(cls, cfg, classes, fresh, events):
        """None unless `panels.merge_reduce` is on and there is a cache dir."""
        if not cfg.get("panels.merge_reduce", True):
            return None
        base = cfg.get("cache.dir")
        if not base:
            return None
        return cls(cfg, classes, fresh, events, os.path.join(base, "hours"))

    # --------------------------------------------------------------- clock
    def saw(self, ts):
        if ts is not None and (self.newest_seen is None or ts > self.newest_seen):
            self.newest_seen = ts

    def closed_end(self, now, roots):
        """The first hour NOT closed: every hour before it is."""
        if self.newest_seen is None or self.newest_seen < now - self.grace:
            # Not yet fed, or quiet: ask the files. The live tail keeps this
            # current while it runs, so this is a start-up and an idle-feed read.
            from .clock import newest_record
            self.saw(newest_record(roots)[0])
        horizon = now if self.newest_seen is None else min(now, self.newest_seen)
        return int((horizon - self.grace) // HOUR)

    # ---------------------------------------------------------------- files
    def _sync(self, roots):
        """Point the store at these roots; on a new fingerprint, re-list."""
        fp = fingerprint(self.code, self.cfg, roots)
        if fp == self.fp:
            return
        self.fp, self.roots = fp, list(roots)
        self.have, self.days, self.chains, self.chain = set(), set(), set(), None
        try:
            names = os.listdir(self.dir)
        except OSError:
            names = []
        for n in names:
            stem, _, rest = n.partition(".")
            if rest != fp + ".hr":
                continue
            try:
                idx = int(stem[1:])
            except ValueError:
                continue
            if stem[0] == "h":
                self.have.add(idx)
            elif stem[0] == "d":
                self.days.add(idx)
            elif stem[0] == "c":
                self.chains.add(idx)
        self.chain = self._newest_chain()
        self.prune()

    def _path(self, kind, idx):
        return os.path.join(self.dir, "%s%d.%s.hr" % (kind, idx, self.fp))

    def _write(self, path, meta, blob):
        """`blob` is a pickle: a reducer set's comes from `snapshots.capture`."""
        os.makedirs(self.dir, mode=0o700, exist_ok=True)
        meta = dict(meta, format=FORMAT, fingerprint=self.fp, saved_at=time.time())
        # Level 3: within a tenth of level 6's size at well over twice its speed.
        body = zlib.compress(blob, 3)
        tmp = "%s.%d.tmp" % (path, os.getpid())
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "wb") as fh:
                pickle.dump(meta, fh, protocol=pickle.HIGHEST_PROTOCOL)
                fh.write(body)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return len(body)

    def _read(self, path, body=True):
        """`(meta, raw body)` of a private, current file, or `(None, None)`."""
        try:
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except OSError:
            return None, None
        with os.fdopen(fd, "rb") as fh:
            st = os.fstat(fh.fileno())
            if st.st_uid != os.getuid() or st.st_mode & 0o022:
                return None, None
            meta = pickle.load(fh)
            if meta.get("format") != FORMAT or meta.get("fingerprint") != self.fp:
                return None, None
            return meta, (zlib.decompress(fh.read()) if body else None)

    def meta(self, hour):
        try:
            return self._read(self._path("h", hour), body=False)[0]
        except Exception:
            return None

    def _drop(self, kind, idx):
        try:
            os.unlink(self._path(kind, idx))
        except OSError:
            pass
        {"h": self.have, "d": self.days, "c": self.chains}[kind].discard(idx)

    def load(self, kind, idx):
        """A saved hour (`h`) or day (`d`) as a live reducer set, or None."""
        try:
            meta, body = self._read(self._path(kind, idx))
            if meta is None:
                return None
            red = snapshots.restore(self.fresh(), pickle.loads(body))
        except Exception:
            self._drop(kind, idx)          # unreadable: reduce it again
            return None
        for r in red.values():
            if hasattr(r, "bin_s"):
                r.bin_s = meta.get("bin_s", HOUR_BIN_S)
        return red

    def _save_hour(self, hour, red, meta):
        # the day it is part of no longer matches it
        self._drop("d", hour // 24)
        size = self._write(self._path("h", hour), dict(meta, kind="hour", hour=hour,
                                                       bin_s=HOUR_BIN_S),
                           snapshots.capture(red))
        self.have.add(hour)
        return size

    def _save_chain(self, hour, carry, ages):
        """Save the join state hour `hour` ends with, which the hour after it starts from."""
        size = self._write(self._path("c", hour), {"kind": "carry", "hour": hour},
                           pickle.dumps((hour, carry, ages), protocol=pickle.HIGHEST_PROTOCOL))
        self.chains.add(hour)
        self.stats["chains_saved"] += 1
        self.stats["chain_bytes"] += size
        return size

    def _load_chain(self, hour):
        """Hour `hour`'s saved chain, `(hour, carry, ages)`, or None. One that cannot be read is
        dropped, and a run starts as if it had none."""
        if hour not in self.chains or hour not in self.have:
            return None
        try:
            meta, body = self._read(self._path("c", hour))
            if meta is None or meta.get("kind") != "carry":
                raise ValueError("not a chain")
            got = pickle.loads(body)
            if got[0] != hour:
                raise ValueError("another hour's chain")
            return got
        except Exception:
            self._drop("c", hour)
            return None

    def _newest_chain(self):
        """The newest saved hour's chain that can be read, or None."""
        for h in sorted(self.chains & self.have, reverse=True):
            got = self._load_chain(h)
            if got is not None:
                return got
        return None

    def _stale(self, h):
        """Whether saved hour `h` started afresh though the hour before it now has a chain: a run
        before it ended there after `h` was saved (a narrower window's hours, then a wider
        window), and stopped before it reduced `h` again with its join state."""
        if h not in self.have or h - 1 not in self.chains or h - 1 not in self.have:
            return False
        m = self.meta(h)
        return m is not None and not m.get("carried")

    def _prune_chains(self):
        """Keep the chains a run can start from: the newest, and each whose next hour is not
        saved, or is saved afresh and waits to be reduced again from it (`_stale`)."""
        newest = None if self.chain is None else self.chain[0]
        for h in sorted(self.chains):
            if h == newest or (h in self.have and (h + 1 not in self.have or self._stale(h + 1))):
                continue
            self._drop("c", h)

    # --------------------------------------------------------------- reduce
    def _hour_set(self):
        return prepare(self.fresh())

    def ensure(self, roots, h_lo, h_hi, progress=None):
        """Reduce whichever of hours `[h_lo, h_hi)` are not saved, in order.

        A run of missing hours carries on from the chain of the hour just
        before it, when that hour saved one; else it continues the newest
        chain when that is within MAX_GAP_H before it, filling the gap; else
        it starts afresh. If a saved hour that started afresh follows it,
        that hour is reduced again with the run's join state, so the seam
        between the two holds no unjoined records; and a run stopped before
        it did so leaves that hour `_stale`, reduced again here.

        Every hour a run saves also saves its chain (`_reduce_run`), so a
        build stopped after any hour resumes there and reduces nothing
        twice (R67): starting cold after it, it would drop every join across
        that seam, and a call seen through its children would read "still
        running". It costs about 0.16 s an hour on boslogin06 (a 2 MB
        chain), stopped or not. Hours saved with no chain (one that could
        not be written or read) still resume where their run began, afresh
        as it did, and are reduced again (`_began`).

        Still open (pre-existing): the saved hours after that reduced-again
        hour were carried on from its afresh state, and are not reduced
        again.
        Returns the number of hours reduced.
        """
        self._sync(roots)
        need = {h for h in range(h_lo, h_hi) if h not in self.have}
        need.update(h + 1 for h in self.chains if h_lo <= h + 1 < h_hi and self._stale(h + 1))
        need = sorted(need)
        if not need:
            return 0
        runs, start = [], need[0]
        for a, b in zip(need, need[1:] + [None]):
            if b != a + 1:
                runs.append((start, a + 1))
                start = b
        total = sum(b - a for a, b in runs)
        done = [0]

        def tick(**kw):
            done[0] += 1
            if progress:
                progress(done[0], total, **kw)

        files = self._files(roots, runs[0][0])
        n = 0
        try:
            for a, b in runs:
                chain = self.chain
                prev = chain if chain is not None and chain[0] == a - 1 else self._load_chain(a - 1)
                if prev is not None:
                    carry, ages = prev[1], prev[2]
                elif chain is not None and a > chain[0] and a - chain[0] - 1 <= MAX_GAP_H:
                    total += a - chain[0] - 1
                    a, carry, ages = chain[0] + 1, chain[1], chain[2]
                else:
                    # never past the chain: hours before it are its run's, and a hole among them
                    # is filled afresh, as before
                    began = a if chain is not None and chain[0] >= a else self._began(
                        a, None if chain is None else chain[0])
                    if began < a:
                        total += a - began
                        files = self._files(roots, min(began, runs[0][0]))
                    a, carry, ages = began, None, {}
                carry, ages = self._reduce_run(files, a, b, carry, ages, tick)
                n += b - a
                if chain is None or b - 1 >= chain[0]:
                    self.chain = (b - 1, carry, ages)
                elif b in self.have:
                    m = self.meta(b)
                    if m is not None and not m.get("carried"):
                        total += 1
                        carry, ages = self._reduce_run(files, b, b + 1, carry, ages, tick)
                        n += 1
                        if b == self.chain[0]:
                            self.chain = (b, carry, ages)
        except BaseException:
            # A run stopped mid-way may have changed the join state it was handed in place:
            # the newest chain is read again from where it was saved.
            self.chain = self._newest_chain()
            raise
        self._prune_chains()
        return n

    def _began(self, a, floor=None):
        """Where the run that saved the hours just before `a` began, when no
        chain carries on from them (hours whose chain could not be written or
        read): the saved hour it started afresh in, the hours after it each
        carried. `a` itself when the hour before is not saved, or one of
        those hours cannot be read, or the walk back passes an hour not saved
        or reaches `floor` (the chain's hour): no run can be redone from
        there."""
        h = a - 1
        while h in self.have and (floor is None or h > floor):
            m = self.meta(h)
            if m is None:
                return a
            if not m.get("carried"):
                return h
            h -= 1
        return a

    def _files(self, roots, oldest_hour):
        # a run may reach back MAX_GAP_H further, to the newest saved hour
        days = int((time.time() - (oldest_hour - MAX_GAP_H) * HOUR) // 86400) + 2
        return day_files(roots, max(1, days))

    def _reduce_run(self, files, a, b, carry, ages, tick=None):
        """Hours `[a, b)`, each seeded with the join state the one before
        ended with. Each saves that state for itself, its chain, then itself,
        and drops the chain of the hour before. Returns the join state the
        last one ended with."""
        norm = Normalizer(self.cfg)
        pm = self.meta(a - 1)
        # With no read of the hour before to go by, a record from before this
        # hour is the slack of a time-ordered file, not a straggler.
        prev = {f["path"]: f for f in pm["files"]} if pm else None
        for h in range(a, b):
            t0 = time.time()
            red = self._hour_set()
            carried = carry is not None
            if carried:
                seed(red, carry)
            strays = [] if prev is not None else None
            stats = new_stats()
            stream, plan, stats = read_span(files, norm, h * HOUR, (h + 1) * HOUR,
                                            self.events, prev, strays, stats)
            n, newest = feed_all(red, stream)
            carry = take_carry(red)
            strip(red)
            files_meta = [{k: e[k] for k in ("path", "dev", "ino", "head_n", "head", "stop")}
                          for e in plan]
            # Aged before it is saved: the chain is what the next hour starts from. Handed on
            # as it is, since it is saved: the next hour may change it in place, and a window
            # installs a copy (`install_carry`).
            carry, ages = age(self._cls, carry, ages, h)
            # The chain first, then the hour: a saved hour always has the state it carries
            # out, so a build stopped after it resumes there (R67). One saved for an hour
            # whose own save did not complete belongs to no saved hour, and is never used.
            self._save_chain(h, carry, ages)
            self._save_hour(h, red, {"records": n, "newest": newest, "carried": carried,
                                     "files": files_meta, "stragglers": 0,
                                     "reduced_at": time.time()})
            del red
            # no run starts at this hour now
            if h - 1 in self.chains:
                self._drop("c", h - 1)
            if strays:
                self._patch(strays, norm)
            prev = {f["path"]: f for f in files_meta}
            self.stats["hours_reduced"] += 1
            self.stats["records"] += n
            self.stats["bytes"] += stats["bytes"]
            self.stats["reduce_s"] += time.time() - t0
            if tick:
                tick(records=n, bytes=stats["bytes"])
            if self.pause:
                time.sleep(self.pause)      # the live tier's turn
        return carry, ages

    def _patch(self, strays, norm=None):
        """Reduce records that arrived for closed hours, and merge each hour's
        into its saved state. They join nothing in that hour -- the hour's
        joins are done -- but every count, sum and session they belong to
        takes them, so none is lost."""
        by_hour = collections.defaultdict(list)
        for x in strays:
            by_hour[int(x[0] // HOUR)].append(x)
        for h, recs in sorted(by_hour.items()):
            if h not in self.have:
                self.stats["stragglers_lost"] += len(recs)
                continue
            meta = self.meta(h)
            # Where each record it took sits: an hour read again (the seam
            # after a fresh start, a file lost) meets the same records again,
            # and must not merge them twice. By plain name: a day compressed
            # since holds them at the same offsets in its `.gz`.
            done = {(gz.plain_name(p), o) for p, o in (meta or {}).get("patched", ())}
            recs = [x for x in recs if (gz.plain_name(x[2]), x[3]) not in done]
            if not recs:
                continue
            red = self.load("h", h)
            if red is None or meta is None:
                self.stats["stragglers_lost"] += len(recs)
                continue
            part = self._hour_set()
            recs.sort(key=itemgetter(0))
            n, newest = feed_all(part, ((x[0], x[1]) for x in recs))
            strip(part)
            merge_into(red, part)
            for r in red.values():
                if hasattr(r, "bin_s"):
                    r.bin_s = HOUR_BIN_S
            meta = dict(meta, stragglers=meta.get("stragglers", 0) + n,
                        patched=list(done) + [(gz.plain_name(x[2]), x[3]) for x in recs],
                        records=meta.get("records", 0) + n,
                        newest=max(x for x in (meta.get("newest"), newest) if x is not None),
                        patched_at=time.time())
            self._save_hour(h, red, meta)
            self.stats["stragglers"] += n
            self.stats["patched_hours"] += 1

    def missing(self, roots, h_lo, h_hi):
        self._sync(roots)
        return sum(1 for h in range(h_lo, h_hi) if h not in self.have)

    def behind(self, now, roots):
        """Whether an hour has closed that the newest saved one does not reach."""
        self._sync(roots)
        return self.chain is not None and self.chain[0] < self.closed_end(now, roots) - 1

    # ---------------------------------------------------------------- merge
    def parts(self, h_lo, h_hi):
        """What `[h_lo, h_hi)` is merged from, oldest first: whole epoch days
        as one state each (when `panels.day_cache`), the rest hour by hour."""
        h = h_lo
        while h < h_hi:
            if self.day_cache and h % 24 == 0 and h + 24 <= h_hi:
                yield "d", h // 24
                h += 24
            else:
                yield "h", h
                h += 1

    def load_part(self, kind, idx):
        """A part as a reducer set; a missing day is merged from its hours and
        saved first."""
        if kind == "h":
            return self.load("h", idx)
        red = self.load("d", idx) if idx in self.days else None
        if red is not None:
            return red
        day = self.fresh()
        for r in day.values():
            if hasattr(r, "bin_s"):
                r.bin_s = HOUR_BIN_S
        for h in range(idx * 24, idx * 24 + 24):
            part = self.load("h", h)
            if part is None:
                raise LookupError("hour %d is not saved" % h)
            merge_into(day, part)
            del part
        try:
            self._write(self._path("d", idx), {"kind": "day", "day": idx,
                                               "bin_s": HOUR_BIN_S},
                        snapshots.capture(day))
            self.days.add(idx)
            self.stats["days_built"] += 1
        except Exception:
            pass                            # a day not saved is merged again next time
        return day

    def install_carry(self, acc, last_hour):
        """Install the join state hour `last_hour` ended with, if it is the
        newest one's. Returns whether it was."""
        if self.chain is None or self.chain[0] != last_hour:
            return False
        install(acc, pickle.loads(pickle.dumps(self.chain[1],
                                               protocol=pickle.HIGHEST_PROTOCOL)))
        return True

    def newest_of(self, hours):
        """The newest record any of these saved hours holds."""
        for h in sorted(hours, reverse=True):
            m = self.meta(h)
            if m and m.get("newest") is not None:
                return m["newest"]
        return None

    # -------------------------------------------------------------- upkeep
    def advance(self, roots, now=None):
        """Reduce the hours that have closed since the newest saved one (in
        retention), merge a day that just completed, and prune. The registry
        runs this behind its window builds, so a window built later finds its
        hours saved."""
        self._sync(roots)
        now = time.time() if now is None else now
        h_hi = self.closed_end(now, roots)
        h_lo = h_hi - self.retention_h
        if self.chain is None:
            # nothing reduced yet: leave it to the first window that asks
            return 0
        n = self.ensure(roots, max(h_lo, self.chain[0] + 1), h_hi)
        if self.day_cache:
            d = h_hi // 24 - 1              # the newest day all of whose hours are closed
            if d not in self.days and all(h in self.have for h in range(d * 24, d * 24 + 24)):
                try:
                    self.load_part("d", d)
                except LookupError:
                    pass
        self.prune(now)
        return n

    def prune(self, now=None):
        """Drop hours and days past retention, and states of other code or
        config -- all but the newest such generation, which another process
        sharing this directory may still be using."""
        now = time.time() if now is None else now
        oldest = int(now // HOUR) - self.retention_h - 24
        try:
            names = os.listdir(self.dir)
        except OSError:
            return
        other = collections.defaultdict(list)
        for n in names:
            p = os.path.join(self.dir, n)
            if n.endswith(".tmp"):
                try:
                    if os.path.getmtime(p) < now - 3600:
                        os.unlink(p)
                except OSError:
                    pass
                continue
            stem, _, rest = n.partition(".")
            fp = rest[:-3] if rest.endswith(".hr") else None
            if fp is None:
                continue
            if fp != self.fp:
                other[fp].append(p)
                continue
            try:
                idx = int(stem[1:])
            except ValueError:
                continue
            if (stem[0] in "hc" and idx < oldest) or (stem[0] == "d" and idx * 24 < oldest):
                self._drop(stem[0], idx)
                self.stats["pruned"] += 1
        if len(other) > 1:
            def newest(paths):
                try:
                    return max(os.path.getmtime(p) for p in paths)
                except OSError:
                    return 0
            keep = max(other, key=lambda fp: newest(other[fp]))
            for fp, paths in other.items():
                if fp == keep:
                    continue
                for p in paths:
                    try:
                        os.unlink(p)
                        self.stats["pruned"] += 1
                    except OSError:
                        pass

    def disk_bytes(self):
        tot = 0
        for n in os.listdir(self.dir) if os.path.isdir(self.dir) else ():
            if self.fp and n.endswith(".%s.hr" % self.fp):
                try:
                    tot += os.path.getsize(os.path.join(self.dir, n))
                except OSError:
                    pass
        return tot

    def status(self):
        """Read from the API thread while the build thread works: every value
        is taken whole (a sorted copy, a tuple), never iterated live."""
        have, chain = sorted(self.have), self.chain
        return {"hours": len(have), "days": len(self.days), "chains": len(self.chains),
                "newest_hour": (time.strftime("%Y-%m-%d %H:00", time.localtime(
                    have[-1] * HOUR)) if have else None),
                "carried_through": (time.strftime("%Y-%m-%d %H:00", time.localtime(
                    (chain[0] + 1) * HOUR)) if chain else None),
                "grace_s": self.grace, **{k: (round(v, 1) if isinstance(v, float) else v)
                                          for k, v in list(self.stats.items())}}


# ------------------------------------------------------------ a window build
def build(reg, view):
    """Build `view` from saved hours: the exact edge, the hours merged, the
    open hour read. Returns False, having changed nothing the scan relies on,
    when a window cannot be built this way -- no closed hour inside it, or a
    reducer with no `merge` -- and the registry scans it as before.

    The hours it needs and does not have are reduced first, while the view
    still serves what it held: that is the one read here that grows with the
    window, and it happens once per hour, ever. Live records are buffered only
    from the edge read on, as a scan buffers them from its start, and replayed
    past what the reads reached (`PanelView.feed`).
    """
    store = reg.hours
    t0 = time.time()
    roots = reg.roots_fn()
    since = t0 - view.window_s
    h_lo = int(math.ceil(since / HOUR))
    h_hi = store.closed_end(t0, roots)
    if h_hi <= h_lo:
        return False
    acc = view.fresh_reducers()
    lacking = mergeable(acc)
    if lacking:
        view.cache["note"] = "scanned: no merge for %s" % ", ".join(lacking)
        return False
    view.state = "building"
    view.error = None
    view.cache["note"] = None
    # The edge's own hour is reduced too, though only its tail end is in the
    # window: the first whole hour is seeded with what it ended with, so a
    # call that straddles the window's first hour is joined, not split.
    h_from = h_lo - 1
    missing = store.missing(roots, h_from, h_hi)
    parts = list(store.parts(h_lo, h_hi))
    # Progress in hour-reads: a missing hour is one, the edge and the tail one
    # each, a merge a tenth (it is a load and a fold, not a read).
    weight = {"done": 0.0, "total": missing + 2 + 0.1 * len(parts)}
    prog = view.progress = {"pct": 0, "records": 0, "bytes": 0, "total_bytes": 0,
                            "files": 0, "elapsed_s": 0.0, "mode": "merge"}

    def step(w, records=0, bytes=0):
        weight["done"] += w
        prog.update(records=prog["records"] + records, bytes=prog["bytes"] + bytes,
                    total_bytes=max(prog["total_bytes"], prog["bytes"] + bytes),
                    pct=min(99, int(100 * weight["done"] / weight["total"])),
                    elapsed_s=round(time.time() - t0, 1))

    try:
        reduced = store.ensure(roots, h_from, h_hi, progress=lambda i, n, records=0,
                               bytes=0: step(1, records, bytes))
    except Exception as e:      # a store that cannot be written is no store
        view.cache["note"] = "scanned: reducing the hours failed (%s)" % e
        return False
    with reg.lock:
        view._replay = collections.deque()
    try:
        files = store._files(roots, h_lo)
        prog["files"] = len(files)
        norm = Normalizer(store.cfg)
        stats = new_stats()
        stream, _plan, stats = read_span(files, norm, since, h_lo * HOUR, store.events,
                                         stats=stats)
        n_edge, _ = feed_all(acc, stream)
        step(1, n_edge, stats["bytes"])
        for kind, idx in parts:
            part = store.load_part(kind, idx)
            if part is None:
                raise LookupError("the saved %s %d is unreadable" %
                                  ("hour" if kind == "h" else "day", idx))
            merge_into(acc, part)
            del part
            store.stats["merged_states"] += 1
            step(0.1)
        joined = store.install_carry(acc, h_hi - 1)
        stats = new_stats()
        stream, _plan, stats = read_span(files, norm, h_hi * HOUR, None, store.events,
                                         stats=stats)
        n_tail, newest = feed_all(acc, stream)
        step(1, n_tail, stats["bytes"])
    except Exception as e:
        with reg.lock:
            view._replay = None
        view.cache["note"] = "scanned: merging the saved hours failed (%s)" % e
        return False
    if newest is None:
        newest = store.newest_of(range(h_lo, h_hi))
    dispatch = view.build_dispatch(acc)
    with reg.lock:
        view.reducers = acc
        view.dispatch = dispatch
        view.covers_from = since
        # What the reads actually reached, not what they aimed at: an empty or
        # lagging feed must not claim coverage up to `now`.
        view.covers_to = newest or h_hi * HOUR
        view.built_at = time.time()
        view.consumed_through = newest
        view.live_through = None
        view.live_records = 0
        days = sum(1 for k, _ in parts if k == "d")
        view.cache["source"] = "hours"
        view.cache["saved_at"] = None
        view.cache["note"] = (
            "merged %d saved hour state(s)%s, reducing %d new hour(s); read %s "
            "record(s) before the first whole hour and %s since the last closed "
            "one%s" % (len(parts) - days, " and %d day(s)" % days if days else "",
                       reduced, "{:,}".format(n_edge), "{:,}".format(n_tail),
                       "" if joined else "; the newest hour's join state was not at "
                       "hand, so the open hour joins only within itself"))
        pending, view._replay = view._replay, None
        for rec in pending or ():
            view.feed(rec)
        view.state = "ready"
        view.installed_at = time.time()
        prog.update(pct=100, elapsed_s=round(time.time() - t0, 1))
        view.materialize()
    return True
