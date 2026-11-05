"""Tasks: a session's calls cut into the stretches of work it was doing (reducers/phase_tasks.py).

`task_spans` is a pure function: the calls of one session, read once by `phase_segment.prepared`,
and the tag set of each call go in, and spans come out, each a task or a lone stretch, covering
every call once. These tests pin the rules the module states: only what the agent is still waiting
on holds a gap open (a call it moved past does not, and a process a finished call left behind
does), a few calls hours from other work are set apart unless every stretch is that small, a name
new to the session that then recurs cuts where it first occurs, a commit that ends a burst of git
cuts only when the work around it changes, a short task merges unless it holds the agent's words,
no session-day shows more than `cap` tasks, and nothing merges across a gap.

`name_tasks` then names the spans: a task is labeled with the agent's own words when it holds any,
else with a verb for what the agent spent most of the task's time on and the names most specific to
the task. Each phase of the segmented session belongs to one task, by position.

    cd dashboard && uv run python -m unittest discover -s tests -p 'test_phase_tasks.py' -v
"""
import copy
import random
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from rc_dashboard.reducers.phase_log import OPEN, PhaseLog  # noqa: E402
from rc_dashboard.reducers.phase_names import KINDS  # noqa: E402
from rc_dashboard.reducers.phase_rules import CATS  # noqa: E402
from rc_dashboard.reducers.phase_segment import DEFAULTS as PHASE_DEFAULTS  # noqa: E402
from rc_dashboard.reducers.phase_segment import prepared, segment  # noqa: E402
from rc_dashboard.reducers.phase_tasks import DEFAULTS, VERBS, WEIGHTS, Span  # noqa: E402
from rc_dashboard.reducers.phase_tasks import name_tasks, task_spans  # noqa: E402
from test_phase_segment import PREVIEWS as TEXTS, K  # noqa: E402

PREVIEWS = [None]           # masked previews by id: 0 is none, and `C(text=...)` interns one
TAGSETS = [((), None)]      # tag sets by id, as the reducer interns them: 0 is the empty set
_TAG_IDS = {}
_PREVIEW_IDS = {}


def tagset_id(names, words):
    """The id of the tag set of `names` and `words` in TAGSETS, interned; 0 for the empty set."""
    key = (tuple(names), words)
    if key == ((), None):
        return 0
    i = _TAG_IDS.get(key)
    if i is None:
        i = _TAG_IDS[key] = len(TAGSETS)
        TAGSETS.append(key)
    return i


def preview_id(text):
    """The id of `text` in PREVIEWS, interned; 0 for no text."""
    if not text:
        return 0
    i = _PREVIEW_IDS.get(text)
    if i is None:
        i = _PREVIEW_IDS[text] = len(PREVIEWS)
        PREVIEWS.append(text)
    return i


def C(s, d, cat, names=(), words=None, past=None, h=0, text=None):
    """A call that starts at `s` and runs `d` seconds, with the tag set of `names` (kind and name
    pairs) and `words` (source, line, and ref). `h` is its command's hash, which calls on a timer
    share, and `text` its masked preview."""
    return (s, s + d, cat, h, preview_id(text), 0, None, past, tagset_id(names, words))


def spans(calls, **params):
    return task_spans(prepared(calls, PREVIEWS, PHASE_DEFAULTS), TAGSETS,
                      dict(DEFAULTS, **params), 1.0)


def at(hour, minute=0, day=(2026, 10, 14)):
    """A local time on `day`, a day away from any change of clocks, as is the next: a test that
    counts local days then does not depend on the host's zone."""
    return time.mktime(day + (hour, minute, 0, 0, 0, -1))


def kinds(sp):
    return [x.kind for x in sp]


def shape(sp):
    return [(x.kind, x.lo, x.hi, x.t0, x.t1, x.gap) for x in sp]


class CutTest(unittest.TestCase):
    def test_a_long_gap_with_nothing_waited_on_cuts(self):
        a = [C(60 * i, 5, "edit", [("write", "a.py")]) for i in range(10)]
        b = [C(6000 + 60 * i, 5, "edit", [("write", "b.py")]) for i in range(10)]
        sp = spans(a + b)
        self.assertEqual([x.kind for x in sp], ["task", "task"])
        self.assertAlmostEqual(sp[1].gap[0], 6000 - (540 + 5))

    def test_a_gap_while_the_last_call_runs_does_not_cut(self):
        sp = spans([C(60 * i, 5, "run", [("run", "a.py")]) for i in range(10)]
                   + [C(600, 10869, "wait", [("job", "phaseB")])]
                   + [C(11500 + 60 * i, 5, "run", [("run", "a.py")]) for i in range(10)])
        self.assertEqual(len(sp), 1)

    def test_a_call_the_agent_moved_past_holds_no_gap(self):
        sp = spans([C(0, 50000, "run", [("run", "linktest")])]
                   + [C(60 * i, 5, "edit", [("write", "a.py")]) for i in range(1, 10)]
                   + [C(9000 + 60 * i, 5, "edit", [("write", "b.py")]) for i in range(10)])
        self.assertEqual(len(sp), 2)
        self.assertEqual(sp[1].gap[1], [0])          # the moved-past call is still alive

    def test_a_process_a_finished_call_left_behind_holds_the_gap(self):
        sp = spans([C(60 * i, 5, "run", [("run", "a.py")]) for i in range(9)]
                   + [C(540, 1, "run", [("run", "update.sh")], past=8000)]
                   + [C(7000 + 60 * i, 5, "run", [("run", "a.py")]) for i in range(10)])
        self.assertEqual(len(sp), 1)

    def test_a_new_object_that_takes_over_cuts_where_it_first_occurs(self):
        a = [C(60 * i, 5, "edit", [("write", "a.py")]) for i in range(30)]
        b = [C(1800 + 60 * i, 5, "run", [("dir", "novel_events/controls")]) for i in range(30)]
        sp = spans(a + b)
        self.assertEqual([round(x.t0) for x in sp], [0, 1800])

    def test_a_closing_commit_cuts_only_when_the_names_change(self):
        a = [C(60 * i, 5, "edit", [("write", "x.py")]) for i in range(15)]
        commit = [C(900, 2, "git", [], ("commit", "Fix x", ""))]
        same = [C(960 + 60 * i, 5, "edit", [("write", "x.py")]) for i in range(15)]
        other = [C(960 + 60 * i, 5, "edit", [("write", "y_old.py")]) for i in range(15)]
        self.assertEqual(len(spans(a + commit + same)), 1)
        self.assertEqual(len(spans(a + commit + other)), 2)

    def test_a_lone_call_hours_from_other_work_is_set_apart(self):
        sp = spans([C(60 * i, 5, "edit", [("write", "a.py")]) for i in range(20)]
                   + [C(20000, 3, "explore", [("read", "harness/x.py")])]
                   + [C(40000 + 60 * i, 5, "edit", [("write", "a.py")]) for i in range(20)])
        self.assertEqual([x.kind for x in sp], ["task", "lone", "task"])

    def test_short_tasks_merge_unless_they_hold_words(self):
        # Three pieces of one stretch with no gap, each cut where its new object takes over. The
        # middle one, y, is 5 calls over 3 minutes. It merges into the neighbour whose names it
        # shares more of, whichever side that is, and stays a task when it holds the agent's words.
        def work(x_names, z_names, words=None):
            x = [C(60 * i, 5, "edit", x_names) for i in range(20)]                  # 0 .. 1145
            y = [C(1200 + 45 * i, 5, "edit", [("write", "y.py"), ("write", "shared.py")])
                 for i in range(4)] + [C(1350, 2, "git", [], words)]                # 1200 .. 1352
            z = [C(1380 + 60 * i, 5, "edit", z_names) for i in range(20)]           # 1380 ..
            return x + y + z
        shared, x, z = [("write", "shared.py")], [("write", "x.py")], [("write", "z.py")]
        with_z = spans(work(x, z + shared))
        self.assertEqual([round(s.t0) for s in with_z], [0, 1200])
        with_x = spans(work(x + shared, z))
        self.assertEqual([round(s.t0) for s in with_x], [0, 1380])
        holding = spans(work(x, z + shared, ("commit", "Fix z", "")))
        self.assertEqual([round(s.t0) for s in holding], [0, 1200, 1380])

    def test_the_cap_merges_the_most_alike_neighbours(self):
        # 14 pieces of 10 calls in one stretch and one local day, each cut where its names first
        # occur. Pairs of neighbours share some of their names, and the pairs that share the most
        # come last, so merging the earliest pairs, or any four, gives another answer. Each piece
        # of a pair names `u` of its own and the two share `s`: alike by s / (s + 2u).
        pairs = [(0, 3, 1), (2, 2, 1), (4, 1, 1), (6, 1, 2), (8, 1, 3), (10, 1, 4)]
        names = {k: [("write", "own%d_0.py" % k)] for k in range(14)}
        for first, u, s in pairs:
            for k in (first, first + 1):
                names[k] = [("write", "own%d_%d.py" % (k, j)) for j in range(u)] \
                    + [("write", "shared%d_%d.py" % (first, j)) for j in range(s)]
        base = at(6)
        calls = [C(base + 1000 * k + 100 * j, 5, "edit", names[k])
                 for k in range(14) for j in range(10)]
        sp = spans(calls)
        self.assertEqual(kinds(sp), ["task"] * DEFAULTS["cap"])
        # the pairs alike by 2/3, 3/5, 1/2, and 1/3 are merged; those by 1/5 and 1/7 are not
        self.assertEqual([round(x.t0 - base) for x in sp],
                         [0, 1000, 2000, 3000, 4000, 6000, 8000, 10000, 12000, 13000])

    def test_open_and_unnamed_calls_pass_through(self):
        open_call = (100.0, 900.0, "wait", 0, 0, OPEN, None, None, 0, ("sleep",))
        sp = spans([C(60 * i, 5, "unknown") for i in range(5)] + [open_call])
        self.assertEqual([x.kind for x in sp], ["task"])


class GapTest(unittest.TestCase):
    def test_the_gap_is_measured_from_where_the_agent_stopped_waiting(self):
        first = [C(60 * i, 5, "edit", [("write", "a.py")]) for i in range(10)]      # ends at 545

        def later(t):
            return [C(t + 60 * i, 5, "edit", [("write", "a.py")]) for i in range(10)]
        self.assertEqual(len(spans(first + later(545 + 3599.5))), 1)
        sp = spans(first + later(545 + 3600))
        self.assertEqual(len(sp), 2)                        # a gap of gap_s is long enough
        self.assertEqual(sp[1].gap, (3600.0, []))
        self.assertIsNone(sp[0].gap)

    def test_a_long_call_in_a_parallel_batch_holds_the_gap(self):
        def session(second):
            return spans([C(60 * i, 5, "edit", [("write", "a.py")]) for i in range(10)]
                         + [C(600.0, 5000, "run", [("run", "b.py")]),
                            C(second, 5, "run", [("run", "b.py")])]
                         + [C(7000 + 60 * i, 5, "edit", [("write", "a.py")]) for i in range(10)])
        # started within a second of each other they are one step, and the long call holds the gap
        self.assertEqual(len(session(600.5)), 1)
        # two seconds on, the second call's step began over the long one, which is moved past
        sp = session(602.0)
        self.assertEqual(len(sp), 2)
        self.assertEqual(sp[1].gap[1], [10])

    def test_the_gap_lists_the_calls_still_running_earliest_first_three_at_most(self):
        sp = spans([C(10 * i, 100000, "run", [("run", "long%d.py" % i)]) for i in range(5)]
                   + [C(100 + 60 * i, 5, "edit", [("write", "a.py")]) for i in range(10)]
                   + [C(9000 + 60 * i, 5, "edit", [("write", "b.py")]) for i in range(10)])
        self.assertEqual(len(sp), 2)
        self.assertEqual(sp[1].gap[1], [0, 1, 2])

    def test_a_call_that_ends_inside_the_gap_is_still_listed(self):
        def session(first_call_seconds):
            return spans([C(0, first_call_seconds, "run", [("run", "a.py")])]
                         + [C(60 * i, 5, "edit", [("write", "a.py")]) for i in range(1, 10)]
                         + [C(9000 + 60 * i, 5, "edit", [("write", "b.py")]) for i in range(10)])
        self.assertEqual(session(1000)[1].gap[1], [0])      # running as the gap begins, at 545
        self.assertEqual(session(300)[1].gap[1], [])        # over before it begins

    def test_a_process_left_behind_holds_every_later_gap_until_it_ends(self):
        calls = [C(60 * i, 5, "edit", [("write", "a.py")]) for i in range(9)] \
            + [C(540, 1, "run", [("run", "update.sh")], past=20000)] \
            + [C(5000 + 60 * i, 5, "edit", [("write", "a.py")]) for i in range(10)] \
            + [C(30000 + 60 * i, 5, "edit", [("write", "b.py")]) for i in range(10)]
        sp = spans(calls)
        self.assertEqual(len(sp), 2)                        # 5000 s to 30000 s, held until 20000
        self.assertAlmostEqual(sp[1].gap[0], 30000 - 20000)
        self.assertEqual(sp[0].t1, 20000)                   # the lane lasts as long as the process


class LoneTest(unittest.TestCase):
    def test_a_sessions_only_stretch_is_a_task_however_small(self):
        self.assertEqual(kinds(spans([C(0, 1, "explore", [("read", "a.py")])])), ["task"])
        self.assertEqual(kinds(spans([C(60 * i, 5, "edit") for i in range(3)])), ["task"])

    def test_a_stretch_at_the_start_or_the_end_is_lone_when_it_is_small(self):
        few = [C(60 * i, 5, "explore", [("read", "a.py")]) for i in range(3)]
        many = [C(20000 + 60 * i, 5, "edit", [("write", "b.py")]) for i in range(20)]
        self.assertEqual(kinds(spans(few + many)), ["lone", "task"])
        many = [C(60 * i, 5, "edit", [("write", "b.py")]) for i in range(20)]
        few = [C(20000 + 60 * i, 5, "explore", [("read", "a.py")]) for i in range(3)]
        self.assertEqual(kinds(spans(many + few)), ["task", "lone"])

    def test_a_stretch_with_too_many_calls_or_too_long_is_a_task(self):
        many = [C(60 * i, 5, "edit", [("write", "b.py")]) for i in range(20)]

        def after(n, every):
            return spans(many + [C(20000 + every * i, 1, "explore", [("read", "a.py")])
                                 for i in range(n)])
        self.assertEqual(kinds(after(8, 10)), ["task", "lone"])         # 8 calls, the most
        self.assertEqual(kinds(after(9, 10)), ["task", "task"])         # 9 calls
        # lone_s is 7200, not 600, since the tuning on the seven hand-labeled sessions (2026-11-03)
        self.assertEqual(kinds(after(8, 1028)), ["task", "lone"])       # 8 calls over 7196 s
        self.assertEqual(kinds(after(8, 1029)), ["task", "task"])       # over 7203 s: past lone_s

    def test_stretches_that_would_all_be_lone_are_all_tasks(self):
        # A lone stretch is hours from other work. Where every stretch is that small there is no
        # other work, so each is a task, and every session with calls has one.
        sp = spans([C(60 * i, 5, "explore", [("read", "a.py")]) for i in range(3)]
                   + [C(20000, 3, "edit", [("write", "b.py")])]
                   + [C(40000 + 60 * i, 5, "run", [("run", "c.py")]) for i in range(2)])
        self.assertEqual(kinds(sp), ["task"] * 3)
        self.assertEqual([(x.lo, x.hi) for x in sp], [(0, 3), (3, 4), (4, 6)])
        self.assertEqual([x.gap is not None for x in sp], [False, True, True])
        # a gap at every boundary: five calls, five tasks
        sp = spans([C(7200 * i, 5, "explore", [("read", "a.py")]) for i in range(5)])
        self.assertEqual(kinds(sp), ["task"] * 5)
        self.assertEqual([(x.lo, x.hi) for x in sp], [(i, i + 1) for i in range(5)])
        self.assertEqual([x.gap[0] if x.gap else None for x in sp], [None] + [7200.0 - 5] * 4)

    def test_a_small_stretch_beside_a_large_one_is_still_lone(self):
        def large(t):
            return [C(t + 60 * i, 5, "edit", [("write", "a.py")]) for i in range(20)]

        def small(t):
            return [C(t, 3, "explore", [("read", "b.py")])]
        self.assertEqual(kinds(spans(large(0) + small(20000))), ["task", "lone"])
        self.assertEqual(kinds(spans(small(0) + large(20000))), ["lone", "task"])

    def test_a_lone_stretch_is_not_counted_toward_the_cap(self):
        a = [C(at(5) + 60 * i, 5, "edit", [("write", "a.py")]) for i in range(12)]
        a2 = [C(at(5) + 720 + 60 * i, 5, "run", [("dir", "newobj")]) for i in range(20)]
        lone = [C(at(9), 3, "explore", [("read", "x.py")])]
        b = [C(at(13) + 60 * i, 5, "edit", [("write", "b.py")]) for i in range(20)]
        # three tasks and a lone stretch: at the cap of 3 nothing merges, as the lone one is no task
        self.assertEqual(kinds(spans(a + a2 + lone + b, cap=3)), ["task", "task", "lone", "task"])
        self.assertEqual(kinds(spans(a + a2 + lone + b, cap=2)), ["task", "lone", "task"])


class NewObjectTest(unittest.TestCase):
    def two_objects(self, name, count, spacing=60, kind="dir"):
        """30 calls on a.py, then `count` that name `name`, which first occurs at 1800. Nothing
        merges, so each cut shows."""
        a = [C(60 * i, 5, "edit", [("write", "a.py")]) for i in range(30)]
        b = [C(1800 + spacing * i, 5, "run", [(kind, name)]) for i in range(count)]
        return spans(a + b, min_s=0, min_calls=0)

    def test_the_name_must_recur_in_enough_later_steps(self):
        # new_steps is 5 since the tuning on the seven hand-labeled sessions (2026-11-03); it was 3
        self.assertEqual(len(self.two_objects("novel", 6)), 2)       # first, then 5 more
        self.assertEqual(len(self.two_objects("novel", 5)), 1)       # first, then only 4

    def test_the_name_must_recur_soon_enough(self):
        # the 5th later step (new_steps, as tuned on 2026-11-03) must be within new_window_s
        self.assertEqual(len(self.two_objects("novel", 6, spacing=360)), 2)    # 5th more, at +1800
        self.assertEqual(len(self.two_objects("novel", 6, spacing=361)), 1)    # at +1805

    def test_a_search_pattern_or_a_directory_changed_into_is_not_an_object(self):
        self.assertEqual(len(self.two_objects("federat", 30, kind="grep")), 1)
        self.assertEqual(len(self.two_objects("proj", 30, kind="cd")), 1)
        self.assertEqual(len(self.two_objects("marinQC51", 30, kind="word")), 2)

    def test_a_name_the_session_held_before_is_not_new(self):
        a = [C(60 * i, 5, "edit", [("write", "a.py")]) for i in range(10)]
        b = [C(600 + 60 * i, 5, "edit", [("write", "b.py")]) for i in range(10)]
        back = [C(1200 + 60 * i, 5, "edit", [("write", "a.py")]) for i in range(10)]
        # b is new at 600, and a is not new when it comes back at 1200
        self.assertEqual([round(x.t0) for x in spans(a + b + back, min_s=0, min_calls=0)],
                         [0, 600])

    def test_a_name_from_an_earlier_stretch_is_not_new(self):
        a = [C(60 * i, 5, "edit", [("write", "a.py")]) for i in range(10)]
        mid = [C(20000 + 60 * i, 5, "edit", [("write", "b.py")]) for i in range(10)]

        def last(name):
            first = [C(40000 + 60 * i, 5, "edit", [("write", "z.py")]) for i in range(5)]
            then = [C(40300 + 60 * i, 5, "edit", [("write", "z.py"), ("write", name)])
                    for i in range(10)]
            return spans(a + mid + first + then, min_s=0, min_calls=0)
        self.assertEqual(len(last("a.py")), 3)          # held since the first stretch: no cut
        self.assertEqual(len(last("c.py")), 4)          # new at 40300: cut

    def test_a_name_is_a_kind_and_a_name(self):
        read = [C(60 * i, 5, "explore", [("read", "x.py")]) for i in range(10)]
        written = [C(600 + 60 * i, 5, "edit", [("write", "x.py")]) for i in range(10)]
        read_on = [C(600 + 60 * i, 5, "explore", [("read", "x.py")]) for i in range(10)]
        # a file read and then written is two names, and the second is new where it begins
        self.assertEqual(len(spans(read + written, min_s=0, min_calls=0)), 2)
        self.assertEqual(len(spans(read + read_on, min_s=0, min_calls=0)), 1)

    def test_a_name_a_lone_stretch_held_is_not_new_either(self):
        lone = [C(60 * i, 5, "edit", [("write", "z.py")]) for i in range(3)]

        def task(name):
            first = [C(20000 + 60 * i, 5, "edit", [("write", "q.py")]) for i in range(5)]
            then = [C(20300 + 60 * i, 5, "edit", [("write", "q.py"), ("write", name)])
                    for i in range(10)]
            return spans(lone + first + then, min_s=0, min_calls=0)
        self.assertEqual(kinds(task("z.py")), ["lone", "task"])           # held, so no cut
        self.assertEqual(kinds(task("c.py")), ["lone", "task", "task"])   # new at 20300: cut

    def test_a_burst_of_one_command_counts_once(self):
        # Six runs at irregular times, so on no timer: as one command (one `h`) within 95 s they
        # hold the name once, and it recurs in no later step; as six commands they cut.
        a = [C(60 * i, 5, "edit", [("write", "a.py")]) for i in range(30)]

        def runs(times, h_of):
            return [C(t, 2, "explore", [("read", "novel.py")], h=h_of(i))
                    for i, t in enumerate(times)]
        burst = (1800, 1803, 1830, 1834, 1890, 1895)
        self.assertEqual(len(spans(a + runs(burst, lambda i: 55), min_s=0, min_calls=0)), 1)
        self.assertEqual(len(spans(a + runs(burst, lambda i: 100 + i), min_s=0, min_calls=0)), 2)
        # each run past SAME_COMMAND_S (180 s) after the last that counted counts again
        apart = (1800, 1981, 2381, 2571, 3171, 3356)
        self.assertEqual(len(spans(a + runs(apart, lambda i: 55), min_s=0, min_calls=0)), 2)

    def test_a_bursts_window_runs_from_the_call_that_counted(self):
        # One command every 60 s and 140 s by turns. From the call that counted, every second call
        # is 200 s on and counts again, six times in 1,000 s, and the name cuts. Were the window
        # run from the call just before, each call would be in it, and the name would count once.
        a = [C(60 * i, 5, "edit", [("write", "a.py")]) for i in range(30)]
        runs = [C(1800 + 200 * (i // 2) + 60 * (i % 2), 2, "explore", [("read", "novel.py")],
                  h=55) for i in range(12)]
        self.assertEqual(len(spans(a + runs, min_s=0, min_calls=0)), 2)

    def test_a_command_run_again_exactly_180_s_after_it_counted_adds_no_names(self):
        # One command every 60 s and 120 s by turns: every third call is exactly SAME_COMMAND_S
        # after the one that counted, and adds none. The name counts at 0, 240, 540, 780, and
        # 1,080 s, five times, too few to cut. Counted at 180 s too, it would count seven times.
        a = [C(60 * i, 5, "edit", [("write", "a.py")]) for i in range(30)]
        runs = [C(1800 + 180 * (i // 2) + 60 * (i % 2), 2, "explore", [("read", "novel.py")],
                  h=55) for i in range(13)]
        self.assertEqual(len(spans(a + runs, min_s=0, min_calls=0)), 1)

    def test_a_poll_adds_its_names_once_after_its_first_call_in_the_stretch(self):
        a = [C(60 * i, 5, "edit", [("write", "a.py")]) for i in range(30)]

        def polls(on_a_timer):          # 12 calls at a steady pace, each naming a job
            return [C(1800 + 60 * i, 5, "slurm", [("job", "pr8")], h=77 if on_a_timer else 100 + i)
                    for i in range(12)]
        self.assertEqual(len(spans(a + polls(False))), 2)           # twelve commands: a new object
        self.assertEqual(len(spans(a + polls(True))), 1)            # one on a timer: counted once


class ClosingTest(unittest.TestCase):
    def test_the_cut_goes_after_the_whole_git_burst(self):
        a = [C(60 * i, 5, "edit", [("write", "x.py")]) for i in range(15)]
        burst = [C(900, 2, "git", [], ("commit", "Fix x", "")), C(950, 2, "git", []),
                 C(1050, 2, "git", [], ("pr", "Fix x", "#9"))]
        other = [C(1100 + 60 * i, 5, "edit", [("write", "y_old.py")]) for i in range(15)]
        sp = spans(a + burst + other, new_steps=99)          # no new-object cut: the burst alone
        self.assertEqual([round(x.t0) for x in sp], [0, 1100])
        self.assertEqual((sp[0].lo, sp[0].hi), (0, 18))

    def test_a_log_entry_closes_nothing(self):
        a = [C(60 * i, 5, "edit", [("write", "x.py")]) for i in range(15)]
        log = [C(900, 2, "git", [], ("log", "Started y", ""))]
        other = [C(960 + 60 * i, 5, "edit", [("write", "z_old.py")]) for i in range(15)]
        self.assertEqual(len(spans(a + log + other, new_steps=99)), 1)

    def test_a_pause_of_more_than_two_minutes_ends_the_burst(self):
        a = [C(60 * i, 5, "edit", [("write", "x.py")]) for i in range(15)]
        other = [C(1081 + 60 * i, 5, "edit", [("write", "y_old.py")]) for i in range(15)]
        commit = C(900, 2, "git", [], ("commit", "Fix x", ""))
        long_pause = spans(a + [commit, C(1021, 2, "git", [])] + other, new_steps=99)
        self.assertEqual([round(x.t0) for x in long_pause], [0, 1021])  # after the commit alone
        two_minutes = spans(a + [commit, C(1020, 2, "git", [])] + other, new_steps=99)
        self.assertEqual([round(x.t0) for x in two_minutes], [0, 1081])  # 120 s: one burst

    def test_a_commit_that_opens_a_stretch_stands_as_a_piece_of_its_own(self):
        # Nothing comes before it, so the names before and after share none, and it cuts. It closes
        # work from before the gap, and joining it to the work after would label that work with a
        # stale message. With new-object cuts off, only the closing rule can make this cut.
        commit = [C(0, 2, "git", [], ("commit", "Wip", ""))]
        edits = [C(60 * i, 5, "edit", [("write", "a.py")]) for i in range(1, 30)]
        for sp in (spans(commit + edits, new_steps=99), spans(commit + edits)):
            self.assertEqual([(x.kind, x.lo, x.hi) for x in sp],
                             [("task", 0, 1), ("task", 1, 30)])
        # after a gap too: the commit opens the second stretch, and the first is left whole
        before = [C(60 * i, 5, "edit", [("write", "x.py")]) for i in range(20)]
        later = [C(20000, 2, "git", [], ("commit", "Wip", ""))] \
            + [C(20060 + 60 * i, 5, "edit", [("write", "a.py")]) for i in range(29)]
        sp = spans(before + later, new_steps=99)
        self.assertEqual([(x.lo, x.hi) for x in sp], [(0, 20), (20, 21), (21, 50)])
        self.assertEqual([x.gap is not None for x in sp], [False, True, False])

    def test_a_commit_among_other_calls_is_no_git_burst(self):
        a = [C(60 * i, 5, "edit", [("write", "x.py")]) for i in range(15)]
        mixed = [C(900, 2, "edit", [], ("commit", "Fix x", ""))]
        other = [C(960 + 60 * i, 5, "edit", [("write", "y_old.py")]) for i in range(15)]
        self.assertEqual(len(spans(a + mixed + other, new_steps=99)), 1)

    def test_no_names_on_either_side_do_not_differ(self):
        a = [C(60 * i, 5, "explore") for i in range(15)]
        commit = [C(900, 2, "git", [], ("commit", "Wip", ""))]
        after = [C(960 + 60 * i, 5, "explore") for i in range(15)]
        self.assertEqual(len(spans(a + commit + after)), 1)

    def test_how_alike_the_names_must_be_is_close_overlap(self):
        a = [C(60 * i, 5, "edit", [("write", "x.py"), ("read", "common.py")]) for i in range(15)]
        commit = [C(900, 2, "git", [], ("commit", "Fix x", ""))]
        after = [C(960 + 60 * i, 5, "edit", [("read", "common.py"), ("write", "y_old.py")])
                 for i in range(15)]
        calls = a + commit + after          # before and after share 1 of the 3 names they hold
        self.assertEqual(len(spans(calls, new_steps=99, close_overlap=0.5)), 2)
        self.assertEqual(len(spans(calls, new_steps=99, close_overlap=0.3)), 1)
        self.assertEqual(len(spans(calls, new_steps=99, close_overlap=1 / 3)), 1)   # not below it

    def series(self, first_s=2, second_at=1100):
        """Work on x, a commit at 900 that runs `first_s`, other work, a second commit at
        `second_at`, and work on z after it."""
        x = [C(60 * i, 5, "edit", [("write", "x.py")]) for i in range(15)]           # to 845
        first = [C(900, first_s, "git", [], ("commit", "Fix x", ""))]
        y = [C(960 + 40 * i, 5, "edit", [("write", "y_old.py")]) for i in range(3)]  # to 1045
        second = [C(second_at, 2, "git", [], ("commit", "Fix y", ""))]
        z = [C(second_at + 60 + 60 * i, 5, "edit", [("write", "z_old.py")]) for i in range(15)]
        return x + first + y + second + z

    def test_a_series_of_commits_closes_once_after_the_last(self):
        # the second commit starts 198 s after the first ends: one piece of work's commits, so the
        # cut goes after the second alone, and the last commit names the task
        calls = self.series()
        self.assertEqual([round(s.t0) for s in spans(calls, new_steps=99)], [0, 1160])
        (t, _), _ = named(calls, new_steps=99)
        self.assertEqual((t["label"], t["source"]), ("Fix y", "commit"))
        # 198 s is past a series of 197 s, and then each commit closes on its own
        self.assertEqual([round(s.t0) for s in spans(calls, new_steps=99, close_series_s=197)],
                         [0, 960, 1160])

    def test_a_series_is_measured_from_where_a_burst_ends(self):
        # the first commit runs 100 s and ends at 1000: the second, at 1300, is 300 s after its
        # end and 400 s after its start, and is of the same series
        self.assertEqual([round(s.t0) for s in spans(self.series(100, 1300), new_steps=99)],
                         [0, 1360])
        self.assertEqual([round(s.t0) for s in spans(self.series(100, 1301), new_steps=99)],
                         [0, 960, 1361])

    def test_a_burst_ends_where_the_last_of_its_calls_does(self):
        # A commit at 900 whose hooks run 400 s, and a `git status` beside it at 960 that ends
        # first: the burst ends at 1300, with the commit. The second commit, at 1500, starts 200 s
        # after that, so the two are one series and close once. (Until 2026-11-03 a burst ended
        # with its last step, at 962, and each commit closed on its own.)
        x = [C(60 * i, 5, "edit", [("write", "x.py")]) for i in range(15)]             # to 845
        burst = [C(900, 400, "git", [], ("commit", "Fix x", "")), C(960, 2, "git", [])]
        y = [C(1020 + 60 * i, 5, "edit", [("write", "y_old.py")]) for i in range(3)]   # to 1145
        second = [C(1500, 2, "git", [], ("commit", "Fix y", ""))]
        z = [C(1560 + 60 * i, 5, "edit", [("write", "z_old.py")]) for i in range(15)]
        sp = spans(x + burst + y + second + z, new_steps=99)
        self.assertEqual([round(s.t0) for s in sp], [0, 1560])

    def test_a_chain_of_three_bursts_closes_once_after_the_third(self):
        # Each commit starts 248 s after the one before it ends, and the third 498 s after the
        # first ends: a series is a chain, each burst measured from the one before, so the three
        # are one series and it closes once, after the third.
        calls = [C(60 * i, 5, "edit", [("write", "x.py")]) for i in range(15)]     # to 845
        for k in range(3):
            calls.append(C(900 + 250 * k, 2, "git", [], ("commit", "Fix %d" % k, "")))
            if k < 2:
                calls += [C(960 + 250 * k + 60 * i, 5, "edit", [("write", "y_old.py")])
                          for i in range(3)]
        calls += [C(1460 + 60 * i, 5, "edit", [("write", "z_old.py")]) for i in range(15)]
        self.assertEqual([round(s.t0) for s in spans(calls, new_steps=99)], [0, 1460])

    def test_a_commit_in_a_mixed_step_joins_no_series(self):
        # A commit in a step that also edits, 100 s after a git burst, and a second burst 295 s
        # after that step ends but 398 s after the first ends. The mixed step is no burst and is
        # in none, so it chains nothing: the two bursts are two series, and each closes.
        x = [C(60 * i, 5, "edit", [("write", "x.py")]) for i in range(15)]             # to 845
        first = [C(900, 2, "git", [], ("commit", "Fix x", ""))]
        mixed = [C(1000, 2, "git", [], ("commit", "Fix y", "")),
                 C(1000.5, 5, "edit", [("write", "y_old.py")])]                      # one step
        y = [C(1060 + 60 * i, 5, "edit", [("write", "y_old.py")]) for i in range(3)]   # to 1185
        second = [C(1300, 2, "git", [], ("commit", "Fix y again", ""))]
        z = [C(1360 + 60 * i, 5, "edit", [("write", "z_old.py")]) for i in range(15)]
        sp = spans(x + first + mixed + y + second + z, new_steps=99)
        self.assertEqual([round(s.t0) for s in sp], [0, 1000, 1360])


class MergeTest(unittest.TestCase):
    def three(self, y_calls):
        x = [C(60 * i, 5, "edit", [("write", "x.py")]) for i in range(20)]
        z = [C(2800 + 60 * i, 5, "edit", [("write", "z.py"), ("write", "shared.py")])
             for i in range(20)]
        return x + y_calls + z

    def test_a_piece_with_too_few_calls_merges_however_long_it_lasts(self):
        y = [C(1200 + 400 * i, 5, "edit", [("write", "y.py"), ("write", "shared.py")])
             for i in range(4)]                      # 1600 s, but 4 calls
        sp = spans(self.three(y))
        self.assertEqual([round(x.t0) for x in sp], [0, 1200])

    def test_a_piece_with_enough_calls_and_time_stays(self):
        y = [C(1200 + 300 * i, 5, "edit", [("write", "y.py"), ("write", "shared.py")])
             for i in range(5)]                      # 1200 s, and 5 calls
        sp = spans(self.three(y))
        self.assertEqual([round(x.t0) for x in sp], [0, 1200, 2800])

    def test_two_short_pieces_in_a_row_both_merge(self):
        # y1 goes into y2, which it shares `y.py` with. The two together share `shared.py` with z
        # and nothing with x, so they go into z.
        x = [C(60 * i, 5, "edit", [("write", "x.py")]) for i in range(20)]
        y1 = [C(1200 + 20 * i, 5, "edit", [("write", "y1.py"), ("write", "y.py")])
              for i in range(5)]
        y2 = [C(1300 + 20 * i, 5, "edit", [("write", "y2.py"), ("write", "y.py"),
                                           ("write", "shared.py")]) for i in range(5)]
        z = [C(1400 + 60 * i, 5, "edit", [("write", "z.py"), ("write", "shared.py")])
             for i in range(20)]
        sp = spans(x + y1 + y2 + z)
        self.assertEqual([round(s.t0) for s in sp], [0, 1200])
        self.assertEqual([(s.lo, s.hi) for s in sp], [(0, 20), (20, 50)])

    def test_a_tie_in_how_alike_goes_to_the_earlier_neighbour(self):
        x = [C(60 * i, 5, "edit", [("write", "x.py")]) for i in range(20)]
        y = [C(1200 + 20 * i, 5, "edit", [("write", "y.py")]) for i in range(5)]
        z = [C(1300 + 60 * i, 5, "edit", [("write", "z.py")]) for i in range(20)]
        sp = spans(x + y + z)                     # y shares nothing with either
        self.assertEqual([round(s.t0) for s in sp], [0, 1300])

    def test_a_short_stretch_between_gaps_stays_a_task(self):
        a = [C(60 * i, 5, "edit", [("write", "a.py")]) for i in range(20)]
        short = [C(20000 + 10 * i, 1, "edit", [("write", "s.py")]) for i in range(10)]
        b = [C(40000 + 60 * i, 5, "edit", [("write", "b.py")]) for i in range(20)]
        self.assertEqual(kinds(spans(a + short + b)), ["task", "task", "task"])


class CapTest(unittest.TestCase):
    def pieces(self, base, n=14):
        """`n` pieces of 10 calls in one stretch, 1,000 s each, each naming an object of its own."""
        return [C(base + 1000 * k + 100 * j, 5, "edit", [("write", "own%d.py" % k)])
                for k in range(n) for j in range(10)]

    def test_each_local_day_holds_cap_tasks(self):
        self.assertEqual(len(spans(self.pieces(at(6)))), DEFAULTS["cap"])
        self.assertEqual(len(spans(self.pieces(at(22)))), 14)       # across midnight: 8 and 6
        self.assertEqual(len(spans(self.pieces(at(6)), cap=7)), 7)

    def test_a_day_lends_its_budget_to_no_other(self):
        # One piece at 23:48, then 14 from 00:05. Each day's budget is its own: the first day's
        # unused nine lend the second nothing, so 1 and 14 make 1 and 10, not 15 under 20.
        late = [C(at(23, 48) + 20 + 90 * j, 5, "edit", [("write", "late.py")]) for j in range(10)]
        sp = spans(late + self.pieces(at(23, 48) + 1020))
        days = [time.localtime(x.t0)[2] for x in sp]
        self.assertEqual((days.count(14), days.count(15)), (1, DEFAULTS["cap"]))

    def test_on_a_tie_a_pair_of_the_day_goes_before_one_across_midnight(self):
        # The same calls: one piece at 23:48, then 14 from 00:05, and no two share a name, so
        # every pair is at 0. The second day's own pairs go first: its first five pieces merge,
        # and the 23:48 piece stays a task of its own, so the day's boundary holds. (Before this
        # rule the tie went to the earliest pair, across midnight, and the 23:48 piece took the
        # second day's first four.)
        late = [C(at(23, 48) + 20 + 90 * j, 5, "edit", [("write", "late.py")]) for j in range(10)]
        sp = spans(late + self.pieces(at(23, 48) + 1020))
        self.assertEqual([(x.lo, x.hi) for x in sp[:2]], [(0, 10), (10, 60)])
        self.assertEqual(len(sp), 1 + DEFAULTS["cap"])

    def test_a_day_that_holds_only_lone_calls_lends_no_budget(self):
        work = self.pieces(at(6))                                    # 14 pieces on one local day
        lone = [C(at(6) + 86400, 2, "explore", [("read", "x.py")])]  # one call, on the next day
        sp = spans(work + lone)
        # the lone call's day holds no piece of a task, and gives the first day no more room
        self.assertEqual(kinds(sp), ["task"] * DEFAULTS["cap"] + ["lone"])

    def across_midnight(self, a_day_earlier=False, day=(2026, 10, 14)):
        """A piece at 23:45 on `day` that shares two names with the first piece after midnight,
        and 11 pieces on the second day, one over the cap; the second day's own most alike pair,
        the 4th and 5th, shares one name. With `a_day_earlier`, the first piece starts a day
        earlier still and runs through the day between, a call every 30 minutes, so it is in the
        same stretch and begins two days before the second."""
        names = [("write", "a.py"), ("write", "s1.py"), ("write", "s2.py")]
        before = [C(at(23, 45, day) + 90 * j, 5, "edit", names) for j in range(10)]
        if a_day_earlier:
            before = [C(at(23, 45, day) - 86400 + 1800 * k, 5, "edit", names)
                      for k in range(48)] + before
        base = at(23, 45, day) + 1000                                # 00:01:40, the next day
        after = []
        for k in range(11):
            names = [("write", "own%d.py" % k)]
            if k == 0:
                names += [("write", "s1.py"), ("write", "s2.py")]
            if k in (3, 4):
                names += [("write", "s34.py")]
            after += [C(base + 1000 * k + 90 * j, 5, "edit", names) for j in range(10)]
        return base, spans(before + after)

    def test_a_task_across_midnight_on_an_over_cap_day_stays_whole(self):
        # The pair across midnight is the most alike, at 1/2, and its later piece is of the day
        # over the cap: it merges, and keeps the earlier's start, so the task runs on from 23:45
        # past midnight. The second day's own pair, at 1/3, stays apart. (Until 2026-11-03 a pair
        # had to be of one day, and that task was cut at midnight's first piece.)
        base, sp = self.across_midnight()
        starts = [round(x.t0 - base) for x in sp]
        self.assertEqual(len(sp), 11)                       # 1 on the first day, 10 on the second
        self.assertNotIn(0, starts)                         # the piece after midnight merged back
        self.assertIn(4000, starts)                         # the second day's own pair stands
        self.assertLess(sp[0].t0, base - 900)               # the first task begins at 23:45
        self.assertEqual(round(sp[1].t0 - base), 1000)      # and holds the first piece after it

    def test_a_task_across_the_end_of_a_month_stays_whole(self):
        # The same from 23:45 on 2026-12-31, the end of a month and of a year: the day before
        # 2027-01-01 is the date before it, 2026-12-31.
        base, sp = self.across_midnight(day=(2026, 12, 31))
        starts = [round(x.t0 - base) for x in sp]
        self.assertEqual(len(sp), 11)
        self.assertNotIn(0, starts)
        self.assertIn(4000, starts)
        self.assertEqual(time.localtime(sp[1].t0)[:3], (2027, 1, 1))

    def test_a_pair_reaches_back_one_day_only(self):
        # the first piece begins two days before the second day: the pair across midnight is of
        # no day, and the second day's own most alike pair merges instead
        base, sp = self.across_midnight(a_day_earlier=True)
        starts = [round(x.t0 - base) for x in sp]
        self.assertEqual([x.gap for x in sp], [None] * len(sp))     # one stretch
        self.assertIn(0, starts)
        self.assertNotIn(4000, starts)

    def test_nothing_merges_across_a_cutting_gap(self):
        calls = [C(at(5) + 4300 * k + 60 * j, 5, "edit", [("write", "f%d.py" % k)])
                 for k in range(12) for j in range(10)]
        sp = spans(calls)
        self.assertEqual(kinds(sp), ["task"] * 12)          # over the cap, and none can merge
        self.assertEqual([x.gap is not None for x in sp], [False] + [True] * 11)

    def test_the_cap_stops_when_each_stretch_has_one_piece_left(self):
        pieces = self.pieces(at(5), 6)
        after = [C(at(5) + 20000 + 100 * j, 5, "edit", [("write", "g.py")]) for j in range(10)]
        sp = spans(pieces + after, cap=1)
        self.assertEqual(kinds(sp), ["task", "task"])
        self.assertEqual([(x.lo, x.hi) for x in sp], [(0, 60), (60, 70)])


class TimesTest(unittest.TestCase):
    def test_a_span_ends_where_the_next_begins_or_where_the_gap_began(self):
        a = [C(60 * i, 5, "edit", [("write", "a.py")]) for i in range(20)]            # to 1145
        b = [C(1200 + 60 * i, 5, "run", [("dir", "d1")]) for i in range(20)]          # to 2345
        c = [C(9000 + 60 * i, 5, "edit", [("write", "c.py")]) for i in range(10)]     # to 9545
        sp = spans(a + b + c)
        self.assertEqual([(x.t0, x.t1) for x in sp], [(0, 1200), (1200, 2345), (9000, 9545)])
        self.assertEqual([x.gap for x in sp], [None, None, (9000 - 2345, [])])

    def test_the_last_span_ends_with_the_last_process_it_left_behind(self):
        calls = [C(60 * i, 5, "edit", [("write", "a.py")], past=9000 if i == 3 else None)
                 for i in range(10)]
        self.assertEqual(spans(calls)[-1].t1, 9000)

    def test_the_last_span_leaves_out_what_the_agent_moved_past(self):
        hung = C(9000, 100000, "run", [("run", "hung.py")], past=200000)
        calls = [C(60 * i, 5, "edit", [("write", "a.py")]) for i in range(10)] + [hung] \
            + [C(9060 + 60 * i, 5, "edit", [("write", "b.py")]) for i in range(10)]
        sp = spans(calls)
        self.assertEqual([(x.t0, x.t1) for x in sp], [(0, 545), (9000, 9605)])


def random_session(rng):
    """A random session: bursts and pauses, long calls, background processes, open calls."""
    t, out = 1_000_000.0, []
    names = [("write", "a.py"), ("write", "b.py"), ("run", "c.py"), ("dir", "d/e"),
             ("read", "f.py"), ("grep", "pat"), ("cd", "proj"), ("job", "j1"), ("pr", "#12"),
             ("word", "marinQC51")]
    for _ in range(rng.randint(1, 90)):
        t += rng.choice([0.0, 0.2, 5, 30, 90, 700, 2500, 5000, 20000])
        d = rng.choice([0.01, 2, 40, 900, 12000, 90000])
        ns = rng.sample(names, rng.choice([0, 1, 1, 2, 3]))
        words = rng.choice([None] * 6 + [("commit", "Fix", ""), ("log", "Note", "")])
        cat = rng.choice(CATS)
        if rng.random() < 0.05:
            out.append((t, t + d, cat, 0, 0, OPEN, None, None, tagset_id(ns, words), ("x",)))
        else:
            past = t + d + rng.choice([2, 500, 30000]) if rng.random() < 0.1 else None
            out.append(C(t, d, cat, ns, words, past, h=rng.choice([0, 0, 0, 7, 8])))
    rng.shuffle(out)
    return out


class CoverTest(unittest.TestCase):
    def test_the_spans_cover_every_call_once_in_time_order(self):
        rng = random.Random(20261102)
        for _ in range(300):
            calls = random_session(rng)
            before = copy.deepcopy(calls)
            prep = prepared(calls, PREVIEWS, PHASE_DEFAULTS)
            frozen = copy.deepcopy(prep)
            sp = task_spans(prep, TAGSETS, DEFAULTS, 1.0)
            self.assertEqual(calls, before)                 # nothing it was given is changed,
            self.assertEqual(prep, frozen)                  # the prepared calls included
            self.assertEqual(shape(sp), shape(spans(calls)))
            cs = sorted(calls, key=lambda c: c[0])
            self.assertEqual(sp[0].lo, 0)
            self.assertEqual(sp[-1].hi, len(cs))
            for x, y in zip(sp, sp[1:]):
                self.assertEqual(x.hi, y.lo)
                self.assertLess(x.t0, y.t0)
            for x in sp:
                self.assertIsInstance(x, Span)
                self.assertIn(x.kind, ("task", "lone"))
                self.assertLess(x.lo, x.hi)
                self.assertEqual(x.t0, cs[x.lo][0])
                self.assertLessEqual(x.t0, x.t1)
                if x.gap is not None:
                    self.assertGreaterEqual(x.gap[0], DEFAULTS["gap_s"])
                    self.assertLessEqual(len(x.gap[1]), 3)
                    self.assertEqual(x.gap[1], sorted(set(x.gap[1])))
                    self.assertTrue(all(i < x.lo for i in x.gap[1]))
            self.assertIsNone(sp[0].gap)
            self.assertIn("task", kinds(sp))            # every session with calls has a task

    def test_no_calls_no_spans(self):
        self.assertEqual(spans([]), [])

    def test_all_the_calls_of_one_step_are_one_task(self):
        sp = spans([C(5 + 0.05 * i, 1, "run", [("run", "a.py")]) for i in range(20)])
        self.assertEqual([(x.kind, x.lo, x.hi) for x in sp], [("task", 0, 20)])

    def test_every_call_open(self):
        calls = [(100.0 + 50 * i, 900.0 + 50 * i, "wait", 0, 0, OPEN, None, None, 0, ("sleep",))
                 for i in range(4)]
        sp = spans(calls)
        self.assertEqual([(x.kind, x.lo, x.hi, x.t1) for x in sp], [("task", 0, 4, 1050.0)])


class PreparedTest(unittest.TestCase):
    def polling(self):
        """Calls out of order, among them `squeue` every two minutes and a test every three."""
        calls = [K(300, 1, "explore", "ls"), K(10, 5, "edit", "cat > a.py")]
        calls += [K(60 + 120 * i, 2, "slurm", "squeue -u me") for i in range(8)]
        calls += [K(400 + 180 * i, 30, "test", "pytest -q") for i in range(6)]
        return calls

    def test_the_calls_come_sorted_and_the_polling_ones_wait(self):
        calls = self.polling()
        cs, cats, poll = prepared(calls, TEXTS, PHASE_DEFAULTS)
        self.assertEqual([c[0] for c in cs], sorted(c[0] for c in calls))
        self.assertEqual(len(cats), len(cs))
        waiting = [i for i, cat in enumerate(cats) if cat == "wait"]
        self.assertEqual(len(waiting), 14)
        self.assertEqual(sorted(poll), waiting)
        by_group = {}
        for i, g in poll.items():
            by_group.setdefault(g, []).append(cs[i][2])
        self.assertEqual(sorted((len(v), set(v).pop()) for v in by_group.values()),
                         [(6, "test"), (8, "slurm")])
        self.assertEqual(sorted(g[0] for g in by_group), ["command", "slurm"])
        self.assertEqual([c[2] for c in calls][:2], ["explore", "edit"])   # the input is as it was

    def test_segment_gives_the_same_phases_with_or_without_it(self):
        params = dict(PHASE_DEFAULTS, absorb_calls=2, absorb_s=60)
        rng = random.Random(7)
        sessions = [self.polling()]
        for _ in range(40):
            t, calls = 0.0, []
            for _ in range(rng.randint(1, 60)):
                t += rng.choice([0.3, 20, 60, 120, 400])
                calls.append(K(t, rng.choice([1, 30, 900]), rng.choice(CATS),
                               rng.choice(["ls", "squeue -u me", "pytest -q", "python a.py",
                                           "sleep 30"])))
            rng.shuffle(calls)
            sessions.append(calls)
        for calls in sessions:
            plain = segment(calls, PhaseLog(), TEXTS, params)
            prep = prepared(calls, TEXTS, params)
            frozen = copy.deepcopy(prep)
            self.assertEqual(segment(calls, PhaseLog(), TEXTS, params, prepared=prep), plain)
            self.assertEqual(prep, frozen)            # and reading it does not change it


def built(calls, project=None, cut_at=None, user=None, **params):
    """The spans of `calls`, the phase block cut at them, and the tasks and the headline named from
    both, as the reducer builds a row. `params` change the task thresholds."""
    p = dict(DEFAULTS, **params)
    prep = prepared(calls, PREVIEWS, PHASE_DEFAULTS)
    sp = task_spans(prep, TAGSETS, p, 1.0)
    block = segment(calls, PhaseLog(), PREVIEWS, PHASE_DEFAULTS, prepared=prep,
                    cuts=[x.t0 for x in sp[1:]])
    tasks, headline = name_tasks(sp, block, prep, TAGSETS, PREVIEWS, p, project, cut_at,
                                 user=user)
    return sp, block, tasks, headline


def named(calls, project=None, cut_at=None, user=None, **params):
    """The tasks and the headline of `calls`, for a session of `user` when one is given."""
    *_, tasks, headline = built(calls, project, cut_at, user, **params)
    return tasks, headline


def fine(kind, t0, t1, n=0, **fields):
    """A phase as `segment` lists it, for the tests that say what the phases are."""
    phase = {"kind": kind, "t0": t0, "t1": t1, "n": n, "mix": [], "top": [], "guess": None,
             "slices": None, "rounds": None, "waits_on": None, "every_s": None, "times": None,
             "every_cmd": None, "open": False, "seen": None, "ran_past": None, "alive": None}
    return dict(phase, **fields)


def by_hand(calls, phases, project=None, sp=None):
    """The tasks and the headline of `calls`, named from `phases` given by hand, which hold the
    calls in order, `n` of them each. `sp` gives the spans, else they are cut as usual."""
    prep = prepared(calls, PREVIEWS, PHASE_DEFAULTS)
    if sp is None:
        sp = task_spans(prep, TAGSETS, DEFAULTS, 1.0)
    return name_tasks(sp, {"list": phases}, prep, TAGSETS, PREVIEWS, DEFAULTS, project, None)


class NameTest(unittest.TestCase):
    def test_the_agents_words_name_a_task_and_say_where_they_came_from(self):
        (t,), _ = named([C(60 * i, 5, "edit", [("write", "x.py")]) for i in range(10)]
                        + [C(600, 2, "git", [], ("commit", "Fix the loader", ""))])
        self.assertEqual((t["label"], t["source"]), ("Fix the loader", "commit"))
        self.assertEqual((t["ref"], t["names"]), ("", ["x.py"]))       # its names are still listed

    def test_a_pr_title_wins_over_a_commit_and_a_commit_over_a_log_entry(self):
        edits = [C(60 * i, 5, "edit", [("write", "v.py")]) for i in range(6)]
        log = C(400, 2, "edit", [], ("log", "Started", ""))
        commit = C(460, 2, "git", [], ("commit", "Fix it", ""))
        pr = C(520, 2, "git", [], ("pr", "Seek-based frame decoding", "#235"))
        (t,), _ = named(edits + [log, commit, pr])
        self.assertEqual((t["label"], t["source"], t["ref"]),
                         ("Seek-based frame decoding", "pr", "#235"))
        (t,), _ = named(edits + [log, commit])
        self.assertEqual((t["label"], t["source"], t["ref"]), ("Fix it", "commit", ""))
        (t,), _ = named(edits + [log])
        self.assertEqual((t["label"], t["source"], t["ref"]), ("Started", "log", ""))

    def test_the_last_commit_or_pr_of_a_task_names_it_and_the_first_log_entry_does(self):
        work = [C(120 * i, 5, "edit", [("write", "x.py")]) for i in range(12)]      # 0 to 1325

        def two(cat, first, second):        # one among the work, one after it
            return named(work + [C(300.5, 2, cat, [], first), C(1400, 2, cat, [], second)])[0]
        (t,) = two("git", ("commit", "Add it", ""), ("commit", "Fix it", ""))
        self.assertEqual((t["label"], t["source"], t["ref"]), ("Fix it", "commit", ""))
        (t,) = two("git", ("pr", "Add it", ""), ("pr", "Fix it", "#9"))
        self.assertEqual((t["label"], t["source"], t["ref"]), ("Fix it", "pr", "#9"))
        (t,) = two("edit", ("log", "Add it", ""), ("log", "Fix it", ""))
        self.assertEqual((t["label"], t["source"], t["ref"]), ("Add it", "log", ""))

    def test_the_first_log_entry_names_a_task_with_no_commit(self):
        work = [C(60 * i, 5, "edit", [("write", "analysis/run.py")]) for i in range(12)]
        entries = [C(100.5, 2, "edit", [], ("log", "Phase B full run approved", "")),
                   C(500.5, 2, "edit", [], ("log", "Phase B full run done", ""))]
        (t,), _ = named(work + entries)
        self.assertEqual((t["label"], t["source"], t["ref"]),
                         ("Phase B full run approved", "log", ""))

    def test_a_built_label_is_a_verb_and_the_names_most_specific_to_the_task(self):
        env = ("read", "slurm/env.sh")                 # every call of the session names it
        one = []
        for i in range(10):                             # edit a.py, then test, ten times
            one += [C(120 * i, 2, "edit", [env, ("write", "a.py")]),
                    C(120 * i + 20, 40, "test", [env])]
        two = [C(9000 + 60 * i, 5, "run", [env, ("run", "b.py")]) for i in range(10)]
        _, block, tasks, _ = built(one + two)
        self.assertEqual([(t["label"], t["source"], t["names"]) for t in tasks],
                         [("Edit and test a.py", None, ["a.py"]), ("Run b.py", None, ["b.py"])],
                         [p["kind"] for p in block["list"]])

    def test_polls_count_once(self):
        writes = [C(60 * i, 5, "edit", [("write", "data/video_io.py")]) for i in range(6)]

        def polls(on_a_timer):                          # a hundred calls that each name the job
            return [C(1000 + 30 * i, 2, "slurm", [("job", "pr8-mutation")],
                      h=77 if on_a_timer else 100 + i) for i in range(100)]
        (t,), _ = named(writes + polls(True))
        self.assertEqual(t["names"], ["data/video_io.py", "pr8-mutation"])
        self.assertEqual(t["label"], "Run on Slurm data/video_io.py · pr8-mutation")
        # a hundred different commands name the job a hundred times, and it comes first
        (t,), _ = named(writes + polls(False), new_steps=999)
        self.assertEqual(t["names"], ["pr8-mutation", "data/video_io.py"])

    def test_two_objects_advancing_together_are_named_together(self):
        calls = []
        for i in range(10):                             # each round edits one module, then tests
            module = "design/objective.py" if i % 2 == 0 else "atomysm/flow_sampling.py"
            calls += [C(120 * i, 2, "edit", [("write", module)]), C(120 * i + 20, 40, "test")]
        (t,), _ = named(calls)
        self.assertEqual(t["label"],
                         "Edit and test design/objective.py · atomysm/flow_sampling.py")

    def test_a_task_that_names_nothing_falls_back_to_the_project(self):
        calls = [C(60 * i, 1, "explore") for i in range(10)]
        (t,), _ = named(calls, project="atomysm")
        self.assertEqual(t["label"], "Read atomysm")
        (t,), _ = named(calls)
        self.assertEqual((t["label"], t["names"]), ("Read", []))

    def test_a_task_of_commands_not_recorded_says_so(self):
        calls = [C(60 * i, 1, "unknown") for i in range(10)]
        for project in ("atomysm", None):
            (t,), _ = named(calls, project=project)
            self.assertEqual(t["label"], "Commands not recorded")
        # a task that is mostly unrecorded but names something is a run of it
        calls = calls[:8] + [C(480 + 60 * i, 1, "explore", [("run", "a.py")]) for i in range(2)]
        phases = [fine("unknown", 0.0, 480.0, 8), fine("explore", 480.0, 541.0, 2)]
        (t,), _ = by_hand(calls, phases, "atomysm")
        self.assertEqual(t["label"], "Run a.py")

    def test_one_span_is_named_by_its_names_even_when_every_call_holds_them(self):
        # with one span every name is in every span, and the rule for a name in every span would
        # name every one-task session by its project alone
        calls = [C(60 * i, 1, "explore", [("read", "slurm/env.sh")]) for i in range(10)]
        (t,), _ = named(calls, project="atomysm")
        self.assertEqual((t["label"], t["names"]), ("Read slurm/env.sh", ["slurm/env.sh"]))

    def test_a_name_in_every_one_of_several_spans_names_none_of_them(self):
        env = ("read", "slurm/env.sh")
        one = [C(60 * i, 5, "edit", [env, ("write", "a.py")]) for i in range(10)]
        two = [C(9000 + 60 * i, 5, "explore", [env]) for i in range(10)]
        (a, b), _ = named(one + two, project="atomysm")
        self.assertEqual((a["label"], a["names"]), ("Edit a.py", ["a.py"]))
        self.assertEqual((b["label"], b["names"]), ("Read atomysm", []))

    def test_a_lone_stretch_is_labeled_by_the_same_rules(self):
        calls = [C(60 * i, 5, "edit", [("write", "a.py")]) for i in range(20)] \
            + [C(20000, 3, "explore", [("read", "harness/x.py")])] \
            + [C(40000 + 60 * i, 5, "edit", [("write", "a.py")]) for i in range(20)]
        tasks, _ = named(calls)
        self.assertEqual([(t["kind"], t["label"], t["n"]) for t in tasks],
                         [("task", "Edit a.py", 20), ("lone", "Read harness/x.py", 1),
                          ("task", "Edit a.py", 20)])


class ScoreTest(unittest.TestCase):
    def test_the_weights_are_pinned_and_cover_every_kind_of_name(self):
        # cd and grep are 0.1 and 0.05 since the tuning on the seven hand-labeled sessions
        # (2026-11-03); they were 0.3 and 0.2
        self.assertEqual(WEIGHTS, {"run": 1.0, "test": 1.0, "write": 1.0, "job": 1.0, "pr": 1.0,
                                   "worktree": 1.0, "branch": 1.0, "dir": 1.0, "pkg": 1.0,
                                   "scratch": 0.8, "read": 0.6, "word": 0.5, "cd": 0.1,
                                   "grep": 0.05})
        self.assertEqual(set(WEIGHTS), set(KINDS))

    def test_a_folder_changed_into_at_every_step_does_not_outrank_a_script_the_task_runs(self):
        # Most of a session's commands `cd` into one folder, and a score counts steps: twenty
        # steps of the folder weigh less than three of the script the task runs
        here = ("cd", "analysis")
        one = [C(60 * i, 1, "run", [here, ("run", "update.sh")] if i < 3 else [here])
               for i in range(20)]
        two = [C(9000 + 60 * i, 1, "edit", [("write", "b.py")]) for i in range(10)]
        tasks, _ = named(one + two)
        self.assertEqual((tasks[0]["label"], tasks[0]["names"]),
                         ("Run update.sh · analysis", ["update.sh", "analysis"]))

    def test_a_search_term_names_a_task_only_when_little_else_does(self):
        # a pattern searched for in six steps weighs less than a file read in one
        calls = [C(60 * i, 1, "explore", [("grep", "^yo_")]) for i in range(6)] \
            + [C(360, 1, "explore", [("read", "tests/check.sh")])]
        (t,), _ = named(calls)
        self.assertEqual(t["names"], ["tests/check.sh", "^yo_"])

    def test_a_heavier_kind_outranks_a_lighter_one_held_in_as_many_steps(self):
        for heavy, light in [("write", "scratch"), ("scratch", "read"), ("read", "word"),
                             ("word", "cd"), ("cd", "grep")]:
            # the lighter name comes first in every call, so only the weights put it second
            calls = [C(60 * i, 1, "explore", [(light, "light_name"), (heavy, "heavy_name")])
                     for i in range(10)]
            (t,), _ = named(calls)
            self.assertEqual(t["names"], ["heavy_name", "light_name"], (heavy, light))

    def test_a_name_in_fewer_tasks_outranks_a_more_frequent_one_in_more(self):
        a, b, c = ("write", "a.py"), ("write", "b.py"), ("write", "c.py")
        one = [C(60 * i, 5, "edit", [a, b] if i < 8 else [a]) for i in range(10)]
        two = [C(9000 + 60 * i, 5, "edit", [a]) for i in range(10)]
        three = [C(18000 + 60 * i, 5, "edit", [c]) for i in range(10)]
        tasks, _ = named(one + two + three)
        # in the first task a.py is in 10 steps and 2 tasks of 3, and b.py in 8 steps and 1 task
        self.assertEqual([t["names"] for t in tasks], [["b.py", "a.py"], ["a.py"], ["c.py"]])

    def test_a_lone_stretch_counts_among_the_spans_a_name_is_in(self):
        x, y, z = ("write", "x.py"), ("write", "y.py"), ("write", "z.py")
        one = [C(60 * i, 5, "edit", [x, y] if i < 8 else [x]) for i in range(10)]
        lone = [C(20000, 3, "explore", [x])]
        two = [C(40000 + 60 * i, 5, "edit", [z]) for i in range(10)]
        tasks, _ = named(one + lone + two)
        self.assertEqual([t["kind"] for t in tasks], ["task", "lone", "task"])
        # x.py is in 10 steps of the first task and in 2 spans of 3, y.py in 8 steps and 1 span:
        # 10 log 2.5 is 9.2, and 8 log 4 is 11.1. Without the lone stretch x.py would be in 1 span
        # of 2, y.py in 1 of 2, and 10 log 3 would outrank 8 log 3.
        self.assertEqual(tasks[0]["names"], ["y.py", "x.py"])

    def test_a_name_in_every_task_but_not_in_a_lone_stretch_still_names_them(self):
        x, w = ("write", "x.py"), ("write", "w.py")
        one = [C(60 * i, 5, "edit", [x]) for i in range(10)]
        lone = [C(20000, 3, "explore", [w])]
        two = [C(40000 + 60 * i, 5, "edit", [x]) for i in range(10)]
        tasks, _ = named(one + lone + two, project="atomysm")
        self.assertEqual([(t["kind"], t["names"]) for t in tasks],
                         [("task", ["x.py"]), ("lone", ["w.py"]), ("task", ["x.py"])])

    def test_a_name_new_to_the_task_is_ranked_up(self):
        a, b = ("write", "a.py"), ("write", "b.py")
        one = [C(60 * i, 5, "edit", [a]) for i in range(10)]
        two = [C(9000 + 60 * i, 5, "edit", [a, b]) for i in range(10)]
        three = [C(18000 + 60 * i, 5, "edit", [b]) for i in range(10)]
        tasks, _ = named(one + two + three)
        # in the second task both are in 10 steps and in 2 tasks, and only b.py first occurs there
        self.assertEqual([t["names"] for t in tasks], [["a.py"], ["b.py", "a.py"], ["b.py"]])

    def test_a_burst_of_one_command_counts_one_step(self):
        # Ten runs of one read in under a minute, as subagents start, at irregular times so on no
        # timer, and three edits of b.py: the read counts one step, and b.py names the task first
        reads = [C(t, 1, "explore", [("read", "lib/defs.py")], h=55)
                 for t in (0, 2, 5, 9, 14, 20, 27, 35, 44, 54)]
        edits = [C(100 + 60 * i, 5, "edit", [("write", "b.py")]) for i in range(3)]
        (t,), _ = named(reads + edits)
        self.assertEqual(t["names"], ["b.py", "lib/defs.py"])

    def test_a_name_counts_once_for_the_steps_it_is_in_not_the_calls(self):
        a = [C(120 * i + d, 2, "edit", [("write", "a.py")]) for i in range(5) for d in (0, 0.5)]
        b = [C(60 + 120 * i, 2, "edit", [("write", "b.py")]) for i in range(6)]
        (t,), _ = named(a + b)
        # ten calls in five steps, against six calls in six steps
        self.assertEqual(t["names"], ["b.py", "a.py"])

    def test_names_that_tie_come_in_the_order_they_occur(self):
        calls = [C(60 * i, 5, "edit", [("write", "b.py"), ("write", "a.py")]) for i in range(10)]
        (t,), _ = named(calls)
        self.assertEqual(t["names"], ["b.py", "a.py"])

    def test_a_name_shows_once_however_many_kinds_hold_it(self):
        calls = [C(60 * i, 5, "edit", [("write", "x.py"), ("read", "x.py"), ("word", "marinQC51")])
                 for i in range(10)]
        (t,), _ = named(calls)
        self.assertEqual((t["label"], t["names"]),
                         ("Edit x.py · marinQC51", ["x.py", "marinQC51"]))

    def test_a_path_and_its_own_basename_show_once(self):
        # `sbatch analysis/refresh.sbatch` names the script as run and its basename as the job
        script = [("run", "analysis/refresh.sbatch"), ("job", "refresh.sbatch"),
                  ("read", "notes/plan.txt")]
        (t,), _ = named([C(60 * i, 1, "run", script) for i in range(10)])
        self.assertEqual(t["names"], ["analysis/refresh.sbatch", "notes/plan.txt"])
        # and a scratch name is the file's last component without its extension, either way round
        scratch = [("run", "scratchpad/q.py"), ("scratch", "q"), ("read", "data.csv")]
        (t,), _ = named([C(60 * i, 1, "run", scratch) for i in range(10)])
        self.assertEqual(t["names"], ["scratchpad/q.py", "data.csv"])
        first = [C(60 * i, 1, "run", [("scratch", "q"), ("read", "data.csv")]
                   + ([("run", "scratchpad/q.py")] if i < 7 else [])) for i in range(10)]
        (t,), _ = named(first)
        self.assertEqual(t["names"], ["q", "data.csv"])

    def test_two_files_of_one_basename_in_two_folders_both_show(self):
        calls = [C(60 * i, 1, "run", [("run", "head/probe.py"), ("run", "new/probe.py")])
                 for i in range(10)]
        (t,), _ = named(calls)
        self.assertEqual(t["names"], ["head/probe.py", "new/probe.py"])
        # nor is a scratch name one file with a path whose stem it is not
        calls = [C(60 * i, 1, "run", [("run", "scratchpad/q.py"), ("scratch", "q2")])
                 for i in range(10)]
        (t,), _ = named(calls)
        self.assertEqual(t["names"], ["scratchpad/q.py", "q2"])

    def test_a_pr_shows_as_pr_and_every_other_name_as_extracted(self):
        cut = "d" * 79 + "…"            # a name `phase_names` cut: the label does not cut it again
        calls = [C(60 * i, 5, "git", [("pr", "#235"), ("dir", cut)]) for i in range(10)]
        (t,), _ = named(calls)
        self.assertEqual(t["names"], ["PR #235", cut])
        self.assertEqual(t["label"], "Commit PR #235 · " + cut)

    def test_a_task_that_only_searched_is_a_search(self):
        terms = [("grep", "federat"), ("grep", "kempner")]
        (t,), _ = named([C(60 * i, 1, "explore", terms) for i in range(10)])
        self.assertEqual(t["label"], "Search for federat · kempner")
        # a search term beside a file is a name like another
        beside = [C(60 * i, 1, "explore", [("read", "a.py"), ("grep", "federat")])
                  for i in range(10)]
        (t,), _ = named(beside)
        self.assertEqual(t["label"], "Read a.py · federat")
        # what a name in every task cannot name leaves the search term the only one
        env = ("read", "slurm/env.sh")
        one = [C(60 * i, 5, "edit", [env, ("write", "a.py")]) for i in range(10)]
        two = [C(9000 + 60 * i, 5, "explore", [env, ("grep", "federat")]) for i in range(10)]
        (_, t), _ = named(one + two)
        self.assertEqual(t["label"], "Search for federat")

    def test_a_poll_is_folded_over_the_stretch_not_over_the_task(self):
        # A job polled every two minutes, and a module edited between the polls: 20 rounds in two
        # tasks of ten. The first poll names the job, and the rest name nothing.
        calls = []
        for i in range(20):
            calls += [C(120 * i, 5, "slurm", [("job", "pr8")], h=77),
                      C(120 * i + 60, 5, "edit", [("write", "w%d.py" % (i // 10))])]
        phases = [fine("wait", 0.0, 1200.0, 20, waits_on="slurm"),
                  fine("wait", 1200.0, 2405.0, 20, waits_on="slurm")]
        one = Span("task", 0, 20, 0.0, 1200.0)
        two = Span("task", 20, 40, 1200.0, 2405.0)
        (a, b), _ = by_hand(calls, phases, sp=[one, two])
        self.assertEqual((a["names"], b["names"]), (["w0.py", "pr8"], ["w1.py"]))
        # after a cutting gap the second task is a new stretch, which names the job again: it is
        # then in both tasks, and names neither
        two = Span("task", 20, 40, 1200.0, 2405.0, (4000.0, []))
        (a, b), _ = by_hand(calls, phases, sp=[one, two])
        self.assertEqual((a["names"], b["names"]), (["w0.py"], ["w1.py"]))

    def test_a_task_of_only_polls_is_named_by_the_first_poll_of_the_stretch(self):
        polls = [C(120 * i, 5, "slurm", [("job", "pr8")], h=77) for i in range(40)]
        (t,), _ = named(polls, project="atomysm")
        self.assertEqual((t["label"], t["names"]), ("Run on Slurm pr8", ["pr8"]))
        # a second task of the same stretch holds only later polls, which name nothing
        phases = [fine("wait", 0.0, 2400.0, 20, waits_on="slurm"),
                  fine("wait", 2400.0, 4805.0, 20, waits_on="slurm")]
        sp = [Span("task", 0, 20, 0.0, 2400.0), Span("task", 20, 40, 2400.0, 4805.0)]
        (a, b), _ = by_hand(polls, phases, "atomysm", sp)
        self.assertEqual((a["label"], b["label"], b["names"]),
                         ("Run on Slurm pr8", "Run on Slurm atomysm", []))


class VerbTest(unittest.TestCase):
    def label(self, phases, cat="explore", names=(("write", "a.py"),), project=None):
        """The label of the one task of ten calls of `cat`, one a minute, each naming `names`, with
        the phases given by hand."""
        (t,), _ = by_hand([C(60 * i, 5, cat, list(names)) for i in range(10)], phases, project)
        return t["label"]

    def test_there_is_a_verb_for_every_category_of_call(self):
        self.assertEqual(set(VERBS), set(CATS))

    def test_a_task_is_labeled_for_the_kind_that_holds_most_of_its_time(self):
        for kind, fields, label in [("run", {}, "Run a.py"),
                                    ("slurm", {}, "Run on Slurm a.py"),
                                    ("wait", {"waits_on": "slurm"}, "Run on Slurm a.py"),
                                    ("wait", {"waits_on": "command"}, "Wait on a.py"),
                                    ("wait", {"waits_on": "sleep"}, "Wait on a.py"),
                                    ("build", {}, "Build a.py"),
                                    ("explore", {}, "Read a.py"),
                                    ("git", {}, "Commit a.py"),
                                    ("edit", {}, "Edit a.py"),
                                    ("unknown", {}, "Run a.py"),
                                    ("test", {}, "Test a.py"),
                                    ("loop", {}, "Test a.py")]:
            with self.subTest(kind=kind, fields=fields):
                self.assertEqual(self.label([fine(kind, 0.0, 545.0, 10, **fields)]), label)
        # a guess at an edit holds no calls
        guess = [fine("edit_guess", 0.0, 400.0), fine("run", 400.0, 545.0, 10)]
        self.assertEqual(self.label(guess), "Edit a.py")

    def test_a_test_is_edit_and_test_when_the_task_edits(self):
        plain = fine("test", 0.0, 545.0, 10)
        self.assertEqual(self.label([plain]), "Test a.py")
        self.assertEqual(self.label([plain], cat="edit"), "Edit and test a.py")  # an edit call
        guessed = fine("test", 0.0, 545.0, 10, slices=[[10.0, 40.0]])               # a guessed edit
        self.assertEqual(self.label([guessed]), "Edit and test a.py")
        looped = fine("loop", 0.0, 545.0, 10, slices=[[10.0, 40.0]])
        self.assertEqual(self.label([looped]), "Edit and test a.py")
        longer = [fine("edit_guess", 0.0, 100.0), fine("test", 100.0, 545.0, 10)]   # a long guess
        self.assertEqual(self.label(longer), "Edit and test a.py")

    def test_a_build_installs_when_the_task_names_a_package(self):
        build = [fine("build", 0.0, 545.0, 10)]
        self.assertEqual(self.label(build, names=[("pkg", "gridss")]), "Install gridss")
        self.assertEqual(self.label(build), "Build a.py")
        # the package need not be one of the two names the label shows
        names = [("write", "a.py"), ("write", "b.py"), ("pkg", "gridss")]
        self.assertEqual(self.label(build, names=names), "Install a.py · b.py")

    def test_kinds_that_share_a_verb_share_their_time(self):
        run = fine("run", 0.0, 150.0, 4)                         # 150 s of running
        slurm = [fine("slurm", 150.0, 250.0, 3), fine("wait", 250.0, 350.0, 3, waits_on="slurm")]
        self.assertEqual(self.label([run] + slurm), "Run on Slurm a.py")
        looped = [fine("test", 150.0, 250.0, 3), fine("loop", 250.0, 350.0, 3)]
        self.assertEqual(self.label([run] + looped), "Test a.py")
        edited = [fine("edit", 150.0, 250.0, 3), fine("edit_guess", 250.0, 350.0),
                  fine("explore", 350.0, 360.0, 3)]
        self.assertEqual(self.label([run] + edited), "Edit a.py")

    def test_time_is_counted_inside_the_task_and_idle_is_left_out(self):
        # the task is 10 calls over 0 to 545
        idle = [fine("explore", 0.0, 100.0, 5), fine("idle", 100.0, 400.0),
                fine("run", 400.0, 545.0, 5)]
        self.assertEqual(self.label(idle), "Run a.py")          # 145 s against 100 s
        # a call the agent moved past can end a phase long after the task does
        tail = [fine("explore", 0.0, 400.0, 8), fine("run", 400.0, 100000.0, 2)]
        self.assertEqual(self.label(tail), "Read a.py")         # 400 s against 145 s inside it

    def test_a_tie_goes_to_the_kind_with_more_calls_then_to_the_earlier(self):
        self.assertEqual(self.label([fine("run", 0.0, 200.0, 3), fine("explore", 200.0, 400.0, 7)]),
                         "Read a.py")
        self.assertEqual(self.label([fine("run", 0.0, 200.0, 5), fine("explore", 200.0, 400.0, 5)]),
                         "Run a.py")


class HeadlineTest(unittest.TestCase):
    def work(self, t0, *names):
        """The calls of a task: ten, from `t0`, each naming `names`."""
        return [C(t0 + 60 * i, 5, "edit", list(names)) for i in range(10)]

    def test_the_headline_is_the_project_and_the_names_that_recur(self):
        pr229, pr233, video = ("pr", "#229"), ("pr", "#233"), ("write", "data/video_io.py")
        calls = self.work(0, pr229, video) + self.work(9000, pr229, pr233) \
            + self.work(18000, pr233, video)
        tasks, headline = named(calls, project="KempnerForge")
        self.assertEqual(len(tasks), 3)                  # each name is in two of them
        self.assertEqual(headline, "KempnerForge · PRs #229, #233 · data/video_io.py")

    def test_names_rank_by_the_tasks_that_hold_them_then_by_where_they_first_occur(self):
        a, b, c, d, e = (("write", n) for n in ("a.py", "b.py", "c.py", "d.py", "e.py"))
        calls = self.work(0, a, c, e) + self.work(9000, a, b, d) + self.work(18000, a, b, c, d) \
            + self.work(27000, a, b)       # a: 4 tasks, b: 3, c and d: 2 (c first), e: 1
        self.assertEqual(named(calls, project="p")[1], "p · a.py · b.py · c.py")
        self.assertEqual(named(calls, project="p", headline_names=4)[1],
                         "p · a.py · b.py · c.py · d.py")
        self.assertEqual(named(calls, project="p", headline_names=1)[1], "p · a.py")

    def test_a_lone_stretch_does_not_count_toward_the_headline(self):
        x, y = ("write", "x.py"), ("write", "y.py")
        lone = [C(20000, 3, "explore", [x])]
        tasks, headline = named(self.work(0, x) + lone + self.work(40000, y), project="p")
        self.assertEqual([t["kind"] for t in tasks], ["task", "lone", "task"])
        self.assertEqual(headline, "p")                  # x.py is in one task and one lone stretch
        self.assertEqual(named(self.work(0, x) + lone + self.work(40000, x), project="p")[1],
                         "p · x.py")

    def test_the_headline_is_none_with_no_project_and_no_name_that_recurs(self):
        calls = self.work(0, ("write", "a.py")) + self.work(9000, ("write", "b.py"))
        self.assertIsNone(named(calls)[1])
        self.assertEqual(named(calls, project="atomysm")[1], "atomysm")
        self.assertEqual(named([], project="atomysm"), ([], "atomysm"))

    def test_a_single_pr_is_not_plural(self):
        calls = self.work(0, ("pr", "#229")) + self.work(9000, ("pr", "#229"))
        self.assertEqual(named(calls)[1], "PR #229")

    def test_a_search_term_or_a_folder_changed_into_is_no_object_of_the_headline(self):
        held = [("grep", "federat|kempner"), ("cd", "scratchpad"), ("write", "x.py")]
        calls = self.work(0, *held) + self.work(9000, *held)
        self.assertEqual(named(calls, project="p")[1], "p · x.py")

    def test_the_headline_does_not_repeat_the_project_or_a_name(self):
        held = [("write", "x.py"), ("read", "x.py"), ("dir", "KempnerForge")]
        calls = self.work(0, *held) + self.work(9000, *held)
        self.assertEqual(named(calls, project="KempnerForge")[1], "KempnerForge · x.py")

    def test_a_path_and_its_own_basename_are_one_name_of_the_headline(self):
        held = [("write", "analysis/CHAT_LOG.md"), ("read", "CHAT_LOG.md"), ("write", "x.py")]
        calls = self.work(0, *held) + self.work(9000, *held)
        self.assertEqual(named(calls, project="p")[1], "p · analysis/CHAT_LOG.md · x.py")

    def test_the_sessions_user_names_no_task_and_is_no_part_of_the_headline(self):
        # The user of boslogin06's apid 1537051 was in its headline on 2026-10-09 (`cbm · xq404 ·
        # …`), from the `squeue -u xq404` its polls ran.
        user = ("word", "xq404")
        calls = self.work(0, user, ("write", "a.py")) + self.work(9000, user, ("write", "b.py")) \
            + self.work(18000, ("write", "c.py"))
        tasks, headline = named(calls, project="cbm")
        self.assertEqual(([t["label"] for t in tasks], headline),
                         (["Edit a.py · xq404", "Edit b.py · xq404", "Edit c.py"], "cbm · xq404"))
        tasks, headline = named(calls, project="cbm", user="xq404")
        self.assertEqual(([t["label"] for t in tasks], headline),
                         (["Edit a.py", "Edit b.py", "Edit c.py"], "cbm"))
        self.assertEqual(tasks[0]["names"], ["a.py"])
        # a name that only holds the user's is another name
        other = ("word", "xq404_runs")
        calls = self.work(0, other, ("write", "a.py")) + self.work(9000, other, ("write", "b.py")) \
            + self.work(18000, ("write", "c.py"))
        self.assertEqual(named(calls, project="cbm", user="xq404")[1], "cbm · xq404_runs")


class TaskTest(unittest.TestCase):
    def two(self):
        a = [C(60 * i, 5, "edit", [("write", "a.py")]) for i in range(10)]              # to 545
        b = [C(9000 + 60 * i, 5, "edit", [("write", "b.py")]) for i in range(10)]       # to 9545
        return a + b

    def test_a_task_says_what_it_spans(self):
        _, block, tasks, _ = built(self.two())
        self.assertEqual([p["kind"] for p in block["list"]], ["edit", "idle", "edit"])
        self.assertEqual(tasks[0], {
            "kind": "task", "t0": 0.0, "t1": 545.0, "n": 10, "label": "Edit a.py", "source": None,
            "ref": "", "names": ["a.py"], "phases": [0, 2], "open": False, "incomplete": False,
            "gap": None, "sid": None})
        self.assertEqual(tasks[1], {
            "kind": "task", "t0": 9000.0, "t1": 9545.0, "n": 10, "label": "Edit b.py",
            "source": None, "ref": "", "names": ["b.py"], "phases": [2, 3], "open": False,
            "incomplete": False, "gap": {"s": 8455.0, "alive": []}, "sid": None})

    def test_times_are_rounded_as_the_phases_are(self):
        calls = [C(1000000.123456 + 60 * i, 5, "edit", [("write", "a.py")]) for i in range(10)]
        _, block, (t,), _ = built(calls)
        self.assertEqual((t["t0"], t["t1"]), (1000000.123, 1000545.123))
        self.assertEqual((block["list"][0]["t0"], block["list"][-1]["t1"]), (t["t0"], t["t1"]))

    def test_a_gap_carries_its_length_and_what_was_still_running(self):
        hung = C(0, 50000, "run", [("run", "linktest")], text="python linktest.py")
        # an open call is never named by its text, which is its first child's (R65)
        opened = (30.0, 60000.0, "run", 0, preview_id("a child's text"), OPEN, None, None, 0,
                  ("sleep",))
        calls = [hung, opened] + [C(60 * i, 5, "edit", [("write", "a.py")]) for i in range(1, 10)] \
            + [C(9000 + 60 * i, 5, "edit", [("write", "b.py")]) for i in range(10)]
        tasks, _ = named(calls)
        self.assertIsNone(tasks[0]["gap"])
        self.assertEqual(tasks[1]["gap"], {"s": 9000.0 - 545.0, "alive": [
            ["python linktest.py", 0.0, "linktest"], [None, 30.0, "sleep"]]})

    def gap_name(self, names, seen=None):
        """The name a gap gives a call still running through it, which holds `names`: a finished
        call, or with `seen` an open one that saw those programs under it."""
        if seen is None:
            hung = C(0, 50000, "run", names, text="a preview")
        else:
            hung = (0.0, 50000.0, "run", 0, 0, OPEN, None, None, tagset_id(names, None), seen)
        calls = [hung] + [C(60 * i, 5, "edit", [("write", "a.py")]) for i in range(1, 10)] \
            + [C(9000 + 60 * i, 5, "edit", [("write", "b.py")]) for i in range(10)]
        tasks, _ = named(calls)
        (entry,) = tasks[1]["gap"]["alive"]
        return entry[2]

    def test_a_gap_names_a_call_by_what_it_ran_tested_submitted_or_made(self):
        # The hung `linktest` call of rtx6000a on 2026-11-01 made its folder, changed into it, and
        # ran a script there: the script names it, whichever comes first in its command.
        made = [("dir", "scratchpad/linktest"), ("cd", "linktest")]
        self.assertEqual(self.gap_name(made + [("run", "t.sh")]), "t.sh")
        self.assertEqual(self.gap_name(made + [("test", "tests/test_x.py"), ("run", "t.sh")]),
                         "t.sh")
        self.assertEqual(self.gap_name(made + [("job", "j1"), ("test", "tests/test_x.py")]),
                         "tests/test_x.py")
        self.assertEqual(self.gap_name(made + [("job", "j1")]), "j1")
        self.assertEqual(self.gap_name(made), "scratchpad/linktest")
        # of two names of one kind, the first; and a name of no such kind names none
        self.assertEqual(self.gap_name([("run", "a.py"), ("run", "b.py")]), "a.py")
        self.assertIsNone(self.gap_name([("read", "x.py"), ("cd", "linktest"), ("word", "w_1")]))
        self.assertIsNone(self.gap_name([]))

    def test_a_gap_names_an_open_call_by_its_names_else_by_the_programs_seen_under_it(self):
        self.assertEqual(self.gap_name([("run", "watch.sh")], ("sleep",)), "watch.sh")
        self.assertEqual(self.gap_name([], ("sleep",)), "sleep")
        self.assertEqual(self.gap_name([("read", "x.log")], ("pgrep", "sleep")), "pgrep and sleep")
        self.assertEqual(self.gap_name([], ("squeue", "grep", "sleep")), "squeue, grep, and sleep")
        self.assertEqual(self.gap_name([], ("tail  -f",)), "tail -f")       # on one line
        self.assertIsNone(self.gap_name([], ()))

    def test_a_task_is_open_when_it_holds_a_call_still_running(self):
        opened = (9600.0, 20000.0, "wait", 0, 0, OPEN, None, None,
                  tagset_id([("job", "x")], None), ("sleep",))
        tasks, _ = named(self.two() + [opened])
        self.assertEqual([t["open"] for t in tasks], [False, True])

    def test_a_task_is_incomplete_when_it_ends_after_the_cut(self):
        # and the last one is whenever there is a cut, however early it ends
        for cut_at, expected in [(None, [False, False]), (100.0, [True, True]),
                                 (545.0, [False, True]), (1000.0, [False, True]),
                                 (9545.0, [False, True]), (20000.0, [False, True])]:
            with self.subTest(cut_at=cut_at):
                tasks, _ = named(self.two(), cut_at=cut_at)
                self.assertEqual([t["incomplete"] for t in tasks], expected)

    def test_the_last_span_is_incomplete_when_the_log_cut_calls(self):
        # The calls held are in time order, and each ends before the first call the log dropped
        # starts (`cut_at`). The dropped calls start at or after it, so they may continue the last
        # span though it ends before: it is incomplete, and the spans before it are not, unless
        # they end after `cut_at`. A lone stretch at the end is the last span too.
        a = [C(60 * i, 5, "edit", [("write", "a.py")]) for i in range(10)]              # to 545
        b = [C(9000 + 60 * i, 5, "edit", [("write", "b.py")]) for i in range(10)]       # to 9545
        c = [C(20000 + 60 * i, 5, "edit", [("write", "c.py")]) for i in range(10)]      # to 20545
        for cut_at, expected in [(None, [False, False, False]), (30000.0, [False, False, True]),
                                 (20545.0, [False, False, True]), (9300.0, [False, True, True])]:
            with self.subTest(cut_at=cut_at):
                tasks, _ = named(a + b + c, cut_at=cut_at)
                self.assertEqual([t["incomplete"] for t in tasks], expected)
        lone = [C(20000, 3, "explore", [("read", "harness/x.py")])]
        for cut_at, expected in [(None, False), (30000.0, True)]:
            with self.subTest(cut_at=cut_at, last="lone"):
                tasks, _ = named(a + lone, cut_at=cut_at)
                self.assertEqual([(t["kind"], t["incomplete"]) for t in tasks],
                                 [("task", False), ("lone", expected)])

    def test_each_phase_names_its_task_and_tasks_tile_the_phases(self):
        a = [C(60 * i, 5, "edit", [("write", "a.py")]) for i in range(20)]
        lone = [C(20000, 3, "explore", [("read", "harness/x.py")])]
        b = [C(40000 + 60 * i, 5, "run", [("dir", "d1")]) for i in range(10)]
        c = [C(40700 + 60 * i, 5, "run", [("dir", "d2")]) for i in range(30)]   # a cut with no gap
        sp, block, tasks, _ = built(a + lone + b + c)
        self.assertEqual(kinds(sp), ["task", "lone", "task", "task"])
        owners = [p["task"] for p in block["list"]]
        self.assertEqual(owners, sorted(owners))
        for i, t in enumerate(tasks):
            first, end = t["phases"]
            self.assertEqual([j for j, o in enumerate(owners) if o == i], list(range(first, end)))
        self.assertEqual(tasks[0]["phases"][0], 0)
        self.assertEqual(tasks[-1]["phases"][1], len(block["list"]))
        for x, y in zip(tasks, tasks[1:]):
            self.assertEqual(x["phases"][1], y["phases"][0])
        # an idle gap between two tasks is the earlier one's, though it lies after its end
        idle = [j for j, p in enumerate(block["list"]) if p["kind"] == "idle"]
        self.assertEqual(idle, [1, 3])
        self.assertEqual([owners[j] for j in idle], [0, 1])

    def test_a_phase_that_outlives_the_last_task_is_still_the_last_ones(self):
        # a call the agent moved past, still running after the last task has ended
        reads = [C(60 * i, 5, "explore", [("read", "a.py")]) for i in range(25)]
        hung = C(100, 100000, "explore", [("read", "a.py")])
        runs = [C(1500 + 60 * i, 10, "run", [("run", "go.py")]) for i in range(6)]
        _, block, (t,), _ = built(reads + [hung] + runs)
        self.assertEqual([p["kind"] for p in block["list"]], ["explore", "run"])
        self.assertEqual(block["list"][-1]["t1"], 100100.0)      # until the hung call ends
        self.assertEqual(t["t1"], 1810.0)                        # and the task does not
        self.assertEqual([p["task"] for p in block["list"]], [0, 0])
        self.assertEqual(t["phases"], [0, 2])
        # inside the task the run holds 365 s, and the reading 1,445 s: it is not named for the tail
        self.assertEqual(t["label"], "Read a.py · go.py")


class NameCoverTest(unittest.TestCase):
    KEYS = {"kind", "t0", "t1", "n", "label", "source", "ref", "names", "phases", "open",
            "incomplete", "gap", "sid"}

    def test_every_span_is_named_and_only_the_phases_gain_a_task(self):
        rng = random.Random(20261103)
        for _ in range(300):
            calls = random_session(rng)
            cut_at = rng.choice([None, 1_000_000.0 + rng.choice([100, 5000, 90000])])
            before = copy.deepcopy(calls)
            prep = prepared(calls, PREVIEWS, PHASE_DEFAULTS)
            sp = task_spans(prep, TAGSETS, DEFAULTS, 1.0)
            block = segment(calls, PhaseLog(), PREVIEWS, PHASE_DEFAULTS, prepared=prep,
                            cuts=[x.t0 for x in sp[1:]])
            listed = copy.deepcopy(block["list"])
            frozen, tags, shapes = copy.deepcopy(prep), copy.deepcopy(TAGSETS), shape(sp)
            tasks, headline = name_tasks(sp, block, prep, TAGSETS, PREVIEWS, DEFAULTS, "proj",
                                         cut_at)
            self.assertEqual(calls, before)                 # nothing it was given is changed,
            self.assertEqual(prep, frozen)                  # the prepared calls included,
            self.assertEqual(TAGSETS, tags)
            self.assertEqual(shape(sp), shapes)
            self.assertEqual([{k: v for k, v in p.items() if k != "task"} for p in block["list"]],
                             listed)                        # and a phase gains only its task
            again = name_tasks(sp, {"list": copy.deepcopy(listed)}, prep, TAGSETS, PREVIEWS,
                               DEFAULTS, "proj", cut_at)
            self.assertEqual(again, (tasks, headline))      # the same calls, the same names
            cs = sorted(calls, key=lambda c: c[0])
            owners = [p["task"] for p in block["list"]]
            self.assertEqual(owners, sorted(owners))
            self.assertEqual(len(tasks), len(sp))
            self.assertEqual(sum(t["n"] for t in tasks), len(cs))
            self.assertTrue(headline is None or isinstance(headline, str))
            for i, (x, t) in enumerate(zip(sp, tasks)):
                self.assertEqual(set(t), self.KEYS)
                self.assertEqual((t["kind"], t["n"], t["t0"], t["t1"], t["sid"]),
                                 (x.kind, x.hi - x.lo, round(x.t0, 3), round(x.t1, 3), None))
                first, end = t["phases"]
                self.assertLess(first, end)
                self.assertEqual([j for j, o in enumerate(owners) if o == i],
                                 list(range(first, end)))
                self.assertTrue(isinstance(t["label"], str) and t["label"])
                self.assertIn(t["source"], (None, "pr", "commit", "log"))
                self.assertTrue(all(isinstance(n, str) and n for n in t["names"]))
                self.assertLessEqual(len(t["names"]), 2)
                self.assertEqual(t["open"], any(c[5] & OPEN for c in cs[x.lo:x.hi]))
                self.assertEqual(t["incomplete"], cut_at is not None
                                 and (i == len(sp) - 1 or x.t1 > cut_at))
                self.assertEqual(t["gap"] is None, x.gap is None)
                if x.gap is not None:
                    self.assertEqual(t["gap"]["s"], round(x.gap[0], 3))
                    self.assertEqual(len(t["gap"]["alive"]), len(x.gap[1]))

    def test_no_calls_no_tasks(self):
        self.assertEqual(named([]), ([], None))


if __name__ == "__main__":
    unittest.main()
