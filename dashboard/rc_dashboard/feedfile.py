"""The collector's day-files on disk: finding them, reading their records, and
seeking in them by time -- in either of the two formats the collector writes.

A day-file is `<root>/<YYYY-MM-DD>/<host>[.<stream>].<fmt>` (or a flat file
under the root), plain or gzip-compressed (`gz`), appended as events occur:

  jsonl   one JSON record to a line
  ebr     EBR, the collector's binary records (`ebr.py`, a byte-identical copy
          of collector/lib/ebr.py): CRC-checked frames, normally one per poll
          round, each with its time bounds and event kinds in its header

Everything that depends on that shape is here -- the listing (`feed_files`,
`dated_dirs`, `day_files`, `host_of`, and the twin rule, `choose`), the line
framing (`iter_lines_reverse`, `iter_lines_forward`, `complete_end`), the
binary searches by timestamp (`seek_to_epoch`, `end_at`), a file's identity
beyond its inode (`head_digest`), and EBR's frames and records (`decoder_at`,
`records_forward`, `records_reverse`, `last_records`, `newest`) -- so a reader
branches on a file's format once, at the top of its loop, and the JSONL loops
stay as they were. Each name that moved here is still importable from where it
was.

TWINS. A collector writing both formats (`EBPFM_FORMAT=both`) leaves two files
per stream, `x.jsonl` and `x.ebr`, holding the same records in the same order;
a reader reads exactly ONE of them, the one `choose()` names for the stem
(`feeds.<tier>.prefer`, jsonl unless set). The choice is monotone -- a live day
never switches twins, so nothing is read twice -- because it is a function of
each twin's FIRST record, which never changes once written:

  one file for the stem                     that file
  neither twin holds a record yet           held: nothing is read
  the preferred twin holds one              it, unless its first record is
                                            more than MIDDAY_S after the other
                                            twin's first: started mid-day, so
                                            the other twin is read, and why
  only the other twin holds one             held for HOLD_S after that record,
                                            then the other twin

A final choice is remembered by the twins' identities, and a held stem still
counts as a day (`dated_dirs`). For either format the plain file beats its
`.gz`, as it always did: both exist while one is compressed into the other.

It is in both code fingerprints (`snapshots.CODE_FILES`, `hours.CODE_FILES`), as
export.py and ebr.py are: a change here discards saved builds and hours.
"""
import collections
import contextlib
import gzip
import hashlib
import os
import threading
import time
import zlib

from . import ebr, gz, fastjson
from .normalize import epoch_of

FORMATS = ("jsonl", "ebr")
SUFFIX = {"jsonl": ".jsonl", "ebr": ".ebr"}
PREFER = "jsonl"            # what `feeds.<tier>.prefer` is unless set
# A preferred twin whose first record is this far after its twin's first was
# started mid-day: the other twin holds the day's start, and is read instead.
MIDDAY_S = 1.0
# How long a stem waits for its preferred twin's first record once the other
# twin has one. A collector writing both formats writes the two in one flush,
# so the wait is a poll round; past this, the preferred twin is not coming.
HOLD_S = 300.0
# A live EBR file's last bytes may not all be visible yet over NFS: a corrupt
# region that starts this close to its end is read as a frame still being
# written (`ebr.frames(settle=)`), not resynced past. A sealed `.gz` has none.
SETTLE = 4 << 20
# The most of a file's head read for its first record (`first_record`).
FIRST_MAX = 64 << 20
# Frames a seek's probe walks past (SEGMENTs, corrupt regions) for one that says
# when its records are.
PROBE_FRAMES = 16


# ------------------------------------------------------------------- names
# The ebpf tier splits each day into two files per host (ebpf_trace.stream_of):
# `<host>.exits.<fmt>` and `<host>.snapshot.<fmt>`. The node tier still writes a
# single `<host>.<fmt>`, and feeds captured before the split are single-file too,
# so all three spellings have to resolve to the same host.
_STREAM_SUFFIXES = (".snapshot", ".exits")


def split_name(path):
    """`(stem, fmt, gz)` of a day-file's name: `h.exits.jsonl.gz` is
    `("h.exits", "jsonl", True)`. `fmt` is None for a name of neither format."""
    name = os.path.basename(path)
    z = gz.is_gz(name)
    if z:
        name = name[:-len(gz.SUFFIX)]
    for fmt in FORMATS:
        suf = SUFFIX[fmt]
        if name.endswith(suf):
            return name[:-len(suf)], fmt, z
    return name, None, z


def format_of(path):
    """`jsonl` or `ebr`, by the file's name (`jsonl` for any other name)."""
    return split_name(path)[1] or "jsonl"


def stem_of(path):
    """`(dir, stem)`: what a file is one of the twins of."""
    return os.path.dirname(path), split_name(path)[0]


def host_of(path):
    """`<host>[.<stream>].<fmt>[.gz]` -> `<host>`.

    Only a RECOGNISED stream suffix is stripped. That is the whole point: a
    hostname may legitimately contain dots, and blindly cutting at the last one
    would turn `login.rc.fas` into `login.rc` -- inventing a host that does not
    exist and splitting one node's records across two of them in the report.
    Either format's suffix goes first, so a host's EBR twin is the same host."""
    stem = split_name(path)[0]
    for suf in _STREAM_SUFFIXES:
        if stem.endswith(suf):
            return stem[:-len(suf)]
    return stem


def twin_names(path):
    """Every name `path`'s twin of the other format could have: plain and `.gz`."""
    d, stem = stem_of(path)
    fmt = split_name(path)[1]
    out = []
    for f in FORMATS:
        if f != fmt:
            p = os.path.join(d, stem + SUFFIX[f])
            out += [p, p + gz.SUFFIX]
    return out


def prefer_of(cfg, tier="ebpf"):
    """The format a tier reads when a stream has both (`feeds.<tier>.prefer`).
    `config.load` refuses anything but the two; a config built another way
    reads JSONL rather than fail."""
    v = cfg.get("feeds.%s.prefer" % tier, PREFER) if cfg is not None else PREFER
    v = str(v or PREFER).strip().lower()
    return v if v in FORMATS else PREFER


# ----------------------------------------------------------------- twins
_twin_lock = threading.Lock()
_firsts = collections.OrderedDict()    # (dev, ino, path) -> first record's epoch
_choices = collections.OrderedDict()   # (dir, stem, prefer, ((fmt, dev, ino), ...)) -> (fmt, why)
_KEEP = 8192


def _keep(memo, key, value):
    with _twin_lock:
        memo[key] = value
        memo.move_to_end(key)
        while len(memo) > _KEEP:
            memo.popitem(last=False)


def first_record(path):
    """`(has_record, ts)`: whether `path` holds one complete record yet -- a
    whole line, or a whole DATA frame -- and that first record's epoch (None
    when it carries none). Once it has one, that never changes, so the answer
    is kept by the file's identity. A `.gz` is streamed from its start
    (`gzip.open`): no seek index is built for it."""
    try:
        st = os.stat(path)
    except OSError:
        return False, None
    key = (st.st_dev, st.st_ino, path)
    with _twin_lock:
        if key in _firsts:
            return True, _firsts[key]
    got = _first_read(path)
    if got[0]:
        _keep(_firsts, key, got[1])
    return got


def _first_read(path):
    ebr_fmt = format_of(path) == "ebr"
    try:
        if gz.is_gz(path):
            with gzip.open(path, "rb") as fh:
                return _first_ebr_stream(fh) if ebr_fmt else _first_jsonl(fh)
        if ebr_fmt:
            return _first_ebr(path, SETTLE)
        with open(path, "rb") as fh:
            return _first_jsonl(fh)
    except (OSError, EOFError, zlib.error, ebr.Error):
        return False, None


def _first_jsonl(fh):
    budget = FIRST_MAX
    while budget > 0:
        line = fh.readline(budget)
        if not line.endswith(b"\n"):
            return False, None          # nothing complete yet
        budget -= len(line)
        if not line.strip():
            continue
        try:
            rec = fastjson.loads(line)
        except ValueError:
            return True, None
        return True, (epoch_of(rec) if isinstance(rec, dict) else None)
    return False, None


def _first_ebr(src, settle):
    dec = ebr.Decoder()
    for f in ebr.frames(src, settle=settle):
        if f.kind == ebr.DATA and f.n_records:
            for _pos, rec in dec.records(src, f):
                return True, epoch_of(rec)
            return True, None
        dec.skip(f, src)
    return False, None


def _first_ebr_stream(fh):
    buf, want = b"", 1 << 16
    while True:
        more = fh.read(want - len(buf))
        buf += more
        got = _first_ebr(buf, len(buf))
        if got[0] or not more or len(buf) >= FIRST_MAX:
            return got
        want = min(FIRST_MAX, want * 4)


def _ago(s):
    return "%.0f s" % s if s < 120 else "%.1f h" % (s / 3600.0) if s >= 7200 else "%.0f min" % (s / 60.0)


def choose(d, stem, cands, prefer=PREFER, now=None):
    """`(path, reason)`: which of `cands` ({fmt: path}, one stem's files in `d`)
    is read, or `(None, reason)` while the stem is held. The rule is in the
    module's docstring; `reason` is None when the preferred twin (or the only
    file) is read for no other reason than that."""
    if len(cands) == 1:
        return next(iter(cands.values())), None
    ids = []
    for fmt in sorted(cands):
        try:
            st = os.stat(cands[fmt])
        except OSError:
            continue
        ids.append((fmt, st.st_dev, st.st_ino))
    if len(ids) < len(cands):           # one went while the directory was listed
        left = {i[0]: cands[i[0]] for i in ids}
        return choose(d, stem, left, prefer, now) if left else (None, "gone")
    if prefer not in cands:
        prefer = sorted(cands)[0]
    other = next(f for f in FORMATS if f != prefer and f in cands)
    key = (d, stem, prefer, tuple(ids))
    with _twin_lock:
        hit = _choices.get(key)
    if hit is not None:
        return cands[hit[0]], hit[1]
    has_p, tp = first_record(cands[prefer])
    has_o, to = first_record(cands[other])
    if has_p:
        if has_o and tp is not None and to is not None and tp > to + MIDDAY_S:
            fmt, why = other, ("the %s twin's first record is %s after the %s twin's: "
                               "it was started mid-day" % (prefer, _ago(tp - to), other))
        else:
            fmt, why = prefer, None
    elif has_o:
        now = time.time() if now is None else now
        if to is not None and now - to < HOLD_S:
            return None, ("held: the %s twin has a record and the preferred %s twin none "
                          "yet" % (other, prefer))
        fmt, why = other, ("the preferred %s twin held no record %d s after the %s twin's "
                           "first" % (prefer, HOLD_S, other))
    else:
        return None, "held: neither twin holds a record yet"
    _keep(_choices, key, (fmt, why))
    return cands[fmt], why


def _listing(d, prefer=PREFER, now=None):
    """`(files, notes)`: the day-files read in `d`, one per stem, and a note for
    every stem with twins or none read (`feeds.resolve_dated` reports them)."""
    try:
        names = os.listdir(d)
    except OSError:
        return [], []
    by = {}
    for n in names:
        if n.startswith("."):
            continue
        stem, fmt, z = split_name(n)
        if fmt is None:
            continue
        slot = by.setdefault(stem, {})
        cur = slot.get(fmt)
        if cur is None or (not z and gz.is_gz(cur)):
            slot[fmt] = os.path.join(d, n)
    files, notes = [], []
    for stem in sorted(by):
        cands = by[stem]
        path, why = choose(d, stem, cands, prefer, now)
        if path is not None:
            files.append(path)
        if len(cands) > 1 or path is None:
            notes.append({"dir": d, "stream": stem, "files": dict(cands),
                          "read": None if path is None else format_of(path),
                          "path": path, "reason": why})
    return sorted(files), notes


def feed_files(d, prefer=PREFER, now=None, report=None):
    """A directory's day-files, one per stream: plain or gzip-compressed (`gz`),
    of the format the twin rule chooses (`choose`; `prefer` is the tier's).

    A plain file wins over its `.gz` twin: both exist while one is being
    compressed into the other, and the plain one is the complete copy.
    `report`, a list, receives a note per stem with twins or none read.
    """
    files, notes = _listing(d, prefer, now)
    if report is not None:
        report.extend(notes)
    return files


def dated_dirs(root, days=0, prefer=PREFER, now=None, report=None):
    """`[(date, files)]` for the newest `days` date dirs under `root` that hold
    a day-file, oldest first; every one of them for 0.

    An empty date dir is passed over, not counted. A capture copied in from
    another cluster has dated dirs made ahead of the files that fill them, and
    counting those pushed the days that do hold records out of every window:
    the 1 h view of such a capture read nothing at all. A dir whose streams
    are all held (twins with no record yet, `choose`) counts, with no files,
    as a dir of empty day-files always did.
    """
    if not os.path.isdir(root):
        return []
    dates = sorted((d for d in os.listdir(root)
                    if d.startswith("20") and os.path.isdir(os.path.join(root, d))),
                   reverse=True)
    out = []
    for d in dates:
        files, notes = _listing(os.path.join(root, d), prefer, now)
        if files or any(n["path"] is None for n in notes):
            out.append((d, files))
            if report is not None:
                report.extend(dict(n, day=d) for n in notes)
            if days and len(out) >= days:
                break
    return out[::-1]


def day_files(roots, days=0, prefer=PREFER):
    out = []
    for root in roots:
        for _d, files in dated_dirs(root, days, prefer):
            out += files
        out += feed_files(root, prefer)
    return sorted(set(out))


def chosen_for(path, prefer=PREFER, now=None):
    """The file read for `path`'s stream now (`path`, its `.gz`, or its twin of
    the other format), or None while the stream is held or gone."""
    d, stem = stem_of(path)
    for p in _listing(d, prefer, now)[0]:
        if split_name(p)[0] == stem:
            return p
    return None


# ------------------------------------------------------- JSONL line framing
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


def complete_end(fh, size, block=65536, fmt="jsonl", settle=0):
    """Offset just past the last complete record in the first `size` bytes, else 0.

    JSONL: just past the last newline. EBR: the end of the last complete frame,
    moved back before a corrupt region in its last `settle` bytes
    (`settled_end`)."""
    if fmt == "ebr":
        return settled_end(ebr_src(fh), size, settle)
    pos = size
    while pos > 0:
        n = min(block, pos)
        pos -= n
        fh.seek(pos)
        cut = fh.read(n).rfind(b"\n")
        if cut != -1:
            return pos + cut + 1
    return 0


def is_boundary(fh, off, fmt="jsonl"):
    """Whether a read can resume at `off`: the start, or just past a line
    (JSONL) or a frame (EBR)."""
    if off == 0:
        return True
    if fmt == "ebr":
        return ebr.is_boundary(ebr_src(fh), off)
    fh.seek(off - 1)
    return fh.read(1) == b"\n"


# ------------------------------------------------------------ time searches
def seek_to_epoch(fh, since, size, fmt="jsonl", probe_lines=64):
    """Offset of the first line in `fh` whose timestamp is >= `since`.

    `size` bytes is the search space; the return is `size` when the file holds
    nothing in range, which lets the caller skip it without opening a stream.

    A probe that lands on lines carrying no resolvable timestamp searches
    EARLIER rather than guessing.  That reads more than strictly necessary,
    which is the right way to be wrong here: the alternative is starting the
    rebuild after rows that belonged in the window.

    EBR (`fmt`): the first frame whose newest record (`ts_max`, its header's)
    is >= `since`, by the same search over frames; a probe that lands on a
    frame still being written, or one whose records cannot all be placed in
    time, searches earlier too.
    """
    if fmt == "ebr":
        return _seek_ebr(fh, since, size)
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


def end_at(fh, t, size, fmt="jsonl"):
    """An offset past which every record is later than `t`, or `size`: for
    EBR, the first frame whose OLDEST record (`ts_min`) is after `t`. Not
    `seek_to_epoch`'s rule: a frame spans a poll round, so the first frame
    with a record after `t` can hold records before it too."""
    if fmt == "ebr":
        return _end_at_ebr(fh, t, size)
    return end_at_jsonl(fh, t, size)


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


# ------------------------------------------------------------------- EBR
def ebr_src(fh):
    """What `ebr` reads an open day-file through: a plain file by its path (it
    is read with os.pread), a compressed one through its reader, which reads
    and seeks by uncompressed offset (`gz.open_feed`)."""
    if isinstance(getattr(fh, "raw", None), gz.GzipRaw):
        return fh
    name = getattr(fh, "name", None)
    return name if isinstance(name, str) else fh


def settle_of(path):
    """`ebr.frames(settle=)` for a file: a plain one may still be written to."""
    return 0 if gz.is_gz(path) else SETTLE


@contextlib.contextmanager
def ebr_source(path):
    """`path` as an `ebr` source: the path of a plain file, the open reader of a
    `.gz`. OSError when it cannot be opened."""
    if gz.is_gz(path):
        with gz.open_feed(path) as fh:
            yield fh
    else:
        os.stat(path)
        yield path


def decoder_at(src, off):
    """A decoder for the frame at `off`: its segment's tables replayed from the
    SEGMENT (`ebr.Decoder.at`). An offset that is no frame's end -- right after
    a corrupt region -- gets a decoder broken until the next SEGMENT, which is
    all a reader can decode from there."""
    try:
        return ebr.Decoder.at(src, off)
    except ebr.Error:
        return ebr.Decoder()


def errors(st):
    """What a decoder could not read, from its stats: bad records, records lost
    with a frame, corrupt or undecodable frames, and frames of a segment this
    reader does not support."""
    return (st["bad_records"] + st["lost_records"] + st["bad_frames"]
            + st["unsupported_frames"])


def settled_end(src, size, settle=SETTLE):
    """`ebr.complete_end`, moved back before the first corrupt region in its
    last `settle` bytes: there a live file's bytes may still be arriving, and a
    reader that took up after them would never read what they hold."""
    end = ebr.complete_end(src, size)
    if not settle or not end:
        return end
    cut = end
    for f in ebr.frames_reverse(src, end, verify=True):
        if f.kind == ebr.BAD:
            cut = f.off
        if f.off <= end - settle:
            break
    return cut


def _placed(f):
    return f.ts_min - f.ts_min == 0.0 and f.ts_max - f.ts_max == 0.0


def _probe_ebr(fh, pos, size):
    """`(off, foff, ts_min, ts_max)` for a probe at `pos`: `off` the first frame
    header at or after it, and from there the first DATA frame placed in time
    (SEGMENTs, corrupt regions and frames with no time at all are walked past,
    PROBE_FRAMES at most, as `_probe` walks past lines with no time): its
    offset and its times. A frame is placed by its header, or -- when a record
    in it has no `ts_epoch`, which leaves the header's times infinite -- by its
    records' own times as `epoch_of` reads them, a legacy record's `ts` string
    included, as `_probe` reads a line's. A capture converted from JSONL older
    than `ts_epoch` is all such frames, and a seek by the headers alone would
    skip all of it. The times are None when there is none by `size` -- the
    frame is still being written -- and `off` is `size` when no header is.
    Remembered by the file's identity, as `_probe`'s answers are."""
    ident = _identity(fh)
    m = _memo_of(ident) if ident is not None else None
    key = ("ebr", pos, size)
    if m is not None:
        hit = m.get(key)
        if hit is not None:
            return hit
    src = ebr_src(fh)
    out = (size, size, None, None)
    off = ebr.resync(src, pos, size)
    if off is not None and off < size:
        out = (off, size, None, None)
        for n, f in enumerate(ebr.frames(src, off, size, verify=False)):
            if f.kind == ebr.DATA and f.n_records:
                times = (f.ts_min, f.ts_max) if _placed(f) else _record_times(src, f)
                if times is not None:
                    out = (off, f.off) + times
                    break
            if n + 1 >= PROBE_FRAMES:
                break
    return _remember(m, key, out) if m is not None else out


def _record_times(src, f):
    """`(ts_min, ts_max)` of a DATA frame its header cannot place, from its
    records' times as `epoch_of` reads them; None when no record has one."""
    lo = hi = None
    try:
        dec = decoder_at(src, f.off)
        for _pos, rec in dec.records(src, f):
            t = epoch_of(rec)
            if t is None:
                continue
            if lo is None or t < lo:
                lo = t
            if hi is None or t > hi:
                hi = t
    except (ebr.Error, ValueError, OSError):
        return None
    return None if lo is None else (lo, hi)


def _seek_ebr(fh, since, size):
    """The first frame that reaches `since`. The offset a probe lands on, not
    the frame it places: what it walked past is read too."""
    lo, hi, best = 0, size, size
    while lo < hi:
        mid = (lo + hi) // 2
        off, _foff, _tmin, tmax = _probe_ebr(fh, mid, size)
        if tmax is None:
            hi = mid                      # still being written: widen backwards
        elif tmax >= since:
            best = min(best, off)
            hi = mid
        else:
            lo = max(off, mid) + 1        # off >= mid, so this always advances
    return best


def _end_at_ebr(fh, t, size):
    """The first frame entirely after `t`: the frame a probe places, not the
    offset it landed on, so what it walked past is read too."""
    lo, hi, best = 0, size, size
    while lo < hi:
        mid = (lo + hi) // 2
        off, foff, tmin, _tmax = _probe_ebr(fh, mid, size)
        if tmin is not None and tmin > t:
            best = min(best, foff)
            hi = mid
        else:
            lo = max(off, mid) + 1        # unplaceable: an end read too far costs a search
    return best


def records_forward(path, start=0, end=None, kinds=None, where=None, stats=None):
    """`(position, record)` of an EBR day-file from frame boundary `start` on,
    oldest first, to `end` or the last complete frame. `kinds`/`where` are
    ebr's filters; `stats` (a dict) receives the decoder's counters."""
    try:
        cm = ebr_source(path)
        src = cm.__enter__()
    except OSError:
        return
    dec = None
    try:
        dec = decoder_at(src, start)
        for f in ebr.frames(src, start, end, settle=settle_of(path)):
            for pr in dec.records(src, f, kinds, where):
                yield pr
    finally:
        cm.__exit__(None, None, None)
        if stats is not None and dec is not None:
            for k, v in dec.stats.items():
                stats[k] = stats.get(k, 0) + v


def records_reverse(path, end, start=0, kinds=None, where=None, stats=None):
    """`(position, record)` of the frames of an EBR day-file in `[start, end)`,
    NEWEST first (`ebr.records_reverse`)."""
    try:
        cm = ebr_source(path)
        src = cm.__enter__()
    except OSError:
        return
    try:
        for pr in ebr.records_reverse(src, end, start, kinds, where, stats):
            yield pr
    finally:
        cm.__exit__(None, None, None)


def last_records(path, n):
    """The last `n` records of an EBR day-file, oldest first, read from its end
    (the node tier's snapshots: only the newest few per host matter)."""
    out = []
    if n <= 0:
        return out
    with ebr_source(path) as src:
        for _pos, rec in ebr.records_reverse(src, ebr.complete_end(src)):
            out.append(rec)
            if len(out) >= n:
                break
    out.reverse()
    return out


def newest(path, want, limit=None):
    """`want(record)` of the newest record of an EBR day-file it is not None
    for, read from the end and at most `limit` bytes back; None if none."""
    with ebr_source(path) as src:
        end = ebr.complete_end(src)
        start = 0 if limit is None else max(0, end - limit)
        for _pos, rec in ebr.records_reverse(src, end, start):
            v = want(rec)
            if v is not None:
                return v
    return None
