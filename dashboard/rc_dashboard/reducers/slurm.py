"""What each session asks of the Slurm scheduler, and how it asks.

"An agent submitted a job" says almost nothing about agent behaviour. What a
login node's capture can show is HOW: on boslogin06 (2026-10-09) agents made
~35 000 scheduler queries for ~2 100 jobs -- 17 a job, against 0.2 for people
-- one Claude session polled squeue every 5 s for 5 h, another submitted 875
jobs, 112 of them in one minute, and 79% of agent submissions asked for
`--parsable`, i.e. a program was reading the job id. So per session this keeps:

* **submissions**, from the collector's `submit` records (one per sbatch /
  srun / salloc, or the wrapper script that returned the job), each judged by
  what came back: `job` (a job id), `dry_run` (`--test-only`, which never makes
  a job), `step` (`srun --jobid`/`--overlap` into an allocation already held),
  `rejected` (no job id, a non-zero exit, and the scheduler's own refusal --
  sbatch's error, or srun's or salloc's that it could not get the allocation;
  that line is its reason) or `no_id` (no id read: it exited 0, or an srun
  ran and failed);
* **how it was asked**: `--parsable`, `--dependency`, `--wrap`, `--array`, and
  whether the request (partition, time, memory, GPUs) was on the command line
  at all -- a `#SBATCH` line in the script is invisible here (`req_src`);
* **queries**: every exit of squeue, sacct, scontrol, sinfo, ... (tool_buckets'
  `slurm-query`), and the shape of them -- calls within `POLL_JOIN_S` of each
  other are one poll, and polls no more than `LOOP_GAP_S` apart are one
  polling loop, whose length and cadence are the session's headline;
* **cancels** (scancel) and **runs** (srun/salloc exits, the payload included).

A session is the agent that owns the collector's key, folded through the
window's process index exactly as the trajectories and the sandbox fold it, so
a Claude Code session's tool calls are one row, not one per call. People and
IDEs have no such owner; their rows are one per user.

Requests are argv-only: a GPU-hour figure counts only submissions whose GPUs
AND time limit were on the command line, and says how many those were.
"""
import collections
import hashlib
import re
from array import array

from . import Quant, add_counts, pct
from .resources import bin_for
from ..redact import redact
from ..tool_buckets import SLURM_Q

POLL_JOIN_S = 2.0          # queries this close together are one poll
LOOP_GAP_S = 180.0         # polls this close together are one loop
LOOP_MIN_S = 600.0         # a loop at least this long is "polling"
MAX_KEYS = 50000           # raw collector keys held
MAX_QUERY_TS = 50000       # query timestamps held per key; the rest are counted
MAX_SUBMIT_TS = 50000      # job submission times held per key; the rest are counted
MINUTE_S = 60.0            # "in one minute": any 60 s, not a clock minute
MAX_SHAPES = 2000          # distinct command-line shapes held per key
ROWS = 200                 # sessions served
FAIL_ROWS = 20

# Abnormal activity at the scheduler: what a session did that an operator would
# ask its owner about. Each rule is the session's own count against a
# threshold, `reducers.slurm.abnormal` in the config, whose defaults these are
# (a test holds the two equal).
#
# Polling the queue is not one of them: checks at most LOOP_GAP_S apart for
# LOOP_MIN_S, the loop above, is how an agent waits for its own job, and it is
# the Slurm tab's description of a session, not an alarm. Polling FAST is: the
# longest stretch of checks at most `poll_gap_s` apart lasting `poll_min_s` or
# more, as one Claude session polled every 5 s for 5 h. The rest are bursts and
# repeats: the most queue checks, or jobs, in any 60 s (one session submitted
# 112 in a minute, where an array is one submission), and refusals, cancels and
# failed runs that came again and again. A session that breaks any rule is
# served whatever its rank, as an agent that wrote outside its home is
# (cascades.py).
ABNORMAL = {
    "poll_gap_s": 30,      # fast polling: checks at most this many seconds apart ...
    "poll_min_s": 1800,    # ... for at least this many seconds
    "query_burst": 30,     # queue checks in any 60 s
    "submit_burst": 20,    # jobs submitted in any 60 s
    "refused": 3,          # submissions the scheduler refused
    "cancels": 10,         # scancel calls
    "failed_runs": 5,      # srun or salloc runs that ended non-zero or on a signal
}
ABNORMAL_RULES = ("fast_polling", "query_burst", "submit_burst", "refused", "cancels",
                  "failed_runs")
ABNORMAL_ROWS = 500        # abnormal sessions served past the busiest ROWS; the rest counted

_DIGITS = re.compile(rb"\d+")
_NUM = re.compile(r"\d+")
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07|[\x00-\x08\x0b-\x1f\x7f]")
# What the scheduler prints when it refuses, at the start of a line: any error
# of sbatch's, which prints nothing else on a failure, and srun's and salloc's
# about the allocation -- "Unable to allocate resources" (srun, and salloc
# --immediate), "Job submit/allocate failed" (salloc; "Failed to allocate
# resources" in older Slurm), "Unable to create step" and "Unable to confirm
# allocation" (srun inside one). Those two also relay the payload's own output,
# and say how it ended in an error line of their own (`srun: error: holy7c24101:
# task 0: Exited with exit code 1`): a program that ran and failed is not a
# refusal, nor is a "denied" or "not available" it printed. The line's start is
# required, prefix and all: numpy's MemoryError says "Unable to allocate 2.00
# GiB for an array". So `rejected` is a lower bound: a submission keeps its
# last 300 characters (`captured_tail`), and a refusal line cut at its start
# there is missed -- which takes a reason of some 200 characters, as Slurm
# prints that line last.
_REFUSAL = re.compile(r"sbatch: error|(?:srun|salloc): error: (?:Unable to allocate"
                      r"|Job submit/allocate failed|Failed to allocate|Unable to create step"
                      r"|Unable to confirm allocation)", re.I)
# A refusal's reason: the scheduler's first error line, which can say more than
# the allocation line after it (QOSMaxSubmitJobPerUserLimit). A refused srun ran
# no task, so none of its error lines is a task's.
_ERROR = re.compile(r"(?:sbatch|srun|salloc): error", re.I)
_ARRAY_PART = re.compile(r"^(\d+)(?:-(\d+)(?::(\d+))?)?$")

FLAGS = (
    ("parsable", re.compile(r"(?:^|\s)--parsable\b")),
    ("dependency", re.compile(r"(?:^|\s)(?:--dependency\b|-d\s)")),
    ("wrap", re.compile(r"(?:^|\s)--wrap\b")),
    ("array", re.compile(r"(?:^|\s)(?:--array\b|-a\s)")),
)
OUTCOMES = ("job", "dry_run", "step", "rejected", "no_id")


def outcome(rec):
    args = rec.get("args") or ""
    if "--test-only" in args:
        return "dry_run"
    if rec.get("job_id") is not None or rec.get("job_ids"):
        return "job"
    if re.search(r"(?:^|\s)(?:--jobid\b|--overlap\b)", args):
        return "step"
    ec = rec.get("exit_code")
    if ec not in (0, None) and (
            (rec.get("tool") or "").startswith("sbatch")
            or any(_REFUSAL.match(x) for x in _lines(rec))):
        return "rejected"
    return "no_id"


def _lines(rec):
    """The captured output's lines, escapes and control characters out."""
    lines = [x.strip() for x in _ANSI.sub("", rec.get("captured_tail") or "").splitlines()]
    return [x for x in lines if x]


def reason(rec):
    """The last line the scheduler printed, with ids and node numbers masked --
    and credentials, since it is served as text of its own."""
    lines = _lines(rec)
    # the scheduler's own error line, else the last line printed: sbatch can
    # follow its error with an example script
    pick = next((x for x in lines if _ERROR.match(x)), lines[-1] if lines else None)
    if pick:
        return _NUM.sub("N", redact(pick))[:160]
    ec, sig = rec.get("exit_code"), rec.get("signal")
    return "exit %s%s, nothing printed" % (ec, ", signal %s" % sig if sig else "")


def array_tasks(spec):
    """Tasks an `--array` spec names (`0-99%10` -> 100), or None."""
    if spec in (None, ""):
        return None
    n = 0
    for part in str(spec).split("%")[0].split(","):
        m = _ARRAY_PART.match(part.strip())
        if not m:
            return None
        a, b, step = int(m.group(1)), m.group(2), int(m.group(3) or 1)
        n += 1 if b is None else max(0, (int(b) - a) // max(1, step) + 1)
    return n or None


def shape(args):
    """A command line with its numbers masked: the same script submitted for
    job 1..872 is one shape, a newly generated script is another."""
    # A stable digest, not `hash()`: Python salts that per process, and a shape
    # saved by one run (a window snapshot, an hour state) is compared with the
    # shapes of the next.
    return int.from_bytes(hashlib.blake2b(
        _DIGITS.sub(b"N", (args or "").encode("utf-8", "replace")), digest_size=8).digest(),
        "little")


class _Key:
    """One collector key's share of a session, before the fold."""
    __slots__ = ("cls", "user", "agent_type", "host", "first", "last", "out", "tools",
                 "flags", "argv_req", "shapes", "shapes_over", "sub_ts", "sub_dropped", "parts",
                 "gpus", "gpu_h", "gpu_jobs", "gpu_h_known", "q", "q_by", "q_ts",
                 "q_dropped", "cancels", "runs", "runs_failed", "fails", "example", "example_t")

    def __init__(self, rec):
        self.cls = rec.get("_a3")
        self.user = rec.get("user")
        self.agent_type = rec.get("agent_type")
        self.host = rec.get("host")
        self.first = self.last = None
        self.out = collections.Counter()
        self.tools = collections.Counter()
        self.flags = collections.Counter()
        self.argv_req = 0
        self.shapes = set()
        self.shapes_over = False
        self.sub_ts = array("d")
        self.sub_dropped = 0
        self.parts = collections.Counter()
        self.gpus = 0
        self.gpu_h = 0.0
        self.gpu_jobs = 0
        self.gpu_h_known = 0
        self.q = 0
        self.q_by = collections.Counter()
        self.q_ts = array("d")
        self.q_dropped = 0
        self.cancels = 0
        self.runs = 0
        self.runs_failed = 0
        self.fails = collections.Counter()
        self.example = None
        self.example_t = None

    def merge(self, o):
        """Fold a LATER share of the same key into this one. `o` is consumed."""
        if o.first is not None and (self.first is None or o.first < self.first):
            self.first = o.first
        if o.last is not None and (self.last is None or o.last > self.last):
            self.last = o.last
        for k in ("out", "tools", "flags", "parts", "q_by", "fails"):
            add_counts(getattr(self, k), getattr(o, k))
        for k in ("argv_req", "gpus", "gpu_h", "gpu_jobs", "gpu_h_known", "q",
                  "cancels", "runs", "runs_failed"):
            setattr(self, k, getattr(self, k) + getattr(o, k))
        # Past MAX_SHAPES one pass stops adding: the count is the same, which
        # shapes made it in can differ.
        if not self.shapes_over:
            self.shapes |= o.shapes
            if len(self.shapes) >= MAX_SHAPES:
                self.shapes_over = True
                while len(self.shapes) > MAX_SHAPES:
                    self.shapes.pop()
        self.shapes_over = self.shapes_over or o.shapes_over
        room = max(0, MAX_QUERY_TS - len(self.q_ts))
        self.q_ts.extend(o.q_ts[:room])
        self.q_dropped += o.q_dropped + max(0, len(o.q_ts) - room)
        room = max(0, MAX_SUBMIT_TS - len(self.sub_ts))
        self.sub_ts.extend(o.sub_ts[:room])
        self.sub_dropped += o.sub_dropped + max(0, len(o.sub_ts) - room)
        # the newest job's, as `_submit` keeps it (a tie goes to the later share)
        if o.example_t is not None and (self.example_t is None
                                        or o.example_t >= self.example_t):
            self.example, self.example_t = o.example, o.example_t

    def seen(self, ts):
        if ts is None:
            return
        if self.first is None or ts < self.first:
            self.first = ts
        if self.last is None or ts > self.last:
            self.last = ts


class SlurmReducer:
    # Exits, not `truncated`: an `srun --pty` still running at a collector stop
    # is written then, and again at its exit after the restart -- one run.
    EVENTS = ("exit", "submit")
    KEY = "slurm"

    def __init__(self, cfg, classes, window_s=86400, procs=None):
        self.cfg = cfg
        self.classes = list(classes)
        self.procs = procs
        # The thresholds are config: config.py checks them at start, and a saved
        # state's fingerprint pins them (snapshots.py), so a change rebuilds.
        ab = (cfg.get("reducers.slurm", {}) or {}).get("abnormal") or {}
        self.abnormal = {k: ab.get(k, v) for k, v in ABNORMAL.items()}
        self.bin_s = bin_for(window_s)
        self.keys = {}
        self.keys_dropped = 0
        self.bins = {}                 # bin -> cls -> [submits, queries]
        self.time_limit = collections.defaultdict(Quant)
        self.fail_rows = collections.Counter()
        self.fail_eg = {}

    # ------------------------------------------------------------------ feed
    def _key(self, rec):
        cls = rec.get("_a3")
        host = rec.get("host")
        if cls == "agent" and rec.get("_kt") == "apid":
            try:
                k = ("a", host, int(rec.get("_kv")), rec.get("tree_root_pid"))
            except (TypeError, ValueError):
                k = ("u", host, cls, rec.get("user"))
        else:
            k = ("u", host, cls, rec.get("user"))
        s = self.keys.get(k)
        if s is None:
            if len(self.keys) >= MAX_KEYS:
                self.keys_dropped += 1
                return None
            s = self.keys[k] = _Key(rec)
        return s

    def _bin(self, ts, cls, i):
        if ts is None:
            return
        row = self.bins.setdefault(int(ts // self.bin_s), {}).setdefault(cls, [0, 0])
        row[i] += 1

    def feed(self, rec):
        ev = rec.get("event")
        if ev == "submit":
            self._submit(rec)
            return
        comm = rec.get("_command") or ""
        if comm in SLURM_Q:
            kind = "q"
        elif comm == "scancel":
            kind = "cancel"
        elif comm in ("srun", "salloc"):
            kind = "run"
        else:
            return
        s = self._key(rec)
        if s is None:
            return
        ts = rec.get("_ts_epoch")
        s.seen(ts)
        if kind == "q":
            s.q += 1
            s.q_by[comm] += 1
            if ts is not None:
                if len(s.q_ts) < MAX_QUERY_TS:
                    s.q_ts.append(ts)
                else:
                    s.q_dropped += 1
            self._bin(ts, s.cls, 1)
        elif kind == "cancel":
            s.cancels += 1
        else:
            s.runs += 1
            if rec.get("exit_code") not in (0, None) or rec.get("signal"):
                s.runs_failed += 1

    def _submit(self, rec):
        s = self._key(rec)
        if s is None:
            return
        ts = rec.get("_ts_epoch")
        s.seen(ts)
        oc = outcome(rec)
        s.out[oc] += 1
        s.tools[rec.get("tool") or rec.get("_command") or "?"] += 1
        args = rec.get("args") or ""
        if oc == "rejected":
            why = reason(rec)
            s.fails[why] += 1
            fk = (s.cls, rec.get("tool") or "?", why)
            self.fail_rows[fk] += 1
            if fk not in self.fail_eg:          # masked, then cut: a key the cut falls in
                self.fail_eg[fk] = {"user": rec.get("user"), "ts": rec.get("ts"),
                                    "args": redact(args)[:300]}
        if oc == "dry_run":
            return
        for name, rx in FLAGS:
            if rx.search(args):
                s.flags[name] += 1
        if rec.get("req_src") == "argv" or any(rec.get(f) is not None for f in (
                "partition", "time_limit_s", "mem", "gpus")):
            s.argv_req += 1
        if not s.shapes_over:
            s.shapes.add(shape(args))
            if len(s.shapes) >= MAX_SHAPES:
                s.shapes_over = True
        if oc != "job":
            return
        if s.example_t is None or (ts or 0) >= s.example_t:
            s.example, s.example_t = redact(args)[:300], ts or 0     # masked, then cut
        if ts is not None:
            if len(s.sub_ts) < MAX_SUBMIT_TS:
                s.sub_ts.append(ts)
            else:
                s.sub_dropped += 1
        self._bin(ts, s.cls, 0)
        for p in str(rec.get("partition") or "").split(","):
            if p.strip():
                s.parts[p.strip()] += 1
        tl = rec.get("time_limit_s")
        self.time_limit[s.cls].add(tl)
        g = rec.get("gpus")
        if isinstance(g, (int, float)) and g > 0:
            tasks = array_tasks(rec.get("array")) or 1
            s.gpus += g * tasks
            s.gpu_jobs += 1
            if isinstance(tl, (int, float)) and tl > 0 and (
                    rec.get("array") in (None, "") or array_tasks(rec.get("array"))):
                s.gpu_h += g * tasks * tl / 3600.0
                s.gpu_h_known += 1

    # ----------------------------------------------------------------- merge
    def merge(self, o):
        """Fold a LATER state into this one. `o` is consumed. Nothing here
        crosses an hour: keys merge key by key, and an hour state's finer bins
        fold into this state's wider ones (resources.ResourceReducer.merge)."""
        for k, s in o.keys.items():
            cur = self.keys.get(k)
            if cur is not None:
                cur.merge(s)
            elif len(self.keys) >= MAX_KEYS:
                self.keys_dropped += 1
            else:
                self.keys[k] = s
        self.keys_dropped += o.keys_dropped
        if self.bin_s % o.bin_s:
            raise ValueError("cannot merge %d s bins into %d s bins" % (o.bin_s, self.bin_s))
        f = self.bin_s // o.bin_s
        for b, row in o.bins.items():
            dst = self.bins.setdefault(b // f, {})
            for cls, v in row.items():
                d = dst.setdefault(cls, [0, 0])
                d[0] += v[0]
                d[1] += v[1]
        for cls, q in o.time_limit.items():
            self.time_limit[cls].merge(q)
        add_counts(self.fail_rows, o.fail_rows)
        # The first failure in time is the example, as `feed` keeps it: a
        # later hour's never replaces this one's, and of two nodes' states of
        # the same hour (hours.py, `ingest.workers`) the earlier one wins.
        for k, eg in o.fail_eg.items():
            cur = self.fail_eg.get(k)
            if cur is None or (eg.get("ts") is not None
                               and (cur.get("ts") is None or eg["ts"] < cur["ts"])):
                self.fail_eg[k] = eg

    # ---------------------------------------------------------------- result
    def _owner(self, host, apid, root):
        if self.procs is None:
            return apid, "unknown"
        try:
            return self.procs.owner(host, apid, root)
        except Exception:
            return apid, "unknown"

    def _sessions(self):
        """Fold every agent key into the agent that owns it."""
        out = {}
        for k, s in self.keys.items():
            if k[0] == "a":
                owner, via = self._owner(k[1], k[2], k[3])
                sk = ("a", k[1], owner)
            else:
                via, sk = None, k
            g = out.get(sk)
            if g is None:
                g = out[sk] = {"keys": [], "via": collections.Counter(), "owner": sk[2]}
            g["keys"].append(s)
            if via:
                g["via"][via] += 1
        return out

    @staticmethod
    def _polls(ts, fast_gap_s=None):
        """Polls (queries joined within POLL_JOIN_S), the longest loop (polls at
        most LOOP_GAP_S apart), and with `fast_gap_s` the longest stretch of
        polls at most that far apart. `ts` sorted."""
        if not ts:
            return {"polls": 0, "median_gap_s": None, "loop": None, "fast": None}
        starts = [ts[0]]
        for a, b in zip(ts, ts[1:]):
            if b - a > POLL_JOIN_S:
                starts.append(b)
        gaps = [b - a for a, b in zip(starts, starts[1:])]
        loop = SlurmReducer._stretch(starts, LOOP_GAP_S)
        if loop:
            loop["polling"] = loop["span_s"] >= LOOP_MIN_S
        return {"polls": len(starts),
                "median_gap_s": round(sorted(gaps)[len(gaps) // 2], 1) if gaps else None,
                "loop": loop,
                "fast": SlurmReducer._stretch(starts, fast_gap_s) if fast_gap_s else None}

    @staticmethod
    def _stretch(starts, gap_s):
        """The longest run of polls each at most `gap_s` after the one before:
        when, how many, how long, and its median gap. None without two polls."""
        best, run = None, [starts[0]]
        for t in starts[1:] + [None]:
            if t is not None and t - run[-1] <= gap_s:
                run.append(t)
                continue
            if len(run) > 1 and (best is None or run[-1] - run[0] > best[-1] - best[0]):
                best = run
            if t is not None:
                run = [t]
        if not best:
            return None
        g = sorted(b - a for a, b in zip(best, best[1:]))
        return {"from": best[0], "to": best[-1], "polls": len(best),
                "span_s": round(best[-1] - best[0], 1), "every_s": round(g[len(g) // 2], 1)}

    @staticmethod
    def _in_a_minute(ts):
        """The most of `ts` (sorted) within any MINUTE_S, a half-open [t, t + 60 s):
        29 at :59 and 29 at :00 are 58, which clock minutes would call 29."""
        most, i = 0, 0
        for j, t in enumerate(ts):
            while t - ts[i] >= MINUTE_S:
                i += 1
            most = max(most, j - i + 1)
        return most

    def abnormal_rules(self):
        """Each rule and its thresholds, in the order the page lists them."""
        th = self.abnormal
        return ([{"id": "fast_polling", "gap_s": th["poll_gap_s"], "min_s": th["poll_min_s"]}]
                + [{"id": r, "min": th[r]} for r in ABNORMAL_RULES[1:]])

    def _abnormal(self, row):
        """The rules a session's row breaks, in ABNORMAL_RULES' order."""
        th = self.abnormal
        fast = row["fast_loop"]
        got = ["fast_polling"] if fast and fast["span_s"] >= th["poll_min_s"] else []
        for rule, v in (("query_burst", row["queries_peak_per_min"]),
                        ("submit_burst", row["peak_per_min"]),
                        ("refused", row["rejected"]),
                        ("cancels", row["cancels"]),
                        ("failed_runs", row["runs_failed"])):
            if v >= th[rule]:
                got.append(rule)
        return got

    def result(self):
        rows, by_cls = [], {}
        for sk, g in self._sessions().items():
            ks = g["keys"]
            s0 = ks[0]
            out, tools, flags, fails = (collections.Counter() for _ in range(4))
            parts, q_by = collections.Counter(), collections.Counter()
            shapes, over = set(), False
            ts, sub_ts, sub_dropped = [], [], 0
            row = {"cls": s0.cls, "user": s0.user, "host": s0.host,
                   "agent_type": next((s.agent_type for s in ks if s.agent_type), None),
                   "session": "%s:%s" % (sk[1], sk[2]) if sk[0] == "a" else None,
                   "keys": len(ks), "fold": dict(g["via"]) or None}
            n = {"q": 0, "q_dropped": 0, "cancels": 0, "runs": 0, "runs_failed": 0,
                 "argv_req": 0, "gpus": 0, "gpu_h": 0.0, "gpu_jobs": 0, "gpu_h_known": 0}
            first = last = None
            example, example_t = None, None
            for s in ks:
                out.update(s.out)
                tools.update(s.tools)
                flags.update(s.flags)
                fails.update(s.fails)
                parts.update(s.parts)
                q_by.update(s.q_by)
                shapes |= s.shapes
                over = over or s.shapes_over
                ts.extend(s.q_ts)
                sub_ts.extend(s.sub_ts)
                sub_dropped += s.sub_dropped
                for f in n:
                    n[f] += getattr(s, f)
                if s.first is not None and (first is None or s.first < first):
                    first = s.first
                if s.last is not None and (last is None or s.last > last):
                    last = s.last
                # the latest JOB's command, whichever key submitted it
                if s.example_t is not None and (example_t is None or s.example_t > example_t):
                    example, example_t = s.example, s.example_t
            jobs = out["job"]
            asked = sum(out[o] for o in ("job", "rejected", "no_id"))
            ts.sort()
            sub_ts.sort()
            polls = self._polls(ts, self.abnormal["poll_gap_s"])
            row.update({
                "first": first, "last": last,
                "jobs": jobs, "outcomes": {o: out[o] for o in OUTCOMES if out[o]},
                "rejected": out["rejected"], "dry_runs": out["dry_run"],
                "tools": dict(tools.most_common(4)),
                # the most jobs in any 60 s; past MAX_SUBMIT_TS a key's jobs have
                # no time, and this is a floor
                "peak_per_min": self._in_a_minute(sub_ts),
                "jobs_unplaced": sub_dropped,
                "shapes": len(shapes), "shapes_capped": over,
                "flags": dict(flags),
                "argv_req_pct": pct(n["argv_req"], asked) if asked else None,
                "partitions": dict(parts.most_common(4)),
                "gpus": n["gpus"], "gpu_jobs": n["gpu_jobs"],
                "gpu_h": round(n["gpu_h"], 1) if n["gpu_h_known"] else None,
                "gpu_h_known": n["gpu_h_known"],
                "queries": n["q"], "queries_by": dict(q_by.most_common(4)),
                "queries_per_job": round(n["q"] / jobs, 1) if jobs else None,
                "polls": polls["polls"], "poll_gap_s": polls["median_gap_s"],
                "loop": polls["loop"], "queries_unplaced": n["q_dropped"],
                # the longest stretch of checks at most `poll_gap_s` apart
                "fast_loop": polls["fast"],
                # the most queue checks in any 60 s: past MAX_QUERY_TS a key's
                # checks have no time (`queries_unplaced`), and this is a floor
                "queries_peak_per_min": self._in_a_minute(ts),
                "cancels": n["cancels"], "runs": n["runs"], "runs_failed": n["runs_failed"],
                "fails": [{"why": w, "n": c} for w, c in fails.most_common(3)],
                "args": example,
            })
            row["abnormal"] = self._abnormal(row)
            rows.append(row)
            c = by_cls.setdefault(s0.cls, {
                "sessions": 0, "users": set(), "jobs": 0, "asked": 0, "rejected": 0,
                "dry_runs": 0, "steps": 0, "queries": 0, "q_by": collections.Counter(),
                "polls": 0, "polling": 0, "polling_s": 0.0, "cancels": 0, "runs": 0,
                "runs_failed": 0, "flags": collections.Counter(), "argv_req": 0,
                "parts": collections.Counter(), "gpus": 0, "gpu_jobs": 0, "gpu_h": 0.0,
                "gpu_h_known": 0, "peak_per_min": 0, "abnormal": 0,
                "abnormal_by": collections.Counter()})
            if jobs or n["q"] or n["cancels"] or n["runs"] or sum(out.values()):
                c["sessions"] += 1
                c["users"].add(s0.user)
            c["jobs"] += jobs
            c["asked"] += asked
            c["rejected"] += out["rejected"]
            c["dry_runs"] += out["dry_run"]
            c["steps"] += out["step"]
            c["queries"] += n["q"]
            c["q_by"].update(q_by)
            c["polls"] += polls["polls"]
            if polls["loop"] and polls["loop"]["polling"]:
                c["polling"] += 1
                c["polling_s"] += polls["loop"]["span_s"]
            c["cancels"] += n["cancels"]
            c["runs"] += n["runs"]
            c["runs_failed"] += n["runs_failed"]
            c["flags"].update(flags)
            c["argv_req"] += n["argv_req"]
            c["parts"].update(parts)
            for f in ("gpus", "gpu_jobs", "gpu_h", "gpu_h_known"):
                c[f] += n[f]
            c["peak_per_min"] = max(c["peak_per_min"], row["peak_per_min"])
            if row["abnormal"]:
                c["abnormal"] += 1
                c["abnormal_by"].update(row["abnormal"])

        classes = {}
        for cls, c in by_cls.items():
            classes[cls] = {
                "sessions": c["sessions"], "users": len(c["users"] - {None}),
                "jobs": c["jobs"], "asked": c["asked"], "rejected": c["rejected"],
                "rejected_pct": pct(c["rejected"], c["asked"]) if c["asked"] else None,
                "dry_runs": c["dry_runs"], "steps": c["steps"],
                "queries": c["queries"], "queries_by": dict(c["q_by"].most_common(8)),
                "queries_per_job": round(c["queries"] / c["jobs"], 1) if c["jobs"] else None,
                "polls": c["polls"], "polling_sessions": c["polling"],
                "polling_h": round(c["polling_s"] / 3600.0, 1),
                "cancels": c["cancels"], "runs": c["runs"], "runs_failed": c["runs_failed"],
                # shares of the submissions that asked for a job (dry runs and
                # steps into a held allocation excluded)
                "flags_pct": {f: pct(c["flags"][f], c["asked"]) for f, _ in FLAGS} if c["asked"] else {},
                "argv_req_pct": pct(c["argv_req"], c["asked"]) if c["asked"] else None,
                "partitions": dict(c["parts"].most_common(8)),
                "gpus": c["gpus"], "gpu_jobs": c["gpu_jobs"],
                "gpu_h": round(c["gpu_h"], 1) if c["gpu_h_known"] else None,
                "gpu_h_known": c["gpu_h_known"],
                "time_limit_s": self.time_limit[cls].as_dict() if cls in self.time_limit else None,
                "peak_per_min": c["peak_per_min"],
                # sessions that broke a rule, and each rule's, 0 included: the
                # class was judged
                "abnormal_sessions": c["abnormal"],
                "abnormal_by": {r: c["abnormal_by"][r] for r in ABNORMAL_RULES},
            }

        rows.sort(key=lambda r: (r["queries"] + 10 * (r["jobs"] + r["rejected"]) + r["runs"]),
                  reverse=True)
        series = self._series()
        fails = [{"cls": k[0], "tool": k[1], "why": k[2], "n": v, "eg": self.fail_eg.get(k)}
                 for k, v in self.fail_rows.most_common(FAIL_ROWS)]
        # The busiest are served, and past them every session that broke a rule
        # however quiet, up to ABNORMAL_ROWS more: the abnormal list is whole
        # unless `abnormal.served` says otherwise.
        busiest = rows[:ROWS]
        later = [r for r in rows[ROWS:] if r["abnormal"]]
        flagged = sum(1 for r in rows if r["abnormal"])
        return {
            "by_class": classes,
            "sessions": busiest + later[:ABNORMAL_ROWS], "sessions_total": len(rows),
            "sessions_busiest": len(busiest),
            "abnormal": {"rules": self.abnormal_rules(), "sessions": flagged,
                         "served": flagged - max(0, len(later) - ABNORMAL_ROWS)},
            "series": series, "rejections": fails,
            "basis": {
                "sessions": "an agent's own key and every key folded into it (the "
                            "process index, as the Sessions tab); people and IDEs one row per user",
                "poll": "queries within %gs are one poll; polls within %gs one loop; "
                        "a loop of %d min or more is counted as polling"
                        % (POLL_JOIN_S, LOOP_GAP_S, LOOP_MIN_S // 60),
                "requests": "argv only -- a #SBATCH line in the script is not seen",
                "queries": "every exit of %s" % ", ".join(sorted(SLURM_Q)),
                "abnormal": "a session breaks a rule when its own count reaches the rule's "
                            "threshold (reducers.slurm.abnormal); fast polling is its longest "
                            "stretch of checks at most poll_gap_s apart lasting poll_min_s; "
                            "a minute is any 60 s",
                "keys_dropped": self.keys_dropped,
            },
        }

    def _series(self):
        if not self.bins:
            return {"bin_s": self.bin_s, "t": [], "by_class": {}}
        lo, hi = min(self.bins), max(self.bins)
        lo = max(lo, hi - 4 * 240 + 1)
        idx = range(lo, hi + 1)
        classes = sorted({c for b in idx for c in self.bins.get(b, ())})
        out = {"bin_s": self.bin_s, "t": [b * self.bin_s for b in idx], "by_class": {}}

        # A bin no class has a Slurm record in is a gap: the feed may have been
        # down. One another class does have records in is a measured 0.
        def at(b, c, i):
            row = self.bins.get(b)
            return None if row is None else row.get(c, (0, 0))[i]
        for c in classes:
            out["by_class"][c] = {"jobs": [at(b, c, 0) for b in idx],
                                  "queries": [at(b, c, 1) for b in idx]}
        return out
