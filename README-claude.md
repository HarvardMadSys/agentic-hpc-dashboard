# agentic-hpc-dashboard

This project measures what coding agents actually do on HPC login nodes, and shows it
without inventing anything.

It has two halves, in this order:

| | what it is | privilege | output |
|---|---|---|---|
| [`collector/`](collector/) | `ebpfm`, a standalone two-tier login-node collector (eBPF + `/proc` node snapshot) | root for the eBPF tier, unprivileged for the node tier | JSONL, two streams per host per day (`snapshot` + `exits`), each day gzipped once it is over |
| [`dashboard/`](dashboard/) | `rc_dashboard`, a FastAPI service that tails those day-files, keeps rolling 24 h aggregates, and pushes them to a React page over a WebSocket | unprivileged | HTTP + WebSocket on `:8080` |

The collector runs on each login node, and the dashboard reads the directory the collector
writes to. The only thing coupling them is the on-disk records (`SCHEMA_VERSION = 8`),
written as JSONL. So the dashboard can read a feed captured months ago, or a whole fleet's
hosts at once, and nothing else needs to be running for the dashboard to start.

The eBPF tier writes each day to two files. `<host>.exits.jsonl` holds the per-process
terminal records. These are written at the node's fork rate and make up most of the volume.
`<host>.snapshot.jsonl` holds the residency census, connections, per-tick network bytes by
endpoint, submits, and the run's own meta/stop bookend records. Every record carries its
own `host`, and the dashboard lists the whole day directory, so the split only affects how
records are filed, not the schema. Once a day is over, the collector gzips its files
(`.jsonl.gz`) and they never change again. The dashboard also keeps a Parquet copy of each
sealed file, built in the background (`dashboard/rc_dashboard/history.py`). Selective reads
of a sealed day (one user's events, or one session's process tree) and searches of it by
time are answered from that copy, which reads exactly like the original file, offsets
included.

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

**Collect** (on the login node). The folder is self-contained, so you can `scp` it over,
or hand over the single file that `./ebpfm.sh bundle` makes:

```bash
sudo ./collector/ebpfm.sh bootstrap    # dependencies + the two sysctls, once per node
sudo ./collector/ebpfm.sh check        # expect 0 FAIL
sudo ./collector/ebpfm.sh start        # both tiers
```

Without root you still get the node tier, with `./collector/ebpfm.sh start`. This is not
a degraded mode. The node tier is a real half of the data, because eBPF cannot produce load
average, memory totals, filesystem space or interface counters at all.

**Serve** (anywhere that can read the collector's output directory):

```bash
cd dashboard && uv sync
uv run python -m rc_dashboard --check-feeds       # what resolved, what didn't, and why
cd web && npm ci && npm run build && cd ..        # builds web/dist/, which the service serves
uv run python -m rc_dashboard new-password        # once: put the hash it prints in dashboard.config.json
uv run python -m rc_dashboard serve --port 8080
```

`serve` only starts with a password set, or with `--open` (`RC_DASH_OPEN=1`) on a machine
that only you can reach. Note that on a login node, every user can reach 127.0.0.1.

Off-cluster, `--check-feeds` exits 1 because the eBPF feed is `required` and absent. Point
the dashboard at a collector root first:

```bash
RC_DASH_EBPF_ROOTS=/var/log/ebpfm \
RC_DASH_NODE_ROOTS=/var/log/ebpfm-login \
uv run python -m rc_dashboard serve
```

For a remote instance, forward the port: `ssh -L 8080:127.0.0.1:8080 <host>`.

**Frontend only**, with no backend and no cluster. A Vite middleware answers every
`/api/*` route and the `/ws` handshake from a static fixture:

```bash
cd dashboard/web && npm install && npm run dev     # http://localhost:5173
```

**Demo**: the whole stack with no cluster. The service runs unchanged over a synthetic
collector feed that it writes and keeps appending to, for fictional hosts and users. Every
record and every page says *fake data* ([`dashboard/README.md`](dashboard/README.md#demo-mode)):

```bash
cd dashboard && uv sync && (cd web && npm ci && npm run build)
uv run python -m rc_dashboard demo                 # http://127.0.0.1:8080
```

## Where to read further

| document | covers |
|---|---|
| [`collector/README.md`](collector/README.md) | the two tiers, why eBPF (and why `sudo` does not escape the cgroup cap but `systemd-run` does), the concurrency model, **measured overhead**, the full event/field schema, `ebpfm.sh` verbs, kernel support |
| [`collector/COVERAGE.md`](collector/COVERAGE.md) | the field-by-field crosswalk: for each of the 40 observable login-node signals, which tier reads it after the cutover |
| [`collector/VENDORED.md`](collector/VENDORED.md) | the five copied modules, why copies rather than symlinks, and the sync rule `ebpfm.sh check` enforces |
| [`dashboard/README.md`](dashboard/README.md) | feed resolution and config precedence, the history store (sealed days as Parquet), the 24 h window, the REST/WS API, the panel conventions, known collector limitations the page surfaces |
| [`dashboard/web/README.md`](dashboard/web/README.md) | the SPA: tabs, charts, filter/URL state, the dev fixture, and the integrity rules the code enforces |
| [`deploy/fleet/README.md`](deploy/fleet/README.md) | the live service on a host of your own: the fleet stream from each node, the settings file, the first deploy, memory and disk, and the hourly check |
| [`deploy/cloudflare/README.md`](deploy/cloudflare/README.md) | the demo, over a synthetic feed, on a public URL |

## The rules both halves obey

These are research-integrity constraints, not preferences. Each one comes from a mistake
this project made once. The component READMEs above state them separately, but they are
the same rules:

- **Null is not zero, and zero is not missing.** A dropped BPF block emits `null`, never
  `0`, because a missing measurement must not look like a zero measurement. Downstream, a
  nullable field carries `coverage_pct` and is never imputed. A feed that is present but has
  no matching rows says `none`, while an absent feed names the path it expected.
- **Three actor classes, never two:** `agent` / `human` / `ide`. `ide` is any editor a person
  drives (VS Code, Cursor, Windsurf, other remote-IDE servers). At ingest, the collector's
  `human-vscode` and its Cursor/Windsurf "agents" are mapped to `ide`. Folding the IDE class
  into "agent" is what produced a GPU near-parity claim that was later retracted.
- **Nothing is fabricated.** The collector emits only what it measured. The page contains
  no `Math.random()` (`npm run check:no-random` checks this) and never interpolates between
  frames. A minute with no record shows up as a hole in the line.
- **`sandbox` and `approval` are independent axes.** `denied`, meaning the collector could
  not read the field, is its own state and is never folded into `unsandboxed`.
- **Scope is stated.** The eBPF tier runs on login nodes, so its resource figures describe
  login-node use, not cluster use.

## Tests

```bash
cd collector && python3 -m unittest discover -s tests -q    # no root, no BPF needed
cd dashboard && uv run python -m unittest discover -s tests -q   # ingest: restarts, the hand-off, backfill order
cd dashboard/web && npm run typecheck && npm run check:no-random
python3 -m unittest discover -s fleet/tests -q          # the fleet stream, over a fake collector
python3 -m unittest discover -s deploy/fleet/tests -q   # the deploy kit, with stand-ins for systemd, tmux, and ssh
```

## A note on paths

The collector and dashboard READMEs were written inside a larger measurement repo and
still refer to sibling trees that are not vendored here (`collect/`, `analyze/`,
`findings/`). Those references only record where the code came from. The code does not
depend on them: the collector has no `../` imports, and the dashboard resolves every feed
path from config, env, or a probed list of candidates.

One legacy name survives, but only in old data. The collector folder used to be
`eBPF_marthen_new/` (it is [`collector/`](collector/) here), and up to and including
schema 6, every record's envelope was stamped with `collector = "ebpf_marthen_new"` and
`source = "ebpf"`. Schema 7 drops both fields, because they were identical on every line,
nothing read them, and the feed's location already says where a record came from. Captures
from before the change still carry them.

## Requirements

- **Collector**: Linux with BCC (`python3-bpfcc`) and kernel headers for the eBPF tier.
  It was developed against kernels 6.8 and 6.17, with a separate 4.18 code path for
  D-state dwell. The node tier only reads `/proc` and needs neither. `bootstrap` installs
  the rest.
- **Dashboard**: Python ≥ 3.11 with the packages `dashboard/pyproject.toml` lists (`fastapi`,
  `uvicorn`, `numpy`, `orjson`, `pyarrow`; `uv sync` installs them), and Node for the
  frontend build (`react`, `react-dom` at runtime). There is no charting library; every
  chart is hand-rolled SVG.
