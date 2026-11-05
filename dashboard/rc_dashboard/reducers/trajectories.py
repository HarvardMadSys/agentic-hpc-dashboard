"""Requirement 6: top trajectories in the window.

A trajectory is the ordered chain of tool calls in one session -- the repo's own
definition (docs/TRAJECTORIES.md, analyze/extract_trajectories.py).  Sessionizing
follows analyze/extract_trajectories.py:278-294 exactly: `apid` from the agent
pid, else `sess` from the cgroup's session-N.scope, else `tty` as user@tty, else
`usr`.  `usr` is counted but NEVER ranked -- "everything that user did today" is
not a trajectory.

An `apid` key is then FOLDED into the agent it belongs to. The collector keys a
process on its nearest classifier-tagged ancestor, and most tagged processes are
not agents (roles.py): every Claude Code Bash tool call runs in a tagged
`zsh -c source ~/.claude/shell-snapshots/...`, so one Claude session arrived as
one key per tool call -- 204 keys for one tree on madsys-gpu1 on 2026-10-06, and
383 for one Codex tree. The fold walks each key up the window's ProcIndex to the
nearest `agent`-role process, at result time, because a helper's parent is only
known once the helper's own record arrives, after its children's. Every row
says how many keys it folds and how the least certain was attributed.

This works at a 24h window because the state is incrementally composable:
records arrive in time order, so each session's counters and its run-length
encoded chain are appended in place.  Memory is O(sessions x chain cap), bounded
by `max_sessions`: past it, whole sessions go, the least recently active first
(`_shed`).

"Top" is SEVEN rankings, not one: by processes, CPU, memory, disk I/O, network,
distinct tools, and recency.  A single ranking is dominated by one poll loop
and one mega-user -- CLAUDE.md records one user at ~81% of agent submissions --
so each list ships with `dominant_user_share_pct`, letting the panel state its
own concentration instead of implying a fleet-wide pattern.

Disk I/O is each process's OWN. The collector reads `task->signal->ioac`, which
the kernel also credits with the I/O of every child the process reaped
(`wait_task_zombie`), so a shell's figure holds its commands' as well -- in the
rtx6000a feed a parent's read+write covered its exited children's in 132 of 133
families. Summed over a session as it comes, a command's bytes counted once per
shell above it. Here a process's figure loses what its children carried
(`reaped`, the join between a child's exit and its parent's later one), so a sum
over any set of processes counts each byte once. Network bytes and peak memory
are per process already.

Each row also carries `drift`: two process-level proxies for an agent leaving the
task it started on. Neither sees the model's context; both are labelled proxies.

* cwd drift -- the share of the session's processes whose cwd lies outside the
  agent's launch directory (the agent process's own cwd, else the first call's).
  The collector reads cwd at exec for tagged processes (each tool-call shell) and
  inherits it otherwise, so a `cd` inside one shell command is not seen.
* tool-mix drift -- Jensen-Shannon divergence (base 2, 0..1) between the purpose
  mix of the first and the last third of the session's processes. Null below
  MIN_DRIFT_N processes: a third of a handful is noise, not a finding.

Each agent row also carries `phases`: what the agent was doing, as a handful of
labelled stretches read from the commands it ran, and `n_calls`, its tool calls.
Which processes are calls and what each one is is `phase_rules`; the bounded
call log a session holds and the join below are `phase_log`; the cutting into
phases is `phase_segment`, when a row is built. Above the phases are `tasks`:
the session cut by what it worked on (`phase_tasks`), each named from the
agent's own words or from the names in its commands, and a `headline` for the
session. A row is cut into tasks, then into phases at their boundaries, then
named, in one pass (`phase_block`). A row whose calls are not
recognized (an agent type outside PHASE_AGENTS, a person, an IDE) says so, and
its `n_calls` is null: not recognized is not none.

A row is one local day, since its session id carries the day, and an agent that
works past midnight has a row for each. Those rows serve one block, cut from the
calls of all of them, so that midnight cuts no task and either row opens the
whole session (`phase_block`). Every other field of a row stays its day's,
`n_calls` among them.

Records arrive child first, so each exit of an agent session is read bottom up
(`_phase`). A direct child of the agent is a call or the agent's own chore, and
takes with it what ran under it. Any other process is folded into its parent's
pending entry (`pend`, joined on the parent's pid as `reaped` is), unless its
parent is a call already held that it outlived -- a job left running in the
background, whose end becomes the call's `past`. A child that outlives a
parent of any other kind (a pipeline's stage that ends after its shell) goes
where that parent went, which `exited` remembers for the latest processes: up
into the entry the parent joined, into the call it was part of as having run
past it, or into housekeeping (`_late`, R57). Pushed under the gone parent, it
would start an entry nobody pops, and read as a call still running. A pid is
reused, so a note is the parent's only when the child began while the noted
process ran; every path a record takes is noted, and a child placed nowhere,
or pushed without that check, is counted. A parent
that never exits leaves its entry pending: a row reads it as a call still
running when it is a steady loop, or when it was busy at the session's last
record with two children or more in its stretch, the time since its children
last left a gap of `idle_s`, and the call runs over that stretch only. A
sparse helper's two children half an hour apart are no call, a helper's
hourly bursts are no call running all day, and the idle between them stays
idle. What else ran under such a parent is counted as housekeeping. A root
that tops its tree ends at its own record: the runtime, a daemon, a server, or
a probe is never a call and is never pushed up, and what waited under it is
counted as its housekeeping (`phase_unread`); a tool call that outlived its
agent and became its own root ends as a call. A record with no time is counted
(`no_ts`) and never placed, and so is what ran under it (`phase_untimed`); one
whose parent is gone (ppid 0 or 1, as a job whose shell exited first reports)
is counted (`phase_reparented`) and never pushed.

A sandbox wrapper's call is named for the command it ran. On boslogin06 (FAS
RC, schema 7) on 2026-10-28, in the first 1.5 M exit records,
`codex-linux-sandbox` ran 2,618 processes, 2,580 of them `bwrap`; `bwrap` ran
2,084, 867 of them Codex re-run as `codex-linux-sandbox` (906 were a `true`
probe and 174 `bwrap --help`); and that inner wrapper ran the command, 887 in
all: `bash -lc ...` 550 times, `sed` 85, `codex` 48, `rg` 47, `cat` 45. So a
pending entry keeps each record's own argv first (the wrappers have none), and
the wrapper's call is that argv read as a direct child's would be: a shell's
command string as shell text, any other program's argv as one command.

Commands are masked (`redact`) and cut when they are interned as previews,
never later, and held raw only in the pending join, cut to RAW_KEEP. Both joins
cross hours, so both are carried (`CARRY_AGED`): the pending entries, and the
(session, pid) of each call, for a process that outlives its call into a later
hour; its end is applied to the call when the hours merge (`past_due`).

A call also names what it worked on and quotes what the agent said (`phase_names.tagset`), read
from the whole masked text, not from the preview's cut. The text is masked once, and the preview
and the tag set are each an id the call holds (`PREV`, `TAGS`), interned per reducer and renumbered
when hours merge (`_previews`, `_tagsets`; 0 is none for a preview and the empty set for a tag
set). A reducer holds at most MAX_PREVIEWS previews and MAX_TAGSETS tag sets: a call that finds no
room for its own has 0, and is counted (`preview_overflow`, `tag_overflow`).
"""
import collections
import copy
import itertools
import math
import os
import re
import time
from array import array
from operator import itemgetter

from ..anonymize import HOME_PATTERNS
from ..procindex import VIA
from ..redact import redact

from . import pct
from .phase_log import (OPEN, P_CATS, P_CMDS, P_FIRST, P_HELP, P_LAST, P_N, P_SID, P_SINCE,
                        P_SINCE_N, P_TEXT, P_WROTE, PAST_SLACK_S, PREV, START_SLACK_S, TAGS,
                        WROTE, PendingJoin, PhaseLog, fold_entry)
from .phase_names import tagset
from .phase_rules import (_TOOL_CALL_ROLES, CATS, PHASE_AGENTS, _argv, _is_sandbox, call_text,
                          category_of, category_of_argv, classify_direct, codex_chore_of,
                          is_fs_helper, is_sandbox_probe, login_helper, norm_hash, parent_is_agent,
                          program_name, shell_text)
from .phase_segment import (DEFAULTS as PHASE_DEFAULTS, NEVER_EXITED, SEEN_SHOWN, prepared,
                            segment)
from .phase_tasks import DEFAULTS as TASK_DEFAULTS, name_tasks, task_spans

RANKS = ("recent", "events", "cpu_s", "peak_rss_mb", "io_mb", "net_mb", "distinct_tools")
RANK_MEANING = {
    "recent": "the newest process exit first, real sessions only",
    "events": "most tool calls",
    "cpu_s": "most self CPU",
    "peak_rss_mb": "largest peak resident memory of any one process",
    "io_mb": "most disk bytes read and written, each process's own",
    "net_mb": "most network bytes sent and received",
    "distinct_tools": "most distinct effective_comm tokens",
}
MAX_SEQ_RUNS = 400          # matches extract_trajectories.MAX_SEQ_RUNS
# Tool calls that only wait on something else: polling the process table
# (ps, pgrep, sleep loops) and polling the scheduler (squeue, sacct).
WAIT_PURPOSES = frozenset({"poll", "slurm_mon"})
MIN_DRIFT_N = 30            # processes; below this the thirds are noise
DRIFT_CAP = 100000          # per-session processes kept for drift; past it, flagged
MAX_CWDS = 200000           # distinct cwds interned per window; past it, unmeasured
REAPED_MAX = 200000         # parents with exited children held for their own exit
_NO_PARENT = (None, 0, 1)   # a ppid that names no process that started it
MAX_PREVIEWS = 200000       # masked command previews interned per reducer; past it, none
MAX_TAGSETS = 200000        # tag sets (names, words) interned per reducer; past it, none
CALLS_AT_MAX = 200000       # (session, pid) of calls held for a process that outlives its call
PAST_DUE_MAX = 50000        # ends a session holds for calls an earlier state has; past it, dropped
# Where the latest processes went (`_went`), for a child that outlives its parent: about twenty
# minutes of boslogin06's agent records (39 a second on 2026-10-28). Past it, the oldest go.
EXITED_MAX = 50000
LATE_HOPS = 8               # parents a late child follows up, each of which has exited too
SEEN_NAME = 40              # characters of a program seen under an open call, after masking: as
#                             the census names them (live.NAME_SHOWN)
MAX_HK_SHAPES = 50          # housekeeping shapes a session names; the rest count as OTHER
OTHER = "other"
PREVIEW = 220               # as proctree.PREVIEW: characters of a command shown, masked first
RAW_KEEP = 1024             # as proctree.RAW_KEEP: characters held raw until masked
PHASE_CACHE_MAX = 4096      # phase blocks kept between reads
UNAVAILABLE = "phases not available for this agent type"
# A home directory: `/home/<x>`, or `/n/home<NN>/<x>` on FAS RC. It names a user, and no project
# (`project_of`).
_HOME_DIR = re.compile(r"/(?:home|n/home\d+)/[^/]+")
# The person a path's home or lab-tree folder is named for, as `anonymize` finds one by default:
# `jdoe` of `/n/netscratch/alpha_lab/Lab/jdoe` (`_people_in`).
_PERSON_FOLDERS = tuple(re.compile(p) for p in HOME_PATTERNS)
_NAME_PIECES = re.compile(r"[_.-]+")    # the parts of a folder's name (`_named_for`)
_CAT_RANK = {c: i for i, c in enumerate(CATS)}


class Session:
    __slots__ = ("sid", "kt", "kv", "host", "user", "cls", "agent_type", "n", "cpu",
                 "tools", "chain", "purpose", "first", "last", "rss", "rd", "wr", "tx", "rx",
                 "sandbox", "approval", "ancestry", "autonomous", "truncated", "no_ts",
                 "users", "pids", "tree_root", "chain_t", "purpose_t",
                 "ev_t", "ev_p", "ev_c", "drift_cut", "wait", "over", "pover",
                 "phase", "past_due")

    def __init__(self, rec):
        self.sid = rec["_sid"]
        self.kt = rec["_kt"]
        self.kv = rec["_kv"]
        self.host = rec.get("host")
        self.user = rec.get("user")
        self.cls = rec["_a3"]
        self.agent_type = rec.get("agent_type")
        self.n = 0
        self.cpu = 0.0
        self.tools = set()
        self.chain = []          # [[tool, runlength], ...]
        self.purpose = []        # [[purpose, runlength], ...]
        # When each run began, parallel to the two chains: a folded session
        # interleaves its keys' runs by these.
        self.chain_t = []
        self.purpose_t = []
        self.tree_root = rec.get("tree_root_pid")
        self.first = None
        self.last = None
        self.rss = 0.0
        self.rd = 0.0            # disk MB, each process's own (`TrajectoryReducer.own_io`)
        self.wr = 0.0
        self.tx = 0.0            # network MB
        self.rx = 0.0
        self.sandbox = "unknown"
        self.approval = "unknown"
        self.ancestry = None
        self.autonomous = 0
        self.truncated = False
        self.no_ts = False
        self.users = {}
        self.pids = []
        # One entry per process, in arrival order, for drift: its time, its
        # purpose id and its cwd id (0 = not measured), interned by the reducer.
        self.ev_t = array("d")
        self.ev_p = array("H")
        self.ev_c = array("I")
        self.drift_cut = False
        self.wait = 0            # tool calls spent waiting (WAIT_PURPOSES), exact
        # Calls a full chain dropped, by tool (and by purpose): never shown, but
        # what lets two halves of a session merge into the chain one pass
        # would have built (`_rle_merge`). None until a chain fills.
        self.over = None
        self.pover = None
        # The tool calls and the housekeeping, bounded (phase_log), and the ends
        # of processes that outlived a call an earlier hour holds: pid ->
        # [latest end, its start], applied when the hours merge (`merge_session`).
        self.phase = PhaseLog()
        self.past_due = None

    def add(self, rec, now, io=(0.0, 0.0)):
        """One exit; `io` is its own disk MB read and written."""
        e = rec.get("_ts_epoch")
        if e is None:
            self.no_ts = True          # counted, never silently stamped as "now"
        self.n += 1
        self.cpu += rec.get("cpu_s") or 0.0
        if rec.get("_purpose") in WAIT_PURPOSES:
            self.wait += 1
        if len(self.pids) < 8 and rec.get("pid") is not None:
            self.pids.append(rec["pid"])
        t = rec.get("_eff") or rec.get("_command") or "(empty)"
        self.tools.add(t)
        if _rle_push(self.chain, self.chain_t, t, e, MAX_SEQ_RUNS, self):
            self.over = _bump(self.over, t)
        p = rec.get("_purpose") or "other"
        if _rle_push(self.purpose, self.purpose_t, p, e, MAX_SEQ_RUNS, None):
            self.pover = _bump(self.pover, p)
        if self.tree_root is None and rec.get("tree_root_pid") is not None:
            self.tree_root = rec["tree_root_pid"]
        if e is not None:
            self.first = e if self.first is None else min(self.first, e)
            self.last = e if self.last is None else max(self.last, e)
        r = rec.get("peak_rss_mb")
        if r:
            self.rss = max(self.rss, r)
        self.rd += io[0]
        self.wr += io[1]
        self.tx += (rec.get("net_tx_bytes") or 0) / 1e6
        self.rx += (rec.get("net_rx_bytes") or 0) / 1e6
        # The derived flag: the deployed collector emits `autonomous: false` even
        # on a root launched with a bypass flag.
        if rec.get("_autonomous"):
            self.autonomous += 1
        if self.sandbox == "unknown" and rec.get("_sandbox") != "unknown":
            self.sandbox = rec.get("_sandbox")
        # Only for a session with no agent process to ask (a tty, a cgroup
        # scope): an agent session's approval is its owner's (`owners`), never
        # whichever record came first -- a tool shell's `na` would win that.
        if self.approval == "unknown" and rec.get("_approval") != "unknown":
            self.approval = rec.get("_approval")
        if not self.ancestry and rec.get("_sandbox_ancestry"):
            self.ancestry = rec["_sandbox_ancestry"]
        if self.agent_type is None and rec.get("agent_type"):
            self.agent_type = rec["agent_type"]
        # The session is keyed on the agent pid, and the FIRST record for it may
        # be one the collector saw without a resolvable user (a seeded process
        # predating the collector, or a root helper). Backfill from any later
        # record rather than leaving the row's owner blank -- "which session" is
        # not useful without "whose".
        # A uid with no passwd entry resolves to no `user`, and "uid 1234" is
        # still someone you can go and find -- so fall back rather than blank.
        ident = rec.get("user") or (
            "uid:%s" % rec["uid"] if rec.get("uid") is not None else None)
        if not self.user and ident:
            self.user = ident
        if self.host is None and rec.get("host"):
            self.host = rec["host"]
        if ident:
            self.users[ident] = self.users.get(ident, 0) + 1


def _rle_push(seq, times, tok, t, cap, owner):
    """Append one call to a run-length chain; True when the full chain drops it."""
    if seq and seq[-1][0] == tok:
        seq[-1][1] += 1
        return False
    if len(seq) >= cap:
        if owner is not None:
            owner.truncated = True
        return True
    seq.append([tok, 1])
    times.append(t)
    return False


def _bump(d, k):
    d = d if d is not None else {}
    d[k] = d.get(k, 0) + 1
    return d


def _own_surface(table, key, v):
    """Hold an agent root's [surface, detail, ppid, agent type]: the newest
    reading, with the first parent seen. A process its parent outlives is
    reparented to init, and the one that started it is the one that says."""
    cur = table.get(key)
    if cur is not None and cur[2] not in _NO_PARENT:
        v = [v[0], v[1], cur[2], v[3]]
    table[key] = v


def _tree(host, root, sid):
    """What a session key is evicted with (`_shed`): every key of its process
    tree, which holds every key a row folds (`_fold_all` resolves within it);
    a key with no tree is its own."""
    return sid if root is None else (host, root)


def _rle_merge(seq, times, over, bseq, btimes, bover, cap):
    """Push a LATER chain onto `seq` as one pass would have pushed its calls.

    Runs append while there is room, the first joining the last when they
    match. Past the cap one pass only ever extends the last run, with the calls
    of its own tool, and drops the rest; `b`'s runs past that point and the
    calls `b` itself dropped (`bover`) split the same way. That is exact
    because a full chain's last run absorbs every later call of its tool, and
    `bover` holds every call `b` dropped. Returns (over, dropped anything).
    """
    i, nb = 0, len(bseq)
    while i < nb:
        tok, n = bseq[i]
        if seq and seq[-1][0] == tok:
            seq[-1][1] += n
        elif len(seq) < cap:
            seq.append([tok, n])
            times.append(btimes[i] if i < len(btimes) else None)
        else:
            break
        i += 1
    dropped = False
    if i < nb or bover:
        last = seq[-1][0]
        for tok, n in list(bseq[i:]) + list((bover or {}).items()):
            if tok == last:
                seq[-1][1] += n
            else:
                over = over if over is not None else {}
                over[tok] = over.get(tok, 0) + n
                dropped = True
    return over, dropped


def _bound_hk(log, bin_cap):
    """Housekeeping held as one pass holds it: at most MAX_HK_SHAPES named
    shapes, the first seen, the rest under OTHER; at most `bin_cap` minutes, the
    oldest, the rest counted in `hk_cut`. A merge or a fold of two logs takes
    the union of theirs, which can hold more."""
    named = [k for k in log.hk if k != OTHER]
    if len(named) > MAX_HK_SHAPES:
        n = sum(log.hk.pop(k) for k in named[MAX_HK_SHAPES:])
        log.hk[OTHER] = log.hk.get(OTHER, 0) + n
    if len(log.hk_bins) > bin_cap:
        bins = {b: log.hk_bins[b] for b in sorted(log.hk_bins)[:bin_cap]}
        log.hk_cut += sum(log.hk_bins.values()) - sum(bins.values())
        log.hk_bins = bins


def _wrote(rec):
    """Whether a process wrote bytes: its disk or its write() figure."""
    io = rec.get("io")
    return isinstance(io, dict) and ((io.get("wr_mb") or 0) > 0 or (io.get("wchar_mb") or 0) > 0)


def _payload(argv, wrote):
    """(text, category, flags) of a command known by the argv a pending entry
    holds: what a sandbox wrapper ran, or what runs under a process that never
    exited. A shell's command string is read as shell text and any other
    program's argv as one command (`category_of_argv`), so a `|` in a regex is
    not a pipe; Codex's fs helper is an edit when it wrote bytes, and unknown
    when it did not."""
    if not argv:
        return None, "unknown", 0
    if is_fs_helper({"args": argv}):
        return argv.strip() or None, ("edit" if wrote else "unknown"), (WROTE if wrote else 0)
    text = (shell_text(None, argv) or "").strip() or argv.strip() or None
    return text, category_of_argv(None, argv), 0


def _rank(cat):
    return _CAT_RANK.get(cat, len(_CAT_RANK))


def _due(s, pid, end, start):
    """Note a process, begun at `start` (None if unknown), that ended at `end`
    under the call `pid` ran, which an earlier state holds: by pid, the latest
    end, for the merge that brings the call in. A session holds PAST_DUE_MAX
    pids; past it the one noted first is dropped. Returns how many were (0 or
    1), for the caller to count."""
    d = s.past_due
    if not isinstance(d, collections.OrderedDict):     # none yet, or a saved state's dict
        d = s.past_due = collections.OrderedDict(d or ())
    cur = d.get(pid)
    if cur is not None:
        if end > cur[0]:
            d[pid] = [end, start]
        return 0
    cut = 0
    if len(d) >= PAST_DUE_MAX:
        d.popitem(last=False)                       # the oldest noted, at once (6f03948)
        cut = 1
    d[pid] = [end, start]
    return cut


def _seen(entry):
    """What an open call was seen running (SEEN, R65): the programs its entry
    kept, by how often the call ran each itself, most first, a tie going to
    the first seen, up to SEEN_SHOWN. A login shell's startup helpers are left
    out, counted apart in the entry (P_HELP): they keep their place in its
    program list, which loop detection reads (R72). Claude's binary under its
    version name reads `claude` (`program_name`)."""
    helped = entry[P_HELP] or {}
    own = {}
    for c, st in entry[P_CMDS].items():
        n = st[0] - helped.get(c, 0)
        if c and n > 0:
            name = program_name(c)
            own[name] = own.get(name, 0) + n
    ran = sorted(own.items(), key=lambda kv: -kv[1])
    return tuple(redact(c)[:SEEN_NAME] for c, _n in ran)[:SEEN_SHOWN]


def _dense(entry, idle_s):
    """True when some command in a pending entry started twice or more at a
    mean gap of `idle_s` or less: what runs under it is busy enough that the
    stretch it spans is no idle gap. Two `git status` half an hour apart are
    not (R37), nor two different programs a helper ran once each."""
    return any(st[0] >= 2 and st[1] <= idle_s for st in entry[P_CMDS].values())


def _people_in(path):
    """The people whose home or lab-tree folders `path` runs through, by the
    patterns `anonymize` finds them with by default (HOME_PATTERNS): `jdoe`
    for `/n/netscratch/alpha_lab/Lab/jdoe`, and `alice` for
    `/home/alice/proj`."""
    out = set()
    for rx in _PERSON_FOLDERS:
        for m in rx.finditer(path):
            if m.groupdict().get("name"):
                out.add(m.group("name"))
    return out


def _named_for(folder, people):
    """True when the folder name `folder` is one of `people`, or has one as a
    part of it, split on `_`, `-`, and `.`: `netscratch_rroe` is named for
    rroe."""
    return folder in people or any(part in people for part in _NAME_PIECES.split(folder))


def _kin(note, start, end=None):
    """How a process that began at `start` stands to the process `note` remembers
    (`_went`), which had its parent's pid: "child" when it began while that one
    ran, within START_SLACK_S either side; "later" when it began after that one
    ended, so a later process has the pid; "earlier" when it began before that
    one did, so an earlier process had it. With no `start`, a note that ended
    before this process's `end` is an earlier process's, and this one's parent
    a later one ("later"). A note with no times is "child": nothing tells."""
    n_start, n_end = note[0], note[1]
    if start is None:
        return "later" if n_end is not None and end is not None and n_end < end else "child"
    if n_end is not None and start > n_end + START_SLACK_S:
        return "later"
    if n_start is not None and start < n_start - START_SLACK_S:
        return "earlier"
    return "child"


def _reprev(c, vmap):
    """Call `c` with its preview id renumbered through `vmap`."""
    return c if not c[PREV] else c[:PREV] + (vmap[c[PREV]],) + c[PREV + 1:]


def _retag(c, tmap):
    """Call `c` with its tag set id renumbered through `tmap`."""
    return c if not c[TAGS] else c[:TAGS] + (tmap[c[TAGS]],) + c[TAGS + 1:]


def _params(params):
    return PHASE_DEFAULTS if params is None else params


def merge_session(a, b, params=None):
    """Fold session `b`, LATER and with the same key, into `a`, as if `b`'s
    records had been fed to `a`. `b` is consumed. `params` are the phase
    thresholds (`TrajectoryReducer.phase_params`), for the call log's caps.
    Returns how many noted ends `a` dropped at PAST_DUE_MAX, for the caller
    to count."""
    p = _params(params)
    due_dropped = 0
    # A process `b` saw outlive a call `a` holds: its end is the call's `past`,
    # as one pass would have set it, before `b`'s calls can reuse the pid.
    if b.past_due:
        for pid, (end, start) in b.past_due.items():
            if not a.phase.ran_past(pid, end, start):
                due_dropped += _due(a, pid, end, start)    # an older state's: kept for its merge
    a.phase.merge(b.phase, p["max_calls"])
    _bound_hk(a.phase, p["hk_bin_cap"])
    a.n += b.n
    a.cpu += b.cpu
    a.wait += b.wait
    a.pids.extend(b.pids[:max(0, 8 - len(a.pids))])
    a.tools |= b.tools
    a.over, cut = _rle_merge(a.chain, a.chain_t, a.over, b.chain, b.chain_t, b.over,
                             MAX_SEQ_RUNS)
    a.truncated = a.truncated or cut
    a.pover, _ = _rle_merge(a.purpose, a.purpose_t, a.pover, b.purpose, b.purpose_t,
                            b.pover, MAX_SEQ_RUNS)
    if b.first is not None:
        a.first = b.first if a.first is None else min(a.first, b.first)
    if b.last is not None:
        a.last = b.last if a.last is None else max(a.last, b.last)
    a.rss = max(a.rss, b.rss)
    a.rd += b.rd
    a.wr += b.wr
    a.tx += b.tx
    a.rx += b.rx
    a.autonomous += b.autonomous
    a.no_ts = a.no_ts or b.no_ts
    # first seen wins, as in `Session.add`
    if a.sandbox == "unknown":
        a.sandbox = b.sandbox
    if a.approval == "unknown":
        a.approval = b.approval
    if not a.ancestry:
        a.ancestry = b.ancestry
    if a.agent_type is None:
        a.agent_type = b.agent_type
    if not a.user:
        a.user = b.user
    if a.host is None:
        a.host = b.host
    if a.tree_root is None:
        a.tree_root = b.tree_root
    for u, c in b.users.items():
        a.users[u] = a.users.get(u, 0) + c
    room = DRIFT_CAP - len(a.ev_p)
    if len(b.ev_p) > room or b.drift_cut:
        a.drift_cut = True
    if room > 0:
        a.ev_t.extend(b.ev_t[:room])
        a.ev_p.extend(b.ev_p[:room])
        a.ev_c.extend(b.ev_c[:room])
    return due_dropped


def _times(ts, n):
    """`n` run start times, to the millisecond; None for one never stamped.

    Padded rather than trusted to line up: a run and its time are appended
    together, but a session restored from an older save may hold none.
    """
    out = [None if t is None else round(t, 3) for t in list(ts or ())[:n]]
    return out + [None] * (n - len(out))


def _interleave(parts, attr, tattr, cap):
    """Merge several sessions' run-length chains by when each run began.

    Runs re-coalesce where neighbours now match. Order is by run START, so two
    keys running at once (parallel tool calls) interleave at run grain, not
    per call. Returns (chain, times, truncated).
    """
    runs = []
    for p in parts:
        for (tok, n), t in zip(getattr(p, attr), getattr(p, tattr)):
            runs.append((float("inf") if t is None else t, tok, n))
    runs.sort(key=lambda r: r[0])
    seq, times, cut = [], [], False
    for t, tok, n in runs:
        if seq and seq[-1][0] == tok:
            seq[-1][1] += n
        elif len(seq) >= cap:
            cut = True
        else:
            seq.append([tok, n])
            times.append(None if t == float("inf") else t)
    return seq, times, cut


def fold(parts, sid, kv, params=None):
    """One Session from the keys that resolve to the same agent.

    `parts[0]` is the owner's own key when it has one: its identity (user,
    agent type, sandbox verdict) wins, since the key process is the agent.
    `params` are the phase thresholds, for the call log's caps. The parts go
    on ingesting, so the folded call log is a new one.
    """
    p = _params(params)
    m = copy.copy(parts[0])
    m.sid, m.kv = sid, kv
    m.phase = PhaseLog.fold([q.phase for q in parts], p["max_calls"])
    _bound_hk(m.phase, p["hk_bin_cap"])
    m.past_due = None           # each part's own: the calls it names are in no other
    m.n = sum(p.n for p in parts)
    m.cpu = sum(p.cpu for p in parts)
    m.tools = set().union(*(p.tools for p in parts))
    m.chain, m.chain_t, cut = _interleave(parts, "chain", "chain_t", MAX_SEQ_RUNS)
    m.purpose, m.purpose_t, _ = _interleave(parts, "purpose", "purpose_t", MAX_SEQ_RUNS)
    m.truncated = cut or any(p.truncated for p in parts)
    firsts = [p.first for p in parts if p.first is not None]
    lasts = [p.last for p in parts if p.last is not None]
    m.first = min(firsts) if firsts else None
    m.last = max(lasts) if lasts else None
    m.rss = max(p.rss for p in parts)
    m.rd = sum(p.rd for p in parts)
    m.wr = sum(p.wr for p in parts)
    m.tx = sum(p.tx for p in parts)
    m.rx = sum(p.rx for p in parts)
    m.autonomous = sum(p.autonomous for p in parts)
    m.wait = sum(p.wait for p in parts)
    m.no_ts = any(p.no_ts for p in parts)
    by_time = sorted(parts, key=lambda p: p.first if p.first is not None else float("inf"))
    for attr, unknown in (("sandbox", "unknown"), ("approval", "unknown"),
                          ("ancestry", None), ("agent_type", None),
                          ("user", None), ("host", None), ("tree_root", None)):
        if getattr(m, attr) == unknown:
            v = next((getattr(p, attr) for p in by_time if getattr(p, attr) != unknown),
                     unknown)
            setattr(m, attr, v)
    m.users = {}
    for p in parts:
        for u, c in p.users.items():
            m.users[u] = m.users.get(u, 0) + c
    m.pids = [pid for p in parts for pid in p.pids][:8]
    ev = sorted(itertools.chain.from_iterable(zip(p.ev_t, p.ev_p, p.ev_c) for p in parts),
                key=lambda x: x[0])
    m.ev_t = array("d", (x[0] for x in ev))
    m.ev_p = array("H", (x[1] for x in ev))
    m.ev_c = array("I", (x[2] for x in ev))
    m.drift_cut = any(p.drift_cut for p in parts)
    return m


def jsd(a, b):
    """Jensen-Shannon divergence, base 2, between two count maps: 0 same, 1 disjoint."""
    na, nb = sum(a.values()), sum(b.values())
    out = 0.0
    for k in set(a) | set(b):
        p, q = a.get(k, 0) / na, b.get(k, 0) / nb
        m = (p + q) / 2
        if p:
            out += 0.5 * p * math.log2(p / m)
        if q:
            out += 0.5 * q * math.log2(q / m)
    return out


def under(path, anchor):
    """`path` is `anchor` or below it -- `/a/proj-old` is NOT under `/a/proj`."""
    return path == anchor or path.startswith(anchor.rstrip("/") + "/")


def _agent_key(s):
    """`(host, agent pid, user)` of a folded row keyed on an agent, else None: what the rows of one
    agent on different days have in common. A pid is reused, and a process keeps its user, so a
    row of another user is another agent's: on boslogin06 on 2026-10-08 and 09, 8 of the 74 agents
    with a row on each day were two users'."""
    if s.kt != "apid":
        return None
    try:
        return s.host, int(s.kv), s.user
    except (TypeError, ValueError):
        return None


def day_of(days, t):
    """The sid of the day among `days` (a block's, `[{sid, first, last}]` in time order) whose span
    from `first` to `last` holds the time `t`, else of the nearest, the earlier of two the same
    distance away."""
    best, near = None, math.inf
    for d in days:
        away = d["first"] - t if t < d["first"] else max(0.0, t - d["last"])
        if away < near:
            best, near = d["sid"], away
    return best


class TrajectoryReducer:
    # Exits are what a trajectory counts. The ticks are read for one thing: the
    # session root's own approval verdict, since a running agent leaves no exit
    # and its ticks are the only records of it (as reducers/sandbox.py).
    EVENTS = ("exit", "truncated", "residency", "conn", "netio")
    KEY = "trajectories"
    # Derived from the sessions and the index on demand; not worth saving. The
    # phase and task thresholds are the config's this reducer runs with, not a
    # saved one's.
    SNAPSHOT_SKIP = ("_fold_cache", "_group_cache", "phase_params", "task_params", "_phase_cache",
                     "_pend_idx", "_day_idx")

    def __init__(self, cfg, classes, window_s=None, procs=None):
        self.cfg = cfg
        # The window's ProcIndex: what each key's process is. Without one the
        # keys are ranked as the collector wrote them.
        self.procs = procs
        r = cfg.get("reducers.trajectories", {}) or {}
        self.top_n = r.get("top_n", 15)
        self.max_runs = r.get("max_runs_shown", 60)
        self.purpose_cap = r.get("purpose_seq_cap", 80)
        self.idle_evict = r.get("idle_evict_s", 7200)
        self.max_sessions = r.get("max_sessions", 150000)
        self.rankable = set(r.get("include_key_types", ["apid", "sess", "tty"]))
        self.sessions = {}
        # (host, agent pid) -> approval, read off the agent's OWN record: the
        # session's approval, whatever its tool calls' records say.
        self.owners = {}
        # (host, agent pid) -> [surface, detail, ppid, agent type], from the same
        # record, and from a host's (normalize.surface_of); the ppid is the
        # first one seen, before any reparenting to init
        self.surfaces = {}
        self.by_kt = collections.Counter()
        self.evicted = 0
        self.fed = 0
        # (state key, folded rows, row sid -> the raw sids folded into it)
        self._fold_cache = (None, None, None)
        # target sid -> (its parts and their counts, the folded session): a
        # live view re-folds every few seconds, and between two reads only
        # the sessions at work have changed
        self._group_cache = {}
        # Interned per window, so a session's drift arrays hold small ints.
        self._purpose_id, self._purposes = {}, []
        self._cwd_id, self._cwds = {None: 0}, [None]
        self.cwd_overflow = 0
        # (host, pid) -> [MB read, MB written] its exited children carried, for
        # its own exit to give back (`own_io`), and the keys touched since
        # `track` (for the hours' ageing).
        self.reaped = {}
        self.reaped_evicted = 0
        self._rtouched = None
        # Phases. The thresholds: the config's over phase_segment.DEFAULTS.
        self.phase_params = dict(PHASE_DEFAULTS, **(r.get("phases") or {}))
        # The tasks above them, likewise: the config's over phase_tasks.DEFAULTS.
        self.task_params = dict(TASK_DEFAULTS, **(r.get("tasks") or {}))
        # (host, pid) -> what ran under a process whose own record has not come
        # (phase_log.PendingJoin), carried across hours as `reaped` is.
        self.pend = PendingJoin(int(self.phase_params["pend_max"]),
                                idle_s=self.phase_params["idle_s"])
        # (host, pid) -> (start, end, where it went, what) for the processes
        # whose own record came most recently: a child that outlives its parent
        # goes where the parent went (`_late`, R57). Carried too, and bounded:
        # `exited_horizon` is the latest end among the notes dropped. An
        # OrderedDict, since it drops its oldest note on nearly every record: a
        # plain dict finds its first key past every slot deleted before it, 20 us
        # a note at 50,000 against 0.3 us, and boslogin06 notes 3.4 M a day.
        self.exited = collections.OrderedDict()
        self.exited_evicted = 0
        self.exited_horizon = 0.0
        self._xtouched = None
        self.late_routed = 0        # late children placed where their parent went
        self.late_unplaced = 0      # and those that could go nowhere: counted, not placed
        self.late_no_start = 0      # pushed under a gone parent unchecked: no start to check
        self.late_hops_out = 0      # pushed into the entry LATE_HOPS parents up, gone or not
        self.late_maybe_evicted = 0     # entries started under a parent whose note may be gone
        self.phase_untimed = 0      # under, or after, a process whose own record had no time
        # (session sid, pid) -> the end of the newest call that pid ran in that
        # session, for a process that outlives its call into a later hour;
        # carried too, and the keys touched since `track`.
        self.calls_at = collections.OrderedDict()
        self.calls_at_evicted = 0
        self._ctouched = None
        self.past_due_evicted = 0   # ends dropped at PAST_DUE_MAX (`_due`)
        # Masked command previews, interned per window as cwds are; 0 is none.
        self._previews, self._preview_id = [None], {None: 0}
        self.preview_overflow = 0
        # Tag sets, `(names, words)` as `phase_names.tagset` gives them, interned likewise; 0 is
        # the empty set.
        self._tagsets, self._tagset_id = [((), None)], {((), None): 0}
        self.tag_overflow = 0
        self.phase_reparented = 0   # records under no parent (ppid 0 or 1): not placed
        self.phase_unread = 0       # records a root held to its own exit: its housekeeping
        # The earliest day row's sid -> (key, phase block, each day row's own calls), for the day
        # rows of an agent the block joins (`phase_block`); the pending join by session: (the
        # entries it indexes, (records fed, entries), the index); and the folded rows by agent:
        # (the fold they are of, `_by_agent`)
        self._phase_cache = {}
        self._pend_idx = (None, None, None)
        self._day_idx = (None, None)
        # The window is the VIEW's, not a fixed config value: one reducer instance
        # exists per selected window, each ranking the sessions that window holds.
        self.window_s = window_s or int(cfg.get("live.window_min", 1440)) * 60

    def feed(self, rec):
        pid = rec.get("pid")
        role = rec.get("_role")
        if role in ("agent", "infra") and rec.get("_a3") == "agent" \
                and pid is not None and pid == rec.get("agent_pid"):
            if role == "agent":
                self.owners[(rec.get("host"), pid)] = rec.get("_approval") or "unknown"
            # a host is never a session (`rooted`), but a row keyed on it is
            # driven from where its argv says
            if role == "agent" or rec.get("_surface") is not None:
                _own_surface(self.surfaces, (rec.get("host"), pid),
                             [rec.get("_surface") or "unknown", rec.get("_surface_detail"),
                              rec.get("ppid"), rec.get("agent_type")])
        if rec.get("event") != "exit":
            return
        io = self.own_io(rec)
        sid = rec.get("_sid")
        if not sid:
            return
        now = time.time()
        s = self.sessions.get(sid)
        if s is None:
            if len(self.sessions) >= self.max_sessions:
                # this record is the newest, and so is its session
                self._shed(_tree(rec.get("host"), rec.get("tree_root_pid"), sid))
            s = self.sessions[sid] = Session(rec)
            self.by_kt[rec["_kt"]] += 1
        s.add(rec, now, io)
        self.fed += 1
        self._track(s, rec)
        if s.cls == "agent":
            self._phase(s, rec)

    # ---------------------------------------------------------------- phases
    def _phase(self, s, rec):
        """One exit of an agent session, read bottom up (the module docstring).

        A direct child of the agent is a call or the agent's own chore, and
        takes what ran under it. Anything else joins its parent's pending
        entry, unless it outlived a call it belongs to. Every path notes where
        the process went (`_went`), for a child recorded after it. Times are
        never None in the log or the join: a record with none is left out
        here, and `Session.add` has counted it (`no_ts`); what ran under it,
        and a child recorded after it, are counted (`phase_untimed`), since an
        entry left for it would wait for a record that has come.
        """
        end = rec.get("_ts_epoch")
        host, pid = rec.get("host"), rec.get("pid")
        if end is None:
            gone = self.pend.pop(host, pid, None) if pid is not None else None
            if gone is not None:
                self.phase_untimed += gone[P_N]
            self._went(host, pid, None, None, "untimed", None)
            return
        d = rec.get("duration_s")
        start = end - d if isinstance(d, (int, float)) and d >= 0 else None
        t0 = end if start is None else start
        role = rec.get("_role")
        # A root: its own agent_pid. One that tops its tree: through schema 8
        # every root does, while through schema 7 every tagged process is its
        # own agent_pid, and only its tree's root is the top.
        root = pid is not None and pid == rec.get("agent_pid")
        tops = root and ((rec.get("_sv") or 0) >= 8 or rec.get("tree_root_pid") == pid)
        # A tool call that outlived its agent: what ran under it was read as a
        # call still running, and its own record ends that call, as a direct
        # child's would.
        orphan = tops and role in _TOOL_CALL_ROLES
        if root and not orphan and (role == "agent" or tops):
            # The runtime, or a root that is no tool call (a daemon, a server, a
            # probe), ends at its own record (R36): it is never a call, never
            # pushed up, and what waited under it is the agent's housekeeping.
            # A probe or a helper inside a tool shell is no top, and is pushed.
            gone = self.pend.pop(host, pid, start)
            if gone is not None:
                self._held(s, gone)
            self._went(host, pid, start, end, "root", None)
            return
        kids = self.pend.pop(host, pid, start) if pid is not None else None
        # Schema 7 tags a sandbox wrapper `bwrap`: the session's own type is
        # asked when the record's is not one whose calls are recognized.
        at = rec.get("agent_type")
        at = at if at in PHASE_AGENTS else s.agent_type
        if orphan or parent_is_agent(rec, self.procs):
            kind, what = classify_direct(rec, at)
            if kind == "call" and what == "wrapper" and kids and is_sandbox_probe(kids[P_TEXT]):
                self._housekeeping(s, "sandbox probe", t0, 1 + kids[P_N])
                self._went(host, pid, start, end, "hk", "sandbox probe")
            elif kind == "call":
                self._call(s, rec, what, t0, end, kids)
                self._went(host, pid, start, end, "call", pid)
            elif kind == "housekeeping":
                self._housekeeping(s, what, t0, 1 + (kids[P_N] if kids else 0))
                self._went(host, pid, start, end, "hk", what)
            else:                           # an agent whose calls are not read
                self._went(host, pid, start, end, "none", None)
            return
        ppid = rec.get("ppid")
        if ppid in _NO_PARENT:
            self.phase_reparented += 1 + (kids[P_N] if kids else 0)
            self._went(host, pid, start, end, "lost", None)
            return
        if self._ran_past(s, ppid, end, start):
            self._went(host, pid, start, end, "call", ppid)
            return
        item = self._leaf(rec, t0, end, s.sid)
        if kids is not None:
            fold_entry(item, kids)          # its own argv first: a wrapper has none
        if self._late(s, host, pid, ppid, start, end, t0, item):
            return
        self.pend.push(host, ppid, item)
        self._went(host, pid, start, end, "pend", ppid)

    def _went(self, host, pid, start, end, where, what):
        """Note where a process that ran from `start` to `end` went when its own
        record came: into its parent's pending entry ("pend", the parent's
        pid), a call ("call", its pid), the agent's housekeeping ("hk", the
        shape), what a root held ("root"), or nowhere: its parent was gone
        ("lost"), it was placed nowhere ("none"), or its record had no time
        ("untimed", with no times). At most EXITED_MAX are held, the one noted
        first dropped and counted, and the latest end dropped kept
        (`exited_horizon`)."""
        if pid is None:
            return
        key = (host, pid)
        if self.exited.pop(key, None) is None and len(self.exited) >= EXITED_MAX:
            old = self.exited.popitem(last=False)[1]
            self.exited_evicted += 1
            if old[1] is not None and old[1] > self.exited_horizon:
                self.exited_horizon = old[1]
        self.exited[key] = (start, end, where, what)
        if self._xtouched is not None:
            self._xtouched.add(key)

    def _late(self, s, host, pid, ppid, start, end, t0, item):
        """Place `item`, a process whose parent's own record already came by,
        where the parent went (R57), and return True; False when it is pushed
        under its parent after all.

        A child that outlives a parent that is no call (a pipeline's stage that
        ends after its shell) would start an entry nobody pops. It goes to the
        entry the parent joined, or on up while each parent there has exited
        too; to the call the parent was part of, as having run past it; or to
        housekeeping, under the parent's shape or as what a root held. It is
        placed through its own session, as `_ran_past` places any child, so the
        hours place it as one pass does.

        A pid is reused, so a note is the parent's only when the child began
        while the noted process ran (`_kin`): no more than START_SLACK_S before
        it began, as `PendingJoin.pop` refuses an entry, nor after it ended. A
        child that began after it is a later process's, still running, and is
        pushed under it. One that began before it is an earlier process's,
        gone, whose note a later one replaced: it goes nowhere. Each step up
        the parents asks the same of the parent noted, against its child's
        start, and stops at LATE_HOPS.

        Counted, never placed quietly: a child that goes nowhere
        (`late_unplaced`: its parent's call is another session's, its parent
        is gone with its note replaced, or its agent's calls are not read), one
        whose parent went nowhere (`phase_reparented`), and one whose parent's
        record had no time (`phase_untimed`), each once. Pushed, and counted:
        one with no start, which cannot be checked (`late_no_start`); one past
        LATE_HOPS parents (`late_hops_out`). And one that starts an entry under
        a parent with neither a note nor an entry, begun before the latest note
        dropped ended: its parent may have exited with its note dropped, and the
        entry never pop (`late_maybe_evicted`, entries, so at most). One that
        joins an entry is not: its parent's exit would have taken the entry. On
        boslogin06 on 2026-10-28, 455 children started such an entry and 161
        joined one; the 616 carried 583,941 records folded in under them, the
        loops of subshells that ran for hours."""
        n = item[P_N]
        hop = self.exited.get((host, ppid))
        if hop is None:
            if start is not None and start <= self.exited_horizon + START_SLACK_S \
                    and (host, ppid) not in self.pend.entries:
                self.late_maybe_evicted += 1
            return False                            # its parent still runs: its entry pops
        if start is None:
            self.late_no_start += n
            return False
        kin = _kin(hop, start)
        if kin == "later":
            return False                            # a later process has the pid, and runs
        steps = 0
        while kin == "child" and hop[2] == "pend":
            nxt = self.exited.get((host, hop[3]))
            if nxt is None:
                break                               # that parent still runs: its entry pops
            k = _kin(nxt, hop[0], hop[1])
            if k == "later":
                break                               # an earlier process's note: the parent runs
            if k == "earlier":
                kin = k
                break
            if steps == LATE_HOPS:                  # that parent has exited too: the entry may
                self.pend.push(host, hop[3], item)  # never pop, and the child is counted
                self._went(host, pid, start, end, "pend", hop[3])
                self.late_hops_out += n
                return True
            hop, steps = nxt, steps + 1
        where, what = hop[2], hop[3]
        if kin == "earlier" or where == "none" or (
                where == "call" and not self._ran_past(s, what, max(end, item[P_LAST]), start)):
            self.late_unplaced += n                 # gone with its note replaced, or no place
            self._went(host, pid, start, end, "none", None)
            return True
        if where == "lost":
            self.phase_reparented += n
            self._went(host, pid, start, end, "lost", None)
            return True
        if where == "untimed":
            self.phase_untimed += n
            self._went(host, pid, start, end, "untimed", None)
            return True
        if where == "pend":
            self.pend.push(host, what, item)
        elif where == "hk":
            self._housekeeping(s, what, t0, n)
        elif where == "root":
            self._by_program(s, item)
        self._went(host, pid, start, end, where, what)
        self.late_routed += n
        return True

    def _call(self, s, rec, kind, t0, end, kids):
        """Hold a direct child's tool call. A shell's command is shell text; an
        argv (Claude's Grep, a bare Codex command) is one command; a wrapper's
        call is the command it ran, from what ran under it. Its text is masked
        once, and that is what its preview is cut from and what its names and
        the agent's own words are read from (`tagset`)."""
        flags = 0
        if kind == "wrapper":
            text, cat, flags = _payload(kids[P_TEXT] if kids else None,
                                        bool(kids and kids[P_WROTE]))
        elif kind in ("bash", "shell"):
            text = call_text(rec, kind)
            cat = category_of(text)
        else:
            text = call_text(rec, kind)
            if is_fs_helper(rec):
                wrote = _wrote(rec) or bool(kids and kids[P_WROTE])
                cat, flags = ("edit", WROTE) if wrote else ("unknown", 0)
            else:
                command, args, _ = _argv(rec)
                cat = category_of_argv(command, args)
        pid = rec.get("pid")
        masked = redact(text)
        s.phase.add((t0, end, cat, norm_hash(text), self._intern_preview(masked), flags, pid, None,
                     self._intern_tagset(tagset(text, masked))),
                    self.phase_params["max_calls"])
        if pid is not None:
            key = (s.sid, pid)
            # moved to the end, so the cap drops the call longest past
            if self.calls_at.pop(key, None) is None and len(self.calls_at) >= CALLS_AT_MAX:
                self.calls_at.popitem(last=False)   # the oldest, at once (6f03948)
                self.calls_at_evicted += 1
            self.calls_at[key] = end
            if self._ctouched is not None:
                self._ctouched.add(key)

    def _housekeeping(self, s, shape, t, n):
        """Count `n` of the agent's own commands; past MAX_HK_SHAPES named
        shapes, under OTHER (a Claude chore is named by its program)."""
        hk = s.phase.hk
        if shape not in hk and shape != OTHER and len(hk) - (OTHER in hk) >= MAX_HK_SHAPES:
            shape = OTHER
        s.phase.add_housekeeping(shape, t, self.phase_params["hk_bin_cap"], n)

    def _held(self, s, entry):
        """Count what a root held to its own exit as its housekeeping
        (`_by_program`)."""
        self.phase_unread += entry[P_N]
        self._by_program(s, entry)

    def _by_program(self, s, entry):
        """Count an entry as housekeeping: by the programs it kept (P_CMDS),
        the rest under OTHER, and in no minute, since an entry keeps none of
        them."""
        left = entry[P_N]
        for cmd, st in entry[P_CMDS].items():
            n = min(st[0], left)
            if n > 0:
                self._housekeeping(s, cmd, None, n)
                left -= n
        if left > 0:
            self._housekeeping(s, OTHER, None, left)

    def _ran_past(self, s, ppid, end, start):
        """True when the parent `ppid` ran a call of this session and this
        process can be its child: it is part of the call, and outlived it. The
        session's own log decides when it holds the pid (`PhaseLog.ran_past`).
        A call an earlier hour holds is known by `calls_at`, as the one pass
        over both hours would have known it, and the end waits in `past_due`
        for the merge."""
        log = s.phase
        if ppid in log.by_pid:
            return log.ran_past(ppid, end, start)
        key = (s.sid, ppid)
        e = self.calls_at.get(key)
        if e is None or (start is not None and start > e + START_SLACK_S):
            return False
        if self._ctouched is not None:
            self._ctouched.add(key)
        if end > e + PAST_SLACK_S:
            self.past_due_evicted += _due(s, ppid, end, start)
        return True

    @staticmethod
    def _leaf(rec, t0, end, sid):
        """The pending entry for one process: its argv, cut to RAW_KEEP, and
        what it is as a command; a sandbox wrapper has none of its own. Its
        program goes in the entry's program list by its basename, as loop
        detection reads it, a login shell's startup helper too, which is
        counted apart (`login_helper`, P_HELP) for `seen` to leave out (R72)."""
        command, args, argv0 = _argv(rec)
        wrote = False
        if _is_sandbox(command, argv0):
            text, cat = None, "unknown"
        elif is_fs_helper(rec):
            wrote = _wrote(rec)
            text, cat = args[:RAW_KEEP].strip() or None, ("edit" if wrote else "unknown")
        else:
            text, cat = args[:RAW_KEEP].strip() or None, category_of_argv(command, args)
        return PendingJoin.leaf(t0, end, cat, os.path.basename(command) or None, text, wrote, sid,
                                helper=login_helper(command, args))

    def _intern_preview(self, masked, count=True):
        """The id of the preview of `masked`, a command's text already masked
        (`redact`), 0 for none: cut, as the inspector shows a command
        (`proctree.shown`). Past MAX_PREVIEWS a call has none, counted unless
        `count` is False (a read, not a record)."""
        if not masked:
            return 0
        shown = masked
        if len(shown) > PREVIEW:
            shown = shown[:PREVIEW - 1] + "…"
        return self._intern_shown(shown, count)

    def _intern_shown(self, shown, count=True):
        i = self._preview_id.get(shown)
        if i is None:
            if len(self._previews) >= MAX_PREVIEWS:
                if count:
                    self.preview_overflow += 1
                return 0
            i = self._preview_id[shown] = len(self._previews)
            self._previews.append(shown)
        return i

    def _intern_tagset(self, ts, count=True):
        """The id of the tag set `ts`, `(names, words)` as `phase_names.tagset`
        gives it from masked text; 0 is the empty set, which is always held.
        Past MAX_TAGSETS a call has the empty set, counted unless `count` is
        False (a read, not a record)."""
        i = self._tagset_id.get(ts)
        if i is None:
            if len(self._tagsets) >= MAX_TAGSETS:
                if count:
                    self.tag_overflow += 1
                return 0
            i = self._tagset_id[ts] = len(self._tagsets)
            self._tagsets.append(ts)
        return i

    def _pending_of(self, members):
        """`self.pend.for_sids(set(members))`, in the join's own order, from an
        index of its entries by session: a result reads it for every row, and
        it holds up to `pend_max`. Rebuilt whenever a record may have changed it."""
        entries = self.pend.entries
        held = self._pend_idx
        if held[0] is not entries or held[1] != (self.fed, len(entries)):
            idx = {}
            for i, e in enumerate(entries.values()):
                idx.setdefault(e[P_SID], []).append((i, e))
            held = self._pend_idx = (entries, (self.fed, len(entries)), idx)
        idx = held[2]
        got = sorted(itertools.chain.from_iterable(idx.get(m, ()) for m in dict.fromkeys(members)),
                     key=itemgetter(0))
        return [e for _, e in got]

    def phase_block(self, s, members):
        """The row's phases (`phase_segment.segment`) and the tasks above them
        (`phase_tasks`) from the calls its log holds and the calls still
        running under its collector keys, `members`, and those of the same
        agent's rows on other days (below); `{unavailable}` for a row whose
        calls are not recognized.

        The calls are cut into tasks first, the phases are cut at the tasks'
        boundaries, and the tasks are then named and each phase marked with
        its task: the block gains `tasks` and a `headline`. One `step_s`, the
        phases', goes to all three, since a task cut by one step and named by
        another would differ silently. The log's `cut_at` says which tasks
        are incomplete (`PhaseLog`), and `project_of` gives the project they
        are named for. The row's user goes to the naming too, masked as the
        names are: a name that is the user names no task.

        One session across midnight. A row is one local day, and an agent
        that works past midnight has a row for each. Every one of them serves
        the same block, of the whole session, so that midnight cuts no task or
        phase. The rows joined are those of the current fold of the same agent
        (`_agent_key`: host, agent pid, and user, as `agent_sessions` groups
        them), keyed on the agent (`apid`), with a time, and whose calls are
        read. A row the window evicted is not held, and so is not joined.
        Their call logs are folded
        (`PhaseLog.fold`: the calls in order of start, the first `max_calls`
        held, the rest counted as `cut`, from `cut_at`), and the pending
        entries of all their collector keys are read with them, as of the
        session's last record, so a call is where it began, whichever row its
        record is in. The block's `n_calls` is the session's, and a row's own,
        the top-level one, stays its day's. The project is read once for the
        session, from the launch folder of the earliest row and the `cd` names
        of every call. `days` lists the rows, `[{sid, first, last}]` in time
        order, and each phase and task has the `sid` of the day whose span from
        `first` to `last` holds its `t0`, else of the nearest, the earlier of
        two the same distance away (`day_of`). A row of one day has no `days`,
        and `sid` is None.

        A pending entry is a call still running when it is a steady loop
        (`PendingJoin.loop_like`), or when it was busy within `idle_s` of the
        session's last record and dense (`_dense`, R37), and either way when
        its stretch, the time since its children last left a gap of `idle_s`,
        is its whole life or has itself run `idle_s`, and lasts a step
        (`step_s`) at least (R57). The call runs over that stretch only: a
        helper that never exits, or runs a burst an hour, would otherwise span
        the gaps between its children and hide the idle between them, and its
        last burst, or a burst of subshells within one second, is no call
        still running. The cost: a call whose children paused for `idle_s`
        and went on is open over its last stretch only, and the busy stretch
        before the pause is no part of it. Anything else under a process that
        never exited is counted as housekeeping, never as a call, and never in
        the log.

        Cached under the earliest row's sid, per every row it joins: its sid,
        records, calls, keys, and the pending entries filed under the keys
        (how many, their records, their latest end). It is shared, read and
        never changed, so its `sid`s and `days` are set before it is kept. An
        entry is filed under its newest child's session (R59), so a loop past
        midnight leaves yesterday's row without a record of that row's: the
        entries are in the key so that row is read again. The tasks add one
        thing to it. Each call carries its names and words (`TAGS`), so they
        are read again with the calls. The project is read from the agent's
        own cwd too (`anchor_of`), which a record of the agent's can give
        after its session's.
        """
        return self._phases(s, members)[0]

    def _phases(self, s, members):
        """`(block, n_calls)`: `phase_block`, and the calls of row `s` alone,
        which `_row` serves as the row's own. `n_calls` is None for a row whose
        calls are not recognized, and the block's own for a row of one day. The
        counts of the rows of a block add up to the block's."""
        if s.cls != "agent" or s.agent_type not in PHASE_AGENTS:
            return {"unavailable": UNAVAILABLE}, None
        parts = [(r, m, self._pending_of(m)) for r, m in self._day_rows(s, members)]
        key = (tuple((r.sid, r.n, len(r.phase.calls), len(m), len(e), sum(x[P_N] for x in e),
                      max((x[P_LAST] for x in e), default=None))
                     for r, m, e in parts),
               self.anchor_of(parts[0][0]))
        at = parts[0][0].sid
        hit = self._phase_cache.get(at)
        if hit is None or hit[0] != key:
            if len(self._phase_cache) >= PHASE_CACHE_MAX:
                self._phase_cache.clear()
            hit = self._phase_cache[at] = (key, *self._build_block(parts))
        return hit[1], hit[2][s.sid]

    def _day_rows(self, s, members):
        """`[(row, members)]`, the day rows whose calls make one block with row
        `s` (`phase_block`), in time order, `s` among them with its `members`.
        The others are the current fold's rows of the same agent
        (`_agent_key`) that have a time and whose calls are read. The fold is
        made if it is stale (`folded`), as `result` makes it before it reads a
        row. `s` alone when it is not keyed on an agent or has no time."""
        rows = [(s, members)]
        key = _agent_key(s)
        if key is not None and s.first is not None:
            others = self._agent_days().get(key, ())
            held = self._fold_cache[2]      # after the line above, which makes the fold if need be
            rows += [(r, held.get(r.sid) or [r.sid]) for r, _via, _k in others
                     if r.sid != s.sid and r.first is not None and r.cls == "agent"
                     and r.agent_type in PHASE_AGENTS]
            rows.sort(key=lambda row: (row[0].first, row[0].sid))
        return rows

    def _build_block(self, parts):
        """`(block, own)` of the day rows `parts`, `[(row, members, pending)]` in
        time order (`phase_block`): the block, and by each row's sid how many
        calls are its own. The calls are those of the rows' logs, the log of a
        row of one day itself and of more a fold of them, and the open ones
        among the pending entries, each read as of the session's last record.
        Nothing is changed but what the block holds."""
        first = parts[0][0]
        p = self.phase_params
        if len(parts) == 1:
            log, last = first.phase, first.last
        else:
            log = PhaseLog.fold([r.phase for r, _m, _e in parts], p["max_calls"])
            _bound_hk(log, p["hk_bin_cap"])
            last = max(r.last for r, _m, _e in parts)
        calls, extra, own = list(log.calls), 0, {}
        for r, _m, entries in parts:
            opened, hidden = self._open_calls(first.agent_type, last, entries)
            calls += opened
            extra += hidden
            own[r.sid] = r.phase.n + len(opened)
        step_s = p["step_s"]
        prep = prepared(calls, self._previews, p)
        spans = task_spans(prep, self._tagsets, self.task_params, step_s)
        block = segment(calls, log, self._previews, p, guess=True,
                        extra_hk={NEVER_EXITED: extra} if extra else None, prepared=prep,
                        cuts=[x.t0 for x in spans[1:]])
        block["tasks"], block["headline"] = name_tasks(
            spans, block, prep, self._tagsets, self._previews, self.task_params,
            self.project_of(first, prep[0]), log.cut_at, step_s,
            user=redact(first.user) if first.user else None)
        if len(parts) == 1:
            for ph in block["list"]:
                ph["sid"] = None
        else:
            days = block["days"] = [
                {"sid": r.sid, "first": _times([r.first], 1)[0], "last": _times([r.last], 1)[0]}
                for r, _m, _e in parts]
            for item in itertools.chain(block["list"], block["tasks"]):
                item["sid"] = day_of(days, item["t0"])
        return block, own

    def project_of(self, s, calls):
        """The project a session's tasks and headline are named for, masked, or
        None. It is the name of the folder the agent was launched in (the
        basename of its `cwd_anchor`), unless that folder is a home directory
        (`_HOME_DIR`) or is named for a person: the user, or one whose home or
        lab-tree folder its path runs through (`_people_in`), as the whole name
        or a part of it (`_named_for`). A person is no project: on boslogin06 on
        2026-10-09 tpoe's agent was launched in another person's folder,
        `/n/netscratch/alpha_lab/Lab/jdoe`, and rroe's in
        `…/Lab/netscratch_rroe`. Then it is the folder its commands `cd`
        into most, the earliest of those that tie, and not one named for those
        people either. With neither it is None: the cwd was `/`, a person's own
        folder, or not recorded, and no call changed into another folder. The
        launch folder comes first because a `cd` folder is often a subfolder of
        the project, and the `cd` folder is only the fallback because a launch
        folder is often a home folder, named for the user. `calls` are the
        session's, sorted by start."""
        anchor, _ = self.cwd_anchor(s, self.anchor_of(s))
        folder = anchor.rstrip("/") if anchor else ""
        name = os.path.basename(folder)
        people = _people_in(folder) | ({s.user} if s.user else set())
        if _named_for(name, people) or _HOME_DIR.fullmatch(folder):
            name = ""
        if not name:
            folders = collections.Counter(
                text for c in calls for kind, text in self._tagsets[c[TAGS]][0]
                if kind == "cd" and not _named_for(text, people))
            name = folders.most_common(1)[0][0] if folders else ""
        return redact(name) or None

    def phase_calls(self, s, members, pending=None):
        """`(calls, extra)` of row `s` alone: the calls its log holds and,
        after them, the open ones under its collector keys `members` (their
        pending entries, `pending` when the caller has them), flagged OPEN; and
        how many commands ran under a process that never exited and are no
        call. The phases of a row of one day are cut from these
        (`phase_block`). A row joined with its agent's rows on other days is cut
        from every day row's calls instead: their logs folded, and their open
        calls read as of the session's last record (`_build_block`). A new
        list, which the caller may keep.
        An open call runs over its entry's last stretch (P_SINCE to P_LAST),
        and carries the programs its entry saw (SEEN, R65): its own command
        line is not recorded until it exits, and its first child's text, which
        it carries as its preview and reads its tag set from, is no command of
        its own. Both ids are interned as a read, not counted when there is no
        room for them.

        In a Codex row, an entry whose own command is one of Codex's chores
        (`phase_rules.codex_chore_of`) is no call either: on boslogin06 on
        2026-10-28 the hourly update check (`sh -s`) ran under a process that
        never exited in three sessions and a probe, and was read as a call
        running for up to 23 hours (1447670) when nothing the model asked for
        ran at all."""
        opened, extra = self._open_calls(
            s.agent_type, s.last, self._pending_of(members) if pending is None else pending)
        return list(s.phase.calls) + opened, extra

    def _open_calls(self, agent_type, last, pending):
        """`(opened, extra)`, as `phase_calls` reads the pending entries `pending`
        of a session of `agent_type` whose last record is at `last`: the calls
        still running, and how many commands are no call."""
        p = self.phase_params
        opened, extra = [], 0
        codex = agent_type == "codex"
        for e in pending:
            if codex and codex_chore_of(e[P_TEXT]):
                extra += e[P_N]
                continue
            # One stretch: its children have been busy since its first, or for `idle_s` at least.
            # A helper's burst is neither, nor a timer's short child after a gap of `idle_s`; and a
            # stretch within one step (`step_s`, the stamps' own noise) is a moment, no call running.
            # The cost: a tool shell whose children paused for `idle_s` and went on loses its busy
            # stretch before the pause, which is no part of the call.
            span = e[P_LAST] - e[P_SINCE]
            steady = span >= p["step_s"] and (e[P_SINCE] <= e[P_FIRST] or span >= p["idle_s"])
            loop = PendingJoin.loop_like(e, p["poll_min"], p["poll_cv"])
            busy = (last is not None and last - e[P_LAST] <= p["idle_s"]
                    and e[P_SINCE_N] >= 2 and _dense(e, p["idle_s"]))
            if steady and (loop or busy):
                text, _cat, _flags = _payload(e[P_TEXT], e[P_WROTE])
                cat = "wait" if loop else min(e[P_CATS] or ("unknown",), key=_rank)
                seen = _seen(e)
                masked = redact(text)
                opened.append((e[P_SINCE], e[P_LAST], cat, norm_hash(text),
                               self._intern_preview(masked, count=False), OPEN, None, None,
                               self._intern_tagset(tagset(text, masked), count=False), seen))
            else:
                extra += e[P_N]
        return opened, extra

    def own_io(self, rec):
        """This process's disk MB (read, written), without its reaped children's.

        A child exits before the parent that reaps it, so by the parent's exit
        every child it reaped has handed its whole figure -- its own and what
        it reaped in turn -- to `reaped`. A child its parent never waited for
        exits after it, and is never taken off. Only `scope: process` figures
        include children; a `leader` one is the leader thread's alone.
        """
        io = rec.get("io")
        if not isinstance(io, dict):
            return 0.0, 0.0
        rd, wr = io.get("rd_mb") or 0.0, io.get("wr_mb") or 0.0
        if io.get("scope") != "process":
            return rd, wr
        host = rec.get("host")
        kids = self.reaped.pop((host, rec.get("pid")), None)
        ppid = rec.get("ppid")
        if ppid is not None and (rd or wr):
            key = (host, ppid)
            acc = self.reaped.get(key)
            if acc is None:
                if len(self.reaped) >= REAPED_MAX:
                    self.reaped.pop(next(iter(self.reaped)))
                    self.reaped_evicted += 1
                acc = self.reaped[key] = [0.0, 0.0]
            acc[0] += rd
            acc[1] += wr
            if self._rtouched is not None:
                self._rtouched.add(key)
        if kids:
            rd, wr = max(0.0, rd - kids[0]), max(0.0, wr - kids[1])
        return rd, wr

    def _track(self, s, rec):
        if len(s.ev_p) >= DRIFT_CAP:
            s.drift_cut = True
            return
        e = rec.get("_ts_epoch")
        s.ev_t.append(e if e is not None else (s.ev_t[-1] if s.ev_t else 0.0))
        p = rec.get("_purpose") or "other"
        pid = self._purpose_id.get(p)
        if pid is None:
            pid = self._purpose_id[p] = len(self._purposes)
            self._purposes.append(p)
        s.ev_p.append(pid)
        c = rec.get("cwd") or None
        cid = self._cwd_id.get(c)
        if cid is None:
            if len(self._cwds) >= MAX_CWDS:
                self.cwd_overflow += 1          # reported in `fold`, read as unmeasured
                cid = 0
            else:
                cid = self._cwd_id[c] = len(self._cwds)
                self._cwds.append(c)
        s.ev_c.append(cid)

    # ----------------------------------------------------------------- merge
    # A window is merged from saved hour states (`hours`). What crosses an
    # hour is three joins: `reaped` (a shell that exits in the next hour gives
    # back the I/O of the children it reaped in this one), the phases' pending
    # entries (what ran under a process whose own record comes in a later
    # hour), and `calls_at` (a call a later hour's process outlived). So they
    # are CARRIED into the next hour's reduction, and are not part of the saved
    # hour.
    CARRY_AGED = ("reaped", "phase_pend", "phase_calls", "phase_exited")

    def track(self):
        """Note every carried key touched from here on, for the ageing."""
        self._rtouched = set()
        self.pend.touched = set()
        self._ctouched = set()
        self._xtouched = set()

    def carry(self):
        return {"reaped": self.reaped, "phase_pend": self.pend.entries,
                "phase_calls": self.calls_at, "phase_exited": self.exited,
                "phase_exited_horizon": self.exited_horizon,
                "touched": {"reaped": self._rtouched or (),
                            "phase_pend": self.pend.touched or (),
                            "phase_calls": self._ctouched or (),
                            "phase_exited": self._xtouched or ()}}

    def seed(self, c):
        self.reaped = c["reaped"]
        x = c.get("phase_calls")
        self.calls_at = (x if isinstance(x, collections.OrderedDict)
                         else collections.OrderedDict(x or ()))
        x = c.get("phase_pend")
        self.pend.entries = x if isinstance(x, collections.OrderedDict) else collections.OrderedDict(x or ())
        x = c.get("phase_exited")
        self.exited = x if isinstance(x, collections.OrderedDict) else collections.OrderedDict(x or ())
        self.exited_horizon = c.get("phase_exited_horizon", 0.0)

    def strip(self):
        self.reaped = {}
        self._rtouched = None
        self.pend.entries = collections.OrderedDict()
        self.pend.touched = None
        self.calls_at = collections.OrderedDict()
        self._ctouched = None
        self.exited = collections.OrderedDict()
        self.exited_horizon = 0.0
        self._xtouched = None

    def merge(self, o):
        """Fold a LATER state into this one. `o` is consumed.

        Sessions merge key by key (`merge_session`): a session spanning two
        hours is one session, its counts added and its chains joined as one
        pass would have built them. The joins across hours (`reaped`, the
        pending entries, `calls_at`) are carried instead (above). The purpose,
        cwd, preview, and tag set ids are each state's own, and are renumbered
        into this one's.
        """
        pmap = array("H", (self._intern_purpose(p) for p in o._purposes))
        cmap = array("I", (self._intern_cwd(c) for c in o._cwds))
        # a cwd the window could no longer intern is counted per process, as
        # `_track` counts it
        lost = {i for i, c in enumerate(cmap) if i and not c}
        # a preview likewise, per call, as `_intern_preview` counts it
        vmap = [0] + [self._intern_shown(t, False) for t in o._previews[1:]]
        vsame = all(i == v for i, v in enumerate(vmap))
        vlost = {i for i, v in enumerate(vmap) if i and not v}
        # and a tag set, as `_intern_tagset` counts it
        tmap = [0] + [self._intern_tagset(t, False) for t in o._tagsets[1:]]
        tsame = all(i == v for i, v in enumerate(tmap))
        tlost = {i for i, v in enumerate(tmap) if i and not v}
        for sid, s in o.sessions.items():
            s.ev_p = array("H", (pmap[i] for i in s.ev_p))
            if lost:
                self.cwd_overflow += sum(1 for i in s.ev_c if i in lost)
            s.ev_c = array("I", (cmap[i] for i in s.ev_c))
            if not (vsame and tsame):
                if vlost:
                    self.preview_overflow += sum(1 for c in s.phase.calls if c[PREV] in vlost)
                if tlost:
                    self.tag_overflow += sum(1 for c in s.phase.calls if c[TAGS] in tlost)
                s.phase.calls = [_retag(_reprev(c, vmap), tmap) for c in s.phase.calls]
            cur = self.sessions.get(sid)
            if cur is None:
                self.sessions[sid] = s
                self.by_kt[s.kt] += 1
            else:
                self.past_due_evicted += merge_session(cur, s, self.phase_params)
        # `o` counted a session it opened for each key; one already open here
        # was counted above, and `o`'s evicted sessions are its own to count
        held = collections.Counter(s.kt for s in o.sessions.values())
        for kt, n in o.by_kt.items():
            self.by_kt[kt] += n - held.get(kt, 0)
        self.owners.update(o.owners)    # the newest record of a root wins, as in `feed`
        for key, v in o.surfaces.items():
            _own_surface(self.surfaces, key, v)
        # The cap, once `o` is in: a session `o` goes on with is as recent as
        # `o`, and the sessions held are only as idle as their newest records.
        if len(self.sessions) > self.max_sessions:
            self._shed()
        self.evicted += o.evicted
        self.fed += o.fed
        self.cwd_overflow += o.cwd_overflow
        self.reaped_evicted += o.reaped_evicted
        self.pend.evicted += o.pend.evicted
        self.pend.refused += o.pend.refused
        self.pend.refused_records += o.pend.refused_records
        self.calls_at_evicted += o.calls_at_evicted
        self.exited_evicted += o.exited_evicted
        self.late_routed += o.late_routed
        self.late_unplaced += o.late_unplaced
        self.late_no_start += o.late_no_start
        self.late_hops_out += o.late_hops_out
        self.late_maybe_evicted += o.late_maybe_evicted
        self.phase_untimed += o.phase_untimed
        self.past_due_evicted += o.past_due_evicted
        self.preview_overflow += o.preview_overflow
        self.tag_overflow += o.tag_overflow
        self.phase_reparented += o.phase_reparented
        self.phase_unread += o.phase_unread

    def _intern_purpose(self, p):
        pid = self._purpose_id.get(p)
        if pid is None:
            pid = self._purpose_id[p] = len(self._purposes)
            self._purposes.append(p)
        return pid

    def _intern_cwd(self, c):
        cid = self._cwd_id.get(c)
        if cid is None:
            if len(self._cwds) >= MAX_CWDS:
                return 0
            cid = self._cwd_id[c] = len(self._cwds)
            self._cwds.append(c)
        return cid

    def drift(self, s, anchor=None):
        """The two drift proxies for one session (see the module docstring).

        `anchor` is the agent's launch directory when the caller knows it and it
        is not `/`; the first measured call's cwd otherwise, and
        `cwd_anchor_src` says which.
        """
        n = len(s.ev_p)
        d = {"n": n, "capped": s.drift_cut, "min_n": MIN_DRIFT_N,
             "mix_jsd": None, "mix_first": None, "mix_last": None}
        if n >= MIN_DRIFT_N:
            k = n // 3
            a = collections.Counter(s.ev_p[:k])
            b = collections.Counter(s.ev_p[n - k:])
            d["mix_jsd"] = round(jsd(a, b), 3)
            d["mix_first"] = [[self._purposes[i], pct(c, k)] for i, c in a.most_common(3)]
            d["mix_last"] = [[self._purposes[i], pct(c, k)] for i, c in b.most_common(3)]
        cw = [c for c in s.ev_c if c]
        d["cwd_coverage_pct"] = pct(len(cw), n) if n else None
        if not cw:
            d.update(cwd_anchor=None, cwd_anchor_src=None, cwd_outside_pct=None,
                     cwd_distinct=0, cwd_top=[], cwd_first=None, cwd_last=None)
            return d
        names = self._cwds
        anchor, src = self.cwd_anchor(s, anchor)
        cnt = collections.Counter(cw)
        outside = sum(k for c, k in cnt.items() if not under(names[c], anchor))
        d.update(cwd_anchor=anchor, cwd_anchor_src=src,
                 cwd_outside_pct=pct(outside, len(cw)), cwd_distinct=len(cnt),
                 cwd_top=[[names[c], k] for c, k in cnt.most_common(5)],
                 cwd_first=names[cw[0]], cwd_last=names[cw[-1]])
        return d

    def cwd_anchor(self, s, anchor=None):
        """`(anchor, src)`, the directory a session's cwd drift is measured from
        and where it came from, as `drift` reports them: `anchor`, the agent's
        launch directory, when the caller knows it and it is not `/` (src
        `agent`); else the first measured call's cwd that is not `/`, or `/`
        when every one was (`first_call`). `(None, None)` when no call's cwd
        was recorded."""
        if not any(s.ev_c):
            return None, None
        if anchor == "/":
            # IDE-hosted agents run at `/`, under which every path is inside:
            # 0% by construction is not a measurement, so anchor on the work.
            anchor = None
        if anchor:
            return anchor, "agent"
        names = self._cwds
        return next((names[c] for c in s.ev_c if c and names[c] != "/"), "/"), "first_call"

    def rooted(self, s):
        """Whether an agent process was seen at the session's root.

        An agent-class key with none is a script's probe -- `codex --version`
        every minute from a status bar is its own key each time -- and counted
        as a session it would outnumber every real one on the fleet."""
        if s.cls != "agent":
            return True
        try:
            return s.kt == "apid" and (s.host, int(s.kv)) in self.owners
        except (TypeError, ValueError):
            return False

    def summary(self, live):
        """The plates: every session in the window, never just the listed few."""
        def q(vals, p):
            if not vals:
                return None
            v = sorted(vals)
            return v[min(len(v) - 1, int(p * (len(v) - 1)))]

        acc = {}
        for s in live:
            d = acc.setdefault(s.cls, {"sessions": 0, "probes": 0, "calls": [], "wall": [],
                                       "users": collections.Counter(), "wait": 0,
                                       "longest": None, "surfaces": collections.Counter()})
            if not self.rooted(s):
                d["probes"] += 1
                continue
            d["sessions"] += 1
            if s.cls == "agent":
                d["surfaces"][self.surface_of(s)[0]] += 1
            d["calls"].append(s.n)
            d["users"][s.user] += s.n
            d["wait"] += s.wait
            if s.first is not None and s.last is not None:
                w = s.last - s.first
                d["wall"].append(w)
                if d["longest"] is None or w > d["longest"]["wall_s"]:
                    d["longest"] = {"sid": s.sid, "wall_s": round(w, 1), "user": s.user,
                                    "agent_type": s.agent_type, "tool_calls": s.n}
        out = {}
        for cls, d in acc.items():
            calls = sum(d["calls"])
            top = d["users"].most_common(1)
            out[cls] = {
                "sessions": d["sessions"], "probes": d["probes"],
                "users": len(d["users"]),
                "top_user_share_pct": pct(top[0][1], calls) if top and calls else None,
                "tool_calls": calls,
                "calls_p50": q(d["calls"], .5), "calls_p90": q(d["calls"], .9),
                "calls_max": max(d["calls"]) if d["calls"] else None,
                "wall_p50_s": round(q(d["wall"], .5), 1) if d["wall"] else None,
                "wall_p90_s": round(q(d["wall"], .9), 1) if d["wall"] else None,
                "longest": d["longest"],
                "wait_pct": pct(d["wait"], calls) if calls else None,
                # agent sessions only: a person's login has no launch surface
                "by_surface": dict(d["surfaces"]) if cls == "agent" else None,
            }
        return out

    def approval_of(self, s):
        """The session root's own verdict; `unknown` when its record was not seen."""
        if s.kt != "apid":
            return s.approval
        try:
            return self.owners.get((s.host, int(s.kv)), "unknown")
        except (TypeError, ValueError):
            return "unknown"

    def surface_of(self, s):
        """(surface, detail) of an agent session: its root's own, `unknown` when
        that record was not seen. A Codex app-server whose argv names no client
        takes the surface of the same agent's process that started it -- the
        terminal UI it is the front-end of (cascades.py) -- and stays unknown
        when that is init: a daemon serves whichever client attaches."""
        if s.cls != "agent":
            return None, None
        try:
            own = self.surfaces.get((s.host, int(s.kv))) if s.kt == "apid" else None
        except (TypeError, ValueError):
            own = None
        if own is None:
            return "unknown", None
        surface, detail, ppid, agent_type = own
        if surface == "unknown" and ppid not in _NO_PARENT:
            up = self.surfaces.get((s.host, ppid))
            if up is not None and up[3] == agent_type and up[0] != "unknown":
                return up[0], up[1]
        return surface, detail

    def anchor_of(self, s):
        """The agent process's own cwd, when the session is keyed on one."""
        if self.procs is None or s.kt != "apid":
            return None
        try:
            e = self.procs.get(s.host, int(s.kv))
        except (TypeError, ValueError):
            return None
        return e[3] if e else None

    def _evict(self, now):
        # Idle eviction must never reach INSIDE the window, or the panel would
        # rank "sessions active in the last idle_evict_s" while claiming to rank
        # the window -- at the 2 h default and a 24 h window it already silently
        # did. Memory stays bounded by `max_sessions` (`_shed`).
        cutoff = now - max(self.idle_evict, self.window_s)
        dead = [k for k, v in self.sessions.items() if (v.last or 0) < cutoff]
        for k in dead:
            del self.sessions[k]
            self.evicted += 1

    def _shed(self, keep=None):
        """Past `max_sessions`: whole sessions go, the least recently active
        first, until a tenth of the cap is free.

        A session here is its tree (`_tree`). Schema 7 keys each Bash tool call
        on its own shell, so a session at work all day is hundreds of keys, and
        its morning's are as old as any: dropped key by key, it kept only its
        afternoon. Recency is the newest record a tree holds, never the clock:
        a window is merged from its hours oldest first, hours behind its end --
        the present, pinned or not -- and against that every session held
        looked idle, and all of them went. `keep` is the tree a record is
        arriving for, the newest by definition. A tree that would free more
        than another tenth of the cap on its own is cut instead, its oldest
        keys first: the one way a session held is split.
        """
        room = self.max_sessions // 10
        trees = {}
        for sid, s in self.sessions.items():
            key = _tree(s.host, s.tree_root, sid)
            t = trees.get(key)
            if t is None:
                t = trees[key] = [0, []]
            t[0] = max(t[0], s.last or 0)
            t[1].append(sid)
        if keep in trees:
            trees[keep][0] = math.inf
        excess = len(self.sessions) - (self.max_sessions - room)
        for _, sids in sorted(trees.values(), key=lambda t: t[0]):
            if excess <= 0:
                break
            if len(sids) - excess > room:
                sids = sorted(sids, key=lambda k: self.sessions[k].last or 0)[:excess]
            for k in sids:
                del self.sessions[k]
            self.evicted += len(sids)
            excess -= len(sids)

    def folded(self):
        """`_fold_all` over the held sessions, cached until something changes."""
        key = (self.fed, self.evicted, self.procs.version if self.procs else None)
        if self._fold_cache[0] != key:
            out, vias, members = self._fold_all(self.sessions.values())
            self._fold_cache = (key, (out, vias), members)
        return self._fold_cache[1]

    def lookup(self, sid):
        """(folded session, owner_via, n_keys, member sids) for row `sid`, or None.

        The member sids are the collector keys the row folds, as each key's own
        `_sid`: exactly the records the row counted, so a re-read for the
        inspector (proctree.py) selects the same processes the row summarises.
        """
        folded = self.folded()[0]
        members = self._fold_cache[2].get(sid)
        if not members:
            return None
        for s, via, k in folded:
            if s.sid == sid:
                return s, via, k, list(members)
        return None

    def _by_agent(self, since=None):
        """(host, agent pid, user) -> [(session, owner_via, n_keys)]: the folded
        rows of each agent (`_agent_key`), one for each day and class (the sid
        carries both). Sessions last active before `since` are left out."""
        by = {}
        for s, via, k in self.folded()[0]:
            key = _agent_key(s)
            if key is not None and (since is None or (s.last or 0) >= since):
                by.setdefault(key, []).append((s, via, k))
        return by

    def _agent_days(self):
        """`_by_agent()`, held until the fold changes: every row asks it for the
        rows it shares a block with (`phase_block`)."""
        fold = self.folded()
        if self._day_idx[0] is not fold:
            self._day_idx = (fold, self._by_agent())
        return self._day_idx[1]

    def agent_sessions(self, since=None):
        """(host, agent pid, user) -> (session, owner_via, n_keys), one per agent.

        Rows are split by day and class (the sid carries both); an agent is
        not, so its rows are folded again here. A pid is reused, so the rows of
        two users with one are two agents (`_agent_key`). Sessions last active
        before `since` are left out.
        """
        out = {}
        for key, items in self._by_agent(since).items():
            if len(items) == 1:
                out[key] = items[0]
                continue
            parts = sorted((i[0] for i in items),
                           key=lambda p: p.first if p.first is not None else float("inf"))
            vs = [i[1] for i in items if i[1]]
            out[key] = (fold(parts, parts[0].sid, parts[0].kv, self.phase_params),
                        max(vs, key=VIA.index) if vs else None, sum(i[2] for i in items))
        return out

    def _fold_all(self, sessions):
        """Resolve every apid key to its agent and merge the keys that share one.

        Returns ([(session, owner_via, n_keys)], per-key via counts, and each
        row's member sids). Builds new objects for merged sessions: the held
        ones keep ingesting after this.
        """
        groups = {}
        vias = collections.Counter()
        for s in sessions:
            target, via = s.sid, None
            if s.kt == "apid" and self.procs is not None:
                try:
                    pid = int(s.kv)
                except (TypeError, ValueError):
                    pid = None
                if pid is not None:
                    owner, via = self.procs.owner(s.host, pid, s.tree_root)
                    vias[via] += 1
                    if owner != pid:
                        target = s.sid[:s.sid.rfind(":") + 1] + str(owner)
            groups.setdefault(target, []).append((s, via))
        out = []
        cache, kept = self._group_cache, {}
        for target, members in groups.items():
            members.sort(key=lambda m: m[0].sid != target)    # the owner's own key first
            parts = [m[0] for m in members]
            if len(parts) == 1 and parts[0].sid == target:
                s = parts[0]
            else:
                # The same session objects, each holding the same number of
                # records, fold to the same session: every change to a part
                # (`Session.add`, `merge_session`) adds to its count.
                sig = tuple((p, p.n) for p in parts)
                hit = cache.get(target)
                s = hit[1] if hit is not None and hit[0] == sig else \
                    fold(parts, target, target.rsplit(":", 1)[1], self.phase_params)
                kept[target] = (sig, s)
            # Report how the FOLDED keys got here, at their least certain: a row
            # assembled partly by fallback must not read as fully resolved.
            vs = [v for p, v in members if p.sid != target] or [v for _, v in members]
            vs = [v for v in vs if v is not None]
            out.append((s, max(vs, key=VIA.index) if vs else None, len(parts)))
        self._group_cache = kept
        members = {t: [m[0].sid for m in ms] for t, ms in groups.items()}
        return out, vias, members

    def _row(self, s, via=None, n_keys=1, members=None):
        """One row. `members` are the collector keys it folds, as `lookup`
        gives them; by default the last full fold's."""
        chain = s.chain[:self.max_runs]
        purpose = s.purpose[:self.purpose_cap]
        surface, surface_detail = self.surface_of(s)
        if members is None:
            held = self._fold_cache[2]
            members = (held.get(s.sid) if held else None) or [s.sid]
        phases, n_calls = self._phases(s, members)
        return {
            "sid": s.sid, "key_type": s.kt, "host": s.host, "user": s.user,
            # how many collector session keys this row folds together, and how
            # the least certain of them was attributed (procindex.VIA)
            "fragments": n_keys, "owner_via": via,
            "drift": self.drift(s, self.anchor_of(s)),
            "actor3": s.cls, "agent_type": s.agent_type,
            "n_events": s.n, "cpu_s": round(s.cpu, 3),
            # a session can span users (a root helper inside a user's tree), so
            # name the dominant one and carry the rest
            "users": sorted(s.users, key=lambda u: -s.users[u]),
            "n_users": len(s.users), "pids": s.pids,
            "agent_pid": (s.kv if s.kt == "apid" else None),
            "distinct_tools": len(s.tools), "chain_runs": len(s.chain),
            "wall_s": round((s.last - s.first), 1) if s.first and s.last else None,
            "peak_rss_mb": round(s.rss, 1),
            "io_rd_mb": round(s.rd, 3), "io_wr_mb": round(s.wr, 3),
            "net_tx_mb": round(s.tx, 3), "net_rx_mb": round(s.rx, 3),
            "sandbox": s.sandbox, "approval": self.approval_of(s),
            "surface": surface, "surface_detail": surface_detail,
            "sandbox_ancestry": s.ancestry, "autonomous": s.autonomous,
            "tool_rle": chain, "chain_truncated": s.truncated or len(s.chain) > self.max_runs,
            "purpose_rle": purpose,
            # When each run began (its first record's time), capped as the runs
            # are, so the page can open the inspector at a run; null where the
            # record carried no time. With the session's first and last record,
            # it is where the run sits in the session.
            "tool_t": _times(s.chain_t, len(chain)),
            "purpose_t": _times(s.purpose_t, len(purpose)),
            "first": _times([s.first], 1)[0], "last": _times([s.last], 1)[0],
            # the tool calls, and what the agent was doing (phase_segment);
            # null and `{unavailable}` where its calls are not recognized. The
            # count is this day's: the block of an agent that worked on more
            # than one day counts the session's.
            "n_calls": n_calls,
            "phases": phases,
        }

    def result(self, now=None):
        now = now or time.time()
        self._evict(now)
        cutoff = now - self.window_s
        folded, vias = self.folded()
        meta = {s.sid: (via, k) for s, via, k in folded}
        live = [s for s, _, _ in folded
                if (s.last or 0) >= cutoff and s.kt in self.rankable]
        ranks, rows, dom = {}, {}, {}
        keyf = {"events": lambda s: s.n, "cpu_s": lambda s: s.cpu,
                "peak_rss_mb": lambda s: s.rss,
                "io_mb": lambda s: s.rd + s.wr,
                "net_mb": lambda s: s.tx + s.rx,
                "distinct_tools": lambda s: len(s.tools),
                "recent": lambda s: s.last or 0}
        # what a user's share of a list is a share OF: the ranked quantity, or
        # for recency (a time, not an amount) the sessions listed
        weight = dict(keyf, recent=lambda s: 1)
        # Recency lists real sessions only (`rooted`): a status-bar probe runs
        # every minute, and a tool call still running is a key of its own
        # until its shell's exit says whose it is -- either would hold the top.
        real = [s for s in live if self.rooted(s)]
        for r in RANKS:
            top = sorted(real if r == "recent" else live, key=keyf[r],
                         reverse=True)[:self.top_n]
            ranks[r] = [s.sid for s in top]
            for s in top:
                rows.setdefault(s.sid, self._row(s, *meta[s.sid]))
            if top:
                byu = collections.Counter()
                for s in top:
                    byu[s.user] += weight[r](s)
                tot = sum(byu.values())
                dom[r] = pct(byu.most_common(1)[0][1], tot) if tot else 0.0
            else:
                dom[r] = None
        by_cls = collections.Counter(s.cls for s, _, _ in folded)
        return {
            "window": {"minutes": self.window_s // 60,
                       "from": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(cutoff)),
                       "to": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))},
            "rank_by": "recent", "rank_meaning": RANK_MEANING,
            "ranks": ranks, "rows": rows,
            "by_class_counts": dict(by_cls), "by_key_type": dict(self.by_kt),
            "fold": {"raw_sessions": len(self.sessions), "sessions": len(folded),
                     "by_via": dict(vias), "cwd_overflow": self.cwd_overflow,
                     "index": self.procs.result() if self.procs is not None else None},
            "dominant_user_share_pct": dom,
            "summary": self.summary(live),
            "buffer": {"sessions": len(self.sessions), "evicted": self.evicted,
                       "reaped": len(self.reaped), "reaped_evicted": self.reaped_evicted,
                       # the phases' joins and bounds, every one counted
                       "phase_pending": len(self.pend.entries),
                       "phase_pending_evicted": self.pend.evicted,
                       "phase_pending_refused": self.pend.refused,
                       "phase_pending_refused_records": self.pend.refused_records,
                       "phase_call_index": len(self.calls_at),
                       "phase_call_index_evicted": self.calls_at_evicted,
                       "phase_past_due_evicted": self.past_due_evicted,
                       "phase_exited": len(self.exited),
                       "phase_exited_evicted": self.exited_evicted,
                       "phase_late_routed": self.late_routed,
                       "phase_late_unplaced": self.late_unplaced,
                       "phase_late_no_start": self.late_no_start,
                       "phase_late_hops_out": self.late_hops_out,
                       "phase_late_maybe_evicted": self.late_maybe_evicted,
                       "phase_untimed": self.phase_untimed,
                       "phase_reparented": self.phase_reparented,
                       "phase_unread": self.phase_unread,
                       "preview_overflow": self.preview_overflow,
                       "phase_tag_overflow": self.tag_overflow},
            "basis": {
                "sessionize": "session_key='apid:<agent_pid>'; absent -> sess (cgroup "
                              "session-(\\d+)\\.scope) -> tty as user@tty -> usr "
                              "(analyze/extract_trajectories.py:278-294)",
                "fold": "an apid key whose process is not an agent (a tool-call shell, "
                        "a sandbox wrapper, an embedded rg, a probe) is folded into "
                        "the nearest agent above it; failing that, into the tree's "
                        "top-most agent (owner_via=tree_root). A key whose process "
                        "was never seen is left alone (owner_via=unknown). Folded "
                        "chains interleave by run start time.",
                "rle": "run-length compression over effective_comm. True run count is "
                       "chain_runs; the displayed chain is capped at max_runs_shown.",
                "io": "io_rd_mb / io_wr_mb sum each process's OWN disk bytes: the "
                      "collector's figure includes every child the process reaped, "
                      "which is taken back off at the parent's exit. A child whose "
                      "exit the window did not see is not taken off. "
                      "net_tx_mb / net_rx_mb are per process already; peak_rss_mb "
                      "is the largest of any one exited process.",
                "summary": "over every session in the window. An agent-class session "
                           "counts only when an agent process was seen at its root; "
                           "the rest (scripts probing `codex --version`) are `probes`. "
                           "wait_pct is the share of tool calls whose purpose is poll "
                           "or slurm_mon (ps, pgrep, sleep loops; squeue, sacct).",
                "phases": "per agent row, read from the commands the agent ran: its tool calls "
                          "named by phase_rules and cut into phases by phase_segment, none "
                          "across the boundary of a task (below), with the calls still "
                          "running under a process that has not exited. "
                          "Unavailable, with n_calls null, where an agent type's calls are "
                          "not recognized; housekeeping is the agent's own commands, counted "
                          "apart. An agent that worked on more than one day has a row for "
                          "each, and each shows one block, cut as a single session from the "
                          "calls of all those rows in the window: the block's n_calls counts "
                          "the session's calls, a row's own n_calls its day's, days lists the "
                          "rows, and sid on a phase or task is the day that holds it.",
                "tasks": "per agent row, above the phases: the same calls cut by phase_tasks "
                         "into stretches of work about one thing. The cuts are a gap of gap_s "
                         "or more with nothing running that the agent waits on, a name new to "
                         "the session that starts to recur, and a commit or pull request that "
                         "closes a burst of git, once for commits minutes apart. Short pieces "
                         "merge into the neighbor they are most like, and while a local day "
                         "holds more than cap tasks, its most alike neighbors merge. A few calls "
                         "hours from other work are a lone stretch, set apart. A task is labeled "
                         "with the agent's own words when its commands hold some (a pull request's "
                         "title, a commit's subject, or an entry in a log file it appends to), "
                         "else with a verb and the one or two names most specific to it. Each "
                         "phase says which task it belongs to. The headline is the project and "
                         "the names that recur across tasks. The project is the folder the "
                         "agent was launched in or, when that is a home folder or is named for "
                         "a person (the user, or one whose home or lab folder holds it), the "
                         "folder its commands change into most. A task that holds a call still "
                         "running is open. When the log dropped calls, the last task is "
                         "incomplete, as is any that ends after the first call dropped.",
                "excluded_key_types": ["usr"],
                "excluded_why": "a usr-keyed group is 'everything that user did', not a "
                                "trajectory; counted in by_key_type, never ranked",
            },
        }
