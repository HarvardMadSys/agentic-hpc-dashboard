# Running the collector and the dashboard on servers of your own

Two ways to run the same collector and dashboard on Linux servers where you have root. Each
server's collector writes its own day-files; the two modes differ only in where the
dashboard runs, and how the files reach it. This page covers what they share.

| Mode | What runs where | Start at |
|---|---|---|
| **single** | One server runs the collector and the dashboard. The dashboard reads the collector's folders where it writes them. | [`single/README.md`](single/README.md) |
| **fleet** | Every node runs the collector and keeps its files on its own disk. One more host runs the dashboard, and copies each node's files to itself with rsync, every minute (`pull.py`). | [`fleet/README.md`](fleet/README.md) |

On a cluster's login nodes where you have no root, use [`hpc/deploy`](../hpc/deploy/README.md)
instead. For a public page over made-up data, use [`cloudflare`](cloudflare/README.md).

## One front door: `rc-deploy`

[`rc-deploy`](rc-deploy) picks the kit for you and runs its scripts; every command below
also has the kit's own script, named beside it, for when you want the parts.

```bash
deploy/rc-deploy init                 # four questions: Slurm? which nodes? root on the nodes? on the dashboard's host?
sudo deploy/rc-deploy plan            # what apply would do: every account, file and unit; nothing done
sudo deploy/rc-deploy apply           # install (single, fleet), or deploy (hpc, as you)
sudo deploy/rc-deploy password        # make the password and put its hash in place
sudo deploy/rc-deploy status          # every collector's verdict, and every node's copy
```

`init` writes one settings file, `deploy/site.env` (git ignores it), and records the kit in
it (`KIT=single`, `fleet`, or `hpc`, whose settings are `hpc/deploy/site.env`). Whether the
page has an HPC tab is asked (`SLURM`), never guessed from root. Edit the file and run
`apply` again to change a setting. `rc-deploy --help` lists the rest: `add-node`,
`remove-node`, `upgrade`, `rollback`, `backup`, `uninstall`.

## What each piece is

| Unit | Runs | As | Made by |
|---|---|---|---|
| `ebpfm.service` | the collector's eBPF tier, `collector/ebpf_trace.py`; starting, stopping or restarting it does the same to the node tier's unit | root | [`common/install-collector.sh`](common/install-collector.sh) (`ebpfm.sh install-unit`) |
| `ebpfm-node.service` | the collector's node tier, a snapshot every 5 minutes; part of `ebpfm.service`, and alone where the eBPF tier cannot run | root | the same |
| `rc-dashboard.service` | `rc_dashboard serve`, on `127.0.0.1:8080` behind a password; its memory cap in `rc-dashboard.service.d/50-memory.conf` | `rc-dashboard` | [`common/install-dashboard.sh`](common/install-dashboard.sh) |
| `rc-dashboard-check.timer` | [`common/check.sh`](common/check.sh) every hour: `rc-dashboard status` ([Watching it](#watching-it)) | `rc-dashboard` | the same |
| `rc-pull.service` (fleet) | [`fleet/pull.py`](fleet/pull.py) `loop`, the copy of every node | `rc-dashboard` | the same, `fleet` |

The collector's output belongs to the group `ebpfm`, mode 750/640: it names real users and
holds their commands. The dashboard reads it as a member of that group (single: the
installer adds `rc-dashboard` to it), or reads the copy, which is its own account's alone
(fleet).

| Where | What |
|---|---|
| `/var/log/ebpfm/<day>/<host>.{exits,snapshot}.jsonl[.gz]` | the eBPF tier's day-files, a sealed day gzipped |
| `/var/log/ebpfm-login/<day>/<host>.jsonl` | the node tier's |
| `/etc/default/ebpfm` | the collector's settings (`EBPFM_GROUP`, `EBPFM_MIN_UID`, and the two output folders, `EBPFM_OUTPUT_DIR` and `EBPFM_NODE_OUTPUT_DIR`, which every later run reads back) |
| `/etc/rc-dashboard/dashboard.env` | the dashboard's settings: yours, kept across installs |
| `/etc/rc-dashboard/paths.env` | where the dashboard reads and keeps things, and its password's hash file: the installer's, rewritten on every run |
| `/etc/rc-dashboard/password.hash` | the password's hash (`common/password.sh`); the dashboard reads it again whenever it changes |
| `/etc/rc-dashboard/install.env` | the installers' own answers (`SERVICE_USER`, `ETC`, `STATE`, `OPT`, `MEMORY_MAX`, the mode, and what they made), read first by every run |
| `/etc/rc-dashboard/slack_webhook` | (yours, optional) a Slack webhook URL the hourly check posts to |
| `/etc/rc-dashboard/fleet.env`, `pull_key` | (fleet) the nodes, and the key the copy logs in to them with |
| `/var/lib/rc-dashboard/` | the dashboard's cache and history store, the Trend tab's roll-up table and the anonymization salt (`cache/`: [What to back up](#what-to-back-up)), and (fleet) the copy, `mirror/` |
| `/opt/rc-dashboard/venv` | the dashboard's Python packages: a release's wheel and the dependencies it pins, or, from a checkout, the dependencies alone (the code runs from the checkout) |
| `/opt/rc-dashboard/kit` | (a release) the three scripts the units run: `check.sh`, `lib.sh` and `pull.py` |

Install from a release: unpack its kit, `agentic-hpc-dashboard-X.Y.Z.tar.gz` from the
[releases page](https://github.com/HarvardMadSys/agentic-hpc-dashboard/releases), where any
account can read it and it can stay, such as `/opt/agentic-hpc-dashboard` (the collector's
units run the collector in it), and run `deploy/rc-deploy` from there. The kit knows its
release (`deploy/VERSION`): the dashboard's installer downloads that release's wheel and the
dependencies it pins, checks each file against the release's checksums, and installs nothing
that differs; nothing is built. A checkout of this repository works the same way, for a
branch or a change of your own: the installer then builds the page and the units run the
checkout's code. The installers need `systemd`; the dashboard's host needs
[uv](https://docs.astral.sh/uv/) (which brings its own Python 3.11 or later where the host
has none) and `curl`, and from a checkout Node.js 20 or later; every host of a fleet needs
`rsync`. Every installer prints what it does as it goes; `--plan` prints what it would do
and changes nothing, and `--help` says what it is for.

## The password

The dashboard does not start until it has a password. On the dashboard's host, as root:

```bash
sudo deploy/common/password.sh          # or: sudo deploy/rc-deploy password
```

It makes the password as you (the account `sudo` was run from): it is kept in your
`~/.config/rc-dashboard/password`, with a netrc for curl beside it, both mode 600. Its
salted hash goes in `/etc/rc-dashboard/password.hash` (`0640`, `root:rc-dashboard`), the
first time `paths.env` names it (`RC_DASH_PASSWORD_HASH_FILE`), and the dashboard starts.
No hash to copy, and no quoting to get right.

- **Rotating:** `sudo deploy/common/password.sh --rotate`. The dashboard reads the new hash
  within seconds, with no restart; everyone signs in again.
- **Sharing:** give colleagues the password from that file, over a channel you trust.
  There is one password for everyone; to take it back, rotate it.
- **A hash in `dashboard.env`** (`RC_DASH_PASSWORD_HASH='scrypt$...'`, in single quotes,
  as earlier versions of this page said) still works, but needs a restart to change.
  `password.sh` comments it out when it installs the hash file.

The cookie, proxies and scripts: [Password](../dashboard/README.md#password).

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

Every hour `rc-dashboard-check.timer` runs [`common/check.sh`](common/check.sh), as the
dashboard's account with its settings: `rc-dashboard status --json`, which judges each
collector from what it wrote (a heartbeat gone quiet, a probe that could not load, events
lost) and, in a fleet, each node's copy (`pull.py`'s state files, named to the dashboard by
`RC_DASH_TRANSPORT_FILES` in `paths.env`), so a failed copy is told apart from a stopped
collector. A level of `warn` or `crit` goes to the journal, and to Slack when
`/etc/rc-dashboard/slack_webhook` holds a webhook URL (mode 640, `root:rc-dashboard`; the
URL is never printed). On `crit` the unit ends failed, so `systemctl --failed` shows it.

```bash
sudo deploy/rc-deploy status                  # the same verdict, for a person (check.sh --print)
journalctl -u rc-dashboard-check              # every hourly finding
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

The same names mean the same setting in every kit, here and in
[`hpc/deploy`](../hpc/deploy/README.md#settings): give them to the installer (or put them in
`deploy/site.env` for `rc-deploy`), and it sets them in `dashboard.env`, printing each
change:

| Name | Sets | Default |
|---|---|---|
| `SLURM` | the HPC tab, and reducing for it (`RC_DASH_SLURM`) | off, said out loud: `SLURM not given` |
| `NODE_NOUN` | what every page calls the hosts (`RC_DASH_NODE_NOUN`) | "servers", or "login nodes" with `SLURM=yes` |
| `SITE_NAME`, `SITE_DESCRIPTION` | the name every page shows, and a few words on what it watches (`RC_DASH_SITE_NAME`, `RC_DASH_SITE_DESC`) | "servers · live" |
| `ANONYMIZE` | an alias for every user name (`RC_DASH_ANONYMIZE`) | off |
| `MEMORY_MAX` (or `DASH_MEMORY_MAX`) | the dashboard's hard memory cap (the drop-in) | half the host's memory, from 2G to 32G |
| `FREE_MIN_GB` | (fleet) nothing copied below this much free space (`FLEET_FREE_MIN_GB`) | 10 |
| `NODES` | (fleet) the nodes of the `fleet.env` the first install makes; after it, `add-node` and `remove-node` (a fleet emptied by `remove-node` stays empty) | |

Yes/no takes `1`/`0`, `true`/`false`, `yes`/`no` or `on`/`off`. `dashboard.env` takes any
other `RC_DASH_*` variable the dashboard's [README](../dashboard/README.md#configuration)
names; restart `rc-dashboard` after an edit. The installer starts it dropping the records
of the deployment's own two accounts (`RC_DASH_DROP_USERS`; take `rc-dashboard` out of it to
see what the dashboard costs the server it runs on).

The installers' own answers come from their environment, else from
`/etc/rc-dashboard/install.env`, where every run saves them (printing any that changed,
`MEMORY_MAX: 16G -> 8G`), else the defaults. So a later run need not be told again:

| Variable | Default | What |
|---|---|---|
| `EBPF_DIR`, `NODE_DIR` | `/var/log/ebpfm`, `/var/log/ebpfm-login` | where the collector writes; saved as `EBPFM_OUTPUT_DIR` and `EBPFM_NODE_OUTPUT_DIR` in `/etc/default/ebpfm`, which wins (systemd lets it override the units' own). A folder named differently there and here is refused, as is one other than an installed unit writes: moving it would leave its days behind; to move it, set it in `/etc/default/ebpfm` |
| `COLLECTOR_GROUP` | `ebpfm` | the group that may read it (saved as `EBPFM_GROUP`) |
| `SERVICE_USER`, `PULL_USER` | `rc-dashboard`, `rc-pull` | the dashboard's account; the account the copy logs in to the nodes as |
| `ETC`, `STATE`, `OPT` | `/etc/rc-dashboard`, `/var/lib/rc-dashboard`, `/opt/rc-dashboard` | the folders above (`install.env` stays at `/etc/rc-dashboard/install.env`, so a run finds it whatever `ETC` is) |
| `MEMORY_MAX` | half the host's memory (to the nearest gigabyte), from 2G to 32G; 8G where `/proc/meminfo` cannot be read | the dashboard's hard memory cap, in `rc-dashboard.service.d/50-memory.conf`: written when there is none, or when `MEMORY_MAX` is given; a plain rerun leaves it, so `systemctl edit rc-dashboard` stays. The first install's choice is saved, and kept; a run says when the cap is more than the host has, which then caps nothing |

Past 32G, raise `MEMORY_MAX` with the fleet: [Memory and disk](../hpc/deploy/README.md#memory-and-disk)
has measurements over 8 busy login nodes, and the settings that trade history for memory.

## Upgrading

From a release's kit, unpack the new release's kit beside the old one, move your settings
across, and apply:

```bash
sudo tar -C /opt -xzf agentic-hpc-dashboard-1.1.0.tar.gz
sudo cp /opt/agentic-hpc-dashboard-1.0.0/deploy/site.env /opt/agentic-hpc-dashboard-1.1.0/deploy/
sudo /opt/agentic-hpc-dashboard-1.1.0/deploy/rc-deploy upgrade      # installs 1.1.0's wheel, rewrites the units
```

`upgrade` from a release's kit runs no git: it installs the kit's release and notes the one
it replaced in `deploy/.rc-deploy-history`. From a checkout, it runs `git pull` first
(`--to v1.1.0` for a tag) and builds what it pulled. By hand, the installer is the same
command as the first install:

```bash
sudo deploy/single/install.sh --no-bootstrap     # or, in a fleet: deploy/fleet/install-dashboard.sh,
                                                 # and install-node.sh --no-bootstrap on each node
```

Every answer the first install was given is read back (install.env, `/etc/default/ebpfm`,
the memory drop-in): an upgrade is the same command, with nothing to repeat.
`--no-bootstrap` skips installing the collector's packages again; run it with bootstrap
after a kernel upgrade, which needs matching headers. The dashboard is installed again and
restarted. The collector's units are written again, but a running collector keeps
running: its restart flushes every live process as `truncated`, so choose the moment,
`sudo systemctl restart ebpfm` (both tiers: `ebpfm-node` is part of it). The exception is a collector started before its
output had a group: it reads the group only at start, and would make every new file
root's alone, the dashboard's reads included. The installer refuses to leave it so, and
restarts it only when given `--restart`. An install made before `install.env` existed has
its answers read from its units the first time (its folders, its memory cap).

**Rolling back:** `sudo deploy/rc-deploy rollback` goes back to what the last `upgrade`
replaced (noted in `deploy/.rc-deploy-history`) and applies it. From a release's kit, that
is the dashboard of the earlier release (or of the one `rollback v1.0.0` names), while the
collector stays this kit's; the old kit's own `rc-deploy apply` takes both back. From a
checkout, it is the earlier commit, or check out any older one and run the installer again.

## What to back up

Almost everything the dashboard keeps is rebuilt from the collector's day-files on its
next start. Two files are not:

| What | Where | Lost, it means |
|---|---|---|
| the Trend tab's roll-up table | `STATE/cache/rollup/rollup.sqlite` | the daily counts past what the day-files still hold (up to a year) |
| the anonymization salt | `STATE/cache/anonymize.salt` | with `ANONYMIZE` on, every alias changes |

Worth keeping too: `/etc/rc-dashboard` (your settings, the password's hash, and in a fleet
the pull key the nodes let in), and the session key (`STATE/cache/session.key`: lost, it
signs everyone out). In a fleet with `FLEET_KEEP_DAYS=0`, the copy (`STATE/mirror`) may be
the only one left of days the nodes have since removed.

```bash
sudo deploy/rc-deploy backup --to /root/rc-dashboard.tar     # deploy/common/backup.sh --to FILE --settings
```

[`common/backup.sh`](common/backup.sh) runs `rc-dashboard backup` as the dashboard's account
(the roll-up table, the salt, the session key, the hash), and with `--settings` keeps
`/etc/rc-dashboard` beside it as `FILE.settings.tar`. `backup.sh --restore FILE` puts it back:
the dashboard stops, its account writes back what the dashboard keeps, root puts the
password's hash in `/etc/rc-dashboard` as `password.sh` does (and names it in `paths.env` on
a host that had no password), and the dashboard starts again. A restore that fails starts
the dashboard again on what it had.

## Moving the dashboard to another host

1. On the old host: `sudo deploy/rc-deploy backup --to rc.tar` (with `--settings`, as
   `rc-deploy` does). In a fleet with `FLEET_KEEP_DAYS=0`, copy `STATE/mirror` too.
2. On the new host: unpack the release's kit (or clone), `deploy/rc-deploy init` (or copy
   `deploy/site.env`), then
   `sudo deploy/rc-deploy apply`. Unpack the settings over the new ones,
   `sudo tar -C /etc -xf rc.tar.settings.tar` (the pull key among them, so the nodes let the
   new host in), run `sudo deploy/rc-deploy apply` again, which reads them, then
   `sudo deploy/common/backup.sh --restore rc.tar`.
3. Fleet: if the address changed, on each node give the same key with the new address:
   `sudo deploy/fleet/install-node.sh --no-bootstrap --key "$(cat pull_key.pub)" --from <the new host's address>`
   (the key's line is replaced; every other line stays).
4. On the old host: `sudo deploy/common/uninstall.sh --purge`.

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
sudo deploy/common/install-dashboard.sh single
```

The dashboard's installer finds `/scratch/ebpfm` in the package's unit. In a fleet, run the
first four lines on each node, and make the `rc-pull` account and its key there by hand, in
the group ([The nodes](fleet/README.md#the-nodes)); that node's line in `fleet.env` names
its folder as the key sees it: `node04=node04.example.org:/scratch/ebpfm:`, or
`node04:ebpfm:` under `rrsync -ro /scratch`. The package has no unit for the node tier.

## Removing everything

```bash
sudo deploy/rc-deploy uninstall              # deploy/common/uninstall.sh; --plan first shows it
sudo deploy/rc-deploy uninstall --purge      # the settings, the data and the accounts too
```

[`common/uninstall.sh`](common/uninstall.sh) stops and removes every unit (the dashboard,
its drop-in, the hourly check, `rc-pull`, and the collector's, through
`ebpfm.sh uninstall-unit`), the venv, and the sysctl drop-in `/etc/sysctl.d/99-ebpfm.conf`
when the installer's bootstrap wrote it (the two kernel settings stay on until the next
boot). `--purge` also takes `/etc/rc-dashboard`, `/var/lib/rc-dashboard`, the accounts, and
the group and `/etc/default/ebpfm` when the installer made them. Run it on each node of a
fleet too. It says what stays: always the collector's day-files (`/var/log/ebpfm*`,
research data, yours to remove), the packages bootstrap installed, the kit or checkout, and your
`~/.config/rc-dashboard`. Take a backup first.

## Troubleshooting

| Symptom | Look at |
|---|---|
| the dashboard does not start | `journalctl -u rc-dashboard`: a missing password (`sudo deploy/common/password.sh`), or a hash that does not parse; after 5 failed starts in 10 minutes, `systemctl reset-failed rc-dashboard` |
| a host reads *silent* on the Feeds tab | in a fleet, `pull.py status` first: a copy that fails (a key, a node down, the disk below `FLEET_FREE_MIN_GB`) looks like a collector that stopped; on the node, `systemctl status ebpfm` |
| `pull.py status` says `STALE` | `rc-pull` is not running: `systemctl status rc-pull`; a wrong `fleet.env` (exit 2) or no node named (exit 4) stops it for good, until fixed and started again |
| `check-feeds` says permission denied | the account is not in the collector's group: run the installer again (it adds it), and restart the collector if it started before the group was set (`--restart`) |
| an upgrade wants to move the collector | it refuses, naming both folders: say the one it writes (`EBPF_DIR=...`), or move it on purpose in `/etc/default/ebpfm` |
| no Slack post | `journalctl -u rc-dashboard-check`; `/etc/rc-dashboard/slack_webhook` must be readable by `rc-dashboard` |

## Tests

```bash
python3 -m unittest discover -s deploy/common/tests -q   # the installers and rc-deploy, with stand-ins for systemd, the account tools and the package managers
python3 -m unittest discover -s deploy/fleet/tests -q    # pull.py, with rsync for real and stand-in nodes
cd dashboard && uv run --frozen python -m unittest tests.test_fleet_copy -q   # the dashboard's tail over pull.py's copy
```
