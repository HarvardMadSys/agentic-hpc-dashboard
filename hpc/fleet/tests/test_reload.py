"""The receiver's host list, read again while it runs: a host added gets its link, one
taken out stops, and nothing else restarts (deploy.sh installs a new stream.json, and the
dashboard reads the copy through a glob). Run for real, a sender per fake collector."""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import fleet_stream as fs                                     # noqa: E402
from fakecollector import FakeCollector                       # noqa: E402
from test_receiver import mirror_tree, source_tree            # noqa: E402
from test_safety import big_disk, sender_argv, wait_for       # noqa: E402


class TestReload(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fleet-reload-")
        self.base = os.path.join(self.tmp, "base")
        os.mkdir(self.base, 0o700)
        self.src = {h: os.path.join(self.tmp, "src-" + h) for h in ("a", "b")}
        for d in self.src.values():
            os.mkdir(d)
        self.conf = os.path.join(self.tmp, "stream.json")
        self.receiver = self.thread = None

    def tearDown(self):
        if self.receiver is not None:
            self.receiver.stop.set()
        if self.thread is not None:
            self.thread.join(15)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def config(self, hosts, **extra):
        cfg = {"base": self.base, "days": 3, "backoff": [0.2, 1.0],
               "hosts": {h: sender_argv(self.src[h]) for h in hosts}}
        cfg.update(extra)
        tmp = self.conf + ".new"
        with open(tmp, "w") as fh:
            json.dump(cfg, fh)
        os.replace(tmp, self.conf)
        return cfg

    def start(self, hosts):
        cfg = self.config(hosts)
        self.receiver = fs.Receiver(cfg, statvfs=big_disk, config_path=self.conf, reload_s=0.2)
        self.thread = threading.Thread(target=self.receiver.run, daemon=True)
        self.thread.start()

    def same(self, host):
        return lambda: mirror_tree(os.path.join(self.base, "mirror", host)) == source_tree(self.src[host])

    def linked(self):
        return sorted(l.host for l in self.receiver.links)

    def test_a_host_added_and_one_taken_out(self):
        ca, cb = FakeCollector(self.src["a"]), FakeCollector(self.src["b"])
        ca.append("exits", b'{"a":1}\n' * 50)
        cb.append("exits", b'{"b":1}\n' * 50)
        self.start(["a"])
        self.assertTrue(wait_for(self.same("a")))
        self.assertFalse(os.path.exists(os.path.join(self.base, "mirror", "b")))
        self.config(["a", "b"])
        self.assertTrue(wait_for(self.same("b")), "the host added was never copied")
        self.assertTrue(wait_for(lambda: self.linked() == ["a", "b"]))
        self.config(["b"])
        self.assertTrue(wait_for(lambda: self.linked() == ["b"]))
        before = mirror_tree(os.path.join(self.base, "mirror", "a"))
        ca.append("exits", b'{"a":2}\n' * 10)
        cb.append("exits", b'{"b":2}\n' * 10)
        self.assertTrue(wait_for(self.same("b")))
        time.sleep(0.5)
        self.assertEqual(mirror_tree(os.path.join(self.base, "mirror", "a")), before,
                         "a host taken out is still copied")
        self.assertTrue(self.thread.is_alive())
        self.receiver.write_status()
        with open(os.path.join(self.base, "state", "status.json")) as fh:
            self.assertEqual(sorted(json.load(fh)["hosts"]), ["b"])

    def test_sighup_and_a_file_that_does_not_load(self):
        FakeCollector(self.src["a"]).append("exits", b'{"a":1}\n' * 5)
        FakeCollector(self.src["b"]).append("exits", b'{"b":1}\n' * 5)
        self.start(["a"])
        self.assertTrue(wait_for(self.same("a")))
        with open(self.conf, "w") as fh:
            fh.write("{not json")
        time.sleep(0.6)                 # several reload rounds: the file changed, and is bad
        self.assertEqual(self.linked(), ["a"])
        self.assertTrue(self.thread.is_alive())
        # the signal's way: the flag SIGHUP sets
        self.config(["a", "b"])
        self.receiver.reload_wanted.set()
        self.assertTrue(wait_for(lambda: self.linked() == ["a", "b"]))
        self.assertTrue(wait_for(self.same("b")))


if __name__ == "__main__":
    unittest.main()
