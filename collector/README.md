# eBPF_marthen_new — login-node collector, one bash entry point

A **standalone** collector for FASRC-style login nodes. Everything it needs is in this
folder: no `../` imports, so it runs anywhere you can copy it (or hand over one
self-extracting file from `ebpfm.sh bundle`). See [VENDORED.md](VENDORED.md) for the four
copied modules and the sync rule, and [COVERAGE.md](COVERAGE.md) for the field-by-field
crosswalk of what each tier collects and what replaced what.

## Two tiers, chosen by privilege

Only half of what a login node needs is a kernel event, so only half needs root:

| Tier | Privilege | What | Output |
|---|---|---|---|
| **node** | **unprivileged** | node health: load, memory, filesystem space, NFS per-mount RTT, interface counters, the D-state census, the process tree, per-user cgroup totals, boot time | `$EBPFM_NODE_OUTPUT_DIR/<date>/<host>.jsonl` |
| **ebpf** | **root** | every process exit with its status, argv, `cwd`, per-process I/O for **all** users, D-state dwell, network bytes, TCP records | `$EBPFM_OUTPUT_DIR/<date>/<host>.jsonl` |

**eBPF cannot produce load average, memory totals, filesystem space or interface
counters**, and it is not a process census (only *tracked* processes are emitted — see
COVERAGE.md §7). The node tier is therefore not a fallback; it is the other half.

```
./ebpfm.sh check                # works unprivileged: node tier PASS, ebpf tier "needs root"
./ebpfm.sh start                # as a user -> node tier only
```
```
sudo ./ebpfm.sh bootstrap       # once per node: dependencies + sysctls
sudo ./ebpfm.sh check           # expect 0 FAIL
sudo ./ebpfm.sh start           # both tiers; the eBPF one in system.slice (see below)
sudo ./ebpfm.sh status
sudo ./ebpfm.sh stop            # SIGTERM -> flushes live processes as `truncated`
sudo ./ebpfm.sh install-unit    # persistent systemd unit, for a fleet deployment
```

`EBPFM_TIER=node|ebpf|all` overrides the privilege-derived default. The node tier's
collection mode follows the tier set: with the eBPF tier running it drops the four
per-process sections the eBPF tier collects properly (`SNAPSHOT_MODE=node`); without it,
it keeps everything, because dropping them on a host with no eBPF tier would lose them
outright. **Losing root on one node costs that node its per-process tier and nothing
else** — this is per host, not fleet-wide.

### Supervision

`start` uses a *transient* `systemd-run` scope: fine for a one-off, but it does not
survive a reboot and has no restart policy. For a fleet, `install-unit` writes
`ebpfm.service` (`Restart=on-failure`, `Slice=system.slice`). The repo keepalive
(`run.sh`) deliberately does **not** manage the eBPF tier — it is unprivileged and cannot
`sudo` across the fleet — so systemd supervises it and `collect/healthcheck.py` is what
notices when it dies.

`Restart=on-failure`, never `always`, and nothing restarts it periodically: SIGTERM makes
it flush every live tracked process as `truncated`, so a cycle-time restart would corrupt
the trace. Same rule `run.sh` documents for the pollers.

---

## Why eBPF, precisely

The unprivileged `/proc` poller it replaces is **quota-bound, not design-bound**. Every
process a user runs on a FASRC login node shares one cgroup v2 slice capped at
`cpu.max = 1.0` CPU. One `/proc` scan costs ~281 ms, so the achievable poll rate is
~2–5 Hz, the capture floor is about twice the interval, and **sampling density is
anti-correlated with load**: the busier the node, the more pids to scan, the slower the
loop, the wider the blind spot. The interval knob does nothing because the loop never
sleeps.

eBPF removes that ceiling by an asymmetry: **a probe's execution is charged to the task
that triggered the event**, not to the collector. Exhaustive capture is drawn from
nobody's poll budget and has no sampling floor. Only the userspace drain costs the
collector anything.

### `sudo` does not escape the cap — `systemd-run` does

This was measured, not assumed. A `sudo`'d child reports the *calling shell's* cgroup:

```
$ sudo cat /proc/self/cgroup
0::/user.slice/user-20006.slice/session-27.scope     # still capped
```

So a `sudo`-launched collector would silently share the user's one CPU with everything
else that user runs, reintroducing the exact bottleneck it exists to escape. `ebpfm.sh
start` therefore launches via `systemd-run --scope --slice=system.slice`, verified:

```
$ sudo ./ebpfm.sh status
ebpfm: RUNNING on node0 (unit ebpfm-node0.service, pid 106680)
  cgroup: /system.slice/ebpfm-node0.service
```

`ebpfm.sh check` reports the cgroup the collector would land in **and that cgroup's
`cpu.max`**, so the escape is verifiable per node rather than assumed. Where systemd is
absent it falls back to `nohup` with a printed warning.

---

## Concurrency model

**Kernel side: concurrent by construction.** A probe runs on whichever CPU took the
event, in that task's context, with no serialization across CPUs — up to 32 simultaneous
instances on the test node. `BPF_HASH` is protected by per-bucket spinlocks, per-CPU maps
avoid contention entirely, and perf output is per-CPU so there is no contention on the
way out.

**Userspace side: single-threaded, and that is the ceiling.** One thread calls
`perf_buffer_poll`, drains every per-CPU buffer, decodes and JSON-encodes. The GIL means
more threads would not parallelize that. Measured below at **157 µs per decoded event**,
so the drain saturates 1.0 CPU at roughly **6 400 events/s**; it sustained 2 011 events/s
with zero loss.

`perf_lost` (in every `stop` record, and the acceptance gate for anything added) is how
you know if it ever binds. Escalation path if it does, in order:

1. **BPF ring buffer** instead of per-CPU perf buffers — one shared ring, fewer wakeups.
   Verified available on 6.8 (`BPF_MAP_TYPE_RINGBUF` and `bpf_ringbuf_output` both probe
   present).
2. **In-kernel aggregation** with per-CPU maps, so hot events become counters.
3. A **separate writer process** — last, because it adds an IPC hop.

---

## Measured overhead

One-off validation via `sudo ./ebpfm.sh overhead`. **Nothing here runs during normal
collection**; it adds no fields to the schema. Re-measure only when a probe is
added or removed.

Measured on **CloudLab `c220g1` (`node0`), Ubuntu 24.04.4, kernel 6.8.0-138-generic,
32 cores, 125 GB**, 3 timed reps per condition, median.

### Per BPF program

`kernel.bpf_stats_enabled=1` for a **separate** window — the knob itself adds two
`bpf_ktime_get_ns` calls per program run, so enabling it during the timed benchmarks
would inflate them.

| Program | Events | ns/event |
|---|---:|---:|
| `sched_process_exec` | 3 011 | 14 037 |
| `kretprobe inet_csk_accept` | 3 | 12 293 |
| `sched_process_exit` | 3 012 | 11 413 |
| `sched_process_fork` | 3 014 | 9 652 |
| `inet_sock_set_state` | 22 | 2 107 |
| `udp_sendmsg` | 4 | 1 279 |
| `sched_stat_blocked` | 17 200 | 335 |
| `tcp_sendmsg` | 268 | 321 |
| `sys_enter_write` | 597 | 204 |
| `tcp_recvmsg` | 4 323 | 182 |

The expensive three are the process-lifecycle probes, which read many task fields and
(at exec) copy argv. **The hot paths are the cheap ones** — `tcp_recvmsg` at 182 ns and
`sched_stat_blocked` at 335 ns are the probes that fire thousands of times per second.

### Workload delta

`bpf_stats` **off**, so the knob does not inflate it. Benchmarks chosen to stress our own
probes.

| Workload | Off | On | Delta |
|---|---:|---:|---:|
| fork/exec (3 000 execs) | 3 610 ms | 3 756 ms | **+4.0 %** |
| loopback TCP | 118 ms | 122 ms | +3.4 % |
| disk write (`dd` 2 GB) | 3 531 ms | 3 625 ms | +2.7 % |

**Re-measured at SCHEMA_VERSION 5** on the same host — the schema-5 fields add
userspace work on the exec path, so this had to be redone:

| Workload | Off | On | Delta |
|---|---:|---:|---:|
| fork/exec (3 000 execs) | 3 490 ms | 3 813 ms | **+9.3 %** |
| loopback TCP | 121 ms | 119 ms | −1.7 % |
| disk write (`dd` 2 GB) | 3 675 ms | 3 707 ms | +0.9 % |

**The drain is unchanged: 157.8 µs/event at 2 011 events/s**, against v4's 157 µs
and 2 011/s, using 31.7 % of one core (v4: 31.5 %). `perf_lost` zero throughout,
and the per-BPF-program costs measured equal or *lower* than v4 — the kernel side
did not get more expensive.

Read the fork/exec row with care: the v4 and v5 runs share a host but not an
instant, and the *baseline alone* swung 3 490–3 675 ms across runs, so +4.0 %
against +9.3 % is not a tight comparison. The per-event drain cost, which is
identical, is the solid number.

One regression was found this way and fixed rather than shipped: reading the
sandbox state for **every** process whose state was unknown charged roughly
15 000 extra `/proc` operations to this 3 000-exec benchmark — a reproducible
+12 % and 194 µs/event — for processes the actor filter then dropped and never
emitted. The predicate now reads only for a process that will actually be
reported (a tty, or agent ancestry), mirroring what the `cwd` predicate already
does. That alone took 194 µs/event back to 157.8.

**The two measurements corroborate each other**, which is the reason to trust either:
fork + exec + exit at 9 652 + 14 037 + 11 413 ns × 3 000 execs ≈ 102 ms of predicted
kernel-side cost, against +146 ms observed. Same order, right direction.

The event counts are the proof the probes were actually attached for the whole window —
3 011 exec events for a benchmark that runs exactly 3 000 execs. An earlier run of this
harness reported *negative* overhead because the feature probe (~40 s on a cold kernel)
consumed the window and the benchmarks ran against an unattached collector; `overhead`
now warms the feature cache first and polls for the collector's ready line instead of
sleeping a fixed interval.

### Collector cost

| | |
|---|---|
| userspace CPU | 9.46 CPU-s over 30 s = **31.5 % of one core** |
| RSS | 201 MB (BCC retains its clang/LLVM arena after compiling) |
| events decoded | 45 230 core + 15 066 argv + 23 tcp = **2 011/s** |
| per decoded event | **157 µs** of userspace CPU |
| `perf_lost` | all zero |

**Read the percentage as a ceiling and the µs/event as the rate to scale.** The
benchmark drives ~830 exec/s, which is one to two orders of magnitude above a real login
node; the per-event figure is what transfers.

One-time costs, excluded from the above and paid once per kernel: the feature probe
recompiles the program once per block, **~36–41 s**, cached thereafter.

### Record growth

By arithmetic, not measurement: cwd ~60 B, D-state ~60 B, network bytes ~50 B,
attribution ~20 B — roughly **13 % growth** on a ~1.5 KB exit record. The `conn`,
`accept`, `d_stack` and `by_agent_type` additions add records or tick fields rather than
per-record bytes.

Schema 5 adds, again by arithmetic: `ts_epoch` ~20 B, the four `sandbox_*` fields
~80 B, `approval_mode`/`approval_src`/`env_flags` ~40 B set (~30 B of nulls when not),
`session_uuid` ~30 B, `args_len`/`args_truncated` ~30 B — roughly a further **13 %**.
`sandbox_detail` is a short comma-joined string rather than a nested object precisely
to keep this down, since `DailyWriter` flushes on every record and width is direct
write cost on the hottest path.

---

## What it collects

This section is the **eBPF tier**. For the node tier's 36-key snapshot schema see
`collect/login/README.md` and `docs/DATA_COLLECTION.md` — it is the same collector and the
same schema the login poller has always written, which is what keeps every existing
`log/login` consumer working.

`SCHEMA_VERSION = 5`, `collector = "ebpf_marthen_new"`. JSONL at
`$EBPFM_OUTPUT_DIR/<date>/<host>.jsonl`, one record per line.

| `event` | When | Grain |
|---|---|---|
| `meta` | startup | resolved blocks, rejected blocks + compiler reason, sysctl state, kernel, BCC version |
| `exit` | process exit | the main record: identity, CPU, RSS, I/O, D-state, net bytes |
| `tcp` | socket close | lifetime, endpoint, bytes, `connect_ms`, `established` |
| `accept` | inbound connect | `inet_csk_accept` in the accepting task's context |
| `conn` | residency tick | one per **standing external** socket: RTT, retransmits, bytes, idle |
| `residency` | residency tick | one per live agent tree |
| `residency_totals` | residency tick | `by_actor3`, `by_agent_type`, `dropped`, `fork_rate` |
| `submit` | `sbatch`/`salloc`/`srun` | `tool`, `job_id`, `work_dir` |
| `truncated` | SIGTERM | one per live tracked process, with accumulated counters |
| `stop` | shutdown | `records`, `truncated`, `perf_lost`, `perf_seen`, `reason` |

**Every** record carries `ts_epoch` (float, `time.time()` at emit) in addition to `ts`.
`ts` is local, naive and second-granular — fine for reading one host's file, useless for
ordering or binning records from seven hosts against each other. `meta` additionally
carries `tz` and `boot_id`, without which a reader cannot convert `ts` at all or tell a DST
shift from a clock jump.

### Schema 5 additions

| Field | Event | What |
|---|---|---|
| `sandbox`, `sandbox_src`, `sandbox_detail` | identity | **namespace isolation**, from `/proc/<pid>/ns/*` against pid 1's. `null` (never `"unsandboxed"`) when the read was denied |
| `sandbox_ancestry` | identity | `bwrap` / `codex-linux-sandbox` / … — *how* it got there, which is a different question from *what it is*. See below |
| `approval_mode`, `approval_src` | identity | unattended execution, split from confinement. `autonomous` is unchanged |
| `env_flags` | identity | agent env variable **NAMES ONLY**, allow-listed by prefix. Read once per agent root |
| `session_uuid` | identity | survives a collector restart, unlike the pid-based `session_key` |
| `args_len`, `args_truncated` | identity | true pre-clamp argv length. `args_truncated` is `null`, not `false`, when unknown |
| `partition`, `gpus`, `array`, `time_limit_s`, `mem`, `cpus_per_task`, `req_src` | `submit` | the resource request, parsed from the tool's argv |
| `io` | `truncated` | processes alive at SIGTERM carried no I/O at all before |
| `top_procs`, `state_counts`, `tcp_open_ext`, `tcp_open` | `residency` | live per-process rows, the zombie census, standing external connections per tree |
| `tcp_open_ext_by_actor3`, `conn_unattributed`, `conn_unmatched_births` | `residency_totals` | per-class totals, the reconciliation gap, and the `tcp_birth` leak gauge |

### `sandbox` vs `sandbox_ancestry` — read both

`sandbox` is **namespace isolation and nothing else**. Seccomp and capabilities are in
`sandbox_detail`, deliberately: every VS Code/Electron renderer is seccomp-filtered and
`actor3="human-vscode"` is a first-class actor class, so folding seccomp into the boolean
would light up that whole class — and on a stock host `systemd-resolve`, `systemd-logind`,
`rsyslogd` and `polkitd` all report `Seccomp=2` from systemd's `SystemCallFilter=`. A
capability set decides nothing either way: an unprivileged process has `CapEff=0` by
definition, and a process that is uid 0 *inside a user namespace* — the primary case of
interest — reports a **full** set.

`sandbox_ancestry` is free (the ancestry is already resolved) and answers the other half.
**Their disagreement is the signal.** Claude Code's bwrap use is a capability *probe*
(`bwrap --ro-bind / /`), and per `findings/login.md` that probe is exactly what wedges in
D-state on a dead automount. So `ancestry="bwrap"` with `sandbox="unsandboxed"` is a probe;
`ancestry="bwrap"` with `sandbox="sandboxed"` is a real confinement.

A true verdict that still surprises: a systemd unit with `PrivateTmp=` really is in a
private mount namespace, so a hardened daemon reports `sandboxed`/`mntns`. Correct for what
the field means; not agent sandboxing. Read `sandbox_src`.

### The eight data points this adds over the poller

A coverage audit found 40 observable login-node signals: 15 already read, 17 belonging to
the node snapshot by nature, and **8 the collector could read and did not**. All eight:

**1. Working directory** — `cwd`, `cwd_source` on identity; `work_dir` on `submit` (named
for the `groupby("work_dir")` join in five `analyze/` scripts). One
`os.readlink('/proc/<pid>/cwd')` — **readlink only, never stat**. `os.stat`,
`os.path.exists`, `os.path.realpath` and listing the directory all follow the link onto
the target mount and block in uninterruptible sleep on a wedged autofs mount, hanging the
single-threaded collector *during the very incident it exists to observe*. **Inherit-first**:
a fork inherits the directory and tool processes almost never `chdir`, so the parent's
value is the default and a readlink happens only for agent roots, submit tools,
tty-attached processes, or when the parent's is unknown — a handful per second instead of
~95. `EBPFM_CWD_ALWAYS=1` forces a readlink per exec, to validate the assumption.
*Caveats:* a `bwrap` sandbox resolves the string in its own mount namespace; a deleted
directory returns a `" (deleted)"` suffix.

**2. NFS and autofs stall time** — `dstate_wait_s`, `dstate_episodes`, `dstate_max_s`,
`dstate_src` on exit. Two independent blocks, both loaded where possible so they can be
cross-checked: `dstate_task` reads `task->stats.sum_block_runtime` at exit (the kernel's
own cumulative total, no extra probe, 5.19+); `dstate_stat` accumulates
`sched:sched_stat_blocked` per tid, which fires only on unblock — far cheaper than
filtering `sched_switch` — and is the path for older kernels. Prefers `task`.
**Both are inert without `kernel.sched_schedstats=1`**, which `bootstrap` sets.

**3. Network usage** — three parts.
  - **Standing flows.** The four-tuple is stored at `SYN_SENT` as a **join key**:
    netlink names the endpoint but carries **no pid**, and `ss -p` only gets one by
    scanning every `/proc/*/fd`. Each tick, query `NETLINK_SOCK_DIAG` directly
    (`SOCK_DIAG_BY_FAMILY`, `NLM_F_DUMP`, AF_INET + AF_INET6, TCP then UDP), parse
    `inet_diag_msg` + `INET_DIAG_INFO`, join on the tuple. Rows with no map entry still
    emit with uid and no pid. Ladder: netlink → `ss --info --tcp` → `/proc/net/tcp`.
  - **Per-process totals, all protocols.** kretprobes on `tcp_sendmsg`/`tcp_recvmsg`/
    `udp_sendmsg`/`udp_recvmsg` → `net_tx_bytes`, `net_rx_bytes`, `net_calls`. **This is
    what makes QUIC visible.** Unix-socket MCP traffic is excluded by family.
    *Caveat:* `sendfile` and `splice` bypass the socket layer.
  - **Close-record enrichment.** `connect_ms` and `established` come free from the same
    tracepoint: `SYN_SENT`→`ESTABLISHED` is connect latency, and `SYN_SENT`→`CLOSE`
    without `ESTABLISHED` is a refused or timed-out connect.

**4. Wait channel and kernel stack** — `d_stack` on `residency`, plus `d_stack_top`, a
per-tree histogram of blocking frames. For each tracked process in `D` at the tick, read
`/proc/<pid>/stack` as root, top 5 frames. **Reading a kernel stack touches no
filesystem**, so it cannot hang on the mount being diagnosed. Replaces the
`ptrace_may_access`-gated `wchan` for all users and names the stuck server. Capped by
`EBPFM_DSTACK_MAX`.

**5. Live processes at stop** — `event="truncated"` per live tracked process on SIGTERM.
**Drains the D-state and `netbytes` hashes first**, so the record carries real
accumulated counters rather than only what `/proc` shows at that instant — strictly
better than the poller's version. SIGKILL is unrecoverable; a missing `stop` record
already signals a truncated file.

**6. Unattributed user processes** — **`EBPFM_MIN_UID` is the discriminator, not the
cgroup.** The cgroup cannot be trusted: tty-attached human processes sat in
`/system.slice/ssh.service` while another user's VS Code sat in a `user-1000.slice`
session scope, so it depends on how the session was established. A non-system uid with no
agent ancestry and no tty is still a user's process. `attribution` on the identity block
takes `ancestry` | `tty` | `uid`, keeping the three physical `actor3` classes intact for
the Hive partitioning while making the recovered set filterable. Recovers the orphaned
`bwrap` case (reparented to init, no tty) that is the wedge's signature. **Default OFF**
(`EBPFM_MIN_UID=0`) — it is the only item that changes what an existing class *means*,
and enabling it silently would make the human population incomparable with earlier
captures.

**7. Per-agent-type totals** — `by_agent_type` in `residency_totals`, per type
`{trees, n_procs, threads, rss_mb, cpu_s, d_state, users, autonomous}`. Userspace
aggregation of data already held; mirrors the census's `totals_by_type`. Sums to the
agent row of `by_actor3`.

**8. Inbound connections** — netlink from (3) already enumerates established inbound
sockets for free (uid, no pid). Full attribution needs the kretprobe on
`inet_csk_accept`, which returns the new `struct sock *` **in the accepting process's own
context** — the inbound `SYN_RECV`→`ESTABLISHED` transition runs in softirq, where the
current task is meaningless. Emits an `accept` record and seeds the birth map so inbound
connections also get a proper close record.

---

## Design principles

1. **Event-driven for anything that happens; sampled only for anything that persists.**
   Per-process events are exhaustive. Only the residency view samples, which is inherent
   to asking what is alive *now*. Every sampled path is bounded.
2. **Keep userspace syscalls off the per-event path** — the reason cwd inherits by
   default rather than reading `/proc` on every exec.
3. **Never dereference a path read from `/proc`** (see data point 1).
4. **Every kernel read is a probed block.** A kernel that cannot compile one drops *that
   block alone* and records the compiler's reason in `meta.features_rejected`. Dropped
   blocks emit `null`, **never `0`** — an absent measurement must not read as a zero
   measurement.
5. **`perf_lost` is the acceptance gate** for anything added to the tick.

### One caveat worth knowing

`bpf_probe_read` of a `struct sock *` return value must be sign-extended from 32 bits.
`tcp_sendmsg` and friends all return `int`, so the x86-64 ABI only defines `eax` and the
upper 32 bits of `rax` are whatever was there before. Without the cast a 6.7 KB download
first measured as **21 474 843 382 bytes** — exactly 20 GiB plus the true 6 902:

```c
int kretprobe__tcp_sendmsg(struct pt_regs *ctx) { _netb_add((s64)(s32)PT_REGS_RC(ctx), 1); return 0; }
```

---

## `ebpfm.sh` verbs

| Verb | Does |
|---|---|
| `bootstrap` | detect distro and kernel, install missing dependencies, set required sysctls, re-verify |
| `check` | privilege, toolchain, kernel config, sysctls, tracepoints, kprobe symbols, netlink reachability, the cgroup the collector would land in and its `cpu.max`, vendored-file divergence |
| `features` | probe the BPF blocks, print the resolved set as JSON |
| `dump-c` | emit the generated BPF C (for debugging a rejected block) |
| `once [secs]` | foreground capture |
| `start` / `stop` / `status` | daemon with a pidfile, via `systemd-run --scope --slice=system.slice` |
| `overhead` | the one-off measurement above |
| `tail [n]` | last n records of today's file |
| `bundle` | emit the whole folder as one self-extracting `.sh` |

### Bootstrap, on a fresh Ubuntu 24.04

1. Writes a `universe` entry into the existing deb822 stanzas directly — **not** via
   `add-apt-repository`, which would itself need `software-properties-common`. Without
   universe there is **no apt candidate for `python3-bpfcc`**, which is the failure a
   fresh node actually hits.
2. `apt-get install -y python3-bpfcc linux-headers-$(uname -r)`; adds
   `linux-tools-$(uname -r)` only if `bpftool` is missing. Skips `bpfcc-tools` (not
   needed, no candidate here).
3. `sysctl -w kernel.sched_schedstats=1 kernel.task_delayacct=1`, persisted to
   `/etc/sysctl.d/99-ebpfm.conf`. **Two of the eight data points are inert without
   these.**
4. Re-runs `check`, exiting non-zero on any FAIL.
5. Is idempotent. On RHEL/Rocky uses `dnf` with `bcc-tools python3-bcc kernel-devel`.

### Environment

`EBPFM_SANDBOX` (on) · `EBPFM_SANDBOX_ALWAYS` (off; read on every exec, for validation) ·
`EBPFM_ENV_PREFIXES` (`CLAUDE_,CODEX_,CURSOR_,ANTHROPIC_`) · `EBPFM_RESIDENCY_TOPN` (5) ·
`EBPFM_OUTPUT_DIR` · `EBPFM_DURATION` · `EBPFM_RESIDENCY_S` · `EBPFM_FLUSH` ·
`EBPFM_MIN_UID` (data point 6, default off) · `EBPFM_CWD_ALWAYS` ·
`EBPFM_DSTACK_MAX` / `_DEPTH` · `EBPFM_CONN_ALL` / `_UNTRACKED` · `EBPFM_MIN_CPU` /
`_DURATION` · `EBPFM_PERF_PAGES` · `EBPFM_POLL_MS` · `EBPFM_FEATURES` (force a block
set) · `EBPFM_FEATURE_CACHE` · `EBPFM_PROBE_VERBOSE=1` (print the compiler line for a
rejected block).

---

## Tests

```bash
python3 -m unittest discover -s tests -q     # 72 tests, no root, no BPF needed
```

Pure helpers, the BTF member walk (including bpftool's literal `(anon)` rendering for
unnamed union members), each BPF block compiled alone, ancestry, attribution precedence
(ancestry beats tty beats uid), tty **non**-inheritance, cwd inheritance, thread-clone
skip, pid recycling, and the netlink parser against a captured response blob.

## Kernel support

Developed against **6.8** (Ubuntu 24.04) and **6.17**. FASRC login nodes run **4.18**,
where `dstate_task` (needs 5.19+) and `tcp_accept` (needs BTF) will be rejected;
`dstate_stat` is the 4.18 path for data point 2, which is why both blocks exist and are
cross-checked. Run `features` on the target kernel before trusting any field, and
`EBPFM_PROBE_VERBOSE=1` to see why a block was rejected.

## Relationship to the other collectors

This bundle is **login nodes only**. Compute-node collection is out of scope: it stays
with `collect/compute/`, which runs `collect/common/snapshot.py` unchanged, in `full` mode.

| Collector | Status |
|---|---|
| `collect/agents/` (poll tracer + census) | **retired** behind `EBPF_LIVE=1` — the eBPF tier's `exit` records are a declared strict superset of `proc_trace.py`'s, and `residency` / `residency_totals` replace the census at 60 s instead of ~300 s |
| `collect/nettcp/` | **retired** behind `EBPF_LIVE=1` — replaced by `tcp` / `accept` / `conn`. It had no downstream consumers |
| `collect/login/` | **narrowed, not retired.** Its collector is vendored here as `node_snapshot.py` and runs as the node tier; `log/login` keeps its schema and its path |
| `collect/agents_ebpf/` (v1) | superseded by this collector on every field |
| `collect/eBPF_marthen/` (v3) | superseded on the login tier. v3 has only `dstate_task`, which needs kernel 5.19+, so on FASRC's 4.18 login nodes it would emit **no D-state dwell at all** — on the exact nodes whose known failure mode is an autofs/NFS wedge. This collector carries `dstate_stat` as the 4.18 path |
