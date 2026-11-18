"""What one node's input cannot do to the receiver.

The receiver is one process serving every node, under a 512 MB cap, and its loop
gives up after 5 starts in 10 minutes: input from one node that makes it bloat or
crash takes every node's stream down. Frames here are built by hand, as a sender
writes them, and applied to a Mirror or fed to a Link.
"""
import datetime
import gzip
import io
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import zlib
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
FLEET = os.path.dirname(HERE)
sys.path.insert(0, FLEET)
sys.path.insert(0, HERE)

import fleet_stream as fs                      # noqa: E402

HOST = "nodeA"
DAY = "2026-11-10"
KEY = DAY + "/nodeA.exits.jsonl"


def days_from_today(*offsets):
    today = datetime.date.today()
    return [(today + datetime.timedelta(days=n)).isoformat() for n in offsets]


def wait_for(pred, timeout=15.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.05)
    return False


def transport(folder, frames, ping_s=None):
    """argv for a transport that writes `frames` and then stays open: silent, or with
    a ping every `ping_s` seconds, as an idle sender sends them."""
    path = os.path.join(folder, "frames")
    buf = io.BytesIO()
    for h, payload in frames:
        fs.write_frame(buf, dict(h), payload)
    with open(path, "wb") as fh:
        fh.write(buf.getvalue())
    code = ("import sys, time\n"
            "sys.path.insert(0, sys.argv[2])\n"
            "import fleet_stream as fs\n"
            "out = sys.stdout.buffer\n"
            "out.write(open(sys.argv[1], 'rb').read())\n"
            "out.flush()\n"
            "while True:\n"
            "    time.sleep(%r)\n"
            "    if %r:\n"
            "        fs.write_frame(out, {'t': 'ping'})\n"
            "        out.flush()\n") % (ping_s or 60, bool(ping_s))
    return [sys.executable, "-c", code, path, FLEET]


def hello(live=DAY):
    return {"t": "hello", "v": 1, "host": HOST, "live": live}, b""


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
        """A data frame on this test's one zlib stream, as one connection carries them."""
        return self.apply({"t": "data", "path": key, "off": off, "plain": len(plain)},
                          self.deflate(plain), inflater)

    def lone(self, h, payload=b""):
        """Frame `h` as the first of a connection: a data frame's `payload` is its plain
        bytes, deflated on a zlib stream of its own. For frames that are refused, each
        of which ends a connection."""
        if h["t"] == "data":
            h, payload = dict(h, plain=len(payload)), Deflater()(payload)
        return self.m.apply(dict(h), payload, zlib.decompressobj())

    def copy(self, key):
        try:
            return self.base.read_all("mirror/%s/%s" % (HOST, key))
        except FileNotFoundError:
            return None

    def held(self, key=KEY):
        """The plain bytes the copy holds for `key`: its plain file, or its `.gz`."""
        plain, gz = self.copy(key), self.copy(key + ".gz")
        return plain if plain is not None else gzip.decompress(gz) if gz is not None else None

    def verify(self, body, key=KEY):
        """`key` streamed, sealed, and checked against its trailer, as at a day's end."""
        self.data(key, 0, body)
        self.apply({"t": "sealed", "path": key, "size": len(body)})
        self.apply({"t": "trailer", "path": key, "crc": zlib.crc32(body) & 0xffffffff,
                    "isize": len(body)})
        self.assertIn(key, self.m.verified)
        self.assertEqual(self.held(key), body)


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


class TestVerified(MirrorCase):
    """A day checked against the source's gzip trailer stays as it was checked. No
    honest sender resets, extends, or fetches again a verified file."""

    BODY = b'{"real":1}\n' * 1000
    FORGED = b'{"forged":2}\n'

    def forgeries(self):
        gz = gzip.compress(self.FORGED)
        crc = zlib.crc32(self.FORGED) & 0xffffffff
        return [
            {"t": "data", "path": KEY, "off": len(self.BODY)},          # extend it
            {"t": "reset", "path": KEY, "why": "x"},                    # erase it
            {"t": "data", "path": KEY, "off": 0},                       # and refill it
            {"t": "sealed", "path": KEY, "size": len(self.FORGED)},
            {"t": "trailer", "path": KEY, "crc": crc, "isize": len(self.FORGED)},
            {"t": "reset", "path": KEY + ".gz", "why": "x"},
            {"t": "gz", "path": KEY + ".gz", "off": 0, "size": len(gz), "payload": gz},
        ]

    def check(self):
        """Each forgery as the first frame of a connection of its own: a refused frame
        ends one. A `sealed` frame changes nothing, and is refused here only because
        its size is not the copy's; a `trailer` for a verified file is ignored."""
        self.verify(self.BODY)
        refused = []
        for h in self.forgeries():
            payload = h.pop("payload", self.FORGED if h["t"] == "data" else b"")
            try:
                self.lone(h, payload)
            except fs.ProtocolError as e:
                refused.append(h["t"])
                if h["t"] != "sealed":
                    self.assertIn("verified", str(e))
        self.assertEqual(self.held(), self.BODY)
        self.assertIn(KEY, self.m.verified)
        self.assertIn(KEY, fs.Mirror(self.base, HOST).verified)       # the ledger on disk
        self.assertEqual(refused, ["data", "reset", "data", "sealed", "reset", "gz"])

    def test_a_verified_day_cannot_be_extended_erased_or_replaced(self):
        """The review's case: after a day was verified, a data frame at its size was
        accepted, a reset deleted the copy and emptied the ledger, and data, sealed,
        and trailer frames for other content left that content verified."""
        self.check()

    def test_nor_its_gz_with_keep_gz(self):
        """The kit's setting: the sealed day is held as `.gz`, which the same frames
        replaced."""
        self.m = fs.Mirror(self.base, HOST, keep_gz=True)
        self.check()
        self.assertEqual(self.base.listdir("mirror/%s/%s" % (HOST, DAY)),
                         ["nodeA.exits.jsonl.gz"])


class TestDays(MirrorCase):
    def test_a_day_after_tomorrow_or_no_date_is_refused(self):
        """Frames for 9999-12-29 to 9999-12-31 made folders that retention then kept
        in place of the real days. A node may be a day ahead (its clock or time
        zone), no more."""
        today, ahead, later = days_from_today(0, 1, 2)
        for day in (later, "9999-12-31", "2026-02-30", "2026-13-01", "0000-01-01"):
            for h in ({"t": "data", "path": day + "/nodeA.exits.jsonl", "off": 0},
                      {"t": "gz", "path": day + "/nodeA.exits.jsonl.gz", "off": 0, "size": 1}):
                with self.assertRaises(fs.ProtocolError, msg=h["path"]):
                    self.lone(h, b"x")
        self.assertEqual(self.m.days(), [])
        self.assertEqual(self.base.listdir("staging/" + HOST), [])
        self.assertEqual(self.lone({"t": "data", "path": ahead + "/nodeA.exits.jsonl", "off": 0},
                                   b"x\n"), 2)
        self.assertEqual(self.m.days(), [ahead])


class TestRetentionByDate(MirrorCase):
    def test_days_are_kept_by_date_not_by_name(self):
        """The review's case: three folders named 9999-12-29 to 9999-12-31, and one
        retention pass with days=3, deleted all three real days. Days are kept from
        `days` - 1 days ago to tomorrow, by date; anything else dated goes, in the
        copy, in staging, and in the ledger."""
        r = fs.Receiver({"base": self.basedir, "days": 3, "hosts": {}})
        r.base = self.base
        r.links = [fs.Link(HOST, ["true"], self.m, 3, r.counters, r.paused, r.stop)]
        kept = days_from_today(-2, -1, 0, 1)
        gone = days_from_today(-3, -30, 2) + ["9999-12-29", "9999-12-30", "9999-12-31", "2026-02-30"]
        for day in kept + gone:          # by hand: frames for most of these are refused
            os.close(self.base.open_append(self.m.rel(day, "nodeA.exits.jsonl")))
            os.close(self.base.open_append(self.m.part(day, "nodeA.exits.jsonl")))
            self.m.verified.add(day + "/nodeA.exits.jsonl")
        self.m._save_ledger()
        r.retention()
        self.assertEqual(self.m.days(), sorted(kept))
        self.assertEqual(self.base.listdir("staging/" + HOST), sorted(kept))
        self.assertEqual(sorted(fs.Mirror(self.base, HOST).verified),
                         sorted(d + "/nodeA.exits.jsonl" for d in kept))


class LinkCase(MirrorCase):
    """A Link over a transport that writes frames given here."""

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp(prefix="fleet-transport-")
        self.link = None

    def tearDown(self):
        if self.link is not None:
            self.stop.set()
            self.link.close()
            self.thread.join(10)
        shutil.rmtree(self.tmp, ignore_errors=True)
        super().tearDown()

    def start_link(self, frames, ping_s=None, backoff=(5, 5)):
        self.paused, self.stop = threading.Event(), threading.Event()
        self.link = fs.Link(HOST, transport(self.tmp, frames, ping_s), self.m, 3, fs.Counters(),
                            self.paused, self.stop, backoff=backoff)
        self.thread = threading.Thread(target=self.link.run, daemon=True)
        self.thread.start()


class TestRefusalIsReported(LinkCase):
    def test_a_refused_frame_is_the_links_last_error_and_is_logged(self):
        body = b'{"real":1}\n' * 100
        self.verify(body)
        forged = b'{"forged":1}\n'
        with mock.patch.object(fs, "_log") as log:
            self.start_link([hello(), ({"t": "data", "path": KEY, "off": len(body),
                                        "plain": len(forged)}, Deflater()(forged))])
            self.assertTrue(wait_for(lambda: "verified" in (self.link.last_error or "")),
                            self.link.status())
            self.assertTrue(any("verified" in c[0][0] for c in log.call_args_list),
                            log.call_args_list)
        self.assertEqual(self.held(), body)


if __name__ == "__main__":
    unittest.main()
