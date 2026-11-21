"""Demo mode: a synthetic collector feed, and the real service over it.

    uv run python -m rc_dashboard demo            # http://127.0.0.1:8080
    ./run-demo.sh                                 # the same, with hot reload, on :8087

The dashboard has one source of truth, the collector's day-files, and the
collector needs root, eBPF and a login node. So a demo writes those files itself
-- a few fictional login nodes, their users, and the Claude Code, Codex, IDE and
shell sessions they run -- and then starts the service, unchanged, over them.
Every reducer, the tail, the backfill, the saved builds and the websocket run
exactly as they do on a cluster; nothing on the page is drawn from a fixture.
The feed keeps growing in real time, so the Live tab moves.

The files are JSONL, as a collector writes them.

Nothing in it was measured, and the page cannot be mistaken for a measurement:

* every record carries `"synthetic": true`, so the label travels with the data:
  an export of the demo says it on every row, and a service pointed at a demo
  feed by hand still finds it;
* the header says "synthetic data" whenever a host's heartbeat does
  (`health.py`), and the Feeds tab marks each such host;
* the site is named "demo · synthetic data" (the rail and the browser tab), and
  its description, which the chat agent is given, says nothing was measured.

What a demo overrides, whatever the shell or the config file says (run.sh
exports the cluster's feed roots, and a demo must never read them):

    feeds.ebpf.roots, feeds.ebpf_node.roots   the demo's own feed
    feeds.sacct/sacctmgr/domains/user_actor   a path in the demo dir that does not exist
    cache.dir                                 the demo's own, so no real saved build is evicted
    rollup.dir                                the demo's own, so no made-up day lands in a
                                              real roll-up table (rollup.py)
    clock.as_of                               "" -- the wall clock: the feed is live
    live.retention_min, backfill_hours        the history written, so every window is filled
    panels.preprocess, preprocess_wait_s      every window built before the port opens
    site.name, site.description               as above
    server.rdns.enabled                       off: the Network bytes panel names its
                                              endpoints by the records' own host names,
                                              and a demo asks no DNS server about them
    server.auth.open                          on: a demo serves with no password unless
                                              server.auth.password_hash is set

The feed is a pure function of the seed, the host count and the clock
(`world.World`): the same window over the same seed is the same bytes, and a
restart (run.sh restarts on every save) resumes it where it stopped rather than
writing anything twice (`feed.Feed`). `--demo-fresh` starts it again, as does a
feed an older demo wrote in another format.
"""
import fcntl
import os
import sys
import threading
import time

from .feed import Feed, day_of
from .world import GENERATOR, World, midnight

HERE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_DIR = os.path.join(HERE, ".cache", "demo")
SITE_NAME = "demo · fake data"
SITE_DESC = ("a demo: fake records for fictional login nodes, users and sessions, "
             "written by rc_dashboard/demo; nothing here was measured")
DEFAULTS = {"dir": DEFAULT_DIR, "days": 3, "hosts": 2, "seed": "demo"}
ENV = {"dir": "RC_DASH_DEMO_DIR", "days": "RC_DASH_DEMO_DAYS",
       "hosts": "RC_DASH_DEMO_HOSTS", "seed": "RC_DASH_DEMO_SEED"}
# What the stamp says the feed is in: a feed an older demo wrote as EBR is
# started afresh rather than resumed into files the service would not read.
FORMAT = "jsonl"
TICK_S = 2.0           # how often the live feed is appended to
CHUNK_S = 3600         # the history is written an hour at a time


def options(args):
    """The demo's settings: --demo-* flag > RC_DASH_DEMO_* > default."""
    out = {}
    for k, dflt in DEFAULTS.items():
        v = getattr(args, "demo_" + k, None)
        if v is None:
            v = os.environ.get(ENV[k]) or dflt
        out[k] = v
    out["days"] = max(1, min(14, int(out["days"])))
    out["hosts"] = max(1, min(6, int(out["hosts"])))
    out["seed"] = str(out["seed"])
    out["dir"] = os.path.abspath(os.path.expanduser(str(out["dir"])))
    out["fresh"] = bool(getattr(args, "demo_fresh", False))
    return out


def configure(cfg, opts):
    """Point the service at the demo's feed and nothing else (see the module doc)."""
    feed = Feed(opts["dir"])
    missing = os.path.join(feed.base, "not-in-a-demo")
    days, mins = opts["days"], opts["days"] * 1440
    for key, val in (
            ("feeds.ebpf.roots", [feed.ebpf]),
            ("feeds.ebpf.days", days + 1),
            ("feeds.ebpf_node.roots", [feed.node]),
            ("feeds.sacct.dir", missing),
            ("feeds.sacct.jobs_csv", None),
            ("feeds.sacctmgr.dir", missing),
            ("feeds.domains.path", os.path.join(missing, "map_user.csv")),
            ("feeds.user_actor.path", os.path.join(missing, "user.csv")),
            ("cache.dir", feed.cache),
            ("rollup.dir", os.path.join(feed.cache, "rollup")),
            ("clock.as_of", ""),
            ("live.retention_min", mins),
            ("live.backfill_hours", days * 24),
            ("live.window_min", min(1440, mins)),
            # Every window built before the port opens: a visitor arriving
            # after a cold start waits on the "starting" page, never on a
            # "merging" bar (deploy/cloudflare).
            ("panels.preprocess", True),
            ("panels.preprocess_wait_s", 300),
            ("site.name", SITE_NAME),
            ("site.description", SITE_DESC),
            ("server.rdns.enabled", False),
            # Made-up data: served with no password unless one is set. `serve`
            # refuses to start open unless told to.
            ("server.auth.open", True)):
        cfg.set(key, val, "demo")
    return cfg


def _log(msg):
    sys.stderr.write("demo: %s\n" % msg)
    sys.stderr.flush()


def _another(key, had, want):
    """The log's words for what the stamp on disk says otherwise."""
    if key == "format":                 # a stamp without one is a JSONL-only demo's
        return "format (%s, not %s)" % (had or "jsonl", want)
    return {"hosts": "host count"}.get(key, key)


class Demo:
    """The feed, its generator, and the thread that keeps it current."""

    def __init__(self, opts):
        self.opts = opts
        self.feed = Feed(opts["dir"])
        self.state = None
        self.world = None
        self._stop = threading.Event()
        self._thread = None
        self._mu = threading.Lock()

    # ------------------------------------------------------------ history
    def claim(self):
        """One writer per feed, as one collector per host: a second demo on the same
        directory would write every record twice."""
        os.makedirs(self.feed.base, exist_ok=True)
        self._lockfh = open(os.path.join(self.feed.base, ".demo.lock"), "a+")
        try:
            fcntl.flock(self._lockfh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._lockfh.seek(0)
            other = self._lockfh.read().strip() or "?"
            raise SystemExit("demo: another demo (pid %s) is writing %s; stop it, or give this "
                             "one its own --demo-dir" % (other, self.feed.base))
        self._lockfh.seek(0)
        self._lockfh.truncate()
        self._lockfh.write(str(os.getpid()))
        self._lockfh.flush()

    def prepare(self, now=None):
        """Write the history up to `now`, resuming a feed this seed already wrote."""
        now = time.time() if now is None else now
        o = self.opts
        self.claim()
        span = o["days"] * 86400
        st = self.feed.load_state()
        want = {"generator": GENERATOR, "seed": o["seed"], "hosts": o["hosts"],
                "format": FORMAT}
        # a stamp without a format is a JSONL-only demo's, from before there was another
        had = None if st is None else dict({"format": "jsonl"}, **st)
        why = None
        if o["fresh"]:
            why = "--demo-fresh"
        elif st is None:
            why = "no demo feed in %s yet" % self.feed.base
        elif any(had.get(k) != v for k, v in want.items()):
            why = "the feed there has another %s" % " and ".join(
                _another(k, had.get(k), v) for k, v in want.items() if had.get(k) != v)
        elif not isinstance(st.get("written_to"), (int, float)) or st["written_to"] < now - span:
            why = "the feed there ends more than %d days ago" % o["days"]
        elif self.covers_from(st) > now - span + 3600:
            # a longer history asked of a shorter feed: resuming would leave the
            # wider windows mostly empty, and records cannot be put before a file's
            why = "the feed there covers less than the %d days asked for" % o["days"]
        if why:
            _log("starting a new feed (%s)" % why)
            self.feed.wipe()
            # whole minutes, so the heartbeats fall where a collector's would
            start = float(int(now - span) // 60 * 60)
            st = dict(want, start=start, written_to=start)
        else:
            cut = self.feed.cut_after(st["written_to"])
            _log("resuming the %s feed in %s from %s%s" % (
                FORMAT, self.feed.base,
                time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st["written_to"])),
                " (cut %d file(s) back to it)" % cut if cut else ""))
        self.state = st
        self.world = World(o["seed"], o["hosts"], st["start"])
        self.feed.prune(day_of(now - span - 86400))
        t, total = st["written_to"], max(1.0, now - st["written_to"])
        last_pct = -1
        while t < now:
            t1 = min(now, t + CHUNK_S)
            self._write(t, t1)
            t = t1
            pct = int(100 * (1 - (now - t) / total))
            if pct // 10 != last_pct // 10:
                _log("writing synthetic history ... %d%%" % pct)
                last_pct = pct
        return self

    def covers_from(self, st):
        """Where the feed on disk begins: its start, or later once old days are pruned."""
        day = self.feed.oldest_day()
        return max(st.get("start") or 0.0, midnight(day) if day else 0.0)

    def _write(self, t0, t1):
        with self._mu:
            items = self.world.records(t0, t1)
            self.feed.append(items)
            self.state["written_to"] = t1
            self.feed.save_state(self.state)
            return len(items)

    # --------------------------------------------------------------- live
    def start_live(self):
        """Append each round's records from now on, as the collector would."""
        def run():
            last_prune = time.time()
            while not self._stop.wait(TICK_S):
                now = time.time()
                try:
                    if now > self.state["written_to"]:
                        self._write(self.state["written_to"], now)
                    if now - last_prune > 3600:
                        self.feed.prune(day_of(now - self.opts["days"] * 86400 - 86400))
                        last_prune = now
                except Exception as e:          # a demo must not take the service down
                    _log("live writer: %s" % e)
        self._thread = threading.Thread(target=run, name="demo-feed", daemon=True)
        self._thread.start()
        return self

    def stop(self):
        """Stop the live writer, close the day-files, and let another demo have the feed."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=10)
        self.feed.close()
        fh, self._lockfh = getattr(self, "_lockfh", None), None
        if fh is not None:
            fh.close()
