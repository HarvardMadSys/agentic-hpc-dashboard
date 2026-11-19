# The bench

Tools to measure the dashboard, and to show a change kept every answer it serves. Each
tool's docstring lists its options; this page is how they fit together, and what to watch
for. Run them from `dashboard/`, with the project's venv (`uv sync`).

| tool | what it does |
|---|---|
| `amplify.py` | writes a synthetic collector feed at fleet density, cloned from the demo generator's |
| `golden.py` | dumps every response the page reads; compares two dumps |
| `run.py` | times the real service: cold start, window builds, the inspector, the page under live load |
| `hotpaths.py` | drives one heavy path in this process, under `cProfile` |
| `stallwatch.py` | serves, and writes every thread's stack whenever the event loop stalls |
| `svc.py` | the service as a subprocess, configured by `RC_DASH_*` alone; used by `run.py` and `golden.py` |

## A change, measured

**1. A feed.** Make it once, and use the same bytes for both sides of a comparison:

```bash
.venv/bin/python bench/amplify.py /tmp/feed --days 2 --hosts 3 --host-clones 2 --density 8 --gzip-sealed --end 1795435200
```

That is the 6-node feed the READMEs' bench figures come from: 514 K records, 787 MB, every
day but the newest gzipped as a fleet keeps them, so the `.gz` paths are read as well. `--end`
fixes the newest record, so the command writes the same bytes again -- but only with the same
demo generator: a change to `rc_dashboard/demo` changes the feed.

**2. The answers.** Dump the base and the change, and compare:

```bash
git worktree add --detach /tmp/base origin/main
ln -s "$PWD/.venv" /tmp/base/dashboard/.venv
(cd /tmp/base/dashboard && .venv/bin/python bench/golden.py dump /tmp/feed /tmp/base.json --cache /tmp/cache-base)
.venv/bin/python bench/golden.py dump /tmp/feed /tmp/new.json --cache /tmp/cache-new
.venv/bin/python bench/golden.py compare /tmp/base.json /tmp/new.json
```

`golden.py` runs the service from the tree it is in, so each dump runs its own tree's code;
`git worktree remove --force /tmp/base` drops the base tree after.
`compare` prints `IDENTICAL` or every difference; a speed-up that changes a number the page
shows is a regression. A key that says when or how fast something ran, rather than what the
feed holds, is left out of both (`VOLATILE`); if two dumps of one tree ever differ, the key
that differs belongs there.

**3. The time.** `run.py FEED` times the service as a deployment runs it, from outside; it
reads the page's own files too, so build `web/dist` first (`cd web && npm ci && npm run build`).
`hotpaths.py FEED --what scan,hours,merge,filter` times one path in this process. Then:

- **Run A, B, A, B.** The same code on the same feed reduced a 24 h window's hours in 14.2 s,
  and on the same box later in 17.1 s; only runs taken side by side compare.
- **Pin `PYTHONHASHSEED`** (`svc.py` pins it for the service). Set order decides which file a
  read opens first, and so what a cache holds when the next one asks: it changes no answer, but
  it moves counts and times (132 span inflations, or 114, for one build).
- **Time without `cProfile` before believing it.** It charges every small call, so a reducer
  that makes millions of `dict.get` calls reads several times its real cost.
- **Count the work as well as the time** on a busy box: a gzip span inflated or an index
  built is the same count however loaded the machine is.

**4. Stalls.** `stallwatch.py OUT.txt [--after 0.5]` serves as `python -m rc_dashboard serve`
does, configured only by `RC_DASH_*`, so give it the feed's roots and `RC_DASH_OPEN=1` (the
service refuses to serve without a password otherwise). Its first argument is the file it
writes to.

## What the feed is not

An hour of one of the bench feed's files is 1 to 3 MB, under one 8 MB gzip span; a FAS login
node's is about 140 MB. A change whose gain or cost scales with that -- what a gzip read
inflates, what a cache of spans holds -- measures very differently here than on a fleet, so
say which it is, and reason about the fleet's sizes as well: `dashboard/README.md`'s sharing of
gzip spans is one, its bench figure several times what a fleet would see.
