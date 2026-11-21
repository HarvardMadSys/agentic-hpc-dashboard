"""The installers, run for real against a copy of the repository and folders here in place
of /etc, /var/lib, /opt and the units' folder. `id` says root; systemctl, the account tools,
chown, uv, npm and ssh-keygen are stand-ins that log their calls."""
import os
import re
import shutil
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIDummyKeyForTheTestsOnly0123456789abcdef rc-pull@dash"

STUB = r'''#!/bin/bash
echo "$(basename "$0") $*" >> "$STUB_LOG"
'''
STUBS = {
    # root, unless STUB_UID says otherwise; anything else as the real id would answer
    # for accounts the stand-in useradd made
    "id": STUB + r'''
case "$1" in
  -u) echo "${STUB_UID:-0}"; exit 0 ;;
  -gn) echo "$2"; exit 0 ;;
esac
grep -qx "user $1" "$STUB_DB" 2>/dev/null
''',
    "getent": STUB + r'''
case "$1" in
  group) grep -qx "group $2" "$STUB_DB" 2>/dev/null ;;
  passwd) grep -qx "user $2" "$STUB_DB" 2>/dev/null && echo "$2:x:990:990::$STUB_HOMES/$2:/bin/sh" ;;
  *) exit 2 ;;
esac
''',
    "groupadd": STUB + 'echo "group ${@: -1}" >> "$STUB_DB"\n',
    "useradd": STUB + 'echo "user ${@: -1}" >> "$STUB_DB"; mkdir -p "$STUB_HOMES/${@: -1}"\n',
    "usermod": STUB,
    "chown": STUB,
    "chgrp": STUB,
    "runuser": STUB + r'''
while [ "$1" != "--" ]; do shift; done; shift
exec "$@"
''',
    # install, without the owner and group only root may give
    "install": STUB + r'''
args=()
while [ $# -gt 0 ]; do
  case $1 in -o|-g) shift 2 ;; *) args+=("$1"); shift ;; esac
done
exec /usr/bin/install "${args[@]}"
''',
    "systemctl": STUB + r'''
case "$1" in
  is-active) for u in $STUB_ACTIVE; do [ "$u" = "${@: -1}" ] && exit 0; done; exit 3 ;;
  show) echo 4242 ;;
esac
exit 0
''',
    "uv": STUB + r'''
mkdir -p "$UV_PROJECT_ENVIRONMENT/bin"
ln -sf "$STUB_PYTHON" "$UV_PROJECT_ENVIRONMENT/bin/python"
''',
    "npm": STUB + r'''
[ "$1" = run ] && mkdir -p dist && echo "<html>" > dist/index.html
exit 0
''',
    "ssh-keygen": STUB + r'''
while [ $# -gt 1 ]; do shift; done
echo "private" > "$1"; echo "''' + KEY + r'''" > "$1.pub"
''',
    "rrsync": "#!/bin/sh\nexit 0\n",
}


class Rig(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="deploy-install-")
        t = self.tmp
        self.repo = os.path.join(t, "checkout")
        for part in ("collector", "deploy"):
            shutil.copytree(os.path.join(REPO, part), os.path.join(self.repo, part),
                            ignore=shutil.ignore_patterns("tests", "__pycache__", "node_modules"))
        os.makedirs(os.path.join(self.repo, "packaging", "rpm"))
        shutil.copy(os.path.join(REPO, "packaging", "rpm", "ebpfm.default"),
                    os.path.join(self.repo, "packaging", "rpm"))
        for f, body in (("dashboard/rc_dashboard/__init__.py", ""),
                        ("dashboard/web/package.json", "{}")):
            p = os.path.join(self.repo, f)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w") as fh:
                fh.write(body)
        self.bin = os.path.join(t, "bin")
        os.makedirs(self.bin)
        for name, body in STUBS.items():
            p = os.path.join(self.bin, name)
            with open(p, "w") as fh:
                fh.write(body)
            os.chmod(p, 0o755)
        self.etc = os.path.join(t, "etc", "rc-dashboard")
        self.units = os.path.join(t, "etc", "systemd", "system")
        self.state = os.path.join(t, "var", "lib", "rc-dashboard")
        self.opt = os.path.join(t, "opt", "rc-dashboard")
        self.default = os.path.join(t, "etc", "default", "ebpfm")
        self.ebpf = os.path.join(t, "var", "log", "ebpfm")
        self.node = os.path.join(t, "var", "log", "ebpfm-login")
        self.log = os.path.join(t, "calls.log")
        for d in (self.units, os.path.dirname(self.default), os.path.join(t, "homes")):
            os.makedirs(d)
        self.env = dict(
            os.environ, PATH=self.bin + ":" + os.environ["PATH"], STUB_LOG=self.log,
            STUB_DB=os.path.join(t, "accounts"), STUB_HOMES=os.path.join(t, "homes"),
            STUB_PYTHON=shutil.which("python3"), ETC=self.etc, STATE=self.state, OPT=self.opt,
            UNIT_DIR=self.units, EBPFM_UNIT_DIR=self.units, EBPFM_DEFAULT_FILE=self.default,
            EBPFM_PACKAGED_UNIT=os.path.join(t, "no-package"), EBPF_DIR=self.ebpf,
            NODE_DIR=self.node, EBPFM_PID_DIR=os.path.join(t, "run"), HOME=t)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_script(self, rel, *args, code=0, **env):
        e = dict(self.env)
        e.update(env)
        p = subprocess.run(["bash", os.path.join(self.repo, rel)] + list(args), env=e,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           universal_newlines=True, timeout=120)
        self.assertEqual(p.returncode, code, p.stdout)
        return p.stdout

    def calls(self):
        try:
            with open(self.log) as fh:
                return fh.read()
        except OSError:
            return ""

    def read(self, path):
        with open(path) as fh:
            return fh.read()

    def keys(self, path):
        out = {}
        for line in self.read(path).splitlines():
            m = re.match(r"^([A-Z_]+)=(.*)$", line)
            if m:
                out[m.group(1)] = m.group(2)
        return out

    def unit(self, name):
        out = {}
        for line in self.read(os.path.join(self.units, name)).splitlines():
            if "=" in line and not line.startswith(("#", "[")):
                k, v = line.split("=", 1)
                out.setdefault(k, []).append(v)
        return out


class TestSingle(Rig):
    def install(self, *args, **env):
        return self.run_script("deploy/single/install.sh", "--no-bootstrap", "--no-check",
                               *args, **env)

    def test_one_host_runs_the_collector_and_the_dashboard(self):
        out = self.install()
        calls = self.calls()
        # the collector: its group, its settings, both units, started
        self.assertIn("groupadd --system ebpfm", calls)
        defaults = self.keys(self.default)
        self.assertEqual(defaults["EBPFM_GROUP"], "ebpfm")
        self.assertEqual(defaults["EBPFM_MIN_UID"], "0")
        ebpfm = self.unit("ebpfm.service")
        self.assertEqual(ebpfm["Environment"], ["EBPFM_OUTPUT_DIR=" + self.ebpf])
        node = self.unit("ebpfm-node.service")
        self.assertEqual(node["Environment"], ["EBPFM_NODE_OUTPUT_DIR=" + self.node])
        self.assertIn("systemctl enable --now ebpfm.service ebpfm-node.service", calls)
        self.assertIn("chgrp -R ebpfm " + self.ebpf, calls)
        # the dashboard: its account, the venv, the bundle, its unit
        self.assertIn("useradd --system", calls)
        self.assertIn("uv sync --frozen --no-dev --no-install-project", calls)
        self.assertIn("npm run build", calls)
        dash = self.unit("rc-dashboard.service")
        self.assertEqual(dash["User"], ["rc-dashboard"])
        self.assertEqual(dash["SupplementaryGroups"], ["ebpfm"])
        self.assertEqual(dash["WorkingDirectory"], [os.path.join(self.repo, "dashboard")])
        self.assertEqual(dash["ExecStart"], [self.opt + "/venv/bin/python -m rc_dashboard serve"])
        self.assertEqual(dash["EnvironmentFile"], [self.etc + "/dashboard.env",
                                                   self.etc + "/paths.env"])
        self.assertEqual(dash["MemoryMax"], ["8G"])
        for name in ("rc-dashboard.service", "ebpfm.service", "ebpfm-node.service"):
            self.assertNotIn("@", self.read(os.path.join(self.units, name)).replace("@USER@", "!"),
                             name)
        paths = self.keys(os.path.join(self.etc, "paths.env"))
        self.assertEqual(paths, {"RC_DASH_EBPF_ROOTS": self.ebpf, "RC_DASH_NODE_ROOTS": self.node,
                                 "RC_DASH_STATIC_DIR": self.repo + "/dashboard/web/dist",
                                 "RC_DASH_CACHE_DIR": self.state + "/cache"})
        settings = self.keys(os.path.join(self.etc, "dashboard.env"))
        self.assertEqual(settings["RC_DASH_DROP_USERS"], "rc-dashboard,rc-pull")
        self.assertEqual(settings["RC_DASH_SLURM"], "0")
        self.assertEqual(settings["RC_DASH_NODE_NOUN"], "servers")
        self.assertNotIn("@", self.read(os.path.join(self.etc, "dashboard.env")))
        # no password yet: enabled, not started, and told how
        self.assertIn("systemctl enable rc-dashboard.service", calls)
        self.assertNotIn("systemctl start rc-dashboard", calls)
        self.assertIn("new-password", out)

    def test_a_password_starts_it_and_a_run_again_restarts_it(self):
        self.install()
        p = os.path.join(self.etc, "dashboard.env")
        text = self.read(p).replace("RC_DASH_PASSWORD_HASH=\n", "RC_DASH_PASSWORD_HASH='scrypt$x$y'\n")
        with open(p, "w") as fh:
            fh.write(text)
        self.install("--no-build")
        self.assertIn("systemctl start rc-dashboard.service", self.calls())
        self.assertIn("RC_DASH_PASSWORD_HASH='scrypt$x$y'", self.read(p), "your settings are kept")
        self.install("--no-build", STUB_ACTIVE="rc-dashboard.service")
        self.assertIn("systemctl restart rc-dashboard.service", self.calls())

    def test_an_open_page_starts_without_a_password(self):
        self.install()
        p = os.path.join(self.etc, "dashboard.env")
        with open(p, "a") as fh:
            fh.write("RC_DASH_OPEN=1\n")
        self.install("--no-build")
        self.assertIn("systemctl start rc-dashboard.service", self.calls())

    def test_a_group_the_collector_already_writes_for_is_kept(self):
        with open(self.default, "w") as fh:
            fh.write("EBPFM_MIN_UID=0\nEBPFM_GROUP=research\n")
        with open(self.env["STUB_DB"], "w") as fh:
            fh.write("group research\n")
        self.install()
        self.assertEqual(self.unit("rc-dashboard.service")["SupplementaryGroups"], ["research"])
        self.assertNotIn("groupadd", self.calls())

    def test_an_empty_group_assignment_does_not_outlast_the_group_set(self):
        """`EBPFM_GROUP=` (empty) leaves the collector's group as it is, and systemd keeps
        the last assignment: setting the group must leave no empty one after it."""
        with open(self.default, "w") as fh:
            fh.write("EBPFM_MIN_UID=0\n#EBPFM_GROUP=\nEBPFM_GROUP=\n")
        self.install()
        active = [l for l in self.read(self.default).splitlines() if l.startswith("EBPFM_GROUP=")]
        self.assertEqual(active, ["EBPFM_GROUP=ebpfm"])
        self.assertEqual(self.unit("rc-dashboard.service")["SupplementaryGroups"], ["ebpfm"])

    def test_a_quoted_group_is_the_group(self):
        """systemd takes the quotes off EBPFM_GROUP="research": so does the installer."""
        with open(self.default, "w") as fh:
            fh.write('EBPFM_MIN_UID=0\nEBPFM_GROUP="research"\n')
        with open(self.env["STUB_DB"], "w") as fh:
            fh.write("group research\n")
        self.install()
        self.assertEqual(self.unit("rc-dashboard.service")["SupplementaryGroups"], ["research"])
        self.assertNotIn("groupadd", self.calls())
        self.assertIn("chgrp -R research " + self.ebpf, self.calls())

    def test_the_folders_the_collector_settings_name_are_read(self):
        """/etc/default/ebpfm may name the output folders (systemd lets an EnvironmentFile=
        override the units' Environment=), and the collector then writes there: the group
        is given those folders, and the dashboard reads them."""
        data = os.path.join(self.tmp, "data", "ebpfm")
        node = os.path.join(self.tmp, "data", "ebpfm-login")
        with open(self.default, "w") as fh:
            fh.write('EBPFM_MIN_UID=0\nEBPFM_OUTPUT_DIR=%s\nEBPFM_NODE_OUTPUT_DIR="%s"\n' % (data, node))
        self.install(EBPF_DIR="", NODE_DIR="")
        paths = self.keys(os.path.join(self.etc, "paths.env"))
        self.assertEqual(paths["RC_DASH_EBPF_ROOTS"], data)
        self.assertEqual(paths["RC_DASH_NODE_ROOTS"], node)
        self.assertIn("chgrp -R ebpfm " + data, self.calls())
        self.assertIn("chgrp -R ebpfm " + node, self.calls())
        self.assertEqual(self.unit("ebpfm.service")["Environment"], ["EBPFM_OUTPUT_DIR=" + data])
        self.assertEqual(self.unit("ebpfm-node.service")["Environment"],
                         ["EBPFM_NODE_OUTPUT_DIR=" + node])

    def test_a_folder_named_two_ways_is_refused(self):
        with open(self.default, "w") as fh:
            fh.write("EBPFM_MIN_UID=0\nEBPFM_OUTPUT_DIR=%s/elsewhere\n" % self.tmp)
        out = self.install(code=1)
        self.assertIn("settle on one", out)
        self.assertFalse(os.path.exists(os.path.join(self.units, "ebpfm.service")))

    def test_refusals(self):
        out = self.install(code=1, STUB_UID="1000")
        self.assertIn("run as root", out)
        open(self.env["EBPFM_PACKAGED_UNIT"], "w").close()
        out = self.install(code=1)
        self.assertIn("package is installed", out)
        os.unlink(self.env["EBPFM_PACKAGED_UNIT"])
        out = self.run_script("deploy/common/install-dashboard.sh", "single", code=1)
        self.assertIn("run install-collector.sh first", out)
        out = self.run_script("deploy/common/install-dashboard.sh", "both", code=2)
        self.assertIn("usage", out)


class TestFleet(Rig):
    def install(self, *args, **env):
        return self.run_script("deploy/fleet/install-dashboard.sh", *args, **env)

    def test_the_dashboard_host_waits_for_its_nodes(self):
        out = self.install()
        fleet = self.keys(os.path.join(self.etc, "fleet.env"))
        self.assertEqual(fleet["FLEET_USER"], "rc-pull")
        self.assertEqual(fleet["FLEET_SSH_KEY"], self.etc + "/pull_key")
        self.assertEqual(fleet["FLEET_MIRROR"], self.state + "/mirror")
        self.assertTrue(os.path.exists(os.path.join(self.etc, "pull_key")))
        self.assertIn(KEY, out, "the public key to give the nodes")
        pull = self.unit("rc-pull.service")
        self.assertEqual(pull["ExecStart"], ["%s/venv/bin/python %s/deploy/fleet/pull.py --config "
                                             "%s/fleet.env loop" % (self.opt, self.repo, self.etc)])
        self.assertEqual(pull["RestartPreventExitStatus"], ["2 3"])
        self.assertEqual(self.unit("rc-dashboard.service")["SupplementaryGroups"], [""])
        self.assertNotIn("systemctl start rc-pull", self.calls())
        self.assertNotIn("groupadd", self.calls())

    def test_named_nodes_start_the_pull_and_are_where_the_dashboard_reads(self):
        self.install()
        p = os.path.join(self.etc, "fleet.env")
        with open(p) as fh:
            text = fh.read().replace('FLEET_HOSTS="server01 server02"', 'FLEET_HOSTS="gpu1 gpu2"')
        with open(p, "w") as fh:
            fh.write(text)
        self.install("--no-build")
        self.assertIn("systemctl start rc-pull.service", self.calls())
        m = self.state + "/mirror"
        paths = self.keys(os.path.join(self.etc, "paths.env"))
        self.assertEqual(paths["RC_DASH_EBPF_ROOTS"], "%s/ebpf/gpu1:%s/ebpf/gpu2" % (m, m))
        self.assertEqual(paths["RC_DASH_NODE_ROOTS"], "%s/node/gpu1:%s/node/gpu2" % (m, m))
        self.install("--no-build", STUB_ACTIVE="rc-pull.service")
        self.assertIn("systemctl restart rc-pull.service", self.calls())

    def test_a_wrong_fleet_env_stops_the_install(self):
        self.install()
        with open(os.path.join(self.etc, "fleet.env"), "a") as fh:
            fh.write("FLEET_HOST=typo\n")
        out = self.install("--no-build", code=1)
        self.assertIn("unknown setting", out)


class TestCollectorSettings(unittest.TestCase):
    """_ebpfm_default reads /etc/default/ebpfm as systemd's EnvironmentFile= does."""

    def value(self, text, key="EBPFM_GROUP"):
        with tempfile.NamedTemporaryFile("w", delete=False) as fh:
            fh.write(text)
        self.addCleanup(os.unlink, fh.name)
        helper = os.path.join(REPO, "deploy", "common", "collector-paths.sh")
        p = subprocess.run(["bash", "-c", 'die() { exit 9; }; DEFAULTS=$1; . "$2"; _ebpfm_default "$3"',
                            "-", fh.name, helper, key],
                           stdout=subprocess.PIPE, universal_newlines=True, check=True)
        return p.stdout

    def test_values(self):
        for text, want in (("EBPFM_GROUP=research\n", "research"),
                           ('EBPFM_GROUP="research"\n', "research"),
                           ("EBPFM_GROUP='research'\n", "research"),
                           ("EBPFM_GROUP= research \n", "research"),
                           ("EBPFM_GROUP=\n", ""),
                           ("#EBPFM_GROUP=research\n", ""),
                           ("EBPFM_GROUP=research\nEBPFM_GROUP=\n", ""),
                           ("EBPFM_GROUP=\nEBPFM_GROUP=lab\n", "lab"),
                           ("", "")):
            self.assertEqual(self.value(text), want, repr(text))


@unittest.skipUnless(shutil.which("rsync"), "rsync is not installed")
class TestNode(Rig):
    def install(self, *args, **env):
        return self.run_script("deploy/fleet/install-node.sh", "--no-bootstrap", "--no-check",
                               *args, **env)

    def authorized(self):
        return self.read(os.path.join(self.tmp, "homes", "rc-pull", ".ssh", "authorized_keys"))

    def test_a_node_runs_the_collector_and_lets_the_pull_read_it(self):
        rr = os.path.join(self.bin, "rrsync")
        out = self.install("--key", KEY, "--from", "10.0.0.5", RRSYNC=rr)
        calls = self.calls()
        self.assertIn("systemctl enable --now ebpfm.service ebpfm-node.service", calls)
        self.assertIn("usermod -a -G ebpfm rc-pull", calls)
        root = os.path.dirname(self.ebpf)
        self.assertEqual(self.authorized(),
                         'restrict,from="10.0.0.5",command="%s -ro %s" %s\n' % (rr, root, KEY))
        self.assertIn("FLEET_EBPF_SOURCE=ebpfm\n", out)
        self.assertIn("FLEET_NODE_SOURCE=ebpfm-login\n", out)
        # again: the key's line is replaced, not added
        self.install("--key", KEY, RRSYNC=rr)
        self.assertEqual(self.authorized(),
                         'restrict,command="%s -ro %s" %s\n' % (rr, root, KEY))

    def test_the_folders_the_collector_settings_name_are_what_the_key_reads(self):
        data = os.path.join(self.tmp, "data")
        with open(self.default, "w") as fh:
            fh.write("EBPFM_MIN_UID=0\nEBPFM_OUTPUT_DIR=%s/ebpfm\nEBPFM_NODE_OUTPUT_DIR=%s/ebpfm-login\n"
                     % (data, data))
        rr = os.path.join(self.bin, "rrsync")
        out = self.install("--key", KEY, RRSYNC=rr, EBPF_DIR="", NODE_DIR="")
        self.assertEqual(self.authorized(), 'restrict,command="%s -ro %s" %s\n' % (rr, data, KEY))
        self.assertIn("FLEET_EBPF_SOURCE=ebpfm\n", out)
        self.assertIn("FLEET_NODE_SOURCE=ebpfm-login\n", out)

    def test_a_quoted_group_is_the_group(self):
        with open(self.default, "w") as fh:
            fh.write("EBPFM_MIN_UID=0\nEBPFM_GROUP='research'\n")
        with open(self.env["STUB_DB"], "w") as fh:
            fh.write("group research\n")
        self.install("--key", KEY, RRSYNC="")
        self.assertIn("usermod -a -G research rc-pull", self.calls())

    def test_without_rrsync_the_key_is_restricted_but_not_to_rsync(self):
        out = self.install("--key", KEY, RRSYNC="")
        self.assertEqual(self.authorized(), "restrict %s\n" % KEY)
        self.assertIn("no rrsync", out)
        self.assertIn("FLEET_EBPF_SOURCE=%s\n" % self.ebpf, out)

    def test_other_lines_stay(self):
        os.makedirs(os.path.join(self.tmp, "homes", "rc-pull", ".ssh"))
        with open(os.path.join(self.tmp, "homes", "rc-pull", ".ssh", "authorized_keys"), "w") as fh:
            fh.write("ssh-ed25519 AAAAother admin\n")
        with open(self.env["STUB_DB"], "w") as fh:
            fh.write("user rc-pull\n")
        self.install("--key", KEY, RRSYNC="")
        self.assertEqual(self.authorized(), "ssh-ed25519 AAAAother admin\nrestrict %s\n" % KEY)

    def test_a_key_that_is_not_one(self):
        out = self.install("--key", "ssh-ed25519 AAAA\nssh-rsa BBBB", code=1)
        self.assertIn("not one OpenSSH public key line", out)
        out = self.install("--key", KEY, "--from", '1.2.3.4" evil', code=1)
        self.assertIn("not an address list", out)


if __name__ == "__main__":
    unittest.main()
