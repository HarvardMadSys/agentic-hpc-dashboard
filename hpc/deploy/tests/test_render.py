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
# The nodes' guard is in the kit's sibling folder (hpc/fleet beside hpc/deploy), as deploy.sh
# has it; the copy itself is the fleet kit's pull.py.
GUARD_REL = os.path.relpath(os.path.join(os.path.dirname(KIT), "fleet"), REPO)
RENDER = os.path.join(KIT, "render.py")
EXAMPLE = os.path.join(KIT, "site.env.example")
OVERRIDES = ["SOURCE_USER=alice", "SOURCE_NODES=node01 node02 node03",
             "DASH_ROOT=/data/alice/rc-dashboard", "SENDER_DIR=/home/alice/rc-fleet-sender"]
ROOT = "/data/alice/rc-dashboard"
SOURCES = ["node01", "node02", "node03"]


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
        self.assertIn('guard=$(dirname "$kit")/fleet', read("deploy.sh"))
        self.assertIn('deploy/fleet/pull.py "$guard" "$kit" > "$work/release.tar"', read("deploy.sh"))

    def test_the_copy_and_the_guard_are_where_the_release_and_the_loop_look(self):
        """deploy.sh archives pull.py and the nodes' guard, the copy's loop runs pull.py from
        the release, and the guard goes to the nodes from it: a move of either breaks."""
        self.assertTrue(os.path.exists(os.path.join(REPO, GUARD_REL, "rsync_guard.py")))
        self.assertTrue(os.path.exists(os.path.join(REPO, "deploy", "fleet", "pull.py")))
        self.assertIn('"release/$guard/rsync_guard.py"', read("deploy.sh"))
        self.assertIn('"$R/current/deploy/fleet/pull.py" --config "$ETC/fleet.env" loop',
                      read("keep-running.sh"))

    def test_caps_live_in_the_loop(self):
        """The copy and the dashboard run in scopes keep-running.sh starts: their caps,
        start checks, and restart rules are there."""
        keep = read("keep-running.sh")
        stream = re.search(r"^  copy\)\n(.*?);;", keep, re.M | re.S).group(1)
        dash = re.search(r"^  dashboard\)\n(.*?);;", keep, re.M | re.S).group(1)
        for part, scope, pause, burst, within in ((stream, "rc-fleet-copy", "10", "5", "600"),
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
        # one glob over the stream's copy, expanded again at every rescan: a node added
        # to SOURCE_NODES needs no restart of the dashboard
        self.assertEqual(c["feeds"]["ebpf"]["roots"], [ROOT + "/data/mirror/*"])
        self.assertIs(c["slurm"]["enabled"], True)
        self.assertEqual(c["ui"]["node_noun"], "login nodes")
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
                    " c.get('panels.day_cache'), 'ask' in c.tree]))\n")
            p = subprocess.run([sys.executable, "-c", code, fh.name], cwd=dash,
                               env=dict(os.environ, PYTHONPATH=dash), stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, universal_newlines=True)
        finally:
            os.unlink(fh.name)
        if p.returncode and "No module named" in p.stderr:
            self.skipTest("the dashboard's dependencies are not installed for %s" % sys.executable)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(json.loads(p.stdout), [[], "Login nodes · live", True, False])

    def test_codex_from_an_older_site_env_is_ignored_and_said(self):
        """Ask LLM is removed: an older site.env's CODEX deploys, with a warning that says
        why, and renders no ask section and no agent's folder on the dashboard's PATH."""
        p = run_render("dashboard.config.json.in", ["CODEX=/opt/agents/bin/codex"])
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("CODEX is ignored: Ask LLM, which ran it, is removed", p.stderr)
        self.assertNotIn("ask", json.loads(p.stdout))
        env = unit(render("rc-tmux.service.in", ["CODEX=/opt/agents/bin/codex"]))["Environment"]
        self.assertEqual(env[0], "RC_DASH_PATH=/usr/local/bin:/usr/bin:/bin")

    def test_site_name_profile_and_anonymize(self):
        c = config(['SITE_NAME=Cluster "A" · live', "RISK_PROFILE=risk-profiles/example.json",
                    "ANONYMIZE=true"])
        self.assertEqual(c["site"]["name"], 'Cluster "A" · live')
        self.assertEqual(c["risk"]["profile"], ROOT + "/current/dashboard/risk-profiles/example.json")
        self.assertTrue(os.path.exists(os.path.join(REPO, "dashboard", "risk-profiles", "example.json")))
        self.assertIs(c["anonymize"]["enabled"], True)
        self.assertEqual(config(["RISK_PROFILE=/etc/rc/rules.json"])["risk"]["profile"], "/etc/rc/rules.json")

    def test_accounts_dropped_from_site(self):
        # Empty, as site.env.example has them: the stream's own account alone.
        self.assertEqual(config()["filters"], {"drop_global": ["alice"], "drop_agent": []})
        c = config(["DROP_USERS=monitor ops,alice monitor", "DROP_AGENT_USERS=builder"])
        self.assertEqual(c["filters"], {"drop_global": ["alice", "monitor", "ops"],
                                        "drop_agent": ["builder"]})

    def test_day_cache_from_site(self):
        # The day cache holds a second day's state beside the built view; a host short
        # of memory can turn it off.
        self.assertIs(config()["panels"]["day_cache"], True)
        self.assertIs(config(["DAY_CACHE=false"])["panels"]["day_cache"], False)

    def test_two_ingest_workers_per_node_unless_site_says(self):
        # A node's hours by reducer group are two processes at a time: two per node is
        # the most that helps.
        self.assertEqual(config()["ingest"], {"workers": 6})
        self.assertEqual(config(["INGEST_WORKERS=1"])["ingest"], {"workers": 1})
        self.assertEqual(config(["INGEST_WORKERS=0"])["ingest"], {"workers": 0})

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
            p = subprocess.run([sys.executable, RENDER, os.path.join(KIT, "fleet.env.in"), fh.name],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
            s = env_of(p.stdout)
        finally:
            os.unlink(fh.name)
        self.assertEqual(c["live"]["retention_min"], 10080)
        self.assertEqual(c["panels"], {"max_windows": 5, "prewarm_min": [60, 360, 720], "day_cache": True})
        self.assertEqual(c["reducers"]["trajectories"]["max_sessions"], 150000)
        self.assertEqual(c["ingest"], {"workers": 2})        # one node
        self.assertEqual(c["site"], {"name": "", "description": ""})
        self.assertNotIn("ask", c)
        self.assertEqual(s["FLEET_FREE_MIN_GB"], "300")
        self.assertEqual(s["FLEET_EBPF_SOURCE"], "/scratch/ebpfm")

    def test_values_are_checked(self):
        for bad in ("DAY_CACHE=maybe", "ANONYMIZE=maybe", "SLURM=maybe", "SHARED_HOME=maybe",
                    "DASH_MEMORY_MAX=lots", "MEMORY_MAX=lots", "HISTORY_DAYS=abc", "DASH_PORT=eighty",
                    "DASH_PORT=99999", "MAX_WINDOWS=five", "PREWARM_MIN=1h", "FREE_MIN_GB=lots",
                    "DASH_ROOT=", "DASH_ROOT=rel/path", "DASH_SSH=", "DASH_SSH=a b@host",
                    "SENDER_DIR=/a b", "SENDER_DIR=rel/dir", "DASH_PYTHON=python 3",
                    "SOURCE_USER=", "SOURCE_NODES=", "SOURCE_NODES=a -oProxyCommand=x",
                    "SOURCE_NODES=a a", "SOURCE_NODES=a b'c", "SOURCE_DOMAIN=a b",
                    "RISK_PROFILE=a b.json", "FREE_MIN_GB=-1", "HISTORY_DAYS=0",
                    "INGEST_WORKERS=-1", "INGEST_WORKERS=four", "INGEST_WORKERS=257",
                    "DROP_USERS=a'b", "DROP_AGENT_USERS=-x", "SOURCE_ROOT=rel/ebpfm",
                    "SOURCE_ROOT=/a b"):
            p = run_render("fleet.env.in", [bad])
            self.assertEqual(p.returncode, 2, bad)

    def test_nothing_under_tmp_or_on_the_nodes(self):
        """The dashboard's host keeps everything in DASH_ROOT: no /tmp, and nothing of the
        deployment on the nodes, where only the guard lives."""
        for path in glob.glob(os.path.join(KIT, "*.in")):
            text = render(os.path.basename(path))
            self.assertNotIn("/tmp", text, path)
            self.assertNotIn("/home/alice", text, path)

    def test_history_sets_presets(self):
        c = config(["HISTORY_DAYS=2"])
        self.assertEqual(c["live"]["window_presets_min"], [60, 360, 720, 1440])
        self.assertEqual(c["live"]["retention_min"], 2880)

    def test_private_tree_from_site(self):
        """PRIVATE_TREE goes into kit.env, where the start checks, the hourly check and
        install-remote.sh read it; anything but yes or no (or true, false, 1, 0...) is
        refused."""
        self.assertEqual(env_of(render("kit.env.in"))["PRIVATE_TREE"], "yes")
        self.assertEqual(env_of(render("kit.env.in", ["PRIVATE_TREE=false"]))["PRIVATE_TREE"], "no")
        with self.assertRaises(AssertionError):
            render("kit.env.in", ["PRIVATE_TREE=maybe"])

    def test_the_copys_settings(self):
        """pull.py's settings: each node's eBPF tier, in the flat layout (data/mirror/<node>,
        where the stream before it kept the copy), through the key the nodes' guard lets in,
        and pull.py takes them as they are."""
        s = env_of(render("fleet.env.in"))
        self.assertEqual(s["FLEET_HOSTS"], " ".join(SOURCES))
        self.assertEqual((s["FLEET_USER"], s["FLEET_DOMAIN"]), ("alice", "cluster.example.org"))
        self.assertEqual(s["FLEET_SSH_KEY"], ROOT + "/etc/rc_fleet_stream")
        self.assertEqual(s["FLEET_SSH_OPTS"], "-o Compression=yes")   # as the stream compressed
        self.assertEqual(s["FLEET_MIRROR"], ROOT + "/data/mirror")
        self.assertEqual((s["FLEET_LAYOUT"], s["FLEET_NODE_SOURCE"]), ("flat", ""))
        self.assertEqual(s["FLEET_DAYS"], str(int(example_value("HISTORY_DAYS")) + 1))
        # the copy keeps what it copies, no more: the stream removed older days too
        self.assertEqual(s["FLEET_KEEP_DAYS"], s["FLEET_DAYS"])
        self.assertEqual(s["FLEET_PRIVATE"], "yes")
        self.assertEqual(env_of(render("fleet.env.in", ["PRIVATE_TREE=false"]))["FLEET_PRIVATE"], "no")
        self.assertEqual(s["FLEET_FREE_MIN_GB"], "300")
        self.assertEqual(env_of(render("fleet.env.in", ["FREE_MIN_GB=50.5"]))["FLEET_FREE_MIN_GB"], "50.5")
        self.assertEqual(env_of(render("fleet.env.in", ["SOURCE_DOMAIN="]))["FLEET_DOMAIN"], "")
        # pull.py reads it as written: the nodes, and the dashboard's roots in its copy
        with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as fh:
            fh.write(render("fleet.env.in"))
        try:
            p = subprocess.run([sys.executable, os.path.join(REPO, "deploy", "fleet", "pull.py"),
                                "--config", fh.name, "roots"], stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, universal_newlines=True)
        finally:
            os.unlink(fh.name)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("RC_DASH_EBPF_ROOTS=%s/data/mirror/*\n" % ROOT, p.stdout)
        self.assertEqual(config()["feeds"]["ebpf"]["roots"], [ROOT + "/data/mirror/*"])

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
        """The windows tmux-session.sh makes are the ones the hourly check looks after,
        install-remote.sh asks for and keep-running.sh knows."""
        session, check, keep = read("tmux-session.sh"), read("check-local.sh"), read("keep-running.sh")
        made = re.findall(r" -n (\w+) ", session)
        self.assertEqual(made, ["copy", "dashboard"])
        self.assertEqual(re.search(r'^WINDOWS="(.*)"$', check, re.M).group(1).split(), made)
        self.assertEqual(re.findall(r"^  (\w+)\)$", keep, re.M), made)
        self.assertIn("for w in copy dashboard; do", read("install-remote.sh"))
        self.assertIn("rc-dashboard.scope", check)

    def test_no_placeholder_left(self):
        names = sorted(os.path.basename(p) for p in glob.glob(os.path.join(KIT, "*.in")))
        self.assertEqual(names, ["dashboard.config.json.in", "fleet.env.in", "kit.env.in",
                                 "rc-tmux.service.in"])
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


def env_of(text):
    """KEY=VALUE lines as {KEY: VALUE}, one pair of quotes off."""
    out = {}
    for line in text.splitlines():
        m = re.match(r"^([A-Z_]+)=(.*)$", line)
        if m:
            v = m.group(2)
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]
            out[m.group(1)] = v
    return out


def render_site(text, name="fleet.env.in", strict=False):
    """render.py over a site.env of its own: (exit, stdout, stderr)."""
    with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as fh:
        fh.write(text)
    try:
        p = subprocess.run([sys.executable, RENDER] + (["--strict"] if strict else [])
                           + [os.path.join(KIT, name), fh.name],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
    finally:
        os.unlink(fh.name)
    return p.returncode, p.stdout, p.stderr


MINIMAL = ("SOURCE_USER=alice\nSOURCE_NODES=\"node01 node02\"\nDASH_SSH=alice@dash\nDASH_NODE=dash\n"
           "DASH_ROOT=/data/rc\nDASH_PORT=8080\n")


class TestSiteEnv(unittest.TestCase):
    """site.env as both its readers take it: bash, and render.py."""

    def test_the_names_every_kit_uses(self):
        code, out, err = render_site(MINIMAL + "DASH_MEMORY_MAX=16G\n", "rc-tmux.service.in")
        self.assertEqual(code, 0, err)
        self.assertIn("RC_DASH_MEMORY_MAX=16G", out)
        code, out, err = render_site(MINIMAL + "MEMORY_MAX=16G\nDASH_MEMORY_MAX=8G\n")
        self.assertEqual(code, 2)
        self.assertIn("settle on one", err)
        for word, want in (("yes", True), ("1", True), ("On", True), ("no", False), ("0", False)):
            c = json.loads(render_site(MINIMAL + "ANONYMIZE=%s\nSLURM=%s\n" % (word, word),
                                       "dashboard.config.json.in")[1])
            self.assertIs(c["anonymize"]["enabled"], want, word)
            self.assertIs(c["slurm"]["enabled"], want, word)
        c = json.loads(render_site(MINIMAL + 'NODE_NOUN="servers"\n', "dashboard.config.json.in")[1])
        self.assertEqual(c["ui"]["node_noun"], "servers")

    def test_a_line_bash_reads_otherwise_is_refused(self):
        """Unquoted blanks: bash ran `login02` as a command and stopped the deploy, while
        render.py had read both nodes."""
        code, out, err = render_site(MINIMAL + "SOURCE_NODES=login01 login02\n")
        self.assertEqual(code, 2)
        self.assertIn('blanks and no quotes', err)
        self.assertIn('SOURCE_NODES="login01 login02"', err)
        code, out, err = render_site(MINIMAL + "SITE_NAME=$(hostname)\n")
        self.assertEqual(code, 2)
        self.assertIn("no $( )", err)
        # single quotes keep a `$` as it is, as bash does
        c = json.loads(render_site(MINIMAL + "SITE_NAME='$HOME live'\n", "dashboard.config.json.in")[1])
        self.assertEqual(c["site"]["name"], "$HOME live")

    def test_an_unknown_key_is_named(self):
        code, out, err = render_site(MINIMAL + "HISTORY_DAY=2\n", "dashboard.config.json.in")
        self.assertEqual(code, 0, err)
        self.assertIn("HISTORY_DAY is not a setting (did you mean HISTORY_DAYS?)", err)
        self.assertEqual(json.loads(out)["live"]["retention_min"], 10080)

    def test_a_bad_number_names_its_key(self):
        for line, key in (("HISTORY_DAYS=abc", "HISTORY_DAYS"), ("DASH_PORT=http", "DASH_PORT"),
                          ("MAX_SESSIONS=lots", "MAX_SESSIONS"), ("PREWARM_MIN=1h", "PREWARM_MIN"),
                          ("FREE_MIN_GB=lots", "FREE_MIN_GB")):
            code, out, err = render_site(MINIMAL + line + "\n", "dashboard.config.json.in")
            self.assertEqual(code, 2, line)
            self.assertIn(key, err, line)
            self.assertNotIn("invalid literal", err, line)

    def test_strict_refuses_the_examples_values(self):
        """deploy.sh renders with --strict: the example's own values name no host."""
        with open(EXAMPLE) as fh:
            example = fh.read()
        code, out, err = render_site(example, strict=True)
        self.assertEqual(code, 2)
        self.assertIn("still site.env.example's value", err)
        code, out, err = render_site(MINIMAL, strict=True)
        self.assertEqual(code, 0, err)
        code, out, err = render_site(MINIMAL.replace("alice@dash", "alice@dash.example.org"), strict=True)
        self.assertEqual(code, 2)


class TestNoSiteInTheKit(unittest.TestCase):
    """The kit is public and general: no deployment's names in what git tracks."""

    def tracked(self):
        files = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard",
                                KIT_REL, STREAM_REL],
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
