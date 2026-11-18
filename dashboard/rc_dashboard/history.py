"""The history store: every sealed day-file of the eBPF tier kept as Parquet.

A collector seals a day by gzipping its day-files (`<day>/<host>.<stream>.jsonl` to
`.jsonl.gz`), and a sealed file never changes again. For each one the store keeps
`feedparquet`'s tables and manifest (`history.dir`, `<cache.dir>/history` unless set):

    <dir>/ebpf/<root basename>-<sha1 of its real path, 10 hex>/
        SOURCE.json                                the source root it mirrors
        <table>/day=<D>/<host>.<stream>.parquet    the records, in file order, `_off`
        _manifest/day=<D>/<host>.<stream>.json     what else the file could be asked

which is also a dataset to query: `read_parquet('<root>/exit/*/*.parquet')` in DuckDB.

The readers take a sealed file's records from its tables when they read selectively
-- one session's, one user's, the commands behind one figure -- and search it by time
without inflating the `.gz` (`replica.py`). The tables give the file's own offsets, so
a reader may take one part of a read from the file and the next from its tables.
Whole-range reads (the backfill, an hour's reduction) read the JSONL, which orjson
turns into records faster than Arrow does.

Nothing waits for the store. It converts in the background, in `history.workers`
processes, every file it has no current replica of -- missing, or its source no
longer the file the manifest names -- the biggest first, at startup and then every
DISCOVER_S as days are sealed; a file not converted yet is read as JSONL. A replica
whose source is gone goes too, and so does one older than the newest `history.days`
dated dirs when that is set. The store is the service's own: each store root is
made 0700, as its other caches are, since it holds every user's command lines.

A conversion writes beside each final name (`.tmp-<pid>`) and renames into place,
the manifest last. A worker stopped by SIGTERM (systemd stopping the service)
removes what it was writing, as on any error; what a SIGKILL leaves is swept the
next time the service starts, once its process is gone.
"""
import collections
import concurrent.futures
import hashlib
import json
import multiprocessing
import os
import re
import signal
import threading
import time

from . import feedfile
from . import feedparquet as fp
from . import replica

STORE_DIR = "history"
MARKER = "SOURCE.json"
DIR_MODE = 0o700
FILE_MODE = 0o600
TIER = "ebpf"
# An hour of a busy login node's exits is 100-150 thousand rows: a read of an
# hour's span touches one or two row groups. 1 Mi rows compress 5-8 % better.
ROW_GROUP_ROWS = 1 << 17
DISCOVER_S = 60.0
# Replicas held between reads, with the indexes their reads built (`Replica.nbytes`,
# about 30 bytes a line): at most this many, and this much memory. A day of 8 busy
# login nodes (26 M lines) fits; a read over a week builds again what was let go.
REPLICAS_MAX = 64
REPLICA_BYTES = 1 << 30
WORKERS_MAX = 8                  # a conversion holds about a row group in memory
DEFAULTS = {"enabled": True, "dir": "", "workers": 0, "days": 0}
_SAFE = re.compile(r"[^A-Za-z0-9._-]")
_TMP = re.compile(r"\.tmp-(\d+)$")       # feedparquet.convert_source's own suffix


def usable_cpus():
    """The CPUs this process may run on (its affinity, so a Slurm allocation or a
    cpuset is taken as the limit it is)."""
    count = getattr(os, "process_cpu_count", None)
    if count is not None:
        n = count()
    else:
        try:
            n = len(os.sched_getaffinity(0))
        except (AttributeError, OSError):
            n = os.cpu_count()
    return max(1, n or 1)


def settings(cfg):
    """`history`, resolved: `{enabled, dir, workers, days}`."""
    h = cfg.get("history") or {}
    if not isinstance(h, dict):
        h = {}
    d = str(h.get("dir") or "").strip() or os.path.join(cfg.get("cache.dir") or ".", STORE_DIR)
    try:
        workers = int(h.get("workers") or 0)
    except (TypeError, ValueError):
        workers = 0
    if workers <= 0:
        workers = min(WORKERS_MAX, usable_cpus())
    try:
        days = max(0, int(h.get("days") or 0))
    except (TypeError, ValueError):
        days = 0
    enabled = h.get("enabled", True)
    if isinstance(enabled, str):
        enabled = enabled.strip().lower() not in ("0", "false", "no", "off", "")
    return {"enabled": bool(enabled), "dir": os.path.abspath(d), "workers": workers,
            "days": days}


def root_name(src):
    """`<sanitized basename>-<sha1(realpath)[:10]>`: one store root per source root."""
    real = os.path.realpath(src)
    base = _SAFE.sub("_", os.path.basename(os.path.normpath(os.path.abspath(src)))) or "root"
    return "%s-%s" % (base, hashlib.sha1(real.encode("utf-8", "surrogateescape"))
                      .hexdigest()[:10])


def _convert(day, path, out):
    """One conversion, in a worker process. A SIGTERM ends the worker, once
    `convert_source` has removed what it was writing: its pool would only hand
    it the next file."""
    try:
        return fp.convert_source(day, path, out, row_group_rows=ROW_GROUP_ROWS)
    except SystemExit:
        os._exit(143)


def _worker_init():
    """A conversion worker: SIGTERM ends it as an error would, so the files it was
    writing are removed (`convert_source`) rather than left half-written."""
    signal.signal(signal.SIGTERM, _terminated)


def _terminated(signum, _frame):
    raise SystemExit(128 + signum)


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True                 # someone else's: running
    return True


def sweep(out):
    """Remove what a conversion killed mid-file left under store root `out`: each
    `.tmp-<pid>` file whose process is gone. Returns how many."""
    n = 0
    for dp, _dns, fns in os.walk(out):
        for f in fns:
            m = _TMP.search(f)
            if m and not _alive(int(m.group(1))):
                try:
                    os.unlink(os.path.join(dp, f))
                    n += 1
                except OSError:
                    pass
    return n


class HistoryStore:
    def __init__(self, cfg):
        s = settings(cfg)
        self.enabled = s["enabled"]
        self.dir = s["dir"]
        self.workers = s["workers"]
        self.days = s["days"]
        roots = [r for r in (cfg.get("feeds.%s.roots" % TIER) or []) if r]
        self.roots = {os.path.abspath(r): os.path.join(self.dir, TIER, root_name(r))
                      for r in roots}
        self.state = "off" if not self.enabled else "idle"
        self.note = None
        self.progress = {"todo": 0, "done": 0, "failed": 0, "records": 0, "bytes_in": 0,
                         "elapsed_s": 0.0}
        self.errors = collections.deque(maxlen=20)
        self.converted = 0                     # since start
        self.swept = 0                         # a killed conversion's leftovers removed
        # sealed files a reader asked for, since start: answered from a replica,
        # or left to the JSONL (no current replica yet)
        self.lookups = {"replica": 0, "jsonl": 0}
        self._lock = threading.Lock()
        self._cache = collections.OrderedDict()   # path -> (ident, Replica)
        self._stop = threading.Event()
        self._thread = None
        self._ready = {}                       # source root -> its store root is made

    # ------------------------------------------------------------- the roots
    def _ensure_root(self, src):
        """The store root of source root `src` made, with its marker. Refuses one
        that mirrors another source (the marker says so)."""
        out = self.roots[src]
        if self._ready.get(src):
            return out
        os.makedirs(out, mode=DIR_MODE, exist_ok=True)
        os.chmod(out, DIR_MODE)
        marker = os.path.join(out, MARKER)
        real = os.path.realpath(src)
        try:
            with open(marker) as fh:
                had = json.load(fh)
        except (OSError, ValueError):
            had = None
        if isinstance(had, dict) and had.get("realpath") not in (None, real):
            raise RuntimeError("history root %s mirrors %s, not %s: remove it, or set "
                               "history.dir elsewhere" % (out, had.get("realpath"), real))
        if not isinstance(had, dict):
            tmp = marker + ".%d.tmp" % os.getpid()
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, FILE_MODE)
            with os.fdopen(fd, "w") as fh:
                json.dump({"source": src, "realpath": real, "tier": TIER,
                           "created": time.time(),
                           "note": "Parquet tables of the source root's sealed day-files "
                                   "(rc_dashboard/history.py)"}, fh, indent=1)
            os.replace(tmp, marker)
        self.swept += sweep(out)
        self._ready[src] = True
        return out

    def _locate(self, path):
        """`(source root, day, stem)` of a sealed day-file under a source root."""
        p = os.path.abspath(path)
        dday = os.path.dirname(p)
        src = os.path.dirname(dday)
        if src not in self.roots or not p.endswith(".jsonl" + ".gz"):
            return None
        return src, os.path.basename(dday), fp.stem_of(p)

    # ------------------------------------------------------------- replicas
    def replica_for(self, path):
        """The replica that stands in for sealed day-file `path` now, or None."""
        if not self.enabled:
            return None
        loc = self._locate(path)
        if loc is None:
            return None
        rep = self._replica(path, loc)
        with self._lock:
            self.lookups["jsonl" if rep is None else "replica"] += 1
        return rep

    def _replica(self, path, loc):
        try:
            st = os.stat(path)
        except OSError:
            return None
        ident = (st.st_size, st.st_mtime_ns, st.st_ino)
        src, day, stem = loc
        out = self.roots[src]
        with self._lock:
            hit = self._cache.get(path)
            # The source unchanged is not enough: `history.days` prunes a copy
            # whose source is still there, and its tables go with its manifest.
            if hit is not None and hit[0] == ident \
                    and os.path.exists(fp.manifest_path(out, day, stem)):
                self._cache.move_to_end(path)
                self._trim()
                return hit[1]
            if hit is not None:
                self._cache.pop(path, None)
        man = fp.read_manifest(out, day, stem)
        if not fp.current(man, path) or not replica.exact(man):
            return None
        rep = replica.Replica(out, man)
        with self._lock:
            self._cache[path] = (ident, rep)
            self._trim()
        return rep

    def _forget(self, gone):
        """Let go of the held replicas of `gone` (out, day, stem): their tables
        are about to be removed, and a reader handed one would open none."""
        gone = set(gone)
        with self._lock:
            for path in list(self._cache):
                loc = self._locate(path)
                if loc is not None and (self.roots.get(loc[0]), loc[1], loc[2]) in gone:
                    del self._cache[path]

    def _trim(self):
        """Let the least recently used replicas go while more than REPLICAS_MAX are
        held, or their indexes hold more than REPLICA_BYTES -- never the one just
        asked for. A reader still holding one keeps it until it is done."""
        held = self.held_bytes()
        while len(self._cache) > 1 and (len(self._cache) > REPLICAS_MAX
                                        or held > REPLICA_BYTES):
            _p, (_i, rep) = self._cache.popitem(last=False)
            held -= rep.nbytes()

    def held_bytes(self):
        return sum(rep.nbytes() for _i, rep in list(self._cache.values()))

    # ------------------------------------------------------------- the work
    def discover(self):
        """`(todo, prune)`: sealed files with no current replica, biggest first,
        as `(day, path, store root)`; and replicas to drop, as `(store root, day,
        stem)`, whose source is gone or older than the newest `days`."""
        todo, prune = [], []
        for src in self.roots:
            if not os.path.isdir(src):
                continue
            out = self._ensure_root(src)
            dirs = feedfile.dated_dirs(src, 0)
            keep_days = {d for d, _f in dirs[-self.days:]} if self.days else None
            have = set()
            for day, files in dirs:
                if keep_days is not None and day not in keep_days:
                    continue
                for p in files:
                    if not p.endswith(".jsonl.gz"):
                        continue
                    have.add((day, fp.stem_of(p)))
                    if not fp.current(fp.read_manifest(out, day, fp.stem_of(p)), p):
                        try:
                            size = os.path.getsize(p)
                        except OSError:
                            continue
                        todo.append((size, day, p, out))
            mdir = os.path.join(out, "_manifest")
            for dd in (os.listdir(mdir) if os.path.isdir(mdir) else ()):
                if not dd.startswith("day="):
                    continue
                for name in os.listdir(os.path.join(mdir, dd)):
                    if name.endswith(".json") and (dd[4:], name[:-5]) not in have:
                        prune.append((out, dd[4:], name[:-5]))
        todo.sort(reverse=True)
        return [(d, p, o) for _s, d, p, o in todo], prune

    def run_once(self):
        """Discover, prune, convert. Returns how many files were converted."""
        if not self.enabled:
            return 0
        todo, prune = self.discover()
        if prune:
            self._forget(prune)
        for out, day, stem in prune:
            fp.drop_source(out, day, stem)
            _rmdirs(out, day)
        if not todo:
            self.state = "idle"
            return 0
        self.state = "converting"
        t0 = time.time()
        with self._lock:
            self.progress = {"todo": len(todo), "done": 0, "failed": 0, "records": 0,
                             "bytes_in": sum(os.path.getsize(p) for _d, p, _o in todo
                                             if os.path.exists(p)),
                             "elapsed_s": 0.0}
        done = 0
        workers = min(self.workers, len(todo))
        if workers <= 1:
            for day, p, out in todo:
                if self._stop.is_set():
                    break
                done += self._finish(p, _try(_convert, day, p, out), t0)
        else:
            ctx = multiprocessing.get_context("spawn")
            with concurrent.futures.ProcessPoolExecutor(workers, mp_context=ctx,
                                                        initializer=_worker_init) as ex:
                futs = {ex.submit(_convert, day, p, out): p for day, p, out in todo}
                for f in concurrent.futures.as_completed(futs):
                    try:
                        res = f.result()
                    except BaseException as e:  # one file failing stops nothing else
                        res = e
                    done += self._finish(futs[f], res, t0)
                    if self._stop.is_set():
                        for g in futs:
                            g.cancel()
                        break
        self.state = "idle"
        return done

    def _finish(self, path, res, t0):
        with self._lock:
            self.progress["elapsed_s"] = round(time.time() - t0, 1)
            if isinstance(res, BaseException):
                self.progress["failed"] += 1
                self.errors.append({"file": path, "error": "%s: %s" % (type(res).__name__, res),
                                    "at": time.time()})
                return 0
            self.progress["done"] += 1
            self.progress["records"] += sum(res.get("records", {}).values())
            self.converted += 1
            self._cache.pop(path, None)
            return 1

    # ------------------------------------------------------------- the thread
    def start(self):
        """Convert in the background and keep converting as days are sealed;
        the readers find replicas through `replica.for_path` from now on."""
        replica.install(self.replica_for if self.enabled else None)
        if not self.enabled or self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="history", daemon=True)
        self._thread.start()

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception as e:
                self.state = "error"
                self.note = "%s: %s" % (type(e).__name__, e)
            self._stop.wait(DISCOVER_S)

    def stop(self):
        self._stop.set()
        replica.install(None)

    # ------------------------------------------------------------- the report
    def status(self):
        """The store's state for /api/feeds and --check-feeds."""
        with self._lock:
            prog = dict(self.progress)
            errors = list(self.errors)
            lookups = dict(self.lookups)
            held = {"replicas": len(self._cache), "bytes": self.held_bytes()}
        replicas = files = 0
        size = 0
        for out in self.roots.values():
            for dp, _dns, fns in os.walk(out):
                for f in fns:
                    if f.endswith(".parquet"):
                        files += 1
                        try:
                            size += os.path.getsize(os.path.join(dp, f))
                        except OSError:
                            pass
                    elif f.endswith(".json") and os.path.basename(os.path.dirname(dp)) \
                            == "_manifest":
                        replicas += 1
        return {"enabled": self.enabled, "state": self.state, "note": self.note,
                "dir": self.dir, "workers": self.workers, "days": self.days,
                "source_roots": sorted(self.roots), "store_roots": sorted(self.roots.values()),
                "replicas": replicas, "files": files, "bytes": size,
                "converted": self.converted, "swept": self.swept, "lookups": lookups,
                "held": held,
                "progress": prog, "errors": errors[-5:]}


def describe(cfg):
    """`--check-feeds`'s lines on the store, read-only: what it holds of the
    sealed day-files and what it would convert."""
    s = settings(cfg)
    if not s["enabled"]:
        return ["  history %-9s off (history.enabled): sealed days are read as JSONL" % TIER]
    st = HistoryStore(cfg)
    sealed = current = 0
    todo_bytes = 0
    for src, out in st.roots.items():
        if not os.path.isdir(src):
            continue
        dirs = feedfile.dated_dirs(src, 0)
        if st.days:
            dirs = dirs[-st.days:]
        for day, files in dirs:
            for p in files:
                if not p.endswith(".jsonl.gz"):
                    continue
                sealed += 1
                if fp.current(fp.read_manifest(out, day, fp.stem_of(p)), p):
                    current += 1
                else:
                    try:
                        todo_bytes += os.path.getsize(p)
                    except OSError:
                        pass
    return ["  history %-9s on: %d sealed day-file(s), %d with a current replica, %d to "
            "convert (%.1f MB compressed); store %s" % (
                TIER, sealed, current, sealed - current, todo_bytes / 1e6,
                os.path.join(s["dir"], TIER))]


def _try(fn, *args):
    try:
        return fn(*args)
    except Exception as e:
        return e


def _rmdirs(out, day):
    """The day's now-empty dirs under every table, removed."""
    for t in list(fp.TABLES) + ["_manifest"]:
        try:
            os.rmdir(os.path.join(out, t, "day=%s" % day))
        except OSError:
            pass
