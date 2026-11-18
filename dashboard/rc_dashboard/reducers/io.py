"""Requirement 4: I/O, at process grain, for the Risk view.

`io.rd_mb`/`wr_mb` are block-layer bytes; `rchar_mb`/`wchar_mb` are bytes through
read()/write().  The gap between them is served without touching the device --
mostly page cache -- and that gap is the interesting agent-polling signal.  It is
reported as an UPPER BOUND, not an exact figure, because rchar also counts pipes,
sockets and procfs reads, which never had a device to miss.

All four are each process's OWN: the collector's figure also holds every child
the process reaped, which is taken back off at its exit (own_io.py), so a sum
over a class, a tool or a user counts a command's bytes once, not once per shell
above it. The join crosses hours, so it is carried (`carry`).

`net_tx_bytes`/`net_rx_bytes`/`net_calls` are all-protocol per-process counters
(this is what makes QUIC visible) and are null -- not zero -- when the netbytes
BPF block was unavailable, so every total ships with its coverage.

Reads and writes are kept apart end to end: the per-tool and per-user rows are
cut to the top N on EACH counter and unioned, so a tool that only writes is not
dropped for reading little.

`net_endpoints` (schema 7) says where those bytes went: every endpoint of the
process's life, with its bytes each way and its send and receive calls. They are
added up per remote address and class, as each process exits, like the totals.
Inbound stays apart from outbound: the collector keys an inbound endpoint by the
local port, so its address is the client's. Every address is kept, not a top N.
The page names addresses when the panel is read (endpoints.py) and ranks the
names, and one name can gather many small addresses, so a cut here would drop
part of a name's traffic. What the list cannot show is counted beside it: the
bytes a block holds with no address listed (`unattributed`, `overflow`,
`unaccounted`), and the processes that moved bytes with no block at all.
"""
import collections
import heapq

from . import Acc, Quant, add_counts, pct
from .own_io import REAPED_MAX, OwnIo

IO_KEYS = ("rchar_mb", "rd_mb", "wchar_mb", "wr_mb")
# The losses a `net_endpoints` block states, beside its endpoints.
EP_LOSSES = ("unattributed", "overflow", "unaccounted")
# Per address, the ports and the command-line names kept in the result: labels
# for the page, which shows a few.
EP_PORTS = 5
EP_HOSTS = 3


def _count(v):
    """A collector byte or call count as an int; absent or junk is none."""
    return int(v) if isinstance(v, (int, float)) and v > 0 else 0


def _endpoint():
    return {"by_class": {}, "ports": collections.Counter(), "hosts": collections.Counter(),
            "procs": 0, "external": False, "provider": None}


def _ep_cover():
    # listed: bytes and calls with an address; lost: what a block holds with
    # none listed; missing: processes that moved bytes with no block at all
    return {"procs": 0, "calls": 0, "tx": 0, "rx": 0,
            "lost_calls": 0, "lost_tx": 0, "lost_rx": 0, "missing": 0, "missing_bytes": 0}


def _top(counts, n):
    """The `n` biggest counts, ties by name: the same whatever order they came in."""
    return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:n])


def _heaviest(rows, n):
    """Union of the top `n` rows on each I/O counter, heaviest syscall readers first."""
    keep = set()
    for k in IO_KEYS:
        keep.update(heapq.nlargest(n, rows, key=lambda name: rows[name][k]))
    return sorted(((k, rows[k]) for k in keep), key=lambda kv: (-kv[1]["rchar_mb"], kv[0]))


class IoReducer:
    # Exits only: a `truncated` record is a process still running at a collector
    # stop, with its whole life's counters so far, and its exit after the
    # restart carries them all again.
    EVENTS = ("exit",)
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
        # (remote address, inbound) -> its calls and bytes per class
        self.endpoints = collections.defaultdict(_endpoint)
        self.ep_cover = collections.defaultdict(_ep_cover)
        # each process's own bytes, its reaped children's taken off (own_io.py)
        self.own = OwnIo()

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
            own = dict(zip(self.own.fields, self.own.take(rec)))
            for k in IO_KEYS:
                v = own[k]
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

        ne = rec.get("net_endpoints")
        if isinstance(ne, dict):
            self._feed_endpoints(rec["_a3"], ne)
        elif (tx or 0) + (rx or 0) > 0:
            cv = self.ep_cover[rec["_a3"]]
            cv["missing"] += 1
            cv["missing_bytes"] += (tx or 0) + (rx or 0)

    def _feed_endpoints(self, cls, ne):
        cv = self.ep_cover[cls]
        cv["procs"] += 1
        seen = set()
        for e in ne.get("endpoints") or ():
            if not isinstance(e, dict) or not e.get("addr"):
                continue
            key = (str(e["addr"]), bool(e.get("inbound")))
            row = self.endpoints[key]
            calls, tx, rx = _count(e.get("calls")), _count(e.get("tx_bytes")), \
                _count(e.get("rx_bytes"))
            by = row["by_class"].get(cls)
            if by is None:
                by = row["by_class"][cls] = {"calls": 0, "tx": 0, "rx": 0}
            by["calls"] += calls
            by["tx"] += tx
            by["rx"] += rx
            row["ports"]["%s/%s" % (e.get("proto") or "?", e.get("port"))] += calls
            if key not in seen:
                seen.add(key)
                row["procs"] += 1
                if e.get("host"):
                    row["hosts"][str(e["host"])] += 1
            if e.get("external"):
                row["external"] = True
            if row["provider"] is None and e.get("provider"):
                row["provider"] = str(e["provider"])
            cv["calls"] += calls
            cv["tx"] += tx
            cv["rx"] += rx
        for k in EP_LOSSES:
            lost = ne.get(k)
            if isinstance(lost, dict):
                cv["lost_calls"] += _count(lost.get("calls"))
                cv["lost_tx"] += _count(lost.get("tx_bytes"))
                cv["lost_rx"] += _count(lost.get("rx_bytes"))

    # ------------------------------------------------------------- merge
    # A window is merged from saved hour states (`hours`). The own-I/O join
    # crosses an hour -- a shell that exits in the next hour gives back what the
    # children it reaped in this one carried -- so it is CARRIED into each
    # hour's reduction, and is not part of the saved hour.
    CARRY_AGED = ("reaped",)

    def track(self):
        self.own.track()

    def carry(self):
        return self.own.carry({})

    def carry_caps(self):
        """What one pass holds the carried map to (`hours.combine_carries`)."""
        return {"reaped": REAPED_MAX}

    def seed(self, c):
        self.own.seed(c)

    def strip(self):
        self.own.strip()

    def merge(self, o):
        """Fold a LATER state into this one. `o` is consumed."""
        self.own.merge(o.own)
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
        for key, e in o.endpoints.items():
            m = self.endpoints[key]
            for cls, by in e["by_class"].items():
                add_counts(m["by_class"].setdefault(cls, {"calls": 0, "tx": 0, "rx": 0}), by)
            add_counts(m["ports"], e["ports"])
            add_counts(m["hosts"], e["hosts"])
            m["procs"] += e["procs"]
            m["external"] = m["external"] or e["external"]
            if m["provider"] is None:
                m["provider"] = e["provider"]   # the earlier one's, as one pass keeps it
        for cls, cv in o.ep_cover.items():
            add_counts(self.ep_cover[cls], cv)

    def _endpoint_rows(self):
        """Every address, the most calls first, then the most bytes."""
        order = {c: i for i, c in enumerate(self.classes)}

        def total(e, k):
            return sum(by[k] for by in e["by_class"].values())
        rows = []
        for (addr, inbound), e in self.endpoints.items():
            rows.append({
                "addr": addr, "inbound": inbound, "external": e["external"],
                "provider": e["provider"], "procs": e["procs"],
                "hosts": _top(e["hosts"], EP_HOSTS), "ports": _top(e["ports"], EP_PORTS),
                "by_class": {c: dict(e["by_class"][c]) for c in
                             sorted(e["by_class"], key=lambda c: (order.get(c, len(order)), c))},
                "_calls": total(e, "calls"), "_bytes": total(e, "tx") + total(e, "rx"),
            })
        rows.sort(key=lambda r: (-r["_calls"], -r["_bytes"], r["addr"], r["inbound"]))
        for r in rows:
            del r["_calls"], r["_bytes"]
        return rows

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
            # Replaced by `endpoints`, the named and ranked list, when the panel
            # is read (Aggregator.panels): it never leaves the service as it is.
            "endpoint_addrs": {"rows": self._endpoint_rows(),
                               "cover": {c: dict(v) for c, v in self.ep_cover.items()}},
            # parents dropped at the join's cap (their children's bytes then count twice)
            "own_io": self.own.status(),
            "notes": {
                "own": "every byte figure is each process's OWN: the collector's io is "
                       "task->signal->ioac, which also holds every child the process "
                       "reaped, and those are taken back off at its exit. A child whose "
                       "exit the window did not see is not taken off.",
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
