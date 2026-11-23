"""`rc-dashboard backup` and `restore` (rc_dashboard/backup.py): the state that
cannot be rebuilt, into one tar and back -- the roll-up table copied with
SQLite's backup, the salt, the session key, the config file, the hash file --
and never a file replaced without --force.

    cd dashboard && .venv/bin/python -m unittest tests.test_backup -q
"""
import io
import json
import os
import shutil
import sqlite3
import stat
import sys
import tarfile
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rc_dashboard import __main__ as cli            # noqa: E402
from rc_dashboard import auth                       # noqa: E402
from rc_dashboard import config as configmod        # noqa: E402


class Backup(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rc-backup-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        env = mock.patch.dict(os.environ, {k: v for k, v in os.environ.items()
                                           if not k.startswith(("RC_DASH_", "EBPFM_"))},
                              clear=True)
        env.start()
        self.addCleanup(env.stop)
        here = mock.patch.object(configmod, "HERE", self.tmp)
        here.start()
        self.addCleanup(here.stop)
        self.cache = self.path("cache")
        self.hash_file = self.path("etc", "password.hash")
        self.config = self.path("dashboard.toml")
        with open(self.config, "w") as fh:
            fh.write('[cache]\ndir = "%s"\n[server.auth]\npassword_hash_file = "%s"\n'
                     % (self.cache, self.hash_file))
        auth.write_hash_file(self.hash_file, auth.hash_password("pw", n=16, r=1, p=1))
        os.makedirs(os.path.join(self.cache, "rollup"), mode=0o700)
        os.makedirs(os.path.join(self.cache, "ask"), mode=0o700)
        self.db = os.path.join(self.cache, "rollup", "rollup.sqlite")
        db = sqlite3.connect(self.db)
        db.execute("PRAGMA journal_mode = WAL")
        db.execute("CREATE TABLE day (date TEXT PRIMARY KEY, records INTEGER)")
        db.execute("INSERT INTO day VALUES ('2026-11-20', 42)")
        db.commit()
        self.addCleanup(db.close)           # held open, as a running service holds it
        self.held = db
        for name, text in (("session.key", "ab" * 32), ("anonymize.salt", "cd" * 32),
                           (os.path.join("ask", "done.json"), "[]")):
            with open(os.path.join(self.cache, name), "w") as fh:
                fh.write(text + "\n")
            os.chmod(os.path.join(self.cache, name), 0o600)

    def path(self, *p):
        return os.path.join(self.tmp, *p)

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_what_goes_in_and_what_stays_out(self):
        tar_path = self.path("b.tar")
        code, out, err = self.run_cli("backup", "--config", self.config, "--to", tar_path)
        self.assertEqual(code, 0, err)
        self.assertEqual(stat.S_IMODE(os.stat(tar_path).st_mode), 0o600)
        with tarfile.open(tar_path) as tar:
            names = set(tar.getnames())
            manifest = json.load(tar.extractfile("MANIFEST.json"))
            snap = tar.extractfile("rollup.sqlite").read()
        self.assertEqual(names, {"MANIFEST.json", "rollup.sqlite", "session.key", "anonymize.salt",
                                 "password.hash", "ask-done.json", "config/dashboard.toml"})
        roles = {f["role"]: f for f in manifest["files"]}
        self.assertEqual(roles["hash_file"]["path"], self.hash_file)
        self.assertEqual(roles["session_key"]["mode"], "0600")
        self.assertIn("rebuilt", manifest["not_included"])
        # the table as SQLite's backup made it, the row a writer committed included
        copy = self.path("copy.sqlite")
        with open(copy, "wb") as fh:
            fh.write(snap)
        self.assertEqual(sqlite3.connect(copy).execute("SELECT records FROM day").fetchone(), (42,))
        self.assertIn("Left out, as rebuilt", out)

    def test_an_existing_tar_is_replaced_only_with_force_but_an_empty_place_is_taken(self):
        tar_path = self.path("b.tar")
        self.assertEqual(self.run_cli("backup", "--config", self.config, "--to", tar_path)[0], 0)
        code, _, err = self.run_cli("backup", "--config", self.config, "--to", tar_path)
        self.assertEqual(code, 1)
        self.assertIn("--force", err)
        self.assertEqual(self.run_cli("backup", "--config", self.config, "--to", tar_path,
                                      "--force")[0], 0)
        made = self.path("made-by-mktemp")
        open(made, "w").close()
        self.assertEqual(self.run_cli("backup", "--config", self.config, "--to", made)[0], 0)
        self.assertTrue(tarfile.is_tarfile(made))

    def test_restore_puts_each_file_back_and_refuses_to_replace(self):
        tar_path = self.path("b.tar")
        self.run_cli("backup", "--config", self.config, "--to", tar_path)
        self.held.close()
        # everything there and the same, but the table: a database is replaced only on --force
        code, _, err = self.run_cli("restore", "--config", self.config, "--from", tar_path)
        self.assertEqual(code, 1)
        self.assertIn("rollup.sqlite", err)
        with open(os.path.join(self.cache, "session.key"), "w") as fh:
            fh.write("changed\n")
        code, _, err = self.run_cli("restore", "--config", self.config, "--from", tar_path)
        self.assertEqual(code, 1)
        self.assertIn("session.key", err)
        with open(os.path.join(self.cache, "session.key")) as fh:
            self.assertEqual(fh.read(), "changed\n")          # nothing written
        code, out, err = self.run_cli("restore", "--config", self.config, "--from", tar_path,
                                      "--force")
        self.assertEqual(code, 0, err)
        with open(os.path.join(self.cache, "session.key")) as fh:
            self.assertEqual(fh.read(), "ab" * 32 + "\n")
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.cache, "session.key")).st_mode),
                         0o600)
        self.assertEqual(sqlite3.connect(self.db).execute("SELECT records FROM day").fetchone(),
                         (42,))

    def test_restore_onto_a_new_host_follows_the_config(self):
        tar_path = self.path("b.tar")
        self.run_cli("backup", "--config", self.config, "--to", tar_path)
        self.held.close()
        new = self.path("newhost")
        os.makedirs(new)
        moved = os.path.join(new, "dashboard.toml")
        # the restored config goes where --config says; RC_DASH_CACHE_DIR moves the cache
        with mock.patch.dict(os.environ, {"RC_DASH_CACHE_DIR": os.path.join(new, "cache")}):
            code, out, err = self.run_cli("restore", "--config", moved, "--from", tar_path)
        self.assertEqual(code, 0, err)
        self.assertTrue(os.path.isfile(moved))
        for name in ("session.key", "anonymize.salt", os.path.join("rollup", "rollup.sqlite"),
                     os.path.join("ask", "done.json")):
            self.assertTrue(os.path.isfile(os.path.join(new, "cache", name)), name)

    def test_a_tar_that_is_no_backup_is_refused(self):
        bad = self.path("bad.tar")
        with tarfile.open(bad, "w") as tar:
            info = tarfile.TarInfo("../etc/passwd")
            info.size = 1
            tar.addfile(info, io.BytesIO(b"x"))
        code, _, err = self.run_cli("restore", "--config", self.config, "--from", bad)
        self.assertEqual(code, 1)
        self.assertIn("which no backup holds", err)


if __name__ == "__main__":
    unittest.main()
