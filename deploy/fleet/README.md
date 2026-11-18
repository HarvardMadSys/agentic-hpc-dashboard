# The dashboard on a host of your own, fed live by the fleet stream

This kit runs the dashboard over live data from many collector nodes, such as a cluster's
login nodes, on one Linux host that you have an account on. One ssh connection from that
host to each node carries the node's collector files, compressed, into a private copy
([`fleet/fleet_stream.py`](../../fleet/fleet_stream.py)). The dashboard reads the copy and
serves the page on `127.0.0.1`, behind a password. Everything is set in one file,
`site.env`, and the kit needs no root on any machine.

For a public page over synthetic data, use the demo instead:
[`deploy/cloudflare`](../cloudflare/README.md) and [`docker/demo`](../../docker/demo/Dockerfile).
The demo writes its own made-up feed, and its Worker asks for a password of its own. This
kit shows real users and their commands, so the service itself asks for the password, and
everything on the host stays private.

```
 source node                          the dashboard's host                       reader
 the collector writes SOURCE_ROOT     tmux session "rc-dashboard"
 fleet_stream.py send  ==== ssh ====> window "stream":    fleet_stream.py receive
   (run by a forced-command key)      window "dashboard": rc_dashboard serve  <== ssh tunnel
                                        on 127.0.0.1:DASH_PORT, behind a password  or proxy
 this machine: deploy.sh ships the sender and each release; check.sh checks the host hourly
```

## What runs where

| Where | What |
|---|---|
| Each source node | The collector, which writes `SOURCE_ROOT/<day>/<host>.<stream>.jsonl`. The sender, `SENDER_DIR/current/fleet_stream.py`, which a forced-command ssh key starts. |
| The dashboard's host | One folder, `DASH_ROOT`, and one tmux session, `rc-dashboard`, which the systemd user unit `rc-tmux.service` starts. The session's window `stream` runs `fleet_stream.py receive`, which keeps one ssh connection to each node and writes the copy. Its window `dashboard` runs the dashboard on `127.0.0.1:DASH_PORT`. |
| This machine | The repository and `site.env`. `deploy.sh` builds and ships each release, and `check.sh` checks the host every hour. |

Each window runs `keep-running.sh`. It starts its process in a systemd scope of its own
(`rc-fleet-stream.scope`, `rc-dashboard.scope`) and starts it again when it ends: after
10 s for the stream and after 30 s for the dashboard. The window shows the process's log
as it grows. Linger keeps the user manager, and with it the session, running when no one
is logged in.

The scopes cap the dashboard at `DASH_MEMORY_MAX` and the stream at 512 MB. The unit
caps the tmux session at 64 MB, and each Ask question's Codex run gets 1 GB. These are
hard caps: past one, the kernel stops the process, and its window starts it again. There
is no soft cap. With no swap, a soft cap (`MemoryHigh`) stalls a process instead of
stopping it, and a stalled dashboard answers nothing.

## What each host needs

- **This machine:** git, `python3`, [uv](https://docs.astral.sh/uv/) (it pins the
  dashboard's requirements for the release), node and npm (the page's bundle), and ssh
  with a key (`SSH_KEY`) that logs in to the nodes and to the dashboard's host. For Slack
  posts from the hourly check: `jq` and `curl`.
- **The dashboard's host:** Linux with systemd, a user manager that you can keep running
  with `loginctl enable-linger`, and tmux. Python 3.11 or later for the dashboard's venv
  (`DASH_PYTHON`; 3.14 reduces the hours about 10% faster than 3.12, with the same panels:
  `uv python install 3.14`, then `DASH_PYTHON=$(uv python find 3.14)`), and
  `/usr/bin/python3` for the stream. A deploy whose `DASH_PYTHON` is another version than the
  venv's makes the venv again, keeps the one before as `venv.old`, and restarts the session.
  `ss` (without it, the port checks read `/proc/net/tcp`), `curl`, and `getfacl` for
  the checks. Disk and memory: see [Memory and disk](#memory-and-disk).
- **Each source node:** the collector ([`collector/`](../../collector), or the RPM), an
  account (`SOURCE_USER`) that can log in with an ssh key, and `/usr/bin/python3` 3.6 or
  later. The sender uses the standard library only, so nothing is installed. The nodes can
  share one home, as a cluster's login nodes do (`SHARED_HOME=yes`), or have one each.

## Settings

Copy [`site.env.example`](site.env.example) to `site.env` in this folder, and fill it
in. Git ignores `site.env`. The example explains each key; in short:

| Keys | What they set |
|---|---|
| `SOURCE_USER`, `SOURCE_NODES`, `SOURCE_DOMAIN`, `SOURCE_ROOT` | The nodes, the account that logs in to them, and the collector's folder on them. |
| `SENDER_DIR`, `SHARED_HOME` | Where the sender lives on the nodes, and whether their home is shared. |
| `DASH_SSH`, `DASH_NODE`, `DASH_ROOT`, `DASH_PORT`, `DASH_PYTHON`, `PRIVATE_TREE` | The dashboard's host, its folder, its port, the Python that makes its venv, and whether the folder stays private ([When the stream trips](#when-the-stream-trips)). |
| `SITE_NAME`, `SITE_DESCRIPTION`, `RISK_PROFILE`, `ANONYMIZE`, `CODEX` | What the page calls itself, the risk rules, aliases for user names, and Ask. |
| `HISTORY_DAYS`, `MAX_WINDOWS`, `PREWARM_MIN`, `MAX_SESSIONS`, `DAY_CACHE`, `DASH_MEMORY_MAX`, `FREE_MIN_GB` | History, memory, and disk. A key you leave out takes the dashboard's default. |
| `SSH_KEY`, `WORK_DIR`, `NODE_MODULES`, `SLACK_WEBHOOK_FILE` | This machine's key, work folder, bundle build, and the check's Slack posts. |

`render.py` turns `site.env` into the unit (`rc-tmux.service.in`), the dashboard's
settings (`dashboard.config.json.in`), and the stream's settings (`stream.json.in`). It
checks every value first, so a bad `site.env` stops a deploy before anything is shipped.

## First deploy

1. On this machine, fill in `deploy/fleet/site.env`.
2. On the dashboard's host, make the folder and the stream's key:

       umask 077; mkdir -p $DASH_ROOT/etc
       ssh-keygen -t ed25519 -N '' -C rc-fleet-stream -f $DASH_ROOT/etc/rc_fleet_stream

3. On the nodes (once for a shared home, else on each node), add one line to
   `~/.ssh/authorized_keys` of `SOURCE_USER`, with your values in place of the names:

       restrict,from="<the host's address>",command="/usr/bin/python3 <SENDER_DIR>/current/fleet_stream.py send --root <SOURCE_ROOT>" ssh-ed25519 AAAA... rc-fleet-stream

   The key can then run the sender and nothing else, and only from the dashboard's host.
   On the host, `ip route get <a node's address>` shows the address the nodes see. The
   stream accepts each node's host key on its first connection
   (`StrictHostKeyChecking=accept-new`).
4. Make the password. The dashboard's loop does not start without one, so the page is
   never open. On this machine, in the repository:

       cd dashboard && uv run python -m rc_dashboard new-password

   It writes the password to `~/.config/rc-dashboard/password` and a netrc for curl
   beside it (both mode 600, for `localhost` and `127.0.0.1`; `--netrc-host <name>` adds a
   proxy's name), and prints only the hash. On the dashboard's host, put the hash in
   `$DASH_ROOT/etc/auth.env` (mode 600) as one line, `RC_DASH_PASSWORD_HASH=<hash>`.
5. Optional, to spare the nodes' network: if the nodes' sealed days are already in a
   folder the dashboard's host can read, as `<folder>/<node>/<day>/*.jsonl.gz`, seed the
   copy from it before the first start. From a checkout of this repository on the host:
   `python3 fleet/seed_mirror.py <folder> $DASH_ROOT/data --free-min-gb <FREE_MIN_GB>`.
   `fleet/pull-gz.sh <folder>` makes such a folder with rsync, on a machine that logs in to
   the nodes with an ordinary key (the stream's key runs only the sender), with the
   settings from `site.env` in its environment (`set -a; . deploy/fleet/site.env`). The
   stream fetches whatever is missing.
6. On this machine: `deploy/fleet/deploy.sh --first`. It installs the sender on the
   nodes, then the release on the host, makes the venv, starts the session, and turns
   linger on.
7. Add the hourly check to this machine's crontab ([The hourly check](#the-hourly-check)).

## Opening the page

The page listens on `127.0.0.1:DASH_PORT` on the dashboard's host only. With an account
on that host, an ssh tunnel works. On your own machine:

    ssh -N -L 8000:127.0.0.1:<DASH_PORT> <your account>@<the dashboard's host>

Then open http://localhost:8000 and sign in. Any free local port works in place of 8000.
To serve the page at an address of its own, put a reverse proxy or a tunnel service on
the host in front of `127.0.0.1:DASH_PORT`. When the proxy says the request came over
https (`X-Forwarded-Proto`), the session cookie is Secure. See the dashboard's
[Password](../../dashboard/README.md#password) section for the sign-in, the cookie, and
scripts.

To change the password, run step 4 again with `--force`, put the new hash in
`etc/auth.env`, and restart the dashboard
([Watching and restarting](#watching-and-restarting)). Everyone is signed out.

## Where things live

| What | Where |
|---|---|
| The copy of the nodes (sealed days as `.gz`, the live day plain) | `DASH_ROOT/data/mirror/<node>/<day>/` |
| `.gz` days being fetched | `DASH_ROOT/data/staging/` |
| Stream status and traffic | `DASH_ROOT/data/state/` (`status.json`, `traffic.jsonl`) |
| Dashboard caches and both logs | `DASH_ROOT/{cache,logs}` |
| Code, bundle, and venv | `DASH_ROOT/{releases/<sha>,current,venv}` (each release stays until you remove it) |
| Settings, password hash, and the stream key | `DASH_ROOT/etc/` |
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

In a window, Ctrl-C (or `Ctrl-\`) stops its process, which starts again after the pause.
A second Ctrl-C during the pause stops it until the next hourly check, or until
`tmux -L rc-dashboard respawn-pane -t rc-dashboard:dashboard.0` (or `:stream.0`) starts
it. A process is stopped by stopping its scope: SIGTERM, then SIGKILL after 90 s. Open
your own shells outside this session: anything that runs in it shares the session's
64 MB cap.

To restart both processes, or the session itself:

    systemctl --user restart rc-tmux

Other views of the state:

    R=<DASH_ROOT>
    systemctl --user status rc-tmux rc-dashboard.scope rc-fleet-stream.scope
    /usr/bin/python3 $R/current/fleet/fleet_stream.py status --base $R/data
    RC_DASH_ROOT=$R $R/current/deploy/fleet/check-local.sh
    tail $R/logs/rc-fleet-stream.log
    curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:<DASH_PORT>/api/health   # 401: it answers

Through an ssh tunnel, the password's netrc works from this machine:
`curl --netrc-file ~/.config/rc-dashboard/netrc http://localhost:8000/api/health`.

Each start of the dashboard refills its history from the copy. The page answers within a
second. First the refill finds where the history starts in each file, and the Feeds tab
says "positioning". The live panels and the view builds wait for that step; then the
refill reads backwards, beside the builds.

## Memory and disk

The dashboard's defaults hold 7 days of history, so the window picker offers 1h, 6h,
12h, 24h, 3d, and 7d. It holds up to 5 built views (`MAX_WINDOWS`), builds the 1h, 6h,
and 12h views ahead (`PREWARM_MIN`), and keeps up to 150,000 sessions per view
(`MAX_SESSIONS`), the most recently active ones. The day cache (`DAY_CACHE`) merges each
finished day's 24 hour states into one saved state, so a wide window merges days, not
hours.

Every 6 hours a view is built again from the saved hours, beside the held one, until it
replaces it. Views are built one at a time, so a reader who asks for another window
meanwhile waits. After a deploy that changes what an hour holds (the reducers' code,
sessions per view, or days of history), the first build of each view reduces its hours
again from the copy.

The copy keeps a sealed day as `.gz`, as each node does (`keep_gz` in `stream.json`):
about 19 times less disk than plain. The dashboard reads a `.gz` through an index of
about 0.5% of its plain size, and drops the index of a file that is gone or that no
reader used for 30 minutes. At each day's end the stream gzips the day it has just
checked. The stream pauses every node's link while the copy's volume has less free space
than `FREE_MIN_GB`, or than 10% of the volume if that is more.

The dashboard keeps each sealed day as Parquet too, in `DASH_ROOT/cache/history`, and
reads a filtered query of a sealed day -- one user's events, one session's process tree --
from it rather than inflating the `.gz` ([History in
Parquet](../../dashboard/README.md#history-in-parquet)). It takes about a third of the
`.gz`'s disk, and is made in the background after a start and as each day is sealed, by
up to 8 processes (one per CPU the service may use), each holding up to 0.8 GB inside the
`DASH_MEMORY_MAX` cap while it works; they exit when the days are converted. Nothing waits
for it: a day not converted yet is read from its `.gz`. On a deployment over the same 8 nodes
with a 32G cap, the first start after this store came in converted their 7 sealed days
(18.4 GB of `.gz`, 182 M records) into 5.8 GB of Parquet in 12 minutes beside its refill,
the service peaking at 19.1 GiB meanwhile and holding 12.5 GiB after.
The hours are reduced meanwhile too, by `INGEST_WORKERS` processes (two per node unless
set, 16 here) of about 0.5 GB each, plus about 1.7 GB in the service while it merges an
hour: about 9 GB more at once, so a first start can come near 28 GiB of the 32G cap. On a
lower cap, set `INGEST_WORKERS` to the node count (8 here: about 4 GB, and 80 to 90% of
the speed of 16).

For scale, one deployment that reads 8 busy login nodes, measured with the defaults and a
32G cap:

- A day of the 8 nodes is 35 to 47 GB of JSON, 2.0 to 3.1 GB as `.gz`, and about
  133,000 sessions. The stream carries 2.1 to 2.5 GB a day over the network.
- A start refilled 7 days (128 files, 364 GB of JSON). Positioning took 252 s. The
  24-hour view was ready after 28 minutes, and the 1h, 6h, and 12h views in the next 6
  minutes. The refill was done after 102 minutes. The builds and the refill share one
  Python process, which uses about one core.
- With the refill done and four views held, the process held 12.6 GiB, and its peak was
  15.9 GiB. Page cache from the copy filled the rest of the cap.

On a small host, give up history before sessions. With 1 core and 8 GB, the same nodes
ran with `HISTORY_DAYS=1`, `MAX_WINDOWS=1`, `MAX_SESSIONS=50000`, `DAY_CACHE=false`, and
`DASH_MEMORY_MAX=7G`.

## Deploying a new version

On this machine, from a tree with no uncommitted changes to tracked files:

    deploy/fleet/deploy.sh              # install HEAD; the running processes keep their code
    deploy/fleet/deploy.sh --restart    # install HEAD and restart the session

A deploy installs the sender on the nodes first, then the release on the dashboard's
host, so the nodes never run an older sender than the receiver expects. Without
`--restart`, the new code and settings are used at each process's next start. A change to
`site.env` alone (the same commit) keeps the release folder as it is and replaces only
the settings.

## The hourly check

`check.sh` runs `check-local.sh` on the dashboard's host, over ssh, from this machine's
crontab:

    17 * * * * <WORK_DIR>/bin/check.sh >> <WORK_DIR>/check.log 2>&1

The check turns linger back on if it is off, and its own login starts the session after
a reboot. It starts the session if it is down, starts again a window whose process
stopped (a start limit, or Ctrl-C), and makes a missing window. It restarts a dashboard
that listens, has run 20 minutes, and twice gives no answer at all (a 401 without the
password counts as an answer), and one that has not listened after 2 hours. It reports a
node whose link has been down for more than an hour. It checks that the listener on the
port is the dashboard's own and, with `PRIVATE_TREE=true`, audits that nothing in
`DASH_ROOT` is readable by anyone else. It prints `OK`, or one line of findings with an
exit code; anything but `OK` goes to Slack when `SLACK_WEBHOOK_FILE` names a webhook.

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

`PRIVATE_TREE=false` in `site.env` is for a host that only your team can log in to.
There, modes, owners, and ACLs are yours to set. The stream, the start checks, and the
hourly check do not check them, the hourly check does no privacy audit, and a deploy
does not reset them (it makes only `etc/`, which holds the secrets, 700). New folders
and files in the copy follow its default ACL, as entries made there by hand would, so
an account that an ACL lets read the copy can also read each new day, and each day's
`.gz` when it is sealed. A trip then means only that a folder was put in the place of
`DASH_ROOT` or its `data` folder. Use it when, for example, an ACL lets a teammate's
account read the copy: the ACL changes the folders' modes, and with `true` the stream
then stops itself and the dashboard.

## Moving to another host

Set `DASH_SSH`, `DASH_NODE`, and `DASH_ROOT` in `site.env`, and do the steps in
[First deploy](#first-deploy) on the new host, with its own key line on the nodes. Then
remove the old host's deployment ([Removing everything](#removing-everything)) and its
key line.

## Removing everything

On the dashboard's host:

    systemctl --user disable --now rc-tmux
    rm -rf <DASH_ROOT>
    rm ~/.config/systemd/user/rc-tmux.service

On the nodes: `rm -rf <SENDER_DIR>`, and delete the `rc-fleet-stream` line from
`~/.ssh/authorized_keys`. On this machine: delete the check's line from the crontab, and
`rm -rf <WORK_DIR>`.

## Tests

    python3 -m unittest discover -s deploy/fleet/tests
    python3 -m unittest discover -s fleet/tests

The kit's tests run each script for real, with systemctl, tmux, ssh, and the other
system commands replaced by stand-ins that log their calls. They also render every
template from `site.env.example`, load the result with the dashboard's own config loader,
and check that the kit and the stream name no deployment: given a private list of real
names (`RC_REAL_NAMES`, one per line), a failure names the file and the line number in
the list, never the name.
