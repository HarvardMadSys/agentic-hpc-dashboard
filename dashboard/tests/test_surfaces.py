"""Launch surface: where an agent session is driven from, read off its own argv.

Five surfaces in two groups, plus `unknown`:

* CLI -- `terminal` (a person at the agent's own interactive UI) and `script`
  (a program drives it: `claude -p`, the Agent SDK, `codex exec`, an MCP server)
* app -- `desktop` (the Claude desktop app's Code tab or the Codex app, over
  SSH), `extension` (an IDE extension's bundled binary) and `web` (Remote
  Control, from a browser or a phone)

The argv shapes are the ones a census of rtx6000a (2026-10-10 .. 10-29) and eight
FAS login nodes (2026-10-26 .. 10-28) found, home directories shortened.

    cd dashboard && uv run python -m unittest discover -s tests -q
"""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_sessions import HOME, SNAP, cfg, rec, reduce      # noqa: E402
from rc_dashboard.normalize import Normalizer, surface_of     # noqa: E402
from rc_dashboard.reducers.live import LiveReducer           # noqa: E402

CCD = (HOME + "/.claude/remote/ccd-cli/2.1.308-3f6ee6c72f6a --output-format stream-json "
       "--verbose --input-format stream-json --effort=max --model=claude-opus-5-5")
SRV = (HOME + "/.claude/remote/srv/be1c1ca50b7b27c5f9341e4cbabc2039588fe597/server --serve "
       "--socket " + HOME + "/.claude/remote/run/1c590ad2/rpc.sock")
VSCODE_CLAUDE = (HOME + "/.vscode-server/extensions/anthropic.claude-code-2.1.308-linux-x64/"
                 "resources/native-binary/claude --output-format stream-json --verbose "
                 "--input-format stream-json")
DAEMON = HOME + "/.codex/packages/app-server-daemon/releases/0.165.0-x86_64-unknown-linux-musl/bin/codex"


class SurfaceOfTest(unittest.TestCase):
    """One agent root's own argv, and the surface it shows."""

    CASES = [
        # (agent_type, command, args, surface, detail)
        ("claude_code", "claude", "claude", "terminal", "interactive"),
        ("claude_code", "2.1.307", HOME + "/.local/share/claude/versions/2.1.307 --session-id "
         "0aae9d7d --fork-session --resume " + HOME + "/.claude/projects/x.jsonl",
         "terminal", "interactive"),
        # an interactive session with Remote Control switched on is still run from its terminal
        ("claude_code", "claude", "claude --rc", "terminal", "interactive"),
        ("claude_code", "claude", "claude bg-spare --bg-spare /tmp/cc-daemon-2004/18ee9691/spare/"
         "9f243a62.claim.sock", "terminal", "background session"),
        ("claude_code", "2.1.308-3f6ee6c72f6a", CCD, "desktop", "Claude desktop app"),
        ("claude_code", "claude", VSCODE_CLAUDE, "extension", "VS Code"),
        ("claude_code", "claude", HOME + "/.cursor-server/extensions/anthropic.claude-code-2.1.300"
         "-linux-x64/resources/native-binary/claude --output-format stream-json", "extension",
         "Cursor"),
        ("claude_code", "2.1.291", HOME + "/.local/share/claude/versions/2.1.291 --print --sdk-url "
         "https://api.anthropic.com/v1/code/sessions/cse_01 --session-id cse_01", "web",
         "Claude Remote Control"),
        ("claude_code", "claude", HOME + "/.local/bin/claude -p --resume 40ef1996 ORCH: read the note",
         "script", "claude -p"),
        ("claude_code", "claude", "claude --permission-mode auto --output-format stream-json "
         "--verbose --append-system-prompt-file " + HOME + "/sysprompt.md", "script", "Agent SDK"),
        ("claude_code", "claude", HOME + "/kemp/node_modules/@anthropic-ai/claude-agent-sdk-linux-x64"
         "/claude --output-format stream-json --verbose --input-format stream-json",
         "script", "Agent SDK"),
        ("claude_code", "claude", "claude mcp serve", "script", "MCP server"),
        ("codex", "codex", "codex", "terminal", "interactive"),
        ("codex", "codex", HOME + "/.local/bin/codex resume --yolo", "terminal", "interactive"),
        # a word of the prompt is not a subcommand
        ("codex", "codex", "codex please exec the tests", "terminal", "interactive"),
        ("codex", "codex", "codex exec --skip-git-repo-check fix the failing test",
         "script", "codex exec"),
        # a `codex` that is a script: the kernel puts its #! interpreter first
        ("codex", "codex", "/bin/bash " + HOME + "/stubs/codex exec --json fix it",
         "script", "codex exec"),
        ("codex", "node", "node /tmp/massclaw-sdk-P1R1Wk/codex exec --experimental-json "
         "--config forced_login_method=chatgpt", "script", "Codex SDK"),
        ("codex", "codex", HOME + "/.npm-global/lib/node_modules/@openai/codex/node_modules/@openai/"
         "codex-linux-x64/vendor/x86_64-unknown-linux-musl/bin/codex mcp-server",
         "script", "MCP server"),
        # the Codex app's Remote SSH: its login shell's `codex`, a unix socket, code mode on
        ("codex", "codex", "codex -c features.code_mode_host=true app-server --listen unix://",
         "desktop", "Codex app"),
        ("codex", "node", "node " + HOME + "/.nvm/versions/node/v22.22.2/bin/codex -c "
         "features.code_mode_host=true app-server --listen unix://", "desktop", "Codex app"),
        ("codex", "codex", HOME + "/.vscode-server/extensions/openai.chatgpt-26.1007.21434-linux-x64"
         "/bin/linux-x86_64/codex -c features.code_mode_host=true app-server "
         "--analytics-default-enabled", "extension", "VS Code"),
        ("codex", "codex", HOME + "/.codex/packages/standalone/releases/0.165.0-x86_64-unknown-"
         "linux-musl/bin/codex app-server --remote-control --listen unix:// "
         "--analytics-default-enabled --managed-daemon", "web", "Codex Remote Control"),
        # one daemon serves whichever client attaches: its own argv names none
        ("codex", "codex", DAEMON + " app-server --listen unix:// --analytics-default-enabled "
         "--managed-daemon", "unknown", "shared app server"),
        ("codex", "codex", HOME + "/kemp/node_modules/@openai/codex-linux-x64/vendor/x86_64-unknown-"
         "linux-musl/bin/codex app-server --listen stdio://", "unknown", "app server"),
    ]

    def test_each_launch_shape(self):
        for at, command, args, surface, detail in self.CASES:
            with self.subTest(args=args[:70]):
                r = {"agent_type": at, "_command": command, "args": args}
                self.assertEqual(surface_of(r, "agent", "agent"), (surface, detail))

    def test_an_extensions_binary_run_by_hand_is_not_the_extension(self):
        """Where the binary is installed does not say who runs it: two users run
        the VS Code extension's `codex exec` from a terminal (66 of 67 with a
        tty), and the extension itself never starts an interactive UI."""
        codex = HOME + ("/.vscode-server/extensions/openai.chatgpt-26.1007.21434-linux-x64/bin/"
                        "linux-x86_64/codex")
        claude = HOME + ("/.vscode-server/extensions/anthropic.claude-code-2.1.289-linux-x64/"
                         "resources/native-binary/claude")
        for at, args, tty, want in [
            ("codex", codex + " exec --skip-git-repo-check -- review it", "pts/4",
             ("script", "codex exec")),
            ("claude_code", claude + " -p Reply with the single word OK.", None,
             ("script", "claude -p")),
            ("claude_code", claude, "pts/7", ("terminal", "interactive")),
            # no mode and no terminal: the extension started it
            ("claude_code", claude + " --claude-in-chrome-mcp", None, ("extension", "VS Code")),
        ]:
            with self.subTest(args=args[-40:]):
                r = {"agent_type": at, "_command": args.split()[0].rsplit("/", 1)[-1],
                     "args": args, "tty": tty}
                self.assertEqual(surface_of(r, "agent", "agent"), want)

    def test_a_host_says_where_its_sessions_are_driven_from(self):
        """A process that hosts sessions is not one, but its own argv names
        their client: a trajectory row keyed on the desktop app's server holds
        thousands of its processes (rtx6000a, 2026-10-28)."""
        for at, command, args, want in [
            ("claude_code", "server", SRV, ("desktop", "Claude desktop app")),
            ("claude_code", "claude", "claude rc", ("web", "Claude Remote Control")),
            ("claude_code", "claude", HOME + "/.local/bin/claude daemon run --origin transient "
             '--spawned-by {"label":"claude","pid":244703}', ("terminal", "background session")),
            ("claude_code", "claude", "claude bg-pty-host --bg-pty-host /tmp/cc-daemon-2004/18ee9691/"
             "pty/0aae9d7d.sock 154 44", ("terminal", "background session")),
            ("codex", "codex", "codex remote-control start", ("web", "Codex Remote Control")),
            ("codex", "codex", DAEMON + " app-server daemon pid-update-loop",
             ("unknown", "shared app server")),
        ]:
            with self.subTest(args=args[:60]):
                r = {"agent_type": at, "_command": command, "args": args}
                self.assertEqual(surface_of(r, "infra", "agent"), want)

    def test_who_has_no_surface(self):
        # a tool call's shell, an untagged child, and an IDE's own processes are
        # not sessions: their session's surface is on its root
        self.assertEqual(surface_of({"agent_type": "claude_code", "_command": "zsh",
                                     "args": SNAP + "'ls'"}, "tool-shell", "agent"), (None, None))
        self.assertEqual(surface_of({"agent_type": "claude_code", "_command": "git",
                                     "args": "git status"}, None, "agent"), (None, None))
        self.assertEqual(surface_of({"agent_type": "cursor", "_command": "cursor-agent",
                                     "args": "cursor-agent --force"}, "agent", "ide"), (None, None))
        # a client attaches to a session it does not run: its session's root says
        self.assertEqual(surface_of({"agent_type": "codex", "_command": "codex",
                                     "args": "codex app-server proxy"}, "client", "agent"),
                         (None, None))


def desktop_session(t0, root=100, server=90):
    """A Claude desktop session: the remote server, its ccd-cli, and one Bash call."""
    return [
        rec("residency", t0, server, 1, "server", SRV, agent_pid=server, tree_root=server,
            is_agent=True),
        rec("exit", t0 + 10, root + 101, root + 100, "git", "git status",
            agent_pid=root + 100, tree_root=server),
        rec("exit", t0 + 11, root + 100, root, "zsh", SNAP + "'git status'",
            agent_pid=root + 100, tree_root=server, is_agent=True),
        rec("exit", t0 + 60, root, server, "2.1.308-3f6ee6c72f6a", CCD,
            agent_pid=root, tree_root=server, is_agent=True),
    ]


def codex_tui_session(t0, tui=300, server=310):
    """`codex --yolo` starts the app-server daemon, which runs the session's calls."""
    return [
        rec("residency", t0, tui, 50, "codex", "codex --yolo", agent_pid=tui, tree_root=tui,
            is_agent=True, agent_type="codex"),
        rec("conn", t0 + 1, server, tui, "codex", DAEMON + " app-server --listen unix:// "
            "--analytics-default-enabled --managed-daemon", agent_pid=server, tree_root=tui,
            is_agent=True, agent_type="codex"),
        rec("exit", t0 + 5, server + 1, server, "git", "git diff", agent_pid=server,
            tree_root=tui, agent_type="codex"),
    ]


def row_of(payload, pid):
    t = payload["trajectories"]
    rows = [r for r in t["rows"].values() if r["agent_pid"] == str(pid)]
    if len(rows) != 1:
        raise AssertionError("%d rows for agent pid %s" % (len(rows), pid))
    return rows[0]


def node_of(payload, pid):
    for t in payload["cascades"]["trees"]:
        for n in t["nodes"]:
            if n["pid"] == pid:
                return n
    raise AssertionError("no agent node with pid %s" % pid)


class SurfaceGrainTest(unittest.TestCase):
    """A session's surface is its root's, whichever record arrives first."""

    def setUp(self):
        self.t0 = time.time() - 600

    def test_a_trajectory_row_carries_its_roots_surface(self):
        r = row_of(reduce(desktop_session(self.t0)), 100)
        self.assertEqual((r["surface"], r["surface_detail"]), ("desktop", "Claude desktop app"))

    def test_the_roots_surface_wins_whatever_the_order(self):
        recs = desktop_session(self.t0)
        root_first = [recs[0], recs[-1]] + recs[1:-1]
        self.assertEqual(row_of(reduce(root_first), 100)["surface"], "desktop")

    def test_a_session_whose_root_was_never_seen_is_unknown(self):
        recs = desktop_session(self.t0)[:-1]
        self.assertEqual(row_of(reduce(recs), 100)["surface"], "unknown")

    def test_an_agent_node_carries_its_surface(self):
        n = node_of(reduce(desktop_session(self.t0)), 100)
        self.assertEqual((n["surface"], n["surface_detail"]), ("desktop", "Claude desktop app"))

    def test_the_desktop_apps_server_is_the_desktop_app(self):
        n = node_of(reduce(desktop_session(self.t0)), 90)
        self.assertEqual((n["role"], n["surface"]), ("infra", "desktop"))

    def test_a_row_keyed_on_the_desktop_apps_server_is_the_desktop_app(self):
        # the server runs a git of its own: nothing above it to fold into
        recs = desktop_session(self.t0) + [
            rec("exit", self.t0 + 20, 95, 90, "git", "git status --porcelain",
                agent_pid=90, tree_root=90)]
        r = row_of(reduce(recs), 90)
        self.assertEqual((r["surface"], r["surface_detail"]), ("desktop", "Claude desktop app"))
        # it hosts sessions and is not one: the plate does not count it
        s = reduce(recs)["trajectories"]["summary"]["agent"]
        self.assertEqual(s["by_surface"], {"desktop": 1})

    def test_a_codex_daemon_started_by_the_terminal_ui_is_the_terminals(self):
        p = reduce(codex_tui_session(self.t0))
        self.assertEqual(node_of(p, 310)["surface"], "terminal")
        self.assertEqual(row_of(p, 310)["surface"], "terminal")

    def test_a_codex_daemon_reparented_to_init_is_still_its_starters(self):
        # its terminal UI exits first; the daemon's later ticks name init
        recs = codex_tui_session(self.t0)
        recs.append(dict(recs[1], ts_epoch=self.t0 + 9, ppid=1))
        p = reduce(recs)
        self.assertEqual(node_of(p, 310)["surface"], "terminal")
        self.assertEqual(row_of(p, 310)["surface"], "terminal")

    def test_a_codex_daemon_no_terminal_started_stays_unknown(self):
        recs = codex_tui_session(self.t0)
        recs[1] = dict(recs[1], ppid=1)
        p = reduce(recs[1:])
        self.assertEqual(node_of(p, 310)["surface"], "unknown")
        self.assertEqual(row_of(p, 310)["surface"], "unknown")

    def test_the_plate_counts_sessions_by_surface(self):
        recs = desktop_session(self.t0) + [
            rec("exit", self.t0 + 30, 501, 500, "ls", "ls", agent_pid=500, tree_root=500),
            rec("exit", self.t0 + 40, 500, 50, "claude", "claude", agent_pid=500, tree_root=500,
                is_agent=True),
        ]
        s = reduce(recs)["trajectories"]["summary"]["agent"]
        self.assertEqual(s["by_surface"], {"desktop": 1, "terminal": 1})

    def test_a_running_agent_in_the_live_users_carries_its_surface(self):
        # the desktop server is its tree's root; the ccd-cli session runs inside it
        now = time.time()
        norm, live = Normalizer(cfg()), LiveReducer(cfg(), ["agent", "human", "ide"])
        for raw in [rec("residency", now - 30, 90, 1, "server", SRV, agent_pid=90,
                        tree_root=90, is_agent=True, n_procs=5),
                    rec("conn", now - 20, 100, 90, "2.1.308-3f6ee6c72f6a", CCD,
                        agent_pid=100, tree_root=90, is_agent=True)]:
            live.feed(norm.normalize(raw))
        d = live.user_detail("alice", now=now, window_s=3600)
        self.assertEqual([(a["pid"], a["surface"]) for s in d["sessions"] for a in s["agents"]],
                         [(100, "desktop")])
        # and the server's own row says it for the sessions it holds
        self.assertEqual([s["surface"] for s in d["sessions"]], ["desktop"])

    def test_a_codex_daemon_in_the_live_users_is_its_terminals(self):
        now = time.time()
        norm, live = Normalizer(cfg()), LiveReducer(cfg(), ["agent", "human", "ide"])
        for raw in codex_tui_session(now - 30)[:2]:
            live.feed(norm.normalize(raw))
        d = live.user_detail("alice", now=now, window_s=3600)
        self.assertEqual([(s["surface"], [(a["pid"], a["surface"]) for a in s["agents"]])
                          for s in d["sessions"]], [("terminal", [(310, "terminal")])])

    def test_a_terminal_session_in_the_live_users_carries_its_surface(self):
        # the agent is its own tree's root: the session row speaks for it
        now = time.time()
        norm, live = Normalizer(cfg()), LiveReducer(cfg(), ["agent", "human", "ide"])
        for raw in [rec("residency", now - 30, 100, 50, "claude", "claude", agent_pid=100,
                        tree_root=100, is_agent=True, n_procs=3)]:
            live.feed(norm.normalize(raw))
        d = live.user_detail("alice", now=now, window_s=3600)
        self.assertEqual([s["surface"] for s in d["sessions"]], ["terminal"])


if __name__ == "__main__":
    unittest.main()
