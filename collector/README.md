# ebpfm — login-node collector, one bash entry point

A **standalone** collector for FASRC-style login nodes. Everything it needs is in this
folder, with no `../` imports, so it runs anywhere you can copy it. You can also hand
over a single self-extracting file built by `ebpfm.sh bundle`. See [VENDORED.md](VENDORED.md) for the four
copied modules and the sync rule, and [COVERAGE.md](COVERAGE.md) for the field-by-field
crosswalk of what each tier collects and what replaced what.

## Two tiers, chosen by privilege

Only half of what we need from a login node comes from kernel events, so only half of
the collector needs root:

| Tier | Privilege | What | Output |
|---|---|---|---|
| **node** | **unprivileged** | node health: load, memory, filesystem space, NFS per-mount RTT, interface counters, the D-state census, the process tree, per-user cgroup totals, boot time | `$EBPFM_NODE_OUTPUT_DIR/<date>/<host>.jsonl` |
| **ebpf** | **root** | every process exit with its status, argv, `cwd`, per-process I/O for **all** users, D-state dwell, network bytes and the endpoint each went to, TCP records | `$EBPFM_OUTPUT_DIR/<date>/<host>.{snapshot,exits}.jsonl` |

**eBPF cannot produce load average, memory totals, filesystem space or interface
counters**. It is also not a process census: only *tracked* processes are emitted (see
COVERAGE.md §7). So the node tier is not a fallback. It collects the other half.

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

`EBPFM_TIER=node|ebpf|all` overrides the default, which is based on privilege. The node
tier's collection mode depends on which tiers are running. When the eBPF tier is running,
the node tier drops the four per-process sections that the eBPF tier collects properly
(`SNAPSHOT_MODE=node`). Otherwise it keeps everything, because dropping those sections on
a host with no eBPF tier would lose them entirely. **Losing root on one node costs that
node its per-process tier and nothing else.** The effect is per host, not fleet-wide.

### Supervision

`start` uses a *transient* `systemd-run` scope. That is fine for a one-off, but the scope
does not survive a reboot and has no restart policy. For a fleet, `install-unit` writes
`ebpfm.service` (`Restart=on-failure`, `Slice=system.slice`). The repo keepalive
(`run.sh`) deliberately does **not** manage the eBPF tier, because `run.sh` is
unprivileged and cannot `sudo` across the fleet. Instead, systemd supervises the eBPF
tier, and `collect/healthcheck.py` notices when it dies.

The unit uses `Restart=on-failure`, never `always`, and nothing restarts the collector
periodically. SIGTERM makes it flush every live tracked process as `truncated`, so a
restart every cycle would corrupt the trace. This is the same rule `run.sh` documents
for the pollers.

---

## Why eBPF, precisely

The unprivileged `/proc` poller that this collector replaces is **quota-bound, not
design-bound**. Every process a user runs on a FASRC login node shares one cgroup v2
slice capped at `cpu.max = 1.0` CPU. One
`/proc` scan costs ~281 ms, so the achievable poll rate is ~2–5 Hz and the capture floor
is about twice the interval. Worse, **sampling density is anti-correlated with load**.
The busier the node, the more pids there are to scan, so the loop runs slower and the
blind spot gets wider. The interval knob does nothing because the loop never sleeps.

eBPF removes that ceiling because of how its cost is accounted: **a probe's execution is
charged to the task that triggered the event**, not to the collector. Exhaustive capture
therefore comes out of nobody's poll budget and has no sampling floor. Only the userspace
drain costs the collector anything.

### `sudo` does not escape the cap — `systemd-run` does

We measured this. A child started with `sudo` reports the *calling shell's* cgroup:

```
$ sudo cat /proc/self/cgroup
0::/user.slice/user-20006.slice/session-27.scope     # still capped
```

A collector launched with `sudo` would therefore silently share the user's one CPU with
everything else that user runs, which brings back the exact bottleneck it exists to
escape. `ebpfm.sh
start` therefore launches via `systemd-run --scope --slice=system.slice`, and we verified
the result:

```
$ sudo ./ebpfm.sh status
ebpfm: RUNNING on node0 (unit ebpfm-node0.service, pid 106680)
  cgroup: /system.slice/ebpfm-node0.service
```

`ebpfm.sh check` reports the cgroup the collector would land in **and that cgroup's
`cpu.max`**, so you can verify the escape on each node instead of assuming it. Where
systemd is absent, `start` falls back to `nohup` and prints a warning.

---

## Concurrency model

**Kernel side: concurrent by construction.** A probe runs on whichever CPU took the
event, in the context of the task that triggered it, with no serialization across CPUs.
On the test node that means up to 32 simultaneous instances. `BPF_HASH` is protected by
per-bucket spinlocks, per-CPU maps avoid contention entirely, and perf output is per-CPU,
so there is no contention on the way out either.

**Userspace side: single-threaded, and that is the ceiling.** One thread calls
`perf_buffer_poll`, drains every per-CPU buffer, and decodes and JSON-encodes each event.
Because of the GIL, adding threads would not parallelize that work. The drain costs
**157 µs per decoded event** (measured below), so it saturates 1.0 CPU at roughly
**6 400 events/s**. In testing it sustained 2 011 events/s with zero loss.

`perf_lost` tells you if that ceiling is ever reached. It appears in every `stop` record
and is the acceptance gate for anything added. If the ceiling is reached, escalate in
this order:

1. **BPF ring buffer** instead of per-CPU perf buffers: one shared ring, fewer wakeups.
   Verified available on 6.8 (`BPF_MAP_TYPE_RINGBUF` and `bpf_ringbuf_output` both probe
   present).
2. **In-kernel aggregation** with per-CPU maps, so hot events become counters.
3. A **separate writer process**. This comes last because it adds an IPC hop.

---

## Measured overhead

This is a one-off validation, run with `sudo ./ebpfm.sh overhead`. **Nothing here runs
during normal collection**, and it adds no fields to the schema. Re-measure only when a
probe is added or removed.

Measured on **CloudLab `c220g1` (`node0`), Ubuntu 24.04.4, kernel 6.8.0-138-generic,
32 cores, 125 GB**, 3 timed reps per condition, median.

### Per BPF program

These numbers come from a **separate** window with `kernel.bpf_stats_enabled=1`. The
knob itself adds two `bpf_ktime_get_ns` calls per program run, so enabling it during the
timed benchmarks would inflate their results.

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
`tcp_recvmsg`, the hottest of these. That cost has not been measured yet, so run
`overhead` again before relying on schema 7 on a busy node.

The three most expensive programs are the process-lifecycle probes, which read many task
fields and (at exec) copy argv. **The hot paths are the cheap ones.** `tcp_recvmsg` at
182 ns and `sched_stat_blocked` at 335 ns are the probes that fire thousands of times per
second.

### Workload delta

These runs had `bpf_stats` **off**, so the knob does not inflate them. The benchmarks were
chosen to stress our own probes.

| Workload | Off | On | Delta |
|---|---:|---:|---:|
| fork/exec (3 000 execs) | 3 610 ms | 3 756 ms | **+4.0 %** |
| loopback TCP | 118 ms | 122 ms | +3.4 % |
| disk write (`dd` 2 GB) | 3 531 ms | 3 625 ms | +2.7 % |

**Re-measured at SCHEMA_VERSION 5** on the same host. The schema-5 fields add userspace
work on the exec path, so the measurement had to be redone:

| Workload | Off | On | Delta |
|---|---:|---:|---:|
| fork/exec (3 000 execs) | 3 490 ms | 3 813 ms | **+9.3 %** |
| loopback TCP | 121 ms | 119 ms | −1.7 % |
| disk write (`dd` 2 GB) | 3 675 ms | 3 707 ms | +0.9 % |

**The drain is unchanged: 157.8 µs/event at 2 011 events/s**, compared with v4's
157 µs and 2 011/s, using 31.7 % of one core (v4: 31.5 %). `perf_lost` stayed at zero
throughout, and the per-BPF-program costs measured equal to or *lower* than v4, so the
kernel side did not get more expensive.

Read the fork/exec row with care. The v4 and v5 runs used the same host but not at the
same time, and the *baseline alone* swung between 3 490 and 3 675 ms across runs, so
+4.0 % against +9.3 % is not a tight comparison. The per-event drain cost, which is
identical, is the reliable number.

This re-measurement caught one regression, which was fixed before it shipped. The code
read the sandbox state for **every** process whose state was unknown. That charged
roughly 15 000 extra `/proc` operations to this 3 000-exec benchmark (a reproducible
+12 % and 194 µs/event), all for processes that the actor filter then dropped and never
emitted. The predicate now reads sandbox state only for a process that will actually be
reported (one with a tty or agent ancestry), which matches what the `cwd` predicate
already does. That change alone brought the cost from 194 µs/event back to 157.8.

**The two measurements corroborate each other**, which is why either can be trusted.
Fork + exec + exit at 9 652 + 14 037 + 11 413 ns × 3 000 execs predicts ≈ 102 ms of
kernel-side cost, against +146 ms observed. That is the same order of magnitude, and the
difference goes in the expected direction.

The event counts prove that the probes were attached for the whole window: there were
3 011 exec events for a benchmark that runs exactly 3 000 execs. An earlier run of this
harness reported *negative* overhead. The feature probe (~40 s on a cold kernel) had used
up the window, so the benchmarks ran against a collector that was not yet attached.
`overhead` now warms the feature cache first, and it polls for the collector's ready line
instead of sleeping for a fixed interval.

### Collector cost

| | |
|---|---|
| userspace CPU | 9.46 CPU-s over 30 s = **31.5 % of one core** |
| RSS | 201 MB (BCC retains its clang/LLVM arena after compiling) |
| events decoded | 45 230 core + 15 066 argv + 23 tcp = **2 011/s** |
| per decoded event | **157 µs** of userspace CPU |
| `perf_lost` | all zero |

**Read the percentage as a ceiling, and use the µs/event figure to scale.** The
benchmark drives ~830 exec/s, which is one to two orders of magnitude above a real login
node, so the per-event figure is the one that carries over.

One-time costs are excluded from the above and paid once per kernel. The feature probe
recompiles the program once per block, which takes **~36–41 s**, and the result is
cached after that.

### Record growth

These figures are calculated, not measured: cwd ~60 B, D-state ~60 B, network bytes
~50 B, attribution ~20 B. That is roughly **13 % growth** on a ~1.5 KB exit record. The
`conn`, `accept`, `d_stack` and `by_agent_type` additions add records or tick fields
rather than bytes per record.

Schema 5 adds, again by calculation: `ts_epoch` ~20 B, the four `sandbox_*` fields
~80 B, `approval_mode`/`approval_src`/`env_flags` ~40 B when set (~30 B of nulls when
not), `session_uuid` ~30 B, and `args_len`/`args_truncated` ~30 B. That is roughly a
further **13 %**. `sandbox_detail` is a short comma-joined string rather than a nested
object specifically to keep this growth down, because record width is a direct write
cost on the hottest path. (That argument was even stronger when `DailyWriter` flushed on
every record, which is no longer the case as of schema 6. See **I/O cost** below.)

### I/O cost

Two schema-6 changes cut the collector's write path. They are independent, and their
effects compound:

| | records | `write(2)` | on disk |
|---|---|---|---|
| schema 5 — `tcp` records, flush per record | 53 340 | 53 340 | 33.6 MB |
| schema 6 — folded, flush per record | 20 000 | 20 000 | 12.4 MB |
| schema 6 — folded, flush per poll round | 20 000 | **1 687** | 12.4 MB |

*20 000 process exits, 1 in 6 opening 10 connections (33 000 total), `POLL_MS=100`, at
the 830 exec/s rate of the throughput benchmark above.*

**The fold is a flat win.** Writing one record per process instead of one per connection
gives **63 % fewer records and 63 % fewer bytes**, regardless of load.

**The flush policy is the change whose benefit grows with load.** Through schema 5,
`DailyWriter` flushed after every record, so the syscall rate *was* the record rate. That
rate is unbounded, and it is highest exactly when the node is busiest. `DailyWriter` now
flushes once per poll round (`EBPFM_FLUSH_MODE`, `poll` by default; `record` restores the
old behaviour). This caps syscalls at `1000/EBPFM_POLL_MS` per second, however many
records the round produced:

- **idle node** (≤1 record per round): **no change**, 20 000 → 20 000. There is nothing
  to batch, and a round that wrote nothing does not flush at all.
- **busy node** (83 records per round): **20 000 → 1 687, a 11.9×** reduction, because a
  burst of 83 exits costs one write instead of 83.

Together, **53 340 → 1 687 write(2), 31.6×**, at the busy rate.

This is the opposite of the `/proc` poller's defining flaw. The poller's sampling density
was *anti*-correlated with load. The new flush policy costs nothing extra when the node is
idle, and gets cheaper per record exactly as load rises.

The trade-off is durability granularity: a record becomes durable within one poll round
rather than immediately. SIGTERM, SIGINT and the duration stop all go through `close()`,
which flushes, so the only exposure is **SIGKILL or power loss losing at most one round**
(100 ms). `meta` is always flushed immediately on startup, because `ebpfm.sh status` reads
the file to confirm the collector came up. `stop.flushes` and `stop.flush_mode` report the
real flush count for any run, so you can measure the write amplification instead of
assuming it.

`stop.flushes` counts **stream flushes**, and the count is exact. The stop record is built
before it is written, so it includes the flushes that are still to come: the one that
writes the stop record itself, and the ones `close()` does for any other stream left dirty
(the `truncated` records). The old `+ 1` missed the latter. Each stream flush is **one
`write(2)`**. JSONL is written with `os.write` from a buffer. The bytes are identical to
what the text-mode handle wrote, but there is one syscall per round, whereas the handle
split a round larger than 8 KiB into several. So `stop.output.jsonl.writes` is the real
syscall count (up to the stop record).

We measured this through `DailyWriter` on one host's synthetic demo day: 11 742 eBPF
records, flushed per 0.1 s of `ts_epoch`, at 1.6 records per flush (so nearly one syscall
per record). Writing JSONL took **13.5 µs per record** on Python 3.13 and 18.7 on 3.8, for
18.78 MB on disk in 7 279 `write(2)`s. That cost is per record *written*, and records are
only a fraction of the events decoded: every fork, exec and exit is an event, but only the
end of a tracked process is a record. So the write cost is small compared with the 157 µs
per decoded event above. On a busy node, each syscall is also spread over more records.

---

## What it collects

This section covers the **eBPF tier**. For the node tier's 36-key snapshot schema, see
`collect/login/README.md` and `docs/DATA_COLLECTION.md`. The node tier is the same
collector, writing the same schema, that the login poller has always written, so every
existing `log/login` consumer keeps working.

`SCHEMA_VERSION = 8`. JSONL, one record per line,
split across **two files per host per day**:

| file | `event` | why separate |
|---|---|---|
| `$EBPFM_OUTPUT_DIR/<date>/<host>.exits.jsonl` | `exit`, `truncated` | the per-process terminal records. Unbounded: the rate is the node's fork rate, which is highest exactly when the node is busiest. On a build node these records make up essentially the whole output |
| `$EBPFM_OUTPUT_DIR/<date>/<host>.snapshot.jsonl` | `meta`, `conn`, `netio`, `submit`, `residency`, `residency_totals`, `stop` | what the node and the run looked like at a given moment. Small, bounded by the residency tick, and the file you want when asking "what was running" rather than "what ended" |

`stream_of()` in `ebpf_trace.py` is the single authority on which event goes to which
file. An event kind added without a decision there lands in `snapshot` rather than
vanishing.

The split happened within schema 6 and left the **record** schema untouched. Every
record is self-describing and carries its own `host`, so a consumer that globs
`<date>/*.jsonl` sees exactly what the single file used to give it, and a feed
captured before the split reads unchanged. Both files are created when the day
opens, so an empty `.exits.jsonl` means "no exits observed", not "collector never
started".

| `event` | When | Grain |
|---|---|---|
| `meta` | startup | resolved blocks, rejected blocks + compiler reason, sysctl state, kernel, BCC version, `output` (`{"formats": ["jsonl"], "ebr": null}`: the format written) |
| `exit` | process exit | the main record: identity, CPU, RSS, I/O, D-state, net bytes, **`conns`**, **`net_endpoints`** |
| `conn` | residency tick | one per **standing external** socket: `proto`, RTT, retransmits, bytes, idle |
| `netio` | residency tick; a series process's exit; stop | one per process that moved bytes: bytes per endpoint since its previous `netio` |
| `residency` | residency tick | one per live agent tree |
| `residency_totals` | residency tick | `by_actor3`, `by_agent_type`, `dropped`, `fork_rate`, the endpoint table's counters, and (schema 8) the run's `perf_seen`/`perf_lost`, `handler_errors`, `proc_entries`, `proc_vanished`, `thread_acc`, `netpeer_kernel` |
| `submit` | `sbatch`/`salloc`/`srun` | `tool`, `job_id`, `work_dir`, `captured_tail` (masked; null for `--pty`) |
| `truncated` | SIGTERM | one per live tracked process, with accumulated counters |
| `stop` | shutdown | `records`, `flushes` (exact), `flush_mode`, `truncated` (null when the flush did not finish), `perf_lost`, `perf_seen`, `handler_errors`, `proc_vanished`, `conns_dropped`, `netio_records`, `netpeer_full`, `netpeer_orphans`, `argv_hosts`, `reason`, `output` (`jsonl`: records, bytes, `write(2)`s and errors up to this record; `ebr`: null) |

**Every** record carries `ts_epoch` (float, `time.time()` at emit) in addition to `ts`.
`ts` is local, naive and has one-second granularity. That is fine for reading one host's
file, but useless for ordering or binning records from seven hosts against each other.
`meta` also carries `tz` and `boot_id`. Without them a reader cannot convert `ts` at all,
or tell a DST shift from a clock jump.

From schema 7, every record also carries **`collector_pid`**, the collector's own pid, so
you can filter out the collector's own process tree: drop records whose `pid` or `ppid`
equals `collector_pid`. A normal deployment never admits that tree, since it has no tty
and no agent ancestry. But a capture started from a login shell (`sudo ./ebpfm.sh once`)
inherits the tty and is tracked as a human. Without the filter, the collector's own
`truncated` record and its children's exits (`bpftool`, when it dumps BTF) would look like
someone's work. The `ebpfm.sh` and `sudo` processes above the collector have their own
pids and are not covered by this rule. The field also lets you tell apart two runs in one
day's file.

### Agent tags: the program, not a mention

This change was made within schema 8, with no version bump. `classify()`
(`lib/agent_classify.py`, and its copy in `node_snapshot.py` behind `proc_tree.agents`)
now reads the program a command line runs, not every word of it. It used to match its
patterns anywhere, so at a human's prompt `grep claude
notes.txt`, `vim ~/.claude/CLAUDE.md`, `cat ~/.claude/settings.json`, `git commit -m "ask
claude about this"`, `git clone https://github.com/openai/codex` and `pip install
claude-agent-sdk` each became an agent root of its own (`actor` `agent` by ancestry,
`is_agent`, `depth` 0, and its own session). The dashboard takes `actor3` as given, so it
counted each of these as an agent session. The same happened to a tmux or screen session
named `claude` or `codex`, along with every command in its panes, because the tmux or
screen server keeps the client's argv.

The program is one of these:

- argv[0], identified by its name (`claude`, `codex`, `codex-linux-sandbox`, `bwrap`) or by
  where it is installed (`~/.claude/remote/…`, `~/.local/share/claude/versions/…`,
  `~/.codex/…`, an IDE extension, `~/.vscode-server/…`);
- for `env`, `exec`, `nohup`, `setsid`, `nice`, `timeout` and any `VAR=value`, the word
  they run (skipping past the wrapper);
- a package runner's package: `npx`, `npm exec`, `pnpm dlx`, `yarn dlx`, `bunx`, `bun x`;
- an interpreter's script (`node`, `bun`, `deno`, `python`, a shell): `node
  …/@anthropic-ai/claude-code/cli.js`, `/usr/bin/env node …/bin/codex`;
- the first word of a shell's `-c` command (`sh -c exec claude …`);
- for a `bwrap`, every word, because a sandbox is tagged by what it confines (as before).

Besides the program, the classifier also counts the artifacts that only an agent's runtime
writes, in the places it writes them. These are Claude Code's tool-call shell (a shell
sourcing `~/.claude/shell-snapshots/…`), and Cursor's and Windsurf's `CURSOR_SANDBOX` and
`WINDSURF_STATE` when set in the environment a program runs with
(`CURSOR_SANDBOX=1 bash …`, `env CURSOR_SANDBOX=1 …`, a bwrap's
`--setenv CURSOR_SANDBOX 1`). A word that merely names or prints one of them never counts (`grep -r
CURSOR_SANDBOX .`, `echo CURSOR_SANDBOX=1`). The names are case-sensitive, as variable
names are. Windsurf's `windsurf-terminal` counts as a program. `ClassifyTest` pins both lists, and checks that the node tier's copy agrees.

What changes:

- **A command that only mentions an agent belongs to whoever ran it.** At a person's
  prompt it is that person's (`actor` by tty), not an agent root. Inside an agent tree
  nothing changes: an untagged child joins the tree just as a tagged one did. It no longer
  gets its own sandbox and cwd read at exec. It inherits them, as any untagged child does.
- **Some agents are now tagged that were not before**: Claude's native binary started by
  its versions path with nothing else naming Claude (`~/.local/share/claude/versions/2.1.282
  --session-id …`), Claude Code's npm launcher by path (`node
  …/@anthropic-ai/claude-code/cli.js`), and `npx @anthropic-ai/claude-code`.
- **Under `~/.claude/`, only Claude Code's own installs count as Claude**: `~/.claude/local/…`,
  `~/.claude/remote/…` and its CLI's `cli.js`. A hook or status line script there, run
  by hand, belongs to the user, and a program in a project's `.claude/worktrees/…` belongs
  to the project.
- The role table's `helper`s that were tagged only because of a path among their arguments
  (`chmod`, `mkdir` and `sh -c ls` on `~/.claude/` or `~/.codex/`), and the native binary
  run as `rg` or `ugrep`, are no longer tagged. Inside a tree they are the tree's
  processes, at the same depth and with its session.

No record says which rule tagged it, so a capture that spans the deploy contains both
kinds. Use the `meta` record from the collector's restart as the cut point.

### Schema 8: one session per agent, whole processes, masked text

Schema 8 contains two sets of changes: sessions no longer split at every tool call, and
there are fixes from a review of a week of FASRC captures. The two were deployed
together, so no capture has one without the other.

#### One session per agent, not per tool call

This part renames no field. It changes what `is_agent`, `agent_pid`, `session_key`,
`session_uuid`, `depth` and `agent_type` mean for one kind of process.

Through schema 7, `resolve()` made every process whose own argv matched the classifier
(`lib/agent_classify.py`) its own agent root, with `is_agent` true, `depth` 0, and
`agent_pid` set to its own pid. At that time the classifier matched any argv that
mentioned `/.claude/`, `/.codex/` or `.vscode-server`, so all of these became roots, and
each started a session of its own:

- Claude Code's shell for each Bash tool call (`zsh -c source ~/.claude/shell-snapshots/…`)
- its native binary re-executed as `rg`, `ugrep` or `bfs` for Grep and Glob
- `codex-linux-sandbox` and `bwrap`, once per sandboxed tool call
- `--version` and `auth status` probes, and `chmod`/`mkdir` on `~/.claude/…`

One Claude tree on madsys-gpu1 on 2026-10-06 split into 204 `session_key`s this way.

From schema 8, a tagged process is a root only when it is an **agent runtime**, or when
no agent tree is above it. Anything else the classifier tags joins its parent's tree at
`depth`+1, exactly as an untagged child does. `agent_role()` in `ebpf_trace.py` makes that
decision from the process's own argv. It is a port of `dashboard/rc_dashboard/roles.py`,
which applies the same rules to older records when the dashboard reads them, so keep the
two in sync. `agent_role()` sorts tagged processes into `agent`, `tool-shell`,
`embedded-tool`, `sandbox`, `probe`, `client`, `infra`, `ide` and `helper`, and only
`agent` starts a session inside a tree. `AgentRoleTest` pins it against the argv shapes
seen on madsys-gpu1 and the FAS login nodes. These include the agent binary run as its own
helper, which is classed as `helper`: Codex's `--codex-run-as-fs-helper` and Claude's
`--claude-in-chrome-mcp`. Schema 8 itself left the classifier unchanged. The classifier
has since been changed to read only the program (see above).

For a tagged process that is not an agent runtime, inside an agent tree:

| field | schema 7 | schema 8 |
|---|---|---|
| `is_agent` | `true` | `false` |
| `agent_pid`, `session_key`, `session_uuid` | its own | the nearest agent runtime's above it, or the tree root's if there is none |
| `depth` | 0 | its parent's + 1 |
| `agent_type` | its own tag | its tree's: a `bwrap` under Claude Code is `claude_code` |
| `autonomous` | from its argv | `null`, as on every non-root |

For example, a Bash tool call's shell, the command it runs and a Grep now all carry the
Claude process's `session_key`, at depths 1, 2 and 1.

- **Nested agents are still roots.** `claude -p` run from a tool call, and the
  `--print --sdk-url` workers `claude rc` starts, keep their own sessions. `tree_root_pid`
  still names the outermost agent, and it is unchanged for every process.
- **Outside an agent tree nothing changes.** Running `claude --version` at a human
  prompt still gives its own root, with the same `actor` and `actor3`. So did
  `chmod ~/.claude/…`, until the classifier started reading only the program (above).
- **`actor3` can change inside a tree, because `agent_type` follows the tree.** The
  `claude auth status` probe that VS Code's extension host runs every few minutes is now
  `vscode`, so its `actor3` is `human-vscode` and no longer `agent`. Conversely, something
  an agent runs that matches `.vscode-server` is now `agent`.
- **`sandbox_ancestry` still sees `bwrap`.** It used to rely on `agent_type`, which a
  `bwrap` inside a tree no longer carries. The process table now keeps each process's own
  classifier match alongside its tree's. Each tagged process still gets a sandbox read at
  exec, as it did when they were all roots.
- **`residency` changes too.** `max_depth` grows, since tool calls sit below their agent.
  `by_agent_type` changes along with `agent_type`. A tool shell whose command line carries
  a bypass flag no longer counts as `autonomous`.
- **`EBPFM_INCLUDE_ROOTS=0` keeps tool calls.** It drops roots, and a tool shell is no
  longer a root.
- **An exec clears the old attribution.** Before, a root that exec'd into an untagged
  command kept `is_agent: true`, and a process that lost its tree kept its old
  `agent_pid`.

This is a heuristic over argv, so it can misfile a shape it has not seen. A new kind of
agent runtime that gets misfiled joins its spawner's session instead of starting its own.
Its `args`, `command` and `ppid` are still on every record, so a reader can split it out
again. A capture that spans schemas 7 and 8 has sessions at two different granularities,
and `schema_versions` shows where the boundary is. On schema-8 records, the dashboard's
fold finds almost nothing left to fold.

#### Whole processes, ordered events, masked text

A review of a week of FASRC captures (8 login nodes, 2026-10-20 to 25) found four
problems: the endpoint table was full most of the day, exit records were frozen before
their argv arrived, most of the CPU time of multi-threaded processes was missing, and
`args` contained prompts and keys. Schema 8 fixes each of these, and every point below
changes what a field holds.

- **Events are applied in kernel-time order.** Each CPU has its own perf ring and BCC
  drains them in turn. As a result, a single round could deliver a process's exit before
  its own exec, and every argv after every exec. An exec applied after its exit revived
  the dead process as an entry that nothing retired (16-20k of them were live at a time
  on one node). That entry kept its endpoint-table entries and lent its identity to the
  next process that got the same pid. Events are now queued and applied only once nothing
  older can still be in a ring (`EBPFM_REORDER_MS`, 20). An event about an already-exited
  process no longer revives it, and a recycled pid gets a fresh entry. Every residency
  tick also retires entries whose process is gone from `/proc` (on a second miss), and
  counts them in `proc_vanished`.
- **An exit record is built when it is written**, after the 250 ms hold, not when the
  exit arrives, so it includes an argv, parent or attribution that arrived late.
  `ts_epoch` is still the time of the exit. Before this change, args were null on 57% of
  sub-10 ms exits.
- **Exit counters cover the whole process**, using the `thread_acc` block. As each thread
  exits it adds its own CPU, run time, faults, context switches, waits, D-state and I/O,
  and the record comes from the last thread out. Before, the record held the leader's
  counters plus only those of threads already released into `signal_struct`. One
  42-thread Codex process went from 48,297 CPU-s at its last tick to 79.7 s at exit.
  `meta` says which kind you have (`counters_scope`: `process` or `leader`), and `threads`
  is now the largest number of threads seen. For multi-threaded processes, these counters
  are not comparable with schema 7.
  A thread that decremented the group's `live` count early can reach its tracepoint after
  the last thread and also read zero. So while other threads are still unreleased, the
  record goes to whichever thread claims it (`thr_claim`), and the rest add their counts.
  The sums carry the process's start time, so leftovers from a dead process whose exit
  was lost never get merged into the next process with that pid. The record lives in a
  per-CPU map, not on the 512-byte stack.
- **`args` and `captured_tail` are masked by default** (`lib/redact.py`). Credentials
  are masked with the dashboard's own rules. So is the prompt text handed to a coding
  agent on its command line (`claude -p "..."`, `codex exec "..."`, `gemini -p`), whether
  run through node/npx/wrappers or inside `bash -c` and `sbatch --wrap`. Flags, models,
  subcommands, session names (`--name`, `--remote-control`), and the agents' own
  daemons (`claude daemon`, `codex app-server`) are kept, and the record's
  `args_redacted` names what was masked. The claude and codex tables follow each tool's
  `--help`. An option's value is kept unless it reads as a sentence where a name should
  be (codex's `-p` is `--profile`). Where a prompt could appear, only a path, id, or `-`
  is kept, and text in a non-Latin script counts as words, with or without spaces. In a
  `bash -c` string only the prompt changes, apart from a comment next to it. A heredoc's
  body is treated as data, and is masked when it feeds an agent. If the string's quoting
  stops the rules from reading a credential, the credential is masked in a copy of the
  string rebuilt from its words. Classification reads the raw argv in memory, so
  nothing about attribution changes. `EBPFM_REDACT=keys` or `prompts` keeps one kind,
  `0` turns masking off, and a value it cannot parse masks both.
  The node tier masks its argv fields the same way. It works from the separate
  arguments in `/proc/<pid>/cmdline`, as long as they still match the line `ps`
  printed. `w`'s command has no pid, so it is masked from the space-joined line. The
  same goes for a process that is gone before `/proc` is read, an argument that `ps`
  prints as `?`, and a multi-line script. In those cases, the later words of a session
  name are read as part of a prompt.

  Masking is a deny-list, and it is best effort. It masks the shapes listed below, and a
  secret in a shape it does not recognize is written unchanged. Credentials:
  - a PEM private key; an `Authorization` header's credential (a known scheme, one of
    Bearer, Basic, Token, Digest or Bot, is kept; any other scheme is masked with the
    credential: up to the closing quote, or if unquoted, two words and each word after
    a comma); a key, token, secret, cookie
    or subscription-key header (`X-Api-Key`, `X-Auth-Token`, `X-Vault-Token`,
    `PRIVATE-TOKEN`, `Cookie`, `Ocp-Apim-Subscription-Key`), also as a JSON or TOML key;
  - a URL's password, or a username long enough to be a token; `-u`/`--user`/`--proxy-user`
    `user:PASS`, but not a container's uid:gid; a query string's `token`, `key`, `secret`,
    `password`, `sig` and the like;
  - a credential-named flag's value (`--token`, `--api-key`, `--api_key`, `--password`,
    `--secret`, `--access-key`, `--credentials`, ...), also as a JSON list's items; a
    credential-named variable, kwarg or JSON key, by how its name ends (`TOKEN`,
    `SECRET`, `PASSWORD`, `PASSWD`, `PASSPHRASE`, `_PASS`, `_PWD`, `API_KEY`,
    `ACCESS_KEY`, `SECRET_KEY` and Spark's `secret.key`, `CREDENTIALS`, and in capitals
    `_KEY`, `PASS`, `_AUTH`; `PASSWD_FILE` names where a password is, and stays);
  - an argument a specific program takes as a credential: mysql's and mariadb's `-pPASS`, the mongo tools' `-p`,
    redis-cli's `-a`, sshpass's `-p`, a registry login's `-p` (docker, podman, apptainer,
    singularity, ...), `az login -p`, `twine upload -p`, `wandb login KEY`, and a
    credential-named setting in `aws configure set` and `rclone config create`. The
    program name is recognized after a space, a slash, a quote, a backtick, `;` `|` `&` `(` or
    `=`, so these also work inside `bash -c '...'` and Claude Code's `eval '...'`;
  - token shapes wherever they stand: `sk-` (OpenAI, Anthropic, OpenRouter), GitHub's
    `ghp_` and kin, `glpat-`, `hf_`, `AKIA`/`ASIA`, `xox?-`, `AIza`, `ya29.`, `npm_`, a
    JWT, EOS's `zteos64:`, `nvapi-`, `gsk_`, `r8_`, `pplx-`, `xai-`, Stripe's
    `sk_live_`/`rk_live_`, SendGrid's `SG.`, `dop_v1_`, Vault's `hvs.`, PyPI's tokens.

  Prompts: an agent's prompt arguments and options, as above. In a command string, also
  the agent's here-string or heredoc, whatever `echo`, `printf` or `cat` pipes straight
  into it, and a variable set in the same string and expanded where a prompt was masked.
  Finally, an environment variable the agent is started with whose value reads as a
  sentence (`env 'PROMPT=...'`).

  Every rule runs in time linear in the line's length. A line longer than 16 KB is cut
  at a space. The rest is neither read nor written, and `[CUT]` is written after the
  masked part. (A quoted credential that the cut falls inside is masked whole, as is one
  whose quote never closes.) In that case `args_redacted` names `cut` and `args_truncated`
  is true. A submit record's `captured_tail` is masked whole and then cut to its last
  300 characters. The node tier fails closed: if it cannot import `lib/redact.py`, it
  writes every command line as `[REDACTED]` and its `redact` field reads `["error"]`
  (unless `EBPFM_REDACT=0`). `ebpfm.sh check` tests the node tier's masking by running a
  made-up credential through it.
- **Kernel-socket traffic no longer counts as any process's network I/O.** The NFS client
  sends its RPCs from the calling process's context, so every `ls` on an NFS path became
  an endpoint, and these made up 95% of the table. A socket with no file behind it is
  now counted per node in `netpeer_kernel`, and left out of `net_*_bytes` and
  `net_endpoints`.
- **The endpoint table is read per process.** Each new entry is announced on
  `netp_new`. A process's exit reads and deletes only its own entries, and a tick reads
  only the entries of reported processes. The whole table is read every 10 ticks, or
  sooner once it fills. (`netpeer_entries` is that full read's count on those ticks, and
  the indexed count on other ticks.) The default size is 65,536 entries
  (`EBPFM_NETPEER_MAX`).
- **`cgroup` is now correct for anything a service manager starts.** The id-to-path map
  never matched on 4.18 (the id carries a generation in its top half), so a child kept
  the cgroup it inherited at fork. That showed up as `/init.scope` on 22% of one node's
  records. Ids now come from file handles, and a miss falls back to reading `/proc`.
- **`ppid` is the forking process**, not the thread within it that forked. An entry also
  keeps the parent that forked it when exec or exit later reports init as the parent
  (`nohup cmd &`).
- **Submit capture** stops at the job id, or after `EBPFM_SUBMIT_BUDGET` bytes (8192).
  Before, every write of an `srun --pty` session went up the ring (8.4M events in 6.5
  days on one node).
- **Startup attaches the probes first, then seeds from `/proc`**, so no process can
  start in a gap between the two.
- **Python 3.6** (EL8): the BTF read used a 3.7 keyword and failed silently, so no FAS
  node had `tcp_accept` or any BTF block, and `conn.age_s` used the wall clock there.
- New blocks: `fork_raw` (threads never reach the ring), `thread_acc`, `netbytes_page`
  (sendfile on kernels before 6.5). `meta.unsafe_dirs` lists directories on the path to
  the output or to the collector's code that other users can change. A second collector
  for the same output directory and host refuses to start (`.<host>.lock`).

### Schema 7: network bytes by endpoint

`net_tx_bytes`/`net_rx_bytes` said how much a process moved over the network, but not
where it went. Schema 7 keeps those totals and adds the endpoints behind them, for TCP
and UDP alike, so QUIC and DNS have a destination for the first time. TCP already had
endpoints in `conns.peers`, but only for connections opened after the collector
attached, only for the heaviest five, and only once the process exited.

On `exit` and `truncated`, **`net_endpoints`** lists every endpoint the process used
during its life:

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
`netio` (`interval_s`) and the full identity block. So the traffic of an agent that
runs for six days has a time axis, instead of arriving as one total when it exits.

The rules these fields follow:

- **Complete up to a cap, with the rest counted.** Endpoints are sorted by bytes, up to
  `EBPFM_NET_ENDPOINTS_MAX` (256). Past that, an `overflow` object carries the count
  and bytes. On one day on madsys-gpu1 the peak was 52 for one process, a VS Code
  server whose inbound connections were still counted one per client port.
- **`null`, not an empty block,** for a process with no network I/O, as with `conns`.
- **Loss is reported.** Bytes with no address anywhere (a `recv()` on an unconnected
  UDP socket) are in `unattributed`. Bytes that the per-process totals have but the
  endpoint table missed (because the table was full, or the call was in flight at
  attach) are in `unaccounted`. `residency_totals` and `stop` carry `netpeer_full`, the
  calls and bytes that found the table full, and `netpeer_orphans`, the entries deleted
  before they reached a record.
- **One rule for consumers of the time series.** A process joins the series at its first
  tick with traffic. From then on, its exit (or the stop) writes a last `netio`
  (`reason: "exit"`/`"stop"`) for the remainder, and its record says
  `in_series: true`. So the series is every `netio` record, plus the
  `exit`/`truncated` records with `in_series: false` placed at their exit time. That
  second group is exact to within one tick, since a process that had traffic before a
  tick would have joined the series. `in_series` is what stops a consumer from counting
  the same bytes twice.

How the endpoint is found:

- **Where it comes from.** An entry probe on each send/recv function records the
  socket and `msghdr` for the thread. The return probe, which already counted the bytes,
  reads the peer and adds the bytes to a BPF hash keyed by (pid, protocol, direction,
  address, port). TCP and connected UDP use the socket's peer; `sendto`/`recvfrom` on
  an unconnected UDP socket use `msg->msg_name`.
- **Direction.** Outbound traffic is keyed by the remote port and inbound traffic by the
  local port. A client's port is random, so keying on it would create one entry per
  connection. Direction comes from the TCP birth map if the map saw the socket open.
  Otherwise the port range decides (`ip_local_port_range`, compiled in and recorded as
  `meta.config.eph_range`): traffic is inbound if the remote end is on an ephemeral port
  and the local end is not. That guess covers sockets opened before the collector
  started, all UDP, and inbound TCP where `tcp_accept` does not load (4.18).
- **IPv6 UDP.** `udpv6_prot` has its own `udpv6_sendmsg`/`udpv6_recvmsg`, which the
  schema-6 probes never saw. IPv6 UDP went uncounted, and on a dual-stack socket to
  an IPv4 peer, sends were counted but receives were not. `netbytes_udp6` probes both,
  so **`net_tx_bytes`/`net_rx_bytes` cover more from schema 7 on**, which is one
  reason for the version bump. The v4-mapped send path, where `udpv6_sendmsg` calls
  `udp_sendmsg` on the same socket, is counted once.
- **Read, never deleted while alive.** Userspace reads the whole table each tick, and
  again before it writes a networked process's record. A live process's entries are
  never deleted, because a delete would race the kernel's increments and 4.18 has no
  atomic read-and-delete. An exited process's entries are deleted.
- **Names come from argv, never from traffic.** `host` is set only when the process's
  own command line names the endpoint (see below). Nothing parses DNS answers, TLS or
  HTTP. `provider` is still the existing CIDR bucket, and behind a proxy the endpoint is
  the proxy.

**`host` on an endpoint** is a name from the process's own command line: every URL's
host (`curl https://…`, `pip --index-url=…`, `git clone https://…`), and the destination
of `ssh`/`sftp`/`scp`/`rsync`, which covers the `ssh` that git runs for an ssh remote.
Those names are resolved on a background thread and cached, never on the event path.
An endpoint gets a name only when its address is among the addresses the name resolves
to, so `host` is null rather than a guess when:

- the name isn't in argv, as with an agent's own API calls;
- the lookup hasn't finished yet;
- the name resolved to other addresses, which can happen with a CDN that rotates them.

The collector takes only the hostname, never a URL's credentials. It skips address
literals and `localhost`, and never treats a file name or an email address as a
hostname. It looks names up only for processes it reports anyway, and reads each of
their argvs once. It reads the first 4,096 characters of argv (the in-kernel read's
default; an argv read from `/proc` at start has no cap) and never stops in the middle of
a word. It looks for a URL's scheme in at most the 32 characters before its `://`. This
keeps the cost linear in whatever a user puts on a command line. `stop.argv_hosts`
counts lookups that resolved, failed, or were dropped because the queue was full. Each
dropped lookup may mean a missing `host`. `EBPFM_ARGV_HOSTS=0` turns this off.

`conn` records gain **`proto`**. The netlink dump asks for TCP and UDP, and a
connected UDP socket reports `ESTABLISHED`, so a DHCP client's socket
(128.103.1.210:67 on madsys-gpu1) showed up as a standing TCP connection with no pid,
1 439 times a day. A UDP row also no longer joins the TCP-only birth map.

The envelope gains **`collector_pid`** (see above), for filtering out the collector's
own process tree, and loses **`source`** and **`collector`**. Those were the constants
`"ebpf"` and `"ebpf_marthen_new"` on every line, and nothing read them. Pre-7 captures still
carry them.

**`args` is no longer capped by default** (`EBPFM_ARGS_MAXLEN=0`; it was 2048). The
limit now is how much argv the collector can read. The in-kernel read at exec stops at
`EBPFM_ARGV_KMAX` (4096 bytes). A `/proc` read, used for processes seeded at startup,
gets the whole command line up to the 16 KB that the masking reads (*masked text*,
above). `args_truncated` now also
reports the in-kernel cut. Before, it
compared only against the emit cap, so with no cap a clipped command line would have
looked complete. The cost is record width. The identity block, and therefore `args`,
goes on every record about a process, including each tick's `residency`, `conn` and
`netio`, so one long command line is repeated all day. Set a cap to bound that.

**`command` replaces `comm`.** The kernel's comm stops at 15 characters, so
`codex-linux-sandbox` showed up as `codex-linux-san` and a VS Code CLI as `code-2242ebbb54`.
`command` is the comm, completed from argv when it hit that limit. The completion is the
first token whose basename extends the comm: argv[0] for a binary, and the script path
for a script run through its interpreter. On one day of madsys-gpu1 data, most of the 115
records with a 15-character comm were completed this way. A name that has no longer form
anywhere stays as the kernel reported it, for example a thread name set with `prctl`, or
the loader run with no arguments. Keys named after `comm` were renamed to match:
`by_command` on `residency`, `top_procs[].command`,
`dropped.by_command` and `stop.conns_dropped_commands`. `submit.tool` keeps its name but
now holds the whole command. This also fixes a latent bug. `SANDBOX_COMMS` lists
`codex-linux-sandbox`, and since it was matched against the truncated comm, that entry
could never fire. It is now matched on `command`.

**`cwd_source` is gone.** Once the `cwd` value itself is there, whether it was read or
inherited was not worth a field on every record.

**`env_flags` is gone, and `/proc/<pid>/environ` is never opened.** `env_flags` kept only
variable names, but the file also holds the values, including API keys, for every agent
root on the node. `approval_mode` now comes from argv alone, so a bypass set through the
environment or a config file rather than a flag is not seen (`approval_src` is `argv` or
null).

The feature cache now records the block list it was probed against, and the cache is
ignored when that list changes. Without that, a host that ran an older collector would
load its cached set and never try the new blocks.

### Schema 6: connections fold into the process

`tcp` (socket close) and `accept` (inbound connect) were one record per connection.
They no longer exist. The same connections now reach the feed as a **`conns` block on the
owning process's `exit` record**, or on its `truncated` record for a process still
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

This granularity is better, not just cheaper. An agent's traffic is overwhelmingly
**repeat calls to a handful of hosts**, so twenty sequential requests to one API endpoint
used to be twenty near-identical records, and are now one `peers` entry with `n: 20`.
`peers` keeps the heaviest `EBPFM_CONNS_TOPN` (default 5) by bytes, and `peers_total` is
the true distinct count. The endpoint map is capped at 256 keys, and anything past that
is counted in `peers_overflow` rather than silently merged.

The `conns` block follows four rules:

- **`conns` is `null`, not a zeroed block, for a process that opened nothing.** Most
  processes open nothing, and the block costs ~200 B (see the width argument below).
  Whether the TCP blocks loaded at all is a property of the capture, not of each
  process, so it stays in `meta.features`.
- **`rx_bytes`/`tx_bytes` are `null`, never `0`, when no connection contributed a real
  figure** (the `tcp_basic` variant cannot read byte counters). This follows design
  principle 4.
- **`conn_s_total` is the sum of connection lifetimes, not wall time.** Connections
  overlap, so it can exceed the process's own `duration_s`. Don't use it as the base for
  a rate.
- **Loss is counted, never silent.** If a process folded connections but never produced
  a record (because it was unattributable, or its exit event was lost), GC counts it
  into `stop.conns_dropped` / `conns_dropped_commands`. That pair is the acceptance gate
  for this change.

#### The exit record is now held for 250 ms

Kernel ordering forces this hold; it is not there for batching. `do_exit()` runs
`trace_sched_process_exit()` **before** `exit_files()`, so the `TCP_CLOSE` events for the
sockets a process still held are submitted *after* the exit event that describes it.
Writing the exit record inline would fold in only the connections that happened to close
during the process's life, and would miss every connection it was still holding. For an
agent, that is nearly all of them.

So `on_exit` stages the record, and `drain_exits()` writes it one poll round later
(`EBPFM_EXIT_HOLD_MS`, default 250; `0` restores the old inline emit).

This has two consequences worth knowing:

- **File order shifts, but time order does not.** `ts_epoch` is stamped when the record is
  built, so a consumer that sorts on it (which is what it is for) sees no change. A
  consumer that assumed append order *was* time order will now see an `exit` line after
  a `residency` line stamped later.
- **Nothing is at risk at stop.** `drain_exits(final=True)` runs before `flush_live()`, so
  a pending record is still written as an `exit` and cannot also appear as a contradictory
  `truncated`.

The hold also moves the three "too cheap to keep" filters (`EBPFM_MIN_DURATION`,
`EBPFM_MIN_CPU`, the `sleep` timer filter) from decision time to *application* time. A
40 ms `curl` is below `MIN_DURATION`, and dropping it at exit would throw away the
connection that made it worth keeping. **A process that talked to the network is never
treated as a no-op.**

### Schema 5 additions

| Field | Event | What |
|---|---|---|
| `sandbox`, `sandbox_src`, `sandbox_detail` | identity | **namespace isolation**, from `/proc/<pid>/ns/*` against pid 1's. `null` (never `"unsandboxed"`) when the read was denied |
| `sandbox_ancestry` | identity | `bwrap` / `codex-linux-sandbox` / …: *how* the process got there, which is a different question from *what it is*. See below |
| `approval_mode`, `approval_src` | identity | unattended execution, split from confinement. `autonomous` is unchanged |
| `env_flags` | identity | agent env variable names from `/proc/<pid>/environ`. **Gone in schema 7:** the collector no longer reads another process's environment |
| `session_uuid` | identity | survives a collector restart, unlike the pid-based `session_key` |
| `args_len`, `args_truncated` | identity | true pre-clamp argv length. `args_truncated` is `null`, not `false`, when unknown, and `true` when the in-kernel read (`EBPFM_ARGV_KMAX`), an emit cap (`EBPFM_ARGS_MAXLEN`) or the masking's 16 KB cut (`args_redacted` names `cut`) cut it |
| `partition`, `gpus`, `array`, `time_limit_s`, `mem`, `cpus_per_task`, `req_src` | `submit` | the resource request, parsed from the tool's argv |
| `io` | `truncated` | processes alive at SIGTERM carried no I/O at all before |
| `top_procs`, `state_counts`, `tcp_open_ext`, `tcp_open` | `residency` | live per-process rows, the zombie census, standing external connections per tree |
| `tcp_open_ext_by_actor3`, `conn_unattributed`, `conn_unmatched_births` | `residency_totals` | per-class totals, the reconciliation gap, and the `tcp_birth` leak gauge |

### `sandbox` vs `sandbox_ancestry` — read both

`sandbox` is **namespace isolation and nothing else**. Seccomp and capabilities are kept
in `sandbox_detail` on purpose. Every VS Code/Electron renderer is seccomp-filtered, and
`actor3="human-vscode"` is a first-class actor class, so folding seccomp into the boolean
would mark that whole class as sandboxed. On a stock host, `systemd-resolve`,
`systemd-logind`, `rsyslogd` and `polkitd` also all report `Seccomp=2` from systemd's
`SystemCallFilter=`. A capability set decides nothing either way. An unprivileged
process has `CapEff=0` by definition, and a process that is uid 0 *inside a user
namespace* (the main case we care about) reports a **full** set.

`sandbox_ancestry` costs nothing (the ancestry is already resolved) and answers the
other half of the question. **The useful signal is where the two fields disagree.**
Claude Code's bwrap use is a capability *probe* (`bwrap --ro-bind / /`), and according
to `findings/login.md`, that probe is exactly what wedges in D-state on a dead
automount. So `ancestry="bwrap"` with `sandbox="unsandboxed"` is a probe, and
`ancestry="bwrap"` with `sandbox="sandboxed"` is real confinement.

One correct result can still be surprising. A systemd unit with `PrivateTmp=` really is
in a private mount namespace, so a hardened daemon reports `sandboxed`/`mntns`. That is
correct for what the field means, but it is not agent sandboxing. Check `sandbox_src`.

### The eight data points this adds over the poller

A coverage audit found 40 observable login-node signals: 15 already read, 17 belonging to
the node snapshot by nature, and **8 the collector could read and did not**. Here are
all eight.

**1. Working directory.** `cwd` on identity, and `work_dir` on `submit` (named
for the `groupby("work_dir")` join in five `analyze/` scripts). Each read is one
`os.readlink('/proc/<pid>/cwd')`: **readlink only, never stat**. `os.stat`,
`os.path.exists`, `os.path.realpath` and listing the directory all follow the link onto
the target mount. On a wedged autofs mount they block in uninterruptible sleep, which
would hang the single-threaded collector *during the very incident it exists to observe*.
The collector reads **inherit-first**. A fork inherits the directory and tool processes
almost never `chdir`, so the parent's value is the default. A readlink happens only for
agent roots, submit tools, tty-attached processes, or when the parent's value is unknown.
That is a handful per second instead of ~95. `EBPFM_CWD_ALWAYS=1` forces a readlink per
exec, to validate the assumption.
*Caveats:* a `bwrap` sandbox resolves the string in its own mount namespace, and a
deleted directory returns a `" (deleted)"` suffix.

**2. NFS and autofs stall time.** `dstate_wait_s`, `dstate_episodes`, `dstate_max_s` and
`dstate_src` on exit. These come from two independent blocks, both loaded where possible
so they can be cross-checked. `dstate_task` reads `task->stats.sum_block_runtime` at exit
(the kernel's own cumulative total, with no extra probe; 5.19+). `dstate_stat`
accumulates `sched:sched_stat_blocked` per tid, which fires only on unblock and is far
cheaper than filtering `sched_switch`. It is the path for older kernels. When both are
available, `task` is preferred.
**Both are inert without `kernel.sched_schedstats=1`**, which `bootstrap` sets.

**3. Network usage**, in three parts.
  - **Standing flows.** The four-tuple is stored at `SYN_SENT` as a **join key**,
    because netlink names the endpoint but carries **no pid**, and `ss -p` only gets a
    pid by scanning every `/proc/*/fd`. Each tick, the collector queries
    `NETLINK_SOCK_DIAG` directly (`SOCK_DIAG_BY_FAMILY`, `NLM_F_DUMP`, AF_INET +
    AF_INET6, TCP then UDP), parses `inet_diag_msg` + `INET_DIAG_INFO`, and joins on the
    tuple. Rows with no map entry are still emitted, with a uid and no pid. Fallback
    order: netlink → `ss --info --tcp` → `/proc/net/tcp`.
  - **Per-process totals, all protocols.** kretprobes on `tcp_sendmsg`/`tcp_recvmsg`/
    `udp_sendmsg`/`udp_recvmsg` → `net_tx_bytes`, `net_rx_bytes`, `net_calls`. **This is
    what makes QUIC visible.** Unix-socket MCP traffic is excluded by family.
    Schema 7 adds `udpv6_sendmsg`/`udpv6_recvmsg` and the endpoint of every call:
    see **Schema 7** above.
    *Caveat:* `sendfile` and `splice` bypass the socket layer.
  - **Close-record enrichment.** `connect_ms` and `established` come for free from the
    same tracepoint. `SYN_SENT`→`ESTABLISHED` is connect latency, and `SYN_SENT`→`CLOSE`
    without `ESTABLISHED` is a refused or timed-out connect.

**4. Wait channel and kernel stack.** `d_stack` on `residency`, plus `d_stack_top`, a
per-tree histogram of blocking frames. For each tracked process in `D` at the tick, the
collector reads the top 5 frames of `/proc/<pid>/stack` as root. **Reading a kernel
stack touches no filesystem**, so it cannot hang on the mount being diagnosed. This
replaces the `ptrace_may_access`-gated `wchan` for all users, and it names the stuck
server. It is capped by `EBPFM_DSTACK_MAX`.

**5. Live processes at stop.** On SIGTERM, the collector writes one `event="truncated"`
per live tracked process. It **drains the D-state and `netbytes` hashes first**, so the
record carries real accumulated counters rather than only what `/proc` shows at that
instant. That makes it strictly better than the poller's version. SIGKILL is
unrecoverable, but a missing `stop` record already signals a truncated file. A process
that cannot be read is skipped and counted in the stop record's `handler_errors`
(`flush_live`). If the flush fails as a whole, `truncated` is left null, not 0. The
residency tick does the same per process (`residency_proc`).

**6. Unattributed user processes.** **`EBPFM_MIN_UID` is the discriminator, not the
cgroup.** The cgroup cannot be trusted because it depends on how the session was
established: tty-attached human processes sat in `/system.slice/ssh.service`, while
another user's VS Code sat in a `user-1000.slice` session scope. A non-system uid with no
agent ancestry and no tty is still a user's process. `attribution` on the identity block
takes `ancestry` | `tty` | `uid`. This keeps the three physical `actor3` classes intact
for the Hive partitioning, while letting you filter the recovered set. It recovers the
orphaned `bwrap` case (reparented to init, no tty) that is the wedge's signature. It is
**off by default** (`EBPFM_MIN_UID=0`), because it is the only item that changes what an
existing class *means*. Enabling it silently would make the human population
incomparable with earlier captures.

**7. Per-agent-type totals.** `by_agent_type` in `residency_totals`, with
`{trees, n_procs, threads, rss_mb, cpu_s, d_state, users, autonomous}` per type. This is
userspace aggregation of data the collector already holds, and it mirrors the census's
`totals_by_type`. It sums to the agent row of `by_actor3`.

**8. Inbound connections.** Netlink from (3) already enumerates established inbound
sockets for free (with a uid, but no pid). Full attribution needs the kretprobe on
`inet_csk_accept`, which returns the new `struct sock *` **in the accepting process's own
context**. The inbound `SYN_RECV`→`ESTABLISHED` transition runs in softirq, where the
current task is meaningless. As of schema 6 the connection folds into the accepting
process's `conns` block (through schema 5 it was emitted as an `accept` record). It also
seeds the birth map, so inbound connections get a proper close too.

---

## Design principles

1. **Event-driven for anything that happens; sampled only for anything that persists.**
   Per-process events are exhaustive. Only the residency view samples, because asking
   what is alive *now* requires sampling. Every sampled path is bounded.
2. **Keep userspace syscalls off the per-event path.** This is why cwd is inherited by
   default rather than read from `/proc` on every exec.
3. **Never dereference a path read from `/proc`** (see data point 1).
4. **Every kernel read is a probed block.** A kernel that cannot compile a block drops
   *that block alone* and records the compiler's reason in `meta.features_rejected`.
   Dropped blocks emit `null`, **never `0`**, because an absent measurement must not look
   like a zero measurement. The outcome is cached per kernel (`$EBPFM_OUTPUT_DIR/.features_<release>.json`),
   and a start reuses only the results that are final: a block that loaded on top of the
   same blocks, a block superseded by a variant, and a block missing a block it requires.
   A block that was refused by a load or BTF error is probed again at every start, along
   with the blocks that need it, so a transient refusal is not kept for good. The cache
   is ignored when any of these changed: the block list, the BCC version, bpftool or the
   kernel's BTF, or the program text with the settings compiled into it. `bootstrap`
   deletes the cache.
5. **`perf_lost` is the acceptance gate** for anything added to the tick.

### One caveat worth knowing

`bpf_probe_read` of a `struct sock *` return value must be sign-extended from 32 bits.
`tcp_sendmsg` and friends all return `int`, so the x86-64 ABI only defines `eax`, and
the upper 32 bits of `rax` hold whatever was there before. Without the cast, a 6.7 KB
download first measured as **21 474 843 382 bytes**, which is exactly 20 GiB plus the
true 6 902:

```c
int kretprobe__tcp_sendmsg(struct pt_regs *ctx) { _netb_add((s64)(s32)PT_REGS_RC(ctx), 1); return 0; }
```

---

## `ebpfm.sh` verbs

| Verb | Does |
|---|---|
| `bootstrap` | detect distro and kernel, install missing dependencies, set required sysctls, delete the feature cache, re-verify |
| `check` | privilege, toolchain, kernel config, sysctls, tracepoints, kprobe symbols, netlink reachability, the cgroup the collector would land in and its `cpu.max`, vendored-file divergence |
| `features` | probe the BPF blocks, print the resolved set as JSON |
| `dump-c` | emit the generated BPF C (for debugging a rejected block) |
| `once [secs]` | foreground capture |
| `start` / `stop` / `status` | daemon with a pidfile, via `systemd-run --scope --slice=system.slice`; `status` gives each stream's records and size today |
| `overhead` | the one-off measurement above |
| `tail [n] [stream]` | last n records of today's files |
| `bundle` | emit the whole folder as one self-extracting `.sh` |

### Bootstrap, on a fresh Ubuntu 24.04

1. Writes a `universe` entry directly into the existing deb822 stanzas, **not** via
   `add-apt-repository`, which would itself need `software-properties-common`. Without
   universe there is **no apt candidate for `python3-bpfcc`**, and that is the failure a
   fresh node actually hits.
2. Runs `apt-get install -y python3-bpfcc linux-headers-$(uname -r)`, and adds
   `linux-tools-$(uname -r)` only if `bpftool` is missing. Skips `bpfcc-tools` (not
   needed, and there is no candidate here).
3. Runs `sysctl -w kernel.sched_schedstats=1 kernel.task_delayacct=1`, persisted to
   `/etc/sysctl.d/99-ebpfm.conf`. **Two of the eight data points are inert without
   these.**
4. Re-runs `check`, and exits non-zero on any FAIL.
5. Is idempotent. On RHEL/Rocky it uses `dnf` with `bcc-tools python3-bcc kernel-devel`.

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
`EBPFM_NETPEER_MAX` (65536; the kernel endpoint table) · `EBPFM_NET_ENDPOINTS_MAX`
(256; endpoints listed per record) · `EBPFM_NETIO` (on; `0` = no `netio` series, keep
`net_endpoints`) · `EBPFM_ARGV_HOSTS` (on; `0` = no `host` names from argv) ·
`EBPFM_FLUSH_MODE` (`poll`; `record` = flush every record, the schema-5 behaviour;
note this is NOT `EBPFM_FLUSH`, which is about flushing live *processes* at stop) ·
`EBPFM_REDACT` (`keys,prompts`; `0` = write argv raw) · `EBPFM_REORDER_MS` (20; how far
behind kernel time events are applied) · `EBPFM_SUBMIT_BUDGET` (8192; bytes of a submit
tool's output captured) · `EBPFM_MIN_CPU` /
`_DURATION` · `EBPFM_PERF_PAGES` · `EBPFM_POLL_MS` · `EBPFM_FEATURES` (force a block
set) · `EBPFM_FEATURE_CACHE` · `EBPFM_PROBE_VERBOSE=1` (print the compiler line for a
rejected block).

---

## Tests

```bash
python3 -m unittest discover -s tests -q     # 476 tests, no root, no BPF needed
python3 tests/bpf_compile_check.py           # compile every block with BCC, no root
```

`bpf_compile_check.py` runs BCC's real frontend and LLVM over the generated program,
block by block as `probe_features()` would, supplying the tracepoint argument structs
itself (tracefs is root-only). It shows what the rewriter and the 512-byte stack limit
accept on this host's headers. Only an actual load shows what the verifier does.

The unit tests cover: pure helpers, the BTF member walk (including bpftool's literal
`(anon)` rendering for unnamed union members), each BPF block compiled alone, ancestry,
which tagged processes are agent roots (schema 8: the role table and whole agent trees),
attribution precedence (ancestry beats tty beats uid), tty **non**-inheritance, cwd
inheritance, thread-clone skip, pid recycling, and the netlink parser against a captured
response blob.

They also cover the day-files: one `write(2)` per dirty stream (`os.write` counted), the
JSONL bytes unchanged, exact stop `flushes`, a failed write kept and retried, and sealed
days gzipped. `ebpfm.sh`'s helpers are run for real (`_node_tick` with a stub snapshot,
`tail`, and the record count and `stop` record that `overhead` reads). The suite holds
all of the collector's Python, including the Python inside `ebpfm.sh`, to 3.6 syntax
(EL8's version).

The schema-7 network section is also **compiled and run**. `build_net_c()` is built with
the host C compiler against `tests/bpf_mock.h` (map semantics, `bpf_probe_read` as a
copy), and its probes are driven with fake sockets laid out at the BTF offsets. That
covers the entry/return hand-off, the v4-mapped double count, byte order, direction
and a full table. Those tests are skipped without a C compiler. This is not BPF, though.
Only `features` on a real kernel shows whether the program loads and attaches, and the
`netpeer_hdr` variant, which needs kernel headers, is only checked as text.

## Kernel support

Developed against **6.8** (Ubuntu 24.04) and **6.17**. FASRC login nodes run **4.18**.
There, `dstate_task` (which needs 5.19+) will be rejected, and `tcp_accept` and
`netpeer_btf` need BTF, which the collector never managed to read under their Python 3.6
before schema 8.
`dstate_stat` is the 4.18 path for data point 2, which is why both blocks exist and are
cross-checked, and `netpeer_hdr` is the 4.18 path for the endpoint table. Run `features`
on the target kernel before trusting any field, and use `EBPFM_PROBE_VERBOSE=1` to see
why a block was rejected.

## Relationship to the other collectors

This bundle is for **login nodes only**. Compute-node collection is out of scope. It
stays with `collect/compute/`, which runs `collect/common/snapshot.py` unchanged, in `full` mode.

| Collector | Status |
|---|---|
| `collect/agents/` (poll tracer + census) | **retired** behind `EBPF_LIVE=1` — the eBPF tier's `exit` records are a declared strict superset of `proc_trace.py`'s, and `residency` / `residency_totals` replace the census at 60 s instead of ~300 s |
| `collect/nettcp/` | **retired** behind `EBPF_LIVE=1` — replaced by `tcp` / `accept` / `conn`. It had no downstream consumers |
| `collect/login/` | **narrowed, not retired.** Its collector is vendored here as `node_snapshot.py` and runs as the node tier; `log/login` keeps its schema and its path |
| `collect/agents_ebpf/` (v1) | superseded by this collector on every field |
| `collect/eBPF_marthen/` (v3) | superseded on the login tier. v3 has only `dstate_task`, which needs kernel 5.19+, so on FASRC's 4.18 login nodes it would emit **no D-state dwell at all** — on the exact nodes whose known failure mode is an autofs/NFS wedge. This collector carries `dstate_stat` as the 4.18 path |
