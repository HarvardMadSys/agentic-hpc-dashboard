"""Requirement 4: I/O, at process grain, for the Risk view.

`io.rd_mb`/`wr_mb` are block-layer bytes; `rchar_mb`/`wchar_mb` are bytes through
read()/write().  The gap between them is served without touching the device --
mostly page cache -- and that gap is the interesting agent-polling signal.  It is
reported as an UPPER BOUND, not an exact figure, because rchar also counts pipes,
sockets and procfs reads, which never had a device to miss.

`net_tx_bytes`/`net_rx_bytes`/`net_calls` are all-protocol per-process counters
(this is what makes QUIC visible) and are null -- not zero -- when the netbytes
BPF block was unavailable, so every total ships with its coverage.
"""
import collections

from . import Acc, Quant, pct


class IoReducer:
    EVENTS = ("exit", "truncated")
    KEY = "io_process"

    def __init__(self, cfg, classes):
        self.cfg = cfg
        self.classes = list(classes)
        self.top_n = (cfg.get("reducers.io", {}) or {}).get("top_n", 20)
        self.cls = collections.defaultdict(lambda: {
            "events": 0, "io_seen": 0, "net_seen": 0,
            "rd_mb": 0.0, "wr_mb": 0.0, "rchar_mb": 0.0, "wchar_mb": 0.0,
            "net_tx_mb": Acc(), "net_rx_mb": Acc(), "net_calls": Acc(),
            "scope": collections.Counter(),
            "q_rchar": Quant(), "q_wchar": Quant(), "q_rd": Quant(),
        })
        self.by_tool = collections.defaultdict(lambda: {
            "n": 0, "rd_mb": 0.0, "wr_mb": 0.0, "rchar_mb": 0.0, "wchar_mb": 0.0,
            "net_tx_mb": 0.0, "net_rx_mb": 0.0, "net_calls": 0})
        self.by_user = collections.defaultdict(lambda: {
            "events": 0, "with_io": 0, "rchar_mb": 0.0, "wchar_mb": 0.0,
            "rd_mb": 0.0, "wr_mb": 0.0, "net_tx_mb": 0.0, "cls": None})

    def feed(self, rec):
        c = self.cls[rec["_a3"]]
        c["events"] += 1
        io = rec.get("io")
        tool = rec.get("_eff") or rec.get("comm") or "(empty)"
        t = self.by_tool[tool]
        t["n"] += 1
        u = self.by_user[rec.get("user") or "?"]
        u["events"] += 1
        u["cls"] = rec["_a3"]

        if isinstance(io, dict):
            c["io_seen"] += 1
            u["with_io"] += 1
            c["scope"][io.get("scope") or "unknown"] += 1
            for k in ("rd_mb", "wr_mb", "rchar_mb", "wchar_mb"):
                v = io.get(k)
                if v is not None:
                    c[k] += v
                    t[k] += v
                    u[k] += v
            c["q_rchar"].add(io.get("rchar_mb"))
            c["q_wchar"].add(io.get("wchar_mb"))
            c["q_rd"].add(io.get("rd_mb"))
        else:
            c["q_rchar"].add(None)
            c["q_wchar"].add(None)
            c["q_rd"].add(None)

        tx, rx, nc = (rec.get("net_tx_bytes"), rec.get("net_rx_bytes"),
                      rec.get("net_calls"))
        c["net_tx_mb"].add(tx / 1e6 if tx is not None else None)
        c["net_rx_mb"].add(rx / 1e6 if rx is not None else None)
        c["net_calls"].add(nc)
        if tx is not None:
            t["net_tx_mb"] += tx / 1e6
            u["net_tx_mb"] += tx / 1e6
        if rx is not None:
            t["net_rx_mb"] += rx / 1e6
        if nc is not None:
            t["net_calls"] += nc

    def result(self):
        by_class = {}
        for cls, c in self.cls.items():
            cache = max(0.0, c["rchar_mb"] - c["rd_mb"])
            by_class[cls] = {
                "events": c["events"],
                "events_with_io": c["io_seen"],
                "io_coverage_pct": pct(c["io_seen"], c["events"]),
                "rd_mb": round(c["rd_mb"], 2), "wr_mb": round(c["wr_mb"], 2),
                "rchar_mb": round(c["rchar_mb"], 2), "wchar_mb": round(c["wchar_mb"], 2),
                "cache_served_mb": round(cache, 2),
                "cache_hit_pct": pct(cache, c["rchar_mb"]),
                "net_tx_mb": c["net_tx_mb"].as_dict(),
                "net_rx_mb": c["net_rx_mb"].as_dict(),
                "net_calls": c["net_calls"].as_dict(),
                "scope": dict(c["scope"]),
                "quants": {"rchar_mb": c["q_rchar"].as_dict(),
                           "wchar_mb": c["q_wchar"].as_dict(),
                           "rd_mb": c["q_rd"].as_dict()},
            }
        top = sorted(self.by_tool.items(), key=lambda kv: -kv[1]["rchar_mb"])[:self.top_n]
        users = sorted(self.by_user.items(), key=lambda kv: -kv[1]["rchar_mb"])[:self.top_n]

        def _user_row(name, v):
            row = dict(user=name, **{k: (round(x, 3) if isinstance(x, float) else x)
                                     for k, x in v.items()})
            row["io_coverage_pct"] = pct(v["with_io"], v["events"])
            return row
        return {
            "by_class": by_class,
            "by_tool": [dict(tool=k, **{kk: (round(vv, 3) if isinstance(vv, float) else vv)
                                        for kk, vv in v.items()}) for k, v in top],
            "by_user": [_user_row(k, v) for k, v in users],
            "notes": {
                "cache_served_mb": "max(0, rchar - rd). rchar counts ALL read() bytes "
                                   "including pipes, sockets and procfs, so this is an "
                                   "UPPER BOUND on the page-cache-served share.",
                "scope": "'leader' = thread-group-leader accounting (the `signal` BPF "
                         "block was dropped); 'unknown' = a record with no io.scope key.",
                "net": "net_tx/rx/calls cover ALL protocols (QUIC included) and are null, "
                       "not zero, when the netbytes block was dropped -- read coverage_pct.",
            },
        }
