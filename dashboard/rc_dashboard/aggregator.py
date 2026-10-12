"""One streaming pass, many reducers, plus the background tasks that feed them.

Dispatch is by event type, built from each reducer's `EVENTS`, so a reducer only
pays for the records it asked for -- a residency reducer must not be walked past
20M exit records.

Two aggregation tiers, because they have different composability:

* The LIVE tier updates continuously from the tail.  Its state is bucketed or
  incremental, so a week of retention costs hundreds of KB rather than the
  ~10-20 GB/day the raw records occupy, and every selected window is an exact
  slice of the same bins.  On every start it is refilled from the collector's
  files, newest first, while the tail already runs (`backfill_now`) -- it is
  never restored from anything this service wrote itself.
* The HISTORICAL tier (quantiles, CCDFs, Gini) is not resumable from a byte
  offset without persisted sketch state, so it cannot be sliced.  It is built
  PER WINDOW by `windows.WindowRegistry`, on a schedule and on demand, and
  publishes `built_at` and its drift -- the age and the true span of those
  panels are visible rather than implied.  Each build is saved with the byte
  offsets it read to (`snapshots`), which is the persisted sketch state that
  makes a restarted view resumable rather than re-read.
"""
import itertools
import json
import threading
import time

from . import anonymize
from . import ask
from . import clock
from . import feeds as feedmod
from . import gz
from .health import CollectorHealth
from .reducers.risk import ruleset as risk_ruleset
from . import windows as winmod
from .normalize import Normalizer, canonical_class
from .reducers.live import LiveReducer
from .reducers.resources import NodeReducer
from .tail import FileTailSource, read_back

# Records per lock hold during the backfill.  Between batches the tail and the
# API take their turn, so the page stays live and watches the history fill in
# instead of hanging for the length of the read.  Small, because a window build
# running alongside competes for the GIL and stretches every hold: measured with
# both running, 256 kept /api/live at p90 ~10 ms where 2000 let it reach ~150 ms,
# for a backfill 2% slower.
BACKFILL_BATCH = 256


class Aggregator:
    def __init__(self, cfg):
        self.cfg = cfg
        # a config written before the IDE class was named still says human-vscode
        self.classes = [canonical_class(c) for c in cfg.get("filters.classes") or []] + \
            [cfg.get("filters.unlabeled_class", "unlabeled")]
        self.norm = Normalizer(cfg)
        # The cluster's risk rules, built now: any secret pattern it adds must be
        # masked (redact.extend) before the first byte is served.
        risk_ruleset(cfg)
        self.lock = threading.RLock()
        self.reports = feedmod.resolve(cfg)
        self.source = FileTailSource(
            self.reports["ebpf"]["resolved"] or cfg.get("feeds.ebpf.roots") or [],
            days=int(cfg.get("feeds.ebpf.days", 8)))
        # One live reducer, sized at retention; every window slices it.
        self.live = LiveReducer(cfg, self.classes)
        self.node = NodeReducer(cfg, self.classes)
        # each collector's own account of itself: start, heartbeat, stop
        self.health = CollectorHealth(cfg)
        self.asker = ask.Asker(cfg)
        # Stable aliases for every name the service sends out, or None when off.
        # It learns accounts from the roster, so an alias a reader pastes into a
        # filter maps back even for someone the page has not shown yet.
        self.anon = anonymize.from_config(cfg, roster=self._accounts)
        self.panels_by_window = winmod.WindowRegistry(
            cfg, self.classes, self.roots, self.lock)
        self.node_built_at = None
        self.node_result = {}
        self.backfill = {"state": "idle", "pct": 0, "records": 0, "hours": 0}
        # Set once every file has been handed to the tail.  Until then a poll
        # reads nothing: a file the tail holds no offset for is read from byte
        # 0, and the backfill would then count the same history a second time.
        self.positioned = threading.Event()
        self.stats = {"records": 0, "last_poll": None}
        # Set at shutdown. The backfill runs on an executor thread, which the
        # interpreter waits for at exit; unchecked, a restart during the minutes
        # of a week's backfill outlived the dev runner's SIGINT grace, was
        # SIGKILLed only through its `uv run` wrapper, and the orphan kept the
        # port -- the next server failed to bind and every request was a 502.
        self.stopping = False

    def roots(self):
        return (self.reports["ebpf"]["resolved"]
                or self.cfg.get("feeds.ebpf.roots") or [])

    def start(self):
        self.panels_by_window.start()
        # The default window is what nearly every page load asks for first, so
        # its build -- or its resume from the saved copy -- starts with the
        # service rather than with the first reader.
        self.panels_by_window.request(self.window(None)["minutes"])
        # Then the cheap windows, so the first switch to one is instant rather
        # than a scan: queued behind the default, built by the same worker.
        self.panels_by_window.prewarm(
            [m for m in (self.cfg.get("panels.prewarm_min") or [])
             if m != self.window(None)["minutes"]])

    def stop(self):
        self.stopping = True
        self.panels_by_window.stop()

    # -------------------------------------------------------------- ingestion
    def consume(self, records, older=False):
        """Normalize and dispatch a batch.

        `older` marks the backfill: records that predate everything held, which
        feed the live tier only.  The historical views build from disk, oldest
        first, and their own scans already cover every row the backfill reads.
        """
        n = 0
        for raw in records:
            rec = self.norm.normalize(raw)
            if rec is None:
                continue
            if rec.get("event") in LiveReducer.EVENTS:
                try:
                    self.live.feed(rec, older=older)
                except Exception:
                    pass          # one bad record must never stop the stream
            if rec.get("event") in CollectorHealth.EVENTS:
                try:
                    self.health.feed(rec)
                except Exception:
                    pass
            if not older:
                # Each built window keeps ingesting after its scan, so the panels
                # stay current between rebuilds instead of ageing a full interval.
                self.panels_by_window.feed(rec)
            n += 1
        self.stats["records"] += n
        return n

    def read_node_tier(self):
        """The node tier is a SNAPSHOT feed, not an event stream.

        One ~200 KB JSON object per line, and only the newest couple per host
        matter, so this reads the tail of each file rather than streaming it --
        and it is deliberately not part of the event dispatch, which is built
        for millions of small records.
        """
        # Re-resolve rather than trust the cached report: the node tier writes its
        # first file minutes after start, so a report taken at construction time
        # says EMPTY forever and the panel would stay blank until the next
        # scheduled rebuild. Re-globbing a snapshot feed is cheap.
        rep = feedmod.resolve_dated("ebpf_node", self.cfg)
        self.reports["ebpf_node"] = rep
        keep = int(self.cfg.get("feeds.ebpf_node.snapshots_per_host", 2) or 2)
        n = 0
        for f in rep.get("files", []):
            try:
                with gz.open_text(f["path"]) as fh:
                    lines = fh.readlines()[-keep:]
            except OSError:
                continue
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    self.node.feed_snapshot(json.loads(line))
                    n += 1
                except ValueError:
                    continue
        return n

    def poll_once(self):
        if not self.positioned.is_set():
            return 0
        with self.lock:
            n = self.consume(self.source.poll())
            self.source.prune_state()
            self.stats["last_poll"] = time.time()
        return n

    def backfill_now(self):
        """Position the tail, then fill RETENTION behind it.  The one expensive read.

        Every file is handed to the tail at its end first, so the polling that
        app.py runs alongside this follows only what the collector writes from
        here on.  The history behind that point is then read NEWEST FIRST
        (`tail.read_back`) into the live tier, a batch per lock hold, so the page
        is live throughout and watches the bins fill from now backwards.  The
        historical tier is not filled here: it is built per selected window by
        the registry, which seeks to the window's first byte.

        Nothing about a previous run is consulted.  The span read is
        `min(backfill_hours, retention)` back from now, found by timestamp, so
        what the page holds is a function of the collector's files and the clock
        -- never of when this service was last running.
        """
        hours = int(self.cfg.get("live.backfill_hours", 168))
        span = min(hours * 3600, self.live.retention_s) if hours > 0 else 0
        since = time.time() - span if span else None
        try:
            with self.lock:
                plan = self.source.position(since)
                total = sum(e["end"] - e["start"] for e in plan)
                self.backfill = {
                    "state": "running" if span else "skipped",
                    "pct": 0 if span else 100, "records": 0,
                    "hours": hours if span else 0,
                    "from": (time.strftime("%Y-%m-%d %H:%M:%S",
                                           time.localtime(since))
                             if since else None),
                    "bytes": 0, "total_bytes": total, "files": len(plan)}
        except Exception as e:                     # never wedge the tail behind it
            self.backfill = {"state": "error", "error": str(e), "pct": 0,
                             "records": 0, "hours": hours}
            return 0
        finally:
            self.positioned.set()
        # Materialise the node tier now rather than at the first scheduled tick:
        # it is a snapshot feed, not a window aggregate, and leaving it blank for
        # a whole rebuild interval would read as an absent feed. Its failure is
        # its own -- recorded where node.result()'s would be -- and must not
        # leave the eBPF history unread.
        try:
            self.rebuild_history()
        except Exception as e:
            with self.lock:
                self.node_result = {"_error": str(e)}
        if not span:
            return 0

        t0 = time.time()
        stats = {"bytes": 0, "parse_errors": 0, "records": 0}

        def raws():
            for _ts, raw in read_back(plan, stats):
                stats["records"] += 1
                yield raw

        # Consumed lazily, a batch per lock hold, never materialised: holding a
        # batch of parsed records alive makes every garbage-collector pass
        # rescan them, which measured as a 50% slower read.
        stream = raws()
        try:
            while True:
                if self.stopping:
                    with self.lock:
                        self.backfill.update(state="stopped", records=stats["records"])
                    return stats["records"]
                with self.lock:
                    before = stats["records"]
                    self.consume(itertools.islice(stream, BACKFILL_BATCH), older=True)
                    if stats["records"] == before:
                        break
                    self.backfill.update(
                        records=stats["records"], bytes=stats["bytes"],
                        pct=min(99, int(100 * stats["bytes"] / total)) if total else 99)
                # A released lock is not handed over: this thread still holds
                # the GIL and would take it straight back, starving the tail and
                # every API read until the backfill ends. Yield so a waiter gets
                # its turn.
                time.sleep(0)
        except Exception as e:                     # the tail runs on regardless
            with self.lock:
                self.backfill.update(state="error", error=str(e),
                                     records=stats["records"])
            return stats["records"]
        seen = stats["records"]
        with self.lock:
            retained = self.live.buckets.oldest_epoch()
            self.backfill.update(
                state="done", pct=100, records=seen, bytes=stats["bytes"],
                parse_errors=stats["parse_errors"],
                elapsed_s=round(time.time() - t0, 1),
                # What the read actually reached back to. Younger than `from`
                # when the feed itself is -- a collector started inside the
                # window, or `feeds.ebpf.days` not covering it -- and the live
                # window states this too, so the gap reads as a gap in the old
                # bins instead of as a quiet fleet.
                retained_from=(time.strftime("%Y-%m-%d %H:%M:%S",
                                             time.localtime(retained))
                               if retained else None),
                retained_hours=(round((time.time() - retained) / 3600.0, 1)
                                if retained else None))
        return seen

    # ---------------------------------------------------------------- results
    def refresh_feeds(self):
        with self.lock:
            self.reports = feedmod.resolve(self.cfg)
            for name, rep in self.reports.items():
                if name == "ebpf":
                    rep["rows"] = self.stats["records"]
                    rep["schema_versions"] = dict(self.norm.stats["schema_versions"])
                    rep["excluded"] = {
                        "users": self.norm.stats["dropped_users"],
                        "self_observation": self.norm.stats["dropped_self_observation"],
                    }
                    rep["backfill"] = dict(self.backfill)
            return self.reports

    def collectors(self):
        """Is each collector OK: its own records (`health`) and its files on disk.

        Nothing here talks to a collector -- the files are the only interface, so
        a capture copied from another cluster reads the same as a live feed.
        """
        with self.lock:
            files = {}
            for tier in ("ebpf", "ebpf_node"):
                for f in (self.reports.get(tier) or {}).get("files", []):
                    d = files.setdefault(f.get("host") or "?", {}).setdefault(
                        tier, {"files": 0, "bytes": 0, "last_write": None})
                    d["files"] += 1
                    d["bytes"] += f.get("size") or 0
                    d["last_write"] = max(d["last_write"] or 0, f.get("mtime") or 0) or None
            tiers = {t: {k: (self.reports.get(t) or {}).get(k) for k in (
                         "status", "notice", "configured", "resolved", "env", "lag",
                         "newest_record_ts", "max_age_s", "hosts")}
                     for t in ("ebpf", "ebpf_node")}
            return {"hosts": self.health.report(files), "tiers": tiers,
                    "now": time.time(), "backfill": dict(self.backfill)}

    def rebuild_history(self):
        """The scheduled tick: refresh the node snapshot, then every held window.

        A view that has drifted past `panels.max_drift_pct` of its own window is
        re-scanned by the registry's worker; the rest are just re-materialised,
        which is what this always did.
        """
        self.read_node_tier()
        with self.lock:
            try:
                self.node_result = self.node.result()
            except Exception as e:
                self.node_result = {"_error": str(e)}
            self.node_built_at = time.strftime("%Y-%m-%d %H:%M:%S",
                                               time.localtime(time.time()))
        self.panels_by_window.refresh()
        return self.node_result

    def window(self, window_min):
        """Resolve a requested window against retention.  One rule, one place."""
        return winmod.resolve(self.cfg, window_min)

    def facets(self, minutes):
        """The filter's choices, from the UNFILTERED view of this window if it is
        held -- never a build of its own. A filtered view's own counts would
        offer only the people already picked, and the dropdown must keep
        offering everyone else."""
        view = self.panels_by_window.peek(minutes)
        fac = (view.payload or {}).get("facets") if view is not None else None
        return fac if isinstance(fac, dict) and "_error" not in fac else None

    def panels(self, window_min=None, flt=None):
        """Historical panels for one window, each stamped with its feed's presence.

        A window still building returns its envelope with `_status='building'`
        and the progress, rather than an empty panel: "no rows yet" and "not
        finished reading" are different facts and must not render alike.

        `flt` narrows every window panel to some users or agent types; that is a
        view of its own, built like a window (`windows.ViewFilter`), and the
        response's `filter` says which -- and what it could not narrow.
        """
        win = self.window(window_min)
        flt = flt or winmod.NO_FILTER
        view = self.panels_by_window.request(win["minutes"], flt)
        facets = self.facets(win["minutes"])
        with self.lock:
            ebpf = feedmod.panel_meta(self.reports["ebpf"])
            node = feedmod.panel_meta(self.reports["ebpf_node"])
            status = view.status()
            out = {}
            for key, payload in (view.payload or {}).items():
                if key in ("trajectories", "facets") or key.startswith("_"):
                    continue                       # /api/trajectories; below; internal
                body = dict(payload) if isinstance(payload, dict) else {"rows": payload}
                body.update(ebpf)
                body["_window"] = status
                body["_filter"] = flt.as_dict()
                out[key] = body
            # `_status` stays the FEED's status and `_window.state` the build's:
            # "the collector wrote nothing" and "we have not finished reading it"
            # are different facts, and a panel that renders them alike is the
            # zero-is-not-missing mistake wearing a different hat.
            for key in ("tool_mix", "io_process", "resources", "sandbox", "risk", "cascades"):
                if key not in out:
                    out[key] = dict(ebpf, _window=status, _filter=flt.as_dict())
            body = dict(self.node_result) if isinstance(self.node_result, dict) \
                else {"rows": self.node_result}
            body.update(node)
            out["node"] = body
            return {"panels": out,
                    # The node tier is a snapshot of current state, not a window
                    # aggregate, so it keeps its own build stamp.
                    "full_built_at": status["built_at"] or self.node_built_at,
                    "node_built_at": self.node_built_at,
                    "window": win,
                    "window_status": status,
                    "filter": flt.block(),
                    # Who and what the window holds, for the filter's choices:
                    # the unfiltered view's counts, whichever view this is.
                    "facets": facets,
                    "purposes": self.norm.purposes,
                    "purpose_src": self.norm.purpose_src,
                    "classes": self.classes}

    def live_payload(self, window_min=None):
        win = self.window(window_min)
        with self.lock:
            out = self.live.result(window_s=win["minutes"] * 60)
            out["window"].update(requested_minutes=win["requested"],
                                 clamped=win["clamped"], note=win["note"],
                                 label=win["label"])
            out.update(feedmod.panel_meta(self.reports["ebpf"]))
            out["backfill"] = dict(self.backfill)
            out["clock"] = clock.status()
            return out

    def ask_info(self):
        return ask.describe(self.cfg)

    def ask(self, question, window_min=None, history=()):
        """Start one question about the window; the agent runs in the background."""
        win = self.window(window_min)
        got = self.panels(window_min) or {}
        live = self.live_payload(window_min)
        # sessions come from their own endpoint: the per-class summary and the
        # busiest few, in plain fields
        tr = self.trajectories(window_min) or {}
        rows = tr.get("rows") or {}
        busiest = [rows[s] for s in (tr.get("ranks") or {}).get("events", [])[:15] if s in rows]
        keep = ("user", "actor3", "agent_type", "n_events", "cpu_s", "wall_s",
                "distinct_tools", "peak_rss_mb", "sandbox", "approval", "host")
        extra = {"facets": got.get("facets"),
                 "sessions": {"summary": tr.get("summary"),
                              "busiest": [{k: r.get(k) for k in keep} for r in busiest]}}
        panels = got.get("panels") or {}
        if self.anon is not None:
            # The snapshot leaves the host, for the model's provider: aliases only.
            panels, live, extra = (self.anon.payload(panels), self.anon.payload(live),
                                   self.anon.payload(extra))
        return self.asker.submit(question, panels, live, win["label"], history, extra)

    def _accounts(self):
        """Every account in the live roster's retention, for the anonymizer."""
        with self.lock:
            return list(self.live.roster.last)

    def ask_job(self, jid):
        return self.asker.view(jid)

    def live_users(self, window_min=None, q=None, limit=None, running=None):
        win = self.window(window_min)
        with self.lock:
            return self.live.users(window_s=win["minutes"] * 60, q=q, limit=limit,
                                   running=running)

    def live_user(self, name, window_min=None):
        win = self.window(window_min)
        with self.lock:
            return self.live.user_detail(name, window_s=win["minutes"] * 60)

    def trajectories(self, window_min=None, flt=None):
        win = self.window(window_min)
        flt = flt or winmod.NO_FILTER
        view = self.panels_by_window.request(win["minutes"], flt)
        with self.lock:
            payload = (view.payload or {}).get("trajectories")
            out = dict(payload) if isinstance(payload, dict) else {}
            out.update(feedmod.panel_meta(self.reports["ebpf"]))
            out["_window"] = view.status()
            out["filter"] = flt.block()
            return out
