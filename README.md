<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/logo/lockup-dark.svg">
    <img src="docs/logo/lockup.svg" alt="Agent Observatory, Harvard MaDSys" width="400">
  </picture>
</p>

# agentic-hpc-dashboard

The aims of this repository is to collect agent behaviors on HPC login nodes (the collector), and make them visible to human (the dashboard).

<p>
  <a href="https://madsys.seas.harvard.edu/"><img src="dashboard/web/public/logos/madsys.png" alt="Harvard University MaDSys: Measurement and Design of Systems" height="64"></a>
</p>

<p>
  <a href="https://seas.harvard.edu/"><img src="dashboard/web/public/logos/harvard-seas.svg" alt="Harvard John A. Paulson School of Engineering and Applied Sciences" height="44"></a>
  &nbsp;&nbsp;
  <a href="https://www.rc.fas.harvard.edu/"><img src="dashboard/web/public/logos/fasrc.png" alt="FAS Research Computing, Harvard University" height="32"></a>
</p>

## Quick start -- Collector (`/collector`)
A single file (`ebpfm.sh`) acts as a single entry point for the collectors.
Its commands are:
```bash
sudo ./collector/ebpfm.sh bootstrap    # dependencies + the two sysctls, once per node
sudo ./collector/ebpfm.sh check        # expect 0 FAIL
sudo ./collector/ebpfm.sh start        # start the collector
sudo ./collector/ebpfm.sh status       # check the collector runtime
sudo ./collector/ebpfm.sh stop         # stop the collector
```

### Collector -- RPM install (Rocky/RHEL 8)

A signed-off release publishes an `ebpfm-collector` RPM (noarch, built for
EL8 / Rocky 8.10, **x86_64** hosts) as a GitHub Release artifact. Install it,
run the one-time bootstrap, then enable the service:

```bash
sudo dnf install ./ebpfm-collector-<version>-<release>.el8.noarch.rpm
sudo ebpfm check         # expect 0 FAIL on real hardware
sudo systemctl enable --now ebpfm
sudo systemctl status ebpfm
```

The RPM lays down:

| Path | What |
|---|---|
| `/usr/lib/ebpfm/` | the standalone collector bundle (`ebpf_trace.py`, `node_snapshot.py`, `lib/`, `ebpfm.sh`) |
| `/usr/bin/ebpfm` | symlink to `ebpfm.sh` — the entry point for `check`/`status`/`tail` |
| `/usr/lib/systemd/system/ebpfm.service` | the eBPF-tier unit, paths pre-substituted (no `install-unit` needed) |
| `/usr/lib/sysctl.d/99-ebpfm.conf` | the two required sysctls, applied on install and every boot |
| `/etc/default/ebpfm` | the unit's environment: the collector's settings, kept across upgrades |
| `/var/log/ebpfm/` | default output directory |

Dependencies and sysctls are handled by the package, not a separate bootstrap
step:

* **Package deps** (`python3-bcc`, `bpftool`, `iproute`, `jq`, `sysstat`,
  `nfs-utils`, `procps-ng`) are pulled in by `dnf`. `python3-bcc` lives in EPEL,
  so the RPM `Requires: epel-release`; its `-devel` deps live in PowerTools,
  which the RPM's `%post` enables (`powertools`/`PowerTools`/`crb`).
* **Sysctls** (`kernel.sched_schedstats`, `kernel.task_delayacct`) ship as the
  `99-ebpfm.conf` drop-in above. `%post` applies them immediately via
  `systemd-sysctl`, and systemd re-applies them on every boot — no reboot and no
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
before enabling.

Release vs. feature builds:

* **Releases** — a semver git tag `vX.Y.Z` builds `ebpfm-collector-X.Y.Z-1.el8`
  and attaches it to the GitHub Release for that tag.
* **Feature branches** — a push to any branch builds
  `ebpfm-collector-0.0.0-<YYYYMMDD>.<shortsha>.el8` and uploads it as a workflow
  **artifact** (not a release). The `0.0.0` version keeps a branch RPM sorting
  below any real release, and the datestamp+sha makes each build identifiable.

### Collector -- container image

The collector also ships as a container image on the GitHub Container Registry,
built on a project Rocky 8.10 base image. **x86_64 only** at present.

```
ghcr.io/harvardmadsys/agentic-hpc-dashboard/collector     # the collector
ghcr.io/harvardmadsys/agentic-hpc-dashboard/base          # the Rocky 8.10 base it builds on
```

Image tags follow the same scheme as the RPM:

* semver tag `vX.Y.Z` → `X.Y.Z`, `X.Y`, `latest`
* branch push → `<branch>` (moving) and `<branch>-<YYYYMMDD>-<sha>` (immutable)
* `main` → additionally `edge`

`latest` is published with the first `vX.Y.Z` tag; until then, `edge`, main's newest
build, is the one to pull.

Running the eBPF tier in a container needs host-level privileges, because eBPF
attaches to the host kernel and the collector attributes **host** processes. On
Rocky 8.10 physical hardware:

```bash
# One-time, on the HOST (sysctls are not namespaced):
sudo sysctl -w kernel.sched_schedstats=1 kernel.task_delayacct=1

docker run -d --name ebpfm \
  --privileged \                       # eBPF load + attach on EL8's 4.18 kernel
  --pid=host \                         # see/attribute every process on the node
  -v /sys:/sys:ro \                    # BTF + tracefs for program load
  -v /var/log/ebpfm:/var/log/ebpfm \   # persist the JSONL output
  -e EBPFM_HOST="$(hostname -s)" \     # name day-files for the node, not the container
  ghcr.io/harvardmadsys/agentic-hpc-dashboard/collector:latest
```

Notes:

* The container entrypoint runs `ebpf_trace.py` **in the foreground** as PID 1's
  child — it deliberately does *not* use `ebpfm.sh start`'s `systemd-run` cgroup
  escape, which only applies to the capped user slice on a bare login node and
  has no host systemd to talk to inside a container. `docker stop` delivers the
  SIGTERM the collector turns into its `truncated` flush.
* Other subcommands are reachable by passing them as arguments, e.g.
  `docker run --rm ... ghcr.io/.../collector:latest ebpfm check` or
  `... ebpfm features`.
* The collector emits **only** to the mounted output directory; it makes
  no outbound network connections.

## Quick start -- Dashboard (`/dashboard`)
Step-by-step
```bash
cd dashboard && uv sync
uv run python -m rc_dashboard --check-feeds       # what resolved, what didn't, and why
cd web && npm ci && npm run build && cd ..        # builds web/dist/, which the service serves
uv run python -m rc_dashboard new-password        # once: put the hash it prints in dashboard.config.json
uv run python -m rc_dashboard serve --port 8080
```
`serve` starts only behind a password, or with `--open` on a machine only you can reach
([`dashboard/README.md`](dashboard/README.md#password)).

Bash script bundling
```bash
bash run.sh
```

Demo mode, with no cluster, collector or root: the same service over a synthetic feed it
writes itself, for fictional login nodes and users. Every page says *fake data*.
```bash
cd dashboard && uv run python -m rc_dashboard demo     # after the web build above; http://127.0.0.1:8080
bash run-demo.sh                                       # or with hot reload, on :8087
```
As an image (`docker/demo/Dockerfile`), or on a public URL behind a Cloudflare Worker,
<https://dashboard-demo.agentic-system.org>: [`deploy/cloudflare`](deploy/cloudflare/README.md),
redeployed from `develop` by `.github/workflows/demo-deploy.yml` whenever a push changes it.

The real service, live, on a host of your own: [`deploy/fleet`](deploy/fleet/README.md)
streams each node's collector files to that host over ssh (`fleet/fleet_stream.py`) and
runs the dashboard over them behind a password, all set from one settings file.

## Configuration -- Dashboard
```bash
RC_DASH_EBPF_ROOTS=/var/log/ebpfm        # Where the ebpf collected data reside
RC_DASH_NODE_ROOTS=/var/log/ebpfm-login  # Where the other data reside (node-level)
RC_DASH_HISTORY=1                        # keep every sealed (.jsonl.gz) day-file as Parquet too (0: off)
RC_DASH_HISTORY_WORKERS=0                # processes converting in the background: 0 (default) = up to 8
RC_DASH_INGEST_WORKERS=1                 # processes reducing the hours: 1 (default) = one pass; N = one node per process
RC_DASH_INGEST_REDUCER_GROUPS=2          # with N >= 2: each node's hour in 2 processes, one per reducer group (1: one)
RC_DASH_ROLLUP_DIR=/var/lib/rc-dashboard # where the Trend tab's roll-up table is kept (default <cache.dir>/rollup)
```
See [History in Parquet](dashboard/README.md#history-in-parquet) for what the store holds,
which reads it answers, and what it costs.
