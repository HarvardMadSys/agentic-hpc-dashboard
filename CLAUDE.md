# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Branches and pull requests

- **Every pull request targets `develop`, never `main`.** GitHub's default branch is `main`, so
  name the base explicitly when opening one (`base: develop`). `main` is brought up to
  `develop` by a pull request of its own, which the maintainers open.
- Commit subjects follow the history's style, `Area: what changed` (`Dashboard: …`,
  `Collector: …`, `Hours: …`, `Docs: …`, `CI: …`), with a body that says why.

## What this is

Two halves, coupled only by files on disk:

- `collector/`: `ebpfm`, run on each HPC login node through `collector/ebpfm.sh` (`check`,
  `start`, `stop`, `status`, `bundle`, …). The **eBPF tier** (root, `ebpf_trace.py`) writes
  `<root>/<YYYY-MM-DD>/<host>.exits.jsonl` and `<host>.snapshot.jsonl`, gzipped once the day is
  over (a *sealed* day, which never changes again). The **node tier** (any user,
  `node_snapshot.py`, every 300 s) writes `<host>.jsonl`. The record schema is
  `SCHEMA_VERSION` in `ebpf_trace.py` (8).
- `dashboard/`: `rc_dashboard`, a FastAPI service that reads those day-files and serves a
  React page (`dashboard/web`, Vite + TypeScript, built into `web/dist/`, which the service
  serves).

Around them: `hpc/fleet/` streams each node's files to the dashboard's host over ssh;
`hpc/deploy` deploys the live service from one `site.env`; `deploy/cloudflare` is the public
demo; `docker/` holds the base, collector and demo images; `packaging/rpm/` the EL8 RPM.
`docs/README.md` indexes the cross-cutting documents.

## Commands

From the repository root; CI (`.github/workflows/ci.yml`) runs the same suites.

```bash
python3 -m unittest discover -s collector/tests -q        # no root, no BPF needed
python3 -m unittest discover -s hpc/fleet/tests -q
python3 -m unittest discover -s hpc/deploy/tests -q
(cd dashboard && uv sync --frozen && uv run --no-sync python -m unittest discover -s tests -q)   # ~3 min
(cd dashboard/web && npm ci && npm run typecheck && npm run check && npm run build)
```

One file, class or test:

```bash
python3 -m unittest discover -s collector/tests -p 'test_redact.py'
cd dashboard && uv run --frozen python -m unittest tests.test_config.Flags
cd dashboard/web && node --test --test-name-pattern='prototype' mock/check_lookup.mjs
```

- Give `uv` `--frozen` (or `--no-sync` after a frozen sync), as CI does: without it `uv` may
  re-lock and rewrite `dashboard/uv.lock`, and a `uv.lock` change on `develop` redeploys the
  public demo.
- `npm run check` is `check:no-random`, then every `mock/check_*.mjs` under one `node --test`,
  by glob: a new check file runs in CI without touching `ci.yml`.
- `dashboard/tests/e2e_history.py` is not picked up by discovery:
  `cd dashboard && uv run python tests/e2e_history.py` serves one demo feed with the history
  store off and on, and diffs every API.
- There is no linter or formatter; `npm run typecheck` (`tsc -b`) is the page's static check.

Running the dashboard:

```bash
cd dashboard && uv run --frozen python -m rc_dashboard --check-feeds   # which feed paths resolved, and why
cd dashboard && uv run --frozen python -m rc_dashboard demo            # synthetic feed, no cluster: :8080
cd dashboard/web && npm run dev                                # the page alone, from mock/api.json: :5173
```

- `serve` refuses to start without a password hash (`rc_dashboard new-password`), unless
  `--open` / `RC_DASH_OPEN=1`.
- Settings resolve per key: CLI flag > `RC_DASH_*` env > `dashboard.config.json` (gitignored)
  > default; `--print-config` shows where each came from. Point it at data with
  `RC_DASH_EBPF_ROOTS` and `RC_DASH_NODE_ROOTS`.
- `dashboard/run.sh` (hot reload) and its wrappers `run-dev.sh` and `run-fasrc.sh` carry one
  site's defaults (`/scratch/marthen/...` roots, dropped accounts); set the roots anywhere else.
- `npm run dev`'s mock (`mock/plugin.ts`) answers only the main routes; the rest are 404 there,
  and `npm run dev:real` with `RC_BACKEND=http://127.0.0.1:<port>` proxies to a real service.

## Architecture of the dashboard

Every record is parsed (`fastjson.py`) and normalized (`normalize.py`, which derives the class
`_a3`, the session key `_sid`, sandbox and approval, ...) before any reducer sees it.

- **Live tier**: `tail.py` follows the newest day-files; `reducers/live.py:LiveReducer` keeps
  per-minute bins for `live.retention_min` (7 d). On each start the history behind the tail is
  read back into it, newest first.
- **Window views** (`windows.py`): each window, and each user/agent-type filter, is a set of
  reducers (`reducers/*.py`) built by merging per-hour saved states (`hours.py`: each record is
  reduced once into its hour), then fed live; a view is rebuilt once it drifts past
  `panels.max_drift_pct`. Saved builds (`snapshots.py`) resume only when provably a prefix of
  the same files, under a fingerprint of the code and the config.
- **History store** (`history.py`, `replica.py`, `feedparquet.py`): each sealed `.jsonl.gz` is
  also kept as Parquet, every row carrying `_off`, its line's offset, so a replica answers a
  read exactly as the file would. Selective reads (one user, one session) use it.
- **On demand**: `proctree.py` (the session inspector's process tree), `iocmds.py` (the
  commands behind an I/O number), `export.py` (`/api/events` and `/api/export`, one filter
  grammar).
- `app.py` holds the API and websocket routes (sign-in is `auth.py`'s); `aggregator.py` ties
  the tail, the live tier, the views and the node tier together. The page's `src/api/` mirrors the service's payloads (`types.ts`).

`dashboard/README.md` explains each mechanism (its *Layout* table lists every module), and
`docs/figures/README.md` maps every figure on the page to its records, reducer and read path.
Both are long: search their headings.

## Rules the code keeps

Research-integrity constraints, each a past mistake (root `README.md`):

- **Null is not zero.** An unmeasured value is `null` and reads "not measured", never `0`; an
  absent feed names the path it expected.
- **Three actor classes, never two**: `agent`, `human`, `ide`.
- **Nothing fabricated**: no `Math.random` in `dashboard/web/src` (`npm run check:no-random`),
  and no line drawn across a missing bin.
- **`sandbox` and `approval` are independent**, and `denied` is a state of its own.
- Credentials are masked (`redact.py`) on every way out of the service.

## Constraints that bite

- **Python 3.6** for all of `collector/` (the Python inside `ebpfm.sh` included) and the fleet
  sender. CI's EL8 job checks it on EL8's own 3.6: the collector's tests, and
  `python3 -m py_compile hpc/fleet/fleet_stream.py hpc/fleet/seed_mirror.py`. A newer Python accepts
  syntax 3.6 rejects, so a local run on one does not catch a break. The dashboard needs 3.11
  or later.
- **Vendored copies**: `collector/node_snapshot.py` and `collector/lib/{procparse,agent_classify,provider_cidrs,slurm_args}.py`
  are copies of files in the upstream measurement repository, with their upstream hashes in
  `collector/.vendored.sha256` (`ebpfm.sh check` warns when the upstream file has moved). Keep
  local changes few and list them in `collector/VENDORED.md`. `collector/lib/redact.py`'s rules
  must match `dashboard/rc_dashboard/redact.py` word for word (`collector/tests/test_redact.py`).
- Paths such as `collect/`, `analyze/` and `findings/` in comments and docs name that upstream
  repository, not this one.
- **Docs track the code.** A change to a setting, route, panel, module or command updates the
  README that describes it (`dashboard/README.md`, `dashboard/web/README.md`,
  `collector/README.md`, `docs/figures/README.md`, ...) in the same pull request.
- The page's dev fixture `dashboard/web/mock/api.json` is generated: edit `mock/gen_fixture.mjs`
  and run `TZ=America/New_York npm run fixture`. It is deterministic, so the diff should hold
  only what you meant to change.
- **Pushes and tags have side effects.**
  - A push to any branch but `develop` that changes `collector/` or `packaging/rpm/` builds an
    RPM artifact; one that changes `collector/`, `docker/collector/` or `docker/base/` pushes a
    branch-tagged collector image to GHCR, and `docker/base/` also rebuilds the base image.
  - A `v*.*.*` tag runs all three whatever it changes: it publishes the release images
    (`X.Y.Z`, `latest`) and attaches the RPM to the tag's GitHub Release, creating the release
    if there is none. Push a tag only when a release is asked for.
  - A push to `develop` that changes what the demo runs (`dashboard/rc_dashboard/`,
    `dashboard/web/`, `dashboard/pyproject.toml` or `uv.lock`, `docker/demo/`,
    `deploy/cloudflare/`) redeploys the public demo (`demo-deploy.yml`) once the tests pass; a
    change to `dashboard/tests/` alone does not.
