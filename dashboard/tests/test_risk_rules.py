"""Per-cluster risk rules: switched off, changed, added, and their gates.

    cd dashboard && .venv/bin/python -m unittest tests.test_risk_rules -q
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rc_dashboard import config as configmod, redact          # noqa: E402
from rc_dashboard.reducers import risk                        # noqa: E402
from rc_dashboard.reducers.risk import RiskReducer, ruleset   # noqa: E402


class Cfg:
    def __init__(self, conf):
        self.conf = conf

    def get(self, k, d=None):
        return self.conf if k == "risk" else d


def run(conf, lines):
    r = RiskReducer(Cfg(conf), ["agent", "human"])
    for i, (ec, args) in enumerate(lines):
        r.feed({"event": "exit", "_a3": "agent", "_eff": ec, "args": args, "user": "u",
                "host": "h", "cpu_s": 0.0, "pid": i, "exit_code": 0, "signal": None})
    out = r.result()
    # a kind no section shows is absent from the result, not empty
    return ({row["id"]: row["n"] for row in out.get("dangerous", {}).get("rows", [])},
            {row["id"]: row["n"] for row in out.get("secrets", {}).get("rows", [])}, out["rules"])


class Rules(unittest.TestCase):

    def setUp(self):
        risk._RULESETS.clear()
        self.extra = list(redact._EXTRA)

    def tearDown(self):
        risk._RULESETS.clear()
        redact._EXTRA[:] = self.extra

    def test_defaults_are_the_built_in_catalogue(self):
        rs = ruleset(Cfg({}))
        self.assertEqual([d[0] for d in rs.dg], [d[0] for d in risk.DANGEROUS])
        self.assertEqual(rs.dg_gate, risk._DG_GATE)       # every built-in keeps its gate

    def test_disable_and_relabel(self):
        conf = {"dangerous": {"disable": ["nvidia_smi_login"],
                              "override": {"pip_install": {"severity": "low", "label": "pip"}}}}
        dg, _, rules = run(conf, [("nvidia-smi", "nvidia-smi"), ("pip", "pip install x")])
        self.assertNotIn("nvidia_smi_login", dg)
        self.assertEqual(dg["pip_install"], 1)
        row = next(r for r in rules["dangerous"] if r["id"] == "pip_install")
        self.assertEqual((row["severity"], row["label"], row["source"]), ("low", "pip", "changed"))
        self.assertEqual(rules["disabled"][0]["id"], "nvidia_smi_login")

    def test_an_overridden_pattern_runs_without_the_old_gate(self):
        # the built-in rm_rf_shared gate is "rm"; the new pattern needs no "rm"
        conf = {"dangerous": {"override": {"rm_rf_shared": {"pattern": r"\bunlink\b"}}}}
        rs = ruleset(Cfg(conf))
        i = [d[0] for d in rs.dg].index("rm_rf_shared")
        self.assertIsNone(rs.dg_gate[i])
        dg, _, _ = run(conf, [("unlink", "unlink /scratch/x")])
        self.assertEqual(dg.get("rm_rf_shared"), 1)

    def test_a_variable_that_changes_a_pattern_drops_its_gate(self):
        conf = {"vars": {"compute_hosts": r"cn\d+"}}
        rs = ruleset(Cfg(conf))
        i = [d[0] for d in rs.dg].index("ssh_compute")
        self.assertIsNone(rs.dg_gate[i])
        dg, _, _ = run(conf, [("ssh", "ssh cn042 hostname"), ("ssh", "ssh holy7c01 ls")])
        self.assertEqual(dg.get("ssh_compute"), 1)       # this cluster's name, not FASRC's

    def test_added_rules_count_and_a_new_secret_is_masked_everywhere(self):
        conf = {"dangerous": {"add": [{"id": "rclone_sync", "label": "rclone sync on login",
                                       "severity": "med", "pattern": r"(?:^|/)rclone\b[^\n]*\bsync\b"}]},
                "secrets": {"add": [{"id": "site_token", "label": "site token",
                                     "severity": "HIGH", "pattern": r"\bRCT-[A-Z0-9]{12}\b"}]}}
        dg, sec, rules = run(conf, [("rclone", "rclone sync a b"),
                                    ("tool", "tool --x RCT-ABCDEFGHIJKL")])
        self.assertEqual(dg["rclone_sync"], 1)
        self.assertEqual(sec["site_token"], 1)
        self.assertEqual({a["id"] for a in rules["added"]}, {"rclone_sync", "site_token"})
        self.assertEqual(redact.redact("tool --x RCT-ABCDEFGHIJKL"), "tool --x [REDACTED]")

    def test_a_bad_rule_is_reported_not_fatal(self):
        conf = {"dangerous": {"add": [{"id": "broken", "pattern": "("}],
                              "disable": ["no_such_rule"],
                              "override": {"pip_install": {"pattern": "{{nowhere}}"}}}}
        dg, _, rules = run(conf, [("pip", "pip install x")])
        bad = {x["id"] for x in rules["invalid"]}
        self.assertEqual(bad, {"broken", "pip_install"})
        self.assertEqual(rules["unknown"][0]["id"], "no_such_rule")
        self.assertNotIn("pip_install", dg)              # left out, not guessed at


class Profile(unittest.TestCase):

    def test_a_profile_is_merged_into_the_resolved_config(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "site.json")
        with open(p, "w") as fh:
            json.dump({"//": "comment", "dangerous": {"disable": ["nvcc_login"]}}, fh)
        old = os.environ.get("RC_DASH_RISK_PROFILE")
        os.environ["RC_DASH_RISK_PROFILE"] = p
        try:
            cfg = configmod.load(None, None)
        finally:
            if old is None:
                os.environ.pop("RC_DASH_RISK_PROFILE")
            else:
                os.environ["RC_DASH_RISK_PROFILE"] = old
        self.assertEqual(cfg.get("risk.dangerous.disable"), ["nvcc_login"])
        self.assertTrue(cfg.origin("risk.dangerous.disable").startswith("profile:"))
        # the rules themselves are in the config, so in the saved-window fingerprint
        self.assertIn("nvcc_login", json.dumps(cfg.as_dict()))


if __name__ == "__main__":
    unittest.main()


class Shipped(unittest.TestCase):
    """The profiles in risk-profiles/ and their README stay true to the code."""

    DIR = Path(__file__).resolve().parents[1] / "risk-profiles"

    def setUp(self):
        risk._RULESETS.clear()
        self.extra = list(redact._EXTRA)

    def tearDown(self):
        risk._RULESETS.clear()
        redact._EXTRA[:] = self.extra

    def profiles(self):
        out = {}
        for p in sorted(self.DIR.glob("*.json")):
            with open(p) as fh:
                out[p.name] = configmod.strip_comments(json.load(fh))
        return out

    def test_every_shipped_profile_loads_with_nothing_rejected(self):
        ps = self.profiles()
        self.assertIn("example.json", ps)
        for name, conf in ps.items():
            notes = risk.RuleSet(conf).notes
            self.assertEqual((notes["invalid"], notes["unknown"]), ([], []), name)

    def test_the_readme_names_every_built_in_rule_and_variable(self):
        text = (self.DIR / "README.md").read_text()
        for rid in [d[0] for d in risk.DANGEROUS] + [s[0] for s in risk.SECRETS]:
            self.assertIn("`%s`" % rid, text, rid)
        for v in risk.DEFAULT_VARS:
            self.assertIn("`%s`" % v, text, v)

    def test_the_example_gates_never_hide_a_match(self):
        rs = risk.RuleSet(self.profiles()["example.json"])
        lines = ["rm rm -rf /scratch/lab/old", "rm rm -rf ./build", "apptainer apptainer build x.sif x.def",
                 "singularity singularity build a b", "rclone rclone sync a: b:",
                 "pip pip install --user x", "tool tool --key RCT-ABCDEFGHIJKL"]
        for i, (rid, *_rest, rx) in enumerate(rs.dg):
            for hay in lines:
                if rx.search(hay):
                    self.assertTrue(risk._passes(rs.dg_gate[i], hay.casefold()), (rid, hay))
        for i, (sid, *_rest, rx) in enumerate(rs.sec):
            for hay in lines:
                args = hay.split(" ", 1)[1]
                if rx.search(args):
                    self.assertTrue(risk._passes(rs.sec_gate[i], args), (sid, hay))


class Sections(unittest.TestCase):

    def setUp(self):
        risk._RULESETS.clear()

    def tearDown(self):
        risk._RULESETS.clear()

    def test_the_default_is_the_three_the_tab_always_had(self):
        rs = risk.RuleSet({})
        self.assertEqual([x["kind"] for x in rs.sections], ["login_compute", "commands", "credentials"])
        self.assertEqual(len(rs.dg), len(risk.DANGEROUS))
        self.assertEqual(rs.notes["unsectioned"], [])

    def test_a_section_of_its_own_and_the_rest(self):
        conf = {"sections": [
            {"id": "dm", "kind": "commands", "title": "Data movement", "rules": ["bulk_download"]},
            {"id": "other", "kind": "commands"}]}            # rules default to "rest"
        dg, sec, rules = run(conf, [("wget", "wget -O x http://h"), ("pip", "pip install x")])
        secs = {x["id"]: x for x in rules["sections"]}
        self.assertEqual(secs["dm"]["rules"], ["bulk_download"])
        self.assertNotIn("bulk_download", secs["other"]["rules"])
        self.assertEqual((dg["bulk_download"], dg["pip_install"]), (1, 1))
        row = next(r for r in rules["dangerous"] if r["id"] == "bulk_download")
        self.assertEqual(row["section"], "dm")

    def test_what_no_section_shows_is_not_computed(self):
        conf = {"sections": [{"id": "dm", "kind": "commands", "rules": ["bulk_download"]}]}
        r = RiskReducer(Cfg(conf), ["agent"])
        for i, (ec, args) in enumerate([("wget", "wget -O x http://h"), ("pip", "pip install x"),
                                        ("make", "make all"), ("tool", "tool --password=hunter22")]):
            r.feed({"event": "exit", "_a3": "agent", "_eff": ec, "args": args, "user": "u",
                    "host": "h", "cpu_s": 1.0, "pid": i, "exit_code": 0, "signal": None})
        out = r.result()
        self.assertEqual([x["id"] for x in out["dangerous"]["rows"]], ["bulk_download"])
        self.assertNotIn("secrets", out)                 # no credentials section
        self.assertNotIn("login_compute", out)           # no login_compute section
        self.assertEqual(len(r.lc), 0)                   # and its CPU accounting was skipped
        self.assertIn("pip_install", {x["id"] for x in out["rules"]["unsectioned"]})

    def test_bad_sections_are_reported(self):
        conf = {"sections": [{"id": "a", "kind": "commands", "rules": ["pip_install"]},
                             {"id": "b", "kind": "commands", "rules": ["pip_install", "nope"]},
                             {"id": "c", "kind": "nonsense"},
                             {"id": "d", "kind": "commands"}, {"id": "e", "kind": "commands"}]}
        rs = risk.RuleSet(conf)
        errors = " ".join(x["error"] for x in rs.notes["invalid"])
        self.assertIn("already in section a", errors)
        self.assertIn("kind must be one of", errors)
        self.assertIn("only one commands section can take the rest", errors)
        self.assertIn("nope", {x["id"] for x in rs.notes["unknown"]})
        self.assertEqual([x["id"] for x in rs.sections], ["a", "b", "d"])
