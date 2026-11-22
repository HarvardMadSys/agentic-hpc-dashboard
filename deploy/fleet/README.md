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

1. **The dashboard's host.** `sudo deploy/fleet/install-dashboard.sh`. It makes the
   dashboard ([What each piece is](../README.md#what-each-piece-is)), `rc-pull`,
   `/etc/rc-dashboard/fleet.env` and the key `/etc/rc-dashboard/pull_key`, and prints the
   key's public half. `rc-pull` waits until `fleet.env` names your nodes.
2. **Each node.** `sudo deploy/fleet/install-node.sh --key '<that public key>' --from <the dashboard host's address>`.
   It runs the collector ([`common/install-collector.sh`](../common/install-collector.sh):
   both tiers as units, the output readable by the group `ebpfm`), makes the account
   `rc-pull` in that group, and lets the key in for read-only rsync and nothing else
   ([The nodes](#the-nodes)). It prints the two lines `fleet.env` needs for that key.
3. **The dashboard's host again.** Name the nodes in `/etc/rc-dashboard/fleet.env`
   (`FLEET_HOSTS`), with the two lines from step 2, and run `sudo deploy/fleet/install-dashboard.sh --no-build`.
   It writes where the dashboard reads (`paths.env`) and starts `rc-pull`.
4. Make the password and start the dashboard ([The password](../README.md#the-password)).

```bash
sudo -u rc-dashboard /opt/rc-dashboard/venv/bin/python /opt/agentic-hpc-dashboard/deploy/fleet/pull.py --config /etc/rc-dashboard/fleet.env status
journalctl -u rc-pull -f
```

`status` prints each node's last pass: `ok`, and how long ago, or `FAILED` and why.

**Adding or removing a node:** edit `FLEET_HOSTS`, then run step 3's command again. It
rewrites `paths.env` and restarts `rc-pull` and the dashboard, which reads its node list at
start. A node taken out keeps its copy until you remove `mirror/{ebpf,node}/<node>`.

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
`/var/log` as `ebpfm` and `ebpfm-login`, so with that key `fleet.env` names them so:

```
FLEET_EBPF_SOURCE=ebpfm
FLEET_NODE_SOURCE=ebpfm-login
```

Where a node has no `rrsync` command, `install-node.sh` leaves out the command, says so, and
prints the full paths instead; the key can then run any command as `rc-pull`. A node with no
node-tier folder is copied without one; a node with no eBPF-tier folder fails, and says
which folder it lacks.

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

## pull.py by hand

```
pull.py [--config FILE] once     one pass over every node; exit 1 if one failed
pull.py [--config FILE] loop     what rc-pull runs: each node every FLEET_INTERVAL_S
pull.py [--config FILE] status   each node's last pass
pull.py [--config FILE] roots    the RC_DASH_EBPF_ROOTS and RC_DASH_NODE_ROOTS for the copy
```

Run it as `rc-dashboard` (`sudo -u rc-dashboard ...`): the copy is that account's, and a
second `pull.py` on the same copy refuses to start while `rc-pull` holds it. Every setting is
in [`fleet.env.example`](fleet.env.example).
