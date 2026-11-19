"""Run the real service over a bench feed, and read it the way the page does.

Shared by `run.py` (the timers) and `golden.py` (the output guard). The service is
a subprocess (`python -m rc_dashboard serve`), configured only through `RC_DASH_*`
variables, so what is measured is exactly what a deployment runs.
"""
import gzip
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
DASH = os.path.dirname(HERE)
PY = os.path.join(DASH, ".venv", "bin", "python")


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Service:
    def __init__(self, feed, cache, as_of="auto", env=None, log=None, port=None):
        self.feed = os.path.abspath(feed)
        self.cache = os.path.abspath(cache)
        self.port = port or free_port()
        self.as_of = as_of
        self.extra = dict(env or {})
        self.log_path = log or os.path.join(self.cache, "service.log")
        self.proc = None
        self.t0 = None

    def env(self):
        e = {k: v for k, v in os.environ.items() if not k.startswith("RC_DASH_")}
        e.update({
            "RC_DASH_EBPF_ROOTS": os.path.join(self.feed, "feed", "ebpf"),
            "RC_DASH_NODE_ROOTS": os.path.join(self.feed, "feed", "node"),
            "RC_DASH_CACHE_DIR": self.cache,
            "RC_DASH_AS_OF": self.as_of,
            "RC_DASH_HOST": "127.0.0.1",
            "RC_DASH_PORT": str(self.port),
            "RC_DASH_STATIC_DIR": os.path.join(DASH, "web", "dist"),
            "RC_DASH_ASK": "0",
            "RC_DASH_RDNS": "0",
            # no password: a bench service on 127.0.0.1, over a feed of its own
            "RC_DASH_OPEN": "1",
            "RC_DASH_SITE_NAME": "bench",
            # set iteration order fixed, so two runs over the same bytes serve the same bytes
            "PYTHONHASHSEED": "0",
        })
        e.update(self.extra)
        return e

    def start(self):
        os.makedirs(self.cache, exist_ok=True)
        self._log = open(self.log_path, "ab")
        self.t0 = time.monotonic()
        self.proc = subprocess.Popen([PY, "-m", "rc_dashboard", "serve"], cwd=DASH,
                                     env=self.env(), stdout=self._log, stderr=subprocess.STDOUT)
        return self

    def stop(self):
        if self.proc is None:
            return
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGINT)
            try:
                self.proc.wait(30)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        self._log.close()
        self.proc = None

    def __enter__(self):
        return self.start()

    def __exit__(self, *a):
        self.stop()

    # ------------------------------------------------------------------ http
    def url(self, path):
        return "http://127.0.0.1:%d%s" % (self.port, path)

    def get(self, path, timeout=300, gzip_ok=False):
        """`(status, body bytes, seconds)`; status 0 when the server did not answer."""
        req = urllib.request.Request(self.url(path))
        if gzip_ok:
            req.add_header("Accept-Encoding", "gzip")
        t = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    body = gzip.decompress(body)
                return r.status, body, time.monotonic() - t
        except urllib.error.HTTPError as e:
            return e.code, e.read(), time.monotonic() - t
        except (urllib.error.URLError, ConnectionError, socket.timeout, OSError):
            return 0, b"", time.monotonic() - t

    def lines(self, path, n, timeout=300):
        """The first `n` lines of a streamed body, then hang up (the server stops)."""
        out = []
        with urllib.request.urlopen(self.url(path), timeout=timeout) as r:
            for line in r:
                out.append(line)
                if len(out) >= n:
                    break
        return out

    def json(self, path, timeout=300):
        st, body, _ = self.get(path, timeout)
        if st != 200:
            raise RuntimeError("%s -> %s %s" % (path, st, body[:200]))
        return json.loads(body)

    # --------------------------------------------------------------- waiting
    def since(self):
        return time.monotonic() - self.t0

    def wait(self, pred, timeout=1800, every=0.1, what="condition"):
        """Poll `pred()` (exceptions count as not yet) until true; seconds since start."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self.proc is not None and self.proc.poll() is not None:
                raise RuntimeError("service exited (%s) waiting for %s; see %s"
                                   % (self.proc.returncode, what, self.log_path))
            try:
                if pred():
                    return self.since()
            except Exception:
                pass
            time.sleep(every)
        raise TimeoutError("timed out waiting for %s" % what)

    def health(self):
        return self.json("/api/health", timeout=60)

    def view(self, minutes, flt=None):
        """The held view's status for `minutes` (and filter), or None."""
        for w in self.json("/api/windows", timeout=60).get("held", []):
            if w.get("minutes") == minutes and (w.get("filter") or None) == (flt or None):
                return w
        return None

    def wait_ready(self, minutes, flt=None, timeout=3600, installed_after=None):
        def ok():
            v = self.view(minutes, flt)
            if not v or v.get("state") != "ready":
                return False
            return installed_after is None or (v.get("installed_at") or 0) > installed_after
        return self.wait(ok, timeout, what="window %s %s ready" % (minutes, flt or ""))


class Probe(threading.Thread):
    """GET `path` every `every` s and keep the latencies: with a static file, how
    long the event loop takes to answer anything at all."""

    def __init__(self, svc, path="/index.html", every=0.05):
        super().__init__(daemon=True)
        self.svc, self.path, self.every = svc, path, every
        self.lat = []
        self.fail = 0
        # not `_stop`: on Python 3.11 and 3.12 Thread.join() calls its own _stop()
        self._halt = threading.Event()

    def run(self):
        while not self._halt.is_set():
            st, _b, dt = self.svc.get(self.path, timeout=120)
            if st == 200:
                self.lat.append(dt)
            else:
                self.fail += 1
            self._halt.wait(self.every)

    def stop(self):
        self._halt.set()
        self.join(150)
        return stats(self.lat)


def stats(xs):
    if not xs:
        return {"n": 0}
    s = sorted(xs)

    def q(p):
        return round(1000 * s[min(len(s) - 1, int(p * (len(s) - 1)))], 1)
    return {"n": len(s), "p50_ms": q(.5), "p90_ms": q(.9), "p99_ms": q(.99),
            "max_ms": round(1000 * s[-1], 1)}


def feed_meta(feed):
    with open(os.path.join(feed, "feed.json")) as fh:
        return json.load(fh)


if __name__ == "__main__":          # pragma: no cover - manual check
    sys.exit("import this module; see run.py and golden.py")
