"""Requirement 4: I/O, at process grain, for the Risk view.

`io.rd_mb`/`wr_mb` are block-layer bytes; `rchar_mb`/`wchar_mb` are bytes through
read()/write().  The gap between them is served without touching the device --
mostly page cache -- and that gap is the interesting agent-polling signal.  It is
reported as an UPPER BOUND, not an exact figure, because rchar also counts pipes,
sockets and procfs reads, which never had a device to miss.

`net_tx_bytes`/`net_rx_bytes`/`net_calls` are all-protocol per-process counters
(this is what makes QUIC visible) and are null -- not zero -- when the netbytes
BPF block was unavailable, so every total ships with its coverage.

Reads and writes are kept apart end to end: the per-tool and per-user rows are
cut to the top N on EACH counter and unioned, so a tool that only writes is not
dropped for reading little.
"""
import collections
import heapq

from . import Acc, Quant, add_counts, pct

IO_KEYS = ("rchar_mb", "rd_mb", "wchar_mb", "wr_mb")


def _heaviest(rows, n):
    """Union of the top `n` rows on each I/O counter, heaviest syscall readers first."""
    keep = set()
    for k in IO_KEYS:
        keep.update(heapq.nlargest(n, rows, key=lambda name: rows[name][k]))
    return sorted(((k, rows[k]) for k in keep), key=lambda kv: (-kv[1]["rchar_mb"], kv[0]))


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
            "quants": {k: Quant() for k in IO_KEYS},
        })
        self.by_tool = collections.defaultdict(lambda: {
            "events": 0, "with_io": 0, "rd_mb": 0.0, "wr_mb": 0.0, "rchar_mb": 0.0,
            "wchar_mb": 0.0, "net_tx_mb": 0.0, "net_rx_mb": 0.0, "net_calls": 0})
        self.by_user = collections.defaultdict(lambda: {
            "events": 0, "with_io": 0, "rchar_mb": 0.0, "wchar_mb": 0.0,
            "rd_mb": 0.0, "wr_mb": 0.0, "net_tx_mb": 0.0, "net_rx_mb": 0.0, "cls": None})

    def feed(self, rec):
        c = self.cls[rec["_a3"]]
        c["events"] += 1
        io = rec.get("io")
        tool = rec.get("_eff") or rec.get("_command") or "(empty)"
        t = self.by_tool[tool]
        t["events"] += 1
        u = self.by_user[rec.get("user") or "?"]
        u["events"] += 1
        u["cls"] = rec["_a3"]

        if isinstance(io, dict):
            c["io_seen"] += 1
            t["with_io"] += 1
            u["with_io"] += 1
            c["scope"][io.get("scope") or "unknown"] += 1
            for k in IO_KEYS:
                v = io.get(k)
                if v is not None:
                    c[k] += v
                    t[k] += v
                    u[k] += v
                c["quants"][k].add(v)
        else:
            for k in IO_KEYS:
                c["quants"][k].add(None)

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
            u["net_rx_mb"] += rx / 1e6
        if nc is not None:
            t["net_calls"] += nc

    def merge(self, o):
        """Fold a LATER state into this one. `o` is consumed."""
        for cls, c in o.cls.items():
            m = self.cls[cls]
            for k in ("events", "io_seen", "net_seen", "rd_mb", "wr_mb", "rchar_mb",
                      "wchar_mb"):
                m[k] += c[k]
            for k in ("net_tx_mb", "net_rx_mb", "net_calls"):
                m[k].merge(c[k])
            add_counts(m["scope"], c["scope"])
            for k, q in c["quants"].items():
                m["quants"][k].merge(q)
        for name, t in o.by_tool.items():
            m = self.by_tool[name]
            for k, v in t.items():
                m[k] += v
        for name, u in o.by_user.items():
            m = self.by_user[name]
            for k, v in u.items():
                if k == "cls":
                    m[k] = v            # the newest record's, as one pass keeps it
                else:
                    m[k] += v

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
                "quants": {k: q.as_dict() for k, q in c["quants"].items()},
            }

        def _row(key, name, v):
            row = {key: name, **{k: (round(x, 3) if isinstance(x, float) else x)
                                 for k, x in v.items()}}
            row["io_coverage_pct"] = pct(v["with_io"], v["events"])
            return row

        def _tool_row(name, v):
            row = _row("tool", name, v)
            # null, not 0%, for a tool that read nothing: there was no read to hit
            row["cache_hit_pct"] = (pct(max(0.0, v["rchar_mb"] - v["rd_mb"]), v["rchar_mb"])
                                    if v["rchar_mb"] > 0 else None)
            return row
        return {
            "by_class": by_class,
            "by_tool": [_tool_row(k, v) for k, v in _heaviest(self.by_tool, self.top_n)],
            "by_user": [_row("user", k, v) for k, v in _heaviest(self.by_user, self.top_n)],
            "notes": {
                "cache_served_mb": "max(0, rchar - rd). rchar counts ALL read() bytes "
                                   "including pipes, sockets and procfs, so this is an "
                                   "UPPER BOUND on the page-cache-served share.",
                "scope": "'leader' = thread-group-leader accounting (the `signal` BPF "
                         "block was dropped); 'unknown' = a record with no io.scope key.",
                "net": "net_tx/rx/calls cover ALL protocols (QUIC included) and are null, "
                       "not zero, when the netbytes block was dropped -- read coverage_pct.",
                "ranking": "by_tool / by_user hold the top N on EACH of rchar, rd, wchar "
                           "and wr, unioned -- so up to 4N rows, and a write-only tool or "
                           "user is never cut for reading little.",
            },
        }
