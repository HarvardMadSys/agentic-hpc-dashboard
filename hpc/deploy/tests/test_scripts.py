"""password.sh, rollback.sh, backup.sh, uninstall.sh and deploy.sh --plan, run for real
from "this machine", with ssh a stand-in that runs the remote command here, against a
temporary deployment folder, and uv, systemctl, crontab and the venv's Python stand-ins
that log their calls."""
import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
KIT = os.path.dirname(HERE)
REPO = os.path.dirname(os.path.dirname(KIT))

LOG = 'echo "$(basename "$0") $*" >> "$STUB_LOG"\n'
STUBS = {
    # ssh: options, then the target, then the command, run here (stdin and all)
    "ssh": r'''#!/bin/bash
args=("$@")
echo "ssh ${args[*]}" >> "$STUB_LOG"
cmd=${args[-1]}
target=${args[-2]}
case $target in *unreachable*) echo "ssh: connect to host $target: refused" >&2; exit 255 ;; esac
exec bash -c "$cmd"
''',
    # uv run --frozen python ARGS: new-password as the dashboard's contract has it
    "uv": "#!/bin/bash\n" + LOG + r'''
while [ "$1" != python ]; do shift; done; shift
if [ "$1" = -m ] && [ "$3" = new-password ]; then
  shift 3; dir= inst= force=0
  while [ $# -gt 0 ]; do
    case $1 in --dir) dir=$2; shift 2 ;; --install) inst=$2; shift 2 ;; --force) force=1; shift ;; *) shift ;; esac
  done
  if [ -e "$dir/password" ] && [ "$force" = 0 ]; then echo "$dir/password already exists" >&2; exit 1; fi
  mkdir -p "$dir" "$(dirname "$inst")"
  n=$(date +%s%N)
  echo "pw-$n" > "$dir/password"; echo "machine 127.0.0.1 login rc password pw-$n" > "$dir/netrc"
  echo "scrypt\$16384\$8\$1\$c2FsdHNhbHQ=\$aGFzaA==$n" > "$inst"
  exit 0
fi
if [ "$1" = -c ]; then echo "scrypt\$derived" > "${@: -1}"; exit 0; fi
exit 3
''',
    "systemctl": "#!/bin/bash\n" + LOG + "exit 0\n",
    "crontab": "#!/bin/bash\n" + LOG + r'''
case $1 in
  -l) [ -f "$STUB_CRONTAB" ] && cat "$STUB_CRONTAB" ;;
  -) cat > "$STUB_CRONTAB" ;;
esac
''',
}


class Rig(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hpc-scripts-")
        self.home = os.path.join(self.tmp, "home")
        self.root = os.path.join(self.tmp, "dash", "rc-dashboard")
        self.bin = os.path.join(self.tmp, "bin")
        self.log = os.path.join(self.tmp, "calls.log")
        self.crontab = os.path.join(self.tmp, "crontab")
        for d in (self.home, self.bin, os.path.join(self.root, "etc")):
            os.makedirs(d)
        for name, body in STUBS.items():
            p = os.path.join(self.bin, name)
            with open(p, "w") as fh:
                fh.write(body)
            os.chmod(p, 0o755)
        self.site = os.path.join(self.tmp, "site.env")
        self.write_site()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_site(self, **over):
        vals = {"SOURCE_USER": "alice", "SOURCE_NODES": '"node01 node02"', "SOURCE_DOMAIN": "",
                "SENDER_DIR": os.path.join(self.tmp, "sender"), "SHARED_HOME": "yes",
                "DASH_SSH": "alice@dash", "DASH_NODE": "dash", "DASH_ROOT": self.root,
                "DASH_PORT": "8080", "SSH_KEY": "/keys/id", "WORK_DIR": os.path.join(self.tmp, "work")}
        vals.update(over)
        with open(self.site, "w") as fh:
            for k, v in vals.items():
                fh.write("%s=%s\n" % (k, v))

    def run_script(self, name, *args, code=0, **env):
        e = dict(os.environ, PATH=self.bin + ":" + os.environ["PATH"], HOME=self.home,
                 SITE_ENV=self.site, STUB_LOG=self.log, STUB_CRONTAB=self.crontab)
        e.update(env)
        p = subprocess.run(["bash", os.path.join(KIT, name)] + list(args), env=e,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           universal_newlines=True, timeout=60)
        self.assertEqual(p.returncode, code, p.stdout)
        return p.stdout

    def calls(self):
        if not os.path.exists(self.log):
            return ""
        with open(self.log) as fh:
            return fh.read()

    def read(self, path):
        with open(path) as fh:
            return fh.read()


class TestPassword(Rig):
    def test_made_here_and_its_hash_put_there(self):
        out = self.run_script("password.sh")
        mine = os.path.join(self.home, ".config", "rc-dashboard")
        there = os.path.join(self.root, "etc", "password.hash")
        self.assertTrue(os.path.exists(os.path.join(mine, "password")))
        self.assertEqual(self.read(there), self.read(os.path.join(mine, "password.hash")))
        self.assertEqual(stat.S_IMODE(os.stat(there).st_mode), 0o600)
        self.assertIn("uv run --frozen python -m rc_dashboard new-password", self.calls())
        self.assertIn("--netrc-file", out)
        # again: the same password, the same hash
        first = self.read(there)
        self.run_script("password.sh")
        self.assertEqual(self.read(there), first)
        # rotated: a new one, and no restart of anything
        self.run_script("password.sh", "--rotate")
        self.assertNotEqual(self.read(there), first)
        self.assertNotIn("systemctl", self.calls())

    def test_a_password_from_before_hash_files_is_kept(self):
        mine = os.path.join(self.home, ".config", "rc-dashboard")
        os.makedirs(mine)
        with open(os.path.join(mine, "password"), "w") as fh:
            fh.write("old\n")
        self.run_script("password.sh")
        self.assertEqual(self.read(os.path.join(mine, "password")), "old\n")
        self.assertEqual(self.read(os.path.join(self.root, "etc", "password.hash")), "scrypt$derived\n")

    def test_a_hash_in_auth_env_is_retired_once(self):
        auth = os.path.join(self.root, "etc", "auth.env")
        with open(auth, "w") as fh:
            fh.write("RC_DASH_PASSWORD_HASH='scrypt$old'\nRC_DASH_ALLOWED_ORIGINS=https://x\n")
        out = self.run_script("password.sh")
        self.assertEqual(self.read(auth), "# replaced by password.hash: RC_DASH_PASSWORD_HASH='scrypt$old'\n"
                                          "RC_DASH_ALLOWED_ORIGINS=https://x\n")
        self.assertIn("systemctl --user stop rc-dashboard.scope", self.calls())
        self.assertIn("restarts once", out)
        open(self.log, "w").close()
        self.run_script("password.sh", "--rotate")
        self.assertNotIn("systemctl", self.calls(), "a rotation restarts nothing")

    def test_plan(self):
        out = self.run_script("password.sh", "--plan")
        self.assertIn("would run:", out)
        self.assertFalse(os.path.exists(os.path.join(self.root, "etc", "password.hash")))
        self.assertFalse(os.path.exists(os.path.join(self.home, ".config")))


class TestRollback(Rig):
    def release(self, sha):
        d = os.path.join(self.root, "releases", sha)
        os.makedirs(d)
        open(os.path.join(d, ".installed"), "w").close()
        open(os.path.join(d, "requirements.txt"), "w").close()
        with open(os.path.join(self.root, "releases", ".history"), "a") as fh:
            fh.write("%s 2026-11-23 10:00:00\n" % sha)
        cur = os.path.join(self.root, "current")
        if os.path.lexists(cur):
            os.unlink(cur)
        os.symlink("releases/" + sha, cur)

    def setUp(self):
        super().setUp()
        venv = os.path.join(self.root, "venv", "bin")
        os.makedirs(venv)
        with open(os.path.join(venv, "python"), "w") as fh:
            fh.write("#!/bin/bash\n" + LOG + "exit 0\n")
        os.chmod(os.path.join(venv, "python"), 0o755)
        self.release("aaaaaaaaaaaa")
        self.release("bbbbbbbbbbbb")

    def test_back_to_the_release_before(self):
        out = self.run_script("rollback.sh")
        self.assertEqual(os.readlink(os.path.join(self.root, "current")), "releases/aaaaaaaaaaaa")
        calls = self.calls()
        self.assertIn("pip install --quiet --require-hashes -r %s/releases/aaaaaaaaaaaa/requirements.txt"
                      % self.root, calls)
        self.assertIn("systemctl --user restart rc-tmux.service", calls)
        self.assertIn("current: aaaaaaaaaaaa (was bbbbbbbbbbbb)", out)

    def globbed_config(self):
        """The settings as this kit renders them: the copy as one pattern over the mirror."""
        mirror = os.path.join(self.root, "data", "mirror")
        for n in ("node02", "node01"):
            os.makedirs(os.path.join(mirror, n))
        p = os.path.join(self.root, "etc", "dashboard.config.json")
        with open(p, "w") as fh:
            json.dump({"feeds": {"ebpf": {"roots": [mirror + "/*"], "days": 8}}, "ingest": {}}, fh)
        os.chmod(p, 0o600)
        return p, mirror

    def expands_patterns(self, sha):
        d = os.path.join(self.root, "releases", sha, "dashboard", "rc_dashboard")
        os.makedirs(d)
        with open(os.path.join(d, "config.py"), "w") as fh:
            fh.write("def expand_root(path):\n    return [path]\n")

    def test_back_to_a_release_that_reads_no_pattern_names_each_node(self):
        """A release from before the pattern root reads `data/mirror/*` as one folder of
        that name, which is none: it started with no feed. Its settings name each node's
        folder instead, as the pattern matches them now."""
        p, mirror = self.globbed_config()
        self.expands_patterns("bbbbbbbbbbbb")
        out = self.run_script("rollback.sh")
        with open(p) as fh:
            cfg = json.load(fh)
        self.assertEqual(cfg["feeds"]["ebpf"]["roots"],
                         [os.path.join(mirror, "node01"), os.path.join(mirror, "node02")])
        self.assertEqual(cfg["feeds"]["ebpf"]["days"], 8)
        self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o600)
        self.assertIn("each node's folder", out)

    def test_back_to_a_release_that_reads_patterns_keeps_the_pattern(self):
        p, mirror = self.globbed_config()
        for sha in ("aaaaaaaaaaaa", "bbbbbbbbbbbb"):
            self.expands_patterns(sha)
        before = self.read(p)
        self.run_script("rollback.sh")
        self.assertEqual(self.read(p), before)

    def test_plan_and_a_named_release(self):
        out = self.run_script("rollback.sh", "--plan")
        self.assertIn("would go back from bbbbbbbbbbbb to aaaaaaaaaaaa", out)
        self.assertEqual(os.readlink(os.path.join(self.root, "current")), "releases/bbbbbbbbbbbb")
        self.run_script("rollback.sh", "cccccccccccc", code=1)
        self.run_script("rollback.sh", "not-a-sha", code=2)


class TestUninstall(Rig):
    def setUp(self):
        super().setUp()
        for d in ("releases", "venv", "cache/rollup", "data/mirror"):
            os.makedirs(os.path.join(self.root, d))
        os.makedirs(os.path.join(self.tmp, "sender", "x"))
        self.ak = os.path.join(self.home, ".ssh", "authorized_keys")
        os.makedirs(os.path.dirname(self.ak))
        with open(self.ak, "w") as fh:
            fh.write("ssh-ed25519 AAAAmine me@laptop\n"
                     'restrict,command="/usr/bin/python3 /s/current/fleet_stream.py send --root /x" '
                     "ssh-ed25519 AAAAstream rc-fleet-stream\n")
        with open(self.crontab, "w") as fh:
            fh.write("0 1 * * * other-job\n17 * * * * %s/work/bin/check.sh >> log 2>&1\n" % self.tmp)

    def test_without_purge_the_data_stays(self):
        self.run_script("uninstall.sh")
        self.assertFalse(os.path.exists(os.path.join(self.root, "releases")))
        self.assertFalse(os.path.exists(os.path.join(self.root, "venv")))
        self.assertTrue(os.path.exists(os.path.join(self.root, "cache", "rollup")))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "sender")))
        self.assertEqual(self.read(self.ak), "ssh-ed25519 AAAAmine me@laptop\n")
        self.assertEqual(self.read(self.crontab), "0 1 * * * other-job\n")
        self.assertIn("systemctl --user disable --now rc-tmux.service", self.calls())

    def deployed(self):
        """Make the fake DASH_ROOT look as a deploy leaves one: its releases and the `current` link."""
        os.makedirs(os.path.join(self.root, "releases", "abc123"), exist_ok=True)
        os.symlink(os.path.join(self.root, "releases", "abc123"), os.path.join(self.root, "current"))

    def test_purge(self):
        self.deployed()
        self.run_script("uninstall.sh", "--purge")
        self.assertFalse(os.path.exists(self.root))

    def test_purge_refuses_what_is_not_a_deployment(self):
        # a DASH_ROOT slipped onto some other folder (a home, a project) has no
        # releases and no `current` link: --purge refuses it and removes nothing
        out = self.run_script("uninstall.sh", "--purge", code=1)
        self.assertIn("is not a deployment", out)
        self.assertTrue(os.path.exists(os.path.join(self.root, "cache", "rollup")))

    def test_plan(self):
        out = self.run_script("uninstall.sh", "--purge", "--plan")
        self.assertIn("would run on alice@dash:", out)
        self.assertTrue(os.path.exists(os.path.join(self.root, "cache", "rollup")))
        self.assertIn("rc-fleet-stream", self.read(self.ak))


class TestDeployPlan(Rig):
    """deploy.sh --plan, from a clean checkout of the kit: site.env checked, nothing shipped."""

    def checkout(self):
        repo = os.path.join(self.tmp, "repo")
        for part in ("hpc/deploy", "hpc/fleet"):
            shutil.copytree(os.path.join(REPO, part), os.path.join(repo, part),
                            ignore=shutil.ignore_patterns("tests", "__pycache__", "site.env"))
        git = ["git", "-C", repo, "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false"]
        subprocess.run(["git", "init", "-q", repo], check=True)
        subprocess.run(git + ["add", "-A"], check=True)
        subprocess.run(git + ["commit", "-q", "-m", "kit"], check=True)
        shutil.copy(self.site, os.path.join(repo, "hpc", "deploy", "site.env"))
        return repo

    def test_plan_ships_nothing(self):
        repo = self.checkout()
        p = subprocess.run(["bash", os.path.join(repo, "hpc", "deploy", "deploy.sh"), "--plan"],
                           env=dict(os.environ, PATH=self.bin + ":" + os.environ["PATH"],
                                    HOME=self.home, STUB_LOG=self.log),
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
        self.assertEqual(p.returncode, 0, p.stdout)
        self.assertIn("site.env renders: 2 node(s)", p.stdout)
        self.assertIn("nothing was changed", p.stdout)
        self.assertNotIn("ssh", self.calls())
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "work")))

    def test_plan_reads_the_settings_file_it_is_given(self):
        """rc-deploy --site hands deploy.sh its file as SITE_ENV: that one is read, rendered
        and checked, not the kit's own site.env beside it."""
        repo = self.checkout()
        with open(os.path.join(repo, "hpc", "deploy", "site.env")) as fh:
            own = fh.read().replace('SOURCE_NODES="node01 node02"', "SOURCE_NODES=node09")
        self.assertIn("SOURCE_NODES=node09", own)
        with open(os.path.join(repo, "hpc", "deploy", "site.env"), "w") as fh:
            fh.write(own)
        p = subprocess.run(["bash", os.path.join(repo, "hpc", "deploy", "deploy.sh"), "--plan"],
                           env=dict(os.environ, PATH=self.bin + ":" + os.environ["PATH"],
                                    HOME=self.home, STUB_LOG=self.log, SITE_ENV=self.site),
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
        self.assertEqual(p.returncode, 0, p.stdout)
        self.assertIn("site.env renders: 2 node(s)", p.stdout)
        self.assertIn("on node01 node02", p.stdout)
        os.unlink(os.path.join(repo, "hpc", "deploy", "site.env"))
        p = subprocess.run(["bash", os.path.join(repo, "hpc", "deploy", "deploy.sh"), "--plan"],
                           env=dict(os.environ, PATH=self.bin + ":" + os.environ["PATH"],
                                    HOME=self.home, STUB_LOG=self.log, SITE_ENV=self.site),
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
        self.assertEqual(p.returncode, 0, "the kit needs no site.env of its own: " + p.stdout)

    def test_plan_reads_a_settings_file_named_relative_to_where_it_ran(self):
        """deploy.sh changes to the checkout after reading SITE_ENV: a relative path, read
        once, then missed by render.py and every script after it."""
        repo = self.checkout()
        os.unlink(os.path.join(repo, "hpc", "deploy", "site.env"))
        p = subprocess.run(["bash", os.path.join(repo, "hpc", "deploy", "deploy.sh"), "--plan"],
                           cwd=os.path.dirname(self.site),
                           env=dict(os.environ, PATH=self.bin + ":" + os.environ["PATH"],
                                    HOME=self.home, STUB_LOG=self.log,
                                    SITE_ENV=os.path.basename(self.site)),
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
        self.assertEqual(p.returncode, 0, p.stdout)
        self.assertIn("site.env renders: 2 node(s)", p.stdout)

    def test_plan_refuses_the_examples_values(self):
        repo = self.checkout()
        shutil.copy(os.path.join(KIT, "site.env.example"), os.path.join(repo, "hpc", "deploy", "site.env"))
        p = subprocess.run(["bash", os.path.join(repo, "hpc", "deploy", "deploy.sh"), "--plan"],
                           env=dict(os.environ, PATH=self.bin + ":" + os.environ["PATH"],
                                    HOME=self.home, STUB_LOG=self.log),
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("still site.env.example's value", p.stdout)


class TestHelp(unittest.TestCase):
    def test_every_script_says_what_it_does(self):
        for name in ("deploy.sh", "password.sh", "rollback.sh", "backup.sh", "uninstall.sh"):
            p = subprocess.run(["bash", os.path.join(KIT, name), "--help"], stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, universal_newlines=True)
            self.assertEqual(p.returncode, 0, name)
            self.assertTrue(p.stdout.startswith(name), name)


if __name__ == "__main__":
    unittest.main()
