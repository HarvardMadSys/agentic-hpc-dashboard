"""Writes day files the way the collector (collector/ebpf_trace.py) does.

`<root>/<YYYY-MM-DD>/<host>.<stream>.<fmt>`, appended to in place, `<fmt>` each of
`formats`: `jsonl` (the default), `ebr` (its binary records, written here by the
collector's own writer, collector/lib/ebr.py), or both, as twins. At a rollover the
collector opens the new day's files first and then runs `nice gzip -f` on the old
ones, which writes `<file>.gz` and only then unlinks the plain file.
`roll(gzip="thread")` reproduces that delay.
"""
import importlib.util
import io
import json
import os
import subprocess
import sys
import threading
import time

STREAMS = ("exits", "snapshot")
DAY_SUFFIXES = (".jsonl", ".ebr")          # the collector's formats (EBPFM_FORMAT)
DAY0 = 1794268800.0                        # 2026-11-10 00:00 UTC

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
EBR_PY = os.path.join(REPO, "collector", "lib", "ebr.py")


def _load_ebr():
    """collector/lib/ebr.py imported by its path, writing no bytecode beside it; None
    when it is not alongside fleet/."""
    if not os.path.isfile(EBR_PY):
        return None
    spec = importlib.util.spec_from_file_location("collector_ebr", EBR_PY)
    mod = importlib.util.module_from_spec(spec)
    saved, sys.dont_write_bytecode = sys.dont_write_bytecode, True
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.dont_write_bytecode = saved
    return mod


ebr = _load_ebr()


def records(n, start=0, event="exit"):
    """`n` records shaped like the collector's: an event, its time, a process."""
    return [{"event": event, "ts_epoch": DAY0 + i, "pid": 1000 + i, "comm": "python",
             "args": "python train.py --epochs %d" % i} for i in range(start, start + n)]


def ebr_bytes(batches, meta=None):
    """One EBR segment holding each batch of records in frames of its own (one flush
    of the collector's writer per batch), as bytes."""
    buf = io.BytesIO()
    w = ebr.Writer(buf, meta=meta or {})
    for batch in batches:
        for r in batch:
            w.write(r)
        w.flush()
    w.close()
    return buf.getvalue()


class FakeCollector:
    def __init__(self, root, host="nodeA.cluster.example.org", day="2026-11-11", streams=STREAMS,
                 formats=("jsonl",)):
        self.root, self.host, self.day, self.formats = root, host, day, formats
        os.makedirs(os.path.join(root, day), exist_ok=True)
        for s in streams:
            for f in formats:
                open(self.path(s, fmt=f), "ab").close()
        self.threads = []

    def path(self, stream, day=None, fmt="jsonl"):
        return os.path.join(self.root, day or self.day, "%s.%s.%s" % (self.host, stream, fmt))

    def key(self, stream, day=None, fmt="jsonl"):
        """The name a stream frame carries for this file: `<day>/<file>`."""
        return "%s/%s.%s.%s" % (day or self.day, self.host, stream, fmt)

    def append(self, stream, data, day=None, fmt="jsonl"):
        with open(self.path(stream, day, fmt), "ab") as fh:
            fh.write(data)

    def read(self, stream, day=None, fmt="jsonl"):
        with open(self.path(stream, day, fmt), "rb") as fh:
            return fh.read()

    def write_records(self, stream, recs, fmt="jsonl", day=None):
        """Append `recs` in one write, as one poll round's flush: JSON lines, or EBR
        frames from the collector's own writer, which continues the file's last segment
        as the collector does when it opens a day's file again."""
        if fmt == "jsonl":
            self.append(stream, "".join(json.dumps(r) + "\n" for r in recs).encode("utf-8"), day)
            return
        w = ebr.Writer.open_append(self.path(stream, day, fmt),
                                   meta={"host": self.host, "stream": stream})
        try:
            for r in recs:
                w.write(r)
        finally:
            w.close()                      # one os.write of every frame

    def write_twins(self, stream, recs, day=None):
        """The same records to each format's file, JSONL first, as EBPFM_FORMAT=both does."""
        for f in self.formats:
            self.write_records(stream, recs, f, day)

    def roll(self, new_day, gzip=True, delay=0.5):
        """Open the new day's files, then seal the old ones. `gzip`: True runs
        gzip now, "thread" runs it after `delay` seconds, False not at all."""
        old = self.day
        os.makedirs(os.path.join(self.root, new_day), exist_ok=True)
        self.day = new_day
        for s in STREAMS:
            for f in self.formats:
                open(self.path(s, fmt=f), "ab").close()
        if gzip is True:
            self.gzip_day(old)
        elif gzip == "thread":
            t = threading.Thread(target=self._gzip_later, args=(old, delay), daemon=True)
            t.start()
            self.threads.append(t)
        return old

    def _gzip_later(self, day, delay):
        time.sleep(delay)
        self.gzip_day(day)

    def gzip_day(self, day):
        folder = os.path.join(self.root, day)
        for name in sorted(os.listdir(folder)):
            if name.endswith(DAY_SUFFIXES):
                subprocess.check_call(["gzip", "-f", os.path.join(folder, name)])

    def wait(self):
        for t in self.threads:
            t.join(10)

    def replace(self, stream, data, fmt="jsonl"):
        """A new inode at the same path, as a reinstall or a cleanup would leave."""
        p = self.path(stream, fmt=fmt)
        tmp = p + ".new"
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, p)
