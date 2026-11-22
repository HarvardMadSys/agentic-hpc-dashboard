# Coverage crosswalk: what each tier collects, and what replaced what

For each of the node snapshot's 40 keys, which tier reads it; for the four the node tier
drops beside the eBPF tier, what replaces them; and which of the unprivileged poller's fields
changed meaning at the cutover to eBPF. The tiers are described in
[README.md](README.md#two-tiers-chosen-by-privilege), and the eBPF tier's records and fields,
schema by schema, in [README.md](README.md#what-it-collects).

The node tier, `node_snapshot.py`, writes `$EBPFM_NODE_OUTPUT_DIR/<date>/<host>.jsonl` in
the login poller's schema, unchanged: `SNAPSHOT_MODE=node` when the eBPF tier is running,
`full` when it is not.

---

## 1. Node-scope — the node tier is the only source (29 keys)

These are node aggregates and kernel counters rather than task events, so eBPF cannot
produce them.

| Key | Source | Why there is no eBPF analogue |
|---|---|---|
| `timestamp`, `hostname`, `collector`, `schema_version` | — | envelope |
| `ts_epoch`, `tz`, `redact` | — | appended after every other key: the epoch time, the zone, and which masking ran on the command lines (`EBPFM_REDACT`) |
| `uptime` (`load_1/5/15`, `users`) | `uptime` | a run-queue average is a kernel scalar sampled at an instant |
| `process_states` (R/D/S/Z/T) | `ps -e -o stat` | node-wide census; the eBPF tier sees only tracked pids |
| `memory`, `meminfo_detail` | `free -b`, `/proc/meminfo` | **memory totals**; there is nothing to hook |
| `cpu` (per-core), `vmstat` | `mpstat -P ALL`, `vmstat` | per-core utilisation aggregates |
| `proc_stat` (`processes`, `ctxt`, `procs_running/blocked`, **`btime`**) | `/proc/stat` | **`btime` is the reboot-era key for every dataset** (§4) |
| `local_disk_io` | `iostat -xz` | per-device block-layer stats |
| `nfs_io`, `nfs_mountstats`, `nfs_rpc` | `nfsiostat`, `/proc/self/mountstats`, `/proc/net/rpc/nfs` | per-mount RTT / retrans, the primary evidence for an NFS or autofs wedge |
| `mount_topology` | `/proc/self/mountinfo` | autofs / NFS mount census |
| `disk_space` | `statvfs` | **filesystem space and inodes** |
| `network_io` | `sar -n DEV` | **per-interface counters** |
| `tcp_transport`, `socket_summary` | `/proc/net/snmp`, `ss -s` | node-wide protocol counters |
| `responsiveness` (`fork_exec_ms`, `stat_local_ms`, `getent_ms`) | synthetic probes | a deliberately **active** measurement of the latency users experience |
| `system_fds` | `/proc/sys/fs/file-nr` | node-wide fd allocation |
| `gpu`, `gpu_pmon`, `gpu_compute_apps` | nothing: `nvidia-smi` is never run ([VENDORED.md](VENDORED.md)) | always null; login nodes have no GPUs |
| `slurm` | `$SLURM_*` | always null on a login node |

## 2. Per-process / per-user aggregates — kept in the node tier (7 keys)

Unprivileged `ps` and cgroup rollups. The eBPF tier does **not** reproduce them, because it
is not a census (§5).

| Key | Why it stays | eBPF's nearest analogue (not a replacement) |
|---|---|---|
| `d_state_procs` | **uncapped and node-wide**: it catches the `bwrap` wedge (§4) | `residency.d_stack`, `residency_totals.d_stack_top` (tracked pids only) |
| `proc_tree` | parent-chain attribution for the whole host | `depth` / `tree_root_pid` per event (agent trees only) |
| `per_user_cgroup` | exact per-user CPU/mem/pids from cgroup v2 | `residency_totals.by_actor3[].users` (a count) |
| `per_user_resources` | per-user rollup across all processes | none |
| `top_cpu_processes`, `top_rss_processes` | live top-N, with command lines | per-exit `cpu_s` / `peak_rss_mb`, but no *live* top-N |
| `logged_in_users` | the `w` table: tty, idle, from, jcpu | `tty` / `attribution='tty'`, but no session table |

## 3. Carved — dropped from the login feed when the eBPF tier is live (4 keys)

Each was structurally unusable without privilege, or strictly superseded, and each has a
named replacement.

| Dropped key | Why it was not worth keeping | Replacement |
|---|---|---|
| `top_io_processes` | **own-user only**: `/proc/<pid>/io` is ptrace-gated, so other users' processes were silently *omitted* | the `io` block (`task->ioac`) → `exit.io.{rd_mb,wr_mb,rchar_mb,wchar_mb}` + `io.scope`: per process, exact, **all users** |
| `top_sleeping_procs` | `wchan` → `-` and `syscall` → `N/A` for every other user | `residency.d_stack`, top-5 kernel frames ([README](README.md#the-eight-data-points-this-adds-over-the-poller), data point 4) |
| `sleeping_wchans` | other users collapse into the `-` bucket, under-counting real NFS waits | `residency_totals.d_stack_top`, the node's top-10 blocking frames |
| `socket_talkers` | connection **counts** only, own-process attribution | `exit.conns` / `conn`, with real `rx_bytes`/`tx_bytes`, `rtt_ms`, `retrans`, `provider`, `connect_ms`; from schema 7, `exit.net_endpoints` / `netio`, bytes per endpoint for UDP as well as TCP |

## 4. Two things that make the node tier non-optional

1. **Era detection.** `proc_stat.btime` is the only record of boot time. The measurement
   repository's era split (`analyze/split_by_reboot.py`) reads it from the first record of
   each daily *login* file to assign reboot eras to every dataset, `ebpfm` included.
2. **The wedge's own signature.** An orphaned `bwrap` that has been reparented to init, with
   no tty and no agent ancestry, is **invisible to the eBPF tier by default** (§5). That is
   exactly the process `d_state_procs` catches.

## 5. The eBPF tier is not a census

`actor_of()` admits a process by the first of three rules it matches: **agent ancestry**,
then **tty**, then **`EBPFM_MIN_UID`** (default `0`, i.e. off, and it stays off:
[README](README.md#the-eight-data-points-this-adds-over-the-poller), data point 6). A process
that matches none is dropped, and `EBPFM_INCLUDE_ROOTS=0`, idle timers (`sleep`/`usleep` with
no CPU and no I/O), `EBPFM_MIN_DURATION` and `EBPFM_MIN_CPU` drop more.

So detached `nohup` processes, cron jobs, system daemons and orphaned sandboxes do not
appear. **Never use an eBPF record count as a node denominator.** The denominators are in
`residency_totals`: `dropped.n` / `dropped.by_command` count what the actor filter threw
away, and `fork_total` / `fork_rate`, from `/proc/stat`, give the node's real spawn rate.

## 6. Semantics that changed, not just fields

| Poller field | eBPF | Consequence |
|---|---|---|
| `samples` | emitted as `null` | a proxy for observed lifetime *under a sampling regime*, meaningless for an exhaustive collector. It must become null downstream, **not 0** |
| `state_last` | `null` on `exit`, real on `truncated` | only ever meaningful for a still-running process |
| `d_samples` | **no analogue** | replaced by `dstate_wait_s` / `dstate_episodes` / `dstate_max_s`, in real kernel seconds |
| `state_d_frac` | `dstate_wait_s / duration_s` | **better**: real seconds, not a poll count |
| `state_r_frac` | `sched_run_s / duration_s` | **better**: actual on-CPU time |
| `state_z_frac` | **`residency.state_counts['Z']`** (schema 5) | an exit-triggered collector cannot see a zombie, which has not exited, but the residency tick samples live processes: a tick-sampled count, not a per-session fraction |
| `peak_rss_mb` (per-mm high-water) | **`signal->maxrss`**, the **thread-group** high-water mark (the source of `ru_maxrss`) | a change of meaning and a bug fix. The old code read `mm->hiwater_rss`, but the kernel runs `exit_mm()` (which sets `tsk->mm = NULL`) *before* `sched_process_exit` fires, so the field was **`0.0` on every record**, not null: in a capture from before the fix it must be discarded, not averaged. For a single-threaded process old and new agree; for a multi-threaded one the new value is the group peak, which **is not the same quantity** |
| intra-life RSS/CPU trajectory | lost for exited processes | `peak_rss_mb` and total `cpu_s` survive and are *more* accurate, but the shape between exec and exit is gone |

Counts are **not comparable across the cutover**. The poller was CPU-quota-bound at
1.84–3.18 Hz with a measured 0.78 s capture floor. The eBPF tier has no floor, so it sees
strictly more, and the increase is asymmetric because agents spawn proportionally more
short-lived processes than humans do. Report per era, and never mix a short-process count
across the boundary.
