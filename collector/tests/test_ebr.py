"""lib/ebr.py: EBR, the collector's binary record format.

The oracle is the JSON round trip, compared type-exactly: whatever the writer
encodes must decode to json.loads(json.dumps(record)), with the same types
(bool / int / float), the same key order, null vs absent, and fresh objects.
Runs anywhere: no root, no bcc.

    python3 -m unittest discover -s collector/tests -p test_ebr.py
    cd collector && python3 -m unittest tests.test_ebr
"""
import ast
import collections
import enum
import errno
import gzip
import io
import json
import math
import os
import random
import re
import struct
import subprocess
import sys
import shutil
import tempfile
import time
import tokenize
import unittest
import zlib
from pathlib import Path
from unittest import mock

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / 'lib'))

import ebr as E                  # noqa: E402

EBR_PY = str(_ROOT / 'lib' / 'ebr.py')
INF = float('inf')
NAN = float('nan')

try:
    import fcntl
except ImportError:              # pragma: no cover
    fcntl = None


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def read_file(path):
    with open(path, 'rb') as fh:
        return fh.read()


def oracle(rec):
    return json.loads(json.dumps(rec))


def deq(a, b):
    """Type-exact equality, written independently of ebr._same: key order
    counts, NaN equals NaN, -0.0 is not 0.0, 1 is not 1.0 is not True."""
    if type(a) is not type(b):
        return False
    if isinstance(a, dict):
        return list(a) == list(b) and all(deq(a[k], b[k]) for k in a)
    if isinstance(a, list):
        return len(a) == len(b) and all(deq(x, y) for x, y in zip(a, b))
    if isinstance(a, float):
        if a != a:
            return b != b
        return a == b and math.copysign(1.0, a) == math.copysign(1.0, b)
    return a == b


def write_bytes(recs, flush_every=0, **kw):
    bio = io.BytesIO()
    w = E.Writer(bio, **kw)
    for i, r in enumerate(recs):
        w.write(r)
        if flush_every and (i + 1) % flush_every == 0:
            w.flush()
    w.close()
    return bio.getvalue()


def decode(data, kinds=None, where=None, strict=False):
    """(list of (pos, rec), decoder) for every frame of `data`."""
    dec = E.Decoder(strict)
    out = []
    for f in E.frames(data):
        out.extend(dec.records(data, f, kinds, where))
    return out, dec


def records_of(data):
    return [r for _p, r in decode(data)[0]]


def frame_list(data, **kw):
    return [(f.off, f.end, f.kind) for f in E.frames(data, **kw)]


def in_kinds(v, kinds):
    try:
        return v in kinds
    except TypeError:
        return False


def reference_filter(pairs, kinds, where):
    return [(p, r) for p, r in pairs
            if (kinds is None or in_kinds(r.get('event'), kinds))
            and E.match_where(where, r)]


def rewrite_header(data, off, **fields):
    """`data` with the header at `off` changed and its CRC recomputed."""
    t = list(E.HDR.unpack_from(data, off))
    names = ['magic', 'kind', 'flags', 'reserved', 'payload_len', 'n_records', 'ts_min',
             'ts_max', 'seg_back', 'kinds', 'crc']
    for k, v in fields.items():
        t[names.index(k)] = v
    h = struct.pack('<4sBBHIIddII', *t[:10])
    h += struct.pack('<I', zlib.crc32(h))
    return data[:off] + h + data[off + 44:]


def rewrite_payload(data, off, payload):
    """`data` with the frame at `off` given a new payload (CRCs, lengths fixed)."""
    t = list(E.HDR.unpack_from(data, off))
    end = off + 52 + t[4]
    t[4] = len(payload)
    h = struct.pack('<4sBBHIIddII', *t[:10])
    h += struct.pack('<I', zlib.crc32(h))
    frame = h + payload + struct.pack('<II', zlib.crc32(payload), 52 + len(payload))
    return data[:off] + frame + data[end:]


def payload_of(data, off):
    L = E.HDR.unpack_from(data, off)[4]
    return data[off + 44:off + 44 + L]


def data_frames(data):
    return [f for f in E.frames(data) if f.kind == E.DATA]


def segment_of(data, n=0):
    """Decoder.segment after the `n`th SEGMENT of `data`."""
    dec = E.Decoder()
    segs = [f for f in E.frames(data) if f.kind == E.SEGMENT]
    dec.skip(segs[n], data)
    return dec.segment


def frame_bytes(data, f):
    return data[f.off:f.end]


def run_cli(*args, **kw):
    """ebr.py's command line in a child; zstd=False runs it as if this Python
    had no zstd module."""
    inp = kw.pop('input', None)
    cmd = [sys.executable, EBR_PY]
    if kw.pop('zstd', None) is False:
        cmd = [sys.executable, '-c', 'import sys; sys.path.insert(0, sys.argv.pop(1)); '
               'import ebr; ebr._ZSTD = False; sys.exit(ebr.main())', str(_ROOT / 'lib')]
    p = subprocess.run(cmd + [str(a) for a in args],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, input=inp,
                       cwd=kw.pop('cwd', None))
    return p.returncode, p.stdout, p.stderr


DATA = _ROOT / 'tests' / 'data'
HAS_ZSTD = E.zstd_available()
NEEDS_ZSTD = unittest.skipUnless(HAS_ZSTD, 'no zstd module in this Python '
                                 '(Python 3.14+, zstandard or pyzstd)')


def zstd_backends():
    """(name, backend) of every zstd module this Python can import."""
    out = []
    for name in E._ZSTD_ORDER:
        z = E._zstd_load((name,))
        if z:
            out.append((name, z))
    return out


def no_zstd():
    """As if this Python had no zstd module."""
    return mock.patch.object(E, '_ZSTD', False)


def zstd_frame(data, single=True, fcs_flag=None, checksum=False, rle=False, block=1 << 17):
    """A zstd frame of `data` built by hand (RFC 8878): raw blocks (one RLE
    block for `rle`), so no zstd module is needed to make one. A checksum is
    flagged and written as 4 zero bytes, which only a frame walk accepts."""
    n = len(data)
    if fcs_flag is None:
        fcs_flag = 0 if (single and n < 256) else (1 if 256 <= n < 65792 else 2)
    d = fcs_flag << 6 | (0x20 if single else 0) | (0x04 if checksum else 0)
    out = bytearray(b'\x28\xb5\x2f\xfd') + bytes((d,))
    if not single:
        out.append(7 << 3)                          # a 128 KiB window
    if fcs_flag == 0 and single:
        out += bytes((n,))
    elif fcs_flag == 1:
        out += struct.pack('<H', n - 256)
    elif fcs_flag == 2:
        out += struct.pack('<I', n)
    elif fcs_flag == 3:
        out += struct.pack('<Q', n)
    if rle:
        assert data == data[:1] * n
        out += struct.pack('<I', n << 3 | 1 << 1 | 1)[:3] + data[:1]
    else:
        parts = [data[i:i + block] for i in range(0, n, block)] or [b'']
        for i, part in enumerate(parts):
            out += struct.pack('<I', len(part) << 3 | (i == len(parts) - 1))[:3] + part
    if checksum:
        out += b'\x00' * 4
    return bytes(out)


def seg_v2(version=2, sis=2, created=1.5, reset_bytes=E.DEFAULT_RESET_BYTES, kinds=E.KINDS,
           meta=None, tail=b''):
    """A SEGMENT payload laid out by the v2 spec, built here."""
    out = struct.pack('<BBd', version, sis, created) + E._uv(reset_bytes) + E._uv(len(kinds))
    for k in kinds:
        b = k.encode('utf-8', 'surrogatepass')
        out += E._uv(len(b)) + b
    return out + bytes(E._enc_map(meta or {})) + tail


def zstd_payload(raw, frame=None):
    """An F_ZSTD payload: raw_len, then the frame (default: built by hand)."""
    return E._uv(len(raw)) + (zstd_frame(raw) if frame is None else frame)


class Color(enum.IntEnum):
    RED = 3
    BIG = 2 ** 70


class MyFloat(float):
    pass


class MyStr(str):
    def __str__(self):
        return 'not me'


class MyList(list):
    pass


# A node-tier record, keys in node_snapshot.py's order (abridged).
NODE = json.loads(
    '{"timestamp": "2026-11-10 00:00:17", "hostname": "h1", "schema_version": 1, '
    '"collector": {"uid": 0, "privileged": true, "args_maxlen": 256, "mode": "node"}, '
    '"uptime": {"users": 0, "load_1": 0.68, "load_5": 0.63, "load_15": 0.6}, '
    '"logged_in_users": [], "process_states": {"running": 1, "blocked_io": 0, '
    '"sleeping": 700, "zombie": 0, "stopped": 0}, "memory": {"mem": {"total_gb": 256.0, '
    '"used_gb": 16.6}, "swap": {"total_gb": 4.3, "used_gb": 0.0}}, "cpu": [{"cpu": "all", '
    '"usr": 0.88, "idle": 98.51}], "nfs_io": [{"mountpoint": "/home", "ops_s": 5.0, '
    '"rd_retrans": 0}, {"mountpoint": "/scratch", "ops_s": 5.0, "rd_retrans": 0}], '
    '"d_state_procs": [], "system_fds": {"allocated": 18000, "free": 0, '
    '"max": 9223372036854775807}, "ts_epoch": 1794268817.0, "tz": "UTC+0000", '
    '"redact": ["keys", "prompts"], "synthetic": true}')


def exit_rec(i, **kw):
    r = {'ts': '2026-11-10 00:00:%02d' % (i % 60), 'ts_epoch': 1794268800.0 + i * 0.37,
         'host': 'demo-login01', 'event': 'exit', 'collector_pid': 7362,
         'schema_version': 8, 'pid': 740000 + i, 'ppid': 740006, 'user': 'u%d' % (i % 5),
         'tty': None, 'command': ['git', 'python3', 'bash'][i % 3],
         'args': 'git status -z %d' % i, 'cwd': '/home/u%d/src' % (i % 5),
         'is_agent': bool(i % 2), 'cpu_s': 0.0168 * i, 'exit_code': i % 3, 'signal': None,
         'io': {'rd_mb': 10.385, 'wr_mb': 0.5 * i, 'scope': 'process'},
         'conns': None, 'net_endpoints': [{'ep': '1.2.3.4:443', 'tx': i}] if i % 4 == 0 else None}
    r.update(kw)
    return r


def mixed_records(n, seed=1):
    """Records of several kinds, as both streams and the node tier write them."""
    rng = random.Random(seed)
    out = []
    for i in range(n):
        k = rng.random()
        if k < 0.5:
            out.append(exit_rec(i))
        elif k < 0.7:
            out.append({'ts_epoch': 1794268800.0 + i, 'event': 'residency', 'pid': i,
                        'by_command': dict(('cmd%d' % rng.randrange(30), rng.randrange(9))
                                           for _ in range(rng.randrange(4))),
                        'state_counts': {'S': rng.randrange(5)}, 'agent_types': ['vscode']})
        elif k < 0.8:
            out.append({'ts_epoch': 1794268800.0 + i, 'event': 'conn', 'proto': 'tcp',
                        'rtt_ms': rng.random() * 10, 'bytes': rng.randrange(1 << 40)})
        elif k < 0.9:
            r = dict(NODE)
            r['ts_epoch'] = 1794268800.0 + i
            out.append(r)
        else:
            out.append({'ts_epoch': 1794268800.0 + i, 'event': rng.choice(['meta', 'stop', None, 'odd']),
                        'note': 'n%d' % i})
    return out


# ---------------------------------------------------------------------------
# the JSON equivalence
# ---------------------------------------------------------------------------

class RoundTripTest(unittest.TestCase):
    def check(self, recs, **kw):
        data = write_bytes(recs, **kw)
        got = records_of(data)
        self.assertEqual(len(got), len(recs))
        for r, g in zip(recs, got):
            want = oracle(r)
            self.assertTrue(deq(g, want), '%r\n!=\n%r' % (g, want))
        return got

    def test_ints_of_every_width(self):
        vals = [0, 1, -1, 127, 128, -128, -129, 255, 32767, 32768, -32768, -32769,
                2 ** 31 - 1, 2 ** 31, -2 ** 31, -2 ** 31 - 1, 2 ** 53 + 1, 2 ** 63 - 1, 2 ** 63,
                -2 ** 63, -2 ** 63 - 1, 2 ** 64 - 1, 2 ** 64, -2 ** 64,
                (1 << 96) + 12345, -(1 << 96) - 7, 10 ** 40, -10 ** 100]
        # each value in a fixed slot, as the first value of a slot, in a list and in a MAP
        recs = [{'v': v} for v in vals] + [{'v': v} for v in reversed(vals)]
        recs.append({'l': vals})
        recs.append({'m': dict(('k%d' % i, v) for i, v in enumerate(vals))})
        self.check(recs)

    def test_job_ids_and_u64s(self):
        job = (2 ** 96) + 8026641               # a 97-bit job id
        self.check([{'job_id': job, 'cgroup_id': 2 ** 64 - 1, 'ino': 2 ** 63 + 5},
                    {'job_id': 7, 'cgroup_id': 1, 'ino': 0}])

    def test_floats_bit_exact(self):
        vals = [0.0, -0.0, 1.5, 5e-324, -5e-324, 1e308, 2.2250738585072014e-308, 0.1 + 0.2,
                INF, -INF, NAN, 1794268817.145135]
        got = self.check([{'f': v} for v in vals] + [{'l': vals}, {'m': {'a': -0.0, 'b': NAN}}])
        self.assertEqual(math.copysign(1.0, got[1]['f']), -1.0)
        self.assertTrue(math.isnan(got[10]['f']))

    def test_one_vs_one_point_oh_vs_true(self):
        recs = [{'x': 1}, {'x': 1.0}, {'x': True}, {'x': 0}, {'x': False}, {'x': 0.0},
                {'x': [1, 1.0, True, False, 0, 0.0, None]}]
        got = self.check(recs)
        self.assertEqual([type(g['x']) for g in got[:6]], [int, float, bool, int, bool, float])

    def test_null_vs_absent(self):
        got = self.check([{'a': None, 'b': 1}, {'b': 1}, {'b': 1, 'a': None}, {}, {'a': None}])
        self.assertEqual([list(g) for g in got], [['a', 'b'], ['b'], ['b', 'a'], [], ['a']])

    def test_strings(self):
        vals = ['', 'a', 'é', '中文', '😀', 'tab\there', 'nul\x00byte', '"quotes"', '\\',
                ' ', 'x' * 70000]
        self.check([{'s': v} for v in vals] + [{'l': vals}, {'m': dict((v, v) for v in vals)}])

    def test_lone_and_split_surrogates(self):
        vals = ['\ud800', '\udfff', 'a\ud800b', '😀', 'x😀y', '\ude00\ud83d',
                '\ud83d', '\ud83d😀']
        recs = [{'s': v} for v in vals]
        recs.append({'l': vals})
        recs.append({'\ud800key': 1, 'k😀': 2})
        got = self.check(recs)
        self.assertEqual(got[3]['s'], '\U0001F600')      # the split pair is merged, as JSON does
        self.assertEqual(got[0]['s'], '\ud800')          # a lone surrogate survives
        for g in got:
            for v in g.values():
                if isinstance(v, str):
                    self.assertIs(type(v), str)

    def test_non_str_keys(self):
        recs = [{1: 'a', 2.5: 'b', True: 'c', None: 'd', False: 'e'},
                {NAN: 1, INF: 2, -INF: 3, 10 ** 30: 4, -0.0: 5, 1e100: 6},
                {Color.RED: 'enum', MyFloat(1.5): 'float'},
                {'outer': {1: {2: 3}}}, {'l': [{1: 2}]}]
        self.check(recs)

    def test_keys_that_collide_after_conversion(self):
        # json.loads keeps the first position and the last value
        recs = [{1: 'a', '1': 'b'}, {'1': 'a', 1: 'b', 'z': 0}, {'x': {1.0: 'a', '1.0': 'b'}},
                {True: 1, 'true': 2}, {None: 1, 'null': 2, 'a': 3}]
        got = self.check(recs)
        self.assertEqual(got[0], {'1': 'b'})
        self.assertEqual(list(got[1]), ['1', 'z'])

    def test_bad_keys_raise_type_error(self):
        w = E.Writer(io.BytesIO())
        with self.assertRaises(TypeError) as cm:
            w.write({(1, 2): 3})
        self.assertIn('keys must be str, int, float, bool or None', str(cm.exception))
        with self.assertRaises(TypeError):
            w.write({'ok': {frozenset(): 1}})
        w.close()

    def test_subclasses_are_stored_as_plain_values(self):
        od = collections.OrderedDict([('a', 1), ('b', 2), ('c', 3)])
        od.move_to_end('a')
        dd = collections.defaultdict(list)
        dd['x'].append(1)
        recs = [{'e': Color.RED, 'big': Color.BIG, 'f': MyFloat(2.5), 's': MyStr('plain')},
                {'od': od, 'cnt': collections.Counter('abca'), 'dd': dd},
                {'t': (1, 'two', (3.0, None)), 'ml': MyList([1, 2])},
                {MyStr('k'): MyStr('v')}, od]
        got = self.check(recs)
        self.assertIs(type(got[0]['e']), int)
        self.assertIs(type(got[0]['f']), float)
        self.assertEqual(got[0]['s'], 'plain')
        self.assertEqual(list(got[1]['od']), ['b', 'c', 'a'])
        self.assertIs(type(got[2]['t']), list)

    def test_empty_and_deep_containers(self):
        deep_d = {}
        x = deep_d
        for i in range(60):
            x['n'] = {'i': i}
            x = x['n']
        deep_l = []
        y = deep_l
        for _ in range(60):
            y.append([])
            y = y[0]
        self.check([{}, {'a': {}}, {'a': []}, {'a': [[]]}, {'a': [{}]}, {'d': deep_d},
                    {'l': deep_l}, {'mix': [{'a': [{'b': [1, {'c': None}]}]}]}])

    def test_fresh_objects_per_record(self):
        data = write_bytes([{'a': [1], 'b': {'c': 1}}] * 3)
        got = records_of(data)
        got[0]['a'].append(2)
        got[0]['b']['d'] = 1
        self.assertEqual(got[1], {'a': [1], 'b': {'c': 1}})
        self.assertIsNot(got[1]['a'], got[2]['a'])

    def test_node_snapshot_key_order(self):
        got = self.check([NODE, NODE])
        self.assertEqual(list(got[0]), list(NODE))
        self.assertEqual(list(got[1]['memory']['mem']), list(NODE['memory']['mem']))
        self.assertEqual(got[1]['system_fds']['max'], 2 ** 63 - 1)

    def test_unserializable_then_a_clean_record(self):
        bio = io.BytesIO()
        w = E.Writer(bio)
        w.write({'ok': 1})
        for bad in ({'a': object()}, {'a': [1, 2, {3, 4}]}, {'a': b'bytes'}, {'a': 1j},
                    {'x': {'y': [object()]}}):
            with self.assertRaises(TypeError) as cm:
                w.write(bad)
            self.assertIn('is not JSON serializable', str(cm.exception))
        circ = []
        circ.append(circ)
        with self.assertRaises(ValueError) as cm:
            w.write({'c': circ})
        self.assertIn('Circular reference detected', str(cm.exception))
        d = {}
        d['self'] = d
        with self.assertRaises(ValueError):
            w.write(d)
        with self.assertRaises(TypeError):
            w.write(['not', 'a', 'dict'])
        w.write({'ok': 2, 'new': 'string'})
        w.close()
        self.assertEqual(records_of(bio.getvalue()), [{'ok': 1}, {'ok': 2, 'new': 'string'}])
        self.assertEqual(w.stats['encode_errors'], 7)
        self.assertEqual(w.stats['records'], 2)


class JsonFuzzTest(unittest.TestCase):
    """5000 seeded random records round-trip exactly, across segments."""

    KEYS = ['a', 'b', 'event', 'ts_epoch', 'user', 'n', 'x', 'by_command', '', 'ü', 'k k']

    def value(self, rng, depth):
        k = rng.random()
        if depth > 3:
            k = k * 0.6
        if k < 0.08:
            return None
        if k < 0.16:
            return rng.random() < 0.5
        if k < 0.30:
            return rng.choice([0, 1, -1, 127, 128, 70000, -70000, 2 ** 40, 2 ** 63 - 1,
                               2 ** 63, -2 ** 63, 2 ** 64 + 3, rng.randrange(-10 ** 6, 10 ** 6)])
        if k < 0.40:
            return rng.choice([0.0, -0.0, 1.5, rng.random() * 1e9, 5e-324, INF, -INF, NAN])
        if k < 0.55:
            return rng.choice(['', 'x', 'é', '\ud800', '😀', 'str%d' % rng.randrange(500)])
        if k < 0.70:
            return [self.value(rng, depth + 1) for _ in range(rng.randrange(5))]
        if k < 0.75:
            return tuple(self.value(rng, depth + 1) for _ in range(rng.randrange(3)))
        d = {}
        for _ in range(rng.randrange(6)):
            if rng.random() < 0.05:
                key = rng.choice([1, 2.5, True, None, -3])
            elif rng.random() < 0.3:
                key = 'dyn%d' % rng.randrange(40)
            else:
                key = rng.choice(self.KEYS)
            d[key] = self.value(rng, depth + 1)
        return d

    def test_fuzz(self):
        rng = random.Random(20261116)
        recs = []
        for _ in range(5000):
            r = {}
            for _ in range(rng.randrange(1, 9)):
                r[rng.choice(self.KEYS + ['f%d' % rng.randrange(12)])] = self.value(rng, 0)
            recs.append(r)
        data = write_bytes(recs, flush_every=7, reset_bytes=32 << 10)
        got, dec = decode(data)
        self.assertEqual(len(got), len(recs))
        bad = [i for i, (r, (_p, g)) in enumerate(zip(recs, got)) if not deq(g, oracle(r))]
        self.assertEqual(bad, [])
        self.assertGreater(sum(1 for f in E.frames(data) if f.kind == E.SEGMENT), 3)
        self.assertEqual(dec.stats['bad_records'] + dec.stats['bad_frames'], 0)


class WideningAndShapesTest(unittest.TestCase):
    def shapes_of(self, data):
        dec = E.Decoder()
        for f in E.frames(data):
            for _ in dec.records(data, f):
                pass
        return [(tuple(dec._T.S[i] for i in sh.ids), sh.layout) for sh in dec._T.shapes]

    def test_int_widths_grow_and_never_narrow(self):
        vals = [1, 300, 70000, 2 ** 40, 2 ** 70, 5, -1, -2 ** 70]
        recs = [{'v': v} for v in vals]
        data = write_bytes(recs)
        self.assertEqual([r['v'] for r in records_of(data)], vals)
        layouts = [lay[0] for keys, lay in self.shapes_of(data) if keys == ('v',)]
        self.assertEqual(layouts, [E.T_I8, E.T_I16, E.T_I32, E.T_I64, E.T_BIG])

    def test_negative_widening(self):
        vals = [-1, -200, -40000, -2 ** 33, -2 ** 63, -2 ** 63 - 1]
        self.assertEqual([r['v'] for r in records_of(write_bytes([{'v': v} for v in vals]))], vals)

    def test_widening_in_nested_dicts_and_lists(self):
        recs = [{'io': {'n': 1}, 'l': [{'n': 1}]}, {'io': {'n': 2 ** 40}, 'l': [{'n': 2 ** 70}]},
                {'io': {'n': 3}, 'l': [{'n': 3}, {'n': -2 ** 63}]}]
        got = records_of(write_bytes(recs))
        self.assertTrue(all(deq(g, oracle(r)) for g, r in zip(got, recs)))

    def test_a_new_type_pattern_is_a_new_shape(self):
        recs = [{'a': 1, 'b': None}, {'a': None, 'b': 1}, {'a': 1, 'b': None}, {'a': 'x', 'b': None},
                {'a': 1.5, 'b': None}, {'a': True, 'b': None}]
        data = write_bytes(recs)
        self.assertEqual(len(self.shapes_of(data)), 5)
        self.assertTrue(all(deq(g, r) for g, r in zip(records_of(data), recs)))

    def test_a_shape_is_defined_once_per_segment(self):
        data = write_bytes([exit_rec(i) for i in range(50)], flush_every=5)
        shapes = self.shapes_of(data)
        self.assertEqual(len(shapes), len(set(shapes)))
        self.assertEqual(len([s for s in shapes if 'pid' in s[0]]), 2)   # net_endpoints: list or null


class BindingTest(unittest.TestCase):
    """CPython 3.12 guards a specialized call by the callee's func_version alone. A
    `def` takes its code object's version; a function made by types.FunctionType
    takes one from a separate counter that starts at 1. Binding thousands of
    decoders that way walked that counter onto the version of a def-made closure
    with no defaults (mapd, wide, ... in _bind), and a call site specialized on the
    decoder, handed the closure, read its NULL defaults: SIGSEGV, now and then, in
    the dashboard's reader and in its live service (FAS feed, 2026-11-16). Every
    decoder and encoder is made by a `def` inside a factory instead."""

    def test_every_bound_decoder_is_made_by_a_def(self):
        recs = [{'io': {'n': 1}, 'l': [{'n': 1}]}, {'a': 'x', 'b': None}] + \
            [exit_rec(i) for i in range(5)]
        data = write_bytes(recs)
        dec = E.Decoder()
        for f in E.frames(data):
            for _ in dec.records(data, f):
                pass
        fns = [sh.fn for sh in dec._T.shapes if sh.fn is not None]
        self.assertTrue(fns)
        self.assertEqual({f.__qualname__ for f in fns}, {'make.<locals>.dec'})

    def test_the_cached_templates_are_factories(self):
        write_bytes([{'io': {'n': 1}, 'l': [{'n': 1}]}, exit_rec(1)])
        self.assertTrue(E._ENC_CACHE and E._DEC_CACHE)
        self.assertEqual({f.__name__ for f in E._ENC_CACHE.values()}, {'make'})
        self.assertEqual({f.__name__ for f in E._DEC_CACHE.values()}, {'make'})


class DynamicKeysTest(unittest.TestCase):
    def test_map_after_the_eighth_key_set(self):
        recs = [{'event': 'residency', 'by_command': {'cmd%d' % i: i}} for i in range(20)]
        data = write_bytes(recs)
        self.assertEqual(records_of(data), recs)
        dec = decode(data)[1]
        nested = [sh for sh in dec._T.shapes if len(sh.ids) == 1]
        self.assertEqual(len(nested), 8)            # the 9th key set on is MAP
        body = payload_of(data, data_frames(data)[0].off)
        self.assertIn(b'\x00\x01', body)            # a MAP of one entry

    def test_contexts_are_separate(self):
        recs = []
        for i in range(10):
            recs.append({'a': {'k%d' % i: 1}, 'b': {'k%d' % i: 1}})
        data = write_bytes(recs)
        self.assertEqual(records_of(data), recs)

    def test_map_values_and_nested_dicts(self):
        recs = [{'by_actor3': {'human-%d' % i: {'n_procs': i, 'states': {'S': i, 'R': 1}}}}
                for i in range(15)]
        got = records_of(write_bytes(recs))
        self.assertEqual(got, recs)

    def test_wide_dicts(self):
        r300 = dict(('f%03d' % i, i) for i in range(300))
        r300s = dict(('s%03d' % i, 'v%d' % i) for i in range(300))
        r4097 = dict(('g%04d' % i, i % 7) for i in range(4097))
        recs = [r300, r300s, {'nested': r300}, {'nested': r300s}, r4097, {'nested': r4097},
                dict(r300, extra=None)]
        data = write_bytes(recs)
        got = records_of(data)
        self.assertTrue(all(deq(g, r) for g, r in zip(got, recs)))
        dec = decode(data)[1]
        self.assertTrue(all(len(sh.layout) <= E.MAX_FIELDS for sh in dec._T.shapes))
        self.assertTrue(any(len(sh.layout) == 300 for sh in dec._T.shapes))


# ---------------------------------------------------------------------------
# frames
# ---------------------------------------------------------------------------

class FrameLayoutTest(unittest.TestCase):
    def test_header_and_trailer_sizes(self):
        self.assertEqual(E.HDR.size, 44)
        self.assertEqual(E.TRL.size, 8)
        self.assertEqual(E.MAGIC, b'\x89EBR')

    def test_byte_layout(self):
        recs = [{'ts_epoch': 10.5, 'event': 'exit', 'a': 1},
                {'ts_epoch': 3.25, 'event': 'conn'}, {'ts_epoch': 7.0, 'event': None}]
        data = write_bytes(recs, meta={'host': 'h1', 'stream': 'exits', 'version': 'ignored'})
        magic, kind, flags, res, L, n, tmin, tmax, sb, kinds, hcrc = E.HDR.unpack_from(data, 0)
        self.assertEqual((magic, kind, flags, res, n, sb, kinds),
                         (E.MAGIC, E.SEGMENT, 0, 0, 0, 0, 0))
        self.assertTrue(math.isnan(tmin) and math.isnan(tmax))
        self.assertEqual(hcrc, zlib.crc32(data[:40]))
        payload = data[44:44 + L]
        pcrc, flen = E.TRL.unpack_from(data, 44 + L)
        self.assertEqual((pcrc, flen), (zlib.crc32(payload), 52 + L))
        # v2: version u8, str_id_size u8, created f64, reset_bytes uv, kinds, meta map
        version, sis, created = struct.unpack_from('<BBd', payload, 0)
        self.assertEqual((version, sis), (2, 2))
        self.assertLess(abs(created - time.time()), 60)
        reset, p = E._ruv(payload, 10)
        self.assertEqual(reset, E.DEFAULT_RESET_BYTES)
        n, p = E._ruv(payload, p)
        kinds = []
        for _ in range(n):
            k, p = E._ruv(payload, p)
            kinds.append(payload[p:p + k].decode())
            p += k
        self.assertEqual(kinds, list(E.KINDS))
        meta, p = E._dec_map(payload, p)
        self.assertEqual((meta, p), ({'host': 'h1', 'stream': 'exits'}, L))   # 'version' is the segment's
        self.assertNotIn(b'{', payload)                                     # no JSON
        seg = segment_of(data)
        self.assertEqual(list(seg), ['format', 'version', 'str_id_size', 'kinds', 'reset_bytes',
                                     'created', 'host', 'stream'])
        self.assertEqual((seg['format'], seg['version'], seg['str_id_size'], seg['host'],
                          seg['created']), ('ebr', 2, 2, 'h1', created))
        off = 52 + L
        magic, kind, flags, res, L2, n, tmin, tmax, sb, kinds, hcrc = E.HDR.unpack_from(data, off)
        self.assertEqual((kind, flags, res, n, sb), (E.DATA, 0, 0, 3, off))
        self.assertEqual((tmin, tmax), (3.25, 10.5))
        self.assertEqual(kinds, (1 << E.KINDS.index('exit')) | (1 << E.KINDS.index('conn'))
                         | E.BIT_NOEVENT)
        self.assertEqual(hcrc, zlib.crc32(data[off:off + 40]))
        self.assertEqual(off + 52 + L2, len(data))
        got = decode(data)[0]
        self.assertEqual([p for p, _r in got], [off, off + 1, off + 2])

    def test_kinds_bits(self):
        def mask(*events):
            recs = []
            for e in events:
                recs.append({} if e == 'MISSING' else {'event': e})
            return data_frames(write_bytes(recs))[0].kinds
        self.assertEqual(mask('exit'), 1 << 1)
        self.assertEqual(mask('meta', 'accept'), 1 | 1 << 10)
        self.assertEqual(mask('MISSING'), E.BIT_NOEVENT)
        self.assertEqual(mask(None), E.BIT_NOEVENT)
        self.assertEqual(mask('brand_new'), E.BIT_OTHER)
        self.assertEqual(mask(5), E.BIT_OTHER)
        self.assertEqual(mask(['exit']), E.BIT_OTHER)
        self.assertEqual(mask(MyStr('exit')), 1 << 1)

    def test_time_bounds(self):
        def bounds(*ts):
            recs = [{} if t == 'MISSING' else {'ts_epoch': t} for t in ts]
            f = data_frames(write_bytes(recs))[0]
            return f.ts_min, f.ts_max
        self.assertEqual(bounds(5, 2.5), (2.5, 5.0))
        self.assertEqual(bounds(1.0, 'MISSING'), (-INF, INF))
        self.assertEqual(bounds(1.0, True), (-INF, INF))
        self.assertEqual(bounds(1.0, NAN), (-INF, INF))
        self.assertEqual(bounds(INF), (-INF, INF))
        self.assertEqual(bounds('5'), (-INF, INF))
        self.assertEqual(bounds(None), (-INF, INF))
        self.assertEqual(bounds(2 ** 2000), (-INF, INF))
        self.assertEqual(bounds(Color.RED, MyFloat(1.5)), (1.5, 3.0))

    def test_positions_unique_increasing_inside_frames(self):
        data = write_bytes([exit_rec(i) for i in range(100)], flush_every=7)
        last = -1
        dec = E.Decoder()
        for f in E.frames(data):
            for p, _r in dec.records(data, f):
                self.assertTrue(f.off <= p < f.end)
                self.assertGreater(p, last)
                last = p

    def test_kind_mask(self):
        self.assertEqual(E.kind_mask(['exit']), (2, True))
        self.assertEqual(E.kind_mask([None]), (E.BIT_NOEVENT, True))
        self.assertEqual(E.kind_mask(['exit', 'later_kind']), (2 | E.BIT_OTHER, False))
        self.assertEqual(E.kind_mask(['later_kind'], ['meta', 'later_kind']), (2, True))
        self.assertEqual(E.kind_mask(['x'], ['meta']), (E.BIT_OTHER, True))

    def test_sniff(self):
        self.assertEqual(E.sniff(E.MAGIC + b'\x01'), 'ebr')
        self.assertEqual(E.sniff(b'  \n{"a": 1}'), 'jsonl')
        self.assertIsNone(E.sniff(b''))
        self.assertIsNone(E.sniff(b'\x89E'))
        self.assertIsNone(E.sniff(b'[1]'))


# ---------------------------------------------------------------------------
# the writer
# ---------------------------------------------------------------------------

class _TmpCase(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.dir = self._td.name

    def tearDown(self):
        self._td.cleanup()

    def path(self, name='x.ebr'):
        return os.path.join(self.dir, name)


class WriterIOTest(_TmpCase):
    def spy(self, fail=None):
        """os.write, counted; `fail(call_no, fd, data)` may raise or write less."""
        calls = []
        real = os.write

        def write(fd, data):
            calls.append(len(data))
            if fail is not None:
                r = fail(len(calls), fd, data)
                if r is not None:
                    return r
            return real(fd, data)
        return calls, write

    def test_one_write_per_flush(self):
        calls, spy = self.spy()
        with mock.patch.object(E.os, 'write', spy):
            w = E.Writer(self.path(), max_frame_records=10)
            for i in range(25):
                w.write(exit_rec(i))
            n = w.flush()
            self.assertEqual(len(calls), 1)         # SEGMENT and 3 DATA frames, one write
            self.assertEqual(n, os.path.getsize(self.path()))
            self.assertEqual(w.flush(), 0)          # idle: no syscall
            self.assertEqual(len(calls), 1)
            w.write(exit_rec(99))
            w.close()
            self.assertEqual(len(calls), 2)
        kinds = [f.kind for f in E.frames(self.path())]
        self.assertEqual(kinds, [E.SEGMENT, E.DATA, E.DATA, E.DATA, E.DATA])
        self.assertEqual(w.stats['writes'], 2)
        self.assertEqual(w.stats['frames'], 5)
        self.assertEqual(w.stats['records'], 26)
        self.assertEqual(len(records_of(read_file(self.path()))), 26)

    def test_frame_caps(self):
        data = write_bytes([exit_rec(i) for i in range(30)], max_frame_records=8)
        self.assertEqual([f.n_records for f in data_frames(data)], [8, 8, 8, 6])
        data = write_bytes([{'s': 'x' * 3000 + str(i), 'i': i} for i in range(10)],
                           max_frame_bytes=7000)
        self.assertTrue(all(f.payload_len < 7000 + 3200 for f in data_frames(data)))
        self.assertGreater(len(data_frames(data)), 3)

    def test_empty_file_stays_empty(self):
        w = E.Writer(self.path())
        w.close()
        self.assertEqual(os.path.getsize(self.path()), 0)
        w = E.Writer(self.path())
        with self.assertRaises(TypeError):
            w.write({'x': object()})
        w.close()
        self.assertEqual(os.path.getsize(self.path()), 0)

    def test_enospc_is_retried_at_the_next_flush(self):
        def fail(n, fd, data):
            if n == 1:
                raise OSError(errno.ENOSPC, 'No space left on device')
        calls, spy = self.spy(fail)
        w = E.Writer(self.path())
        with mock.patch.object(E.os, 'write', spy):
            w.write(exit_rec(1))
            with self.assertRaises(OSError):
                w.flush()
            self.assertEqual(os.path.getsize(self.path()), 0)
            w.write(exit_rec(2))
            w.flush()
        w.close()
        self.assertEqual(w.stats['write_errors'], 1)
        self.assertEqual(w.stats['lost_records'], 0)
        got = records_of(read_file(self.path()))
        self.assertEqual(got, [oracle(exit_rec(1)), oracle(exit_rec(2))])

    def test_partial_write_is_truncated_back_and_retried(self):
        real = os.write

        def fail2(n, fd, data):
            if n == 2:
                return real(fd, bytes(data)[:len(data) // 2])
            if n == 3:
                raise OSError(errno.EIO, 'I/O error')
        calls, spy = self.spy(fail2)
        w = E.Writer(self.path())
        w.write(exit_rec(1))
        w.flush()
        size1 = os.path.getsize(self.path())
        with mock.patch.object(E.os, 'write', spy):
            w.write(exit_rec(2))
            w.write(exit_rec(3))
            calls[:] = [1]                          # the next call is number 2
            with self.assertRaises(OSError):
                w.flush()
        self.assertEqual(os.path.getsize(self.path()), size1)
        w.write(exit_rec(4))
        w.close()
        got = records_of(read_file(self.path()))
        self.assertEqual(got, [oracle(exit_rec(i)) for i in (1, 2, 3, 4)])
        self.assertEqual(w.stats['lost_records'], 0)
        self.assertEqual(w.stats['write_errors'], 1)

    def test_unrecoverable_partial_write_drops_and_starts_a_segment(self):
        real = os.write

        def fail(n, fd, data):
            if n == 1:
                return real(fd, bytes(data)[:len(data) // 2])
            raise OSError(errno.EIO, 'I/O error')
        calls, spy = self.spy(fail)
        w = E.Writer(self.path())
        w.write(exit_rec(1))
        w.flush()
        with mock.patch.object(E.os, 'write', spy), \
                mock.patch.object(E.os, 'ftruncate', side_effect=OSError(errno.EIO, 'no')):
            w.write(exit_rec(2))
            w.write(exit_rec(3))
            with self.assertRaises(OSError):
                w.flush()
        self.assertEqual(w.stats['lost_records'], 2)
        self.assertEqual(w.end, os.path.getsize(self.path()))
        w.write(exit_rec(4))
        w.close()
        data = read_file(self.path())
        got, dec = decode(data)
        self.assertEqual([r for _p, r in got], [oracle(exit_rec(1)), oracle(exit_rec(4))])
        self.assertEqual(dec.stats['bad_frames'], 1)
        self.assertEqual([f.kind for f in E.frames(data)],
                         [E.SEGMENT, E.DATA, E.BAD, E.SEGMENT, E.DATA])

    def test_pending_bytes_are_bounded(self):
        def fail(n, fd, data):
            raise OSError(errno.ENOSPC, 'full')
        calls, spy = self.spy(fail)
        w = E.Writer(self.path())
        with mock.patch.object(E.os, 'write', spy), mock.patch.object(E, '_PENDING_MAX', 100):
            w.write(exit_rec(1))
            with self.assertRaises(OSError):
                w.flush()
        self.assertEqual(w.stats['lost_records'], 1)
        w.write(exit_rec(2))
        w.close()
        self.assertEqual(records_of(read_file(self.path())), [oracle(exit_rec(2))])

    def test_file_like_targets_and_closed_writers(self):
        bio = io.BytesIO()
        with E.Writer(bio, meta={'stream': 'x'}) as w:
            w.write({'a': 1})
        self.assertEqual(records_of(bio.getvalue()), [{'a': 1}])
        with self.assertRaises(ValueError):
            w.write({'a': 2})
        with self.assertRaises(TypeError):
            E.Writer(42)
        for z in (23, -1, 1.5, '3'):
            with self.assertRaises(ValueError):
                E.Writer(io.BytesIO(), zstd=z)
        with self.assertRaises(TypeError):
            E.Writer(io.BytesIO(), zlib=1)                  # v1's knob is gone
        for z in (0, False, None):
            self.assertEqual(E.Writer(io.BytesIO(), zstd=z).zstd, 0)
        self.assertEqual(sorted(w.stats), sorted(E.WRITER_STATS))

    def test_mode(self):
        old = os.umask(0)
        try:
            E.Writer(self.path(), mode=0o640).close()
        finally:
            os.umask(old)
        self.assertEqual(os.stat(self.path()).st_mode & 0o777, 0o640)


class TornTailTest(_TmpCase):
    def sample(self):
        recs = [exit_rec(i) for i in range(12)] + [dict(NODE)] + [{'event': 'stop', 'i': i}
                                                                   for i in range(5)]
        bio = io.BytesIO()
        w = E.Writer(bio, reset_bytes=1500)
        for i, r in enumerate(recs):
            w.write(r)
            if i % 3 == 2:
                w.flush()
        w.close()
        return recs, bio.getvalue()

    def test_cut_at_every_byte(self):
        recs, data = self.sample()
        full = list(E.frames(data))
        self.assertGreater(sum(1 for f in full if f.kind == E.SEGMENT), 1)
        ends = [0] + [f.end for f in full]
        by_end = {}
        n = 0
        for f in full:
            n += f.n_records
            by_end[f.end] = n
        for k in range(len(data) + 1):
            cut = data[:k]
            ce = max(e for e in ends if e <= k)
            self.assertEqual(E.complete_end(cut), ce, k)
            fl = list(E.frames(cut))
            self.assertEqual([(f.off, f.end, f.kind) for f in fl],
                             [(f.off, f.end, f.kind) for f in full if f.end <= k], k)
            got = records_of(cut)
            want = [oracle(r) for r in recs[:by_end.get(ce, 0)]]
            self.assertEqual(len(got), len(want), k)
            self.assertTrue(all(deq(g, r) for g, r in zip(got, want)), k)

    def test_reopen_repairs_every_cut(self):
        recs, data = self.sample()
        p = self.path()
        for k in range(len(data) + 1):
            with open(p, 'wb') as fh:
                fh.write(data[:k])
            ce = E.complete_end(data[:k])
            w = E.Writer.open_append(p)
            self.assertEqual(w.stats['repaired_bytes'], k - ce, k)
            self.assertEqual(w.end, ce)
            w.write({'after': k})
            w.close()
            got = records_of(read_file(p))
            self.assertEqual(got[-1], {'after': k}, k)
            self.assertEqual(len(got), len(records_of(data[:ce])) + 1, k)

    def test_zero_filled_tail(self):
        recs, data = self.sample()
        p = self.path()
        with open(p, 'wb') as fh:
            fh.write(data + b'\x00' * 5000)
        self.assertEqual(E.complete_end(p), len(data))
        self.assertEqual(frame_list(p), frame_list(data))
        w = E.Writer(p)
        self.assertEqual(w.stats['repaired_bytes'], 5000)
        w.write({'z': 1})
        w.close()
        self.assertEqual(records_of(read_file(p))[-1], {'z': 1})

    def test_a_non_ebr_file_is_never_truncated(self):
        p = self.path('x.jsonl')
        with open(p, 'wb') as fh:
            fh.write(b'{"a": 1}\n')
        with self.assertRaises(E.Error):
            E.Writer(p)
        self.assertEqual(read_file(p), b'{"a": 1}\n')

    def test_a_magic_prefix_is_a_torn_first_frame(self):
        p = self.path()
        with open(p, 'wb') as fh:
            fh.write(b'\x89E')
        w = E.Writer(p)
        self.assertEqual(w.stats['repaired_bytes'], 2)
        w.close()
        self.assertEqual(os.path.getsize(p), 0)

    def test_a_tail_too_big_to_repair_is_left(self):
        recs, data = self.sample()
        p = self.path()
        with open(p, 'wb') as fh:
            fh.write(data + b'\x01' * 300)
        with mock.patch.object(E, 'REPAIR_MAX', 100):
            w = E.Writer(p)
            self.assertEqual(w.stats['repaired_bytes'], 0)
            w.write({'z': 1})
            w.close()
            got, dec = decode(read_file(p))
        self.assertEqual(got[-1][1], {'z': 1})
        self.assertEqual(dec.stats['bad_frames'], 1)


class CorruptionTest(unittest.TestCase):
    def sample(self):
        recs = [exit_rec(i) for i in range(30)]
        return recs, write_bytes(recs, flush_every=3, reset_bytes=2500)

    def test_payload_flip_is_a_bad_region_lost_to_the_next_segment(self):
        recs, data = self.sample()
        fl = list(E.frames(data))
        segs = [f.off for f in fl if f.kind == E.SEGMENT]
        victim = [f for f in fl if f.kind == E.DATA and f.off > segs[0]][1]
        nxt_seg = min(s for s in segs if s > victim.off)
        b = bytearray(data)
        b[victim.off + 60] ^= 0x10
        bad = bytes(b)
        got_fl = list(E.frames(bad))
        self.assertIn((victim.off, victim.end, E.BAD), [(f.off, f.end, f.kind) for f in got_fl])
        got, dec = decode(bad)
        before = sum(f.n_records for f in fl if f.end <= victim.off)
        after = sum(f.n_records for f in fl if f.off >= nxt_seg)
        self.assertEqual(len(got), before + after)
        self.assertEqual([r for _p, r in got],
                         [oracle(r) for r in recs[:before]] + [oracle(r) for r in recs[len(recs) - after:]])
        self.assertEqual(dec.stats['bad_frames'], 1)
        lost = sum(f.n_records for f in fl if victim.end <= f.off < nxt_seg)
        self.assertEqual(dec.stats['lost_records'], lost)

    def test_header_flip(self):
        recs, data = self.sample()
        fl = list(E.frames(data))
        victim = data_frames(data)[2]
        nxt_seg = min(f.off for f in fl if f.kind == E.SEGMENT and f.off > victim.off)
        b = bytearray(data)
        b[victim.off + 13] ^= 0x01                   # n_records
        got, dec = decode(bytes(b))
        self.assertEqual(dec.stats['bad_frames'], 1)
        before = sum(f.n_records for f in fl if f.end <= victim.off)
        after = sum(f.n_records for f in fl if f.off >= nxt_seg)
        self.assertEqual([r for _p, r in got],
                         [oracle(r) for r in recs[:before]] + [oracle(r) for r in recs[len(recs) - after:]])

    def test_definition_flip_with_valid_crcs(self):
        recs, data = self.sample()
        victim = data_frames(data)[0]
        payload = bytearray(payload_of(data, victim.off))
        # str_base, shape_base, defs_len, then the first def's opcode
        p = 0
        for _ in range(3):
            _v, p = E._ruv(bytes(payload), p)
        payload[p] = 9
        got, dec = decode(rewrite_payload(data, victim.off, bytes(payload)))
        self.assertEqual(dec.stats['bad_frames'], 1)
        self.assertGreater(dec.stats['lost_records'], 0)
        with self.assertRaises(E.FrameError):
            decode(rewrite_payload(data, victim.off, bytes(payload)), strict=True)

    def test_settle_holds_a_corrupt_tail_as_pending(self):
        recs, data = self.sample()
        last = data_frames(data)[-2]
        b = bytearray(data)
        b[last.off + 50] ^= 0xff
        bad = bytes(b)
        self.assertIn(E.BAD, [f.kind for f in E.frames(bad)])
        held = list(E.frames(bad, settle=len(bad) - last.off))
        self.assertNotIn(E.BAD, [f.kind for f in held])
        self.assertEqual(held[-1].end, last.off)

    def test_random_damage_never_crashes_or_invents_records(self):
        recs, data = self.sample()
        truth = set(json.dumps(r) for r in [oracle(r) for r in recs])
        rng = random.Random(99)
        for _ in range(300):
            b = bytearray(data)
            k = rng.random()
            i = rng.randrange(len(b))
            if k < 0.5:
                for _ in range(rng.randrange(1, 4)):
                    b[rng.randrange(len(b))] = rng.randrange(256)
            elif k < 0.65:
                b[i:i + rng.randrange(1, 200)] = bytes(200)[:len(b[i:i + 200])]
            elif k < 0.8:
                del b[i:i + rng.randrange(1, 200)]
            else:
                b[i:i] = bytes(rng.randrange(256) for _ in range(rng.randrange(1, 60)))
            bad = bytes(b)
            got, dec = decode(bad)
            for p, r in got:
                self.assertIn(json.dumps(r), truth)
            rev = list(E.records_reverse(bad, len(bad)))
            self.assertEqual(sorted(p for p, _r in rev), sorted(p for p, _r in got))
            for verify in (False, True):
                ce = E._complete_end(E._Src(bad), len(bad), verify)
                self.assertEqual(frame_list(bad, end=ce, verify=verify)[::-1],
                                 [(f.off, f.end, f.kind)
                                  for f in E.frames_reverse(bad, ce, verify=verify)])
            self.assertLessEqual(E.complete_end(bad), len(bad))

    def test_garbage_between_segments(self):
        a = write_bytes([{'a': 1}])
        b = write_bytes([{'b': 2}])
        data = a + b'garbage\x89EBRnot a header' + b
        got, dec = decode(data)
        self.assertEqual([r for _p, r in got], [{'a': 1}, {'b': 2}])
        self.assertEqual(dec.stats['bad_frames'], 1)
        self.assertEqual(E.resync(data, len(a)), len(data) - len(b))


class ReverseAndResyncTest(unittest.TestCase):
    def sample(self):
        recs = mixed_records(60)
        return recs, write_bytes(recs, flush_every=4, reset_bytes=3000)

    def test_frames_reverse_is_frames_reversed(self):
        recs, data = self.sample()
        fwd = [(f.off, f.end, f.kind) for f in E.frames(data)]
        rev = [(f.off, f.end, f.kind) for f in E.frames_reverse(data, len(data))]
        self.assertEqual(rev, fwd[::-1])
        rev = [(f.off, f.end, f.kind) for f in E.frames_reverse(data, len(data), verify=True)]
        self.assertEqual(rev, fwd[::-1])

    def test_frames_reverse_through_a_bad_region(self):
        recs, data = self.sample()
        victims = data_frames(data)[3:5]
        b = bytearray(data)
        b[victims[0].off + 2] ^= 0xff                       # the header of one
        b[victims[1].end - 3] ^= 0xff                       # the trailer of the next
        bad = bytes(b)
        fwd = [(f.off, f.end, f.kind) for f in E.frames(bad)]
        rev = [(f.off, f.end, f.kind) for f in E.frames_reverse(bad, len(bad))]
        self.assertEqual(rev, fwd[::-1])
        self.assertIn(E.BAD, [k for _o, _e, k in fwd])

    def test_short_garbage_right_before_a_header(self):
        a = write_bytes([{'a': i} for i in range(4)], flush_every=1)
        f = data_frames(a)[2]
        data = a[:f.off] + b'0123456789' + a[f.off:]       # 10 bytes: less than a frame
        end = f.off + 10
        self.assertEqual(frame_list(data, end=end)[-1], (f.off, end, E.BAD))
        fwd = decode(data)[0]
        rev = list(E.records_reverse(data, len(data)))
        self.assertEqual(sorted(p for p, _r in rev), sorted(p for p, _r in fwd))
        self.assertEqual([(f.off, f.end, f.kind) for f in E.frames_reverse(data, len(data))],
                         frame_list(data)[::-1])

    def test_reverse_from_a_damaged_last_frame(self):
        recs, data = self.sample()
        last = data_frames(data)[-1]
        b = bytearray(data)
        b[last.off + 50] ^= 0x20                            # its payload only
        bad = bytes(b)
        self.assertEqual([(f.off, f.end, f.kind) for f in E.frames_reverse(bad, len(bad), verify=True)],
                         frame_list(bad)[::-1])
        self.assertEqual([(f.off, f.end, f.kind) for f in E.frames_reverse(bad, len(bad))],
                         frame_list(bad, verify=False)[::-1])
        self.assertEqual(E.complete_end(bad), last.off)

    def test_is_boundary_exactly_at_frame_ends(self):
        recs, data = self.sample()
        ends = set([0] + [f.end for f in E.frames(data)])
        for off in range(len(data) + 1):
            self.assertEqual(E.is_boundary(data, off), off in ends, off)

    def test_resync_and_peek(self):
        recs, data = self.sample()
        fl = list(E.frames(data))
        starts = [f.off for f in fl]
        for off in range(0, len(data), 7):
            want = min([s for s in starts if s >= off] or [None]) if any(s >= off for s in starts) else None
            self.assertEqual(E.resync(data, off), want, off)
        last = fl[-1]
        cut = data[:last.off + 50]
        f = E.peek(cut, last.off)
        self.assertEqual((f.off, f.end, f.kind, f.n_records), (last.off, last.end, last.kind, last.n_records))
        self.assertIsNone(E.peek(cut, last.off + 1))
        self.assertEqual(E.complete_end(cut), last.off)

    def test_records_reverse(self):
        recs, data = self.sample()
        fwd = decode(data)[0]
        rev = list(E.records_reverse(data, len(data)))
        self.assertEqual([p for p, _r in rev], [p for p, _r in fwd][::-1])
        self.assertTrue(all(deq(a[1], b[1]) for a, b in zip(rev, fwd[::-1])))
        for f in E.frames(data):
            start = f.end
            want = [x for x in fwd if x[0] >= start][::-1]
            got = list(E.records_reverse(data, len(data), start=start))
            self.assertEqual([p for p, _r in got], [p for p, _r in want])
            end = f.end
            want = [x for x in fwd if x[0] < end][::-1]
            got = list(E.records_reverse(data, end))
            self.assertEqual([p for p, _r in got], [p for p, _r in want])
        st = {}
        kinds = {'exit'}
        where = {'user': {'u1', 'u2'}}
        got = list(E.records_reverse(data, len(data), kinds=kinds, where=where, stats=st))
        want = reference_filter(fwd, kinds, where)[::-1]
        self.assertEqual([p for p, _r in got], [p for p, _r in want])
        self.assertGreater(st['filtered'], 0)

    def test_records_reverse_with_a_bad_region(self):
        recs, data = self.sample()
        victim = data_frames(data)[5]
        b = bytearray(data)
        b[victim.off + 70] ^= 0x04
        bad = bytes(b)
        fwd = decode(bad)[0]
        st = {}
        rev = list(E.records_reverse(bad, len(bad), stats=st))
        self.assertEqual([p for p, _r in rev], [p for p, _r in fwd][::-1])
        self.assertEqual(st['bad_frames'], 1)


class DecoderAtTest(unittest.TestCase):
    def test_from_every_boundary(self):
        recs = mixed_records(50, seed=3)
        data = write_bytes(recs, flush_every=3, reset_bytes=2500)
        full = decode(data)[0]
        for f in [None] + list(E.frames(data)):
            b = 0 if f is None else f.end
            dec = E.Decoder.at(data, b)
            got = []
            for g in E.frames(data, start=b):
                got.extend(dec.records(data, g))
            want = [x for x in full if x[0] >= b]
            self.assertEqual([p for p, _r in got], [p for p, _r in want], b)
            self.assertTrue(all(deq(x[1], y[1]) for x, y in zip(got, want)))
            self.assertFalse(dec.broken)

    def test_not_a_boundary(self):
        data = write_bytes([{'a': 1}, {'a': 2}], flush_every=1)
        with self.assertRaises(E.StateError):
            E.Decoder.at(data, 5)
        with self.assertRaises(E.StateError):
            E.Decoder.at(data, len(data) - 1)

    def test_a_gap_breaks_the_decoder_until_the_next_segment(self):
        a = write_bytes([{'a': i} for i in range(6)], flush_every=2)
        b = write_bytes([{'b': 1}])
        data = a + b
        fl = data_frames(data)
        dec = E.Decoder()
        got = []
        for f in E.frames(data):
            if f.off == fl[1].off:
                continue                                # skipped: a gap
            got.extend(r for _p, r in dec.records(data, f))
        self.assertEqual(got, [{'a': 0}, {'a': 1}, {'b': 1}])
        self.assertEqual(dec.stats['lost_frames'], 1)
        dec = E.Decoder(strict=True)
        fr = list(E.frames(data))
        dec.skip(fr[0])
        with self.assertRaises(E.StateError):
            dec.skip(fr[2])

    def test_an_abandoned_iterator_keeps_the_tables(self):
        recs = [exit_rec(i) for i in range(20)]
        data = write_bytes(recs, flush_every=5)
        dec = E.Decoder()
        got = []
        for i, f in enumerate(E.frames(data)):
            it = dec.records(data, f)
            if i % 2 == 0:
                continue                                # never iterated
            got.extend(r for _p, r in it)
        want = [oracle(r) for i, r in enumerate(recs) if (i // 5) % 2 == 0]
        self.assertEqual(got, want)


class SegmentTest(_TmpCase):
    def test_many_small_segments(self):
        recs = mixed_records(400, seed=5)
        data = write_bytes(recs, flush_every=2, reset_bytes=4096)
        fl = list(E.frames(data))
        segs = [f for f in fl if f.kind == E.SEGMENT]
        self.assertGreater(len(segs), 10)
        starts = set(f.off for f in segs)
        for f in fl:
            if f.kind == E.DATA:
                self.assertIn(f.off - f.seg_back, starts)
                self.assertEqual(f.seg_off, f.off - f.seg_back)
        for a, b in zip(segs, segs[1:]):
            last = max(f.end for f in fl if f.end <= b.off)
            self.assertLess(b.off - a.off - (last - a.off), 1)
        self.assertTrue(all(deq(g, oracle(r)) for g, r in zip(records_of(data), recs)))

    def test_segment_is_written_lazily_with_the_first_frame(self):
        calls = []
        real = os.write

        def spy(fd, data):
            calls.append(bytes(data[:5]))
            return real(fd, data)
        p = self.path()
        with mock.patch.object(E.os, 'write', spy):
            w = E.Writer(p)
            self.assertEqual(w.flush(), 0)
            w.write({'a': 1})
            w.flush()
            w.close()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][4], E.SEGMENT)
        self.assertEqual([f.kind for f in E.frames(p)], [E.SEGMENT, E.DATA])

    def test_table_overflow_starts_a_segment(self):
        with mock.patch.object(E, 'MAX_STRINGS', 40), mock.patch.object(E, 'MAX_SHAPES', 6):
            recs = [{'k%d' % (i % 9): 's%d' % i, 'i': i} for i in range(120)]
            bio = io.BytesIO()
            w = E.Writer(bio)
            for r in recs:
                w.write(r)
            with self.assertRaises(E.RecordTooLarge):
                w.write(dict(('x%d' % i, 'v%d' % i) for i in range(30)))
            w.write({'after': 'ok'})
            w.close()
            data = bio.getvalue()
        got, dec = decode(data)
        self.assertEqual([r for _p, r in got], recs + [{'after': 'ok'}])
        self.assertGreater(sum(1 for f in E.frames(data) if f.kind == E.SEGMENT), 5)
        self.assertEqual(w.stats['encode_errors'], 1)

    def test_a_record_too_large_for_a_frame(self):
        with mock.patch.object(E, '_FRAME_ROOM', 5000):
            bio = io.BytesIO()
            w = E.Writer(bio)
            w.write({'a': 'x' * 100})
            with self.assertRaises(E.RecordTooLarge):
                w.write({'b': 'y' * 6000})
            w.write({'c': 'z' * 4000})
            w.write({'d': 1})
            w.close()
        self.assertEqual(records_of(bio.getvalue()), [{'a': 'x' * 100}, {'c': 'z' * 4000}, {'d': 1}])


class OpenAppendTest(_TmpCase):
    def segments(self, p):
        return sum(1 for f in E.frames(p) if f.kind == E.SEGMENT)

    def test_resumes_one_segment(self):
        p = self.path()
        for i in range(5):
            w = E.Writer.open_append(p, meta={'host': 'h'})
            w.write(exit_rec(i))
            w.write(dict(NODE))
            w.close()
            self.assertEqual(w.stats['segments'], 1 if i == 0 else 0)
        self.assertEqual(self.segments(p), 1)
        got = records_of(read_file(p))
        self.assertEqual(len(got), 10)
        self.assertTrue(deq(got[-1], oracle(NODE)))
        # the resumed tables are reused: a later frame defines nothing new
        last = data_frames(read_file(p))[-1]
        payload = payload_of(read_file(p), last.off)
        sb, q = E._ruv(payload, 0)
        hb, q = E._ruv(payload, q)
        dl, q = E._ruv(payload, q)
        self.assertGreater(sb, 0)
        self.assertGreater(hb, 0)
        self.assertEqual(E._count_defs(payload[q:q + dl])[1], 0)    # no shape defined again

    def test_a_new_segment_past_reset_bytes(self):
        p = self.path()
        for i in range(6):
            w = E.Writer.open_append(p, reset_bytes=2000)
            for j in range(3):
                w.write(exit_rec(i * 3 + j))
            w.close()
        self.assertGreater(self.segments(p), 1)
        self.assertEqual(len(records_of(read_file(p))), 18)

    def test_a_new_segment_after_a_torn_tail(self):
        p = self.path()
        w = E.Writer.open_append(p)
        w.write({'a': 1})
        w.close()
        with open(p, 'ab') as fh:
            fh.write(E.MAGIC + b'\x02torn')
        w = E.Writer.open_append(p)
        self.assertEqual(w.stats['repaired_bytes'], 9)
        w.write({'a': 2})
        w.close()
        self.assertEqual(self.segments(p), 2)
        self.assertEqual(records_of(read_file(p)), [{'a': 1}, {'a': 2}])

    def test_a_new_segment_after_a_segment_of_another_version(self):
        for seg in (json.dumps({'format': 'ebr', 'version': 2, 'str_id_size': 2,
                                'kinds': list(E.KINDS)}).encode(),
                    seg_v2(version=3), seg_v2(version=1)):
            p = self.path()
            with open(p, 'wb') as fh:
                fh.write(E._frame(E.SEGMENT, 0, seg, 0, NAN, NAN, 0, 0))
            w = E.Writer.open_append(p)
            w.write({'a': 1})
            w.close()
            self.assertEqual(self.segments(p), 2)
            got, dec = decode(read_file(p))
            self.assertEqual([r for _p, r in got], [{'a': 1}])
            os.unlink(p)

    def test_a_v1_segment_is_never_continued(self):
        p = self.path()
        shutil.copy(str(DATA / 'v1.ebr'), p)
        before = self.segments(p)
        for i in range(3):
            w = E.Writer.open_append(p)
            w.write({'after': i})
            w.close()
        self.assertEqual(self.segments(p), before + 1)      # one v2 segment, then resumed
        self.assertEqual(segment_of(read_file(p), before)['version'], 2)
        self.assertEqual(records_of(read_file(p))[-3:], [{'after': i} for i in range(3)])

    def test_a_new_segment_under_another_registry(self):
        p = self.path()
        with mock.patch.object(E, 'KINDS', E.KINDS + ('newer',)):
            w = E.Writer.open_append(p)
            w.write({'a': 1})
            w.close()
        w = E.Writer.open_append(p)
        w.write({'a': 2})
        w.close()
        self.assertEqual(self.segments(p), 2)

    @unittest.skipIf(fcntl is None, 'no flock')
    def test_busy_while_another_writer_holds_the_lock(self):
        p = self.path()
        fd = os.open(p, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            t0 = os.times()[4]
            with self.assertRaises(E.Busy):
                E.Writer.open_append(p, lock_wait=0.3)
            self.assertGreaterEqual(os.times()[4] - t0, 0.25)
            with self.assertRaises(E.Busy):
                E.Writer(p)
            w = E.Writer(p, lock=False)
            w.close()
        finally:
            os.close(fd)
        w = E.Writer(p)
        w.write({'a': 1})
        w.close()

    def test_unlocked_where_flock_is_not_supported(self):
        p = self.path()
        err = OSError(errno.ENOLCK, 'No locks available')
        with mock.patch.object(E.fcntl, 'flock', side_effect=err):
            w = E.Writer(p)
            w.write({'a': 1})
            w.close()
        self.assertEqual(records_of(read_file(p)), [{'a': 1}])


class LegacyZlibTest(unittest.TestCase):
    """F_ZLIB: v1 writers' zlib=1..9. Nothing writes it any more; it reads."""

    def zlib_frame(self, recs):
        """A v2 file of `recs` in one DATA frame, then that frame deflated."""
        data = write_bytes(recs)
        f = data_frames(data)[0]
        forged = rewrite_payload(data, f.off, zlib.compress(payload_of(data, f.off), 6))
        return rewrite_header(forged, f.off, flags=E.F_ZLIB)

    def test_the_v1_fixture_reads(self):
        data = read_file(str(DATA / 'v1-zlib.ebr'))
        flags = [f.flags for f in data_frames(data)]
        self.assertIn(E.F_ZLIB, flags)
        self.assertIn(0, flags)                      # a frame zlib would grow stays raw
        want = [json.loads(x) for x in read_file(str(DATA / 'v1.jsonl')).splitlines()]
        got, dec = decode(data)
        self.assertEqual(len(got), len(want))
        self.assertTrue(all(deq(g, w) for (_p, g), w in zip(got, want)))
        self.assertEqual(sum(dec.stats[k] for k in ('bad_frames', 'unsupported_frames',
                                                    'lost_records', 'zstd_unavailable')), 0)

    def test_a_zlib_frame_reads_in_any_segment(self):
        recs = [exit_rec(i) for i in range(10)]
        got, dec = decode(self.zlib_frame(recs), strict=True)
        self.assertTrue(all(deq(g, oracle(r)) for (_p, g), r in zip(got, recs)))
        self.assertEqual(len(got), 10)

    def bad_zlib(self, body):
        data = write_bytes([{'a': 1}, {'a': 2}])
        f = data_frames(data)[0]
        forged = rewrite_payload(data, f.off, body)
        forged = rewrite_header(forged, f.off, flags=E.F_ZLIB)
        return decode(forged)

    def test_an_inflation_bomb_is_bad(self):
        with mock.patch.object(E, 'MAX_PAYLOAD', 1 << 20):
            got, dec = self.bad_zlib(zlib.compress(b'\x00' * (2 << 20)))
        self.assertEqual(got, [])
        self.assertEqual(dec.stats['bad_frames'], 1)

    def test_truncated_and_trailing_zlib_are_bad(self):
        z = zlib.compress(b'\x00\x00\x00')
        for body in (z[:-3], z + b'junk', b'not zlib', z + z):
            got, dec = self.bad_zlib(body)
            self.assertEqual(dec.stats['bad_frames'], 1, body)


class CompatTest(unittest.TestCase):
    def test_unknown_flags_make_the_segment_unsupported(self):
        # bit 2, and both bit0 (zlib) and bit1 (zstd): never BAD, skipped to
        # the next SEGMENT; zstd_unavailable counts F_ZSTD frames only
        for flags in (4, 0x80, E.F_ZLIB | E.F_ZSTD):
            a = write_bytes([{'a': i} for i in range(6)], flush_every=2)
            b = write_bytes([{'b': 1}])
            f = data_frames(a)[1]
            data = rewrite_header(a, f.off, flags=flags) + b
            got, dec = decode(data)
            self.assertEqual([r for _p, r in got], [{'a': 0}, {'a': 1}, {'b': 1}], flags)
            self.assertEqual(dec.stats['unsupported_frames'], 2, flags)
            self.assertEqual((dec.stats['bad_frames'], dec.stats['zstd_unavailable']), (0, 0))

    def test_unknown_kinds_are_skipped(self):
        a = write_bytes([{'a': i} for i in range(4)], flush_every=2)
        f = data_frames(a)[1]
        adv = E._frame(9, 0, b'advisory', 0, NAN, NAN, 0, 0)
        data = a[:f.off] + adv + a[f.off:]
        got, dec = decode(data)
        self.assertEqual([r for _p, r in got], [{'a': i} for i in range(4)])
        self.assertIn(9, [f.kind for f in E.frames(data)])

    def test_json_segments_are_read_by_v1_rules(self):
        """A SEGMENT payload starting '{' is v1: version 1, format ebr and a
        str_id_size of 2 or 4, else the segment is unsupported."""
        b = write_bytes([{'b': 1}])
        for seg in ({'format': 'ebr', 'version': 2, 'str_id_size': 2},
                    {'format': 'other', 'version': 1, 'str_id_size': 2},
                    {'format': 'ebr', 'version': 1, 'str_id_size': 3},
                    {'format': 'ebr', 'version': True, 'str_id_size': 2},
                    {'format': 'ebr', 'version': 1.0, 'str_id_size': 2}):
            v2 = E._frame(E.SEGMENT, 0, json.dumps(seg).encode(), 0, NAN, NAN, 0, 0)
            junk = E._frame(E.DATA, 0, b'\x00\x00\x00\x01\x05whatever', 1, 1.0, 1.0,
                            len(v2), 2)
            data = v2 + junk + b
            got, dec = decode(data)
            self.assertEqual([r for _p, r in got], [{'b': 1}], seg)
            self.assertEqual(dec.stats['unsupported_frames'], 1, seg)
            self.assertEqual(dec.stats['bad_frames'], 0, seg)

    def test_str_id_size_4(self):
        enc = E._Encoder(4)
        recs = [exit_rec(i) for i in range(5)] + [{'s': 'x', 'l': ['a', 'b'], 'm': {'k': 'v'}}]
        bodies = [enc.record(r) for r in recs]
        payload = (E._uv(0) + E._uv(0) + E._uv(len(enc.defs)) + bytes(enc.defs)
                   + b''.join(E._uv(len(b)) + b for b in bodies))
        for segp in (json.dumps({'format': 'ebr', 'version': 1, 'str_id_size': 4,
                                 'kinds': list(E.KINDS)}).encode(),
                     seg_v2(sis=4)):
            s = E._frame(E.SEGMENT, 0, segp, 0, NAN, NAN, 0, 0)
            d = E._frame(E.DATA, 0, payload, len(recs), -INF, INF, len(s), 0xffffffff)
            got, dec = decode(s + d)
            self.assertEqual(len(got), len(recs))
            self.assertTrue(all(deq(g, oracle(r)) for (_p, g), r in zip(got, recs)))
            self.assertEqual(dec.segment['str_id_size'], 4)

    def test_reserved_bytes_are_ignored(self):
        data = write_bytes([{'a': 1}])
        for f in E.frames(data):
            data = rewrite_header(data, f.off, reserved=0xbeef)
        self.assertEqual(records_of(data), [{'a': 1}])


# ---------------------------------------------------------------------------
# v2: maps, the binary SEGMENT, v1 files
# ---------------------------------------------------------------------------

def split_v2(payload):
    """A v2 SEGMENT payload's fields, read here by the spec: (version,
    str_id_size, created, reset_bytes, kinds, meta, end of meta)."""
    version, sis, created = struct.unpack_from('<BBd', payload, 0)
    reset, p = E._ruv(payload, 10)
    n, p = E._ruv(payload, p)
    kinds = []
    for _ in range(n):
        k, p = E._ruv(payload, p)
        kinds.append(payload[p:p + k].decode('utf-8', 'surrogatepass'))
        p += k
    meta, p = E._dec_map(payload, p)
    return version, sis, created, reset, kinds, meta, p


class MapTest(unittest.TestCase):
    """The map grammar of SEGMENT meta and MARK payloads."""

    def test_bytes(self):
        def enc(d):
            return bytes(E._enc_map(d))
        self.assertEqual(enc({}), b'\x00')
        self.assertEqual(enc({'a': 1}), b'\x01\x01a\x03\x02')
        self.assertEqual(enc({'n': None, 'f': False, 't': True}),
                         b'\x03\x01n\x00\x01f\x01\x01t\x02')
        self.assertEqual(enc({'i': -1}), b'\x01\x01i\x03\x01')
        self.assertEqual(enc({'x': 1.5}), b'\x01\x01x\x05' + struct.pack('<d', 1.5))
        self.assertEqual(enc({'s': 'é'}), b'\x01\x01s\x06\x02\xc3\xa9')
        self.assertEqual(enc({'m': {}}), b'\x01\x01m\x07\x00')
        self.assertEqual(enc({'l': [1, 'a']}), b'\x01\x01l\x08\x02\x03\x02\x06\x01a')
        self.assertEqual(enc({'b': 2 ** 64}),
                         b'\x01\x01b\x04\x09' + (2 ** 64).to_bytes(9, 'little', signed=True))
        self.assertEqual(enc({'i': 2 ** 63 - 1})[:4], b'\x01\x01i\x03')    # int64: zigzag
        self.assertEqual(enc({'i': -2 ** 63})[:4], b'\x01\x01i\x03')
        self.assertEqual(enc({'i': 2 ** 63})[:4], b'\x01\x01i\x04')        # past it: bytes

    def test_round_trip(self):
        d = {'none': None, 't': True, 'f': False, 'zero': 0, 'neg': -7, 'i64': 2 ** 63 - 1,
             'i64n': -2 ** 63, 'big': 2 ** 200, 'nbig': -2 ** 64 - 1, 'one': 1.0, 'nan': NAN,
             'inf': INF, 'ninf': -INF, 'nz': -0.0, 'tiny': 5e-324, 's': '', 'lone': '\ud800x',
             'astral': '\U0001F600', 'split': '😀', 'l': [],
             'ln': [[[]], [{}], [None, 1, 'x', 2.5, [2 ** 70]]], 'm': {'a': {'b': {'c': 'deep'}}},
             'a key with spaces': 1, '': 'the empty key'}
        b = bytes(E._enc_map(d))
        got, p = E._dec_map(b)
        self.assertEqual(p, len(b))
        self.assertTrue(deq(got, d), got)
        self.assertEqual(got['split'], '😀')      # as given: not merged, as JSON would
        # read from where it starts; it says where it ends
        self.assertEqual(E._dec_map(b'xx' + b + b'tail', 2)[1], 2 + len(b))

    def test_what_json_would_convert(self):
        d = {7: 'int', 2.5: 'float', None: 'null', True: 'bool', '7': 'str seven', NAN: 'nan',
             'tuple': (1, (2,)), 'enum': Color.RED, 'big enum': Color.BIG, 'myfloat': MyFloat(2.5),
             'mystr': MyStr('x'), 'mylist': MyList([1]), 'bytes': b'raw', 'set': {3},
             'class': Color, 'ordered': collections.OrderedDict([('z', 1), ('a', 2)])}
        got = E._dec_map(bytes(E._enc_map(d)))[0]
        self.assertTrue(deq(got, json.loads(json.dumps(d, default=str))), got)

    def test_refused(self):
        with self.assertRaises(TypeError):
            E._enc_map({(1, 2): 'a tuple key'})
        loop = {}
        loop['self'] = loop
        with self.assertRaises(ValueError):
            E._enc_map(loop)
        lst = []
        lst.append(lst)
        with self.assertRaises(ValueError):
            E._enc_map({'l': lst})
        deep = cur = {}
        for _ in range(100):
            cur['d'] = {}
            cur = cur['d']
        with self.assertRaises(ValueError):
            E._enc_map(deep)

    def test_malformed(self):
        good = bytes(E._enc_map({'a': [1, 'x', {'b': 2.5}], 'c': 2 ** 70}))
        for k in range(len(good)):
            with self.assertRaises(E.FrameError, msg=k):
                E._dec_map(good[:k])
        for bad in (b'\x01\x01a\x09', b'\x01\x01a\xff', b'\x01\x02\xff\xfe\x00', b'\x05',
                    b'\x01\x01a\x06\x05ab', b'\x01\x01a\x08\x7f\x00', b'\x01\x01a\x04\x09\x00',
                    b'\xff' * 11):
            with self.assertRaises(E.FrameError, msg=bad):
                E._dec_map(bad)
        with self.assertRaises(E.FrameError):
            E._dec_map(b'\x01\x01k\x07' * 70 + b'\x00')        # nested past 64
        self.assertEqual(E._dec_map(b'\x01\x01k\x07' * 60 + b'\x00')[1], 241)


class SegmentV2Test(unittest.TestCase):
    """The binary SEGMENT, and how a reader picks a SEGMENT's parser."""

    def file(self, segp, recs=({'a': 1}, {'a': 2})):
        """A SEGMENT with payload `segp`, a DATA frame of `recs` encoded
        against it, then a good file of one record."""
        enc = E._Encoder(2)
        bodies = [enc.record(dict(r)) for r in recs]
        payload = (E._uv(0) + E._uv(0) + E._uv(len(enc.defs)) + bytes(enc.defs)
                   + b''.join(E._uv(len(b)) + b for b in bodies))
        s = E._frame(E.SEGMENT, 0, segp, 0, NAN, NAN, 0, 0)
        d = E._frame(E.DATA, 0, payload, len(recs), -INF, INF, len(s), 0xffffffff)
        return s + d + write_bytes([{'after': 1}])

    def test_every_meta_value_type(self):
        meta = {'host': 'h', 'pid': 1234, 'neg': -5, 'big': 2 ** 70, 'nbig': -2 ** 90,
                'f': 1.5, 'nan': NAN, 'inf': INF, 'nz': -0.0, 'tiny': 5e-324, 't': True,
                'no': False, 'none': None, 'list': [1, 'a', None, [2.5]], 'tuple': (1, 2),
                'nested': {'a': {'b': [{}]}}, 'lone': '\ud800', 'astral': '\U0001F600',
                'enum': Color.RED, 'myfloat': MyFloat(2.5), 'mystr': MyStr('s'),
                'bytes': b'x', 'set': {1}, 'object': Color, 7: 'an int key', 2.5: 'a float key',
                None: 'a None key', True: 'a bool key'}
        data = write_bytes([{'a': 1}], meta=meta)
        seg = segment_of(data)
        self.assertEqual(list(seg)[:6], ['format', 'version', 'str_id_size', 'kinds',
                                         'reset_bytes', 'created'])
        mine = dict((k, v) for k, v in seg.items() if k not in E._SEG_KEYS)
        self.assertTrue(deq(mine, json.loads(json.dumps(meta, default=str))), mine)
        self.assertEqual(records_of(data), [{'a': 1}])

    def test_the_segments_own_keys_win(self):
        meta = {'version': 'x', 'format': 'y', 'kinds': 'z', 'str_id_size': 9, 'reset_bytes': -1,
                'created': 'then', 'zlib': 9, 'host': 'h'}
        data = write_bytes([{'a': 1}], meta=meta)
        seg = segment_of(data)
        self.assertEqual((seg['version'], seg['format'], seg['kinds'], seg['str_id_size'],
                          seg['reset_bytes']), (2, 'ebr', list(E.KINDS), 2, E.DEFAULT_RESET_BYTES))
        self.assertIsInstance(seg['created'], float)
        self.assertEqual((seg['zlib'], seg['host']), (9, 'h'))         # not v2's: meta
        self.assertEqual(split_v2(payload_of(data, 0))[5], {'zlib': 9, 'host': 'h'})  # not stored
        forged = seg_v2(meta={'version': 99, 'format': 'other', 'kinds': 'k', 'str_id_size': 77,
                              'reset_bytes': -1, 'created': 'c', 'host': 'h'}, sis=2, created=4.5)
        data = self.file(forged)
        seg = segment_of(data)
        self.assertEqual(seg, {'format': 'ebr', 'version': 2, 'str_id_size': 2,
                               'kinds': list(E.KINDS), 'reset_bytes': E.DEFAULT_RESET_BYTES,
                               'created': 4.5, 'host': 'h'})
        self.assertEqual(records_of(data), [{'a': 1}, {'a': 2}, {'after': 1}])

    def test_the_first_byte_picks_the_parser(self):
        v1 = {'format': 'ebr', 'version': 1, 'str_id_size': 2, 'kinds': list(E.KINDS)}
        for segp, ok in ((seg_v2(), True), (json.dumps(v1).encode(), True),
                         (seg_v2(version=3), False), (seg_v2(version=0), False),
                         (seg_v2(version=1), False), (seg_v2(version=255), False),
                         (b' ' + json.dumps(v1).encode(), False), (b'\x7c{}', False)):
            got, dec = decode(self.file(segp))
            st = dec.stats
            if ok:
                self.assertEqual([r for _p, r in got], [{'a': 1}, {'a': 2}, {'after': 1}], segp)
                self.assertEqual(st['unsupported_frames'], 0, segp)
            else:
                self.assertEqual([r for _p, r in got], [{'after': 1}], segp)
                self.assertEqual(st['unsupported_frames'], 1, segp)
            self.assertEqual((st['bad_frames'], st['lost_records']), (0, 0), segp)
        self.assertEqual(segment_of(self.file(json.dumps(v1).encode()))['version'], 1)

    def test_bytes_after_meta_are_ignored(self):
        plain = self.file(seg_v2(meta={'host': 'h'}))
        more = self.file(seg_v2(meta={'host': 'h'}, tail=b'\x07room\x00for fields'))
        self.assertEqual(records_of(more), records_of(plain))
        self.assertEqual(segment_of(more), segment_of(plain))
        self.assertEqual(decode(more, strict=True)[1].stats['bad_frames'], 0)

    def test_a_malformed_v2_segment_is_bad(self):
        good = seg_v2(meta={'host': 'h', 'l': [1, 2]})
        cases = [good[:k] for k in range(len(good))]          # truncated anywhere, empty too
        cases += [seg_v2(sis=3), seg_v2(sis=0), seg_v2(sis=8),
                  seg_v2()[:-1] + b'\x01\x01h\x09',            # a bad tag in meta
                  seg_v2(kinds=['meta', '\udcff'])[:-1].replace(b'\xed\xb3\xbf', b'\xff\xfe\xfd')
                  + b'\x00',                                    # a kind that is not UTF-8
                  struct.pack('<BBd', 2, 2, 0.0) + E._uv(5) + E._uv(100) + b'\x01a']
        for c in cases:
            got, dec = decode(self.file(c))
            self.assertEqual([r for _p, r in got], [{'after': 1}], c)
            st = dec.stats
            self.assertEqual((st['bad_frames'], st['unsupported_frames'], st['lost_records']),
                             (1, 0, 2), c)
        with self.assertRaises(E.FrameError):
            decode(self.file(seg_v2(sis=3)), strict=True)

    def test_a_json_segment_that_is_not_an_object_or_not_json_is_bad(self):
        for segp in (b'{"format": "ebr"', b'{nope}', b'{' + b'[' * 100000):
            got, dec = decode(self.file(segp))
            self.assertEqual([r for _p, r in got], [{'after': 1}], segp[:20])
            self.assertEqual(dec.stats['bad_frames'], 1, segp[:20])

    def test_meta_is_checked_when_the_writer_opens(self):
        with self.assertRaises(TypeError):
            E.Writer(io.BytesIO(), meta={(1, 2): 'x'})
        loop = {}
        loop['loop'] = loop
        with self.assertRaises(ValueError):
            E.Writer(io.BytesIO(), meta={'m': loop})
        d = tempfile.mkdtemp()
        try:
            p = os.path.join(d, 'x.ebr')
            with self.assertRaises(TypeError):
                E.Writer(p, meta={(1, 2): 'x'})
            self.assertFalse(os.path.exists(p))
        finally:
            shutil.rmtree(d)
        bio = io.BytesIO()
        with E.Writer(bio, meta={'o': object}) as w:
            w.write({'a': 1})
        self.assertEqual(segment_of(bio.getvalue())['o'], str(object))


class V1FixtureTest(_TmpCase):
    """Files that format v1's writer wrote (afcc5ab's lib/ebr.py, VERSION 1),
    kept in tests/data and read by this codec: v1.ebr (3 segments, raw frames:
    Writer(reset_bytes=3500, max_frame_records=5), flushed every 7 records)
    and v1-zlib.ebr (Writer(reset_bytes=3000, zlib=6): deflated frames and one
    raw), each of the 72 records of v1.jsonl, whose hazards are the codec's:
    big ints, NaN and infinities, -0.0, lone surrogates, MAP-encoded dynamic
    keys, legacy records with only `ts`, and ts_epoch missing, null, a string,
    a bool or a big int."""

    NAMES = ('v1.ebr', 'v1-zlib.ebr')

    def setUp(self):
        _TmpCase.setUp(self)
        self.lines = read_file(str(DATA / 'v1.jsonl'))
        self.want = [json.loads(x) for x in self.lines.splitlines()]

    def check(self, data, want):
        got, dec = decode(data, strict=True)
        self.assertEqual(len(got), len(want))
        self.assertTrue(all(deq(g, w) for (_p, g), w in zip(got, want)))
        st = dec.stats
        self.assertEqual(sum(st[k] for k in ('bad_frames', 'bad_records', 'lost_frames',
                                             'unsupported_frames', 'zstd_unavailable')), 0)
        rev = list(E.records_reverse(data, len(data)))
        self.assertEqual([p for p, _r in rev], [p for p, _r in got][::-1])
        self.assertTrue(all(deq(a[1], b[1]) for a, b in zip(rev, got[::-1])))
        for b in [0] + [f.end for f in E.frames(data)]:
            d = E.Decoder.at(data, b, strict=True)
            rest = []
            for f in E.frames(data, start=b):
                rest.extend(d.records(data, f))
            self.assertEqual([p for p, _r in rest], [p for p, _r in got if p >= b], b)
            self.assertTrue(all(deq(x[1], y[1]) for x, y in zip(rest, [g for g in got if g[0] >= b])))
        it = [r for _p, r in E.iter_records(io.BytesIO(data))]
        self.assertTrue(len(it) == len(want) and all(deq(a, b) for a, b in zip(it, want)))

    def versions(self, data):
        n = sum(1 for f in E.frames(data) if f.kind == E.SEGMENT)
        return [segment_of(data, i)['version'] for i in range(n)]

    def test_v1_files_read_exactly(self):
        for name in self.NAMES:
            data = read_file(str(DATA / name))
            self.assertEqual(self.versions(data), [1] * len(self.versions(data)), name)
            self.assertGreater(len(self.versions(data)), 1, name)
            self.assertEqual(segment_of(data)['zlib'], 6 if 'zlib' in name else 0)
            self.check(data, self.want)

    def test_v1_files_through_the_command_line(self):
        for name in self.NAMES:
            p = str(DATA / name)
            self.assertEqual(run_cli('cat', p)[:2], (0, self.lines), name)
            self.assertEqual(run_cli('count', p)[:2], (0, b'72\n'), name)
            self.assertEqual(run_cli('compare', str(DATA / 'v1.jsonl'), p)[0], 0, name)
            rc, out, err = run_cli('verify', '--strict', '--json', p)
            rep = json.loads(out.decode())['files'][0]
            self.assertEqual((rc, rep['records'], rep['decoded'], rep['problems']), (0, 72, 72, 0))
            self.assertEqual(rep['zlib_frames'] > 0, 'zlib' in name)
            rc, out, err = run_cli('frames', p)
            self.assertEqual(rc, 0)
            segs = [ln for ln in out.decode().splitlines() if ' SEGMENT ' in ln]
            self.assertTrue(segs and all(ln.endswith(' v1') for ln in segs), out)

    def append_to(self, name, zstd=0):
        """The fixture `name` appended to by this writer, five times, each a
        batch of records and a MARK."""
        p = self.path(name)
        shutil.copy(str(DATA / name), p)
        more = mixed_records(40, seed=21)
        for i in range(0, 40, 8):
            w = E.Writer.open_append(p, meta={'host': 'h', 'stream': 'more'}, zstd=zstd)
            for r in more[i:i + 8]:
                w.write(r)
            w.mark({'next': i + 8})
            w.close()
        return p, self.want + [oracle(r) for r in more]

    def test_a_v1_file_appended_to_reads_exactly(self):
        for name in self.NAMES:
            p, want = self.append_to(name)
            data = read_file(p)
            v = self.versions(data)
            self.assertEqual(v, [1] * (len(v) - 1) + [2], name)      # one v2 segment, resumed
            self.check(data, want)
            self.assertEqual(E.last_mark(p), ({'next': 40}, len(data), 0))
            jl = self.path(name + '.jsonl')
            with open(jl, 'w') as fh:
                fh.write(''.join(json.dumps(r) + '\n' for r in want))
            self.assertEqual(run_cli('cat', p)[:2], (0, read_file(jl)), name)
            self.assertEqual(run_cli('compare', jl, p)[0], 0, name)
            self.assertEqual(run_cli('count', p)[1], b'%d\n' % len(want))
            rc, out, err = run_cli('verify', '--strict', p)
            self.assertEqual(rc, 0, out + err)
            self.assertIn(b'5 marks', out)
            rc, out, err = run_cli('cat', '--tail', 3, p)
            self.assertEqual(out, b''.join(read_file(jl).splitlines(True)[-3:]))

    @NEEDS_ZSTD
    def test_a_v1_zlib_file_appended_to_with_zstd(self):
        p, want = self.append_to('v1-zlib.ebr', zstd=3)
        data = read_file(p)
        self.assertEqual(set(f.flags for f in data_frames(data)), {0, E.F_ZLIB, E.F_ZSTD})
        self.check(data, want)
        rc, out, err = run_cli('verify', '--strict', '--json', p)
        rep = json.loads(out.decode())['files'][0]
        self.assertEqual((rc, rep['problems'], rep['mark_frames']), (0, 0, 5))
        self.assertTrue(rep['zstd_frames'] and rep['zlib_frames'])


# ---------------------------------------------------------------------------
# MARK frames
# ---------------------------------------------------------------------------

class MarkTest(_TmpCase):
    """MARK (kind 3): a writer's checkpoint, flushed with the frames before
    it, and skipped by every reader of records."""

    def batches(self, n=6, size=7, **kw):
        """`n` batches of `size` records, each its records, a MARK, a flush."""
        recs = mixed_records(n * size, seed=31)
        bio = io.BytesIO()
        w = E.Writer(bio, **kw)
        for b in range(n):
            for r in recs[b * size:(b + 1) * size]:
                w.write(r)
            w.mark({'batch': b, 'next': (b + 1) * size})
            w.flush()
        w.close()
        return recs, bio.getvalue()

    def test_a_flush_is_one_write_of_the_records_and_their_mark(self):
        calls = []
        real = os.write

        def spy(fd, data):
            calls.append(bytes(data))
            return real(fd, data)
        recs = mixed_records(30, seed=2)
        with mock.patch.object(E.os, 'write', spy):
            w = E.Writer(self.path(), max_frame_records=4)
            for b in range(3):
                for r in recs[b * 10:(b + 1) * 10]:
                    w.write(r)
                w.mark({'b': b})
                self.assertEqual(w.flush(), len(calls[-1]))
            w.close()
        self.assertEqual(len(calls), 3)
        for b, blob in enumerate(calls):
            fl = list(E.frames(blob))
            self.assertEqual([f.kind for f in fl],
                             ([E.SEGMENT] if b == 0 else []) + [E.DATA] * 3 + [E.MARK])
            self.assertEqual(E.mark_info(blob, fl[-1]), {'b': b})
        self.assertEqual((w.stats['mark_frames'], w.stats['writes'], w.stats['frames']), (3, 3, 13))
        self.assertEqual(records_of(read_file(self.path())), [oracle(r) for r in recs])

    def test_its_header_and_the_positions_around_it(self):
        recs, data = self.batches(reset_bytes=3000)
        fl = list(E.frames(data))
        marks = [f for f in fl if f.kind == E.MARK]
        segs = [f.off for f in fl if f.kind == E.SEGMENT]
        self.assertEqual(len(marks), 6)
        self.assertGreater(len(segs), 1)
        for m in marks:
            self.assertEqual((m.flags, m.n_records, m.kinds), (0, 0, 0))
            self.assertTrue(math.isnan(m.ts_min) and math.isnan(m.ts_max))
            self.assertEqual(m.seg_off, max(s for s in segs if s < m.off))
        got = decode(data)[0]
        dframes = [f for f in fl if f.kind == E.DATA]
        for p, _r in got:
            self.assertTrue(any(f.off <= p < f.end for f in dframes), p)
        self.assertEqual(len(got), len(recs))
        self.assertTrue(all(deq(g, oracle(r)) for (_p, g), r in zip(got, recs)))

    def test_frames_reverse_is_frames_reversed(self):
        recs, data = self.batches(reset_bytes=3000)
        fwd = frame_list(data)
        self.assertIn(E.MARK, [k for _o, _e, k in fwd])
        for verify in (False, True):
            self.assertEqual([(f.off, f.end, f.kind)
                              for f in E.frames_reverse(data, len(data), verify=verify)], fwd[::-1])
        for f in E.frames(data):
            self.assertTrue(E.is_boundary(data, f.end))

    def test_decoder_at_either_side_of_a_mark(self):
        recs, data = self.batches(reset_bytes=3000)
        full = decode(data)[0]
        for m in [f for f in E.frames(data) if f.kind == E.MARK]:
            for b in (m.off, m.end):
                dec = E.Decoder.at(data, b, strict=True)
                got = []
                for g in E.frames(data, start=b):
                    got.extend(dec.records(data, g))
                self.assertEqual([p for p, _r in got], [p for p, _r in full if p >= b], b)
                self.assertFalse(dec.broken or dec.unsupported)

    def test_readers_of_records_never_see_one(self):
        recs, data = self.batches(reset_bytes=3000)
        plain = write_bytes(recs, flush_every=7, reset_bytes=3000)       # no MARKs
        for kinds, where in ((None, None), ({'exit'}, None), (None, {'user': {'u1'}}),
                             ({'conn', None}, {'proto': {'tcp'}}), ({'meta'}, None)):
            a = [r for _p, r in decode(data, kinds, where)[0]]
            b = [r for _p, r in decode(plain, kinds, where)[0]]
            self.assertTrue(deq(a, b), (kinds, where))
            rev = [r for _p, r in E.records_reverse(data, len(data), kinds=kinds, where=where)]
            self.assertTrue(deq(rev, a[::-1]), (kinds, where))
        it = [r for _p, r in E.iter_records(io.BytesIO(data))]
        self.assertTrue(deq(it, [oracle(r) for r in recs]))
        p = self.path()
        with open(p, 'wb') as fh:
            fh.write(data)
        lo, hi = 1794268810.0, 1794268830.0
        rc, out, _ = run_cli('cat', '--from', lo, '--to', hi, p)
        want = [json.dumps(oracle(r)) for r in recs if lo <= r['ts_epoch'] < hi]
        self.assertEqual((rc, out.decode().splitlines()), (0, want))
        self.assertEqual(run_cli('count', p)[1], b'%d\n' % len(recs))
        rc, out, _ = run_cli('cat', '--tail', 2, p)
        self.assertEqual(out.decode().splitlines(), [json.dumps(oracle(r)) for r in recs[-2:]])

    def test_open_append_resumes_across_marks(self):
        p = self.path()
        for i in range(4):
            w = E.Writer.open_append(p)
            w.write(exit_rec(i))
            w.mark({'i': i})
            w.close()
            self.assertEqual(w.stats['segments'], 1 if i == 0 else 0)
        w = E.Writer.open_append(p)
        w.mark({'i': 'a mark alone'})
        w.close()
        self.assertEqual(w.stats['segments'], 0)
        data = read_file(p)
        self.assertEqual([f.kind for f in E.frames(data)],
                         [E.SEGMENT] + [E.DATA, E.MARK] * 4 + [E.MARK])
        self.assertEqual(records_of(data), [oracle(exit_rec(i)) for i in range(4)])
        last = data_frames(data)[-1]
        payload = payload_of(data, last.off)
        sb, q = E._ruv(payload, 0)
        hb, q = E._ruv(payload, q)
        dl, q = E._ruv(payload, q)
        self.assertEqual(E._count_defs(payload[q:q + dl])[1], 0)    # the tables were resumed
        self.assertEqual(E.last_mark(p), ({'i': 'a mark alone'}, len(data), 0))

    def test_a_mark_first_in_a_segment(self):
        bio = io.BytesIO()
        w = E.Writer(bio)
        w.mark({'start': 0})
        self.assertEqual(w.flush(), len(bio.getvalue()))
        w.write({'a': 1})
        w.close()
        data = bio.getvalue()
        fl = list(E.frames(data))
        self.assertEqual([f.kind for f in fl], [E.SEGMENT, E.MARK, E.DATA])
        self.assertEqual((fl[1].seg_back, fl[2].seg_back), (fl[1].off, fl[2].off))
        self.assertEqual(records_of(data), [{'a': 1}])
        self.assertEqual(E.last_mark(data), ({'start': 0}, fl[1].end, 1))
        dec = E.Decoder.at(data, fl[1].end, strict=True)
        self.assertEqual([r for _p, r in dec.records(data, fl[2])], [{'a': 1}])

    def test_a_mark_when_the_segment_has_just_crossed_reset_bytes(self):
        # the frame before it takes the segment past reset_bytes: the MARK
        # opens the next segment rather than grow the full one
        bio = io.BytesIO()
        w = E.Writer(bio, reset_bytes=500)
        for i in range(5):
            w.write(exit_rec(i))
        w.mark({'m': 1})
        w.write(exit_rec(5))
        w.mark({'m': 2})
        w.close()
        data = bio.getvalue()
        fl = list(E.frames(data))
        self.assertEqual([f.kind for f in fl[:4]], [E.SEGMENT, E.DATA, E.SEGMENT, E.MARK])
        self.assertGreaterEqual(fl[1].end, 500)
        self.assertEqual(fl[3].seg_off, fl[2].off)
        self.assertEqual(records_of(data), [oracle(exit_rec(i)) for i in range(6)])
        self.assertEqual(E.last_mark(data), ({'m': 2}, len(data), 0))
        # a MARK that takes the segment past reset_bytes itself: the next record opens one
        bio = io.BytesIO()
        w = E.Writer(bio, reset_bytes=300)
        w.mark({'pad': 'x' * 400})
        w.write({'a': 1})
        w.close()
        data = bio.getvalue()
        fl = list(E.frames(data))
        self.assertEqual([f.kind for f in fl], [E.SEGMENT, E.MARK, E.SEGMENT, E.DATA])
        self.assertEqual(E.last_mark(data), ({'pad': 'x' * 400}, fl[1].end, 1))
        self.assertEqual(records_of(data), [{'a': 1}])

    def test_last_mark(self):
        self.assertEqual(E.last_mark(write_bytes(mixed_records(20), flush_every=5)), (None, 0, 20))
        self.assertEqual(E.last_mark(b''), (None, 0, 0))
        recs, data = self.batches()
        fl = list(E.frames(data))
        marks = [f for f in fl if f.kind == E.MARK]
        self.assertEqual(E.last_mark(data), ({'batch': 5, 'next': 42}, marks[-1].end, 0))
        for m, nxt in zip(marks, marks[1:]):
            mid = [f for f in fl if m.end <= f.off and f.end <= nxt.off and f.kind == E.DATA]
            self.assertEqual(E.last_mark(data, mid[-1].end),
                             (E.mark_info(data, m), m.end, sum(f.n_records for f in mid)))
            self.assertEqual(E.last_mark(data, nxt.off)[1:], (m.end, sum(f.n_records for f in mid)))
        # a torn tail: last_mark starts at complete_end
        self.assertEqual(E.last_mark(data + data[:30])[0], {'batch': 5, 'next': 42})
        # corrupt before the newest MARK (in the file): nothing behind it is read
        b = bytearray(data)
        b[[f for f in fl if f.kind == E.DATA][1].off + 60] ^= 0x40
        self.assertEqual(E.last_mark(bytes(b))[0], {'batch': 5, 'next': 42})
        # corrupt between the newest MARK and the end: what lies behind is unknown
        p = self.path()
        with open(p, 'wb') as fh:
            fh.write(data)
        w = E.Writer.open_append(p, max_frame_records=2)
        for r in mixed_records(6, seed=5):
            w.write(r)
        w.close()
        after = [f for f in E.frames(p) if f.off >= len(data) and f.kind == E.DATA]
        self.assertEqual(E.last_mark(p), ({'batch': 5, 'next': 42}, len(data), 6))
        b = bytearray(read_file(p))
        b[after[1].off + 60] ^= 0x40
        with self.assertRaises(E.FrameError):
            E.last_mark(bytes(b))

    def test_mark_info(self):
        recs, data = self.batches(n=2)
        fl = list(E.frames(data))
        m = [f for f in fl if f.kind == E.MARK][0]
        self.assertEqual(E.mark_info(data, m), {'batch': 0, 'next': 7})
        unverified = [f for f in E.frames(data, verify=False) if f.kind == E.MARK][0]
        self.assertIsNone(unverified.payload)
        self.assertEqual(E.mark_info(data, unverified), {'batch': 0, 'next': 7})
        with self.assertRaises(ValueError):
            E.mark_info(data, fl[0])                                  # a SEGMENT
        good = bytes(E._enc_map({'a': 1}))
        for payload, flags in ((good, E.F_ZSTD), (good, E.F_ZLIB), (b'\x01\x01a\x09', 0),
                               (good + b'x', 0), (b'', 0)):
            fb = E._frame(E.MARK, flags, payload, 0, NAN, NAN, 0, 0)
            with self.assertRaises(E.FrameError, msg=(payload, flags)):
                E.mark_info(fb, list(E.frames(fb))[0])

    def test_what_mark_refuses(self):
        w = E.Writer(io.BytesIO())
        with self.assertRaises(TypeError):
            w.mark(['not', 'a', 'dict'])
        with self.assertRaises(TypeError):
            w.mark({(1, 2): 'a tuple key'})
        loop = {}
        loop['l'] = loop
        with self.assertRaises(ValueError):
            w.mark(loop)
        with mock.patch.object(E, '_FRAME_ROOM', 100):
            with self.assertRaises(E.RecordTooLarge):
                w.mark({'pad': 'x' * 200})
        w.mark({'ok': 1})                       # nothing refused was queued
        w.close()
        with self.assertRaises(ValueError):
            w.mark({'late': 1})
        self.assertEqual(w.stats['mark_frames'], 1)


class MarkResumeTest(_TmpCase):
    """What a converter relies on to resume exactly once. A flush writes a
    batch's frames and its MARK in one os.write; after a crash anywhere in
    that write, and the repair of its torn tail at the next open, last_mark()
    names the newest MARK that survived and how many records of the next
    batch did, which is every record the file holds."""

    def session(self, p, src, batches, **kw):
        """One run of the converter: resume after the newest MARK, then a
        batch per entry of `batches` (a record count): its records, a MARK
        with the next source position, one flush. Returns where its last
        flush began."""
        w = E.Writer.open_append(p, **kw)       # repairs a torn tail
        try:
            info, _end, after = E.last_mark(p)
            pos = (info['next'] if info else 0) + after
            start = w.end
            for n in batches:
                for r in src[pos:pos + n]:
                    w.write(r)
                pos = min(len(src), pos + n)
                w.mark({'next': pos})
                start = w.end
                w.flush()
        finally:
            w.close()
        return start

    def test_every_cut_of_the_last_write(self):
        src = [exit_rec(i, args='a', cwd='/') if i % 3 else {'event': 'conn', 'i': i,
                                                             'ts_epoch': 5.0 + i}
               for i in range(40)]
        cases = (('three frames', [[5, 4], [1, 0], [7]], {'max_frame_records': 3},
                  [E.DATA, E.DATA, E.DATA, E.MARK]),
                 # the batch takes the segment past reset_bytes: its MARK opens the next
                 ('a segment inside', [[2], [4]], {'reset_bytes': 900},
                  [E.DATA, E.SEGMENT, E.MARK]),
                 # after a writer that never marked, past reset_bytes: not resumed
                 ('a new segment', [None, [3]], {'reset_bytes': 900},
                  [E.SEGMENT, E.DATA, E.MARK]),
                 ('a mark alone', [[10, 0]], {}, [E.MARK]),
                 ('the first write', [[2]], {}, [E.SEGMENT, E.DATA, E.MARK]))
        for name, sessions, kw, shape in cases:
            p0 = self.path(name.replace(' ', '-') + '.ebr')
            for batches in sessions:
                if batches is None:
                    with E.Writer(p0, **kw) as w:
                        for r in src[:12]:
                            w.write(r)
                    continue
                last = self.session(p0, src, batches, **kw)
            data = read_file(p0)
            fl = list(E.frames(data, start=last))
            self.assertEqual([f.kind for f in fl], shape, name)
            ends = set(f.end for f in fl) | set(f.off for f in fl)
            for k in range(last, len(data) + 1):
                p = self.path('cut.ebr')
                with open(p, 'wb') as fh:
                    fh.write(data[:k])
                E.Writer.open_append(p, **kw).close()                  # the repair
                self.assertEqual(os.path.getsize(p), E.complete_end(data[:k]), (name, k))
                info, end, after = E.last_mark(p)
                n = (info['next'] if info else 0) + after
                got = records_of(read_file(p))
                self.assertEqual(len(got), n, (name, k))
                self.assertTrue(all(deq(g, oracle(r)) for g, r in zip(got, src)), (name, k))
                self.assertEqual(info is None, end == 0)
                self.assertLessEqual(end, os.path.getsize(p))
                if k in ends or k % 13 == 0:
                    # the converter's next run: every source record, once
                    self.session(p, src, [len(src)], **kw)
                    got = records_of(read_file(p))
                    self.assertEqual(len(got), len(src), (name, k))
                    self.assertTrue(all(deq(g, oracle(r)) for g, r in zip(got, src)), (name, k))
                    self.assertEqual(E.last_mark(p), ({'next': len(src)}, os.path.getsize(p), 0))


# ---------------------------------------------------------------------------
# zstd
# ---------------------------------------------------------------------------

def fake_zstd(compress):
    """A stand-in zstd module for the writer, compressing with `compress`."""
    z = E._Zstd()
    z.name = 'fake'
    z.compressor = lambda level: compress
    z.decompressor = None
    return z


class ZstdTest(_TmpCase):
    """F_ZSTD frames written and read with every zstd module this Python has."""

    def backends(self):
        bs = zstd_backends()
        if not bs:
            self.skipTest('no zstd module in this Python')
        return bs

    def test_every_module_reads_what_every_module_writes(self):
        recs = mixed_records(200, seed=51) + [dict(('k%04d' % i, 'v' * (i % 7)) for i in range(5000))]
        bs = self.backends()
        for wname, wz in bs:
            with mock.patch.object(E, '_ZSTD', wz):
                bio = io.BytesIO()
                w = E.Writer(bio, zstd=3)
                for i, r in enumerate(recs):
                    w.write(r)
                    if i % 25 == 24:
                        w.flush()
                w.close()
                data = bio.getvalue()
            flags = [f.flags for f in data_frames(data)]
            self.assertEqual(flags, [E.F_ZSTD] * len(flags), wname)
            self.assertEqual(w.stats['zstd_frames'], len(flags))
            for rname, rz in bs:
                with self.subTest(written=wname, read=rname), mock.patch.object(E, '_ZSTD', rz):
                    got, dec = decode(data, strict=True)
                    self.assertEqual(len(got), len(recs))
                    self.assertTrue(all(deq(g, oracle(r)) for (_p, g), r in zip(got, recs)))
                    rev = list(E.records_reverse(data, len(data)))
                    self.assertEqual([p for p, _r in rev], [p for p, _r in got][::-1])
                    for f in data_frames(data)[::3]:
                        d = E.Decoder.at(data, f.off, strict=True)
                        self.assertEqual([p for p, _r in d.records(data, f)],
                                         [p for p, _r in got if f.off <= p < f.end])

    @NEEDS_ZSTD
    def test_mixed_raw_and_zstd_frames(self):
        recs = [exit_rec(i) for i in range(40)] + [{'tiny': i} for i in range(3)]
        bio = io.BytesIO()
        w = E.Writer(bio, zstd=3)
        for i, r in enumerate(recs):
            w.write(r)
            if i in (19, 39, 40, 41):
                w.flush()
        w.close()
        data = bio.getvalue()
        flags = [f.flags for f in data_frames(data)]
        self.assertEqual(flags, [E.F_ZSTD, E.F_ZSTD, 0, 0, 0])   # zstd would grow a tiny frame
        self.assertEqual(w.stats['zstd_frames'], 2)
        self.assertTrue(all(deq(g, oracle(r)) for g, r in zip(records_of(data), recs)))
        self.assertEqual(len(records_of(data)), len(recs))
        self.assertLess(len(data), len(write_bytes(recs, flush_every=20)))
        for f in data_frames(data):
            if f.flags:
                n = E._ruv(payload_of(data, f.off), 0)[0]
                self.assertGreater(n + 1, f.payload_len)            # kept only when smaller
        # a frame of noise, which zstd cannot shrink: big ints of random bits
        rng = random.Random(8)
        noise = [{'a': rng.getrandbits(4000), 'b': -rng.getrandbits(4000)} for _ in range(20)]
        data = write_bytes(noise, zstd=19)
        self.assertEqual([f.flags for f in data_frames(data)], [0])
        self.assertEqual(len(records_of(data)), 20)
        self.assertTrue(all(deq(g, oracle(r)) for g, r in zip(records_of(data), noise)))

    @NEEDS_ZSTD
    def test_open_append_resumes_a_zstd_segment_only_where_it_can_read_it(self):
        p = self.path()
        for i in range(3):
            with E.Writer.open_append(p, zstd=3) as w:
                for j in range(6):
                    w.write(exit_rec(i * 6 + j))
                w.mark({'i': i})
        segs = [f for f in E.frames(p) if f.kind == E.SEGMENT]
        self.assertEqual(len(segs), 1)                          # resumed, through zstd frames
        self.assertEqual(set(f.flags for f in data_frames(read_file(p))), {E.F_ZSTD})
        with no_zstd():                                         # its tables are inside them
            with E.Writer.open_append(p) as w:
                w.write(exit_rec(18))
            self.assertEqual(w.stats['segments'], 1)
            got, dec = decode(read_file(p))
            self.assertEqual([r for _p, r in got], [oracle(exit_rec(18))])
            self.assertEqual(dec.stats['zstd_unavailable'], 3)
            marks = [f for f in E.frames(p) if f.kind == E.MARK]
            self.assertEqual(E.last_mark(p), ({'i': 2}, marks[-1].end, 1))   # headers only
        self.assertEqual(records_of(read_file(p)), [oracle(exit_rec(i)) for i in range(19)])

    @NEEDS_ZSTD
    def test_levels(self):
        recs = [exit_rec(i) for i in range(30)]
        sizes = {}
        for z in (1, 3, 9, 19, 22, True):
            bio = io.BytesIO()
            w = E.Writer(bio, zstd=z)
            for r in recs:
                w.write(r)
            w.close()
            self.assertEqual(w.zstd, 3 if z is True else z)
            self.assertEqual(records_of(bio.getvalue()), [oracle(r) for r in recs], z)
            sizes[z] = len(bio.getvalue())
        self.assertLess(sizes[19], len(write_bytes(recs)))

    def test_a_frame_zstd_would_not_shrink_is_stored_as_is(self):
        recs = [exit_rec(i) for i in range(10)]
        for compress in (lambda data: b'z' * len(data), lambda data: b'z' * (len(data) - 1),
                         lambda data: 1 // 0):
            with mock.patch.object(E, '_ZSTD', fake_zstd(compress)):
                bio = io.BytesIO()
                w = E.Writer(bio, zstd=5)
                for r in recs:
                    w.write(r)
                w.close()
            data = bio.getvalue()
            # len(payload) - 1 bytes of frame, plus raw_len, is not smaller either
            self.assertEqual([f.flags for f in E.frames(data)], [0, 0])
            self.assertEqual(w.stats['zstd_frames'], 0)
            self.assertEqual(records_of(data), [oracle(r) for r in recs])

    def test_no_module_no_zstd_writer(self):
        with no_zstd():
            self.assertFalse(E.zstd_available())
            self.assertIsNone(E.zstd_backend())
            with self.assertRaises(E.Error) as cm:
                E.Writer(io.BytesIO(), zstd=3)
            for s in ('zstd', '3.14', 'zstandard', 'pyzstd'):
                self.assertIn(s, str(cm.exception))
            p = self.path()
            with self.assertRaises(E.Error):
                E.Writer(p, zstd=True)
            self.assertFalse(os.path.exists(p))
            with self.assertRaises(E.Error):
                E.Writer.open_append(p, zstd=1)
            E.Writer(io.BytesIO(), zstd=0).close()

    def test_segments_and_marks_are_never_compressed(self):
        calls = []

        def compress(data):
            calls.append(data)
            return b''
        with mock.patch.object(E, '_ZSTD', fake_zstd(compress)):
            bio = io.BytesIO()
            w = E.Writer(bio, zstd=3, meta={'host': 'h' * 500})
            w.mark({'m': 'x' * 500})
            w.write({'a': 'y' * 500})
            w.mark({'m': 'z' * 500})
            w.close()
        data = bio.getvalue()
        self.assertEqual(len(calls), 1)                               # the DATA frame
        self.assertEqual([(f.kind, f.flags) for f in E.frames(data)],
                         [(E.SEGMENT, 0), (E.MARK, 0), (E.DATA, E.F_ZSTD), (E.MARK, 0)])

    @NEEDS_ZSTD
    def test_a_compressed_segment_reads_where_its_flags_do(self):
        # no writer compresses a SEGMENT; a reader takes its flags as any frame's
        data = write_bytes([{'a': 1}])
        segp = payload_of(data, 0)
        z = E._zstd().compressor(3)(segp + b'\x00' * 200)
        zp = E._uv(len(segp) + 200) + z
        forged = rewrite_header(rewrite_payload(data, 0, zp), 0, flags=E.F_ZSTD)
        got, dec = decode(forged, strict=True)
        self.assertEqual([r for _p, r in got], [{'a': 1}])
        with no_zstd():
            got, dec = decode(forged)
        self.assertEqual(got, [])
        st = dec.stats
        self.assertEqual((st['unsupported_frames'], st['zstd_unavailable'], st['bad_frames']),
                         (1, 1, 0))


class ZstdWithoutAModuleTest(_TmpCase):
    """A reader with no zstd module meets an F_ZSTD frame as an unknown flag:
    the segment is skipped and counted to the next SEGMENT (the frame defines
    strings and shapes the rest of it uses), never BAD; headers still read.
    The frames here are flagged zstd and never looked inside, so this runs
    without a module."""

    def sample(self):
        """Two segments: the first's frames 1 and 3 (of 0-4) flagged zstd."""
        a = write_bytes([exit_rec(i) for i in range(15)], flush_every=3)
        b = write_bytes([exit_rec(i) for i in range(15, 18)])
        for f in (data_frames(a)[1], data_frames(a)[3]):
            a = rewrite_header(a, f.off, flags=E.F_ZSTD)
        return a + b

    def test_skipped_to_the_next_segment_never_bad(self):
        data = self.sample()
        want = [740000 + i for i in (0, 1, 2, 15, 16, 17)]
        with no_zstd():
            for strict in (False, True):
                got, dec = decode(data, strict=strict)
                self.assertEqual([r['pid'] for _p, r in got], want)
                st = dec.stats
                self.assertEqual((st['unsupported_frames'], st['zstd_unavailable'], st['bad_frames'],
                                  st['lost_records'], st['lost_frames'], st['bad_records']),
                                 (4, 2, 0, 0, 0, 0))
            st = {}
            rev = list(E.records_reverse(data, len(data), stats=st))
            self.assertEqual([r['pid'] for _p, r in rev], want[::-1])
            self.assertEqual((st['bad_frames'], st['zstd_unavailable']), (0, 2))
            fl = data_frames(data)
            dec = E.Decoder.at(data, fl[2].off)            # replays the zstd frame before it
            self.assertTrue(dec.unsupported and not dec.broken)
            self.assertEqual(list(dec.records(data, fl[2])), [])
            it = [r['pid'] for _p, r in E.iter_records(io.BytesIO(data))]
            self.assertEqual(it, want)

    def test_headers_read_without_it(self):
        data = self.sample()
        plain = write_bytes([exit_rec(i) for i in range(15)], flush_every=3) + \
            write_bytes([exit_rec(i) for i in range(15, 18)])
        with no_zstd():
            def heads(d):
                return [(f.off, f.end, f.kind, f.n_records, repr(f.ts_min), repr(f.ts_max),
                         f.kinds, f.seg_back) for f in E.frames(d)]
            self.assertEqual(heads(data), heads(plain))
            self.assertEqual([(f.off, f.kind) for f in E.frames_reverse(data, len(data))],
                             [(f.off, f.kind) for f in E.frames(data)][::-1])
            self.assertEqual(E.complete_end(data), len(data))
            self.assertEqual(E.last_mark(data), (None, 0, 18))
        p = self.path()
        with open(p, 'wb') as fh:
            fh.write(data)
        self.assertEqual(run_cli('count', p, zstd=False)[:2], (0, b'18\n'))
        rc, out, err = run_cli('frames', p, zstd=False)
        self.assertEqual(rc, 0, err)
        rc, out, err = run_cli('verify', p, zstd=False)
        self.assertEqual(rc, 1)
        self.assertIn(b'zstd_unavailable: 2 zstd frames not read, not corrupt', out)
        self.assertIn(b'pip install zstandard', out)
        self.assertNotIn(b'BAD', out)
        rc, out, err = run_cli('verify', '--json', p, zstd=False)
        rep = json.loads(out.decode())['files'][0]
        self.assertEqual((rep['zstd_frames'], rep['zstd_unavailable'], rep['bad_frames'],
                          rep['unsupported_frames'], rep['records'], rep['decoded']),
                         (2, 2, 0, 4, 18, 6))
        rc, out, err = run_cli('cat', '--kind', 'exit', p, zstd=False)
        self.assertEqual((rc, len(out.splitlines())), (1, 6))
        self.assertIn(b'zstd_unavailable=2', err)

    @NEEDS_ZSTD
    def test_with_a_module_a_frame_that_is_not_zstd_is_bad(self):
        got, dec = decode(self.sample())
        self.assertEqual(dec.stats['bad_frames'], 1)
        self.assertEqual(dec.stats['zstd_unavailable'], 0)


class ZstdBombTest(unittest.TestCase):
    """What an F_ZSTD payload must be: raw_len (1..64 MiB), then exactly one
    zstd frame of exactly raw_len bytes; anything else is a BAD frame, found
    without producing more than raw_len + 1 bytes."""

    def setUp(self):
        self.bs = zstd_backends()
        if not self.bs:
            self.skipTest('no zstd module in this Python')
        self.data = write_bytes([exit_rec(i) for i in range(8)])
        self.f = data_frames(self.data)[0]
        self.raw = payload_of(self.data, self.f.off)

    def forge(self, body, flags=E.F_ZSTD):
        d = rewrite_payload(self.data, self.f.off, body)
        return rewrite_header(d, self.f.off, flags=flags)

    def test_good_frames_of_every_kind_read(self):
        raw = self.raw
        n = len(raw)
        for name, z in self.bs:
            with mock.patch.object(E, '_ZSTD', z):
                frames = [z.compressor(3)(raw), zstd_frame(raw), zstd_frame(raw, fcs_flag=3),
                          zstd_frame(raw, single=False, fcs_flag=0),        # size unknown
                          zstd_frame(raw, block=97)]                         # many blocks
                try:
                    import zstandard
                    frames.append(zstandard.ZstdCompressor(level=3, write_checksum=True).compress(raw))
                    frames.append(zstandard.ZstdCompressor(level=3, write_content_size=False)
                                  .compress(raw))
                except ImportError:
                    pass
                for fr in frames:
                    got, dec = decode(self.forge(E._uv(n) + fr), strict=True)
                    self.assertEqual(len(got), 8, (name, fr[:8]))

    def test_bombs_and_malformed_frames_are_bad(self):
        raw = self.raw
        n = len(raw)
        for name, z in self.bs:
            good = z.compressor(3)(raw)
            more = raw + b'\x00' * 100
            lying = bytearray(zstd_frame(more, fcs_flag=2))
            lying[5:9] = struct.pack('<I', n)                    # says n, holds n + 100
            skippable = b'\x50\x2a\x4d\x18' + struct.pack('<I', 3) + b'abc'
            cases = {
                'raw_len past 64 MiB': E._uv(E.MAX_PAYLOAD + 1) + good,
                'raw_len 2**60': E._uv(2 ** 60) + good,
                'raw_len one short': E._uv(n - 1) + good,
                'raw_len one long': E._uv(n + 1) + good,
                'raw_len 0': E._uv(0) + good,
                'forged content size': E._uv(n) + zstd_frame(raw, fcs_flag=3)[:5]
                + struct.pack('<Q', 1 << 40) + zstd_frame(raw, fcs_flag=3)[13:],
                'content size that lies': E._uv(n) + bytes(lying),
                'size unknown, holds more': E._uv(n) + zstd_frame(more, single=False, fcs_flag=0),
                'trailing garbage': E._uv(n) + good + b'junk',
                'two frames': E._uv(n) + good + good,
                'two frames, raw_len of both': E._uv(2 * n) + good + good,
                'a skippable frame first': E._uv(n) + skippable + good,
                'a skippable frame alone': E._uv(n) + skippable,
                'not zstd': E._uv(n) + raw,
                'empty': b'',
                'raw_len alone': E._uv(n),
                'reserved bit': E._uv(n) + good[:4] + bytes((good[4] | 0x08,)) + good[5:],
            }
            for k in (4, 5, 6, len(good) // 2, len(good) - 1):
                cases['truncated to %d' % k] = E._uv(n) + good[:k]
            for what, body in cases.items():
                with self.subTest(module=name, case=what), mock.patch.object(E, '_ZSTD', z):
                    got, dec = decode(self.forge(body))
                    self.assertEqual(got, [])
                    self.assertEqual((dec.stats['bad_frames'], dec.stats['lost_records'],
                                      dec.stats['zstd_unavailable']), (1, 8, 0))
                    with self.assertRaises(E.FrameError) as cm:
                        decode(self.forge(body), strict=True)
                    if what == 'forged content size':          # refused before decompressing
                        self.assertIn('a zstd frame of %d bytes, not %d' % (1 << 40, n),
                                      str(cm.exception))

    def test_flag_combinations(self):
        good = E._uv(len(self.raw)) + self.bs[0][1].compressor(3)(self.raw)
        with mock.patch.object(E, '_ZSTD', self.bs[0][1]):
            for flags, records, unsupported in ((E.F_ZSTD, 8, 0), (0, 0, 0),
                                                (E.F_ZSTD | E.F_ZLIB, 0, 1), (E.F_ZSTD | 4, 0, 1)):
                got, dec = decode(self.forge(good, flags))
                self.assertEqual((len(got), dec.stats['unsupported_frames']),
                                 (records, unsupported), flags)
                self.assertEqual(dec.stats['bad_frames'], 1 if flags == 0 else 0, flags)
            got, dec = decode(self.forge(zlib.compress(self.raw), E.F_ZLIB))
            self.assertEqual(len(got), 8)                       # zlib still reads beside it


class ZstdFrameWalkTest(unittest.TestCase):
    """_zstd_frame_end: where a zstd frame ends, from its headers alone."""

    def test_frames_built_by_hand(self):
        rng = random.Random(3)
        for data in (b'', b'x', b'y' * 255, b'z' * 256, b'w' * 65791, b'v' * 65792,
                     bytes(rng.getrandbits(8) for _ in range(140000))):
            for kw in ({}, {'single': False, 'fcs_flag': 0}, {'checksum': True}, {'fcs_flag': 3},
                       {'single': False, 'fcs_flag': 2, 'block': 1000}):
                fr = zstd_frame(data, **kw)
                self.assertEqual(E._zstd_frame_end(fr), len(fr), (len(data), kw))
                self.assertEqual(E._zstd_frame_end(fr + b'junk'), len(fr))
                self.assertEqual(E._zstd_frame_end(b'pre' + fr, 3), 3 + len(fr))
                self.assertEqual(E._zstd_frame_end(memoryview(fr)), len(fr))
                for k in sorted(set((0, 3, 4, 5, 6, 7, len(fr) // 2, len(fr) - 1))):
                    if 0 <= k < len(fr):
                        with self.assertRaises(E.FrameError, msg=(len(data), kw, k)):
                            E._zstd_frame_end(fr[:k])
        rle = zstd_frame(b'\x00' * 1000, rle=True)
        self.assertEqual(E._zstd_frame_end(rle), len(rle))
        good = zstd_frame(b'abc')
        with self.assertRaises(E.FrameError):
            E._zstd_frame_end(good[:4] + bytes((good[4] | 0x08,)) + good[5:])   # reserved bit
        with self.assertRaises(E.FrameError):
            E._zstd_frame_end(good[:6] + bytes((good[6] | 0x06,)) + good[7:])   # block type 3
        with self.assertRaises(E.FrameError):
            E._zstd_frame_end(b'\x50\x2a\x4d\x18' + struct.pack('<I', 0))       # skippable
        for name, z in zstd_backends():
            for kw in ({}, {'single': False, 'fcs_flag': 0}, {'fcs_flag': 3}, {'block': 7}):
                fr = zstd_frame(b'abc' * 100, **kw)
                self.assertEqual(z.decompressor()(fr, 300), b'abc' * 100, (name, kw))
            self.assertEqual(z.decompressor()(rle, 1000), b'\x00' * 1000, name)

    def test_frames_of_every_module(self):
        rng = random.Random(4)
        noise = bytes(rng.getrandbits(8) for _ in range(300000))
        texts = (b'', b'a', b'hello ' * 10, noise[:5000], b'\x00' * 400000,
                 b''.join(b'%d,' % i for i in range(120000)), noise)
        for name, z in zstd_backends():
            for level in (1, 3, 19):
                c = z.compressor(level)
                for t in texts:
                    fr = c(t)
                    self.assertEqual(E._zstd_frame_end(fr), len(fr), (name, level, len(t)))
                    if t:
                        self.assertEqual(bytes(z.decompressor()(fr, len(t))), t, (name, len(t)))
        try:
            import zstandard
        except ImportError:
            return
        for kw in ({'write_checksum': True}, {'write_content_size': False},
                   {'write_checksum': True, 'write_content_size': False}):
            fr = zstandard.ZstdCompressor(level=3, **kw).compress(texts[5])
            self.assertEqual(E._zstd_frame_end(fr), len(fr), kw)


class ZstdShimTest(unittest.TestCase):
    """The zstd module is found on first use, in order, and only then."""

    def test_nothing_is_imported_until_zstd_is_asked_for(self):
        code = ('import io, sys; sys.path.insert(0, sys.argv[1]); import ebr\n'
                'mods = ("zstandard", "pyzstd", "compression.zstd")\n'
                'b = io.BytesIO(); w = ebr.Writer(b); w.write({"a": 1}); w.mark({"m": 1}); w.close()\n'
                'list(ebr.iter_records(io.BytesIO(b.getvalue())))\n'
                'print([m for m in mods if m in sys.modules], ebr._ZSTD)\n'
                'print(ebr.zstd_available() == (ebr._ZSTD is not False))\n')
        r = subprocess.run([sys.executable, '-c', code, str(_ROOT / 'lib')],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.splitlines(), ['[] None', 'True'])

    def test_the_order_and_what_is_skipped(self):
        self.assertIs(E._zstd_load(()), False)
        self.assertIs(E._zstd_load(('lz4',)), False)
        # (`from compression import zstd` finds an imported zstd on the package)
        hidden = {'zstandard': None, 'pyzstd': None, 'compression': None, 'compression.zstd': None}
        with mock.patch.dict(sys.modules, hidden):
            self.assertIs(E._zstd_load(), False)
            with mock.patch.object(E, '_ZSTD', None):
                self.assertFalse(E.zstd_available())
                self.assertIs(E._ZSTD, False)                   # looked up once
        # a module of that name that does not speak the API: skipped
        odd = type(sys)('zstandard')
        odd.__version__ = '0.0'
        odd.ZstdCompressor = odd.ZstdDecompressor = odd.ZstdError = Exception
        odd.frame_content_size = len
        with mock.patch.dict(sys.modules, {'zstandard': odd}):
            self.assertIs(E._zstd_load(('zstandard',)), False)
        bs = zstd_backends()
        if bs:
            self.assertEqual(E._zstd_load().name, bs[0][1].name)
            self.assertTrue(bs[0][1].name.startswith(bs[0][0]))
            with mock.patch.object(E, '_ZSTD', bs[-1][1]):
                self.assertEqual(E.zstd_backend(), bs[-1][1].name)


class FilterTest(unittest.TestCase):
    EVENTS = ['exit', 'conn', 'residency', None, 'weird', 5, ['exit'], 'MISSING']

    def corpus(self):
        rng = random.Random(7)
        recs = []
        for i in range(400):
            r = {}
            ev = rng.choice(self.EVENTS)
            if ev != 'MISSING':
                r['event'] = ev
            r['ts_epoch'] = 1000.0 + i
            if rng.random() < 0.8:
                r['user'] = rng.choice(['alice', 'bob', None, 'carol', 7, True])
            r['n'] = rng.choice([1, 3, 7, 2 ** 70, 1.5, None, False])
            r['cwd'] = rng.choice(['/home/a/x', '/home/ab', '/scratch/a', None, 3])
            if rng.random() < 0.3:
                r['obj'] = rng.choice([{'a': 1}, [1, 2], 'str', None])
            recs.append(r)
        recs.append(dict(('w%04d' % i, i) for i in range(4097)))      # a MAP record
        recs[-1]['event'] = 'exit'
        recs[-1]['user'] = 'alice'
        return recs

    def test_filters_equal_post_decode_filtering(self):
        recs = self.corpus()
        data = write_bytes(recs, flush_every=9, reset_bytes=8000)
        allp = decode(data)[0]
        kinds_cases = [None, {'exit'}, {'exit', None}, {None}, {'weird'}, {5}, set(),
                       frozenset(['conn', 'residency'])]
        where_cases = [None, {'user': {'alice'}}, {'user': {'None'}}, {'user': {'True', '7'}},
                       [{'user': {'alice'}}, {'n': {'3'}}], {'cwd': E.Prefix('/home/a')},
                       {'n': lambda v: isinstance(v, int) and not isinstance(v, bool) and v > 5},
                       {'obj': lambda v: isinstance(v, dict)}, {'n': {str(2 ** 70)}},
                       [], {}, {'absent': {'None'}}, {'absent': {'x'}},
                       {'user': {'alice', 'bob'}, 'cwd': E.Prefix('/home')},
                       [{'event': {'exit'}}, {'obj': {'str'}}], {'w0001': {'1'}}]
        for kinds in kinds_cases:
            for where in where_cases:
                got, dec = decode(data, kinds, where)
                want = reference_filter(allp, kinds, where)
                self.assertEqual([p for p, _r in got], [p for p, _r in want], (kinds, where))
                self.assertTrue(all(deq(a[1], b[1]) for a, b in zip(got, want)))
                self.assertEqual(dec.stats['records'] + dec.stats['filtered'], len(allp),
                                 (kinds, where))

    def test_frames_skipped_by_mask(self):
        recs = [{'event': 'exit', 'i': i} for i in range(10)] + [{'event': 'conn'}]
        data = write_bytes(recs, flush_every=10)
        got, dec = decode(data, kinds={'conn'})
        self.assertEqual([r for _p, r in got], [{'event': 'conn'}])
        self.assertEqual(dec.stats['filtered'], 10)

    def test_match_where_reference(self):
        rec = {'user': 'alice', 'n': 3, 'b': True, 'x': None}
        self.assertTrue(E.match_where(None, rec))
        self.assertTrue(E.match_where({}, rec))
        self.assertFalse(E.match_where([], rec))
        self.assertTrue(E.match_where({'user': {'alice'}}, rec))
        self.assertTrue(E.match_where({'missing': {'None'}}, rec))
        self.assertTrue(E.match_where({'x': {'None'}}, rec))
        self.assertTrue(E.match_where({'b': {'True'}}, rec))
        self.assertFalse(E.match_where({'n': E.Prefix('3')}, rec))
        self.assertTrue(E.match_where({'user': E.Prefix('al')}, rec))
        self.assertTrue(E.match_where([{'user': {'bob'}}, {'n': lambda v: v == 3}], rec))
        with self.assertRaises(TypeError):
            E.match_where({'user': 'alice'}, rec)
        with self.assertRaises(TypeError):
            decode(write_bytes([rec]), where={'user': 'alice'})

    def test_jsonl_and_ebr_filter_alike(self):
        recs = self.corpus()[:200]
        jl = ''.join(json.dumps(r) + '\n' for r in recs).encode()
        data = write_bytes(recs)
        for kinds, where in ((None, {'user': {'alice'}}), ({'exit'}, None),
                             ({'conn', None}, {'cwd': E.Prefix('/home')})):
            a = [r for _p, r in E.iter_records(io.BytesIO(jl), kinds, where)]
            b = [r for _p, r in E.iter_records(io.BytesIO(data), kinds, where)]
            self.assertTrue(len(a) == len(b) and all(deq(x, y) for x, y in zip(a, b)))


class InjectionTest(unittest.TestCase):
    KEYS = ['"}),__import__(\'os\')#', 'K[0]', 'line\nbreak', "'''", '"""', '\\', 'a = 1',
            'zz_unique_key_xyz', 'def', 'return', ')', '{', 'S[a[0]]', 'b"".join']

    def test_keys_never_reach_the_generated_source(self):
        sources = []
        real = compile

        def spy(src, name, mode, *a, **kw):
            sources.append(src)
            return real(src, name, mode, *a, **kw)
        E._ENC_CACHE.clear()
        E._DEC_CACHE.clear()
        E.compile = spy
        try:
            rec = dict((k, i) for i, k in enumerate(self.KEYS))
            rec.update({'s' + k: k for k in self.KEYS})
            rec['nested'] = dict((k, {k: [k]}) for k in self.KEYS)
            wide = dict(('%s%d' % (self.KEYS[i % len(self.KEYS)], i), 'v') for i in range(40))
            recs = [rec, wide, {'o': wide}]
            got = records_of(write_bytes(recs))
        finally:
            del E.compile
        self.assertEqual(got, recs)
        self.assertGreater(len(sources), 3)
        allowed = re.compile(r'[A-Za-z0-9_ ,.():\[\]{}=*"\n+-]*\Z')
        for src in sources:
            self.assertTrue(allowed.match(src), src)
            for k in ('__import__', 'zz_unique', "'''", '\\', 'line'):
                self.assertNotIn(k, src)

    def forged(self, defs):
        seg = write_bytes([{'a': 1}])
        seg_frames = list(E.frames(seg))
        s = seg[:seg_frames[0].end]
        payload = E._uv(0) + E._uv(0) + E._uv(len(defs)) + defs + E._uv(1) + b'\x01'
        d = E._frame(E.DATA, 0, payload, 1, 1.0, 1.0, len(s), 1)
        return s + d

    def test_forged_definitions_are_bad_and_compile_nothing(self):
        cases = [b'\x01\x01a\x02\x01\x00\x0b',                 # slot type 11
                 b'\x01\x01a\x02\x01\x00\xff',                 # slot type 255
                 b'\x01\x01a\x02\xc1\x40' + b'\x00\x01' * 3,   # 8257 fields
                 b'\x03\x01a',                                 # opcode 3
                 b'\x01\x01a\x02\x01\x05\x01',                 # key id never defined
                 b'\x01\x01a\x02\x02\x00\x01\x00\x01',         # a repeated key
                 b'\x01\x05ab']                                # string past the defs
        E._DEC_CACHE.clear()
        sources = []
        E.compile = lambda *a, **kw: sources.append(a) or compile(*a, **kw)
        try:
            for defs in cases:
                got, dec = decode(self.forged(defs))
                self.assertEqual(got, [], defs)
                self.assertEqual(dec.stats['bad_frames'], 1, defs)
        finally:
            del E.compile
        self.assertEqual(sources, [])
        self.assertEqual(len(E._DEC_CACHE), 0)

    def test_a_forged_record_is_a_bad_record(self):
        data = write_bytes([{'a': 1}, {'a': 2}])
        f = data_frames(data)[0]
        payload = bytearray(payload_of(data, f.off))
        payload[-1] = 0x7f                               # the last record's value byte
        payload[-3] = 0x09                               # its shape id: undefined
        got, dec = decode(rewrite_payload(data, f.off, bytes(payload)))
        self.assertEqual([r for _p, r in got], [{'a': 1}])
        self.assertEqual(dec.stats['bad_records'], 1)


# ---------------------------------------------------------------------------
# the read paths that skip work
# ---------------------------------------------------------------------------

def damage(data, rng):
    """`data` with random damage: bytes changed, zeroed, cut out or put in."""
    b = bytearray(data)
    k = rng.random()
    i = rng.randrange(len(b))
    if k < 0.5:
        for _ in range(rng.randrange(1, 4)):
            b[rng.randrange(len(b))] = rng.randrange(256)
    elif k < 0.65:
        b[i:i + rng.randrange(1, 200)] = bytes(200)[:len(b[i:i + 200])]
    elif k < 0.8:
        del b[i:i + rng.randrange(1, 200)]
    else:
        b[i:i] = bytes(rng.randrange(256) for _ in range(rng.randrange(1, 60)))
    return bytes(b)


def decoder_state(d):
    T = d._T
    return (d.broken, d.unsupported, d.next_off, d.segment, dict(d.stats),
            None if T is None else (list(T.S), [(sh.ids, sh.layout) for sh in T.shapes]))


class ReadPathTest(unittest.TestCase):
    """What skips work gives what the general paths give: a decoder placed by
    `_walk`, the frames a settled end checks in one loop (`verify_back`), a
    frame whose header says every record is wanted, a dict decoded inline as
    the shape its slot held first, and a source shared and read ahead."""

    def reference_at(self, data, off):
        """Decoder.at as it was: frames() and skip() from the segment's start."""
        d = E.Decoder()
        if off == 0:
            return d
        f = next(E.frames_reverse(data, off))
        so = f.off - f.seg_back
        h = E.peek(data, so)
        if h is None or h.kind != E.SEGMENT:
            d._set_broken()
            d.next_off = off
            return d
        for g in E.frames(data, so, off, True):
            d.skip(g, data)
        if d.next_off != off:
            d._set_broken()
        d.next_off = off
        return d

    def test_a_decoder_placed_is_the_one_frames_and_skip_place(self):
        recs = mixed_records(240, seed=11)
        rng = random.Random(5)
        # raw frames, which the replay applies where they lie, and frames with
        # flags, which it leaves to Decoder._apply: v1's deflated ones, and
        # zstd's when this Python has a module
        datas = [write_bytes(recs, flush_every=3, reset_bytes=6000),
                 read_file(str(DATA / 'v1-zlib.ebr'))]
        if HAS_ZSTD:
            datas.append(write_bytes(recs, flush_every=3, reset_bytes=6000, zstd=3))
        for data in datas:
            for trial in range(30):
                bad = data if trial == 0 else damage(data, rng)
                ends = [f.end for f in E.frames(bad, verify=False) if f.kind != E.BAD]
                for off in ends[trial % 5::5]:
                    got, want = E.Decoder.at(bad, off), self.reference_at(bad, off)
                    self.assertEqual(decoder_state(got), decoder_state(want), (trial, off))
                    a = [pr for g in E.frames(bad, off) for pr in got.records(bad, g)]
                    b = [pr for g in E.frames(bad, off) for pr in want.records(bad, g)]
                    self.assertEqual([p for p, _r in a], [p for p, _r in b], (trial, off))
                    self.assertTrue(all(deq(x, y) for (_p, x), (_q, y) in zip(a, b)),
                                    (trial, off))       # NaN equals NaN: v1.jsonl's records

    def test_verify_back_is_a_reverse_walk_that_meets_no_bad_region(self):
        data = write_bytes(mixed_records(160, seed=13), flush_every=2, reset_bytes=5000)
        rng = random.Random(17)
        for trial in range(250):
            bad = data if trial == 0 else damage(data, rng)
            end = E.complete_end(bad)
            for stop in (0, end // 3, end - 400, end - 1, end):
                clean = True
                for f in E.frames_reverse(bad, end, verify=True):
                    if f.kind == E.BAD:
                        clean = False
                        break
                    if f.off <= stop:
                        break
                self.assertEqual(E.verify_back(bad, end, stop), clean, (trial, stop))

    def test_a_frame_whose_events_are_all_wanted_is_read_whole(self):
        recs = []
        for i in range(96):
            ev = ['exit', 'conn', 'odd', None, 'meta', 5][(i // 6) % 6]
            recs.append({'i': i} if ev is None else {'event': ev, 'i': i})
        recs += [{'event': 'exit', 'i': 200}, {'event': 'meta', 'i': 201}, {'event': 'odd'}]
        data = write_bytes(recs, flush_every=6)
        allp = decode(data)[0]
        real = E.Decoder._some
        calls = []

        def spy(self, *a):
            calls.append(a[4])
            return real(self, *a)
        cases = [{'exit'}, {'exit', 'conn', 'meta'}, {'odd'}, {'odd', 'exit'}, {None},
                 {None, 'exit', 'conn'}, frozenset(E.KINDS), {5}]
        for kinds in cases:
            del calls[:]
            with mock.patch.object(E.Decoder, '_some', spy):
                got, dec = decode(data, kinds)
                rev = list(E.records_reverse(data, len(data), kinds=kinds))
            want = reference_filter(allp, kinds, None)
            self.assertEqual(got, want, kinds)
            self.assertEqual(rev, want[::-1], kinds)
            self.assertEqual(dec.stats['records'] + dec.stats['filtered'], len(allp), kinds)
            # a frame of only wanted events never goes record by record
            self.assertLess(len(calls), 2 * len(data_frames(data)), kinds)
        del calls[:]
        with mock.patch.object(E.Decoder, '_some', spy):
            decode(data, {'exit', 'conn', 'meta'})
        self.assertEqual(len(calls), 1)             # the frame that holds an 'odd' too

    def test_a_dict_of_another_shape_than_its_slot_held_first(self):
        first = {'a': 1, 'o': {'x': 1, 's': 'v'}, 'p': {'y': None}, 'q': {}}
        others = [{'a': 2, 'o': {'x': 2, 's': 'w'}, 'p': {'y': None}, 'q': {}},
                  {'a': 3, 'o': {'z': 'another shape'}, 'p': {'y': None}, 'q': {}},
                  {'a': 4, 'o': {'x': 300000, 's': 'w'}, 'p': {'y': None}, 'q': {}},
                  {'a': 5, 'o': {'x': 1, 's': 'v'}, 'p': {'y': [1, {'k': 2}]}, 'q': {'e': None}},
                  {'a': 6, 'o': dict(('k%d' % i, i) for i in range(40)), 'p': {}, 'q': {}}]
        maps = [{'a': 7 + i, 'o': {'c%d' % i: i}, 'p': {'y': i}, 'q': {}} for i in range(12)]
        recs = [first] + others + maps + others + [first]
        for fe in (1, 3, 0):
            data = write_bytes(recs, flush_every=fe)
            got = records_of(data)
            self.assertEqual(len(got), len(recs))
            self.assertTrue(all(deq(g, oracle(r)) for g, r in zip(got, recs)), fe)
            rev = [r for _p, r in E.records_reverse(data, len(data))]
            self.assertTrue(all(deq(g, oracle(r)) for g, r in zip(rev[::-1], recs)), fe)

    def test_inline_dicts_keep_keys_out_of_the_source(self):
        sources = []
        real = compile

        def spy(src, *a, **kw):
            sources.append(src)
            return real(src, *a, **kw)
        E._DEC_CACHE.clear()
        E.compile = spy
        try:
            keys = InjectionTest.KEYS
            rec = dict(('%s%d' % (k, j), {k: j, 's' + k: k, 'n': None}) for j, k in enumerate(keys))
            recs = [rec, dict(rec), {'z': 1}]
            self.assertEqual(records_of(write_bytes(recs)), [oracle(r) for r in recs])
        finally:
            del E.compile
        self.assertTrue(any('if b[p] == C' in s for s in sources))
        allowed = re.compile(r'[A-Za-z0-9_ ,.():\[\]{}=*"\n+-]*\Z')
        for src in sources:
            self.assertTrue(allowed.match(src), src)
            for k in ('__import__', 'zz_unique', "'''", '\\', 'line'):
                self.assertNotIn(k, src)

    def test_shapes_that_differ_in_int_widths_share_their_code(self):
        E._DEC_CACHE.clear()
        recs = [{'n': v, 's': 'x'} for v in (1, 300, 70000, 2 ** 40)]
        self.assertEqual(records_of(write_bytes(recs, flush_every=1)), recs)
        self.assertEqual(len(E._DEC_CACHE), 1)

    def test_a_source_is_closed_by_what_opened_it(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, 'x.ebr')
            with open(p, 'wb') as fh:
                fh.write(write_bytes([exit_rec(1)]))
            with E.source(p) as s:
                self.assertIsNotNone(s.fd)
                with E.source(s) as s2:
                    self.assertIs(s2, s)
                self.assertIsNotNone(s.fd)
                self.assertEqual([f.kind for f in E.frames(s)], [E.SEGMENT, E.DATA])
                self.assertTrue(E.verify_back(s, os.path.getsize(p)))
            self.assertIsNone(s.fd)

    def test_read_ahead_is_small_after_a_jump_and_grows_as_reads_move_on(self):
        data = write_bytes(mixed_records(24000, seed=3), flush_every=3)
        sizes = []

        class Spy(io.BytesIO):
            def read(self, n=-1):
                if self.tell() < len(data):         # not a short read's retry at the end
                    sizes.append(n)
                return io.BytesIO.read(self, n)
        s = E._Src(Spy(data))
        off = E.resync(s, len(data) // 2)
        self.assertTrue(sizes and all(n <= E._WIN0 for n in sizes), sizes)   # a probe's
        del sizes[:]
        got = [(f.off, f.end, f.payload) for f in E.frames(s, off)]
        self.assertEqual(got, [(f.off, f.end, f.payload) for f in E.frames(data, off)])
        self.assertEqual(sizes[:3], [E._WIN0 * 4, E._WIN0 * 16, E._WIN])
        self.assertLessEqual(len(sizes), 3 + 2 * (len(data) // 2) // E._WIN + 2)
        del sizes[:]
        got = [(f.off, f.end) for f in E.frames_reverse(s, len(data))]
        self.assertEqual(got, [(f.off, f.end) for f in E.frames_reverse(data, len(data))])
        self.assertEqual(max(sizes), E._WIN)


# ---------------------------------------------------------------------------
# the command line
# ---------------------------------------------------------------------------

class CliTest(_TmpCase):
    def setUp(self):
        _TmpCase.setUp(self)
        self.recs = mixed_records(80, seed=11)
        self.recs.append({'event': 'exit', 'big': 2 ** 90, 'nan': NAN, 's': '\ud800x',
                          'ts_epoch': 1794268900.5})
        self.jsonl = self.path('h.exits.jsonl')
        with open(self.jsonl, 'w') as fh:
            for r in self.recs:
                fh.write(json.dumps(r) + '\n')
        self.lines = read_file(self.jsonl)

    def convert(self, src=None, dst=None, *opts):
        dst = dst or self.path('h.exits.ebr')
        rc, out, err = run_cli('convert', *(list(opts) + [src or self.jsonl, dst]))
        self.assertEqual(rc, 0, err)
        return dst

    def test_cat_is_the_jsonl_twin(self):
        ebr = self.convert()
        rc, out, err = run_cli('cat', ebr)
        self.assertEqual((rc, out), (0, self.lines), err)
        rc, out, err = run_cli('cat', self.jsonl)
        self.assertEqual((rc, out), (0, self.lines))
        gz = self.path('h.exits.ebr.gz')
        with open(ebr, 'rb') as a, gzip.open(gz, 'wb') as b:
            b.write(a.read())
        rc, out, err = run_cli('cat', gz)
        self.assertEqual((rc, out), (0, self.lines))
        jgz = self.path('h.exits.jsonl.gz')
        with gzip.open(jgz, 'wb') as b:
            b.write(self.lines)
        rc, out, err = run_cli('cat', jgz)
        self.assertEqual((rc, out), (0, self.lines))

    def test_cat_reads_stdin_in_either_format(self):
        # '-' is stdin: the bytes read to tell the format are replayed, and stdin
        # is never closed and opened again (it cannot be)
        ebr = self.convert()
        for data in (self.lines, read_file(ebr), gzip.compress(read_file(ebr)),
                     gzip.compress(self.lines)):
            rc, out, err = run_cli('cat', '-', input=data)
            self.assertEqual((rc, out), (0, self.lines), err)
        rc, out, err = run_cli('cat', '--tail', 2, '-', input=self.lines)
        self.assertEqual((rc, out), (0, b''.join(self.lines.splitlines(True)[-2:])), err)

    def test_count_kinds_reads_stdin_once(self):
        # --kinds decodes every record; from stdin it must read on from the
        # reader that told the format, not open stdin again
        ebr = self.convert()
        want = len(self.recs)
        for data in (self.lines, read_file(ebr), gzip.compress(read_file(ebr)),
                     gzip.compress(self.lines)):
            for opts in ((), ('--kinds',)):
                rc, out, err = run_cli('count', '--json', *(opts + ('-',)), input=data)
                self.assertEqual(rc, 0, err)
                self.assertEqual(json.loads(out)['total'], want, (opts, out))

    def test_verify_strict_passes_a_sealed_day_and_stdin(self):
        # a stream source (a .gz, stdin) is sized by what it held: no phantom
        # pending tail past its last frame
        ebr = self.convert()
        gz = self.path('h.exits.ebr.gz')
        with open(ebr, 'rb') as a, gzip.open(gz, 'wb') as b:
            b.write(a.read())
        for args, inp in (((ebr,), None), ((gz,), None), (('-',), read_file(ebr))):
            rc, out, err = run_cli('verify', '--strict', *args, input=inp)
            self.assertEqual(rc, 0, out + err)
            self.assertNotIn(b'pending', out)
        size = len(read_file(ebr))
        rc, out, err = run_cli('frames', gz)
        self.assertEqual(rc, 0, err)
        self.assertEqual(int(out.splitlines()[-1].split()[1]), size)

    def test_cat_options(self):
        ebr = self.convert()
        lines = self.lines.splitlines(True)
        rc, out, _ = run_cli('cat', '--head', 5, ebr)
        self.assertEqual(out, b''.join(lines[:5]))
        for f in (ebr, self.jsonl):
            rc, out, _ = run_cli('cat', '--tail', 7, f)
            self.assertEqual(out, b''.join(lines[-7:]), f)
        rc, out, _ = run_cli('cat', '--kind', 'conn,residency', ebr)
        want = [ln for ln, r in zip(lines, self.recs) if r.get('event') in ('conn', 'residency')]
        self.assertEqual(out, b''.join(want))
        rc, out, _ = run_cli('cat', '--where', 'user=u1', '--where', 'user=u2', ebr)
        want = [ln for ln, r in zip(lines, self.recs) if str(r.get('user')) in ('u1', 'u2')]
        self.assertEqual(out, b''.join(want))
        lo, hi = 1794268820.0, 1794268850.0
        rc, out, _ = run_cli('cat', '--from', lo, '--to', hi, ebr)
        want = [ln for ln, r in zip(lines, self.recs) if lo <= r['ts_epoch'] < hi]
        self.assertEqual(out, b''.join(want))
        rc, out, _ = run_cli('cat', '--pos', '--head', 2, ebr)
        pos = [int(x.split(b'\t')[0]) for x in out.splitlines()]
        self.assertEqual(pos, [p for p, _r in decode(read_file(ebr))[0][:2]])
        rc, out, _ = run_cli('cat', '--head', 3, ebr, self.jsonl)
        self.assertEqual(out, b''.join(lines[:3]))

    def test_convert_options_and_bad_lines(self):
        gz = self.convert(None, self.path('o.ebr.gz'), '--reset-bytes', '4096', '--window', '0')
        with gzip.open(gz, 'rb') as fh:
            self.assertEqual(fh.read(4), E.MAGIC)
        rc, out, _ = run_cli('cat', gz)
        self.assertEqual(out, self.lines)
        bad = self.path('bad.jsonl')
        with open(bad, 'wb') as fh:
            fh.write(b'{"a": 1}\nnot json\n\n[1, 2]\n{"b": 2}\n')
        rc, out, err = run_cli('convert', bad, self.path('bad.ebr'))
        self.assertEqual(rc, 1, err)
        rc, out, _ = run_cli('cat', self.path('bad.ebr'))
        self.assertEqual(out, b'{"a": 1}\n{"b": 2}\n')
        rc, out, err = run_cli('convert', '-', self.path('stdin.ebr'), input=self.lines)
        self.assertEqual(rc, 0, err)
        rc, out, _ = run_cli('cat', self.path('stdin.ebr'))
        self.assertEqual(out, self.lines)

    def test_verify(self):
        ebr = self.convert()
        rc, out, err = run_cli('verify', ebr)
        self.assertEqual(rc, 0, out + err)
        self.assertIn(b'OK', out)
        rc, out, _ = run_cli('verify', '--json', ebr, self.jsonl)
        rep = json.loads(out.decode())
        self.assertTrue(rep['ok'])
        self.assertEqual(rep['files'][0]['records'], len(self.recs))
        data = read_file(ebr)
        with open(ebr, 'ab') as fh:
            fh.write(data[:30])                     # a pending tail
        self.assertEqual(run_cli('verify', ebr)[0], 0)
        self.assertEqual(run_cli('verify', '--strict', ebr)[0], 1)
        self.assertEqual(run_cli('verify', '--quick', ebr)[0], 0)
        b = bytearray(data)
        b[data_frames(data)[1].off + 60] ^= 1
        with open(ebr, 'wb') as fh:
            fh.write(bytes(b))
        rc, out, _ = run_cli('verify', ebr)
        self.assertEqual(rc, 1)
        self.assertIn(b'BAD region', out)

    def test_compare(self):
        ebr = self.convert()
        self.assertEqual(run_cli('compare', self.jsonl, ebr)[0], 0)
        short = self.path('short.jsonl')
        with open(short, 'wb') as fh:
            fh.write(b''.join(self.lines.splitlines(True)[:-3]))
        self.assertEqual(run_cli('compare', short, ebr)[0], 1)
        self.assertEqual(run_cli('compare', '--live', short, ebr)[0], 0)
        other = self.path('other.jsonl')
        recs = [dict(r) for r in self.recs]
        recs[3]['pid'] = -1
        recs[4] = dict(reversed(list(recs[4].items())))
        with open(other, 'w') as fh:
            for r in recs:
                fh.write(json.dumps(r) + '\n')
        rc, out, _ = run_cli('compare', other, ebr)
        self.assertEqual(rc, 1)
        self.assertIn(b'record 3', out)
        self.assertIn(b'key order', out)
        one = self.path('one.jsonl')
        with open(one, 'w') as fh:
            fh.write('{"a": 1}\n')
        two = self.path('two.jsonl')
        with open(two, 'w') as fh:
            fh.write('{"a": 1.0}\n')
        self.assertEqual(run_cli('compare', one, two)[0], 1)

    def test_count(self):
        ebr = self.convert()
        rc, out, _ = run_cli('count', ebr)
        self.assertEqual((rc, out), (0, b'%d\n' % len(self.recs)))
        rc, out, _ = run_cli('count', self.jsonl)
        self.assertEqual(out, b'%d\n' % len(self.recs))
        rc, out, _ = run_cli('count', '--json', '--kinds', ebr, self.jsonl)
        rep = json.loads(out.decode())
        self.assertEqual(rep['total'], 2 * len(self.recs))
        self.assertEqual(rep['files'][ebr]['kinds'], rep['files'][self.jsonl]['kinds'])

    def test_append(self):
        p = self.path('h.ebr')
        half = self.lines.splitlines(True)
        for chunk in (half[:40], half[40:]):
            rc, out, err = run_cli('append', '--host', 'h', '--stream', 'node',
                                   '--writer', 'test', p, input=b''.join(chunk))
            self.assertEqual(rc, 0, err)
        self.assertEqual(run_cli('cat', p)[1], self.lines)
        self.assertEqual(sum(1 for f in E.frames(p) if f.kind == E.SEGMENT), 1)
        seg = segment_of(read_file(p))
        self.assertEqual((seg['host'], seg['stream'], seg['writer'], seg['version']),
                         ('h', 'node', 'test', 2))
        self.assertIsInstance(seg['pid'], int)
        rc, out, err = run_cli('append', p, input=b'{"ok": 1}\nbroken\n')
        self.assertEqual(rc, 1)
        self.assertEqual(records_of(read_file(p))[-1], {'ok': 1})

    @unittest.skipIf(fcntl is None, 'no flock')
    def test_append_busy(self):
        p = self.path('h.ebr')
        fd = os.open(p, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            rc, out, err = run_cli('append', '--lock-wait', '0.2', p, input=b'{"a": 1}\n')
        finally:
            os.close(fd)
        self.assertEqual(rc, 2, err)
        self.assertIn(b'locked', err)

    def test_bench_frames_and_version(self):
        rc, out, err = run_cli('bench', '--limit', 60, '--reps', 1, '--json', self.jsonl)
        self.assertEqual(rc, 0, err)
        rep = json.loads(out.decode())
        self.assertEqual(rep['mismatches'], 0)
        self.assertEqual(rep['records'], 60)
        for k in ('json.dumps', 'ebr encode', 'json.loads', 'ebr decode', 'ebr reject (where)'):
            self.assertIn(k, rep['us_per_record'])
        rc, out, err = run_cli('bench', '--limit', 30, '--reps', 1, '--frame-records', 5,
                               self.jsonl)
        self.assertEqual(rc, 0, err)
        self.assertIn(b'round-trip mismatches: 0', out)
        ebr = self.convert()
        rc, out, _ = run_cli('frames', ebr)
        self.assertEqual(rc, 0)
        self.assertIn(b'SEGMENT', out)
        self.assertIn(b'DATA', out)
        rc, out, _ = run_cli('--version')
        self.assertEqual(rc, 0)
        self.assertEqual(out.decode(), 'ebr 2 (zstd: %s)\n' % (E.zstd_backend() or 'unavailable'))
        self.assertEqual(run_cli('--version', zstd=False)[1], b'ebr 2 (zstd: unavailable)\n')

    @NEEDS_ZSTD
    def test_convert_with_zstd(self):
        ebr = self.convert(None, None, '--zstd', '3')
        data = read_file(ebr)
        flags = [f.flags for f in data_frames(data)]
        self.assertIn(E.F_ZSTD, flags)
        self.assertEqual(run_cli('cat', ebr)[:2], (0, self.lines))
        self.assertEqual(run_cli('compare', self.jsonl, ebr)[0], 0)
        self.assertEqual(run_cli('count', ebr)[1], b'%d\n' % len(self.recs))
        rc, out, err = run_cli('verify', '--strict', '--json', ebr)
        rep = json.loads(out.decode())['files'][0]
        self.assertEqual((rc, rep['zstd_frames'], rep['zstd_unavailable']),
                         (0, flags.count(E.F_ZSTD), 0))
        rc, out, err = run_cli('verify', ebr)
        self.assertIn(b' zstd), ', out)
        rc, out, err = run_cli('frames', ebr)
        self.assertIn(b'zstd, ', out)
        # header-only readers need no zstd module: the count, the frame table
        self.assertEqual(run_cli('count', ebr, zstd=False)[:2], (0, b'%d\n' % len(self.recs)))
        self.assertEqual(run_cli('frames', ebr, zstd=False)[0], 0)
        rc, out, err = run_cli('verify', ebr, zstd=False)
        self.assertEqual(rc, 1)
        self.assertIn(b'zstd_unavailable', out)
        self.assertNotIn(b'BAD', out)

    def test_zstd_asked_for_without_a_zstd_module(self):
        out_p = self.path('z.ebr')
        rc, out, err = run_cli('convert', '--zstd', '3', self.jsonl, out_p, zstd=False)
        self.assertEqual(rc, 2)
        self.assertIn(b'pip install zstandard', err)
        self.assertFalse(os.path.exists(out_p))
        rc, out, err = run_cli('bench', '--zstd', '3', '--limit', 10, '--reps', 1, self.jsonl,
                               zstd=False)
        self.assertEqual(rc, 2, err)
        rc, out, err = run_cli('bench', '--limit', 20, '--reps', 1, '--json', self.jsonl,
                               zstd=False)
        rep = json.loads(out.decode())
        self.assertEqual((rc, rep['zstd'], rep['mismatches']), (0, None, 0))
        self.assertEqual(list(rep['sizes']), ['jsonl', 'ebr', 'jsonl.gz-6', 'ebr.gz-6'])
        self.assertIsNone(rep['us_per_record']['ebr+zstd decode'])
        rc, out, err = run_cli('bench', '--limit', 20, '--reps', 1, self.jsonl, zstd=False)
        self.assertEqual(rc, 0, err)
        self.assertIn(b'ebr+zstd: unavailable', out)
        # append (the node tier's): the snapshot goes in uncompressed, and it says why
        p = self.path('n.ebr')
        rc, out, err = run_cli('append', '--zstd', '3', p, input=self.lines, zstd=False)
        self.assertEqual(rc, 0, err)
        self.assertIn(b'--zstd 3: this Python has no zstd module', err)
        self.assertIn(b'appending uncompressed', err)
        self.assertEqual(run_cli('cat', p)[1], self.lines)
        self.assertEqual(set(f.flags for f in E.frames(p)), {0})
        # a level that is not one is a usage error
        self.assertEqual(run_cli('convert', '--zstd', '23', self.jsonl, out_p)[0], 2)
        self.assertEqual(run_cli('append', '--zstd', 'fast', p, input=b'')[0], 2)
        self.assertEqual(run_cli('bench', '--zstd', '-1', self.jsonl)[0], 2)

    @NEEDS_ZSTD
    def test_bench_and_append_with_zstd(self):
        rc, out, err = run_cli('bench', '--limit', 60, '--reps', 1, '--json', self.jsonl)
        rep = json.loads(out.decode())
        self.assertEqual((rc, rep['mismatches']), (0, 0), err)
        self.assertIn('ebr+zstd3', rep['sizes'])
        self.assertEqual(rep['zstd']['module'], E.zstd_backend())
        for k in ('ebr+zstd encode', 'ebr+zstd decode'):
            self.assertIsNotNone(rep['us_per_record'][k])
        rc, out, err = run_cli('bench', '--limit', 60, '--reps', 1, '--zstd', '19', self.jsonl)
        self.assertEqual(rc, 0, err)
        self.assertIn(b'ebr+zstd19', out)
        rep = json.loads(run_cli('bench', '--limit', 20, '--reps', 1, '--zstd', '0', '--json',
                                 self.jsonl)[1].decode())
        self.assertIsNone(rep['zstd'])
        p = self.path('a.ebr')
        rc, out, err = run_cli('append', '--zstd', '3', p, input=self.lines)
        self.assertEqual((rc, err), (0, b''))
        self.assertIn(E.F_ZSTD, [f.flags for f in E.frames(p)])
        self.assertEqual(run_cli('cat', p)[1], self.lines)

    def test_frames_and_verify_show_versions_and_marks(self):
        p = self.path('m.ebr')
        shutil.copy(str(DATA / 'v1.ebr'), p)
        w = E.Writer.open_append(p)
        for r in self.recs[:5]:
            w.write(r)
        w.mark({'next': 5, 'src': 'x.jsonl'})
        w.close()
        with open(p, 'ab') as fh:              # and a SEGMENT of a version yet to come
            fh.write(E._frame(E.SEGMENT, 0, seg_v2(version=3), 0, NAN, NAN, 0, 0))
        rc, out, err = run_cli('frames', p)
        self.assertEqual(rc, 0, err)
        rows = out.decode().splitlines()
        self.assertTrue(rows[0].endswith(' info'))
        segs = [r for r in rows if ' SEGMENT ' in r]
        self.assertEqual([r.rsplit(' ', 1)[1] for r in segs[:-1]], ['v1'] * (len(segs) - 2) + ['v2'])
        self.assertTrue(segs[-1].endswith(' v3: a version this reader does not know'), segs[-1])
        self.assertTrue([r for r in rows if ' MARK ' in r][0].endswith(
            '  {"next": 5, "src": "x.jsonl"}'), rows)
        rc, out, err = run_cli('verify', '--json', p)
        rep = json.loads(out.decode())['files'][0]
        self.assertEqual((rc, rep['mark_frames'], rep['advisory_frames'], rep['bad_marks'],
                          rep['problems']), (0, 1, 1, 0, 0))
        rc, out, err = run_cli('verify', p)
        self.assertIn(b' segments, 1 marks), ', out)
        # a MARK whose map is malformed is a problem for verify (not for readers of records)
        with open(p, 'ab') as fh:
            fh.write(E._frame(E.MARK, 0, b'\x01\x01a\x09', 0, NAN, NAN, 0, 0))
        rc, out, err = run_cli('verify', p)
        self.assertEqual(rc, 1)
        self.assertIn(b'bad_marks: 1', out)
        self.assertIn(b'malformed', run_cli('frames', p)[1])
        self.assertEqual(run_cli('cat', p)[0], 0)

    def test_exit_codes(self):
        self.assertEqual(run_cli()[0], 2)
        self.assertEqual(run_cli('cat')[0], 2)
        self.assertEqual(run_cli('nope')[0], 2)
        self.assertEqual(run_cli('cat', self.path('missing.ebr'))[0], 2)
        self.assertEqual(run_cli('cat', '--from', 'yesterday-ish', self.jsonl)[0], 2)
        rc, out, _ = run_cli('cat', '--from', '2026-11-10 00:00', self.jsonl)
        self.assertEqual(rc, 0)

    def test_sigpipe_is_quiet(self):
        ebr = self.convert()
        p = subprocess.Popen([sys.executable, EBR_PY, 'cat', ebr], stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE)
        p.stdout.read(10)
        p.stdout.close()
        err = p.stderr.read()
        p.wait()
        p.stderr.close()
        self.assertNotIn(b'Traceback', err)


# ---------------------------------------------------------------------------
# readers' plumbing
# ---------------------------------------------------------------------------

class SourceTest(_TmpCase):
    def test_every_kind_of_source_reads_alike(self):
        recs = mixed_records(40, seed=9)
        data = write_bytes(recs, flush_every=5)
        p = self.path()
        with open(p, 'wb') as fh:
            fh.write(data)
        want = frame_list(data)
        self.assertEqual(frame_list(p), want)
        self.assertEqual(frame_list(Path(p)), want)
        self.assertEqual(frame_list(bytearray(data)), want)
        self.assertEqual(frame_list(memoryview(data)), want)
        with open(p, 'rb') as fh:
            self.assertEqual(frame_list(fh), want)

    def test_a_file_object_is_never_read_through_its_fileno(self):
        data = write_bytes([{'a': 1}])

        class NoFileno(io.BytesIO):
            def fileno(self):
                raise AssertionError('fileno used')
        self.assertEqual(records_of(NoFileno(data).getvalue()), [{'a': 1}])
        src = NoFileno(data)
        dec = E.Decoder()
        got = []
        for f in E.frames(src):
            got.extend(r for _p, r in dec.records(src, f))
        self.assertEqual(got, [{'a': 1}])
        self.assertEqual(E.complete_end(src), len(data))
        self.assertEqual(list(E.records_reverse(src, len(data)))[0][1], {'a': 1})

    def test_iter_records_and_open_feed(self):
        recs = mixed_records(30, seed=4)
        data = write_bytes(recs)
        jl = ''.join(json.dumps(r) + '\n' for r in recs).encode()
        files = {'a.ebr': data, 'a.jsonl': jl, 'a.ebr.gz': gzip.compress(data),
                 'a.jsonl.gz': gzip.compress(jl), 'b.ebr': data}
        for name, blob in files.items():
            with open(self.path(name), 'wb') as fh:
                fh.write(blob)
            got = [r for _p, r in E.iter_records(self.path(name))]
            self.assertTrue(len(got) == len(recs) and all(deq(g, oracle(r)) for g, r in zip(got, recs)), name)
            fh, fmt = E.open_feed(self.path(name))
            fh.close()
            self.assertEqual(fmt, 'ebr' if '.ebr' in name else 'jsonl')
        pos = [p for p, _r in E.iter_records(self.path('a.jsonl'))]
        self.assertEqual(pos[:3], [0, len(json.dumps(recs[0])) + 1,
                                   len(json.dumps(recs[0])) + len(json.dumps(recs[1])) + 2])
        st = {}
        with open(self.path('p.jsonl'), 'wb') as fh:
            fh.write(jl + b'{"partial": ')
        got = list(E.iter_records(self.path('p.jsonl'), stats=st))
        self.assertEqual((len(got), st['pending'], st['bad_lines']), (len(recs), 1, 0))


# ---------------------------------------------------------------------------
# the copies and Python 3.6
# ---------------------------------------------------------------------------

class DashboardSyncTest(unittest.TestCase):
    def test_the_dashboard_copy_is_byte_identical(self):
        dash = _ROOT.parent / 'dashboard' / 'rc_dashboard' / 'ebr.py'
        if not dash.exists():
            self.skipTest('no dashboard copy of ebr.py yet')
        self.assertEqual(dash.read_bytes(), (_ROOT / 'lib' / 'ebr.py').read_bytes())


class Py36Test(unittest.TestCase):
    """EL8's python3 is 3.6.8: the AST and a ban list keep 3.7+ out."""

    FILES = [_ROOT / 'lib' / 'ebr.py', _ROOT / 'ebpf_trace.py', Path(__file__).resolve()]
    BANNED_NAMES = re.compile(r'\b(time_ns|isascii|removeprefix|removesuffix|fromisoformat|'
                              r'bit_count|dataclass|dataclasses|capture_output)\b')

    def code_text(self, src):
        """The source without its strings and comments, one token per word."""
        out = []
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type in (tokenize.COMMENT, tokenize.STRING):
                if tok.type == tokenize.STRING:
                    prefix = re.match(r'[A-Za-z]*', tok.string).group(0)
                    self.assertNotIn('f', prefix.lower(), 'an f-string: %s' % tok.string[:40])
                continue
            if getattr(tokenize, 'FSTRING_START', None) == tok.type:
                self.fail('an f-string at line %d' % tok.start[0])
            out.append(tok.string)
        return ' '.join(out)

    def test_parses_as_python_3_6(self):
        for path in self.FILES:
            src = path.read_text(encoding='utf-8')
            if sys.version_info >= (3, 8):
                ast.parse(src, feature_version=(3, 6))
            tree = ast.parse(src)
            for node in ast.walk(tree):
                self.assertNotIn(type(node).__name__, ('NamedExpr', 'JoinedStr'), path)
                if isinstance(node, (ast.FunctionDef, ast.Lambda)):
                    self.assertFalse(getattr(node.args, 'posonlyargs', None), path)

    def test_ban_list(self):
        for path in self.FILES:
            src = path.read_text(encoding='utf-8')
            code = self.code_text(src)
            m = self.BANNED_NAMES.search(code)
            self.assertIsNone(m, '%s: %s' % (path.name, m and m.group(0)))
            self.assertNotIn(':=', code, path)
            self.assertIsNone(re.search(r'@ lru_cache (?!\()', code), path)
            for node in ast.walk(ast.parse(src)):
                if not isinstance(node, ast.Call):
                    continue
                self.assertLessEqual(len(node.args) + len(node.keywords), 255, path)
                kws = set(k.arg for k in node.keywords)
                name = getattr(node.func, 'attr', None) or getattr(node.func, 'id', None)
                if name in ('run', 'Popen', 'call', 'check_call', 'check_output'):
                    self.assertFalse(kws & set(['text', 'capture_output']), path)
                if name == 'zip':
                    self.assertNotIn('strict', kws, path)
                if name == 'add_subparsers':
                    self.assertNotIn('required', kws, path)

    def test_generated_calls_stay_under_255_arguments(self):
        rec = dict(('f%04d' % i, i) for i in range(1000))
        rec.update(('s%04d' % i, 'v') for i in range(1000))
        sources = []
        real = compile

        def spy(src, *a, **kw):
            sources.append(src)
            return real(src, *a, **kw)
        E._ENC_CACHE.clear()
        E._DEC_CACHE.clear()
        E.compile = spy
        try:
            self.assertEqual(records_of(write_bytes([rec])), [rec])
        finally:
            del E.compile
        for src in sources:
            for node in ast.walk(ast.parse(src)):
                if isinstance(node, ast.Call):
                    self.assertLessEqual(len(node.args) + len(node.keywords), 255)
                if isinstance(node, ast.FunctionDef):
                    self.assertLessEqual(len(node.args.args), 255)


if __name__ == '__main__':
    unittest.main()
