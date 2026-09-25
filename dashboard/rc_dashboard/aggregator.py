"""One streaming pass, many reducers, plus the background tasks that feed them.

Dispatch is by event type, built from each reducer's `EVENTS`, so a reducer only
pays for the records it asked for -- a residency reducer must not be walked past
20M exit records.

Two aggregation tiers, because they have different composability:

* The LIVE tier updates continuously from the tail.  Its state is bucketed or
  incremental, so a week of retention costs hundreds of KB rather than the
  ~10-20 GB/day the raw records occupy, and every selected window is an exact
  slice of the same bins.
* The HISTORICAL tier (quantiles, CCDFs, Gini) is not resumable from a byte
  offset without persisted sketch state, so it cannot be sliced.  It is built
  PER WINDOW by `windows.WindowRegistry`, on a schedule and on demand, and
  publishes `built_at` and its drift -- the age and the true span of those
  panels are visible rather than implied.
"""
import json
import os
import threading
import time

from . import feeds as feedmod
from . import windows as winmod
from .normalize import Normalizer
from .reducers.live import LiveReducer
from .reducers.resources import NodeReducer
from .tail import FileTailSource


class Aggregator:
    def __init__(self, cfg):
        self.cfg = cfg
        self.classes = list(cfg.get("filters.classes") or []) + \
            [cfg.get("filters.unlabeled_class", "unlabeled")]
        self.norm = Normalizer(cfg)
        self.lock = threading.RLock()
        self.reports = feedmod.resolve(cfg)
        state = os.path.join(cfg.get("cache.state_dir"), "ebpf_offsets.json")
        self.source = FileTailSource(
            self.reports["ebpf"]["resolved"] or cfg.get("feeds.ebpf.roots") or [],
            days=int(cfg.get("feeds.ebpf.days", 8)), state_path=state,
            cold_start_mb=int(cfg.get("live.backfill_mb", 2048)))
        # One live reducer, sized at retention; every window slices it.
        self.live = LiveReducer(cfg, self.classes)
        self.node = NodeReducer(cfg, self.classes)
        self.panels_by_window = winmod.WindowRegistry(
            cfg, self.classes, self.roots, self.lock)
        self.node_built_at = None
        self.node_result = {}
        self.backfill = {"state": "idle", "pct": 0, "records": 0, "hours": 0}
        self.stats = {"records": 0, "last_poll": None}

    def roots(self):
        return (self.reports["ebpf"]["resolved"]
                or self.cfg.get("feeds.ebpf.roots") or [])

    def start(self):
        self.panels_by_window.start()

    def stop(self):
        self.panels_by_window.stop()

    # -------------------------------------------------------------- ingestion
    def consume(self, records):
        n = 0
        for raw in records:
            rec = self.norm.normalize(raw)
            if rec is None:
                continue
            if rec.get("event") in LiveReducer.EVENTS:
                try:
                    self.live.feed(rec)
                except Exception:
                    pass          # one bad record must never stop the stream
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
        with self.lock:
            n = self.consume(self.source.poll())
            self.source.prune_state()
            self.source.save_state()
            self.stats["last_poll"] = time.time()
        return n

    def backfill_now(self):
        """Fill RETENTION once at startup.  The one expensive read.

        This populates the live bins only.  The historical tier is not filled
        here: it is built per selected window by the registry, which seeks to
        the window's first byte instead of replaying everything the tailer
        happens to reach.  Records older than retention are discarded on arrival
        by the buckets, so over-reading costs time, never correctness.
        """
        hours = int(self.cfg.get("live.backfill_hours", 168))
        if hours <= 0:
            self.backfill = {"state": "skipped", "pct": 100, "records": 0, "hours": 0}
            self.source.seek_all_to_end()
            self.source.save_state()
            self.read_node_tier()
            return 0
        self.backfill = {"state": "running", "pct": 0, "records": 0, "hours": hours}
        total = sum(f["size"] for f in self.reports["ebpf"]["files"]) or 1
        seen = 0
        with self.lock:
            for rec in self.source.poll(cold_start_allowed=True):
                self.consume([rec])
                seen += 1
                if seen % 20000 == 0:
                    read = self.source.stats["bytes_read"]
                    self.backfill.update(pct=min(99, int(100 * read / total)),
                                         records=seen)
            self.source.save_state()
        retained = self.live.buckets.oldest_epoch()
        self.backfill.update(
            state="done", pct=100, records=seen,
            # What the read actually reached back to, which after a cold start is
            # bounded by live.backfill_mb per file rather than by the hours asked
            # for. The live window states this too, so a short fill reads as a
            # gap in the old bins instead of as a quiet fleet.
            retained_from=(time.strftime("%Y-%m-%d %H:%M:%S",
                                         time.localtime(retained))
                           if retained else None),
            retained_hours=(round((time.time() - retained) / 3600.0, 1)
                            if retained else None))
        # Materialise the node tier now rather than at the first scheduled tick:
        # it is a snapshot feed, not a window aggregate, and leaving it blank for
        # a whole rebuild interval would read as an absent feed.
        self.rebuild_history()
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
