# What the collector collects

> We are overcollecting while we are still measuring and analysing; once the analysis is
> done, the collector will be configured to collect only the necessary data. We also capture
> some data twice, to verify the accuracy of our prior collection efforts, which run as
> unprivileged processes on the login nodes with 1 CPU core.

Every signal the collector records, in plain words, by where it comes from and when. Each
record kind and field is in [README.md](README.md#what-it-collects); each of the node
snapshot's keys, and which tier reads it, in [COVERAGE.md](COVERAGE.md).

The collector has two tiers ([README.md](README.md#two-tiers-chosen-by-privilege)):

- the **eBPF tier** runs as root and records events from the kernel as they happen;
- the **node tier** runs as any user and takes a snapshot of the node every 5 minutes
  (`EBPFM_NODE_INTERVAL`, 300 s) from `/proc`, `/sys` and standard tools (`ps`, `free`,
  `uptime`, `w`, `vmstat`, `mpstat`, `iostat`, `sar`, `nfsiostat`, `ss`).

Command lines are masked before they are written (`EBPFM_REDACT`, on by default):
credentials become `[REDACTED]`, and the prompts handed to an agent on its command line
`[REDACTED-PROMPT]`.

## eBPF tier (root)

### Each process, when it exits

- **Process lifecycle (fork, exec, exit)**: pid, parent pid, uid, executable name (e.g., python3, bash, rsync), timestamp
- **Command line (at exec)**: argv, masked as above, and whether it was cut (the kernel reads its first 4096 bytes, `EBPFM_ARGV_KMAX`); the working directory
- **Process context**: controlling terminal type (e.g., pts/0, tty1, or headless), cgroup, process group id, session id
- **Who it belongs to**: whether it is a coding agent or runs under one, and which (agent type, the agent's pid, its depth in the agent's process tree, its session), else a person at a terminal or an IDE
- **Exit status**: exit code, terminating signal, whether a core was dumped
- **CPU time**: user and system time for the whole process, CPU time of reaped children, time on CPU, time waiting in the run queue
- **Memory**: peak resident set size, thread count, minor and major page faults, voluntary and involuntary context switches
- **I/O**: bytes read and written at the storage layer, bytes read and written through syscalls
- **Delay accounting**: time blocked on disk I/O, on swap-in, on free pages
- **Uninterruptible sleep (D state)**: total time, the longest single stretch, number of times
- **Network bytes (TCP/UDP, IPv4/IPv6)**: bytes sent, bytes received, number of send/receive calls; and per remote endpoint: address, port, protocol, direction (inbound/outbound), bytes sent, bytes received, call count, and a host name only where the process's own command line names it
- **Its connections**: how many it opened and accepted, how many were established or failed and how many external, their bytes sent and received, their summed lifetime, the median and longest connect latency, and the top remote peers by bytes (address, port, bytes)
- **Sandboxing**: namespace identity (user/mount/pid/net, from `/proc/<pid>/ns`) and, from `/proc/<pid>/status`, seccomp mode, no-new-privs, capability bounding set and pid-namespace depth: whether it ran in a sandbox of its own

### Every 60 seconds (`EBPFM_RESIDENCY_S`)

- **Running agent trees**: one record per live agent tree, and the kernel stack frames its blocked processes wait in (top 5)
- **Node-wide totals**: live processes, threads and memory by class and by agent type, the fork rate, the node's top 10 blocking kernel frames, and the collector's own counters (events seen and lost, handler errors)
- **Open sockets (TCP/UDP, IPv4/IPv6)**, external ones by default, read over netlink: address family (inet or inet6), protocol, state, local and remote address and port, owning uid, RTT, retransmits, bytes sent and received, segments in/out, time since last send/receive, receive and send queue depth
- **Network bytes per endpoint**, since the previous tick, for each process that moved any

### Each Slurm submission (sbatch/salloc/srun)

- the last 300 characters of the tool's stdout/stderr, credentials masked (none for an interactive `srun --pty`), and the job id parsed from it (the collector reads up to 8 KB, `EBPFM_SUBMIT_BUDGET`); the working directory; and the request (partition, GPUs, array) where the command line states it

### Once a run, at start and stop

- **At start**: the collector's own version, kernel, BCC version, which BPF blocks loaded and why any did not, the scheduler-stats and delay-accounting sysctls, boot id, time zone, ephemeral port range
- **At stop**: records written, events lost, and one record for each process still running, with what it had accumulated

## Node tier (any user, every 5 minutes)

**Processes**
- **Census**: count of running, blocked, sleeping, zombie, stopped processes
- **Blocked processes (D state)**: pid, parent, state, user, wait channel, top kernel stack frames, open file-descriptor count, command
- **Top processes by CPU and by memory**: pid, parent, user, CPU %, memory %, RSS, state, start, CPU time, command
- **Per user**: CPU %, RSS and process count
- **Whole-host process tree**: pid, parent, command, agent tag
- Command lines are masked as above and cut at 256 characters (`SNAPSHOT_ARGS_MAXLEN`)
- **Only on a node with no eBPF tier** (with both running, these read `null`: the eBPF tier records them properly, for every user): the top processes by I/O, cancelled writes included; sleeping processes by wait channel and current syscall; and the top processes by established connection count

**Node-wide kernel counters**
- **Scheduler**: boot time, cumulative forks, context switches, running and blocked processes
- **Memory**: dirty, writeback, NFS-unstable, slab (reclaimable/unreclaimable), committed address space
- **TCP transport**: segments out, retransmitted segments, input errors, failed connection attempts, listen drops/overflows, timeouts
- **NFS**: RPC calls, retransmissions, auth refreshes, accounting per mount (bad XIDs, request backlog, read/write op counts, retransmits, queue/RTT/execution time)
- **Mounts**: autofs trigger count, NFS mount count, distinct NFS servers
- **System**: allocated/free/max file descriptors

**Per-user cgroup**
- CPU usage, throttling periods and throttled time, CPU quota and period, current memory, current and maximum pids

**Node health (using command line tools)**
- **Load**: 1/5/15-minute load averages, logged-in user count
- **Responsiveness**: how long it takes to start a process, to stat a local file, and to look up an account
- **Sessions**: user, tty, origin host, login time, idle time, session CPU
- **Memory**: total/used/free/shared/cache/available; swap total/used/free
- **CPU per core**: user, system, iowait, irq, softirq, idle %
- **Virtual memory**: runnable/blocked processes, swap in/out, block in/out, interrupts/s, context switches/s
- **Disk I/O per device**: reads and writes per second, kB/s, read and write await, utilization
- **Network per interface**: packets/s and kB/s in each direction, utilization
- **NFS per mount**: ops/s, read/write kB/s, average RTT and execution time, retransmits
- **Sockets**: total sockets; TCP established/closed/orphaned/time-wait; UDP total
- **Filesystem space (local only)**: total size, used %, inode used %
- **Slurm context**: job id, step id, partition (when running inside a job)
