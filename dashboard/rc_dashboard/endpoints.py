"""The far ends of the network bytes, named and ranked when the panel is read.

The I/O reducer adds up, per remote address and class, the bytes and the send
and receive calls that each process's `net_endpoints` block lists
(reducers/io.py). This module turns those address rows into the Network bytes
panel's list, on the way out, in three steps:

* **Names.** An address is named by a reverse-DNS (PTR) lookup made from this
  service's host, else by a name a process's own command line gave for it (the
  collector's `host`), else it is listed as the address. Lookups run on a few
  background threads, never on a request: an address not looked up yet is
  listed by what is known and counted in `pending`, and the page reads the
  panel again a little later. Answers are kept in <cache.dir>/rdns.json, so a
  restart does not ask again. A name is asked for again after a week, "no name"
  after a day, and a lookup that failed (a timeout, a server error) after ten
  minutes; the old answer is served in the meantime. The lookups are this
  host's, whatever the capture: a private address in another site's capture is
  named as this host's resolver names it.

* **Grouping.** Addresses with one name are one row, inbound kept apart from
  outbound: an inbound endpoint's address is the client's.

* **Ranking.** The rows are the union of the top N by calls and the top N by
  bytes (sent plus received) for every mix of classes. The page ranks the
  classes its class choice shows, and the union holds that mix's whole top N,
  so its ranking is exact without every name being sent.

`server.rdns.enabled: false` turns the lookups off; names then come from the
records alone. Nothing here is in a saved state's fingerprint (snapshots.py): a
name is applied when the panel is read, so turning lookups off, or a name
changing, costs a restart at most and never a rebuild.
"""
import collections
import heapq
import ipaddress
import itertools
import json
import os
import queue
import re
import socket
import tempfile
import threading
import time

TOP = 10
TTL_S = 7 * 86400            # a name
MISS_TTL_S = 86400           # an address the DNS says has no name
RETRY_S = 600                # a lookup that got no answer
SAVE_EVERY_S = 30            # at most this often while lookups keep landing
MAX_ENTRIES = 200000         # kept in rdns.json; the ones expiring last stay
ADDRS_SHOWN = 5              # addresses listed per row, the busiest first
PORTS_SHOWN = 3

# herror codes that are the DNS's answer, rather than a failure to get one
_NO_NAME = (1, 4)            # HOST_NOT_FOUND, NO_DATA
_HOSTNAME = re.compile(r"[a-z0-9_](?:[a-z0-9_.-]*[a-z0-9_])?")
# A PTR record written without its final dot gets its zone appended:
# `nfs05.example.edu.20.2.0.192.in-addr.arpa`.
_ARPA_TAIL = re.compile(r"(?:\.\d{1,3})+\.in-addr\.arpa$|(?:\.[0-9a-f])+\.ip6\.arpa$")
# Where a name came from, best first.
SOURCES = ("dns", "argv", "address")

NOTE = ("The remote ends of the network bytes, counted as each process exited, like the "
        "totals. A row is one name: reverse DNS from the dashboard's host, else a name a "
        "process's command line gave, else the address. `calls` are send and receive "
        "calls; `tx`/`rx` are bytes sent and received, per class. `rows` holds the top "
        "`top` by calls and by bytes for every mix of classes; `cover` is what the list "
        "cannot show: `lost_*` the bytes and calls a block held with no address listed, "
        "`missing` the processes that moved bytes with no endpoint block at all. `procs` "
        "sums the process records that listed each of a row's addresses, so a row of "
        "several addresses can count one process more than once.")


def clean_name(name, addr):
    """A PTR answer as the page shows it, or None when it is no name."""
    if not isinstance(name, str):
        return None
    n = name.strip().rstrip(".").lower()
    if n.endswith(".arpa"):
        cut = _ARPA_TAIL.sub("", n)
        # the arpa name alone, which cuts to its first label, is no name
        n = cut if cut != n and not re.fullmatch(r"\d+|[0-9a-f]", cut) else ""
    if not n or n == str(addr).lower() or not _HOSTNAME.fullmatch(n):
        return None
    return n


def _target(addr):
    """The address to ask about, or None for a string that is not one."""
    try:
        ip = ipaddress.ip_address(str(addr).split("%", 1)[0])
    except ValueError:
        return None
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return str(ip)


def _gethostbyaddr(addr):
    return socket.gethostbyaddr(addr)[0]


def _write_json(path, obj):
    d = os.path.dirname(path) or "."
    try:
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".rdns.", dir=d)      # mode 0600
    except OSError:
        return
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(obj, fh, separators=(",", ":"))
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass


class Resolver:
    """Reverse-DNS names for addresses, looked up off the request path.

    `names` answers from what is known and queues the rest for `workers`
    threads, started at the first miss. `lookup` and `now` are for tests.
    """

    def __init__(self, path=None, workers=4, lookup=None, now=time.time, ttl_s=TTL_S,
                 miss_ttl_s=MISS_TTL_S, retry_s=RETRY_S):
        self.path = path
        self.workers = max(1, int(workers))
        self.lookup = lookup or _gethostbyaddr
        self.now = now
        self.ttl_s, self.miss_ttl_s, self.retry_s = ttl_s, miss_ttl_s, retry_s
        self.lock = threading.Lock()
        self.idle = threading.Condition(self.lock)
        self.save_lock = threading.Lock()
        # addr -> [name or None, expires, kept on disk]
        self.cache = {}
        self.queued = set()
        self.q = queue.Queue()
        self.threads = []
        self.dirty = False
        self.saved_at = 0.0
        self.closed = False
        self._load()

    def names(self, addrs):
        """({addr: name or None} for every address with an answer, {the addresses
        still being looked up}). Never waits: a miss is queued, in the order
        asked, and a stale answer is served while it is asked for again."""
        known, pending, want = {}, set(), []
        with self.lock:
            now = self.now()
            for a in addrs:
                if a in known or a in pending:
                    continue
                hit = self.cache.get(a)
                if hit is not None:
                    known[a] = hit[0]
                    if hit[1] <= now and a not in self.queued:
                        want.append(a)
                elif _target(a) is None:
                    known[a] = None             # not an address: nothing to ask
                else:
                    pending.add(a)
                    if a not in self.queued:
                        want.append(a)
            if want and not self.closed:
                for a in want:
                    self.queued.add(a)
                    self.q.put(a)
                while len(self.threads) < self.workers:
                    t = threading.Thread(target=self._work, daemon=True,
                                         name="rdns-%d" % len(self.threads))
                    t.start()
                    self.threads.append(t)
        return known, pending

    def _ask(self, a):
        """(name or None, seconds it holds, whether to keep it on disk)."""
        try:
            name = clean_name(self.lookup(_target(a)), a)
        except socket.herror as e:
            if e.errno in _NO_NAME:
                return None, self.miss_ttl_s, True
            return None, self.retry_s, False
        except Exception:               # a timeout, a server error: try again later
            return None, self.retry_s, False
        return (name, self.ttl_s, True) if name else (None, self.miss_ttl_s, True)

    def _work(self):
        while True:
            a = self.q.get()
            if a is None or self.closed:
                return
            name, hold, keep = self._ask(a)
            with self.lock:
                old = self.cache.get(a)
                if not keep and old is not None:
                    # no answer this time: what was known stands until the retry
                    self.cache[a] = [old[0], self.now() + hold, old[2]]
                else:
                    self.cache[a] = [name, self.now() + hold, keep]
                self.queued.discard(a)
                self.dirty = self.dirty or keep
                done = not self.queued
                if done:
                    self.idle.notify_all()
            if self.closed:
                return
            self._save(force=done)

    def _load(self):
        if not self.path:
            return
        try:
            with open(self.path) as fh:
                got = json.load(fh).get("names") or {}
        except (OSError, ValueError, AttributeError):
            return
        for a, v in got.items():
            if (isinstance(a, str) and isinstance(v, list) and len(v) == 2
                    and isinstance(v[1], (int, float))):
                self.cache[a] = [clean_name(v[0], a), float(v[1]), True]

    def _save(self, force=False):
        if not self.path:
            return
        with self.save_lock:
            with self.lock:
                if not self.dirty or (not force and self.now() - self.saved_at < SAVE_EVERY_S):
                    return
                keep = sorted(((a, v) for a, v in self.cache.items() if v[2]),
                              key=lambda kv: -kv[1][1])[:MAX_ENTRIES]
                self.dirty = False
                self.saved_at = self.now()
            _write_json(self.path, {"v": 1, "names": {a: [v[0], v[1]] for a, v in keep}})

    def wait_idle(self, timeout=None):
        """Until nothing is queued, or `timeout` seconds; False if it ran out."""
        end = None if timeout is None else time.monotonic() + timeout
        with self.lock:
            while self.queued:
                left = None if end is None else end - time.monotonic()
                if left is not None and left <= 0:
                    return False
                self.idle.wait(left)
        return True

    def close(self, wait_s=1.0):
        """Stop the workers, and save what they found. What is still queued is
        dropped (the next start asks again), and a worker still waiting on a
        slow lookup is left to it: they are daemon threads, and each batch was
        saved as it finished, so a shutdown waits `wait_s` at most."""
        with self.lock:
            if self.closed:
                return
            self.closed = True
            threads = list(self.threads)
            while True:
                try:
                    self.q.get_nowait()
                except queue.Empty:
                    break
            self.queued.clear()
            self.idle.notify_all()
        for _ in threads:
            self.q.put(None)
        end = time.monotonic() + wait_s
        for t in threads:
            t.join(timeout=max(0.0, end - time.monotonic()))
        self._save(force=True)


def from_config(cfg):
    """The service's resolver, or None when `server.rdns.enabled` is off."""
    if not cfg.get("server.rdns.enabled", True):
        return None
    d = cfg.get("cache.dir")
    return Resolver(path=os.path.join(d, "rdns.json") if d else None,
                    workers=cfg.get("server.rdns.workers", 4) or 4)


def _calls(r):
    return sum(by["calls"] for by in r["by_class"].values())


def _bytes(r):
    return sum(by["tx"] + by["rx"] for by in r["by_class"].values())


def lookup_order(rows):
    """Every address once, the most calls and the most bytes in turn, so the
    endpoints the page shows first are the ones named first."""
    by_calls = [r["addr"] for r in rows]          # the reducer's order
    by_bytes = [r["addr"] for r in sorted(
        rows, key=lambda r: (-_bytes(r), -_calls(r), r["addr"], r["inbound"]))]
    out, seen = [], set()
    for pair in itertools.zip_longest(by_calls, by_bytes):
        for a in pair:
            if a is not None and a not in seen:
                seen.add(a)
                out.append(a)
    return out


def _busiest(counts, n):
    return [k for k, _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:n]]


def fold(block, names, pending, classes, top=TOP):
    """Address rows -> rows by name, cut to every class mix's top `top`.

    `names` maps an address to its reverse-DNS name (None: it has none) and
    `pending` holds the addresses still being looked up."""
    groups = {}
    addrs = set()
    for r in block.get("rows") or ():
        addr = r["addr"]
        addrs.add(addr)
        if names.get(addr):
            name, source = names[addr], "dns"
        elif r.get("hosts"):
            name, source = next(iter(r["hosts"])), "argv"
        else:
            name, source = addr, "address"
        key = (name, bool(r.get("inbound")))
        g = groups.get(key)
        if g is None:
            g = groups[key] = {"name": name, "source": source, "inbound": key[1],
                               "external": False, "provider": None, "procs": 0,
                               "pending": False, "by_class": {},
                               "_addrs": collections.Counter(), "_ports": collections.Counter()}
        elif SOURCES.index(source) < SOURCES.index(g["source"]):
            g["source"] = source
        for c, by in r["by_class"].items():
            m = g["by_class"].setdefault(c, {"calls": 0, "tx": 0, "rx": 0})
            for k in ("calls", "tx", "rx"):
                m[k] += by[k]
        g["_addrs"][addr] += _calls(r)
        g["_ports"].update(r.get("ports") or {})
        # exact for one address; summed over several, a process that reached
        # two of them counts twice (who reached which is not kept)
        g["procs"] += r.get("procs") or 0
        g["external"] = g["external"] or bool(r.get("external"))
        g["provider"] = g["provider"] or r.get("provider")
        g["pending"] = g["pending"] or addr in pending

    seen_cls = {c for g in groups.values() for c in g["by_class"]}
    present = [c for c in classes if c in seen_cls] + sorted(seen_cls - set(classes))
    glist = list(groups.values())
    # per group, per present class: (calls, bytes)
    vals = [[(g["by_class"][c]["calls"], g["by_class"][c]["tx"] + g["by_class"][c]["rx"])
             if c in g["by_class"] else (0, 0) for c in present] for g in glist]
    keep = set()
    for k in range(1, len(present) + 1):
        for mix in itertools.combinations(range(len(present)), k):
            sums = [(sum(v[j][0] for j in mix), sum(v[j][1] for j in mix)) for v in vals]
            for first in (0, 1):                     # by calls, by bytes
                cands = [i for i, s in enumerate(sums) if s[first] > 0]
                keep.update(heapq.nsmallest(top, cands, key=lambda i: (
                    -sums[i][first], -sums[i][1 - first], glist[i]["name"], glist[i]["inbound"])))
    rows = []
    for i in sorted(keep, key=lambda i: (-_calls(glist[i]), -_bytes(glist[i]),
                                         glist[i]["name"], glist[i]["inbound"])):
        g = glist[i]
        row = {k: v for k, v in g.items() if not k.startswith("_")}
        row["by_class"] = {c: g["by_class"][c] for c in present if c in g["by_class"]}
        row["addrs"] = _busiest(g["_addrs"], ADDRS_SHOWN)
        row["n_addrs"] = len(g["_addrs"])
        row["ports"] = _busiest(g["_ports"], PORTS_SHOWN)
        rows.append(row)
    return {"rows": rows, "top": top, "classes": present, "names": len(groups),
            "addresses": len(addrs), "pending": len(addrs & set(pending)),
            "cover": block.get("cover") or {}}


def serve(body, resolver, classes, top=TOP):
    """An io_process body as it goes out: `endpoint_addrs` replaced by
    `endpoints`, the named and ranked list. `body` is the caller's own copy."""
    block = body.pop("endpoint_addrs", None)
    if not isinstance(block, dict):
        return body
    rows = block.get("rows") or []
    names, pending = resolver.names(lookup_order(rows)) if resolver is not None else ({}, set())
    out = fold(block, names, pending, classes, top=top)
    out["rdns"] = resolver is not None
    body["endpoints"] = out
    body["notes"] = dict(body.get("notes") or {}, endpoints=NOTE)
    return body
