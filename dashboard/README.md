# Live agent-behaviour dashboard

A FastAPI service that tails the [`ebpfm`](../collector) collector's JSONL,
keeps rolling aggregates over a **selectable** window, and pushes them to a React page over a
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

## Two deployments on this host

| script | port | data | clock | risk profile | Slurm |
|---|---|---|---|---|---|
| `run-dev.sh` | 8086 | this dev cluster's live feed, `/scratch/marthen/ebpfm/{ebpf,login}` (rtx6000a) | wall | `gpu-dev.json` | off |
| `run-fasrc.sh` | 8085 | FAS RC capture, `/scratch/marthen/fas-rc-test-data` (boslogin06, 2026-10-08..09, `.jsonl.gz`) | pinned to the capture's end | `fasrc.json` | on |

Both hot-reload as `./run.sh build`, from the same tree. `site.name` (`RC_DASH_SITE_NAME`) is
shown under the sidebar title and in the browser tab, so a frozen capture and a live feed open
side by side are never mistaken for each other. `slurm.enabled` (`RC_DASH_SLURM=0`) turns the
Slurm tab off and skips reducing it, for a cluster without Slurm; the Overview's "asked of Slurm"
rows go with it.

The FAS RC `sacct` export (`/scratch/marthen/agentic-hpc-measurement/raw_data/sacct`, one parquet
file a day, 2026-08-23..09-30) is not read: the service is stdlib-only and has no parquet reader,
and those days overlap neither deployment's eBPF data, so no job id the collector saw can be
joined to them.

## A capture from another cluster

A feed collected elsewhere and copied here -- `run-fasrc.sh` serves boslogin06's, on `:8085`
with the same hot reload as `./run.sh build` -- differs from a live one in two ways, and the
service handles both without touching a record.

**It is compressed.** A day-file may be `<host>.<stream>.jsonl.gz`, read in place
(`rc_dashboard/gz.py`). Every reader here addresses a file by byte offset (the window plan's
binary search, the read-back that walks from the end, a saved window's resume, the tail),
and gzip offers none of that, so the first open inflates the file once and keeps a copy of the
inflater every 8 MB of output. Reaching any offset then inflates at most 8 MB, and offsets are
into the uncompressed stream, so a byte range or a saved build means the same thing for either
form. On boslogin06's 2026-10-08..09 (330 MB -> 7.9 GB) indexing takes ~6 s and holds ~32 MB;
it lives in the process, because an inflater cannot be pickled, so each restart indexes again.
A plain file wins over its `.gz` twin, concatenated members are followed, and a stream cut
short is read as far as it goes. A date dir holding no day-file is passed over rather than
counted as a day: a copied capture has dirs made ahead of the files, and counting them left
the 1 h view of the capture nothing to read.

**It is old.** On the wall clock every window would end in days of nothing. `clock.as_of`
(`RC_DASH_AS_OF`, `--as-of`) pins the service's present: `auto` is one second after the newest
record, or give a local time. The pinned clock is **frozen** (`rc_dashboard/clock.py` says why:
a running one would date every saved build in the future after a hot-reload restart, and
re-scan instead of resuming). Every live frame carries `clock`, the header reads *frozen
capture · as of ...* instead of *live*, and the page takes "now" from the server, not the
browser, so the events query, the export bounds and every "ago" agree with the windows.
Durations the service measures with that clock read 0 s.

### Testing against a capture

The pinned view is deterministic: the same windows over the same bytes on every restart. That
is what makes a capture useful as a test, at four levels:

| level | what it checks | state |
|---|---|---|
| the pinned view (`run-fasrc.sh`) | every tab on another cluster's data: its users, its kernel's feature set (4.18: no node tier, no `dstate_task`, no `tcp_accept`) rendering *not measured*, never 0 | running |
| scale | index, backfill and window-build cost at that cluster's volume -- boslogin06 writes ~2.7 M exit records a day, about four times rtx6000a | measured on the instance (`/api/feeds` `compressed`, `/api/windows`) |
| cross-check | a few totals counted straight from the `.gz` by an independent script (exits per class per hour, distinct users, agent sessions) against `/api/live` and `/api/panels` at the pinned clock | proposed |
| replay | the live path the frozen view never runs -- the tail following appends, WebSocket pushes, drift re-builds, the build/tail seam -- by writing the capture into a plain staging root at N x speed while the service's clock runs from the replay's start | proposed |

## Is each collector OK?

The **Feeds** tab leads with one row per host and a verdict an admin can act on, judged
only from what the collector wrote (`rc_dashboard/health.py`) -- the dashboard never
contacts a collector, so a capture copied from another cluster is judged exactly as a live
feed. The collector writes a `meta` record when it starts (pid, kernel, the probes that
loaded and the ones that did not, the sysctls they need), a `residency_totals` heartbeat
every minute, and a `stop` record when it exits cleanly (why, how long it ran, and kernel
events lost against seen).

| verdict | means |
|---|---|
| OK | a heartbeat within `feeds.ebpf.max_age_s` (5 min) |
| degraded | running, but losing data it could have: a sysctl a probe needs is off, or its last run lost more than 0.1% of kernel events |
| stopped | no recent heartbeat and a stop record after the last one; its reason is shown |
| silent | no recent heartbeat and no stop record: killed, crashed, or the host is down |
| no heartbeat | files for the host, but no heartbeat in what was read |

A probe the kernel cannot load (boslogin06's `tcp_accept`: no BTF on 4.18) is listed as a
**limit** beside the verdict but does not change it -- it is true of every run until the
kernel changes. A running collector's lost-event count is written only at stop, so the run
in progress shows "reported when it stops", never 0. Below the collectors: the data sources
(an optional one nobody set up reads "not set up", not red), the startup read, and the
resolved configuration.

## The time window

The window is chosen on the page — presets plus a custom entry (`90m`, `4h`, `2d`) — and it
lives in the query string next to the class filter, so a pasted link restores the span as
well as the view. It is `?win=<minutes>`; omit it and the service's own `live.window_min`
applies, which is why a bookmark made before this control existed still opens on 24 h.

**Retention and window are different numbers.** `live.retention_min` (default 7 d) is what
the process *holds* and the ceiling the picker can reach; `live.window_min` (default 24 h) is
only the view a fresh page opens on. Raising the view is free. Raising retention costs memory
and a longer backfill.

At ~1.5 KB per exit record a login fleet produces 10–20 GB/day, so raw events never sit in
memory. Four bounded paths:

| path | how a window change is served | storage |
|---|---|---|
| rolling aggregates | **sliced** — exact, instant, no re-read | per-minute buckets, one slot per minute of retention. 7 d ≈ 10 080 slots × a few counters. |
| historical reducers | **rebuilt** from the rows | quantiles, CCDFs, Gini, the dedup index and the trajectory chains carry no time index |
| event stream | cut to the window | a bounded tail of `live.event_tail` (500) — a tail, **not** an aggregate, so a wider window does not deepen it |
| search / export | `from`/`to` pinned to the window | on-demand re-scan, streamed, never buffered |

Slicing is exact because the bins are fixed-width and wall-clock aligned: the 1-hour view is
the last 60 rows of the 7-day view, to the record, and the headline under a chart is the sum
of exactly the bins the chart drew. A window **wider than retention is clamped and says so** —
drawing bins the process never held is the same error as imputing a zero.

Rebuilding costs the *window*, not the *day*. A day-file is appended in time order, so it is
sorted by timestamp and `export.seek_to_epoch` binary-searches the first in-window byte: a
1-hour view of a 15 GB day-file reads ~20 probes and one hour of rows. Files entirely older
than the window are skipped without being opened. The rebuild feeds records **oldest first**,
which is a correctness requirement and not a preference — the tool-mix shell-wrapper dedup
relies on a child's exit arriving before the shell that waited on it, and a trajectory's
run-length-encoded chain is appended in place.

Three things a rebuild reports rather than assumes:

- **Progress.** A wide window takes minutes, so panels render a *third* kind of nothing —
  neither `none` (which would assert a measurement) nor the empty state (which would blame
  the feed) — with a real percentage.
- **Drift.** A built view keeps ingesting, so it holds more than its own window. The excess is
  published in seconds and the view re-scans at `panels.max_drift_pct` (25%) of the window —
  proportional, so 1 h refreshes often and cheaply and 7 d rarely and expensively.
- **The seam.** A build reads the day-files while the tailer is also advancing through them.
  Rows landing in that seam are buffered and replayed; if the buffer overflows the count is
  published as `replay_dropped` rather than quietly going missing.

One pre-existing inconsistency had to be fixed for the control to mean anything: trajectory
sessions were evicted at `reducers.trajectories.idle_evict_s` (2 h) while the panel's own
`window` block claimed 1440 minutes, so "top trajectories in the window" actually ranked
sessions active in the last two hours. Eviction now never reaches inside the window
(`max(idle_evict_s, window)`), which makes the label true and makes
`reducers.trajectories.max_sessions` (50 000) the real memory bound — worth lowering if you
raise retention on a busy fleet, since it applies per held window.

Built windows are an LRU of `panels.max_windows` (2), and builds are serialised on one worker:
two concurrent rebuilds would contend for the same page cache and each make the other look
slow. Ten readers on the same window cost one build, because the WebSocket sends one frame per
*distinct* window rather than one per client. The default window starts building with the
service, not with the first page load.

Two things keep a build from costing more than it must:

- **Only the records the panels read are parsed.** Every historical reducer reads `exit` and
  `truncated` and nothing else, so a scan skips the other ~70% of a day-file's lines (residency,
  connections, sockets) on a byte match before `json.loads`. The match cannot miss a record — any
  JSON writer emits `"event": "exit"` — and a false hit only costs a parse.
- **Every build is saved, and resumed rather than repeated.** At the end of each scan the
  reducers are pickled to `<cache.dir>/windows/` together with exactly how far into each day-file
  the scan read: `(st_dev, st_ino)`, a digest of the first 4 KB, and the offset past the last
  complete line. A restart (and `run.sh` restarts on every save), or a return to a window the
  LRU evicted, loads that and reads each file on from its offset — only what the collector wrote
  since. On one login node's feed a 24 h window goes from ~7 s to ~0.2 s and a 7 d window from
  ~42 s to ~1.4 s, plus the catch-up. A snapshot is ~12 MB per day of window; `panels.cache_keep`
  (8) bounds how many are kept.

A resumed view holds the records a service that had never stopped would hold, and reproduces a
fresh scan's panels exactly — checked on real data, and in `tests/test_window_cache.py`. It keeps
its original `built_at`, so its age and its drift read as they would had nothing restarted; the
catch-up *is* drift, and re-scans at the same budget. The snapshot is ignored, and the window
scanned from scratch, unless all of these hold — and the window status's `cache.note` says which
one failed:

- the same code and config (a fingerprint of the reducers, the normaliser, the scan and every
  config key outside `server`) — changing how a record is counted must never resume a reduction
  counted the old way;
- the same files: every one it read still there, the same inode *and* the same first 4 KB (a
  file deleted and rewritten can get its inode number back), at least as long, a line ending at
  the saved offset;
- built within the window's drift budget. An older one would be re-scanned the moment it caught
  up, so catching it up would only add to the cost.

A drift re-build always scans: it exists to move the window's start forward, which an older copy
cannot do. Pickle runs code for whoever can write the file, so the directory is created 0700,
files 0600, and a symlink or a file another user owns or can write is never loaded.
`panels.cache_windows=false` (`RC_DASH_CACHE_WINDOWS=0`) turns all of it off.

### Hourly merge-reduce

With `panels.merge_reduce` on (the default; `RC_DASH_MERGE_REDUCE=0` turns it off), a record is
parsed and reduced **once**, into the state of the epoch hour it belongs to, and that state is
saved under `<cache.dir>/hours/` (`hours.py`). An unfiltered window is then the records from its
start to the first whole hour, read as before, so the start is exact rather than snapped to an
hour; every closed hour inside it, merged from their saved states (whole epoch days from one saved
merge of their 24 hours, `panels.day_cache`); and the hour still open, read as before. The view
is live-fed as always. Every window reducer has `merge(other)`, which folds a later state into its
own exactly as feeding it the later records would have, so a merged window is the window one pass
builds (`tests/test_merge.py`, `tests/test_hours.py`; checked on the real feed). The quantile panels
are now a mergeable sketch of every value rather than a capped sample: `n`, `max` and `mean` exact,
each quantile within 1% (`rel_err`), `sampled` false.

Joins that cross an hour -- the shell-wrapper dedup, the process index's parent requests, a
sandbox wrapper absorbing its payload, the keys the workspace has admitted -- are carried from
each hour into the next one's reduction, so the hours are reduced in order, and a merged window
takes the join state its last hour ended with. That carried state is aged out after 24 hours
untouched, which is the one place a merged 7 d window differs from one pass over 7 days: it no
longer holds week-old pending requests, which on rtx6000a made one pass refuse a few legitimate
parents whose pid an older process had used. An hour closes once the newest record is
`panels.hour_grace_s` (300 s) past its end; a record that arrives for a closed hour is found by the
next hour's read and merged into its hour's saved state, not lost. On one login node's feed a
24 h window then builds in ~2-3 s and a 7 d one in ~8 s after a restart, a window switch or a drift
refresh (against ~15 s and ~160 s scanned); filling the hours the first time costs about what one
scan does. A week of hours is ~38 MB on disk. A view filtered to users or agent types is still
scanned.

## API

| endpoint | purpose |
|---|---|
| `GET /api/config` | resolved config, `_origin` per key |
| `GET /api/feeds` | per feed: configured vs resolved paths, status, lag, rows, notice, backfill |
| `GET /api/collectors` | per host: is its collector OK -- verdict and reasons, heartbeat age, the run in progress, every run and how it ended (events lost), probes that could not load (`health.py`) |
| `GET /api/windows` | offered windows, the retention ceiling, and every held window's build state |
| `GET /api/panels?window_min=&user=&agent_type=` | historical aggregates for that window + its build state; `user` / `agent_type` (repeatable, `none` = untagged) narrow it to a view of its own, built and saved like a window; `filter` names it, `facets` lists the window's users and agent types |
| `GET /api/live?window_min=` | rolling window: per-minute bins, plates, hosts, event tail, submits |
| `GET /api/trajectories?rank=&window_min=&user=&agent_type=` | ranked sessions in that window, narrowed the same way |
| `GET /api/trajectory?sid=&window_min=&from=` | one session as a **process tree**, re-read on demand (below) |
| `GET /api/events?<filters>` | paged event query |
| `GET /api/export?<filters>` | **streaming filtered raw JSONL** |
| `WS /ws` | push: `live`, `feeds`, `rebuilt`, `backfill`, `windows`; accepts `{"type":"window","minutes":N}` |

`window_min` is optional everywhere and omitting it is `live.window_min`, never an error, so a
script or bookmark that predates the control keeps the view it had. A value above retention is
clamped, and the response carries `requested`, `clamped` and a `note` saying why.

`/api/trajectory` (`rc_dashboard/proctree.py`) is the inspector behind a clicked trajectory row.
No reducer holds per-process rows -- one day's busiest session on rtx6000a ran 1.59 M processes --
so it re-reads the window's day-files for exactly the records the row folds (their `_sid`), through
the same normaliser scan a window build uses, with a byte pre-check on `session_key` so the rest of
the file is skipped before parsing. Parents come from each record's own `ppid`; a parent placed on
weaker evidence (the process index, the collector's `depth`, the fold) says so in `link`, and one
with no exit record in the read is drawn without one. Consecutive siblings of one shape fold into
counted groups. Every bound is in the response, never applied quietly: the read stops at
`reducers.trajectories.inspect_max_records` (150 000) or `inspect_max_bytes` and says where
(`scan.stopped_at`; `from=` continues from it), at most `inspect_hold` (20 000) processes and
`inspect_per_parent` (2 000) childless ones of one command per parent are itemised and the rest
counted under their parent, and `inspect_nodes` (6 000) rows are served breadth first with the rest
counted. Command lines are masked (`redact.py`) and then cut to 220 characters. One read runs at
a time and is served again for a minute.

Filters are shared by `/api/events` and `/api/export`, so **the download is exactly the view**:
`from to class[] agent_type[] user[] host[] tool[] purpose[] bucket[] session_key sandbox
approval depth_min depth_max cpu_min duration_min exit_code signal q`.

The export's first line is a `_meta` header (filters, window, feed paths, schema versions) and
its last is a `_trailer` (row count, whether the cap truncated it). Body rows are the
collector's own fields plus additive `_`-prefixed derived keys — originals unmodified,
except credentials.

**Credentials never leave the service.** Tokens, passwords, keys and signed URLs on a command
line are replaced by `[REDACTED]` on every way out (built panels, the live frame,
`/api/events`, `/api/export`, and the `q` search, which matches the masked text), keeping the
flag or variable name so a reader can see what was there. Records stay raw in memory, because
the secrets, sandbox and approval detectors need to read them. The rules and their measured cost
are in `rc_dashboard/redact.py`.

## Conventions the panels obey

These are research-integrity rules, not preferences. Each one is a mistake this project has
already made once.

- **Three classes, never two** — `agent` / `human` / `ide` (+ `unlabeled`). `ide` is any
  editor a person drives: the VS Code, Cursor and Windsurf remote-server trees the collector
  tags (it calls the first `human-vscode` and counts the other two as agents), plus other
  editors' servers recognised by their own executable path (`normalize.ide_class`). An agent
  CLI started in an IDE terminal keeps its own agent type and stays `agent`. Folding the IDE
  class into "agent" is what produced a retracted GPU near-parity claim. `actor3` is
  *derived* via `agent_lib.actor3`, never approximated from the binary `actor`, which would
  silently empty the IDE class.
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
  disables both.
- **`sandbox` is namespace isolation, and it is labelled so.** The collector compares
  `/proc/<pid>/ns/*` with pid 1's: Landlock and seccomp-only sandboxes are invisible to it,
  and a container, an apptainer user namespace or a `PrivateTmp=` unit counts as isolated.
  The key `sandboxed` is shown as "namespace-isolated". It has **four** states — `denied`,
  shown as "exited before read", is the namespace links being gone when read (the ebpf tier
  is root; every such record on rtx6000a lived under 6 ms) and is never folded into
  `unsandboxed`. Every share is taken over the KNOWN verdicts, with coverage beside it.
- **A session is judged by its tool calls.** Claude Code and Codex wrap each tool call and
  never themselves, so the session root's own verdict says nothing about confinement. A tool
  call is a sandbox wrapper invocation (bwrap / codex-linux-sandbox, nested ones folded into
  the outermost) or a Claude Code Bash shell no wrapper encloses; its outcome is `isolated`,
  `not_isolated`, `failed` (the wrapper exited non-zero and no payload ran — tried, and the
  sandbox did not come up: all 612 codex-linux-sandbox runs on rtx6000a), `no_payload` or
  `unknown`. A wrapper's own record is read before it unshares, so it always reads "not
  isolated" and is not a "capability probe"; what ran under it decides the call.
- **Approval is read off the session root's own argv, and says no more than that.** Only a
  process whose role is `agent` (and not an IDE's) carries a verdict, from exit records and
  from its residency / conn / netio identity blocks — a running VS Code-launched Claude is
  only ever seen in its own `conn` records. States: `bypassed` (a bypass flag at launch),
  `auto` (`--permission-mode auto`), `supervised` shown as "no bypass flag at launch"
  (settings files, codex `config.toml` and a mid-session switch are invisible),
  `unobservable` (`codex app-server` takes its policy per thread), `na` (a tool shell, probe,
  IDE helper or human process) and `unknown` (a child: its verdict is on the root).
- **CPU is `sum(cpu_s)`, self, once per process.** `child_cpu_s` is never added:
  `analyze/common.py:364` measures the naive sum inflating login CPU 1.83× and *unevenly by
  class*, which would invert an agent-vs-human comparison. `schedstat_ratio` is emitted so a
  regression in that basis is visible on the page.
- **"Top" is four rankings, not one** — `events`, `cpu_s`, `distinct_tools`, `chain_runs`,
  each with `dominant_user_share_pct`, because a single ranking is dominated by one poll loop
  and one mega-user.
- **Scope is stated** — the eBPF tier runs on login nodes, so its resource figures are
  login-node use, not cluster use.

## Risk rules differ by cluster

What counts as risky is a site's call: nvidia-smi is a pointless question on a GPU-less login
node and a sensible one on a GPU dev box, compute nodes are named differently everywhere, and a
site has its own tools and token formats. The rules are built in (`reducers/risk.py`, which
documents every key) and changed per cluster in the config's `risk` section, or in a **profile**
file merged over it: `RC_DASH_RISK_PROFILE=risk-profiles/gpu-dev.json`.

```json
{
  "vars": {"compute_hosts": "holy|bos|node|gpu|c\\d{3,}"},
  "dangerous": {
    "disable": ["nvidia_smi_login", "nvcc_login"],
    "override": {"pip_install": {"severity": "low"}},
    "add": [{"id": "rclone_sync", "label": "rclone sync on login", "severity": "med",
             "pattern": "(?:^|/)rclone\\b[^\\n]*\\bsync\\b"}]
  },
  "secrets": {"add": [{"id": "site_token", "label": "site token", "severity": "HIGH",
                       "pattern": "\\bRCT-[A-Z0-9]{12}\\b"}]}
}
```

`{{name}}` in a pattern is a `vars` entry. A changed pattern, an added rule, or a variable that
changes a pattern runs without the built-in's pre-check (a cheap literal test that a stale one
would turn into missed matches). A rule that does not compile is left out and named on the Risk
tab, never fatal. A secret pattern a cluster adds is also **masked** everywhere the page shows a
command line (`redact.extend`), so a credential the panel counts is never shown elsewhere. The
profile's content is part of the resolved config, so changing a rule re-scans the saved windows
instead of resuming counts made by the old one. The Risk tab's **Rules in effect** lists every
rule, its pattern, and whether it is built in, changed, added or switched off.
The tab itself is modular: `risk.sections` lists its sections in order -- compute on a login
node, groups of command rules, credentials -- so a cluster drops the ones it has no use for (an
unlisted section is not computed) or splits rules into sections of its own.
`risk-profiles/fasrc.json` (FASRC login nodes; `run-fasrc.sh` uses it) and `gpu-dev.json`
(a GPU box that is also the login node) are shipped; **[`risk-profiles/README.md`](risk-profiles/README.md)**
documents every key and every built-in rule, and [`example.json`](risk-profiles/example.json) uses
every key, annotated.

## Agents: subagents and blast radius

The **Agents** tab (`?tab=agents`; the old `?tab=cascades` still opens it) answers "which
agents started which other agents, and where did each one work". A node is a process whose
role (`roles.py`) is `agent`, `infra` or `client`; tool-call shells, sandboxes, probes and
helpers are steps between nodes. A node's parent is the nearest node above it in the process
tree, else the daemon's own `--spawned-by {"pid": N}`, else (for a `bg-pty-host` orphaned to
init) its user's only daemon, labelled as inferred, else the tree root. An agent started,
through any number of steps, beneath another agent is that agent's **subagent**. Two outcomes
are kept apart: **nested**, an agent with subagents, and **hosted**, a daemon, Claude
Desktop's remote server or `claude rc` running sessions side by side. A Codex TUI whose
direct child is `codex app-server` is shown as that session's front-end, not as a second
agent.

What it cannot see: Claude Code's Task-tool subagents and workflow agents run inside their
parent's process. Only agents that are processes of their own appear; an in-process
subagent's commands are counted in its parent's. On madsys-gpu1 over 2026-10-01..07 that was
49 agent trees, 4 hosted and none nested.

**Workspace & blast radius** (`reducers/workspace.py`, composed per agent by the cascades
reducer) is where each agent worked, from its **home** -- the agent process's own cwd, or,
when that is `/` (IDE-hosted), its first call's directory off the root, labelled so. The
collector records no file paths, so three kinds of evidence are kept apart on the page and
in the payload's `basis`:

| reading | kind | what it is |
|---|---|---|
| ran in | **measured** | processes whose cwd the collector recorded as that directory. It reads cwd at exec for some processes and copies the parent's at fork for the rest -- before a shell's own `cd`, and before the chdir Claude Code's Bash tool makes into the directory its last call left it in (on rtx6000a 1 173 of 1 180 tool-call shells on 2026-10-11 were copied). Each view says how much was read (`cwd_read_pct`) |
| bytes | **measured, per process** | rchar/wchar (syscall, pipes and terminals included) and read_bytes/write_bytes (disk) of the processes that ran there -- credited to where they ran, not to where the bytes went |
| named | **inferred** | distinct paths a command line names, classified by the command: read (`cat`, `grep`, `ls`, a script run, `<`), write (`>`/`>>`, a `cp`/`mv`/`rsync` destination, `tee`, `touch`, `mkdir`, `sed -i`, `tar -x`, `git clone`, `wget -O`, `curl -o`), delete (`rm`, `rmdir`, `git rm`, `find -delete`), and anything else path-like as named. Naming a path is not proof of touching it |

Exact file-level reads and writes would need a collector probe (openat / unlink / rename
tracepoints) that is not built, and Claude Code's Read, Edit and Write tools and Codex's
apply_patch run inside the agent's own process and leave no command line at all.

Directories inside the home are shown relative (`./src`), outside it with `~` for the user's
home and `~bob` for another's; deeper than `collapse_depth` (4) levels below the home or a
top-level root (`/tmp`, `/scratch`, `~/.cache`, `/n/<fs>`, another user's home) they fold into
that ancestor, as do a directory's quieter subdirectories past six, and every fold is counted.
A **write or delete named outside the home is flagged** and labelled by where it landed
(another user's home and system paths first, the agent's own `/tmp` scratch last). The
toggle **this agent only / with its subagents** adds every agent beneath it, judged against
the outer agent's home. Claude Code's tool calls are read through their `eval '<cmd>'`
(`proctree.ran`, the inspector's own reader), so the snapshot each call sources and the cwd
file it writes are never counted; a fork that inherited its parent's argv is not read twice. A
relative path is placed against the process's cwd, never against `/` (an agent's process is at
`/` when it inherited an IDE's or a daemon's directory, and `ls src` from there is not `/src`),
and never by climbing out of a copied cwd with `..`: `./bin/cachesim ../data/x` with a cwd
copied from an agent in `/home/marthen` would otherwise read as `/home/data`, another user's
home.

Cost and bounds: each distinct argv is parsed once (a regex tokenizer, never `shlex`) and the
per-record path is a few dict lookups. On rtx6000a's 2026-10-10 (1.89 M records, a ~90 s 24 h
build) `feed` took 2.4 s, ~1.4 µs a record, and the saved window grew by 2.4 MB. State is one
entry per collector session key, capped at `reducers.workspace.max_keys` (150 000) keys,
`max_dirs` (300) directories per key, `max_dir_entries` (400 000) overall, 64 named paths per
directory and 32 per command line; past any cap the excess is counted, not kept. Served paths
are masked (`redact`) and no raw argv is served.

The **drift** proxies (`cwd_outside_pct`, `mix_jsd`) are the trajectory rows' reading and stay
on the Trajectories tab; the Agents page no longer carries them.

## Slurm: how sessions use the scheduler

"An agent submitted job N" says nothing about agent behaviour; how agents drive the
scheduler does. On boslogin06's 2026-10-09 agents made 16.6 scheduler queries per job
against 0.2 for people, 41 agent sessions polled the queue for 10 minutes or more (94.9 h
of it; one Claude session every 4.9 s for 4.9 h), one session submitted 112 jobs in a
minute, and 79% of agent submissions used `--parsable` (people: 0.2%) -- a program reading
the job id. The **Slurm** tab (`?tab=slurm`, hidden by `slurm.enabled: false`) is built on
that, from the window's `slurm` reducer (`reducers/slurm.py`):

- **per session** -- an agent's collector keys folded into the agent, as Trajectories fold
  them; people and IDEs one row per user: jobs, refusals, the busiest minute, distinct
  command lines (numbers masked: one script resubmitted is one), the flags it submits with,
  queries, its longest **polling loop** (queries within 2 s are one poll, polls within 3 min
  one loop, 10 min or more counts as polling), queries per job, cancels, GPU-hours asked;
- **per class**, the same as rates and shares side by side;
- **what came back** from each submission: a job id, a dry run (`--test-only` makes no job),
  a step into an allocation already held (`srun --jobid`/`--overlap`), or a **refusal** --
  no id, a non-zero exit and the scheduler's own error line, which is the reason shown
  (`QOSMaxSubmitJobPerUserLimit`, a partition rule). An `srun` whose program ran and failed
  is not a refusal;
- queries and jobs per time bin, by class. A bin with no Slurm record of any class is a
  gap, not 0.

Requests are read off argv: a `#SBATCH` line in the script is not seen, so GPU-hours count
only jobs with GPUs and a time limit on the command line, and say how many those were.

## Known collector limitations the page surfaces

- **`peak_rss_mb` is always 0** on kernel 6.8: the kernel runs `exit_mm()` (clearing
  `task->mm`) before the `sched_process_exit` tracepoint fires, so the `if (mm)` guard at
  `ebpf_trace.py:834` never assigns. Reading `task->signal->maxrss` instead survives
  `exit_mm()` (verified). Until then the metric renders **not measured**.
- **The collector's session key splits one session per tool call.** It keys a process on
  its nearest classifier-tagged ancestor, and the classifier tags anything whose argv
  mentions `~/.claude/`, `~/.codex/` or `~/.vscode-server/` -- including the shell every
  Claude Code Bash tool call runs in. The dashboard decides what each tagged process
  really is (`roles.py`) and folds each key into the agent above it (`procindex.py`);
  trajectory rows say how many keys they fold and how the least certain one was
  attributed, and the Sandbox tab attributes tool calls to sessions through the same fold. On madsys-gpu1 a
  24 h window went from 3 281 keys to 636 sessions, one VS Code server alone from 688.
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
| `rc_dashboard/tail.py` | `IngestSource`; the file tail, and the newest-first read-back that refills retention on every start (a push source drops in here) |
| `rc_dashboard/gz.py` | a `.jsonl.gz` day-file read in place, seekable by uncompressed offset through a per-process inflater index |
| `rc_dashboard/health.py` | each collector's verdict, from its own meta / heartbeat / stop records |
| `rc_dashboard/clock.py` | the service's present; `clock.as_of` pins (and freezes) it for a captured feed |
| `rc_dashboard/normalize.py` | schema contract, `actor3`, sandbox/approval, session keys, null gating |
| `rc_dashboard/roles.py` | what a classifier-tagged process is: agent, tool-shell, probe, sandbox, infra, ... |
| `rc_dashboard/procindex.py` | per-window index of tagged processes; folds session keys into their agent |
| `rc_dashboard/buckets.py` | rolling per-minute aggregates; exact sub-window slicing |
| `rc_dashboard/reducers/` | tools, io, resources, sandbox, trajectories, cascades (agent trees), workspace (where each agent worked), slurm (sessions at the scheduler), live, node |
| `rc_dashboard/windows.py` | window clamp rule, per-window reducer sets, the LRU + build worker |
| `rc_dashboard/snapshots.py` | saved window builds: fingerprint, capture/restore, the 0600 store |
| `rc_dashboard/sacct.py` | `collect/slurm_query.bash` exports → per-job rows, generation-merged |
| `rc_dashboard/export.py` | shared filter grammar, streaming export, the windowed re-scan (`seek_to_epoch`) |
| `rc_dashboard/app.py` | FastAPI: REST, WebSocket, static mount |
| `web/` | React + Vite + TS; `dist/` is what the service serves |

## Ingestion, precisely

**What the page holds is a function of the collector's files and the clock — never of when this
service was running.** Nothing in the live tier is restored from a previous run (the historical
windows resume from saved builds, but only from ones that are provably a prefix of the same files —
see *The time window*). On every start the service hands
each day-file to the tail at the end of its last complete line, then reads the history behind
that point back into the live tier: `min(live.backfill_hours, live.retention_min)` of it, located
by the same timestamp binary search the window rebuild uses. Offsets used to be persisted while
the bins they fed were not, so a restart resumed at EOF with empty bins and the page showed only
what the collector wrote after the service came back — and `run.sh` restarts the service on
every save.

The read-back runs **newest first**, k-way merged across hosts, while the tail is already
polling; a poll that lands before the hand-off reads nothing, so no record is counted twice. The
page is live within a second and watches the chart fill from now backwards. The filled span is
always contiguous, so the picker's `filled 5h of 7d` stays true beside `reading history · N%`.
The read takes the aggregation lock one small batch at a time, so neither the page nor the tail
queues behind it. A week of one login node's feed (4 GB, 2.4 M records) takes 35–45 s here. Bins
are sums and take records in any order; the "latest" state — each host's plate, the event and
submission tails — only takes an older record where nothing newer has claimed the slot.

`DailyWriter.write()` (`collector/ebpf_trace.py`) buffers and flushes once per poll round, which
is not atomic against a concurrent reader. So the tailer reads to EOF, finds the last newline,
yields only complete lines, and **advances its stored offset only that far** — trailing bytes are
re-read next cycle. Idempotent, never a truncated JSON line, never a lost record. The startup
hand-off obeys the same rule, so a line still being written belongs to the tail, never to the
read-back.

Offsets are keyed on `(st_dev, st_ino)`, not path, so a recreated file is detected as a new inode
rather than silently seeked past its end. A file first seen after startup — a new date
directory, a new host — is read from byte 0, at most 64 MB per poll.
