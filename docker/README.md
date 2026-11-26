# Container images

Four images, each from a Dockerfile here, each built with the repository root as the build
context:

| image | Dockerfile | what | published by |
|---|---|---|---|
| `ghcr.io/harvardmadsys/agentic-hpc-dashboard/base` | [`base/Dockerfile.rocky810`](base/Dockerfile.rocky810) | Rocky Linux 8.10 with EPEL and PowerTools enabled, and git, rsync, curl and rpm-build | [`base-image.yml`](../.github/workflows/base-image.yml) |
| `ghcr.io/harvardmadsys/agentic-hpc-dashboard/collector` | [`collector/Dockerfile`](collector/Dockerfile) | the [collector](../collector/README.md), built from `base`; **x86_64 only** | [`collector-image.yml`](../.github/workflows/collector-image.yml) |
| `rc-dashboard-demo`, a local tag | [`demo/Dockerfile`](demo/Dockerfile) | the dashboard's [demo mode](../dashboard/README.md#demo-mode), over a synthetic feed | not to GHCR: [`deploy/cloudflare`](../deploy/cloudflare/README.md) builds it and runs it as the public demo |
| `ghcr.io/harvardmadsys/agentic-hpc-dashboard/dashboard` | [`dashboard/Dockerfile`](dashboard/Dockerfile) | the [dashboard](../dashboard/README.md) service, over day-files mounted in ([below](#the-dashboard)) | [`dashboard-image.yml`](../.github/workflows/dashboard-image.yml): `edge` from `develop`, `X.Y.Z`, `X.Y` and `latest` from a `vX.Y.Z` tag |

## Tags

`base` and `collector` are built when a push changes what goes into them, and tagged:

* semver tag `vX.Y.Z` → `X.Y.Z`, `X.Y` and `latest`
* a push to a branch other than `develop` → `<branch>` (moving) and
  `<branch>-<YYYYMMDD>-<sha>` (immutable); a push to `develop` builds nothing
* `main` → also `edge`

No `vX.Y.Z` tag has been pushed yet, so there is no `latest`: pull `edge`, main's newest
build, as the examples below do.

`dashboard` is tagged otherwise ([The dashboard](#the-dashboard)): a pull request or a push that
changes what goes into it is built and checked, and published nowhere; `develop` pushes `edge`,
and a `vX.Y.Z` tag pushes `X.Y.Z`, `X.Y` and `latest`.

## The collector

Running the eBPF tier in a container needs host-level privileges, because eBPF attaches to the
host's kernel and the collector attributes the **host's** processes. On Rocky 8.10, on
physical hardware:

```bash
# Once, on the HOST (kernel settings are not namespaced):
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
| `--privileged` | to load and attach eBPF programs on EL8's 4.18 kernel |
| `--pid=host` | to see and attribute every process on the node |
| `-v /sys:/sys:ro` | BTF and tracefs, to load the programs |
| `-v /var/log/ebpfm:/var/log/ebpfm` | to keep the day-files on the host |
| `-e EBPFM_HOST=…` | to name the day-files for the node, not the container (the tracer and every `ebpfm` command in the container use it) |

On a 5.14 or later kernel, set `kernel.task_delayacct=1` too. EL8's 4.18 has no such knob, and
its delay accounting is on by default ([`packaging/rpm`](../packaging/rpm/README.md#dependencies-and-kernel-settings)).

- The entrypoint ([`collector/entrypoint.sh`](collector/entrypoint.sh)) `exec`s
  `ebpf_trace.py` **in the foreground**, as the container's main process, not through
  `ebpfm.sh start`, whose `systemd-run` escapes a login node's capped user slice: a container
  has no such slice, and no host systemd to ask. `docker stop` sends the SIGTERM that the
  collector answers by writing every process still running as `truncated`.
- The other commands are arguments:
  `docker run --rm … ghcr.io/harvardmadsys/agentic-hpc-dashboard/collector:edge ebpfm check`,
  or `… ebpfm features`. The image sets `EBPFM_TIER=ebpf`, the one tier it runs, so `check`
  and `status` ask about that tier alone.
- `docker exec ebpfm ebpfm status` says whether the tracer runs and is still writing (exit 0
  healthy, 1 degraded or stale, 3 not running; `--json` for a monitor): the entrypoint
  leaves the pid file and the note of its settings that `status` reads.
  `docker exec ebpfm ebpfm version` names the collector, as each run's `meta` record does
  (`collector_version`).
- The collector writes **only** to its output folder (`/var/log/ebpfm`; the node tier, which
  this image does not run, would write to `/var/log/ebpfm-login`). Its one use of the network
  is resolving the host names that command lines mention (`getaddrinfo`, through the host's
  resolver), to name the endpoints a process talked to; `EBPFM_ARGV_HOSTS=0` turns that off.

To build it yourself, from the repository root, on `base:edge` until a release publishes
`base:latest`:

```bash
docker build -f docker/collector/Dockerfile \
  --build-arg BASE_IMAGE=ghcr.io/harvardmadsys/agentic-hpc-dashboard/base:edge \
  --build-arg EBPFM_VERSION="$(git describe --always --dirty)" \
  -t ebpfm-collector .
```

`EBPFM_VERSION` stamps the collector's version into the image (`ebpfm version`, and
`collector_version` in each run's `meta` record); without it, the image has no `.git` to
ask and names itself by a hash of its code (`sha256:…`).

## The demo

```bash
docker build -f docker/demo/Dockerfile -t rc-dashboard-demo .
docker run --rm -p 8080:8080 rc-dashboard-demo          # http://127.0.0.1:8080
```

No collector, no cluster, no root. The feed is written afresh inside the container at each
start, two days of it, and every window is built from it before the port opens, so a cold
start takes a while. The page is built in a Node 22 stage, and the service runs on
Python 3.13. It serves without a password and answers under any host name, since
its data is made up; the public copy's Cloudflare Worker asks for a password of its own.

## The dashboard

The [dashboard](../dashboard/README.md) service, from [`dashboard/Dockerfile`](dashboard/Dockerfile):
a Node 22 stage builds the page, a Python stage builds the package as a wheel with the page inside
it, and the image installs that wheel on Python 3.13 with the dependencies `dashboard/uv.lock`
pins. It reads the collector's day-files from `/feeds/ebpf` and `/feeds/node` and keeps what it must
keep in `/state` (`RC_DASH_CACHE_DIR=/state/cache`: the session key, the anonymize salt, the Trend
tab's roll-up table, and the caches rebuilt from the day-files), and serves `rc-dashboard serve` on
`0.0.0.0:8080`.

```bash
# Once: a password, its hash in the state volume (the password itself in /state/password).
docker run --rm -v rc-dashboard-state:/state ghcr.io/harvardmadsys/agentic-hpc-dashboard/dashboard:edge \
  rc-dashboard new-password --dir /state/password --install /state/password.hash

docker run -d --name rc-dashboard -p 127.0.0.1:8080:8080 \
  -v /var/log/ebpfm:/feeds/ebpf:ro -v /var/log/ebpfm-login:/feeds/node:ro \
  -v rc-dashboard-state:/state \
  --group-add "$(getent group ebpfm | cut -d: -f3)" \
  -e RC_DASH_PASSWORD_HASH_FILE=/state/password.hash \
  -e TZ=America/New_York \
  ghcr.io/harvardmadsys/agentic-hpc-dashboard/dashboard:edge
```

| option | why |
|---|---|
| `-p 127.0.0.1:8080:8080` | the page on the host's loopback only; reach it through `ssh -N -L 8080:127.0.0.1:8080 <host>`, or a proxy in front |
| `-v …:/feeds/ebpf:ro`, `-v …:/feeds/node:ro` | the collector's folders (or a fleet's copy), read-only. A glob over several mounts works: `-e RC_DASH_EBPF_ROOTS='/feeds/ebpf/*'` |
| `-v rc-dashboard-state:/state` | what survives a new container: without it every start makes a new session key (everyone signs in again) and a new salt (new aliases), and the Trend tab starts empty |
| `--group-add <gid>` | the collector writes its files 0640 for its group (`EBPFM_GROUP`); the image runs as uid 10001, which must be in it |
| `-e RC_DASH_PASSWORD_HASH_FILE=…` | **the image refuses to start without a password**, as the service does anywhere: a hash file (read again when it changes, and on `docker kill -s HUP rc-dashboard`), or `RC_DASH_PASSWORD_HASH`; `RC_DASH_OPEN=1` serves the page with none |
| `-e TZ=…` | the zone the page shows every time in: the cluster's ([The time zone](../dashboard/README.md#the-time-zone)) |

- Every other setting is an `RC_DASH_*` variable, or a config file mounted in and named by
  `RC_DASH_CONFIG` (`-v ./dashboard.toml:/etc/rc-dashboard/dashboard.toml:ro -e
  RC_DASH_CONFIG=/etc/rc-dashboard/dashboard.toml`); a relative path in it is beside it.
- The other commands are arguments: `docker exec rc-dashboard rc-dashboard status --json` (exit
  0, 1 or 2), `… rc-dashboard check-feeds`, `… rc-dashboard backup --to /state/backup.tar`.
- `rc-dashboard --version` names the commit the image was built from (`GIT_COMMIT`, stamped into
  the package).

To build it yourself, from the repository root:

```bash
docker build -f docker/dashboard/Dockerfile --build-arg GIT_COMMIT="$(git rev-parse HEAD)" -t rc-dashboard .
```

[`dashboard-image.yml`](../.github/workflows/dashboard-image.yml) builds it, and the wheel, on every
pull request and push that changes what goes into them (not a `.md` file alone), and checks that
the image refuses to start with no password and that the wheel carries the page. It pushes the image
only from `develop` (`edge`) and from a `vX.Y.Z` tag (`X.Y.Z`, `X.Y`, `latest`), and on a tag
attaches the wheel to the tag's GitHub Release, making the release if there is none. A tag stamps
its version into both, so `rc-dashboard --version` says `X.Y.Z`.
