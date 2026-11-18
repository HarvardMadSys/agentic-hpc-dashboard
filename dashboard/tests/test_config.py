"""The configuration fails closed (rc_dashboard/config.py): a config file
someone named must exist, a yes/no setting reads as yes or no or stops the
service, and an unknown key where a typo would leave names unmasked, the page
open or the agent on stops it too.

    cd dashboard && .venv/bin/python -m unittest tests.test_config -q
"""
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rc_dashboard import config as configmod        # noqa: E402

DASH = str(Path(__file__).resolve().parents[1])
# what every one of them must take, and what it must refuse
YES = ("1", "true", "True", "TRUE", "yes", "Yes", "on", "ON", " on ", "\ttrue\n")
NO = ("0", "false", "False", "FALSE", "no", "No", "off", "OFF", " 0", "false ")
NEITHER = ("n", "y", "2", "-1", "maybe", "nope", "falsey", "of", " ", "0 0", "tru")


def clean_env():
    """os.environ without any RC_DASH_* the shell running the tests may set."""
    return mock.patch.dict(os.environ, {k: v for k, v in os.environ.items()
                                        if not k.startswith("RC_DASH_")}, clear=True)


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rc-config-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        env = clean_env()
        env.start()
        self.addCleanup(env.stop)
        # and no dashboard.config.json of the checkout's
        here = mock.patch.object(configmod, "HERE", self.tmp)
        here.start()
        self.addCleanup(here.stop)

    def write(self, tree, name="c.json"):
        path = os.path.join(self.tmp, name)
        with open(path, "w") as fh:
            fh.write(tree if isinstance(tree, str) else json.dumps(tree))
        return path

    def load(self, tree=None, path=None):
        """(config, stderr) for a file holding `tree`."""
        err = io.StringIO()
        with redirect_stderr(err):
            cfg = configmod.load(None, path or self.write(tree))
        return cfg, err.getvalue()

    def refused(self, tree=None, path=None):
        """The message the service stops with."""
        with self.assertRaises(SystemExit) as cm:
            self.load(tree, path)
        return str(cm.exception)


class NamedFile(Case):
    def test_a_file_named_on_the_command_line_must_exist(self):
        missing = os.path.join(self.tmp, "typo.json")
        msg = self.refused(path=missing)
        self.assertIn(missing, msg)
        self.assertIn("--config", msg)

    def test_a_file_named_in_the_environment_must_exist(self):
        missing = os.path.join(self.tmp, "typo.json")
        with mock.patch.dict(os.environ, {"RC_DASH_CONFIG": missing}):
            with self.assertRaises(SystemExit) as cm:
                configmod.load(None, None)
        self.assertIn(missing, str(cm.exception))
        self.assertIn("RC_DASH_CONFIG", str(cm.exception))

    def test_a_file_that_is_not_a_json_object_stops_it(self):
        for text in ("{\"ask\": ", "[1, 2]", "null"):
            with self.subTest(text=text):
                self.assertIn("c.json", self.refused(path=self.write(text)))

    def test_with_none_named_the_defaults_stand(self):
        cfg = configmod.load(None, None)
        self.assertEqual(cfg.get("server.auth.password_hash"), "")


class Flags(Case):
    def flag_vars(self):
        """Every env var whose setting is a yes/no one, by the defaults."""
        out = {}
        tree = configmod.defaults()
        for dotted, (var, coerce) in configmod.ENV_MAP.items():
            if isinstance(configmod._get(tree, dotted, None), bool):
                out[var] = (dotted, coerce)
        return out

    def test_one_reading_for_every_yes_no_variable(self):
        flags = self.flag_vars()
        for var in ("RC_DASH_ASK", "RC_DASH_ANONYMIZE", "RC_DASH_RDNS", "RC_DASH_SLURM",
                    "RC_DASH_HISTORY", "RC_DASH_CACHE_WINDOWS", "RC_DASH_MERGE_REDUCE",
                    "RC_DASH_PREPROCESS", "RC_DASH_DROP_SELF_OBS"):
            self.assertIn(var, flags)
        for var, (dotted, coerce) in flags.items():
            self.assertIs(coerce, configmod._flag, var)

    def test_yes_and_no_in_any_spelling_and_nothing_else(self):
        for var, (dotted, _) in sorted(self.flag_vars().items()):
            for raw, want in [(v, True) for v in YES] + [(v, False) for v in NO]:
                with self.subTest(var=var, raw=raw), mock.patch.dict(os.environ, {var: raw}):
                    self.assertIs(configmod.load(None, None).get(dotted), want)
            for raw in NEITHER:
                with self.subTest(var=var, raw=raw), mock.patch.dict(os.environ, {var: raw}):
                    with self.assertRaises(SystemExit) as cm:
                        configmod.load(None, None)
                    self.assertIn(var, str(cm.exception))

    def test_a_number_that_does_not_parse_names_its_variable(self):
        with mock.patch.dict(os.environ, {"RC_DASH_PORT": "80a"}):
            with self.assertRaises(SystemExit) as cm:
                configmod.load(None, None)
        self.assertIn("RC_DASH_PORT", str(cm.exception))

    def test_a_string_in_the_file_reads_as_the_variable_does(self):
        cfg, _ = self.load({"ask": {"enabled": "false"}, "anonymize": {"enabled": " On "},
                            "panels": {"cache_windows": "off"}, "slurm": {"enabled": 0},
                            "server": {"rdns": {"enabled": 1}}})
        self.assertIs(cfg.get("ask.enabled"), False)
        self.assertIs(cfg.get("anonymize.enabled"), True)
        self.assertIs(cfg.get("panels.cache_windows"), False)
        self.assertIs(cfg.get("slurm.enabled"), False)
        self.assertIs(cfg.get("server.rdns.enabled"), True)

    def test_anything_else_in_the_file_stops_it(self):
        for bad in ("maybe", "", None, 2, 0.5, [], {"x": 1}):
            with self.subTest(bad=bad):
                self.assertIn("anonymize.enabled", self.refused({"anonymize": {"enabled": bad}}))


class UnknownKeys(Case):
    def test_a_typo_where_it_would_fail_open_stops_it_naming_the_key_meant(self):
        for tree, typo, meant in (
                ({"anonymize": {"enable": True}}, "anonymize.enable", "anonymize.enabled"),
                ({"anonymise": {"enabled": True}}, "anonymise", "anonymize"),
                ({"ask": {"enable": False}}, "ask.enable", "ask.enabled"),
                ({"server": {"auth": {"pasword_hash": "x"}}}, "server.auth.pasword_hash",
                 "server.auth.password_hash"),
                ({"server": {"hots": "0.0.0.0"}}, "server.hots", "server.host"),
                ({"sever": {"host": "0.0.0.0"}}, "sever", "server")):
            with self.subTest(typo=typo):
                msg = self.refused(tree)
                self.assertIn("%s is not a setting" % typo, msg)
                self.assertIn("the nearest is %s" % meant, msg)

    def test_every_typo_is_named_at_once(self):
        msg = self.refused({"anonymize": {"enable": True}, "ask": {"enable": False}})
        self.assertIn("anonymize.enable ", msg)
        self.assertIn("ask.enable ", msg)

    def test_a_section_given_as_one_value_stops_it(self):
        self.assertIn("anonymize: true is not an object", self.refused({"anonymize": True}))
        self.assertIn("server.auth", self.refused({"server": {"auth": "scrypt$..."}}))

    def test_elsewhere_an_old_key_is_a_warning_and_nothing_more(self):
        # the EBR format's keys, from before it was removed
        cfg, err = self.load({"feeds": {"ebpf": {"prefer": "ebr", "days": 3}},
                              "ingest": {"convert": {"enabled": True}},
                              "ebr": {"zstd": 3}})
        for key in ("feeds.ebpf.prefer", "ingest.convert", "ebr"):
            self.assertIn("%s is not a setting" % key, err)
        self.assertEqual(cfg.get("feeds.ebpf.days"), 3)

    def test_keys_the_code_reads_with_defaults_of_its_own_are_known(self):
        _, err = self.load({"reducers": {"cascades": {"max_nodes": 5},
                                         "workspace": {"max_dirs": 9},
                                         "sessions": {"proc_index_max": 10},
                                         "trajectories": {"inspect_workers": 2,
                                                          "inspect_nodes": 100}},
                            "live": {"user_rows": 5},
                            "risk": {"vars": {"scratch": "/n/netscratch"},
                                     "dangerous": {"override": {"rm_rf": {"severity": "low"}}}}})
        self.assertEqual(err, "")

    def test_the_example_file_loads_without_a_word(self):
        # its risk profile is a path under dashboard/
        with mock.patch.object(configmod, "HERE", DASH):
            _, err = self.load(path=os.path.join(DASH, "dashboard.config.example.json"))
        self.assertEqual(err, "")


if __name__ == "__main__":
    unittest.main()
