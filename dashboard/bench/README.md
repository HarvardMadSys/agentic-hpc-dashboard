# The bench

These tools measure the dashboard, and show that a change still gives every answer the
dashboard served before. Each tool's docstring lists its options. This page explains how the
tools fit together and what to watch out for. Run them from `dashboard/`, with the project's
venv (`uv sync`).

| tool | what it does |
|---|---|
| `amplify.py` | writes a synthetic collector feed at fleet density, cloned from the demo generator's |
| `golden.py` | dumps every response the page reads; compares two dumps |
| `run.py` | times the real service: cold start, window builds, the inspector, the page under live load |
| `hotpaths.py` | drives one heavy path in this process, under `cProfile` |
| `stallwatch.py` | serves, and writes every thread's stack whenever the event loop stalls |
| `svc.py` | the service as a subprocess, configured by `RC_DASH_*` alone; used by `run.py` and `golden.py` |

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

`golden.py` runs the service from the tree it lives in, so each dump runs its own tree's code.
Afterwards, `git worktree remove --force /tmp/base` removes the base tree.
`compare` prints either `IDENTICAL` or every difference it finds. A speed-up that changes a
number the page shows is a regression. Some keys describe when or how fast something ran
rather than what the feed holds; those are left out of both dumps (`VOLATILE`). If two dumps
of the same tree ever differ, the key that differs belongs in that list.

**3. The time.** `run.py FEED` times the service from outside, run the way a deployment runs
it. It also reads the page's own files, so build `web/dist` first (`cd web && npm ci && npm run build`).
`hotpaths.py FEED --what scan,hours,merge,filter` times one path in-process. Then:

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

**4. Stalls.** `stallwatch.py OUT.txt [--after 0.5]` serves the same way
`python -m rc_dashboard serve` does, and is configured only by `RC_DASH_*`. Give it the feed's
roots and `RC_DASH_OPEN=1`, because otherwise the service refuses to serve without a password.
Its first argument is the file it writes the stacks to.

## What the feed is not

An hour of one of the bench feed's files is 1 to 3 MB, which fits inside one 8 MB gzip span.
An hour on a FAS login node is about 140 MB. Some changes have a gain or cost that scales with
that size, such as how much a gzip read inflates or how much a cache of spans holds. Those
changes measure very differently here than on a fleet, so say when a change is one of them,
and reason about the fleet's sizes as well. The sharing of gzip spans described in
`dashboard/README.md` is one such change: its bench figure is several times what a fleet
would see.
