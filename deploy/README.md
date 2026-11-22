# Running the collector and the dashboard on servers of your own

Two ways to run the same collector and dashboard on Linux servers where you have root. Each
server's collector writes its own day-files; the two modes differ only in where the
dashboard runs, and how the files reach it. This page covers what they share.

| Mode | What runs where | Start at |
|---|---|---|
| **single** | One server runs the collector and the dashboard. The dashboard reads the collector's folders where it writes them. | [`single/README.md`](single/README.md) |
| **fleet** | Every node runs the collector and keeps its files on its own disk. One more host runs the dashboard, and copies each node's files to itself with rsync, every minute (`pull.py`). | [`fleet/README.md`](fleet/README.md) |

On a cluster's login nodes, where you have no root, use [`hpc/deploy`](../hpc/deploy/README.md)
instead. For a public page over made-up data, use [`cloudflare`](cloudflare/README.md).

## What each piece is

| Unit | Runs | As | Made by |
|---|---|---|---|
| `ebpfm.service` | the collector's eBPF tier, `collector/ebpf_trace.py` | root | [`common/install-collector.sh`](common/install-collector.sh) (`ebpfm.sh install-unit`) |
| `ebpfm-node.service` | the collector's node tier, a snapshot every 5 minutes | root | the same |
| `rc-dashboard.service` | `rc_dashboard serve`, on `127.0.0.1:8080` behind a password | `rc-dashboard` | [`common/install-dashboard.sh`](common/install-dashboard.sh) |
| `rc-pull.service` (fleet) | [`fleet/pull.py`](fleet/pull.py) `loop`, the copy of every node | `rc-dashboard` | the same, `fleet` |

The collector's output belongs to the group `ebpfm`, mode 750/640: it names real users and
holds their commands. The dashboard reads it as a member of that group (single), or reads the
copy, which is its own account's alone (fleet).

| Where | What |
|---|---|
| `/var/log/ebpfm/<day>/<host>.{exits,snapshot}.jsonl[.gz]` | the eBPF tier's day-files, a sealed day gzipped |
| `/var/log/ebpfm-login/<day>/<host>.jsonl` | the node tier's |
| `/etc/default/ebpfm` | the collector's settings (`EBPFM_GROUP`, `EBPFM_MIN_UID`, ...) |
| `/etc/rc-dashboard/dashboard.env` | the dashboard's settings: yours, kept across installs |
| `/etc/rc-dashboard/paths.env` | where the dashboard reads and keeps things: the installer's, rewritten on every run |
| `/etc/rc-dashboard/fleet.env`, `pull_key` | (fleet) the nodes, and the key the copy logs in to them with |
| `/var/lib/rc-dashboard/` | the dashboard's cache and history store, and (fleet) the copy, `mirror/` |
| `/opt/rc-dashboard/venv` | the dashboard's Python packages; the code runs from the checkout |

The installers run from a checkout of this repository, and the units run the code in it,
so clone it where any account can read it, such as `/opt/agentic-hpc-dashboard`. They need
`systemd`; the dashboard's host needs [uv](https://docs.astral.sh/uv/) and Node.js 20 or
later to build (uv brings its own Python 3.11 or later where the host has none), and every
host of a fleet needs `rsync`.

## The password

The dashboard does not start until `dashboard.env` holds a password hash. Make one, as
yourself, on the dashboard's host:

```bash
cd /opt/agentic-hpc-dashboard/dashboard && /opt/rc-dashboard/venv/bin/python -m rc_dashboard new-password
```

It keeps the password in `~/.config/rc-dashboard/password`, readable by you alone, and
prints only the hash. Put the hash in `/etc/rc-dashboard/dashboard.env` as
`RC_DASH_PASSWORD_HASH='scrypt$...'`, in single quotes since it holds `$` signs, then
`sudo systemctl restart rc-dashboard`. A new hash signs everyone out. The cookie, proxies and
scripts: [Password](../dashboard/README.md#password).

## Opening the page

The page listens on `127.0.0.1:8080` on the dashboard's host (`RC_DASH_HOST`,
`RC_DASH_PORT`). From your machine:

```bash
ssh -N -L 8080:127.0.0.1:8080 <you>@<the dashboard's host>
```

Then open <http://localhost:8080> and sign in. For an address of its own, put a reverse
proxy on the host in front of the port; one that rewrites the `Host` header needs the page's
public origin in `RC_DASH_ALLOWED_ORIGINS`.

## Watching it

```bash
systemctl status ebpfm ebpfm-node rc-dashboard        # and rc-pull, in a fleet
journalctl -u rc-dashboard -f
sudo /opt/agentic-hpc-dashboard/collector/ebpfm.sh status
```

What the dashboard reads, as its own account, from the folders `paths.env` names: each
feed, its files and hosts, and how far behind its newest record is.

```bash
sudo -u rc-dashboard bash -c 'set -a; . /etc/rc-dashboard/paths.env; set +a
  cd /opt/agentic-hpc-dashboard/dashboard && exec /opt/rc-dashboard/venv/bin/python -m rc_dashboard check-feeds'
```

Each start of the dashboard refills its history from the day-files; the page answers within
a second, and the Feeds tab shows the refill's progress.

## Settings

`dashboard.env` takes any `RC_DASH_*` variable the dashboard's
[README](../dashboard/README.md#configuration) names; restart `rc-dashboard` after an edit.
The installer starts it with the page calling the hosts "servers" (`RC_DASH_NODE_NOUN`),
with no HPC tab (`RC_DASH_SLURM=0`), and dropping the records of the deployment's own two
accounts (`RC_DASH_DROP_USERS`; take `rc-dashboard` out of it to see what the dashboard costs
the server it runs on). The installers' own settings come from their environment:

| Variable | Default | What |
|---|---|---|
| `EBPF_DIR`, `NODE_DIR` | `/var/log/ebpfm`, `/var/log/ebpfm-login` | where the collector writes. An `EBPFM_OUTPUT_DIR` or `EBPFM_NODE_OUTPUT_DIR` already set in `/etc/default/ebpfm` wins, since systemd lets it override the units' own; one named differently in both places is refused |
| `COLLECTOR_GROUP` | `ebpfm` | the group that may read it |
| `SERVICE_USER`, `PULL_USER` | `rc-dashboard`, `rc-pull` | the dashboard's account; the account the copy logs in to the nodes as |
| `ETC`, `STATE`, `OPT` | `/etc/rc-dashboard`, `/var/lib/rc-dashboard`, `/opt/rc-dashboard` | the folders above |
| `MEMORY_MAX` | `8G` | the dashboard's hard memory cap: past it the kernel stops the dashboard, and systemd starts it again |

Raise `MEMORY_MAX` with the fleet: [Memory and disk](../hpc/deploy/README.md#memory-and-disk)
has measurements over 8 busy login nodes, and the settings that trade history for memory.

## Upgrading

```bash
cd /opt/agentic-hpc-dashboard && git pull
sudo deploy/single/install.sh            # or, in a fleet: deploy/fleet/install-dashboard.sh, and install-node.sh on each node
```

The dashboard is rebuilt and restarted. The collector's units are written again, but a
running collector keeps running: its restart flushes every live process as `truncated`, so
choose the moment, `sudo systemctl restart ebpfm ebpfm-node`. The exception is a collector
started before its output had a group: it reads the group only at start, and would make
every new file root's alone, the dashboard's reads included. The installer refuses to leave
it so, and restarts it only when given `--restart`.

## The collector from the RPM

On EL8, the collector also comes as an RPM ([`packaging/rpm/README.md`](../packaging/rpm/README.md)),
which installs `ebpfm.service` itself and writes to `/scratch/ebpfm`, a login node's large
volume. `install-collector.sh` refuses to run beside it: two collectors would write every
record twice. To use the package's collector instead:

```bash
sudo groupadd --system ebpfm
echo EBPFM_GROUP=ebpfm | sudo tee -a /etc/default/ebpfm
sudo chgrp -R ebpfm /scratch/ebpfm && sudo chmod -R g+rX,o-rwx /scratch/ebpfm   # what it wrote before
sudo systemctl restart ebpfm         # it reads EBPFM_GROUP at start; flushes live processes as `truncated`
sudo EBPF_DIR=/scratch/ebpfm deploy/common/install-dashboard.sh single
```

In a fleet, run the first four lines on each node, and make the `rc-pull` account and its key
there by hand, in the group ([The nodes](fleet/README.md#the-nodes)). In `fleet.env`, name
the folder as that key sees it: `FLEET_EBPF_SOURCE=/scratch/ebpfm`, or `ebpfm` under
`rrsync -ro /scratch`. The package has no unit for the node tier.

## Removing everything

```bash
sudo systemctl disable --now rc-dashboard rc-pull 2>/dev/null
sudo rm -f /etc/systemd/system/rc-dashboard.service /etc/systemd/system/rc-pull.service
sudo /opt/agentic-hpc-dashboard/collector/ebpfm.sh uninstall-unit     # each collector host
sudo rm -rf /etc/rc-dashboard /var/lib/rc-dashboard /opt/rc-dashboard
sudo userdel rc-dashboard; sudo userdel -r rc-pull                    # the latter on each node
```

The collector's day-files stay in `/var/log/ebpfm*` until you remove them.

## Tests

```bash
python3 -m unittest discover -s deploy/common/tests -q   # the installers, with stand-ins for systemd and the account tools
python3 -m unittest discover -s deploy/fleet/tests -q    # pull.py, with rsync for real and stand-in nodes
cd dashboard && uv run --frozen python -m unittest tests.test_fleet_copy -q   # the dashboard's tail over pull.py's copy
```
