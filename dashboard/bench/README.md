# The bench

Tools to measure the dashboard, and to show a change kept the answers it serves. Each
tool's docstring shows its usage and `--help` lists every option (not `stallwatch.py`'s: its
first argument is the file it writes, `--help` included); this page is how they fit together,
and what to watch for. Run them from `dashboard/`, with the project's venv (`uv sync`).

| tool | what it does |
|---|---|
| `amplify.py` | writes a synthetic collector feed at fleet density, cloned from the demo generator's |
| `golden.py` | dumps the main responses of a 24 h window (panels, a filtered view, sessions, the inspector, live, users, collectors, events, export); compares two dumps |
| `run.py` | times the real service: cold start, window builds, the inspector, the page under live load |
| `hotpaths.py` | drives the heavy paths named in `--what` in this process, each under `cProfile` |
| `stallwatch.py` | serves, and writes every thread's stack whenever the event loop stalls |
| `svc.py` | the service as a subprocess, configured by the `RC_DASH_*` it sets and, if there is one, `dashboard.config.json`; used by `run.py` and `golden.py` |

**Move `dashboard.config.json` aside first.** The service reads it whatever the bench sets, and
a `password_hash` in it signs the bench out: every request is a 401, and the bench waits out
its 30-minute timeout.

## A change, measured

**1. A feed.** Make it once, and use the same bytes for both sides of a comparison.

```bash
.venv/bin/python bench/amplify.py /tmp/feed --days 2 --hosts 3 --host-clones 2 --density 8 --gzip-sealed --end 1795435200
```

This is the 6-node feed that the bench figures in the READMEs come from: 514 K records and
787 MB. Every day except the newest is gzipped, the way a fleet keeps them, so the `.gz` read
paths get exercised too. `--end` fixes the time of the newest record, so the command writes
the same bytes each time it runs. That holds only with the same demo generator, though: a
change to `rc_dashboard/demo` changes the feed.

**2. The answers.** Dump the responses from the base and from the change, and compare them.

```bash
git worktree add --detach /tmp/base origin/main
ln -s "$PWD/.venv" /tmp/base/dashboard/.venv
(cd /tmp/base/dashboard && .venv/bin/python bench/golden.py dump /tmp/feed /tmp/base.json --cache /tmp/cache-base)
.venv/bin/python bench/golden.py dump /tmp/feed /tmp/new.json --cache /tmp/cache-new
.venv/bin/python bench/golden.py compare /tmp/base.json /tmp/new.json
```

`golden.py` runs the service from the tree it is in, so each dump runs its own tree's code;
`git worktree remove --force /tmp/base` drops the base tree after.
`compare` prints `IDENTICAL`, or the differences, up to 60, and how many it found; a speed-up
that changes a number the page shows is a regression. A key that says when or how fast something ran, rather than what the
feed holds, is left out of both (`VOLATILE`); if two dumps of one tree ever differ, the key
that differs belongs there.

**3. The time.** `run.py FEED` times the service as a deployment runs it, from outside; it
reads the page's own files too, so build `web/dist` first (`cd web && npm ci && npm run build`).
`hotpaths.py FEED --what scan,hours,merge,filter` times those paths in this process. Then:

- **Run A, B, A, B.** The same code on the same feed reduced a 24 h window's hours in 14.2 s,
  and later, on the same box, in 17.1 s. Only runs taken side by side can be compared.
- **Pin `PYTHONHASHSEED`** (`svc.py` pins it for the service). Set order decides which file a
  read opens first, and therefore what a cache holds when the next read asks for it. That
  changes no answer, but it does move counts and times (132 span inflations or 114, for the
  same build).
- **Time without `cProfile` before believing a result.** `cProfile` adds overhead to every
  small call, so a reducer that makes millions of `dict.get` calls looks several times more
  expensive than it really is.
- **Count the work as well as the time** on a busy box. The number of gzip spans inflated or
  indexes built stays the same however loaded the machine is.

**4. Stalls.** `stallwatch.py OUT.txt [--after 0.5]` serves as `python -m rc_dashboard serve`
does, configured by `RC_DASH_*` and `dashboard.config.json`, so give it the feed's roots and
`RC_DASH_OPEN=1` (the service refuses to serve without a password otherwise). Its first
argument is the file it writes to.

## What the feed is not

An hour of one of the bench feed's files is 1 to 3 MB, which fits inside one 8 MB gzip span.
An hour on a FAS login node is about 140 MB. Some changes have a gain or cost that scales with
that size, such as how much a gzip read inflates or how much a cache of spans holds. Those
changes measure very differently here than on a fleet, so say when a change is one of them,
and reason about the fleet's sizes as well. The sharing of gzip spans described in
`dashboard/README.md` is one such change: its bench figure is several times what a fleet
would see.
