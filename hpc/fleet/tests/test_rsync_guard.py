"""rsync_guard.py, the copy key's forced command on the nodes: its checks one by one, the
script run as sshd runs it, and real rsync clients -- deploy/fleet/pull.py among them --
through an ssh stand-in that does sshd's part: it runs the key's forced command in place
of what the client asked for, which it hands over in SSH_ORIGINAL_COMMAND."""
import gzip
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
FLEET = os.path.dirname(HERE)
REPO = os.path.dirname(os.path.dirname(FLEET))
GUARD = os.path.join(FLEET, "rsync_guard.py")
PULL = os.path.join(REPO, "deploy", "fleet", "pull.py")
sys.path.insert(0, FLEET)
import rsync_guard                                                   # noqa: E402

RSYNC = shutil.which("rsync")

# What rsync's client sends the node for pull.py's three calls (rsync 3.2), and for the
# same calls from an EL8 client (3.1), whose capabilities read e.iLsfxC.
LIST = "rsync --server --sender -re.LsfxCIvu --list-only --timeout=600 . {root}/"
APPEND = "rsync --server --sender -tRe.LsfxCIvu --timeout=600 --append --files-from=- --from0 . {root}/"
FETCH = "rsync --server --sender -tRe.LsfxCIvu --timeout=600 --files-from=- --from0 . {root}/"
OLD_LIST = "rsync --server --sender -re.iLsfxC --list-only --timeout=600 . {root}/"
OLD_APPEND = "rsync --server --sender -tRe.iLsfxC --timeout=600 --append --files-from=- --from0 . {root}/"

# ssh as rsync runs it: options, then [user@]host, then the command, joined by blanks as
# sshd gets it; sshd's side runs the forced command with that command in its environment.
SSH = r'''#!/bin/bash
while [ $# -gt 0 ]; do
  case $1 in
    -i|-o|-p|-l|-F|-S|-E|-c|-m|-b|-J|-O) shift 2 ;;
    -*) shift ;;
    *) shift; break ;;
  esac
done
printf '%s\n' "$*" >> "$GUARD_CALLS"
SSH_ORIGINAL_COMMAND="$*" exec bash -c "$GUARD_FORCED"
'''


class Tree(unittest.TestCase):
    """A node's folders: the collector's (the root), and things outside it."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rsync-guard-")
        self.root = os.path.join(self.tmp, "node", "ebpfm")
        self.outside = os.path.join(self.tmp, "node", "private")
        for d in (os.path.join(self.root, "2026-11-24"), os.path.join(self.root, "2026-11-25"),
                  self.outside):
            os.makedirs(d)
        with open(os.path.join(self.outside, "secret.jsonl"), "w") as fh:
            fh.write('{"secret": 1}\n')

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def check(self, command, roots=None):
        roots = [os.path.realpath(r) for r in (roots or [self.root])]
        return rsync_guard.check(command.format(root=self.root, outside=self.outside), roots)

    def refused(self, command, why, roots=None):
        with self.assertRaises(rsync_guard.Refused) as cm:
            self.check(command, roots)
        self.assertIn(why, str(cm.exception), command)
        return str(cm.exception)


class TestAllowed(Tree):
    def test_what_pull_py_sends(self):
        """Its listing, its append and its fetch, from a client of today or of EL8, run
        with the very arguments they came with."""
        for cmd in (LIST, APPEND, FETCH, OLD_LIST, OLD_APPEND):
            want = cmd.format(root=self.root).split()[1:]
            self.assertEqual(self.check(cmd), want, cmd)

    def test_read_only_choices_a_client_may_add(self):
        """rsync -av, -z, -n and the like change what the sender reads or how it says so:
        they read, and write nothing on the node."""
        for cmd in ("rsync --server --sender -vlogDtpre.iLsfxCIvu . {root}/",
                    "rsync --server --sender -vnzre.iLsfxCIvu --compress-level=6 "
                    "--bwlimit=5000 --info=progress2 --safe-links . {root}/2026-11-25/",
                    "rsync --server --sender -e.LsfxCIvu . {root}"):
            self.check(cmd)

    def test_paths_under_the_root(self):
        """The root itself, with or without its slash, a folder or a file in it, several
        at once, and one not there yet (rsync says so; pull.py expects it of a collector
        that has written nothing)."""
        self.check("rsync --server --sender -tRe.LsfxCIvu . {root} {root}/2026-11-24/ "
                   "{root}/2026-11-25/x.jsonl {root}/2026-12-01/")
        shutil.rmtree(self.root)
        self.check(LIST)

    def test_links_that_stay_inside(self):
        """A root reached through a link, as /scratch may be, and a link inside it to
        another folder inside it."""
        alias = os.path.join(self.tmp, "scratch-alias")
        os.symlink(os.path.dirname(self.root), alias)
        os.symlink(os.path.join(self.root, "2026-11-24"), os.path.join(self.root, "yesterday"))
        roots = [os.path.join(alias, "ebpfm")]
        self.assertTrue(rsync_guard.check(
            "rsync --server --sender -re.LsfxCIvu . %s/ %s/yesterday/" % (roots[0], self.root),
            [os.path.realpath(roots[0])]))

    def test_several_roots(self):
        login = os.path.join(self.tmp, "node", "ebpfm-login")
        os.makedirs(login)
        self.check("rsync --server --sender -re.LsfxCIvu . {root}/ %s/" % login,
                   roots=[self.root, login])
        self.refused("rsync --server --sender -re.LsfxCIvu . %s/" % login, "outside")


class TestRefused(Tree):
    def test_anything_but_rsync_sending(self):
        for cmd, why in (("", "no shell"), ("   ", "no shell"),
                         ("ls -la {root}", "not ls"), ("bash -i", "not bash"),
                         ("scp -f {root}/x", "not scp"), ("/usr/bin/rsync --server --sender . {root}/", "not /usr/bin/rsync"),
                         ("rsync --version", "only `rsync --server --sender`"),
                         ("rsync --daemon --server .", "only `rsync --server --sender`"),
                         # rsync receiving: a push, which writes here
                         ("rsync --server -logDtpre.iLsfxCIvu . {root}/", "only `rsync --server --sender`"),
                         ("rsync --sender --server -re.LsfxCIvu . {root}/", "only `rsync --server --sender`")):
            self.refused(cmd, why)

    def test_deleting_or_removing(self):
        for opt in ("--remove-source-files", "--remove-sent-files", "--delete", "--delete-after",
                    "--delete-missing-args", "--del", "--max-delete=1", "--force"):
            self.refused("rsync --server --sender -tRe.LsfxCIvu %s --files-from=- --from0 . "
                         "{root}/" % opt, "deletes")

    def test_writing_or_running_on_the_node(self):
        for opt, why in (("--log-file=/tmp/x", "writes"), ("--write-batch=x", "writes"),
                         ("--only-write-batch=x", "writes"), ("--temp-dir=/tmp", "writes"),
                         ("--partial-dir=.p", "writes"), ("--backup-dir=/tmp", "writes"),
                         ("--mkpath", "writes"), ("--inplace", "writes"),
                         ("--chmod=a+rwx", "writes"), ("--fake-super", "writes"),
                         ("--rsh=sh", "runs something other"),
                         ("--rsync-path=/bin/sh", "runs something other"),
                         ("--server", "runs something other"), ("--sender", "runs something other"),
                         ("--daemon", "runs something other"),
                         ("--copy-devices", "reads devices")):
            self.refused("rsync --server --sender -re.LsfxCIvu %s . {root}/" % opt, why)

    def test_following_links_out(self):
        for opt in ("-tLe.LsfxCIvu", "-tRke.LsfxCIvu", "-Ke.LsfxCIvu", "--copy-links",
                    "--copy-unsafe-links", "--copy-dirlinks", "--keep-dirlinks",
                    "--no-munge-links"):
            self.refused("rsync --server --sender %s . {root}/" % opt, "links")

    def test_options_smuggled_past_the_check(self):
        """-s and --protect-args send the paths once rsync runs, where this cannot see
        them; an option after the `.` is a path, and a bare -e or a letter hidden among
        allowed ones is still refused."""
        for cmd, why in (("rsync --server --sender -tRse.LsfxCIvu . {root}/", "past this check"),
                         ("rsync --server --sender -sre.iLsfxCIvu", "past this check"),   # as -s sends it
                         ("rsync --server --sender --protect-args -tRe.LsfxCIvu . {root}/", "past this check"),
                         ("rsync --server --sender --secluded-args -tRe.LsfxCIvu . {root}/", "past this check"),
                         ("rsync --server --sender -tRe.LsfxCIvu . --delete {root}/", "not a full path"),
                         ("rsync --server --sender -tRe.LsfxCIvu . -x", "not a full path"),
                         ("rsync --server --sender -e sh . {root}/", "names a command"),
                         ("rsync --server --sender -te . {root}/", "names a command"),
                         ("rsync --server --sender -tRbe.LsfxCIvu . {root}/", "backups"),
                         ("rsync --server --sender -tR%e.Ls . {root}/", "not an option"),
                         ("rsync --server --sender - . {root}/", "not an option"),
                         ("rsync --server --sender --files-from=/etc/passwd . {root}/", "only -"),
                         ("rsync --server --sender --files-from - . {root}/", "only -"),
                         ("rsync --server --sender --exclude-from={outside}/x . {root}/", "reads a file"),
                         ("rsync --server --sender --timeout=60s . {root}/", "value this key does not take"),
                         ("rsync --server --sender --info=/x . {root}/", "value this key does not take"),
                         ("rsync --server --sender --timeout . {root}/", "same word"),
                         ("rsync --server --sender --append=1 . {root}/", "takes no value"),
                         ("rsync --server --sender --whatever . {root}/", "not an option"),
                         ("rsync --server --sender -re.LsfxCIvu {root}/", "not an option"),
                         ("rsync --server --sender -re.LsfxCIvu", "no `.`"),
                         ("rsync --server --sender -re.LsfxCIvu .", "no folder")):
            self.refused(cmd, why)

    def test_quoting_is_refused(self):
        for cmd in ("rsync --server --sender . {root}/a\\ b", "rsync --server --sender . '{root}/'",
                    'rsync --server --sender . "{root}/"', "rsync --server --sender . {root}/\nid"):
            self.refused(cmd, "quoting")

    def test_paths_out_of_the_root(self):
        os.symlink(self.outside, os.path.join(self.root, "2026-11-23"))
        for path, why in (("2026-11-25/", "not a full path"), ("~/ebpfm/", "not a full path"),
                          ("{root}/../private/", "`..`"), ("{root}/2026-11-25/../../private/", "`..`"),
                          ("{outside}/", "outside"), ("/etc/passwd", "outside"), ("/", "outside"),
                          ("{root}-other/", "outside"), ("{root}/2026-11-23/", "outside"),
                          ("{root}/2026-11-23/secret.jsonl", "outside"),
                          ("{root}/*", "wildcard"), ("{root}/2026-11-2?/", "wildcard"),
                          ("{root}/[0-9]*", "wildcard")):
            self.refused("rsync --server --sender -re.LsfxCIvu . {root}/ %s" % path, why)


def run_guard(args, command, env=None):
    e = dict(os.environ)
    e.pop("SSH_ORIGINAL_COMMAND", None)
    if command is not None:
        e["SSH_ORIGINAL_COMMAND"] = command
    e.update(env or {})
    return subprocess.run([sys.executable, GUARD] + list(args), env=e, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, universal_newlines=True, timeout=60)


class TestAsSshdRunsIt(Tree):
    """The script itself, with an rsync stand-in that prints the arguments it was run with."""

    def setUp(self):
        super().setUp()
        self.stub = os.path.join(self.tmp, "rsync")
        with open(self.stub, "w") as fh:
            fh.write('#!/bin/bash\nprintf "%s\\n" "$0" "$@"\n')
        os.chmod(self.stub, 0o755)

    def test_runs_rsync_with_the_arguments_it_was_asked_for(self):
        cmd = APPEND.format(root=self.root)
        p = run_guard(["--rsync", self.stub, self.root], cmd)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout.splitlines(), [self.stub] + cmd.split()[1:])
        self.assertEqual(p.stderr, "")
        p = run_guard(["--rsync=%s" % self.stub, self.root, "/elsewhere"], cmd)
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_a_refusal_is_one_line_and_exit_1(self):
        p = run_guard(["--rsync", self.stub, self.root],
                      "rsync --server --sender --remove-source-files . %s/" % self.root)
        self.assertEqual((p.returncode, p.stdout), (1, ""))
        self.assertEqual(len(p.stderr.splitlines()), 1)
        self.assertTrue(p.stderr.startswith("rsync_guard.py: refused: --remove-source-files"), p.stderr)
        p = run_guard(["--rsync", self.stub, self.root], None)
        self.assertEqual(p.returncode, 1)
        self.assertIn("no shell", p.stderr)

    def test_installed_wrong_is_exit_2(self):
        cmd = LIST.format(root=self.root)
        for args, why in (([], "no folder to read"), (["--rsync", self.stub], "no folder to read"),
                          (["--rsync", self.stub, "ebpfm"], "not a full path"),
                          (["--rsync", self.stub, "/"], "not a full path below /"),
                          (["--rsync", self.stub, "/."], "not a full path below /"),
                          (["--rsync", "rsync", self.root], "not a full path to a program"),
                          (["--rsync", os.path.join(self.tmp, "none"), self.root], "not a full path to a program"),
                          (["--root", self.root], "not an option")):
            p = run_guard(args, cmd)
            self.assertEqual(p.returncode, 2, args)
            self.assertIn(why, p.stderr, args)
            self.assertEqual(p.stdout, "")


@unittest.skipUnless(RSYNC, "rsync is not installed")
class Wire(Tree):
    """Real rsync, its node side behind the guard, through the ssh stand-in."""

    def setUp(self):
        super().setUp()
        self.ssh = os.path.join(self.tmp, "ssh")
        with open(self.ssh, "w") as fh:
            fh.write(SSH)
        os.chmod(self.ssh, 0o755)
        self.calls = os.path.join(self.tmp, "calls")
        self.mirror = os.path.join(self.tmp, "dash", "data", "mirror")
        self.env = dict(os.environ, GUARD_CALLS=self.calls, LC_ALL="C",
                        GUARD_FORCED=" ".join(shlex.quote(a) for a in (
                            sys.executable, GUARD, "--rsync", RSYNC, self.root)))
        self.env.pop("RSYNC_PROTECT_ARGS", None)

    def write(self, rel, data, mode="ab"):
        p = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, mode) as fh:
            fh.write(data)

    def seal(self, rel):
        """What the collector's `gzip -f` leaves: the whole .gz, and no plain file."""
        p = os.path.join(self.root, rel)
        with open(p, "rb") as fh, gzip.open(p + ".gz", "wb") as out:
            out.write(fh.read())
        os.unlink(p)

    def rsync(self, *args, stdin=None):
        return subprocess.run([RSYNC, "-e", self.ssh] + list(args), env=self.env,
                              input=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              universal_newlines=True, timeout=60)

    def sent(self):
        if not os.path.exists(self.calls):
            return []
        with open(self.calls) as fh:
            return fh.read().splitlines()


def lines(start, n):
    return b"".join(b'{"n":%d,"pad":"%s"}\n' % (i, b"x" * 40) for i in range(start, start + n))


class TestPullThroughTheGuard(Wire):
    """deploy/fleet/pull.py copies a node whose key runs the guard, as the HPC kit sets it
    up: every call it makes is let through, and the copy is the collector's own."""

    def fleet_env(self, **over):
        vals = {"FLEET_HOSTS": "n1=alice@n1:%s:" % self.root, "FLEET_SSH": self.ssh,
                "FLEET_SSH_KEY": "", "FLEET_EBPF_SOURCE": self.root, "FLEET_NODE_SOURCE": "",
                "FLEET_MIRROR": self.mirror, "FLEET_FREE_MIN_GB": "0"}
        vals.update(over)
        p = os.path.join(self.tmp, "fleet.env")
        with open(p, "w") as fh:
            for k, v in vals.items():
                fh.write('%s="%s"\n' % (k, v))
        return p

    def pull(self, conf, code=0):
        p = subprocess.run([sys.executable, PULL, "--config", conf, "once"], env=self.env,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           universal_newlines=True, timeout=120)
        self.assertEqual(p.returncode, code, p.stdout + p.stderr)
        return p.stdout + p.stderr

    def copy(self, rel):
        return os.path.join(self.mirror, "ebpf", "n1", rel)

    def state(self):
        with open(os.path.join(self.mirror, ".state", "n1.json")) as fh:
            return json.load(fh)

    def test_live_day_sealed_day_and_the_seal_between(self):
        self.write("2026-11-24/n1.exits.jsonl", lines(0, 40))
        self.seal("2026-11-24/n1.exits.jsonl")
        self.write("2026-11-25/n1.exits.jsonl", lines(40, 10))
        self.write("2026-11-25/collector.log", b"not a day-file\n")
        conf = self.fleet_env()
        self.pull(conf)
        with gzip.open(self.copy("2026-11-24/n1.exits.jsonl.gz")) as fh:
            self.assertEqual(fh.read(), lines(0, 40))
        live = self.copy("2026-11-25/n1.exits.jsonl")
        with open(live, "rb") as fh:
            self.assertEqual(fh.read(), lines(40, 10))
        self.assertFalse(os.path.exists(self.copy("2026-11-25/collector.log")))
        self.assertTrue(self.state()["ok"], self.state())
        # it grows in place, as the dashboard needs (it knows a file by its inode)
        ino = os.stat(live).st_ino
        self.write("2026-11-25/n1.exits.jsonl", lines(50, 5))
        self.pull(conf)
        self.assertEqual(os.stat(live).st_ino, ino)
        with open(live, "rb") as fh:
            self.assertEqual(fh.read(), lines(40, 15))
        # the day sealed on the node: its .gz comes, and the plain copy goes once it inflates
        self.seal("2026-11-25/n1.exits.jsonl")
        self.pull(conf)
        self.assertFalse(os.path.exists(live))
        with gzip.open(live + ".gz") as fh:
            self.assertEqual(fh.read(), lines(40, 15))
        sent = self.sent()
        self.assertTrue(sent)
        for cmd in sent:
            self.assertTrue(cmd.startswith("rsync --server --sender "), cmd)
            self.assertTrue(cmd.endswith(" . %s/" % self.root), cmd)

    def test_a_node_whose_collector_wrote_nothing_yet(self):
        """No root on the node: the guard lets rsync say so, and pull.py names the folder."""
        shutil.rmtree(self.root)
        out = self.pull(self.fleet_env(), code=1)
        self.assertIn("listing alice@n1:%s failed" % self.root, out)
        self.assertIn("change_dir", out)
        self.assertIn(self.root, self.state()["error"])
        self.assertNotIn("rsync_guard", self.state()["error"])

    def test_a_sealed_day_placed_ahead_is_not_fetched_again(self):
        """Seeding: a node's own .gz put in the copy before the first pass, byte for byte,
        stays as it is, its inode with it; one that differs is fetched from the node."""
        self.write("2026-11-23/n1.exits.jsonl", lines(0, 30))
        self.seal("2026-11-23/n1.exits.jsonl")
        self.write("2026-11-24/n1.exits.jsonl", lines(30, 30))
        self.seal("2026-11-24/n1.exits.jsonl")
        self.write("2026-11-25/n1.exits.jsonl", lines(60, 5))
        for day in ("2026-11-23", "2026-11-24"):
            os.makedirs(self.copy(day))
        same = self.copy("2026-11-23/n1.exits.jsonl.gz")
        shutil.copy(os.path.join(self.root, "2026-11-23/n1.exits.jsonl.gz"), same)
        other = self.copy("2026-11-24/n1.exits.jsonl.gz")
        with gzip.open(other, "wb", compresslevel=1) as fh:     # the same lines, packed otherwise
            fh.write(lines(30, 30) + lines(0, 1))
        ino = os.stat(same).st_ino
        self.pull(self.fleet_env())
        self.assertEqual(os.stat(same).st_ino, ino)
        with open(other, "rb") as fh, open(os.path.join(self.root, "2026-11-24/n1.exits.jsonl.gz"), "rb") as node:
            self.assertEqual(fh.read(), node.read())
        self.assertEqual(self.state()["tiers"]["ebpf"]["fetched_gz"], 1)


class TestByHand(Wire):
    def test_rsync_av_and_a_listing_with_the_key(self):
        """What a person may run with the key to look: a listing, and rsync -av of a day."""
        self.write("2026-11-25/n1.exits.jsonl", lines(0, 3))
        p = self.rsync("--list-only", "alice@n1:%s/2026-11-25/" % self.root)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("n1.exits.jsonl", p.stdout)
        dst = os.path.join(self.tmp, "look")
        p = self.rsync("-av", "alice@n1:%s/2026-11-25/" % self.root, dst)
        self.assertEqual(p.returncode, 0, p.stderr)
        with open(os.path.join(dst, "n1.exits.jsonl"), "rb") as fh:
            self.assertEqual(fh.read(), lines(0, 3))


class TestHostileClients(Wire):
    """What an rsync client with the key could try, beyond what pull.py does: each is
    refused before rsync runs, or (the names --files-from carries) kept inside the root
    by rsync's own sender."""

    def test_writing_to_the_node_is_refused(self):
        local = os.path.join(self.tmp, "local.jsonl")
        with open(local, "w") as fh:
            fh.write("{}\n")
        p = self.rsync(local, "alice@n1:%s/2026-11-25/" % self.root)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("rsync_guard.py: refused: this key reads", p.stderr)
        self.assertEqual(os.listdir(os.path.join(self.root, "2026-11-25")), [])

    def test_removing_the_nodes_files_is_refused(self):
        self.write("2026-11-24/n1.exits.jsonl", lines(0, 3))
        dst = os.path.join(self.tmp, "dst")
        p = self.rsync("-r", "--remove-source-files", "alice@n1:%s/" % self.root, dst)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("--remove-source-files deletes or removes files", p.stderr)
        self.assertTrue(os.path.exists(os.path.join(self.root, "2026-11-24/n1.exits.jsonl")))
        # --delete on a pull deletes on the receiving side only: rsync does not send it to
        # the node, whose files stay (the guard refuses it if it ever came: TestRefused)
        p = self.rsync("-r", "--delete", "alice@n1:%s/" % self.root, dst)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertTrue(os.path.exists(os.path.join(self.root, "2026-11-24/n1.exits.jsonl")))
        self.assertNotIn("--delete", "\n".join(self.sent()))

    def test_reading_outside_the_root_is_refused(self):
        os.symlink(self.outside, os.path.join(self.root, "2026-11-23"))
        dst = os.path.join(self.tmp, "dst")
        for src in (self.outside + "/", self.root + "/../private/", self.root + "/2026-11-23/",
                    "/etc/hostname"):
            p = self.rsync("-r", "alice@n1:" + src, dst)
            self.assertNotEqual(p.returncode, 0, src)
            self.assertIn("rsync_guard.py: refused:", p.stderr, src)
        p = self.rsync("-rL", "alice@n1:%s/" % self.root, dst)
        self.assertIn("follows links out", p.stderr)
        self.assertFalse(os.path.exists(os.path.join(dst, "secret.jsonl")))
        self.assertFalse(os.path.exists(os.path.join(dst, "2026-11-23", "secret.jsonl")))

    def test_names_sent_over_the_connection_stay_inside(self):
        """--files-from=- is let through, and its names come once rsync runs: rsync's
        sender takes `..` and a leading `/` off each, so they name files in the root."""
        self.write("2026-11-25/n1.exits.jsonl", lines(0, 2))
        dst = os.path.join(self.tmp, "dst")
        names = "\0".join(("../private/secret.jsonl", self.outside + "/secret.jsonl",
                           "2026-11-25/../../private/secret.jsonl", "2026-11-25/n1.exits.jsonl"))
        p = self.rsync("-t", "--files-from=-", "--from0", "alice@n1:%s/" % self.root, dst + "/",
                       stdin=names + "\0")
        self.assertNotIn("rsync_guard.py", p.stderr)
        self.assertTrue(os.path.exists(os.path.join(dst, "2026-11-25", "n1.exits.jsonl")))
        got = [os.path.join(d, f) for d, _, fs in os.walk(dst) for f in fs]
        self.assertFalse([g for g in got if g.endswith("secret.jsonl")], got)

    def test_a_shell_or_protected_args_are_refused(self):
        p = subprocess.run([self.ssh, "-o", "BatchMode=yes", "alice@n1", "cat", "/etc/passwd"],
                           env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           universal_newlines=True, timeout=60)
        self.assertEqual((p.returncode, p.stdout), (1, ""))
        self.assertIn("not cat", p.stderr)
        p = self.rsync("-r", "--protect-args", "alice@n1:%s/" % self.root, os.path.join(self.tmp, "d"))
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("past this check", p.stderr)


if __name__ == "__main__":
    unittest.main()
