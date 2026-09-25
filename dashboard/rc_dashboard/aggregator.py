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
  panels are visible rather than implied.
"""
import itertools
import json
import threading
import time

from . import feeds as feedmod
from . import windows as winmod
from .normalize import Normalizer
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
        self.classes = list(cfg.get("filters.classes") or []) + \
            [cfg.get("filters.unlabeled_class", "unlabeled")]
        self.norm = Normalizer(cfg)
        self.lock = threading.RLock()
        self.reports = feedmod.resolve(cfg)
        self.source = FileTailSource(
            self.reports["ebpf"]["resolved"] or cfg.get("feeds.ebpf.roots") or [],
            days=int(cfg.get("feeds.ebpf.days", 8)))
        # One live reducer, sized at retention; every window slices it.
        self.live = LiveReducer(cfg, self.classes)
        self.node = NodeReducer(cfg, self.classes)
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

    def roots(self):
        return (self.reports["ebpf"]["resolved"]
                or self.cfg.get("feeds.ebpf.roots") or [])

    def start(self):
        self.panels_by_window.start()

    def stop(self):
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
                with open(f["path"], errors="replace") as fh:
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
            self.node_built_at = time.strftime("%Y-%m-%d %H:%M:%S")
        self.panels_by_window.refresh()
        return self.node_result

    def window(self, window_min):
        """Resolve a requested window against retention.  One rule, one place."""
        return winmod.resolve(self.cfg, window_min)

    def panels(self, window_min=None):
        """Historical panels for one window, each stamped with its feed's presence.

        A window still building returns its envelope with `_status='building'`
        and the progress, rather than an empty panel: "no rows yet" and "not
        finished reading" are different facts and must not render alike.
        """
        win = self.window(window_min)
        view = self.panels_by_window.request(win["minutes"])
        with self.lock:
            ebpf = feedmod.panel_meta(self.reports["ebpf"])
            node = feedmod.panel_meta(self.reports["ebpf_node"])
            status = view.status()
            out = {}
            for key, payload in (view.payload or {}).items():
                if key == "trajectories":
                    continue                       # served by /api/trajectories
                body = dict(payload) if isinstance(payload, dict) else {"rows": payload}
                body.update(ebpf)
                body["_window"] = status
                out[key] = body
            # `_status` stays the FEED's status and `_window.state` the build's:
            # "the collector wrote nothing" and "we have not finished reading it"
            # are different facts, and a panel that renders them alike is the
            # zero-is-not-missing mistake wearing a different hat.
            for key in ("tool_mix", "io_process", "resources", "sandbox", "risk"):
                if key not in out:
                    out[key] = dict(ebpf, _window=status)
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
            return out

    def trajectories(self, window_min=None):
        win = self.window(window_min)
        view = self.panels_by_window.request(win["minutes"])
        with self.lock:
            payload = (view.payload or {}).get("trajectories")
            out = dict(payload) if isinstance(payload, dict) else {}
            out.update(feedmod.panel_meta(self.reports["ebpf"]))
            out["_window"] = view.status()
            return out
