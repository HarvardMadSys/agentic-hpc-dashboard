"""Phases in the trajectories reducer: every agent row carries its tool calls' phases.

Records are built as the collector writes them (`test_sessions.rec`), a child's exit before the
process that waited on it, and reduced through the reducer set a window build uses. Schema 8
names the agent on every record (`agent_pid`) and tags only the runtime; schema 7 tags each tool
shell, which is then its own `agent_pid`, and the fold puts the keys back together.

    cd dashboard && uv run python -m unittest discover -s tests -p 'test_traj_phases.py' -v
"""
import json
import pickle
import random
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_merge as TM                                     # noqa: E402
from rc_dashboard import config as configmod                # noqa: E402
from rc_dashboard import hours, proctree, snapshots         # noqa: E402
from rc_dashboard.normalize import Normalizer               # noqa: E402
from rc_dashboard.reducers import phase_segment             # noqa: E402
from rc_dashboard.reducers import trajectories as trajmod   # noqa: E402
from rc_dashboard.windows import PanelView                  # noqa: E402
from test_sessions import HOME, SNAP, cfg, claude_session, ranked, rec, reduce   # noqa: E402

SNAP8 = SNAP.replace("/bin/zsh -c", "/bin/bash -c")     # schema 8 records the shell as `bash`
UNAVAILABLE = {"unavailable": "phases not available for this agent type"}
CODEX = HOME + "/.codex/bin/codex"
HELPER = CODEX + " --codex-run-as-fs-helper"


def quiet_t0(span):
    """A start such that `[t0, t0 + span]` ends a minute ago and lies in one local day: a row's
    sid carries its day, so a span across midnight would be two rows."""
    end = time.time() - 60
    t0 = end - span
    if time.localtime(t0)[:3] != time.localtime(end)[:3]:
        midnight = time.mktime(time.localtime(end)[:3] + (0, 0, 0, 0, 0, -1))
        t0 = midnight - span - 60
    return t0


T0 = quiet_t0(4400) + 200          # the tests' records lie in [T0 - 200, T0 + 4200]


def root8(t, root=100, command="claude", args="claude", **kw):
    """A tick of the agent runtime: the one tagged process of a schema-8 tree."""
    return rec("residency", t, root, 50, command, args, agent_pid=root, tree_root=root,
               is_agent=True, schema_version=8, depth=0, age_s=60, **kw)


def s8(t, pid, ppid, command, args, root=100, **kw):
    """A schema-8 exit: it names its agent, whatever its depth, and is not tagged."""
    kw.setdefault("depth", 1 if ppid == root else 2)
    return rec("exit", t, pid, ppid, command, args, agent_pid=root, tree_root=root,
               schema_version=8, **kw)


def bash8(t, pid, cmd, root=100, **kw):
    """Claude Code's Bash tool, schema 8: a direct child of the agent."""
    return s8(t, pid, root, "bash", SNAP8 + "'%s'" % cmd, root=root, **kw)


def fed(recs, c=None):
    """The trajectory reducer of a view fed `recs`, normalised and dispatched as a build does
    it, but with nothing swallowed: a record with no time cannot go through `hours.feed_all`."""
    c = c or cfg()
    view = PanelView(c, TM.classes(c), 1440)
    red = view.reducers = view.fresh_reducers()
    dispatch = PanelView.build_dispatch(red)
    norm = Normalizer(c)
    for raw in recs:
        r = norm.normalize(raw)
        if r is not None:
            for x in dispatch.get(r.get("event"), ()):
                x.feed(r)
    return red["trajectories"]


def row_of(payload, pid=100):
    if "trajectories" not in payload:               # a reducer's own result
        payload = {"trajectories": payload}
    rows = [r for r in ranked(payload) if r["agent_pid"] == str(pid)]
    if len(rows) != 1:
        raise AssertionError("%d rows for agent %s: %s" % (len(rows), pid,
                                                             [r["sid"] for r in ranked(payload)]))
    return rows[0]


def kinds(row):
    return [p["kind"] for p in row["phases"]["list"]]


class TrajPhasesTest(unittest.TestCase):
    def test_a_claude_session_has_calls_and_phases(self):
        row = ranked(reduce(claude_session(T0)))[0]
        self.assertEqual(row["n_calls"], 3)                       # two Bash calls and one Grep
        self.assertGreaterEqual(len(row["phases"]["list"]), 1)
        self.assertEqual(row["phases"]["n_calls"], 3)
        self.assertEqual(row["phases"]["basis"],
                         "read from the commands the agent ran and the words it wrote in them")

    def test_schema8_shells_are_calls_and_their_children_are_not(self):
        row = row_of(reduce([
            root8(T0),
            s8(T0 + 10, 202, 201, "git", "git status", depth=2),
            bash8(T0 + 11, 201, "git status"),
        ]))
        self.assertEqual(row["n_calls"], 1)
        self.assertEqual(kinds(row), ["explore"])
        self.assertEqual(row["phases"]["housekeeping"]["n"], 0)       # the git is the call's
        self.assertEqual(row["phases"]["list"][0]["top"], [["git status", 1]])

    def test_a_claude_shell_whose_argv_was_not_kept_is_an_unknown_call(self):      # R64
        # Schema 7 can record a tool shell's exit before its argv (spec finding 7): no argv, or
        # only the shell's options. It still ran for the model and took time, with a `python`
        # under it: one call, read as unknown, and nothing of it is housekeeping.
        for args in ("", "/bin/bash -c -l"):
            with self.subTest(args=args):
                row = row_of(reduce([
                    root8(T0),
                    s8(T0 + 30, 202, 201, "python", "python x.py", depth=2, duration_s=20.0),
                    s8(T0 + 31, 201, 100, "bash", args, duration_s=25.0),
                ]))
                self.assertEqual(row["n_calls"], 1)
                self.assertEqual(kinds(row), ["unknown"])
                self.assertEqual(row["phases"]["housekeeping"]["n"], 0)

    def test_a_grep_under_a_shell_is_part_of_that_call(self):
        row = row_of(reduce([
            root8(T0),
            s8(T0 + 10, 203, 201, "2.1.308-3f6ee6c", "ugrep -n foo src", depth=2),
            bash8(T0 + 11, 201, "grep -n foo src"),
        ]))
        self.assertEqual(row["n_calls"], 1)
        self.assertEqual(row["phases"]["housekeeping"]["n"], 0)

    def test_desktop_housekeeping_is_counted_not_phased(self):
        row = row_of(reduce([
            root8(T0),
            s8(T0 + 10, 401, 100, "git", "git -c core.fsmonitor= status --porcelain"),
            s8(T0 + 12, 402, 100, "realpath", "realpath " + HOME + "/proj"),
        ]))
        self.assertEqual(row["n_calls"], 0)
        hk = row["phases"]["housekeeping"]
        self.assertEqual(hk["n"], 2)
        self.assertEqual(sorted(hk["shapes"]), [["git", 1], ["realpath", 1]])
        self.assertEqual([k for k in kinds(row) if k != "idle"], [])

    def test_codex_bare_commands_are_calls_and_its_chores_are_not(self):
        kw = dict(agent_pid=300, tree_root=300, agent_type="codex")
        row = row_of(reduce([
            rec("residency", T0, 300, 50, "codex", "codex", is_agent=True, age_s=60, **kw),
            rec("exit", T0 + 10, 301, 300, "cat", "cat AGENTS.md", **kw),
            rec("exit", T0 + 11, 302, 300, "git",
                "git -c safe.bareRepository=explicit rev-parse HEAD", **kw),
            rec("exit", T0 + 12, 303, 300, "lsb_release", "/bin/sh /usr/bin/lsb_release -a", **kw),
            # past the idle threshold, so neither call is absorbed into the other
            rec("exit", T0 + 400, 304, 300, "bash", "/usr/bin/bash -lc pytest -q",
                duration_s=5.0, **kw),
        ]), pid=300)
        self.assertEqual(row["n_calls"], 2)
        self.assertEqual(row["phases"]["housekeeping"]["n"], 2)
        self.assertTrue({"explore", "test"} <= set(kinds(row)), kinds(row))

    def codex_helper(self, io):
        """codex-linux-sandbox 310 > bwrap 311 > codex --codex-run-as-fs-helper 312."""
        kw = dict(root=300, agent_type="codex")
        return row_of(reduce([
            root8(T0, root=300, command="codex", args="codex", agent_type="codex"),
            s8(T0 + 10, 312, 311, "codex", HELPER, io=io, depth=3, **kw),
            s8(T0 + 10.2, 311, 310, "bwrap",
               "bwrap --as-pid-1 --new-session --die-with-parent --ro-bind / / -- " + HELPER,
               depth=2, **kw),
            s8(T0 + 10.4, 310, 300, "codex-linux-sandbox",
               HOME + "/.codex/tmp/arg0/codex-arg0x/codex-linux-sandbox --sandbox-policy-cwd "
               + HOME + "/proj --command-cwd " + HOME + "/proj -- " + HELPER, **kw),
        ]), pid=300)

    def test_codex_fs_helper_that_wrote_is_a_seen_edit(self):
        row = self.codex_helper({"wr_mb": 0.012, "scope": "process"})
        self.assertEqual(row["n_calls"], 1)
        self.assertEqual(kinds(row), ["edit"])
        self.assertIn("--codex-run-as-fs-helper", row["phases"]["list"][0]["top"][0][0])
        row = self.codex_helper({"wr_mb": 0.0, "wchar_mb": 0.0, "scope": "process"})
        self.assertEqual(row["n_calls"], 1)
        self.assertEqual(kinds(row), ["unknown"])

    def test_a_loop_whose_shell_never_exits_is_an_open_wait(self):
        recs = [root8(T0 - 100), bash8(T0 - 50, 201, "ls -la")]
        for i in range(10):
            t = T0 + 20 * i
            recs += [s8(t + 0.1, 600 + 2 * i, 777, "2.1.308-3f6ee6c", "ugrep -qE passed f",
                        duration_s=0.1, depth=2),
                     s8(t + 20, 601 + 2 * i, 777, "sleep", "sleep 20", duration_s=20, depth=2)]
        row = row_of(reduce(recs))
        last = row["phases"]["list"][-1]
        self.assertEqual(last["kind"], "wait")
        self.assertTrue(last["open"])
        self.assertEqual(row["n_calls"], 2)          # the ls, and the loop still running

    def test_an_open_call_is_seen_through_its_programs_never_named_by_a_child(self):   # R65
        # A tool shell (777) that never exits loops `pgrep -f train` and `sleep 20` for ten minutes,
        # and from T0 + 300 the agent runs tests beside it. Its first child's text is no command of
        # its own: the wait lists no `top` entry and no "kept running" line for it, and says which
        # programs ran under it. What it waits on is still read from that text (R60).
        recs = [root8(T0 - 100)]
        for i in range(30):
            t = T0 + 20 * i
            recs += [s8(t + 0.1, 600 + 2 * i, 777, "pgrep", "pgrep -f train", duration_s=0.05, depth=2),
                     s8(t + 20, 601 + 2 * i, 777, "sleep", "sleep 20", duration_s=20, depth=2)]
        recs += [bash8(T0 + 300 + 30 * k + 5, 900 + k, "pytest -q tests/test_%d.py" % k, duration_s=5.0)
                 for k in range(6)]
        row = row_of(reduce(sorted(recs, key=lambda r: r["ts_epoch"])))
        wait, test = row["phases"]["list"][0], row["phases"]["list"][-1]
        self.assertEqual((wait["kind"], wait["open"], wait["n"]), ("wait", True, 1))
        self.assertEqual(wait["seen"], ["pgrep", "sleep"])
        self.assertEqual(wait["ran_past"], None)
        self.assertEqual(wait["top"], [])
        self.assertEqual(wait["waits_on"], "process")
        self.assertLess(wait["t1"], T0 + 590)                       # the call runs on past its phase
        self.assertEqual((test["kind"], test["open"], test["seen"]), ("test", False, None))

    def test_seen_names_the_calls_own_programs_most_run_first(self):   # leftovers L3, R72
        # A tool shell (777) loops `sleep 20`, with a `pgrep` every other round, and a login shell it
        # starts runs the profile's helpers. The program list keeps its first five programs, two
        # helpers among them, as loop detection reads them; `seen` leaves the helpers out. What ran
        # most comes first, a tie going to the first seen; and Claude's binary under its version
        # name (`2.1.311`, as its Grep runs it) reads `claude`.
        recs = [root8(T0 - 100)]
        for i in range(30):
            t = T0 + 20 * i
            if i % 2 == 0:
                recs.append(s8(t + 0.1, 600 + 2 * i, 777, "pgrep", "pgrep -f train", duration_s=0.05,
                               depth=2))
            recs.append(s8(t + 20, 601 + 2 * i, 777, "sleep", "sleep 20", duration_s=20, depth=2))
        recs.append(s8(T0 + 1, 710, 777, "2.1.311", "ugrep -G --ignore-files x .", duration_s=0.2,
                       depth=2))
        helpers = (("grepconf.sh", "/bin/sh /usr/libexec/grepconf.sh -c"), ("id", "/usr/bin/id -un"),
                   ("hostname", "/usr/bin/hostname"), ("flatpak", "flatpak --installations"),
                   ("grep", "grep -q /opt/puppetlabs/bin"), ("grepconf.sh", ""), ("id", ""))
        for k, (cmd, argv) in enumerate(helpers):
            t = T0 + 2 + k if k < 2 else T0 + 30 + 0.01 * k
            recs.append(s8(t, 700 + k, 777, cmd, argv, duration_s=0.005, depth=2))
        row = row_of(reduce(sorted(recs, key=lambda r: r["ts_epoch"])))
        wait = row["phases"]["list"][0]
        self.assertEqual((wait["kind"], wait["open"]), ("wait", True))
        self.assertEqual(wait["seen"], ["sleep", "pgrep", "claude"])
        self.assertEqual(wait["waits_on"], "process")

    def test_a_loop_whose_first_programs_are_login_helpers_keeps_its_reading(self):   # R72
        # 1190345 on boslogin06: a tool shell's login shell runs the profile's helpers, puppet's
        # `grep -q /opt/puppetlabs/bin` among them, then conda's hook, before the call's own `until
        # grep ...; do sleep 20; done`. The program list keeps its first five programs, helpers
        # included, so the loop's greps join puppet's `grep` there and read as polling: a wait, as at
        # 9263355. `seen` names the call's own programs alone.
        recs = [root8(T0 - 100)]
        first = (("grepconf.sh", "/bin/sh /usr/libexec/grepconf.sh -c"), ("id", "/usr/bin/id -un"),
                 ("hostname", "/usr/bin/hostname"), ("grep", "grep -q /opt/puppetlabs/bin"),
                 ("sed", "sed -n 1p /h/.condarc"), ("readlink", "readlink -f /h/miniforge3/bin"),
                 ("basename", "basename /h/miniforge3"), ("conda", "conda shell.bash hook"),
                 ("dirname", "dirname /h/miniforge3/bin"))
        for k, (cmd, argv) in enumerate(first):
            recs.append(s8(T0 + 0.01 * k, 700 + k, 777, cmd, argv, duration_s=0.005, depth=2))
        for i in range(1, 31):
            t = T0 + 20 * i
            recs += [s8(t + 0.05, 800 + 2 * i, 777, "grep", "grep -q DONE out.log", duration_s=0.05,
                        depth=2),
                     s8(t + 20.05, 801 + 2 * i, 777, "sleep", "sleep 20", duration_s=20, depth=2)]
        row = row_of(reduce(sorted(recs, key=lambda r: r["ts_epoch"])))
        opened = [p for p in row["phases"]["list"] if p["open"]]
        self.assertEqual([p["kind"] for p in opened], ["wait"])
        self.assertEqual(opened[0]["seen"], ["grep", "sed"])

    def test_a_loop_that_outlived_its_agent_ends_with_its_own_record(self):
        # Schema 8: a Bash tool shell whose agent is gone is its own root (the spec's finding 8,
        # apid 1881673). While it runs its loop is a call still running; its exit ends the call.
        shell = SNAP8 + "'until [ -f out ]; do pgrep -f train; sleep 15; done'"
        kw = dict(agent_pid=500, tree_root=500, is_agent=True, schema_version=8, depth=0)
        recs = [rec("residency", T0, 500, 1, "bash", shell, age_s=600, **kw)]
        for i in range(12):
            t = T0 + 15 * i
            recs += [s8(t + 0.1, 600 + 2 * i, 500, "pgrep", "pgrep -f train", root=500,
                        duration_s=0.05),
                     s8(t + 15, 601 + 2 * i, 500, "sleep", "sleep 15", root=500, duration_s=15)]
        running = row_of(reduce(recs), 500)
        self.assertEqual(kinds(running), ["wait"])
        self.assertTrue(running["phases"]["list"][0]["open"])
        payload = reduce(recs + [rec("exit", T0 + 181, 500, 1, "bash", shell, duration_s=600,
                                     **kw)])
        done = row_of(payload, 500)
        self.assertEqual(kinds(done), ["wait"])
        self.assertFalse(done["phases"]["list"][0]["open"])
        self.assertEqual(done["n_calls"], 1)
        self.assertEqual(done["phases"]["housekeeping"]["n"], 0)
        self.assertEqual(payload["trajectories"]["buffer"]["phase_reparented"], 0)
        self.assertEqual(payload["trajectories"]["buffer"]["phase_pending"], 0)

    def test_a_background_job_is_its_calls_ran_past(self):          # Review Focus 1
        row = row_of(reduce([
            root8(T0),
            bash8(T0 + 5, 201, "nohup python train.py &"),
            s8(T0 + 3600, 202, 201, "python", "python train.py", duration_s=3595.5, depth=2),
        ]))
        self.assertEqual(row["n_calls"], 1)
        run = [p for p in row["phases"]["list"] if p["kind"] == "run"]
        self.assertEqual(len(run), 1)
        (text, secs), = run[0]["ran_past"]
        self.assertEqual(text, "nohup python train.py &")
        self.assertAlmostEqual(secs, 3595, delta=1)
        self.assertFalse(any(p["open"] for p in row["phases"]["list"]))

    def test_a_call_the_agent_moved_past_holds_no_idle_and_the_gap_names_it(self):
        # moved past (2026-11-02 ruling): linktest runs from T0 to T0 + 4000, and the agent made
        # two calls while it ran. The quiet between them is idle, and the row says what still ran.
        row = row_of(reduce([
            root8(T0),
            bash8(T0 + 4000, 201, "linktest", duration_s=4000.0),
            bash8(T0 + 61, 202, "ls a", duration_s=1.0),
            bash8(T0 + 2001, 203, "ls b", duration_s=1.0),
        ]))
        self.assertEqual(kinds(row), ["run", "idle", "explore"])
        idle = row["phases"]["list"][1]
        self.assertAlmostEqual(idle["t1"] - idle["t0"], 1939, delta=0.01)
        self.assertEqual([[text, round(since - T0)] for text, since in idle["alive"]],
                         [["linktest", 0]])
        self.assertEqual([p["alive"] for p in row["phases"]["list"] if p["kind"] != "idle"],
                         [None, None])

    def test_a_helper_that_never_exits_does_not_hide_idle(self):    # Review Focus 2, R37
        # The helper's last child is long before the session's last record, or (R37) within
        # idle_s of it but sparse: either way what it ran is no call, and the gap stays idle.
        for last_git in (1500, 1950):
            with self.subTest(last_git=last_git):
                recs = [root8(T0)]
                for i in range(6):
                    recs.append(bash8(T0 + 10 * i, 201 + i, "ls src%d" % i))
                recs += [s8(T0 + 100, 401, 300, "git", "git status", depth=2),
                         s8(T0 + last_git, 402, 300, "git", "git status", depth=2)]
                for i in range(6):
                    recs.append(bash8(T0 + 2000 + 10 * i, 501 + i, "ls src%d" % i))
                recs.sort(key=lambda r: r["ts_epoch"])
                row = row_of(reduce(recs))
                ks = kinds(row)
                self.assertIn("idle", ks)
                i = ks.index("idle")
                self.assertTrue(0 < i < len(ks) - 1, ks)
                idle = row["phases"]["list"][i]
                self.assertLessEqual(idle["t0"], T0 + 51)
                self.assertGreaterEqual(idle["t1"], T0 + 1999)
                self.assertFalse(any(p["open"] for p in row["phases"]["list"]))
                hk = row["phases"]["housekeeping"]
                self.assertEqual(hk["n"], 2)
                self.assertEqual(hk["shapes"], [["under a process that never exited", 2]])
                self.assertEqual(hk["never_exited"], 2)                 # R43: counted apart, too
                self.assertEqual(row["n_calls"], 12)

    def test_a_sandbox_that_runs_true_is_codex_testing_it_not_a_call(self):
        # boslogin06: codex-linux-sandbox > bwrap > /usr/bin/true, 805 times on 2026-10-28
        kw = dict(root=300, agent_type="codex")
        sandbox = HOME + "/.codex/tmp/arg0/codex-arg0x/codex-linux-sandbox --sandbox-policy-cwd " + HOME
        recs = [root8(T0, root=300, command="codex", args="codex", agent_type="codex")]
        for i, payload in enumerate(("/usr/bin/true", "/bin/bash -lc pytest -q")):
            base, t = 310 + 10 * i, T0 + 10 + 100 * i
            recs += [s8(t, base + 2, base + 1, payload.split()[0].rsplit("/", 1)[1], payload, depth=3, **kw),
                     s8(t + 0.1, base + 1, base, "bwrap", "bwrap --ro-bind / / -- " + payload, depth=2, **kw),
                     s8(t + 0.2, base, 300, "codex-linux-sandbox", sandbox, **kw)]
        row = row_of(reduce(recs), 300)
        self.assertEqual(row["n_calls"], 1)                                     # the pytest
        self.assertEqual(kinds(row), ["test"])
        self.assertEqual(row["phases"]["housekeeping"]["shapes"], [["sandbox probe", 3]])

    def test_a_sandboxed_payload_is_the_call_named_for_its_command(self):       # R33
        # Codex's main sandboxed path, 932 wrapper calls on boslogin06: codex-linux-sandbox runs bwrap,
        # which runs the model's command. The wrappers have no text of their own, so the call is the
        # payload's, read as a shell's command string: one test call, `pytest -q`.
        kw = dict(root=300, agent_type="codex")
        sandbox = (HOME + "/.codex/tmp/arg0/codex-arg0x/codex-linux-sandbox --sandbox-policy-cwd "
                   + HOME + " --command-cwd " + HOME + "/proj")
        payload = "/bin/bash -lc pytest -q"
        row = row_of(reduce([
            root8(T0, root=300, command="codex", args="codex", agent_type="codex"),
            s8(T0 + 10, 312, 311, "bash", payload, depth=3, duration_s=8.0, **kw),
            s8(T0 + 10.1, 311, 310, "bwrap", "bwrap --ro-bind / / --dev /dev -- " + payload,
               depth=2, duration_s=8.2, **kw),
            s8(T0 + 10.2, 310, 300, "codex-linux-sandbox", sandbox, duration_s=8.4, **kw),
        ]), 300)
        self.assertEqual(row["n_calls"], 1)
        (p,) = row["phases"]["list"]
        self.assertEqual((p["kind"], p["n"], p["top"]), ("test", 1, [["pytest -q", 1]]))
        self.assertEqual(row["phases"]["housekeeping"]["n"], 0)

    def test_a_codex_chore_under_a_process_that_never_exits_is_no_call(self):
        # Codex's update check (`sh -s`, with what its script runs) on a timer, under a server that
        # never exits: steady enough to be a loop, but Codex's own (boslogin06 apid 1447670 read it
        # as a call running for 23 h). A loop of the model's under a Codex call stays an open wait.
        kw = dict(root=300, agent_type="codex")
        recs = [root8(T0 - 100, root=300, command="codex", args="codex", agent_type="codex"),
                s8(T0 - 50, 305, 300, "bash", "/bin/bash -lc ls -la", **kw)]
        for i in range(6):
            t = T0 + 120 * i
            recs += [s8(t + 0.2, 330 + 2 * i, 320 + 2 * i, "uname", "uname -m", depth=3, **kw),
                     s8(t + 0.4, 320 + 2 * i, 310, "sh", "/bin/sh -s", depth=2, **kw)]
        recs.sort(key=lambda r: r["ts_epoch"])
        row = row_of(reduce(recs), 300)
        self.assertEqual(row["n_calls"], 1)
        self.assertFalse(any(p["open"] for p in row["phases"]["list"]))
        hk = row["phases"]["housekeeping"]
        self.assertEqual((hk["n"], hk["never_exited"]), (12, 12))
        # the model's loop under a Codex shell call that has not returned
        loop = [s8(T0 + 20 * i + 20, 401 + i, 400, "sleep", "sleep 20", duration_s=20, depth=2, **kw)
                for i in range(8)]
        row = row_of(reduce(sorted(recs + loop, key=lambda r: r["ts_epoch"])), 300)
        self.assertEqual(row["n_calls"], 2)
        self.assertEqual([p["kind"] for p in row["phases"]["list"] if p["open"]], ["wait"])

    def test_a_child_that_outlives_its_pipeline_stage_goes_where_the_stage_went(self):   # R57
        # A Bash call (201) runs a pipeline in a subshell (210) whose two `grep` stages exit after
        # it. Pushed under 210 they would start an entry nobody pops, dense at the edge: a call
        # "still running" in a session that has ended. They go where 210 went, into 201's entry,
        # and once 201 has ended too, into 201's call as having run past it.
        for late_end in (T0 + 10.3, T0 + 50.0):
            with self.subTest(late_end=late_end):
                recs = [root8(T0), bash8(T0 + 1, 199, "ls"),
                        s8(T0 + 10.0, 210, 201, "bash", SNAP8 + "'a | grep x | head -1'", depth=2,
                           duration_s=9.0),
                        bash8(T0 + 11.0, 201, "a | grep x | head -1", duration_s=10.5)]
                recs += [s8(late_end + 0.1 * i, 211 + i, 210, "grep", "grep x", depth=3,
                            duration_s=late_end - (T0 + 9.5)) for i in range(2)]
                payload = reduce(sorted(recs, key=lambda r: r["ts_epoch"]))
                row = row_of(payload)
                buf = payload["trajectories"]["buffer"]
                self.assertEqual(row["n_calls"], 2)
                self.assertFalse(any(p["open"] for p in row["phases"]["list"]))
                self.assertEqual((buf["phase_late_routed"], buf["phase_pending"]), (2, 0))
                if late_end > T0 + 11:
                    ran = [r for p in row["phases"]["list"] for r in (p["ran_past"] or [])]
                    self.assertEqual(ran, [["a | grep x | head -1", round(late_end + 0.1 - (T0 + 11.0), 1)]])

    def test_a_helper_that_bursts_now_and_then_is_no_call_running(self):   # R57
        # A process that never exits runs a burst of ten `git` every 1000 s, the last at the
        # session's end: its bursts are no call running for the hour between them, nor its last
        # burst a call still running, and the idle stays idle.
        recs = [root8(T0), bash8(T0 + 5, 201, "ls"), bash8(T0 + 3000.5, 202, "ls src")]
        for b in range(4):
            recs += [s8(T0 + 1000 * b + 0.1 * i, 401 + 10 * b + i, 400, "git", "git status", depth=2)
                     for i in range(10)]
        row = row_of(reduce(sorted(recs, key=lambda r: r["ts_epoch"])))
        self.assertEqual(row["n_calls"], 2)
        self.assertFalse(any(p["open"] for p in row["phases"]["list"]))
        self.assertIn("idle", kinds(row))
        self.assertEqual(row["phases"]["housekeeping"]["never_exited"], 40)

    def test_a_timer_whose_short_children_leave_idle_between_them_is_no_call(self):   # R57
        # A process that never exits runs `curl` every 1000 s, a second each: steady enough to be a
        # loop, but nothing ran between its children, so it is no call running all along.
        recs = [root8(T0), bash8(T0 + 5, 201, "ls"), bash8(T0 + 4001, 202, "ls src")]
        recs += [s8(T0 + 1000 * k, 401 + k, 400, "curl", "curl -s https://x/update", depth=2,
                    duration_s=1.0) for k in range(5)]
        row = row_of(reduce(sorted(recs, key=lambda r: r["ts_epoch"])))
        self.assertEqual(row["n_calls"], 2)
        self.assertFalse(any(p["open"] for p in row["phases"]["list"]))
        self.assertEqual(row["phases"]["housekeeping"]["never_exited"], 5)

    def test_a_burst_of_different_programs_at_the_edge_is_no_call_running(self):   # R57
        # boslogin06's Codex launchers: a never-exiting process ran a handful of programs once each in
        # a few seconds at the end of its row. No command ran twice: it is not dense (R37).
        recs = [root8(T0), bash8(T0 + 5, 201, "ls")]
        recs += [s8(T0 + 100 + k, 401 + k, 400, cmd, cmd + " x", depth=2)
                 for k, cmd in enumerate(("pkill", "loginctl", "node", "mkdir", "head"))]
        row = row_of(reduce(sorted(recs, key=lambda r: r["ts_epoch"])))
        self.assertFalse(any(p["open"] for p in row["phases"]["list"]))
        self.assertEqual(row["phases"]["housekeeping"]["never_exited"], 5)

    def test_a_burst_within_one_step_is_no_call_running(self):   # R57
        # Three subshells in the same second under a process whose record never came, at the end of a
        # row (boslogin06: 3902885, 837618, jingyuanhu's probes): a moment, not a call still running.
        recs = [root8(T0), bash8(T0 + 5, 201, "ls")]
        recs += [s8(T0 + 100 + 0.2 * k, 401 + k, 400, "bash", "bash", depth=2) for k in range(3)]
        row = row_of(reduce(sorted(recs, key=lambda r: r["ts_epoch"])))
        self.assertFalse(any(p["open"] for p in row["phases"]["list"]))
        self.assertEqual(row["phases"]["housekeeping"]["never_exited"], 3)

    def test_a_call_busy_again_for_idle_s_after_a_pause_is_still_running(self):   # R57
        # A call's children, 10 and 40 s apart in turn (no steady pace), stop for six minutes, then
        # run again until the edge for six more: it is open over that last stretch.
        recs = [root8(T0), bash8(T0 + 1, 199, "ls")]
        t, pid = T0 + 10, 300
        for stop in (T0 + 600, T0 + 1340):
            gap = 10.0
            while t < stop:
                recs.append(s8(t, pid, 250, "python", "python step.py", depth=2, duration_s=2.0))
                t, pid, gap = t + gap, pid + 1, 50.0 - gap
            t = T0 + 960
        row = row_of(reduce(sorted(recs, key=lambda r: r["ts_epoch"])))
        opened = [p for p in row["phases"]["list"] if p["open"]]
        self.assertEqual(len(opened), 1)
        self.assertGreaterEqual(opened[0]["t0"], T0 + 900)          # the stretch, not the pause
        self.assertGreaterEqual(opened[0]["t1"], T0 + 1300)

    def test_a_secret_is_masked_in_previews(self):                  # Review Focus 4
        secret = "abcdef0123456789abcdef"
        row = row_of(reduce([
            root8(T0),
            bash8(T0 + 5, 201, 'curl -H "Authorization: Bearer %s" https://x/api' % secret),
        ]))
        blob = json.dumps(row["phases"])
        self.assertNotIn(secret, blob)
        self.assertIn("[REDACTED]", blob)

    def test_schema7_and_8_shells_in_one_session(self):             # Review Focus 5
        recs = [rec("residency", T0, 100, 50, "claude", "claude", agent_pid=100, tree_root=100,
                    is_agent=True, age_s=60)]
        for i, pid in enumerate((200, 210)):                # schema 7: each shell its own key
            recs += [rec("exit", T0 + 10 * i + 1, pid + 1, pid, "ls", "ls", agent_pid=pid,
                         tree_root=100),
                     rec("exit", T0 + 10 * i + 2, pid, 100, "zsh", SNAP + "'ls'", agent_pid=pid,
                         tree_root=100, is_agent=True)]
        for i, pid in enumerate((220, 230)):                # schema 8: the agent's key
            recs += [s8(T0 + 30 + 10 * i, pid + 1, pid, "git", "git status", depth=2),
                     bash8(T0 + 31 + 10 * i, pid, "git status")]
        row = row_of(reduce(recs))
        self.assertEqual(row["n_calls"], 4)
        self.assertEqual(row["fragments"], 3)

    def test_an_unsupported_agent_says_so(self):
        kw = dict(agent_pid=100, tree_root=100, agent_type="cursor")
        row = row_of(reduce([
            rec("residency", T0, 100, 50, "cursor-agent", "cursor-agent", is_agent=True,
                age_s=60, **kw),
            rec("exit", T0 + 5, 201, 100, "bash", "/bin/bash -c ls", **kw),
        ]))
        self.assertEqual(row["phases"], UNAVAILABLE)
        self.assertIsNone(row["n_calls"])         # not recognized is not none: null, not zero

    def test_a_person_has_no_phases(self):
        payload = reduce([rec("exit", T0 + 5, 901, 900, "ls", "ls -la", agent_pid=None,
                              tree_root=None, actor="human", actor3="human", agent_type=None,
                              tty="pts/3", user="bob")])
        row, = [r for r in ranked(payload) if r["actor3"] == "human"]
        self.assertEqual(row["phases"], UNAVAILABLE)
        self.assertIsNone(row["n_calls"])

    def test_a_call_still_running_at_the_edge_is_open(self):         # R37
        # Test runs one after another under a shell that has not exited: only its children are
        # recorded. Busy at the session's edge, and dense, it is a call still running.
        row = row_of(reduce([
            root8(T0),
            bash8(T0 + 5, 201, "ls"),
            s8(T0 + 100, 301, 300, "python", "python -m pytest -q tests/a.py", duration_s=60,
               depth=2),
            s8(T0 + 101, 302, 300, "tail", "tail -n 50", duration_s=61, depth=2),
            s8(T0 + 160, 303, 300, "python", "python -m pytest -q tests/b.py", duration_s=59,
               depth=2),
        ]))
        last = row["phases"]["list"][-1]
        self.assertTrue(last["open"])
        self.assertEqual(last["kind"], "test")       # the highest of what ran under it
        self.assertEqual(row["phases"]["housekeeping"]["n"], 0)

    def test_a_sparse_process_at_the_edge_is_not_a_call(self):       # R37
        # One child, busy at the edge but never twice: nothing says a call is still running.
        row = row_of(reduce([
            root8(T0),
            bash8(T0 + 5, 201, "ls"),
            s8(T0 + 100, 301, 300, "python", "python -m pytest -q", duration_s=60, depth=2),
        ]))
        self.assertFalse(any(p["open"] for p in row["phases"]["list"]))
        self.assertEqual(row["n_calls"], 1)
        self.assertEqual(row["phases"]["housekeeping"]["shapes"],
                         [["under a process that never exited", 1]])

    def test_a_root_that_is_no_tool_call_ends_at_its_own_record(self):    # R36
        # Schema 8, each its own root and exiting under ppid 50: the daemon and the Remote
        # Control server (infra), and a version probe. None is a call, and nothing is left
        # reading as still running.
        for agent_type, command, args, role in (
                ("claude_code", "claude", "claude daemon run --origin transient", "infra"),
                ("claude_code", "claude", "claude rc", "infra"),
                ("codex", "codex", "codex --version", "probe")):
            with self.subTest(args=args):
                kw = dict(agent_pid=900, tree_root=900, is_agent=True, schema_version=8,
                          depth=0, agent_type=agent_type)
                recs = [rec("residency", T0, 900, 50, command, args, age_s=60, **kw),
                        rec("exit", T0 + 30, 900, 50, command, args, duration_s=40, **kw)]
                self.assertEqual(Normalizer(cfg()).normalize(dict(recs[1]))["_role"], role)
                payload = reduce(recs)
                row = row_of(payload, 900)
                self.assertEqual(row["n_calls"], 0)
                self.assertEqual(row["phases"]["list"], [])
                self.assertEqual(payload["trajectories"]["buffer"]["phase_pending"], 0)

    def test_what_an_exiting_root_held_is_its_housekeeping(self):         # R36
        # Schema 7: the desktop app's server (infra) tops its tree. Only an agent's children
        # are read as direct, so its chores wait under it, and its exit counts them as its
        # housekeeping, by program and in no minute.
        srv = (HOME + "/.claude/remote/srv/89cb/server --serve --socket " + HOME
               + "/.claude/remote/run/c0e8/rpc.sock")
        kw = dict(agent_pid=800, tree_root=800)
        recs = [rec("residency", T0, 800, 1, "server", srv, is_agent=True, age_s=60, **kw)]
        recs += [rec("exit", T0 + 10 + 400 * i, 810 + i, 800, "git", "git status --porcelain",
                     **kw) for i in range(3)]
        recs.append(rec("exit", T0 + 1300, 800, 1, "server", srv, is_agent=True,
                        duration_s=1400, **kw))
        payload = reduce(recs)
        row = row_of(payload, 800)
        self.assertEqual(row["n_calls"], 0)
        self.assertEqual(row["phases"]["list"], [])
        hk = row["phases"]["housekeeping"]
        self.assertEqual((hk["n"], hk["shapes"], hk["cut"]), (3, [["git", 3]], 3))
        buf = payload["trajectories"]["buffer"]
        self.assertEqual((buf["phase_pending"], buf["phase_reparented"], buf["phase_unread"]),
                         (0, 0, 3))

    def test_a_schema7_loop_that_tops_its_tree_ends_with_its_own_record(self):   # R36
        # Through schema 7 a tool shell is always its own agent_pid; one that is also its tree's
        # root outlived its agent, and its exit is a call, as schema 8's orphan is.
        shell = SNAP + "'until [ -f out ]; do sleep 15; done'"
        kw = dict(agent_pid=500, tree_root=500)
        recs = [rec("residency", T0, 500, 1, "zsh", shell, is_agent=True, age_s=600, **kw)]
        recs += [rec("exit", T0 + 15 * (i + 1), 600 + i, 500, "sleep", "sleep 15",
                     duration_s=15, **kw) for i in range(12)]
        self.assertTrue(row_of(reduce(recs), 500)["phases"]["list"][-1]["open"])
        done = row_of(reduce(recs + [rec("exit", T0 + 181, 500, 1, "zsh", shell, is_agent=True,
                                          duration_s=600, **kw)]), 500)
        self.assertEqual(kinds(done), ["wait"])
        self.assertFalse(done["phases"]["list"][0]["open"])
        self.assertEqual(done["n_calls"], 1)
        self.assertEqual(done["phases"]["housekeeping"]["n"], 0)

    def test_a_record_with_no_time_is_counted_but_not_placed(self):       # R18
        def no_time(r):
            for k in ("ts", "ts_epoch", "start"):
                r.pop(k, None)
            return r

        traj = fed([root8(T0), bash8(T0 + 5, 201, "ls"),
                    no_time(bash8(T0 + 6, 202, "ls")),                             # a call
                    no_time(s8(T0 + 7, 301, 201, "sleep", "sleep 1", depth=2)),    # past its call
                    no_time(s8(T0 + 8, 401, 400, "sleep", "sleep 1", depth=2))])   # under none
        timeless = [s for s in traj.sessions.values() if s.no_ts]
        self.assertEqual([(s.n, s.phase.calls, s.phase.n) for s in timeless], [(3, [], 0)])
        self.assertEqual(traj.pend.entries, {})
        for s in traj.sessions.values():
            for c in s.phase.calls:
                self.assertIsNotNone(c[0])
                self.assertIsNotNone(c[1])
        self.assertEqual(row_of(traj.result())["n_calls"], 1)

    def test_a_reparented_process_is_never_pushed(self):                  # R18
        traj = fed([root8(T0), s8(T0 + 5, 202, 1, "python", "python train.py", depth=2),
                    s8(T0 + 6, 203, 0, "sleep", "sleep 1", depth=2)])
        self.assertEqual(traj.pend.entries, {})
        self.assertEqual(traj.result()["buffer"]["phase_reparented"], 2)

    def test_housekeeping_shapes_are_bounded(self):                 # R20
        recs = [root8(T0)] + [s8(T0 + 1 + i, 1000 + i, 100, "tool%d" % i, "tool%d -x" % i)
                              for i in range(60)]
        hk = row_of(reduce(recs))["phases"]["housekeeping"]
        self.assertEqual(hk["n"], 60)
        self.assertIn(["other", 10], hk["shapes"])

    def test_previews_past_the_cap_are_counted(self):
        with mock.patch.object(trajmod, "MAX_PREVIEWS", 3):          # 0 is none: two texts fit
            payload = reduce([root8(T0)] + [bash8(T0 + 5 * i, 201 + i, "ls dir%d" % i)
                                            for i in range(4)])
        row = row_of(payload)
        self.assertEqual(row["n_calls"], 4)
        tops = [t for p in row["phases"]["list"] for t, _ in p["top"]]
        self.assertEqual(sorted(tops), ["ls dir0", "ls dir1"])
        self.assertEqual(payload["trajectories"]["buffer"]["preview_overflow"], 2)

    def test_the_configured_thresholds_are_the_segmenters_defaults(self):     # R2
        self.assertEqual(configmod.defaults()["reducers"]["trajectories"]["phases"],
                         phase_segment.DEFAULTS)

    def test_the_preview_bounds_are_the_inspectors(self):
        self.assertEqual((trajmod.PREVIEW, trajmod.RAW_KEEP),
                         (proctree.PREVIEW, proctree.RAW_KEEP))

    def test_what_an_open_call_was_seen_through_is_named_as_the_census_names(self):   # R65
        from rc_dashboard.reducers import live
        self.assertEqual((trajmod.SEEN_SHOWN, trajmod.SEEN_NAME),
                         (live.CENSUS_SHOWN, live.NAME_SHOWN))


def opened(row):
    """A row's open phases, as (kind, start, end) in seconds from T0."""
    return [(p["kind"], round(p["t0"] - T0), round(p["t1"] - T0))
            for p in row["phases"]["list"] if p["open"]]


def ran(row):
    return [r for p in row["phases"]["list"] for r in (p["ran_past"] or [])]


def buffer_of(payload, *keys):
    buf = payload["trajectories"]["buffer"] if "trajectories" in payload else payload["buffer"]
    return tuple(buf[k] for k in keys)


def pipeline_a(late_child=True):
    """Session A (agent 100): Bash call 201 runs a pipeline in subshell 210, which ends at T0 + 10
    into 201's entry; 201 ends at T0 + 11. Its `grep` stage, 211, began at T0 + 5 and ends at
    T0 + 3000."""
    recs = [root8(T0 - 60), s8(T0 + 10, 210, 201, "bash", SNAP8 + "'a | grep x'", depth=2,
                               duration_s=9.0),
            bash8(T0 + 11, 201, "a | grep x", duration_s=10.0)]
    if late_child:
        recs.append(s8(T0 + 3000, 211, 210, "grep", "grep x", depth=3, duration_s=2995.0))
    return recs


def poll_b(t0=T0 + 2800, t1=T0 + 3100):
    """Session B (agent 500): Bash call 400 never exits, and polls with `sleep 20`."""
    recs, t, pid = [root8(T0 - 60, root=500)], t0, 600
    while t < t1:
        recs.append(s8(t + 20, pid, 400, "sleep", "sleep 20", root=500, depth=2, duration_s=20.0))
        t, pid = t + 20, pid + 1
    return recs


def by_time(recs):
    return sorted(recs, key=lambda r: r["ts_epoch"])


class LateChildTest(unittest.TestCase):
    """R57: a child recorded after its parent goes where the parent went. A pid is reused, so a
    note is the parent's only when the child began while the noted process ran (fix round 1)."""

    def test_a_child_of_an_earlier_process_with_the_pid_never_follows_a_later_one(self):
        # The review's case (r57_reuse.py): a child of B's call takes pid 210 at T0 + 2949 and
        # ends at T0 + 2950. A's 211 must not follow it into B's call, where it would stretch B's
        # open wait back to T0 + 5. It goes nowhere, counted, as its own parent's note is gone.
        alone = reduce(by_time(pipeline_a() + poll_b()))
        self.assertEqual(ran(row_of(alone)), [["a | grep x", 2989.0]])
        self.assertEqual(opened(row_of(alone, 500)), [("wait", 2800, 3100)])
        self.assertEqual(buffer_of(alone, "phase_late_routed", "phase_late_unplaced"), (1, 0))
        reused = reduce(by_time(pipeline_a() + poll_b() + [
            s8(T0 + 2950, 210, 400, "cat", "cat out.txt", root=500, depth=2, duration_s=1.0)]))
        self.assertEqual(ran(row_of(reused)), [])
        self.assertEqual(opened(row_of(reused, 500)), [("wait", 2800, 3100)])
        self.assertEqual(buffer_of(reused, "phase_late_routed", "phase_late_unplaced"), (0, 1))

    def test_a_child_of_a_later_process_with_the_pid_is_pushed_under_it(self):
        # B's call 400 runs a process that has pid 210 again, from T0 + 500, and that never exits:
        # its loop's children began long after A's 210 ended, and are its own, not A's call's.
        recs = pipeline_a(late_child=False) + [root8(T0 - 60, root=500)]
        recs += [s8(T0 + 520 + 20 * k, 700 + k, 210, "sleep", "sleep 20", root=500, depth=3,
                    duration_s=20.0) for k in range(10)]
        payload = reduce(by_time(recs))
        self.assertEqual(ran(row_of(payload)), [])
        self.assertEqual(opened(row_of(payload, 500)), [("wait", 500, 700)])
        self.assertEqual(buffer_of(payload, "phase_late_routed", "phase_late_unplaced",
                                   "phase_pending"), (0, 0, 1))

    def test_a_parent_whose_pid_a_later_process_took_is_no_way_up(self):
        # 210 went into call 201's entry, and 201 ended; then a child of B's call took pid 201 and
        # ended. Climbing from 210 to that note would take 211 into B's call.
        payload = reduce(by_time(pipeline_a() + poll_b() + [
            s8(T0 + 2950, 201, 400, "cat", "cat out.txt", root=500, depth=2, duration_s=1.0)]))
        self.assertEqual(opened(row_of(payload, 500)), [("wait", 2800, 3100)])
        self.assertEqual(ran(row_of(payload)), [])
        self.assertEqual(buffer_of(payload, "phase_late_routed", "phase_late_unplaced"), (0, 1))

    def test_a_parent_note_of_an_earlier_process_with_its_pid_stops_the_walk(self):
        # A's call 300 ended at T0 + 20. B's call 400 then runs a process with pid 300 again, from
        # T0 + 100 and still running, under which 310 runs a pipeline whose stage 311 ends after
        # 310. 311 goes where 310 went, the live 300's entry, never into A's call 300.
        recs = [root8(T0 - 60), root8(T0 - 60, root=500), bash8(T0 + 20, 300, "ls", duration_s=1.0),
                s8(T0 + 120, 310, 300, "bash", "bash -c 'a | grep x'", root=500, depth=3,
                   duration_s=10.0),
                s8(T0 + 200, 311, 310, "grep", "grep x", root=500, depth=4, duration_s=85.0)]
        payload = reduce(by_time(recs))
        self.assertEqual(ran(row_of(payload)), [])
        self.assertEqual(row_of(payload, 500)["phases"]["housekeeping"]["never_exited"], 2)
        self.assertEqual(buffer_of(payload, "phase_late_routed", "phase_late_unplaced",
                                   "phase_pending"), (1, 0, 1))

    def test_a_late_child_of_a_chore_is_that_chore(self):
        hook = "python3 " + HOME + "/.claude/hooks/policy.py"
        payload = reduce([root8(T0), s8(T0 + 10, 401, 100, "python3", hook, duration_s=5.0),
                          s8(T0 + 60, 402, 401, "git", "git status", depth=2, duration_s=52.0)])
        self.assertEqual(row_of(payload)["phases"]["housekeeping"]["shapes"], [["python3", 2]])
        self.assertEqual(buffer_of(payload, "phase_late_routed", "phase_pending"), (1, 0))

    def test_a_late_child_of_a_root_is_what_the_root_held(self):
        srv = (HOME + "/.claude/remote/srv/89cb/server --serve --socket " + HOME
               + "/.claude/remote/run/c0e8/rpc.sock")
        kw = dict(agent_pid=800, tree_root=800)
        payload = reduce([
            rec("residency", T0, 800, 1, "server", srv, is_agent=True, age_s=60, **kw),
            rec("exit", T0 + 10, 810, 800, "git", "git status --porcelain", **kw),
            rec("exit", T0 + 1300, 800, 1, "server", srv, is_agent=True, duration_s=1400, **kw),
            rec("exit", T0 + 1320, 811, 800, "git", "git fetch", duration_s=30.0, **kw)])
        self.assertEqual(row_of(payload, 800)["phases"]["housekeeping"]["shapes"], [["git", 2]])
        self.assertEqual(buffer_of(payload, "phase_unread", "phase_late_routed", "phase_pending"),
                         (1, 1, 0))

    def test_a_late_child_of_a_reparented_process_is_counted_once(self):
        traj = fed([root8(T0), s8(T0 + 10, 501, 1, "python", "python job.py", depth=2,
                                  duration_s=8.0),
                    s8(T0 + 40, 502, 501, "sleep", "sleep 35", depth=3, duration_s=35.0)])
        self.assertEqual(buffer_of(traj.result(), "phase_reparented", "phase_late_routed",
                                   "phase_pending"), (2, 0, 0))

    def test_a_late_child_of_an_agent_whose_calls_are_not_read_is_counted(self):
        kw = dict(root=900, agent_type="copilot")
        traj = fed([root8(T0, root=900, command="copilot", args="copilot", agent_type="copilot"),
                    s8(T0 + 10, 901, 900, "bash", "bash -c 'a | grep x'", duration_s=9.0, **kw),
                    s8(T0 + 60, 902, 901, "grep", "grep x", depth=2, duration_s=55.0, **kw)])
        self.assertEqual(buffer_of(traj.result(), "phase_late_unplaced", "phase_late_routed",
                                   "phase_pending"), (1, 0, 0))

    def test_an_entry_an_earlier_process_left_under_a_reused_pid_is_refused_and_counted(self):
        # Final Important 5. Two children of a process 300 whose own record never came wait under
        # its pid; eight minutes later a Bash call that got pid 300 exits. Their entry began before
        # it could have: it is refused, as before, and now counted, with the records it held.
        payload = reduce([root8(T0 - 60),
                          s8(T0 + 10, 301, 300, "ls", "ls", depth=2),
                          s8(T0 + 11, 302, 300, "cat", "cat f", depth=2),
                          bash8(T0 + 500, 300, "pwd", duration_s=1.0)])
        self.assertEqual(row_of(payload)["n_calls"], 1)
        self.assertEqual(buffer_of(payload, "phase_pending_refused", "phase_pending_refused_records",
                                   "phase_pending"), (1, 2, 0))

    def test_a_late_child_with_no_start_is_pushed_and_counted(self):
        late = s8(T0 + 60, 211, 210, "grep", "grep x", depth=3)
        late.pop("duration_s")
        traj = fed(pipeline_a(late_child=False) + [late])
        self.assertEqual(buffer_of(traj.result(), "phase_late_no_start", "phase_late_routed",
                                   "phase_pending"), (1, 0, 1))

    def test_what_ran_under_a_record_with_no_time_is_counted(self):
        def no_time(r):
            for k in ("ts", "ts_epoch", "start"):
                r.pop(k, None)
            return r

        traj = fed([root8(T0), s8(T0 + 8, 212, 210, "ls", "ls", depth=3, duration_s=1.0),
                    no_time(s8(T0 + 10, 210, 201, "bash", SNAP8 + "'ls; grep x f'", depth=2,
                               duration_s=9.0)),
                    bash8(T0 + 11, 201, "ls; grep x f", duration_s=10.0),
                    s8(T0 + 60, 211, 210, "grep", "grep x f", depth=3, duration_s=55.0)])
        self.assertEqual(buffer_of(traj.result(), "phase_untimed", "phase_late_routed",
                                   "phase_pending"), (2, 0, 0))

    def test_a_child_whose_parents_note_was_dropped_is_pushed_and_counted(self):
        # 210's note is dropped before its stage 211 ends: 211 starts an entry under it that may
        # never pop, counted as one. Its sibling 212 joins that entry, and is not counted again.
        # Nor is 702, begun as early, under 700, which still runs and has an entry already: its
        # exit would have taken the entry.
        recs = pipeline_a(late_child=False) + [bash8(T0 + 20 + i, 220 + i, "ls %d" % i)
                                                for i in range(4)]
        recs += [s8(T0 + 100, 211, 210, "grep", "grep x", depth=3, duration_s=95.0),
                 s8(T0 + 101, 212, 210, "head", "head -1", depth=3, duration_s=96.0),
                 s8(T0 + 300, 701, 700, "ls", "ls", depth=3, duration_s=1.0),
                 s8(T0 + 310, 702, 700, "python", "python job.py", depth=3, duration_s=260.0)]
        with mock.patch.object(trajmod, "EXITED_MAX", 3):
            traj = fed(by_time(recs))
        self.assertEqual(buffer_of(traj.result(), "phase_exited", "phase_exited_evicted",
                                   "phase_late_maybe_evicted", "phase_late_routed",
                                   "phase_pending"), (3, 7, 1, 0, 2))
        self.assertEqual(traj.exited_horizon, T0 + 100)

    def test_a_walk_past_late_hops_is_pushed_and_counted(self):
        # 230 ran under 220 under 210 under call 201, each recorded before its parent; 230's child
        # 231 ends after them all. With one hop it stops at 210's entry, which never pops.
        recs = [root8(T0 - 60),
                s8(T0 + 5, 230, 220, "bash", "bash -c x", depth=4, duration_s=2.0),
                s8(T0 + 6, 220, 210, "bash", "bash -c x", depth=3, duration_s=4.0),
                s8(T0 + 7, 210, 201, "bash", "bash -c x", depth=2, duration_s=6.0),
                bash8(T0 + 8, 201, "x", duration_s=7.5),
                s8(T0 + 100, 231, 230, "grep", "grep x", depth=5, duration_s=97.0)]
        with mock.patch.object(trajmod, "LATE_HOPS", 1):
            payload = reduce(recs)
        self.assertEqual(buffer_of(payload, "phase_late_hops_out", "phase_late_routed",
                                   "phase_pending"), (1, 0, 1))
        payload = reduce(recs)                              # at LATE_HOPS, up to the call
        self.assertEqual(buffer_of(payload, "phase_late_hops_out", "phase_late_routed",
                                   "phase_pending"), (0, 1, 0))
        self.assertEqual(ran(row_of(payload)), [["x", 92.0]])


def session(rec_):
    return trajmod.Session(Normalizer(cfg()).normalize(rec_))


class LifeCycleTest(unittest.TestCase):
    def test_a_merge_keeps_the_housekeeping_bounds(self):          # R20, R21
        a, b = session(bash8(T0, 201, "ls")), session(bash8(T0, 201, "ls"))
        for i in range(40):
            a.phase.add_housekeeping("a%d" % i, T0 + 60 * i, 10080)
            b.phase.add_housekeeping("b%d" % i, T0 + 3600 + 60 * i, 10080)
        minutes = sorted(set(a.phase.hk_bins) | set(b.phase.hk_bins))
        trajmod.merge_session(a, b, dict(phase_segment.DEFAULTS, hk_bin_cap=50))
        hk = a.phase
        self.assertEqual([k for k in hk.hk if k != "other"],
                         ["a%d" % i for i in range(40)] + ["b%d" % i for i in range(10)])
        self.assertEqual(hk.hk["other"], 30)
        self.assertEqual(sorted(hk.hk_bins), minutes[:50])            # the oldest minutes kept
        self.assertEqual(hk.hk_cut, 30)
        self.assertEqual(sum(hk.hk_bins.values()) + hk.hk_cut, sum(hk.hk.values()))

    def test_a_fold_keeps_the_bin_bound(self):                     # R21
        parts = [session(bash8(T0, 201, "ls")), session(bash8(T0, 202, "ls"))]
        for k, p in enumerate(parts):
            for i in range(30):
                p.phase.add_housekeeping("git", T0 + 3600 * k + 60 * i, 10080)
        m = trajmod.fold(parts, parts[0].sid, "100", dict(phase_segment.DEFAULTS, hk_bin_cap=40))
        self.assertEqual(len(m.phase.hk_bins), 40)
        self.assertEqual(m.phase.hk_cut, 20)
        self.assertIsNot(m.phase, parts[0].phase)
        self.assertEqual(len(parts[0].phase.hk_bins), 30)           # the parts are untouched

    def test_the_inspector_row_is_the_list_row(self):              # R31
        c = cfg()
        view = PanelView(c, TM.classes(c), 1440)
        red = view.reducers = view.fresh_reducers()
        norm = Normalizer(c)

        def feed(recs):
            d = PanelView.build_dispatch(red)
            for r in recs:
                for x in d.get(r["event"], ()):
                    x.feed(norm.normalize(r))

        feed(claude_session(T0))
        traj = red["trajectories"]
        sid = ranked({"trajectories": traj.result()})[0]["sid"]
        # A tool shell still running (schema 7: its own key) whose loop's children exit after
        # the list was folded: the fold cache is stale. The inspector's lookup folds this row's
        # keys alone (`proctree.lookup`), and building its row then folds the window again.
        feed([rec("residency", T0 + 40, 500, 100, "zsh", SNAP + "'until test -f x; do sleep 5; "
                  "done'", agent_pid=500, tree_root=100, is_agent=True, age_s=5)])
        feed([rec("exit", T0 + 41 + 5 * i, 600 + i, 500, "sleep", "sleep 5", agent_pid=500,
                  tree_root=100, duration_s=5) for i in range(6)])
        hit = proctree.lookup(traj, sid)
        self.assertIn(sid[:sid.rfind(":") + 1] + "500", hit[3])
        # not passing by accident: the stale fold's members would miss the loop. It is first,
        # since a row reads the current fold for the days of its agent, and that renews the fold.
        stale = traj._row(*hit[:3])
        self.assertFalse(stale["phases"]["list"][-1]["open"])
        inspector = traj._row(*hit)
        traj._phase_cache.clear()                   # the list's row is built afresh
        listed = traj.result()["rows"][sid]
        self.assertTrue(listed["phases"]["list"][-1]["open"])
        self.assertEqual(inspector["phases"], listed["phases"])
        self.assertEqual(inspector["n_calls"], listed["n_calls"])

    def test_a_loop_past_midnight_is_the_new_days_and_yesterdays_row_is_read_again(self):   # R59
        # A tool shell (777) that never exits polls with `sleep 20` from ten minutes before a
        # local midnight to ten after. Its children are the old day's row's, then the new day's:
        # the pending entry is filed under its newest child's, so the loop is the new day's, one
        # open wait from its stretch's start in the block both rows serve, and the old day's row,
        # though no record of it came, drops the copy it had cached.
        midnight = time.mktime(time.localtime(time.time() - 7200)[:3] + (0, 0, 0, 0, 0, -1))
        loop = [s8(midnight - 600 + 20 * k, 600 + k, 777, "sleep", "sleep 20", depth=2,
                   duration_s=20.0) for k in range(60)]
        before = [r for r in loop if r["ts_epoch"] < midnight]
        after = [r for r in loop if r["ts_epoch"] >= midnight]
        c = cfg()
        view = PanelView(c, TM.classes(c), 1440)
        red = view.reducers = view.fresh_reducers()
        dispatch, norm = PanelView.build_dispatch(red), Normalizer(c)

        def feed(recs):
            for r in recs:
                for x in dispatch.get(r["event"], ()):
                    x.feed(norm.normalize(r))

        def day_rows(now):
            rows = traj.result(now)["rows"]
            return {sid.split("|")[1]: row for sid, row in rows.items() if sid.endswith(":100")}

        feed([root8(midnight - 900), bash8(midnight - 500, 201, "ls")] + before)
        traj = red["trajectories"]
        old = time.strftime("%Y-%m-%d", time.localtime(midnight - 1))
        new = time.strftime("%Y-%m-%d", time.localtime(midnight + 1))
        rows = day_rows(midnight - 5)
        self.assertEqual(list(rows), [old])
        self.assertEqual([p["kind"] for p in rows[old]["phases"]["list"] if p["open"]], ["wait"])
        sid = rows[old]["sid"]
        key = traj._phase_cache[sid][0]
        feed([root8(midnight + 5)] + after + [bash8(midnight + 630, 202, "ls")])
        rows = day_rows(midnight + 650)
        self.assertEqual(sorted(rows), sorted([old, new]))
        self.assertEqual(rows[old]["phases"], rows[new]["phases"])
        # each row counts its own calls, and the loop, a call still running, is the new day's
        self.assertEqual([rows[old]["n_calls"], rows[new]["n_calls"],
                          rows[new]["phases"]["n_calls"]], [1, 2, 3])
        waits = [p for p in rows[new]["phases"]["list"] if p["open"]]
        self.assertEqual([p["kind"] for p in waits], ["wait"])
        self.assertEqual(waits[0]["t0"], round(midnight - 620, 3))          # its stretch's start
        # not passing by accident: no record of the old day's came, and its own part of the key
        # (the first of the parts the cache holds the block under) is as it was but for its
        # entries: its sid, records, calls, and keys are the same, and the loop's entry has gone
        # to the new day
        was, now = key[0][0], traj._phase_cache[sid][0][0][0]
        self.assertEqual(now[:4], was[:4])
        self.assertNotEqual(now, was)
        self.assertEqual((was[4], now[4]), (1, 0))

    def test_phases_survive_a_snapshot_round_trip(self):
        recs = [root8(T0)]
        recs += [bash8(T0 + 10 * i, 201 + i, "ls src%d" % i) for i in range(6)]
        recs += [s8(T0 + 100, 401, 300, "git", "git status", depth=2),
                 s8(T0 + 900, 701, 700, "sleep", "sleep 1", depth=2)]
        recs += [bash8(T0 + 2000 + 10 * i, 501 + i, "pytest -q") for i in range(3)]
        recs.sort(key=lambda r: r["ts_epoch"])
        c = cfg()
        view = PanelView(c, TM.classes(c), 1440)
        view.reducers = view.fresh_reducers()
        TM.feed(view.reducers, [Normalizer(c).normalize(r) for r in recs])
        before = view.reducers["trajectories"].result()["rows"]
        state = pickle.loads(snapshots.capture(view.reducers))
        for skipped in ("phase_params", "task_params", "_phase_cache", "_day_idx"):
            self.assertNotIn(skipped, state["trajectories"])
        after = snapshots.restore(view.fresh_reducers(), state)["trajectories"].result()["rows"]
        self.assertEqual(set(after), set(before))
        for sid in before:
            self.assertEqual(after[sid]["phases"], before[sid]["phases"])
            self.assertEqual(after[sid], before[sid])
        sid, = [s for s, r in before.items() if r["agent_pid"] == "100"]
        self.assertIn("idle", [p["kind"] for p in before[sid]["phases"]["list"]])
        self.assertEqual(before[sid]["phases"]["housekeeping"]["n"], 2)      # held in `pend`
        # restored under another config, the saved calls are read with its thresholds
        tree = configmod.defaults()
        tree["reducers"]["trajectories"]["phases"]["idle_s"] = 100000
        tree["reducers"]["trajectories"]["tasks"]["gap_s"] = 100
        c2 = configmod.Config(tree, {}, None)
        red2 = snapshots.restore(PanelView(c2, TM.classes(c2), 1440).fresh_reducers(),
                                 pickle.loads(snapshots.capture(view.reducers)))
        traj2 = red2["trajectories"]
        self.assertEqual(traj2.phase_params["idle_s"], 100000)
        self.assertNotIn("idle", [p["kind"] for p in traj2.result()["rows"][sid]["phases"]["list"]])
        # the tasks too: the half hour between the reads and the tests is a gap of 100 s or more
        self.assertEqual(traj2.task_params["gap_s"], 100)
        self.assertEqual(len(before[sid]["phases"]["tasks"]), 1)
        self.assertEqual(len(traj2.result()["rows"][sid]["phases"]["tasks"]), 2)


# ------------------------------------------------------------------ merging hours
HOUR = TM.HOUR


def seam(span):
    """An epoch hour B with `[B - span, B + span]` in one local day, inside the day before
    TM.NOW: the stream around it is one row per agent."""
    for k in range(2, 22):
        b = TM.NOW - k * HOUR
        if time.localtime(b - span)[:3] == time.localtime(b + span)[:3]:
            return b
    raise AssertionError("no hour boundary away from midnight")


def merge_stream(b):
    """Two agents around the hour boundary `b`, with every join the phases make crossing it."""
    out = []
    for k in range(0, 10800, 600):                     # the runtimes' ticks
        out.append(root8(b - 5400 + k))
        out.append(root8(b - 5400 + k + 1, root=300, command="codex", args="codex",
                         agent_type="codex"))
    cmds = ["ls -la src", "pytest -q tests/test_io.py", "sed -i s/a/b/ src/io.py", "git status",
            "rg -n TODO src", "git commit -am wip"]
    rng = random.Random(29)            # an agent's own pace: never regular, so never polling
    pid, t = 1000, b - 5000
    while t < b + 5000:                                # tool calls, the command under the shell
        c = rng.choice(cmds)
        out += [s8(t, pid + 1, pid, c.split()[0], c, depth=2), bash8(t + 0.4, pid, c)]
        pid += 2
        t += rng.choice((3, 8, 15, 30, 45, 90, 140))
        if b + 2000 < t < b + 2200:
            t += 900                                   # the user's turn
    # the command exits before the hour turns, its shell after
    out += [s8(b - 0.5, 5001, 5000, "make", "make all", duration_s=30, depth=2),
            bash8(b + 0.5, 5000, "make all", duration_s=31.5)]
    # a job started in the background in one hour ends in the next
    out += [bash8(b - 600, 6000, "nohup python train.py &"),
            s8(b + 1800, 6001, 6000, "python", "python train.py", duration_s=2400.4, depth=2)]
    # a loop across the seam under a shell that never exits
    for k in range(90):
        tt = b - 900 + 20 * k
        out += [s8(tt + 0.1, 7000 + 2 * k, 777, "2.1.308-3f6ee6c",
                   "ugrep -qE passed|failed out.log", duration_s=0.1, depth=2),
                s8(tt + 20, 7001 + 2 * k, 777, "sleep", "sleep 20", duration_s=20, depth=2)]
    for k in range(0, 10000, 300):                     # the desktop app's checks
        out.append(s8(b - 5000 + k, 20000 + k, 100, "realpath", "realpath " + HOME + "/proj"))
    # a helper that never exits, with children on both sides
    out += [s8(b - 1200, 9001, 9000, "git", "git status", depth=2),
            s8(b + 1200, 9002, 9000, "git", "git status", depth=2)]
    # Codex's fs helper writes before the hour turns; its wrappers exit after
    kw = dict(root=300, agent_type="codex")
    out += [s8(b - 2.0, 312, 311, "codex", HELPER, io={"wr_mb": 0.01, "scope": "process"},
               depth=3, **kw),
            s8(b + 1.0, 311, 310, "bwrap", "bwrap --as-pid-1 --die-with-parent -- " + HELPER,
               depth=2, **kw),
            s8(b + 2.0, 310, 300, "codex-linux-sandbox", HOME + "/.codex/tmp/arg0/"
               "codex-linux-sandbox --sandbox-policy-cwd " + HOME + " -- " + HELPER, **kw),
            s8(b + 900, 320, 300, "bash", "/usr/bin/bash -lc pytest -q", duration_s=20, **kw)]
    out.sort(key=lambda r: r["ts_epoch"])
    return out


class MergeHoursTest(unittest.TestCase):
    def test_phases_survive_merging_hours(self):
        raw = merge_stream(seam(5400))
        one = TM.materialize(TM.one_pass(TM.normalized(raw)))
        saved, carry, fresh = TM.hour_states(TM.normalized(raw))
        self.assertGreaterEqual(len(saved), 3)
        got = TM.materialize(TM.merged(saved, fresh, carry))
        for pid in (100, 300):
            with self.subTest(agent=pid):
                a, b = row_of(one, pid), row_of(got, pid)
                self.assertEqual(a["phases"]["list"], b["phases"]["list"])
                self.assertEqual(a["phases"], b["phases"])
                self.assertEqual(a["n_calls"], b["n_calls"])
        self.assertEqual(TM.diff(one["trajectories"], got["trajectories"]), [])
        # not passing by accident: each join the hours carry is in it
        a = row_of(one)
        past = [r for p in a["phases"]["list"] for r in (p["ran_past"] or ())]
        self.assertIn("nohup python train.py &", [t for t, _ in past])
        self.assertTrue(any(p["open"] for p in a["phases"]["list"]))
        self.assertEqual(a["phases"]["housekeeping"]["shapes"][0][0], "realpath")
        self.assertIn(["under a process that never exited", 2],
                      a["phases"]["housekeeping"]["shapes"])
        codex = row_of(one, 300)
        self.assertEqual(codex["n_calls"], 2)
        self.assertIn("edit", kinds(codex))

    def test_a_late_child_across_the_seam_is_its_calls(self):
        # Without the carried call index the job's end, in the next hour, would be read as a
        # call under a process that never exited.
        b = seam(5400)
        raw = [root8(b - 1000), bash8(b - 600, 6000, "nohup python train.py &"),
               s8(b + 1800, 6001, 6000, "python", "python train.py", duration_s=2400.4, depth=2),
               bash8(b + 1900, 6100, "ls")]
        saved, carry, fresh = TM.hour_states(TM.normalized(raw))
        got = row_of(TM.materialize(TM.merged(saved, fresh, carry)))
        past = [r for p in got["phases"]["list"] for r in (p["ran_past"] or ())]
        self.assertEqual([t for t, _ in past], ["nohup python train.py &"])
        self.assertAlmostEqual(past[0][1], 2400, delta=1)
        self.assertEqual(got["phases"]["housekeeping"]["n"], 0)
        self.assertEqual(got["n_calls"], 2)

    def test_a_late_end_waits_for_the_merge_that_brings_its_call_in(self):
        # A saved day is merged from its own hours first: there the job's end, two hours after
        # its call, finds no call to extend, and must keep until the window brings the call in.
        b = seam(5400)
        raw = [root8(b - 1000), bash8(b - 600, 6000, "nohup python train.py &"),
               bash8(b + 600, 6100, "ls"),
               s8(b + 5000, 6001, 6000, "python", "python train.py", duration_s=5600.4, depth=2)]
        saved, carry, fresh = TM.hour_states(TM.normalized(raw))
        self.assertEqual(len(saved), 3)

        def part(blob):
            return snapshots.restore(fresh(), pickle.loads(blob))

        day = fresh()
        for r in day.values():                      # as `HourStore.load_part` builds a day
            if hasattr(r, "bin_s"):
                r.bin_s = hours.HOUR_BIN_S
        hours.merge_into(day, part(saved[1]))
        hours.merge_into(day, part(saved[2]))
        kept = [s.past_due for s in day["trajectories"].sessions.values() if s.past_due]
        self.assertEqual(len(kept), 1)
        c = cfg()
        view = PanelView(c, TM.classes(c), 1440)
        acc = view.reducers = view.fresh_reducers()
        hours.merge_into(acc, part(saved[0]))
        hours.merge_into(acc, day)
        hours.install(acc, carry)
        got = row_of(TM.materialize(view))
        want = row_of(TM.materialize(TM.one_pass(TM.normalized(raw))))
        self.assertEqual(got["phases"], want["phases"])
        past = [r for p in got["phases"]["list"] for r in (p["ran_past"] or ())]
        self.assertEqual([t for t, _ in past], ["nohup python train.py &"])
        self.assertAlmostEqual(past[0][1], 5600, delta=1)

    def test_past_due_is_bounded_and_says_so(self):                     # R38
        # Five jobs end in the hour after their calls; a session holds three such ends. The
        # oldest two noted are dropped, counted, and their calls keep no `past`.
        b = seam(5400)
        raw = [root8(b - 1000)]
        raw += [bash8(b - 600 + 10 * i, 6000 + 10 * i, "nohup python job%d.py &" % i)
                for i in range(5)]
        raw += [s8(b + 1800 + 10 * i, 6001 + 10 * i, 6000 + 10 * i, "python",
                   "python job%d.py" % i, duration_s=2400.4, depth=2) for i in range(5)]
        with mock.patch.object(trajmod, "PAST_DUE_MAX", 3):
            saved, carry, fresh = TM.hour_states(TM.normalized(raw))
            got = TM.materialize(TM.merged(saved, fresh, carry))
        self.assertEqual(got["trajectories"]["buffer"]["phase_past_due_evicted"], 2)
        past = [t for p in row_of(got)["phases"]["list"] for t, _ in (p["ran_past"] or ())]
        self.assertEqual(sorted(past), ["nohup python job%d.py &" % i for i in (2, 3, 4)])
        one = TM.materialize(TM.one_pass(TM.normalized(raw)))
        self.assertEqual(one["trajectories"]["buffer"]["phase_past_due_evicted"], 0)
        self.assertEqual(len([t for p in row_of(one)["phases"]["list"]
                              for t, _ in (p["ran_past"] or ())]), 5)


if __name__ == "__main__":
    unittest.main()
