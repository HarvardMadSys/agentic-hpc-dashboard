"""A sealed day-file read from its Parquet tables, as the file itself.

The history store (`history.py`) keeps, for every sealed `<day>/<host>.<stream>.jsonl.gz`,
its records as `feedparquet` tables -- every row carrying `_off`, the offset of its line
in the uncompressed file -- and a manifest with the rest of what the file could be
asked: its size and complete end, its first 4 KB, the offset of every line that is not
a record. A `Replica` answers from those every question the readers ask of a day-file,
with the same offsets, so a position, a saved build or an hour's span means the same
thing whichever of the two a reader took it from:

    size, complete_end          the uncompressed file's size; the end of its last line
    head_digest(n)              `feedfile.head_digest` of its first n bytes
    is_boundary(off)            `feedfile.is_boundary`
    next_boundary(x), prev_boundary(x)
                                the first line boundary at or after `x`, the last at or
                                before it: where a read by chunks stops
    seek_to_epoch(since, size)  `feedfile.seek_to_epoch`: the same probes, the same answer
    end_at(t, size)             `feedfile.end_at_jsonl`, likewise
    records(start, end, ...)    `(off, nbytes, record)` for every record line in
                                [start, end), forward or newest first, each record what
                                `fastjson.loads` makes of its line; `kinds` and `where`
                                (`where.py`) leave out, before they are built, records a
                                reader would drop
    bad_in(start, end)          lines in the range that do not parse: a reader's
                                `parse_errors`

Which records a range holds, in which order, at which offsets, and each one key for
key and type for type: all the same as the file's (tests/test_replica.py checks them
against the JSONL, line by line and probe by probe).

A replica stands in for its file only while it describes it exactly: the manifest
names the file as it is on disk (`feedfile` identity: name, size, mtime, inode), and
the file holds no line a JSONL reader would read differently -- a last line with no
newline (still being written) or a line that is JSON but not an object (which the
JSONL readers count as a line that does not parse, and no table keeps). Such a file
is read as JSONL, as every file was before. Nor
does a replica stand in once a table its manifest names is gone, or is not the size
it was written at (`feedparquet.tables_whole`). A read that raises all the same --
a table damaged with its size intact, or gone since the replica was taken -- goes
on from the file itself, from where the replica left it, and the store is told
(`failed`): it lets the replica go, says so on the Feeds tab, and converts the file
again.
"""
import base64
import hashlib
import heapq
import json
import threading
from operator import itemgetter

from . import feedparquet as fp
from . import where as wh
from .normalize import epoch_of

BATCH_ROWS = 8192            # rows built into records at a time
_TIME_KEYS = ("ts_epoch", "ts", "timestamp")

_lookup = None               # path -> Replica or None, set by the history store
_failed = None               # (path, Replica, exception): a read of one raised


def install(fn, failed=None):
    """Name the function the readers ask for a sealed file's replica (`for_path`),
    and the one they tell when a read of one raises (`failed`); None turns
    replicas off."""
    global _lookup, _failed
    _lookup = fn
    _failed = failed


def failed(path, rep, exc):
    """A read of `path`'s replica `rep` raised `exc` -- a table gone or damaged
    since the replica was taken. The reader goes on from the file itself; this
    tells the store, which lets the replica go and says so
    (`history.HistoryStore.replica_failed`)."""
    fn = _failed
    if fn is not None:
        try:
            fn(path, rep, exc)
        except Exception:
            pass


def for_path(path):
    """The replica that stands in for `path` now, or None (read the file)."""
    fn = _lookup
    if fn is None or not path.endswith(".gz"):
        return None
    try:
        return fn(path)
    except Exception:
        return None


def exact(man):
    """Whether a manifest's file can be stood in for (see the module docstring)."""
    return man.get("partial") is None and \
        not any(k == "nondict" for _o, k in man.get("nonrec") or ())


def _np():
    import numpy
    return numpy


class Replica:
    def __init__(self, out, man):
        self.out = out
        self.man = man
        self.day = man["day"]
        self.stem = man["stem"]
        self.size = man["usize"]
        self.complete_end = man["complete_end"]
        self._head = base64.b64decode(man["head"])
        self.tables = [(fp.TABLES[t], fp.out_path(out, t, self.day, self.stem))
                       for t, n in sorted(man["records"].items()) if n]
        np = _np()
        nonrec = man.get("nonrec") or ()
        self._nonrec = np.array([o for o, _k in nonrec], dtype=np.int64)
        self._bad = np.array([o for o, k in nonrec if k != "blank"], dtype=np.int64)
        self._lock = threading.Lock()
        self._starts = None      # every line's start, ascending
        self._probe_ix = None    # (ts, has, next) by line, for the time searches
        self._offs = {}          # table name -> its rows' `_off`
        self._pf = {}            # table name -> ParquetFile
        self._rg = {}            # table name -> first row of each row group, and the end

    # ------------------------------------------------------------ what it is
    def head_digest(self, n):
        return hashlib.sha1(self._head[:n]).hexdigest()

    def is_boundary(self, off):
        if off == 0 or off == self.complete_end:
            return True
        starts = self.starts()
        i = int(starts.searchsorted(off))
        return i < len(starts) and int(starts[i]) == off

    def next_boundary(self, x):
        """The first line boundary at or after offset `x` (the file's start, a
        line's start, or `complete_end`): where a read that runs to the end of
        the line holding byte `x - 1` stops."""
        if x <= 0:
            return 0
        if x >= self.complete_end:
            return self.complete_end
        starts = self.starts()
        i = int(starts.searchsorted(x))
        return int(starts[i]) if i < len(starts) else self.complete_end

    def prev_boundary(self, x):
        """The last line boundary at or before offset `x`: 0 when no line ends
        before it."""
        if x >= self.complete_end:
            return self.complete_end
        if x <= 0:
            return 0
        starts = self.starts()
        i = int(starts.searchsorted(x, side="right")) - 1
        return int(starts[i]) if i >= 0 else 0

    def bad_in(self, start, end):
        b = self._bad
        return int(b.searchsorted(end) - b.searchsorted(start))

    def record_count(self, start, end):
        """How many record lines start in [start, end)."""
        s, nr = self.starts(), self._nonrec
        return int((s.searchsorted(end) - s.searchsorted(start))
                   - (nr.searchsorted(end) - nr.searchsorted(start)))

    # ------------------------------------------------------------ the parts
    def _file(self, name, path):
        pf = self._pf.get(name)
        if pf is None:
            pf = self._pf[name] = fp._pa().parquet.ParquetFile(path)
            md = pf.metadata
            firsts = [0]
            for i in range(md.num_row_groups):
                firsts.append(firsts[-1] + md.row_group(i).num_rows)
            self._rg[name] = firsts
        return pf

    def offs(self, name, path):
        """The `_off` of a table's rows, in file order (ascending)."""
        with self._lock:
            got = self._offs.get(name)
            if got is None:
                pf = self._file(name, path)
                got = self._offs[name] = pf.read(columns=["_off"]).column("_off") \
                    .to_numpy()
            return got

    def starts(self):
        """Every line's start: each record's `_off` and each other line's."""
        with self._lock:
            got = self._starts
        if got is None:
            np = _np()
            got = np.sort(np.concatenate([self.offs(t.name, p) for t, p in self.tables]
                                         + [self._nonrec]), kind="stable")
            with self._lock:
                self._starts = got
        return got

    def _index(self):
        """(starts, ts, has, nxt) over every line: a record line's time as
        `epoch_of` reads it (`has` false when it reads none, as for a line that is
        not a record), and for each line the first line at or after it with a time."""
        with self._lock:
            got = self._probe_ix
        if got is not None:
            return got
        np = _np()
        offs, tss, hass = [], [], []
        for t, p in self.tables:
            o, ts, has = self._times(t, p)
            offs.append(o)
            tss.append(ts)
            hass.append(has)
        offs.append(self._nonrec)
        tss.append(np.zeros(len(self._nonrec)))
        hass.append(np.zeros(len(self._nonrec), dtype=bool))
        starts = np.concatenate(offs)
        order = np.argsort(starts, kind="stable")
        starts = starts[order]
        ts = np.concatenate(tss)[order]
        has = np.concatenate(hass)[order]
        n = len(starts)
        nxt = np.where(has, np.arange(n, dtype=np.int32 if n < 2 ** 31 else np.int64), n)
        if n:
            nxt = np.minimum.accumulate(nxt[::-1])[::-1]
        with self._lock:
            if self._starts is None:
                self._starts = starts
            else:
                starts = self._starts         # the same values: hold them once
            got = self._probe_ix = (starts, ts, has, nxt)
        return got

    def _times(self, table, path):
        """`(off, ts, has)` arrays of a table's rows: `epoch_of` of each record."""
        np = _np()
        pa = fp._pa()
        pf = self._file(table.name, path)
        names = set(pf.schema_arrow.names)
        # a row's `ts_epoch` is its time unless `_extra` holds its time keys: the
        # rest of what `epoch_of` reads is read only for the rows that need it
        tab = pf.read(columns=["_off", "_extra"] + (["ts_epoch"] if "ts_epoch" in names
                                                    else []))
        off = tab.column("_off").to_numpy()
        n = tab.num_rows
        ts = np.zeros(n)
        has = np.zeros(n, dtype=bool)
        extra = tab.column("_extra")
        if "ts_epoch" in names:
            te = tab.column("ts_epoch")
            fast = pa.compute.and_(te.is_valid(), extra.is_null())
            fast_np = fast.to_numpy(zero_copy_only=False)
            ts[fast_np] = te.filter(fast).to_numpy()
            has[fast_np] = True
            slow = np.nonzero(~fast_np)[0]
        else:
            slow = np.arange(n)
        if len(slow):
            cols = ["_extra", "_absent"] + [k for k in ("ts_epoch", "ts") if k in names]
            sub = pf.read(columns=cols).take(pa.array(slow))
            rows = _Columns(table, sub, [k for k in _TIME_KEYS if k in names])
            for k, i in enumerate(slow):
                e = epoch_of(rows.record(k, _TIME_KEYS))
                if e is not None:
                    ts[i] = e
                    has[i] = True
        return off, ts, has

    def nbytes(self):
        """The memory this replica's indexes hold, which the history store bounds:
        about 30 bytes a line once a time search and a ranged read have been made."""
        with self._lock:
            arrays = [self._starts, self._nonrec, self._bad] + list(self._offs.values())
            arrays += list(self._probe_ix or ())
        seen = set()
        n = 0
        for a in arrays:
            if a is not None and id(a) not in seen:
                seen.add(id(a))
                n += a.nbytes
        return n

    # ------------------------------------------------------------ time searches
    def _probe(self, pos, limit):
        starts, ts, _has, nxt = self._index()
        n = len(starts)
        i = 0 if pos == 0 else int(starts.searchsorted(pos, side="right"))
        off = int(starts[i]) if i < n else self.size
        j = int(nxt[i]) if i < n else n
        if j < n and j - i < limit:
            return off, float(ts[j])
        return off, None

    def seek_to_epoch(self, since, size=None, probe_lines=64):
        """`feedfile.seek_to_epoch(fh, since, size)` of the file."""
        size = self.size if size is None else size
        lo, hi, best = 0, size, size
        while lo < hi:
            mid = (lo + hi) // 2
            off, ts = self._probe(mid, probe_lines)
            if ts is None:
                hi = mid
            elif ts >= since:
                best = min(best, off)
                hi = mid
            else:
                lo = max(off, mid) + 1
        return best

    def end_at(self, t, size=None, probe_lines=64):
        """`feedfile.end_at_jsonl(fh, t, size)` of the file."""
        size = self.size if size is None else size
        lo, hi, best = 0, size, size
        while lo < hi:
            mid = (lo + hi) // 2
            off, ts = self._probe(mid, probe_lines)
            if ts is not None and ts > t:
                best = min(best, off)
                hi = mid
            else:
                lo = max(off, mid) + 1
        return best

    # ------------------------------------------------------------ records
    def records(self, start=0, end=None, reverse=False, kinds=None, where=None):
        """`(off, nbytes, record)` for every record line starting in [start, end),
        oldest first or (`reverse`) newest first. `kinds`: only these events;
        `where`: only records it passes (`where.match`)."""
        end = self.complete_end if end is None else min(end, self.complete_end)
        if start >= end:
            return iter(())
        kinds = None if kinds is None else frozenset(kinds)
        streams = []
        for table, path in self.tables:
            evs = [e for e in table.events if e is not None]
            if kinds is not None and evs and not kinds.intersection(evs):
                continue
            streams.append(self._rows(table, path, start, end, reverse, kinds, where))
        if not streams:
            return iter(())
        if len(streams) == 1:
            return streams[0]
        return heapq.merge(*streams, key=itemgetter(0), reverse=reverse)

    def _rows(self, table, path, start, end, reverse, kinds, where):
        np = _np()
        pa = fp._pa()
        offs = self.offs(table.name, path)
        a = int(offs.searchsorted(start))
        b = int(offs.searchsorted(end))
        if a >= b:
            return
        pf = self._file(table.name, path)
        firsts = self._rg[table.name]
        starts = self.starts()
        groups = [g for g in range(len(firsts) - 1) if firsts[g] < b and firsts[g + 1] > a]
        evs = [e for e in table.events if e is not None]
        need_kind = kinds is not None and not (evs and kinds.issuperset(evs))
        filtering = need_kind or where is not None
        wfields = wh.fields(where)
        fcols = None
        if filtering:
            have = set(pf.schema_arrow.names)
            fcols = [c for c in dict.fromkeys(
                (["event"] if need_kind else []) + wfields + ["_absent", "_extra"]) if c in have]
        for g in (reversed(groups) if reverse else groups):
            lo, hi = max(a, firsts[g]) - firsts[g], min(b, firsts[g + 1]) - firsts[g]
            mask = None
            if filtering:
                # decided on the columns the filter names, before a row is decoded whole
                ft = pf.read_row_group(g, columns=fcols).slice(lo, hi - lo)
                mask = _keep(table, ft.combine_chunks().to_batches()[0] if ft.num_rows else None,
                             kinds if need_kind else None, where, wfields, ft.num_rows)
                if not any(mask):
                    continue
            batches, at = [], 0
            for rb in pf.iter_batches(batch_size=BATCH_ROWS, row_groups=[g]):
                n = rb.num_rows
                s, e = max(lo - at, 0), min(hi - at, n)
                if s < e:
                    part = rb.slice(s, e - s)
                    if mask is not None:
                        m = mask[at + s - lo:at + e - lo]
                        if not any(m):
                            at += n
                            if at >= hi:
                                break
                            continue
                        part = part.filter(pa.array(m))
                    batches.append(part)
                at += n
                if at >= hi:
                    break
            for rb in (reversed(batches) if reverse else batches):
                o = rb.column("_off").to_numpy()
                nxt = starts.searchsorted(o, side="right")
                last = nxt >= len(starts)
                nbytes = (np.where(last, self.complete_end,
                                   starts[np.minimum(nxt, len(starts) - 1)]) - o).tolist()
                o = o.tolist()
                recs = _Columns(table, rb).records()
                rng = range(rb.num_rows - 1, -1, -1) if reverse else range(rb.num_rows)
                for k in rng:
                    yield o[k], nbytes[k], recs[k]


def _keep(table, rb, kinds, where, wfields, n):
    """Which of `n` rows (`rb`: their filter columns) a reader would keep by
    `kinds` and `where`, as a list of bools. A row whose `_absent` names one of
    the keys tested, or whose `_extra` mentions one, is decided on its record's
    keys one by one; the others a column at a time: a set test on a string or an
    integer column is `is_in`, a `Prefix` on a string one `starts_with`, an
    `AfterColon` one `is_in` past the first colon, anything else the test on each
    value."""
    if n == 0 or rb is None:
        return []
    pa = fp._pa()
    pc = pa.compute
    names = (["event"] if kinds is not None else []) + list(wfields)
    odd = None
    ab = rb.column("_absent")
    if pc.any(ab.is_valid()).as_py():
        flat = pc.list_flatten(ab)
        hit = pc.is_in(flat, value_set=pa.array(names, pa.string()))
        rows = set(pc.filter(pc.list_parent_indices(ab), hit).to_pylist())
        if rows:
            odd = [k in rows for k in range(n)]
    ex = rb.column("_extra")
    if pc.any(ex.is_valid()).as_py():
        m = None
        for f in names:
            h = pc.fill_null(pc.match_substring(ex, pattern=json.dumps(f)), False)
            m = h if m is None else pc.or_(m, h)
        if m is not None and pc.any(m).as_py():
            m = m.to_pylist()
            odd = m if odd is None else [x or y for x, y in zip(odd, m)]
    cols = {f: (rb.column(f) if f in rb.schema.names else None) for f in set(names)}
    kinds_l = sorted(kinds) if kinds is not None else None
    # the clean rows, a column at a time
    if kinds is not None:
        c = cols["event"]
        keep = (pc.is_in(c, value_set=pa.array(kinds_l, pa.string())).fill_null(False)
                if c is not None else pa.array([False] * n))
    else:
        keep = None
    if where is not None:
        anyb = None
        for br in wh.branches(where):
            allt = None
            for f, t in br.items():
                m = _test_column(table, cols.get(f), f, t, n)
                allt = m if allt is None else pc.and_(allt, m)
            if allt is None:
                allt = pa.array([True] * n)
            anyb = allt if anyb is None else pc.or_(anyb, allt)
        keep = anyb if keep is None else pc.and_(keep, anyb)
    out = keep.to_pylist()
    if odd is not None:
        sub = None
        for k in range(n):
            if odd[k]:
                if sub is None:
                    sub = _Columns(table, rb, names)
                rec = sub.record(k, names)
                out[k] = (kinds is None or _in(rec.get("event"), kinds)) and \
                    (where is None or wh.match(where, rec))
    return out


def _test_column(table, col, f, t, n):
    """One `where` test over a column, as a BooleanArray (nulls as False)."""
    pa = fp._pa()
    pc = pa.compute
    if col is None:                                  # no such column: every value None
        return pa.array([wh.test(t, None)] * n)
    kind = dict(table.columns).get(f)
    if kind is fp.STR and isinstance(t, wh.AfterColon):
        # the part after the first colon, as `str.split(":", 1)[-1]` gives it
        tail = pc.replace_substring_regex(col, pattern="^[^:]*:", replacement="",
                                          max_replacements=1)
        return pc.fill_null(pc.is_in(tail, value_set=pa.array(sorted(t), pa.string())),
                            False)
    if kind in (fp.I32, fp.I64) and isinstance(t, (set, frozenset)):
        # `str(v)` of an int is its plain decimal, so only those members can match
        ints = [int(s) for s in t if isinstance(s, str) and _decimal(s)]
        ints = [i for i in ints if -2 ** 63 <= i < 2 ** 63]
        hit = pc.is_in(col.cast(pa.int64()), value_set=pa.array(ints, pa.int64()))
        return pc.if_else(col.is_null(), pa.scalar("None" in t), hit.fill_null(False))
    if kind is fp.STR and isinstance(t, (set, frozenset)):
        hit = pc.is_in(col, value_set=pa.array(sorted(map(str, t)), pa.string()))
        # str(None) is "None": a null passes when the set holds that word
        return pc.if_else(col.is_null(), pa.scalar("None" in t), hit)
    if kind is fp.STR and isinstance(t, wh.Prefix):
        return pc.fill_null(pc.starts_with(col, pattern=str.__str__(t)), False)
    if kind is fp.LOCAL:
        col = pc.strftime(col.cast(pa.timestamp("s")), format=fp.TS_FORMAT)
    vals = col.to_pylist()
    if kind is not None and _needs_back(kind):
        vals = [None if v is None else kind.back(v) for v in vals]
    return pa.array([wh.test(t, v) for v in vals], pa.bool_())


def _decimal(s):
    """Whether `s` is what `str` makes of some int: no sign but a leading minus,
    no leading zero, no spaces or underscores."""
    try:
        return str(int(s)) == s
    except ValueError:
        return False


def _in(v, kinds):
    try:
        return v in kinds
    except TypeError:               # an unhashable event is in no set of names
        return False


def _needs_back(kind):
    """Whether a column's values, as Arrow turns them into Python, must be turned
    back further to be what the line held: a map (Arrow gives pairs), JSON text,
    a struct that may lack a key (Arrow gives every field) -- or anything holding
    one. A struct of plain values, or a list of those, already is the line's."""
    if kind is fp.JSON or isinstance(kind, fp.Map):
        return True
    if isinstance(kind, fp.Struct):
        return bool(kind.optional) or any(_needs_back(k) for _n, k in kind.fields)
    if isinstance(kind, fp.List):
        return _needs_back(kind.item)
    return False


class _Columns:
    """A record batch's columns as Python values, read back as `feedparquet.rebuild`
    reads a row, but a column at a time: timestamps formatted, nested blocks and
    JSON text turned back into what the line held, `_absent` and `_extra` applied."""

    def __init__(self, table, rb, only=None):
        pa = fp._pa()
        pc = pa.compute
        self.table = table
        self.n = rb.num_rows
        names = set(rb.schema.names)
        self.vals = {}
        self.optional_set = None           # rows where a key written only when set is
        for k, kind in table.columns:
            if only is not None and k not in only:
                continue
            if k not in names:
                continue
            col = rb.column(k)
            if kind is fp.LOCAL:
                col = pc.strftime(col.cast(pa.timestamp("s")), format=fp.TS_FORMAT)
            v = col.to_pylist()
            if _needs_back(kind):
                back = kind.back
                v = [None if x is None else back(x) for x in v]
            self.vals[k] = v
        opt = {k for spec in table.keys.values() for k, _kind, o in spec if o}
        for k in opt:
            if k in self.vals and k in names:
                valid = rb.column(k).is_valid()
                self.optional_set = valid if self.optional_set is None else \
                    pc.or_(self.optional_set, valid)
        if self.optional_set is not None:
            self.optional_set = self.optional_set.to_pylist()
        self.absent = rb.column("_absent").to_pylist()
        self.extra = rb.column("_extra").to_pylist()
        self.events = self.vals.get("event")
        if self.events is None and "event" in names:
            self.events = rb.column("event").to_pylist()
        self._plans = {}

    def records(self):
        """Every row as its record, a list. Rows with the same layout -- event, the
        keys it lacks, nothing in `_extra`, no key written only when set -- are
        built by zipping their values with the layout's keys; the rest one by one."""
        out = [None] * self.n
        groups = {}
        events, absent, extra, opt = self.events, self.absent, self.extra, self.optional_set
        for k in range(self.n):
            if extra[k] is not None or (opt is not None and opt[k]):
                out[k] = self.record(k)
                continue
            ev = events[k] if events is not None else None
            a = absent[k]
            key = (ev, tuple(a) if a else None)
            g = groups.get(key)
            if g is None:
                groups[key] = [k]
            else:
                g.append(k)
        none = None
        for (ev, a), rows in groups.items():
            keys, cols = [], []
            for key, col, o in self._plan(ev if type(ev) is str else None):
                if (a and key in a) or o:
                    continue
                keys.append(key)
                cols.append(col)
            if any(c is None for c in cols):
                if none is None:
                    none = [None] * self.n
                cols = [none if c is None else c for c in cols]
            if len(rows) == self.n:
                for k, row in enumerate(zip(*cols)):
                    out[k] = dict(zip(keys, row))
            else:
                for k in rows:
                    out[k] = dict(zip(keys, [c[k] for c in cols]))
        return out

    def _plan(self, ev):
        plan = self._plans.get(ev)
        if plan is None:
            spec = self.table.keys.get(ev)
            if spec is None:
                spec = self.table.keys.get(None) or next(iter(self.table.keys.values()))
            plan = self._plans[ev] = tuple(
                (k, self.vals.get(k) if kind is not fp.NULL else None, opt)
                for k, kind, opt in spec)
        return plan

    def record(self, k, only=None):
        """Row `k` as its record (only the keys in `only`, if given)."""
        extra = self.extra[k]
        extra = json.loads(extra) if extra else None
        absent = self.absent[k]
        ev = self.events[k] if self.events is not None else None
        if extra is not None and "event" in extra:
            ev = extra["event"]
        out = {}
        for key, col, opt in self._plan(ev if type(ev) is str else None):
            if only is not None and key not in only:
                continue
            if extra is not None and key in extra:
                out[key] = extra.pop(key)
                continue
            if absent and key in absent:
                continue
            v = None if col is None else col[k]
            if v is None:
                if not opt:
                    out[key] = None
            else:
                out[key] = v
        if extra:
            if only is None:
                out.update(extra)
            else:
                out.update((key, v) for key, v in extra.items() if key in only)
        return out
