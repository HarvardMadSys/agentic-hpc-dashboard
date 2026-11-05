"""The call log and the pending join (reducers/phase_log.py).

Two pure data structures, no I/O. `PhaseLog` is one session's bounded list of tool calls and its
housekeeping counts: the cap counts what it does not hold, a process that outlived its call extends
that call, and two logs add up whether they are two hours of one session (`merge`) or several
collector keys of one agent (`fold`, which must never share a list with the parts that go on
ingesting). `PendingJoin` holds what a process's children did until the process's own record
arrives, since a child's exit is recorded before its parent's: children fold into their parent's
entry, a loop is regular per command rather than over all its children, a pid that was reused does
not take an earlier process's children, and the cap evicts and counts.

    cd dashboard && uv run python -m unittest discover -s tests -p 'test_phase_log.py' -v
"""
import copy
import pickle
import random
import statistics
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rc_dashboard.reducers.phase_log import (CAT, E, FLAGS, H, OPEN, P_CATS, P_CMDS,  # noqa: E402
                                             P_FIRST, P_HELP, P_LAST, P_N, P_SID, P_SINCE,
                                             P_SINCE_N, P_TEXT, P_WROTE, PAST, PID, PREV, S, WROTE,
                                             PendingJoin, PhaseLog, fold_entry)


def C(s, e, cat="explore", pid=None):
    return (s, e, cat, 0, 0, 0, pid, None)


class LogTest(unittest.TestCase):
    def test_the_cap_counts_what_it_does_not_hold(self):
        lg = PhaseLog()
        for i in range(5):
            lg.add(C(10 * i, 10 * i + 1), cap=3)
        self.assertEqual((len(lg.calls), lg.cut, lg.cut_at, lg.n), (3, 2, 30, 5))

    def test_a_process_outliving_its_call_extends_it(self):
        lg = PhaseLog()
        lg.add(C(0, 5, "run", pid=201), cap=10)
        self.assertTrue(lg.ran_past(201, 3600))
        self.assertEqual(lg.calls[0][PAST], 3600)
        self.assertFalse(lg.ran_past(999, 10))

    def test_housekeeping_is_binned_by_minute(self):
        lg = PhaseLog()
        for t in (60, 61, 125):
            lg.add_housekeeping("git", t, bin_cap=10)
        self.assertEqual((lg.hk, lg.hk_bins), ({"git": 3}, {60: 2, 120: 1}))

    def test_merge_and_fold_add_up(self):
        a, b = PhaseLog(), PhaseLog()
        a.add(C(0, 1), 10)
        b.add(C(5, 6), 10)
        b.add_housekeeping("git", 5, 10)
        f = PhaseLog.fold([a, b], 10)
        self.assertEqual((f.n, f.hk), (2, {"git": 1}))
        a.merge(b, 10)
        self.assertEqual(a.n, 2)

    def test_the_layouts_later_code_builds_tuples_by(self):
        # (start, end, cat, h, prev, flags, pid, past)
        # [first, last, n, cats, cmds, text, wrote, sid]
        self.assertEqual((S, E, CAT, H, PREV, FLAGS, PID, PAST), tuple(range(8)))
        self.assertEqual((P_FIRST, P_LAST, P_N, P_CATS, P_CMDS, P_TEXT, P_WROTE, P_SID),
                         tuple(range(8)))
        self.assertEqual((OPEN, WROTE), (1, 2))

    # ------------------------------------------------------------ the cap
    def test_cut_at_is_the_earliest_start_among_the_calls_not_held(self):
        # a call that started before the first one dropped but exited after it (two at once)
        lg = PhaseLog()
        for s in (0, 10, 40, 25):
            lg.add(C(s, s + 30), cap=2)
        self.assertEqual((len(lg.calls), lg.cut, lg.cut_at), (2, 2, 25))

    def test_a_call_the_cap_dropped_has_no_pid_to_run_past(self):
        lg = PhaseLog()
        lg.add(C(0, 1, pid=11), cap=1)
        lg.add(C(5, 6, pid=12), cap=1)
        self.assertEqual(lg.by_pid, {11: 0})
        self.assertFalse(lg.ran_past(12, 100))

    def test_a_reused_pid_names_the_newest_call(self):
        lg = PhaseLog()
        lg.add(C(0, 1, pid=7), cap=10)
        lg.add(C(500, 501, pid=7), cap=10)
        self.assertTrue(lg.ran_past(7, 900))
        self.assertEqual((lg.calls[0][PAST], lg.calls[1][PAST]), (None, 900))

    # ------------------------------------------------------------ ran_past
    def test_ran_past_only_ever_extends_and_still_claims_the_child(self):
        lg = PhaseLog()
        lg.add(C(0, 5, "run", pid=201), cap=10)
        # a child recorded at or before the call's own end did not outlive it, but it is its child
        self.assertTrue(lg.ran_past(201, 5))
        self.assertTrue(lg.ran_past(201, 3))
        self.assertIsNone(lg.calls[0][PAST])
        lg.ran_past(201, 3600)
        lg.ran_past(201, 100)
        self.assertEqual(lg.calls[0][PAST], 3600)
        self.assertEqual(lg.calls[0][:PAST], (0, 5, "run", 0, 0, 0, 201))

    def test_a_later_process_that_reused_the_pid_is_not_the_calls_child(self):   # Review Focus 3
        # a call's shell ended at 5; hours later another process has its pid, and its child exits
        lg = PhaseLog()
        lg.add(C(0, 5, "run", pid=201), cap=10)
        self.assertFalse(lg.ran_past(201, 7200, start=3600))      # began long after the call ended
        self.assertIsNone(lg.calls[0][PAST])
        self.assertFalse(lg.ran_past(201, 7200, start=65.5))      # just past a minute of slack
        self.assertTrue(lg.ran_past(201, 5.2, start=4.9))         # began while the call ran
        self.assertTrue(lg.ran_past(201, 3600, start=2))          # a job it started, still running
        self.assertEqual(lg.calls[0][PAST], 3600)
        self.assertTrue(lg.ran_past(201, 3601, start=64.0))       # within a minute of its end
        self.assertEqual(lg.calls[0][PAST], 3601)
        self.assertTrue(lg.ran_past(201, 3602, start=65.0))       # exactly a minute after it
        self.assertEqual(lg.calls[0][PAST], 3602)
        self.assertTrue(lg.ran_past(201, 7200))                   # no start known: the pid decides

    def test_a_process_ending_with_its_call_did_not_outlive_it(self):
        # two exits that came together can be written in either order: a few ms is not "past"
        lg = PhaseLog()
        lg.add(C(0, 5, "run", pid=201), cap=10)
        self.assertTrue(lg.ran_past(201, 5.003))
        self.assertTrue(lg.ran_past(201, 6.0))                # exactly PAST_SLACK_S after: not past
        self.assertIsNone(lg.calls[0][PAST])
        self.assertTrue(lg.ran_past(201, 6.5))
        self.assertEqual(lg.calls[0][PAST], 6.5)

    # ------------------------------------------------------- housekeeping
    def test_housekeeping_with_no_time_or_past_the_bin_cap_is_counted_and_not_binned(self):
        lg = PhaseLog()
        lg.add_housekeeping("git", 60, bin_cap=2)
        lg.add_housekeeping("git", 130, bin_cap=2)
        lg.add_housekeeping("git", 200, bin_cap=2)          # a third minute, past the cap
        lg.add_housekeeping("git", 61, bin_cap=2)           # a minute already held still counts
        lg.add_housekeeping("realpath", None, bin_cap=2)    # never stamped
        self.assertEqual(lg.hk, {"git": 4, "realpath": 1})
        self.assertEqual(lg.hk_bins, {60: 2, 120: 1})
        self.assertEqual(lg.hk_cut, 2)
        self.assertEqual(sum(lg.hk.values()), sum(lg.hk_bins.values()) + lg.hk_cut)

    def test_housekeeping_adds_n_at_once_and_nothing_for_zero(self):
        lg = PhaseLog()
        lg.add_housekeeping("git", 125, bin_cap=10, n=4)
        lg.add_housekeeping("realpath", 900, bin_cap=10, n=0)      # nothing happened: nothing held
        self.assertEqual((lg.hk, lg.hk_bins, lg.hk_cut), ({"git": 4}, {120: 4}, 0))
        lg.add_housekeeping("lsof", 500, bin_cap=0, n=3)
        self.assertEqual((lg.hk["lsof"], lg.hk_cut), (3, 3))

    # -------------------------------------------------------------- merge
    def test_merge_holds_what_one_pass_would_have(self):
        a, b = PhaseLog(), PhaseLog()
        for s in (0, 10):
            a.add(C(s, s + 1, pid=100 + s), 4)
        for s in (20, 30, 40):
            b.add(C(s, s + 1, pid=100 + s), 4)
        a.merge(b, 4)
        self.assertEqual([c[S] for c in a.calls], [0, 10, 20, 30])
        self.assertEqual((a.cut, a.cut_at, a.n), (1, 40, 5))
        self.assertEqual(a.by_pid, {100: 0, 110: 1, 120: 2, 130: 3})       # 140 was not held
        self.assertTrue(a.ran_past(130, 999))
        self.assertEqual(a.calls[3][PAST], 999)

    def test_merge_into_a_full_log_drops_every_later_call_and_keeps_the_earlier_cut(self):
        a, b = PhaseLog(), PhaseLog()
        for s in (0, 10, 20):
            a.add(C(s, s + 1), 2)                  # a held two, dropped one at 20
        b.add(C(100, 101), 2)
        for s in (200, 210, 220):
            b.add(C(s, s + 1), 2)                  # b held two, dropped 210 and 220
        a.merge(b, 2)
        self.assertEqual(([c[S] for c in a.calls], a.cut, a.cut_at, a.n), ([0, 10], 5, 20, 7))

    def test_merge_adds_housekeeping_and_takes_the_earlier_cut(self):
        a, b = PhaseLog(), PhaseLog()
        a.add_housekeeping("git", 60, 10)
        a.add_housekeeping("git", None, 10)                 # never stamped: in `hk`, in no bin
        b.add_housekeeping("git", 61, 10, n=2)
        b.add_housekeeping("lsof", 130, 10)
        b.add_housekeeping("lsof", None, 10, n=2)
        b.cut, b.cut_at = 3, 900.0
        a.merge(b, 10)
        self.assertEqual(a.hk, {"git": 4, "lsof": 3})
        self.assertEqual(a.hk_bins, {60: 3, 120: 1})
        self.assertEqual((a.hk_cut, a.cut, a.cut_at), (3, 3, 900.0))
        self.assertEqual(sum(a.hk.values()), sum(a.hk_bins.values()) + a.hk_cut)

    def test_merge_of_empty_logs_changes_nothing(self):
        a, b = PhaseLog(), PhaseLog()
        a.merge(b, 5)
        self.assertEqual((a.calls, a.by_pid, a.hk, a.hk_bins, a.hk_cut, a.cut, a.cut_at, a.n),
                         ([], {}, {}, {}, 0, 0, None, 0))

    # --------------------------------------------------------------- fold
    def test_fold_is_a_new_object_and_never_shares_a_list_with_a_part(self):
        a, b = PhaseLog(), PhaseLog()
        a.add(C(0, 1, pid=10), 10)
        a.add_housekeeping("git", 60, 10)
        b.add(C(5, 6, pid=20), 10)
        b.add_housekeeping("git", 61, 10)
        f = PhaseLog.fold([a, b], 10)
        for mine in (f.calls, f.by_pid, f.hk, f.hk_bins):
            for part in (a, b):
                theirs = [id(part.calls), id(part.by_pid), id(part.hk), id(part.hk_bins)]
                self.assertNotIn(id(mine), theirs)
        # the parts go on ingesting; the fold is a picture of the moment it was taken
        a.add(C(50, 51), 10)
        a.add_housekeeping("git", 60, 10)
        a.ran_past(10, 700)
        self.assertEqual((f.n, f.hk, f.hk_bins), (2, {"git": 2}, {60: 2}))
        self.assertIsNone(f.calls[0][PAST])
        # and anything done to the fold is not done to a part
        f.add(C(99, 100), 10)
        f.add_housekeeping("lsof", 5, 10)
        f.hk_bins[60] = 99
        self.assertEqual((len(b.calls), "lsof" in b.hk, b.hk_bins), (1, False, {60: 1}))
        self.assertEqual(a.hk_bins, {60: 2})

    def test_fold_of_one_part_is_still_a_copy(self):
        a = PhaseLog()
        a.add(C(0, 1, pid=10), 10)
        f = PhaseLog.fold([a], 10)
        self.assertIsNot(f, a)
        self.assertIsNot(f.calls, a.calls)
        self.assertEqual((f.calls, f.n), (a.calls, 1))

    def test_fold_interleaves_the_keys_by_start_and_keeps_the_earliest_cap(self):
        a, b = PhaseLog(), PhaseLog()
        for s in (0, 20, 40):
            a.add(C(s, s + 1, "run", pid=s), 10)
        for s in (10, 30, 50):
            b.add(C(s, s + 1, "test", pid=1000 + s), 10)
        b.cut, b.cut_at = 2, 70.0                  # b dropped two calls of its own, from 70 on
        f = PhaseLog.fold([a, b], 4)
        self.assertEqual([c[S] for c in f.calls], [0, 10, 20, 30])
        self.assertEqual((f.cut, f.cut_at, f.n), (4, 40, 8))
        self.assertEqual(f.by_pid, {0: 0, 1010: 1, 20: 2, 1030: 3})
        self.assertTrue(f.ran_past(1030, 900))
        self.assertEqual(f.calls[3][PAST], 900)

    def test_fold_sums_housekeeping_and_keeps_the_earliest_cut(self):
        a, b = PhaseLog(), PhaseLog()
        a.add_housekeeping("git", 60, 10, n=2)
        a.add_housekeeping("git", None, 10)
        a.cut, a.cut_at = 1, 300.0
        b.add_housekeeping("git", 61, 10)
        b.add_housekeeping("realpath", 130, 10)
        b.cut, b.cut_at = 1, 200.0
        f = PhaseLog.fold([a, b], 10)
        self.assertEqual(f.hk, {"git": 4, "realpath": 1})
        self.assertEqual(f.hk_bins, {60: 3, 120: 1})
        self.assertEqual((f.hk_cut, f.cut, f.cut_at, f.n), (1, 2, 200.0, 2))

    def test_fold_of_nothing_is_an_empty_log(self):
        f = PhaseLog.fold([], 10)
        self.assertEqual((f.calls, f.n, f.cut_at), ([], 0, None))

    # ------------------------------------------------------------- pickle
    def test_a_log_pickles_and_copies(self):
        lg = PhaseLog()
        lg.add(C(0, 5, "run", pid=201), 10)
        lg.ran_past(201, 3600)
        lg.add_housekeeping("git", 60, 10)
        def state(x):
            return (x.calls, x.by_pid, x.hk, x.hk_bins, x.hk_cut, x.cut, x.cut_at, x.n)

        for clone in (pickle.loads(pickle.dumps(lg)), pickle.loads(pickle.dumps(lg, 2)),
                      copy.deepcopy(lg)):
            self.assertEqual(state(clone), state(lg))
        self.assertFalse(hasattr(lg, "__dict__"))


def leaf(pj, s, e=None, cat="explore", cmd="ugrep", text=None, wrote=False, sid="s"):
    return pj.leaf(s, s + 0.1 if e is None else e, cat, cmd, text, wrote, sid)


def gaps(starts):
    starts = sorted(starts)
    return [b - a for a, b in zip(starts, starts[1:])]


class PendingTest(unittest.TestCase):
    def test_children_fold_into_their_parent_and_pop_once(self):
        pj = PendingJoin(cap=100)
        for i in range(3):
            pj.push("h", 500,
                    pj.leaf(10 + 20 * i, 11 + 20 * i, "explore", "ugrep", None, False, "sid1"))
        e = pj.pop("h", 500, start=5)
        self.assertEqual((e[P_N], e[P_FIRST], e[P_LAST], e[P_CATS]), (3, 10, 51, {"explore": 3}))
        self.assertIsNone(pj.pop("h", 500, start=5))

    def test_a_loop_is_regular_per_command_even_when_its_commands_alternate(self):
        pj = PendingJoin(cap=100)
        for i in range(6):
            pj.push("h", 600, pj.leaf(20 * i, 20 * i + 0.1, "explore", "ugrep", None, False, "s"))
            pj.push("h", 600,
                    pj.leaf(20 * i + 0.2, 20 * i + 19.9, "wait", "sleep", None, False, "s"))
        self.assertTrue(pj.loop_like(pj.entries[("h", 600)], poll_min=5, poll_cv=0.25))
        self.assertFalse(pj.loop_like(pj.entries[("h", 600)], poll_min=7, poll_cv=0.25))

    def test_a_reused_pid_does_not_take_an_earlier_processes_children(self):   # Review Focus 3
        pj = PendingJoin(cap=100)
        pj.push("h", 500, pj.leaf(10, 11, "explore", "ls", None, False, "sid1"))
        self.assertIsNone(pj.pop("h", 500, start=1000))
        self.assertNotIn(("h", 500), pj.entries)

    def test_the_payload_text_and_a_write_travel_up(self):
        pj = PendingJoin(cap=100)
        pj.push("h", 301, pj.leaf(1, 2, "edit", "codex", "--codex-run-as-fs-helper", True, "s"))
        bw = pj.pop("h", 301, start=0.5)
        pj.push("h", 300, bw)
        e = pj.pop("h", 300, start=0.4)
        self.assertTrue(e[P_WROTE])
        self.assertEqual(e[P_TEXT], "--codex-run-as-fs-helper")

    def test_the_cap_evicts_and_counts(self):
        pj = PendingJoin(cap=2)
        for p in (1, 2, 3):
            pj.push("h", 1000 + p, pj.leaf(p, p, "run", "x", None, False, "s"))
        self.assertEqual((len(pj.entries), pj.evicted), (2, 1))

    # ------------------------------------------------------------- shape
    def test_a_leaf_is_a_one_record_entry(self):
        pj = PendingJoin(cap=10)
        e = pj.leaf(5.0, 7.5, "slurm", "squeue", "squeue -u me", False, "sid9")
        self.assertEqual(len(e), 11)
        self.assertIsNone(e[P_HELP])                                # no login helper (R72)
        self.assertEqual(
            (e[P_FIRST], e[P_LAST], e[P_N], e[P_CATS], e[P_TEXT], e[P_WROTE], e[P_SID]),
            (5.0, 7.5, 1, {"slurm": 1}, "squeue -u me", False, "sid9"))
        self.assertEqual(e[P_CMDS], {"squeue": [1, 0.0, 0.0, 5.0, 5.0]})
        self.assertEqual((e[P_SINCE], e[P_SINCE_N]), (5.0, 0))      # no child folded in yet
        # a record that names no command is counted, and has no gaps to keep
        self.assertEqual(pj.leaf(1, 2, "run", None, None, False, "s")[P_CMDS], {})
        # whatever says it wrote (a byte count, a flag) is stored as a flag, and nothing as False
        self.assertIs(pj.leaf(1, 2, "edit", "codex", "x", 0.012, "s")[P_WROTE], True)
        self.assertIs(pj.leaf(1, 2, "edit", "codex", "x", None, "s")[P_WROTE], False)

    # ------------------------------------------------------- the stretch (R57)
    def test_the_stretch_runs_from_the_last_gap_of_idle_s_in_what_the_children_did(self):
        pj = PendingJoin(cap=10, idle_s=300)
        pj.push("h", 1, leaf(pj, 0.0, 5.0))                 # busy from 0 to 5
        pj.push("h", 1, leaf(pj, 10.0, 20.0))               # 5 s after: the same stretch
        e = pj.entries[("h", 1)]
        self.assertEqual((e[P_SINCE], e[P_SINCE_N]), (0.0, 2))
        pj.push("h", 1, leaf(pj, 400.0, 401.0))             # 380 s after the last end: a new one
        self.assertEqual((e[P_SINCE], e[P_SINCE_N]), (400.0, 1))
        pj.push("h", 1, leaf(pj, 500.0, 501.0))             # 99 s after: the same
        self.assertEqual((e[P_SINCE], e[P_SINCE_N]), (400.0, 2))
        self.assertEqual((e[P_FIRST], e[P_LAST], e[P_N]), (0.0, 501.0, 4))

    def test_a_long_child_recorded_late_keeps_the_stretch_it_ran_through(self):
        pj = PendingJoin(cap=10, idle_s=300)
        pj.push("h", 1, leaf(pj, 0.0, 1.0))
        pj.push("h", 1, leaf(pj, 600.0, 601.0))             # a new stretch, it seems...
        pj.push("h", 1, leaf(pj, 0.5, 700.0))               # ...but this one ran all along
        e = pj.entries[("h", 1)]
        self.assertEqual((e[P_SINCE], e[P_SINCE_N]), (0.5, 2))

    def test_a_sleep_loop_is_one_stretch_however_long_it_sleeps(self):
        pj = PendingJoin(cap=10, idle_s=300)
        t = 0.0
        for _ in range(10):
            pj.push("h", 1, leaf(pj, t, t + 600.0, cmd="sleep"))     # sleep 600, then the next
            t += 600.5
        e = pj.entries[("h", 1)]
        self.assertEqual((e[P_SINCE], e[P_SINCE_N]), (0.0, 10))

    def test_with_no_idle_s_a_stretch_never_ends(self):
        pj = PendingJoin(cap=10)
        pj.push("h", 1, leaf(pj, 0.0))
        pj.push("h", 1, leaf(pj, 9999.0))
        e = pj.entries[("h", 1)]
        self.assertEqual((e[P_SINCE], e[P_SINCE_N]), (0.0, 2))

    def test_pending_state_is_plain_lists_and_dicts_that_pickle(self):
        pj = PendingJoin(cap=10)
        pj.push("h", 1, pj.leaf(1, 2, "edit", "codex", "x", True, "s"))
        pj.push("h", 1, pj.leaf(3, 4, "wait", "sleep", None, False, "s"))
        e = pj.entries[("h", 1)]
        self.assertIs(type(e), list)
        self.assertIs(type(e[P_CATS]), dict)
        self.assertIs(type(e[P_CMDS]), dict)
        self.assertTrue(all(type(v) is list for v in e[P_CMDS].values()))
        self.assertEqual(pickle.loads(pickle.dumps(pj.entries)), pj.entries)
        self.assertFalse(hasattr(pj, "__dict__"))

    # -------------------------------------------------------------- push
    def test_push_adds_counts_and_takes_the_first_and_last_and_ors_the_write(self):
        pj = PendingJoin(cap=10)
        pj.push("h", 9, pj.leaf(20, 25, "explore", "ls", None, True, "s"))
        pj.push("h", 9, pj.leaf(10, 12, "explore", "cat", None, False, "s"))   # began earlier
        pj.push("h", 9, pj.leaf(30, 31, "edit", "sed", None, False, "s"))
        e = pj.entries[("h", 9)]
        self.assertEqual((e[P_FIRST], e[P_LAST], e[P_N], e[P_CATS]),
                         (10, 31, 3, {"explore": 2, "edit": 1}))
        self.assertIs(e[P_WROTE], True)                  # one write is enough, whenever it came
        quiet = fold_entry(pj.leaf(1, 2, "run", "x", None, False, "s"),
                           pj.leaf(3, 4, "run", "y", None, False, "s"))
        self.assertIs(quiet[P_WROTE], False)

    def test_the_entrys_own_text_comes_first_then_the_first_text_it_lacked(self):
        pj = PendingJoin(cap=10)
        pj.push("h", 9, pj.leaf(1, 2, "run", "bwrap", None, False, "s"))
        pj.push("h", 9, pj.leaf(3, 4, "run", "a", "first text", False, "s"))
        pj.push("h", 9, pj.leaf(5, 6, "run", "b", "second text", False, "s"))
        self.assertEqual(pj.entries[("h", 9)][P_TEXT], "first text")

    def test_the_session_is_the_newest_child_named(self):                  # R59
        # A loop past midnight: its children are yesterday's session's, then today's, and the
        # entry is today's, as a finished call is its own record's. A child with none named
        # leaves it.
        pj = PendingJoin(cap=10)
        pj.push("h", 9, pj.leaf(1, 2, "run", "a", None, False, None))
        pj.push("h", 9, pj.leaf(3, 4, "run", "b", None, False, "s1"))
        self.assertEqual(pj.entries[("h", 9)][P_SID], "s1")
        pj.push("h", 9, pj.leaf(5, 6, "run", "c", None, False, "s2"))
        self.assertEqual(pj.entries[("h", 9)][P_SID], "s2")
        pj.push("h", 9, pj.leaf(7, 8, "run", "d", None, False, None))
        self.assertEqual(pj.entries[("h", 9)][P_SID], "s2")

    def test_a_fold_keeps_the_session_of_the_entry_folded_into(self):      # R59
        # The reducer folds what ran under a process into that process's own leaf, the newer:
        # the folded-in side never wins.
        own = fold_entry(PendingJoin.leaf(9, 10, "run", "bash", None, False, "today"),
                         PendingJoin.leaf(1, 2, "run", "ls", None, False, "yesterday"))
        self.assertEqual(own[P_SID], "today")
        unnamed = fold_entry(PendingJoin.leaf(9, 10, "run", "bash", None, False, None),
                             PendingJoin.leaf(1, 2, "run", "ls", None, False, "yesterday"))
        self.assertEqual(unnamed[P_SID], "yesterday")

    def test_a_host_and_a_pid_name_one_parent(self):
        pj = PendingJoin(cap=10)
        pj.push("h1", 9, pj.leaf(1, 2, "run", "a", None, False, "s"))
        pj.push("h2", 9, pj.leaf(1, 2, "run", "a", None, False, "s"))
        self.assertEqual(sorted(pj.entries), [("h1", 9), ("h2", 9)])

    def test_touched_is_noted_only_while_tracking(self):
        pj = PendingJoin(cap=10)
        self.assertIsNone(pj.touched)
        pj.push("h", 1, pj.leaf(1, 2, "run", "a", None, False, "s"))
        self.assertIsNone(pj.touched)
        pj.touched = set()
        pj.push("h", 1, pj.leaf(3, 4, "run", "a", None, False, "s"))
        pj.push("h", 2, pj.leaf(3, 4, "run", "a", None, False, "s"))
        pj.pop("h", 1, start=0)
        self.assertEqual(pj.touched, {("h", 1), ("h", 2)})

    def test_the_cap_evicts_the_one_pushed_to_least_recently_and_a_held_key_does_not_evict(self):
        pj = PendingJoin(cap=2)
        pj.push("h", 1, pj.leaf(1, 1, "run", "x", None, False, "s"))
        pj.push("h", 2, pj.leaf(2, 2, "run", "x", None, False, "s"))
        pj.push("h", 1, pj.leaf(3, 3, "run", "x", None, False, "s"))   # held: folds, nothing leaves
        self.assertEqual((sorted(pj.entries), pj.evicted), ([("h", 1), ("h", 2)], 0))
        self.assertEqual(pj.entries[("h", 1)][P_N], 2)
        pj.push("h", 3, pj.leaf(4, 4, "run", "x", None, False, "s"))
        self.assertEqual((sorted(pj.entries), pj.evicted), ([("h", 1), ("h", 3)], 1))

    def test_a_loop_pushed_to_all_along_outlives_the_entries_that_never_pop(self):
        # A tool shell polls all day (its pid 7, a child every 20 s), while late children of parents
        # long gone each leave an entry that is never pushed to again: the loop keeps its start.
        pj = PendingJoin(cap=50)
        t = 0.0
        for k in range(200):
            pj.push("h", 7, pj.leaf(t, t + 15, "wait", "sleep", "sleep 15", False, "s"))
            for j in range(3):
                pj.push("h", 1000 + 3 * k + j, pj.leaf(t + 1, t + 1, "explore", "head", None, False, "s"))
            t += 20.0
        self.assertEqual(pj.entries[("h", 7)][P_FIRST], 0.0)
        self.assertEqual(pj.entries[("h", 7)][P_N], 200)
        self.assertEqual((len(pj.entries), pj.evicted), (50, 551))

    def test_a_cap_of_zero_keeps_the_newest_and_does_not_raise(self):
        pj = PendingJoin(cap=0)
        for p in (1, 2, 3):
            pj.push("h", p, pj.leaf(p, p, "run", "x", None, False, "s"))
        self.assertEqual((sorted(pj.entries), pj.evicted), ([("h", 3)], 2))

    # --------------------------------------------------------------- pop
    def test_pop_refuses_only_what_began_over_a_minute_before_the_parent_did(self):
        pj = PendingJoin(cap=10)
        pj.push("h", 1, pj.leaf(940.0, 941.0, "run", "x", None, False, "s"))
        self.assertIsNotNone(pj.pop("h", 1, start=1000.0))     # exactly a minute: the stamps' slack
        pj.push("h", 1, pj.leaf(939.9, 941.0, "run", "x", None, False, "s"))
        self.assertIsNone(pj.pop("h", 1, start=1000.0))
        self.assertNotIn(("h", 1), pj.entries)

    def test_a_refused_entry_is_counted_with_the_records_it_held(self):   # final Important 5
        pj = PendingJoin(cap=10)
        self.assertEqual((pj.refused, pj.refused_records), (0, 0))
        for k in range(3):
            pj.push("h", 1, pj.leaf(100.0 + k, 101.0 + k, "run", "x", None, False, "s"))
        self.assertIsNone(pj.pop("h", 1, start=5000.0))         # another process, with that pid
        self.assertEqual((pj.refused, pj.refused_records), (1, 3))
        pj.push("h", 2, pj.leaf(4990.0, 4991.0, "run", "x", None, False, "s"))
        self.assertIsNotNone(pj.pop("h", 2, start=5000.0))      # taken: not refused
        self.assertIsNone(pj.pop("h", 3, start=5000.0))         # no entry: nothing refused
        self.assertEqual((pj.refused, pj.refused_records), (1, 3))

    def test_a_child_that_seems_to_begin_30_s_before_its_parent_is_not_refused(self):   # R16
        # A record is stamped when the collector handled the exit, and a start is that less the
        # duration, so a real child can seem to begin before its parent: 0.62% of real pairs by
        # one second to a minute. Refusing them lost each one its text and its counts.
        pj = PendingJoin(cap=10)
        pj.push("h", 1, pj.leaf(70.0, 71.0, "run", "x", "payload", False, "s"))
        e = pj.pop("h", 1, start=100.0)                         # the parent seems to begin at 100
        self.assertIsNotNone(e)
        self.assertEqual((e[P_N], e[P_TEXT]), (1, "payload"))

    def test_pop_with_no_start_takes_the_entry(self):
        pj = PendingJoin(cap=10)
        pj.push("h", 1, pj.leaf(5, 6, "run", "x", None, False, "s"))
        self.assertEqual(pj.pop("h", 1, start=None)[P_N], 1)

    def test_pop_of_a_parent_with_no_children_is_none(self):
        self.assertIsNone(PendingJoin(cap=10).pop("h", 1, start=0))

    # ----------------------------------------------------------- for_sids
    def test_for_sids_returns_the_entries_of_those_sessions(self):
        pj = PendingJoin(cap=10)
        pj.push("h", 1, pj.leaf(1, 2, "run", "x", None, False, "a"))
        pj.push("h", 2, pj.leaf(1, 2, "run", "x", None, False, "b"))
        pj.push("h", 3, pj.leaf(1, 2, "run", "x", None, False, "c"))
        got = pj.for_sids({"a", "c"})
        self.assertEqual(sorted(e[P_SID] for e in got), ["a", "c"])
        self.assertEqual(pj.for_sids(set()), [])
        self.assertIn(pj.entries[("h", 1)], got)

    # ------------------------------------------------------ gap statistics
    def test_the_gap_stats_are_welfords_over_one_commands_starts(self):
        pj = PendingJoin(cap=10)
        starts = [0.0, 3.0, 4.0, 10.0, 11.5, 30.0]
        for s in starts:
            pj.push("h", 1, leaf(pj, s))
        n, mean, m2, last, first = pj.entries[("h", 1)][P_CMDS]["ugrep"]
        g = gaps(starts)
        self.assertEqual((n, last, first), (6, 30.0, 0.0))
        self.assertAlmostEqual(mean, statistics.fmean(g))
        self.assertAlmostEqual(m2, sum((x - statistics.fmean(g)) ** 2 for x in g))

    def test_a_start_recorded_out_of_order_makes_a_gap_and_never_a_negative_one(self):
        pj = PendingJoin(cap=10)
        for s in (10.0, 30.0, 20.0):              # parallel children exit in another order
            pj.push("h", 1, leaf(pj, s))
        # the gaps are 20 s (10 to 30) and 10 s (from 30 back to 20), never -10
        self.assertEqual(pj.entries[("h", 1)][P_CMDS]["ugrep"], [3, 15.0, 50.0, 30.0, 10.0])

    def test_two_entries_fold_with_chans_formulas_and_the_gap_between_them(self):
        pj = PendingJoin(cap=10)
        a_starts, b_starts = [0.0, 10.0, 20.0, 31.0], [100.0, 105.0, 111.0]
        for s in a_starts:
            pj.push("h", 1, leaf(pj, s))
        for s in b_starts:
            pj.push("h", 2, leaf(pj, s))
        pj.push("h", 1, pj.pop("h", 2, start=0))      # a whole entry joins another
        n, mean, m2, last, _first = pj.entries[("h", 1)][P_CMDS]["ugrep"]
        g = gaps(a_starts + b_starts)                    # the 69 s from 31 to 100 too (R57)
        self.assertEqual((n, last), (7, 111.0))
        self.assertAlmostEqual(mean, statistics.fmean(g))
        self.assertAlmostEqual(m2, sum((x - statistics.fmean(g)) ** 2 for x in g))

    def test_two_runs_fold_to_the_stats_of_their_gaps_over_random_runs(self):
        rng = random.Random(7)
        for _ in range(300):
            a_starts = sorted(rng.uniform(0.0, 500.0) for _ in range(rng.randint(1, 6)))
            b_starts = sorted(rng.uniform(1000.0, 1500.0) for _ in range(rng.randint(1, 6)))
            pj = PendingJoin(cap=100)
            for s in a_starts:
                pj.push("h", 1, leaf(pj, s))
            for s in b_starts:
                pj.push("h", 2, leaf(pj, s))
            whole = fold_entry(pj.pop("h", 1, start=0), pj.pop("h", 2, start=0))
            g = gaps(a_starts + b_starts)         # the gap between the two runs is one of them
            n, mean, m2, last, _first = whole[P_CMDS]["ugrep"]
            self.assertEqual((n, last), (len(a_starts) + len(b_starts), b_starts[-1]))
            self.assertAlmostEqual(mean, statistics.fmean(g), places=6)
            self.assertAlmostEqual(m2, sum((x - statistics.fmean(g)) ** 2 for x in g), places=4)

    def test_folds_read_every_gap_between_their_runs_exactly(self):
        # The gap between two runs is a gap of the command's starts too (R57): folding three runs
        # gives the stats of every gap, the pauses between runs included.
        rng = random.Random(3)
        for _ in range(200):
            runs, t = [], 0.0
            for _r in range(3):
                starts = []
                for _s in range(rng.randint(5, 12)):
                    t += rng.uniform(1.0, 30.0)
                    starts.append(t)
                t += rng.uniform(100.0, 300.0)
                runs.append(starts)
            pj = PendingJoin(cap=10)
            for i, starts in enumerate(runs):
                for s in starts:
                    pj.push("h", i, leaf(pj, s))
            whole = fold_entry(fold_entry(pj.pop("h", 0, start=0), pj.pop("h", 1, start=0)),
                               pj.pop("h", 2, start=0))
            n, mean, m2, _last, _first = whole[P_CMDS]["ugrep"]
            g = gaps([x for starts in runs for x in starts])
            self.assertEqual(n, sum(map(len, runs)))
            self.assertAlmostEqual(mean, statistics.fmean(g), places=6)
            self.assertAlmostEqual(m2, sum((x - statistics.fmean(g)) ** 2 for x in g), places=3)

    def test_bursts_an_hour_apart_are_no_loop(self):
        # boslogin06 apid 3380957: `uname` twice a second in each hourly update check, which read
        # as a loop polling every 0.4 s while the gap between bursts was taken as the mean
        pj = PendingJoin(cap=10)
        for h in range(6):
            pj.push("h", 10 + h, leaf(pj, 3600.0 * h))
            pj.push("h", 10 + h, leaf(pj, 3600.0 * h + 0.4))
            pj.push("h", 1, pj.pop("h", 10 + h, start=0))
        whole = pj.entries[("h", 1)]
        self.assertEqual(whole[P_CMDS]["ugrep"][0], 12)
        self.assertFalse(pj.loop_like(whole, poll_min=5, poll_cv=0.25))

    def test_a_fold_keeps_the_count_of_gaps_one_below_the_count_of_starts(self):
        # So the next fold weighs it right: a loop split across entries is still one regular loop.
        pj = PendingJoin(cap=10)
        for i in range(3):
            pj.push("h", 1, leaf(pj, 20.0 * i))
            pj.push("h", 2, leaf(pj, 60.0 + 20.0 * i))
            pj.push("h", 3, leaf(pj, 120.0 + 20.0 * i))
        whole = fold_entry(fold_entry(pj.pop("h", 1, 0), pj.pop("h", 2, 0)), pj.pop("h", 3, 0))
        self.assertEqual(whole[P_CMDS]["ugrep"][0], 9)
        self.assertTrue(pj.loop_like(whole, poll_min=9, poll_cv=0.25))
        self.assertFalse(pj.loop_like(whole, poll_min=10, poll_cv=0.25))

    def test_only_five_commands_are_tracked_and_the_rest_count_in_the_total(self):
        pj = PendingJoin(cap=10)
        for i, cmd in enumerate("abcdefg"):
            pj.push("h", 1, leaf(pj, float(i), cmd=cmd))
        pj.push("h", 1, leaf(pj, 20.0, cmd="a"))
        e = pj.entries[("h", 1)]
        self.assertEqual(sorted(e[P_CMDS]), ["a", "b", "c", "d", "e"])
        self.assertEqual((e[P_N], e[P_CMDS]["a"][0]), (8, 2))
        # a whole entry folded in is held to the same five
        pj.push("h", 2, leaf(pj, 50.0, cmd="z"))
        pj.push("h", 2, leaf(pj, 60.0, cmd="a"))
        pj.push("h", 1, pj.pop("h", 2, start=0))
        e = pj.entries[("h", 1)]
        self.assertEqual((sorted(e[P_CMDS]), e[P_N], e[P_CMDS]["a"][0]),
                         (["a", "b", "c", "d", "e"], 10, 3))

    def test_a_helper_start_is_counted_apart_from_its_programs_and_folds_with_it(self):   # R72
        # The program list is as it was, helpers in it; P_HELP says how many of a program's starts
        # were a login shell's helper, so `seen` can leave out a program the call never ran itself.
        pj = PendingJoin(cap=10)
        a = pj.leaf(1.0, 1.1, "explore", "grep", "grep -q /opt/puppetlabs/bin", False, "s", helper=True)
        self.assertEqual((a[P_CMDS]["grep"][0], a[P_HELP]), (1, {"grep": 1}))
        b = pj.leaf(20.0, 20.1, "explore", "grep", "grep -q DONE f", False, "s")
        self.assertIsNone(b[P_HELP])
        fold_entry(b, a)
        self.assertEqual((b[P_CMDS]["grep"][0], b[P_HELP]), (2, {"grep": 1}))
        self.assertIsNot(b[P_HELP], a[P_HELP])
        # a helper the full list does not admit is counted nowhere, as its starts are not
        for i, cmd in enumerate("vwxyz"):
            pj.push("h", 1, leaf(pj, float(i), cmd=cmd))
        pj.push("h", 1, pj.leaf(9.0, 9.1, "explore", "id", "/usr/bin/id -un", False, "s", helper=True))
        e = pj.entries[("h", 1)]
        self.assertEqual((sorted(e[P_CMDS]), e[P_N], e[P_HELP]), (list("vwxyz"), 6, None))

    def test_fold_entry_does_not_share_a_stat_list_with_what_it_folds_in(self):
        pj = PendingJoin(cap=10)
        a, b = leaf(pj, 1.0, cmd="x"), leaf(pj, 5.0, cmd="y")
        out = fold_entry(a, b)
        self.assertIs(out, a)
        out[P_CMDS]["y"][0] = 99
        self.assertEqual(b[P_CMDS]["y"][0], 1)

    # ---------------------------------------------------------- loop_like
    def test_loop_like_wants_a_regular_command_started_often_enough(self):
        pj = PendingJoin(cap=10)
        for s in (0.0, 5.0, 40.0, 41.0, 100.0, 180.0):
            pj.push("h", 1, leaf(pj, s))                                     # irregular
        for i in range(6):
            pj.push("h", 2, leaf(pj, 20.0 * i + (0.5 if i % 2 else 0.0)))   # 20 s, give or take
        self.assertFalse(pj.loop_like(pj.entries[("h", 1)], poll_min=5, poll_cv=0.25))
        self.assertTrue(pj.loop_like(pj.entries[("h", 2)], poll_min=5, poll_cv=0.25))
        self.assertFalse(pj.loop_like(pj.entries[("h", 2)], poll_min=5, poll_cv=0.001))
        self.assertFalse(pj.loop_like(pj.entries[("h", 2)], poll_min=7, poll_cv=0.25))

    def test_loop_like_is_false_for_starts_at_one_instant_and_for_one_start(self):
        pj = PendingJoin(cap=10)
        for _ in range(8):
            pj.push("h", 1, leaf(pj, 50.0))                       # a burst: no spacing at all
        pj.push("h", 2, leaf(pj, 50.0))
        self.assertFalse(pj.loop_like(pj.entries[("h", 1)], poll_min=5, poll_cv=0.25))
        self.assertFalse(pj.loop_like(pj.entries[("h", 2)], poll_min=1, poll_cv=0.25))
        no_command = pj.leaf(1, 2, "run", None, None, False, "s")
        self.assertFalse(pj.loop_like(no_command, poll_min=1, poll_cv=0.25))
        foreign = pj.leaf(1, 2, "run", "x", None, False, "s")
        foreign[P_CMDS]["x"] = [1, 5.0, 0.0, 1.0]            # one start has no gap: no divide
        self.assertFalse(pj.loop_like(foreign, poll_min=1, poll_cv=0.25))

    def test_the_spread_is_the_population_stdev_of_the_gaps(self):
        # gaps 10, 10, 10, 10, 20: mean 12, stdev 4 over the five gaps, so 1/3 of the mean
        pj = PendingJoin(cap=10)
        for s in (0.0, 10.0, 20.0, 30.0, 40.0, 60.0):
            pj.push("h", 1, leaf(pj, s))
        e = pj.entries[("h", 1)]
        self.assertFalse(pj.loop_like(e, poll_min=5, poll_cv=0.32))
        self.assertTrue(pj.loop_like(e, poll_min=5, poll_cv=0.34))

    def test_loop_like_is_per_command_and_not_over_all_the_children(self):
        # 40 rounds of a 0.12 s check and a 20 s sleep: the children's starts alternate between gaps
        # of 0.12 s and 20 s, and each command's own starts are 20.12 s apart.
        pj = PendingJoin(cap=10)
        for i in range(40):
            pj.push("h", 1, leaf(pj, 20.12 * i, cmd="pgrep"))
            pj.push("h", 1, leaf(pj, 20.12 * i + 0.12, cmd="sleep"))
        e = pj.entries[("h", 1)]
        self.assertTrue(pj.loop_like(e, poll_min=5, poll_cv=0.1))
        every = sorted([20.12 * i for i in range(40)] + [20.12 * i + 0.12 for i in range(40)])
        g = gaps(every)
        self.assertGreater(statistics.pstdev(g) / statistics.fmean(g), 0.9)


if __name__ == "__main__":
    unittest.main()
