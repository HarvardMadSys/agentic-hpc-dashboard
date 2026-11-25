#!/usr/bin/env python3
"""pull.py: copy each fleet node's collector day-files to the dashboard's host, with rsync.

    pull.py [--config FILE] once     one pass over every node, then exit: 1 if a node failed
    pull.py [--config FILE] loop     each node on its own, a pass every FLEET_INTERVAL_S;
                                     SIGHUP reads FILE again and starts or stops nodes
    pull.py [--config FILE] status [--stale DURATION] [--json]
                                     each node's last pass, from the mirror's state folder:
                                     exit 1 if one FAILED, never ran, or is STALE (no good
                                     pass for 3 x FLEET_INTERVAL_S, or DURATION)
    pull.py [--config FILE] roots [--per-node]
                                     the dashboard's RC_DASH_EBPF_ROOTS, RC_DASH_NODE_ROOTS
                                     (a glob over the copy's nodes) and RC_DASH_TRANSPORT_FILES
    pull.py [--config FILE] check    the nodes FILE names: exit 0, 4 if it names none, 2 if
                                     it is wrong

Exit 2 for a wrong FILE, 3 when another pull.py holds the copy, 4 when FILE names no node
(rc-pull.service starts again on none of them).

FILE (default /etc/rc-dashboard/fleet.env) holds KEY=VALUE lines, and KEY+=VALUE to add to
a list (as bash reads `FLEET_HOSTS+=" node03"`); fleet.env.example explains each key. A node
may name its own source folders, for a fleet whose nodes differ:
FLEET_HOSTS+=" node03=10.0.0.13:/scratch/ebpfm:/scratch/ebpfm-login". Every node keeps writing its own files, and this copies them: node
NAME's eBPF tier into MIRROR/ebpf/NAME/<day>/, its node tier into MIRROR/node/NAME/<day>/,
in the collector's own layout. The dashboard reads each MIRROR/<tier>/NAME as one root
(`roots`), exactly as it would read the node's own folder. FLEET_LAYOUT=flat copies the eBPF
tier alone, into MIRROR/NAME/<day>/: the HPC kit's copy, kept where its own stream kept it.

How a file is copied. The dashboard follows a day-file by its inode and an offset into it
(rc_dashboard/tail.py), so the copy must change as the node's file does:

- A plain `.jsonl` grows in place: rsync --append writes only the bytes past the copy's
  end, into the same file. rsync's default -- a new file renamed over the old one -- would
  give the dashboard a new inode on every pass, and it would read the whole day again.
  A copy that ends in a partial line is read as one still being written, as its source is.
- A sealed day's `.gz` is copied only once its plain twin is gone from the node: the
  collector's `gzip -f` unlinks the plain file only after the whole `.gz` is written, so
  a `.gz` with no plain twin is complete and never changes again. It is written beside
  the copy and renamed into place, so it is whole from the moment it appears.
- Then the copy's plain twin goes, once the `.gz` inflates whole (CRC and length checked):
  the dashboard reads a plain file before its `.gz`, and the plain copy may be short --
  the bytes written between the last pass and the day's end are only in the `.gz`. The
  dashboard takes up the `.gz` at the offset it had reached in the plain file, as it does
  when a collector seals a day on its own disk.
- A plain file that is shorter on the node than in the copy was replaced there: the copy
  is removed and fetched again from its start, and the dashboard reads it as a new file.
  Only a shorter file is known for replaced: one replaced by a file at least as long is
  appended to as if it had grown. The collector never does that -- it appends, and starts
  a new file each day -- and telling the two apart would mean reading the file.

What is copied: `<day>/<name>.jsonl` and `<day>/<name>.jsonl.gz`, for the FLEET_DAYS newest
day folders that hold one (0: every one), and nothing else. No link is followed. The copy
is made 700/600: it names real users and holds their commands. While the copy's volume has
less than FLEET_FREE_MIN_GB free, nothing is pulled.

Python 3.6 or later, standard library only. Needs rsync here and on each node, and an ssh
key that logs in to each node as an account that can read the collector's folders.
"""
import argparse
import concurrent.futures
import errno
import fcntl
import json
import os
import random
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
import zlib

DEFAULT_CONFIG = "/etc/rc-dashboard/fleet.env"
DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\Z")
# A collector day-file, as rsync lists it relative to the source: `<day>/<name>`.
FILE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})/([A-Za-z0-9][A-Za-z0-9._@+-]*\.jsonl(?:\.gz)?)\Z")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\Z")
PLAIN_PATH = re.compile(r"^[^\s'\"\\]+\Z")         # paths that go into an ssh command line
GZ = ".gz"
OK_RC = (0, 24)            # 24: a file vanished mid-transfer -- a day being gzipped
LIST_TIMEOUT_S = 300
STATE = ".state"
TIERS = (("ebpf", "FLEET_EBPF_SOURCE"), ("node", "FLEET_NODE_SOURCE"))
# The day folders and the day-files in them, and nothing else: no log, no feature cache.
FILTER = ["--include=/[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]/", "--exclude=/*",
          "--include=/*/*.jsonl", "--include=/*/*.jsonl.gz", "--exclude=*"]
CHMOD = "--chmod=Du=rwx,Dgo=,Fu=rw,Fgo="
SSH_OPTS = ["-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=20",
            "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=4",
            "-o", "LogLevel=ERROR"]

# A FLEET_HOSTS entry: NAME[=TARGET][:EBPF_SOURCE[:NODE_SOURCE]], a v6 TARGET in brackets.
ENTRY_RE = re.compile(r"^(?P<name>[^=:\s]+)"
                      r"(?:=(?P<target>[^:\[\]\s]*\[[0-9A-Fa-f:.%]+\]|[^:\s\[\]]+))?"
                      r"(?::(?P<ebpf>[^:\s]*)(?::(?P<node>[^:\s]*))?)?\Z")

DEFAULTS = {
    "FLEET_HOSTS": "",
    "FLEET_USER": "",
    "FLEET_DOMAIN": "",
    "FLEET_SSH_KEY": "/etc/rc-dashboard/pull_key",
    "FLEET_SSH_OPTS": "",
    "FLEET_EBPF_SOURCE": "/var/log/ebpfm",
    "FLEET_NODE_SOURCE": "/var/log/ebpfm-login",
    "FLEET_MIRROR": "/var/lib/rc-dashboard/mirror",
    "FLEET_DAYS": "8",
    "FLEET_KEEP_DAYS": "0",
    "FLEET_INTERVAL_S": "60",
    "FLEET_TIMEOUT_S": "600",
    "FLEET_FREE_MIN_GB": "10",
    "FLEET_JOBS": "8",
    "FLEET_RSYNC": "rsync",
    "FLEET_SSH": "ssh",
    "FLEET_LAYOUT": "tiers",
}
LAYOUTS = ("tiers", "flat")


class ConfigError(Exception):
    pass


class NoNodes(ConfigError):
    """FLEET_HOSTS names no node: the copy waits for its nodes (exit 4)."""


class PullError(Exception):
    pass


class Missing(PullError):
    """The node has no such folder: no collector tier has written there yet."""


# rsync's word for a source folder that is not there: `change_dir "<path>" failed: No such
# file or directory (2)`. The errno, not the words: the node may speak another language.
NO_FOLDER = re.compile(r'change_dir .* failed: .*\(2\)')


def say(msg):
    sys.stdout.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    sys.stdout.flush()


# ------------------------------------------------------------------ settings

def parse_env(path):
    """KEY=VALUE lines, as a systemd EnvironmentFile or `. file` reads them: `#` starts a
    comment line, and a value may be quoted. No $-expansion. KEY+=VALUE adds VALUE to
    KEY's list, as bash does (`FLEET_HOSTS+=" node03"`), so a node is one line to add."""
    vals = {}
    with open(path) as fh:
        for n, line in enumerate(fh, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export "):].lstrip()
            if "=" not in line:
                raise ConfigError("%s:%d: not KEY=VALUE: %r" % (path, n, line))
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip()
            add = k.endswith("+")
            if add:
                k = k[:-1].rstrip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]
            vals[k] = (vals.get(k, "") + " " + v).strip() if add else v
    return vals


def _int(vals, key, lo, hi):
    raw = vals[key]
    try:
        n = int(raw)
    except ValueError:
        raise ConfigError("%s=%r is not a whole number" % (key, raw))
    if not lo <= n <= hi:
        raise ConfigError("%s=%d is not in %d..%d" % (key, n, lo, hi))
    return n


# The names every kit gives the same setting (deploy/README.md, "Settings"), taken here
# beside FLEET_*: one of them set two different ways is refused.
ALIASES = {"FREE_MIN_GB": "FLEET_FREE_MIN_GB"}


def _nearest(key, names):
    """The known name `key` is one slip from, or None."""
    import difflib
    got = difflib.get_close_matches(key, names, n=1, cutoff=0.8)
    return got[0] if got else None


class Settings(object):
    def __init__(self, vals):
        unknown = sorted(k for k in vals if k.startswith("FLEET_") and k not in DEFAULTS)
        if unknown:
            hints = ["%s (did you mean %s?)" % (k, _nearest(k, DEFAULTS)) if _nearest(k, DEFAULTS)
                     else k for k in unknown]
            raise ConfigError("unknown setting(s) %s: fleet.env.example lists them all"
                              % ", ".join(hints))
        vals = dict(vals)
        for alias, key in ALIASES.items():
            if alias in vals:
                if key in vals and vals[key] != vals[alias]:
                    raise ConfigError("%s=%r and %s=%r say two things; settle on one"
                                      % (key, vals[key], alias, vals[alias]))
                vals.setdefault(key, vals[alias])
        v = dict(DEFAULTS)
        v.update({k: x for k, x in vals.items() if k in DEFAULTS})
        self.vals = v
        self.mirror = os.path.abspath(v["FLEET_MIRROR"])
        self.state = os.path.join(self.mirror, STATE)
        self.days = _int(v, "FLEET_DAYS", 0, 100000)
        self.keep_days = _int(v, "FLEET_KEEP_DAYS", 0, 100000)
        if self.keep_days and (self.days == 0 or self.keep_days < self.days):
            raise ConfigError(
                "FLEET_KEEP_DAYS=%d keeps fewer day folders than FLEET_DAYS=%d copies, so every "
                "pass would copy again what the last one removed: set it to FLEET_DAYS or more "
                "(FLEET_DAYS=0 copies every day the node holds), or 0 to keep every one"
                % (self.keep_days, self.days))
        self.interval = _int(v, "FLEET_INTERVAL_S", 5, 86400)
        self.timeout = _int(v, "FLEET_TIMEOUT_S", 30, 86400)
        self.jobs = _int(v, "FLEET_JOBS", 1, 256)
        try:
            self.free_min = float(v["FLEET_FREE_MIN_GB"]) * (1 << 30)
        except ValueError:
            raise ConfigError("FLEET_FREE_MIN_GB=%r is not a number" % v["FLEET_FREE_MIN_GB"])
        self.layout = v["FLEET_LAYOUT"]
        if self.layout not in LAYOUTS:
            raise ConfigError("FLEET_LAYOUT=%r: tiers (MIRROR/<tier>/NAME) or flat (the eBPF "
                              "tier alone, in MIRROR/NAME)" % self.layout)
        self.sources = {}
        for tier, key in TIERS:
            src = _source(v[key], key)
            if src and (tier == "ebpf" or self.layout == "tiers"):
                self.sources[tier] = src
        if "ebpf" not in self.sources:
            raise ConfigError("FLEET_EBPF_SOURCE is empty: the eBPF tier is the one feed the "
                              "dashboard needs")
        for key in ("FLEET_SSH_KEY", "FLEET_MIRROR", "FLEET_USER", "FLEET_DOMAIN"):
            if v[key] and not PLAIN_PATH.match(v[key]):
                raise ConfigError("%s=%r: no spaces, quotes, or backslashes" % (key, v[key]))
        self.node_sources = {}
        self.hosts = self._hosts(v)

    def _hosts(self, v):
        """{name: ssh target}. An entry is NAME, or NAME=TARGET for a node that ssh
        reaches by another name: [user@]host, [user@][v6 address], or a Host alias in
        ~/.ssh/config. Either may end in :EBPF_SOURCE[:NODE_SOURCE], that node's own
        folders where the fleet's nodes differ (an rrsync key on some, full paths or an
        RPM's /scratch/ebpfm on others); NODE_SOURCE empty: its node tier is not pulled.
        Sets node_sources[name] = {tier: folder}."""
        out = {}
        for entry in v["FLEET_HOSTS"].replace(",", " ").split():
            m = ENTRY_RE.match(entry)
            if not m:
                raise ConfigError("FLEET_HOSTS: %r is not NAME[=TARGET][:EBPF_SOURCE"
                                  "[:NODE_SOURCE]]" % entry)
            if not NAME_RE.match(m.group("name")):
                raise ConfigError("FLEET_HOSTS: %r is not a node name (letters, digits, "
                                  ". _ -)" % m.group("name"))
            name, target = m.group("name"), m.group("target")
            if name in out:
                raise ConfigError("FLEET_HOSTS names %s twice" % name)
            if not target:
                target = name + ("." + v["FLEET_DOMAIN"] if v["FLEET_DOMAIN"] else "")
            if "@" not in target and v["FLEET_USER"]:
                target = v["FLEET_USER"] + "@" + target
            if not PLAIN_PATH.match(target) or target.startswith("-"):
                raise ConfigError("FLEET_HOSTS: %r is not an ssh target" % target)
            out[name] = target
            src = dict(self.sources)
            if m.group("ebpf") is not None:
                ebpf = _source(m.group("ebpf"), "FLEET_HOSTS (%s)" % name)
                if not ebpf:
                    raise ConfigError("FLEET_HOSTS: %s names no eBPF-tier folder after its ':'"
                                      % name)
                src["ebpf"] = ebpf
            if m.group("node") is not None:
                node = _source(m.group("node"), "FLEET_HOSTS (%s)" % name)
                if node and self.layout == "flat":
                    raise ConfigError("FLEET_HOSTS: %s names a node-tier folder, and "
                                      "FLEET_LAYOUT=flat copies the eBPF tier alone" % name)
                if node:
                    src["node"] = node
                else:
                    src.pop("node", None)
            self.node_sources[name] = src
        if not out:
            raise NoNodes("FLEET_HOSTS names no node: name them in %s (or `rc-deploy add-node`)"
                          % "fleet.env")
        return out

    def sources_for(self, name):
        """{tier: folder on the node} for one node: its own, or the fleet's."""
        return self.node_sources.get(name, self.sources)

    def pulls_node_tier(self):
        return any("node" in self.sources_for(n) for n in self.hosts)

    def ssh(self):
        """The remote shell rsync runs (`rsync -e`), as one string."""
        cmd = [self.vals["FLEET_SSH"]]
        if self.vals["FLEET_SSH_KEY"]:
            cmd += ["-i", self.vals["FLEET_SSH_KEY"]]
        cmd += SSH_OPTS
        # The nodes' host keys, and one connection to each node shared by a pass's rsyncs
        # where the socket's path fits: ssh binds the master at <ControlPath>.<16 random
        # characters> and then moves it into place, %C is 40 characters, and a unix socket's
        # path holds 107. A path too long is no fallback: ssh exits 255.
        cmd += ["-o", "UserKnownHostsFile=" + os.path.join(self.state, "known_hosts")]
        if len(os.path.join(self.state, "cm-")) + 40 + 17 <= 107:
            cmd += ["-o", "ControlMaster=auto", "-o", "ControlPersist=120",
                    "-o", "ControlPath=" + os.path.join(self.state, "cm-%C")]
        cmd += shlex.split(self.vals["FLEET_SSH_OPTS"])
        return " ".join(shlex.quote(c) for c in cmd)

    def root(self, tier, name):
        if self.layout == "flat":
            return os.path.join(self.mirror, name)
        return os.path.join(self.mirror, tier, name)

    def roots(self, per_node=False):
        """The dashboard's settings for the copy, as KEY=VALUE lines: its two roots, a glob
        over the copy's node folders that the dashboard expands again at every rescan, so
        a node added to FLEET_HOSTS needs no restart of it (per_node: one root per node
        named now, colon-separated, as before); and RC_DASH_TRANSPORT_FILES, each node's
        state file, for the dashboard's status. The node tier's glob stays while no
        node's node tier is pulled: it matches nothing until a node added with one has
        been copied, which the dashboard's next rescan then finds -- add-node reloads
        rc-pull alone, and writes no paths.env -- and a glob that matches nothing is a
        missing root, never the dashboard's default places. Per node, an empty list names
        a folder that is not there instead: left empty, the dashboard would look in its
        default places, and could read this host's own collector."""
        if self.layout == "flat":
            # the eBPF tier alone; the node tier's root names a folder that never is (in
            # the state folder, which no node's name can be), so the dashboard reads no
            # default place for it
            ebpf = (":".join(self.root("ebpf", n) for n in self.hosts) if per_node
                    else os.path.join(self.mirror, "*"))
            node = os.path.join(self.state, "no-node-tier")
        elif per_node:
            ebpf = ":".join(self.root("ebpf", n) for n in self.hosts)
            node = ":".join(self.root("node", n) for n in self.hosts
                            if "node" in self.sources_for(n))
            if not node:
                node = os.path.join(self.mirror, "node-tier-not-pulled")
        else:
            ebpf = os.path.join(self.mirror, "ebpf", "*")
            node = os.path.join(self.mirror, "node", "*")
        return ("RC_DASH_EBPF_ROOTS=%s\nRC_DASH_NODE_ROOTS=%s\nRC_DASH_TRANSPORT_FILES=%s\n"
                % (ebpf, node, os.path.join(self.state, "*.json")))


def _source(raw, key):
    """A source folder on the node, as rsync is given it: no trailing slash, no blanks,
    quotes or backslashes, and not an option. Empty stays empty."""
    src = raw.rstrip("/")
    if src and (not PLAIN_PATH.match(src) or src.startswith("-")):
        raise ConfigError("%s=%r: no spaces, quotes, or backslashes" % (key, raw))
    return src


def load(path):
    try:
        return Settings(parse_env(path))
    except OSError as e:
        raise ConfigError("cannot read %s: %s" % (path, e.strerror or e))


# ------------------------------------------------------------------ the copy

def gz_complete(path, chunk=64 << 10):
    """True if `path` is a whole gzip file: every member ends, with its CRC and length
    right. A `.gz` cut short, or one with bytes after its last member, is not."""
    try:
        with open(path, "rb") as fh:
            d = None
            while True:
                buf = fh.read(chunk)
                if not buf:
                    return d is not None and d.eof
                while buf:
                    if d is None or d.eof:
                        d = zlib.decompressobj(16 + zlib.MAX_WBITS)
                    d.decompress(buf)
                    buf = d.unused_data if d.eof else b""
    except (OSError, zlib.error):
        return False


def local_files(root):
    """{"<day>/<name>": size} of the day-files under one root of the copy."""
    out = {}
    try:
        days = os.listdir(root)
    except OSError:
        return out
    for day in days:
        d = os.path.join(root, day)
        if not DAY_RE.match(day) or os.path.islink(d) or not os.path.isdir(d):
            continue
        for name in os.listdir(d):
            rel = day + "/" + name
            if not FILE_RE.match(rel):
                continue
            try:
                st = os.lstat(os.path.join(d, name))
            except OSError:
                continue
            if stat.S_ISREG(st.st_mode):
                out[rel] = st.st_size
    return out


def parse_listing(text):
    """{"<day>/<name>": size} from `rsync --list-only`: regular files only, in the
    collector's layout; anything else is left out."""
    out = {}
    for line in text.splitlines():
        if not line.startswith("-"):
            continue                    # a folder, a link, a device
        parts = line.split(None, 4)
        if len(parts) != 5 or not FILE_RE.match(parts[4]):
            continue
        try:
            out[parts[4]] = int(re.sub(r"[,.']", "", parts[1]))
        except ValueError:
            continue
    return out


def newest_days(files, days):
    """The `days` newest day folders that hold a day-file; every one for 0."""
    all_days = sorted({rel.split("/", 1)[0] for rel in files})
    return set(all_days[-days:] if days else all_days)


def plan(remote, local, days):
    """What one pass does to one root of the copy.

    remote, local: {"<day>/<name>": size}. Returns a dict of sorted lists:
      append   plain files to grow (or start) with rsync --append
      replace  plain files shorter on the node than in the copy: removed, then fetched
      fetch    `.gz` files whose plain twin is gone from the node, and that the copy
               lacks or holds at another size
      seal     plain files of the copy that are gone from the node, whose `.gz` the copy
               holds or fetches now: removed once the `.gz` inflates whole
    """
    window = newest_days(remote, days)
    append, replace, fetch, seal = [], [], [], []
    for rel, size in remote.items():
        if rel.split("/", 1)[0] not in window:
            continue
        if rel.endswith(GZ):
            if rel[:-len(GZ)] in remote:
                continue                # gzip has not finished with it
            if local.get(rel) != size:
                fetch.append(rel)
            continue
        have = local.get(rel)
        if have is None or have < size:
            append.append(rel)
        elif have > size:
            replace.append(rel)
            append.append(rel)
    for rel in local:
        if not rel.endswith(GZ) and rel not in remote and (
                (rel + GZ) in local or (rel + GZ) in fetch):
            seal.append(rel)
    return {k: sorted(v) for k, v in
            (("append", append), ("replace", replace), ("fetch", fetch), ("seal", seal))}


class Puller(object):
    """One node's passes. `stop` (a threading.Event) ends a pass between steps, and
    `cancel()` stops the rsync it is waiting on."""

    def __init__(self, settings, name, stop=None):
        self.s = settings
        self.name = name
        self.target = settings.hosts[name]
        self.stop = stop or threading.Event()
        self.proc = None
        self.lock = threading.Lock()
        self.bad_gz = {}               # path -> (size, mtime) that did not inflate whole

    def cancel(self):
        with self.lock:
            p = self.proc
        if p is not None and p.poll() is None:
            try:
                p.terminate()
            except OSError:
                pass

    def rsync(self, args, stdin=None, timeout=None):
        """(returncode, stdout, stderr) of one rsync; returncode -1 when stopped."""
        if self.stop.is_set():
            return -1, "", "stopped"
        cmd = [self.s.vals["FLEET_RSYNC"], "--timeout=%d" % self.s.timeout,
               "-e", self.s.ssh()] + args
        env = dict(os.environ, LC_ALL="C")
        try:
            p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, universal_newlines=True, env=env)
        except OSError as e:
            raise PullError("cannot run %s: %s" % (cmd[0], e.strerror or e))
        # Published, and the stop flag read, under the lock cancel() takes: retire()
        # sets the flag and then cancels, so either cancel() finds this process or the
        # flag is seen here. A retirement while Popen ran once found no process to end,
        # and the transfer ran to its end for a node no longer pulled.
        with self.lock:
            self.proc = p
            stopped = self.stop.is_set()
        try:
            if stopped:
                p.terminate()
                try:
                    p.communicate(timeout=10)
                except subprocess.TimeoutExpired:
                    p.kill()
                    p.communicate()
                return -1, "", "stopped"
            out, err = p.communicate(stdin, timeout=timeout)
        except subprocess.TimeoutExpired:
            p.kill()
            out, err = p.communicate()
            return -2, out, "no answer in %d s" % timeout
        finally:
            with self.lock:
                self.proc = None
        if self.stop.is_set():
            return -1, out, "stopped"
        return p.returncode, out, err

    def remote(self, source):
        rc, out, err = self.rsync(["-r", "--prune-empty-dirs", "--list-only"] + FILTER
                                  + ["%s:%s/" % (self.target, source)],
                                  timeout=LIST_TIMEOUT_S)
        if rc not in OK_RC:
            exc = Missing if (rc == 23 and NO_FOLDER.search(err or "")) else PullError
            raise exc("listing %s:%s failed (rsync exit %s): %s"
                      % (self.target, source, rc, _last(err)))
        return parse_listing(out)

    def copy(self, source, root, files, append):
        """rsync exactly `files` from the node's `source` into `root`."""
        args = ["-t", CHMOD, "--files-from=-", "--from0"]
        args += ["--append"] if append else ["--partial-dir=.rsync-partial"]
        rc, _out, err = self.rsync(args + ["%s:%s/" % (self.target, source), root + "/"],
                                   stdin="\0".join(files) + "\0")
        if rc not in OK_RC:
            raise PullError("copying from %s:%s failed (rsync exit %s): %s"
                            % (self.target, source, rc, _last(err)))

    def seal(self, root, rel):
        """Remove the copy's plain `rel` if its `.gz` inflates whole. True if removed."""
        gz = os.path.join(root, rel + GZ)
        try:
            st = os.stat(gz)
        except OSError:
            return False
        key = (st.st_size, st.st_mtime)
        if self.bad_gz.get(gz) == key:
            return False                # unchanged since it last failed to inflate
        if not gz_complete(gz):
            self.bad_gz[gz] = key
            say("%s: %s does not inflate whole; its plain copy stays" % (self.name, rel + GZ))
            return False
        self.bad_gz.pop(gz, None)
        try:
            os.unlink(os.path.join(root, rel))
        except OSError as e:
            if e.errno != errno.ENOENT:
                raise
        return True

    def pull_tier(self, tier, source):
        root = self.s.root(tier, self.name)
        os.makedirs(root, mode=0o700, exist_ok=True)
        remote = self.remote(source)
        todo = plan(remote, local_files(root), self.s.days)
        # A replaced file is fetched as a new one. Its old copy is held aside, its inode with
        # it, until the new copy exists: freed any earlier, that inode number could go to the
        # new copy, and the dashboard, which knows a file by (st_dev, st_ino), would read the
        # new file at the old one's offset. A pass that ends first leaves it to a later one.
        for rel in todo["replace"]:
            say("%s: %s is shorter on the node than in the copy: it was replaced there; "
                "fetching it again" % (self.name, rel))
            os.rename(os.path.join(root, rel), aside_of(root, rel))
        local = local_files(root)
        behind = sum(remote[r] - local.get(r, 0) for r in todo["append"])
        if todo["append"]:
            self.copy(source, root, todo["append"], append=True)
        if todo["fetch"]:
            self.copy(source, root, todo["fetch"], append=False)
        release_aside(root)
        sealed = [r for r in todo["seal"] if self.seal(root, r)]
        for r in sealed:
            say("%s: %s sealed: the copy now reads its .gz" % (self.name, r))
        if self.s.keep_days:
            prune(root, self.s.keep_days, self.name, newest_days(remote, self.s.days))
        return {"files": len(remote), "appended": len(todo["append"]), "bytes": behind,
                "fetched_gz": len(todo["fetch"]), "sealed": len(sealed),
                "replaced": len(todo["replace"]),
                "hosts": sorted({host_of(rel.split("/", 1)[1]) for rel in remote})}

    def pull(self):
        """One pass over the node's tiers. Returns the state recorded for it: the state
        file the dashboard reads (RC_DASH_TRANSPORT_FILES) and `status` prints. Besides
        the pass, it says what kind of copy this is, how often it runs, and the host names
        the node's day-files carry (`file_hosts`), which may differ from its name here."""
        t0 = time.time()
        state = {"host": self.name, "target": self.target, "attempt_epoch": t0,
                 "kind": "rsync", "interval_s": self.s.interval, "file_hosts": []}
        try:
            free = disk_free(self.s.mirror)
            if free is not None and free < self.s.free_min:
                raise PullError("paused: %.1f GB free where the copy is, under FLEET_FREE_MIN_GB"
                                % (free / float(1 << 30)))
            tiers = {}
            sources = self.s.sources_for(self.name)
            for tier, _key in TIERS:
                if tier in sources and not self.stop.is_set():
                    try:
                        tiers[tier] = self.pull_tier(tier, sources[tier])
                    except Missing:
                        # The node tier is optional: a node may run the eBPF tier alone.
                        # The eBPF tier is the one feed the dashboard needs.
                        if tier == "ebpf":
                            raise
                        tiers[tier] = {"missing": sources[tier]}
            if self.stop.is_set():
                raise PullError("stopped")
            hosts = set()
            for r in tiers.values():
                hosts.update(r.pop("hosts", ()))
            state.update(ok=True, ok_epoch=time.time(), tiers=tiers, error=None,
                         file_hosts=sorted(hosts))
        except (PullError, OSError) as e:
            state.update(ok=False, error=str(e))
        state["seconds"] = round(time.time() - t0, 3)
        prev = read_state(self.s, self.name)
        if state["ok"]:
            moved = sum(t.get("appended", 0) + t.get("fetched_gz", 0)
                        for t in state["tiers"].values())
            if moved or not prev.get("ok", True):
                say("%s: ok%s" % (self.name, "".join(
                    (" | %s: no %s on the node" % (t, r["missing"])) if "missing" in r else
                    (" | %s: +%d B in %d file(s), %d .gz, %d sealed"
                     % (t, r["bytes"], r["appended"], r["fetched_gz"], r["sealed"]))
                    for t, r in sorted(state["tiers"].items()))))
        else:
            state["ok_epoch"] = prev.get("ok_epoch")
            state["file_hosts"] = prev.get("file_hosts") or []
            if not self.stop.is_set():
                say("%s: FAILED: %s" % (self.name, state["error"]))
        # A pass its puller was stopped in (a reload retiring the node, rc-pull stopping)
        # writes nothing: it was cut short, so its rsync failed as "stopped", and the
        # state file of a node taken out was deleted (remove-node, Loop.reload), which
        # this brought back as a copy failure reported for good. The flag is read under
        # the lock retire() sets it under, so no write follows a retirement.
        with self.lock:
            if not self.stop.is_set():
                write_state(self.s, self.name, state)
        return state


STREAM_SUFFIXES = (".exits", ".snapshot")


def host_of(name):
    """`<host>[.<stream>].jsonl[.gz]` -> `<host>`, as the dashboard names a day-file's
    host: only a stream suffix it knows is cut, since a host name may hold dots."""
    for ext in (".gz", ".jsonl"):
        if name.endswith(ext):
            name = name[:-len(ext)]
    for suf in STREAM_SUFFIXES:
        if name.endswith(suf):
            return name[:-len(suf)]
    return name


def _last(err, n=3):
    lines = [l for l in (err or "").strip().splitlines() if l.strip()]
    return " | ".join(lines[-n:]) or "no message"


def disk_free(path):
    try:
        st = os.statvfs(path)
    except OSError:
        return None
    return st.f_bavail * st.f_frsize


def aside_of(root, rel):
    """Where a replaced file's old copy waits for its replacement: a name no reader takes
    for a day-file."""
    day, name = rel.split("/", 1)
    return os.path.join(root, day, ".%s.replaced" % name)


ASIDE_RE = re.compile(r"^\.([A-Za-z0-9][A-Za-z0-9._@+-]*\.jsonl)\.replaced\Z")


def release_aside(root):
    """Remove the old copies held aside whose file is in the copy again, plain or sealed:
    the new copy has an inode of its own, so the old one can be freed. One whose file is
    not there yet stays, and holds its inode, until a later pass fetches the file."""
    try:
        days = [d for d in os.listdir(root) if DAY_RE.match(d)]
    except OSError:
        return
    for day in days:
        d = os.path.join(root, day)
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for n in names:
            m = ASIDE_RE.match(n)
            if m and (os.path.isfile(os.path.join(d, m.group(1)))
                      or os.path.isfile(os.path.join(d, m.group(1) + GZ))):
                os.unlink(os.path.join(d, n))


def prune(root, keep, name, window=()):
    """Remove the copy's day folders past the `keep` newest, but none in `window`, the days
    this pass copies. Only FLEET_KEEP_DAYS turns this on: the copy may be the only one left
    of a day the node has since removed."""
    days = sorted(d for d in os.listdir(root)
                  if DAY_RE.match(d) and os.path.isdir(os.path.join(root, d))
                  and not os.path.islink(os.path.join(root, d)))
    for d in days[:-keep]:
        if d in window:
            continue
        shutil.rmtree(os.path.join(root, d))
        say("%s: removed %s (FLEET_KEEP_DAYS=%d)" % (name, os.path.join(root, d), keep))


# ------------------------------------------------------------------ state

def _state_path(s, name):
    return os.path.join(s.state, name + ".json")


def read_state(s, name):
    try:
        with open(_state_path(s, name)) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def write_state(s, name, state):
    p = _state_path(s, name)
    tmp = "%s.%d.tmp" % (p, os.getpid())
    with open(tmp, "w") as fh:
        json.dump(state, fh, sort_keys=True)
    os.rename(tmp, p)


def drop_state(s, name):
    """The state file of a node no longer pulled: left, it aged into a copy failure."""
    try:
        os.unlink(_state_path(s, name))
    except FileNotFoundError:
        pass


def prepare(s):
    """The copy's folder and its state folder, private; and the lock that keeps a second
    pull.py off this copy, which holds the holder's pid: the HPC kit's deploy sends that
    pid SIGHUP (a scope has no main process to signal alone). Returns the lock's file,
    held until the process ends."""
    for d in (s.mirror, s.state):
        try:
            os.makedirs(d, mode=0o700)
        except OSError as e:
            if e.errno != errno.EEXIST:
                raise
    os.chmod(s.mirror, 0o700)
    os.chmod(s.state, 0o700)
    fh = open(os.path.join(s.state, "pull.lock"), "a")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    fh.truncate(0)
    fh.write("%d\n" % os.getpid())
    fh.flush()
    return fh


# ------------------------------------------------------------------ commands

def cmd_once(s):
    stop = threading.Event()
    pullers = [Puller(s, n, stop) for n in s.hosts]
    _on_term(stop, lambda: pullers)

    def one(p):
        try:
            return p.pull()
        except Exception as e:          # noqa: BLE001 -- one node's surprise fails only it
            say("%s: pass ended with %s: %s" % (p.name, type(e).__name__, e))
            return {"ok": False}

    with concurrent.futures.ThreadPoolExecutor(max_workers=s.jobs) as ex:
        states = list(ex.map(one, pullers))
    return 0 if all(st["ok"] for st in states) else 1


class Loop(object):
    """`loop`: each node on its own thread, a pass every FLEET_INTERVAL_S. SIGHUP reads
    the settings file again: a node added starts, a node taken out stops and its pull
    state goes once its last pass has ended (its copy stays), a node whose target or
    folders changed starts again, and the others take the new settings at their next
    pass. A file that does not load keeps every node as it was, and says why; a new
    FLEET_MIRROR needs a restart, since the copy and its lock are where they are."""

    def __init__(self, s, path):
        self.s, self.path = s, path
        self.stop = threading.Event()
        self.reload_wanted = threading.Event()
        self.nodes = {}                  # name -> (Puller, Thread)

    def start(self, name, delay=0.0):
        p = Puller(self.s, name, threading.Event())
        t = threading.Thread(target=self.run, args=(p, delay), name=name)
        t.daemon = True
        self.nodes[name] = (p, t)
        t.start()

    def retire(self, name):
        p, t = self.nodes.pop(name)
        with p.lock:                     # no state write after this (Puller.pull)
            p.stop.set()
        p.cancel()
        return t

    def run(self, p, delay):
        # Spread the first passes over the interval, so the nodes are not asked at once.
        if p.stop.wait(delay):
            return
        while not p.stop.is_set():
            t0 = time.time()
            try:
                p.pull()
            except Exception as e:      # noqa: BLE001 -- one node's surprise stops only it
                say("%s: pass ended with %s: %s" % (p.name, type(e).__name__, e))
            p.stop.wait(max(1.0, p.s.interval - (time.time() - t0)) + random.uniform(0, 1))

    def reload(self):
        try:
            new = load(self.path)
        except NoNodes as e:
            # The last node taken out is a reload like any other: every puller
            # retires. Kept as "a broken file", it went on copying a node the
            # settings no longer named, and undid a purge of its copy. rc-pull
            # stays up, idle, so the reload that names a node again reaches it.
            names = list(self.nodes)
            retired = [self.retire(n) for n in names]
            for t in retired:
                t.join(30)
            for n in names:
                drop_state(self.s, n)
            say("reload: %s; every node retired, and rc-pull waits for one to be named" % e)
            return
        except ConfigError as e:
            say("reload: %s; every node stays as it was" % e)
            return
        if new.mirror != self.s.mirror:
            say("reload: FLEET_MIRROR is now %s; restart rc-pull to move the copy there. "
                "Every node stays as it was" % new.mirror)
            return
        old, self.s = self.s, new
        gone = [n for n in self.nodes if n not in new.hosts]
        changed = [n for n in self.nodes if n in new.hosts and (
            new.hosts[n] != old.hosts.get(n) or new.sources_for(n) != old.sources_for(n))]
        added = [n for n in new.hosts if n not in self.nodes]
        retired = [self.retire(n) for n in gone + changed]
        for t in retired:
            t.join(30)
        # A node taken out: its state once its last pass has ended, which remove-node's
        # own delete, sent with the reload, can come before.
        for n in gone:
            drop_state(self.s, n)
        for p, _t in self.nodes.values():
            p.s = new                    # taken at its next pass
        for i, n in enumerate(changed + added):
            self.start(n, delay=new.interval * float(i) / max(1, len(changed + added)))
        say("reloaded %s: %d node(s)%s%s%s" % (
            self.path, len(self.nodes),
            "".join(" | +%s" % n for n in added), "".join(" | -%s" % n for n in gone),
            "".join(" | ~%s" % n for n in changed)))

    def main(self):
        def term(signum, _frame):
            self.stop.set()
            for p, _t in list(self.nodes.values()):
                p.stop.set()
                p.cancel()

        signal.signal(signal.SIGTERM, term)
        signal.signal(signal.SIGINT, term)
        signal.signal(signal.SIGHUP, lambda *a: self.reload_wanted.set())
        names = list(self.s.hosts)
        say("pulling %d node(s) every %d s into %s" % (len(names), self.s.interval,
                                                         self.s.mirror))
        for i, n in enumerate(names):
            self.start(n, delay=self.s.interval * float(i) / len(names))
        while not self.stop.is_set():
            if self.reload_wanted.is_set():
                self.reload_wanted.clear()
                self.reload()
            self.stop.wait(0.5)
        for _p, t in list(self.nodes.values()):
            t.join(30)
        say("stopped")
        return 0


def cmd_loop(s, path):
    return Loop(s, path).main()


def _on_term(stop, pullers):
    def handler(signum, _frame):
        stop.set()
        for p in pullers():
            p.cancel()
    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGINT, handler)


def fmt_age(seconds):
    """`42s`, `5m 3s`, `3h 12m`, `3d 2h`: an age a person reads at a glance."""
    s = int(max(0, seconds))
    if s < 60:
        return "%ds" % s
    if s < 3600:
        return "%dm %ds" % (s // 60, s % 60)
    if s < 86400:
        return "%dh %dm" % (s // 3600, s % 3600 // 60)
    return "%dd %dh" % (s // 86400, s % 86400 // 3600)


def parse_duration(text):
    """`90`, `90s`, `15m`, `2h`, `1d`: seconds."""
    m = re.match(r"^\s*(\d+(?:\.\d+)?)\s*([smhd]?)\s*\Z", str(text))
    if not m:
        raise ValueError("%r is not a duration such as 300, 15m, 2h or 1d" % text)
    return float(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]


# Where the copy runs, for a verdict that asks whether it does: rc-pull.service, or
# the HPC kit's copy window (hpc/deploy/keep-running.sh), which runs this same loop.
RUNS_IN = "rc-pull; on the HPC kit, its copy window"


def node_status(s, name, now, stale_s):
    """One node's verdict from its state file: ok, STALE (no good pass for stale_s:
    the copy is stopped, or every pass hangs), FAILED, or never."""
    st = read_state(s, name)
    row = {"host": name, "kind": "rsync", "ok": False, "last_ok_age_s": None,
           "last_attempt_age_s": None, "error": None, "file_hosts": st.get("file_hosts") or [],
           "bytes_last_pass": None}
    if not st:
        row.update(state="never", error="no pass recorded: is the copy running? (%s)" % RUNS_IN)
        return row
    if st.get("ok_epoch"):
        row["last_ok_age_s"] = round(now - st["ok_epoch"], 1)
    if st.get("attempt_epoch"):
        row["last_attempt_age_s"] = round(now - st["attempt_epoch"], 1)
    if not st.get("ok"):
        row.update(state="FAILED", error=st.get("error") or "no message")
    elif row["last_ok_age_s"] is not None and row["last_ok_age_s"] > stale_s:
        row.update(state="STALE", error="no pass for %s: is the copy running? (%s)"
                   % (fmt_age(row["last_ok_age_s"]), RUNS_IN))
    else:
        row.update(state="ok", ok=True)
        row["bytes_last_pass"] = sum(t.get("bytes", 0) for t in (st.get("tiers") or {}).values())
    return row


def cmd_status(s, stale_s=None, as_json=False):
    now = time.time()
    stale_s = stale_s if stale_s is not None else 3 * s.interval
    rows = [node_status(s, n, now, stale_s) for n in s.hosts]
    worst = 0 if all(r["ok"] for r in rows) else 1
    if as_json:
        print(json.dumps({"stale_after_s": stale_s, "nodes": rows}, sort_keys=True))
        return worst
    w = max(len(r["host"]) for r in rows)
    for r in rows:
        age = ("%s ago" % fmt_age(r["last_ok_age_s"])) if r["last_ok_age_s"] is not None else "never"
        if r["ok"]:
            detail = "%d B appended in the last pass" % r["bytes_last_pass"]
        else:
            detail = r["error"]
            if r["state"] == "FAILED" and r["last_attempt_age_s"] is not None \
                    and r["last_attempt_age_s"] > stale_s:
                detail += " (last tried %s ago: is the copy running?)" % fmt_age(r["last_attempt_age_s"])
        print("%-*s  %-6s  last ok %-12s  %s" % (w, r["host"], r["state"], age, detail))
    return worst


def cmd_check(s):
    print("%d node(s): %s" % (len(s.hosts), " ".join(sorted(s.hosts))))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=os.environ.get("FLEET_CONFIG", DEFAULT_CONFIG),
                    help="the settings file (default %(default)s)")
    ap.add_argument("command", choices=("once", "loop", "status", "roots", "check"))
    ap.add_argument("--stale", default=None, metavar="DURATION",
                    help="status: a node with no good pass for this long is STALE "
                         "(default 3 x FLEET_INTERVAL_S)")
    ap.add_argument("--json", action="store_true", help="status: one JSON document")
    ap.add_argument("--per-node", action="store_true",
                    help="roots: one root per node named now, instead of a glob")
    args = ap.parse_args(argv)
    try:
        s = load(args.config)
    except NoNodes as e:
        sys.stderr.write("pull.py: %s\n" % e)
        return 4
    except ConfigError as e:
        sys.stderr.write("pull.py: %s\n" % e)
        return 2
    if args.command == "roots":
        sys.stdout.write(s.roots(per_node=args.per_node))
        return 0
    if args.command == "check":
        return cmd_check(s)
    if args.command == "status":
        try:
            stale = parse_duration(args.stale) if args.stale is not None else None
        except ValueError as e:
            sys.stderr.write("pull.py: --stale: %s\n" % e)
            return 2
        return cmd_status(s, stale, args.json)
    os.umask(0o077)
    lock = prepare(s)
    if lock is None:
        sys.stderr.write("pull.py: another pull.py holds %s; not starting a second\n"
                         % os.path.join(s.state, "pull.lock"))
        return 3
    return cmd_once(s) if args.command == "once" else cmd_loop(s, args.config)


if __name__ == "__main__":
    sys.exit(main())
