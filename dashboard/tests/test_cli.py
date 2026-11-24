"""The command line (rc_dashboard/__main__.py): a parser per command, each flag
with its help, the flags of one command only under it -- and every invocation
that worked before still working: a flag before or after the command, no command
for `serve`, `--check-feeds` and `--print-config` as flags, the retired
`--sacct-dir` and `--jobs-csv`, and what deploy/, hpc/ and run*.sh run.

    cd dashboard && .venv/bin/python -m unittest tests.test_cli -q
"""
import argparse
import io
import os
import re
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rc_dashboard import __main__ as cli            # noqa: E402
from rc_dashboard import about                      # noqa: E402
from rc_dashboard import config as configmod        # noqa: E402

REPO = Path(__file__).resolve().parents[2]
SUBCOMMAND_ONLY = {"--dir": ["new-password"], "--netrc-host": ["new-password"],
                   "--rollup-days": ["rollup"], "--install": ["new-password"],
                   "--demo-dir": ["demo"], "--demo-days": ["demo"], "--demo-hosts": ["demo"],
                   "--demo-seed": ["demo"], "--demo-fresh": ["demo"],
                   "--demo-write-only": ["demo"],
                   "--force": ["init", "new-password", "backup", "restore", "rollup"]}


def parse(*argv):
    return cli.build_parser().parse_args(list(argv))


def subparsers():
    ap = cli.build_parser()
    sub = next(a for a in ap._actions if isinstance(a, argparse._SubParsersAction))
    return ap, sub.choices


class Arrange(unittest.TestCase):
    def test_no_command_is_serve(self):
        self.assertEqual(parse().command, "serve")
        a = parse("--port", "9000")
        self.assertEqual((a.command, a.port), ("serve", 9000))

    def test_a_flag_before_or_after_the_command(self):
        for argv in (["--config", "x.toml", "check-feeds"], ["check-feeds", "--config", "x.toml"],
                     ["--config=x.toml", "check-feeds"]):
            with self.subTest(argv=argv):
                a = parse(*argv)
                self.assertEqual((a.command, a.config), ("check-feeds", "x.toml"))
        # a value that looks like a command is a value
        a = parse("--as-of", "serve", "status")
        self.assertEqual((a.command, a.as_of), ("status", "serve"))
        # a command's own flag before it, as when every flag was global
        a = parse("--force", "--rollup-days", "30", "rollup")
        self.assertEqual((a.command, a.force, a.rollup_days), ("rollup", True, 30))
        a = parse("--demo-days", "2", "demo")
        self.assertEqual((a.command, a.demo_days), ("demo", 2))
        a = parse("--ebpf-root", "/a", "serve", "--ebpf-root", "/b")
        self.assertEqual(a.ebpf_root, ["/a", "/b"])

    def test_the_hidden_flags(self):
        a = parse("--check-feeds")
        self.assertEqual((a.command, a.check_feeds), ("serve", True))
        a = parse("demo", "--print-config", "--demo-dir", "/d")
        self.assertEqual((a.command, a.print_config, a.demo_dir), ("demo", True, "/d"))

    def test_a_word_that_is_no_command_is_refused(self):
        err = io.StringIO()
        with redirect_stderr(err), self.assertRaises(SystemExit) as cm:
            parse("serv")
        self.assertEqual(cm.exception.code, 2)
        self.assertIn("invalid choice: 'serv'", err.getvalue())

    def test_a_flag_another_command_owns_is_refused(self):
        err = io.StringIO()
        with redirect_stderr(err), self.assertRaises(SystemExit):
            parse("serve", "--rollup-days", "3")
        self.assertIn("--rollup-days", err.getvalue())


class Help(unittest.TestCase):
    def test_every_flag_has_help_and_every_command_examples(self):
        ap, cmds = subparsers()
        for name, p in cmds.items():
            self.assertTrue(p.description, name)
            self.assertIn("examples:", p.epilog or "", name)
            for a in p._actions:
                if a.option_strings and a.help is not argparse.SUPPRESS:
                    with self.subTest(command=name, flag=a.option_strings):
                        self.assertTrue(a.help and len(a.help) > 10)
        top = [a for a in ap._actions if a.option_strings]
        self.assertTrue(all(a.help for a in top))

    def test_a_commands_flags_show_only_under_it(self):
        ap, cmds = subparsers()
        top = ap.format_help()
        for flag, owners in SUBCOMMAND_ONLY.items():
            self.assertNotIn(flag, top)
            for name, p in cmds.items():
                with self.subTest(flag=flag, command=name):
                    shown = re.search(r"(^|\s)%s(\s|,|$)" % re.escape(flag), p.format_help())
                    self.assertEqual(bool(shown), name in owners)
        self.assertLess(len([l for l in top.splitlines() if l.strip().startswith("--")]), 3)

    def test_the_retired_and_hidden_flags_are_not_shown(self):
        _, cmds = subparsers()
        for name, p in cmds.items():
            text = p.format_help()
            for flag in ("--sacct-dir", "--jobs-csv", "--check-feeds", "--print-config"):
                self.assertNotIn(flag, text, name)

    def test_help_exits_0(self):
        for argv in (["--help"], ["serve", "--help"], ["--config", "x", "--help"],
                     ["status", "-h"]):
            with self.subTest(argv=argv):
                out = io.StringIO()
                with redirect_stdout(out), self.assertRaises(SystemExit) as cm:
                    parse(*argv)
                self.assertEqual(cm.exception.code, 0)
                self.assertIn("usage: rc-dashboard", out.getvalue())


class Version(unittest.TestCase):
    def test_the_flag_and_the_command(self):
        line = about.version_line()
        self.assertRegex(line, r"^rc-dashboard \d+\.\d+\.\d+( \([0-9a-f]{4,12}\))?$")
        for argv in (["--version"], ["serve", "--version"]):
            out = io.StringIO()
            with redirect_stdout(out), self.assertRaises(SystemExit) as cm:
                parse(*argv)
            self.assertEqual((cm.exception.code, out.getvalue()), (0, line + "\n"))
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(cli.main(["version"]), 0)
        self.assertEqual(out.getvalue(), line + "\n")


class WhatDeploymentsRun(unittest.TestCase):
    """Every way deploy/, hpc/, docker/, bench/ and dashboard/run*.sh start it parses."""

    STOP = ("&&", "||", "|", ";")

    def invocations(self):
        out = set()
        for base in ("deploy", "hpc", "docker", "dashboard/bench"):
            for p in (REPO / base).rglob("*"):
                if not p.is_file() or "node_modules" in p.parts or "tests" in p.parts:
                    continue
                if p.suffix not in (".sh", ".in", ".py", ".md", ".example", ""):
                    continue
                try:
                    text = p.read_text(errors="replace").replace("\\\n", " ")
                except OSError:
                    continue
                for m in re.finditer(r"-m rc_dashboard(?=[\s\"'`)]|$)([^\n`]*)", text):
                    words = []
                    for w in m.group(1).split():
                        if w in self.STOP or w.startswith((">", "<", "2>", "1>", "&", ")")):
                            break
                        last = w.endswith(")")            # cmd=(python -m rc_dashboard serve)
                        w = w.rstrip(")").strip("\"'")
                        if w:
                            words.append(w)
                        if last:
                            break
                    out.add(tuple(words))
        for p in (REPO / "dashboard").glob("run*.sh"):
            for cmd in ("serve", "demo"):          # RC_DASH_COMMAND
                out.add((cmd,))
        return out

    def test_each_parses(self):
        found = self.invocations()
        self.assertIn(("serve",), found)
        _ap, cmds = subparsers()
        takes = {o for p in cmds.values() for a in p._actions if a.nargs != 0
                 for o in a.option_strings}
        for words in found:
            argv = []
            for w in words:
                if "$" in w:                        # a shell variable: a flag's value, or args
                    if argv and argv[-1] in takes:
                        argv.append("X")
                    continue
                argv.append(w)
            with self.subTest(argv=" ".join(words)):
                if argv and argv[0].startswith("."):          # rc_dashboard.feedparquet
                    continue
                parse(*argv)


class Serve(unittest.TestCase):
    def test_the_refusal_names_the_hash_file_first(self):
        msg = cli.NO_PASSWORD % {"port": 8080}
        self.assertLess(msg.index("password_hash_file"), msg.index('"password_hash": "<hash>"'))
        self.assertIn("RC_DASH_PASSWORD_HASH_FILE", msg)
        self.assertIn("new-password --install", msg)

    def test_a_hash_file_is_a_password(self):
        tmp = tempfile.mkdtemp(prefix="rc-cli-")
        self.addCleanup(shutil.rmtree, tmp, True)
        tree = configmod.defaults()
        tree["server"]["auth"]["password_hash_file"] = os.path.join(tmp, "pw.hash")
        self.assertIsNone(cli.refusal(configmod.Config(tree, {}, None)))
        tree["server"]["auth"]["password_hash"] = "scrypt$x"
        with self.assertRaises(SystemExit) as cm:
            cli.refusal(configmod.Config(tree, {}, None))
        self.assertIn("are both set", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
