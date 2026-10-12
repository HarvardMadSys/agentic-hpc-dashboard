"""Read a gzip-compressed day-file as if it were the plain JSONL inside it.

Every reader in this package addresses a day-file by byte offset: a window build
binary-searches for its first in-window byte (`export.seek_to_epoch`), the
read-back walks a file from its end, a saved window resumes from the offset its
scan reached, and the tail reads on from where it stopped. A `.jsonl.gz` -- the
form a feed captured on another cluster arrives in -- supports none of that by
itself: gzip is a stream, and reaching byte N means inflating every byte before
it.

So the first open of a compressed file inflates it once and keeps an index:
every `SPAN` bytes of output, a copy of the inflater's state
(`zlib.Decompress.copy`, its 32 KB window included) and the compressed offset
it had read to. Reaching any offset then costs inflating at most one span from
the checkpoint before it, and the reader keeps the span it last inflated, so a
forward or a backward walk inflates each span once. Offsets everywhere are into
the UNCOMPRESSED stream: a byte range planned against a compressed file means
what it means for a plain one, and a saved window resumes the same way.

What it costs, measured on boslogin06's 2026-10-08 exits file (66 MB -> 1.49 GB):

* indexing inflates the whole file once, ~1.1 s here (~1.3 GB of output a
  second), the first time anything opens it;
* the index holds ~40 KB per checkpoint, about 0.5% of the uncompressed size at
  an 8 MB span, and lives only in this process -- an inflater's state cannot be
  pickled -- so a restart indexes again;
* each open reader holds the two spans it last inflated, 2 x `SPAN` bytes.

A compressed file is taken to be complete. One that changes (size, mtime or
inode) is indexed again from the start, and a stream that stops without its
gzip trailer -- still being copied, or cut short -- is read as far as it goes,
which the line readers already treat as a last line still being written.
"""
import bisect
import collections
import io
import os
import threading
import time
import zlib

SPAN = 8 << 20            # uncompressed bytes between checkpoints
_STEP = 4 << 20           # most output one inflate call may produce
_CHUNK = 1 << 18          # compressed bytes read at a time
_GZIP = 16 + zlib.MAX_WBITS
_MAGIC = b"\x1f\x8b"
_KEEP = 256               # indexes held, least recently used dropped first

SUFFIX = ".gz"


def is_gz(path):
    return str(path).endswith(SUFFIX)


def plain_name(path):
    """`x.jsonl.gz` -> `x.jsonl`; anything else unchanged."""
    return path[:-len(SUFFIX)] if is_gz(path) else path


def _inflate(fh, d, coff):
    """Inflate from compressed offset `coff` with inflater `d`.

    Yields `(out, coff, d)` after every call: the output, the offset of the
    first compressed byte not yet consumed, and the inflater that continues
    from there -- together a point the stream can be resumed at exactly.
    Concatenated members are followed through; anything after the last member
    that is not one is reported, not inflated.
    """
    fh.seek(coff)
    pending, eof_in = b"", False
    while True:
        if len(pending) < 2 and not eof_in:
            more = fh.read(_CHUNK)
            if more:
                pending += more
            else:
                eof_in = True
        out = d.decompress(pending, _STEP)
        # At the end of a member the leftover is `unused_data`; CPython can
        # leave the same bytes in `unconsumed_tail` too, so never add the two.
        left = len(d.unused_data) if d.eof else len(d.unconsumed_tail)
        coff += len(pending) - left
        if d.eof:
            pending = d.unused_data
            while len(pending) < 2:
                more = fh.read(_CHUNK)
                if not more:
                    break
                pending += more
            if not pending:
                yield out, coff, None
                return                              # clean end of the last member
            if pending[:2] != _MAGIC:
                yield out, coff, None
                raise _Trailing(len(pending) + max(0, _size(fh) - fh.tell()))
            d = zlib.decompressobj(_GZIP)           # the next member
            yield out, coff, d
            continue
        pending = d.unconsumed_tail
        if not out and not pending and eof_in:
            raise _Truncated()
        yield out, coff, d


class _Trailing(Exception):
    pass


class _Truncated(Exception):
    pass


def _size(fh):
    return os.fstat(fh.fileno()).st_size


class Index:
    """Where to resume inflating for every `SPAN` of one file's output."""

    def __init__(self, ident):
        self.ident = ident        # (dev, ino, compressed size, mtime_ns)
        self.offs = []            # uncompressed offset of each checkpoint
        self.points = []          # (compressed offset, pristine inflater)
        self.size = 0             # uncompressed bytes
        self.members = 0
        self.note = None
        self.build_s = 0.0

    @classmethod
    def build(cls, fh, ident):
        idx = cls(ident)
        t0 = time.monotonic()
        d = zlib.decompressobj(_GZIP)
        idx.offs.append(0)
        idx.points.append((0, d.copy()))
        size, due, members = 0, SPAN, 1
        try:
            for out, coff, nd in _inflate(fh, d, 0):
                size += len(out)
                if nd is None:
                    continue                        # the end; the stream says how
                if nd is not d:
                    members += 1
                    d = nd
                if size >= due:
                    idx.offs.append(size)
                    idx.points.append((coff, d.copy()))
                    due = size + SPAN
        except _Trailing as e:
            idx.note = ("%d byte(s) after the last gzip member are not gzip data "
                        "and were ignored" % e.args[0])
        except _Truncated:
            idx.note = ("the gzip stream ends without its trailer (still being "
                        "written, or cut short); read as far as it goes")
        except zlib.error as e:
            idx.note = "corrupt from about compressed byte %d: %s" % (
                idx.points[-1][0], e)
        idx.size, idx.members = size, members
        idx.build_s = time.monotonic() - t0
        return idx

    def span(self, fh, i):
        """The inflated bytes from checkpoint `i` to the next one (or the end)."""
        start = self.offs[i]
        want = (self.offs[i + 1] if i + 1 < len(self.offs) else self.size) - start
        coff, d0 = self.points[i]
        parts, got = [], 0
        try:
            for out, _coff, nd in _inflate(fh, d0.copy(), coff):
                parts.append(out)
                got += len(out)
                if got >= want or nd is None:
                    break
        except (_Trailing, _Truncated, zlib.error):
            pass                                    # the index already stopped there
        data = b"".join(parts)
        return data[:want] if len(data) > want else data

    def nbytes(self):
        """An estimate of what the checkpoints hold: an inflater is ~40 KB."""
        return 40 * 1024 * len(self.points)


_lock = threading.Lock()
_indexes = collections.OrderedDict()       # path -> Index
_building = {}                             # path -> Lock, one build per file


def _ident(st):
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)


def index_for(fh, path):
    """The index for the open compressed file `fh`, building it once."""
    ident = _ident(os.fstat(fh.fileno()))
    with _lock:
        idx = _indexes.get(path)
        if idx is not None and idx.ident == ident:
            _indexes.move_to_end(path)
            return idx
        gate = _building.setdefault(path, threading.Lock())
    with gate:
        with _lock:
            idx = _indexes.get(path)
            if idx is not None and idx.ident == ident:
                return idx
        idx = Index.build(fh, ident)
        with _lock:
            _indexes[path] = idx
            _indexes.move_to_end(path)
            while len(_indexes) > _KEEP:
                _indexes.popitem(last=False)
        return idx


class GzipRaw(io.RawIOBase):
    """A compressed file read through its index: seekable, by uncompressed offset."""

    def __init__(self, path):
        super().__init__()
        self._fh = open(path, "rb")
        try:
            self.index = index_for(self._fh, path)
        except BaseException:
            self._fh.close()
            raise
        self._pos = 0
        self._span_at = -1
        self._span = memoryview(b"")
        self._prev = (-1, memoryview(b""))     # the span before: a read that
                                               # straddles two must not inflate
                                               # each of them twice

    def readable(self):
        return True

    def seekable(self):
        return True

    def fileno(self):
        return self._fh.fileno()

    @property
    def size(self):
        return self.index.size

    def tell(self):
        return self._pos

    def seek(self, offset, whence=io.SEEK_SET):
        if whence == io.SEEK_SET:
            pos = offset
        elif whence == io.SEEK_CUR:
            pos = self._pos + offset
        elif whence == io.SEEK_END:
            pos = self.index.size + offset
        else:
            raise ValueError("invalid whence (%r)" % whence)
        if pos < 0:
            raise ValueError("negative seek position %d" % pos)
        self._pos = pos
        return pos

    def readinto(self, b):
        pos = self._pos
        if pos >= self.index.size:
            return 0
        if not (self._span_at <= pos < self._span_at + len(self._span)):
            i = bisect.bisect_right(self.index.offs, pos) - 1
            at = self.index.offs[i]
            prev = (self._span_at, self._span)
            if self._prev[0] == at:
                self._span_at, self._span = self._prev
            else:
                self._span_at, self._span = at, memoryview(self.index.span(self._fh, i))
            self._prev = prev
            if not (self._span_at <= pos < self._span_at + len(self._span)):
                return 0                            # past what the stream holds
        k = pos - self._span_at
        n = min(len(b), len(self._span) - k)
        b[:n] = self._span[k:k + n]
        self._pos = pos + n
        return n

    def close(self):
        if not self.closed:
            self._span = memoryview(b"")
            self._prev = (-1, memoryview(b""))
            self._fh.close()
        super().close()


def open_feed(path, buffering=1 << 20):
    """A day-file opened for binary reading, compressed or not.

    A compressed one reads, seeks and tells by uncompressed offset; `fileno()`
    is still the file on disk, so its device and inode are what they are.
    """
    if is_gz(path):
        return io.BufferedReader(GzipRaw(path), buffer_size=buffering)
    return open(path, "rb")


def open_text(path, errors="replace"):
    if is_gz(path):
        return io.TextIOWrapper(open_feed(path), errors=errors)
    return open(path, errors=errors)


class Stat(tuple):
    """`os.stat_result`'s four fields the readers use, with the size of what the
    file HOLDS: a compressed file's offsets are into its uncompressed stream, so
    its size must be too."""
    __slots__ = ()

    def __new__(cls, st, size):
        return tuple.__new__(cls, (st.st_dev, st.st_ino, size, st.st_mtime))

    st_dev = property(lambda s: s[0])
    st_ino = property(lambda s: s[1])
    st_size = property(lambda s: s[2])
    st_mtime = property(lambda s: s[3])


def fstat(fh):
    """`os.fstat` for a file `open_feed` returned."""
    st = os.fstat(fh.fileno())
    raw = getattr(fh, "raw", None)
    if isinstance(raw, GzipRaw):
        return Stat(st, raw.size)
    return st


def stat(path):
    """`os.stat` by path, with a compressed file's uncompressed size."""
    if not is_gz(path):
        return os.stat(path)
    with open_feed(path) as fh:
        return fstat(fh)


def status():
    """What the process holds, for /api/feeds."""
    with _lock:
        idx = list(_indexes.items())
    return {"files": len(idx),
            "uncompressed_bytes": sum(i.size for _, i in idx),
            "index_bytes": sum(i.nbytes() for _, i in idx),
            "index_s": round(sum(i.build_s for _, i in idx), 2),
            "span_bytes": SPAN,
            "notes": {p: i.note for p, i in idx if i.note}}
