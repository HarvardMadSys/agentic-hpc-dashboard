"""check.sh, the hourly check this machine runs: it logs in to the dashboard's host and
runs check-local.sh there, in the deployment's folder. ssh is a stand-in that logs its
arguments and answers as check-local.sh would."""
import os
import shutil
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(os.path.dirname(HERE), "check.sh")
KIT_REL = os.path.relpath(os.path.dirname(HERE), os.path.dirname(os.path.dirname(os.path.dirname(HERE))))

SSH = r'''#!/bin/bash
for a in "$@"; do printf '%s\n' "$a"; done > "$STUB_LOG"
echo "${STUB_OUT:-OK}"
exit "${STUB_RC:-0}"
'''

SITE = """SOURCE_USER=alice
SOURCE_NODES="node01 node02"
SOURCE_DOMAIN=cluster.example.org
DASH_SSH=alice@dash.example.org
DASH_NODE=dash
DASH_ROOT=/data/alice/rc-dashboard
SSH_KEY=/keys/id_ed25519
SLACK_WEBHOOK_FILE=/nonexistent
"""


class TestCheckRemote(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="check-remote-")
        self.bin = os.path.join(self.tmp, "bin")
        os.mkdir(self.bin)
        p = os.path.join(self.bin, "ssh")
        with open(p, "w") as fh:
            fh.write(SSH)
        os.chmod(p, 0o700)
        self.log = os.path.join(self.tmp, "ssh.args")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_check(self, site=SITE, **env):
        path = os.path.join(self.tmp, "site.env")
        with open(path, "w") as fh:
            fh.write(site)
        e = dict(os.environ, PATH=self.bin + ":" + os.environ["PATH"], SITE_ENV=path,
                 STUB_LOG=self.log, DRY_SLACK="1")
        e.update(env)
        p = subprocess.run(["bash", SCRIPT], env=e, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           universal_newlines=True)
        args = []
        if os.path.exists(self.log):
            with open(self.log) as fh:
                args = fh.read().splitlines()
        return p.returncode, p.stdout + p.stderr, args

    def test_runs_check_local_in_the_deployment_folder(self):
        code, out, args = self.run_check()
        self.assertEqual(code, 0, out)
        self.assertIn("alice@dash.example.org", args)
        self.assertEqual(args[args.index("-i") + 1], "/keys/id_ed25519")
        self.assertEqual(args[-1], "RC_DASH_ROOT=/data/alice/rc-dashboard "
                                   "/data/alice/rc-dashboard/current/%s/check-local.sh" % KIT_REL)
        self.assertNotIn("slack", out)

    def test_a_finding_goes_to_slack(self):
        code, out, args = self.run_check(STUB_RC="10", STUB_OUT="RESTARTED stream")
        self.assertEqual(code, 10)
        self.assertIn("slack (dry): *rc dashboard* on `dash`: RESTARTED stream (exit 10)", out)

    def test_no_webhook_logs_the_finding_only(self):
        """Slack is optional: with no SLACK_WEBHOOK_FILE the finding stays in the log."""
        site = SITE.replace("SLACK_WEBHOOK_FILE=/nonexistent\n", "SLACK_WEBHOOK_FILE=\n")
        code, out, args = self.run_check(site=site, STUB_RC="16", STUB_OUT="STREAM node02 down 2.0 h",
                                         DRY_SLACK="0")
        self.assertEqual(code, 16)
        self.assertIn("rc=16 STREAM node02 down 2.0 h", out)
        self.assertNotIn("slack", out)

    def test_a_missing_webhook_file_is_said(self):
        code, out, args = self.run_check(STUB_RC="10", STUB_OUT="RESTARTED stream", DRY_SLACK="0")
        self.assertEqual(code, 10)
        self.assertIn("slack: /nonexistent is missing or empty", out)

    def test_needs_the_host_and_the_folder(self):
        for key in ("DASH_SSH", "DASH_ROOT"):
            site = "".join(l + "\n" for l in SITE.splitlines() if not l.startswith(key + "="))
            code, out, args = self.run_check(site=site)
            self.assertEqual(code, 2, key)
            self.assertIn(key, out)
            self.assertEqual(args, [], "no login without " + key)


if __name__ == "__main__":
    unittest.main()
