"""`rc-dashboard backup --to FILE.tar` and `restore --from FILE.tar`: the state
that cannot be rebuilt.

Nearly everything the service keeps is rebuilt from the collector's day-files
-- the saved window builds and hours, the panels, the history store's Parquet,
the reverse-DNS names -- and is deliberately left out. What is not:

  rollup.sqlite    the Trend tab's roll-up table (`rollup.dir`, else
                   <cache.dir>/rollup/): the only copy of a day whose day-files
                   are gone. Copied with SQLite's online backup, so a service
                   writing it meanwhile is fine
  anonymize.salt   <cache.dir>/anonymize.salt: a new one gives every name a new
                   alias (when `anonymize.salt` is set in the config instead, the
                   config holds it)
  session.key      <cache.dir>/session.key: a new one signs everyone out
  config/<name>    the config file the service reads, if it reads one
  password.hash    `server.auth.password_hash_file`, if set
  ask-done.json    <cache.dir>/ask/done.json: Ask's suggestions marked done

Each is in the tar under that name, with MANIFEST.json saying where it was, its
mode, size and SHA-256. The tar is written 0600 beside its name and renamed into
place: it holds the salt and the session key.

`restore` puts the config file back first -- to --config (or RC_DASH_CONFIG),
else where it was -- then reads that config, with this environment's variables
and flags, for where the rest goes now: a backup made on one host restores onto
another's layout. A file already there and different is replaced only with
--force; nothing is written until every target is known to be free. Stop the
service first: it holds the table open, and keeps the salt and key it read.
"""
import datetime
import hashlib
import io
import json
import os
import sqlite3
import tarfile
import tempfile
import time

from . import about
from . import config as configmod

FORMAT = 1
MANIFEST = "MANIFEST.json"
NOT_INCLUDED = ("the saved window builds, hours and panels (<cache.dir>/windows, hours, "
                "payloads), the history store's Parquet (history.dir) and the reverse-DNS "
                "names (rdns.json): each is rebuilt from the collector's day-files or by "
                "asking again")


class BackupError(Exception):
    pass


def _cache(cfg):
    return cfg.get("cache.dir") or "."


def targets(cfg, manifest=None):
    """{role: (name in the tar, path)} of everything that may be backed up, where
    `cfg` puts it. The hash file falls back to where the backup found it."""
    from . import auth, rollup
    cache = _cache(cfg)
    out = {"rollup": ("rollup.sqlite", rollup.path_of(cfg)),
           "session_key": ("session.key", os.path.join(cache, "session.key")),
           "ask_done": ("ask-done.json", os.path.join(cache, "ask", "done.json"))}
    if not cfg.get("anonymize.salt"):
        out["salt"] = ("anonymize.salt", os.path.join(cache, "anonymize.salt"))
    try:
        _stored, hash_file = auth.password_settings(cfg)
    except SystemExit:
        hash_file = ""
    if not hash_file and manifest is not None:
        was = [f for f in manifest.get("files", []) if f.get("role") == "hash_file"]
        hash_file = was[0]["path"] if was else ""
    if hash_file:
        out["hash_file"] = ("password.hash", os.path.abspath(hash_file))
    return out


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _snapshot_sqlite(path, into):
    """A consistent copy of SQLite database `path` at `into`, with its online
    backup: a writer meanwhile waits a moment, and is never seen half done."""
    src = sqlite3.connect(path, timeout=30)
    try:
        dst = sqlite3.connect(into)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def backup(cfg, to, force=False, out=None):
    """Write the tar. Returns 0."""
    out = out or _stdout()
    to = os.path.abspath(os.path.expanduser(to))
    # an empty file is a place made for it (`mktemp`), with nothing to lose
    if os.path.exists(to) and not force and not (os.path.isfile(to) and os.path.getsize(to) == 0):
        raise BackupError("%s already exists; pass --force to replace it" % to)
    files = []
    members = []                        # (name, bytes)
    with tempfile.TemporaryDirectory(prefix="rc-backup-") as work:
        for role, (name, path) in targets(cfg).items():
            if not os.path.isfile(path):
                continue
            if role == "rollup":
                snap = os.path.join(work, name)
                try:
                    _snapshot_sqlite(path, snap)
                except sqlite3.Error as e:
                    raise BackupError("the roll-up table %s: %s" % (path, e))
                with open(snap, "rb") as fh:
                    data = fh.read()
            else:
                with open(path, "rb") as fh:
                    data = fh.read()
            files.append({"name": name, "role": role, "path": path,
                          "mode": "%04o" % (os.stat(path).st_mode & 0o7777),
                          "size": len(data), "sha256": _sha(data)})
            members.append((name, data))
        if cfg.config_file and os.path.isfile(cfg.config_file):
            path = os.path.abspath(cfg.config_file)
            with open(path, "rb") as fh:
                data = fh.read()
            name = "config/" + os.path.basename(path)
            files.append({"name": name, "role": "config", "path": path,
                          "mode": "%04o" % (os.stat(path).st_mode & 0o7777),
                          "size": len(data), "sha256": _sha(data)})
            members.append((name, data))
        manifest = {"format": FORMAT, "made_at": datetime.datetime.now().isoformat(" ", "seconds"),
                    "made_by": about.version_line(), "host": about.host(), "files": files,
                    "not_included": NOT_INCLUDED}
        d = os.path.dirname(to)
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-", suffix=".tar")    # 0600
        try:
            with os.fdopen(fd, "wb") as fh, tarfile.open(fileobj=fh, mode="w") as tar:
                _add(tar, MANIFEST, json.dumps(manifest, indent=1).encode() + b"\n")
                for name, data in members:
                    _add(tar, name, data)
            os.replace(tmp, to)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
    print("Wrote %s (mode 600):" % to, file=out)
    for f in files:
        print("  %-22s %8d bytes  from %s" % (f["name"], f["size"], f["path"]), file=out)
    if not files:
        print("  nothing: none of the state it keeps is there yet", file=out)
    print("Left out, as rebuilt: %s." % NOT_INCLUDED, file=out)
    return 0


def _add(tar, name, data):
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mode = 0o600
    info.mtime = int(time.time())
    tar.addfile(info, io.BytesIO(data))


def _read(src):
    """{name: bytes} and the manifest of backup tar `src`, every member checked
    against it."""
    src = os.path.abspath(os.path.expanduser(src))
    try:
        tar = tarfile.open(src, mode="r")
    except (OSError, tarfile.TarError) as e:
        raise BackupError("%s: %s" % (src, e))
    with tar:
        data = {}
        for m in tar.getmembers():
            if not m.isfile() or m.name.startswith("/") or ".." in m.name.split("/"):
                raise BackupError("%s holds %s, which no backup holds" % (src, m.name))
            data[m.name] = tar.extractfile(m).read()
    try:
        manifest = json.loads(data.pop(MANIFEST))
    except (KeyError, ValueError):
        raise BackupError("%s has no %s: not a backup rc-dashboard wrote" % (src, MANIFEST))
    if manifest.get("format") != FORMAT:
        raise BackupError("%s is a backup of format %r; this rc-dashboard reads %d"
                          % (src, manifest.get("format"), FORMAT))
    for f in manifest.get("files", []):
        got = data.get(f.get("name"))
        if got is None or _sha(got) != f.get("sha256"):
            raise BackupError("%s: %s is missing or not what was backed up"
                              % (src, f.get("name")))
    return data, manifest


def _write(path, data, mode):
    """`data` to `path` in one step, `mode`; folders made on the way are 0700."""
    d = os.path.dirname(path)
    os.makedirs(d, mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _restore_sqlite(path, blob, mode):
    """The database in `blob` into `path` with SQLite's own backup, so the table's
    write-ahead log beside it is taken care of rather than left to be replayed
    over the restored copy."""
    d = os.path.dirname(path)
    os.makedirs(d, mode=0o700, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="rc-restore-") as work:
        snap = os.path.join(work, "rollup.sqlite")
        with open(snap, "wb") as fh:
            fh.write(blob)
        src = sqlite3.connect(snap)
        try:
            dst = sqlite3.connect(path, timeout=30)
            try:
                src.backup(dst)
            finally:
                dst.close()
        except sqlite3.Error as e:
            raise BackupError("the roll-up table %s: %s" % (path, e))
        finally:
            src.close()
    os.chmod(path, mode)


def restore(args, src, force=False, out=None):
    """Put a backup back (see the module's doc). Returns 0."""
    out = out or _stdout()
    data, manifest = _read(src)
    by_role = {f["role"]: f for f in manifest.get("files", [])}
    plan = []                           # (path, bytes, mode, what)
    cfg_entry = by_role.get("config")
    config_path = getattr(args, "config", None) or os.environ.get("RC_DASH_CONFIG") or None
    staged = None
    try:
        if cfg_entry is not None:
            config_path = os.path.abspath(os.path.expanduser(config_path or cfg_entry["path"]))
            # read from a copy beside where it goes: its relative paths resolve
            # as they will there (config._resolve_paths)
            os.makedirs(os.path.dirname(config_path), exist_ok=True)
            fd, staged = tempfile.mkstemp(dir=os.path.dirname(config_path), prefix=".tmp-",
                                          suffix=os.path.splitext(config_path)[1])
            with os.fdopen(fd, "wb") as fh:
                fh.write(data[cfg_entry["name"]])
            plan.append((config_path, data[cfg_entry["name"]], int(cfg_entry["mode"], 8),
                         "config"))
        cfg = configmod.load(args, staged or config_path)
        where = targets(cfg, manifest)
        rollup_path = where["rollup"][1]
        for role, f in by_role.items():
            if role == "config":
                continue
            if role not in where:
                print("  %s: left out, as this config keeps it elsewhere (%s)"
                      % (f["name"], "anonymize.salt" if role == "salt" else role), file=out)
                continue
            plan.append((where[role][1], data[f["name"]], int(f["mode"], 8), f["name"]))
        clash = []
        for path, blob, _mode, _what in plan:
            if os.path.exists(path):
                if path == rollup_path:
                    clash.append(path)          # a live database: never the same bytes
                    continue
                with open(path, "rb") as fh:
                    if fh.read() != blob:
                        clash.append(path)
        if clash and not force:
            raise BackupError("%s already there and different; pass --force to replace "
                              "%s" % (", ".join(clash), "them" if len(clash) > 1 else "it"))
        for path, blob, mode, what in plan:
            if path == rollup_path:
                _restore_sqlite(path, blob, mode)
            else:
                _write(path, blob, mode)
            print("  %-22s -> %s" % (what, path), file=out)
    finally:
        if staged and os.path.exists(staged):
            os.unlink(staged)
    print("Restored %d file(s) from %s, made %s by %s on %s." % (
        len(plan), os.path.abspath(src), manifest.get("made_at"), manifest.get("made_by"),
        manifest.get("host")), file=out)
    return 0


def _stdout():
    import sys
    return sys.stdout
