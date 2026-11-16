"""EBR v1: ebpfm binary records.

A framed, append-only, self-describing container for the collector's JSON
records. It is 4-8x smaller than JSONL, costs about what json.dumps costs to
write, less than json.loads to read, and a reader can skip a frame by its header
(time bounds, event kinds) without decoding a record.

`collector/lib/ebr.py` is canonical. `dashboard/rc_dashboard/ebr.py` is a
byte-identical copy, with sync tests on both sides. Stdlib only, Python 3.6+,
no import-time side effects; it works as `import ebr` and as `rc_dashboard.ebr`.
`python3 ebr.py --help` lists the command-line verbs.

CONTRACT (normative)
  For every record the collector can write,
      decode(encode(r)) == json.loads(json.dumps(r))
  with exact types (bool vs int vs float; str never bytes), exact key order,
  null vs absent, and fresh dicts and lists per decoded record. So: bool is
  tested before int; int, float and str subclasses are stored as their plain
  value (IntEnum -> int); tuples become lists; non-str keys are converted the
  way json.dumps converts them (float repr, NaN/Infinity/-Infinity,
  true/false/null, int repr), and keys that collide after conversion keep the
  first position and the last value, as json.loads does; lone surrogates
  survive (a string is normalised through JSON, which merges split pairs, then
  stored as UTF-8 with 'surrogatepass'); floats are bit-exact (NaN, +-inf,
  -0.0, 5e-324); ints of any size round-trip. An unserializable value raises
  TypeError, a circular (or too deep) one ValueError("Circular reference
  detected"), and no part of that record is written.

FRAME (integers little-endian; HDR = Struct('<4sBBHIIddIII'), 44 bytes;
       trailer Struct('<II'), 8 bytes)
  off size field
    0   4  magic b"\\x89EBR", also the resync marker (JSONL never starts with it)
    4   1  kind: 1 SEGMENT, 2 DATA; any other kind is advisory and skipped
    5   1  flags: bit0 = payload is zlib; any other bit -> segment unsupported
    6   2  reserved, written 0, ignored by readers
    8   4  payload_len L (readers reject L > 64 MiB)
   12   4  n_records
   16   8  ts_min f64 } over finite, non-bool int/float `ts_epoch` values;
   24   8  ts_max f64 } (-inf, +inf) if any record lacks one; NaN if no records
   32   4  seg_back = frame.off - segment.off (0 for a SEGMENT)
   36   4  kinds bitmask: bit i = KINDS[i]; bit 30 NOEVENT (event missing or
           null); bit 31 OTHER (an event not in the registry, or not a str)
   40   4  crc32 of bytes 0..39
   44   L  payload, as stored (compressed when bit0 is set)
 44+L   4  crc32 of the stored payload
 48+L   4  frame_len = 52 + L, so readers can walk backwards from EOF

  A frame is complete when its header CRC holds, it ends within the file, its
  trailer says 52 + L and (when verifying) its payload CRC holds. A record's
  position is frame.off + i, i its index in the frame: unique, increasing and
  below frame.end. Every resumable offset is a frame boundary.

SEGMENT payload: compact ASCII JSON
  {"format":"ebr","version":1,"str_id_size":2,"kinds":[registry],
   "reset_bytes":N,"zlib":L,"created":epoch, ...writer meta (host, stream, ...)}
  A SEGMENT starts a dictionary epoch: empty string and shape tables.

DATA payload (after inflating when bit0 is set)
  payload := str_base:uv shape_base:uv defs_len:uv defs record{n_records}
  def     := 0x01 n:uv utf8(surrogatepass)               STR   -> next string id
           | 0x02 nfields:uv (key_str_id:uv type:u8)*    SHAPE -> next shape id
  record  := rec_len:uv dict
  dict    := sid:uv(>=1) fixed var*                          (shape)
           | 0x00 n:uv (key_str_id:uv tagged)*               (MAP)
  fixed   := one struct '<' + codes: the shape's BOOL/I*/F64 slots in field
             order, then its STR slots in field order
  var     := its OBJ (dict), LIST (list) and BIG (big) slots, in field order
  list    := n:uv tagged*
  big     := n:uv int.to_bytes(n, 'little', signed=True)
  tagged  := 0 null | 1 false | 2 true | 3 zigzag:uv (int64) | 4 big | 5 f64
           | 6 str_id:uv | 7 dict | 8 list
  uv is unsigned LEB128, at most 10 bytes. String ids start at 0, shape ids
  at 1, both per segment. str_base and shape_base are the table sizes before
  the frame's defs; a reader whose tables differ is broken until the next
  SEGMENT.

  slot types (u8 in SHAPE): 0 NULL (no bytes) | 1 BOOL '?' | 2 I8 'b'
  | 3 I16 'h' | 4 I32 'i' | 5 I64 'q' | 6 F64 'd' (bit-exact) | 7 STR 'H'
  ('I' when str_id_size is 4) | 8 OBJ | 9 LIST | 10 BIG (ints beyond int64)

  A shape is its keys plus its layout: another null or type pattern is another
  shape. An int slot starts at the narrowest width that fits its first value
  and only ever widens (a new shape). A dict whose keys are data (by_command,
  state_counts, ...) is MAP-encoded once its context (the parent key; for MAP
  values, (context, '*')) has produced 8 distinct key sets, and any dict above
  4096 fields is MAP; the top-level record is never MAP below that.

KINDS registry (append-only; each SEGMENT records its writer's)
  bits 0-10: meta exit truncated conn netio submit residency residency_totals
  stop tcp accept (the last two retired); 30 NOEVENT; 31 OTHER. A reader skips
  a frame by its mask only if every requested name is in the registry the mask
  was built from (Decoder uses the SEGMENT's own, which is exact).

SEGMENTS AND CAPS
  A new segment starts on every Writer open (unless it resumes the last one),
  after the frame that takes the segment past reset_bytes (2 MiB), before the
  65535-entry string or shape table would overflow, and after a write error
  that could not be undone. Soft frame caps: 1 MiB or 8192 records. Hard
  limits: 64 MiB payload, 4096 fields per shape. The SEGMENT frame is written
  lazily, in the same os.write as the first DATA frame, so a file opened and
  closed with no records stays 0 bytes.

COMPATIBILITY
  The header, magic and CRCs are frozen. A SEGMENT with another format or
  version, or a str_id_size other than 2 or 4, or a frame with an unknown flag
  bit, makes that segment unsupported: skipped and counted until the next
  SEGMENT. Unknown frame kinds are skipped. An unknown opcode, slot type or tag
  is a BAD frame. Readers accept str_id_size 2 and 4. The container version is
  separate from the records' schema_version.

CRASHES AND CONCURRENCY
  One writer per file (flock, plus the collector's own lock). Readers never
  lock and never mmap. Opening a Writer refuses a non-EBR file and truncates a
  torn tail past complete_end() of at most 64 MiB + 52 bytes (only a crashed
  writer leaves one). A flush is one os.write. A failed write is retried at the
  next flush; a partial write that cannot be undone drops the pending frames
  (counted in lost_records) and starts a new segment. For a reader, an
  incomplete or bad-CRC last frame is PENDING; a bad frame followed by a valid
  one is a BAD region: it resyncs at the next magic and the decoder is broken
  until the next SEGMENT (so the loss is bounded by reset_bytes). Live readers
  pass `settle` so NFS partial visibility near the tail reads as PENDING.

API
  sniff(head) -> 'ebr' | 'jsonl' | None      kind_mask(names, registry=None)
  Prefix(str), match_where(where, rec)       (the reference filter semantics)
  Writer(target, meta=None, reset_bytes=, zlib=0, max_frame_records=,
         max_frame_bytes=, mode=0o666, lock=True, resume=False, lock_wait=0)
    .write(rec) .flush() -> bytes .close() .end .stats (a copy)
  Writer.open_append(path, **kw): resume=True, lock_wait=30; it continues the
    last segment when that is clean, of this version and registry, under
    reset_bytes, and the open repaired nothing
  frames(src, start=0, end=None, verify=True, settle=0) -> Frame...
  frames_reverse(src, end, verify=False)  peek(src, off)
  resync(src, pos, end=None)  complete_end(src, size=None)  is_boundary(src, off)
  verify_back(src, end, stop=0): the frames back from boundary `end` to the
    first at or before `stop` all whole, CRCs good (no BAD region there)
  Decoder(strict=False); Decoder.at(src, off, strict=False)
    .records(src, frame, kinds=None, where=None) -> (pos, rec)...
    .skip(frame, src=None)  .segment .broken .unsupported .stats
  records_reverse(src, end, start=0, kinds=None, where=None, stats=None)
  iter_records(path_or_fh, kinds=None, where=None, stats=None)
  open_feed(path) -> (binary file, 'ebr'|'jsonl')  main(argv=None)
  `src` is a path (read with os.pread), a bytes-like object, or any other
  object with seek/read (never its fileno()); source(src) opens one for a
  run of calls (`with source(path) as s:`), each of which would otherwise
  open its own.
"""
import errno
import gzip
import io
import json
import math
import os
import struct
import sys
import time
import zlib as _zlib

try:
    import fcntl
except ImportError:  # pragma: no cover - not POSIX: write unlocked
    fcntl = None

MAGIC = b"\x89EBR"
FORMAT = "ebr"
VERSION = 1
SEGMENT = 1
DATA = 2
BAD = -1                    # pseudo-kind of a corrupt region; never in a header
F_ZLIB = 1
KINDS = ("meta", "exit", "truncated", "conn", "netio", "submit", "residency",
         "residency_totals", "stop", "tcp", "accept")
BIT_NOEVENT = 1 << 30
BIT_OTHER = 1 << 31
DEFAULT_RESET_BYTES = 2 << 20
STR_ID_SIZE = 2
MAX_PAYLOAD = 64 << 20
MAX_FIELDS = 4096
MAX_STRINGS = 65535
MAX_SHAPES = 65535
MAP_AFTER = 8

HDR = struct.Struct("<4sBBHIIddIII")
TRL = struct.Struct("<II")
HDR_SIZE = HDR.size         # 44
TRL_SIZE = TRL.size         # 8
OVERHEAD = HDR_SIZE + TRL_SIZE
REPAIR_MAX = MAX_PAYLOAD + OVERHEAD

T_NULL, T_BOOL, T_I8, T_I16, T_I32, T_I64, T_F64, T_STR, T_OBJ, T_LIST, T_BIG = range(11)
SLOT_NAMES = ("NULL", "BOOL", "I8", "I16", "I32", "I64", "F64", "STR", "OBJ", "LIST", "BIG")

WRITER_STATS = ("records", "frames", "segments", "bytes", "writes", "encode_errors",
                "write_errors", "lost_records", "repaired_bytes", "zlib_frames")
DECODER_STATS = ("frames", "records", "filtered", "bad_records", "bad_frames",
                 "lost_frames", "lost_records", "unsupported_frames")

_INF = float("inf")
_NAN = float("nan")
_I64 = 1 << 63
_HDR40 = struct.Struct("<4sBBHIIddII")
_U32 = struct.Struct("<I")
_F64 = struct.Struct("<d")
_FIXED = ("", "?", "b", "h", "i", "q", "d")
_SIG_CAP = 16384            # cached encoder signatures
_CODE_CAP = 4096            # compiled layouts per cache
_WIN = 1 << 20              # read-ahead window, at most
_WIN0 = 16 << 10            # ... after a jump; it grows 4x a read while the reads move on
_PENDING_MAX = 16 << 20     # unwritten bytes kept for a retry
_FRAME_ROOM = MAX_PAYLOAD - 64
_KIND_BIT = dict((k, 1 << i) for i, k in enumerate(KINDS) if i < 30)
_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_crc = _zlib.crc32
# crc32(data + its own crc32, little-endian) for any data: a stored CRC and the
# bytes it covers, checked in one call
_RESIDUE = 0x2144DF1C


class Error(ValueError):
    """Anything wrong with an EBR file or its use."""


class FrameError(Error):
    """Malformed bytes: a bad frame, definition or record."""


class StateError(Error):
    """The decoder's tables do not match the frame (a gap, or the wrong offset)."""


class RecordTooLarge(Error):
    """A record that cannot fit one frame or one segment's tables."""


class Busy(Error):
    """Another writer holds the file's lock."""


class _Full(Exception):
    """Internal: a string or shape table is full; the writer starts a segment."""


class Prefix(str):
    """A `where` test: the field is a str starting with this prefix."""
    __slots__ = ()

    def __repr__(self):
        return "Prefix(%s)" % str.__repr__(self)


# ---------------------------------------------------------------------------
# varints
# ---------------------------------------------------------------------------

def _uv_slow(n):
    out = bytearray()
    while n > 0x7f:
        out.append((n & 0x7f) | 0x80)
        n >>= 7
    out.append(n)
    return bytes(out)


_UV = [_uv_slow(_i) for _i in range(1 << 14)]


def _uv(n):
    """Unsigned LEB128."""
    if 0 <= n < 16384:
        return _UV[n]
    if n < 0:
        raise ValueError("negative varint")
    return _uv_slow(n)


def _ruv(b, p):
    """(value, next offset) of the varint at `p`; IndexError past the end."""
    c = b[p]
    if c < 0x80:
        return c, p + 1
    v = c & 0x7f
    s = 7
    while True:
        p += 1
        c = b[p]
        if c < 0x80:
            return v | (c << s), p + 1
        v |= (c & 0x7f) << s
        s += 7
        if s > 63:
            raise FrameError("varint longer than 10 bytes")


_BYTE = [bytes((_i,)) for _i in range(256)]


# ---------------------------------------------------------------------------
# encoder
# ---------------------------------------------------------------------------

class _Interner(dict):
    """str -> id. A hit stays in C (`I[v]`); a miss defines the string."""
    __slots__ = ("defs", "n", "cap")

    def __missing__(self, s):
        k = s if type(s) is str else str.__str__(s)
        orig = k
        try:
            b = k.encode("utf-8")
        except UnicodeEncodeError:
            # what json.loads(json.dumps(s)) gives: split surrogate pairs
            # merged, lone surrogates kept
            k = json.loads(json.dumps(k))
            i = dict.get(self, k)
            if i is not None:
                dict.__setitem__(self, orig, i)
                return i
            b = k.encode("utf-8", "surrogatepass")
        n = self.n
        if n >= self.cap:
            raise _Full("string table full")
        self.defs += b"\x01" + _uv(len(b)) + b
        self.n = n + 1
        dict.__setitem__(self, k, n)
        if orig is not k:
            dict.__setitem__(self, orig, n)
        return n


_SHAPE, _MAP, _CONV = 1, 2, 3


class _Sig(object):
    __slots__ = ("mode", "K", "layout")


def _type_error(t):
    return TypeError("Object of type %s is not JSON serializable" % t.__name__)


def _int_slot(v):
    if -128 <= v <= 127:
        return T_I8
    if -32768 <= v <= 32767:
        return T_I16
    if -2147483648 <= v <= 2147483647:
        return T_I32
    if -_I64 <= v < _I64:
        return T_I64
    return T_BIG


_NONE_T = type(None)


def _slot_of(t, v):
    if t is _NONE_T:
        return T_NULL
    if t is bool:
        return T_BOOL
    if t is str:
        return T_STR
    if t is float:
        return T_F64
    if t is int:
        return _int_slot(v)
    if t is dict:
        return T_OBJ
    if t is list or t is tuple:
        return T_LIST
    if issubclass(t, str):
        return T_STR
    if issubclass(t, int):
        return _int_slot(v)
    if issubclass(t, float):
        return T_F64
    if issubclass(t, (list, tuple)):
        return T_LIST
    if issubclass(t, dict):
        return T_OBJ
    raise _type_error(t)


def _conv_key(k):
    """A dict key as json.dumps writes it."""
    if isinstance(k, str):
        return str.__str__(k)
    if isinstance(k, float):
        if k != k:
            return "NaN"
        if k == _INF:
            return "Infinity"
        if k == -_INF:
            return "-Infinity"
        return float.__repr__(k)
    if k is True:
        return "true"
    if k is False:
        return "false"
    if k is None:
        return "null"
    if isinstance(k, int):
        return int.__repr__(k)
    raise TypeError("keys must be str, int, float, bool or None, not %s" % type(k).__name__)


def _convert(d):
    out = {}
    for k, v in d.items():
        out[_conv_key(k)] = v           # a collision keeps the first position, last value
    return out


def _big(v):
    n = (v + (v < 0)).bit_length() // 8 + 1
    return _uv(n) + int.to_bytes(v, n, "little", signed=True)


_ENC_CACHE = {}
_FT = type(_uv)
_SID_CODES = ("B", "H", "BH")


def _sid_args(sid):
    """(width, s0, s1): uv(sid) as one or two struct fields."""
    if sid < 128:
        return 1, sid, 0
    if sid < 16384:
        return 2, (sid & 0x7f) | 0x80 | ((sid >> 7) << 8), 0
    return 3, (sid & 0x7f) | 0x80, ((sid >> 7) & 0x7f) | 0x80 | ((sid >> 14) << 8)


def _enc_tmpl(layout, sis, sidw):
    """The packer of one layout: enc(v, d, ctx, I, K, obj, lst, big, SG,
    s0, s1) -> the dict's body, shape id included (s0, s1: uv(sid) as struct
    fields). `v` is the dict's values() view, unpacked in field order. A
    nested dict looks its signature up in SG (the nested cache's get) and
    falls back to obj() on a miss, or on a struct.error so obj() can widen it.

    The source holds only fixed tokens, integers and struct codes; keys and
    strings arrive as arguments (bound per signature as defaults)."""
    key = (layout, sis, sidw)
    fn = _ENC_CACHE.get(key)
    if fn is not None:
        return fn
    sid = ["s0", "s1"][:2 if sidw == 3 else 1]
    ncodes, nargs, scodes, sargs, names, parts, pre = [], [], [], [], [], [], []
    sc = "H" if sis == 2 else "I"
    for i, t in enumerate(layout):
        a = "a%d" % i
        names.append(a if t != T_NULL else "_")
        if t == T_NULL:
            continue
        if t == T_STR:
            scodes.append(sc)
            sargs.append("I[%s]" % a)
        elif T_BOOL <= t <= T_F64:
            ncodes.append(_FIXED[t])
            nargs.append(a)
        elif t == T_OBJ:
            pre.append("    w%d = %s.values()\n"
                       "    f = SG((*%s, *map(type, w%d)))\n"
                       "    if f is None:\n"
                       "        x%d = obj(%s, K[%d])\n"
                       "    else:\n"
                       "        try:\n"
                       "            x%d = f(w%d, %s, K[%d])\n"
                       "        except SE:\n"
                       "            x%d = obj(%s, K[%d])\n"
                       % (i, a, a, i, i, a, i, i, i, a, i, i, a, i))
            parts.append("x%d" % i)
        elif t == T_LIST:
            parts.append("lst(%s, K[%d])" % (a, i))
        elif t == T_BIG:
            parts.append("big(%s)" % a)
        else:
            raise FrameError("bad slot type %r" % (t,))
    # the fixed part: numeric slots in field order, then STR slots
    codes = [_SID_CODES[sidw - 1]] + ncodes + scodes
    args = sid + nargs + sargs
    st = struct.Struct("<" + "".join(codes))
    if layout and all(T_BOOL <= t <= T_F64 for t in layout):
        # every value packs as it is: hand the view to struct
        body = "    return P(%s, *v)\n" % ", ".join(sid)
    else:
        parts.insert(0, ("P(*(%s,))" if len(args) > 250 else "P(%s)") % ", ".join(args))
        expr = parts[0] if len(parts) == 1 else 'b"".join((%s,))' % ", ".join(parts)
        unpack = "    (%s,) = v\n" % ", ".join(names) if names else ""
        body = "%s%s    return %s\n" % (unpack, "".join(pre), expr)
    src = "def enc(v, d, ctx, I, K, obj, lst, big, SG, s0, s1):\n" + body
    ns = {"P": st.pack, "SE": struct.error, "map": map, "type": type, "__builtins__": {}}
    exec(compile(src, "<ebr-encoder>", "exec"), ns)
    fn = ns["enc"]
    if len(_ENC_CACHE) >= _CODE_CAP:
        _ENC_CACHE.clear()
    _ENC_CACHE[key] = fn
    return fn


class _Encoder(object):
    """Records -> bodies, with the string and shape tables of one segment.

    record(d) returns the body of one record. Definitions a body needs are
    appended to `defs` (shared with the interner) as they are made, and the
    writer emits them with the frame: a record that fails part-way leaves
    valid, unused definitions behind and no body. `I.n` and `nshapes` are the
    table sizes; reset() starts an empty epoch (a new segment)."""

    def __init__(self, str_id_size=STR_ID_SIZE):
        self.sis = str_id_size
        self.cap = MAX_STRINGS if str_id_size == 2 else 0xffffffff
        self.defs = bytearray()
        self.nshapes = 0
        self.I = None
        _build_encoder(self)
        self.reset()


def _build_encoder(E):
    """The encoder's functions, as closures over its state: the hot paths
    read cells, not attributes.

    A dict is identified by its flat signature (*keys, *value types). `top`
    and `sigs` map a signature straight to the function that encodes it in
    this epoch (cleared at every reset); `tsig`/`nsig` keep what outlives an
    epoch: the mode (shape, MAP, convert-keys) and the layout, whose int
    widths only grow."""
    sis = E.sis
    defs = E.defs
    top = {}                    # signature -> fn(values, d, ctx), top-level records
    sigs = {}                   # signature -> fn(values, d, ctx), nested dicts
    tsig = {}                   # signature -> _Sig, top-level
    nsig = {}                   # signature -> _Sig, nested
    ctxs = {}                   # context -> key tuples seen (the MAP rule)
    shape_ids = {}              # (keys, layout) -> sid, this epoch
    sigs_get = sigs.get
    I = None

    def reset():
        nonlocal I
        I = _Interner()
        I.defs = defs
        I.n = 0
        I.cap = E.cap
        E.I = I
        del defs[:]
        shape_ids.clear()
        top.clear()
        sigs.clear()
        E.nshapes = 0

    def load(strings, shapes):
        reset()
        for i, s in enumerate(strings):
            if s not in I:
                dict.__setitem__(I, s, i)
        I.n = len(strings)
        for sh in shapes:
            E.nshapes += 1
            shape_ids.setdefault(sh, E.nshapes)

    def define(sig):
        """The packer of `sig` in this epoch, defining its shape if new."""
        sk = (sig.K, sig.layout)
        sid = shape_ids.get(sk)
        if sid is None:
            parts = [b"\x02", _uv(len(sig.K))]
            for k, t in zip(sig.K, sig.layout):
                parts.append(_uv(I[k]))
                parts.append(_BYTE[t])
            if E.nshapes >= MAX_SHAPES:
                raise _Full("shape table full")
            defs.extend(b"".join(parts))
            E.nshapes += 1
            sid = E.nshapes
            shape_ids[sk] = sid
        w, s0, s1 = _sid_args(sid)
        tmpl = _enc_tmpl(sig.layout, sis, w)
        return _FT(tmpl.__code__, tmpl.__globals__, "enc",
                   (I, sig.K, obj, lst, _big, sigs_get, s0, s1))

    def prep(cache, slow, key, vw, ctx, is_top):
        sig = slow.get(key)
        if sig is None:
            n = len(key) // 2
            keys = key[:n]
            mode = _SHAPE
            for k in keys:
                if not isinstance(k, str):
                    mode = _CONV
                    break
            if mode == _SHAPE:
                if n > MAX_FIELDS:
                    mode = _MAP
                elif not is_top:
                    seen = ctxs.get(ctx)
                    if seen is None:
                        if len(ctxs) >= _SIG_CAP:
                            ctxs.clear()
                        seen = ctxs[ctx] = set()
                    if keys not in seen:
                        if len(seen) >= MAP_AFTER:
                            mode = _MAP
                        else:
                            seen.add(keys)
            sig = _Sig()
            sig.mode = mode
            sig.K = keys
            sig.layout = tuple(map(_slot_of, key[n:], vw)) if mode == _SHAPE else None
            if len(slow) >= _SIG_CAP:
                slow.clear()
                cache.clear()
                if not is_top:
                    ctxs.clear()
            slow[key] = sig
        if sig.mode == _SHAPE:
            fn = define(sig)
        elif sig.mode == _MAP:
            fn = map_top if is_top else map_nested
        else:
            fn = conv_top if is_top else conv_nested
        if len(cache) >= _SIG_CAP:
            cache.clear()
        cache[key] = fn
        return fn

    def widen(cache, slow, key, vals):
        """Widen the int slots `vals` overflowed (never narrows): a new
        layout, so a new shape."""
        sig = slow.get(key)
        if sig is None or sig.mode != _SHAPE:
            return False
        lay = list(sig.layout)
        grew = False
        for i, t in enumerate(lay):
            if T_I8 <= t <= T_I64:
                need = _int_slot(vals[i])
                if need > t:
                    lay[i] = need
                    grew = True
        if grew:
            sig.layout = tuple(lay)
            cache[key] = define(sig)
        return grew

    def record(d):
        try:
            vw = d.values()
            key = (*d, *map(type, vw))
            fn = top.get(key)
            if fn is None:
                fn = prep(top, tsig, key, vw, None, True)
            try:
                return fn(vw, d, None)
            except struct.error:
                if widen(top, tsig, key, tuple(vw)):
                    return record(d)
                raise
        except RecursionError:
            raise ValueError("Circular reference detected")

    def obj(d, ctx):
        vw = d.values()
        key = (*d, *map(type, vw))
        fn = sigs.get(key)
        if fn is None:
            fn = prep(sigs, nsig, key, vw, ctx, False)
        try:
            return fn(vw, d, ctx)
        except struct.error:
            if widen(sigs, nsig, key, tuple(vw)):
                return obj(d, ctx)
            raise

    def map_top(_v, d, ctx):
        return mapf(d, None)

    def map_nested(_v, d, ctx):
        return mapf(d, ctx)

    def conv_top(_v, d, ctx):
        return record(_convert(d))

    def conv_nested(_v, d, ctx):
        return obj(_convert(d), ctx)

    def mapf(d, ctx):
        sub = (ctx, "*")
        out = [b"\x00", _uv(len(d))]
        ap = out.append
        for k, v in d.items():
            i = I[k]
            ap(_UV[i] if i < 16384 else _uv_slow(i))
            t = type(v)
            if t is int and -_I64 <= v < _I64:
                z = (v << 1) ^ (v >> 63)
                ap(b"\x03" + (_UV[z] if z < 16384 else _uv_slow(z)))
            elif t is str:
                i = I[v]
                ap(b"\x06" + (_UV[i] if i < 16384 else _uv_slow(i)))
            else:
                ap(tag(v, sub))
        return b"".join(out)

    def lst(v, ctx):
        n = len(v)
        if not n:
            return b"\x00"
        out = [_uv(n)]
        ap = out.append
        for x in v:
            t = type(x)
            if t is str:
                i = I[x]
                ap(b"\x06" + (_UV[i] if i < 16384 else _uv_slow(i)))
            elif t is dict:
                xv = x.values()
                f = sigs_get((*x, *map(type, xv)))
                if f is None:
                    ap(b"\x07" + obj(x, ctx))
                else:
                    try:
                        ap(b"\x07" + f(xv, x, ctx))
                    except struct.error:
                        ap(b"\x07" + obj(x, ctx))
            elif t is int and -_I64 <= x < _I64:
                z = (x << 1) ^ (x >> 63)
                ap(b"\x03" + (_UV[z] if z < 16384 else _uv_slow(z)))
            else:
                ap(tag(x, ctx))
        return b"".join(out)

    def tag(v, ctx):
        t = type(v)
        if t is str:
            return b"\x06" + _uv(I[v])
        if t is int:
            if -_I64 <= v < _I64:
                return b"\x03" + _uv((v << 1) ^ (v >> 63))
            return b"\x04" + _big(v)
        if t is float:
            return b"\x05" + _F64.pack(v)
        if v is None:
            return b"\x00"
        if v is True:
            return b"\x02"
        if v is False:
            return b"\x01"
        if t is dict:
            return b"\x07" + obj(v, ctx)
        if t is list or t is tuple:
            return b"\x08" + lst(v, ctx)
        if isinstance(v, str):
            return b"\x06" + _uv(I[v])
        if isinstance(v, int):
            if -_I64 <= v < _I64:
                return b"\x03" + _uv((v << 1) ^ (v >> 63))
            return b"\x04" + _big(v)
        if isinstance(v, float):
            return b"\x05" + _F64.pack(v)
        if isinstance(v, dict):
            return b"\x07" + obj(v, ctx)
        if isinstance(v, (list, tuple)):
            return b"\x08" + lst(v, ctx)
        raise _type_error(t)

    E.reset = reset
    E.load = load
    E.record = record
    E.obj = obj


# ---------------------------------------------------------------------------
# decoder tables
# ---------------------------------------------------------------------------

_DEC_CACHE = {}
_TEMPLATE_AT = 32           # fields from which a shape decodes by template copy


def _fixed_index(layout):
    """Where each field's value sits in the unpacked fixed part: the numeric
    slots (BOOL..F64) in field order, then the STR slots in field order;
    None for NULL and var slots."""
    nn = 0
    for t in layout:
        if T_BOOL <= t <= T_F64:
            nn += 1
    out = []
    jn, js = 0, nn
    for t in layout:
        if T_BOOL <= t <= T_F64:
            out.append(jn)
            jn += 1
        elif t == T_STR:
            out.append(js)
            js += 1
        else:
            out.append(None)
    return out


_KIDS_MAX = 48              # OBJ slots of one shape decoded inline, at most
# The slot type a layout's decoder is generated for: a number of any width is
# an F64 to it (the widths are the struct's, bound per shape), so shapes that
# differ only as an int slot widens share one code object
_PAT = (T_NULL, T_F64, T_F64, T_F64, T_F64, T_F64, T_F64, T_STR, T_OBJ, T_LIST, T_BIG)
_STRUCTS = {}


def _fixed_struct(layout, sis):
    """The struct of a layout's fixed part: its numeric slots in field order,
    then its STR slots."""
    key = (layout, sis)
    st = _STRUCTS.get(key)
    if st is None:
        num = [_FIXED[t] for t in layout if T_BOOL <= t <= T_F64]
        nstr = sum(1 for t in layout if t == T_STR)
        st = struct.Struct("<" + "".join(num) + ("H" if sis == 2 else "I") * nstr)
        if len(_STRUCTS) >= _CODE_CAP:
            _STRUCTS.clear()
        _STRUCTS[key] = st
    return st


def _obj_lines(k, kid, target):
    """The lines that decode an OBJ slot into `target`. `kid` is None, or the
    pattern of the flat shape the slot is taken to hold (its id, struct,
    1 + size and keys bound as C<k>, U<k>, Z<k>, K<k>): a dict of that shape
    is then decoded right here, and any other through F as ever."""
    gen = "%s, p = F[b[p]](b, p + 1)" % target
    if kid is None:
        return [gen]
    cidx = _fixed_index(kid)
    items = []
    for i, t in enumerate(kid):
        if t == T_NULL:
            items.append("K%d[%d]: None" % (k, i))
        elif t == T_STR:
            items.append("K%d[%d]: S[c[%d]]" % (k, i, cidx[i]))
        else:
            items.append("K%d[%d]: c[%d]" % (k, i, cidx[i]))
    out = ["if b[p] == C%d:" % k]
    if any(t != T_NULL for t in kid):
        out.append("    c = U%d(b, p + 1)" % k)
    return out + ["    %s = {%s}" % (target, ", ".join(items)),
                  "    p = p + Z%d" % k, "else:", "    " + gen]


def _dec_fn(layout, sis, kids=None):
    """(struct, dec) for one layout. A bound copy of dec (see _compile_shape)
    decodes one dict of the shape from its fixed part at `p` on:
    dec(b, p) -> (dict, next offset). It unpacks the fixed part itself, unless
    the caller already has: dec(b, p, st.unpack_from(b, p)). The var parts
    follow in field order; a dict among them is decoded by what its shape id's
    first byte names in the segment's table (F, see _bind), one call deep --
    or, for an OBJ slot `kids` gives the layout of (one per OBJ slot, None for
    none), inline when it is of that shape (_obj_lines). Wide shapes copy a
    template dict, which already holds the keys in order and None for NULL
    slots, and fill it in bulk. The code is the layout's pattern's (_PAT)."""
    if len(layout) > MAX_FIELDS:
        raise FrameError("shape of %d fields" % len(layout))
    for t in layout:
        if type(t) is not int or not T_NULL <= t <= T_BIG:
            raise FrameError("bad slot type %r" % (t,))
    st = _fixed_struct(layout, sis)
    pat = tuple([_PAT[t] for t in layout])
    kp = None if kids is None else tuple(
        None if c is None else tuple([_PAT[t] for t in c]) for c in kids)
    key = (pat, kp)
    fn = _DEC_CACHE.get(key)
    if fn is not None:
        return st, fn
    num = sum(1 for t in pat if t == T_F64)
    nstr = sum(1 for t in pat if t == T_STR)
    idx = _fixed_index(pat)
    params = ["b", "p", "a", "U", "SZ", "S", "K", "F", "lst", "big", "TM", "KN", "KS"]
    kp = kp or ()
    nobj = [0]

    def var(t, target):
        if t == T_LIST:
            return ["%s, p = lst(b, p)" % target]
        if t == T_BIG:
            return ["%s, p = big(b, p)" % target]
        k = nobj[0]
        nobj[0] = k + 1
        kid = kp[k] if k < len(kp) else None
        if kid is not None:
            params.extend(("C%d" % k, "U%d" % k, "Z%d" % k, "K%d" % k))
        return _obj_lines(k, kid, target)
    lines = []
    if num or nstr:
        lines += ["if a is None:", "    a = U(b, p)", "p = p + SZ"]
    if len(pat) >= _TEMPLATE_AT:
        lines.append("d = TM.copy()")
        if num:
            lines.append("d.update(zip(KN, a))")
        if nstr:
            lines.append("d.update(zip(KS, map(S.__getitem__, a[%d:])))" % num)
        for i, t in enumerate(pat):
            if t >= T_OBJ:
                lines += var(t, "x")
                lines.append("d[K[%d]] = x" % i)
        lines.append("return d, p")
    else:
        items = []
        for i, t in enumerate(pat):
            if t == T_NULL:
                items.append("K[%d]: None" % i)
            elif t == T_STR:
                items.append("K[%d]: S[a[%d]]" % (i, idx[i]))
            elif t == T_F64:
                items.append("K[%d]: a[%d]" % (i, idx[i]))
            else:
                lines += var(t, "x%d" % i)
                items.append("K[%d]: x%d" % (i, i))
        lines.append("return {%s}, p" % ", ".join(items))
    src = ("def dec(%s):\n" % ", ".join(params)
           + "".join("    %s\n" % x for x in lines))
    ns = {"zip": zip, "map": map, "__builtins__": {}}
    exec(compile(src, "<ebr-decoder>", "exec"), ns)
    fn = ns["dec"]
    if len(_DEC_CACHE) >= _CODE_CAP:
        _DEC_CACHE.clear()
    _DEC_CACHE[key] = fn
    return st, fn


class _Shape(object):
    __slots__ = ("ids", "layout", "sid", "K", "unpack", "size", "fn")


def _compile_shape(T, sh, b=None, p=None):
    """Bind the shape's decoder to its keys and to this segment's tables, and
    put it where its shape id is looked up (T.F, T.FS). `b`, `p`: a record of
    the shape, its fixed part at `p` -- the first one decoded -- whose OBJ
    slots holding a flat shape have the decoder take each to hold that shape
    (_kids): a writer gives a key's dicts one shape while its key set holds."""
    lay = sh.layout
    kids = None
    if b is not None and T_OBJ in lay:
        kids = _kids(T, lay, _fixed_struct(lay, T.sis).size, b, p)
    st, fn = _dec_fn(lay, T.sis, None if kids is None else tuple(
        None if k is None else k[1].layout for k in kids))
    S = T.S
    K = tuple([S[i] for i in sh.ids])
    TM = dict.fromkeys(K)
    KN = tuple([k for k, t in zip(K, lay) if T_BOOL <= t <= T_F64])
    KS = tuple([k for k, t in zip(K, lay) if t == T_STR])
    sh.K = K
    sh.unpack = st.unpack_from
    sh.size = st.size
    args = [None, sh.unpack, st.size, S, K, T.F, T.lst, T.big, TM, KN, KS]
    for k in kids or ():
        if k is not None:
            ch = k[1]
            if ch.fn is None:
                _compile_shape(T, ch)
            args += [k[0], ch.unpack, 1 + ch.size, ch.K]
    sh.fn = f = _FT(fn.__code__, fn.__globals__, "dec", tuple(args))
    T.FS[sh.sid] = f
    if sh.sid < 0x80:
        T.F[sh.sid] = f
    return f


def _kids(T, lay, size, b, p):
    """For each OBJ slot of the record of layout `lay` whose fixed part
    (`size` bytes) is at `p`: (id, shape) of the dict it holds, when that
    shape is flat (no var slot) and narrow (below _TEMPLATE_AT fields), else
    None. None when no slot's is, or the record does not decode."""
    out = []
    q = p + size
    F = T.F
    shapes = T.shapes
    try:
        for t in lay:
            if t == T_OBJ:
                c = b[q]
                kid = None
                if 0 < c < 0x80 and c <= len(shapes) and len(out) < _KIDS_MAX:
                    ch = shapes[c - 1]
                    if len(ch.layout) < _TEMPLATE_AT and max(ch.layout or (0,)) < T_OBJ:
                        kid = (c, ch)
                out.append(kid)
                _x, q = F[c](b, q + 1)
            elif t == T_LIST:
                _x, q = T.lst(b, q)
            elif t == T_BIG:
                _x, q = T.big(b, q)
    except Exception:
        return None
    return out if any(k is not None for k in out) else None


def _stub(T, sh):
    """A defined shape's decoder until its first use compiles it."""
    def dec(b, p, a=None):
        f = sh.fn
        if f is None:
            f = _compile_shape(T, sh, b, p)
        return f(b, p, a)
    return dec


class _Tables(object):
    """One segment's string and shape tables, and the decoders bound to them."""

    def __init__(self, sis, reg):
        self.sis = sis
        self.reg = reg
        self.S = []
        self.shapes = []
        self.masks = {}
        self.plans = {}
        _bind(self)

    def mask(self, kinds):
        """(mask, rest) of `kinds` under this segment's registry (exact): the
        frame bits a record of one of them sets, and those only a record of
        none of them sets. A frame with no bit of `mask` holds none of them;
        one with no bit of `rest` holds nothing else. A registry that is not
        a list of names masks every frame in, and none as holding only them."""
        hit = self.masks.get(id(kinds))
        if hit is not None and hit[0] is kinds:
            return hit[1]
        reg = self.reg
        if not isinstance(reg, list) or not all(isinstance(k, str) for k in reg):
            r = (0xffffffff, 0xffffffff)
        else:
            m = kind_mask(kinds, reg)[0]
            r = (m, (~m & 0xffffffff) | BIT_OTHER)
        if len(self.masks) >= 64:
            self.masks.clear()
        self.masks[id(kinds)] = (kinds, r)
        return r

    def plan(self, kinds, where):
        key = (id(kinds), id(where))
        hit = self.plans.get(key)
        if hit is not None and hit.kinds is kinds and hit.where is where:
            return hit
        p = _Plan(kinds, where)
        if len(self.plans) >= 64:
            self.plans.clear()
        self.plans[key] = p
        return p


def _bind(T):
    """The var-part decoders of one segment, closed over its tables, and the
    tables a dict is decoded through by its shape id: T.FS by the id, and T.F
    by the id's first byte -- a decoder of the dict that follows it, for an id
    below 0x80 (the dict's own shape, or MAP for 0), else one that reads the
    rest of the id and looks it up in T.FS. A dict is one call deep from F."""
    S = T.S
    ruv = _ruv
    unpack_d = _F64.unpack_from
    from_bytes = int.from_bytes
    F = T.F = [None] * 0x100
    FS = T.FS = [None]

    def big(b, p):
        n = b[p]
        if n < 0x80:
            p += 1
        else:
            n, p = ruv(b, p)
        q = p + n
        if q > len(b):
            raise FrameError("big int past the payload")
        return from_bytes(b[p:q], "little", signed=True), q

    def tag(b, p):
        t = b[p]
        if t == 6:
            c = b[p + 1]
            if c < 0x80:
                return S[c], p + 2
            i, p = ruv(b, p + 1)
            return S[i], p
        if t == 3:
            c = b[p + 1]
            if c < 0x80:
                return (c >> 1) ^ -(c & 1), p + 2
            z, p = ruv(b, p + 1)
            return (z >> 1) ^ -(z & 1), p
        if t == 5:
            return unpack_d(b, p + 1)[0], p + 9
        if t == 0:
            return None, p + 1
        if t == 1:
            return False, p + 1
        if t == 2:
            return True, p + 1
        if t == 7:
            return obj(b, p + 1)
        if t == 8:
            return lst(b, p + 1)
        if t == 4:
            return big(b, p + 1)
        raise FrameError("bad tag %d" % t)

    def lst(b, p):
        n = b[p]
        if n < 0x80:
            p += 1
        else:
            n, p = ruv(b, p)
        out = []
        ap = out.append
        for _ in range(n):
            if b[p] == 6:
                c = b[p + 1]
                if c < 0x80:
                    ap(S[c])
                    p += 2
                    continue
            v, p = tag(b, p)
            ap(v)
        return out, p

    def mapd(b, p):
        n = b[p]
        if n < 0x80:
            p += 1
        else:
            n, p = ruv(b, p)
        d = {}
        for _ in range(n):
            c = b[p]
            if c < 0x80:
                p += 1
            else:
                c, p = ruv(b, p)
            k = S[c]
            t = b[p]
            if t == 3:
                c = b[p + 1]
                if c < 0x80:
                    d[k] = (c >> 1) ^ -(c & 1)
                    p += 2
                    continue
            v, p = tag(b, p)
            d[k] = v
        return d, p

    def obj(b, p):
        return F[b[p]](b, p + 1)

    def wide(b, p):
        c, p = ruv(b, p - 1)
        return FS[c](b, p)

    def undefined(b, p):
        raise FrameError("shape %d is not defined" % b[p - 1])

    F[0] = FS[0] = mapd
    for c in range(1, 0x80):
        F[c] = undefined
    for c in range(0x80, 0x100):
        F[c] = wide
    T.obj, T.lst, T.big, T.tag = obj, lst, big, tag


def _apply_defs(T, b, p, end):
    """Append the definitions in b[p:end] to T, validating every one."""
    S = T.S
    shapes = T.shapes
    append = S.append
    decode = bytes.decode
    # A frame's new strings are mostly ASCII: decoded together, each is a
    # slice of that (a definitions run with a byte >= 0x80 is decoded apart)
    p0 = p
    try:
        txt = decode(b[p:end], "ascii")
    except UnicodeDecodeError:
        txt = None
    while p < end:
        op = b[p]
        if op == 1:
            n = b[p + 1]
            if n < 0x80:
                p += 2
            else:
                n, p = _ruv(b, p + 1)
            q = p + n
            if q > end:
                raise FrameError("string past the definitions")
            if txt is None:
                append(decode(b[p:q], "utf-8", "surrogatepass"))
            else:
                append(txt[p - p0:q - p0])
            p = q
        elif op == 2:
            nf, p = _ruv(b, p + 1)
            if nf > MAX_FIELDS:
                raise FrameError("shape of %d fields" % nf)
            ns = len(S)
            ids, lay = [], []
            for _ in range(nf):
                k, p = _ruv(b, p)
                t = b[p]
                p += 1
                if k >= ns:
                    raise FrameError("shape key %d is not a defined string" % k)
                if t > T_BIG:
                    raise FrameError("bad slot type %d" % t)
                ids.append(k)
                lay.append(t)
            if p > end:
                raise FrameError("shape past the definitions")
            if len(set([S[k] for k in ids])) != nf:
                raise FrameError("shape with a repeated key")
            sh = _Shape()
            sh.ids = tuple(ids)
            sh.layout = tuple(lay)
            sh.fn = None
            shapes.append(sh)
            sh.sid = sid = len(shapes)
            dec = _stub(T, sh)
            T.FS.append(dec)
            if sid < 0x80:
                T.F[sid] = dec
        else:
            raise FrameError("bad definition opcode %d" % op)
    if p != end:
        raise FrameError("definitions overrun")


# ---------------------------------------------------------------------------
# filters
# ---------------------------------------------------------------------------

def _in(v, kinds):
    try:
        return v in kinds
    except TypeError:               # an unhashable event is in no set of names
        return False


def _test(t, v):
    if isinstance(t, (set, frozenset)):
        return str(v) in t
    if isinstance(t, Prefix):
        return isinstance(v, str) and v.startswith(t)
    if callable(t):
        return bool(t(v))
    raise TypeError("a where test is a set, a Prefix or a callable, not %s"
                    % type(t).__name__)


def _branches(where):
    if where is None:
        return None
    out = [where] if isinstance(where, dict) else list(where)
    for br in out:
        if not isinstance(br, dict):
            raise TypeError("where is a dict or a list of dicts")
        for t in br.values():
            if not (isinstance(t, (set, frozenset, Prefix)) or callable(t)):
                raise TypeError("a where test is a set, a Prefix or a callable, not %s"
                                % type(t).__name__)
    return out


def match_where(where, rec):
    """The reference semantics of `where`: one dict (every test passes) or a
    list of dicts (any dict passes). A test sees rec.get(field), so a missing
    field is None: a set passes if str(value) is in it, a Prefix if the value
    is a str starting with it, a callable if fn(value) is truthy."""
    if where is None:
        return True
    for br in ([where] if isinstance(where, dict) else where):
        for f, t in br.items():
            if not _test(t, rec.get(f)):
                break
        else:
            return True
    return False


def _post(rec, kinds, where):
    if kinds is not None and not _in(rec.get("event"), kinds):
        return False
    return where is None or match_where(where, rec)


def _testfn(t):
    if isinstance(t, (set, frozenset)):
        return lambda v: str(v) in t
    if isinstance(t, Prefix):
        p = str.__str__(t)
        return lambda v: isinstance(v, str) and v.startswith(p)
    return lambda v: bool(t(v))


class _Probe(object):
    """A filter compiled against one shape: what can be decided from the
    fixed slots, before the record is built."""
    __slots__ = ("reject", "full", "kj", "kstr", "wb")


_REJECT = _Probe()
_REJECT.reject = True


class _Plan(object):
    """kinds/where compiled against one segment."""

    def __init__(self, kinds, where):
        self.kinds = kinds
        self.where = where
        self.br = _branches(where)
        self.fns = None if self.br is None else [
            [(f, _testfn(t), {}) for f, t in br.items()] for br in self.br]
        self.kcache = {}
        self.probes = {}

    def probe(self, sh):
        pr = self.probes.get(sh)
        if pr is None:
            pr = self._probe(sh)
            if len(self.probes) >= _SIG_CAP:
                self.probes.clear()
            self.probes[sh] = pr
        return pr

    def _probe(self, sh):
        idx = {}
        for k, t, j in zip(sh.K, sh.layout, _fixed_index(sh.layout)):
            idx[k] = (t, j)
        pr = _Probe()
        pr.reject = pr.full = pr.kstr = False
        pr.kj = pr.wb = None
        if self.kinds is not None:
            e = idx.get("event")
            if e is None or e[0] == T_NULL:
                if not _in(None, self.kinds):
                    return _REJECT
            elif e[1] is None:
                pr.full = True
            else:
                pr.kj = e[1]
                pr.kstr = e[0] == T_STR
        if self.fns is not None:
            wb = []
            for tests in self.fns:
                out = []
                for f, fn, cache in tests:
                    e = idx.get(f)
                    if e is None or e[0] == T_NULL:
                        if fn(None):
                            continue
                        out = None
                        break
                    if e[1] is None:
                        pr.full = True
                    out.append((e[1], e[0] == T_STR, fn, cache))
                if out is None:
                    continue
                if not out:
                    wb = None           # one branch always passes
                    break
                wb.append(out)
            if wb is not None and not wb:
                return _REJECT
            pr.wb = wb
        return pr


# ---------------------------------------------------------------------------
# reading bytes
# ---------------------------------------------------------------------------

def _read_exact(fh, n):
    parts = []
    got = 0
    while got < n:
        x = fh.read(n - got)
        if not x:
            break
        parts.append(x)
        got += len(x)
    return parts[0] if len(parts) == 1 else b"".join(parts)


class _Src(object):
    """A byte source. A path is opened privately and read with os.pread; a
    bytes-like object is used directly; any other object is read only with
    seek/read (never its fileno(): the dashboard's gzip readers expose the
    compressed file's). `stream=True` reads such an object forward only.

    Reads go through one window of read-ahead. It starts small after a jump
    (_WIN0: a seek's probe reads a few headers, not a megabyte) and grows 4x
    with each read that moves on from it, up to _WIN."""

    def __init__(self, obj, stream=False):
        self.fd = None
        self.fh = None
        self.buf = None
        self.own = False
        self.stream = False
        self.w0 = 0
        self.w = b""
        self.win = _WIN0
        if isinstance(obj, (bytes, bytearray, memoryview)):
            self.buf = self.w = bytes(obj)
        elif isinstance(obj, str) or hasattr(obj, "__fspath__"):
            self.fd = os.open(os.fspath(obj), os.O_RDONLY | _O_CLOEXEC)
            self.own = True
        elif hasattr(obj, "read"):
            self.fh = obj
            self.stream = bool(stream)
        else:
            raise TypeError("cannot read EBR from %s" % type(obj).__name__)

    @classmethod
    def from_fd(cls, fd):
        s = cls.__new__(cls)
        s.fd, s.fh, s.buf, s.own, s.stream, s.w0, s.w = fd, None, None, False, False, 0, b""
        s.win = _WIN0
        return s

    def close(self):
        if self.own and self.fd is not None:
            os.close(self.fd)
        self.fd = None
        self.w = b""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def size(self):
        """Current size, or None for a forward-only stream."""
        if self.buf is not None:
            return len(self.buf)
        if self.fd is not None:
            return os.fstat(self.fd).st_size
        if self.stream:
            return None
        if self.fh is None:
            raise ValueError("size of a closed EBR source")
        return self.fh.seek(0, io.SEEK_END)

    def _raw(self, off, n):
        if self.fd is not None:
            out = os.pread(self.fd, n, off)
            if 0 < len(out) < n:
                parts = [out]
                got = len(out)
                while got < n:
                    x = os.pread(self.fd, n - got, off + got)
                    if not x:
                        break
                    parts.append(x)
                    got += len(x)
                out = b"".join(parts)
            return out
        if self.fh is None:
            # a closed path source: never pread a descriptor that may be reused
            raise ValueError("read from a closed EBR source")
        self.fh.seek(off)
        return _read_exact(self.fh, n)

    def read(self, off, n):
        """Up to `n` bytes at `off` (fewer only at the end)."""
        if n <= 0:
            return b""
        b = self.buf
        if b is not None:
            return b[off:off + n]
        w0 = self.w0
        w = self.w
        if w0 <= off and off + n <= w0 + len(w):
            return w[off - w0:off - w0 + n]
        if self.stream:
            return self._sread(off, n)
        if n >= _WIN:
            return self._raw(off, n)
        win = min(self.win << 2, _WIN) if w0 <= off <= w0 + len(w) else _WIN0
        self.win = win
        w = self._raw(off, max(n, win))
        self.w0 = off
        self.w = w
        return w[:n]

    def read_back(self, end, n):
        """The bytes [end - n, end), with the window placed for earlier reads."""
        off = end - n
        if off < 0:
            off, n = 0, end
        b = self.buf
        if b is not None:
            return b[off:end]
        w0 = self.w0
        w = self.w
        if w0 <= off and end <= w0 + len(w):
            return w[off - w0:end - w0]
        if self.stream:
            raise Error("a stream cannot be read backwards")
        if n >= _WIN:
            return self._raw(off, n)
        # moving back from the window -- or from just before it: the header
        # of a frame across the window's start -- is a walk backwards
        win = min(self.win << 2, _WIN) if w0 - _WIN0 <= end <= w0 + len(w) else _WIN0
        self.win = win
        w0 = max(0, end - max(n, win))
        w = self._raw(w0, end - w0)
        self.w0 = w0
        self.w = w
        return w[off - w0:]

    def _sread(self, off, n):
        w0 = self.w0
        w = self.w
        wend = w0 + len(w)
        if off < w0:
            raise Error("a stream cannot be read backwards")
        if off >= wend:
            skip = off - wend
            while skip > 0:
                x = self.fh.read(min(skip, _WIN))
                if not x:
                    # the stream ended short of `off`: the window starts where
                    # it did end, so drain() never counts bytes that were not there
                    self.w0, self.w = off - skip, b""
                    return b""
                skip -= len(x)
            keep = b""
        else:
            keep = w[off - w0:]
        more = _read_exact(self.fh, max(n - len(keep), _WIN))
        w = keep + more if keep else more
        self.w0 = off
        self.w = w
        return w[:n]

    def drain(self, off):
        """A stream's total size, reading what is left after `off`."""
        if not self.stream:
            return self.size()
        end = self.w0 + len(self.w)
        if off > end:
            end = off
        while True:
            x = self.fh.read(_WIN)
            if not x:
                return end
            end += len(x)


def _open_src(obj):
    if isinstance(obj, _Src):
        return obj, False
    return _Src(obj), True


# ---------------------------------------------------------------------------
# frames
# ---------------------------------------------------------------------------

class Frame(object):
    """One frame's header, where it is, and (when read) its payload.

    `kind` is SEGMENT, DATA, BAD (a corrupt region [off, end)) or an advisory
    kind; `payload` is the stored payload when the frame was verified, else
    None; `crc` is its CRC from the trailer."""
    __slots__ = ("off", "end", "kind", "flags", "payload_len", "n_records", "ts_min",
                 "ts_max", "seg_back", "kinds", "crc", "src", "payload")

    @property
    def seg_off(self):
        return self.off - self.seg_back

    def __repr__(self):
        return ("Frame(off=%d, end=%d, kind=%r, flags=%d, n_records=%d, seg_back=%d)"
                % (self.off, self.end, self.kind, self.flags, self.n_records, self.seg_back))


def _hdr_at(s, off):
    """The header tuple at `off` if a valid header is there, else None."""
    if off < 0:
        return None
    return _hdr_of(s.read(off, HDR_SIZE))


def _hdr_of(h):
    """The header tuple of bytes `h` if they are a valid header, else None."""
    if len(h) != HDR_SIZE or h[:4] != MAGIC:
        return None
    t = HDR.unpack(h)
    if _crc(h[:40]) != t[10] or t[4] > MAX_PAYLOAD:
        return None
    return t


_new_frame = object.__new__


def _mk(t, off, s):
    f = _new_frame(Frame)
    f.off = off
    f.kind = t[1]
    f.flags = t[2]
    f.payload_len = t[4]
    f.n_records = t[5]
    f.ts_min = t[6]
    f.ts_max = t[7]
    f.seg_back = t[8]
    f.kinds = t[9]
    f.end = off + OVERHEAD + t[4]
    f.src = s
    f.crc = None
    f.payload = None
    return f


def _bad(off, end, s):
    f = Frame()
    f.off, f.end, f.kind, f.flags, f.payload_len, f.n_records = off, end, BAD, 0, 0, 0
    f.ts_min = f.ts_max = _NAN
    f.seg_back = f.kinds = 0
    f.crc = f.payload = None
    f.src = s
    return f


_HDR_UNPACK = HDR.unpack_from
_TRL_UNPACK = TRL.unpack_from
_KFLN = struct.Struct("<4xBB2xII")          # a header's kind, flags, payload_len, n_records


def _frame_at(s, off, limit, verify):
    """The complete frame at `off` (ending by `limit`), or None."""
    w = s.w
    i = off - s.w0
    if i < 0 or i + HDR_SIZE > len(w):
        s.read(off, HDR_SIZE)               # moves the window
        w = s.w
        i = off - s.w0
        if i < 0 or i + HDR_SIZE > len(w):
            return None
    if not w.startswith(MAGIC, i):
        return None
    t = _HDR_UNPACK(w, i)
    L = t[4]
    if _crc(w[i:i + 40]) != t[10] or L > MAX_PAYLOAD:
        return None
    end = off + OVERHEAD + L
    if limit is not None and end > limit:
        return None
    j = i + HDR_SIZE
    k = j + L
    if k + TRL_SIZE <= len(w):
        pcrc, flen = _TRL_UNPACK(w, k)
        payload = w[j:k] if verify else None
    else:
        payload = None
        if verify:                          # forward order: a stream cannot go back
            payload = s.read(off + HDR_SIZE, L)
            if len(payload) != L:
                return None
        tr = s.read(off + HDR_SIZE + L, TRL_SIZE)
        if len(tr) != TRL_SIZE:
            return None
        pcrc, flen = TRL.unpack(tr)
    if flen != OVERHEAD + L or (verify and _crc(payload) != pcrc):
        return None
    f = _new_frame(Frame)
    f.off = off
    f.end = end
    f.kind = t[1]
    f.flags = t[2]
    f.payload_len = L
    f.n_records = t[5]
    f.ts_min = t[6]
    f.ts_max = t[7]
    f.seg_back = t[8]
    f.kinds = t[9]
    f.crc = pcrc
    f.src = s
    f.payload = payload
    return f


def _frame_ending_at(s, end, verify):
    """The complete frame whose trailer ends at `end`, or None. It is read
    back from `end`, so a walk backwards keeps its window over what it reads
    next -- a frame across the window's start included."""
    if end < OVERHEAD:
        return None
    tr = s.read_back(end, TRL_SIZE)
    if len(tr) != TRL_SIZE:
        return None
    pcrc, flen = TRL.unpack(tr)
    if flen < OVERHEAD or flen > end or flen - OVERHEAD > MAX_PAYLOAD:
        return None
    off = end - flen
    payload = None
    if verify:
        fr = s.read_back(end, flen)
        t = _hdr_of(fr[:HDR_SIZE])
        if t is None or t[4] != flen - OVERHEAD or len(fr) != flen:
            return None
        payload = fr[HDR_SIZE:flen - TRL_SIZE]
        if _crc(payload) != pcrc:
            return None
    else:
        t = _hdr_of(s.read_back(off + HDR_SIZE, HDR_SIZE))
        if t is None or t[4] != flen - OVERHEAD:
            return None
    f = _mk(t, off, s)
    f.crc = pcrc
    f.payload = payload
    return f


def _resync(s, pos, limit):
    """The first offset in [pos, limit] holding a valid header, or None. It
    looks in chunks that grow from _WIN0: a header is seldom far."""
    want = _WIN0
    while limit is None or pos <= limit:
        n = want if limit is None else min(want, limit - pos + 4)
        want = min(want << 2, _WIN)
        chunk = s.read(pos, n)
        if len(chunk) < 4:
            return None
        i = chunk.find(MAGIC)
        while i >= 0:
            p = pos + i
            if limit is not None and p > limit:
                return None
            if _hdr_at(s, p) is not None:
                return p
            i = chunk.find(MAGIC, i + 1)
        if len(chunk) < n:
            return None
        pos += len(chunk) - 3
    return None


def _magics_back(s, end, lo=0):
    """Offsets in [lo, end) holding a valid header, nearest `end` first."""
    hi = end
    while hi > lo:
        a = max(lo, hi - _WIN)
        chunk = s.read_back(min(hi + 3, end), min(hi + 3, end) - a)
        i = chunk.rfind(MAGIC)
        while i >= 0:
            p = a + i
            if p < hi and _hdr_at(s, p) is not None:
                yield p
            i = chunk.rfind(MAGIC, 0, i)
        hi = a


def frames(src, start=0, end=None, verify=True, settle=0):
    """Yield the complete frames in [start, end) (end: the size of the source).

    A corrupt region followed by a valid header is yielded as
    Frame(kind=BAD, off, end=that header), unless it starts within `settle`
    bytes of the end, where it is PENDING like an incomplete last frame:
    nothing more is yielded. `start` must be a frame boundary."""
    s, own = _open_src(src)
    try:
        size = s.size()
        limit = size if end is None else (end if size is None else min(end, size))
        pos = start
        unpack, trailer, crc, new = _HDR_UNPACK, _TRL_UNPACK, _crc, _new_frame
        while limit is None or pos < limit:
            # _frame_at's checks, inline, for a frame whole in the window: a
            # collector's frame holds a poll round, so there are many of them
            w = s.w
            i = pos - s.w0
            if i >= 0 and w.startswith(MAGIC, i) and i + HDR_SIZE <= len(w):
                t = unpack(w, i)
                L = t[4]
                j = i + HDR_SIZE
                k = j + L
                e = pos + OVERHEAD + L
                if (k + TRL_SIZE <= len(w) and L <= MAX_PAYLOAD
                        and (limit is None or e <= limit) and crc(w[i:i + 40]) == t[10]):
                    pcrc, flen = trailer(w, k)
                    payload = w[j:k] if verify else None
                    if flen == OVERHEAD + L and (payload is None or crc(payload) == pcrc):
                        f = new(Frame)
                        f.off = pos
                        f.end = e
                        f.kind = t[1]
                        f.flags = t[2]
                        f.payload_len = L
                        f.n_records = t[5]
                        f.ts_min = t[6]
                        f.ts_max = t[7]
                        f.seg_back = t[8]
                        f.kinds = t[9]
                        f.crc = pcrc
                        f.src = s
                        f.payload = payload
                        yield f
                        pos = e
                        continue
            f = _frame_at(s, pos, limit, verify)
            if f is not None:
                yield f
                pos = f.end
                continue
            nxt = _resync(s, pos + 1, limit)
            if nxt is None or (limit is not None and limit - pos <= settle):
                return
            yield _bad(pos, nxt, s)
            pos = nxt
    finally:
        if own:
            s.close()


def _back_resync(s, E, verify):
    for c in _magics_back(s, E):
        fw = list(frames(s, c, E, verify))
        if fw and fw[-1].end == E:
            return c, fw
    return None


def frames_reverse(src, end, verify=False):
    """Yield the frames that end at or before `end`, newest first.

    `end` should be a boundary (complete_end(), a frame's end); if it is not,
    the walk starts from the end of the last complete frame before it (judged
    with the same `verify`), so from a boundary the result is frames()
    reversed. A broken backward chain is resynced by walking forward from the
    nearest valid header before it, so corrupt regions come out as the BAD
    frames frames() gives."""
    s, own = _open_src(src)
    try:
        E = end
        if E > 0 and _frame_ending_at(s, E, verify) is None:
            E = _complete_end(s, E, verify)
        while E > 0:
            f = _frame_ending_at(s, E, verify)
            if f is not None:
                yield f
                E = f.off
                continue
            got = _back_resync(s, E, verify)
            if got is None:
                yield _bad(0, E, s)
                return
            c, fw = got
            for f in reversed(fw):
                yield f
            E = c
    finally:
        if own:
            s.close()


def peek(src, off):
    """The frame header at `off` (payload not read), even of a frame that is
    still incomplete; its `end` is where it says it ends. None if no valid
    header is there."""
    s, own = _open_src(src)
    try:
        t = _hdr_at(s, off)
        return None if t is None else _mk(t, off, s)
    finally:
        if own:
            s.close()


def resync(src, pos, end=None):
    """The first offset >= pos (and <= end) with MAGIC and a valid header CRC."""
    s, own = _open_src(src)
    try:
        return _resync(s, pos, s.size() if end is None else end)
    finally:
        if own:
            s.close()


def is_boundary(src, off):
    """True if `off` is 0 or the end of a frame whose header is valid."""
    if off == 0:
        return True
    s, own = _open_src(src)
    try:
        return _frame_ending_at(s, off, False) is not None
    finally:
        if own:
            s.close()


def complete_end(src, size=None):
    """The end of the last complete frame in [0, size): everything after it is
    a torn or pending tail. Fast path: a frame ends exactly at `size`; else a
    backward scan of the last 64 MiB + 52 bytes; else a forward walk."""
    s, own = _open_src(src)
    try:
        return _complete_end(s, s.size() if size is None else size, True)
    finally:
        if own:
            s.close()


def verify_back(src, end, stop=0):
    """True when the frames that end at boundary `end` chain back, each one
    complete with both its CRCs good, to the first that starts at or before
    `stop` (or to the start): frames_reverse(src, end, verify=True) then
    yields no BAD region down to there. False at the first frame that is not
    so. One read and one loop: no Frame is built."""
    s, own = _open_src(src)
    try:
        return _verify_back(s, end, stop)
    finally:
        if own:
            s.close()


def _verify_back(s, end, stop):
    E = end
    b0 = max(0, min(end, stop) - _WIN0)
    buf = s.read(b0, end - b0)
    if len(buf) != end - b0:
        return False
    crc = _crc
    u32 = _U32.unpack_from
    while E > 0:
        if E - b0 < OVERHEAD and b0:
            nb = max(0, E - OVERHEAD - _WIN0)
            more = s.read(nb, b0 - nb)
            if len(more) != b0 - nb:
                return False
            buf, b0 = more + buf, nb
        i = E - b0
        if i < OVERHEAD:
            return False
        flen = u32(buf, i - 4)[0]
        L = flen - OVERHEAD
        if L < 0 or flen > E or L > MAX_PAYLOAD:
            return False
        off = E - flen
        if off < b0:
            nb = max(0, off - _WIN0)
            more = s.read(nb, b0 - nb)
            if len(more) != b0 - nb:
                return False
            buf, b0 = more + buf, nb
            i = E - b0
        j = off - b0
        # _frame_ending_at(verify=True)'s checks: the header, its length and
        # CRC, and the payload's CRC (each stored CRC checked with its bytes)
        if (not buf.startswith(MAGIC, j) or u32(buf, j + 8)[0] != L
                or crc(buf[j:j + HDR_SIZE]) != _RESIDUE
                or crc(buf[j + HDR_SIZE:i - 4]) != _RESIDUE):
            return False
        if off <= stop:
            return True
        E = off
    return True


class _Opened(object):
    __slots__ = ("s", "own")

    def __init__(self, src):
        self.s, self.own = _open_src(src)

    def __enter__(self):
        return self.s

    def __exit__(self, *exc):
        if self.own:
            self.s.close()


def source(src):
    """`src` opened once for several calls in a row (`with source(p) as s:`):
    a call given a path or a file object opens its own, and fills its own
    read-ahead. What this opened is closed at the end; a source it was given
    is left open."""
    return _Opened(src)


def _complete_end(s, size, verify):
    if not size:
        return 0
    if _frame_ending_at(s, size, verify) is not None:
        return size
    lo = max(0, size - REPAIR_MAX)
    for c in _magics_back(s, size, lo):
        f = _frame_at(s, c, size, verify)
        if f is not None:
            return f.end
    if lo == 0:
        return 0
    last = 0
    for f in frames(s, 0, size, verify):
        if f.kind != BAD:
            last = f.end
    return last


def _inflate(b):
    d = _zlib.decompressobj()
    try:
        out = d.decompress(b, MAX_PAYLOAD + 1)
    except _zlib.error as e:
        raise FrameError("bad zlib payload: %s" % e)
    if len(out) > MAX_PAYLOAD or d.unconsumed_tail or not d.eof or d.unused_data:
        raise FrameError("zlib payload is not one stream of at most 64 MiB")
    return out


def _frame(kind, flags, payload, n, tmin, tmax, seg_back, kinds):
    h = _HDR40.pack(MAGIC, kind, flags, 0, len(payload), n, tmin, tmax, seg_back, kinds)
    return b"".join((h, _U32.pack(_crc(h)), payload,
                     TRL.pack(_crc(payload), OVERHEAD + len(payload))))


def kind_mask(names, registry=None):
    """(mask, exact): the frame bits that can hold records whose event is one
    of `names` (None for a missing or null event). `exact` is False when a
    name is missing from `registry` (default: this module's KINDS), since a
    newer writer may give it a bit of its own: a frame may then be skipped by
    its mask only when exact is True. Under a SEGMENT's own registry it is."""
    reg = KINDS if registry is None else registry
    index = {}
    for i, k in enumerate(reg):
        index.setdefault(k, i)
    mask = 0
    exact = True
    for k in names:
        if k is None:
            mask |= BIT_NOEVENT
        elif isinstance(k, str) and k in index and index[k] < 30:
            mask |= 1 << index[k]
        else:
            mask |= BIT_OTHER
            if registry is None and isinstance(k, str) and k not in index:
                exact = False
    return mask, exact


# ---------------------------------------------------------------------------
# decoding
# ---------------------------------------------------------------------------

_EMPTY = ()


class Decoder(object):
    """Applies frames in order and yields their records.

    `records()` and `skip()` apply a frame's state changes at once (a SEGMENT
    resets the tables, a DATA frame adds its definitions); `records()` then
    returns a lazy iterator of (position, record), so an iterator that is
    abandoned never desyncs the tables. A frame that does not start where the
    last one ended, a BAD region or a definition that does not validate makes
    the decoder `broken` until the next SEGMENT (strict: an exception). A
    SEGMENT this reader does not support makes it `unsupported` until the
    next one; its frames are skipped and counted."""

    def __init__(self, strict=False):
        self.strict = bool(strict)
        self.segment = None
        self.broken = False
        self.unsupported = False
        self.next_off = 0
        self.stats = dict.fromkeys(DECODER_STATS, 0)
        self._T = None
        self._ok = False            # tables usable: a supported segment, not broken

    @classmethod
    def at(cls, src, off, strict=False):
        """A decoder ready for the frame at boundary `off`: the tables of
        off's segment are replayed from its SEGMENT. StateError if `off` is
        not a boundary."""
        d = cls(strict)
        if off == 0:
            return d
        s, own = _open_src(src)
        try:
            f = _frame_ending_at(s, off, False)
            if f is None:
                raise StateError("offset %d is not a frame boundary" % off)
            so = f.off - f.seg_back
            t = _hdr_at(s, so)
            if t is None or t[1] != SEGMENT:
                if strict:
                    raise StateError("the frame at %d names no segment" % f.off)
                d._set_broken()
                d.next_off = off
                return d
            _walk(d, s, so, off)
            if d.next_off != off:
                if strict:
                    raise StateError("cannot replay the segment at %d up to %d" % (so, off))
                d._set_broken()
            d.next_off = off
            return d
        finally:
            if own:
                s.close()

    def skip(self, frame, src=None):
        """Apply `frame`'s state changes without decoding its records."""
        self._apply(src, frame)

    def records(self, src, frame, kinds=None, where=None):
        """Apply `frame`, then return an iterator of its (position, record).

        `kinds`: a set of event names (None for no/null event); `where`: see
        match_where(). Records a filter rejects still use their positions."""
        r = self._apply(src, frame)
        if r is None:
            return iter(_EMPTY)
        T, b, p = r
        n = frame.n_records
        if kinds is not None:
            mask, rest = T.mask(kinds)
            if not frame.kinds & mask:
                self.stats["filtered"] += n
                return iter(_EMPTY)
            if where is None and not frame.kinds & rest:
                kinds = None            # its header says every record is of `kinds`
        if kinds is None and where is None:
            if n == 1:
                return self._one(T, b, p, frame.off)
            return self._all(T, b, p, frame.off, n)
        return self._some(T, b, p, frame.off, n, kinds, where)

    def _set_broken(self):
        self.broken = True
        self._ok = False

    def _break(self, err):
        if self.strict:
            raise err
        self._set_broken()

    def _payload(self, src, frame):
        b = frame.payload
        if b is None:
            s = src if src is not None else frame.src
            if s is None:
                raise StateError("no source for the payload of the frame at %d" % frame.off)
            s, own = _open_src(s)
            try:
                b = s.read(frame.off + HDR_SIZE, frame.payload_len)
            finally:
                if own:
                    s.close()
            if len(b) != frame.payload_len or (frame.crc is not None and _crc(b) != frame.crc):
                raise FrameError("bad payload in the frame at %d" % frame.off)
        if frame.flags & F_ZLIB:
            b = _inflate(b)
        return b

    def _segment(self, src, frame):
        self.segment = self._T = None
        self._ok = False
        try:
            if frame.flags & ~F_ZLIB:
                raise _Full()
            seg = json.loads(self._payload(src, frame).decode("utf-8"))
            if not isinstance(seg, dict):
                raise FrameError("SEGMENT payload is not an object")
        except _Full:
            self.unsupported, self.broken = True, False
            return
        except (Error, ValueError) as e:
            self.stats["bad_frames"] += 1
            self.unsupported = False
            self._break(FrameError("bad SEGMENT at %d: %s" % (frame.off, e)))
            return
        self.segment = seg
        self.broken = False
        v = seg.get("version")
        sis = seg.get("str_id_size")
        if (seg.get("format") != FORMAT or type(v) is not int or v != VERSION
                or type(sis) is not int or sis not in (2, 4)):
            self.unsupported = True
            return
        self.unsupported = False
        self._T = _Tables(sis, seg.get("kinds"))
        self._ok = True

    def _apply(self, src, frame):
        """Apply the frame. For a DATA frame that can be decoded, returns
        (tables, payload, offset of its first record); else None."""
        st = self.stats
        st["frames"] += 1
        if not (frame.kind == DATA and self._ok and frame.off == self.next_off
                and not frame.flags & ~F_ZLIB):
            return self._apply_other(src, frame)
        self.next_off = frame.end
        b = frame.payload
        if b is None or frame.flags:
            try:
                b = self._payload(src, frame)
            except (Error, IndexError, UnicodeDecodeError, struct.error) as e:
                return self._bad_defs(e, frame.n_records, frame.off)
        return self._data(b, frame.n_records, frame.off)

    def _data(self, b, n, off):
        """_apply's own part for a DATA frame in step (`frames` counted,
        `next_off` moved past it): its definitions from payload `b`, inflated.
        `n` is its record count, `off` its offset."""
        st = self.stats
        T = self._T
        try:
            c = b[0]
            if c < 0x80:
                sb, p = c, 1
            elif b[1] < 0x80:
                sb, p = (c & 0x7f) | (b[1] << 7), 2
            else:
                sb, p = _ruv(b, 0)
            c = b[p]
            if c < 0x80:
                hb = c
                p += 1
            else:
                hb, p = _ruv(b, p)
            c = b[p]
            if c < 0x80:
                dl = c
                p += 1
            else:
                dl, p = _ruv(b, p)
            de = p + dl
            if de > len(b):
                raise FrameError("definitions past the payload")
            S = T.S
            same = sb == len(S) and hb == len(T.shapes)
            if same and dl:
                if b[p] == 1 and b[p + 1] < 0x80 and p + 2 + b[p + 1] == de:
                    # one short string, as a poll round's frame defines: its time
                    S.append(b[p + 2:de].decode("utf-8", "surrogatepass"))
                else:
                    _apply_defs(T, b, p, de)
        except (Error, IndexError, UnicodeDecodeError, struct.error) as e:
            return self._bad_defs(e, n, off)
        if not same:
            st["lost_frames"] += 1
            st["lost_records"] += n
            self._break(StateError("frame at %d starts at tables (%d, %d), decoder has (%d, %d)"
                                   % (off, sb, hb, len(T.S), len(T.shapes))))
            return None
        return T, b, de

    def _bad_defs(self, e, n, off):
        st = self.stats
        st["bad_frames"] += 1
        st["lost_records"] += n
        self._T = None
        self._break(e if isinstance(e, Error) else
                    FrameError("bad definitions in the frame at %d: %s" % (off, e)))
        return None

    def _apply_other(self, src, frame):
        """Everything but a decodable DATA frame."""
        st = self.stats
        k = frame.kind
        if k == SEGMENT:
            self.next_off = frame.end
            self._segment(src, frame)
            return None
        if k == BAD:
            st["bad_frames"] += 1
            self.next_off = frame.end
            self._T = None
            self._break(FrameError("corrupt region [%d, %d)" % (frame.off, frame.end)))
            return None
        if frame.off != self.next_off:
            if self.strict:
                raise StateError("frame at %d, expected one at %d" % (frame.off, self.next_off))
            self._set_broken()
        self.next_off = frame.end
        if k != DATA:
            return None
        if self.unsupported:
            st["unsupported_frames"] += 1
            return None
        if self.broken or self._T is None:
            self._set_broken()
            st["lost_frames"] += 1
            st["lost_records"] += frame.n_records
            return None
        # a supported segment, but a flag this reader does not know
        self.unsupported = True
        self._ok = False
        st["unsupported_frames"] += 1
        return None

    def _one(self, T, b, p, off):
        """A one-record frame, decoded now (its tables are applied either way)."""
        try:
            c = b[p]
            if c < 0x80:
                p += 1
            else:
                c, p = _ruv(b, p)
            end = p + c
            if end > len(b):
                raise FrameError("record past the payload")
            rec, q = T.F[b[p]](b, p + 1)
            if q != end:
                raise FrameError("record length mismatch")
        except Exception as e:
            self._bad_record(off, e)
            return iter(_EMPTY)
        self.stats["records"] += 1
        return iter(((off, rec),))

    def _bad_record(self, pos, e):
        if self.strict:
            raise FrameError("bad record at %d: %s" % (pos, e))
        self.stats["bad_records"] += 1

    def _list(self, T, b, p, off, n):
        """list(self._all(...)), decoded in one loop."""
        st = self.stats
        F = T.F
        ruv = _ruv
        size = len(b)
        out = []
        ap = out.append
        try:
            for i in range(n):
                try:
                    c = b[p]
                    if c < 0x80:
                        p += 1
                    else:
                        c, p = ruv(b, p)
                    end = p + c
                    if end > size:
                        raise FrameError("record past the payload")
                except (IndexError, FrameError) as e:
                    self._bad_record(off + i, e)
                    st["bad_records"] += n - i - 1
                    break
                try:
                    rec, q = F[b[p]](b, p + 1)
                    if q != end:
                        raise FrameError("record length mismatch")
                except Exception as e:
                    self._bad_record(off + i, e)
                    p = end
                    continue
                p = end
                ap((off + i, rec))
        finally:
            st["records"] += len(out)
        return out

    def _all(self, T, b, p, off, n):
        st = self.stats
        F = T.F
        ruv = _ruv
        size = len(b)
        for i in range(n):
            try:
                c = b[p]
                if c < 0x80:
                    p += 1
                else:
                    c, p = ruv(b, p)
                end = p + c
                if end > size:
                    raise FrameError("record past the payload")
            except (IndexError, FrameError) as e:
                self._bad_record(off + i, e)
                st["bad_records"] += n - i - 1
                return
            try:
                rec, q = F[b[p]](b, p + 1)
                if q != end:
                    raise FrameError("record length mismatch")
            except Exception as e:
                self._bad_record(off + i, e)
                p = end
                continue
            p = end
            st["records"] += 1
            yield off + i, rec

    def _some(self, T, b, p, off, n, kinds, where):
        st = self.stats
        plan = T.plan(kinds, where)
        probe = plan.probe
        kcache = plan.kcache
        S = T.S
        shapes = T.shapes
        F = T.F
        ruv = _ruv
        size = len(b)
        for i in range(n):
            try:
                c = b[p]
                if c < 0x80:
                    p += 1
                else:
                    c, p = ruv(b, p)
                end = p + c
                if end > size:
                    raise FrameError("record past the payload")
            except (IndexError, FrameError) as e:
                self._bad_record(off + i, e)
                st["bad_records"] += n - i - 1
                return
            try:
                c = b[p]
                if 0 < c < 0x80:
                    sh = shapes[c - 1]
                    f = sh.fn
                    if f is None:
                        f = _compile_shape(T, sh, b, p + 1)
                    pr = probe(sh)
                    if pr.reject:
                        st["filtered"] += 1
                        p = end
                        continue
                    q = p + 1
                    a = sh.unpack(b, q)
                    ok = True
                    if not pr.full:
                        j = pr.kj
                        if j is not None:
                            x = a[j]
                            if pr.kstr:
                                r = kcache.get(x)
                                if r is None:
                                    r = kcache[x] = _in(S[x], kinds)
                            else:
                                r = _in(x, kinds)
                            ok = r
                        wb = pr.wb
                        if ok and wb is not None:
                            ok = False
                            for tests in wb:
                                for j, isstr, fn, cache in tests:
                                    x = a[j]
                                    if isstr:
                                        r = cache.get(x)
                                        if r is None:
                                            r = cache[x] = fn(S[x])
                                    else:
                                        r = fn(x)
                                    if not r:
                                        break
                                else:
                                    ok = True
                                    break
                        if not ok:
                            st["filtered"] += 1
                            p = end
                            continue
                    rec, q = f(b, q, a)
                    if pr.full:
                        ok = _post(rec, kinds, where)
                else:
                    rec, q = F[c](b, p + 1)
                    ok = _post(rec, kinds, where)
                if q != end:
                    raise FrameError("record length mismatch")
            except Exception as e:
                self._bad_record(off + i, e)
                p = end
                continue
            p = end
            if not ok:
                st["filtered"] += 1
                continue
            st["records"] += 1
            yield off + i, rec


def _walk(d, s, pos, end, keep=None):
    """`for f in frames(s, pos, end, True): d.skip(f, s)`: what places a
    decoder (Decoder.at) and what applies a segment before it is read
    backwards (records_reverse). A frame in step, whole in the window, as
    nearly all are, has its definitions applied from there: no Frame, no
    generator, no call but the decoder's. `keep`, seven lists, receives the
    tables, payload, first record's offset, offset, end, record count and
    kinds of each DATA frame left decodable."""
    size = s.size()
    if size is not None and size < end:
        end = size
    st = d.stats
    crc = _crc
    kfln = _KFLN.unpack_from
    trl = _TRL_UNPACK
    u32 = _U32.unpack_from
    while pos < end:
        w = s.w
        i = pos - s.w0
        if (d._ok and pos == d.next_off and i >= 0 and w.startswith(MAGIC, i)
                and i + HDR_SIZE <= len(w)):
            kind, flags, L, n = kfln(w, i)
            j = i + HDR_SIZE
            k = j + L
            e = pos + OVERHEAD + L
            # _frame_at's checks, then _apply's for a DATA frame in step
            if (kind == DATA and not flags and k + TRL_SIZE <= len(w) and e <= end
                    and L <= MAX_PAYLOAD and crc(w[i:j]) == _RESIDUE):
                pcrc, flen = trl(w, k)
                b = w[j:k]
                if flen == OVERHEAD + L and crc(b) == pcrc:
                    st["frames"] += 1
                    d.next_off = e
                    r = d._data(b, n, pos)
                    if r is not None and keep is not None:
                        Ts, bufs, ps, offs, ends, ns, ks = keep
                        Ts.append(r[0])
                        bufs.append(b)
                        ps.append(r[2])
                        offs.append(pos)
                        ends.append(e)
                        ns.append(n)
                        ks.append(u32(w, i + 36)[0])
                    pos = e
                    continue
        # anything else: one frame, or one corrupt region, as frames() gives it
        f = _frame_at(s, pos, end, True)
        if f is None:
            nxt = _resync(s, pos + 1, end)
            if nxt is None:
                return
            f = _bad(pos, nxt, s)
        r = d._apply(s, f)
        if r is not None and keep is not None:
            Ts, bufs, ps, offs, ends, ns, ks = keep
            Ts.append(r[0])
            bufs.append(r[1])
            ps.append(r[2])
            offs.append(f.off)
            ends.append(f.end)
            ns.append(f.n_records)
            ks.append(f.kinds)
        pos = f.end


def records_reverse(src, end, start=0, kinds=None, where=None, stats=None):
    """Yield (position, record) newest first, for the frames in [start, end).

    Per segment, from the end: its frames are walked forward from the SEGMENT
    to the range's end, applying definitions only, and then decoded backwards
    against those (superset) tables. `stats`, a dict, receives the Decoder
    counters."""
    s, own = _open_src(src)
    try:
        st = stats if stats is not None else {}
        for k in DECODER_STATS:
            st.setdefault(k, 0)
        E = end
        while E > start:
            so = None
            for f in frames_reverse(s, E):
                if f.end <= start:
                    return
                if f.kind == BAD:
                    st["bad_frames"] += 1
                    continue
                if f.kind != DATA:
                    continue
                so = f.off - f.seg_back
                t = _hdr_at(s, so)
                if t is None or t[1] != SEGMENT:
                    so = None
                    st["lost_frames"] += 1
                    st["lost_records"] += f.n_records
                    continue
                fend = f.end
                break
            if so is None:
                return
            # The segment, applied forward to fend: what decoding each of its
            # frames takes is kept flat -- not its Frame: it holds thousands
            dec = Decoder()
            dec.stats = st
            keep = Ts, bufs, ps, offs, ends, ns, ks = [], [], [], [], [], [], []
            _walk(dec, s, so, fend, keep)
            for i in range(len(offs) - 1, -1, -1):
                if ends[i] <= start:
                    return
                T = Ts[i]
                n = ns[i]
                kk = kinds
                if kinds is not None:
                    mask, rest = T.mask(kinds)
                    if not ks[i] & mask:
                        st["filtered"] += n
                        continue
                    if where is None and not ks[i] & rest:
                        kk = None       # every record is of `kinds`
                if kk is None and where is None:
                    got = dec._list(T, bufs[i], ps[i], offs[i], n)
                else:
                    got = list(dec._some(T, bufs[i], ps[i], offs[i], n, kk, where))
                got.reverse()
                for pr in got:
                    yield pr
            E = so
    finally:
        if own:
            s.close()


# ---------------------------------------------------------------------------
# writing
# ---------------------------------------------------------------------------

def _count_defs(b):
    """(STR defs, SHAPE defs) in a run of definitions."""
    ns = nh = p = 0
    while p < len(b):
        if b[p] == 1:
            n, p = _ruv(b, p + 1)
            p += n
            ns += 1
        else:
            nf, p = _ruv(b, p + 1)
            for _ in range(nf):
                _k, p = _ruv(b, p)
                p += 1
            nh += 1
    return ns, nh


def _zlevel(z):
    if z is True:
        return 1
    if z is False or z is None:
        return 0
    if type(z) is int and 0 <= z <= 9:
        return z
    raise ValueError("zlib is a bool or a level 0-9, not %r" % (z,))


class Writer(object):
    """Appends records to an EBR file (a path) or to any object with .write.

    write(rec) encodes at once (TypeError/ValueError for a record json.dumps
    would refuse, and nothing of it is written); flush() writes every frame
    pending in ONE os.write and returns the bytes written. A path is opened
    O_APPEND and flock'ed (lock_wait seconds, then Busy); a torn tail is
    truncated (stats['repaired_bytes']), a non-EBR file refused. With
    `resume`, the file's last segment is continued when it is clean, under
    reset_bytes and of this version; otherwise every open starts a segment."""
    __slots__ = ("_st", "meta", "reset_bytes", "zlib", "max_frame_records", "max_frame_bytes",
                 "path", "closed", "end", "_fd", "_fh", "_owned", "_locked", "_enc", "_record",
                 "_defs", "_pending", "_pending_bytes", "_seg_off", "_seg_bytes", "_seg_hdr",
                 "_new_seg", "_str_base", "_shape_base", "_recs", "_n", "_mask", "_tmin",
                 "_tmax", "_unpl")

    def __init__(self, target, meta=None, reset_bytes=DEFAULT_RESET_BYTES, zlib=0,
                 max_frame_records=8192, max_frame_bytes=1 << 20, mode=0o666,
                 lock=True, resume=False, lock_wait=0):
        self._st = dict.fromkeys(WRITER_STATS, 0)
        self.meta = dict(meta or {})
        json.dumps(self.meta, default=str)          # fail now, not at the first frame
        self.reset_bytes = int(reset_bytes)
        if not 0 < self.reset_bytes <= 1 << 31:
            raise ValueError("reset_bytes must be in (0, 2 GiB]")
        self.zlib = _zlevel(zlib)
        self.max_frame_records = max(1, min(int(max_frame_records), 1 << 24))
        self.max_frame_bytes = max(1, min(int(max_frame_bytes), MAX_PAYLOAD))
        self.path = None
        self.closed = False
        self.end = 0
        self._fd = None
        self._fh = None
        self._owned = False
        self._locked = False
        self._enc = _Encoder(STR_ID_SIZE)
        self._record = self._enc.record
        self._defs = self._enc.defs
        self._pending = []                  # (bytes, kind, n_records, flags)
        self._pending_bytes = 0
        self._seg_off = 0
        self._seg_bytes = 0
        self._seg_hdr = True                # the segment's SEGMENT frame is not queued yet
        self._new_seg = True                # the next record starts a segment
        self._str_base = self._shape_base = 0
        self._recs = bytearray()
        self._n = 0
        self._mask = 0
        self._tmin = _INF
        self._tmax = -_INF
        self._unpl = False
        if isinstance(target, (str, bytes)) or hasattr(target, "__fspath__"):
            self.path = os.fspath(target)
            self._open(mode, lock, lock_wait, resume)
        elif hasattr(target, "write"):
            self._fh = target
            try:
                self.end = target.tell()
            except (AttributeError, OSError, ValueError):
                self.end = 0
        else:
            raise TypeError("a Writer target is a path or has .write, not %s"
                            % type(target).__name__)

    @property
    def stats(self):
        """records frames segments bytes writes encode_errors write_errors
        lost_records repaired_bytes zlib_frames (a copy)."""
        d = dict(self._st)
        d["records"] += self._n
        return d

    @classmethod
    def open_append(cls, path, **kw):
        """A writer that continues `path`'s last segment when it can, waiting
        up to 30 s (lock_wait) for another writer to finish."""
        kw.setdefault("lock_wait", 30)
        kw["resume"] = True
        return cls(path, **kw)

    # -- opening ----------------------------------------------------------
    def _open(self, mode, lock, lock_wait, resume):
        fd = os.open(self.path, os.O_RDWR | os.O_APPEND | os.O_CREAT | _O_CLOEXEC, mode)
        try:
            if lock:
                self._lock(fd, lock_wait)
            size = os.fstat(fd).st_size
            src = _Src.from_fd(fd)
            if size:
                head = src.read(0, 4)
                if head != MAGIC[:len(head)]:
                    raise Error("%s is not an EBR file" % self.path)
                ce = complete_end(src, size)
                torn = size - ce
                if torn and torn <= REPAIR_MAX:
                    os.ftruncate(fd, ce)
                    self._st["repaired_bytes"] = torn
                    size = ce
            self.end = size
            if resume and size and not self._st["repaired_bytes"]:
                # (a writer that crashed mid-frame gets a fresh segment)
                self._resume(src, size)
        except BaseException:
            if self._locked:
                self._unlock(fd)
            os.close(fd)
            raise
        self._fd = fd
        self._owned = True

    def _lock(self, fd, wait):
        if fcntl is None:
            return
        deadline = time.time() + max(0.0, float(wait))
        unsupported = (errno.ENOLCK, errno.EINVAL, getattr(errno, "EOPNOTSUPP", -1),
                       getattr(errno, "ENOTSUP", -1))
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._locked = True
                return
            except OSError as e:
                if e.errno in unsupported:
                    return                  # no locks here: proceed unlocked
                if e.errno not in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
                    raise
            left = deadline - time.time()
            if left <= 0:
                raise Busy("%s is locked by another writer" % self.path)
            time.sleep(min(0.05, left))

    def _unlock(self, fd):
        self._locked = False
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass

    def _resume(self, src, size):
        f = _frame_ending_at(src, size, True)
        if f is None:
            return
        so = f.off - f.seg_back
        if so < 0 or size - so >= self.reset_bytes:
            return
        fl = list(frames(src, so, size, True))
        if (not fl or fl[0].off != so or fl[0].kind != SEGMENT or fl[-1].end != size
                or any(g.kind != DATA for g in fl[1:])):
            return
        dec = Decoder()
        for g in fl:
            dec.skip(g, src)
            if dec.broken or dec.unsupported:
                return
        seg = dec.segment
        if (dec._T is None or seg.get("str_id_size") != STR_ID_SIZE
                or seg.get("kinds") != list(KINDS)):
            return
        T = dec._T
        S = T.S
        self._enc.load(S, [(tuple([S[i] for i in sh.ids]), sh.layout) for sh in T.shapes])
        self._seg_off = so
        self._seg_bytes = size - so
        self._seg_hdr = False
        self._new_seg = False
        self._str_base = self._enc.I.n
        self._shape_base = self._enc.nshapes

    # -- records ----------------------------------------------------------
    def write(self, rec):
        """Encode `rec` (a dict) into the open frame. TypeError/ValueError
        for a record json.dumps would refuse; nothing of it is written."""
        if not isinstance(rec, dict):
            raise TypeError("a record is a dict, not %s" % type(rec).__name__)
        if self._new_seg:
            if self.closed:
                raise ValueError("write to a closed Writer")
            self._reset_segment()
        defs = self._defs
        d0 = len(defs)
        try:
            body = self._record(rec)
        except _Full:
            body = self._refill(rec)
            d0 = 0
        except Exception:
            self._st["encode_errors"] += 1
            raise
        recs = self._recs
        n = len(body)
        if len(recs) + len(defs) + n > _FRAME_ROOM:
            self._oversize(n, d0)
            recs = self._recs
        recs += _UV[n] if n < 16384 else _uv_slow(n)
        recs += body
        ev = rec.get("event")
        if type(ev) is str:
            self._mask |= _KIND_BIT.get(ev, BIT_OTHER)
        elif ev is None:
            self._mask |= BIT_NOEVENT
        else:
            self._mask |= _KIND_BIT.get(ev, BIT_OTHER) if isinstance(ev, str) else BIT_OTHER
        t = rec.get("ts_epoch")
        if type(t) is float and t - t == 0.0:
            if t < self._tmin:
                self._tmin = t
            if t > self._tmax:
                self._tmax = t
        else:
            self._ts_slow(t)
        n = self._n + 1
        self._n = n
        if n >= self.max_frame_records or len(recs) + len(defs) >= self.max_frame_bytes:
            self._close_frame()

    def _ts_slow(self, t):
        if isinstance(t, (int, float)) and not isinstance(t, bool):
            try:
                t = float(t)
            except OverflowError:
                t = _NAN
            if t - t == 0.0:
                if t < self._tmin:
                    self._tmin = t
                if t > self._tmax:
                    self._tmax = t
                return
        self._unpl = True

    def _refill(self, rec):
        """A table filled up: start a segment and encode again."""
        self._reset_segment()
        try:
            return self._record(rec)
        except _Full:
            self._new_seg = True
            self._st["encode_errors"] += 1
            raise RecordTooLarge("record needs more than %d strings or shapes" % MAX_STRINGS)
        except Exception:
            self._st["encode_errors"] += 1
            raise

    def _oversize(self, n, d0):
        """The record does not fit the open frame: close the frame with the
        definitions made before the record, then refuse the record if it
        cannot fit an empty frame either."""
        enc = self._enc
        defs = self._defs
        mine = len(defs) - d0 + n + 32
        if self._n:
            tail = bytes(defs[d0:])
            ns, nh = _count_defs(tail)
            del defs[d0:]
            self._close_frame((enc.I.n - ns, enc.nshapes - nh))
            defs.extend(tail)
        if mine > _FRAME_ROOM:
            del defs[:]
            self._new_seg = True            # its definitions will never be written
            self._st["encode_errors"] += 1
            raise RecordTooLarge("a record of %d bytes does not fit a frame" % n)

    # -- frames -----------------------------------------------------------
    def _reset_segment(self):
        if self._n:
            self._close_frame()
        self._enc.reset()
        self._str_base = self._shape_base = 0
        self._seg_hdr = True
        self._new_seg = False

    def _queue_segment(self):
        seg = {"format": FORMAT, "version": VERSION, "str_id_size": self._enc.sis,
               "kinds": list(KINDS), "reset_bytes": self.reset_bytes, "zlib": self.zlib,
               "created": time.time()}
        for k, v in self.meta.items():
            if k not in seg:
                seg[k] = v
        payload = json.dumps(seg, separators=(",", ":"), default=str).encode("ascii")
        fb = _frame(SEGMENT, 0, payload, 0, _NAN, _NAN, 0, 0)
        self._seg_off = self.end + self._pending_bytes
        self._pending.append((fb, SEGMENT, 0, 0))
        self._pending_bytes += len(fb)
        self._seg_bytes = len(fb)
        self._seg_hdr = False

    def _close_frame(self, bases=None):
        n = self._n
        if not n:
            return
        if self._seg_hdr:
            self._queue_segment()
        defs = self._defs
        sb = self._str_base
        hb = self._shape_base
        dl = len(defs)
        if sb < 16384 and hb < 16384 and dl < 16384:
            head = _UV[sb] + _UV[hb] + _UV[dl]
        else:
            head = _uv(sb) + _uv(hb) + _uv(dl)
        if dl:
            payload = b"".join((head, defs, self._recs))
            del defs[:]
        else:
            payload = head + self._recs
        flags = 0
        if self.zlib:
            z = _zlib.compress(payload, self.zlib)
            if len(z) < len(payload):
                payload = z
                flags = F_ZLIB
        L = len(payload)
        off = self.end + self._pending_bytes
        if self._unpl:
            tmin, tmax = -_INF, _INF
        else:
            tmin, tmax = self._tmin, self._tmax
        h = _HDR40.pack(MAGIC, DATA, flags, 0, L, n, tmin, tmax, off - self._seg_off, self._mask)
        self._pending.append((b"".join((h, _U32.pack(_crc(h)), payload,
                                        TRL.pack(_crc(payload), OVERHEAD + L))), DATA, n, flags))
        L += OVERHEAD
        self._pending_bytes += L
        self._seg_bytes += L
        self._st["records"] += n
        self._recs = bytearray()
        self._n = 0
        self._mask = 0
        self._tmin = _INF
        self._tmax = -_INF
        self._unpl = False
        if bases is None:
            self._str_base = self._enc.I.n
            self._shape_base = self._enc.nshapes
        else:
            self._str_base, self._shape_base = bases
        if self._seg_bytes >= self.reset_bytes:
            self._new_seg = True

    def flush(self):
        """Write every pending frame in one os.write; returns the bytes
        written (0, and no syscall, when there is nothing to write)."""
        if self._n:
            self._close_frame()
        pend = self._pending
        if not pend:
            return 0
        data = pend[0][0] if len(pend) == 1 else b"".join([x[0] for x in pend])
        st = self._st
        done = 0
        try:
            if self._fd is not None:
                total = len(data)
                mv = memoryview(data)
                while done < total:
                    k = os.write(self._fd, mv[done:] if done else data)
                    st["writes"] += 1
                    if k <= 0:
                        raise OSError(errno.EIO, "write(2) wrote nothing")
                    done += k
            elif self._fh is not None:
                self._fh.write(data)
                st["writes"] += 1
                done = len(data)
            else:
                raise ValueError("flush of a closed Writer")
        except BaseException:
            st["write_errors"] += 1
            self._failed(done)
            raise
        st["bytes"] += done
        for _fb, kind, _n, flags in pend:
            st["frames"] += 1
            if kind == SEGMENT:
                st["segments"] += 1
            if flags & F_ZLIB:
                st["zlib_frames"] += 1
        self.end += done
        self._pending = []
        self._pending_bytes = 0
        return done

    def _failed(self, done):
        fd = self._fd
        if fd is not None:
            if done:
                try:
                    os.ftruncate(fd, self.end)
                    done = 0
                except OSError:
                    pass
            if not done:
                # the file is as it was: keep the frames for the next flush
                if self._pending_bytes > _PENDING_MAX:
                    self._drop()
                return
            self._drop()
            try:
                self.end = os.fstat(fd).st_size
            except OSError:
                self.end += done
            return
        self._drop()
        try:
            self.end = self._fh.tell()
        except Exception:
            self.end += done

    def _drop(self):
        self._st["lost_records"] += sum(x[2] for x in self._pending)
        self._pending = []
        self._pending_bytes = 0
        self._new_seg = True        # the dropped frames held definitions

    def close(self):
        if self.closed:
            return
        try:
            self.flush()
        finally:
            self.closed = True
            self._new_seg = True
            if self._pending:
                self._st["lost_records"] += sum(x[2] for x in self._pending)
                self._pending = []
                self._pending_bytes = 0
            fd = self._fd
            self._fd = None
            if fd is not None:
                if self._locked:
                    self._unlock(fd)
                if self._owned:
                    os.close(fd)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


# ---------------------------------------------------------------------------
# whole files, either format
# ---------------------------------------------------------------------------

def sniff(b):
    """'ebr' if `b` (a file's first bytes) starts with MAGIC, 'jsonl' if its
    first non-blank byte is '{', else None."""
    b = bytes(b[:4096])
    if b[:4] == MAGIC:
        return "ebr"
    s = b.lstrip(b" \t\r\n")
    if s[:1] == b"{":
        return "jsonl"
    return None


class _Prefixed(object):
    """A forward-only reader that first returns `head`, then `fh`."""

    def __init__(self, head, fh):
        self.head = head
        self.fh = fh

    def read(self, n=-1):
        h = self.head
        if not h:
            return self.fh.read(n)
        if n is None or n < 0:
            self.head = b""
            return h + self.fh.read()
        if n <= len(h):
            self.head = h[n:]
            return h[:n]
        self.head = b""
        return h + self.fh.read(n - len(h))

    def close(self):
        self.fh.close()


def _head(fh, n=4096):
    """(reader, its first bytes) without losing them: peek, or read and seek
    back, or read and chain."""
    p = getattr(fh, "peek", None)
    if p is not None:
        try:
            return fh, p(n)[:n]
        except (OSError, ValueError):
            pass
    try:
        if fh.seekable():
            pos = fh.tell()
            h = _read_exact(fh, n)
            fh.seek(pos)
            return fh, h
    except (AttributeError, OSError, ValueError):
        pass
    h = _read_exact(fh, n)
    return _Prefixed(h, fh), h


def open_feed(path):
    """(binary file object, 'ebr'|'jsonl') for a feed file, plain or gzipped
    (by content). '-' is stdin. The format is sniffed from the content, else
    taken from the name."""
    if path == "-":
        raw = sys.stdin.buffer
        own = False
    else:
        raw = open(path, "rb")
        own = True
    try:
        fh, head = _head(raw)
        if head[:2] == b"\x1f\x8b":
            if own:
                raw.close()
                fh = gzip.open(path, "rb")
            else:
                fh = gzip.GzipFile(fileobj=fh, mode="rb")
            fh, head = _head(fh)
    except BaseException:
        if own:
            raw.close()
        raise
    fmt = sniff(head)
    if fmt is None:
        name = str(path)
        if name.endswith(".gz"):
            name = name[:-3]
        fmt = "ebr" if name.endswith(".ebr") else "jsonl"
    return fh, fmt


def _lines(fh):
    """(offset, line bytes, ends with newline) of a byte stream."""
    pos = 0
    rest = b""
    while True:
        chunk = fh.read(1 << 20)
        if not chunk:
            if rest:
                yield pos, rest, False
            return
        data = rest + chunk if rest else chunk
        i = 0
        while True:
            j = data.find(b"\n", i)
            if j < 0:
                break
            yield pos + i, data[i:j + 1], True
            i = j + 1
        pos += i
        rest = data[i:]


def _is_path(x):
    return isinstance(x, str) or hasattr(x, "__fspath__")


def iter_records(path_or_fh, kinds=None, where=None, stats=None):
    """(position, record) for every record of a feed: EBR or JSONL, plain or
    gzipped. A JSONL position is the line's byte offset. `stats` (a dict)
    receives the Decoder's counters, or for JSONL lines/bad_lines/pending."""
    if _is_path(path_or_fh) and path_or_fh != "-":
        fh, fmt = open_feed(os.fspath(path_or_fh))
        own = True
    else:
        if path_or_fh == "-":
            path_or_fh = sys.stdin.buffer
        fh, head = _head(path_or_fh)
        own = False
        if head[:2] == b"\x1f\x8b":
            fh, head = _head(gzip.GzipFile(fileobj=fh, mode="rb"))
        fmt = sniff(head) or "jsonl"
    try:
        if fmt == "ebr":
            if own and isinstance(fh, io.BufferedReader):
                s = _Src(os.fspath(path_or_fh))
            else:
                s = _Src(fh, stream=True)
            dec = Decoder()
            try:
                for f in frames(s):
                    for pr in dec.records(s, f, kinds, where):
                        yield pr
            finally:
                s.close()
                if stats is not None:
                    for k, v in dec.stats.items():
                        stats[k] = stats.get(k, 0) + v
            return
        n = bad = pending = 0
        try:
            for pos, line, nl in _lines(fh):
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    if nl:
                        bad += 1
                    else:
                        pending += 1
                    continue
                if not isinstance(rec, dict):
                    bad += 1
                    continue
                n += 1
                if (kinds is None and where is None) or _post(rec, kinds, where):
                    yield pos, rec
        finally:
            if stats is not None:
                stats["lines"] = stats.get("lines", 0) + n
                stats["bad_lines"] = stats.get("bad_lines", 0) + bad
                stats["pending"] = stats.get("pending", 0) + pending
    finally:
        if own:
            fh.close()


# ---------------------------------------------------------------------------
# command line
# ---------------------------------------------------------------------------

def _err(msg):
    sys.stderr.write("ebr: %s\n" % msg)


def _same(a, b):
    """Type-exact equality: key order counts, NaN equals NaN, -0.0 is not 0.0."""
    ta = type(a)
    if ta is not type(b):
        return False
    if ta is dict:
        if len(a) != len(b) or list(a) != list(b):
            return False
        for k in a:
            if not _same(a[k], b[k]):
                return False
        return True
    if ta is list:
        if len(a) != len(b):
            return False
        for x, y in zip(a, b):
            if not _same(x, y):
                return False
        return True
    if ta is float:
        if a != a:
            return b != b
        return a == b and math.copysign(1.0, a) == math.copysign(1.0, b)
    return a == b


def _diff(a, b, path="$"):
    """Where two values first differ, as a short description."""
    if type(a) is not type(b):
        return "%s: %s %r vs %s %r" % (path, type(a).__name__, _short(a),
                                       type(b).__name__, _short(b))
    if type(a) is dict:
        ka, kb = list(a), list(b)
        if ka != kb:
            if set(ka) != set(kb):
                return "%s: keys only in A %r, only in B %r" % (
                    path, sorted(set(ka) - set(kb))[:5], sorted(set(kb) - set(ka))[:5])
            return "%s: key order differs" % path
        for k in ka:
            if not _same(a[k], b[k]):
                return _diff(a[k], b[k], "%s.%s" % (path, k))
    if type(a) is list:
        if len(a) != len(b):
            return "%s: %d vs %d items" % (path, len(a), len(b))
        for i, (x, y) in enumerate(zip(a, b)):
            if not _same(x, y):
                return _diff(x, y, "%s[%d]" % (path, i))
    return "%s: %r vs %r" % (path, _short(a), _short(b))


def _short(v):
    r = repr(v)
    return r if len(r) <= 80 else r[:77] + "..."


def _time_arg(s):
    try:
        return float(s)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M",
                "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return time.mktime(time.strptime(s, fmt))
        except ValueError:
            continue
    import argparse
    raise argparse.ArgumentTypeError("not an epoch or a local 'YYYY-MM-DD[ HH:MM[:SS]]': %r" % s)


def _kinds_arg(vals):
    if not vals:
        return None
    out = set()
    for v in vals:
        for k in v.split(","):
            k = k.strip()
            out.add(None if k in ("null", "None", "") else k)
    return out


def _where_arg(vals, lo=None, hi=None):
    where = {}
    for v in vals or ():
        if "^=" in v and ("=" not in v.split("^=", 1)[0]):
            f, p = v.split("^=", 1)
            where[f] = Prefix(p)
            continue
        if "=" not in v:
            raise Error("--where takes FIELD=VALUE or FIELD^=PREFIX, not %r" % v)
        f, x = v.split("=", 1)
        t = where.get(f)
        if not isinstance(t, set):
            t = where[f] = set()
        t.add(x)
    if lo is not None or hi is not None:
        prev = where.get("ts_epoch")

        def in_window(x, prev=prev):
            if isinstance(x, bool) or not isinstance(x, (int, float)):
                return False
            if (lo is not None and not x >= lo) or (hi is not None and not x < hi):
                return False
            return prev is None or _test(prev, x)
        where["ts_epoch"] = in_window
    return where or None


def _ebr_iter(s, kinds, where, lo, hi, st):
    for k in DECODER_STATS:
        st.setdefault(k, 0)
    dec = Decoder()
    dec.stats = st
    for f in frames(s):
        if f.kind == DATA and ((lo is not None and f.ts_max < lo)
                               or (hi is not None and f.ts_min >= hi)):
            dec.skip(f, s)
            continue
        for pr in dec.records(s, f, kinds, where):
            yield pr


def _feed_iter(path, kinds, where, lo, hi, st, opened=None):
    """(pos, rec) of one file of either format, with frame skipping by time.
    `opened` is what open_feed(path) already returned, when the caller had to
    look at the format first: stdin cannot be opened twice."""
    fh, fmt = opened if opened is not None else open_feed(path)
    if fmt == "ebr":
        try:
            if path != "-" and isinstance(fh, io.BufferedReader):
                fh.close()
                s = _Src(path)
            else:
                s = _Src(fh, stream=True)
            try:
                for pr in _ebr_iter(s, kinds, where, lo, hi, st):
                    yield pr
            finally:
                s.close()
        finally:
            if path != "-":
                fh.close()
        return
    # JSONL: read on from the reader open_feed() returned, which replays the
    # bytes it read to tell the format. Reopening would lose them, and '-'
    # (stdin) cannot be reopened at all once closed.
    try:
        for pr in iter_records(fh, kinds, where, st):
            yield pr
    finally:
        if path != "-":
            fh.close()


def _problems(st):
    return (st.get("bad_frames", 0) + st.get("bad_records", 0) + st.get("lost_records", 0)
            + st.get("lost_frames", 0) + st.get("unsupported_frames", 0)
            + st.get("bad_lines", 0))


class _Out(object):
    def __init__(self):
        self.buf = []
        self.n = 0

    def put(self, s):
        self.buf.append(s)
        if len(self.buf) >= 512:
            self.flush()

    def flush(self):
        if self.buf:
            sys.stdout.write("".join(self.buf))
            self.buf = []
        sys.stdout.flush()


def _cmd_cat(a):
    kinds = _kinds_arg(a.kind)
    where = _where_arg(a.where, a.from_, a.to)
    st = {}
    out = _Out()
    many = len(a.files) > 1
    dumps = json.dumps

    def line(path, pos, rec):
        if a.pos:
            return ("%s:%d\t%s\n" % (path, pos, dumps(rec)) if many
                    else "%d\t%s\n" % (pos, dumps(rec)))
        return dumps(rec) + "\n"

    if a.tail is not None:
        want = a.tail
        got = []
        for path in reversed(a.files):
            if len(got) >= want:
                break
            got.extend(_tail_of(path, want - len(got), kinds, where, a.from_, a.to, st))
        for path, pos, rec in reversed(got[:want]):
            out.put(line(path, pos, rec))
    else:
        left = a.head
        for path in a.files:
            if left is not None and left <= 0:
                break
            for pos, rec in _feed_iter(path, kinds, where, a.from_, a.to, st):
                out.put(line(path, pos, rec))
                if left is not None:
                    left -= 1
                    if left <= 0:
                        break
    out.flush()
    if _problems(st):
        _err("data problems: %s" % ", ".join("%s=%d" % kv for kv in sorted(st.items())
                                              if kv[1] and kv[0] not in ("frames", "records", "filtered", "lines")))
        return 1
    return 0


def _tail_of(path, n, kinds, where, lo, hi, st):
    """The last `n` matching (path, pos, rec) of one file, newest first."""
    if n <= 0:
        return []
    fh, fmt = open_feed(path)
    plain = path != "-" and isinstance(fh, io.BufferedReader)
    if fmt == "ebr" and plain:
        fh.close()
        out = []
        s = _Src(path)
        try:
            for pos, rec in records_reverse(s, complete_end(s), 0, kinds, where, st):
                out.append((path, pos, rec))
                if len(out) >= n:
                    break
        finally:
            s.close()
        return out
    ring = []
    for pos, rec in _feed_iter(path, kinds, where, lo, hi, st, opened=(fh, fmt)):
        ring.append((path, pos, rec))
        if len(ring) >= 2 * n:
            del ring[:-n]
    return list(reversed(ring[-n:]))


def _window_flush(w, rec, win, cur):
    if win > 0:
        t = rec.get("ts_epoch")
        if isinstance(t, (int, float)) and not isinstance(t, bool) and t - t == 0:
            k = math.floor(t / win)
            if cur is not None and k != cur:
                w.flush()
            return k
    return cur


def _cmd_convert(a):
    fh, fmt = open_feed(a.input)
    out = gzip.open(a.output, "wb") if a.output.endswith(".gz") else open(a.output, "wb")
    bad = n = 0
    try:
        w = Writer(out, meta={"writer": "ebr.py convert",
                              "source": os.path.basename(a.input)},
                   reset_bytes=a.reset_bytes, zlib=a.zlib)
        cur = None
        if fmt == "ebr":
            st = {}
            src = (_Src(a.input) if a.input != "-" and isinstance(fh, io.BufferedReader)
                   else _Src(fh, stream=True))
            for _pos, rec in _ebr_iter(src, None, None, None, None, st):
                cur = _window_flush(w, rec, a.window, cur)
                w.write(rec)
                n += 1
            bad = _problems(st)
        else:
            for _pos, line, nl in _lines(fh):
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                    if not isinstance(rec, dict):
                        raise ValueError("not an object")
                except ValueError:
                    bad += 1
                    continue
                cur = _window_flush(w, rec, a.window, cur)
                try:
                    w.write(rec)
                except (TypeError, ValueError):
                    bad += 1
                    continue
                n += 1
        w.close()
    finally:
        out.close()
        fh.close()
    sys.stderr.write("ebr: %d records -> %s (%d frames, %d segments, %d bytes)%s\n"
                     % (n, a.output, w.stats["frames"], w.stats["segments"], w.stats["bytes"],
                        ", %d bad lines" % bad if bad else ""))
    return 1 if bad else 0


def _verify_one(path, quick, strict):
    fh, fmt = open_feed(path)
    r = {"file": path, "format": fmt}
    if fmt != "ebr":
        st = {}
        n = 0
        try:
            for _ in iter_records(fh, None, None, st):
                n += 1
        finally:
            fh.close()
        r.update(records=n, bad_lines=st.get("bad_lines", 0), pending=st.get("pending", 0))
        r["problems"] = r["bad_lines"] + (r["pending"] if strict else 0)
        return r
    plain = path != "-" and isinstance(fh, io.BufferedReader)
    if plain:
        fh.close()
        s = _Src(path)
    else:
        s = _Src(fh, stream=True)
    dec = Decoder()
    nframes = nseg = ndata = nadv = header_records = mism = 0
    bad = []
    pos = 0
    try:
        for f in frames(s):
            pos = f.end
            if f.kind == BAD:
                bad.append([f.off, f.end])
                dec.skip(f, s)
                continue
            nframes += 1
            if f.kind == SEGMENT:
                nseg += 1
            elif f.kind == DATA:
                ndata += 1
                header_records += f.n_records
            else:
                nadv += 1
            if quick or f.kind != DATA:
                dec.skip(f, s)
                continue
            reg = None
            got = 0
            for _p, rec in dec.records(s, f):
                got += 1
                if reg is None:
                    reg = (dec.segment or {}).get("kinds")
                    reg = reg if isinstance(reg, list) else list(KINDS)
                    regi = dict((k, i) for i, k in enumerate(reg) if isinstance(k, str))
                ev = rec.get("event")
                if ev is None:
                    bit = BIT_NOEVENT
                elif isinstance(ev, str) and ev in regi and regi[ev] < 30:
                    bit = 1 << regi[ev]
                else:
                    bit = BIT_OTHER
                t = rec.get("ts_epoch")
                inside = True
                if isinstance(t, (int, float)) and not isinstance(t, bool):
                    try:
                        ft = float(t)
                    except OverflowError:
                        ft = _NAN
                    if ft - ft == 0:
                        inside = f.ts_min <= ft <= f.ts_max
                    else:
                        inside = f.ts_min == -_INF and f.ts_max == _INF
                else:
                    inside = f.ts_min == -_INF and f.ts_max == _INF
                if not (f.kinds & bit) or not inside:
                    mism += 1
        size = s.drain(pos)
    finally:
        s.close()
    st = dec.stats
    r.update(frames=nframes, segments=nseg, data_frames=ndata, advisory_frames=nadv,
             records=header_records, decoded=None if quick else st["records"],
             bytes=size, bad_regions=bad, bad_frames=st["bad_frames"],
             bad_records=st["bad_records"], lost_records=st["lost_records"],
             unsupported_frames=st["unsupported_frames"], header_mismatches=mism,
             pending=size - pos)
    r["problems"] = (len(bad) + st["bad_frames"] + st["bad_records"] + st["lost_records"]
                     + st["lost_frames"] + st["unsupported_frames"] + mism
                     + (1 if strict and size > pos else 0))
    return r


def _cmd_verify(a):
    reps = []
    rc = 0
    for path in a.files:
        r = _verify_one(path, a.quick, a.strict)
        reps.append(r)
        if r["problems"]:
            rc = 1
    if a.json:
        sys.stdout.write(json.dumps({"ok": rc == 0, "files": reps}) + "\n")
        return rc
    for r in reps:
        if r["format"] != "ebr":
            sys.stdout.write("%s: jsonl, %d records, %d bad lines, %d pending%s\n" % (
                r["file"], r["records"], r["bad_lines"], r["pending"],
                "" if r["problems"] else ", OK"))
            continue
        sys.stdout.write("%s: ebr, %d frames (%d segments), %d records, %d bytes%s\n" % (
            r["file"], r["frames"], r["segments"], r["records"], r["bytes"],
            "" if r["problems"] else ", OK"))
        for off, end in r["bad_regions"]:
            sys.stdout.write("  BAD region [%d, %d) %d bytes\n" % (off, end, end - off))
        for k in ("bad_frames", "bad_records", "lost_records", "unsupported_frames",
                  "header_mismatches"):
            if r[k]:
                sys.stdout.write("  %s: %d\n" % (k, r[k]))
        if r["pending"]:
            sys.stdout.write("  pending tail: %d bytes%s\n" % (
                r["pending"], " (a problem under --strict)" if a.strict else ""))
    return rc


def _cmd_compare(a):
    sa, sb = {}, {}
    ia = _feed_iter(a.a, None, None, None, None, sa)
    ib = _feed_iter(a.b, None, None, None, None, sb)
    n = diffs = 0
    extra = None
    while True:
        xa = next(ia, None)
        xb = next(ib, None)
        if xa is None or xb is None:
            if xa is not None or xb is not None:
                left = 1 + sum(1 for _ in (ia if xa is not None else ib))
                extra = ("A" if xa is not None else "B", left)
            break
        if not _same(xa[1], xb[1]):
            diffs += 1
            if diffs <= a.max_diffs:
                sys.stdout.write("record %d (A pos %d, B pos %d): %s\n"
                                 % (n, xa[0], xb[0], _diff(xa[1], xb[1])))
        n += 1
    bad = _problems(sa) + _problems(sb)
    rc = 1 if diffs or bad else 0
    msg = "%d records compared, %d differ" % (n, diffs)
    if extra is not None:
        msg += "; %s has %d more" % extra
        if not a.live:
            rc = 1
    if bad:
        msg += "; data problems: A %d, B %d" % (_problems(sa), _problems(sb))
    sys.stdout.write(msg + ("" if rc else ", OK") + "\n")
    return rc


def _count_one(path, kinds):
    fh, fmt = open_feed(path)
    r = {"format": fmt, "records": 0}
    try:
        if kinds:
            by = {}
            st = {}
            n = 0
            # read on from the reader open_feed() returned: it replays what it
            # read to tell the format, and '-' (stdin) cannot be opened twice
            for _p, rec in _feed_iter(path, None, None, None, None, st, opened=(fh, fmt)):
                ev = rec.get("event")
                k = ev if isinstance(ev, str) or ev is None else repr(ev)
                k = "null" if k is None else k
                by[k] = by.get(k, 0) + 1
                n += 1
            r["records"] = n
            r["kinds"] = by
            r["problems"] = _problems(st)
            return r
        if fmt == "ebr":
            plain = path != "-" and isinstance(fh, io.BufferedReader)
            s = _Src(path) if plain else _Src(fh, stream=True)
            n = nf = nbad = 0
            try:
                for f in frames(s, verify=False):
                    if f.kind == DATA:
                        n += f.n_records
                    elif f.kind == BAD:
                        nbad += 1
                    nf += 1
            finally:
                s.close()
            r.update(records=n, frames=nf, bad_regions=nbad, problems=nbad)
            return r
        n = 0
        last = b"\n"
        while True:
            chunk = fh.read(1 << 20)
            if not chunk:
                break
            n += chunk.count(b"\n")
            last = chunk[-1:]
        r["records"] = n
        r["problems"] = 0
        r["unterminated"] = last != b"\n"
        return r
    finally:
        fh.close()


def _cmd_count(a):
    reps = {}
    total = 0
    rc = 0
    for path in a.files:
        r = _count_one(path, a.kinds)
        reps[path] = r
        total += r["records"]
        if r.get("problems"):
            rc = 1
    if a.json:
        sys.stdout.write(json.dumps({"files": reps, "total": total}) + "\n")
        return rc
    for path in a.files:
        r = reps[path]
        if len(a.files) == 1:
            sys.stdout.write("%d\n" % r["records"])
        else:
            sys.stdout.write("%d\t%s\n" % (r["records"], path))
        for k, v in sorted((r.get("kinds") or {}).items()):
            sys.stdout.write("  %d\t%s\n" % (v, k))
    if len(a.files) > 1:
        sys.stdout.write("%d\ttotal\n" % total)
    return rc


def _cmd_append(a):
    meta = {}
    for k in ("host", "stream", "writer"):
        v = getattr(a, k)
        if v:
            meta[k] = v
    meta["pid"] = os.getpid()
    try:
        w = Writer.open_append(a.file, meta=meta, zlib=a.zlib, reset_bytes=a.reset_bytes,
                               lock_wait=a.lock_wait)
    except Busy as e:
        _err(str(e))
        return 2
    bad = 0
    try:
        for _pos, line, nl in _lines(sys.stdin.buffer):
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
                if not isinstance(rec, dict):
                    raise ValueError("not an object")
            except ValueError:
                bad += 1
                continue
            try:
                w.write(rec)
            except (TypeError, ValueError):
                bad += 1
        w.flush()
    finally:
        w.close()
    if bad:
        _err("%d malformed lines skipped" % bad)
        return 1
    return 0


def _cmd_frames(a):
    fh, fmt = open_feed(a.file)
    plain = a.file != "-" and isinstance(fh, io.BufferedReader)
    if fmt != "ebr":
        fh.close()
        _err("%s is not EBR" % a.file)
        return 1
    if plain:
        fh.close()
        s = _Src(a.file)
    else:
        s = _Src(fh, stream=True)
    names = {SEGMENT: "SEGMENT", DATA: "DATA", BAD: "BAD"}
    rc = 0
    pos = 0
    sys.stdout.write("%12s %12s %-8s %5s %9s %7s %19s %19s %9s %10s\n" % (
        "off", "end", "kind", "flags", "payload", "records", "ts_min", "ts_max",
        "seg_back", "kinds"))
    try:
        for f in frames(s, verify=True):
            pos = f.end
            if f.kind == BAD:
                rc = 1
            sys.stdout.write("%12d %12d %-8s %5d %9d %7d %19.6f %19.6f %9d 0x%08x\n" % (
                f.off, f.end, names.get(f.kind, "kind%d" % f.kind), f.flags, f.payload_len,
                f.n_records, f.ts_min, f.ts_max, f.seg_back, f.kinds))
        size = s.drain(pos)
    finally:
        s.close()
        if not plain:
            fh.close()
    if size > pos:
        sys.stdout.write("pending tail: %d bytes at %d\n" % (size - pos, pos))
    return rc


def _groups(recs, win, cap):
    """The records split the way a collector flushes them: one group per
    `win` seconds of ts_epoch (per `cap` records when win is 0)."""
    out = []
    cur = None
    g = []
    for r in recs:
        if win > 0:
            t = r.get("ts_epoch")
            if isinstance(t, (int, float)) and not isinstance(t, bool) and t - t == 0:
                k = math.floor(t / win)
                if cur is not None and k != cur and g:
                    out.append(g)
                    g = []
                cur = k
        elif len(g) >= cap:
            out.append(g)
            g = []
        g.append(r)
    if g:
        out.append(g)
    return out


def _bench_records(path, limit):
    recs = []
    for _pos, rec in iter_records(path):
        recs.append(rec)
        if len(recs) >= limit:
            break
    return recs


def _best(fn, reps):
    best = None
    for _ in range(max(1, reps)):
        t0 = time.perf_counter()
        fn()
        dt = time.perf_counter() - t0
        if best is None or dt < best:
            best = dt
    return best


def _cmd_bench(a):
    recs = _bench_records(a.file, a.limit)
    if not recs:
        _err("%s holds no records" % a.file)
        return 1
    n = len(recs)
    lines = [json.dumps(r) for r in recs]
    jsonl = ("\n".join(lines) + "\n").encode("utf-8")
    win = 0.0 if a.frame_records else a.window
    cap = a.frame_records or 8192

    groups = _groups(recs, win, cap)

    def build(level):
        bio = io.BytesIO()
        w = Writer(bio, meta={"writer": "ebr.py bench"}, zlib=level, max_frame_records=cap)
        wr = w.write
        fl = w.flush
        for g in groups:
            for r in g:
                wr(r)
            fl()
        w.close()
        return bio.getvalue(), w.stats

    def body_only():
        enc = _Encoder()
        rec = enc.record
        for r in recs:
            try:
                rec(r)
            except _Full:
                enc.reset()
                rec(r)

    ebr, wst = build(0)
    ebrz, zst = build(a.zlib)
    gz_j = gzip.compress(jsonl, 6)
    gz_e = gzip.compress(ebr, 6)

    def decode_all(where=None, kinds=None):
        dec = Decoder()
        s = _Src(ebr)
        k = 0
        for f in frames(s):
            for _ in dec.records(s, f, kinds, where):
                k += 1
        return k

    # reject on a field the dashboard filters by (its strings repeat), else the
    # first str field
    first_str = None
    for k, v in recs[0].items():
        if isinstance(v, str) and (first_str is None or k in ("user", "host", "hostname")):
            first_str = k
            if k in ("user", "host", "hostname"):
                break
    reject = {first_str or "event": {"\x00never"}}
    timing = {}
    dumps, loads = json.dumps, json.loads
    timing["json.dumps"] = _best(lambda: [dumps(r) for r in recs], a.reps)
    timing["ebr encode"] = _best(lambda: build(0), a.reps)
    timing["ebr body encode"] = _best(body_only, a.reps)
    timing["json.loads"] = _best(lambda: [loads(x) for x in lines], a.reps)
    try:
        import orjson
        oloads = orjson.loads
        timing["orjson.loads"] = _best(lambda: [oloads(x) for x in lines], a.reps)
    except ImportError:
        timing["orjson.loads"] = None
    timing["ebr decode"] = _best(decode_all, a.reps)
    timing["ebr reject (where)"] = _best(lambda: decode_all(where=reject), a.reps)
    timing["ebr skip (kinds)"] = _best(lambda: decode_all(kinds={"\x00never"}), a.reps)
    us = dict((k, None if v is None else v / n * 1e6) for k, v in timing.items())

    # round trip, and per-kind body sizes
    got = [rec for _p, rec in iter_records(io.BytesIO(ebr))]
    mism = 0
    if len(got) != n:
        mism = abs(len(got) - n)
    for x, line in zip(got, lines):
        if not _same(x, json.loads(line)):
            mism += 1
    enc = _Encoder()
    kinds = {}
    for r, line in zip(recs, lines):
        ev = r.get("event")
        k = ev if isinstance(ev, str) else ("null" if ev is None else repr(ev))
        try:
            body = enc.record(r)
        except _Full:
            enc.reset()
            body = enc.record(r)
        e = kinds.setdefault(k, [0, 0, 0])
        e[0] += 1
        e[1] += len(line) + 1
        e[2] += len(body) + len(_uv(len(body)))
    res = {
        "file": a.file, "records": n, "python": sys.version.split()[0],
        "sizes": {"jsonl": len(jsonl), "ebr": len(ebr), "ebr+zlib%d" % a.zlib: len(ebrz),
                  "jsonl.gz-6": len(gz_j), "ebr.gz-6": len(gz_e)},
        "frames": wst["frames"], "segments": wst["segments"],
        "us_per_record": us,
        "kinds": dict((k, {"records": v[0], "jsonl_b": v[1] / float(v[0]),
                           "ebr_body_b": v[2] / float(v[0])}) for k, v in kinds.items()),
        "mismatches": mism,
    }
    if a.json:
        sys.stdout.write(json.dumps(res) + "\n")
        return 1 if mism else 0
    w = sys.stdout.write
    w("ebr bench: %s, %d records, Python %s, %s\n" % (
        a.file, n, res["python"],
        ("frames of %d records" % a.frame_records) if a.frame_records
        else ("frames per %gs of ts_epoch" % win)))
    w("  %-14s %12s %9s %7s\n" % ("size", "bytes", "B/rec", "ratio"))
    for k in ("jsonl", "ebr", "ebr+zlib%d" % a.zlib, "jsonl.gz-6", "ebr.gz-6"):
        v = res["sizes"][k]
        w("  %-14s %12d %9.1f %6.2fx\n" % (k, v, v / float(n), len(jsonl) / float(v)))
    w("  (%d frames, %d segments)\n" % (wst["frames"], wst["segments"]))
    w("  %-20s %8s  (best of %d)\n" % ("us/record", "", a.reps))
    for k in ("json.dumps", "ebr encode", "ebr body encode", "json.loads", "orjson.loads",
              "ebr decode", "ebr reject (where)", "ebr skip (kinds)"):
        v = us[k]
        w("  %-20s %8s\n" % (k, "n/a" if v is None else "%.2f" % v))
    w("  %-18s %8s %10s %10s %7s\n" % ("event", "records", "jsonl B", "ebr B", "ratio"))
    for k, v in sorted(res["kinds"].items(), key=lambda kv: -kv[1]["records"]):
        w("  %-18s %8d %10.1f %10.1f %6.2fx\n" % (
            k, v["records"], v["jsonl_b"], v["ebr_body_b"], v["jsonl_b"] / v["ebr_body_b"]))
    w("  round-trip mismatches: %d\n" % mism)
    return 1 if mism else 0


def main(argv=None):
    import argparse
    import signal
    try:
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    except (AttributeError, ValueError):
        pass
    if hasattr(sys, "set_int_max_str_digits"):
        sys.set_int_max_str_digits(0)

    p = argparse.ArgumentParser(
        prog="ebr.py", description="EBR (ebpfm binary records) tools. Exit status: "
        "0 ok, 1 a data problem, 2 a usage or I/O error.")
    p.add_argument("--version", action="version", version="ebr %d" % VERSION)
    sub = p.add_subparsers(dest="verb", metavar="VERB")
    sub.required = True

    c = sub.add_parser("cat", help="print records as JSONL (the collector's bytes)")
    g = c.add_mutually_exclusive_group()
    g.add_argument("--head", type=int, metavar="N")
    g.add_argument("--tail", type=int, metavar="N")
    c.add_argument("--from", dest="from_", type=_time_arg, metavar="T",
                   help="ts_epoch >= T (epoch, or local 'YYYY-MM-DD[ HH:MM[:SS]]')")
    c.add_argument("--to", type=_time_arg, metavar="T", help="ts_epoch < T")
    c.add_argument("--kind", action="append", metavar="K,..",
                   help="event names ('null' for none)")
    c.add_argument("--where", action="append", metavar="F=V",
                   help="field equals (str form); F^=P for a prefix; repeat to AND")
    c.add_argument("--pos", action="store_true", help="prefix each line with its position")
    c.add_argument("files", nargs="+", metavar="FILE")
    c.set_defaults(fn=_cmd_cat)

    c = sub.add_parser("convert", help="JSONL (or EBR) to EBR; OUT ending .gz is gzipped")
    c.add_argument("--zlib", type=int, default=0, metavar="L")
    c.add_argument("--reset-bytes", type=int, default=DEFAULT_RESET_BYTES, metavar="B")
    c.add_argument("--window", type=float, default=1.0, metavar="S",
                   help="one frame per S seconds of ts_epoch (0: by size only)")
    c.add_argument("input", metavar="IN", help="'-' for stdin")
    c.add_argument("output", metavar="OUT")
    c.set_defaults(fn=_cmd_convert)

    c = sub.add_parser("verify", help="check every frame and record")
    c.add_argument("--quick", action="store_true", help="frames and CRCs only")
    c.add_argument("--strict", action="store_true", help="a pending tail is a problem")
    c.add_argument("--json", action="store_true")
    c.add_argument("files", nargs="+", metavar="FILE")
    c.set_defaults(fn=_cmd_verify)

    c = sub.add_parser("compare", help="type-exact comparison of two feeds, any formats")
    c.add_argument("--live", action="store_true", help="one may be a prefix of the other")
    c.add_argument("--max-diffs", type=int, default=10, metavar="N")
    c.add_argument("a", metavar="A")
    c.add_argument("b", metavar="B")
    c.set_defaults(fn=_cmd_compare)

    c = sub.add_parser("count", help="records per file (EBR: from frame headers)")
    c.add_argument("--json", action="store_true")
    c.add_argument("--kinds", action="store_true", help="per event (decodes every record)")
    c.add_argument("files", nargs="+", metavar="FILE")
    c.set_defaults(fn=_cmd_count)

    c = sub.add_parser("append", help="append JSONL from stdin, one flush")
    c.add_argument("--host")
    c.add_argument("--stream")
    c.add_argument("--writer")
    c.add_argument("--zlib", type=int, default=0, metavar="L")
    c.add_argument("--reset-bytes", type=int, default=DEFAULT_RESET_BYTES, metavar="B")
    c.add_argument("--lock-wait", type=float, default=30.0, metavar="S")
    c.add_argument("file", metavar="FILE")
    c.set_defaults(fn=_cmd_append)

    c = sub.add_parser("bench", help="sizes and codec speed, in memory")
    c.add_argument("--limit", type=int, default=50000, metavar="N")
    g = c.add_mutually_exclusive_group()
    g.add_argument("--window", type=float, default=0.1, metavar="S")
    g.add_argument("--frame-records", type=int, default=0, metavar="K")
    c.add_argument("--zlib", type=int, default=1, metavar="L")
    c.add_argument("--reps", type=int, default=3, metavar="R")
    c.add_argument("--json", action="store_true")
    c.add_argument("file", metavar="FILE")
    c.set_defaults(fn=_cmd_bench)

    c = sub.add_parser("frames", help="a table of the frames (debugging)")
    c.add_argument("file", metavar="FILE")
    c.set_defaults(fn=_cmd_frames)

    a = p.parse_args(argv)
    try:
        return a.fn(a)
    except Busy as e:
        _err(str(e))
        return 2
    except Error as e:
        _err(str(e))
        return 1
    except (OSError, IOError) as e:
        _err(str(e))
        return 2


if __name__ == "__main__":
    sys.exit(main())
