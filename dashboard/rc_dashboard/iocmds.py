"""Every command behind an I/O number, looked up when it is clicked.

A panel's I/O figure -- disk bytes, syscall bytes, network bytes -- is a sum,
and the reducers drop the command lines as they add them up. Clicking one asks
this module for them: the window's exit records are read again from the
day-files, the ones the click names are kept (its class, user, agent type,
tool, tool kind, agent session, directory, time bin), and they are grouped by
command line, every one of them, none cut.

Reading again costs the window's bytes: about 3 s for a day of rtx6000a and
about a minute for a day of the FAS RC capture (6.2 GB of gzip, 2.8 M exits).
So a lookup is a job the page polls, saying how far it has read. A finished
one is kept, and the next reader who asks the same thing gets it at once: for
good on a pinned clock, whose day-files cannot change, and for `TTL_LIVE`
seconds on a live feed.

Nothing here is part of a window build, so changing it rebuilds no window
(`snapshots.CODE_FILES`). The filters are `/api/events`' own
(`export.parse_filters` and `make_predicate`), plus the two a blast-radius row
needs: `apid` (the agent pids whose processes count, on one `host`) and `cwd`
(a directory: processes that ran in it or below it).
"""
import json
import re
import threading
import time
import uuid

from . import clock, export
from .proctree import _trie
from .normalize import Normalizer
from .redact import redact

EVENTS = frozenset({"exit", "truncated"})

# what a page can be sorted by, and the row field each reads
SORT = ("cmd", "tool", "n", "rd_mb", "wr_mb", "rchar_mb", "wchar_mb",
        "tx_mb", "rx_mb", "cpu_s", "last", "disk_mb", "net_mb")

CMD_MAX = 2000        # characters of one command line served; the rest is cut
MAX_COMMANDS = 1_000_000   # distinct commands held; past it they are counted
PAGE_MAX = 1000       # rows one page may ask for
KEEP = 6              # finished lookups held
TTL_LIVE = 120        # seconds a finished lookup answers again on a live feed
WORKERS = 2           # lookups reading at once; the rest wait their turn

# the group's slots, one list per distinct command line
N, IO_N, RD, WR, RCHAR, WCHAR, TX, RX, CPU, TOOL, USERS, FIRST, LAST = range(13)


def _q(v):
    return re.escape(v.encode())


def prefilter(f, apid, host, cwd):
    """A byte check on a raw line that never misses a record the filters keep,
    or None when none of them can be checked on the bytes.

    It is what makes a narrow click cheap: one user's lines, or one agent's,
    are found without parsing the rest. A false hit only costs a parse; the
    predicate still decides.
    """
    checks = []
    users = f.get("user")
    if users:
        checks.append(re.compile(rb'"user"\s*:\s*"(?:' + b"|".join(_q(u) for u in sorted(users))
                                 + rb')"').search)
    if apid:
        # a trie, as proctree's: a Claude session's hundreds of tool-call keys
        # as a flat alternation would be tried one by one on every line
        alt = _trie(sorted(apid))
        sk = re.compile(rb'"session_key"\s*:\s*"(?:[^":]*:)?' + alt + rb'"').search
        ap = re.compile(rb'"agent_pid"\s*:\s*"?' + alt + rb'(?![0-9])').search
        checks.append(lambda line: sk(line) or ap(line))
    # only a plain path: one JSON would escape is left to the predicate
    if cwd and json.dumps(cwd)[1:-1] == cwd:
        checks.append(re.compile(rb'"cwd"\s*:\s*"' + _q(cwd.rstrip("/") or "/")).search)
    if host:
        checks.append(re.compile(rb'"host"\s*:\s*"' + _q(host) + rb'"').search)
    if not checks:
        return None
    if len(checks) == 1:
        return checks[0]
    return lambda line: all(c(line) for c in checks)


def agent_keys(view, host, pids):
    """The session keys whose processes count as these agents', folded as the
    blast radius folds them (`Workspace.fold`): each agent's own pid, and every
    tool call's key the window's process index walks up to one of them. A
    Claude Code session can hold hundreds, one per tool call.

    `view` is the held window the page is showing; without one, or before its
    workspace is built, only the agents' own keys are known."""
    want = {int(p) for p in pids}
    out = {str(p) for p in want}
    ws = getattr((view.reducers.get("cascades") if view is not None else None), "ws", None)
    if ws is None:
        return out
    for (h, kv), st in list(ws.keys.items()):
        if h != host:
            continue
        try:
            kpid = int(kv)
        except (TypeError, ValueError):
            continue
        owner = ws.procs.owner(h, kpid, st.root)[0] if ws.procs is not None else kpid
        if owner in want:
            out.add(str(kv))
    return out


def predicate(f, apid, host, cwd):
    """`make_predicate` with the blast radius's two filters added."""
    base = export.make_predicate(f)
    root = cwd.rstrip("/") if cwd else None

    def ok(rec):
        if not base(rec):
            return False
        if host and rec.get("host") != host:
            return False
        if apid and (rec.get("_kt") != "apid" or str(rec.get("_kv")) not in apid):
            return False
        if root is not None:
            c = rec.get("cwd") or ""
            if not (c == root or c.startswith(root + "/") or (root == "" and c.startswith("/"))):
                return False
        return True
    return ok


def _stamp(e):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(e)) if e is not None else None


class Job:
    """One lookup: its query, how far it has read, and, done, its commands."""

    def __init__(self, key, query, since, until):
        self.id = uuid.uuid4().hex[:12]
        self.key = key
        self.query = query
        self.since, self.until = since, until
        self.state = "queued"
        self.error = None
        self.total_bytes = 0
        self.stats = {"bytes": 0, "scanned": 0, "skipped": 0, "parse_errors": 0,
                      "no_ts": 0, "matched": 0}
        self.t0 = time.monotonic()
        self.elapsed = None
        self.done_at = None        # wall clock, for TTL_LIVE
        self.groups = None         # cmd -> slots, while reading
        self.rows = None           # [(cmd, slots)], done
        self.orders = {}           # (sort, dir) -> rows in that order
        self.totals = None
        self.over = 0              # commands past MAX_COMMANDS: counted, not listed

    def status(self):
        el = self.elapsed if self.elapsed is not None else time.monotonic() - self.t0
        out = {"id": self.id, "state": self.state, "error": self.error,
               "query": self.query, "from": _stamp(self.since), "to": _stamp(self.until),
               "bytes": self.stats["bytes"], "total_bytes": self.total_bytes,
               "scanned": self.stats["scanned"], "matched": self.stats["matched"],
               "elapsed_s": round(el, 1)}
        if self.state == "done":
            out["totals"] = self.totals
            out["commands"] = len(self.rows)
            out["commands_over"] = self.over
        return out

    def page(self, sort, desc, offset, limit):
        k = SORT.index(sort) if sort in SORT else SORT.index("rd_mb")
        o = self.orders.get((k, desc))
        if o is None:
            if SORT[k] == "cmd":
                o = sorted(self.rows, key=lambda r: r[0], reverse=desc)
            elif SORT[k] == "tool":
                o = sorted(self.rows, key=lambda r: (r[1][TOOL] or "", r[0]), reverse=desc)
            elif SORT[k] in ("disk_mb", "net_mb"):
                # read and written together, as Tool mix and Top tools sum them
                a, b = (RD, WR) if SORT[k] == "disk_mb" else (TX, RX)
                o = sorted(self.rows, key=lambda r: r[1][a] + r[1][b], reverse=desc)
            else:
                slot = {"n": N, "rd_mb": RD, "wr_mb": WR, "rchar_mb": RCHAR, "wchar_mb": WCHAR,
                        "tx_mb": TX, "rx_mb": RX, "cpu_s": CPU, "last": LAST}[SORT[k]]
                o = sorted(self.rows, key=lambda r: r[1][slot] or 0, reverse=desc)
            self.orders = {(k, desc): o}      # one order held: a page re-sorts on demand
        return [_row(c, g) for c, g in o[offset:offset + limit]]


def _row(cmd, g):
    shown = cmd if len(cmd) <= CMD_MAX else cmd[:CMD_MAX] + " …"
    return {"cmd": redact(shown), "cut": len(cmd) > CMD_MAX, "tool": g[TOOL],
            "n": g[N], "io_n": g[IO_N],
            "rd_mb": round(g[RD], 3), "wr_mb": round(g[WR], 3),
            "rchar_mb": round(g[RCHAR], 3), "wchar_mb": round(g[WCHAR], 3),
            "tx_mb": round(g[TX] / 1e6, 3), "rx_mb": round(g[RX] / 1e6, 3),
            "cpu_s": round(g[CPU], 3), "users": sorted(g[USERS]),
            "first": _stamp(g[FIRST]), "last": _stamp(g[LAST])}


class IoCommands:
    """The lookups: started, polled, paged, and the finished ones kept."""

    def __init__(self, cfg, roots):
        self.cfg = cfg
        self.roots = roots
        self.jobs = {}             # id -> Job, every one still held
        self.lock = threading.Lock()
        self.gate = threading.Semaphore(WORKERS)

    def start(self, f, window_min, apid=None, host=None, cwd=None):
        """A job for this query: one already reading or finished for the same
        question, else a new one, started."""
        now = time.time()
        lo, hi = export._epoch_bound(f.get("from")), export._epoch_bound(f.get("to"))
        until = min(hi, now) if hi is not None else now
        since = lo if lo is not None else until - 60 * int(window_min or 1440)
        apid = frozenset(str(p) for p in apid) if apid else None
        query = {k: (sorted(v) if isinstance(v, (set, frozenset)) else v)
                 for k, v in f.items() if v not in (None, set(), "")}
        query.update({k: v for k, v in (("apid", sorted(apid) if apid else None),
                                        ("host", host), ("cwd", cwd)) if v})
        # A live feed's "now" moves every second, so a query without its own
        # end is keyed on its span, and reused only while it is fresh.
        key = json.dumps([query, round(since) if lo is not None else None,
                          round(until) if hi is not None else None,
                          None if hi is not None or lo is not None else int(window_min or 1440)],
                         sort_keys=True, default=str)
        pinned = clock.status()["pinned"]
        with self.lock:
            for j in self.jobs.values():
                if j.key != key or j.state == "error":
                    continue
                if j.state != "done" or pinned or now - j.done_at < TTL_LIVE:
                    return j
            j = Job(key, query, since, until)
            self.jobs[j.id] = j
            self._trim()
        threading.Thread(target=self._run, args=(j, f, apid, host, cwd),
                         name="iocmds-" + j.id, daemon=True).start()
        return j

    def get(self, jid):
        with self.lock:
            return self.jobs.get(jid)

    def _trim(self):
        done = sorted((j for j in self.jobs.values() if j.state in ("done", "error")),
                      key=lambda j: j.done_at or 0)
        for j in done[:max(0, len(done) - KEEP)]:
            del self.jobs[j.id]

    def _run(self, j, f, apid, host, cwd):
        with self.gate:
            try:
                j.state = "reading"
                self._read(j, f, apid, host, cwd)
                j.state = "done"
            except Exception as e:          # said on the page, never swallowed
                j.error = "%s: %s" % (type(e).__name__, e)
                j.state = "error"
            finally:
                j.elapsed = time.monotonic() - j.t0
                j.done_at = time.time()
                with self.lock:
                    self._trim()

    def _read(self, j, f, apid, host, cwd):
        plan, j.total_bytes = export.window_plan(self.roots(), j.since)
        norm = Normalizer(self.cfg)
        ok = predicate(f, apid, host, cwd)
        by_event = export.event_filter(EVENTS)
        match = prefilter(f, apid, host, cwd)
        wanted = by_event if match is None else (
            lambda line: match(line) if by_event(line) is not None else None)
        groups = j.groups = {}
        tot = [0, 0, 0.0, 0.0, 0.0, 0.0, 0, 0, 0.0]
        st = j.stats
        # Files one after another, not merged on time: a sum has no order, and
        # a merge holds every file open to seed it.
        for entry in plan:
            for _ts, rec in export._window_stream(entry, norm, j.since, j.until, st, wanted):
                if rec.get("event") not in EVENTS or not ok(rec):
                    continue
                st["matched"] += 1
                cmd = rec.get("args") or rec.get("_command") or ""
                g = groups.get(cmd)
                if g is None:
                    if len(groups) >= MAX_COMMANDS:
                        j.over += 1
                        g = None
                    else:
                        g = groups[cmd] = [0, 0, 0.0, 0.0, 0.0, 0.0, 0, 0, 0.0,
                                           rec.get("_eff") or rec.get("_command"), set(), None, None]
                io = rec.get("io") or {}
                rd, wr = io.get("rd_mb") or 0.0, io.get("wr_mb") or 0.0
                rc, wc = io.get("rchar_mb") or 0.0, io.get("wchar_mb") or 0.0
                tx, rx = rec.get("net_tx_bytes") or 0, rec.get("net_rx_bytes") or 0
                cpu = rec.get("cpu_s") or 0.0
                has_io = io.get("rchar_mb") is not None
                for t in ((tot, g) if g is not None else (tot,)):
                    t[N] += 1
                    t[IO_N] += has_io
                    t[RD] += rd
                    t[WR] += wr
                    t[RCHAR] += rc
                    t[WCHAR] += wc
                    t[TX] += tx
                    t[RX] += rx
                    t[CPU] += cpu
                if g is not None:
                    u = rec.get("user")
                    if u:
                        g[USERS].add(u)
                    e = rec.get("_ts_epoch")
                    if e is not None:
                        if g[FIRST] is None or e < g[FIRST]:
                            g[FIRST] = e
                        if g[LAST] is None or e > g[LAST]:
                            g[LAST] = e
        j.rows = list(groups.items())
        j.groups = None
        j.totals = {"n": tot[N], "io_n": tot[IO_N], "rd_mb": round(tot[RD], 3),
                    "wr_mb": round(tot[WR], 3), "rchar_mb": round(tot[RCHAR], 3),
                    "wchar_mb": round(tot[WCHAR], 3), "tx_mb": round(tot[TX] / 1e6, 3),
                    "rx_mb": round(tot[RX] / 1e6, 3), "cpu_s": round(tot[CPU], 3)}
