"""Is the collection working? `rc-dashboard status`, GET /api/status and GET /metrics.

/api/health's `ok` is about the service's own loops: it stays true with every
collector silent. This answers the other question, per host -- is its collector
running (health.py's verdict, from its own meta / heartbeat / stop records), how
old is the newest record of each tier, and is the copy that brings its files
here working -- and rolls it up into one level a monitor can act on:

  ok    every collector heartbeating, every copy working, nothing stale
  warn  something to look at: a collector degraded, or with no heartbeat in its
        files; a copy that failed its last pass but worked within `stale_s`; the
        node tier's newest record older than `stale_s`; a transport file that
        cannot be read
  crit  data is not arriving: the required feed missing or empty; a collector
        silent or stopped; a copy with no good pass for `stale_s`; a host's
        newest eBPF record older than `stale_s` while its verdict says nothing

The command computes it from the same config `serve` reads, with no service
running: the feeds resolved as `serve` resolves them, and each host's own records
read from the newest end of its heartbeat stream (`<host>.snapshot.jsonl`, or the
single file of an older capture), at most SCAN_BYTES of it and the newest
SCAN_DAYS dated folders: a run that started, or ended, before that is not seen.
The service answers from what it has read (`from_service`). Exit 0 ok, 1 warn,
2 crit. The JSON is a contract (hpc/deploy's check codes against it):

  {"level": "ok|warn|crit", "why": [str],
   "hosts": [{"host": str, "verdict": "ok|degraded|stopped|silent|unknown",
              "heartbeat_age_s": num|null,
              "newest_age_s": {"ebpf": num|null, "ebpf_node": num|null},
              "transport": {"kind": "rsync", "ok": bool,
                            "last_ok_age_s": num|null, "error": str|null} | null}]}

A null is a value nobody measured, never 0: no heartbeat in what was read, no
file of that tier for the host, a copy that never worked.

**Transports** (`status.transport_files`, RC_DASH_TRANSPORT_FILES: paths or glob
patterns) are the state files of what copies the files here: deploy/fleet/pull.py's
`<mirror>/.state/<node>.json`, which both kits copy with (kind `rsync`): `host`, `ok`,
`ok_epoch` (its last good pass), `error`, `file_hosts` (the host names the node's
day-files carry). A pass marked ok but older than `stale_s` is not ok: pull.py may
have stopped.

Each maps to a collector host by name (its `host`, or a name in `file_hosts`),
else by where that host's files are (`<mirror>/{ebpf,node}/<node>/`, or `<mirror>/<node>/`
in pull.py's flat layout, the HPC kit's). A transport's host with no files here still
gets a row: the copy that never brought any is what to fix.
"""
import glob
import json
import os
import re
import time

from . import feeds as feedmod
from . import fastjson, redact
from .config import is_pattern
from .feedfile import dated_dirs, feed_files, host_of, iter_lines_reverse, split_name
from .health import CollectorHealth
from .normalize import epoch_of

LEVELS = ("ok", "warn", "crit")
EXIT = {"ok": 0, "warn": 1, "crit": 2}
VERDICTS = ("ok", "degraded", "stopped", "silent", "unknown")
SCAN_BYTES = 64 << 20           # per host: how far back its own records are read
SCAN_DAYS = 8                   # ... and how many dated folders, at least
# A line holding none of these is no record the verdict reads: not parsed.
HEALTH_TAGS = (b"residency_totals", b'"meta"', b'"stop"')
STREAM_SUFFIX = {".exits": "exits", ".snapshot": "snapshot"}
_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")


# ------------------------------------------------------------------ time
_DURATION = re.compile(r"\s*(\d+(?:\.\d+)?)\s*([smhd]?)\s*", re.I)
_UNIT = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration(text):
    """Seconds in `300`, `90s`, `15m`, `2h`, `1.5h` or `1d`; ValueError otherwise."""
    m = _DURATION.fullmatch(str(text))
    if not m or float(m.group(1)) <= 0:
        raise ValueError("%r is not a duration such as 300, 90s, 15m, 2h or 1d" % (text,))
    return float(m.group(1)) * _UNIT[m.group(2).lower()]


def age_text(s):
    if s is None:
        return "never"
    s = int(s)
    if s < 120:
        return "%d s" % s
    if s < 7200:
        return "%d min" % (s // 60)
    if s < 172800:
        return "%.1f h" % (s / 3600.0)
    return "%.1f d" % (s / 86400.0)


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _round(v):
    return None if v is None else round(max(0.0, v), 1)


# ------------------------------------------------------------ transports
def _stem(path):
    return os.path.splitext(os.path.basename(path))[0]


def read_transports(patterns, stale_s, now=None):
    """`(transports, problems)`: each transport {"name", "kind", "ok",
    "last_ok_age_s", "error", "names" (the host names it answers to), "roots"
    (where the files it copies are)}; problems, the files that could not be
    read, in words. `now` is the wall clock: the writers stamp it."""
    now = time.time() if now is None else now
    out, problems = [], []
    for pat in patterns or []:
        if not isinstance(pat, str) or not pat:
            continue
        pat = os.path.expanduser(pat)
        paths = sorted(glob.glob(pat)) if is_pattern(pat) else [pat]
        if not paths:
            problems.append("no transport file matches %s" % pat)
        for p in paths:
            try:
                with open(p) as fh:
                    d = json.load(fh)
            except FileNotFoundError:
                problems.append("transport file %s is not there" % p)
                continue
            except (OSError, ValueError) as e:
                problems.append("transport file %s cannot be read: %s"
                                % (p, getattr(e, "strerror", None) or e))
                continue
            if isinstance(d, dict) and "ok" in d and ("host" in d or "attempt_epoch" in d):
                out.append(_rsync(p, d, stale_s, now))
            else:
                problems.append("transport file %s is not a state file of deploy/fleet/pull.py" % p)
    return out, problems


def _rsync(path, d, stale_s, now):
    name = str(d.get("host") or _stem(path))
    ok_at = _num(d.get("ok_epoch"))
    age = (now - ok_at) if ok_at is not None else None
    err = d.get("error") if not d.get("ok") else None
    ok = bool(d.get("ok")) and age is not None and age <= stale_s
    if d.get("ok") and not ok:
        err = ("no good pass for %s: is pull.py running?" % age_text(age) if age is not None
               else "no good pass recorded")
    elif not d.get("ok") and not err:
        err = "its last pass failed, with no message"
    mirror = os.path.dirname(os.path.dirname(os.path.abspath(path)))
    names = [name] + [str(h) for h in (d.get("file_hosts") or []) if h]
    return {"name": name, "kind": "rsync", "ok": ok, "last_ok_age_s": _round(age),
            "error": redact.redact(str(err)) if err else None, "names": names,
            "roots": [os.path.join(mirror, "ebpf", name), os.path.join(mirror, "node", name),
                      os.path.join(mirror, name)]}     # FLEET_LAYOUT=flat


def public_transport(t):
    """A transport as the contract has it."""
    if t is None:
        return None
    return {k: t[k] for k in ("kind", "ok", "last_ok_age_s", "error")}


def match_transports(transports, host_files):
    """{host: transport} for every host in `host_files` ({host: [file paths]})
    that a transport answers for: by name, else by where its files are; and the
    transports that answer for none of them."""
    by_name = {}
    for t in transports:
        for n in t["names"]:
            by_name.setdefault(n, t)
    out, used = {}, set()
    for host, paths in host_files.items():
        t = by_name.get(host)
        if t is None:
            for cand in transports:
                roots = [r.rstrip("/") + "/" for r in cand["roots"]]
                if any(p.startswith(r) for p in paths for r in roots):
                    t = cand
                    break
        if t is not None:
            out[host] = t
            used.add(id(t))
    return out, [t for t in transports if id(t) not in used]


# ---------------------------------------------------------------- records
def _stream_of(path):
    """`exits`, `snapshot`, or `single` (an older capture's one file per host)."""
    stem = split_name(path)[0]
    for suf, kind in STREAM_SUFFIX.items():
        if stem.endswith(suf):
            return kind
    return "single"


def newest_epoch(path):
    """The newest record's epoch in a day-file, read from its end, or None."""
    got = feedmod.newest_record_ts(path)
    return got[1] if got else None


def host_files(report):
    """{host: [day-file paths]} of a feed report."""
    out = {}
    for f in report.get("files") or []:
        out.setdefault(f.get("host") or host_of(f["path"]), []).append(f["path"])
    return out


def _day_of(path):
    """A day-file's dated folder, or "" for a flat file under a root (an older
    capture's layout), which so sorts before every day."""
    d = os.path.basename(os.path.dirname(path))
    return d if _DAY.fullmatch(d) else ""


def newest_by_host(report, skip_sealed=True):
    """{host: epoch} of the newest record of each host in a feed report, read from
    the end of its newest plain day-files (a sealed `.gz` would be inflated whole
    to reach its end; with `skip_sealed` it is left out, which the heartbeat
    stream's own read covers)."""
    best = {}
    newest_day = {}
    for f in report.get("files") or []:
        host = f.get("host") or host_of(f["path"])
        newest_day[host] = max(newest_day.get(host, ""), _day_of(f["path"]))
    for f in report.get("files") or []:
        host = f.get("host") or host_of(f["path"])
        if _day_of(f["path"]) != newest_day.get(host):
            continue
        if skip_sealed and f.get("compressed"):
            continue
        ts = newest_epoch(f["path"])
        if ts is not None and (best.get(host) is None or ts > best[host]):
            best[host] = ts
    return best


def scan_health(cfg, report, scan_bytes=SCAN_BYTES):
    """`(CollectorHealth, {host: newest epoch read})`: each host's own records
    (meta, heartbeat, stop), read from the newest end of its heartbeat stream
    until its newest run's start and the run before it are both read, or
    `scan_bytes`, or the newest dated folders run out."""
    health = CollectorHealth(cfg)
    days = max(SCAN_DAYS, int(cfg.get("feeds.ebpf.days", 8) or 8))
    paths = {}
    for root in report.get("resolved") or []:
        try:
            listing = [p for _d, files in dated_dirs(root, days) for p in files]
            # A root may also hold flat day-files (feeds.resolve_dated): every one of them.
            # They are hosts' streams as often as days, and cut to the newest `days` by
            # name, a host past the eighth went unread. Each host's read stops at
            # `scan_bytes` whatever its files.
            listing += feed_files(root)
        except OSError:
            continue
        for p in listing:
            if _stream_of(p) in ("snapshot", "single"):
                paths.setdefault(host_of(p), []).append(p)
    newest = {}
    for host, files in paths.items():
        # the newest day first; a flat file after every dated one, the latest name first
        files.sort(key=lambda p: (_day_of(p), os.path.basename(p)), reverse=True)
        metas, spent = 0, 0
        for p in files:
            for line in iter_lines_reverse(p):
                spent += len(line) + 1
                if spent > scan_bytes:
                    break
                if host not in newest:
                    try:
                        ts = epoch_of(fastjson.record(line))
                    except ValueError:
                        ts = None
                    if ts is not None:
                        newest[host] = ts
                if not any(t in line for t in HEALTH_TAGS):
                    continue
                try:
                    rec = fastjson.record(line)
                except ValueError:
                    continue
                if rec.get("event") not in CollectorHealth.EVENTS:
                    continue
                rec.setdefault("host", host)
                rec["_ts_epoch"] = epoch_of(rec)
                health.feed(rec)
                if rec["event"] == "meta":
                    metas += 1
                    if metas >= 2:
                        break
            if spent > scan_bytes or metas >= 2:
                break
    return health, newest


# ------------------------------------------------------------------ judge
def all_files(reports):
    """{host: [day-file paths]} over both tiers."""
    files = {}
    for tier in ("ebpf", "ebpf_node"):
        for host, paths in host_files(reports.get(tier) or {}).items():
            files.setdefault(host, []).extend(paths)
    return files


def assemble(cfg, reports, rows, newest, stale_s, now=None, wall=None):
    """The status document, from feed reports, a CollectorHealth's report
    (`rows`) and {tier: {host: newest epoch}}. `now` is the service's present (a
    pinned clock's, on a capture), `wall` the real time the transport files are
    stamped in. A host's newest eBPF record is no older than its last heartbeat."""
    now = time.time() if now is None else now
    wall = time.time() if wall is None else wall
    transports, problems = read_transports(cfg.get("status.transport_files") or [],
                                           stale_s, wall)
    matched, unmatched = match_transports(transports, all_files(reports))
    hosts = []
    for r in rows:
        host = r["host"]
        ages = {}
        for tier in ("ebpf", "ebpf_node"):
            ts = (newest.get(tier) or {}).get(host)
            beat = (r.get("heartbeat") or {}).get("last") if tier == "ebpf" else None
            if beat is not None and (ts is None or beat > ts):
                ts = beat
            ages[tier] = _round(now - ts) if ts is not None else None
        hosts.append({"host": host,
                      "verdict": r["verdict"] if r["verdict"] in VERDICTS else "unknown",
                      "heartbeat_age_s": _round((r.get("heartbeat") or {}).get("age_s")),
                      "newest_age_s": ages,
                      "transport": public_transport(matched.get(host)),
                      "_reasons": r.get("reasons") or []})
    for t in unmatched:
        hosts.append({"host": t["name"], "verdict": "unknown", "heartbeat_age_s": None,
                      "newest_age_s": {"ebpf": None, "ebpf_node": None},
                      "transport": public_transport(t),
                      "_reasons": ["no day-file of this host here"]})

    level, why = "ok", []

    def note(lvl, msg):
        nonlocal level
        if LEVELS.index(lvl) > LEVELS.index(level):
            level = lvl
        why.append(msg)

    for tier in ("ebpf", "ebpf_node"):
        rep = reports.get(tier) or {}
        if rep.get("required") and rep.get("status") in ("missing", "empty"):
            note("crit", rep.get("notice") or "no %s feed" % tier)
    for p in problems:
        note("warn", p)
    for h in hosts:
        name, v, t = h["host"], h["verdict"], h["transport"]
        if t is not None and not t["ok"]:
            bad = t["last_ok_age_s"] is None or t["last_ok_age_s"] > stale_s
            note("crit" if bad else "warn", "%s: copy (%s) FAILED: %s; last good %s" % (
                name, t["kind"], t["error"] or "no message",
                "never" if t["last_ok_age_s"] is None else age_text(t["last_ok_age_s"]) + " ago"))
        reason = (h["_reasons"] or [None])[0]
        if v in ("silent", "stopped"):
            note("crit", "%s: collector %s: %s" % (name, v, reason or v))
        elif v == "degraded":
            note("warn", "%s: collector degraded: %s" % (name, "; ".join(h["_reasons"])))
        elif v == "unknown" and (t is None or t["ok"]):
            note("warn", "%s: %s" % (name, reason or "no heartbeat in its files"))
        eb = h["newest_age_s"]["ebpf"]
        if v not in ("silent", "stopped") and eb is not None and eb > stale_s:
            note("crit", "%s: its newest eBPF record is %s old" % (name, age_text(eb)))
        nd = h["newest_age_s"]["ebpf_node"]
        if nd is not None and nd > stale_s:
            note("warn", "%s: its newest node-tier record is %s old" % (name, age_text(nd)))
    for h in hosts:
        h.pop("_reasons", None)
    return {"level": level, "why": why, "hosts": hosts}


def collect(cfg, stale_s=None, now=None, wall=None):
    """`rc-dashboard status`: the document, computed from the config with no
    service running (the feeds resolved, each host's records read)."""
    stale_s = float(stale_s if stale_s is not None else cfg.get("status.stale_s", 900))
    now = time.time() if now is None else now
    reports = feedmod.resolve(cfg)
    health, beat_newest = scan_health(cfg, reports["ebpf"])
    newest = {"ebpf": newest_by_host(reports["ebpf"]),
              "ebpf_node": newest_by_host(reports["ebpf_node"], skip_sealed=False)}
    for host, ts in beat_newest.items():
        if newest["ebpf"].get(host) is None or ts > newest["ebpf"][host]:
            newest["ebpf"][host] = ts
    rows = health.report({h: {} for h in all_files(reports)}, now=now)
    return assemble(cfg, reports, rows, newest, stale_s, now=now, wall=wall)


def from_service(agg, stale_s=None, wall=None):
    """GET /api/status: the document from what the running service has read --
    its collectors' health and its feed reports -- and each host's newest record,
    read from the end of its plain files now (a sealed host's from its heartbeat)."""
    cfg = agg.cfg
    stale_s = float(stale_s if stale_s is not None else cfg.get("status.stale_s", 900))
    with agg.lock:
        reports = {k: dict(v) for k, v in agg.reports.items()}
    now = time.time()
    newest = {"ebpf": newest_by_host(reports.get("ebpf") or {}),
              "ebpf_node": newest_by_host(reports.get("ebpf_node") or {}, skip_sealed=False)}
    files = all_files(reports)
    with agg.lock:
        rows = agg.health.report({h: {} for h in files}, now=now)
    return assemble(cfg, reports, rows, newest, stale_s, now=now, wall=wall)


def transports_by_host(cfg, reports, wall=None):
    """{host: transport, as the contract has it} for the hosts with files here, and
    `(name, transport)` for each transport that answers for none of them: for
    /api/collectors' rows."""
    stale_s = float(cfg.get("status.stale_s", 900))
    transports, _problems = read_transports(cfg.get("status.transport_files") or [], stale_s,
                                            wall)
    matched, unmatched = match_transports(transports, all_files(reports))
    return ({h: public_transport(t) for h, t in matched.items()},
            [(t["name"], public_transport(t)) for t in unmatched])


# ------------------------------------------------------------------ text
def text(doc):
    """`rc-dashboard status` without --json."""
    lines = ["%s" % doc["level"].upper()]
    lines += ["  - %s" % w for w in doc["why"]]
    if doc["hosts"]:
        w = max(len(h["host"]) for h in doc["hosts"])
        lines.append("")
        lines.append("%-*s  %-9s %-11s %-11s %-11s  %s" % (
            w, "host", "collector", "heartbeat", "ebpf", "node tier", "copy"))
        for h in doc["hosts"]:
            t = h["transport"]
            copy = "-" if t is None else ("%s ok" % t["kind"] if t["ok"] else
                                          "%s FAILED: %s" % (t["kind"], t["error"]))
            lines.append("%-*s  %-9s %-11s %-11s %-11s  %s" % (
                w, h["host"], h["verdict"], _ago(h["heartbeat_age_s"]),
                _ago(h["newest_age_s"]["ebpf"]), _ago(h["newest_age_s"]["ebpf_node"]), copy))
    return "\n".join(lines)


def _ago(s):
    return "-" if s is None else age_text(s) + " ago"


# --------------------------------------------------------------- metrics
def _label(v):
    return str(v).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def metrics(doc, version=None):
    """The document in Prometheus' text format: the same numbers, a sample left
    out where the document has null (never written as 0)."""
    out = []

    def family(name, help_, rows, kind="gauge"):
        out.append("# HELP %s %s" % (name, help_))
        out.append("# TYPE %s %s" % (name, kind))
        for labels, value in rows:
            if value is None:
                continue
            lab = ",".join('%s="%s"' % (k, _label(v)) for k, v in labels)
            out.append("%s%s %s" % (name, "{%s}" % lab if lab else "",
                                    repr(float(value)) if isinstance(value, float) else value))

    family("rc_dashboard_status_level", "The roll-up: 0 ok, 1 warn, 2 crit.",
           [((), EXIT[doc["level"]])])
    if version:
        family("rc_dashboard_build_info", "The running service's version and commit.",
               [((("version", version.get("version")),
                  ("commit", version.get("commit") or "")), 1)])
    hosts = doc["hosts"]
    family("rc_dashboard_collector_verdict",
           "Each host's collector verdict: 1 for the one it has, 0 for the others.",
           [((("host", h["host"]), ("verdict", v)), int(h["verdict"] == v))
            for h in hosts for v in VERDICTS])
    family("rc_dashboard_heartbeat_age_seconds",
           "Seconds since each host's collector last wrote its heartbeat.",
           [((("host", h["host"]),), h["heartbeat_age_s"]) for h in hosts])
    family("rc_dashboard_newest_record_age_seconds",
           "Seconds since each host's newest record, by tier.",
           [((("host", h["host"]), ("tier", tier)), h["newest_age_s"][tier])
            for h in hosts for tier in ("ebpf", "ebpf_node")])
    family("rc_dashboard_transport_ok",
           "Whether the copy bringing each host's files here works (1) or not (0).",
           [((("host", h["host"]), ("kind", h["transport"]["kind"])),
             int(bool(h["transport"]["ok"]))) for h in hosts if h["transport"]])
    family("rc_dashboard_transport_last_ok_age_seconds",
           "Seconds since the copy bringing each host's files here last worked.",
           [((("host", h["host"]), ("kind", h["transport"]["kind"])),
             h["transport"]["last_ok_age_s"]) for h in hosts if h["transport"]])
    return "\n".join(out) + "\n"
