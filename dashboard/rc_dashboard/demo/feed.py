"""The demo feed on disk: laid out, appended and resumed as the collector's own.

    <dir>/feed/ebpf/<YYYY-MM-DD>/<host>.exits.jsonl      exit records
    <dir>/feed/ebpf/<YYYY-MM-DD>/<host>.snapshot.jsonl   meta, residency, ticks, submits, stop
    <dir>/feed/node/<YYYY-MM-DD>/<host>.jsonl            the node tier's snapshots
    <dir>/cache/                                         the service's saved builds
    <dir>/demo.json                                      what wrote the feed, and how far

A day is the record's LOCAL date, as the collector names its directories, and
each file is in time order, which the service's timestamp binary search relies
on. Every write is whole lines, flushed once per round, so the tail never reads
half a record.

The feed is a function of the seed and the clock (`world.World`), so a restart
resumes it exactly: `demo.json` says how far it was written, anything past that
in the newest files (a round cut off mid-write) is cut, and the generator
continues from there. A stamp from another generator, seed or host count, or one
older than the history asked for, starts the feed afresh.
"""
import json
import os
import shutil
import time

STATE = "demo.json"


def day_of(epoch):
    return time.strftime("%Y-%m-%d", time.localtime(epoch))


class Feed:
    def __init__(self, base):
        self.base = os.path.abspath(base)
        self.ebpf = os.path.join(self.base, "feed", "ebpf")
        self.node = os.path.join(self.base, "feed", "node")
        self.cache = os.path.join(self.base, "cache")
        self.state_path = os.path.join(self.base, STATE)
        self._fh = {}

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
    def path(self, tier, epoch, host, stream):
        root = self.node if tier == "node" else self.ebpf
        name = "%s.jsonl" % host if tier == "node" else "%s.%s.jsonl" % (host, stream)
        return os.path.join(root, day_of(epoch), name)

    def append(self, items):
        """Append `(epoch, tier, host, stream, line)` items, already in time order."""
        touched = set()
        for epoch, tier, host, stream, line in items:
            p = self.path(tier, epoch, host, stream)
            fh = self._fh.get(p)
            if fh is None:
                os.makedirs(os.path.dirname(p), exist_ok=True)
                fh = self._fh[p] = open(p, "ab")
            fh.write(line)
            touched.add(p)
        for p in touched:
            self._fh[p].flush()
        # a day is written for as long as its records arrive, then let go
        for p in [p for p in self._fh if p not in touched and len(self._fh) > 8]:
            self._fh.pop(p).close()
        return len(touched)

    def close(self):
        for fh in self._fh.values():
            try:
                fh.close()
            except OSError:
                pass
        self._fh.clear()

    # ------------------------------------------------------------ resume
    def _files(self, root, days):
        if not os.path.isdir(root):
            return []
        dates = sorted((d for d in os.listdir(root) if d.startswith("20")), reverse=True)[:days]
        out = []
        for d in dates:
            dd = os.path.join(root, d)
            out += [os.path.join(dd, f) for f in sorted(os.listdir(dd)) if f.endswith(".jsonl")]
        return out

    def cut_after(self, watermark):
        """Drop every line at or after `watermark` from the newest files: what a
        round cut off mid-write left past the last round the stamp recorded."""
        self.close()
        cut = 0
        for p in self._files(self.ebpf, 2) + self._files(self.node, 2):
            cut += _cut_file(p, watermark)
        return cut

    def prune(self, before_day):
        """Remove whole day directories older than `before_day` (YYYY-MM-DD)."""
        self.close()
        for root in (self.ebpf, self.node):
            if not os.path.isdir(root):
                continue
            for d in os.listdir(root):
                if d.startswith("20") and d < before_day:
                    shutil.rmtree(os.path.join(root, d), ignore_errors=True)


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
