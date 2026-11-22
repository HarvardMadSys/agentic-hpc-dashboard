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
  The seam is drawn in bytes, not in time: a live record is one the build
  already read when its line ends where the build stopped reading that file, or
  before (`PanelView.seam`). Hosts lag one another, so a record older than the
  newest one the build read can still be one it never saw.

A user or agent-type filter is the same kind of thing as a window: a different
set of records, so a different set of reducers, not a mask over the default
view's numbers -- a quantile, a dedup index or a trajectory chain cannot be
un-mixed after the fact. So the registry keys a view by `(minutes, filter)`, and
a filtered view is built, live-fed, saved, resumed and evicted exactly as a
window is (`ViewFilter`) -- save that it is also built again, and never
resumed, once the hour still open as it was built has closed: it joined that
hour's exits' own disk bytes within itself, and the hours now hold the figures
worked out over every record (`WindowRegistry._own_figures`). The unfiltered
key is the bare minutes, so the default view, its saved copy and the prewarm
are what they were before filters existed; a filtered view is evicted before
any unfiltered one, and never pushes out the default window.

And one thing is reused rather than recomputed: every build's reducer state is
saved with how far into each day-file it read (`snapshots`), so a restart or a
return to a window evicted from the LRU resumes it -- reading only what the
collector wrote since -- instead of reducing the whole window again. A resumed
view keeps its original `built_at`, and its status says it was restored.

The panels each view materialises are saved as well (`payloads`), so a view the
registry creates -- after a restart, or for a window the LRU dropped -- shows
the last ones at once, with their age, as a drift re-build shows its previous
reduction, until its own build swaps fresh ones in.
"""
import collections
import hashlib
import json
import re
import threading
import time

from . import gz, hours as hourmod, payloads, redact, snapshots
from .anonymize import Verbatim
from .export import (UNTAGGED, agent_type_token, agent_type_want, agent_type_where,
                     parse_filters, resume_plan, window_plan, window_scan)
from .feedfile import HEAD_BYTES
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
# Reduce every closed hour in retention and merge its whole days, ahead of any
# window (`panels.preprocess`, `hours.HourStore.preprocess`).
PREPROCESS_JOB = "_preprocess"

# Values one filter may name. A filter is a whole view -- reducer state and a
# window's read -- so a request naming hundreds is refused, not built.
FILTER_MAX_VALUES = 64
# Names the byte-level pre-check can match as written: a JSON writer could
# escape anything else, and a pre-check must never miss a line.
_PLAIN = re.compile(r"^[A-Za-z0-9._@+-]+$")


def _newest(a, b):
    return b if a is None else a if b is None else max(a, b)


def _hour_label(h):
    """Epoch hour `h` as the local time it begins at, and its zone: the
    service's, which a reader of its status may not share."""
    return time.strftime("%Y-%m-%d %H:00 %Z", time.localtime(h * hourmod.HOUR))


# How much of what a build read just before where it stopped in a plain
# day-file its seam keeps a digest of, beside its first bytes' (`seam_of`).
TAIL_BYTES = 4096


def _twin(path):
    """The stream's other name: its `.gz` once sealed, or its plain file."""
    return gz.plain_name(path) if gz.is_gz(path) else path + gz.SUFFIX


class Look:
    """What a day-file holds now, read once and outside the aggregator's lock,
    for the views' seams to compare with when the tail went back to its start
    (`PanelView.restarted`): its size, its first HEAD_BYTES and, for a plain
    file, the TAIL_BYTES before each of `ends`, where builds stopped reading."""

    def __init__(self, path, ends=()):
        self.size = None
        self.first = b""
        self.before = {}
        try:
            with gz.open_feed(path) as fh:
                size = gz.fstat(fh).st_size
                self.first = fh.read(HEAD_BYTES)
                if not gz.is_gz(path):
                    for end in set(ends):
                        if end <= size:
                            n = min(TAIL_BYTES, end)
                            fh.seek(end - n)
                            self.before[end] = fh.read(n)
                self.size = size
        except OSError:
            pass

    def holds(self, got):
        """Whether the file still holds what a build read of it (`got`, its seam
        entry): at least as many bytes as it read, the same first ones, and, in
        a plain file, the same TAIL_BYTES before where it stopped.

        Detected: a file cut short below where the build stopped; one written
        again with other first bytes; a plain file written again past where the
        build stopped with its first bytes as they were, its bytes before that
        point not. Not: a change elsewhere between the two, nor, in a `.gz`
        (read past its head only by inflating it), past its first bytes -- a
        sealed day is not cut short in place."""
        _dev, _ino, end, head_n, head, tail = got
        if self.size is None or self.size < end \
                or hashlib.sha1(self.first[:head_n]).hexdigest() != head:
            return False
        if tail is None:
            return True                 # a `.gz`: its first bytes stand for it
        before = self.before.get(end)
        return before is not None and hashlib.sha1(before).hexdigest() == tail


def label(minutes):
    """`90m`, `6h`, `24h`, `7d` -- the same string the picker shows, made once
    here so the page and the service cannot disagree about what to call it.

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
    """A pre-parse pattern for `"field": "<one of values>"`, or None when unsafe."""
    if not values or not all(_PLAIN.match(v) for v in values):
        return None
    names = b"|".join(re.escape(v.encode()) for v in sorted(values))
    return re.compile(rb'"' + field.encode() + rb'"\s*:\s*"(?:' + names + rb')"')


class ViewFilter:
    """Which users' and which agent types' records a view holds; empty is all of them.

    `match` is the one predicate: the build applies it to every record it reads
    before dispatch, and the live feed applies it to every record it hands the
    view, so a filtered view counts exactly what an unfiltered one would have
    counted for those people. Users and agent types AND together; values within
    each OR. An agent type is the collector's own tag (`claude_code`, `codex`,
    `vscode`, ...), and `none` asks for the records it did not tag -- a person's
    own shell, and the editors it does not recognise.

    One figure is not the records' own: a process's disk bytes less what its
    reaped children carried (reducers/own_io.py), where the child may be one the
    filter leaves out -- a Codex a Claude started, under `agent_type=claude_code`.
    The build gives each exit of a closed hour the figure the hours worked out
    over every record (`WindowRegistry._own_figures`), loading those of the
    records the filter keeps (`holds`); the hour still open, and the live feed,
    are joined over what the filter keeps, until that hour closes: the view is
    then built again (`WindowRegistry.refresh`), and a saved build of it is
    not resumed (`WindowRegistry._resume`).

    Records with no user and no agent type -- the per-host `residency_totals`
    ticks -- cannot be attributed to anyone, so a filtered view does not hold
    them, and `NOT_NARROWED` says what that leaves out rather than letting an
    empty series read as an idle person.
    """

    __slots__ = ("users", "agent_types", "key", "_u", "_t", "line_match", "line_find",
                 "where")

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
        # The reader's own text, so every filter block and window status echoes
        # it as sent: rewritten as a name, `alice` came back as her alias.
        self.agent_types = tuple(sorted(Verbatim(t) for t in agent_type_want(
            str(t) for t in agent_types or () if t not in (None, ""))))
        self._u = frozenset(self.users)
        self._t = frozenset(self.agent_types)
        self.key = (self.users, self.agent_types) if self.users or self.agent_types \
            else None
        # A speed-up only, never the decision: `match` still runs on what it lets
        # through. A narrow filter on a busy feed parses a few percent of the
        # lines a full build does. `none` cannot be searched for -- an untagged
        # record may carry `null` or no key at all -- so it only narrows by user.
        checks = [c.search for c in (_byte_check("user", self.users),
                                     None if UNTAGGED in self._t
                                     else _byte_check("agent_type", self.agent_types))
                  if c is not None]
        if len(checks) == 2:
            a, b = checks
            self.line_match = lambda line: a(line) and b(line)
        else:
            self.line_match = checks[0] if checks else None
        # The first check's pattern, which the scan runs over whole blocks of a
        # day-file to find the lines worth looking at (`export.window_scan`
        # `find`); `line_match` still decides each one it finds.
        self.line_find = checks[0].__self__ if checks else None
        # The same narrowing for a sealed file's replica (`where.py`), from
        # the same inputs: it rejects a record before building it, and
        # `match` still decides each one it lets through. `none` it can test:
        # a null or empty agent type is `str(v)` "None" or "".
        w = {}
        if self.users:
            w["user"] = frozenset(self.users)
        if self.agent_types:
            w["agent_type"] = agent_type_where(self.agent_types)
        self.where = w or None

    @classmethod
    def from_params(cls, params):
        """From query params, in the filters' vocabulary: `user`, `agent_type`,
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

    def holds(self, part):
        """Whether the records of `part`, a user and an agent type token
        (`hours.own_part`), are ones `match` keeps: the own figures a build of
        this view loads (`hours.OwnFigures`)."""
        user, token = part
        return (not self._u or user in self._u) and (not self._t or token in self._t)

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
        # A filtered view's, with hours kept: the hour still open as its build
        # read, from which it joins its exits' own disk bytes within itself
        # (`WindowRegistry._own_figures`); built again once it has closed
        # (`WindowRegistry.refresh`). None for any other view.
        self.own_open_hour = None
        self.consumed_through = None    # newest ts the build ingested
        # Where the build read each day-file to: path -> (st_dev, st_ino, the
        # offset just past the last whole line it read). A live record whose
        # line ends there or before is one the build already counted.
        self.seam = {}
        # Live records not counted: the build had read them already, or they
        # are older than the window's start (a host catching up on records
        # from before it).
        self.live_skipped = {"already_read": 0, "before_window": 0}
        self.reducers = {}              # key -> reducer, live-fed after the swap
        self.dispatch = {}              # event -> [reducer]
        self.payload = {}               # materialised result(), per reducer key
        self.materialized_at = None     # epoch `payload` was materialised (all keys)
        self._fresh = {}                # key -> (when, live_records then) it was materialised
        self.touched = time.time()
        self.replay_dropped = 0
        self.live_records = 0
        self.live_through = None        # newest ts ingested LIVE since the build
        self._replay = None             # deque while a build is in flight
        self._replaying = False         # feeding what was held, as the build installs
        # Where the reducers came from and what happened to the saved copy:
        # `source` is "scan" or "snapshot", `note` says why a snapshot was or
        # was not used, `saved_at` when this build was last written down.
        # And the panels (`payloads`): `restored` says, while the view shows
        # panels saved by an earlier view of its window, which ones; `panels_saved_at`
        # is when its own were last written down; `panels_note` why saved
        # panels were not shown, or why its own could not be saved. `own_io`,
        # of a filtered view, where its exits' own disk bytes came from, and
        # from which hour it joins them within itself until that hour closes
        # and it is built again (`WindowRegistry._own_figures`, `refresh`).
        self.cache = {"source": None, "note": None, "saved_at": None,
                      "restored": None, "panels_saved_at": None, "panels_note": None,
                      "own_io": None}

    # ------------------------------------------------------------------ feed
    def feed(self, rec, at=None, unread=False):
        """Called under the aggregator's lock, once per live record. `at` is
        where the tail read it, `((path, st_dev, st_ino), end, ...)`, `end` the
        offset just past its line (`tail.FileTailSource.poll_at`). `unread`: a
        record the build did not read, whatever the seam says (`replay`).

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
                self._replay.append((rec, at))
            return
        if not self.dispatch:
            return
        ts = rec.get("_ts_epoch")
        # Already counted: the build read the same line from disk.
        if not unread and self._read_already(ts, at):
            self.live_skipped["already_read"] += 1
            return
        # Not in the window: a scan now would pass it over, as the build did.
        if ts is not None and self.covers_from is not None and ts < self.covers_from:
            self.live_skipped["before_window"] += 1
            return
        self.live_records += 1
        if ts is not None and (self.live_through is None or ts > self.live_through):
            self.live_through = ts
        for r in self.dispatch.get(rec.get("event"), ()):
            try:
                r.feed(rec)
            except Exception:
                pass                    # one bad record must never stop the stream

    def _read_already(self, ts, at):
        """Whether the build already read the live record read at `at`: its line
        ends where the build stopped reading its file, or before (`seam`).

        By its place, not its time. `consumed_through` is the newest record the
        build read from ANY file, and a host whose records lagged the others'
        when the view was built writes its catch-up older than that: by time,
        all of it was dropped until the next re-scan, while the live tier
        counted it. A record that does not say where it was read falls back to
        its time.

        Once the tail is past the build's end in a file, every line it reads
        of it after is too: the file leaves `seam`. A record of another file
        under its name (another inode) is not one the build read: live, the
        file leaves `seam` with it, the tail being on the new file for good;
        not as held records are replayed (`replay`), where a record of the file
        the build read may come after it. A day the collector compresses is
        the same bytes at the same offsets under its other name, whichever of
        the two the build read and the tail read it under (`tail.py`): the
        build the plain file and the tail the `.gz` it was sealed into since,
        or the tail the plain file while the build ran and the build the
        `.gz`."""
        if at is None:
            return ts is not None and self.consumed_through is not None \
                and ts <= self.consumed_through
        seam = self.seam
        if not seam:
            return False
        (path, dev, ino), end = at[0], at[1]
        key = path
        got = seam.get(path)
        if got is None:
            key = gz.plain_name(path) if gz.is_gz(path) else path + gz.SUFFIX
            got = seam.get(key)
            if got is None:
                return False
        elif got[0] != dev or got[1] != ino:
            # Another file under its name: not one the build read. Held for
            # replay, a record of the file the build read may come after it, so
            # the seam stays; live, the tail is on the new file for good.
            if not self._replaying:
                del seam[key]
            return False
        if end <= got[2]:
            return True
        del seam[key]
        return False

    def restarted(self, path, look=None):
        """The tail went back to the start of `path`: it was cut short in place,
        its inode the same. A build that read it before that read what is gone,
        so the file leaves the seam and nothing of it is skipped as read; one
        that read it after read what the tail now reads again, and keeps it.
        Which, `look` says (`Look.holds`: what the file holds now, read outside
        the lock); without one, the file leaves the seam. The tail notices a cut
        only at its next poll, after a build may have read the file. Under the
        aggregator's lock, before the tail hands over what it read there. A
        build under way installs a seam of its own, so it is told too, in order
        with the records held for it (`_replay`, `replay`)."""
        for key in (path, _twin(path)):
            got = self.seam.get(key)
            if got is not None and (look is None or not look.holds(got)):
                del self.seam[key]
        if self._replay is not None:
            self._replay.append((None, path))

    def replay(self, pending, held=None, checked=0):
        """The records held while the build ran (`_replay`), fed now its seam is
        in, and the tail's notices among them (`restarted`), each where it came.

        The first `checked` of `pending` were looked at before the lock was
        taken (`WindowRegistry.restarts_checked`), and `held` says, of each file
        a notice among them was for, whether the build read what it holds now.
        If it did, what the tail read of it before its last notice among them is
        another generation of the file, cut away before the build read it: fed
        as unread, the seam left as it is, and those notices keep it. If not,
        what came before a notice is judged by the seam, which the notice then
        drops. A notice past the first `checked` -- the file cut again after the
        check, whatever was decided for it there -- drops the seam, and what
        came between it and the last checked notice is judged by the seam.
        Under the aggregator's lock."""
        held = held or {}
        pending = list(pending or ())
        last = {at: i for i, (rec, at) in enumerate(pending[:checked]) if rec is None}
        self._replaying = True
        try:
            for i, (rec, at) in enumerate(pending):
                if rec is None:
                    if not (i < checked and held.get(at)):
                        self.restarted(at)
                elif at is not None and held.get(at[0][0]) and i < last.get(at[0][0], -1):
                    self.feed(rec, at, unread=True)
                else:
                    self.feed(rec, at)
        finally:
            self._replaying = False

    @staticmethod
    def seam_of(files, end_key):
        """The seam of a build that read `files` -- each its `path`, `dev`,
        `ino`, `head_n` and `head`, and where the read stopped under `end_key`:
        path -> (dev, ino, end, head_n, head, tail). `tail` is the digest of the
        TAIL_BYTES of a plain file before `end`, read back here as the build
        ends (`Look.holds`), or "" when it cannot be; a `.gz` has None, as it
        would be inflated up to `end` for it. Outside the aggregator's lock."""
        seam = {}
        for f in files:
            end, tail = f[end_key], None
            if not gz.is_gz(f["path"]):
                n, tail = min(TAIL_BYTES, end), ""
                try:
                    with open(f["path"], "rb") as fh:
                        fh.seek(end - n)
                        got = fh.read(n)
                    if len(got) == n:
                        tail = hashlib.sha1(got).hexdigest()
                except OSError:
                    pass
            seam[f["path"]] = (f["dev"], f["ino"], end, f["head_n"], f["head"], tail)
        return seam

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
        self.materialized_at = time.time()
        now = time.monotonic()
        self._fresh = {k: (now, self.live_records) for k in out}
        # These panels are the view's own: any it showed from a saved copy are gone.
        self.cache["restored"] = None
        return self.payload

    def materialize_stepwise(self, lock):
        """`materialize()`, taking `lock` one reducer at a time.

        Each reducer's `result()` is read, and masked, under the lock: the live
        feed changes reducer state under the same lock. The payload is swapped in
        whole once every reducer is done, so a reader meanwhile sees the previous
        one entire, and only if the reducers it was read from are still the
        view's: a build that installed new ones meanwhile materializes its own.
        One hold is then the slowest reducer's, not all of them together -- and
        every reader of the views and of the live tier, the tail included,
        waited out the whole of it after each build and refresh.

        What that gives up is one instant for the whole payload. A live record
        can land between two reducers' turns, so one panel may count a few
        records another does not yet -- a total ahead of a breakdown by what
        arrived in those milliseconds. `_fresh` stamps each key with its own time
        and count, the next refresh brings them level, and `_save_panels` saves
        the payload as it was read. `materialize()`, one hold for all, is still
        here for a caller that needs a single instant."""
        reds = self.reducers
        out, fresh = {}, {}
        for key in list(reds):
            with lock:
                r = reds.get(key)
                if r is None:
                    continue
                try:
                    res = r.result()
                except Exception as e:
                    res = {"_error": str(e)}
                out[key] = redact.payload(res)
                fresh[key] = (time.monotonic(), self.live_records)
            time.sleep(0)               # a waiter's turn at the lock
        with lock:
            if self.reducers is not reds:
                return self.payload
            self.payload = out
            self.materialized_at = time.time()
            self._fresh = fresh
            # These panels are the view's own: any it showed from a saved copy are gone.
            self.cache["restored"] = None
        return self.payload

    def materialize_key(self, key, max_age_s):
        """Re-materialise one reducer's payload when it is older than
        `max_age_s` and the view has ingested since. The rest wait for the
        next `refresh`: this is for a panel read live, between rebuilds. Its
        age is on the monotonic clock: a pinned one (`clock.as_of`) never moves."""
        r = self.reducers.get(key)
        if r is None or self._replay is not None:
            return
        at, seen = self._fresh.get(key, (float("-inf"), None))
        now = time.monotonic()
        if now - at < max_age_s or seen == self.live_records:
            return
        try:
            out = r.result()
        except Exception as e:
            out = {"_error": str(e)}
        self.payload = dict(self.payload, **{key: redact.payload(out)})
        self._fresh[key] = (now, self.live_records)

    # ---------------------------------------------------------- saved panels
    def saved_panels(self):
        """`(meta, payload)`: what `payloads` saves of this view, the panels last
        materialised and the span they cover. Under the aggregator's lock."""
        meta = {k: getattr(self, k) for k in payloads.SPAN}
        meta.update(materialized_at=self.materialized_at, source=self.cache.get("source"))
        return meta, self.payload

    def show_saved(self, meta, payload):
        """Show panels an earlier view of this window saved, until this view's
        own build swaps fresh ones in: as a drift re-build shows its previous
        reduction, with that reduction's own span, age and drift.

        No reducer is installed, so the live feed still waits for the build, and
        `installed_at` stays unset, so the page re-fetches when the build lands.
        Under the aggregator's lock."""
        self.payload = payload
        for k in payloads.SPAN:
            setattr(self, k, meta.get(k))
        self.materialized_at = meta.get("materialized_at")
        at = self.materialized_at or self.built_at
        self.cache["restored"] = (
            "showing the %s panels saved at %s, until this window is built again"
            % (label(self.minutes) + (" (filtered)" if self.flt else ""),
               time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(at)) if at else "an unknown time"))

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
            # what the live feed handed this view and it did not count, and why
            "live_skipped": dict(self.live_skipped),
            "replay_dropped": self.replay_dropped,
            "cache": dict(self.cache),
        }
        if drift is not None and drift > 0:
            st["covers_note"] = (
                "built over the last %s, plus %d s of records ingested since; "
                "re-scans at %d s of drift"
                % (label(self.minutes), int(drift), int(budget)))
        return st


class FreshReducers:
    """A window's fresh reducer set, made on call: the hour store's factory.
    A class rather than a closure so that it pickles -- the hour store's node
    workers (`hours`, `ingest.workers`) each make their own sets from it."""

    def __init__(self, cfg, classes, ret):
        self.cfg = cfg
        self.classes = list(classes)
        self.ret = ret

    def __call__(self):
        return PanelView(self.cfg, self.classes, self.ret).fresh_reducers()


class WindowRegistry:
    """A bounded LRU of built windows, with one serialised build worker.

    One worker, not a pool: two concurrent rebuilds would contend for the same
    day-file page cache and each make the other look slow, and the point of the
    seek-based plan is that a build is I/O-bound on exactly its window.
    """

    def __init__(self, cfg, classes, roots_fn, lock, ready=None):
        self.cfg = cfg
        self.classes = list(classes)
        self.roots_fn = roots_fn
        self.lock = lock                # the aggregator's; guards reducer swaps
        self.views = collections.OrderedDict()   # view_key -> PanelView
        self.max_views = max(1, int(cfg.get("panels.max_windows", 2)))
        if cfg.get("panels.preprocess"):
            # Every preset is built and held, so none evicts another; one slot
            # more for a filtered view or a window typed in by hand.
            self.max_views = max(self.max_views, len(presets(cfg)) + 1)
        self.budget_pct = max(1, int(cfg.get("panels.max_drift_pct", 25))) / 100.0
        self.store = snapshots.SnapshotStore.from_config(cfg)
        # The panels each view last served, shown by a view this registry
        # creates until its own build lands (`payloads`). None when off.
        self.payloads = payloads.PayloadStore.from_config(cfg)
        # Hourly merge-reduce (`hours`, `panels.merge_reduce`): an unfiltered
        # window is merged from saved hour states rather than re-read. None
        # when off, and every window is scanned as before.
        ret = retention_min(cfg)
        self.hours = hourmod.HourStore.from_config(
            cfg, classes, FreshReducers(cfg, classes, ret), EVENTS)
        self._queue = collections.deque()
        self._cv = threading.Condition()
        self._worker = None
        self._stop = False
        # Set when builds may start (the aggregator's `positioned`), or None. A
        # build is pure Python and keeps the GIL; the tail's positioning inflates
        # .gz files in short C calls that must each win it back, and beside a
        # build it got 5% of a core on rtx6000b (2026-11-13) -- while the live
        # tail, which reads nothing until positioning ends, stood still.
        self.ready = ready
        self._busy = False              # the worker is running a job (`wait_idle`)

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
        if self.hours is not None:
            self.hours.close()          # the node workers (`ingest.workers`)

    def _run(self):
        while self.ready is not None and not self.ready.is_set() and not self._stop:
            self.ready.wait(0.5)
        while not self._stop:
            with self._cv:
                while not self._queue and not self._stop:
                    self._cv.wait(timeout=5)
                if self._stop:
                    return
                key = self._queue.popleft()
                self._busy = True
            try:
                self._job(key)
            finally:
                with self._cv:
                    self._busy = False
                    self._cv.notify_all()

    def _job(self, key):
        if key == HOURS_JOB:
            try:
                self.hours.advance(self.roots_fn())
            except Exception:
                pass                # retried at the next refresh
            return
        if key == PREPROCESS_JOB:
            try:
                self.hours.preprocess(self.roots_fn())
            except Exception:
                pass                # each window build reduces what it lacks
            return
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

    def preprocess(self):
        """Queue the reduction of every closed hour in retention, ahead of the
        window builds, so each of them is a merge with nothing to reduce."""
        if self.hours is not None:
            self._enqueue(PREPROCESS_JOB)

    def wait_idle(self, timeout):
        """Block until nothing is queued or building, or `timeout` seconds
        pass. Returns whether it got there."""
        deadline = time.monotonic() + max(0.0, float(timeout))
        with self._cv:
            while self._queue or self._busy:
                left = deadline - time.monotonic()
                if left <= 0:
                    return False
                self._cv.wait(timeout=min(left, 1.0))
        return True

    # ----------------------------------------------------------------- build
    def _build(self, view):
        # Make room first. A request evicts only a ready view, so a window that
        # someone asks for while another view builds can leave both held past
        # `max_windows`. Apply the cap again here, before this build reads, with
        # the same rule as a request and with this view kept.
        with self.lock:
            self._evict(keep=view_key(view.minutes, view.flt), filtered=bool(view.flt))
        if self.hours is not None and not view.flt and hourmod.build(self, view):
            self._save_panels(view)
            return
        t0 = time.time()                # the present: the window's end
        m0 = time.monotonic()           # and this times the build
        view.state = "building"
        view.error = None
        roots = self.roots_fn()
        # A filtered view's exits take their own disk bytes from the hours,
        # which are reduced first if need be, before live records are held
        # for the build: a reduction can take as long as an unfiltered build's.
        own = self._own_figures(view, roots, t0)
        # And the hour still open as it reads (`hours.closed_end`): from it on,
        # it joins its exits within itself, over what its filter keeps (every
        # exit, if the hours could not be reduced). Saved with the build, whose
        # copy is resumed only while that hour is still open (`_resume`); held,
        # the view is built again once it has closed (`refresh`). None for an
        # unfiltered view, whose joins read every record, and with no hours kept.
        # Taken before the scan, as the figures are: an hour that closes while
        # a long scan runs is saved as the open one, and the next tick builds
        # the view again -- a second scan, expected, that takes its figures.
        open_hour = None
        if view.flt and self.hours is not None:
            open_hour = own.h_hi if own is not None else self.hours.closed_end(t0, roots)
        # Buffer live records from here, so the seam between "the scan reached
        # EOF" and "the fresh reducers are installed" replays rather than drops.
        with self.lock:
            view._replay = collections.deque()
        view.cache["note"] = None
        # Only a view with nothing in memory resumes. A drift re-build exists to
        # move the window's start forward, which a saved copy -- older still --
        # cannot do.
        resumed = None if view.reducers else self._resume(view, roots, t0, open_hour)
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
        # The scan's counts as it reads: what it has read when a record comes
        # through depends on how far each file was read ahead of it -- a line at
        # a time, a block, a replica's rows -- and only the total is the read's.
        scanned = {}
        for rec, stats in window_scan(plan, norm, since, events=EVENTS,
                                      match=view.flt.line_match, find=view.flt.line_find,
                                      where=view.flt.where, stats=scanned):
            if keep is not None and not keep(rec):
                continue
            if own is not None:
                own(rec)
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
                    elapsed_s=round(time.monotonic() - m0, 1))

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
        if open_hour is not None:
            # The hour still open as this build read. A resumed one's saved
            # part was open in no earlier hour (`_resume`), so this read's is
            # the whole's.
            meta["own_open_hour"] = open_hour
        # How far this read went in each file: the live feed's records up to
        # there are its own (`PanelView.feed`).
        seam = PanelView.seam_of(meta["files"], "end")
        # Serialised now, before the swap: after it the live feed mutates these
        # reducers under the lock, and pickling them there would stall every
        # read for as long as it takes.
        blob, unsaved = None, None
        if self.store is not None:
            try:
                blob = snapshots.capture(reducers)
            except Exception as e:
                unsaved = "not saved: %s" % e
        held, checked = self.restarts_checked(view, seam)

        with self.lock:
            view.reducers = reducers
            view.dispatch = dispatch
            # `replay_dropped` stays the view's own: a seam drop is a record the
            # LIVE feed lost, and a resume re-reads the files past the saved
            # offsets, so a saved build carries none of its predecessor's.
            for k in ("covers_from", "covers_to", "built_at", "consumed_through",
                      "live_through", "live_records"):
                setattr(view, k, meta[k])
            view.own_open_hour = open_hour
            view.seam = seam
            view.live_skipped = {"already_read": 0, "before_window": 0}
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
            if own is not None:
                view.cache["own_io"] = (
                    "the exits of the window's %d closed hour(s) took their own disk bytes "
                    "as the hours worked them out over every record (%d exit(s)%s%s); those of "
                    "the hour still open, from %s, and what the view is fed live, are joined "
                    "within it, over what its filter keeps, until that hour has closed and "
                    "the view is built again"
                    % (own.h_hi - own.h_lo, own.given,
                       " read since the saved build" if saved else "",
                       "; %d more, of hours with none saved, joined within it" % own.left
                       if own.left else "", _hour_label(own.h_hi)))
            pending, view._replay = view._replay, None
            view.replay(pending, held, checked)
        # The panels, a reducer per hold; the view says it is ready once they are in,
        # and until then shows what it showed while it was building.
        view.materialize_stepwise(self.lock)
        with self.lock:
            view.state = "ready"
            view.installed_at = time.time()
            view.progress.update(records=n, pct=100, bytes=scanned.get("bytes", 0),
                                 elapsed_s=round(time.monotonic() - m0, 1))

        if blob is not None:
            try:
                out = self.store.save(view.minutes, roots, meta, blob, flt=view.flt)
                view.cache["saved_at"] = time.strftime(
                    "%Y-%m-%d %H:%M:%S", time.localtime(out["saved_at"]))
            except Exception as e:      # a cache that cannot be written is no cache
                view.cache["note"] = "not saved: %s" % e
        self._save_panels(view)

    def _own_figures(self, view, roots, t0):
        """For a filtered view, what puts each exit's own disk bytes on the
        records its build reads (`hours.OwnFigures`), with the closed hours of
        its window reduced first, as an unfiltered build of it would.

        A filtered read never parses a record its filter leaves out
        (`ViewFilter`), so its joins never take such a child off its parent: a
        Claude whose figure holds a Codex it started kept the Codex's bytes
        under `agent_type=claude_code`. The hour chain reads every record, and
        works each exit's figure out once (reducers/own_io.py, `Ledger`); every
        exit of a closed hour takes it. One of the hour still open, or fed live,
        is joined within the view, as before, and so is every exit when there
        is no hour store or the hours cannot be reduced: the view says which
        (`cache.own_io`). With an hour store, that lasts until the hour still
        open has closed: a held view is then built again (`refresh`), and a
        saved build is not resumed (`_resume`). None for an unfiltered view,
        whose joins read every record."""
        view.cache["own_io"] = None
        if not view.flt:
            return None
        if self.hours is None:
            view.cache["own_io"] = ("each exit's own disk bytes joined within the view, from "
                                    "what its filter keeps: no hours are kept "
                                    "(panels.merge_reduce)")
            return None
        m0 = time.monotonic()
        prog = view.progress = {"pct": 0, "records": 0, "bytes": 0, "total_bytes": 0,
                                "files": 0, "elapsed_s": 0.0, "mode": "hours"}

        def step(i, n, records=0, bytes=0):
            prog.update(records=prog["records"] + records, bytes=prog["bytes"] + bytes,
                        total_bytes=prog["bytes"] + bytes, pct=min(99, int(100 * i / max(1, n))),
                        elapsed_s=round(time.monotonic() - m0, 1))
        own, got = hourmod.own_figures(self.hours, roots, t0 - view.window_s, t0,
                                       view.flt.holds, step)
        if own is None:
            view.cache["own_io"] = ("each exit's own disk bytes joined within the view, from "
                                    "what its filter keeps: %s; built again once the hour "
                                    "still open has closed" % got)
        return own

    # ---------------------------------------------------------- saved panels
    def _show_saved(self, view):
        """Give a view this registry just created the panels saved for its window,
        when there are any it may show (`payloads`). Read outside the lock, shown
        under it, and only while the view has nothing of its own: a build that
        landed first wins."""
        if self.payloads is None:
            return
        budget = view.window_s * self.budget_pct
        now = time.time()

        def check(meta):
            built = meta.get("built_at")
            if built is None:
                return "the saved panels carry no build time"
            age = now - built
            if age > budget:
                return ("saved panels were built %s ago, past this window's %s drift "
                        "budget" % (label(max(1, age // 60)), label(max(1, budget // 60))))
            if age < -60:
                return "saved panels are dated in the future"
            return None
        try:
            meta, payload, why = self.payloads.load(view.minutes, self.roots_fn(),
                                                    view.flt, check)
        except Exception as e:          # never a reason to fail the window
            meta, payload, why = None, None, "saved panels unreadable (%s)" % e
        with self.lock:
            if view.payload or view.reducers or view.state == "ready":
                return
            if meta is None:
                view.cache["panels_note"] = "panels not restored: %s" % why
                return
            view.show_saved(meta, payload)
            view.cache["panels_note"] = None

    def _save_panels(self, view):
        """Write down the panels a view just materialised (`payloads`). Taken
        under the lock, written outside it: the panels are never changed in
        place, only replaced."""
        if self.payloads is None:
            return
        with self.lock:
            if view.state != "ready" or not view.payload or view.cache.get("restored"):
                return
            meta, payload = view.saved_panels()
        try:
            out = self.payloads.save(view.minutes, self.roots_fn(), meta, payload,
                                     flt=view.flt)
        except Exception as e:          # a cache that cannot be written is no cache
            view.cache["panels_note"] = "panels not saved: %s" % e
            return
        if out is not None:
            view.cache["panels_saved_at"] = time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(out["saved_at"]))
            view.cache["panels_note"] = None

    def _resume(self, view, roots, t0, open_hour=None):
        """The saved reduction for this window and what is left to read, or None.

        Returns `(meta, reducers, plan, total_bytes)`. Says on the view why a
        snapshot was not used, so "it re-read the whole window" is never a
        guess. Anything unreadable is discarded: a cache that cannot be read is
        a cache to rebuild, never a reason to fail the window.

        `open_hour`, for a filtered view with hours kept, is the hour still open
        as this build reads (`hours.HourStore.closed_end`). A filtered build
        joins the exits of the hour still open within itself, over what its
        filter keeps (`_own_figures`); a build made once that hour has closed
        gives them the figures the hours worked out over every record instead,
        and a resumed copy, which reads on from where it stopped, never would.
        So a copy is refused once the hour it was open in has closed: a parent
        whose child the filter leaves out would keep the child's bytes.
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
            if open_hour is not None:
                was = meta.get("own_open_hour")
                if was is None:
                    return ("the saved build does not say which hour it joined its exits' "
                            "own disk bytes within itself from")
                if was < open_hour:
                    return ("the saved build joined its exits' own disk bytes within itself, "
                            "over what its filter keeps, from %s on; that hour has closed "
                            "since, and a build takes the figures the hours worked out over "
                            "every record there" % _hour_label(was))
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
        if fresh:
            # Before its build is queued, so no build can be in flight: the
            # saved panels go up first, and the build replaces them.
            self._show_saved(view)
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
                view = self.views[m] = PanelView(self.cfg, self.classes, m)
                self.views.move_to_end(m, last=False)
            self._show_saved(view)
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
    def feed(self, rec, at=None):
        if self.hours is not None:
            self.hours.saw(rec.get("_ts_epoch"))
        for view in list(self.views.values()):
            view.feed(rec, at)

    def seam_ends(self, paths):
        """Where each view's build stopped reading each of `paths`, under either
        of its names: what `Look` reads the bytes before. Under the
        aggregator's lock."""
        out = {}
        for view in list(self.views.values()):
            for path in paths:
                for key in (path, _twin(path)):
                    got = view.seam.get(key)
                    if got is not None:
                        out.setdefault(path, set()).add(got[2])
        return out

    def restarted(self, path, look=None):
        """The tail went back to the start of `path`; `look` is what it holds
        now (`PanelView.restarted`). Under the aggregator's lock."""
        for view in list(self.views.values()):
            view.restarted(path, look)

    def restarts_checked(self, view, seam):
        """`(held, checked)`: of each file the tail went back to the start of
        while `view`'s build ran -- the notices held for it -- whether the build
        read what it holds now, read now, outside the lock, against `seam`, the
        seam the build is about to install (`Look.holds`); and how many of the
        held entries that covers. A notice held after them, for a file cut
        again since, was not checked. `PanelView.replay` applies each decision
        where its notice came, so what was held before it is judged as it was
        read."""
        with self.lock:
            looked = list(view._replay or ())
        paths = {at for rec, at in looked if rec is None}
        out = {}
        for path in paths:
            keys = [k for k in (path, _twin(path)) if k in seam]
            look = Look(path, [seam[k][2] for k in keys])
            out[path] = bool(keys) and all(look.holds(seam[k]) for k in keys)
        return out, len(looked)

    def refresh(self):
        """Re-materialise every ready view, and re-scan the ones that drifted.

        Takes no clock: drift is now a property of what the views have ingested,
        not of how long ago they were built.

        And re-scan a filtered view once the hour still open as it was built
        has closed (`PanelView.own_open_hour`). It joined that hour's exits, and
        every record fed live since, within itself, over what its filter keeps:
        a parent whose child the filter leaves out kept the child's bytes. A
        build now gives them the figures the hours worked out over every record
        (`_own_figures`), a tick after the hour closes rather than at its drift
        budget: about one filtered scan an hour for each filtered view held.
        """
        budget_pct = max(1, int(self.cfg.get("panels.max_drift_pct", 25))) / 100.0
        with self.lock:
            held = list(self.views.items())
        closed = None
        if self.hours is not None:
            now, roots = time.time(), self.roots_fn()
            if self.hours.behind(now, roots):
                self._enqueue(HOURS_JOB)    # reduce the hours closed since, behind the builds
            if any(v.own_open_hour is not None for _k, v in held):
                closed = self.hours.closed_end(now, roots)
        for key, view in held:
            if view.state != "ready":
                continue
            drift = view.drift_s() or 0
            if drift > view.window_s * budget_pct or (
                    closed is not None and view.own_open_hour is not None
                    and view.own_open_hour < closed):
                self._enqueue(key)
            else:
                view.materialize_stepwise(self.lock)
                self._save_panels(view)

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
