"""Three risk views the prototype only ever faked, now measured.

The retired demo shipped `dangerous` and `login_compute` as hardcoded literals
in `rc_synth.CAL` -- user counts and success rates calibrated to published
findings, not computed from anything. They are computable from the schema-5
exit stream, so here they are computed.

All three share one framing that matters: **the eBPF tier runs on login nodes
only** (`collector/README.md`, "Relationship to the other collectors").
So a build or an interpreter burning CPU in this feed *is by construction*
compute on a login node -- there is no need to infer the host's role, and the
per-host breakdown is there to say which one.

On `works_pct`: a policy-unaware command that SUCCEEDS is the interesting case.
One that fails was stopped by something; one that returns 0 means the guardrail
does not exist. That is why the exit code is carried rather than just the count.
"""
import collections
import os
import re
import sys

from . import INSTANCE_CAP, pct, who

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "..", "analyze"))
try:
    from tool_bucket_lib import bucket
except Exception:
    def bucket(ec):                                      # noqa: E301
        return "other"

# Buckets that represent real computation rather than orchestration. `build` and
# `interpreter` are the two that have no business consuming login-node CPU;
# `net/data` is bulk transfer, which is a separate policy question, so it is
# carried but flagged distinctly.
COMPUTE_BUCKETS = ("build", "interpreter")
TRANSFER_BUCKETS = ("net/data",)

# (id, label, axis, severity, regex over "<effective_comm> <args>")
#
# axis:
#   environment-unaware -- it runs, and tells you nothing useful here (no GPU on
#                          a login node, so `nvidia-smi` is a wasted question)
#   policy-unaware      -- it works, and it is what the facility asks you not to do
#
# Ported from the prototype's own catalogue so the categories stay comparable
# with `findings/`, but every number beside them is now measured.
DANGEROUS = [
    # environment-unaware ONLY on a GPU-less login node (true of the FASRC
    # fleet this was written for, false on a GPU dev box like rtx6000a, where
    # nvidia-smi is a perfectly sensible question). See the panel's basis note.
    ("nvidia_smi_login", "nvidia-smi (pointless without a local GPU)",
     "environment-unaware", "low", r"(?:^|/)nvidia-smi\b"),
    ("nvcc_login", "nvcc compile on a login node", "environment-unaware", "low",
     r"(?:^|/)nvcc\b"),
    ("pip_install", "pip install on a login node", "policy-unaware", "med",
     r"(?:^|/)pip3?\b[^\n]*\binstall\b"),
    ("conda_install", "conda/mamba install or create on login", "policy-unaware", "med",
     r"(?:^|/)(?:conda|mamba|micromamba)\b[^\n]*\b(?:install|create|env\s+create)\b"),
    ("build_login", "compiler or make on a login node", "policy-unaware", "med",
     r"(?:^|/)(?:make|cmake|ninja|gcc|g\+\+|clang|nvcc|cargo|go)\b"),
    ("ssh_compute", "ssh straight into a compute node", "policy-unaware", "med",
     r"(?:^|/)ssh\b[^\n]*\b(?:holy|bos|node|gpu|c\d{3,})[\w.-]*"),
    ("server_on_login", "http.server / tensorboard / streamlit on login",
     "policy-unaware", "med",
     r"-m\s+http\.server|(?:^|/)tensorboard\b|(?:^|/)streamlit\b|(?:^|/)jupyter\b[^\n]*\b(?:lab|notebook)\b"),
    ("bulk_download", "wget/curl bulk download on login", "policy-unaware", "med",
     r"(?:^|/)(?:wget|curl)\b[^\n]*(?:-O|--output|-o\s)"),
    ("hf_download", "huggingface download on login", "policy-unaware", "med",
     r"(?:huggingface-cli|(?:^|/)hf)\b[^\n]*\bdownload\b"),
    ("headless_browser", "headless browser swarm", "policy-unaware", "low",
     r"(?:chromium|chrome|msedge)[^\n]*--headless|(?:^|/)playwright\b|(?:^|/)puppeteer\b"),
    ("git_force_push", "git push --force", "policy-unaware", "low",
     r"(?:^|/)git\b[^\n]*\bpush\b[^\n]*(?:--force(?!-with-lease)|(?<!\w)-f(?!\w))"),
    ("rm_rf_shared", "rm -rf against a shared path", "policy-unaware", "HIGH",
     r"(?:^|/)rm\b[^\n]*-[a-zA-Z]*[rR][a-zA-Z]*f|(?:^|/)rm\b[^\n]*-[a-zA-Z]*f[a-zA-Z]*[rR]"),
    ("chmod_777", "chmod 777", "policy-unaware", "med",
     r"(?:^|/)chmod\b[^\n]*\b777\b"),
]

# (id, label, severity, regex).  NOTHING matched by these is ever emitted --
# see SecretsScan.  The point is to count exposure, not to reproduce it.
SECRETS = [
    ("openai_key", "OpenAI/Anthropic-style key literal", "HIGH",
     r"\bsk-[A-Za-z0-9_\-]{20,}"),
    ("github_pat", "GitHub personal access token", "HIGH",
     r"\bgh[pousr]_[A-Za-z0-9]{30,}"),
    ("hf_token", "HuggingFace token", "HIGH", r"\bhf_[A-Za-z0-9]{30,}"),
    ("aws_key_id", "AWS access key id", "HIGH", r"\bAKIA[0-9A-Z]{16}\b"),
    ("slack_token", "Slack token", "HIGH", r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    ("bearer_header", "Authorization: Bearer header", "HIGH",
     r"Authorization:\s*Bearer\s+\S+"),
    ("api_key_env", "API-key environment assignment", "HIGH",
     r"\b(?:ANTHROPIC|OPENAI|HF|HUGGINGFACE|GITHUB|GH|GITLAB|AWS_SECRET_ACCESS|"
     r"AZURE|GOOGLE|WANDB|SLACK)[A-Z_]*(?:KEY|TOKEN|SECRET|PASSWORD)\s*=\s*\S+"),
    ("token_flag", "--token / --api-key / --secret with an inline value", "med",
     r"--(?:token|api[-_]?key|secret|access[-_]?key)(?:=|\s+)(?!\$|\"?\$)\S+"),
    # `-p<value>` is only a password in a handful of database clients. Left
    # unscoped it matched `uv -p 3.12`, `mkdir -p`, `ps -p`, `patch -p1` --
    # 57 hits in 1,039 real argv, a ~5% false-positive rate. A credentials
    # panel that cries wolf is worse than no panel, so the shorthand is
    # tool-scoped and the long form carries the general case.
    ("password_flag", "--password with an inline value", "med",
     r"--password(?:=|\s+)(?!\$|\"?\$)\S+"),
    ("db_password_short", "database client -p<password>", "med",
     r"(?:^|/)(?:mysql|mysqldump|mysqladmin|mariadb|psql|mongosh|redis-cli)\b"
     r"[^\n]*(?<!\w)-p(?!\s)\S{4,}"),
    ("curl_userpass", "curl -u user:password", "med",
     r"(?:^|/)curl\b[^\n]*\s-u\s+\S+:\S+"),
    ("private_key_inline", "inline private key material", "HIGH",
     r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
]

_DANGEROUS = [(i, l, a, s, re.compile(r, re.I)) for i, l, a, s, r in DANGEROUS]
_SECRETS = [(i, l, s, re.compile(r)) for i, l, s, r in SECRETS]


class RiskReducer:
    """`login_compute`, `dangerous` and `secrets` in one pass over `exit`."""

    EVENTS = ("exit", "truncated")
    KEY = "risk"

    def __init__(self, cfg, classes):
        self.cfg = cfg
        self.classes = list(classes)
        # login-node compute
        self.lc = collections.defaultdict(
            lambda: {"n": 0, "cpu_s": 0.0, "users": set(), "hosts": set(),
                     "classes": collections.Counter(), "kind": None})
        # heaviest individual processes, across all compute tools
        self.lc_procs = []
        self.lc_by_class = collections.defaultdict(
            lambda: {"n": 0, "cpu_s": 0.0, "users": set()})
        self.total_cpu = 0.0
        self.total_events = 0
        # dangerous commands
        self.dg = collections.defaultdict(
            lambda: {"n": 0, "ok": 0, "rated": 0, "cpu_s": 0.0,
                     "users": set(), "hosts": set(),
                     "classes": collections.Counter(), "inst": []})
        # secrets in argv
        self.sec = collections.defaultdict(
            lambda: {"n": 0, "users": set(), "tools": collections.Counter(),
                     "classes": collections.Counter(), "inst": []})
        self.args_seen = 0
        self.args_truncated = 0

    def feed(self, rec):
        cls = rec["_a3"]
        user = rec.get("user")
        host = rec.get("host")
        cpu = rec.get("cpu_s") or 0.0
        ec = rec.get("_eff") or rec.get("comm") or ""
        args = rec.get("args") or ""
        self.total_events += 1
        self.total_cpu += cpu
        if args:
            self.args_seen += 1
            if rec.get("args_truncated"):
                self.args_truncated += 1

        # ---- login-node compute -------------------------------------------
        b = bucket(ec)
        if b in COMPUTE_BUCKETS or b in TRANSFER_BUCKETS:
            e = self.lc[ec]
            e["n"] += 1
            e["cpu_s"] += cpu
            e["kind"] = "compute" if b in COMPUTE_BUCKETS else "transfer"
            if user:
                e["users"].add(user)
            if host:
                e["hosts"].add(host)
            e["classes"][cls] += 1
            if b in COMPUTE_BUCKETS:
                # keep only the heaviest; a bounded list, not every process
                self.lc_procs.append(who(rec))
                if len(self.lc_procs) > 400:
                    self.lc_procs.sort(key=lambda w: -(w.get("cpu_s") or 0))
                    del self.lc_procs[200:]
                c = self.lc_by_class[cls]
                c["n"] += 1
                c["cpu_s"] += cpu
                if user:
                    c["users"].add(user)

        # ---- dangerous commands -------------------------------------------
        hay = "%s %s" % (ec, args)
        for rid, _lbl, _axis, _sev, rx in _DANGEROUS:
            if rx.search(hay):
                d = self.dg[rid]
                d["n"] += 1
                d["cpu_s"] += cpu
                if user:
                    d["users"].add(user)
                if host:
                    d["hosts"].add(host)
                d["classes"][cls] += 1
                if len(d["inst"]) < INSTANCE_CAP:
                    d["inst"].append(who(rec))
                # `succeeded` only counts records that actually carry a verdict
                sig, code = rec.get("signal"), rec.get("exit_code")
                if sig is None and code is not None:
                    d["rated"] += 1
                    if code == 0:
                        d["ok"] += 1

        # ---- secrets in argv ----------------------------------------------
        # The matched text is deliberately never bound to a name that can reach
        # the payload: `rx.search()`'s result is tested and discarded. Emitting
        # the value -- or the argv around it -- would make this dashboard the
        # leak, on a shared machine, for other people's credentials.
        if args:
            for sid, _lbl, _sev, rx in _SECRETS:
                if rx.search(args) is not None:
                    v = self.sec[sid]
                    v["n"] += 1
                    if user:
                        v["users"].add(user)
                    v["tools"][ec or "(empty)"] += 1
                    v["classes"][cls] += 1
                    # identity WITHOUT argv: the pattern fired, so the argv is
                    # exactly what must not be echoed back
                    if len(v["inst"]) < INSTANCE_CAP:
                        v["inst"].append(who(rec, with_args=False))

    # ---------------------------------------------------------------- result
    def result(self):
        lc_rows = []
        for tool, e in sorted(self.lc.items(), key=lambda kv: -kv[1]["cpu_s"]):
            lc_rows.append({
                "tool": tool, "kind": e["kind"], "n": e["n"],
                "cpu_s": round(e["cpu_s"], 2),
                "cpu_h": round(e["cpu_s"] / 3600.0, 4),
                "cpu_pct": pct(e["cpu_s"], self.total_cpu),
                "users": len(e["users"]), "hosts": sorted(e["hosts"]),
                "classes": dict(e["classes"]),
            })
        compute_cpu = sum(r["cpu_s"] for r in lc_rows if r["kind"] == "compute")
        self.lc_procs.sort(key=lambda w: -(w.get("cpu_s") or 0))
        lc_top = self.lc_procs[:INSTANCE_CAP * 2]

        dg_rows = []
        for rid, lbl, axis, sev, _rx in _DANGEROUS:
            d = self.dg.get(rid)
            if not d:
                continue
            dg_rows.append({
                "id": rid, "command": lbl, "axis": axis, "severity": sev,
                "n": d["n"], "users": len(d["users"]), "hosts": sorted(d["hosts"]),
                "instances": d["inst"],
                "cpu_s": round(d["cpu_s"], 2),
                "rated": d["rated"], "succeeded": d["ok"],
                "works_pct": (pct(d["ok"], d["rated"]) if d["rated"] else None),
                "classes": dict(d["classes"]),
            })
        sev_rank = {"HIGH": 0, "med": 1, "low": 2}
        dg_rows.sort(key=lambda r: (sev_rank.get(r["severity"], 3), -r["users"], -r["n"]))

        sec_rows = []
        for sid, lbl, sev, _rx in _SECRETS:
            v = self.sec.get(sid)
            if not v:
                continue
            sec_rows.append({
                "id": sid, "pattern": lbl, "severity": sev, "n": v["n"],
                "users": len(v["users"]),
                "tools": [t for t, _ in v["tools"].most_common(6)],
                "classes": dict(v["classes"]),
                "instances": v["inst"],
            })
        sec_rows.sort(key=lambda r: (sev_rank.get(r["severity"], 3), -r["n"]))

        return {
            "login_compute": {
                "rows": lc_rows[:25],
                "top_procs": lc_top,
                "compute_cpu_s": round(compute_cpu, 2),
                "compute_cpu_h": round(compute_cpu / 3600.0, 4),
                "share_of_all_cpu_pct": pct(compute_cpu, self.total_cpu),
                "by_class": {c: {"n": v["n"], "cpu_s": round(v["cpu_s"], 2),
                                 "cpu_h": round(v["cpu_s"] / 3600.0, 4),
                                 "users": len(v["users"])}
                             for c, v in self.lc_by_class.items()},
                "basis": "The eBPF tier runs on login nodes only, so a build or an "
                         "interpreter here IS compute on a login node -- the host's "
                         "role is not inferred. `transfer` rows (wget/curl/rsync) are "
                         "bulk movement, a separate policy question, and are excluded "
                         "from compute_cpu_h. `top_procs` names the heaviest "
                         "individual processes -- pid, user, actor and argv -- "
                         "because 'which tool' does not tell you who to talk to.",
            },
            "dangerous": {
                "rows": dg_rows,
                "axes": {
                    "environment-unaware": "it runs, and tells you nothing useful here",
                    "policy-unaware": "it works, and it is what the facility asks you not to do",
                },
                "site_assumption": "The `environment-unaware` rows assume a GPU-less "
                                   "login node, which is true of the FASRC fleet but not "
                                   "of a GPU dev box -- on one of those, nvidia-smi is a "
                                   "reasonable command and the row should be read as a "
                                   "count, not a criticism.",
                "basis": "works_pct is over records carrying an exit verdict (`rated`): "
                         "signal-terminated records have no exit_code and are excluded "
                         "rather than counted as failures. A high works_pct is the "
                         "finding -- it means nothing stopped the command.",
            },
            "secrets": {
                "rows": sec_rows,
                "args_scanned": self.args_seen,
                "args_truncated": self.args_truncated,
                "truncation_pct": pct(self.args_truncated, self.args_seen),
                "redaction": "No matched value, and no surrounding argv, is ever "
                             "emitted -- only the pattern that fired, the counts, and "
                             "which tools. On a shared node the alternative would make "
                             "this dashboard the leak. `instances` carries pid, "
                             "user, actor and tool so the exposure is actionable, "
                             "and deliberately NO argv.",
                "undercount": "argv is captured up to EBPFM_ARGS_MAXLEN (2048 by "
                              "default), so a credential beyond the cap is invisible. "
                              "args_truncated is the share of records where that is "
                              "possible, i.e. the floor on what could be missed.",
            },
            "totals": {"events": self.total_events,
                       "cpu_s": round(self.total_cpu, 2)},
        }
