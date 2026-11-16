"""The bench: how long the service takes to start, build, open a session, and answer.

    .venv/bin/python bench/run.py FEED [--out results.json] [--only coldstart,builds,inspector,page]

FEED is a directory written by `amplify.py`. Each scenario runs the real service
(`svc.Service`) and times it from outside, with `time.monotonic` -- a pinned clock
reads every server-side duration as 0 s:

* **coldstart** -- from an empty cache, then again from the cache that run left:
  when the page first answers, when the tail is positioned, when the default
  window is ready, when the history backfill ends; and how long a static file
  takes to come back meanwhile (the event loop's latency).
* **builds** -- with the service up: a 48 h and a 12 h window, and the default
  window filtered to one user and to one agent type, each from request to ready.
* **inspector** -- `/api/trajectory` for the busiest session of each kind, read
  cold, again (cached), and newest-first.
* **page** -- on the wall clock, with records appended live (`--rate-x` times
  the feed's own pace, re-stamped to now): latency of `/api/live`,
  `/api/panels`, `/api/trajectories` and of a static file while a filtered view
  and a second window build, and the payload sizes.
"""
import argparse
import json
import os
import shutil
import sys
import threading
import time
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import amplify      # noqa: E402
from svc import Probe, Service, feed_meta, stats      # noqa: E402


def q(params):
    return urllib.parse.urlencode(params, doseq=True)


def top_user(s, minutes=1440):
    panels = s.json("/api/panels?window_min=%d" % minutes)
    users = sorted((panels.get("facets") or {}).get("users") or [],
                   key=lambda u: (-(u.get("n") or 0), str(u.get("user") or u.get("value"))))
    return (users[0].get("user") or users[0].get("value")) if users else None


def startup(s, timeline):
    """Fill `timeline` with when each startup milestone was first seen."""
    s.wait(lambda: s.get("/index.html", timeout=5)[0] == 200, every=0.05, what="listen")
    timeline["listen_s"] = round(s.since(), 2)
    s.wait(lambda: s.get("/api/health", timeout=30)[0] == 200, every=0.05, what="health")
    timeline["health_s"] = round(s.since(), 2)
    timeline["positioned_s"] = round(s.wait(
        lambda: s.health()["backfill"].get("state") not in (None, "idle", "positioning"),
        what="positioned"), 2)
    seen = {}

    def both():
        h = s.health()
        for w in h["windows"]["held"]:
            if w.get("minutes") == 1440 and not w.get("filter") and w.get("state") == "ready":
                seen.setdefault("default_ready_s", round(s.since(), 2))
        if h["backfill"].get("state") == "done":
            seen.setdefault("backfill_done_s", round(s.since(), 2))
        return len(seen) == 2
    s.wait(both, what="default window and backfill")
    timeline.update(seen)


def coldstart(feed, work):
    out = {}
    cache = os.path.join(work, "cache-cold")
    if os.path.exists(cache):
        shutil.rmtree(cache)
    for label in ("fresh", "warm"):
        tl = {}
        s = Service(feed, cache, as_of="auto")
        s.start()
        probe = Probe(s)
        probe.start()
        try:
            startup(s, tl)
        finally:
            tl["static_latency"] = probe.stop()
            s.stop()
        out[label] = tl
        print("coldstart %-5s %s" % (label, json.dumps(tl)))
    return out


def builds(feed, work):
    out = {}
    cache = os.path.join(work, "cache-cold")            # the warm cache coldstart left
    with Service(feed, cache, as_of="auto") as s:
        startup(s, {})
        user = top_user(s)
        cases = [("48h", {"window_min": 2880}, 2880, None),
                 ("12h", {"window_min": 720}, 720, None),
                 ("24h user", {"window_min": 1440, "user": user}, 1440,
                  {"users": [user], "agent_types": []}),
                 ("24h codex", {"window_min": 1440, "agent_type": "codex"}, 1440,
                  {"users": [], "agent_types": ["codex"]})]
        for label, params, minutes, flt in cases:
            t = time.monotonic()
            s.json("/api/panels?" + q(params))
            s.wait_ready(minutes, flt)
            out[label] = round(time.monotonic() - t, 2)
            print("build %-10s %6.2f s" % (label, out[label]))
    return out


def inspector(feed, work):
    out = {}
    cache = os.path.join(work, "cache-cold")
    with Service(feed, cache, as_of="auto") as s:
        startup(s, {})
        tr = s.json("/api/trajectories?rank=events&window_min=1440")
        rows = tr.get("rows") or {}
        picks, kinds = [], set()
        for sid in sorted(rows, key=lambda k: (-((rows[k] or {}).get("n_events") or 0), k)):
            r = rows.get(sid) or {}
            kind = (r.get("actor3"), r.get("agent_type"))
            if kind not in kinds:
                kinds.add(kind)
                picks.append((sid, "%s/%s" % kind))
        for sid, kind in picks[:4]:
            row = {}
            for label, order in (("cold", "oldest"), ("cached", "oldest"), ("newest", "newest")):
                st, body, dt = s.get("/api/trajectory?" + q({"sid": sid, "window_min": 1440,
                                                              "order": order}), gzip_ok=True)
                row[label] = round(dt, 2)
                row["bytes"] = len(body)
            out[kind] = row
            print("inspector %-22s %s" % (kind, json.dumps(row)))
    return out


class Appender(threading.Thread):
    """Append the feed's next records, re-stamped to now, `rate_x` feed-seconds per second."""

    def __init__(self, feed, rate_x=10.0):
        super().__init__(daemon=True)
        m = feed_meta(feed)
        self.world = amplify.World(m["seed"], m["demo_hosts"], m["start"])
        self.hosts = {h: [h] + ["%s-c%d" % (h, j) for j in range(1, m["host_clones"])]
                      for h in self.world.hosts}
        self.density = m["density"]
        self.t = m["end"]
        self.rate_x = rate_x
        self.w = amplify.Writer(feed)
        self.lines = 0
        self._halt = threading.Event()      # not `_stop`: Thread.join() calls _stop() (3.11, 3.12)

    def run(self):
        while not self._halt.wait(1.0):
            t1 = self.t + self.rate_x
            items = []
            for epoch, tier, host, stream, line in self.world.records(self.t, t1):
                rec = json.loads(line)
                # re-stamped into the present, spread over the second it is written in
                e = time.time() - (t1 - epoch) / self.rate_x
                if "ts_epoch" in rec:
                    rec["ts_epoch"] = round(e, 6)
                for key in ("ts", "timestamp"):
                    if key in rec:
                        rec[key] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(e))
                items.append((e, tier, host, stream, json.dumps(rec).encode()))
            amplify.emit(self.w, items, self.hosts, self.density)
            for fh in self.w.fh.values():
                fh.flush()
            self.lines = self.w.lines
            self.t = t1

    def stop(self):
        self._halt.set()
        self.join(10)
        self.w.close()


class Poller(threading.Thread):
    def __init__(self, s, path, every):
        super().__init__(daemon=True)
        self.s, self.path, self.every = s, path, every
        self.lat, self.size, self.fail = [], 0, 0
        self._halt = threading.Event()      # not `_stop`: Thread.join() calls _stop() (3.11, 3.12)

    def run(self):
        while not self._halt.is_set():
            st, body, dt = self.s.get(self.path, timeout=300, gzip_ok=True)
            if st == 200:
                self.lat.append(dt)
                self.size = len(body)
            else:
                self.fail += 1
            self._halt.wait(self.every)

    def stop(self):
        self._halt.set()
        self.join(320)
        return dict(stats(self.lat), bytes=self.size, fail=self.fail)


def page(feed, work, seconds=75, rate_x=10.0):
    """On the wall clock, so the poll, the live frames and the re-rank all run."""
    live = os.path.join(work, "feed-live")
    if os.path.exists(live):
        shutil.rmtree(live)
    shutil.copytree(feed, live)
    cache = os.path.join(work, "cache-live")
    if os.path.exists(cache):
        shutil.rmtree(cache)
    # the frozen run's saved hours are this run's too (the clock is not in their key)
    if os.path.isdir(os.path.join(work, "cache-cold", "hours")):
        shutil.copytree(os.path.join(work, "cache-cold", "hours"), os.path.join(cache, "hours"))
    out = {}
    with Service(live, cache, as_of="") as s:
        startup(s, out.setdefault("startup", {}))
        user = top_user(s)
        app = Appender(live, rate_x)
        app.start()
        probe = Probe(s)
        polls = {"live": Poller(s, "/api/live", 1.0),
                 "trajectories": Poller(s, "/api/trajectories?rank=recent", 1.0),
                 "panels": Poller(s, "/api/panels", 5.0)}
        probe.start()
        for p in polls.values():
            p.start()
        time.sleep(10)
        # two builds beside the readers: a filtered view (always a scan) and a window
        t_b = time.monotonic()
        s.json("/api/panels?" + q({"window_min": 1440, "user": user}))
        s.json("/api/panels?window_min=720")
        time.sleep(max(0, seconds - 10))
        out["static"] = probe.stop()
        for k, p in polls.items():
            out[k] = p.stop()
        app.stop()
        out["appended_lines"] = app.lines
        out["builds_started_s"] = round(t_b - s.t0, 1)
    for k in ("static", "live", "trajectories", "panels"):
        print("page %-13s %s" % (k, json.dumps(out[k])))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("feed")
    ap.add_argument("--out", default=None)
    ap.add_argument("--work", default=None, help="caches and the live copy (default FEED/bench)")
    ap.add_argument("--only", default="coldstart,builds,inspector,page")
    ap.add_argument("--seconds", type=int, default=75)
    ap.add_argument("--rate-x", type=float, default=10.0)
    a = ap.parse_args(argv)
    work = a.work or os.path.join(a.feed, "bench")
    os.makedirs(work, exist_ok=True)
    res = {"feed": feed_meta(a.feed), "at": time.strftime("%Y-%m-%d %H:%M:%S")}
    for name in a.only.split(","):
        t = time.monotonic()
        if name == "coldstart":
            res[name] = coldstart(a.feed, work)
        elif name == "builds":
            res[name] = builds(a.feed, work)
        elif name == "inspector":
            res[name] = inspector(a.feed, work)
        elif name == "page":
            res[name] = page(a.feed, work, a.seconds, a.rate_x)
        print("== %s took %.0f s" % (name, time.monotonic() - t))
    if a.out:
        with open(a.out, "w") as fh:
            json.dump(res, fh, indent=1)
        print("wrote", a.out)


if __name__ == "__main__":
    main()
