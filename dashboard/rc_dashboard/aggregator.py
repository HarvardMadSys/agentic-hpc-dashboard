"""One streaming pass, many reducers, plus the background tasks that feed them.

Dispatch is by event type, built from each reducer's `EVENTS`, so a reducer only
pays for the records it asked for -- a residency reducer must not be walked past
20M exit records.

Two aggregation tiers, because they have different composability:

* The LIVE tier updates continuously from the tail.  Its state is bucketed or
  incremental, so a 24-hour window costs tens of KB rather than the ~10-20 GB/day
  the raw records occupy.
* The HISTORICAL tier (quantiles, CCDFs, Gini) is not resumable from a byte
  offset without persisted sketch state, so it rebuilds on a schedule and
  publishes `full_built_at` -- the age of those panels is visible rather than
  implied.
"""
import json
import os
import threading
import time

from . import feeds as feedmod
from .normalize import Normalizer
from .reducers.io import IoReducer
from .reducers.live import LiveReducer
from .reducers.resources import NodeReducer, ResourceReducer
from .reducers.risk import RiskReducer
from .reducers.sandbox import SandboxReducer
from .reducers.tools import ToolMixReducer
from .reducers.trajectories import TrajectoryReducer
from .tail import FileTailSource

LIVE_REDUCERS = (LiveReducer, TrajectoryReducer)
HIST_REDUCERS = (ToolMixReducer, IoReducer, ResourceReducer, SandboxReducer,
                 RiskReducer)


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
            days=int(cfg.get("feeds.ebpf.days", 2)), state_path=state)
        self.live = {r.KEY: r(cfg, self.classes) for r in LIVE_REDUCERS}
        self.hist = {r.KEY: r(cfg, self.classes) for r in HIST_REDUCERS}
        self.node = NodeReducer(cfg, self.classes)
        self.full_built_at = None
        self.hist_result = {}
        self.backfill = {"state": "idle", "pct": 0, "records": 0, "hours": 0}
        self._dispatch = self._build_dispatch()
        self.stats = {"records": 0, "last_poll": None}

    def _build_dispatch(self):
        d = {}
        for pool in (self.live, self.hist):
            for r in pool.values():
                for ev in getattr(r, "EVENTS", ()):
                    d.setdefault(ev, []).append(r)
        return d

    # -------------------------------------------------------------- ingestion
    def consume(self, records, live_only=False):
        n = 0
        for raw in records:
            rec = self.norm.normalize(raw)
            if rec is None:
                continue
            ev = rec.get("event")
            for r in self._dispatch.get(ev, ()):
                if live_only and r.KEY not in self.live:
                    continue
                try:
                    r.feed(rec)
                except Exception:
                    pass          # one bad record must never stop the stream
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
        """Populate the window once at startup.  The one expensive read."""
        hours = int(self.cfg.get("live.backfill_hours", 24))
        if hours <= 0:
            self.backfill = {"state": "skipped", "pct": 100, "records": 0, "hours": 0}
            self.source.seek_all_to_end()
            self.source.save_state()
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
        self.backfill.update(state="done", pct=100, records=seen)
        self.read_node_tier()
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
        self.read_node_tier()
        with self.lock:
            out = {}
            for key, r in self.hist.items():
                try:
                    out[key] = r.result()
                except Exception as e:
                    out[key] = {"_error": str(e)}
            try:
                out["node"] = self.node.result()
            except Exception as e:
                out["node"] = {"_error": str(e)}
            self.hist_result = out
            self.full_built_at = time.strftime("%Y-%m-%d %H:%M:%S")
            return out

    def panels(self):
        """Historical panels, each stamped with its feed's presence."""
        with self.lock:
            ebpf = feedmod.panel_meta(self.reports["ebpf"])
            node = feedmod.panel_meta(self.reports["ebpf_node"])
            out = {}
            for key, payload in (self.hist_result or {}).items():
                meta = node if key == "node" else ebpf
                body = dict(payload) if isinstance(payload, dict) else {"rows": payload}
                body.update(meta)
                out[key] = body
            for key in ("tool_mix", "io_process", "resources", "sandbox", "risk"):
                out.setdefault(key, dict(ebpf))
            out.setdefault("node", dict(node))
            return {"panels": out, "full_built_at": self.full_built_at,
                    "purposes": self.norm.purposes,
                    "purpose_src": self.norm.purpose_src,
                    "classes": self.classes}

    def live_payload(self):
        with self.lock:
            out = self.live["live"].result()
            out.update(feedmod.panel_meta(self.reports["ebpf"]))
            out["backfill"] = dict(self.backfill)
            return out

    def trajectories(self):
        with self.lock:
            out = self.live["trajectories"].result()
            out.update(feedmod.panel_meta(self.reports["ebpf"]))
            return out
