# Agent-behaviour dashboard

One self-contained HTML page that answers *what are the coding agents on this cluster
actually doing* from the consolidated collector set — the three instrument tiers of
[`collect/CONSOLIDATION.md`](../collect/CONSOLIDATION.md):

| tier | collector | what the dashboard reads it for |
|---|---|---|
| **process (root, every node)** | `eBPF_marthen` `exit` / `residency` / `tcp` / `submit` | purpose mix, session size & depth, lifetimes, residency accumulation, exit status, egress, autonomy flags |
| **scheduler (unprivileged poller)** | `slurm_jobs`, `slurm_nodes`, `gpu_binding`, `sdiag`, `policy` | live census, GPU bindings, slurmctld control loop |
| **accounting (operator)** | `sacct` export, `sacctmgr` | settled per-job record, identity layer |

## Build it

```bash
python3 dashboard/build_dashboard_data.py
```

No dependencies beyond the standard library. It writes:

- `dashboard/data.json` — the panel bundle, one entry per panel with its provenance
- `dashboard/agent_dashboard.html` — **the page to open or deploy.** A complete document:
  doctype, `<meta charset="utf-8">`, viewport, the viewer, and the bundle inlined
- `dashboard/agent_dashboard.embed.html` — the same page without a document skeleton,
  for hosts that supply their own (an iframe, a CMS block, the Artifact publisher)

Useful switches:

```bash
python3 dashboard/build_dashboard_data.py --sacct /path/to/SlurmData_2026-06-01.csv
python3 dashboard/build_dashboard_data.py --ebpf-days 5 --max-ebpf-records 8000000
python3 dashboard/build_dashboard_data.py --no-synth      # measured panels only
python3 dashboard/build_dashboard_data.py --json-only     # skip the HTML render
```

A raw `____`-delimited sacct export is folded to one row per job automatically
(`reduce_sacct.py`, cached under `dashboard/.cache/`), because the request lives on the
job row while `MaxRSS` / `TotalCPU` / `gres/gpuutil` live on the step rows.

## Real-time behaviour

The page opens on **Live**: exit rate per minute for the last 90 minutes, resident agent
trees per login node, jobs in flight, and the raw process-exit stream. It re-reads
`data.json` from its own directory every 60 s (`refresh_s` in the bundle) and swaps in
the new build when `built_at` changes, so pointing a cron or a systemd timer at

```bash
*/1 * * * * cd /path/to/repo && python3 dashboard/build_dashboard_data.py --json-only
```

is all a live deployment needs. Between reads the live tier advances locally so the
stream and the rate chart keep moving; the header shows how stale the current read is
and turns amber past three intervals. A standalone copy of `agent_dashboard.html` (no
`data.json` beside it) keeps working off its inlined bundle.

## Panel provenance

The UI presents the dashboard as deployed — no per-panel source badges. Provenance is
still tracked in the data, not dropped: every panel in `data.json` carries `_prov`
(`real` / `synthetic`) alongside `_source`, and the builder prints the split on every
run:

- **real here** — `jobs` (434,553 job rows from the local sacct export), `identity`
  (2,782 users / 578 accounts / 38 QOS from the `sacctmgr` dump)
- **generated here** — the process tier and the `dcgm`/`sdiag`-derived panels, from
  [`rc_synth.py`](rc_synth.py): schema-faithful to the real records, calibrated to the
  magnitudes published in [`findings/`](../findings), deterministic under one seed

Run the same command on a login node, where `log/<component>` resolves through `data/`
to live telemetry, and those panels come from the collectors instead, with **no
dashboard change**: [`rc_real.rollup_ebpf()`](rc_real.py) reduces raw
`exit`/`residency`/`tcp` records into the identical panel shapes `rc_synth` produces.

## Agent attribution, and its limit

The accounting tier has no actor field. The dashboard labels a job `agent-attributed`
only when the accounting row itself carries an agent path marker — `.claude/`,
`claude-tmp`, `claude-code`, `.codex/`, `.cursor`, `.aider` in `WorkDir` or `SubmitLine`.
That is a **high-precision, low-recall** signal: in the local export it resolves 1,166
jobs across 3 users, so the Jobs tab carries a small-n banner and should be read as a
case study, not a rate.

The authoritative actor comes from the process tier — eBPF `submit` records linking an
`sbatch` to its job id (exact), and the user-level `user_actor` column from
`dataclean/user.csv` (dense). When either is present, wire it in ahead of the marker
heuristic.

## Conventions the panels obey

- **Four grains, always** — agent-vs-other is reported per job (array-expanded), per
  submission (array-collapsed), per user, and per `work_dir`, because they disagree by
  orders of magnitude and the per-capita views routinely erase the per-job gap.
- **Count and cost, side by side** — polling is ~30% of an agent's process exits and ~2%
  of its CPU. Any panel showing one weighting alone is the error the research most wants
  to prevent, so purpose mix ships as two charts on one scale each (never a dual axis).
- **Three classes, not two** — `agent` / `human-vscode` / `human` (`agent_lib.actor3`).
  Folding VS Code into "agent" is what produced the retracted GPU near-parity claim.
- **State the grain for lifetimes** — agent *roots* outlive human logins; agent
  *processes* are shorter-lived than human ones. Same telemetry, opposite sentence.
- **Colour** — the Harvard identity palette, **light only** (no dark theme; every colour
  is painted explicitly so the page holds on any host ground). Crimson (PMS 1807C
  `#A51C30`) is the accent and the `agent` series colour, on a crimson-biased neutral
  field from the core palette. Chart series are brand hues re-stepped for the chart
  surface so the eight-slot set clears the lightness-band, chroma-floor and
  colour-vision-deficiency gates (worst adjacent-pair CVD ΔE 9.6); the tail folds into
  one grey `other` rather than a generated ninth hue. Crimson is reserved for agent
  identity, so the process-exit stream tags class as text and lets purpose own the
  colour. Status hues (green / warm yellow / salmon / red) always ship with a label.
- **Data only** — panels carry a title, the feed that fills them, and the chart or table.
  No interpretive notes, no subtitles, no per-panel commentary; the reporting rules above
  are enforced in what each panel *plots* (both grains, both weightings, three classes),
  not in prose beside it.

## Files

| file | role |
|---|---|
| `build_dashboard_data.py` | merges the tiers, stamps provenance, renders the HTML |
| `rc_real.py` | readers/reducers for sacct, `sacctmgr`, and raw eBPF / proc-trace JSONL |
| `rc_synth.py` | calibrated synthetic panels; all assumptions in one `CAL` table |
| `reduce_sacct.py` | raw export → one row per job |
| `agent_dashboard.template.html` | the viewer (`/*__RC_DATA__*/` is the data marker) |
| `agent_dashboard.html` | generated standalone document; this is the thing you open |
| `agent_dashboard.embed.html` | generated fragment, for an embedding host |

## Browser notes

The page is plain HTML, SVG and vanilla JS — no framework, no chart library, nothing to
install. Three things are handled explicitly because they are where engines disagree:

- **Encoding.** The template source is pure ASCII: every typographic glyph is a
  `\uXXXX` escape built at runtime (or an HTML entity in the static markup), and the
  standalone file declares `charset=utf-8`. A dash or middot cannot turn into mojibake
  even if a server serves the file with no charset, or with the wrong one.
- **SVG sizing.** Charts carry natural `width`/`height` attributes plus a `viewBox`, and
  CSS scales them with `max-width:100%; height:auto` — the one sizing idiom every engine
  agrees on. `width="100%"` against a fixed `height` attribute is where Safari and
  Chrome diverge, and `overflow:visible` let that divergence paint over the next panel.
- **No `color-mix()`.** Every colour is a literal or a custom property, so nothing
  depends on a recent colour-function implementation.
