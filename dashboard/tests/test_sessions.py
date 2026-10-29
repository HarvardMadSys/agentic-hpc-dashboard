"""Sessionizing tests: one agent session is one trajectory, not one per tool call.

The collector tags a process as its own agent root whenever its argv matches the
agent classifier, and that classifier matches any path under `~/.claude/` or
`~/.codex/`. Every Claude Code Bash tool call runs as
`zsh -c source ~/.claude/shell-snapshots/...`, so each one became the
`agent_pid` -- and so the `session_key` -- of everything it ran. On madsys-gpu1
on 2026-10-06 one Claude tree split into 204 session keys and one Codex tree
into 383. These tests pin the fold that undoes that, and the cases it must
leave alone.

Runs anywhere: the records are built here and fed through the same reducer set
a window build uses.

    cd dashboard && uv run python -m unittest discover -s tests -q
"""
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rc_dashboard import config as configmod      # noqa: E402
from rc_dashboard.normalize import Normalizer      # noqa: E402
from rc_dashboard.roles import role_of             # noqa: E402
from rc_dashboard.windows import PanelView         # noqa: E402

TS = "%Y-%m-%d %H:%M:%S"
HOME = "/home/alice"
SNAP = "/bin/zsh -c source %s/.claude/shell-snapshots/snapshot-zsh-1.sh && eval " % HOME


def cfg():
    return configmod.Config(configmod.defaults(), {}, None)


def rec(event, t, pid, ppid, command, args, *, agent_pid, tree_root,
        is_agent=False, agent_type="claude_code", depth=None, user="alice",
        host="h1", cwd=HOME + "/proj", **kw):
    """One collector record with the schema-7 identity block."""
    r = {"schema_version": 7, "event": event, "ts": time.strftime(TS, time.localtime(t)),
         "ts_epoch": t, "host": host, "user": user, "uid": 1000,
         "actor": "agent", "actor3": "agent", "attribution": "ancestry",
         "agent_type": agent_type, "pid": pid, "ppid": ppid,
         "command": command, "args": args, "cwd": cwd, "tty": None,
         "is_agent": is_agent, "agent_pid": agent_pid,
         "session_key": "apid:%s" % agent_pid if agent_pid is not None else None,
         "tree_root_pid": tree_root,
         "depth": depth if depth is not None else (0 if is_agent else 1),
         "sandbox": "unsandboxed", "sandbox_src": "none"}
    if event in ("exit", "truncated"):
        r.update(cpu_s=0.01, duration_s=0.5,
                 start=time.strftime(TS, time.localtime(t - 0.5)))
    r.update(kw)
    return r


def reduce(recs, minutes=1440):
    """Normalise and dispatch exactly as `WindowRegistry._build` does."""
    c = cfg()
    classes = list(c.get("filters.classes")) + [c.get("filters.unlabeled_class")]
    view = PanelView(c, classes, minutes)
    reducers = view.fresh_reducers()
    dispatch = PanelView.build_dispatch(reducers)
    norm = Normalizer(c)
    for raw in recs:
        r = norm.normalize(raw)
        if r is None:
            continue
        for red in dispatch.get(r.get("event"), ()):
            red.feed(r)
    view.reducers = reducers
    return view.materialize()


def ranked(payload, rank="events"):
    t = payload["trajectories"]
    return [t["rows"][sid] for sid in t["ranks"][rank]]


def claude_session(t0, root=100):
    """A Claude Code session in tmux: two Bash tool calls and one Grep.

    Order is the collector's: a child's exit is written before the shell that
    waited on it.
    """
    return [
        rec("residency", t0, root, 50, "claude", "claude",
            agent_pid=root, tree_root=root, is_agent=True),
        # Bash tool call 1: the snapshot shell is classifier-tagged, its child is not
        rec("exit", t0 + 10, 201, 200, "git", "git status",
            agent_pid=200, tree_root=root),
        rec("exit", t0 + 11, 200, root, "zsh", SNAP + "'git status'",
            agent_pid=200, tree_root=root, is_agent=True),
        # Grep: the native binary re-executes itself as ripgrep
        rec("exit", t0 + 20, 210, root, "2.1.291", "rg --no-config --files --hidden " + HOME,
            agent_pid=root, tree_root=root),
        # Bash tool call 2
        rec("exit", t0 + 30, 301, 300, "ls", "ls -la",
            agent_pid=300, tree_root=root),
        rec("exit", t0 + 31, 300, root, "zsh", SNAP + "'ls -la'",
            agent_pid=300, tree_root=root, is_agent=True),
    ]


CODEX_STANDALONE = (HOME + "/.codex/packages/standalone/releases/0.162.1-x86_64-unknown-"
                    "linux-musl/bin/codex")
CLAUDE_VSCODE = (HOME + "/.vscode-server/extensions/anthropic.claude-code-2.1.300-linux-x64/"
                 "resources/native-binary/claude")


class RoleTest(unittest.TestCase):
    """What a classifier-tagged process actually is, on argv shapes seen on
    madsys-gpu1 and the FAS login nodes."""

    CASES = [
        # (agent_type, command, args, role)
        ("claude_code", "claude", "claude", "agent"),
        ("claude_code", "claude", "claude --rc", "agent"),
        # the Remote Control server: it runs no tool calls of its own, it starts
        # a `--print --sdk-url` worker per remote session
        ("claude_code", "claude", "claude rc", "infra"),
        ("claude_code", "claude", "claude remote-control", "infra"),
        ("claude_code", "claude", "claude -p summarise this", "agent"),
        ("claude_code", "2.1.291", HOME + "/.local/share/claude/versions/2.1.291 --print "
         "--sdk-url https://api.anthropic.com/v1/code/sessions/cse_01", "agent"),
        ("claude_code", "claude", HOME + "/.vscode-server/extensions/anthropic.claude-code-2.1.285"
         "-linux-x64/resources/native-binary/claude --output-format stream-json --verbose",
         "agent"),
        ("claude_code", "2.1.286", HOME + "/.claude/remote/ccd-cli/2.1.286 --output-format "
         "stream-json --verbose --input-format stream-json", "agent"),
        ("claude_code", "2.1.282", HOME + "/.local/share/claude/versions/2.1.282 --session-id "
         "08749fc1-dedd-48d3-add6-8890798cd937 --agent claude", "agent"),
        ("claude_code", "2.1.282", "claude bg-spare --bg-spare /tmp/cc-daemon-1/x.claim.sock",
         "agent"),
        ("claude_code", "claude", HOME + "/.local/bin/claude daemon run --origin transient "
         '--spawned-by {"label":"claude","cwd":"/x","pid":1136358}', "infra"),
        ("claude_code", "2.1.282", "claude bg-pty-host --bg-pty-host /tmp/cc-daemon-1/p.sock "
         "200 50 -- " + HOME + "/.local/share/claude/versions/2.1.282 --session-id x", "infra"),
        ("claude_code", "server", HOME + "/.claude/remote/srv/89cb/server --serve --socket "
         + HOME + "/.claude/remote/run/c0e8/rpc.sock", "infra"),
        ("claude_code", "claude", "claude attach 189b4394", "client"),
        ("claude_code", "claude", "claude agents", "client"),
        ("claude_code", "claude", "claude --version", "probe"),
        ("claude_code", "claude", HOME + "/.vscode-server/extensions/anthropic.claude-code-2.1.286"
         "-linux-x64/resources/native-binary/claude auth status --json", "probe"),
        ("claude_code", "server", HOME + "/.claude/remote/srv/89cb/server --version", "probe"),
        # the VS Code extension's binary run as the Chrome MCP server, by the
        # extension host (FAS login nodes, 2026-10-20 .. 10-28)
        ("claude_code", "claude", CLAUDE_VSCODE + " --claude-in-chrome-mcp", "helper"),
        ("claude_code", "2.1.285", "rg --no-config --files --hidden " + HOME + "/.claude/plugins",
         "embedded-tool"),
        ("claude_code", "2.1.280", "ugrep -G --ignore-files --hidden -I -rn foo", "embedded-tool"),
        ("claude_code", "zsh", SNAP + "'git status'", "tool-shell"),
        ("claude_code", "bash", "/bin/bash -c source " + HOME + "/.claude/shell-snapshots/"
         "snapshot-bash-1.sh 2>/dev/null || true && eval 'make'", "tool-shell"),
        ("claude_code", "sh", "sh -c ls -1 " + HOME + "/.claude/projects/x", "helper"),
        ("claude_code", "chmod", "chmod 700 " + HOME + "/.claude/remote", "helper"),
        ("codex", "codex", "codex", "agent"),
        ("codex", "codex", HOME + "/.vscode-server/extensions/openai.chatgpt-26.917.62051-linux-x64"
         "/bin/linux-x86_64/codex -c features.code_mode_host=true app-server --analytics-default",
         "agent"),
        ("codex", "codex", HOME + "/.codex/packages/app-server-daemon/current/bin/codex "
         "app-server --listen unix:// --managed-daemon", "agent"),
        ("codex", "node", "node " + HOME + "/.nvm/versions/node/v22.22.0/bin/codex", "agent"),
        ("codex", "codex", "codex exec fix the tests", "agent"),
        ("codex", "codex", "codex app-server proxy", "client"),
        ("codex", "codex", "codex app-server daemon pid-update-loop", "infra"),
        ("codex", "codex-code-mode-host", HOME + "/.codex/packages/app-server-daemon/releases/"
         "0.159.3/bin/codex-code-mode-host", "infra"),
        ("codex", "codex", HOME + "/.codex/packages/app-server-daemon/current/bin/codex --version",
         "probe"),
        ("codex", "node", "node " + HOME + "/.nvm/versions/node/v22.22.0/bin/codex --version",
         "probe"),
        ("codex", "codex-linux-sandbox", HOME + "/.codex/tmp/arg0/codex-arg0lL3/codex-linux-"
         "sandbox --sandbox-policy-cwd " + HOME + "/w --command-cwd /", "sandbox"),
        ("codex", "bwrap", "bwrap --as-pid-1 --new-session --die-with-parent", "sandbox"),
        ("bwrap", "bwrap", "/usr/bin/bwrap --unshare-user --ro-bind / / /bin/true", "sandbox"),
        # Codex's sandboxed fs helper, from each place Codex is installed, and
        # the sandbox that runs it (FAS login nodes, 2026-10-20 .. 10-28)
        ("codex", "codex", CODEX_STANDALONE + " --codex-run-as-fs-helper", "helper"),
        ("codex", "codex", HOME + "/.vscode-server/extensions/openai.chatgpt-26.1007.21434-linux-x64"
         "/bin/linux-x86_64/codex --codex-run-as-fs-helper", "helper"),
        ("codex", "codex", HOME + "/.codex/packages/app-server-daemon/releases/0.165.0-x86_64-"
         "unknown-linux-musl/bin/codex --codex-run-as-fs-helper", "helper"),
        ("codex", "codex", HOME + "/.nvm/versions/node/v25.9.0/lib/node_modules/@openai/codex/"
         "node_modules/@openai/codex-linux-x64/vendor/x86_64-unknown-linux-musl/bin/codex "
         "--codex-run-as-fs-helper", "helper"),
        ("codex", "codex", "codex-linux-sandbox --sandbox-policy-cwd " + HOME + "/w --command-cwd / "
         '--permission-profile {"type":"managed"} --apply-seccomp-then-exec -- '
         + CODEX_STANDALONE + " --codex-run-as-fs-helper", "sandbox"),
        ("codex", "mkdir", "mkdir -p " + HOME + "/.codex/packages/app-server-daemon", "helper"),
        ("codex", "sh", "sh -c printf '%b' " + HOME + "/.codex/x", "helper"),
        ("vscode", "code-4fe60c8b1c", HOME + "/.vscode-server/code-4fe60c8b1cdac1c4 --cli-data-dir "
         + HOME + "/.vscode-server/cli agent", "ide"),
        ("vscode", "node", HOME + "/.vscode-server/cli/servers/Stable-07f/server/node "
         "--dns-result-order=ipv4first " + HOME + "/.vscode-server/cli/servers/Stable-07f/server/"
         "out/bootstrap-fork --type=extensionHost", "ide"),
        ("vscode", "cpuUsage.sh", "/bin/bash " + HOME + "/.vscode-server/cli/servers/Stable-a44/"
         "server/out/vs/base/node/cpuUsage.sh 123", "helper"),
    ]

    def test_each_observed_shape_gets_its_role(self):
        for agent_type, command, args, want in self.CASES:
            with self.subTest(args=args[:70]):
                self.assertEqual(role_of(agent_type, command, args), want)

    def test_the_binaries_other_helpers_are_helpers_too(self):
        """Not seen on a node yet, but picked the same way: the rest of Codex's
        `--codex-run-as-*`, and the two other servers Claude 2.1.309's entry
        point runs in place of a session."""
        for agent_type, command, args in (
                ("codex", "codex", CODEX_STANDALONE + " --codex-run-as-apply-patch x"),
                ("claude_code", "claude", "claude --chrome-native-host"),
                ("claude_code", "2.1.309", HOME + "/.local/share/claude/versions/2.1.309 "
                 "--eval-mock-server /tmp/eval/spec.json n1")):
            with self.subTest(args=args[-40:]):
                self.assertEqual(role_of(agent_type, command, args), "helper")

    def test_a_helper_flag_counts_only_as_the_first_argument(self):
        """Both binaries take a helper from their first argument alone (Codex's
        argv[1], Claude's `process.argv[2]`), so the same flag further on -- in
        a prompt, say -- leaves the session a session."""
        self.assertEqual(role_of("codex", "codex", "codex exec why --codex-run-as-fs-helper"),
                         "agent")
        self.assertEqual(role_of("claude_code", "claude",
                                 "claude -p what is --claude-in-chrome-mcp"), "agent")


class SessionizeTest(unittest.TestCase):
    def setUp(self):
        self.t0 = time.time() - 600

    def test_a_claude_session_is_one_trajectory_not_one_per_tool_call(self):
        rows = ranked(reduce(claude_session(self.t0)))
        self.assertEqual(len(rows), 1, [r["sid"] for r in rows])
        row = rows[0]
        self.assertEqual(row["agent_pid"], "100")
        self.assertEqual(row["n_events"], 5)
        # the three raw keys: apid:100 (the Grep), apid:200 and apid:300 (the shells)
        self.assertEqual(row["fragments"], 3)

    def test_the_folded_chain_keeps_tool_calls_in_time_order(self):
        row = ranked(reduce(claude_session(self.t0)))[0]
        self.assertEqual([tok for tok, _ in row["tool_rle"]],
                         ["git", "zsh", "2.1.291", "ls", "zsh"])

    def test_each_run_carries_when_it_began(self):
        # The page opens the inspector at a run: the row says when each began.
        row = ranked(reduce(claude_session(self.t0)))[0]
        t = self.t0
        self.assertEqual(row["tool_t"], [round(x, 3) for x in (t + 10, t + 11, t + 20,
                                                               t + 30, t + 31)])
        self.assertEqual(len(row["purpose_t"]), len(row["purpose_rle"]))
        self.assertEqual(row["purpose_t"][0], round(t + 10, 3))
        self.assertEqual((row["first"], row["last"]), (round(t + 10, 3), round(t + 31, 3)))

    def test_run_times_are_capped_exactly_as_the_runs_are(self):
        t = self.t0
        recs = [rec("residency", t, 100, 50, "claude", "claude", agent_pid=100,
                    tree_root=100, is_agent=True)]
        recs += [rec("exit", t + 1 + i, 1000 + i, 100, "a" if i % 2 else "b", "x",
                     agent_pid=100, tree_root=100) for i in range(70)]
        row = ranked(reduce(recs))[0]
        self.assertEqual(len(row["tool_rle"]), 60)
        self.assertEqual(len(row["tool_t"]), 60)
        self.assertTrue(row["chain_truncated"])
        self.assertEqual(row["tool_t"][-1], round(t + 60, 3))

    def test_a_nested_agent_keeps_its_own_trajectory(self):
        # `claude rc` spawns a worker per remote session, an agent of its own
        # inside the host's tree, which must stay a session of its own.
        t0 = self.t0
        recs = [
            rec("residency", t0, 100, 50, "claude", "claude rc",
                agent_pid=100, tree_root=100, is_agent=True),
            rec("exit", t0 + 5, 160, 150, "git", "git log",
                agent_pid=150, tree_root=100),
            rec("exit", t0 + 6, 150, 100, "2.1.291",
                HOME + "/.local/share/claude/versions/2.1.291 --print --sdk-url "
                "https://api.anthropic.com/v1/code/sessions/cse_01",
                agent_pid=150, tree_root=100, is_agent=True),
            rec("exit", t0 + 8, 170, 100, "ls", "ls", agent_pid=100, tree_root=100),
        ]
        rows = ranked(reduce(recs))
        self.assertEqual(sorted(r["agent_pid"] for r in rows), ["100", "150"])

    def test_codexs_fs_helper_is_a_tool_call_of_codexs_session(self):
        # A sandboxed file access runs the Codex binary as its own fs helper,
        # under `codex-linux-sandbox`, and the classifier tags both. Read as an
        # agent, each helper was a session of its own: 6 699 of them on the FAS
        # login nodes from 2026-10-26 to 10-28.
        t0 = self.t0
        recs = [
            rec("residency", t0, 400, 50, "codex", CODEX_STANDALONE + " app-server --listen "
                "unix://", agent_type="codex", agent_pid=400, tree_root=400, is_agent=True),
            rec("exit", t0 + 5, 431, 430, "codex", CODEX_STANDALONE + " --codex-run-as-fs-helper",
                agent_type="codex", agent_pid=431, tree_root=400, is_agent=True),
            rec("exit", t0 + 6, 430, 400, "codex", "codex-linux-sandbox --sandbox-policy-cwd "
                + HOME + "/w --command-cwd / --apply-seccomp-then-exec -- " + CODEX_STANDALONE
                + " --codex-run-as-fs-helper",
                agent_type="codex", agent_pid=430, tree_root=400, is_agent=True),
        ]
        rows = ranked(reduce(recs))
        self.assertEqual([r["agent_pid"] for r in rows], ["400"])
        # the two keys, apid:430 and apid:431, walked up to Codex
        self.assertEqual((rows[0]["fragments"], rows[0]["owner_via"]), (2, "ancestor"))

    def test_a_tool_shell_whose_parent_was_never_seen_folds_into_that_parent(self):
        # The Bash tool's shell is spawned by the agent itself, so an unseen
        # parent of one IS the agent -- e.g. a Claude Desktop `ccd-cli` session,
        # whose API traffic rides the remote server's bridge, so it holds no
        # socket of its own and never appears in a `conn` record.
        t0 = self.t0
        recs = [
            rec("exit", t0 + 1, 201, 200, "git", "git status", agent_pid=200, tree_root=100),
            rec("exit", t0 + 2, 200, 90, "zsh", SNAP + "'git status'",
                agent_pid=200, tree_root=100, is_agent=True),
            rec("exit", t0 + 3, 301, 300, "ls", "ls", agent_pid=300, tree_root=100),
            rec("exit", t0 + 4, 300, 90, "zsh", SNAP + "'ls'",
                agent_pid=300, tree_root=100, is_agent=True),
        ]
        rows = ranked(reduce(recs))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["agent_pid"], "90")
        self.assertEqual(rows[0]["owner_via"], "parent")

    def test_a_helper_whose_parent_was_never_seen_folds_into_the_tree_root(self):
        t0 = self.t0
        recs = [
            rec("exit", t0 + 1, 201, 200, "ls", "ls -1", agent_pid=200, tree_root=100),
            # a generic helper, and its parent, pid 90, has no record in the window
            rec("exit", t0 + 2, 200, 90, "sh", "sh -c ls -1 " + HOME + "/.claude/projects/x",
                agent_pid=200, tree_root=100, is_agent=True),
            rec("exit", t0 + 3, 220, 100, "ls", "ls", agent_pid=100, tree_root=100),
        ]
        rows = ranked(reduce(recs))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["agent_pid"], "100")
        self.assertEqual(rows[0]["owner_via"], "tree_root")

    def test_a_key_whose_process_was_never_seen_stays_its_own_session(self):
        # Nothing says what pid 200 is -- it may still be running -- so its
        # children are not guessed into anything.
        t0 = self.t0
        recs = claude_session(t0)[:1] + [
            rec("exit", t0 + 5, 201, 200, "git", "git status", agent_pid=200, tree_root=100),
        ]
        rows = ranked(reduce(recs))
        self.assertEqual(sorted(r["agent_pid"] for r in rows), ["200"])
        self.assertEqual(rows[0]["owner_via"], "unknown")

    def test_the_fold_is_reported(self):
        t = reduce(claude_session(self.t0))["trajectories"]
        self.assertEqual(t["fold"]["raw_sessions"], 3)
        self.assertEqual(t["fold"]["sessions"], 1)

    def test_sandbox_session_counts_do_not_count_tool_calls_as_sessions(self):
        sb = reduce(claude_session(self.t0))["sandbox"]
        self.assertEqual(sb["n_sessions"].get("agent"), 1)
        self.assertEqual(sb["tool_calls"]["agent"], {"not_isolated": 2})

    def test_a_session_is_judged_by_its_tool_calls_not_its_owner(self):
        """The owner's own records (pid 210, its Grep) are outside the sandbox
        by design; only the tool calls say whether the session was confined.
        One of two confined is `partial`, both is `sandboxed` -- even though the
        owner's own key reads unsandboxed."""
        recs = claude_session(self.t0)
        for r in recs:
            if r["pid"] in (200, 201):          # the first tool call was confined
                r["sandbox"], r["sandbox_src"] = "sandboxed", "ns"
        sb = reduce(recs)["sandbox"]
        self.assertEqual(sb["by_class_sessions"]["agent"], {"partial": 1})
        for r in recs:
            if r["pid"] in (300, 301):
                r["sandbox"], r["sandbox_src"] = "sandboxed", "ns"
        sb = reduce(recs)["sandbox"]
        self.assertEqual(sb["by_class_sessions"]["agent"], {"sandboxed": 1})


if __name__ == "__main__":
    unittest.main()
