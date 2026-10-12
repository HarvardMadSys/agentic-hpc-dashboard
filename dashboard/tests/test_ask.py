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


class LocalScopeTest(unittest.TestCase):
    """A question about one panel: its own data, longer lists, its own notes."""

    def test_one_panel_keeps_its_notes_and_longer_lists(self):
        panel = {"risk": {"rows": [{"i": i} for i in range(40)], "basis": "how it counts",
                          "series": [1, 2, 3]}}
        s = json.loads(ask.snapshot(panel, steps=ask.LOCAL_STEPS, keep=ask.LOCAL_KEEP))
        self.assertEqual(len(s["panels"]["risk"]["rows"]), 40)
        self.assertEqual(s["panels"]["risk"]["basis"], "how it counts")
        self.assertNotIn("series", s["panels"]["risk"])
        whole = json.loads(ask.snapshot(panel))
        self.assertNotIn("basis", whole["panels"]["risk"])

    def test_a_guide_of_its_own_replaces_the_window_guide(self):
        g = ask.panel_guide("Worst individual processes", "Resources", ["panels.resources"])
        t = ask.prompt("q", "{}", "last 24h", guide_text=g)
        self.assertIn('"Worst individual processes" on the Resources tab', t)
        self.assertIn("panels.resources", t)
        self.assertNotIn("live_now.users.rows", t)        # the whole-window guide
        self.assertIn("live_now.users.rows", ask.prompt("q", "{}", "last 24h"))

    def test_the_job_says_what_it_is_about_and_the_agent_gets_the_guide(self):
        out = tempfile.mktemp()
        cmd = [PY, "-c", "import sys; open(sys.argv[1], 'w').write(sys.stdin.read()); print('ok')",
               out]
        a = ask.Asker(Cfg(**{"ask.command": cmd}))
        scope = {"about": "Top tools", "guide": ask.panel_guide("Top tools", "Agents", ["x"]),
                 "steps": ask.LOCAL_STEPS, "keep": ask.LOCAL_KEEP}
        j = a.submit("q", {"tool_mix": {"head": [{"b": "git"}] * 30}}, None, "w", scope=scope)
        self.assertEqual(j["about"], "Top tools")
        self.assertEqual(wait(a, j["id"])["state"], "done")
        with open(out) as fh:
            sent = fh.read()
        self.assertIn('"Top tools" on the Agents tab', sent)
        self.assertEqual(json.loads(sent.split("<snapshot>")[1].split("</snapshot>")[0])
                         ["panels"]["tool_mix"]["head"].__len__(), 30)
        self.assertIsNone(a.submit("q", {}, None, "w")["about"])


def proc(pid, cmd, kids=(), n=None, cpu=0.01, sub=None, code=0, start=0.0, **kv):
    d = {"k": "p", "pid": pid, "cmd": cmd, "start": start, "dur": 0.5, "cpu": cpu,
         "n": n or 1 + sum(k.get("n") or 0 for k in kids), "sub_cpu": sub or cpu,
         "ev": "exit", "exit_code": code}
    if kids:
        d["kids"] = list(kids)
    d.update(kv)
    return d


def session_body(n_kids=14):
    kids = [proc(10 + i, "sh", kids=[proc(100 + i, "ls", code=2)], sub=0.02 + i / 1000,
                 start=float(i), args="sh -c 'ls /secret/dir'") for i in range(n_kids)]
    kids.append({"k": "g", "motif": ["git"], "period": 1, "reps": 40, "count": 40, "n": 40,
                 "start": 50.0, "sub_cpu": 0.4, "exits": {"0": 39, "128": 1}, "signals": {}})
    kids.append(proc(99, "python", kids=[proc(98, "nvcc")], cpu=30.0, sub=42.0, start=60.0))
    kids.append({"k": "agg", "why": "hold", "n": 5000, "sub_cpu": 7.0,
                 "cmds": [["realpath", 4000], ["git", 1000]]})
    root = proc(1, "server", kids=kids, cpu=None, sub=60.0, role="infra",
                agent_type="claude_code", ev=None, exit_code=None)
    root["n"] = 5100
    return {
        "row": {"sid": "h|2026-10-12|agent|apid:1", "user": "alice", "agent_type": "claude_code",
                "actor3": "agent", "n_events": 5100, "cpu_s": 60.0, "wall_s": 7200, "first": 1e9,
                "last": 1e9 + 7200, "tool_rle": [["git", 25], ["ls", 1], ["python", 2]],
                "purpose_rle": [["git", 25], ["filesearch", 1], ["compute", 2]],
                "chain_truncated": False},
        "nav": {"first_tool": {"t": 1e9, "cmd": "git"}, "busiest": {"t": 1e9 + 60, "n": 300},
                "longest": {"t": 1e9 + 100, "cmd": "python", "dur": 900.0},
                "failures": {"n": 15, "listed": 15,
                             "items": [[1e9 + i, 1e9 + i, 100 + i, "ls", 2, None] for i in range(14)]
                             + [[1e9 + 99, 1e9 + 99, 7, "git", 128, None]]}},
        "scan": {"records": 5100, "first": 1e9, "last": 1e9 + 7200, "stopped": None},
        "bounds": {"itemised": 100, "counted": 5000},
        "tree": root,
    }


class SessionDigestTest(unittest.TestCase):
    """A session for a question: totals, sequence, landmarks, and an outline."""

    def test_totals_sequence_and_landmarks(self):
        d = ask.session_digest(session_body())
        self.assertEqual(d["session"]["user"], "alice")
        self.assertEqual(d["session"]["wall"], "2.0 h")
        self.assertEqual(d["tools"], "git x25 > ls > python x2")
        self.assertEqual(d["purposes"]["share_pct"]["git"], round(100 * 25 / 28, 1))
        self.assertEqual(d["landmarks"]["failures"]["total"], 15)
        self.assertEqual(d["landmarks"]["failures"]["by_command"][0], "ls exit 2 x14")
        self.assertEqual(d["landmarks"]["longest_process"]["dur"], "15.0 min")
        self.assertEqual(d["read"]["counted"], 5000)

    def test_the_outline_counts_children_then_opens_a_varied_few(self):
        lines = ask.session_digest(session_body())["tree"]
        self.assertTrue(lines[0].startswith("server [claude_code, infra]"))
        census = next(x for x in lines if "children by command" in x)
        # a child's own exit, not its children's: each sh exited 0, its ls failed
        self.assertIn("git x40, 1 failed; sh x14 (28 processes);", census)
        opened = [x for x in lines if x.startswith("  ") and not x.startswith("   ")
                  and "children" not in x]
        # the costliest subtree is opened; ten copies of one helper are not
        self.assertTrue(any(x.startswith("  python") for x in opened))
        self.assertLessEqual(sum(x.startswith("  sh") for x in opened), 2)
        self.assertTrue(any(x.startswith("    nvcc") for x in lines))
        self.assertTrue(any("+5000 more processes counted" in x for x in lines))
        self.assertTrue(any("git x40 in a row" in x and "1 failed" in x for x in lines))

    def test_command_lines_only_when_asked(self):
        plain = json.dumps(ask.session_digest(session_body()))
        self.assertNotIn("/secret/dir", plain)
        self.assertIn("/secret/dir", json.dumps(ask.session_digest(session_body(), with_argv=True)))

    def test_a_long_outline_stops_and_says_so(self):
        lines = ask.session_digest(session_body(), max_lines=5)["tree"]
        self.assertEqual(lines[-1], "... the outline stops at 5 lines")

if __name__ == "__main__":
    unittest.main()
