# agentic-hpc-dashboard

The aims of this repository is to collect agent behaviors on HPC login nodes (the collector), and make them visible to human (the dashboard).

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
ghcr.io/fasrc/agentic-hpc-dashboard/collector     # the collector
ghcr.io/fasrc/agentic-hpc-dashboard/base          # the Rocky 8.10 base it builds on
```

Image tags follow the same scheme as the RPM:

* semver tag `vX.Y.Z` → `X.Y.Z`, `X.Y`, `latest`
* branch push → `<branch>` (moving) and `<branch>-<YYYYMMDD>-<sha>` (immutable)
* `main` → additionally `edge`

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
  ghcr.io/fasrc/agentic-hpc-dashboard/collector:latest
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
* The collector emits **only** to the mounted JSONL output directory; it makes
  no outbound network connections.

## Quick start -- Dashboard (`/dashboard`)
Step-by-step
```bash
cd dashboard && uv sync
uv run python -m rc_dashboard --check-feeds       # what resolved, what didn't, and why
cd web && npm ci && npm run build && cd ..        # builds web/dist/, which the service serves
uv run python -m rc_dashboard serve --port 8080
```

Bash script bundling
```bash
bash run.sh
```

## Configuration -- Dashboard
```bash
RC_DASH_EBPF_ROOTS=/var/log/ebpfm        # Where the ebpf collected data reside
RC_DASH_NODE_ROOTS=/var/log/ebpfm-login  # Where the other data reside (node-level)
```
