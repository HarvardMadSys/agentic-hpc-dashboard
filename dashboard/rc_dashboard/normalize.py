"""Turn a raw collector record into the shape the reducers consume.

The contract is SCHEMA_VERSION 5.  A thin shim keeps the pre-v4 capture usable as
an offline test input, but the shim's job is to make older records *honest*, not
to pretend they are v5: a field the old collector could not measure must arrive
as None, never as 0.

Three rules this module exists to enforce:

1. `actor3` is derived, never approximated.  `r.get("actor3") or r.get("actor")`
   yields the BINARY actor on an older record, which silently empties the
   `human-vscode` class -- a class-accounting bug in a three-class dashboard. It
   also swallows v5's deliberate explicit `actor3: null` on an untracked-pid
   `tcp` record.  Unlabelled becomes its own class, never `human`.

2. Null is not zero.  `null_if_off` (ebpf_trace.py:1075) emits None for every
   field whose BPF block was dropped, but the pre-v4 collector wrote 0 in some of
   those slots.  A 0 there means "not measured", so it is gated back to None and
   counted, rather than averaged in as a real zero.

3. `sandbox` and `approval_mode` are separate axes.  Schema 5 carries both; for
   an older record they are simply unknown.  `sandbox` in particular has FOUR
   states, because "could not read" (ptrace-gated for other users when the
   collector is unprivileged) must not be reported as "unsandboxed" -- that would
   deflate the sandbox rate for every user except the collector's owner, a
   systematic bias that reads like a finding.
"""
import os
import re
import sys
from datetime import datetime

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SESSION_SCOPE_RE = re.compile(r"session-([0-9]+)\.scope")
TS_FORMAT = "%Y-%m-%d %H:%M:%S"


def epoch_of(rec):
    """Epoch seconds for a record, or None.  Never guesses."""
    e = rec.get("ts_epoch")
    if isinstance(e, (int, float)):
        return float(e)
    ts = rec.get("ts") or rec.get("timestamp")
    if not ts:
        return None
    try:
        return datetime.strptime(str(ts)[:19], TS_FORMAT).timestamp()
    except ValueError:
        return None

# Fields the v4+ collector emits as null-when-absent but pre-v4 wrote as 0.
# On a pre-v4 record these are forced to None and counted in excluded_pre_v4.
NULLABLE_V4_ONLY = (
    "threads", "cmaj_flt", "child_cpu_s", "sched_wait_s", "blkio_wait_s",
    "swapin_wait_s", "freepages_wait_s", "dstate_wait_s", "dstate_episodes",
    "dstate_max_s", "net_tx_bytes", "net_rx_bytes", "net_calls",
    "min_flt", "nvcsw", "nivcsw", "pgrp", "sid",
)
# Never let a reducer see these: permanent null placeholders in v4+.
ALWAYS_NULL = ("samples", "state_last")

# The collector observing its own instrumentation. node_snapshot.py:1162-1165
# shells out to mpstat/iostat/sar/nfsiostat every tick and queries nvidia-smi,
# and ebpfm.sh drives it -- so without this the dashboard reports its own probes
# as fleet activity, and the Risk panel accuses the node tier of running
# `nvidia-smi` on a login node. The repo already excludes the monitoring
# operator globally (`drop_global`) for exactly this reason; this is the same
# rule applied to the instrument rather than its owner.
#
# Every drop is COUNTED and reported, never silent: an exclusion you cannot see
# is indistinguishable from data that was never there.
# Matched against `args` only -- not comm+args, which doubles the token and
# breaks a leading anchor.
#
# These are the collector's EXACT invocations, copied from
# eBPF_marthen_new/node_snapshot.py:1162-1176. Matching the precise flag sets
# rather than a loose `nvidia-smi --query-` matters: a user legitimately running
# `nvidia-smi --query-gpu=name` must still be counted, and only the snapshot's
# own verbatim calls excluded. Nobody types these by hand.
SELF_OBSERVATION = re.compile(
    r"node_snapshot\.py"
    r"|ebpfm\.sh\b"
    r"|nvidia-smi\s+--query-gpu=index,name,temperature\.gpu"
    r"|nvidia-smi\s+--query-compute-apps=gpu_uuid,pid,process_name"
    r"|nvidia-smi\s+pmon\s+-c\s+1\s*$"
    r"|\bmpstat\s+-P\s+ALL\s+1\s+1\s*$"
    r"|\biostat\s+-xz\s+1\s+2\s*$"
    r"|\bsar\s+-n\s+DEV\s+1\s+2\s*$"
    r"|\bnfsiostat\s+5\s+2\s*$"
    r"|\bvmstat\s+1\s+1\s*$"
)

SANDBOX_STATES = ("sandboxed", "unsandboxed", "denied", "unknown")
APPROVAL_STATES = ("supervised", "bypassed", "unknown")


# ------------------------------------------------------------ optional repo libs
def _load_helpers():
    """agent_lib / a purpose taxonomy, each optional with a working fallback.

    `purpose_lib` is the declared source of truth but imports matplotlib at module
    level, and it exports `classify_comm` -- NOT `purpose_of`, which is why the
    retired builder's probe for `purpose_of` never matched it and the taxonomy
    silently fell through to deep_traj_lib's 12 purposes (no `guard`, no `shell`).
    Probe the real name first.
    """
    sys.path.insert(0, os.path.join(REPO, "analyze"))
    eff = actor3 = autonomous = None
    shell_comms = frozenset()
    try:
        from agent_lib import (effective_comm as eff, actor3 as actor3,   # noqa: F811
                               is_autonomous as autonomous, SHELL_COMMS as shell_comms)
    except Exception:
        pass
    purpose_of, purpose_src, purposes = None, "builtin", None
    for mod, attr in (("purpose_lib", "classify_comm"), ("deep_traj_lib", "purpose_of")):
        try:
            m = __import__(mod)
            fn = getattr(m, attr, None)
            if fn:
                purpose_of, purpose_src = fn, mod
                purposes = getattr(m, "PURPOSES", None)
                break
        except Exception:
            continue
    if eff is None:
        eff = lambda comm, args: comm                                    # noqa: E731
    if actor3 is None:
        actor3 = _fallback_actor3
    if autonomous is None:
        autonomous = lambda a: bool(a and _AUTONOMOUS.search(a))         # noqa: E731
    if purpose_of is None:
        purpose_of = lambda c: _BUILTIN_PURPOSE.get(c or "", "other")    # noqa: E731
    return eff, actor3, autonomous, shell_comms, purpose_of, purpose_src, purposes


_REAL_AGENTS = frozenset({"claude_code", "codex", "cursor", "copilot", "windsurf"})


def _fallback_actor3(actor, agent_type):
    """Mirror of agent_lib.actor3 -- must NOT degrade to the binary `actor`."""
    if actor == "agent":
        if agent_type in _REAL_AGENTS or agent_type == "bwrap":
            return "agent"
        if agent_type == "vscode":
            return "human-vscode"
        return "human"
    return actor


_AUTONOMOUS = re.compile(
    r"--dangerously-skip-permissions|--dangerously-bypass-approvals-and-sandbox"
    r"|danger-full-access|--ask-for-approval\s+never|--yolo|--sandbox\s+danger", re.I)

# Approval and sandbox are different questions; the old single regex mixed them.
_APPROVAL_ONLY = re.compile(
    r"--dangerously-skip-permissions|--ask-for-approval\s+never|--yolo"
    r"|--permission-mode\s+bypassPermissions", re.I)
_BYPASS_BOTH = re.compile(
    r"--dangerously-bypass-approvals-and-sandbox|--sandbox\s+danger-full-access"
    r"|--sandbox\s+danger|danger-full-access", re.I)
_SANDBOX_ON = re.compile(
    r"codex-linux-sandbox|CURSOR_SANDBOX|--sandbox\s+(?:read-only|workspace-write)", re.I)

_BUILTIN_PURPOSE = {}
for _p, _cs in {
    "poll": "ps pgrep pidof top free uptime nvidia-smi date hostname id whoami printenv nproc w who tput",
    "slurm_mon": "squeue sacct sstat sinfo scontrol sprio watch",
    "slurm_sub": "sbatch salloc srun scancel",
    "build": "make cmake ninja gcc g++ cc1plus nvcc ld cargo",
    "git": "git git-remote-https gitstatusd gh",
    "data": "rsync wget curl tar gzip cp mv scp zstd",
    "compute": "python python3 R Rscript julia matlab jupyter",
    "runtime": "node claude codex cursor bwrap electron esbuild",
    "editor": "vim nvim emacs nano code",
    "filesearch": ("grep rg find ls cat sed awk wc sort jq base64 head tail stat "
                   "mkdir rm xargs cut tr getopt getconf infocmp lsb_release ssh uname"),
}.items():
    for _c in _cs.split():
        _BUILTIN_PURPOSE[_c] = _p


def schema_of(rec):
    """Declared schema version; absent means the pre-v4 tracer or the poller."""
    sv = rec.get("schema_version")
    if isinstance(sv, int):
        return sv
    return 3


def session_key_of(rec):
    """(key_type, key_value), exactly as analyze/extract_trajectories.py:278-294.

    First key that applies wins.  `usr` is the last resort and is deliberately
    NOT a trajectory -- it means "everything that user did today" -- so the
    trajectory reducer counts it but never ranks it.
    """
    sk = rec.get("session_key")
    if sk:
        return "apid", sk.split(":", 1)[-1]
    ap = rec.get("agent_pid")
    if ap is not None:
        return "apid", str(ap)
    m = SESSION_SCOPE_RE.search(rec.get("cgroup") or "")
    if m:
        return "sess", m.group(1)
    tty = rec.get("tty")
    if tty and tty not in ("?", "-"):
        return "tty", "%s@%s" % (rec.get("user") or "?", tty)
    return "usr", rec.get("user") or "?"


def sandbox_of(rec, sv):
    """(state, source).  Four states -- `denied` is NOT `unsandboxed`.

    Schema 5 decides this in the collector by comparing /proc/<pid>/ns/* against
    pid 1's.  Seccomp and capabilities are deliberately NOT part of the verdict
    (they live in sandbox_detail): every Electron renderer is seccomp-filtered
    and `human-vscode` is a first-class class here, and an unprivileged process
    has CapEff=0 by definition.  For an older record we simply do not know.
    """
    if sv >= 5 and "sandbox" in rec:
        v = rec.get("sandbox")
        src = rec.get("sandbox_src")
        if v in ("sandboxed", "unsandboxed"):
            return v, src
        return ("denied" if src == "denied" else "unknown"), src
    args = rec.get("args") or ""
    if _BYPASS_BOTH.search(args):
        return "unsandboxed", "argv"
    if _SANDBOX_ON.search(args):
        return "sandboxed", "argv"
    return "unknown", None


def approval_of(rec, sv, is_autonomous):
    """(state, source).  Independent of sandbox: an agent can be confined AND
    unattended, which is the common Codex configuration."""
    if sv >= 5 and rec.get("approval_mode"):
        return rec["approval_mode"], rec.get("approval_src") or "argv"
    args = rec.get("args") or ""
    if _BYPASS_BOTH.search(args) or _APPROVAL_ONLY.search(args):
        return "bypassed", "argv"
    if rec.get("is_agent"):
        return "supervised", "argv"
    return "unknown", None


class Normalizer:
    """Stateful because the exclusion filters and the counters are per-run."""

    def __init__(self, cfg):
        (self.effective_comm, self.actor3, self.is_autonomous, self.shell_comms,
         self.purpose_of, self.purpose_src, self.purposes) = _load_helpers()
        f = cfg.get("filters", {})
        self.drop_global = set(f.get("drop_global") or [])
        self.drop_agent = set(f.get("drop_agent") or [])
        self.unlabeled = f.get("unlabeled_class", "unlabeled")
        self.drop_self = f.get("drop_self_observation", True)
        self.stats = {
            "schema_versions": {}, "excluded_pre_v4": {}, "dropped_users": 0,
            "parse_errors": 0, "records": 0, "skew_dropped": 0,
            "dropped_self_observation": 0,
            "purpose_src": self.purpose_src,
        }

    def __call__(self, rec):
        return self.normalize(rec)

    def normalize(self, rec):
        """Returns the record with derived keys added, or None if filtered out.

        Derived keys are additive and prefixed `_`, so the raw record is still
        byte-faithful for the export path.
        """
        sv = schema_of(rec)
        s = self.stats
        s["schema_versions"][sv] = s["schema_versions"].get(sv, 0) + 1
        s["records"] += 1

        user = rec.get("user")
        ev = rec.get("event")

        # class first: it gates the agent-only exclusion
        if "actor3" in rec:
            a3 = rec.get("actor3")           # v4+: authoritative, incl. explicit null
        else:
            a3 = self.actor3(rec.get("actor"), rec.get("agent_type"))
        a3 = a3 or self.unlabeled

        if user and (user in self.drop_global
                     or (a3 == "agent" and user in self.drop_agent)):
            s["dropped_users"] += 1
            return None

        if self.drop_self and ev in ("exit", "truncated"):
            if SELF_OBSERVATION.search(rec.get("args") or ""):
                s["dropped_self_observation"] += 1
                return None

        rec["_sv"] = sv
        rec["_a3"] = a3

        if ev in ("exit", "truncated"):
            self._gate_nulls(rec, sv)
            comm = rec.get("comm") or ""
            args = rec.get("args") or ""
            ec = self.effective_comm(comm, args)
            rec["_eff"] = ec
            rec["_purpose"] = self.purpose_of(ec)
            rec["_shell_resolved"] = bool(comm in self.shell_comms and ec != comm)
            # pre-v4 emitted cpu_pct on a 0-duration process; recompute, never trust
            d = rec.get("duration_s")
            cpu = rec.get("cpu_s")
            rec["_cpu_pct"] = (100.0 * cpu / d) if (d and d >= 0.01 and cpu) else None

        if ev in ("exit", "truncated", "submit", "residency"):
            kt, kv = session_key_of(rec)
            rec["_kt"], rec["_kv"] = kt, kv
            rec["_sid"] = "%s|%s|%s|%s:%s" % (rec.get("host"), (rec.get("ts") or "")[:10],
                                              a3, kt, kv)
            sb, sbsrc = sandbox_of(rec, sv)
            ap, apsrc = approval_of(rec, sv, self.is_autonomous)
            rec["_sandbox"], rec["_sandbox_src"] = sb, sbsrc
            rec["_approval"], rec["_approval_src"] = ap, apsrc
            rec["_sandbox_ancestry"] = rec.get("sandbox_ancestry")

        # Time is resolved HERE, once, so no downstream consumer invents one.
        # `ts_epoch` is schema 5; an older record only has the local-naive `ts`
        # string, and a record with neither must stay None -- falling back to
        # "now" would stamp a week-old capture as live, which is precisely the
        # fabrication this rewrite exists to remove.
        rec["_ts_epoch"] = epoch_of(rec)
        return rec

    def _gate_nulls(self, rec, sv):
        """A pre-v4 0 in a v4-only slot means 'block absent', not 'measured zero'."""
        if sv < 4:
            ex = self.stats["excluded_pre_v4"]
            for k in NULLABLE_V4_ONLY:
                if rec.get(k) is not None:
                    ex[k] = ex.get(k, 0) + 1
                rec[k] = None
        for k in ALWAYS_NULL:
            rec[k] = None
        io = rec.get("io")
        if isinstance(io, dict) and not io.get("scope"):
            # absent pre-v4; assuming "process" would overstate (vs leader accounting)
            io["scope"] = "unknown"
