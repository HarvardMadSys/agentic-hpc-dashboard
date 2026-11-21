# CLAUDE.md

`collector/` runs on HPC login nodes and writes one JSONL file per host per day (a day-file);
`dashboard/` is a FastAPI service, with its React page in `dashboard/web`, that reads them.
The two are coupled only by those files. `dashboard/README.md` explains each mechanism and
module; `docs/README.md` indexes the rest.

## Git

- Pull requests target `develop`, never `main` (GitHub's default): name the base explicitly.
- Commit subjects read `Area: what changed` (`Dashboard: …`, `Collector: …`, `Docs: …`), with
  a body that says why.
- Pushes have side effects. A push to `develop` that changes `dashboard/rc_dashboard/`,
  `dashboard/web/`, `dashboard/pyproject.toml`, `dashboard/uv.lock`, `docker/demo/` or
  `deploy/cloudflare/` redeploys the public demo. A push to any other branch that changes
  `collector/`, `packaging/rpm/`, `docker/base/` or `docker/collector/` publishes an RPM
  artifact or GHCR images. A `v*.*.*` tag publishes a release: push one only when asked.

## Commands

From the repository root; CI (`.github/workflows/ci.yml`) runs the same suites.

```bash
python3 -m unittest discover -s collector/tests -q        # no root or BPF needed
python3 -m unittest discover -s deploy/fleet/tests -q     # needs rsync
python3 -m unittest discover -s deploy/common/tests -q
python3 -m unittest discover -s hpc/fleet/tests -q
python3 -m unittest discover -s hpc/deploy/tests -q
(cd dashboard && uv sync --frozen && uv run --no-sync python -m unittest discover -s tests -q)   # ~3 min
(cd dashboard/web && npm ci && npm run typecheck && npm run check && npm run build)
(cd dashboard && uv run --frozen python -m rc_dashboard demo)   # synthetic feed on :8080
(cd dashboard/web && npm run dev)                               # the page alone, from mock/api.json, on :5173
```

- Always give `uv` `--frozen` (or `--no-sync` after a frozen sync); otherwise it may rewrite
  `uv.lock`, which redeploys the demo.
- There is no linter or formatter; `npm run typecheck` is the page's static check.
- `dashboard/tests/e2e_history.py` is outside discovery. Run it after changing the history
  store: it serves one feed with the store off and on and diffs every API.
- `npm run dev`'s mock answers only the main routes; `npm run dev:real` with
  `RC_BACKEND=http://127.0.0.1:<port>` proxies to a running service.

## Research-integrity rules

Each is a past mistake.

- **Null is not zero.** An unmeasured value is `null` and reads "not measured", never `0`; an
  absent feed names the path it expected.
- **Three actor classes, never two**: `agent`, `human`, `ide`.
- **Nothing fabricated**: no `Math.random` in `dashboard/web/src`, and no line drawn across a
  missing bin.
- **`sandbox` and `approval` are independent**, and `denied` is a state of its own.
- Credentials are masked (`redact.py`) on every way out of the service.

## Constraints

- **Python 3.6** for `collector/` (the Python inside `ebpfm.sh` included),
  `hpc/fleet/{fleet_stream,seed_mirror}.py` and `deploy/fleet/pull.py`. A newer local Python
  won't catch a break; CI's EL8 job will. The dashboard needs 3.11+.
- **Vendored files**: `collector/node_snapshot.py` and
  `collector/lib/{procparse,agent_classify,provider_cidrs,slurm_args}.py` are copies from an
  upstream measurement repository, which is also what `collect/`, `analyze/` and `findings/`
  in comments refer to. Keep local changes few and list them in `collector/VENDORED.md`.
- `collector/lib/redact.py` and `dashboard/rc_dashboard/redact.py` keep identical rules.
- A copy of a day-file must grow in place, never be replaced by rename (rsync's default): the
  dashboard knows a file by inode and offset, and would count a renamed copy again.
- `dashboard/web/src/api/types.ts` mirrors the service's payloads: change them together.
- `dashboard/web/mock/api.json` is generated: edit `mock/gen_fixture.mjs` and run
  `TZ=America/New_York npm run fixture`.
- **Docs track the code**: a change to a setting, route, panel, module or command updates the
  README that describes it in the same pull request.
