"""The vendored tool taxonomy: it buckets, and it matches its original.

    cd dashboard && .venv/bin/python -m unittest discover -s tests -q
"""
import importlib.util
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rc_dashboard import tool_buckets                              # noqa: E402

# Where the research repo keeps the original, on the machines that have it.
ORIGINALS = [os.path.expanduser("~/rc_measurement/analyze/tool_bucket_lib.py"),
             os.path.expanduser("~/analyze/tool_bucket_lib.py")]

TOOLS = ["python", "python3", "python3.11", "wandb-service(", "git", "git-remote-https", "squeue",
         "sacct", "sbatch", "srun", "ps", "pgrep", "sleep", "lsb_release", "nvidia-smi",
         "cpuUsage.sh", "rg", "find", "grep", "make", "gcc", "cc1plus", "cargo", "curl",
         "rsync", "cat", "sed", "vim", "zsh", "bash", "conda", "pip", "node", "codex",
         "claude", "2.1.295-8b2928c", "bwrap", "", "some-unknown-tool"]


class BucketTest(unittest.TestCase):

    def test_tools_land_in_real_buckets_not_all_other(self):
        got = {t: tool_buckets.bucket(t) for t in ("python3", "git", "squeue", "ps", "rg", "make")}
        self.assertEqual(got, {"python3": "interpreter", "git": "git", "squeue": "slurm-query",
                               "ps": "poll/probe", "rg": "search", "make": "build"})

    def test_the_vendored_copy_matches_the_original_where_it_exists(self):
        found = [p for p in ORIGINALS if os.path.exists(p)]
        if not found:
            self.skipTest("the research repo's analyze/ is not on this machine")
        spec = importlib.util.spec_from_file_location("tool_bucket_lib_original", found[0])
        orig = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(orig)
        self.assertEqual(tuple(orig.BUCKETS), tuple(tool_buckets.BUCKETS))
        for t in TOOLS:
            with self.subTest(tool=t):
                self.assertEqual(orig.bucket(t), tool_buckets.bucket(t))


if __name__ == "__main__":
    unittest.main()
