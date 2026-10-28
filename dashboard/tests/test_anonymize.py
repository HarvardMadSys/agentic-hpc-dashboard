"""Anonymization: every name becomes a stable alias, and nothing else changes.

    cd dashboard && .venv/bin/python -m unittest discover -s tests -q
"""
import os
import re
import shutil
import stat
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rc_dashboard import anonymize as anonmod        # noqa: E402
from rc_dashboard import config as configmod         # noqa: E402
from rc_dashboard.anonymize import (AMBIGUOUS, EMAIL_MASK, UID_MASK, UNKNOWN,   # noqa: E402
                                    Anonymizer)

ALIAS = re.compile(r"user-[0-9a-f]{8}")
LAB = re.compile(r"lab-[0-9a-f]{8}")


def anon(*users, **kw):
    return Anonymizer(b"salt", known_users=users, **kw)


class AliasTest(unittest.TestCase):

    def test_an_alias_is_stable_for_one_salt_and_differs_across_salts(self):
        a, b = anon("glen"), anon("glen")
        self.assertRegex(a.user_alias("glen"), ALIAS)
        self.assertEqual(a.user_alias("glen"), b.user_alias("glen"))
        other = Anonymizer(b"another", known_users=["glen"])
        self.assertNotEqual(a.user_alias("glen"), other.user_alias("glen"))

    def test_a_user_and_a_lab_of_the_same_spelling_get_different_aliases(self):
        a = anon("glen")
        a.learn_lab("sham_lab")
        self.assertNotEqual(a.users["glen"][5:], a.labs["sham_lab"][4:])
        self.assertRegex(a.labs["sham_lab"], LAB)


class WordTest(unittest.TestCase):

    def setUp(self):
        self.a = anon("glen")
        self.g = self.a.user_alias("glen")

    def test_a_name_is_matched_as_a_whole_word_wherever_it_stands(self):
        for raw, want in [("/home/glen/x", "/home/%s/x"), ("glen@rtx6000a", "%s@rtx6000a"),
                          ("-home-glen-workspace", "-home-%s-workspace"),
                          ("cd /scratch/glen && ls", "cd /scratch/%s && ls"),
                          ("glen.log", "%s.log"), ("owner=glen", "owner=%s")]:
            self.assertEqual(self.a.text(raw), want % self.g, raw)

    def test_a_longer_word_that_contains_a_name_is_left_alone(self):
        for raw in ("glenn", "aglen", "glen2"):
            self.assertEqual(self.a.text(raw), raw)

    def test_a_name_inside_an_underscored_word_is_masked(self):
        self.assertEqual(self.a.text("glen_results.csv"), "%s_results.csv" % self.g)

    def test_a_dotted_name_matches_whole_and_in_claude_codes_hyphen_spelling(self):
        a = anon("george.weingart", "george")
        gw, g = a.user_alias("george.weingart"), a.user_alias("george")
        self.assertEqual(a.text("/home/george.weingart/x"), "/home/%s/x" % gw)
        self.assertEqual(a.text("-home-george-weingart-proj"), "-home-%s-proj" % gw)
        self.assertEqual(a.text("george ran it"), "%s ran it" % g)

    def test_rewriting_twice_is_rewriting_once(self):
        a = anon("glen", "bob.smith")
        a.learn_lab("tambe_lab")
        s = "sbatch -A tambe_lab /home/glen/x bob.smith@h me@fas.harvard.edu /tmp/claude-2004/y"
        once = a.text(s)
        self.assertEqual(a.text(once), once)


class PatternTest(unittest.TestCase):

    def test_a_home_segment_is_masked_whether_or_not_the_account_is_known(self):
        a = anon()
        out = a.text("cat /n/home08/bowen/notes.txt")
        self.assertNotIn("bowen", out)
        self.assertRegex(out, r"/n/home08/user-[0-9a-f]{8}/notes\.txt")

    def test_a_name_a_path_pattern_caught_is_not_learned_as_an_account(self):
        # `/home/logs` is a folder, not a person: masking it there is fine,
        # masking every `logs` on the page is not.
        a = anon()
        a.text("/home/logs/today")
        self.assertNotIn("logs", a.users)
        self.assertEqual(a.text("tail logs"), "tail logs")

    def test_lab_trees_mask_the_lab_and_the_person(self):
        a = anon()
        for raw in ("/n/holylabs/LABS/xlin/Lab/julialin/run.py",
                    "/n/holylabs/ydu_lab/Lab/jslee/2d_ebm/bin/python",
                    "/n/netscratch/zhuang_lab/Everyone/zhiyun/data",
                    "/n/lab_storage/ydu_lab/will/workspace"):
            out = a.text(raw)
            for word in ("xlin", "julialin", "ydu", "jslee", "zhuang", "zhiyun", "will"):
                self.assertNotRegex(out, r"(?<![A-Za-z0-9])%s(?![A-Za-z0-9])" % word, raw)

    def test_claude_code_folder_names_mask_the_same_homes_and_labs(self):
        a = anon("glen")
        a.learn_lab("albergo_lab")
        out = a.text("~/.claude/projects/-n-netscratch-albergo-lab-Everyone-frank-cbm/memory")
        self.assertNotIn("albergo", out)
        self.assertNotIn("frank", out)
        out = a.text("/tmp/claude-2004/-n-home02-bowen-proj/x")
        self.assertNotIn("bowen", out)

    def test_labs_come_from_slurm_accounts_and_lab_names_and_are_masked_first(self):
        a = anon("tambe")
        out = a.text("sbatch --account=kempner_grads -p gpu job.sh; ls /n/holylabs/tambe_lab")
        self.assertNotIn("kempner_grads", out)
        self.assertNotIn("tambe", out)
        self.assertRegex(out, r"--account=lab-[0-9a-f]{8}")
        self.assertRegex(out, r"/n/holylabs/lab-[0-9a-f]{8}$")   # one lab, not user-…_lab
        self.assertEqual(a.text("ls -A mydir"), "ls -A mydir")     # -A of ls is no account

    def test_a_clipped_name_at_the_end_of_a_long_string_is_masked_by_its_prefix(self):
        a = anon("pranavsitaraman", "pradeep", "prabhu")
        cut = "python eval.py " + "--flag x " * 10 + "--out /n/home08/pranavsitaram"
        self.assertGreaterEqual(len(cut), anonmod.CLIPPED_MIN)
        out = a.text(cut)
        self.assertTrue(out.endswith(a.user_alias("pranavsitaraman")), out)
        amb = "python eval.py " + "--flag x " * 10 + "--owner pra"
        self.assertTrue(a.text(amb).endswith(AMBIGUOUS), a.text(amb))
        self.assertEqual(a.text("--owner pra"), "--owner pra")     # short: not a clipped line


class MaskTest(unittest.TestCase):

    def test_emails_are_masked_and_a_git_remote_is_not(self):
        a = anon()
        self.assertEqual(a.text("mail ms@fas.harvard.edu now"), "mail %s now" % EMAIL_MASK)
        self.assertEqual(a.text("git clone git@github.com:org/repo"),
                         "git clone git@github.com:org/repo")

    def test_uids_are_masked_in_every_spelling(self):
        a = anon()
        for raw, want in [("/tmp/claude-2004/x", "/tmp/claude-%s/x"),
                          ("/user.slice/user-2004.slice/user@2004.service/x",
                           "/user.slice/user-%s.slice/user@%s.service/x"),
                          ("/run/user/2004/bus", "/run/user/%s/bus"), ("uid=2004", "uid=%s")]:
            self.assertEqual(a.text(raw), want.replace("%s", UID_MASK), raw)
        self.assertEqual(a.payload({"uid": 2004, "user": "x1"})["uid"], None)

    def test_the_switches_turn_each_mask_off(self):
        a = anon(labs=False, emails=False, uids=False)
        s = "sbatch -A sham_lab a@b.org /tmp/claude-2004"
        self.assertEqual(a.text(s), s)


class ReservedTest(unittest.TestCase):

    def test_kept_and_system_accounts_and_class_names_are_never_aliased(self):
        a = anon("root")
        a.learn_user("gdm", uid=120)
        a.learn_user("agent")
        self.assertEqual(a.text("root gdm agent"), "root gdm agent")
        self.assertEqual(a.status()["system_accounts"], 1)
        self.assertEqual(a.status()["reserved_collisions"], 1)   # agent; root is kept on purpose

    def test_a_name_that_is_not_a_plausible_account_is_not_learned(self):
        a = anon("1", "x", "user-0123abcd")
        self.assertEqual(a.users, {})


class PayloadTest(unittest.TestCase):

    def test_names_anywhere_in_a_payload_are_masked_and_the_shape_is_kept(self):
        a = anon()
        body = {"rows": [{"user": "glen", "args": "ls /home/glen", "n": 3}],
                "by_user": {"glen": 3, "other": 1},
                "trees": [{"users": ["ana"], "note": "ana's job"}],
                "paths": {"/home/glen/x": 1}}
        out = a.payload(body)
        g, n = a.user_alias("glen"), a.user_alias("ana")
        self.assertEqual(out["rows"][0], {"user": g, "args": "ls /home/%s" % g, "n": 3})
        self.assertEqual(out["by_user"], {g: 3, "other": 1})
        self.assertEqual(out["trees"][0], {"users": [n], "note": "%s's job" % n})
        self.assertEqual(out["paths"], {"/home/%s/x" % g: 1})
        self.assertEqual(body["rows"][0]["user"], "glen")              # a copy, not in place

    def test_a_name_is_masked_even_when_the_string_that_introduces_it_comes_later(self):
        a = anon()
        out = a.payload([{"args": "ssh carol@h1"}, {"user": "carol"}])
        self.assertNotIn("carol", repr(out))

    def test_structural_keys_stay_what_the_page_reads(self):
        a = anon("glen")
        out = a.payload({"by_class": {"agent": 1}, "glen_total": 2})
        self.assertEqual(out, {"by_class": {"agent": 1}, "glen_total": 2})


class RequestTest(unittest.TestCase):

    def test_an_alias_maps_back_and_anything_else_maps_to_nothing(self):
        a = anon("glen")
        self.assertEqual(a.dealias_user(a.user_alias("glen")), "glen")
        self.assertEqual(a.dealias_user("glen"), UNKNOWN)            # no probing by real name
        self.assertEqual(a.dealias_user("user-00000000"), UNKNOWN)

    def test_an_unknown_alias_rereads_the_roster_once(self):
        roster = ["glen"]
        a = anon(roster=lambda: list(roster))
        roster.append("ana")
        alias = Anonymizer(b"salt").user_alias("ana")
        self.assertEqual(a.dealias_user(alias), "ana")


class ConfigTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rc_anon_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def cfg(self, **kw):
        tree = configmod.defaults()
        tree["cache"]["dir"] = self.tmp
        tree["anonymize"].update(kw)
        return configmod.Config(tree, {}, None)

    def test_off_by_default(self):
        self.assertIsNone(anonmod.from_config(self.cfg()))

    def test_a_missing_salt_is_generated_once_owner_only_and_then_reused(self):
        first = anonmod.from_config(self.cfg(enabled=True))
        path = os.path.join(self.tmp, "anonymize.salt")
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(first.salt_source, "generated")
        again = anonmod.from_config(self.cfg(enabled=True))
        self.assertEqual(again.salt_source, "file")
        self.assertEqual(first.user_alias("glen"), again.user_alias("glen"))

    def test_a_configured_salt_wins_and_is_never_printed(self):
        a = anonmod.from_config(self.cfg(enabled=True, salt="s3cret"))
        self.assertEqual(a.salt_source, "config")
        printed = self.cfg(enabled=True, salt="s3cret").as_dict(with_origins=True)
        self.assertEqual(printed["anonymize"]["salt"]["_value"], "(hidden)")

    def test_the_operator_named_in_the_config_is_a_known_name(self):
        a = anonmod.from_config(self.cfg(enabled=True))
        self.assertIn("juncheng", a.users)          # filters.drop_global's default


if __name__ == "__main__":
    unittest.main()
