"""Ingest tests: what the page can show must depend on the collector's files alone.

Runs anywhere: no collector, no cluster, no network. Each test writes a small
feed into a scratch directory laid out the way the collector lays it out --
`<root>/<date>/<host>.{exits,snapshot}.jsonl` -- and drives the Aggregator
through the calls `app.py` makes.

    cd dashboard && .venv/bin/python -m unittest discover -s tests -q
"""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rc_dashboard import config as configmod      # noqa: E402
from rc_dashboard.aggregator import Aggregator     # noqa: E402
from rc_dashboard import tail                     # noqa: E402

TS = "%Y-%m-%d %H:%M:%S"
DAY = "2026-09-25"
NONE = {"agent": 0, "human-vscode": 0, "human": 0, "unlabeled": 0}


def stamp(epoch):
    return time.strftime(TS, time.localtime(epoch))


def exit_rec(epoch, pid, actor3="agent", host="h1"):
    return {"schema_version": 6, "event": "exit", "ts": stamp(epoch),
            "ts_epoch": epoch, "host": host, "user": "alice", "actor3": actor3,
            "agent_type": "claude_code" if actor3 == "agent" else None,
            "comm": "git", "args": "git status", "pid": pid, "ppid": 1,
            "cpu_s": 0.5, "duration_s": 1.0}


def totals_rec(epoch, n_procs, host="h1"):
    return {"schema_version": 6, "event": "residency_totals", "ts": stamp(epoch),
            "ts_epoch": epoch, "host": host,
            "by_actor3": {"agent": {"n_procs": n_procs, "threads": n_procs,
                                    "rss_mb": 100.0, "d_state": 0, "users": 1}},
            "by_agent_type": {}, "trees": 1, "tracked_live": n_procs}


def line(rec):
    return json.dumps(rec).encode() + b"\n"


class FeedCase(unittest.TestCase):
    """A scratch collector root, and a config that reads only from it."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rc_ingest_")
        self.root = os.path.join(self.tmp, "ebpf")
        os.makedirs(os.path.join(self.root, DAY))
        self.now = time.time()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def path(self, name):
        return os.path.join(self.root, DAY, name)

    def append(self, name, recs, raw=b""):
        with open(self.path(name), "ab") as fh:
            for r in recs:
                fh.write(line(r))
            fh.write(raw)

    def cfg(self, **live):
        tree = configmod.defaults()
        tree["feeds"]["ebpf"]["roots"] = [self.root]
        # The node tier is not under test: point it at nothing.
        tree["feeds"]["ebpf_node"]["roots"] = [os.path.join(self.tmp, "node")]
        # Where the retired offset store lived. Scratch, so a regression that
        # brings it back is caught by these tests instead of writing into the
        # repo tree.
        tree["cache"]["state_dir"] = os.path.join(self.tmp, "state")
        tree["live"].update(live)
        return configmod.Config(tree, {}, None)

    def thirty_exits(self):
        """One exit every 5 minutes over the last 150 minutes, 20 agent + 10 human.

        Oldest first, the order the collector appends in: exit `i` is
        `5 * i + 1` minutes old.
        """
        return [exit_rec(self.now - 60 * (5 * i + 1), pid=1000 + i,
                         actor3="agent" if i % 3 else "human")
                for i in reversed(range(30))]

    @staticmethod
    def events(agg):
        return agg.live_payload(1440)["now"]["events"]


class RestartTest(FeedCase):

    def test_a_restart_serves_the_history_the_previous_run_already_read(self):
        # The reported bug: offsets survived a restart while the bins they fed
        # did not, so a second process resumed at EOF and showed nothing older
        # than its own start.
        self.append("h1.exits.jsonl", self.thirty_exits())
        cfg = self.cfg()

        first = Aggregator(cfg)
        first.backfill_now()
        first.poll_once()
        self.assertEqual(self.events(first), dict(NONE, agent=20, human=10))

        second = Aggregator(cfg)
        second.backfill_now()
        second.poll_once()
        self.assertEqual(self.events(second), dict(NONE, agent=20, human=10))

    def test_a_poll_that_runs_before_positioning_reads_nothing(self):
        # app.py polls while the backfill runs. A file the tail has not been
        # handed yet would be read from byte 0 -- and then read again by the
        # backfill, counting the whole history twice.
        self.append("h1.exits.jsonl", self.thirty_exits())
        agg = Aggregator(self.cfg())

        self.assertEqual(agg.poll_once(), 0)
        agg.backfill_now()
        agg.poll_once()
        self.assertEqual(self.events(agg), dict(NONE, agent=20, human=10))


class HandOffTest(FeedCase):

    def test_the_read_back_and_the_tail_split_each_file_at_the_positioning_point(self):
        # Three complete lines and the first half of a fourth, as the collector
        # leaves a file mid-write. The half line belongs to the tail: it is not
        # parseable yet, and positioning past it would lose it once complete.
        t = self.now - 600
        recs = [exit_rec(t + 60 * i, pid=i) for i in range(1, 6)]
        fourth = line(recs[3])
        self.append("h1.exits.jsonl", recs[:3], raw=fourth[:20])

        src = tail.FileTailSource([self.root], days=8)
        plan = src.position(since=self.now - 3600)
        # Everything after this point is the tail's, including records written
        # while the read-back is still running.
        self.append("h1.exits.jsonl", [], raw=fourth[20:] + line(recs[4]))

        stats = {"bytes": 0, "parse_errors": 0}
        self.assertEqual([raw["pid"] for _ts, raw in tail.read_back(plan, stats)],
                         [3, 2, 1])
        self.assertEqual([raw["pid"] for raw in src.poll()], [4, 5])
        self.assertEqual([raw["pid"] for raw in src.poll()], [])


class BackfillTest(FeedCase):

    def test_the_backfill_leaves_each_hosts_newest_state_and_a_time_ordered_tail(self):
        # Hosts interleave in time. The event tail must interleave with them,
        # and each host's plate must show its NEWEST tick -- whichever order
        # the files are read in.
        m = 60
        for host, base in (("h1", 100), ("h2", 200)):
            self.append("%s.snapshot.jsonl" % host,
                        [totals_rec(self.now - 50 * m, 1, host),
                         totals_rec(self.now - 30 * m, 2, host),
                         totals_rec(self.now - 10 * m, 3, host)])
        self.append("h1.exits.jsonl", [exit_rec(self.now - 40 * m, 101, host="h1"),
                                       exit_rec(self.now - 20 * m, 102, host="h1"),
                                       exit_rec(self.now - 5 * m, 103, host="h1")])
        self.append("h2.exits.jsonl", [exit_rec(self.now - 35 * m, 201, host="h2"),
                                       exit_rec(self.now - 15 * m, 202, host="h2"),
                                       exit_rec(self.now - 2 * m, 203, host="h2")])
        agg = Aggregator(self.cfg())
        agg.backfill_now()

        live = agg.live_payload(1440)
        self.assertEqual([e["pid"] for e in live["events"]],
                         [203, 103, 202, 102, 201, 101])
        self.assertEqual([(h["host"], h["ts"], h["procs"]) for h in live["hosts"]],
                         [("h1", stamp(self.now - 10 * m), 3),
                          ("h2", stamp(self.now - 10 * m), 3)])

    def test_a_failing_node_tier_does_not_stall_the_backfill(self):
        # The node tier is read first so its panel is not blank for the length
        # of the backfill. A snapshot line that is valid JSON but not an object
        # makes that read raise -- which must not leave the eBPF history unread
        # and the page saying "reading history" forever.
        self.append("h1.exits.jsonl", self.thirty_exits())
        node_day = os.path.join(self.tmp, "node", DAY)
        os.makedirs(node_day)
        with open(os.path.join(node_day, "n1.jsonl"), "w") as fh:
            fh.write("[1, 2]\n")
        agg = Aggregator(self.cfg())
        agg.backfill_now()
        self.assertEqual((agg.backfill["state"], self.events(agg)),
                         ("done", dict(NONE, agent=20, human=10)))

    def test_backfill_hours_bounds_the_startup_read_by_time(self):
        # Exits at 1, 6, 11, ... 146 minutes ago: twelve of them inside the
        # last hour (i = 0..11), the rest older.
        self.append("h1.exits.jsonl", self.thirty_exits())
        agg = Aggregator(self.cfg(backfill_hours=1))
        agg.backfill_now()
        # i % 3 == 0 is human: i = 0, 3, 6, 9 -> 4 human, 8 agent.
        self.assertEqual(self.events(agg), dict(NONE, agent=8, human=4))

    def test_backfill_hours_zero_starts_empty_and_fills_forward(self):
        self.append("h1.exits.jsonl", self.thirty_exits())
        agg = Aggregator(self.cfg(backfill_hours=0))
        agg.backfill_now()
        self.assertEqual(self.events(agg), NONE)

        self.append("h1.exits.jsonl", [exit_rec(time.time() - 1, pid=9)])
        agg.poll_once()
        self.assertEqual(self.events(agg), dict(NONE, agent=1))


class ConcurrencyTest(FeedCase):

    def test_the_page_can_read_while_the_backfill_runs(self):
        # The point of reading history concurrently: the tail and every API read
        # get the lock between backfill batches, instead of queueing behind the
        # whole read. A lock that is released and immediately re-taken by the
        # thread still holding the GIL is never handed over.
        import threading
        t = self.now - 20000
        self.append("h1.exits.jsonl", [exit_rec(t + 0.5 * i, pid=i) for i in range(30000)])
        agg = Aggregator(self.cfg())
        fill = threading.Thread(target=agg.backfill_now)
        fill.start()
        try:
            deadline = time.time() + 20
            while agg.backfill.get("records", 0) == 0 and fill.is_alive() \
                    and time.time() < deadline:
                time.sleep(0.001)
            self.assertTrue(fill.is_alive(), "backfill ended before a read was tried")
            agg.live_payload(1440)
            self.assertTrue(fill.is_alive(),
                            "a read queued behind the whole backfill instead of a batch")
        finally:
            fill.join()


class PanelsTest(FeedCase):

    def test_the_default_window_builds_its_panels_over_the_whole_feed(self):
        self.append("h1.exits.jsonl", self.thirty_exits())
        agg = Aggregator(self.cfg())
        agg.start()
        try:
            agg.backfill_now()
            agg.panels(1440)
            deadline = time.time() + 20
            while time.time() < deadline:
                # status(), not panels(): asking again re-queues a failed build,
                # and the failure is what this has to be able to see.
                held = {w["minutes"]: w for w in agg.panels_by_window.status()["held"]}
                if held[1440]["state"] in ("ready", "error"):
                    break
                time.sleep(0.05)
            self.assertEqual((held[1440]["state"], held[1440]["error"]), ("ready", None))
            by_class = agg.panels(1440)["panels"]["tool_mix"]["by_class"]
            self.assertEqual((by_class["agent"]["calls"], by_class["human"]["calls"]),
                             (20, 10))
        finally:
            agg.stop()


if __name__ == "__main__":
    unittest.main()
