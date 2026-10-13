"""The trajectory inspector's process tree: links, groups, bounds, masking.

A ranked row is a run-length summary; the inspector re-reads one session's
records and draws them as the process tree they were. These tests pin the
parts that are easy to get quietly wrong: a parent whose exit is written AFTER
its children's, a process that outlived its parent, a parent that left no
record at all, a poll loop of a thousand identical processes, the budgets that
keep a 1.6 M-process session servable, and that a credential on a command line
is masked before the preview is cut.

    cd dashboard && .venv/bin/python -m unittest discover -s tests -q
"""
import collections
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rc_dashboard import config as configmod                       # noqa: E402
from rc_dashboard import proctree                                  # noqa: E402
from rc_dashboard.normalize import Normalizer                      # noqa: E402
from rc_dashboard.export import window_plan, window_scan           # noqa: E402
from rc_dashboard.proctree import (PREVIEW, TreeBuilder, key_matcher,  # noqa: E402
                                   key_patterns, key_scan, ran, runs)
from rc_dashboard.windows import PanelView                         # noqa: E402

AGENT = 100
T0 = 1_790_000_000.0


def ex(t, pid, ppid, cmd, args=None, *, kv=AGENT, depth=1, dur=0.5, cpu=0.01,
       code=0, signal=None, role=None, event="exit"):
    """A normalised exit record, as the scan hands it to the builder."""
    return {"event": event, "_ts_epoch": t, "pid": pid, "ppid": ppid,
            "_command": cmd, "args": args if args is not None else cmd,
            "duration_s": dur, "cpu_s": cpu, "exit_code": code, "signal": signal,
            "_kv": str(kv), "depth": depth, "_role": role, "_a3": "agent",
            "agent_type": "claude_code", "user": "alice", "cwd": "/home/alice/proj"}


def build(recs, owner=AGENT, keys=(AGENT,), index=None, budget=3000, **kw):
    b = TreeBuilder(**kw)
    for r in recs:
        b.feed(r)
    root = b.resolve(owner, keys, index or (lambda pid: None))
    tree, stats = b.render(root, budget)
    return b, tree, stats


def procs(node):
    """Every itemised process dict in a served tree, depth first."""
    out = []
    if node.get("k") == "p":
        out.append(node)
    for k in node.get("kids", []) + node.get("items", []):
        out += procs(k)
    return out


def find(node, pid):
    return next((p for p in procs(node) if p["pid"] == pid), None)


def kids(node):
    return node.get("kids", [])


class LinkTest(unittest.TestCase):

    def test_a_parent_written_after_its_children_claims_them(self):
        # A tool call: the shell's exit comes after the two commands it ran.
        _, tree, _ = build([
            ex(T0 + 1, 201, 200, "git", "git status"),
            ex(T0 + 2, 202, 200, "sed", "sed -n 1,9p f"),
            ex(T0 + 3, 200, AGENT, "zsh", depth=0, kv=200, role="tool-shell", dur=3),
        ], keys=(AGENT, 200))
        self.assertEqual(tree["pid"], AGENT)
        self.assertIsNone(tree["ev"], "the agent left no exit: it is drawn without one")
        shell = find(tree, 200)
        self.assertEqual(shell["link"], "ppid")
        self.assertEqual([k["pid"] for k in kids(shell)], [201, 202])
        self.assertTrue(all(k["link"] == "ppid" for k in kids(shell)))
        self.assertEqual(tree["n"], 3)

    def test_a_child_written_after_its_parent_still_hangs_from_it(self):
        # The collector names the parent a process was started by; a child
        # that outlives it, or lands a poll round late, comes after its exit.
        _, tree, _ = build([
            ex(T0 + 2, 700, AGENT, "bash", dur=2),     # alive T0 .. T0+2
            ex(T0 + 3, 701, 700, "tee", dur=2),        # started T0+1
        ])
        self.assertEqual([k["pid"] for k in kids(find(tree, 700))], [701])
        self.assertEqual(find(tree, 701)["link"], "ppid")

    def test_children_are_in_start_order_not_exit_order(self):
        # The long one started first and exited last.
        _, tree, _ = build([
            ex(T0 + 5, 302, AGENT, "ls", dur=1.0),
            ex(T0 + 9, 301, AGENT, "make", dur=9.0),
        ])
        self.assertEqual([k["pid"] for k in kids(tree)], [301, 302])

    def test_a_process_that_outlived_its_parent_is_placed_by_depth_and_flagged(self):
        # Reparented to init: its record names pid 1, the collector still knows
        # it sat two levels under key 200.
        _, tree, _ = build([
            ex(T0 + 1, 200, AGENT, "zsh", kv=200, depth=0, role="tool-shell"),
            ex(T0 + 50, 205, 1, "sleep", kv=200, depth=2),
        ], keys=(AGENT, 200))
        orphan = find(tree, 205)
        self.assertTrue(orphan["orphan"])
        self.assertEqual(orphan["link"], "depth")
        self.assertEqual(orphan["gap"], 1, "one level between it and its key was never recorded")
        self.assertIn(orphan, kids(find(tree, 200)))

    def test_a_parent_with_no_record_is_drawn_from_the_index_and_says_so(self):
        index = {200: {"ppid": AGENT, "cmd": "zsh", "role": "tool-shell", "owner": AGENT}}
        _, tree, _ = build([ex(T0 + 1, 201, 200, "python", depth=1, kv=200)],
                           keys=(AGENT, 200), index=index.get)
        shell = find(tree, 200)
        self.assertIsNone(shell["ev"])
        self.assertEqual(shell["cmd"], "zsh")
        self.assertEqual(shell["link"], "index")
        self.assertEqual(kids(shell)[0]["pid"], 201)

    def test_an_unrecorded_middle_parent_is_placed_under_its_key_by_depth(self):
        # rtx6000a, 2026-10-10: 1.59 M `python` under pid 561706, which exited
        # the next day; its children's depth 2 below key 561705 places it.
        _, tree, _ = build([
            ex(T0 + 1, 401, 400, "python", kv=300, depth=2),
            ex(T0 + 2, 300, AGENT, "zsh", kv=300, depth=0, role="tool-shell", dur=2),
        ], keys=(AGENT, 300))
        middle = find(tree, 400)
        self.assertIsNone(middle["ev"])
        self.assertEqual(middle["link"], "depth")
        self.assertNotIn("gap", middle)
        self.assertIn(middle, kids(find(tree, 300)))

    def test_the_agents_own_parent_is_not_part_of_the_session(self):
        _, tree, _ = build([
            ex(T0 + 1, 201, AGENT, "git"),
            ex(T0 + 2, AGENT, 50, "claude", depth=0, role="agent"),
        ])
        self.assertEqual(tree["pid"], AGENT)
        self.assertEqual(tree["ev"], "exit")
        self.assertIsNone(find(tree, 50))

    def test_a_key_with_no_process_link_hangs_from_the_agent_by_the_fold(self):
        # A key folded by tree_root: nothing read connects it to the agent.
        _, tree, _ = build([
            ex(T0 + 1, 501, 500, "git", kv=500, depth=1),
            ex(T0 + 2, 500, 499, "zsh", kv=500, depth=0, role="tool-shell", dur=2),
        ], keys=(AGENT, 500), index={499: {"owner": 7}}.get)
        shell = find(tree, 500)
        self.assertEqual(shell["link"], "fold")
        self.assertIn(shell, kids(tree))

    def test_a_reused_pid_does_not_adopt_children_that_started_before_it(self):
        _, tree, _ = build([
            ex(T0 + 1, 601, 600, "sleep", kv=AGENT, depth=2, dur=0.5),   # started T0+0.5
            ex(T0 + 100, 600, AGENT, "bash", kv=AGENT, depth=1, dur=2),  # started T0+98
        ])
        self.assertEqual(kids(find(tree, 600)), [])
        self.assertIsNotNone(find(tree, 601))


class GroupTest(unittest.TestCase):

    def test_a_poll_loop_is_one_group_with_its_totals(self):
        recs = [ex(T0 + i, 1000 + i, 900, "pgrep", "pgrep -f vite", kv=900, depth=1,
                   cpu=0.002, code=0 if i % 40 else 1)
                for i in range(120)]
        recs.append(ex(T0 + 200, 900, AGENT, "zsh", kv=900, depth=0, role="tool-shell",
                       dur=201))
        _, tree, _ = build(recs, keys=(AGENT, 900))
        (g,) = kids(find(tree, 900))
        self.assertTrue(all(i["link"] == "ppid" for i in g["items"]))
        self.assertEqual(g["k"], "g")
        self.assertEqual((g["motif"], g["reps"], g["count"], g["n"]), (["pgrep"], 120, 120, 120))
        self.assertEqual(g["exits"], {"0": 117, "1": 3})
        self.assertEqual(len(g["items"]), proctree.GROUP_ITEMS)
        self.assertEqual(g["more"], 120 - proctree.GROUP_ITEMS)
        self.assertEqual(g["samples"], [{"args": "pgrep -f vite", "n": 120}])
        self.assertAlmostEqual(g["cpu"], 0.24, places=6)
        self.assertEqual(g["last"], T0 + 119 - 0.5,
                         "when the last member started: the undrawn ones started by then")

    def test_a_pipeline_racing_its_children_still_groups(self):
        # `lsb_release -a` forks `cut` and `tr`; which starts first is a race.
        recs = []
        for i in range(5):
            p = 800 + 10 * i
            a, b = ("cut", "tr") if i % 2 else ("tr", "cut")
            recs += [ex(T0 + 10 * i + 0.1, p + 1, p, a, dur=0.05),
                     ex(T0 + 10 * i + 0.2, p + 2, p, b, dur=0.05),
                     ex(T0 + 10 * i + 0.5, p, AGENT, "lsb_release", dur=0.5)]
        _, tree, _ = build(recs)
        (g,) = kids(tree)
        self.assertEqual((g["k"], g["reps"], g["n"]), ("g", 5, 15))

    def test_an_alternating_loop_is_one_motif(self):
        recs = []
        for i in range(30):
            recs.append(ex(T0 + 2 * i, 2000 + 2 * i, 900, "pgrep", kv=900))
            recs.append(ex(T0 + 2 * i + 1, 2001 + 2 * i, 900, "sleep", kv=900))
        recs.append(ex(T0 + 99, 900, AGENT, "bash", kv=900, depth=0, dur=100))
        _, tree, _ = build(recs, keys=(AGENT, 900))
        (g,) = kids(find(tree, 900))
        self.assertEqual((g["motif"], g["period"], g["reps"]), (["pgrep", "sleep"], 2, 30))

    def test_tool_calls_of_the_same_shape_group_but_keep_what_each_ran(self):
        snap = "/bin/zsh -c source /h/.claude/shell-snapshots/s.sh && eval '%s' < /dev/null"
        recs = []
        for i, cmd in enumerate(["git status", "git diff", "git log", "git show"]):
            sh = 300 + 10 * i
            recs.append(ex(T0 + 3 * i, sh + 1, sh, "git", cmd, kv=sh, depth=1))
            recs.append(ex(T0 + 3 * i + 1, sh, AGENT, "zsh", snap % cmd, kv=sh, depth=0,
                           role="tool-shell", dur=2))
        _, tree, _ = build(recs, keys=(AGENT, 300, 310, 320, 330))
        (g,) = kids(tree)
        self.assertEqual((g["motif"], g["reps"]), (["zsh › git"], 4))
        self.assertEqual(sorted(s["cmdline"] for s in g["samples"]),
                         ["git diff", "git log", "git show", "git status"])
        self.assertEqual(g["items"][0]["cmdline"], "git status")
        self.assertNotIn("args", g["items"][0], "the snapshot boilerplate is not the preview")

    def test_runs_picks_the_motif_covering_most(self):
        self.assertEqual(runs([1, 1, 1, 2, 3]), [(0, 1, 3), (3, 1, 1), (4, 1, 1)])
        self.assertEqual(runs([1, 2, 1, 2, 1, 2, 3]), [(0, 2, 3), (6, 1, 1)])
        self.assertEqual(runs([1, 1, 2]), [(0, 1, 1), (1, 1, 1), (2, 1, 1)])
        self.assertEqual(runs([-1, -1, -1]), [(0, 1, 1), (1, 1, 1), (2, 1, 1)],
                         "a stub or an agent never folds into a group")


class BoundsTest(unittest.TestCase):

    def test_past_the_hold_budget_processes_are_counted_not_dropped(self):
        recs = [ex(T0 + i, 3000 + i, AGENT, "cmd%d" % (i % 7)) for i in range(50)]
        b, tree, _ = build(recs, hold=20)
        self.assertEqual(b.matched, 50)
        self.assertEqual(b.itemised - b.absorbed, 20)
        agg = [k for k in kids(tree) if k["k"] == "agg"]
        self.assertEqual(len(agg), 1)
        self.assertEqual(agg[0]["n"], 30)
        self.assertEqual(sum(n for _, n in agg[0]["cmds"]), 30)
        self.assertEqual(tree["n"], 50, "every record read is in the root's count")

    def test_one_poll_loop_cannot_spend_the_whole_hold(self):
        recs = [ex(T0 + i * 0.01, 10000 + i, 900, "python", kv=900, depth=1)
                for i in range(400)]
        recs.append(ex(T0 + 10, 900, AGENT, "zsh", kv=900, depth=0, dur=11))
        recs.append(ex(T0 + 11, 901, AGENT, "git", kv=AGENT, depth=1))
        b, tree, _ = build(recs, keys=(AGENT, 900), hold=1000, per_parent=50)
        loop = find(tree, 900)
        g, a = kids(loop)
        self.assertEqual((g["k"], g["count"]), ("g", 50))
        self.assertEqual((a["k"], a["n"]), ("agg", 350))
        self.assertEqual(find(tree, 901)["cmd"], "git", "a later call is still drawn")
        self.assertEqual(tree["n"], 402)

    def test_the_entry_budget_cuts_the_deep_end_and_counts_it(self):
        recs = []
        for i in range(40):
            sh = 5000 + 10 * i
            recs.append(ex(T0 + i, sh + 1, sh, "c%d" % i, kv=sh, depth=1))
            recs.append(ex(T0 + i + 0.5, sh, AGENT, "s%d" % i, kv=sh, depth=0, dur=1))
        keys = [AGENT] + [5000 + 10 * i for i in range(40)]
        _, tree, stats = build(recs, keys=keys, budget=50)
        self.assertEqual(stats["shown"], 50)
        top = kids(tree)
        self.assertEqual(len(top), 40, "the first level is whole")
        more = [k for p in top for k in kids(p) if k["k"] == "more"]
        shown = sum(1 for p in top for k in kids(p) if k["k"] == "p")
        self.assertEqual(shown, 9)
        self.assertEqual(sum(m["n"] for m in more), 31)
        self.assertEqual(stats["elided"], 31)

    def test_unmeasured_cpu_is_not_summed_as_zero(self):
        _, tree, _ = build([ex(T0 + 1, 201, AGENT, "a", cpu=None),
                            ex(T0 + 2, 202, AGENT, "b", cpu=None)])
        self.assertIsNone(tree["sub_cpu"])
        self.assertEqual(tree["cpu_nm"], 2)
        self.assertIsNone(find(tree, 201)["cpu"])


class NavTest(unittest.TestCase):
    """The navigator is the read's, not the served rows': every record counts."""

    def purposed(self, recs, purpose):
        for r in recs:
            r["_purpose"] = purpose
        return recs

    def test_starts_are_binned_by_purpose_counted_ones_included(self):
        recs = self.purposed([ex(T0 + 10 + i, 3000 + i, AGENT, "pgrep") for i in range(30)],
                             "poll")
        recs += self.purposed([ex(T0 + 40 + i, 4000 + i, AGENT, "make") for i in range(20)],
                              "build")
        b, tree, _ = build(recs, hold=20)
        self.assertEqual(b.itemised - b.absorbed, 20, "most of them only counted")
        nav = b.nav(AGENT, T0, T0 + 60)
        d = nav["density"]
        by = collections.Counter()
        for i, p, c in d["bins"]:
            by[nav["purposes"][p]] += c
            self.assertLess(i, d["n"])
        self.assertEqual(by, {"poll": 30, "build": 20})
        self.assertEqual(nav["starts"], 50)
        self.assertLessEqual(d["n"], proctree.DENSITY_BINS + 1)
        self.assertAlmostEqual(d["lo"] / d["w"], round(d["lo"] / d["w"]), places=6,
                               msg="bins sit on round clock times")

    def test_a_start_before_the_axis_is_counted_apart_not_piled_into_the_first_bin(self):
        b, _, _ = build([ex(T0 + 100, 201, AGENT, "tmux", dur=5000),
                         ex(T0 + 101, 202, AGENT, "ls")])
        d = b.nav(AGENT, T0, T0 + 101)["density"]
        self.assertEqual(d["before"], 1)
        self.assertEqual(sum(c for _, _, c in d["bins"]), 1)

    def test_what_the_quick_jumps_land_on(self):
        m0 = (int(T0 // 60) + 3) * 60
        recs = [
            ex(T0 + 5, 401, AGENT, "git", dur=1),                         # first process
            ex(T0 + 30, 400, AGENT, "zsh", role="tool-shell", dur=20),    # first tool call
            ex(T0 + 60, 402, AGENT, "grep", code=1),
            ex(T0 + 61, 403, AGENT, "python", signal=9, code=None),
            ex(T0 + 62, 404, AGENT, "sleep", event="truncated", code=None),  # alive, not failed
            ex(T0 + 400, 600, AGENT, "make", dur=300),
            ex(T0 + 900, AGENT, 50, "claude", role="agent", dur=1000, depth=0),
        ] + [ex(m0 + 1 + i, 700 + i, AGENT, "pgrep") for i in range(30)]
        recs.sort(key=lambda r: r["_ts_epoch"])
        b, _, _ = build(recs)
        nav = b.nav(AGENT, T0 - 100, T0 + 900)
        self.assertEqual((nav["first_tool"]["pid"], nav["first_tool"]["via"]), (400, "tool"))
        self.assertEqual(nav["first_tool"]["t"], T0 + 10)
        self.assertEqual((nav["longest"]["pid"], nav["longest"]["dur"]), (600, 300),
                         "the session root is the session, not its longest process")
        self.assertEqual(nav["busiest"], {"t": m0, "n": 30})
        f = nav["failures"]
        self.assertEqual((f["n"], f["listed"]), (2, 2))
        self.assertEqual([(i[2], i[4], i[5]) for i in f["items"]],
                         [(402, 1, None), (403, None, 9)])

    def test_with_no_tool_call_the_first_process_is_the_first_tool(self):
        b, _, _ = build([ex(T0 + 5, 401, AGENT, "git"), ex(T0 + 6, 402, AGENT, "ls")])
        ft = b.nav(AGENT, T0, T0 + 6)["first_tool"]
        self.assertEqual((ft["pid"], ft["via"]), (401, "first"))


class MaskTest(unittest.TestCase):

    def test_a_token_straddling_the_preview_cut_is_masked_not_halved(self):
        token = "hf_" + "A" * 40
        args = "python train.py " + "x" * (PREVIEW - 30) + " --hf-token " + token
        _, tree, _ = build([ex(T0 + 1, 201, AGENT, "python", args)])
        shown = find(tree, 201)["args"]
        self.assertLessEqual(len(shown), PREVIEW)
        self.assertNotIn("hf_AAAA", shown)
        self.assertNotIn("AAAAAAAA", json.dumps(tree))

    def test_the_command_a_tool_shell_ran_is_masked_too(self):
        cmd = "curl -H 'Authorization: Bearer sk-ant-" + "b" * 30 + "' https://x"
        args = ("/bin/zsh -c source /h/.claude/shell-snapshots/s.sh && eval '"
                + cmd.replace("'", "'\\''") + "' < /dev/null")
        _, tree, _ = build([ex(T0 + 1, 201, AGENT, "zsh", args, role="tool-shell")])
        line = find(tree, 201)["cmdline"]
        self.assertIn("[REDACTED]", line)
        self.assertNotIn("bbbbbbbb", json.dumps(tree))

    def test_ran_unwraps_the_snapshot_and_the_sandbox(self):
        self.assertEqual(ran("tool-shell", "zsh -c source /h/.claude/shell-snapshots/a "
                                           "&& eval 'echo '\\''hi'\\''' < /dev/null"),
                         "echo 'hi'")
        self.assertEqual(ran("sandbox", "bwrap --ro-bind / / -- /bin/bash -lc 'make'"),
                         "/bin/bash -lc 'make'")
        self.assertIsNone(ran(None, "ls -la"))


class KeyMatcherTest(unittest.TestCase):

    def test_matches_the_session_keys_and_nothing_with_a_longer_pid(self):
        m = key_matcher(["h1|2026-10-11|agent|apid:12", "h1|2026-10-11|agent|apid:345"])
        self.assertTrue(m(b'{"event": "exit", "session_key": "apid:12", "pid": 9}'))
        self.assertTrue(m(b'{"session_key":"apid:345"}'))
        self.assertTrue(m(b'{"session_key": null, "agent_pid": 12, "x": 1}'))
        self.assertFalse(m(b'{"session_key": "apid:123"}'))
        self.assertFalse(m(b'{"session_key": null, "agent_pid": 123}'))
        self.assertFalse(m(b'{"pid": 12}'))

    def test_the_trie_matches_exactly_its_words(self):
        words = ["12", "123", "1234", "19", "2", "20", "999999"]
        rx = re.compile(b"(?:" + proctree._trie(sorted(words)) + b")\\Z")
        for w in words + ["1", "124", "3", "12345", "21", "9999", "0"]:
            self.assertEqual(bool(rx.match(w.encode())), w in words, w)

    def test_no_byte_check_for_a_session_not_keyed_on_a_pid(self):
        self.assertIsNone(key_matcher(["h1|2026-10-11|human|tty:alice@pts1"]))


# ------------------------------------------------------------- end to end

TS = "%Y-%m-%d %H:%M:%S"
SNAP = "/bin/zsh -c source /home/alice/.claude/shell-snapshots/snapshot-zsh-1.sh && eval "


def raw(event, t, pid, ppid, command, args, *, agent_pid, tree_root=AGENT, is_agent=False,
        depth=1, host="h1", **kw):
    """One collector record with the schema-7 identity block."""
    r = {"schema_version": 7, "event": event, "ts": time.strftime(TS, time.localtime(t)),
         "ts_epoch": t, "host": host, "user": "alice", "uid": 1000,
         "actor": "agent", "actor3": "agent", "attribution": "ancestry",
         "agent_type": "claude_code", "pid": pid, "ppid": ppid,
         "command": command, "args": args, "cwd": "/home/alice/proj", "tty": None,
         "is_agent": is_agent, "agent_pid": agent_pid,
         "session_key": "apid:%s" % agent_pid, "tree_root_pid": tree_root,
         "depth": 0 if is_agent else depth,
         "sandbox": "unsandboxed", "sandbox_src": "none"}
    if event in ("exit", "truncated"):
        r.update(cpu_s=0.01, duration_s=0.5, exit_code=0,
                 start=time.strftime(TS, time.localtime(t - 0.5)))
    r.update(kw)
    return r


class KeyScanTest(unittest.TestCase):
    """The byte search reads exactly the records the line scan reads."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="keyscan-")
        self.cfg = configmod.Config(configmod.defaults(), {}, None)
        t0 = time.time() - 7200
        if time.localtime(t0).tm_mday != time.localtime(t0 + 400).tm_mday:
            t0 -= 1200
        self.t0 = t0

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def write(self, recs, *, partial=None, day_offset=0, host="h1", compact=False):
        day = os.path.join(self.dir, time.strftime(
            "%Y-%m-%d", time.localtime(self.t0 + day_offset * 86400)))
        os.makedirs(day, exist_ok=True)
        sep = (",", ":") if compact else None
        with open(os.path.join(day, host + ".jsonl"), "w") as fh:
            for r in recs:
                fh.write(json.dumps(r, separators=sep) + "\n")
            if partial is not None:
                fh.write(json.dumps(partial)[:-7])          # still being written

    def both(self, members, since, until=None):
        plan, _ = window_plan([self.dir], since, 3)
        norm = Normalizer(self.cfg)
        line = [(r["_ts_epoch"], r["pid"]) for r, _ in window_scan(
            [dict(e) for e in plan], norm, since, until, events=proctree.EVENTS,
            match=key_matcher(members)) if r.get("_sid") in members]
        fast = [(r["_ts_epoch"], r["pid"]) for r, _ in key_scan(
            [dict(e) for e in plan], Normalizer(self.cfg), since, until,
            key_patterns(members)) if r.get("_sid") in members]
        return line, fast

    def sid(self, kv):
        return "h1|%s|agent|apid:%s" % (time.strftime("%Y-%m-%d", time.localtime(self.t0)), kv)

    def recs(self):
        t0 = self.t0
        out = []
        for i in range(40):
            out.append(raw("exit", t0 + i, 5000 + i, 12, "a%d" % i, "a", agent_pid=12,
                           tree_root=12))
            out.append(raw("exit", t0 + i + 0.5, 6000 + i, 123, "b%d" % i, "b",
                           agent_pid=123, tree_root=123))
        # keyed by agent_pid alone, and an event the read does not take
        out.append(dict(raw("exit", t0 + 50, 7001, 12, "c", "c", agent_pid=12, tree_root=12),
                        session_key=None))
        out.append(raw("conn", t0 + 51, 7002, 12, "d", "d", agent_pid=12, tree_root=12))
        out.append(raw("residency", t0 + 52, 12, 1, "claude", "claude", agent_pid=12,
                       tree_root=12, is_agent=True, age_s=60))
        out.sort(key=lambda r: r["ts_epoch"])
        return out

    def test_same_records_as_the_line_scan_and_a_longer_pid_is_not_a_hit(self):
        self.write(self.recs())
        line, fast = self.both({self.sid(12)}, self.t0 - 10)
        self.assertEqual(fast, line)
        self.assertEqual(len(fast), 42, "40 exits, one keyed by agent_pid alone, one tick")
        self.assertNotIn(6000, [p for _, p in fast], "apid:123 is not apid:12")

    def test_compact_json_and_many_keys(self):
        recs = self.recs()
        self.write(recs, compact=True)
        members = {self.sid(12), self.sid(123)} | {self.sid(k) for k in range(130, 200)}
        line, fast = self.both(members, self.t0 - 10)
        self.assertEqual(fast, line)
        self.assertEqual(len(fast), 82)

    def test_a_half_written_last_line_is_left_and_a_complete_one_read(self):
        recs = self.recs()
        last = raw("exit", self.t0 + 60, 7100, 12, "e", "e", agent_pid=12, tree_root=12)
        self.write(recs + [last], partial=raw("exit", self.t0 + 61, 7101, 12, "f", "f",
                                              agent_pid=12, tree_root=12))
        line, fast = self.both({self.sid(12)}, self.t0 - 10)
        self.assertEqual(fast, line)
        pids = [p for _, p in fast]
        self.assertIn(7100, pids, "a hit in the last complete line")
        self.assertNotIn(7101, pids, "the line still being written")

    def test_chunks_split_nothing(self):
        self.write(self.recs())
        old = proctree.CHUNK
        proctree.CHUNK = 700                            # under two lines a chunk
        try:
            line, fast = self.both({self.sid(12)}, self.t0 - 10)
        finally:
            proctree.CHUNK = old
        self.assertEqual(fast, line)
        self.assertEqual(len(fast), 42)

    def test_since_until_and_two_days_merged_in_time_order(self):
        self.write(self.recs())
        nxt = [raw("exit", self.t0 + 86400 + i, 8000 + i, 12, "n", "n", agent_pid=12,
                   tree_root=12) for i in range(5)]
        self.write(nxt, day_offset=1)
        day2 = "h1|%s|agent|apid:12" % time.strftime("%Y-%m-%d",
                                                      time.localtime(self.t0 + 86400))
        line, fast = self.both({self.sid(12), day2}, self.t0 + 10, self.t0 + 86402)
        self.assertEqual(fast, line)
        self.assertEqual([p for _, p in fast][-3:], [8000, 8001, 8002])
        self.assertEqual(fast, sorted(fast))


class EndToEndTest(unittest.TestCase):
    """Day-files on disk, a window reduced from them, the tree re-read from them."""

    def setUp(self):
        proctree._cache.clear()
        self.dir = tempfile.mkdtemp(prefix="proctree-")
        t0 = time.time() - 7200
        if time.localtime(t0).tm_mday != time.localtime(t0 + 300).tm_mday:
            t0 -= 600                                   # keep it inside one day
        self.t0 = t0
        day = os.path.join(self.dir, time.strftime("%Y-%m-%d", time.localtime(t0)))
        os.makedirs(day)
        tok = "ghp_" + "Z" * 36
        mine = [
            raw("residency", t0, AGENT, 50, "claude",
                "claude --resume x --dangerously-skip-permissions",
                agent_pid=AGENT, is_agent=True, age_s=3600),
            raw("exit", t0 + 10, 201, 200, "git", "git push https://alice:%s@github.com/x" % tok,
                agent_pid=200),
            raw("exit", t0 + 11, 200, AGENT, "zsh", SNAP + "'git push'",
                agent_pid=200, is_agent=True),
        ] + [raw("exit", t0 + 20 + i, 300 + i, AGENT, "pgrep", "pgrep -f vite",
                 agent_pid=AGENT) for i in range(10)]
        other = [raw("exit", t0 + 12, 901, 900, "make", "make", agent_pid=900,
                     tree_root=900),
                 raw("exit", t0 + 13, 900, 1, "claude", "claude", agent_pid=900,
                     tree_root=900, is_agent=True)]
        lines = sorted(mine + other, key=lambda r: r["ts_epoch"])
        with open(os.path.join(day, "h1.jsonl"), "w") as fh:
            for r in lines:
                fh.write(json.dumps(r) + "\n")
        # Another host, same pids: never part of an h1 session.
        with open(os.path.join(day, "h2.jsonl"), "w") as fh:
            for r in lines:
                fh.write(json.dumps(dict(r, host="h2")) + "\n")

        self.cfg = configmod.Config(configmod.defaults(), {}, None)
        classes = list(self.cfg.get("filters.classes")) + [self.cfg.get("filters.unlabeled_class")]
        self.view = PanelView(self.cfg, classes, 1440)
        red = self.view.fresh_reducers()
        dispatch = PanelView.build_dispatch(red)
        norm = Normalizer(self.cfg)
        for r in lines:
            n = norm.normalize(json.loads(json.dumps(r)))
            for rd in dispatch.get(n.get("event"), ()):
                rd.feed(n)
        self.view.reducers = red
        self.view.state = "ready"
        self.sid = "h1|%s|agent|apid:%d" % (time.strftime("%Y-%m-%d", time.localtime(t0)), AGENT)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def inspect(self, sid=None, **kw):
        return proctree.inspect(self.cfg, self.view, threading.RLock(), [self.dir],
                                sid or self.sid, **kw)

    def test_the_tree_is_the_rows_records_and_only_those(self):
        body = self.inspect()
        self.assertTrue(body["found"])
        self.assertEqual(body["row"]["n_events"], 12)
        self.assertEqual(body["scan"]["records"], 12, "same processes as the row counts")
        self.assertEqual(body["scan"]["files"], 1, "only the sid's host file is read")
        self.assertEqual(body["scan"]["method"], "key search")
        self.assertEqual(body["scan"]["parsed"], 12,
                         "the session's 12 lines in the span are parsed; the other's never are")
        tree = body["tree"]
        self.assertEqual(tree["pid"], AGENT)
        self.assertEqual(tree["args"], "claude --resume x --dangerously-skip-permissions",
                         "the agent, from its residency tick")
        self.assertIsNone(tree["ev"])
        shell = find(tree, 200)
        self.assertEqual(shell["cmdline"], "git push")
        self.assertEqual(kids(shell)[0]["pid"], 201)
        g = [k for k in kids(tree) if k["k"] == "g"]
        self.assertEqual((g[0]["motif"], g[0]["reps"]), (["pgrep"], 10))
        self.assertIsNone(find(tree, 901))
        self.assertIsNone(body["scan"]["stopped"])

    def test_the_row_takes_its_approval_from_the_agent_not_a_tool_call(self):
        # The tool shell's exit comes first and says `na`; the agent itself is
        # only ever seen in its residency tick, launched with a bypass flag.
        body = self.inspect()
        self.assertEqual(body["row"]["approval"], "bypassed")
        self.assertEqual(body["row"]["autonomous"], 0,
                         "no exit of the agent itself was counted, so no bypassed exit")

    def test_served_text_is_masked(self):
        text = json.dumps(self.inspect())
        self.assertNotIn("Z" * 20, text)

    def test_a_record_budget_stops_the_read_and_says_where(self):
        old = proctree.DEFAULTS["inspect_max_records"]
        proctree.DEFAULTS["inspect_max_records"] = 5
        try:
            body = self.inspect()
        finally:
            proctree.DEFAULTS["inspect_max_records"] = old
        self.assertEqual(body["scan"]["stopped"], "records")
        self.assertEqual(body["scan"]["records"], 5)
        self.assertIsNotNone(body["scan"]["stopped_at"])
        # and the page continues from there
        later = self.inspect(frm=body["scan"]["stopped_at"])
        self.assertEqual(later["scan"]["records"], 12 - 5 + 1,
                         "the record at the cut is read again, never skipped")

    def test_the_rows_keys_are_found_without_folding_the_window(self):
        traj = self.view.reducers["trajectories"]
        traj._fold_cache = (None, None, None)
        got = proctree.lookup(traj, self.sid)
        self.assertIsNone(traj._fold_cache[0], "found without folding every other row")
        want = traj.lookup(self.sid)
        self.assertEqual((got[1], got[2], sorted(got[3])), (want[1], want[2], sorted(want[3])))
        self.assertEqual(len(got[3]), 2, "the agent's key and its tool call's")
        self.assertEqual((got[0].n, got[0].chain, got[0].purpose),
                         (want[0].n, want[0].chain, want[0].purpose))
        self.assertIsNone(proctree.lookup(traj, self.sid[:-3] + "4242"))

    def test_the_payload_carries_the_navigator(self):
        body = self.inspect()
        nav = body["nav"]
        self.assertEqual(nav["starts"], 12)
        self.assertEqual(sum(c for _, _, c in nav["density"]["bins"]), 12)
        self.assertEqual((nav["first_tool"]["pid"], nav["first_tool"]["via"]), (200, "tool"))
        self.assertEqual(nav["failures"], {"n": 0, "listed": 0, "items": []})

    def test_a_built_tree_is_served_again_until_the_window_is_rebuilt(self):
        self.assertFalse(self.inspect()["cached"])
        again = self.inspect()
        self.assertTrue(again["cached"])
        self.assertEqual(again["scan"]["computed_at"], self.inspect()["scan"]["computed_at"])
        self.view.installed_at = (self.view.installed_at or 0) + 1
        self.assertFalse(self.inspect()["cached"], "a rebuilt window is a fresh read")

    def test_the_cache_keeps_the_most_recently_opened(self):
        old = proctree.CACHE_KEEP
        proctree.CACHE_KEEP = 2
        try:
            a, b, c = self.t0 + 1, self.t0 + 2, self.t0 + 3
            for f in (a, b):
                self.inspect(frm=f)
            self.assertTrue(self.inspect(frm=a)["cached"])     # a is now the newest
            self.inspect(frm=c)                                # pushes b out
            self.assertTrue(self.inspect(frm=a)["cached"])
            self.assertFalse(self.inspect(frm=b)["cached"])
        finally:
            proctree.CACHE_KEEP = old

    def test_an_old_tree_is_read_again(self):
        self.inspect()
        self.assertFalse(self.inspect(now=time.time() + proctree.CACHE_TTL + 1)["cached"])

    def test_a_background_read_gives_way_to_a_reader(self):
        proctree._waiting[0] = 1
        try:
            self.assertIsNone(self.inspect(background=True))
        finally:
            proctree._waiting[0] = 0
        self.assertFalse(self.inspect()["cached"], "nothing half-read was kept")
        self.assertTrue(self.inspect(background=True)["cached"])

    def test_an_unknown_session_is_said_so(self):
        body = self.inspect(sid="h1|2026-01-01|agent|apid:4242")
        self.assertIs(body["found"], False)
        self.assertIn("window", body["why"])

    def test_a_window_still_building_is_not_an_empty_session(self):
        self.view.reducers = {}
        body = self.inspect()
        self.assertIsNone(body["found"])


if __name__ == "__main__":
    unittest.main()


class IoTest(unittest.TestCase):
    """Each process's own I/O, and its subtree's, as `sub_cpu` sums CPU."""

    def io(self, r, rd=0.0, wr=0.0, rc=0.0, wc=0.0, tx=None, rx=None):
        r["io"] = {"rd_mb": rd, "wr_mb": wr, "rchar_mb": rc, "wchar_mb": wc}
        if tx is not None:
            r["net_tx_bytes"], r["net_rx_bytes"] = tx, rx or 0
        return r

    def test_own_and_subtree(self):
        _, tree, _ = build([
            self.io(ex(T0 + 1, 201, 200, "cat", "cat a"), rd=1.5, rc=2.0),
            self.io(ex(T0 + 2, 202, 200, "curl", "curl x"), wr=0.5, tx=3_000_000, rx=1_000_000),
            ex(T0 + 2.5, 203, 200, "true"),                 # no io block: unmeasured
            self.io(ex(T0 + 3, 200, AGENT, "bash", "bash -c x", dur=5), rc=0.25),
        ])
        bash = find(tree, 200)
        self.assertEqual(bash["io"]["rchar_mb"], 0.25)
        sub = bash["sub_io"]
        self.assertEqual((sub["rd_mb"], sub["wr_mb"], sub["rchar_mb"]), (1.5, 0.5, 2.25))
        self.assertEqual((sub["tx_mb"], sub["rx_mb"]), (3.0, 1.0))
        self.assertEqual(find(tree, 202)["io"]["tx_mb"], 3.0)
        # nothing moved, or nothing measured: no block at all
        self.assertNotIn("io", find(tree, 203))
        # a leaf's subtree is itself: not repeated
        self.assertNotIn("sub_io", find(tree, 201))

    def test_counted_processes_keep_their_io(self):
        # past the hold budget, processes are counted under their parent; their
        # bytes still reach the parent's subtree total
        recs = [self.io(ex(T0 + i, 300 + i, 200, "cat", "cat f%d" % i), rd=1.0)
                for i in range(1, 40)]
        recs.append(ex(T0 + 50, 200, AGENT, "bash", "bash -c loop", dur=60))
        _, tree, _ = build(recs, hold=8)
        self.assertAlmostEqual(find(tree, 200)["sub_io"]["rd_mb"], 39.0)
