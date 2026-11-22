"""A session at a glance: its category, the uses it rests on, and its summary (`phase_summary`).

The rules are read off hand-built blocks first, where every number is known, and then through the
reducer, where the blocks are the segmenter's own: a row's block carries the three, and a session
across midnight carries one of each.

    cd dashboard && uv run python -m unittest discover -s tests -p 'test_phase_summary.py' -v
"""
import copy
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from rc_dashboard import config as configmod                            # noqa: E402
from rc_dashboard.reducers import phase_segment as SEG                  # noqa: E402
from rc_dashboard.reducers import phase_summary as PS                   # noqa: E402
from rc_dashboard.reducers.phase_log import TAGS, PhaseLog              # noqa: E402
from rc_dashboard.reducers.phase_rules import norm_hash                 # noqa: E402
from test_sessions import reduce                                        # noqa: E402
from test_traj_phases import bash8, root8, row_of, s8                   # noqa: E402
from test_traj_tasks import (T1, around_midnight, day_rows, fed, reads)  # noqa: E402

# The tag sets the hand-built calls name, by id; 0 is the empty set, as in a reducer.
TAGSETS = [
    ((), None),
    ((("job", "train"), ("run", "train.sbatch")), None),
    ((("read", "a.py"), ("grep", "loader")), None),
    ((("read", "b.py"),), None),
    ((("write", "b.py"), ("test", "tests/test_b.py")), ("commit", "Fix the loader", "")),
    ((("write", "c.py"),), ("commit", "Speed up the loader", "")),
    ((("pkg", "numpy"), ("pkg", "scipy")), None),
    ((("run", "plot.py"), ("write", "fig.png")), None),
]


def call(tags=0):
    """A call that names the tag set `tags`; nothing else of it is read."""
    c = [0] * (TAGS + 1)
    c[TAGS] = tags
    return tuple(c)


def ph(kind, t0, t1, n=1, waits_on=None, every_s=None):
    return {"kind": kind, "t0": t0, "t1": t1, "n": n, "waits_on": waits_on, "every_s": every_s}


def block(phases, n_calls=None, tasks=1, quoted=0, lone=0, hk=0):
    """A block of `phases`, with `tasks` tasks of which `quoted` are in the agent's words, `lone`
    lone stretches, and `hk` commands of housekeeping. `n_calls` is the phases' calls unless
    given."""
    ts = [{"kind": "task", "source": "commit" if i < quoted else None} for i in range(tasks)]
    ts += [{"kind": "lone", "source": None} for _ in range(lone)]
    return {"n_calls": sum(p["n"] for p in phases) if n_calls is None else n_calls,
            "housekeeping": {"n": hk}, "list": phases, "tasks": ts}


def kind(b, calls=(), params=None):
    return PS.summarize(b, list(calls), TAGSETS, params)


def cut(rows):
    """The block `phase_segment` cuts, with its tuned thresholds, from calls `(start, end, category,
    text)`, or `(..., past)` for one whose process outlived it, and the calls as it sorts them: what a
    reducer hands the summary."""
    previews = [None] + [r[3] for r in rows]
    calls = [(float(r[0]), float(r[1]), r[2], norm_hash(r[3]), i + 1, 0, None,
              r[4] if len(r) > 4 else None, 0) for i, r in enumerate(rows)]
    prep = SEG.prepared(calls, previews, SEG.DEFAULTS)
    return SEG.segment(calls, PhaseLog(), previews, SEG.DEFAULTS, prepared=prep), prep[0]


class CategoryTest(unittest.TestCase):
    def test_the_use_that_holds_half_the_work_or_more_is_the_category(self):
        got = kind(block([ph("run", 0, 600, 3), ph("explore", 600, 1000, 20)]))
        self.assertEqual(got["category"], "run")
        self.assertEqual(got["uses"], [["run", 60.0], ["explore", 40.0]])

    def test_no_use_at_half_is_a_mix(self):
        got = kind(block([ph("slurm", 0, 390), ph("explore", 390, 770),
                          ph("run", 770, 1000)]))
        self.assertEqual(got["category"], "mixed")
        self.assertEqual([u for u, _ in got["uses"]], ["hpc", "explore", "run"])

    def test_a_wait_on_slurm_is_slurm_work(self):
        got = kind(block([ph("slurm", 0, 60, 2), ph("wait", 60, 3660, 30, "slurm", 120.0),
                          ph("explore", 3660, 4000, 40)]))
        self.assertEqual(got["category"], "hpc")
        self.assertEqual(got["uses"][0][0], "hpc")

    def test_any_other_wait_is_no_use_and_the_rest_decides(self):
        got = kind(block([ph("run", 0, 100, 1), ph("wait", 100, 7300, 60, "output", 60.0),
                          ph("explore", 7300, 7350, 10)]))
        self.assertEqual(got["category"], "run")
        self.assertEqual(got["uses"], [["run", 66.7], ["explore", 33.3]])

    def test_edits_tests_loops_and_guesses_are_develop(self):
        for k in ("edit", "edit_guess", "test", "loop"):
            with self.subTest(kind=k):
                self.assertEqual(kind(block([ph(k, 0, 100, 0 if k == "edit_guess" else 3),
                                             ph("explore", 100, 120, 3)]))["category"],
                                 "develop")

    def test_build_and_git_are_their_own_uses(self):
        self.assertEqual(kind(block([ph("build", 0, 100)]))["category"], "build")
        self.assertEqual(kind(block([ph("git", 0, 100)]))["category"], "git")

    def test_phases_with_no_length_are_weighed_by_their_calls(self):
        got = kind(block([ph("explore", 5, 5, 1), ph("edit", 5, 5, 2)]))
        self.assertEqual(got["category"], "develop")
        self.assertEqual(got["uses"], [["develop", 66.7], ["explore", 33.3]])

    def test_a_tie_goes_to_more_calls_then_to_the_first_use(self):
        self.assertEqual(kind(block([ph("run", 0, 100, 1), ph("explore", 100, 200, 9)])
                              )["uses"][0][0], "explore")
        self.assertEqual(kind(block([ph("run", 0, 100, 1), ph("explore", 100, 200, 1)])
                              )["uses"][0][0], "explore")

    def test_a_hung_call_is_running_and_not_the_last_phase_s_work(self):
        # S1 of the review: `./linktest --all` hangs from 0 to 50,400 s, and six `sed -i` come at
        # 300 to 351 s. The lane runs the edits' phase to 50,400 s; it was "Mostly changed files
        # from the shell (99% of 14 h of work)" with develop 99.4, run 0.6.
        b, calls = cut([(0, 50400, "run", "./linktest --all")]
                       + [(300 + g, 301 + g, "edit", "sed -i s/a/b%d/ conf%d.txt" % (i, i))
                          for i, g in enumerate([0, 7, 19, 26, 41, 50])])
        got = PS.summarize(b, calls, TAGSETS)
        self.assertEqual(got["category"], "run")
        self.assertEqual(got["uses"], [["run", 99.9], ["develop", 0.1]])
        self.assertEqual(got["summary"], "Only ran programs (14 h of work). 7 tool calls.")

    def test_a_run_left_going_while_the_agent_reads_its_log_is_running_too(self):
        # S2 of the review: `python train.py` runs for 3 h in the background, with 52 quick reads
        # of its log meanwhile. It was "Mostly explored code and files (99% of 3.0 h of work)".
        rows, t, k = [(0, 10800, "run", "python train.py")], 120, 0
        while t < 10700:
            rows.append((t, t + 1, "explore", "tail -n %d train.log" % (5 + k)))
            t += 150 + (k * 37) % 120
            k += 1
        b, calls = cut(rows)
        got = PS.summarize(b, calls, TAGSETS)
        self.assertEqual(got["category"], "run")
        self.assertEqual(got["uses"], [["run", 50.6], ["explore", 49.4]])
        # the run counts beside the reading, so the work is longer than the 3 h it was done in, and
        # says so: "5.9 h of work" alone reads as the session's length
        self.assertTrue(got["summary"].startswith(
            "Mostly ran programs (51% of 5.9 h of work in 3.0 h), then explored code and files (49%)."),
            got["summary"])

    def test_the_last_phase_is_weighed_to_its_own_calls_and_ran_on_time_to_its_call_s_use(self):
        # by hand, as segment says it: 300 s of running, then 51 s of edits whose phase the lane
        # runs to 50,400 s while the run's call goes on
        got = kind(block([dict(ph("run", 0, 300, 1), own_t1=50400, ran_on=[["run", 50100.0]]),
                          dict(ph("edit", 300, 50400, 6), own_t1=351, ran_on=None)]))
        self.assertEqual(got["uses"], [["run", 99.9], ["develop", 0.1]])
        # a command not recorded that ran on is time not recorded; a wait's is no use
        unk = kind(block([dict(ph("explore", 0, 100, 3), own_t1=5000, ran_on=[["unknown", 4900.0]]),
                          dict(ph("explore", 100, 5000, 3), own_t1=160)]))
        self.assertEqual(unk["category"], "unknown")
        waited = kind(block([dict(ph("explore", 0, 100, 3), own_t1=5000, ran_on=[["wait", 4900.0]]),
                             dict(ph("explore", 100, 5000, 3), own_t1=160)]))
        self.assertEqual((waited["category"], waited["uses"]), ("explore", [["explore", 100.0]]))
        self.assertIn("(2.7 min of work)", waited["summary"])

    def test_a_test_whose_process_ran_on_is_testing_not_changing_files(self):
        # quick reads around a short pytest whose child runs until 10,000 s: the pytest is taken in by
        # the reads' phase, and its time past it is develop's (`ran_on`). Nothing was edited, and it
        # said "Mostly changed files from the shell (99% of 2.8 h of work)".
        b, calls = cut([(10 * i, 10 * i + 1, "explore", "cat src/f%d.py" % i) for i in range(3)]
                       + [(35, 37, "test", "pytest -q tests/test_a.py", 10000.0)]
                       + [(40 + 10 * i, 41 + 10 * i, "explore", "cat src/g%d.py" % i) for i in range(3)])
        self.assertEqual([p["kind"] for p in b["list"]], ["explore"])
        got = PS.summarize(b, calls, TAGSETS)
        self.assertEqual(got["uses"], [["develop", 99.4], ["explore", 0.6]])
        self.assertEqual(got["summary"], "Mostly tested code (99% of 2.8 h of work). 7 tool calls.")

    def test_develop_is_said_by_what_it_held(self):
        # tests alone: no edit anywhere
        self.assertEqual(kind(block([ph("test", 0, 600, 5)]))["summary"],
                         "Only tested code (10 min of work). 5 tool calls.")
        # a test's time past its phase is testing, an edit's editing, as their phases would be
        ran_on = lambda cat: block([dict(ph("explore", 0, 60, 3), own_t1=6000, ran_on=[[cat, 5940.0]]),
                                    dict(ph("explore", 60, 6000, 3), own_t1=120)])
        self.assertTrue(kind(ran_on("test"))["summary"].startswith("Mostly tested code (98% of"))
        self.assertTrue(kind(ran_on("edit"))["summary"].startswith("Mostly changed files from the shell"))
        # a test with an edit the reads' phase took in, or a guessed edit, edited and tested
        edit = [0] * (TAGS + 1)
        edit[2] = "edit"
        self.assertTrue(kind(block([ph("explore", 0, 100, 4), ph("test", 100, 700, 5)]), [tuple(edit)]
                             )["summary"].startswith("Mostly edited and tested code"))
        self.assertTrue(kind(block([dict(ph("test", 0, 600, 5), slices=[[10.0, 40.0]])])
                             )["summary"].startswith("Only edited and tested code"))

    def test_the_work_s_length_is_never_read_as_the_session_s(self):
        # with nothing run on, the work is the phases' time and is said alone
        alone = kind(block([ph("run", 0, 600, 3), ph("explore", 600, 1000, 20)]))
        self.assertIn("(60% of 17 min of work)", alone["summary"])
        # a mix whose work ran side by side says the time it was done in
        side = kind(block([dict(ph("run", 0, 600, 1), own_t1=3600, ran_on=[["run", 3000.0]]),
                           dict(ph("explore", 600, 3600, 30), own_t1=2400),
                           dict(ph("idle", 3600, 9000, 0)),
                           dict(ph("build", 9000, 12600, 4), own_t1=12600)]))
        self.assertTrue(side["summary"].startswith("Mixed work (2.8 h in 2.0 h): "), side["summary"])

    def test_commands_not_recorded_for_half_of_the_time_make_the_session_unknown(self):
        got = kind(block([ph("unknown", 0, 900, 5), ph("explore", 900, 1000, 3)]))
        self.assertEqual(got["category"], "unknown")
        # the uses are still the work's
        self.assertEqual(got["uses"], [["explore", 100.0]])
        less = kind(block([ph("unknown", 0, 400, 5), ph("explore", 400, 1000, 3)]))
        self.assertEqual(less["category"], "explore")

    def test_waiting_and_nothing_else_is_wait(self):
        got = kind(block([ph("wait", 0, 86400, 1, "sleep")]))
        self.assertEqual((got["category"], got["uses"]), ("wait", []))

    def test_every_category_is_listed_and_the_uses_are_the_users_tabs(self):
        self.assertEqual(PS.USES, ("explore", "develop", "build", "run", "hpc", "git"))
        self.assertEqual(set(PS.CATEGORIES), set(PS.USES) | {"mixed", "wait", "unknown", "none"})

    def test_mostly_is_a_threshold(self):
        b = block([ph("run", 0, 600, 3), ph("explore", 600, 1000, 20)])
        self.assertEqual(kind(b, params=dict(PS.DEFAULTS, mostly=0.7))["category"], "mixed")
        self.assertEqual(kind(b, params=dict(PS.DEFAULTS, mostly=0.6))["category"], "run")

    def test_it_changes_nothing_it_is_given(self):
        b = block([ph("run", 0, 600, 3), ph("wait", 600, 900, 2, "process")])
        calls = [call(7), call(2)]
        was = (copy.deepcopy(b), list(calls), copy.deepcopy(TAGSETS))
        first = kind(b, calls)
        self.assertEqual((b, calls, TAGSETS), was)
        self.assertEqual(kind(b, calls), first)


class SummaryTest(unittest.TestCase):
    def test_a_slurm_session_says_its_mix_its_names_and_its_wait(self):
        b = block([ph("explore", 0, 250, 40), ph("slurm", 250, 1500, 60),
                   ph("wait", 1500, 18300, 161, "slurm", 120.0), ph("idle", 18300, 20000, 0),
                   ph("edit", 20000, 20146, 10)], tasks=7, quoted=5, lone=2)
        got = kind(b, [call(1), call(1), call(2)])
        self.assertEqual(got["summary"],
                         "Mostly ran jobs on Slurm (98% of 5.1 h of work). 271 tool calls in 7 "
                         "tasks, 5 named in the agent's own words; 1 Slurm job name, 1 script "
                         "run. Waited 4.7 h on Slurm, polling every 2 min.")

    def test_one_use_alone_is_said_as_only_that_use(self):
        got = kind(block([ph("explore", 0, 600, 22)]), [call(2), call(3), call(2)])
        self.assertEqual(got["summary"],
                         "Only explored code and files (10 min of work). 22 tool calls; "
                         "2 files read, 1 search.")

    def test_a_second_use_is_named_when_it_holds_a_tenth_of_the_work(self):
        got = kind(block([ph("run", 0, 800, 5), ph("slurm", 800, 950, 2),
                          ph("explore", 950, 1000, 4)]), [call(7), call(1)])
        self.assertEqual(got["summary"],
                         "Mostly ran programs (80% of 17 min of work), then ran jobs on Slurm "
                         "(15%). 11 tool calls; 2 scripts run, 1 file written, 1 Slurm job "
                         "name.")

    def test_a_mix_names_its_two_largest_uses_and_a_third_that_holds_a_tenth(self):
        got = kind(block([ph("slurm", 0, 3900, 200), ph("explore", 3900, 7700, 300),
                          ph("run", 7700, 8900, 100), ph("test", 8900, 9900, 30)], tasks=3,
                         quoted=3))
        self.assertEqual(got["summary"],
                         "Mixed work (2.8 h): ran jobs on Slurm (39%), explored code and files "
                         "(38%), and ran programs (12%). 630 tool calls in 3 tasks, all named "
                         "in the agent's own words.")

    def test_develop_says_edited_and_tested_only_when_it_tested(self):
        tested = kind(block([ph("loop", 0, 900, 20), ph("explore", 900, 1000, 10)]),
                      [call(4), call(5), call(4)])
        self.assertTrue(tested["summary"].startswith("Mostly edited and tested code (90% of"))
        self.assertIn("2 files written, 1 test file, 2 commits", tested["summary"])
        edits = kind(block([ph("edit", 5, 5, 2)]))
        self.assertEqual(edits["summary"], "Only changed files from the shell. 2 tool calls.")

    def test_counts_are_of_distinct_names_and_at_most_three(self):
        calls = [call(1), call(1), call(7), call(7), call(2)]
        got = kind(block([ph("slurm", 0, 500, 3), ph("run", 500, 800, 2)]), calls)
        self.assertIn("; 1 Slurm job name, 2 scripts run, 1 file written.", got["summary"])

    def test_a_job_is_named_once_by_a_call_by_its_given_name_else_by_its_script(self):
        # `sbatch --job-name=train scripts/job.sbatch`: phase_names files the script's basename as
        # a job too; `sbatch other.sbatch` has no name of its own
        tags = TAGSETS + [((("job", "train"), ("run", "scripts/job.sbatch"),
                            ("job", "job.sbatch")), None),
                          ((("run", "other.sbatch"), ("job", "other.sbatch")), None)]
        got = PS.summarize(block([ph("slurm", 0, 500, 2)]), [call(8), call(9)], tags)
        self.assertIn("; 2 Slurm job names, 2 scripts run.", got["summary"])

    def test_a_task_clause_needs_two_tasks_and_lone_stretches_are_no_tasks(self):
        one = kind(block([ph("explore", 0, 60, 3)], tasks=1, quoted=1, lone=3))
        self.assertEqual(one["summary"], "Only explored code and files (60 s of work). "
                                         "3 tool calls.")
        two = kind(block([ph("explore", 0, 60, 3)], tasks=2))
        self.assertIn("3 tool calls in 2 tasks.", two["summary"])

    def test_waiting_is_said_when_it_holds_a_tenth_of_the_session_but_idle(self):
        short = kind(block([ph("run", 0, 1000, 3), ph("wait", 1000, 1050, 2, "process")]))
        self.assertNotIn("Waited", short["summary"])
        long_ = kind(block([ph("run", 0, 1000, 3), ph("wait", 1000, 1300, 2, "process", 30.0),
                            ph("idle", 1300, 90000, 0)]))
        self.assertIn("Waited 5.0 min on a process, polling every 30 s.", long_["summary"])

    def test_two_targets_are_said_without_a_pace(self):
        got = kind(block([ph("run", 0, 600, 3), ph("wait", 600, 3600, 9, "output", 60.0),
                          ph("wait", 3600, 4800, 4, "sleep")]))
        self.assertIn("Waited 50 min on a file or its output and 20 min on a fixed sleep.",
                      got["summary"])

    def test_work_of_under_a_second_is_said_without_its_length(self):
        self.assertEqual(kind(block([ph("run", 5, 5.007, 1)]))["summary"],
                         "Only ran programs. 1 tool call.")
        mostly = kind(block([ph("run", 5, 5.6, 1), ph("explore", 5.6, 5.9, 2)]))
        self.assertTrue(mostly["summary"].startswith("Mostly ran programs (67% of its work), then"),
                        mostly["summary"])
        mixed = kind(block([ph("run", 0, 0.36, 1), ph("explore", 0.36, 0.675, 2),
                            ph("git", 0.675, 0.9)]))
        self.assertTrue(mixed["summary"].startswith("Mixed work: ran programs (40%)"),
                        mixed["summary"])

    def test_waiting_alone_is_said_once(self):
        got = kind(block([ph("wait", 0, 86406, 3, "sleep")]))
        self.assertEqual(got["summary"], "Only waited: 24 h on a fixed sleep. 3 tool calls.")

    def test_commands_not_recorded_say_their_share(self):
        got = kind(block([ph("unknown", 0, 44612, 150), ph("wait", 44612, 56168, 40, "command"),
                          ph("explore", 56168, 56300, 16)]))
        self.assertEqual(got["summary"], "Commands not recorded for over 99% of 12 h. 206 tool "
                                         "calls. Waited 3.2 h on something else.")
        self.assertEqual(kind(block([ph("unknown", 3, 3, 1)]))["summary"],
                         "Commands not recorded. 1 tool call.")

    def test_a_session_of_no_tool_call_says_what_the_agent_ran_of_its_own(self):
        self.assertEqual(kind(block([], n_calls=0, hk=13))["summary"],
                         "No tool calls, and 13 commands of the agent's own.")
        self.assertEqual(kind(block([], n_calls=0))["summary"], "No tool calls.")

    def test_the_counts_follow_the_uses_named(self):
        got = kind(block([ph("build", 0, 500, 9)]), [call(6), call(7)])
        self.assertIn("; 2 packages or modules, 1 script run.", got["summary"])

    def test_a_share_that_rounds_to_none_or_to_all_says_so(self):
        self.assertEqual(PS._pct(1, 1000), "under 1%")
        self.assertEqual(PS._pct(998, 1000), "over 99%")
        self.assertEqual(PS._pct(1000, 1000), "100%")
        self.assertEqual(PS._pct(125, 1000), "13%")      # a tie rounds up, as the page's


class DurTest(unittest.TestCase):
    def test_a_duration_reads_as_the_page_writes_it(self):
        cases = {0.85: "850 ms", 4.25: "4.3 s", 35: "35 s", 89.6: "90 s", 150: "2.5 min",
                 720: "12 min", 5399: "90 min", 9720: "2.7 h", 50400: "14 h", 267840: "3.1 d"}
        for s, text in cases.items():
            with self.subTest(s=s):
                self.assertEqual(PS.dur(s), text)

    def test_a_pace_drops_a_zero_tenth(self):
        self.assertEqual(PS._pace(120.0), "2 min")
        self.assertEqual(PS._pace(30.0), "30 s")
        self.assertEqual(PS._pace(150.0), "2.5 min")


# ----------------------------------------------------------------- through the reducer
def configured(summary=None):
    tree = configmod.defaults()
    tree["reducers"]["trajectories"]["summary"].update(summary or {})
    return configmod.Config(tree, {}, None)


def explore_session():
    """Twelve reads, one every 30 s."""
    return [root8(T1)] + [bash8(T1 + 30 * i, 201 + i, "cat src/f%d.py" % i) for i in range(12)]


def long_run():
    """Ten quick reads, then a script that runs for fifty minutes, then a read."""
    recs = [root8(T1)] + [bash8(T1 + 10 + 20 * i, 201 + i, "cat src/f%d.py" % i)
                          for i in range(10)]
    recs.append(bash8(T1 + 3220, 301, "python train.py", duration_s=3000))
    recs.append(bash8(T1 + 3230, 302, "cat out.log"))
    return sorted(recs, key=lambda r: r["ts_epoch"])


def slurm_session():
    """A job submitted, then the queue polled every two minutes for forty minutes."""
    recs = [root8(T1), bash8(T1 + 10, 201, "sbatch --job-name=train job.sbatch")]
    recs += [bash8(T1 + 130 + 120 * i, 301 + i, "squeue -u alice") for i in range(20)]
    return recs


class RowTest(unittest.TestCase):
    def block_of(self, recs, c=None):
        return row_of(fed(recs, c).result())["phases"]

    def test_an_agent_rows_block_carries_its_category_uses_and_summary(self):
        b = self.block_of(explore_session())
        self.assertEqual(b["category"], "explore")
        self.assertEqual(b["uses"], [["explore", 100.0]])
        self.assertTrue(b["summary"].startswith("Only explored code and files"), b["summary"])
        self.assertIn("12 tool calls; 12 files read.", b["summary"])
        # it goes out as JSON, and nothing a round trip would change
        served = {k: b[k] for k in ("category", "uses", "summary")}
        self.assertEqual(json.loads(json.dumps(served, allow_nan=False)), served)

    def test_a_long_run_among_quick_reads_is_a_run(self):
        b = self.block_of(long_run())
        self.assertEqual(b["category"], "run")
        self.assertGreater(dict(b["uses"])["run"], 90)

    def test_a_job_and_the_queue_polled_after_it_are_slurm_work(self):
        b = self.block_of(slurm_session())
        self.assertEqual(b["category"], "hpc")
        self.assertIn("1 Slurm job name", b["summary"])
        self.assertIn("on Slurm, polling every 2 min.", b["summary"])

    def test_a_row_with_no_calls_is_none(self):
        chore = s8(T1 + 10, 401, 100, "git", "git -c core.fsmonitor= status --porcelain")
        b = row_of(reduce([root8(T1), chore]))["phases"]
        self.assertEqual((b["n_calls"], b["category"], b["uses"]), (0, "none", []))
        self.assertEqual(b["summary"], "No tool calls, and 1 command of the agent's own.")

    def test_the_configured_threshold_decides(self):
        self.assertEqual(self.block_of(long_run(), configured({"mostly": 0.99}))["category"],
                         "mixed")

    def test_the_configured_thresholds_are_the_defaults(self):
        self.assertEqual(configmod.defaults()["reducers"]["trajectories"]["summary"],
                         PS.DEFAULTS)

    def test_a_session_across_midnight_has_one_category_and_one_summary(self):
        m = around_midnight()
        traj = fed([root8(m - 3500)] + reads(m - 2950, 61, 201))
        first, second = day_rows(traj, m + 3700)
        self.assertNotEqual(first["sid"], second["sid"])
        for k in ("category", "uses", "summary"):
            self.assertEqual(first["phases"][k], second["phases"][k])
        self.assertEqual(first["phases"]["category"], "explore")
        self.assertIn("61 tool calls", first["phases"]["summary"])

    def test_a_person_or_an_unrecognized_agent_has_no_category(self):
        recs = [root8(T1, command="aider", args="aider", agent_type="aider"),
                bash8(T1 + 10, 201, "ls", agent_type="aider")]
        rows = [r for r in fed(recs).result()["rows"].values() if r["agent_pid"] == "100"]
        for r in rows:
            self.assertNotIn("category", r["phases"])


if __name__ == "__main__":
    unittest.main()
