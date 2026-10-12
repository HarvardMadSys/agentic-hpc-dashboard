"""Merge-reduce: a window merged from hour states is the window one pass builds.

Every window reducer has `merge(other)`, and a window is built from saved
per-hour states (rc_dashboard/hours.py). These tests hold the two ways of
building a window to the same answer over the same records: one pass over a
stream, against the stream cut at every epoch hour, each hour reduced on its
own -- seeded with the join state the hour before ended with, then stripped of
it and saved, as the hour store does -- and the saved hours merged in order.

The stream is synthetic but shaped like the collector's: agents whose tool
calls, subshells and sandbox wrappers straddle the hour boundaries (so the
dedup index, the process index and the sandbox's wrapper join each cross an
hour), a session long enough to fill its run-length chain partway through an
hour, polling loops and job submissions, people's shells, and the per-host
residency ticks. Caps that a day never reaches are lowered here so the
merges past them are exercised too.

Everything must match exactly, except floats, which are summed in a different
order and may differ in the last place. The quantile sketches are themselves
exact under merge, so the quantiles match exactly too.

    cd dashboard && .venv/bin/python -m unittest discover -s tests -q
"""
import copy
import math
import pickle
import random
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from rc_dashboard import config as configmod       # noqa: E402
from rc_dashboard import hours, snapshots           # noqa: E402
from rc_dashboard.normalize import Normalizer       # noqa: E402
from rc_dashboard.reducers import Quant             # noqa: E402
from rc_dashboard.reducers import trajectories as trajmod   # noqa: E402
from rc_dashboard.windows import PanelView           # noqa: E402
from test_sessions import HOME, SNAP, rec            # noqa: E402

HOUR = 3600
# The present every result is computed at, so "the last 24 h" and the window
# stamps a result carries agree between the two builds.
NOW = (int(time.time()) // HOUR) * HOUR - HOUR
T0 = NOW - 4 * HOUR + 1234.5          # the stream's first record: mid-hour
T1 = NOW - 10.0                       # ... and its last
BOUNDS = [h * HOUR for h in range(int(T0 // HOUR) + 1, int(T1 // HOUR) + 1)]


def cfg():
    return configmod.Config(configmod.defaults(), {}, None)


def classes(c):
    return list(c.get("filters.classes")) + [c.get("filters.unlabeled_class")]


def plain(event, t, pid, ppid, command, args, user="bob", host="h2", actor3="human",
          **kw):
    """A record of a process no classifier tagged."""
    r = {"schema_version": 7, "event": event,
         "ts": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t)), "ts_epoch": t,
         "host": host, "user": user, "uid": 1001, "actor": actor3, "actor3": actor3,
         "agent_type": None, "pid": pid, "ppid": ppid, "command": command,
         "args": args, "cwd": "/home/%s/work" % user, "tty": "pts/3",
         "is_agent": False, "agent_pid": None, "session_key": None,
         "tree_root_pid": None, "depth": None, "sandbox": "unsandboxed",
         "sandbox_src": "none"}
    if event in ("exit", "truncated"):
        r.update(cpu_s=0.01, duration_s=0.5)
    r.update(kw)
    return r


def totals(t, host, n):
    return {"schema_version": 7, "event": "residency_totals",
            "ts": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t)), "ts_epoch": t,
            "host": host,
            "by_actor3": {"agent": {"n_procs": n, "threads": n, "rss_mb": 50.0 * n,
                                    "d_state": n % 2, "users": 1},
                          "human": {"n_procs": 3, "threads": 3, "rss_mb": 30.0,
                                    "d_state": 0, "users": 1}},
            "by_agent_type": {}, "trees": 1, "tracked_live": n}


def cost(rng):
    """The per-process measurements, some of them missing, as the collector's are."""
    d = rng.choice([0.05, 0.5, 2.0, 30.0])
    out = {"cpu_s": round(rng.random() * d, 4), "duration_s": d,
           "sched_run_s": round(rng.random() * d, 4) if rng.random() < .8 else None,
           "sched_wait_s": round(rng.random() * 0.1, 5) if rng.random() < .8 else None,
           "peak_rss_mb": round(rng.random() * 500, 1),
           "min_flt": rng.randrange(1000), "maj_flt": rng.choice([0, 0, 0, 1, 3, 40]),
           "nvcsw": rng.randrange(100), "nivcsw": rng.randrange(10),
           "blkio_wait_s": rng.choice([None, 0.0, 0.002]),
           "dstate_wait_s": rng.choice([None, 0.0, 0.0, 0.01, 0.5]),
           "dstate_episodes": rng.choice([None, 0, 1, 3]),
           "dstate_max_s": rng.choice([None, 0.01, 0.4]),
           "net_tx_bytes": rng.choice([None, 0, 1500, 90000]),
           "net_rx_bytes": rng.choice([None, 0, 2000]), "net_calls": rng.choice([None, 4])}
    if rng.random() < .7:
        out["io"] = {"rchar_mb": round(rng.random() * 9, 3), "wchar_mb": round(rng.random(), 3),
                     "rd_mb": round(rng.random() * 2, 3), "wr_mb": round(rng.random(), 3),
                     "scope": rng.choice(["process", "leader"])}
    return out


COMMANDS = [
    ("git", "git status"), ("cat", "cat %s/proj/README.md" % HOME),
    ("ls", "ls -la %s/proj/src" % HOME), ("rm", "rm -rf /tmp/agent-scratch"),
    ("cp", "cp %s/proj/a.txt /scratch/shared/out/" % HOME),
    ("python3", "python3 train.py --epochs 3"), ("pip", "pip install torch"),
    ("make", "make -j8"), ("sed", "sed -i s/a/b/ %s/proj/src/x.py" % HOME),
    ("curl", "curl -u bob:hunter22 https://example.org/x -o /tmp/x"),
    ("git", "git push --force origin main"), ("grep", "grep -rn TODO %s/proj" % HOME),
]


def agent_stream(rng, host="h1", root=100, base=10000, every=23.0):
    """A Claude Code session: ticks from its root, and Bash tool calls -- the
    tagged snapshot shell, the command, sometimes an untagged subshell with a
    tagged search under it, sometimes a bwrap-wrapped call. Children exit
    before the process that waited on them, as the collector writes them."""
    out = []
    kw = dict(agent_pid=root, tree_root=root, host=host)
    t = T0
    while t < T1 - 60:
        out.append(rec("conn", t, root, 50, "claude", "claude --dangerously-skip-permissions",
                       is_agent=True, agent_pid=root, tree_root=root, host=host))
        t += 300
    t = T0 + 3.0
    i = 0
    pid = base
    cwds = [HOME + "/proj", HOME + "/proj/src", "/scratch/shared/run%d" % (base % 7), "/"]
    while t < T1 - 30:
        comm, args = COMMANDS[i % len(COMMANDS)] if rng.random() < .8 else \
            rng.choice(COMMANDS)
        shell, child = pid, pid + 1
        cwd = rng.choice(cwds)
        kind = i % 5
        if kind == 3:
            # an untagged subshell between the shell and a tagged search: the
            # search asks the process index for its parent
            sub, rg = pid + 2, pid + 3
            out += [
                rec("exit", t, rg, sub, "2.1.291", "rg --files " + HOME + "/proj",
                    is_agent=True, cwd=cwd, **dict(kw, agent_pid=rg), **cost(rng)),
                rec("exit", t + 0.7, sub, shell, "zsh", "zsh -c true", cwd=cwd,
                    **dict(kw, agent_pid=shell), **cost(rng)),
                rec("exit", t + 1.1, shell, root, "zsh", SNAP + "'rg x'", is_agent=True,
                    cwd=cwd, **dict(kw, agent_pid=shell), **cost(rng)),
            ]
        elif kind == 4:
            # a bwrap-wrapped call: payload, the tool shell inside, the wrapper
            out += [
                rec("exit", t, child + 1, child, comm, args, cwd=cwd,
                    sandbox="sandboxed", sandbox_src="inherit",
                    **dict(kw, agent_pid=child), **cost(rng)),
                rec("exit", t + 0.4, child, shell, "zsh", SNAP + "'%s'" % args,
                    is_agent=True, cwd=cwd, sandbox="sandboxed", sandbox_src="userns:exec",
                    **dict(kw, agent_pid=child), **cost(rng)),
                rec("exit", t + 0.9, shell, root, "bwrap",
                    "bwrap --unshare-net --ro-bind / / -- /bin/zsh", agent_type="bwrap",
                    is_agent=True, exit_code=rng.choice([0, 0, 1]), cwd=cwd,
                    **dict(kw, agent_pid=shell), **cost(rng)),
            ]
        else:
            # a shell wrapper the dedup must match to its child
            wrap = kind == 1
            out += [
                rec("exit", t, child, shell, comm, args, cwd=cwd,
                    **dict(kw, agent_pid=shell), **cost(rng)),
                rec("exit", t + 0.6, shell, root, "bash" if wrap else "zsh",
                    ("bash -lc %s" % args) if wrap else SNAP + "'%s'" % args,
                    is_agent=not wrap, cwd=cwd, **dict(kw, agent_pid=shell), **cost(rng)),
            ]
        pid += 4
        i += 1
        t += every * (0.5 + rng.random())
    # the root exits at the very end, with its approval
    out.append(rec("exit", T1 - 5, root, 50, "claude", "claude --dangerously-skip-permissions",
                   is_agent=True, agent_pid=root, tree_root=root, host=host, **cost(rng)))
    return out


def straddles(rng, host="h1", root=100, base=500000):
    """Joins cut by every hour boundary: a child exits a moment before the
    hour turns and the process that waited on it a moment after."""
    out = []
    kw = dict(agent_pid=root, tree_root=root, host=host)
    for j, b in enumerate(BOUNDS):
        p = base + 10 * j
        cwd = HOME + "/proj"
        out += [
            # the shell-wrapper dedup across the seam
            rec("exit", b - 0.5, p + 1, p, "make", "make all", cwd=cwd,
                **dict(kw, agent_pid=p), **cost(rng)),
            rec("exit", b + 0.5, p, root, "bash", "bash -lc make all", cwd=cwd,
                **dict(kw, agent_pid=p), **cost(rng)),
            # the process index: a tagged search whose untagged parent exits
            # in the next hour
            rec("exit", b - 1.0, p + 3, p + 2, "2.1.291", "rg --files " + HOME,
                is_agent=True, cwd=cwd, **dict(kw, agent_pid=p + 3), **cost(rng)),
            rec("exit", b + 1.0, p + 2, p + 4, "zsh", "zsh -c true", cwd=cwd,
                **dict(kw, agent_pid=p + 4), **cost(rng)),
            rec("exit", b + 1.5, p + 4, root, "zsh", SNAP + "'rg x'", is_agent=True,
                cwd=cwd, **dict(kw, agent_pid=p + 4), **cost(rng)),
            # the sandbox: a payload before the hour, its wrapper after
            rec("exit", b - 0.3, p + 6, p + 5, "make", "make", cwd=cwd,
                sandbox="sandboxed", sandbox_src="inherit",
                **dict(kw, agent_pid=p + 5), **cost(rng)),
            rec("exit", b - 0.2, p + 5, p + 7, "zsh", SNAP + "'make'", is_agent=True,
                cwd=cwd, sandbox="sandboxed", sandbox_src="userns:exec",
                **dict(kw, agent_pid=p + 5), **cost(rng)),
            rec("exit", b + 0.3, p + 7, root, "bwrap",
                "bwrap --unshare-net --ro-bind / / -- /bin/zsh", agent_type="bwrap",
                is_agent=True, exit_code=0, cwd=cwd, **dict(kw, agent_pid=p + 7),
                **cost(rng)),
            # a key admitted in one hour, then a version probe under it in the
            # next: one pass keeps the probe, since the key is not new
            rec("exit", b - 2.0, p + 8, root, "zsh", SNAP + "'ls %s'" % cwd,
                is_agent=True, cwd=cwd, **dict(kw, agent_pid=p + 8), **cost(rng)),
            rec("exit", b + 2.0, p + 9, p + 8, "claude", "claude --version",
                is_agent=True, cwd=cwd, args_n=2, **dict(kw, agent_pid=p + 8),
                **cost(rng)),
        ]
    return out


def long_session(rng, host="h2"):
    """A person's shell long enough to fill its run-length chain mid-hour,
    alternating tools so every call is a new run."""
    out = []
    t = T0 + 1.0
    tools = ["ls", "vim", "ls", "git", "make", "ls", "python3"]
    k = 0
    while t < T1 - 5:
        tool = tools[k % len(tools)] if rng.random() < .9 else "htop"
        out.append(plain("exit", t, 70000 + k, 6000, tool, tool + " x", host=host,
                         **cost(rng)))
        k += 1
        t += 17 + rng.random() * 9
    return out


def slurm_stream(rng, host="h1", root=100):
    out = []
    kw = dict(agent_pid=root, tree_root=root, host=host)
    # a polling loop that runs across an hour boundary, every 5 s
    b = BOUNDS[0]
    t = b - 700
    k = 0
    while t < b + 900:
        out.append(rec("exit", t, 90000 + k, 89000, "squeue", "squeue -u alice",
                       **dict(kw, agent_pid=89000), **cost(rng)))
        k += 1
        t += 5 + rng.random()
    for j in range(40):
        t = T0 + 200 + j * (T1 - T0 - 400) / 40
        sub = {"schema_version": 7, "event": "submit",
               "ts": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t)),
               "ts_epoch": t, "host": host, "user": "alice", "uid": 1000,
               "actor": "agent", "actor3": "agent", "agent_type": "claude_code",
               "pid": 95000 + j, "ppid": 89000, "agent_pid": 89000,
               "session_key": "apid:89000", "tree_root_pid": root,
               "tool": "sbatch", "command": "sbatch",
               "args": "sbatch --parsable -p gpu --gres=gpu:%d -t 60 job%d.sh" % (1 + j % 3, j),
               "exit_code": 0 if j % 7 else 1,
               "job_id": 4000 + j if j % 7 else None,
               "captured_tail": "" if j % 7 else "sbatch: error: Invalid partition",
               "partition": "gpu", "gpus": 1 + j % 3, "time_limit_s": 3600 * (1 + j % 4),
               "req_src": "argv"}
        out.append(sub)
    return out


def infra_stream(rng, host="h1"):
    """A daemon-started background agent and a Remote Control worker: the
    cascades' nodes, ticking in every hour."""
    out = []
    t = T0 + 5
    while t < T1 - 5:
        out += [
            rec("residency", t, 300, 1, "claude", "claude daemon run", agent_pid=300,
                tree_root=300, is_agent=True, host=host, age_s=t - T0 + 100),
            rec("residency", t + 1, 310, 300, "claude",
                "claude --bg-pty-host --spawned-by {\"pid\": 300}", agent_pid=310,
                tree_root=300, is_agent=True, host=host, age_s=t - T0 + 50),
            rec("conn", t + 2, 320, 310, "claude", "claude -p summarise", agent_pid=320,
                tree_root=300, is_agent=True, host=host),
        ]
        t += 600
    return out


def stream(seed=7):
    rng = random.Random(seed)
    recs = agent_stream(rng) + agent_stream(rng, host="h2", root=200, base=20000,
                                            every=41.0)
    recs += straddles(rng) + long_session(rng) + slurm_stream(rng) + infra_stream(rng)
    t = T0
    while t < T1:
        recs += [totals(t, "h1", 3 + int(t) % 4), totals(t + 0.5, "h2", 2)]
        t += 60
    # people and an editor, with risky and costly commands
    for j in range(900):
        t = T0 + (j + 0.5) * (T1 - T0) / 900
        comm, args = COMMANDS[j % len(COMMANDS)]
        actor = "human" if j % 3 else "human-vscode"
        recs.append(plain("exit", t, 200000 + j, 199000 + j // 5, comm, args,
                          user=rng.choice(["bob", "carol", "dave"]), actor3=actor,
                          agent_type="vscode" if actor == "human-vscode" else None,
                          **cost(rng)))
    recs.sort(key=lambda r: r["ts_epoch"])
    return recs


def normalized(raw):
    """Normalise a fresh copy, with a shell resolver like the analyze helpers'
    (the builtin fallback resolves nothing, so the dedup would never fire)."""
    c = cfg()
    norm = Normalizer(c)
    norm.effective_comm = lambda comm, args: (
        args.split()[2] if comm == "bash" and args.startswith("bash -lc ") else comm)
    norm.shell_comms = frozenset({"bash"})
    out = []
    for r in copy.deepcopy(raw):
        n = norm.normalize(r)
        if n is not None:
            out.append(n)
    return out


def feed(red, recs):
    return hours.feed_all(red, ((r["_ts_epoch"], r) for r in recs))


def one_pass(recs, minutes=1440):
    c = cfg()
    view = PanelView(c, classes(c), minutes)
    view.reducers = view.fresh_reducers()
    feed(view.reducers, recs)
    return view


def hour_states(recs, carry_from=None):
    """The hours of `recs`, each reduced as the store reduces it: seeded with
    the join state the previous hour ended with, stripped of it, and saved
    (a pickle round trip). Returns the saved states and the last carry."""
    c = cfg()
    fresh = lambda: PanelView(c, classes(c), 10080).fresh_reducers()   # noqa: E731
    kinds = {k: type(r) for k, r in fresh().items()}
    by_hour = {}
    for r in recs:
        by_hour.setdefault(int(r["_ts_epoch"] // HOUR), []).append(r)
    carry, ages, saved = carry_from, {}, []
    for h in sorted(by_hour):
        red = hours.prepare(fresh())
        if carry is not None:
            hours.seed(red, carry)
        feed(red, by_hour[h])
        carry = hours.take_carry(red)
        hours.strip(red)
        saved.append(snapshots.capture(red))
        carry = pickle.loads(pickle.dumps(carry))
        carry, ages = hours.age(kinds, carry, ages, h)
    return saved, carry, fresh


def merged(saved, fresh, carry, minutes=1440, edge=(), tail=()):
    """A window: `edge` fed one by one, the saved hours merged, then the
    carried join state installed and `tail` fed one by one."""
    c = cfg()
    view = PanelView(c, classes(c), minutes)
    acc = view.reducers = view.fresh_reducers()
    feed(acc, edge)
    for blob in saved:
        part = snapshots.restore(fresh(), pickle.loads(blob))
        hours.merge_into(acc, part)
    hours.install(acc, carry)
    feed(acc, tail)
    return view


def materialize(view):
    with mock.patch("time.time", return_value=NOW):
        return pickle.loads(pickle.dumps(view.materialize()))


def diff(a, b, path="", out=None, limit=20):
    """Where two payloads differ; floats compared to the last few places."""
    out = [] if out is None else out
    if len(out) >= limit:
        return out
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b), key=str):
            if k not in a or k not in b:
                out.append("%s/%s: only in %s" % (path, k, "one pass" if k in a else "merged"))
            else:
                diff(a[k], b[k], "%s/%s" % (path, k), out, limit)
    elif isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        if len(a) != len(b):
            out.append("%s: length %d vs %d" % (path, len(a), len(b)))
        for i, (x, y) in enumerate(zip(a, b)):
            diff(x, y, "%s[%d]" % (path, i), out, limit)
    elif isinstance(a, float) or isinstance(b, float):
        if not (isinstance(a, (int, float)) and isinstance(b, (int, float))
                and math.isclose(a, b, rel_tol=1e-9, abs_tol=0.011)):
            out.append("%s: %r vs %r" % (path, a, b))
    elif a != b:
        out.append("%s: %r vs %r" % (path, a, b))
    return out


class Caps:
    """Lower the caps a day never reaches, so merging past them is tested."""

    def __enter__(self):
        self._p = [mock.patch.object(trajmod, "MAX_SEQ_RUNS", 25),
                   mock.patch.object(trajmod, "DRIFT_CAP", 60)]
        for p in self._p:
            p.start()
        return self

    def __exit__(self, *a):
        for p in self._p:
            p.stop()


class SketchTest(unittest.TestCase):
    def test_merged_sketches_are_the_sketch_one_pass_builds(self):
        rng = random.Random(3)
        vals = [rng.lognormvariate(0, 3) for _ in range(20000)] + [0.0] * 50 + [None] * 9
        one, a, b = Quant(), Quant(), Quant()
        for v in vals:
            one.add(v)
        for v in vals[:7001]:
            a.add(v)
        for v in vals[7001:]:
            b.add(v)
        a.merge(pickle.loads(pickle.dumps(b)))
        x, y = one.as_dict(), a.as_dict()
        self.assertEqual(diff(x, y), [])
        self.assertEqual(x["n"], 20050)

    def test_quantiles_are_within_the_stated_error(self):
        rng = random.Random(5)
        vals = [rng.lognormvariate(1, 2) for _ in range(30000)]
        q = Quant()
        for v in vals:
            q.add(v)
        got = q.as_dict()
        ex = sorted(vals)
        for p in (.10, .25, .50, .75, .90, .99):
            want = ex[int(p * (len(ex) - 1))]
            self.assertLessEqual(abs(got["p%d" % round(p * 100)] - want),
                                 Quant.REL_ERR * want + 1e-4, p)
        self.assertEqual(got["max"], round(max(vals), 4))
        self.assertAlmostEqual(got["mean"], sum(vals) / len(vals), places=3)
        self.assertFalse(got["sampled"])

    def test_an_all_zero_field_is_still_flagged(self):
        q = Quant()
        for _ in range(10):
            q.add(0)
        self.assertTrue(q.as_dict()["all_zero"])


class MergeEquivalenceTest(unittest.TestCase):
    """One pass over a stream against the same stream merged from its hours."""

    @classmethod
    def setUpClass(cls):
        with Caps():
            cls.raw = stream()
            recs = normalized(cls.raw)
            cls.recs = recs
            cls.single = materialize(one_pass(normalized(cls.raw)))
            saved, carry, fresh = hour_states(normalized(cls.raw))
            cls.hours_n = len(saved)
            cls.merged = materialize(merged(saved, fresh, carry))

    def test_the_stream_crosses_hours_and_exercises_every_join(self):
        self.assertGreaterEqual(self.hours_n, 4)
        tm = self.single["tool_mix"]
        self.assertGreater(tm["dedup"]["dropped"], len(BOUNDS))
        sb = self.single["sandbox"]
        self.assertGreater(sum(c.get("isolated", 0) for c in sb["tool_calls"].values()),
                           len(BOUNDS))
        t = self.single["trajectories"]
        self.assertTrue(any(r["chain_truncated"] for r in t["rows"].values()))
        self.assertGreater(self.single["slurm"]["by_class"]["agent"]["jobs"], 0)

    def test_every_reducer_matches_one_pass(self):
        with Caps():
            for key in sorted(self.single):
                with self.subTest(reducer=key):
                    self.assertEqual(diff(self.single[key], self.merged[key]), [])

    def test_a_join_across_the_seam_is_made_as_one_pass_makes_it(self):
        # Without the carried state each seam would keep a shell its child
        # already counted, and leave each straddling wrapper's payload as a
        # call of its own: check the merge is not passing by accident.
        with Caps():
            saved = []
            c = cfg()
            fresh = lambda: PanelView(c, classes(c), 10080).fresh_reducers()  # noqa: E731
            by_hour = {}
            for r in normalized(self.raw):
                by_hour.setdefault(int(r["_ts_epoch"] // HOUR), []).append(r)
            for h in sorted(by_hour):
                red = hours.prepare(fresh())
                feed(red, by_hour[h])
                hours.strip(red)
                saved.append(snapshots.capture(red))
            cold = materialize(merged(saved, fresh, None))
        self.assertLess(cold["tool_mix"]["dedup"]["dropped"],
                        self.single["tool_mix"]["dedup"]["dropped"])
        self.assertNotEqual(diff(self.single["sandbox"], cold["sandbox"]), [])


class WindowAssemblyTest(unittest.TestCase):
    """A window as the registry builds one: the partial first hour read
    record by record, the whole hours merged, the open hour read on top --
    against one pass over the window."""

    def test_edge_hours_and_tail_match_one_pass(self):
        with Caps():
            recs = normalized(stream(seed=11))
            first, last = int(T0 // HOUR) + 1, int(T1 // HOUR)
            edge = [r for r in recs if r["_ts_epoch"] < first * HOUR]
            mid = [r for r in recs if first * HOUR <= r["_ts_epoch"] < last * HOUR]
            tail = [r for r in recs if r["_ts_epoch"] >= last * HOUR]
            # the store reduces the edge's hour whole, carrying into the rest
            saved, carry_in, fresh = hour_states(edge)
            saved, carry, fresh = hour_states(mid, carry_from=carry_in)
            got = materialize(merged(saved, fresh, carry, edge=normalized(stream(11))[:len(edge)],
                                     tail=tail))
            want = materialize(one_pass(normalized(stream(seed=11))))
            for key in sorted(want):
                with self.subTest(reducer=key):
                    self.assertEqual(diff(want[key], got[key]), [])

    def test_a_merged_window_keeps_ingesting_as_one_pass_would(self):
        # After the merge the view is live-fed; the joins it makes then use the
        # installed carry -- a shell whose child is in the last merged hour.
        with Caps():
            recs = normalized(stream(seed=13))
            cut = BOUNDS[-1]
            head = [r for r in recs if r["_ts_epoch"] < cut]
            later = [r for r in recs if r["_ts_epoch"] >= cut]
            saved, carry, fresh = hour_states(head)
            got = materialize(merged(saved, fresh, carry, tail=later))
            want = materialize(one_pass(normalized(stream(seed=13))))
            self.assertEqual(diff(want, got), [])


class HourSeriesTest(unittest.TestCase):
    def test_minute_bins_fold_into_any_window_width(self):
        recs = normalized(stream(seed=17))
        saved, carry, fresh = hour_states(recs)
        for minutes in (60, 360, 1440, 10080):
            with self.subTest(minutes=minutes):
                want = materialize(one_pass(normalized(stream(17)), minutes))
                got = materialize(merged(saved, fresh, carry, minutes))
                for key in ("resources", "slurm"):
                    self.assertEqual(diff(want[key]["series"], got[key]["series"]), [])


if __name__ == "__main__":
    unittest.main()
