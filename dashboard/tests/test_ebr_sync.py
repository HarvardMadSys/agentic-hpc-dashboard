"""rc_dashboard/ebr.py is collector/lib/ebr.py, byte for byte.

The collector's copy is canonical: it writes the files, the dashboard reads
them, and a codec the two disagree on is a feed the dashboard misreads. The
collector's suite checks the same (test_ebr.DashboardSyncTest), as both suites
do for redact.py's rules.

    cd dashboard && uv run python -m unittest tests.test_ebr_sync -q
"""
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))

DASH = HERE / "rc_dashboard" / "ebr.py"
COLLECTOR = HERE.parent / "collector" / "lib" / "ebr.py"


class EbrSyncTest(unittest.TestCase):

    def test_the_dashboards_codec_is_the_collectors_byte_for_byte(self):
        if not COLLECTOR.exists():
            self.skipTest("no collector/lib/ebr.py alongside this dashboard")
        self.assertEqual(DASH.read_bytes(), COLLECTOR.read_bytes(),
                         "copy collector/lib/ebr.py over rc_dashboard/ebr.py; never edit "
                         "the dashboard's copy alone")

    def test_it_is_the_module_the_package_imports(self):
        from rc_dashboard import ebr
        self.assertEqual(Path(ebr.__file__).resolve(), DASH)
        self.assertEqual(ebr.VERSION, 2)


if __name__ == "__main__":
    unittest.main()
