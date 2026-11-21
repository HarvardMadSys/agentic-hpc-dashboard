# HPC: the dashboard over a cluster's login nodes

What is here runs the same collector and the same dashboard as the rest of the repository,
over a cluster's login nodes, under the limits such a site sets: the collector is installed
by the site's administrators (the [RPM](../packaging/rpm)), and you have an ordinary account
on the nodes and on the dashboard's host, with no root on either.

| Folder | What |
|---|---|
| [`fleet/`](fleet/) | `fleet_stream.py`: carries each node's collector files to the dashboard's host as they are written, over one ssh connection per node, started by a forced-command key that can run nothing else. Python 3.6 standard library only, so the nodes' `/usr/bin/python3` runs the sender with nothing installed. Also `seed_mirror.py` and `pull-gz.sh`, which seed a new copy from sealed days pulled ahead of time. |
| [`deploy/`](deploy/) | The kit that runs the stream and the dashboard on a host of your own: one settings file (`site.env`), systemd *user* units, linger, a tmux session, memory-capped scopes, the hourly check, and the privacy checks a shared host needs. [`deploy/README.md`](deploy/README.md) is the runbook. |

## Which deployment to use

| Where the collectors run | Use |
|---|---|
| A cluster's login nodes, where you have no root, the nodes share one home, and the dashboard's host is shared too | this folder: [`hpc/deploy`](deploy/README.md) |
| One server of your own, which runs the collector and the dashboard | [`deploy/single`](../deploy/single/README.md) |
| Servers of your own, each with its collector, and one more that runs the dashboard and pulls their files with rsync | [`deploy/fleet`](../deploy/fleet/README.md) |

The three read the collector's day-files the same way, so the dashboard cannot tell which one
brought them: every mode lays a node's files out as `<root>/<YYYY-MM-DD>/<host>.<stream>.jsonl`,
and a sealed day as the collector's own `.jsonl.gz`.

## What stays where it is

Some of the rest of the repository is about HPC too. It stays where it is, because it is part
of the collector or the dashboard, switched by a setting, rather than a separate part that a
deployment adds:

- **Slurm.** The collector records `sbatch`/`salloc`/`srun` submissions as they happen; with
  no Slurm on a host there are none. The dashboard's Slurm tab is off with `RC_DASH_SLURM=0`
  (`slurm.enabled`), which the two modes in [`deploy/`](../deploy/README.md) set.
- **What a node is called.** The page says "login nodes" unless `RC_DASH_NODE_NOUN`
  (`ui.node_noun`) names them otherwise; the two modes set "servers".
- **Risk rules per cluster.** [`dashboard/risk-profiles/`](../dashboard/risk-profiles/README.md)
  holds one profile per site (`fasrc.json` among them), chosen by `RC_DASH_RISK_PROFILE`.
- **The RPM.** [`packaging/rpm`](../packaging/rpm) builds the collector for EL8 and writes to
  `/scratch/ebpfm`, the login nodes' large local volume. A server without `/scratch` can use it
  with a unit drop-in for `EBPFM_OUTPUT_DIR`, or install the collector from this checkout as
  [`deploy/`](../deploy/README.md) does.
- **The dashboard's own run scripts.** `dashboard/run-fasrc.sh` (a FAS RC capture) and
  `dashboard/run-dev.sh` (the dev cluster) are the hot-reload loop of `dashboard/run.sh`,
  pointed at two feeds; they serve development, not a deployment.

## Tests

```bash
python3 -m unittest discover -s hpc/fleet/tests -q    # the stream, over a fake collector
python3 -m unittest discover -s hpc/deploy/tests -q   # the kit, with stand-ins for systemd, tmux, and ssh
```
