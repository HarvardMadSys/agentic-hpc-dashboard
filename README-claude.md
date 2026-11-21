# agentic-hpc-dashboard

Measuring what coding agents actually do on HPC login nodes, and showing it without
inventing anything.

Two halves, in this order:

| | what it is | privilege | output |
|---|---|---|---|
| [`collector/`](collector/) | `ebpfm` — a standalone two-tier login-node collector (eBPF + `/proc` node snapshot) | root for the eBPF tier, unprivileged for the node tier | JSONL, two streams per host per day (`snapshot` + `exits`), each day gzipped once it is over |
| [`dashboard/`](dashboard/) | `rc_dashboard` — FastAPI service that tails those day-files, keeps rolling 24 h aggregates, and pushes them to a React page over a WebSocket | unprivileged | HTTP + WebSocket on `:8080` |

The collector runs on each login node; the dashboard reads the directory the collector
writes. They are coupled only by the on-disk records (`SCHEMA_VERSION = 8`), written as
JSONL, so the dashboard can read a feed captured months ago, or a fleet's worth of hosts
at once, and nothing needs to be running for it to start.

The eBPF tier splits each day in two: `<host>.exits.jsonl` for the per-process terminal
records, whose rate is the node's fork rate and which dominate the volume, and
`<host>.snapshot.jsonl` for the residency census, connections, per-tick network bytes by
endpoint, submits and the run's own meta/stop bookends. Each record carries its own `host`,
and the dashboard lists the day directory, so the split is a filing decision, not a schema
one. Once a day is over the collector gzips its files (`.jsonl.gz`), and they never change
again; the dashboard keeps each such sealed file as Parquet too, in the background
(`dashboard/rc_dashboard/history.py`), and answers a selective read of a sealed day -- one
user's events, one session's process tree -- or a search of it by time from that copy,
which reads exactly as the file does, offsets and all.

```
login node                                    anywhere with read access
┌──────────────────────────────┐              ┌────────────────────────────────┐
│ ebpfm.sh                     │   snapshot   │ rc_dashboard                   │
│  ├ ebpf tier   (root)  ──────┼─ + exits ────┼─▶ tail → normalize → buckets   │
│  └ node tier   (user)  ──────┼─ snapshot ───┼─▶ reducers → /api + WS         │
└──────────────────────────────┘              │                  └─▶ web/ SPA  │
                                              └────────────────────────────────┘
```

## Quick start

**Collect** (on the login node — the folder is self-contained, `scp` it or hand over one
file from `./ebpfm.sh bundle`):

```bash
sudo ./collector/ebpfm.sh bootstrap    # dependencies + the two sysctls, once per node
sudo ./collector/ebpfm.sh check        # expect 0 FAIL
sudo ./collector/ebpfm.sh start        # both tiers
```

Without root you still get the node tier: `./collector/ebpfm.sh start`. That is a real
half of the data, not a degraded mode — eBPF cannot produce load average, memory totals,
filesystem space or interface counters at all.

**Serve** (anywhere that can read the collector's output directory):

```bash
cd dashboard && uv sync
uv run python -m rc_dashboard --check-feeds       # what resolved, what didn't, and why
cd web && npm ci && npm run build && cd ..        # builds web/dist/, which the service serves
uv run python -m rc_dashboard new-password        # once: put the hash it prints in dashboard.config.json
uv run python -m rc_dashboard serve --port 8080
```

`serve` starts only behind a password, or with `--open` (`RC_DASH_OPEN=1`) on a machine only
you can reach: on a login node every user can reach 127.0.0.1.

Off-cluster, `--check-feeds` exits 1 — the eBPF feed is `required` and absent. Point it at
a collector root first:

```bash
RC_DASH_EBPF_ROOTS=/var/log/ebpfm \
RC_DASH_NODE_ROOTS=/var/log/ebpfm-login \
uv run python -m rc_dashboard serve
```

For a remote instance: `ssh -L 8080:127.0.0.1:8080 <host>`.

**Frontend only**, with no backend and no cluster — a Vite middleware answers every
`/api/*` route and the `/ws` handshake from a static fixture:

```bash
cd dashboard/web && npm install && npm run dev     # http://localhost:5173
```

**Demo**, the whole stack with no cluster — the service runs unchanged over a synthetic
collector feed it writes and keeps appending to, for fictional hosts and users; every
record and every page says *fake data* ([`dashboard/README.md`](dashboard/README.md#demo-mode)):

```bash
cd dashboard && uv sync && (cd web && npm ci && npm run build)
uv run python -m rc_dashboard demo                 # http://127.0.0.1:8080
```

## Where to read further

| document | covers |
|---|---|
| [`collector/README.md`](collector/README.md) | the two tiers, why eBPF (and why `sudo` does not escape the cgroup cap — `systemd-run` does), the concurrency model, **measured overhead**, the full event/field schema, `ebpfm.sh` verbs, kernel support |
| [`collector/COVERAGE.md`](collector/COVERAGE.md) | the field-by-field crosswalk: for each of the 40 observable login-node signals, which tier reads it after the cutover |
| [`collector/VENDORED.md`](collector/VENDORED.md) | the five copied modules, why copies rather than symlinks, and the sync rule `ebpfm.sh check` enforces |
| [`dashboard/README.md`](dashboard/README.md) | feed resolution and config precedence, the history store (sealed days as Parquet), the 24 h window, the REST/WS API, the panel conventions, known collector limitations the page surfaces |
| [`dashboard/web/README.md`](dashboard/web/README.md) | the SPA: tabs, charts, filter/URL state, the dev fixture, and the integrity rules the code enforces |
| [`deploy/README.md`](deploy/README.md) | the live service on servers where you have root, two ways: one server that runs the collector and the dashboard ([`single`](deploy/single/README.md)), or a fleet whose files one host copies with rsync ([`fleet`](deploy/fleet/README.md), and why the copy appends in place) |
| [`hpc/README.md`](hpc/README.md) | what is HPC-specific and lives apart: the fleet stream over ssh, and [`hpc/deploy/README.md`](hpc/deploy/README.md), the no-root kit for a cluster's login nodes (the settings file, the first deploy, memory and disk, the hourly check) |
| [`deploy/cloudflare/README.md`](deploy/cloudflare/README.md) | the demo, over a synthetic feed, on a public URL |

## The rules both halves obey

These are research-integrity constraints, not preferences; each one is a mistake this
project made once. They are stated per-component in the READMEs above, but they are the
same rules:

- **Null is not zero, and zero is not missing.** A dropped BPF block emits `null`, never
  `0` — an absent measurement must not read as a zero measurement. Downstream, a nullable
  field carries `coverage_pct` and is never imputed; a present feed with no matching rows
  says `none`, while an absent feed names the path it expected.
- **Three actor classes, never two** — `agent` / `human` / `ide`. `ide` is any editor a person
  drives (VS Code, Cursor, Windsurf, other remote-IDE servers); the collector's `human-vscode`
  and its Cursor/Windsurf "agents" are mapped into it at ingest. Folding the IDE class into
  "agent" is what produced a retracted GPU near-parity claim.
- **Nothing is fabricated.** The collector emits only what it measured; the page contains
  no `Math.random()` (`npm run check:no-random` asserts it) and never interpolates between
  frames. A minute with no record is a hole in the line.
- **`sandbox` and `approval` are independent axes**, and `denied` — the collector being
  unable to read the field — is its own state, never folded into `unsandboxed`.
- **Scope is stated.** The eBPF tier runs on login nodes, so its resource figures are
  login-node use, not cluster use.

## Tests

```bash
cd collector && python3 -m unittest discover -s tests -q    # no root, no BPF needed
cd dashboard && uv run python -m unittest discover -s tests -q   # ingest: restarts, the hand-off, backfill order
cd dashboard/web && npm run typecheck && npm run check:no-random
python3 -m unittest discover -s deploy/fleet/tests -q   # pull.py, with rsync for real and stand-in nodes
python3 -m unittest discover -s deploy/common/tests -q  # the single and fleet installers, with stand-ins for systemd
python3 -m unittest discover -s hpc/fleet/tests -q      # the HPC fleet stream, over a fake collector
python3 -m unittest discover -s hpc/deploy/tests -q     # the HPC deploy kit, with stand-ins for systemd, tmux, and ssh
```

## A note on paths

The collector and dashboard READMEs were written inside a larger measurement repo and
still refer to sibling trees that are not vendored here (`collect/`, `analyze/`,
`findings/`). Those references are provenance, and the code does not depend on them — the
collector has no `../` imports, and the dashboard resolves every feed path from config,
env, or a probed candidate list.

One legacy name survives only in old data. The collector folder was once
`eBPF_marthen_new/` and is [`collector/`](collector/) here, and through schema 6 every
record's envelope stamped `collector = "ebpf_marthen_new"` and `source = "ebpf"`. Schema 7
drops both: they were the same on every line, nothing read them, and the feed's own
location already says where a record came from. Captures from before the change still
carry them.

## Requirements

- **Collector**: Linux with BCC (`python3-bpfcc`) and kernel headers for the eBPF tier;
  developed against 6.8 and 6.17, with a 4.18 path for D-state dwell. The node tier is
  pure `/proc` and needs neither. `bootstrap` installs the rest.
- **Dashboard**: Python ≥ 3.11 with what `dashboard/pyproject.toml` lists (`fastapi`,
  `uvicorn`, `numpy`, `orjson`, `pyarrow`; `uv sync` installs them), Node for the
  frontend build (`react`, `react-dom` at runtime; no charting library, every chart is
  hand-rolled SVG).
