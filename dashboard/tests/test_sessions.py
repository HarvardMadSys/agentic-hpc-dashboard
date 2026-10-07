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


class RoleTest(unittest.TestCase):
    """What a classifier-tagged process actually is, on argv shapes seen on madsys-gpu1."""

    CASES = [
        # (agent_type, command, args, role)
        ("claude_code", "claude", "claude", "agent"),
        ("claude_code", "claude", "claude --rc", "agent"),
        ("claude_code", "claude", "claude rc", "agent"),
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

    def test_a_nested_agent_keeps_its_own_trajectory(self):
        # `claude rc` spawns a worker per remote session: a real agent under an
        # agent, which must stay a session of its own.
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

    def test_sandbox_tree_counts_do_not_count_tool_calls_as_trees(self):
        sb = reduce(claude_session(self.t0))["sandbox"]
        self.assertEqual(sb["n_trees"].get("agent"), 1)

    def test_a_folded_tree_keeps_a_known_verdict_over_its_owners_unknown(self):
        recs = claude_session(self.t0)
        for r in recs:
            if r["pid"] in (200, 201):          # the first tool call was confined
                r["sandbox"], r["sandbox_src"] = "sandboxed", "ns"
            elif r["pid"] == 210:               # the owner's own key could not be read
                r.pop("sandbox"), r.pop("sandbox_src")
        sb = reduce(recs)["sandbox"]
        self.assertEqual(sb["by_class_trees"]["agent"], {"sandboxed": 1})


if __name__ == "__main__":
    unittest.main()
