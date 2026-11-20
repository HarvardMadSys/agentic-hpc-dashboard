<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/logo/lockup-dark.svg">
    <img src="docs/logo/lockup.svg" alt="Agent Observatory, Harvard MaDSys" width="400">
  </picture>
</p>

# agentic-hpc-dashboard

Measuring what coding agents actually do on HPC login nodes, and showing it without
inventing anything. A collector records what runs on each login node, agents and people
alike; a dashboard, **Agent Observatory**, makes it visible to people.

<p>
  <a href="https://madsys.seas.harvard.edu/"><img src="dashboard/web/public/logos/madsys.png" alt="Harvard University MaDSys: Measurement and Design of Systems" height="64"></a>
</p>

<p>
  <a href="https://seas.harvard.edu/"><img src="dashboard/web/public/logos/harvard-seas.svg" alt="Harvard John A. Paulson School of Engineering and Applied Sciences" height="44"></a>
  &nbsp;&nbsp;
  <a href="https://www.rc.fas.harvard.edu/"><img src="dashboard/web/public/logos/fasrc.png" alt="FAS Research Computing, Harvard University" height="32"></a>
</p>

**Contents:** [What is here](#what-is-here) ·
[How the halves fit](#how-the-halves-fit) ·
[Collector](#collector) ·
[Dashboard](#dashboard) ·
[Documentation map](#documentation-map) ·
[The rules both halves obey](#the-rules-both-halves-obey) ·
[Tests](#tests) ·
[CI and releases](#ci-and-releases) ·
[Requirements](#requirements) ·
[Paths and names from elsewhere](#paths-and-names-from-elsewhere)

## What is here

Two halves, in this order:

| | what it is | privilege | output |
|---|---|---|---|
| [`collector/`](collector/) | `ebpfm`: a standalone two-tier login-node collector (eBPF + a `/proc` node snapshot) behind one entry point, `ebpfm.sh` | root for the eBPF tier, unprivileged for the node tier | JSONL day-files: two per host per day from the eBPF tier (`exits` + `snapshot`, gzipped once the day is over), one from the node tier |
| [`dashboard/`](dashboard/) | `rc_dashboard`: a FastAPI service that tails those day-files, keeps rolling aggregates over a selectable window, and pushes them to a React page ([`dashboard/web/`](dashboard/web/)) over a WebSocket | unprivileged | HTTP + WebSocket, on `127.0.0.1:8080` by default |

And around them:

| | what it is |
|---|---|
| [`fleet/`](fleet/) | the fleet stream: each login node's collector files sent over ssh, as they are written, to the host that runs the dashboard |
| [`deploy/fleet/`](deploy/fleet/) | the live service on a host of your own: the dashboard behind a password, fed by the fleet stream from every node, all set from one settings file, with an hourly check |
| [`deploy/cloudflare/`](deploy/cloudflare/) | the public demo: a Cloudflare Worker in front of a container running the dashboard over a synthetic feed |
| [`docker/`](docker/) | three images: `base` (Rocky 8.10), `collector` (built from `base`), and `demo` (the dashboard over a synthetic feed) |
| [`packaging/rpm/`](packaging/rpm/) | the `ebpfm-collector` RPM for EL8: the spec, the sysctl drop-in and the unit's `/etc/default/ebpfm` |
| [`docs/`](docs/) | documents about more than one part: what each figure computes, the feed as Parquet, the schema-9 proposal, plans, the logo ([index](docs/README.md)) |
| [`.github/workflows/`](.github/workflows/) | the test suites on every pull request, and the RPM, images and demo they gate ([CI and releases](#ci-and-releases)) |

## How the halves fit

The collector runs on each login node; the dashboard reads the directories the collector
writes. They are coupled only by the on-disk records (`SCHEMA_VERSION = 8`), written as
JSONL, so the dashboard can read a feed captured months ago, or a fleet's worth of hosts
at once, and nothing needs to be running for it to start.

The eBPF tier splits each day in two: `<host>.exits.jsonl` for the per-process terminal
records, whose rate is the node's fork rate and which dominate the volume, and
`<host>.snapshot.jsonl` for the residency census, connections, per-tick network bytes by
endpoint, submits and the run's own meta/stop bookends. Each record carries its own `host`,
and the dashboard lists the day directory, so the split is a filing decision, not a schema
one. Once a day is over the collector gzips its files (`.jsonl.gz`), and they never change
again; the dashboard keeps each such sealed file as Parquet too, in the background
([History in Parquet](dashboard/README.md#history-in-parquet)), and answers a selective
read of a sealed day (one user's events, one session's process tree) or a search of it by
time from that copy, which reads exactly as the file does, offsets and all.

```
login node                                    anywhere with read access
┌──────────────────────────────┐              ┌────────────────────────────────┐
│ ebpfm.sh                     │   snapshot   │ rc_dashboard                   │
│  ├ ebpf tier   (root)  ──────┼─ + exits ────┼─▶ tail → normalize → buckets   │
│  └ node tier   (user)  ──────┼─ snapshot ───┼─▶ reducers → /api + WS         │
└──────────────────────────────┘              │                  └─▶ web/ SPA  │
                                              └────────────────────────────────┘
```

"Anywhere with read access" is the login node itself, a host that mounts the collector's
output, or a host the [fleet stream](deploy/fleet/README.md) mirrors it to.

## Collector

Full reference: [`collector/README.md`](collector/README.md). What it records, in plain
words: [`collector/WHAT-IT-COLLECTS.md`](collector/WHAT-IT-COLLECTS.md).

### From the source tree

The folder is self-contained: `scp` it to the login node, or hand over one file from
`./collector/ebpfm.sh bundle`.

```bash
sudo ./collector/ebpfm.sh bootstrap    # dependencies + the two sysctls, once per node
sudo ./collector/ebpfm.sh check        # one line per precondition; expect 0 FAIL
sudo ./collector/ebpfm.sh start        # both tiers
sudo ./collector/ebpfm.sh status       # each stream's records and size today
sudo ./collector/ebpfm.sh stop         # flushes live processes as `truncated`
```

Without root you still get the node tier: `./collector/ebpfm.sh start`. That is a real
half of the data, not a degraded mode: eBPF cannot produce load average, memory totals,
filesystem space or interface counters at all. `start` runs the eBPF tier as a transient
systemd service, which does not survive a reboot; for a fleet,
`sudo ./collector/ebpfm.sh install-unit` installs the persistent unit, or install the RPM
below.

Where the day-files go, unless `EBPFM_OUTPUT_DIR` (eBPF tier) and `EBPFM_NODE_OUTPUT_DIR`
(node tier) say otherwise:

| run as | eBPF tier | node tier |
|---|---|---|
| root, with a writable `/scratch` | `/scratch/ebpfm` | `/scratch/ebpfm-login` |
| root, otherwise | `/var/log/ebpfm` | `/var/log/ebpfm-login` |
| an ordinary user | (needs root) | `~/.ebpfm/login` |

Point the dashboard at the same directories ([Dashboard](#dashboard)).

### RPM (Rocky/RHEL 8)

A signed-off release publishes an `ebpfm-collector` RPM (noarch, built for
EL8 / Rocky 8.10, **x86_64** hosts) as a GitHub Release artifact. Install it,
check the node, then enable the service:

```bash
sudo dnf install ./ebpfm-collector-<version>-<release>.el8.noarch.rpm
sudo ebpfm check         # expect 0 FAIL on real hardware
sudo systemctl enable --now ebpfm
sudo systemctl status ebpfm
```

The RPM lays down:

| Path | What |
|---|---|
| `/usr/lib/ebpfm/` | the standalone collector bundle (`ebpf_trace.py`, `node_snapshot.py`, `lib/`, `ebpfm.sh`, `README.md`) |
| `/usr/bin/ebpfm` | symlink to `ebpfm.sh`, the entry point for `check`/`status`/`tail` |
| `/usr/lib/systemd/system/ebpfm.service` | the eBPF-tier unit, paths pre-substituted (no `install-unit` needed) |
| `/usr/lib/sysctl.d/99-ebpfm.conf` | the two required sysctls, applied on install and every boot |
| `/etc/default/ebpfm` | the unit's environment: the collector's settings, kept across upgrades |
| `/scratch/ebpfm/` | the output directory (the unit's `EBPFM_OUTPUT_DIR`), made by the collector when it first starts; the unit waits for `/scratch` to be mounted |

Dependencies and sysctls are handled by the package, not a separate bootstrap
step:

* **Package deps** (`python3-bcc`, `bpftool`, `iproute`, `jq`, `sysstat`,
  `nfs-utils`, `procps-ng`) are pulled in by `dnf`. `python3-bcc` lives in EPEL,
  so the RPM `Requires: epel-release`; its `-devel` deps live in PowerTools,
  which the RPM's `%post` enables (`powertools`/`PowerTools`/`crb`).
* **Sysctls** (`kernel.sched_schedstats`, `kernel.task_delayacct`) ship as the
  `99-ebpfm.conf` drop-in above. `%post` applies them immediately via
  `systemd-sysctl`, and systemd re-applies them on every boot: no reboot and no
  `bootstrap` needed.
  * On EL8's 4.18 kernel `kernel.task_delayacct` has **no writable sysctl** (it
    was added upstream in 5.14), but delay accounting is compiled in
    (`CONFIG_TASK_DELAY_ACCT=y`) and on by default, so the data is still
    collected. The drop-in lists that key with a leading dash
    (`-kernel.task_delayacct`) so `systemd-sysctl` skips the absent knob
    silently instead of logging `Couldn't write '1' to 'kernel/task_delayacct'`.
    `ebpfm check` reports it as a non-blocking WARN ("no such knob — compile-time
    only"); `sched_schedstats` does exist on 4.18 and is set to 1 normally.

The service is **not** enabled on install; run `ebpfm check` (expect 0 FAIL)
before enabling. The unit runs the eBPF tier only; for the node tier as well, run
`ebpfm start` as an ordinary user.

Release vs. feature builds ([`rpm-release.yml`](.github/workflows/rpm-release.yml)):

* **Releases**: a semver git tag `vX.Y.Z` builds `ebpfm-collector-X.Y.Z-1.el8`
  and attaches it to the GitHub Release for that tag.
* **Feature branches**: a push to any branch but `develop` that changes the collector
  or its packaging builds `ebpfm-collector-0.0.0-<YYYYMMDD>.<shortsha>.el8` and
  uploads it as a workflow **artifact** (not a release). The `0.0.0` version keeps a
  branch RPM sorting below any real release, and the datestamp+sha makes each build
  identifiable.

### Container image

The collector also ships as a container image on the GitHub Container Registry,
built on a project Rocky 8.10 base image. **x86_64 only** at present.

```
ghcr.io/harvardmadsys/agentic-hpc-dashboard/collector     # the collector
ghcr.io/harvardmadsys/agentic-hpc-dashboard/base          # the Rocky 8.10 base it builds on
```

Image tags follow the same scheme as the RPM:

* semver tag `vX.Y.Z` → `X.Y.Z`, `X.Y`, `latest`
* a push to a branch other than `develop` that changes what the image is built from →
  `<branch>` (moving) and `<branch>-<YYYYMMDD>-<sha>` (immutable)
* `main` → additionally `edge`

`latest` exists only once a `vX.Y.Z` tag has been pushed; `edge`, main's newest build,
is the one to pull until then, and is what the examples below use.

Running the eBPF tier in a container needs host-level privileges, because eBPF
attaches to the host kernel and the collector attributes **host** processes. On
Rocky 8.10 physical hardware:

```bash
# One-time, on the HOST (sysctls are not namespaced):
sudo sysctl -w kernel.sched_schedstats=1

docker run -d --name ebpfm \
  --privileged \
  --pid=host \
  -v /sys:/sys:ro \
  -v /var/log/ebpfm:/var/log/ebpfm \
  -e EBPFM_HOST="$(hostname -s)" \
  ghcr.io/harvardmadsys/agentic-hpc-dashboard/collector:edge
```

| option | why |
|---|---|
| `--privileged` | eBPF load + attach on EL8's 4.18 kernel |
| `--pid=host` | see and attribute every process on the node |
| `-v /sys:/sys:ro` | BTF + tracefs for program load |
| `-v /var/log/ebpfm:/var/log/ebpfm` | persist the JSONL output |
| `-e EBPFM_HOST=…` | name the day-files for the node, not the container |

On a 5.14 or later kernel set `kernel.task_delayacct=1` too; EL8's 4.18 has no such
knob, and its delay accounting is on by default (see the RPM notes above).

Notes:

* The container entrypoint execs `ebpf_trace.py` **in the foreground**, as the
  container's main process. It deliberately does *not* use `ebpfm.sh start`'s
  `systemd-run` cgroup escape, which only applies to the capped user slice on a bare
  login node and has no host systemd to talk to inside a container. `docker stop` delivers the
  SIGTERM the collector turns into its `truncated` flush.
* Other subcommands are reachable by passing them as arguments, e.g.
  `docker run --rm ... ghcr.io/.../collector:edge ebpfm check` or
  `... ebpfm features`.
* The collector writes **only** to the mounted output directory. Its one use of the
  network is resolving the host names that command lines mention (`getaddrinfo`, through
  the host's resolver), to name the endpoints a process talked to; `EBPFM_ARGV_HOSTS=0`
  turns that off.

## Dashboard

Full reference: [`dashboard/README.md`](dashboard/README.md); the page itself:
[`dashboard/web/README.md`](dashboard/web/README.md).

### From the source tree

```bash
cd dashboard && uv sync
uv run python -m rc_dashboard --check-feeds       # what resolved, what didn't, and why
cd web && npm ci && npm run build && cd ..        # builds web/dist/, which the service serves
uv run python -m rc_dashboard new-password        # once: put the hash it prints in dashboard.config.json
uv run python -m rc_dashboard serve --port 8080
```

`serve` starts only behind a password: put the hash `new-password` prints in
`dashboard.config.json` as `"server": {"auth": {"password_hash": "..."}}`
([Password](dashboard/README.md#password)). `--open` (`RC_DASH_OPEN=1`) serves without
one, on a machine only you can reach: on a login node every user can reach 127.0.0.1.

Off-cluster, `--check-feeds` exits 1: the eBPF feed is `required` and absent. Point it at
the collector's output first (the [table above](#from-the-source-tree) says where that is):

```bash
RC_DASH_EBPF_ROOTS=/var/log/ebpfm \
RC_DASH_NODE_ROOTS=/var/log/ebpfm-login \
uv run python -m rc_dashboard serve
```

For a remote instance: `ssh -L 8080:127.0.0.1:8080 <host>`.

### Configuration

Every setting resolves per key: **CLI flag > `RC_DASH_*` env var > `dashboard.config.json`
> built-in default**; `--print-config` shows the resolved tree and where each value came
from ([Configuration](dashboard/README.md#configuration)). The ones most deployments set:

```bash
RC_DASH_EBPF_ROOTS=/var/log/ebpfm        # where the eBPF tier's day-files are
RC_DASH_NODE_ROOTS=/var/log/ebpfm-login  # where the node tier's are
RC_DASH_HISTORY=1                        # keep every sealed (.jsonl.gz) day-file as Parquet too (0: off)
RC_DASH_HISTORY_WORKERS=0                # processes converting in the background: 0 (default) = up to 8
RC_DASH_INGEST_WORKERS=1                 # processes reducing the hours: 1 (default) = one pass; N = one node per process
RC_DASH_INGEST_REDUCER_GROUPS=2          # with N >= 2: each node's hour in 2 processes, one per reducer group (1: one)
```

See [History in Parquet](dashboard/README.md#history-in-parquet) for what the store holds,
which reads it answers, and what it costs, and
[Hourly merge-reduce](dashboard/README.md#hourly-merge-reduce) for the workers.

### Developing it

**Hot reload on both halves**: [`dashboard/run.sh`](dashboard/run.sh) restarts the
service on every `.py` edit and keeps the page fresh. `dev`, the default, is Vite's dev
server with hot module replacement on `:5173`, proxying to the service on `:8124`;
`build` rebuilds the real bundle into `web/dist/` on each edit; `api` runs the service
alone. It runs `serve`, so it needs the password above, or `RC_DASH_OPEN=1` on a machine
only you can reach. Its feed defaults are the development cluster's own paths, as are
the wrappers beside it (`run-dev.sh`, `run-fasrc.sh`; [Two deployments on this
host](dashboard/README.md#two-deployments-on-this-host)), so set the roots anywhere else:

```bash
cd dashboard && RC_DASH_EBPF_ROOTS=/var/log/ebpfm RC_DASH_NODE_ROOTS=/var/log/ebpfm-login ./run.sh
```

**The page alone**, with no service and no cluster: a Vite middleware answers the routes the
tabs are drawn from, and the `/ws` handshake, from a static fixture
([`dashboard/web/README.md`](dashboard/web/README.md#dev)):

```bash
cd dashboard/web && npm ci && npm run dev     # http://localhost:5173
```

### Demo, with no cluster

The service runs unchanged over a synthetic collector feed it writes and keeps appending
to, for fictional hosts and users; every record and every page says *fake data*
([Demo mode](dashboard/README.md#demo-mode)). No cluster, collector or root:

```bash
cd dashboard && uv sync && (cd web && npm ci && npm run build)
uv run python -m rc_dashboard demo        # http://127.0.0.1:8080
./run-demo.sh                             # or with hot reload, on :8087
```

The same demo is an image ([`docker/demo/Dockerfile`](docker/demo/Dockerfile)), and is
public at <https://dashboard-demo.agentic-system.org>, behind a Cloudflare Worker
([`deploy/cloudflare`](deploy/cloudflare/README.md)), redeployed from `develop` by
[`demo-deploy.yml`](.github/workflows/demo-deploy.yml) whenever a push changes it and every
test suite passes.

### Live, on a host of your own

[`deploy/fleet`](deploy/fleet/README.md) streams each node's collector files to that host
over ssh ([`fleet/`](fleet/README.md)) and runs the dashboard over them behind a password,
all set from one settings file.

## Documentation map

| document | covers |
|---|---|
| [`collector/README.md`](collector/README.md) | the two tiers, why eBPF (and why `sudo` does not escape the cgroup cap; `systemd-run` does), the concurrency model, **measured overhead**, the full event/field schema, `ebpfm.sh` verbs, kernel support |
| [`collector/WHAT-IT-COLLECTS.md`](collector/WHAT-IT-COLLECTS.md) | every signal the collector records, in plain words, by source |
| [`collector/COVERAGE.md`](collector/COVERAGE.md) | the field-by-field crosswalk: for each observable login-node signal, which tier reads it |
| [`collector/VENDORED.md`](collector/VENDORED.md) | the copied modules, why copies rather than symlinks, and the sync rule `ebpfm.sh check` enforces |
| [`dashboard/README.md`](dashboard/README.md) | feed resolution and config precedence, the history store (sealed days as Parquet), the time window and the hourly merge-reduce, the REST/WS API, the panel conventions, sign-in, every tab's method, known collector limitations the page surfaces, the module layout |
| [`dashboard/web/README.md`](dashboard/web/README.md) | the page: tabs, charts, filter and URL state, the dev fixture, and the integrity rules the code enforces |
| [`dashboard/risk-profiles/README.md`](dashboard/risk-profiles/README.md) | what counts as risky on a given cluster, and how to write and check a profile |
| [`dashboard/bench/README.md`](dashboard/bench/README.md) | a synthetic feed at fleet density, timers, a profiler, and the guard that the main served answers stayed the same |
| [`docs/figures/README.md`](docs/figures/README.md) | every figure on the page: what it computes, from which records, by which read path, at what cost |
| [`docs/feed-parquet/README.md`](docs/feed-parquet/README.md) | the feed as typed Parquet tables: the dashboard's history store, and an analysis dataset |
| [`docs/feed-format-v9/README.md`](docs/feed-format-v9/README.md) | a proposal for feed schema 9, not implemented, with a prototype that measures it |
| [`docs/plans/`](docs/plans/) | implementation plans, kept as the record once landed |
| [`fleet/README.md`](fleet/README.md) | the fleet stream's three programs, and what each one is for |
| [`deploy/fleet/README.md`](deploy/fleet/README.md) | the live service on a host of your own: the fleet stream from each node, the settings file, the first deploy, memory and disk, and the hourly check |
| [`deploy/cloudflare/README.md`](deploy/cloudflare/README.md) | the demo, over a synthetic feed, on a public URL |
| [`docs/logo/README.md`](docs/logo/README.md) | the mark and the lockups |

## The rules both halves obey

These are research-integrity constraints, not preferences; each one is a mistake this
project made once. They are stated per component in the READMEs above, but they are the
same rules:

- **Null is not zero, and zero is not missing.** A dropped BPF block emits `null`, never
  `0`: an absent measurement must not read as a zero measurement. Downstream, a nullable
  field carries `coverage_pct` and is never imputed; a present feed with no matching rows
  says `none`, while an absent feed names the path it expected.
- **Three actor classes, never two**: `agent` / `human` / `ide`. `ide` is any editor a person
  drives (VS Code, Cursor, Windsurf, other remote-IDE servers); the collector's `human-vscode`
  and its Cursor/Windsurf "agents" are mapped into it at ingest. Folding the IDE class into
  "agent" is what produced a retracted GPU near-parity claim.
- **Nothing is fabricated.** The collector emits only what it measured; the page contains
  no `Math.random()` (`npm run check:no-random` asserts it) and never interpolates between
  frames. A minute with no record is a hole in the line.
- **`sandbox` and `approval` are independent axes**, and `denied` (the collector being
  unable to read the field) is its own state, never folded into `unsandboxed`.
- **Scope is stated.** The eBPF tier runs on login nodes, so its resource figures are
  login-node use, not cluster use.

## Tests

Every suite runs from the repository root, needs no root and no BPF, and is what CI runs
on every pull request ([`ci.yml`](.github/workflows/ci.yml)):

```bash
python3 -m unittest discover -s collector/tests -q        # the collector
python3 -m unittest discover -s fleet/tests -q            # the fleet stream, over a fake collector
python3 -m unittest discover -s deploy/fleet/tests -q     # the deploy kit, with stand-ins for systemd, tmux and ssh
(cd dashboard && uv run python -m unittest discover -s tests -q)        # the service
(cd dashboard/web && npm ci && npm run typecheck && npm run check && npm run build)   # the page
```

`npm run check` runs every `check:*` script: no `Math.random`, then each
`mock/check_*.mjs` under `node --test`. CI also runs the collector's suite on EL8's own
Python 3.6, the one the login nodes run, and compiles the fleet sender there
(`python3 -m py_compile fleet/fleet_stream.py fleet/seed_mirror.py`), and runs the
dashboard's on 3.11 and 3.13.

## CI and releases

| workflow | runs on | does |
|---|---|---|
| [`ci.yml`](.github/workflows/ci.yml) | every pull request; pushes to `develop` and `main` | every test suite above; reads no secret and writes nothing |
| [`rpm-release.yml`](.github/workflows/rpm-release.yml) | a `vX.Y.Z` tag; a push to any branch but `develop` that changes `collector/` or `packaging/rpm/` | the collector's tests on EL8, then the RPM: attached to the tag's GitHub Release, or uploaded as a workflow artifact |
| [`base-image.yml`](.github/workflows/base-image.yml) | a `vX.Y.Z` tag; a push to any branch but `develop` that changes `docker/base/` | `ghcr.io/…/base` |
| [`collector-image.yml`](.github/workflows/collector-image.yml) | a `vX.Y.Z` tag; a push to any branch but `develop` that changes `collector/`, `docker/collector/` or `docker/base/` | `ghcr.io/…/collector`, on the base its ref should use |
| [`demo-deploy.yml`](.github/workflows/demo-deploy.yml) | a push to `develop` that changes what the demo runs | `ci.yml` first, then the public demo |

Pull requests go to `develop`; `main` is brought up to `develop` by a pull request of
its own.

## Requirements

- **Collector**: Linux with BCC (`python3-bpfcc` on Ubuntu, `python3-bcc` on
  RHEL/Rocky) and kernel headers for the eBPF tier; developed against 6.8 and 6.17, with a
  4.18 path for D-state dwell and the endpoint table. Its Python is held to 3.6, EL8's.
  The node tier needs neither: it reads `/proc` and `/sys` and runs `ps`, `free`,
  `uptime`, `w`, `vmstat`, `mpstat`, `iostat`, `sar`, `nfsiostat` and `ss` (procps-ng,
  sysstat, nfs-utils, iproute); `check` fails without `ps`, and a missing tool blanks its
  section. `bootstrap` installs the eBPF tier's dependencies; the RPM pulls in all of them.
- **Dashboard**: Python ≥ 3.11 with what `dashboard/pyproject.toml` lists (`fastapi`,
  `uvicorn`, `numpy`, `orjson`, `pyarrow`; `uv sync` installs them), Node for the
  frontend build (`react`, `react-dom` at runtime; no charting library, every chart is
  hand-rolled SVG).

## Paths and names from elsewhere

The collector and dashboard READMEs were written inside a larger measurement repository
and still refer to trees that are not part of this one (`collect/`, `analyze/`,
`findings/`). Those references are provenance, and the code does not depend on them: the
collector has no `../` imports, and the dashboard resolves every feed path from config,
env, or a probed candidate list.

One legacy name survives only in old data. The collector folder was once
`eBPF_marthen_new/` and is [`collector/`](collector/) here, and through schema 6 every
record's envelope stamped `collector = "ebpf_marthen_new"` and `source = "ebpf"`. Schema 7
drops both: they were the same on every line, nothing read them, and the feed's own
location already says where a record came from. Captures from before the change still
carry them.
