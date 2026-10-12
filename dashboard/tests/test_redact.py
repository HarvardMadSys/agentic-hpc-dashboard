"""Credential masking: what is masked, what is left alone, and every exit.

    cd dashboard && .venv/bin/python -m unittest discover -s tests -q
"""
import re
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from rc_dashboard import redact                                    # noqa: E402
from rc_dashboard.export import make_predicate, parse_filters       # noqa: E402
from rc_dashboard.reducers.live import LiveReducer                  # noqa: E402
from rc_dashboard.reducers.risk import SECRETS                      # noqa: E402
from rc_dashboard.windows import PanelView                          # noqa: E402

MASK = redact.MASK

# (command line, the secret in it, a piece of structure that must survive).
# The values are made up; the shapes are the real ones.
MASKED = [
    ("python train.py --hf-token hf_" + "a" * 34, "hf_" + "a" * 34, "--hf-token"),
    ("OPENAI_API_KEY=sk-proj-" + "b" * 30 + " python run.py", "sk-proj-" + "b" * 30,
     "OPENAI_API_KEY="),
    ('curl -H "Authorization: Bearer abc.def.ghijkl" https://api.x/v1', "abc.def.ghijkl",
     "Authorization: Bearer"),
    ('curl -H "X-Api-Key: 9f8e7d6c5b4a" https://api.x', "9f8e7d6c5b4a", "X-Api-Key:"),
    ("git clone https://alice:hunter2pw@github.com/org/repo", "hunter2pw", "https://alice:"),
    ("git clone https://ghp_" + "c" * 36 + "@github.com/org/repo", "ghp_" + "c" * 36,
     "@github.com"),
    ("curl -u bob:s3cretpass https://x.example", "s3cretpass", "-u bob:"),
    ("app --password=correcthorse", "correcthorse", "--password="),
    ("app --api-key 0123456789abcdef", "0123456789abcdef", "--api-key"),
    ("export AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMIK7MDENG", "wJalrXUtnFEMIK7MDENG",
     "AWS_SECRET_ACCESS_KEY="),
    ("PGPASSWORD=pgsecret99 psql -h db -p 5432", "pgsecret99", "PGPASSWORD="),
    ("mysql -u root -pdbsecret42 prod", "dbsecret42", "mysql -u root -p"),
    ("redis-cli -h cache -a redissecret ping", "redissecret", "-a"),
    ("sshpass -p sshsecret ssh host", "sshsecret", "sshpass -p"),
    ("curl 'https://s3.x/obj?X-Amz-Signature=deadbeefcafe&X-Amz-Date=1'", "deadbeefcafe",
     "X-Amz-Date=1"),
    ("""curl -d '{"password": "jsonsecret"}' https://x""", "jsonsecret", '"password"'),
    ("aws configure set aws_access_key_id AKIAABCDEFGHIJKLMNOP", "AKIAABCDEFGHIJKLMNOP",
     "aws configure"),
    ("echo -----BEGIN RSA PRIVATE KEY-----\nMIIEsecretbody\n-----END RSA PRIVATE KEY----- > k",
     "MIIEsecretbody", "> k"),
    ("slack-notify xoxb-1234567890-abcdefghij", "xoxb-1234567890-abcdefghij", "slack-notify"),
]

# Real command lines, from real login-node feeds, that must come back as they were.
UNCHANGED = [
    "gh auth git-credential get",
    "git -c credential.helper= -c credential.helper=!gh auth git-credential get",
    "python -u train.py --max-tokens 4096 --tokenizer gpt2",
    "psql -p 5432 -U me mydb",
    "redis-cli -p 6379 ping",
    "mkdir -p /scratch/run && uv -p 3.12 sync",
    "export PATH=/usr/bin:$PATH",
    "app --token $HF_TOKEN --password \"$DB_PW\"",
    "curl https://example.com/a?b=c&page=2",
    "node -e 'for (const c of s) { token = tokens[i]; }'",
    "claude --permission-mode bypassPermissions -p hello",
    "ssh -o PubkeyAuthentication=no host",
    "git log --format=%H -n 5",
]


class Cfg:
    def get(self, dotted, default=None):
        return default


class RuleTest(unittest.TestCase):

    def test_each_secret_is_masked_and_its_context_kept(self):
        for line, secret, kept in MASKED:
            with self.subTest(line=line[:40]):
                out = redact.redact(line)
                self.assertNotIn(secret, out)
                self.assertIn(MASK, out)
                self.assertIn(kept, out)

    def test_ordinary_commands_are_left_alone(self):
        for line in UNCHANGED:
            with self.subTest(line=line[:40]):
                self.assertEqual(redact.redact(line), line)

    def test_masking_twice_changes_nothing(self):
        for line, _s, _k in MASKED:
            once = redact.redact(line)
            self.assertEqual(redact.redact(once), once)

    def test_whatever_the_secrets_panel_flags_is_masked(self):
        # One sample per detector in reducers/risk.py: the panel counts it, so
        # the API must never show its value.
        samples = {
            "openai_key": ("x sk-" + "a" * 24, "sk-" + "a" * 24),
            "github_pat": ("x ghp_" + "A" * 36, "ghp_" + "A" * 36),
            "hf_token": ("x hf_" + "a" * 34, "hf_" + "a" * 34),
            "aws_key_id": ("x AKIAABCDEFGHIJKLMNOP", "AKIAABCDEFGHIJKLMNOP"),
            "slack_token": ("x xoxb-1234567890-abc", "xoxb-1234567890-abc"),
            "bearer_header": ("curl -H 'Authorization: Bearer tok123456'", "tok123456"),
            "api_key_env": ("WANDB_API_KEY=wandbsecret python x", "wandbsecret"),
            "token_flag": ("x --token tokensecret1", "tokensecret1"),
            "password_flag": ("x --password pwsecret1", "pwsecret1"),
            "db_password_short": ("mysql -u r -pdbsecret1 db", "dbsecret1"),
            "redis_password": ("redis-cli -a redissecret1", "redissecret1"),
            "curl_userpass": ("curl -u u:curlsecret1 https://x", "curlsecret1"),
            "private_key_inline": ("-----BEGIN PRIVATE KEY-----\nkeybody1\n"
                                   "-----END PRIVATE KEY-----", "keybody1"),
        }
        self.assertEqual(set(samples), {s[0] for s in SECRETS})
        for sid, _label, _sev, rx in SECRETS:
            line, secret = samples[sid]
            with self.subTest(detector=sid):
                self.assertTrue(re.search(rx, line), "sample does not trip its detector")
                self.assertNotIn(secret, redact.redact(line))

    def test_a_command_line_with_no_credential_costs_little(self):
        # A guard against a pattern that backtracks pathologically (that costs
        # milliseconds a line), not a benchmark: ~16 us is typical, and a busy
        # login node can be several times slower, so the bound is loose.
        lines = UNCHANGED * 400
        t = time.perf_counter()
        for line in lines:
            redact.redact(line)
        self.assertLess((time.perf_counter() - t) / len(lines), 2e-3)


class ExitTest(unittest.TestCase):
    SECRET = "hunter2pw"
    ARGS = "deploy --password " + SECRET

    def test_a_raw_record_is_copied_masked_and_the_original_kept(self):
        rec = {"event": "exit", "args": self.ARGS, "pid": 7}
        out = redact.record(rec)
        self.assertNotIn(self.SECRET, out["args"])
        self.assertEqual(out["pid"], 7)
        self.assertEqual(rec["args"], self.ARGS)   # the detectors still need it raw

    def test_a_payload_is_masked_wherever_argv_sits(self):
        body = {"rows": [{"tool": "deploy", "args": self.ARGS,
                          "instances": [{"args": self.ARGS, "cpu_s": 1.5}]}],
                "argv": [self.ARGS], "n": 3}
        out = redact.payload(body)
        self.assertNotIn(self.SECRET, repr(out))
        self.assertEqual(out["rows"][0]["instances"][0]["cpu_s"], 1.5)
        self.assertEqual(out["n"], 3)
        self.assertIn(self.SECRET, repr(body))     # a copy, not in place

    def test_a_built_window_serves_masked_argv(self):
        class Stub:
            def result(s):
                return {"worst": [{"args": self.ARGS}]}
        view = PanelView(Cfg(), ["agent"], 60)
        view.reducers = {"resources": Stub()}
        self.assertNotIn(self.SECRET, repr(view.materialize()))
        self.assertNotIn(self.SECRET, repr(view.payload))

    def test_the_live_tail_serves_masked_argv_and_never_the_raw_copy(self):
        live = LiveReducer(Cfg(), ["agent"])
        now = time.time()
        live.feed({"event": "exit", "_a3": "agent", "_ts_epoch": now - 5, "ts": "t",
                   "args": self.ARGS, "pid": 1})
        for _ in range(2):                         # masked once, served many times
            events = live.result(now=now, window_s=3600)["events"]
            self.assertEqual(len(events), 1)
            self.assertNotIn(self.SECRET, repr(events))
            self.assertNotIn("_argv", events[0])

    def test_a_token_at_the_cascade_nodes_cut_is_masked_whole(self):
        # A cascade node keeps 200 characters of argv. A key starting just
        # before the cut, cut first and masked after, would be too short to
        # recognise -- and its first characters would be served.
        from test_sessions import claude_session, reduce
        recs = claude_session(time.time() - 600)
        recs[0]["args"] = "claude --resume " + "x" * 176 + " sk-" + "k" * 30
        root = next(n for t in reduce(recs)["cascades"]["trees"] for n in t["nodes"]
                    if n["pid"] == 100)
        self.assertNotIn("sk-k", root["args"])
        self.assertIn(MASK[:5], root["args"])

    def test_search_cannot_find_a_masked_secret(self):
        rec = {"event": "exit", "_command": "deploy", "args": self.ARGS}
        hit = make_predicate(parse_filters({"q": "hunter2"}))
        self.assertFalse(hit(rec))
        self.assertTrue(make_predicate(parse_filters({"q": "--password"}))(rec))


if __name__ == "__main__":
    unittest.main()
