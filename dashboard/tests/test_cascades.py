"""Agent tree tests: which agents started which, and which are whose subagents.

The shapes are the ones madsys-gpu1 actually shows (2026-09-20 .. 10-07): no
`claude -p` or `codex exec` from a tool call in two weeks, but Remote Control
workers under `claude rc`, Claude Desktop `ccd-cli` sessions under the remote
`server`, and background sessions `claude daemon run` hosts through a
`bg-pty-host`. The tool-call spawn is tested too, because that is the case the
view exists to catch when it happens.

    cd dashboard && uv run python -m unittest discover -s tests -q
"""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import test_merge as TM                                              # noqa: E402
from test_sessions import HOME, SNAP, claude_session, rec, reduce   # noqa: E402
from test_sandbox import CODEX_APP, codex_failed_call, root_tick     # noqa: E402

PROJ = HOME + "/proj"


def midnight(days_back):
    """The local midnight that starts the day `days_back` days ago."""
    return time.mktime(time.localtime(time.time() - days_back * 86400)[:3] + (0, 0, 0, 0, 0, -1))


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

    def test_an_env_node_launcher_is_a_front_end_too(self):
        # npm's `codex` run through its #! line: comm `codex`, argv `/usr/bin/env node
        # .../bin/codex ...` (FAS login nodes, 2026-11-09 .. 16), read as an agent with
        # a Codex subagent because its comm is no script host
        t0 = self.t0
        native = (HOME + "/.nvm/versions/node/v22.20.0/lib/node_modules/@openai/codex/"
                  "node_modules/@openai/codex-linux-x64/vendor/x86_64-unknown-linux-musl/bin/codex")
        recs = [
            rec("exit", t0 + 9, 100, 50, "codex", "/usr/bin/env node " + HOME
                + "/.nvm/versions/node/v22.20.0/bin/codex exec -s read-only review the diff",
                agent_pid=100, tree_root=100, is_agent=True, agent_type="codex"),
            rec("exit", t0 + 8, 110, 100, "codex", native + " exec -s read-only review the diff",
                agent_pid=110, tree_root=100, is_agent=True, agent_type="codex"),
        ]
        p = reduce(recs)
        t = tree_of(p, 100)
        self.assertEqual((t["root"]["role"], t["root"]["frontend"]), ("client", True))
        self.assertEqual((t["agent_depth"], p["cascades"]["summary"]["nested"]), (1, 0))

    def test_a_front_end_whose_argv_was_cut_before_app_server_is_one_too(self):
        # A node keeps 200 characters of argv. Under a long nvm path a Codex TUI's child
        # names `app-server` past that, and the rule read the cut text: 56 front ends of
        # one FAS user (2026-11-09 .. 16) read as an agent with a subagent
        t0 = self.t0
        child = (HOME + "/.nvm/versions/node/v22.23.2/lib/node_modules/@openai/codex/"
                 "node_modules/@openai/codex-linux-x64/vendor/x86_64-unknown-linux-musl/bin/codex"
                 " -c features.code_mode_host=true -c model_reasoning_effort=high app-server"
                 " --analytics-default-enabled")
        self.assertNotIn("app-server", child[:200])
        recs = [
            rec("residency", t0, 100, 50, "codex", "codex", agent_pid=100, tree_root=100,
                is_agent=True, agent_type="codex"),
            rec("conn", t0 + 1, 150, 100, "codex", child, agent_pid=150, tree_root=100,
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

    def daemon(self, starter_is_parent=False):
        """A session (100) that started `claude daemon` (300), whose pty host
        (310) runs a background session (320). Detached, the daemon keeps its
        starter as its ppid until the starter exits (`starter_is_parent`), and
        the collector may then record the starter's tree as its own."""
        t0 = self.t0
        return claude_session(t0)[:1] + [
            rec("residency", t0 + 1, 300, 100 if starter_is_parent else 1, "claude",
                HOME + "/.local/bin/claude daemon run --origin transient --spawned-by "
                '{"label":"claude","cwd":"%s","pid":100}' % PROJ,
                agent_pid=300, tree_root=100 if starter_is_parent else 300, is_agent=True),
            rec("residency", t0 + 2, 310, 1, "2.1.282",
                "claude bg-pty-host --bg-pty-host /tmp/cc-daemon-1000/ab/pty/x.sock 200 50 "
                "-- " + HOME + "/.local/share/claude/versions/2.1.282 --session-id x",
                agent_pid=310, tree_root=310, is_agent=True),
            rec("conn", t0 + 3, 320, 310, "2.1.282",
                HOME + "/.local/share/claude/versions/2.1.282 --session-id "
                "08749fc1-dedd-48d3-add6-8890798cd937 --agent claude",
                agent_pid=320, tree_root=310, is_agent=True),
        ]

    def test_the_daemon_hosts_its_sessions_and_is_no_ones_subagent(self):
        """`claude daemon` is its user's: every background session goes through
        it, whoever asked for it, so the session that happened to start it does
        not wear them all as subagents. Who started it stays on its node."""
        p = reduce(self.daemon())
        t = tree_of(p, 300)
        d = node(t, 300)
        self.assertEqual((d["role"], d["parent"], d["via"]), ("infra", None, None))
        self.assertEqual(d["spawned_by"], 100)
        self.assertEqual((node(t, 310)["parent"], node(t, 310)["via"]),
                         (300, "same_user_daemon"))
        self.assertEqual((node(t, 320)["parent"], node(t, 320)["via"]), (310, "ancestor"))
        self.assertEqual((t["agent_depth"], t["agents_nested"]), (1, 0))
        # the session that started the daemon is a tree of its own
        self.assertEqual([n["pid"] for n in tree_of(p, 100)["nodes"]], [100])
        s = p["cascades"]["summary"]
        self.assertEqual((s["nested"], s["hosted"], s["agents_nested"]), (0, 1, 0))

    def test_the_daemon_roots_its_tree_while_its_starter_is_its_parent(self):
        p = reduce(self.daemon(starter_is_parent=True))
        d = node(tree_of(p, 300), 300)
        self.assertEqual((d["ppid"], d["parent"], d["via"]), (100, None, None))
        self.assertEqual([n["pid"] for n in tree_of(p, 100)["nodes"]], [100])

    def test_a_background_session_is_driven_from_where_its_pty_host_says(self):
        # its own argv, `--session-id x` in a pty, reads as an interactive session
        n = node(tree_of(reduce(self.daemon()), 300), 320)
        self.assertEqual((n["surface"], n["surface_detail"]), ("terminal", "background session"))

    def daemon_with_spares(self, claimed_calls=0):
        """A session that started `claude daemon`, which keeps three spares --
        one that waited and exited, two still waiting, one of them claimed if
        `claimed_calls` -- and runs one background session that did the work."""
        t0 = self.t0
        native = HOME + "/.local/share/claude/versions/2.1.325"
        spare = "claude bg-spare --bg-spare /tmp/cc-daemon-1000/8fb1/spare/%d.claim.sock"
        recs = claude_session(t0)[:1] + [
            rec("residency", t0 + 1, 300, 1, "claude",
                HOME + "/.local/bin/claude daemon run --origin transient --spawned-by "
                '{"label":"claude","cwd":"%s","pid":100}' % PROJ,
                agent_pid=300, tree_root=300, is_agent=True, cwd=HOME),
            rec("exit", t0 + 60, 401, 300, "2.1.325", spare % 401, agent_pid=401,
                tree_root=300, is_agent=True, cwd="/tmp/cc-daemon-1000/8fb1",
                cpu_s=0.9, duration_s=58),
            rec("residency", t0 + 2, 402, 300, "2.1.325", spare % 402, agent_pid=402,
                tree_root=300, is_agent=True, cwd=HOME),
            rec("residency", t0 + 3, 403, 300, "2.1.325", spare % 403, agent_pid=403,
                tree_root=300, is_agent=True, cwd=HOME),
            rec("residency", t0 + 4, 310, 1, "2.1.325",
                "claude bg-pty-host --bg-pty-host /tmp/cc-daemon-1000/8fb1/pty/x.sock 200 50 "
                "-- " + native + " --session-id x", agent_pid=310, tree_root=310,
                is_agent=True, cwd=HOME),
            rec("residency", t0 + 5, 320, 310, "2.1.325", native + " --session-id x",
                agent_pid=320, tree_root=310, is_agent=True, cwd=PROJ),
        ]
        for i, (owner, k) in enumerate([(320, 4)] + [(403, claimed_calls)]):
            for j in range(k):
                pid = 500 + 10 * i + 2 * j
                recs += [
                    rec("exit", t0 + 10 + j, pid + 1, pid, "git", "git status",
                        agent_pid=pid, tree_root=310 if owner == 320 else 300),
                    rec("exit", t0 + 11 + j, pid, owner, "zsh", SNAP + "'git status'",
                        agent_pid=pid, tree_root=310 if owner == 320 else 300, is_agent=True),
                ]
        return recs

    def test_a_spare_nobody_claimed_is_the_daemons_not_an_agent(self):
        """`claude daemon` starts sessions ahead of need (`claude bg-spare`); one
        that waits is no agent, so the daemon's tree holds only the session it
        ran, not three more that ran nothing."""
        p = reduce(self.daemon_with_spares())
        c = p["cascades"]
        t = tree_of(p, 300)
        agents = sorted(n["pid"] for n in t["nodes"] if n["role"] == "agent")
        self.assertEqual(agents, [320])
        for pid in (401, 402, 403):                        # waited, or waited and exited
            self.assertNotIn(pid, [n["pid"] for n in t["nodes"]])
        self.assertEqual((t["n_agents"], t["agents_nested"], t["idle_spares"]), (1, 0, 3))
        self.assertGreaterEqual(t["hidden_nodes"], 3)
        self.assertEqual((c["summary"]["agents"], c["summary"]["agents_nested"]), (2, 0))
        self.assertEqual(c["summary"]["idle_spares"], 3)
        self.assertEqual(t["max_fanout"], 1)
        # the session is the daemon's, through its pty host: four calls, a shell
        # and a `git` each
        self.assertEqual(node(t, 320)["tool_calls"], 8)

    def test_a_spare_that_ran_something_was_claimed_and_is_an_agent(self):
        t = tree_of(reduce(self.daemon_with_spares(claimed_calls=2)), 300)
        n = node(t, 403)
        self.assertEqual((n["role"], n["spare"], n["agent_level"]), ("agent", True, 1))
        self.assertEqual(n["tool_calls"], 4)                 # two calls, two processes each
        self.assertEqual((t["n_agents"], t["idle_spares"]), (2, 2))

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

    def test_two_users_agents_that_share_a_pid_are_one_agent_in_the_tree(self):
        # Neither agent left a record of its own, so each is known from its tool calls, as the
        # agent they ran under (`parent`): alice's before midnight, and bob's after it, with
        # fewer calls and the pid she had. `agent_sessions` keeps the two apart, and a node is by
        # host and pid, so the earlier session is the one agent shown, with its own calls.
        m = midnight(2)
        raw = claude_session(m - 3000)[1:]
        raw += [dict(r, user="bob") for r in claude_session(m + 600)[1:-2]]
        view = TM.one_pass(TM.normalized(raw))
        sessions = view.reducers["trajectories"].agent_sessions()
        self.assertEqual({k: (s.user, s.n, via) for k, (s, via, _) in sessions.items()},
                         {("h1", 100, "alice"): ("alice", 5, "parent"),
                          ("h1", 100, "bob"): ("bob", 3, "parent")})
        panel = view.reducers["cascades"].result(m + 3700)
        self.assertEqual((panel["summary"]["agents"], panel["summary"]["agents_implied"]), (1, 1))
        (tree,) = panel["trees"]
        self.assertEqual([(n["pid"], n["user"], n["implied"], n["tool_calls"])
                          for n in tree["nodes"]], [(100, "alice", True, 5)])

    def test_the_role_census_counts_what_the_classifier_tagged(self):
        census = reduce(claude_session(self.t0))["cascades"]["role_census"]
        got = {(r["agent_type"], r["role"]): r["n"] for r in census}
        self.assertEqual(got, {("claude_code", "tool-shell"): 2})

    def test_a_sandboxed_tool_call_is_not_an_agent_of_its_own(self):
        # Claude Code's sandbox runs a Bash call's shell inside bwrap, behind the sandbox
        # runtime's proxy prologue (`_srt_http=$!`): the shell's parent is the inner
        # bwrap, not the agent, and that bwrap's exit comes first, so the index never
        # holds it. Read as "a tool shell's parent is its agent", every sandboxed call
        # became an implied agent: 981 in one FAS tree on 2026-11-13.
        t0 = self.t0
        bw = "bwrap --new-session --die-with-parent --unshare-net --bind /tmp/claude-http-1.sock"
        srt = ("/bin/bash -c { socat TCP6-LISTEN:3128,fork UNIX-CONNECT:/tmp/claude-http-1.sock; } "
               "& _srt_http=$! SRT_ALLOW_NESTED_USERNS=0 ARGV0=apply-seccomp /proc/self/fd/3 "
               "/bin/bash -c 'source " + HOME + "/.claude/shell-snapshots/snapshot-bash-1.sh "
               "&& eval make'")
        recs = [
            rec("residency", t0, 100, 50, "claude", "claude", agent_pid=100, tree_root=100,
                is_agent=True),
            rec("exit", t0 + 10, 401, 1, "bwrap", bw, agent_pid=400, tree_root=100),
            rec("exit", t0 + 11, 403, 402, "make", "make", agent_pid=402, tree_root=100),
            rec("exit", t0 + 12, 402, 401, "bash", srt, agent_pid=402, tree_root=100,
                is_agent=True),
            rec("exit", t0 + 13, 400, 100, "bwrap", bw, agent_pid=400, tree_root=100,
                is_agent=True),
        ]
        c = reduce(recs)["cascades"]
        self.assertEqual((c["summary"]["agents"], c["summary"]["agents_implied"],
                          c["summary"]["nested"]), (1, 0, 0))

    def test_the_sandboxs_seccomp_stage_is_not_an_agent_either(self):
        # The runtime's second stage: `ARGV0=apply-seccomp /proc/self/fd/3` (comm `3`)
        # starts the call's shell, `/bin/bash -c exec 3<&- || exit 126; source ...`,
        # and exits before it; 75 implied agents of the same FAS tree were that shim.
        t0 = self.t0
        shim = ("/proc/self/fd/3 /bin/bash -c exec 3<&- || exit 126; source " + HOME
                + "/.claude/shell-snapshots/snapshot-bash-1.sh && eval make")
        shell = ("/bin/bash -c exec 3<&- || exit 126; source " + HOME
                 + "/.claude/shell-snapshots/snapshot-bash-1.sh && eval make")
        recs = [
            rec("residency", t0, 100, 50, "claude", "claude", agent_pid=100, tree_root=100,
                is_agent=True),
            rec("exit", t0 + 10, 404, 402, "3", shim, agent_pid=404, tree_root=100),
            rec("exit", t0 + 11, 406, 405, "make", "make", agent_pid=405, tree_root=100),
            rec("exit", t0 + 12, 405, 404, "bash", shell, agent_pid=405, tree_root=100,
                is_agent=True),
        ]
        c = reduce(recs)["cascades"]
        self.assertEqual((c["summary"]["agents"], c["summary"]["agents_implied"],
                          c["summary"]["nested"]), (1, 0, 0))

    def test_a_sockets_age_does_not_date_the_agent(self):
        # A `conn` record's age_s is its socket's age, and the FAS login nodes' schema-7
        # collectors stamp it off the wall clock against BPF ktime: ~1.79e9 s, so 14,821
        # agents of 2026-11-09..16 read as started on 1970-01-02. A tick's age stands.
        t0 = self.t0
        recs = [
            rec("residency", t0, 100, 50, "claude", "claude", agent_pid=100, tree_root=100,
                is_agent=True, age_s=120.0),
            rec("conn", t0 + 1, 100, 50, "claude", "claude", agent_pid=100, tree_root=100,
                is_agent=True, age_s=1794184119.1),
        ]
        n = node(tree_of(reduce(recs), 100), 100)
        self.assertEqual(n["started"], time.strftime("%Y-%m-%d %H:%M:%S",
                                                     time.localtime(t0 - 120.0)))


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



class TreesSentTest(unittest.TestCase):
    """Which trees the panel sends: every one the window holds, up to a cap the
    page can see, the deepest first. With 25, a window with 25 trees of
    subagents sent no session that started no other."""

    def setUp(self):
        self.t0 = time.time() - 3600

    def recs(self, nested=30):
        recs = []
        for i in range(nested):
            root, shell, sub = 1000 + 10 * i, 1001 + 10 * i, 1002 + 10 * i
            recs += [
                rec("residency", self.t0 + i, root, 50, "claude", "claude",
                    agent_pid=root, tree_root=root, is_agent=True),
                rec("exit", self.t0 + i + 5, sub, shell, "claude", "claude -p summarise",
                    agent_pid=sub, tree_root=root, is_agent=True),
                rec("exit", self.t0 + i + 6, shell, root, "zsh", SNAP + "'claude -p summarise'",
                    agent_pid=shell, tree_root=root, is_agent=True),
            ]
        return recs + claude_session(self.t0, root=100)       # one session on its own

    def test_a_session_on_its_own_is_sent_beside_many_trees_of_subagents(self):
        c = reduce(self.recs())["cascades"]
        roots = [t["root"]["pid"] for t in c["trees"]]
        self.assertEqual(len(roots), 31)
        self.assertIn(100, roots)
        self.assertEqual((c["summary"]["trees"], c["summary"]["trees_sent"]), (31, 31))

    def test_past_the_cap_the_deepest_are_sent_and_the_rest_counted(self):
        from rc_dashboard.normalize import Normalizer
        from rc_dashboard.windows import PanelView
        import test_sessions
        cfg = test_sessions.cfg()
        view = PanelView(cfg, list(cfg.get("filters.classes")) + [cfg.get("filters.unlabeled_class")], 1440)
        reducers = view.fresh_reducers()
        reducers["cascades"].top_n = 5
        dispatch = PanelView.build_dispatch(reducers)
        norm = Normalizer(cfg)
        for raw in self.recs():
            r = norm.normalize(raw)
            if r is not None:
                for red in dispatch.get(r.get("event"), ()):
                    red.feed(r)
        view.reducers = reducers
        c = view.materialize()["cascades"]
        self.assertEqual(len(c["trees"]), 5)
        self.assertTrue(all(t["agent_depth"] == 2 for t in c["trees"]))
        self.assertEqual((c["summary"]["trees"], c["summary"]["trees_sent"]), (31, 5))


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


class LaunchTagTest(unittest.TestCase):
    """What an agent's box is tagged with: how it was launched, the version its
    program names, and whether its tool calls ran in a sandbox."""

    def setUp(self):
        self.t0 = time.time() - 600

    def test_an_agent_carries_its_launch_and_version(self):
        native = HOME + "/.local/share/claude/versions/2.1.291"
        recs = [rec("conn", self.t0, 100, 50, "2.1.291", native + " --dangerously-skip-permissions",
                    agent_pid=100, tree_root=100, is_agent=True)]
        n = node(tree_of(reduce(recs), 100), 100)
        self.assertEqual((n["approval"], n["version"]), ("bypassed", "2.1.291"))
        self.assertEqual(n["approval_detail"], "--dangerously-skip-permissions")

    def test_a_codex_app_server_takes_its_front_ends_launch(self):
        # `codex --yolo` starts the app-server; the app-server's argv names no approval
        rel = (HOME + "/.codex/packages/app-server-daemon/releases/"
               "0.162.1-x86_64-unknown-linux-musl/bin/codex")
        recs = [
            rec("residency", self.t0, 100, 50, "codex", "codex --yolo", agent_pid=100,
                tree_root=100, is_agent=True, agent_type="codex"),
            rec("conn", self.t0 + 1, 150, 100, "codex", rel + " app-server --listen unix://",
                agent_pid=150, tree_root=100, is_agent=True, agent_type="codex"),
        ]
        n = node(tree_of(reduce(recs), 100), 150)
        self.assertEqual((n["approval"], n["version"]), ("bypassed", "0.162.1"))

    def test_calls_that_ran_unconfined_make_an_unsandboxed_agent(self):
        n = node(tree_of(reduce(claude_session(self.t0)), 100), 100)
        self.assertEqual((n["sandbox"], n["sandbox_calls"]), ("unsandboxed", {"not_isolated": 2}))

    def test_a_sandbox_that_failed_to_start_is_failed_not_unsandboxed(self):
        recs = [root_tick(self.t0, 1000, CODEX_APP, "codex")] + codex_failed_call(self.t0 + 1)
        n = node(tree_of(reduce(recs), 1000), 1000)
        self.assertEqual((n["sandbox"], n["sandbox_calls"]), ("failed", {"failed": 1}))

    def test_an_agent_with_no_tool_call_has_no_sandbox_verdict(self):
        n = node(tree_of(reduce([root_tick(self.t0, 100)]), 100), 100)
        self.assertEqual((n["sandbox"], n["sandbox_calls"]), (None, None))


if __name__ == "__main__":
    unittest.main()
