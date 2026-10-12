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
import re

from . import INSTANCE_CAP, add_counts, pct, who

from ..tool_buckets import BUCKETS, bucket

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
    # The compute nodes' names are the cluster's own: `risk.vars.compute_hosts`.
    ("ssh_compute", "ssh straight into a compute node", "policy-unaware", "med",
     r"(?:^|/)ssh\b[^\n]*\b(?:{{compute_hosts}})[\w.-]*"),
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
    # Not psql or redis-cli: for both, `-p` is the PORT (`psql -p5432`), and
    # redis-cli takes its password as `-a`.
    ("db_password_short", "database client -p<password>", "med",
     r"(?:^|/)(?:mysql|mysqldump|mysqladmin|mariadb|mongosh)\b"
     r"[^\n]*(?<!\w)-p(?!\s)\S{4,}"),
    ("redis_password", "redis-cli -a <password>", "med",
     r"(?:^|/)redis-cli\b[^\n]*\s-a\s*(?!\$)\S+"),
    ("curl_userpass", "curl -u user:password", "med",
     r"(?:^|/)curl\b[^\n]*\s-u\s+\S+:\S+"),
    ("private_key_inline", "inline private key material", "HIGH",
     r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
]

# `{{name}}` in a pattern is a cluster variable (`risk.vars`); these are the
# defaults, written for the FASRC fleet.
DEFAULT_VARS = {"compute_hosts": r"holy|bos|node|gpu|c\d{3,}"}
_VAR = re.compile(r"\{\{(\w+)\}\}")


def subst(pattern, vars_):
    """`pattern` with each `{{name}}` replaced; KeyError names an unset one."""
    return _VAR.sub(lambda m: vars_[m.group(1)], pattern)


_DANGEROUS = [(i, l, a, s, re.compile(subst(r, DEFAULT_VARS), re.I)) for i, l, a, s, r in DANGEROUS]
_SECRETS = [(i, l, s, re.compile(r)) for i, l, s, r in SECRETS]

# A literal every match of a pattern must contain, so a command line with none
# of them can skip that pattern's regex. Most command lines trip nothing, and
# the regexes were the single largest cost of a window build. A gate only ever
# lets MORE lines through to the regex: it can cost a search, never a match --
# tests/test_risk_cache.py holds every gate to its regex over a corpus.
# Dangerous patterns are case-insensitive, so their gates test a casefolded
# line (casefold, unlike lower, folds the Unicode letters `re.I` treats as
# ASCII ones). Secret patterns are case-sensitive and test the line as is.
DANGEROUS_GATES = {
    "nvidia_smi_login": ("nvidia-smi",),
    "nvcc_login": ("nvcc",),
    "pip_install": ("pip",),
    "conda_install": ("conda", "mamba"),
    "build_login": ("make", "ninja", "gcc", "g++", "clang", "nvcc", "cargo", "go"),
    "ssh_compute": ("ssh",),
    "server_on_login": ("http.server", "tensorboard", "streamlit", "jupyter"),
    "bulk_download": ("wget", "curl"),
    "hf_download": ("download",),
    "headless_browser": ("--headless", "playwright", "puppeteer"),
    "git_force_push": ("push",),
    "rm_rf_shared": ("rm",),
    "chmod_777": ("chmod",),
}
SECRET_GATES = {
    "openai_key": ("sk-",),
    "github_pat": ("ghp_", "gho_", "ghu_", "ghs_", "ghr_"),
    "hf_token": ("hf_",),
    "aws_key_id": ("AKIA",),
    "slack_token": ("xox",),
    "bearer_header": ("Authorization:",),
    "api_key_env": ("KEY", "TOKEN", "SECRET", "PASSWORD"),
    "token_flag": ("--token", "--api", "--secret", "--access"),
    "password_flag": ("--password",),
    "db_password_short": ("mysql", "mariadb", "mongosh"),
    "redis_password": ("redis-cli",),
    "curl_userpass": ("curl",),
    "private_key_inline": ("PRIVATE KEY",),
}
# aligned with _DANGEROUS / _SECRETS; a pattern with no gate always runs
_DG_GATE = [DANGEROUS_GATES.get(d[0]) for d in _DANGEROUS]
_SEC_GATE = [SECRET_GATES.get(sc[0]) for sc in _SECRETS]


def _passes(gate, hay):
    return gate is None or any(t in hay for t in gate)


# ------------------------------------------------------------- per cluster
#
# What counts as risky differs by cluster: nvidia-smi is a pointless question
# on a GPU-less login node and a sensible one on a GPU dev box, compute nodes
# are named differently everywhere, and a site has its own tools and token
# formats. The catalogue above is the default; the `risk` config section
# changes it, and `risk.profile` names a JSON file of the same shape that is
# merged over it at load (`config.py`), so a cluster's rules travel as one file
# (dashboard/risk-profiles/). The resolved section is part of every saved
# window's fingerprint, so a changed rule never resumes counts made by the old.
#
#   vars        {"compute_hosts": "holy|bos|..."}: `{{name}}` in any pattern
#   dangerous / secrets:
#     disable   [id, ...]: switched off
#     override  {id: {label, severity, axis, pattern, gate}}: any field changed.
#               A new pattern drops the built-in gate unless one is given: a
#               gate that no longer fits would hide matches
#     add       [{id, label, severity, axis, pattern, gate}]: new rules. `gate`
#               is optional -- literals every match contains, to skip the regex
#               cheaply; leave it out if unsure
#   login_compute  {"compute": [...], "transfer": [...]}: tool buckets
#
# Dangerous patterns are case-insensitive, secret patterns are not. A rule that
# does not compile, or names an unset variable, is left out and reported in the
# panel's `rules`, never fatal. Every secret pattern a cluster adds or changes is
# also masked on the way out (`redact.extend`): a credential the panel counts
# must never be shown by the rest of the page.
SEVERITIES = ("HIGH", "med", "low")
_RULESETS = {}

# The Risk tab is built of SECTIONS, and which sections a cluster has is its
# call too: a GPU box that is also the login node has no use for "compute on a
# login node", and a site may want its data-movement rules apart from the rest.
# `risk.sections` is the ordered list; each is one of these kinds:
#
#   login_compute  CPU in builds and interpreters on the login node
#   commands       dangerous-command rules: a list of ids, or "rest" for every
#                  enabled rule no other commands section lists
#   credentials    credential rules, the same way
#
# A rule no section shows is not counted -- a count nobody can see is a cost
# with no reader -- and is listed as such under Rules in effect; a kind no
# section uses is not computed at all. Masking credentials on the way out does
# not depend on any of this: it is always on.
KINDS = {"login_compute": "Compute on a login node",
         "commands": "Commands that should not run here",
         "credentials": "Credentials exposed in argv"}
DEFAULT_SECTIONS = [
    {"id": "login_compute", "kind": "login_compute"},
    {"id": "commands", "kind": "commands", "rules": "rest"},
    {"id": "credentials", "kind": "credentials", "rules": "rest"},
]


class RuleSet:
    def __init__(self, conf):
        conf = conf or {}
        self.profile = conf.get("profile") or None
        self.vars = dict(DEFAULT_VARS, **(conf.get("vars") or {}))
        self.notes = {"disabled": [], "changed": [], "added": [], "invalid": [], "unknown": [],
                      "unsectioned": []}
        self.dg, self.dg_gate, self.dg_meta = self._build(
            "dangerous", conf.get("dangerous") or {},
            [(i, l, a, s, r) for i, l, a, s, r in DANGEROUS], DANGEROUS_GATES, re.I)
        self.sec, self.sec_gate, self.sec_meta = self._build(
            "secrets", conf.get("secrets") or {},
            [(i, l, None, s, r) for i, l, s, r in SECRETS], SECRET_GATES, 0)
        lc = conf.get("login_compute") or {}
        self.compute = tuple(lc.get("compute") or COMPUTE_BUCKETS)
        self.transfer = tuple(lc.get("transfer") or TRANSFER_BUCKETS)
        # a misspelt bucket would count nothing, silently: name it instead
        for b in sorted(set(self.compute + self.transfer) - set(BUCKETS)):
            self.notes["unknown"].append({"kind": "login_compute", "id": b})
        # what the rest of the page must mask too -- whether or not a section
        # counts the rule: masking is never switched off
        self.masks = [m["pattern_resolved"] for m in self.sec_meta if m["source"] != "built-in"]
        self.sections = self._sections(conf.get("sections"))
        self.login_compute_on = any(x["kind"] == "login_compute" for x in self.sections)
        self.dg, self.dg_gate, self.dg_meta = self._keep(
            "commands", "dangerous", self.dg, self.dg_gate, self.dg_meta)
        self.sec, self.sec_gate, self.sec_meta = self._keep(
            "credentials", "secrets", self.sec, self.sec_gate, self.sec_meta)

    def _sections(self, raw):
        """The ordered sections, each rule placed in exactly one."""
        raw = DEFAULT_SECTIONS if raw is None else raw
        pools = {"commands": [m["id"] for m in self.dg_meta],
                 "credentials": [m["id"] for m in self.sec_meta]}
        self._placed = {"commands": {}, "credentials": {}}
        out, ids, rest = [], set(), {}
        for spec in raw:
            spec = spec or {}
            sid, kind = spec.get("id"), spec.get("kind")
            if kind not in KINDS or not sid or sid in ids:
                self.notes["invalid"].append({
                    "kind": "section", "id": sid,
                    "error": ("kind must be one of %s" % ", ".join(KINDS) if kind not in KINDS
                              else "a section needs a unique id")})
                continue
            ids.add(sid)
            sec = {"id": sid, "kind": kind, "title": spec.get("title") or KINDS[kind], "rules": []}
            if kind != "login_compute":
                want = spec.get("rules", "rest")
                if want == "rest":
                    if kind in rest:
                        self.notes["invalid"].append({"kind": "section", "id": sid, "error":
                                                      "only one %s section can take the rest" % kind})
                        continue
                    rest[kind] = sec
                elif isinstance(want, list):
                    for rid in want:
                        if rid not in pools[kind]:
                            self.notes["unknown"].append({"kind": "section %s" % sid, "id": rid})
                        elif rid in self._placed[kind]:
                            self.notes["invalid"].append({"kind": "section", "id": sid, "error":
                                                          "%s is already in section %s" % (
                                                              rid, self._placed[kind][rid])})
                        else:
                            self._placed[kind][rid] = sid
                            sec["rules"].append(rid)
                else:
                    self.notes["invalid"].append({"kind": "section", "id": sid, "error":
                                                  'rules must be a list of ids or "rest"'})
                    continue
            out.append(sec)
        for kind, sec in rest.items():
            sec["rules"] = [r for r in pools[kind] if r not in self._placed[kind]]
            for r in sec["rules"]:
                self._placed[kind][r] = sec["id"]
        return out

    def _keep(self, kind, rkind, rules, gates, meta):
        """Only the rules a section shows are matched; the rest are noted."""
        placed = self._placed[kind]
        keep = [i for i, m in enumerate(meta) if m["id"] in placed]
        for i, m in enumerate(meta):
            if m["id"] in placed:
                m["section"] = placed[m["id"]]
            else:
                self.notes["unsectioned"].append({"kind": rkind, "id": m["id"], "label": m["label"]})
        return [rules[i] for i in keep], [gates[i] for i in keep], [meta[i] for i in keep]

    def _build(self, kind, conf, builtin, gates, flags):
        disable = set(conf.get("disable") or [])
        override = conf.get("override") or {}
        known = {b[0] for b in builtin}
        for rid in sorted((disable | set(override)) - known):
            self.notes["unknown"].append({"kind": kind, "id": rid})
        specs = []
        for rid, label, axis, sev, pat in builtin:
            if rid in disable:
                self.notes["disabled"].append({"kind": kind, "id": rid, "label": label})
                continue
            o = override.get(rid) or {}
            gate = gates.get(rid)
            # A gate is proven only against the pattern it was written for: if
            # the cluster's variables change the pattern, it runs ungated.
            try:
                if subst(pat, self.vars) != subst(pat, DEFAULT_VARS):
                    gate = None
            except KeyError:
                gate = None
            if "pattern" in o:
                pat, gate = o["pattern"], o.get("gate")
            elif "gate" in o:
                gate = o["gate"]
            spec = {"id": rid, "label": o.get("label", label), "axis": o.get("axis", axis),
                    "severity": o.get("severity", sev), "pattern": pat, "gate": gate,
                    "source": "changed" if o else "built-in"}
            if o:
                self.notes["changed"].append({"kind": kind, "id": rid, "fields": sorted(o)})
            specs.append(spec)
        for a in conf.get("add") or []:
            rid = (a or {}).get("id")
            if not rid or not a.get("pattern") or rid in known:
                self.notes["invalid"].append({"kind": kind, "id": rid, "error":
                                              "an added rule needs a new id and a pattern"})
                continue
            known.add(rid)
            specs.append({"id": rid, "label": a.get("label", rid),
                          "axis": a.get("axis", "policy-unaware" if kind == "dangerous" else None),
                          "severity": a.get("severity", "med"), "pattern": a["pattern"],
                          "gate": a.get("gate"), "source": "added"})
            self.notes["added"].append({"kind": kind, "id": rid, "label": a.get("label", rid)})
        rules, gate_list, meta = [], [], []
        for sp in specs:
            try:
                resolved = subst(sp["pattern"], self.vars)
                rx = re.compile(resolved, flags)
            except KeyError as e:
                self.notes["invalid"].append({"kind": kind, "id": sp["id"],
                                              "error": "unset variable %s" % e})
                continue
            except re.error as e:
                self.notes["invalid"].append({"kind": kind, "id": sp["id"], "error": str(e)})
                continue
            if sp["severity"] not in SEVERITIES:
                sp["severity"] = "med"
            g = sp["gate"]
            if g is not None:
                g = tuple(x.casefold() if flags & re.I else x for x in g)
            if kind == "dangerous":
                rules.append((sp["id"], sp["label"], sp["axis"], sp["severity"], rx))
            else:
                rules.append((sp["id"], sp["label"], sp["severity"], rx))
            gate_list.append(g)
            meta.append(dict(sp, pattern_resolved=resolved, gate=list(g) if g else None))
        return rules, gate_list, meta

    def summary(self):
        """What the panel says about the rules it counted with."""
        def rows(meta):
            return [{k: m.get(k) for k in ("id", "label", "severity", "axis", "pattern_resolved",
                                           "source", "section")} for m in meta]
        return {"profile": self.profile, "vars": self.vars, "sections": self.sections,
                "dangerous": rows(self.dg_meta), "secrets": rows(self.sec_meta),
                "login_compute": {"compute": list(self.compute), "transfer": list(self.transfer)},
                **self.notes}


def ruleset(cfg):
    """The rule set for this config, built once per distinct `risk` section.

    Building it also registers the masks for any secret pattern the cluster
    added or changed, so call it before anything is served (the aggregator does).
    """
    import json
    conf = cfg.get("risk") or {}
    key = json.dumps(conf, sort_keys=True, default=str)
    rs = _RULESETS.get(key)
    if rs is None:
        rs = _RULESETS[key] = RuleSet(conf)
        if rs.masks:
            from .. import redact
            redact.extend(rs.masks)
    return rs


# Matching a command line against every pattern above was HALF of a window
# build -- 2.5M regex searches for a 6 h window. But a login node repeats
# itself: 90% of command lines are ones already seen (one poll loop ran the same
# `ps` 25,611 times in an afternoon), so each distinct line is matched once and
# the verdict remembered. Bounded, and dropped whole when full: a cache miss is
# only slower, never different.
MATCH_CACHE_MAX = 50000



def _by_type(c):
    """{(class, agent type): n} as {class: {agent type: n}}, "" for untagged:
    the agent-type breakdown behind each row's per-class counts."""
    out = {}
    for (cls, at), n in c.items():
        out.setdefault(cls, {})[at] = n
    return out

class RiskReducer:
    """`login_compute`, `dangerous` and `secrets` in one pass over `exit`."""

    EVENTS = ("exit", "truncated")
    KEY = "risk"

    # the rule set is the config's, rebuilt by every fresh instance -- never saved
    SNAPSHOT_SKIP = ("_match", "rules")

    def __init__(self, cfg, classes):
        self.cfg = cfg
        self.classes = list(classes)
        self.rules = ruleset(cfg)
        self._match = {}       # (tool, argv) -> (dangerous idx, secret idx); a cache
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
                     "classes": collections.Counter(), "types": collections.Counter(),
                     "inst": []})
        # secrets in argv
        self.sec = collections.defaultdict(
            lambda: {"n": 0, "users": set(), "tools": collections.Counter(),
                     "classes": collections.Counter(), "types": collections.Counter(),
                     "inst": []})
        self.args_seen = 0
        self.args_truncated = 0

    def feed(self, rec):
        cls = rec["_a3"]
        user = rec.get("user")
        host = rec.get("host")
        cpu = rec.get("cpu_s") or 0.0
        ec = rec.get("_eff") or rec.get("_command") or ""
        args = rec.get("args") or ""
        self.total_events += 1
        self.total_cpu += cpu
        if args:
            self.args_seen += 1
            if rec.get("args_truncated"):
                self.args_truncated += 1

        # ---- login-node compute -------------------------------------------
        b = bucket(ec)
        rs = self.rules
        if rs.login_compute_on and (b in rs.compute or b in rs.transfer):
            e = self.lc[ec]
            e["n"] += 1
            e["cpu_s"] += cpu
            e["kind"] = "compute" if b in rs.compute else "transfer"
            if user:
                e["users"].add(user)
            if host:
                e["hosts"].add(host)
            e["classes"][cls] += 1
            if b in rs.compute:
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
        hit_dg, hit_sec = self._matches(ec, args)
        for i in hit_dg:
            rid = rs.dg[i][0]
            d = self.dg[rid]
            d["n"] += 1
            d["cpu_s"] += cpu
            if user:
                d["users"].add(user)
            if host:
                d["hosts"].add(host)
            d["classes"][cls] += 1
            d["types"][(cls, rec.get("agent_type") or "")] += 1
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
            for i in hit_sec:
                sid = rs.sec[i][0]
                v = self.sec[sid]
                v["n"] += 1
                if user:
                    v["users"].add(user)
                v["tools"][ec or "(empty)"] += 1
                v["classes"][cls] += 1
                v["types"][(cls, rec.get("agent_type") or "")] += 1
                # identity WITHOUT argv: the pattern fired, so the argv is
                # exactly what must not be echoed back
                if len(v["inst"]) < INSTANCE_CAP:
                    v["inst"].append(who(rec, with_args=False))

    def _matches(self, ec, args):
        """Which dangerous and secret patterns a command line trips, by index.

        Only indices are kept: the matched text never leaves `rx.search`."""
        key = (ec, args)
        m = self._match.get(key)
        if m is None:
            hay = "%s %s" % (ec, args)
            low = hay.casefold()
            rs = self.rules
            m = (tuple(i for i, d in enumerate(rs.dg)
                       if _passes(rs.dg_gate[i], low) and d[-1].search(hay)),
                 tuple(i for i, sc in enumerate(rs.sec)
                       if args and _passes(rs.sec_gate[i], args) and sc[-1].search(args)))
            if len(self._match) >= MATCH_CACHE_MAX:
                self._match.clear()
            self._match[key] = m
        return m

    # ----------------------------------------------------------------- merge
    def merge(self, o):
        """Fold a LATER state into this one. `o` is consumed.

        Per-tool and per-pattern counts add and their user and host sets union.
        The instance samples are the FIRST few in time, so this state's come
        before `o`'s; the heaviest processes are re-cut by the rule `feed`
        cuts them by, which keeps the top of the list exactly what one pass
        keeps.
        """
        for tool, e in o.lc.items():
            m = self.lc[tool]
            m["n"] += e["n"]
            m["cpu_s"] += e["cpu_s"]
            m["users"] |= e["users"]
            m["hosts"] |= e["hosts"]
            add_counts(m["classes"], e["classes"])
            m["kind"] = e["kind"]
        self.lc_procs.extend(o.lc_procs)
        if len(self.lc_procs) > 400:
            self.lc_procs.sort(key=lambda w: -(w.get("cpu_s") or 0))
            del self.lc_procs[200:]
        for cls, v in o.lc_by_class.items():
            m = self.lc_by_class[cls]
            m["n"] += v["n"]
            m["cpu_s"] += v["cpu_s"]
            m["users"] |= v["users"]
        self.total_cpu += o.total_cpu
        self.total_events += o.total_events
        for rid, d in o.dg.items():
            m = self.dg[rid]
            for k in ("n", "ok", "rated", "cpu_s"):
                m[k] += d[k]
            m["users"] |= d["users"]
            m["hosts"] |= d["hosts"]
            add_counts(m["classes"], d["classes"])
            add_counts(m["types"], d["types"])
            m["inst"].extend(d["inst"][:max(0, INSTANCE_CAP - len(m["inst"]))])
        for sid, v in o.sec.items():
            m = self.sec[sid]
            m["n"] += v["n"]
            m["users"] |= v["users"]
            for k in ("tools", "classes", "types"):
                add_counts(m[k], v[k])
            m["inst"].extend(v["inst"][:max(0, INSTANCE_CAP - len(m["inst"]))])
        self.args_seen += o.args_seen
        self.args_truncated += o.args_truncated

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
        for rid, lbl, axis, sev, _rx in self.rules.dg:
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
                "types": _by_type(d["types"]),
            })
        sev_rank = {"HIGH": 0, "med": 1, "low": 2}
        dg_rows.sort(key=lambda r: (sev_rank.get(r["severity"], 3), -r["users"], -r["n"]))

        sec_rows = []
        for sid, lbl, sev, _rx in self.rules.sec:
            v = self.sec.get(sid)
            if not v:
                continue
            sec_rows.append({
                "id": sid, "pattern": lbl, "severity": sev, "n": v["n"],
                "users": len(v["users"]),
                "tools": [t for t, _ in v["tools"].most_common(6)],
                "classes": dict(v["classes"]),
                "types": _by_type(v["types"]),
                "instances": v["inst"],
            })
        sec_rows.sort(key=lambda r: (sev_rank.get(r["severity"], 3), -r["n"]))

        out = {
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
                "undercount": "argv is captured up to the collector's in-kernel read "
                              "(EBPFM_ARGV_KMAX, 4096 bytes by default), or less if "
                              "EBPFM_ARGS_MAXLEN caps it, so a credential beyond the "
                              "cut is invisible. args_truncated is the share of "
                              "records where that is possible, i.e. the floor on "
                              "what could be missed.",
            },
            "totals": {"events": self.total_events,
                       "cpu_s": round(self.total_cpu, 2)},
            # the rules these counts were made with: built in, changed, added,
            # switched off or rejected, and the cluster profile they came from
            "rules": self.rules.summary(),
        }
        # The page's sections, in order; a kind no section shows is not
        # computed, so it is absent rather than a list of zeros.
        kinds = {x["kind"] for x in self.rules.sections}
        for kind, key in (("login_compute", "login_compute"), ("commands", "dangerous"),
                          ("credentials", "secrets")):
            if kind not in kinds:
                out.pop(key)
        out["sections"] = self.rules.sections
        return out
