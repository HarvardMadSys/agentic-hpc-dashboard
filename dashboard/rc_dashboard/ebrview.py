"""The service reads EBR only: what the collector wrote only as JSONL is read
through an EBR copy, made at startup and kept current (`ingest.convert`).

EBR is what the readers are fastest and smallest on, and a feed is often JSONL
alone: a collector from before EBR, a capture copied in from another cluster,
a day before the collector was moved to `both`. So before the service reads
anything, each tier's day-files are mirrored into a VIEW of EBR files, and the
service is pointed at the view instead of the collector's roots
(`feeds.<tier>.roots`; the collector's own stay in `feeds.<tier>.source_roots`).
Every reader -- the tail, the backfill, window builds, hours, the inspector,
the I/O lookups, the event scans, the feed report -- then sees EBR alone.

WHERE. `ingest.convert.dir`, `<cache.dir>/ebr-view` unless set, holds one view
root per tier and source root: `<dir>/<tier>/<basename>-<sha1(realpath)[:10]>`,
with `SOURCE.json` naming the source (a guard against a hash collision, and a
note for whoever lists the directory). A view root mirrors exactly what a reader
reads from its source root: its newest `feeds.<tier>.days` dated dirs
(`feedfile.dated_dirs`) and as many flat files (`feedfile.feed_files`).

PER STREAM, by the twin rule with prefer=ebr (`feedfile._listing`):

  held              nothing yet: no file in the view
  the EBR file      LINKED: a symlink of the same name to the source file. A
                    plain file gone to its `.gz` is relinked: the `.gz` link
                    first, then the plain one removed, the collector's order
  the JSONL file    CONVERTED into `<stem>.ebr`: always a plain file, sealed
                    days too; zstd frames only with `ingest.convert.zstd`

A decision is kept by the view: an entry that exists decides (a symlink is a
link, a file a conversion), so a restart never moves a stream to another file
mid-day. Only a stream with no entry goes through the twin rule. A converted
file's mtime is its source's, so the feed report reads as the collector wrote.

CONVERSION matches the JSONL tail (`tail.FileTailSource._read_one`) record for
record: complete lines only, blank ones passed, each other parsed with
`fastjson.loads`; one that does not parse, or is not an object, is a bad line,
counted. A bulk conversion (startup, a sealed day, a stream far behind) cuts a
frame on the minute (`FRAME_S`, as the demo's writer does); a poll's
conversion is one batch and one flush, as a collector's round.

CHECKPOINTS. A converted file carries advisory MARK frames (`ebr.MARK`): where
in its source the records before it end (`off`, in uncompressed bytes), with
the counts and a digest of the source's first min(4 KB, off) bytes. One is
written at most a minute apart while converting, at the end of every bulk
conversion, and when a day is sealed. A restart resumes at the last one
(`ebr.last_mark`), passing the records written after it, and checks the source
first: the same first bytes, and at least `off` of them -- else the file is
converted afresh, into a new inode the readers take as a replaced file. A sealed
file whose `.gz` is still the same file on disk is not opened at all.

STARTUP. `catch_up()` converts everything behind before anything reads (the
app runs it first, in a process pool, `ingest.convert.workers`); `sync()` then
runs before every poll, so a line the collector writes is read in the same
poll it is converted. Heavier work found while live -- a day sealed into its
`.gz`, a source replaced, a new stream with a backlog -- is a bulk conversion
on a thread of its own, so a poll stays bounded.
"""
import collections
import concurrent.futures
import gzip
import hashlib
import json
import multiprocessing
import os
import re
import stat
import threading
import time
from concurrent.futures.process import BrokenProcessPool

from . import ebr, fastjson
from .feedfile import HEAD_BYTES, _listing, dated_dirs, format_of, host_of, split_name

VIEW_DIR = "ebr-view"           # under cache.dir unless ingest.convert.dir says
MARKER = "SOURCE.json"          # in every view root: the source it mirrors
# The view is this service's alone, as its other caches are (payloads.py,
# hours.py, snapshots.py): its records hold every user's command lines, which
# the collector keeps 0640 in 0750 dirs. A link's target keeps its own mode.
DIR_MODE = 0o700
FILE_MODE = 0o600
WRITER = "rc_dashboard.ebrview"  # a converted file's segments say who wrote them
MARK_V = 1                      # a MARK's `v`
TIERS = ("ebpf", "ebpf_node")
FRAME_S = 60                    # a bulk conversion's frame: one cell of this epoch grid
MARK_EVERY_S = 60.0             # live: a MARK at most this often (monotonic seconds)
BULK_MARK_BYTES = 64 << 20      # bulk: a MARK every this much of the source
CHUNK = 8 << 20                 # bulk: read this much of the source at a time
LINE_MAX = 64 << 20             # a longer line is never complete (the tail's READ_CAP)
SYNC_FILE_BYTES = 64 << 20      # live: the most one poll converts of one file
SYNC_POLL_BYTES = 256 << 20     # live: ... and of all files together
JOB_BYTES = 64 << 20            # live: a file this far behind is converted in bulk, apart
DISCOVER_S = 30.0               # every day, stream and flat file listed again this often
IDLE_CLOSE_S = 300.0            # a file not written this long has its writer closed
LOCK_WAIT_S = 10.0              # a bulk conversion waits this long for a file's lock
POOL_MIN_BYTES = 32 << 20       # less to convert than this is not worth starting processes
RETRY_S = (5.0, 30.0, 120.0, 600.0)
ERRORS_KEEP = 20
_TMP = re.compile(r"^\..+\.(?:rebuild|link)\.\d+$")     # our own temp names in a view dir
_SAFE = re.compile(r"[^A-Za-z0-9._-]")
_GZ = ".gz"


class ConvertError(ValueError):
    """A view that cannot be used as configured: the service refuses to start."""


class _Rebuild(Exception):
    """The view file no longer describes its source: convert it afresh."""


class _Stopped(Exception):
    """The service is stopping: the file is left as it is (a MARK resumes it)."""


def truthy(v):
    if isinstance(v, str):
        return v.strip().lower() not in ("0", "false", "no", "off", "")
    return bool(v)


def usable_cpus():
    """The CPUs this process may run on: every one of them is a conversion
    worker unless `ingest.convert.workers` says otherwise. Its affinity mask, so
    a Slurm allocation, a cgroup cpuset or taskset is taken as the limit it is
    (os.process_cpu_count, 3.13+), else the machine's count."""
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
    """`ingest.convert`, resolved: `{enabled, dir, zstd, workers}`."""
    c = cfg.get("ingest.convert") or {}
    if not isinstance(c, dict):
        c = {}
    d = str(c.get("dir") or "").strip()
    if not d:
        d = os.path.join(cfg.get("cache.dir") or ".", VIEW_DIR)
    try:
        workers = int(c.get("workers") or 0)
    except (TypeError, ValueError):
        workers = 0
    if workers <= 0:
        workers = usable_cpus()
    try:
        level = int(c.get("zstd") or 0)
    except (TypeError, ValueError):
        level = 0
    return {"enabled": truthy(c.get("enabled", True)), "dir": os.path.abspath(d),
            "zstd": max(0, level), "workers": workers}


def zstd_kw(level):
    """`(Writer kwargs, why not)`: zstd frames only when asked for and available.
    With level 0 nothing is passed, so this works with an ebr.py that has no
    `zstd=` as well as with one that has."""
    if level <= 0:
        return {}, None
    if not getattr(ebr, "zstd_available", lambda: False)():
        return {}, ("ingest.convert.zstd is %d, but zstd frames cannot be written here "
                    "(ebr.zstd_available): the view is written uncompressed" % level)
    return {"zstd": level}, None


def view_root_name(src):
    """`<sanitized basename>-<sha1(realpath)[:10]>`: one view root per source root."""
    real = os.path.realpath(src)
    base = _SAFE.sub("_", os.path.basename(os.path.normpath(os.path.abspath(src)))) or "root"
    return "%s-%s" % (base, hashlib.sha1(real.encode("utf-8", "surrogateescape"))
                      .hexdigest()[:10])


def _inside(path, root):
    path, root = os.path.realpath(path), os.path.realpath(root)
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


# ------------------------------------------------------------------ converting
class _Counts(object):
    """A conversion's running counts. `skip`: records already in the view file
    after its last MARK, passed on a resume rather than written again."""
    __slots__ = ("records", "bad", "skip", "minute", "written", "unwritable", "cut")

    def __init__(self, records=0, bad=0, skip=0, cut=False):
        self.records = records
        self.bad = bad
        self.skip = skip
        self.minute = None
        self.written = 0
        self.unwritable = 0
        self.cut = cut              # bulk: a frame per minute of the epoch grid


def feed_lines(data, w, c):
    """Write the records of `data`, complete JSONL lines, to writer `w`, as the
    JSONL tail reads them: a blank line is passed, a line `fastjson.loads`
    refuses or that is not an object counts as bad."""
    loads = fastjson.loads
    for line in data.split(b"\n"):
        if line[:1] != b"{" and not line.strip():
            continue
        try:
            rec = loads(line)
        except ValueError:
            c.bad += 1
            continue
        if type(rec) is not dict:
            c.bad += 1
            continue
        if c.skip:
            c.skip -= 1
            continue
        if c.cut:
            t = rec.get("ts_epoch")
            tt = type(t)
            if tt is int or (tt is float and t - t == 0.0):
                m = int(t // FRAME_S)
                if c.minute is None:
                    c.minute = m
                elif m > c.minute:
                    w.flush()           # the frame of the minute before ends here
                    c.minute = m
        try:
            w.write(rec)
        except (TypeError, ValueError):
            # A record EBR cannot hold (nested past its depth, or past a frame):
            # never one the collector writes; counted with the bad lines.
            c.bad += 1
            c.unwritable += 1
            continue
        c.records += 1
        c.written += 1


def _digest(b):
    return hashlib.sha1(b).hexdigest()


def mark_info(src, st, off, records, bad, head, head_len, sealed):
    """What a MARK says: where in `src` (by name) the records before it end,
    and `src` as it was on disk (`st`)."""
    return {"v": MARK_V, "src": os.path.basename(src), "off": int(off),
            "records": int(records), "bad": int(bad), "head": head,
            "head_len": int(head_len), "sealed": bool(sealed),
            "src_size": int(st.st_size), "src_mtime_ns": int(st.st_mtime_ns),
            "src_ino": int(st.st_ino)}


def _same_src(info, st):
    return (info.get("src_size"), info.get("src_mtime_ns"), info.get("src_ino")) == \
        (st.st_size, st.st_mtime_ns, st.st_ino)


def _info_ok(info):
    """A MARK this version wrote, with values it can resume from."""
    try:
        return (info.get("v") == MARK_V and isinstance(info.get("src"), str)
                and all(type(info.get(k)) is int and info[k] >= 0
                        for k in ("off", "records", "bad", "head_len"))
                and isinstance(info.get("head"), str) and info["head_len"] <= HEAD_BYTES
                and info["head_len"] <= info["off"])
    except AttributeError:
        return False


def tier_days(cfg, tier):
    """`feeds.<tier>.days` as the readers take it (`tail.FileTailSource`): the
    newest that many dated dirs, and 0 every one of them (`dated_dirs`)."""
    v = cfg.get("feeds.%s.days" % tier)
    try:
        return max(0, int(v))
    except (TypeError, ValueError):
        return 8 if tier == "ebpf" else 2


def narrow(path, mode):
    """`path` narrowed to `mode` when it is this user's and allows more: a dir
    or a file a version before this one made with the umask's mode. Never a
    symlink, whose chmod would reach the collector's file it points at."""
    try:
        st = os.lstat(path)
    except OSError:
        return
    if stat.S_ISLNK(st.st_mode) or st.st_uid != os.getuid() or not (st.st_mode & 0o777 & ~mode):
        return
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def private_dir(path):
    """`path` made 0700 (and narrowed to it, when it is wider)."""
    os.makedirs(path, mode=DIR_MODE, exist_ok=True)
    narrow(path, DIR_MODE)


def narrow_tree(top):
    """Every dir and file under `top` narrowed to the view's modes; links left."""
    for dp, dns, fns in os.walk(top):
        for d in dns:
            narrow(os.path.join(dp, d), DIR_MODE)
        for f in fns:
            narrow(os.path.join(dp, f), FILE_MODE)


def touch(path, st):
    """The view file's mtime is its source's: the feed report's "last written",
    and a collector's files, read as the collector wrote them."""
    try:
        os.utime(path, ns=(st.st_mtime_ns, st.st_mtime_ns))
    except OSError:
        pass


def _head_of(fd, n):
    return _digest(os.pread(fd, n, 0) if n else b"")


def writer_meta(src):
    """What a converted file's segments say of themselves, beside ebr's own."""
    return {"writer": WRITER, "host": host_of(src), "stream": split_name(src)[0],
            "source": os.path.basename(src)}


class Task(object):
    """A bulk conversion's surroundings: whom it tells its progress (in units
    of `work`: a plain source's bytes, a `.gz`'s compressed bytes), and what
    tells it to stop."""

    def __init__(self, progress=None, stop=None):
        self._progress = progress
        self._stop = stop

    def progress(self, n):
        if self._progress is not None and n:
            self._progress(n)

    def check(self):
        if self._stop is not None and self._stop():
            raise _Stopped()


def _pump_plain(fd, off, w, c, task, on_mark):
    """Convert plain JSONL file `fd`'s complete lines from `off` on. Returns
    `(offset past the last line consumed, stuck)`: stuck when a line longer
    than LINE_MAX stops it, as one stops the JSONL tail. `on_mark(off)` every
    BULK_MARK_BYTES, while no record is being passed over (`c.skip`)."""
    marked = off
    want = CHUNK
    while True:
        task.check()
        buf = os.pread(fd, want, off)
        if not buf:
            return off, False
        cut = buf.rfind(b"\n")
        if cut < 0:
            if len(buf) < want:
                return off, False       # a line still being written
            if want >= LINE_MAX:
                return off, True
            want = min(LINE_MAX, want * 4)
            continue
        want = CHUNK
        feed_lines(buf[:cut + 1], w, c)
        off += cut + 1
        task.progress(cut + 1)
        if off - marked >= BULK_MARK_BYTES and not c.skip:
            on_mark(off)
            marked = off


class _GzPump(object):
    """A `.gz` source inflated from its start: its first `off` bytes passed
    (their head checked against `head`), the complete lines after converted.
    It keeps the stream's first HEAD_BYTES, for the MARKs' digests."""

    def __init__(self, path, off=0, head_len=0, head=None):
        self.path = path
        self.off = off
        self.head_len = head_len
        self.head = head
        self.head_bytes = bytearray()
        self.size = 0                   # uncompressed bytes inflated
        self.st = None

    def _check_head(self):
        if self.head_len and _digest(bytes(self.head_bytes[:self.head_len])) != self.head:
            raise _Rebuild("%s starts with other bytes than the ones converted"
                           % os.path.basename(self.path))

    def head_at(self, off):
        n = min(HEAD_BYTES, off)
        return _digest(bytes(self.head_bytes[:n])), n

    def run(self, w, c, task, on_mark):
        """`(offset past the last line consumed, stuck)`, as `_pump_plain`."""
        off = self.off
        checked = not self.head_len
        carry = b""
        marked = off
        stuck = False
        with open(self.path, "rb") as raw:
            self.st = os.fstat(raw.fileno())
            with gzip.GzipFile(fileobj=raw, mode="rb") as fh:
                last = 0
                while True:
                    task.check()
                    try:
                        buf = fh.read1(CHUNK)
                    except EOFError:
                        buf = b""           # cut short: read as far as it goes
                    at = raw.tell()
                    task.progress(at - last)
                    last = at
                    if not buf:
                        break
                    start = self.size
                    self.size += len(buf)
                    if start < HEAD_BYTES:
                        self.head_bytes += buf[:HEAD_BYTES - start]
                    if self.size <= self.off:
                        continue
                    if not checked:
                        self._check_head()
                        checked = True
                    if start < self.off:
                        buf = buf[self.off - start:]
                    data = carry + buf if carry else buf
                    cut = data.rfind(b"\n")
                    if cut < 0:
                        if len(data) >= LINE_MAX:
                            stuck = True    # never complete: nothing past it is read
                            break
                        carry = data
                        continue
                    feed_lines(data[:cut + 1], w, c)
                    off += cut + 1
                    carry = data[cut + 1:]
                    if off - marked >= BULK_MARK_BYTES and not c.skip:
                        on_mark(off)
                        marked = off
        if self.size < self.off and not stuck:
            raise _Rebuild("%s holds %d bytes, fewer than the %d converted" % (
                os.path.basename(self.path), self.size, self.off))
        if not checked:
            self._check_head()
        return off, stuck


def convert(view, src, stem=None, zkw=None, task=None, fresh=False):
    """Bring converted file `view` up to date with its JSONL source `src` (plain
    or `.gz`), in bulk: resumed at its last MARK, or converted afresh (a new
    file, `os.replace`d over it) when it no longer describes its source. A
    MARK ends it whenever anything changed. Returns the file's state, a dict;
    raises _Stopped when told to stop, OSError and ebr.Error (a lock held)."""
    t0 = time.monotonic()
    task = task or Task()
    zkw = zkw or {}
    stem = stem if stem is not None else split_name(src)[0]
    why = None
    if not fresh:
        try:
            out = _resume(view, src, stem, zkw, task)
            out["elapsed_s"] = time.monotonic() - t0
            return out
        except _Rebuild as e:
            why = str(e)
    out = _fresh(view, src, zkw, task)
    out["rebuilt"] = why or "asked to"
    out["elapsed_s"] = time.monotonic() - t0
    return out


def _state(view, src, st, off, c, head, head_len, sealed, base, stuck):
    vst = os.stat(view)
    return {"src": src, "off": int(off), "records": int(c.records), "bad": int(c.bad),
            "head": head, "head_len": int(head_len), "sealed": bool(sealed),
            "src_id": [int(st.st_size), int(st.st_mtime_ns), int(st.st_ino)],
            "vsize": int(vst.st_size), "vino": int(vst.st_ino),
            "new_records": int(c.records - base[0]), "new_bad": int(c.bad - base[1]),
            "unwritable": int(c.unwritable), "rebuilt": None,
            "stuck": ("a line of %s longer than %d bytes at offset %d: as the JSONL tail, "
                      "nothing past it is read" % (os.path.basename(src), LINE_MAX, off)
                      if stuck else None)}


def _pump(src, off, head_len, head, w, c, task):
    """Convert `src` from `off` (its head checked first). Returns `(off, the
    source's stat, its head digest at off and the length it covers, sealed,
    stuck)`."""
    if src.endswith(_GZ):
        pump = _GzPump(src, off, head_len, head)

        def on_mark(at):
            h, n = pump.head_at(at)
            w.mark(mark_info(src, pump.st, at, c.records, c.bad, h, n, False))
            w.flush()
        new_off, stuck = pump.run(w, c, task, on_mark)
        h, n = pump.head_at(new_off)
        return new_off, pump.st, h, n, True, stuck
    fd = os.open(src, os.O_RDONLY)
    try:
        st = os.fstat(fd)
        if st.st_size < off:
            raise _Rebuild("%s is shorter (%d bytes) than the %d converted" % (
                os.path.basename(src), st.st_size, off))
        if head_len and _head_of(fd, head_len) != head:
            raise _Rebuild("%s starts with other bytes than the ones converted"
                           % os.path.basename(src))

        def on_mark(at):
            n = min(HEAD_BYTES, at)
            w.mark(mark_info(src, os.fstat(fd), at, c.records, c.bad, _head_of(fd, n), n,
                             False))
            w.flush()
        new_off, stuck = _pump_plain(fd, off, w, c, task, on_mark)
        st = os.fstat(fd)
        n = min(HEAD_BYTES, new_off)
        h = head if (n == head_len and head is not None) else _head_of(fd, n)
        return new_off, st, h, n, False, stuck
    finally:
        os.close(fd)


def _resume(view, src, stem, zkw, task):
    try:
        w = ebr.Writer.open_append(view, meta=writer_meta(src), lock_wait=LOCK_WAIT_S,
                                   mode=FILE_MODE, **zkw)
    except ebr.Busy:
        raise
    except ebr.Error as e:
        raise _Rebuild("the view file cannot be appended to: %s" % e)
    try:
        try:
            info, _end, k = ebr.last_mark(view)
        except (ebr.Error, OSError) as e:
            raise _Rebuild("its checkpoints cannot be read: %s" % e)
        if info is None:
            # no MARK yet: every record it holds is one of the source's first
            info = {"off": 0, "records": 0, "bad": 0, "head": _digest(b""), "head_len": 0,
                    "sealed": False}
            had = False
        else:
            had = True
            if not _info_ok(info):
                raise _Rebuild("its last checkpoint is not one this version wrote")
            if split_name(info["src"])[0] != stem:
                raise _Rebuild("its checkpoint names another stream (%s)" % info["src"])
            if not src.endswith(_GZ) and not k:
                # The file the checkpoint describes, the same size, written since:
                # rewritten in place (an append grows it), maybe with the same head.
                try:
                    st0 = os.stat(src)
                except OSError:
                    st0 = None
                if (st0 is not None and st0.st_ino == info.get("src_ino")
                        and st0.st_size == info.get("src_size")
                        and st0.st_mtime_ns != info.get("src_mtime_ns")):
                    raise _Rebuild("%s was written in place since its checkpoint"
                                   % os.path.basename(src))
        off = info["off"]
        base = (info["records"] + k, info["bad"])
        c = _Counts(records=base[0], bad=base[1], skip=k, cut=True)
        if src.endswith(_GZ) and had and info["sealed"] and not k:
            st = os.stat(src)
            if _same_src(info, st):
                # sealed, and its .gz is still that file: nothing to read
                w.close()
                return _state(view, src, st, off, c, info["head"], info["head_len"], True,
                              base, False)
        new_off, st, head, head_len, sealed, stuck = _pump(
            src, off, info["head_len"], info["head"], w, c, task)
        if c.skip:
            raise _Rebuild("%s holds fewer records past its checkpoint than the view does"
                           % os.path.basename(src))
        changed = (new_off != off or c.records != info["records"] or c.bad != info["bad"]
                   or sealed != bool(info["sealed"])
                   or (sealed and not _same_src(info, st)))
        if changed:
            w.mark(mark_info(src, st, new_off, c.records, c.bad, head, head_len, sealed))
        w.close()
    finally:
        if not w.closed:
            w.close()
    if changed:
        touch(view, st)
    return _state(view, src, st, new_off, c, head, head_len, sealed, base, stuck)


def _fresh(view, src, zkw, task):
    """`src` converted from its start into a new file, renamed over `view`:
    a new inode, which every reader takes as a file replaced."""
    d, name = os.path.split(view)
    tmp = os.path.join(d, ".%s.rebuild.%d" % (name, os.getpid()))
    try:
        os.unlink(tmp)
    except OSError:
        pass
    c = _Counts(cut=True)
    w = ebr.Writer(tmp, meta=writer_meta(src), mode=FILE_MODE, **zkw)
    try:
        off, st, head, head_len, sealed, stuck = _pump(src, 0, 0, None, w, c, task)
        if off or sealed:
            w.mark(mark_info(src, st, off, c.records, c.bad, head, head_len, sealed))
        w.close()
        touch(tmp, st)
        os.replace(tmp, view)
    except BaseException:
        w.close()
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return _state(view, src, st, off, c, head, head_len, sealed, (0, 0), stuck)


# ---------------------------------------------------------------- pool worker
_WORKER = {"progress": None, "stop": None, "parent": None}


def _worker_init(progress, stop, parent):
    _WORKER.update(progress=progress, stop=stop, parent=parent)


def run_task(task):
    """One bulk conversion in a pool worker: `task` names the view file, its
    source, its stem, the writer's zstd and its cell in the shared progress
    array. Never raises: a failure is returned as `{"error": ...}`."""
    arr, stop, parent = _WORKER["progress"], _WORKER["stop"], _WORKER["parent"]
    slot = task.get("slot")

    def progress(n):
        if arr is not None and slot is not None:
            arr[slot] += n

    def stopped():
        return (stop is not None and stop.is_set()) or \
            (parent is not None and os.getppid() != parent)
    return guarded(task, Task(progress, stopped))


def guarded(task, t):
    try:
        return convert(task["view"], task["src"], task.get("stem"), task.get("zkw"), t,
                       fresh=task.get("fresh", False))
    except _Stopped:
        return {"stopped": True}
    except Exception as e:
        return {"error": "%s: %s" % (type(e).__name__, e)}


# ----------------------------------------------------------------- the view
class _Root(object):
    """One source root and the view root mirroring it."""

    def __init__(self, tier, src, view, days):
        self.tier = tier
        self.src = src
        self.view = view
        self.days = days
        self.newest = None              # the newest dated dir seen in the source
        self.held = 0                   # streams held at the last full listing


class _Entry(object):
    """One stream of the view: a link, or a converted file and where its
    conversion stands (`loaded`: the fields below hold the file's state)."""

    def __init__(self, root, day, stem, mode, view):
        self.root, self.day, self.stem, self.mode = root, day, stem, mode
        self.view = view                # the entry's path in the view
        self.src = None                 # its source file now
        self.loaded = False
        self.off = self.records = self.bad = 0
        self.head, self.head_len = _digest(b""), 0
        self.sealed = False
        self.src_id = None              # (size, mtime_ns, ino) of the source read
        self.vsize = self.vino = None
        self.writer = None
        self.marked_at = 0.0            # monotonic: the last MARK written
        self.dirty = False              # lines consumed since the last MARK
        self.wrote_at = 0.0             # monotonic: the last write
        self.lag = 0
        self.job = None                 # a bulk conversion of it running apart
        self.retry_at = 0.0
        self.fails = 0
        self.error = None
        self.frozen = None              # why it is no longer converted
        self.unwritable = 0

    @property
    def src_dir(self):
        return os.path.join(self.root.src, self.day) if self.day else self.root.src

    @property
    def plain(self):
        return os.path.join(self.src_dir, self.stem + ".jsonl")


class EbrView(object):
    """The view of every tier the service reads. Made by `from_config`, the
    config pointed at it by `prepare`, filled by `catch_up`, kept current by
    `sync`; `status(tier)` is the tier's `convert` block in /api/feeds."""

    def __init__(self, cfg):
        self.cfg = cfg
        s = settings(cfg)
        self.enabled = s["enabled"]
        self.dir = s["dir"]
        self.level = s["zstd"]
        self.workers = s["workers"]
        self.zkw, self.zstd_note = zstd_kw(self.level)
        self.roots = []
        self.tiers = {}                 # tier -> {"sources", "views", "dir"}
        self.entries = {}               # (view dir, stem) -> _Entry
        self.state = "off"
        self.progress = {"pct": 0, "files": 0, "work": 0, "done": 0, "records": 0,
                         "elapsed_s": None, "workers": 0, "records_per_s": None}
        self.errors = collections.deque(maxlen=ERRORS_KEEP)
        self.hooks = {}                 # tests: "catch_up" -> called as it starts
        self._mu = threading.RLock()    # the entries map and the errors
        self._sync_mu = threading.Lock()    # one sync, catch-up or close at a time
        self._stop = threading.Event()
        self._pool_stop = None
        self._jobs = None               # live bulk conversions, one at a time
        self._full_at = None            # monotonic: the last full listing
        self._rot = 0

    # ----------------------------------------------------------- setting up
    @classmethod
    def from_config(cls, cfg):
        """A view of every tier with a source root that exists, or None: off,
        or nothing to mirror. ConvertError for a view that cannot be."""
        if not settings(cfg)["enabled"]:
            return None
        v = cls(cfg)
        v._plan_roots()
        return v if v.roots else None

    def _plan_roots(self):
        for tier in TIERS:
            srcs = []
            for r in self.cfg.get("feeds.%s.roots" % tier) or []:
                if r and os.path.isdir(r) and os.path.abspath(r) not in srcs:
                    srcs.append(os.path.abspath(r))
            if not srcs:
                continue
            days = tier_days(self.cfg, tier)
            base = os.path.join(self.dir, tier)
            views = []
            for src in srcs:
                if _inside(self.dir, src):
                    raise ConvertError(
                        "ingest.convert.dir %s is inside the %s source root %s, where the "
                        "view would be read as the collector's files. Set ingest.convert.dir "
                        "(RC_DASH_CONVERT_DIR) elsewhere, or turn conversion off "
                        "(RC_DASH_CONVERT=0)" % (self.dir, tier, src))
                if _inside(src, self.dir):
                    raise ConvertError("the %s source root %s is inside ingest.convert.dir %s"
                                       % (tier, src, self.dir))
                view = os.path.join(base, view_root_name(src))
                if view in views:
                    continue
                views.append(view)
                self.roots.append(_Root(tier, src, view, days))
            self.tiers[tier] = {"sources": [r.src for r in self.roots if r.tier == tier],
                                "views": views, "dir": base}

    def prepare(self):
        """Make every view root, and point the config's readers at them:
        `feeds.<tier>.roots` the view roots, `prefer` ebr, and the collector's
        roots kept as `feeds.<tier>.source_roots`. Before the Aggregator is
        made: everything it builds reads the config once."""
        if self.roots:
            private_dir(self.dir)
        for tier, t in self.tiers.items():
            private_dir(t["dir"])
        for r in self.roots:
            private_dir(r.view)
            # what a version before this one made with the umask's modes
            narrow_tree(r.view)
            marker = os.path.join(r.view, MARKER)
            real = os.path.realpath(r.src)
            try:
                with open(marker) as fh:
                    had = json.load(fh)
            except (OSError, ValueError):
                had = None
            if isinstance(had, dict) and had.get("realpath") not in (None, real):
                raise ConvertError("view root %s mirrors %s, not %s (%s): remove it, or set "
                                   "ingest.convert.dir elsewhere"
                                   % (r.view, had.get("realpath"), real, MARKER))
            if not isinstance(had, dict):
                tmp = marker + ".%d.tmp" % os.getpid()
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, FILE_MODE)
                with os.fdopen(fd, "w") as fh:
                    json.dump({"source": r.src, "realpath": real, "tier": r.tier,
                               "writer": WRITER, "created": time.time(),
                               "note": "the EBR view of the source root: links to its EBR "
                                       "day-files, EBR conversions of its JSONL ones "
                                       "(rc_dashboard/ebrview.py)"}, fh, indent=1)
                os.replace(tmp, marker)
        for tier, t in self.tiers.items():
            was = self.cfg.origin("feeds.%s.roots" % tier)
            self.cfg.set("feeds.%s.source_roots" % tier, list(t["sources"]), was)
            self.cfg.set("feeds.%s.roots" % tier, list(t["views"]),
                         "convert: the EBR view of %s" % was)
            self.cfg.set("feeds.%s.prefer" % tier, "ebr", "convert")
        # nothing is read before the catch-up has filled the view (`converting`)
        self.state = "catching_up"
        return self

    # ------------------------------------------------------------- listing
    def _note(self, msg, entry=None):
        with self._mu:
            self.errors.append({"at": time.time(), "file": entry.view if entry else None,
                                "error": msg})

    @staticmethod
    def _view_entries(vdir):
        """`{stem: (file or None, [links])}` of what the view holds in `vdir`:
        its converted files (`<stem>.ebr`, regular) and its links (`.ebr`,
        `.ebr.gz`, plain first). Anything else is not the view's."""
        out = {}
        try:
            names = sorted(os.listdir(vdir))
        except OSError:
            return out
        for n in names:
            if n.startswith("."):
                continue
            stem, fmt, z = split_name(n)
            if fmt != "ebr":
                continue
            p = os.path.join(vdir, n)
            f, links = out.get(stem, (None, []))
            if os.path.islink(p):
                links = sorted(links + [p], key=lambda x: x.endswith(_GZ))
            elif not z and os.path.isfile(p):
                f = p
            else:
                continue
            out[stem] = (f, links)
        return out

    @staticmethod
    def _streams(files, notes):
        """`{stem: (path read or None, {fmt: path})}` of one source dir."""
        out = {}
        for p in files:
            out[split_name(p)[0]] = (p, {format_of(p): p})
        for n in notes:
            out[n["stream"]] = (n["path"], dict(n["files"]))
        return out

    def _discover(self, full, now=None):
        """List the sources and bring the view's entries in line: new streams
        decided, links moved to their `.gz`, gone streams removed, days out of
        retention pruned. `full`: every day and flat file; else a day new since
        (which makes it a full one), or the newest day again."""
        for root in self.roots:
            try:
                self._discover_root(root, full, now)
            except Exception as e:      # one root's listing must not stop the others
                self._note("listing %s: %s: %s" % (root.src, type(e).__name__, e))

    def _discover_root(self, root, full, now):
        try:
            dates = sorted(d for d in os.listdir(root.src) if d.startswith("20")
                           and os.path.isdir(os.path.join(root.src, d)))
        except OSError:
            dates = []
        newest = dates[-1] if dates else None
        if not full and newest == root.newest:
            if newest is not None:
                files, notes = _listing(os.path.join(root.src, newest), "ebr", now)
                if files or any(n["path"] is None for n in notes):
                    self._reconcile(root, newest, self._streams(files, notes), create=True)
            return
        root.newest = newest
        notes = []
        kept = dated_dirs(root.src, root.days, "ebr", now, report=notes)
        by_day = collections.defaultdict(list)
        for n in notes:
            by_day[n.get("day")].append(n)
        held = 0
        for day, files in kept:
            streams = self._streams(files, by_day.get(day, ()))
            held += sum(1 for p, _c in streams.values() if p is None)
            self._reconcile(root, day, streams, create=True)
        flat, fnotes = _listing(root.src, "ebr", now)
        want = {split_name(p)[0] for p in flat[-root.days:]}
        streams = {k: v for k, v in self._streams(flat, fnotes).items()
                   if k in want or v[0] is None}
        held += sum(1 for p, _c in streams.values() if p is None)
        root.held = held
        self._reconcile(root, "", streams, create=True)
        # what the view holds and does not mirror any more
        oldest = kept[0][0] if kept else None
        days_kept = {d for d, _f in kept}
        try:
            vdays = [d for d in os.listdir(root.view) if d.startswith("20")
                     and os.path.isdir(os.path.join(root.view, d))]
        except OSError:
            vdays = []
        for vd in vdays:
            if vd in days_kept:
                continue
            if (oldest is not None and vd < oldest) or \
                    not os.path.isdir(os.path.join(root.src, vd)):
                self._prune_day(root, vd)
            else:
                files, notes = _listing(os.path.join(root.src, vd), "ebr", now)
                self._reconcile(root, vd, self._streams(files, notes), create=False)
        self._sweep_tmp(root, vdays)

    def _sweep_tmp(self, root, vdays):
        """A rebuild or a link cut short leaves its temp file: an hour on, it goes."""
        cutoff = time.time() - 3600
        for vd in [""] + list(vdays):
            d = os.path.join(root.view, vd) if vd else root.view
            try:
                names = os.listdir(d)
            except OSError:
                continue
            for n in names:
                if _TMP.match(n):
                    p = os.path.join(d, n)
                    try:
                        if os.lstat(p).st_mtime < cutoff:
                            os.unlink(p)
                    except OSError:
                        pass

    def _prune_day(self, root, day):
        vdir = os.path.join(root.view, day)
        for stem, (f, links) in self._view_entries(vdir).items():
            self._drop(root, day, stem, ([f] if f else []) + links)
        for n in os.listdir(vdir) if os.path.isdir(vdir) else ():
            if _TMP.match(n):
                try:
                    os.unlink(os.path.join(vdir, n))
                except OSError:
                    pass
        try:
            os.rmdir(vdir)
        except OSError:
            pass                        # something not the view's is left in it

    def _drop(self, root, day, stem, paths):
        vdir = os.path.join(root.view, day) if day else root.view
        with self._mu:
            e = self.entries.pop((vdir, stem), None)
        if e is not None:
            self._close(e, mark=False)
        for p in paths:
            try:
                os.unlink(p)
            except OSError:
                pass

    def _reconcile(self, root, day, streams, create):
        """One source dir's streams (`_streams`) against its view dir."""
        vdir = os.path.join(root.view, day) if day else root.view
        have = self._view_entries(vdir)
        for stem in [s for s in have if s not in streams]:
            f, links = have.pop(stem)
            self._drop(root, day, stem, ([f] if f else []) + links)     # its stream is gone
        for stem, (chosen, cands) in streams.items():
            got = have.get(stem)
            if got is not None:
                f, links = got
                if f is not None:
                    for p in links:     # a file decides: a link beside it is not the view's
                        self._unlink(p)
                    self._converted(root, day, stem, f, cands)
                    continue
                target = cands.get("ebr")
                if target is not None:
                    self._link(root, day, stem, links, target)
                    continue
                # the EBR file it linked to is gone: the stream is decided again
                self._drop(root, day, stem, links)
            if not create or chosen is None:
                continue                # held, or a day the view no longer adds to
            if format_of(chosen) == "ebr":
                self._link(root, day, stem, [], chosen)
            else:
                path = os.path.join(vdir, stem + ".ebr")
                private_dir(vdir)
                # the decision, kept: an empty file is a conversion still to come
                os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, FILE_MODE))
                self._converted(root, day, stem, path, cands)

    @staticmethod
    def _unlink(p):
        try:
            os.unlink(p)
        except OSError:
            pass

    def _link(self, root, day, stem, links, target):
        """The view's link to `target`, the stream's EBR file: made, or moved
        from a plain file to its `.gz` -- the new link first, as the collector
        writes the `.gz` before it removes the plain file."""
        vdir = os.path.join(root.view, day) if day else root.view
        want = os.path.join(vdir, os.path.basename(target))
        with self._mu:
            e = self.entries.get((vdir, stem))
            if e is None or e.mode != "link":
                e = self.entries[(vdir, stem)] = _Entry(root, day, stem, "link", want)
        e.src = target
        ok = False
        if want in links:
            try:
                ok = os.readlink(want) == target
            except OSError:
                ok = False
        if not ok:
            private_dir(vdir)
            tmp = os.path.join(vdir, ".%s.link.%d" % (os.path.basename(want), os.getpid()))
            self._unlink(tmp)
            os.symlink(target, tmp)
            os.replace(tmp, want)
        e.view = want
        for p in links:
            if p != want:
                self._unlink(p)
        return e

    def _converted(self, root, day, stem, path, cands):
        """A converted file of the view: its entry, and its source now."""
        vdir = os.path.dirname(path)
        with self._mu:
            e = self.entries.get((vdir, stem))
            if e is None or e.mode != "convert":
                e = self.entries[(vdir, stem)] = _Entry(root, day, stem, "convert", path)
        src = cands.get("jsonl")
        if src is None:
            if e.frozen is None:
                # Converting it from its EBR twin's end would be the tail's switch
                # (`tail._switched`); the view keeps the records it has instead.
                e.frozen = ("its JSONL source is gone and its EBR twin %s is not read: "
                            "the view keeps what was converted" % os.path.basename(
                                cands.get("ebr") or "?"))
                self._close(e, mark=True)
                self._note(e.frozen, e)
            return e
        e.frozen = None
        e.src = src
        return e

    # ------------------------------------------------------------ catch-up
    def catch_up(self, on_progress=None):
        """Convert everything behind, before anything reads: the full listing,
        then every converted file brought up to its source's end, `workers` at
        a time in processes (spawned), or in this one. Progress is in source
        bytes (a `.gz`'s compressed ones), told to `on_progress(progress)`."""
        with self._sync_mu:
            return self._catch_up(on_progress)

    def _catch_up(self, on_progress):
        t0 = time.monotonic()
        self.state = "catching_up"
        hook = self.hooks.get("catch_up")
        if hook is not None:
            hook(self)
        try:
            self._discover(full=True)
            self._full_at = time.monotonic()
            with self._mu:
                entries = [e for e in self.entries.values()
                           if e.mode == "convert" and e.frozen is None and e.src]
            tasks = []
            for e in entries:
                work = self._work(e)
                if work is not None:
                    tasks.append((work, e))
            tasks.sort(key=lambda t: -t[0])         # the biggest first: the pool ends sooner
            total = sum(w for w, _e in tasks)
            n = min(self.workers, len(tasks))
            self.progress = {"pct": 0 if total else 100, "files": len(tasks), "work": total,
                             "done": 0, "records": 0, "elapsed_s": 0.0, "workers": n,
                             "records_per_s": None}
            if on_progress is not None:
                on_progress(self.progress)
            if n > 1 and total >= POOL_MIN_BYTES:
                self._pool(tasks, total, n, on_progress, t0)
            elif tasks:
                self.progress["workers"] = 1
                self._serial(tasks, total, on_progress, t0)
            el = time.monotonic() - t0
            recs = self.progress["records"]
            self.progress.update(pct=100, done=total, elapsed_s=round(el, 3),
                                 records_per_s=(round(recs / el) if el > 0 and recs else None))
            self.state = "stopped" if self._stop.is_set() else "live"
        except Exception as e:
            self.state = "error"
            self._note("catch-up: %s: %s" % (type(e).__name__, e))
        if on_progress is not None:
            on_progress(self.progress)
        return self.progress

    def _work(self, e):
        """How much of `e`'s source a catch-up reads (bytes; a .gz's compressed
        ones), or None for nothing: a sealed file of the same .gz, or one whose
        last MARK is at its source's end. Those are taken as they are."""
        try:
            vst = os.stat(e.view)
            sst = os.stat(e.src)
        except OSError:
            return 0 if os.path.exists(e.src) else None
        if not vst.st_size:
            return sst.st_size
        try:
            if ebr.complete_end(e.view, vst.st_size) != vst.st_size:
                return 1                # a torn tail: the conversion's writer repairs it
            info, _end, k = ebr.last_mark(e.view)
        except (ebr.Error, OSError):
            return sst.st_size
        if info is None or not _info_ok(info) or k or \
                split_name(info["src"])[0] != e.stem:
            return sst.st_size
        if e.src.endswith(_GZ):
            if not (info["sealed"] and _same_src(info, sst)):
                return sst.st_size
        else:
            if info["sealed"] or info["off"] != sst.st_size:
                return max(1, sst.st_size - info["off"])
            try:
                fd = os.open(e.src, os.O_RDONLY)
                try:
                    if _head_of(fd, info["head_len"]) != info["head"]:
                        return sst.st_size
                finally:
                    os.close(fd)
            except OSError:
                return sst.st_size
        self._install(e, {"src": e.src, "off": info["off"], "records": info["records"],
                          "bad": info["bad"], "head": info["head"],
                          "head_len": info["head_len"], "sealed": info["sealed"],
                          "src_id": [sst.st_size, sst.st_mtime_ns, sst.st_ino],
                          "vsize": vst.st_size, "vino": vst.st_ino})
        return None

    def _task(self, e, slot=None, fresh=False):
        return {"view": e.view, "src": e.src, "stem": e.stem, "zkw": dict(self.zkw),
                "slot": slot, "fresh": fresh}

    def _serial(self, tasks, total, on_progress, t0):
        done = [0]

        def progress(nb):
            done[0] += nb
            self._tick(min(total, done[0]), total, on_progress, t0)
        for work, e in tasks:
            if self._stop.is_set():
                break
            base = done[0]
            got = guarded(self._task(e), Task(progress, self._stop.is_set))
            done[0] = base + work
            self._finish(e, got)
            self._tick(min(total, done[0]), total, on_progress, t0)

    def _pool(self, tasks, total, n, on_progress, t0):
        """`tasks` in `n` spawned processes, a task a file. A process that dies
        takes the pool with it: what is left is converted in this one."""
        ctx = multiprocessing.get_context("spawn")
        arr = ctx.Array("q", len(tasks), lock=False)
        stop = ctx.Event()
        try:
            ex = concurrent.futures.ProcessPoolExecutor(
                max_workers=n, mp_context=ctx, initializer=_worker_init,
                initargs=(arr, stop, os.getpid()))
        except (OSError, ValueError, ImportError) as e:
            self._note("conversion workers could not start (%s): converting in this process"
                       % e)
            return self._serial(tasks, total, on_progress, t0)
        self._pool_stop = stop
        works = [w for w, _e in tasks]
        finished, left, pending = set(), [], {}
        try:
            for i, (_work, e) in enumerate(tasks):
                pending[ex.submit(run_task, self._task(e, i))] = (i, e)
            while pending:
                if self._stop.is_set():
                    stop.set()
                done, _ = concurrent.futures.wait(list(pending), timeout=0.25)
                for f in done:
                    i, e = pending.pop(f)
                    finished.add(i)
                    try:
                        got = f.result()
                    except BrokenProcessPool:
                        left.append((works[i], e))
                        continue
                    except Exception as err:
                        got = {"error": "%s: %s" % (type(err).__name__, err)}
                    self._finish(e, got)
                prog = sum(works[i] if i in finished else min(arr[i], works[i])
                           for i in range(len(tasks)))
                self._tick(min(total, prog), total, on_progress, t0)
        finally:
            stop.set()
            ex.shutdown(wait=not self._stop.is_set(), cancel_futures=True)
            self._pool_stop = None
        if left and not self._stop.is_set():
            self._note("the conversion workers stopped with %d file(s) left: converting "
                       "them in this process" % len(left))
            self._serial(left, total, on_progress, t0)

    def _tick(self, done, total, on_progress, t0):
        p = self.progress
        p["done"] = done
        p["pct"] = min(99, int(100 * done / total)) if total else 99
        p["elapsed_s"] = round(time.monotonic() - t0, 3)
        if on_progress is not None:
            on_progress(p)

    def _finish(self, e, got):
        """A bulk conversion's result into its entry."""
        if got.get("stopped"):
            e.loaded = False
            return
        if got.get("error"):
            self._failed(e, got["error"])
            return
        if self.state == "catching_up":
            self.progress["records"] = self.progress.get("records", 0) + got["new_records"]
        if got.get("rebuilt"):
            self._note("converted afresh: %s" % got["rebuilt"], e)
        if got.get("unwritable"):
            self._note("%d record(s) EBR cannot hold, counted as bad lines"
                       % got["unwritable"], e)
        fails = e.fails
        self._install(e, got)
        if got.get("stuck"):
            e.fails = fails             # backing off further each time, as for a failure
            self._failed(e, got["stuck"], keep=True)

    def _install(self, e, st):
        e.src = st["src"]
        e.off, e.records, e.bad = st["off"], st["records"], st["bad"]
        e.head, e.head_len, e.sealed = st["head"], st["head_len"], st["sealed"]
        e.src_id = tuple(st["src_id"])
        e.vsize, e.vino = st["vsize"], st["vino"]
        e.loaded = True
        e.dirty = False
        e.marked_at = time.monotonic()
        e.fails = 0
        e.error = None
        e.lag = 0

    def _failed(self, e, err, keep=False):
        """`e` failed: said, and tried again later (sooner the first times).
        Unless `keep`, its state is read from the file again then."""
        if not keep:
            self._close(e, mark=False)
            e.loaded = False
        e.fails += 1
        e.error = err if isinstance(err, str) else "%s: %s" % (type(err).__name__, err)
        e.retry_at = time.monotonic() + RETRY_S[min(len(RETRY_S), e.fails) - 1]
        self._note(e.error, e)

    # ---------------------------------------------------------------- live
    def sync(self):
        """Before every poll: list what is new (the newest day every poll,
        everything every DISCOVER_S), convert what each live source gained
        since (bounded), relink what was sealed, prune. One file's failure is
        reported and retried later; it stops nothing else. Returns the source
        bytes converted."""
        if self._stop.is_set() or not self._sync_mu.acquire(blocking=False):
            return 0
        try:
            return self._sync()
        finally:
            self._sync_mu.release()

    def _sync(self):
        now = time.monotonic()
        self._reap()
        full = self._full_at is None or now - self._full_at >= DISCOVER_S
        self._discover(full=full)
        if full:
            self._full_at = now
        with self._mu:
            entries = list(self.entries.values())
        if entries:
            # a different file first each poll, so none waits on the budget for long
            self._rot = (self._rot + 1) % len(entries)
            entries = entries[self._rot:] + entries[:self._rot]
        budget = SYNC_POLL_BYTES
        n = 0
        for e in entries:
            if self._stop.is_set():
                break
            try:
                if e.mode == "link":
                    self._sync_link(e)
                elif budget > 0:
                    got = self._sync_convert(e, budget, now)
                    budget -= got
                    n += got
            except Exception as err:
                self._failed(e, err)
        return n

    def _sync_link(self, e):
        if e.src and not e.src.endswith(_GZ) and not os.path.exists(e.src) \
                and os.path.exists(e.src + _GZ):
            # sealed: the .gz link first, then the plain one goes
            self._link(e.root, e.day, e.stem, [e.view], e.src + _GZ)

    def _sync_convert(self, e, budget, now):
        if e.job is not None or e.frozen is not None or now < e.retry_at:
            return 0
        if not e.loaded:
            self._submit(e)
            return 0
        plain = e.plain
        try:
            st = os.stat(plain)
        except FileNotFoundError:
            st = None
        if st is None:
            gzp = plain + _GZ
            try:
                gst = os.stat(gzp)
            except FileNotFoundError:
                return 0                # gone: the listing decides what becomes of it
            e.src = gzp
            if not e.sealed or (gst.st_size, gst.st_mtime_ns, gst.st_ino) != e.src_id:
                self._submit(e)         # sealed by the collector: finished from the .gz
            else:
                self._idle(e, now)
            return 0
        e.src = plain
        if e.sealed or st.st_ino != e.src_id[2] or st.st_size < e.off:
            self._submit(e)             # back from its .gz, replaced, or cut short
            return 0
        behind = st.st_size - e.off
        if behind <= 0:
            if (st.st_size, st.st_mtime_ns) != tuple(e.src_id[:2]):
                # Written since, and grown past nothing it held: rewritten in
                # place, since an append always grows it. Its first bytes may
                # match the old ones, so it is converted afresh, into a new file.
                self._submit(e, fresh=True)
                return 0
            e.lag = 0
            self._idle(e, now)
            return 0
        if behind > JOB_BYTES:
            e.lag = behind
            self._submit(e)             # far behind: converted in bulk, apart
            return 0
        n = min(behind, SYNC_FILE_BYTES, budget)
        fd = os.open(plain, os.O_RDONLY)
        try:
            if os.fstat(fd).st_ino != e.src_id[2]:
                return 0
            # A file deleted and written again can get its inode number back:
            # the bytes converted must still be the ones it starts with.
            if e.head_len and _head_of(fd, e.head_len) != e.head:
                data = None
            else:
                data = os.pread(fd, n, e.off)
        finally:
            os.close(fd)
        if data is None:
            self._submit(e)             # another file: converted afresh
            return 0
        cut = data.rfind(b"\n")
        if cut < 0:
            e.lag = 0 if n == behind else behind
            self._idle(e, now)
            return 0
        w = self._writer(e)
        if w is None:
            return 0
        c = _Counts(records=e.records, bad=e.bad)
        feed_lines(data[:cut + 1], w, c)
        e.off += cut + 1
        e.records, e.bad = c.records, c.bad
        e.unwritable += c.unwritable
        e.dirty = True
        e.lag = 0 if n == behind else behind - (cut + 1)
        e.src_id = (st.st_size, st.st_mtime_ns, st.st_ino)
        if now - e.marked_at >= MARK_EVERY_S:
            self._queue_mark(e, st)     # with this batch: one write (its head checked above)
        e.vsize += w.flush()
        e.wrote_at = now
        touch(e.view, st)
        return cut + 1

    def _writer(self, e):
        """`e`'s writer, opened on the file as this view left it. None, and a
        bulk conversion to resume it from the file, when it is not."""
        if e.writer is not None:
            return e.writer
        try:
            vst = os.stat(e.view)
        except OSError:
            vst = None
        if vst is None or vst.st_ino != e.vino or vst.st_size != e.vsize:
            self._submit(e)
            return None
        w = ebr.Writer.open_append(e.view, meta=writer_meta(e.plain), lock_wait=0,
                                   mode=FILE_MODE, **self.zkw)
        if w.stats.get("repaired_bytes") or w.end != e.vsize:
            w.close()
            self._submit(e)
            return None
        e.writer = w
        return w

    def _queue_mark(self, e, st, check=False):
        """A MARK of where `e` stands, queued in its writer (the next flush
        writes it). Only for the source converted: `check` reads its first
        bytes again first. False when it is not that source any more."""
        n = min(HEAD_BYTES, e.off)
        if n != e.head_len or check:
            fd = os.open(e.plain, os.O_RDONLY)
            try:
                if check and (os.fstat(fd).st_size < e.off
                              or _head_of(fd, e.head_len) != e.head):
                    return False
                if n != e.head_len:
                    e.head, e.head_len = _head_of(fd, n), n
            finally:
                os.close(fd)
        e.writer.mark(mark_info(e.plain, st, e.off, e.records, e.bad, e.head, e.head_len,
                                False))
        e.marked_at = time.monotonic()
        e.dirty = False
        return True

    def _mark_now(self, e):
        """A MARK written now, if `e`'s source is still the one converted."""
        try:
            st = os.stat(e.plain)
        except OSError:
            return
        if st.st_ino != e.src_id[2] or not self._queue_mark(e, st, check=True):
            return
        e.vsize += e.writer.flush()

    def _idle(self, e, now):
        """No new line: a MARK once one is due, and the writer closed once the
        file has been quiet a while."""
        if e.writer is None:
            return
        if e.dirty and now - e.marked_at >= MARK_EVERY_S:
            self._mark_now(e)
        if now - e.wrote_at >= IDLE_CLOSE_S:
            self._close(e, mark=True)

    def _close(self, e, mark):
        """Close `e`'s writer, a MARK of where it stands written first (`mark`)."""
        w = e.writer
        if w is None:
            return
        try:
            if mark and e.dirty and e.loaded:
                self._mark_now(e)
            w.close()
            e.vsize = w.end
        except Exception as err:
            e.loaded = False
            self._note("closing %s: %s: %s" % (e.view, type(err).__name__, err), e)
            try:
                w.close()
            except Exception:
                pass
        finally:
            e.writer = None

    def _submit(self, e, fresh=False):
        """Convert `e` in bulk, apart from the polls: its writer closed (a MARK
        first), the conversion resumed from the file itself, or from the start
        into a new file (`fresh`)."""
        if e.job is not None or self._stop.is_set():
            return
        self._close(e, mark=True)
        if self._jobs is None:
            self._jobs = concurrent.futures.ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="ebrview-job")
        e.loaded = False
        e.job = self._jobs.submit(guarded, self._task(e, fresh=fresh),
                                   Task(None, self._stop.is_set))

    def _reap(self):
        """Every bulk conversion that has finished, into its entry."""
        with self._mu:
            busy = [e for e in self.entries.values() if e.job is not None]
        for e in busy:
            if e.job.done():
                f, e.job = e.job, None
                try:
                    got = f.result()
                except Exception as err:
                    got = {"error": "%s: %s" % (type(err).__name__, err)}
                self._finish(e, got)

    # ------------------------------------------------------------- closing
    def close(self):
        """Stop: no new conversion, every open file MARKed and closed."""
        self._stop.set()
        if self._pool_stop is not None:
            self._pool_stop.set()
        got = self._sync_mu.acquire(timeout=5)
        try:
            with self._mu:
                entries = list(self.entries.values())
            for e in entries:
                self._close(e, mark=True)
            if self._jobs is not None:
                self._jobs.shutdown(wait=False, cancel_futures=True)
        finally:
            if got:
                self._sync_mu.release()

    # -------------------------------------------------------------- report
    def converting(self):
        """Whether the catch-up is still running: the readers wait for it."""
        return self.state == "catching_up"

    def status(self, tier):
        """The `convert` block of the tier's entry in /api/feeds."""
        t = self.tiers.get(tier)
        if t is None:
            return status_off(self.cfg, tier)
        with self._mu:
            entries = [e for e in self.entries.values() if e.root.tier == tier]
            errors = list(self.errors)
        conv = [e for e in entries if e.mode == "convert"]
        files = []
        for e in sorted(entries, key=lambda x: x.view):
            f = {"source": e.src, "view": e.view, "mode": e.mode}
            if e.mode == "convert":
                f.update(records=e.records, bad=e.bad, lag=e.lag, sealed=e.sealed,
                         state=("frozen" if e.frozen else "converting" if e.job is not None
                                else "error" if e.error else "live" if e.loaded
                                else "pending"))
                if e.frozen:
                    f["note"] = e.frozen
                if e.error:
                    f["error"] = e.error
            files.append(f)
        views = tuple(v + os.sep for v in t["views"])
        mine = [x for x in errors if not x["file"] or x["file"].startswith(views)]
        p = dict(self.progress)
        return {
            "enabled": True, "state": self.state,
            "pct": 100 if self.state in ("live", "stopped") else p.get("pct", 0),
            "dir": t["dir"], "source_roots": list(t["sources"]), "view_roots": list(t["views"]),
            "zstd": self.zkw.get("zstd", 0), "zstd_note": self.zstd_note,
            "workers": self.workers,
            "counts": {"converted": len(conv), "linked": len(entries) - len(conv),
                       "held": sum(r.held for r in self.roots if r.tier == tier)},
            "records": sum(e.records for e in conv),
            "bad_lines": sum(e.bad for e in conv),
            "bytes_in": sum(e.off for e in conv),
            "bytes_out": sum(e.vsize or 0 for e in conv),
            "lag_bytes": sum(e.lag for e in conv),
            "catch_up": p,
            "errors": mine[-ERRORS_KEEP:],
            "files": files}


def status_off(cfg, tier, error=None):
    """The `convert` block of a tier the service reads as configured: off, or
    the view could not be made (`error`)."""
    if not settings(cfg)["enabled"]:
        return {"enabled": False, "state": "off",
                "reason": "ingest.convert.enabled is off (RC_DASH_CONVERT=0)"}
    if error:
        return {"enabled": True, "state": "error",
                "reason": "the view could not be made, so the day-files are read as they "
                          "are: %s" % error}
    return {"enabled": True, "state": "off", "reason": "no %s source root exists" % tier}


def describe(cfg, now=None):
    """`--check-feeds`: what conversion would do, a line a tier. Reads the
    sources and the view as they are, and writes nothing."""
    s = settings(cfg)
    if not s["enabled"]:
        return ["  convert   off (ingest.convert.enabled, RC_DASH_CONVERT): the day-files "
                "are read as they are"]
    out = []
    for tier in TIERS:
        srcs = [os.path.abspath(r) for r in (cfg.get("feeds.%s.roots" % tier) or [])
                if r and os.path.isdir(r)]
        if not srcs:
            continue
        days = tier_days(cfg, tier)
        n = {"convert": 0, "link": 0, "held": 0, "done": 0}
        todo = 0
        bad = []
        for src in srcs:
            if _inside(s["dir"], src):
                bad.append(src)
                continue
            view = os.path.join(s["dir"], tier, view_root_name(src))
            dirs = [(d, os.path.join(src, d)) for d, _f in dated_dirs(src, days, "ebr", now)]
            for day, d in dirs + [("", src)]:
                files, notes = _listing(d, "ebr", now)
                if not day:
                    files = files[-days:]
                n["held"] += sum(1 for x in notes if x["path"] is None)
                vdir = os.path.join(view, day) if day else view
                have = EbrView._view_entries(vdir)
                for p in files:
                    stem = split_name(p)[0]
                    f, links = have.get(stem, (None, []))
                    if links and f is None or (f is None and format_of(p) == "ebr"):
                        n["link"] += 1
                        continue
                    n["convert"] += 1
                    src_p = p if format_of(p) == "jsonl" else None
                    if src_p is None:
                        continue
                    size = os.path.getsize(src_p)
                    done = 0
                    if f is not None:
                        try:
                            info, _e, k = ebr.last_mark(f)
                        except (OSError, ebr.Error):
                            info, k = None, 0
                        if info and _info_ok(info) and not k:
                            done = size if (info["sealed"] and src_p.endswith(_GZ)) \
                                else info["off"]
                    if done >= size:
                        n["done"] += 1
                    else:
                        todo += size - done
        line = ("  convert %-9s on: %d jsonl stream(s) read as EBR (%d up to date, %.1f MB to "
                "convert), %d ebr stream(s) linked, %d held; view %s"
                % (tier, n["convert"], n["done"], todo / 1e6, n["link"], n["held"],
                   os.path.join(s["dir"], tier)))
        out.append(line)
        for src in bad:
            out.append("  ! convert %s: ingest.convert.dir %s is inside the source root %s: "
                       "the service refuses to start" % (tier, s["dir"], src))
    return out or ["  convert   on, but neither tier has a source root that exists"]
