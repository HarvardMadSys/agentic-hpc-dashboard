# Live agent-behaviour dashboard

A FastAPI service that tails the [`ebpfm`](../collector) collector's JSONL,
keeps rolling aggregates over a 24-hour window, and pushes them to a React page over a
WebSocket. Every number on the page is measured: a feed with no data names the path it
expected instead of showing a plausible figure, and a metric the collector could not
populate reads **not measured** rather than `0`.

This replaces a static-HTML demo, retired from the tree and recoverable in git history
under `dashboard/archive/`. That demo generated most of its own numbers (`rc_synth.py`),
and its viewer manufactured new events between polls; nothing here does either. `grep -r Math.random rc_dashboard web/src` returns
nothing, by design and by CI-able assertion (`npm run check:no-random`).

## Run it

```bash
cd dashboard && uv sync
uv run python -m rc_dashboard --check-feeds     # what resolved, what didn't, and why
cd web && npm ci && npm run build && cd ..      # builds web/dist/, which the service serves
uv run python -m rc_dashboard serve --port 8080
```

Off-cluster, `--check-feeds` exits 1 because the eBPF feed is `required` and absent. That is
the point: point it at a collector root first.

```bash
RC_DASH_EBPF_ROOTS=/var/log/ebpfm \
RC_DASH_NODE_ROOTS=/var/log/ebpfm-login \
uv run python -m rc_dashboard serve
```

Reach a remote instance over a tunnel: `ssh -L 8080:127.0.0.1:8080 <host>`.

## Configuration

Every source is configurable, resolved **per key** in the order
**CLI flag > `RC_DASH_*` env var > config file > built-in default**. A flag you don't pass
never outranks the file.

- Copy [`dashboard.config.example.json`](dashboard.config.example.json) to
  `dashboard.config.json` (gitignored) and edit. `//`-prefixed keys are comments, stripped on
  load, so the example file is itself a legal config.
- `--print-config` prints the resolved tree with a `_origin` per key, so "why is it reading
  *that*" is never a guess.
- Path defaults are **candidate lists**, probed against the filesystem. `--check-feeds` shows
  every candidate and which one won.

JSON rather than TOML: the format is shared with the rest of the repo (every collector writes
JSONL; the retired builder's contract was `data.json`) and `--print-config` has to round-trip it.

## The two feeds

| feed | what it carries | default candidates |
|---|---|---|
| `ebpf` (**required**) | the root tier: per-process exit, residency, tcp/conn, submit | `$EBPFM_LOG_ROOT`, `$EBPFM_OUTPUT_DIR`, `$RC_MEASUREMENT_ROOT/ebpfm/logs`, `log/ebpfm`, `/var/log/ebpfm` |
| `ebpf_node` | the unprivileged node tier: load, memory, NFS, per-user cgroup | `$EBPFM_NODE_LOG_ROOT`, `…/ebpfm/node/logs`, `/var/log/ebpfm-login`, `log/login` |

`log/login` is last but load-bearing: `node_snapshot.py` is a vendored copy of
`collect/common/snapshot.py`, so the existing login feed is schema-compatible and works before
`ebpfm --node` is deployed anywhere.

Reading the collector's output uses a **separate** env var from the collector's own
`EBPFM_OUTPUT_DIR`, following `collect/healthcheck.py`: sharing one would let an operator shell
silently repoint the readers along with the writer.

## Why a 24-hour window is not just a bigger number

At ~1.5 KB per exit record, a login fleet produces on the order of 10–20 GB/day, so a day of
raw events cannot sit in memory. Three bounded paths instead:

| path | window | storage |
|---|---|---|
| rolling aggregates | full 24 h | per-minute buckets — 1440 slots × a few counters, tens of KB. Every chart reads these. |
| event stream | last `live.event_tail` (500) | a bounded tail, **not** 24 h |
| search / export | any | on-demand re-scan of the day-files, streamed, never buffered |

Trajectories work at this window because per-session state is incrementally composable:
records arrive in time order, so counters and the run-length-encoded tool chain update in
place. Idle sessions are evicted and the count reported.

Quantiles, CCDFs and the dedup index are **not** resumable from a byte offset, so those panels
rebuild on a schedule and publish `full_built_at` — their age is visible, not implied.

## API

| endpoint | purpose |
|---|---|
| `GET /api/config` | resolved config, `_origin` per key |
| `GET /api/feeds` | per feed: configured vs resolved paths, status, lag, rows, notice, backfill |
| `GET /api/panels` | historical aggregates + `full_built_at` |
| `GET /api/live` | rolling window: per-minute bins, plates, hosts, event tail, submits |
| `GET /api/trajectories?rank=` | ranked sessions |
| `GET /api/events?<filters>` | paged event query |
| `GET /api/export?<filters>` | **streaming filtered raw JSONL** |
| `WS /ws` | push: `live`, `feeds`, `rebuilt`, `backfill` |

Filters are shared by `/api/events` and `/api/export`, so **the download is exactly the view**:
`from to class[] agent_type[] user[] host[] tool[] purpose[] bucket[] session_key sandbox
approval depth_min depth_max cpu_min duration_min exit_code signal q`.

The export's first line is a `_meta` header (filters, window, feed paths, schema versions) and
its last is a `_trailer` (row count, whether the cap truncated it). Body rows are the
collector's own fields plus additive `_`-prefixed derived keys — originals unmodified.

## Conventions the panels obey

These are research-integrity rules, not preferences. Each one is a mistake this project has
already made once.

- **Three classes, never two** — `agent` / `human-vscode` / `human` (+ `unlabeled`). Folding
  VS Code into "agent" is what produced a retracted GPU near-parity claim. `actor3` is
  *derived* via `agent_lib.actor3`, never approximated from the binary `actor`, which would
  silently empty the `human-vscode` class.
- **Count and cost, side by side** — every categorical panel ships an event-weighted and a
  CPU-weighted chart on separate single axes. Never a dual axis. You can see why immediately:
  `poll/probe` is ~28% of an agent's tool calls and ~0% of its CPU.
- **Null is not zero** — a dropped BPF block emits `null`. Nullable fields carry
  `coverage_pct` and are never imputed. A field present-but-always-exactly-zero is flagged
  `all_zero` and renders **not measured**.
- **Zero is not missing** — a present feed with no matching rows says `none`; an absent feed
  names its configured path. Different states, different renderings.
- **`sandbox` and `approval` are independent axes.** `--dangerously-skip-permissions` bypasses
  approval but leaves the sandbox intact; `--dangerously-bypass-approvals-and-sandbox`
  disables both. `sandbox` has **four** states — `denied` (unreadable, ptrace-gated for other
  users when the collector is unprivileged) must never be folded into `unsandboxed`, which
  would deflate the sandbox rate for everyone except the collector's owner.
- **Ancestry disagreeing with ground truth is the signal.** `sandbox_ancestry=bwrap` with
  `sandbox=unsandboxed` is Claude Code's capability probe (`bwrap --ro-bind / /`), the shape
  that wedges in D-state on a dead automount per `findings/login.md`. With
  `sandbox=sandboxed` it is real confinement.
- **CPU is `sum(cpu_s)`, self, once per process.** `child_cpu_s` is never added:
  `analyze/common.py:364` measures the naive sum inflating login CPU 1.83× and *unevenly by
  class*, which would invert an agent-vs-human comparison. `schedstat_ratio` is emitted so a
  regression in that basis is visible on the page.
- **"Top" is four rankings, not one** — `events`, `cpu_s`, `distinct_tools`, `chain_runs`,
  each with `dominant_user_share_pct`, because a single ranking is dominated by one poll loop
  and one mega-user.
- **Scope is stated** — the eBPF tier runs on login nodes, so its resource figures are
  login-node use, not cluster use.

## Known collector limitations the page surfaces

- **`peak_rss_mb` is always 0** on kernel 6.8: the kernel runs `exit_mm()` (clearing
  `task->mm`) before the `sched_process_exit` tracepoint fires, so the `if (mm)` guard at
  `ebpf_trace.py:834` never assigns. Reading `task->signal->maxrss` instead survives
  `exit_mm()` (verified). Until then the metric renders **not measured**.
- **The node tier daemon needs an absolute path.** `ebpfm.sh:466` re-execs `"$0"`, so
  `bash ebpfm.sh start` leaves the nohup'd child unable to find itself. Invoke it by absolute
  path.
- **`submit` request fields are argv-only.** A job whose `#SBATCH` directives carry the
  request reports `null` for `partition`/`gpus`/`array`; `req_src` says which. Recovering them
  needs a `slurm_jobs` census join, which is not a configured feed.
- **Scheduler and GPU panels are absent, not zero.** `running_agent_jobs` and GPU duty need
  the census and `dcgm`. The Live tab shows *agent job submissions in the window* from eBPF
  `submit` records instead — per-submission and actor-attributed, which is better evidence
  than a census count.

## Layout

| path | role |
|---|---|
| `rc_dashboard/config.py` | per-key precedence, candidate probing, `--print-config` |
| `rc_dashboard/feeds.py` | resolve + report every feed; ports `healthcheck.check_target` / `last_timestamp` |
| `rc_dashboard/tail.py` | `IngestSource`; offset-resume file tailer (a push source drops in here) |
| `rc_dashboard/normalize.py` | schema contract, `actor3`, sandbox/approval, session keys, null gating |
| `rc_dashboard/buckets.py` | rolling per-minute aggregates |
| `rc_dashboard/reducers/` | tools, io, resources, sandbox, trajectories, live, node |
| `rc_dashboard/sacct.py` | `collect/slurm_query.bash` exports → per-job rows, generation-merged |
| `rc_dashboard/export.py` | shared filter grammar + streaming export |
| `rc_dashboard/app.py` | FastAPI: REST, WebSocket, static mount |
| `web/` | React + Vite + TS; `dist/` is what the service serves |

## Ingestion, precisely

`DailyWriter.write()` (`ebpf_trace.py:1010`) writes then flushes, which is not atomic against
a concurrent reader. So the tailer reads to EOF, finds the last newline, yields only complete
lines, and **advances its stored offset only that far** — trailing bytes are re-read next
cycle. Idempotent, never a truncated JSON line, never a lost record.

Offsets are keyed on `(st_dev, st_ino)`, not path, so a new date directory cold-starts instead
of inheriting yesterday's offset, and a recreated file is detected as a new inode rather than
silently seeked past its end.
