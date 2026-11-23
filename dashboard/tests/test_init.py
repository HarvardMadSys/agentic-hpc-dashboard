"""`rc-dashboard init` (rc_dashboard/init_cmd.py): what it finds, what it asks,
what it writes, what it never replaces, and the commands it prints.

    cd dashboard && .venv/bin/python -m unittest tests.test_init -q
"""
import argparse
import io
import json
import os
import shutil
import stat
import sys
import tempfile
import tomllib
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rc_dashboard import __main__ as cli            # noqa: E402
from rc_dashboard import auth                       # noqa: E402
from rc_dashboard import config as configmod        # noqa: E402
from rc_dashboard import init_cmd                   # noqa: E402

DAY = "2026-11-20"


def args(**kw):
    base = dict(yes=True, ebpf_root=None, node_root=None, site_name=None, slurm=None,
                config=None, password_dir=None, port=None, force=False)
    base.update(kw)
    return argparse.Namespace(**base)


class Init(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rc-init-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        env = mock.patch.dict(os.environ, {k: v for k, v in os.environ.items()
                                           if not k.startswith(("RC_DASH_", "EBPFM_"))},
                              clear=True)
        env.start()
        self.addCleanup(env.stop)
        for name, val in (("HERE", self.path("dash")), ("REPO", self.tmp),
                          ("EBPFM_DEFAULTS", self.path("etc-default-ebpfm"))):
            p = mock.patch.object(configmod, name, val)
            p.start()
            self.addCleanup(p.stop)
        os.makedirs(self.path("dash"))
        # the collector's folder, named where its unit's settings name it
        d = self.path("scratch", "ebpfm", DAY)
        os.makedirs(d)
        with open(os.path.join(d, "h1.exits.jsonl"), "w") as fh:
            fh.write(json.dumps({"ts": DAY + " 10:00:00", "host": "h1", "event": "exit"}) + "\n")
        with open(self.path("etc-default-ebpfm"), "w") as fh:
            fh.write("EBPFM_OUTPUT_DIR=%s\n" % self.path("scratch", "ebpfm"))

    def path(self, *p):
        return os.path.join(self.tmp, *p)

    def run_init(self, a, answers=None):
        out = io.StringIO()
        feed = iter(answers or [])

        def read(prompt):
            try:
                return next(feed)
            except StopIteration:
                raise EOFError
        err = io.StringIO()
        with redirect_stderr(err):
            code = init_cmd.main(a, read=read, out=out)
        return code, out.getvalue(), err.getvalue()

    def test_yes_writes_a_config_that_loads_a_password_and_prints_what_next(self):
        a = args(config=self.path("dash", "dashboard.toml"), password_dir=self.path("pw"),
                 site_name="cluster X", slurm=False, port=18190)
        code, out, err = self.run_init(a)
        self.assertEqual(code, 0, err + out)
        with open(self.path("dash", "dashboard.toml"), "rb") as fh:
            tree = tomllib.load(fh)
        self.assertEqual(tree["site"]["name"], "cluster X")
        self.assertEqual(tree["feeds"]["ebpf"]["roots"], [self.path("scratch", "ebpfm")])
        self.assertNotIn("ebpf_node", tree["feeds"])          # none found: the default places
        self.assertIs(tree["slurm"]["enabled"], False)
        self.assertEqual(tree["server"]["port"], 18190)
        hash_file = tree["server"]["auth"]["password_hash_file"]
        self.assertEqual(hash_file, self.path("pw", "password.hash"))
        with open(self.path("pw", "password")) as fh:
            pw = fh.read().strip()
        self.assertTrue(auth.verify_password(pw, auth.read_hash_file(hash_file)))
        self.assertEqual(stat.S_IMODE(os.stat(self.path("pw")).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(self.path("pw", "password")).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(hash_file).st_mode), 0o640)
        self.assertNotIn(pw, out + err)
        # it loads as the service will load it, and the feed it names is read
        cfg = configmod.load(None, self.path("dash", "dashboard.toml"))
        self.assertEqual(cfg.get("site.name"), "cluster X")
        self.assertRegex(out, r"%s\s+found \(1 files\)" % self.path("scratch", "ebpfm"))
        self.assertIn("uv run --frozen rc-dashboard serve", out)
        self.assertIn("ssh -N -L 18190:127.0.0.1:18190 ", out)
        self.assertIn(self.path("pw", "password"), out)

    def test_it_never_replaces_a_config_without_force(self):
        target = self.path("dash", "dashboard.toml")
        a = args(config=target, password_dir=self.path("pw"))
        self.assertEqual(self.run_init(a)[0], 0)
        with open(target) as fh:
            before = fh.read()
        code, _, err = self.run_init(a)
        self.assertEqual(code, 2)
        self.assertIn("pass --force", err)
        with open(target) as fh:
            self.assertEqual(fh.read(), before)
        # a second run keeps the password, unless --force
        with open(self.path("pw", "password")) as fh:
            pw = fh.read()
        code, out, _ = self.run_init(args(config=target, password_dir=self.path("pw"), force=True,
                                          site_name="again"))
        self.assertEqual(code, 0)
        with open(self.path("pw", "password")) as fh:
            self.assertNotEqual(fh.read(), pw)

    def test_a_kept_password_gets_its_hash_file(self):
        os.makedirs(self.path("pw"), mode=0o700)
        with open(self.path("pw", "password"), "w") as fh:
            fh.write("Old-pw:1\n")
        code, out, _ = self.run_init(args(config=self.path("c.toml"), password_dir=self.path("pw")))
        self.assertEqual(code, 0)
        self.assertIn("Kept the password", out)
        self.assertTrue(auth.verify_password("Old-pw:1",
                                             auth.read_hash_file(self.path("pw", "password.hash"))))

    def test_never_beside_the_other_default_file(self):
        with open(self.path("dash", "dashboard.config.json"), "w") as fh:
            fh.write("{}")
        code, _, err = self.run_init(args(config=self.path("dash", "dashboard.toml"),
                                          password_dir=self.path("pw")))
        self.assertEqual(code, 2)
        self.assertIn("refuse to choose", err)
        self.assertFalse(os.path.exists(self.path("pw")))

    def test_it_asks_and_takes_the_answers(self):
        answers = [self.path("asked.toml"),       # where
                   "",                            # the eBPF folder: what was found
                   "/n/node-a, /n/node-b",        # the node tier: two
                   "my site",                     # the name
                   "y",                           # Slurm
                   "18191"]                       # the port
        code, out, err = self.run_init(args(yes=False, password_dir=self.path("pw")), answers)
        self.assertEqual(code, 0, err)
        with open(self.path("asked.toml"), "rb") as fh:
            tree = tomllib.load(fh)
        self.assertEqual(tree["feeds"]["ebpf"]["roots"], [self.path("scratch", "ebpfm")])
        self.assertEqual(tree["feeds"]["ebpf_node"]["roots"], ["/n/node-a", "/n/node-b"])
        self.assertEqual((tree["site"]["name"], tree["slurm"]["enabled"], tree["server"]["port"]),
                         ("my site", True, 18191))

    def test_no_one_to_answer_says_to_give_yes(self):
        code, _, err = self.run_init(args(yes=False, password_dir=self.path("pw")), [])
        self.assertEqual(code, 2)
        self.assertIn("--yes", err)

    def test_slurm_is_found_by_sbatch_or_etc_slurm(self):
        with mock.patch.object(init_cmd.shutil, "which", lambda n: "/usr/bin/sbatch"):
            self.assertTrue(init_cmd.slurm_here())
        with mock.patch.object(init_cmd.shutil, "which", lambda n: None), \
                mock.patch.object(init_cmd.os.path, "isdir", lambda p: p == "/etc/slurm"):
            self.assertTrue(init_cmd.slurm_here())
        with mock.patch.object(init_cmd.shutil, "which", lambda n: None), \
                mock.patch.object(init_cmd.os.path, "isdir", lambda p: False):
            self.assertFalse(init_cmd.slurm_here())

    def test_the_command_line(self):
        a = cli.build_parser().parse_args(["init", "--yes", "--no-slurm", "--ebpf-root", "/x",
                                           "--site-name", "s", "--config", "/c.toml",
                                           "--password-dir", "/p", "--force", "--port", "9"])
        self.assertEqual((a.command, a.yes, a.slurm, a.ebpf_root, a.site_name, a.config,
                          a.password_dir, a.force, a.port),
                         ("init", True, False, ["/x"], "s", "/c.toml", "/p", True, 9))
        self.assertIs(cli.build_parser().parse_args(["init", "--slurm"]).slurm, True)
        self.assertIsNone(cli.build_parser().parse_args(["init"]).slurm)


if __name__ == "__main__":
    unittest.main()
