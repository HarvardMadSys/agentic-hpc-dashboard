"""Sandbox and approval: two independent axes, never one flag.

`sandbox` (is it confined?) and `approval_mode` (is it unattended?) are
orthogonal, and all four combinations occur.  The retired single `autonomous`
boolean conflated them: --dangerously-skip-permissions bypasses APPROVAL but
leaves the sandbox intact, while --dangerously-bypass-approvals-and-sandbox
disables BOTH.

`sandbox` has FOUR states, not three.  `denied` -- the collector could not read
/proc/<pid>/ns/*, which is ptrace-gated for other users when it runs
unprivileged -- must never be folded into `unsandboxed`: doing so would deflate
the sandbox rate for every user except the collector's owner, a systematic bias
that reads like a finding.

The second table is the one the collector's own author added and it is the more
interesting of the two.  `sandbox_ancestry` says how a process came to be
sandboxed; `sandbox` says whether it actually is.  Their DISAGREEMENT is the
signal: Claude Code's bwrap use is a capability probe (`bwrap --ro-bind / /`),
and per findings/login.md that probe is what wedges in D-state on a dead
automount.  ancestry=bwrap with sandbox=unsandboxed is the probe; ancestry=bwrap
with sandbox=sandboxed is real confinement.
"""
import collections

from . import Reducer, pct


class SandboxReducer:
    EVENTS = ("exit", "truncated")
    KEY = "sandbox"

    def __init__(self, cfg, classes):
        self.cfg = cfg
        self.classes = list(classes)
        self.sb = collections.defaultdict(collections.Counter)
        self.ap = collections.defaultdict(collections.Counter)
        self.matrix = collections.Counter()       # (ancestry, sandbox) -> n
        self.matrix_dwait = collections.Counter()  # same key -> summed dstate_wait_s
        self.matrix_dseen = collections.Counter()
        self.src = collections.Counter()
        self.detail = collections.Counter()
        self.agents_only = collections.defaultdict(collections.Counter)
        self.trees = {}                            # session -> (sandbox, approval, cls)

    def feed(self, rec):
        cls = rec["_a3"]
        sb, ap = rec.get("_sandbox", "unknown"), rec.get("_approval", "unknown")
        self.sb[cls][sb] += 1
        self.ap[cls][ap] += 1
        if rec.get("_sandbox_src"):
            self.src[rec["_sandbox_src"]] += 1
        if rec.get("sandbox_detail"):
            for tok in str(rec["sandbox_detail"]).split(","):
                self.detail[tok] += 1
        anc = rec.get("_sandbox_ancestry") or "none"
        self.matrix[(anc, sb)] += 1
        dw = rec.get("dstate_wait_s")
        if dw is not None:
            self.matrix_dwait[(anc, sb)] += dw
            self.matrix_dseen[(anc, sb)] += 1
        if rec.get("is_agent"):
            self.agents_only[cls][(sb, ap)] += 1
        # tree-level: the panel counts sessions, matching how the flag is scoped
        sid = rec.get("_sid")
        if sid and rec.get("agent_pid") is not None:
            prev = self.trees.get(sid)
            if prev is None or (prev[0] == "unknown" and sb != "unknown"):
                self.trees[sid] = (sb, ap, cls)

    def result(self):
        tree_sb = collections.defaultdict(collections.Counter)
        tree_ap = collections.defaultdict(collections.Counter)
        for sb, ap, cls in self.trees.values():
            tree_sb[cls][sb] += 1
            tree_ap[cls][ap] += 1
        rows = []
        for (anc, sb), n in sorted(self.matrix.items(), key=lambda kv: -kv[1]):
            seen = self.matrix_dseen.get((anc, sb), 0)
            rows.append({
                "ancestry": anc, "sandbox": sb, "n": n,
                "dstate_wait_s": round(self.matrix_dwait.get((anc, sb), 0.0), 2),
                "dstate_coverage_pct": pct(seen, n),
                "reading": ("capability probe" if anc == "bwrap" and sb == "unsandboxed"
                            else "real confinement" if anc == "bwrap" and sb == "sandboxed"
                            else None),
            })
        # A flat {state: count} map per class. Shares are the consumer's to
        # derive: shipping counts AND a precomputed pct invites the two to
        # disagree after a filter is applied.
        def _share(d):
            return {cls: dict(c) for cls, c in d.items()}

        def _totals(d):
            return {cls: sum(c.values()) for cls, c in d.items()}
        return {
            "by_class_events": _share(self.sb),
            "approval_by_class_events": _share(self.ap),
            "by_class_trees": _share(tree_sb),
            "approval_by_class_trees": _share(tree_ap),
            "n_events": _totals(self.sb),
            "n_trees": _totals(tree_sb),
            "ancestry_matrix": rows,
            "sandbox_src": dict(self.src),
            "sandbox_detail": dict(self.detail),
            "agent_only": {c: [{"sandbox": k[0], "approval": k[1], "n": v}
                               for k, v in sorted(m.items(), key=lambda kv: -kv[1])]
                           for c, m in self.agents_only.items()},
            "states": {"sandbox": ["sandboxed", "unsandboxed", "denied", "unknown"],
                       "approval": ["supervised", "bypassed", "unknown"]},
            "notes": {
                "denied": "sandbox='denied' means /proc/<pid>/ns/* could not be read "
                          "(ptrace-gated for other users when the collector is "
                          "unprivileged). It is NOT 'unsandboxed'.",
                "meaning": "sandbox = namespace isolation. Seccomp and capabilities are "
                           "evidence in sandbox_detail, never the verdict: every Electron "
                           "renderer is seccomp-filtered, and an unprivileged process has "
                           "CapEff=0 by definition.",
                "ancestry": "A sandboxed verdict with ancestry=none may be a hardened "
                            "systemd unit (PrivateTmp=/ProtectSystem=), not an agent "
                            "sandbox. Read sandbox_src and ancestry together.",
            },
        }
