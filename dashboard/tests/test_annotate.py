"""Annotating tool calls with a model's purpose: the calls taken from a tree,
the prompt, the answer read back, the providers, and the answers kept.

    cd dashboard && .venv/bin/python -m unittest discover -s tests -q

No test reaches a real model: the HTTP providers talk to a local stand-in
server, and the CLI provider runs a small Python stand-in.
"""
import http.server
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rc_dashboard import annotate                                   # noqa: E402
from rc_dashboard import config as configmod                        # noqa: E402

PY = sys.executable


class Cfg:
    def __init__(self, **kv):
        self.kv = {"cache.dir": tempfile.mkdtemp(), "annotate.enabled": True,
                   "annotate.provider": "anthropic"}
        self.kv.update(kv)

    def get(self, dotted, default=None):
        return self.kv.get(dotted, default)


def wait(a, jid, s=20):
    end = time.time() + s
    while time.time() < end:
        v = a.view(jid)
        if v["state"] in ("done", "error"):
            return v
        time.sleep(0.02)
    raise AssertionError("job never finished")


TREE = {"row": {"agent_type": "claude_code"},
        "tree": {"k": "p", "cmd": "claude", "start": 0, "cwd": "/home/u/proj", "kids": [
            {"k": "p", "cmd": "zsh", "role": "tool-shell", "start": 20, "cmdline": "pytest -q"},
            {"k": "p", "cmd": "zsh", "role": "tool-shell", "start": 10, "cmdline": "git status",
             "cwd": "/home/u/proj/sub"},
            {"k": "p", "cmd": "rg", "start": 5, "args": "rg foo"},
            {"k": "g", "motif": ["zsh"], "start": 30, "samples": [
                {"cmdline": "squeue -u me", "n": 4}, {"args": "sleep 5", "n": 4}],
             "items": [{"k": "p", "cmd": "zsh", "start": 30, "cmdline": "squeue -u me"}]},
            {"k": "p", "cmd": "zsh", "role": "tool-shell", "start": 40, "cmdline": "pytest -q"},
        ]}}


def answer_for(user):
    """A model's answer: every call labelled by its command's first word."""
    out = {}
    for line in user.splitlines():
        if line.startswith("{"):
            c = json.loads(line)
            out[c["id"]] = {"purpose": "do " + c["cmd"].split()[0],
                            "kind": "test" if "pytest" in c["cmd"] else "made-up"}
    return json.dumps(out)


class CallsTest(unittest.TestCase):
    def test_distinct_tool_calls_in_start_order_with_their_folder(self):
        calls, total = annotate.calls_in(TREE)
        self.assertEqual([c["cmd"] for c in calls], ["git status", "pytest -q", "squeue -u me"])
        self.assertEqual(total, 3)
        self.assertEqual(calls[0]["cwd"], "/home/u/proj/sub")
        self.assertEqual(calls[1]["cwd"], "/home/u/proj")
        # a process that is not a tool call (`args` only) is not one
        self.assertNotIn("rg foo", [c["cmd"] for c in calls])

    def test_the_cap_keeps_the_oldest_and_counts_the_rest(self):
        calls, total = annotate.calls_in(TREE, cap=1)
        self.assertEqual([c["cmd"] for c in calls], ["git status"])
        self.assertEqual(total, 3)

    def test_the_prompt_names_the_agent_and_numbers_the_calls(self):
        calls, _ = annotate.calls_in(TREE)
        text = annotate.prompt_for(calls, "claude_code")
        self.assertIn("claude_code", text)
        rows = [json.loads(x) for x in text.splitlines() if x.startswith("{")]
        self.assertEqual([r["id"] for r in rows], ["1", "2", "3"])
        self.assertEqual(rows[0]["cwd"], "/home/u/proj/sub")


class ParseTest(unittest.TestCase):
    def test_reads_the_object_through_a_fence_and_prose(self):
        text = 'Here:\n```json\n{"1": {"purpose": "see the repo state", "kind": "GIT"},' \
               ' "2": {"purpose": "x", "kind": "nonsense"}}\n```'
        self.assertEqual(annotate.parse(text, 2),
                         {0: ("see the repo state", "git"), 1: ("x", "other")})

    def test_ids_out_of_range_and_empty_purposes_are_left_unlabelled(self):
        got = annotate.parse('{"0": "a", "3": "b", "2": {"purpose": " "}, "1": "plain"}', 2)
        self.assertEqual(got, {0: ("plain", "other")})

    def test_a_list_of_objects_is_read_too(self):
        got = annotate.parse('{"calls": [{"id": 1, "purpose": "p", "kind": "run"}]}', 1)
        self.assertEqual(got, {0: ("p", "run")})

    def test_not_json_is_nothing(self):
        self.assertEqual(annotate.parse("I cannot tell", 3), {})
        self.assertEqual(annotate.parse("{broken", 3), {})

    def test_a_purpose_is_masked_and_cut(self):
        got = annotate.parse(json.dumps({"1": {"purpose": "use token ghp_" + "a" * 36 + " " + "w" * 300}}), 1)
        p = got[0][0]
        self.assertNotIn("ghp_" + "a" * 36, p)
        self.assertLessEqual(len(p), annotate.PURPOSE_MAX)


class DescribeTest(unittest.TestCase):
    def test_off_by_default(self):
        self.assertFalse(configmod.defaults()["annotate"]["enabled"])
        self.assertFalse(annotate.describe(Cfg(**{"annotate.enabled": False}))["enabled"])

    def test_anthropic_needs_a_key_and_has_a_default_model(self):
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}):
            d = annotate.describe(Cfg())
        self.assertFalse(d["available"])
        self.assertIn("ANTHROPIC_API_KEY", d["error"])
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "k"}):
            d = annotate.describe(Cfg())
        self.assertTrue(d["available"], d["error"])
        self.assertEqual(d["model"], "claude-opus-5-5")

    def test_the_key_comes_from_the_named_variable_then_a_file(self):
        with mock.patch.dict(os.environ, {"MY_KEY": "from-env"}):
            self.assertEqual(annotate.api_key(Cfg(**{"annotate.api_key_env": "MY_KEY"})), "from-env")
        f = tempfile.NamedTemporaryFile("w", delete=False)
        f.write("from-file\n")
        f.close()
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}):
            self.assertEqual(annotate.api_key(Cfg(**{"annotate.api_key_file": f.name})), "from-file")

    def test_openai_needs_a_model_and_a_local_endpoint_needs_no_key(self):
        d = annotate.describe(Cfg(**{"annotate.provider": "openai"}))
        self.assertIn("annotate.model", d["error"])
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
            d = annotate.describe(Cfg(**{"annotate.provider": "openai", "annotate.model": "m",
                                         "annotate.base_url": "http://127.0.0.1:11434/v1"}))
        self.assertTrue(d["available"], d["error"])

    def test_an_unknown_provider_says_which_there_are(self):
        d = annotate.describe(Cfg(**{"annotate.provider": "gemini-ish"}))
        self.assertIn("anthropic", d["error"])

    def test_cli_presets_hand_the_model_their_own_way(self):
        argv = annotate.argv_for(Cfg(**{"annotate.provider": "claude", "annotate.model": "m"}), "/w", "/a")
        self.assertEqual(argv[:2], ["claude", "-p"])
        self.assertEqual(argv[-2:], ["--model", "m"])
        argv = annotate.argv_for(Cfg(**{"annotate.provider": "codex", "annotate.model": "m"}), "/w", "/a")
        self.assertIn("-m", argv)
        self.assertIn("/a", argv)

    def test_the_api_key_is_hidden_from_the_printed_config(self):
        self.assertIn("annotate.api_key", configmod.HIDDEN)


class _Handler(http.server.BaseHTTPRequestHandler):
    seen = []
    reply = None

    def do_POST(self):
        n = int(self.headers.get("content-length") or 0)
        body = json.loads(self.rfile.read(n))
        _Handler.seen.append((self.path, {k.lower(): v for k, v in self.headers.items()}, body))
        out = json.dumps(_Handler.reply(body)).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


class HttpProviderTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = "http://127.0.0.1:%d" % cls.srv.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        _Handler.seen.clear()
        # no proxy between the test and its own server
        self.env = mock.patch.dict(os.environ, {"NO_PROXY": "*", "no_proxy": "*"})
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def test_anthropic_messages_request_and_answer(self):
        _Handler.reply = lambda b: {"stop_reason": "end_turn", "content": [
            {"type": "thinking", "thinking": ""},
            {"type": "text", "text": answer_for(b["messages"][0]["content"])}]}
        cfg = Cfg(**{"annotate.base_url": self.base, "annotate.api_key": "sk-test"})
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}):
            text = annotate.complete(cfg, "sys", annotate.prompt_for([{"cmd": "ls"}]), 5)
        self.assertEqual(json.loads(text)["1"]["purpose"], "do ls")
        path, headers, body = _Handler.seen[0]
        self.assertEqual(path, "/v1/messages")
        self.assertEqual(headers.get("x-api-key"), "sk-test")
        self.assertEqual(headers.get("anthropic-version"), annotate.ANTHROPIC_VERSION)
        self.assertEqual(body["model"], "claude-opus-5-5")
        self.assertEqual(body["system"], "sys")
        self.assertEqual(body["output_config"], {"effort": "low"})
        self.assertNotIn("temperature", body)

    def test_anthropic_refusal_is_an_error_not_an_answer(self):
        _Handler.reply = lambda b: {"stop_reason": "refusal", "content": [],
                                    "stop_details": {"category": "cyber"}}
        cfg = Cfg(**{"annotate.base_url": self.base, "annotate.api_key": "k"})
        with self.assertRaisesRegex(annotate.ProviderError, "cyber"):
            annotate.complete(cfg, "s", "u", 5)

    def test_openai_compatible_chat_completions(self):
        _Handler.reply = lambda b: {"choices": [{"message": {
            "content": answer_for(b["messages"][1]["content"])}}]}
        cfg = Cfg(**{"annotate.provider": "openai", "annotate.model": "local-m",
                     "annotate.base_url": self.base + "/v1", "annotate.api_key": "k2"})
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
            text = annotate.complete(cfg, "sys", annotate.prompt_for([{"cmd": "make"}]), 5)
        self.assertEqual(json.loads(text)["1"]["purpose"], "do make")
        path, headers, body = _Handler.seen[0]
        self.assertEqual(path, "/v1/chat/completions")
        self.assertEqual(headers.get("authorization"), "Bearer k2")
        self.assertEqual(body["messages"][0], {"role": "system", "content": "sys"})
        self.assertEqual(body["model"], "local-m")


STAND_IN = r'''
import json, sys
text = sys.stdin.read()
out = {}
for line in text.splitlines():
    if line.startswith("{"):
        c = json.loads(line)
        out[c["id"]] = {"purpose": "cli " + c["cmd"], "kind": "run"}
print(json.dumps(out))
'''


class CliProviderTest(unittest.TestCase):
    def test_a_command_gets_the_prompt_on_stdin_and_answers_on_stdout(self):
        f = tempfile.NamedTemporaryFile("w", suffix=".py", delete=False)
        f.write(STAND_IN)
        f.close()
        cfg = Cfg(**{"annotate.provider": "command", "annotate.command": [PY, f.name]})
        self.assertTrue(annotate.describe(cfg)["available"])
        text = annotate.complete(cfg, "sys", annotate.prompt_for([{"cmd": "ls -la"}]), 10)
        self.assertEqual(annotate.parse(text, 1), {0: ("cli ls -la", "run")})


class AnnotatorTest(unittest.TestCase):
    def setUp(self):
        self.asked = []

        def fake(cfg, system, user, timeout):
            self.asked.append(user)
            return answer_for(user)

        self.fake = fake
        self.env = mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "k"})
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def test_asks_in_batches_keeps_answers_and_never_asks_twice(self):
        cfg = Cfg(**{"annotate.batch_size": 2})
        a = annotate.Annotator(cfg, complete_fn=self.fake)
        calls, _ = annotate.calls_in(TREE)
        j = wait(a, a.submit(calls, "claude_code")["id"])
        self.assertEqual(j["state"], "done", j["error"])
        self.assertEqual((j["total"], j["done"], j["unlabelled"]), (3, 3, 0))
        self.assertEqual(len(self.asked), 2)
        self.assertEqual(j["results"]["pytest -q"], ["do pytest", "test"])
        self.assertEqual(j["results"]["git status"], ["do git", "other"])
        # known now, and again from disk in a new process
        self.assertIsNone(a.submit(calls))
        b = annotate.Annotator(cfg, complete_fn=self.fake)
        self.assertEqual(b.known(["squeue -u me", "nope"]), {"squeue -u me": ["do squeue", "other"]})
        self.assertIsNone(b.submit(calls))
        self.assertEqual(len(self.asked), 2)

    def test_a_second_request_follows_the_job_already_asking(self):
        gate = threading.Event()

        def slow(cfg, system, user, timeout):
            gate.wait(10)
            return answer_for(user)

        a = annotate.Annotator(Cfg(), complete_fn=slow)
        calls, _ = annotate.calls_in(TREE)
        first = a.submit(calls[:2])
        # the same calls again, and one more: a job for the new one, and the
        # first job named to follow for the rest
        second = a.submit(calls)
        self.assertEqual(second["total"], 1)
        self.assertEqual(a.following(calls, exclude=second["id"]), [first["id"]])
        # nothing new: nothing started, and the first job is the one to follow
        self.assertIsNone(a.submit(calls[:2]))
        self.assertEqual(a.following(calls[:2]), [first["id"]])
        gate.set()
        self.assertEqual(wait(a, first["id"])["state"], "done")
        wait(a, second["id"])
        self.assertEqual(a.following(calls), [])

    def test_a_job_still_running_is_kept_however_many_wait(self):
        gate = threading.Event()

        def slow(cfg, system, user, timeout):
            gate.wait(10)
            return answer_for(user)

        a = annotate.Annotator(Cfg(), complete_fn=slow)
        first = a.submit([{"cmd": "cmd-0"}])
        for i in range(1, annotate.Annotator.KEEP_JOBS + 5):
            a.submit([{"cmd": "cmd-%d" % i}])
        self.assertIsNotNone(a.view(first["id"]))
        self.assertEqual(a.following([{"cmd": "cmd-0"}]), [first["id"]])
        gate.set()
        self.assertEqual(wait(a, first["id"])["state"], "done")
        # once ended, the oldest go as new ones come
        for jid in list(a.jobs):
            wait(a, jid)
        a.submit([{"cmd": "one-more"}])
        self.assertLessEqual(len(a.jobs), annotate.Annotator.KEEP_JOBS)
        self.assertIsNone(a.view(first["id"]))

    def test_the_cache_stays_within_its_cap_as_answers_come(self):
        cfg = Cfg(**{"annotate.cache_max": 10, "annotate.batch_size": 4})
        a = annotate.Annotator(cfg, complete_fn=self.fake)
        calls = [{"cmd": "cmd-%d" % i} for i in range(25)]
        wait(a, a.submit(calls)["id"])
        self.assertLessEqual(len(a._known), 10)
        with open(a._path(), encoding="utf-8") as fh:
            self.assertLessEqual(sum(1 for _ in fh), 10)
        # the newest are the ones kept, in memory and on disk
        self.assertEqual(a.known(["cmd-24", "cmd-0"]), {"cmd-24": ["do cmd-24", "other"]})
        b = annotate.Annotator(cfg, complete_fn=self.fake)
        self.assertIn("cmd-24", b.known(["cmd-24"]))
        self.assertLessEqual(len(b._table()), 10)

    def test_another_model_is_asked_afresh(self):
        cfg = Cfg()
        a = annotate.Annotator(cfg, complete_fn=self.fake)
        calls, _ = annotate.calls_in(TREE)
        wait(a, a.submit(calls)["id"])
        cfg.kv["annotate.model"] = "claude-haiku-5-5"
        b = annotate.Annotator(cfg, complete_fn=self.fake)
        self.assertEqual(b.known([c["cmd"] for c in calls]), {})

    def test_a_changed_prompt_is_asked_afresh(self):
        f = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False)
        f.write("Answer in English.")
        f.close()
        cfg = Cfg(**{"annotate.system_prompt_file": f.name})
        a = annotate.Annotator(cfg, complete_fn=self.fake)
        calls, _ = annotate.calls_in(TREE)
        wait(a, a.submit(calls)["id"])
        self.assertEqual(len(a.known([c["cmd"] for c in calls])), 3)
        # the file is read on every question: an edit is a new prompt
        with open(f.name, "w") as fh:
            fh.write("Réponds en français.")
        b = annotate.Annotator(cfg, complete_fn=self.fake)
        self.assertEqual(b.known([c["cmd"] for c in calls]), {})
        self.assertIsNotNone(b.submit(calls))
        # and the inline prompt counts as well
        cfg2 = Cfg(**{"cache.dir": cfg.kv["cache.dir"], "annotate.system_prompt": "Be brief."})
        self.assertEqual(annotate.Annotator(cfg2, complete_fn=self.fake).known(["git status"]), {})

    def test_a_call_the_answer_leaves_out_is_counted_not_filled_in(self):
        a = annotate.Annotator(Cfg(), complete_fn=lambda *a: '{"1": "only the first"}')
        calls, _ = annotate.calls_in(TREE)
        j = wait(a, a.submit(calls)["id"])
        self.assertEqual(j["unlabelled"], 2)
        self.assertEqual(list(j["results"]), ["git status"])

    def test_a_provider_error_ends_the_job_with_it(self):
        def boom(*a):
            raise annotate.ProviderError("401 bad key")
        a = annotate.Annotator(Cfg(), complete_fn=boom)
        calls, _ = annotate.calls_in(TREE)
        j = wait(a, a.submit(calls)["id"])
        self.assertEqual(j["state"], "error")
        self.assertIn("bad key", j["error"])
        # nothing is held as asked: a second try asks again
        self.assertIsNotNone(a.submit(calls))

    def test_off_or_unavailable_refuses(self):
        with self.assertRaisesRegex(ValueError, "turned off"):
            annotate.Annotator(Cfg(**{"annotate.enabled": False})).submit([{"cmd": "x"}])
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}):
            with self.assertRaisesRegex(ValueError, "API key"):
                annotate.Annotator(Cfg()).submit([{"cmd": "x"}])


if __name__ == "__main__":
    unittest.main()
