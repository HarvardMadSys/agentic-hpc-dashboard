"""The three classes: agent, human, IDE -- and which processes land in IDE.

    cd dashboard && .venv/bin/python -m unittest discover -s tests -q
"""
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rc_dashboard import config as configmod                        # noqa: E402
from rc_dashboard.export import parse_filters                       # noqa: E402
from rc_dashboard.normalize import Normalizer                       # noqa: E402

HOME = "/home/alice"


def norm():
    return Normalizer(configmod.Config(configmod.defaults(), {}, None))


def rec(actor3, agent_type=None, args="bash", schema=7, actor=None):
    r = {"schema_version": schema, "event": "exit", "ts_epoch": time.time() - 60,
         "host": "h1", "user": "alice", "pid": 10, "ppid": 1, "command": args.split()[0],
         "args": args, "agent_type": agent_type}
    if actor3 is not ...:
        r["actor3"] = actor3
    if actor is not None:
        r["actor"] = actor
    return r


def a3(r):
    return norm().normalize(r)["_a3"]


class IdeClassTest(unittest.TestCase):

    def test_the_collectors_vscode_class_is_the_ide_class(self):
        self.assertEqual(a3(rec("human-vscode", "vscode")), "ide")

    def test_cursor_and_windsurf_are_ides_though_the_collector_says_agent(self):
        for t in ("cursor", "windsurf"):
            with self.subTest(agent_type=t):
                self.assertEqual(a3(rec("agent", t, HOME + "/.%s-server/bin/x/node" % t)), "ide")

    def test_an_agent_cli_inside_an_ide_terminal_stays_an_agent(self):
        args = "node %s/.cursor-server/extensions/claude-code/cli.js" % HOME
        self.assertEqual(a3(rec("agent", "claude_code", args)), "agent")

    def test_an_untagged_editor_server_is_an_ide_by_its_own_path(self):
        for args in (HOME + "/.zed_server/zed-remote-server-stable proxy",
                     HOME + "/.cache/JetBrains/RemoteDev/dist/x/bin/remote-dev-server.sh run",
                     HOME + "/.positron-server/bin/abc/node out/server-main.js"):
            with self.subTest(args=args[:40]):
                self.assertEqual(a3(rec("human", None, args)), "ide")

    def test_a_person_mentioning_an_ide_path_is_still_a_person(self):
        self.assertEqual(a3(rec("human", None, "ls %s/.zed_server/" % HOME)), "human")
        self.assertEqual(a3(rec("human", None, "rm -rf %s/.cursor-server" % HOME)), "human")

    def test_a_record_from_before_actor3_gets_the_same_classes(self):
        self.assertEqual(a3(rec(..., "cursor", actor="agent", schema=3)), "ide")
        self.assertEqual(a3(rec(..., "vscode", actor="agent", schema=3)), "ide")
        self.assertEqual(a3(rec(..., "codex", actor="agent", schema=3)), "agent")

    def test_the_default_classes_are_agent_human_ide(self):
        c = configmod.Config(configmod.defaults(), {}, None)
        self.assertEqual(list(c.get("filters.classes")), ["agent", "human", "ide"])

    def test_an_old_link_filtering_on_human_vscode_still_works(self):
        self.assertEqual(parse_filters({"class": "human-vscode,agent"})["class"], {"ide", "agent"})


class TickTotalsTest(unittest.TestCase):

    def test_a_ticks_class_totals_use_the_ide_class_and_move_cursor_out_of_agent(self):
        r = {"schema_version": 7, "event": "residency_totals", "ts_epoch": time.time() - 60,
             "host": "h1",
             "by_actor3": {"agent": {"n_procs": 10, "rss_mb": 1000.0, "users": 3},
                           "human-vscode": {"n_procs": 4, "rss_mb": 200.0, "users": 2},
                           "human": {"n_procs": 5, "rss_mb": 50.0, "users": 4}},
             "by_agent_type": {"cursor": {"n_procs": 3, "rss_mb": 300.0, "users": 1},
                               "vscode": {"n_procs": 4, "rss_mb": 200.0, "users": 2},
                               "claude_code": {"n_procs": 7, "rss_mb": 700.0, "users": 2}},
             "tcp_open_ext_by_actor3": {"agent": 2, "human-vscode": 1}}
        out = norm().normalize(r)
        by = out["_by_class"]
        self.assertEqual(by["agent"]["n_procs"], 7)
        self.assertEqual(by["agent"]["rss_mb"], 700.0)
        self.assertEqual(by["ide"]["n_procs"], 4 + 3)        # VS Code as before, plus Cursor
        self.assertEqual(by["ide"]["rss_mb"], 500.0)
        self.assertEqual(by["human"]["n_procs"], 5)
        self.assertEqual(out["_tcp_by_class"], {"agent": 2, "ide": 1})
        self.assertEqual(out["by_actor3"]["agent"]["n_procs"], 10)   # the raw record is untouched


if __name__ == "__main__":
    unittest.main()
