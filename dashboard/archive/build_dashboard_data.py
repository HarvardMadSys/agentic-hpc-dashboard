#!/usr/bin/env python3
"""Build the agent-behaviour dashboard: ``dashboard/data.json`` + a self-contained
``dashboard/agent_dashboard.html``.

    python3 dashboard/build_dashboard_data.py                 # real where present, synthetic elsewhere
    python3 dashboard/build_dashboard_data.py --no-synth      # real panels only (gaps stay empty)
    python3 dashboard/build_dashboard_data.py --sacct PATH    # point at a different sacct export
    python3 dashboard/build_dashboard_data.py --ebpf-days 3   # widen the eBPF window
    python3 dashboard/build_dashboard_data.py --json-only     # skip the HTML render

Every panel in the bundle carries ``_prov`` -- one of ``real`` / ``synthetic`` -- plus
the collector it came from, and the UI badges each card accordingly.  Re-running this
on a login node (where ``log/<component>`` resolves to live telemetry) replaces the
synthetic process-tier panels with measured ones; no dashboard code changes.
"""
import argparse
import datetime as dt
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import rc_real
import rc_synth
import reduce_sacct

# panel -> (collector tier, what feeds it, why it matters)
PANEL_META = {
    "jobs":             ("sacct (slurmdbd export)", "settled accounting record per job"),
    "identity":         ("sacctmgr", "accounts, QOS, admin levels, federation"),
    "fleet":            ("eBPF exit + residency_totals", "the same population counted six ways"),
    "purpose_mix":      ("eBPF exit (argv -> purpose)", "count vs cost: polling is ~30% of events, ~2% of CPU"),
    "session_sizes":    ("eBPF exit grouped by session_key", "93.6% of agent sessions are a single exit"),
    "archetypes":       ("trajectory features", "what a session was actually doing"),
    "lifetime":         ("eBPF residency (age_s) + census roots", "agent roots outlive human logins; 24-45% never die"),
    "residency_series": ("eBPF residency / agent census", "resident roots accumulate until the fleet reboot"),
    "depth_profile":    ("eBPF exit (depth)", "orchestrate -> poll -> search -> compute migration"),
    "diurnal":          ("eBPF exit (ts)", "agents are ~flat day/night; humans are not"),
    "exit_status":      ("eBPF exit (exit_code/signal)", "hook-storm vs real work; benign-nonzero filter"),
    "tcp_providers":    ("eBPF tcp + residency.tcp_open_providers", "where the agent's bytes go"),
    "autonomy":         ("eBPF exit (autonomous)", "approval-bypass flags, counted tree-level"),
    "bwrap":            ("login snapshot (blocked_io) + eBPF", "the D-state pile that only a reboot clears"),
    "dangerous":        ("eBPF exit argv catalog", "environment-unaware vs policy-unaware commands"),
    "login_compute":    ("eBPF exit (cpu_s on login hosts)", "compute smuggled onto login nodes"),
    "top_commands":     ("eBPF exit (effective_comm)", "a tiny head of literal commands dominates"),
    "gpu_duty":         ("dcgm x gpu_binding, jobstats trace", "agents hold fewer GPU-hours but drive them harder"),
    "scheduler":        ("sdiag", "slurmctld RPC load and who causes it"),
    "concentration":    ("all tiers", "every aggregate is one-user-dominated"),
    "session_gallery":  ("eBPF exit sequences", "concrete trajectories, as purpose ribbons"),
    "live":             ("eBPF exit + residency_totals · slurm_jobs census",
                         "the current minute, across all three tiers"),
}


def _is_raw_export(path):
    """A raw sacct export is ``____``-delimited; a reduced one is a plain CSV."""
    with open(path, errors="replace") as fh:
        return "____" in fh.readline()


def _tag(obj, prov, panel):
    """Attach provenance to a panel payload (dicts get keys, lists get wrapped)."""
    src, why = PANEL_META.get(panel, ("", ""))
    meta = {"_prov": prov, "_source": src, "_why": why}
    if isinstance(obj, dict):
        out = dict(obj)
        out.update(meta)
        return out
    return dict(meta, rows=obj)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sacct", default=None, help="sacct export CSV (default: newest SlurmData_*.csv)")
    ap.add_argument("--jobs-csv", default=os.environ.get("RC_JOBS_CSV"),
                    help="pre-reduced per-job CSV (skips re-streaming the raw export)")
    ap.add_argument("--ebpf-days", type=int, default=2)
    ap.add_argument("--max-ebpf-records", type=int, default=2_000_000)
    ap.add_argument("--no-synth", action="store_true", help="omit synthetic panels")
    ap.add_argument("--json-only", action="store_true")
    ap.add_argument("--out", default=os.path.join(HERE, "data.json"))
    args = ap.parse_args()

    bundle = {"built_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
              "repo": os.path.basename(REPO), "panels": {}, "sources": [],
              "refresh_s": 60,
              "cadence": {"eBPF / proc-trace": "event + 60 s", "slurm_jobs": "300 s",
                          "slurm_nodes": "300 s", "gpu_binding": "300 s", "dcgm": "60 s",
                          "sdiag": "60 s", "policy": "daily", "sacctmgr": "daily",
                          "sacct": "on demand"}}
    P = bundle["panels"]

    # ---------------------------------------------------------------- real: sacct
    jobs_src = args.jobs_csv or args.sacct or rc_real.find_sacct_csv()
    if jobs_src and os.path.exists(jobs_src):
        label = os.path.basename(jobs_src)
        if _is_raw_export(jobs_src):
            cache = os.path.join(HERE, ".cache", "jobs_" + label)
            os.makedirs(os.path.dirname(cache), exist_ok=True)
            if not os.path.exists(cache) or os.path.getmtime(cache) < os.path.getmtime(jobs_src):
                print("reduce <- %s (step rows folded into job rows)" % label)
                reduce_sacct.reduce(jobs_src, cache)
            jobs_src = cache
        print("sacct  <- %s" % label)
        rolled = rc_real.rollup_sacct(jobs_src, rc_real.load_domains())
        rolled["_file"] = label
        P["jobs"] = _tag(rolled, "real", "jobs")
        bundle["sources"].append({"tier": "sacct", "path": label,
                                  "rows": rolled["window"]["job_rows"], "prov": "real"})

    # ---------------------------------------------------------------- real: sacctmgr
    sm = rc_real.find_sacctmgr()
    if sm:
        print("identity <- %s" % sm)
        P["identity"] = _tag(rc_real.rollup_sacctmgr(sm), "real", "identity")
        bundle["sources"].append({"tier": "sacctmgr", "path": os.path.basename(sm),
                                  "rows": P["identity"]["n_assoc_rows"], "prov": "real"})

    # ---------------------------------------------------------------- real: eBPF / tracer
    files = rc_real.ebpf_files(args.ebpf_days)
    eb = rc_real.rollup_ebpf(files, args.max_ebpf_records) if files else None
    if eb:
        print("ebpf   <- %d files, %d records" % (eb["_files"], eb["_records_read"]))
        for panel in ("fleet", "purpose_mix", "session_sizes", "depth_profile", "diurnal",
                      "exit_status", "tcp_providers", "autonomy"):
            if eb.get(panel):
                P[panel] = _tag(eb[panel], "real", panel)
        if eb.get("top_commands_measured"):
            P["top_commands"] = _tag({"measured": eb["top_commands_measured"]},
                                     "real", "top_commands")
        if eb.get("residency_ticks"):
            P["residency_series"] = _tag({"ticks": eb["residency_ticks"]}, "real",
                                         "residency_series")
        bundle["sources"].append({"tier": "eBPF / proc-trace", "path": "log/ebpf_*",
                                  "rows": eb["_records_read"], "prov": "real"})
    else:
        bundle["sources"].append({"tier": "eBPF / proc-trace", "path": "log/ebpf_* (absent)",
                                  "rows": 0, "prov": "synthetic"})

    # ---------------------------------------------------------------- synthetic fill
    if not args.no_synth:
        syn = rc_synth.build()
        for panel, payload in syn.items():
            if panel not in P:
                P[panel] = _tag(payload, "synthetic", panel)
        bundle["calibration"] = rc_synth.CAL
        bundle["synth_seed"] = rc_synth.SEED

    # the live tier is read by the page's own refresh loop, so it sits at the top level;
    # its panel entry keeps only the chrome (feed name + question) the cards render.
    if "live" in P:
        bundle["live"] = {k: v for k, v in P["live"].items() if not k.startswith("_")}
        P["live"] = {k: v for k, v in P["live"].items() if k.startswith("_")}

    bundle["provenance_summary"] = {
        "real": sorted(k for k, v in P.items() if v.get("_prov") == "real"),
        "synthetic": sorted(k for k, v in P.items() if v.get("_prov") == "synthetic"),
    }

    with open(args.out, "w") as fh:
        json.dump(bundle, fh, separators=(",", ":"))
    print("wrote %s (%.1f KB)" % (args.out, os.path.getsize(args.out) / 1024.0))
    print("  real:      %s" % ", ".join(bundle["provenance_summary"]["real"]))
    print("  synthetic: %s" % ", ".join(bundle["provenance_summary"]["synthetic"]))

    if not args.json_only:
        tpl = os.path.join(HERE, "agent_dashboard.template.html")
        if not os.path.exists(tpl):
            print("no template at %s -- JSON only" % tpl)
            return
        with open(tpl, encoding="utf-8") as fh:
            html = fh.read()
        with open(args.out, encoding="utf-8") as fh:
            blob = fh.read()
        marker = "/*__RC_DATA__*/null"
        if marker not in html:
            sys.exit("template is missing the %s data marker" % marker)
        page = html.replace(marker, blob)

        # Standalone page: a complete document, so a browser opening it over file://
        # (or any server that does not set a charset) still decodes it as UTF-8 and
        # sizes the viewport.  This is the file to open or deploy.
        split = page.index('<div class="app">')
        doc = ('<!doctype html>\n<html lang="en">\n<head>\n'
               '<meta charset="utf-8">\n'
               '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
               + page[:split] +
               '</head>\n<body>\n' + page[split:] + '\n</body>\n</html>\n')
        out = os.path.join(HERE, "agent_dashboard.html")
        with open(out, "w", encoding="utf-8") as fh:
            fh.write(doc)
        print("wrote %s (%.1f KB)" % (out, os.path.getsize(out) / 1024.0))

        # Embeddable fragment: no doctype/html/head/body, for hosts that supply their
        # own document skeleton (the Artifact publisher, an iframe, a CMS block).
        emb = os.path.join(HERE, "agent_dashboard.embed.html")
        with open(emb, "w", encoding="utf-8") as fh:
            fh.write(page)
        print("wrote %s (%.1f KB)" % (emb, os.path.getsize(emb) / 1024.0))


if __name__ == "__main__":
    main()
