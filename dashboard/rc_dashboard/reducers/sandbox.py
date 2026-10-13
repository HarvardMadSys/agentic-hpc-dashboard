"""Sandbox and approval: two independent axes, at the grain each one lives at.

`sandbox` (is it confined?) and `approval_mode` (is it unattended?) are
orthogonal, and all four combinations occur.  The retired single `autonomous`
boolean conflated them: --dangerously-skip-permissions bypasses APPROVAL but
leaves the sandbox intact, while --dangerously-bypass-approvals-and-sandbox
disables BOTH.

What `sandbox` measures.  The collector compares /proc/<pid>/ns/* against pid
1's, so the per-event verdict is NAMESPACE ISOLATION and nothing else.  Landlock
has no /proc exposure and seccomp is kept as detail, so a Landlock- or
seccomp-only sandbox reads as not isolated; a container, an apptainer user
namespace or a PrivateTmp= unit reads as isolated without being an agent
sandbox.  `denied` is a fourth state, never folded into `unsandboxed`: the
namespace links were gone before the read (the ebpf tier is root, so this is a
process that exited in a few ms, not ptrace-gating).

Why the session grain is built from TOOL CALLS.  Claude Code and Codex never run
themselves inside their sandbox; they wrap each tool call.  The agent process is
outside by design, so letting the session root's own verdict decide the session
reported a session whose every call was confined as "unsandboxed".  A session's
verdict is therefore the outcome of its tool calls:

  tool call   a sandbox wrapper invocation (bwrap / codex-linux-sandbox, the
              outermost one -- wrappers nested under it are folded in), or a
              Claude Code Bash tool shell that no wrapper encloses
  outcome     isolated      a payload under it was measured in its own namespaces
              not_isolated  a payload ran and was measured in the host's
              failed        the wrapper exited non-zero and no payload ran: the
                            sandbox was TRIED and did not come up.  On rtx6000a
                            that is every one of 612 codex-linux-sandbox runs
                            (unprivileged user namespaces are AppArmor-restricted
                            there), and it must not read like "never tried".
              no_payload    exited 0 with nothing under it (`bwrap --help`)
              unknown       the payload's own verdict was unknown / not readable

A wrapper is read at its own exec, BEFORE it unshares, so the wrapper record
itself is always "not isolated"; that says nothing about the call it wraps,
and it is not a "capability probe" -- the payload under it is what counts.
Codex's unwrapped commands are not counted: a direct child of `codex
app-server` is as often its own `getconf`/`lsb_release`/`git` as a tool call,
and argv does not tell them apart.

Approval is read off the session root's OWN record (role `agent`, pid ==
agent_pid), from any identity block -- exit, truncated, and the residency /
conn / netio ticks, which are the only place a running session's root is seen.
Those ticks are never counted in the per-event matrices.  A child record's
approval is `unknown` and can never set its session's: the old first-record-
wins rule made the answer depend on whether the children or the root exited
first, and children always do.
"""
import collections

from . import add_counts, pct
from ..normalize import APPROVAL_STATES, SANDBOX_STATES

# The kernel cuts comm at 15 characters, so schema <= 6 carries
# `codex-linux-san`; matching the prefix covers both.
WRAPPERS = ("bwrap", "codex-linux-san")

SESSION_STATES = ("sandboxed", "partial", "failed", "unsandboxed", "unknown", "no_calls")
CALL_OUTCOMES = ("isolated", "not_isolated", "failed", "no_payload", "unknown")
# A share is taken over these only: `unknown` is not "no", and a probe that ran
# nothing (`no_payload`) is not a tool call at all.
CALL_KNOWN = ("isolated", "not_isolated", "failed")
APPROVAL_KNOWN = ("bypassed", "auto", "supervised")

# below[(host, ppid)] slots -- what the direct children of `ppid` reported
_PAYLOAD, _ISO, _UNK, _START, _UNITS = range(5)
# units[uid] slots
(_U_KIND, _U_HOST, _U_PID, _U_APID, _U_ROOT, _U_CLS, _U_TYPE, _U_USER, _U_CMD,
 _U_ISO, _U_PAYLOAD, _U_UNK, _U_FAILED, _U_OK, _U_ABSORBED) = range(15)


def _start(rec):
    e, d = rec.get("_ts_epoch"), rec.get("duration_s")
    if e is None or d is None:
        return None
    return e - d


def _outcome(u):
    if u[_U_ISO]:
        return "isolated"
    if u[_U_PAYLOAD]:
        return "unknown" if u[_U_UNK] else "not_isolated"
    if u[_U_FAILED]:
        return "failed"
    if u[_U_OK]:
        return "no_payload"
    return "unknown"


def session_state(calls):
    """One session's verdict from its tool-call outcomes (a Counter)."""
    known = sum(calls.get(k, 0) for k in CALL_KNOWN)
    if not known:
        return "unknown" if calls.get("unknown") else "no_calls"
    iso = calls.get("isolated", 0)
    if iso == known:
        return "sandboxed"
    if iso:
        return "partial"
    if calls.get("failed"):
        return "failed"
    return "unsandboxed"


def _reading(anc, kind, sb):
    if kind == "wrapper":
        if sb == "unsandboxed":
            return ("wrapper, read at its own exec before it unshares -- says nothing "
                    "about the call it wraps; see the tool-call outcomes")
        if sb == "sandboxed":
            return "wrapper already inside an isolated namespace (nested)"
        return None
    if sb == "sandboxed":
        if anc == "none":
            return ("isolated with no agent sandbox ancestor: a container, apptainer, a "
                    "systemd unit, or a reused pid inheriting one -- not an agent sandbox")
        return "under a sandbox wrapper, namespace-isolated"
    if sb == "unsandboxed" and anc != "none":
        return "under a sandbox wrapper, not isolated"
    return None


class SandboxReducer:
    EVENTS = ("exit", "truncated", "residency", "conn", "netio")
    # Only these are counted; the tick records are read for the root's identity.
    COUNTED = ("exit", "truncated")
    KEY = "sandbox"
    # Bounds the parent-keyed map of what children reported. An entry lives until
    # its parent's own record arrives, so this only fills with parents that never
    # exit in the window; overflow is counted, never silent.
    BELOW_CAP = 200000

    def __init__(self, cfg, classes, procs=None):
        self.cfg = cfg
        self.classes = list(classes)
        # The window's ProcIndex: a tool call is attributed to the session that
        # owns it through the same fold the trajectories use.
        self.procs = procs
        self.sb = collections.defaultdict(collections.Counter)
        self.ap = collections.defaultdict(collections.Counter)
        self.matrix = collections.Counter()       # (ancestry, kind, sandbox) -> n
        self.matrix_dwait = collections.Counter()  # same key -> summed dstate_wait_s
        self.matrix_dseen = collections.Counter()
        self.src = collections.Counter()
        self.detail = collections.Counter()
        self.agents_only = collections.defaultdict(collections.Counter)
        self.roots = {}       # (host, pid) -> [cls, agent_type, approval, detail, user]
        self.below = {}       # (host, ppid) -> [payload, iso, unk, earliest start, [uid]]
        self.units = {}       # (host, pid, exit epoch) -> unit (see the _U_ slots)
        self.n_units = 0
        self.below_capped = 0
        # Set in an hour state (`seed`, `strip`): the units carried in from
        # earlier hours, and those of them a wrapper in this hour absorbed.
        self._carried = ()
        self.absorbed_prior = []

    # ------------------------------------------------------------------ feed
    def feed(self, rec):
        ev = rec.get("event")
        pid = rec.get("pid")
        if rec.get("_role") == "agent" and rec.get("_a3") == "agent" \
                and pid is not None and pid == rec.get("agent_pid"):
            self.roots[(rec.get("host"), pid)] = [
                rec.get("_a3"), rec.get("agent_type"), rec.get("_approval") or "unknown",
                rec.get("_approval_detail"), rec.get("user")]
        if ev not in self.COUNTED:
            return
        cls = rec["_a3"]
        sb, ap = rec.get("_sandbox", "unknown"), rec.get("_approval", "unknown")
        self.sb[cls][sb] += 1
        self.ap[cls][ap] += 1
        if rec.get("_sandbox_src"):
            self.src[rec["_sandbox_src"]] += 1
        if rec.get("sandbox_detail"):
            for tok in str(rec["sandbox_detail"]).split(","):
                self.detail[tok] += 1
        wrapper = (rec.get("_command") or "").startswith(WRAPPERS)
        anc = rec.get("_sandbox_ancestry") or "none"
        mk = (anc, "wrapper" if wrapper else "process", sb)
        self.matrix[mk] += 1
        dw = rec.get("dstate_wait_s")
        if dw is not None:
            self.matrix_dwait[mk] += dw
            self.matrix_dseen[mk] += 1
        if rec.get("is_agent"):
            self.agents_only[cls][(sb, ap)] += 1
        if cls == "agent" and pid is not None:
            self._calls(rec, pid, wrapper, sb)

    def _calls(self, rec, pid, wrapper, sb):
        """Fold this exit into the tool-call units, bottom up.

        A child exits before the process that waited on it, so by the time a
        wrapper's own record arrives, everything under it has reported to its
        pid: whether a payload ran, whether one was isolated, and the units
        (nested wrappers, tool shells) below it, which it absorbs.
        """
        host = rec.get("host")
        role = rec.get("_role")
        start = _start(rec)
        got = self.below.pop((host, pid), None)
        if got is not None and start is not None and got[_START] is not None \
                and start > got[_START] + 1.0:
            got = None          # a reused pid: those children were an earlier process's
        up = list(got[_UNITS]) if got else []
        if wrapper or role == "tool-shell":
            if wrapper:
                iso = sb == "sandboxed" or bool(got and got[_ISO])
                payload, unk = bool(got and got[_PAYLOAD]), bool(got and got[_UNK])
                for uid in up:
                    u = self.units.get(uid)
                    if u is None:
                        continue
                    u[_U_ABSORBED] = True
                    iso = iso or u[_U_ISO]
                    payload = payload or u[_U_PAYLOAD]
                    unk = unk or u[_U_UNK]
                up = []
                code, sig = rec.get("exit_code"), rec.get("signal")
                failed = sig is not None or (code is not None and code != 0)
                ok = code == 0 and sig is None
            else:
                # The tool shell IS the call's payload; its own reading decides it.
                iso, payload, unk = sb == "sandboxed", True, sb in ("unknown", "denied")
                failed, ok = False, True
            # Named by the exit itself, not by a counter: a window's edge and
            # the saved hour it overlaps reduce the same exit, and must call
            # it the same unit (`merge`).
            e = rec.get("_ts_epoch")
            uid = (host, pid, e) if e is not None else ("n", self.n_units)
            self.n_units += 1
            self.units[uid] = [
                "wrapper" if wrapper else "tool-shell", host, pid, rec.get("agent_pid"),
                rec.get("tree_root_pid"), rec["_a3"], rec.get("agent_type"), rec.get("user"),
                (rec.get("_command") or "")[:24], iso, payload, unk, failed, ok, False]
            up.append(uid)
        if role == "agent":
            up = []             # a session root: its calls are final, nothing above absorbs them
        ppid = rec.get("ppid")
        if ppid is None:
            return
        key = (host, ppid)
        cur = self.below.get(key)
        if cur is None:
            if len(self.below) >= self.BELOW_CAP:
                self.below_capped += 1
                return
            cur = self.below[key] = [False, False, False, None, []]
        if not wrapper:
            cur[_PAYLOAD] = True
            if sb in ("unknown", "denied"):
                cur[_UNK] = True
        if sb == "sandboxed":
            cur[_ISO] = True
        if start is not None and (cur[_START] is None or start < cur[_START]):
            cur[_START] = start
        if up:
            cur[_UNITS].extend(up)

    # ----------------------------------------------------------------- merge
    # A window is merged from saved hour states (`hours`). What crosses an
    # hour is `below`: a wrapper whose payload exited in the previous hour
    # finds it there. So `below`, and the units it names, are CARRIED into the
    # next hour's reduction, where the wrapper absorbs its payload exactly as
    # one pass would. The saved hour holds only its own units, plus which
    # carried units a wrapper in it absorbed (`absorbed_prior`).
    CARRY_AGED = ("below",)

    def carry(self):
        return {"below": self.below, "units": self.units, "n_units": self.n_units}

    @staticmethod
    def carry_done(c):
        """Keep only the units the (aged) `below` still names."""
        units = c["units"]
        c["units"] = {uid: units[uid] for b in c["below"].values()
                      for uid in b[_UNITS] if uid in units}

    def seed(self, c):
        self.below = c["below"]
        self.units = dict(c["units"])
        self._carried = set(c["units"])
        self.n_units = c["n_units"]

    def install(self, c):
        """Take on the join state at the end of the hours merged in, so the
        records after them -- a window's tail, its live feed -- join as one
        pass would. A carried unit this state does not hold predates the
        window: it is held only for a wrapper in the window to absorb, marked
        absorbed so it is never counted as one of the window's calls."""
        self.below = c["below"]
        for uid, u in c["units"].items():
            if uid not in self.units:
                u[_U_ABSORBED] = True
                self.units[uid] = u
        self.n_units = max(self.n_units, c["n_units"])

    def strip(self):
        carried = self._carried
        self.absorbed_prior = [uid for uid in carried
                               if uid in self.units and self.units[uid][_U_ABSORBED]]
        self.units = {uid: u for uid, u in self.units.items() if uid not in carried}
        self._carried = ()
        self.below = {}

    def merge(self, o):
        """Fold a LATER state into this one. `o` is consumed."""
        for mine, theirs in ((self.sb, o.sb), (self.ap, o.ap),
                             (self.agents_only, o.agents_only)):
            for cls, c in theirs.items():
                add_counts(mine[cls], c)
        for mine, theirs in ((self.matrix, o.matrix), (self.matrix_dwait, o.matrix_dwait),
                             (self.matrix_dseen, o.matrix_dseen), (self.src, o.src),
                             (self.detail, o.detail)):
            add_counts(mine, theirs)
        self.roots.update(o.roots)          # the newest record of a root wins, as in `feed`
        for uid, u in o.units.items():
            self.units.setdefault(uid, u)   # the same exit twice is one unit
        for uid in o.absorbed_prior:
            u = self.units.get(uid)
            if u is not None:
                u[_U_ABSORBED] = True
        self.n_units = max(self.n_units, o.n_units)
        self.below_capped += o.below_capped

    # ---------------------------------------------------------------- result
    def _owner(self, host, apid, root):
        if self.procs is None or apid is None:
            return apid
        try:
            return self.procs.owner(host, apid, root)[0]
        except Exception:
            return apid

    def _sessions(self):
        """(host, owner pid) -> session dict: every agent-role root in the window,
        plus the owner of any tool call whose root was never seen."""
        out = {}
        for key, (cls, at, ap, det, user) in self.roots.items():
            out[key] = {"cls": cls, "agent_type": at, "approval": ap, "detail": det,
                        "user": user, "calls": collections.Counter()}
        attempts = collections.Counter()
        for u in self.units.values():
            if u[_U_ABSORBED]:
                continue
            oc = _outcome(u)
            key = (u[_U_HOST], self._owner(u[_U_HOST], u[_U_APID], u[_U_ROOT]))
            s = out.get(key)
            if s is None:
                s = out[key] = {"cls": u[_U_CLS], "agent_type": u[_U_TYPE],
                                "approval": "unknown", "detail": None, "user": u[_U_USER],
                                "calls": collections.Counter()}
            s["calls"][oc] += 1
            if u[_U_KIND] == "wrapper":
                w = ("codex-linux-sandbox" if u[_U_CMD].startswith("codex-linux-san")
                     else "bwrap" if u[_U_CMD].startswith("bwrap") else u[_U_CMD])
                attempts[(w, u[_U_TYPE], oc)] += 1
        return out, attempts

    def agent_calls(self):
        """(host, owner pid) -> its tool calls' outcomes, for every session that
        made one: what the agent trees tag an agent with (session_state)."""
        return {k: dict(s["calls"]) for k, s in self._sessions()[0].items() if s["calls"]}

    def result(self):
        sessions, attempts = self._sessions()
        sess_sb = collections.defaultdict(collections.Counter)
        sess_ap = collections.defaultdict(collections.Counter)
        calls = collections.defaultdict(collections.Counter)
        modes = collections.Counter()
        for (host, pid), s in sessions.items():
            cls = s["cls"] or "unlabeled"
            state = session_state(s["calls"])
            s["state"] = state
            sess_sb[cls][state] += 1
            sess_ap[cls][s["approval"]] += 1
            calls[cls].update(s["calls"])
            if (host, pid) in self.roots:
                modes[(s["agent_type"], s["approval"], s["detail"])] += 1

        def _share(d):
            # A flat {state: count} map per class. Shares are the consumer's to
            # derive, over the KNOWN states the payload names below: shipping
            # counts AND a precomputed pct invites the two to disagree.
            return {cls: dict(c) for cls, c in d.items()}

        def _totals(d):
            return {cls: sum(c.values()) for cls, c in d.items()}

        rows = []
        for (anc, kind, sb), n in sorted(self.matrix.items(), key=lambda kv: -kv[1]):
            seen = self.matrix_dseen.get((anc, kind, sb), 0)
            rows.append({
                "ancestry": anc, "kind": kind, "sandbox": sb, "n": n,
                "dstate_wait_s": round(self.matrix_dwait.get((anc, kind, sb), 0.0), 2),
                "dstate_coverage_pct": pct(seen, n),
                "reading": _reading(anc, kind, sb),
            })

        def _calls_known(c):
            return sum(c.get(k, 0) for k in CALL_KNOWN)

        table = []
        for (host, pid), s in sessions.items():
            c = s["calls"]
            known = _calls_known(c)
            table.append({
                "host": host, "pid": pid, "user": s["user"], "actor3": s["cls"],
                "agent_type": s["agent_type"], "approval": s["approval"],
                "approval_detail": s["detail"], "state": s["state"],
                "calls": dict(c), "calls_known": known,
                "isolated_pct": pct(c.get("isolated", 0), known) if known else None,
            })
        table.sort(key=lambda r: (-sum(r["calls"].values()), -(r["calls"].get("failed", 0)),
                                  str(r["host"]), r["pid"] or 0))
        return {
            "by_class_events": _share(self.sb),
            "approval_by_class_events": _share(self.ap),
            "n_events": _totals(self.sb),
            "by_class_sessions": _share(sess_sb),
            "approval_by_class_sessions": _share(sess_ap),
            "n_sessions": _totals(sess_sb),
            "tool_calls": _share(calls),
            "attempts": [{"wrapper": w, "agent_type": at, "outcome": oc, "n": n}
                         for (w, at, oc), n in sorted(attempts.items(),
                                                      key=lambda kv: -kv[1])],
            "sessions": table[:40],
            "sessions_total": len(table),
            "launch_modes": [{"agent_type": at, "approval": ap, "detail": det, "n": n}
                             for (at, ap, det), n in sorted(modes.items(),
                                                            key=lambda kv: -kv[1])],
            "ancestry_matrix": rows,
            "sandbox_src": dict(self.src),
            "sandbox_detail": dict(self.detail),
            "agent_only": {c: [{"sandbox": k[0], "approval": k[1], "n": v}
                               for k, v in sorted(m.items(), key=lambda kv: -kv[1])]
                           for c, m in self.agents_only.items()},
            "below_capped": self.below_capped,
            "states": {"sandbox": list(SANDBOX_STATES),
                       "sandbox_known": ["sandboxed", "unsandboxed"],
                       "session": list(SESSION_STATES),
                       "session_known": ["sandboxed", "partial", "failed", "unsandboxed"],
                       "tool_call": list(CALL_OUTCOMES),
                       "tool_call_known": list(CALL_KNOWN),
                       "approval": list(APPROVAL_STATES),
                       "approval_known": list(APPROVAL_KNOWN)},
            "notes": {
                "meaning": "sandbox = NAMESPACE ISOLATION: /proc/<pid>/ns/{user,mnt,pid,net} "
                           "against pid 1's. Landlock has no /proc exposure and seccomp is "
                           "detail only, so a Landlock- or seccomp-only sandbox reads as not "
                           "isolated; a container, an apptainer user namespace or a "
                           "PrivateTmp= systemd unit reads as isolated without being an "
                           "agent sandbox.",
                "denied": "'exited before read' (key: denied): the namespace links were "
                          "gone when the collector looked -- it runs as root, so this is a "
                          "process that lived a few ms, not a permission problem. It is "
                          "NOT 'not isolated', and shares are taken over known verdicts.",
                "inherit": "most per-event verdicts are copied from the parent at fork "
                           "(sandbox_src=inherit), not read on that pid; namespaces are "
                           "inherited across fork, so this is exact unless a pid was reused.",
                "sessions": "a session is an agent-role root (roles.py) seen in the window, "
                            "plus the owner of any tool call whose root was not seen. Its "
                            "sandbox verdict is the outcome of its TOOL CALLS -- the agent "
                            "process itself is outside its own sandbox by design.",
                "tool_calls": "a tool call is a sandbox wrapper invocation (bwrap, "
                              "codex-linux-sandbox; nested wrappers folded into the "
                              "outermost) or a Claude Code Bash tool shell no wrapper "
                              "encloses. Codex's unwrapped commands are not counted: argv "
                              "cannot tell them from its own getconf/lsb_release/git.",
                "failed": "'sandbox attempt failed': the wrapper exited non-zero and no "
                          "payload ran under it -- tried, and the sandbox did not come up "
                          "(e.g. unprivileged user namespaces AppArmor-restricted). A "
                          "payload the collector dropped as a timer would look the same.",
                "approval": "read from the session root's own argv only. 'no bypass flag at "
                            "launch' (key: supervised) is all argv can say: "
                            "~/.claude/settings.json, a --settings file, codex config.toml "
                            "and a mid-session mode switch are invisible. `codex app-server` "
                            "takes its policy per thread over JSON-RPC: not observable. "
                            "`--permission-mode auto` is its own state.",
                "events_approval": "per event, approval is on the session root's record "
                                   "only: a child is 'unknown', a tagged non-agent (tool "
                                   "shell, probe, IDE) and a human process are 'n/a'.",
            },
        }
