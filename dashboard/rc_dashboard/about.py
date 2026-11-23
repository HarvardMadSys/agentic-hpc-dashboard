"""What is running, and where: the version and the commit it was built from, the
time zone the service formats times in, and its host's name.

`rc-dashboard --version` (and `version`) prints the first; /api/config carries
all three under `_meta` (config.Config.as_dict), so the page can say which build
it talks to, format every time in the service's zone rather than the browser's,
and name the host when `site.name` is empty.

The commit comes from, in order:

  build    BUILD.json beside this file, {"commit": "abc1234"}, which an image or
           a wheel is stamped with when it is built (docker/dashboard/Dockerfile,
           .github/workflows/dashboard-image.yml); never in git
  git      `git rev-parse --short HEAD` of the checkout this file is in -- only
           when that work tree's top is this repository, never a repository
           that merely contains it (a release unpacked under a home in git)
  package  neither: the package's version alone, and the commit is null
"""
import functools
import json
import os
import re
import socket
import subprocess
import time

from . import __version__

PKG = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(PKG))
STAMP = os.path.join(PKG, "BUILD.json")
_HEX = re.compile(r"[0-9a-f]{4,40}")


def _stamp():
    """The commit a build stamped into the package, or None."""
    try:
        with open(STAMP) as fh:
            commit = str(json.load(fh).get("commit") or "").strip().lower()
    except (OSError, ValueError, AttributeError):
        return None
    if not _HEX.fullmatch(commit):
        return None
    return commit[:7] if len(commit) > 12 else commit


def _git():
    """The checkout's HEAD, short, or None: not a checkout, no git, or a work
    tree whose top is not this repository."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["LC_ALL"] = "C"
    try:
        p = subprocess.run(["git", "-C", PKG, "rev-parse", "--show-toplevel", "--short", "HEAD"],
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env,
                           universal_newlines=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    lines = p.stdout.split()
    if p.returncode or len(lines) != 2:
        return None
    top, commit = lines
    try:
        if os.path.realpath(top) != os.path.realpath(REPO):
            return None
    except OSError:
        return None
    return commit if _HEX.fullmatch(commit) else None


@functools.lru_cache(maxsize=1)
def version():
    """{"version", "commit", "origin"}: the package's version, the commit it was
    built from (or None), and where that came from (`build`, `git`, `package`)."""
    commit = _stamp()
    if commit is not None:
        return {"version": __version__, "commit": commit, "origin": "build"}
    commit = _git()
    if commit is not None:
        return {"version": __version__, "commit": commit, "origin": "git"}
    return {"version": __version__, "commit": None, "origin": "package"}


def version_line():
    """`rc-dashboard 0.1.0 (abc1234)`, or without the commit when none is known."""
    v = version()
    return "rc-dashboard %s%s" % (v["version"], " (%s)" % v["commit"] if v["commit"] else "")


# -------------------------------------------------------------- the time zone
def _valid_zone(name):
    """`name` when it is an IANA zone this host knows, else None."""
    if not name or name.startswith("/") or ".." in name.split("/"):
        return None
    try:
        import zoneinfo
        zoneinfo.ZoneInfo(name)
    except Exception:           # ZoneInfoNotFoundError, ValueError, an unreadable file
        return None
    return name


def _after_zoneinfo(path):
    """The zone a path under a zoneinfo folder names: `.../zoneinfo/America/New_York`
    is `America/New_York`."""
    i = path.rfind("zoneinfo/")
    return path[i + len("zoneinfo/"):] if i >= 0 else None


@functools.lru_cache(maxsize=4)
def zone_name(tz=None, localtime="/etc/localtime"):
    """The IANA name of the zone this process formats times in, or None: TZ when
    set (`:America/New_York` and a path under a zoneinfo folder too; a POSIX rule
    such as `EST5EDT,M3.2.0,M11.1.0` names no zone), else what /etc/localtime
    links to under a zoneinfo folder. Read once."""
    if tz is not None:
        raw = tz.lstrip(":").strip()
        if not raw:
            return "UTC"                    # TZ set empty: the C library's UTC
        if raw.startswith("/"):
            raw = _after_zoneinfo(raw) or ""
        return _valid_zone(raw)
    try:
        target = os.readlink(localtime)
    except OSError:
        return None
    return _valid_zone(_after_zoneinfo(target) or "")


def zone(now=None):
    """{"name", "abbrev", "utc_offset_s"} of the service's zone at `now` (the
    service's present: a pinned clock's, clock.py): what the page formats every
    time in."""
    lt = time.localtime(time.time() if now is None else now)
    return {"name": zone_name(os.environ.get("TZ")), "abbrev": lt.tm_zone,
            "utc_offset_s": lt.tm_gmtoff}


def host():
    """This host's short name."""
    try:
        name = socket.gethostname()
    except OSError:
        return None
    return name.split(".")[0] or None
