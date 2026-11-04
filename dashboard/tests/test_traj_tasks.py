"""A call's names and the agent's own words, and the tasks a row is cut into from them.

`phase_names.tagset` reads them from a call's masked text. The reducer interns each set
(`_tagsets`, `_tagset_id`) and the call holds its id (`TAGS`), as it holds the id of its preview.
0 is the empty set, and it is also the id of a set the reducer has no room for. The ids are kept
through a call that a process outlives (`ran_past`), the fold of an agent's day rows, the merge of
saved hours (renumbered, bounded, and counted), and a pickle.

A row's phase block then carries `tasks` and a `headline` (`phase_block`): the calls are cut into
tasks (`phase_tasks`), the phases are cut at their boundaries, and the tasks are named.

    cd dashboard && uv run python -m unittest discover -s tests -p 'test_traj_tasks.py' -v
"""
import json
import pickle
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_merge as TM                                                 # noqa: E402
from rc_dashboard import config as configmod                            # noqa: E402
from rc_dashboard import proctree, snapshots                            # noqa: E402
from rc_dashboard.normalize import Normalizer                           # noqa: E402
from rc_dashboard.reducers import phase_names, phase_segment, phase_tasks   # noqa: E402
from rc_dashboard.reducers import trajectories as trajmod               # noqa: E402
from rc_dashboard.reducers.phase_log import (FLAGS, OPEN, PAST, PREV, SEEN, TAGS,   # noqa: E402
                                             PhaseLog)
from rc_dashboard.windows import PanelView                              # noqa: E402
from test_sessions import HOME, SNAP, cfg, ranked, rec, reduce          # noqa: E402
from test_traj_phases import (T0, bash8, fed, quiet_t0, root8, row_of, s8,   # noqa: E402
                              seam)

EMPTY = ((), None)
T1 = quiet_t0(9000) + 200       # the records lie in [T1 - 200, T1 + 8800]: one day, over before now


def logged(red):
    """The one session of `red` that holds calls."""
    (s,) = [s for s in red.sessions.values() if s.phase.calls]
    return s


def ids_of(red):
    """The tag set id of each call of the one session that holds calls, in the log's order."""
    return [c[TAGS] for c in logged(red).phase.calls]


def tag_sets(red):
    """Each session's calls as the tag sets they name, in the order the log holds them: what the
    ids stand for, which two reducers can agree on while numbering them differently."""
    return {sid: [red._tagsets[c[TAGS]] for c in s.phase.calls]
            for sid, s in red.sessions.items()}


def resolved(red):
    """Each session's calls whole, with the ids of the preview and the tag set replaced by what
    they stand for: two reducers hold the same calls when these are equal."""
    def whole(c):
        return (c[:PREV] + (red._previews[c[PREV]],) + c[PREV + 1:TAGS]
                + (red._tagsets[c[TAGS]],) + c[TAGS + 1:])
    return {sid: [whole(c) for c in s.phase.calls] for sid, s in red.sessions.items()}


def consistent(red):
    """The table and its id map describe each other, and 0 is the empty set."""
    return (red._tagsets[0] == EMPTY
            and red._tagset_id == {ts: i for i, ts in enumerate(red._tagsets)})


def around_the_hour(before, after):
    """One agent whose calls run the commands `before` in the hour up to a boundary and `after` in
    the hour past it, one every 500 seconds, as the records come: in time order."""
    b = seam(5400)
    raw = [root8(b - 1800), root8(b + 100)]
    raw += [bash8(b - 1700 + 500 * i, 300 + i, cmd) for i, cmd in enumerate(before)]
    raw += [bash8(b + 200 + 500 * i, 400 + i, cmd) for i, cmd in enumerate(after)]
    return sorted(raw, key=lambda r: r["ts_epoch"])


def hours_of(raw):
    """`raw` reduced hour by hour as the store does it, and merged as a window is: the saved
    hours, a fresh reducer set, and the merged view."""
    saved, carry, fresh = TM.hour_states(TM.normalized(raw))
    assert len(saved) == 2, len(saved)
    return saved, fresh, TM.merged(saved, fresh, carry)


def fed_loop(poll="python poll_status.py"):
    """A reducer fed a tool shell (777) that never exits and runs `poll` and `sleep 20` for ten
    minutes: a call still running, read from its first child's text. Returns the reducer and the
    session."""
    recs = [root8(T0 - 100)]
    for i in range(30):
        t = T0 + 20 * i
        recs += [s8(t + 0.1, 600 + 2 * i, 777, "python", poll, duration_s=0.05, depth=2),
                 s8(t + 20, 601 + 2 * i, 777, "sleep", "sleep 20", duration_s=20, depth=2)]
    red = fed(sorted(recs, key=lambda r: r["ts_epoch"]))
    (s,) = red.sessions.values()
    return red, s


def open_call(red, s):
    """The one call still running under the session `s`, as a row's phases are cut from it."""
    calls, _ = red.phase_calls(s, [s.sid])
    (call,) = [c for c in calls if c[FLAGS] & OPEN]
    return call


class TagsTest(unittest.TestCase):
    def test_a_call_keeps_its_names_and_its_words(self):
        red = fed([root8(T0),
                   bash8(T0 + 5, 201, 'git commit -m "Fix the loader" && pytest tests/test_io.py')])
        (s,) = [s for s in red.sessions.values() if s.phase.calls]
        names, words = red._tagsets[s.phase.calls[0][TAGS]]
        self.assertIn(("test", "tests/test_io.py"), names)
        self.assertEqual(words, ("commit", "Fix the loader", ""))

    def test_a_call_that_ran_past_keeps_its_names(self):
        lg = PhaseLog()
        lg.add((0.0, 1.0, "run", 1, 0, 0, 7, None, 3), 10)
        self.assertTrue(lg.ran_past(7, 50.0))
        self.assertEqual(lg.calls[0][PAST:], (50.0, 3))

    def test_the_bound_counts_what_it_drops(self):
        with mock.patch.object(trajmod, "MAX_TAGSETS", 2):
            payload = reduce([root8(T0)]
                             + [bash8(T0 + 5 * i, 201 + i, "cat f%d.py" % i) for i in range(4)])
        self.assertEqual(payload["trajectories"]["buffer"]["phase_tag_overflow"], 3)

    def test_names_survive_the_hour_merge_and_a_pickle(self):
        # Hour one names a.py, the test, and the commit; hour two the test again, c.py, nothing,
        # and a job that ends in the same hour (so the call has a `past`). Hour two's own table
        # holds the test as 1 and c.py as 2, so the merge renumbers.
        b = seam(5400)
        raw = around_the_hour(
            ["cat a.py", "pytest tests/test_io.py", 'git commit -m "Fix the loader"'],
            ["pytest tests/test_io.py", "cat c.py", "ls"])
        raw += [bash8(b + 1700, 450, "nohup python train.py &"),
                s8(b + 2500, 451, 450, "python", "python train.py", duration_s=799.9, depth=2)]
        raw.sort(key=lambda r: r["ts_epoch"])
        one = TM.one_pass(TM.normalized(raw)).reducers["trajectories"]
        saved, fresh, view = hours_of(raw)
        merged = view.reducers["trajectories"]
        (calls,) = tag_sets(one).values()
        self.assertEqual([names for names, _words in calls],
                         [(("read", "a.py"),), (("test", "tests/test_io.py"),), (),
                          (("test", "tests/test_io.py"),), (("read", "c.py"),), (),
                          (("run", "train.py"),)])
        self.assertEqual(calls[2][1], ("commit", "Fix the loader", ""))
        self.assertEqual(tag_sets(merged), tag_sets(one))
        # every other field of a renumbered call stays: `past`, `pid`, `flags`, and the preview
        self.assertEqual(resolved(merged), resolved(one))
        self.assertEqual(logged(merged).phase.calls[-1][PAST], b + 2500)
        self.assertEqual((merged.tag_overflow, one.tag_overflow), (0, 0))
        self.assertTrue(consistent(merged))
        # not passing by accident: hour two's calls were numbered differently on their own
        hour2 = snapshots.restore(fresh(), pickle.loads(saved[1]))["trajectories"]
        self.assertTrue(consistent(hour2))
        own = ids_of(hour2)
        held = ids_of(merged)[-len(own):]
        self.assertNotEqual(own, held)
        self.assertEqual([hour2._tagsets[i] for i in own], [merged._tagsets[i] for i in held])
        # a pickle of the reducer: the same calls, and a table that goes on from where it was
        again = pickle.loads(pickle.dumps(one))
        self.assertEqual(tag_sets(again), tag_sets(one))
        self.assertEqual(resolved(again), resolved(one))
        self.assertTrue(consistent(again))
        self.assertEqual(again._intern_tagset(one._tagsets[2]), 2)
        new = ((("write", "z.py"),), None)
        self.assertEqual(again._intern_tagset(new), len(one._tagsets))
        self.assertEqual(again._tagsets[-1], new)
        self.assertEqual(again.tag_overflow, 0)

    # --------------------------------------------------------------- the layout
    def test_the_layout_later_code_builds_calls_by(self):
        # (start, end, cat, h, prev, flags, pid, past, tags), and an open call's seen after tags
        self.assertEqual((PAST, TAGS, SEEN), (7, 8, 9))
        red = fed([root8(T0), bash8(T0 + 5, 201, "cat a.py")])
        self.assertEqual(len(logged(red).phase.calls[0]), 9)
        self.assertEqual(len(open_call(*fed_loop())), 10)

    # ---------------------------------------------------------------- the table
    def test_the_empty_set_is_zero_and_always_held(self):
        red = fed([root8(T0), bash8(T0 + 5, 201, "ls"), bash8(T0 + 9, 202, "pwd")])
        self.assertEqual(red._tagsets, [EMPTY])
        self.assertEqual(ids_of(red), [0, 0])
        with mock.patch.object(trajmod, "MAX_TAGSETS", 1):          # full before anything is held
            self.assertEqual(red._intern_tagset(EMPTY), 0)
        self.assertEqual((red.tag_overflow, red._tagsets), (0, [EMPTY]))

    def test_a_call_whose_set_cannot_be_kept_has_the_empty_set(self):
        with mock.patch.object(trajmod, "MAX_TAGSETS", 2):
            red = fed([root8(T0)]
                      + [bash8(T0 + 5 * i, 201 + i, "cat f%d.py" % i) for i in range(4)])
        self.assertEqual(ids_of(red), [1, 0, 0, 0])
        self.assertEqual(red._tagsets, [EMPTY, ((("read", "f0.py"),), None)])
        self.assertEqual(red.tag_overflow, 3)

    def test_a_set_is_interned_once_and_counted_unless_it_is_a_read(self):
        red = fed([])
        one, two = ((("read", "a.py"),), None), ((("read", "b.py"),), None)
        with mock.patch.object(trajmod, "MAX_TAGSETS", 2):
            self.assertEqual([red._intern_tagset(one), red._intern_tagset(one)], [1, 1])
            self.assertEqual(red._intern_tagset(two, count=False), 0)
            self.assertEqual(red.tag_overflow, 0)
            self.assertEqual(red._intern_tagset(two), 0)
            self.assertEqual(red.tag_overflow, 1)
        self.assertEqual((red._tagsets, consistent(red)), ([EMPTY, one], True))

    # ------------------------------------------------------------------ masking
    def test_a_call_is_masked_once_for_its_preview_and_its_names(self):
        secret = "ghp_" + "a" * 36
        with mock.patch.object(trajmod, "redact", wraps=trajmod.redact) as ours, \
                mock.patch.object(phase_names, "redact", wraps=phase_names.redact) as theirs:
            red = fed([root8(T0), bash8(
                T0 + 5, 201, 'git commit -m "Use token %s for the push"' % secret)])
        self.assertEqual((ours.call_count, theirs.call_count), (1, 0))
        _names, words = red._tagsets[ids_of(red)[0]]
        self.assertIn(phase_names.MASK, words[1])
        self.assertNotIn("ghp_", repr((red._previews, red._tagsets)))

    # ------------------------------------------------------------ the life cycle
    def test_a_job_that_outlives_its_call_leaves_the_call_its_names(self):
        red = fed([root8(T0), bash8(T0 + 5, 6000, "nohup python train.py &"),
                   s8(T0 + 1800, 6001, 6000, "python", "python train.py", duration_s=1795.0,
                      depth=2)])
        (call,) = logged(red).phase.calls
        self.assertEqual(call[PAST], T0 + 1800)
        self.assertEqual(red._tagsets[call[TAGS]], ((("run", "train.py"),), None))

    def test_an_open_call_names_what_its_text_names(self):
        red, s = fed_loop()
        call = open_call(red, s)
        self.assertEqual(red._tagsets[call[TAGS]], ((("run", "poll_status.py"),), None))
        self.assertEqual(call[SEEN], ("python", "sleep"))
        self.assertEqual(red._previews[call[PREV]], "python poll_status.py")
        self.assertTrue(consistent(red))

    def test_an_open_calls_text_is_masked_once_for_its_preview_and_its_names(self):
        secret = "ghp_" + "a" * 36
        red, s = fed_loop("python poll_status.py %s" % secret)
        self.assertIn(secret, repr(red.pend.entries))               # the join holds it raw
        with mock.patch.object(trajmod, "redact", wraps=trajmod.redact) as ours, \
                mock.patch.object(phase_names, "redact", wraps=phase_names.redact) as theirs:
            call = open_call(red, s)
        self.assertEqual(len([a for a in ours.call_args_list if "poll_status" in a.args[0]]), 1)
        self.assertEqual(theirs.call_count, 0)
        self.assertEqual(red._tagsets[call[TAGS]], ((("run", "poll_status.py"),), None))
        self.assertIn(phase_names.MASK, red._previews[call[PREV]])
        self.assertNotIn("ghp_", repr((red._previews, red._tagsets)))

    def test_an_open_call_whose_set_cannot_be_kept_is_not_counted(self):    # a read, not a record
        red, s = fed_loop()
        self.assertEqual(red._tagsets, [EMPTY])
        with mock.patch.object(trajmod, "MAX_TAGSETS", 1):
            call = open_call(red, s)
        self.assertEqual(call[TAGS], 0)
        self.assertEqual((red.tag_overflow, red._tagsets), (0, [EMPTY]))

    def test_a_fold_of_an_agents_day_rows_keeps_each_calls_names(self):
        midnight = time.mktime(time.localtime(time.time() - 7200)[:3] + (0, 0, 0, 0, 0, -1))
        red = fed([root8(midnight - 400), bash8(midnight - 300, 201, "pytest tests/test_io.py"),
                   root8(midnight + 100), bash8(midnight + 300, 202, "cat notes.md")])
        self.assertEqual(len([s for s in red.sessions.values() if s.phase.calls]), 2)   # two rows
        ((folded, _via, _keys),) = red.agent_sessions().values()
        self.assertEqual([red._tagsets[c[TAGS]] for c in folded.phase.calls],
                         [((("test", "tests/test_io.py"),), None), ((("read", "notes.md"),), None)])

    def test_a_merge_counts_the_sets_it_cannot_keep(self):
        # A table of three holds the empty set and two more. Hour one names a.py and b.py. Hour
        # two names a.py, c.py, and d.py: on its own it drops d.py, and the merge has no room
        # for c.py either.
        raw = around_the_hour(["cat a.py", "cat b.py"], ["cat a.py", "cat c.py", "cat d.py"])
        with mock.patch.object(trajmod, "MAX_TAGSETS", 3):
            one = TM.one_pass(TM.normalized(raw)).reducers["trajectories"]
            saved, fresh, view = hours_of(raw)
            merged = view.reducers["trajectories"]
            hour2 = snapshots.restore(fresh(), pickle.loads(saved[1]))["trajectories"]
            self.assertEqual(hour2.tag_overflow, 1)
            payload = TM.materialize(view)
        self.assertEqual(ids_of(one), [1, 2, 1, 0, 0])
        self.assertEqual(ids_of(merged), ids_of(one))
        self.assertEqual((one.tag_overflow, merged.tag_overflow), (2, 2))
        self.assertEqual(payload["trajectories"]["buffer"]["phase_tag_overflow"], 2)
        self.assertEqual(tag_sets(merged), tag_sets(one))
        self.assertTrue(consistent(merged))


# ------------------------------------------------------------------------------ a row's tasks
def configured(phases=None, tasks=None):
    """The default config, with some phase or task thresholds changed."""
    tree = configmod.defaults()
    tree["reducers"]["trajectories"]["phases"].update(phases or {})
    tree["reducers"]["trajectories"]["tasks"].update(tasks or {})
    return configmod.Config(tree, {}, None)


def row_after(recs, **thresholds):
    """The one agent row of a reducer fed `recs`, as the page lists it."""
    return row_of(fed(recs, configured(**thresholds)).result())


class Live:
    """A reducer set fed in steps, as a live view is."""

    def __init__(self):
        c = cfg()
        view = PanelView(c, TM.classes(c), 1440)
        self.red = view.reducers = view.fresh_reducers()
        self.traj = self.red["trajectories"]
        self.norm = Normalizer(c)

    def feed(self, recs):
        dispatch = PanelView.build_dispatch(self.red)
        for r in recs:
            for x in dispatch.get(r["event"], ()):
                x.feed(self.norm.normalize(r))
        return self


def call7(t, pid, cmd, root=100):
    """A Claude Code Bash call as schema 7 records it: its tool shell is tagged, so it is a
    collector key of its own (`agent_pid`), and the command it ran exits just before it."""
    return [rec("exit", t - 0.5, pid + 1, pid, cmd.split()[0], cmd, agent_pid=pid,
                tree_root=root),
            rec("exit", t, pid, root, "zsh", SNAP + "'%s'" % cmd, agent_pid=pid, tree_root=root,
                is_agent=True)]


def direct(traj, sid, step_s):
    """`(tasks, headline, phases)` of row `sid` from the pipeline's own functions, called as their
    modules document, with one `step_s` for the phases, the cut, and the naming. The project is the
    folder the test records are in. The row is of one day, so the reducer's one addition to the
    phases, their `sid`, is None."""
    s, _via, _keys, members = traj.lookup(sid)
    calls, extra = traj.phase_calls(s, members)
    params = dict(traj.phase_params, step_s=step_s)
    prep = phase_segment.prepared(calls, traj._previews, params)
    spans = phase_tasks.task_spans(prep, traj._tagsets, phase_tasks.DEFAULTS, step_s)
    block = phase_segment.segment(
        calls, s.phase, traj._previews, params, guess=True,
        extra_hk={phase_segment.NEVER_EXITED: extra} if extra else None,
        prepared=prep, cuts=[x.t0 for x in spans[1:]])
    tasks, headline = phase_tasks.name_tasks(
        spans, block, prep, traj._tagsets, traj._previews, phase_tasks.DEFAULTS, "proj",
        s.phase.cut_at, step_s, user=s.user)
    return tasks, headline, [dict(p, sid=None) for p in block["list"]]


def labels(tasks):
    return [t["label"] for t in tasks]


class RowTasksTest(unittest.TestCase):
    def two_tasks(self):
        """A test file run 25 times, then a script 25 times an hour and a half later."""
        return ([root8(T1)]
                + [bash8(T1 + 30 * i, 201 + i, "pytest tests/test_a.py") for i in range(25)]
                + [bash8(T1 + 5000 + 30 * i, 301 + i, "python b.py") for i in range(25)])

    def test_an_agent_row_has_tasks_and_every_phase_names_one(self):
        payload = reduce(self.two_tasks())
        block = row_of(payload)["phases"]
        self.assertEqual(block["v"], 2)
        self.assertEqual(block["basis"],
                         "read from the commands the agent ran and the words it wrote in them")
        self.assertEqual([t["kind"] for t in block["tasks"]], ["task", "task"])
        self.assertTrue(all(p["task"] in (0, 1) for p in block["list"]))
        # the tasks tile the phases, in order
        (first, mid), (mid2, last) = [t["phases"] for t in block["tasks"]]
        self.assertEqual((first, mid, last), (0, mid2, len(block["list"])))
        self.assertIn("tasks", payload["trajectories"]["basis"])
        # the block goes out as JSON: no NaN, and nothing a round trip would change
        served = {"tasks": block["tasks"], "headline": block["headline"]}
        self.assertEqual(json.loads(json.dumps(served, allow_nan=False)), served)

    def test_a_commit_names_its_task(self):
        cmds = ["cat a.py", "python a.py", "pytest tests/test_a.py", "head a.py", "tail a.py",
                "python a.py -v"]
        recs = [root8(T1)] + [bash8(T1 + 47 * i, 201 + i, cmd) for i, cmd in enumerate(cmds)]
        recs.append(bash8(T1 + 47 * 6, 210, 'git commit -m "Fix the loader"'))
        (task,) = row_of(reduce(recs))["phases"]["tasks"]
        self.assertEqual((task["source"], task["label"], task["ref"]),
                         ("commit", "Fix the loader", ""))

    def test_the_configured_task_thresholds_are_the_task_defaults(self):
        self.assertEqual(configmod.defaults()["reducers"]["trajectories"]["tasks"],
                         phase_tasks.DEFAULTS)

    def test_the_configured_task_thresholds_cut_the_tasks(self):
        self.assertEqual(len(row_after(self.two_tasks())["phases"]["tasks"]), 2)
        merged = row_after(self.two_tasks(), tasks={"gap_s": 100000})["phases"]["tasks"]
        self.assertEqual(len(merged), 1)

    def test_a_row_with_no_calls_has_no_tasks(self):
        chore = s8(T0 + 10, 401, 100, "git", "git -c core.fsmonitor= status --porcelain")
        block = row_of(reduce([root8(T0), chore]))["phases"]
        self.assertEqual((block["n_calls"], block["tasks"]), (0, []))
        self.assertIn("headline", block)

    def test_the_tasks_follow_the_calls_a_row_is_fed(self):
        recs = self.two_tasks()
        live = Live().feed(recs[:26])                       # the root and the first 25 calls
        self.assertEqual(len(row_of(live.traj.result())["phases"]["tasks"]), 1)
        live.feed(recs[26:])
        self.assertEqual(len(row_of(live.traj.result())["phases"]["tasks"]), 2)

    def test_a_boundary_with_no_gap_cuts_the_phases_too(self):
        # Twelve reads of a.py a minute apart, then twelve of b.py: a name new to the session
        # that recurs starts a task, though there is no gap. Both are reading, and without the cut
        # they would be one phase.
        recs = [root8(T1 - 100)]
        for name, start, pid in (("a.py", T1, 201), ("b.py", T1 + 720, 301)):
            recs += [bash8(start + 60 * i, pid + i, "sed -n %d,%dp %s" % (10 * i + 1, 10 * i + 10,
                                                                          name))
                     for i in range(12)]
        block = row_of(reduce(recs))["phases"]
        self.assertEqual(labels(block["tasks"]), ["Read a.py", "Read b.py"])
        self.assertEqual([(p["kind"], p["n"], p["task"]) for p in block["list"]],
                         [("explore", 12, 0), ("explore", 12, 1)])
        self.assertEqual([t["phases"] for t in block["tasks"]], [[0, 1], [1, 2]])

    # --------------------------------------------------------- past the call cap, and the live edge
    def cut_session(self):
        """Twelve calls in two stretches an hour and a half apart: three reads of a.py, then eight
        quick reads of b.py and one long run that began first and exits last."""
        recs = ([root8(T1 - 100)]
                + [bash8(T1 + 60 * i, 201 + i, cmd) for i, cmd in
                   enumerate(["cat a.py", "head a.py", "tail a.py"])]
                + [bash8(T1 + 5400, 300, "python b.py", duration_s=400)]
                + [bash8(T1 + 5100 + 30 * i, 301 + i, "cat b%d.py" % i) for i in range(8)])
        return sorted(recs, key=lambda r: r["ts_epoch"])

    def test_tasks_after_the_cut_say_so(self):
        # The log holds five calls: the three reads and two quick ones. The long run, which began
        # before them, exits after, and is one of the seven counted and dropped. So the log is
        # complete up to where the run began, and the second task, which lasts past that, says so.
        block = row_after(self.cut_session(), phases={"max_calls": 5})["phases"]
        self.assertEqual((block["n_calls"], block["cut"]), (12, 7))
        self.assertAlmostEqual(block["cut_at"], T1 + 5000, places=2)
        self.assertEqual([t["kind"] for t in block["tasks"]], ["task", "task"])
        self.assertEqual([t["incomplete"] for t in block["tasks"]], [False, True])
        # not by accident: with every call held there is no cut, and no task says so
        whole = row_after(self.cut_session())["phases"]
        self.assertEqual((whole["cut"], whole["cut_at"]), (0, None))
        self.assertEqual([t["incomplete"] for t in whole["tasks"]], [False] * len(whole["tasks"]))

    def test_the_last_task_is_incomplete_when_the_log_cut_calls_after_it(self):
        # Three stretches an hour apart, nine reads each, and three more reads in the third. The
        # calls come in time order and the log holds the first 27: every one it holds ends before
        # the first dropped call starts, which may continue the third task. Only the third says so.
        recs = [root8(T1 - 100)]
        for k, name in enumerate("abc"):
            recs += [bash8(T1 + 3900 * k + 20 * i, 201 + 20 * k + i, "cat %s%d.py" % (name, i))
                     for i in range(12 if name == "c" else 9)]
        block = row_after(recs, phases={"max_calls": 27})["phases"]
        self.assertEqual((block["n_calls"], block["cut"]), (30, 3))
        self.assertEqual([t["kind"] for t in block["tasks"]], ["task"] * 3)
        self.assertLess(block["tasks"][-1]["t1"], block["cut_at"])
        self.assertEqual([t["incomplete"] for t in block["tasks"]], [False, False, True])
        # not by accident: with every call held there is no cut, and no task says so
        whole = row_after(recs)["phases"]
        self.assertEqual([t["incomplete"] for t in whole["tasks"]], [False] * len(whole["tasks"]))

    def test_the_live_edge_task_is_open(self):
        # A tool shell that never exits runs a poll and `sleep 20` through the second stretch: a
        # call still running, named by its first child's text.
        recs = [root8(T1 - 100)]
        recs += [bash8(T1 + 45 * i, 201 + i, "cat a%d.py" % i) for i in range(10)]
        recs += [bash8(T1 + 5000 + 45 * i, 301 + i, "cat b%d.py" % i) for i in range(10)]
        for i in range(30):
            t = T1 + 5000 + 20 * i
            recs += [s8(t + 0.1, 600 + 2 * i, 777, "python", "python poll_status.py",
                        duration_s=0.05, depth=2),
                     s8(t + 20, 601 + 2 * i, 777, "sleep", "sleep 20", duration_s=20, depth=2)]
        block = row_after(sorted(recs, key=lambda r: r["ts_epoch"]))["phases"]
        self.assertEqual([t["open"] for t in block["tasks"]], [False, True])
        self.assertIn("poll_status.py", block["tasks"][-1]["names"])
        served = {"tasks": block["tasks"], "headline": block["headline"]}
        self.assertEqual(json.loads(json.dumps(served, allow_nan=False)), served)

    def test_a_gap_lists_the_call_still_running_through_it(self):
        # A tool shell loops from a minute before the first stretch through the hour and a half
        # between it and the second. Its call is one the agent moved past, and the gap lists it
        # with no preview: a call still running has no command of its own, only its first child's
        # text. The gap line names it as the tasks read it, by the script that text runs.
        recs = [root8(T1 - 100)]
        recs += [bash8(T1 + 45 * i, 201 + i, "cat a%d.py" % i) for i in range(10)]
        recs += [bash8(T1 + 5000 + 45 * i, 301 + i, "cat b%d.py" % i) for i in range(10)]
        for i in range(270):
            t = T1 - 60 + 20 * i
            recs += [s8(t + 0.1, 1000 + 2 * i, 777, "python", "python poll_status.py",
                        duration_s=0.05, depth=2),
                     s8(t + 20, 1001 + 2 * i, 777, "sleep", "sleep 20", duration_s=20, depth=2)]
        block = row_after(sorted(recs, key=lambda r: r["ts_epoch"]))["phases"]
        first, second = block["tasks"]
        self.assertIsNone(first["gap"])
        self.assertEqual(len(second["gap"]["alive"]), 1)
        self.assertIsNone(second["gap"]["alive"][0][0])
        self.assertEqual(second["gap"]["alive"][0][2], "poll_status.py")
        served = {"tasks": block["tasks"], "headline": block["headline"]}
        self.assertEqual(json.loads(json.dumps(served, allow_nan=False)), served)

    def test_calls_with_no_text_make_a_task_that_says_so(self):
        # Schema 7 can record a tool shell's exit before its argv: the call ran, and nothing says
        # what it was.
        recs = [root8(T1)] + [s8(T1 + 60 * i, 201 + i, 100, "bash", "", duration_s=5.0)
                              for i in range(10)]
        (task,) = row_of(reduce(recs))["phases"]["tasks"]
        self.assertEqual((task["label"], task["names"], task["n"]),
                         (phase_tasks.NOT_RECORDED, [], 10))

    # ------------------------------------------------------------------------- one step_s
    def same_as_direct(self, recs, step_s):
        """The row of `recs` at `step_s`, which equals the pipeline's own at that `step_s`."""
        traj = fed(recs, configured(phases={"step_s": step_s}))
        row = row_of(traj.result())
        block = row["phases"]
        self.assertEqual((block["tasks"], block["headline"], block["list"]),
                         direct(traj, row["sid"], step_s))
        return traj, row

    def test_a_longer_step_names_the_tasks_by_that_step(self):
        # x.py is read by six calls within 15 s, y.py by three calls ten minutes apart. By 1 s
        # steps x.py is in six and y.py in three, and by 30 s steps x.py is in one and y.py in
        # three.
        recs = [root8(T1 - 100)]
        recs += [bash8(T1 + 3 * k, 201 + k, "%s x.py" % cmd)
                 for k, cmd in enumerate(["cat", "head", "tail", "wc", "nl", "less"])]
        recs += [bash8(T1 + 600 * (k + 1), 211 + k, "%s y.py" % cmd)
                 for k, cmd in enumerate(["cat", "head", "tail"])]
        traj, row = self.same_as_direct(recs, 30.0)
        self.assertEqual(labels(row["phases"]["tasks"]), ["Read y.py · x.py"])
        # not by accident: one step of 1 s names the task the other way round
        self.assertEqual(labels(direct(traj, row["sid"], 1.0)[0]), ["Read x.py · y.py"])

    def test_a_longer_step_cuts_the_tasks_by_that_step(self):
        # A long run and a quick call ten seconds after it. By 1 s steps the agent has moved past
        # the run, which holds nothing, and the gap to the next call is over an hour. By 30 s
        # steps they are one step, the run holds the gap, and there is no cut.
        recs = [root8(T1 - 100)]
        recs += [bash8(T1 + 40 * i, 201 + i, "cat a%d.py" % i) for i in range(10)]
        recs += [bash8(T1 + 410, 301, "ls"),
                 bash8(T1 + 4400, 302, "python long.py", duration_s=4000)]
        recs += [bash8(T1 + 4500 + 40 * i, 311 + i, "cat b%d.py" % i) for i in range(10)]
        traj, row = self.same_as_direct(sorted(recs, key=lambda r: r["ts_epoch"]), 30.0)
        self.assertEqual(len(row["phases"]["tasks"]), 1)
        self.assertEqual(len(direct(traj, row["sid"], 1.0)[0]), 2)

    # ------------------------------------------------------------------------ the project
    def test_the_project_is_the_launch_folder(self):
        recs = [root8(T1)] + [bash8(T1 + 60 * i, 201 + i, cmd)
                              for i, cmd in enumerate(["ls", "ls -la", "ls src"])]
        row = row_of(reduce(recs))
        self.assertEqual(row["drift"]["cwd_anchor"], HOME + "/proj")
        self.assertEqual(row["phases"]["headline"], "proj")
        self.assertEqual(labels(row["phases"]["tasks"]), ["Read proj"])

    def test_a_launch_folder_named_for_the_user_gives_way_to_the_folder_cd_into_most(self):
        # The user's own folder is no project: a home directory (the user's or another's, as
        # `/home/<x>` or `/n/home<NN>/<x>`), or any folder named for the user. The row's user is
        # alice, and her commands change into `work`.
        cmds = ["cd /home/alice/work && ls", "cd /home/alice/work && ls -la",
                "cd /home/alice/work && ls src"]
        for cwd in (HOME, "/n/home04/alice", "/home/bob", "/n/home04/bob",
                    "/n/netscratch/lab/Lab/alice"):
            with self.subTest(cwd=cwd):
                recs = [root8(T1, cwd=cwd)] + [bash8(T1 + 60 * i, 201 + i, cmd, cwd=cwd)
                                               for i, cmd in enumerate(cmds)]
                row = row_of(reduce(recs))
                self.assertEqual(row["user"], "alice")
                self.assertEqual(row["phases"]["headline"], "work")

    def test_a_launch_folder_named_for_a_person_gives_way_to_the_folder_cd_into_most(self):
        # Named for a person: another's folder in a lab tree, or one with the user's name as a part
        # of its own. On boslogin06 on 2026-10-09 tpoe's agent was launched in
        # `/n/netscratch/alpha_lab/Lab/jdoe`, and rroe's in `…/Lab/netscratch_rroe`.
        def headline(cwd, cmds):
            recs = [root8(T1, cwd=cwd)] + [bash8(T1 + 60 * i, 201 + i, cmd, cwd=cwd)
                                           for i, cmd in enumerate(cmds)]
            return row_of(reduce(recs))["phases"]["headline"]

        work = ["cd /x/work && ls", "cd /x/work && ls -la", "cd /x/work && ls src"]
        for cwd in ("/n/netscratch/alpha_lab/Lab/jdoe",
                    "/n/netscratch/gamma_lab/Lab/netscratch_alice", "/data/alice_runs",
                    "/data/runs-alice.v2", "/n/holylabs/lab_x/Users/carol"):
            with self.subTest(cwd=cwd):
                self.assertEqual(headline(cwd, work), "work")
        # a folder changed into that is named for one of them is passed over too
        lab = "/n/netscratch/alpha_lab/Lab/jdoe"
        self.assertEqual(headline(lab, ["cd %s && ls" % lab, "cd %s && ls -la" % lab,
                                        "cd /x/netscratch_alice && ls", "cd /x/work && ls"]),
                         "work")
        # a project in a person's folder is a project, and a name run into another word is a name
        for cwd, project in (("/n/netscratch/lab/Lab/carol/cbm", "cbm"),
                             ("/data/alicefold", "alicefold")):
            with self.subTest(cwd=cwd):
                self.assertEqual(headline(cwd, work), project)

    def test_a_real_launch_folder_keeps_its_name_beside_a_cd_subfolder(self):
        # `proj` is not alice's home directory, though it lies in it, and `wt` is only where
        # the commands went
        cmds = ["cd /home/alice/proj/wt && ls", "cd /home/alice/proj/wt && ls -la",
                "cd /home/alice/proj/wt && ls src"]
        recs = [root8(T1)] + [bash8(T1 + 60 * i, 201 + i, cmd) for i, cmd in enumerate(cmds)]
        row = row_of(reduce(recs))
        self.assertEqual(row["drift"]["cwd_anchor"], HOME + "/proj")
        self.assertEqual(row["phases"]["headline"], "proj")

    def test_a_row_whose_only_folder_is_the_users_home_has_no_project(self):
        # It names nothing, so a built label is the verb alone, and there is no headline
        for cwd in (HOME, "/n/home04/alice"):
            with self.subTest(cwd=cwd):
                recs = [root8(T1, cwd=cwd)] + [bash8(T1 + 60 * i, 201 + i, cmd, cwd=cwd)
                                               for i, cmd in enumerate(["ls", "ls -la", "ls src"])]
                block = row_of(reduce(recs))["phases"]
                self.assertEqual(labels(block["tasks"]), ["Read"])
                self.assertIsNone(block["headline"])

    def test_a_cd_folder_named_for_the_user_is_no_project_either(self):
        # The commands change into alice's own folder most, and into `work` once: she has no
        # project there, and the one name that is not hers is the project
        home = ["cd /n/home04/alice && ls", "cd /n/home04/alice && ls -la",
                "cd /n/home04/alice && ls src"]
        for cmds, expected in ((home + ["cd /home/alice/work && ls"], "work"), (home, None)):
            with self.subTest(folders=len(cmds)):
                recs = [root8(T1, cwd=HOME)] + [bash8(T1 + 60 * i, 201 + i, cmd, cwd=HOME)
                                                for i, cmd in enumerate(cmds)]
                self.assertEqual(row_of(reduce(recs))["phases"]["headline"], expected)

    def test_a_name_that_is_the_rows_user_names_no_task(self):
        # The reducer hands the naming the row's user. Here the first stretch only changes into
        # alice's home, and its one name, `alice`, named its task until 2026-11-03 ("Read alice")
        cwd = HOME + "/proj"
        recs = [root8(T1, cwd=cwd)]
        recs += [bash8(T1 + 60 * i, 201 + i, "cd /n/home04/alice && ls x%d" % i, cwd=cwd)
                 for i in range(10)]
        recs += [bash8(T1 + 6000 + 60 * i, 301 + i, "cat b%d.py" % i, cwd=cwd) for i in range(10)]
        tasks = row_of(reduce(recs))["phases"]["tasks"]
        self.assertEqual([(t["kind"], t["label"], t["names"]) for t in tasks][0],
                         ("task", "Read proj", []))

    def test_the_project_follows_the_agents_own_cwd(self):
        # The calls are the first records, and name their own cwd. The agent's record comes after,
        # with the folder it was launched in, and the row is read again with no new call.
        live = Live().feed([bash8(T1 + 60 * i, 201 + i, cmd, cwd=HOME + "/alpha")
                            for i, cmd in enumerate(["ls", "ls -la", "ls src"])])
        self.assertEqual(row_of(live.traj.result())["phases"]["headline"], "alpha")
        live.feed([root8(T1 + 200, cwd=HOME + "/beta")])
        row = row_of(live.traj.result())
        self.assertEqual(row["drift"]["cwd_anchor"], HOME + "/beta")
        self.assertEqual(row["phases"]["headline"], "beta")

    def test_without_a_launch_folder_the_project_is_the_folder_cd_into_most(self):
        # no cwd was recorded, or it was `/` (an IDE's agent): the folder its commands `cd` into
        # most, and the first of two that tie
        for cwd in (None, "/"):
            for folders, expected in ((["beta", "alpha", "alpha", "beta"], "beta"),
                                      (["beta", "alpha", "alpha", "beta", "alpha"], "alpha")):
                with self.subTest(cwd=cwd, folders=folders):
                    recs = [root8(T1, cwd=cwd)] + [
                        bash8(T1 + 60 * i, 201 + i, "cd /home/alice/%s && ls" % name, cwd=cwd)
                        for i, name in enumerate(folders)]
                    self.assertEqual(row_of(reduce(recs))["phases"]["headline"], expected)

    def test_the_project_is_masked(self):
        secret = "ghp_" + "a" * 36
        cwd = HOME + "/" + secret
        recs = [root8(T1, cwd=cwd)] + [bash8(T1 + 60 * i, 201 + i, cmd, cwd=cwd)
                                       for i, cmd in enumerate(["ls", "ls -la", "ls src"])]
        block = row_of(reduce(recs))["phases"]
        self.assertEqual((block["headline"], labels(block["tasks"])),
                         (phase_names.MASK, ["Read " + phase_names.MASK]))
        self.assertNotIn("ghp_", json.dumps(block))

    # ------------------------------------------------------------------ the row and the inspector
    def test_the_row_and_the_inspector_agree(self):
        # Two stretches of tool-shell calls (schema 7: each its own key) an hour and a half apart,
        # and a tool shell still running whose loop's children exit after the list was folded: the
        # fold cache is stale. The inspector's lookup folds this row's keys alone, and building
        # its row then folds the window again (as in test_traj_phases).
        recs = [rec("residency", T1 - 100, 100, 50, "claude", "claude", agent_pid=100,
                    tree_root=100, is_agent=True)]
        for i in range(10):
            recs += call7(T1 + 45 * i, 200 + 10 * i, "cat a%d.py" % i)
            recs += call7(T1 + 5000 + 45 * i, 400 + 10 * i, "cat b%d.py" % i)
        live = Live().feed(sorted(recs, key=lambda r: r["ts_epoch"]))
        traj = live.traj
        sid = row_of(traj.result())["sid"]
        live.feed([rec("residency", T1 + 5300, 500, 100, "zsh",
                       SNAP + "'until test -f x; do sleep 5; done'", agent_pid=500, tree_root=100,
                       is_agent=True, age_s=5)])
        live.feed([rec("exit", T1 + 5301 + 5 * i, 600 + i, 500, "sleep", "sleep 5", agent_pid=500,
                       tree_root=100, duration_s=5) for i in range(6)])
        hit = proctree.lookup(traj, sid)
        self.assertIn(sid[:sid.rfind(":") + 1] + "500", hit[3])
        # not passing by accident: the stale fold's members would miss the loop. It is first,
        # since a row reads the current fold for the days of its agent, and that renews the fold.
        stale = traj._row(*hit[:3])
        self.assertEqual([t["open"] for t in stale["phases"]["tasks"]], [False, False])
        inspector = traj._row(*hit)
        traj._phase_cache.clear()                   # the list's row is built afresh
        listed = traj.result()["rows"][sid]
        self.assertEqual([t["open"] for t in listed["phases"]["tasks"]], [False, True])
        self.assertEqual(inspector["phases"], listed["phases"])
        self.assertEqual(inspector["n_calls"], listed["n_calls"])


# ------------------------------------------------------------------------- across midnight
def local_midnight(t):
    """The local midnight that starts the day `t` is in."""
    return time.mktime(time.localtime(t)[:3] + (0, 0, 0, 0, 0, -1))


def around_midnight():
    """The latest local midnight `m` with `m + 7200 < time.time() - 60`. A test places its records
    in `[m - 3600, m + 3600]`: on two local days, a row for each, and over before now."""
    limit = time.time() - 60 - 7200
    m = local_midnight(limit)
    return m if m < limit else local_midnight(m - 1)


def reads(first, count, pid, root=100, **kw):
    """`count` reads of a file of its own, one every 100 s from `first`, by agent `root`, with the
    pids `pid` on."""
    return [bash8(first + 100 * i, pid + i, "cat src/f%d.py" % (pid + i), root=root, **kw)
            for i in range(count)]


def day_rows(traj, now, pid=100, host="h1"):
    """The rows of one agent at `now`, one for each local day it worked on, in time order. `now` is
    just past the records, and not the clock's: `m` can be a day and a half back, and a row older
    than the window is not listed."""
    rows = traj.result(now)["rows"].values()
    return sorted((r for r in rows if (r["host"], r["agent_pid"]) == (host, str(pid))),
                  key=lambda r: r["first"])


class AcrossMidnightTest(unittest.TestCase):
    """A row is one local day (a session id carries it), so an agent that works past midnight has
    two. Each of them serves the block of the whole session."""

    def setUp(self):
        self.m = around_midnight()
        self.now = self.m + 3700                    # past every record

    def two_days(self):
        """Agent 100 reads a file every 100 s from m - 2950 to m + 3050: thirty calls on one day,
        and thirty-one on the next."""
        return [root8(self.m - 3500)] + reads(self.m - 2950, 61, 201)

    def rows_of(self, recs, **thresholds):
        traj = fed(recs, configured(**thresholds))
        return traj, day_rows(traj, self.now)

    def test_an_agent_across_midnight_reads_as_one_session(self):
        m = self.m
        _traj, (first, second) = self.rows_of(self.two_days())
        self.assertNotEqual(first["sid"], second["sid"])
        block = first["phases"]
        self.assertEqual(second["phases"], block)
        self.assertEqual(block["n_calls"], 61)
        # one task, which midnight does not cut
        (task,) = block["tasks"]
        self.assertLess(task["t0"], m)
        self.assertGreater(task["t1"], m)
        self.assertEqual([d["sid"] for d in block["days"]], [first["sid"], second["sid"]])
        self.assertEqual([(d["first"], d["last"]) for d in block["days"]],
                         [(first["first"], first["last"]), (second["first"], second["last"])])
        # the one block goes out as JSON, with the ids of its days
        self.assertEqual(json.loads(json.dumps(block, allow_nan=False)), block)

    def test_a_call_whose_exit_lands_on_the_next_day_makes_no_phase_there(self):
        # A run that began at m - 3000 and exits at m + 2000, a record of the next day's row.
        # Twenty reads from m - 2900 to m - 1000 are made while it runs, so the agent has moved
        # past it, and nothing holds the time after the last read: no idle phase, and no phase
        # at all that begins after it. The run is a call of the first day's work, where it began.
        m = self.m
        recs = [root8(m - 3500)] + reads(m - 2900, 20, 201)
        recs.append(bash8(m + 2000, 300, "python long.py", duration_s=5000))
        _traj, (first, second) = self.rows_of(recs)
        block = first["phases"]
        self.assertEqual(second["phases"], block)
        self.assertEqual([r["n_calls"] for r in (first, second)], [20, 1])
        self.assertEqual(block["n_calls"], 21)
        self.assertLessEqual(max(p["t0"] for p in block["list"]), m - 1000)
        self.assertEqual(block["list"][-1]["t1"], round(m + 2000, 3))     # the lane runs as long
        self.assertNotIn("idle", [p["kind"] for p in block["list"]])
        # no phase and no task is the next day's: the run began before that row's first record
        held = {p["sid"] for p in block["list"]} | {t["sid"] for t in block["tasks"]}
        self.assertEqual(held, {first["sid"]})

    def test_each_task_carries_the_day_row_that_holds_it(self):
        # Twelve reads from m - 3500 to m - 2400, and twelve from m + 1300 to m + 2400: over an
        # hour apart, so two tasks, one on each day. The idle gap between them begins at the
        # last read of the first.
        m = self.m
        recs = [root8(m - 3600)] + reads(m - 3500, 12, 201) + reads(m + 1300, 12, 301)
        _traj, (first, second) = self.rows_of(recs)
        block = first["phases"]
        self.assertEqual(second["phases"], block)
        self.assertEqual([t["kind"] for t in block["tasks"]], ["task", "task"])
        self.assertEqual([t["sid"] for t in block["tasks"]], [first["sid"], second["sid"]])
        self.assertEqual([(p["kind"], p["sid"]) for p in block["list"]],
                         [("explore", first["sid"]), ("idle", first["sid"]),
                          ("explore", second["sid"])])

    def test_a_phase_that_begins_in_no_days_span_has_the_nearest_day(self):
        # A day's span is from its first record to its last. A phase begins when its first call
        # does, which is before that call's record, so it is often in no span: the nearest holds
        # it, and the earlier of two the same distance away.
        _traj, (first, _second) = self.rows_of(self.two_days())
        block = first["phases"]
        self.assertLess(block["list"][0]["t0"], first["first"])
        self.assertEqual(block["list"][0]["sid"], first["sid"])
        # two days 2 s apart: a time 1 s from each is the earlier's
        days = [{"sid": "a", "first": 10.0, "last": 20.0},
                {"sid": "b", "first": 22.0, "last": 30.0}]
        self.assertEqual([trajmod.day_of(days, t)
                          for t in (5.0, 10.0, 20.0, 20.5, 21.0, 21.5, 22.0, 35.0)],
                         ["a", "a", "a", "a", "a", "b", "b", "b"])

    def test_only_the_day_rows_present_are_joined(self):
        # Only the later day's records are fed: one row, and its block is its own calls'
        m = self.m
        later = [r for r in self.two_days() if r["ts_epoch"] > m]
        _traj, (row,) = self.rows_of(later)
        block = row["phases"]
        self.assertNotIn("days", block)
        self.assertEqual((block["n_calls"], row["n_calls"]), (31, 31))
        (task,) = block["tasks"]
        self.assertGreater(task["t0"], m)
        self.assertEqual({p["sid"] for p in block["list"]} | {task["sid"]}, {None})

    def test_a_row_the_window_evicted_is_not_in_the_block(self):
        # The window held both rows, and a read a day later finds the first idle past it: that
        # row goes, and the joined block cached while both were there is not served to the one
        # that is left.
        traj = fed(self.two_days(), configured())
        both = day_rows(traj, self.now)
        self.assertEqual(len(both), 2)
        self.assertEqual(len(both[0]["phases"]["days"]), 2)
        (alone,) = day_rows(traj, self.m + 87000)
        self.assertEqual(alone["sid"], both[1]["sid"])
        self.assertNotIn("days", alone["phases"])
        self.assertEqual(alone["phases"]["n_calls"], 31)
        self.assertEqual({p["sid"] for p in alone["phases"]["list"]}, {None})

    def test_a_day_rows_own_n_calls_stays_its_own(self):
        _traj, (first, second) = self.rows_of(self.two_days())
        self.assertEqual((first["n_calls"], second["n_calls"]), (30, 31))
        self.assertEqual(first["phases"]["n_calls"], first["n_calls"] + second["n_calls"])
        # the rest of a row is its day's: its records, and when it began and ended
        self.assertEqual((first["n_events"], second["n_events"]), (30, 31))
        self.assertLess(first["last"], self.m)
        self.assertGreater(second["first"], self.m)

    def test_a_row_of_one_day_has_no_days_and_no_sid(self):
        recs = [root8(T1)] + reads(T1, 12, 201)
        block = row_of(reduce(recs))["phases"]
        self.assertNotIn("days", block)
        self.assertEqual({p["sid"] for p in block["list"]}, {None})
        self.assertEqual([t["sid"] for t in block["tasks"]], [None])
        self.assertEqual(block["n_calls"], 12)

    def test_only_the_same_agent_on_the_same_host_is_joined(self):
        # Agent 300 works on the second day only, and a second host has an agent with the pid
        # 100 on the first
        m = self.m
        recs = self.two_days() + reads(m + 100, 10, 501, root=300)
        recs += [bash8(m - 500, 601, "cat on_h2.py", host="h2")]
        traj = fed(sorted(recs, key=lambda r: r["ts_epoch"]), configured())
        mine = day_rows(traj, self.now)
        self.assertEqual([len(r["phases"]["days"]) for r in mine], [2, 2])
        self.assertEqual(mine[0]["phases"]["n_calls"], 61)
        for pid, host, n in ((300, "h1", 10), (100, "h2", 1)):
            with self.subTest(agent=pid, host=host):
                (row,) = day_rows(traj, self.now, pid, host)
                self.assertNotIn("days", row["phases"])
                self.assertEqual(row["phases"]["n_calls"], n)

    def test_another_users_agent_with_the_same_pid_is_not_joined(self):
        # A pid is reused: on boslogin06 on 2026-10-08 and 09, 8 of the 74 agents with a row on
        # each of the two days had two users, such as one that began the first evening with 8,795
        # records and one the next noon with 2. A process keeps its user, so bob's agent, which
        # has the pid alice's had, is another agent, and no part of her session.
        m = self.m
        recs = [root8(m - 3500)] + reads(m - 2950, 30, 201)
        recs += reads(m + 100, 20, 301, user="bob")
        traj, (hers, his) = self.rows_of(recs)
        self.assertEqual((hers["user"], his["user"]), ("alice", "bob"))
        self.assertEqual((hers["agent_pid"], his["agent_pid"]), ("100", "100"))
        for row, n in ((hers, 30), (his, 20)):
            self.assertNotIn("days", row["phases"])
            self.assertEqual((row["n_calls"], row["phases"]["n_calls"]), (n, n))
            self.assertEqual({p["sid"] for p in row["phases"]["list"]}, {None})

    def test_another_users_agent_with_the_same_pid_is_another_session(self):
        # `agent_sessions` folds an agent's day rows by the same rule as the block above: host,
        # agent pid, and user. Alice's agent on the first day and bob's on the second share a pid
        # on one host, so they stay two sessions, while carol's one agent across midnight is one.
        m = self.m
        recs = [root8(m - 3500)] + reads(m - 2950, 30, 201)
        recs += reads(m + 100, 20, 301, user="bob")
        recs += [root8(m - 3500, root=500, user="carol")]
        recs += reads(m - 2950, 61, 601, root=500, user="carol")
        traj = fed(sorted(recs, key=lambda r: r["ts_epoch"]), configured())
        got = traj.agent_sessions()
        self.assertEqual(sorted(got), [("h1", 100, "alice"), ("h1", 100, "bob"),
                                       ("h1", 500, "carol")])
        self.assertEqual({k[2]: (s.user, s.n) for k, (s, _via, _keys) in got.items()},
                         {"alice": ("alice", 30), "bob": ("bob", 20), "carol": ("carol", 61)})

    def test_a_row_with_no_time_is_no_day_of_the_session(self):
        # A record with no time is a row of its own, whose id has no day. It has no span to hold
        # a phase, so it is no part of the session, and the session is no part of it.
        recs = self.two_days() + [bash8(self.m, 900, "cat src/x.py", ts=None, ts_epoch=None)]
        traj = fed(recs, configured())
        (untimed,) = [s for s in traj.sessions.values() if s.first is None]
        row = traj._row(untimed)                    # a read of the window would evict it
        self.assertEqual((row["n_calls"], row["phases"]["n_calls"]), (0, 0))
        self.assertNotIn("days", row["phases"])
        both = day_rows(traj, self.now)
        self.assertEqual([len(r["phases"]["days"]) for r in both], [2, 2])
        self.assertEqual(both[0]["phases"]["n_calls"], 61)

    def test_a_row_that_gains_a_call_is_built_again(self):
        # The cache is keyed on every day row the block holds: a call on either day, a read
        # later, makes the block again, for both rows. With nothing new the block is the one held.
        m = self.m
        recs = self.two_days()
        live = Live().feed(recs[:-1])
        first, second = day_rows(live.traj, self.now)
        self.assertEqual(first["phases"]["n_calls"], 60)
        again = day_rows(live.traj, self.now)
        self.assertIs(again[0]["phases"], first["phases"])
        self.assertIs(again[1]["phases"], first["phases"])
        live.feed(recs[-1:])                                # the last call, on the second day
        first, second = day_rows(live.traj, self.now)
        self.assertEqual((first["phases"]["n_calls"], second["phases"]["n_calls"]), (61, 61))
        self.assertEqual((first["n_calls"], second["n_calls"]), (30, 31))
        live.feed([bash8(m - 20, 700, "cat src/late.py")])  # and a late one, on the first
        first, second = day_rows(live.traj, self.now)
        self.assertEqual((first["phases"]["n_calls"], second["phases"]["n_calls"]), (62, 62))
        self.assertEqual((first["n_calls"], second["n_calls"]), (31, 31))
        self.assertEqual(first["phases"], second["phases"])

    def test_every_row_of_a_session_is_named_for_one_project(self):
        # Launched in the user's own folder, so the project is the folder the commands change
        # into most, over the session: `beta` three times, `alpha` once. The first day alone
        # would say `alpha`, and the second `beta`.
        m = self.m
        recs = [root8(m - 3500, cwd=HOME), bash8(m - 2000, 201, "cd /home/alice/alpha && ls",
                                                 cwd=HOME)]
        recs += [bash8(m + 100 * (i + 1), 210 + i, "cd /home/alice/beta && ls", cwd=HOME)
                 for i in range(3)]
        _traj, (first, second) = self.rows_of(recs)
        self.assertEqual([r["phases"]["headline"] for r in (first, second)], ["beta", "beta"])
        # not by accident: the first day's row alone is named for what it holds
        (alone,) = self.rows_of(recs[:2])[1]
        self.assertEqual(alone["phases"]["headline"], "alpha")

    def test_a_call_is_open_only_if_it_was_busy_at_the_sessions_last_record(self):
        # A tool shell (777) that never exits ran `ls`, `cat`, and `ls` just before midnight: busy
        # when its day's records end, and dense, but no loop. On its own it is a call still
        # running. Beside the next day's work the session's last record is hours later, and
        # nothing has run under it since: it is no call, and its commands are counted as
        # housekeeping under a process that never exited.
        m = self.m
        day = [root8(m - 3500), bash8(m - 1500, 201, "cat src/a.py"),
               s8(m - 120, 700, 777, "ls", "ls", duration_s=0.05, depth=2),
               s8(m - 100, 701, 777, "cat", "cat x.txt", duration_s=0.05, depth=2),
               s8(m - 80, 702, 777, "ls", "ls", duration_s=0.05, depth=2)]
        (alone,) = self.rows_of(day)[1]
        self.assertEqual([p["open"] for p in alone["phases"]["list"]], [False, False, True])
        self.assertEqual(alone["phases"]["housekeeping"]["never_exited"], 0)
        first, second = self.rows_of(day + reads(m + 1000, 5, 301))[1]
        self.assertFalse(any(p["open"] for p in first["phases"]["list"]))
        self.assertFalse(any(t["open"] for t in first["phases"]["tasks"]))
        self.assertEqual(first["phases"]["housekeeping"]["never_exited"], 3)
        self.assertEqual((first["n_calls"], second["n_calls"]), (1, 5))

    def test_the_housekeeping_of_both_days_is_the_blocks(self):
        # Two chores of the agent's own on the first day and three on the second, each in a
        # minute of its own, with room for three minutes: the oldest three are placed, and the
        # other two are counted
        m, chore = self.m, "git -c core.fsmonitor= status --porcelain"
        recs = self.two_days()
        recs += [s8(m - 600 + 60 * i, 800 + i, 100, "git", chore) for i in range(2)]
        recs += [s8(m + 600 + 60 * i, 900 + i, 100, "git", chore) for i in range(3)]
        _traj, (first, second) = self.rows_of(recs, phases={"hk_bin_cap": 3})
        hk = first["phases"]["housekeeping"]
        self.assertEqual(second["phases"]["housekeeping"], hk)
        self.assertEqual((hk["n"], hk["cut"]), (5, 2))
        # a chore began half a second before its record's time, which is the minute it is placed in
        self.assertEqual(hk["bins"], [[int((t - 0.5) // 60) * 60, 1]
                                      for t in (m - 600, m - 540, m + 600)])
        self.assertEqual(first["phases"]["n_calls"], 61)

    def test_a_row_asked_for_before_any_fold_finds_its_other_days(self):
        # `_row` on a session, as a caller with no fold yet has it: the fold it reads the days of
        # the agent from is made then, and the other row's keys are that fold's
        traj = fed(self.two_days(), configured())
        self.assertIsNone(traj._fold_cache[2])
        row = traj._row(min(traj.sessions.values(), key=lambda s: s.first))
        self.assertEqual(len(row["phases"]["days"]), 2)
        self.assertEqual((row["n_calls"], row["phases"]["n_calls"]), (30, 61))

    def test_the_cut_of_the_log_is_the_joined_logs(self):
        # Neither day's log is over the cap of 40 calls, but the two together are: the block says
        # twenty-one calls were dropped, from where the forty-first began, and its last task is
        # incomplete. Each row still counts its own calls, which add up to the session's.
        _traj, (first, second) = self.rows_of(self.two_days(), phases={"max_calls": 40})
        block = first["phases"]
        self.assertEqual((block["n_calls"], block["cut"]), (61, 21))
        self.assertAlmostEqual(block["cut_at"], self.m - 2950.5 + 4000, places=2)
        self.assertEqual([t["incomplete"] for t in block["tasks"]], [True])
        self.assertEqual((first["n_calls"], second["n_calls"]), (30, 31))
        # not by accident: with room for them all nothing is dropped
        whole = self.rows_of(self.two_days())[1][0]["phases"]
        self.assertEqual((whole["cut"], whole["cut_at"]), (0, None))
        self.assertEqual([t["incomplete"] for t in whole["tasks"]], [False])

    def test_the_hours_merge_to_the_block_one_pass_builds(self):
        raw = sorted(self.two_days(), key=lambda r: r["ts_epoch"])
        normal = TM.normalized(raw)
        one = TM.materialize(TM.one_pass(normal, 4320))
        saved, carry, fresh = TM.hour_states(normal)
        self.assertGreaterEqual(len(saved), 2)
        got = TM.materialize(TM.merged(saved, fresh, carry, 4320))
        self.assertEqual(TM.diff(one["trajectories"], got["trajectories"]), [])
        for payload in (one, got):
            first, second = sorted((r for r in ranked(payload) if r["agent_pid"] == "100"),
                                   key=lambda r: r["first"])
            self.assertEqual(first["phases"], second["phases"])
            self.assertEqual(len(first["phases"]["days"]), 2)
            self.assertEqual(first["phases"]["n_calls"], 61)


if __name__ == "__main__":
    unittest.main()
