# Operating the collector

For whoever installs `ebpfm` on a login node and then lives with it: installing it,
checking the node, starting and stopping it, telling whether it is healthy, changing a
setting, upgrading it, removing it, and what to do when something looks wrong. What
it records, and why, is in [README.md](README.md) and [WHAT-IT-COLLECTS.md](WHAT-IT-COLLECTS.md).

Every command below is `ebpfm.sh` in this folder: `ebpfm` once the RPM is installed,
`./collector/ebpfm.sh` from a checkout. `ebpfm help` lists the commands and
`ebpfm <command> --help` explains one; `--help` never runs anything.

## At a glance

| | the eBPF tier | the node tier |
|---|---|---|
| runs as | root | any user (root under its unit) |
| records | every process's exit, with its command line, working directory, I/O, network and how it ended; a census every 60 s; each Slurm submission | node health every 300 s: load, memory, filesystems, NFS, interfaces, D-state processes, per-user cgroups |
| day-files | `EBPFM_OUTPUT_DIR/<date>/<host>.{snapshot,exits}.jsonl` | `EBPFM_NODE_OUTPUT_DIR/<date>/<host>.jsonl` |
| as a unit | `ebpfm.service`, which starts, stops and restarts both tiers | `ebpfm-node.service`, part of `ebpfm.service`; alone where the eBPF tier cannot run |
| log | `journalctl -u ebpfm`; under `start`, `EBPFM_OUTPUT_DIR/ebpfm_<host>.log` | `journalctl -u ebpfm-node`; under `start`, `EBPFM_NODE_OUTPUT_DIR/ebpfm-node_<host>.log` |

As root the folders are `/scratch/ebpfm` and `/scratch/ebpfm-login` where `/scratch` is
writable, else `/var/log/ebpfm` and `/var/log/ebpfm-login`; as a user, `~/.ebpfm/ebpf`
and `~/.ebpfm/login`. `install` writes the choice into `/etc/default/ebpfm`, so it is
made once: a `/scratch` that mounts later does not move it, and every later command,
both units and the dashboard's operator agree on it.

## Installing it

On a node with root, from a checkout or an unpacked bundle:

```bash
sudo ./collector/ebpfm.sh install --dry-run    # the plan; changes nothing (needs no root)
sudo ./collector/ebpfm.sh install
```

`install` does the whole first install, in order:

1. the dependencies (python3-bcc, the running kernel's headers, bpftool, iproute, jq)
   and the two kernel settings the eBPF tier needs, as `bootstrap` does;
2. `EBPFM_OUTPUT_DIR` and `EBPFM_NODE_OUTPUT_DIR` written into `/etc/default/ebpfm`
   (made from the commented defaults if it is missing; every other line is kept);
3. both systemd units, as `install-unit` does;
4. `check`; each tier that passes it is enabled, started and waited for until its first
   record lands;
5. a verdict: `READY: both tiers`, `READY: node tier only` or `NOT READY: N FAIL`, each
   FAIL with its fix.

Where the eBPF tier fails its check, the node tier still starts, keeping every section
(`SNAPSHOT_MODE=full` in the settings file, marked as `install`'s; an `install` that
finds the eBPF tier ready takes it out again). `install` exits 0 only on
`READY: both tiers`. Run it again after fixing a FAIL, or after an upgrade: what is
set stays set.

It refuses two cases, saying why:

- **A bundle's temporary copy.** `./ebpfm-bundle.sh <command>` runs from a copy it deletes
  afterwards, so units written from it would run nothing. Unpack it first:
  `sudo ./ebpfm-bundle.sh --extract /opt/ebpfm && sudo /opt/ebpfm/ebpfm.sh install`.
- **A node with the RPM installed.** The package has its own unit and dependencies:
  `sudo systemctl enable --now ebpfm` ([packaging/rpm](../packaging/rpm/README.md)).

The other ways in:

| way | gives you | guide |
|---|---|---|
| the RPM (Rocky/RHEL 8) | the eBPF tier's unit and every dependency; the node tier has no unit in the package yet | [packaging/rpm/README.md](../packaging/rpm/README.md) |
| the container image | the eBPF tier, in the foreground | [docker/README.md](../docker/README.md#the-collector) |
| `deploy/common/install-collector.sh` | both units, plus the group the dashboard reads the output through | [deploy/README.md](../deploy/README.md) |
| `start`, without root or units | the tier(s) this user may run, until the next reboot | below |

Without root anywhere, `./collector/ebpfm.sh start` runs the node tier as your own
user: half of the data, not a degraded mode.

## Checking the node

```bash
sudo ebpfm check
```

One line per precondition of each tier this user runs: the settings, the node tier's
tools and output folder, then, for the eBPF tier, privilege, BCC, kernel headers and
config, the two sysctls, tracepoints, kprobe symbols, netlink, the cgroup its drain is
charged to, and its output folder. Each FAIL has a `fix:` line under it, and the last
lines are the verdict. It changes nothing; its exit status is the number of FAILs.
WARNs are degradations it names (a block dropped, a section left empty), not blockers.

`check` tests preconditions. Only a start shows which BPF blocks the kernel really
loads: `start` prints them, `ebpfm features` probes them alone (about 40 s the first
time on a kernel), and `meta.features_rejected` in the day-files says why each other
one did not.

## Starting and stopping

With the units (`install`), systemd starts both tiers at boot:

```bash
sudo systemctl status ebpfm ebpfm-node
sudo systemctl restart ebpfm                # both tiers: after a settings change or an upgrade
sudo systemctl restart ebpfm-node           # the node tier alone, which loses no live process
sudo EBPFM_TIER=ebpf ./collector/ebpfm.sh stop   # the eBPF tier alone; the node tier runs on
```

Without them, `start` runs each tier in the background until the next reboot: the eBPF
tier as a transient systemd service in `system.slice` (with `nohup` where systemd is
not running), then the node tier as a loop. It waits for evidence that each tier
collects, the eBPF tier's `meta` record and the node tier's first snapshot, and if a
tier dies it prints the log's last lines. If the eBPF tier does not come up, the node
tier keeps every section.

| command | exit status |
|---|---|
| `start` | 0 running and confirmed, or already running · 1 a tier failed · 2 a refused setting or usage · 4 started, not confirmed within the wait (`--wait SECS`, 60; `--no-wait` skips it) |
| `stop` | 0 stopped, or not running · 1 a tier could not be stopped (it says why) |

`stop` stops the eBPF tier first; as it stops it writes every live process as
`truncated`, so a stop is never free. Where a tier runs as a unit, `start`, `stop` and
`status` act on the unit, and `stop` leaves it enabled.

A tier `start` began leaves a note of its settings beside its pid file
(`EBPFM_PID_DIR`: `/run` as root, `~/.run` as a user), so `status`, `stop` and `tail` from
another shell find it even when that shell's settings differ.

## Is it healthy?

```bash
sudo ebpfm status
sudo ebpfm status --json      # the same, for a script or a monitor
```

For each tier this user runs: whether it runs (its unit or pid, enabled, restarts),
how old its newest record is, today's records and size, the version it runs against
the version installed, its log, the free space where it writes, and for the eBPF tier
`perf_lost` and `handler_errors` from its latest totals (unknown until it has written
one, never 0 by default). A tier whose newest record is older than twice its interval
is **STALE**: 2 × `EBPFM_RESIDENCY_S` (120 s) for the eBPF tier, whose census ticks
even on an idle node; 2 × `EBPFM_NODE_INTERVAL` (600 s) for the node tier.

| exit | verdict | means |
|---|---|---|
| 0 | HEALTHY | every tier runs and wrote within its interval (or is still starting) |
| 1 | DEGRADED | a tier is stale, lost events (`perf_lost`), is short of disk, or one of two tiers is not running |
| 3 | NOT RUNNING | no tier runs |
| 4 | UNKNOWN | a tier runs, but this user cannot read its files (try with sudo), or it has no heartbeat (`EBPFM_RESIDENCY_S=0`) |

A monitor needs nothing else, for example from root's crontab:

```bash
*/10 * * * * ebpfm status >/dev/null || ebpfm status | mail -s "ebpfm on $(hostname -s)" root
```

The dashboard's **Feeds** tab says the same from the other end: each feed, where it was
looked for and how far behind its newest record is.

## Settings

Each setting is an environment variable. Each command resolves it from, in order:
its own environment, then the settings file `/etc/default/ebpfm`, then the default.
The units read the same file (`EnvironmentFile=`), so `ebpfm status` and `tail` see what
they see.

```bash
ebpfm config                       # every setting: value, where it came from, default
ebpfm config NODE_INTERVAL GROUP   # just these
ebpfm config --defaults-file       # the commented defaults file
```

- **The file** is systemd's environment-file format: `KEY=VALUE` per line, `#` comments,
  no `export`, no `$VAR`. The RPM installs it with every setting commented out at its
  default ([packaging/rpm/ebpfm.default](../packaging/rpm/ebpfm.default), generated from
  [lib/ebpfm_settings.py](lib/ebpfm_settings.py)); `install` writes the same where there
  is none. `EBPFM_DEFAULT_FILE` names another file; empty, none.
- **A value a tier cannot use refuses the command** that would run it, with exit status
  2 and where the value is set:
  `ebpfm: EBPFM_NODE_INTERVAL='five': expected seconds (>= 30) (set in /etc/default/ebpfm:12)`.
  Looking (`status`, `stop`, `tail`, `config`, `help`) still works, and `check` lists
  each such value as a FAIL. The node tier's unit stays failed on one rather than
  restarting every 30 s.
- **A name that is no setting** is named, with the nearest one, and ignored:
  `EBPFM_RESIDENCY (set in /etc/default/ebpfm:4) is not a setting, so it is ignored (did you mean EBPFM_RESIDENCY_S?)`.
- **Each tier reads its settings when it starts.** After an edit, restart it:
  `sudo systemctl restart ebpfm`, which restarts both tiers (the eBPF tier writes
  `truncated` records as it stops). Run directly by its unit, the eBPF tier says on stderr (its journal) when it
  falls back to a default for a value it cannot read.

### Every setting

The day-file folders, `EBPFM_TIER`, `EBPFM_PID_DIR`, `EBPFM_HOST` and `SNAPSHOT_MODE`
default by who runs the command and where; `ebpfm config` shows what they come to on
this node. "read by": the eBPF tier, the node tier, both, or `ebpfm.sh` itself.

<!-- settings-table:begin -->
| setting | default | read by | what it does |
|---|---|---|---|
| `EBPFM_TIER` | all as root, node otherwise | command | Which tiers `start`, `stop`, `status`, `check` and `once` act on: node, ebpf or all. |
| `EBPFM_OUTPUT_DIR` | as root /scratch/ebpfm where /scratch is writable, else /var/log/ebpfm; as a user ~/.ebpfm/ebpf | eBPF | The eBPF tier's day-files: `<dir>/<YYYY-MM-DD>/<host>.{snapshot,exits}.jsonl`. An installed unit sets the folder it was installed with; this file overrides it. |
| `EBPFM_NODE_OUTPUT_DIR` | as root /scratch/ebpfm-login where /scratch is writable, else /var/log/ebpfm-login; as a user ~/.ebpfm/login | node | The node tier's day-files: `<dir>/<YYYY-MM-DD>/<host>.jsonl`. |
| `EBPFM_PID_DIR` | /run as root, ~/.run otherwise | command | Where `start` keeps its pid files, and each running tier a note of its settings that `status`, `stop` and `tail` read. |
| `EBPFM_DIR_MODE` | 0750 | both | Mode of the folders both tiers create. 0 or empty: leave the umask's. |
| `EBPFM_FILE_MODE` | 0640 | both | Mode of the day-files both tiers create. 0 or empty: leave the umask's. |
| `EBPFM_GROUP` | (none) | both | A group (name or gid) given every folder and day-file, so its members can read them. The output names real users and what they ran. |
| `EBPFM_UNIT_DIR` | /etc/systemd/system | command | Where `install` and `install-unit` write ebpfm.service and ebpfm-node.service. |
| `EBPFM_DEFAULT_FILE` | /etc/default/ebpfm | command | The settings file ebpfm.sh reads, as the units read it. Empty: read none. From the environment only. |
| `EBPFM_HOST` | the short host name (hostname -s) | both | The host name both tiers write their day-files under, and every command looks for. Set it in a container (-e EBPFM_HOST=$(hostname -s)), whose own name is not the node's. |
| `EBPFM_NODE_INTERVAL` | 300 | node | Seconds between node snapshots. A snapshot is cut short 10 s before the next is due. |
| `EBPFM_NODE_TOP_N` | 25 | node | Rows kept in each per-process and per-user table of a snapshot. |
| `SNAPSHOT_MODE` | node beside the eBPF tier, full without it | node | full: every section. node: leave out the four per-process sections the eBPF tier collects properly (top_io_processes, top_sleeping_procs, sleeping_wchans, socket_talkers). |
| `SNAPSHOT_ARGS_MAXLEN` | 256 | node | Characters of each command line a snapshot keeps, after masking. |
| `SNAPSHOT_CMD_TIMEOUT` | 20 | node | Seconds each tool a snapshot runs (ps, w, mpstat, ...) may take before its section is left empty. |
| `SNAPSHOT_PROC_TREE` | 1 | node | The whole-host process tree (pid, ppid, command, agent tag). 0 leaves it out. |
| `SNAPSHOT_D_STACK_MAX` | 300 | node | Processes in D state whose kernel stack and open files a snapshot reads (as root). |
| `SNAPSHOT_PROBE_NFS_PATH` | (off) | node | A file on NFS that each snapshot reads one block of, to time an NFS read (responsiveness.nfs_read_ms). |
| `EBPFM_REDACT` | keys,prompts | both | keys: credentials become [REDACTED]; prompts: the prompt handed to a coding agent becomes [REDACTED-PROMPT]. 0 (or none, off) writes command lines raw. A value that cannot be read masks both. |
| `EBPFM_DURATION` | 0 | eBPF | Seconds to run, then stop; 0 runs until stopped. `once` defaults to 30. |
| `EBPFM_RESIDENCY_S` | 60 | eBPF | Seconds between residency ticks (what is running, open sockets, totals); 0: no tick. `status` calls the tier stale after two ticks without a record. |
| `EBPFM_POLL_MS` | 100 | eBPF | Milliseconds between drains of the kernel rings. |
| `EBPFM_PERF_PAGES` | 256 | eBPF | Pages per CPU in each kernel ring. More absorbs bigger bursts at the cost of locked memory. |
| `EBPFM_FLUSH_MODE` | poll | eBPF | poll: one write(2) per stream per poll round. record: one per record. |
| `EBPFM_FLUSH` | 1 | eBPF | At stop, write every live tracked process as `truncated`. Not EBPFM_FLUSH_MODE. |
| `EBPFM_COMPRESS` | 1 | eBPF | Gzip a day's files once the day is over (`<file>.jsonl.gz`). 0 leaves them plain. |
| `EBPFM_REORDER_MS` | 20 | eBPF | Milliseconds behind kernel time that events are applied, so they apply in order. |
| `EBPFM_EXIT_HOLD_MS` | 250 | eBPF | Milliseconds an exit record waits for the connections that close after the exit. 0 writes it at once, without them. |
| `EBPFM_GC_GRACE` | 300 | eBPF | Seconds an exited process's state is kept. |
| `EBPFM_ARGS_MAXLEN` | 0 | eBPF | Characters of `args` written; 0: no cap beyond what is read. |
| `EBPFM_ARGV_KMAX` | 4096 | eBPF | Bytes of argv read in the kernel at exec: the real limit when EBPFM_ARGS_MAXLEN is 0. |
| `EBPFM_WRITE_KMAX` | 1024 | eBPF | Bytes captured from each write() of a Slurm submit tool. |
| `EBPFM_SUBMIT_COMMS` | sbatch,salloc,srun | eBPF | The tools whose output a `submit` record captures, comma-separated. |
| `EBPFM_SUBMIT_BUDGET` | 8192 | eBPF | Bytes of a submit tool's output captured before capture stops. |
| `EBPFM_MIN_UID` | 0 | eBPF | Also record processes of uids from this one up with no agent ancestry and no tty (attribution `uid`). 0: off. Turning it on changes what the human class means: keep 0 and filter downstream. |
| `EBPFM_INCLUDE_ROOTS` | 1 | eBPF | Record an agent's own process. 0: only what it runs. |
| `EBPFM_MIN_CPU` | 0 | eBPF | Leave out processes that used less CPU than this many seconds. |
| `EBPFM_MIN_DURATION` | 0 | eBPF | Leave out processes that ran for less than this many seconds. |
| `EBPFM_KEEP_TIMERS` | 0 | eBPF | Record a timer (EBPFM_TIMER_COMMS) that is no agent and used no CPU or I/O. |
| `EBPFM_TIMER_COMMS` | sleep,usleep | eBPF | The commands treated as timers, comma-separated. |
| `EBPFM_CWD` | 1 | eBPF | Read each reported process's working directory. |
| `EBPFM_CWD_ALWAYS` | 0 | eBPF | Read the working directory at every exec instead of inheriting it (to validate). |
| `EBPFM_SANDBOX` | 1 | eBPF | Read each reported process's namespaces and confinement. |
| `EBPFM_SANDBOX_ALWAYS` | 0 | eBPF | Read the sandbox state at every exec (to validate). |
| `EBPFM_RESIDENCY_TOPN` | 5 | eBPF | Heaviest processes listed per agent tree in a residency record. |
| `EBPFM_DSTACK_MAX` | 300 | eBPF | Kernel stacks of processes in D state read per tick. |
| `EBPFM_DSTACK_DEPTH` | 5 | eBPF | Frames kept of each kernel stack. |
| `EBPFM_CONN_ALL` | 0 | eBPF | Also report loopback and private standing sockets, not only external ones. |
| `EBPFM_CONN_UNTRACKED` | 1 | eBPF | Report standing sockets no tracked process owns (uid, no pid). |
| `EBPFM_TCP_ALL` | 0 | eBPF | Also fold the connections of untracked processes. |
| `EBPFM_CONNS_TOPN` | 5 | eBPF | Peers and providers kept in a `conns` block. |
| `EBPFM_NETPEER_MAX` | 65536 | eBPF | Entries in the kernel endpoint table (about 3 MB at the default). |
| `EBPFM_NET_ENDPOINTS_MAX` | 256 | eBPF | Endpoints listed per record; the rest are counted in `overflow`. |
| `EBPFM_NETIO` | 1 | eBPF | The per-tick `netio` series. 0: none (`net_endpoints` stays). |
| `EBPFM_ARGV_HOSTS` | 1 | eBPF | Name endpoints from host names on the command line, resolved with the host's resolver. 0: no lookups. |
| `EBPFM_FEATURES` | auto | eBPF | auto: probe which BPF blocks this kernel loads. A comma list pins them (`ebpfm features` lists them). |
| `EBPFM_FEATURE_CACHE` | 1 | eBPF | Keep each kernel's probe result, so a restart does not probe again. |
| `EBPFM_PROBE_VERBOSE` | 0 | eBPF | Print the compiler's message for each block it rejects. |
| `EBPFM_BENCH_REPS` | 3 | command | Timed repetitions per condition in `overhead`. |
<!-- settings-table:end -->

## The host name

Both tiers name their day-files with the short host name (`hostname -s`), and every
command looks for that name; `EBPFM_HOST` overrides it everywhere at once, as a
container needs. Before this version the eBPF tier's unit passed the full host name
(`%H`): on a node whose kernel hostname is fully qualified, that tier's files carried
the long name and the node tier's the short one, and the dashboard showed two hosts.
From a reinstall of the unit (`install`, `install-unit`, or the RPM's upgrade) both use
the short name, so such a node's eBPF history splits at that point: earlier days under
the long name, later ones under the short. `status` and `tail` still find a day the old
unit wrote.

One collector writes an output folder per machine: a second one on the same machine (a
unit and a `start`, or two host names) refuses to start. Hosts that share a folder over
NFS do not get in each other's way.

## Upgrading

Find what runs: `ebpfm version` is the installed collector (the RPM's or bundle's
stamp, `git describe` in a checkout, else a hash of the code), the eBPF tier records
its own as `collector_version` in each `meta` record, and `status` warns when a running
tier's differs from the installed one.

| installed from | upgrade with | then |
|---|---|---|
| a checkout | `git pull`, then `sudo ./collector/ebpfm.sh install` | `sudo systemctl restart ebpfm`, both tiers, when you choose: the eBPF tier's stop writes `truncated` records |
| the RPM | `sudo dnf upgrade ebpfm-collector` | the package restarts `ebpfm.service` itself, with the same `truncated` records; `/etc/default/ebpfm` is kept |
| a bundle | `sudo ./ebpfm-bundle.sh --extract /opt/ebpfm`, then `sudo /opt/ebpfm/ebpfm.sh install` | the restart, as for a checkout |
| the container | pull the new tag and recreate the container | `docker stop` writes the `truncated` records |

An upgrade that rewrites the eBPF tier's unit changes its host name where it was fully
qualified ([The host name](#the-host-name)).

## Removing it

```bash
sudo ebpfm stop                                # tiers started with `start`
sudo ebpfm uninstall-unit --dry-run            # what it would stop and remove
sudo ebpfm uninstall-unit                      # stops, disables and removes both units
sudo rm -f /etc/default/ebpfm /etc/sysctl.d/99-ebpfm.conf
```

`bootstrap` (and so `install`) may also have added the `universe` component to Ubuntu's
apt sources, keeping each original as `<file>.ebpfm.bak`; restore those if you want it
gone. The day-files stay in the output folders until you delete them; they name real
users. With the RPM, `sudo dnf remove ebpfm-collector` stops and disables the unit and
removes the package's files, keeping a changed `/etc/default/ebpfm` as `.rpmsave`.

## Troubleshooting

| symptom | likely cause | what to do |
|---|---|---|
| `check`: `FAIL python3-bcc not importable` | BCC is not installed | `sudo ebpfm bootstrap` |
| `check`: `FAIL sched/sched_process_* MISSING (core)` | tracefs is not mounted (containers, minimal images) | `mount -t tracefs nodev /sys/kernel/tracing`, as its `fix:` line says |
| `check`: `FAIL kernel.task_delayacct 0` | the sysctl is off: D-state and delay fields would read 0 | `sudo ebpfm bootstrap` |
| `check`: `FAIL node_snapshot syntax` as a user | before this version: `py_compile` could not write `__pycache__` beside a root-owned install | upgrade; the check no longer writes anything |
| `start` exits 2 | a setting a tier cannot use | the message names it and where it is set; `ebpfm config` |
| `start`: the eBPF tier `FAILED: it exited` | the log's last lines, printed under it, say why: no BCC, a pinned block that will not load, another collector writing the folder | fix that; `ebpfm check` |
| `start`: `not yet after 60s` (exit 4) | a first probe on a new kernel compiles for about 40 s, or the node tier's first snapshot is stuck on a hung mount | `ebpfm status` a minute later; `ebpfm stop` to give up |
| `status`: `STALE` | the tier runs but writes nothing: a hung filesystem, a full disk, a stuck process | its log (`status` prints where); `df` on the output folder; `sudo systemctl restart ebpfm-node` |
| `status`: `perf_lost` above 0 | the drain fell behind a burst and events were dropped | raise `EBPFM_PERF_PAGES` (a power of two) and restart the eBPF tier |
| `status`: `UNKNOWN` as a user | the output folders are 0750 root's | `sudo ebpfm status`, or `EBPFM_GROUP` to share them with a group |
| `status`: `runs X, but Y is installed` | upgraded while running | restart the tier when you choose |
| `status` looks in the wrong folder | a tier started with settings this shell does not have, from before tiers left a note | `EBPFM_OUTPUT_DIR=... ebpfm status`, or stop and start it again |
| the dashboard shows a node twice | the eBPF unit from before this version wrote the full host name | reinstall the unit ([The host name](#the-host-name)) |
| `install`: `NOT READY` | the FAIL lines under it, each with its fix | fix them, then `install` again |
| `error: another collector (pid N) on this machine is writing ...` | a unit and a `start` (or a second unit) for one folder | `ebpfm status` shows which; `ebpfm stop` the one you do not want |
