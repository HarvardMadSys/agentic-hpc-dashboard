# A fleet: collectors on many servers, the dashboard on one more

Every node runs the collector and keeps its day-files on its own disk. One more host, the
dashboard's, copies each node's files to itself with rsync over ssh, each node every 60 s on
its own, so a slow node delays no other, and the dashboard reads the copy as it would a
node's own folder. This page is the install and the copy; [`../README.md`](../README.md) has
the rest.

```
node (each)                                     the dashboard's host
 ebpfm, ebpfm-node -> /var/log/ebpfm*            rc-pull: pull.py loop
 rc-pull: reads them, by key only  <== rsync ==   -> /var/lib/rc-dashboard/mirror/{ebpf,node}/<node>/<day>/
                                                 rc-dashboard: reads the copy, serves 127.0.0.1:8080
```

## Install

Clone the repository on every host where any account can read it, such as
`/opt/agentic-hpc-dashboard`. The dashboard's host first, since it makes the key the nodes
let in:

1. **The dashboard's host.** `deploy/rc-deploy init` (it picks this kit when you have root
   on the nodes and on this host, and asks whether they run Slurm), then
   `sudo deploy/rc-deploy apply`. That is `deploy/fleet/install-dashboard.sh`, which makes
   the dashboard ([What each piece is](../README.md#what-each-piece-is)), `rc-pull`,
   `/etc/rc-dashboard/fleet.env` and the key `/etc/rc-dashboard/pull_key`, and prints the
   command to run on each node, its public key filled in. `rc-pull` waits until
   `fleet.env` names your nodes.
2. **Each node.** `sudo deploy/fleet/install-node.sh --key '<that public key>' --from <the dashboard host's address>`
   (on the dashboard's host, `ip route get <a node's address>` shows, as `src`, the address
   the nodes see).
   It runs the collector ([`common/install-collector.sh`](../common/install-collector.sh):
   both tiers as units, the output readable by the group `ebpfm`), makes the account
   `rc-pull` in that group, and lets the key in for read-only rsync and nothing else
   ([The nodes](#the-nodes)). It prints the node's line for `fleet.env`, its folders in it
   as this key reads them:

       FLEET_HOSTS+=" node03=node03.example.org:ebpfm:ebpfm-login"

3. **The dashboard's host again.** `sudo deploy/rc-deploy add-node 'node03=node03.example.org:ebpfm:ebpfm-login'`
   for each node (or paste the lines into `/etc/rc-dashboard/fleet.env` and run
   `sudo deploy/fleet/install-dashboard.sh --no-build`). The first node starts `rc-pull`;
   each next one reloads it.
4. Make the password, which starts the dashboard: `sudo deploy/rc-deploy password`
   ([The password](../README.md#the-password)).

Settings for the dashboard (`SLURM`, `SITE_NAME`, `MEMORY_MAX`, `FREE_MIN_GB`, the first
`NODES`): [Settings](../README.md#settings). `--plan` on any installer prints what it would
do; `--help` says what it is for.

```bash
sudo -u rc-dashboard /opt/rc-dashboard/venv/bin/python /opt/agentic-hpc-dashboard/deploy/fleet/pull.py --config /etc/rc-dashboard/fleet.env status
journalctl -u rc-pull -f
```

`status` prints each node's last pass: `ok`, and how long ago (`3h 12m ago`); `FAILED`
and why; `STALE` when no pass has gone well for three intervals (`rc-pull` is not running,
or every pass hangs: `--stale 30m` sets another threshold); `never`. It exits 1 unless
every node is `ok`. The hourly check ([Watching it](../README.md#watching-it)) reads the
same state, and the dashboard shows it beside each node's verdict.

## Adding or removing a node

```bash
sudo deploy/rc-deploy add-node 'node09=10.0.0.19:ebpfm:ebpfm-login'   # the line install-node.sh printed
sudo deploy/rc-deploy remove-node node09                              # its copy stays; --purge-copy removes it
```

`add-node` checks the line with `pull.py` first (a line it would refuse leaves `fleet.env`
as it was), adds `FLEET_HOSTS+=" ..."` to `fleet.env`, and reloads `rc-pull`
(`systemctl reload rc-pull`: SIGHUP). The new node is copied within a minute, and the
dashboard, which reads the copy through a glob (`RC_DASH_EBPF_ROOTS=.../mirror/ebpf/*`,
expanded again at every rescan), shows it with no restart, so nothing refills its history.
By hand: edit `FLEET_HOSTS`, then `sudo systemctl reload rc-pull`. A node taken out keeps
its copy, and the dashboard its days, until you remove `mirror/{ebpf,node}/<node>`; on the
node itself, `sudo deploy/common/uninstall.sh --purge` takes the collector and the key away.
`remove-node` also removes the node's last pull state (`mirror/.state/<node>.json`), which
would otherwise age into a copy failure the hourly check reports for good (by hand, remove
it too). Taking out the last node stops every pull; `rc-pull` stays up, idle, and copies
again from the reload that names a node.

## The nodes

The dashboard's host logs in to each node as `rc-pull` with `pull_key`. That account can
read the collector's output, as a member of its group, and nothing more than any account
can. `install-node.sh --key` gives the key one line in `~rc-pull/.ssh/authorized_keys`:

```
restrict,from="<address>",command="/usr/bin/rrsync -ro /var/log" ssh-ed25519 AAAA... rc-pull@<dash>
```

`restrict` takes away the terminal and every forwarding, `from` lets the key in only from
the dashboard's address, and `rrsync` (`man rrsync`; recent rsync packages install it) lets
it run read-only rsync of `/var/log` and nothing else. rrsync sees the folders under
`/var/log` as `ebpfm` and `ebpfm-login`, so the node's line names them so.

Where a node has no `rrsync` command, `install-node.sh` leaves out the command, says so, and
prints the full paths in the node's line instead; the key can then run any command as
`rc-pull`. Since each node's line carries its own folders, a fleet whose nodes differ (an
rrsync key on some, full paths or the RPM's `/scratch/ebpfm` on others) needs nothing more:
`FLEET_EBPF_SOURCE` and `FLEET_NODE_SOURCE` are only the folders of a node whose line names
none. A node with no node-tier folder is copied without one (its line ends in `:`, or the
folder is missing); a node with no eBPF-tier folder fails, and says which folder it lacks.
Run again without `--key` (an upgrade), `install-node.sh` keeps the key it installed, and
prints the line again.

The nodes' host keys are accepted on first contact and kept in
`/var/lib/rc-dashboard/mirror/.state/known_hosts`; a node whose key then changes is refused
until you remove its line there.

## How the copy reads as the node

The dashboard follows a day-file by its inode and an offset into it, so the copy must grow
in place as the node's file does: plain rsync renames a new file over the old one on every
pass, and the dashboard would read the whole day again each minute, every record counted
again. [`pull.py`](pull.py) appends to a live day-file's copy (`rsync --append`), copies a
sealed day's `.gz` once gzip is done with it, then drops the plain copy once the `.gz`
inflates whole; its docstring gives each rule. `dashboard/tests/test_fleet_copy.py` runs the
dashboard's own tail over such a copy: every record is read once.

Only `<day>/<name>.jsonl` and `<day>/<name>.jsonl.gz` are copied, of the `FLEET_DAYS` newest
days, and no link is followed. The copy is private to `rc-dashboard` (700/600), and nothing
is copied while its volume has less than `FLEET_FREE_MIN_GB` free. A node's copy lags the
node by up to `FLEET_INTERVAL_S` (60 s) plus a pass; the dashboard calls a feed stale after
5 minutes without a new record (`RC_DASH_EBPF_MAX_AGE_S`).

Each node's last pass is in `mirror/.state/<node>.json`, which the dashboard reads
(`RC_DASH_TRANSPORT_FILES`, in `paths.env`): `host`, `target`, `kind` (`rsync`), `interval_s`,
`ok`, `ok_epoch` (the last good pass), `attempt_epoch`, `error`, `tiers` (what the pass
moved), and `file_hosts` (the host names in the node's day-files, which may differ from its
name in `FLEET_HOSTS`).

## pull.py by hand

```
pull.py [--config FILE] once                       one pass over every node; exit 1 if one failed
pull.py [--config FILE] loop                       what rc-pull runs: each node every FLEET_INTERVAL_S; SIGHUP reloads FILE
pull.py [--config FILE] status [--stale D] [--json]  each node's last pass; exit 1 unless every node is ok
pull.py [--config FILE] roots [--per-node]         RC_DASH_EBPF_ROOTS, RC_DASH_NODE_ROOTS (a glob) and RC_DASH_TRANSPORT_FILES
pull.py [--config FILE] check                      the nodes FILE names; exit 4 when none, 2 when FILE is wrong
```

Run it as `rc-dashboard` (`sudo -u rc-dashboard ...`): the copy is that account's, and a
second `pull.py` on the same copy refuses to start while `rc-pull` holds it. Every setting is
in [`fleet.env.example`](fleet.env.example); a node is `NAME[=TARGET][:EBPF_SOURCE[:NODE_SOURCE]]`,
and `FLEET_HOSTS+=" ..."` adds one. `FREE_MIN_GB`, the name every kit gives it, works for
`FLEET_FREE_MIN_GB`; a `FLEET_` key it does not know is an error that names the nearest one.

## Running it

| | |
|---|---|
| Upgrade | the dashboard's host: `sudo deploy/rc-deploy upgrade`; each node: `git pull` and `sudo deploy/fleet/install-node.sh --no-bootstrap` ([Upgrading](../README.md#upgrading)) |
| Back up | `sudo deploy/rc-deploy backup` keeps `/etc/rc-dashboard`, the pull key among it; with `FLEET_KEEP_DAYS=0` keep `mirror/` too ([What to back up](../README.md#what-to-back-up)) |
| Move | [Moving the dashboard to another host](../README.md#moving-the-dashboard-to-another-host) |
| Remove | `sudo deploy/rc-deploy uninstall [--purge]` on the dashboard's host, `sudo deploy/common/uninstall.sh [--purge]` on each node |
| Troubleshoot | [Troubleshooting](../README.md#troubleshooting): a node *silent* on the Feeds tab is `pull.py status` first |
