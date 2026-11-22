# ebpfm — login-node collector, one bash entry point

A **standalone** collector for login nodes. It has no `../` imports, so it runs anywhere this
folder is copied, or from the one self-extracting file `ebpfm.sh bundle` writes. Every signal
in plain words is in [WHAT-IT-COLLECTS.md](WHAT-IT-COLLECTS.md); which tier reads each
login-node signal, in [COVERAGE.md](COVERAGE.md); the six copied modules and the rule that
keeps them in step, in [VENDORED.md](VENDORED.md).

## Two tiers, chosen by privilege

Only half of what we need from a login node comes from kernel events, so only half of the
collector needs root:

| Tier | Privilege | What | Output |
|---|---|---|---|
| **node** | **unprivileged** | node health: load, memory, filesystem space, NFS per-mount RTT, interface counters, the D-state census, the process tree, per-user cgroup totals, boot time | `$EBPFM_NODE_OUTPUT_DIR/<date>/<host>.jsonl` |
| **ebpf** | **root** | each tracked process's exit with its status, argv, `cwd`, per-process I/O for **all** users, D-state dwell, network bytes and the endpoint each went to, its TCP connections | `$EBPFM_OUTPUT_DIR/<date>/<host>.{snapshot,exits}.jsonl` |

**eBPF cannot produce load average, memory totals, filesystem space or interface
counters**, and it is not a process census: it writes only *tracked* processes
([COVERAGE.md](COVERAGE.md#5-the-ebpf-tier-is-not-a-census)). So the node tier is not a
fallback. It collects the other half.

### Run

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
sudo ./ebpfm.sh install-unit    # persistent systemd units, one per tier
```

`EBPFM_TIER=node|ebpf|all` overrides the choice by privilege. Beside the eBPF tier, the node
tier drops the four per-process sections that tier collects properly (`SNAPSHOT_MODE=node`);
without it, it keeps them (`full`), since dropping them there would lose them. An explicit
`SNAPSHOT_MODE` wins. **Losing root on one node costs that node its per-process tier and
nothing else.**

### Supervision

`start` runs the eBPF tier as a *transient* systemd service
(`systemd-run --unit=ebpfm-<host>`), which does not survive a reboot and has no restart
policy, and the node tier as a `nohup` loop. `install-unit` writes two persistent units with
this folder's paths filled in: `ebpfm.service` (`Restart=on-failure`, `Slice=system.slice`)
and `ebpfm-node.service`, the node tier's loop. It enables neither:
`systemctl enable --now ebpfm ebpfm-node`. While `ebpfm-node` is active, `start`, `stop` and
`status` defer to it, and `install-unit` refuses while a loop `start` began still runs, so the
loop never runs twice. The node unit collects in `SNAPSHOT_MODE=node`; on a host without the
eBPF tier, set `SNAPSHOT_MODE=full` in `/etc/default/ebpfm`.
[`deploy/`](../deploy/README.md) runs both units on each of its servers.

`Restart=on-failure`, never `always`, and nothing restarts it periodically: SIGTERM makes it
write every live tracked process as `truncated`, so a periodic restart would corrupt the
trace.

---

## Why eBPF, precisely

The unprivileged `/proc` poller that this collector replaces is **quota-bound, not
design-bound**. Every process a user runs on a FASRC login node shares one cgroup v2 slice
capped at `cpu.max = 1.0` CPU, and one `/proc` scan costs ~281 ms, so the poller missed
short processes (its measured rate and floor are in
[COVERAGE.md](COVERAGE.md#6-semantics-that-changed-not-just-fields)). Worse, **its sampling
density fell as load rose**: the busier the node, the more pids to scan and the slower the
loop, which never slept.

eBPF removes that ceiling because **a probe's execution is charged to the task that
triggered the event**, not to the collector, so exhaustive capture comes out of nobody's poll
budget. Only the userspace drain costs the collector anything.

### `sudo` does not escape the cap — `systemd-run` does

A child started with `sudo` stays in the calling shell's cgroup:

```
$ sudo cat /proc/self/cgroup
0::/user.slice/user-20006.slice/session-27.scope     # still capped
```

So a `sudo`-launched collector would share the user's one CPU with everything else that user
runs. `ebpfm.sh start` therefore launches it as a transient service in `system.slice`
(`systemd-run --unit=ebpfm-<host> --slice=system.slice`):

```
$ sudo ./ebpfm.sh status
ebpfm: RUNNING on node0 (unit ebpfm-node0.service, pid 106680)
  cgroup: /system.slice/ebpfm-node0.service
```

`ebpfm.sh check` reports its own cgroup and that cgroup's `cpu.max` (what `once` or the
`nohup` fallback would be charged to), and `status` the running unit's, so the escape is
verified per node, not assumed. Without systemd, `start` falls back to `nohup` with a
warning.

---

## Concurrency model

**Kernel side: concurrent by construction.** A probe runs on whichever CPU took the event, in
the context of the task that triggered it, with no serialization across CPUs. Hash maps lock
per bucket, and per-CPU maps and perf buffers do not contend at all.

**Userspace side: single-threaded, and that is the ceiling.** One thread drains every per-CPU
buffer and decodes and JSON-encodes each event; under the GIL, more threads would not help.
At **157 µs per decoded event** (measured below) it saturates one CPU at roughly **6 400
events/s**. In testing it sustained 2 011 events/s with zero loss.

`perf_lost`, in every `residency_totals` and `stop` record, says whether that ceiling was
reached, and is the acceptance gate for anything added. If it is reached, escalate in this
order: a **BPF ring buffer** instead of per-CPU perf buffers (one shared ring, fewer
wakeups); **in-kernel aggregation** in per-CPU maps, so hot events become counters; and last,
because it adds an IPC hop, a **separate writer process**.

---

## Measured overhead

A one-off validation, run with `sudo ./ebpfm.sh overhead`. **Nothing here runs during normal
collection**, and it adds no fields. Re-measure when a probe is added or removed, and record
the numbers here with the node and kernel.

Measured on **CloudLab `c220g1` (`node0`), Ubuntu 24.04.4, kernel 6.8.0-138-generic,
32 cores, 125 GB**, 3 timed reps per condition, median, at schemas 4 and 5 (the I/O cost at
6). **Schemas 7 and 8 are not in these numbers**: 7 adds an entry probe to each send/recv
function and more work to each return probe, which puts cost on `tcp_recvmsg`, the busiest of
them; 8 adds the `fork_raw`, `thread_acc` and `netbytes_page` blocks. Run `overhead` again
before relying on these numbers on a busy node.

### Per BPF program

From a **separate** window with `kernel.bpf_stats_enabled=1`: the knob adds two
`bpf_ktime_get_ns` calls per program run, so it stays off for the timed benchmarks.

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

The process-lifecycle probes cost most: they read many task fields and, at exec, copy argv.
**The hot paths are the cheap ones**: `tcp_recvmsg` (182 ns) and `sched_stat_blocked`
(335 ns) fire thousands of times a second. The event counts show the probes were attached for
the whole window: 3 011 exec events for a benchmark of exactly 3 000 execs.

### Workload delta

With `bpf_stats` **off**, on benchmarks chosen to stress the collector's own probes:

| Workload | Schema | Off | On | Delta |
|---|---:|---:|---:|---:|
| fork/exec (3 000 execs) | 4 | 3 610 ms | 3 756 ms | +4.0 % |
| | 5 | 3 490 ms | 3 813 ms | **+9.3 %** |
| loopback TCP | 4 | 118 ms | 122 ms | +3.4 % |
| | 5 | 121 ms | 119 ms | −1.7 % |
| disk write (`dd` 2 GB) | 4 | 3 531 ms | 3 625 ms | +2.7 % |
| | 5 | 3 675 ms | 3 707 ms | +0.9 % |

The two schemas' runs share a host but not an instant, and the baseline alone swung
3 490–3 675 ms across runs, so +4.0 % against +9.3 % is not a tight comparison; the
per-event drain cost, unchanged between them, is the solid number. Read the sandbox state
only for a process that will be reported (a tty, or agent ancestry), as `cwd` is: reading it
for every process whose state was unknown cost a reproducible +12 % (194 µs/event) here.
`ebpfm.sh overhead` writes 512 MiB with `dd`, not 2 GB, so its disk row does not compare with
these.

### Collector cost

At schema 4. Schema 5 measured 157.8 µs/event at 2 011 events/s, 31.7 % of one core,
`perf_lost` zero, and per-program costs equal or lower.

| | |
|---|---|
| userspace CPU | 9.46 CPU-s over 30 s = **31.5 % of one core** |
| RSS | 201 MB (BCC retains its clang/LLVM arena after compiling) |
| events decoded | 45 230 core + 15 066 argv + 23 tcp = **2 011/s** |
| per decoded event | **157 µs** of userspace CPU |
| `perf_lost` | all zero |

**Read the percentage as a ceiling, and scale by the µs/event figure**: the benchmark drives
~830 exec/s, one to two orders of magnitude above a real login node.

One-time costs are excluded: the feature probe compiles the program once per block,
**~36–41 s**, once per kernel, and caches the result.

### I/O cost

Two schema-6 changes cut the collector's write path, and their effects compound:

| | records | `write(2)` | on disk |
|---|---|---|---|
| schema 5 — `tcp` records, flush per record | 53 340 | 53 340 | 33.6 MB |
| schema 6 — folded, flush per record | 20 000 | 20 000 | 12.4 MB |
| schema 6 — folded, flush per poll round | 20 000 | **1 687** | 12.4 MB |

*20 000 process exits, 1 in 6 opening 10 connections (33 000 total), `POLL_MS=100`, at the
830 exec/s rate of the throughput benchmark above.*

- **The fold**, one record per process instead of one per connection, gives **63 % fewer
  records and bytes**, at any load.
- **Flushing once per poll round** (`EBPFM_FLUSH_MODE=poll`, the default; `record` flushes
  after every record, as through schema 5) bounds the syscalls at one `write(2)` per dirty
  stream per round, `1000/EBPFM_POLL_MS` rounds a second, however many records a round
  produced. An idle node (≤1 record per round) sees no change, and a round that wrote nothing
  does not flush; a busy one (83 records per round) goes **20 000 → 1 687, 11.9×**. Together,
  at the busy rate: **53 340 → 1 687 `write(2)`, 31.6×**.

The trade-off is durability: a record is durable within one poll round, not at once. SIGTERM,
SIGINT and the duration stop all flush through `close()`, so only **SIGKILL or power loss**
loses anything, at most one round (100 ms). `meta` is flushed at once on startup, because
`ebpfm.sh status` looks for it.

`stop.flush_mode` names the policy, and `stop.flushes` counts a run's stream flushes exactly,
one `write(2)` each (`stop.output.jsonl.writes`). Writing JSONL costs **13.5 µs per record**
on Python 3.13 and 18.7 on 3.8 (one host's synthetic demo day through `DailyWriter`: 11 742
records, 1.6 per flush, 18.78 MB in 7 279 `write(2)`s), small beside the 157 µs per decoded
event, since only the end of a tracked process is a record.

---

## What it collects

This section covers the **eBPF tier**: its records and fields. Every signal in plain words,
the node tier's included, is in [WHAT-IT-COLLECTS.md](WHAT-IT-COLLECTS.md); the node tier's
40 keys, and which tier reads each, in [COVERAGE.md](COVERAGE.md).

`SCHEMA_VERSION = 8`. JSONL, one record per line, split across **two files per host per
day**:

| file | `event` | why separate |
|---|---|---|
| `$EBPFM_OUTPUT_DIR/<date>/<host>.exits.jsonl` | `exit`, `truncated` | the per-process terminal records. Unbounded: the rate is the node's fork rate, which is highest exactly when the node is busiest. On a build node these records make up essentially the whole output |
| `$EBPFM_OUTPUT_DIR/<date>/<host>.snapshot.jsonl` | `meta`, `conn`, `netio`, `submit`, `residency`, `residency_totals`, `stop` | what the node and the run looked like at a given moment. Small, bounded by the residency tick, and the file you want when asking "what was running" rather than "what ended" |

`stream_of()` in `ebpf_trace.py` decides which file each event goes to; a kind it has no
decision for lands in `snapshot`. Every record carries its own `host`, so globbing
`<date>/*.jsonl` reads a capture made before the split (within schema 6), one file a day, the
same way. Both files are created when the day opens, so an empty `.exits.jsonl` means "no
exits observed", not "collector never started".

| `event` | When | Grain |
|---|---|---|
| `meta` | startup | resolved blocks, rejected blocks + compiler reason, sysctl state, kernel, BCC version, `output` (`{"formats": ["jsonl"], "ebr": null}`: the format written) |
| `exit` | process exit | the main record: identity, `exit_code`/`signal`/`core_dumped`, CPU, RSS, I/O, D-state, net bytes, **`conns`**, **`net_endpoints`** |
| `conn` | residency tick | one per **standing external** socket: `proto`, RTT, retransmits, bytes, idle |
| `netio` | residency tick; a series process's exit; stop | one per process that moved bytes: bytes per endpoint since its previous `netio` |
| `residency` | residency tick | one per live agent tree |
| `residency_totals` | residency tick | `by_actor3`, `by_agent_type`, `dropped`, `fork_rate`, the endpoint table's counters, and (schema 8) the run's `perf_seen`/`perf_lost`, `handler_errors`, `proc_entries`, `proc_vanished`, `thread_acc`, `netpeer_kernel` |
| `submit` | `sbatch`/`salloc`/`srun` | `tool`, `job_id`, `work_dir`, `captured_tail` (masked; null for `--pty`) |
| `truncated` | SIGTERM | one per live tracked process, with accumulated counters |
| `stop` | shutdown | `records`, `flushes` (exact), `flush_mode`, `truncated` (null when the flush did not finish), `perf_lost`, `perf_seen`, `handler_errors`, `proc_vanished`, `conns_dropped`, `netio_records`, `netpeer_full`, `netpeer_orphans`, `argv_hosts`, `reason`, `output` (`jsonl`: records, bytes, `write(2)`s and errors up to this record; `ebr`: null) |

**Every** record carries `ts_epoch` (float epoch seconds) besides `ts`, which is local, naive
and second-granular: fine for reading one host's file, useless for ordering or binning
records from several hosts. `ts_epoch` is when the record was emitted, except that an `exit`
record's is when the exit was handled. `meta` also carries `tz` and `boot_id`; without them a
reader cannot convert `ts` at all, or tell a DST shift from a clock jump.

From schema 7, every record also carries **`collector_pid`**, the collector's own pid. Drop
records whose `pid` or `ppid` equals it: a normal deployment never admits the collector's
tree (no tty, no agent ancestry), but a capture started from a login shell
(`sudo ./ebpfm.sh once`) inherits the tty, and its own `truncated` record and its children's
exits (`bpftool`) would look like someone's work. The `ebpfm.sh` and `sudo` above it are not
covered. The field also tells two runs in one day's file apart.

### Agent tags: the program, not a mention

`classify()` (`lib/agent_classify.py`, and its copy in `node_snapshot.py` behind
`proc_tree.agents`) tags a command line by the program it runs, not by every word in it. The
program is argv[0], by its name (`claude`, `codex`, `codex-linux-sandbox`, `bwrap`) or where
it is installed (`~/.local/share/claude/versions/…`, `~/.codex/…`, an IDE extension,
`~/.vscode-server/…`; under `~/.claude/`, only Claude Code's own installs: `local/`, `remote/`
and its CLI's `cli.js`); the word that `env`, `exec`, `nohup`, `setsid`, `nice`, `timeout` or a
`VAR=value` runs; a package runner's package (`npx`, `npm exec`, `pnpm dlx`, `yarn dlx`,
`bunx`, `bun x`); an interpreter's script (`node …/@anthropic-ai/claude-code/cli.js`); the
first word of a shell's `-c` command (`sh -c exec claude …`); and, for a `bwrap`, every word,
because a sandbox is tagged by what it confines.

The classifier also counts what only an agent's runtime writes, where it writes it: Claude
Code's tool-call shell (a shell sourcing `~/.claude/shell-snapshots/…`), Cursor's
`CURSOR_SANDBOX` and Windsurf's `WINDSURF_STATE` in the environment a program runs with
(`CURSOR_SANDBOX=1 bash …`, `env CURSOR_SANDBOX=1 …`, a bwrap's `--setenv CURSOR_SANDBOX 1`;
case-sensitive), and Windsurf's `windsurf-terminal` as a program. A word that merely names or
prints one never counts (`grep -r CURSOR_SANDBOX .`, `echo CURSOR_SANDBOX=1`). `ClassifyTest`
pins both lists, and checks that the node tier's copy agrees.

So a command that only mentions an agent (`grep claude notes.txt`, `vim ~/.claude/CLAUDE.md`,
`git clone https://github.com/openai/codex`, a tmux session named `claude`) belongs to
whoever ran it: at a person's prompt it is that person's (`actor` by tty), and inside an agent
tree it joins the tree like any untagged child, inheriting its sandbox and `cwd` reads. A hook
or status-line script under `~/.claude/`, run by hand, is the user's, and a program in a
project's `.claude/worktrees/…` is the project's.

This changed within schema 8, with no version bump. Before it, each such mention was an agent
root with a session of its own, and some agents went untagged: Claude's native binary started
by its versions path, its npm launcher by path, and `npx @anthropic-ai/claude-code`. No record
says which rule tagged it, so in a capture that spans the change, use the `meta` record of
the collector's restart as the cut point.

### Schema 8: one session per agent, whole processes, masked text

Schema 8 contains two sets of changes, deployed together, so no capture has one without the
other.

#### One session per agent, not per tool call

This part renames no field. It changes what `is_agent`, `agent_pid`, `session_key`,
`session_uuid`, `depth` and `agent_type` mean for one kind of process. Through schema 7,
every process the classifier tagged was its own agent root (`is_agent` true, `depth` 0,
`agent_pid` its own pid), so each of these started a session of its own: Claude Code's shell
for each Bash tool call, its native binary re-executed as `rg`, `ugrep` or `bfs` for Grep and
Glob, `codex-linux-sandbox` and `bwrap` once per sandboxed tool call, and `--version` and
`auth status` probes.

From schema 8, a tagged process is a root only when it is an **agent runtime**, or when no
agent tree is above it; anything else the classifier tags joins its parent's tree at
`depth`+1, like an untagged child. `agent_role()` in `ebpf_trace.py` decides from the
process's own argv, sorting tagged processes into `agent`, `tool-shell`, `embedded-tool`,
`sandbox`, `probe`, `client`, `infra`, `ide` and `helper`; only `agent` starts a session
inside a tree. It is a port of `dashboard/rc_dashboard/roles.py`, which applies the same
rules to older records when the dashboard reads them, so keep the two in sync.
`AgentRoleTest` pins it against the argv shapes seen on madsys-gpu1 and the FAS login nodes,
including the agent binary run as its own helper, which is a `helper`: Codex's
`--codex-run-as-fs-helper` and Claude's `--claude-in-chrome-mcp`.

For a tagged process that is not an agent runtime, inside an agent tree:

| field | schema 7 | schema 8 |
|---|---|---|
| `is_agent` | `true` | `false` |
| `agent_pid`, `session_key`, `session_uuid` | its own | the nearest agent runtime's above it, or the tree root's if there is none |
| `depth` | 0 | its parent's + 1 |
| `agent_type` | its own tag | its tree's: a `bwrap` under Claude Code is `claude_code` |
| `autonomous` | from its argv | `null`, as on every non-root |

A Bash tool call's shell, the command it runs and a Grep thus all carry the Claude process's
`session_key`, at depths 1, 2 and 1.

- **Nested agents are still roots**: `claude -p` run from a tool call, and the `--print
  --sdk-url` workers `claude rc` starts, keep their own sessions. `tree_root_pid` still names
  the outermost agent, and outside an agent tree nothing changes (`claude --version` at a
  human prompt is still its own root).
- **`actor3` can change inside a tree, because `agent_type` follows the tree**: the `claude
  auth status` probe VS Code's extension host runs every few minutes is `vscode`, so
  `human-vscode`, not `agent`; something an agent runs that matches `.vscode-server` is
  `agent`. Each tagged process keeps its own match too, so `sandbox_ancestry` still sees
  `bwrap`.
- **`residency`**: `max_depth` grows, `by_agent_type` follows `agent_type`, and a tool shell
  whose command line carries a bypass flag no longer counts as `autonomous`.
- **`EBPFM_INCLUDE_ROOTS=0` keeps tool calls**: it drops roots, and a tool shell is no longer
  one.
- **An exec clears the old attribution**: a root that execs an untagged command is no longer
  `is_agent`, and a process that loses its tree no longer keeps its old `agent_pid`.

This is a heuristic over argv, so it can misfile a shape it has not seen: a new kind of agent
runtime joins its spawner's session instead of starting its own. Its `args`, `command` and
`ppid` are on every record, so a reader can split it out again. A capture that spans schemas
7 and 8 has sessions at two granularities; each record's `schema_version` marks the
boundary.

#### Whole processes, ordered events, masked text

Each of these changes what a field holds.

- **Events are applied in kernel-time order**, once nothing older can still be in a CPU's
  ring (`EBPFM_REORDER_MS`, 20). Through schema 7 a late exec could revive an exited process
  as an entry nothing retired (16–20k live at a time on one node), which lent its identity to
  the next process with the same pid. Now a recycled pid gets a fresh entry, and each
  residency tick retires entries whose process is gone from `/proc` (on a second miss),
  counting them in `proc_vanished`.
- **An exit record is built when it is written**, after the 250 ms hold, so it includes an
  argv, parent or attribution that arrived late; `ts_epoch` is still the time of the exit.
  Through schema 7, `args` was null on 57 % of sub-10 ms exits.
- **Exit counters cover the whole process** (the `thread_acc` block): as each thread exits it
  adds its own CPU, run time, faults, context switches, waits, D-state and I/O, and the
  record comes from the last thread out. Through schema 7 the record held the leader's
  counters plus only those of threads already released into `signal_struct`, undercounting a
  multi-threaded process by up to 99.8 %, so `cpu_s`, `sched_run_s`, `min_flt`/`maj_flt`,
  `nvcsw`/`nivcsw`, the `*_wait_s` fields, `dstate_*`, `io`, `threads` and `peak_rss_mb` of
  multi-threaded processes do not compare across the boundary. `meta.counters_scope` says
  which kind you have (`process` or `leader`), and `threads` is now the largest number of
  threads seen.
- **`args` and `captured_tail` are masked by default** (`lib/redact.py`). Credentials are
  masked with the dashboard's own rules, and so is the prompt text handed to a coding agent
  on its command line (`claude -p "..."`, `codex exec "..."`, `gemini -p`), whether run
  through node/npx/wrappers or inside `bash -c` and `sbatch --wrap`. Flags, models,
  subcommands, session names (`--name`, `--remote-control`) and the agents' own daemons
  (`claude daemon`, `codex app-server`) are kept, and the record's `args_redacted` names what
  was masked; `args_len` is still the raw argv's length. The claude and codex tables follow
  each tool's `--help`. An option's value is kept unless it reads as a sentence where a name
  should be (codex's `-p` is `--profile`). Where a prompt could appear, only a path, an id or
  `-` is kept, and text in a non-Latin script counts as words, with or without spaces. In a
  `bash -c` string only the prompt changes, apart from a comment next to it. A heredoc's body
  is data, masked when it feeds an agent. If the string's quoting stops the rules from
  reading a credential, the credential is masked in a copy of the string rebuilt from its
  words. Classification reads the raw argv in memory, so attribution does not change.
  `EBPFM_REDACT=keys` or `prompts` keeps one kind, `0` turns masking off, and a value it
  cannot parse masks both.

  The node tier masks its argv fields the same way, from the separate arguments in
  `/proc/<pid>/cmdline` while they still match the line `ps` printed. `w`'s command (it has
  no pid), a process gone before `/proc` is read, an argument `ps` prints as `?`, and a
  multi-line script are masked from the space-joined line, where the later words of a session
  name are read as part of a prompt.

  Masking is a deny-list, and best effort: it masks the shapes listed below, and a secret in
  a shape it does not recognize is written unchanged. Credentials:
  - a PEM private key; an `Authorization` header's credential (a known scheme, one of Bearer,
    Basic, Token, Digest or Bot, is kept; any other scheme is masked with the credential: up
    to the closing quote, or if unquoted, two words and each word after a comma); a key,
    token, secret, cookie or subscription-key header (`X-Api-Key`, `X-Auth-Token`,
    `X-Vault-Token`, `PRIVATE-TOKEN`, `Cookie`, `Ocp-Apim-Subscription-Key`), also as a JSON
    or TOML key;
  - a URL's password, or a username long enough to be a token; `-u`/`--user`/`--proxy-user`
    `user:PASS`, but not a container's uid:gid; a query string's `token`, `key`, `secret`,
    `password`, `sig` and the like;
  - a credential-named flag's value (`--token`, `--api-key`, `--api_key`, `--password`,
    `--secret`, `--access-key`, `--credentials`, ...), also as a JSON list's items; a
    credential-named variable, kwarg or JSON key, by how its name ends (`TOKEN`, `SECRET`,
    `PASSWORD`, `PASSWD`, `PASSPHRASE`, `_PASS`, `_PWD`, `API_KEY`, `ACCESS_KEY`,
    `SECRET_KEY` and Spark's `secret.key`, `CREDENTIALS`, and in capitals `_KEY`, `PASS`,
    `_AUTH`; `PASSWD_FILE` names where a password is, and stays);
  - an argument a specific program takes as a credential: mysql's and mariadb's `-pPASS`, the
    mongo tools' `-p`, redis-cli's `-a`, sshpass's `-p`, a registry login's `-p` (docker,
    podman, apptainer, singularity, ...), `az login -p`, `twine upload -p`, `wandb login KEY`,
    and a credential-named setting in `aws configure set` and `rclone config create`. The
    program name is recognized after a space, a slash, a quote, a backtick, `;` `|` `&` `(` or
    `=`, so these also work inside `bash -c '...'` and Claude Code's `eval '...'`;
  - token shapes wherever they stand: `sk-` (OpenAI, Anthropic, OpenRouter), GitHub's `ghp_`
    and kin, `glpat-`, `hf_`, `AKIA`/`ASIA`, `xox?-`, `AIza`, `ya29.`, `npm_`, a JWT, EOS's
    `zteos64:`, `nvapi-`, `gsk_`, `r8_`, `pplx-`, `xai-`, Stripe's `sk_live_`/`rk_live_`,
    SendGrid's `SG.`, `dop_v1_`, Vault's `hvs.`, PyPI's tokens.

  Prompts: an agent's prompt arguments and options, as above. In a command string, also the
  agent's here-string or heredoc, whatever `echo`, `printf` or `cat` pipes straight into it,
  and a variable set in the same string and expanded where a prompt was masked. Finally, an
  environment variable the agent is started with whose value reads as a sentence (`env
  'PROMPT=...'`).

  Every rule runs in time linear in the line's length. A line longer than 16 KB is cut at a
  space: the rest is neither read nor written, `[CUT]` follows the masked part (a quoted
  credential the cut falls inside is masked whole, as is one whose quote never closes),
  `args_redacted` names `cut` and `args_truncated` is true. A submit record's
  `captured_tail` is masked whole and then cut to its last 300 characters. The node tier
  fails closed: if it cannot import `lib/redact.py`, it writes every command line as
  `[REDACTED]` and its `redact` field reads `["error"]` (unless `EBPFM_REDACT=0`). `ebpfm.sh
  check` tests the node tier's masking on a made-up credential.
- **Kernel-socket traffic is not any process's network I/O.** The NFS client sends its RPCs
  from the calling process's context, so through schema 7 every `ls` on an NFS path became an
  endpoint (95 % of the table). A socket with no file behind it is now counted per node in
  `netpeer_kernel` and left out of `net_*_bytes` and `net_endpoints`, which therefore read
  lower for NFS-heavy processes.
- **The endpoint table** (65 536 entries, `EBPFM_NETPEER_MAX`) **is read per process**, and
  whole every 10 ticks or once it fills; `netpeer_entries` is that full read's count on those
  ticks, and the indexed count on others.
- **`cgroup` is correct for anything a service manager starts.** On 4.18 the id-to-path map
  never matched (the id carries a generation in its top half), so a child kept the cgroup it
  inherited at fork: `/init.scope` on 22 % of one node's records was that artefact. Ids now
  come from file handles, and a miss falls back to reading `/proc`.
- **`ppid` is the forking process**, not the thread within it that forked, and an entry keeps
  the parent that forked it when exec or exit later reports init (`nohup cmd &`).
- **Submit capture** stops at the job id, or after `EBPFM_SUBMIT_BUDGET` bytes (8192).
- **Startup attaches the probes first, then seeds from `/proc`**, so no process can start in
  a gap between the two.
- **Python 3.6** (EL8): through schema 7 the BTF read used a 3.7 keyword and failed silently,
  so FAS captures before schema 8 have no `tcp_accept` or other BTF block, and their
  `conn.age_s` came from the wall clock (about 56 years off).
- New blocks: `fork_raw` (threads never reach the ring), `thread_acc`, `netbytes_page` (TCP
  `sendfile`/`splice` sends, on kernels before 6.5). `meta.unsafe_dirs` lists directories on
  the path to the output or to the collector's code that other users can change. A second
  collector for the same output directory and host refuses to start (`.<host>.lock`).

### Schema 7: network bytes by endpoint

`net_tx_bytes`/`net_rx_bytes` say how much a process moved over the network. Schema 7 keeps
those totals and adds the endpoints behind them, for TCP and UDP alike, so QUIC and DNS have a
destination too (`conns.peers` names only the heaviest five TCP peers, of connections opened
after the collector attached, and only at exit). On `exit` and `truncated`,
**`net_endpoints`** lists every endpoint the process used during its life:

```json
"net_endpoints": {"n": 2, "in_series": true, "unattributed": null,
  "endpoints": [
    {"proto": "tcp", "addr": "140.82.112.3", "port": 443, "host": "github.com",
     "inbound": false, "external": true, "provider": "github",
     "tx_bytes": 139000, "rx_bytes": 880000, "calls": 412},
    {"proto": "udp", "addr": "10.31.0.5", "port": 53, "host": null, "inbound": false,
     "external": false, "provider": null, "tx_bytes": 80, "rx_bytes": 240, "calls": 4}]}
```

In the snapshot stream, **`netio`** is the time series: one record per process per residency
tick, with the bytes each endpoint moved since that process's previous `netio` (`interval_s`)
and the full identity block, so an agent that runs for six days has a time axis for its
traffic.

- **Complete up to a cap, with the rest counted.** Endpoints are sorted by bytes, up to
  `EBPFM_NET_ENDPOINTS_MAX` (256); past that, an `overflow` object carries the count and
  bytes.
- **`null`, not an empty block,** for a process with no network I/O, as with `conns`.
- **Loss is reported.** Bytes with no address anywhere (a `recv()` on an unconnected UDP
  socket) are in `unattributed`. Bytes the per-process totals have but the endpoint table
  missed (because it was full, or the call was in flight at attach) are in `unaccounted`.
  `residency_totals` and `stop` carry `netpeer_full`, the calls and bytes that found the table
  full, and `netpeer_orphans`, the entries deleted before they reached a record.
- **One rule for consumers of the time series.** A process joins the series at its first
  tick with traffic. From then on, its exit (or the stop) writes a last `netio` (`reason:
  "exit"`/`"stop"`) for the remainder, and its record says `in_series: true`. So the series is
  every `netio` record, plus the `exit`/`truncated` records with `in_series: false` placed at
  their exit time, exact to within one tick. `in_series` is what stops a consumer from
  counting the same bytes twice.

How the endpoint is found:

- **Direction.** Outbound traffic is keyed by the remote port and inbound by the local port,
  since a client's port is random. Direction comes from the TCP birth map if it saw the socket
  open; otherwise traffic is inbound if the remote end is on an ephemeral port and the local
  end is not (`ip_local_port_range`, recorded as `meta.config.eph_range`). That guess covers
  sockets opened before the collector started, all UDP, and inbound TCP where `tcp_accept`
  did not load.
- **IPv6 UDP.** `netbytes_udp6` probes `udpv6_sendmsg`/`udpv6_recvmsg`, which schema 6 never
  saw: IPv6 UDP went uncounted, as did receives on a dual-stack socket from an IPv4 peer. So
  **`net_tx_bytes`/`net_rx_bytes` cover more from schema 7 on**. The v4-mapped send path is
  counted once.
- **Names come from argv, never from traffic.** Nothing parses DNS answers, TLS or HTTP.
  `provider` is the CIDR bucket, and behind a proxy the endpoint is the proxy.

**`host` on an endpoint** is a name from the process's own command line: every URL's host
(`curl https://…`, `pip --index-url=…`, `git clone https://…`), and the destination of
`ssh`/`sftp`/`scp`/`rsync`, including the `ssh` git runs. Names are resolved on a background
thread, and an endpoint gets one only when its address is among the name's addresses, so
`host` is null, not a guess, when the name is not in argv (an agent's own API calls), the
lookup has not finished, or the name resolved elsewhere (a CDN). The collector takes only the
hostname, never a URL's credentials, skips address literals and `localhost`, never takes a
file name or an email address for a host, and looks up names only for processes it reports.
`stop.argv_hosts` counts lookups resolved, failed, and dropped on a full queue (each a
possibly missing `host`). `EBPFM_ARGV_HOSTS=0` turns this off.

`conn` records gain **`proto`**: the netlink dump asks for TCP and UDP, and a connected UDP
socket reports `ESTABLISHED`, so a DHCP client's socket read as a standing TCP connection. A
UDP row does not join the TCP-only birth map.

The envelope gains **`collector_pid`** (above) and loses **`source`** and **`collector`**,
the constants `"ebpf"` and `"ebpf_marthen_new"`, which pre-7 captures still carry.

**`args` is not capped by default** (`EBPFM_ARGS_MAXLEN=0`; it was 2048). The limit is how
much argv the collector can read: `EBPFM_ARGV_KMAX` (4096 bytes) in the kernel at exec, and,
for processes seeded from `/proc` at startup, the whole command line up to the 16 KB the
masking reads. `args_truncated` reports the in-kernel cut as well as an emit cap. The cost is
record width. The identity block, and therefore `args`, goes on every record about a process,
including each tick's `residency`, `conn` and `netio`, so one long command line is repeated
all day. Set a cap to bound that.

**`command` replaces `comm`.** The kernel's comm stops at 15 characters (`codex-linux-san`,
`code-2242ebbb54`). `command` is the comm, completed from argv when it hit that limit: the
first token whose basename extends it, argv[0] for a binary and the script path for a script
run through its interpreter. A name with no longer form anywhere stays as the kernel reported
it, for example a thread name set with `prctl`. Keys named after `comm` were renamed:
`by_command` on `residency`, `top_procs[].command`, `dropped.by_command` and
`stop.conns_dropped_commands`. `submit.tool` keeps its name but holds the whole command.

**`cwd_source` and `env_flags` are gone, and `/proc/<pid>/environ` is never opened**: the
file holds every variable's value, API keys included. `approval_mode` therefore comes from
argv alone, so a bypass set through the environment or a config file is not seen
(`approval_src` is `argv` or null).

### Schema 6: connections fold into the process

The per-connection `tcp` (socket close) and `accept` (inbound connect) records no longer
exist. The same connections reach the feed as a **`conns` block on the owning process's
`exit` record**, or on its `truncated` record for a process still alive at stop:

```json
"conns": {"n": 21, "accepted": 0, "established": 20, "failed": 1, "external": 21,
          "rx_bytes": 883110, "tx_bytes": 140022, "conn_s_total": 41.3,
          "connect_ms_p50": 31.2, "connect_ms_max": 88.0,
          "providers": {"anthropic": 20}, "peers_total": 2,
          "peers": [{"daddr": "104.18.0.1", "dport": 443, "n": 20, "external": true,
                     "provider": "anthropic", "inbound": 0, "failed": 0,
                     "rx_bytes": 880000, "tx_bytes": 139000}]}
```

An agent's traffic is overwhelmingly **repeat calls to a handful of hosts**, so twenty
sequential requests to one API endpoint are one `peers` entry with `n: 20`. `peers` keeps the
heaviest `EBPFM_CONNS_TOPN` (5) by bytes, and `peers_total` is the true distinct count. The
endpoint map is capped at 256 keys, and anything past that is counted in `peers_overflow`
rather than merged.

- **`conns` is `null`, not a zeroed block, for a process that opened nothing.** Whether the
  TCP blocks loaded at all is a property of the capture, in `meta.features`.
- **`rx_bytes`/`tx_bytes` are `null`, never `0`, when no connection contributed a real
  figure** (the `tcp_basic` variant cannot read byte counters): design principle 4.
- **`conn_s_total` is the sum of connection lifetimes, not wall time.** Connections overlap,
  so it can exceed the process's own `duration_s`. Don't use it as the base for a rate.
- **Loss is counted, never silent.** Connections folded into a process that never produced a
  record (unattributable, or its exit event lost) are counted in `stop.conns_dropped` /
  `conns_dropped_commands`.

#### The exit record is held for 250 ms

Kernel ordering forces this hold. `do_exit()` runs `trace_sched_process_exit()` **before**
`exit_files()`, so the `TCP_CLOSE` events for the sockets a process still held arrive *after*
its exit event. Written inline, the exit record would miss every connection the process was
still holding, which for an agent is nearly all of them. So the record is written one poll
round later (`EBPFM_EXIT_HOLD_MS`, 250; `0` writes it inline).

- **File order shifts, but time order does not.** An `exit` line can follow a `residency`
  line stamped later, but its `ts_epoch` is the exit's, so a consumer that sorts on it (which
  is what it is for) sees no change.
- **Nothing is at risk at stop.** Pending records are written as `exit` before live processes
  are flushed, so none also appears as a contradictory `truncated`.
- The "too cheap to keep" filters (`EBPFM_MIN_DURATION`, `EBPFM_MIN_CPU`, the `sleep` timer
  filter) apply when the record is written, not at exit: dropping a 40 ms `curl` at exit would
  throw away the connection that made it worth keeping. **A process that talked to the
  network is never treated as a no-op.**

### Schema 5 additions

| Field | Event | What |
|---|---|---|
| `sandbox`, `sandbox_src`, `sandbox_detail` | identity | **namespace isolation**, from `/proc/<pid>/ns/*` against pid 1's. `null` (never `"unsandboxed"`) when the read was denied. `sandbox_detail` is a comma-joined string, to keep records narrow. See below |
| `sandbox_ancestry` | identity | `bwrap` / `codex-linux-sandbox` / …: *how* the process got there, which is a different question from *what it is*. See below |
| `approval_mode`, `approval_src` | identity | unattended execution, split from confinement. `autonomous` is unchanged |
| `env_flags` | identity | agent environment variable names, from `/proc/<pid>/environ`; schemas 5 and 6 only (see Schema 7) |
| `session_uuid` | identity | survives a collector restart, unlike the pid-based `session_key` |
| `args_len`, `args_truncated` | identity | true pre-clamp argv length. `args_truncated` is `null`, not `false`, when unknown, and `true` when the in-kernel read (`EBPFM_ARGV_KMAX`), an emit cap (`EBPFM_ARGS_MAXLEN`) or the masking's 16 KB cut (`args_redacted` names `cut`) cut it |
| `partition`, `gpus`, `array`, `time_limit_s`, `mem`, `cpus_per_task`, `req_src` | `submit` | the resource request, parsed from the tool's argv only: the `#SBATCH` script is never read (design principle 3) |
| `io` | `truncated` | processes alive at SIGTERM carried no I/O at all before |
| `top_procs`, `state_counts`, `tcp_open_ext`, `tcp_open` | `residency` | live per-process rows, the zombie census, standing external connections per tree |
| `tcp_open_ext_by_actor3`, `conn_unattributed`, `conn_unmatched_births` | `residency_totals` | per-class totals, the reconciliation gap, and the `tcp_birth` leak gauge |

### `sandbox` vs `sandbox_ancestry` — read both

`sandbox` is **namespace isolation and nothing else**. Seccomp, no-new-privs and a reduced
bounding set go in `sandbox_detail` only: every VS Code/Electron renderer and many systemd
daemons run seccomp-filtered, and `actor3="human-vscode"` is a class of its own, so folding
seccomp in would mark that whole class sandboxed; and an unprivileged process has `CapEff=0`
by definition, while one that is uid 0 *inside a user namespace* (the main case) reports a
**full** set, so a capability set decides nothing either way.

`sandbox_ancestry` costs nothing (the ancestry is already resolved) and answers the other
half of the question. **The useful signal is where the two fields disagree.** Claude Code's
bwrap use is a capability *probe* (`bwrap --ro-bind / /`), the kind that wedges in D-state on
a dead automount, so `ancestry="bwrap"` with `sandbox="unsandboxed"` is a probe, and
`ancestry="bwrap"` with `sandbox="sandboxed"` is real confinement.

One correct result can still surprise: a systemd unit with `PrivateTmp=` really is in a
private mount namespace, so a hardened daemon reports `sandboxed`/`mntns`. That is right for
what the field means, but it is not agent sandboxing. Check `sandbox_src`.

### The eight data points this adds over the poller

Eight signals that an unprivileged poller cannot read, or reads worse:

**1. Working directory.** `cwd` on identity, and `work_dir` on `submit`. Each read is one
`os.readlink('/proc/<pid>/cwd')`: **readlink only, never stat**. Anything that follows the
link (`os.stat`, `os.path.exists`, `os.path.realpath`, a listing) blocks in uninterruptible
sleep on a wedged autofs mount, and would hang the single-threaded collector *during the very
incident it exists to observe*. Reads are **inherit-first**: a fork inherits its parent's
directory and tool processes almost never `chdir`, so a readlink happens only for agent
roots, submit tools, tty-attached processes, or when the parent's value is unknown
(`EBPFM_CWD_ALWAYS=1` forces one per exec, to validate that). *Caveats:* a `bwrap` sandbox
resolves the path in its own mount namespace, and a deleted directory reads with a
`" (deleted)"` suffix.

**2. NFS and autofs stall time.** `dstate_wait_s`, `dstate_episodes`, `dstate_max_s` and
`dstate_src` on exit, from two independent blocks, both loaded where possible so they can be
cross-checked. `dstate_task` reads the kernel's own cumulative `task->stats.sum_block_runtime`
at exit (5.19+). `dstate_stat` accumulates `sched:sched_stat_blocked` per thread, which fires
only on unblock, and is the path for older kernels. When both load, `task` is preferred.
**Both are inert without `kernel.sched_schedstats=1`**, which `bootstrap` sets.

**3. Network usage**, in three parts.
  - **Standing flows.** Netlink (`NETLINK_SOCK_DIAG`, `lib/sockdiag.py`) names each socket's
    endpoint but carries **no pid**, so each tick joins its rows to the four-tuple stored at
    `SYN_SENT`. Rows with no match still go out, with a uid and no pid; where the dump fails,
    the tick's socket stats are null.
  - **Per-process totals, all protocols.** kretprobes on `tcp_sendmsg`/`tcp_recvmsg`/
    `udp_sendmsg`/`udp_recvmsg` (and, from schema 7, the IPv6 UDP pair) give `net_tx_bytes`,
    `net_rx_bytes` and `net_calls`. **This is what makes QUIC visible.** Unix-socket (MCP)
    traffic is excluded by family. TCP `sendfile` and `splice` sends are counted by
    `netbytes_page` before 6.5, and through `tcp_sendmsg` from 6.5.
  - **Connect latency** comes free from the same tracepoint: `SYN_SENT`→`ESTABLISHED` is
    `connect_ms`, and `SYN_SENT`→`CLOSE` without `ESTABLISHED` a refused or timed-out connect.

**4. Wait channel and kernel stack.** `d_stack` on `residency`, each tree's top 5 blocking
frames, and `d_stack_top` on `residency_totals`, the node's top 10: for each tracked process
in `D` at the tick, the top 5 frames of `/proc/<pid>/stack`, read as root. **Reading a kernel
stack touches no filesystem**, so it cannot hang on the mount being diagnosed. It replaces
`wchan`, which other users' processes do not show. Capped by `EBPFM_DSTACK_MAX`.

**5. Live processes at stop.** On SIGTERM, one `truncated` record per live tracked process,
after draining the D-state and network-byte maps, so it carries accumulated kernel counters
rather than only what `/proc` shows at that instant. SIGKILL is unrecoverable; a missing
`stop` record already marks a file as cut short. A process that cannot be read is skipped and
counted in the stop record's `handler_errors`, and a flush that fails as a whole leaves
`truncated` null, not 0. The residency tick does the same per process.

**6. Unattributed user processes.** A non-system uid with no agent ancestry and no tty is
still a user's process, and **`EBPFM_MIN_UID` is the discriminator, not the cgroup**, which
depends on how a session was set up (tty-attached human processes sat in
`/system.slice/ssh.service`, another user's VS Code in a `user-1000.slice` session scope).
`attribution` on the identity block is `ancestry`, `tty` or `uid`, so the recovered set,
which includes the orphaned `bwrap` (reparented to init, no tty) that is the wedge's
signature, can be filtered while the three `actor3` classes (`agent`, `human`, and
`human-vscode`, which the dashboard calls `ide`) stay intact. It is **off by default**
(`EBPFM_MIN_UID=0`) and stays off: it is the one setting that changes what an existing class
*means*, so turning it on would make the human population incomparable with every earlier
capture. If it is ever on, filter on `attribution`.

**7. Per-agent-type totals.** `by_agent_type` in `residency_totals`, with `{trees, n_procs,
threads, rss_mb, cpu_s, d_state, users, autonomous}` per type, aggregated from what the
collector already holds. It sums to the agent row of `by_actor3`.

**8. Inbound connections.** Netlink (3) lists established inbound sockets with a uid but no
pid. Attribution needs the kretprobe on `inet_csk_accept`, which returns the new socket **in
the accepting process's own context** (the inbound `SYN_RECV`→`ESTABLISHED` transition runs in
softirq, where the current task means nothing). The connection folds into that process's
`conns` block, and seeds the birth map, so it gets a proper close too.

---

## Design principles

1. **Event-driven for anything that happens; sampled only for anything that persists.**
   Per-process events are exhaustive. Only the residency view samples, because asking what
   is alive *now* requires sampling. Every sampled path is bounded.
2. **Keep userspace syscalls off the per-event path.** This is why cwd is inherited by
   default rather than read from `/proc` on every exec.
3. **Never dereference a path read from `/proc`** (see data point 1).
4. **Every kernel read is a probed block.** A kernel that cannot compile or load a block
   drops *that block alone* and records why in `meta.features_rejected`. A dropped block's
   fields are `null`, **never `0`**, because an absent measurement must not look like a zero
   one. Results are cached per kernel (`$EBPFM_OUTPUT_DIR/.features_<release>.json`); a block
   refused by a load or BTF error is probed again at every start, and the cache is ignored
   when the block list, the BCC version, bpftool or the kernel's BTF, or the program text
   with the settings compiled into it changes. `bootstrap` deletes it.
5. **`perf_lost` is the acceptance gate** for anything added to the tick.

---

## `ebpfm.sh` verbs

| Verb | Does |
|---|---|
| `bootstrap` | install missing dependencies, set the required sysctls, delete the feature cache, re-run `check` |
| `check` | privilege, toolchain, kernel config, sysctls, tracepoints, kprobe symbols, netlink, its own cgroup and that cgroup's `cpu.max`, the node tier's tools, vendored-file divergence; exit code = FAIL count |
| `features` | probe the BPF blocks, print the resolved set as JSON |
| `dump-c` | print the generated BPF C (for debugging a rejected block) |
| `once [secs]` | foreground capture (30 s by default) |
| `start` / `stop` / `status` | the tiers in the background, as in *Supervision*; `status` gives each stream's records and size today |
| `overhead` | the one-off measurement above |
| `tail [n] [stream]` | the last n records (3 by default) of today's files |
| `install-unit` / `uninstall-unit` | install or remove `ebpfm.service` and `ebpfm-node.service`, with this folder's paths filled in (the RPM ships `ebpfm.service` itself) |
| `bundle [out]` | the runtime files, `README.md`, `VENDORED.md` and `.vendored.sha256` as one self-extracting `.sh` |

### Bootstrap

1. On Ubuntu, adds `universe` to the existing apt sources (deb822 stanzas edited in place,
   **not** `add-apt-repository`, which would itself need `software-properties-common`).
   Without universe there is **no apt candidate for `python3-bpfcc`**, which is the failure a
   fresh node actually hits.
2. Installs whichever of `python3-bpfcc`, `linux-headers-$(uname -r)`,
   `linux-tools-$(uname -r)` (for `bpftool`), `iproute2` and `jq` are missing. On RHEL and
   Rocky, `dnf` (or `yum`) does the same with `python3-bcc`, `kernel-devel-$(uname -r)`,
   `bpftool`, `iproute` and `jq`.
3. Sets `kernel.sched_schedstats=1` and `kernel.task_delayacct=1`, persisted to
   `/etc/sysctl.d/99-ebpfm.conf`. **Two of the eight data points are inert without these.**
4. Deletes the feature cache and re-runs `check`, exiting non-zero on any FAIL.

It is idempotent.

### Environment

Where the day-files go, unless `EBPFM_OUTPUT_DIR` (eBPF tier) and `EBPFM_NODE_OUTPUT_DIR`
(node tier) say otherwise: as root, `/scratch/ebpfm` and `/scratch/ebpfm-login` where
`/scratch` is writable, else `/var/log/ebpfm` and `/var/log/ebpfm-login`; as a user,
`~/.ebpfm/login` (and `~/.ebpfm/ebpf`). An installed unit sets `EBPFM_OUTPUT_DIR` to the
folder `install-unit` resolved, and the RPM's to `/scratch/ebpfm`. An `EBPFM_OUTPUT_DIR` in
`/etc/default/ebpfm` overrides it (systemd's `EnvironmentFile=` wins over `Environment=`),
but the unit's `RequiresMountsFor=/scratch`, and `ebpfm status` and `tail`, which read their
own environment, do not follow it.

`ebpfm.sh`: `EBPFM_TIER` (`node`, `ebpf` or `all`; by default, by privilege) ·
`EBPFM_PID_DIR` (`/run`; a user's `~/.run`) · `EBPFM_NODE_INTERVAL` (300 s between node
snapshots) · `EBPFM_NODE_TOP_N` (25) · `EBPFM_BENCH_REPS` (3, for `overhead`) ·
`EBPFM_UNIT_DIR` (`/etc/systemd/system`, for `install-unit`) · `EBPFM_DIR_MODE` /
`EBPFM_FILE_MODE` (0750 / 0640) and `EBPFM_GROUP` (a group to own them), read by `ebpfm.sh`
and the eBPF tier.

The eBPF tier (`ebpf_trace.py`):

- **run:** `EBPFM_OUTPUT_DIR` · `EBPFM_DURATION` (0: until stopped) · `EBPFM_RESIDENCY_S`
  (60; 0: no residency tick) · `EBPFM_POLL_MS` (100) · `EBPFM_PERF_PAGES` (256 per CPU) ·
  `EBPFM_FLUSH_MODE` (`poll`; `record` flushes every record) · `EBPFM_FLUSH` (on: write live
  processes as `truncated` at stop; not to be confused with `EBPFM_FLUSH_MODE`) ·
  `EBPFM_COMPRESS` (on: a sealed day's files become `.jsonl.gz`; `0` leaves them plain) ·
  `EBPFM_REORDER_MS` (20; how far behind kernel time events are applied) ·
  `EBPFM_EXIT_HOLD_MS` (250; `0` writes exit records inline and loses the connections that
  close after the exit event) · `EBPFM_GC_GRACE` (300 s an exited process's state is kept)
- **argv and Slurm:** `EBPFM_ARGS_MAXLEN` (0: no cap on emitted `args`) · `EBPFM_ARGV_KMAX`
  (4096; bytes of argv read in-kernel at exec, the real limit when uncapped) ·
  `EBPFM_REDACT` (`keys,prompts`; `0` writes argv raw) · `EBPFM_WRITE_KMAX` (1024; bytes
  captured per `write()`) · `EBPFM_SUBMIT_COMMS` (`sbatch,salloc,srun`: the tools whose output
  `submit` captures) · `EBPFM_SUBMIT_BUDGET` (8192; bytes of that output captured)
- **which processes:** `EBPFM_MIN_UID` (0: off; data point 6) · `EBPFM_INCLUDE_ROOTS` (on;
  `0`: no record for an agent's own process) · `EBPFM_MIN_CPU` / `_DURATION` (0) ·
  `EBPFM_KEEP_TIMERS` (off: a `sleep` that is no agent and used no CPU or I/O is not written;
  `EBPFM_TIMER_COMMS`, `sleep,usleep`, names them)
- **per-process reads:** `EBPFM_CWD` (on) · `EBPFM_CWD_ALWAYS` (off; a readlink on every
  exec, to validate) · `EBPFM_SANDBOX` (on) · `EBPFM_SANDBOX_ALWAYS` (off; a read on every
  exec, to validate) · `EBPFM_RESIDENCY_TOPN` (5) · `EBPFM_DSTACK_MAX` / `_DEPTH` (300 reads a
  tick / 5 frames)
- **network:** `EBPFM_CONN_ALL` (off: external sockets only) · `EBPFM_CONN_UNTRACKED` (on:
  netlink rows with no pid) · `EBPFM_TCP_ALL` (off; on also folds connections of untracked
  processes) · `EBPFM_CONNS_TOPN` (5) · `EBPFM_NETPEER_MAX` (65536; the kernel endpoint
  table) · `EBPFM_NET_ENDPOINTS_MAX` (256; endpoints listed per record) · `EBPFM_NETIO` (on;
  `0`: no `netio` series, `net_endpoints` kept) · `EBPFM_ARGV_HOSTS` (on; `0`: no `host` names
  from argv)
- **blocks:** `EBPFM_FEATURES` (`auto`, or a pinned block list) · `EBPFM_FEATURE_CACHE` (on)
  · `EBPFM_PROBE_VERBOSE=1` (print the compiler line for a rejected block)

---

## Tests

```bash
python3 -m unittest discover -s tests -q     # no root, no BPF needed
python3 tests/bpf_compile_check.py           # compile every block with BCC, no root
```

`bpf_compile_check.py` runs BCC's real frontend and LLVM over the generated program, block by
block as `probe_features()` would, supplying the tracepoint argument structs itself (tracefs
is root-only). It shows what the rewriter and the 512-byte stack limit accept on this host's
headers; only an actual load shows what the verifier does.

The unit tests hold all of the collector's Python, the Python inside `ebpfm.sh` included, to
3.6 syntax (EL8's). The schema-7 network program is also compiled with the host C compiler
against `tests/bpf_mock.h` and driven with fake sockets (skipped without a C compiler). That
is not BPF: only `features` on a real kernel shows whether the program loads and attaches,
and the `netpeer_hdr` variant is only checked as text.

## Kernel support

Developed against **6.8** (Ubuntu 24.04) and **6.17**. FASRC login nodes run **4.18**, where
`dstate_task` (5.19+) is rejected and `dstate_stat` is the path for data point 2, which is
why both blocks exist and are cross-checked. `tcp_accept` and `netpeer_btf` need the kernel's
BTF and `bpftool`; `netpeer_hdr` is the endpoint table's path without them. Run `features` on
the target kernel before trusting any field, and use `EBPFM_PROBE_VERBOSE=1` to see why a
block was rejected.

## Relationship to the other collectors

This bundle is for **login nodes only**. Compute-node collection is out of scope: it stays
with the measurement repository's `collect/compute/`, which runs `collect/common/snapshot.py`
unchanged, in `full` mode. Of that repository's login-node collectors, it **retires**
`collect/agents/` (the eBPF tier's `exit` records are a declared strict superset of
`proc_trace.py`'s, and `residency`/`residency_totals` replace the census, at 60 s instead of
~300 s) and `collect/nettcp/` (replaced by `exit.conns` and `conn`), both behind
`EBPF_LIVE=1`; it
**supersedes** the earlier eBPF collectors `collect/agents_ebpf/` (v1) and
`collect/eBPF_marthen/` (v3, which has only `dstate_task`, and so no D-state dwell at all on
4.18); and it **narrows** `collect/login/`, whose snapshot runs here as the node tier, with
`log/login`'s schema and path unchanged.
