"""The output guard: what the service serves over a bench feed, to compare across changes.

    .venv/bin/python bench/golden.py dump FEED OUT.json [--cache DIR]
    .venv/bin/python bench/golden.py compare A.json B.json

`dump` starts the service from an empty cache with the clock pinned to the feed's
end (`clock.as_of=auto`, so every build over the same bytes is the same build) and
the hash seed fixed, waits for the backfill and the default window, opens a
filtered view and the inspector on the busiest sessions, and writes every
response a page reads, with the keys that say when or how fast something ran
removed (`VOLATILE`). `compare` parses both and lists every difference: a speed-up
that changes a number the page shows is a regression, whatever it saves.
"""
import argparse
import json
import os
import shutil
import sys
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from svc import Service      # noqa: E402

# How long or when something ran, where it came from on this run, or how the
# process holds it: none of it is a number the page reports about the feed.
VOLATILE = frozenset((
    "elapsed_s", "last_ms", "last_pass_ms", "build_s", "saved_at", "panels_saved_at",
    "panels_note", "restored", "cache", "installed_at", "materialized_at", "built_age_s",
    "last_poll", "compressed", "elapsed_ms", "took_s", "scan_s",
    # the wall clock, which a pinned clock leaves running beside it
    "generated_at", "behind_wall_s",
))


def strip(o):
    if isinstance(o, dict):
        return {k: strip(v) for k, v in o.items() if k not in VOLATILE}
    if isinstance(o, list):
        return [strip(v) for v in o]
    return o


def q(params):
    return urllib.parse.urlencode(params, doseq=True)


# With --anon: names masked, under one fixed salt, so two runs alias alike.
SALT_HEX = "00" * 31 + "2a"


def dump(feed, out, cache, anon=False):
    if os.path.exists(cache):
        shutil.rmtree(cache)
    env = {}
    if anon:
        os.makedirs(cache, mode=0o700)
        fd = os.open(os.path.join(cache, "anonymize.salt"), os.O_WRONLY | os.O_CREAT, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(SALT_HEX + "\n")
        env["RC_DASH_ANONYMIZE"] = "1"
    got = {}
    with Service(feed, cache, as_of="auto", env=env) as s:
        s.wait(lambda: s.health()["backfill"].get("state") == "done", what="backfill")
        s.wait_ready(1440)
        panels = s.json("/api/panels?window_min=1440")
        got["panels"] = panels
        users = sorted((panels.get("facets") or {}).get("users") or [],
                       key=lambda u: (-(u.get("n") or 0), str(u.get("user") or u.get("value"))))
        user = (users[0].get("user") or users[0].get("value")) if users else None
        if user:
            s.json("/api/panels?" + q({"window_min": 1440, "user": user}))
            s.wait_ready(1440, {"users": [user], "agent_types": []})
            got["panels_user"] = s.json("/api/panels?" + q({"window_min": 1440, "user": user}))
            got["trajectories_user"] = s.json("/api/trajectories?" + q({"window_min": 1440,
                                                                        "user": user}))
        tr = s.json("/api/trajectories?rank=events&window_min=1440")
        got["trajectories"] = tr
        # the busiest session of each kind (class, agent type), so the inspector is
        # read for an agent with phases, an IDE, and a person, not three clones of one
        rows = tr.get("rows") or {}
        sids, kinds = [], set()
        for sid in sorted(rows, key=lambda k: (-((rows[k] or {}).get("n_events") or 0), k)):
            kind = ((rows.get(sid) or {}).get("actor3"), (rows.get(sid) or {}).get("agent_type"))
            if kind not in kinds:
                kinds.add(kind)
                sids.append(sid)
        sids = sids[:4]
        tty = [sid for sid, r in (tr.get("rows") or {}).items()
               if (r or {}).get("key_type") in ("tty", "sess")][:1]
        for sid in sids + tty:
            for order in ("oldest", "newest"):
                got["trajectory %s %s" % (sid, order)] = s.json(
                    "/api/trajectory?" + q({"sid": sid, "window_min": 1440, "order": order}))
        got["live"] = s.json("/api/live")
        got["live_1h"] = s.json("/api/live?window_min=60")
        got["users"] = s.json("/api/users?window_min=1440")
        # each host's verdict; not `tiers`, the feeds' freshness as of the last
        # report refresh (every 30 s, and by file mtime), which is when, not what
        got["collectors"] = {k: v for k, v in s.json("/api/collectors").items() if k != "tiers"}
        got["events"] = s.json("/api/events?limit=300")
        # the rows, not the `_meta` header: it carries the feed report as of its last
        # refresh (schema counts, paths), which is when the export ran, not what it holds
        rows = [json.loads(x) for x in s.lines("/api/export", 5000) if x.strip()]
        got["export"] = [r for r in rows if "_meta" not in r]
        got["_meta"] = {"user": user, "sids": sids, "tty": tty}
    with open(out, "w") as fh:
        json.dump(strip(got), fh, sort_keys=True)
    print("wrote %s (%d responses)" % (out, len(got)))


def diff(a, b, path="", out=None, cap=60):
    out = [] if out is None else out
    if len(out) >= cap:
        return out
    if type(a) is not type(b) and not (isinstance(a, (int, float)) and isinstance(b, (int, float))):
        out.append("%s: %r != %r" % (path or "/", _short(a), _short(b)))
    elif isinstance(a, dict):
        for k in sorted(set(a) | set(b)):
            if k not in a or k not in b:
                out.append("%s/%s: only in %s" % (path, k, "A" if k in a else "B"))
            else:
                diff(a[k], b[k], "%s/%s" % (path, k), out, cap)
    elif isinstance(a, list):
        if len(a) != len(b):
            out.append("%s: %d items != %d items" % (path, len(a), len(b)))
        for i, (x, y) in enumerate(zip(a, b)):
            diff(x, y, "%s[%d]" % (path, i), out, cap)
    elif a != b:
        out.append("%s: %r != %r" % (path or "/", _short(a), _short(b)))
    return out


def _short(v):
    s = repr(v)
    return s if len(s) < 120 else s[:117] + "..."


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("dump")
    d.add_argument("feed")
    d.add_argument("out")
    d.add_argument("--cache", default=None)
    d.add_argument("--anon", action="store_true", help="names masked (anonymize.enabled)")
    c = sub.add_parser("compare")
    c.add_argument("a")
    c.add_argument("b")
    a = ap.parse_args(argv)
    if a.cmd == "dump":
        dump(a.feed, a.out, a.cache or os.path.join(a.feed, "cache-golden"), anon=a.anon)
        return 0
    with open(a.a) as fa, open(a.b) as fb:
        # stripped again, so a key found volatile later applies to dumps already made
        A, B = strip(json.load(fa)), strip(json.load(fb))
    out = diff(A, B)
    for line in out:
        print(line)
    print("IDENTICAL" if not out else "%d+ difference(s)" % len(out))
    return 1 if out else 0


if __name__ == "__main__":
    sys.exit(main())
