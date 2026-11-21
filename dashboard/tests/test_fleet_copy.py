"""The tail over the copy that deploy/fleet/pull.py makes of a node's files.

The copy must read as the node's own folder does: every record once, while the node's
collector appends to a day, ends a line between two passes, and seals the day between two
passes (test_gz.py's `test_lines_written_just_before_the_compression_arrive_once`, across
rsync). pull.py runs for real, with rsync, through an ssh stand-in that runs the node's
side in a folder here.

    cd dashboard && .venv/bin/python -m unittest tests.test_fleet_copy -q
"""
import gzip
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from rc_dashboard import gz                             # noqa: E402
from rc_dashboard.tail import FileTailSource            # noqa: E402
from test_ingest import exit_rec, line                  # noqa: E402

PULL = Path(__file__).resolve().parents[2] / "deploy" / "fleet" / "pull.py"
DAY = "2026-11-20"
SSH = r'''#!/bin/bash
while [ $# -gt 0 ]; do
  case $1 in -i|-o|-p|-l|-F|-S|-J) shift 2 ;; -*) shift ;; *) host=${1#*@}; shift; break ;; esac
done
cd "$FAKE_NODES/$host" || exit 255
exec sh -c "$*"
'''


@unittest.skipUnless(shutil.which("rsync"), "rsync is not installed")
class TestTheCopyReadsAsTheNode(unittest.TestCase):
    def setUp(self):
        gz._indexes.clear()
        self.tmp = tempfile.mkdtemp(prefix="fleet-copy-")
        self.nodes = os.path.join(self.tmp, "nodes")
        self.mirror = os.path.join(self.tmp, "mirror")
        self.src = os.path.join(self.nodes, "n1", "ebpfm", DAY, "n1.exits.jsonl")
        self.copy = os.path.join(self.mirror, "ebpf", "n1", DAY, "n1.exits.jsonl")
        os.makedirs(os.path.dirname(self.src))
        ssh = os.path.join(self.tmp, "ssh")
        with open(ssh, "w") as fh:
            fh.write(SSH)
        os.chmod(ssh, 0o755)
        self.conf = os.path.join(self.tmp, "fleet.env")
        with open(self.conf, "w") as fh:
            fh.write("FLEET_HOSTS=n1\nFLEET_SSH=%s\nFLEET_SSH_KEY=\nFLEET_EBPF_SOURCE=ebpfm\n"
                     "FLEET_NODE_SOURCE=\nFLEET_MIRROR=%s\nFLEET_FREE_MIN_GB=0\n"
                     % (ssh, self.mirror))
        base = int(time.time()) - 3600
        self.raw = [line(exit_rec(base + i, pid=i)) for i in range(10)]

    def tearDown(self):
        gz._indexes.clear()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def node_writes(self, data):
        with open(self.src, "ab") as fh:
            fh.write(data)

    def node_seals(self):
        """The collector's `gzip -f`: the whole .gz, then no plain file."""
        with open(self.src, "rb") as fh, gzip.open(self.src + ".gz", "wb") as out:
            out.write(fh.read())
        os.unlink(self.src)

    def pull(self):
        p = subprocess.run([sys.executable, str(PULL), "--config", self.conf, "once"],
                           env=dict(os.environ, FAKE_NODES=self.nodes), stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, universal_newlines=True, timeout=120)
        self.assertEqual(p.returncode, 0, p.stdout)

    @staticmethod
    def pids(recs):
        return [r["pid"] for r in recs]

    def test_every_record_once_through_appends_a_cut_line_and_the_seal(self):
        tail = FileTailSource([os.path.join(self.mirror, "ebpf", "n1")], days=8)
        self.node_writes(b"".join(self.raw[:5]) + self.raw[5][:20])
        self.pull()
        got = self.pids(tail.poll())
        self.assertEqual(got, [0, 1, 2, 3, 4])
        ino = os.stat(self.copy).st_ino

        self.node_writes(self.raw[5][20:] + self.raw[6] + self.raw[7])
        self.pull()
        self.assertEqual(os.stat(self.copy).st_ino, ino, "a pass must not replace the copy")
        more = self.pids(tail.poll())
        self.assertEqual(more, [5, 6, 7])
        got += more

        # The day's last lines land after the last pass, and the day is sealed before
        # the next: the copy's plain file never had them. They come from the .gz.
        self.node_writes(self.raw[8] + self.raw[9])
        self.node_seals()
        self.pull()
        self.assertFalse(os.path.exists(self.copy))
        self.assertTrue(os.path.exists(self.copy + ".gz"))
        more = self.pids(tail.poll())
        self.assertEqual(more, [8, 9])
        got += more
        self.assertEqual(self.pids(tail.poll()), [])
        self.assertEqual(got, list(range(10)))
        self.assertEqual(tail.stats["resets"], 0)
        self.assertEqual(tail.stats["parse_errors"], 0)

    def test_a_tail_that_starts_on_a_sealed_copy_reads_it_once(self):
        self.node_writes(b"".join(self.raw[:6]))
        self.pull()
        self.node_writes(b"".join(self.raw[6:]))
        self.node_seals()
        self.pull()
        tail = FileTailSource([os.path.join(self.mirror, "ebpf", "n1")], days=8)
        self.assertEqual(self.pids(tail.poll()), list(range(10)))
        self.assertEqual(self.pids(tail.poll()), [])


if __name__ == "__main__":
    unittest.main()
