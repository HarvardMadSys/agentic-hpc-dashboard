"""Sandbox and approval: what each verdict is read from, and at which grain.

Pins the fixes from the 2026-10-11 audit of rtx6000a's capture:

* a session's approval comes from its ROOT's own record -- never from whichever
  record of the session happened to arrive first (children always exit first);
* only an agent-role process gets an approval verdict; a tool shell, a probe and
  a human process are `na`, an untagged child is `unknown`;
* `--permission-mode bypassPermissions` is a bypass everywhere, `autonomous`
  included, and `--permission-mode auto` / `codex app-server` are their own states;
* a session's sandbox verdict is its TOOL CALLS' outcome, and a wrapper that
  exited non-zero with no payload is a FAILED attempt, not "never tried".

The record shapes are the collector's, several copied from real trees.

    cd dashboard && uv run python -m unittest discover -s tests -q
"""
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_sessions as TS                         # noqa: E402
from rc_dashboard.normalize import (Normalizer, approval_of, bypass_flag_of,   # noqa: E402
                                    sandbox_of)
from rc_dashboard.reducers.live import LiveReducer  # noqa: E402
from rc_dashboard.reducers.sandbox import session_state  # noqa: E402

rec, reduce = TS.rec, TS.reduce
HOME, SNAP = TS.HOME, TS.SNAP
VSCODE_CLAUDE = (HOME + "/.vscode-server/extensions/anthropic.claude-code-2.1.285-linux-x64/"
                 "resources/native-binary/claude --output-format stream-json "
                 "--permission-prompt-tool stdio --allow-dangerously-skip-permissions ")
CODEX_APP = HOME + "/.codex/packages/standalone/releases/0.162.1/codex app-server"


def bypassed_claude_session(t0, root=100):
    """A VS Code-launched Claude in bypassPermissions: one tool call, one git the
    agent runs itself, and the root's own exit last -- the collector's order."""
    return [
        rec("exit", t0 + 5, 150, root, "git", "git status", agent_pid=root, tree_root=root),
        rec("exit", t0 + 9, 201, 200, "ls", "ls -la", agent_pid=200, tree_root=root),
        rec("exit", t0 + 10, 200, root, "zsh", SNAP + "'ls -la'",
            agent_pid=200, tree_root=root, is_agent=True),
        rec("exit", t0 + 60, root, 50, "claude", VSCODE_CLAUDE + "--permission-mode bypassPermissions",
            agent_pid=root, tree_root=root, is_agent=True),
    ]


def sessions_of(sb):
    return {(r["host"], r["pid"]): r for r in sb["sessions"]}


class ApprovalGrainTest(unittest.TestCase):
    def setUp(self):
        self.t0 = time.time() - 3600

    def test_session_approval_does_not_depend_on_exit_order(self):
        """Children exit first, so first-record-wins made this `unknown`; the
        root arriving first made it `bypassed`. Both orders must agree."""
        recs = bypassed_claude_session(self.t0)
        children_first = reduce(recs)["sandbox"]
        root_first = reduce([recs[-1]] + recs[:-1])["sandbox"]
        self.assertEqual(children_first["approval_by_class_sessions"]["agent"], {"bypassed": 1})
        self.assertEqual(root_first["approval_by_class_sessions"]["agent"], {"bypassed": 1})

    def test_a_running_session_is_seen_through_its_roots_conn_record(self):
        """No root exit in the window: its own `conn` identity block carries the
        launch flags. A residency tick of a VS Code tree names the VS Code server,
        which must not stand in for the session."""
        recs = bypassed_claude_session(self.t0)[:-1]
        recs.insert(0, rec("residency", self.t0, 40, 1, "code-a44adf7f53",
                           HOME + "/.vscode-server/cli/servers/Stable-x/server/node",
                           agent_type="vscode", agent_pid=40, tree_root=40, is_agent=True))
        recs.insert(1, rec("conn", self.t0 + 1, 100, 50, "claude",
                           VSCODE_CLAUDE + "--permission-mode bypassPermissions",
                           agent_pid=100, tree_root=40, is_agent=True))
        sb = reduce(recs)["sandbox"]
        self.assertEqual(sb["approval_by_class_sessions"]["agent"], {"bypassed": 1})
        # tick records are identity only: they never enter the per-event counts
        self.assertEqual(sb["n_events"]["agent"], 3)

    def test_children_and_tool_shells_never_claim_supervised(self):
        sb = reduce(bypassed_claude_session(self.t0))["sandbox"]
        self.assertEqual(sb["approval_by_class_events"]["agent"],
                         {"unknown": 2, "na": 1, "bypassed": 1})

    def test_probes_are_not_sessions(self):
        """A `codex --version` poll every few minutes was 3,858 "supervised agent
        trees" in one day on rtx6000a."""
        recs = [rec("exit", self.t0 + i, 500 + i, 1, "codex", HOME + "/.codex/bin/codex --version",
                    agent_type="codex", agent_pid=500 + i, tree_root=500 + i, is_agent=True)
                for i in range(5)]
        sb = reduce(recs)["sandbox"]
        self.assertEqual(sb["n_sessions"], {})
        self.assertEqual(sb["approval_by_class_events"]["agent"], {"na": 5})


class ApprovalStatesTest(unittest.TestCase):
    def ap(self, args, role="agent", agent_type="claude_code", **kw):
        r = dict({"args": args, "agent_type": agent_type}, **kw)
        return approval_of(r, role, "agent")[0]

    def test_launch_states(self):
        self.assertEqual(self.ap("claude --permission-mode bypassPermissions"), "bypassed")
        self.assertEqual(self.ap("claude --permission-mode=bypassPermissions"), "bypassed")
        self.assertEqual(self.ap("claude --dangerously-skip-permissions"), "bypassed")
        self.assertEqual(self.ap("claude --permission-mode auto"), "auto")
        self.assertEqual(self.ap("claude --permission-mode acceptEdits"), "supervised")
        self.assertEqual(self.ap("claude"), "supervised")
        self.assertEqual(self.ap(CODEX_APP, agent_type="codex"), "unobservable")
        self.assertEqual(self.ap("codex -a never", agent_type="codex"), "bypassed")
        self.assertEqual(self.ap("codex --ask-for-approval=never", agent_type="codex"), "bypassed")
        self.assertEqual(self.ap("codex -c approval_policy=\"never\"", agent_type="codex"),
                         "bypassed")
        self.assertEqual(self.ap("codex --sandbox danger-full-access", agent_type="codex"),
                         "bypassed")

    def test_allow_dangerously_is_not_a_bypass(self):
        """It only lets the user switch to bypass later; 30+ roots on rtx6000a
        carried it with `--permission-mode auto`."""
        self.assertEqual(self.ap(VSCODE_CLAUDE + "--permission-mode auto"), "auto")
        self.assertIsNone(bypass_flag_of(VSCODE_CLAUDE))

    def test_flags_inside_a_tool_calls_text_are_not_launch_flags(self):
        self.assertEqual(self.ap('codex exec "grep -r danger-full-access docs"',
                                 agent_type="codex"), "supervised")
        self.assertEqual(self.ap("zsh -c --dangerously-skip-permissions", role="tool-shell"), "na")

    def test_the_collectors_reading_of_the_uncapped_argv_still_counts(self):
        self.assertEqual(self.ap("claude", approval_mode="bypassed"), "bypassed")

    def test_who_gets_no_verdict(self):
        self.assertEqual(approval_of({"args": "git"}, None, "agent")[0], "unknown")
        self.assertEqual(approval_of({"args": "vim"}, None, "human")[0], "na")
        self.assertEqual(approval_of({"args": "node x"}, "ide", "ide")[0], "na")
        # a Cursor / Windsurf tree is filed under `ide`: its processes are never
        # a session, even one roles.py would call `agent`
        self.assertEqual(approval_of({"args": "cursor-agent --force", "agent_type": "cursor"},
                                     "agent", "ide")[0], "na")

    def test_autonomous_follows_approval(self):
        """The collector's `autonomous` missed bypassPermissions: 0 true records
        in 28 days that held 30k bypassed ones."""
        n = Normalizer(TS.cfg())
        r = n.normalize(rec("exit", time.time(), 9, 1, "claude",
                            "claude --permission-mode bypassPermissions",
                            agent_pid=9, tree_root=9, is_agent=True, autonomous=False))
        self.assertTrue(r["_autonomous"])
        self.assertTrue(n.is_autonomous("claude --permission-mode bypassPermissions"))
        self.assertFalse(n.is_autonomous("claude --permission-mode auto"))


class LegacySchemaTest(unittest.TestCase):
    def test_argv_intent_is_never_a_verdict(self):
        """All 612 codex-linux-sandbox runs on rtx6000a exited 1 with no payload:
        asking for a sandbox is not being in one."""
        r = {"args": "codex --sandbox workspace-write"}
        self.assertEqual(sandbox_of(r, 4, "agent"), ("unknown", "argv-intent"))
        self.assertEqual(sandbox_of({"args": "bash -c codex-linux-sandbox"}, 4, None),
                         ("unknown", None))
        self.assertEqual(sandbox_of({"args": "codex --sandbox danger-full-access"}, 4, "agent"),
                         ("unknown", "argv-intent"))

    def test_schema5_unreadable_stays_its_own_state(self):
        self.assertEqual(sandbox_of({"sandbox": None, "sandbox_src": "denied"}, 5)[0], "denied")
        self.assertEqual(sandbox_of({"sandbox": None, "sandbox_src": None}, 5)[0], "unknown")


def codex_failed_call(t0, root=1000, base=2000):
    """One sandboxed Codex call exactly as rtx6000a records it: codex-linux-sandbox
    probes bwrap (a test run, `--help`), then runs the real bwrap; every bwrap
    that tries to unshare exits 1 and no payload ever runs."""
    c, probe, real = base, base + 1, base + 6
    kw = dict(tree_root=root, agent_type="codex")
    return [
        rec("exit", t0 + 0.001, base + 2, probe, "bwrap", "bwrap --help", agent_pid=base + 2,
            is_agent=True, exit_code=0, **dict(kw, agent_type="bwrap")),
        rec("exit", t0 + 0.002, base + 3, probe, "bwrap", "bwrap --unshare-user --ro-bind / / true",
            agent_pid=probe, exit_code=1, **dict(kw, agent_type="bwrap")),
        rec("exit", t0 + 0.003, probe, c, "bwrap", "bwrap --unshare-user --ro-bind / / true",
            agent_pid=probe, is_agent=True, exit_code=1, **dict(kw, agent_type="bwrap")),
        rec("exit", t0 + 0.004, base + 4, c, "bwrap", "bwrap --help", agent_pid=base + 4,
            is_agent=True, exit_code=0, **dict(kw, agent_type="bwrap")),
        rec("exit", t0 + 0.006, base + 7, real, "bwrap", "bwrap --unshare-user --bind /w /w -- "
            "/x/.codex/tmp/arg0/codex-linux-sandbox", agent_pid=real, exit_code=1, **kw),
        rec("exit", t0 + 0.007, real, c, "bwrap", "bwrap --unshare-user --bind /w /w -- "
            "/x/.codex/tmp/arg0/codex-linux-sandbox", agent_pid=real, is_agent=True,
            exit_code=1, **kw),
        rec("exit", t0 + 0.013, c, root, "codex-linux-sandbox",
            HOME + "/.codex/tmp/arg0/codex-linux-sandbox --sandbox-policy-cwd /w --command-cwd /w "
            "-- bash -c 'rg x'", agent_pid=c, is_agent=True, exit_code=1, **kw),
    ]


def bwrap_call(t0, root, base, isolated=True, sandbox_src="userns:exec"):
    """A Claude Bash call under bwrap: launcher (read before it unshares), the
    tool shell as its payload, and the command the shell ran."""
    sb = "sandboxed" if isolated else "unsandboxed"
    return [
        rec("exit", t0 + 0.5, base + 2, base + 1, "make", "make", agent_pid=base + 1,
            tree_root=root, sandbox=sb, sandbox_src="inherit"),
        rec("exit", t0 + 0.6, base + 1, base, "zsh", SNAP + "'make'", agent_pid=base + 1,
            tree_root=root, is_agent=True, sandbox=sb, sandbox_src=sandbox_src),
        rec("exit", t0 + 0.7, base, root, "bwrap", "bwrap --unshare-net --ro-bind / / -- /bin/zsh",
            agent_type="bwrap", agent_pid=base, tree_root=root, is_agent=True, exit_code=0,
            sandbox="unsandboxed", sandbox_src="none:exec"),
    ]


def root_tick(t0, root, args="claude", agent_type="claude_code"):
    return rec("conn", t0, root, 1, args.split()[0].split("/")[-1], args, agent_type=agent_type,
               agent_pid=root, tree_root=root, is_agent=True)


class ToolCallTest(unittest.TestCase):
    def setUp(self):
        self.t0 = time.time() - 3600

    def test_failed_attempt_is_its_own_state(self):
        recs = [root_tick(self.t0, 1000, CODEX_APP, "codex")] + codex_failed_call(self.t0 + 1)
        sb = reduce(recs)["sandbox"]
        self.assertEqual(sb["tool_calls"]["agent"], {"failed": 1})
        self.assertEqual(sb["by_class_sessions"]["agent"], {"failed": 1})
        self.assertEqual(sb["approval_by_class_sessions"]["agent"], {"unobservable": 1})
        self.assertEqual(sb["attempts"], [{"wrapper": "codex-linux-sandbox",
                                           "agent_type": "codex", "outcome": "failed", "n": 1}])

    def test_every_call_confined_is_a_sandboxed_session(self):
        """The owner itself is outside its sandbox by design and reads
        unsandboxed; that must not decide the session."""
        recs = [root_tick(self.t0, 100)]
        for k in range(5):
            recs += bwrap_call(self.t0 + 10 * k, 100, 300 + 10 * k)
        recs.append(rec("exit", self.t0 + 99, 100, 1, "claude", "claude", agent_pid=100,
                        tree_root=100, is_agent=True, sandbox_src="none:tick"))
        sb = reduce(recs)["sandbox"]
        self.assertEqual(sb["tool_calls"]["agent"], {"isolated": 5})
        self.assertEqual(sb["by_class_sessions"]["agent"], {"sandboxed": 1})
        row = sessions_of(sb)[("h1", 100)]
        self.assertEqual(row["isolated_pct"], 100.0)

    def test_a_wrapper_whose_payload_was_not_isolated(self):
        recs = [root_tick(self.t0, 100)] + bwrap_call(self.t0, 100, 300, isolated=False)
        sb = reduce(recs)["sandbox"]
        self.assertEqual(sb["tool_calls"]["agent"], {"not_isolated": 1})
        self.assertEqual(sb["by_class_sessions"]["agent"], {"unsandboxed": 1})

    def test_a_payload_that_exited_before_read_is_unknown(self):
        recs = [root_tick(self.t0, 100)] + bwrap_call(self.t0, 100, 300, isolated=False)
        recs[2].pop("sandbox")
        recs[2]["sandbox_src"] = "denied"
        recs[2]["sandbox"] = None
        sb = reduce(recs)["sandbox"]
        self.assertEqual(sb["tool_calls"]["agent"], {"unknown": 1})
        self.assertEqual(sb["by_class_sessions"]["agent"], {"unknown": 1})

    def test_a_probe_that_ran_nothing_is_not_a_tool_call(self):
        recs = [root_tick(self.t0, 100),
                rec("exit", self.t0 + 1, 300, 100, "bwrap", "bwrap --help", agent_type="bwrap",
                    agent_pid=300, tree_root=100, is_agent=True, exit_code=0)]
        sb = reduce(recs)["sandbox"]
        self.assertEqual(sb["tool_calls"]["agent"], {"no_payload": 1})
        self.assertEqual(sb["by_class_sessions"]["agent"], {"no_calls": 1})

    def test_a_reused_pid_does_not_inherit_an_earlier_processes_children(self):
        """A child that reported to pid 300 an hour BEFORE this bwrap started
        belonged to some earlier process with that pid."""
        recs = [root_tick(self.t0, 100),
                rec("exit", self.t0, 301, 300, "zsh", "zsh -c x", agent_pid=100, tree_root=100,
                    sandbox="sandboxed", sandbox_src="userns:exec"),
                rec("exit", self.t0 + 3600, 300, 100, "bwrap", "bwrap --ro-bind / / true",
                    agent_type="bwrap", agent_pid=300, tree_root=100, is_agent=True, exit_code=1)]
        sb = reduce(recs)["sandbox"]
        self.assertEqual(sb["tool_calls"]["agent"], {"failed": 1})

    def test_the_wrapper_reading_is_not_called_a_probe(self):
        recs = [root_tick(self.t0, 100)] + bwrap_call(self.t0, 100, 300)
        rows = reduce(recs)["sandbox"]["ancestry_matrix"]
        wr = [r for r in rows if r["kind"] == "wrapper"]
        self.assertTrue(wr)
        for r in rows:
            self.assertNotIn("probe", r["reading"] or "")

    def test_session_state_rules(self):
        self.assertEqual(session_state({}), "no_calls")
        self.assertEqual(session_state({"unknown": 2}), "unknown")
        self.assertEqual(session_state({"isolated": 2, "unknown": 9}), "sandboxed")
        self.assertEqual(session_state({"isolated": 1, "failed": 1}), "partial")
        self.assertEqual(session_state({"failed": 1, "not_isolated": 3}), "failed")
        self.assertEqual(session_state({"not_isolated": 3, "no_payload": 1}), "unsandboxed")


class ShareContractTest(unittest.TestCase):
    """Shares are taken over KNOWN verdicts. Dividing by every state put 9,330
    unknown trees under 40 known ones and printed "0% sandboxed"; it also made
    `denied` deflate the rate exactly as folding it into unsandboxed would."""

    def test_the_payload_names_the_known_states(self):
        sb = reduce(bypassed_claude_session(time.time() - 60))["sandbox"]
        st = sb["states"]
        self.assertEqual(st["sandbox_known"], ["sandboxed", "unsandboxed"])
        self.assertNotIn("denied", st["sandbox_known"])
        self.assertEqual(st["tool_call_known"], ["isolated", "not_isolated", "failed"])
        self.assertEqual(st["approval_known"], ["bypassed", "auto", "supervised"])
        self.assertNotIn("unobservable", st["approval_known"])
        for k in ("session_known", "tool_call", "session", "approval", "sandbox"):
            self.assertIn(k, st)


class LiveBypassTest(unittest.TestCase):
    def live(self, recs, now):
        n = Normalizer(TS.cfg())
        lr = LiveReducer(TS.cfg(), ["agent", "human", "ide"])
        for r in recs:
            r = n.normalize(r)
            if r is not None and r.get("event") in LiveReducer.EVENTS:
                lr.feed(r)
        return lr.result(now=now)["now"]

    def test_bypass_count_comes_from_live_roots(self):
        now = time.time()
        recs = [rec("conn", now - 30, 100, 50, "claude",
                    VSCODE_CLAUDE + "--permission-mode bypassPermissions",
                    agent_pid=100, tree_root=40, is_agent=True),
                rec("conn", now - 30, 101, 50, "claude", "claude", agent_pid=101,
                    tree_root=101, is_agent=True),
                # a tool shell saying the flag is not a session saying it
                rec("conn", now - 30, 102, 100, "zsh", SNAP + "--dangerously-skip-permissions",
                    agent_pid=102, tree_root=40, is_agent=True)]
        out = self.live(recs, now)
        self.assertEqual(out["autonomous_roots"], 1)
        self.assertEqual(out["live_agent_sessions"], 2)

    def test_no_roots_is_null_not_zero(self):
        now = time.time()
        out = self.live([rec("exit", now - 5, 7, 1, "ls", "ls", agent_pid=None,
                             tree_root=None, actor3="human", tty="pts/1")], now)
        self.assertIsNone(out["autonomous_roots"])

    def test_an_exited_root_is_not_live(self):
        now = time.time()
        args = "claude --permission-mode bypassPermissions"
        out = self.live([rec("conn", now - 30, 100, 50, "claude", args, agent_pid=100,
                             tree_root=100, is_agent=True),
                         rec("exit", now - 10, 100, 50, "claude", args, agent_pid=100,
                             tree_root=100, is_agent=True)], now)
        self.assertIsNone(out["autonomous_roots"])


if __name__ == "__main__":
    unittest.main()
