"""The guard: with anonymization on, no real name leaves the service at all.

Starts the real app on a free local port over a scratch feed full of names --
two accounts, a person who never ran anything here and appears only inside a
home path, two labs, an email address and a uid -- then reads every GET route,
the export stream, the event search and the first websocket frames, and looks
for any of those names in any byte. It also checks the aliases DO arrive, so
the test cannot pass by serving nothing.

    cd dashboard && .venv/bin/python -m unittest discover -s tests -q
"""
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import uvicorn                                     # noqa: E402
from fastapi.routing import APIRoute               # noqa: E402
from websockets.sync.client import connect         # noqa: E402

from rc_dashboard import app as appmod             # noqa: E402
from rc_dashboard import config as configmod       # noqa: E402
from test_ingest import DAY, line, stamp           # noqa: E402

SECRETS = ("alice", "bob.smith", "bob-smith", "carol", "tambe", "kempner_grads",
           "me@fas.harvard.edu", "claude-1234", "user-1234.slice", "user@1234.service",
           '"uid": 1234', '"uid":1234')
UID = 1234


def exit_rec(epoch, pid, user, args, actor3="agent", cwd=None):
    return {"schema_version": 6, "event": "exit", "ts": stamp(epoch), "ts_epoch": epoch,
            "host": "h1", "user": user, "uid": UID, "actor3": actor3,
            "agent_type": "claude_code" if actor3 == "agent" else None,
            "agent_pid": 4242 if actor3 == "agent" else None, "tree_root_pid": 4242,
            "session_key": "apid:4242" if actor3 == "agent" else "tty:7",
            "depth": 1 if actor3 == "agent" else None,
            "comm": args.split()[0], "args": args, "pid": pid, "ppid": 4242,
            "cwd": cwd or "/home/%s/proj" % user,
            "cgroup": "/user.slice/user-%d.slice/user@%d.service/app.scope" % (UID, UID),
            "cpu_s": 0.2, "duration_s": 1.0, "peak_rss_mb": 12.0,
            "io": {"rd_mb": 0.1, "wr_mb": 0.1, "rchar_mb": 0.2, "wchar_mb": 0.2,
                   "scope": "process"}}


def feed(now):
    recs = []
    cmds = [("alice", "python /home/alice/proj/train.py", "agent"),
            ("alice", "cat /n/home03/carol/data/notes.txt", "agent"),
            ("alice", "sbatch --account=tambe_lab --wrap 'python x.py'", "agent"),
            ("alice", "sbatch -A kempner_grads job.sh", "agent"),
            ("alice", "mail -s hi me@fas.harvard.edu", "agent"),
            ("alice", "ls /tmp/claude-1234/-home-alice-proj/scratchpad", "agent"),
            ("bob.smith", "vim /home/bob.smith/thesis.tex", "human"),
            ("bob.smith", "ls /n/holylabs/tambe_lab/Lab/bob.smith", "human")]
    for i in range(40):
        user, args, a3 = cmds[i % len(cmds)]
        recs.append(exit_rec(now - 60 * (40 - i), 5000 + i, user, args, a3))
    recs.append({"schema_version": 6, "event": "residency", "ts": stamp(now - 30),
                 "ts_epoch": now - 30, "host": "h1", "user": "alice", "uid": UID,
                 "actor3": "agent", "agent_type": "claude_code", "agent_pid": 4242,
                 "tree_root_pid": 4242, "n_procs": 3, "threads": 5, "rss_mb": 120.0,
                 "cpu_s": 4.0, "age_s": 900, "d_state": 0,
                 "top_procs": [{"pid": 4243, "command": "python /home/alice/proj/train.py",
                                "rss_mb": 50.0}]})
    recs.append({"schema_version": 6, "event": "submit", "ts": stamp(now - 120),
                 "ts_epoch": now - 120, "host": "h1", "user": "alice", "uid": UID,
                 "actor3": "agent", "agent_type": "claude_code", "agent_pid": 4242,
                 "tool": "sbatch", "job_id": 1001, "exit_code": 0, "partition": "gpu",
                 "args": "sbatch --account=tambe_lab -p gpu --wrap 'python /n/home03/carol/x.py'",
                 "work_dir": "/home/alice/proj"})
    recs.append({"schema_version": 6, "event": "residency_totals", "ts": stamp(now - 30),
                 "ts_epoch": now - 30, "host": "h1",
                 "by_actor3": {"agent": {"n_procs": 3, "threads": 5, "rss_mb": 120.0,
                                         "d_state": 0, "users": 1}},
                 "by_agent_type": {}, "trees": 1, "tracked_live": 3})
    return recs


class LeakTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="rc_anon_leak_")
        root = os.path.join(cls.tmp, "ebpf")
        os.makedirs(os.path.join(root, DAY))
        now = time.time()
        with open(os.path.join(root, DAY, "h1.exits.jsonl"), "wb") as fh:
            for r in feed(now):
                fh.write(line(r))
        tree = configmod.defaults()
        tree["feeds"]["ebpf"]["roots"] = [root]
        tree["feeds"]["ebpf_node"]["roots"] = [os.path.join(cls.tmp, "node")]
        tree["cache"]["dir"] = os.path.join(cls.tmp, "cache")
        tree["cache"]["state_dir"] = os.path.join(cls.tmp, "state")
        tree["server"]["static_dir"] = os.path.join(cls.tmp, "no-ui")
        tree["panels"]["prewarm_min"] = []
        tree["ask"]["enabled"] = False
        tree["anonymize"]["enabled"] = True
        cfg = configmod.Config(tree, {}, None)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)    # on_event
            cls.app = appmod.create_app(cfg)
        cls.anon = cls.app.state.agg.anon
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            cls.port = s.getsockname()[1]
        cls.server = uvicorn.Server(uvicorn.Config(
            cls.app, host="127.0.0.1", port=cls.port, log_level="error"))
        cls.thread = threading.Thread(target=cls.server.run, daemon=True)
        cls.thread.start()
        deadline = time.time() + 60
        while not cls.server.started and time.time() < deadline:
            time.sleep(0.05)
        # Wait until the live tier holds the feed and the window has been built.
        while time.time() < deadline:
            live = json.loads(cls.get("/api/live")[1])
            panels = json.loads(cls.get("/api/panels")[1])
            if (live.get("users") or {}).get("total") and \
                    (panels.get("window_status") or {}).get("state") == "ready":
                break
            time.sleep(0.3)
        else:
            raise AssertionError("the app never built its window")

    @classmethod
    def tearDownClass(cls):
        cls.server.should_exit = True
        cls.thread.join(timeout=20)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @classmethod
    def get(cls, path):
        url = "http://127.0.0.1:%d%s" % (cls.port, path)
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    def assertClean(self, where, body):
        for s in SECRETS:
            self.assertNotIn(s, body, "%s leaks %r" % (where, s))

    def test_no_route_serves_a_real_name(self):
        alice = self.anon.user_alias("alice")
        ranks = json.loads(self.get("/api/trajectories")[1]).get("ranks") or {}
        sid = next(iter(ranks.get("events") or []), "none")
        fill = {"name": alice, "sid": sid, "jid": "nope"}
        seen = []
        for r in self.app.routes:
            if not isinstance(r, APIRoute) or "GET" not in r.methods:
                continue
            path = r.path.replace("{jid}", "nope")
            need = {p.name: fill[p.name] for p in r.dependant.query_params
                    if p.name in fill}
            url = path + ("?" + urllib.parse.urlencode(need) if need else "")
            status, body = self.get(url)
            if path != "/":          # "frontend not built": no UI in this test, by design
                self.assertLess(status, 500, url)
            self.assertClean(url, body)
            seen.append(path)
        for must in ("/api/panels", "/api/live", "/api/trajectories", "/api/trajectory",
                     "/api/events", "/api/export", "/api/live/users", "/api/live/user",
                     "/api/config", "/api/feeds", "/api/health", "/api/windows"):
            self.assertIn(must, seen)

    def test_the_aliases_do_arrive(self):
        alice, bob = self.anon.user_alias("alice"), self.anon.user_alias("bob.smith")
        body = self.get("/api/live")[1] + self.get("/api/panels")[1]
        self.assertIn(alice, body)
        self.assertIn(bob, body)
        self.assertIn(self.anon.labs["tambe_lab"], self.get("/api/export")[1])

    def test_the_websocket_frames_are_clean(self):
        with connect("ws://127.0.0.1:%d/ws" % self.port, open_timeout=20) as ws:
            for _ in range(3):
                self.assertClean("/ws", ws.recv(timeout=20))

    def test_a_filter_takes_an_alias_and_a_real_name_finds_nobody(self):
        alice = self.anon.user_alias("alice")
        got = json.loads(self.get("/api/live/user?name=%s" % alice)[1])
        self.assertIsNotNone(got["row"])
        self.assertEqual(got["user"], alice)
        self.assertIsNone(json.loads(self.get("/api/live/user?name=alice")[1])["row"])
        rows = json.loads(self.get("/api/events?user=%s" % alice)[1])["rows"]
        self.assertTrue(rows)
        self.assertEqual({r["user"] for r in rows}, {alice})
        self.assertEqual(json.loads(self.get("/api/events?user=alice")[1])["rows"], [])

    def test_search_matches_what_the_reader_sees(self):
        alice = self.anon.user_alias("alice")
        self.assertEqual(json.loads(self.get("/api/events?q=alice")[1])["rows"], [])
        self.assertTrue(json.loads(self.get("/api/events?q=%s" % alice)[1])["rows"])
        found = json.loads(self.get("/api/live/users?q=%s" % alice[:9])[1])["rows"]
        self.assertEqual([r["user"] for r in found], [alice])
        self.assertEqual(json.loads(self.get("/api/live/users?q=ali")[1])["rows"], [])

    def test_health_says_it_is_on_without_naming_anyone(self):
        h = json.loads(self.get("/api/health")[1])["anonymize"]
        self.assertTrue(h["enabled"])
        self.assertGreaterEqual(h["users_known"], 2)
        self.assertEqual(h["alias_collisions"], 0)


if __name__ == "__main__":
    unittest.main()
