"""Resolve every feed, and report what was looked for as well as what was found.

One module owns this so the UI's empty states, the Feeds tab and `--check-feeds`
cannot disagree about a path.  A panel whose feed is absent renders the
`configured` list verbatim, so "no data from <path>" always names a real string
the operator can act on.

`newest_record_ts` and the status vocabulary are ported from
collect/healthcheck.py (check_target / last_timestamp), not imported: that module
is a CLI with module-level BASE and _default_nodes() side effects, and the
dashboard must not take a dependency on collect/.
"""
import json
import os
import time
from datetime import datetime

from . import gz
from .normalize import epoch_of
from .feedfile import dated_dirs, feed_files, host_of

TS_FORMAT = "%Y-%m-%d %H:%M:%S"     # every collector stamps records this way

# The two tiers the collector writes, both `<root>/<YYYY-MM-DD>/<host>...` day-files.
TIER = {
    "ebpf": ("eBPF / proc-trace", "ebpfm exit/residency/tcp/submit", "event + 60 s"),
    "ebpf_node": ("eBPF node tier", "node_snapshot: load, memory, NFS, per-user cgroup", "300 s"),
}
ENV_OF = {"ebpf": "RC_DASH_EBPF_ROOTS", "ebpf_node": "RC_DASH_NODE_ROOTS"}
CLI_OF = {"ebpf": "--ebpf-root", "ebpf_node": "--node-root"}
FEEDS = tuple(TIER)


def newest_record_ts(path, ts_field="ts"):
    """`(ts, epoch)` of the newest record, read from the tail: its `ts` as the
    collector wrote it, in its node's time zone, and its epoch
    (`normalize.epoch_of`), which says how old it is wherever it is read.
    None if nothing parses.

    The window grows until it holds a complete line: some collectors write one
    large JSON object per line (a census record is ~200 KB), so a fixed small
    tail would only ever see a mid-line fragment.
    """
    try:
        size = gz.stat(path).st_size
    except OSError:
        return None
    window = 65536
    while True:
        start = max(0, size - window)
        try:
            with gz.open_feed(path) as fh:
                fh.seek(start)
                data = fh.read(size - start)
        except OSError:
            return None
        lines = data.decode("utf-8", "replace").splitlines()
        if start > 0 and lines:
            lines = lines[1:]           # window cut mid-line: drop the fragment
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue                # half-written final line: try the one before
            if not isinstance(rec, dict):
                continue                # JSON, but no record (`0`, `null`, `[]`)
            ts = rec.get(ts_field) or rec.get("timestamp")
            if ts:
                try:
                    return datetime.strptime(ts, TS_FORMAT), epoch_of(rec)
                except (TypeError, ValueError):     # a `ts` that is not a string, too
                    return None
        if start == 0 or window > 256 * 1024 * 1024:
            return None
        window *= 4


def fmt_age(age):
    if age is None:
        return "-"
    age = int(age)
    if age < 90:
        return "%ds" % age
    if age < 5400:
        return "%dm%02ds" % (age // 60, age % 60)
    return "%dh%02dm" % (age // 3600, (age % 3600) // 60)


def _blank(name, cfg):
    tier, label, cadence = TIER[name]
    return {"name": name, "tier": tier, "label": label, "cadence": cadence,
            "configured": [], "resolved": [], "missing": [], "origin": "default",
            "env": ENV_OF.get(name), "cli": CLI_OF.get(name),
            "exists": False, "status": "missing",
            "required": bool(cfg.get("feeds.%s.required" % name, False)),
            "n_files": 0, "bytes": 0, "days": [], "hosts": [], "files": [],
            "newest_file": None, "newest_mtime": None, "newest_record_ts": None,
            "lag_s": None, "lag": None, "max_age_s": cfg.get("feeds.%s.max_age_s" % name),
            "rows": 0, "schema_versions": {}, "notice": None,
            "backfill": None}


def resolve_dated(name, cfg):
    """`<root>/<YYYY-MM-DD>/<host>[.<stream>].jsonl[.gz]` -- the ebpf and node tiers."""
    rep = _blank(name, cfg)
    roots = cfg.get("feeds.%s.roots" % name, []) or []
    days = int(cfg.get("feeds.%s.days" % name, 2) or 2)
    rep["configured"] = list(roots)
    rep["origin"] = cfg.origin("feeds.%s.roots" % name)
    rep["resolved"] = [r for r in roots if os.path.isdir(r)]
    rep["missing"] = [r for r in roots if not os.path.isdir(r)]
    if not rep["resolved"]:
        return rep
    rep["exists"] = True

    files, dates = [], set()
    for root in rep["resolved"]:
        for d, found in dated_dirs(root, days):
            dates.add(d)
            files += found
        # a root may also hold flat <thing>_<date>.jsonl files
        files += feed_files(root)[-days:]
    rep["days"] = sorted(dates)[-days:]
    for p in sorted(set(files)):
        try:
            st = os.stat(p)
        except OSError:
            continue
        rep["files"].append({"path": p, "size": st.st_size, "mtime": st.st_mtime,
                             "host": host_of(p), "compressed": gz.is_gz(p)})
        rep["bytes"] += st.st_size
    rep["n_files"] = len(rep["files"])
    rep["hosts"] = sorted({f["host"] for f in rep["files"]})
    read = rep["files"]
    if any(f["compressed"] for f in rep["files"]):
        # `size`/`bytes` stay what is on disk; what it holds is here
        rep["compressed"] = gz.status()
    if not read:
        rep["status"] = "empty"
        return rep

    # On a tie the later path: two hosts' files written in one round often
    # share an mtime tick, and the first of them would be an accident of
    # iteration.
    newest = max(read, key=lambda f: (f["mtime"], f["path"]))
    rep["newest_file"] = newest["path"]
    rep["newest_mtime"] = datetime.fromtimestamp(newest["mtime"]).strftime(TS_FORMAT)
    got = newest_record_ts(newest["path"])
    if got is not None:
        ts, epoch = got
        rep["newest_record_ts"] = ts.strftime(TS_FORMAT)
        if epoch is not None:
            # From the record's epoch, not its `ts`: that is the node's local time,
            # and read in another zone it was hours off. time.time(), not
            # datetime.now(): a pinned clock (`clock`) covers it. A record stamped
            # ahead of this clock (the node's runs fast) is no lag at all.
            rep["lag_s"] = max(0.0, time.time() - epoch)
            rep["lag"] = fmt_age(rep["lag_s"])
    max_age = rep["max_age_s"]
    rep["status"] = ("stale" if (max_age and rep["lag_s"] is not None
                                 and rep["lag_s"] > max_age) else "ok")
    return rep


_NOTICE = {
    "missing": ("No %s feed. Looked in %d location(s); none exist. "
                "Set %s (or %s) to the collector's output root."),
    "empty": "The %s feed resolved to %s but holds no matching files yet.",
    "stale": "The %s feed's newest record is %s old (threshold %s).",
}


def _notice(rep):
    if rep["status"] == "ok":
        return None
    if rep["status"] == "missing":
        return _NOTICE["missing"] % (rep["name"], len(rep["configured"]),
                                     rep["env"] or "the config file",
                                     rep["cli"] or "feeds.%s" % rep["name"])
    if rep["status"] == "empty":
        return _NOTICE["empty"] % (rep["name"], ", ".join(rep["resolved"]))
    if rep["status"] == "stale":
        return _NOTICE["stale"] % (rep["name"], rep["lag"], fmt_age(rep["max_age_s"]))
    return None


def resolve(cfg):
    """{feed_name: report}.  Never raises; a broken feed becomes status='missing'."""
    out = {}
    for name in FEEDS:
        try:
            rep = resolve_dated(name, cfg)
        except Exception as e:                       # a feed must never break the page
            rep = _blank(name, cfg)
            rep["status"] = "missing"
            rep["notice"] = "%s feed could not be resolved: %s" % (name, e)
            out[name] = rep
            continue
        rep["notice"] = _notice(rep)
        out[name] = rep
    return out


def panel_meta(rep):
    """The block every panel carries so one renderer handles every empty state."""
    return {"_feed": rep["name"], "_status": rep["status"],
            "_present": rep["status"] in ("ok", "stale"),
            "_paths": rep["configured"], "_notice": rep["notice"],
            "_env": rep["env"], "_cli": rep["cli"]}


def print_table(reports):
    """`--check-feeds`, shaped like collect/healthcheck.py's so operators can read it."""
    print("%-11s %-8s %6s %-21s %7s  %s"
          % ("tier", "status", "files", "newest record", "lag", "path"))
    bad = 0
    for r in reports.values():
        if r["required"] and r["status"] in ("missing", "empty"):
            bad += 1
        path = (r["resolved"] or [None])[0]
        if path is None:
            path = "(%d candidate(s), none exist)" % len(r["configured"])
        print("%-11s %-8s %6d %-21s %7s  %s"
              % (r["name"], r["status"].upper(), r["n_files"],
                 r["newest_record_ts"] or r["newest_mtime"] or "-",
                 r["lag"] or "-", path))
    for r in reports.values():
        if r["notice"]:
            print("  ! %s" % r["notice"])
    return 1 if bad else 0
