<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/logo/lockup-dark.svg">
    <img src="docs/logo/lockup.svg" alt="Agent Observatory, Harvard MaDSys" width="400">
  </picture>
</p>

# agentic-hpc-dashboard

The aim of this repository is to collect agent behavior on HPC login nodes (the collector) and make it visible to humans (the dashboard).

<p>
  <a href="https://madsys.seas.harvard.edu/"><img src="dashboard/web/public/logos/madsys.png" alt="Harvard University MaDSys: Measurement and Design of Systems" height="64"></a>
</p>

<p>
  <a href="https://seas.harvard.edu/"><img src="dashboard/web/public/logos/harvard-seas.svg" alt="Harvard John A. Paulson School of Engineering and Applied Sciences" height="44"></a>
  &nbsp;&nbsp;
  <a href="https://www.rc.fas.harvard.edu/"><img src="dashboard/web/public/logos/fasrc.png" alt="FAS Research Computing, Harvard University" height="32"></a>
</p>

## Quick start -- Collector (`/collector`)
One script, `ebpfm.sh`, is the entry point for the collectors.
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
| `/usr/bin/ebpfm` | symlink to `ebpfm.sh`, the entry point for `check`/`status`/`tail` |
| `/usr/lib/systemd/system/ebpfm.service` | the eBPF-tier unit, paths pre-substituted (no `install-unit` needed) |
| `/usr/lib/sysctl.d/99-ebpfm.conf` | the two required sysctls, applied on install and every boot |
| `/etc/default/ebpfm` | the unit's environment: the collector's settings, kept across upgrades |
| `/var/log/ebpfm/` | default output directory |

The package handles dependencies and sysctls itself, so there is no separate
bootstrap step:

* **Package deps** (`python3-bcc`, `bpftool`, `iproute`, `jq`, `sysstat`,
  `nfs-utils`, `procps-ng`) are pulled in by `dnf`. `python3-bcc` comes from
  EPEL, so the RPM has `Requires: epel-release`. Its `-devel` deps come from
  PowerTools, which the RPM's `%post` script enables (`powertools`/`PowerTools`/`crb`).
* **Sysctls** (`kernel.sched_schedstats`, `kernel.task_delayacct`) ship as the
  `99-ebpfm.conf` drop-in listed above. `%post` applies them immediately via
  `systemd-sysctl`, and systemd re-applies them on every boot, so you don't need
  to reboot or run `bootstrap`.
  * On EL8's 4.18 kernel, `kernel.task_delayacct` has **no writable sysctl**
    (upstream added it in 5.14). Delay accounting is still compiled in
    (`CONFIG_TASK_DELAY_ACCT=y`) and on by default, so the data is still
    collected. The drop-in lists the key with a leading dash
    (`-kernel.task_delayacct`), so `systemd-sysctl` silently skips the missing
    knob instead of logging `Couldn't write '1' to 'kernel/task_delayacct'`.
    `ebpfm check` reports it as a non-blocking WARN ("no such knob — compile-time
    only"). `sched_schedstats` does exist on 4.18 and is set to 1 as usual.

Installing the RPM does **not** enable the service. Run `ebpfm check` (expect
0 FAIL) before you enable it.

Release vs. feature builds:

* **Releases**: a semver git tag `vX.Y.Z` builds `ebpfm-collector-X.Y.Z-1.el8`
  and attaches it to the GitHub Release for that tag.
* **Feature branches**: a push to any branch builds
  `ebpfm-collector-0.0.0-<YYYYMMDD>.<shortsha>.el8` and uploads it as a workflow
  **artifact** (not a release). The `0.0.0` version makes a branch RPM sort
  below any real release, and the date stamp plus sha makes each build identifiable.

### Collector -- container image

The collector also ships as a container image on the GitHub Container Registry,
built on the project's own Rocky 8.10 base image. It is **x86_64 only** for now.

```
ghcr.io/harvardmadsys/agentic-hpc-dashboard/collector     # the collector
ghcr.io/harvardmadsys/agentic-hpc-dashboard/base          # the Rocky 8.10 base it builds on
```

Image tags follow the same scheme as the RPM:

* semver tag `vX.Y.Z` → `X.Y.Z`, `X.Y`, `latest`
* branch push → `<branch>` (moving) and `<branch>-<YYYYMMDD>-<sha>` (immutable)
* `main` → additionally `edge`

`latest` is published with the first `vX.Y.Z` tag. Until then, pull `edge`, which is
main's newest build.

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

* The container entrypoint runs `ebpf_trace.py` **in the foreground** as a child
  of PID 1. It deliberately does *not* use the `systemd-run` cgroup escape from
  `ebpfm.sh start`. That escape only matters for the capped user slice on a bare
  login node, and inside a container there is no host systemd for it to talk to.
  `docker stop` sends a SIGTERM, which the collector handles with its `truncated` flush.
* To run other subcommands, pass them as arguments, e.g.
  `docker run --rm ... ghcr.io/.../collector:latest ebpfm check` or
  `... ebpfm features`.
* The collector writes **only** to the mounted output directory. It makes
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
`serve` only starts with a password set, or with `--open` on a machine that only you can
reach ([`dashboard/README.md`](dashboard/README.md#password)).

Or, bundled as one bash script:
```bash
bash run.sh
```

Demo mode needs no cluster, collector or root. It runs the same service over a synthetic
feed that it writes itself, for fictional login nodes and users. Every page says *fake data*.
```bash
cd dashboard && uv run python -m rc_dashboard demo     # after the web build above; http://127.0.0.1:8080
bash run-demo.sh                                       # or with hot reload, on :8087
```
The demo also runs as an image (`docker/demo/Dockerfile`), and on a public URL behind a
Cloudflare Worker, <https://dashboard-demo.agentic-system.org> (see
[`deploy/cloudflare`](deploy/cloudflare/README.md)). `.github/workflows/demo-deploy.yml`
redeploys it from `develop` whenever a push changes it.

To run the real service live on a host of your own, see [`deploy/fleet`](deploy/fleet/README.md).
It streams each node's collector files to that host over ssh (`fleet/fleet_stream.py`) and
runs the dashboard on them behind a password. Everything is set from one settings file.

## Configuration -- Dashboard
```bash
RC_DASH_EBPF_ROOTS=/var/log/ebpfm        # Where the ebpf collected data reside
RC_DASH_NODE_ROOTS=/var/log/ebpfm-login  # Where the other data reside (node-level)
RC_DASH_HISTORY=1                        # keep every sealed (.jsonl.gz) day-file as Parquet too (0: off)
RC_DASH_HISTORY_WORKERS=0                # processes converting in the background: 0 (default) = up to 8
RC_DASH_INGEST_WORKERS=1                 # processes reducing the hours: 1 (default) = one pass; N = one node per process
RC_DASH_INGEST_REDUCER_GROUPS=2          # with N >= 2: each node's hour in 2 processes, one per reducer group (1: one)
```
See [History in Parquet](dashboard/README.md#history-in-parquet) for what the store holds,
which reads it answers, and what it costs.
