"""The built-in dangerous-command rules read words, and every rule reads a line in bounded time.

As regexes over the whole line the built-in rules fired on commands that are none of what they
say, and two took seconds on one line never seen before -- inside every window build and hour
reduction, under the aggregator's lock. A cluster's own patterns read a bounded part of a line.

    cd dashboard && uv run python -m unittest discover -s tests -p 'test_risk_tokens.py' -v
"""
import json
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rc_dashboard import config as configmod, redact          # noqa: E402
from rc_dashboard.reducers import risk                        # noqa: E402
from rc_dashboard.reducers.risk import RiskReducer            # noqa: E402

BOUND = 0.25        # seconds; what each shape took on develop is in its test


class Cfg:
    def __init__(self, conf=None):
        self.conf = conf or {}

    def get(self, k, d=None):
        return self.conf if k == "risk" else d


def fired(ec, args, conf=None):
    """The dangerous rules a fresh reducer finds on one command line, by id."""
    r = RiskReducer(Cfg(conf), ["agent", "human"])
    return [r.rules.dg[i][0] for i in r._matches(ec, args)[0]]


MISFIRES = [                                 # the review's: none of them is what the rule says
    ("rm", "rm notes-draft.txt", "rm_rf_shared"),
    ("rm", "rm run-perf.log", "rm_rf_shared"),
    ("python", "python /home/dave/go/analysis.py", "build_login"),
    ("ls", "ls /usr/local/go/bin", "build_login"),
    ("ssh", "ssh git@github.com git-upload-pack 'nodejs/node.git'", "ssh_compute"),
    ("git", "git push --force-with-lease --force-if-includes origin main", "git_force_push"),
    ("grep", "grep -n def /home/dave/.venv/lib/python3.11/site-packages/pip/_internal/commands/"
             "install.py", "pip_install"),
    ("chmod", "chmod 600 /scratch/job.777.out", "chmod_777"),
]
HITS = [                                     # (command, argv, the rule it is)
    ("rm", "rm -rf /scratch/shared/old", "rm_rf_shared"), ("rm", "rm -fr x", "rm_rf_shared"),
    ("rm", "/bin/rm -Rf x", "rm_rf_shared"), ("rm", "rm -r -f x", "rm_rf_shared"),
    ("rm", "rm --recursive --force x", "rm_rf_shared"), ("RM", "RM -Rf /x", "rm_rf_shared"),
    ("git", "git push --force", "git_force_push"),
    ("git", "git push origin main -f", "git_force_push"),
    ("git", "git -C /w push -uf origin x", "git_force_push"),
    ("git", "git push --force-with-lease --force", "git_force_push"),
    ("pip", "/usr/bin/python3 /home/u/.venv/bin/pip install torch", "pip_install"),
    ("pip3.11", "pip3.11 install x", "pip_install"),
    ("python3", "python3 /home/u/.local/bin/pip install x", "pip_install"),
    ("conda", "conda env create -f e.yml", "conda_install"), ("micromamba", "micromamba install x",
                                                               "conda_install"),
    ("make", "make -j8", "build_login"), ("g++", "g++ -O3 a.cc", "build_login"),
    ("gcc-12", "gcc-12 a.c", "build_login"), ("go", "go build ./...", "build_login"),
    ("ssh", "ssh holy7c12345", "ssh_compute"), ("ssh", "ssh -p 22 u@holygpu7c1 ls", "ssh_compute"),
    ("ssh", "ssh -vp 22 c123", "ssh_compute"), ("ssh", "ssh -f holy7c1 sleep 1", "ssh_compute"),
    ("ssh", "ssh ssh://u@node7:22", "ssh_compute"), ("python3", "python3 -m http.server 8000",
                                                     "server_on_login"),
    ("jupyter", "jupyter lab --no-browser", "server_on_login"),
    ("curl", "curl -sSLO https://y", "bulk_download"),
    ("curl", "curl -fo out.txt https://y", "bulk_download"),       # -f is --fail: no value
    ("wget", "wget -O out https://x", "bulk_download"),
    ("hf", "hf download m", "hf_download"),
    ("chrome", "/opt/google/chrome/chrome --headless --dump-dom x", "headless_browser"),
    ("node", "node /x/node_modules/playwright/cli.js test", "headless_browser"),
    ("chmod", "chmod -R 777 d", "chmod_777"), ("nvidia-smi", "nvidia-smi -L", "nvidia_smi_login"),
    ("nvcc", "nvcc x.cu", "nvcc_login"),
]
QUIET = [                                    # what the words say is not the rule's
    ("rm", "rm --force x"), ("rm", "rm -f x"), ("rm", "rm -r x"), ("rm", "rm -- -rf"),
    ("git", "git commit -m push -f"), ("git", "git push -o ci.skip origin x"),
    ("pip", "pip uninstall x"), ("python", "python /opt/conda/lib/install.py"),
    ("ls", "ls /usr/lib/gcc/x86_64-linux-gnu"), ("ssh-keygen", "ssh-keygen -f node.key"),
    ("curl", "curl https://example.org/my-old-file"), ("chmod", "chmod 600 777"),
    ("ls", "ls /usr/bin/nvidia-smi"), ("jupyter", "jupyter kernelspec list"),
    # a flag that takes a value: what follows it in the word is the value, not more flags
    ("curl", "curl -XPOST https://example.com"), ("curl", "curl -Afoo https://example.com"),
    ("curl", "curl -HOrigin:x https://example.com"),
    ("curl", "curl -Fupload=@photo.jpg https://example.com/up"),     # -F, not -f: a value
    ("wget", "wget -UMozilla/5.0 https://x/y"), ("git", "git push -ofoo origin x"),
]


class WordsTest(unittest.TestCase):

    def setUp(self):
        risk._RULESETS.clear()

    tearDown = setUp

    def test_the_misfires_are_gone(self):
        for ec, args, rid in MISFIRES:
            self.assertNotIn(rid, fired(ec, args), args)

    def test_what_the_rules_are_for_still_fires(self):
        for ec, args, rid in HITS:
            self.assertIn(rid, fired(ec, args), args)

    def test_words_that_are_not_the_rule_do_not_fire(self):
        for ec, args in QUIET:
            self.assertEqual(fired(ec, args), [], args)

    def test_the_script_python_runs_is_read_past_pythons_options(self):
        """Python's flags, alone or grouped, and the values of `-W`, `-X` and
        `--check-hash-based-pycs`, attached or in the next word, come before the script."""
        for args in ("python3 -u /usr/bin/pip install torch", "python3 -uB /x/pip install y",
                     "python3 -E -s -I /x/pip install y", "python3 -OO /x/pip install y",
                     "python3 -W ignore /x/pip install y", "python3 -Wignore /x/pip install y",
                     "python3 -X dev /x/pip install y", "python3 -Xdev -u /x/pip install y",
                     "python3 --check-hash-based-pycs always /x/pip install y",
                     "python3 -- /x/pip install y"):
            self.assertEqual(fired("python3", args), ["pip_install"], args)
        self.assertEqual(fired("python3", "python3 -u /opt/venv/bin/tensorboard"),
                         ["server_on_login"])
        # `-c` and `-m` end the options: what follows is code or a module, and no script (pip's
        # module is pip all the same: test_pip_run_as_a_module_is_pip)
        self.assertEqual(fired("python3", "python3 -u -m http.server 8000"), ["server_on_login"])
        self.assertEqual(fired("python3", "python3 -c /x/pip install y"), [])
        self.assertEqual(fired("python3", "python3 -m pip install torch"), ["pip_install"])

    def test_pip_run_as_a_module_is_pip(self):
        """`python -m pip` runs what the script pip runs: pip named by `-m`, in the next word or
        the rest of its word, past the interpreter's options. After the script, or `-c` and its
        code, `-m pip` is theirs; and pipx is another tool."""
        for args in ("python -m pip install x", "python3 -m pip3 install x",
                     "python3 -u -m pip install x", "python -I -m pip install x",
                     "python3 -mpip install x"):
            self.assertEqual(fired(args.split()[0], args), ["pip_install"], args)
        for args in ("python -m pipx install x", "python run.py -m pip install x",
                     'python -c "import pip" -m pip install x'):
            self.assertEqual(fired("python", args), [], args)

    def test_rm_rf_needs_both_flags(self):
        self.assertEqual(fired("rm", "rm --force -v x"), [])
        self.assertEqual(fired("rm", "rm -v -R x --force"), ["rm_rf_shared"])

    def test_every_gate_lets_through_what_its_rule_reads(self):
        for i, d in enumerate(risk._DANGEROUS):
            for ec, args, _ in HITS + MISFIRES:
                line = risk._Line(ec, args)
                if d[-1].test(line):
                    self.assertTrue(risk._passes(risk._DG_GATE[i], line.low), (d[0], args))

    def test_a_rule_reads_the_same_through_search(self):
        for ec, args, _ in HITS:
            want = fired(ec, args)
            got = [d[0] for d in risk._DANGEROUS if d[-1].search("%s %s" % (ec, args))]
            self.assertEqual(got, want, args)

    def test_the_page_says_what_each_rule_reads(self):
        rows = risk.RuleSet({}).summary()["dangerous"]
        ssh = next(r for r in rows if r["id"] == "ssh_compute")
        self.assertIn("holy|bos|node|gpu", ssh["pattern_resolved"])
        self.assertTrue(all(r["pattern_resolved"] for r in rows))


class BoundedTest(unittest.TestCase):

    def setUp(self):
        risk._RULESETS.clear()
        self.extra = list(redact._EXTRA)

    def tearDown(self):
        risk._RULESETS.clear()
        redact._EXTRA[:] = self.extra

    def timed(self, ec, args, conf=None):
        t0 = time.perf_counter()
        out = fired(ec, args, conf)
        return time.perf_counter() - t0, out

    def test_the_built_in_rules_on_crafted_lines(self):
        for args, took in (("/git " * 400 + "push " * 400, "2.8 s"),
                           ("/rm " * 400 + "-" + "r" * 2400, "12.5 s")):
            dt, out = self.timed("true", args)
            self.assertEqual(out, [])
            self.assertLess(dt, BOUND, took)

    def test_a_clusters_pattern_reads_4096_characters(self):
        conf = {"dangerous": {"add": [{"id": "slow", "pattern": r"(?:^|/)x\b[^\n]*\sy\b"},
                                      {"id": "far", "pattern": r"\bneedle\b"}]}}
        dt, out = self.timed("true", "/x " * 13000, conf)     # 39 KB, never a match
        self.assertEqual(out, [])
        self.assertLess(dt, BOUND)                            # about 2 s read whole
        self.assertEqual(fired("tool", "tool " + "a " * 3000 + "needle", conf), [])
        self.assertEqual(fired("tool", "tool " + "a " * 1000 + "needle", conf), ["far"])

    def test_a_secret_pattern_reads_4096_characters(self):
        r = RiskReducer(Cfg(), ["agent", "human"])
        t0 = time.perf_counter()
        sec = r._matches("mysql", "/mysql " * 3000)[1]        # 21 KB, never a match
        self.assertEqual(sec, ())
        self.assertLess(time.perf_counter() - t0, BOUND)

    def test_the_shipped_example_reads_a_crafted_line_in_time(self):
        """example.json's rm override took 17 s on this line, as it was shipped."""
        path = Path(__file__).resolve().parents[1] / "risk-profiles" / "example.json"
        with open(path) as fh:
            conf = configmod.strip_comments(json.load(fh))
        dt, out = self.timed("rm", "rm " + "/rm " * 400 + " -" + "r" * 2400, conf)
        self.assertEqual(out, [])
        self.assertLess(dt, BOUND)
        self.assertIn("rm_rf_shared", fired("rm", "rm -rf /scratch/lab/old", conf))
        self.assertIn("rm_rf_shared", fired("rm", "rm /scratch/lab/old -fr", conf))
        self.assertNotIn("rm_rf_shared", fired("rm", "rm -rf ./build", conf))
        self.assertNotIn("rm_rf_shared", fired("rm", "rm notes-draft.txt /scratch/x", conf))


if __name__ == "__main__":
    unittest.main()
