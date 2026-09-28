# ebpfm — login-node collector, one bash entry point

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
| **ebpf** | **root** | every process exit with its status, argv, `cwd`, per-process I/O for **all** users, D-state dwell, network bytes and the endpoint each went to, TCP records | `$EBPFM_OUTPUT_DIR/<date>/<host>.{snapshot,exits}.jsonl` |

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

**Schema 7 is not in this table.** It adds an entry probe to each send/recv function
and more work to each return probe (see **Schema 7** below), which puts cost on
`tcp_recvmsg`, the hottest of these. It has not been measured yet: run `overhead`
again before relying on it on a busy node.

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
to keep this down: width is direct write cost on the hottest path. (That argument was
sharper still when `DailyWriter` flushed on every record — see **I/O cost** below, which
is no longer true as of schema 6.)

### I/O cost

Two schema-6 changes cut the collector's write path. They are independent and compose:

| | records | `write(2)` | on disk |
|---|---|---|---|
| schema 5 — `tcp` records, flush per record | 53 340 | 53 340 | 33.6 MB |
| schema 6 — folded, flush per record | 20 000 | 20 000 | 12.4 MB |
| schema 6 — folded, flush per poll round | 20 000 | **1 687** | 12.4 MB |

*20 000 process exits, 1 in 6 opening 10 connections (33 000 total), `POLL_MS=100`, at
the 830 exec/s rate of the throughput benchmark above.*

**The fold is the flat win**: one record per process instead of one per connection is
**63 % fewer records and 63 % fewer bytes**, and it does not depend on load.

**The flush policy is the load-proportional one.** `DailyWriter` flushed after every
record through schema 5, so the syscall rate *was* the record rate — unbounded, and
highest exactly when the node is busiest. It now flushes once per poll round
(`EBPFM_FLUSH_MODE`, `poll` by default; `record` restores the old behaviour), which caps
syscalls at `1000/EBPFM_POLL_MS` per second however many records the round produced:

- **idle node** (≤1 record per round): **no change**, 20 000 → 20 000. There is nothing
  to batch, and a round that wrote nothing does not flush at all.
- **busy node** (83 records per round): **20 000 → 1 687, a 11.9×** reduction, because a
  burst of 83 exits costs one write instead of 83.

Together, **53 340 → 1 687 write(2), 31.6×**, at the busy rate.

Note the shape: this is the opposite of the `/proc` poller's defining flaw. The poller's
sampling density was *anti*-correlated with load; this costs nothing extra when idle and
gets cheaper per record exactly as load rises.

What it trades is durability granularity: a record is durable within one poll round rather
than immediately. SIGTERM, SIGINT and the duration stop all run through `close()`, which
flushes, so the exposure is **SIGKILL or power loss losing at most one round** (100 ms).
`meta` is flushed immediately on startup regardless, because `ebpfm.sh status` reads the
file to confirm the collector came up. `stop.flushes` and `stop.flush_mode` report the
real syscall count for any run, so the amplification is measurable rather than assumed.

---

## What it collects

This section is the **eBPF tier**. For the node tier's 36-key snapshot schema see
`collect/login/README.md` and `docs/DATA_COLLECTION.md` — it is the same collector and the
same schema the login poller has always written, which is what keeps every existing
`log/login` consumer working.

`SCHEMA_VERSION = 7`. JSONL, one record per line,
split across **two files per host per day**:

| file | `event` | why separate |
|---|---|---|
| `$EBPFM_OUTPUT_DIR/<date>/<host>.exits.jsonl` | `exit`, `truncated` | the per-process terminal records. Unbounded: the rate is the node's fork rate, highest exactly when the node is busiest, and on a build node this is essentially the whole file |
| `$EBPFM_OUTPUT_DIR/<date>/<host>.snapshot.jsonl` | `meta`, `conn`, `netio`, `submit`, `residency`, `residency_totals`, `stop` | what the node and the run looked like at a moment. Small, bounded by the residency tick, and the part you want when asking "what was running" rather than "what ended" |

`stream_of()` in `ebpf_trace.py` is the single authority on the mapping; an event
kind added without a decision there lands in `snapshot` rather than vanishing.

The split left the **record** schema untouched; it happened within schema 6. Every
record is self-describing and carries its own `host`, so a consumer that globs
`<date>/*.jsonl` sees exactly what the single file used to give it, and a feed
captured before the split reads unchanged. Both files are created when the day
opens, so an empty `.exits.jsonl` means "no exits observed", not "collector never
started".

| `event` | When | Grain |
|---|---|---|
| `meta` | startup | resolved blocks, rejected blocks + compiler reason, sysctl state, kernel, BCC version |
| `exit` | process exit | the main record: identity, CPU, RSS, I/O, D-state, net bytes, **`conns`**, **`net_endpoints`** |
| `conn` | residency tick | one per **standing external** socket: `proto`, RTT, retransmits, bytes, idle |
| `netio` | residency tick; a series process's exit; stop | one per process that moved bytes: bytes per endpoint since its previous `netio` |
| `residency` | residency tick | one per live agent tree |
| `residency_totals` | residency tick | `by_actor3`, `by_agent_type`, `dropped`, `fork_rate`, the endpoint table's counters |
| `submit` | `sbatch`/`salloc`/`srun` | `tool`, `job_id`, `work_dir` |
| `truncated` | SIGTERM | one per live tracked process, with accumulated counters |
| `stop` | shutdown | `records`, `flushes`, `flush_mode`, `truncated`, `perf_lost`, `perf_seen`, `conns_dropped`, `netio_records`, `netpeer_full`, `netpeer_orphans`, `argv_hosts`, `reason` |

**Every** record carries `ts_epoch` (float, `time.time()` at emit) in addition to `ts`.
`ts` is local, naive and second-granular — fine for reading one host's file, useless for
ordering or binning records from seven hosts against each other. `meta` additionally
carries `tz` and `boot_id`, without which a reader cannot convert `ts` at all or tell a DST
shift from a clock jump.

From schema 7, every record also carries **`collector_pid`**, the collector's own pid, so
its own process tree can be filtered out: drop records whose `pid` or `ppid` equals it.
A normal deployment never admits that tree, since it has no tty and no agent ancestry.
But a capture started from a login shell (`sudo ./ebpfm.sh once`) inherits the tty and is
tracked as a human, so the collector's own `truncated` record and its children's exits
(`bpftool`, when it dumps BTF) would otherwise read as someone's work. The `ebpfm.sh` and
`sudo` processes above it have pids of their own and are not covered by the rule. The
field also tells two runs in one day's file apart.

### Schema 7: network bytes by endpoint

`net_tx_bytes`/`net_rx_bytes` said how much a process moved over the network, not
where to. Schema 7 keeps those totals and adds the endpoint behind them, for TCP and
UDP alike, so QUIC and DNS have a destination for the first time. TCP already had
one in `conns.peers`, but only for connections opened after the collector attached,
only the heaviest five, and only once the process exited.

On `exit` and `truncated`, **`net_endpoints`** lists every endpoint of the process's
life:

```json
"net_endpoints": {"n": 2, "in_series": true, "unattributed": null,
  "endpoints": [
    {"proto": "tcp", "addr": "140.82.112.3", "port": 443, "host": "github.com",
     "inbound": false, "external": true, "provider": "github",
     "tx_bytes": 139000, "rx_bytes": 880000, "calls": 412},
    {"proto": "udp", "addr": "10.31.0.5", "port": 53, "host": null, "inbound": false,
     "external": false, "provider": null, "tx_bytes": 80, "rx_bytes": 240, "calls": 4}]}
```

In the snapshot stream, **`netio`** is the time series: one record per process per
residency tick, with the bytes each endpoint moved since that process's previous
`netio` (`interval_s`) and the full identity block. A six-day agent's traffic
therefore has a time axis instead of arriving as one total when it exits.

The rules it keeps:

- **Complete up to a cap, and the rest counted.** Sorted by bytes, up to
  `EBPFM_NET_ENDPOINTS_MAX` (256); past that, an `overflow` object carries the count
  and bytes. A day on madsys-gpu1 peaked at 52 for one process, and that was a VS
  Code server whose inbound connections were still counted one per client port.
- **`null`, not an empty block,** for a process with no network I/O, as `conns`.
- **Loss is stated.** Bytes with no address anywhere (a `recv()` on an unconnected
  UDP socket) are in `unattributed`. Bytes the per-process totals have but the
  endpoint table missed (table full, or the call was in flight at attach) are
  `unaccounted`. `residency_totals` and `stop` carry `netpeer_full`, the calls and
  bytes that found the table full, and `netpeer_orphans`, the entries deleted before
  they reached a record.
- **One consumer rule for the time series.** A process joins the series at its first
  tick with traffic. From then on its exit (or the stop) writes a last `netio`
  (`reason: "exit"`/`"stop"`) for the remainder, and its record says
  `in_series: true`. So the series is every `netio` record, plus the
  `exit`/`truncated` records with `in_series: false` placed at their exit time. That
  second group is exact to within one tick, since a process with traffic before a
  tick would have joined, and `in_series` is what stops a consumer counting the same
  bytes twice.

How the endpoint is found:

- **Where it comes from.** An entry probe on each send/recv function records the
  socket and `msghdr` for the thread; the return probe that already counted the bytes
  reads the peer and adds them to a BPF hash keyed by (pid, protocol, direction,
  address, port). TCP and connected UDP use the socket's peer; `sendto`/`recvfrom` on
  an unconnected UDP socket use `msg->msg_name`.
- **Direction.** Outbound traffic is keyed by the remote port, inbound by the local
  one: a client's port is random, and keying on it would make one entry per
  connection. Direction comes from the TCP birth map when it saw the socket open.
  Otherwise the port range decides (`ip_local_port_range`, compiled in and recorded as
  `meta.config.eph_range`): a remote end on an ephemeral port and a local end off one
  is inbound. That guess covers sockets opened before the collector started, all
  UDP, and inbound TCP where `tcp_accept` does not load (4.18).
- **IPv6 UDP.** `udpv6_prot` has its own `udpv6_sendmsg`/`udpv6_recvmsg`, which the
  schema-6 probes never saw: IPv6 UDP went uncounted, and on a dual-stack socket to
  an IPv4 peer sends were counted but receives were not. `netbytes_udp6` probes both,
  so **`net_tx_bytes`/`net_rx_bytes` cover more from schema 7 on**, which is one
  reason for the bump. The v4-mapped send path, where `udpv6_sendmsg` calls
  `udp_sendmsg` on the same socket, is counted once.
- **Read, never deleted while alive.** Userspace reads the whole table each tick and
  before it writes a networked process's record. A live process's entries are never
  deleted, because a delete would race the kernel's increments and 4.18 has no atomic
  read-and-delete; an exited process's are.
- **Names come from argv, never from traffic.** `host` is set only when the process's
  own command line names the endpoint (see below). Nothing parses DNS answers, TLS or
  HTTP. `provider` is still the existing CIDR bucket, and behind a proxy the endpoint is
  the proxy.

**`host` on an endpoint** is a name from the process's own command line: every URL's
host (`curl https://…`, `pip --index-url=…`, `git clone https://…`), and the destination
of `ssh`/`sftp`/`scp`/`rsync`, which covers the `ssh` that git runs for an ssh remote.
Those names are resolved on a background thread and cached, and never on the event path.
An endpoint takes a name only when its address is among the addresses the name resolves
to, so `host` is null rather than a guess when:

- the name isn't in argv, as with an agent's own API calls;
- the lookup hasn't finished yet;
- the name answered with other addresses, as a CDN that rotates them can.

The collector takes only the hostname, never a URL's credentials. It skips address
literals and `localhost`, and never treats a file name or an email address as a
hostname. It looks names up only for processes it reports anyway. `stop.argv_hosts`
counts lookups resolved, failed, and dropped because the queue was full; each dropped
one may be a missing `host`. `EBPFM_ARGV_HOSTS=0` turns this off.

`conn` records gain **`proto`**. The netlink dump asks for TCP and UDP, and a
connected UDP socket reports `ESTABLISHED`, so a DHCP client's socket
(128.103.1.210:67 on madsys-gpu1) read as a standing TCP connection with no pid,
1 439 times a day. A UDP row also no longer joins the TCP-only birth map.

The envelope gains **`collector_pid`** (see above), for filtering the collector's own
process tree out, and loses **`source`** and **`collector`**. Those were the constants
`"ebpf"` and `"ebpf_marthen_new"` on every line, and nothing read them. Pre-7 captures still
carry them.

**`args` is no longer capped by default** (`EBPFM_ARGS_MAXLEN=0`; it was 2048). What limits
it now is how much argv the collector can read: the in-kernel read at exec stops at
`EBPFM_ARGV_KMAX` (4096 bytes), while a `/proc` read, used for processes seeded at
startup, is whole. `args_truncated` now also reports the in-kernel cut. Before, it
compared only against the emit cap, so with no cap a clipped command line would have
read as complete. The cost is width: identity, and so `args`, rides on every record
about a process, including each tick's `residency`, `conn` and `netio`, so one long
command line is repeated all day. Set a cap to bound that.

**`command` replaces `comm`.** The kernel's comm stops at 15 characters, so
`codex-linux-sandbox` read as `codex-linux-san` and a VS Code CLI as `code-2242ebbb54`.
`command` is the comm, completed from argv when it hit that limit: the first token
whose basename extends it, which is argv[0] for a binary and the script path for a
script run through its interpreter. On one day of madsys-gpu1 data, most of the 115
records with a 15-character comm completed this way. What exists nowhere longer stays
as the kernel reported it: a thread name set with `prctl`, or the loader run with no
arguments. Keys named after it follow: `by_command` on `residency`, `top_procs[].command`,
`dropped.by_command` and `stop.conns_dropped_commands`. `submit.tool` keeps its name and
takes the whole command. One latent bug goes with it. `SANDBOX_COMMS` lists
`codex-linux-sandbox`, and matched against the cut comm that entry could never fire;
it is now matched on `command`.

**`cwd_source` is gone.** Whether `cwd` was read or inherited was not worth a field on
every record once the value itself is there.

**`env_flags` is gone, and `/proc/<pid>/environ` is never opened.** It kept only variable
names, but the file holds the values too, API keys among them, for every agent root on
the node. `approval_mode` now comes from argv alone, so a bypass set through the
environment or a config file rather than a flag is not seen (`approval_src` is `argv` or
null).

The feature cache now records the block list it was probed against and is ignored
when that list changes. Without that, a host that ran an older collector would load
its cached set and never try the new blocks.

### Schema 6: connections fold into the process

`tcp` (socket close) and `accept` (inbound connect) were one record per connection.
They no longer exist. The same connections now reach the feed as a **`conns` block on the
owning process's `exit` record** — and on its `truncated` record, for a process still
alive at stop.

```json
"conns": {"n": 21, "accepted": 0, "established": 20, "failed": 1, "external": 21,
          "rx_bytes": 883110, "tx_bytes": 140022, "conn_s_total": 41.3,
          "connect_ms_p50": 31.2, "connect_ms_max": 88.0,
          "providers": {"anthropic": 20}, "peers_total": 2,
          "peers": [{"daddr": "104.18.0.1", "dport": 443, "n": 20, "external": true,
                     "provider": "anthropic", "inbound": 0, "failed": 0,
                     "rx_bytes": 880000, "tx_bytes": 139000}]}
```

Why the grain is better, not just cheaper: an agent's traffic is overwhelmingly **repeat
calls to a handful of hosts**, so twenty sequential requests to one API endpoint were
twenty near-identical records and are now one `peers` entry with `n: 20`. `peers` keeps
the heaviest `EBPFM_CONNS_TOPN` (default 5) by bytes; `peers_total` is the true distinct
count, the endpoint map is capped at 256 keys and anything past that is counted in
`peers_overflow` rather than silently merged.

Four rules it keeps:

- **`conns` is `null`, not a zeroed block, for a process that opened nothing.** Most
  processes open nothing, and the block is ~200 B against the width argument below.
  Whether the TCP blocks loaded at all is a property of the capture and stays in
  `meta.features` — it is not a per-process fact.
- **`rx_bytes`/`tx_bytes` are `null`, never `0`, when no connection contributed a real
  figure** (the `tcp_basic` variant cannot read byte counters). Design principle 4.
- **`conn_s_total` is summed connection lifetimes, not wall time.** Connections overlap,
  so it can exceed the process's own `duration_s`. It is not a rate base.
- **Loss is counted, never silent.** A process that folded connections but never produced
  a record — unattributable, or its exit event was lost — is counted at GC into
  `stop.conns_dropped` / `conns_dropped_commands`. That pair is the acceptance gate for this
  change.

#### The exit record is now held for 250 ms

This is forced by kernel ordering, not by batching. `do_exit()` runs
`trace_sched_process_exit()` **before** `exit_files()`, so the `TCP_CLOSE` events for the
sockets a process still held are submitted *after* the exit event that describes it.
Writing the exit record inline would fold in only the connections that happened to close
during the process's life and miss every one it was still holding — for an agent, nearly
all of them.

So `on_exit` stages the record and `drain_exits()` writes it one poll round later
(`EBPFM_EXIT_HOLD_MS`, default 250; `0` restores the old inline emit).

Two consequences worth knowing:

- **File order, not time order, is what shifts.** `ts_epoch` is stamped when the record is
  built, so a consumer that sorts on it — which is what it is for — sees nothing change.
  A consumer that assumed append order *was* time order will now see an `exit` line after
  a `residency` line stamped later.
- **Nothing is at risk at stop.** `drain_exits(final=True)` runs before `flush_live()`, so
  a pending record is still written as an `exit` and cannot also appear as a contradictory
  `truncated`.

The hold also moves the three "too cheap to keep" filters (`EBPFM_MIN_DURATION`,
`EBPFM_MIN_CPU`, the `sleep` timer filter) from decision to *application* time: a 40 ms
`curl` is below `MIN_DURATION`, and dropping it at exit would throw away the connection
that made it worth keeping. **A process that talked to the network is never a no-op.**

### Schema 5 additions

| Field | Event | What |
|---|---|---|
| `sandbox`, `sandbox_src`, `sandbox_detail` | identity | **namespace isolation**, from `/proc/<pid>/ns/*` against pid 1's. `null` (never `"unsandboxed"`) when the read was denied |
| `sandbox_ancestry` | identity | `bwrap` / `codex-linux-sandbox` / … — *how* it got there, which is a different question from *what it is*. See below |
| `approval_mode`, `approval_src` | identity | unattended execution, split from confinement. `autonomous` is unchanged |
| `env_flags` | identity | agent env variable names from `/proc/<pid>/environ`. **Gone in schema 7:** the collector no longer reads another process's environment |
| `session_uuid` | identity | survives a collector restart, unlike the pid-based `session_key` |
| `args_len`, `args_truncated` | identity | true pre-clamp argv length. `args_truncated` is `null`, not `false`, when unknown, and `true` when the in-kernel read (`EBPFM_ARGV_KMAX`) or an emit cap (`EBPFM_ARGS_MAXLEN`) cut it |
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

**1. Working directory** — `cwd` on identity; `work_dir` on `submit` (named
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
    Schema 7 adds `udpv6_sendmsg`/`udpv6_recvmsg` and the endpoint of every call:
    see **Schema 7** above.
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
current task is meaningless. Folds into the accepting process's `conns` block (as of
schema 6; it emitted an `accept` record through schema 5) and seeds the birth map so
inbound connections also get a proper close.

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

`EBPFM_ARGS_MAXLEN` (0 = no cap on emitted `args`) · `EBPFM_ARGV_KMAX` (4096; bytes of
argv read in-kernel at exec, the real limit when uncapped) ·
`EBPFM_SANDBOX` (on) · `EBPFM_SANDBOX_ALWAYS` (off; read on every exec, for validation) ·
`EBPFM_RESIDENCY_TOPN` (5) ·
`EBPFM_OUTPUT_DIR` · `EBPFM_DURATION` · `EBPFM_RESIDENCY_S` · `EBPFM_FLUSH` ·
`EBPFM_MIN_UID` (data point 6, default off) · `EBPFM_CWD_ALWAYS` ·
`EBPFM_DSTACK_MAX` / `_DEPTH` · `EBPFM_CONN_ALL` / `_UNTRACKED` · `EBPFM_TCP_ALL` ·
`EBPFM_CONNS_TOPN` (5) · `EBPFM_EXIT_HOLD_MS` (250; `0` = emit exit records inline and
lose the connections that close after the exit event) ·
`EBPFM_NETPEER_MAX` (16384; the kernel endpoint table) · `EBPFM_NET_ENDPOINTS_MAX`
(256; endpoints listed per record) · `EBPFM_NETIO` (on; `0` = no `netio` series, keep
`net_endpoints`) · `EBPFM_ARGV_HOSTS` (on; `0` = no `host` names from argv) ·
`EBPFM_FLUSH_MODE` (`poll`; `record` = flush every record, the schema-5 behaviour —
note this is NOT `EBPFM_FLUSH`, which is about flushing live *processes* at stop) ·
`EBPFM_MIN_CPU` /
`_DURATION` · `EBPFM_PERF_PAGES` · `EBPFM_POLL_MS` · `EBPFM_FEATURES` (force a block
set) · `EBPFM_FEATURE_CACHE` · `EBPFM_PROBE_VERBOSE=1` (print the compiler line for a
rejected block).

---

## Tests

```bash
python3 -m unittest discover -s tests -q     # 278 tests, no root, no BPF needed
```

Pure helpers, the BTF member walk (including bpftool's literal `(anon)` rendering for
unnamed union members), each BPF block compiled alone, ancestry, attribution precedence
(ancestry beats tty beats uid), tty **non**-inheritance, cwd inheritance, thread-clone
skip, pid recycling, and the netlink parser against a captured response blob.

The schema-7 network section is also **compiled and run**: `build_net_c()` is built with
the host C compiler against `tests/bpf_mock.h` (map semantics, `bpf_probe_read` as a
copy), and its probes are driven with fake sockets laid out at the BTF offsets. That
covers the entry/return hand-off, the v4-mapped double count, byte order, direction
and a full table. Those tests are skipped without a C compiler. It is not BPF:
whether the program loads and attaches is still only shown by `features` on a real
kernel, and the `netpeer_hdr` variant, which needs kernel headers, is only checked
as text.

## Kernel support

Developed against **6.8** (Ubuntu 24.04) and **6.17**. FASRC login nodes run **4.18**,
where `dstate_task` (needs 5.19+), `tcp_accept` and `netpeer_btf` (need BTF) will be
rejected; `dstate_stat` is the 4.18 path for data point 2, which is why both blocks
exist and are cross-checked, and `netpeer_hdr` is the 4.18 path for the endpoint table. Run `features` on the target kernel before trusting any field, and
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
