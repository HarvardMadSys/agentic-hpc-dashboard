# The dashboard on a host of your own, fed live by the fleet stream

This kit runs the dashboard over live data from many collector nodes, such as a cluster's
login nodes, on one Linux host that you have an account on, with no root on any machine. One
ssh connection from that host to each node carries the node's collector files, compressed,
into a private copy ([`hpc/fleet`](../fleet/README.md)). The dashboard reads the copy and
serves the page on `127.0.0.1`, behind a password. Everything is set in one file,
`site.env`, which `deploy/rc-deploy init` writes from four questions (it picks this kit when
you have no root on the nodes or on the dashboard's host). On servers where you have root,
[`deploy/`](../../deploy/README.md) is simpler. `deploy/rc-deploy` runs this kit's scripts
too (`apply`, `add-node`, `password`, `status`, `upgrade`, `rollback`, `backup`, `uninstall`);
each of them below also has the script it runs.

```
 source node                          the dashboard's host                       reader
 the collector writes SOURCE_ROOT     tmux session "rc-dashboard" (rc-tmux.service)
 fleet_stream.py send  ==== ssh ====> window "stream":    fleet_stream.py receive
   (run by a forced-command key)      window "dashboard": rc_dashboard serve  <== ssh tunnel
                                        on 127.0.0.1:DASH_PORT, behind a password  or proxy
 this machine: deploy.sh ships the sender and each release; check.sh checks the host hourly
```

## What runs where

The systemd user unit `rc-tmux.service` starts the tmux session. Each of its windows runs
`keep-running.sh`, which starts its process in a systemd scope of its own
(`rc-fleet-stream.scope`, `rc-dashboard.scope`), starts it again when it ends (after 10 s
for the stream, 30 s for the dashboard), and shows its log. Linger keeps the user manager,
and with it the session, running when no one is logged in.

The scopes cap the dashboard at `DASH_MEMORY_MAX` and the stream at 512 MB, the unit caps
the tmux session at 64 MB, and each Ask question's Codex run gets 1 GB. Past a cap the
kernel stops the process, and its window starts it again. There is no soft cap: with no
swap, `MemoryHigh` stalls a process instead of stopping it, and a stalled dashboard answers
nothing.

## What each host needs

- **This machine:** git, `python3`, [uv](https://docs.astral.sh/uv/) (it pins the
  dashboard's requirements for the release), node and npm (the page's bundle), and ssh
  with a key (`SSH_KEY`) that logs in to the nodes and to the dashboard's host. For Slack
  posts from the hourly check: `jq` and `curl`.
- **The dashboard's host:** Linux with systemd, a user manager that you can keep running
  with `loginctl enable-linger`, and tmux. Python 3.11 or later for the dashboard's venv
  (`DASH_PYTHON`), and `/usr/bin/python3` for the stream. `ss` (without it, the port checks
  read `/proc/net/tcp`), `curl`, and `getfacl` for the checks. Access to PyPI, or a mirror
  pip is set up for: each deploy installs the dashboard's pinned requirements into its venv
  there. Disk and memory: [Memory and disk](#memory-and-disk).
- **Each source node:** the collector ([`collector/`](../../collector), or the RPM), an
  account (`SOURCE_USER`) that can log in with an ssh key, and `/usr/bin/python3` 3.6 or
  later; the sender needs nothing installed. The nodes can share one home, as a cluster's
  login nodes do (`SHARED_HOME=yes`), or have one each.

## Settings

`deploy/rc-deploy init` writes `site.env` in this folder (git ignores it), or copy
[`site.env.example`](site.env.example) there and fill it in; the example explains each key.
Required: `SOURCE_USER`, `SOURCE_NODES`, `DASH_SSH`, `DASH_NODE`, `DASH_ROOT`, `DASH_PORT`,
`WORK_DIR` and `SSH_KEY`. `SOURCE_ROOT` is read only by `hpc/fleet/pull-gz.sh` (step 5); the
sender's folder on the nodes is the `--root` in its key line (step 3).

The same names mean the same setting in every kit ([`deploy/`](../../deploy/README.md#settings)
takes them too): `SITE_NAME`, `SITE_DESCRIPTION`, `SLURM` (the HPC tab: on unless `no`),
`NODE_NOUN` ("login nodes"), `ANONYMIZE`, `MEMORY_MAX` (the dashboard's cap; `DASH_MEMORY_MAX`,
its old name, still works) and `FREE_MIN_GB`. Yes/no takes `1`/`0`, `true`/`false`,
`yes`/`no` or `on`/`off`.

`render.py` turns `site.env` into the unit (`rc-tmux.service.in`), the dashboard's
settings (`dashboard.config.json.in`), and the stream's settings (`stream.json.in`). It
checks every value first, naming the key of a bad number, and `deploy.sh` its own two keys,
so a bad `site.env` stops a deploy before anything is shipped. bash reads the file too
(`deploy.sh`, `ship-sender.sh`, `check.sh` source it), so render.py refuses a line the two
would read differently: a value with blanks and no quotes (`SOURCE_NODES=a b`: bash runs
`b`), or one with `$( )` or backquotes. A key it does not know is named, with the one it is
nearest to (`HISTORY_DAY` is not a setting: did you mean `HISTORY_DAYS`?). `deploy.sh`
renders with `--strict`, which also refuses a value still the example's own
(`your-account`, `*.example.org`).

## First deploy

1. On this machine, fill in `hpc/deploy/site.env`.
2. On the dashboard's host, make the folder and the stream's key, with `DASH_ROOT` as
   `site.env` sets it (nothing sets it on the host):

       DASH_ROOT=<DASH_ROOT>
       umask 077; mkdir -p $DASH_ROOT/etc
       ssh-keygen -t ed25519 -N '' -C rc-fleet-stream -f $DASH_ROOT/etc/rc_fleet_stream

3. On the nodes (once for a shared home, else on each node), add one line to
   `~/.ssh/authorized_keys` of `SOURCE_USER`, with your values in place of the names:

       restrict,from="<the host's address>",command="/usr/bin/python3 <SENDER_DIR>/current/fleet_stream.py send --root <SOURCE_ROOT>" ssh-ed25519 AAAA... rc-fleet-stream

   The key can then run the sender and nothing else, and only from the dashboard's host.
   On the host, `ip route get <a node's address>` shows the address the nodes see. The
   stream accepts each node's host key on its first connection
   (`StrictHostKeyChecking=accept-new`).
4. The password. Neither the dashboard's loop (`check-local.sh --auth-set`) nor
   `rc_dashboard serve` starts without one, so the page is never open. `deploy.sh` makes it
   in step 6 when the host has none; to make it first, on this machine:

       hpc/deploy/password.sh          # or: deploy/rc-deploy password

   It keeps the password in `~/.config/rc-dashboard/password`, with a netrc for curl beside
   it ([Password](../../dashboard/README.md#password)), and puts only its hash on the
   dashboard's host, in `$DASH_ROOT/etc/password.hash` (mode 600), which the dashboard reads
   again whenever it changes. Nothing to paste. A hash in `etc/auth.env`
   (`RC_DASH_PASSWORD_HASH=...`), as earlier versions of this page said, still works, quotes
   or none; `password.sh` comments it out when it puts the hash file in place, and restarts
   the dashboard once to read the file.
5. Optional, to spare the nodes' network: if the nodes' sealed days are already in a
   folder the dashboard's host can read, as `<folder>/<node>/<day>/*.jsonl.gz`, seed the
   copy from it before the first start, from a checkout of this repository on the host:
   `python3 hpc/fleet/seed_mirror.py <folder> $DASH_ROOT/data --free-min-gb <FREE_MIN_GB>`.
   `hpc/fleet/pull-gz.sh <folder>` makes such a folder with rsync, on a machine that logs in
   to the nodes with an ordinary key (the stream's key runs only the sender), with
   `site.env`'s settings in its environment (`set -a; . hpc/deploy/site.env`). The stream
   fetches whatever is missing.
6. On this machine: `hpc/deploy/deploy.sh --first` (or `deploy/rc-deploy apply`;
   `deploy.sh --plan` first checks `site.env` and says what it would ship). It installs the
   sender on the nodes, then the release on the host, makes the venv and the password when
   there is none, starts the session, and turns linger on.
7. Add the hourly check to this machine's crontab ([The hourly check](#the-hourly-check)).

## Opening the page

The page listens on `127.0.0.1:DASH_PORT` on the dashboard's host only. With an account
on that host, an ssh tunnel works. On your own machine:

    ssh -N -L 8000:127.0.0.1:<DASH_PORT> <your account>@<the dashboard's host>

Then open http://localhost:8000 and sign in; any free local port works in place of 8000.
For an address of its own, put a reverse proxy or a tunnel service on the host in front of
`127.0.0.1:DASH_PORT`. One that rewrites the `Host` to `127.0.0.1:DASH_PORT` needs the page's
origin named, as a line `RC_DASH_ALLOWED_ORIGINS=https://<the public name>` in
`etc/auth.env`; one that keeps the public name (cloudflared does) needs nothing more. The
sign-in, the cookie, and scripts: [Password](../../dashboard/README.md#password).

To change the password, `hpc/deploy/password.sh --rotate` (or `deploy/rc-deploy password
--rotate`): a new password here, its hash there, and the dashboard reads it within seconds,
with no restart. Everyone is signed out. To share the page, give colleagues the password
from `~/.config/rc-dashboard/password` over a channel you trust.

## Where things live

| What | Where |
|---|---|
| The copy of the nodes (sealed days as `.gz`, the live day plain) | `DASH_ROOT/data/mirror/<node>/<day>/` |
| `.gz` days being fetched | `DASH_ROOT/data/staging/` |
| Stream status and traffic | `DASH_ROOT/data/state/` (`status.json`, `traffic.jsonl`) |
| Dashboard caches and both logs | `DASH_ROOT/{cache,logs}` |
| Code, bundle, and venv | `DASH_ROOT/{releases/<sha>,current,venv}` (each release stays until you remove it; `releases/.history` says which came before which) |
| Settings, the password's hash (`password.hash`), and the stream key | `DASH_ROOT/etc/` |
| The Trend tab's roll-up table, and the anonymization salt | `DASH_ROOT/cache/{rollup/,anonymize.salt}` ([What to back up](#what-to-back-up)) |
| The unit | `~/.config/systemd/user/rc-tmux.service` on the dashboard's host |
| The sender | `SENDER_DIR/{<sha>,current}` on the nodes (the release before stays for a rollback) |
| The key's forced command | one `restrict,from=...,command=...` line in `~/.ssh/authorized_keys` on the nodes |
| The hourly check and its copy of `site.env` | `WORK_DIR/bin/` on this machine |

With `PRIVATE_TREE=true` (the default), everything is mode 700 or 600. The stream writes
only through a folder handle that it checked at start, and stops if the path is swapped
([When the stream trips](#when-the-stream-trips)).

## Watching and restarting

On the dashboard's host:

    tmux -L rc-dashboard attach         # Ctrl-b n moves between the windows; Ctrl-b d leaves

In a window, Ctrl-C (or `Ctrl-\`) stops its process (SIGTERM, then SIGKILL after 90 s),
which starts again after the pause. A second Ctrl-C during the pause keeps it stopped until
the next hourly check, or until `tmux -L rc-dashboard respawn-pane -t rc-dashboard:dashboard.0`
(or `:stream.0`). Open your own shells outside this session: anything that runs in it shares
the session's 64 MB cap.

To restart both processes, or the session itself:

    systemctl --user restart rc-tmux

Other views of the state:

    deploy/rc-deploy status          # from this machine: every collector's verdict, and every link
    R=<DASH_ROOT>
    systemctl --user status rc-tmux rc-dashboard.scope rc-fleet-stream.scope
    /usr/bin/python3 $R/current/hpc/fleet/fleet_stream.py status --base $R/data
    RC_DASH_ROOT=$R $R/current/hpc/deploy/check-local.sh   # the hourly check itself: starts what is down
    tail $R/logs/rc-fleet-stream.log
    curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:<DASH_PORT>/api/health   # 401: it answers

Through an ssh tunnel, the password's netrc works from this machine:
`curl --netrc-file ~/.config/rc-dashboard/netrc http://localhost:8000/api/health`.

Each start of the dashboard refills its history from the copy; the page answers within a
second. While the refill finds where the history starts in each file, the Feeds tab says
"positioning" and the live panels and view builds wait; then the refill reads backwards,
beside the builds.

## Memory and disk

The history keys in `site.env` trade history for memory. Their defaults
([`site.env.example`](site.env.example)) hold 7 days, up to 5 built views and up to 150,000
sessions per view, with the day cache (`DAY_CACHE`) merging each finished day's hours into
one saved state, so a wide window merges days, not hours. Views are built one at a time, and
a view is built again beside the one it replaces. After a deploy that changes what an hour
holds (the reducers' code, sessions per view, or days of history), the first build of each
view reduces its hours again from the copy.

The copy keeps a sealed day as `.gz`, as each node does (`keep_gz` in `stream.json`):
about 19 times less disk than plain. The dashboard reads a `.gz` through an index of
about 0.5% of its plain size, dropped when the file is gone or no reader has used it for
30 minutes. The stream pauses every node's link while the copy's volume has less free space
than `FREE_MIN_GB`, or than 10% of the volume if that is more. The dashboard also keeps each
sealed day as Parquet in `DASH_ROOT/cache/history`, at about a third of the `.gz`'s disk
([History in Parquet](../../dashboard/README.md#history-in-parquet)).

For scale, one deployment that reads 8 busy login nodes, measured with the defaults and a
32G cap:

- A day of the 8 nodes is 35 to 47 GB of JSON, 2.0 to 3.1 GB as `.gz`, and about
  133,000 sessions. The stream carries 2.1 to 2.5 GB a day over the network.
- A start refilled 7 days (128 files, 364 GB of JSON). Positioning took 252 s. The
  24-hour view was ready after 28 minutes, and the 1h, 6h, and 12h views in the next 6
  minutes. The refill was done after 102 minutes, in one Python process using about one
  core.
- With the refill done and four views held, the process held 12.6 GiB, and its peak was
  15.9 GiB. Page cache from the copy filled the rest of the cap.
- A first start that converts every sealed day to Parquet (up to 8 processes of up to
  0.8 GB each) peaked at 19.1 GiB. The hours are reduced meanwhile by `INGEST_WORKERS`
  processes (two per node unless set, 16 here) of about 0.5 GB each, plus about 1.7 GB in
  the service while it merges an hour, so such a start can come near 28 GiB. On a lower cap,
  set `INGEST_WORKERS` to the node count (8 here: about 4 GB, at 80 to 90% of the speed).

On a small host, give up history before sessions. With 1 core and 8 GB, the same nodes
ran with `HISTORY_DAYS=1`, `MAX_WINDOWS=1`, `MAX_SESSIONS=50000`, `DAY_CACHE=false`, and
`DASH_MEMORY_MAX=7G`.

## Adding a node

1. Add it to `SOURCE_NODES` in `site.env` (`deploy/rc-deploy add-node login03` does this
   step and the last).
2. If the nodes do not share a home (`SHARED_HOME=no`), add the stream key's line to
   `SOURCE_USER`'s `~/.ssh/authorized_keys` on the new node, as in step 3 of
   [First deploy](#first-deploy).
3. `hpc/deploy/deploy.sh`, with no `--restart`. It ships the sender to the new node and a new
   `stream.json` to the host; the stream reads that file again within seconds (or on
   SIGHUP), starts the new node's link, and changes no other link. The dashboard reads the
   copy through a glob (`DASH_ROOT/data/mirror/*`, expanded again at every rescan), so it
   shows the new node with no restart, and nothing refills its history.

A node taken out of `SOURCE_NODES` (`deploy/rc-deploy remove-node login03`) has its link
closed the same way; its copy stays in `DASH_ROOT/data/mirror/<node>`, and the dashboard
shows its days, until you remove that folder (`remove-node --purge-copy`, which also takes
its half-done fetches in `data/staging/<node>` and its verified ledger
`data/state/<node>.verified.json`). The stream forgets a ledger's entries for files the copy
no longer holds, so a node whose copy was removed, by hand too, is copied again whole when
it is added back. Delete its key
line on the node if its home is its own. The last node is not taken out (a deployment streams
from at least one): add its replacement first, or `rc-deploy uninstall` to stop collecting.

## Deploying a new version

On this machine, from a tree with no uncommitted changes to tracked files:

    hpc/deploy/deploy.sh              # install HEAD; the running processes keep their code
    hpc/deploy/deploy.sh --restart    # install HEAD and restart the session
    deploy/rc-deploy upgrade          # git pull, then deploy.sh --restart

A deploy installs the sender on the nodes first, then the release on the dashboard's
host, so the nodes never run an older sender than the receiver expects. Without
`--restart`, the new code and settings are used at each process's next start; a deploy
whose unit names another `ExecStart` than the installed one, or another `DASH_PYTHON`
version than the venv's, restarts the session anyway. A change to `site.env` alone (the
same commit) keeps the release folder as it is and replaces only the settings.

To go back, `hpc/deploy/rollback.sh` (or `deploy/rc-deploy rollback`) moves `current` on the
dashboard's host to the release it ran before (`releases/.history`; or name one:
`rollback.sh <sha>`), installs that release's pinned requirements in the venv, and
restarts the session. The settings stay as the last deploy rendered them, except for a
release from before the copy was one pattern root (`data/mirror/*`): that release would read
the pattern as a folder of that name and find no feed, so its settings name each node's
folder, as the pattern matches them then, until the next `deploy.sh`. The nodes keep their
sender, which is newer than the receiver: the order deploy.sh keeps anyway. For good, check
out the older commit and `deploy.sh --restart`.

## The hourly check

`check.sh` runs `check-local.sh` on the dashboard's host, over ssh, from this machine's
crontab:

    17 * * * * <WORK_DIR>/bin/check.sh >> <WORK_DIR>/check.log 2>&1

The check turns linger back on if it is off, and its own login starts the session after
a reboot. It starts the session if it is down, starts again a window whose process
stopped (a start limit, or Ctrl-C), and makes a missing window. It restarts a dashboard
that listens, has run 20 minutes, and twice gives no answer at all (a 401 without the
password counts as an answer), and one that has not listened after 2 hours. It reports a
node whose link has been down for more than an hour (the stream closes a link whose node
sends nothing, not even its pings, for 5 minutes: `stall_s`), and one connected with
nothing from it for an hour (`STREAM ... stale`). It checks that the listener on the port
is the dashboard's own and, with `PRIVATE_TREE=true`, audits that nothing in `DASH_ROOT` is
readable by anyone else. It checks the password's hash as the dashboard will read it (in
`etc/password.hash`, or `etc/auth.env`, one pair of quotes taken off as systemd does), and
that it parses (`AUTH`). It asks the dashboard for its own verdict, `rc-dashboard status
--json`, from the same settings it serves with: a collector silent, stopped or losing
events, or a link the stream reports down, is a `STATUS warn` or `STATUS crit` finding
(exit 17). It prints `OK`, or one line of findings with an exit code; anything but `OK`
goes to Slack when `SLACK_WEBHOOK_FILE` names a webhook. This machine must be up for it
to run: a laptop that sleeps checks nothing.

## When the stream trips

If `DASH_ROOT` or its `data` folder stops being the folder the stream opened (renamed,
replaced, or opened up to other users), the stream stops the dashboard and then itself,
and writes `DASH_ROOT/TRIPPED`. Neither process starts while that file exists: both
windows stop and say why, and the hourly check reports `TRIPPED`, stops either process
if it still runs, and restarts nothing. To recover, on the dashboard's host:

1. Find out what happened: `ls -la $DASH_ROOT/` (owners, modes, and symlinks), and the
   stream's log in `$DASH_ROOT/logs/`.
2. Make `DASH_ROOT` and its `data` folder this account's own 700 folders again (with
   `PRIVATE_TREE=true`). If anything else was put there, keep it aside and start from
   fresh folders; the stream fetches the copy again.
3. `rm $DASH_ROOT/TRIPPED`, then `systemctl --user restart rc-tmux`.

`PRIVATE_TREE=false` in `site.env` is for a host that only your team can log in to, for
example when an ACL lets a teammate's account read the copy (with `true`, the modes the ACL
changes stop the stream). Modes, owners, and ACLs are then yours: nothing checks or audits
them, a deploy makes only `etc/`, which holds the secrets, 700, and new folders and files
in the copy follow its default ACL. A trip then means only that a folder was put in the
place of `DASH_ROOT` or its `data` folder.

## What to back up

Almost everything in `DASH_ROOT` is rebuilt: the copy from the nodes, the history and the
hour states from the copy. Two files are not:

| What | Where | Lost, it means |
|---|---|---|
| the Trend tab's roll-up table | `DASH_ROOT/cache/rollup/rollup.sqlite` | the daily counts past what the nodes still hold (up to a year) |
| the anonymization salt | `DASH_ROOT/cache/anonymize.salt` | with `ANONYMIZE` on, every alias changes |

Worth keeping too: `DASH_ROOT/etc/` (the password's hash, the stream key the nodes let in)
and `site.env` here. `hpc/deploy/backup.sh` (or `deploy/rc-deploy backup`) runs
`rc-dashboard backup` on the host, from the dashboard's own settings (the roll-up table,
the salt, the session key, the config, the hash), and brings the file here, mode 600;
`rc-dashboard restore --from FILE`, on the host, puts it back.

## Moving to another host

1. Back up first, from this machine: `hpc/deploy/backup.sh --to rc.tar`. It holds the
   Trend tab's table and the salt, which nothing can rebuild.
2. Set `DASH_SSH`, `DASH_NODE`, and `DASH_ROOT` in `site.env` for the new host, and do the
   steps in [First deploy](#first-deploy) there, with its own key line on the nodes.
3. Put the backup back on the new host, with the session stopped:
   `systemctl --user stop rc-tmux`, copy `rc.tar` there, then
   `cd $DASH_ROOT/current/dashboard && RC_DASH_CONFIG=$DASH_ROOT/etc/dashboard.config.json $DASH_ROOT/venv/bin/python -m rc_dashboard restore --from rc.tar`,
   and `systemctl --user start rc-tmux`.
4. Only then, on the old host, remove the software and the data: from this machine, with
   the old host's values in a copy of `site.env`,
   `SITE_ENV=<that copy> hpc/deploy/uninstall.sh --purge`, or by hand as in
   [Removing everything](#removing-everything); and delete the old host's key line on the
   nodes. The sender, the crontab line and `WORK_DIR` serve the new host.

## Removing everything

From this machine: `hpc/deploy/uninstall.sh` (or `deploy/rc-deploy uninstall`; `--plan`
first prints what it would do). It stops the session and removes its unit, the releases
and the venv on the dashboard's host; removes the sender and the stream key's line (the one
that runs `fleet_stream.py send`; every other line stays) on the nodes; and takes the
check's line out of this machine's crontab. It keeps `DASH_ROOT`'s data and settings,
`WORK_DIR` and `~/.config/rc-dashboard`; `--purge` takes them too. Take a backup first.

By hand, on the dashboard's host:

    systemctl --user disable --now rc-tmux
    rm -rf <DASH_ROOT>
    rm ~/.config/systemd/user/rc-tmux.service
    loginctl disable-linger          # unless something else of yours needs it

On the nodes: `rm -rf <SENDER_DIR>`, and delete the `rc-fleet-stream` line from
`~/.ssh/authorized_keys`. On this machine: delete the check's line from the crontab,
`rm -rf <WORK_DIR>`, and `rm -rf ~/.config/rc-dashboard` (the password and its netrc).
Linger is left on by `uninstall.sh` too: turn it off unless something else of yours
needs it.

## Troubleshooting

| Symptom | Look at |
|---|---|
| `AUTH ... does not parse` | the hash in `etc/password.hash` or `etc/auth.env` is cut, or not a hash: `hpc/deploy/password.sh` makes and puts a new one |
| the dashboard's window says it started 4 times and stopped | its log, `DASH_ROOT/logs/rc-dashboard.log`; then `tmux -L rc-dashboard respawn-pane -t rc-dashboard:dashboard.0`, or wait for the hourly check |
| `STREAM <node> down` | the error after it is ssh's: a key line missing or wrong on the node (step 3), or a host key that changed |
| `STATUS crit: <node> silent` | the link is up but the collector on the node writes nothing: the site's collector (`ebpfm status` on the node) |
| `deploy.sh`: a value is still the example's | fill `site.env` in: `--strict` refuses `your-account` and `*.example.org` |
| `TRIPPED` | [When the stream trips](#when-the-stream-trips) |

## Tests

    python3 -m unittest discover -s hpc/deploy/tests

They run each script for real, with systemctl, tmux, ssh, and the other system commands
replaced by stand-ins that log their calls, render every template from
`site.env.example` and load the result with the dashboard's own config loader. They also
check that the kit and the stream name no deployment: given a private list of real names
(`RC_REAL_NAMES`, one per line), a failure names the file and the line in the list, never
the name.
