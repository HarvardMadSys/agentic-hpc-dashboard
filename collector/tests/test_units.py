"""ebpfm.sh's systemd units, run for real: install-unit, uninstall-unit, and how start,
stop and status defer to the node tier's unit. `id` says root and `systemctl` is a
stand-in that logs its calls, so no root and no systemd are needed."""
import os
import shutil
import subprocess
import sys
import tarfile
import io
import base64
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = str(_ROOT / "ebpfm.sh")

ID = """#!/bin/sh
if [ "$1" = "-u" ]; then echo 0; else exec /usr/bin/id "$@"; fi
"""
# is-active answers yes for the units named in STUB_ACTIVE; show answers a pid.
SYSTEMCTL = """#!/bin/sh
echo "systemctl $*" >> "$STUB_LOG"
case "$1" in
  is-active) for u in $STUB_ACTIVE; do [ "$u" = "$3" ] && exit 0; done; exit 3 ;;
  show) echo 4242 ;;
esac
exit 0
"""


class Rig(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ebpfm-units-")
        self.bin = os.path.join(self.tmp, "bin")
        self.units = os.path.join(self.tmp, "units")
        self.log = os.path.join(self.tmp, "calls.log")
        for d in (self.bin, self.units):
            os.makedirs(d)
        for name, body in (("id", ID), ("systemctl", SYSTEMCTL)):
            p = os.path.join(self.bin, name)
            with open(p, "w") as fh:
                fh.write(body)
            os.chmod(p, 0o755)
        self.out = os.path.join(self.tmp, "out", "ebpf")
        self.node_out = os.path.join(self.tmp, "out", "node")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_sh(self, *args, script=SCRIPT, active="", **env):
        e = dict(os.environ, PATH=self.bin + ":" + os.environ["PATH"], STUB_LOG=self.log,
                 STUB_ACTIVE=active, EBPFM_UNIT_DIR=self.units, EBPFM_OUTPUT_DIR=self.out,
                 EBPFM_NODE_OUTPUT_DIR=self.node_out,
                 EBPFM_PID_DIR=os.path.join(self.tmp, "run"), HOME=self.tmp)
        e.update(env)
        return subprocess.run(["bash", script] + list(args), env=e, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, universal_newlines=True, timeout=60)

    def calls(self):
        try:
            with open(self.log) as fh:
                return fh.read()
        except OSError:
            return ""

    def unit(self, name):
        with open(os.path.join(self.units, name)) as fh:
            return fh.read()


class TestInstall(Rig):
    def test_both_tiers_get_a_unit(self):
        p = self.run_sh("install-unit")
        self.assertEqual(p.returncode, 0, p.stdout)
        ebpf, node = self.unit("ebpfm.service"), self.unit("ebpfm-node.service")
        for text in (ebpf, node):
            self.assertNotIn("@EBPFM", text)
        self.assertIn("ExecStart=/usr/bin/env python3 %s/ebpf_trace.py %%H" % _ROOT, ebpf)
        self.assertIn("Environment=EBPFM_OUTPUT_DIR=%s" % self.out, ebpf)
        self.assertIn("ExecStart=/bin/bash %s/ebpfm.sh _node_daemon" % _ROOT, node)
        self.assertIn("Environment=EBPFM_NODE_OUTPUT_DIR=%s" % self.node_out, node)
        self.assertIn("systemctl daemon-reload", self.calls())
        # installed, not enabled: the admin enables each tier
        self.assertNotIn("enable", self.calls())
        self.assertIn("systemctl enable --now ebpfm-node", p.stdout)

    def test_a_copy_without_the_node_template_installs_the_one(self):
        """A bundle or package from before the node tier had a unit."""
        old = os.path.join(self.tmp, "old", "collector")
        shutil.copytree(str(_ROOT), old, ignore=shutil.ignore_patterns("tests", "__pycache__"))
        os.unlink(os.path.join(old, "ebpfm-node.service"))
        p = self.run_sh("install-unit", script=os.path.join(old, "ebpfm.sh"))
        self.assertEqual(p.returncode, 0, p.stdout)
        self.assertEqual(sorted(os.listdir(self.units)), ["ebpfm.service"])
        self.assertNotIn("ebpfm-node", p.stdout)

    def test_a_node_loop_start_began_must_stop_first(self):
        """Its loop would run beside the unit's once the unit is enabled, writing every
        snapshot twice, and `stop` would stop only the unit: install-unit refuses, as it
        does for the eBPF tier's, and installs neither unit."""
        loop = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)",
                                 "_node_daemon"])
        self.addCleanup(loop.wait)
        self.addCleanup(loop.kill)
        host = subprocess.run(["hostname", "-s"], stdout=subprocess.PIPE,
                              universal_newlines=True).stdout.strip()
        run = os.path.join(self.tmp, "run")
        os.makedirs(run)
        with open(os.path.join(run, "ebpfm-node_%s.pid" % host), "w") as fh:
            fh.write(str(loop.pid))
        p = self.run_sh("install-unit")
        self.assertNotEqual(p.returncode, 0, p.stdout)
        self.assertIn("node tier", p.stdout)
        self.assertIn("pid %d" % loop.pid, p.stdout)
        self.assertEqual(os.listdir(self.units), [])

    def test_uninstall_removes_both(self):
        self.run_sh("install-unit")
        p = self.run_sh("uninstall-unit")
        self.assertEqual(p.returncode, 0, p.stdout)
        self.assertEqual(os.listdir(self.units), [])
        self.assertIn("systemctl disable --now ebpfm.service", self.calls())
        self.assertIn("systemctl disable --now ebpfm-node.service", self.calls())

    def test_the_node_template(self):
        text = (_ROOT / "ebpfm-node.service").read_text()
        self.assertIn("Restart=on-failure", text)
        self.assertNotIn("Restart=always", text)
        self.assertIn("EnvironmentFile=-/etc/default/ebpfm", text)
        self.assertIn("_node_daemon) shift; _node_loop", Path(SCRIPT).read_text())


class TestDefer(Rig):
    """While the node tier's unit is active, nothing starts a second loop beside it."""

    def test_start_defers_to_the_unit(self):
        p = self.run_sh("start", active="ebpfm-node.service", EBPFM_TIER="node")
        self.assertIn("already running", p.stdout)
        self.assertIn("unit ebpfm-node.service", p.stdout)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "run")),
                         "no pid file: no loop was started")

    def test_status_names_the_unit(self):
        p = self.run_sh("status", active="ebpfm-node.service", EBPFM_TIER="node")
        self.assertIn("node tier: RUNNING", p.stdout)
        self.assertIn("unit ebpfm-node.service, pid 4242", p.stdout)

    def test_stop_stops_the_unit(self):
        p = self.run_sh("stop", active="ebpfm-node.service", EBPFM_TIER="node")
        self.assertIn("systemctl stop ebpfm-node.service", self.calls())
        self.assertIn("still ENABLED", p.stdout)

    def test_without_the_unit_status_is_as_before(self):
        p = self.run_sh("status", EBPFM_TIER="node")
        self.assertIn("node tier: NOT RUNNING", p.stdout)


class TestBundle(Rig):
    def test_the_bundle_carries_the_node_unit(self):
        out = os.path.join(self.tmp, "b.sh")
        p = self.run_sh("bundle", out)
        self.assertEqual(p.returncode, 0, p.stdout)
        with open(out, "rb") as fh:
            payload = fh.read().split(b"__EBPFM_PAYLOAD__\n", 1)[1]
        with tarfile.open(fileobj=io.BytesIO(base64.b64decode(payload))) as tar:
            names = {os.path.basename(n) for n in tar.getnames()}
        self.assertIn("ebpfm-node.service", names)
        self.assertIn("ebpfm.service", names)


if __name__ == "__main__":
    unittest.main()
