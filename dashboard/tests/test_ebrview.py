"""The EBR view (ebrview.py): the service reads EBR only, converting what the
collector wrote only as JSONL at startup and keeping the copies current.

A view must hold exactly the records the JSONL tail would have read, once each,
through appends, partial lines, crashes at any byte, a source truncated or
replaced, and the collector's nightly gzip; it links the collector's own EBR
files instead of converting them, by the twin rule with prefer=ebr, and keeps
each decision across restarts. The app reads only the view, answers 503 to what
reads the day-files while it is being filled, and gives the answers it gives
without one.

    cd dashboard && uv run python -m unittest tests.test_ebrview -q
"""
import gzip
import json
import math
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import warnings
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from rc_dashboard import config as configmod       # noqa: E402
from rc_dashboard import ebr, ebrview, fastjson    # noqa: E402

DAY = "2026-11-14"
DAY2 = "2026-11-15"
T0 = 1_794_000_000.0


def stamp(t):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t))


def rec(t, pid, host="h1", event="exit", **kw):
    r = {"schema_version": 8, "event": event, "ts": stamp(t), "ts_epoch": t, "host": host,
         "user": "alice", "actor3": "agent", "agent_type": "claude_code", "comm": "git",
         "args": "git status %d" % pid, "pid": pid, "ppid": 1, "cpu_s": 0.25,
         "duration_s": 1.5}
    r.update(kw)
    return r


def line(r):
    return (json.dumps(r) + "\n").encode()


def same(a, b):
    """Type- and key-order-exact equality (NaN equals NaN, -0.0 is not 0.0)."""
    if type(a) is not type(b):
        return False
    if isinstance(a, dict):
        return list(a) == list(b) and all(same(a[k], b[k]) for k in a)
    if isinstance(a, list):
        return len(a) == len(b) and all(same(x, y) for x, y in zip(a, b))
    if isinstance(a, float):
        if a != a:
            return b != b
        return a == b and math.copysign(1.0, a) == math.copysign(1.0, b)
    return a == b


def tail_records(data):
    """What the JSONL tail yields of `data`: the complete lines' records, every
    one `fastjson.loads` takes that is an object (`tail._read_one`)."""
    out = []
    end = data.rfind(b"\n") + 1
    for ln in data[:end].split(b"\n"):
        if not ln.strip():
            continue
        try:
            r = fastjson.loads(ln)
        except ValueError:
            continue
        if isinstance(r, dict):
            out.append(r)
    return out


def view_records(path):
    return [r for _pos, r in ebr.iter_records(path)]


def gzip_in_place(path):
    """The collector's nightly gzip: `x.gz` written whole, then `x` removed."""
    with open(path, "rb") as src, gzip.open(path + ".gz", "wb") as dst:
        shutil.copyfileobj(src, dst)
    os.unlink(path)
    return path + ".gz"


class ViewCase(unittest.TestCase):
    """A scratch collector root per tier, and a view of it."""

    DAYS = 3

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rc_ebrview_")
        self.root = os.path.join(self.tmp, "ebpf")
        self.node = os.path.join(self.tmp, "node")
        os.makedirs(os.path.join(self.root, DAY))
        self.views = []

    def tearDown(self):
        for v in self.views:
            v.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def cfg(self, node=False, **convert):
        tree = configmod.defaults()
        tree["feeds"]["ebpf"]["roots"] = [self.root]
        tree["feeds"]["ebpf"]["days"] = self.DAYS
        tree["feeds"]["ebpf_node"]["roots"] = [self.node] if node else \
            [os.path.join(self.tmp, "no-node")]
        tree["cache"]["dir"] = os.path.join(self.tmp, "cache")
        if getattr(self, "RETENTION", None):
            tree["live"]["retention_min"] = self.RETENTION
        tree["ingest"]["convert"].update(convert)
        return configmod.Config(tree, {}, None)

    def view(self, node=False, catch_up=True, **convert):
        cfg = self.cfg(node, **convert)
        v = ebrview.EbrView.from_config(cfg)
        self.assertIsNotNone(v)
        v.prepare()
        self.views.append(v)
        self.vroot = cfg.get("feeds.ebpf.roots")[0]
        self.cfg_ = cfg
        if catch_up:
            v.catch_up()
        return v

    def restart(self, v, **kw):
        v.close()
        self.views.remove(v)
        return self.view(**kw)

    def src(self, name, day=DAY):
        return os.path.join(self.root, day, name) if day else os.path.join(self.root, name)

    def vpath(self, name, day=DAY):
        return os.path.join(self.vroot, day, name) if day else os.path.join(self.vroot, name)

    def append(self, name, data, day=DAY):
        p = self.src(name, day)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "ab") as fh:
            fh.write(data if isinstance(data, bytes) else b"".join(line(r) for r in data))
        return p

    def settle(self, v, polls=200):
        """Sync until no bulk conversion is running apart (a seal, a rebuild)."""
        for _ in range(polls):
            v.sync()
            if not any(e.job is not None for e in v.entries.values()):
                return
            time.sleep(0.01)
        self.fail("a bulk conversion never finished")

    def entry(self, v, name, day=DAY):
        d = os.path.join(self.vroot, day) if day else self.vroot
        stem = ebrview.split_name(name)[0]
        return v.entries.get((d, stem))

    def assertConverted(self, name, day=DAY):
        """The view file holds exactly what the JSONL tail reads of its source."""
        src = self.src(name, day)
        if not os.path.exists(src):
            src += ".gz"
        with (gzip.open(src, "rb") if src.endswith(".gz") else open(src, "rb")) as fh:
            want = tail_records(fh.read())
        got = view_records(self.vpath(ebrview.split_name(name)[0] + ".ebr", day))
        self.assertEqual(len(got), len(want))
        for i, (a, b) in enumerate(zip(got, want)):
            self.assertTrue(same(a, b), "record %d: %r != %r" % (i, a, b))
        return got


# ------------------------------------------------------------------ the records
class ExactTest(ViewCase):

    def test_records_are_the_tails_type_for_type(self):
        t = T0
        tricky = [
            rec(t, 1, f=1.0, i=1, neg0=-0.0, big=2 ** 70, nbig=-(2 ** 80), tiny=5e-324,
                huge=1.7976931348623157e308, s="héllo ☃ \U0001F600", esc='a"b\\c\nd',
                n=None, yes=True, no=False, lst=[1, "x", None, [2, {"k": 3}]],
                d={"z": 1, "a": 2, "m": {"q": [1.5]}}, ed={}, el=[]),
            rec(t + 1, 2, args="", cpu_s=0, duration_s=0.0),
        ]
        raw = b"".join(line(r) for r in tricky)
        # what only the fallback parser takes, and keys out of order or twice
        raw += (b'{"event": "exit", "ts_epoch": %r, "lone": "\\ud800", "nan": NaN, '
                b'"inf": Infinity}\n' % (t + 2))
        raw += b'{"b": 1, "a": 2, "b": 3, "ts_epoch": %r}\n' % (t + 3)
        raw += b'{"ts_epoch": 1794000004, "w": 18446744073709551616}\n'
        self.append("h1.exits.jsonl", raw)
        self.view()
        got = self.assertConverted("h1.exits.jsonl")
        self.assertEqual(len(got), 5)
        self.assertEqual(list(got[3]), ["b", "a", "ts_epoch"])

    def test_blank_malformed_and_non_object_lines_are_bad_lines_counted(self):
        raw = b"".join(line(rec(T0 + i, i)) for i in range(3))
        raw += b"\n   \n\t\r\n"                       # blank: passed, not counted
        raw += b"{not json\n[1, 2]\n42\n\"s\"\nnull\n"   # 5 bad lines
        raw += b"".join(line(rec(T0 + 10 + i, 10 + i)) for i in range(2))
        raw += b'{"half": '                            # still being written
        self.append("h1.exits.jsonl", raw)
        v = self.view()
        self.assertConverted("h1.exits.jsonl")
        e = self.entry(v, "h1.exits.jsonl")
        self.assertEqual((e.records, e.bad), (5, 5))
        info, _end, k = ebr.last_mark(self.vpath("h1.exits.ebr"))
        self.assertEqual((info["records"], info["bad"], k), (5, 5, 0))
        self.assertEqual(info["off"], len(raw) - len(b'{"half": '))
        st = v.status("ebpf")
        self.assertEqual((st["records"], st["bad_lines"]), (5, 5))

    def test_appends_over_several_syncs_and_a_partial_line_held(self):
        self.append("h1.exits.jsonl", [rec(T0, 1)])
        v = self.view()
        want = [rec(T0, 1)]
        for i in range(2, 30):
            r = rec(T0 + i, i)
            want.append(r)
            data = line(r)
            cut = len(data) // 2
            self.append("h1.exits.jsonl", data[:cut])
            v.sync()                    # half a line: nothing new
            self.assertEqual(len(view_records(self.vpath("h1.exits.ebr"))), i - 1)
            self.append("h1.exits.jsonl", data[cut:])
            v.sync()
            self.assertEqual(len(view_records(self.vpath("h1.exits.ebr"))), i)
        self.assertEqual(view_records(self.vpath("h1.exits.ebr")), want)
        self.assertEqual(self.entry(v, "h1.exits.jsonl").lag, 0)

    def test_a_bulk_conversion_cuts_a_frame_per_minute(self):
        recs = [rec(T0 + 7 * i, i) for i in range(100)]          # 700 s
        recs.insert(50, rec(recs[49]["ts_epoch"] - 90, 999))      # late: joins its frame
        self.append("h1.exits.jsonl", recs)
        self.view()
        frames = [f for f in ebr.frames(self.vpath("h1.exits.ebr")) if f.kind == ebr.DATA]
        minutes = {int(r["ts_epoch"] // 60) for r in recs}
        self.assertEqual(len(frames), len(minutes))     # the late record is in its round's
        for f in frames:
            self.assertLessEqual(int(f.ts_max // 60) - int(f.ts_min // 60), 2)
        self.assertConverted("h1.exits.jsonl")

    def test_a_poll_is_one_flush_and_a_mark_at_most_a_minute_apart(self):
        self.append("h1.exits.jsonl", [rec(T0, 1)])
        v = self.view()
        p = self.vpath("h1.exits.ebr")
        marks = lambda: sum(1 for f in ebr.frames(p) if f.kind == ebr.MARK)   # noqa: E731
        m0 = marks()
        with mock.patch.object(ebr.Writer, "flush", autospec=True,
                               side_effect=ebr.Writer.flush) as fl:
            self.append("h1.exits.jsonl", [rec(T0 + 100 + i, 100 + i) for i in range(500)])
            v.sync()
            self.assertEqual(fl.call_count, 1)
        self.assertEqual(marks(), m0)               # a minute has not passed
        e = self.entry(v, "h1.exits.jsonl")
        e.marked_at -= ebrview.MARK_EVERY_S
        self.append("h1.exits.jsonl", [rec(T0 + 700, 700)])
        v.sync()
        self.assertEqual(marks(), m0 + 1)
        info, _end, k = ebr.last_mark(p)
        self.assertEqual((info["records"], k), (502, 0))
        v.close()                                   # nothing new: no MARK
        self.assertEqual(marks(), m0 + 1)
        self.assertConverted("h1.exits.jsonl")


# --------------------------------------------------------------- checkpoints
class ResumeTest(ViewCase):

    def test_a_crash_at_any_byte_of_the_last_write_resumes_every_record_once(self):
        self.append("h1.exits.jsonl", [rec(T0 + i, i) for i in range(40)])
        v = self.view()
        p = self.vpath("h1.exits.ebr")
        before = os.path.getsize(p)
        self.entry(v, "h1.exits.jsonl").marked_at -= ebrview.MARK_EVERY_S   # a MARK is due
        self.append("h1.exits.jsonl", [rec(T0 + 100 + i, 100 + i) for i in range(6)])
        self.append("h1.exits.jsonl", b"\n{oops\n")
        v.sync()
        v.close()
        self.views.remove(v)
        whole = Path(p).read_bytes()
        self.assertGreater(len(whole), before)
        kinds = [f.kind for f in ebr.frames(p, before)]
        self.assertEqual(kinds, [ebr.DATA, ebr.MARK])    # the batch, and its MARK
        for cut in range(before, len(whole)):
            with self.subTest(cut=cut - before):
                with open(p, "wb") as fh:
                    fh.write(whole[:cut])
                ino = os.stat(p).st_ino
                v = self.view()
                self.assertConverted("h1.exits.jsonl")
                e = self.entry(v, "h1.exits.jsonl")
                self.assertEqual((e.records, e.bad), (46, 1))
                self.assertEqual(os.stat(p).st_ino, ino)        # resumed, not rebuilt
                # and it goes on from there
                self.append("h1.exits.jsonl", [rec(T0 + 500, 500 + cut)])
                v.sync()
                self.assertEqual(len(view_records(p)), 47)
                v.close()
                self.views.remove(v)
                os.truncate(self.src("h1.exits.jsonl"),
                            os.path.getsize(self.src("h1.exits.jsonl")) - len(
                                line(rec(T0 + 500, 500 + cut))))

    def test_a_crash_before_the_first_mark_passes_what_the_file_holds(self):
        recs = [rec(T0 + i, i) for i in range(20)]
        self.append("h1.exits.jsonl", recs)
        cfg = self.cfg()
        vroot = os.path.join(ebrview.settings(cfg)["dir"], "ebpf",
                             ebrview.view_root_name(self.root))
        os.makedirs(os.path.join(vroot, DAY))
        with ebr.Writer(os.path.join(vroot, DAY, "h1.exits.ebr")) as w:
            for r in recs[:7]:
                w.write(r)                      # 7 records, no MARK: a crash
        self.view()
        self.assertConverted("h1.exits.jsonl")

    def test_a_source_cut_short_or_replaced_is_converted_afresh(self):
        self.append("h1.exits.jsonl", [rec(T0 + i, i) for i in range(30)])
        v = self.view()
        p = self.vpath("h1.exits.ebr")
        ino = os.stat(p).st_ino
        # cut short, below what was converted
        os.truncate(self.src("h1.exits.jsonl"), len(line(rec(T0, 0))) * 5)
        self.settle(v)
        self.assertNotEqual(os.stat(p).st_ino, ino)
        self.assertConverted("h1.exits.jsonl")
        ino = os.stat(p).st_ino
        # replaced by another file: a new inode with other first bytes
        os.unlink(self.src("h1.exits.jsonl"))
        self.append("h1.exits.jsonl", [rec(T0 + 1000 + i, 1000 + i) for i in range(40)])
        self.settle(v)
        self.assertNotEqual(os.stat(p).st_ino, ino)
        self.assertConverted("h1.exits.jsonl")
        # and on a restart, checked the same way
        ino = os.stat(p).st_ino
        v.close()
        os.truncate(self.src("h1.exits.jsonl"), len(line(rec(T0 + 1000, 1000))) * 3)
        self.restart(v)
        self.assertNotEqual(os.stat(p).st_ino, ino)
        self.assertConverted("h1.exits.jsonl")

    def test_a_source_rewritten_in_place_to_its_length_is_converted_afresh(self):
        # An append always grows a source, so the same length with a new mtime is
        # a rewrite in place, whose first bytes may even match: converted afresh,
        # live and on a restart (against the last MARK's record of the source).
        a, b = rec(T0, 1), rec(T0, 2)
        self.assertEqual(len(line(a)), len(line(b)))
        s = self.append("h1.exits.jsonl", [a])
        v = self.view()
        p = self.vpath("h1.exits.ebr")

        def rewrite(r, later_s):
            st = os.stat(s)
            with open(s, "r+b") as fh:
                fh.write(line(r))
            os.utime(s, ns=(st.st_atime_ns, st.st_mtime_ns + int(later_s * 1e9)))
            self.assertEqual((os.stat(s).st_ino, os.stat(s).st_size), (st.st_ino, st.st_size))

        ino = os.stat(p).st_ino
        rewrite(b, 1)
        self.settle(v)
        self.assertNotEqual(os.stat(p).st_ino, ino)
        self.assertEqual([r["pid"] for r in view_records(p)], [2])
        self.assertConverted("h1.exits.jsonl")
        # untouched since, a poll leaves it be
        ino = os.stat(p).st_ino
        self.settle(v)
        self.assertEqual(os.stat(p).st_ino, ino)
        # and on a restart
        v.close()
        rewrite(a, 2)
        self.restart(v)
        self.assertNotEqual(os.stat(p).st_ino, ino)
        self.assertEqual([r["pid"] for r in view_records(p)], [1])
        self.assertConverted("h1.exits.jsonl")

    def test_a_source_rewritten_while_stopped_past_its_head_is_converted_afresh(self):
        # The same length and the same first 4 KB, a later record changed while
        # the service was down: the startup's fast path must not keep the old copy.
        recs = [rec(T0 + i, 1000 + i) for i in range(200)]
        s = self.append("h1.exits.jsonl", recs)
        self.assertGreater(os.path.getsize(s), ebrview.HEAD_BYTES)
        v = self.view()
        p = self.vpath("h1.exits.ebr")
        ino = os.stat(p).st_ino
        v.close()
        last = rec(T0 + 199, 2199)
        self.assertEqual(len(line(last)), len(line(recs[-1])))
        st = os.stat(s)
        with open(s, "r+b") as fh:
            fh.seek(st.st_size - len(line(last)))
            fh.write(line(last))
        os.utime(s, ns=(st.st_atime_ns, st.st_mtime_ns + 10 ** 9))
        v = self.restart(v)
        self.settle(v)
        self.assertNotEqual(os.stat(p).st_ino, ino)
        self.assertEqual(view_records(p)[-1]["pid"], 2199)
        self.assertConverted("h1.exits.jsonl")

    def test_a_copy_of_the_source_is_read_on_not_converted_again(self):
        self.append("h1.exits.jsonl", [rec(T0 + i, i) for i in range(30)])
        v = self.view()
        p = self.vpath("h1.exits.ebr")
        ino, head = os.stat(p).st_ino, Path(p).read_bytes()
        # the same bytes and more under a new inode (a copy renamed into place)
        s = self.src("h1.exits.jsonl")
        with open(s, "rb") as fh:
            data = fh.read()
        os.unlink(s)
        self.append("h1.exits.jsonl", data + line(rec(T0 + 99, 99)))
        self.settle(v)
        self.assertEqual(os.stat(p).st_ino, ino)
        self.assertTrue(Path(p).read_bytes().startswith(head))
        self.assertConverted("h1.exits.jsonl")

    def test_a_sealed_day_is_finished_from_its_gz_then_left_alone(self):
        self.append("h1.exits.jsonl", [rec(T0 + i, i) for i in range(50)])
        v = self.view()
        p = self.vpath("h1.exits.ebr")
        ino, head = os.stat(p).st_ino, Path(p).read_bytes()
        # the last lines of the day, then the collector's gzip, before a poll
        self.append("h1.exits.jsonl", [rec(T0 + 100 + i, 100 + i) for i in range(5)])
        gz = gzip_in_place(self.src("h1.exits.jsonl"))
        conv = []
        real = ebrview.feed_lines

        def counting(data, w, c):
            conv.append(len(tail_records(data)))
            return real(data, w, c)
        with mock.patch.object(ebrview, "feed_lines", counting):
            self.settle(v)
        self.assertEqual(sum(conv), 5)          # the earlier lines are not converted again
        self.assertEqual(os.stat(p).st_ino, ino)
        self.assertTrue(Path(p).read_bytes().startswith(head))
        self.assertConverted("h1.exits.jsonl")
        info, _end, k = ebr.last_mark(p)
        st = os.stat(gz)
        self.assertTrue(info["sealed"])
        self.assertEqual((info["src"], info["src_size"], info["src_ino"], k),
                         ("h1.exits.jsonl.gz", st.st_size, st.st_ino, 0))
        e = self.entry(v, "h1.exits.jsonl")
        self.assertTrue(e.sealed)
        self.assertEqual(os.path.getmtime(p), os.path.getmtime(gz))
        # a restart reads nothing: no inflating, no conversion, no write
        size, mtime = os.path.getsize(p), os.stat(p).st_mtime_ns
        with mock.patch.object(ebrview._GzPump, "run", side_effect=AssertionError("inflated")):
            v = self.restart(v)
            self.settle(v)
        self.assertEqual(v.progress["files"], 0)
        self.assertEqual((os.path.getsize(p), os.stat(p).st_mtime_ns), (size, mtime))
        self.assertTrue(self.entry(v, "h1.exits.jsonl").sealed)

    def test_a_day_sealed_while_the_service_was_down(self):
        self.append("h1.exits.jsonl", [rec(T0 + i, i) for i in range(30)])
        v = self.view()
        v.close()
        self.append("h1.exits.jsonl", [rec(T0 + 100 + i, 100 + i) for i in range(4)])
        gzip_in_place(self.src("h1.exits.jsonl"))
        self.restart(v)
        self.assertConverted("h1.exits.jsonl")
        self.assertTrue(ebr.last_mark(self.vpath("h1.exits.ebr"))[0]["sealed"])

    def test_a_sealed_day_whose_gz_is_another_file_is_converted_afresh(self):
        self.append("h1.exits.jsonl", [rec(T0 + i, i) for i in range(30)])
        gzip_in_place(self.src("h1.exits.jsonl"))
        v = self.view()
        p = self.vpath("h1.exits.ebr")
        ino = os.stat(p).st_ino
        v.close()
        os.unlink(self.src("h1.exits.jsonl.gz"))
        self.append("h1.exits.jsonl", [rec(T0 + 500 + i, 500 + i) for i in range(10)])
        gzip_in_place(self.src("h1.exits.jsonl"))
        self.restart(v)
        self.assertNotEqual(os.stat(p).st_ino, ino)
        self.assertConverted("h1.exits.jsonl")


# --------------------------------------------------------------- decisions
class DecisionTest(ViewCase):

    def ebr_file(self, name, recs, day=DAY):
        p = self.src(name, day)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with ebr.Writer.open_append(p) as w:
            for r in recs:
                w.write(r)
        return p

    def test_a_collectors_ebr_file_is_linked_then_relinked_to_its_gz(self):
        p = self.ebr_file("h1.exits.ebr", [rec(T0 + i, i) for i in range(10)])
        v = self.view()
        link = self.vpath("h1.exits.ebr")
        self.assertTrue(os.path.islink(link))
        self.assertEqual(os.readlink(link), p)
        st = v.status("ebpf")
        self.assertEqual(st["counts"], {"converted": 0, "linked": 1, "held": 0})
        gzip_in_place(p)
        order = []
        real_replace, real_unlink = os.replace, os.unlink

        def replace(a, b):
            if str(b).startswith(self.vroot):      # not another thread's file
                order.append(("link", os.path.basename(b)))
            return real_replace(a, b)

        def unlink(a):
            if str(a).startswith(self.vroot) and not os.path.basename(a).startswith("."):
                order.append(("unlink", os.path.basename(a)))
            return real_unlink(a)
        with mock.patch.object(ebrview.os, "replace", replace), \
                mock.patch.object(ebrview.os, "unlink", unlink):
            v.sync()
        self.assertEqual(order, [("link", "h1.exits.ebr.gz"), ("unlink", "h1.exits.ebr")])
        self.assertFalse(os.path.lexists(link))
        self.assertEqual(os.readlink(link + ".gz"), p + ".gz")
        self.assertEqual(len(view_records(link + ".gz")), 10)

    def test_twins_link_the_ebr_twin(self):
        recs = [rec(T0 + i, i) for i in range(10)]
        self.append("h1.exits.jsonl", recs)
        self.ebr_file("h1.exits.ebr", recs)
        v = self.view()
        self.assertTrue(os.path.islink(self.vpath("h1.exits.ebr")))
        self.assertEqual(v.status("ebpf")["counts"]["linked"], 1)

    def test_an_ebr_twin_started_mid_day_has_its_jsonl_converted(self):
        recs = [rec(T0 + 60 * i, i) for i in range(10)]
        self.append("h1.exits.jsonl", recs)
        self.ebr_file("h1.exits.ebr", recs[5:])
        v = self.view()
        p = self.vpath("h1.exits.ebr")
        self.assertFalse(os.path.islink(p))
        self.assertConverted("h1.exits.jsonl")
        self.assertEqual(v.status("ebpf")["counts"]["converted"], 1)

    def test_a_held_stream_has_nothing_until_a_twin_holds_a_record(self):
        self.append("h1.exits.jsonl", b'{"half": ')
        self.ebr_file("h1.exits.ebr", [])
        open(self.src("h1.exits.ebr"), "ab").close()
        v = self.view()
        self.assertEqual(os.listdir(self.vroot), [ebrview.MARKER])
        self.assertEqual(v.status("ebpf")["counts"], {"converted": 0, "linked": 0, "held": 1})
        recs = [rec(T0 + i, i) for i in range(3)]
        self.append("h1.exits.jsonl", b"1}\n" + b"".join(line(r) for r in recs))
        self.ebr_file("h1.exits.ebr", [{"half": 1}] + recs)
        v.sync()
        self.assertTrue(os.path.islink(self.vpath("h1.exits.ebr")))

    def test_decisions_persist_across_a_restart(self):
        recs = [rec(T0 + i, i) for i in range(10)]
        self.append("h1.exits.jsonl", recs)
        v = self.view()
        self.assertFalse(os.path.islink(self.vpath("h1.exits.ebr")))
        # an EBR twin with the same records: the twin rule would read it now
        self.ebr_file("h1.exits.ebr", recs)
        v = self.restart(v)
        self.assertFalse(os.path.islink(self.vpath("h1.exits.ebr")))
        self.assertConverted("h1.exits.jsonl")
        # and the other way: a link stays a link though only JSONL is newer
        self.ebr_file("h2.exits.ebr", recs)
        v.sync()
        self.assertTrue(os.path.islink(self.vpath("h2.exits.ebr")))
        self.append("h2.exits.jsonl", recs)
        v = self.restart(v)
        self.assertTrue(os.path.islink(self.vpath("h2.exits.ebr")))

    def test_a_stream_gone_from_the_source_is_gone_from_the_view(self):
        self.append("h1.exits.jsonl", [rec(T0, 1)])
        self.ebr_file("h2.exits.ebr", [rec(T0, 2)])
        v = self.view()
        os.unlink(self.src("h1.exits.jsonl"))
        os.unlink(self.src("h2.exits.ebr"))
        v._full_at = None
        v.sync()
        self.assertEqual(sorted(os.listdir(os.path.join(self.vroot, DAY))), [])
        self.assertEqual(v.status("ebpf")["counts"], {"converted": 0, "linked": 0, "held": 0})

    def test_a_bounded_view_drops_the_days_past_its_bound(self):
        # ingest.convert.days 3, the tail's 3 and windows of a day (1 + 2): 3 kept.
        # Unbounded (the default), every day stays (test_every_day_and_flat_file...).
        days = ["2026-11-%02d" % d for d in range(10, 16)]      # 6 days, 3 kept
        for d in days:
            self.append("h1.exits.jsonl", [rec(T0, 1)], day=d)
        self.RETENTION = 1440
        v = self.view(days=3)
        self.assertEqual(sorted(x for x in os.listdir(self.vroot) if x.startswith("20")),
                         days[-3:])
        # a new day: the oldest kept falls out
        self.append("h1.exits.jsonl", [rec(T0 + 86400, 1)], day="2026-11-16")
        v.sync()
        self.assertEqual(sorted(x for x in os.listdir(self.vroot) if x.startswith("20")),
                         days[-2:] + ["2026-11-16"])
        # a source day removed takes its view day with it
        shutil.rmtree(os.path.join(self.root, days[-1]))
        v._full_at = None
        v.sync()
        self.assertNotIn(days[-1], os.listdir(self.vroot))
        # but a day newer than the oldest kept, held or empty, is not pruned
        os.makedirs(os.path.join(self.root, "2026-11-17"))
        v._full_at = None
        v.sync()
        self.assertIn(days[-2], os.listdir(self.vroot))

    def test_flat_files_in_the_root(self):
        for d in range(10, 15):
            self.append("thing_2026-11-%02d.jsonl" % d, [rec(T0 + d, d)], day="")
        v = self.view()
        # every one: the scans read every flat file (feedfile.day_files), and the
        # tail its newest feeds.<tier>.days of the view's
        flat = sorted(n for n in os.listdir(self.vroot) if n.endswith(".ebr"))
        self.assertEqual(flat, ["thing_2026-11-%02d.ebr" % d for d in range(10, 15)])
        self.assertConverted("thing_2026-11-14.jsonl", day="")
        self.assertConverted("thing_2026-11-10.jsonl", day="")
        self.append("thing_2026-11-15.jsonl", [rec(T0 + 15, 15)], day="")
        v._full_at = None
        v.sync()
        flat = sorted(n for n in os.listdir(self.vroot) if n.endswith(".ebr"))
        self.assertEqual(flat, ["thing_2026-11-%02d.ebr" % d for d in range(10, 16)])

    def test_the_node_tier(self):
        p = os.path.join(self.node, DAY, "h1.jsonl")
        os.makedirs(os.path.dirname(p))
        snap = {"schema_version": 8, "ts": stamp(T0), "ts_epoch": T0, "host": "h1",
                "load": [0.5, 0.25], "users": {"alice": {"cpu_usage_usec": 123}}}
        with open(p, "w") as fh:
            for i in range(3):
                fh.write(json.dumps(dict(snap, ts_epoch=T0 + 60 * i)) + "\n")
        v = self.view(node=True)
        vroot = self.cfg_.get("feeds.ebpf_node.roots")[0]
        got = view_records(os.path.join(vroot, DAY, "h1.ebr"))
        self.assertEqual([r["ts_epoch"] for r in got], [T0, T0 + 60, T0 + 120])
        self.assertEqual(v.status("ebpf_node")["counts"]["converted"], 1)
        self.assertEqual(self.cfg_.get("feeds.ebpf_node.source_roots"), [self.node])

    def test_zstd_frames_when_asked_for_and_available(self):
        recs = [rec(T0 + i, i, args="x" * 200) for i in range(300)]
        self.append("h1.exits.jsonl", recs)
        if not getattr(ebr, "zstd_available", lambda: False)():
            v = self.view(zstd=3)
            self.assertIn("uncompressed", v.status("ebpf")["zstd_note"])
            self.assertConverted("h1.exits.jsonl")
            self.skipTest("this ebr.py writes no zstd frames")
        v = self.view(zstd=3)
        self.assertEqual(v.status("ebpf")["zstd"], 3)
        flags = [f.flags for f in ebr.frames(self.vpath("h1.exits.ebr")) if f.kind == ebr.DATA]
        self.assertTrue(any(flags))
        self.assertConverted("h1.exits.jsonl")

    def test_no_zstd_unless_asked(self):
        self.append("h1.exits.jsonl", [rec(T0 + i, i, args="x" * 200) for i in range(300)])
        v = self.view()
        self.assertEqual(v.status("ebpf")["zstd"], 0)
        self.assertFalse(any(f.flags for f in ebr.frames(self.vpath("h1.exits.ebr"))))


# ------------------------------------------------------------- setting it up
class SetupTest(ViewCase):

    def test_where_the_view_lives(self):
        self.append("h1.exits.jsonl", [rec(T0, 1)])
        v = self.view()
        base = os.path.join(self.tmp, "cache", "ebr-view", "ebpf")
        self.assertEqual(os.path.dirname(self.vroot), base)
        self.assertRegex(os.path.basename(self.vroot), r"^ebpf-[0-9a-f]{10}$")
        with open(os.path.join(self.vroot, ebrview.MARKER)) as fh:
            self.assertEqual(json.load(fh)["realpath"], os.path.realpath(self.root))
        self.assertEqual(self.cfg_.get("feeds.ebpf.prefer"), "ebr")
        self.assertEqual(self.cfg_.get("feeds.ebpf.source_roots"), [self.root])
        self.assertTrue(self.cfg_.origin("feeds.ebpf.roots").startswith("convert"))
        self.assertEqual(v.status("ebpf")["source_roots"], [self.root])
        # elsewhere when set
        d = os.path.join(self.tmp, "views")
        self.view(dir=d)
        self.assertTrue(self.vroot.startswith(d + os.sep))

    def test_the_caches_pruners_leave_the_view_alone(self):
        from rc_dashboard import hours, payloads, sacct, snapshots
        self.append("h1.exits.jsonl", [rec(T0 + i, i) for i in range(5)])
        self.view()
        before = sorted(os.path.join(dp, n) for dp, _d, ns in os.walk(self.vroot) for n in ns)
        cache = self.cfg_.get("cache.dir")
        snapshots.SnapshotStore(os.path.join(cache, "windows"), self.cfg_, keep=1).prune()
        payloads.PayloadStore(os.path.join(cache, "payloads"), self.cfg_, keep=1).prune()
        store = hours.HourStore(self.cfg_, ["agent"], dict, frozenset({"exit"}),
                                os.path.join(cache, "hours"))
        store.prune(now=T0 + 10 * 86400 * 365)
        sacct.prune_cache(cache, "0" * 12, keep=0)
        after = sorted(os.path.join(dp, n) for dp, _d, ns in os.walk(self.vroot) for n in ns)
        self.assertEqual(after, before)
        self.assertTrue(any(p.endswith("h1.exits.ebr") for p in after))

    def test_the_newest_file_of_a_tie_is_the_later_path(self):
        # Two hosts' files written in one round share an mtime tick on a coarse
        # clock: the feed report names the same one whichever twin it reads.
        from rc_dashboard import feeds
        for h in ("h1", "h2"):
            self.append("%s.exits.jsonl" % h, [rec(T0 + 60 * (h == "h1"), 1, host=h)])
        for h in ("h1", "h2"):
            os.utime(self.src("%s.exits.jsonl" % h), ns=(10 ** 18, 10 ** 18))
        rep = feeds.resolve_dated("ebpf", self.cfg(enabled=False))
        self.assertEqual(os.path.basename(rep["newest_file"]), "h2.exits.jsonl")

    def _modes(self, top):
        """{relative path: (kind, mode)} of everything under `top`, links as links."""
        out = {}
        for dp, dns, fns in os.walk(top):
            for n in dns + fns:
                p = os.path.join(dp, n)
                st = os.lstat(p)
                kind = "link" if os.path.islink(p) else ("dir" if os.path.isdir(p) else "file")
                out[os.path.relpath(p, top)] = (kind, st.st_mode & 0o777)
        return out

    def test_the_view_is_private_to_the_service(self):
        # The records hold every user's command lines, which the collector keeps
        # 0640: a copy readable by every local user would leak them.
        old = os.umask(0o022)
        try:
            self.append("h1.exits.jsonl", [rec(T0 + i, i) for i in range(5)])
            ebr_src = self.src("h2.exits.ebr")
            with ebr.Writer(ebr_src) as w:
                w.write(rec(T0, 9, host="h2"))
            os.chmod(ebr_src, 0o640)
            self.append("flat_2026-11-14.jsonl", [rec(T0, 7)], day="")
            self.view()
            base = os.path.join(self.tmp, "cache", "ebr-view")
            self.assertEqual(os.stat(base).st_mode & 0o777, 0o700)
            modes = self._modes(base)
            self.assertTrue(modes)
            for rel, (kind, mode) in modes.items():
                if kind == "dir":
                    self.assertEqual(mode, 0o700, rel)
                elif kind == "file":
                    self.assertEqual(mode, 0o600, rel)
            self.assertTrue(any(k == "link" for k, _m in modes.values()))
            self.assertTrue(any(r.endswith("h1.exits.ebr") for r in modes))
            # a link's target is the collector's file: its mode is left alone
            self.assertEqual(os.stat(ebr_src).st_mode & 0o777, 0o640)
        finally:
            os.umask(old)

    def test_an_older_views_modes_are_narrowed(self):
        old = os.umask(0o022)
        try:
            self.append("h1.exits.jsonl", [rec(T0 + i, i) for i in range(5)])
            ebr_src = self.src("h2.exits.ebr")
            with ebr.Writer(ebr_src) as w:
                w.write(rec(T0, 9, host="h2"))
            os.chmod(ebr_src, 0o644)
            v = self.view()
            base = os.path.join(self.tmp, "cache", "ebr-view")
            # as the version before this one left it: the umask's modes
            for rel, (kind, _m) in self._modes(base).items():
                p = os.path.join(base, rel)
                if kind == "dir":
                    os.chmod(p, 0o755)
                elif kind == "file":
                    os.chmod(p, 0o644)
            os.chmod(base, 0o755)
            self.restart(v)
            self.assertEqual(os.stat(base).st_mode & 0o777, 0o700)
            for rel, (kind, mode) in self._modes(base).items():
                if kind != "link":
                    self.assertEqual(mode, 0o700 if kind == "dir" else 0o600, rel)
            self.assertEqual(os.stat(ebr_src).st_mode & 0o777, 0o644)
            self.assertConverted("h1.exits.jsonl")
        finally:
            os.umask(old)

    def test_every_day_and_flat_file_is_mirrored(self):
        # The scans without a horizon (/api/events, /api/export, a window's plan)
        # read every dated dir and flat file a source holds (feedfile.day_files):
        # the view mirrors all of them, whatever feeds.<tier>.days says, and each
        # reader keeps its own limits on the view.
        from rc_dashboard import feedfile
        days = ("2026-11-11", "2026-11-12", "2026-11-13", DAY)
        for i, d in enumerate(days):
            self.append("h1.exits.jsonl", [rec(T0 - 86400 * (3 - i), i)], day=d)
        flats = ("cap_2026-11-11.jsonl", "cap_2026-11-12.jsonl", "cap_2026-11-13.jsonl")
        for i, f in enumerate(flats):
            self.append(f, [rec(T0 + i, 100 + i)], day="")
        self.DAYS = 1
        self.view()
        self.assertEqual(sorted(d for d in os.listdir(self.vroot) if d.startswith("20")),
                         list(days))
        for d in days:
            self.assertConverted("h1.exits.jsonl", day=d)
        for f in flats:
            self.assertConverted(f, day="")

        def streams(root, prefer):
            return sorted((os.path.relpath(os.path.dirname(p), root), ebrview.split_name(p)[0])
                          for p in feedfile.day_files([root], 0, prefer))
        self.assertEqual(streams(self.vroot, "ebr"), streams(self.root, "jsonl"))

    def test_a_bounded_view_keeps_what_the_bounded_readers_take(self):
        # ingest.convert.days bounds a long history, never below the tail's
        # feeds.<tier>.days or the widest window's days plus two.
        days = ("2026-11-10", "2026-11-11", "2026-11-12", "2026-11-13", DAY)
        for i, d in enumerate(days):
            self.append("h1.exits.jsonl", [rec(T0 - 86400 * (4 - i), i)], day=d)
        self.DAYS, self.RETENTION = 1, 60       # windows of an hour: 1 day + 2
        self.view(days=2)
        self.assertEqual(sorted(d for d in os.listdir(self.vroot) if d.startswith("20")),
                         list(days[-3:]))
        self.DAYS = 4
        self.view(dir=os.path.join(self.tmp, "four"), days=2)
        self.assertEqual(sorted(d for d in os.listdir(self.vroot) if d.startswith("20")),
                         list(days[-4:]))

    def test_an_older_days_source_is_read_on_the_full_pass(self):
        # Every poll looks at the newest two days; older ones, which a straggler
        # may still write to, at the full listing every DISCOVER_S.
        days = ("2026-11-12", "2026-11-13", DAY)
        for i, d in enumerate(days):
            self.append("h1.exits.jsonl", [rec(T0 - 86400 * (2 - i), i)], day=d)
        v = self.view()
        self.append("h1.exits.jsonl", [rec(T0 - 2 * 86400 + 5, 50)], day=days[0])
        v._full_at = time.monotonic()           # the next poll is not a full one
        v.sync()
        self.assertEqual(len(view_records(self.vpath("h1.exits.ebr", day=days[0]))), 1)
        v._full_at = None                       # and the next one is
        self.settle(v)
        self.assertConverted("h1.exits.jsonl", day=days[0])

    def test_a_view_inside_a_source_root_is_refused(self):
        cfg = self.cfg(dir=os.path.join(self.root, "view"))
        with self.assertRaises(ebrview.ConvertError):
            ebrview.EbrView.from_config(cfg)

    def test_a_view_root_of_another_source_is_refused(self):
        self.append("h1.exits.jsonl", [rec(T0, 1)])
        self.view()
        with open(os.path.join(self.vroot, ebrview.MARKER), "w") as fh:
            json.dump({"realpath": "/elsewhere"}, fh)
        with self.assertRaises(ebrview.ConvertError):
            self.view()

    def test_off(self):
        self.assertIsNone(ebrview.EbrView.from_config(self.cfg(enabled=False)))
        st = ebrview.status_off(self.cfg(enabled=False), "ebpf")
        self.assertEqual((st["enabled"], st["state"]), (False, "off"))

    def test_the_settings_and_their_env_vars(self):
        conf = os.path.join(self.tmp, "c.json")
        with open(conf, "w") as fh:
            fh.write("{}")
        env = {"RC_DASH_CONFIG": conf}
        with mock.patch.dict(os.environ, env, clear=False):
            for k in [k for k in os.environ if k.startswith("RC_DASH_CONVERT")]:
                del os.environ[k]
            cfg = configmod.load(None, None)
            self.assertEqual(cfg.get("ingest.convert"),
                             {"enabled": True, "dir": "", "zstd": 0, "workers": 0,
                              "days": 0})
            s = ebrview.settings(cfg)
            # every CPU this process may run on: its affinity, not the machine's count
            self.assertEqual(s["workers"], len(os.sched_getaffinity(0)))
            self.assertEqual(s["workers"], ebrview.usable_cpus())
            with mock.patch.object(os, "process_cpu_count", lambda: 3, create=True):
                self.assertEqual(ebrview.settings(cfg)["workers"], 3)
            self.assertEqual(s["dir"], os.path.join(cfg.get("cache.dir"), "ebr-view"))
            for off in ("0", "false", "no", "off", "OFF"):
                os.environ["RC_DASH_CONVERT"] = off
                self.assertFalse(configmod.load(None, None).get("ingest.convert.enabled"))
            os.environ["RC_DASH_CONVERT"] = "1"
            os.environ["RC_DASH_CONVERT_DIR"] = "/x/views"
            os.environ["RC_DASH_CONVERT_ZSTD"] = "3"
            os.environ["RC_DASH_CONVERT_WORKERS"] = "2"
            os.environ["RC_DASH_CONVERT_DAYS"] = "5"
            cfg = configmod.load(None, None)
            self.assertEqual(cfg.get("ingest.convert"),
                             {"enabled": True, "dir": "/x/views", "zstd": 3, "workers": 2,
                              "days": 5})
            self.assertEqual(cfg.origin("ingest.convert.zstd"), "env:RC_DASH_CONVERT_ZSTD")
            for var, bad in (("RC_DASH_CONVERT_ZSTD", "99"), ("RC_DASH_CONVERT_WORKERS", "-1"),
                             ("RC_DASH_CONVERT_DAYS", "-1")):
                os.environ[var] = bad
                with self.assertRaises(SystemExit):
                    configmod.load(None, None)
                os.environ[var] = "1"
            with open(conf, "w") as fh:
                json.dump({"ingest": {"convert": {"enabled": "maybe"}}}, fh)
            del os.environ["RC_DASH_CONVERT"]
            with self.assertRaises(SystemExit):
                configmod.load(None, None)

    def test_the_example_config_lists_it(self):
        with open(Path(__file__).resolve().parents[1] / "dashboard.config.example.json") as fh:
            ex = configmod.strip_comments(json.load(fh))
        self.assertEqual(ex["ingest"]["convert"], configmod.CONVERT)

    def test_check_feeds_says_what_it_would_do_and_does_nothing(self):
        self.append("h1.exits.jsonl", [rec(T0 + i, i) for i in range(5)])
        cfg = self.cfg()
        lines = ebrview.describe(cfg)
        self.assertEqual(len(lines), 1)
        self.assertIn("1 jsonl stream(s) read as EBR", lines[0])
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "cache")))
        self.view()
        self.assertIn("(1 up to date", ebrview.describe(self.cfg())[0])
        self.assertIn("off", ebrview.describe(self.cfg(enabled=False))[0])

    def test_the_catch_up_in_processes(self):
        for h in range(4):
            self.append("h%d.exits.jsonl" % h, [rec(T0 + i, i, host="h%d" % h)
                                                for i in range(200)])
        with mock.patch.object(ebrview, "POOL_MIN_BYTES", 0):
            v = self.view(workers=2)
        self.assertEqual(v.progress["workers"], 2)
        self.assertEqual(v.progress["files"], 4)
        self.assertEqual(v.progress["records"], 800)
        for h in range(4):
            self.assertConverted("h%d.exits.jsonl" % h)
        self.assertEqual(v.state, "live")

    def test_a_file_that_fails_is_reported_and_tried_again(self):
        self.append("h1.exits.jsonl", [rec(T0 + i, i) for i in range(5)])
        self.append("h2.exits.jsonl", [rec(T0 + i, i, host="h2") for i in range(5)])
        real = ebrview.convert

        def broken(view, src, *a, **kw):
            if "h1" in src:
                raise OSError(5, "disk on fire")
            return real(view, src, *a, **kw)
        with mock.patch.object(ebrview, "convert", broken):
            v = self.view()
        self.assertConverted("h2.exits.jsonl")
        st = v.status("ebpf")
        self.assertTrue(any("disk on fire" in x["error"] for x in st["errors"]))
        e = self.entry(v, "h1.exits.jsonl")
        self.assertIn("disk on fire", e.error)
        e.retry_at = 0
        self.settle(v)
        self.assertConverted("h1.exits.jsonl")
        self.assertIsNone(self.entry(v, "h1.exits.jsonl").error)


# ------------------------------------------------------------------- the app
def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class App(object):
    """The real app on a free port, over `cfg`."""

    def __init__(self, cfg, before_start=None):
        import uvicorn
        from rc_dashboard import app as appmod
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)    # on_event
            self.app = appmod.create_app(cfg)
        if before_start is not None:
            before_start(self.app)
        self.port = free_port()
        self.server = uvicorn.Server(uvicorn.Config(self.app, host="127.0.0.1",
                                                    port=self.port, log_level="error"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        deadline = time.time() + 30
        while not self.server.started and time.time() < deadline:
            time.sleep(0.02)

    def get(self, path):
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d%s" % (self.port, path),
                                        timeout=60) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def json(self, path):
        code, body = self.get(path)
        assert code == 200, (path, code, body[:300])
        return json.loads(body)

    def wait(self, pred, what, timeout=60):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if pred():
                return
            time.sleep(0.05)
        raise AssertionError("timed out waiting for %s" % what)

    def stop(self):
        self.server.should_exit = True
        self.thread.join(timeout=20)


class AppTest(unittest.TestCase):
    """A JSONL-only feed, served with conversion on and off."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rc_ebrview_app_")
        self.root = os.path.join(self.tmp, "ebpf")
        os.makedirs(os.path.join(self.root, DAY2))
        self.now = time.time()
        recs = []
        for i in range(120):
            t = self.now - 3 * 3600 + 80 * i
            recs.append(rec(t, 1000 + i, host="h%d" % (i % 2), user=("alice", "bob")[i % 3 % 2],
                            agent_pid=4242, session_key="apid:4242", depth=1))
        day = time.strftime("%Y-%m-%d", time.localtime(self.now))
        self.day = day
        os.makedirs(os.path.join(self.root, day), exist_ok=True)
        for h in ("h0", "h1"):
            with open(os.path.join(self.root, day, "%s.exits.jsonl" % h), "wb") as fh:
                for r in recs:
                    if r["host"] == h:
                        fh.write(line(r))
        self.apps = []

    def tearDown(self):
        for a in self.apps:
            a.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def cfg(self, name, enabled=True):
        tree = configmod.defaults()
        tree["feeds"]["ebpf"]["roots"] = [self.root]
        tree["feeds"]["ebpf_node"]["roots"] = [os.path.join(self.tmp, "no-node")]
        tree["cache"]["dir"] = os.path.join(self.tmp, "cache-" + name)
        tree["server"]["static_dir"] = os.path.join(self.tmp, "no-ui")
        tree["panels"]["prewarm_min"] = []
        tree["ask"]["enabled"] = False
        tree["server"]["rdns"]["enabled"] = False
        tree["ingest"]["poll_ms"] = 200
        tree["ingest"]["convert"]["enabled"] = enabled
        return configmod.Config(tree, {}, None)

    def start(self, name, enabled=True, before_start=None):
        a = App(self.cfg(name, enabled), before_start)
        self.apps.append(a)
        return a

    def ready(self, a):
        a.wait(lambda: (a.json("/api/health").get("backfill") or {}).get("state")
               in ("done", "skipped"), "the backfill")

    def test_the_view_is_read_and_answers_as_the_jsonl(self):
        on, off = self.start("on"), self.start("off", enabled=False)
        for a in (on, off):
            self.ready(a)
        feeds = on.json("/api/feeds")["ebpf"]
        c = feeds["convert"]
        self.assertEqual((c["enabled"], c["state"], c["counts"]["converted"]), (True, "live", 2))
        self.assertEqual(c["records"], 120)
        self.assertEqual(c["source_roots"], [self.root])
        self.assertEqual(feeds["formats"]["read"], {"ebr": 2})
        self.assertEqual({f["format"] for f in feeds["files"]}, {"ebr"})
        self.assertEqual(feeds["source_roots"], [self.root])
        self.assertTrue(all(f["path"].startswith(c["view_roots"][0]) for f in feeds["files"]))
        offc = off.json("/api/feeds")["ebpf"]["convert"]
        self.assertEqual((offc["enabled"], offc["state"]), (False, "off"))
        self.assertEqual(off.json("/api/feeds")["ebpf"]["formats"]["read"], {"jsonl": 2})
        for path in ("/api/events?limit=500", "/api/events?limit=50&order=asc",
                     "/api/events?limit=20&user=bob"):
            a, b = on.json(path), off.json(path)
            self.assertEqual(a["rows"], b["rows"], path)
            if "user=" not in path:     # a filtered read counts what its prefilter passes
                self.assertEqual(a["total_scanned"], b["total_scanned"], path)
        self.assertEqual(len(on.json("/api/events?limit=500")["rows"]), 120)
        ea = on.get("/api/export?order=asc")[1].splitlines()[1:]
        eb = off.get("/api/export?order=asc")[1].splitlines()[1:]
        self.assertEqual(ea[:-1], eb[:-1])
        self.assertEqual(len(ea), 121)
        self.assertEqual(on.json("/api/health")["records"], off.json("/api/health")["records"])
        # and what the collector appends is read through the view, poll by poll
        n0 = on.json("/api/health")["records"]
        r = rec(time.time(), 77777, host="h0")
        with open(os.path.join(self.root, self.day, "h0.exits.jsonl"), "ab") as fh:
            fh.write(line(r))
        on.wait(lambda: any(x["pid"] == 77777 for x in on.json(
            "/api/events?limit=5")["rows"]), "the appended record")
        on.wait(lambda: on.json("/api/health")["records"] == n0 + 1, "the tail to read it")

    def test_what_reads_the_day_files_answers_503_while_converting(self):
        gate, entered = threading.Event(), threading.Event()

        def hold(app):
            def hook(_view):
                entered.set()
                gate.wait(60)
            app.state.agg.convert.hooks["catch_up"] = hook
        a = self.start("gated", before_start=hold)
        self.assertTrue(entered.wait(30))
        try:
            for path in ("/api/events", "/api/export", "/api/trajectory?sid=x",
                         "/api/io/commands?window_min=60"):
                code, body = a.get(path)
                self.assertEqual(code, 503, path)
                self.assertEqual(json.loads(body)["error"], "converting", path)
            self.assertEqual(a.json("/api/feeds")["ebpf"]["convert"]["state"], "catching_up")
            self.assertEqual(a.json("/api/health")["backfill"]["state"], "converting")
            self.assertEqual(a.get("/api/live")[0], 200)
            self.assertEqual(a.get("/api/panels")[0], 200)
        finally:
            gate.set()
        self.ready(a)
        self.assertEqual(a.get("/api/events")[0], 200)
        self.assertEqual(a.json("/api/feeds")["ebpf"]["convert"]["state"], "live")


if __name__ == "__main__":
    unittest.main()
