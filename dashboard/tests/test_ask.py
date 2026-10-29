"""Chat with an LLM: the snapshot it is given, the command it runs, the jobs.

    cd dashboard && .venv/bin/python -m unittest discover -s tests -q

No test runs a real agent: `ask.command` points at a small Python stand-in.
"""
import json
import os
import re
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rc_dashboard import ask                                         # noqa: E402
from rc_dashboard.aggregator import Aggregator                       # noqa: E402

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


class SuggestionsTest(unittest.TestCase):
    """The operator's suggestions: an answer asked for as JSON, read leniently
    and checked strictly."""

    ANSWER = json.dumps({"summary": "A quiet day with one noisy poller.", "suggestions": [
        {"priority": "low", "title": "Note the IDE share", "action": "nothing yet",
         "evidence": "IDE 0.3% of processes", "users": [], "panel": "Overview"},
        {"priority": "HIGH", "title": "Ask user-1a2b3c4d to stop polling squeue",
         "action": "email them about --wait", "evidence": "13,340 queue checks in 24h",
         "users": ["user-1a2b3c4d", 7], "panel": "Sessions at the scheduler"},
        {"priority": "urgent", "title": "Check NFS", "evidence": "cp 1,971 s in D-state"},
        {"title": ""}, "not an object"]})

    def test_a_clean_answer_is_read_most_urgent_first(self):
        got = ask.parse_suggestions(self.ANSWER)
        self.assertEqual(got["summary"], "A quiet day with one noisy poller.")
        self.assertEqual([x["priority"] for x in got["suggestions"]], ["high", "medium", "low"])
        top = got["suggestions"][0]
        self.assertEqual(top["users"], ["user-1a2b3c4d"])           # a non-string dropped
        self.assertEqual(got["suggestions"][1]["title"], "Check NFS")  # unknown priority: medium
        self.assertIsNone(got["suggestions"][1]["action"])

    def test_a_fenced_or_prefaced_answer_still_reads(self):
        for text in ("```json\n%s\n```" % self.ANSWER, "Here you go: %s" % self.ANSWER):
            self.assertEqual(len(ask.parse_suggestions(text)["suggestions"]), 3)

    def test_anything_else_is_not_suggestions(self):
        for text in ("", "Nothing to report.", "{not json}", '{"summary": "x"}',
                     '{"suggestions": "none"}'):
            self.assertIsNone(ask.parse_suggestions(text), text)

    def test_at_most_eight_and_every_field_cut_to_size(self):
        many = {"suggestions": [{"title": "t%d" % i, "action": "a" * 5000} for i in range(20)]}
        got = ask.parse_suggestions(json.dumps(many))["suggestions"]
        self.assertEqual(len(got), 8)
        self.assertLessEqual(len(got[0]["action"]), 600)
        self.assertTrue(got[0]["action"].endswith("…"))
        paths = "rm -rf " + ", ".join("/tmp/claude-1000/project-%d/scratchpad/stage" % i for i in range(30))
        cut = ask.parse_suggestions(json.dumps({"suggestions": [{"title": "t", "action": paths}]}))
        action = cut["suggestions"][0]["action"]
        self.assertTrue(action.endswith("/scratchpad/stage…"), action[-40:])   # a whole path, then …

    def test_secrets_in_an_answer_are_masked(self):
        text = json.dumps({"suggestions": [{"title": "rotate it",
                                            "evidence": "token sk-" + "a" * 40}]})
        self.assertNotIn("a" * 40, json.dumps(ask.parse_suggestions(text)))

    def test_a_panel_is_placed_on_its_tab_by_key_or_by_name(self):
        for name, want in (("panels.risk", ("risk", "Risk & Warning")),
                           ("live_now.users", ("live", "Active users")),
                           ("node", ("live", "Login nodes right now")),
                           ("Resources · I/O", ("resources", "Resources")),
                           ("Risk & Warning", ("risk", "Risk & Warning")),
                           ("somewhere else", (None, "somewhere else"))):
            self.assertEqual(ask.panel_tab(name), want, name)
        got = ask.parse_suggestions(json.dumps({"suggestions": [{"title": "t", "panel": "panels.sandbox"}]}))
        self.assertEqual((got["suggestions"][0]["tab"], got["suggestions"][0]["panel"]),
                         ("internal", "Internal · Sandbox"))

    def test_the_paths_a_command_line_names(self):
        for args, want in (
                ("rm -rf /n/holylabs/x/Lab/ann/tmp ./rel /dev/null", ["/n/holylabs/x/Lab/ann/tmp"]),
                ('bash -lc "cd /scratch/a && rm -rf /scratch/a/build 2>/dev/null"',
                 ["/scratch/a", "/scratch/a/build"]),
                ("wget -O /home/ann/data.tgz https://example.com/data.tgz", ["/home/ann/data.tgz"]),
                ("/usr/bin/rm -rf ~/old", ["~/old"]),          # not the program itself
                ("rm -rf /", []), ("", []), (None, [])):
            self.assertEqual(ask.paths_in(args), want, args)
        self.assertEqual(len(ask.paths_in(" ".join("/p%d" % i for i in range(20)))), 5)

    def test_who_ran_each_flagged_command_is_counted_from_its_listed_runs(self):
        risk = {"dangerous": {"rows": [
                    {"id": "rm_rf_shared", "command": "rm -rf", "n": 49,
                     "instances": [{"user": "ann", "args": "rm -rf /n/lab/shared/a"}] * 9
                     + [{"user": "bob", "args": "rm -rf /n/lab/shared/b"}] * 3 + [{"user": None}]},
                    {"id": "quiet", "command": "x", "n": 0, "instances": []}]},
                "secrets": {"rows": [{"id": "redis_password", "pattern": "redis -a", "n": 1,
                                      "instances": [{"user": "cy"}]}]},
                "login_compute": {"top_procs": [{"user": "ann"}, {"user": "dee"}]}}
        who = ask.risk_who(risk)
        self.assertEqual(who["rm_rf_shared"], {"command": "rm -rf", "n": 49, "listed": 13,
                                              "users": {"ann": 9, "bob": 3},
                                              "paths": {"/n/lab/shared/a": 9, "/n/lab/shared/b": 3}})
        self.assertEqual(who["redis_password"]["users"], {"cy": 1})
        self.assertNotIn("paths", who["redis_password"])     # a credential's runs carry no argv
        self.assertEqual(who["login_compute"]["users"], {"ann": 1, "dee": 1})
        self.assertNotIn("quiet", who)
        self.assertEqual(ask.risk_who(None), {})

    def test_a_suggestions_job_carries_them_and_a_chat_job_does_not(self):
        cmd = [PY, "-c", "import sys; sys.stdin.read(); print(sys.argv[1])", self.ANSWER]
        a = ask.Asker(Cfg(**{"ask.command": cmd}))
        j = a.submit(ask.SUGGEST_QUESTION, {}, None, "w",
                     scope={"about": "suggestions for the operator", "parse": "suggestions",
                            "guide": ask.GUIDE + "\n\n" + ask.OPERATOR_GUIDE})
        done = wait(a, j["id"])
        self.assertEqual(done["suggestions"]["suggestions"][0]["priority"], "high")
        self.assertIsNone(wait(a, a.submit("q", {}, None, "w")["id"])["suggestions"])


class DoneTest(unittest.TestCase):
    """Suggestions marked done: kept by the service, from its own answers,
    until undone, and told to the next pass."""

    def setUp(self):
        self.cfg = Cfg()
        self.a = ask.Asker(self.cfg)
        self.a.jobs["j1"] = {"id": "j1", "window": "24h", "suggestions": ask.parse_suggestions(
            SuggestionsTest.ANSWER)}
        self.a.jobs["chat"] = {"id": "chat", "window": "24h", "suggestions": None}

    def test_marking_keeps_the_services_own_suggestion_newest_first(self):
        self.a.mark_done("j1", 1)
        items = self.a.mark_done("j1", 0)["items"]
        self.assertEqual([(x["job"], x["index"]) for x in items], [("j1", 0), ("j1", 1)])
        self.assertEqual(items[0]["title"], "Ask user-1a2b3c4d to stop polling squeue")
        self.assertEqual(items[0]["window"], "24h")
        self.assertEqual(len(self.a.mark_done("j1", 0)["items"]), 2)      # once, not twice

    def test_what_cannot_be_marked(self):
        with self.assertRaises(LookupError):
            self.a.mark_done("gone", 0)
        for jid, i in (("j1", 9), ("j1", -1), ("j1", "0"), ("j1", True), ("chat", 0)):
            with self.assertRaises(ValueError, msg=(jid, i)):
                self.a.mark_done(jid, i)

    def test_it_lasts_on_disk_until_undone(self):
        did = self.a.mark_done("j1", 0)["items"][0]["id"]
        again = ask.Asker(self.cfg)                       # a restart
        self.assertEqual([x["id"] for x in again.done_list()["items"]], [did])
        self.assertEqual(again.undo_done(did)["items"], [])
        self.assertEqual(ask.Asker(self.cfg).done_list()["items"], [])
        with self.assertRaises(LookupError):
            again.undo_done(did)
        path = self.a._done_path()
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)

    def test_only_the_newest_are_kept(self):
        self.a.DONE_KEEP = 2
        for i in range(3):
            self.a.mark_done("j1", i)
        self.assertEqual([x["index"] for x in self.a.done_list()["items"]], [2, 1])

    def test_the_next_pass_is_told_what_was_done(self):
        self.a.mark_done("j1", 0)
        told = self.a.done_for_prompt()
        self.assertEqual(told[0]["title"], "Ask user-1a2b3c4d to stop polling squeue")
        self.assertEqual(told[0]["users"], ["user-1a2b3c4d"])
        self.assertTrue(told[0]["marked_done"])
        self.assertNotIn("job", told[0])

    def test_the_next_pass_is_not_handed_the_old_citations(self):
        self.a.jobs["j2"] = {"id": "j2", "window": "24h", "suggestions": ask.parse_suggestions(
            json.dumps({"summary": None, "suggestions": [
                {"priority": "high", "title": "Stop rm", "action": "email them [c4, c7] now",
                 "evidence": "188 runs [c4]"}]}))}
        self.a.mark_done("j2", 0)
        self.assertEqual(self.a.done_for_prompt()[0]["action"], "email them now")


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


REF = re.compile(r"\[([a-z]{1,2}\d+)\] ")


def echo(answer):
    """An agent that reads the prompt and answers `answer`."""
    return [PY, "-c", "import sys; sys.stdin.read(); print(sys.argv[1])", answer]


class CiteTest(unittest.TestCase):
    """Citations: every section, sub-section, and row the model reads carries a
    ref; the answer cites them; and the job keeps what each cited ref was --
    where it sits, the data the model saw, and where the page shows it."""

    def snap(self, panels=PANELS, **kv):
        cites = {}
        return json.loads(ask.snapshot(panels, cites=cites, **kv)), cites

    def test_sections_sub_sections_and_rows_carry_a_ref_first(self):
        s, cites = self.snap()
        tm, risk = s["panels"]["tool_mix"], s["panels"]["risk"]
        self.assertEqual(list(tm)[0], "ref")
        self.assertEqual(list(risk["dangerous"])[0], "ref")
        row = risk["dangerous"]["rows"][0]
        self.assertEqual(list(row)[0], "ref")
        rows = [r for r in tm["by_class"]["agent"]["rows"] if isinstance(r, dict)]
        refs = [r["ref"] for r in rows]
        self.assertEqual(len(rows), 12)
        self.assertEqual(len(set(refs)), 12)
        # one prefix per section: tool_mix's refs never collide with risk's
        self.assertNotEqual(re.sub(r"\d", "", tm["ref"]), re.sub(r"\d", "", risk["ref"]))
        c = cites[row["ref"]]
        self.assertEqual(c["path"], ["panels", "risk", "dangerous", "rows", 0])
        self.assertEqual(c["obj"]["command"], "rm -rf")
        self.assertNotIn("args", c["obj"])          # what the model saw, not the record

    def test_a_dict_three_levels_down_is_cited_by_its_parent(self):
        s, _ = self.snap({"resources": {"by_class": {"agent": {"quants": {"p50": 1}}}}})
        agent = s["panels"]["resources"]["by_class"]["agent"]
        self.assertIn("ref", agent)
        self.assertNotIn("ref", agent["quants"])

    def test_link_hints_ride_with_the_ref_and_never_reach_the_model(self):
        cites = {}
        text = ask.snapshot({"x": {"rows": [{"user": "alice",
                                              "_go": {"sid": "h|d|agent|apid:1", "t": 5}}]}},
                            cites=cites)
        self.assertNotIn("_go", text)
        self.assertNotIn("apid:1", text)
        row = json.loads(text)["panels"]["x"]["rows"][0]
        # carried whole: its `t` is not the bulky per-bin field compaction drops
        self.assertEqual(cites[row["ref"]]["go"], {"sid": "h|d|agent|apid:1", "t": 5})

    def test_outline_lines_and_landmarks_name_their_process(self):
        cites = {}
        s = json.loads(ask.snapshot(None, extra=ask.session_digest(session_body()),
                                    steps=ask.SESSION_STEPS, cites=cites))
        self.assertNotIn("_go_tree", s)
        first = REF.match(s["tree"][0])
        self.assertIsNotNone(first, s["tree"][0])
        self.assertTrue(s["tree"][0][len(first.group(0)):].startswith("server [claude_code"))
        self.assertEqual(cites[first.group(1)]["go"]["jump"], {"t": 0.0, "by": "start", "pid": 1})
        nvcc = next(x for x in s["tree"] if "nvcc" in x)
        self.assertRegex(nvcc, r"^    \[[a-z]{1,2}\d+\] nvcc")     # the indent stays in front
        census = next(x for x in s["tree"] if "children by command" in x)
        self.assertNotRegex(census, r"\[[a-z]{1,2}\d+\]")          # not a process: no ref
        lm = s["landmarks"]
        self.assertEqual(cites[lm["longest_process"]["ref"]]["go"]["jump"]["t"], 1e9 + 100)
        fail = REF.match(lm["failures"]["first"][0])
        self.assertEqual(cites[fail.group(1)]["go"]["jump"],
                         {"t": 1e9, "by": "start", "pid": 100})
        self.assertEqual(cites[s["session"]["ref"]]["go"], {"sid": "h|2026-10-12|agent|apid:1"})

    def test_a_ref_the_budget_cut_off_is_not_kept(self):
        # keys compaction cannot shorten: the text is cut at the budget
        cites = {}
        text = ask.snapshot({"p%d" % i: {"k" * 300: i} for i in range(12)}, budget_kb=2,
                            cites=cites)
        self.assertEqual(len(text), 2 * 1024)
        self.assertTrue(0 < len(cites) < 12)
        for r in cites:
            self.assertIn('"ref":"%s"' % r, text)

    def test_the_answer_keeps_what_it_cites_and_nothing_made_up(self):
        s, _ = self.snap()
        row = s["panels"]["risk"]["dangerous"]["rows"][0]["ref"]
        sec = s["panels"]["tool_mix"]["ref"]
        answer = "One rm -rf [%s]; see [%s, zz99] and [zz98]." % (row, sec)
        a = ask.Asker(Cfg(**{"ask.command": echo(answer)}))
        v = wait(a, a.submit("q", PANELS, None, "w")["id"])
        self.assertEqual(v["answer"], answer)
        self.assertEqual(set(v["cites"]), {row, sec})
        c = v["cites"][row]
        self.assertEqual(c["path"], ["panels", "risk", "dangerous", "rows", 0])
        self.assertEqual(c["data"]["command"], "rm -rf")
        self.assertNotIn("ref", c["data"])
        self.assertNotIn("obj", c)

    def test_the_agent_is_asked_to_cite_unless_turned_off(self):
        for on in (True, False):
            out = tempfile.mktemp()
            cmd = [PY, "-c", "import sys; open(sys.argv[1], 'w').write(sys.stdin.read());"
                             "print('ok [a1]')", out]
            a = ask.Asker(Cfg(**{"ask.command": cmd, "ask.citations": on}))
            v = wait(a, a.submit("q", PANELS, None, "w")["id"])
            with open(out) as fh:
                sent = fh.read()
            self.assertEqual(ask.CITE_GUIDE in sent, on)
            self.assertEqual('"ref":"a1"' in sent, on)
            self.assertEqual(bool(v["cites"]), on)

    def test_an_earlier_answer_goes_back_without_its_citations(self):
        t = ask.prompt("q", "{}", "w", history=[{"q": "before?", "a": "yes [b7] and [c2, c3]."}],
                       cite_text=ask.CITE_GUIDE)
        self.assertIn(ask.CITE_GUIDE, t)
        self.assertIn("Your earlier answer: yes and.", t)

    def test_suggestions_cite_inside_their_fields(self):
        s, _ = self.snap()
        ref = s["panels"]["risk"]["dangerous"]["rows"][0]["ref"]
        answer = json.dumps({"summary": "s", "suggestions": [
            {"priority": "high", "title": "Stop rm", "action": "email them",
             "evidence": "1 rm -rf on login [%s]" % ref, "users": [], "panel": "Risk & Warning"}]})
        a = ask.Asker(Cfg(**{"ask.command": echo(answer)}))
        v = wait(a, a.submit(ask.SUGGEST_QUESTION, PANELS, None, "w",
                             scope={"parse": "suggestions"})["id"])
        self.assertIn("[%s]" % ref, v["suggestions"]["suggestions"][0]["evidence"])
        self.assertEqual(set(v["cites"]), {ref})

    def test_a_preview_is_small_and_says_what_it_left_out(self):
        p = ask.preview({"ref": "a1", "user": "alice", "note": "y" * 1000,
                         "by_class": {"ref": "a2", "agent": 3, "human": {"n": 1}},
                         "rows": [{"i": i} for i in range(12)] + ["... 38 more"],
                         "tags": ["a", "b"]})
        self.assertNotIn("ref", p)
        self.assertEqual(p["user"], "alice")
        self.assertLessEqual(len(p["note"]), 241)
        self.assertEqual(p["by_class"], {"agent": 3, "human": {"n": 1}})
        self.assertEqual(p["rows"], "50 rows")
        self.assertEqual(p["tags"], ["a", "b"])
        self.assertEqual(ask.preview("  [a3] git status: dur 1 s"), "git status: dur 1 s")

    def test_the_job_says_which_view_its_links_open(self):
        a = ask.Asker(Cfg(**{"ask.command": echo("ok")}))
        view = {"window_min": 60, "users": ["alice"], "atypes": [], "cls": ["agent"], "sid": None}
        self.assertEqual(a.submit("q", {}, None, "w", scope={"view": view})["view"], view)
        self.assertIsNone(a.submit("q", {}, None, "w")["view"])


class AggregatorCiteTest(unittest.TestCase):
    """What the aggregator hands the snapshot so a cited row can be opened."""

    def agg(self):
        agg = Aggregator.__new__(Aggregator)
        agg.anon = None
        agg.window = lambda m: {"minutes": 60, "label": "last 1 h"}
        agg.panels = lambda m, v=None: {"panels": {}, "facets": None,
                                        "window_status": {"state": "ready"}}
        agg.live_payload = lambda m: {}
        agg.trajectories = lambda m, v=None: {
            "rows": {"h|d|agent|apid:1": {"sid": "h|d|agent|apid:1", "user": "alice",
                                          "n_events": 9}},
            "ranks": {"events": ["h|d|agent|apid:1"], "recent": ["h|d|agent|apid:1"]},
            "summary": {}}
        agg.asker = mock.Mock()
        agg.asker.submit.return_value = {"id": "x"}
        return agg

    def test_the_busiest_sessions_carry_their_sid_as_a_hint(self):
        agg = self.agg()
        agg.ask("q", 60)
        extra, scope = agg.asker.submit.call_args[0][5:7]
        row = extra["sessions"]["busiest"][0]
        self.assertNotIn("sid", row)
        self.assertEqual(row["_go"], {"sid": "h|d|agent|apid:1"})
        self.assertEqual(scope["view"]["window_min"], 60)

    def test_a_panel_question_states_its_view(self):
        agg = self.agg()
        agg._ask_panel("q", 60, (), {"parts": [{"panel": "trajectories"}],
                                     "classes": ["agent"]}, None)
        scope = agg.asker.submit.call_args[0][6]
        self.assertEqual(scope["view"], {"window_min": 60, "users": [], "atypes": [],
                                         "cls": ["agent"], "sid": None})

if __name__ == "__main__":
    unittest.main()
