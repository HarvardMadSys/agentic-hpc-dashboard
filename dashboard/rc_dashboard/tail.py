"""Follow the collector's append-only JSONL, and read back the history behind it.

`IngestSource` is the seam: today the only implementation follows files, but a
`PushSource` fed by an HTTP/UDP sink in the collector implements the same
`poll()` and the reducers never learn which one fed them.

Where the tail STARTS is decided from the collector's files alone, on every
start.  `position()` hands each file to the tail at its current end and returns
the byte range holding the history behind that point, found by timestamp
(`export.seek_to_epoch`); `read_back()` reads that range newest first.  Offsets
are never persisted.  They once were, and the aggregates they fed were not: a
restarted service resumed at EOF with empty bins, so the page held only what
the collector wrote after the service came back -- and `run.sh` restarts it on
every save.  What the page can show must not depend on whether the service
happened to be running while the collector wrote.

Two correctness rules, both from how the collector writes:

* `DailyWriter.write()` (collector/ebpf_trace.py) buffers `json + '\n'` and
  flushes once per poll round, which is NOT atomic against a concurrent reader.
  So we read to EOF, find the last newline, yield only complete lines, and
  advance the stored offset only that far.  Trailing bytes are re-read next
  cycle: idempotent, never a truncated JSON line, never a lost record.  The
  hand-off obeys the same rule: a file is positioned after its last COMPLETE
  line, so a line still being written belongs to the tail, never the read-back.

* Offsets are keyed on `(st_dev, st_ino)`, not path, so a recreated file is
  detected as a new inode rather than silently seeking past its EOF.  A file
  first seen after startup -- a new date directory, a new host -- is new to this
  process and is read from byte 0.  Hosts roll over independently, so several
  date dirs stay in the window.

  The one exception is a day the collector compresses at its rollover
  (`_compress_day`): `x.jsonl` becomes `x.jsonl.gz`, a new name and a new inode
  holding the same bytes.  Offsets into a `.gz` count its uncompressed bytes
  (`gz.open_feed`), so the `.gz` takes up where the plain file was left -- read
  from byte 0, the whole day would be counted twice.

"""
import concurrent.futures
import glob
import heapq
import json
import os
from operator import itemgetter

from . import gz, fastjson
from .feedfile import (complete_end, dated_dirs, feed_files, iter_lines_reverse,
                       seek_to_epoch)
from .normalize import epoch_of

# The most one poll reads from one file.  A tail that has fallen behind -- or a
# file first seen after startup, read from byte 0 -- catches up over several
# polls instead of pulling a whole day-file into memory at once.
READ_CAP = 64 << 20

# Files positioned at once at start. Positioning a sealed `.gz` inflates all of it
# to index it (`gz.Index.build`): minutes for a week of a busy fleet, one file after
# another. zlib lets go of the GIL while it inflates, and the index is built once
# per file whoever asks (`gz.index_for`), so files are positioned side by side --
# a few at a time: each open reader holds two inflated spans (`gz.SPAN`).
POSITION_WORKERS = max(1, min(4, os.cpu_count() or 1))


def _disk(st):
    """A file's identity on disk: changes whenever it is appended to or replaced."""
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)


class IngestSource:
    """Anything that can hand the aggregator a batch of raw records."""

    def poll(self):
        raise NotImplementedError

    def close(self):
        pass


class FileTailSource(IngestSource):
    def __init__(self, roots, days=2):
        self.roots = list(roots)
        self.days = days
        self.state = {}          # path -> offset record; this process only
        self.stats = {"files": 0, "bytes_read": 0, "partial_holds": 0,
                      "parse_errors": 0, "resets": 0}
        self.restarted = []      # files gone back to the start of (`take_restarted`)

    # ------------------------------------------------------------------ state
    def prune_state(self):
        """Forget files that have left the date window, so the map stays small.

        A plain file compressed since the last poll has left the listing too,
        but its offset is where its `.gz` takes up: it is kept until the tail
        has met the `.gz`.
        """
        keep = set(self.files())

        def kept(k):
            return k in keep or (k + gz.SUFFIX in keep and k + gz.SUFFIX not in self.state)
        self.state = {k: v for k, v in self.state.items() if kept(k)}

    def position(self, since):
        """Hand every current file to the tail at its end; return what lies behind.

        Each file's offset becomes the end of its last complete line.  The
        return value is the `[start, end)` byte range of every file holding
        records from epoch `since` onward -- the history `read_back` fills the
        live tier from.  `since=None` plans nothing: the tail starts at the end
        and fills forward.
        """
        def one(p):
            try:
                fh = gz.open_feed(p)
            except OSError:
                return None
            with fh:
                try:
                    st = gz.fstat(fh)
                    disk = _disk(os.fstat(fh.fileno()))
                    end = complete_end(fh, st.st_size)
                    start = end if since is None else seek_to_epoch(fh, since, end)
                except OSError:
                    return None
            return p, st, disk, end, start

        def guarded(p):
            try:
                return one(p)
            except Exception as e:      # raised below, in file order
                return e

        files = self.files()
        if POSITION_WORKERS > 1 and len(files) > 1:
            with concurrent.futures.ThreadPoolExecutor(
                    min(POSITION_WORKERS, len(files)), thread_name_prefix="position") as ex:
                done = list(ex.map(guarded, files))
        else:
            done = [guarded(p) for p in files]
        plan = []
        # In file order, as one pass would: a file that failed stops it there,
        # with every file before it handed to the tail and none after.
        for got in done:
            if isinstance(got, Exception):
                raise got
            if got is None:
                continue
            p, st, disk, end, start = got
            self._remember(p, st, end, disk)
            if start < end:
                plan.append({"path": p, "start": start, "end": end, "since": since})
        return plan

    # ------------------------------------------------------------------ files
    def files(self):
        out = []
        for root in self.roots:
            for _d, files in dated_dirs(root, self.days):
                out += files
            out += feed_files(root)[-self.days:]
        return sorted(set(out))

    # ------------------------------------------------------------------- read
    def _read_one(self, path):
        # A file the tail has read to its end and that has not changed on disk
        # since is not opened: a sealed `.gz` would otherwise be opened and its
        # index looked up every poll, for nothing.
        held = self.state.get(path)
        if held is not None and held.get("disk") is not None \
                and held.get("offset", 0) >= held.get("size", 0):
            try:
                if _disk(os.stat(path)) == held["disk"]:
                    return
            except OSError:
                pass
        try:
            fh = gz.open_feed(path)
        except OSError:
            return
        with fh:
            try:
                st = gz.fstat(fh)
                disk = _disk(os.fstat(fh.fileno()))
            except OSError:
                return
            rec = self.state.get(path)
            if rec is None and gz.is_gz(path):
                rec = self._compressed(path, st)
            offset = 0
            if rec and rec.get("dev") == st.st_dev and rec.get("inode") == st.st_ino:
                offset = rec.get("offset", 0)
                if st.st_size < offset:
                    offset = 0                     # truncated or rewritten
                    self.stats["resets"] += 1
                    self.restarted.append(path)
            elif rec:
                self.stats["resets"] += 1          # replaced: different inode
            if offset >= st.st_size:
                self._remember(path, st, offset, disk)
                return
            fh.seek(offset)
            data = fh.read(min(st.st_size - offset, READ_CAP))

        cut = data.rfind(b"\n")
        if cut == -1:
            self.stats["partial_holds"] += 1       # no complete line yet; re-read later
            return
        complete, consumed = data[:cut + 1], cut + 1
        self.stats["bytes_read"] += consumed

        # Each record with where it was read: the file, and the offset just
        # past its line. A view built while this tail was behind in a file
        # tells by these a line its build already read from one it never saw
        # (`windows.PanelView.feed`).
        src = (path, st.st_dev, st.st_ino)
        end = offset
        for line in complete.split(b"\n"):
            end += len(line) + 1
            if not line.strip():
                continue
            try:
                raw = fastjson.record(line)
            except ValueError:
                self.stats["parse_errors"] += 1
                continue
            yield src, end, raw

        # The inode that was read, not the path's now: a day compressed since
        # has no plain file left to stat, and its `.gz` takes up from here.
        self._remember(path, st, offset + consumed, disk)

    def take_restarted(self):
        """The files this tail went back to the start of since the last call:
        each was cut short in place, its inode the same, so what it holds from
        its start now is not what anyone read of it before
        (`windows.PanelView.restarted`)."""
        out, self.restarted = self.restarted, []
        return out

    def _compressed(self, path, st):
        """The offset record of `.gz` file `path` if this tail followed it under
        its plain name, moved to the `.gz`; else None."""
        twin = self.state.get(gz.plain_name(path))
        if twin is None:
            return None
        rec = self.state[path] = dict(twin, dev=st.st_dev, inode=st.st_ino)
        return rec

    def _remember(self, path, st, offset, disk=None):
        # `disk` is the file as it was when it was read: a later append changes it,
        # and only then is the file opened again (`_read_one`).
        self.state[path] = {"dev": st.st_dev, "inode": st.st_ino, "size": st.st_size,
                            "offset": offset, "mtime": st.st_mtime, "disk": disk}

    def poll(self):
        """Yield every complete record appended since the last call."""
        for _src, _end, raw in self.poll_at():
            yield raw

    def poll_at(self):
        """`poll`, each record as `(src, end, raw)`: `src` the file it was read
        from, `(path, st_dev, st_ino)`, and `end` the offset just past its line
        (uncompressed, in a `.gz`)."""
        paths = self.files()
        self.stats["files"] = len(paths)
        for p in paths:
            yield from self._read_one(p)


def read_back(plan, stats):
    """Every record in `plan`, NEWEST FIRST, k-way merged across files.

    Newest first so the live tier fills from now backwards: the recent bins --
    the ones a reader is looking at -- land in the first seconds, and the filled
    span stays contiguous the whole time, so `retained_from` remains a true
    statement of how far back the chart reaches while the read is running.

    A day-file is appended in time order, so each file read backwards is
    descending and `heapq.merge` interleaves hosts correctly; concatenating
    would put all of one host's day ahead of another's.  Yields `(epoch, raw)`;
    a record with no timestamp keeps its place in its file.
    """
    return heapq.merge(*[_read_back_one(e, stats) for e in plan],
                       key=itemgetter(0), reverse=True)


def _read_back_one(entry, stats):
    last = float("inf")
    path = entry["path"]
    # Compressed since it was positioned: the range holds in the `.gz` as well.
    if not gz.is_gz(path) and not os.path.exists(path) \
            and os.path.exists(path + gz.SUFFIX):
        path += gz.SUFFIX
    for line in iter_lines_reverse(path, start=entry["start"], end=entry["end"]):
        stats["bytes"] += len(line) + 1
        try:
            raw = fastjson.record(line)
        except ValueError:
            stats["parse_errors"] += 1
            continue
        ts = epoch_of(raw)
        if ts is not None:
            last = ts
        yield last, raw
