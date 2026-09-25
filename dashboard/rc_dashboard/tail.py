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
"""
import glob
import heapq
import json
import os

from .export import iter_lines_reverse, seek_to_epoch
from .normalize import epoch_of

# The most one poll reads from one file.  A tail that has fallen behind -- or a
# file first seen after startup, read from byte 0 -- catches up over several
# polls instead of pulling a whole day-file into memory at once.
READ_CAP = 64 << 20


class IngestSource:
    """Anything that can hand the aggregator a batch of raw records."""

    def poll(self):
        raise NotImplementedError

    def close(self):
        pass


def complete_end(fh, size, block=65536):
    """Offset just past the last newline in the first `size` bytes, else 0."""
    pos = size
    while pos > 0:
        n = min(block, pos)
        pos -= n
        fh.seek(pos)
        cut = fh.read(n).rfind(b"\n")
        if cut != -1:
            return pos + cut + 1
    return 0


class FileTailSource(IngestSource):
    def __init__(self, roots, days=2):
        self.roots = list(roots)
        self.days = days
        self.state = {}          # path -> offset record; this process only
        self.stats = {"files": 0, "bytes_read": 0, "partial_holds": 0,
                      "parse_errors": 0, "resets": 0}

    # ------------------------------------------------------------------ state
    def prune_state(self):
        """Forget files that have left the date window, so the map stays small."""
        keep = set(self.files())
        self.state = {k: v for k, v in self.state.items() if k in keep}

    def position(self, since):
        """Hand every current file to the tail at its end; return what lies behind.

        Each file's offset becomes the end of its last complete line.  The
        return value is the `[start, end)` byte range of every file holding
        records from epoch `since` onward -- the history `read_back` fills the
        live tier from.  `since=None` plans nothing: the tail starts at the end
        and fills forward.
        """
        plan = []
        for p in self.files():
            try:
                fh = open(p, "rb")
            except OSError:
                continue
            with fh:
                try:
                    st = os.fstat(fh.fileno())
                    end = complete_end(fh, st.st_size)
                    start = end if since is None else seek_to_epoch(fh, since, end)
                except OSError:
                    continue
            self._remember(p, st, end, None)
            if start < end:
                plan.append({"path": p, "start": start, "end": end})
        return plan

    # ------------------------------------------------------------------ files
    def files(self):
        out = []
        for root in self.roots:
            if not os.path.isdir(root):
                continue
            dates = sorted(d for d in os.listdir(root)
                           if d.startswith("20") and os.path.isdir(os.path.join(root, d)))
            for d in dates[-self.days:]:
                out += sorted(glob.glob(os.path.join(root, d, "*.jsonl")))
            out += sorted(glob.glob(os.path.join(root, "*.jsonl")))[-self.days:]
        return sorted(set(out))

    # ------------------------------------------------------------------- read
    def _read_one(self, path):
        try:
            fh = open(path, "rb")
        except OSError:
            return
        with fh:
            try:
                st = os.fstat(fh.fileno())
            except OSError:
                return
            rec = self.state.get(path)
            offset = 0
            if rec and rec.get("dev") == st.st_dev and rec.get("inode") == st.st_ino:
                offset = rec.get("offset", 0)
                if st.st_size < offset:
                    offset = 0                     # truncated or rewritten
                    self.stats["resets"] += 1
            elif rec:
                self.stats["resets"] += 1          # replaced: different inode
            if offset >= st.st_size:
                self._remember(path, st, offset, rec)
                return
            fh.seek(offset)
            data = fh.read(min(st.st_size - offset, READ_CAP))

        cut = data.rfind(b"\n")
        if cut == -1:
            self.stats["partial_holds"] += 1       # no complete line yet; re-read later
            return
        complete, consumed = data[:cut + 1], cut + 1
        self.stats["bytes_read"] += consumed

        for line in complete.split(b"\n"):
            if not line.strip():
                continue
            try:
                yield json.loads(line)
            except ValueError:
                self.stats["parse_errors"] += 1

        st2 = os.stat(path)
        self._remember(path, st2, offset + consumed, self.state.get(path))

    def _remember(self, path, st, offset, prev):
        self.state[path] = {"dev": st.st_dev, "inode": st.st_ino,
                            "size": st.st_size, "offset": offset,
                            "mtime": st.st_mtime}

    def poll(self):
        """Yield every complete record appended since the last call."""
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
                       key=lambda kr: kr[0], reverse=True)


def _read_back_one(entry, stats):
    last = float("inf")
    for line in iter_lines_reverse(entry["path"], start=entry["start"],
                                   end=entry["end"]):
        stats["bytes"] += len(line) + 1
        try:
            raw = json.loads(line)
        except ValueError:
            stats["parse_errors"] += 1
            continue
        ts = epoch_of(raw)
        if ts is not None:
            last = ts
        yield last, raw
