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
        r = sub(0, "srun -p test python x.py", exit_code=1,
                captured_tail="\x1b[1;31mTraceback\x1b[0m\nValueError: 3 is not 4\n")
        self.assertEqual(outcome(r), "no_id")
        self.assertEqual(reason(r), "ValueError: N is not N")   # escapes gone, numbers masked

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
