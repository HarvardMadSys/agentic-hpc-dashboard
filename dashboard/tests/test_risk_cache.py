"""The risk reducer remembers each command line's verdict: same answers, fewer regexes.

    cd dashboard && .venv/bin/python -m unittest discover -s tests -q
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rc_dashboard.reducers import risk                              # noqa: E402
from rc_dashboard.reducers.risk import RiskReducer                  # noqa: E402


class Cfg:
    def get(self, dotted, default=None):
        return default


LINES = [("rm", "rm -rf /scratch/shared/old"), ("ps", "ps aux"), ("pip", "pip install torch"),
         ("curl", "curl -u bob:hunter2 -o x https://h"), ("ps", "ps aux"),
         ("mysql", "mysql -u root -pdbsecret42 prod"), ("git", "git push --force"),
         ("ps", "ps aux"), ("rm", "rm -rf /scratch/shared/old")]


def recs():
    for i, (ec, args) in enumerate(LINES * 50):
        yield {"event": "exit", "_a3": ("agent", "human")[i % 2], "_eff": ec, "args": args,
               "user": "u%d" % (i % 3), "host": "h1", "cpu_s": 0.01 * (i % 5), "pid": i,
               "exit_code": i % 2, "signal": None}


def reduce(cache_max):
    old = risk.MATCH_CACHE_MAX
    risk.MATCH_CACHE_MAX = cache_max
    try:
        r = RiskReducer(Cfg(), ["agent", "human"])
        for rec in recs():
            r.feed(rec)
        return r.result()
    finally:
        risk.MATCH_CACHE_MAX = old


class MatchCacheTest(unittest.TestCase):

    def test_remembered_verdicts_change_nothing(self):
        # a cache of 1 is cleared on almost every new line: effectively none
        self.assertEqual(reduce(1), reduce(50000))

    def test_each_distinct_line_is_matched_once(self):
        r = RiskReducer(Cfg(), ["agent", "human"])
        for rec in recs():
            r.feed(rec)
        self.assertEqual(len(r._match), len(set(LINES)))

    def test_the_cache_is_not_saved_with_the_window(self):
        self.assertIn("_match", RiskReducer.SNAPSHOT_SKIP)


class TypeBreakdownTest(unittest.TestCase):
    def test_each_row_breaks_its_class_counts_down_by_agent_type(self):
        r = RiskReducer(Cfg(), ["agent", "human"])
        for i, at in enumerate(["claude_code", "claude_code", "codex", None]):
            r.feed({"event": "exit", "_a3": "agent" if at else "human", "_eff": "rm",
                    "args": "rm -rf /scratch/shared/old", "user": "u", "host": "h1",
                    "cpu_s": 0.0, "pid": i, "agent_type": at, "exit_code": 0})
        row = next(x for x in r.result()["dangerous"]["rows"] if x["n"] == 4)
        self.assertEqual(row["types"], {"agent": {"claude_code": 2, "codex": 1}, "human": {"": 1}})
        self.assertEqual(row["classes"], {"agent": 3, "human": 1})


class GateTest(unittest.TestCase):
    """A gate may only skip a regex that could not have matched."""

    CORPUS = [ec + " " + a for ec, a in LINES] + [
        "nvidia-smi -L", "/usr/bin/nvcc -O2 x.cu", "pip3 install torch", "conda env create -f e.yml",
        "micromamba install numpy", "cmake --build .", "g++ -O3 a.cc", "go build ./...",
        "ssh holy7c12345", "python -m http.server 8000", "jupyter lab --no-browser",
        "wget -O out https://x", "curl -o f https://y", "huggingface-cli download m",
        "chromium --headless --dump-dom", "git push --force origin main", "git push -f",
        "rm -rf /n/shared", "rm -fr x", "chmod 777 d", "NVIDIA-SMI", "RM -RF /x",
        "curl -H 'Authorization: Bearer abc.def' https://api", "export OPENAI_API_KEY=sk-abc",
        "tool --token=abcdef", "x --password hunter2", "mysql -u r -pSecret99 db",
        "redis-cli -a s3cr3t", "curl -u bob:pw https://h", "-----BEGIN RSA PRIVATE KEY-----",
        "echo sk-" + "a" * 24, "echo ghp_" + "b" * 36, "echo hf_" + "c" * 32,
        "echo AKIA" + "D" * 16, "echo xoxb-1234567890ab", "ls -la", "",
        "ſſh holy1", "rm -rſ /x",
    ]

    def test_every_gate_agrees_with_its_regex(self):
        for line in self.CORPUS:
            low = line.casefold()
            for i, d in enumerate(risk._DANGEROUS):
                if d[-1].search(line):
                    self.assertTrue(risk._passes(risk._DG_GATE[i], low), (d[0], line))
            for i, sc in enumerate(risk._SECRETS):
                if sc[-1].search(line):
                    self.assertTrue(risk._passes(risk._SEC_GATE[i], line), (sc[0], line))

    def test_every_pattern_has_a_gate(self):
        self.assertTrue(all(g for g in risk._DG_GATE))
        self.assertTrue(all(g for g in risk._SEC_GATE))

if __name__ == "__main__":
    unittest.main()
