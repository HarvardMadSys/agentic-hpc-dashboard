# One server: the collector and the dashboard together

One Linux server where you have root runs both: the collector records what every process on
it does, and the dashboard reads those files where they are written and serves the page.
This page is the install; [`../README.md`](../README.md) has the rest.

## Install

On the server, as root:

```bash
tar -C /opt -xzf agentic-hpc-dashboard-1.0.0.tar.gz        # a release's kit, where any account can read it
cd /opt/agentic-hpc-dashboard-1.0.0                         # (or a clone of the repository: it builds its own code)
deploy/rc-deploy init                  # asks whether it runs Slurm, and picks this kit
sudo deploy/rc-deploy plan             # what it would do; nothing done
sudo deploy/rc-deploy apply            # deploy/single/install.sh, with the settings init wrote
sudo deploy/rc-deploy password         # makes the password, puts its hash in place, starts the dashboard
```

or the kit's own scripts, with the settings in the environment:

```bash
sudo SLURM=yes deploy/single/install.sh        # --plan first shows what it would do
sudo deploy/common/password.sh
```

`install.sh` runs [`common/install-collector.sh`](../common/install-collector.sh), which
installs the collector's dependencies and sysctls (`ebpfm.sh bootstrap`), checks the kernel
(`ebpfm.sh check`, which must report no `FAIL`) and runs both tiers as units, then
[`common/install-dashboard.sh single`](../common/install-dashboard.sh), which makes the
dashboard's account (in the collector's group), venv (the release's wheel, or the
checkout's dependencies and page bundle), settings, unit and its hourly check
([What each piece is](../README.md#what-each-piece-is)). Each says what it does as it goes.

Options: `--no-bootstrap` when the collector's dependencies are already in place,
`--no-check` to go on even though the check reports a `FAIL`, `--restart` to restart a
collector already running without the group ([Upgrading](../README.md#upgrading)),
`--no-build` to keep the venv and the bundle as they are, and `--plan` to print what it
would do and change nothing. Settings (`SLURM`, `SITE_NAME`, `MEMORY_MAX`, `EBPF_DIR`, ...):
[Settings](../README.md#settings); a later run is told none of them again.

Then open the page ([Opening the page](../README.md#opening-the-page)).

## Running it

| | |
|---|---|
| Watch | `sudo deploy/rc-deploy status`, and the hourly check: [Watching it](../README.md#watching-it) |
| Upgrade | `sudo deploy/rc-deploy upgrade`, or `git pull` and `sudo deploy/single/install.sh --no-bootstrap`: [Upgrading](../README.md#upgrading) |
| Back up | `sudo deploy/rc-deploy backup`: [What to back up](../README.md#what-to-back-up) |
| Move | [Moving the dashboard to another host](../README.md#moving-the-dashboard-to-another-host); the collector stays with the server it watches |
| Remove | `sudo deploy/rc-deploy uninstall [--purge]`: [Removing everything](../README.md#removing-everything) |
| Troubleshoot | [Troubleshooting](../README.md#troubleshooting) |
