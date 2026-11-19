"""What one node's input cannot do to the receiver.

The receiver is one process serving every node, under a 512 MB cap, and its loop
gives up after 5 starts in 10 minutes: input from one node that makes it bloat or
crash takes every node's stream down. Frames here are built by hand, as a sender
writes them, and applied to a Mirror or fed to a Link.
"""
import datetime
import gzip
import io
import json
import os
import shutil
import subprocess
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
from fakecollector import FakeCollector        # noqa: E402
from test_receiver import Rig                  # noqa: E402

SCRIPT = os.path.join(FLEET, "fleet_stream.py")
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


def run_sender(root, hello_line, wait=1.0, ping_s=30.0, during=None):
    """(exit status, [(header, plain or raw payload)]) of a sender run in this process
    over `root`, given `hello_line` (bytes, or a dict sent as JSON), until its stdin
    closes `wait` seconds later, after `during()` if given."""
    if isinstance(hello_line, dict):
        hello_line = (json.dumps(hello_line) + "\n").encode("utf-8")
    r, w = os.pipe()
    inp, out, res = os.fdopen(r, "rb", buffering=0), io.BytesIO(), []
    t = threading.Thread(target=lambda: res.append(fs.send_main(root, inp, out, 0.05, ping_s, 0.1)),
                         daemon=True)
    t.start()
    try:
        os.write(w, hello_line)
    except BrokenPipeError:              # it stopped reading, as it should have
        pass
    if during is not None:
        during()
    time.sleep(wait)
    os.close(w)
    t.join(10)
    inp.close()
    out.seek(0)
    frames, z = [], zlib.decompressobj()
    while True:
        f = fs.read_frame(out)
        if f is None:
            return (res[0] if res else None), frames
        h, payload = f
        frames.append((h, z.decompress(payload) if h.get("t") == "data" else payload))


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
        r.links[0].live = kept[2]        # the node's day is this host's
        r.retention()
        self.assertEqual(self.m.days(), sorted(kept))
        self.assertEqual(self.base.listdir("staging/" + HOST), sorted(kept))
        self.assertEqual(sorted(fs.Mirror(self.base, HOST).verified),
                         sorted(d + "/nodeA.exits.jsonl" for d in kept))

    def test_the_days_a_node_sends_are_kept_whichever_way_its_day_runs(self):
        """1a1a11a's case: a sender's window starts `days` - 1 days before its own live
        day, retention's before this host's today. For a node a day behind (its time
        zone, folders named in UTC, around midnight) retention deleted the oldest day
        it sends, and each connection sent that day again. Retention keeps one day
        more for a node behind; a node a day ahead sends nothing before this host's
        window. A live day not known yet (no hello since a restart), further back, or
        no date counts as a day behind, no more: a hello cannot turn retention off."""
        r = fs.Receiver({"base": self.basedir, "days": 3, "hosts": {}})
        r.base = self.base
        r.links = [fs.Link(HOST, ["true"], self.m, 3, r.counters, r.paused, r.stop)]
        behind, behind2, ahead = days_from_today(-1, -2, 1)
        a_day_behind = days_from_today(-3, -2, -1, 0, 1)
        for live, kept in ((behind, a_day_behind),
                           (ahead, days_from_today(-2, -1, 0, 1)),
                           (None, a_day_behind),
                           (behind2, a_day_behind),
                           ("2000-01-01", a_day_behind),
                           ("0001-01-01", a_day_behind),
                           ("no day", a_day_behind),
                           (5, a_day_behind)):
            for day in days_from_today(-4, -3, -2, -1, 0, 1) + ["2000-01-01"]:
                os.close(self.base.open_append(self.m.rel(day, "nodeA.exits.jsonl")))
                self.m.verified.add(day + "/nodeA.exits.jsonl")
            r.links[0].live = live
            r.retention()
            self.assertEqual(self.m.days(), kept, live)
            self.assertEqual(sorted(self.m.verified), [d + "/nodeA.exits.jsonl" for d in kept], live)


class TestSkewedNode(unittest.TestCase):
    def test_a_node_a_day_behind_is_not_sent_again_at_each_connection(self):
        """The same, end to end: the oldest day such a node sends stays checked in the
        copy through a retention pass, and the next connection sends none of it."""
        rig = Rig(days=3)
        try:
            d = days_from_today(-3, -2, -1)      # the node's live day is this host's yesterday
            c = FakeCollector(rig.src, day=d[0])
            c.append("exits", b'{"d":0}\n' * 100)
            c.roll(d[1], gzip=True)
            c.append("exits", b'{"d":1}\n' * 100)
            c.roll(d[2], gzip=True)
            c.append("exits", b'{"d":2}\n')
            rig.start()
            sealed = [c.key(s, day) for day in d[:2] for s in ("exits", "snapshot")]
            # The hello names the live day before its first bytes are applied, and
            # those make its folder: wait for the folder too, so that the retention
            # pass below meets all three days.
            self.assertTrue(wait_for(lambda: all(k in rig.mirror.verified for k in sealed)
                                     and rig.link.live == d[2]
                                     and os.path.isdir(os.path.join(rig.mdir, d[2]))),
                            "%s; the copy holds %s" % (
                                rig.link.status(),
                                sorted(os.listdir(rig.mdir)) if os.path.isdir(rig.mdir)
                                else "no folder"))
            r = fs.Receiver({"base": rig.basedir, "days": 3, "hosts": {}})
            r.base, r.links = rig.base, [rig.link]
            r.retention()
            self.assertEqual(sorted(os.listdir(rig.mdir)), d)
            gz = rig.counters.total(HOST, "gz")
            rig.halt()
            rig.start()
            self.assertTrue(wait_for(lambda: rig.link.connected), rig.link.status())
            time.sleep(1.0)
            self.assertEqual(rig.counters.total(HOST, "gz"), gz)
            self.assertIsNone(rig.link.last_error)
        finally:
            rig.cleanup()

    def test_the_hello_lists_every_day_the_copy_keeps(self):
        """1a1a11a's case: the hello listed the copy's newest `days` folders, while
        retention keeps by date, tomorrow included. With a folder for tomorrow in the
        copy (left from a time the node ran ahead), the oldest verified day dropped out
        of the hello, the sender sent it again, and the receiver refused it at every
        connection."""
        rig = Rig(days=3)
        try:
            d = days_from_today(-2, -1, 0, 1)
            c = FakeCollector(rig.src, day=d[0])
            c.append("exits", b'{"d":0}\n' * 100)
            c.roll(d[1], gzip=True)
            c.append("exits", b'{"d":1}\n' * 100)
            c.roll(d[2], gzip=True)
            c.append("exits", b'{"d":2}\n')
            rig.start()
            sealed = [c.key(s, day) for day in d[:2] for s in ("exits", "snapshot")]
            self.assertTrue(wait_for(lambda: all(k in rig.mirror.verified for k in sealed)),
                            rig.link.status())
            rig.halt()
            os.makedirs(os.path.join(rig.mdir, d[3]))
            append(os.path.join(rig.mdir, d[3], "nodeA.cluster.example.org.exits.jsonl"), b"")
            gz = rig.counters.total(HOST, "gz")
            rig.start()
            self.assertTrue(wait_for(lambda: rig.link.connected), rig.link.status())
            time.sleep(1.0)
            self.assertIsNone(rig.link.last_error)
            self.assertEqual(len(rig.link.attempts), 1)
            self.assertEqual(rig.counters.total(HOST, "gz"), gz)
        finally:
            rig.cleanup()


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

    def start_link(self, frames, ping_s=None, backoff=(5, 5), stall_s=None):
        self.paused, self.stop = threading.Event(), threading.Event()
        self.link = fs.Link(HOST, transport(self.tmp, frames, ping_s), self.m, 3, fs.Counters(),
                            self.paused, self.stop, backoff=backoff)
        if stall_s is not None:
            self.link.stall_s = stall_s
        self.down0 = self.link.down_since
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
        self.assertFalse(self.link.connected)
        self.assertEqual(self.link.down_since, self.down0)


class TestStall(LinkCase):
    def test_a_link_silent_after_its_hello_is_closed_and_stays_down(self):
        """The review's case: a transport that sent a hello and then nothing showed
        connected: true, its lag_s growing for a day, and no alert. The read now ends
        after stall_s and the link reconnects; nothing after a hello ever came, so it
        never counted as up, and down_since still says when it went down."""
        self.start_link([hello()], backoff=(0.1, 0.2), stall_s=0.3)
        self.assertTrue(wait_for(lambda: len(self.link.attempts) >= 3), self.link.status())
        st = self.link.status()
        self.assertFalse(st["connected"])
        self.assertIn("Stalled", st["last_error"] or "")
        self.assertEqual(st["down_since"], self.down0)

    def test_an_idle_sender_that_pings_is_not_stalled(self):
        self.start_link([hello()], ping_s=0.1, backoff=(0.1, 0.2), stall_s=0.5)
        self.assertTrue(wait_for(lambda: self.link.connected), self.link.status())
        time.sleep(1.5)
        st = self.link.status()
        self.assertTrue(st["connected"], st)
        self.assertIsNone(st["down_since"])
        self.assertEqual(len(self.link.attempts), 1)

    def test_a_long_frame_is_not_a_stall(self):
        """Only waiting for the sender counts. Checking a big sealed day against its
        trailer, and gzipping it with keep_gz, can take minutes; here each write the
        frames make takes longer than stall_s, and the link stays up."""
        def slow(fd, data):
            time.sleep(0.6)
            return os.write(fd, data)
        self.m = fs.Mirror(self.base, HOST, write=slow, keep_gz=True)
        body = b'{"k":1}\n' * 1000
        crc = zlib.crc32(body) & 0xffffffff
        self.start_link([hello(),
                         ({"t": "data", "path": KEY, "off": 0, "plain": len(body)}, Deflater()(body)),
                         ({"t": "sealed", "path": KEY, "size": len(body)}, b""),
                         ({"t": "trailer", "path": KEY, "crc": crc, "isize": len(body)}, b"")],
                        ping_s=0.1, backoff=(0.1, 0.2), stall_s=0.3)
        self.assertTrue(wait_for(lambda: KEY in self.m.verified), self.link.status())
        self.assertEqual(len(self.link.attempts), 1, self.link.last_error)
        self.assertTrue(self.link.connected)


class TestSenderPings(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="fleet-src-")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_the_sender_pings_while_it_inflates_what_the_copy_has(self):
        """A sealed day the copy has most of comes from the node's `.gz`: the sender
        inflates the part the copy has, sending nothing, before the rest. For a big
        day that was minutes with no ping, longer than a receiver now waits."""
        os.makedirs(os.path.join(self.root, "2026-11-11"))
        os.makedirs(os.path.join(self.root, DAY))
        body = (b'{"pid":12345,"cmd":"python train.py --epochs 10"}\n' * 260000)[:3 * fs.CHUNK]
        with gzip.open(os.path.join(self.root, DAY, "nodeA.exits.jsonl.gz"), "wb") as fh:
            fh.write(body)
        have = {KEY: {"size": len(body) - 10, "verified": False}}
        _, frames = run_sender(self.root, {"v": 1, "days": 3, "have": have}, ping_s=0.0)
        first = next(i for i, (h, _) in enumerate(frames) if h.get("path") == KEY)
        self.assertEqual([h["t"] for h, _ in frames[:first]], ["hello", "ping", "ping"])
        self.assertEqual(frames[first][0]["off"], len(body) - 10)
        self.assertEqual(frames[first][1], body[-10:])


def append(path, data):
    with open(path, "ab") as fh:
        fh.write(data)


def unprivileged():
    """A sender's preexec_fn: as nobody when these tests run as root, whom a folder's
    mode does not stop."""
    if os.geteuid() == 0:
        os.setgroups([])
        os.setgid(65534)
        os.setuid(65534)


def data_for(frames, key):
    return b"".join(p for h, p in frames if h["t"] == "data" and h["path"] == key)


class TestSenderDays(unittest.TestCase):
    """Which day folders on the node the sender follows and sends."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="fleet-src-")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def plain(self, day, body=b""):
        path = os.path.join(self.root, day, "nodeA.exits.jsonl")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        append(path, body)
        return path

    def sealed(self, day, body):
        os.makedirs(os.path.join(self.root, day))
        with open(os.path.join(self.root, day, "nodeA.exits.jsonl.gz"), "wb") as fh:
            fh.write(gzip.compress(body))

    def test_a_folder_dated_later_does_not_take_the_live_stream(self):
        """The review's case: an empty `2036-01-01` folder beside today's made the hello
        say live=2036-01-01, and today's appends stopped crossing. The live day is the
        newest real date no later than the node's own tomorrow; a name that ends in a
        newline, or that is no date, is no day."""
        today, later = days_from_today(0, 2)
        f = self.plain(today, b'{"line":1}\n')
        for name in ("2036-01-01", later, today + "\n"):
            os.mkdir(os.path.join(self.root, name))
        self.plain("2026-02-30", b'{"no":"date"}\n')

        def more():
            for i in (2, 3):
                time.sleep(0.4)
                append(f, b'{"line":%d}\n' % i)
        _, frames = run_sender(self.root, {"v": 1, "days": 8, "have": {}}, during=more)
        self.assertEqual(frames[0][0]["live"], today)
        key = today + "/nodeA.exits.jsonl"
        self.assertEqual(data_for(frames, key), b'{"line":1}\n{"line":2}\n{"line":3}\n')
        self.assertEqual(set(h["path"] for h, _ in frames if "path" in h), {key})

    def test_sealed_days_are_sent_within_days_of_the_live_one(self):
        """The receiver keeps days by date. A node with a gap in its folders has old
        days among its newest `days`; sent, they would be dropped there and sent again
        at each connection."""
        old, yesterday, today = days_from_today(-10, -1, 0)
        self.sealed(old, b'{"old":1}\n')
        self.sealed(yesterday, b'{"y":1}\n')
        self.plain(today, b'{"t":1}\n')
        _, frames = run_sender(self.root, {"v": 1, "days": 3, "have": {}})
        days = set(h["path"].split("/")[0] for h, _ in frames if "path" in h)
        self.assertEqual(days, {yesterday, today})

    def test_a_live_file_the_copy_holds_verified_is_not_sent(self):
        """The receiver refuses to change a verified file, so the sender never sends one,
        live day or not."""
        today = days_from_today(0)[0]
        self.plain(today, b'{"t":1}\n')
        key = today + "/nodeA.exits.jsonl"
        _, frames = run_sender(self.root, {"v": 1, "days": 3, "have": {key: {"verified": True}}})
        self.assertEqual([h["t"] for h, _ in frames if h["t"] != "ping"], ["hello"])


class TestSenderReadsOnlyTheCapture(unittest.TestCase):
    """What the sender reads under --root, and what it takes from the receiver's hello."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fleet-src-")
        self.root = os.path.join(self.tmp, "root")
        self.outside = os.path.join(self.tmp, "outside")
        os.mkdir(self.root)
        os.mkdir(self.outside)
        self.fifo = None

    def tearDown(self):
        if self.fifo:                    # a sender stuck opening it to read is let go
            try:
                os.close(os.open(self.fifo, os.O_WRONLY | os.O_NONBLOCK))
            except OSError:
                pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_links_and_fifos_under_root_are_not_followed(self):
        """The review's case: a link in the live day and one in a sealed day, both to a
        file outside --root, and a day folder that was itself a link to another
        folder: the sender sent all three. A live file swapped for a link was opened
        through it too, and a FIFO named like a day-file would hold the sender up."""
        before, yesterday, today = days_from_today(-2, -1, 0)
        secret = os.path.join(self.outside, "secret.txt")
        append(secret, b"SECRET-OUTSIDE-ROOT\n")
        other = os.path.join(self.outside, "other")
        os.mkdir(other)
        append(os.path.join(other, "z.jsonl"), b"FROM-A-LINKED-FOLDER\n")
        for day in (yesterday, today):
            os.mkdir(os.path.join(self.root, day))
        live = os.path.join(self.root, today, "nodeA.exits.jsonl")
        append(live, b'{"real":1}\n')
        os.symlink(secret, os.path.join(self.root, today, "leak.jsonl"))
        os.symlink(secret, os.path.join(self.root, yesterday, "leak.jsonl"))
        os.symlink(other, os.path.join(self.root, before))
        self.fifo = os.path.join(self.root, today, "fifo.jsonl")
        os.mkfifo(self.fifo)

        def swap():                      # the live file, swapped for a link
            time.sleep(0.4)
            os.symlink(secret, live + ".tmp")
            os.replace(live + ".tmp", live)
        res, frames = run_sender(self.root, {"v": 1, "days": 5, "have": {}}, during=swap)
        sent = b"".join(p for h, p in frames if h["t"] in ("data", "gz"))
        self.assertNotIn(b"SECRET", sent)
        self.assertNotIn(b"FROM-A-LINKED", sent)
        key = today + "/nodeA.exits.jsonl"
        self.assertEqual(set(h["path"] for h, _ in frames if "path" in h), {key})
        self.assertEqual(data_for(frames, key), b'{"real":1}\n')
        self.assertEqual(res, 0)

    def test_a_day_folder_it_may_not_read_is_an_error(self):
        """Codex's case: the sending account can list --root but not read a day's
        folder. The sender sent hello and pings and exited 0, so the receiver showed a
        healthy link while none of that day came. Only a folder that is gone, is not
        a folder, or is a link is passed over; any other error stops the sender with an
        error frame, as before. Today's folder, then a sealed day's."""
        os.chmod(self.tmp, 0o755)
        os.chmod(self.root, 0o755)
        yesterday, today = days_from_today(-1, 0)
        for day in (yesterday, today):
            os.mkdir(os.path.join(self.root, day), 0o755)
            append(os.path.join(self.root, day, "nodeA.exits.jsonl"), b'{"a":1}\n')
        for locked in (today, yesterday):
            folder = os.path.join(self.root, locked)
            os.chmod(folder, 0)
            try:
                p = subprocess.Popen([sys.executable, "-I", SCRIPT, "send", "--root", self.root,
                                      "--poll", "0.05", "--ping", "0.2"],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     preexec_fn=unprivileged)
                p.stdin.write(b'{"v": 1, "days": 3, "have": {}}\n')
                p.stdin.flush()
                time.sleep(1.0)
                try:
                    p.stdin.close()
                except BrokenPipeError:
                    pass
                frames = []
                while True:
                    f = fs.read_frame(p.stdout)
                    if f is None:
                        break
                    frames.append(f[0])
                code = p.wait(10)
                p.stdout.close()
            finally:
                os.chmod(folder, 0o755)
            errors = [h["msg"] for h in frames if h["t"] == "error"]
            self.assertEqual(len(errors), 1, (locked, [h["t"] for h in frames]))
            self.assertIn("Permission denied", errors[0])
            self.assertIn(locked, errors[0])          # the receiver's last_error names it
            self.assertEqual(code, 1, locked)
            # Fatal for a sealed day too, so the live day stops with it: no frame lets a
            # sender report an error and go on.
            self.assertFalse([h for h in frames if h.get("path", "").startswith(today)], locked)

    def test_head_n_from_the_hello_is_kept_to_head(self):
        """The review's case: head_n=-1 read a whole 64 MiB file, and head_n=10**12 ran
        out of memory. It is taken as no less than 0 and no more than HEAD."""
        class File(io.BytesIO):
            def __init__(self, data):
                super().__init__(data)
                self.asked = []

            def read(self, n=-1):
                self.asked.append(n)
                return super().read(n)
        with open(os.devnull, "rb") as inp:
            s = fs.Sender(self.root, inp, io.BytesIO())
            for head_n in (-1, 10 ** 12, 100):
                fh = File(b"x" * (2 * fs.HEAD))
                s.have = {"k": {"size": 5, "head": 0, "head_n": head_n}}
                s.head_differs("k", fh)
                self.assertTrue(fh.asked)
                self.assertTrue(all(0 <= n <= fs.HEAD for n in fh.asked), (head_n, fh.asked))

    def test_a_size_below_zero_is_taken_as_zero(self):
        """The review's case: a size of -5 in the hello made the sender seek to -5, and
        it stopped with an error."""
        today = days_from_today(0)[0]
        os.mkdir(os.path.join(self.root, today))
        append(os.path.join(self.root, today, "nodeA.exits.jsonl"), b'{"a":1}\n')
        key = today + "/nodeA.exits.jsonl"
        res, frames = run_sender(self.root, {"v": 1, "days": 3,
                                             "have": {key: {"size": -5, "verified": False}}})
        self.assertEqual([h for h, _ in frames if h["t"] == "error"], [])
        self.assertEqual(data_for(frames, key), b'{"a":1}\n')

    def test_a_hello_line_is_capped(self):
        """The hello was read whole, however long, before the sender answered. Past
        MAX_HELLO bytes it now answers with an error and stops reading."""
        p = subprocess.Popen([sys.executable, SCRIPT, "send", "--root", self.root],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        stopped, chunk = False, b"x" * (1 << 16)
        try:
            for _ in range(2 * fs.MAX_HELLO // len(chunk)):
                p.stdin.write(chunk)
                p.stdin.flush()
        except BrokenPipeError:
            stopped = True
        try:
            p.stdin.close()              # the end of the input, where it still reads
        except BrokenPipeError:
            pass
        try:
            f = fs.read_frame(p.stdout)
            code = p.wait(10)
        finally:
            p.stdout.close()
            if p.poll() is None:
                p.kill()
                p.wait(5)
        self.assertTrue(stopped)
        self.assertEqual(code, 1)
        self.assertEqual(f[0]["t"], "error")
        self.assertIn("hello longer", f[0]["msg"])


if __name__ == "__main__":
    unittest.main()
