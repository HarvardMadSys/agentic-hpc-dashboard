# HPC: the dashboard over a cluster's login nodes

What is here runs the same collector and the same dashboard as the rest of the repository
over a cluster's login nodes, under the limits such a site sets: the site's administrators
install the collector (the [RPM](../packaging/rpm)), and you have an ordinary account on the
nodes and on the dashboard's host, with no root on either. On servers where you have root,
use [`deploy/`](../deploy/README.md) instead.

| Folder | What |
|---|---|
| [`fleet/`](fleet/) | The guard: `rsync_guard.py`, the forced command of the key the dashboard's host copies each node with. It lets that key run read-only rsync of the collector's folder and nothing else. The copy itself is [`deploy/fleet/pull.py`](../deploy/fleet/pull.py), the fleet kit's. |
| [`deploy/`](deploy/) | The kit that runs the copy and the dashboard on a host of your own: one settings file (`site.env`), a systemd user unit, the hourly check, and the privacy checks a shared host needs. [`deploy/README.md`](deploy/README.md) is the runbook. [`../deploy/rc-deploy`](../deploy/rc-deploy) `init` writes `site.env` from four questions, and runs this kit's scripts (`apply`, `add-node`, `password`, `status`, `upgrade`, `rollback`, `backup`, `uninstall`). |

## What stays where it is

Some of the rest of the repository is about HPC too. It stays where it is because it is part
of the collector or the dashboard, switched by a setting:

- **Slurm.** The collector records `sbatch`/`salloc`/`srun` submissions as they happen; with
  no Slurm on a host there are none. `RC_DASH_SLURM=0` (`slurm.enabled`) turns off the
  dashboard's HPC tab; every kit takes `SLURM=yes|no` for it, and `rc-deploy init` asks.
- **What a node is called.** The page says "login nodes" unless `RC_DASH_NODE_NOUN`
  (`ui.node_noun`; `NODE_NOUN` in every kit) names them otherwise.
- **Risk rules per cluster.** [`dashboard/risk-profiles/`](../dashboard/risk-profiles/README.md)
  holds one profile per site (`fasrc.json` among them), chosen by `RC_DASH_RISK_PROFILE`.
- **The RPM.** [`packaging/rpm`](../packaging/rpm) builds the collector for EL8, writing to
  `/scratch/ebpfm`, the login nodes' large local volume.
- **The dashboard's own run scripts.** `dashboard/run-fasrc.sh` and `dashboard/run-dev.sh`
  point the hot-reload loop of `dashboard/run.sh` at two sites' feeds, for development.
