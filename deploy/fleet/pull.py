#!/usr/bin/env python3
"""pull.py: copy each fleet node's collector day-files to the dashboard's host, with rsync.

    pull.py [--config FILE] once     one pass over every node, then exit: 1 if a node failed
    pull.py [--config FILE] loop     each node on its own, a pass every FLEET_INTERVAL_S
    pull.py [--config FILE] status   each node's last pass, from the mirror's state folder
    pull.py [--config FILE] roots    the dashboard's RC_DASH_EBPF_ROOTS and RC_DASH_NODE_ROOTS

FILE (default /etc/rc-dashboard/fleet.env) holds KEY=VALUE lines; fleet.env.example
explains each key. Every node keeps writing its own files, and this copies them: node
NAME's eBPF tier into MIRROR/ebpf/NAME/<day>/, its node tier into MIRROR/node/NAME/<day>/,
in the collector's own layout. The dashboard reads each MIRROR/<tier>/NAME as one root
(`roots`), exactly as it would read the node's own folder.

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
}


class ConfigError(Exception):
    pass


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
    comment line, and a value may be quoted. No $-expansion."""
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
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]
            vals[k] = v
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


class Settings(object):
    def __init__(self, vals):
        unknown = sorted(k for k in vals if k.startswith("FLEET_") and k not in DEFAULTS)
        if unknown:
            raise ConfigError("unknown setting(s) %s: fleet.env.example lists them all"
                              % ", ".join(unknown))
        v = dict(DEFAULTS)
        v.update({k: x for k, x in vals.items() if k in DEFAULTS})
        self.vals = v
        self.mirror = os.path.abspath(v["FLEET_MIRROR"])
        self.state = os.path.join(self.mirror, STATE)
        self.days = _int(v, "FLEET_DAYS", 0, 100000)
        self.keep_days = _int(v, "FLEET_KEEP_DAYS", 0, 100000)
        self.interval = _int(v, "FLEET_INTERVAL_S", 5, 86400)
        self.timeout = _int(v, "FLEET_TIMEOUT_S", 30, 86400)
        self.jobs = _int(v, "FLEET_JOBS", 1, 256)
        try:
            self.free_min = float(v["FLEET_FREE_MIN_GB"]) * (1 << 30)
        except ValueError:
            raise ConfigError("FLEET_FREE_MIN_GB=%r is not a number" % v["FLEET_FREE_MIN_GB"])
        self.sources = {}
        for tier, key in TIERS:
            src = v[key].rstrip("/")
            if src:
                if not PLAIN_PATH.match(src) or src.startswith("-"):
                    raise ConfigError("%s=%r: no spaces, quotes, or backslashes" % (key, v[key]))
                self.sources[tier] = src
        if "ebpf" not in self.sources:
            raise ConfigError("FLEET_EBPF_SOURCE is empty: the eBPF tier is the one feed the "
                              "dashboard needs")
        for key in ("FLEET_SSH_KEY", "FLEET_MIRROR", "FLEET_USER", "FLEET_DOMAIN"):
            if v[key] and not PLAIN_PATH.match(v[key]):
                raise ConfigError("%s=%r: no spaces, quotes, or backslashes" % (key, v[key]))
        self.hosts = self._hosts(v)

    def _hosts(self, v):
        """{name: ssh target}. An entry is NAME, or NAME=TARGET for a node that ssh
        reaches by another name: [user@]host, or a Host alias in ~/.ssh/config."""
        out = {}
        for entry in v["FLEET_HOSTS"].replace(",", " ").split():
            name, _, target = entry.partition("=")
            if not NAME_RE.match(name):
                raise ConfigError("FLEET_HOSTS: %r is not a node name (letters, digits, "
                                  ". _ -)" % name)
            if name in out:
                raise ConfigError("FLEET_HOSTS names %s twice" % name)
            if not target:
                target = name + ("." + v["FLEET_DOMAIN"] if v["FLEET_DOMAIN"] else "")
            if "@" not in target and v["FLEET_USER"]:
                target = v["FLEET_USER"] + "@" + target
            if not PLAIN_PATH.match(target) or target.startswith("-"):
                raise ConfigError("FLEET_HOSTS: %r is not an ssh target" % target)
            out[name] = target
        if not out:
            raise ConfigError("FLEET_HOSTS names no node")
        return out

    def ssh(self):
        """The remote shell rsync runs (`rsync -e`), as one string."""
        cmd = [self.vals["FLEET_SSH"]]
        if self.vals["FLEET_SSH_KEY"]:
            cmd += ["-i", self.vals["FLEET_SSH_KEY"]]
        cmd += SSH_OPTS
        # The nodes' host keys, and one connection to each node shared by a pass's rsyncs
        # where the socket's path fits a unix socket (%C is 40 characters).
        cmd += ["-o", "UserKnownHostsFile=" + os.path.join(self.state, "known_hosts")]
        if len(os.path.join(self.state, "cm-")) + 40 <= 100:
            cmd += ["-o", "ControlMaster=auto", "-o", "ControlPersist=120",
                    "-o", "ControlPath=" + os.path.join(self.state, "cm-%C")]
        cmd += shlex.split(self.vals["FLEET_SSH_OPTS"])
        return " ".join(shlex.quote(c) for c in cmd)

    def root(self, tier, name):
        return os.path.join(self.mirror, tier, name)

    def roots(self):
        """The dashboard's two roots variables, as KEY=VALUE lines. With no node tier
        pulled, its variable names a folder that is not there: left empty, the dashboard
        would look in its default places, and could read this host's own collector."""
        ebpf = ":".join(self.root("ebpf", n) for n in self.hosts)
        if "node" in self.sources:
            node = ":".join(self.root("node", n) for n in self.hosts)
        else:
            node = os.path.join(self.mirror, "node-tier-not-pulled")
        return "RC_DASH_EBPF_ROOTS=%s\nRC_DASH_NODE_ROOTS=%s\n" % (ebpf, node)


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
        with self.lock:
            self.proc = p
        try:
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
        # A replaced file is fetched as a new one. Its old copy is moved aside until then,
        # so the new copy cannot be given the inode number the old one frees: the
        # dashboard knows a file by (st_dev, st_ino), and would take the new file for the
        # old one, at the old one's offset.
        aside = []
        for rel in todo["replace"]:
            say("%s: %s is shorter on the node than in the copy: it was replaced there; "
                "fetching it again" % (self.name, rel))
            day, name = rel.split("/", 1)
            p = os.path.join(root, day, ".%s.replaced" % name)
            os.rename(os.path.join(root, rel), p)
            aside.append(p)
        local = local_files(root)
        behind = sum(remote[r] - local.get(r, 0) for r in todo["append"])
        try:
            if todo["append"]:
                self.copy(source, root, todo["append"], append=True)
        finally:
            for p in aside:
                os.unlink(p)
        if todo["fetch"]:
            self.copy(source, root, todo["fetch"], append=False)
        sealed = [r for r in todo["seal"] if self.seal(root, r)]
        for r in sealed:
            say("%s: %s sealed: the copy now reads its .gz" % (self.name, r))
        if self.s.keep_days:
            prune(root, self.s.keep_days, self.name)
        return {"files": len(remote), "appended": len(todo["append"]), "bytes": behind,
                "fetched_gz": len(todo["fetch"]), "sealed": len(sealed),
                "replaced": len(todo["replace"])}

    def pull(self):
        """One pass over the node's tiers. Returns the state recorded for it."""
        t0 = time.time()
        state = {"host": self.name, "target": self.target, "attempt_epoch": t0}
        try:
            free = disk_free(self.s.mirror)
            if free is not None and free < self.s.free_min:
                raise PullError("paused: %.1f GB free where the copy is, under FLEET_FREE_MIN_GB"
                                % (free / float(1 << 30)))
            tiers = {}
            for tier, _key in TIERS:
                if tier in self.s.sources and not self.stop.is_set():
                    try:
                        tiers[tier] = self.pull_tier(tier, self.s.sources[tier])
                    except Missing:
                        # The node tier is optional: a node may run the eBPF tier alone.
                        # The eBPF tier is the one feed the dashboard needs.
                        if tier == "ebpf":
                            raise
                        tiers[tier] = {"missing": self.s.sources[tier]}
            if self.stop.is_set():
                raise PullError("stopped")
            state.update(ok=True, ok_epoch=time.time(), tiers=tiers, error=None)
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
            if state["error"] != "stopped":
                say("%s: FAILED: %s" % (self.name, state["error"]))
        if state.get("error") != "stopped":
            write_state(self.s, self.name, state)
        return state


def _last(err, n=3):
    lines = [l for l in (err or "").strip().splitlines() if l.strip()]
    return " | ".join(lines[-n:]) or "no message"


def disk_free(path):
    try:
        st = os.statvfs(path)
    except OSError:
        return None
    return st.f_bavail * st.f_frsize


def prune(root, keep, name):
    """Remove the copy's day folders past the `keep` newest. Only FLEET_KEEP_DAYS turns
    this on: the copy may be the only one left of a day the node has since removed."""
    days = sorted(d for d in os.listdir(root)
                  if DAY_RE.match(d) and os.path.isdir(os.path.join(root, d))
                  and not os.path.islink(os.path.join(root, d)))
    for d in days[:-keep]:
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


def prepare(s):
    """The copy's folder and its state folder, private; and the lock that keeps a second
    pull.py off this copy. Returns the lock's file, held until the process ends."""
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
    return fh


# ------------------------------------------------------------------ commands

def cmd_once(s):
    stop = threading.Event()
    pullers = [Puller(s, n, stop) for n in s.hosts]
    _on_term(stop, pullers)
    def one(p):
        try:
            return p.pull()
        except Exception as e:          # noqa: BLE001 -- one node's surprise fails only it
            say("%s: pass ended with %s: %s" % (p.name, type(e).__name__, e))
            return {"ok": False}

    with concurrent.futures.ThreadPoolExecutor(max_workers=s.jobs) as ex:
        states = list(ex.map(one, pullers))
    return 0 if all(st["ok"] for st in states) else 1


def cmd_loop(s):
    stop = threading.Event()
    pullers = [Puller(s, n, stop) for n in s.hosts]
    _on_term(stop, pullers)
    say("pulling %d node(s) every %d s into %s" % (len(pullers), s.interval, s.mirror))

    def run(i, p):
        # Spread the first passes over the interval, so the nodes are not asked at once.
        if stop.wait(s.interval * float(i) / len(pullers)):
            return
        while not stop.is_set():
            t0 = time.time()
            try:
                p.pull()
            except Exception as e:      # noqa: BLE001 -- one node's surprise stops only it
                say("%s: pass ended with %s: %s" % (p.name, type(e).__name__, e))
            stop.wait(max(1.0, s.interval - (time.time() - t0)) + random.uniform(0, 1))

    threads = [threading.Thread(target=run, args=(i, p), name=p.name)
               for i, p in enumerate(pullers)]
    for t in threads:
        t.daemon = True
        t.start()
    while any(t.is_alive() for t in threads):
        for t in threads:
            t.join(0.5)
    say("stopped")
    return 0


def _on_term(stop, pullers):
    def handler(signum, _frame):
        stop.set()
        for p in pullers:
            p.cancel()
    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGINT, handler)


def cmd_status(s):
    now = time.time()
    rows = []
    worst = 0
    for name in s.hosts:
        st = read_state(s, name)
        if not st:
            rows.append((name, "never", "-", "no pass recorded"))
            worst = max(worst, 1)
            continue
        ok_at = st.get("ok_epoch")
        age = "%ds ago" % (now - ok_at) if ok_at else "never"
        if st.get("ok"):
            behind = sum(t.get("bytes", 0) for t in (st.get("tiers") or {}).values())
            rows.append((name, "ok", age, "%d B appended in the last pass" % behind))
        else:
            rows.append((name, "FAILED", age, st.get("error") or ""))
            worst = max(worst, 1)
    w = max(len(r[0]) for r in rows)
    for r in rows:
        print("%-*s  %-6s  last ok %-12s  %s" % (w, r[0], r[1], r[2], r[3]))
    return worst


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=os.environ.get("FLEET_CONFIG", DEFAULT_CONFIG),
                    help="the settings file (default %(default)s)")
    ap.add_argument("command", choices=("once", "loop", "status", "roots"))
    args = ap.parse_args(argv)
    try:
        s = load(args.config)
    except ConfigError as e:
        sys.stderr.write("pull.py: %s\n" % e)
        return 2
    if args.command == "roots":
        sys.stdout.write(s.roots())
        return 0
    if args.command == "status":
        return cmd_status(s)
    os.umask(0o077)
    lock = prepare(s)
    if lock is None:
        sys.stderr.write("pull.py: another pull.py holds %s; not starting a second\n"
                         % os.path.join(s.state, "pull.lock"))
        return 3
    return cmd_once(s) if args.command == "once" else cmd_loop(s)


if __name__ == "__main__":
    sys.exit(main())
