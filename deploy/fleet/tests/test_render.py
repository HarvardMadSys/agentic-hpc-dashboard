"""The kit's templates, rendered from site.env.example with a made-up site."""
import glob
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
KIT = os.path.dirname(HERE)
REPO = os.path.dirname(os.path.dirname(KIT))
KIT_REL = os.path.relpath(KIT, REPO)
RENDER = os.path.join(KIT, "render.py")
EXAMPLE = os.path.join(KIT, "site.env.example")
OVERRIDES = ["SOURCE_USER=alice", "SOURCE_NODES=node01 node02 node03",
             "DASH_ROOT=/data/alice/rc-dashboard", "SENDER_DIR=/home/alice/rc-fleet-sender"]
ROOT = "/data/alice/rc-dashboard"
SOURCES = ["node01", "node02", "node03"]
SSH_OPTS = ["-T", "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-o", "Compression=no",
            "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=4",
            "-o", "ConnectTimeout=20", "-o", "StrictHostKeyChecking=accept-new",
            "-o", "LogLevel=ERROR"]


def run_render(name, extra=()):
    return subprocess.run([sys.executable, RENDER, os.path.join(KIT, name), EXAMPLE] + OVERRIDES + list(extra),
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)


def render(name, extra=()):
    p = run_render(name, extra)
    if p.returncode:
        raise AssertionError("render.py %s failed (%d): %s" % (name, p.returncode, p.stderr))
    return p.stdout


def config(extra=()):
    return json.loads(render("dashboard.config.json.in", extra))


def unit(text):
    """A unit file as {key: [values]}, every section together; repeats keep order."""
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", ";", "[")) or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out.setdefault(k.strip(), []).append(v.strip())
    return out


def example_value(key):
    with open(EXAMPLE) as fh:
        for line in fh:
            m = re.match(r"\s*%s=(.*)$" % key, line)
            if m:
                return m.group(1).strip().strip('"')
    raise KeyError(key)


def read(name):
    with open(os.path.join(KIT, name)) as fh:
        return fh.read()


class TestRender(unittest.TestCase):
    def test_tmux_unit(self):
        """The one unit: the tmux session whose two windows run the stream and the
        dashboard. It must run whatever state the two processes are in: no start check
        may keep it down. Its windows learn the deployment's folder from it."""
        t = unit(render("rc-tmux.service.in"))
        for k, v in (("Type", "forking"), ("ConditionHost", "dash*"), ("UMask", "0077"),
                     ("LimitCORE", "0"), ("MemoryMax", "64M"), ("Restart", "always"),
                     ("RestartSec", "10"), ("StartLimitIntervalSec", "600"),
                     ("StartLimitBurst", "5"), ("TimeoutStopSec", "120"),
                     ("WantedBy", "default.target")):
            self.assertEqual(t.get(k), [v], k)
        self.assertNotIn("ExecStartPre", t)
        self.assertNotIn("MemoryHigh", t, "a soft cap without swap stalls a process instead of killing it")
        self.assertEqual(t.get("ExecStart"), ["%s/current/%s/tmux-session.sh" % (ROOT, KIT_REL)])
        env = t.get("Environment", [])
        self.assertEqual(env, ["RC_DASH_PATH=/usr/local/bin:/usr/bin:/bin", "RC_DASH_ROOT=" + ROOT,
                               "RC_DASH_MEMORY_MAX=32G"])

    def test_the_unit_finds_the_kit_where_the_release_holds_it(self):
        """The release is `git archive` of the kit's own folder, so the unit and the
        hourly check name the folder this kit is in, whatever it is called."""
        self.assertTrue(os.path.exists(os.path.join(REPO, KIT_REL, "tmux-session.sh")))
        self.assertEqual(re.search(r"^KIT=(\S+)$", read("check.sh"), re.M).group(1), KIT_REL)
        self.assertIn('kit=${here#"$repo"/}', read("deploy.sh"))
        self.assertIn('fleet "$kit" > "$work/release.tar"', read("deploy.sh"))

    def test_caps_live_in_the_loop(self):
        """The stream and the dashboard run in scopes keep-running.sh starts: their caps,
        start checks, and restart rules are there."""
        keep = read("keep-running.sh")
        stream = re.search(r"^  stream\)\n(.*?);;", keep, re.M | re.S).group(1)
        dash = re.search(r"^  dashboard\)\n(.*?);;", keep, re.M | re.S).group(1)
        for part, scope, pause, burst, within in ((stream, "rc-fleet-stream", "10", "5", "600"),
                                                  (dash, "rc-dashboard", "30", "4", "21600")):
            for k, v in (("scope", scope), ("pause", pause), ("burst", burst), ("within", within)):
                self.assertRegex(part, r"\b%s=%s\b" % (k, re.escape(v)), scope)
        self.assertIn("cap=512M;", stream)
        # The unit sets the dashboard's cap from site.env (DASH_MEMORY_MAX).
        self.assertIn("cap=${RC_DASH_MEMORY_MAX:-7G};", dash)
        self.assertIn("checks=(--dirs-only --base-ok)", stream)
        self.assertIn("checks=(--dirs-only --base-ok --auth-set --port-free)", dash)
        self.assertNotIn("MemoryHigh=", keep)

    def test_config_values(self):
        c = config()
        # Every node comes over the stream; the dashboard's host has no node tier.
        self.assertEqual(c["feeds"]["ebpf"]["roots"], [ROOT + "/data/mirror/" + h for h in SOURCES])
        self.assertEqual(c["feeds"]["ebpf_node"]["roots"], [])
        self.assertEqual(c["server"]["host"], "127.0.0.1")
        self.assertEqual(c["server"]["port"], int(example_value("DASH_PORT")))
        self.assertEqual(c["server"]["static_dir"], ROOT + "/current/dist")
        self.assertEqual(c["risk"]["profile"], "")
        self.assertNotIn("uds", c["server"])
        self.assertEqual(c["cache"]["dir"], ROOT + "/cache")
        # The senders' account is the stream, not a user.
        self.assertEqual(c["filters"]["drop_global"], ["alice"])
        self.assertEqual(c["filters"]["drop_agent"], [])
        self.assertIs(c["anonymize"]["enabled"], False)
        self.assertEqual(c["clock"]["as_of"], "")
        self.assertEqual(c["site"], {"name": "Login nodes · live", "description": "a cluster's login nodes"})
        days = int(example_value("HISTORY_DAYS"))
        self.assertEqual(c["live"]["retention_min"], days * 1440)
        self.assertEqual(c["live"]["backfill_hours"], days * 24)
        self.assertEqual(c["feeds"]["ebpf"]["days"], days + 1)
        self.assertNotIn("password_hash", c["server"].get("auth", {}))

    def test_the_dashboard_reads_the_rendered_config(self):
        """The dashboard's own loader takes the file as it is: every key is one it knows,
        and the values land where the kit means them to."""
        dash = os.path.join(REPO, "dashboard")
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            fh.write(render("dashboard.config.json.in"))
        try:
            code = ("import json, sys\nfrom rc_dashboard import config\n"
                    "c = config.load(config_path=sys.argv[1])\n"
                    "print(json.dumps([c.get('feeds.ebpf_node.roots'), c.get('site.name'),"
                    " c.get('panels.day_cache'), c.get('ask.enabled')]))\n")
            p = subprocess.run([sys.executable, "-c", code, fh.name], cwd=dash,
                               env=dict(os.environ, PYTHONPATH=dash), stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, universal_newlines=True)
        finally:
            os.unlink(fh.name)
        if p.returncode and "No module named" in p.stderr:
            self.skipTest("the dashboard's dependencies are not installed for %s" % sys.executable)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(json.loads(p.stdout), [[], "Login nodes · live", True, False])

    def test_ask_follows_codex(self):
        c = config()
        self.assertIs(c["ask"]["enabled"], False)
        self.assertEqual(c["ask"]["command"][7], "codex")
        c = config(["CODEX=/opt/agents/bin/codex"])
        self.assertIs(c["ask"]["enabled"], True)
        self.assertEqual(c["ask"]["command"][0:7],
                         ["systemd-run", "--user", "--scope", "--quiet", "--collect", "-p", "MemoryMax=1G"])
        self.assertEqual(c["ask"]["command"][7], "/opt/agents/bin/codex")
        env = unit(render("rc-tmux.service.in", ["CODEX=/opt/agents/bin/codex"]))["Environment"]
        self.assertEqual(env[0], "RC_DASH_PATH=/opt/agents/bin:/usr/local/bin:/usr/bin:/bin")

    def test_site_name_profile_and_anonymize(self):
        c = config(['SITE_NAME=Cluster "A" · live', "RISK_PROFILE=risk-profiles/example.json",
                    "ANONYMIZE=true"])
        self.assertEqual(c["site"]["name"], 'Cluster "A" · live')
        self.assertEqual(c["risk"]["profile"], ROOT + "/current/dashboard/risk-profiles/example.json")
        self.assertTrue(os.path.exists(os.path.join(REPO, "dashboard", "risk-profiles", "example.json")))
        self.assertIs(c["anonymize"]["enabled"], True)
        self.assertEqual(config(["RISK_PROFILE=/etc/rc/rules.json"])["risk"]["profile"], "/etc/rc/rules.json")

    def test_day_cache_from_site(self):
        # The day cache holds a second day's state beside the built view; a host short
        # of memory can turn it off.
        self.assertIs(config()["panels"]["day_cache"], True)
        self.assertIs(config(["DAY_CACHE=false"])["panels"]["day_cache"], False)

    def test_seven_days_offer_every_window(self):
        c = config()
        self.assertEqual(c["live"]["retention_min"], 10080)
        self.assertEqual(c["live"]["backfill_hours"], 168)
        self.assertEqual(c["live"]["window_presets_min"], [60, 360, 720, 1440, 4320, 10080])
        self.assertEqual(c["feeds"]["ebpf"]["days"], 8)
        self.assertEqual(c["panels"]["max_windows"], 5)
        self.assertEqual(c["panels"]["prewarm_min"], [60, 360, 720])
        self.assertEqual(c["reducers"]["trajectories"]["max_sessions"], 150000)

    def test_left_out_keys_take_the_dashboards_defaults(self):
        with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as fh:
            fh.write("SOURCE_USER=alice\nSOURCE_NODES=node01\nDASH_SSH=alice@dash\nDASH_NODE=dash\n"
                     "DASH_ROOT=/data/rc\nDASH_PORT=8080\n")
        try:
            p = subprocess.run([sys.executable, RENDER, os.path.join(KIT, "dashboard.config.json.in"), fh.name],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
            self.assertEqual(p.returncode, 0, p.stderr)
            c = json.loads(p.stdout)
            p = subprocess.run([sys.executable, RENDER, os.path.join(KIT, "stream.json.in"), fh.name],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
            s = json.loads(p.stdout)
        finally:
            os.unlink(fh.name)
        self.assertEqual(c["live"]["retention_min"], 10080)
        self.assertEqual(c["panels"], {"max_windows": 5, "prewarm_min": [60, 360, 720], "day_cache": True})
        self.assertEqual(c["reducers"]["trajectories"]["max_sessions"], 150000)
        self.assertEqual(c["site"], {"name": "", "description": ""})
        self.assertIs(c["ask"]["enabled"], False)
        self.assertEqual(s["free_min_gb"], 300)

    def test_values_are_checked(self):
        for bad in ("DAY_CACHE=maybe", "ANONYMIZE=yes", "SHARED_HOME=maybe", "DASH_MEMORY_MAX=lots",
                    "DASH_ROOT=", "DASH_ROOT=rel/path", "DASH_SSH=", "DASH_SSH=a b@host",
                    "SENDER_DIR=/a b", "SENDER_DIR=rel/dir", "CODEX=codex", "DASH_PYTHON=python 3",
                    "SOURCE_USER=", "SOURCE_NODES=", "SOURCE_NODES=a -oProxyCommand=x",
                    "SOURCE_NODES=a a", "SOURCE_NODES=a b'c", "SOURCE_DOMAIN=a b",
                    "RISK_PROFILE=a b.json", "FREE_MIN_GB=-1", "HISTORY_DAYS=0"):
            p = run_render("stream.json.in", [bad])
            self.assertEqual(p.returncode, 2, bad)

    def test_nothing_under_tmp_or_on_the_nodes(self):
        """The dashboard's host keeps everything in DASH_ROOT: no /tmp, and nothing of the
        deployment on the nodes, where only the sender lives."""
        for path in glob.glob(os.path.join(KIT, "*.in")):
            text = render(os.path.basename(path))
            self.assertNotIn("/tmp", text, path)
            self.assertNotIn("/home/alice", text, path)

    def test_history_sets_presets(self):
        c = config(["HISTORY_DAYS=2"])
        self.assertEqual(c["live"]["window_presets_min"], [60, 360, 720, 1440])
        self.assertEqual(c["live"]["retention_min"], 2880)

    def test_private_tree_from_site(self):
        """PRIVATE_TREE goes into stream.json, where the stream, the start checks, the hourly
        check, and install-remote.sh read it; anything but true or false is refused."""
        self.assertIs(json.loads(render("stream.json.in"))["private"], True)
        self.assertIs(json.loads(render("stream.json.in", ["PRIVATE_TREE=false"]))["private"], False)
        with self.assertRaises(AssertionError):
            render("stream.json.in", ["PRIVATE_TREE=maybe"])

    def test_stream_config(self):
        s = json.loads(render("stream.json.in"))
        self.assertEqual(s["days"], int(example_value("HISTORY_DAYS")) + 1)
        self.assertEqual(s["base"], ROOT + "/data")
        self.assertEqual(s["base_parents"], 1)
        self.assertEqual(s["trip_marker"], ROOT + "/TRIPPED")
        # Sealed days stay .gz in the copy, as on each node's disk: about 19 times less.
        self.assertIs(s["keep_gz"], True)
        self.assertEqual(s["free_min_gb"], 300)
        self.assertEqual(sorted(s["hosts"]), SOURCES)
        for host, argv in s["hosts"].items():
            self.assertEqual(argv[0], "ssh")
            joined = " ".join(argv)
            self.assertIn(" ".join(SSH_OPTS), joined)
            self.assertIn("-i %s/etc/rc_fleet_stream" % ROOT, joined)
            self.assertIn("alice@%s.cluster.example.org" % host, argv)
            self.assertEqual(argv[-1], "send")
        self.assertEqual(json.loads(render("stream.json.in", ["FREE_MIN_GB=50.5"]))["free_min_gb"], 50.5)

    def test_no_domain_reaches_each_node_by_its_name(self):
        s = json.loads(render("stream.json.in", ["SOURCE_DOMAIN="]))
        for host, argv in s["hosts"].items():
            self.assertIn("alice@" + host, argv)

    def test_every_unit_is_shipped(self):
        """Each unit template is rendered by deploy.sh, shipped, installed and enabled by
        install-remote.sh, and started by the hourly check when it is down."""
        deploy = read("deploy.sh")
        rendered = re.search(r"^for t in (.*); do$", deploy, re.M).group(1).split()
        shipped = re.search(r'tar -C "\$work" -czf - (.*?) \|', deploy, re.S).group(1).split()
        install = read("install-remote.sh")
        installed = re.search(r'^install -m 600 (.*) "\$HOME/\.config/systemd/user/"$', install, re.M).group(1)
        enabled = re.search(r"^systemctl --user enable (.*?) >", install, re.M).group(1).split()
        checked = re.search(r"^UNIT=(.*)$", read("check-local.sh"), re.M).group(1).split()
        units = sorted(os.path.basename(p)[:-3] for p in glob.glob(os.path.join(KIT, "*.service.in")))
        self.assertEqual(units, ["rc-tmux.service"])
        for u in units:
            self.assertIn(u, rendered, "deploy.sh does not render %s" % u)
            self.assertIn(u, shipped, "deploy.sh does not ship %s" % u)
            self.assertIn('"$inc/%s"' % u, installed, "install-remote.sh does not install %s" % u)
            self.assertIn(u, enabled, "install-remote.sh does not enable %s" % u)
            self.assertIn(u, checked, "check-local.sh does not start %s" % u)
        for name in rendered:
            self.assertIn(name, shipped, "deploy.sh renders %s and does not ship it" % name)
            self.assertIn('"$inc/%s"' % name, install, "install-remote.sh does not install %s" % name)

    def test_names_agree(self):
        """The windows tmux-session.sh makes are the ones the hourly check looks after and
        keep-running.sh knows; the dashboard's scope is the one the stream stops on a trip."""
        session, check, keep = read("tmux-session.sh"), read("check-local.sh"), read("keep-running.sh")
        made = re.findall(r" -n (\w+) ", session)
        self.assertEqual(made, ["stream", "dashboard"])
        self.assertEqual(re.search(r'^WINDOWS="(.*)"$', check, re.M).group(1).split(), made)
        self.assertEqual(re.findall(r"^  (\w+)\)$", keep, re.M), made)
        self.assertEqual(json.loads(render("stream.json.in"))["dashboard_unit"], "rc-dashboard.scope")
        self.assertIn("rc-dashboard.scope", check)
        self.assertIn("rc-fleet-stream.scope", check)

    def test_no_placeholder_left(self):
        names = sorted(os.path.basename(p) for p in glob.glob(os.path.join(KIT, "*.in")))
        self.assertEqual(names, ["dashboard.config.json.in", "rc-tmux.service.in", "stream.json.in"])
        for name in names:
            self.assertIsNone(re.search(r"@[A-Z][A-Z0-9_]*@", render(name)), name)

    def test_unfilled_placeholder_fails(self):
        with tempfile.NamedTemporaryFile("w", suffix=".in", delete=False) as fh:
            fh.write("x=@NOPE@\n")
        try:
            p = subprocess.run([sys.executable, RENDER, fh.name, EXAMPLE] + OVERRIDES,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
            self.assertEqual(p.returncode, 2)
            self.assertIn("NOPE", p.stderr)
        finally:
            os.unlink(fh.name)


class TestNoSiteInTheKit(unittest.TestCase):
    """The kit is public and general: no deployment's names in what git tracks."""

    def tracked(self):
        files = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard",
                                KIT_REL, "fleet"],
                               cwd=REPO, stdout=subprocess.PIPE, universal_newlines=True).stdout.split()
        self.assertTrue(files)
        for f in files:
            with open(os.path.join(REPO, f), errors="replace") as fh:
                yield f, fh.read()

    def test_site_env_is_ignored(self):
        p = subprocess.run(["git", "check-ignore", "-q", os.path.join(KIT_REL, "site.env")], cwd=REPO)
        self.assertEqual(p.returncode, 0, "git must ignore site.env")

    def test_no_real_names(self):
        """Checked against a private list of real names, one per line, when this host has
        one (RC_REAL_NAMES, or ~/.config/rc-dashboard/real-names.txt). A failure names the
        file and the list's line number, never the name."""
        names_file = os.environ.get("RC_REAL_NAMES",
                                    os.path.expanduser("~/.config/rc-dashboard/real-names.txt"))
        if not os.path.exists(names_file):
            self.skipTest("no list of real names on this host")
        with open(names_file) as fh:
            names = [(i, n.strip()) for i, n in enumerate(fh, 1) if n.strip()]
        found = ["%s: line %d of the list" % (f, i) for f, text in self.tracked()
                 for i, n in names if n in text]
        self.assertEqual(found, [])


if __name__ == "__main__":
    unittest.main()
