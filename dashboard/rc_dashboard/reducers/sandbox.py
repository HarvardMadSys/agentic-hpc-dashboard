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

from . import add_counts, min_of, pct
from ..normalize import APPROVAL_STATES, SANDBOX_STATES
from ..procindex import _start as process_start, users_conflict

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
 _U_ISO, _U_PAYLOAD, _U_UNK, _U_FAILED, _U_OK, _U_ABSORBED, _U_START) = range(16)
# roots[(host, pid)] slots, after cls, agent_type, approval, detail: the user, the
# earliest time the process is known at (its start, else its first record), its exit
_R_USER, _R_START, _R_END = 4, 5, 6
_SLACK_S = 1.0          # two readings of one process's start differ by this much


def _in_root(r, user, t):
    """Whether a record or tool call of `user` at time `t` (a record's start,
    else its own time; a call's start) can be of root r's process. A pid is
    reused: another user's, or one from after the root exited, is another
    process's. `_root_gen` picks among the roots it can be of."""
    if users_conflict(r[_R_USER], user):
        return False
    return not (t is not None and r[_R_END] is not None and t > r[_R_END] + _SLACK_S)


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


class SandboxReducer:
    EVENTS = ("exit", "truncated", "residency", "conn", "netio")
    # Only exits are counted; the other records are read for the root's
    # identity. A `truncated` one is a process still running at a collector
    # stop, which its exit after the restart counts again.
    COUNTED = ("exit",)
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
        # (host, pid) -> [cls, agent_type, approval, detail, user, start, exit]: the
        # first root on a pid; `later_roots` holds those after it (a pid is reused)
        self.roots = {}
        self.later_roots = {}
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
            st, t = process_start(rec), rec.get("_ts_epoch")
            at = st if st is not None else t
            self._take_root((rec.get("host"), pid), [
                rec.get("_a3"), rec.get("agent_type"), rec.get("_approval") or "unknown",
                rec.get("_approval_detail"), rec.get("user"), at,
                t if ev == "exit" else None], at)
        if ev not in self.COUNTED:
            return
        cls = rec["_a3"]
        sb, ap = rec.get("_sandbox", "unknown"), rec.get("_approval", "unknown")
        self.sb[cls][sb] += 1
        self.ap[cls][ap] += 1
        wrapper = (rec.get("_command") or "").startswith(WRAPPERS)
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
            # it the same unit (`merge`). An unstamped one by the counter, under
            # its host: hours reduced by node (hours.py, `ingest.workers`) count
            # each node's units apart, and two nodes' counters meet in a merge.
            e = rec.get("_ts_epoch")
            uid = (host, pid, e) if e is not None else ("n", host, self.n_units)
            self.n_units += 1
            self.units[uid] = [
                "wrapper" if wrapper else "tool-shell", host, pid, rec.get("agent_pid"),
                rec.get("tree_root_pid"), rec["_a3"], rec.get("agent_type"), rec.get("user"),
                (rec.get("_command") or "")[:24], iso, payload, unk, failed, ok, False, start]
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

    def _all_roots(self):
        """((host, pid), generation, root): the first root on a pid is 0."""
        for key, r in self.roots.items():
            yield key, 0, r
            for i, m in enumerate(self.later_roots.get(key, ()), 1):
                yield key, i, m

    def _root_gen(self, key, user, t):
        """(generation, root) of the root on `key` that a record or a tool call
        of `user` at time `t` is of, or (None, None). Of its user's roots that
        had not exited by `t`, the last to have started by then. When none had,
        the first of them: a start known only from a later record (a tick with
        no age) or read late is after the true one, and lifetimes on a pid
        follow one another, so the first root still running after `t` is the
        one that followed the last exit before it. A time not known takes the
        first of its user's roots."""
        r0 = self.roots.get(key)
        if r0 is None:
            return None, None
        live = [(g, r) for g, r in enumerate([r0] + self.later_roots.get(key, []))
                if _in_root(r, user, t)]
        if not live or t is None:
            return live[0] if live else (None, None)
        started = [(g, r) for g, r in live
                   if r[_R_START] is not None and r[_R_START] <= t + _SLACK_S]
        if started:
            return max(started, key=lambda gr: gr[1][_R_START])
        return min(live, key=lambda gr: (gr[1][_R_START] is None, gr[1][_R_START] or 0.0))

    def _take_root(self, key, r, at):
        """A root's record (`feed`), or a later state's root (`merge`), as of
        time `at`. The newest record of a root wins, as its approval reads the
        same on each; its earliest time is kept and its exit the one seen. Of
        another process on the pid (_in_root), it is a root of its own."""
        _gen, cur = self._root_gen(key, r[_R_USER], at)
        if cur is None:
            if key in self.roots:
                self.later_roots.setdefault(key, []).append(r)
            else:
                self.roots[key] = r
            return
        start = min_of(cur[_R_START], r[_R_START])
        cur[:_R_START] = r[:_R_START]
        cur[_R_START] = start
        if r[_R_END] is not None:
            cur[_R_END] = r[_R_END]

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

    def carry_caps(self):
        """What one pass holds the carried map to (`hours.combine_carries`)."""
        return {"below": self.BELOW_CAP}

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
        for mine, theirs in ((self.sb, o.sb), (self.ap, o.ap)):
            for cls, c in theirs.items():
                add_counts(mine[cls], c)
        for key, _gen, r in o._all_roots():   # the newest record of a root wins, as in `feed`
            self._take_root(key, r, r[_R_START])
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
        """(host, owner pid, root generation) -> session dict: every agent-role
        root in the window, a pid's later ones apart (a pid is reused), plus the
        owner of any tool call whose root was never seen (generation None)."""
        out = {}
        for (host, pid), gen, r in self._all_roots():
            out[(host, pid, gen)] = {"cls": r[0], "agent_type": r[1], "approval": r[2],
                                     "detail": r[3], "user": r[_R_USER],
                                     "calls": collections.Counter(), "root": True}
        attempts = collections.Counter()
        for u in self.units.values():
            if u[_U_ABSORBED]:
                continue
            oc = _outcome(u)
            host = u[_U_HOST]
            owner = self._owner(host, u[_U_APID], u[_U_ROOT])
            # by when the call started, which its agent was running at: a
            # background call's shell can exit after its agent has
            gen, _r = self._root_gen((host, owner), u[_U_USER], u[_U_START])
            key = (host, owner, gen)
            s = out.get(key)
            if s is None:
                s = out[key] = {"cls": u[_U_CLS], "agent_type": u[_U_TYPE],
                                "approval": "unknown", "detail": None, "user": u[_U_USER],
                                "calls": collections.Counter(), "root": False}
            s["calls"][oc] += 1
            if u[_U_KIND] == "wrapper":
                w = ("codex-linux-sandbox" if u[_U_CMD].startswith("codex-linux-san")
                     else "bwrap" if u[_U_CMD].startswith("bwrap") else u[_U_CMD])
                attempts[(w, u[_U_TYPE], oc)] += 1
        return out, attempts

    def agent_calls(self):
        """(host, owner pid) -> its tool calls' outcomes, for every session that
        made one: what the agent trees tag an agent with (session_state). The
        trees draw the first process on a pid: its root's calls, or where no
        root was seen, the calls of none."""
        out = {}
        for (host, pid, gen), s in self._sessions()[0].items():
            if s["calls"] and (gen == 0 or (gen is None and (host, pid) not in self.roots)):
                out[(host, pid)] = dict(s["calls"])
        return out

    def result(self):
        sessions, attempts = self._sessions()
        sess_sb = collections.defaultdict(collections.Counter)
        sess_ap = collections.defaultdict(collections.Counter)
        calls = collections.defaultdict(collections.Counter)
        modes = collections.Counter()
        for s in sessions.values():
            cls = s["cls"] or "unlabeled"
            state = session_state(s["calls"])
            s["state"] = state
            sess_sb[cls][state] += 1
            sess_ap[cls][s["approval"]] += 1
            calls[cls].update(s["calls"])
            if s["root"]:
                modes[(s["agent_type"], s["approval"], s["detail"])] += 1

        def _share(d):
            # A flat {state: count} map per class. Shares are the consumer's to
            # derive, over the KNOWN states the payload names below: shipping
            # counts AND a precomputed pct invites the two to disagree.
            return {cls: dict(c) for cls, c in d.items()}

        def _totals(d):
            return {cls: sum(c.values()) for cls, c in d.items()}

        def _calls_known(c):
            return sum(c.get(k, 0) for k in CALL_KNOWN)

        table = []
        for (host, pid, gen), s in sessions.items():
            c = s["calls"]
            known = _calls_known(c)
            table.append({
                # `gen` tells apart the sessions of processes that held the same
                # pid one after another (`_all_roots`: 0 the first, None a call
                # with no root found): host and pid alone are not a row's key
                "host": host, "pid": pid, "gen": gen, "user": s["user"], "actor3": s["cls"],
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
                "meaning": "sandboxed = ran in its own namespaces: /proc/<pid>/ns/{user,mnt,pid,net} "
                           "against pid 1's. Landlock has no /proc exposure and seccomp is "
                           "detail only, so a Landlock- or seccomp-only sandbox reads as not "
                           "sandboxed; a container, an apptainer user namespace or a "
                           "PrivateTmp= systemd unit reads as sandboxed without being an "
                           "agent sandbox.",
                "denied": "'exited before it was checked' (key: denied): the namespace links were "
                          "gone when the collector looked -- it runs as root, so this is a "
                          "process that lived a few ms, not a permission problem. It is "
                          "NOT 'not sandboxed', and shares are taken over the known answers.",
                "inherit": "most per-process answers are copied from the parent at fork "
                           "(sandbox_src=inherit), not read on that pid; namespaces are "
                           "inherited across fork, so this is exact unless a pid was reused.",
                "sessions": "a session is an agent-role root (roles.py) seen in the window, "
                            "plus the owner of any tool call whose root was not seen. A pid is "
                            "reused: a root of another user, or one started after the held root "
                            "exited, is a session of its own, and a call goes to the root of "
                            "its user that was running when it started. Whether "
                            "it ran sandboxed is the outcome of its TOOL CALLS -- the agent "
                            "process itself is outside its own sandbox by design.",
                "tool_calls": "a tool call is a sandbox wrapper invocation (bwrap, "
                              "codex-linux-sandbox; nested wrappers folded into the "
                              "outermost) or a Claude Code Bash tool shell no wrapper "
                              "encloses. Codex's unwrapped commands are not counted: argv "
                              "cannot tell them from its own getconf/lsb_release/git.",
                "failed": "'sandbox failed to start': the wrapper exited non-zero and no "
                          "wrapped command ran under it -- tried, and the sandbox did not come up "
                          "(e.g. unprivileged user namespaces AppArmor-restricted). A "
                          "wrapped command the collector dropped as a timer would look the same.",
                "approval": "read from the session root's own command line only. 'no bypass "
                            "flag' (key: supervised) is all a command line can say: "
                            "~/.claude/settings.json, a --settings file, codex config.toml "
                            "and a mid-session mode switch are invisible. `codex app-server` "
                            "takes its policy per thread over JSON-RPC: its command line can't tell. "
                            "`--permission-mode auto` is its own state.",
                "events_approval": "per event, approval is on the session root's record "
                                   "only: a child is 'unknown', a tagged non-agent (tool "
                                   "shell, probe, IDE) and a human process are 'n/a'.",
            },
        }
