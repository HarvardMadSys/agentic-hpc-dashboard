# One server: the collector and the dashboard together

One Linux server where you have root runs both: the collector records what every process on
it does, and the dashboard reads those files where they are written and serves the page.
This page is the install; [`../README.md`](../README.md) has the rest.

## Install

On the server, as root:

```bash
git clone <this repository> /opt/agentic-hpc-dashboard     # where any account can read it
cd /opt/agentic-hpc-dashboard
sudo deploy/single/install.sh
```

It runs [`common/install-collector.sh`](../common/install-collector.sh), which installs the
collector's dependencies and sysctls (`ebpfm.sh bootstrap`), checks the kernel
(`ebpfm.sh check`, which must report no `FAIL`) and runs both tiers as units, then
[`common/install-dashboard.sh single`](../common/install-dashboard.sh), which makes the
dashboard's account, venv, page bundle, settings and unit
([What each piece is](../README.md#what-each-piece-is)). Each says what it does as it goes.

Options: `--no-bootstrap` when the collector's dependencies are already in place,
`--no-check` to go on even though the check reports a `FAIL`, `--restart` to restart a
collector already running without the group ([Upgrading](../README.md#upgrading)), and
`--no-build` to keep the venv and the bundle as they are.

Then make the password ([The password](../README.md#the-password)) and open the page
([Opening the page](../README.md#opening-the-page)).
