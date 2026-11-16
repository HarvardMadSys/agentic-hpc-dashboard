"""Write a synthetic collector feed at fleet density, for the bench.

    .venv/bin/python bench/amplify.py OUT [--days 2] [--hosts 4] [--host-clones 2]
                                          [--density 16] [--end EPOCH] [--gzip-sealed]

The demo generator (`rc_dashboard/demo`) writes a realistic feed for a few
fictional login nodes, but at ~250 records a host-hour, where a busy login node
writes ~80,000. This clones it toward real density without changing its shape:

* **host clones** copy each demo host under `<host>-c<j>`, a node of its own;
* **density clones** copy each record of a session K-1 more times within its host,
  with every process and session identity moved out of the others' way -- pids
  (and parents, tree roots, agent pids, process groups, session ids) by 10M x k,
  the cgroup's session scope, the tty, the session uuid -- so a clone is a session
  of its own, and its tree links to the same clone of its parents. Host-level
  records (`residency_totals`, `meta`, `stop`) are not multiplied within a host.

Lines are written as the collector writes them (`json.dumps`, default
separators), in time order, one file per host, stream and local day under
`OUT/feed/ebpf/<YYYY-MM-DD>/` and `OUT/feed/node/<YYYY-MM-DD>/`. `--gzip-sealed`
compresses every day before the newest, as the fleet stream keeps them
(`keep_gz`) and as a capture copied from another cluster arrives.

The feed is a pure function of its arguments: `--end` fixes the newest record,
so the same command writes the same bytes (with the default `--end`, "now",
that is a fresh feed each time, which the live page test wants).
"""
import argparse
import gzip
import json
import os
import re
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from rc_dashboard.demo.world import World      # noqa: E402

PID_KEYS = ("pid", "ppid", "tree_root_pid", "agent_pid", "pgrp", "sid")
PID_OFF = 10_000_000
HOST_LEVEL = frozenset(("residency_totals", "meta", "stop"))
_SCOPE = re.compile(r"session-(\d+)\.scope")


def day_of(epoch):
    return time.strftime("%Y-%m-%d", time.localtime(epoch))


def clone(rec, host, k):
    """Record `rec` as density clone `k` on `host` (k=0 is the record itself)."""
    r = dict(rec)
    r["host"] = host
    if not k:
        return r
    d = k * PID_OFF
    for key in PID_KEYS:
        v = r.get(key)
        if isinstance(v, int) and v > 1:          # 0 and 1 (no parent, init) stay
            r[key] = v + d
    sk = r.get("session_key")
    if isinstance(sk, str) and sk.startswith("apid:") and sk[5:].isdigit():
        r["session_key"] = "apid:%d" % (int(sk[5:]) + d)
    cg = r.get("cgroup")
    if isinstance(cg, str) and "session-" in cg:
        r["cgroup"] = _SCOPE.sub(lambda m: "session-%d.scope" % (int(m.group(1)) + k * 100000), cg)
    tty = r.get("tty")
    if isinstance(tty, str) and tty.startswith("pts/") and tty[4:].isdigit():
        r["tty"] = "pts/%d" % (int(tty[4:]) + k * 1000)
    su = r.get("session_uuid")
    if isinstance(su, str):
        r["session_uuid"] = "%s%02x" % (su, k)
    return r


class Writer:
    """Append lines to `<root>/<day>/<name>`, keeping a few files open."""

    def __init__(self, base):
        self.base = base
        self.fh = {}
        self.lines = 0
        self.bytes = 0

    def write(self, root, epoch, name, line):
        p = os.path.join(self.base, "feed", root, day_of(epoch), name)
        fh = self.fh.get(p)
        if fh is None:
            if len(self.fh) > 256:
                self.close()
            os.makedirs(os.path.dirname(p), exist_ok=True)
            fh = self.fh[p] = open(p, "ab", buffering=1 << 20)
        fh.write(line)
        self.lines += 1
        self.bytes += len(line)

    def close(self):
        for fh in self.fh.values():
            fh.close()
        self.fh.clear()


def emit(w, items, hosts, density):
    """Write one batch of demo items, amplified. `hosts` maps a demo host to its
    clones' names."""
    for epoch, tier, host, stream, line in items:
        rec = json.loads(line)
        if tier == "node":
            for h in hosts[host]:
                r = dict(rec, hostname=h)
                w.write("node", epoch, "%s.jsonl" % h, (json.dumps(r) + "\n").encode())
            continue
        ks = 1 if rec.get("event") in HOST_LEVEL else density
        for h in hosts[host]:
            name = "%s.%s.jsonl" % (h, stream)
            for k in range(ks):
                w.write("ebpf", epoch, name, (json.dumps(clone(rec, h, k)) + "\n").encode())


def gzip_sealed(base):
    """Compress every day-file of every day before the newest, as the fleet keeps them."""
    n = 0
    for root in ("ebpf", "node"):
        d = os.path.join(base, "feed", root)
        if not os.path.isdir(d):
            continue
        days = sorted(x for x in os.listdir(d) if re.fullmatch(r"\d{4}-\d{2}-\d{2}", x))
        for day in days[:-1]:
            for name in sorted(os.listdir(os.path.join(d, day))):
                p = os.path.join(d, day, name)
                if not name.endswith(".jsonl"):
                    continue
                with open(p, "rb") as src, gzip.open(p + ".gz", "wb", compresslevel=6) as dst:
                    shutil.copyfileobj(src, dst, 1 << 20)
                os.unlink(p)
                n += 1
    return n


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("out")
    ap.add_argument("--days", type=float, default=2.0)
    ap.add_argument("--hosts", type=int, default=4, help="demo hosts (1-6)")
    ap.add_argument("--host-clones", type=int, default=2)
    ap.add_argument("--density", type=int, default=16, help="clones per session record")
    ap.add_argument("--seed", default="bench")
    ap.add_argument("--end", type=float, default=None,
                    help="epoch of the newest record (default: now, to the minute)")
    ap.add_argument("--gzip-sealed", action="store_true")
    a = ap.parse_args(argv)

    end = float(int(a.end if a.end is not None else time.time()) // 60 * 60)
    start = end - a.days * 86400
    if os.path.exists(os.path.join(a.out, "feed")):
        shutil.rmtree(os.path.join(a.out, "feed"))
    world = World(a.seed, a.hosts, start)
    hosts = {h: [h] + ["%s-c%d" % (h, j) for j in range(1, a.host_clones)] for h in world.hosts}
    w = Writer(a.out)
    t0 = time.monotonic()
    t = start
    while t < end:
        t1 = min(end, t + 3600)
        emit(w, world.records(t, t1), hosts, a.density)
        t = t1
    w.close()
    gz = gzip_sealed(a.out) if a.gzip_sealed else 0
    meta = {"start": start, "end": end, "days": a.days, "hosts": sum(len(v) for v in hosts.values()),
            "demo_hosts": a.hosts, "host_clones": a.host_clones,
            "density": a.density, "seed": a.seed, "lines": w.lines, "bytes": w.bytes,
            "gzipped_files": gz,
            "elapsed_s": round(time.monotonic() - t0, 1)}
    with open(os.path.join(a.out, "feed.json"), "w") as fh:
        json.dump(meta, fh, indent=1)
    print(json.dumps(meta))


if __name__ == "__main__":
    main()
