"""The phase report: the days it reads, what it counts per row, the lines it prints for a row's
tasks, and day-files read end to end.

    cd dashboard && uv run python -m unittest discover -s tests -p 'test_phase_report.py' -v
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from rc_dashboard import phase_report as pr                     # noqa: E402
from rc_dashboard.reducers import trajectories as trajmod       # noqa: E402
from rc_dashboard.reducers.phase_log import FLAGS, OPEN         # noqa: E402
from test_sessions import HOME, rec                             # noqa: E402
from test_traj_phases import HELPER, bash8, fed, root8, s8      # noqa: E402
from test_traj_tasks import configured                          # noqa: E402

DAY = "2026-10-28"
DAY2 = "2026-10-29"
DAY3 = "2026-10-30"
NOON = time.mktime((2026, 10, 28, 12, 0, 0, 0, 0, -1))
MIDNIGHT = time.mktime((2026, 10, 29, 0, 0, 0, 0, 0, -1))      # the local midnight that ends DAY
NOON2 = time.mktime((2026, 10, 29, 12, 0, 0, 0, 0, -1))


def call(t, cat, flags=0):
    """A call tuple as `phase_log` lays it out."""
    return (t, t + 1.0, cat, 1, 0, flags, None, None, 0)


def phase(kind, t0, t1, n=0, **kw):
    return dict({"kind": kind, "t0": t0, "t1": t1, "n": n, "open": False}, **kw)


def task(**kw):
    """A task as a row's phase block carries it."""
    return dict({"kind": "task", "t0": NOON, "t1": NOON + 3000, "n": 12, "label": "Run train.py",
                 "source": None, "ref": "", "names": [], "phases": [0, 1], "open": False,
                 "incomplete": False, "gap": None, "sid": None}, **kw)


def reads(first, count, pid, root=100, every=100):
    """`count` reads of a file of its own by agent `root`, one every `every` seconds from `first`,
    with the pids `pid` on."""
    return [bash8(first + every * i, pid + i, "cat src/f%d.py" % (pid + i), root=root)
            for i in range(count)]


def write_feed(root, by_day):
    """Day-files as the collector writes them: `<root>/<day>/h1.exits.jsonl`, each holding the
    records `by_day` gives that day."""
    for day, recs in by_day.items():
        os.makedirs(os.path.join(root, day))
        with open(os.path.join(root, day, "h1.exits.jsonl"), "w") as fh:
            for r in sorted(recs, key=lambda r: r["ts_epoch"]):
                fh.write(json.dumps(r) + "\n")


class DayTest(unittest.TestCase):
    def test_a_day_is_local_midnight_to_midnight(self):
        since, until = pr.day_bounds(DAY)
        self.assertEqual(time.localtime(since)[:6], (2026, 10, 28, 0, 0, 0))
        self.assertEqual(time.localtime(until)[:6], (2026, 10, 29, 0, 0, 0))

    def test_the_days_directory_and_the_next_ones_are_read(self):
        with tempfile.TemporaryDirectory() as d:
            for sub in ("2026-10-27", DAY, "2026-10-29"):
                os.makedirs(os.path.join(d, sub))
            self.assertEqual(pr.day_dirs([d], DAY),
                             [os.path.join(d, DAY), os.path.join(d, "2026-10-29")])
            self.assertEqual(pr.day_dirs([d], "2026-10-29"), [os.path.join(d, "2026-10-29")])
            self.assertEqual(pr.day_dirs([d], "2026-10-01"), [])     # a dated root without it

    def test_a_root_that_holds_its_files_is_read_as_it_is(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "h1.exits.jsonl").write_text("")
            self.assertEqual(pr.day_dirs([d], DAY), [d])

    def test_days_end_at_the_midnight_after_the_last_of_them(self):
        since, until = pr.day_bounds(DAY, 3)
        self.assertEqual(time.localtime(since)[:6], (2026, 10, 28, 0, 0, 0))
        self.assertEqual(time.localtime(until)[:6], (2026, 10, 31, 0, 0, 0))
        self.assertEqual(pr.day_bounds(DAY, 1), pr.day_bounds(DAY))

    def test_each_days_directory_and_the_one_after_the_last_are_read(self):
        with tempfile.TemporaryDirectory() as d:
            for sub in ("2026-10-27", DAY, DAY2, DAY3):
                os.makedirs(os.path.join(d, sub))

            def under(*names):
                return [os.path.join(d, n) for n in names]
            self.assertEqual(pr.day_dirs([d], DAY, 2), under(DAY, DAY2, DAY3))
            self.assertEqual(pr.day_dirs([d], DAY, 1), under(DAY, DAY2))
            self.assertEqual(pr.day_dirs([d], "2026-10-27", 3),
                             under("2026-10-27", DAY, DAY2, DAY3))

    def test_a_day_the_root_lacks_hides_none_of_the_others(self):
        with tempfile.TemporaryDirectory() as d:
            for sub in (DAY2, DAY3):
                os.makedirs(os.path.join(d, sub))
            self.assertEqual(pr.day_dirs([d], DAY, 2),
                             [os.path.join(d, DAY2), os.path.join(d, DAY3)])
            self.assertEqual(pr.day_dirs([d], DAY, 1), [])          # as before: not the first day

    def test_the_days_are_a_count_of_at_least_one(self):
        with tempfile.TemporaryDirectory() as d:
            for days in ("0", "-1", "x"):
                err = io.StringIO()
                with self.subTest(days=days), contextlib.redirect_stderr(err):
                    with self.assertRaises(SystemExit):
                        pr.main(["--roots", d, "--day", DAY, "--days", days])
                    self.assertIn("argument --days: ", err.getvalue())


class StatsTest(unittest.TestCase):
    def test_quantiles_are_the_nearest_rank(self):
        vals = list(range(1, 11))
        self.assertEqual(pr.quantile(vals, 0.5), 5)
        self.assertEqual(pr.quantile(vals, 0.9), 9)
        self.assertEqual(pr.quantile([7], 0.9), 7)
        self.assertIsNone(pr.quantile([], 0.5))

    def test_idle_is_no_phase_and_an_active_hour_has_a_call_starting_in_it(self):
        t = NOON
        block = {"list": [phase("explore", t, t + 100, 2), phase("idle", t + 100, t + 4000),
                          phase("test", t + 4000, t + 4100, 1)]}
        st = pr.row_stats(block, [call(t, "explore"), call(t + 50, "explore"),
                                  call(t + 4000, "test")])
        self.assertEqual(st["phases"], 2)
        self.assertEqual(st["active_hours"], 2)                     # 12:00 and 13:06
        self.assertEqual(st["per_hour"], 1.0)
        self.assertEqual((st["no_edit"], st["off"], st["unplaced"]), ([], [], 0))

    def test_a_solid_edit_with_no_edit_call_is_counted(self):
        t = NOON
        block = {"list": [phase("edit", t, t + 60, 1), phase("edit", t + 60, t + 120, 1),
                          phase("edit_guess", t + 120, t + 180), phase("test", t + 180, t + 200, 1)]}
        st = pr.row_stats(block, [call(t, "edit"), call(t + 60, "explore"), call(t + 180, "test")])
        self.assertEqual(st["no_edit"], [1])                        # the guess is never solid

    def test_a_call_is_placed_in_the_last_phase_begun_before_it(self):
        t = NOON
        # the block rounds t0 to the millisecond: a call 0.4 ms before it is its own
        block = {"list": [phase("explore", t, t + 10.001, 1), phase("run", t + 10.001, t + 20, 2)]}
        calls = [call(t, "explore"), call(t + 10.0006, "run"), call(t + 15, "run")]
        self.assertEqual(pr.place(block["list"], calls), [0, 1, 1])
        self.assertEqual(pr.row_stats(block, calls)["off"], [])
        block["list"][1]["n"] = 3
        self.assertEqual(pr.row_stats(block, calls)["off"], [1])     # its count is not its calls

    def test_a_call_in_an_idle_gap_or_before_every_phase_is_reported(self):
        t = NOON
        block = {"list": [phase("explore", t, t + 10, 1), phase("idle", t + 10, t + 400)]}
        st = pr.row_stats(block, [call(t - 5, "explore"), call(t, "explore")])
        self.assertEqual(st["unplaced"], 1)

    def test_a_row_with_no_calls_has_no_rate(self):
        st = pr.row_stats({"list": []}, [])
        self.assertEqual((st["phases"], st["active_hours"], st["per_hour"]), (0, 0, None))

    def test_open_phases_are_counted(self):
        t = NOON
        block = {"list": [phase("wait", t, t + 600, 1, open=True)]}
        self.assertEqual(pr.row_stats(block, [call(t, "wait", OPEN)])["open"], 1)

    def test_a_row_of_a_joined_block_counts_what_is_its_days(self):
        # One block of two days' rows: each phase says whose it is, and each row counts those
        # phases, and its own calls for the hours, so that nothing is counted twice
        t = NOON
        block = {"list": [phase("explore", t, t + 100, 2, sid="a"),
                          phase("run", t + 7200, t + 7300, 2, open=True, sid="b")]}
        calls = [call(t, "explore"), call(t + 50, "explore"),
                 call(t + 7200, "run"), call(t + 7250, "run")]
        first = pr.row_stats(block, calls, "a", calls[:2])
        second = pr.row_stats(block, calls, "b", calls[2:])
        self.assertEqual([(st["phases"], st["active_hours"], st["open"]) for st in (first, second)],
                         [(1, 1, 0), (1, 1, 1)])
        self.assertEqual([st["per_hour"] for st in (first, second)], [1.0, 1.0])
        self.assertEqual([(st["no_edit"], st["off"], st["unplaced"]) for st in (first, second)],
                         [([], [], 0)] * 2)
        # a phase whose count is wrong is its own row's to report, and a call in no phase too
        block["list"][1]["n"] = 3
        calls.append(call(t - 5, "explore"))
        first = pr.row_stats(block, calls, "a", calls[:2] + calls[4:])
        second = pr.row_stats(block, calls, "b", calls[2:4])
        self.assertEqual([(st["off"], st["unplaced"]) for st in (first, second)],
                         [([], 1), ([1], 0)])

    def test_a_block_of_one_day_is_its_rows_whole(self):
        # its phases have no `sid`, so every one is the row's, whatever sid the row is asked by
        t = NOON
        block = {"list": [phase("explore", t, t + 100, 2), phase("run", t + 200, t + 300, 1)]}
        calls = [call(t, "explore"), call(t + 50, "explore"), call(t + 200, "run")]
        whole = pr.row_stats(block, calls)
        self.assertEqual(pr.row_stats(block, calls, "h1|2026-10-28|agent|apid:1", calls), whole)
        self.assertEqual(whole["phases"], 2)

    def test_an_item_with_no_sid_is_every_rows_and_one_with_a_sid_is_that_rows(self):
        items = [{"sid": "a"}, {"sid": "b"}, {"sid": None}, {}]
        self.assertEqual(pr.of_day(items, "a"), [{"sid": "a"}, {"sid": None}, {}])
        self.assertEqual(pr.of_day(items, "c"), [{"sid": None}, {}])


class ShapeTest(unittest.TestCase):
    def shape(self, command, args, kind):
        return pr.call_shape({"command": command, "args": args}, kind)

    def test_a_bare_command_is_its_program_and_the_fs_helper_is_named(self):
        self.assertEqual(self.shape("awk", "awk -F: {print $1}", "bare"), "awk")
        self.assertEqual(self.shape("codex", HELPER, "bare"), "codex fs helper")

    def test_a_shell_call_is_its_first_program_past_its_setup(self):
        self.assertEqual(self.shape("bash", "/usr/bin/bash -lc cd /x && FOO=1 pytest -q", "shell"),
                         "pytest")
        self.assertEqual(self.shape("bash", "/usr/bin/bash -lc cd /x", "shell"), "(setup only)")

    def test_a_wrapper_is_its_payloads_program(self):
        self.assertEqual(pr.call_shape({"command": "bwrap", "args": "bwrap -- x"}, "wrapper",
                                       "/usr/bin/bash -lc sed -n 1,20p a.py"), "sed")
        self.assertEqual(pr.call_shape({"command": "bwrap", "args": "bwrap"}, "wrapper"),
                         "(no payload)")


class TaskLinesTest(unittest.TestCase):
    """The lines a row's tasks print: a task, the gap before it, and a lone stretch."""

    def test_a_built_label_is_bare(self):
        self.assertEqual(pr.task_line(0, task(), DAY),
                         "  T0 12:00–12:50  50m00s  task  Run train.py")

    def test_a_quoted_label_shows_its_quotes_and_where_the_words_came_from(self):
        for i, (source, ref, tail) in enumerate((("commit", "", '"Fix the loader" [commit]'),
                                                 ("pr", "#235", '"Fix the loader" [pr #235]'),
                                                 ("log", "", '"Fix the loader" [log]'))):
            with self.subTest(source=source):
                fixed = task(label="Fix the loader", source=source, ref=ref)
                self.assertEqual(pr.task_line(i + 3, fixed, DAY),
                                 "  T%d 12:00–12:50  50m00s  task  %s" % (i + 3, tail))

    def test_a_time_on_another_day_carries_its_date(self):
        # as the phase lines' clock does: the task began the evening before
        line = pr.task_line(0, task(t0=NOON - 50400, t1=NOON), DAY)
        self.assertEqual(line, "  T0 10-27 22:00–12:00  14h00m  task  Run train.py")

    def test_a_label_is_one_masked_line(self):
        line = pr.task_line(0, task(label="Run\ttrain.py\n  --token=abc123def456ghi789"), DAY)
        self.assertNotIn("\n", line)
        self.assertNotIn("\t", line)
        self.assertIn("Run train.py", line)
        self.assertNotIn("abc123def456ghi789", line)

    def test_a_gap_line_is_the_gap_and_names_what_is_still_running(self):
        self.assertEqual(pr.gap_line({"s": 7680, "alive": []}, DAY), "  gap 2h08m")
        # by the name the service gives the call, as the page does, and else by its preview
        alive = [["mkdir -p scratchpad/linktest && cd x && timeout 60 bash t.sh", NOON, "t.sh"]]
        self.assertEqual(pr.gap_line({"s": 7680, "alive": alive}, DAY),
                         "  gap 2h08m · t.sh still running since 12:00")
        self.assertEqual(pr.gap_line({"s": 7680, "alive": [["linktest --all", NOON, None]]}, DAY),
                         "  gap 2h08m · linktest --all still running since 12:00")

    def test_a_call_that_has_no_preview_is_said_to_be_still_running(self):
        # an open call is read through its children, and has no preview: it is named by them
        self.assertEqual(pr.gap_line({"s": 7680, "alive": [[None, NOON + 61, "pgrep and sleep"]]},
                                     DAY),
                         "  gap 2h08m · pgrep and sleep still running since 12:01")
        self.assertEqual(pr.gap_line({"s": 7680, "alive": [[None, NOON + 61, None]]}, DAY),
                         "  gap 2h08m · a call still running since 12:01")
        # each of the calls still running is named, the earliest first
        alive = [["a", NOON, None], [None, NOON + 60, None]]
        self.assertEqual(pr.gap_line({"s": 90, "alive": alive}, DAY),
                         "  gap 1m30s · a still running since 12:00 · "
                         "a call still running since 12:01")

    def test_a_preview_is_masked_and_cut_to_one_line(self):
        long = "python train.py --token=abc123def456ghi789 " + "x " * 60
        line = pr.gap_line({"s": 7680, "alive": [[long, NOON, None]]}, DAY)
        self.assertNotIn("abc123def456ghi789", line)
        self.assertTrue(line.startswith("  gap 2h08m · python train.py"))
        self.assertIn("… still running since 12:00", line)
        self.assertLess(len(line), 120)

    def test_a_lone_line_counts_its_calls_at_its_start(self):
        lone = task(kind="lone", n=1, t0=NOON + 960, t1=NOON + 961, label="Read x.py")
        self.assertEqual(pr.lone_line(lone, DAY), "  1 call at 12:16 · Read x.py")
        self.assertEqual(pr.lone_line(dict(lone, n=3), DAY), "  3 calls at 12:16 · Read x.py")
        quoted = dict(lone, label="Fix the loader", source="commit")
        self.assertEqual(pr.lone_line(quoted, DAY), '  1 call at 12:16 · "Fix the loader" [commit]')

    def test_a_tasks_gap_comes_before_it_and_its_number_is_its_place_in_the_list(self):
        tasks = [task(),
                 task(kind="lone", n=2, t0=NOON + 9000, t1=NOON + 9010, label="Read b.py",
                      gap={"s": 7000, "alive": []}),
                 task(t0=NOON + 15000, t1=NOON + 16000,
                      gap={"s": 5400, "alive": [[None, NOON, None]]})]
        self.assertEqual(pr.task_lines(tasks, DAY), [
            "  T0 12:00–12:50  50m00s  task  Run train.py",
            "  gap 1h56m",
            "  2 calls at 14:30 · Read b.py",
            "  gap 1h30m · a call still running since 12:00",
            "  T2 16:10–16:26  16m40s  task  Run train.py"])
        self.assertEqual(pr.task_lines([], DAY), [])


class JoinedCallsTest(unittest.TestCase):
    """A row of an agent that worked on two days is handed the calls its block is cut from."""

    def rows(self, recs, **thresholds):
        """`rows_of` a reducer fed `recs`, the rows of agent 100 only, the earliest first."""
        traj = fed(sorted(recs, key=lambda r: r["ts_epoch"]), configured(**thresholds))
        return sorted((r for r in pr.rows_of(traj) if r[1]["agent_pid"] == "100"),
                      key=lambda r: r[1]["first"])

    def two_days(self):
        """Agent 100 reads a file every 100 s from MIDNIGHT - 2950 to MIDNIGHT + 3050: thirty
        calls on the first day, thirty-one on the second."""
        return [root8(MIDNIGHT - 3500)] + reads(MIDNIGHT - 2950, 61, 201)

    def test_a_row_of_one_day_holds_its_own_calls_and_the_blocks_are_those(self):
        (s, row, mem, held), = self.rows([root8(NOON - 30)] + reads(NOON, 5, 201))
        self.assertEqual(len(held.block), 5)
        self.assertIs(held.own, held.block)
        self.assertEqual((row["n_calls"], held.hidden), (5, 0))
        self.assertNotIn("days", row["phases"])

    def test_a_joined_row_holds_the_sessions_calls_and_its_own(self):
        (_s1, first, _m1, one), (_s2, second, _m2, two) = self.rows(self.two_days())
        block = first["phases"]
        self.assertEqual(second["phases"], block)
        # the calls the block was cut from, whichever row asks, and each row's own
        self.assertEqual(one.block, two.block)
        self.assertEqual(len(one.block), block["n_calls"] - block["cut"])
        self.assertEqual((len(one.own), len(two.own)), (first["n_calls"], second["n_calls"]))
        self.assertEqual(len(one.own) + len(two.own), len(one.block))
        # the block's phases are placed on them without a miss
        for row, held in ((first, one), (second, two)):
            st = pr.row_stats(row["phases"], held.block, row["sid"], held.own)
            self.assertEqual((st["no_edit"], st["off"], st["unplaced"]), ([], [], 0))
        self.assertEqual(sum(pr.row_stats(block, one.block, r["sid"], h.own)["phases"]
                             for r, h in ((first, one), (second, two))),
                         len([p for p in block["list"] if p["kind"] != "idle"]))

    def test_an_open_call_is_judged_by_the_sessions_last_record_as_the_block_judges_it(self):
        # A tool shell (777) that never exits ran `ls`, `cat`, and `ls` just before midnight: busy
        # when its day's records end, and dense, but no loop. On its own it is a call still
        # running. Beside the next day's work the session's last record is hours later, and
        # nothing has run under it since: no call. A day row read by itself would say it is.
        day = [root8(MIDNIGHT - 3500), bash8(MIDNIGHT - 1500, 201, "cat src/a.py"),
               s8(MIDNIGHT - 120, 700, 777, "ls", "ls", duration_s=0.05, depth=2),
               s8(MIDNIGHT - 100, 701, 777, "cat", "cat x.txt", duration_s=0.05, depth=2),
               s8(MIDNIGHT - 80, 702, 777, "ls", "ls", duration_s=0.05, depth=2)]
        (_s, _row, _m, alone), = self.rows(day)
        self.assertEqual([bool(c[FLAGS] & OPEN) for c in alone.block], [False, True])
        both = self.rows(day + reads(MIDNIGHT + 1000, 5, 301))
        (_s1, first, _m1, one), (_s2, second, _m2, two) = both
        self.assertFalse(any(c[FLAGS] & OPEN for c in one.block))
        self.assertEqual((len(one.block), first["phases"]["n_calls"]), (6, 6))
        self.assertEqual((len(one.own), len(two.own)), (first["n_calls"], second["n_calls"]))
        self.assertEqual((one.hidden, two.hidden), (3, 0))
        for row, held in ((first, one), (second, two)):
            st = pr.row_stats(row["phases"], held.block, row["sid"], held.own)
            self.assertEqual((st["no_edit"], st["off"], st["unplaced"], st["open"]), ([], [], 0, 0))

    def test_the_calls_past_the_logs_cap_are_the_ones_the_block_leaves_out(self):
        # Neither day's log is over the cap of 40 calls, but the two together are: the block holds
        # the first forty and counts the rest as cut, and each row still holds its own
        (_s1, first, _m1, one), (_s2, second, _m2, two) = self.rows(
            self.two_days(), phases={"max_calls": 40})
        block = first["phases"]
        self.assertEqual((block["n_calls"], block["cut"]), (61, 21))
        self.assertEqual(len(one.block), 40)
        self.assertEqual((len(one.own), len(two.own)), (30, 31))
        for row, held in ((first, one), (second, two)):
            st = pr.row_stats(row["phases"], held.block, row["sid"], held.own)
            self.assertEqual((st["no_edit"], st["off"], st["unplaced"]), ([], [], 0))

    def test_a_call_still_running_is_counted_once_by_the_row_that_holds_it(self):
        # A tool shell that never exits and polls on a timer after midnight is a call still
        # running: one in the block, and among the calls of the row its children's records are in
        loop = [s8(MIDNIGHT + 1000 + 20 * i + 0.1, 800 + i, 777, "python", "python poll.py",
                   duration_s=0.05, depth=2) for i in range(30)]
        (_s1, first, _m1, one), (_s2, second, _m2, two) = self.rows(self.two_days() + loop)
        self.assertEqual(sum(1 for c in one.block if c[FLAGS] & OPEN), 1)
        self.assertEqual([sum(1 for c in held.own if c[FLAGS] & OPEN) for held in (one, two)],
                         [0, 1])
        self.assertEqual((len(one.own), len(two.own)), (first["n_calls"], second["n_calls"]))
        self.assertEqual(len(one.block), first["n_calls"] + second["n_calls"])


class EndToEndTest(unittest.TestCase):
    """A day-file written as the collector writes one, read by the CLI."""

    def records(self):
        t = NOON
        codex = dict(root=300, agent_type="codex")
        out = [root8(t - 30), root8(t - 30, root=300, command="codex", args="codex",
                                    agent_type="codex")]
        out += [bash8(t + 10 * k, 201 + k, "ls -la") for k in range(3)]
        out += [s8(t + 40, 301, 300, "awk", "awk -F: {print $1} /etc/passwd", **codex),
                s8(t + 41, 302, 300, "lsb_release", "/bin/sh /usr/bin/lsb_release -a", **codex)]
        return sorted(out, key=lambda r: r["ts_epoch"])

    def run_report(self, *extra):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, DAY))
            with open(os.path.join(d, DAY, "h1.exits.jsonl"), "w") as fh:
                for r in self.records():
                    fh.write(json.dumps(r) + "\n")
            out = io.StringIO()
            self.assertEqual(pr.main(["--roots", d, "--day", DAY] + list(extra), out=out), 0)
            return out.getvalue()

    def test_a_day_is_read_and_reported(self):
        text = self.run_report("--apid", "100", "--tree", "300")
        self.assertIn("== rows asked for: 2", text)
        self.assertIn("apid 100", text)
        self.assertIn("tree 300", text)
        self.assertIn("claude_code: bash 3", text)
        self.assertIn("codex: bare 1", text)
        self.assertIn("bare by shape: awk 1", text)
        self.assertIn("lsb_release 1", text)                        # Codex's own: housekeeping
        self.assertIn("rows: 2: 2 sessions (an agent process at the root) and 0 probes", text)
        self.assertIn("-- sessions: 2 rows, 2 with calls, 0 with none", text)
        self.assertIn("raw sessions: 2, held in one pass", text)
        self.assertIn("solid edits without an edit call: 0", text)
        self.assertIn("calls placed in no phase: 0", text)
        self.assertIn("pending join at the day's end: 0 entries (cap 50,000), 0 evicted, 0 refused "
                      "as an earlier process's (0 records);", text)        # final Important 5
        # every bound of the late-child join is reported (review Minor 5)
        self.assertIn("late children: where each process went, 5 noted (cap 50,000), 0 dropped; "
                      "placed where their parent went 0; placed nowhere 0; under a record with "
                      "no time 0; pushed with no start to check 0; pushed past 8 parents up 0; "
                      "entries started under a parent whose note may have been dropped, at most 0",
                      text)
        self.assertNotIn("!!", text)

    def test_a_pid_with_no_row_is_said(self):
        self.assertIn("!! no row for: 999", self.run_report("--apid", "999"))

    def test_a_day_is_held_whole_past_max_sessions(self):
        # Past the cap the reducer evicts by the wall clock: for a past day, every session.
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump({"reducers": {"trajectories": {"max_sessions": 1}}}, fh)
        try:
            text = self.run_report("--config", fh.name)
        finally:
            os.unlink(fh.name)
        self.assertIn("raw sessions: 2, held in one pass (max_sessions 1 is lifted", text)
        self.assertIn("rows: 2: 2 sessions", text)
        self.assertIn("sessions evicted 0", text)

    def test_a_row_lists_its_tasks_after_its_phases(self):
        text = self.run_report("--apid", "100")
        lines = text.split("\n== calls by kind")[0].splitlines()      # the rows asked for
        (first,) = [i for i, line in enumerate(lines) if line.startswith("  T0 ")]
        self.assertRegex(lines[first], r"^  T0 \d\d:\d\d–\d\d:\d\d  \S+  task  Read proj$")
        phase_lines = [i for i, line in enumerate(lines) if line.startswith("     ")]
        self.assertTrue(phase_lines)
        self.assertLess(max(phase_lines), first)
        self.assertEqual(task_block(text)[0], '  1 task · headline "proj"')

    def test_days_reads_more_than_one_day(self):
        later = [root8(NOON2 - 30, root=400)] + reads(NOON2, 3, 501, root=400)
        feed = {DAY: self.records(), DAY2: later}
        one = run_feed(feed, "--day", DAY, "--apid", "400")
        self.assertIn("!! no row for: 400", one)
        self.assertIn("phase report · %s · " % DAY, one)
        self.assertIn("rows: 2: 2 sessions", one)
        two = run_feed(feed, "--day", DAY, "--days", "2", "--apid", "400")
        self.assertNotIn("!!", two)
        self.assertIn("phase report · %s to %s · " % (DAY, DAY2), two)
        self.assertIn("== rows asked for: 1", two)
        self.assertIn("rows: 3: 3 sessions", two)               # the first day's two, and one more
        self.assertRegex(task_block(two)[1],
                         r"^  T0 10-29 \d\d:\d\d–10-29 \d\d:\d\d  \S+  task  Read ")

    def test_the_summary_counts_tasks_per_session_day(self):
        text = run_feed({DAY: stretches()}, "--day", DAY)
        line = ("   tasks per session-day: median 3, p90 3, max 3 (apid:100, 3 tasks over 3 h); "
                "rows with 2 or more active hours: 1 of 1")
        self.assertEqual(text.count(line), 2)               # over the sessions, and over every row
        self.assertIn("tasks named by the agent's words: 1 of 3 (33%): commit 1", text)
        self.assertIn("lone stretches: 0 in 0 rows", text)
        self.assertNotIn("!!", text)

    def test_the_summary_has_no_rate_where_no_row_was_busy_for_two_hours(self):
        text = run_feed({DAY: [root8(NOON + 570)] + reads(NOON + 600, 3, 201, every=10)},
                        "--day", DAY)
        self.assertIn("   tasks per session-day: median -, p90 -, max -; "
                      "rows with 2 or more active hours: 0 of 1", text)
        self.assertIn("tasks named by the agent's words: 0 of 1 (0%)", text)

    def test_the_summary_comes_back_as_a_dict_too(self):
        # the three stretches, and a fourth at 16:30 that commits again
        t = NOON + 1.5
        recs = stretches() + [bash8(t + 16200, 501, "git status"),
                              bash8(t + 16210, 502, 'git commit -m "Add the decoder"')]
        with tempfile.TemporaryDirectory() as d:
            write_feed(d, {DAY: recs})
            since, until = pr.day_bounds(DAY)
            traj, read = pr.reduce_day(pr.configmod.load(None, None), pr.day_dirs([d], DAY),
                                       since, until)
            summary = pr.report(io.StringIO(), traj, read, DAY)
        self.assertEqual(summary["tasks_per_day"], {"median": 4, "p90": 4, "max": 4, "rows": 1})
        self.assertEqual([summary[k] for k in ("tasks", "named", "lone", "tag_overflow")],
                         [4, 2, 0, 0])

    def test_the_thresholds_line_holds_the_task_parameters_too(self):
        (line,) = [x for x in self.run_report().splitlines() if x.startswith("thresholds: ")]
        for param in ('"idle_s": 300', '"gap_s": 3600', '"lone_calls": 8', '"headline_names": 3'):
            self.assertIn(param, line)
        # the configured ones, not the defaults
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump({"reducers": {"trajectories": {"tasks": {"gap_s": 7200}}}}, fh)
        try:
            (line,) = [x for x in self.run_report("--config", fh.name).splitlines()
                       if x.startswith("thresholds: ")]
        finally:
            os.unlink(fh.name)
        self.assertIn('"gap_s": 7200', line)
        self.assertIn('"idle_s": 300', line)

    def test_the_tag_sets_dropped_are_counted_beside_the_previews(self):
        self.assertIn("preview overflow 0; phase_tag_overflow 0; sessions evicted 0",
                      self.run_report())
        # room for the empty set only: each call that names something loses its tag set
        with mock.patch.object(trajmod, "MAX_TAGSETS", 1):
            text = run_feed({DAY: [root8(NOON - 30)] + reads(NOON, 3, 201)}, "--day", DAY)
        self.assertIn("preview overflow 0; phase_tag_overflow 3; sessions evicted 0", text)


def stretches():
    """Agent 100 at 12:00, 13:30, and 15:00: three reads, then a `git status` and a commit, then
    three reads. The stretches are an hour and a half apart, so each is a task, and the commit's
    subject names the second. Each lies within one clock hour: three active hours."""
    t = NOON + 1.5
    return ([root8(t - 30)]
            + [bash8(t + 10 * k, 201 + k, "cat src/a%d.py" % k) for k in range(3)]
            + [bash8(t + 5400, 301, "git status"),
               bash8(t + 5410, 302, 'git commit -m "Fix the loader"')]
            + [bash8(t + 10800 + 10 * k, 401 + k, "cat src/c%d.py" % k) for k in range(3)])


def run_feed(by_day, *args):
    """The text of the report of day-files `by_day` gives, written under a fresh root."""
    with tempfile.TemporaryDirectory() as d:
        write_feed(d, by_day)
        out = io.StringIO()
        if pr.main(["--roots", d] + list(args), out=out) != 0:
            raise AssertionError("the report did not run")
        return out.getvalue()


def task_block(text):
    """The lines a row prints for its tasks: those after the last phase line of the rows asked
    for."""
    lines = text.split("\n== calls by kind")[0].splitlines()
    last = max(i for i, line in enumerate(lines) if line.startswith("     "))
    return lines[last + 1:]


def twelve_reads_a_day():
    """Agent 100 reads a file every four minutes, twelve times from 12:20, on each of two days:
    calls in two clock hours each day, and a task a day a day apart."""
    return {DAY: [root8(NOON - 30)] + reads(NOON + 1200.5, 12, 201, every=240),
            DAY2: [root8(NOON2 - 30)] + reads(NOON2 + 1200.5, 12, 301, every=240)}


def across_midnight():
    """Agent 100 reads a file every four minutes from 22:00 until 02:00 the next day: one task, and
    one phase, which the midnight in the middle of them does not cut."""
    first = MIDNIGHT - 7200 + 0.5               # its exit: the call began on the hour
    recs = [root8(first - 30), root8(MIDNIGHT + 30)] + reads(first, 61, 201, every=240)
    return {DAY: [r for r in recs if r["ts_epoch"] < MIDNIGHT],
            DAY2: [r for r in recs if r["ts_epoch"] >= MIDNIGHT]}


class JoinedSessionReportTest(unittest.TestCase):
    """An agent that works past midnight has a row for each day, and they share one block."""

    def lines_of(self, by_day, *args):
        return run_feed(by_day, "--day", DAY, "--days", "2", "--apid", "100", *args).splitlines()

    def test_the_tasks_of_a_session_are_printed_once_under_its_first_row(self):
        lines = self.lines_of(twelve_reads_a_day())
        rows = [i for i, line in enumerate(lines) if line.startswith("-- apid 100 · ")]
        self.assertEqual(len(rows), 2)
        first_sid, second_sid = ("h1|%s|agent|apid:100" % d for d in (DAY, DAY2))
        self.assertIn(first_sid, lines[rows[0]])
        self.assertIn(second_sid, lines[rows[1]])
        at = {p: [i for i, line in enumerate(lines) if line.startswith(p)]
              for p in ("  T0 ", "  T1 ", "  gap ", "  (same session as ")}
        self.assertEqual({p: len(v) for p, v in at.items()},
                         {"  T0 ": 1, "  T1 ": 1, "  gap ": 1, "  (same session as ": 1})
        # both days' tasks, with the gap between them, under the first row
        self.assertLess(rows[0], at["  T0 "][0])
        self.assertLess(at["  T0 "][0], at["  gap "][0])
        self.assertLess(at["  gap "][0], at["  T1 "][0])
        self.assertLess(at["  T1 "][0], rows[1])
        self.assertIn('  2 tasks over 2 days (1 on %s, 1 on %s) · headline "proj"' % (DAY, DAY2),
                      lines)
        self.assertRegex(lines[at["  T1 "][0]], r"^  T1 10-29 12:20–10-29 13:04  \S+  task  ")
        self.assertRegex(lines[at["  gap "][0]], r"^  gap 23h\d\dm$")
        # the second row says where they are
        self.assertGreater(at["  (same session as "][0], rows[1])
        self.assertEqual(lines[at["  (same session as "][0]], "  (same session as %s)" % first_sid)

    def test_each_row_prints_the_phases_of_its_own_day_and_nothing_is_counted_twice(self):
        text = "\n".join(self.lines_of(twelve_reads_a_day()))
        phase_lines = [x for x in text.split("\n== calls by kind")[0].splitlines()
                       if x.startswith("     ")]
        # each day's phases, once: the first day's explore and the idle gap that begins on it,
        # then the second day's explore
        self.assertEqual(len(phase_lines), 3)
        self.assertTrue(phase_lines[0].startswith("     12:20:00–13:04:00"))
        self.assertIn("12 calls", phase_lines[0])
        self.assertIn("idle", phase_lines[1])
        self.assertTrue(phase_lines[2].startswith("     10-29 12:20:00–10-29 13:04:00"))
        self.assertIn("   records 12 · first ", text)         # a row's own records and calls
        self.assertIn("n_calls 12\n", text)
        self.assertIn("rows: 2: 2 sessions (an agent process at the root) and 0 probes (none, as "
                      "the page counts them); calls 24", text)
        self.assertIn("phases by kind: explore 2, idle 1\n", text)         # each counted once
        self.assertIn("tasks per session-day: median 1, p90 1, max 1 ", text)
        self.assertIn("tasks named by the agent's words: 0 of 2 (0%)", text)
        self.assertIn("calls placed in no phase: 0", text)
        self.assertNotIn("!!", text)

    def test_a_task_across_midnight_is_counted_on_the_day_it_begins(self):
        lines = self.lines_of(across_midnight())
        (t0,) = [x for x in lines if x.startswith("  T0 ")]
        self.assertRegex(t0, r"^  T0 22:00–10-29 02:00  4h00m  task  ")
        self.assertFalse([x for x in lines if x.startswith("  T1 ")])
        text = "\n".join(lines)
        # the first row holds the task and the second none: both have two active hours or more
        self.assertIn("tasks per session-day: median 0, p90 1, max 1 "
                      "(%s apid:100, 1 task over 2 h); rows with 2 or more active hours: 2 of 2"
                      % DAY, text)
        self.assertIn('  1 task over 2 days (1 on %s, 0 on %s) · headline "proj"' % (DAY, DAY2),
                      text)
        # and the one phase is the first day's, as it begins there
        self.assertIn("   0 phases (not idle) over 3 active hour(s): 0.0 per active hour", text)
        self.assertNotIn("!!", text)

    def test_commands_under_a_process_that_never_exited_are_the_row_that_held_them(self):
        # A tool shell (777) that never exits ran three commands just before midnight, busy but
        # no loop: beside the next day's work they are counted as housekeeping, and the row they
        # ran in has them, once
        by_day = {DAY: [root8(MIDNIGHT - 3500), bash8(MIDNIGHT - 1500, 201, "cat src/a.py"),
                        s8(MIDNIGHT - 120, 700, 777, "ls", "ls", duration_s=0.05, depth=2),
                        s8(MIDNIGHT - 100, 701, 777, "cat", "cat x.txt", duration_s=0.05, depth=2),
                        s8(MIDNIGHT - 80, 702, 777, "ls", "ls", duration_s=0.05, depth=2)],
                  DAY2: [root8(MIDNIGHT + 30)] + reads(MIDNIGHT + 1000, 5, 301)}
        text = "\n".join(self.lines_of(by_day))
        held = "   housekeeping 3 () · 3 under a process that never exited\n"
        self.assertEqual(text.count(held), 1)
        self.assertEqual(text.count("   housekeeping 0 ()\n"), 1)
        self.assertIn("\nhousekeeping: 3 commands; by shape (top 20): "
                      "under a process that never exited 3\n", text)
        self.assertIn("open phases: 0 in 0 rows", text)
        self.assertNotIn("!!", text)

    def test_the_first_row_that_is_asked_for_prints_the_tasks_of_its_session(self):
        # Only the second day's row is asked for, as its tree root is another: it is the first
        # of the session to print, so it prints the tasks and points nowhere
        by_day = twelve_reads_a_day()
        by_day[DAY2] = [dict(r, tree_root_pid=900) for r in by_day[DAY2] if r["pid"] != 100]
        text = run_feed(by_day, "--day", DAY, "--days", "2", "--tree", "900")
        self.assertIn("== rows asked for: 1", text)
        self.assertEqual(len([x for x in text.splitlines() if x.startswith("  T")]), 2)
        self.assertNotIn("(same session as", text)

    def test_the_rows_of_the_summary_name_their_days_when_there_are_several(self):
        text = "\n".join(self.lines_of(across_midnight()))
        self.assertRegex(text, r"most phases: %s apid:100 \d+ \(" % DAY)
        self.assertIn("most phases: apid:100 ", run_feed({DAY: stretches()}, "--day", DAY))


class GapAndLoneLinesTest(unittest.TestCase):
    """A gap between tasks, a call still running through it, and a lone stretch, read end to end."""

    def feed(self):
        """Agent 100 reads a file a minute for twelve minutes from noon, and starts a long run five
        seconds in, which it moves past. Two hours later it makes two calls and stops: they are a
        lone stretch, with the gap before them, and the run is going until 15:00."""
        return {DAY: [root8(NOON - 30)] + reads(NOON + 0.5, 12, 201, every=60)
                + [bash8(NOON + 10805, 900, "python long.py", duration_s=10800)]
                + reads(NOON + 7200.5, 2, 301, every=10)}

    def test_a_lone_stretch_is_a_line_of_its_own_with_the_gap_before_it(self):
        text = run_feed(self.feed(), "--day", DAY, "--apid", "100")
        header, t0, gap, lone = task_block(text)
        self.assertEqual(header, '  1 task, 1 lone stretch · headline "proj"')
        self.assertRegex(t0, r"^  T0 12:00–12:11  \S+  task  ")
        self.assertRegex(gap, r"^  gap 1h49m · long.py still running since 12:00$")
        self.assertRegex(lone, r"^  2 calls at 14:00 · ")
        self.assertIn("lone stretches: 1 in 1 row", text)


if __name__ == "__main__":
    unittest.main()
