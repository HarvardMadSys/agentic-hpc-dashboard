# One server: the collector and the dashboard together

One Linux server where you have root runs both: the collector writes what every process on
it does, and the dashboard reads those files where they are written and serves the page.
[`../README.md`](../README.md) has what runs where, the settings, and the rest of the
day-to-day; this page is the install.

## Install

On the server, as root:

```bash
git clone <this repository> /opt/agentic-hpc-dashboard     # where any account can read it
cd /opt/agentic-hpc-dashboard
sudo deploy/single/install.sh
```

It runs two steps, each of which says what it does as it goes:

1. [`common/install-collector.sh`](../common/install-collector.sh): installs the collector's
   dependencies and its two sysctls (`ebpfm.sh bootstrap`), checks the kernel
   (`ebpfm.sh check`, which must report no `FAIL`), and runs both tiers as systemd units,
   `ebpfm` and `ebpfm-node`, writing to `/var/log/ebpfm` and `/var/log/ebpfm-login` for the
   group `ebpfm`.
2. [`common/install-dashboard.sh single`](../common/install-dashboard.sh): makes the account
   `rc-dashboard` in that group, the venv (`/opt/rc-dashboard/venv`, with uv), the page's
   bundle (npm), `/etc/rc-dashboard/dashboard.env` and `paths.env`, and the unit
   `rc-dashboard`.

Options: `--no-bootstrap` when the collector's dependencies are already in place,
`--no-check` to go on even though the collector's check reports a `FAIL`, `--restart` to let
it restart a collector already running without the group (see below), and `--no-build` to
keep the venv and the bundle as they are.

On a server whose collector already runs, started before its output had a group, the
install stops and says so: the collector reads the group only when it starts, and would go on
making files that only root can read, the dashboard's reads included. Run it again with
`--restart` to let it restart the collector, which flushes every process it is following as
`truncated`, or restart the collector yourself first.

Then make the password ([`../README.md`](../README.md#the-password)), put its hash in
`/etc/rc-dashboard/dashboard.env`, and:

```bash
sudo systemctl start rc-dashboard
ssh -N -L 8080:127.0.0.1:8080 <you>@<the server>      # on your machine; then http://localhost:8080
```

## Sharing the server with what it watches

The dashboard runs on the server it watches, so its own processes would show on its page:
they are dropped, and counted as dropped (`RC_DASH_DROP_USERS` in `dashboard.env`; take
`rc-dashboard` out of it to see what the dashboard costs). `MEMORY_MAX` (8G) caps its memory;
past the cap the kernel stops it and systemd starts it again.
