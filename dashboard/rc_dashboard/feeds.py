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
import glob
import json
import os
import time
from datetime import datetime

from . import ebr, gz
from .feedfile import dated_dirs, feed_files, format_of, host_of, newest, prefer_of
from .feedfile import _STREAM_SUFFIXES  # noqa: F401  (moved with host_of; importable as before)

TS_FORMAT = "%Y-%m-%d %H:%M:%S"     # every collector stamps records this way

TIER = {
    "ebpf": ("eBPF / proc-trace", "ebpfm exit/residency/tcp/submit", "event + 60 s"),
    "ebpf_node": ("eBPF node tier", "node_snapshot: load, memory, NFS, per-user cgroup", "300 s"),
    "sacct": ("sacct (slurmdbd export)", "settled accounting record per job", "on demand"),
    "sacctmgr": ("sacctmgr", "accounts, QOS, admin levels, federation", "daily"),
    "domains": ("mapping", "user -> science domain", "static"),
    "user_actor": ("dataclean", "user-level actor label (dense)", "static"),
}
ENV_OF = {
    "ebpf": "RC_DASH_EBPF_ROOTS", "ebpf_node": "RC_DASH_NODE_ROOTS",
    "sacct": "RC_DASH_SACCT_DIR", "sacctmgr": "RC_DASH_SACCTMGR_DIR",
    "domains": "RC_DASH_DOMAINS_CSV", "user_actor": "RC_DASH_USER_CSV",
}
CLI_OF = {
    "ebpf": "--ebpf-root", "ebpf_node": "--node-root", "sacct": "--sacct-dir",
}
FEEDS = tuple(TIER)


def newest_record_ts(path, ts_field="ts"):
    """Newest record's timestamp, read from the tail.  None if nothing parses.

    The window grows until it holds a complete line: some collectors write one
    large JSON object per line (a census record is ~200 KB), so a fixed small
    tail would only ever see a mid-line fragment. An EBR day-file's frames are
    decoded from its end instead, as far back (`feedfile.newest`).
    """
    if format_of(path) == "ebr":
        return _newest_ebr_ts(path, ts_field)
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
            ts = rec.get(ts_field) or rec.get("timestamp")
            if ts:
                try:
                    return datetime.strptime(ts, TS_FORMAT)
                except ValueError:
                    return None
        if start == 0 or window > 256 * 1024 * 1024:
            return None
        window *= 4


def _newest_ebr_ts(path, ts_field):
    try:
        ts = newest(path, lambda rec: rec.get(ts_field) or rec.get("timestamp") or None,
                    256 << 20)
    except (OSError, ebr.Error):
        return None
    if ts is None:
        return None
    try:
        return datetime.strptime(ts, TS_FORMAT)
    except (TypeError, ValueError):
        return None


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
            "rows": 0, "schema_versions": {}, "degraded": [], "notice": None,
            "backfill": None,
            # the dated tiers' two formats (`resolve_dated`)
            "prefer": None, "formats": {"read": {}, "unread": {}}, "twins": [],
            "held": [], "format_notice": None}


def resolve_dated(name, cfg):
    """`<root>/<YYYY-MM-DD>/<host>[.<stream>].<jsonl|ebr>` -- the ebpf and node tiers.

    Where a stream is written in both formats, the report says which twin is
    read and which not (`formats`, `twins`), why when it is not the preferred
    one, the streams held until a twin has a record (`held`, listed in
    `files` with `held` set), and `format_notice` when a stream is read from
    its non-preferred twin or the twin read lags the other by more than
    `max_age_s` -- which it reports, and never acts on (`feedfile.choose`)."""
    rep = _blank(name, cfg)
    roots = cfg.get("feeds.%s.roots" % name, []) or []
    days = int(cfg.get("feeds.%s.days" % name, 2) or 2)
    prefer = rep["prefer"] = prefer_of(cfg, name)
    rep["configured"] = list(roots)
    rep["origin"] = cfg.origin("feeds.%s.roots" % name)
    # Read through the EBR view of the collector's roots (ebrview.py): these
    # are the collector's own, the roots above the view's.
    src = cfg.get("feeds.%s.source_roots" % name)
    if src:
        rep["source_roots"] = list(src)
    rep["resolved"] = [r for r in roots if os.path.isdir(r)]
    rep["missing"] = [r for r in roots if not os.path.isdir(r)]
    if not rep["resolved"]:
        return rep
    rep["exists"] = True

    files, dates, notes = [], set(), []
    for root in rep["resolved"]:
        for d, found in dated_dirs(root, days, prefer, report=notes):
            dates.add(d)
            files += found
        # a root may also hold flat <thing>_<date>.jsonl files
        files += feed_files(root, prefer, report=notes)[-days:]
    rep["days"] = sorted(dates)[-days:]
    files = sorted(set(files))
    held = {p for n in notes if n["path"] is None for p in n["files"].values()}
    _twins(rep, notes)

    for p in files + sorted(held - set(files)):
        try:
            st = os.stat(p)
        except OSError:
            continue
        rep["files"].append({"path": p, "size": st.st_size, "mtime": st.st_mtime,
                             "host": host_of(p), "compressed": gz.is_gz(p),
                             "format": format_of(p), "held": p in held})
        rep["bytes"] += st.st_size
    rep["n_files"] = len(rep["files"])
    rep["hosts"] = sorted({f["host"] for f in rep["files"]})
    read = [f for f in rep["files"] if not f["held"]]
    for f in read:
        rep["formats"]["read"][f["format"]] = rep["formats"]["read"].get(f["format"], 0) + 1
    if any(f["compressed"] for f in rep["files"]):
        # `size`/`bytes` stay what is on disk; what it holds is here
        rep["compressed"] = gz.status()
    if not read:
        rep["status"] = "empty"
        return rep

    # On a tie the later path: two hosts' files written in one round often
    # share an mtime tick, and the first of them would be an accident of
    # iteration -- one twin's report naming one host, the other twin's another.
    newest = max(read, key=lambda f: (f["mtime"], f["path"]))
    rep["newest_file"] = newest["path"]
    rep["newest_mtime"] = datetime.fromtimestamp(newest["mtime"]).strftime(TS_FORMAT)
    _lagging(rep, notes)
    ts = newest_record_ts(newest["path"])
    if ts is not None:
        rep["newest_record_ts"] = ts.strftime(TS_FORMAT)
        # time.time(), not datetime.now(): a pinned clock (`clock`) covers it
        rep["lag_s"] = (datetime.fromtimestamp(time.time()) - ts).total_seconds()
        rep["lag"] = fmt_age(rep["lag_s"])
    max_age = rep["max_age_s"]
    rep["status"] = ("stale" if (max_age and rep["lag_s"] is not None
                                 and rep["lag_s"] > max_age) else "ok")
    return rep


def _twins(rep, notes):
    """`twins`, `held` and the unread counts from the listing's notes."""
    unread = rep["formats"]["unread"]
    for n in notes:
        if n["path"] is None:
            rep["held"].append({"day": n.get("day"), "stream": n["stream"],
                                "files": sorted(n["files"].values()), "reason": n["reason"]})
            continue
        others = sorted(f for f in n["files"] if f != n["read"])
        for f in others:
            unread[f] = unread.get(f, 0) + 1
        rep["twins"].append({"day": n.get("day"), "stream": n["stream"], "read": n["read"],
                             "unread": others[0] if len(others) == 1 else others,
                             "reason": n["reason"]})
    off = [t for t in rep["twins"] if t["read"] != rep["prefer"]]
    if off:
        rep["format_notice"] = (
            "%d stream(s) read from their %s twin, not the preferred %s: %s%s" % (
                len(off), off[0]["read"], rep["prefer"],
                "; ".join("%s%s: %s" % (t["stream"], " (%s)" % t["day"] if t["day"] else "",
                                         t["reason"]) for t in off[:3]),
                "; ..." if len(off) > 3 else ""))


def _lagging(rep, notes):
    """Add to `format_notice` the streams whose twin read was last written more
    than `max_age_s` before the twin left unread: one format's writer stopped."""
    max_age = rep["max_age_s"]
    if not max_age:
        return
    lag = []
    for n in notes:
        if n["path"] is None or len(n["files"]) < 2:
            continue
        try:
            mine = os.stat(n["path"]).st_mtime
            theirs = max(os.stat(p).st_mtime for f, p in n["files"].items() if f != n["read"])
        except OSError:
            continue
        if theirs - mine > max_age:
            lag.append("%s%s (%s, %s behind its %s twin)" % (
                n["stream"], " (%s)" % n["day"] if n.get("day") else "", n["read"],
                fmt_age(theirs - mine), "/".join(f for f in n["files"] if f != n["read"])))
    if lag:
        msg = "the twin read lags the other: %s%s" % ("; ".join(lag[:3]),
                                                      "; ..." if len(lag) > 3 else "")
        rep["format_notice"] = msg if not rep["format_notice"] else (
            rep["format_notice"] + ". " + msg[0].upper() + msg[1:])


def resolve_globbed(name, cfg):
    """A directory of files matching a glob -- sacct day-files, sacctmgr dumps."""
    rep = _blank(name, cfg)
    d = cfg.get("feeds.%s.dir" % name)
    pat = cfg.get("feeds.%s.pattern" % name, "*")
    rep["configured"] = [os.path.join(d, pat)] if d else []
    rep["origin"] = cfg.origin("feeds.%s.dir" % name)
    if not d or not os.path.isdir(d):
        rep["missing"] = rep["configured"]
        return rep
    rep["resolved"] = [d]
    rep["exists"] = True
    files = sorted(glob.glob(os.path.join(d, pat)))
    maxd = int(cfg.get("feeds.%s.max_days" % name, 0) or 0)
    if maxd > 0:
        files = files[-maxd:]
    for p in files:
        try:
            st = os.stat(p)
        except OSError:
            continue
        rep["files"].append({"path": p, "size": st.st_size, "mtime": st.st_mtime})
        rep["bytes"] += st.st_size
    rep["n_files"] = len(rep["files"])
    if not rep["files"]:
        rep["status"] = "empty"
        return rep
    newest = max(rep["files"], key=lambda f: f["mtime"])
    rep["newest_file"] = newest["path"]
    rep["newest_mtime"] = datetime.fromtimestamp(newest["mtime"]).strftime(TS_FORMAT)
    rep["status"] = "ok"
    return rep


def resolve_file(name, cfg):
    rep = _blank(name, cfg)
    p = cfg.get("feeds.%s.path" % name)
    rep["configured"] = [p] if p else []
    rep["origin"] = cfg.origin("feeds.%s.path" % name)
    if p and os.path.isfile(p):
        st = os.stat(p)
        rep.update(exists=True, resolved=[p], status="ok", n_files=1, bytes=st.st_size,
                   newest_file=p,
                   newest_mtime=datetime.fromtimestamp(st.st_mtime).strftime(TS_FORMAT))
        rep["files"] = [{"path": p, "size": st.st_size, "mtime": st.st_mtime}]
    else:
        rep["missing"] = rep["configured"]
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
        if rep.get("source_roots"):
            return _NOTICE["empty"] % (rep["name"], "%s (read through its EBR view)" % (
                ", ".join(rep["source_roots"])))
        return _NOTICE["empty"] % (rep["name"], ", ".join(rep["resolved"]))
    if rep["status"] == "stale":
        return _NOTICE["stale"] % (rep["name"], rep["lag"], fmt_age(rep["max_age_s"]))
    if rep["status"] == "degraded":
        return "The %s feed is missing column(s): %s." % (rep["name"],
                                                          ", ".join(rep["degraded"]))
    return None


def resolve(cfg):
    """{feed_name: report}.  Never raises; a broken feed becomes status='missing'."""
    out = {}
    for name in FEEDS:
        try:
            if name in ("ebpf", "ebpf_node"):
                rep = resolve_dated(name, cfg)
            elif name in ("sacct", "sacctmgr"):
                rep = resolve_globbed(name, cfg)
            else:
                rep = resolve_file(name, cfg)
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
            "_present": rep["status"] in ("ok", "stale", "degraded"),
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
        if r.get("prefer") and r["exists"]:
            fm = r["formats"]
            print("  formats %-9s prefer %s; read %s; unread %s; %d held" % (
                r["name"], r["prefer"],
                ", ".join("%s %d" % kv for kv in sorted(fm["read"].items())) or "-",
                ", ".join("%s %d" % kv for kv in sorted(fm["unread"].items())) or "-",
                len(r["held"])))
    for r in reports.values():
        if r["notice"]:
            print("  ! %s" % r["notice"])
        if r.get("format_notice"):
            print("  ! %s" % r["format_notice"])
    return 1 if bad else 0
