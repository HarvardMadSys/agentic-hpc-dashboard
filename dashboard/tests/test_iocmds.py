"""Every command behind an I/O number (iocmds.py), looked up on click.

    cd dashboard && .venv/bin/python -m unittest tests.test_iocmds -q
"""
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from rc_dashboard import iocmds                         # noqa: E402
from rc_dashboard.export import parse_filters           # noqa: E402
from test_ingest import FeedCase, exit_rec              # noqa: E402


def rec(epoch, pid, args, user="alice", actor3="agent", rd=0.0, wr=0.0, rc=0.0, wc=0.0,
        tx=0, rx=0, cwd="/home/alice/proj", apid=None, host="h1", comm=None):
    r = exit_rec(epoch, pid, actor3=actor3, host=host)
    r.update(user=user, args=args, comm=comm or args.split()[0], cwd=cwd,
             io={"rchar_mb": rc, "wchar_mb": wc, "rd_mb": rd, "wr_mb": wr},
             net_tx_bytes=tx, net_rx_bytes=rx)
    if apid is not None:
        r.update(agent_pid=apid, session_key="apid:%d" % apid)
    return r


class Lookup(FeedCase):

    def setUp(self):
        super().setUp()
        t = self.now - 600
        self.recs = [
            rec(t + 1, 11, "git status", rd=1.0, wr=0.5, rc=2.0, wc=1.0, apid=7),
            rec(t + 2, 12, "git status", rd=2.0, wr=0.5, rc=3.0, wc=1.0, apid=7),
            rec(t + 3, 13, "python train.py --lr 3", rd=12.0, wr=4.0, tx=5_000_000,
                cwd="/home/alice/proj/sub", apid=7),
            rec(t + 4, 14, "cat notes.md", user="bob", actor3="human", rd=0.25,
                cwd="/home/bob"),
            rec(t + 5, 15, "rg TODO", rd=0.5, cwd="/tmp", apid=8),
            rec(t + 6, 16, "git status", host="h2", rd=7.0, apid=7),
        ]
        self.append("h1.jsonl", [r for r in self.recs if r["host"] == "h1"])
        self.append("h2.jsonl", [r for r in self.recs if r["host"] == "h2"])
        self.io = iocmds.IoCommands(self.cfg(), lambda: [self.root])

    def run_job(self, params, window_min=60, **kw):
        f = parse_filters({k: [v] for k, v in params.items()})
        f.pop("q", None)
        f["event"] = None
        j = self.io.start(f, window_min, **kw)
        for _ in range(200):
            if j.state in ("done", "error"):
                break
            time.sleep(0.02)
        self.assertEqual(j.state, "done", j.error)
        return j

    def rows(self, j, sort="rd_mb", desc=True):
        return j.page(sort, desc, 0, 1000)

    def test_every_command_grouped_and_summed(self):
        j = self.run_job({})
        rows = {(r["cmd"], tuple(r["users"])): r for r in self.rows(j)}
        git = rows[("git status", ("alice",))]
        self.assertEqual(git["n"], 3)                      # both hosts
        self.assertAlmostEqual(git["rd_mb"], 10.0)
        self.assertAlmostEqual(git["rchar_mb"], 5.0)
        self.assertEqual(len(rows), 4)                     # every command, none cut
        self.assertEqual(j.totals["n"], 6)
        self.assertAlmostEqual(j.totals["rd_mb"], 22.75)
        self.assertAlmostEqual(j.totals["tx_mb"], 5.0)
        self.assertEqual(rows[("python train.py --lr 3", ("alice",))]["tool"], "python")

    def test_sorted_and_paged(self):
        j = self.run_job({})
        by_rd = [r["cmd"] for r in self.rows(j)]
        self.assertEqual(by_rd[0], "python train.py --lr 3")
        self.assertEqual([r["cmd"] for r in self.rows(j, "cmd", False)],
                         sorted(by_rd))
        self.assertEqual([r["cmd"] for r in j.page("rd_mb", True, 1, 2)], by_rd[1:3])

    def test_class_and_user(self):
        self.assertEqual([r["cmd"] for r in self.rows(self.run_job({"class": "human"}))],
                         ["cat notes.md"])
        j = self.run_job({"user": "alice", "tool": "git"})
        self.assertEqual([(r["cmd"], r["n"]) for r in self.rows(j)], [("git status", 3)])

    def test_blast_radius_row(self):
        # agent 7 on h1, in /home/alice/proj or below: not h2's, not agent 8's
        j = self.run_job({}, apid=["7"], host="h1", cwd="/home/alice/proj")
        got = {r["cmd"]: r["n"] for r in self.rows(j)}
        self.assertEqual(got, {"git status": 2, "python train.py --lr 3": 1})
        # a directory names itself and what is below it, not a sibling prefix
        self.append("h1.jsonl", [rec(self.now - 100, 20, "ls", cwd="/home/alice/project2",
                                     apid=7)])
        j = self.run_job({"user": "alice"}, window_min=59, apid=["7"], host="h1",
                         cwd="/home/alice/proj/")
        self.assertNotIn("ls", {r["cmd"] for r in self.rows(j)})

    def test_time_bin(self):
        t = self.now - 600
        j = self.run_job({"from": str(t + 2.5), "to": str(t + 5.5)})
        self.assertEqual({r["cmd"] for r in self.rows(j)},
                         {"python train.py --lr 3", "cat notes.md", "rg TODO"})

    def test_prefilter_never_misses(self):
        f = parse_filters({"user": ["alice"]})
        m = iocmds.prefilter(f, frozenset({"7"}), "h1", "/home/alice/proj")
        import json
        for r in self.recs:
            want = (r["user"] == "alice" and r.get("agent_pid") == 7 and r["host"] == "h1"
                    and r["cwd"].startswith("/home/alice/proj"))
            if want:
                self.assertTrue(m(json.dumps(r).encode()), r)

    def test_same_question_reuses_the_lookup(self):
        a = self.run_job({"user": "bob"})
        b = self.run_job({"user": "bob"})
        self.assertIs(a, b)
        c = self.run_job({"user": "alice"})
        self.assertIsNot(a, c)

    def test_secrets_masked(self):
        self.append("h1.jsonl", [rec(self.now - 50, 30, "curl -H 'Authorization: Bearer abc123def'")])
        j = self.run_job({}, window_min=58)
        cmds = " ".join(r["cmd"] for r in self.rows(j))
        self.assertNotIn("abc123def", cmds)


class AgentKeys(unittest.TestCase):
    """A blast-radius row's agent, as the session keys folded into it."""

    def test_tool_call_keys_fold_into_their_agent(self):
        class Procs:
            def owner(self, host, pid, root):
                return {500: 7, 501: 7, 600: 8}.get(pid, pid), "ancestor"

        class St:
            root = None

        class Ws:
            procs = Procs()
            keys = {("h1", "7"): St(), ("h1", "500"): St(), ("h1", "501"): St(),
                    ("h1", "600"): St(), ("h2", "500"): St(), ("h1", "x"): St()}

        class Cas:
            ws = Ws()

        class View:
            reducers = {"cascades": Cas()}

        self.assertEqual(iocmds.agent_keys(View(), "h1", ["7"]), {"7", "500", "501"})
        self.assertEqual(iocmds.agent_keys(View(), "h1", ["7", "8"]), {"7", "8", "500", "501", "600"})
        # no window held yet: the agent's own key, never nothing
        self.assertEqual(iocmds.agent_keys(None, "h1", ["7"]), {"7"})


if __name__ == "__main__":
    unittest.main()
