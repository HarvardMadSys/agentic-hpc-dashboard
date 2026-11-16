"""The sender, watched through the frames it writes.

Each test runs `fleet_stream.py send` as its own process over a root written by
FakeCollector, writes the hello a receiver would, and decodes what comes back.
"""
import gzip
import io
import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
FLEET = os.path.dirname(HERE)
sys.path.insert(0, FLEET)
sys.path.insert(0, HERE)

import fleet_stream as fs                      # noqa: E402
from fakecollector import FakeCollector        # noqa: E402

SCRIPT = os.path.join(FLEET, "fleet_stream.py")
HOST = "nodeA.cluster.example.org"


class SenderProc:
    """One sender process, with its frames decoded on a reader thread.

    Every `data` payload goes through one decompressor in arrival order, as a
    receiver's would, and each frame is queued as (header, payload, plain)."""

    def __init__(self, root, have=None, days=3):
        self.p = subprocess.Popen(
            [sys.executable, SCRIPT, "send", "--root", root,
             "--poll", "0.05", "--ping", "0.5", "--trailer-poll", "0.1"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        hello = {"v": 1, "days": days, "have": have or {}}
        self.p.stdin.write((json.dumps(hello) + "\n").encode("utf-8"))
        self.p.stdin.flush()
        self.q = queue.Queue()
        self.z = zlib.decompressobj()
        self.t = threading.Thread(target=self._read, daemon=True)
        self.t.start()

    def _read(self):
        try:
            while True:
                f = fs.read_frame(self.p.stdout)
                if f is None:
                    break
                h, payload = f
                plain = self.z.decompress(payload) if h.get("t") == "data" else b""
                self.q.put((h, payload, plain))
        finally:
            self.q.put(None)

    def frames_until(self, pred, timeout=8.0):
        """Frames up to and including the first one `pred` accepts."""
        out, end = [], time.time() + timeout
        while time.time() < end:
            try:
                f = self.q.get(timeout=max(0.01, end - time.time()))
            except queue.Empty:
                break
            if f is None:
                break
            out.append(f)
            if pred(f):
                return out
        raise AssertionError("no matching frame; got %r" % [x[0] for x in out])

    def frames_for(self, seconds):
        out, end = [], time.time() + seconds
        while time.time() < end:
            try:
                f = self.q.get(timeout=max(0.01, end - time.time()))
            except queue.Empty:
                continue
            if f is None:
                break
            out.append(f)
        return out

    def close(self):
        try:
            self.p.stdin.close()
        except OSError:
            pass
        try:
            self.p.wait(5)
        except subprocess.TimeoutExpired:
            self.p.kill()
            self.p.wait(5)
        self.p.stdout.close()


def data_of(frames, key):
    return b"".join(plain for h, _, plain in frames if h.get("t") == "data" and h.get("path") == key)


def got_bytes(key, n):
    """A predicate: the data frames seen so far for `key` add up to `n` bytes."""
    seen = [0]

    def pred(f):
        h = f[0]
        if h.get("t") == "data" and h.get("path") == key:
            seen[0] += h["plain"]
        return seen[0] >= n
    return pred


class Base(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="fleet-src-")
        self.procs = []

    def tearDown(self):
        for p in self.procs:
            p.close()
        shutil.rmtree(self.root, ignore_errors=True)

    def sender(self, **kw):
        p = SenderProc(self.root, **kw)
        self.procs.append(p)
        return p


class TestFrames(unittest.TestCase):
    def test_frame_roundtrip(self):
        buf = io.BytesIO()
        n = fs.write_frame(buf, {"t": "data", "path": "2026-11-11/f.jsonl", "off": 3}, b"xyz")
        raw = buf.getvalue()
        self.assertEqual(n, len(raw))
        h, payload = fs.read_frame(io.BytesIO(raw))
        self.assertEqual(h, {"t": "data", "path": "2026-11-11/f.jsonl", "off": 3, "len": 3})
        self.assertEqual(payload, b"xyz")
        self.assertIsNone(fs.read_frame(io.BytesIO(b"")))
        with self.assertRaises(fs.ProtocolError):
            fs.read_frame(io.BytesIO(raw[:-1]))


class TestSender(Base):
    def test_first_frame_is_hello(self):
        FakeCollector(self.root)
        s = self.sender()
        h = s.frames_until(lambda f: True)[0][0]
        self.assertEqual(h["t"], "hello")
        self.assertEqual(h["live"], "2026-11-11")
        self.assertEqual(h["v"], 1)

    def test_live_bytes_follow(self):
        c = FakeCollector(self.root)
        s = self.sender()
        line = b'{"a":1}\n'
        frames = []
        for i in range(3):
            c.append("exits", line)
            frames += s.frames_until(got_bytes(c.key("exits"), len(line)))
            time.sleep(0.2)
        self.assertEqual(data_of(frames, c.key("exits")), line * 3)
        offs = [h["off"] for h, _, _ in frames if h.get("t") == "data" and h["path"] == c.key("exits")]
        plains = [h["plain"] for h, _, _ in frames if h.get("t") == "data" and h["path"] == c.key("exits")]
        self.assertEqual(offs, [sum(plains[:i]) for i in range(len(plains))])

    def test_partial_line_streams_as_is(self):
        c = FakeCollector(self.root)
        c.append("exits", b'{"a"')
        s = self.sender()
        frames = s.frames_until(got_bytes(c.key("exits"), 4))
        self.assertEqual(data_of(frames, c.key("exits")), b'{"a"')

    def test_resume_from_have(self):
        c = FakeCollector(self.root)
        body = b"0123456789abcdefghij"
        c.append("exits", body)
        s = self.sender(have={c.key("exits"): {"size": 5, "verified": False}})
        frames = s.frames_until(got_bytes(c.key("exits"), 15))
        first = [h for h, _, _ in frames if h.get("t") == "data" and h["path"] == c.key("exits")][0]
        self.assertEqual(first["off"], 5)
        self.assertEqual(data_of(frames, c.key("exits")), body[5:])

    def test_rollover_seals_then_trailer(self):
        c = FakeCollector(self.root)
        old = b'{"old":1}\n' * 50
        c.append("exits", old)
        s = self.sender()
        s.frames_until(got_bytes(c.key("exits"), len(old)))
        old_key = c.key("exits")
        c.roll("2026-11-12", gzip="thread", delay=0.5)
        c.append("exits", b'{"new":1}\n')
        frames = s.frames_until(lambda f: f[0].get("t") == "trailer" and f[0]["path"] == old_key)
        kinds = [(h["t"], h.get("path")) for h, _, _ in frames]
        sealed = [h for h, _, _ in frames if h["t"] == "sealed" and h["path"] == old_key][0]
        self.assertEqual(sealed["size"], len(old))
        i_sealed = kinds.index(("sealed", old_key))
        i_new = kinds.index(("data", c.key("exits")))
        i_trailer = kinds.index(("trailer", old_key))
        self.assertLess(i_sealed, i_trailer)
        self.assertLess(i_new, i_trailer)
        trailer = frames[i_trailer][0]
        self.assertEqual(trailer["crc"], zlib.crc32(old) & 0xffffffff)
        self.assertEqual(trailer["isize"], len(old) % 2 ** 32)

    def test_missing_sealed_day_sends_gz(self):
        c = FakeCollector(self.root, day="2026-11-10")
        old = b'{"x":2}\n' * 100
        c.append("exits", old)
        old_key = c.key("exits")
        c.roll("2026-11-11", gzip=True)
        with open(c.path("exits", "2026-11-10") + ".gz", "rb") as fh:
            gz_bytes = fh.read()
        s = self.sender()
        frames = s.frames_until(lambda f: f[0].get("t") == "trailer" and f[0]["path"] == old_key)
        joined = b"".join(p for h, p, _ in frames if h["t"] == "gz" and h["path"] == old_key + ".gz")
        self.assertEqual(joined, gz_bytes)
        self.assertEqual(gzip.decompress(joined), old)

    def test_partial_sealed_day_from_gz(self):
        c = FakeCollector(self.root, day="2026-11-10")
        old = b'{"y":3}\n' * 40
        c.append("exits", old)
        old_key = c.key("exits")
        c.roll("2026-11-11", gzip=True)
        s = self.sender(have={old_key: {"size": 10, "verified": False}})
        frames = s.frames_until(lambda f: f[0].get("t") == "trailer" and f[0]["path"] == old_key)
        data_frames = [h for h, _, _ in frames if h["t"] == "data" and h["path"] == old_key]
        self.assertEqual(data_frames[0]["off"], 10)
        self.assertEqual(data_of(frames, old_key), old[10:])
        kinds = [(h["t"], h.get("path")) for h, _, _ in frames]
        self.assertLess(kinds.index(("sealed", old_key)), kinds.index(("trailer", old_key)))
        sealed = [h for h, _, _ in frames if h["t"] == "sealed" and h["path"] == old_key][0]
        self.assertEqual(sealed["size"], len(old))

    def test_verified_day_skipped(self):
        c = FakeCollector(self.root, day="2026-11-10")
        c.append("exits", b'{"z":1}\n')
        keys = [c.key("exits"), c.key("snapshot")]
        c.roll("2026-11-11", gzip=True)
        have = {k: {"size": 8 if "exits" in k else 0, "verified": True} for k in keys}
        s = self.sender(have=have)
        frames = s.frames_for(1.0)
        self.assertFalse([h for h, _, _ in frames if str(h.get("path", "")).startswith("2026-11-10/")])

    def test_late_file_picked_up(self):
        c = FakeCollector(self.root, streams=("exits",))
        s = self.sender()
        s.frames_until(lambda f: f[0]["t"] == "hello")
        time.sleep(0.3)
        c.append("snapshot", b'{"snap":1}\n')
        frames = s.frames_until(got_bytes(c.key("snapshot"), 11))
        self.assertEqual(data_of(frames, c.key("snapshot")), b'{"snap":1}\n')

    def test_replaced_file_resets(self):
        c = FakeCollector(self.root)
        c.append("exits", b"AAAAAAAAAA\n")
        s = self.sender()
        s.frames_until(got_bytes(c.key("exits"), 11))
        c.replace("exits", b"BB\n")
        frames = s.frames_until(lambda f: f[0]["t"] == "reset" and f[0]["path"] == c.key("exits"))
        after = s.frames_until(got_bytes(c.key("exits"), 3))
        first = [h for h, _, _ in after if h["t"] == "data" and h["path"] == c.key("exits")][0]
        self.assertEqual(first["off"], 0)
        self.assertEqual(data_of(after, c.key("exits")), b"BB\n")
        self.assertTrue(frames)

    def test_head_mismatch_resets(self):
        c = FakeCollector(self.root)
        body = b"NEWCONTENT" * 4
        c.append("exits", body)
        have = {c.key("exits"): {"size": 10, "verified": False,
                                 "head": zlib.crc32(b"OLDOLDOLDO") & 0xffffffff, "head_n": 10}}
        s = self.sender(have=have)
        frames = s.frames_until(lambda f: f[0]["t"] == "reset" and f[0]["path"] == c.key("exits"))
        after = s.frames_until(got_bytes(c.key("exits"), len(body)))
        first = [h for h, _, _ in after if h["t"] == "data" and h["path"] == c.key("exits")][0]
        self.assertEqual(first["off"], 0)
        self.assertEqual(data_of(after, c.key("exits")), body)
        self.assertTrue(frames)

    def test_head_match_resumes(self):
        c = FakeCollector(self.root)
        body = b"0123456789abcdefghij"
        c.append("exits", body)
        have = {c.key("exits"): {"size": 10, "verified": False,
                                 "head": zlib.crc32(body[:10]) & 0xffffffff, "head_n": 10}}
        s = self.sender(have=have)
        frames = s.frames_until(got_bytes(c.key("exits"), 10))
        self.assertFalse([h for h, _, _ in frames if h["t"] == "reset"])
        self.assertEqual(data_of(frames, c.key("exits")), body[10:])

    def test_chunks_are_bounded(self):
        c = FakeCollector(self.root, day="2026-11-10")
        c.append("exits", os.urandom(6 << 20))
        gz_key = c.key("exits") + ".gz"
        c.roll("2026-11-11", gzip=True)
        big = (b'{"pid":12345,"cmd":"python train.py --epochs 10"}\n' * 210000)[:10 << 20]
        c.append("exits", big)
        s = self.sender()
        frames = s.frames_until(got_bytes(c.key("exits"), len(big)), timeout=60)
        data_frames = [h for h, _, _ in frames if h["t"] == "data"]
        gz_frames = [h for h, _, _ in frames if h["t"] == "gz" and h["path"] == gz_key]
        self.assertTrue(gz_frames)
        self.assertTrue(all(h["plain"] <= fs.CHUNK for h in data_frames))
        self.assertTrue(all(h["len"] <= fs.CHUNK for h in gz_frames))
        self.assertEqual(data_of(frames, c.key("exits")), big)

    def test_missing_root_sends_error(self):
        s = SenderProc(os.path.join(self.root, "no-such-dir"))
        self.procs.append(s)
        frames = s.frames_until(lambda f: True)
        self.assertEqual(frames[0][0]["t"], "error")
        self.assertEqual(s.p.wait(5), 1)

    def test_exits_on_stdin_eof(self):
        FakeCollector(self.root)
        s = self.sender()
        s.frames_until(lambda f: f[0]["t"] == "hello")
        s.p.stdin.close()
        self.assertEqual(s.p.wait(2), 0)

    def test_ping_when_idle(self):
        FakeCollector(self.root)
        s = self.sender()
        frames = s.frames_for(1.2)
        self.assertTrue([h for h, _, _ in frames if h["t"] == "ping"])


if __name__ == "__main__":
    unittest.main()
