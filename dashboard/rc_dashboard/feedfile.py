"""The collector's day-files on disk: finding them, reading their lines, and
seeking in them by time.

A day-file is `<root>/<YYYY-MM-DD>/<host>[.<stream>].jsonl` (or a flat `.jsonl`
under the root), plain or gzip-compressed (`gz`), appended as events occur, one
JSON record to a line. Everything that depends on that shape is here -- the
listing (`feed_files`, `dated_dirs`, `day_files`, `host_of`), the line framing
(`iter_lines_reverse`, `iter_lines_forward`, `complete_end`), the binary
searches by timestamp (`seek_to_epoch`, `end_at_jsonl`) and a file's identity
beyond its inode (`head_digest`) -- so a second on-disk format has one place to
be told apart. They were gathered here from export.py, tail.py, proctree.py and
feeds.py, and each is still importable from where it was.

It is in both code fingerprints (`snapshots.CODE_FILES`, `hours.CODE_FILES`), as
export.py is: a change here discards saved builds and hours.
"""
import collections
import glob
import hashlib
import os
import threading

from . import gz, fastjson
from .normalize import epoch_of


def feed_files(d):
    """A directory's day-files, plain or gzip-compressed (`gz`).

    A plain file wins over its `.gz` twin: both exist while one is being
    compressed into the other, and the plain one is the complete copy.
    """
    found = set(glob.glob(os.path.join(d, "*.jsonl")))
    found.update(p for p in glob.glob(os.path.join(d, "*.jsonl" + gz.SUFFIX))
                 if gz.plain_name(p) not in found)
    return sorted(found)


def dated_dirs(root, days=0):
    """`[(date, files)]` for the newest `days` date dirs under `root` that hold
    a day-file, oldest first; every one of them for 0.

    An empty date dir is passed over, not counted. A capture copied in from
    another cluster has dated dirs made ahead of the files that fill them, and
    counting those pushed the days that do hold records out of every window:
    the 1 h view of such a capture read nothing at all.
    """
    if not os.path.isdir(root):
        return []
    dates = sorted((d for d in os.listdir(root)
                    if d.startswith("20") and os.path.isdir(os.path.join(root, d))),
                   reverse=True)
    out = []
    for d in dates:
        files = feed_files(os.path.join(root, d))
        if files:
            out.append((d, files))
            if days and len(out) >= days:
                break
    return out[::-1]


def day_files(roots, days=0):
    out = []
    for root in roots:
        for _d, files in dated_dirs(root, days):
            out += files
        out += feed_files(root)
    return sorted(set(out))


# The ebpf tier splits each day into two files per host (ebpf_trace.stream_of):
# `<host>.exits.jsonl` and `<host>.snapshot.jsonl`. The node tier still writes a
# single `<host>.jsonl`, and feeds captured before the split are single-file too,
# so all three spellings have to resolve to the same host.
_STREAM_SUFFIXES = (".snapshot", ".exits")


def host_of(path):
    """`<host>[.<stream>].jsonl` -> `<host>`.

    Only a RECOGNISED stream suffix is stripped. That is the whole point: a
    hostname may legitimately contain dots, and blindly cutting at the last one
    would turn `login.rc.fas` into `login.rc` -- inventing a host that does not
    exist and splitting one node's records across two of them in the report."""
    stem = gz.plain_name(os.path.basename(path))
    if stem.endswith(".jsonl"):
        stem = stem[:-len(".jsonl")]
    for suf in _STREAM_SUFFIXES:
        if stem.endswith(suf):
            return stem[:-len(suf)]
    return stem


def iter_lines_reverse(path, block=1 << 20, start=0, end=None):
    """Yield complete lines from the END of a file backwards, bounded memory.

    A day-file is gigabytes, so it cannot be read into memory and reversed. This
    walks backwards one block at a time and reverses within the block, holding at
    most `block` bytes plus one straddling line.

    The first element of a split block is the fragment whose start lies in the
    PREVIOUS (earlier) block, so it is carried rather than yielded -- except at
    `start`, where it is a complete line and is yielded last.

    `[start, end)` narrows the walk to a byte range whose bounds are line
    boundaries; the default is the whole file as it stands when opened.
    """
    try:
        fh = gz.open_feed(path)
    except OSError:
        return
    with fh:
        if end is None:
            fh.seek(0, os.SEEK_END)
            end = fh.tell()
        pos = end
        carry = b""
        while pos > start:
            size = min(block, pos - start)
            pos -= size
            fh.seek(pos)
            buf = fh.read(size) + carry
            parts = buf.split(b"\n")
            carry = parts.pop(0)
            for line in reversed(parts):
                if line.strip():
                    yield line
        if carry.strip():
            yield carry


def iter_lines_forward(path, start=0):
    """Yield complete lines from byte `start` on, as bytes."""
    try:
        fh = gz.open_feed(path)
    except OSError:
        return
    with fh:
        fh.seek(start)
        for line in fh:
            if line.strip():
                yield line


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


def seek_to_epoch(fh, since, size, probe_lines=64):
    """Offset of the first line in `fh` whose timestamp is >= `since`.

    `size` bytes is the search space; the return is `size` when the file holds
    nothing in range, which lets the caller skip it without opening a stream.

    A probe that lands on lines carrying no resolvable timestamp searches
    EARLIER rather than guessing.  That reads more than strictly necessary,
    which is the right way to be wrong here: the alternative is starting the
    rebuild after rows that belonged in the window.
    """
    lo, hi, best = 0, size, size
    while lo < hi:
        mid = (lo + hi) // 2
        off, ts = _probe(fh, mid, probe_lines)
        if ts is None:
            hi = mid                      # unplaceable: widen backwards
        elif ts >= since:
            best = min(best, off)
            hi = mid
        else:
            lo = max(off, mid) + 1        # off >= mid, so this always advances
    return best


def end_at_jsonl(fh, t, size, probe_lines=64):
    """An offset past which every line is later than `t`, or `size`.

    `export.seek_to_epoch`'s binary search with the opposite caution: a probe
    that cannot be placed in time searches LATER, because an end bound read too
    far only costs a search, and one cut too early would lose records.
    """
    lo, hi, best = 0, size, size
    while lo < hi:
        mid = (lo + hi) // 2
        # the probe `seek_to_epoch` makes, and remembers for a file unchanged
        off, ts = _probe(fh, mid, probe_lines)
        if ts is not None and ts > t:
            best = min(best, off)
            hi = mid
        else:
            lo = max(off, mid) + 1
    return best


# What `_probe` and `head_digest` found, by the file's identity on disk. A
# probe's answer is a function of the bytes, and the bytes of a file whose
# device, inode, size and mtime are unchanged are the bytes it read: a sealed
# day-file is asked the same questions over and over -- the hourly reduction
# plans every file it reads every hour, and the first levels of each binary
# search are the same -- and on a `.gz` each answer inflated an 8 MB span. A
# file that grows has a new identity, so nothing is answered for it from before.
_MEMO_FILES = 1024
_MEMO_PROBES = 8192
_memo = collections.OrderedDict()       # identity -> {key: answer}
_memo_lock = threading.Lock()


def _identity(fh):
    try:
        st = os.fstat(fh.fileno())
    except (OSError, AttributeError, ValueError):
        return None
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)


def _memo_of(ident):
    with _memo_lock:
        m = _memo.get(ident)
        if m is None:
            m = _memo[ident] = {}
            while len(_memo) > _MEMO_FILES:
                _memo.popitem(last=False)
        else:
            _memo.move_to_end(ident)
        return m


def _remember(m, key, answer):
    if len(m) >= _MEMO_PROBES:
        m.clear()
    m[key] = answer
    return answer


def _probe(fh, pos, limit):
    """Align to the next whole line at/after `pos`; return (offset, ts|None).
    Answered from the memo for a file unchanged since it was asked (`_memo`)."""
    ident = _identity(fh)
    if ident is None:
        return _probe_read(fh, pos, limit)
    m = _memo_of(ident)
    hit = m.get(("probe", pos, limit))
    if hit is not None:
        return hit
    return _remember(m, ("probe", pos, limit), _probe_read(fh, pos, limit))


def _probe_read(fh, pos, limit):
    fh.seek(pos)
    if pos:
        fh.readline()                     # drop the line we landed inside
    off = fh.tell()
    for _ in range(limit):
        line = fh.readline()
        if not line:
            return off, None
        if not line.strip():
            continue
        try:
            ts = epoch_of(fastjson.loads(line))
        except ValueError:
            continue
        if ts is not None:
            return off, ts
    return off, None


HEAD_BYTES = 4096


def head_digest(fh, n):
    """Digest of a file's first `n` bytes: its identity beyond the inode number.
    Read once per file identity (`_memo`): on a `.gz` it inflates the first span."""
    ident = _identity(fh)
    m = _memo_of(ident) if ident is not None else None
    if m is not None:
        hit = m.get(("head", n))
        if hit is not None:
            return hit
    fh.seek(0)
    out = hashlib.sha1(fh.read(n)).hexdigest()
    return _remember(m, ("head", n), out) if m is not None else out
