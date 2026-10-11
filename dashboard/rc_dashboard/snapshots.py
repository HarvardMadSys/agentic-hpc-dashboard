"""Built windows, saved to disk so a restart or a window switch resumes instead of re-reading.

A window build is the one expensive read left after startup: a 24 h view of one
login node is ~0.7 GB of JSONL and ~10 s, a 7 d view a minute or more. `run.sh`
restarts the service on every save and the registry holds two windows, so the
same views were being reduced from scratch over and over.

What is saved is a build's reducer state the moment its scan ends, together with
exactly how far into each day-file that scan read: `(st_dev, st_ino)` and the
offset just past the last complete line. A resumed view reads each file on from
that offset and nothing else (`export.resume_plan`), so it holds the records a
service that had never stopped would hold -- the snapshot is a memo of having
read those bytes, not a second source of truth. What the page shows is still a
function of the collector's files and the clock.

A snapshot is used only when all of these hold, and is otherwise ignored and the
window scanned as before:

* **Same code and config.** The fingerprint covers the reducers, the normaliser,
  the scan, this module, the optional `analyze/` helpers they import, and every
  config key outside `server`. Changing how a record is counted must never
  resume a reduction counted the old way.
* **Same files.** Every file it read is still there, the same inode with the
  same first 4 KB, at least as long, with a line ending at the saved offset.
* **Inside the window's drift budget** (`panels.max_drift_pct`), measured from
  when it was built. An older one would be re-scanned the moment it caught up,
  so catching it up would only add to the cost.

A resumed view keeps its original `built_at`, so its age and its drift read as
they would had the service never stopped, and the window status says it was
restored.

Pickle runs code for whoever can write the file, so the directory is made 0700,
files are written 0600, and a file that is a symlink, not owned by this uid, or
writable by anyone else is never loaded.
"""
import collections
import glob
import hashlib
import json
import os
import pickle
import sys
import time

FORMAT = 1
PKG = os.path.dirname(os.path.abspath(__file__))
# Everything that decides what a reducer's state means.
CODE_FILES = ("normalize.py", "export.py", "windows.py", "snapshots.py")
HELPER_MODULES = ("agent_lib", "tool_bucket_lib", "purpose_lib", "deep_traj_lib")


def code_digest():
    """The source of everything that decides what a reducer's state means.

    Taken once, when the store is made: that is the code this process is
    running, whatever has been saved to the files since.
    """
    h = hashlib.sha256(b"format %d python %d.%d\n" % ((FORMAT,) + sys.version_info[:2]))
    paths = [os.path.join(PKG, f) for f in CODE_FILES]
    paths += sorted(glob.glob(os.path.join(PKG, "reducers", "*.py")))
    for name in HELPER_MODULES:
        f = getattr(sys.modules.get(name), "__file__", None)
        if f:
            paths.append(f)
    for p in paths:
        h.update(os.path.basename(p).encode() + b"\0")
        try:
            with open(p, "rb") as fh:
                h.update(fh.read())
        except OSError:
            h.update(b"(missing)")
    return h.hexdigest()


def fingerprint(code, cfg, roots):
    """16 hex chars naming the code, config and feed a saved state belongs to."""
    tree = {k: v for k, v in cfg.as_dict().items() if k != "server"}
    blob = json.dumps([code, tree, list(roots)], sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def capture(reducers):
    """Reducer state as plain picklable data, serialised.

    `cfg` stays behind -- the fingerprint already pins it -- and every
    defaultdict goes as a plain dict: several are built from lambdas, which
    pickle cannot name. `restore` pours the contents back into a fresh
    instance's own defaultdict, factory and all.

    A reference to another reducer in the set stays behind too, along with
    anything a reducer lists in `SNAPSHOT_SKIP` (derived caches). The process
    index is shared by the reducers that fold session keys, and the cascades
    read the trajectory reducer; pickled, each would come back as a private
    copy, and a resumed view would feed one object and read another. The
    fresh instances `restore` fills are already linked, so the links are kept.
    """
    ids = {id(r) for r in reducers.values()}
    state = {}
    for key, r in reducers.items():
        st = {}
        skip = set(getattr(r, "SNAPSHOT_SKIP", ()))
        for attr, v in vars(r).items():
            if attr == "cfg" or attr in skip or id(v) in ids:
                continue
            if isinstance(v, collections.defaultdict):
                v = dict(v)
            st[attr] = v
        state[key] = st
    return pickle.dumps(state, protocol=pickle.HIGHEST_PROTOCOL)


def restore(fresh, state):
    """Load `capture`d state into `fresh` reducers, in place."""
    if set(fresh) != set(state):
        raise ValueError("saved reducers %s, expected %s"
                         % (sorted(state), sorted(fresh)))
    for key, st in state.items():
        r = fresh[key]
        for attr, v in st.items():
            cur = getattr(r, attr, None)
            if isinstance(cur, collections.defaultdict):
                cur.clear()
                cur.update(v)
            else:
                setattr(r, attr, v)
    return fresh


class SnapshotStore:
    """One file per window, named by its minutes and the fingerprint.

    The fingerprint is taken per call, not once: the feed's roots are
    re-resolved while the service runs, and a snapshot of one root must never
    be resumed against another.
    """

    def __init__(self, directory, cfg, keep=8):
        self.dir = directory
        self.cfg = cfg
        self.keep = max(1, int(keep))
        self.code = code_digest()

    @classmethod
    def from_config(cls, cfg):
        """None when disabled, or when there is nowhere to put it."""
        if not cfg.get("panels.cache_windows", True):
            return None
        base = cfg.get("cache.dir")
        if not base:
            return None
        return cls(os.path.join(base, "windows"), cfg, cfg.get("panels.cache_keep", 8))

    def path(self, minutes, roots):
        fp = fingerprint(self.code, self.cfg, roots)
        return os.path.join(self.dir, "w%d.%s.pkl" % (int(minutes), fp)), fp

    def load(self, minutes, roots, check):
        """`(meta, state, None)`, or `(None, None, reason)`.

        `check(meta)` returns a reason to refuse, or None; the state -- the
        expensive part -- is only unpickled once the cheap header has passed.
        """
        path, fp = self.path(minutes, roots)
        try:
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except FileNotFoundError:
            other = glob.glob(os.path.join(self.dir, "w%d.*.pkl" % int(minutes)))
            return None, None, ("the saved build was made by different code, config "
                                "or feed roots" if other
                                else "no saved build for this window")
        with os.fdopen(fd, "rb") as fh:
            st = os.fstat(fh.fileno())
            if st.st_uid != os.getuid() or st.st_mode & 0o022:
                return None, None, "snapshot file is not private to this user; ignored"
            meta = pickle.load(fh)
            if meta.get("format") != FORMAT or meta.get("fingerprint") != fp \
                    or meta.get("minutes") != int(minutes):
                return None, None, "saved by a different code or config"
            why = check(meta)
            if why:
                return None, None, why
            return meta, pickle.load(fh), None

    def save(self, minutes, roots, meta, blob):
        """Write atomically: a reader sees the old file or the new one, whole."""
        os.makedirs(self.dir, mode=0o700, exist_ok=True)
        dest, fp = self.path(minutes, roots)
        meta = dict(meta, format=FORMAT, fingerprint=fp, minutes=int(minutes),
                    saved_at=time.time())
        tmp = "%s.%d.tmp" % (dest, os.getpid())
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "wb") as fh:
                pickle.dump(meta, fh, protocol=pickle.HIGHEST_PROTOCOL)
                fh.write(blob)
            os.replace(tmp, dest)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        self.prune()
        return meta

    def discard(self, minutes, roots):
        try:
            os.unlink(self.path(minutes, roots)[0])
        except OSError:
            pass

    def prune(self):
        """Keep the newest `keep` snapshots; any fingerprint, any window."""
        try:
            names = [os.path.join(self.dir, n) for n in os.listdir(self.dir)
                     if n.startswith("w") and n.endswith(".pkl")]
            names.sort(key=os.path.getmtime, reverse=True)
        except OSError:
            return
        for p in names[self.keep:]:
            try:
                os.unlink(p)
            except OSError:
                pass
