"""Profile the service's heavy paths in-process, on a bench feed.

    .venv/bin/python bench/hotpaths.py FEED [--what scan,hours,merge,filter,rerank,materialize,inspect]
                                           [--top 25] [--out DIR]

Builds the aggregator the service builds (`config.load()` from `RC_DASH_*`, the
clock pinned to the feed's end) without starting its threads, and drives each
path synchronously under `cProfile`:

* **scan**: the default window reduced by one pass over its records (no saved
  hours, no saved build) -- the per-record cost of a build;
* **hours**: the same window's closed hours reduced from an empty hour store;
* **merge**: the window merged from those saved hours, as a restart does;
* **filter**: the default window narrowed to the busiest user (always a scan);
* **rerank**: the trajectories' `result()`, as `/api/trajectories` re-ranks
  every few seconds, after records arrived;
* **materialize**: every reducer's `result()` and the masking, per reducer;
* **inspect**: `/api/trajectory`'s read for the busiest sessions of each kind.

Each prints its wall time and the functions it spent it in; `--out` keeps the
`.prof` files for `python -m pstats`.
"""
import argparse
import cProfile
import io
import os
import pstats
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DASH = os.path.dirname(HERE)
sys.path.insert(0, DASH)


def setup(feed, cache, merge_reduce=True, cache_windows=False):
    os.environ.update({
        "RC_DASH_EBPF_ROOTS": os.path.join(feed, "feed", "ebpf"),
        "RC_DASH_NODE_ROOTS": os.path.join(feed, "feed", "node"),
        "RC_DASH_CACHE_DIR": cache,
        "RC_DASH_AS_OF": "auto",
        "RC_DASH_ASK": "0",
        "RC_DASH_RDNS": "0",
        "RC_DASH_MERGE_REDUCE": "1" if merge_reduce else "0",
        "RC_DASH_CACHE_WINDOWS": "1" if cache_windows else "0",
        "RC_DASH_PREWARM": "",
    })
    from rc_dashboard import clock, config, feeds
    cfg = config.load()
    clock.install(cfg, feeds.resolve(cfg)["ebpf"]["resolved"])
    from rc_dashboard.aggregator import Aggregator
    agg = Aggregator(cfg)
    agg.positioned.set()
    return cfg, agg


def run(label, fn, top, out):
    pr = cProfile.Profile()
    t = time.monotonic()
    pr.enable()
    try:
        res = fn()
    finally:
        pr.disable()
    dt = time.monotonic() - t
    print("\n=== %s: %.2f s" % (label, dt))
    s = io.StringIO()
    st = pstats.Stats(pr, stream=s)
    st.sort_stats("tottime").print_stats(top)
    print("\n".join(s.getvalue().splitlines()[:top + 12]))
    if out:
        os.makedirs(out, exist_ok=True)
        pr.dump_stats(os.path.join(out, label.replace(" ", "_") + ".prof"))
    return res, dt


def build(agg, minutes, flt=None):
    reg = agg.panels_by_window
    view = reg.request(minutes, flt)
    reg._build(view)
    if view.state != "ready":
        raise RuntimeError("build did not finish: %s %s" % (view.state, view.error))
    return view


def busiest_user(view):
    fac = (view.payload or {}).get("facets") or {}
    users = sorted(fac.get("users") or [], key=lambda u: (-(u.get("n") or 0),
                                                          str(u.get("user") or u.get("value"))))
    return (users[0].get("user") or users[0].get("value")) if users else None


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("feed")
    ap.add_argument("--what", default="scan,hours,merge,filter,rerank,materialize,inspect")
    ap.add_argument("--top", type=int, default=25)
    ap.add_argument("--out", default=None)
    ap.add_argument("--work", default=None)
    ap.add_argument("--minutes", type=int, default=1440)
    a = ap.parse_args(argv)
    what = a.what.split(",")
    work = a.work or os.path.join(a.feed, "profile")
    times = {}

    if "scan" in what:
        cache = os.path.join(work, "cache-scan")
        shutil.rmtree(cache, ignore_errors=True)
        _cfg, agg = setup(a.feed, cache, merge_reduce=False)
        _v, times["scan"] = run("scan %dmin" % a.minutes, lambda: build(agg, a.minutes),
                                a.top, a.out)

    view = agg = None
    if any(w in what for w in ("hours", "merge", "filter", "rerank", "materialize", "inspect")):
        cache = os.path.join(work, "cache-hours")
        fresh = "hours" in what
        if fresh:
            shutil.rmtree(cache, ignore_errors=True)
        _cfg, agg = setup(a.feed, cache, merge_reduce=True)
        hrs = agg.panels_by_window.hours
        if fresh:
            def reduce_hours():
                now = time.time()
                lo = int((now - a.minutes * 60) // 3600) - 1
                hi = int(now // 3600)
                hrs.ensure(agg.roots(), lo, hi)
            _r, times["hours"] = run("hours cold reduction", reduce_hours, a.top, a.out)
        view, times["merge"] = run("merge %dmin from saved hours" % a.minutes,
                                   lambda: build(agg, a.minutes), a.top, a.out)

    if "filter" in what and view is not None:
        from rc_dashboard.windows import ViewFilter
        user = busiest_user(view)
        _v, times["filter"] = run("filter user=%s" % user,
                                  lambda: build(agg, a.minutes, ViewFilter([user])), a.top, a.out)

    if "rerank" in what and view is not None:
        traj = view.reducers["trajectories"]

        def rerank():
            for _ in range(3):
                traj.fed += 1          # as a live record would: the fold is made again
                traj.result()
        _r, dt = run("rerank x3", rerank, a.top, a.out)
        times["rerank (each)"] = dt / 3

    if "materialize" in what and view is not None:
        from rc_dashboard import redact
        per = {}
        t0 = time.monotonic()
        for key, r in view.reducers.items():
            t = time.monotonic()
            redact.payload(r.result())
            per[key] = round(time.monotonic() - t, 3)
        times["materialize"] = time.monotonic() - t0
        print("\n=== materialize: %.2f s  %s" % (times["materialize"],
                                                  sorted(per.items(), key=lambda kv: -kv[1])))

    if "inspect" in what and view is not None:
        from rc_dashboard import proctree
        rows = ((view.payload or {}).get("trajectories") or {}).get("rows") or {}
        picks, kinds = [], set()
        for sid in sorted(rows, key=lambda k: (-((rows[k] or {}).get("n_events") or 0), k)):
            kind = ((rows[sid] or {}).get("actor3"), (rows[sid] or {}).get("agent_type"))
            if kind not in kinds:
                kinds.add(kind)
                picks.append(sid)
        tty = [sid for sid, r in rows.items() if (r or {}).get("key_type") in ("tty", "sess")][:1]

        def inspect_all():
            for sid in picks[:4] + tty:
                with proctree._cache_lock:
                    proctree._cache.clear()
                t = time.monotonic()
                proctree.serve(agg, sid, a.minutes)
                print("  inspect %-60s %.2f s" % (sid, time.monotonic() - t))
        _r, times["inspect"] = run("inspect", inspect_all, a.top, a.out)

    print("\nSUMMARY " + " ".join("%s=%.2fs" % kv for kv in times.items()))


if __name__ == "__main__":
    main()
