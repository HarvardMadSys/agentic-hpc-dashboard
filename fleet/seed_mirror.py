#!/usr/bin/env python3
"""seed_mirror: build a fleet_stream receiver's copy ahead of time from pulled `.gz` days.

A new receiver first fetches every sealed day its sources still hold, as the
collector's `.gz`, and only then streams the live day. This builds the sealed part of
the copy from `.gz` files pulled another way (pull-gz.sh, or any copy kept in the
same `<host>/<day>/` layout). Each `PULL/<host>/<day>/<name>.gz`, of either of the
collector's formats (`<name>` is `<host>.<stream>.jsonl` or `.ebr`), is copied as it is
to `BASE/mirror/<host>/<day>/<name>.gz`, with zlib checking every member's CRC and
size on the way as the receiver does, and listed in `BASE/state/<host>.verified.json`.
A receiver started on BASE tells each source that those days are verified, and the
source sends only what is missing: the live day's plain bytes, and any sealed day newer
than the pull.

    seed_mirror.py PULL BASE [--hosts HOST ...] [--inflate] [--free-min-gb GB]

- The receiver counts a listed `.gz` as held whatever its `keep_gz` says. Set
  `"keep_gz": true` in its config so the days that seal later stay `.gz` too.
- `--inflate` writes the plain `<name>` instead, as a receiver without `keep_gz` does:
  about 19 times the disk for JSONL, but the dashboard reads plain files with no index
  in memory.

- Run it before the receiver starts, or with the receiver stopped: it refuses a BASE
  whose status a receiver wrote in the last 5 minutes.
- BASE must be this account's folder with mode 700, as the receiver requires; a
  missing one is made so.
- Days go newest first, across hosts, and seeding stops before free space falls under
  the receiver's own floor (300 GB, or 10% of the volume if more), below which it
  pauses every link. The days left over, the oldest, the stream fetches. Give
  `--free-min-gb` the receiver's `free_min_gb` when its config sets one.
- A `.gz` that does not inflate (cut short while it was pulled, say) is left out, so
  the stream fetches that day; the exit status is then 1.
- A plain file a receiver began for a day the pull holds sealed is replaced by the
  `.gz`: the dashboard reads a plain file before its `.gz` twin.
- Run it again after another pull: listed files already in the copy are kept as they
  are, in either form.
- The receiver keeps only its "days" newest day folders per host, so "days" must cover
  the seeded days plus the live one; the last line says how many.
"""
import argparse
import json
import os
import sys
import time
import zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fleet_stream as fs   # noqa: E402

RECENT_STATUS_S = 300       # a receiver writes state/status.json every 10 s


class _Stop(Exception):
    """Seeding more would leave the receiver too little space."""


def _say(line):
    print(line, flush=True)


def _status_age(basedir):
    """Seconds since a receiver last wrote BASE/state/status.json, or None."""
    try:
        with open(os.path.join(basedir, "state", "status.json")) as fh:
            at = float(json.load(fh).get("at_epoch") or 0)
    except (OSError, ValueError, AttributeError, TypeError):
        return None
    return time.time() - at


def _pulled(pull, hosts):
    """{day: {host: [plain names]}} for every `.jsonl.gz` and `.ebr.gz` in the pull."""
    found = {}
    for host in hosts:
        top = os.path.join(pull, host)
        if not os.path.isdir(top):
            continue
        for day in fs.list_days(top):
            folder = os.path.join(top, day)
            names = [n[:-len(fs.GZ)] for n in sorted(os.listdir(folder))
                     if n.endswith(fs.GZ) and n[:-len(fs.GZ)].endswith(fs.SUFFIXES)
                     and os.path.isfile(os.path.join(folder, n))]
            if names:
                found.setdefault(day, {})[host] = names
    return found


class _Tee:
    """Reads of a file, each also appended to `fd`: a copy made while zlib checks it."""

    def __init__(self, fh, fd, what):
        self.fh, self.fd, self.what, self.n = fh, fd, what, 0

    def read(self, size):
        b = self.fh.read(size)
        if b:
            fs._append_at(os.write, self.fd, self.n, b, self.what)
            self.n += len(b)
        return b


def _copy_gz(base, mirror, day, name, gz_path):
    """Copy one pulled `.gz` into the copy as it is, checked as the receiver checks a
    fetched one, and put it in place as the receiver does; returns (bytes written,
    plain bytes it holds)."""
    tmp = mirror.gz_tmp(day, name)
    base.unlink(tmp)
    wfd = base.open_new(tmp)
    tee, n = None, 0
    try:
        with open(gz_path, "rb") as fh:
            tee = _Tee(fh, wfd, tmp)
            for piece in fs.inflate_pieces(tee):
                n += len(piece)
    except BaseException:
        os.close(wfd)
        base.unlink(tmp)
        raise
    os.close(wfd)
    mirror.place_gz(day, name, tmp)
    return tee.n, n


def _inflate_into(base, mirror, day, name, gz_path):
    """Inflate one pulled `.gz` into the copy, as the receiver's Mirror._finish_gz
    does with a fetched one; returns (bytes written, plain bytes), the same here."""
    tmp, rel = mirror.tmp(day, name), mirror.rel(day, name)
    base.unlink(tmp)
    wfd = base.open_new(tmp)
    n = 0
    try:
        with open(gz_path, "rb") as fh:
            for piece in fs.inflate_pieces(fh):
                fs._append_at(os.write, wfd, n, piece, tmp)
                n += len(piece)
    except BaseException:
        os.close(wfd)
        base.unlink(tmp)
        raise
    os.close(wfd)
    base.rename(tmp, rel)
    mirror.mark_verified("%s/%s" % (day, name))
    return n, n


def seed(pull, basedir, hosts=None, statvfs=None, say=_say, inflate=False, free_min_gb=None):
    """Build the copy. Returns the exit status: 0 done, 1 something was left for the
    stream (a `.gz` that did not inflate, or too little free space), 2 refused."""
    pull, basedir = os.path.abspath(pull), os.path.abspath(basedir)
    try:
        if hosts is None:
            hosts = sorted(h for h in os.listdir(pull)
                           if fs.NAME_RE.match(h) and os.path.isdir(os.path.join(pull, h)))
        found = _pulled(pull, hosts)
    except OSError as e:
        say("cannot read %s: %s" % (e.filename or pull, e.strerror))
        return 2
    bad = [h for h in hosts if not fs.NAME_RE.match(h)]
    if bad or not hosts:
        say("no host folders to seed from in %s%s" % (pull, (": bad names " + ", ".join(bad)) if bad else ""))
        return 2
    age = _status_age(basedir)
    if age is not None and age < RECENT_STATUS_S:
        say("a receiver wrote %s/state/status.json %d s ago: stop it before seeding its copy"
            % (basedir, max(0, age)))
        return 2
    if not os.path.lexists(basedir):
        os.makedirs(basedir, mode=0o700)
        os.chmod(basedir, 0o700)
    try:
        base = fs.SafeBase.open(basedir)
    except fs.UnsafeBase as e:
        say("refusing %s, as the receiver would: %s" % (basedir, e))
        return 2

    free_of = statvfs or base.free_bytes
    mirrors, counts = {}, {h: {"new": 0, "kept": 0, "bytes": 0, "failed": 0} for h in hosts}
    rc, stop = 0, None
    try:
        for day in sorted(found, reverse=True):
            for host in sorted(found[day]):
                if host not in mirrors:
                    mirrors[host] = fs.Mirror(base, host)
                m, c = mirrors[host], counts[host]
                have = set(base.listdir("mirror/%s/%s" % (host, day)))
                for name in found[day][host]:
                    key = "%s/%s" % (day, name)
                    if not fs.KEY_RE.match(key):
                        say("  %s %s: not a name the receiver takes; skipped" % (host, key))
                        continue
                    if key in m.verified and (name in have or name + fs.GZ in have):
                        c["kept"] += 1
                        continue
                    free, total = free_of()
                    floor = fs.free_floor({"free_min_gb": free_min_gb}, total)
                    if free < floor:
                        stop = ("stopped: %.0f GB free of %.0f GB, and the receiver pauses every link "
                                "below %.0f GB. The days not seeded, the oldest, the stream fetches."
                                % (free / 1e9, total / 1e9, floor / 1e9))
                        raise _Stop()
                    gz = os.path.join(pull, host, day, name + fs.GZ)
                    try:
                        wrote, n = (_inflate_into if inflate else _copy_gz)(base, m, day, name, gz)
                    except fs.DiskFull as e:
                        stop = "stopped: %s. The days not seeded, the oldest, the stream fetches." % e
                        raise _Stop()
                    except (fs.ProtocolError, zlib.error, OSError) as e:
                        c["failed"] += 1
                        rc = 1
                        say("  %s %s: did not inflate (%s); left for the stream" % (host, key, e))
                        continue
                    c["new"] += 1
                    c["bytes"] += wrote
                    say("  %s %s: %.2f GB%s" % (host, key, wrote / 1e9,
                                                "" if inflate else " (%.2f GB plain)" % (n / 1e9)))
    except _Stop:
        say(stop)
        rc = 1
    need = 1
    for host in hosts:
        c = counts[host]
        days = mirrors[host].days() if host in mirrors else []
        need = max(need, len(days) + 1)
        span = "%s..%s" % (days[0], days[-1]) if days else "none"
        say("%s: %d new, %d already in the copy, %d left for the stream; days %s; %.1f GB written"
            % (host, c["new"], c["kept"], c["failed"], span, c["bytes"] / 1e9))
    base.close()
    say('Set the receiver\'s "days" to at least %d (HISTORY_DAYS to at least %d in site.env): '
        'it keeps only that many newest day folders per host.' % (need, need - 1))
    if not inflate:
        say('Set "keep_gz": true in its config as well, so the days that seal from now on '
            'stay .gz too.')
    return rc


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 epilog="See the module docstring for the details.")
    ap.add_argument("pull", help="the pulled days: PULL/<host>/<day>/<name>.jsonl.gz "
                                 "and <name>.ebr.gz")
    ap.add_argument("base", help="the receiver's base folder (its config's \"base\")")
    ap.add_argument("--hosts", nargs="+", help="only these hosts (default: every folder in PULL)")
    ap.add_argument("--inflate", action="store_true",
                    help="write plain files, as a receiver without keep_gz does")
    ap.add_argument("--free-min-gb", type=float, default=None,
                    help="the receiver's free_min_gb, if its config sets one (300)")
    a = ap.parse_args(argv)
    os.umask(0o077)
    return seed(a.pull, a.base, a.hosts, inflate=a.inflate, free_min_gb=a.free_min_gb)


if __name__ == "__main__":
    sys.exit(main())
