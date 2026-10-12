"""Chat with an LLM: the snapshot it is given, the command it runs, the jobs.

    cd dashboard && .venv/bin/python -m unittest discover -s tests -q

No test runs a real agent: `ask.command` points at a small Python stand-in.
"""
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rc_dashboard import ask                                         # noqa: E402

PY = sys.executable


class Cfg:
    def __init__(self, **kv):
        self.kv = {"cache.dir": tempfile.mkdtemp()}
        self.kv.update(kv)

    def get(self, dotted, default=None):
        return self.kv.get(dotted, default)


def wait(a, jid, s=20):
    end = time.time() + s
    while time.time() < end:
        v = a.view(jid)
        if v["state"] in ("done", "error"):
            return v
        time.sleep(0.05)
    raise AssertionError("job never finished")


PANELS = {"tool_mix": {"by_class": {"agent": {"calls": 12, "rows": [{"b": "git"}] * 40}},
                       "series": {"t": list(range(5000))}},
          "risk": {"dangerous": {"rows": [{"command": "rm -rf", "args": "rm -rf /secret/path",
                                           "instances": [{"pid": 1}]}]}}}


class SnapshotTest(unittest.TestCase):
    def test_drops_bulk_and_command_lines_and_keeps_summaries(self):
        s = json.loads(ask.snapshot(PANELS))
        p = s["panels"]
        self.assertNotIn("series", p["tool_mix"])
        self.assertEqual(p["tool_mix"]["by_class"]["agent"]["calls"], 12)
        row = p["risk"]["dangerous"]["rows"][0]
        self.assertNotIn("args", row)
        self.assertNotIn("instances", row)
        self.assertIn("... 28 more", p["tool_mix"]["by_class"]["agent"]["rows"])

    def test_command_lines_only_when_asked(self):
        s = json.loads(ask.snapshot(PANELS, with_argv=True))
        self.assertIn("args", s["panels"]["risk"]["dangerous"]["rows"][0])

    def test_fits_the_budget(self):
        big = {"x": [{"k": "v" * 50, "n": i} for i in range(20000)]}
        self.assertLessEqual(len(ask.snapshot(big, budget_kb=4)), 4 * 1024)

    def test_secrets_are_masked(self):
        s = ask.snapshot({"p": {"note": "token sk-" + "a" * 40}})
        self.assertNotIn("a" * 40, s)


class PromptTest(unittest.TestCase):
    def test_carries_question_window_history_and_snapshot(self):
        t = ask.prompt("who ran rm?", "{}", "last 24h",
                       [{"q": "first?", "a": "first answer"}])
        for part in ("who ran rm?", "last 24h", "first answer", "<snapshot>"):
            self.assertIn(part, t)


class DeploymentTest(unittest.TestCase):
    def test_states_what_it_knows_and_assumes_nothing(self):
        cfg = Cfg(**{"site.name": "dev", "site.description": "a GPU workstation"})
        line = ask.deployment(cfg, {"hosts": [{"host": "rtx6000a"}]})
        self.assertIn("a GPU workstation", line)
        self.assertIn("1 host reporting: rtx6000a", line)
        self.assertEqual(ask.deployment(Cfg()), "not described")

    def test_the_prompt_names_no_kind_of_machine(self):
        t = ask.prompt("q", "{}", "24h", about="not described")
        self.assertNotIn("login node", t)
        self.assertIn("Deployment: not described.", t)


class SystemPromptTest(unittest.TestCase):
    def test_built_in_by_default_with_the_guide(self):
        t = ask.prompt("q", "{}", "24h", system=ask.system_text(Cfg()))
        self.assertIn("agent-observability dashboard", t)
        self.assertIn("Where to look in the snapshot", t)

    def test_inline_prompt_replaces_the_built_in_and_fills_placeholders(self):
        cfg = Cfg(**{"ask.system_prompt": "Be terse about {site} ({deployment}) for {window}.",
                     "site.name": "fasrc"})
        sys_ = ask.system_text(cfg, "2 hosts", "24h")
        self.assertEqual(sys_, "Be terse about fasrc (2 hosts) for 24h.")
        self.assertNotIn("agent-observability dashboard", ask.prompt("q", "{}", "24h", system=sys_))

    def test_file_wins_and_is_read_every_time(self):
        f = Path(tempfile.mkdtemp()) / "p.txt"
        f.write_text("first")
        cfg = Cfg(**{"ask.system_prompt_file": str(f), "ask.system_prompt": "inline"})
        self.assertEqual(ask.system_text(cfg), "first")
        f.write_text("second")
        self.assertEqual(ask.system_text(cfg), "second")

    def test_unreadable_file_fails_the_question_up_front(self):
        a = ask.Asker(Cfg(**{"ask.system_prompt_file": "/no/such/prompt.txt",
                             "ask.command": [PY, "-c", "0"]}))
        with self.assertRaises(ValueError):
            a.submit("q", {}, None, "w")

    def test_guide_can_be_turned_off(self):
        self.assertNotIn("Where to look", ask.prompt("q", "{}", "w", guide=False))


class CommandTest(unittest.TestCase):
    def test_codex_preset_fills_paths_and_model(self):
        argv = ask.argv_for(Cfg(**{"ask.agent": "codex", "ask.model": "gpt-x"}), "/w", "/w/a.md")
        self.assertEqual(argv[:2], ["codex", "exec"])
        self.assertIn("/w/a.md", argv)
        self.assertEqual(argv[argv.index("-m") + 1], "gpt-x")
        self.assertEqual(argv[-1], "-")

    def test_no_model_flag_when_unset(self):
        self.assertNotIn("-m", ask.argv_for(Cfg(), "/w", "/w/a.md"))

    def test_unknown_agent_is_an_error(self):
        with self.assertRaises(ValueError):
            ask.argv_for(Cfg(**{"ask.agent": "nope"}), "/w", "/a")

    def test_missing_binary_is_reported(self):
        d = ask.describe(Cfg(**{"ask.command": ["no-such-agent-binary"]}))
        self.assertFalse(d["available"])
        self.assertIn("not found", d["error"])


class JobTest(unittest.TestCase):
    def test_stdout_is_the_answer_for_a_custom_command(self):
        cmd = [PY, "-c", "import sys; q=sys.stdin.read(); print('saw', 'who ran rm?' in q)"]
        a = ask.Asker(Cfg(**{"ask.command": cmd}))
        v = wait(a, a.submit("who ran rm?", PANELS, None, "last 24h")["id"])
        self.assertEqual(v["state"], "done", v)
        self.assertEqual(v["answer"], "saw True")

    def test_answer_file_and_event_stream(self):
        prog = ("import sys,json; sys.stdin.read();"
                "print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'hi'}}));"
                "open(sys.argv[1],'w').write('from the file')")
        a = ask.Asker(Cfg(**{"ask.command": [PY, "-c", prog, "{answer_file}"]}))
        v = wait(a, a.submit("q", {}, None, "w")["id"])
        self.assertEqual(v["answer"], "from the file")

    def test_timeout_kills_the_agent(self):
        a = ask.Asker(Cfg(**{"ask.command": [PY, "-c", "import time; time.sleep(30)"],
                             "ask.timeout_s": 1}))
        v = wait(a, a.submit("q", {}, None, "w")["id"], s=15)
        self.assertEqual(v["state"], "error")
        self.assertIn("timed out", v["error"])

    def test_turned_off_refuses(self):
        a = ask.Asker(Cfg(**{"ask.enabled": False, "ask.command": [PY, "-c", "0"]}))
        with self.assertRaises(ValueError):
            a.submit("q", {}, None, "w")

    def test_one_at_a_time(self):
        cmd = [PY, "-c", "import sys,time; sys.stdin.read(); time.sleep(0.6); print('ok')"]
        a = ask.Asker(Cfg(**{"ask.command": cmd, "ask.max_concurrent": 1}))
        j1 = a.submit("one", {}, None, "w")["id"]
        j2 = a.submit("two", {}, None, "w")["id"]
        time.sleep(0.3)
        self.assertEqual(a.view(j2)["state"], "queued")
        self.assertEqual(wait(a, j1)["state"], "done")
        self.assertEqual(wait(a, j2)["state"], "done")


class BalanceTest(unittest.TestCase):
    def test_the_largest_section_gives_way_first(self):
        panels = {"big": {"rows": [{"k": "x" * 80, "i": i} for i in range(400)]},
                  "small": {"rows": [{"i": i} for i in range(20)]}}
        s = json.loads(ask.snapshot(panels, budget_kb=1))
        self.assertEqual(len(s["panels"]["small"]["rows"]), 13)      # 12 + "... 8 more"
        self.assertLess(len(s["panels"]["big"]["rows"]), 13)

    def test_extra_sections_ride_along(self):
        s = json.loads(ask.snapshot({}, extra={"sessions": {"summary": {"agent": 3}}}))
        self.assertEqual(s["sessions"]["summary"]["agent"], 3)

if __name__ == "__main__":
    unittest.main()
