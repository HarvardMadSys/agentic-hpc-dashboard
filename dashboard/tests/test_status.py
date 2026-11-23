"""Is the collection working (rc_dashboard/status.py): `rc-dashboard status`,
/api/status, /metrics, and the transport files of pull.py and the stream's
receiver.

    cd dashboard && .venv/bin/python -m unittest tests.test_status -q
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
from rc_dashboard import status as statusmod        # noqa: E402

STAMP = "%Y-%m-%d %H:%M:%S"


def clean_env():
    return mock.patch.dict(os.environ, {k: v for k, v in os.environ.items()
                                        if not k.startswith(("RC_DASH_", "EBPFM_"))}, clear=True)


def rec(event, t, host, pid=7, **kw):
    r = {"ts": time.strftime(STAMP, time.localtime(t)), "ts_epoch": t, "host": host,
         "event": event, "collector_pid": pid, "schema_version": 8}
    r.update(kw)
    return r


def write(path, recs):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a") as fh:
        for r in recs:
            fh.write(json.dumps(r) + "\n")


class Feed(unittest.TestCase):
    """A feed of two hosts: h-ok heartbeating now, h-silent quiet for an hour."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rc-status-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        env = clean_env()
        env.start()
        self.addCleanup(env.stop)
        here = mock.patch.object(configmod, "HERE", self.tmp)
        here.start()
        self.addCleanup(here.stop)
        self.now = time.time()
        self.day = time.strftime("%Y-%m-%d", time.localtime(self.now))
        self.ebpf = os.path.join(self.tmp, "ebpf")
        self.node = os.path.join(self.tmp, "node")
        d = os.path.join(self.ebpf, self.day)
        now = self.now
        write(os.path.join(d, "h-ok.snapshot.jsonl"),
              [rec("meta", now - 600, "h-ok", collector_version="1.2.3-1",
                   sysctl={"kernel.sched_schedstats": "1"})]
              + [rec("residency_totals", now - 600 + 60 * i, "h-ok", interval_s=60.0)
                 for i in range(10)])
        write(os.path.join(d, "h-ok.exits.jsonl"), [rec("exit", now - 20, "h-ok")])
        write(os.path.join(d, "h-silent.snapshot.jsonl"),
              [rec("meta", now - 7200, "h-silent", pid=9),
               rec("residency_totals", now - 3600, "h-silent", pid=9, interval_s=60.0)])
        write(os.path.join(self.node, self.day, "h-ok.jsonl"), [rec("node", now - 100, "h-ok")])

    def cfg(self, **extra):
        tree = {"feeds": {"ebpf": {"roots": [self.ebpf]}, "ebpf_node": {"roots": [self.node]}},
                "cache": {"dir": os.path.join(self.tmp, "cache")}}
        for k, v in extra.items():
            configmod._set(tree, k, v)
        path = os.path.join(self.tmp, "c.json")
        with open(path, "w") as fh:
            json.dump(tree, fh)
        err = io.StringIO()
        with redirect_stderr(err):
            return configmod.load(None, path)

    def host(self, doc, name):
        return next(h for h in doc["hosts"] if h["host"] == name)


class Collect(Feed):
    def test_the_contract(self):
        doc = statusmod.collect(self.cfg(), now=self.now, wall=self.now)
        self.assertEqual(set(doc), {"level", "why", "hosts"})
        self.assertIn(doc["level"], statusmod.LEVELS)
        for h in doc["hosts"]:
            self.assertEqual(set(h), {"host", "verdict", "heartbeat_age_s", "newest_age_s",
                                      "transport"})
            self.assertIn(h["verdict"], ("ok", "degraded", "stopped", "silent", "unknown"))
            self.assertEqual(set(h["newest_age_s"]), {"ebpf", "ebpf_node"})
        json.dumps(doc)

    def test_a_silent_collector_is_crit_and_a_live_one_ok(self):
        doc = statusmod.collect(self.cfg(), now=self.now, wall=self.now)
        self.assertEqual(doc["level"], "crit")
        ok, silent = self.host(doc, "h-ok"), self.host(doc, "h-silent")
        self.assertEqual(ok["verdict"], "ok")
        self.assertLess(ok["heartbeat_age_s"], 120)
        self.assertAlmostEqual(ok["newest_age_s"]["ebpf"], 20, delta=2)
        self.assertAlmostEqual(ok["newest_age_s"]["ebpf_node"], 100, delta=2)
        self.assertEqual(silent["verdict"], "silent")
        self.assertAlmostEqual(silent["heartbeat_age_s"], 3600, delta=2)
        # no node tier for it: not measured, never 0
        self.assertIsNone(silent["newest_age_s"]["ebpf_node"])
        self.assertTrue(any(w.startswith("h-silent: collector silent") for w in doc["why"]))
        self.assertFalse(any(w.startswith("h-ok") for w in doc["why"]))

    def test_only_the_live_host_is_ok(self):
        shutil.rmtree(os.path.join(self.ebpf, self.day))
        d = os.path.join(self.ebpf, self.day)
        write(os.path.join(d, "h-ok.snapshot.jsonl"),
              [rec("meta", self.now - 600, "h-ok"),
               rec("residency_totals", self.now - 30, "h-ok", interval_s=60.0)])
        doc = statusmod.collect(self.cfg(), now=self.now, wall=self.now)
        self.assertEqual(doc["level"], "ok", doc["why"])
        self.assertEqual(doc["why"], [])

    def test_a_missing_feed_is_crit_and_names_where_it_looked(self):
        doc = statusmod.collect(self.cfg(**{"feeds.ebpf.roots": [os.path.join(self.tmp, "no")]}),
                                now=self.now, wall=self.now)
        self.assertEqual(doc["level"], "crit")
        self.assertTrue(any("No ebpf feed" in w for w in doc["why"]), doc["why"])

    def test_stale_node_tier_warns(self):
        doc = statusmod.collect(self.cfg(), stale_s=50, now=self.now, wall=self.now)
        self.assertTrue(any("node-tier record" in w for w in doc["why"]), doc["why"])

    def test_the_scan_stops_at_two_starts(self):
        d = os.path.join(self.ebpf, self.day)
        p = os.path.join(d, "h-ok.snapshot.jsonl")
        os.unlink(p)
        recs = []
        for run in range(4):
            t0 = self.now - 4000 + run * 1000
            recs += [rec("meta", t0, "h-ok", pid=100 + run),
                     rec("residency_totals", t0 + 60, "h-ok", pid=100 + run)]
        write(p, recs)
        health, newest = statusmod.scan_health(self.cfg(), {"resolved": [self.ebpf]})
        runs = health.hosts["h-ok"].runs
        self.assertEqual(sorted(runs), [102, 103])
        self.assertAlmostEqual(newest["h-ok"], self.now - 4000 + 3060, delta=1)


class Transports(Feed):
    def rsync_state(self, name, **kw):
        mirror = os.path.join(self.tmp, "mirror")
        state = {"host": name, "target": name, "attempt_epoch": self.now - 10, "ok": True,
                 "ok_epoch": self.now - 10, "tiers": {}, "error": None, "seconds": 1.0,
                 "kind": "rsync", "interval_s": 60, "file_hosts": []}
        state.update(kw)
        p = os.path.join(mirror, ".state", name + ".json")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as fh:
            json.dump(state, fh)
        return os.path.join(mirror, ".state", "*.json")

    def test_an_rsync_pass_that_failed_reads_as_a_failed_copy(self):
        pat = self.rsync_state("h-ok", ok=False, error="ssh: connect to host h-ok: timed out",
                               ok_epoch=self.now - 7200)
        doc = statusmod.collect(self.cfg(**{"status.transport_files": [pat]}),
                                now=self.now, wall=self.now)
        t = self.host(doc, "h-ok")["transport"]
        self.assertEqual(set(t), {"kind", "ok", "last_ok_age_s", "error"})
        self.assertEqual((t["kind"], t["ok"]), ("rsync", False))
        self.assertAlmostEqual(t["last_ok_age_s"], 7200, delta=2)
        self.assertIn("timed out", t["error"])
        self.assertEqual(doc["level"], "crit")
        self.assertTrue(any("h-ok: copy (rsync) FAILED" in w for w in doc["why"]))
        self.assertIsNone(self.host(doc, "h-silent")["transport"])

    def test_a_good_pass_is_ok_until_it_is_stale(self):
        pat = self.rsync_state("h-ok")
        t = statusmod.collect(self.cfg(**{"status.transport_files": [pat]}),
                              now=self.now, wall=self.now)
        self.assertTrue(self.host(t, "h-ok")["transport"]["ok"])
        pat = self.rsync_state("h-ok", ok_epoch=self.now - 3600, attempt_epoch=self.now - 3600)
        t = statusmod.collect(self.cfg(**{"status.transport_files": [pat]}),
                              now=self.now, wall=self.now)["hosts"]
        tr = next(h for h in t if h["host"] == "h-ok")["transport"]
        self.assertFalse(tr["ok"])
        self.assertIn("pull.py running", tr["error"])

    def test_a_node_named_otherwise_maps_by_its_file_hosts_or_its_folder(self):
        pat = self.rsync_state("node1", file_hosts=["h-ok"])
        doc = statusmod.collect(self.cfg(**{"status.transport_files": [pat]}),
                                now=self.now, wall=self.now)
        self.assertEqual(self.host(doc, "h-ok")["transport"]["kind"], "rsync")
        self.assertNotIn("node1", [h["host"] for h in doc["hosts"]])
        # by folder: files under <mirror>/ebpf/node2/ are node2's copy
        mirror = os.path.join(self.tmp, "mirror")
        root = os.path.join(mirror, "ebpf", "node2")
        write(os.path.join(root, self.day, "far.snapshot.jsonl"),
              [rec("residency_totals", self.now - 5, "far")])
        pat = self.rsync_state("node2")
        doc = statusmod.collect(self.cfg(**{"status.transport_files": [pat],
                                            "feeds.ebpf.roots": [self.ebpf, root]}),
                                now=self.now, wall=self.now)
        self.assertIsNotNone(self.host(doc, "far")["transport"])

    def test_a_copy_that_never_brought_a_file_has_a_row(self):
        pat = self.rsync_state("h-new", ok=False, ok_epoch=None, error="Permission denied")
        doc = statusmod.collect(self.cfg(**{"status.transport_files": [pat]}),
                                now=self.now, wall=self.now)
        row = self.host(doc, "h-new")
        self.assertEqual(row["verdict"], "unknown")
        self.assertEqual(row["newest_age_s"], {"ebpf": None, "ebpf_node": None})
        self.assertIsNone(row["transport"]["last_ok_age_s"])
        self.assertTrue(any("h-new: copy (rsync) FAILED: Permission denied; last good never"
                            in w for w in doc["why"]))

    def test_the_stream_receivers_status(self):
        base = os.path.join(self.tmp, "base")
        st = {"at": "x", "at_epoch": self.now - 5, "paused": False,
              "hosts": {"h-ok": {"connected": True, "live": self.day, "lag_s": 3.0,
                                 "down_since": None, "last_error": None, "bytes_today": 1},
                        "h-silent": {"connected": False, "live": None, "lag_s": None,
                                     "down_since": self.now - 600,
                                     "last_error": "ConnectionError: ssh exited 255",
                                     "bytes_today": 0}}}
        p = os.path.join(base, "state", "status.json")
        os.makedirs(os.path.dirname(p))
        with open(p, "w") as fh:
            json.dump(st, fh)
        doc = statusmod.collect(self.cfg(**{"status.transport_files": [p]}),
                                now=self.now, wall=self.now)
        ok, down = self.host(doc, "h-ok")["transport"], self.host(doc, "h-silent")["transport"]
        self.assertEqual((ok["kind"], ok["ok"], ok["error"]), ("stream", True, None))
        self.assertAlmostEqual(ok["last_ok_age_s"], 8, delta=1)
        self.assertEqual((down["ok"], down["error"]), (False, "ConnectionError: ssh exited 255"))
        self.assertAlmostEqual(down["last_ok_age_s"], 600, delta=2)
        # a receiver that stopped writing: every link not ok, saying why
        st["at_epoch"] = self.now - 4000
        with open(p, "w") as fh:
            json.dump(st, fh)
        doc = statusmod.collect(self.cfg(**{"status.transport_files": [p]}),
                                now=self.now, wall=self.now)
        self.assertIn("is it running", self.host(doc, "h-ok")["transport"]["error"])

    def test_a_file_that_is_not_a_state_file_warns(self):
        p = os.path.join(self.tmp, "x.json")
        with open(p, "w") as fh:
            fh.write("[1, 2]")
        doc = statusmod.collect(self.cfg(**{"status.transport_files": [
            p, os.path.join(self.tmp, "gone.json"), os.path.join(self.tmp, "none-*.json")]}),
            now=self.now, wall=self.now)
        why = " | ".join(doc["why"])
        self.assertIn("is not a state file", why)
        self.assertIn("is not there", why)
        self.assertIn("no transport file matches", why)

    def test_errors_are_masked(self):
        pat = self.rsync_state("h-ok", ok=False, error="curl https://x/?token=abcdef0123456789abc failed")
        doc = statusmod.collect(self.cfg(**{"status.transport_files": [pat]}),
                                now=self.now, wall=self.now)
        self.assertNotIn("abcdef0123456789abc", json.dumps(doc))


class Command(Feed):
    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_exit_codes_and_json(self):
        path = self.cfg().config_file
        code, out, err = self.run_cli("status", "--json", "--config", path)
        self.assertEqual(code, 2, err)
        doc = json.loads(out)
        self.assertEqual(doc["level"], "crit")
        # flags before the command are taken as well
        code, out, _ = self.run_cli("--config", path, "status")
        self.assertEqual(code, 2)
        self.assertIn("CRIT", out)
        self.assertIn("h-silent", out)
        code, _, err = self.run_cli("status", "--stale", "soon", "--config", path)
        self.assertEqual(code, 2)
        self.assertIn("not a duration", err)

    def test_durations(self):
        for text, want in (("300", 300), ("90s", 90), ("15m", 900), ("2h", 7200),
                           ("1d", 86400), ("1.5h", 5400)):
            self.assertEqual(statusmod.parse_duration(text), want)
        for bad in ("", "0", "-5m", "5 minutes", "m"):
            with self.assertRaises(ValueError):
                statusmod.parse_duration(bad)


class Metrics(unittest.TestCase):
    def test_null_is_no_sample_and_labels_are_escaped(self):
        doc = {"level": "warn", "why": [], "hosts": [
            {"host": 'a"b', "verdict": "unknown", "heartbeat_age_s": None,
             "newest_age_s": {"ebpf": 1.5, "ebpf_node": None},
             "transport": {"kind": "rsync", "ok": False, "last_ok_age_s": None, "error": "x"}}]}
        text = statusmod.metrics(doc, version={"version": "0.1.0", "commit": None})
        self.assertIn("rc_dashboard_status_level 1\n", text)
        self.assertIn('rc_dashboard_collector_verdict{host="a\\"b",verdict="unknown"} 1', text)
        self.assertIn('rc_dashboard_newest_record_age_seconds{host="a\\"b",tier="ebpf"} 1.5', text)
        self.assertNotIn('tier="ebpf_node"}', text)
        self.assertNotIn("rc_dashboard_heartbeat_age_seconds{", text)
        self.assertIn('rc_dashboard_transport_ok{host="a\\"b",kind="rsync"} 0', text)
        self.assertNotIn("rc_dashboard_transport_last_ok_age_seconds{", text)
        self.assertIn('rc_dashboard_build_info{version="0.1.0",commit=""} 1', text)


if __name__ == "__main__":
    unittest.main()


class Service(Feed):
    """The real app over the feed, behind a password in a hash file: /api/status,
    /metrics, /api/collectors' transport, /api/config's _meta, /api/feeds'
    candidates, and a new hash taken up with no restart."""

    PASSWORD = "Status-pw:1"

    def setUp(self):
        super().setUp()
        import base64
        import socket
        import threading
        import warnings
        import uvicorn
        from rc_dashboard import app as appmod
        from rc_dashboard import auth
        self.auth = auth
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        self.hash_file = os.path.join(self.tmp, "etc", "password.hash")
        auth.write_hash_file(self.hash_file, auth.hash_password(self.PASSWORD, n=16, r=1, p=1))
        mirror = os.path.join(self.tmp, "mirror", ".state")
        os.makedirs(mirror)
        with open(os.path.join(mirror, "h-ok.json"), "w") as fh:
            json.dump({"host": "h-ok", "ok": False, "ok_epoch": self.now - 60,
                       "attempt_epoch": self.now - 5, "error": "rsync: connection unexpectedly closed"}, fh)
        cfg = self.cfg(**{"server.auth.password_hash_file": self.hash_file,
                          "server.port": self.port, "server.static_dir": os.path.join(self.tmp, "ui"),
                          "panels.prewarm_min": [], "site.name": "",
                          "status.transport_files": [os.path.join(mirror, "*.json")]})
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            self.app = appmod.create_app(cfg)
        self.app.state.auth.fail_delay_s = 0
        self.server = uvicorn.Server(uvicorn.Config(self.app, host="127.0.0.1", port=self.port,
                                                    log_level="error"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        self.addCleanup(self.thread.join, 20)
        self.addCleanup(setattr, self.server, "should_exit", True)
        deadline = time.time() + 60
        while not self.server.started and time.time() < deadline:
            time.sleep(0.05)
        self.basic = "Basic " + base64.b64encode(b"rc:" + self.PASSWORD.encode()).decode()
        # the backfill has read the collectors' own records
        while time.time() < deadline:
            if self.app.state.agg.backfill.get("state") in ("done", "error"):
                break
            time.sleep(0.05)

    def req(self, path, auth_header=None, method="GET", body=None, headers=None):
        import http.client
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        h = dict(headers or {})
        if auth_header:
            h["Authorization"] = auth_header
        try:
            c.request(method, path, body=body, headers=h)
            r = c.getresponse()
            return r.status, {k.lower(): v for k, v in r.getheaders()}, r.read()
        finally:
            c.close()

    def test_status_and_metrics_want_the_password(self):
        for path in ("/api/status", "/metrics"):
            self.assertEqual(self.req(path)[0], 401, path)
        status, _, body = self.req("/api/status", self.basic)
        self.assertEqual(status, 200, body)
        doc = json.loads(body)
        self.assertEqual(set(doc), {"level", "why", "hosts"})
        self.assertEqual(doc["level"], "crit")
        self.assertEqual(self.host(doc, "h-silent")["verdict"], "silent")
        self.assertFalse(self.host(doc, "h-ok")["transport"]["ok"])
        self.assertEqual(self.req("/api/status?stale=soon", self.basic)[0], 400)
        status, h, body = self.req("/metrics", self.basic)
        self.assertEqual(status, 200)
        self.assertTrue(h["content-type"].startswith("text/plain"))
        self.assertIn(b"rc_dashboard_status_level 2", body)
        self.assertIn(b'rc_dashboard_collector_verdict{host="h-silent",verdict="silent"} 1', body)

    def test_collectors_carry_the_copy_and_the_collector_version(self):
        status, _, body = self.req("/api/collectors", self.basic)
        rows = {r["host"]: r for r in json.loads(body)["hosts"]}
        self.assertEqual(rows["h-ok"]["transport"]["kind"], "rsync")
        self.assertIn("connection unexpectedly closed", rows["h-ok"]["transport"]["error"])
        self.assertIsNone(rows["h-silent"]["transport"])
        self.assertEqual(rows["h-ok"]["running"]["collector_version"], "1.2.3-1")
        self.assertIsNone(rows["h-silent"]["runs"][0]["collector_version"])

    def test_config_meta_and_feed_candidates(self):
        tree = json.loads(self.req("/api/config", self.basic)[2])
        meta = tree["_meta"]
        self.assertEqual(set(meta["version"]["_value"]), {"version", "commit"})
        self.assertIn(meta["version"]["_origin"], ("build", "git", "package"))
        self.assertEqual(set(meta["tz"]["_value"]), {"name", "abbrev", "utc_offset_s"})
        self.assertEqual(meta["tz"]["_origin"], "runtime")
        self.assertEqual(meta["host"]["_origin"], "runtime")
        self.assertEqual(tree["server"]["auth"]["password_hash_file"]["_value"], self.hash_file)
        self.assertIs(tree["ui"]["show_internal"]["_value"], False)
        feeds = json.loads(self.req("/api/feeds", self.basic)[2])
        self.assertEqual(feeds["ebpf"]["candidates"],
                         [{"path": self.ebpf, "state": "found", "note": None}])

    def test_the_sign_in_form_names_the_host_when_the_site_has_no_name(self):
        from rc_dashboard import about
        status, _, body = self.req("/")
        self.assertEqual(status, 401)
        self.assertIn(("Sign in · %s" % about.host()).encode(), body)
        self.assertNotIn(b"rc_measurement", body)

    def test_a_new_hash_file_is_taken_up_without_a_restart(self):
        import urllib.parse
        form = {"content-type": "application/x-www-form-urlencoded"}
        sign_in = lambda pw: self.req("/login", method="POST", headers=form,      # noqa: E731
                                      body=urllib.parse.urlencode({"password": pw, "next": "/"}))
        status, h, _ = sign_in(self.PASSWORD)
        self.assertEqual(status, 303)
        cookie = h["set-cookie"].split(";")[0]
        self.assertEqual(self.req("/api/health", headers={"Cookie": cookie})[0], 200)
        self.auth.write_hash_file(self.hash_file,
                                  self.auth.hash_password("New-pw:2", n=16, r=1, p=1))
        # a sign-in attempt looks at the file: the old password no longer opens it
        self.assertEqual(sign_in(self.PASSWORD)[0], 401)
        self.assertEqual(sign_in("New-pw:2")[0], 303)
        # and the cookie made under the old hash no longer counts
        self.assertEqual(self.req("/api/health", headers={"Cookie": cookie})[0], 401)
        # a file that holds no hash leaves the password as it was
        with open(self.hash_file, "w") as fh:
            fh.write("not a hash\n")
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(sign_in("New-pw:2")[0], 303)
        self.assertIn("stays as it was", err.getvalue())
