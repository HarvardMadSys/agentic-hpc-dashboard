"""What one node's input cannot do to the receiver.

The receiver is one process serving every node, under a 512 MB cap, and its loop
gives up after 5 starts in 10 minutes: input from one node that makes it bloat or
crash takes every node's stream down. Frames here are built by hand, as a sender
writes them, and applied to a Mirror or fed to a Link.
"""
import io
import os
import shutil
import sys
import tempfile
import threading
import unittest
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
FLEET = os.path.dirname(HERE)
sys.path.insert(0, FLEET)
sys.path.insert(0, HERE)

import fleet_stream as fs                      # noqa: E402

HOST = "nodeA"
KEY = "2026-11-10/nodeA.exits.jsonl"


class Deflater:
    """One zlib stream across frames, sync-flushed after each, as Sender.send_plain
    makes them. `finish` ends the stream instead, which no sender does."""

    def __init__(self):
        self.z = zlib.compressobj(6)

    def __call__(self, plain, finish=False):
        return self.z.compress(plain) + self.z.flush(zlib.Z_FINISH if finish else zlib.Z_SYNC_FLUSH)


class SpyInflater:
    """A Link's inflater that records how many bytes each decompress() handed back."""

    def __init__(self):
        self.z, self.out = zlib.decompressobj(), []

    def decompress(self, data, max_length=0):
        b = self.z.decompress(data, max_length)
        self.out.append(len(b))
        return b

    def __getattr__(self, name):
        return getattr(self.z, name)


class MirrorCase(unittest.TestCase):
    def setUp(self):
        self.basedir = tempfile.mkdtemp(prefix="fleet-hostile-")
        os.chmod(self.basedir, 0o700)
        self.base = fs.SafeBase.open(self.basedir)
        self.m = fs.Mirror(self.base, HOST)
        self.deflate = Deflater()
        self.inflater = zlib.decompressobj()

    def tearDown(self):
        self.base.close()
        shutil.rmtree(self.basedir, ignore_errors=True)

    def apply(self, h, payload=b"", inflater=None):
        return self.m.apply(dict(h), payload, inflater or self.inflater)

    def data(self, key, off, plain, inflater=None):
        return self.apply({"t": "data", "path": key, "off": off, "plain": len(plain)},
                          self.deflate(plain), inflater)

    def copy(self, key):
        try:
            return self.base.read_all("mirror/%s/%s" % (HOST, key))
        except FileNotFoundError:
            return None


class TestInflateBound(MirrorCase):
    def test_a_small_payload_cannot_inflate_past_the_size_it_states(self):
        """The review's case: 1 MB that inflated to 1 GiB before its stated size (1) was
        compared. Here 64 MiB of zeros in about 64 KB: no more than one byte past the
        stated size ever leaves the inflater, and the copy gets nothing."""
        bomb = self.deflate(bytes(64 << 20))
        self.assertLess(len(bomb), fs.MAX_PAYLOAD)
        spy = SpyInflater()
        with self.assertRaises(fs.ProtocolError):
            self.apply({"t": "data", "path": KEY, "off": 0, "plain": 1}, bomb, spy)
        self.assertLessEqual(max(spy.out), 2)
        self.assertFalse(self.copy(KEY))

    def test_a_frame_says_at_most_chunk_bytes(self):
        """A sender's data frame holds at most CHUNK plain bytes. One that says more, or
        says something that is not a count, is refused before anything is inflated."""
        body = b"x" * (fs.CHUNK + 1)
        for plain in (len(body), -1, "1", None, True, 1.5):
            spy = SpyInflater()
            with self.assertRaises(fs.ProtocolError, msg=repr(plain)):
                self.apply({"t": "data", "path": KEY, "off": 0, "plain": plain},
                           zlib.compress(body), spy)
            self.assertEqual(spy.out, [], repr(plain))
        self.assertFalse(self.copy(KEY))

    def test_a_frame_that_ends_the_zlib_stream_is_refused(self):
        """No sender ends its zlib stream. Once one has ended, each later payload would
        be kept in the inflater (its unused_data), up to 4 MB a frame."""
        with self.assertRaises(fs.ProtocolError):
            self.apply({"t": "data", "path": KEY, "off": 0, "plain": 3},
                       self.deflate(b"abc", finish=True))

    def test_honest_frames_still_apply(self):
        """A full CHUNK of incompressible bytes, an empty frame, then a short one, through
        one inflater, as a Link applies them."""
        first = os.urandom(fs.CHUNK)
        self.assertEqual(self.data(KEY, 0, first), fs.CHUNK)
        self.assertEqual(self.data(KEY, fs.CHUNK, b""), 0)
        self.assertEqual(self.data(KEY, fs.CHUNK, b'{"a":1}\n'), 8)
        self.assertEqual(self.copy(KEY), first + b'{"a":1}\n')


class Pipe(io.BytesIO):
    """A transport's stderr that records the size each readline() asks for."""

    def __init__(self, data):
        super().__init__(data)
        self.asked = []

    def readline(self, size=-1):
        self.asked.append(size)
        return super().readline(size)


class TestStderr(unittest.TestCase):
    def test_a_line_with_no_end_is_read_in_bounded_pieces(self):
        """The review's case: 200 MiB on the transport's stderr with no newline took the
        receiver from 35 MB to 238 MB, as readline() held all of it. Each read asks for
        at most STDERR_LINE bytes; the rest of the long line is dropped, and the next
        line is kept whole."""
        pipe = Pipe(b"A" * 100000 + b"\nHost key verification failed.\n")
        link = fs.Link(HOST, ["true"], None, 3, fs.Counters(), threading.Event(), threading.Event())
        link._drain_stderr(pipe)
        self.assertTrue(pipe.asked)
        self.assertTrue(all(0 < n <= fs.STDERR_LINE for n in pipe.asked), pipe.asked[:3])
        self.assertEqual(list(link.stderr_tail), ["A" * 300, "Host key verification failed."])


if __name__ == "__main__":
    unittest.main()
