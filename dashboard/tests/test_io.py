"""I/O reducer tests: reads and writes are reported, and ranked, separately.

    cd dashboard && .venv/bin/python -m unittest discover -s tests -q
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rc_dashboard.reducers.io import IoReducer     # noqa: E402


class Cfg:
    def __init__(self, tree):
        self.tree = tree

    def get(self, dotted, default=None):
        return self.tree.get(dotted, default)


def exit_rec(tool, user, rchar=0.0, rd=0.0, wchar=0.0, wr=0.0, tx=None, rx=None):
    return {"event": "exit", "_a3": "agent", "_eff": tool, "user": user,
            "io": {"rchar_mb": rchar, "rd_mb": rd, "wchar_mb": wchar, "wr_mb": wr,
                   "scope": "proc"},
            "net_tx_bytes": tx, "net_rx_bytes": rx, "net_calls": None}


# a, b, c read; w only writes. With top_n=2, c is third on every read counter and
# last on every write counter, so it is the one row that should be cut.
ROWS = [
    ("a", 100.0, 50.0, 1.0, 0.2),
    ("b", 90.0, 40.0, 2.0, 0.1),
    ("c", 80.0, 30.0, 0.0, 0.0),
    ("w", 0.0, 0.0, 1000.0, 500.0),
]


def reduce(rows=ROWS, top_n=2, **net):
    r = IoReducer(Cfg({"reducers.io": {"top_n": top_n}}), ["agent"])
    for name, rchar, rd, wchar, wr in rows:
        r.feed(exit_rec(name, name, rchar, rd, wchar, wr, **net))
    return r.result()


class RankingTest(unittest.TestCase):

    def test_a_write_heavy_tool_survives_the_top_n_cut(self):
        tools = {t["tool"] for t in reduce()["by_tool"]}
        self.assertEqual(tools, {"a", "b", "w"})

    def test_a_write_heavy_user_survives_the_top_n_cut(self):
        users = {u["user"] for u in reduce()["by_user"]}
        self.assertEqual(users, {"a", "b", "w"})


class RowShapeTest(unittest.TestCase):

    def test_tool_rows_carry_read_and_write_on_both_layers(self):
        w = next(t for t in reduce()["by_tool"] if t["tool"] == "w")
        self.assertEqual((w["rd_mb"], w["rchar_mb"], w["wr_mb"], w["wchar_mb"]),
                         (0.0, 0.0, 500.0, 1000.0))

    def test_tool_rows_carry_their_event_count_and_coverage(self):
        r = IoReducer(Cfg({}), ["agent"])
        r.feed(exit_rec("git", "u", rchar=4.0, rd=1.0))
        rec = exit_rec("git", "u")
        rec["io"] = None
        r.feed(rec)
        git = r.result()["by_tool"][0]
        self.assertEqual((git["events"], git["io_coverage_pct"], git["cache_hit_pct"]),
                         (2, 50.0, 75.0))

    def test_user_rows_carry_both_layers_and_both_network_directions(self):
        a = next(u for u in reduce(tx=2e6, rx=3e6)["by_user"] if u["user"] == "a")
        self.assertEqual((a["rd_mb"], a["rchar_mb"], a["wr_mb"], a["wchar_mb"]),
                         (50.0, 100.0, 0.2, 1.0))
        self.assertEqual((a["net_tx_mb"], a["net_rx_mb"]), (2.0, 3.0))


class DistributionTest(unittest.TestCase):

    def test_the_distributions_cover_disk_writes_too(self):
        q = reduce()["by_class"]["agent"]["quants"]
        self.assertEqual(list(q), ["rchar_mb", "rd_mb", "wchar_mb", "wr_mb"])
        self.assertEqual(q["wr_mb"]["max"], 500.0)


# Documentation addresses (RFC 5737) and names: nothing here is a real endpoint.
A, B, C = "192.0.2.10", "198.51.100.7", "203.0.113.5"


def ep(addr, port=443, calls=10, tx=1000, rx=4000, proto="tcp", inbound=False,
       host=None, external=True, provider=None):
    """One entry of a `net_endpoints` block, as the schema-7 collector writes it."""
    return {"proto": proto, "addr": addr, "port": port, "host": host, "inbound": inbound,
            "external": external, "provider": provider, "tx_bytes": tx, "rx_bytes": rx,
            "calls": calls}


def net_rec(cls, *eps, user="u", **block):
    """An exit whose endpoints are `eps`, its byte totals theirs plus any `block`
    loss, so the panel's totals and the list agree as the collector's do."""
    lost = [v for v in block.values() if isinstance(v, dict)]
    tx = sum(e["tx_bytes"] for e in eps) + sum(v.get("tx_bytes", 0) for v in lost)
    rx = sum(e["rx_bytes"] for e in eps) + sum(v.get("rx_bytes", 0) for v in lost)
    return {"event": "exit", "_a3": cls, "_eff": "curl", "user": user, "io": None,
            "net_tx_bytes": tx, "net_rx_bytes": rx,
            "net_calls": sum(e["calls"] for e in eps),
            "net_endpoints": dict({"n": len(eps), "in_series": False,
                                   "endpoints": list(eps), "unattributed": None}, **block)}


def endpoints(*recs):
    r = IoReducer(Cfg({}), ["agent", "human", "ide"])
    for rec in recs:
        r.feed(rec)
    return r.result()["endpoint_addrs"]


class EndpointTest(unittest.TestCase):
    """Every remote address a process exchanged bytes with, per class."""

    def test_an_address_adds_up_across_processes_and_ports_per_class(self):
        out = endpoints(
            net_rec("agent", ep(A, 443, calls=10, tx=100, rx=400),
                    ep(A, 80, calls=2, tx=10, rx=20)),
            net_rec("agent", ep(A, 443, calls=5, tx=50, rx=200)),
            net_rec("human", ep(A, 443, calls=1, tx=1, rx=2),
                    ep(B, 53, proto="udp", calls=4, tx=80, rx=240, external=False)))
        a, b = out["rows"]
        self.assertEqual(a["addr"], A)
        self.assertEqual(a["by_class"], {"agent": {"calls": 17, "tx": 160, "rx": 620},
                                         "human": {"calls": 1, "tx": 1, "rx": 2}})
        # one process listing the address on two ports is still one process
        self.assertEqual(a["procs"], 3)
        self.assertEqual(a["ports"], {"tcp/443": 16, "tcp/80": 2})
        self.assertEqual((a["inbound"], a["external"]), (False, True))
        self.assertEqual((b["addr"], b["external"], b["ports"]), (B, False, {"udp/53": 4}))

    def test_rows_come_busiest_first_whatever_order_they_arrived_in(self):
        recs = [net_rec("agent", ep(B, calls=3)), net_rec("agent", ep(A, calls=9)),
                net_rec("human", ep(C, calls=3, tx=9999))]
        one = endpoints(*recs)
        other = endpoints(*reversed(recs))
        self.assertEqual([r["addr"] for r in one["rows"]], [A, C, B])
        self.assertEqual(one, other)

    def test_inbound_stays_apart_from_outbound(self):
        out = endpoints(net_rec("ide", ep(C, 22, calls=3, inbound=True), ep(C, 443, calls=7)))
        self.assertEqual([(r["addr"], r["inbound"], r["ports"]) for r in out["rows"]],
                         [(C, False, {"tcp/443": 7}), (C, True, {"tcp/22": 3})])

    def test_a_name_the_command_line_gave_rides_with_its_address(self):
        out = endpoints(net_rec("agent", ep(A, host="example.org", provider="github")),
                        net_rec("agent", ep(A, host="example.org")),
                        net_rec("agent", ep(A)))
        row, = out["rows"]
        self.assertEqual((row["hosts"], row["provider"]), ({"example.org": 2}, "github"))

    def test_what_the_list_misses_is_counted_beside_it(self):
        no_block = {"event": "exit", "_a3": "agent", "_eff": "curl", "user": "u",
                    "net_tx_bytes": 300, "net_rx_bytes": 700, "net_calls": 2}
        off = dict(no_block, net_endpoints=None)            # the netpeer blocks did not load
        quiet = dict(no_block, net_tx_bytes=0, net_rx_bytes=0, net_endpoints=None)
        lossy = net_rec("agent", ep(A, calls=4, tx=40, rx=60),
                        unattributed={"tx_bytes": 5, "rx_bytes": 6, "calls": 1},
                        overflow={"n": 2, "tx_bytes": 7, "rx_bytes": 8, "calls": 3},
                        unaccounted={"tx_bytes": 9, "rx_bytes": 10})
        cover = endpoints(no_block, off, quiet, lossy)["cover"]["agent"]
        self.assertEqual(cover, {"procs": 1, "calls": 4, "tx": 40, "rx": 60,
                                 "lost_calls": 4, "lost_tx": 21, "lost_rx": 24,
                                 "missing": 2, "missing_bytes": 2000})

    def test_two_halves_merged_are_the_whole(self):
        recs = [net_rec("agent", ep(A, calls=4, host="example.org")),
                net_rec("human", ep(A, 80, calls=1), ep(B, calls=2, provider="aws")),
                net_rec("ide", ep(C, 22, inbound=True)),
                net_rec("agent", ep(B, calls=7, tx=1, rx=1),
                        unattributed={"tx_bytes": 1, "rx_bytes": 2, "calls": 1})]
        whole = endpoints(*recs)
        first, second = (IoReducer(Cfg({}), ["agent", "human", "ide"]) for _ in range(2))
        for rec in recs[:2]:
            first.feed(rec)
        for rec in recs[2:]:
            second.feed(rec)
        first.merge(second)
        self.assertEqual(first.result()["endpoint_addrs"], whole)


if __name__ == "__main__":
    unittest.main()
