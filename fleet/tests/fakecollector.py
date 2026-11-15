"""Writes day files the way the collector (collector/ebpf_trace.py) does.

`<root>/<YYYY-MM-DD>/<host>.<stream>.jsonl`, appended to in place. At a
rollover the collector opens the new day's pair first and then runs
`nice gzip -f` on the old pair, which writes `<file>.gz` and only then unlinks
the plain file. `roll(gzip="thread")` reproduces that delay.
"""
import os
import subprocess
import threading
import time

STREAMS = ("exits", "snapshot")


class FakeCollector:
    def __init__(self, root, host="nodeA.cluster.example.org", day="2026-11-11", streams=STREAMS):
        self.root, self.host, self.day = root, host, day
        os.makedirs(os.path.join(root, day), exist_ok=True)
        for s in streams:
            open(self.path(s), "ab").close()
        self.threads = []

    def path(self, stream, day=None):
        return os.path.join(self.root, day or self.day, "%s.%s.jsonl" % (self.host, stream))

    def key(self, stream, day=None):
        """The name a stream frame carries for this file: `<day>/<file>`."""
        return "%s/%s.%s.jsonl" % (day or self.day, self.host, stream)

    def append(self, stream, data, day=None):
        with open(self.path(stream, day), "ab") as fh:
            fh.write(data)

    def read(self, stream, day=None):
        with open(self.path(stream, day), "rb") as fh:
            return fh.read()

    def roll(self, new_day, gzip=True, delay=0.5):
        """Open the new day's pair, then seal the old one. `gzip`: True runs
        gzip now, "thread" runs it after `delay` seconds, False not at all."""
        old = self.day
        os.makedirs(os.path.join(self.root, new_day), exist_ok=True)
        self.day = new_day
        for s in STREAMS:
            open(self.path(s), "ab").close()
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
            if name.endswith(".jsonl"):
                subprocess.check_call(["gzip", "-f", os.path.join(folder, name)])

    def wait(self):
        for t in self.threads:
            t.join(10)

    def replace(self, stream, data):
        """A new inode at the same path, as a reinstall or a cleanup would leave."""
        p = self.path(stream)
        tmp = p + ".new"
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, p)
