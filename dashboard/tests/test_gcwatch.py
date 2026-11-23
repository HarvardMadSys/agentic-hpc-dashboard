"""gcwatch: `settle()` freezes what is alive, then hands the heap's free pages
back to the system where the C library can (glibc's `malloc_trim`).

    cd dashboard && uv run python -m unittest tests.test_gcwatch -q
"""
import gc
import platform
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rc_dashboard import gcwatch                       # noqa: E402


class SettleTest(unittest.TestCase):
    def setUp(self):
        self.state = dict(gcwatch._state)
        self.frozen = gc.get_freeze_count()
        # A first settle() here, whatever an earlier one in this process did,
        # so none of these is a full collection it did not ask for.
        gcwatch._state["full_at"] = None

    def tearDown(self):
        # settle() froze this whole process: leave the next test what it had.
        # What was frozen before cannot be told from what settle() froze, so a
        # process that came with some keeps all of it.
        if not self.frozen:
            gc.unfreeze()
        gcwatch._state.clear()
        gcwatch._state.update(self.state)

    def test_each_settle_hands_the_free_pages_back_once(self):
        pads = []
        with mock.patch.object(gcwatch, "_trim", lambda pad: pads.append(pad) or 1):
            gcwatch.settle()
            gcwatch.settle()
        self.assertEqual(pads, [0, 0])
        st = gcwatch.status()
        self.assertEqual(st["trims"], self.state["trims"] + 2)
        self.assertIsNotNone(st["last_trim_ms"])
        self.assertEqual(st["settles"], self.state["settles"] + 2)

    def test_no_lock_is_held_through_the_trim(self):
        # a settle() racing this one waits for the freeze, not the trim
        held = []
        with mock.patch.object(gcwatch, "_trim", lambda pad: held.append(gcwatch._lock.locked()) or 1):
            gcwatch.settle()
        self.assertEqual(held, [False])

    def test_with_no_malloc_trim_settle_still_freezes_and_says_none_ran(self):
        with mock.patch.object(gcwatch, "_trim", None):
            self.assertEqual(gcwatch.settle(), "froze")
        self.assertGreater(gc.get_freeze_count(), 0)
        st = gcwatch.status()
        self.assertEqual((st["trims"], st["last_trim_ms"]),
                         (self.state["trims"], self.state["last_trim_ms"]))

    def test_the_trim_comes_after_a_full_collection(self):
        # the cycles a full collection frees are what it hands back
        order = []
        real = gc.collect
        with mock.patch.object(gcwatch, "_trim", lambda pad: order.append("trim") or 1), \
                mock.patch.object(gc, "collect", lambda *a: order.append("collect") or real(*a)):
            gcwatch.settle()
            self.assertEqual(gcwatch.settle(full_every_s=0), "collected and froze")
        self.assertEqual(order, ["trim", "collect", "trim"])

    @unittest.skipUnless(platform.libc_ver()[0] == "glibc", "malloc_trim is glibc's")
    def test_glibc_has_malloc_trim(self):
        self.assertIsNotNone(gcwatch._trim)
        self.assertIn(gcwatch._trim(0), (0, 1))


if __name__ == "__main__":
    unittest.main()
