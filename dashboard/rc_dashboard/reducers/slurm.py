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
  sbatch's error, or an srun/salloc `error:` or allocation line; that line is
  its reason) or `no_id` (no id read: it exited 0, or an srun ran and failed);
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
MAX_SHAPES = 2000          # distinct command-line shapes held per key
ROWS = 200                 # sessions served
FAIL_ROWS = 20

_DIGITS = re.compile(rb"\d+")
_NUM = re.compile(r"\d+")
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07|[\x00-\x08\x0b-\x1f\x7f]")
# What the scheduler prints when it refuses. sbatch prints nothing else on a
# failure; srun and salloc also relay the payload's own output, and a program
# that ran and failed is not a refusal.
_REFUSAL = re.compile(r"(?:sbatch|srun|salloc): error|submission failed|Unable to allocate"
                      r"|allocation failure|violates|Invalid (?:account|partition|qos)"
                      r"|not available|presently disabled|denied", re.I)
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
            or _REFUSAL.search(rec.get("captured_tail") or "")):
        return "rejected"
    return "no_id"


def reason(rec):
    """The last line the scheduler printed, with ids and node numbers masked --
    and credentials, since it is served as text of its own."""
    lines = [x.strip() for x in _ANSI.sub("", rec.get("captured_tail") or "").splitlines()]
    lines = [x for x in lines if x]
    # the scheduler's own error line, else the last line printed: sbatch can
    # follow its error with an example script
    pick = next((x for x in lines if _REFUSAL.search(x)), lines[-1] if lines else None)
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
                 "flags", "argv_req", "shapes", "shapes_over", "minutes", "parts",
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
        self.minutes = collections.Counter()
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
        for k in ("out", "tools", "flags", "minutes", "parts", "q_by", "fails"):
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
    EVENTS = ("exit", "truncated", "submit")
    KEY = "slurm"

    def __init__(self, cfg, classes, window_s=86400, procs=None):
        self.cfg = cfg
        self.classes = list(classes)
        self.procs = procs
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
            self.fail_eg.setdefault(fk, {"user": rec.get("user"), "ts": rec.get("ts"),
                                         "args": args[:300]})
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
            s.example, s.example_t = args[:300], ts or 0
        if ts is not None:
            s.minutes[int(ts // 60)] += 1
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
        for k, eg in o.fail_eg.items():
            self.fail_eg.setdefault(k, eg)

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
    def _polls(ts):
        """Polls (queries joined within POLL_JOIN_S) and the longest loop."""
        if not ts:
            return {"polls": 0, "median_gap_s": None, "loop": None}
        ts = sorted(ts)
        starts = [ts[0]]
        for a, b in zip(ts, ts[1:]):
            if b - a > POLL_JOIN_S:
                starts.append(b)
        gaps = [b - a for a, b in zip(starts, starts[1:])]
        best, run = None, [starts[0]]
        for t in starts[1:] + [None]:
            if t is not None and t - run[-1] <= LOOP_GAP_S:
                run.append(t)
                continue
            if len(run) > 1 and (best is None or run[-1] - run[0] > best[-1] - best[0]):
                best = run
            if t is not None:
                run = [t]
        loop = None
        if best:
            g = sorted(b - a for a, b in zip(best, best[1:]))
            loop = {"from": best[0], "to": best[-1], "polls": len(best),
                    "span_s": round(best[-1] - best[0], 1),
                    "every_s": round(g[len(g) // 2], 1),
                    "polling": best[-1] - best[0] >= LOOP_MIN_S}
        return {"polls": len(starts),
                "median_gap_s": round(sorted(gaps)[len(gaps) // 2], 1) if gaps else None,
                "loop": loop}

    def result(self):
        rows, by_cls = [], {}
        for sk, g in self._sessions().items():
            ks = g["keys"]
            s0 = ks[0]
            out, tools, flags, fails = (collections.Counter() for _ in range(4))
            minutes, parts, q_by = (collections.Counter() for _ in range(3))
            shapes, over = set(), False
            ts = []
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
                minutes.update(s.minutes)
                parts.update(s.parts)
                q_by.update(s.q_by)
                shapes |= s.shapes
                over = over or s.shapes_over
                ts.extend(s.q_ts)
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
            polls = self._polls(ts)
            row.update({
                "first": first, "last": last,
                "jobs": jobs, "outcomes": {o: out[o] for o in OUTCOMES if out[o]},
                "rejected": out["rejected"], "dry_runs": out["dry_run"],
                "tools": dict(tools.most_common(4)),
                "peak_per_min": max(minutes.values()) if minutes else 0,
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
                "cancels": n["cancels"], "runs": n["runs"], "runs_failed": n["runs_failed"],
                "fails": [{"why": w, "n": c} for w, c in fails.most_common(3)],
                "args": example,
            })
            rows.append(row)
            c = by_cls.setdefault(s0.cls, {
                "sessions": 0, "users": set(), "jobs": 0, "asked": 0, "rejected": 0,
                "dry_runs": 0, "steps": 0, "queries": 0, "q_by": collections.Counter(),
                "polls": 0, "polling": 0, "polling_s": 0.0, "cancels": 0, "runs": 0,
                "runs_failed": 0, "flags": collections.Counter(), "argv_req": 0,
                "parts": collections.Counter(), "gpus": 0, "gpu_jobs": 0, "gpu_h": 0.0,
                "gpu_h_known": 0, "peak_per_min": 0})
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
            }

        rows.sort(key=lambda r: (r["queries"] + 10 * (r["jobs"] + r["rejected"]) + r["runs"]),
                  reverse=True)
        series = self._series()
        fails = [{"cls": k[0], "tool": k[1], "why": k[2], "n": v, "eg": self.fail_eg.get(k)}
                 for k, v in self.fail_rows.most_common(FAIL_ROWS)]
        return {
            "by_class": classes,
            "sessions": rows[:ROWS], "sessions_total": len(rows),
            "series": series, "rejections": fails,
            "basis": {
                "sessions": "an agent's own key and every key folded into it (the "
                            "process index, as Trajectories); people and IDEs one row per user",
                "poll": "queries within %gs are one poll; polls within %gs one loop; "
                        "a loop of %d min or more is counted as polling"
                        % (POLL_JOIN_S, LOOP_GAP_S, LOOP_MIN_S // 60),
                "requests": "argv only -- a #SBATCH line in the script is not seen",
                "queries": "every exit of %s" % ", ".join(sorted(SLURM_Q)),
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
