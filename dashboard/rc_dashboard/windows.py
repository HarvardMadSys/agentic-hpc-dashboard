"""Selectable time windows: one clamp rule, and the historical rebuild.

The two aggregation tiers answer a window change differently, and the difference
is not cosmetic:

* The LIVE tier holds fixed-width per-minute bins over RETENTION, so any window
  up to retention is an exact slice of bins the process already has.  Free,
  instant, no re-read -- see `buckets.RollingBuckets._span`.

* The HISTORICAL tier -- quantiles, CCDFs, Gini, the shell-dedup index, the
  trajectory chains -- carries no time index and is not resumable from a byte
  offset.  That is already why it rebuilds on a schedule and publishes
  `full_built_at` rather than pretending to be live.  It cannot be sliced, so a
  window change REBUILDS it from the rows, over exactly that window, at a cost
  proportional to the window (`export.window_plan` seeks past everything older).

So a window is not a filter applied to one set of numbers; it is a second set of
reducers.  Each built window is a `PanelView`, and the registry keeps a small
LRU of them because reducer state -- a 200k-value quantile sample, a 200k-session
dedup index, 50k trajectories -- is the expensive part, not the payload.

Three things are reported rather than assumed, in keeping with the rest of the
service:

* **A window wider than retention is refused, not served.**  It is clamped and
  the response says `clamped`, because drawing bins the process never held is
  the same error as imputing a zero.
* **A built view keeps ingesting, so it drifts past its own window.**  The drift
  is published in seconds and the view re-scans once it reaches
  `panels.max_drift_pct` of the window.  An aging panel says its age.
* **The records a rebuild could not replay are counted.**  A build reads the
  day-files while the tailer is also advancing through them; the handful of rows
  landing in that seam are buffered and replayed, and if the buffer overflows
  the count is published as `replay_dropped` instead of quietly going missing.

And one thing is reused rather than recomputed: every build's reducer state is
saved with how far into each day-file it read (`snapshots`), so a restart or a
return to a window evicted from the LRU resumes it -- reading only what the
collector wrote since -- instead of reducing the whole window again. A resumed
view keeps its original `built_at`, and its status says it was restored.
"""
import collections
import threading
import time

from . import snapshots
from .export import resume_plan, window_plan, window_scan
from .normalize import Normalizer
from .reducers.io import IoReducer
from .reducers.resources import ResourceReducer
from .reducers.risk import RiskReducer
from .reducers.sandbox import SandboxReducer
from .reducers.tools import ToolMixReducer
from .reducers.trajectories import TrajectoryReducer

# Built with the config and class list alone. `ResourceReducer` and
# `TrajectoryReducer` also take the window length, so `fresh_reducers` makes them.
HIST_REDUCERS = (ToolMixReducer, IoReducer, SandboxReducer, RiskReducer)
# The only records a view reads. A build skips the rest before parsing them, and
# the live feed, the replay buffer and the drift all ignore them, so "the newest
# record the view holds" means the same thing whichever path it came in by.
EVENTS = frozenset(ev for r in HIST_REDUCERS + (ResourceReducer, TrajectoryReducer)
                   for ev in r.EVENTS)

# Below this a window is mostly bin-edge noise: one minute of a login fleet is a
# handful of exits, and every distribution panel would read `n=0`.
MIN_WINDOW_MIN = 5

# Live records buffered during a rebuild, for replay across the read seam. The
# seam is sub-second in practice (the scan and the tailer reach EOF together),
# so this is generous; what matters is that overflow is COUNTED, not silent.
REPLAY_CAP = 20000


def _newest(a, b):
    return b if a is None else a if b is None else max(a, b)


def label(minutes):
    """`90m`, `6h`, `24h`, `7d` -- the same string the picker shows, made once
    here so the page and the export header cannot disagree about what to call it.

    A day stays `24h` rather than becoming `1d`: this dashboard's whole prior
    vocabulary is "the 24-hour window", and renaming the default view while
    changing its behaviour would make the two changes indistinguishable.
    """
    minutes = int(minutes)
    if minutes % 1440 == 0 and minutes >= 2880:
        return "%dd" % (minutes // 1440)
    if minutes % 60 == 0:
        return "%dh" % (minutes // 60)
    if minutes > 60:
        return "%dh%02dm" % divmod(minutes, 60)
    return "%dm" % minutes


def retention_min(cfg):
    return max(MIN_WINDOW_MIN, int(cfg.get("live.retention_min", 10080)))


def resolve(cfg, raw):
    """Normalise a requested window to one the process can actually answer.

    Returns the minutes to use plus why, never a bare number: the caller has to
    be able to tell the reader that what they asked for is not what they got.
    """
    cap = retention_min(cfg)
    default = min(int(cfg.get("live.window_min", 1440) or 1440), cap)
    try:
        want = int(raw)
    except (TypeError, ValueError):
        return {"minutes": default, "requested": None, "clamped": False,
                "retention_minutes": cap, "label": label(default), "note": None}
    if want < MIN_WINDOW_MIN:
        return {"minutes": MIN_WINDOW_MIN, "requested": want, "clamped": True,
                "retention_minutes": cap, "label": label(MIN_WINDOW_MIN),
                "note": "below the %d-minute floor" % MIN_WINDOW_MIN}
    if want > cap:
        return {"minutes": cap, "requested": want, "clamped": True,
                "retention_minutes": cap, "label": label(cap),
                "note": "beyond retention (live.retention_min=%d); the process "
                        "never held those bins" % cap}
    return {"minutes": want, "requested": want, "clamped": False,
            "retention_minutes": cap, "label": label(want), "note": None}


def presets(cfg):
    """The offered windows, filtered to what retention can actually serve.

    Filtered rather than clamped: offering `7d` on a service retaining 24 h and
    then silently serving 24 h is how a reader ends up believing a number.
    """
    cap = retention_min(cfg)
    raw = cfg.get("live.window_presets_min") or [60, 360, 720, 1440, 4320, 10080]
    out, seen = [], set()
    for m in sorted(int(x) for x in raw):
        if m < MIN_WINDOW_MIN or m > cap or m in seen:
            continue
        seen.add(m)
        out.append({"minutes": m, "label": label(m)})
    if cap not in seen:
        out.append({"minutes": cap, "label": label(cap)})
    return out


class PanelView:
    """One window's historical reducers, their build state, and their drift."""

    def __init__(self, cfg, classes, minutes):
        self.cfg = cfg
        self.classes = list(classes)
        self.minutes = int(minutes)
        self.window_s = self.minutes * 60
        self.state = "queued"           # queued | building | ready | error
        self.error = None
        self.progress = {"pct": 0, "records": 0, "bytes": 0, "total_bytes": 0,
                         "files": 0, "elapsed_s": 0.0}
        self.built_at = None            # epoch the reduction was built (a resume keeps it)
        self.installed_at = None        # epoch these reducers went live: build OR resume
        self.covers_from = None
        self.covers_to = None
        self.consumed_through = None    # newest ts the build ingested
        self.reducers = {}              # key -> reducer, live-fed after the swap
        self.dispatch = {}              # event -> [reducer]
        self.payload = {}               # materialised result(), per reducer key
        self.touched = time.time()
        self.replay_dropped = 0
        self.live_records = 0
        self.live_through = None        # newest ts ingested LIVE since the build
        self._replay = None             # deque while a build is in flight
        # Where the reducers came from and what happened to the saved copy:
        # `source` is "scan" or "snapshot", `note` says why a snapshot was or
        # was not used, `saved_at` when this build was last written down.
        self.cache = {"source": None, "note": None, "saved_at": None}

    # ------------------------------------------------------------------ feed
    def feed(self, rec):
        """Called under the aggregator's lock, once per live record.

        During a build the record is buffered instead: the fresh reducers do not
        exist yet, and feeding the outgoing ones would only update state that is
        about to be discarded.
        """
        if rec.get("event") not in EVENTS:
            return
        if self._replay is not None:
            if len(self._replay) >= REPLAY_CAP:
                self.replay_dropped += 1
            else:
                self._replay.append(rec)
            return
        if not self.dispatch:
            return
        ts = rec.get("_ts_epoch")
        # Already counted by the scan: the build read the same rows from disk.
        if ts is not None and self.consumed_through is not None \
                and ts <= self.consumed_through:
            return
        self.live_records += 1
        if ts is not None and (self.live_through is None or ts > self.live_through):
            self.live_through = ts
        for r in self.dispatch.get(rec.get("event"), ()):
            try:
                r.feed(rec)
            except Exception:
                pass                    # one bad record must never stop the stream

    # ----------------------------------------------------------------- build
    def fresh_reducers(self):
        red = {r.KEY: r(self.cfg, self.classes) for r in HIST_REDUCERS}
        red["resources"] = ResourceReducer(self.cfg, self.classes,
                                           window_s=self.window_s)
        red["trajectories"] = TrajectoryReducer(self.cfg, self.classes,
                                                window_s=self.window_s)
        return red

    @staticmethod
    def build_dispatch(reducers):
        d = {}
        for r in reducers.values():
            for ev in getattr(r, "EVENTS", ()):
                d.setdefault(ev, []).append(r)
        return d

    def materialize(self):
        """Turn reducer state into the payload the API serves."""
        out = {}
        for key, r in self.reducers.items():
            try:
                out[key] = r.result()
            except Exception as e:
                out[key] = {"_error": str(e)}
        self.payload = out
        return out

    # ---------------------------------------------------------------- status
    def drift_s(self):
        """Seconds of records held BEYOND the window the view was built for.

        Measured from the newest record actually INGESTED SINCE the build, not
        from the wall clock. The two differ whenever the feed is quiet or
        lagging, and using the clock there is a rebuild loop rather than a
        refresh: `covers_to` is the newest row the SCAN found, so on any feed
        whose newest row is already older than the drift budget -- an idle
        cluster overnight, a collector behind by more than 25% of the window --
        every tick re-queues a full re-scan that recomputes the same
        `covers_to` and cures nothing. No new records means no drift.
        """
        if self.covers_to is None:
            return None
        if self.live_through is None:
            return 0.0
        return max(0.0, self.live_through - self.covers_to)

    def status(self, now=None):
        now = now or time.time()
        drift = self.drift_s()
        budget = self.window_s * max(1, int(
            self.cfg.get("panels.max_drift_pct", 25))) / 100.0
        st = {
            "minutes": self.minutes, "label": label(self.minutes),
            "state": self.state, "error": self.error,
            # Distinguishes a FIRST build (nothing to show; the page must say it
            # is reducing) from a drift RE-build (the previous reduction is still
            # valid and still on screen). Blanking good numbers for twenty
            # minutes because a 7-day view is refreshing would be a worse lie
            # than showing them with their age attached.
            "has_payload": bool(self.payload),
            # Moves whenever the reducers are swapped in, including on a resume,
            # where `built_at` deliberately does not: a page keyed on `built_at`
            # alone would never re-fetch a restored window.
            "installed_at": (round(self.installed_at, 3)
                             if self.installed_at else None),
            "progress": dict(self.progress),
            "built_at": (time.strftime("%Y-%m-%d %H:%M:%S",
                                       time.localtime(self.built_at))
                         if self.built_at else None),
            "built_age_s": round(now - self.built_at, 1) if self.built_at else None,
            "covers_from": (time.strftime("%Y-%m-%d %H:%M:%S",
                                          time.localtime(self.covers_from))
                            if self.covers_from else None),
            "covers_to": (time.strftime("%Y-%m-%d %H:%M:%S",
                                        time.localtime(self.covers_to))
                          if self.covers_to else None),
            # The view has ingested this much past its own window. Published
            # because the panels ARE that wide -- rounding it away would make
            # the label a claim the numbers do not support.
            "drift_s": round(drift, 1) if drift is not None else None,
            "drift_budget_s": round(budget, 1),
            "live_records": self.live_records,
            "replay_dropped": self.replay_dropped,
            "cache": dict(self.cache),
        }
        if drift is not None and drift > 0:
            st["covers_note"] = (
                "built over the last %s, plus %d s of records ingested since; "
                "re-scans at %d s of drift"
                % (label(self.minutes), int(drift), int(budget)))
        return st


class WindowRegistry:
    """A bounded LRU of built windows, with one serialised build worker.

    One worker, not a pool: two concurrent rebuilds would contend for the same
    day-file page cache and each make the other look slow, and the point of the
    seek-based plan is that a build is I/O-bound on exactly its window.
    """

    def __init__(self, cfg, classes, roots_fn, lock):
        self.cfg = cfg
        self.classes = list(classes)
        self.roots_fn = roots_fn
        self.lock = lock                # the aggregator's; guards reducer swaps
        self.views = collections.OrderedDict()   # minutes -> PanelView
        self.max_views = max(1, int(cfg.get("panels.max_windows", 2)))
        self.budget_pct = max(1, int(cfg.get("panels.max_drift_pct", 25))) / 100.0
        self.store = snapshots.SnapshotStore.from_config(cfg)
        self._queue = collections.deque()
        self._cv = threading.Condition()
        self._worker = None
        self._stop = False

    # ---------------------------------------------------------------- worker
    def start(self):
        if self._worker is None:
            self._worker = threading.Thread(target=self._run, name="panel-build",
                                            daemon=True)
            self._worker.start()

    def stop(self):
        self._stop = True
        with self._cv:
            self._cv.notify_all()

    def _run(self):
        while not self._stop:
            with self._cv:
                while not self._queue and not self._stop:
                    self._cv.wait(timeout=5)
                if self._stop:
                    return
                minutes = self._queue.popleft()
            with self.lock:
                view = self.views.get(minutes)   # may have been evicted meanwhile
            if view is not None:
                try:
                    self._build(view)
                except Exception as e:                 # never kill the worker
                    view.state, view.error = "error", str(e)
                    view._replay = None

    def _enqueue(self, minutes):
        with self._cv:
            if minutes not in self._queue:
                self._queue.append(minutes)
                self._cv.notify_all()

    # ----------------------------------------------------------------- build
    def _build(self, view):
        t0 = time.time()
        view.state = "building"
        view.error = None
        # Buffer live records from here, so the seam between "the scan reached
        # EOF" and "the fresh reducers are installed" replays rather than drops.
        with self.lock:
            view._replay = collections.deque()
        roots = self.roots_fn()
        view.cache["note"] = None
        # Only a view with nothing in memory resumes. A drift re-build exists to
        # move the window's start forward, which a saved copy -- older still --
        # cannot do.
        resumed = None if view.reducers else self._resume(view, roots, t0)
        if resumed:
            saved, reducers, plan, total = resumed
            since = saved["covers_from"]
        else:
            saved, reducers = None, view.fresh_reducers()
            since = t0 - view.window_s
            days = int(view.window_s // 86400) + 2
            plan, total = window_plan(roots, since, days)
        view.progress = {"pct": 0, "records": 0, "bytes": 0,
                         "total_bytes": total, "files": len(plan),
                         "elapsed_s": 0.0, "mode": "resume" if saved else "scan"}

        dispatch = PanelView.build_dispatch(reducers)
        norm = Normalizer(self.cfg)      # fresh: build stats must not pollute /api/feeds
        newest = None
        n = 0
        for rec, stats in window_scan(plan, norm, since, events=EVENTS):
            ts = rec.get("_ts_epoch")
            if ts is not None and (newest is None or ts > newest):
                newest = ts
            for r in dispatch.get(rec.get("event"), ()):
                try:
                    r.feed(rec)
                except Exception:
                    pass
            n += 1
            if n % 20000 == 0:
                view.progress.update(
                    records=n, bytes=stats["bytes"],
                    pct=min(99, int(100 * stats["bytes"] / total)) if total else 99,
                    elapsed_s=round(time.time() - t0, 1))

        if saved:
            # The saved build's own span, and the catch-up as what it is:
            # records that arrived after that build, i.e. drift.
            meta = {"covers_from": saved["covers_from"],
                    "covers_to": saved["covers_to"],
                    "built_at": saved["built_at"],
                    "consumed_through": _newest(saved["consumed_through"], newest),
                    "live_through": _newest(saved["live_through"], newest),
                    "live_records": saved["live_records"] + n}
        else:
            meta = {"covers_from": since,
                    # What the build actually reached, not what it aimed at: an
                    # empty or lagging feed must not claim coverage up to `now`.
                    "covers_to": newest or t0,
                    "built_at": time.time(),
                    "consumed_through": newest,
                    "live_through": None,  # drift is measured from this build on
                    "live_records": 0}
        meta["files"] = [{k: e[k] for k in ("path", "dev", "ino", "head_n", "head",
                                            "end")}
                         for e in plan if "ino" in e]
        # Serialised now, before the swap: after it the live feed mutates these
        # reducers under the lock, and pickling them there would stall every
        # read for as long as it takes.
        blob, unsaved = None, None
        if self.store is not None:
            try:
                blob = snapshots.capture(reducers)
            except Exception as e:
                unsaved = "not saved: %s" % e

        with self.lock:
            view.reducers = reducers
            view.dispatch = dispatch
            # `replay_dropped` stays the view's own: a seam drop is a record the
            # LIVE feed lost, and a resume re-reads the files past the saved
            # offsets, so a saved build carries none of its predecessor's.
            for k in ("covers_from", "covers_to", "built_at", "consumed_through",
                      "live_through", "live_records"):
                setattr(view, k, meta[k])
            view.cache["source"] = "snapshot" if saved else "scan"
            if saved:
                view.cache["note"] = (
                    "restored the %s reduction built %s, then read the %s "
                    "record(s) written since" % (
                        label(view.minutes),
                        time.strftime("%Y-%m-%d %H:%M:%S",
                                      time.localtime(saved["built_at"])),
                        "{:,}".format(n)))
            elif unsaved:
                view.cache["note"] = unsaved
            pending, view._replay = view._replay, None
            for rec in pending or ():
                view.feed(rec)
            view.state = "ready"
            view.installed_at = time.time()
            view.progress.update(records=n, pct=100,
                                 elapsed_s=round(time.time() - t0, 1))
            view.materialize()

        if blob is not None:
            try:
                out = self.store.save(view.minutes, roots, meta, blob)
                view.cache["saved_at"] = time.strftime(
                    "%Y-%m-%d %H:%M:%S", time.localtime(out["saved_at"]))
            except Exception as e:      # a cache that cannot be written is no cache
                view.cache["note"] = "not saved: %s" % e

    def _resume(self, view, roots, t0):
        """The saved reduction for this window and what is left to read, or None.

        Returns `(meta, reducers, plan, total_bytes)`. Says on the view why a
        snapshot was not used, so "it re-read the whole window" is never a
        guess. Anything unreadable is discarded: a cache that cannot be read is
        a cache to rebuild, never a reason to fail the window.
        """
        if self.store is None:
            return None
        budget = view.window_s * self.budget_pct

        def check(meta):
            age = t0 - meta["built_at"]
            if age > budget:
                return ("saved build is %s old, past this window's %s drift "
                        "budget" % (label(max(1, age // 60)), label(max(1, budget // 60))))
            if age < -60:
                return "saved build is dated in the future"
            return None
        try:
            meta, state, why = self.store.load(view.minutes, roots, check)
            if meta is None:
                view.cache["note"] = "scanned: %s" % why
                return None
            days = int((t0 - meta["covers_from"]) // 86400) + 2
            got = resume_plan(roots, meta["covers_from"], meta["files"], days)
            if got is None:
                view.cache["note"] = ("scanned: the day-files the saved build read "
                                      "have changed since")
                return None
            reducers = snapshots.restore(view.fresh_reducers(), state)
        except Exception as e:
            view.cache["note"] = "scanned: saved build unreadable (%s); discarded" % e
            self.store.discard(view.minutes, roots)
            return None
        return (meta, reducers) + got

    # --------------------------------------------------------------- lookup
    def request(self, minutes):
        """The view for `minutes`, queueing a build when it is not held.

        Under the aggregator's lock: the ingest thread walks this same map once
        per record, and the build worker swaps reducers into it.
        """
        minutes = int(minutes)
        with self.lock:
            view = self.views.get(minutes)
            fresh = view is None
            if fresh:
                view = PanelView(self.cfg, self.classes, minutes)
                self.views[minutes] = view
                self._evict()
            self.views.move_to_end(minutes)
            view.touched = time.time()
            # A failed build must be retryable. Without this a transient read
            # error -- a day-file rotated mid-scan, a stale NFS handle -- would
            # wedge that window permanently, since `refresh` only walks ready
            # views and nothing else ever re-queues it.
            retry = view.state == "error"
            if retry:
                view.state = "queued"
        if fresh or retry:
            self._enqueue(minutes)
        return view

    def _evict(self):
        """Drop the least recently asked-for READY view past the cap.

        A building view is never evicted: its worker holds the only reference to
        the reducers it is filling, and dropping it mid-scan would spend the read
        for nothing.
        """
        while len(self.views) > self.max_views:
            victim = next((m for m, v in self.views.items()
                           if v.state in ("ready", "error")), None)
            if victim is None:
                return                  # everything in flight: let it finish
            del self.views[victim]

    # ----------------------------------------------------------------- ticks
    def feed(self, rec):
        for view in list(self.views.values()):
            view.feed(rec)

    def refresh(self):
        """Re-materialise every ready view, and re-scan the ones that drifted.

        Takes no clock: drift is now a property of what the views have ingested,
        not of how long ago they were built.
        """
        budget_pct = max(1, int(self.cfg.get("panels.max_drift_pct", 25))) / 100.0
        with self.lock:
            held = list(self.views.items())
        for minutes, view in held:
            if view.state != "ready":
                continue
            drift = view.drift_s() or 0
            if drift > view.window_s * budget_pct:
                self._enqueue(minutes)
            else:
                with self.lock:
                    view.materialize()

    def status(self, now=None):
        now = now or time.time()
        with self.lock:
            held = [v.status(now) for v in self.views.values()]
        return {"held": held, "max_windows": self.max_views,
                "queued": list(self._queue),
                "presets": presets(self.cfg),
                "retention_minutes": retention_min(self.cfg)}
