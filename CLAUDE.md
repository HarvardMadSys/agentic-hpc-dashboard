# CLAUDE.md

`collector/` runs on HPC login nodes and writes JSONL day-files, one per host, stream and day:
`<host>.exits.jsonl` and `<host>.snapshot.jsonl` from the eBPF tier, gzipped once the day is
over, and `<host>.jsonl` from the node tier. `dashboard/` is a FastAPI service, with its React
page in `dashboard/web`, that reads them. The two are coupled only by those files. `dashboard/README.md` explains each mechanism and
module; `docs/README.md` indexes the rest.

## Git

- Pull requests target `develop`, never `main` (GitHub's default): name the base explicitly.
- Commit subjects read `Area: what changed` (`Dashboard: …`, `Collector: …`, `Docs: …`), with
  a body that says why.
- Pushes and tags have side effects (`.github/workflows/`; each workflow also runs when its own
  file changes):
  - A push to `develop` that changes `dashboard/rc_dashboard/`, `dashboard/web/`,
    `dashboard/pyproject.toml`, `dashboard/uv.lock`, `docker/demo/` or `deploy/cloudflare/`
    redeploys the public demo once CI passes; a change to `*.md` files alone does not.
  - A push to any other branch that changes `collector/` or `packaging/rpm/` builds an RPM
    artifact, and one that changes `collector/`, `docker/collector/` or `docker/base/` pushes a
    branch-tagged collector image to GHCR (`docker/base/` rebuilds the base image too). Docs
    count: these paths take `*.md` files as well.
  - A push to any branch, or a pull request, that changes `dashboard/pyproject.toml`,
    `dashboard/uv.lock`, `dashboard/rc_dashboard/`, `dashboard/risk-profiles/`,
    `dashboard/web/`, `docker/dashboard/` or `.github/workflows/dashboard-image.yml` (not
    `*.md` alone) builds the dashboard's wheel and image; a push to `develop` also pushes the
    image to GHCR as `edge`.
  - A `v*.*.*` tag runs all four whatever it changes: it publishes the collector's and the
    dashboard's `X.Y.Z`, `X.Y` and `latest` images and attaches the RPM, the dashboard's
    wheel, its pinned requirements, their checksums and the deploy kit's tarball to the tag's
    GitHub Release, creating the release if there is none (the dashboard takes only
    `vX.Y.Z`, whose version it stamps into the wheel, image and kit; the kits' installers
    download from that release). Push a tag only when a release is asked for.

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
(cd dashboard && uv run --frozen rc-dashboard demo)            # synthetic feed on :8080
(cd dashboard/web && npm run dev)                               # the page alone, from mock/api.json, on :5173
```

- Always give `uv` `--frozen` (or `--no-sync` after a frozen sync); otherwise it may rewrite
  `uv.lock`, which redeploys the demo.
- There is no linter or formatter; `npm run typecheck` is the page's static check.
- `dashboard/tests/e2e_history.py` is outside discovery. After changing the history store, run
  `(cd dashboard && uv run --frozen python tests/e2e_history.py)`: it serves one feed with the
  store off and on and diffs every API. CI runs it too (`e2e-history`), as is and with
  `--restart --seal-between`.
- `npm run dev`'s mock answers only the main routes; `npm run dev:real` with
  `RC_BACKEND=http://127.0.0.1:<port>` proxies to a running service.
- `deploy/common/tests` runs the installers as root with stand-ins: any new host-changing
  command an installer runs needs a stand-in in that rig, and installer tests always pass
  `--no-bootstrap`.

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
  `hpc/fleet/rsync_guard.py`, `deploy/fleet/pull.py` and `deploy/rc-deploy`. A
  newer local Python won't catch a break; CI's EL8 job will. The dashboard needs 3.11+.
- **Vendored files**: `collector/node_snapshot.py` and
  `collector/lib/{procparse,agent_classify,provider_cidrs,slurm_args}.py` are copies from an
  upstream measurement repository, which is also what `collect/`, `analyze/` and `findings/`
  in comments refer to. Keep local changes few and list them in `collector/VENDORED.md`.
- `collector/lib/redact.py` and `dashboard/rc_dashboard/redact.py` keep identical rules.
- A copy of a day-file must grow in place, never be replaced by rename (rsync's default): the
  dashboard knows a file by inode and offset, and would count a renamed copy again.
- `dashboard/web/src/api/types.ts` mirrors the service's payloads: change them together.
- A new setting is internal unless every site must choose it: then it joins `config.PUBLIC`
  and the table in `dashboard/README.md#configuration` (a test holds them equal).
  `rc-dashboard print-config --reference` prints the public ones, `--all` every one, both
  generated from the defaults. A new config section that no reduction reads goes in
  `config.UNREDUCED`, or it changes the saved builds' and hours' fingerprints and discards them.
- `dashboard/web/mock/api.json` is generated: edit `mock/gen_fixture.mjs` and run
  `TZ=America/New_York npm run fixture`.
- **Docs track the code**: a change to a setting, route, panel, module or command updates the
  README that describes it in the same pull request.
