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

A user or agent-type filter is the same kind of thing as a window: a different
set of records, so a different set of reducers, not a mask over the default
view's numbers -- a quantile, a dedup index or a trajectory chain cannot be
un-mixed after the fact. So the registry keys a view by `(minutes, filter)`, and
a filtered view is built, live-fed, saved, resumed and evicted exactly as a
window is (`ViewFilter`). The unfiltered key is the bare minutes, so the
default view, its saved copy and the prewarm are what they were before filters
existed; a filtered view is evicted before any unfiltered one, and never pushes
out the default window.

And one thing is reused rather than recomputed: every build's reducer state is
saved with how far into each day-file it read (`snapshots`), so a restart or a
return to a window evicted from the LRU resumes it -- reading only what the
collector wrote since -- instead of reducing the whole window again. A resumed
view keeps its original `built_at`, and its status says it was restored.
"""
import collections
import hashlib
import json
import re
import threading
import time

from . import hours as hourmod, redact, snapshots
from .export import (UNTAGGED, agent_type_token, agent_type_want, parse_filters,
                     resume_plan, window_plan, window_scan)
from .normalize import Normalizer
from .procindex import ProcIndex
from .reducers.cascades import CascadeReducer
from .reducers.facets import FacetReducer
from .reducers.io import IoReducer
from .reducers.resources import ResourceReducer
from .reducers.risk import RiskReducer
from .reducers.sandbox import SandboxReducer
from .reducers.slurm import SlurmReducer
from .reducers.tools import ToolMixReducer
from .reducers.trajectories import TrajectoryReducer
from .reducers.workspace import WorkspaceReducer

HIST_REDUCERS = (ToolMixReducer, IoReducer, ResourceReducer, RiskReducer)
# The only records a view reads. A build skips the rest before parsing them, and
# the live feed, the replay buffer and the drift all ignore them, so "the newest
# record the view holds" means the same thing whichever path it came in by.
# The process index and the cascades read the tick records (residency, conn,
# netio) as well: a long-lived agent's identity is often only there.
EVENTS = frozenset(ev for r in HIST_REDUCERS + (SandboxReducer, TrajectoryReducer,
                                                ProcIndex, CascadeReducer, SlurmReducer)
                   for ev in r.EVENTS)

# Below this a window is mostly bin-edge noise: one minute of a login fleet is a
# handful of exits, and every distribution panel would read `n=0`.
MIN_WINDOW_MIN = 5

# Live records buffered during a rebuild, for replay across the read seam. The
# seam is sub-second in practice (the scan and the tailer reach EOF together),
# so this is generous; what matters is that overflow is COUNTED, not silent.
REPLAY_CAP = 20000

# The build worker's queue entry for "reduce the hours closed since" (`hours`).
HOURS_JOB = "_hours"

# Values one filter may name. A filter is a whole view -- reducer state and a
# window's read -- so a request naming hundreds is refused, not built.
FILTER_MAX_VALUES = 64
# Names the byte-level pre-check can match as written: a JSON writer could
# escape anything else, and a pre-check must never miss a line.
_PLAIN = re.compile(r"^[A-Za-z0-9._@+-]+$")


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


def _byte_check(field, values):
    """A pre-parse search for `"field": "<one of values>"`, or None when unsafe."""
    if not values or not all(_PLAIN.match(v) for v in values):
        return None
    names = b"|".join(re.escape(v.encode()) for v in sorted(values))
    return re.compile(rb'"' + field.encode() + rb'"\s*:\s*"(?:' + names + rb')"').search


class ViewFilter:
    """Which users' and which agent types' records a view holds; empty is all of them.

    `match` is the one predicate: the build applies it to every record it reads
    before dispatch, and the live feed applies it to every record it hands the
    view, so a filtered view counts exactly what an unfiltered one would have
    counted for those people. Users and agent types AND together; values within
    each OR. An agent type is the collector's own tag (`claude_code`, `codex`,
    `vscode`, ...), and `none` asks for the records it did not tag -- a person's
    own shell, and the editors it does not recognise.

    Records with no user and no agent type -- the per-host `residency_totals`
    ticks -- cannot be attributed to anyone, so a filtered view does not hold
    them, and `NOT_NARROWED` says what that leaves out rather than letting an
    empty series read as an idle person.
    """

    __slots__ = ("users", "agent_types", "key", "_u", "_t", "line_match")

    NOT_NARROWED = (
        "resident memory and live processes over time: sampled from each host's "
        "per-class residency ticks, which carry no user or agent type, so a "
        "filtered view has none",
        "login nodes right now: a host-wide snapshot",
        "the live tier (the Live tab's plates and per-minute rate, and the live "
        "tiles on Overview): one shared rolling count by class",
    )

    def __init__(self, users=(), agent_types=()):
        self.users = tuple(sorted({str(u) for u in users or () if u not in (None, "")}))
        self.agent_types = tuple(sorted(agent_type_want(
            str(t) for t in agent_types or () if t not in (None, ""))))
        self._u = frozenset(self.users)
        self._t = frozenset(self.agent_types)
        self.key = (self.users, self.agent_types) if self.users or self.agent_types \
            else None
        # A speed-up only, never the decision: `match` still runs on what it lets
        # through. A narrow filter on a busy feed parses a few percent of the
        # lines a full build does. `none` cannot be searched for -- an untagged
        # record may carry `null` or no key at all -- so it only narrows by user.
        checks = [c for c in (_byte_check("user", self.users),
                              None if UNTAGGED in self._t
                              else _byte_check("agent_type", self.agent_types))
                  if c is not None]
        if len(checks) == 2:
            a, b = checks
            self.line_match = lambda line: a(line) and b(line)
        else:
            self.line_match = checks[0] if checks else None

    @classmethod
    def from_params(cls, params):
        """From query params, in `/api/events`'s vocabulary: `user`, `agent_type`,
        each repeatable or comma-separated (`export.parse_filters`)."""
        f = parse_filters({k: params.get(k) for k in ("user", "agent_type")})
        users, types = f.get("user") or (), f.get("agent_type") or ()
        if len(users) > FILTER_MAX_VALUES or len(types) > FILTER_MAX_VALUES:
            raise ValueError("a view filter names at most %d users and %d agent "
                             "types" % (FILTER_MAX_VALUES, FILTER_MAX_VALUES))
        return cls(users, types)

    def __bool__(self):
        return self.key is not None

    def match(self, rec):
        if self._u and rec.get("user") not in self._u:
            return False
        if self._t and agent_type_token(rec.get("agent_type")) not in self._t:
            return False
        return True

    def slug(self):
        """A short stable name for the saved file: same filter, same name."""
        return hashlib.sha1(json.dumps(self.key).encode()).hexdigest()[:12]

    def as_dict(self):
        """What the response says it was built for; None when unfiltered."""
        if not self:
            return None
        return {"users": list(self.users), "agent_types": list(self.agent_types)}

    def block(self):
        """The `filter` block every filtered or unfiltered response carries."""
        return {"active": bool(self), "users": list(self.users),
                "agent_types": list(self.agent_types),
                "not_narrowed": list(self.NOT_NARROWED) if self else []}


NO_FILTER = ViewFilter()


def view_key(minutes, flt=None):
    """The registry's key for a view. Unfiltered it is the bare minutes, so the
    default window -- its LRU slot, its queue entry, its saved file -- is keyed
    exactly as it was before filters existed."""
    return int(minutes) if not flt else (int(minutes), flt.key)


class PanelView:
    """One window's historical reducers, their build state, and their drift."""

    def __init__(self, cfg, classes, minutes, flt=None):
        self.cfg = cfg
        self.classes = list(classes)
        self.minutes = int(minutes)
        self.flt = flt or NO_FILTER
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
        self._fresh = {}                # key -> (when, live_records then) it was materialised
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
        if self.flt and not self.flt.match(rec):
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
        red = {r.KEY: r(self.cfg, self.classes) for r in HIST_REDUCERS
               if r is not ResourceReducer}
        # Its time series bins at a width chosen for the window.
        red[ResourceReducer.KEY] = ResourceReducer(self.cfg, self.classes,
                                                   window_s=self.window_s)
        # One process index per window, shared by the reducers that count
        # sessions: they fold the collector's session keys through it.
        procs = red[ProcIndex.KEY] = ProcIndex(self.cfg)
        sb = red["sandbox"] = SandboxReducer(self.cfg, self.classes, procs=procs)
        # What each session asks of the scheduler, folded the same way. A
        # cluster without Slurm (`slurm.enabled: false`) does not reduce it at
        # all: every panel that reads it then says it is absent.
        if self.cfg.get("slurm.enabled", True):
            red[SlurmReducer.KEY] = SlurmReducer(self.cfg, self.classes,
                                                 window_s=self.window_s, procs=procs)
        traj = red["trajectories"] = TrajectoryReducer(
            self.cfg, self.classes, window_s=self.window_s, procs=procs)
        # Where each agent worked; the cascades compose it per agent and subagent,
        # and tag each with whether its calls ran in a sandbox.
        ws = red[WorkspaceReducer.KEY] = WorkspaceReducer(
            self.cfg, self.classes, window_s=self.window_s, procs=procs)
        red["cascades"] = CascadeReducer(self.cfg, self.classes, window_s=self.window_s,
                                         procs=procs, traj=traj, ws=ws, sb=sb)
        red[FacetReducer.KEY] = FacetReducer(self.cfg, self.classes)
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
        # The reducers hold raw argv, because their detectors need it; nothing
        # leaves for the API without its credentials masked (`redact`).
        self.payload = redact.payload(out)
        self._fresh = {k: (time.time(), self.live_records) for k in out}
        return self.payload

    def materialize_key(self, key, max_age_s):
        """Re-materialise one reducer's payload when it is older than
        `max_age_s` and the view has ingested since. The rest wait for the
        next `refresh`: this is for a panel read live, between rebuilds."""
        r = self.reducers.get(key)
        if r is None or self._replay is not None:
            return
        at, seen = self._fresh.get(key, (0.0, None))
        now = time.time()
        if now - at < max_age_s or seen == self.live_records:
            return
        try:
            out = r.result()
        except Exception as e:
            out = {"_error": str(e)}
        self.payload = dict(self.payload, **{key: redact.payload(out)})
        self._fresh[key] = (now, self.live_records)

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
            # Which records this view holds; None is all of them. A held-window
            # list mixes filtered and unfiltered views of the same minutes, and
            # the page must not take one's progress for the other's.
            "filter": self.flt.as_dict(),
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
        self.views = collections.OrderedDict()   # view_key -> PanelView
        self.max_views = max(1, int(cfg.get("panels.max_windows", 2)))
        self.budget_pct = max(1, int(cfg.get("panels.max_drift_pct", 25))) / 100.0
        self.store = snapshots.SnapshotStore.from_config(cfg)
        # Hourly merge-reduce (`hours`, `panels.merge_reduce`): an unfiltered
        # window is merged from saved hour states rather than re-read. None
        # when off, and every window is scanned as before.
        ret = retention_min(cfg)
        self.hours = hourmod.HourStore.from_config(
            cfg, classes, lambda: PanelView(cfg, classes, ret).fresh_reducers(), EVENTS)
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
                key = self._queue.popleft()
            if key == HOURS_JOB:
                try:
                    self.hours.advance(self.roots_fn())
                except Exception:
                    pass                # retried at the next refresh
                continue
            with self.lock:
                view = self.views.get(key)       # may have been evicted meanwhile
            if view is not None:
                try:
                    self._build(view)
                except Exception as e:                 # never kill the worker
                    view.state, view.error = "error", str(e)
                    view._replay = None

    def _enqueue(self, key):
        with self._cv:
            if key not in self._queue:
                self._queue.append(key)
                self._cv.notify_all()

    # ----------------------------------------------------------------- build
    def _build(self, view):
        if self.hours is not None and not view.flt and hourmod.build(self, view):
            return
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
        # The same predicate the live feed applies (`PanelView.feed`), so a
        # filtered view's scan and its live tail hold the same records. Its
        # `newest`, and so its span and drift, are those records' own.
        keep = view.flt.match if view.flt else None
        every = 20000 if keep is None else 500
        for rec, stats in window_scan(plan, norm, since, events=EVENTS,
                                      match=view.flt.line_match):
            if keep is not None and not keep(rec):
                continue
            ts = rec.get("_ts_epoch")
            if ts is not None and (newest is None or ts > newest):
                newest = ts
            for r in dispatch.get(rec.get("event"), ()):
                try:
                    r.feed(rec)
                except Exception:
                    pass
            n += 1
            if n % every == 0:
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
                        label(view.minutes) + (" (filtered)" if view.flt else ""),
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
                out = self.store.save(view.minutes, roots, meta, blob, flt=view.flt)
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
            meta, state, why = self.store.load(view.minutes, roots, check,
                                               flt=view.flt)
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
            self.store.discard(view.minutes, roots, flt=view.flt)
            return None
        return (meta, reducers) + got

    # --------------------------------------------------------------- lookup
    def request(self, minutes, flt=None):
        """The view for `minutes` (and `flt`), queueing a build when it is not held.

        Under the aggregator's lock: the ingest thread walks this same map once
        per record, and the build worker swaps reducers into it.
        """
        minutes = int(minutes)
        flt = flt or NO_FILTER
        key = view_key(minutes, flt)
        with self.lock:
            view = self.views.get(key)
            fresh = view is None
            if fresh:
                view = PanelView(self.cfg, self.classes, minutes, flt)
                self.views[key] = view
                self._evict(keep=key, filtered=bool(flt))
            self.views.move_to_end(key)
            view.touched = time.time()
            # A failed build must be retryable. Without this a transient read
            # error -- a day-file rotated mid-scan, a stale NFS handle -- would
            # wedge that window permanently, since `refresh` only walks ready
            # views and nothing else ever re-queues it.
            retry = view.state == "error"
            if retry:
                view.state = "queued"
        if fresh or retry:
            self._enqueue(key)
        return view

    def peek(self, minutes, flt=None):
        """The view held for this key, or None -- never queues a build."""
        with self.lock:
            return self.views.get(view_key(minutes, flt))

    def prewarm(self, minutes_list):
        """Build these windows in the background, behind anything asked for.

        Each goes to the FRONT of the LRU, so it is the first evicted: a window
        nobody opened must never push out one somebody did. Skipped once the
        registry is full, for the same reason. Built, it is saved like any
        other, so a later switch to it resumes instead of re-reading."""
        for m in minutes_list:
            m = int(m)
            with self.lock:
                if m in self.views or len(self.views) >= self.max_views:
                    continue
                self.views[m] = PanelView(self.cfg, self.classes, m)
                self.views.move_to_end(m, last=False)
            self._enqueue(m)

    def _evict(self, keep=None, filtered=False):
        """Drop the least recently asked-for READY view past the cap.

        A building view is never evicted: its worker holds the only reference to
        the reducers it is filling, and dropping it mid-scan would spend the read
        for nothing.

        Filtered views go first, oldest first, and one still waiting in the
        queue goes too -- nothing has been read for it yet, and a reader clicking
        through four users in a row wants the fourth, not all four built in turn.
        Only then an unfiltered view, as before; and a FILTERED request never
        evicts the default window, which nearly every reader is on. With no
        filtered views held this is exactly the rule it always was.
        """
        default = resolve(self.cfg, None)["minutes"]
        while len(self.views) > self.max_views:
            victim = next((k for k, v in self.views.items()
                           if k != keep and v.flt
                           and v.state in ("ready", "error", "queued")), None)
            if victim is None:
                victim = next((k for k, v in self.views.items()
                               if k != keep and not v.flt
                               and v.state in ("ready", "error")
                               and not (filtered and k == default)), None)
            if victim is None:
                return                  # everything in flight: let it finish
            del self.views[victim]
            with self._cv:
                try:
                    self._queue.remove(victim)
                except ValueError:
                    pass

    # ----------------------------------------------------------------- ticks
    def feed(self, rec):
        if self.hours is not None:
            self.hours.saw(rec.get("_ts_epoch"))
        for view in list(self.views.values()):
            view.feed(rec)

    def refresh(self):
        """Re-materialise every ready view, and re-scan the ones that drifted.

        Takes no clock: drift is now a property of what the views have ingested,
        not of how long ago they were built.
        """
        budget_pct = max(1, int(self.cfg.get("panels.max_drift_pct", 25))) / 100.0
        if self.hours is not None and self.hours.behind(time.time(), self.roots_fn()):
            self._enqueue(HOURS_JOB)    # reduce the hours closed since, behind the builds
        with self.lock:
            held = list(self.views.items())
        for key, view in held:
            if view.state != "ready":
                continue
            drift = view.drift_s() or 0
            if drift > view.window_s * budget_pct:
                self._enqueue(key)
            else:
                with self.lock:
                    view.materialize()

    def status(self, now=None):
        now = now or time.time()
        with self.lock:
            held = [v.status(now) for v in self.views.values()]
            queued = list(self._queue)
        return {"held": held, "max_windows": self.max_views,
                # The unfiltered windows by minutes, as always; filtered ones
                # apart, so a reader of `queued` never mistakes one for the other.
                "queued": [k for k in queued if isinstance(k, int)],
                "queued_filtered": [{"minutes": k[0], "users": list(k[1][0]),
                                     "agent_types": list(k[1][1])}
                                    for k in queued if isinstance(k, tuple)],
                "presets": presets(self.cfg),
                "retention_minutes": retention_min(self.cfg),
                "hours": self.hours.status() if self.hours is not None else None}
