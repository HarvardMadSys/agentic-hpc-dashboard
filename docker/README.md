# Container images

Three images, each from a Dockerfile here, each built with the repository root as the build
context:

| image | Dockerfile | what | published by |
|---|---|---|---|
| `ghcr.io/harvardmadsys/agentic-hpc-dashboard/base` | [`base/Dockerfile.rocky810`](base/Dockerfile.rocky810) | Rocky Linux 8.10 with EPEL and PowerTools enabled, and git, rsync, curl and rpm-build | [`base-image.yml`](../.github/workflows/base-image.yml) |
| `ghcr.io/harvardmadsys/agentic-hpc-dashboard/collector` | [`collector/Dockerfile`](collector/Dockerfile) | the [collector](../collector/README.md), built from `base`; **x86_64 only** | [`collector-image.yml`](../.github/workflows/collector-image.yml) |
| `rc-dashboard-demo`, a local tag | [`demo/Dockerfile`](demo/Dockerfile) | the dashboard's [demo mode](../dashboard/README.md#demo-mode), over a synthetic feed | not to GHCR: [`deploy/cloudflare`](../deploy/cloudflare/README.md) builds it and runs it as the public demo |

## Tags

`base` and `collector` are built when a push changes what goes into them, and tagged:

* semver tag `vX.Y.Z` → `X.Y.Z`, `X.Y` and `latest`
* a push to a branch other than `develop` → `<branch>` (moving) and
  `<branch>-<YYYYMMDD>-<sha>` (immutable); a push to `develop` builds nothing
* `main` → also `edge`

No `vX.Y.Z` tag has been pushed yet, so there is no `latest`: pull `edge`, main's newest
build, as the examples below do.

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
| `-e EBPFM_HOST=…` | to name the day-files for the node, not the container |

On a 5.14 or later kernel, set `kernel.task_delayacct=1` too. EL8's 4.18 has no such knob, and
its delay accounting is on by default ([`packaging/rpm`](../packaging/rpm/README.md#dependencies-and-kernel-settings)).

- The entrypoint ([`collector/entrypoint.sh`](collector/entrypoint.sh)) `exec`s
  `ebpf_trace.py` **in the foreground**, as the container's main process, not through
  `ebpfm.sh start`, whose `systemd-run` escapes a login node's capped user slice: a container
  has no such slice, and no host systemd to ask. `docker stop` sends the SIGTERM that the
  collector answers by writing every process still running as `truncated`.
- The other commands are arguments:
  `docker run --rm … ghcr.io/harvardmadsys/agentic-hpc-dashboard/collector:edge ebpfm check`,
  or `… ebpfm features`.
- The collector writes **only** to its output folders (`/var/log/ebpfm` for the eBPF tier,
  `/var/log/ebpfm-login` for the node tier). Its one use of the network is resolving the host
  names that command lines mention (`getaddrinfo`, through the host's resolver), to name the
  endpoints a process talked to; `EBPFM_ARGV_HOSTS=0` turns that off.

To build it yourself, from the repository root, on `base:edge` until a release publishes
`base:latest`:

```bash
docker build -f docker/collector/Dockerfile \
  --build-arg BASE_IMAGE=ghcr.io/harvardmadsys/agentic-hpc-dashboard/base:edge \
  -t ebpfm-collector .
```

## The demo

```bash
docker build -f docker/demo/Dockerfile -t rc-dashboard-demo .
docker run --rm -p 8080:8080 rc-dashboard-demo          # http://127.0.0.1:8080
```

No collector, no cluster, no root. The feed is written afresh inside the container at each
start, two days of it, and every window is built from it before the port opens, so a cold
start takes a while. The page is built in a Node 22 stage, and the service runs on
Python 3.13. Ask LLM is off: the image has no agent CLI, and a public demo must not spend
anyone's model budget. It serves without a password and answers under any host name, since
its data is made up; the public copy's Cloudflare Worker asks for a password of its own.
