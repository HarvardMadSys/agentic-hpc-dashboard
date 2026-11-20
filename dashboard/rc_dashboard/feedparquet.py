"""Parquet tables for the collector's eBPF-tier feed: a typed, columnar copy of the
day-files that every record can be read back from exactly.

    python -m rc_dashboard.feedparquet convert --out DIR ROOT [ROOT ...]
    python -m rc_dashboard.feedparquet verify  --out DIR ROOT [ROOT ...]
    python -m rc_dashboard.feedparquet schema  [--markdown]

A ROOT is what `feeds.ebpf.roots` lists: a directory of `<YYYY-MM-DD>/` day dirs of
JSONL day-files, plain or `.gz`. The service keeps the same tables of every sealed
day-file as its history store (`history.py`), and its readers take a sealed file's
records from them (`replica.py`).

LAYOUT. One directory per table, one file per day-file (`<host>.<stream>`, its
rows in the day-file's order), and a manifest per day-file saying what was read:

    DIR/<table>/day=<YYYY-MM-DD>/<host>.<stream>.parquet
    DIR/_manifest/day=<YYYY-MM-DD>/<host>.<stream>.json

so `read_parquet('DIR/exit/*/*.parquet', hive_partitioning=true)` in DuckDB,
`pl.scan_parquet('DIR/exit/**/*.parquet')`, or `pyarrow.dataset.dataset('DIR/exit',
partitioning='hive')` is a whole table, `day` a partition column, and a day leaves
the copy the way it leaves the feed: its directories go. Each file also carries its
host in its `host` column and its source in the footer, so it stands alone.

POSITIONS. Every row also has `_off`, the offset of its line in the uncompressed
day-file, and the manifest keeps what a reader of the file could ask that the rows
cannot say: the file's size and complete end, its first HEAD_BYTES, and the offset
of every line that is not a record (blank, malformed). With those a sealed file's
tables answer every question its readers ask of it -- a time search, a ranged read,
`is_boundary`, the head digest -- with the same offsets (`replica.py`).

TABLES. One per record kind (`TABLES`): `exit` (exit and truncated, one row per
process end), `conn`, `netio`, `residency`, `residency_totals`, `submit`, `meta` and
`stop`. Records are routed by `event`, not by stream, so a capture from before the
exits/snapshot split reads the same. `other` takes an event kind this module does
not know (a pre-6 `tcp`, a future `session`): its envelope, with the rest in
`_extra`, so nothing is ever dropped for being new.

COLUMNS. A table's columns are its records' keys, in the collector's order
(`collector/ebpf_trace.py`: `_envelope`, `_base_identity`, `_exit_record`,
`flush_live`, `conn_tick`, `_emit_netio`, `residency_tick`, `_emit_submit`, `main`),
with types read from the code that writes them and checked against a week of FAS
captures:

* integers are int32 where the kernel bounds them (pids, ports, exit codes,
  signals, depths, a process's threads, `args_len`) and int64 everywhere else;
* every float is float64, as written: the collector rounds at the source, and a
  narrower float would change the number a reader gets back;
* `ts` and `start`, local naive second-granular strings, are timestamp[ms] with no
  zone (Parquet's `isAdjustedToUTC=false`, which is what they are). `ts_epoch` is
  the authoritative time, as in the feed, and stays the float it was;
* nested blocks keep their shape: `io`, `conns`, `net_endpoints` are structs,
  endpoint and peer lists are lists of structs, count maps whose keys are data
  (`by_command`, `providers`, `state_counts`, ...) are map<string, int64>;
* the few blocks with no fixed shape (`meta.config`, `meta.btf_offs`, the run's
  `output` descriptors) are JSON text.

Columns are nullable, and null means what it means in the feed (not measured,
not applicable); it is never a stand-in for a key a record did not have.

EXACT. Two columns make the copy lossless without a registry of past schemas:

* `_absent`: the keys of the record's event (`Table.keys`) the record did not
  have, null when it had them all. A schema-7 record lacks `args_redacted`, so it
  reads `["args_redacted"]`; a reader that cares tells "no such field then" from
  "nothing was masked". A few keys are written only when they hold something
  (`net_endpoints.overflow` and `.unaccounted`, `netio.overflow`,
  `conns.peers_overflow`, the demo's `synthetic`); for those, absent and null are
  the same thing and nothing is recorded.
* `_extra`: a JSON object of everything a column could not hold exactly -- a key
  no table names (schema 9's `pk`, pre-7's `comm`), a value of another type than
  its column's (an int where a float is written: `1` would read back `1.0`), one
  out of its column's range, text that is not valid UTF-8, a timestamp string in
  another format, a nested block of another shape. Null on every row that has
  none, which on the FAS feed is every row.

`records()` puts a record back together from a row: the event's keys in order,
less those `_absent` names, each from its column or from `_extra`, then any key
`_extra` holds that no table names. `verify` compares every record so rebuilt with
the record read from the day-file, type for type (`1` is not `1.0`, `True` is not
`1`), and counts those in another key order apart. On a week of FAS's schema-7
feed (8 hosts, 179 M records) every record was the same, in its own key order; on
the demo's schema 8 every record is the same, the demo writing one block's keys
(`by_agent_type`'s `users`, `autonomous`) in another order than the collector.

What it does not keep: a line that does not parse or is not a JSON object,
counted in the manifest as `malformed`, as the dashboard's readers count it (a
blank line is not counted; each one's offset and kind is in `nonrec`); a last
line with no newline, the torn end of a live file, which ends the read at
`partial`; and the order of keys a record had beyond its event's (an `_extra`
key comes back last).

Encoding: zstd 9, row groups of 1 Mi rows, statistics on every column, dictionary
pages on every column but `pid`/`ppid` (delta-packed) and `ts_epoch` (byte-stream
split) in the tables keyed by process (`BY_PROCESS`). Rows stay in file order,
which is time order to within the collector's 250 ms exit hold, so a filter on
`ts_epoch` skips row groups by their min/max. Measured on a FAS host-day, zstd 9
is 5 % smaller than zstd 3 at no cost that matters next to parsing the JSON, and
zstd 19 10 % smaller again at eight times the encoding time (`--zstd-level`);
row groups of 128 Ki or 256 Ki rows cost 5-8 %.

The day-files are read as the dashboard reads them (`fastjson`): orjson reads an
integer past 64 bits as a float, so the copy holds that float, in `_extra`.
"""
import argparse
import base64
import datetime as _dt
import gzip
import json
import math
import os
import sys
import time

from . import fastjson, feedfile
from .feedfile import HEAD_BYTES

PARQUET_VERSION = 2          # this layout's own version, in every footer and manifest
TS_FORMAT = "%Y-%m-%d %H:%M:%S"
ROW_GROUP_ROWS = 1 << 20     # rows per row group
BATCH_ROWS = 1 << 16         # records converted to Arrow at a time
ZSTD_LEVEL = 9
META_KEY = b"rcfeed"         # the footer's key-value entry this module writes
# The tables hold what the day-files hold -- every user's argv, cwd and traffic --
# so they are made no more readable than the collector makes those (its
# EBPFM_DIR_MODE / EBPFM_FILE_MODE defaults); the umask can only narrow them.
DIR_MODE = 0o750
FILE_MODE = 0o640

_MISSING = object()


def _pa():
    """pyarrow, imported on first use, so a module that only names a kind does not
    load it. It is a main dependency: the history store (history.py) needs it."""
    try:
        import pyarrow
        import pyarrow.compute
        import pyarrow.parquet  # noqa: F401
    except ImportError as e:     # pragma: no cover - exercised only without pyarrow
        raise SystemExit("feedparquet needs pyarrow, a dependency of the dashboard: "
                         "`uv sync` in dashboard/ installs it: %s" % e)
    return pyarrow


# ------------------------------------------------------------------ kinds
class Kind:
    """A column's type: the Arrow type it is stored as, the Python type of a JSON
    value that fits it as-is (`pytype`, None when `check` must look), how to tell
    a value fits, and how to read one back from `Table.to_pylist()`."""
    pytype = None
    nested = False

    def arrow(self):
        raise NotImplementedError

    def check(self, v):
        return type(v) is self.pytype

    def store(self, v):
        return v

    def back(self, v):
        return v


class Scalar(Kind):
    def __init__(self, name, pytype):
        self.name = name
        self.pytype = pytype

    def arrow(self):
        pa = _pa()
        return {"string": pa.string(), "int32": pa.int32(), "int64": pa.int64(),
                "float64": pa.float64(), "bool": pa.bool_()}[self.name]

    def __repr__(self):
        return self.name


class Local(Kind):
    """A local, naive, second-granular time as the collector writes it
    (`2026-11-14 00:00:20`), stored as a timestamp with no zone: timestamp[ms],
    since Parquet has no seconds unit (pyarrow would write [s] as [ms] anyway, and
    read it back as [ms]). Converted a batch at a time (`Builder._local`); a string
    that does not come back the same from the timestamp goes to `_extra`."""
    pytype = str

    def arrow(self):
        return _pa().timestamp("ms")

    def back(self, v):
        return v.strftime(TS_FORMAT)

    def __repr__(self):
        return "timestamp[ms] (local)"


class Json(Kind):
    """Any JSON value, kept as its text: for blocks with no fixed shape."""

    def arrow(self):
        return _pa().string()

    def check(self, v):
        return True

    def store(self, v):
        return json.dumps(v, ensure_ascii=False, separators=(",", ":"))

    def back(self, v):
        return json.loads(v)

    def __repr__(self):
        return "string (JSON)"


class Struct(Kind):
    """A JSON object with fixed keys. It fits only with exactly its keys, each of
    its type; a key in `optional` is one the collector writes only when it holds
    something, so absent and null are one there, and present-with-null does not
    fit (it would come back absent)."""
    nested = True

    def __init__(self, fields, optional=()):
        self.fields = tuple(fields)
        self.kinds = dict(self.fields)
        self.optional = frozenset(optional)
        self.nreq = len(self.fields) - len(self.optional)
        self.names = tuple(n for n, _k in self.fields)
        self.types = tuple(k.pytype for _n, k in self.fields)
        # all scalars, none optional: the collector's own dict is checked by its
        # key tuple and one type test per value (the exit record's `io`, every
        # endpoint), the general walk below only for any other shape
        self.flat = not self.optional and all(
            not k.nested and k.pytype is not None for _n, k in self.fields)

    def arrow(self):
        pa = _pa()
        return pa.struct([pa.field(n, k.arrow()) for n, k in self.fields])

    def check(self, v):
        if type(v) is not dict:
            return False
        if self.flat and tuple(v) == self.names:
            for x, t in zip(v.values(), self.types):
                if x is not None and type(x) is not t:
                    return False
            return True
        kinds, opt = self.kinds, self.optional
        nreq = 0
        for k, x in v.items():
            kind = kinds.get(k)
            if kind is None:
                return False
            if k in opt:
                if x is None:
                    return False
            else:
                nreq += 1
            if x is not None and not kind.check(x):
                return False
        return nreq == self.nreq

    def back(self, v):
        out = {}
        opt = self.optional
        for n, k in self.fields:
            x = v[n]
            if x is None:
                if n not in opt:
                    out[n] = None
            else:
                out[n] = k.back(x) if k.nested else x
        return out

    def __repr__(self):
        return "struct<%s>" % ", ".join("%s: %r" % f for f in self.fields)


class List(Kind):
    nested = True

    def __init__(self, item):
        self.item = item

    def arrow(self):
        return _pa().list_(self.item.arrow())

    def check(self, v):
        if type(v) is not list:
            return False
        item = self.item
        t = item.pytype
        for x in v:
            if x is not None and type(x) is not t and not item.check(x):
                return False
        return True

    def back(self, v):
        if self.item.nested:
            return [None if x is None else self.item.back(x) for x in v]
        return v

    def __repr__(self):
        return "list<%r>" % (self.item,)


class Map(Kind):
    """A JSON object whose keys are data (a command, a provider, a state):
    map<string, value>, in the object's own order."""
    nested = True

    def __init__(self, value):
        self.value = value

    def arrow(self):
        pa = _pa()
        return pa.map_(pa.string(), self.value.arrow())

    def check(self, v):
        if type(v) is not dict:
            return False
        val = self.value
        t = val.pytype
        for x in v.values():
            if x is not None and type(x) is not t and not val.check(x):
                return False
        return True

    def back(self, v):
        if self.value.nested:
            return {k: (None if x is None else self.value.back(x)) for k, x in v}
        return dict(v)

    def __repr__(self):
        return "map<string, %r>" % (self.value,)


class NullOnly(Kind):
    """A key the collector always writes as null (`samples`: a placeholder kept for
    the poller's readers). No column; a value it ever holds goes to `_extra`."""

    def arrow(self):
        return None

    def check(self, v):
        return False

    def __repr__(self):
        return "always null (no column)"


STR = Scalar("string", str)
I32 = Scalar("int32", int)
I64 = Scalar("int64", int)
F64 = Scalar("float64", float)
BOOL = Scalar("bool", bool)
LOCAL = Local()
JSON = Json()
NULL = NullOnly()
COUNTS = Map(I64)
OPT = "optional"

# ------------------------------------------------------------------ blocks
IO = Struct([("rd_mb", F64), ("wr_mb", F64), ("rchar_mb", F64), ("wchar_mb", F64),
             ("scope", STR)])
PEER = Struct([("daddr", STR), ("dport", I32), ("n", I64), ("external", BOOL),
               ("provider", STR), ("inbound", I64), ("failed", I64),
               ("rx_bytes", I64), ("tx_bytes", I64)])
CONNS = Struct([("n", I64), ("accepted", I64), ("established", I64), ("failed", I64),
                ("external", I64), ("rx_bytes", I64), ("tx_bytes", I64),
                ("conn_s_total", F64), ("connect_ms_p50", F64), ("connect_ms_max", F64),
                ("providers", COUNTS), ("peers", List(PEER)), ("peers_total", I64),
                ("peers_overflow", I64)], optional=("peers_overflow",))
ENDPOINT = Struct([("proto", STR), ("addr", STR), ("port", I32), ("host", STR),
                   ("inbound", BOOL), ("external", BOOL), ("provider", STR),
                   ("tx_bytes", I64), ("rx_bytes", I64), ("calls", I64)])
UNATTRIBUTED = Struct([("tx_bytes", I64), ("rx_bytes", I64), ("calls", I64)])
OVERFLOW = Struct([("n", I64), ("tx_bytes", I64), ("rx_bytes", I64), ("calls", I64)])
UNACCOUNTED = Struct([("tx_bytes", I64), ("rx_bytes", I64)])
NET_ENDPOINTS = Struct([("n", I64), ("in_series", BOOL), ("endpoints", List(ENDPOINT)),
                        ("unattributed", UNATTRIBUTED), ("overflow", OVERFLOW),
                        ("unaccounted", UNACCOUNTED)],
                       optional=("overflow", "unaccounted"))
TOP_PROC = Struct([("pid", I32), ("command", STR), ("rss_mb", F64), ("cpu_s", F64),
                   ("cpu_delta_s", F64), ("state", STR), ("threads", I32)])
NETPEER_TOTAL = Struct([("calls", I64), ("tx_bytes", I64), ("rx_bytes", I64)])
NETPEER_ORPHANS = Struct([("entries", I64), ("tx_bytes", I64), ("rx_bytes", I64),
                          ("calls", I64)])
BY_ACTOR3 = Map(Struct([("n_procs", I64), ("threads", I64), ("rss_mb", F64),
                        ("d_state", I64), ("users", I64), ("states", COUNTS)]))
BY_AGENT_TYPE = Map(Struct([("trees", I64), ("n_procs", I64), ("threads", I64),
                            ("rss_mb", F64), ("cpu_s", F64), ("d_state", I64),
                            ("users", I64), ("autonomous", I64)]))
DROPPED = Struct([("n", I64), ("by_command", COUNTS)])
TCP_OPEN = Map(Struct([("open", I64), ("ext", I64), ("estab", I64)]))
THREAD_ACC = Struct([("taken", I64), ("late", I64), ("missing", I64), ("stale", I64),
                     ("swept", I64), ("full", I64)])
ARGV_HOSTS = Struct([("resolved", I64), ("failed", I64), ("dropped", I64)])

# ------------------------------------------------------------------ keys
# (key, kind[, OPT]) in the collector's order. OPT: written only when it holds
# something, so its absence is not recorded in `_absent`.
ENVELOPE = (("ts", LOCAL), ("ts_epoch", F64), ("host", STR), ("event", STR),
            ("collector_pid", I32), ("schema_version", I32),
            ("synthetic", BOOL, OPT))       # the demo feed's mark (`rc_dashboard.demo`)

IDENTITY = (("actor", STR), ("actor3", STR), ("attribution", STR), ("pid", I32),
            ("ppid", I32), ("uid", I64), ("user", STR), ("tty", STR), ("command", STR),
            ("args", STR), ("args_redacted", List(STR)), ("argv_source", STR),
            ("args_len", I32), ("args_truncated", BOOL), ("cwd", STR), ("sandbox", STR),
            ("sandbox_src", STR), ("sandbox_detail", STR), ("sandbox_ancestry", STR),
            ("approval_mode", STR), ("approval_src", STR), ("agent_type", STR),
            ("agent_pid", I32), ("session_key", STR), ("session_uuid", STR),
            ("tree_root_pid", I32), ("depth", I32), ("is_agent", BOOL),
            ("autonomous", BOOL), ("cgroup", STR))

EXIT = ENVELOPE + IDENTITY + (
    ("fork_only", BOOL), ("start", LOCAL), ("duration_s", F64), ("duration_src", STR),
    ("cpu_s", F64), ("cpu_scope", STR), ("child_cpu_s", F64), ("cpu_pct", F64),
    ("peak_rss_mb", F64), ("threads", I32), ("min_flt", I64), ("maj_flt", I64),
    ("cmaj_flt", I64), ("nvcsw", I64), ("nivcsw", I64), ("sched_run_s", F64),
    ("sched_wait_s", F64), ("blkio_wait_s", F64), ("swapin_wait_s", F64),
    ("freepages_wait_s", F64), ("dstate_wait_s", F64), ("dstate_episodes", I64),
    ("dstate_max_s", F64), ("dstate_src", STR), ("net_tx_bytes", I64),
    ("net_rx_bytes", I64), ("net_calls", I64), ("pgrp", I32), ("sid", I32),
    ("exit_code", I32), ("signal", I32), ("core_dumped", BOOL), ("io", IO),
    ("samples", NULL), ("state_last", STR), ("conns", CONNS),
    ("net_endpoints", NET_ENDPOINTS))

# flush_live(): a process still alive at stop, its counters read from /proc
TRUNCATED = ENVELOPE + IDENTITY + (
    ("start", LOCAL), ("duration_s", F64), ("duration_src", STR), ("cpu_s", F64),
    ("cpu_scope", STR), ("child_cpu_s", F64), ("rss_mb", F64), ("threads", I32),
    ("state_last", STR), ("min_flt", I64), ("maj_flt", I64), ("dstate_wait_s", F64),
    ("dstate_episodes", I64), ("dstate_max_s", F64), ("dstate_src", STR),
    ("net_tx_bytes", I64), ("net_rx_bytes", I64), ("net_calls", I64), ("io", IO),
    ("conns", CONNS), ("net_endpoints", NET_ENDPOINTS), ("exit_code", I32),
    ("signal", I32), ("core_dumped", BOOL), ("samples", NULL))

CONN = ENVELOPE + IDENTITY + (
    ("family", STR), ("proto", STR), ("saddr", STR), ("sport", I32), ("daddr", STR),
    ("dport", I32), ("external", BOOL), ("provider", STR), ("inbound", BOOL),
    ("age_s", F64), ("established", BOOL), ("stats_src", STR), ("state", STR),
    ("rtt_ms", F64), ("retrans", I64), ("rx_bytes", I64), ("tx_bytes", I64),
    ("idle_send_s", F64), ("idle_recv_s", F64), ("segs_in", I64), ("segs_out", I64),
    ("rqueue", I64), ("wqueue", I64))

NETIO = ENVELOPE + IDENTITY + (
    ("reason", STR), ("interval_s", F64), ("endpoints", List(ENDPOINT)),
    ("unattributed", UNATTRIBUTED), ("overflow", OVERFLOW, OPT))

RESIDENCY = ENVELOPE + IDENTITY + (
    ("root_alive", BOOL), ("age_s", F64), ("headless", BOOL), ("n_procs", I64),
    ("threads", I64), ("rss_mb", F64), ("cpu_s", F64), ("d_state", I64),
    ("max_depth", I32), ("agent_types", List(STR)), ("by_command", COUNTS),
    ("d_stack", COUNTS), ("state_counts", COUNTS), ("top_procs", List(TOP_PROC)),
    ("tcp_open_ext", I64), ("tcp_open", I64))

RESIDENCY_TOTALS = ENVELOPE + (
    ("interval_s", F64), ("tracked_live", I64), ("trees", I64), ("conn_records", I64),
    ("by_actor3", BY_ACTOR3), ("by_agent_type", BY_AGENT_TYPE), ("dropped", DROPPED),
    ("tcp_open_ext_by_actor3", TCP_OPEN), ("conn_unattributed", I64),
    ("conn_unmatched_births", I64), ("fork_total", I64), ("fork_rate", F64),
    ("d_stack_top", COUNTS), ("d_stacks_read", I64), ("netio_records", I64),
    ("netpeer_entries", I64), ("netpeer_full", NETPEER_TOTAL),
    ("netpeer_orphans", NETPEER_ORPHANS), ("netpeer_kernel", NETPEER_TOTAL),
    ("perf_seen", COUNTS), ("perf_lost", COUNTS), ("handler_errors", COUNTS),
    ("proc_entries", I64), ("proc_vanished", I64), ("proc_late_exits", I64),
    ("thread_acc", THREAD_ACC))

SUBMIT = ENVELOPE + IDENTITY + (
    ("partition", STR), ("gpus", I64), ("array", STR), ("time_limit_s", I64),
    ("mem", STR), ("cpus_per_task", I64), ("req_src", STR), ("tool", STR),
    ("job_id", I64), ("job_ids", List(I64)), ("work_dir", STR), ("exit_code", I32),
    ("signal", I32), ("start", LOCAL), ("duration_s", F64), ("captured_bytes", I64),
    ("captured_tail", STR))

META = ENVELOPE + (
    ("kernel", STR), ("bcc", STR), ("euid", I64), ("tz", STR), ("boot_id", STR),
    ("features", List(STR)), ("features_rejected", Map(STR)), ("cpu_scope", STR),
    ("counters_scope", STR), ("redact", List(STR)), ("argv_source_default", STR),
    ("dstate_src_default", STR), ("btf_offs", JSON), ("sysctl", Map(STR)),
    ("cgroup", STR), ("netlink_sockdiag", BOOL), ("unsafe_dirs", JSON),
    ("config", JSON), ("seeded_pids", I64), ("output", JSON))

STOP = ENVELOPE + (
    ("uptime_s", F64), ("records", I64), ("truncated", I64), ("perf_lost", COUNTS),
    ("perf_seen", COUNTS), ("handler_errors", COUNTS), ("proc_vanished", I64),
    ("proc_late_exits", I64), ("flush_mode", STR), ("flushes", I64),
    ("conns_dropped", I64), ("conns_dropped_commands", COUNTS), ("netio_records", I64),
    ("netpeer_full", NETPEER_TOTAL), ("netpeer_orphans", NETPEER_ORPHANS),
    ("argv_hosts", ARGV_HOSTS), ("reason", STR), ("output", JSON))

# An event kind no table names: its envelope, every key optional, the rest in _extra.
OTHER = tuple((k, kind, OPT) for k, kind, *_ in ENVELOPE)


class Table:
    """One Parquet table: the events it holds, each with its keys in order
    (`keys`), and the columns those keys make (`columns`, in `order`).
    `encodings` takes columns off dictionary encoding (`writer_options`)."""

    def __init__(self, name, doc, keys, order=None, encodings=None):
        self.name = name
        self.doc = doc
        self.encodings = dict(encodings or {})
        self.events = tuple(keys)
        self.keys = {}          # event -> ((key, kind, optional), ...)
        kinds = {}
        for ev, spec in keys.items():
            self.keys[ev] = tuple((s[0], s[1], len(s) > 2 and s[2] == OPT) for s in spec)
            for k, kind, _opt in self.keys[ev]:
                if kinds.setdefault(k, kind) is not kind:
                    raise ValueError("%s: %s has two kinds" % (name, k))
        if order is None:
            order = []
            for spec in self.keys.values():
                order += [k for k, _kind, _o in spec if k not in order]
        self.columns = tuple((k, kinds[k]) for k in order if kinds[k] is not NULL)
        missing = set(k for k in kinds if kinds[k] is not NULL) - set(order)
        if missing:
            raise ValueError("%s: no column order for %s" % (name, sorted(missing)))
        self.index = {k: i for i, (k, _kind) in enumerate(self.columns)}
        self._schema = None
        self._leaves = None

    @property
    def leaves(self):
        """The Parquet leaf column paths (`conns.peers.list.element.n`), as an
        empty file of this schema names them: what per-column options take."""
        if self._leaves is None:
            pa = _pa()
            buf = pa.BufferOutputStream()
            pa.parquet.write_table(self.schema.empty_table(), buf)
            pf = pa.parquet.ParquetFile(pa.BufferReader(buf.getvalue()))
            self._leaves = tuple(pf.schema.column(i).path for i in range(len(pf.schema)))
        return self._leaves

    @property
    def schema(self):
        if self._schema is None:
            pa = _pa()
            fields = [pa.field(k, kind.arrow()) for k, kind in self.columns]
            fields += [pa.field("_off", pa.int64()),
                       pa.field("_absent", pa.list_(pa.string())),
                       pa.field("_extra", pa.string())]
            self._schema = pa.schema(fields)
        return self._schema


def _after(spec, key, extra):
    """`spec`'s keys in order, with `extra` placed right after `key`."""
    out = []
    for k, _kind, *_ in spec:
        out.append(k)
        if k == key:
            out += extra
    return out


# Every column is dictionary-encoded but these. In the tables keyed by process, a
# pid is near its neighbours' (pids are handed out in order, and a tree's
# processes exit together) and nearly unique, so its deltas pack where a
# dictionary only grows; ts_epoch's bytes, split by position, leave zstd long runs
# of the same high bytes. On a FAS host-day: exit 10.5 % smaller, netio and
# residency 4 %. Not on conn, whose sockets repeat each tick with the same pid,
# which is what a dictionary is for (there it cost 1 %).
BY_PROCESS = {"pid": "DELTA_BINARY_PACKED", "ppid": "DELTA_BINARY_PACKED",
              "ts_epoch": "BYTE_STREAM_SPLIT"}

TABLES = {t.name: t for t in (
    Table("exit", "one row per process end: `exit` at exit, `truncated` for one still "
          "alive at the collector's stop (counters from /proc)",
          {"exit": EXIT, "truncated": TRUNCATED},
          order=_after(EXIT, "peak_rss_mb", ["rss_mb"]), encodings=BY_PROCESS),
    Table("conn", "one row per standing external socket per residency tick", {"conn": CONN}),
    Table("netio", "one row per process that moved bytes, per tick: bytes per endpoint "
          "since its previous netio", {"netio": NETIO}, encodings=BY_PROCESS),
    Table("residency", "one row per live agent tree per tick", {"residency": RESIDENCY},
          encodings=BY_PROCESS),
    Table("residency_totals", "one row per host per tick: census, loss and health "
          "counters", {"residency_totals": RESIDENCY_TOTALS}),
    Table("submit", "one row per sbatch/salloc/srun: the request and the job id",
          {"submit": SUBMIT}),
    Table("meta", "one row per collector start: kernel, features, settings", {"meta": META}),
    Table("stop", "one row per collector stop: the run's totals", {"stop": STOP}),
    Table("other", "an event kind no other table names: the envelope, the rest in _extra",
          {None: OTHER}),
)}
EVENT_TABLE = {ev: t for t in TABLES.values() for ev in t.events if ev is not None}


def table_of(rec):
    ev = rec.get("event")
    return EVENT_TABLE.get(ev) if type(ev) is str else None


# ------------------------------------------------------------------ records -> Arrow
class Builder:
    """Records of one table, `add`ed in order, into Arrow record batches.

    A record is read through its LAYOUT: the tuple of its keys, in its order.
    Every record of an event the collector writes has the same one, so what a
    layout means -- which column each position fills, how its value is checked,
    which of the event's keys it lacks (`_absent`) and which it has that no
    column names (`_extra`) -- is worked out once and the record's values are
    walked by position."""
    LAYOUTS_MAX = 1024

    def __init__(self, table):
        self.table = table
        self.ncols = len(table.columns) + 3      # then `_off`, `_absent`, `_extra`
        self.rows = []
        self.layouts = {}
        self.extra_rows = 0

    def _layout(self, ev, keys):
        spec = self.table.keys.get(ev)
        if spec is None:
            spec = self.table.keys.get(None) or next(iter(self.table.keys.values()))
        known = {}
        for k, kind, opt in spec:
            i = self.table.index.get(k, -1)
            pyt = kind.pytype if i >= 0 and not kind.nested else None
            known[k] = (i, pyt, kind, k, opt)
        # a key the event does not have: no column, and kept even when null
        steps = tuple(known.get(k) or (-1, None, None, k, True) for k in keys)
        present = set(keys)
        absent = [k for k, _kind, opt in spec if k not in present and not opt] or None
        if len(self.layouts) >= self.LAYOUTS_MAX:
            self.layouts.clear()
        lay = self.layouts[(ev, keys)] = (steps, absent)
        return lay

    def add(self, rec, off=None):
        """Add one record; `off` is its line's offset in its day-file (`_off`)."""
        ev = rec.get("event")
        if type(ev) is not str:
            ev = None
        keys = tuple(rec)
        lay = self.layouts.get((ev, keys))
        if lay is None:
            lay = self._layout(ev, keys)
        steps, absent = lay
        row = [None] * self.ncols
        extra = None
        for (i, pyt, kind, k, opt), v in zip(steps, rec.values()):
            if v is None:
                if opt:                       # present, null: it would read back absent
                    if extra is None:
                        extra = {}
                    extra[k] = None
                continue
            if type(v) is pyt:
                row[i] = v
                continue
            if i >= 0 and pyt is None and kind.check(v):
                row[i] = kind.store(v)
                continue
            if extra is None:
                extra = {}
            extra[k] = v
        row[-3] = off
        row[-2] = absent
        row[-1] = extra
        self.rows.append(row)
        return len(self.rows)

    def batch(self):
        """The rows added since the last batch, as a RecordBatch (None if none)."""
        rows = self.rows
        if not rows:
            return None
        pa = _pa()
        self.rows = []
        cols = list(zip(*rows))
        arrays = []
        for i, (k, kind) in enumerate(self.table.columns):
            arrays.append(self._array(k, kind, cols[i], rows))
        arrays.append(pa.array(cols[-3], type=pa.int64()))
        arrays.append(pa.array(cols[-2], type=pa.list_(pa.string())))
        extras = []
        for r in rows:
            e = r[-1]
            if e is None:
                extras.append(None)
            else:
                self.extra_rows += 1
                extras.append(json.dumps(e, ensure_ascii=True, separators=(",", ":")))
        arrays.append(pa.array(extras, type=pa.string()))
        return pa.RecordBatch.from_arrays(arrays, schema=self.table.schema)

    def _array(self, k, kind, vals, rows):
        pa = _pa()
        if kind is LOCAL:
            return self._local(k, vals, rows)
        try:
            return pa.array(vals, type=kind.arrow())
        except (pa.ArrowInvalid, pa.ArrowTypeError, OverflowError, UnicodeError,
                ValueError, TypeError):
            pass
        # Something in this batch does not fit (out of range, not UTF-8): find it.
        t = kind.arrow()
        good = []
        for j, v in enumerate(vals):
            if v is not None:
                try:
                    pa.array([v], type=t)
                except (pa.ArrowInvalid, pa.ArrowTypeError, OverflowError, UnicodeError,
                        ValueError, TypeError):
                    self._misfit(rows[j], k, kind.back(v) if kind is JSON else v)
                    v = None
            good.append(v)
        return pa.array(good, type=t)

    def _local(self, k, vals, rows):
        pa = _pa()
        pc = pa.compute
        try:
            s = pa.array(vals, type=pa.string())
        except UnicodeError:
            s = None
        if s is None:
            good = []
            for j, v in enumerate(vals):
                if v is not None:
                    try:
                        v.encode("utf-8")
                    except UnicodeError:
                        self._misfit(rows[j], k, v)
                        v = None
                good.append(v)
            s = pa.array(good, type=pa.string())
        # parsed at [s], so that strftime's %S prints no fraction to compare
        t = pc.strptime(s, format=TS_FORMAT, unit="s", error_is_null=True)
        same = pc.equal(pc.strftime(t, format=TS_FORMAT), s)
        bad = pc.and_kleene(s.is_valid(), pc.invert(pc.fill_null(same, False)))
        if pc.any(bad).as_py():
            for j in pc.indices_nonzero(bad).to_pylist():
                self._misfit(rows[j], k, vals[j])
            t = pc.if_else(bad, pa.scalar(None, t.type), t)
        return t.cast(LOCAL.arrow())

    @staticmethod
    def _misfit(row, k, v):
        if row[-1] is None:
            row[-1] = {}
        row[-1][k] = v


# ------------------------------------------------------------------ Arrow -> records
def rebuild(table, row):
    """The record a row of `table` came from (a dict from `to_pylist()`)."""
    extra = row.get("_extra")
    extra = json.loads(extra) if extra else None
    absent = row.get("_absent")
    ev = row.get("event")
    if extra is not None and "event" in extra:
        ev = extra["event"]
    spec = table.keys.get(ev if type(ev) is str else None)
    if spec is None:
        spec = table.keys.get(None) or next(iter(table.keys.values()))
    out = {}
    for k, kind, opt in spec:
        if extra is not None and k in extra:
            out[k] = extra.pop(k)
            continue
        if absent and k in absent:
            continue
        v = row.get(k) if kind is not NULL else None
        if v is None:
            if not opt:
                out[k] = None
        else:
            out[k] = kind.back(v) if (kind.nested or kind is LOCAL or kind is JSON) else v
    if extra:
        out.update(extra)
    return out


def records(path, offsets=False):
    """Every record a Parquet file of this layout holds, in order; with `offsets`,
    as `(off, record)`, `off` its line's offset in its day-file. (Every column is
    read: a record cannot be put back together from some of them.)"""
    pa = _pa()
    pf = pa.parquet.ParquetFile(path)
    name = (pf.schema_arrow.metadata or {}).get(META_KEY)
    table = TABLES[json.loads(name)["table"]] if name else None
    if table is None:
        raise ValueError("%s: not a feedparquet file (no %r in its footer)" % (path, META_KEY))
    for b in pf.iter_batches(batch_size=BATCH_ROWS):
        for row in b.to_pylist():
            yield (row["_off"], rebuild(table, row)) if offsets else rebuild(table, row)


# ------------------------------------------------------------------ day-files -> records
class Source:
    """The records of one JSONL day-file as `(offset, record)`, in order, offsets
    into the uncompressed file. What is not a record is kept by its offset in
    `nonrec` -- a blank line, a line that does not parse (`bad`) or is not an
    object (`nondict`) -- because a reader's time search counts those lines too
    (`feedfile._probe`); and a last line with no newline, which every reader
    leaves for the writer to finish, ends the read (`partial`, its offset).
    After the read: `usize` the bytes read, `head` the first HEAD_BYTES of them."""

    def __init__(self, path):
        self.path = path
        self.malformed = 0
        self.nonrec = []
        self.partial = None
        self.usize = 0
        self.head = b""

    def __iter__(self):
        opener = gzip.open if self.path.endswith(".gz") else open
        loads = fastjson.loads
        off = 0
        head = bytearray()
        with opener(self.path, "rb") as fh:
            for line in fh:
                at = off
                off += len(line)
                if len(head) < HEAD_BYTES:
                    head += line[:HEAD_BYTES - len(head)]
                if not line.endswith(b"\n"):
                    self.partial = at          # a last line still being written
                    break
                if not line.strip():
                    self.nonrec.append([at, "blank"])
                    continue
                try:
                    rec = loads(line)
                except ValueError:
                    self.malformed += 1
                    self.nonrec.append([at, "bad"])
                    continue
                if type(rec) is not dict:
                    self.malformed += 1
                    self.nonrec.append([at, "nondict"])
                    continue
                yield at, rec
        self.usize = off
        self.head = bytes(head)


def identity(path):
    """What a source file is: its name and what `stat` says of it. A replica is
    current while its source still says the same (a sealed `.gz` never changes)."""
    st = os.stat(path)
    return {"name": os.path.basename(path), "size": st.st_size,
            "mtime_ns": st.st_mtime_ns, "ino": st.st_ino}


def stem_of(path):
    """`<host>.<stream>` of a day-file: what its tables' files are named."""
    return feedfile.split_name(path)[0]


def sources(roots, days=0, include_today=False, today=None):
    """`[(day, path)]` for every day-file under `roots`, oldest first. Today's
    are left out unless asked for: a live day is still growing."""
    today = today or _dt.date.today().isoformat()
    out = []
    for root in roots:
        for day, files in feedfile.dated_dirs(root, days):
            if day >= today and not include_today:
                continue
            out += [(day, p) for p in files]
    return sorted(set(out))


def out_path(out, table, day, stem):
    return os.path.join(out, table, "day=%s" % day, "%s.parquet" % stem)


def manifest_path(out, day, stem):
    return os.path.join(out, "_manifest", "day=%s" % day, "%s.json" % stem)


def read_manifest(out, day, stem):
    try:
        with open(manifest_path(out, day, stem)) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def current(man, path, out=None):
    """Whether manifest `man` describes `path` as it is now, in this version --
    and, given the store root `out` it was written under, whether every table
    it names is there as it was written (`tables_whole`)."""
    try:
        return (man is not None and man.get("parquet_version") == PARQUET_VERSION
                and man.get("source") == identity(path)
                and (out is None or tables_whole(man, out)))
    except OSError:
        return False


def tables_whole(man, out):
    """Whether each table manifest `man` names records in is a file under store
    root `out` of the byte size the conversion wrote (`bytes`). A table gone,
    cut short or emptied -- the store cleaned up in part, a crash before its
    data reached the disk -- leaves a manifest that names a replica there is
    not: the file is read as JSONL, and converted again."""
    sizes = man.get("bytes")
    if not isinstance(sizes, dict):
        return False
    for name, n in (man.get("records") or {}).items():
        if not n:
            continue
        want = sizes.get(name)
        try:
            got = os.path.getsize(out_path(out, name, man["day"], man["stem"]))
        except (OSError, KeyError, TypeError):
            return False
        if want is None or got != want:
            return False
    return True


def _fsync(path, directory=False):
    """`path`'s data on the disk, or (`directory`) its entries: a rename names
    the new file only once the file itself is there. A directory's flush is as
    far as the filesystem allows: some refuse one, and it then goes without."""
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    except OSError:
        if not directory:
            raise
    finally:
        os.close(fd)


def writer_options(table, level=ZSTD_LEVEL):
    """zstd at `level`, statistics on every column, and dictionary pages on every
    column but those `table.encodings` encodes otherwise (and `_off`, delta-packed:
    an offset is unique and ascending)."""
    enc = dict(table.encodings, _off="DELTA_BINARY_PACKED")
    return dict(compression="zstd", compression_level=level, write_statistics=True,
                use_dictionary=[p for p in table.leaves if p not in enc],
                column_encoding=enc)


def convert_source(day, path, out, level=ZSTD_LEVEL,
                   row_group_rows=ROW_GROUP_ROWS, batch_rows=BATCH_ROWS):
    """Write the tables of one day-file, then its manifest. Files are written
    beside their final names, flushed to the disk and renamed into place, the
    manifest last, so a manifest names only files that are whole -- after a
    crash or a power loss too."""
    pa = _pa()
    pq = pa.parquet
    t0 = time.monotonic()
    stem = stem_of(path)
    host = feedfile.host_of(path)
    ident = identity(path)
    builders, writers, pending, counts = {}, {}, {}, {}
    tmp_tag = ".tmp-%d" % os.getpid()

    def footer(name):
        return {META_KEY: json.dumps({
            "parquet_version": PARQUET_VERSION, "table": name, "host": host, "day": day,
            "stem": stem, "source": ident}, separators=(",", ":")).encode()}

    def flush(name, final=False):
        b = builders[name].batch()
        if b is not None:
            pending[name].append(b)
        rows = sum(x.num_rows for x in pending[name])
        if pending[name] and (final or rows >= row_group_rows):
            if name not in writers:
                p = out_path(out, name, day, stem)
                _makedirs(os.path.dirname(p))
                schema = TABLES[name].schema.with_metadata(footer(name))
                writers[name] = pq.ParquetWriter(p + tmp_tag, schema,
                                                 **writer_options(TABLES[name], level))
            writers[name].write_table(pa.Table.from_batches(pending[name]),
                                      row_group_size=row_group_rows)
            pending[name] = []

    src = Source(path)
    try:
        for off, rec in src:
            t = table_of(rec) or TABLES["other"]
            name = t.name
            b = builders.get(name)
            if b is None:
                b = builders[name] = Builder(t)
                pending[name] = []
                counts[name] = 0
            counts[name] += 1
            if b.add(rec, off) >= batch_rows:
                flush(name)
        for name in builders:
            flush(name, final=True)
        for name, w in writers.items():
            w.close()
        # Each table on the disk before its name, and every name before the
        # manifest that points at them: renamed with nothing flushed, a power
        # loss could leave a manifest naming tables that were never written.
        for name in writers:
            p = out_path(out, name, day, stem)
            os.chmod(p + tmp_tag, FILE_MODE)
            _fsync(p + tmp_tag)
            os.replace(p + tmp_tag, p)
        for d in sorted({os.path.dirname(out_path(out, n, day, stem)) for n in writers}):
            _fsync(d, directory=True)
    except BaseException:
        for name, w in writers.items():
            try:
                w.close()
            except Exception:
                pass
            try:
                os.unlink(out_path(out, name, day, stem) + tmp_tag)
            except OSError:
                pass
        raise
    # a table this day-file no longer has must not keep an older conversion's file
    for name in TABLES:
        if name not in writers:
            try:
                os.unlink(out_path(out, name, day, stem))
            except OSError:
                pass
    man = {"parquet_version": PARQUET_VERSION, "day": day, "stem": stem, "host": host,
           "source": ident, "usize": src.usize,
           "complete_end": src.usize if src.partial is None else src.partial,
           "partial": src.partial, "head": base64.b64encode(src.head).decode(),
           "nonrec": src.nonrec, "records": counts, "malformed": src.malformed,
           "extra_rows": {n: b.extra_rows for n, b in builders.items() if b.extra_rows},
           "seconds": round(time.monotonic() - t0, 1),
           "bytes": {n: os.path.getsize(out_path(out, n, day, stem)) for n in writers}}
    mp = manifest_path(out, day, stem)
    _makedirs(os.path.dirname(mp))
    with open(mp + tmp_tag, "w") as fh:
        json.dump(man, fh, indent=1, sort_keys=True)
        fh.flush()
        os.fsync(fh.fileno())
    os.chmod(mp + tmp_tag, FILE_MODE)
    os.replace(mp + tmp_tag, mp)
    _fsync(os.path.dirname(mp), directory=True)
    return man


def drop_source(out, day, stem):
    """Remove every table file and the manifest of one day-file's replica (its
    source is gone), the manifest first so no reader takes a half-gone replica."""
    for p in [manifest_path(out, day, stem)] + [out_path(out, t, day, stem) for t in TABLES]:
        try:
            os.unlink(p)
        except OSError:
            pass


def _makedirs(d):
    """`d` and any parent it needs, each made DIR_MODE (as the umask allows)."""
    if os.path.isdir(d):
        return
    parent = os.path.dirname(d)
    if parent and parent != d:
        _makedirs(parent)
    try:
        os.mkdir(d, DIR_MODE)
    except FileExistsError:
        pass


# ------------------------------------------------------------------ verify
def same(a, b):
    """Equal as JSON values, type for type: 1 is not 1.0, True is not 1, and a
    NaN equals a NaN. Key order is not compared here (`same_order` does)."""
    ta, tb = type(a), type(b)
    if ta is not tb:
        return False
    if ta is dict:
        if len(a) != len(b):
            return False
        for k, v in a.items():
            if k not in b or not same(v, b[k]):
                return False
        return True
    if ta is list:
        return len(a) == len(b) and all(same(x, y) for x, y in zip(a, b))
    if ta is float and math.isnan(a):
        return math.isnan(b)
    return a == b


def same_order(a, b):
    if type(a) is dict:
        return list(a) == list(b) and all(same_order(v, b[k]) for k, v in a.items())
    if type(a) is list:
        return all(same_order(x, y) for x, y in zip(a, b))
    return True


def verify_source(day, path, out, limit=5):
    """Read every record of a day-file and of its tables side by side, each
    table in order, and compare the records and their offsets. A report dict."""
    stem = stem_of(path)
    iters, counts = {}, {}
    diffs, order = [], 0
    n = 0
    for off, rec in Source(path):
        t = table_of(rec) or TABLES["other"]
        it = iters.get(t.name)
        if it is None:
            p = out_path(out, t.name, day, stem)
            it = iters[t.name] = records(p, offsets=True) if os.path.exists(p) else iter(())
            counts[t.name] = 0
        got = next(it, _MISSING)
        n += 1
        counts[t.name] += 1
        if got is _MISSING:
            diffs.append({"table": t.name, "row": counts[t.name] - 1, "why": "missing"})
            break
        goff, got = got
        if goff != off or not same(rec, got):
            if len(diffs) < limit:
                keys = sorted(k for k in set(rec) | set(got)
                              if k not in rec or k not in got or not same(rec[k], got[k]))
                diffs.append({"table": t.name, "row": counts[t.name] - 1, "keys": keys,
                              "off": [off, goff]})
            else:
                diffs.append(None)
        elif not same_order(rec, got):
            order += 1
    left = {name: sum(1 for _ in it) for name, it in iters.items()}
    left = {k: v for k, v in left.items() if v}
    for name in TABLES:
        if name not in iters and os.path.exists(out_path(out, name, day, stem)):
            left[name] = sum(1 for _ in records(out_path(out, name, day, stem)))
    return {"day": day, "stem": stem, "records": n, "by_table": counts,
            "differ": len(diffs), "examples": [d for d in diffs if d][:limit],
            "order_only": order, "rows_left_over": left}


# ------------------------------------------------------------------ schema text
def _type_name(kind):
    if kind is LOCAL:
        return "timestamp[ms] (local, no zone)"
    if kind is JSON:
        return "string (JSON text)"
    return str(kind.arrow())


BOOKKEEPING = (("_off", "int64"), ("_absent", "list<string>"),
               ("_extra", "string (JSON object)"))


def schema_text(markdown=False):
    """Every table's columns and types: in full, or as Markdown with the envelope
    and the identity block given once (`schema --markdown`, docs/feed-parquet)."""
    if not markdown:
        lines = []
        for t in TABLES.values():
            lines.append("%s -- %s" % (t.name, t.doc))
            lines += ["  %-24s %s" % (k, _type_name(kind)) for k, kind in t.columns]
            lines += ["  %-24s %s" % kv for kv in BOOKKEEPING]
            lines.append("")
        return "\n".join(lines)

    def rows(spec):
        out = ["| column | type |", "|---|---|"]
        for s in spec:
            k, kind = s[0], s[1]
            opt = len(s) > 2 and s[2] in (True, OPT)
            if kind is NULL:
                out.append("| `%s` | always null: no column |" % k)
            else:
                out.append("| `%s` | `%s`%s |" % (k, _type_name(kind),
                                                  " (written only when set)" if opt else ""))
        return out

    env = set(k for k, *_ in ENVELOPE)
    ident = set(k for k, *_ in IDENTITY)
    lines = ["### Envelope: every table", ""] + rows(ENVELOPE) + [
        "", "### Identity: `exit`, `conn`, `netio`, `residency`, `submit`", ""] + rows(IDENTITY)
    for t in TABLES.values():
        has = set(k for spec in t.keys.values() for k, *_ in spec)
        base = "the envelope" + (" and the identity" if ident <= has else "")
        lines += ["", "### `%s`" % t.name, "", "%s. Columns: %s, then" % (
            t.doc[0].upper() + t.doc[1:], base), ""]
        seen = set(env | (ident if ident <= has else set()))
        own = []
        for ev, spec in t.keys.items():
            for s in spec:
                if s[0] not in seen:
                    seen.add(s[0])
                    own.append(s)
        if t.name == "exit":
            lines.append("(`rss_mb` is a `truncated` row's; `fork_only`, `cpu_pct`, `peak_rss_mb`, "
                         "`cmaj_flt`, `nvcsw`, `nivcsw`, `sched_*`, the delay waits, `pgrp` and "
                         "`sid` are an `exit` row's)")
            lines.append("")
        lines += rows(own) if own else ["(nothing else: every other key goes to `_extra`)"]
    lines += ["", "### Every table ends with", "", "| column | type |", "|---|---|"]
    lines += ["| `%s` | `%s` |" % kv for kv in BOOKKEEPING]
    return "\n".join(lines)


# ------------------------------------------------------------------ CLI
def _work(args):
    kind, day, path, out, opts = args
    if kind == "convert":
        return convert_source(day, path, out, **opts)
    return verify_source(day, path, out)


def _run(kind, jobs, todo, out, opts):
    """`(work, result)` per day-file as each finishes; `result` is the exception a
    day-file raised (a corrupt `.gz`, a full disk), and the others go on."""
    work = [(kind, d, p, out, opts) for d, p in todo]
    if jobs <= 1 or len(work) <= 1:
        for w in work:
            try:
                res = _work(w)
            except Exception as e:
                res = e
            yield w, res
        return
    import concurrent.futures as cf
    with cf.ProcessPoolExecutor(max_workers=jobs) as ex:
        futs = {ex.submit(_work, w): w for w in work}
        for f in cf.as_completed(futs):
            try:
                res = f.result()
            except Exception as e:
                res = e
            yield futs[f], res


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m rc_dashboard.feedparquet",
                                 description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("convert", "verify"):
        p = sub.add_parser(name)
        p.add_argument("roots", nargs="+", help="feed roots: directories of <YYYY-MM-DD>/")
        p.add_argument("--out", required=True, help="the Parquet dataset's directory")
        p.add_argument("--days", type=int, default=0, help="newest N days per root (0: all)")
        p.add_argument("--day", action="append", default=[], help="only this day (repeatable)")
        p.add_argument("--include-today", action="store_true",
                       help="also today's day-files, which are still being written")
        p.add_argument("--jobs", type=int, default=max(1, min(8, os.cpu_count() or 1)))
        if name == "convert":
            p.add_argument("--force", action="store_true",
                           help="convert again even when the sources are unchanged")
            p.add_argument("--zstd-level", type=int, default=ZSTD_LEVEL)
            p.add_argument("--row-group-rows", type=int, default=ROW_GROUP_ROWS)
    p = sub.add_parser("schema")
    p.add_argument("--markdown", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd == "schema":
        _pa()
        print(schema_text(a.markdown))
        return 0
    todo = sources(a.roots, a.days, a.include_today)
    if a.day:
        todo = [t for t in todo if t[0] in set(a.day)]
    if a.cmd == "convert":
        if not a.force:
            todo = [(d, p) for d, p in todo
                    if not current(read_manifest(a.out, d, stem_of(p)), p, a.out)]
        # the biggest first, so the longest conversion does not start last
        todo.sort(key=lambda t: -os.path.getsize(t[1]))
        opts = {"level": a.zstd_level, "row_group_rows": a.row_group_rows}
    else:
        opts = {}
    if not todo:
        print("nothing to %s" % a.cmd, file=sys.stderr)
        return 0
    bad = 0
    for (_k, day, path, _o, _opts), res in _run(a.cmd, a.jobs, todo, a.out, opts):
        name = os.path.basename(path)
        if isinstance(res, Exception):
            bad += 1
            print("%s %s: FAILED: %s: %s" % (day, name, type(res).__name__, res))
        elif a.cmd == "convert":
            print("%s %s: %s records in %.1f s%s%s" % (
                day, name, sum(res["records"].values()), res["seconds"],
                ", %d malformed" % res["malformed"] if res["malformed"] else "",
                ", _extra on %s" % res["extra_rows"] if res["extra_rows"] else ""))
        else:
            ok = not res["differ"] and not res["rows_left_over"]
            bad += not ok
            print("%s %s: %d records, %s%s" % (
                day, name, res["records"], "same" if ok else "DIFFER: %s" % json.dumps(
                    {k: res[k] for k in ("differ", "examples", "rows_left_over")}),
                ", %d in another key order" % res["order_only"] if res["order_only"] else ""))
        sys.stdout.flush()
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
