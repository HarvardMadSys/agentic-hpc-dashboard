# Coverage crosswalk: what each tier collects, and what replaced what

`README.md` asserts *"a coverage audit found 40 observable login-node signals: 15 already
read, 17 belonging to the node snapshot by nature, and 8 the collector could read and did
not."* That audit was never written down — this file is it.

It answers one question per row: **after the cutover, where does this signal come from?**

---

## 1. The two tiers

| Tier | Privilege | Written by | Lands in |
|---|---|---|---|
| **node** | unprivileged | `node_snapshot.py` (vendored `collect/common/snapshot.py`) | `login/logs/<date>/<host>.jsonl` — unchanged schema, unchanged path |
| **ebpf** | root | `ebpf_trace.py` | `ebpfm/logs/<date>/<host>.{snapshot,exits}.jsonl` |

`ebpfm.sh` runs both as root, the node tier alone as an ordinary user. The node tier's
mode follows the tier set: `SNAPSHOT_MODE=node` when the eBPF tier is running, `full`
when it is not.

**The eBPF tier is not a replacement for the node tier, and cannot become one.** It emits
only *events*, and only for *tracked* processes (§4). Everything in §2 stays where it is.

---

## 2. Node-scope — the node tier is the only source (20 keys)

eBPF cannot produce any of these. They are node aggregates and kernel counters, not task
events. Stated plainly: **eBPF cannot produce load average, memory totals, filesystem
space, or interface counters.**

| Key | Source | Why there is no eBPF analogue |
|---|---|---|
| `timestamp`, `hostname`, `collector`, `schema_version` | — | envelope |
| `uptime` (`load_1/5/15`, `users`) | `uptime` | a run-queue average is a kernel scalar sampled at an instant |
| `process_states` (R/D/S/Z/T) | `ps -e -o stat` | node-wide census; the eBPF tier sees only tracked pids |
| `memory`, `meminfo_detail` | `free -b`, `/proc/meminfo` | **memory totals** — nothing to hook |
| `cpu` (per-core), `vmstat` | `mpstat -P ALL`, `vmstat` | per-core utilisation aggregates |
| `proc_stat` (`processes`, `ctxt`, `procs_running/blocked`, **`btime`**) | `/proc/stat` | **`btime` is the reboot-era key for every dataset** — see §5 |
| `local_disk_io` | `iostat -xz` | per-device block-layer stats |
| `nfs_io`, `nfs_mountstats`, `nfs_rpc` | `nfsiostat`, `/proc/self/mountstats`, `/proc/net/rpc/nfs` | per-mount RTT / retrans — the wedge's primary evidence |
| `mount_topology` | `/proc/self/mountinfo` | autofs / NFS mount census |
| `disk_space` | `statvfs` | **filesystem space and inodes** |
| `network_io` | `sar -n DEV` | **per-interface counters** |
| `tcp_transport`, `socket_summary` | `/proc/net/snmp`, `ss -s` | node-wide protocol counters |
| `responsiveness` (`fork_exec_ms`, `stat_local_ms`, `getent_ms`) | synthetic probes | deliberately **active** measurement of what users feel |
| `system_fds` | `/proc/sys/fs/file-nr` | node-wide fd allocation |
| `gpu`, `gpu_pmon`, `gpu_compute_apps` | `nvidia-smi` | always null on a login node |
| `slurm` | `$SLURM_*` | always null on a login node |

## 3. Per-process / per-user aggregates — kept in the node tier (8 keys)

Unprivileged `ps` / cgroup rollups. The eBPF tier does **not** reproduce them, because it
is not a census (§4).

| Key | Why it stays | eBPF's nearest analogue (not a replacement) |
|---|---|---|
| `d_state_procs` | the `bwrap`-wedge discriminator behind `findings/login.md`; read by 13 analyze scripts; **uncapped and node-wide** | `residency.d_stack`, `residency_totals.d_stack_top` — tracked pids only |
| `proc_tree` | parent-chain attribution for the whole host; read by 6 scripts | `depth` / `tree_root_pid` per event — agent trees only |
| `per_user_cgroup` | exact per-user CPU/mem/pids from cgroup v2; read by 7 scripts | `residency_totals.by_actor3[].users` (a count) |
| `per_user_resources` | per-user rollup across all processes | none |
| `top_cpu_processes`, `top_rss_processes` | live top-N with full argv | per-exit `cpu_s` / `peak_rss_mb` — no *live* top-N |
| `logged_in_users` | the `w` table: tty, idle, from, jcpu | `tty` / `attribution='tty'` — no session table |

## 4. Carved — dropped from the login feed when the eBPF tier is live (4 keys)

Each was either structurally unusable unprivileged or strictly superseded. Each has a
named replacement.

| Dropped key | Why it was not worth keeping | Replacement |
|---|---|---|
| `top_io_processes` | **own-user only.** `/proc/<pid>/io` is ptrace-gated, so other users' processes were silently *omitted* — on a shared login node the section is structurally incomplete | `io` block (`task->ioac`) → `exit.io.{rd_mb,wr_mb,rchar_mb,wchar_mb}` + `io.scope`. Per process, exact, **all users** |
| `top_sleeping_procs` | `wchan` → `-` and `syscall` → `N/A` for every other user | `dstate_stat` → `residency.d_stack`: top-5 kernel frames from `/proc/<pid>/stack`. **Reading a kernel stack touches no filesystem**, so it cannot hang on the mount being diagnosed |
| `sleeping_wchans` | other users collapse into the `-` bucket, under-counting real NFS waits | `residency_totals.d_stack_top` — per-node top-10 blocking frames |
| `socket_talkers` | connection **counts** only, own-process attribution | `netbytes` + `tcp_*` → `exit.conns` / `conn` with real `rx_bytes`/`tx_bytes`, `rtt_ms`, `retrans`, `provider`, `connect_ms` |

## 5. What the eBPF tier adds that nothing had before

The eight from `README.md`, plus three the poller could never recover:

*(schema 5 adds `sandbox*`, `approval_mode`, `env_flags`, `session_uuid`, `ts_epoch` on
every record, `args_len`/`args_truncated`, the `submit` resource request, `io` on
`truncated`, and `top_procs`/`state_counts`/`tcp_open_ext` on `residency` — see §7b.)*

`cwd` / `cwd_source` / `work_dir` · `dstate_wait_s` / `_episodes` / `_max_s` / `_src` ·
`net_tx_bytes` / `net_rx_bytes` / `net_calls` (QUIC-visible) · `d_stack` / `d_stack_top` ·
`event="truncated"` with drained kernel counters · `attribution` (`ancestry`|`tty`|`uid`) ·
`residency_totals.by_agent_type` · `exit.conns` (inbound folded in) · **`exit_code` / `signal` /
`core_dumped`** · **sub-0.78 s processes** (the poller's measured capture floor) ·
**per-process I/O for all users**.

---

## 6. Two things that make the node tier non-optional

1. **Era detection.** `analyze/split_by_reboot.py::detect_reboots()` reads
   `proc_stat.btime` from the first record of each daily *login* file. The resulting
   per-host boot map is what assigns reboot eras to `agents`, `dcgm`, `slurm_jobs`,
   `slurm_nodes` **and `ebpfm` itself**. No other dataset carries boot time. Without the
   node tier, era partitioning — the organising principle of this whole study — stops.
2. **The wedge's own signature.** An orphaned `bwrap` reparented to init, with no tty and
   no agent ancestry, is **invisible to the eBPF tier by default** (§7). It is exactly
   what `d_state_procs` catches.

## 7. The eBPF tier is not a census

`actor_of()` admits a process on three rungs, in strict precedence — **agent ancestry**,
then **tty**, then **`EBPFM_MIN_UID`** (default `0`, i.e. off) — and drops it otherwise.
Further filters: `INCLUDE_ROOTS=0`, idle timers (`sleep`/`usleep` with no CPU and no I/O),
`MIN_DURATION`, `MIN_CPU`.

So detached `nohup` processes, cron jobs, system daemons and orphaned sandboxes do not
appear. **Never use an eBPF record count as a node denominator.** `residency_totals`
carries the honest ones: `dropped.n` / `dropped.by_comm` for what the actor filter threw
away, and `fork_total` / `fork_rate` from `/proc/stat` for the node's real spawn rate.

`EBPFM_MIN_UID` stays **off**: enabling it changes what `actor3="human"` *means* and would
make the human population incomparable with every earlier capture. If it is ever turned
on, filter downstream on `attribution` — which is why that field exists.

## 7b. Schema 5: what the dashboard asked for, and what it got instead

Ten additive fields. Three were built differently from the request, each for a reason that
would otherwise have produced a confidently wrong number.

| Requested | Built | Why |
|---|---|---|
| `sandbox` from namespaces **or** `Seccomp>=2` **or** reduced `CapEff` | namespaces **only**; seccomp and caps in `sandbox_detail` | An unprivileged process has `CapEff=0` *by definition*, so that rung alone would report the whole login fleet as sandboxed; and a process that is uid 0 inside a user namespace — the case of interest — reports a **full** set, so the inverse rule fails too. Seccomp is set by systemd's `SystemCallFilter=` on ordinary daemons and by every Electron renderer, and `human-vscode` is a first-class actor class here |
| `#SBATCH` script scan for `partition`/`gpus`/`array`/`time_limit` | argv only; the rest from the `slurm_jobs` census join on `job_id` | The script path comes from `work_dir`, a path read from `/proc`. Resolving one is what the `read_cwd` doctrine forbids — it blocks on a wedged autofs mount during the very incident the collector exists to observe. The census carries all four already and is authoritative: what Slurm granted, not what the script asked for |
| `args_len` as the pre-cap length | +2 lines of BPF so it is the **true** pre-clamp length | The kernel clamp overwrote the length before it left the kernel, so a userspace measure would have saturated at `ARGV_KMAX` — wrong exactly for the long agent invocations the field exists to count |

Two defects were found while tracing this and fixed in the same pass:

- The three hand-built identity stubs supplied **7 keys against `_base_identity`'s 21**, so
  `tcp` and `conn` records were *already* omitting fourteen fields — absent, not null.
  (`tcp` is gone as of schema 6; `conn` still carries the full identity.)
- `decode_argv` could **fabricate an agent classification**: a clamped buffer ends
  mid-token with no trailing NUL, so `/opt/tools/claude-wrapper` truncated to `…/claude`
  matched the `claude_code` pattern's end anchor.

And one latent leak: `tcp_birth` is only deleted on TCP_CLOSE, so a lost perf event leaks
the entry permanently — a phantom `conn` record every tick, then silent loss of new births
once the map fills. Now reported as `residency_totals.conn_unmatched_births`.

## 8. Semantics that changed, not just fields

| Poller field | eBPF | Consequence |
|---|---|---|
| `samples` | emitted as `null` | a proxy for observed lifetime *under a sampling regime*; meaningless for an exhaustive collector. Must become null downstream, **not 0** |
| `state_last` | `null` on `exit`, real on `truncated` | only ever meaningful for a still-running process |
| `d_samples` | **no analogue** | replaced by a better measurement, not a translation: `dstate_wait_s` / `dstate_episodes` / `dstate_max_s` in real kernel seconds |
| `state_d_frac` | `dstate_wait_s / duration_s` | **better** — real seconds, not a poll count |
| `state_r_frac` | `sched_run_s / duration_s` | **better** — actual on-CPU time |
| `state_z_frac` | **`residency.state_counts['Z']`** (schema 5) | Was lost: an exit-triggered collector cannot see a zombie, because a zombie has not exited. The residency tick *can* — it samples live processes — so the census is recoverable from schema 5 on, as a tick-sampled count rather than a per-session fraction |
| `peak_rss_mb` (per-mm high-water) | **`signal->maxrss`** — the **thread-group** high-water mark, i.e. `ru_maxrss`'s source | Changed meaning, and a bug fix. The old read was `mm->hiwater_rss`, but the kernel runs `exit_mm()` — which sets `tsk->mm = NULL` — *before* `sched_process_exit` fires, so the guard never passed and the field was **`0.0` on every record ever emitted**, not null. For a single-threaded process the two agree exactly (verified against `getrusage`/`time -v`: 309.75 MB both ways); for a multi-threaded one the new value is the group peak, which is the more useful number but **is not the same quantity**. Any pre-fix capture's `peak_rss_mb` is a constant zero and must be discarded, not averaged |
| intra-life RSS/CPU trajectory | lost for exited processes | `peak_rss_mb` (`signal->maxrss`) and total `cpu_s` survive and are *more* accurate; the shape between exec and exit is gone |

Counts are **not comparable across the cutover**: the poller was CPU-quota-bound at
1.84–3.18 Hz with a measured 0.78 s capture floor, and the eBPF tier has no floor, so it
sees strictly more — and asymmetrically, since agents spawn proportionally more
short-lived processes than humans. Report per era; never mix a short-process count across
the boundary.
