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
import tempfile
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


def run_cli(*args, **kw):
    inp = kw.pop('input', None)
    p = subprocess.run([sys.executable, EBR_PY] + [str(a) for a in args],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, input=inp,
                       cwd=kw.pop('cwd', None))
    return p.returncode, p.stdout, p.stderr


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
        seg = json.loads(payload.decode('ascii'))
        self.assertEqual(list(seg)[:7], ['format', 'version', 'str_id_size', 'kinds',
                                          'reset_bytes', 'zlib', 'created'])
        self.assertEqual((seg['format'], seg['version'], seg['str_id_size'], seg['host']),
                         ('ebr', 1, 2, 'h1'))
        self.assertEqual(seg['kinds'], list(E.KINDS))
        self.assertNotIn(b', ', payload)            # compact
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
        with self.assertRaises(ValueError):
            E.Writer(io.BytesIO(), zlib=10)
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

    def test_a_new_segment_after_a_version_2_segment(self):
        p = self.path()
        seg = json.dumps({'format': 'ebr', 'version': 2, 'str_id_size': 2,
                          'kinds': list(E.KINDS)}).encode()
        with open(p, 'wb') as fh:
            fh.write(E._frame(E.SEGMENT, 0, seg, 0, NAN, NAN, 0, 0))
        w = E.Writer.open_append(p)
        w.write({'a': 1})
        w.close()
        self.assertEqual(self.segments(p), 2)
        got, dec = decode(read_file(p))
        self.assertEqual([r for _p, r in got], [{'a': 1}])

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


class ZlibTest(unittest.TestCase):
    def test_mixed_raw_and_zlib_frames(self):
        recs = [exit_rec(i) for i in range(40)] + [{'tiny': i} for i in range(3)]
        bio = io.BytesIO()
        w = E.Writer(bio, zlib=True)
        for i, r in enumerate(recs):
            w.write(r)
            if i in (19, 39, 40, 41):
                w.flush()
        w.close()
        data = bio.getvalue()
        flags = [f.flags for f in data_frames(data)]
        self.assertIn(E.F_ZLIB, flags)
        self.assertIn(0, flags)                      # a frame zlib would grow stays raw
        self.assertEqual(w.stats['zlib_frames'], flags.count(E.F_ZLIB))
        self.assertTrue(all(deq(g, oracle(r)) for g, r in zip(records_of(data), recs)))
        self.assertEqual(len(records_of(data)), len(recs))
        self.assertLess(len(data), len(write_bytes(recs, flush_every=20)))

    def test_levels(self):
        for z in (1, 6, 9):
            data = write_bytes([exit_rec(i) for i in range(30)], zlib=z)
            self.assertEqual(len(records_of(data)), 30)

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
        for body in (z[:-3], z + b'junk', b'not zlib'):
            got, dec = self.bad_zlib(body)
            self.assertEqual(dec.stats['bad_frames'], 1)


class CompatTest(unittest.TestCase):
    def test_unknown_flag_makes_the_segment_unsupported(self):
        a = write_bytes([{'a': i} for i in range(6)], flush_every=2)
        b = write_bytes([{'b': 1}])
        f = data_frames(a)[1]
        data = rewrite_header(a, f.off, flags=2) + b
        got, dec = decode(data)
        self.assertEqual([r for _p, r in got], [{'a': 0}, {'a': 1}, {'b': 1}])
        self.assertEqual(dec.stats['unsupported_frames'], 2)
        self.assertEqual(dec.stats['bad_frames'], 0)

    def test_unknown_kinds_are_skipped(self):
        a = write_bytes([{'a': i} for i in range(4)], flush_every=2)
        f = data_frames(a)[1]
        adv = E._frame(9, 0, b'advisory', 0, NAN, NAN, 0, 0)
        data = a[:f.off] + adv + a[f.off:]
        got, dec = decode(data)
        self.assertEqual([r for _p, r in got], [{'a': i} for i in range(4)])
        self.assertIn(9, [f.kind for f in E.frames(data)])

    def test_version_2_and_other_formats(self):
        b = write_bytes([{'b': 1}])
        for seg in ({'format': 'ebr', 'version': 2, 'str_id_size': 2},
                    {'format': 'other', 'version': 1, 'str_id_size': 2},
                    {'format': 'ebr', 'version': 1, 'str_id_size': 3},
                    {'format': 'ebr', 'version': True, 'str_id_size': 2}):
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
        seg = json.dumps({'format': 'ebr', 'version': 1, 'str_id_size': 4,
                          'kinds': list(E.KINDS)}).encode()
        s = E._frame(E.SEGMENT, 0, seg, 0, NAN, NAN, 0, 0)
        d = E._frame(E.DATA, 0, payload, len(recs), -INF, INF, len(s), 0xffffffff)
        got, dec = decode(s + d)
        self.assertEqual(len(got), len(recs))
        self.assertTrue(all(deq(g, oracle(r)) for (_p, g), r in zip(got, recs)))
        self.assertEqual(dec.segment['str_id_size'], 4)
        st = struct.Struct('<I')
        self.assertEqual(st.size, 4)

    def test_reserved_bytes_are_ignored(self):
        data = write_bytes([{'a': 1}])
        for f in E.frames(data):
            data = rewrite_header(data, f.off, reserved=0xbeef)
        self.assertEqual(records_of(data), [{'a': 1}])


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
        gz = self.convert(None, self.path('o.ebr.gz'), '--zlib', '1', '--window', '0')
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
        seg = json.loads(payload_of(read_file(p), 0).decode())
        self.assertEqual((seg['host'], seg['stream'], seg['writer']), ('h', 'node', 'test'))
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
        self.assertEqual((rc, out.strip()), (0, b'ebr 1'))

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
