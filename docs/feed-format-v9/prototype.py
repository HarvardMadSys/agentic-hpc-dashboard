#!/usr/bin/env python3
"""Feed schema 9, prototyped against a schema-8 feed: convert, prove the round trip, measure.

    python3 prototype.py demo <dir> [--hours 24] [--hosts 2]   # a synthetic schema-8 feed
    python3 prototype.py run <ebpf root> <out dir> [--node <node root>] [--tz <zone>]

`<ebpf root>` holds the collector's day directories,
`<YYYY-MM-DD>/<host>.{exits,snapshot}.jsonl[.gz]`. `--node` points at the node
tier's root and is read only for each host's boot time (`proc_stat.btime`).
`--tz` is the collector's time zone (an IANA name, e.g. America/New_York), the
zone schema 8's naive `ts` and `start` strings were written in; without it, the
zone this runs in. A wrong zone moves no instant here -- `start_epoch` comes from
`ts_epoch - duration_s` -- but every rebuilt string then differs, and the report
shows how many.

`run` reads the feed twice. The first pass keeps, for the whole feed, every
process's life and every session's facts (`Index`): on a multi-week, many-node
feed that index is what memory grows with. The second pass then does three
things one host-day at a time, holding that host-day's records:

1. convert  writes the schema-9 feed under <out dir>/v9: the same two streams plus
            `<host>.procs.jsonl`, which holds the `session` and `proc` definitions;
2. verify   reads the three files it just wrote back from disk, expands every
            schema-9 record into the schema-8 record it came from, and compares
            them field by field. A difference that is not on the declared list
            (DECLARED below) fails the run: nothing may be lost that the
            declaration does not name;
3. measure  bytes (plain, gzip -6 as the collector seals a day, zstd -3), orjson
            parse time and stdlib encode time, for schema 8 as the collector writes
            it, schema 8 with compact separators, and schema 9; and, for the exits
            stream, two alternatives: positional rows, and Parquet when pyarrow is
            installed.

A schema-8 record carries no process start time in clock ticks, so `pk` and `ppk`
are rebuilt here from `ts_epoch - duration_s` (exits) and `ts_epoch - age_s`
(residency) against the boot time. The collector reads them exactly
(`task->start_time`); the rebuilt ones are the right length for measuring and are
never compared. A process no exit or residency record gives a start for (seen
only on other ticks, or only as a parent) gets a guessed one, kept apart from
the real lives so it can never take a real process's key; the report counts
them, and counts pid reuse from real lives only. A host with no node-tier boot
time counts ticks from 30 days before its first record; the report counts those
hosts too.

Standard library only; orjson, zstandard and pyarrow are used when installed.
"""
import argparse
import bisect
import collections
import gzip
import json
import os
import re
import sys
import time
import zlib
from datetime import datetime

try:
    import orjson
except ImportError:                     # pragma: no cover
    orjson = None
try:
    import zstandard
except ImportError:                     # pragma: no cover
    zstandard = None

V8, V9 = 8, 9
HZ = 100                                # USER_HZ: /proc/<pid>/stat's starttime unit
TS = "%Y-%m-%d %H:%M:%S"
DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Schema 8's identity block, in `_base_identity` order (collector/ebpf_trace.py).
V8_IDENTITY = (
    "actor", "actor3", "attribution", "pid", "ppid", "uid", "user", "tty", "command",
    "args", "args_redacted", "argv_source", "args_len", "args_truncated", "cwd",
    "sandbox", "sandbox_src", "sandbox_detail", "sandbox_ancestry", "approval_mode",
    "approval_src", "agent_type", "agent_pid", "session_key", "session_uuid",
    "tree_root_pid", "depth", "is_agent", "autonomous", "cgroup")
# Facts about the session, not the process: constant across every record that
# names the session, and written once, in its `session` record.
SESSION_ONLY = ("agent_type", "agent_pid", "session_key", "session_uuid",
                "tree_root_pid", "autonomous")
PROC_KEYS = tuple(k for k in V8_IDENTITY if k not in SESSION_ONLY)

ENVELOPE = ("ts", "ts_epoch", "host", "event", "collector_pid", "schema_version", "synthetic")
RUN_EVENTS = ("meta", "stop", "residency_totals")      # keep collector_pid (health.py)
EXIT_EVENTS = ("exit", "truncated")
TICK_EVENTS = ("residency", "conn", "netio", "submit")
# What schema 9 drops from an `exit` or `truncated` record is only what it can
# rebuild for THAT record. An `exit` (`_exit_record`) always has `cpu_pct`
# (cpu_s / duration_s), and `samples`/`state_last` are null poller placeholders.
# A `truncated` record is built elsewhere (`flush_live`): it has no `cpu_pct`, its
# `state_last` is the process's last /proc state, its `cpu_scope` is always
# `process`, and its counters' `io.scope` is `proc`. So `samples` and `state_last`
# are dropped only when null, and a scope only when it is the capture's own
# (`meta.cpu_scope`, `meta.counters_scope`).
NULL_ONLY = ("samples", "state_last")
MIB = 1048576

# Every difference the round trip may show, and why. Anything else fails `verify`.
DECLARED = {
    "ts": "rebuilt from ts_epoch; the collector stamps the two separately, so the "
          "string can sit one second off -- or every one differs, when --tz is not "
          "the collector's zone",
    "start": "rebuilt from start_epoch (ts_epoch - duration_s); differs where the "
             "string is more than 2 s from that instant read in this zone: --tz not "
             "the collector's, or the hour a DST change repeats",
    "collector_pid": "on per-process records, rebuilt from the latest meta of the host",
    "cpu_pct": "rebuilt from the rounded cpu_s/duration_s, so the last digit can move",
    "conns.peers": "each merged into an endpoint row that agrees on bytes and counts, "
                   "or kept whole in conns.peers_unmatched; checked separately below",
}

# The zone schema 8's naive strings are read and written in (`--tz`); None is
# the zone this process runs in.
ZONE = None


def stamp(epoch):
    if ZONE is not None:
        return datetime.fromtimestamp(epoch, ZONE).strftime(TS)
    return time.strftime(TS, time.localtime(epoch))


def sess_of(r):
    """The session a schema-8 record names: its session_uuid, or `apid:<pid>` where
    the collector could not digest one (`session_uuid()` returns null when the
    root's start time is unreadable) but the record is still in an agent tree."""
    if r.get("session_uuid"):
        return r["session_uuid"]
    return "apid:%d" % r["agent_pid"] if r.get("agent_pid") is not None else None


def parse_stamp(s):
    """The instant a naive string names in ZONE. In the hour a DST change repeats
    it is a guess (the first of the two), which is why no instant is taken from a
    string that disagrees with `ts_epoch - duration_s` (`to_v9`)."""
    if ZONE is not None:
        return datetime.strptime(s, TS).replace(tzinfo=ZONE).timestamp()
    return time.mktime(time.strptime(s, TS))


if orjson is not None:
    def loads(b):
        return orjson.loads(b)

    def compact(rec):
        return orjson.dumps(rec) + b"\n"
else:                                   # pragma: no cover
    def loads(b):
        return json.loads(b)

    def compact(rec):
        return (json.dumps(rec, separators=(",", ":"), ensure_ascii=False) + "\n").encode()


def collector_style(rec):
    """Schema 8 as DailyWriter writes it: `json.dumps(rec) + '\\n'`, default separators."""
    return (json.dumps(rec) + "\n").encode()


# ---------------------------------------------------------------------------- input

def read_records(path):
    opener = gzip.open if path.endswith(".gz") else open
    out = []
    with opener(path, "rb") as fh:
        for ln in fh:
            if ln.strip():
                out.append(json.loads(ln))       # json, not orjson: keeps key order and floats
    return out


def day_files(root):
    """{(day, host): {stream: path}}, oldest day first."""
    out = collections.defaultdict(dict)
    for day in sorted(os.listdir(root)):
        d = os.path.join(root, day)
        if not (DAY_RE.match(day) and os.path.isdir(d)):
            continue
        for name in sorted(os.listdir(d)):
            m = re.match(r"^(.+)\.(exits|snapshot)\.jsonl(\.gz)?$", name)
            if m:
                out[(day, m.group(1))][m.group(2)] = os.path.join(d, name)
    return dict(sorted(out.items()))


def boot_times(node_root):
    out = {}
    if not node_root or not os.path.isdir(node_root):
        return out
    for day in sorted(os.listdir(node_root)):
        d = os.path.join(node_root, day)
        if not os.path.isdir(d):
            continue
        for name in os.listdir(d):
            if not re.match(r"^.+\.jsonl(\.gz)?$", name):
                continue
            for r in read_records(os.path.join(d, name))[:1]:
                bt = (r.get("proc_stat") or {}).get("btime")
                host = r.get("host") or r.get("hostname")
                if bt and host:
                    out.setdefault(host, float(bt))
    return out


# ---------------------------------------------------------------------------- index

class Index:
    """What a second pass needs to know about the whole feed: each process's life
    (for `pk`), each session's facts, and each host's collector runs."""

    def __init__(self, btime):
        self.btime = dict(btime)
        self.btime_guessed = set()                     # hosts with no node-tier boot time
        self.lives = collections.defaultdict(list)     # (host, pid) -> [[start, end|None]]
        # Starts no exit or residency gave, kept apart from `lives`: (host, pid)
        # -> [[first seen, last seen]]. Never matched before a real life, never
        # counted as pid reuse.
        self.guesses = collections.defaultdict(list)
        self.sessions = {}                             # (host, sess) -> facts
        self.runs = collections.defaultdict(list)      # host -> [(ts, collector_pid, meta)]
        self.conflicts = collections.Counter()         # session facts that disagreed

    def _life(self, host, pid, start, end):
        for life in self.lives[(host, pid)]:
            if abs(life[0] - start) < 2.0:
                if end is not None and (life[1] is None or end > life[1]):
                    life[1] = end
                return
        self.lives[(host, pid)].append([start, end])

    def add(self, r):
        host, ev, ts = r.get("host"), r.get("event"), r.get("ts_epoch")
        if ev == "meta":
            self.runs[host].append((ts, r.get("collector_pid"), r))
        if host not in self.btime:
            self.btime[host] = (ts or 0) - 30 * 86400
            self.btime_guessed.add(host)
        if ev in EXIT_EVENTS and r.get("pid") is not None \
                and (r.get("duration_s") is not None or r.get("start") is not None):
            # Only a record that knows its start is a life: one that does not (a
            # truncated process with no exec time and no /proc start) would be a
            # life starting at its own timestamp, and a second process on its pid.
            self._life(host, r["pid"], exit_start(r), ts)
        elif ev == "residency" and r.get("pid") is not None and r.get("age_s") is not None:
            self._life(host, r["pid"], ts - r["age_s"], None)
        sess = sess_of(r)
        if sess:
            s = self.sessions.setdefault((host, sess), {
                "agent_type": r.get("agent_type"), "agent_pid": r.get("agent_pid"),
                "tree_root_pid": r.get("tree_root_pid"), "first": ts, "root": None})
            for k in ("agent_type", "agent_pid", "tree_root_pid"):
                if s[k] != r.get(k):
                    self.conflicts[k] += 1
            if r.get("is_agent") and r.get("pid") == r.get("agent_pid"):
                root = {k: r.get(k) for k in ("autonomous", "approval_mode", "approval_src")}
                if s["root"] is not None and s["root"] != root:
                    self.conflicts["root"] += 1
                s["root"] = root
            s["first"] = min(s["first"], ts)

    def finish(self):
        for lives in self.lives.values():
            lives.sort(key=lambda x: x[0])
        for runs in self.runs.values():
            runs.sort(key=lambda x: x[0])

    def pk(self, host, pid, at):
        """'<pid>.<start ticks since boot>' for the process `pid` alive at `at`.

        A real life (an exit's or a residency's) always wins. Without one, a
        guess: the latest guess for that pid that began before `at`, stretched to
        `at`, unless a real life of the same pid began in between -- the pid was
        reused, so the guess cannot be the same process -- and otherwise a new
        guess starting at `at`. The real lives are never changed here, so what a
        lookup returns does not depend on which lookups came before it."""
        if pid is None:
            return None
        start = None
        for s, e in self.lives.get((host, pid), ()):
            if s <= at + 2.0 and (e is None or e >= at - 2.0):
                start = s
        if start is None:
            start = self._guess(host, pid, at)
        return "%d.%d" % (pid, max(0, int(round((start - self.btime[host]) * HZ))))

    def _guess(self, host, pid, at):
        guesses = self.guesses[(host, pid)]
        g = None
        for cand in guesses:                    # in order of first sight
            if cand[0] <= at + 2.0:
                g = cand
        if g is not None:
            lo, hi = sorted((g[1], at))
            if not any(lo < s <= hi for s, _e in self.lives.get((host, pid), ())):
                g[1] = max(g[1], at)
                return g[0]
        guesses.append([at, at])
        guesses.sort(key=lambda x: x[0])
        return at

    def guessed(self):
        return sum(len(g) for g in self.guesses.values())

    def reused(self):
        """(host, pid) pairs that named more than one process, from real lives only."""
        return sum(1 for lives in self.lives.values() if len(lives) > 1)

    def start_of(self, pk_, host):
        return round(self.btime[host] + int(pk_.split(".")[1]) / HZ, 2)

    def run_at(self, host, ts):
        """(collector_pid, meta) of the run that wrote a record at `ts`."""
        runs = self.runs.get(host) or []
        i = bisect.bisect_right([r[0] for r in runs], ts) - 1
        return (runs[i][1], runs[i][2]) if i >= 0 else (None, {})


def exit_start(r):
    if r.get("duration_s") is not None:
        return r["ts_epoch"] - r["duration_s"]
    if r.get("start") is not None:
        return parse_stamp(r["start"])
    return r["ts_epoch"]


# --------------------------------------------------------------------------- convert

def scopes(ix, host, ts):
    """The capture's own scopes for a record at `ts`: its run's `meta`."""
    meta = ix.run_at(host, ts)[1]
    return (meta.get("cpu_scope") or "process", meta.get("counters_scope") or "process")


def io_v9(io8, scope):
    """The io block in bytes. `scope` goes only where it is not the capture's
    (`scope`): a truncated record's counters are /proc's (`proc`)."""
    if io8 is None:
        return None
    out = {}
    for k, v in io8.items():
        if k == "scope":
            if v != scope:
                out["scope"] = v
        elif k.endswith("_mb"):
            out[k[:-3] + "_bytes"] = None if v is None else int(round(v * 1e6))
        else:
            out[k] = v
    return out


def peer_merged(p, e):
    """Whether endpoint row `e` holds everything peer `p` says: the same TCP
    endpoint, the same bytes, and the peer's connection counts."""
    return (e.get("proto") == "tcp" and e.get("addr") == p.get("daddr")
            and e.get("port") == p.get("dport") and e.get("tx_bytes") == p.get("tx_bytes")
            and e.get("rx_bytes") == p.get("rx_bytes") and e.get("conns") == p.get("n")
            and e.get("failed") == p.get("failed"))


def endpoints_v9(ne, conns):
    """net_endpoints without its derivable `n`; TCP rows gain the connection counts
    `conns.peers` held. Returns (block, peers not merged).

    A peer merges into a row only when both name the same TCP endpoint AND carry
    the same bytes: the two come from different probes (`tcp_info` against the
    send/recv endpoint table), so a peer whose bytes differ is an independent
    measurement and is kept whole, in `conns.peers_unmatched`."""
    left = list((conns or {}).get("peers") or [])
    if ne is None:
        return None, left
    rows = [dict(e) for e in ne.get("endpoints") or []]
    for p in list(left):
        for e in rows:
            if "conns" not in e and e.get("proto") == "tcp" \
                    and e.get("addr") == p.get("daddr") and e.get("port") == p.get("dport") \
                    and e.get("tx_bytes") == p.get("tx_bytes") \
                    and e.get("rx_bytes") == p.get("rx_bytes"):
                e["conns"], e["failed"] = p.get("n"), p.get("failed")
                left.remove(p)
                break
    out = {k: v for k, v in ne.items() if k not in ("n", "endpoints")}
    out["endpoints"] = rows
    return out, left


def conns_v9(c, peers_left):
    """The conns block without `peers`, which the endpoint rows now hold.
    `providers` stays: it counts every connection (`_conn_fold`), failed ones
    and those past the top-N peers included, which no endpoint row can."""
    if c is None:
        return None
    out = {k: v for k, v in c.items() if k != "peers"}
    if peers_left:                       # never silently dropped
        out["peers_unmatched"] = peers_left
    return out


class Day:
    """One host's day: the definitions already written to its procs file."""

    def __init__(self):
        self.procs = {}                  # pk -> identity last written
        self.sessions = set()
        self.out = []                    # procs stream, in time order


def define_session(ix, day, host, sess, ts):
    if not sess or sess in day.sessions:
        return
    s = ix.sessions[(host, sess)]
    root = s["root"] or {}
    first = s["first"]
    day.sessions.add(sess)
    day.out.append({
        "event": "session", "ts_epoch": ts, "host": host, "schema_version": V9,
        "sess": sess, "agent_type": s["agent_type"],
        "root_pk": ix.pk(host, s["agent_pid"], first),
        "tree_root_pk": ix.pk(host, s["tree_root_pid"], first),
        # the root's verdicts, so no reader has to find the root's own record
        "autonomous": root.get("autonomous"), "approval_mode": root.get("approval_mode"),
        "approval_src": root.get("approval_src")})


def define_proc(day, r, pk_, ppk, sess, start_epoch, ts):
    ident = [r.get(k) for k in PROC_KEYS] + [ppk, sess]
    if day.procs.get(pk_) == ident:
        return
    day.procs[pk_] = ident
    rec = {"event": "proc", "ts_epoch": ts, "host": r["host"], "schema_version": V9,
           "pk": pk_, "ppk": ppk, "sess": sess, "start_epoch": start_epoch}
    rec.update((k, r.get(k)) for k in PROC_KEYS)
    day.out.append(rec)


def to_v9(r, ix, day, counts):
    ev, host, ts = r["event"], r["host"], r["ts_epoch"]
    out = {"event": ev, "ts_epoch": ts, "host": host, "schema_version": V9}
    if "synthetic" in r:
        out["synthetic"] = r["synthetic"]
    if ev in RUN_EVENTS:
        out["collector_pid"] = r.get("collector_pid")
    if ev not in EXIT_EVENTS + TICK_EVENTS:
        out.update((k, v) for k, v in r.items() if k not in ENVELOPE)
        return out

    sess = sess_of(r)
    tracked = r.get("actor") is not None
    if ev in EXIT_EVENTS:
        start = round(exit_start(r), 3)
        if r.get("start") is not None and stamp(start) != r["start"]:
            s = parse_stamp(r["start"])
            if s - 2.0 <= start <= s + 3.0:
                # The same instant, a second boundary apart: ts_epoch is stamped
                # when the exit is handled, a little after the kernel's exit (the
                # collector would write start_epoch from exec_ktime). Moved into
                # the string's second, so the string comes back as written.
                counts["start_epoch moved into its string's second"] += 1
                start = round(min(max(start, s), s + 0.999), 3)
            else:
                # Not the same instant read in this zone: --tz is not the
                # collector's, or the string is in an hour a DST change repeats.
                # The instant stays the one ts_epoch - duration_s gives.
                counts["start string more than 2 s from ts_epoch - duration_s "
                       "(--tz not the collector's?)"] += 1
        pk_ = ix.pk(host, r.get("pid"), start)
        ppk = ix.pk(host, r.get("ppid"), start) if r.get("ppid") else None
        out.update(pk=pk_, ppk=ppk, sess=sess)
        out.update((k, r.get(k)) for k in PROC_KEYS)
        # Null wherever schema 8's `start` is null, as it was: a truncated record
        # with no exec time and no /proc start, and also an exit whose start the
        # collector could not place (no boot time) though it has `duration_s`.
        # `duration_s` is kept either way, and `pk` still takes a start: the
        # one ts_epoch - duration_s gives, or a guess.
        out["start_epoch"] = round(start, 3) if r.get("start") is not None else None
        define_session(ix, day, host, sess, ts)
    elif tracked:
        pk_ = ix.pk(host, r.get("pid"), ts)
        start = ix.start_of(pk_, host)
        ppk = ix.pk(host, r.get("ppid"), start) if r.get("ppid") else None
        out.update(pk=pk_, sess=sess)
        define_session(ix, day, host, sess, ts)
        define_proc(day, r, pk_, ppk, sess, start, ts)
    else:                                # an untracked socket owner: the stub stays inline
        out.update(pk=None, pid=r.get("pid"), uid=r.get("uid"), user=r.get("user"),
                   command=r.get("command"))

    peers_left = []
    if ev in EXIT_EVENTS:
        ne, peers_left = endpoints_v9(r.get("net_endpoints"), r.get("conns"))
        cpu_scope, io_scope = scopes(ix, host, ts)
    for k, v in r.items():
        if k in ENVELOPE or k in V8_IDENTITY:
            continue
        if ev in EXIT_EVENTS:
            if k == "start":
                continue                    # start_epoch, above
            if k == "cpu_pct" and ev == "exit":
                continue                    # cpu_s / duration_s, rebuilt
            if k in NULL_ONLY and v is None:
                continue
            if k == "cpu_scope" and v == cpu_scope:
                continue                    # the capture's, in meta
            if k == "peak_rss_mb":
                out["peak_rss_bytes"] = None if v is None else int(round(v * MIB))
                continue
            if k == "rss_mb":               # a truncated record's VmRSS at the stop
                out["rss_bytes"] = None if v is None else int(round(v * MIB))
                continue
            if k == "io":
                out["io"] = io_v9(v, io_scope)
                continue
            if k == "conns":
                out["conns"] = conns_v9(v, peers_left)
                counts["peers kept as peers_unmatched"] += len(peers_left)
                continue
            if k == "net_endpoints":
                out["net_endpoints"] = ne
                continue
        if ev == "residency" and k == "rss_mb":
            out["rss_bytes"] = None if v is None else int(round(v * MIB))
            continue
        if ev == "residency" and k == "top_procs" and v:
            out["top_procs"] = [
                {(kk[:-3] + "_bytes" if kk == "rss_mb" else kk):
                 (int(round(vv * MIB)) if kk == "rss_mb" and vv is not None else vv)
                 for kk, vv in p.items()} for p in v]
            continue
        out[k] = v
    return out


# ---------------------------------------------------------------------------- expand

def to_v8(r, ix, defs, sessions):
    """The schema-8 record a schema-9 record says, given the day's definitions."""
    ev, host, ts = r["event"], r["host"], r["ts_epoch"]
    run_pid, meta = ix.run_at(host, ts)
    out = {"ts": stamp(ts), "ts_epoch": ts, "host": host, "event": ev,
           "collector_pid": r.get("collector_pid", run_pid), "schema_version": V8}
    if "synthetic" in r:
        out["synthetic"] = r["synthetic"]
    if ev not in EXIT_EVENTS + TICK_EVENTS:
        out.update((k, v) for k, v in r.items()
                   if k not in ("event", "ts_epoch", "host", "schema_version",
                                "synthetic", "collector_pid"))
        return out

    if ev in EXIT_EVENTS:
        ident = r
    elif r.get("pk") is not None:
        lst = defs.get(r["pk"]) or []
        i = bisect.bisect_right([d[0] for d in lst], ts) - 1
        if i < 0:
            raise LookupError("no proc definition of %s at or before %s" % (r["pk"], ts))
        ident = lst[i][1]
    else:
        ident = dict.fromkeys(V8_IDENTITY)
        ident.update(pid=r.get("pid"), uid=r.get("uid"), user=r.get("user"),
                     command=r.get("command"))
    sess = r.get("sess") if ev in TICK_EVENTS else ident.get("sess")
    s = sessions.get(sess) if sess else None
    if sess and (s is None or s["ts_epoch"] > ts):
        # every reference comes after its definition (`define_session`)
        raise LookupError("no session definition of %s at or before %s" % (sess, ts))
    for k in V8_IDENTITY:
        out[k] = ident.get(k)
    if s is not None:
        def pid_of(pk_):
            return int(pk_.split(".")[0]) if pk_ else None
        agent_pid = pid_of(s["root_pk"])
        out.update(agent_type=s["agent_type"], agent_pid=agent_pid,
                   session_key="apid:%d" % agent_pid if agent_pid is not None else None,
                   session_uuid=None if sess.startswith("apid:") else sess,
                   tree_root_pid=pid_of(s["tree_root_pk"]),
                   autonomous=s["autonomous"] if out.get("is_agent") else None)

    skip = set(("event", "ts_epoch", "host", "schema_version", "synthetic", "pk",
                "ppk", "sess", "start_epoch", "collector_pid")) | set(PROC_KEYS)
    if ev not in EXIT_EVENTS:
        skip.update(("pid", "uid", "user", "command"))
    for k, v in r.items():
        if k in skip:
            continue
        if k == "peak_rss_bytes":
            out["peak_rss_mb"] = None if v is None else round(v / MIB, 2)
        elif k == "rss_bytes":
            # the collector rounds a truncated record's to 2 places, a residency's to 1
            places = 2 if ev in EXIT_EVENTS else 1
            out["rss_mb"] = None if v is None else round(v / MIB, places)
        elif k == "top_procs" and v:
            out[k] = [{("rss_mb" if kk == "rss_bytes" else kk):
                       (round(vv / MIB, 1) if kk == "rss_bytes" and vv is not None else vv)
                       for kk, vv in p.items()} for p in v]
        elif k == "io" and v is not None:
            io = {}
            for kk, vv in v.items():
                if kk.endswith("_bytes"):
                    io[kk[:-6] + "_mb"] = None if vv is None else round(vv / 1e6, 3)
                else:
                    io[kk] = vv
            io.setdefault("scope", meta.get("counters_scope") or "process")
            out["io"] = io
        elif k == "net_endpoints" and v is not None:
            rows = [{kk: vv for kk, vv in e.items() if kk not in ("conns", "failed")}
                    for e in v.get("endpoints") or []]
            # `n` counts every distinct endpoint; past the cap the cut ones are in
            # `overflow.n` (collector `_endpoint_list`)
            n_all = len(rows) + ((v.get("overflow") or {}).get("n") or 0)
            out[k] = dict(n=n_all, **{kk: vv for kk, vv in v.items() if kk != "endpoints"})
            out[k]["endpoints"] = rows
        elif k == "conns" and v is not None:
            # the peers themselves are checked against the record they came from
            out[k] = {kk: vv for kk, vv in v.items() if kk != "peers_unmatched"}
        else:
            out[k] = v
    if ev in EXIT_EVENTS:
        out["start"] = stamp(r["start_epoch"]) if r.get("start_epoch") is not None else None
        if ev == "exit":
            d, c = out.get("duration_s"), out.get("cpu_s")
            out["cpu_pct"] = (round(100.0 * c / d, 1) if d and d >= 0.01 and c is not None
                              else None)
        out.setdefault("cpu_scope", meta.get("cpu_scope") or "process")
        for k in NULL_ONLY:
            out.setdefault(k, None)
    return out


def diff(a, b, path=""):
    """Paths where two decoded records differ (dict key order ignored)."""
    if isinstance(a, dict) and isinstance(b, dict):
        out = []
        for k in set(a) | set(b):
            if k not in a or k not in b:
                out.append(path + k)
            else:
                out.extend(diff(a[k], b[k], path + k + "."))
        return out
    if isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
        out = []
        for x, y in zip(a, b):
            out.extend(diff(x, y, path))
        return out
    if isinstance(a, float) or isinstance(b, float):
        try:
            return [] if abs(float(a) - float(b)) < 1e-9 else [path.rstrip(".")]
        except (TypeError, ValueError):
            pass
    return [] if a == b else [path.rstrip(".")]


# --------------------------------------------------------------------------- measure

class Sink:
    """Counts a byte stream plain, gzip -6 and zstd -3 without holding it."""

    def __init__(self):
        self.raw = 0
        self._gz = zlib.compressobj(6, zlib.DEFLATED, 31)
        self.gz = 0
        self._zs = zstandard.ZstdCompressor(level=3).compressobj() if zstandard else None
        self.zs = 0

    def add(self, b):
        self.raw += len(b)
        self.gz += len(self._gz.compress(b))
        if self._zs is not None:
            self.zs += len(self._zs.compress(b))

    def close(self):
        self.gz += len(self._gz.flush())
        if self._zs is not None:
            self.zs += len(self._zs.flush())
            self._zs = None
        return self


class Measure:
    SAMPLE = 100000                     # exits kept for the timing runs

    def __init__(self):
        self.sinks = collections.defaultdict(Sink)    # (variant, stream) -> Sink
        self.lines = collections.Counter()            # (variant, stream) -> records
        self.exit8, self.exit9 = [], []               # decoded, for timing
        self.rows = []                                # positional rows, for timing
        self.cols = None

    def add(self, variant, stream, b):
        self.sinks[(variant, stream)].add(b)
        self.lines[(variant, stream)] += 1

    def add_exit(self, r8, r9):
        if self.cols is None:
            self.cols = list(r9)
        for k in r9:
            if k not in self.cols:
                self.cols.append(k)
        row = compact([r9.get(k) for k in self.cols if k != "event"])
        if len(self.exit8) < self.SAMPLE:
            self.exit8.append(r8)
            self.exit9.append(r9)
            self.rows.append(row)
        self.add("v9 positional", "exits", row)


def timeit(fn, xs, reps=3):
    best = None
    for _ in range(reps):
        t = time.perf_counter()
        for x in xs:
            fn(x)
        dt = time.perf_counter() - t
        best = dt if best is None else min(best, dt)
    return 1e6 * best / max(1, len(xs))


def parquet_size(rows, path):
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError:
        return None
    flat = []
    for r in rows:
        f = {k: v for k, v in r.items() if not isinstance(v, (dict, list))}
        for k in ("io",):
            for kk, vv in (r.get(k) or {}).items():
                f["%s_%s" % (k, kk)] = vv
        for k in ("conns", "net_endpoints"):
            f[k] = json.dumps(r[k]) if r.get(k) is not None else None
        flat.append(f)
    pq.write_table(pa.Table.from_pylist(flat), path, compression="zstd")
    return os.path.getsize(path)


# ------------------------------------------------------------------------------- run

def run(ebpf_root, out_dir, node_root=None):
    files = day_files(ebpf_root)
    if not files:
        sys.exit("no <day>/<host>.{exits,snapshot}.jsonl under %s" % ebpf_root)
    ix = Index(boot_times(node_root))
    for streams in files.values():
        for path in streams.values():
            for r in read_records(path):
                ix.add(r)
    ix.finish()

    m = Measure()
    counts = collections.Counter()
    declared = collections.Counter()
    undeclared = collections.Counter()
    examples = {}
    n = 0
    peers_checked = peers_matched = 0
    for (day, host), streams in files.items():
        recs = {s: read_records(p) for s, p in streams.items()}
        order = sorted(((r["ts_epoch"], s, i) for s, rs in recs.items()
                        for i, r in enumerate(rs)), key=lambda x: x[0])
        d = Day()
        v9 = {s: [None] * len(rs) for s, rs in recs.items()}
        for _ts, s, i in order:
            v9[s][i] = to_v9(recs[s][i], ix, d, counts)

        dst = os.path.join(out_dir, "v9", day)
        os.makedirs(dst, exist_ok=True)
        for s, rs in (("procs", d.out),) + tuple(v9.items()):
            with open(os.path.join(dst, "%s.%s.jsonl" % (host, s)), "wb") as fh:
                for r in rs:
                    b = compact(r)
                    fh.write(b)
                    m.add("v9", s, b)
                    m.add("v9 omit-null", s, compact(
                        {k: v for k, v in r.items() if v is not None}))
        for s, rs in recs.items():
            for r in rs:
                m.add("v8 as written", s, collector_style(r))
                m.add("v8 compact", s, compact(r))

        # verify: read the three files back and expand them, as a reader would --
        # the definitions too, so a procs file written out of order or cut short
        # fails here rather than passing on what memory still held
        written = {s: read_records(os.path.join(dst, "%s.%s.jsonl" % (host, s)))
                   for s in ("procs",) + tuple(recs)}
        defs = collections.defaultdict(list)
        sessions = {}
        for r in written["procs"]:
            if r["event"] == "proc":
                defs[r["pk"]].append((r["ts_epoch"], r))
            else:
                sessions[r["sess"]] = r
        for pk_ in defs:
            defs[pk_].sort(key=lambda d_: d_[0])
        for s, rs in recs.items():
            if len(written[s]) != len(rs):
                undeclared["%s records (written %d, read %d)" % (
                    s, len(rs), len(written[s]))] += 1
                continue
            for i, r8 in enumerate(rs):
                r9 = written[s][i]
                if s == "exits":
                    m.add_exit(r8, v9[s][i])
                try:
                    back = to_v8(r9, ix, defs, sessions)
                except LookupError as e:        # a reference with no definition
                    undeclared["unresolved reference"] += 1
                    examples.setdefault("unresolved reference", (r9.get("pk"), repr(e)))
                    continue
                n += 1
                for p in diff(r8, back):
                    if p in DECLARED:
                        declared[p] += 1
                    else:
                        undeclared[p] += 1
                        examples.setdefault(p, (r8.get(p.split(".")[0]),
                                                back.get(p.split(".")[0])))
                # `conns.peers` is a declared difference only because every peer is
                # still in the v9 record: merged into an endpoint row that agrees on
                # bytes and counts, or kept whole. A peer in neither is lost.
                kept = ((r9.get("conns") or {}).get("peers_unmatched")) or []
                rows = ((r9.get("net_endpoints") or {}).get("endpoints")) or []
                for p in (r8.get("conns") or {}).get("peers") or []:
                    peers_checked += 1
                    if any(peer_merged(p, e) for e in rows):
                        peers_matched += 1
                    elif p not in kept:
                        undeclared["conns.peers (not kept)"] += 1
                        examples.setdefault("conns.peers (not kept)", (p, None))

    report(m, ix, counts, n, declared, undeclared, examples, peers_checked, peers_matched,
           out_dir)
    return 1 if undeclared else 0


def report(m, ix, counts, n, declared, undeclared, examples, peers_checked, peers_matched,
           out_dir):
    for s in m.sinks.values():
        s.close()
    p = print
    p("\n## Round trip: %d records expanded back to schema 8" % n)
    p("   undeclared differences: %d%s" % (sum(undeclared.values()),
                                           "" if undeclared else "  (none: nothing lost)"))
    for k, c in undeclared.most_common():
        p("     UNDECLARED %-28s %7d  e.g. %r" % (k, c, examples[k]))
    for k, c in declared.most_common():
        p("     declared   %-28s %7d  %s" % (k, c, DECLARED[k]))
    p("   conns.peers merged into an endpoint row that agrees on bytes and counts: %d of "
      "%d; the rest kept whole in conns.peers_unmatched" % (peers_matched, peers_checked))
    for k, c in counts.most_common():
        p("   %s: %d" % (k, c))
    p("   processes given a guessed start (no exit or residency gave one): %d"
      % ix.guessed())
    p("   (host, pid) pairs that named more than one process, from real lives only: "
      "%d of %d -- one pk each in v9" % (ix.reused(), len(ix.lives)))
    if ix.btime_guessed:
        p("   hosts with no node-tier boot time (pk ticks from 30 days before their "
          "first record): %d (%s)" % (len(ix.btime_guessed),
                                      ", ".join(sorted(ix.btime_guessed))))
    if ix.conflicts:
        p("   session facts that disagreed across records: %s" % dict(ix.conflicts))

    p("\n## Bytes by stream (plain / gzip -6 / zstd -3)")
    streams = ("exits", "snapshot", "procs")
    variants = ("v8 as written", "v8 compact", "v9", "v9 omit-null", "v9 positional")
    tot = collections.defaultdict(lambda: [0, 0, 0])
    p("   %-14s %-9s %9s %13s %11s %11s   %s" % ("variant", "stream", "records", "plain", "gzip",
                                                "zstd", "plain/gzip vs v8 as written"))
    for v in variants:
        for s in streams:
            k = (v, s)
            if k not in m.sinks:
                continue
            sk = m.sinks[k]
            b = m.sinks.get(("v8 as written", s))
            vs = "%5.1f%% / %5.1f%%" % (100.0 * sk.raw / b.raw, 100.0 * sk.gz / b.gz) if b else ""
            p("   %-14s %-9s %9d %13s %11s %11s   %s" % (v, s, m.lines[k], fmt(sk.raw), fmt(sk.gz),
                                                       fmt(sk.zs) if sk.zs else "-", vs))
            if v != "v9 positional":
                for j, x in enumerate((sk.raw, sk.gz, sk.zs)):
                    tot[v][j] += x
    p("   totals:")
    base = tot["v8 as written"]
    for v in ("v8 as written", "v8 compact", "v9", "v9 omit-null"):
        t = tot[v]
        p("   %-14s %13s (%5.1f%%) %11s (%5.1f%%) %11s" % (
            v, fmt(t[0]), 100.0 * t[0] / base[0], fmt(t[1]), 100.0 * t[1] / base[1],
            fmt(t[2]) if t[2] else "-"))

    ex8 = m.sinks[("v8 as written", "exits")]
    ex9 = m.sinks[("v9", "exits")]
    p("\n## Exits stream, per record")
    p("   v8 as written %6.0f B   v9 %6.0f B   (%.1f%% of v8; gzip %.1f%%)" % (
        ex8.raw / m.lines[("v8 as written", "exits")], ex9.raw / m.lines[("v9", "exits")],
        100.0 * ex9.raw / ex8.raw, 100.0 * ex9.gz / ex8.gz))
    pq = parquet_size(m.exit9, os.path.join(out_dir, "exits.v9.parquet"))
    if pq:
        share = len(m.exit9) / float(m.lines[("v9", "exits")])
        p("   Parquet (zstd) of the v9 exits: %s for %d records (%.0f%% of them); "
          "gzip JSONL of the same stream: %s" % (fmt(pq), len(m.exit9), 100 * share, fmt(ex9.gz)))

    p("\n## Cost per exit record (us; best of 3, %d records)" % len(m.exit8))
    b9 = [compact(r) for r in m.exit9]
    w8 = [collector_style(r) for r in m.exit8]
    if orjson is not None:
        cols = [k for k in m.cols if k != "event"]

        def positional(b):
            return dict(zip(cols, orjson.loads(b)))
        p("   dashboard parse, orjson.loads:   v8 %.2f   v9 %.2f   v9 positional, to a dict %.2f" % (
            timeit(orjson.loads, w8), timeit(orjson.loads, b9), timeit(positional, m.rows)))
    p("   dashboard parse, json.loads:     v8 %.2f   v9 %.2f" % (
        timeit(json.loads, w8), timeit(json.loads, b9)))
    p("   collector encode, json.dumps:    v8 %.2f   v9 %.2f   (v9 compact separators %.2f)" % (
        timeit(json.dumps, m.exit8), timeit(json.dumps, m.exit9),
        timeit(lambda r: json.dumps(r, separators=(",", ":")), m.exit9)))


def fmt(b):
    for unit in ("B", "KB", "MB", "GB"):
        if b < 1000 or unit == "GB":
            return "%.1f %s" % (b, unit) if unit != "B" else "%d B" % b
        b /= 1000.0


# ------------------------------------------------------------------------------ demo

def demo(out, hours, hosts, truncated=False):
    """A synthetic schema-8 feed from the dashboard's demo generator."""
    here = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, os.path.join(here, "..", "..", "dashboard"))
    from rc_dashboard.demo.feed import Feed
    from rc_dashboard.demo.world import World
    end = float(int(time.time()) // 60 * 60)
    t = end - hours * 3600
    w, f = World("demo", hosts, t), Feed(out)
    while t < end:
        t1 = min(end, t + 3600)
        f.append(w.records(t, t1))
        t = t1
    f.close()
    if truncated:
        print("added %d truncated record(s); %s's capture is leader-scoped"
              % add_stops(f.ebpf))
    print("wrote %s (ebpf root %s, node root %s)" % (out, f.ebpf, f.node))


def add_stops(root):
    """What the demo generator never writes, added to its feed (`demo --truncated`).

    Each host-day ends with a collector stop: a `truncated` record for each of up
    to six agent trees live at their last residency tick, written as the
    collector's `flush_live` writes one (no cpu_pct, a last /proc state, cpu_scope
    `process`, /proc counters with io.scope `proc`), one with no start and one
    with no /proc I/O. And the first host's capture is `leader`-scoped, as on a
    kernel without the `signal` block: its meta and its exits say `leader`, which
    its truncated records do not. Returns (records added, that host)."""
    files = day_files(root)
    leader = min((h for _d, h in files), default=None)
    added = 0
    for (_day, host), streams in files.items():
        snap, exits = streams.get("snapshot"), streams.get("exits")
        if not snap or not exits or snap.endswith(".gz") or exits.endswith(".gz"):
            continue
        srecs, erecs = read_records(snap), read_records(exits)
        if host == leader:
            for r in srecs:
                if r.get("event") == "meta":
                    r["cpu_scope"] = r["counters_scope"] = "leader"
            for r in erecs:
                if r.get("event") == "exit":
                    r["cpu_scope"] = "leader"
                    if r.get("io"):
                        r["io"]["scope"] = "leader"
        roots = {r["pid"]: r for r in srecs if r.get("event") == "residency"}
        metas = [r for r in srecs if r.get("event") == "meta"]
        t_stop = max(r["ts_epoch"] for r in srecs + erecs) + 1.0
        for i, r in enumerate(list(roots.values())[:6]):
            start_ep = r["ts_epoch"] - r["age_s"]
            known = i != 1                          # no exec time and no /proc start
            rec = {"ts": stamp(t_stop), "ts_epoch": t_stop, "host": host,
                   "event": "truncated",
                   "collector_pid": metas[-1]["collector_pid"] if metas
                   else r.get("collector_pid"),
                   "schema_version": r.get("schema_version")}
            if "synthetic" in r:
                rec["synthetic"] = r["synthetic"]
            rec.update((k, r.get(k)) for k in V8_IDENTITY)
            rec.update({
                "start": stamp(start_ep) if known else None,
                "duration_s": round(t_stop - start_ep, 4) if known else None,
                "duration_src": "proc", "cpu_s": r.get("cpu_s"), "cpu_scope": "process",
                "child_cpu_s": 0.25 * i, "rss_mb": round((r.get("rss_mb") or 0) + 0.01 * i, 2),
                "threads": r.get("threads"), "state_last": "RSDSSZ"[i],
                "min_flt": 1000 + i, "maj_flt": i,
                "dstate_wait_s": None, "dstate_episodes": None, "dstate_max_s": None,
                "dstate_src": None,
                "net_tx_bytes": 100 * i, "net_rx_bytes": 200 * i, "net_calls": i,
                "io": None if i == 2 else {"rd_mb": round(1.234 + i, 3), "wr_mb": 0.5,
                                           "rchar_mb": 2.0, "wchar_mb": 0.75,
                                           "scope": "proc"},
                "conns": None, "net_endpoints": None,
                "exit_code": None, "signal": None, "core_dumped": None, "samples": None})
            erecs.append(rec)
            added += 1
        for path, recs in ((snap, srecs), (exits, erecs)):
            with open(path, "w") as fh:
                for r in recs:
                    fh.write(json.dumps(r, separators=(",", ":")) + "\n")
    return added, leader


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("demo")
    d.add_argument("dir")
    d.add_argument("--hours", type=float, default=24)
    d.add_argument("--hosts", type=int, default=2)
    d.add_argument("--truncated", action="store_true",
                   help="end each host-day with a collector stop (truncated records) and "
                        "make the first host's capture leader-scoped (add_stops)")
    r = sub.add_parser("run")
    r.add_argument("ebpf_root")
    r.add_argument("out_dir")
    r.add_argument("--node")
    r.add_argument("--tz", help="the collector's time zone, an IANA name (default: this "
                                "process's)")
    a = ap.parse_args(argv)
    if a.cmd == "demo":
        return demo(a.dir, a.hours, a.hosts, a.truncated)
    if a.tz:
        global ZONE
        from zoneinfo import ZoneInfo
        ZONE = ZoneInfo(a.tz)
    return run(a.ebpf_root, a.out_dir, a.node)


if __name__ == "__main__":
    sys.exit(main())
