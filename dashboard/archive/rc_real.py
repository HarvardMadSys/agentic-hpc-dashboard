#!/usr/bin/env python3
"""Real-data half of the agent-behaviour dashboard bundle.

Reads whatever real telemetry is present on this machine and returns plain-Python
aggregates.  Every aggregate carries its own provenance so the dashboard can mark
a panel REAL vs SYNTHETIC:

  sacct export  (``SlurmData_*.csv``, ``____``-delimited, 78 col)  -> job facts
  ``sacctmgr_*.jsonl``                                             -> identity layer
  ``dataclean/mapping/map_user.csv``                               -> user -> science domain
  ``log/<component>/*.jsonl``                                      -> live collector feeds

Nothing here invents numbers.  If a source is absent the aggregate is omitted and
``build_dashboard_data.py`` substitutes a clearly-labelled synthetic panel.
"""
import csv
import collections
import glob
import json
import os
import re
import statistics

csv.field_size_limit(10 ** 7)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ---------------------------------------------------------------- agent evidence
# Path/command markers an agent leaves in the accounting record.  This is the only
# agent signal the *accounting* tier carries: the authoritative per-submission actor
# comes from the login-node process tier (eBPF `submit` records / the retired
# proc-trace), and the user-level one from ``dataclean/user.csv`` -- neither of which
# is guaranteed present here.
AGENT_MARKERS = [
    ("claude-code", r"\.claude\b|claude-tmp|claude-code|/claude/"),
    ("codex", r"\.codex\b|/codex/|run_.*codex|codex-cli"),
    ("cursor", r"\.cursor\b|/cursor/"),
    ("aider", r"\.aider\b|aider-chat"),
]
AGENT_RE = {name: re.compile(rx, re.I) for name, rx in AGENT_MARKERS}
ANY_AGENT_RE = re.compile("|".join(rx for _, rx in AGENT_MARKERS), re.I)

STATES = ["COMPLETED", "FAILED", "TIMEOUT", "OUT_OF_MEMORY", "CANCELLED", "PREEMPTED",
          "NODE_FAIL", "REQUEUED"]


def _mem_bytes(s):
    if not s:
        return None
    m = re.match(r"([\d.]+)\s*([KMGT]?)", s.strip())
    if not m:
        return None
    return float(m.group(1)) * {"": 1, "K": 1024, "M": 1024 ** 2,
                                "G": 1024 ** 3, "T": 1024 ** 4}[m.group(2)]


def _num(s, cast=float, default=None):
    try:
        return cast(s)
    except (TypeError, ValueError):
        return default


def _pct(part, whole):
    return round(100.0 * part / whole, 2) if whole else 0.0


def _hist(values, edges):
    """Bucket values into ``edges`` (right-open); returns counts per bucket."""
    counts = [0] * (len(edges) + 1)
    for v in values:
        for i, e in enumerate(edges):
            if v < e:
                counts[i] += 1
                break
        else:
            counts[-1] += 1
    return counts


def _quants(values, qs=(0.1, 0.25, 0.5, 0.75, 0.9, 0.99)):
    if not values:
        return {}
    vs = sorted(values)
    out = {}
    for q in qs:
        i = min(len(vs) - 1, max(0, int(round(q * (len(vs) - 1)))))
        out["p%g" % (q * 100)] = round(vs[i], 4)
    out["mean"] = round(sum(vs) / len(vs), 4)
    out["n"] = len(vs)
    return out


# ---------------------------------------------------------------- source discovery
def find_sacct_csv():
    for p in sorted(glob.glob(os.path.join(REPO, "SlurmData_*.csv"))):
        return p
    return None


def find_sacctmgr():
    files = sorted(glob.glob(os.path.join(REPO, "sacctmgr_*.jsonl")))
    return files[-1] if files else None


def load_domains():
    """user -> (domain, lab) from the committed job-type mapping table."""
    path = os.path.join(REPO, "dataclean", "mapping", "map_user.csv")
    if not os.path.exists(path):
        return {}
    out = {}
    with open(path, errors="replace") as fh:
        for row in csv.DictReader(fh):
            out[row["user"]] = (row.get("top_domain") or "unknown", row.get("lab") or "")
    return out


# ---------------------------------------------------------------- sacct rollup
def agent_kind(work_dir, submit_line):
    """Which agent product left a marker in the accounting record, if any."""
    blob = (work_dir or "") + " " + (submit_line or "")
    for name in AGENT_RE:
        if AGENT_RE[name].search(blob):
            return name
    return None


def rollup_sacct(path, domains=None):
    """Stream the per-job CSV (or the raw ``____`` export) into dashboard aggregates.

    Returns a dict of panels.  Grains follow the repo rule: every agent-vs-other
    statement is reported at all four units -- job (array-expanded), submission
    (array-collapsed), user, and work_dir.
    """
    domains = domains or {}
    with open(path, errors="replace") as _fh:          # sniff, don't guess from the name
        raw = "____" in _fh.readline()

    def rows():
        if not raw:
            with open(path, errors="replace") as fh:
                for r in csv.DictReader(fh):
                    yield r
            return
        # raw export: reuse the streaming reducer's logic, job rows only
        with open(path, errors="replace") as fh:
            hdr = fh.readline().rstrip("\n").split("____")
            for line in fh:
                p = line.rstrip("\n").split("____")
                if len(p) != len(hdr):
                    continue
                d = dict(zip(hdr, p))
                if "." in d["JobID"]:
                    continue
                d["req_gpu"] = (re.search(r"gres/gpu=(\d+)", d.get("ReqTRES") or "") or [None, ""])[1]
                d["alloc_gpu"] = (re.search(r"gres/gpu=(\d+)", d.get("AllocTRES") or "") or [None, ""])[1]
                m = re.search(r"gres/gpu:([A-Za-z0-9_.\-]+)=", d.get("AllocTRES") or "")
                d["gpu_model"] = m.group(1) if m else ""
                d["gpuutil"] = ""
                d["total_cpu_s"] = ""
                base = d["JobID"]
                d["is_array"] = "1" if "_" in base else "0"
                d["array_job_id"] = base.split("_")[0]
                yield d

    n_rows = 0
    win = [None, None]
    # per-class accumulators keyed by 'agent' / 'other'
    cls_jobs = collections.Counter()
    cls_state = collections.defaultdict(collections.Counter)
    cls_subs = collections.defaultdict(set)
    cls_users = collections.defaultdict(set)
    cls_wd = collections.defaultdict(set)
    cls_coreh = collections.Counter()
    cls_gpuh = collections.Counter()
    cls_gpujobs = collections.Counter()
    cls_arrays = collections.Counter()
    cls_single = collections.Counter()
    cls_walluse = collections.defaultdict(list)   # elapsed / timelimit
    cls_cpueff = collections.defaultdict(list)
    cls_memeff = collections.defaultdict(list)
    cls_queue = collections.defaultdict(list)
    cls_elapsed = collections.defaultdict(list)
    cls_hour = collections.defaultdict(collections.Counter)   # submit hour-of-day
    cls_tl = collections.defaultdict(list)
    by_product = collections.Counter()
    prod_state = collections.defaultdict(collections.Counter)
    user_jobs = collections.Counter()
    user_cls = {}
    partitions = collections.Counter()
    domain_jobs = collections.defaultdict(collections.Counter)
    # resubmission / retry detection: identical (user, submit_line) repeats
    subline = collections.Counter()
    subline_cls = {}
    gpu_class = collections.defaultdict(collections.Counter)
    timeline = collections.defaultdict(collections.Counter)   # (date, hour) -> class

    for d in rows():
        n_rows += 1
        prod = agent_kind(d.get("WorkDir"), d.get("SubmitLine"))
        c = "agent" if prod else "other"
        state = (d.get("State") or "?").split()[0]
        user = d.get("User") or "?"
        sub = d.get("Submit") or ""
        if sub:
            if win[0] is None or sub < win[0]:
                win[0] = sub
            if win[1] is None or sub > win[1]:
                win[1] = sub
            cls_hour[c][int(sub[11:13]) if len(sub) >= 13 else 0] += 1
            timeline[sub[:13]][c] += 1

        cls_jobs[c] += 1
        cls_state[c][state] += 1
        cls_subs[c].add(d.get("array_job_id") or d.get("JobID"))
        cls_users[c].add(user)
        if d.get("WorkDir"):
            cls_wd[c].add(d["WorkDir"])
        if d.get("is_array") == "1":
            cls_arrays[c] += 1
        user_jobs[user] += 1
        user_cls[user] = c if user_cls.get(user, c) == c else "mixed"
        if d.get("Partition"):
            partitions[d["Partition"]] += 1
        dom = domains.get(user, ("unknown", ""))[0]
        domain_jobs[c][dom] += 1
        if prod:
            by_product[prod] += 1
            prod_state[prod][state] += 1
            sl = (d.get("SubmitLine") or "")[:300]
            if sl:
                subline[(user, sl)] += 1
                subline_cls[(user, sl)] = prod

        ncpus = _num(d.get("NCPUS"), int, 0) or 0
        elapsed = _num(d.get("ElapsedRaw"), int, 0) or 0
        tl = _num(d.get("TimelimitRaw"), int, 0) or 0     # minutes
        agpu = _num(d.get("alloc_gpu"), int, 0) or 0
        if ncpus == 1:
            cls_single[c] += 1
        cls_coreh[c] += ncpus * elapsed / 3600.0
        if agpu:
            cls_gpujobs[c] += 1
            cls_gpuh[c] += agpu * elapsed / 3600.0
        if elapsed:
            cls_elapsed[c].append(elapsed)
        if tl:
            cls_tl[c].append(tl * 60)
            if elapsed:
                cls_walluse[c].append(min(1.5, elapsed / (tl * 60.0)))
        tcpu = _num(d.get("total_cpu_s"), float, None)
        if tcpu and elapsed > 60 and ncpus:
            cls_cpueff[c].append(min(1.5, tcpu / (elapsed * ncpus)))
        rss = _mem_bytes(d.get("MaxRSS"))
        reqmem = _mem_bytes(d.get("ReqMem"))
        if rss and reqmem:
            cls_memeff[c].append(min(1.5, rss / reqmem))
        st, sb = d.get("Start") or "", d.get("Submit") or ""
        if st.startswith("2026") and sb.startswith("2026"):
            import datetime as _dt
            try:
                q = (_dt.datetime.strptime(st, "%Y-%m-%dT%H:%M:%S")
                     - _dt.datetime.strptime(sb, "%Y-%m-%dT%H:%M:%S")).total_seconds()
                if 0 <= q < 30 * 86400:
                    cls_queue[c].append(q)
            except ValueError:
                pass
        # gpu_class per CONSOLIDATION.md §3 (util source: sacct gres/gpuutil)
        if agpu:
            util = _num(d.get("gpuutil"), float, None)
            gpu_class[c]["gpu_used" if (util or 0) > 0 else
                          ("gpu_idle" if util is not None else "gpu_util_unknown")] += 1
        elif (_num(d.get("req_gpu"), int, 0) or 0):
            gpu_class[c]["gpu_requested_unmet"] += 1

    def per(c):
        return {
            "jobs": cls_jobs[c],
            "submissions": len(cls_subs[c]),
            "users": len(cls_users[c]),
            "work_dirs": len(cls_wd[c]),
            "core_hours": round(cls_coreh[c], 1),
            "gpu_hours": round(cls_gpuh[c], 1),
            "gpu_jobs": cls_gpujobs[c],
            "array_pct": _pct(cls_arrays[c], cls_jobs[c]),
            "single_core_pct": _pct(cls_single[c], cls_jobs[c]),
            "array_collapse": round(cls_jobs[c] / max(1, len(cls_subs[c])), 2),
            "state_pct": {s: _pct(cls_state[c][s], cls_jobs[c]) for s in STATES},
            "state_n": {s: cls_state[c][s] for s in STATES},
            "walltime_used_frac": _quants(cls_walluse[c]),
            "cpu_eff": _quants(cls_cpueff[c]),
            "mem_eff": _quants(cls_memeff[c]),
            "queue_wait_s": _quants(cls_queue[c]),
            "elapsed_s": _quants(cls_elapsed[c]),
            "timelimit_s": _quants(cls_tl[c]),
            "hour_of_day": [cls_hour[c].get(h, 0) for h in range(24)],
            "gpu_class": dict(gpu_class[c]),
            "top_domains": domain_jobs[c].most_common(8),
        }

    # retry / loop pathology: same user + same submit line, repeated
    repeats = [{"user": u, "n": n, "product": subline_cls[(u, sl)],
                "submit_line": sl[:180]}
               for (u, sl), n in subline.most_common(12) if n > 1]
    # per-user concentration (Gini + top share) over all users
    counts = sorted(user_jobs.values())
    tot = sum(counts) or 1
    cum = 0.0
    for i, v in enumerate(counts, 1):
        cum += i * v
    gini = round((2 * cum) / (len(counts) * tot) - (len(counts) + 1) / len(counts), 3) if counts else 0.0

    return {
        "window": {"submit_min": win[0], "submit_max": win[1], "job_rows": n_rows},
        "classes": {c: per(c) for c in ("agent", "other")},
        "by_product": dict(by_product),
        "product_state_pct": {p: {s: _pct(prod_state[p][s], by_product[p]) for s in STATES}
                              for p in by_product},
        "retry_repeats": repeats,
        "concentration": {
            "gini_user_jobs": gini,
            "n_users": len(user_jobs),
            "top10_share_pct": _pct(sum(n for _, n in user_jobs.most_common(10)), tot),
            "rank_size": [n for _, n in user_jobs.most_common(60)],
            "top_users": [{"user": u, "jobs": n, "class": user_cls.get(u, "other")}
                          for u, n in user_jobs.most_common(12)],
        },
        "partitions": partitions.most_common(14),
        "timeline": sorted(({"t": k, "agent": v.get("agent", 0), "other": v.get("other", 0)}
                            for k, v in timeline.items()), key=lambda r: r["t"]),
    }


# ---------------------------------------------------------------- purpose taxonomy
def _load_purpose():
    """Reuse the repo's 13-way purpose taxonomy; fall back to a minimal local map.

    ``analyze/purpose_lib.py`` is the source of truth.  It imports matplotlib, which
    the dashboard does not otherwise need, so the import is optional: ``deep_traj_lib``
    is tried next (agent_lib only), and a small built-in map last, so the builder runs
    on a bare Python.
    """
    import sys
    sys.path.insert(0, os.path.join(REPO, "analyze"))
    for mod in ("purpose_lib", "deep_traj_lib"):
        try:
            m = __import__(mod)
            fn = getattr(m, "purpose_of", None)
            if fn:
                return fn, getattr(m, "PURPOSES", None), mod
        except Exception:
            continue
    sets = {
        "poll": {"ps", "pgrep", "pidof", "top", "free", "uptime", "nvidia-smi", "date",
                 "hostname", "id", "whoami", "printenv", "nproc", "w", "who", "tput"},
        "slurm_mon": {"squeue", "sacct", "sstat", "sinfo", "scontrol", "sprio", "watch"},
        "slurm_sub": {"sbatch", "salloc", "srun", "scancel"},
        "build": {"make", "cmake", "ninja", "gcc", "g++", "cc1plus", "nvcc", "ld", "cargo"},
        "git": {"git", "git-remote-https", "gitstatusd"},
        "data": {"rsync", "wget", "curl", "tar", "gzip", "cp", "mv", "scp", "zstd"},
        "compute": {"python", "python3", "R", "Rscript", "julia", "matlab", "jupyter"},
        "runtime": {"node", "claude", "codex", "cursor", "bwrap", "electron", "esbuild"},
        "editor": {"vim", "nvim", "emacs", "nano", "code"},
        "filesearch": {"grep", "rg", "find", "ls", "cat", "sed", "awk", "wc", "sort", "jq",
                       "base64", "head", "tail", "stat", "mkdir", "rm", "xargs", "cut"},
    }
    c2p = {c: k for k, cs in sets.items() for c in cs}
    return (lambda comm: c2p.get(comm or "", "other")), None, "builtin"


# ---------------------------------------------------------------- eBPF / tracer feed
EBPF_DIRS = ("log/ebpf_login", "log/ebpf_compute", "log/agents")


def ebpf_files(limit_days=2):
    """Newest ``<date>/<host>.jsonl`` files across the eBPF / tracer log trees."""
    found = []
    for rel in EBPF_DIRS:
        root = os.path.join(REPO, rel)
        if not os.path.isdir(root):
            continue
        dates = sorted(d for d in os.listdir(root) if d.startswith("20"))
        for d in dates[-limit_days:]:
            found += sorted(glob.glob(os.path.join(root, d, "*.jsonl")))
        found += sorted(glob.glob(os.path.join(root, "*.jsonl")))[-limit_days:]
    return found


def rollup_ebpf(files, max_records=2_000_000):
    """Reduce raw eBPF / proc-trace JSONL into the same panel shapes ``rc_synth``
    produces, so a panel is source-agnostic.

    Handles SCHEMA_VERSION 3 ``exit`` / ``residency`` / ``residency_totals`` /
    ``tcp`` / ``submit`` records and the retired poller's ``exit`` records (which
    carry ``d_samples``/``samples`` instead of delay accounting).
    """
    purpose_of, _, purpose_src = _load_purpose()
    try:
        import sys
        sys.path.insert(0, os.path.join(REPO, "analyze"))
        from agent_lib import effective_comm, actor3 as _actor3
    except Exception:
        effective_comm = lambda comm, args: comm
        _actor3 = None

    n = 0
    hosts = set()
    ev_cls = collections.Counter()
    cpu_cls = collections.Counter()
    purp_ev = collections.defaultdict(collections.Counter)
    purp_cpu = collections.defaultdict(collections.Counter)
    depth_ev = collections.Counter()
    depth_cpu = collections.Counter()
    depth_purp = collections.defaultdict(collections.Counter)
    sess_events = collections.Counter()       # (class, session_key) -> events
    comm_ev = collections.Counter()
    comm_cpu = collections.Counter()
    exit_codes = collections.Counter()
    signals = collections.Counter()
    hour_cls = collections.defaultdict(collections.Counter)
    users_cls = collections.defaultdict(set)
    autonomous = collections.Counter()
    tcp = collections.defaultdict(lambda: {"conns": 0, "tx_mb": 0.0, "rx_mb": 0.0})
    submits = collections.Counter()
    resid = []            # residency ticks
    longlived = [0, 0]    # n, failures
    hooks = collections.Counter()
    types = collections.Counter()

    for path in files:
        try:
            fh = open(path, errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                if n >= max_records:
                    break
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                n += 1
                ev = r.get("event")
                host = r.get("host") or r.get("hostname")
                if host:
                    hosts.add(host)
                if ev == "exit":
                    c = r.get("actor3") or r.get("actor") or "unknown"
                    cpu = r.get("cpu_s") or 0.0
                    ev_cls[c] += 1
                    cpu_cls[c] += cpu
                    comm = effective_comm(r.get("comm") or "", r.get("args") or "")
                    p = purpose_of(comm)
                    purp_ev[c][p] += 1
                    purp_cpu[c][p] += cpu
                    comm_ev[comm or "(empty)"] += 1
                    comm_cpu[comm or "(empty)"] += cpu
                    d = r.get("depth")
                    if d is not None:
                        depth_ev[d] += 1
                        depth_cpu[d] += cpu
                        depth_purp[d][p] += 1
                    sk = r.get("session_key") or (
                        "apid:%s" % r.get("agent_pid") if r.get("agent_pid") else None)
                    if sk:
                        sess_events[(c, host, sk)] += 1
                    ts = r.get("ts") or ""
                    if len(ts) >= 13:
                        hour_cls[c][int(ts[11:13])] += 1
                    if r.get("user"):
                        users_cls[c].add(r["user"])
                    if r.get("agent_type"):
                        types[r["agent_type"]] += 1
                    if r.get("autonomous"):
                        autonomous[c] += 1
                    code, sig = r.get("exit_code"), r.get("signal")
                    if sig:
                        signals[sig] += 1
                    elif code is not None:
                        exit_codes[code] += 1
                    if (r.get("duration_s") or 0) > 60:
                        longlived[0] += 1
                        if sig or (code or 0) != 0:
                            longlived[1] += 1
                    args = r.get("args") or ""
                    for hook in ("PreToolUse", "PostToolUse", "UserPromptSubmit",
                                 "Stop", "SessionEnd", "SessionStart", "Notification"):
                        if hook in args:
                            hooks[hook] += 1
                elif ev == "tcp":
                    prov = r.get("provider") or ("external" if r.get("external") else "local")
                    t = tcp[prov]
                    t["conns"] += 1
                    t["tx_mb"] += (r.get("tx_bytes") or 0) / 1e6
                    t["rx_mb"] += (r.get("rx_bytes") or 0) / 1e6
                elif ev == "submit":
                    submits[r.get("tool") or "?"] += 1
                elif ev == "residency":
                    resid.append({"ts": r.get("ts"), "host": host,
                                  "agent_type": r.get("agent_type"),
                                  "age_s": r.get("age_s"), "n_procs": r.get("n_procs"),
                                  "rss_mb": r.get("rss_mb"), "cpu_s": r.get("cpu_s"),
                                  "d_state": r.get("d_state"),
                                  "tcp_open_ext": r.get("tcp_open_ext"),
                                  "headless": r.get("headless")})

    if not ev_cls and not resid:
        return None

    tot_ev = sum(ev_cls.values()) or 1
    tot_cpu = sum(cpu_cls.values()) or 1.0

    def _mix(c):
        e, k = purp_ev[c], purp_cpu[c]
        te, tk = sum(e.values()) or 1, sum(k.values()) or 1.0
        return [{"purpose": p, "events_pct": round(100 * e[p] / te, 2),
                 "cpu_pct": round(100 * k[p] / tk, 2)}
                for p in sorted(e, key=lambda x: -e[x])]

    sizes = collections.defaultdict(list)
    for (c, _h, _sk), v in sess_events.items():
        sizes[c].append(v)
    sess_panel = {}
    for c, vals in sizes.items():
        vals.sort()
        N = len(vals)
        q = lambda p: vals[min(N - 1, int(p * (N - 1)))]
        pts = []
        for k in range(0, 81):
            x = 10 ** (k / 20.0)
            surv = sum(1 for v in vals if v >= x)
            if surv == 0:
                break
            pts.append({"x": round(x, 3), "ccdf": round(surv / N, 6)})
        sess_panel[c] = {"ccdf": pts, "p50": q(.5), "p90": q(.9), "p99": q(.99),
                         "max": vals[-1], "n_sessions": N,
                         "mean": round(sum(vals) / N, 2),
                         "share_events_top1pct": round(
                             100 * sum(vals[int(.99 * N):]) / max(1, sum(vals)), 1)}

    return {
        "_records_read": n, "_files": len(files), "_purpose_src": purpose_src,
        "fleet": {"events_per_day": dict(ev_cls), "hosts": sorted(hosts),
                  "users": {c: len(u) for c, u in users_cls.items()},
                  "cpu_s": {c: round(v, 1) for c, v in cpu_cls.items()},
                  "agent_types": dict(types)},
        "purpose_mix": {c: _mix(c) for c in purp_ev},
        "session_sizes": sess_panel,
        "depth_profile": {"rows": [
            {"depth": d, "events_pct": round(100 * depth_ev[d] / tot_ev, 2),
             "cpu_per_event_ms": round(1000 * depth_cpu[d] / max(1, depth_ev[d]), 2),
             "purpose_pct": {p: round(100 * v / max(1, sum(depth_purp[d].values())), 1)
                             for p, v in depth_purp[d].most_common(6)}}
            for d in sorted(depth_ev)],
            "mean_depth": round(sum(d * v for d, v in depth_ev.items()) / tot_ev, 3)},
        "diurnal": {c: [round(100 * hour_cls[c].get(h, 0) / max(1, sum(hour_cls[c].values())), 3)
                        for h in range(24)] for c in hour_cls},
        "exit_status": {
            "codes": ([{"exit_code": c, "n": v} for c, v in exit_codes.most_common(10)]
                      + [{"exit_code": None, "signal": s, "n": v}
                         for s, v in signals.most_common(6)]),
            "total": tot_ev,
            "longlived": {"n": longlived[0],
                          "fail_pct": round(100 * longlived[1] / max(1, longlived[0]), 2)},
            "hooks": dict(hooks),
            "comm_mix": [(c, round(100 * v / tot_ev, 2)) for c, v in comm_ev.most_common(14)]},
        "tcp_providers": {"rows": sorted(
            ({"provider": p, "conns": v["conns"], "tx_mb": round(v["tx_mb"], 1),
              "rx_mb": round(v["rx_mb"], 1)} for p, v in tcp.items()),
            key=lambda r: -r["conns"])},
        "submits": dict(submits),
        "residency_ticks": resid[-4000:],
        "autonomy": {"by_class": dict(autonomous)},
        "top_commands_measured": [
            {"comm": c, "events_pct": round(100 * v / tot_ev, 2),
             "cpu_pct": round(100 * comm_cpu[c] / tot_cpu, 2)}
            for c, v in comm_ev.most_common(15)],
    }


# ---------------------------------------------------------------- identity layer
def rollup_sacctmgr(path):
    """QOS / account / admin-level context from the daily ``sacctmgr`` dump."""
    with open(path, errors="replace") as fh:
        rec = json.loads(fh.readline())
    sec = rec.get("sections", {})

    def table(name, ncols):
        txt = (sec.get(name) or {}).get("text") or ""
        return [ln.split("|") for ln in txt.splitlines() if ln.count("|") >= ncols - 1]

    users = table("users_withassoc", 8)
    accounts = table("accounts", 4)
    qos = table("qos", 3)
    clusters = table("clusters", 7)
    admin = collections.Counter(r[2] for r in users if len(r) > 2)
    return {
        "host": rec.get("host"), "timestamp": rec.get("timestamp"),
        "n_assoc_rows": len(users),
        "n_users": len({r[0] for r in users if r and r[0]}),
        "n_accounts": len({r[0] for r in accounts if r and r[0]}),
        "n_qos": len({r[0] for r in qos if r and r[0]}),
        "admin_levels": dict(admin),
        "clusters": [{"cluster": r[0], "rpc": r[3] if len(r) > 3 else "",
                      "federation": r[5] if len(r) > 5 else ""} for r in clusters],
        "federation_rows": len(table("federation", 5)),
        "top_qos": [r[0] for r in qos[:24] if r and r[0]],
    }
