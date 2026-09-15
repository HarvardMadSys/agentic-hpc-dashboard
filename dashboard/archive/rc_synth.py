#!/usr/bin/env python3
"""Synthetic half of the agent-behaviour dashboard bundle.

Every panel the **process tier** would fill (eBPF `exit` / `residency` / `tcp` /
`submit`, the retired proc-trace + census, `dcgm`, `sdiag`) is generated here when
that feed is not present on this machine.  The generator is:

* **schema-faithful** - field names, units and value domains are those of
  ``collect/eBPF_marthen/ebpf_trace.py`` (SCHEMA_VERSION 3) and
  ``collect/common/agent_classify.py``, so a panel wired against synthetic data
  keeps working when the real feed lands;
* **calibrated** - magnitudes come from the published per-era findings
  (``findings/agent.md``, ``findings/agents_extended.md``,
  ``findings/ebpf_trace_analysis.md``), so shapes are right even though rows are
  invented;
* **deterministic** - one seed, so the dashboard does not shimmer between builds;
* **labelled** - the bundle marks every synthetic panel, and the UI paints it with
  a hatched SYNTHETIC badge.  Nothing here is presented as measurement.

Calibration is kept in one table (``CAL``) so a reviewer can diff the assumptions
against the findings docs without reading the generator.
"""
import datetime as dt
import math
import random

SEED = 20260911

# --------------------------------------------------------------------- calibration
CAL = {
    # findings/agent.md - per-day process-exit volume by 3-way class (period3)
    "events_per_day": {"agent": 1_110_000, "human-vscode": 530_000, "human": 300_000},
    # census means: standing instances / roots / cores / RSS-GB / distinct users
    "standing": {
        "claude_code":  dict(instances=894,  roots=600,  cores=4.1, rss_gb=111, users=97,  d_state=5),
        "codex":        dict(instances=1615, roots=628,  cores=2.1, rss_gb=62,  users=34,  d_state=1),
        "cursor":       dict(instances=197,  roots=37,   cores=1.4, rss_gb=39,  users=55,  d_state=2),
        "vscode":       dict(instances=3496, roots=2531, cores=6.3, rss_gb=271, users=220, d_state=14),
        "human":        dict(instances=1514, roots=1025, cores=1.8, rss_gb=24,  users=795, d_state=0),
    },
    # Kaplan-Meier root-lifetime survival probabilities, by agent product
    "km": {
        "claude_code": {"median_h": 3.3, "s1h": 0.65, "s6h": 0.43, "s24h": 0.31, "never_dies": 0.24},
        "codex":       {"median_h": 24.0, "s1h": 0.80, "s6h": 0.62, "s24h": 0.50, "never_dies": 0.45},
        "cursor":      {"median_h": 3.1, "s1h": 0.62, "s6h": 0.40, "s24h": 0.12, "never_dies": 0.02},
    },
    # era-long resident accumulation (period3: 27.8 d, roots 53 -> 4156, RSS 78 -> 348 GB)
    "era": {"days": 28, "roots0": 53, "roots1": 4156, "rss0_gb": 78.2, "rss1_gb": 348.5,
            "cpu0_pct": 501, "cpu1_pct": 986},
    # purpose taxonomy, dual-weighted: (% of events, % of CPU) per class
    "purpose": {
        "agent": {"poll": (29, 2), "runtime": (19, 45), "filesearch": (14, 3),
                  "slurm_mon": (4, 1), "slurm_sub": (1, 1), "build": (6, 12),
                  "compute": (4, 21), "git": (5, 2), "data": (2, 9),
                  "editor": (1, 1), "other": (15, 3)},
        "human-vscode": {"poll": (60, 4), "editor": (12, 28), "filesearch": (7, 3),
                         "slurm_mon": (5, 1), "slurm_sub": (1, 1), "build": (3, 9),
                         "compute": (2, 34), "git": (3, 2), "data": (1, 7),
                         "runtime": (1, 1), "other": (5, 10)},
        "human": {"poll": (38, 4), "slurm_mon": (26, 3), "slurm_sub": (19, 2),
                  "filesearch": (4, 2), "build": (2, 8), "compute": (3, 61),
                  "git": (2, 1), "data": (1, 7), "editor": (1, 1),
                  "runtime": (0, 0), "other": (4, 11)},
    },
    # archetype cascade (% of each class's sessions)
    "archetype": {
        "agent": {"ephemeral": 93.6, "env-churn": 0.4, "slurm-orchestrator": 0.1,
                  "varied-activity": 1.1, "idle-resident": 3.7, "poll-loop": 0.4,
                  "login-compute": 0.2, "ide-monitor": 0.2, "other": 0.3},
        "human-vscode": {"ephemeral": 97.2, "env-churn": 0.4, "slurm-orchestrator": 0.1,
                         "varied-activity": 0.2, "idle-resident": 1.1, "poll-loop": 0.3,
                         "login-compute": 0.0, "ide-monitor": 0.6, "other": 0.1},
        "human": {"ephemeral": 13.6, "env-churn": 43.8, "slurm-orchestrator": 20.3,
                  "varied-activity": 10.2, "idle-resident": 3.8, "poll-loop": 4.2,
                  "login-compute": 0.9, "ide-monitor": 0.0, "other": 3.2},
    },
    # eBPF single-host capture, rtx6000a 2026-08-08 (~45 min, 1 user)
    "ebpf_host": {"exits_median_min": 1798, "exits_p90_min": 3835, "exits_peak_min": 5686,
                  "cpu_s": 8704, "write_gb": 119, "peak_rss_gb": 36.8,
                  "depth_pct": {0: 19.85, 1: 41.39, 2: 33.42, 3: 5.31, 4: 0.03},
                  "success_pct": 99.67, "signal_deaths": 18,
                  "hook_storm_exit_pct": 97.74, "hook_storm_wall_pct": 3.06,
                  "heavy_exits": 586, "heavy_cpu_pct": 94.2,
                  "longlived_n": 43, "longlived_fail_pct": 30.2,
                  "comm_mix": [("node", 10.2), ("sh", 7.7), ("python3", 5.9), ("bash", 5.8),
                               ("mkdir", 4.2), ("base64", 4.1), ("cat", 3.9), ("jq", 3.7),
                               ("git", 3.3), ("grep", 3.1), ("rg", 2.4), ("sed", 2.1)],
                  "hooks": {"PreToolUse": 1769, "PostToolUse": 1721, "UserPromptSubmit": 28,
                            "Stop": 16, "SessionEnd": 10}},
    # autonomy-flagged process exits per era, and the period3 daily level
    "autonomy": {"per_era": [3041, 3649, 10511], "vscode": 0, "human": 14},
    # bwrap D-state pile: monotone within an era, cleared by the reboot
    "bwrap": {"period2_peak": 1438, "period3_level": 0, "spawning_users": 185},
    # GPU: hours by class, and how hard the device is actually driven
    "gpu": {"hours": {"agent": 63_731, "human-vscode": 95_646, "human": 121_312, "mixed": 42_194},
            "sm_util_pct": {"agent": 60.1, "human-vscode": 53.7, "human": 48.9, "mixed": 61.9},
            "power_w": {"agent": 275, "human-vscode": 277, "human": 214, "mixed": 288},
            "duty_gpuh_weighted": {"agent": 72.9, "human": 54.0}},
    # slurmctld control loop (sdiag)
    "sched": {"rpc_s": 57.6, "poll_pct": 67, "partition_info_pct": 26.7,
              "attribution": {"human": 65.5, "human-vscode": 20.2, "agent": 14.3},
              "polls_per_submit": {"agent": 3.2, "human": 1.3},
              "agent_submits": 187_753, "agent_cancels": 10_658, "peak_agent_submits_h": 5773},
    "hosts": ["boslogin06", "boslogin07", "boslogin08",
              "holylogin05", "holylogin06", "holylogin07", "holylogin08"],
    # dangerous / misbehaving command catalog: (cmd, axis, severity, users, works_pct)
    "dangerous": [
        ("nvidia-smi on a login node", "environment-unaware", "low", 131, 100),
        ("pip install on a login node", "policy-unaware", "med", 203, 95),
        ("conda install/create on login", "policy-unaware", "med", 208, 92),
        ("gcc/make build on login", "policy-unaware", "med", 169, 98),
        ("ssh directly into a compute node", "policy-unaware", "med", 75, 40),
        ("http.server / tensorboard / streamlit on login", "policy-unaware", "med", 70, 88),
        ("wget/curl bulk download on login", "policy-unaware", "med", 61, 97),
        ("hf download on login", "policy-unaware", "med", 50, 96),
        ("nvcc compile on a login node", "environment-unaware", "low", 46, 10),
        ("agent server backend left running", "policy-unaware", "med", 39, 90),
        ("headless chromium swarm", "policy-unaware", "low", 33, 95),
        ("git push --force (no lease)", "policy-unaware", "med", 23, 100),
        ("secrets passed in argv", "policy-unaware", "HIGH", 21, 100),
        ("crontab install", "policy-unaware", "med", 18, 85),
        ("curl | bash", "policy-unaware", "med", 17, 90),
        ("podman run", "environment-unaware", "low", 16, 55),
        ("firefox for an OAuth flow", "environment-unaware", "low", 15, 20),
        ("aria2c multi-stream download", "policy-unaware", "med", 13, 95),
        ("sudo", "environment-unaware", "low", 11, 0),
        ("systemctl", "environment-unaware", "low", 6, 0),
        ("apt-get / dpkg", "environment-unaware", "low", 4, 0),
        ("docker run", "environment-unaware", "low", 4, 0),
        ("LAMMPS (lmp) MD run on login", "policy-unaware", "HIGH", 3, 100),
        ("socat proxy bound to 0.0.0.0", "policy-unaware", "HIGH", 1, 100),
        ("chmod 777", "policy-unaware", "low", 1, 100),
    ],
    # login-node compute the agents actually burned (CPU-hours)
    "login_compute": [("lmp (LAMMPS)", 137, 3), ("python3 training/eval", 402, 61),
                      ("make/cc1plus", 289, 44), ("ffmpeg/convert", 71, 12),
                      ("tar/zstd/gzip", 118, 57), ("rsync/scp", 62, 39),
                      ("node (harness)", 40, 96)],
    # top literal command templates, as % of that class's login-node events
    "top_commands": {
        "agent": [("bash -c '( cd <wd> && <cmd> )' wrapper", 12.0),
                  ("make -s superclean (one user's loop)", 6.0),
                  ("source <snapshot>.env env-restore", 20.0),
                  ("pgrep -f <job> (run_in_background 1 Hz)", 9.4),
                  ("squeue -j <id> --noheader", 3.1),
                  ("rg --files <path>", 2.6), ("git status --porcelain", 2.2),
                  ("cat <file>", 2.0), ("jq -r <filter>", 1.7)],
        "human-vscode": [("cpuUsage.sh", 19.6), ("ps -ax -o pid,comm", 21.8),
                         ("ls -l /proc/*/fd/*", 4.1), ("git status", 2.0),
                         ("squeue -u <me>", 1.4)],
        "human": [("watch -n 1 squeue -u <me>", 22.0), ("squeue -u <me>", 12.0),
                  ("sbatch <script>", 8.0), ("ls -l", 4.0), ("scancel <id>", 1.2)],
    },
}

AGENT_TYPES = ("claude_code", "codex", "cursor", "copilot", "windsurf")
CLASSES3 = ("agent", "human-vscode", "human")
PROVIDERS = {"anthropic": (0.46, 12.0, 340.0), "openai": (0.21, 9.0, 260.0),
             "github": (0.14, 3.0, 90.0), "pypi": (0.07, 1.0, 210.0),
             "huggingface": (0.05, 0.6, 900.0), "google": (0.04, 0.8, 40.0),
             "other": (0.03, 0.5, 30.0)}


def _rng():
    return random.Random(SEED)


def _pareto(rng, alpha, xm, cap):
    """Heavy tail: p50 tiny, p99 enormous - the shape every session metric has."""
    return min(cap, xm * (1.0 - rng.random()) ** (-1.0 / alpha))


# --------------------------------------------------------------------- panels
def fleet(rng):
    st = CAL["standing"]
    real_agent = ("claude_code", "codex", "cursor")
    return {
        "events_per_day": CAL["events_per_day"],
        "standing": st,
        "roots": {"agent": sum(st[t]["roots"] for t in real_agent),
                  "human-vscode": st["vscode"]["roots"], "human": st["human"]["roots"]},
        "cores": {"agent": round(sum(st[t]["cores"] for t in real_agent), 1),
                  "human-vscode": st["vscode"]["cores"], "human": st["human"]["cores"]},
        "rss_gb": {"agent": sum(st[t]["rss_gb"] for t in real_agent),
                   "human-vscode": st["vscode"]["rss_gb"], "human": st["human"]["rss_gb"]},
        "users": {"agent": 505, "human-vscode": 484, "human": 1298, "mixed": 109, "unknown": 174},
        "hosts": CAL["hosts"],
        "headless_pct": 96,
        "per_user_exits_day": {"agent": 2200, "human": 233},
        "spawn_rate_per_s": {"agent": 20.0, "human": 3.5},
    }


def purpose_mix(rng):
    return {c: [{"purpose": p, "events_pct": e, "cpu_pct": k}
                for p, (e, k) in sorted(CAL["purpose"][c].items(), key=lambda kv: -kv[1][0])]
            for c in CLASSES3}


def session_sizes(rng):
    """Per-class CCDF of events-per-session, sampled from the published quantiles.

    The shape is the point: p50 = 1 event, p90 = 15, max in the thousands, and a
    top-1% of sessions holding roughly half of all events.  Anything log-normal-ish
    looks wrong here, so the generator samples an inverse-CDF interpolated through
    the measured anchors instead of fitting a parametric family.
    """
    ANCHORS = {
        # p -> events-per-session, from findings/agent.md (eBPF apid grouping)
        "agent":        [(0.0, 1), (0.50, 1), (0.90, 15), (0.99, 180),
                         (0.999, 1200), (0.99995, 7309), (1.0, 7309)],
        "human-vscode": [(0.0, 1), (0.50, 1), (0.90, 9), (0.99, 95),
                         (0.999, 640), (0.99995, 2400), (1.0, 2400)],
        "human":        [(0.0, 1), (0.50, 4), (0.90, 62), (0.99, 520),
                         (0.999, 1900), (0.99995, 5200), (1.0, 5200)],
    }

    def _q(anchors, p):
        for (p0, v0), (p1, v1) in zip(anchors, anchors[1:]):
            if p <= p1:
                if p1 == p0:
                    return v1
                f = (p - p0) / (p1 - p0)
                return max(1, int(round(math.exp(math.log(v0) * (1 - f)
                                                 + math.log(v1) * f))))
        return anchors[-1][1]

    out = {}
    for c, anchors in ANCHORS.items():
        N = 40000
        vals = sorted(_q(anchors, rng.random()) for _ in range(N))
        pts, tot = [], sum(vals)
        for k in range(0, 81):
            x = 10 ** (k / 20.0)
            surv = sum(1 for v in vals if v >= x)
            if surv == 0:
                break
            pts.append({"x": round(x, 3), "ccdf": round(surv / N, 6)})
        q = lambda p: vals[min(N - 1, int(p * (N - 1)))]
        out[c] = {"ccdf": pts, "p50": q(.5), "p90": q(.9), "p99": q(.99), "max": vals[-1],
                  "n_sessions": N, "mean": round(tot / N, 2),
                  "share_events_top1pct": round(100.0 * sum(vals[int(.99 * N):]) / tot, 1)}
    return out


def archetypes(rng):
    return CAL["archetype"]


def lifetime(rng):
    """Kaplan-Meier-shaped root survival per product, hitting the published S(t)."""
    out = {}
    for prod, k in CAL["km"].items():
        anchors = [(0.0, 1.0), (1.0, k["s1h"]), (6.0, k["s6h"]), (24.0, k["s24h"]),
                   (24 * 7.0, k["never_dies"] + (k["s24h"] - k["never_dies"]) * 0.35),
                   (24 * 27.8, k["never_dies"])]
        curve, prev_t, prev_s = [], None, None
        for t, s in anchors:
            if prev_t is None:
                curve.append({"t_h": t, "s": round(s, 4)})
            else:
                steps = 14
                for i in range(1, steps + 1):
                    # geometric interpolation in t, linear in log S
                    f = i / steps
                    tt = prev_t + (t - prev_t) * f if prev_t > 0 else t * f
                    ss = math.exp(math.log(max(prev_s, 1e-6)) * (1 - f)
                                  + math.log(max(s, 1e-6)) * f)
                    curve.append({"t_h": round(tt, 3), "s": round(ss, 4)})
            prev_t, prev_s = t, s
        out[prod] = {"curve": curve, "median_h": k["median_h"],
                     "never_dies_pct": round(100 * k["never_dies"], 1),
                     "born_per_host_h": round(rng.uniform(0.9, 1.2), 2),
                     "died_per_host_h": round(rng.uniform(0.45, 0.9), 2)}
    out["_age_grain"] = {"agent_roots": {"median_d": 7.5, "mean_d": 9.0, "p90_d": 18.5,
                                         "max_d": 27.8, "over_1d_pct": 93.6},
                         "human_tty": {"median_d": 3.6, "mean_d": 6.7, "over_1d_pct": 68.4}}
    out["_instance_grain"] = {"agent": {"p50_s": 1.5, "p90_s": 300.0},
                              "human": {"p50_s": 2.1, "p90_s": 10.0}}
    return out


def residency_series(rng):
    """The era sawtooth: resident roots climb monotonically, reboot zeroes them."""
    e = CAL["era"]
    start = dt.date(2026, 7, 6)
    rows = []
    for d in range(e["days"]):
        f = d / max(1, e["days"] - 1)
        # accumulation is super-linear early, then saturating
        g = f ** 0.75
        roots = e["roots0"] + (e["roots1"] - e["roots0"]) * g
        rss = e["rss0_gb"] + (e["rss1_gb"] - e["rss0_gb"]) * (f ** 0.55)
        cpu = e["cpu0_pct"] + (e["cpu1_pct"] - e["cpu0_pct"]) * (f ** 0.5)
        jitter = 1.0 + rng.uniform(-0.04, 0.04)
        rows.append({"date": (start + dt.timedelta(days=d)).isoformat(),
                     "day": d + 1,
                     "roots": int(roots * jitter),
                     "rss_gb": round(rss * jitter, 1),
                     "cpu_pct": round(cpu * jitter, 1),
                     "rss_per_root_mb": round(rss * 1024 / max(1, roots), 1),
                     "cpu_per_root_pct": round(cpu / max(1, roots), 3),
                     "bwrap_d_state": 0})
    return {"rows": rows,
             "note": "resident roots 53 -> 4,156 over 27.8 d (77.9x); cleared only by the fleet reboot"}


def depth_profile(rng):
    dp = CAL["ebpf_host"]["depth_pct"]
    # purpose migrates with depth: orchestrate -> poll -> search -> compute -> compile
    shape = {0: {"runtime": 62, "poll": 14, "other": 24},
             1: {"poll": 44, "filesearch": 18, "runtime": 12, "build": 8,
                 "git": 8, "other": 10},
             2: {"filesearch": 30, "poll": 22, "compute": 16, "build": 14, "git": 8,
                 "other": 10},
             3: {"compute": 34, "build": 30, "filesearch": 14, "data": 12, "other": 10},
             4: {"build": 44, "compute": 34, "data": 12, "other": 10}}
    rows = []
    for d, pct in sorted(dp.items()):
        rows.append({"depth": d, "events_pct": pct,
                     "cpu_per_event_ms": round(8 * (1.9 ** d) * rng.uniform(.9, 1.1), 1),
                     "purpose_pct": shape.get(d, {"other": 100})})
    return {"rows": rows, "mean_depth": 1.24, "max_depth": 6,
            "agent_le1_pct": 64, "agent_le2_pct": 87}


def diurnal(rng):
    """Hour-of-day event shares. Agent nearly flat (94%), human strongly day-bound."""
    out = {}
    for c, flat, peak_h in (("agent", 0.94, 15), ("human-vscode", 0.78, 14),
                            ("human", 0.85, 13)):
        vals = []
        for h in range(24):
            diur = math.cos((h - peak_h) / 24.0 * 2 * math.pi)
            amp = (1 - flat)
            vals.append(round(1.0 + amp * diur + rng.uniform(-0.015, 0.015), 4))
        s = sum(vals)
        out[c] = [round(100 * v / s, 3) for v in vals]
    out["_note"] = "agent day/night ratio 94%, human 85%; agent trough only ~24% below its mean"
    return out


def exit_status(rng):
    h = CAL["ebpf_host"]
    total = 96_000
    ok = int(total * h["success_pct"] / 100.0)
    fails = total - ok - h["signal_deaths"]
    # benign-nonzero: grep=1, ls glob=2, git fatal=128
    codes = [{"exit_code": 0, "n": ok, "benign": True, "label": "success"},
             {"exit_code": 1, "n": int(fails * .58), "benign": True, "label": "grep/test no-match"},
             {"exit_code": 2, "n": int(fails * .19), "benign": True, "label": "glob/ls not found"},
             {"exit_code": 128, "n": int(fails * .09), "benign": True, "label": "git fatal"},
             {"exit_code": 127, "n": int(fails * .06), "benign": False, "label": "command not found"},
             {"exit_code": 126, "n": int(fails * .03), "benign": False, "label": "not executable"},
             {"exit_code": 137, "n": int(fails * .03), "benign": False, "label": "OOM / SIGKILL wrap"},
             {"exit_code": None, "signal": 15, "n": 17, "benign": True, "label": "SIGTERM teardown"},
             {"exit_code": None, "signal": 13, "n": 1, "benign": False, "label": "SIGPIPE"}]
    return {"codes": codes, "total": total,
            "longlived": {"n": h["longlived_n"], "fail_pct": h["longlived_fail_pct"],
                          "max_duration_s": 20549},
            "hook_storm": {"exit_pct": h["hook_storm_exit_pct"], "wall_pct": h["hook_storm_wall_pct"]},
            "heavy": {"exits": h["heavy_exits"], "exits_pct": 0.612, "cpu_pct": h["heavy_cpu_pct"],
                      "write_pct": 99.94},
            "hooks": h["hooks"], "comm_mix": h["comm_mix"],
            "burst": {"median_per_min": h["exits_median_min"], "p90_per_min": h["exits_p90_min"],
                      "peak_per_min": h["exits_peak_min"], "peak_per_s": 442,
                      "p90_minutes_share_pct": 24.0}}


def tcp_providers(rng):
    """eBPF `tcp` records rolled up by `provider` (nettcp/provider_cidrs.py buckets)."""
    conns = 41_900
    rows = []
    for p, (share, tx_kb, rx_kb) in PROVIDERS.items():
        n = int(conns * share * rng.uniform(.95, 1.05))
        rows.append({"provider": p, "conns": n,
                     "tx_mb": round(n * tx_kb / 1024.0, 1),
                     "rx_mb": round(n * rx_kb / 1024.0, 1),
                     "median_duration_s": round(rng.uniform(1.4, 46.0), 1)})
    rows.sort(key=lambda r: -r["conns"])
    return {"rows": rows, "standing_open_ext_per_root": 2.1,
            "note": "connections opened before the tracer attached appear only in "
                    "`residency.tcp_open_providers`, never as a `tcp` close record"}


def autonomy(rng):
    per_era = CAL["autonomy"]["per_era"]
    start = dt.date(2026, 7, 6)
    daily = []
    for d in range(28):
        base = per_era[2] / 28.0
        daily.append({"date": (start + dt.timedelta(days=d)).isoformat(),
                      "tree_level": int(base * rng.uniform(.55, 1.7)),
                      "root_only": int(base * rng.uniform(.2, .55))})
    return {"per_era": [{"era": e, "exits": n} for e, n in
                        zip(("period1", "period2", "period3"), per_era)],
            "daily": daily, "vscode_exits": CAL["autonomy"]["vscode"],
            "human_exits": CAL["autonomy"]["human"],
            "note": "count tree-level: Codex puts bypass flags on CHILD processes, so "
                    "root-only counting under-counts badly"}


def bwrap(rng):
    rows = []
    start = dt.date(2026, 6, 18)
    for d in range(18):          # period2: monotone climb to the peak
        rows.append({"date": (start + dt.timedelta(days=d)).isoformat(), "era": "period2",
                     "d_state": int(CAL["bwrap"]["period2_peak"] * (d / 17.0) ** 1.25)})
    start3 = dt.date(2026, 7, 6)
    for d in range(21):          # period3: held at zero all 21 days
        rows.append({"date": (start3 + dt.timedelta(days=d)).isoformat(), "era": "period3",
                     "d_state": 0})
    return {"rows": rows, "spawning_users": CAL["bwrap"]["spawning_users"],
            "note": "alarm on bwrap-in-D + blocked_io, not on load average - the pile "
                    "climbs days before load moves"}


def dangerous(rng):
    return [{"command": c, "axis": a, "severity": s, "users": u, "works_pct": w}
            for c, a, s, u, w in CAL["dangerous"]]


def login_compute(rng):
    rows = [{"command": c, "cpu_h": h, "users": u} for c, h, u in CAL["login_compute"]]
    return {"rows": rows, "total_cpu_h": sum(r["cpu_h"] for r in rows),
            "note": "agents burned ~1,119 CPU-h of *compute* on login nodes in one era"}


def top_commands(rng):
    return CAL["top_commands"]


def gpu_duty(rng):
    g = CAL["gpu"]
    return {"hours": g["hours"], "sm_util_pct": g["sm_util_pct"], "power_w": g["power_w"],
            "duty_gpuh_weighted": g["duty_gpuh_weighted"],
            "note": "agents hold FEWER GPU-hours than humans (0.53x) but drive the device "
                    "HARDER (+18.9 pts duty cycle) - both panels must be read together"}


def scheduler(rng):
    s = CAL["sched"]
    hours = []
    for h in range(24 * 7):
        base = s["rpc_s"] * (1 + 0.22 * math.cos((h % 24 - 14) / 24.0 * 2 * math.pi))
        hours.append({"h": h, "rpc_s": round(base * rng.uniform(.9, 1.1), 1)})
    return dict(s, series=hours)


def concentration(rng):
    """Zipf rank-size of per-user event volume, solved to the published Gini.

    A Zipf law ``v(r) = r**-s`` is fitted by bisection so the resulting Gini lands on
    the measured 0.94-0.97 band, rather than sampled (sampling a tail this heavy has
    enough variance to miss the band by 0.1).
    """
    def _gini(vals):
        asc = sorted(vals)
        tot = sum(asc) or 1.0
        cum = sum(i * v for i, v in enumerate(asc, 1))
        return (2 * cum) / (len(asc) * tot) - (len(asc) + 1) / len(asc)

    def _fit(n, target):
        lo, hi = 0.2, 6.0
        for _ in range(60):
            mid = (lo + hi) / 2
            vals = [(r ** -mid) for r in range(1, n + 1)]
            if _gini(vals) < target:
                lo = mid
            else:
                hi = mid
        return [(r ** -lo) for r in range(1, n + 1)], lo

    out = {}
    for c, n, target in (("agent", 505, 0.96), ("human", 1298, 0.95)):
        vals, expo = _fit(n, target)
        tot = sum(vals)
        out[c] = {"rank_size": [round(v / tot * 100, 4) for v in vals[:80]],
                  "gini": round(_gini(vals), 3), "zipf_exponent": round(expo, 3),
                  "top1_share_pct": round(100 * vals[0] / tot, 1),
                  "top10_share_pct": round(100 * sum(vals[:10]) / tot, 1),
                  "n_users": n}
    out["_documented"] = {"gini_range": [0.94, 0.97],
                          "top_agent_submitter_share_pct": 84.0,
                          "top_agent_canceller_share_pct": 47.9}
    out["_note"] = ("every misbehaviour pattern in this corpus is one-user-dominated: "
                    "the top agent submitter alone is ~84% of agent submissions, so read "
                    "every count-weighted agent headline as one user until proven otherwise")
    return out


def session_gallery(rng):
    """A few concrete synthetic sessions, so the UI can show a real trajectory ribbon."""
    purposes = ["poll", "runtime", "filesearch", "build", "compute", "git",
                "slurm_sub", "slurm_mon", "data", "editor", "other"]
    shapes = {"poll-loop": ["poll"] * 18 + ["slurm_mon"] * 4,
              "slurm-orchestrator": ["filesearch", "slurm_sub", "slurm_mon",
                                     "slurm_mon", "poll", "slurm_sub", "git"],
              "login-compute": ["filesearch", "build", "compute", "compute", "compute", "poll"],
              "env-churn": ["other", "other", "build", "other", "poll"],
              "varied-activity": ["filesearch", "git", "build", "compute", "editor",
                                  "slurm_sub", "poll", "data"],
              "idle-resident": ["runtime", "poll", "poll"]}
    rows = []
    for i in range(24):
        arch = list(shapes)[i % len(shapes)]
        prod = ("claude_code", "codex", "cursor")[i % 3]
        seq = shapes[arch] * rng.randint(1, 9)
        rng.shuffle(seq) if arch == "varied-activity" else None
        n = len(seq)
        dom = max(set(seq), key=seq.count)
        rows.append({
            "session_id": "period3|2026-07-%02d|%s|agent|apid:%d" % (
                12 + i % 14, CAL["hosts"][i % 7], 20000 + i * 137),
            "host": CAL["hosts"][i % 7], "agent_type": prod, "archetype": arch,
            "n_events": n, "distinct_comms": len(set(seq)),
            "dom_frac": round(seq.count(dom) / n, 2),
            "shape": "loop" if seq.count(dom) / n >= 0.8 else
                     ("varied" if len(set(seq)) >= 5 else "mixed"),
            "duration_s": round(n * rng.uniform(1.5, 90), 1),
            "cpu_s": round(n * rng.uniform(0.01, 3.5), 2),
            "headless": True, "autonomous": rng.random() < 0.09,
            "max_depth": min(6, 1 + int(rng.random() * 3)),
            "purpose_seq": seq[:80],
        })
    rows.sort(key=lambda r: -r["n_events"])
    return {"rows": rows, "purposes": purposes}


def build(seed=SEED):
    rng = random.Random(seed)
    return {
        "fleet": fleet(rng),
        "purpose_mix": purpose_mix(rng),
        "session_sizes": session_sizes(rng),
        "archetypes": archetypes(rng),
        "lifetime": lifetime(rng),
        "residency_series": residency_series(rng),
        "depth_profile": depth_profile(rng),
        "diurnal": diurnal(rng),
        "exit_status": exit_status(rng),
        "tcp_providers": tcp_providers(rng),
        "autonomy": autonomy(rng),
        "bwrap": bwrap(rng),
        "dangerous": dangerous(rng),
        "login_compute": login_compute(rng),
        "top_commands": top_commands(rng),
        "gpu_duty": gpu_duty(rng),
        "scheduler": scheduler(rng),
        "concentration": concentration(rng),
        "session_gallery": session_gallery(rng),
        "live": live(rng),
    }


# --------------------------------------------------------------------- live state
LIVE_ARGS = [
    ("pgrep", "pgrep -f run_sweep_7b", "poll", 0),
    ("ps", "ps -o pid,stat,rss,args -u $USER", "poll", 0),
    ("squeue", "squeue -j 8112934 --noheader -o %T", "slurm_mon", 0),
    ("sbatch", "sbatch --parsable --array=0-63 sweep.slurm", "slurm_sub", 0),
    ("rg", "rg --files-with-matches 'def forward' src/", "filesearch", 0),
    ("cat", "cat /n/holylabs/Lab/ckpt/run_7b/config.json", "filesearch", 0),
    ("jq", "jq -r '.metrics.loss' out/step_4400.json", "filesearch", 0),
    ("git", "git status --porcelain=v1", "git", 0),
    ("git", "git diff --stat HEAD~1", "git", 0),
    ("make", "make -s superclean", "build", 0),
    ("cc1plus", "cc1plus -O2 -std=c++17 kernels/attention.cu", "build", 0),
    ("python3", "python3 tools/eval_ckpt.py --step 4400", "compute", 0),
    ("python3", "python3 -c 'import torch; print(torch.__version__)'", "compute", 0),
    ("node", "node --no-warnings .claude/hooks/PostToolUse.js", "runtime", 0),
    ("node", "node --no-warnings .claude/hooks/PreToolUse.js", "runtime", 0),
    ("bash", "bash -c '( cd /n/home04/lab/proj && ./scripts/check.sh )'", "other", 0),
    ("tar", "tar -I zstd -cf shards/part_012.tar.zst data/part_012", "data", 1),
    ("rsync", "rsync -a --info=progress2 ckpt/ /n/netscratch/lab/ckpt/", "data", 1),
    ("grep", "grep -rn 'nan' logs/train_7b.log", "filesearch", 1),
    ("nvidia-smi", "nvidia-smi --query-gpu=utilization.gpu --format=csv", "poll", 0),
    ("scancel", "scancel 8112901", "slurm_sub", 0),
    ("sed", "sed -i 's/lr=3e-4/lr=1e-4/' configs/7b.yaml", "filesearch", 0),
]
LIVE_USERS = [("kdesai", "claude_code"), ("ylin42", "claude_code"), ("thomwg11", "claude_code"),
              ("pbonilla", "codex"), ("aweiss", "cursor"), ("mgarcia9", "claude_code"),
              ("rjoshi", "codex"), ("sopark", None), ("cminsky", None), ("dkufel", "claude_code")]


def live(rng):
    """The live tier: what the collectors are reporting right now.

    ``rate`` is the last 90 minutes of process exits per minute per class; ``events``
    is the tail of the eBPF ``exit`` stream; ``hosts`` is the current
    ``residency_totals`` roll-up per login node; ``now`` is the header read-out.
    """
    now = dt.datetime.now().replace(second=0, microsecond=0)
    base = {"agent": CAL["events_per_day"]["agent"] / 1440.0,
            "human-vscode": CAL["events_per_day"]["human-vscode"] / 1440.0,
            "human": CAL["events_per_day"]["human"] / 1440.0}
    rate, level = [], dict(base)
    for i in range(90, 0, -1):
        t = now - dt.timedelta(minutes=i)
        diur = 1.0 + 0.10 * math.cos((t.hour - 15) / 24.0 * 2 * math.pi)
        row = {"t": t.strftime("%H:%M")}
        for c in CLASSES3:
            # mean-reverting walk, with the occasional agent burst (hook storms)
            level[c] += (base[c] * diur - level[c]) * 0.35 + rng.gauss(0, base[c] * 0.08)
            burst = 2.4 if (c == "agent" and rng.random() < 0.06) else 1.0
            row[c] = max(1, int(level[c] * burst))
        rate.append(row)

    hosts = []
    for h in CAL["hosts"]:
        roots = int(rng.uniform(0.7, 1.4) * CAL["era"]["roots1"] / len(CAL["hosts"]))
        hosts.append({"host": h, "roots": roots,
                      "procs": int(roots * rng.uniform(1.4, 2.6)),
                      "rss_gb": round(roots * rng.uniform(0.06, 0.13), 1),
                      "cpu_pct": round(rng.uniform(120, 1100), 1),
                      "d_state": int(rng.random() * 4),
                      "users": int(rng.uniform(28, 96)),
                      "tcp_open_ext": int(roots * rng.uniform(0.4, 1.1)),
                      "load_1": round(rng.uniform(2.5, 34.0), 1)})

    events = []
    t = dt.datetime.now()
    for i in range(140):
        comm, args, purpose, heavy = LIVE_ARGS[rng.randrange(len(LIVE_ARGS))]
        user, prod = LIVE_USERS[rng.randrange(len(LIVE_USERS))]
        cls = "agent" if prod in ("claude_code", "codex") else (
              "human-vscode" if prod == "cursor" else "human")
        t -= dt.timedelta(seconds=rng.expovariate(1 / 2.6))
        dur = _pareto(rng, 1.25, 0.02, 21000) * (60 if heavy else 1)
        code, sig = 0, None
        r = rng.random()
        if r > 0.985:
            code, sig = None, 15
        elif r > 0.96:
            code = 1
        events.append({
            "ts": t.strftime("%H:%M:%S"), "host": CAL["hosts"][rng.randrange(7)],
            "user": user, "agent_type": prod, "actor3": cls, "comm": comm,
            "args": args, "purpose": purpose, "depth": min(4, int(abs(rng.gauss(1.3, 0.9)))),
            "duration_s": round(dur, 3),
            "cpu_s": round(dur * rng.uniform(0.002, 0.7), 3),
            "peak_rss_mb": round(rng.uniform(2, 900) * (40 if heavy else 1), 1),
            "exit_code": code, "signal": sig,
        })
    events.sort(key=lambda e: e["ts"], reverse=True)

    submits = []
    ts = now
    for i in range(14):
        user, prod = LIVE_USERS[rng.randrange(6)]
        ts -= dt.timedelta(seconds=rng.expovariate(1 / 95.0))
        submits.append({
            "ts": ts.strftime("%H:%M:%S"), "host": CAL["hosts"][rng.randrange(7)],
            "user": user, "agent_type": prod, "tool": "sbatch" if rng.random() > .12 else "salloc",
            "job_id": 8112000 + rng.randrange(9000),
            "partition": ["gpu", "gpu_requeue", "sapphire", "serial_requeue", "kempner_h100",
                          "shared"][rng.randrange(6)],
            "gpus": rng.choice([0, 0, 1, 1, 2, 4, 8]),
            "array": rng.choice(["", "", "0-63", "0-479", "0-15"]),
        })

    last = rate[-1]
    return {
        "updated": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "rate": rate, "hosts": hosts, "events": events, "submits": submits,
        "now": {
            "exits_per_min": sum(last[c] for c in CLASSES3),
            "exits_per_min_agent": last["agent"],
            "agent_roots": sum(h["roots"] for h in hosts),
            "agent_procs": sum(h["procs"] for h in hosts),
            "rss_gb": round(sum(h["rss_gb"] for h in hosts), 1),
            "d_state": sum(h["d_state"] for h in hosts),
            "tcp_open_ext": sum(h["tcp_open_ext"] for h in hosts),
            "running_agent_jobs": 1184, "pending_agent_jobs": 417,
            "agent_gpu_jobs": 263, "agent_gpu_idle_now": 61,
            "autonomous_roots": 34,
        },
    }
