"""What a ranked session cost: memory, disk I/O, network, and how recent it is.

Disk I/O is the one that needs care. The collector reads `task->signal->ioac`,
which the kernel also credits with every child the process reaped, so a
shell's figure holds its commands' as well; summed as it comes, a command's
bytes counted once per shell above it. These tests pin each process's OWN
figure, across an hour seam too, where the parent's exit comes an hour after
its child's.

    cd dashboard && .venv/bin/python -m unittest discover -s tests -q
"""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_merge import (HOUR, NOW, diff, hour_states, materialize, merged,   # noqa: E402
                        normalized, one_pass)
from test_sessions import SNAP, cfg, ranked, rec, reduce                    # noqa: E402

from rc_dashboard.aggregator import Aggregator                              # noqa: E402
from rc_dashboard.normalize import Normalizer                               # noqa: E402
from rc_dashboard.reducers.live import LiveReducer                          # noqa: E402
from rc_dashboard.windows import PanelView                                  # noqa: E402


def io(rd, wr, scope="process"):
    return {"rd_mb": rd, "wr_mb": wr, "rchar_mb": rd, "wchar_mb": wr, "scope": scope}


def session(t0, root=100, user="alice", host="h1", scope="process"):
    """A Claude Code session: `cat` run by a Bash tool call's shell, and a Grep.

    The shell's figure is the kernel's: its own 0.5 MB read and 2 MB written,
    plus the 10 MB its child `cat` read.
    """
    base = root * 10
    return [
        rec("residency", t0, root, 50, "claude", "claude", agent_pid=root, tree_root=root,
            is_agent=True, user=user, host=host),
        rec("exit", t0 + 10, base + 1, base, "cat", "cat big.bin", agent_pid=base,
            tree_root=root, user=user, host=host, io=io(10.0, 0.0, scope),
            net_tx_bytes=0, net_rx_bytes=0, peak_rss_mb=3.0),
        rec("exit", t0 + 11, base, root, "zsh", SNAP + "'cat big.bin'", agent_pid=base,
            tree_root=root, is_agent=True, user=user, host=host,
            io=io(10.5 if scope == "process" else 0.5, 2.0, scope),
            net_tx_bytes=0, net_rx_bytes=0, peak_rss_mb=4.0),
        rec("exit", t0 + 20, base + 2, root, "rg", "rg TODO", agent_pid=root, tree_root=root,
            user=user, host=host, io=io(1.0, 0.0, scope),
            net_tx_bytes=3_000_000, net_rx_bytes=7_000_000, peak_rss_mb=120.0),
    ]


class OwnIoTest(unittest.TestCase):

    def test_a_shell_does_not_count_its_childs_disk_io_again(self):
        row = ranked(reduce(session(time.time() - 600)))[0]
        self.assertAlmostEqual(row["io_rd_mb"], 11.5)     # 10 + 0.5 + 1, not 21.5
        self.assertAlmostEqual(row["io_wr_mb"], 2.0)

    def test_a_leader_only_figure_is_already_its_own(self):
        row = ranked(reduce(session(time.time() - 600, scope="leader")))[0]
        self.assertAlmostEqual(row["io_rd_mb"], 11.5)

    def test_network_adds_up_and_memory_is_the_largest_process(self):
        row = ranked(reduce(session(time.time() - 600)))[0]
        self.assertAlmostEqual(row["net_tx_mb"], 3.0)
        self.assertAlmostEqual(row["net_rx_mb"], 7.0)
        self.assertEqual(row["peak_rss_mb"], 120.0)


class RanksTest(unittest.TestCase):

    def setUp(self):
        t0 = time.time() - 3000
        # bob's session is the later one and moves the most bytes; alice's the
        # larger process, with more processes
        alice = session(t0, root=100, user="alice")
        alice.append(rec("exit", t0 + 25, 1003, 100, "python", "python big.py", agent_pid=100,
                         tree_root=100, io=io(0.0, 0.0), peak_rss_mb=900.0))
        bob = session(t0 + 600, root=500, user="bob")
        bob[1]["io"] = io(400.0, 0.0)
        bob[2]["io"] = io(400.5, 2.0)
        bob[3]["net_rx_bytes"] = 70_000_000
        self.p = reduce(alice + bob)
        self.users = lambda rank: [r["user"] for r in ranked(self.p, rank)]

    def test_each_resource_ranks_its_own_heaviest_first(self):
        self.assertEqual(self.users("io_mb"), ["bob", "alice"])
        self.assertEqual(self.users("net_mb"), ["bob", "alice"])
        self.assertEqual(self.users("peak_rss_mb"), ["alice", "bob"])
        self.assertEqual(self.users("events"), ["alice", "bob"])

    def test_recency_puts_the_newest_activity_first(self):
        self.assertEqual(self.users("recent"), ["bob", "alice"])

    def test_recency_leaves_out_a_probe_and_a_tool_call_still_running(self):
        t0 = time.time() - 600
        recs = session(t0) + [
            # a status-bar script probing codex: no agent at its root
            rec("exit", t0 + 100, 900, 1, "codex", "codex --version", agent_pid=900,
                tree_root=900, is_agent=True, agent_type="codex"),
            # a Bash tool call's child: its shell has not exited, so it is unseen
            rec("exit", t0 + 110, 1201, 1200, "sleep", "sleep 60", agent_pid=1200,
                tree_root=100),
        ]
        p = reduce(recs)["trajectories"]
        top = [p["rows"][s]["agent_pid"] for s in p["ranks"]["recent"]]
        self.assertEqual(top, ["100"])
        self.assertIn("1200", [p["rows"][s]["agent_pid"] for s in p["ranks"]["events"]])

    def test_chain_runs_is_no_longer_a_ranking(self):
        t = self.p["trajectories"]
        self.assertNotIn("chain_runs", t["ranks"])
        self.assertEqual(set(t["ranks"]), set(t["rank_meaning"]))

    def test_a_recency_share_counts_sessions_not_timestamps(self):
        self.assertEqual(self.p["trajectories"]["dominant_user_share_pct"]["recent"], 50.0)


class SeamTest(unittest.TestCase):
    """The child exits in one hour, the shell that reaped it in the next."""

    def setUp(self):
        seam = (int(NOW // HOUR) - 1) * HOUR
        recs = session(seam - 30)
        # move the shell's exit (and the Grep after it) past the seam
        recs[2]["ts_epoch"] = seam + 30
        recs[2]["ts"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(seam + 30))
        recs[3]["ts_epoch"] = seam + 40
        recs[3]["ts"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(seam + 40))
        self.recs = recs

    def row(self, payload):
        t = payload["trajectories"]
        return t["rows"][t["ranks"]["events"][0]]

    def test_the_join_is_carried_across_the_seam(self):
        want = materialize(one_pass(normalized(self.recs)))
        saved, carry, fresh = hour_states(normalized(self.recs))
        self.assertGreaterEqual(len(saved), 2)
        got = materialize(merged(saved, fresh, carry))
        self.assertAlmostEqual(self.row(want)["io_rd_mb"], 11.5)
        self.assertEqual(diff(want["trajectories"], got["trajectories"]), [])

    def test_without_the_carry_the_shell_would_count_its_child_again(self):
        # the carry, dropped: each hour reduced on its own
        saved = []
        for r in normalized(self.recs):
            s, _, fresh = hour_states([r])
            saved += s
        cold = materialize(merged(saved, fresh, None))
        self.assertAlmostEqual(self.row(cold)["io_rd_mb"], 21.5)


class LiveTest(unittest.TestCase):
    """The panel re-reads the sessions every few seconds while a view is fed."""

    def view(self, recs):
        c = cfg()
        classes = list(c.get("filters.classes")) + [c.get("filters.unlabeled_class")]
        v = PanelView(c, classes, 1440)
        v.reducers = v.fresh_reducers()
        v.dispatch = PanelView.build_dispatch(v.reducers)
        norm = Normalizer(c)
        for raw in recs:
            v.feed(norm.normalize(raw))
        v.materialize()
        return v, norm

    def test_a_live_record_reaches_the_ranking_on_the_next_read(self):
        t0 = time.time() - 600
        v, norm = self.view(session(t0))
        before = v.payload["trajectories"]
        v.feed(norm.normalize(rec("exit", t0 + 60, 1099, 100, "ls", "ls", agent_pid=100,
                                  tree_root=100, net_tx_bytes=5_000_000)))
        v.materialize_key("trajectories", 3600)       # not stale yet: kept
        self.assertIs(v.payload["trajectories"], before)
        v.materialize_key("trajectories", 0)
        after = v.payload["trajectories"]
        row = after["rows"][after["ranks"]["recent"][0]]
        self.assertEqual(row["n_events"], 4)
        self.assertAlmostEqual(row["net_tx_mb"], 8.0)
        v.materialize_key("trajectories", 0)          # nothing new since: kept
        self.assertIs(v.payload["trajectories"], after)

    def running(self, recs, rows, now):
        live = LiveReducer(cfg(), ["agent", "human", "ide"])
        norm = Normalizer(cfg())
        for raw in recs:
            live.feed(norm.normalize(raw))
        return Aggregator._running_rows(type("A", (), {"live": live})(), rows, now=now)

    def test_only_the_newest_row_of_a_running_agent_is_marked(self):
        now = time.time()
        rows = {"d1": {"host": "h1", "agent_pid": "100", "last": now - 90000},
                "d2": {"host": "h1", "agent_pid": "100", "last": now - 30},
                "gone": {"host": "h1", "agent_pid": "500", "last": now - 40}}
        recs = [rec("conn", now - 50, 100, 50, "claude", "claude", agent_pid=100,
                    tree_root=100, is_agent=True),
                rec("residency", now - 20, 100, 50, "claude", "claude", agent_pid=100,
                    tree_root=100, is_agent=True, n_procs=6, rss_mb=480.0),
                # an agent last heard from long ago is not running now
                rec("conn", now - 3600, 500, 50, "claude", "claude", agent_pid=500,
                    tree_root=500, is_agent=True)]
        out = self.running(recs, rows, now)
        self.assertNotIn("running", out["d1"])
        self.assertNotIn("running", out["gone"])
        self.assertEqual(out["d2"]["running"]["n_procs"], 6)
        self.assertEqual(out["d2"]["running"]["rss_mb"], 480.0)
        self.assertNotIn("running", rows["d2"])       # the cached row is untouched


if __name__ == "__main__":
    unittest.main()
