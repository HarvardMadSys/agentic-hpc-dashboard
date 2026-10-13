"""Agent tree tests: which agents started which, and which are whose subagents.

The shapes are the ones madsys-gpu1 actually shows (2026-09-20 .. 10-07): no
`claude -p` or `codex exec` from a tool call in two weeks, but Remote Control
workers under `claude rc`, Claude Desktop `ccd-cli` sessions under the remote
`server`, and background agents started by `claude daemon run --spawned-by`
through a `bg-pty-host`. The tool-call spawn is tested too, because that is the
case the view exists to catch when it happens.

    cd dashboard && uv run python -m unittest discover -s tests -q
"""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_sessions import HOME, SNAP, claude_session, rec, reduce   # noqa: E402

PROJ = HOME + "/proj"


def tree_of(payload, root_pid):
    for t in payload["cascades"]["trees"]:
        if t["root"]["pid"] == root_pid:
            return t
    raise AssertionError("no tree rooted at %s: %s" % (
        root_pid, [t["root"]["pid"] for t in payload["cascades"]["trees"]]))


def node(tree, pid):
    return next(n for n in tree["nodes"] if n["pid"] == pid)


class CascadeTest(unittest.TestCase):
    def setUp(self):
        self.t0 = time.time() - 600

    def test_a_lone_agent_is_a_tree_one_agent_deep(self):
        c = reduce(claude_session(self.t0))["cascades"]
        self.assertEqual(c["summary"]["trees"], 1)
        self.assertEqual(c["summary"]["nested"], 0)
        self.assertEqual(c["depth_hist"], {"1": 1})

    def test_an_agent_started_by_an_agent_is_a_cascade(self):
        t0 = self.t0
        recs = claude_session(t0)[:1] + [
            rec("exit", t0 + 5, 250, 200, "codex", "codex exec review the diff",
                agent_pid=250, tree_root=100, is_agent=True, agent_type="codex"),
            rec("exit", t0 + 6, 200, 100, "zsh", SNAP + "'codex exec review the diff'",
                agent_pid=200, tree_root=100, is_agent=True),
        ]
        p = reduce(recs)
        c = p["cascades"]
        self.assertEqual(c["summary"]["nested"], 1)
        self.assertEqual(tree_of(p, 100)["agent_depth"], 2)
        self.assertIn({"parent": "claude_code agent", "child": "codex agent", "n": 1},
                      c["edges"])

    def test_remote_control_workers_are_hosted_not_nested(self):
        t0 = self.t0
        recs = [
            rec("residency", t0, 100, 50, "claude", "claude rc",
                agent_pid=100, tree_root=100, is_agent=True),
            rec("conn", t0 + 1, 150, 100, "2.1.291",
                HOME + "/.local/share/claude/versions/2.1.291 --print --sdk-url "
                "https://api.anthropic.com/v1/code/sessions/cse_01",
                agent_pid=150, tree_root=100, is_agent=True),
        ]
        p = reduce(recs)
        self.assertEqual(p["cascades"]["summary"]["nested"], 0)
        self.assertEqual(p["cascades"]["summary"]["hosted"], 1)
        t = tree_of(p, 100)
        self.assertEqual((node(t, 150)["parent"], node(t, 150)["via"]), (100, "ancestor"))

    def test_a_codex_front_end_is_not_a_second_agent(self):
        # `codex` (the TUI) starts `codex app-server`, which runs the session
        t0 = self.t0
        recs = [
            rec("residency", t0, 100, 50, "codex", "codex", agent_pid=100, tree_root=100,
                is_agent=True, agent_type="codex"),
            rec("conn", t0 + 1, 150, 100, "codex", HOME + "/.codex/packages/app-server-daemon"
                "/current/bin/codex app-server --listen unix://", agent_pid=150,
                tree_root=100, is_agent=True, agent_type="codex"),
        ]
        p = reduce(recs)
        t = tree_of(p, 100)
        self.assertEqual(t["agent_depth"], 1)
        self.assertEqual(t["root"]["role"], "client")
        self.assertTrue(t["root"]["frontend"])
        self.assertEqual(p["cascades"]["summary"]["nested"], 0)
        self.assertEqual(p["cascades"]["summary"]["hosted"], 0)

    def test_a_node_launcher_is_a_front_end_not_an_agent_with_a_subagent(self):
        # npm's `codex` is a node script that execs the native binary (rtx6000a)
        t0 = self.t0
        native = ("/usr/lib/node_modules/@openai/codex/node_modules/@openai/codex-linux-x64"
                  "/vendor/x86_64-unknown-linux-musl/codex/codex")
        recs = [
            rec("exit", t0 + 9, 100, 50, "node", "node /usr/local/bin/codex", agent_pid=100,
                tree_root=100, is_agent=True, agent_type="codex"),
            rec("exit", t0 + 8, 110, 100, "codex", native, agent_pid=110, tree_root=100,
                is_agent=True, agent_type="codex"),
        ]
        p = reduce(recs)
        t = tree_of(p, 100)
        self.assertEqual((t["root"]["role"], t["root"]["frontend"]), ("client", True))
        self.assertEqual((t["agent_depth"], p["cascades"]["summary"]["nested"]), (1, 0))

    def test_infrastructure_with_no_agent_below_is_counted_not_listed(self):
        t0 = self.t0
        daemon = HOME + "/.codex/packages/app-server-daemon/current/bin/codex"
        recs = [
            rec("residency", t0, 100, 1, "codex-code-mode-host", daemon + "-code-mode-host",
                agent_pid=100, tree_root=100, is_agent=True, agent_type="codex"),
            rec("conn", t0 + 1, 150, 100, "codex", daemon + " app-server --listen unix://",
                agent_pid=150, tree_root=100, is_agent=True, agent_type="codex"),
            rec("exit", t0 + 2, 160, 100, "codex", daemon + " app-server daemon pid-update-loop",
                agent_pid=160, tree_root=100, is_agent=True, agent_type="codex"),
        ]
        t = tree_of(reduce(recs), 100)
        self.assertEqual(sorted(n["pid"] for n in t["nodes"]), [100, 150])
        self.assertEqual(t["hidden_nodes"], 1)

    def test_an_agent_started_from_a_tool_call_links_to_the_agent_that_ran_it(self):
        t0 = self.t0
        recs = claude_session(t0)[:1] + [
            rec("exit", t0 + 5, 250, 200, "claude", "claude -p write the summary",
                agent_pid=250, tree_root=100, is_agent=True),
            rec("exit", t0 + 6, 200, 100, "zsh", SNAP + "'claude -p write the summary'",
                agent_pid=200, tree_root=100, is_agent=True),
        ]
        t = tree_of(reduce(recs), 100)
        self.assertEqual(t["agent_depth"], 2)
        n = node(t, 250)
        self.assertEqual((n["parent"], n["via"]), (100, "ancestor"))
        self.assertEqual(n["path"], ["zsh"])           # what it was started through

    def test_background_agents_link_back_to_the_session_that_started_the_daemon(self):
        t0 = self.t0
        recs = claude_session(t0)[:1] + [
            rec("residency", t0 + 1, 300, 1, "claude",
                HOME + "/.local/bin/claude daemon run --origin transient --spawned-by "
                '{"label":"claude","cwd":"%s","pid":100}' % PROJ,
                agent_pid=300, tree_root=300, is_agent=True),
            rec("residency", t0 + 2, 310, 1, "2.1.282",
                "claude bg-pty-host --bg-pty-host /tmp/cc-daemon-1000/ab/pty/x.sock 200 50 "
                "-- " + HOME + "/.local/share/claude/versions/2.1.282 --session-id x",
                agent_pid=310, tree_root=310, is_agent=True),
            rec("conn", t0 + 3, 320, 310, "2.1.282",
                HOME + "/.local/share/claude/versions/2.1.282 --session-id "
                "08749fc1-dedd-48d3-add6-8890798cd937 --agent claude",
                agent_pid=320, tree_root=310, is_agent=True),
        ]
        t = tree_of(reduce(recs), 100)
        self.assertEqual((node(t, 300)["parent"], node(t, 300)["via"]), (100, "spawned_by"))
        self.assertEqual((node(t, 310)["parent"], node(t, 310)["via"]),
                         (300, "same_user_daemon"))
        self.assertEqual((node(t, 320)["parent"], node(t, 320)["via"]), (310, "ancestor"))
        self.assertEqual(t["agent_depth"], 2)            # 100, then 320

    def test_sessions_hosted_by_infrastructure_are_not_a_cascade(self):
        # Claude Desktop's remote server runs one ccd-cli per chat: hosted, not nested
        t0 = self.t0
        srv = HOME + "/.claude/remote/srv/89cb/server"
        cli = HOME + "/.claude/remote/ccd-cli/2.1.286 --output-format stream-json --verbose"
        recs = [
            rec("residency", t0, 400, 1, "server", srv + " --serve --socket x.sock",
                agent_pid=400, tree_root=400, is_agent=True),
            rec("exit", t0 + 5, 410, 400, "2.1.286", cli, agent_pid=410, tree_root=400,
                is_agent=True),
            rec("exit", t0 + 6, 420, 400, "2.1.286", cli, agent_pid=420, tree_root=400,
                is_agent=True),
        ]
        p = reduce(recs)
        t = tree_of(p, 400)
        self.assertEqual(t["agent_depth"], 1)
        self.assertEqual(t["max_fanout"], 2)
        self.assertEqual(p["cascades"]["summary"]["nested"], 0)
        self.assertEqual(p["cascades"]["summary"]["hosted"], 1)

    def grep_from_a_subshell(self, with_subshell=True):
        """`grep` in a Bash tool call runs Claude's binary as ugrep, from a
        subshell the tool's shell forked (argv inherited, so never tagged).
        Seen on madsys-gpu1 on 2026-10-10, where it read as three nested agents."""
        t0 = self.t0
        ug = ("ugrep -G --ignore-files --hidden -I -n needle "
              + HOME + "/.claude/projects/x.jsonl")
        recs = claude_session(t0)[:1] + [
            rec("exit", t0 + 5, 202, 201, "2.1.296", ug, agent_pid=202, tree_root=100,
                is_agent=True),
        ]
        if with_subshell:
            recs += [
                rec("exit", t0 + 6, 201, 200, "zsh", SNAP + "'grep -n needle x'",
                    agent_pid=200, tree_root=100, argv_source="inherit"),
                rec("exit", t0 + 7, 200, 100, "zsh", SNAP + "'grep -n needle x'",
                    agent_pid=200, tree_root=100, is_agent=True),
            ]
        return reduce(recs)

    def test_a_built_in_grep_run_from_a_subshell_is_not_an_agent(self):
        p = self.grep_from_a_subshell()
        c = p["cascades"]["summary"]
        self.assertEqual((c["nested"], c["agents_implied"], c["agents"]), (0, 0, 1))
        rows = [p["trajectories"]["rows"][s] for s in p["trajectories"]["ranks"]["events"]]
        self.assertEqual([(r["agent_pid"], r["owner_via"]) for r in rows], [("100", "ancestor")])

    def test_a_built_in_grep_whose_parent_was_never_seen_folds_into_the_tree_root(self):
        p = self.grep_from_a_subshell(with_subshell=False)
        self.assertEqual(p["cascades"]["summary"]["agents_implied"], 0)
        rows = [p["trajectories"]["rows"][s] for s in p["trajectories"]["ranks"]["events"]]
        self.assertEqual([(r["agent_pid"], r["owner_via"]) for r in rows], [("100", "tree_root")])

    def reused_parent_pid(self, user):
        """haoran's Claude Desktop server, pid 28324, was started by pid 28307 on
        2026-10-09. After the host's pids wrapped, an unrelated `cut` run under
        glen's codex took 28307 -- read as codex starting haoran's server."""
        t0 = self.t0
        srv = HOME + "/.claude/remote/srv/89cb/server --serve --socket x.sock"
        return reduce([
            rec("residency", t0, 1467449, 1385881, "codex", "codex", agent_pid=1467449,
                tree_root=1467449, is_agent=True, agent_type="codex", user="glen"),
            rec("residency", t0 + 1, 28324, 28307, "server", srv, agent_pid=28324,
                tree_root=28324, is_agent=True, user="haoran", age_s=100000),
            rec("conn", t0 + 1, 28483, 28324, "2.1.293", HOME + "/.claude/remote/ccd-cli/"
                "2.1.293 --output-format stream-json --verbose", agent_pid=28483,
                tree_root=28324, is_agent=True, user="haoran"),
            rec("exit", t0 + 2, 28307, 28305, "cut", "cut -d: -f2", agent_pid=1467449,
                tree_root=1467449, agent_type="codex", user=user),
            rec("exit", t0 + 3, 28305, 1467449, "lsb_release", "/bin/sh /usr/bin/lsb_release -a",
                agent_pid=1467449, tree_root=1467449, agent_type="codex", user=user),
        ])

    def test_a_reused_pid_never_links_two_users_processes(self):
        p = self.reused_parent_pid("glen")
        self.assertIsNone(node(tree_of(p, 28324), 28324)["parent"])
        self.assertEqual(p["cascades"]["summary"]["nested"], 0)

    def test_a_reused_pid_that_started_after_the_child_is_not_its_parent(self):
        p = self.reused_parent_pid("haoran")      # same user: only the start times say no
        self.assertIsNone(node(tree_of(p, 28324), 28324)["parent"])

    def test_the_role_census_counts_what_the_classifier_tagged(self):
        census = reduce(claude_session(self.t0))["cascades"]["role_census"]
        got = {(r["agent_type"], r["role"]): r["n"] for r in census}
        self.assertEqual(got, {("claude_code", "tool-shell"): 2})


class DriftTest(unittest.TestCase):
    def setUp(self):
        self.t0 = time.time() - 3000

    def session(self, calls, launch_cwd=PROJ):
        """A Claude agent and one tool-call shell per (command, cwd)."""
        t0 = self.t0
        recs = [rec("residency", t0, 100, 50, "claude", "claude", agent_pid=100,
                    tree_root=100, is_agent=True, cwd=launch_cwd)]
        for i, (cmd, cwd) in enumerate(calls):
            pid = 1000 + i
            recs.append(rec("exit", t0 + 10 + i, pid, 100, cmd, cmd, agent_pid=100,
                            tree_root=100, cwd=cwd))
        return recs

    def drift(self, recs):
        p = reduce(recs)
        rows = [p["trajectories"]["rows"][s] for s in p["trajectories"]["ranks"]["events"]]
        self.assertEqual(len(rows), 1)
        return rows[0]["drift"], p

    def test_cwd_drift_is_the_share_of_processes_outside_the_launch_directory(self):
        d, _ = self.drift(self.session([
            ("git", PROJ), ("git", PROJ), ("ls", PROJ + "/sub"),
            ("ls", "/scratch/other"), ("cat", "/scratch/other")]))
        self.assertEqual(d["cwd_anchor"], PROJ)
        self.assertEqual(d["cwd_outside_pct"], 40.0)
        self.assertEqual(d["cwd_distinct"], 3)
        self.assertEqual(d["cwd_last"], "/scratch/other")

    def test_an_agent_launched_at_the_filesystem_root_is_anchored_on_its_first_call(self):
        # IDE-hosted agents run with cwd `/`, under which every path is "inside":
        # the share would read 0% by construction, which is not a measurement.
        d, _ = self.drift(self.session([("git", PROJ), ("ls", "/scratch/other")],
                                       launch_cwd="/"))
        self.assertEqual((d["cwd_anchor"], d["cwd_anchor_src"]), (PROJ, "first_call"))
        self.assertEqual(d["cwd_outside_pct"], 50.0)

    def test_the_first_call_anchor_skips_calls_made_at_the_root(self):
        d, _ = self.drift(self.session([("ls", "/"), ("git", PROJ), ("ls", "/scratch/other")],
                                       launch_cwd="/"))
        self.assertEqual(d["cwd_anchor"], PROJ)
        self.assertEqual(d["cwd_outside_pct"], 66.67)

    def test_a_sibling_with_a_shared_prefix_is_outside(self):
        d, _ = self.drift(self.session([("git", PROJ), ("git", PROJ + "-old")]))
        self.assertEqual(d["cwd_outside_pct"], 50.0)

    def test_tool_mix_drift_compares_the_first_and_last_third(self):
        calls = [("git", PROJ)] * 10 + [("ls", PROJ)] * 10 + [("make", PROJ)] * 10
        d, _ = self.drift(self.session(calls))
        self.assertEqual(d["mix_jsd"], 1.0)              # git only, then build only
        self.assertEqual(d["mix_first"][0][0], "git")
        self.assertEqual(d["mix_last"][0][0], "build")

    def test_an_unchanging_mix_has_no_drift(self):
        d, _ = self.drift(self.session([("git", PROJ)] * 30))
        self.assertEqual(d["mix_jsd"], 0.0)

    def test_too_short_a_session_has_no_mix_drift(self):
        d, _ = self.drift(self.session([("git", PROJ)] * 10 + [("make", PROJ)] * 10))
        self.assertIsNone(d["mix_jsd"])

    def test_the_agent_node_carries_its_session_and_home_but_no_drift(self):
        # drift is the trajectory rows' reading; the Agents page shows where an
        # agent worked instead (workspace.py), so its nodes carry the home
        _, p = self.drift(self.session([("git", PROJ), ("ls", "/scratch/other")]))
        n = node(tree_of(p, 100), 100)
        self.assertNotIn("drift", n)
        self.assertEqual((n["home"], n["home_src"]), (PROJ, "agent"))
        self.assertEqual(n["tool_calls"], 2)
        self.assertEqual(n["workspace"]["dirs"], 2)
        self.assertEqual(n["workspace"]["inside_pct"], 50.0)



class PickTest(unittest.TestCase):
    """A tree bigger than what is sent: its shape, then each parent's busiest
    agents in turn, and what the rest did counted on their parent."""

    @staticmethod
    def agent(pid, parent, calls, fanout=0, outside=0, level=2):
        return {"pid": pid, "parent": parent, "role": "agent", "fanout": fanout,
                "tool_calls": calls, "cpu_s": calls / 100, "agent_level": level,
                "workspace": {"outside_writes": outside}}

    def tree(self):
        from rc_dashboard.reducers.cascades import _pick
        root = {"pid": 1, "parent": None, "role": "client", "fanout": 1}
        lead = self.agent(10, 1, 5000, fanout=101, level=1)
        # a hundred subagents walked in start order; the busy ones come last
        subs = [self.agent(100 + i, 10, 0 if i < 40 else 1) for i in range(97)]
        subs += [self.agent(300, 10, 59, outside=2), self.agent(301, 10, 12),
                 self.agent(302, 10, 3)]
        mid = self.agent(400, 10, 7, fanout=1)
        deep = self.agent(401, 400, 2, level=3)
        nodes = [root, lead] + subs + [mid, deep]
        return _pick, nodes

    def test_the_busiest_are_sent_and_the_shape_kept(self):
        pick, nodes = self.tree()
        sent = pick(nodes, 12)
        pids = [x["pid"] for x in sent]
        self.assertEqual(len(sent), 12)
        self.assertEqual(pids[:2], [1, 10])                  # walk order kept
        for must in (300, 301, 302, 400, 401):               # busiest, and the shape
            self.assertIn(must, pids)
        self.assertNotIn(100, pids)                          # ran nothing: folded

    def test_what_was_not_sent_is_counted_on_its_parent(self):
        pick, nodes = self.tree()
        sent = pick(nodes, 12)
        lead = next(x for x in sent if x["pid"] == 10)
        sent_subs = sum(1 for x in sent if x["pid"] >= 100 and x["pid"] < 400)
        f = lead["folded"]
        self.assertEqual(f["agents"], 100 - sent_subs)
        self.assertEqual(f["ran_nothing"], 40)
        self.assertEqual(f["agents"], f["ran_nothing"] + f["one"])
        self.assertEqual(f["outside"], 0)                    # the one that wrote outside was sent
        self.assertNotIn("folded", next(x for x in sent if x["pid"] == 400))


if __name__ == "__main__":
    unittest.main()
