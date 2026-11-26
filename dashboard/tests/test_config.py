"""The configuration fails closed (rc_dashboard/config.py): a config file
someone named must exist, a yes/no setting reads as yes or no or stops the
service, and an unknown key where a typo would leave names unmasked or the page
open stops it too.

    cd dashboard && .venv/bin/python -m unittest tests.test_config -q
"""
import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rc_dashboard import __main__ as cli            # noqa: E402
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
        for var in ("RC_DASH_ANONYMIZE", "RC_DASH_RDNS", "RC_DASH_SLURM", "RC_DASH_HISTORY", "RC_DASH_CACHE_WINDOWS", "RC_DASH_MERGE_REDUCE",
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
        cfg, _ = self.load({"rollup": {"enabled": "false"}, "anonymize": {"enabled": " On "},
                            "panels": {"cache_windows": "off"}, "slurm": {"enabled": 0},
                            "server": {"rdns": {"enabled": 1}}})
        self.assertIs(cfg.get("rollup.enabled"), False)
        self.assertIs(cfg.get("anonymize.enabled"), True)
        self.assertIs(cfg.get("panels.cache_windows"), False)
        self.assertIs(cfg.get("slurm.enabled"), False)
        self.assertIs(cfg.get("server.rdns.enabled"), True)

    def test_anything_else_in_the_file_stops_it(self):
        for bad in ("maybe", "", None, 2, 0.5, [], {"x": 1}):
            with self.subTest(bad=bad):
                self.assertIn("anonymize.enabled", self.refused({"anonymize": {"enabled": bad}}))


class WhoMayConnect(Case):
    def test_names_and_origins_are_lists(self):
        with mock.patch.dict(os.environ, {"RC_DASH_ALLOWED_HOSTS": "dash.example.org, *",
                                          "RC_DASH_ALLOWED_ORIGINS": "https://dash.example.org"}):
            cfg = configmod.load(None, None)
        self.assertEqual(cfg.get("server.allowed_hosts"), ["dash.example.org", "*"])
        self.assertEqual(cfg.get("server.allowed_origins"), ["https://dash.example.org"])
        for key in ("allowed_hosts", "allowed_origins", "dev_origins"):
            with self.subTest(key=key):
                self.assertIn("server.%s" % key, self.refused({"server": {key: "dash.example.org"}}))
                self.assertIn("server.%s" % key, self.refused({"server": {key: [1]}}))


class SlurmAbnormal(Case):
    def test_each_threshold_is_a_count_and_the_reducer_s_defaults_are_the_config_s(self):
        from rc_dashboard.reducers.slurm import ABNORMAL
        self.assertEqual(configmod.defaults()["reducers"]["slurm"]["abnormal"], ABNORMAL)
        cfg, _ = self.load({"reducers": {"slurm": {"abnormal": {"refused": 1}}}})
        # one changed, the rest kept: objects merge key by key
        self.assertEqual(cfg.get("reducers.slurm.abnormal"), dict(ABNORMAL, refused=1))
        for key in ("cancels", "poll_gap_s"):
            for bad in (0, -3, "5", 2.5, True, None):
                with self.subTest(key=key, bad=bad):
                    self.assertIn("reducers.slurm.abnormal." + key, self.refused(
                        {"reducers": {"slurm": {"abnormal": {key: bad}}}}))


class UnknownKeys(Case):
    def test_a_typo_where_it_would_fail_open_stops_it_naming_the_key_meant(self):
        for tree, typo, meant in (
                ({"anonymize": {"enable": True}}, "anonymize.enable", "anonymize.enabled"),
                ({"anonymise": {"enabled": True}}, "anonymise", "anonymize"),
                ({"server": {"auth": {"pasword_hash": "x"}}}, "server.auth.pasword_hash",
                 "server.auth.password_hash"),
                ({"server": {"hots": "0.0.0.0"}}, "server.hots", "server.host"),
                ({"sever": {"host": "0.0.0.0"}}, "sever", "server")):
            with self.subTest(typo=typo):
                msg = self.refused(tree)
                self.assertIn("%s is not a setting" % typo, msg)
                self.assertIn("the nearest is %s" % meant, msg)

    def test_every_typo_is_named_at_once(self):
        msg = self.refused({"anonymize": {"enable": True}, "server": {"hots": "0.0.0.0"}})
        self.assertIn("anonymize.enable ", msg)
        self.assertIn("server.hots ", msg)

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

    def test_the_full_reference_loads_without_a_word(self):
        """Every setting `print-config --reference --all` writes is one the loader knows."""
        path = os.path.join(self.tmp, "full.toml")
        with open(path, "w") as fh:
            fh.write(configmod.reference_toml(everything=True))
        with mock.patch.object(configmod, "HERE", DASH):
            _, err = self.load(path=path)
        self.assertEqual(err, "")


class Retired(Case):
    """The feeds removed once nothing read them (sacct, sacctmgr, the domain and
    user CSVs): every way there was to set one is named and ignored, never
    refused, so a unit, a wrapper or a file written for them still starts."""

    def test_a_file_that_sets_one_starts_without_it(self):
        cfg, err = self.load({"feeds": {"sacct": {"dir": "/n/sacct", "jobs_csv": None},
                                        "user_actor": {"path": "user.csv"},
                                        "ebpf": {"days": 3}}})
        for feed in ("sacct", "user_actor"):
            self.assertIn("feeds.%s is retired" % feed, err)
        self.assertNotIn("is not a setting", err)
        # dropped, so neither /api/config nor a saved build's fingerprint holds it
        self.assertEqual(sorted(cfg.as_dict()["feeds"]), ["ebpf", "ebpf_node"])
        self.assertEqual(cfg.get("feeds.ebpf.days"), 3)

    def test_a_variable_that_set_one_is_named_and_ignored(self):
        env = {"RC_DASH_SACCT_DIR": "/n/sacct", "RC_DASH_DOMAINS_CSV": "map_user.csv",
               "RC_USER_CSV": "user.csv"}
        with mock.patch.dict(os.environ, env):
            cfg, err = self.load({})
        for var in env:
            self.assertIn("%s is retired" % var, err)
        self.assertEqual(sorted(cfg.as_dict()["feeds"]), ["ebpf", "ebpf_node"])

    def test_every_variable_there_was_is_named(self):
        self.assertEqual(len(configmod.RETIRED_ENV), 9)
        for var in configmod.RETIRED_ENV:
            with self.subTest(var=var), mock.patch.dict(os.environ, {var: "x"}):
                _, err = self.load({})
                self.assertIn("%s is retired" % var, err)

    def test_a_flag_that_set_one_is_taken_named_and_ignored(self):
        ap = cli.build_parser()
        self.assertNotIn("--sacct-dir", ap.format_help())
        self.assertNotIn("--jobs-csv", ap.format_help())
        args = ap.parse_args(["--sacct-dir", "/n/sacct", "--jobs-csv", "jobs.csv"])
        err = io.StringIO()
        with redirect_stderr(err):
            cfg = configmod.load(args, self.write({}))
        self.assertIn("--sacct-dir is retired", err.getvalue())
        self.assertIn("--jobs-csv is retired", err.getvalue())
        self.assertEqual(sorted(cfg.as_dict()["feeds"]), ["ebpf", "ebpf_node"])


class RetiredSections(Case):
    """The sections of features since removed (`annotate`, the tool-call purposes
    a model wrote; `export`, the Search & export tab's download; `ask`, Ask LLM):
    a file or a variable written for one is named and ignored, never refused, and
    nothing of it is served -- a key it held included."""

    KEY = "sk-ant-api03-EXAMPLEEXAMPLEEXAMPLE0123"
    # each section as a site's file may still hold it
    FILE = {"annotate": {"enabled": True, "provider": "command", "api_key": KEY,
                         "command": ["env", "ANTHROPIC_API_KEY=" + KEY, "claude", "-p"],
                         "system_prompt": "token " + KEY, "batch_size": 40},
            "export": {"max_rows": 100},
            "ask": {"enabled": True, "agent": "codex", "model": "",
                    "command": ["env", "OPENAI_API_KEY=" + KEY, "codex", "exec", "-"],
                    "env_passthrough": ["AWS_*"], "timeout_s": 240, "max_concurrent": 1,
                    "context_kb": 120, "include_commands": False, "system_prompt": "token " + KEY,
                    "system_prompt_file": "", "snapshot_guide": True, "citations": True,
                    "unmasked_answers": False}}
    # every variable there was to set one, and its section
    VARS = {"RC_DASH_ANNOTATE": "annotate", "RC_DASH_ANNOTATE_PROVIDER": "annotate",
            "RC_DASH_ANNOTATE_MODEL": "annotate", "RC_DASH_ANNOTATE_BASE_URL": "annotate",
            "RC_DASH_ANNOTATE_KEY_ENV": "annotate", "RC_DASH_ANNOTATE_KEY_FILE": "annotate",
            "RC_DASH_ANNOTATE_CMD": "annotate", "RC_DASH_EXPORT_MAX_ROWS": "export",
            "RC_DASH_ASK": "ask", "RC_DASH_ASK_AGENT": "ask", "RC_DASH_ASK_MODEL": "ask",
            "RC_DASH_ASK_CMD": "ask", "RC_DASH_ASK_ENV_PASSTHROUGH": "ask",
            "RC_DASH_ASK_PROMPT_FILE": "ask"}

    def test_every_one_is_retired(self):
        self.assertEqual(sorted(configmod.RETIRED_SECTIONS), sorted(self.FILE))
        self.assertEqual(configmod.RETIRED_SECTION_ENV, self.VARS)
        for name in self.FILE:
            self.assertNotIn(name, configmod.defaults())
            self.assertFalse([k for k in configmod.ENV_MAP if k.startswith(name + ".")])

    def test_a_file_that_sets_one_starts_without_it(self):
        cfg, err = self.load(dict(self.FILE, slurm={"enabled": False}))
        for name in self.FILE:
            self.assertIn("config file %s: %s is retired" % (os.path.join(self.tmp, "c.json"),
                                                            name), err)
            self.assertIsNone(cfg.get(name))
        self.assertNotIn("is not a setting", err)
        self.assertIs(cfg.get("slurm.enabled"), False)
        # dropped, so neither /api/config nor a saved build's fingerprint holds
        # it, nor the key it held, which /api/config no longer hides
        served = cfg.as_dict(with_origins=True)
        for name in self.FILE:
            self.assertNotIn(name, served)
            self.assertNotIn(name, cfg.as_dict())
        self.assertNotIn(self.KEY, json.dumps(served))

    def test_a_variable_that_set_one_is_named_and_ignored(self):
        for var, name in sorted(self.VARS.items()):
            # `maybe`, which a yes/no or a number setting refused: retired, it stops nothing
            with self.subTest(var=var), mock.patch.dict(os.environ, {var: "maybe"}):
                cfg, err = self.load({})
                self.assertIn("warning: %s is retired" % var, err)
                self.assertIsNone(cfg.get(name))


class AskRetired(Case):
    """Ask LLM was removed. A config file or an environment written for it still
    starts the service: whatever it set is named in one warning, which says so,
    and dropped -- the keys the old checks refused (a typo, a yes/no that is
    neither) included, since none is read now."""

    SAID = "is retired: Ask LLM was removed in this version, and the setting is ignored"

    def warnings(self, err):
        return [line for line in err.splitlines() if line.startswith("warning:")]

    def test_a_section_in_a_toml_file_is_one_warning(self):
        for text in ('[ask]\nenabled = true\nagent = "codex"\nenv_passthrough = ["AWS_*"]\n'
                     'command = "env OPENAI_API_KEY=sk-x codex exec -"\n',
                     # dotted keys, the same table
                     'ask.enabled = false\nask.model = "some-model"\n',
                     # what the old checks refused: a typo, and a yes/no that is neither
                     '[ask]\nenable = true\nenabled = "maybe"\n'):
            with self.subTest(text=text):
                path = self.write(text, "c.toml")
                cfg, err = self.load(path=path)
                self.assertEqual(self.warnings(err),
                                 ["warning: config file %s: ask %s" % (path, self.SAID)])
                self.assertIsNone(cfg.get("ask"))
                self.assertNotIn("ask", cfg.as_dict())
                self.assertNotIn("ask", cfg.as_dict(with_origins=True))

    def test_the_section_in_a_json_file_is_one_warning(self):
        cfg, err = self.load({"ask": {"enabled": True, "timeout_s": "soon"}, "slurm": {"enabled": False}})
        self.assertEqual(self.warnings(err), ["warning: config file %s: ask %s"
                                              % (os.path.join(self.tmp, "c.json"), self.SAID)])
        self.assertIsNone(cfg.get("ask"))
        self.assertIs(cfg.get("slurm.enabled"), False)

    def test_a_variable_is_one_warning_whatever_it_says(self):
        for raw in ("1", "0", "false", "maybe"):
            with self.subTest(raw=raw), mock.patch.dict(os.environ, {"RC_DASH_ASK": raw}):
                cfg, err = self.load({})
                self.assertEqual(self.warnings(err), ["warning: RC_DASH_ASK %s" % self.SAID])
                self.assertIsNone(cfg.get("ask"))

    def test_print_config_no_longer_knows_it(self):
        self.assertNotIn("ask", configmod.defaults())
        self.assertFalse([k for k in configmod.ENV_MAP if k.startswith("ask.")])
        self.assertNotIn("ask", configmod.STRICT)
        self.assertNotIn("[ask]", configmod.reference_toml())
        self.assertNotIn("RC_DASH_ASK", configmod.reference_toml())


if __name__ == "__main__":
    unittest.main()


class Toml(Case):
    """A TOML file is the same settings as a JSON one, checked the same way."""

    def test_toml_and_json_resolve_the_same(self):
        js = self.write({"site": {"name": "x"}, "slurm": {"enabled": False},
                         "live": {"retention_min": 1440}, "filters": {"drop_global": ["svc"]}})
        tm = self.write('[site]\nname = "x"\n[slurm]\nenabled = false\n[live]\n'
                        'retention_min = 1440\n[filters]\ndrop_global = ["svc"]\n', "c.toml")
        a, _ = self.load(path=js)
        b, _ = self.load(path=tm)
        self.assertEqual(a.as_dict(), b.as_dict())
        self.assertEqual(b.origin("site.name"), "file:%s" % tm)

    def test_the_same_checks(self):
        msg = self.refused(path=self.write('[server]\nhots = "0.0.0.0"\n', "c.toml"))
        self.assertIn("server.hots is not a setting; the nearest is server.host", msg)
        msg = self.refused(path=self.write('[slurm]\nenabled = "maybe"\n', "c.toml"))
        self.assertIn("slurm.enabled", msg)
        cfg, _ = self.load(path=self.write('[slurm]\nenabled = "off"\n', "c.toml"))
        self.assertIs(cfg.get("slurm.enabled"), False)
        msg = self.refused(path=self.write('[site\nname = 1\n', "c.toml"))
        self.assertIn("c.toml", msg)

    def test_the_toml_writer_keeps_any_text(self):
        """json.dumps wrote a character past U+FFFF as a UTF-16 pair, which TOML refuses:
        `rc-dashboard init` for a site named `Cluster 🚀` wrote a config that would not
        load. Every string goes in as itself, escaped only where TOML says."""
        import tomllib
        for v in ("Cluster 🚀", "ünïcödé · 日本", 'a "quote" and a \\ backslash',
                  "tab\tnewline\nreturn\r", "bell\x07 and del\x7f", ""):
            self.assertEqual(tomllib.loads("k = " + configmod._toml_value(v))["k"], v, repr(v))
        self.assertEqual(tomllib.loads(configmod._toml_key("rack 🚀") + " = 1"), {"rack 🚀": 1})
        self.assertIn("🚀", configmod._toml_value("Cluster 🚀"), "as itself, not escaped")
        from rc_dashboard import init_cmd
        text = init_cmd.toml_config("Cluster 🚀", "/scratch/ebpfm/日本", "/var/log/ebpfm-login",
                                    False, 8080, "", "2026-11-24", "login01")
        self.assertEqual(tomllib.loads(text)["site"]["name"], "Cluster 🚀")

    def test_a_toml_date_reads_as_text(self):
        cfg, _ = self.load(path=self.write("[clock]\nas_of = 2026-11-02T09:00:00\n", "c.toml"))
        self.assertEqual(cfg.get("clock.as_of"), "2026-11-02 09:00:00")

    def test_named_by_the_environment(self):
        tm = self.write('[site]\nname = "from toml"\n', "x.toml")
        with mock.patch.dict(os.environ, {"RC_DASH_CONFIG": tm}):
            cfg = configmod.load(None, None)
        self.assertEqual(cfg.get("site.name"), "from toml")


class DefaultFiles(Case):
    def test_either_default_file_is_read(self):
        for name, text in (("dashboard.config.json", '{"site": {"name": "j"}}'),
                           ("dashboard.toml", '[site]\nname = "t"\n')):
            with self.subTest(name=name):
                path = self.write(text, name)
                self.assertEqual(configmod.find_config_file(), path)
                os.unlink(path)

    def test_both_are_refused_naming_which_to_keep(self):
        self.write("{}", "dashboard.config.json")
        later = self.write("", "dashboard.toml")
        os.utime(later, (time.time() + 60,) * 2)
        with self.assertRaises(SystemExit) as cm:
            configmod.find_config_file()
        msg = str(cm.exception)
        self.assertIn("dashboard.config.json and", msg)
        self.assertIn("dashboard.toml was changed last", msg)
        # a file named outright settles it
        self.assertEqual(configmod.find_config_file("/x.toml"), "/x.toml")


class RelativePaths(Case):
    def test_relative_paths_are_under_the_repository(self):
        cfg, _ = self.load({"cache": {"dir": "dashboard/.cache"},
                            "server": {"static_dir": "dashboard/web/dist",
                                       "auth": {"password_hash_file": "~/pw.hash"}},
                            "feeds": {"ebpf": {"roots": ["log/x", "/abs/y", "data/*/ebpf"]}},
                            "history": {"dir": ""}})
        repo = configmod.REPO
        self.assertEqual(cfg.get("cache.dir"), os.path.join(repo, "dashboard", ".cache"))
        self.assertEqual(cfg.get("server.static_dir"), os.path.join(repo, "dashboard", "web", "dist"))
        self.assertEqual(cfg.get("server.auth.password_hash_file"),
                         os.path.expanduser("~/pw.hash"))
        self.assertEqual(cfg.get("feeds.ebpf.roots"),
                         [os.path.join(repo, "log", "x"), "/abs/y",
                          os.path.join(repo, "data", "*", "ebpf")])
        self.assertEqual(cfg.get("history.dir"), "")      # empty is the default, still

    def test_the_readmes_relative_paths_find_the_page_and_the_cache(self):
        with mock.patch.object(configmod, "HERE", DASH):
            cfg, _ = self.load({"cache": {"dir": "dashboard/.cache"},
                                "server": {"static_dir": "dashboard/web/dist"}})
        self.assertEqual(cfg.get("cache.dir"), os.path.join(DASH, ".cache"))
        self.assertEqual(cfg.get("server.static_dir"), os.path.join(DASH, "web", "dist"))

    def test_installed_without_a_checkout_they_are_beside_the_file(self):
        with mock.patch.object(configmod, "CHECKOUT", False):
            cfg, _ = self.load({"cache": {"dir": "state"}})
        self.assertEqual(cfg.get("cache.dir"), os.path.join(self.tmp, "state"))

    def test_a_flag_or_a_variable_is_the_shells(self):
        with mock.patch.dict(os.environ, {"RC_DASH_CACHE_DIR": "rel/cache"}):
            cfg, _ = self.load({})
        self.assertEqual(cfg.get("cache.dir"), "rel/cache")


class Candidates(Case):
    def test_the_collectors_defaults_and_its_settings_file(self):
        etc = self.write('# comment\nEBPFM_MIN_UID=0\nEBPFM_OUTPUT_DIR = "/data/ebpfm"\n'
                         "EBPFM_OUTPUT_DIR='/data/ebpfm2'\n; old\nEBPFM_NODE_OUTPUT_DIR=/data/login\n",
                         "ebpfm.default")
        with mock.patch.object(configmod, "EBPFM_DEFAULTS", etc):
            ebpf, node = configmod._ebpf_candidates(), configmod._node_candidates()
        # the last assignment wins, quotes off, ahead of the fixed list
        self.assertEqual(ebpf[0], "/data/ebpfm2")
        self.assertLess(ebpf.index("/scratch/ebpfm"), ebpf.index("/var/log/ebpfm"))
        self.assertEqual(node[0], "/data/login")
        self.assertLess(node.index("/scratch/ebpfm-login"), node.index("/var/log/ebpfm-login"))
        with mock.patch.object(configmod, "EBPFM_DEFAULTS", os.path.join(self.tmp, "none")):
            self.assertNotIn("/data/ebpfm2", configmod._ebpf_candidates())

    def test_a_folder_named_twice_is_listed_once(self):
        with mock.patch.dict(os.environ, {"EBPFM_OUTPUT_DIR": "/var/log/ebpfm/"}):
            c = configmod._ebpf_candidates()
        self.assertEqual(sum(os.path.normpath(p) == "/var/log/ebpfm" for p in c), 1)

    def test_the_env_file_reader(self):
        etc = self.write('A=1\nexport B="two words"\n  C = \'3\'  \nnot a line\n#D=4\nA=5\n', "e")
        self.assertEqual(configmod.env_file(etc), {"A": "5", "B": "two words", "C": "3"})
        self.assertEqual(configmod.env_file(os.path.join(self.tmp, "none")), {})


class Reference(Case):
    def drop_none(self, node):
        return {k: self.drop_none(v) if isinstance(v, dict) else v
                for k, v in node.items() if v is not None}

    def test_it_reads_back_as_the_defaults(self):
        import tomllib
        everything = tomllib.loads(configmod.reference_toml(everything=True))
        self.assertEqual(everything, self.drop_none(configmod.defaults()))
        public = tomllib.loads(configmod.reference_toml())
        want = {}
        for path in configmod.PUBLIC_KEYS:
            configmod._set(want, path, configmod._get(configmod.defaults(), path))
        self.assertEqual(public, self.drop_none(want))

    def test_it_names_each_variable(self):
        text = configmod.reference_toml(everything=True)
        for dotted, (var, _c) in configmod.ENV_MAP.items():
            self.assertIn("# %s" % var, text, dotted)
        # a setting whose default is unset is a comment, naming its variable still (an
        # internal one: the whole reference)
        with mock.patch.object(configmod, "defaults", lambda: {"x": {"cmd": None}}), \
                mock.patch.dict(configmod.ENV_MAP, {"x.cmd": ("RC_DASH_X_CMD", str)}):
            self.assertIn("# cmd = (unset)  # RC_DASH_X_CMD",
                          configmod.reference_toml(everything=True))

    def test_the_command_prints_it(self):
        for argv, everything in ((["print-config", "--reference"], False),
                                 (["print-config", "--reference", "--all"], True)):
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(cli.main(argv), 0)
            self.assertEqual(out.getvalue(), configmod.reference_toml(everything))


class Public(Case):
    """A site chooses a few settings; the rest are internal, loadable but left out of the
    reference, the starter and the README's table."""

    def test_each_is_a_setting_with_a_line_of_help(self):
        dflt = configmod.defaults()
        for path, text in configmod.PUBLIC:
            with self.subTest(key=path):
                self.assertIsNot(configmod._get(dflt, path, configmod._MISSING),
                                 configmod._MISSING)
                self.assertTrue(text and not text.endswith("."), text)
        self.assertEqual(len(set(configmod.PUBLIC_KEYS)), len(configmod.PUBLIC_KEYS))

    def test_they_stay_few(self):
        """A new setting is internal unless every site must choose it."""
        self.assertLessEqual(len(configmod.PUBLIC), 24)

    def test_the_readme_table_names_each_and_only_them(self):
        with open(os.path.join(DASH, "README.md")) as fh:
            text = fh.read()
        table = text[text.index("| Setting | Variable |"):]
        table = table[:table.index("\n\n")]
        named = [line.split("`")[1] for line in table.splitlines()[2:]]
        self.assertEqual(sorted(named), sorted(configmod.PUBLIC_KEYS))

    def test_the_reference_says_how_many_are_internal(self):
        n = sum(1 for _ in configmod._leaf_paths(configmod.defaults()))
        self.assertIn("# %d more settings are internal" % (n - len(configmod.PUBLIC)),
                      configmod.reference_toml())

    def test_print_config_shows_them_and_any_internal_one_changed(self):
        path = self.write({"site": {"name": "c"}, "panels": {"max_windows": 3}})
        # and the hidden --print-config of another command, with its own --all: serve's
        # resolution, which `print-config --all` would not apply for every command
        for argv, everything in ((["print-config", "--config", path], False),
                                 (["print-config", "--config", path, "--all"], True),
                                 (["serve", "--print-config", "--config", path], False),
                                 (["serve", "--print-config", "--all", "--config", path], True)):
            out, err = io.StringIO(), io.StringIO()
            with clean_env(), redirect_stdout(out), redirect_stderr(err):
                self.assertEqual(cli.main(argv), 0)
            tree = json.loads(out.getvalue())
            self.assertEqual(tree["site"]["name"]["_value"], "c")
            self.assertTrue(tree["site"]["name"]["_origin"].startswith("file"))
            self.assertEqual(tree["panels"]["max_windows"]["_value"], 3)     # changed: shown
            self.assertIn("_meta", tree)
            self.assertEqual("ingest" in tree, everything)                   # all at defaults
            self.assertEqual("internal settings at their defaults are left out" in
                             err.getvalue(), not everything)


class Examples(Case):
    """The starter and run.sh hold nothing of one site's."""

    def test_no_site_of_ours_in_the_examples_or_run_sh(self):
        texts = {}
        for name in ("dashboard.example.toml", "run.sh"):
            with open(os.path.join(DASH, name)) as fh:
                texts[name] = fh.read()
        for name, text in texts.items():
            for word in ("netscratch", "juncheng", "flamraoui", "marthen", "fasrc.json",
                         "holy|bos", "human-vscode"):
                with self.subTest(file=name, word=word):
                    self.assertNotIn(word, text)
        with open(os.path.join(DASH, "run.sh")) as fh:
            for line in fh:
                if line.lstrip().startswith("#") or "uv run" not in line:
                    continue
                self.assertIn("uv run --frozen", line)

    def test_the_toml_starter_loads(self):
        with mock.patch.object(configmod, "HERE", DASH):
            cfg, err = self.load(path=os.path.join(DASH, "dashboard.example.toml"))
        self.assertEqual(err, "")
        self.assertEqual(cfg.get("server.auth.password_hash_file"),
                         os.path.expanduser("~/.config/rc-dashboard/password.hash"))
        with open(os.path.join(DASH, "dashboard.example.toml")) as fh:
            self.assertLessEqual(len(fh.read().splitlines()), 16)


class Unreduced(Case):
    """What no reduction reads moves neither saved-state fingerprint."""

    def test_new_settings_keep_the_fingerprints(self):
        from rc_dashboard import hours, snapshots
        a, _ = self.load({})
        b, _ = self.load({"status": {"transport_files": ["/x/*.json"], "stale_s": 60},
                          "ui": {"show_internal": True},
                          "server": {"auth": {"password_hash_file": "/etc/pw.hash"}}})
        for fp in (snapshots.fingerprint, hours.fingerprint):
            self.assertEqual(fp("code", a, ["/r"]), fp("code", b, ["/r"]))
        self.assertNotIn("status", a.as_dict())
        self.assertIn("status", a.as_dict(with_origins=True))

    def test_what_ask_llm_left_keeps_the_fingerprints(self):
        """A file or a variable that still sets Ask LLM's section moves neither: it is
        dropped before the tree is made, and was outside both trees besides."""
        from rc_dashboard import hours, snapshots
        a, _ = self.load({})
        with mock.patch.dict(os.environ, {"RC_DASH_ASK": "1", "RC_DASH_ASK_CMD": "codex exec -"}):
            b, _ = self.load({"ask": {"enabled": True, "context_kb": 60}})
        for fp in (snapshots.fingerprint, hours.fingerprint):
            self.assertEqual(fp("code", a, ["/r"]), fp("code", b, ["/r"]))
        self.assertNotIn("ask", b.as_dict())

    def test_status_settings_are_checked(self):
        msg = self.refused({"status": {"transport_files": "/x.json"}})
        self.assertIn("status.transport_files", msg)
        msg = self.refused({"status": {"stale_s": 0}})
        self.assertIn("status.stale_s", msg)
        with mock.patch.dict(os.environ, {"RC_DASH_TRANSPORT_FILES": "/a.json,/b/*.json",
                                          "RC_DASH_SHOW_INTERNAL": "yes"}):
            cfg, _ = self.load({})
        self.assertEqual(cfg.get("status.transport_files"), ["/a.json", "/b/*.json"])
        self.assertIs(cfg.get("ui.show_internal"), True)
