"""The guard: with anonymization on, no real name leaves the service at all.

Starts the real app on a free local port over a scratch feed full of names --
two accounts, a person who never ran anything here and appears only inside a
home path, two labs, an email address and a uid -- then reads every GET route
and the first websocket frames, and looks for any of those names in any byte. It also checks the aliases DO arrive, so
the test cannot pass by serving nothing.

Behind a password; every request sends it.

`ReaderTextTest` sends real names instead: a real name a reader types finds
nothing, even for an account no response has named yet.

    cd dashboard && .venv/bin/python -m unittest discover -s tests -q
"""
import base64
import json
import os
import re
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
from rc_dashboard import auth                      # noqa: E402
from rc_dashboard import config as configmod       # noqa: E402
from rc_dashboard import export                    # noqa: E402
from rc_dashboard.anonymize import Anonymizer      # noqa: E402
from test_ingest import DAY, line, stamp           # noqa: E402

PASSWORD = "Leak-test:1"
AUTH = {"Authorization": "Basic " + base64.b64encode(("rc:" + PASSWORD).encode()).decode()}
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
    # the agent's own tick, first, since a day-file is in time order: a session
    # with an agent at its root, as the Users tab's portraits count one
    recs.append({"schema_version": 6, "event": "residency", "ts": stamp(now - 2500),
                 "ts_epoch": now - 2500, "host": "h1", "user": "alice", "uid": UID,
                 "actor3": "agent", "agent_type": "claude_code", "pid": 4242, "ppid": 1,
                 "agent_pid": 4242, "tree_root_pid": 4242, "depth": 0, "is_agent": True,
                 "comm": "claude", "args": "claude", "cwd": "/home/alice/proj"})
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
    # a Bash tool call of alice's agent, wrapped as its shell runs one: the
    # inspector serves the command it ran (proctree.ran)
    recs.append(exit_rec(now - 90, 5100, "alice",
                         "/bin/zsh -c -l source /home/alice/.claude/shell-snapshots/snapshot-zsh-1.sh"
                         " && eval 'python train.py --data /n/home03/carol/data' < /dev/null"))
    return recs


class Served(unittest.TestCase):
    """The real app on a free local port over `records`, with names masked."""

    @classmethod
    def records(cls, now):
        return feed(now)

    @classmethod
    def configure(cls, tree):
        """Changes to the config before the app is made."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="rc_anon_leak_")
        root = os.path.join(cls.tmp, "ebpf")
        os.makedirs(os.path.join(root, DAY))
        cls.now = now = time.time()
        with open(os.path.join(root, DAY, "h1.exits.jsonl"), "wb") as fh:
            for r in cls.records(now):
                fh.write(line(r))
        # A host whose name holds a user's: every route that names hosts masks it --
        # /metrics once formatted the raw status document, and showed it.
        with open(os.path.join(root, DAY, "alice-ws.snapshot.jsonl"), "wb") as fh:
            for ev, t in (("meta", now - 600), ("residency_totals", now - 60)):
                fh.write(line({"schema_version": 8, "event": ev, "ts": stamp(t), "ts_epoch": t,
                               "host": "alice-ws", "collector_pid": 7, "interval_s": 60.0}))
        tree = configmod.defaults()
        tree["feeds"]["ebpf"]["roots"] = [root]
        tree["feeds"]["ebpf_node"]["roots"] = [os.path.join(cls.tmp, "node")]
        tree["cache"]["dir"] = os.path.join(cls.tmp, "cache")
        tree["cache"]["state_dir"] = os.path.join(cls.tmp, "state")
        tree["server"]["static_dir"] = os.path.join(cls.tmp, "no-ui")
        tree["panels"]["prewarm_min"] = []
        tree["anonymize"]["enabled"] = True
        tree["server"]["auth"]["password_hash"] = auth.hash_password(PASSWORD, n=16, r=1, p=1)
        cls.configure(tree)
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
            if (live.get("users") or {}).get("rows_total") and \
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
            with urllib.request.urlopen(urllib.request.Request(url, headers=AUTH),
                                        timeout=60) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    def assertClean(self, where, body):
        for s in SECRETS:
            self.assertNotIn(s, body, "%s leaks %r" % (where, s))


class LeakTest(Served):

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
                     "/api/live/users", "/api/live/user", "/api/users", "/api/user",
                     "/api/config", "/api/feeds", "/api/health", "/api/windows",
                     "/api/status", "/metrics"):
            self.assertIn(must, seen)

    def test_a_portrait_takes_an_alias_and_a_real_name_finds_nobody(self):
        alice = self.anon.user_alias("alice")
        roster = json.loads(self.get("/api/users")[1])
        self.assertIn(alice, [r["user"] for r in roster["users"]])
        got = json.loads(self.get("/api/user?name=%s" % alice)[1])
        self.assertTrue(got["found"])
        self.assertEqual(got["user"], alice)
        self.assertClean("/api/user", json.dumps(got))
        self.assertFalse(json.loads(self.get("/api/user?name=alice")[1])["found"])

    def test_the_aliases_do_arrive(self):
        alice, bob = self.anon.user_alias("alice"), self.anon.user_alias("bob.smith")
        body = self.get("/api/live")[1] + self.get("/api/panels")[1]
        self.assertIn(alice, body)
        self.assertIn(bob, body)
        self.assertIn(self.anon.labs["tambe_lab"], body)

    def test_the_websocket_frames_are_clean(self):
        with connect("ws://127.0.0.1:%d/ws" % self.port, open_timeout=20,
                     additional_headers=AUTH) as ws:
            for _ in range(3):
                self.assertClean("/ws", ws.recv(timeout=20))

    def test_a_filter_takes_an_alias_and_a_real_name_finds_nobody(self):
        alice = self.anon.user_alias("alice")
        got = json.loads(self.get("/api/live/user?name=%s" % alice)[1])
        self.assertIsNotNone(got["row"])
        self.assertEqual(got["user"], alice)
        self.assertIsNone(json.loads(self.get("/api/live/user?name=alice")[1])["row"])

    def test_search_matches_what_the_reader_sees(self):
        alice = self.anon.user_alias("alice")
        found = json.loads(self.get("/api/live/users?q=%s" % alice[:9])[1])["rows"]
        self.assertEqual([r["user"] for r in found], [alice])
        self.assertEqual(json.loads(self.get("/api/live/users?q=ali")[1])["rows"], [])

    def test_health_says_it_is_on_without_naming_anyone(self):
        h = json.loads(self.get("/api/health")[1])["anonymize"]
        self.assertTrue(h["enabled"])
        self.assertGreaterEqual(h["users_known"], 2)
        self.assertEqual(h["alias_collisions"], 0)


SALT = "reader-text-salt-1"   # 16 bytes or more, as a salt must be
MARK = "zq7mark"        # in dave's command lines only: his rows wherever they are served


def named_later(now):
    """`feed`, and accounts no response has named: `dave` and `erin` ran things
    three hours ago, past the hour the live roster holds here but in the
    day-file a filter reads. `frank`'s tool and directory carry
    his name, and his processes are keyed by his account alone (`usr:`). Enough
    newer exits that the old ones are past the first page of anything
    unfiltered."""
    recs = feed(now)
    for i in range(3):
        recs.append(exit_rec(now - 3 * 3600 + i, 9000 + i, "dave",
                             "rsync -a /scratch/dave/run%d remote: %s" % (i, MARK), "human",
                             cwd="/scratch/dave"))
        recs.append(exit_rec(now - 3 * 3600 + 30 + i, 9100 + i, "erin",
                             "rsync -a /scratch/erin/out%d remote:" % i, "human",
                             cwd="/scratch/erin"))
        frank = exit_rec(now - 900 + i, 9200 + i, "frank", "/home/frank/bin/frank_sync --go",
                         "human", cwd="/scratch/frank/run")
        del frank["session_key"]
        recs.append(frank)
    for i in range(170):
        recs.append(exit_rec(now - 2400 + 12 * i, 6000 + i, ("alice", "bob.smith")[i % 2],
                             "git status", ("agent", "human")[i % 2]))
    return sorted(recs, key=lambda r: r["ts_epoch"])


class ReaderTextTest(Served):
    """What a reader sends, with real names in it, over `named_later`."""

    records = staticmethod(named_later)

    @classmethod
    def configure(cls, tree):
        tree["live"].update(retention_min=60, window_min=60, backfill_hours=1,
                            window_presets_min=[60])
        tree["anonymize"]["salt"] = SALT

    @staticmethod
    def alias(name):
        """`name`'s alias, worked out without teaching the service the name."""
        return Anonymizer(SALT).user_alias(name)

    def lookup(self, query):
        """A finished /api/io/commands lookup."""
        job = json.loads(self.get("/api/io/commands?" + query)[1])
        deadline = time.time() + 60
        while job["state"] not in ("done", "error"):
            self.assertLess(time.time(), deadline, job)
            time.sleep(0.1)
            job = json.loads(self.get("/api/io/commands/%s" % job["id"])[1])
        self.assertEqual(job["state"], "done", job)
        return job

    def test_a_real_name_in_a_tool_or_a_directory_finds_nothing(self):
        frank, alice = self.alias("frank"), self.alias("alice")
        real, shown = "/home/frank/bin/frank_sync", "/home/%s/bin/%s_sync" % (frank, frank)
        for query, n in (("tool=" + urllib.parse.quote(real), 0),
                         ("tool=" + urllib.parse.quote(shown), 3),
                         ("cwd=/scratch/frank", 0),
                         ("cwd=" + urllib.parse.quote("/scratch/" + frank), 3),
                         ("cwd=/home/alice", 0)):
            self.assertEqual(self.lookup(query)["matched"], n, query)
        self.assertGreater(self.lookup("cwd=" + urllib.parse.quote("/home/" + alice))["matched"], 0)

    def test_a_real_name_in_a_session_id_names_no_session(self):
        # frank's processes are keyed by his account (`usr:frank`); the
        # mapping itself is pinned in test_anonymize (dealias_sid)
        day = time.strftime("%Y-%m-%d", time.localtime(self.now - 900))
        for _ in range(3):
            body = json.loads(self.get("/api/trajectory?sid=" + urllib.parse.quote(
                "h1|%s|human|usr:frank" % day))[1])
            self.assertFalse(body["found"], body)

    def test_what_a_reader_sends_comes_back_as_sent(self):
        # Rewritten on the way out, each of these turned a name the reader
        # typed into its alias -- alice's here -- and left `zed9`, no account,
        # as it was: a table of names to aliases for anyone with a guess.
        alice, quote = self.alias("alice"), urllib.parse.quote
        self.assertEqual(json.loads(self.get("/api/panels?agent_type=alice")[1])["filter"]
                         ["agent_types"], ["alice"])
        # and the filtered view's status, which every reader's /api/windows lists
        windows = self.get("/api/windows")[1]
        self.assertIn('"agent_types":["alice"]', windows.replace(" ", ""))
        job = self.lookup("host=alice&cwd=" + quote("/home/alice"))
        self.assertEqual(job["query"], {"host": "alice", "cwd": "/home/alice"})
        self.assertEqual(json.loads(self.get("/api/io/commands/%s?sort=alice" % job["id"])[1])
                         ["sort"], "alice")
        self.assertEqual(json.loads(self.get("/api/trajectory?sid=alice")[1])["sid"], "alice")
        self.assertNotIn(alice, windows)

    # The bodies each POST route reads, with SENT where the reader's text goes.
    # No route takes one today; a POST route added later fails the walk below
    # until it is listed here.
    SENT = object()
    BODIES = {
        # counts of tabs: a name in a count is refused, in a key dropped, never repeated
        "/api/usage": [{"views": {"tab:live": 1}}, {"views": {"tab:live": SENT}},
                       {"views": SENT}],
    }

    @classmethod
    def body_with(cls, body, name):
        if body is cls.SENT:
            return name
        if isinstance(body, dict):
            return {k: cls.body_with(v, name) for k, v in body.items()}
        if isinstance(body, list):
            return [cls.body_with(v, name) for v in body]
        return body

    def send(self, method, path, body=None):
        req = urllib.request.Request("http://127.0.0.1:%d%s" % (self.port, path), method=method,
                                     data=None if body is None else json.dumps(body).encode(),
                                     headers=dict(AUTH, **{"content-type": "application/json"}))
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    def test_no_route_answers_a_real_name_with_its_alias_or_its_rows(self):
        """dave's name in every query, path and body parameter of every route,
        one at a time (a POST route's query, which only its view filter reads,
        all at once): no answer may carry his alias, or any of his rows (each
        holds MARK). His records are older than the live roster and past the
        first page of anything unfiltered, so only a filter could reach them,
        and only a translation of his name could produce his alias. Nor may any
        carry the feed's other names (SECRETS), which its records hold
        everywhere. The routes are the app's own; a POST route needs its body
        in BODIES."""
        dave, quote = self.alias("dave"), urllib.parse.quote
        # The service knows his name, as it does once it has masked a record of
        # his: a bare `dave` echoed through the rewrite would be translated.
        self.anon.learn_user("dave")
        self.assertEqual(self.anon.users.get("dave"), dave)
        day = time.strftime("%Y-%m-%d", time.localtime(self.now - 3 * 3600))
        names = ("dave", "/home/dave", "h1|%s|human|usr:dave" % day)
        ranks = json.loads(self.get("/api/trajectories")[1])["ranks"]
        sid = next(iter(ranks["recent"]))
        # a lookup to name, made when its route is reached: the walk's own
        # lookups push older ones out
        made = {"/api/io/commands/{jid}": lambda: self.lookup("")["id"]}
        fill = {"name": self.alias("alice"), "sid": sid}
        filters = list(export.LIST_FIELDS + export.SCALARS)

        def check(where, status, body):
            # turned away at the sign-in, a request reaches no route, and the walk
            # would pass having read nothing
            self.assertNotEqual(status, 401, where)
            if where[1] != "/":           # "frontend not built": no UI here, by design
                self.assertLess(status, 500, where)
            self.assertNotIn(dave, body, where)
            self.assertNotIn(MARK, body, where)
            self.assertClean(where, body)

        swept, seen = [], set()
        for r in self.app.routes:
            if not isinstance(r, APIRoute):
                continue
            declared = [p.alias for p in r.dependant.query_params]
            required = {p.alias: fill.get(p.alias, "x") for p in r.dependant.query_params
                        if p.field_info.is_required()}
            path = re.sub(r"\{[^}]+\}", made.get(r.path, lambda: "nope")(), r.path)
            for method in sorted(r.methods):
                seen.add((method, r.path))
                bodies = [None]
                if method == "POST":
                    self.assertIn(r.path, self.BODIES, "a POST route the walk does not know")
                    bodies = self.BODIES[r.path]
                elif method == "GET" and "{" not in r.path:
                    swept.append((r.path, required))
                for name in names:
                    for slot in [p.name for p in r.dependant.path_params]:
                        check((method, r.path, slot, name), *self.send(method, r.path.replace(
                            "{%s}" % slot, quote(name, safe="")), self.body_with(bodies[0], "q")))
                    if method == "GET":
                        for key in dict.fromkeys(declared + filters):
                            query = urllib.parse.urlencode(dict(required, **{key: name}))
                            check((method, r.path, key, name), *self.get(path + "?" + query))
                        continue
                    # the others read the query for the page's view filter alone: it
                    # goes with the route's first body
                    query = urllib.parse.urlencode(dict({k: name for k in declared + filters},
                                                        **required))
                    check((method, r.path, "?" + query), *self.send(
                        method, path + "?" + query, self.body_with(bodies[0], "q")))
                    for body in bodies:
                        check((method, r.path, body, name),
                              *self.send(method, path, self.body_with(body, name)))
        self.assertGreater(len(seen), 15)
        # and nothing the walk left behind -- a filtered view, a lookup -- tells
        # any later reader
        for path, required in swept:
            check(("GET", path), *self.get(path + "?" + urllib.parse.urlencode(required)))
        with connect("ws://127.0.0.1:%d/ws" % self.port, open_timeout=20,
                     additional_headers=AUTH) as ws:
            for _ in range(3):
                check(("WS", "/ws"), 200, ws.recv(timeout=20))

if __name__ == "__main__":
    unittest.main()
