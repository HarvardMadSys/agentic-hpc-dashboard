"""From tool calls to phases (reducers/phase_segment.py).

`segment` is a pure function: one session's calls in, the phase block out. These tests pin the
rules the module states: one kind in a row is one phase and a stray call is absorbed into it, idle
ends a phase and only a call the agent is still waiting on holds a gap, a command on a timer is
waiting (a test or a run only on the strict bar), irregular re-runs of one test are an edit-test
loop, a pause before a test or a run is a guess that is always named one and never merges with a
call, time goes to what the agent started last, and the block reports what it was given, what it
cut, and the housekeeping beside it, changing nothing it was handed.

    cd dashboard && uv run python -m unittest discover -s tests -p 'test_phase_segment.py' -v
"""
import random
import sys
import unittest
from bisect import bisect_right
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rc_dashboard.reducers.phase_log import OPEN, PhaseLog  # noqa: E402
from rc_dashboard.reducers.phase_rules import norm_hash  # noqa: E402
from rc_dashboard.reducers.phase_segment import (DEFAULTS, NEVER_EXITED, _polling,  # noqa: E402
                                                 prepared, segment)

PREVIEWS = [None]           # masked previews by id, as the reducer interns them: 0 is none
_IDS = {}


def idx(text):
    """The id of `text` in PREVIEWS, interned; 0 for no text."""
    if not text:
        return 0
    i = _IDS.get(text)
    if i is None:
        i = _IDS[text] = len(PREVIEWS)
        PREVIEWS.append(text)
    return i


def K(s, d, cat, text="x", flags=0, pid=None, past=None):
    """A call that starts at `s` and runs `d` seconds, with the empty tag set. An open call adds
    the programs seen under it after this (`K(...) + (("sleep",),)`)."""
    return (s, s + d, cat, norm_hash(text), idx(text), flags, pid, past, 0)


# The absorb pair the mechanism tests here were written at, small enough to keep the few runs each
# builds apart; a test that is about absorbing passes its own. The tuned defaults absorb far more,
# and `TunedTest` reads them as they are.
WRITTEN_AT = {"absorb_calls": 2, "absorb_s": 60}


def seg(calls, guess=True, cuts=(), **params):
    return segment(calls, PhaseLog(), PREVIEWS, dict(DEFAULTS, **dict(WRITTEN_AT, **params)), guess,
                   cuts=cuts)


def kinds(b):
    return [p["kind"] for p in b["list"]]


def state(lg):
    """Everything a PhaseLog holds, copied."""
    return (list(lg.calls), dict(lg.by_pid), dict(lg.hk), dict(lg.hk_bins), lg.hk_cut, lg.cut,
            lg.cut_at)


class SegmentTest(unittest.TestCase):
    def test_one_kind_in_a_row_is_one_phase(self):
        b = seg([K(i * 10, 2, "explore", "ls %d" % i) for i in range(5)])
        self.assertEqual((kinds(b), b["list"][0]["n"]), (["explore"], 5))

    def test_a_stray_call_is_absorbed(self):
        calls = [K(i * 10, 2, "explore", "ls %d" % i) for i in range(4)] \
            + [K(41, 3, "run", "python a.py")] \
            + [K(50 + i * 10, 2, "explore", "cat %d" % i) for i in range(4)]
        b = seg(calls)
        self.assertEqual(kinds(b), ["explore"])
        self.assertEqual(b["list"][0]["n"], 9)

    def test_an_idle_gap_ends_a_phase(self):
        calls = [K(i * 10, 2, "explore", "ls") for i in range(3)] \
            + [K(700 + i * 10, 2, "explore", "ls") for i in range(3)]
        b = seg(calls)
        self.assertEqual(kinds(b), ["explore", "idle", "explore"])
        self.assertEqual((b["list"][1]["t0"], b["list"][1]["t1"]), (22, 700))

    def test_regular_squeue_is_waiting_on_slurm(self):
        b = seg([K(i * 120.0, 0.3, "slurm", "squeue -h -u $USER -o %j") for i in range(33)])
        p = b["list"][0]
        self.assertEqual((p["kind"], p["waits_on"], p["times"]), ("wait", "slurm", 33))
        self.assertAlmostEqual(p["every_s"], 120.0, places=0)

    def test_irregular_reruns_of_one_test_are_an_edit_test_loop(self):
        calls = [K(t, 5, "test", "pytest tests/test_io.py") for t in (0, 95, 230, 300)]
        p = seg(calls)["list"]
        self.assertEqual([x["kind"] for x in p], ["loop"])
        self.assertEqual((p[0]["rounds"], len(p[0]["slices"])), (4, 3))

    def test_perfectly_regular_reruns_are_polling(self):
        b = seg([K(i * 60.0, 5, "test", "pytest -q") for i in range(6)])
        self.assertEqual(kinds(b), ["wait"])

    def test_a_pause_after_reading_before_a_run_is_a_guess(self):
        calls = [K(i * 10, 2, "explore", "cat f%d" % i) for i in range(10)] \
            + [K(200, 90, "run", "python run.py")]
        b = seg(calls)
        self.assertEqual(kinds(b), ["explore", "edit_guess", "run"])
        g = b["list"][1]
        self.assertEqual((g["t0"], g["t1"]), (92, 200))
        self.assertIn("after reading", g["guess"])

    def test_a_short_guess_becomes_a_slice_not_a_phase(self):
        calls = [K(i * 10, 2, "explore", "cat f%d" % i) for i in range(10)] \
            + [K(122, 90, "run", "python run.py")]
        b = seg(calls)
        self.assertEqual(kinds(b), ["explore", "run"])
        self.assertEqual(b["list"][1]["slices"], [[92, 122]])

    def test_a_lone_short_call_never_merges_into_a_guess_or_idle(self):
        calls = [K(i * 10, 2, "explore", "cat f%d" % i) for i in range(10)] \
            + [K(200, 5, "run", "python run.py")]
        self.assertEqual(kinds(seg(calls)), ["explore", "edit_guess", "run"])

    def test_a_seen_edit_leaves_nothing_to_guess(self):
        calls = [K(0, 2, "explore", "cat a"), K(10, 1, "edit", "sed -i s/a/b/ a"),
                 K(200, 5, "run", "python a")]
        self.assertNotIn("edit_guess", kinds(seg(calls, absorb_calls=1)))

    def test_no_guess_under_guess_min_or_across_idle(self):
        self.assertNotIn("edit_guess",
                         kinds(seg([K(0, 2, "explore", "cat"), K(12, 5, "run", "py")])))
        self.assertEqual(kinds(seg([K(0, 2, "explore", "cat"), K(1300, 5, "run", "py")],
                                   absorb_calls=1)),
                         ["explore", "idle", "run"])

    def test_guesses_are_off_when_asked(self):
        calls = [K(i * 10, 2, "explore", "cat f%d" % i) for i in range(10)] \
            + [K(200, 30, "run", "python run.py")]
        self.assertNotIn("edit_guess", kinds(seg(calls, guess=False)))

    def test_time_goes_to_what_was_started_last(self):
        calls = [K(0, 10, "explore", "ls"), K(1, 1, "explore", "ls"),
                 K(60, 3540, "run", "python train.py"),
                 K(3700, 5, "explore", "tail log"), K(3710, 5, "explore", "tail log2")]
        b = seg(calls, guess=False)
        self.assertEqual([(p["kind"], p["t0"], p["t1"]) for p in b["list"]],
                         [("explore", 0, 60), ("run", 60, 3700), ("explore", 3700, 3715)])

    def test_an_open_call_marks_its_phase_open(self):
        b = seg([K(0, 2, "explore", "ls"), K(10, 600, "wait", "", flags=OPEN)], absorb_calls=1)
        self.assertTrue(b["list"][-1]["open"])

    def test_a_call_that_ran_past_is_reported(self):
        b = seg([K(0, 5, "run", "nohup python train.py &", pid=201, past=3600)])
        self.assertEqual(b["list"][0]["ran_past"], [["nohup python train.py &", 3595]])

    def test_a_run_taken_in_takes_the_kind_of_the_one_that_took_it(self):
        # Four quick reads, then two loops polling for an hour (boslogin06 apid 114602): by calls
        # the reads would name the hour, and it was a wait. Its mix leads with its own kind.
        loop = "until grep -q done out; do sleep 20; done"
        calls = [K(2 * i, 0.2, "explore", "rg x%d" % i) for i in range(4)] \
            + [K(10, 3600, "wait", loop, flags=OPEN), K(12, 3600, "wait", loop + " ", flags=OPEN)]
        p = seg(calls, absorb_calls=6, absorb_s=300)["list"]
        self.assertEqual([(q["kind"], q["n"], q["open"]) for q in p], [("wait", 6, True)])
        self.assertEqual(p[0]["mix"], [["wait", 2], ["explore", 4]])
        # absorbed into the longer neighbour, it takes that one's kind too
        calls = [K(0, 900, "run", "python train.py")] + \
            [K(950 + 2 * i, 0.5, "explore", "cat log%d" % i) for i in range(3)] + \
            [K(960, 60, "test", "pytest -q")]
        self.assertEqual(kinds(seg(calls, absorb_calls=6, absorb_s=300)), ["run"])

    def test_a_mixed_phase_says_its_mix(self):
        names = {"E": "explore", "R": "run", "G": "git"}
        calls = [K(i * 5, 1, names[c], "%s %d" % (c, i)) for i, c in enumerate("EEREREGRG")]
        p = seg(calls, guess=False)["list"]
        self.assertEqual(len(p), 1)
        self.assertEqual((p[0]["kind"], p[0]["mix"]),
                         ("explore", [["explore", 4], ["run", 3], ["git", 2]]))

    def test_a_busy_hour_reads_as_a_handful(self):
        calls = []
        for m in range(60):         # one explore burst a minute, an isolated run every 6 minutes
            calls += [K(m * 60 + i * 3, 1, "explore", "cat f%d" % i) for i in range(4)]
            if m % 6 == 0:
                calls.append(K(m * 60 + 30, 2, "run", "python check.py"))
        self.assertLessEqual(len(seg(calls)["list"]), 6)

    def test_the_block_shape(self):
        b = seg([K(0, 1, "explore", "ls")])
        self.assertEqual(set(b), {"v", "basis", "n_calls", "cut", "cut_at", "housekeeping", "list"})
        self.assertEqual((b["v"], b["basis"]),
                         (2, "read from the commands the agent ran and the words it wrote in them"))

    def test_extra_housekeeping_counts_in_the_block_and_nowhere_else(self):
        lg = PhaseLog()
        lg.add_housekeeping("git", 60, bin_cap=10)
        kept = state(lg)
        b = segment([K(0, 1, "explore", "ls")], lg, PREVIEWS, dict(DEFAULTS),
                    extra_hk={"under a process that never exited": 2})
        self.assertEqual(b["housekeeping"]["n"], 1 + 2)
        self.assertEqual(state(lg), kept)


class BeyondTheBriefTest(unittest.TestCase):
    """What the module states and the brief's tests leave open."""

    def test_segment_changes_nothing_it_is_given(self):
        calls = [K(200, 90, "run", "python run.py")] \
            + [K(i * 10, 2, "explore", "cat f%d" % i) for i in range(10)]
        lg = PhaseLog()
        for c in calls:
            lg.add(c, cap=5)
        lg.add_housekeeping("git", 60, bin_cap=10)
        given, previews, kept = list(calls), list(PREVIEWS), state(lg)
        first = segment(calls, lg, PREVIEWS, dict(DEFAULTS), extra_hk={"x": 1})
        self.assertEqual((calls, PREVIEWS, state(lg)), (given, previews, kept))
        self.assertEqual(segment(calls, lg, PREVIEWS, dict(DEFAULTS), extra_hk={"x": 1}), first)

    def test_the_housekeeping_block_places_or_counts_every_command(self):
        lg = PhaseLog()
        for i, shape in enumerate("abcdef"):            # shape f six times, ..., shape a once
            for _ in range(i + 1):
                lg.add_housekeeping(shape, 60.0 * (i + 1), bin_cap=3)
        lg.add_housekeeping("a", None, bin_cap=3)       # no time: counted, never binned
        hk = segment([], lg, PREVIEWS, dict(DEFAULTS),
                     extra_hk={"under a process that never exited": 2})["housekeeping"]
        self.assertEqual(hk["n"], 21 + 1 + 2)
        self.assertEqual(hk["shapes"], [["f", 6], ["e", 5], ["d", 4], ["c", 3], ["a", 2]])
        self.assertEqual(hk["bins"], [[60, 1], [120, 2], [180, 3]])
        self.assertEqual(hk["n"], sum(n for _, n in hk["bins"]) + hk["cut"])

    def test_never_exited_is_an_int_that_is_always_there_and_0_when_none(self):     # R43
        hk = seg([K(0, 1, "explore", "ls")])["housekeeping"]
        self.assertEqual(set(hk), {"n", "shapes", "bins", "cut", "never_exited"})
        self.assertEqual(hk["never_exited"], 0)
        lg = PhaseLog()
        lg.add_housekeeping("git", 60, bin_cap=10, n=4)
        hk = segment([], lg, PREVIEWS, dict(DEFAULTS))["housekeeping"]
        self.assertEqual((hk["n"], hk["never_exited"]), (4, 0))
        self.assertIsInstance(hk["never_exited"], int)

    def test_never_exited_counts_that_shape_of_extra_housekeeping_and_no_other(self):   # R43
        lg = PhaseLog()
        lg.add_housekeeping("git", 60, bin_cap=10, n=5)
        hk = segment([], lg, PREVIEWS, dict(DEFAULTS),
                     extra_hk={NEVER_EXITED: 12, "x": 3})["housekeeping"]
        self.assertEqual(hk["never_exited"], 12)
        self.assertEqual((hk["n"], hk["cut"]), (5 + 12 + 3, 12 + 3))   # both are in the total and unplaced
        for none in ({"x": 3}, {NEVER_EXITED: 0}, {NEVER_EXITED: -2}, {}, None):
            with self.subTest(extra=none):
                self.assertEqual(
                    segment([], PhaseLog(), PREVIEWS, dict(DEFAULTS), extra_hk=none)["housekeeping"]["never_exited"], 0)

    def test_never_exited_is_counted_when_its_shape_is_not_in_the_top_five(self):       # R43
        # The shapes list holds five, so a reader who looked for this shape there would miss it.
        lg = PhaseLog()
        for i, shape in enumerate("abcdef"):
            lg.add_housekeeping(shape, 60, bin_cap=10, n=20 + i)
        hk = segment([], lg, PREVIEWS, dict(DEFAULTS), extra_hk={NEVER_EXITED: 12})["housekeeping"]
        self.assertNotIn(NEVER_EXITED, [s for s, _ in hk["shapes"]])
        self.assertEqual(len(hk["shapes"]), 5)
        self.assertEqual((hk["never_exited"], hk["n"]), (12, sum(range(20, 26)) + 12))

    def test_the_block_counts_the_calls_the_log_cut(self):
        lg = PhaseLog()
        for i in range(3):
            lg.add(K(i * 10, 1, "explore", "ls %d" % i), cap=2)
        b = segment(lg.calls, lg, PREVIEWS, dict(DEFAULTS))
        self.assertEqual((b["n_calls"], b["cut"], b["cut_at"]), (3, 1, 20))
        self.assertEqual(sum(p["n"] for p in b["list"]), 2)

    def test_no_calls_no_phases(self):
        b = seg([])
        self.assertEqual((b["n_calls"], b["list"], b["housekeeping"]["n"]), (0, [], 0))

    def test_a_slice_is_its_phases_and_says_why(self):
        calls = [K(i * 10, 2, "explore", "cat f%d" % i) for i in range(10)] \
            + [K(122, 90, "run", "python run.py")]
        e, r = seg(calls)["list"]
        self.assertEqual((r["kind"], r["guess"]), ("run", "a pause after reading, before the run"))
        self.assertEqual((e["t1"], r["t0"], r["t1"]), (92, 92, 212))
        self.assertIsNone(e["guess"])

    def test_a_guess_between_two_runs_names_the_command(self):
        calls = [K(0, 5, "test", "pytest -q"), K(10, 1, "explore", "cat a"),
                 K(100, 5, "test", "pytest -q")]
        b = seg(calls, absorb_calls=1)
        self.assertEqual(kinds(b), ["test", "explore", "edit_guess", "test"])
        self.assertEqual(b["list"][2]["guess"], "a pause between two runs of pytest -q")

    def test_seen_edits_between_runs_make_a_loop_too(self):
        calls = [K(0, 5, "test", "pytest -x"), K(30, 1, "edit", "sed -i s/a/b/ a.py"),
                 K(40, 5, "test", "pytest -x"), K(70, 1, "edit", "sed -i s/b/c/ a.py"),
                 K(80, 1, "explore", "cat a.py"), K(90, 1, "edit", "sed -i s/c/d/ b.py"),
                 K(100, 5, "test", "pytest -x")]
        p = seg(calls)["list"]
        self.assertEqual(kinds({"list": p}), ["loop"])
        self.assertEqual((p[0]["rounds"], p[0]["slices"], p[0]["n"]), (3, None, 7))

    def test_loops_of_two_commands_stay_two_loops(self):
        calls = [K(t, 5, "test", "pytest a") for t in (0, 60, 120)] \
            + [K(t, 5, "test", "pytest b") for t in (180, 240, 300)]
        b = seg(calls)
        self.assertEqual([(p["kind"], p["rounds"], p["top"]) for p in b["list"]],
                         [("loop", 3, [["pytest a", 3]]), ("loop", 3, [["pytest b", 3]])])

    def test_a_call_between_loops_of_two_commands_joins_one_of_them(self):
        # The loops are the same length, so the read goes into the first. It never makes them one.
        calls = [K(t, 5, "test", "pytest a") for t in (0, 60, 120)] \
            + [K(130, 2, "explore", "cat x")] \
            + [K(t, 5, "test", "pytest b") for t in (140, 200, 260)]
        b = seg(calls)
        self.assertEqual([(p["kind"], p["rounds"], p["n"]) for p in b["list"]],
                         [("loop", 3, 4), ("loop", 3, 3)])

    def test_rounds_count_every_run_of_the_command_absorbed_ones_too(self):
        # the fourth run follows the third with no pause to edit in, so the loop ends at the
        # third; the fourth, a stray call, is absorbed into it, and it is a run all the same
        b = seg([K(t, 5, "test", "pytest -q") for t in (0, 60, 120, 130)])
        self.assertEqual([(p["kind"], p["rounds"], p["n"]) for p in b["list"]],
                         [("loop", 4, 4)])

    def test_a_call_absorbed_into_a_loop_from_before_keeps_the_loops_command(self):
        calls = [K(0, 2, "explore", "cat a")] \
            + [K(t, 5, "test", "pytest -q") for t in (10, 70, 130)]
        b = seg(calls)
        self.assertEqual([(p["kind"], p["rounds"], p["n"]) for p in b["list"]],
                         [("loop", 3, 4)])

    def test_an_idle_gap_breaks_a_loop(self):
        def calls(after):                   # two runs, a read `after` s later, a third run
            return [K(0, 5, "test", "pytest -q"), K(60, 5, "test", "pytest -q"),
                    K(65 + after, 2, "explore", "cat a.py"),
                    K(165 + after, 5, "test", "pytest -q")]
        self.assertEqual(kinds(seg(calls(35))), ["loop"])
        self.assertEqual(kinds(seg(calls(635))), ["test", "idle", "explore", "edit_guess", "test"])

    def test_an_idle_gap_does_not_block_a_between_runs_guess(self):
        # Nothing but reading lies between the two runs; the idle gap is no step. So the pause
        # before the third run is still a pause between two runs of it.
        b = seg([K(0, 5, "test", "pytest -q"), K(60, 5, "test", "pytest -q"),
                 K(700, 2, "explore", "cat a.py"), K(800, 5, "test", "pytest -q")])
        g = b["list"][3]
        self.assertEqual((g["kind"], g["t0"], g["t1"], g["guess"]),
                         ("edit_guess", 702, 800, "a pause between two runs of pytest -q"))

    def test_one_round_is_not_a_loop(self):
        calls = [K(t, 5, "test", "pytest tests/test_io.py") for t in (0, 95)]
        self.assertNotIn("loop", kinds(seg(calls, absorb_calls=1)))

    def test_the_top_commands_are_counted_and_cut(self):
        texts = ["ls", "cat a", "ls", "cat b", "ls", "cat a", "rg x"]
        p = seg([K(i * 10, 2, "explore", t) for i, t in enumerate(texts)])["list"][0]
        self.assertEqual(p["top"], [["ls", 3], ["cat a", 2], ["cat b", 1]])

    def test_a_test_on_a_loose_timer_is_a_loop_and_squeue_on_it_is_polling(self):
        starts = (0, 60, 135, 195, 270, 330)             # gaps' stdev is 0.11 of their mean
        p = seg([K(t, 5, "test", "pytest -k io") for t in starts])["list"]
        self.assertEqual([(x["kind"], x["rounds"], len(x["slices"])) for x in p], [("loop", 6, 5)])
        p = seg([K(t, 0.3, "slurm", "squeue --me") for t in starts])["list"]
        self.assertEqual([(x["kind"], x["every_s"], x["times"]) for x in p], [("wait", 66.0, 6)])

    def test_a_process_left_running_extends_only_the_last_phase(self):
        calls = [K(0, 5, "run", "nohup python a.py &", pid=1, past=500),
                 K(100, 2, "explore", "ls"), K(110, 2, "explore", "cat x"),
                 K(120, 5, "run", "nohup python b.py &", pid=2, past=900)]
        b = seg(calls, guess=False, absorb_calls=1)
        self.assertEqual([(p["kind"], p["t0"], p["t1"]) for p in b["list"]],
                         [("run", 0, 100), ("explore", 100, 120), ("run", 120, 900)])
        self.assertEqual(b["list"][0]["ran_past"], [["nohup python a.py &", 495]])

    def test_a_process_left_running_does_not_hide_idle(self):
        # A job left running in the background is its call's, as `past`, but it is not a call:
        # the hour the agent made none is idle. It does keep the lane open at the end.
        b = seg([K(0, 5, "run", "nohup python train.py &", pid=1, past=7200),
                 K(3600, 2, "explore", "tail train.log")])
        self.assertEqual([(p["kind"], p["t0"], p["t1"]) for p in b["list"]],
                         [("run", 0, 5), ("idle", 5, 3600), ("explore", 3600, 7200)])
        self.assertEqual(b["list"][0]["ran_past"], [["nohup python train.py &", 7195]])

    def test_the_lane_does_not_end_while_a_call_runs(self):
        # train.py runs from 60 to 3600; the agent reads its log at 100. Time goes to what it
        # started last, so train.py's phase ends at 100, says it kept running, and the lane does
        # not end until it does.
        calls = [K(0, 10, "explore", "ls"), K(1, 1, "explore", "ls -la"),
                 K(60, 3540, "run", "python train.py"),
                 K(100, 2, "explore", "tail log"), K(105, 2, "explore", "tail log2")]
        b = seg(calls, guess=False)
        self.assertEqual([(p["kind"], p["t0"], p["t1"]) for p in b["list"]],
                         [("explore", 0, 60), ("run", 60, 100), ("explore", 100, 3600)])
        self.assertEqual(b["list"][1]["ran_past"], [["python train.py", 3500]])
        self.assertIsNone(b["list"][2]["ran_past"])

    def test_a_long_call_mid_session_is_listed_in_its_own_phase(self):
        calls = [K(0, 2, "explore", "cat a"), K(5, 2, "explore", "cat b"),
                 K(60, 1000, "run", "python train.py"),
                 K(100, 2, "explore", "tail log"), K(110, 2, "explore", "tail log2"),
                 K(1200, 5, "git", "git add ."), K(1210, 5, "git", "git commit -m x")]
        b = seg(calls, guess=False)
        # moved past (2026-11-02 ruling): train.py ran to 1060, but the agent began other calls at
        # 100 and made none from 112 to 1200, so that stretch is idle, and the gap names train.py
        self.assertEqual([(p["kind"], p["t0"], p["t1"], p["ran_past"]) for p in b["list"]],
                         [("explore", 0, 60, None),
                          ("run", 60, 100, [["python train.py", 960]]),
                          ("explore", 100, 112, None),
                          ("idle", 112, 1200, None),
                          ("git", 1200, 1215, None)])
        self.assertEqual(b["list"][3]["alive"], [["python train.py", 60]])

    def test_a_call_is_listed_once_with_the_larger_figure(self):
        for past, said in ((1500, 960), (3000, 1940)):     # 960 s past its phase; past its end
            calls = [K(0, 2, "explore", "cat a"), K(5, 2, "explore", "cat b"),
                     K(60, 1000, "run", "nohup python train.py", pid=7, past=past),
                     K(100, 2, "explore", "tail log"), K(110, 2, "explore", "tail log2")]
            b = seg(calls, guess=False)
            self.assertEqual(b["list"][1]["ran_past"], [["nohup python train.py", said]])
            self.assertEqual(b["list"][-1]["t1"], past)

    def test_a_call_ending_within_a_second_of_its_phase_did_not_run_past_it(self):
        # the stamps' noise, as for `past`: an exit is stamped when the collector handled it
        for d, said in ((10.5, None), (11.5, [["python a.py", 1.5]])):
            calls = [K(0, d, "run", "python a.py"), K(10, 2, "explore", "ls"),
                     K(20, 2, "explore", "cat")]
            b = seg(calls, guess=False, absorb_calls=1)
            self.assertEqual((kinds(b), b["list"][0]["ran_past"]), (["run", "explore"], said))

    def test_idle_begins_at_exactly_idle_s(self):
        self.assertEqual(kinds(seg([K(0, 10, "explore", "ls"), K(310, 1, "explore", "ls")])),
                         ["explore", "idle", "explore"])
        self.assertEqual(kinds(seg([K(0, 10, "explore", "ls"), K(309, 1, "explore", "ls")])),
                         ["explore"])

    def test_a_tie_in_calls_goes_to_time_before_rule_order(self):
        b = seg([K(0, 30, "explore", "cat a"), K(35, 2, "run", "python a.py")])
        self.assertEqual((kinds(b), b["list"][0]["n"], b["list"][0]["mix"]), (["explore"], 2, []))

    def test_a_step_is_named_for_what_dominates_it(self):
        # `ruff` makes its step a test step, but the phase is named for its calls: one each, and
        # the read took the time. Named so, it is one with the reading before it.
        calls = [K(0, 2, "explore", "cat a"), K(5, 2, "explore", "cat b"),
                 K(10, 0.1, "test", "ruff check ."), K(10.5, 5, "explore", "find . -name x")]
        b = seg(calls)
        self.assertEqual((kinds(b), b["list"][0]["n"]), (["explore"], 4))

    def test_the_smallest_run_goes_first_by_duration(self):
        calls = [K(t, 2, "explore", "cat %s" % c) for t, c in ((0, "a"), (9, "b"), (18, "c"))] \
            + [K(25, 1, "run", "python a.py"), K(30, 30, "test", "pytest -q")] \
            + [K(t, 5, "git", g) for t, g in ((65, "git add a"), (115, "git commit -m a"),
                                               (160, "git push"))]
        # the 1 s run goes first, into the longer of its neighbours: the 30 s test, not the reading
        self.assertEqual(kinds(seg(calls, guess=False)), ["explore", "test", "git"])

    def test_the_smallest_run_goes_first_by_calls(self):
        calls = [K(t, 1, "explore", "cat %s" % c) for t, c in ((0, "a"), (2, "b"), (4, "c"))] \
            + [K(8, 1, "git", "git add a"), K(9, 1, "git", "git add b")] \
            + [K(12, 10, "run", "python a.py")] \
            + [K(t, 5, "build", "make %s" % c) for t, c in ((25, "a"), (60, "b"), (120, "c"))]
        # the one-call run goes before the shorter two-call one, into the builds after it, and the
        # two git calls then follow it there
        self.assertEqual(kinds(seg(calls, guess=False, absorb_calls=3)), ["explore", "build"])

    def test_a_run_between_two_of_one_kind_joins_both(self):
        calls = [K(t, 1, "explore", "cat %s" % c) for t, c in ((0, "a"), (4, "b"), (9, "c"))] \
            + [K(15, 4, "run", "python a.py"), K(15.5, 4, "run", "python b.py")] \
            + [K(25, 1, "explore", "cat d"), K(44, 1, "explore", "cat e")]
        # into the longer neighbour alone, the two runs' 8 s would outweigh its two reads' 2 s
        b = seg(calls, guess=False, absorb_calls=3)
        self.assertEqual((kinds(b), b["list"][0]["n"]), (["explore"], 7))

    def test_a_seen_edit_before_reading_leaves_nothing_to_guess(self):
        calls = [K(0, 2, "explore", "cat a"), K(10, 1, "edit", "sed -i s/a/b/ a"),
                 K(20, 2, "explore", "cat a"), K(200, 5, "run", "python a")]
        self.assertNotIn("edit_guess", kinds(seg(calls, absorb_calls=1)))

    def test_an_edit_before_the_last_run_does_not_block_the_next_guess(self):
        calls = [K(0, 1, "edit", "sed -i s/a/b/ a.py"), K(10, 5, "test", "pytest -q"),
                 K(20, 2, "explore", "cat a.py"), K(100, 5, "test", "pytest -q")]
        self.assertEqual(kinds(seg(calls, absorb_calls=1)),
                         ["edit", "test", "explore", "edit_guess", "test"])

    def test_no_guess_after_anything_but_reading(self):
        calls = [K(0, 1, "git", "git commit -m x"), K(100, 5, "run", "python a.py")]
        self.assertEqual(kinds(seg(calls, absorb_calls=1)), ["git", "run"])

    def test_regularity_is_the_population_stdev(self):
        # gaps 100, 59, 100, 59, 100: the population stdev is 0.240 of the mean, the sample's 0.269
        b = seg([K(t, 0.3, "slurm", "squeue --me -h") for t in (0, 100, 159, 259, 318, 418)])
        self.assertEqual(kinds(b), ["wait"])

    def test_a_poll_split_by_other_work_counts_its_own_times(self):
        calls = [K(i * 60.0, 0.3, "slurm", "squeue -u me") for i in range(10)] \
            + [K(250, 200, "build", "make -j8")]
        b = seg(calls)
        self.assertEqual([(p["kind"], p["times"]) for p in b["list"]],
                         [("wait", 5), ("build", None), ("wait", 5)])

    def test_calls_in_one_second_are_one_step(self):
        # Codex starts `git diff --check` in the same second as a test run: the pause before that
        # second is the pause before the test.
        calls = [K(i * 10, 2, "explore", "cat f%d" % i) for i in range(10)] \
            + [K(200, 0.1, "explore", "git diff --check"), K(200.4, 30, "test", "pytest -x")]
        b = seg(calls)
        self.assertEqual(kinds(b), ["explore", "edit_guess", "test"])
        self.assertEqual(b["list"][1]["guess"], "a pause after reading, before the test run")

    def test_runs_of_two_commands_are_not_one_loop(self):
        calls = []
        for k, t in enumerate((0, 60, 120, 180)):
            calls += [K(t, 5, "test", "pytest %s" % "ab"[k % 2]), K(t + 10, 1, "explore", "cat")]
        self.assertNotIn("loop", kinds(seg(calls, absorb_calls=1)))

    def test_a_short_pause_between_two_runs_leaves_one_phase(self):
        b = seg([K(0, 5, "test", "pytest -q"), K(30, 5, "test", "pytest -q")], absorb_calls=1)
        self.assertEqual([(p["kind"], p["n"], p["slices"]) for p in b["list"]],
                         [("test", 2, [[5, 30]])])

    def test_random_sessions_keep_the_invariants(self):
        # At the pair these tests were written at, and at the tuned defaults (review Minor 3).
        rng = random.Random(4)
        cats = ("test", "run", "build", "edit", "git", "wait", "explore", "unknown", "slurm")
        tuned = {k: DEFAULTS[k] for k in WRITTEN_AT}
        for _ in range(400):
            calls, t = [], rng.uniform(0, 1e9)
            for _ in range(rng.randint(0, 60)):
                t += rng.choice((0.2, 3, 15, 40, 90, 200, 400)) * rng.uniform(0.9, 1.1)
                cat = rng.choice(cats)
                calls.append(K(t, rng.choice((0.1, 2, 30, 120)), cat,
                               "%s %d" % (cat, rng.randint(0, 3)),
                               flags=OPEN if rng.random() < 0.05 else 0,
                               past=t + 5000 if rng.random() < 0.05 else None))
            given = list(calls)
            guess = rng.random() < 0.8
            for pair in (WRITTEN_AT, tuned):
                with self.subTest(pair=pair):
                    self.check_invariants(calls, given, pair, guess)
            rng.shuffle(calls)
            if len({c[0] for c in calls}) == len(calls):
                for pair in (WRITTEN_AT, tuned):
                    self.assertEqual(seg(calls, guess=True, **pair), seg(given, guess=True, **pair))

    def check_invariants(self, calls, given, pair, guess):
        ps = seg(calls, guess=guess, **pair)["list"]
        self.assertEqual(calls, given)
        self.assertEqual(sum(p["n"] for p in ps), len(calls))
        for p, q in zip(ps, ps[1:]):
            self.assertEqual(p["t1"], q["t0"])
            self.assertFalse(p["kind"] == q["kind"] and p["kind"] not in ("idle", "edit_guess"),
                             "two neighbours of one kind: %s" % p["kind"])
        if calls:                       # the lane ends when everything the agent started has
            self.assertEqual(ps[-1]["t1"], round(max(
                c[1] if c[7] is None else max(c[1], c[7]) for c in calls), 3))
        for p in ps:
            self.assertLessEqual(p["t0"], p["t1"])
            if p["kind"] in ("idle", "edit_guess"):
                self.assertEqual(p["n"], 0)
                self.assertEqual(p["guess"] is not None, p["kind"] == "edit_guess")
            if p["kind"] == "edit_guess":
                self.assertTrue(20 <= p["t1"] - p["t0"] < 300)
        # A phase holds the next `n` calls by start, and one that is not a loop holds a call of its
        # own kind, as read once the polling is: an absorbed run never names what took it in.
        cs = sorted(calls, key=lambda c: c[0])
        held = [c[2] for c in cs]
        _polling(cs, held, PREVIEWS, dict(DEFAULTS, **pair))
        i = 0
        for p in ps:
            mine, i = held[i:i + p["n"]], i + p["n"]
            if p["n"] and p["kind"] != "loop":
                self.assertIn(p["kind"], mine, "a %s phase holding %s" % (p["kind"], sorted(set(mine))))
            if p["kind"] == "unknown":                  # R66: never over a call of a known kind
                self.assertEqual(set(mine), {"unknown"}, "an unknown phase holding %s" % sorted(set(mine)))

    def test_unknown_calls_on_a_timer_stay_unknown(self):
        b = seg([K(i * 60.0, 1, "unknown", "codex --codex-run-as-fs-helper") for i in range(6)])
        self.assertEqual(kinds(b), ["unknown"])

    def test_an_open_loop_says_what_it_waits_on(self):
        loop = "until squeue -j 5 | grep -q R; do sleep 30; done"
        b = seg([K(0, 2, "explore", "ls"), K(10, 600, "wait", loop, flags=OPEN)], absorb_calls=1)
        p = b["list"][-1]
        self.assertEqual((p["kind"], p["waits_on"], p["every_s"], p["times"], p["open"]),
                         ("wait", "slurm", None, None, True))

    def test_a_phase_holding_a_known_call_is_never_named_unknown(self):       # R66
        # The review's case: five shells whose argv was not kept, over 400 s, beside eight short
        # reads, Slurm checks, and runs. Their long runs took the short ones in and named the
        # phase "commands not recorded"; it is named for the known kind with the most calls, and
        # its mix says the rest.
        calls = [K(80.0 * i, 79.0, "unknown", None) for i in range(5)]
        calls += [K(t, 1.0, cat, text) for t, cat, text in (
            (40, "explore", "ls a"), (120, "slurm", "squeue -u me"), (200, "explore", "cat b"),
            (240.5, "run", "python x.py"), (280, "explore", "rg c"), (320.5, "slurm", "sacct -j 5"),
            (360, "run", "python y.py"), (390, "explore", "ls d"))]
        b = segment(calls, PhaseLog(), PREVIEWS, dict(DEFAULTS), True)
        self.assertEqual([(p["kind"], p["n"], p["mix"]) for p in b["list"]],
                         [("explore", 13, [["explore", 4], ["unknown", 5], ["slurm", 2]])])
        # A tie on calls goes to the kind seen first
        calls = [K(80.0 * i, 79.0, "unknown", None) for i in range(4)]
        calls += [K(t, 1.0, cat, text) for t, cat, text in (
            (40, "slurm", "squeue -u me"), (120, "explore", "ls a"), (200, "slurm", "sacct -j 5"),
            (280, "explore", "cat b"))]
        b = segment(calls, PhaseLog(), PREVIEWS, dict(DEFAULTS), True)
        self.assertEqual([(p["kind"], p["n"]) for p in b["list"]], [("slurm", 8)])
        # A phase of unknown calls alone is still "commands not recorded"
        b = segment([K(80.0 * i, 79.0, "unknown", None) for i in range(4)], PhaseLog(), PREVIEWS,
                    dict(DEFAULTS), True)
        self.assertEqual([(p["kind"], p["n"]) for p in b["list"]], [("unknown", 4)])

    def test_top_lists_the_phases_own_kind_first_then_by_count_then_the_earlier(self):   # R70
        # 906929's shape on :8091: a wait whose previews are all distinct, two git calls first, so
        # its top three began with `git add` and `git commit`. The waits come first now.
        calls = [K(0, 1, "git", "git add -A"), K(5, 1, "git", "git commit -m wip"),
                 K(10, 60, "wait", "sleep 60"), K(75, 60, "wait", "until [ -f out ]; do sleep 5; done"),
                 K(140, 1, "explore", "cat out")]
        (p,) = seg(calls, absorb_calls=6, absorb_s=300)["list"]
        self.assertEqual(p["kind"], "wait")
        self.assertEqual(p["top"], [["sleep 60", 1], ["until [ -f out ]; do sleep 5; done", 1],
                                    ["git add -A", 1]])
        # then by count, and a tie to the earlier; the git it took in, though it ran as often, after
        calls = [K(0, 1, "explore", "ls"), K(3, 1, "explore", "cat a"), K(6, 1, "explore", "cat a"),
                 K(9, 1, "explore", "rg x"), K(12, 1, "git", "git status"), K(15, 1, "git", "git status")]
        (p,) = seg(calls, absorb_calls=6, absorb_s=300)["list"]
        self.assertEqual(p["kind"], "explore")
        self.assertEqual(p["top"], [["cat a", 2], ["ls", 1], ["rg x", 1]])
        # a loop's own kind is test: its test ahead of the git it reads between runs
        loop = []
        for k in range(3):
            t = 300.0 * k
            loop += [K(t, 20, "test", "pytest -q"), K(t + 60, 1, "git", "git status"),
                     K(t + 70, 1, "git", "git diff"), K(t + 80, 1, "edit", "sed -i s/a/b/ f.py")]
        (q,) = [x for x in seg(loop)["list"] if x["kind"] == "loop"]
        self.assertEqual(q["top"][0], ["pytest -q", 3])

    def test_a_pace_names_the_command_whose_starts_set_it(self):        # R65
        # Six `squeue -u me` calls every 120 s, and a call still running whose first child gave the
        # same text, started on the pace. The pace is the polled command's, named by `every_cmd`
        # from the calls that ran it; the open call adds to `times`, and to neither `top` nor
        # `ran_past`, and says which programs ran under it.
        calls = [K(120.0 * i, 2, "slurm", "squeue -u me") for i in range(6)]
        calls.append(K(720.0, 1500, "slurm", "squeue -u me", flags=OPEN) + (("squeue", "sleep"),))
        calls.append(K(900.0, 1, "explore", "ls"))
        p = seg(calls, absorb_calls=1)["list"][0]
        self.assertEqual((p["kind"], p["every_s"], p["times"], p["every_cmd"]),
                         ("wait", 120.0, 7, "squeue -u me"))
        self.assertEqual(p["top"], [["squeue -u me", 6]])
        self.assertEqual((p["open"], p["seen"], p["ran_past"]), (True, ["squeue", "sleep"], None))
        # a pace set by calls still running alone is named for the first program seen under them
        alone = [K(120.0 * i, 30, "wait", "sleep 20", flags=OPEN) + (("sleep",),) for i in range(6)]
        q = seg(alone)["list"][0]
        self.assertEqual((q["every_s"], q["times"], q["every_cmd"], q["top"]), (120.0, 6, "sleep", []))

    def test_a_poll_that_sleeps_first_says_what_it_polls(self):         # R60
        # `sleep 120 && squeue -u $USER` again and again: each call begins by sleeping two minutes,
        # so it is a wait (R58), and it waits on the scheduler it then asks: on the page, "waiting
        # on Slurm · ... every 2 min", not on a fixed sleep.
        calls = [K(125.0 * i, 121, "wait", "sleep 120 && squeue -u $USER") for i in range(8)]
        p = seg(calls)["list"][0]
        self.assertEqual((p["kind"], p["waits_on"], p["every_s"], p["times"]),
                         ("wait", "slurm", 125.0, 8))

    def test_a_finished_wait_says_what_it_waited_on(self):
        loop = "until [ -f out ]; do sleep 5; done"
        b = seg([K(0, 2, "explore", "ls"), K(10, 600, "wait", loop)], absorb_calls=1)
        p = b["list"][-1]
        self.assertEqual((p["kind"], p["waits_on"], p["every_s"], p["open"]),
                         ("wait", "output", None, False))

    def test_only_a_wait_says_what_it_waits_on(self):
        p = seg([K(i * 10, 2, "explore", "ls %d" % i) for i in range(5)])["list"][0]
        self.assertEqual((p["waits_on"], p["every_s"], p["times"], p["rounds"], p["slices"],
                          p["guess"], p["ran_past"], p["mix"], p["open"]),
                         (None, None, None, None, None, None, None, [], False))

    def test_a_busy_hour_of_distinct_commands_reads_as_a_handful(self):
        # The brief's busy hour repeats `cat f0` every 60 s and `python check.py` every 360 s, so
        # polling reads it all as waiting. Here no command repeats: it is the absorb pass's work.
        calls = []
        for m in range(60):
            calls += [K(m * 60 + i * 3, 1, "explore", "cat f%d_%d" % (m, i)) for i in range(4)]
            if m % 6 == 0:
                calls.append(K(m * 60 + 30, 2, "run", "python check_%d.py" % m))
        b = seg(calls)
        self.assertLessEqual(len(b["list"]), 6)
        self.assertNotIn("wait", kinds(b))


class TunedTest(unittest.TestCase):
    """The defaults as tuned on real days (phase_report): a busy hour reads as a handful."""

    @staticmethod
    def cycles(n, t=0.0):
        """An agent at work, as on rtx6000a on 2026-10-29: every four minutes it reads, pauses,
        runs a script, edits, and tests, with no command repeated."""
        calls = []
        for c in range(n):
            calls += [K(t + i * 5, 1, "explore", "sed -n %d,40p f%d.py" % (i, c)) for i in range(4)]
            calls += [K(t + 60, 30, "run", "python probe_%d.py" % c), K(t + 100, 1, "edit", "cp a b%d" % c),
                      K(t + 130, 20, "test", "pytest -q tests/test_%d.py" % c)]
            t += 240
        return calls

    def test_a_busy_hour_of_edit_test_work_reads_as_a_handful(self):
        tuned = segment(self.cycles(15), PhaseLog(), PREVIEWS, dict(DEFAULTS), True)
        self.assertLessEqual(len(tuned["list"]), 6)
        self.assertGreaterEqual(len(seg(self.cycles(15))["list"]), 30)    # at the pair it was

    def test_a_busy_hour_at_the_defaults_is_one_phase_with_its_pauses_as_slices(self):
        # What the tuned defaults give (review Minor 3): the reading, the runs, the copies, and
        # the tests are one phase, named for the 60 reads among its 105 calls, which hold over
        # half of them, so no mix is listed. Each cycle's pause before its run is a guess kept as
        # a slice of it, never a phase of its own. A phase that is not a loop holding a call of
        # its own kind is `test_random_sessions_keep_the_invariants`'s, at both pairs.
        b = segment(self.cycles(15), PhaseLog(), PREVIEWS, dict(DEFAULTS), True)
        self.assertEqual([(p["kind"], p["n"]) for p in b["list"]], [("explore", 105)])
        p = b["list"][0]
        self.assertEqual(p["mix"], [])
        self.assertEqual(len(p["slices"]), 15)
        self.assertEqual(p["guess"], "a pause after reading, before the run")
        self.assertEqual(p["top"][0][1], 1)                   # no command repeats

    def test_a_long_run_is_never_absorbed_and_keeps_its_kind(self):
        # It takes the small runs beside it in, and stays a run: a phase of its own between them.
        calls = self.cycles(2) + [K(480, 1800, "run", "python train.py")] + self.cycles(2, 2300)
        b = segment(calls, PhaseLog(), PREVIEWS, dict(DEFAULTS), True)
        self.assertEqual(kinds(b), ["explore", "run", "explore"])
        run = b["list"][1]
        self.assertTrue(run["t0"] <= 480 and run["t1"] >= 2280, run)


class MovedPastTest(unittest.TestCase):
    """The user's ruling of 2026-11-02: only what the agent is still waiting on holds a gap. A call
    still running when a later step begins is one the agent moved past: it holds no idle gap and
    no guess, the lane still runs as long as it does, and an idle gap names the calls that are
    still running as it begins (`alive`)."""

    def test_a_call_the_agent_moved_past_holds_no_idle(self):
        b = seg([K(0, 50000, "run", "linktest"), K(60, 1, "explore", "ls a"),
                 K(9000, 1, "explore", "ls b")])
        idle = [p for p in b["list"] if p["kind"] == "idle"]
        self.assertEqual([(p["t0"], p["t1"]) for p in idle], [(61.0, 9000.0)])
        self.assertEqual(idle[0]["alive"], [["linktest", 0.0]])

    def test_a_call_with_no_later_call_still_holds_the_gap(self):
        b = seg([K(0, 1, "explore", "ls"), K(10, 5000, "test", "pytest -x")])
        self.assertNotIn("idle", kinds(b))

    def test_a_long_call_in_a_parallel_batch_holds_the_gap(self):
        b = seg([K(0, 3000, "test", "pytest -x"), K(0.5, 1, "explore", "ls"),
                 K(3100, 1, "explore", "ls b")])
        self.assertNotIn("idle", kinds(b))

    def test_an_idle_gap_with_nothing_running_has_no_alive(self):
        b = seg([K(0, 1, "explore", "ls a"), K(1000, 1, "explore", "ls b")])
        self.assertIsNone(next(p for p in b["list"] if p["kind"] == "idle")["alive"])

    def test_the_gap_begins_when_the_step_before_it_ends(self):
        # The call is the last the agent started before the gap: it is waited on until it ends.
        b = seg([K(0, 1, "explore", "ls"), K(10, 5000, "test", "pytest -x"),
                 K(5400, 1, "explore", "ls b")])
        idle = [p for p in b["list"] if p["kind"] == "idle"]
        self.assertEqual([(p["t0"], p["t1"], p["alive"]) for p in idle], [(5010, 5400, None)])

    def test_the_lane_still_runs_as_long_as_a_call_the_agent_moved_past(self):
        # moved past (2026-11-02 ruling): it holds no gap, but the lane does not end while it runs,
        # and its own phase lists it as having kept running
        b = seg([K(0, 50000, "run", "linktest"), K(60, 1, "explore", "ls a"),
                 K(9000, 1, "explore", "ls b")])
        self.assertEqual([(p["kind"], p["t0"], p["t1"]) for p in b["list"]],
                         [("run", 0, 61), ("idle", 61, 9000), ("explore", 9000, 50000)])
        self.assertEqual(b["list"][0]["ran_past"], [["linktest", 49939.0]])

    def test_only_the_step_before_the_gap_holds_it(self):
        # The second step ends at 5000, so the gap after it begins there, though the first step's
        # call runs on; and the second step's own call is no one the agent moved past.
        b = seg([K(0, 50000, "run", "linktest"), K(100, 4900, "test", "pytest -x"),
                 K(6000, 1, "explore", "ls")])
        idle = [p for p in b["list"] if p["kind"] == "idle"]
        self.assertEqual([(p["t0"], p["t1"], p["alive"]) for p in idle],
                         [(5000, 6000, [["linktest", 0.0]])])

    def test_a_call_the_agent_moved_past_holds_no_guess_either(self):
        # moved past (2026-11-02 ruling): the pause after reading is the agent's, not the call's
        b = seg([K(0, 50000, "run", "linktest"), K(60, 2, "explore", "cat a"),
                 K(160, 5, "run", "python a.py")], absorb_calls=1)
        self.assertEqual(kinds(b), ["run", "explore", "edit_guess", "run"])
        self.assertEqual((b["list"][2]["t0"], b["list"][2]["t1"]), (62, 160))

    def test_alive_names_up_to_three_calls_earliest_start_first(self):
        longs = [K(2 * i, 50000, "run", name) for i, name in enumerate("abcd")]
        b = seg(longs[::-1] + [K(60, 1, "explore", "ls a"), K(9000, 1, "explore", "ls b")])
        (idle,) = [p for p in b["list"] if p["kind"] == "idle"]
        self.assertEqual(idle["alive"], [["a", 0.0], ["b", 2.0], ["c", 4.0]])

    def test_alive_is_what_runs_as_the_gap_begins_though_it_may_end_inside_it(self):
        # `short` ended before the gap began; `mid` is running when it does, and ends at 3005
        b = seg([K(0, 30, "run", "short"), K(5, 3000, "run", "mid"), K(60, 1, "explore", "ls a"),
                 K(9000, 1, "explore", "ls b")])
        (idle,) = [p for p in b["list"] if p["kind"] == "idle"]
        self.assertEqual((idle["t0"], idle["alive"]), (61, [["mid", 5.0]]))

    def test_a_call_that_ends_as_the_gap_begins_is_not_alive(self):
        # the first call is still running when the second step starts, and ends with the step
        b = seg([K(0, 61, "run", "ends with it"), K(60, 1, "explore", "ls a"),
                 K(9000, 1, "explore", "ls b")])
        (idle,) = [p for p in b["list"] if p["kind"] == "idle"]
        self.assertEqual((idle["t0"], idle["alive"]), (61, None))

    def test_alive_rounds_since_to_a_millisecond(self):
        b = seg([K(0.12345, 50000, "run", "linktest"), K(60, 1, "explore", "ls a"),
                 K(9000, 1, "explore", "ls b")])
        (idle,) = [p for p in b["list"] if p["kind"] == "idle"]
        self.assertEqual(idle["alive"], [["linktest", 0.123]])

    def test_a_call_without_a_preview_is_alive_with_none(self):
        b = seg([K(0, 50000, "unknown", None), K(60, 1, "explore", "ls a"),
                 K(9000, 1, "explore", "ls b")])
        (idle,) = [p for p in b["list"] if p["kind"] == "idle"]
        self.assertEqual(idle["alive"], [[None, 0.0]])

    def test_an_open_call_is_alive_but_not_named_by_its_first_child(self):       # R65
        # An open call's text is its first child's, no command of its own: as `top` and `ran_past`
        # never name it, the gap says a call is still running and no more.
        loop = K(0, 50000, "wait", "sleep 20", flags=OPEN) + (("sleep",),)
        b = seg([loop, K(60, 1, "explore", "ls a"), K(9000, 1, "explore", "ls b")])
        (idle,) = [p for p in b["list"] if p["kind"] == "idle"]
        self.assertEqual(idle["alive"], [[None, 0.0]])

    def test_every_phase_has_alive_and_only_an_idle_one_can_hold_calls(self):
        calls = [K(i * 10, 2, "explore", "cat f%d" % i) for i in range(10)] \
            + [K(200, 90, "run", "python run.py")] \
            + [K(2000 + i * 120.0, 0.3, "slurm", "squeue -u me") for i in range(6)] \
            + [K(3000 + t, 5, "test", "pytest -q") for t in (0, 95, 230, 300)]
        b = seg(calls)
        self.assertEqual(kinds(b), ["explore", "edit_guess", "run", "idle", "wait", "idle", "loop"])
        for p in b["list"]:
            self.assertIn("alive", p)
            self.assertIsNone(p["alive"])
        b = seg([K(0, 50000, "run", "linktest"), K(60, 1, "explore", "ls a"),
                 K(9000, 1, "explore", "ls b")])
        self.assertEqual([p["alive"] is not None for p in b["list"]], [False, True, False])

    def test_idle_and_alive_by_the_definition_on_random_calls(self):
        # Random calls on a coarse grid, with ties. An idle gap runs from the end of the step before
        # it (calls within `step_s` of a step's first are one step) to the start of the next step,
        # when that is `idle_s` or more. Its `alive` is the first three calls, by start, that began
        # before it and end after it. Every call has its own text.
        rng = random.Random(11)
        starts = (0, 0.5, 1, 1.5, 30, 60, 400, 410, 900, 2000, 2300, 4000, 4000.5)
        spans = (0, 0.5, 5, 90, 350, 1000, 5000, 50000)
        running_gaps = capped_gaps = 0
        for _ in range(500):
            calls = [K(rng.choice(starts), rng.choice(spans), rng.choice(("run", "explore")),
                       "call %d" % i) for i in range(rng.randint(2, 14))]
            by_start = sorted(calls, key=lambda c: c[0])
            steps = []                              # [first start, latest end] of each
            for c in by_start:
                if steps and c[0] - steps[-1][0] <= DEFAULTS["step_s"]:
                    steps[-1][1] = max(steps[-1][1], c[1])
                else:
                    steps.append([c[0], c[1]])
            gaps = [(a[1], b[0]) for a, b in zip(steps, steps[1:])
                    if b[0] - a[1] >= DEFAULTS["idle_s"]]
            phases = seg(calls, absorb_calls=1)["list"]
            idle = [p for p in phases if p["kind"] == "idle"]
            self.assertEqual([(p["t0"], p["t1"]) for p in idle], gaps, calls)
            for p in phases:
                if p["kind"] != "idle":
                    self.assertIsNone(p["alive"])
            for p in idle:
                running = [[PREVIEWS[c[4]], c[0]] for c in by_start if c[0] < p["t0"] < c[1]]
                self.assertEqual(p["alive"], running[:3] or None, calls)
                running_gaps += bool(running)
                capped_gaps += len(running) > 3
        # the grid leaves calls running through many gaps, and more than three through some
        self.assertGreater(running_gaps, 100)
        self.assertGreater(capped_gaps, 10)


class CutsTest(unittest.TestCase):
    """The tasks above the phases (`phase_tasks`) cut a session at the start of each task after
    the first, and no phase holds calls from both sides of a cut. A call that starts at a cut is
    after it. A cut is no phase of its own: it keeps the calls on its two sides apart, as an idle
    gap does, and leaves nothing in the list."""

    def test_no_phase_crosses_a_cut(self):
        b = seg([K(i * 10, 1, "explore", "ls %d" % i) for i in range(20)], cuts=(95.0,))
        self.assertEqual([(p["kind"], p["n"]) for p in b["list"]], [("explore", 10), ("explore", 10)])
        self.assertEqual(b["list"][1]["t0"], 100.0)

    def test_a_short_run_is_not_absorbed_across_a_cut(self):
        b = seg([K(0, 1, "test", "pytest")] + [K(10 + i, 1, "explore", "ls %d" % i) for i in range(8)],
                cuts=(5.0,), absorb_calls=6, absorb_s=300)
        self.assertEqual(kinds(b), ["test", "explore"])

    def test_a_call_that_starts_at_a_cut_is_after_it(self):
        b = seg([K(i * 10, 1, "explore", "ls %d" % i) for i in range(6)], cuts=(30.0,))
        self.assertEqual([(p["n"], p["t0"]) for p in b["list"]], [(3, 0.0), (3, 30.0)])

    def test_two_calls_of_one_step_are_two_phases_when_a_cut_lies_between_them(self):
        calls = [K(10, 0.1, "explore", "ls a"), K(10.4, 0.1, "explore", "ls b")]
        self.assertEqual([p["n"] for p in seg(calls)["list"]], [2])
        for cut in (10.2, 10.4):        # between the two starts, and at the later one
            with self.subTest(cut=cut):
                b = seg(calls, cuts=(cut,))
                self.assertEqual([(p["n"], p["t0"], p["t1"]) for p in b["list"]],
                                 [(1, 10.0, 10.4), (1, 10.4, 10.5)])
        for cut in (10.0, 10.5):        # at the first start, and past the last: nothing on one side
            with self.subTest(cut=cut):
                self.assertEqual([p["n"] for p in seg(calls, cuts=(cut,))["list"]], [2])

    def test_an_idle_gap_before_a_cut_is_unchanged(self):
        calls = [K(i * 10, 2, "explore", "ls %d" % i) for i in range(5)] \
            + [K(700 + i * 10, 2, "explore", "cat %d" % i) for i in range(5)]
        plain = seg(calls)
        self.assertEqual(kinds(plain), ["explore", "idle", "explore"])
        for cut in (500.0, 700.0):      # inside the gap, and where the call that ends it starts
            with self.subTest(cut=cut):
                self.assertEqual(seg(calls, cuts=(cut,)), plain)
        # the gap still lists the calls running as it begins
        calls = [K(0, 50000, "run", "linktest"), K(60, 1, "explore", "ls a"),
                 K(9000, 1, "explore", "ls b")]
        plain = seg(calls)
        self.assertEqual(plain["list"][1]["alive"], [["linktest", 0.0]])
        self.assertEqual(seg(calls, cuts=(9000.0,)), plain)

    def test_a_pause_before_a_cut_stays_with_the_calls_before_it(self):
        # The 30 s pause after reading is a guess, a slice of the run after it, which then begins
        # at 92. With a cut at the run's start the pause is the time of the calls before the cut.
        calls = [K(i * 10, 2, "explore", "cat f%d" % i) for i in range(10)] \
            + [K(122, 90, "run", "python run.py")]
        read, run = seg(calls)["list"]
        self.assertEqual((read["t1"], run["t0"], read["slices"], run["slices"]),
                         (92, 92, None, [[92, 122]]))
        read, run = seg(calls, cuts=(122.0,))["list"]
        self.assertEqual((read["t1"], run["t0"], read["slices"], run["slices"]),
                         (122, 122, [[92, 122]], None))
        self.assertEqual(read["guess"], "a pause after reading, before the run")

    def test_a_loop_does_not_run_across_a_cut(self):
        calls = [K(t, 5, "test", "pytest -q") for t in (0, 95, 230, 300, 430, 495)]
        self.assertEqual([(p["kind"], p["rounds"]) for p in seg(calls)["list"]], [("loop", 6)])
        b = seg(calls, cuts=(300.0,), absorb_calls=6, absorb_s=300)
        self.assertEqual([(p["kind"], p["rounds"], p["n"], len(p["slices"])) for p in b["list"]],
                         [("loop", 3, 3, 3), ("loop", 3, 3, 2)])
        self.assertEqual(b["list"][1]["t0"], 300)

    def test_a_poll_is_two_waits_at_a_cut_and_each_counts_its_own_times(self):
        calls = [K(i * 120.0, 0.3, "slurm", "squeue -h -u $USER -o %j") for i in range(33)]
        b = seg(calls, cuts=(16 * 120.0,))
        self.assertEqual([(p["kind"], p["waits_on"], p["every_s"], p["times"]) for p in b["list"]],
                         [("wait", "slurm", 120.0, 16), ("wait", "slurm", 120.0, 17)])

    def test_a_call_still_running_at_a_cut_stays_with_the_side_it_started_on(self):
        calls = [K(0, 2, "explore", "ls a"), K(10, 2, "explore", "ls b"),
                 K(20, 900, "explore", "find / -name x")] \
            + [K(100 + 10 * i, 1, "explore", "cat %d" % i) for i in range(6)]
        self.assertEqual([p["n"] for p in seg(calls)["list"]], [9])
        b = seg(calls, cuts=(100.0,))
        self.assertEqual([(p["n"], p["t0"], p["t1"]) for p in b["list"]],
                         [(3, 0.0, 100.0), (6, 100.0, 920.0)])
        self.assertEqual(b["list"][0]["ran_past"], [["find / -name x", 820.0]])
        self.assertIsNone(b["list"][1]["ran_past"])

    def test_an_open_call_goes_with_the_side_it_starts_on(self):
        # Alone it is a small run and joins the calls after it; the cut keeps it from the ones before.
        calls = [K(i * 10, 2, "explore", "ls %d" % i) for i in range(4)] \
            + [K(40, 30, "wait", "sleep 20", flags=OPEN) + (("sleep",),)] \
            + [K(100, 2, "explore", "cat a"), K(110, 2, "explore", "cat b")]
        plain = seg(calls, absorb_calls=6, absorb_s=300)
        self.assertEqual([(p["n"], p["open"]) for p in plain["list"]], [(7, True)])
        b = seg(calls, cuts=(40.0,), absorb_calls=6, absorb_s=300)
        self.assertEqual([(p["n"], p["open"], p["seen"]) for p in b["list"]],
                         [(4, False, None), (3, True, ["sleep"])])

    def test_a_cut_with_no_call_on_one_side_changes_nothing(self):
        calls = [K(i * 10, 2, "explore", "ls %d" % i) for i in range(5)] \
            + [K(60, 5, "run", "python a.py")]
        plain = seg(calls)
        self.assertEqual(kinds(plain), ["explore"])
        # at the first call's start, before it, after the last call's start, and both
        for cuts in ((0.0,), (-50.0,), (60.5,), (-1e9, 1e9)):
            with self.subTest(cuts=cuts):
                self.assertEqual(seg(calls, cuts=cuts), plain)
        # at the last call's start the call is alone after it
        self.assertEqual(kinds(seg(calls, cuts=(60.0,))), ["explore", "run"])
        self.assertEqual(seg([], cuts=(5.0,)), seg([]))

    def test_cuts_are_read_as_a_set(self):
        calls = [K(i * 10, 1, "explore", "ls %d" % i) for i in range(20)]
        once = seg(calls, cuts=(45.0, 95.0, 145.0))
        self.assertEqual([p["n"] for p in once["list"]], [5, 5, 5, 5])
        self.assertEqual(seg(calls, cuts=(145.0, 45.0, 95.0, 45.0)), once)
        # two cuts with no call between them are as one
        two = seg(calls, cuts=(95.0, 96.0))
        self.assertEqual([p["n"] for p in two["list"]], [10, 10])
        self.assertEqual(two, seg(calls, cuts=(95.0,)))

    def test_the_cuts_given_are_not_changed(self):
        cuts = [145.0, 45.0, 95.0, 45.0]
        seg([K(i * 10, 1, "explore", "ls %d" % i) for i in range(20)], cuts=cuts)
        self.assertEqual(cuts, [145.0, 45.0, 95.0, 45.0])

    def test_calls_read_once_with_prepared_give_the_same_block_with_cuts(self):
        # The tasks read the calls with `prepared`, cut them, and give `segment` the read and the
        # cuts. The polls make the read matter: one cut falls between two of them.
        calls = [K(i * 10, 1, "explore", "ls %d" % i) for i in range(10)] \
            + [K(1000 + i * 120.0, 0.3, "slurm", "squeue --me") for i in range(8)]
        params = dict(DEFAULTS, **WRITTEN_AT)
        read = prepared(calls, PREVIEWS, params)
        cuts = (45.0, 1480.0)
        b = segment(calls, PhaseLog(), PREVIEWS, params, True, prepared=read, cuts=cuts)
        self.assertEqual(b, seg(calls, cuts=cuts))
        self.assertEqual([(p["kind"], p["n"], p["times"]) for p in b["list"]],
                         [("explore", 5, None), ("explore", 5, None), ("idle", 0, None),
                          ("wait", 4, 4), ("wait", 4, 4)])

    def test_random_sessions_never_cross_a_cut(self):
        # At the pair the mechanism tests were written at, and at the tuned defaults. A cut falls on
        # a call's start or a little either side of one, inside a step as well as between steps.
        rng = random.Random(9)
        cats = ("test", "run", "build", "edit", "git", "wait", "explore", "unknown", "slurm")
        tuned = {k: DEFAULTS[k] for k in WRITTEN_AT}
        cut_something = 0
        for _ in range(300):
            calls, t = [], rng.uniform(0, 1e9)
            for _ in range(rng.randint(0, 60)):
                t += rng.choice((0.2, 3, 15, 40, 90, 200, 400)) * rng.uniform(0.9, 1.1)
                cat = rng.choice(cats)
                calls.append(K(t, rng.choice((0.1, 2, 30, 120)), cat,
                               "%s %d" % (cat, rng.randint(0, 3)),
                               flags=OPEN if rng.random() < 0.05 else 0,
                               past=t + 5000 if rng.random() < 0.05 else None))
            starts = sorted(c[0] for c in calls)
            cuts = [s + rng.choice((-0.1, 0, 0.1, 0.5))
                    for s in (rng.choices(starts, k=rng.randint(0, 5)) if starts else [])]
            guess = rng.random() < 0.8
            for pair in (WRITTEN_AT, tuned):
                with self.subTest(pair=pair):
                    cut_something += self.check_cuts(calls, cuts, pair, guess)
        self.assertGreater(cut_something, 200)      # most of the sessions have a cut that matters

    def check_cuts(self, calls, cuts, pair, guess):
        """Check the phases of `calls` cut at `cuts`; 1 if the cuts changed them, else 0."""
        b = seg(calls, guess=guess, cuts=cuts, **pair)
        ps, ordered = b["list"], sorted(cuts)

        def side(call):                 # how many cuts are at or before its start
            return bisect_right(ordered, call[0])

        cs = sorted(calls, key=lambda c: c[0])
        held, i = [], 0
        for p in ps:
            held.append(cs[i:i + p["n"]])
            i += p["n"]
        self.assertEqual(i, len(cs))
        for p, mine in zip(ps, held):
            self.assertLessEqual(len({side(c) for c in mine}), 1,
                                 "a %s phase across a cut" % p["kind"])
            if mine and side(mine[0]):  # it begins at or after the last cut before its calls
                self.assertGreaterEqual(p["t0"], ordered[side(mine[0]) - 1] - 0.001, p["kind"])
        for (p, mine), (q, next_mine) in zip(zip(ps, held), zip(ps[1:], held[1:])):
            self.assertEqual(p["t1"], q["t0"])
            if mine and next_mine and p["kind"] == q["kind"] and p["kind"] != "loop":
                self.assertNotEqual(side(mine[-1]), side(next_mine[0]),
                                    "two neighbours of one kind, no cut between: %s" % p["kind"])
        if calls:                       # the lane ends when everything the agent started has
            self.assertEqual(ps[-1]["t1"], round(max(
                c[1] if c[7] is None else max(c[1], c[7]) for c in calls), 3))
            # a cut before the first call or after the last one changes nothing
            outside = cuts + [min(c[0] for c in calls) - 5.0, max(c[0] for c in calls) + 5.0]
            self.assertEqual(seg(calls, guess=guess, cuts=outside, **pair), b)
        return int(b != seg(calls, guess=guess, **pair))


if __name__ == "__main__":
    unittest.main()
