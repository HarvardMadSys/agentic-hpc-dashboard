"""The demo feed on disk: laid out, appended and resumed as the collector's own.

    <dir>/feed/ebpf/<YYYY-MM-DD>/<host>.exits.<fmt>      exit records
    <dir>/feed/ebpf/<YYYY-MM-DD>/<host>.snapshot.<fmt>   meta, residency, ticks, submits, stop
    <dir>/feed/node/<YYYY-MM-DD>/<host>.<fmt>            the node tier's snapshots
    <dir>/cache/                                         the service's saved builds
    <dir>/demo.json                                      what wrote the feed, and how far

`<fmt>` is `ebr` (rc_dashboard/ebr.py; what the demo writes unless told
otherwise), `jsonl`, or both: twins that hold the same records in the same
order, as a collector on EBPFM_FORMAT=both writes them. Both tiers go through
the same writers. A day is the record's LOCAL date, as the collector names its
directories, and each file is in time order, which the service's timestamp
binary search relies on.

A round (`append`) is written whole -- JSONL as whole lines, EBR as whole frames
-- and every file it touched is flushed at its end, so the tail never reads half
a record and no frame spans two rounds. An EBR frame also ends on the minute (a
60 s grid on the records' epoch, as `ebr.py convert --window 60` cuts one), so
the history's frames are a minute each, not an hour, and a reader seeks and
skips it as it does a collector's per-round frames. Each EBR file keeps one
`ebr.Writer` from round to round, opened with `Writer.open_append` (which
continues the file's last segment) only when the demo first writes to the file:
at startup, after a cut, or on a new day. A day's files are let go once a
record of a later day comes.

The feed is a function of the seed and the clock (`world.World`), so a restart
resumes it exactly -- JSONL: same bytes; EBR: same decoded records (its frames
and segments follow the rounds that wrote them). `demo.json` says how far it was
written, anything past that in the newest files (a round cut off mid-write) is
cut, and the generator continues from there. A stamp from another generator,
seed, host count or format, or one older than the history asked for, starts the
feed afresh.
"""
import json
import os
import shutil
import time

from .. import ebr, fastjson

STATE = "demo.json"
FORMATS = ("jsonl", "ebr", "both")
SUFFIXES = (".jsonl", ".ebr")
WRITER = "rc_dashboard.demo"        # an EBR segment's `writer`
FRAME_S = 60                        # an EBR frame holds one cell of this epoch grid, of one round
# What a SEGMENT says of itself, beside its writer's meta (ebr.Writer).
_SEGMENT_OWN = frozenset(("format", "version", "str_id_size", "kinds", "reset_bytes",
                          "zlib", "created"))


def day_of(epoch):
    return time.strftime("%Y-%m-%d", time.localtime(epoch))


def _day(path):
    return os.path.basename(os.path.dirname(path))


class Feed:
    """The demo's day-files in format `fmt` (FORMATS). It is `jsonl` unless given,
    for a caller that reads the files as JSONL; the demo passes its own
    (`--demo-format`, ebr by default)."""

    def __init__(self, base, fmt="jsonl"):
        if fmt not in FORMATS:
            raise ValueError("a demo feed is %s, not %r" % (", ".join(FORMATS), fmt))
        self.fmt = fmt
        self.exts = ("jsonl", "ebr") if fmt == "both" else (fmt,)
        self.base = os.path.abspath(base)
        self.ebpf = os.path.join(self.base, "feed", "ebpf")
        self.node = os.path.join(self.base, "feed", "node")
        self.cache = os.path.join(self.base, "cache")
        self.state_path = os.path.join(self.base, STATE)
        self._fh = {}           # path -> an open JSONL file, or an ebr.Writer
        self._minute = {}       # EBR path -> the minute of the frame it has open

    # ------------------------------------------------------------- state
    def load_state(self):
        try:
            with open(self.state_path) as fh:
                st = json.load(fh)
            return st if isinstance(st, dict) else None
        except (OSError, ValueError):
            return None

    def save_state(self, st):
        os.makedirs(self.base, exist_ok=True)
        tmp = self.state_path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(st, fh, indent=1, sort_keys=True)
        os.replace(tmp, self.state_path)

    def wipe(self):
        """The feed, the service's saved builds over it, and the stamp."""
        self.close()
        for d in (os.path.join(self.base, "feed"), self.cache):
            shutil.rmtree(d, ignore_errors=True)
        try:
            os.unlink(self.state_path)
        except OSError:
            pass

    # ------------------------------------------------------------- write
    def _name(self, tier, day, host, stream, ext):
        if tier == "node":
            return os.path.join(self.node, day, "%s.%s" % (host, ext))
        return os.path.join(self.ebpf, day, "%s.%s.%s" % (host, stream, ext))

    def path(self, tier, epoch, host, stream, ext="jsonl"):
        """The day-file a record at `epoch` goes to, in format `ext` (jsonl, ebr)."""
        return self._name(tier, day_of(epoch), host, stream, ext)

    def append(self, items):
        """Append `(epoch, tier, host, stream, line)` items, already in time order:
        each line to the JSONL file, its record to the EBR one. Returns how many
        files were written to."""
        handles, minute = self._fh, self._minute
        jsonl, binary = "jsonl" in self.exts, "ebr" in self.exts
        loads = fastjson.loads
        touched = {}
        cur = None
        for epoch, tier, host, stream, line in items:
            day = day_of(epoch)
            if day != cur:
                cur = day
                # no record goes to an earlier day again
                self._let_go_before(day)
            if jsonl:
                p = self._name(tier, day, host, stream, "jsonl")
                fh = handles.get(p)
                if fh is None:
                    os.makedirs(os.path.dirname(p), exist_ok=True)
                    fh = handles[p] = open(p, "ab")
                fh.write(line)
                touched[p] = fh
            if binary:
                p = self._name(tier, day, host, stream, "ebr")
                w = handles.get(p)
                if w is None:
                    w = handles[p] = self._open_ebr(p, host, stream or tier)
                m = epoch // FRAME_S
                if minute.get(p) != m:
                    w.flush()               # the frame of the minute before ends here
                    minute[p] = m
                w.write(loads(line))
                touched[p] = w
        # the round's last lines and frames, whole: one flush per file
        for p, h in touched.items():
            if handles.get(p) is h:
                h.flush()
        return len(touched)

    def _open_ebr(self, path, host, stream):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self._minute[path] = None
        return ebr.Writer.open_append(path, meta={"host": host, "stream": stream,
                                                  "writer": WRITER, "synthetic": True})

    def _let_go_before(self, day):
        """Close the files of the days before `day` (YYYY-MM-DD)."""
        for p in [p for p in self._fh if _day(p) < day]:
            self._minute.pop(p, None)
            self._fh.pop(p).close()

    def close(self):
        """Flush and close every open day-file, releasing each EBR file's lock."""
        for h in self._fh.values():
            try:
                h.close()
            except (OSError, ValueError):
                pass
        self._fh.clear()
        self._minute.clear()

    # ------------------------------------------------------------ resume
    def _files(self, root, days):
        if not os.path.isdir(root):
            return []
        dates = sorted((d for d in os.listdir(root) if d.startswith("20")), reverse=True)[:days]
        out = []
        for d in dates:
            dd = os.path.join(root, d)
            out += [os.path.join(dd, f) for f in sorted(os.listdir(dd)) if f.endswith(SUFFIXES)]
        return out

    def cut_after(self, watermark):
        """Drop every record at or after `watermark` from the newest files: what a
        round cut off mid-write left past the last round the stamp recorded."""
        self.close()
        cut = 0
        for p in self._files(self.ebpf, 2) + self._files(self.node, 2):
            cut += (_cut_file_ebr if p.endswith(".ebr") else _cut_file)(p, watermark)
        return cut

    def oldest_day(self):
        """The oldest day directory of the eBPF tier (YYYY-MM-DD), or None."""
        if not os.path.isdir(self.ebpf):
            return None
        days = sorted(d for d in os.listdir(self.ebpf) if d.startswith("20"))
        return days[0] if days else None

    def prune(self, before_day):
        """Remove whole day directories older than `before_day` (YYYY-MM-DD). The
        files being written, all of a later day, stay open."""
        self._let_go_before(before_day)
        for root in (self.ebpf, self.node):
            if not os.path.isdir(root):
                continue
            for d in os.listdir(root):
                if d.startswith("20") and d < before_day:
                    shutil.rmtree(os.path.join(root, d), ignore_errors=True)


def _before(rec, watermark):
    ts = rec.get("ts_epoch")
    return isinstance(ts, (int, float)) and ts < watermark


def _cut_file(path, watermark, window=1 << 20):
    """Truncate `path` before its first line at or after `watermark`, and drop a
    partial last line. The file is in time order and the cut is one round from
    its end, so this reads back only as far as it needs."""
    try:
        fh = open(path, "r+b")
    except OSError:
        return 0
    with fh:
        size = fh.seek(0, 2)
        while True:
            start = max(0, size - window)
            fh.seek(start)
            lines = fh.read().split(b"\n")
            offs, off = [], start
            for ln in lines:
                offs.append(off)
                off += len(ln) + 1
            # the piece after the last newline is a line still being written
            keep = offs[-1] if lines[-1] else size
            first = 1 if start > 0 else 0       # the window may cut the first line
            done = False
            for i in range(len(lines) - 2, first - 1, -1):
                try:
                    ts = json.loads(lines[i]).get("ts_epoch")
                except ValueError:
                    ts = None
                if isinstance(ts, (int, float)) and ts < watermark:
                    done = True
                    break
                keep = offs[i]
            if done or start == 0:
                break
            window *= 4
        if keep < size:
            fh.truncate(keep)
            return 1
    return 0


def _cut_file_ebr(path, watermark):
    """`_cut_file` for an EBR file: truncate it to its last complete frame (one cut
    off mid-write goes), then walk back from there, dropping every frame whose
    records all lie at or after `watermark` -- and any SEGMENT, empty frame or
    corrupt region among them -- up to the first frame before it.

    `append` ends a frame at every round's end, and the stamp is a round's end,
    so no frame should straddle the watermark. If one does all the same, it is
    decoded, the file is truncated before it, and its records before the
    watermark are written again, as a frame of their own."""
    try:
        fh = open(path, "rb")
    except OSError:
        return 0
    redo, meta = [], None
    with fh:
        size = fh.seek(0, 2)
        keep = ebr.complete_end(fh, size)
        for f in ebr.frames_reverse(fh, keep):
            if f.kind != ebr.DATA or not f.n_records or f.ts_min >= watermark:
                keep = f.off
                continue
            if f.ts_max < watermark:
                break
            # across the watermark, or a record without a time (+-inf): look inside
            dec = ebr.Decoder.at(fh, f.off)
            redo = [rec for _pos, rec in dec.records(fh, f) if _before(rec, watermark)]
            meta = {k: v for k, v in (dec.segment or {}).items() if k not in _SEGMENT_OWN}
            keep = f.off
            break
    if keep >= size:
        return 0
    os.truncate(path, keep)
    if redo:
        with ebr.Writer.open_append(path, meta=meta) as w:
            for rec in redo:
                w.write(rec)
    return 1
