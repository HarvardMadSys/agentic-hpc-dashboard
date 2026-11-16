"""The Network bytes panel's endpoint list: addresses named, grouped, and ranked.

The I/O reducer adds the bytes and calls up per remote address (test_io). When
the panel is read, endpoints.py names each address -- reverse DNS from a
background lookup, else a name the process's command line gave, else the
address itself -- groups the addresses by name, and keeps the top N by calls and
by bytes for every mix of classes, so the page can rank the classes it shows.

Addresses and names are documentation ones (RFC 5737, example.com and .org).

    cd dashboard && .venv/bin/python -m unittest discover -s tests -q
"""
import itertools
import json
import os
import random
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from rc_dashboard import endpoints as epmod          # noqa: E402
from rc_dashboard.aggregator import Aggregator       # noqa: E402
from rc_dashboard.reducers.io import IoReducer       # noqa: E402
from rc_dashboard.windows import ViewFilter          # noqa: E402
from test_io import A, B, C, Cfg, ep, net_rec        # noqa: E402
from test_window_cache import WIN, CacheCase, varied_exit   # noqa: E402

CLASSES = ["agent", "human", "ide", "unlabeled"]
D = "192.0.2.20"


def addrs(*recs):
    r = IoReducer(Cfg({}), CLASSES)
    for rec in recs:
        r.feed(rec)
    return r.result()["endpoint_addrs"]


def fold(block, names=None, pending=(), top=10):
    return epmod.fold(block, names or {}, set(pending), CLASSES, top=top)


def total(row, classes, k):
    return sum(row["by_class"].get(c, {}).get(k, 0) for c in classes)


def ranked(rows, classes, by, top):
    """The page's ranking: the most of `by` over `classes` first, then the
    other measure, then the name, outbound before inbound; none of it, no rank."""
    def val(r):
        calls = total(r, classes, "calls")
        size = total(r, classes, "tx") + total(r, classes, "rx")
        return (calls, size) if by == "calls" else (size, calls)
    keep = [r for r in rows if val(r)[0] > 0]
    keep.sort(key=lambda r: (-val(r)[0], -val(r)[1], r["name"], r["inbound"]))
    return [(r["name"], r["inbound"]) for r in keep[:top]]


class FoldTest(unittest.TestCase):

    def test_the_addresses_one_name_answers_for_are_one_row(self):
        block = addrs(net_rec("agent", ep(A, calls=5, tx=10, rx=20)),
                      net_rec("human", ep(B, 80, calls=3, tx=1, rx=2)),
                      net_rec("agent", ep(C, calls=1)))
        out = fold(block, {A: "s3.example.com", B: "s3.example.com", C: None})
        s3, c = out["rows"]
        self.assertEqual((s3["name"], s3["source"], s3["addrs"], s3["n_addrs"]),
                         ("s3.example.com", "dns", [A, B], 2))
        self.assertEqual(s3["by_class"], {"agent": {"calls": 5, "tx": 10, "rx": 20},
                                          "human": {"calls": 3, "tx": 1, "rx": 2}})
        self.assertEqual((s3["ports"], s3["procs"]), (["tcp/443", "tcp/80"], 2))
        self.assertEqual((c["name"], c["source"], c["addrs"]), (C, "address", [C]))
        self.assertEqual((out["names"], out["addresses"], out["pending"]), (2, 3, 0))

    def test_without_a_reverse_name_the_command_line_name_then_the_address(self):
        block = addrs(net_rec("agent", ep(A, calls=3, host="example.org")),
                      net_rec("agent", ep(B, calls=2)),
                      net_rec("agent", ep(C, calls=1, host="example.org")))
        out = fold(block, {C: "edge.example.com"})
        self.assertEqual([(r["name"], r["source"]) for r in out["rows"]],
                         [("example.org", "argv"), (B, "address"),
                          ("edge.example.com", "dns")])

    def test_an_address_still_being_looked_up_is_listed_by_what_is_known(self):
        block = addrs(net_rec("agent", ep(A, calls=3)), net_rec("agent", ep(B, calls=2)))
        out = fold(block, {B: "b.example.com"}, pending=[A])
        self.assertEqual([(r["name"], r["pending"]) for r in out["rows"]],
                         [(A, True), ("b.example.com", False)])
        self.assertEqual(out["pending"], 1)

    def test_inbound_and_outbound_under_one_name_stay_apart(self):
        block = addrs(net_rec("ide", ep(A, 22, calls=4, inbound=True), ep(A, calls=9)))
        out = fold(block, {A: "host.example.com"})
        self.assertEqual([(r["name"], r["inbound"], r["ports"]) for r in out["rows"]],
                         [("host.example.com", False, ["tcp/443"]),
                          ("host.example.com", True, ["tcp/22"])])

    def test_a_row_says_where_it_reaches_and_who_runs_it(self):
        block = addrs(net_rec("agent", ep(A, provider="aws"), ep(B, external=False)))
        out = fold(block, {A: "a.example.com", B: "nfs.example.com"})
        got = {r["name"]: (r["external"], r["provider"]) for r in out["rows"]}
        self.assertEqual(got, {"a.example.com": (True, "aws"),
                               "nfs.example.com": (False, None)})

    def test_the_list_holds_the_top_for_every_mix_of_classes(self):
        # The page re-ranks the rows over the classes it shows, so for every mix
        # the rows must hold that mix's whole top N -- here checked against a
        # ranking of every name, many small addresses folding into a few names.
        rng = random.Random(3)
        recs, names = [], {}
        for i in range(60):
            addr = "198.51.100.%d" % i
            names[addr] = rng.choice([None, None, "a.example.com", "b.example.com",
                                      "c.example.com"])
            for _ in range(rng.randint(1, 3)):
                recs.append(net_rec(rng.choice(CLASSES), ep(
                    addr, calls=rng.randint(1, 50), tx=rng.randint(0, 9000),
                    rx=rng.randint(0, 9000), inbound=rng.random() < .1)))
        block = addrs(*recs)
        every = fold(block, names, top=10 ** 6)["rows"]       # every name, uncut
        out = fold(block, names, top=5)
        self.assertLess(len(out["rows"]), len(every))
        for k in range(1, len(CLASSES) + 1):
            for mix in itertools.combinations(CLASSES, k):
                for by in ("calls", "bytes"):
                    with self.subTest(mix=mix, by=by):
                        self.assertEqual(ranked(out["rows"], mix, by, 5),
                                         ranked(every, mix, by, 5))

    def test_a_mix_that_never_reached_an_endpoint_does_not_rank_it(self):
        block = addrs(net_rec("agent", ep(A, calls=9)), net_rec("agent", ep(B, calls=8)),
                      net_rec("ide", ep(C, calls=1)))
        out = fold(block, top=1)
        # agent's top is A, ide's is C, and B is no mix's top
        self.assertEqual(sorted(r["name"] for r in out["rows"]), [A, C])

    def test_what_the_list_leaves_out_rides_along(self):
        block = addrs(net_rec("agent", ep(A), unattributed={"tx_bytes": 1, "rx_bytes": 2,
                                                             "calls": 1}))
        out = fold(block)
        self.assertEqual(out["cover"], block["cover"])
        self.assertEqual((out["top"], out["classes"]), (10, ["agent"]))


class NameTest(unittest.TestCase):

    def test_a_reverse_name_is_cleaned_or_refused(self):
        clean = epmod.clean_name
        self.assertEqual(clean("Host.Example.COM.", A), "host.example.com")
        # a PTR record written without its final dot: the zone is appended
        self.assertEqual(clean("nfs05.example.edu.20.2.0.192.in-addr.arpa", A),
                         "nfs05.example.edu")
        self.assertIsNone(clean("10.2.0.192.in-addr.arpa", A))
        self.assertEqual(clean("_localdnsstub", "127.0.0.53"), "_localdnsstub")
        for junk in ("", None, A, "two words.example.com", "x\n.example.com"):
            self.assertIsNone(clean(junk, A), junk)


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


class ResolverTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rc_rdns_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.asked = []

    def resolver(self, answers, **kw):
        def lookup(addr):
            self.asked.append(addr)
            got = answers.get(addr)
            if isinstance(got, BaseException):
                raise got
            if got is None:
                raise socket.herror(1, "Unknown host")
            return got
        kw.setdefault("path", os.path.join(self.tmp, "rdns.json"))
        r = epmod.Resolver(lookup=lookup, **kw)
        self.addCleanup(r.close)
        return r

    def settled(self, r, addrs):
        r.wait_idle(5)
        return r.names(addrs)

    def test_a_lookup_never_holds_up_the_request_that_wants_it(self):
        gate = threading.Event()

        def slow(addr):
            gate.wait(5)
            return "slow.example.com"
        r = epmod.Resolver(lookup=slow, path=None)
        self.addCleanup(r.close)
        names, pending = r.names([A])
        self.assertEqual((names, pending), ({}, {A}))
        gate.set()
        r.wait_idle(5)
        self.assertEqual(r.names([A]), ({A: "slow.example.com"}, set()))

    def test_no_name_is_remembered_and_a_failure_is_tried_again_later(self):
        clock = Clock()
        r = self.resolver({A: None, B: socket.herror(2, "Host name lookup failure")},
                          now=clock, retry_s=600)
        r.names([A, B])
        self.assertEqual(self.settled(r, [A, B]), ({A: None, B: None}, set()))
        clock.t += 601
        r.names([A, B])                 # B's failure is old now: asked again
        self.assertEqual(self.settled(r, [A, B]), ({A: None, B: None}, set()))
        self.assertEqual(sorted(self.asked), [A, B, B])

    def test_a_stale_name_is_served_while_it_is_looked_up_again(self):
        clock = Clock()
        answers = {A: "old.example.com"}
        r = self.resolver(answers, now=clock, ttl_s=100)
        r.names([A])
        r.wait_idle(5)
        answers[A] = "new.example.com"
        clock.t += 101
        self.assertEqual(r.names([A]), ({A: "old.example.com"}, set()))
        self.assertEqual(self.settled(r, [A]), ({A: "new.example.com"}, set()))

    def test_a_refresh_that_gets_no_answer_keeps_the_name(self):
        clock = Clock()
        answers = {A: "a.example.com"}
        r = self.resolver(answers, now=clock, ttl_s=100, retry_s=600)
        r.names([A])
        r.wait_idle(5)
        answers[A] = socket.timeout("timed out")
        clock.t += 101
        r.names([A])                    # stale: served, and asked again
        self.assertEqual(self.settled(r, [A]), ({A: "a.example.com"}, set()))
        asked = len(self.asked)
        r.names([A])                    # not again until the retry is due
        r.wait_idle(5)
        self.assertEqual(len(self.asked), asked)
        clock.t += 601
        r.names([A])
        r.wait_idle(5)
        self.assertEqual(len(self.asked), asked + 1)
        r.close()
        with open(os.path.join(self.tmp, "rdns.json")) as fh:
            self.assertEqual(json.load(fh)["names"][A][0], "a.example.com")

    def test_close_drops_the_queue_and_returns_without_waiting_on_a_lookup(self):
        started, gate = threading.Event(), threading.Event()
        asked = []

        def slow(addr):
            asked.append(addr)
            started.set()
            gate.wait(5)
            return "x.example.com"
        r = epmod.Resolver(lookup=slow, path=None, workers=1)
        r.names([A, B, C, D])           # the one worker takes A and waits
        started.wait(5)
        t = time.monotonic()
        r.close(wait_s=0.2)
        self.assertLess(time.monotonic() - t, 1.0)
        gate.set()
        r.threads[0].join(5)
        self.assertFalse(r.threads[0].is_alive())
        self.assertEqual(asked, [A])    # nothing queued was looked up after close
        self.assertEqual(r.names([B]), ({}, {B}))   # nor is anything asked later

    def test_names_outlive_a_restart(self):
        r = self.resolver({A: "a.example.com", B: None})
        r.names([A, B])
        r.wait_idle(5)
        r.close()
        with open(os.path.join(self.tmp, "rdns.json")) as fh:
            self.assertEqual(sorted(json.load(fh)["names"]), [A, B])
        again = self.resolver({})
        self.assertEqual(again.names([A, B]), ({A: "a.example.com", B: None}, set()))
        self.assertEqual(sorted(self.asked), [A, B])

    def test_a_mapped_address_is_looked_up_as_ipv4_and_junk_never(self):
        r = self.resolver({A: "a.example.com"})
        r.names(["::ffff:" + A, "not-an-address"])
        self.assertEqual(self.settled(r, ["::ffff:" + A, "not-an-address"]),
                         ({"::ffff:" + A: "a.example.com", "not-an-address": None}, set()))
        self.assertEqual(self.asked, [A])

    def test_addresses_are_looked_up_in_the_order_asked(self):
        gate = threading.Event()
        order = []

        def lookup(addr):
            gate.wait(5)
            order.append(addr)
            return "x.example.com"
        r = epmod.Resolver(lookup=lookup, path=None, workers=1)
        self.addCleanup(r.close)
        r.names([A])                    # the one worker takes A and waits
        r.names([B, C, D, B])
        gate.set()
        r.wait_idle(5)
        self.assertEqual(order, [A, B, C, D])

    def test_the_heaviest_addresses_are_asked_for_first(self):
        # by calls and by bytes in turn, so a big download with few calls is
        # named as soon as the endpoint with the most calls
        block = addrs(net_rec("agent", ep(A, calls=50, tx=1, rx=1)),
                      net_rec("agent", ep(B, calls=40, tx=1, rx=1)),
                      net_rec("agent", ep(C, calls=30, tx=1, rx=1)),
                      net_rec("agent", ep(D, calls=1, tx=10 ** 9, rx=0)))
        self.assertEqual(epmod.lookup_order(block["rows"]), [A, D, B, C])


class Names:
    """A resolver that already knows every name it is asked for."""

    def __init__(self, names):
        self.known = names

    def names(self, addrs):
        return {a: self.known.get(a) for a in addrs}, set()


class PayloadTest(CacheCase):

    def feed_net(self, start_min):
        recs = []
        for i, (user, cls, peer) in enumerate([("glen", "agent", A), ("ana", "human", B),
                                               ("glen", "human", C), ("ana", "agent", A)]):
            r = varied_exit(self.now - 60 * (start_min - i), i)
            r.update(user=user, actor3=cls, agent_type="claude_code" if cls == "agent" else None,
                     schema_version=7, net_tx_bytes=1000 * (i + 1), net_rx_bytes=4000,
                     net_endpoints={"n": 1, "in_series": False, "unattributed": None,
                                    "endpoints": [ep(peer, calls=10 * (i + 1),
                                                     tx=1000 * (i + 1), rx=4000)]})
            recs.append(r)
        self.append("h1.exits.jsonl", recs)

    def test_the_panel_is_served_named_and_ranked_through_the_who_filter(self):
        self.feed_net(30)
        agg = Aggregator(self.cfg())
        agg.rdns = Names({A: "api.example.com", B: "b.example.com"})
        reg = agg.panels_by_window
        reg._build(reg.request(WIN))
        io = agg.panels(WIN)["panels"]["io_process"]
        self.assertNotIn("endpoint_addrs", io)
        rows = {r["name"]: r for r in io["endpoints"]["rows"]}
        self.assertEqual(sorted(rows), [C, "api.example.com", "b.example.com"])
        self.assertEqual(rows["api.example.com"]["by_class"],
                         {"agent": {"calls": 50, "tx": 5000, "rx": 8000}})
        self.assertEqual(io["endpoints"]["rdns"], True)

        glen = ViewFilter(["glen"])
        agg.panels(WIN, glen)                       # queued
        reg._build(reg.peek(WIN, glen))
        io = agg.panels(WIN, glen)["panels"]["io_process"]
        self.assertEqual(sorted(r["name"] for r in io["endpoints"]["rows"]),
                         [C, "api.example.com"])

    def test_with_lookups_off_the_list_names_what_the_records_name(self):
        self.feed_net(30)
        cfg = self.cfg()
        cfg.tree["server"]["rdns"]["enabled"] = False
        agg = Aggregator(cfg)
        self.assertIsNone(agg.rdns)
        reg = agg.panels_by_window
        reg._build(reg.request(WIN))
        out = agg.panels(WIN)["panels"]["io_process"]["endpoints"]
        self.assertEqual((sorted(r["name"] for r in out["rows"]), out["rdns"], out["pending"]),
                         (sorted([A, B, C]), False, 0))


if __name__ == "__main__":
    unittest.main()
