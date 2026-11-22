# The `ebpfm-collector` RPM

The [collector](../../collector/README.md) packaged for EL8: Rocky or RHEL 8.10, on **x86_64**
hosts. The package itself is `noarch`, built from [`ebpfm-collector.spec`](ebpfm-collector.spec)
in a Rocky 8.10 container by [`rpm-release.yml`](../../.github/workflows/rpm-release.yml), after
the collector's tests pass there. It installs the collector, a systemd unit for the eBPF tier
and the two kernel settings that tier needs, and `dnf` resolves every dependency, so no
`bootstrap` step is needed.

## Getting it

- **Releases.** A semver tag `vX.Y.Z` builds `ebpfm-collector-X.Y.Z-1.el8` and attaches it to
  the GitHub Release for that tag. No release has been tagged yet.
- **Branch builds.** A push to any branch but `develop` (`main` included) that changes
  `collector/`, `packaging/rpm/` or the workflow, or a manual run of the workflow, builds
  `ebpfm-collector-0.0.0-<YYYYMMDD>.<shortsha>.el8` and uploads it as a workflow **artifact**,
  not a release; the `0.0.0` sorts it below any real release. A push to `develop` builds
  nothing.

## Installing it

```bash
sudo dnf install ./ebpfm-collector-<version>-<release>.el8.noarch.rpm
sudo ebpfm check                      # expect 0 FAIL on real hardware
sudo systemctl enable --now ebpfm
sudo systemctl status ebpfm
```

The service is **not** enabled on install: run `ebpfm check` first. The unit runs the eBPF
tier only; for the node tier as well, run `ebpfm start` as an ordinary user.

## What it lays down

| path | what |
|---|---|
| `/usr/lib/ebpfm/` | the collector (`ebpf_trace.py`, `node_snapshot.py`, `lib/`, `ebpfm.sh`, `README.md`, `.vendored.sha256`) |
| `/usr/bin/ebpfm` | a symlink to `ebpfm.sh`, the entry point for `check`, `status`, `tail` and the rest |
| `/usr/lib/systemd/system/ebpfm.service` | the eBPF tier's unit, its paths filled in (no `install-unit` needed) |
| `/usr/lib/sysctl.d/99-ebpfm.conf` | the two required kernel settings, applied on install and at every boot |
| `/etc/default/ebpfm` | the unit's environment: the collector's settings, kept across upgrades |

The unit writes to `/scratch/ebpfm/` (its `EBPFM_OUTPUT_DIR`, which `/etc/default/ebpfm` can
override) rather than `/var/log`: the exits stream can pass 1 GB a day, and `/scratch` is the
large local volume where the root disk is small. The package does not own that folder, since
`/scratch` may be a separate mount that is absent at install time: the collector creates it on
its first start, and the unit waits for `/scratch` to be mounted (`RequiresMountsFor=/scratch`).

## Dependencies and kernel settings

- **Packages** (`python3-bcc`, `bpftool`, `iproute`, `jq`, `sysstat`, `nfs-utils`,
  `procps-ng`) are pulled in by `dnf`. `python3-bcc` lives in EPEL, so the RPM requires
  `epel-release`; its `-devel` dependencies live in PowerTools, which the RPM's `%post`
  enables (`powertools`, `PowerTools` or `crb`).
- **Kernel settings** (`kernel.sched_schedstats`, `kernel.task_delayacct`) ship as the
  `99-ebpfm.conf` drop-in, which `%post` applies at once through `systemd-sysctl` and systemd
  applies again at every boot, so no reboot is needed. EL8's 4.18 kernel has **no writable
  `kernel.task_delayacct`** (it arrived upstream in 5.14), but delay accounting is compiled in
  (`CONFIG_TASK_DELAY_ACCT=y`) and on by default, so the data is still collected. The drop-in
  lists the key with a leading dash (`-kernel.task_delayacct`), so `systemd-sysctl` skips the
  absent knob silently, and `ebpfm check` reports it as a non-blocking WARN ("no such knob
  (compile-time only)"). `sched_schedstats` exists on 4.18 and is set to 1.

## The files here

| file | what |
|---|---|
| [`ebpfm-collector.spec`](ebpfm-collector.spec) | the spec; the workflow sets Version and Release (`--define _ebpfm_version …`, `_ebpfm_release …`) |
| [`99-ebpfm.conf`](99-ebpfm.conf) | the sysctl drop-in |
| [`ebpfm.default`](ebpfm.default) | `/etc/default/ebpfm`: every setting commented out at its default, but `EBPFM_MIN_UID=0` |
