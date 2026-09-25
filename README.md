# agentic-hpc-dashboard

Measuring what coding agents actually do on HPC login nodes, and showing it without
inventing anything.

Two halves, in this order:

| | what it is | privilege | output |
|---|---|---|---|
| [`collector/`](collector/) | `ebpfm` — a standalone two-tier login-node collector (eBPF + `/proc` node snapshot) | root for the eBPF tier, unprivileged for the node tier | JSONL, two files per host per day (`snapshot` + `exits`) |
| [`dashboard/`](dashboard/) | `rc_dashboard` — FastAPI service that tails that JSONL, keeps rolling 24 h aggregates, and pushes them to a React page over a WebSocket | unprivileged | HTTP + WebSocket on `:8080` |

The collector runs on each login node; the dashboard reads the directory the collector
writes. They are coupled only by the on-disk JSONL schema (`SCHEMA_VERSION = 6`), so the
dashboard can read a feed captured months ago, or a fleet's worth of hosts at once, and
nothing needs to be running for it to start.

The eBPF tier splits each day in two: `<host>.exits.jsonl` for the per-process terminal
records, whose rate is the node's fork rate and which dominate the volume, and
`<host>.snapshot.jsonl` for the residency census, connections, submits and the run's own
meta/stop bookends. Records are unchanged either way — each carries its own `host`, and
the dashboard globs the day directory — so this is a filing decision, not a schema one.

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
uv run python -m rc_dashboard serve --port 8080
```

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

## Where to read further

| document | covers |
|---|---|
| [`collector/README.md`](collector/README.md) | the two tiers, why eBPF (and why `sudo` does not escape the cgroup cap — `systemd-run` does), the concurrency model, **measured overhead**, the full event/field schema, `ebpfm.sh` verbs, kernel support |
| [`collector/COVERAGE.md`](collector/COVERAGE.md) | the field-by-field crosswalk: for each of the 40 observable login-node signals, which tier reads it after the cutover |
| [`collector/VENDORED.md`](collector/VENDORED.md) | the five copied modules, why copies rather than symlinks, and the sync rule `ebpfm.sh check` enforces |
| [`dashboard/README.md`](dashboard/README.md) | feed resolution and config precedence, the 24 h window, the REST/WS API, the panel conventions, known collector limitations the page surfaces |
| [`dashboard/web/README.md`](dashboard/web/README.md) | the SPA: tabs, charts, filter/URL state, the dev fixture, and the integrity rules the code enforces |

## The rules both halves obey

These are research-integrity constraints, not preferences; each one is a mistake this
project made once. They are stated per-component in the READMEs above, but they are the
same rules:

- **Null is not zero, and zero is not missing.** A dropped BPF block emits `null`, never
  `0` — an absent measurement must not read as a zero measurement. Downstream, a nullable
  field carries `coverage_pct` and is never imputed; a present feed with no matching rows
  says `none`, while an absent feed names the path it expected.
- **Three actor classes, never two** — `agent` / `human-vscode` / `human`. Folding VS Code
  into "agent" is what produced a retracted GPU near-parity claim.
- **Nothing is fabricated.** The collector emits only what it measured; the page contains
  no `Math.random()` (`npm run check:no-random` asserts it) and never interpolates between
  frames. A minute with no record is a hole in the line.
- **`sandbox` and `approval` are independent axes**, and `denied` — the collector being
  unable to read the field — is its own state, never folded into `unsandboxed`.
- **Scope is stated.** The eBPF tier runs on login nodes, so its resource figures are
  login-node use, not cluster use.

## Tests

```bash
cd collector && python3 -m unittest discover -s tests -q    # 72 tests, no root, no BPF needed
cd dashboard && uv run python -m unittest discover -s tests -q   # ingest: restarts, the hand-off, backfill order
cd dashboard/web && npm run typecheck && npm run check:no-random
```

## A note on paths

The collector and dashboard READMEs were written inside a larger measurement repo and
still refer to sibling trees that are not vendored here (`collect/`, `analyze/`,
`findings/`). Those references are provenance, and the code does not depend on them — the
collector has no `../` imports, and the dashboard resolves every feed path from config,
env, or a probed candidate list.

One legacy name survives deliberately. The collector folder was once `eBPF_marthen_new/`
and is [`collector/`](collector/) here, but the JSONL envelope still stamps
`collector = "ebpf_marthen_new"` — a wire value, so renaming it would split feeds captured
before the change from those captured after. Nothing reads the field; it is provenance for
whoever opens the data later.

## Requirements

- **Collector**: Linux with BCC (`python3-bpfcc`) and kernel headers for the eBPF tier;
  developed against 6.8 and 6.17, with a 4.18 path for D-state dwell. The node tier is
  pure `/proc` and needs neither. `bootstrap` installs the rest.
- **Dashboard**: Python ≥ 3.11 (`fastapi`, `uvicorn` — that is all), Node for the
  frontend build (`react`, `react-dom` at runtime; no charting library, every chart is
  hand-rolled SVG).
