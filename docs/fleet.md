# Many nodes: a fleet

The [root README](../README.md#quick-start) runs everything on one node. This page is for
more than one: every node runs the collector and keeps its day-files on its own disk, and one
more host runs the dashboard over a copy of them, pulled with rsync every minute
([`deploy/fleet/pull.py`](../deploy/fleet/pull.py)). The copy grows each file in place, never
replacing it, because the dashboard knows a file by its inode and how far it has read.

## 1. Pick a layout

Two questions decide it: do you have root on the nodes, and on the dashboard's host?

| Layout | Root | What runs where | Guide |
|---|---|---|---|
| **A fleet** | on the nodes and the dashboard's host | every node runs the collector as systemd units; the dashboard's host runs the dashboard, and the copy as an account in the collector's group | [`deploy/fleet`](../deploy/fleet/README.md) |
| **A cluster's login nodes** | nowhere | the site's administrators install the collector (the [RPM](../packaging/rpm/README.md)); a host of your own runs the dashboard and the copy as a systemd user unit, through a key the nodes let run nothing but read-only rsync ([`hpc/fleet`](../hpc/fleet/README.md)), set from one file and checked every hour | [`hpc/deploy`](../hpc/deploy/README.md) |

[`deploy/README.md`](../deploy/README.md) covers what the root layouts share: the units, the
password, the hourly check, settings, upgrading, backups, moving and removing.
[`hpc/README.md`](../hpc/README.md) says what is HPC-specific.

## 2. Get the code onto the dashboard's host

Start from a release's kit, `agentic-hpc-dashboard-X.Y.Z.tar.gz` on the
[releases page](https://github.com/HarvardMadSys/agentic-hpc-dashboard/releases), unpacked
where any account can read it and it can stay (`/opt/agentic-hpc-dashboard`): it installs
that release's wheel, checked against the release's checksums, with nothing to build. A
checkout works the same way and builds its own code. The login nodes' layout ships a commit
from where it runs, so it needs a checkout: `git clone --branch vX.Y.Z` for a release.

## 3. Install the collector on every node

Where you have root, the fleet kit installs it on each node for you
([`deploy/fleet`](../deploy/fleet/README.md#install)). By hand, on each node:
`sudo ./collector/ebpfm.sh install` from a copy of the `collector/` folder, which is
self-contained, or from the single file `./collector/ebpfm.sh bundle` writes
(`./ebpfm-bundle.sh --extract /opt/ebpfm` unpacks it). The
[RPM](../packaging/rpm/README.md) for Rocky/RHEL 8 and the
[container image](../docker/README.md#the-collector) run the eBPF tier too. Running it day to
day: [`collector/OPERATIONS.md`](../collector/OPERATIONS.md).

## 4. Deploy the dashboard and the copy

One front door, [`deploy/rc-deploy`](../deploy/rc-deploy), picks the layout from four
questions (Slurm? which nodes? root on the nodes? root on the dashboard's host?) and runs
its scripts:

```bash
sudo deploy/rc-deploy init      # writes the settings file, and records the layout in it (hpc: no sudo)
sudo deploy/rc-deploy plan      # what it would do; nothing done (hpc runs as you, no sudo)
sudo deploy/rc-deploy apply
sudo deploy/rc-deploy password  # makes the password; its hash goes where the dashboard reads it
deploy/rc-deploy status         # every collector's verdict and every node's copy
```

`rc-deploy --help` lists the rest: `add-node`, `remove-node`, `upgrade`, `rollback`,
`backup`, `uninstall`.

## 5. Open the page

The dashboard listens on `127.0.0.1:8080` on its host, behind the password. From your
machine:

```bash
ssh -N -L 8080:127.0.0.1:8080 <you>@<the dashboard's host>
```

Then open <http://localhost:8080> and sign in. A reverse proxy in front of it:
[Opening the page](../deploy/README.md#opening-the-page).

## Watching it

`deploy/rc-deploy status`, or `rc-dashboard status` on the dashboard's host, says for each
node whether its collector works and whether its copy is current, so a failed copy is told
apart from a stopped collector (exit 0, 1 or 2, for a monitor). The kits run the same check
every hour: [Watching it](../deploy/README.md#watching-it),
[The hourly check](../hpc/deploy/README.md#the-hourly-check).

## Settings

The deployment kits take the same names for the same settings: `SITE_NAME`,
`SITE_DESCRIPTION`, `SLURM`, `NODE_NOUN`, `ANONYMIZE` (1/0, true/false, yes/no, on/off),
`MEMORY_MAX` and `FREE_MIN_GB`. They go in `/etc/rc-dashboard/site.env` (rc-deploy), the
deploy/ installers' environment, or `hpc/deploy/site.env`; each sets the dashboard's own
setting ([Settings](../deploy/README.md#settings)). The collector reads `/etc/default/ebpfm`
(`ebpfm.sh config` shows every setting).

## The copy and privacy

The copy is pulled with a key that runs read-only rsync of the collector's folders and
nothing else, only from the dashboard's host, into a folder only the dashboard's account can
read: by an account in the collector's group, through `rrsync -ro` where the node has it
([`deploy/fleet`](../deploy/fleet/README.md#how-the-copy-reads-as-the-node)), or, with no root
on the nodes, through [`hpc/fleet`](../hpc/fleet/README.md)'s guard. What the collector and the
dashboard do with the records on every node:
[Data and privacy](../README.md#data-and-privacy).
