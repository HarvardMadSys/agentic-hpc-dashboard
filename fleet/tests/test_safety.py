"""The receiver's guards: the base folder checks, the watchdog, owner-only modes,
retention, the free-space pause, and a disk that fills mid-write."""
import errno
import os
import shutil
import stat
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
FLEET = os.path.dirname(HERE)
sys.path.insert(0, FLEET)
sys.path.insert(0, HERE)

import fleet_stream as fs                                     # noqa: E402
from fakecollector import FakeCollector                       # noqa: E402
from test_receiver import HOST, Rig, mirror_tree, source_tree  # noqa: E402

SCRIPT = os.path.join(FLEET, "fleet_stream.py")


def sender_argv(root):
    return [sys.executable, SCRIPT, "send", "--root", root,
            "--poll", "0.05", "--ping", "0.5", "--trailer-poll", "0.1"]


def wait_for(pred, timeout=15.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.05)
    return False


class TestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fleet-safety-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_base_mode_refused(self):
        p = os.path.join(self.tmp, "base")
        os.mkdir(p)
        os.chmod(p, 0o755)
        with self.assertRaises(fs.UnsafeBase):
            fs.SafeBase.open(p)

    def test_base_symlink_refused(self):
        real = os.path.join(self.tmp, "real")
        os.mkdir(real)
        os.chmod(real, 0o700)
        link = os.path.join(self.tmp, "link")
        os.symlink(real, link)
        with self.assertRaises(fs.UnsafeBase):
            fs.SafeBase.open(link)

    def test_base_wrong_owner_refused(self):
        p = os.path.join(self.tmp, "base")
        os.mkdir(p)
        os.chmod(p, 0o700)
        with self.assertRaises(fs.UnsafeBase):
            fs.SafeBase.open(p, uid=os.getuid() + 1)


class TestParents(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fleet-parents-")
        self.parent = os.path.join(self.tmp, "user")
        self.base = os.path.join(self.parent, "rc-dashboard")
        os.mkdir(self.parent, 0o700)
        os.mkdir(self.base, 0o700)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_parent_symlink_swap_trips(self):
        b = fs.SafeBase.open(self.base, parents=1)
        b.check()
        os.rename(self.parent, self.parent + ".old")
        os.symlink(self.parent + ".old", self.parent)
        with self.assertRaises(fs.UnsafeBase):
            b.check()

    def test_parent_must_be_private(self):
        os.chmod(self.parent, 0o755)
        with self.assertRaises(fs.UnsafeBase):
            fs.SafeBase.open(self.base, parents=1)


class TestNotPrivate(unittest.TestCase):
    """With `private` false (site.env's PRIVATE_TREE=false, on a host that only the team can
    log in to), modes, owners, and ACLs are the user's business. The base is still a real
    folder, reached through one handle, and a folder put in its place still trips."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fleet-open-")
        self.parent = os.path.join(self.tmp, "user")
        self.base = os.path.join(self.parent, "rc-dashboard")
        os.mkdir(self.parent, 0o700)
        os.mkdir(self.base, 0o700)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_open_folders_are_accepted(self):
        os.chmod(self.parent, 0o710)
        os.chmod(self.base, 0o750)
        b = fs.SafeBase.open(self.base, parents=1, private=False)
        b.check()
        b.close()

    def test_a_mode_change_does_not_trip(self):
        """An ACL that lets one more account read the copy changes the folders' modes (an
        ACL's mask shows as the group bits); with private false that is no trip."""
        b = fs.SafeBase.open(self.base, parents=1, private=False)
        os.chmod(self.parent, 0o710)
        os.chmod(self.base, 0o750)
        b.check()
        b.close()

    def test_a_replaced_folder_still_trips(self):
        b = fs.SafeBase.open(self.base, parents=1, private=False)
        os.rename(self.base, self.base + ".old")
        os.mkdir(self.base, 0o700)
        with self.assertRaises(fs.UnsafeBase):
            b.check()
        b.close()

    def test_a_symlink_is_still_refused(self):
        link = os.path.join(self.tmp, "link")
        os.symlink(self.base, link)
        with self.assertRaises(fs.UnsafeBase):
            fs.SafeBase.open(link, private=False)

    def test_private_is_the_default(self):
        os.chmod(self.base, 0o750)
        with self.assertRaises(fs.UnsafeBase):
            fs.SafeBase.open(self.base)


class ReceiverCase(unittest.TestCase):
    """A Receiver over one source host, run on a thread."""

    def setUp(self):
        self.src = tempfile.mkdtemp(prefix="fleet-src-")
        self.basedir = tempfile.mkdtemp(prefix="fleet-base-")
        os.chmod(self.basedir, 0o700)
        self.thread, self.result, self.receiver = None, [], None

    def tearDown(self):
        if self.receiver is not None:
            self.receiver.stop.set()
        if self.thread is not None:
            self.thread.join(15)
        shutil.rmtree(self.src, ignore_errors=True)
        shutil.rmtree(self.basedir, ignore_errors=True)
        shutil.rmtree(self.basedir + ".old", ignore_errors=True)
        shutil.rmtree(self.src + ".markers", ignore_errors=True)

    def start(self, days=3, cfg_extra=None, **kw):
        self.marker = os.path.join(self.src + ".markers", "TRIPPED")
        os.makedirs(os.path.dirname(self.marker), 0o700, exist_ok=True)
        cfg = {"base": self.basedir, "days": days, "hosts": {HOST: sender_argv(self.src)},
               "dashboard_unit": "rc-dashboard.scope", "backoff": [0.2, 1.0],
               "trip_marker": self.marker}
        cfg.update(cfg_extra or {})
        self.receiver = fs.Receiver(cfg, **kw)
        self.thread = threading.Thread(target=lambda: self.result.append(self.receiver.run()),
                                       daemon=True)
        self.thread.start()

    @property
    def mdir(self):
        return os.path.join(self.basedir, "mirror", HOST)

    def same(self):
        return mirror_tree(self.mdir) == source_tree(self.src)


class TestWatchdog(ReceiverCase):
    def test_swap_trips_watchdog(self):
        c = FakeCollector(self.src)
        c.append("exits", b'{"w":1}\n' * 100)
        called = []
        # The marker is written first: a dashboard start that slips past the stop sees it.
        self.start(watchdog_s=0.1, stop_dashboard=lambda: called.append(os.path.isfile(self.marker)))
        self.assertTrue(wait_for(self.same))
        os.rename(self.basedir, self.basedir + ".old")
        os.mkdir(self.basedir, 0o777)
        c.append("exits", b'{"after":1}\n' * 10)
        self.thread.join(1.5)
        self.assertFalse(self.thread.is_alive())
        self.assertEqual(self.result, [fs.EXIT_UNSAFE])
        self.assertEqual(called, [True])
        self.assertEqual(os.listdir(self.basedir), [])
        self.assertTrue(os.path.exists(os.path.join(self.basedir + ".old", "state", "TRIPPED")))
        self.assertTrue(os.path.isfile(self.marker))

    def test_dashboard_stopped_before_links(self):
        c = FakeCollector(self.src)
        c.append("exits", b'{"w":1}\n' * 10)
        links_alive = []
        self.start(watchdog_s=0.1,
                   stop_dashboard=lambda: links_alive.append(any(t.is_alive() for t in self.receiver.threads)))
        self.assertTrue(wait_for(self.same))
        os.rename(self.basedir, self.basedir + ".old")
        os.mkdir(self.basedir, 0o700)
        self.thread.join(5)
        self.assertEqual(links_alive, [True])

    def test_not_private_starts_on_an_open_folder_and_keeps_going(self):
        os.chmod(self.basedir, 0o750)
        c = FakeCollector(self.src)
        c.append("exits", b'{"w":1}\n' * 100)
        self.start(watchdog_s=0.1, cfg_extra={"private": False})
        self.assertTrue(wait_for(self.same))
        os.chmod(self.basedir, 0o710)
        c.append("exits", b'{"after":1}\n' * 10)
        self.assertTrue(wait_for(self.same))
        time.sleep(0.5)                 # several watchdog rounds after the change
        self.assertTrue(self.thread.is_alive())
        self.assertEqual(self.result, [])
        self.assertFalse(os.path.exists(self.marker))

    def test_start_refusal_stops_dashboard(self):
        os.chmod(self.basedir, 0o755)
        called = []
        self.start(stop_dashboard=lambda: called.append(os.path.isfile(self.marker)))
        self.thread.join(5)
        self.assertEqual(self.result, [fs.EXIT_UNSAFE])
        self.assertEqual(called, [True])
        self.assertTrue(os.path.isfile(self.marker))


class TestModes(unittest.TestCase):
    def setUp(self):
        self.old_umask = os.umask(0o022)
        self.rig = Rig()

    def tearDown(self):
        self.rig.cleanup()
        os.umask(self.old_umask)

    def test_modes_under_umask_022(self):
        c = FakeCollector(self.rig.src, day="2026-11-10")
        c.append("exits", b'{"m":1}\n' * 50)
        c.roll("2026-11-11", gzip=True)
        c.append("exits", b'{"m":2}\n')
        self.rig.start()
        self.assertTrue(wait_for(lambda: mirror_tree(self.rig.mdir) == source_tree(self.rig.src)))
        self.rig.counters.flush(self.rig.base)
        seen = 0
        for top, dirs, files in os.walk(self.rig.basedir):
            for d in dirs:
                self.assertEqual(stat.S_IMODE(os.stat(os.path.join(top, d)).st_mode), 0o700, d)
            for f in files:
                self.assertEqual(stat.S_IMODE(os.stat(os.path.join(top, f)).st_mode), 0o600, f)
                seen += 1
        self.assertGreaterEqual(seen, 5)


class TestRetentionAndSpace(ReceiverCase):
    def test_retention_keeps_days(self):
        c = FakeCollector(self.src, day="2026-11-09")
        c.append("exits", b'{"r":9}\n')
        c.roll("2026-11-10", gzip=True)
        c.append("exits", b'{"r":10}\n')
        c.roll("2026-11-11", gzip=True)
        c.append("exits", b'{"r":11}\n')
        old = os.path.join(self.mdir, "2026-11-09")
        os.makedirs(old, 0o700)
        os.chmod(os.path.join(self.basedir, "mirror"), 0o700)
        os.chmod(self.mdir, 0o700)
        with open(os.path.join(old, "%s.exits.jsonl" % c.host), "wb") as fh:
            fh.write(b'{"r":9}\n')
        self.start(days=2)
        self.assertTrue(wait_for(lambda: sorted(os.listdir(self.mdir)) == ["2026-11-09", "2026-11-10", "2026-11-11"]
                                 and len(os.listdir(os.path.join(self.mdir, "2026-11-11"))) == 2
                                 and self.receiver.links[0].mirror.verified))
        self.receiver.retention()
        self.assertEqual(sorted(os.listdir(self.mdir)), ["2026-11-10", "2026-11-11"])
        self.assertEqual(sorted(os.listdir(self.src)), ["2026-11-09", "2026-11-10", "2026-11-11"])

    def test_free_space_pauses_and_resumes(self):
        c = FakeCollector(self.src)
        c.append("exits", b'{"f":1}\n')
        space = {"free": 3e12}
        self.start(statvfs=lambda: (space["free"], 3.5e12), space_s=0.1)
        self.assertTrue(wait_for(self.same))
        space["free"] = 100e9
        self.assertTrue(wait_for(lambda: self.receiver.paused.is_set()))
        time.sleep(0.3)
        size_paused = os.path.getsize(os.path.join(self.mdir, c.key("exits")))
        c.append("exits", b'{"f":2}\n' * 100)
        time.sleep(1.0)
        self.assertEqual(os.path.getsize(os.path.join(self.mdir, c.key("exits"))), size_paused)
        space["free"] = 3e12
        self.assertTrue(wait_for(self.same))
        self.assertFalse(self.receiver.paused.is_set())


    def test_the_floor_is_a_setting(self):
        """200 GB free of 1 TB pauses a receiver on the default floor (300 GB), and
        not one whose config sets free_min_gb 50 (the floor is then 10%, 100 GB)."""
        self.assertEqual(fs.free_floor({}, 1e12), 300e9)
        self.assertEqual(fs.free_floor({"free_min_gb": 50}, 1e12), 100e9)
        self.assertEqual(fs.free_floor({"free_min_gb": 50, "free_min_fraction": 0.01}, 1e12), 50e9)
        self.assertEqual(fs.free_floor({}, 10e12), 1e12)
        c = FakeCollector(self.src)
        c.append("exits", b'{"f":1}\n')
        self.start(statvfs=lambda: (200e9, 1e12), space_s=0.1, cfg_extra={"free_min_gb": 50})
        self.assertTrue(wait_for(self.same))
        time.sleep(0.3)
        self.assertFalse(self.receiver.paused.is_set())


class TestStatusFile(ReceiverCase):
    def test_status_has_epoch(self):
        FakeCollector(self.src).append("exits", b'{"s":1}\n')
        self.start()
        p = os.path.join(self.basedir, "state", "status.json")
        self.assertTrue(wait_for(lambda: os.path.exists(p)))
        import json
        st = json.load(open(p))
        self.assertLess(abs(st["at_epoch"] - time.time()), 30)
        self.assertIn("down_since", st["hosts"][HOST])


class TestDiskFull(unittest.TestCase):
    def setUp(self):
        self.rig = Rig()

    def tearDown(self):
        self.rig.cleanup()

    def test_enospc_truncates_frame(self):
        c = FakeCollector(self.rig.src, streams=("exits",))
        calls, cut = [0], {}

        def write(fd, data):
            calls[0] += 1
            if calls[0] == 3:
                cut["off"] = os.fstat(fd).st_size
                os.write(fd, bytes(data[:max(1, len(data) // 2)]))
                raise OSError(errno.ENOSPC, "No space left on device")
            return os.write(fd, data)

        self.rig.start(write=write)
        for i in range(5):
            c.append("exits", b'{"n":%d}\n' % i * 20)
            time.sleep(0.2)
        self.assertTrue(wait_for(lambda: self.rig.paused.is_set()))
        copy = os.path.join(self.rig.mdir, c.key("exits"))
        self.assertEqual(os.path.getsize(copy), cut["off"])
        self.rig.paused.clear()
        self.assertTrue(wait_for(lambda: mirror_tree(self.rig.mdir) == source_tree(self.rig.src)))


if __name__ == "__main__":
    unittest.main()
