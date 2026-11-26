"""deploy/rc-deploy, the front door: init's four questions and the kit they call for, and
each command handed to its kit's scripts. RC_DEPLOY_ECHO=1 shows the kit's command
instead of running it; the settings files, install.env and fleet.env are temporary, and
systemctl is a stand-in that logs its calls."""
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
TOOL = os.path.join(REPO, "deploy", "rc-deploy")

SYSTEMCTL = '''#!/bin/bash
echo "systemctl $*" >> "$STUB_LOG"
exit 0
'''


class Rig(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rc-deploy-")
        self.site = os.path.join(self.tmp, "site.env")
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.bin)
        with open(os.path.join(self.bin, "systemctl"), "w") as fh:
            fh.write(SYSTEMCTL)
        os.chmod(os.path.join(self.bin, "systemctl"), 0o755)
        self.log = os.path.join(self.tmp, "calls.log")
        self.etc = os.path.join(self.tmp, "etc")
        self.state = os.path.join(self.tmp, "state")
        os.makedirs(self.etc)
        self.install_env = os.path.join(self.tmp, "install.env")
        with open(self.install_env, "w") as fh:
            fh.write("MODE=fleet\nETC=%s\nSTATE=%s\nOPT=%s/opt\n" % (self.etc, self.state, self.tmp))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def tool(self, *args, code=0, stdin="", echo=True, **env):
        e = dict(os.environ, PATH=self.bin + ":" + os.environ["PATH"], STUB_LOG=self.log,
                 INSTALL_ENV=self.install_env, RC_DEPLOY_AS_ROOT="1",
                 RC_DEPLOY_HISTORY=os.path.join(self.tmp, "history"), USER="alice")
        if echo:
            e["RC_DEPLOY_ECHO"] = "1"
        e.update(env)
        p = subprocess.run([sys.executable, TOOL, "--site", self.site] + list(args), env=e,
                           input=stdin, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           universal_newlines=True, timeout=60)
        self.assertEqual(p.returncode, code, p.stdout)
        return p.stdout

    def keys(self):
        out = {}
        with open(self.site) as fh:
            for line in fh:
                m = re.match(r"^([A-Z_]+)=(.*)$", line.rstrip("\n"))
                if m:
                    out[m.group(1)] = m.group(2)
        return out


class TestWhere(Rig):
    """single and fleet keep their settings with the install (ROOT_SITE), so another
    checkout or kit, another folder, reads them; a deploy/site.env from before moves there."""

    def setUp(self):
        super().setUp()
        self.root_site = os.path.join(self.tmp, "etc-rc", "site.env")
        self.old_site = os.path.join(self.tmp, "kit", "deploy", "site.env")
        os.makedirs(os.path.dirname(self.old_site))
        self.where = dict(RC_DEPLOY_ROOT_SITE=self.root_site, RC_DEPLOY_OLD_SITE=self.old_site,
                          RC_DEPLOY_HPC_SITE=os.path.join(self.tmp, "hpc-site.env"),
                          RC_DEPLOY_HISTORY="")

    def bare(self, *args, code=0, root=True):
        """rc-deploy with no --site: it finds the settings file itself."""
        e = dict(os.environ, PATH=self.bin + ":" + os.environ["PATH"], STUB_LOG=self.log,
                 INSTALL_ENV=self.install_env, RC_DEPLOY_ECHO="1", USER="alice", **self.where)
        e["RC_DEPLOY_AS_ROOT"] = "1" if root else "0"
        p = subprocess.run([sys.executable, TOOL] + list(args), env=e, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, universal_newlines=True, timeout=60)
        self.assertEqual(p.returncode, code, p.stdout)
        return p.stdout

    def test_init_writes_them_with_the_install(self):
        out = self.bare("init", "--yes", "--set", "NODES=", "--set", "ROOT_ON_DASHBOARD=yes")
        self.assertIn("settings in %s" % self.root_site, out)
        with open(self.root_site) as fh:
            self.assertIn("KIT=single", fh.read())
        self.assertIn("kit single, settings %s" % self.root_site, self.bare("which"))

    def test_init_without_root_says_the_command_with_the_answers(self):
        """Every answer decided so far, typed or taken by --yes, and --kit: the command run
        again as root asks nothing it was told and comes to the same kit (it once said
        `init` alone, and the defaults chose another)."""
        out = self.bare("init", "--yes", "--set", "NODES=", "--set", "ROOT_ON_DASHBOARD=yes",
                        "--set", "SITE_NAME=c 1", code=1, root=False)
        self.assertIn("which only root writes", out)
        self.assertIn("init --set NODES= --set ROOT_ON_DASHBOARD=yes --set 'SITE_NAME=c 1' "
                      "--set SLURM=yes --yes`", out)
        self.assertFalse(os.path.exists(self.root_site))
        out = self.bare("init", "--kit", "fleet", "--yes", "--set", "NODES=n1 n2", code=1, root=False)
        said = re.search(r"run `sudo \S+ (init [^`]*)`", out).group(1)
        self.assertEqual(said, "init --kit fleet --set 'NODES=n1 n2' --set ROOT_ON_DASHBOARD=no "
                               "--set ROOT_ON_NODES=no --set SLURM=yes --yes")
        out = self.bare(*shlex.split(said), code=0)       # as root, from what it said
        self.assertIn("the kit: fleet", out)

    def test_a_settings_file_from_before_moves_once_root_runs(self):
        with open(self.old_site, "w") as fh:
            fh.write("KIT=single\nSLURM=no\n")
        out = self.bare("which", root=False)
        self.assertIn("the next command run as root moves them", out)
        self.assertIn("settings %s" % self.old_site, out)
        out = self.bare("which")
        self.assertIn("settings moved from %s to %s" % (self.old_site, self.root_site), out)
        # a link stays at the old place: a checkout rolled back past the move reads only
        # there, and finds the same settings; this one reads them once, not twice
        self.assertEqual(os.readlink(self.old_site), self.root_site)
        with open(self.root_site) as fh:
            self.assertEqual(fh.read(), "KIT=single\nSLURM=no\n")
        out = self.bare("which")
        self.assertNotIn("moved", out)
        self.assertNotIn("two settings files", out)
        self.assertIn("settings %s" % self.root_site, out)

    def test_the_history_is_beside_them_and_the_kits_own_still_read(self):
        os.makedirs(os.path.dirname(self.root_site))
        with open(self.root_site, "w") as fh:
            fh.write("KIT=single\n")
        self.bare("upgrade", "--to", "v1")
        hist = os.path.join(os.path.dirname(self.root_site), "rc-deploy-history")
        with open(hist) as fh:
            self.assertEqual(len(fh.read().splitlines()), 1)
        os.unlink(hist)
        # from before: the kit's own .rc-deploy-history, which rollback still reads
        old = os.path.join(REPO, "deploy", ".rc-deploy-history")
        if os.path.exists(old):
            self.skipTest("this checkout has a history of its own")
        try:
            with open(old, "w") as fh:
                fh.write("abc123 def456 2026-11-01 10:00:00\n")
            self.assertIn("checkout --detach abc123", self.bare("rollback"))
        finally:
            os.unlink(old)


class TestInit(Rig):
    def test_one_server_with_root(self):
        out = self.tool("init", "--yes", "--set", "NODES=", "--set", "ROOT_ON_DASHBOARD=yes")
        k = self.keys()
        self.assertEqual(k["KIT"], "single")
        self.assertEqual((k["SLURM"], k["NODE_NOUN"], k["SITE_NAME"]),
                         ("yes", '"login nodes"', '"login nodes · live"'))
        self.assertIn("the kit: single", out)

    def test_slurm_is_asked_not_taken_from_root(self):
        self.tool("init", "--yes", "--set", "SLURM=no", "--set", "NODES=", "--set", "ROOT_ON_DASHBOARD=yes")
        k = self.keys()
        self.assertEqual((k["KIT"], k["SLURM"], k["NODE_NOUN"]), ("single", "no", "servers"))

    def test_root_everywhere_is_a_fleet(self):
        self.tool("init", "--yes", "--set", "NODES=n1, n2", "--set", "ROOT_ON_NODES=yes",
                  "--set", "ROOT_ON_DASHBOARD=yes", "--set", "DASH_ADDRESS=10.0.0.5")
        k = self.keys()
        self.assertEqual((k["KIT"], k["NODES"], k["DASH_ADDRESS"]), ("fleet", '"n1 n2"', "10.0.0.5"))

    def test_no_root_on_the_nodes_streams(self):
        out = self.tool("init", "--set", "NODES=login01 login02", "--set", "ROOT_ON_NODES=no",
                        "--set", "ROOT_ON_DASHBOARD=no", "--set", "DASH_SSH=alice@dash.cluster.org",
                        "--set", "DASH_ROOT=/data/alice/rc", "--set", "SOURCE_DOMAIN=cluster.org",
                        "--yes")
        k = self.keys()
        self.assertEqual(k["KIT"], "hpc")
        self.assertEqual((k["SOURCE_USER"], k["SOURCE_NODES"], k["DASH_NODE"], k["DASH_ROOT"]),
                         ("alice", '"login01 login02"', "dash", "/data/alice/rc"))
        self.assertIn("no root on the nodes", out)
        with open(self.site) as fh:
            self.assertIn("# ---- The source nodes", fh.read(), "the example's notes stay")
        # what it wrote renders, and passes deploy.sh's --strict check
        p = subprocess.run([sys.executable, os.path.join(REPO, "hpc", "deploy", "render.py"),
                            "--strict", os.path.join(REPO, "hpc", "deploy", "stream.json.in"), self.site],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_a_site_name_reads_back_as_given_by_every_reader(self):
        """init wrote SITE_NAME=O'Reilly, an unmatched quote: the next apply, which sources
        site.env under set -e, failed, and a name with " or $ in it came back changed.
        Each value is written so bash, render.py and rc-deploy itself read it back as it
        was given; one no quoting keeps for all three is refused, naming it."""
        given = {"SITE_NAME": "O'Reilly's cluster",
                 "SITE_DESCRIPTION": 'the "big" $HOME one, `x` \\ $(y)'}
        sets = []
        for k, v in sorted(given.items()):
            sets += ["--set", "%s=%s" % (k, v)]
        self.tool("init", "--set", "NODES=login01", "--set", "ROOT_ON_NODES=no",
                  "--set", "ROOT_ON_DASHBOARD=no", "--set", "DASH_SSH=alice@dash",
                  "--set", "DASH_ROOT=/data/rc", "--yes", *sets)
        p = subprocess.run(["bash", "-c", 'set -e; . "$1"; printf "%s\\n%s" "$SITE_NAME" '
                            '"$SITE_DESCRIPTION"', "-", self.site],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout, "%s\n%s" % (given["SITE_NAME"], given["SITE_DESCRIPTION"]))
        code = ("import importlib.machinery, json, sys\n"
                "sys.path.insert(0, sys.argv[2])\n"
                "import render\n"
                "rc = importlib.machinery.SourceFileLoader('rc', sys.argv[1]).load_module()\n"
                "a, b = render.parse_env(sys.argv[3], {}), rc.read_env(sys.argv[3])\n"
                "print(json.dumps([{k: d[k] for k in ('SITE_NAME', 'SITE_DESCRIPTION')}"
                " for d in (a, b)]))\n")
        p = subprocess.run([sys.executable, "-c", code, TOOL,
                            os.path.join(REPO, "hpc", "deploy"), self.site],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(json.loads(p.stdout), [given, given])
        os.unlink(self.site)
        out = self.tool("init", "--yes", "--set", "NODES=", "--set", "ROOT_ON_DASHBOARD=yes",
                        "--set", "SITE_NAME=it's $5", code=1)
        self.assertIn("SITE_NAME", out)

    def test_questions_on_stdin(self):
        out = self.tool("init", stdin="maybe\nyes\nn1 n2\nyes\nyes\n\n")
        self.assertIn("is not one of", out, "a word that is not yes or no is asked again")
        self.assertEqual(self.keys()["KIT"], "fleet")

    def test_which(self):
        self.tool("init", "--yes", "--set", "NODES=", "--set", "ROOT_ON_DASHBOARD=yes")
        self.assertEqual(self.tool("which").strip(), "kit single, settings %s" % self.site)


class TestDispatch(Rig):
    def init(self, kit):
        sets = {"single": ["NODES=", "ROOT_ON_DASHBOARD=yes"],
                "fleet": ["NODES=n1", "ROOT_ON_NODES=yes", "ROOT_ON_DASHBOARD=yes"],
                "hpc": ["NODES=n1", "ROOT_ON_NODES=no", "ROOT_ON_DASHBOARD=no",
                        "DASH_SSH=alice@dash", "DASH_ROOT=/data/rc"]}[kit]
        args = ["init", "--yes"]
        for s in sets:
            args += ["--set", s]
        self.tool(*args)

    def test_apply_hands_the_settings_to_the_kit(self):
        self.init("single")
        out = self.tool("apply")
        self.assertRegex(out, r"RUN .*NODE_NOUN='login nodes' .*SLURM=yes .*deploy/single/install\.sh\n")
        out = self.tool("plan")
        self.assertRegex(out, r"RUN .*deploy/single/install\.sh --plan\n")

    def test_each_command_to_its_kit(self):
        self.init("fleet")
        for args, want in ((["apply"], r"deploy/fleet/install-dashboard\.sh\n"),
                           (["password", "--rotate"], r"deploy/common/password\.sh --rotate\n"),
                           (["status"], r"deploy/common/check\.sh --print\n"),
                           (["backup", "--to", "/b.tar"], r"deploy/common/backup\.sh --to /b\.tar --settings\n"),
                           (["uninstall", "--purge", "--plan"], r"deploy/common/uninstall\.sh --purge --plan\n")):
            self.assertRegex(self.tool(*args), r"RUN .*" + want, args)
        os.unlink(self.site)
        self.init("hpc")
        for args, want in ((["apply"], r"SITE_ENV=\S+ \S+/hpc/deploy/deploy\.sh\n"),
                           (["password"], r"hpc/deploy/password\.sh\n"),
                           (["rollback"], r"hpc/deploy/rollback\.sh\n"),
                           (["backup"], r"hpc/deploy/backup\.sh\n"),
                           (["uninstall"], r"hpc/deploy/uninstall\.sh\n")):
            self.assertRegex(self.tool(*args), r"RUN .*" + want, args)

    def test_refusals(self):
        out = self.tool("apply", code=1)
        self.assertIn("rc-deploy init", out)
        self.init("single")
        self.assertIn("no nodes to add", self.tool("add-node", "n2", code=1))
        with open(self.site, "a") as fh:
            fh.write("KIT=cluster\n")
        self.assertIn("one of single, fleet, hpc", self.tool("apply", code=1))


class TestNodes(Rig):
    def setUp(self):
        super().setUp()
        self.tool("init", "--yes", "--set", "NODES=n1", "--set", "ROOT_ON_NODES=yes",
                  "--set", "ROOT_ON_DASHBOARD=yes")
        self.fleet = os.path.join(self.etc, "fleet.env")
        with open(self.fleet, "w") as fh:
            fh.write("# the nodes\nFLEET_HOSTS=n1\nFLEET_MIRROR=%s/mirror\n" % self.state)

    def read(self):
        with open(self.fleet) as fh:
            return fh.read()

    def test_a_node_added_reloads_the_copy_and_nothing_else(self):
        out = self.tool("add-node", "n2=10.0.0.2:/scratch/ebpfm:", echo=False)
        self.assertIn('FLEET_HOSTS+=" n2=10.0.0.2:/scratch/ebpfm:"\n', self.read())
        with open(self.log) as fh:
            calls = fh.read()
        self.assertIn("systemctl reload rc-pull.service", calls)
        self.assertNotIn("restart", calls)
        self.assertIn("no restart", out)

    def test_a_node_named_at_init_is_completed_by_its_installed_line(self):
        """init's NODES puts bare names in FLEET_HOSTS (the default folders), and apply says
        to add-node the line each node's install prints: refused, as a name already there,
        so a node with other folders could not be set up. A bare entry is completed by it;
        an entry with its own target or folders stays as it is."""
        full = "n1=n1.example.org:ebpfm:ebpfm-login"
        out = self.tool("add-node", full, echo=False)
        self.assertIn("completed", out)
        text = self.read()
        self.assertIn('FLEET_HOSTS+=" %s"\n' % full, text)
        self.assertNotRegex(text, r'FLEET_HOSTS="[^"]*\bn1\b', "the bare entry is gone")
        self.assertIn("# the nodes\n", text)
        with open(self.log) as fh:
            self.assertIn("systemctl reload rc-pull.service", fh.read())
        out = self.tool("add-node", "n1=other.example.org:ebpfm:", code=1, echo=False)
        self.assertIn("as %s" % full, out)
        self.assertIn('FLEET_HOSTS+=" %s"\n' % full, self.read())

    def test_a_line_pull_refuses_leaves_the_file_as_it_was(self):
        before = self.read()
        self.tool("add-node", "n2=host:-x", code=1, echo=False)
        self.assertEqual(self.read(), before)
        self.assertIn("already", self.tool("add-node", "n1", code=1, echo=False))

    def test_a_node_taken_out(self):
        with open(self.fleet, "a") as fh:
            fh.write('FLEET_HOSTS+=" n2=10.0.0.2"\nFLEET_HOSTS+=" n3"\n')
        os.makedirs(os.path.join(self.state, "mirror", "ebpf", "n2"))
        out = self.tool("remove-node", "n2", echo=False)
        text = self.read()
        self.assertIn('FLEET_HOSTS="n1 n3"\n', text)
        self.assertNotIn("n2", text)
        self.assertIn("# the nodes\n", text)
        self.assertIn("its copy stays", out)
        self.assertTrue(os.path.exists(os.path.join(self.state, "mirror", "ebpf", "n2")))
        self.tool("remove-node", "n3", "--purge-copy", echo=False)
        self.assertIn("not in FLEET_HOSTS", self.tool("remove-node", "n9", code=1, echo=False))

    def test_a_node_taken_out_leaves_no_pull_state(self):
        """Nothing pulls it any more: a state file left behind aged into a copy failure
        the hourly check reported for good. It goes, whether the copy stays or not."""
        with open(self.fleet, "a") as fh:
            fh.write('FLEET_HOSTS+=" n2"\n')
        states = os.path.join(self.state, "mirror", ".state")
        os.makedirs(states)
        for n in ("n1", "n2"):
            with open(os.path.join(states, n + ".json"), "w") as fh:
                fh.write('{"ok": true}\n')
        self.tool("remove-node", "n2", echo=False)
        self.assertEqual(os.listdir(states), ["n1.json"])

    def test_fleet_env_keeps_its_mode_and_owner(self):
        """The installer makes fleet.env 0640 root:rc-dashboard; rc-pull reads it as the
        dashboard's account. A copy put in its place must be readable to it as before."""
        os.chmod(self.fleet, 0o640)
        group = os.getgid()
        if os.getuid() == 0:
            group = 1 if os.getgid() != 1 else 2
            os.chown(self.fleet, 0, group)
        self.tool("add-node", "n2", echo=False)
        self.tool("remove-node", "n2", echo=False)
        st = os.stat(self.fleet)
        self.assertEqual(st.st_mode & 0o7777, 0o640)
        self.assertEqual(st.st_gid, group)
        self.assertEqual([n for n in os.listdir(self.etc) if n.startswith(".")], [],
                         "no temporary file left beside it")

    def test_the_last_hpc_node_is_refused_before_the_file_changes(self):
        """An HPC deployment streams from at least one node (render.py): an emptied
        SOURCE_NODES stopped deploy.sh with the settings already changed, the receiver
        still copying the node, and every later deploy stopped too."""
        os.unlink(self.site)
        self.tool("init", "--yes", "--set", "NODES=login01", "--set", "ROOT_ON_NODES=no",
                  "--set", "ROOT_ON_DASHBOARD=no", "--set", "DASH_SSH=alice@dash",
                  "--set", "DASH_ROOT=/data/rc")
        with open(self.site) as fh:
            before = fh.read()
        out = self.tool("remove-node", "login01", "--purge-copy", code=1)
        self.assertIn("login01 is the only node in SOURCE_NODES", out)
        self.assertIn("add-node", out)
        self.assertNotIn("RUN ", out, "nothing deployed, nothing removed")
        with open(self.site) as fh:
            self.assertEqual(fh.read(), before)

    def test_a_moved_mirror_is_where_the_node_is_cleared(self):
        """pull.py keeps the copy and its .state under fleet.env's FLEET_MIRROR: a mirror
        moved off $STATE/mirror was missed, its copy and pull state left behind."""
        moved = os.path.join(self.tmp, "big", "mirror")
        with open(self.fleet, "w") as fh:
            fh.write('FLEET_HOSTS="n1 n2"\nFLEET_MIRROR=%s\n' % moved)
        for d in (os.path.join(moved, "ebpf", "n2"), os.path.join(moved, ".state"),
                  os.path.join(self.state, "mirror", "ebpf", "n2")):
            os.makedirs(d)
        with open(os.path.join(moved, ".state", "n2.json"), "w") as fh:
            fh.write('{"ok": true}\n')
        out = self.tool("remove-node", "n2", "--purge-copy", echo=False)
        self.assertFalse(os.path.exists(os.path.join(moved, "ebpf", "n2")), out)
        self.assertFalse(os.path.exists(os.path.join(moved, ".state", "n2.json")), out)
        self.assertTrue(os.path.exists(os.path.join(self.state, "mirror", "ebpf", "n2")),
                        "nothing outside the mirror pull.py uses")

    def test_hpc_settings_read_as_deploy_sh_reads_them(self):
        """site.env is sourced by bash (deploy.sh): SSH_KEY=$HOME/.ssh/... is the key in
        that home, not a file named $HOME. rc-deploy reads it through the kit's own
        parser (render.py), as deploy.sh's render does."""
        os.unlink(self.site)
        self.tool("init", "--yes", "--set", "NODES=login01", "--set", "ROOT_ON_NODES=no",
                  "--set", "ROOT_ON_DASHBOARD=no", "--set", "DASH_SSH=alice@dash",
                  "--set", "DASH_ROOT=/data/rc")
        with open(self.site, "a") as fh:
            fh.write("SSH_KEY=$HOME/.ssh/id_test\n")
        out = self.tool("status", HOME="/home/tester")
        self.assertIn("RUN ssh -i /home/tester/.ssh/id_test ", out)
        with open(self.site, "a") as fh:
            fh.write("SOURCE_USER=$(whoami)\n")
        self.assertIn("no $( ) or backquotes", self.tool("status", code=1))

    def test_an_hpc_purge_reaches_the_host_as_deploy_sh_does(self):
        """remove-node --purge-copy ssh'd to the dashboard's host without the kit's key:
        with no agent holding it, the purge failed after SOURCE_NODES had changed, and a
        retry was refused, the node being no longer listed."""
        os.unlink(self.site)
        self.tool("init", "--yes", "--set", "NODES=login01", "--set", "ROOT_ON_NODES=no",
                  "--set", "ROOT_ON_DASHBOARD=no", "--set", "DASH_SSH=alice@dash",
                  "--set", "DASH_ROOT=/data/rc")
        self.tool("add-node", "login02")
        with open(self.site, "a") as fh:
            fh.write("SSH_KEY=$HOME/.ssh/id_test\n")
        out = self.tool("remove-node", "login01", "--purge-copy", HOME="/home/tester")
        # its copy, its half-done fetches and its verified ledger, which, left behind,
        # refused the node's files when it was added again
        self.assertIn("RUN ssh -i /home/tester/.ssh/id_test -o IdentitiesOnly=yes -o BatchMode=yes "
                      "-o ConnectTimeout=20 -o LogLevel=ERROR alice@dash "
                      "'rm -rf /data/rc/data/mirror/login01 /data/rc/data/staging/login01 "
                      "/data/rc/data/state/login01.verified.json'", out)

    def test_a_relative_site_file_reaches_the_kit_whole(self):
        """The kit's scripts change directory: rc-deploy hands them the settings file's
        full path, whatever --site was given as."""
        os.unlink(self.site)
        self.tool("init", "--yes", "--set", "NODES=login01", "--set", "ROOT_ON_NODES=no",
                  "--set", "ROOT_ON_DASHBOARD=no", "--set", "DASH_SSH=alice@dash",
                  "--set", "DASH_ROOT=/data/rc")
        e = dict(os.environ, PATH=self.bin + ":" + os.environ["PATH"], STUB_LOG=self.log,
                 INSTALL_ENV=self.install_env, RC_DEPLOY_AS_ROOT="1", RC_DEPLOY_ECHO="1",
                 RC_DEPLOY_HISTORY=os.path.join(self.tmp, "history"), USER="alice")
        p = subprocess.run([sys.executable, TOOL, "--site", os.path.basename(self.site),
                            "add-node", "login02"], cwd=os.path.dirname(self.site), env=e,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           universal_newlines=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stdout)
        self.assertIn("RUN SITE_ENV=%s " % self.site, p.stdout)

    def test_hpc_nodes_go_into_site_env(self):
        os.unlink(self.site)
        self.tool("init", "--yes", "--set", "NODES=login01", "--set", "ROOT_ON_NODES=no",
                  "--set", "ROOT_ON_DASHBOARD=no", "--set", "DASH_SSH=alice@dash",
                  "--set", "DASH_ROOT=/data/rc")
        out = self.tool("add-node", "login02")
        self.assertEqual(self.keys()["SOURCE_NODES"], '"login01 login02"')
        self.assertRegex(out, r"RUN SITE_ENV=\S+ \S+/hpc/deploy/deploy\.sh\n")
        self.tool("remove-node", "login01")
        self.assertEqual(self.keys()["SOURCE_NODES"], "login02")


class TestRelease(Rig):
    """A release's kit (deploy/VERSION) installs that release: init records it, apply hands
    it to the installers (install-dashboard.sh --release), upgrade needs no git, and
    rollback takes the dashboard back to an earlier release's wheel. A checkout builds its
    own code, as before."""

    def setUp(self):
        super().setUp()
        self.version = os.path.join(self.tmp, "VERSION")

    def release(self, v):
        with open(self.version, "w") as fh:
            fh.write(v + "\n")

    def tool(self, *args, **env):
        return super().tool(*args, RC_DEPLOY_VERSION_FILE=self.version, **env)

    def init_fleet(self):
        self.tool("init", "--yes", "--set", "NODES=n1", "--set", "ROOT_ON_NODES=yes",
                  "--set", "ROOT_ON_DASHBOARD=yes")

    def test_a_release_kit_installs_its_release(self):
        self.release("v1.2.3")
        self.init_fleet()
        self.assertEqual(self.keys()["RELEASE"], "v1.2.3")
        out = self.tool("apply")
        self.assertRegex(out, r"RUN .*RELEASE=v1\.2\.3 .*deploy/fleet/install-dashboard\.sh\n")

    def test_a_checkout_builds_its_own_code(self):
        self.init_fleet()
        self.assertNotIn("RELEASE", self.keys())
        self.assertNotIn("RELEASE=", self.tool("apply"))

    def test_upgrade_from_a_release_kit_runs_no_git(self):
        self.release("v1.2.3")
        self.init_fleet()
        self.release("v1.3.0")              # the newer release's kit, unpacked
        out = self.tool("upgrade")
        self.assertNotIn("RUN git", out)
        self.assertIn("release v1.2.3 -> v1.3.0", out)
        self.assertEqual(self.keys()["RELEASE"], "v1.3.0")
        self.assertRegex(out, r"RUN .*RELEASE=v1\.3\.0 .*install-dashboard\.sh\n")
        out = self.tool("upgrade", "--to", "v1.4.0", code=1)
        self.assertIn("unpack the kit of v1.4.0", out)

    def test_rollback_from_a_release_kit_takes_an_earlier_wheel(self):
        self.release("v1.2.3")
        self.init_fleet()
        self.release("v1.3.0")
        self.tool("upgrade")
        out = self.tool("rollback")         # the release before the last upgrade
        self.assertNotIn("RUN git", out)
        self.assertEqual(self.keys()["RELEASE"], "v1.2.3")
        self.assertRegex(out, r"RUN .*RELEASE=v1\.2\.3 .*install-dashboard\.sh\n")
        self.assertIn("vX.Y.Z", self.tool("rollback", "abc1234", code=1))

    def test_a_failed_or_repeated_upgrade_keeps_the_release_to_go_back_to(self):
        """The release is written to the settings and the history only once installed: a
        failed upgrade leaves both naming the release still running, and installing the
        same release again records no transition, so rollback still finds the one before."""
        hist = os.path.join(self.tmp, "history")
        self.release("v1.2.3")
        self.init_fleet()
        self.release("v1.3.0")
        out = self.tool("upgrade", code=1, RC_DEPLOY_ECHO_FAIL="install-dashboard.sh")
        self.assertIn("install-dashboard.sh ended with exit 1", out)
        self.assertEqual(self.keys()["RELEASE"], "v1.2.3")
        self.assertFalse(os.path.exists(hist))
        self.tool("upgrade")                                  # the retry, which works
        self.assertIn("already release v1.3.0", self.tool("upgrade"))
        with open(hist) as fh:
            self.assertEqual([l.split()[:2] for l in fh], [["v1.2.3", "v1.3.0"]])
        out = self.tool("rollback")
        self.assertEqual(self.keys()["RELEASE"], "v1.2.3")
        self.assertRegex(out, r"RUN .*RELEASE=v1\.2\.3 .*install-dashboard\.sh\n")

    def test_a_failed_rollback_leaves_the_settings(self):
        self.release("v1.2.3")
        self.init_fleet()
        self.release("v1.3.0")
        self.tool("upgrade")
        self.tool("rollback", code=1, RC_DEPLOY_ECHO_FAIL="install-dashboard.sh")
        self.assertEqual(self.keys()["RELEASE"], "v1.3.0")

    def test_a_release_that_is_no_release_is_refused(self):
        out = self.tool("init", "--yes", "--set", "NODES=", "--set", "ROOT_ON_DASHBOARD=yes",
                        "--set", "RELEASE=latest", code=1)
        self.assertIn("is not a release", out)


class TestHelp(unittest.TestCase):
    def test_help(self):
        p = subprocess.run([sys.executable, TOOL, "--help"], stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, universal_newlines=True)
        self.assertEqual(p.returncode, 0)
        for word in ("init", "add-node", "rollback", "uninstall", "Slurm", "hpc/deploy/site.env"):
            self.assertIn(word, p.stdout)


if __name__ == "__main__":
    unittest.main()
