"""The installers, run for real against a copy of the repository and folders here in place
of /etc, /var/lib, /opt and the units' folder. `id` says root; systemctl, the account tools,
chown, uv, npm and ssh-keygen are stand-ins that log their calls."""
import hashlib
import json
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
    "userdel": STUB + 'sed -i "/^user ${@: -1}$/d" "$STUB_DB"\n',
    "groupdel": STUB + 'sed -i "/^group ${@: -1}$/d" "$STUB_DB"\n',
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
  show) echo "${STUB_MAINPID:-4242}" ;;
esac
exit 0
''',
    # uv sync (a checkout's venv), uv venv PATH (a release's), uv pip install (logged)
    "uv": STUB + r'''
case "$1" in
  venv) for last in "$@"; do :; done
        mkdir -p "$last/bin"; ln -sf "$STUB_PYTHON" "$last/bin/python" ;;
  pip) exit 0 ;;
  *) mkdir -p "$UV_PROJECT_ENVIRONMENT/bin"
     ln -sf "$STUB_PYTHON" "$UV_PROJECT_ENVIRONMENT/bin/python" ;;
esac
''',
    # a release's files, from STUB_RELEASE: curl ... -o OUT URL
    "curl": STUB + r'''
out= url=
while [ $# -gt 0 ]; do
  case $1 in -o) out=$2; shift 2 ;; -*) shift ;; *) url=$1; shift ;; esac
done
f="$STUB_RELEASE/$(basename "$url")"
[ -n "${STUB_RELEASE:-}" ] && [ -f "$f" ] || exit 22
cp "$f" "$out"
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
    # install-node.sh only asks whether rsync is here
    "rsync": "#!/bin/sh\nexit 0\n",
    # A safety net: should anything run the collector's bootstrap here, it reaches these
    # stand-ins and fails, and no package or kernel setting of this host changes.
    "apt-get": STUB + "exit 1\n",
    "dnf": STUB + "exit 1\n",
    "yum": STUB + "exit 1\n",
    "sysctl": STUB + "exit 1\n",
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
            NODE_DIR=self.node, EBPFM_PID_DIR=os.path.join(t, "run"), HOME=t,
            INSTALL_ENV=os.path.join(t, "etc", "rc-dashboard-install", "install.env"),
            EBPFM_SYSCTL_CONF=os.path.join(t, "etc", "sysctl.d", "99-ebpfm.conf"),
            RC_MEMINFO=os.path.join(t, "meminfo"))
        self.install_env = self.env["INSTALL_ENV"]
        self.host_memory(16)        # half of it, 8G, is the dashboard's cap unless told
        for k in ("SLURM", "SITE_NAME", "SITE_DESCRIPTION", "NODE_NOUN", "ANONYMIZE", "MEMORY_MAX",
                  "DASH_MEMORY_MAX", "NODES", "FREE_MIN_GB", "SERVICE_USER", "PULL_USER",
                  "COLLECTOR_GROUP", "SUDO_USER", "PLAN"):
            self.env.pop(k, None)

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

    def host_memory(self, gib, kb=None):
        """The memory this host has, as /proc/meminfo says it: `gib` whole, or `kb` as a
        real host says it, a little under its nominal size."""
        with open(self.env["RC_MEMINFO"], "w") as fh:
            fh.write("MemTotal:       %d kB\nMemFree:         1024 kB\n" % (kb or gib * 1048576))

    def memory(self):
        """The dashboard's cap, from its drop-in."""
        p = os.path.join(self.units, "rc-dashboard.service.d", "50-memory.conf")
        return re.search(r"^MemoryMax=(.*)$", self.read(p), re.M).group(1)

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
        # the folders, for every later run and for `ebpfm status`
        self.assertEqual(defaults["EBPFM_OUTPUT_DIR"], self.ebpf)
        self.assertEqual(defaults["EBPFM_NODE_OUTPUT_DIR"], self.node)
        # each unit reads the settings file the installer wrote (moved here, as the rig
        # moves it), and tells ebpfm.sh the same
        ebpfm = self.unit("ebpfm.service")
        self.assertEqual(ebpfm["EnvironmentFile"], ["-" + self.default])
        self.assertEqual(ebpfm["Environment"], ["EBPFM_DEFAULT_FILE=" + self.default,
                                                "EBPFM_OUTPUT_DIR=" + self.ebpf])
        node = self.unit("ebpfm-node.service")
        self.assertEqual(node["EnvironmentFile"], ["-" + self.default])
        self.assertEqual(node["Environment"], ["EBPFM_NODE_OUTPUT_DIR=" + self.node,
                                               "EBPFM_DEFAULT_FILE=" + self.default])
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
        self.assertNotIn("MemoryMax", dash)
        self.assertEqual(self.memory(), "8G")
        # a member of the collector's group, so `sudo -u rc-dashboard ... check-feeds` reads it
        self.assertIn("usermod -a -G ebpfm rc-dashboard", calls)
        # the hourly check
        check = self.unit("rc-dashboard-check.service")
        self.assertEqual(check["User"], ["rc-dashboard"])
        self.assertEqual(check["SupplementaryGroups"], ["ebpfm"])
        self.assertEqual(check["ExecStart"], [self.repo + "/deploy/common/check.sh"])
        self.assertEqual(self.unit("rc-dashboard-check.timer")["OnUnitActiveSec"], ["1h"])
        self.assertIn("systemctl enable --quiet --now rc-dashboard-check.timer", calls)
        for name in ("rc-dashboard.service", "ebpfm.service", "ebpfm-node.service",
                     "rc-dashboard-check.service", "rc-dashboard-check.timer"):
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
        self.assertIn("systemctl enable --quiet rc-dashboard.service", calls)
        self.assertNotIn("systemctl start rc-dashboard", calls)
        self.assertIn("deploy/common/password.sh", out)
        # no Slurm said: the default, said out loud
        self.assertIn("SLURM not given", out)
        saved = self.keys(self.install_env)
        self.assertEqual((saved["MODE"], saved["SERVICE_USER"], saved["ETC"], saved["MEMORY_MAX"]),
                         ("single", "rc-dashboard", self.etc, "8G"))
        self.assertEqual(saved["MADE_GROUP"], "yes")

    def collector_running(self, group=None):
        """The eBPF tier as it runs: ebpfm.service active, its MainPID a process whose
        environment has EBPFM_GROUP=<group>, or none."""
        env = {k: v for k, v in os.environ.items() if k != "EBPFM_GROUP"}
        if group:
            env["EBPFM_GROUP"] = group
        p = subprocess.Popen(["sleep", "60"], env=env)
        self.addCleanup(p.wait)
        self.addCleanup(p.kill)
        return {"STUB_ACTIVE": "ebpfm.service", "STUB_MAINPID": str(p.pid)}

    def test_a_collector_running_without_the_group_is_not_left_so(self):
        """It reads EBPFM_GROUP at start: run on as it is, every file it makes from now on
        would be root's alone, the dashboard's reads included. Not restarted unasked, since
        a restart flushes its live processes as `truncated`: refused, or --restart."""
        running = self.collector_running()
        out = self.install(code=1, **running)
        self.assertIn("--restart", out)
        self.assertNotIn("systemctl restart ebpfm", self.calls())
        self.assertNotIn("readable by group", out)
        out = self.install("--restart", **running)
        self.assertIn("systemctl restart ebpfm.service", self.calls())

    def test_a_collector_running_with_the_group_keeps_running(self):
        """An upgrade over a collector that already writes for the group leaves it be."""
        self.install(**self.collector_running(group="ebpfm"))
        self.assertNotIn("systemctl restart ebpfm", self.calls())

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
        self.assertEqual(self.unit("ebpfm.service")["Environment"],
                         ["EBPFM_DEFAULT_FILE=" + self.default, "EBPFM_OUTPUT_DIR=" + data])
        self.assertEqual(self.unit("ebpfm-node.service")["Environment"],
                         ["EBPFM_NODE_OUTPUT_DIR=" + node, "EBPFM_DEFAULT_FILE=" + self.default])

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


class TestRelease(Rig):
    """--release vX.Y.Z: the dashboard as that release published it. Its wheel and pinned
    requirements are each checked against the release's checksums before anything is
    installed; no Node, npm or build runs here; and the service reads nothing from the
    folder the kit was unpacked in, since the scripts its units run are copied to
    OPT/kit."""

    VER = "1.2.3"

    def setUp(self):
        super().setUp()
        # a release's kit holds no dashboard code for the service to read
        shutil.rmtree(os.path.join(self.repo, "dashboard"))
        self.rel = os.path.join(self.tmp, "release")
        os.makedirs(self.rel)
        self.wheel = "rc_dashboard-%s-py3-none-any.whl" % self.VER
        self.reqs = "rc_dashboard-%s-requirements.txt" % self.VER
        for name, body in ((self.wheel, b"a wheel"), (self.reqs, b"fastapi==1 --hash=sha256:00\n")):
            with open(os.path.join(self.rel, name), "wb") as fh:
                fh.write(body)
        self.sums()

    def sums(self, leave_out=None, **wrong):
        lines = []
        for name in (self.wheel, self.reqs):
            if name == leave_out:
                continue
            with open(os.path.join(self.rel, name), "rb") as fh:
                digest = hashlib.sha256(fh.read()).hexdigest()
            lines.append("%s  %s\n" % (wrong.get(name, digest), name))
        with open(os.path.join(self.rel, "rc_dashboard-%s.sha256" % self.VER), "w") as fh:
            fh.writelines(lines)

    def install(self, *args, code=0, **env):
        return self.run_script("deploy/fleet/install-dashboard.sh", *args, code=code,
                               STUB_RELEASE=self.rel, **env)

    def test_the_release_is_installed_and_no_unit_points_into_the_kit(self):
        self.install("--release", "v" + self.VER)
        calls = self.calls()
        url = "https://github.com/HarvardMadSys/agentic-hpc-dashboard/releases/download/v1.2.3/"
        for name in (self.wheel, self.reqs, "rc_dashboard-1.2.3.sha256"):
            self.assertIn(url + name, calls)
        self.assertIn("uv venv --allow-existing", calls)
        self.assertRegex(calls, r"uv pip install .*--require-hashes -r \S+/%s" % re.escape(self.reqs))
        self.assertRegex(calls, r"uv pip install .*--no-deps --reinstall \S+/%s" % re.escape(self.wheel))
        self.assertNotIn("npm", calls)
        self.assertNotIn("uv sync", calls)
        kit = os.path.join(self.opt, "kit")
        for f in ("deploy/common/check.sh", "deploy/common/lib.sh", "deploy/fleet/pull.py"):
            self.assertTrue(os.path.isfile(os.path.join(kit, f)), f)
        for unit in ("rc-dashboard.service", "rc-dashboard-check.service",
                     "rc-dashboard-check.timer", "rc-pull.service"):
            self.assertNotIn(self.repo, self.read(os.path.join(self.units, unit)), unit)
        self.assertEqual(self.unit("rc-pull.service")["ExecStart"],
                         ["%s/venv/bin/python %s/deploy/fleet/pull.py --config %s/fleet.env loop"
                          % (self.opt, kit, self.etc)])
        self.assertEqual(self.unit("rc-dashboard-check.service")["ExecStart"],
                         ["%s/deploy/common/check.sh" % kit])
        self.assertEqual(self.unit("rc-dashboard.service")["WorkingDirectory"], [self.state])
        self.assertIn("https://github.com/HarvardMadSys/agentic-hpc-dashboard/blob/v1.2.3/"
                      "deploy/README.md", self.read(os.path.join(self.units, "rc-dashboard.service")))
        # the wheel carries the page: the service finds it in the package
        self.assertNotIn("RC_DASH_STATIC_DIR", self.keys(os.path.join(self.etc, "paths.env")))
        self.assertEqual(self.keys(self.install_env)["RELEASE"], "v1.2.3")

    def test_a_file_the_release_did_not_publish_installs_nothing(self):
        self.sums(**{self.wheel: "0" * 64})
        out = self.install("--release", "1.2.3", code=1)
        self.assertIn("is not the one it published", out)
        self.assertNotIn("uv pip install", self.calls())
        self.sums(leave_out=self.reqs)
        out = self.install("--release", "1.2.3", code=1)
        self.assertIn("names no %s" % self.reqs, out)
        self.assertNotIn("uv pip install", self.calls())

    def test_the_release_kept_in_install_env_is_installed_again(self):
        self.install("--release", "v" + self.VER)
        with open(self.log, "w"):
            pass
        self.install()                      # an upgrade, told nothing
        self.assertIn("/v1.2.3/%s" % self.wheel, self.calls())
        self.assertNotIn("npm", self.calls())

    def test_a_name_that_is_no_release_is_refused(self):
        out = self.install("--release", "latest", code=1)
        self.assertIn("is not a release", out)


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
        self.assertEqual(pull["RestartPreventExitStatus"], ["2 3 4"])
        self.assertEqual(pull["ExecReload"], ["/bin/kill -HUP $MAINPID"])
        self.assertEqual(fleet["FLEET_HOSTS"], "", "no example nodes to pull by mistake")
        self.assertEqual(self.unit("rc-dashboard.service")["SupplementaryGroups"], [""])
        self.assertNotIn("systemctl start rc-pull", self.calls())
        self.assertNotIn("groupadd", self.calls())

    def test_named_nodes_start_the_pull_and_are_where_the_dashboard_reads(self):
        self.install()
        p = os.path.join(self.etc, "fleet.env")
        with open(p) as fh:
            text = fh.read().replace('FLEET_HOSTS=\n', 'FLEET_HOSTS="gpu1 gpu2"\n')
        with open(p, "w") as fh:
            fh.write(text)
        self.install("--no-build")
        self.assertIn("systemctl start rc-pull.service", self.calls())
        m = self.state + "/mirror"
        paths = self.keys(os.path.join(self.etc, "paths.env"))
        # a glob the dashboard expands again at every rescan: a node added needs no restart
        self.assertEqual(paths["RC_DASH_EBPF_ROOTS"], "%s/ebpf/*" % m)
        self.assertEqual(paths["RC_DASH_NODE_ROOTS"], "%s/node/*" % m)
        self.assertEqual(paths["RC_DASH_TRANSPORT_FILES"], "%s/.state/*.json" % m)
        self.install("--no-build", STUB_ACTIVE="rc-pull.service")
        self.assertIn("systemctl restart rc-pull.service", self.calls())

    def test_the_old_templates_example_nodes_are_no_nodes(self):
        """A fleet.env from before kept the example's server01 server02 until you named
        yours: still no nodes, as it was."""
        self.install()
        p = os.path.join(self.etc, "fleet.env")
        with open(p) as fh:
            text = fh.read().replace('FLEET_HOSTS=\n', 'FLEET_HOSTS="server01 server02"\n')
        with open(p, "w") as fh:
            fh.write(text.replace("FLEET_DAYS=8", "FLEET_DAYS=7"))
        out = self.install("--no-build")
        self.assertIn("still names the example's nodes", out)
        self.assertNotIn("systemctl start rc-pull", self.calls())

    def test_nodes_given_at_the_first_install(self):
        out = self.install(NODES="gpu1 gpu2=10.0.0.2:/scratch/ebpfm:", FREE_MIN_GB="50")
        fleet = self.keys(os.path.join(self.etc, "fleet.env"))
        self.assertEqual(fleet["FLEET_HOSTS"], '"gpu1 gpu2=10.0.0.2:/scratch/ebpfm:"')
        self.assertEqual(fleet["FLEET_FREE_MIN_GB"], "50")
        self.assertIn("systemctl start rc-pull.service", self.calls())
        out = self.install("--no-build", NODES="gpu3")
        self.assertIn("NODES is left out", out)
        self.assertEqual(self.keys(os.path.join(self.etc, "fleet.env"))["FLEET_HOSTS"],
                         '"gpu1 gpu2=10.0.0.2:/scratch/ebpfm:"')

    def test_a_fleet_emptied_on_purpose_stays_empty(self):
        """NODES names the nodes of the fleet.env the install makes. A fleet whose last node
        was taken out (`rc-deploy remove-node` leaves FLEET_HOSTS="") is empty on purpose:
        the next apply or upgrade, handed install.env's NODES again, took it for one never
        set up, named the retired nodes again and pulled them."""
        self.install(NODES="n1")
        p = os.path.join(self.etc, "fleet.env")
        self.assertEqual(self.keys(p)["FLEET_HOSTS"], "n1")
        text = re.sub(r"(?m)^FLEET_HOSTS=.*$", 'FLEET_HOSTS=""', self.read(p))
        with open(p, "w") as fh:
            fh.write(text)
        self.assertEqual(self.keys(p)["FLEET_HOSTS"], '""')
        with open(self.log, "w"):
            pass
        out = self.install("--no-build", NODES="n1")
        self.assertEqual(self.keys(p)["FLEET_HOSTS"], '""')
        self.assertIn("NODES is left out", out)
        self.assertNotIn("systemctl start rc-pull", self.calls())

    def test_a_wrong_fleet_env_stops_the_install(self):
        self.install()
        with open(os.path.join(self.etc, "fleet.env"), "a") as fh:
            fh.write("FLEET_HOST=typo\n")
        out = self.install("--no-build", code=1)
        self.assertIn("unknown setting", out)


class TestAnswersStay(Rig):
    """What an install was told stays for every later run: the collector's folders in
    /etc/default/ebpfm, the installer's own answers in install.env, the memory cap in its
    drop-in. A run that is not told again neither moves the collector nor shrinks the cap."""

    def install(self, *args, **env):
        return self.run_script("deploy/single/install.sh", "--no-bootstrap", "--no-check",
                               *args, **env)

    def test_a_rerun_without_the_environment_keeps_every_answer(self):
        data = os.path.join(self.tmp, "data", "ebpfm")
        node = os.path.join(self.tmp, "data", "ebpfm-login")
        self.install(EBPF_DIR=data, NODE_DIR=node, MEMORY_MAX="16G")
        self.install("--no-build", EBPF_DIR="", NODE_DIR="")
        self.assertEqual(self.unit("ebpfm.service")["Environment"],
                         ["EBPFM_DEFAULT_FILE=" + self.default, "EBPFM_OUTPUT_DIR=" + data])
        self.assertEqual(self.unit("ebpfm-node.service")["Environment"],
                         ["EBPFM_NODE_OUTPUT_DIR=" + node, "EBPFM_DEFAULT_FILE=" + self.default])
        paths = self.keys(os.path.join(self.etc, "paths.env"))
        self.assertEqual((paths["RC_DASH_EBPF_ROOTS"], paths["RC_DASH_NODE_ROOTS"]), (data, node))
        self.assertEqual(self.memory(), "16G")
        self.assertEqual(self.keys(self.install_env)["MEMORY_MAX"], "16G")
        self.assertEqual(self.keys(self.default)["EBPFM_OUTPUT_DIR"], data)

    def test_a_change_is_printed(self):
        self.install(MEMORY_MAX="16G")
        out = self.install("--no-build", MEMORY_MAX="12G")
        self.assertIn("MEMORY_MAX: 16G -> 12G", out)
        self.assertEqual(self.memory(), "12G")
        out = self.install("--no-build", DASH_MEMORY_MAX="10G")       # the hpc kit's name
        self.assertEqual(self.memory(), "10G")

    def test_an_edit_of_the_drop_in_stays(self):
        """`systemctl edit rc-dashboard` is how a cap is changed for good."""
        self.install()
        p = os.path.join(self.units, "rc-dashboard.service.d", "50-memory.conf")
        with open(p, "w") as fh:
            fh.write("[Service]\nMemoryMax=20G\n")
        self.install("--no-build")
        self.assertEqual(self.memory(), "20G")
        self.assertEqual(self.keys(self.install_env)["MEMORY_MAX"], "20G")

    def test_the_cap_follows_the_host_unless_given(self):
        """Untold, the cap is half the host's memory, from 2G to 32G, said as it is chosen;
        a host whose memory cannot be read gets 8G, said too. MemTotal is a little under a
        host's nominal size, so it is rounded to the nearest gigabyte first: a "16 GB" host
        that says 15.5 GiB gets 8G, not 7G. Once chosen the cap is saved, so the next run
        keeps it, and says when it is more than the host has."""
        for gib, kb, said, cap in ((3, None, 3, "2G"), (24, None, 24, "12G"),
                                   (16, 16265000, 16, "8G"), (15, 15400000, 15, "8G"),
                                   (64, 65700000, 63, "32G"), (512, None, 512, "32G")):
            with self.subTest(gib=gib, kb=kb):
                for p in (self.install_env, os.path.join(self.units, "rc-dashboard.service.d",
                                                         "50-memory.conf")):
                    if os.path.exists(p):
                        os.remove(p)
                self.host_memory(gib, kb)
                out = self.install()
                self.assertIn("MEMORY_MAX=%s: half this host's %dG" % (cap, said), out)
                self.assertEqual(self.memory(), cap)
                self.assertNotIn("caps nothing", out)
        self.host_memory(8)
        out = self.install("--no-build")
        self.assertNotIn("half this host's", out)
        self.assertEqual(self.memory(), "32G")                   # saved, not chosen again
        self.assertIn("warning: MEMORY_MAX=32G is more than this host's 8G, so it caps nothing", out)
        os.remove(self.install_env)
        os.remove(os.path.join(self.units, "rc-dashboard.service.d", "50-memory.conf"))
        os.remove(self.env["RC_MEMINFO"])
        out = self.install("--no-build")
        self.assertEqual(self.memory(), "8G")
        self.assertIn("MEMORY_MAX=8G: could not read %s" % self.env["RC_MEMINFO"], out)

    def test_a_folder_moved_by_hand_is_refused_not_followed(self):
        """Told another folder than the one the collector writes, a run stops: following
        it would leave the collector's days behind, and the dashboard reading the wrong one."""
        self.install()
        out = self.install("--no-build", code=1, EBPF_DIR=os.path.join(self.tmp, "elsewhere"))
        self.assertIn("settle on one", out)

    def test_an_install_from_before_keeps_its_answers(self):
        """An install made before install.env existed: its folders are in its units and its
        cap in the dashboard's unit, and the first run of this installer takes them there."""
        data = os.path.join(self.tmp, "data", "ebpfm")
        for name, line in (("ebpfm.service", "Environment=EBPFM_OUTPUT_DIR=" + data),
                           ("ebpfm-node.service", "Environment=EBPFM_NODE_OUTPUT_DIR=" + data + "-login"),
                           ("rc-dashboard.service", "User=rc-dashboard\nMemoryMax=24G")):
            with open(os.path.join(self.units, name), "w") as fh:
                fh.write("[Service]\n%s\n" % line)
        with open(self.default, "w") as fh:
            fh.write("EBPFM_MIN_UID=0\nEBPFM_GROUP=ebpfm\n")
        with open(self.env["STUB_DB"], "w") as fh:
            fh.write("group ebpfm\n")
        out = self.install(EBPF_DIR="", NODE_DIR="")
        self.assertIn("MEMORY_MAX=24G, as the installed rc-dashboard.service has it", out)
        self.assertEqual(self.memory(), "24G")
        self.assertEqual(self.keys(os.path.join(self.etc, "paths.env"))["RC_DASH_EBPF_ROOTS"], data)
        self.assertEqual(self.keys(self.default)["EBPFM_OUTPUT_DIR"], data)


class TestSettingsWords(Rig):
    """The names every kit gives the same settings, set in dashboard.env."""

    def install(self, *args, **env):
        return self.run_script("deploy/single/install.sh", "--no-bootstrap", "--no-check",
                               *args, **env)

    def test_slurm_is_asked_not_taken_from_root(self):
        out = self.install(SLURM="yes", ANONYMIZE="On", SITE_DESCRIPTION="two login nodes")
        d = self.keys(os.path.join(self.etc, "dashboard.env"))
        self.assertEqual(d["RC_DASH_SLURM"], "1")
        self.assertEqual(d["RC_DASH_NODE_NOUN"], '"login nodes"')
        self.assertEqual(d["RC_DASH_SITE_NAME"], '"login nodes · live"')
        self.assertEqual(d["RC_DASH_ANONYMIZE"], "1")
        self.assertEqual(d["RC_DASH_SITE_DESC"], '"two login nodes"')
        self.assertNotIn("SLURM not given", out)

    def test_a_setting_given_later_changes_that_line_alone(self):
        self.install()
        p = os.path.join(self.etc, "dashboard.env")
        with open(p, "a") as fh:
            fh.write("RC_DASH_RDNS=0\n")
        out = self.install("--no-build", SITE_NAME="lab · live", ANONYMIZE="false")
        d = self.keys(p)
        self.assertEqual(d["RC_DASH_SITE_NAME"], '"lab · live"')
        self.assertEqual(d["RC_DASH_ANONYMIZE"], "0")
        self.assertEqual(d["RC_DASH_RDNS"], "0", "your own lines stay")
        self.assertEqual(d["RC_DASH_SLURM"], "0")
        self.assertIn("RC_DASH_SITE_NAME in %s: servers · live -> lab · live" % p, out)

    def test_words_that_are_not_yes_or_no(self):
        out = self.install(code=1, SLURM="maybe")
        self.assertIn("SLURM='maybe' is not one of", out)
        self.assertFalse(os.path.exists(os.path.join(self.etc, "dashboard.env")))


class TestHelpAndPlan(Rig):
    def test_help(self):
        for rel in ("deploy/single/install.sh", "deploy/fleet/install-dashboard.sh",
                    "deploy/fleet/install-node.sh", "deploy/common/install-collector.sh",
                    "deploy/common/install-dashboard.sh", "deploy/common/password.sh",
                    "deploy/common/check.sh", "deploy/common/uninstall.sh"):
            out = self.run_script(rel, "--help")
            self.assertIn(os.path.basename(rel), out.splitlines()[0], rel)
            self.assertNotIn("set -e", out, rel)
            self.assertGreater(len(out.splitlines()), 3, rel)

    def test_plan_changes_nothing(self):
        before = sorted(os.listdir(self.tmp))
        out = self.run_script("deploy/single/install.sh", "--plan")
        self.assertIn("would run: groupadd --system ebpfm", out)
        self.assertIn("would run: useradd --system", out)
        self.assertIn("would write %s/dashboard.env (new)" % self.etc, out)
        self.assertIn("would write %s/rc-dashboard.service (new)" % self.units, out)
        self.assertIn("would run: systemctl enable --quiet --now rc-dashboard-check.timer", out)
        self.assertIn("nothing was changed", out)
        self.assertEqual(sorted(set(os.listdir(self.tmp)) - {"calls.log"}), before)
        for d in (self.etc, self.state, self.opt, os.path.dirname(self.install_env)):
            self.assertFalse(os.path.exists(d), d)
        self.assertEqual(os.listdir(self.units), [])
        self.assertFalse(os.path.exists(self.default))
        calls = self.calls()
        for word in ("groupadd", "useradd", "usermod", "systemctl enable", "uv sync", "npm"):
            self.assertNotIn(word, calls, word)

    def test_plan_over_an_install_shows_the_changes(self):
        self.run_script("deploy/single/install.sh", "--no-bootstrap", "--no-check")
        out = self.run_script("deploy/single/install.sh", "--plan", "--no-build", "--no-bootstrap",
                              "--no-check", SITE_NAME="lab · live")
        self.assertIn("would change %s/dashboard.env" % self.etc, out)
        self.assertIn('+RC_DASH_SITE_NAME="lab · live"', out)
        self.assertIn("%s/paths.env stays as it is" % self.etc, out)
        d = self.keys(os.path.join(self.etc, "dashboard.env"))
        self.assertEqual(d["RC_DASH_SITE_NAME"], '"servers · live"', "the plan changed nothing")


PY_STUB = r"""#!/bin/bash
# the venv's Python, for password.sh: new-password as the dashboard's contract has it
echo "python $*" >> "$STUB_LOG"
if [ "$1" = -m ] && [ "$3" = new-password ]; then
  shift 3; dir= inst= force=0
  while [ $# -gt 0 ]; do
    case $1 in --dir) dir=$2; shift 2 ;; --install) inst=$2; shift 2 ;; --force) force=1; shift ;; *) shift ;; esac
  done
  if [ -e "$dir/password" ] && [ "$force" = 0 ]; then echo "$dir/password already exists" >&2; exit 1; fi
  mkdir -p "$dir" "$(dirname "$inst")"
  n=$(date +%s%N)
  echo "pw-$n" > "$dir/password"; echo "machine 127.0.0.1 login rc password pw-$n" > "$dir/netrc"
  echo "scrypt\$16384\$8\$1\$c2FsdHNhbHQ=\$aGFzaGhhc2hoYXNoaGFzaA==$n" > "$inst"
  exit 0
fi
if [ "$1" = -c ]; then echo "scrypt\$derived" > "${@: -1}"; exit 0; fi
exec python3 "$@"
"""


class TestPassword(Rig):
    def setUp(self):
        super().setUp()
        self.run_script("deploy/single/install.sh", "--no-bootstrap", "--no-check")
        py = os.path.join(self.opt, "venv", "bin", "python")
        os.unlink(py)
        with open(py, "w") as fh:
            fh.write(PY_STUB)
        os.chmod(py, 0o755)
        with open(self.env["STUB_DB"], "a") as fh:
            fh.write("user alice\n")
        self.mine = os.path.join(self.tmp, "homes", "alice", ".config", "rc-dashboard")

    def password(self, *args, **env):
        return self.run_script("deploy/common/password.sh", *args, SUDO_USER="alice", **env)

    def test_one_step_from_nothing_to_a_running_dashboard(self):
        out = self.password()
        hashed = os.path.join(self.etc, "password.hash")
        self.assertTrue(self.read(hashed).startswith("scrypt$"))
        self.assertTrue(os.path.exists(os.path.join(self.mine, "password")))
        self.assertEqual(self.keys(os.path.join(self.etc, "paths.env"))["RC_DASH_PASSWORD_HASH_FILE"],
                         hashed)
        self.assertIn("systemctl start rc-dashboard.service", self.calls())
        self.assertIn("--netrc-file", out)

    def test_rotation_needs_no_restart(self):
        self.password()
        first = self.read(os.path.join(self.etc, "password.hash"))
        pw = self.read(os.path.join(self.mine, "password"))
        open(self.log, "w").close()
        out = self.password()                       # again: the same password, nothing new
        self.assertEqual(self.read(os.path.join(self.mine, "password")), pw)
        self.password("--rotate", STUB_ACTIVE="rc-dashboard.service")
        self.assertNotEqual(self.read(os.path.join(self.etc, "password.hash")), first)
        self.assertNotEqual(self.read(os.path.join(self.mine, "password")), pw)
        self.assertNotIn("systemctl restart rc-dashboard", self.calls())
        self.assertNotIn("systemctl start rc-dashboard", self.calls())

    def test_a_password_from_before_hash_files_is_kept(self):
        os.makedirs(self.mine)
        with open(os.path.join(self.mine, "password"), "w") as fh:
            fh.write("old-password\n")
        p = os.path.join(self.etc, "dashboard.env")
        with open(p) as fh:
            text = fh.read().replace("RC_DASH_PASSWORD_HASH=\n", "RC_DASH_PASSWORD_HASH='scrypt$old'\n")
        with open(p, "w") as fh:
            fh.write(text)
        out = self.password()
        self.assertEqual(self.read(os.path.join(self.mine, "password")), "old-password\n")
        self.assertEqual(self.read(os.path.join(self.etc, "password.hash")), "scrypt$derived\n")
        self.assertNotIn("RC_DASH_PASSWORD_HASH", self.keys(p))
        self.assertIn("commenting out RC_DASH_PASSWORD_HASH", out)


RESTORE_STUB = r"""#!/bin/bash
# the venv's Python, for backup.sh --restore: rc-dashboard restore as its contract has it,
# writing where the settings it was given say, and saying which settings those were
echo "python $*" >> "$STUB_LOG"
if [ "$1" = -m ] && [ "$3" = restore ]; then
  echo "restore as $(id -un 2>/dev/null) hash_file=${RC_DASH_PASSWORD_HASH_FILE:-} inline=${RC_DASH_PASSWORD_HASH-unset}" >> "$STUB_LOG"
  if [ -n "${STUB_RESTORE_FAIL:-}" ]; then echo "restore: $STUB_RESTORE_FAIL" >&2; exit 1; fi
  mkdir -p "$RC_DASH_CACHE_DIR"
  echo restored-key > "$RC_DASH_CACHE_DIR/session.key"
  if [ -z "${STUB_RESTORE_NO_HASH:-}" ]; then
    (umask 027; echo 'scrypt$restored' > "$RC_DASH_PASSWORD_HASH_FILE")
    echo "  password.hash          -> $RC_DASH_PASSWORD_HASH_FILE"
  fi
  exit 0
fi
exec python3 "$@"
"""


class TestRestore(Rig):
    """backup.sh --restore on an installed host, whose ETC (and the password's hash in
    it) is root's, and which the dashboard's account may only read."""

    def setUp(self):
        super().setUp()
        self.run_script("deploy/single/install.sh", "--no-bootstrap", "--no-check")
        py = os.path.join(self.opt, "venv", "bin", "python")
        os.unlink(py)
        with open(py, "w") as fh:
            fh.write(RESTORE_STUB)
        os.chmod(py, 0o755)
        self.tar = os.path.join(self.tmp, "backup.tar")
        with open(self.tar, "w") as fh:
            fh.write("a backup\n")
        self.hashed = os.path.join(self.etc, "password.hash")
        open(self.log, "w").close()

    def restore(self, code=0, **env):
        return self.run_script("deploy/common/backup.sh", "--restore", self.tar, code=code, **env)

    def restore_line(self):
        return [l for l in self.calls().splitlines() if l.startswith("restore as")][0]

    def test_a_host_without_a_password_takes_the_restored_one(self):
        """Moving: the new host has no password yet. The hash goes in ETC as password.sh
        puts it, paths.env names it, and the dashboard starts reading it."""
        out = self.restore()
        self.assertEqual(self.read(self.hashed), "scrypt$restored\n")
        self.assertEqual(self.keys(os.path.join(self.etc, "paths.env"))["RC_DASH_PASSWORD_HASH_FILE"],
                         self.hashed)
        calls = self.calls()
        self.assertIn("runuser -u rc-dashboard -- env -C", calls, "the restore runs as the account")
        self.assertRegex(calls, r"chown root:rc-dashboard \S*/\.password\.hash\.")
        self.assertIn("systemctl stop rc-dashboard.service", calls)
        self.assertIn("systemctl start rc-dashboard.service", calls)
        # the hash went to the account's folder first, never straight into ETC
        self.assertRegex(self.restore_line(), r"hash_file=\S*/\.restore\.[^/]+/password\.hash ")
        self.assertIn("naming the hash file in paths.env", out)
        self.assertEqual([n for n in os.listdir(self.state) if n.startswith(".restore.")], [],
                         "the account's folder for the restore is gone")
        self.assertEqual(self.read(os.path.join(self.state, "cache", "session.key")), "restored-key\n")

    def test_a_host_with_a_password_has_it_replaced_without_an_install(self):
        with open(self.hashed, "w") as fh:
            fh.write("scrypt$old\n")
        with open(os.path.join(self.etc, "paths.env"), "a") as fh:
            fh.write("RC_DASH_PASSWORD_HASH_FILE=%s\n" % self.hashed)
        out = self.restore()
        self.assertEqual(self.read(self.hashed), "scrypt$restored\n")
        self.assertNotIn("naming the hash file", out)
        self.assertIn("systemctl start rc-dashboard.service", self.calls())

    def test_an_inline_hash_gives_way_to_the_restored_file(self):
        p = os.path.join(self.etc, "dashboard.env")
        with open(p) as fh:
            text = fh.read().replace("RC_DASH_PASSWORD_HASH=\n", "RC_DASH_PASSWORD_HASH='scrypt$old'\n")
        with open(p, "w") as fh:
            fh.write(text)
        out = self.restore()
        self.assertIn("inline= ", self.restore_line() + " ", "the restore ran without the inline hash")
        self.assertNotIn("RC_DASH_PASSWORD_HASH", self.keys(p))
        self.assertIn("commenting out RC_DASH_PASSWORD_HASH", out)
        self.assertEqual(self.read(self.hashed), "scrypt$restored\n")

    def test_a_backup_without_a_password_leaves_the_hosts_alone(self):
        with open(self.hashed, "w") as fh:
            fh.write("scrypt$old\n")
        self.restore(STUB_RESTORE_NO_HASH="1")
        self.assertEqual(self.read(self.hashed), "scrypt$old\n")
        self.assertIn("systemctl start rc-dashboard.service", self.calls())

    def test_a_failed_restore_starts_the_dashboard_again(self):
        out = self.restore(code=1, STUB_RESTORE_FAIL="the backup is damaged")
        self.assertIn("restore: the backup is damaged", out)
        self.assertIn("the dashboard starts again", out)
        calls = self.calls().splitlines()
        self.assertLess(calls.index("systemctl stop rc-dashboard.service"),
                        calls.index("systemctl start rc-dashboard.service"))
        self.assertFalse(os.path.exists(self.hashed))
        self.assertEqual([n for n in os.listdir(self.state) if n.startswith(".restore.")], [])


class TestCheck(Rig):
    def setUp(self):
        super().setUp()
        self.run_script("deploy/single/install.sh", "--no-bootstrap", "--no-check")

    def check(self, doc, rc=0, code=0, *args):
        p = os.path.join(self.tmp, "status.json")
        with open(p, "w") as fh:
            fh.write(doc if isinstance(doc, str) else json.dumps(doc))
        return self.run_script("deploy/common/check.sh", *args, code=code, DRY_SLACK="1",
                               RC_STATUS_CMD="cat %s; exit %d" % (p, rc))

    def test_ok_is_quiet(self):
        out = self.check({"level": "ok", "why": [], "hosts": []})
        self.assertIn(" ok", out)
        self.assertNotIn("slack", out)

    def test_warn_and_crit_go_to_slack(self):
        doc = {"level": "crit", "why": ["a collector is silent"],
               "hosts": [{"host": "login02", "verdict": "silent", "heartbeat_age_s": 7200,
                          "transport": {"kind": "rsync", "ok": False, "last_ok_age_s": 7200,
                                        "error": "Permission denied (publickey)"}},
                         {"host": "login01", "verdict": "ok", "transport": None}]}
        out = self.check(doc, rc=2, code=2)
        self.assertIn("slack (dry): *rc dashboard* on", out)
        self.assertIn("crit: a collector is silent; login02: silent (copy: Permission denied "
                      "(publickey))", out)
        self.assertNotIn("login01", out)
        out = self.check(dict(doc, level="warn", hosts=[]), rc=1, code=1)
        self.assertIn("warn: a collector is silent", out)

    def test_the_status_runs_the_code_the_unit_runs(self):
        """The status command runs in the unit's WorkingDirectory, so it imports what the
        dashboard runs: a checkout's dashboard/ folder, or a release's state folder, where
        only the venv's wheel imports. Not this script's own kit, which after a rollback is
        another release's code."""
        venv = os.path.join(self.tmp, "fake-venv")
        os.makedirs(os.path.join(venv, "bin"))
        py = os.path.join(venv, "bin", "python")
        with open(py, "w") as fh:
            fh.write('#!/bin/sh\nprintf \'{"level": "ok", "why": [], "hosts": [], "cwd": "%s"}\\n\' "$PWD"\n')
        os.chmod(py, 0o755)
        unit = os.path.join(self.units, "rc-dashboard.service")
        out = self.run_script("deploy/common/check.sh", "--print", RC_CHECK_VENV=venv)
        self.assertIn('"cwd": "%s/dashboard"' % self.repo, out)          # a checkout's
        with open(unit) as fh:
            text = fh.read()
        with open(unit, "w") as fh:
            fh.write(re.sub(r"(?m)^WorkingDirectory=.*$", "WorkingDirectory=%s" % self.state, text))
        out = self.run_script("deploy/common/check.sh", "--print", RC_CHECK_VENV=venv)
        self.assertIn('"cwd": "%s"' % self.state, out)                     # a release's

    def test_no_status_at_all_is_said(self):
        out = self.check("Traceback (most recent call last)", rc=1, code=3)
        self.assertIn("could not run rc-dashboard status (exit 1)", out)

    NO_STATUS = "echo \"error: argument command: invalid choice: 'status'\" >&2; exit 2"

    def fleet_with_copy(self, ok_epoch_ago):
        """MODE=fleet, one node n1 whose last pull passed `ok_epoch_ago` seconds ago."""
        import time as _t
        with open(self.install_env) as fh:
            text = fh.read().replace("MODE=single", "MODE=fleet")
        with open(self.install_env, "w") as fh:
            fh.write(text)
        mirror = os.path.join(self.tmp, "mirror")
        os.makedirs(os.path.join(mirror, ".state"))
        with open(os.path.join(self.etc, "fleet.env"), "w") as fh:
            fh.write("FLEET_HOSTS=n1\nFLEET_MIRROR=%s\n" % mirror)
        with open(os.path.join(mirror, ".state", "n1.json"), "w") as fh:
            json.dump({"ok": True, "ok_epoch": _t.time() - ok_epoch_ago,
                       "attempt_epoch": _t.time() - ok_epoch_ago}, fh)

    def test_a_release_without_status_is_left_alone(self):
        out = self.run_script("deploy/common/check.sh", DRY_SLACK="1", RC_STATUS_CMD=self.NO_STATUS)
        self.assertIn("this release has no rc-dashboard status: only the copy was checked, "
                      "and it is ok", out)
        self.assertNotIn("slack", out)

    def test_a_release_without_status_still_has_its_copy_checked(self):
        """A release from before `rc-dashboard status` once returned before the copy was
        looked at: a fleet whose pulls had all stopped passed its hourly check."""
        self.fleet_with_copy(86400)
        out = self.run_script("deploy/common/check.sh", code=2, DRY_SLACK="1",
                              RC_STATUS_CMD=self.NO_STATUS)
        self.assertIn("crit: copy: n1 STALE", out)
        self.assertIn("only the copy was checked", out)
        self.assertIn("slack (dry): *rc dashboard* on", out)

    def test_a_fleets_copy_that_stopped_is_crit(self):
        """rc-pull dead: every node's last state says ok, days ago. pull.py status says
        STALE, and the check says crit, whatever the dashboard has read."""
        self.fleet_with_copy(86400)
        out = self.check({"level": "ok", "why": [], "hosts": []}, code=2)
        self.assertIn("crit: copy: n1 STALE (no pass for 1d 0h: is rc-pull running?", out)


class TestUninstall(Rig):
    def setUp(self):
        super().setUp()
        self.run_script("deploy/single/install.sh", "--no-bootstrap", "--no-check")
        # as a host whose bootstrap wrote its sysctls (never run here)
        with open(self.install_env) as fh:
            text = fh.read().replace("RAN_BOOTSTRAP=no", "RAN_BOOTSTRAP=yes")
        with open(self.install_env, "w") as fh:
            fh.write(text)
        os.makedirs(os.path.dirname(self.env["EBPFM_SYSCTL_CONF"]))
        open(self.env["EBPFM_SYSCTL_CONF"], "w").close()
        os.makedirs(self.ebpf, exist_ok=True)

    def test_without_purge_settings_and_data_stay(self):
        open(self.log, "w").close()
        out = self.run_script("deploy/common/uninstall.sh")
        for name in ("rc-dashboard.service", "rc-dashboard-check.timer", "rc-dashboard.service.d"):
            self.assertFalse(os.path.exists(os.path.join(self.units, name)), name)
        self.assertIn("uninstall-unit", out + self.calls() + "uninstall-unit")
        self.assertFalse(os.path.exists(self.opt))
        self.assertFalse(os.path.exists(self.env["EBPFM_SYSCTL_CONF"]))
        for kept in (self.etc, self.state, self.ebpf):
            self.assertTrue(os.path.exists(kept), kept)
            self.assertIn(kept, out)
        self.assertNotIn("userdel", self.calls())

    def test_purge_takes_what_the_installers_made(self):
        out = self.run_script("deploy/common/uninstall.sh", "--purge")
        for gone in (self.etc, self.state, self.opt, self.default):
            self.assertFalse(os.path.exists(gone), gone)
        calls = self.calls()
        self.assertIn("userdel rc-dashboard", calls)
        self.assertIn("groupdel ebpfm", calls)
        self.assertTrue(os.path.exists(self.ebpf), "day-files are never removed")
        self.assertIn("(the collector's day-files)", out)

    def test_plan(self):
        out = self.run_script("deploy/common/uninstall.sh", "--purge", "--plan")
        self.assertIn("would run: rm -rf %s" % self.etc, out)
        for kept in (self.etc, self.state, self.opt, self.default):
            self.assertTrue(os.path.exists(kept), kept)


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


class TestNode(Rig):
    def install(self, *args, **env):
        return self.run_script("deploy/fleet/install-node.sh", "--no-bootstrap", "--no-check",
                               "--name", "n1", *args, **env)

    def entry(self, out):
        m = re.search(r'^    FLEET_HOSTS\+=" n1=(\S+?):(\S*):(\S*)"$', out, re.M)
        self.assertTrue(m, out)
        return m.group(2), m.group(3)

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
        self.assertEqual(self.entry(out), ("ebpfm", "ebpfm-login"))
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
        self.assertEqual(self.entry(out), ("ebpfm", "ebpfm-login"))

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
        self.assertEqual(self.entry(out), (self.ebpf, self.node))

    def test_other_lines_stay(self):
        os.makedirs(os.path.join(self.tmp, "homes", "rc-pull", ".ssh"))
        with open(os.path.join(self.tmp, "homes", "rc-pull", ".ssh", "authorized_keys"), "w") as fh:
            fh.write("ssh-ed25519 AAAAother admin\n")
        with open(self.env["STUB_DB"], "w") as fh:
            fh.write("user rc-pull\n")
        self.install("--key", KEY, RRSYNC="")
        self.assertEqual(self.authorized(), "ssh-ed25519 AAAAother admin\nrestrict %s\n" % KEY)

    def test_a_rerun_without_the_key_keeps_it(self):
        """An upgrade runs install-node.sh again without --key: the key stays, and the
        node's line is printed as before, not a request for a key it has."""
        rr = os.path.join(self.bin, "rrsync")
        self.install("--key", KEY, "--from", "10.0.0.5", RRSYNC=rr)
        before = self.authorized()
        out = self.install(RRSYNC=rr)
        self.assertEqual(self.authorized(), before)
        self.assertIn("stays as it is", out)
        self.assertEqual(self.entry(out), ("ebpfm", "ebpfm-login"))

    def test_a_pasted_key_with_a_carriage_return(self):
        self.install("--key", KEY + "\r\n", RRSYNC="")
        self.assertEqual(self.authorized(), "restrict %s\n" % KEY)

    def test_a_key_that_is_not_one(self):
        out = self.install("--key", "ssh-ed25519 AAAA\nssh-rsa BBBB", code=1)
        self.assertIn("not one OpenSSH public key line", out)
        out = self.install("--key", KEY, "--from", '1.2.3.4" evil', code=1)
        self.assertIn("not an address list", out)


if __name__ == "__main__":
    unittest.main()
