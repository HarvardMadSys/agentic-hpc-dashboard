"""The Slurm reducer: what came back from a submission, polling loops, the fold.

    cd dashboard && .venv/bin/python -m unittest tests.test_slurm -q
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rc_dashboard.reducers.slurm import SlurmReducer, array_tasks, outcome, reason  # noqa: E402

T0 = 1791518400.0          # 2026-10-09 00:00 EDT


def sub(t, args, apid=100, user="alice", cls="agent", **kw):
    r = {"event": "submit", "_a3": cls, "_ts_epoch": t, "ts": "2026-10-09",
         "host": "h1", "user": user, "agent_type": "claude_code" if cls == "agent" else None,
         "_kt": "apid", "_kv": str(apid), "tree_root_pid": 1, "tool": args.split()[0],
         "args": args, "exit_code": 0, "job_id": None, "job_ids": None}
    r.update(kw)
    return r


def ex(t, comm, apid=100, user="alice", cls="agent", **kw):
    r = {"event": "exit", "_a3": cls, "_ts_epoch": t, "host": "h1", "user": user,
         "agent_type": "claude_code" if cls == "agent" else None, "_kt": "apid",
         "_kv": str(apid), "tree_root_pid": 1, "_command": comm, "exit_code": 0}
    r.update(kw)
    return r


class FoldTo:
    """Every key folds into the agent at pid 100."""

    def owner(self, host, pid, root):
        return (100, "self" if pid == 100 else "ancestor")


class Outcomes(unittest.TestCase):

    def test_what_came_back(self):
        self.assertEqual(outcome(sub(0, "sbatch --parsable x.sh", job_id=5, job_ids=[5])), "job")
        self.assertEqual(outcome(sub(0, "sbatch --test-only x.sh")), "dry_run")
        self.assertEqual(outcome(sub(0, "srun --jobid 5 --overlap hostname")), "step")
        refused = sub(0, "sbatch x.sh", exit_code=1, captured_tail=(
            "sbatch: error: QOSMaxSubmitJobPerUserLimit\n"
            "sbatch: error: Batch job submission failed: Job violates accounting/QOS policy\n"))
        self.assertEqual(outcome(refused), "rejected")
        self.assertEqual(reason(refused), "sbatch: error: QOSMaxSubmitJobPerUserLimit")

    def test_a_program_that_ran_and_failed_is_not_a_refusal(self):
        # srun relays the program's output, then says how it ended in an error
        # line of its own; words of the program's are not the scheduler's either
        for tail in ("\x1b[1;31mTraceback\x1b[0m\nValueError: 3 is not 4\n"
                     "srun: error: holy7c24101: task 0: Exited with exit code 1\n",
                     "srun: error: holy7c24101: tasks 0-3: Killed\n",
                     "PermissionError: [Errno 13] Permission denied: '/n/x'\n"
                     "CUDA not available\n"
                     "srun: error: holy7c24101: task 0: Exited with exit code 1\n",
                     # the tail's first line cut (it keeps 300 characters): numpy's
                     # words, which a refusal phrase matched anywhere would take
                     "ryMemoryError: Unable to allocate 2.00 GiB for an array with shape "
                     "(100000, 2680) and data type float64\n"
                     "srun: error: holy7c24101: task 0: Exited with exit code 1\n"):
            r = sub(0, "srun -p test python x.py", exit_code=1, captured_tail=tail)
            self.assertEqual(outcome(r), "no_id", tail)

    def test_an_srun_or_salloc_that_got_no_allocation_was_refused(self):
        # its reason the scheduler's first error line, as sbatch's is
        for args, tail, why in (
                ("srun -p gpu -A x python x.py",
                 "srun: error: QOSMaxSubmitJobPerUserLimit\n"
                 "srun: error: Unable to allocate resources: Job violates accounting/QOS "
                 "policy (job submit limit, user's size and/or time limits)\n",
                 "srun: error: QOSMaxSubmitJobPerUserLimit"),
                ("salloc -p nope", "salloc: error: Job submit/allocate failed: Invalid "
                 "partition name specified\n", "salloc: error: Job submit/allocate failed: "
                 "Invalid partition name specified"),
                # an older salloc's words for the same
                ("salloc -p gpu", "salloc: error: Failed to allocate resources: Requested node "
                 "configuration is not available\n", "salloc: error: Failed to allocate "
                 "resources: Requested node configuration is not available"),
                ("salloc --immediate=30 -p gpu", "salloc: Pending job allocation 4242\n"
                 "salloc: job 4242 queued and waiting for resources\n"
                 "salloc: error: Unable to allocate resources: Requested nodes are busy\n",
                 "salloc: error: Unable to allocate resources: Requested nodes are busy"),
                # srun inside an allocation, SLURM_JOB_ID from the environment
                ("srun -n 64 python x.py", "srun: error: Unable to create step for job 4242: "
                 "More processors requested than permitted\n", "srun: error: Unable to create "
                 "step for job N: More processors requested than permitted"),
                ("srun python x.py", "srun: error: Unable to confirm allocation for job 4242: "
                 "Invalid job id specified\nsrun: Check SLURM_JOB_ID environment variable. "
                 "Expired or invalid job 4242\n", "srun: error: Unable to confirm allocation "
                 "for job N: Invalid job id specified")):
            r = sub(0, args, exit_code=1, captured_tail=tail)
            self.assertEqual(outcome(r), "rejected", tail)
            self.assertEqual(reason(r), why)

    def test_a_reason_has_no_escapes_and_no_numbers(self):
        r = sub(0, "sbatch x.sh", exit_code=1,
                captured_tail="\x1b[1;31mTraceback\x1b[0m\nValueError: 3 is not 4\n")
        self.assertEqual(reason(r), "ValueError: N is not N")

    def test_reason_is_masked(self):
        r = sub(0, "sbatch x.sh", exit_code=1,
                captured_tail="sbatch: error: denied for TOKEN=sk-ant-api03-abcdefghijklmnopqrstuvwxyz\n")
        self.assertNotIn("abcdefghijklmnop", reason(r))

    def test_array_tasks(self):
        self.assertEqual(array_tasks("0-99%10"), 100)
        self.assertEqual(array_tasks("1,3,5-7"), 5)
        self.assertEqual(array_tasks("0-10:2"), 6)
        self.assertIsNone(array_tasks("abc"))


class Sessions(unittest.TestCase):

    def run_reducer(self, recs, procs=None):
        red = SlurmReducer({}, ["agent", "human", "ide"], window_s=86400, procs=procs)
        for r in recs:
            red.feed(r)
        return red.result()

    def test_a_polling_loop_and_the_fold(self):
        recs = [sub(T0, "sbatch --parsable --dependency=afterok:1 a.sh", apid=100,
                    job_id=1, job_ids=[1], partition="gpu", gpus=2, time_limit_s=3600),
                sub(T0 + 1, "sbatch --parsable a.sh", apid=200, job_id=2, job_ids=[2]),
                sub(T0 + 2, "sbatch --test-only a.sh", apid=200)]
        # a tool call's shell (apid 200) polls: squeue + sacct together, every 30 s, 20 min
        for i in range(41):
            recs.append(ex(T0 + 60 + 30 * i, "squeue", apid=200))
            recs.append(ex(T0 + 60.5 + 30 * i, "sacct", apid=200))
        recs.append(ex(T0 + 4000, "scancel", apid=100))
        out = self.run_reducer(recs, FoldTo())
        rows = [r for r in out["sessions"] if r["cls"] == "agent"]
        self.assertEqual(len(rows), 1)                  # both keys are one session
        r = rows[0]
        self.assertEqual(r["keys"], 2)
        self.assertEqual(r["jobs"], 2)
        self.assertEqual(r["dry_runs"], 1)
        self.assertEqual(r["queries"], 82)
        self.assertEqual(r["polls"], 41)                # a squeue+sacct pair is one poll
        self.assertTrue(r["loop"]["polling"])
        self.assertEqual(r["loop"]["every_s"], 30.0)
        self.assertEqual(r["loop"]["span_s"], 1200.0)
        self.assertEqual(r["queries_per_job"], 41.0)
        self.assertEqual(r["flags"], {"parsable": 2, "dependency": 1})
        self.assertEqual(r["gpu_h"], 2.0)
        self.assertEqual(r["cancels"], 1)
        # the latest job's command, though a query key's record is newer
        self.assertEqual(r["args"], "sbatch --parsable a.sh")
        a = out["by_class"]["agent"]
        self.assertEqual(a["polling_sessions"], 1)
        self.assertEqual(a["flags_pct"]["parsable"], 100.0)

    def test_a_key_the_cut_falls_in_is_masked_whole(self):
        # A row keeps 300 characters of a command line. Cut first, a key that
        # began just before the cut was too short to read as one, and its first
        # characters were served, as they were after the page's own masking.
        from rc_dashboard import redact
        args = "sbatch --wrap " + "x" * 273 + " sk-" + "k" * 30
        recs = [sub(T0, args, job_id=1, job_ids=[1]),
                sub(T0 + 1, args, exit_code=1, captured_tail="sbatch: error: invalid account\n")]
        out = self.run_reducer(recs, FoldTo())
        row = [r for r in out["sessions"] if r["cls"] == "agent"][0]
        self.assertIn(redact.MASK, row["args"])
        self.assertEqual([f["eg"]["args"] for f in out["rejections"]], [row["args"]])
        self.assertNotIn("sk-k", repr(redact.payload(out)))

    def test_a_failed_program_is_no_refusal_in_the_class_rates(self):
        tail = ("Traceback (most recent call last):\nValueError: 3 is not 4\n"
                "srun: error: holy7c24101: task 0: Exited with exit code 1\n")
        out = self.run_reducer([
            sub(T0, "srun -p test python x.py", user="erin", cls="human", exit_code=1,
                captured_tail=tail),
            sub(T0 + 5, "sbatch --array=0-99 job.sh", user="erin", cls="human",
                job_id=5, job_ids=[5])])
        h = out["by_class"]["human"]
        self.assertEqual((h["asked"], h["rejected"], h["rejected_pct"]), (2, 0, 0.0))
        self.assertEqual(out["rejections"], [])

    def test_without_the_fold_each_key_is_its_own_row(self):
        recs = [sub(T0, "sbatch a.sh", apid=100, job_id=1, job_ids=[1]),
                ex(T0 + 5, "squeue", apid=200)]
        out = self.run_reducer(recs)
        self.assertEqual(len([r for r in out["sessions"] if r["cls"] == "agent"]), 2)

    def test_burst_and_shapes(self):
        recs = [sub(T0 + i, "sbatch --parsable run_%d.sh" % i, job_id=i, job_ids=[i])
                for i in range(30)]
        recs += [sub(T0 + 120, "sbatch --wrap 'python train.py'", job_id=99, job_ids=[99])]
        r = self.run_reducer(recs)["sessions"][0]
        self.assertEqual(r["peak_per_min"], 30)
        self.assertEqual(r["shapes"], 2)               # run_N.sh is one shape, --wrap another

    def test_people_are_one_row_per_user_and_refusals_are_listed(self):
        recs = [sub(T0, "sbatch a.sh", cls="human", user="bob", job_id=1, job_ids=[1]),
                sub(T0 + 1, "sbatch b.sh", cls="human", user="bob", exit_code=1,
                    captured_tail="sbatch: error: invalid partition specified: gpu_x\n"),
                ex(T0 + 2, "squeue", cls="human", user="bob")]
        out = self.run_reducer(recs)
        rows = [r for r in out["sessions"] if r["cls"] == "human"]
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["session"])
        h = out["by_class"]["human"]
        self.assertEqual((h["jobs"], h["rejected"], h["asked"]), (1, 1, 2))
        self.assertEqual(out["rejections"][0]["why"],
                         "sbatch: error: invalid partition specified: gpu_x")

    def test_a_bin_with_no_record_is_a_gap_not_zero(self):
        recs = [sub(T0, "sbatch a.sh", job_id=1, job_ids=[1]),
                ex(T0 + 3 * 600, "squeue")]
        s = self.run_reducer(recs)["series"]
        q = s["by_class"]["agent"]["queries"]
        self.assertEqual(q[0], 0)                      # a job landed: a measured 0
        self.assertIsNone(q[1])                        # nothing at all: a gap
        self.assertEqual(q[3], 1)


class Abnormal(unittest.TestCase):
    """What makes a session's activity at the scheduler abnormal: its own count
    against each rule's threshold, read from the config, served with the rules."""

    def result(self, recs, cfg=None):
        red = SlurmReducer(cfg or {}, ["agent", "human", "ide"], window_s=86400)
        for r in sorted(recs, key=lambda r: r["_ts_epoch"]):
            red.feed(r)
        return red.result()

    @staticmethod
    def session(apid, n):
        """Records for session `apid` that reach each rule with `n` of it: the
        rule's threshold, or one short of it."""
        t = T0 + 3600 * apid               # each session in its own hour, minute-aligned
        return {
            # checks 30 s apart: n + 1 of them span n * 30 s, so 20 make 600 s
            "polling": [ex(t + 30 * i, "squeue", apid=apid) for i in range(n + 1)],
            "query_burst": [ex(t + i, "squeue", apid=apid) for i in range(n)],
            "submit_burst": [sub(t + i, "sbatch job.sh", apid=apid, job_id=i, job_ids=[i])
                             for i in range(n)],
            "refused": [sub(t + i, "sbatch bad.sh", apid=apid, exit_code=1,
                            captured_tail="sbatch: error: QOSMaxSubmitJobPerUserLimit\n")
                        for i in range(n)],
            "cancels": [ex(t + 120 * i, "scancel", apid=apid) for i in range(n)],
            "failed_runs": [ex(t + 120 * i, "srun", apid=apid, exit_code=1) for i in range(n)],
        }

    def test_each_rule_at_its_threshold_and_one_short(self):
        from rc_dashboard.reducers.slurm import ABNORMAL, ABNORMAL_RULES
        at = dict(ABNORMAL, polling=20)        # 20 gaps of 30 s: 10 min
        for i, rule in enumerate(ABNORMAL_RULES):
            with self.subTest(rule=rule):
                on = self.session(10 + i, at[rule])[rule]
                off = self.session(20 + i, at[rule] - 1)[rule]
                rows = {r["session"]: r for r in self.result(on + off)["sessions"]}
                self.assertEqual(rows["h1:%d" % (10 + i)]["abnormal"], [rule])
                self.assertEqual(rows["h1:%d" % (20 + i)]["abnormal"], [])

    def test_the_busiest_minute_of_queue_checks_is_a_clock_minute(self):
        recs = [ex(T0 + 50 + i * 0.5, "squeue") for i in range(40)]      # 20 s across a minute
        r = self.result(recs)["sessions"][0]
        self.assertEqual(r["queries_peak_per_min"], 20)
        self.assertEqual(r["abnormal"], [])

    def test_thresholds_come_from_the_config_and_are_served(self):
        recs = self.session(5, 1)["refused"]
        self.assertEqual(self.result(recs)["sessions"][0]["abnormal"], [])
        out = self.result(recs, {"reducers.slurm": {"abnormal": {"refused": 1}}})
        self.assertEqual(out["sessions"][0]["abnormal"], ["refused"])
        rules = {r["id"]: r for r in out["abnormal"]["rules"]}
        self.assertEqual(rules["refused"], {"id": "refused", "min": 1})
        self.assertEqual(rules["polling"], {"id": "polling", "gap_s": 180.0, "min_s": 600.0})
        self.assertEqual(list(rules), ["polling", "query_burst", "submit_burst", "refused",
                                       "cancels", "failed_runs"])

    def test_each_class_counts_its_sessions_by_rule_zero_included(self):
        s = self.session(7, 3)
        recs = s["refused"] + [ex(T0 + 60 * i, "scancel", cls="human", user="bob")
                               for i in range(12)]
        out = self.result(recs)
        a, h = out["by_class"]["agent"], out["by_class"]["human"]
        self.assertEqual(a["abnormal_sessions"], 1)
        self.assertEqual(a["abnormal_by"]["refused"], 1)
        self.assertEqual(a["abnormal_by"]["polling"], 0)
        self.assertEqual(h["abnormal_sessions"], 1)
        self.assertEqual(h["abnormal_by"]["cancels"], 1)
        self.assertEqual(set(h["abnormal_by"]), set(a["abnormal_by"]))
        self.assertEqual(out["abnormal"]["sessions"], 2)

    def test_an_abnormal_session_is_served_however_quiet(self):
        from unittest import mock
        from rc_dashboard.reducers import slurm
        # four busy sessions with nothing abnormal -- 50 checks 7 s apart: under 10
        # min, and 9 a minute at most -- and two quieter ones that broke a rule
        recs = [ex(T0 + 7 * i + 600 * k, "squeue", apid=k) for k in range(4) for i in range(50)]
        recs += self.session(50, 3)["refused"] + self.session(51, 3)["refused"]
        with mock.patch.object(slurm, "ROWS", 2):
            out = self.result(recs)
        self.assertEqual(out["sessions_total"], 6)
        self.assertEqual(out["sessions_busiest"], 2)
        self.assertEqual([r["session"] for r in out["sessions"][2:]], ["h1:50", "h1:51"])
        self.assertEqual((out["abnormal"]["sessions"], out["abnormal"]["served"]), (2, 2))
        # past ABNORMAL_ROWS they are counted, not served
        with mock.patch.object(slurm, "ROWS", 2), mock.patch.object(slurm, "ABNORMAL_ROWS", 1):
            out = self.result(recs)
        self.assertEqual(len(out["sessions"]), 3)
        self.assertEqual((out["abnormal"]["sessions"], out["abnormal"]["served"]), (2, 1))


class Merge(unittest.TestCase):
    """A window is merged from saved hour states (rc_dashboard/hours.py): two
    halves merged in time order must be the reducer one pass builds."""

    def stream(self):
        recs = []
        for i in range(60):
            recs.append(sub(T0 + 37 * i, "sbatch --parsable -p gpu run_%d.sh" % i,
                            apid=100 + i % 3, job_id=i, job_ids=[i], partition="gpu",
                            gpus=1 + i % 2, time_limit_s=600 * (1 + i % 5)))
            if i % 9 == 4:
                recs.append(sub(T0 + 37 * i + 1, "sbatch bad.sh", apid=100, exit_code=1,
                                captured_tail="sbatch: error: invalid partition %d\n" % i))
        # a polling loop running straight through the cut
        for i in range(200):
            recs.append(ex(T0 + 1000 + 10 * i, "squeue", apid=200))
        recs.append(ex(T0 + 1500, "scancel", apid=100))
        recs.append(ex(T0 + 2100, "srun", apid=101, exit_code=1))
        recs += [sub(T0 + 3 + i, "sbatch x.sh", cls="human", user="bob",
                     job_id=900 + i, job_ids=[900 + i]) for i in range(5)]
        return sorted(recs, key=lambda r: r["_ts_epoch"])

    def reducer(self, recs, bin_s=None):
        red = SlurmReducer({}, ["agent", "human", "ide"], window_s=86400, procs=FoldTo())
        if bin_s:
            red.bin_s = bin_s
        for r in recs:
            red.feed(r)
        return red

    def test_two_halves_merged_are_one_pass(self):
        recs = self.stream()
        cut = T0 + 1700
        for bin_s in (None, 60):           # the later half at an hour state's width
            with self.subTest(bin_s=bin_s):
                one = self.reducer(recs).result()
                a = self.reducer([r for r in recs if r["_ts_epoch"] < cut], bin_s)
                b = self.reducer([r for r in recs if r["_ts_epoch"] >= cut], bin_s)
                if bin_s:
                    whole = self.reducer([])
                    whole.merge(a)
                    a = whole
                a.merge(b)
                self.assertEqual(a.result(), one)

    def test_a_shape_is_the_same_in_every_process(self):
        # Not `hash()`, which Python salts per process: a shape saved by one
        # run is compared with the next run's.
        from rc_dashboard.reducers.slurm import shape
        self.assertEqual(shape("sbatch --parsable -p gpu job12.sh"), 4645932230307576293)
        self.assertEqual(shape("sbatch --parsable -p gpu job12.sh"),
                         shape("sbatch --parsable -p gpu job99.sh"))


if __name__ == "__main__":
    unittest.main()
