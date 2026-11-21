# Coverage crosswalk: what each tier collects, and what replaced what

`README.md` asserts *"a coverage audit found 40 observable login-node signals: 15 already
read, 17 belonging to the node snapshot by nature, and 8 the collector could read and did
not."* That audit was never written down, so this file records it.

Each row answers one question: **after the cutover, where does this signal come from?**

---

## 1. The two tiers

| Tier | Privilege | Written by | Lands in |
|---|---|---|---|
| **node** | unprivileged | `node_snapshot.py` (vendored `collect/common/snapshot.py`) | `$EBPFM_NODE_OUTPUT_DIR/<date>/<host>.jsonl` — the login poller's schema, unchanged |
| **ebpf** | root | `ebpf_trace.py` | `$EBPFM_OUTPUT_DIR/<date>/<host>.{snapshot,exits}.jsonl` |

Run as root, `ebpfm.sh` starts both tiers. Run as an ordinary user, it starts only the
node tier. The node tier's mode depends on which tiers are running: `SNAPSHOT_MODE=node`
when the eBPF tier is running, `full` when it is not.

**The eBPF tier is not a replacement for the node tier, and cannot become one.** It emits
only *events*, and only for *tracked* processes (§7). Everything in §2 stays where it is.

---

## 2. Node-scope — the node tier is the only source (29 keys)

eBPF cannot produce any of these, because they are node aggregates and kernel counters
rather than task events. In particular, **eBPF cannot produce load average, memory
totals, filesystem space, or interface counters.**

| Key | Source | Why there is no eBPF analogue |
|---|---|---|
| `timestamp`, `hostname`, `collector`, `schema_version` | — | envelope |
| `ts_epoch`, `tz`, `redact` | — | appended in schema 8 after every other key: the epoch time, the zone, and which masking ran on the command lines (`EBPFM_REDACT`) |
| `uptime` (`load_1/5/15`, `users`) | `uptime` | a run-queue average is a kernel scalar sampled at an instant |
| `process_states` (R/D/S/Z/T) | `ps -e -o stat` | node-wide census; the eBPF tier sees only tracked pids |
| `memory`, `meminfo_detail` | `free -b`, `/proc/meminfo` | **memory totals**; there is nothing to hook |
| `cpu` (per-core), `vmstat` | `mpstat -P ALL`, `vmstat` | per-core utilisation aggregates |
| `proc_stat` (`processes`, `ctxt`, `procs_running/blocked`, **`btime`**) | `/proc/stat` | **`btime` is the reboot-era key for every dataset** — see §6 |
| `local_disk_io` | `iostat -xz` | per-device block-layer stats |
| `nfs_io`, `nfs_mountstats`, `nfs_rpc` | `nfsiostat`, `/proc/self/mountstats`, `/proc/net/rpc/nfs` | per-mount RTT / retrans (the primary evidence for the wedge) |
| `mount_topology` | `/proc/self/mountinfo` | autofs / NFS mount census |
| `disk_space` | `statvfs` | **filesystem space and inodes** |
| `network_io` | `sar -n DEV` | **per-interface counters** |
| `tcp_transport`, `socket_summary` | `/proc/net/snmp`, `ss -s` | node-wide protocol counters |
| `responsiveness` (`fork_exec_ms`, `stat_local_ms`, `getent_ms`) | synthetic probes | a deliberately **active** measurement of the latency users experience |
| `system_fds` | `/proc/sys/fs/file-nr` | node-wide fd allocation |
| `gpu`, `gpu_pmon`, `gpu_compute_apps` | nothing: `nvidia-smi` is no longer run | always null, since login nodes have no GPUs; the keys stay for the key-order contract |
| `slurm` | `$SLURM_*` | always null on a login node |

## 3. Per-process / per-user aggregates — kept in the node tier (7 keys)

Unprivileged `ps` / cgroup rollups. The eBPF tier does **not** reproduce them, because it
is not a census (§7).

| Key | Why it stays | eBPF's nearest analogue (not a replacement) |
|---|---|---|
| `d_state_procs` | the `bwrap`-wedge discriminator behind `findings/login.md`; read by 13 analyze scripts; **uncapped and node-wide** | `residency.d_stack`, `residency_totals.d_stack_top` (tracked pids only) |
| `proc_tree` | parent-chain attribution for the whole host; read by 6 scripts | `depth` / `tree_root_pid` per event (agent trees only) |
| `per_user_cgroup` | exact per-user CPU/mem/pids from cgroup v2; read by 7 scripts | `residency_totals.by_actor3[].users` (a count) |
| `per_user_resources` | per-user rollup across all processes | none |
| `top_cpu_processes`, `top_rss_processes` | live top-N with full argv | per-exit `cpu_s` / `peak_rss_mb`, but no *live* top-N |
| `logged_in_users` | the `w` table: tty, idle, from, jcpu | `tty` / `attribution='tty'`, but no session table |

## 4. Carved — dropped from the login feed when the eBPF tier is live (4 keys)

Each of these keys was either structurally unusable without privilege or strictly
superseded, and each has a named replacement.

| Dropped key | Why it was not worth keeping | Replacement |
|---|---|---|
| `top_io_processes` | **own-user only.** `/proc/<pid>/io` is ptrace-gated, so other users' processes were silently *omitted* — on a shared login node the section is structurally incomplete | `io` block (`task->ioac`) → `exit.io.{rd_mb,wr_mb,rchar_mb,wchar_mb}` + `io.scope`. Per process, exact, **all users** |
| `top_sleeping_procs` | `wchan` → `-` and `syscall` → `N/A` for every other user | `residency.d_stack`: top-5 kernel frames from `/proc/<pid>/stack`, read at the tick whatever BPF blocks loaded. **Reading a kernel stack touches no filesystem**, so it cannot hang on the mount being diagnosed |
| `sleeping_wchans` | other users collapse into the `-` bucket, under-counting real NFS waits | `residency_totals.d_stack_top` — per-node top-10 blocking frames |
| `socket_talkers` | connection **counts** only, own-process attribution | `netbytes` + `tcp_*` → `exit.conns` / `conn` with real `rx_bytes`/`tx_bytes`, `rtt_ms`, `retrans`, `provider`, `connect_ms`; from schema 7, `netpeer_*` → `exit.net_endpoints` / `netio`, bytes per endpoint for UDP as well as TCP |

## 5. What the eBPF tier adds that nothing had before

These are the eight from `README.md`, plus three the poller could never recover:

*(Schema 5 adds `sandbox*`, `approval_mode`, `env_flags` (dropped in 7), `session_uuid`, `ts_epoch` on
every record, `args_len`/`args_truncated`, the `submit` resource request, `io` on
`truncated`, and `top_procs`/`state_counts`/`tcp_open_ext` on `residency`. See §7b.)*

`cwd` / `work_dir` · `dstate_wait_s` / `_episodes` / `_max_s` / `_src` ·
`net_tx_bytes` / `net_rx_bytes` / `net_calls` (QUIC-visible) · `net_endpoints` / `netio`
(schema 7: the endpoint of every byte, and its time series) · `d_stack` / `d_stack_top` ·
`event="truncated"` with drained kernel counters · `attribution` (`ancestry`|`tty`|`uid`) ·
`residency_totals.by_agent_type` · `exit.conns` (inbound folded in) · **`exit_code` / `signal` /
`core_dumped`** · **sub-0.78 s processes** (the poller's measured capture floor) ·
**per-process I/O for all users**.

---

## 6. Two things that make the node tier non-optional

1. **Era detection.** `analyze/split_by_reboot.py::detect_reboots()` reads
   `proc_stat.btime` from the first record of each daily *login* file. The resulting
   per-host boot map assigns reboot eras to `agents`, `dcgm`, `slurm_jobs`,
   `slurm_nodes` **and `ebpfm` itself**. No other dataset carries boot time. Without the
   node tier, era partitioning (the organising principle of this whole study) stops
   working.
2. **The wedge's own signature.** An orphaned `bwrap` that has been reparented to init,
   with no tty and no agent ancestry, is **invisible to the eBPF tier by default** (§7).
   That is exactly the process `d_state_procs` catches.

## 7. The eBPF tier is not a census

`actor_of()` admits a process if it matches one of three rules, checked in strict order:
**agent ancestry**, then **tty**, then **`EBPFM_MIN_UID`** (default `0`, i.e. off). A
process that matches none of them is dropped. Further filters are `INCLUDE_ROOTS=0`, idle
timers (`sleep`/`usleep` with no CPU and no I/O), `MIN_DURATION` and `MIN_CPU`.

As a result, detached `nohup` processes, cron jobs, system daemons and orphaned sandboxes
do not appear. **Never use an eBPF record count as a node denominator.** The correct
denominators are in `residency_totals`: `dropped.n` / `dropped.by_command` count what the
actor filter threw away, and `fork_total` / `fork_rate` from `/proc/stat` give the node's
real spawn rate.

`EBPFM_MIN_UID` stays **off**. Enabling it changes what `actor3="human"` *means*, and would
make the human population incomparable with every earlier capture. If it is ever turned
on, filter downstream on `attribution`. That is why the field exists.

## 7b. Schema 5: what the dashboard asked for, and what it got instead

Schema 5 added ten fields. Three were built differently from what was requested, because
building them as requested would have produced numbers that look right but are wrong.

| Requested | Built | Why |
|---|---|---|
| `sandbox` from namespaces **or** `Seccomp>=2` **or** reduced `CapEff` | namespaces **only**; seccomp and caps in `sandbox_detail` | An unprivileged process has `CapEff=0` *by definition*, so the caps rule alone would report the whole login fleet as sandboxed. A process that is uid 0 inside a user namespace (the case we care about) reports a **full** set, so the inverse rule fails too. Seccomp is set by systemd's `SystemCallFilter=` on ordinary daemons and by every Electron renderer, and `human-vscode` is a first-class actor class here |
| `#SBATCH` script scan for `partition`/`gpus`/`array`/`time_limit` | argv only; the rest comes from joining the `slurm_jobs` census on `job_id` | The script path comes from `work_dir`, a path read from `/proc`. The `read_cwd` doctrine forbids resolving such a path, because it blocks on a wedged autofs mount during the very incident the collector exists to observe. The census already carries all four fields and is authoritative, since it records what Slurm granted rather than what the script asked for |
| `args_len` as the pre-cap length | +2 lines of BPF so it is the **true** pre-clamp length | The kernel clamp overwrote the length before it left the kernel, so a userspace measure would have saturated at `ARGV_KMAX`. That would be wrong for exactly the long agent invocations the field exists to count |

Two defects turned up while tracing this, and both were fixed in the same pass:

- The three hand-built identity stubs supplied **7 keys against `_base_identity`'s 21**, so
  `tcp` and `conn` records were *already* omitting fourteen fields (absent, not null).
  (`tcp` is gone as of schema 6; `conn` still carries the full identity.)
- `decode_argv` could **fabricate an agent classification**. A clamped buffer ends
  mid-token with no trailing NUL, so `/opt/tools/claude-wrapper` truncated to `…/claude`
  matched the end anchor of the `claude_code` pattern.

There was also one latent leak. `tcp_birth` is only deleted on TCP_CLOSE, so a lost perf
event leaks the entry permanently. The result is a phantom `conn` record every tick, and
then silent loss of new births once the map fills. The leak is now reported as
`residency_totals.conn_unmatched_births`.

## 8. Semantics that changed, not just fields

| Poller field | eBPF | Consequence |
|---|---|---|
| `samples` | emitted as `null` | a proxy for observed lifetime *under a sampling regime*, which is meaningless for an exhaustive collector. It must become null downstream, **not 0** |
| `state_last` | `null` on `exit`, real on `truncated` | only ever meaningful for a still-running process |
| `d_samples` | **no analogue** | replaced by a better measurement rather than translated: `dstate_wait_s` / `dstate_episodes` / `dstate_max_s` in real kernel seconds |
| `state_d_frac` | `dstate_wait_s / duration_s` | **better** — real seconds, not a poll count |
| `state_r_frac` | `sched_run_s / duration_s` | **better** — actual on-CPU time |
| `state_z_frac` | **`residency.state_counts['Z']`** (schema 5) | This was lost, because an exit-triggered collector cannot see a zombie (a zombie has not exited). The residency tick *can* see one, because it samples live processes. So from schema 5 on the census is recoverable, as a tick-sampled count rather than a per-session fraction |
| `peak_rss_mb` (per-mm high-water) | **`signal->maxrss`**, the **thread-group** high-water mark (the source of `ru_maxrss`) | Both a change of meaning and a bug fix. The old code read `mm->hiwater_rss`, but the kernel runs `exit_mm()` (which sets `tsk->mm = NULL`) *before* `sched_process_exit` fires. The guard never passed, so the field was **`0.0` on every record ever emitted**, not null. For a single-threaded process the old and new values agree exactly (verified against `getrusage`/`time -v`: 309.75 MB both ways). For a multi-threaded process the new value is the group peak, which is the more useful number but **is not the same quantity**. In any capture from before the fix, `peak_rss_mb` is a constant zero and must be discarded, not averaged |
| intra-life RSS/CPU trajectory | lost for exited processes | `peak_rss_mb` (`signal->maxrss`) and total `cpu_s` survive and are *more* accurate, but the shape between exec and exit is gone |

## 8b. Schema 8: fields whose meaning changed

| Field | Schema 7 | Schema 8 | Consequence |
|---|---|---|---|
| `cpu_s`, `sched_run_s`, `min_flt`/`maj_flt`, `nvcsw`/`nivcsw`, `sched_wait_s`, `*_wait_s`, `dstate_*`, `io`, `threads`, `peak_rss_mb` | the group leader's, plus threads already released into `signal_struct` | every thread's, with `thread_acc` (`meta.counters_scope`) | multi-threaded processes were undercounted by up to 99.8%; do not compare across the boundary |
| `args`, `captured_tail` | raw | credentials and agent prompts masked; `args_redacted` says what | `args_len`/`args_truncated` still describe the raw argv |
| `net_*_bytes`, `net_endpoints` | included the NFS client's RPCs | kernel sockets excluded, counted per node in `netpeer_kernel` | lower for NFS-heavy processes |
| `cgroup` | inherited at fork when the id lookup missed, which on 4.18 was always | from file-handle ids, else `/proc` | `/init.scope` on user processes was an artefact |
| `ppid` (fork) | the forking thread | the forking process, kept across reparenting | children of worker threads and of `nohup` keep their ancestry |

Counts are **not comparable across the cutover**. The poller was CPU-quota-bound at
1.84–3.18 Hz with a measured 0.78 s capture floor. The eBPF tier has no floor, so it sees
strictly more, and the increase is asymmetric because agents spawn proportionally more
short-lived processes than humans do. Report per era, and never mix a short-process count
across the boundary.
