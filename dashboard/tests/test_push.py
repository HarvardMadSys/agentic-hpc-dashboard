"""The push to the page, and the service's account of itself.

A websocket reader that stops reading must hold up nothing: each reader has its
own sender with a mailbox of one frame per kind (`app.Reader`), so the ingest
loop only posts, and a reader that takes no frame within the send timeout is
dropped. `/api/health` says `ok` false once the tail stops moving, and each
background loop says its last error.

Reproduced before the fix (the review's r9): a raw socket that completed the
handshake and never read froze the ingest for the 125 s it was watched, a
well-behaved reader got 2 frames in that time, and `/api/health` said `ok`
with a `last_poll` 126 s old.

    cd dashboard && .venv/bin/python -m unittest tests.test_push -q
"""
import asyncio
import base64
import io
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
import warnings
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import uvicorn                                     # noqa: E402
from websockets.exceptions import ConnectionClosed     # noqa: E402
from websockets.sync.client import connect         # noqa: E402

from rc_dashboard import app as appmod             # noqa: E402
from rc_dashboard import config as configmod       # noqa: E402
from test_ingest import DAY, exit_rec, line        # noqa: E402


class Socket:
    """A websocket as `Reader` sees it: `send_text`, which takes every frame,
    never returns (a reader that stopped reading), or raises (one gone)."""

    def __init__(self, mode="reads"):
        self.mode = mode
        self.sent = []

    async def send_text(self, text):
        if self.mode == "stuck":
            await asyncio.Event().wait()
        if self.mode == "gone":
            raise RuntimeError("the reader went away")
        self.sent.append(text)


class ReaderTest(unittest.TestCase):

    def run_readers(self, socks, post, settle=0.5, timeout=0.2):
        """`post(readers)` with each reader's sender running; what each sent and
        who was dropped, and how long the posting took."""
        gone = []

        async def go():
            readers = [appmod.Reader(s, 1440) for s in socks]
            for r in readers:
                r.task = asyncio.ensure_future(r.run(timeout, lambda r, why: gone.append(
                    (r.ws, why))))
            t0 = time.monotonic()
            await post(readers)
            took = time.monotonic() - t0
            await asyncio.sleep(settle)
            for r in readers:
                r.task.cancel()
            return took
        took = asyncio.run(go())
        return took, gone

    def test_a_reader_that_takes_no_frame_is_dropped_and_holds_up_no_one(self):
        stuck, good = Socket("stuck"), Socket()

        async def post(readers):
            for i in range(200):
                for r in readers:
                    r.post("live", "frame %d" % i)          # never waits on a socket
                await asyncio.sleep(0.001)
        took, gone = self.run_readers([stuck, good], post)
        self.assertLess(took, 2.0)
        self.assertEqual(gone, [(stuck, "took over 0.2 s to take a frame")])
        self.assertEqual(good.sent[-1], "frame 199")
        self.assertEqual(stuck.sent, [])

    def test_a_kind_waiting_is_replaced_by_its_newer_frame(self):
        sock = Socket()

        async def post(readers):
            r = readers[0]
            r.post("live", "live 1")
            r.post("windows", "windows 1")
            r.post("live", "live 2")             # the newest of each, in the order last posted
        self.run_readers([sock], post)
        self.assertEqual(sock.sent, ["windows 1", "live 2"])

    def test_a_reader_gone_is_dropped(self):
        sock = Socket("gone")

        async def post(readers):
            readers[0].post("live", "x")
        _took, gone = self.run_readers([sock], post)
        self.assertEqual(gone, [(sock, "RuntimeError: the reader went away")])


def _health(port):
    with urllib.request.urlopen("http://127.0.0.1:%d/api/health" % port, timeout=30) as r:
        return json.loads(r.read())


def _until(what, cond, timeout=20.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        got = cond()
        if got:
            return got
        time.sleep(0.05)
    raise AssertionError("timed out waiting for %s" % what)


class ServiceTest(unittest.TestCase):
    """The real app over a scratch feed, on a free local port."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="rc_push_")
        root = os.path.join(cls.tmp, "ebpf")
        os.makedirs(os.path.join(root, DAY))
        cls.feed = os.path.join(root, DAY, "h1.exits.jsonl")
        now = time.time()
        with open(cls.feed, "wb") as fh:
            for i in range(40):
                fh.write(line(exit_rec(now - 60 * (40 - i), 5000 + i)))
        tree = configmod.defaults()
        tree["feeds"]["ebpf"]["roots"] = [root]
        tree["feeds"]["ebpf_node"]["roots"] = [os.path.join(cls.tmp, "node")]
        tree["cache"]["dir"] = os.path.join(cls.tmp, "cache")
        tree["cache"]["state_dir"] = os.path.join(cls.tmp, "state")
        tree["server"]["static_dir"] = os.path.join(cls.tmp, "no-ui")
        tree["server"]["rdns"]["enabled"] = False
        tree["history"]["enabled"] = False
        tree["panels"]["prewarm_min"] = []
        tree["ingest"]["poll_ms"] = 200
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)    # on_event
            cls.app = appmod.create_app(configmod.Config(tree, {}, None))
        cls.agg = cls.app.state.agg
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            cls.port = s.getsockname()[1]
        cls.server = uvicorn.Server(uvicorn.Config(cls.app, host="127.0.0.1", port=cls.port,
                                                   log_level="error"))
        cls.thread = threading.Thread(target=cls.server.run, daemon=True)
        cls.thread.start()
        _until("the service", lambda: cls.server.started)
        _until("the backfill", lambda: cls.agg.backfill.get("state") == "done")

    @classmethod
    def tearDownClass(cls):
        cls.server.should_exit = True
        cls.thread.join(timeout=20)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def url(self):
        return "ws://127.0.0.1:%d/ws" % self.port

    def feeding(self):
        """Append an exit every 0.1 s until the test ends."""
        stop = threading.Event()

        def run():
            pid = 20000
            with open(self.feed, "ab") as fh:
                while not stop.is_set():
                    pid += 1
                    fh.write(line(exit_rec(time.time(), pid, actor3="human")))
                    fh.flush()
                    time.sleep(0.1)
        t = threading.Thread(target=run, daemon=True)
        t.start()
        self.addCleanup(t.join, 5)
        self.addCleanup(stop.set)

    def stuck_reader(self):
        """A socket that completes the handshake, asks for a window again and
        again, and never reads a byte (the review's r9)."""
        s = socket.socket()
        s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 2048)
        s.connect(("127.0.0.1", self.port))
        s.sendall(("GET /ws HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nUpgrade: websocket\r\n"
                   "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n"
                   "Sec-WebSocket-Version: 13\r\n\r\n" % (
                       self.port, base64.b64encode(os.urandom(16)).decode())).encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            buf += s.recv(1)

        def frame(payload):
            mask = os.urandom(4)
            return bytes([0x81, 0x80 | len(payload)]) + mask + \
                bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        s.sendall(b"".join(frame(json.dumps({"type": "window",
                                             "minutes": 10080 - (i % 2)}).encode())
                           for i in range(4000)))
        self.addCleanup(s.close)
        return s

    def test_a_reader_that_stops_reading_holds_up_neither_the_tail_nor_the_others(self):
        self.feeding()
        got, stop = [], threading.Event()

        def good():
            with connect(self.url(), open_timeout=20, max_size=None) as ws:
                while not stop.is_set():
                    try:
                        ws.recv(timeout=1)
                    except TimeoutError:
                        continue
                    except Exception:
                        break
                    got.append(time.time())
        reader = threading.Thread(target=good, daemon=True)
        reader.start()
        self.addCleanup(reader.join, 5)
        self.addCleanup(stop.set)
        _until("the reader", lambda: len(self.app.state.clients) == 1)
        stats = self.app.state.ws_stats
        with mock.patch.object(appmod, "WS_SEND_TIMEOUT_S", 1.0):
            dropped = stats["dropped"]
            self.stuck_reader()
            t0, r0 = time.time(), self.agg.stats["records"]
            # the tail goes on, and so do the others' frames, while the stuck
            # one is dropped once a frame has waited WS_SEND_TIMEOUT_S on it
            _until("records", lambda: self.agg.stats["records"] >= r0 + 20)
            _until("frames", lambda: sum(1 for t in got if t > t0) >= 5)
            _until("the stuck reader dropped", lambda: stats["dropped"] > dropped)
        self.assertEqual(len(self.app.state.clients), 1)
        health = _health(self.port)
        self.assertTrue(health["ok"], health["ingest"])
        self.assertGreaterEqual(health["websocket"]["dropped"], 1)

    def test_health_says_so_when_the_tail_stops(self):
        release = threading.Event()
        real = self.agg.poll_once

        def stuck():
            release.wait(30)
            return real()
        self.addCleanup(release.set)
        with mock.patch.object(appmod, "STALE_MIN_S", 0.5), \
                mock.patch.object(self.agg, "poll_once", stuck):
            body = _until("ok false", lambda: (lambda b: None if b["ok"] else b)(
                _health(self.port)))
            self.assertGreaterEqual(body["ingest"]["age_s"], body["ingest"]["stale_after_s"])
            release.set()
            _until("ok again", lambda: _health(self.port)["ok"])

    def test_a_loop_that_fails_says_so_and_logs_it(self):
        def failing():
            raise RuntimeError("the day-files could not be read")
        log = io.StringIO()
        with mock.patch.object(appmod, "STALE_MIN_S", 0.5), \
                mock.patch.object(self.agg, "poll_once", failing), \
                mock.patch("sys.stderr", log):
            body = _until("the error", lambda: (lambda b: b if not b["ok"] and (
                b["loops"].get("ingest") or {}).get("error") else None)(_health(self.port)))
        self.assertEqual(body["loops"]["ingest"]["error"],
                         "RuntimeError: the day-files could not be read")
        self.assertGreaterEqual(body["loops"]["ingest"]["errors"], 1)
        self.assertIn("ingest loop: RuntimeError: the day-files could not be read",
                      log.getvalue())
        _until("ok again", lambda: _health(self.port)["ok"])

    def windows_loop_fails(self, rounds):
        """The windows loop's read of the builds' states raises, `rounds` times
        (None: every time). The loops read in the event loop's executor; the
        health route's own read, on one of AnyIO's workers, goes through."""
        real = self.agg.panels_by_window.status
        left = {"n": rounds}

        def status(*a, **k):
            if not threading.current_thread().name.startswith("AnyIO") \
                    and (left["n"] is None or left["n"] > 0):
                if left["n"] is not None:
                    left["n"] -= 1
                raise RuntimeError("the builds' states could not be read")
            return real(*a, **k)
        return mock.patch.object(self.agg.panels_by_window, "status", status)

    def test_one_failed_round_of_a_loop_leaves_ok(self):
        loops = self.app.state.loops
        errors = loops["windows"]["errors"]
        with self.windows_loop_fails(1), mock.patch("sys.stderr", io.StringIO()):
            _until("the round that failed", lambda: loops["windows"]["errors"] > errors)
            body = _health(self.port)
            self.assertTrue(body["ok"], body["why"])
            _until("a round through", lambda: loops["windows"]["streak"] == 0)
        self.assertTrue(_health(self.port)["ok"])

    def test_a_loop_that_fails_round_after_round_turns_ok_false_and_says_why(self):
        with self.windows_loop_fails(None), mock.patch("sys.stderr", io.StringIO()):
            body = _until("ok false", lambda: (lambda b: None if b["ok"] else b)(
                _health(self.port)))
        self.assertEqual(body["why"], ["the windows loop failed its last 3 rounds: "
                                       "RuntimeError: the builds' states could not be read"])
        _until("ok again", lambda: _health(self.port)["ok"])

    def default_view(self):
        return _until("the default window", lambda: (lambda v: v if v is not None and
                                                      v.state == "ready" else None)(
            self.agg.panels_by_window.peek(1440)))

    def test_one_record_a_view_could_not_take_leaves_ok(self):
        self.feeding()
        view = self.default_view()
        real, left = view.feed, {"n": 1}

        def feed(rec, at=None):
            if left["n"]:
                left["n"] -= 1
                raise RuntimeError("the view could not take this one")
            return real(rec, at)
        errors = self.agg.stats["feed_errors"]
        with mock.patch.object(view, "feed", feed), mock.patch("sys.stderr", io.StringIO()):
            _until("the record", lambda: self.agg.stats["feed_errors"] > errors)
            body = _health(self.port)
            self.assertTrue(body["ok"], body["why"])
            _until("a batch through", lambda: self.agg.stats["feed_error_streak"] == 0)
        self.assertTrue(_health(self.port)["ok"])

    def test_a_view_that_takes_no_record_turns_ok_false_and_is_logged_once(self):
        self.feeding()
        view = self.default_view()
        log = io.StringIO()
        with mock.patch.object(view, "feed", side_effect=RuntimeError("the view takes none")), \
                mock.patch("sys.stderr", log):
            body = _until("ok false", lambda: (lambda b: None if b["ok"] else b)(
                _health(self.port)))
            errors = self.agg.stats["feed_errors"]
            _until("more records it could not take",
                   lambda: self.agg.stats["feed_errors"] >= errors + 5)
        self.assertEqual(len(body["why"]), 1)
        self.assertTrue(body["why"][0].startswith("a view could not take a record in "),
                        body["why"])
        self.assertTrue(body["why"][0].endswith(": RuntimeError: the view takes none"))
        self.assertEqual(log.getvalue().count("view feed: RuntimeError: the view takes none"), 1)
        _until("ok again", lambda: _health(self.port)["ok"])

    def test_a_reader_past_the_cap_is_turned_away(self):
        _until("no readers", lambda: not self.app.state.clients)
        with mock.patch.object(appmod, "WS_MAX_CLIENTS", 1):
            with connect(self.url(), open_timeout=20, max_size=None) as first:
                self.assertEqual(json.loads(first.recv(timeout=20))["type"], "live")
                with connect(self.url(), open_timeout=20, max_size=None) as second:
                    with self.assertRaises(ConnectionClosed) as cm:
                        second.recv(timeout=20)
                    self.assertEqual(cm.exception.rcvd.code, 1013)
            self.assertGreaterEqual(_health(self.port)["websocket"]["refused"], 1)


if __name__ == "__main__":
    unittest.main()
