"""ship-sender.sh, run for real with ssh replaced by a stand-in that runs each remote
command here, in a folder per node (or one folder for a home the nodes share)."""
import os
import shutil
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(os.path.dirname(HERE), "ship-sender.sh")
SHA = "0123456789ab"

# The stand-in: logs the node, fails for the nodes in STUB_DOWN, and runs the remote
# command with HOME set to that node's home, so SENDER_DIR lands in it.
SSH = r'''#!/bin/bash
dest=
while [ $# -gt 1 ]; do
  case $1 in -i|-o) shift 2 ;; -*) shift ;; *) dest=$1; shift; break ;; esac
done
node=${dest#*@}
echo "$node" >> "$STUB_LOG"
for d in $STUB_DOWN; do [ "$node" = "$d" ] && exit 255; done
if [ "$STUB_SHARED" = yes ]; then home=$STUB_HOMES/shared; else home=$STUB_HOMES/$node; fi
mkdir -p "$home"
cd "$home" && HOME=$home exec bash -c "${1//\$HOME/$home}"
'''


class TestShipSender(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ship-sender-")
        self.bin = os.path.join(self.tmp, "bin")
        self.homes = os.path.join(self.tmp, "homes")
        os.makedirs(self.bin)
        with open(os.path.join(self.bin, "ssh"), "w") as fh:
            fh.write(SSH)
        os.chmod(os.path.join(self.bin, "ssh"), 0o700)
        self.log = os.path.join(self.tmp, "nodes.log")
        self.file = os.path.join(self.tmp, "rsync_guard.py")
        with open(self.file, "w") as fh:
            fh.write("# the guard\n")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def ship(self, shared="yes", down="", sender_dir="SENDER", code=0):
        site = os.path.join(self.tmp, "site.env")
        with open(site, "w") as fh:
            fh.write('SOURCE_USER=alice\nSOURCE_NODES="node01 node02 node03"\n'
                     "SOURCE_DOMAIN=cluster.example.org\nSSH_KEY=/keys/id\n"
                     "SHARED_HOME=%s\nSENDER_DIR=%s\n"
                     % (shared, "" if sender_dir is None else "\\$HOME/rc-fleet-sender"))
        env = dict(os.environ, PATH=self.bin + ":" + os.environ["PATH"], SITE_ENV=site,
                   STUB_LOG=self.log, STUB_DOWN=down, STUB_SHARED=shared, STUB_HOMES=self.homes)
        p = subprocess.run(["bash", SCRIPT, SHA, self.file], env=env, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, universal_newlines=True)
        self.assertEqual(p.returncode, code, p.stdout + p.stderr)
        return p

    def nodes(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log) as fh:
            return fh.read().split()

    def installed(self, home):
        cur = os.path.join(self.homes, home, "rc-fleet-sender", "current")
        return os.path.islink(cur) and os.readlink(cur) == SHA

    def test_a_shared_home_takes_one_node(self):
        self.ship()
        self.assertEqual(self.nodes(), ["node01.cluster.example.org"] * 2)
        self.assertTrue(self.installed("shared"))

    def test_a_shared_home_skips_a_node_that_is_down(self):
        self.ship(down="node01.cluster.example.org")
        self.assertEqual(self.nodes(), ["node01.cluster.example.org"] + ["node02.cluster.example.org"] * 2)
        self.assertTrue(self.installed("shared"))

    def test_each_node_has_its_own_home(self):
        p = self.ship(shared="no")
        for n in ("node01", "node02", "node03"):
            self.assertTrue(self.installed(n + ".cluster.example.org"), n)
            self.assertIn("%s: guard %s" % (n, SHA), p.stdout)

    def test_a_node_down_fails_when_each_needs_it(self):
        p = self.ship(shared="no", down="node02.cluster.example.org", code=1)
        self.assertIn("node02: FAILED", p.stderr)
        self.assertTrue(self.installed("node01.cluster.example.org"))
        self.assertTrue(self.installed("node03.cluster.example.org"))

    def test_no_node_answers(self):
        down = " ".join("node0%d.cluster.example.org" % i for i in (1, 2, 3))
        p = self.ship(down=down, code=1)
        self.assertIn("not on", p.stderr)

    def test_no_sender_dir_leaves_the_nodes_alone(self):
        self.ship(sender_dir=None, code=2)
        self.assertEqual(self.nodes(), [])


if __name__ == "__main__":
    unittest.main()
