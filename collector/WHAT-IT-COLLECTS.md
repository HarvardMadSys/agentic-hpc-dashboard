# Collectors
> We are overcollecting data right now as we are still in the stage of doing measurement and analysis. Once we are done with the analysis, the collector will be configured to collect only the necessary data.

> We also capture some duplicate data to verify the accuracy of our prior collection efforts (which run as unprivileged processes on the login nodes with 1 CPU core).

## What it collects

The collectors collect data from two sources:
user-level and kernel-level (using eBPF).

### eBPF-based collection
- **Process lifecycle (fork, exec, exit)**: pid, parent pid, uid, executable name (e.g., python3, bash, rsync), timestamp
- **Command line (at exec)**: full argv, whether it was cut (kernel only captures 4096 bytes)
- **Process context**: controlling terminal type (e.g., pts/0, tty1, or headless), cgroup, process group id, session id
- **Exit status**: exit code, terminating signal, whether a core was dumped
- **CPU time (at exit)**: user and system time for the whole process, CPU time of reaped children, time on CPU, time waiting in the run queue
- **Memory (at exit)**: peak resident set size, thread count, minor and major page faults, voluntary and involuntary context switches
- **I/O (at exit)**: bytes read and written at the storage layer, bytes read and written through syscalls
- **Delay accounting (at exit)**: time blocked on disk I/O, on swap-in, on free pages
- **Uninterruptible sleep (D state)**: total time, longest time spent in D state (the longest session a process spends in D state), number of times in D state
- **Network bytes per process (TCP/UDP, IPv4/IPv6)**: bytes sent, bytes received, number of send/receive calls
- **Network bytes per endpoint (TCP/UDP)**: remote address, port, protocol, direction (inbound/outbound), bytes sent, bytes received, call count
- **TCP connections**: local and remote address and port, address family, open time, duration, connect latency, established or failed, bytes sent and received, inbound (accepted) or outbound
- **Slurm submissions (sbatch/salloc/srun)**: the tool's stdout/stderr, and the job id parsed from it

### User-level collection

Sampled as a snapshot at regular intervals (default to 5 minutes) by peeking at /proc and /sys, or using standard command-line tools.

**Per process**
- **Process state**: state (R/S/D/Z/T), parent pid, process group, session, controlling terminal, start time, thread count
- **Resource usage**: CPU ticks (own and children), minor/major page faults, current and peak resident set size
- **I/O counters**: bytes read/written at the storage layer and through syscalls, cancelled writes
- **Command line and working directory**: full command line, working directory
- **Sandboxing**: `/proc/{pid}/ns` (user/mount/pid/net namespace identity), `proc/{pid}/status` (seccomp mode, no-new-privs, capability bounding set, pid-namespace depth)
- **Blocked processes**: top kernel stack frames of a process in D state, current syscall, open file-descriptor count
- **Scheduler**: on-CPU time, run-queue wait, timeslices; cgroup path

**Node-wide kernel counters**
- **Scheduler**: boot time, cumulative forks, context switches, running and blocked processes
- **Memory**: dirty, writeback, NFS-unstable, slab (reclaimable/unreclaimable), committed address space
- **TCP transport**: segments out, retransmitted segments, input errors, failed connection attempts, listen drops/overflows, timeouts
- **NFS**: RPC calls, retransmissions, auth refreshes, accounting per mount (bad XIDs, request backlog, read/write op counts, retransmits, queue/RTT/execution time)
- **Mounts**: autofs trigger count, NFS mount count, distinct NFS servers
- **System**: allocated/free/max file descriptors, scheduler-stats and delay-accounting sysctls, boot id, ephemeral port range

**Per-user cgroup**
- CPU usage, throttling periods and throttled time, CPU quota and period, current memory, current and maximum pids

**Open sockets (TCP/UDP, IPv4/IPv6)**
- Address family (inet or ipv6), protocol, state, local and remote address and port, owning uid, RTT and its variance, retransmits, congestion window, bytes sent and received, segments in/out, time since last send/receive, receive and send queue depth

**Node health (using command line tools)**
- **Load**: 1/5/15-minute load averages, logged-in user count
- **Sessions**: user, tty, origin host, login time, idle time, session CPU
- **Process census**: count of running, blocked, sleeping, zombie, stopped processes
- **Memory**: total/used/free/shared/cache/available; swap total/used/free
- **CPU per core**: user, system, iowait, irq, softirq, idle %
- **Virtual memory**: runnable/blocked processes, swap in/out, block in/out, interrupts/s, context switches/s
- **Disk I/O per device**: reads and writes per second, kB/s, read and write await, utilization
- **Network per interface**: packets/s and kB/s in each direction, utilization
- **NFS per mount**: ops/s, read/write kB/s, average RTT and execution time, retransmits
- **Sockets**: total sockets; TCP established/closed/orphaned/time-wait; UDP total; top processes by established connection count
- **Processes**: D-state processes (pid, parent, state, user, wait channel, command); top processes by CPU and by memory (pid, parent, user, CPU %, memory %, RSS, state, start, CPU time, command); per-user CPU %, RSS and process count; whole-host process tree (pid, parent, command, agent tag); sleeping processes by wait channel
- **Filesystem space (local only)**: total size, used %, inode used %
- **Slurm context**: job id, step id, partition (when running inside a job)
