#!/usr/bin/env python3
"""fleet_stream: carry each node's ebpfm capture to one host as it is written.

The collector writes `<root>/<YYYY-MM-DD>/<host>.<stream>.jsonl` on each node's own
disk (root is /scratch/ebpfm, the packaged collector's output folder) and gzips a day
once it is sealed. This tool brings every node's files to the host that runs the
dashboard, over one ssh connection per node, so each byte crosses the network once. It
copies bytes, not records: a copy that ends in a partial line is read as one still
being written, as its source would be.

  send     runs on a source node, started by a forced-command ssh key. It reads
           `--root` (default /scratch/ebpfm) and writes frames to stdout: the bytes
           the collector appends (zlib), sealed days the receiver lacks as the
           collector's own `.gz`, and each sealed day's gzip trailer for checking.
  receive  runs on the dashboard's host (`--config`, a JSON file; see Receiver). It
           keeps one `send` running per source host and writes the copy the
           dashboard reads: `<base>/mirror/<host>/<day>/`.
  status   prints a receiver's state from its base folder.

It uses the Python 3.6 standard library only, so the nodes' `/usr/bin/python3` runs it
with nothing installed.

Wire format. Every frame is a 4-byte big-endian header length, a compact JSON header,
and `header["len"]` payload bytes (`write_frame`, `read_frame`). The receiver's hello
is one JSON line on the sender's stdin:

    {"v": 1, "days": N, "have": {"<day>/<file>": {"size": int, "verified": bool}}}

(a verified day the copy keeps as `.gz` is listed by its plain name as
`{"verified": true}`, with no size), and the sender answers with frames, by `t`:

    hello    {v, host, live}         always first
    data     {path, off, plain}      zlib chunk of plain bytes [off, off+plain)
    gz       {path, off, size}       raw bytes of the source `.gz`
    sealed   {path, size}            the plain file is final at `size`
    trailer  {path, crc, isize}      the source `.gz`'s last 8 bytes, once gzip is done
    reset    {path, why}             drop the copy of `path`; it is sent again from 0
    ping     {}                      after `--ping` seconds of silence, catch-up included
    error    {msg}                   the sender exits 1 right after

Only `.jsonl` day-files (and their `.gz`) are sent; any other file in a day folder stays
on the node, and a hello's other keys are ignored. The live day is the newest day folder
dated no later than the node's own tomorrow; of the sealed days, those among the `days`
newest that are within `days` days of the live one are sent. Under `--root` the sender
follows no link and reads regular files only, it takes the hello's counts as no less
than 0 (and `head_n` as no more than HEAD), and a hello longer than MAX_HELLO is an
error.

The receiver refuses a frame no sender writes, and reconnects: a data frame of more
than CHUNK plain bytes, or one that ends the zlib stream; a day that is not a real
date, or is after the receiver's tomorrow; and a `data`, `gz`, or `reset` frame for a
file the copy holds verified.
"""
import argparse
import collections
import datetime
import errno
import json
import os
import re
import select
import signal
import socket
import stat
import struct
import subprocess
import sys
import threading
import time
import zlib

PROTOCOL = 1
CHUNK = 4 << 20                # most plain (data) or raw (gz) bytes in one frame
HEAD = 64 << 10                # bytes of a file's start the hello fingerprints
EXIT_UNSAFE = 3                # the receiver's base folder is not what it was
MAX_HEADER = 1 << 20
MAX_HELLO = 4 << 20            # the receiver's hello line; a real one is a few KB
MAX_PAYLOAD = CHUNK + (1 << 16)    # deflate can grow incompressible input a little
STDERR_LINE = 4096             # a transport's stderr line is kept up to this; the rest goes
STALL_S = 300.0                # a receiver's read with nothing from the sender this long ends it
DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\Z")
GZ = ".gz"
GZ_LEVEL = 6                   # gzip's default, as the collector's `nice gzip -f` uses


class ProtocolError(Exception):
    pass


class Stalled(ProtocolError):
    """The transport is open, but nothing has come from the sender, not even a ping."""


# --------------------------------------------------------------------- frames

def write_frame(out, header, payload=b""):
    """Write one frame and return the bytes written. Sets `header["len"]`."""
    header["len"] = len(payload)
    h = json.dumps(header, separators=(",", ":"), sort_keys=True).encode("utf-8")
    buf = struct.pack(">I", len(h)) + h + payload
    out.write(buf)
    return len(buf)


def _read_exact(inp, n):
    parts, got = [], 0
    while got < n:
        b = inp.read(n - got)
        if not b:
            break
        parts.append(b)
        got += len(b)
    return b"".join(parts)


def read_frame(inp):
    """(header, payload), or None at a clean end of stream."""
    head = _read_exact(inp, 4)
    if not head:
        return None
    if len(head) < 4:
        raise ProtocolError("the stream ended inside a frame's length")
    (hl,) = struct.unpack(">I", head)
    if hl > MAX_HEADER:
        raise ProtocolError("a header of %d bytes" % hl)
    raw = _read_exact(inp, hl)
    if len(raw) < hl:
        raise ProtocolError("the stream ended inside a header")
    try:
        header = json.loads(raw.decode("utf-8"))
    except ValueError:
        raise ProtocolError("a header that is not JSON")
    if not isinstance(header, dict):
        raise ProtocolError("a header that is not an object")
    n = header.get("len", 0)
    if not isinstance(n, int) or n < 0 or n > MAX_PAYLOAD:
        raise ProtocolError("a payload of %r bytes" % (n,))
    payload = _read_exact(inp, n)
    if len(payload) < n:
        raise ProtocolError("the stream ended inside a payload")
    return header, payload


# ---------------------------------------------------------------- day folders

def list_days(root):
    """The `YYYY-MM-DD` folders under `root`, oldest first: real folders, not links to
    one. Raises OSError."""
    with os.scandir(root) as it:
        return sorted(e.name for e in it
                      if DAY_RE.match(e.name) and e.is_dir(follow_symlinks=False))


# The sender reaches each day folder under --root through a handle opened without
# following a link, and each file in it the same way, as the receiver's SafeBase does:
# a link put there points at what the sending account can read, not at the capture.
# --root itself may be a link; nothing under it is followed.

def open_day(root, day):
    """A handle on the day folder `root/day`, or None when it is gone, is not a folder,
    or is a link to one. Any other OSError (a folder the sending account may not read)
    is raised: the sender stops with an error the receiver shows, instead of sending
    pings and none of that day."""
    try:
        return os.open(os.path.join(root, day), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as e:
        if e.errno in (errno.ENOENT, errno.ENOTDIR, errno.ELOOP):
            return None
        raise


def open_file(dfd, name):
    """`name` in the folder `dfd`, open to read, or None when it is not there or is not a
    regular file. A link is not followed; a FIFO is opened without waiting for a writer,
    then left. Any other OSError (a file it may not read) is raised."""
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dfd)
    except OSError as e:
        if e.errno in (errno.ENOENT, errno.ELOOP, errno.ENXIO):
            return None
        raise
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            return None
        os.set_blocking(fd, True)
        return os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise


def entry(dfd, name):
    """`name`'s own stat in the folder `dfd` (a link's, not its target's), or None."""
    try:
        return os.stat(name, dir_fd=dfd, follow_symlinks=False)
    except FileNotFoundError:
        return None


def have_count(have, field, most=None):
    """A count the receiver's hello gives for a file, as an int no less than 0 (and no
    more than `most`): a head_n of -1 read a whole file, and one of 10**12 ran out of
    memory."""
    n = max(0, int((have or {}).get(field, 0)))
    return n if most is None else min(n, most)


def day_date(day):
    """The date a `YYYY-MM-DD` name stands for, or None when it names no real date."""
    if not DAY_RE.match(day):
        return None
    try:
        return datetime.date(int(day[:4]), int(day[5:7]), int(day[8:10]))
    except ValueError:
        return None


def tomorrow(today=None):
    """The day after `today` (this host's date), as `YYYY-MM-DD`: the newest day a
    node may name. A node's clock or time zone can put its day one ahead, no more."""
    return ((today or datetime.date.today()) + datetime.timedelta(days=1)).isoformat()


def stream_names(folder):
    """The plain name (`x.jsonl`) of every stream in a day folder (a path or a handle),
    plain or gzipped."""
    names = set()
    for n in os.listdir(folder):
        if n.endswith(".jsonl"):
            names.add(n)
        elif n.endswith(".jsonl" + GZ):
            names.add(n[:-len(GZ)])
    return sorted(names)


def gz_trailer(fh):
    """(crc32, size mod 2**32) of the uncompressed data, from an open gzip file's last
    8 bytes."""
    fh.seek(-8, os.SEEK_END)
    return struct.unpack("<II", fh.read(8))


def inflate_pieces(fh):
    """The plain bytes of an open gzip file, in pieces of at most CHUNK, across
    members. zlib checks each member's CRC32 and size; a file that ends inside a
    member raises ProtocolError."""
    d = zlib.decompressobj(16 + zlib.MAX_WBITS)
    started, buf, eof = False, b"", False
    while True:
        if not buf and not eof:
            buf = fh.read(1 << 20)
            eof = not buf
        if not buf and eof:
            if started and not d.eof:
                raise ProtocolError("the .gz ends inside a member")
            return
        out = d.decompress(buf, CHUNK)
        started = True
        if d.eof:
            buf = d.unused_data
            d = zlib.decompressobj(16 + zlib.MAX_WBITS)
            started = False
        else:
            buf = d.unconsumed_tail
        if out:
            yield out


# --------------------------------------------------------------------- sender

class Sender:
    """One connection's worth of sending. See the module docstring for the frames."""

    def __init__(self, root, inp, out, poll_s=1.0, ping_s=30.0, trailer_poll_s=30.0):
        self.root, self.out = root, out
        self.in_fd = inp.fileno()
        self.poll_s, self.ping_s, self.trailer_poll_s = poll_s, ping_s, trailer_poll_s
        self.z = zlib.compressobj(6)
        self.have = {}
        self.live = None
        self.files = {}        # key -> {"fh", "ino", "off", "path"}: the live day's open files
        self.awaiting = {}     # key -> its `.gz` path, until gzip is done and the trailer sent
        self.done = set()      # keys sealed during this connection
        self.next_trailer = 0.0
        self.last_out = time.time()

    # ---- output
    def emit(self, header, payload=b""):
        write_frame(self.out, header, payload)
        self.out.flush()
        self.last_out = time.time()

    def ping_if_quiet(self):
        """A ping once `ping_s` passed with nothing sent, so the receiver can tell a
        sender with nothing new, or busy in a long catch-up, from one that stopped."""
        if time.time() - self.last_out >= self.ping_s:
            self.emit({"t": "ping"})

    def reset(self, key, why):
        self.emit({"t": "reset", "path": key, "why": why})
        self.have.pop(key, None)           # the receiver no longer holds it

    def head_differs(self, key, fh):
        """True when the receiver's fingerprint of `key` (its first `head_n` bytes)
        does not match the start of the open source file: replaced while the link
        was down, so resuming at the copy's size would splice two files."""
        have = self.have.get(key) or {}
        if "head" not in have:
            return False
        n = have_count(have, "head_n", HEAD)
        fh.seek(0)
        buf = fh.read(n)
        return len(buf) < n or (zlib.crc32(buf) & 0xffffffff) != have["head"]

    def announce(self, key):
        """A file the receiver lacks gets a zero-length data frame, so its copy exists
        too: nothing else would ever name an empty one. The live day's files are named
        so before any of their bytes cross, so they appear in the copy together, as
        the collector makes them."""
        if key not in self.have:
            payload = self.z.compress(b"") + self.z.flush(zlib.Z_SYNC_FLUSH)
            self.emit({"t": "data", "path": key, "off": 0, "plain": 0}, payload)
            self.have[key] = {"size": 0, "verified": False}

    def send_plain(self, key, fh, off, end):
        """Send [off, end) of an open plain file; returns the offset reached."""
        while off < end:
            fh.seek(off)
            data = fh.read(min(CHUNK, end - off))
            if not data:
                break
            payload = self.z.compress(data) + self.z.flush(zlib.Z_SYNC_FLUSH)
            self.emit({"t": "data", "path": key, "off": off, "plain": len(data)}, payload)
            off += len(data)
        return off

    def send_inflated(self, key, fh, skip):
        """Send an open `.gz` file's plain bytes from `skip` on; returns the plain size."""
        have = self.have.get(key) or {}
        head_n = have_count(have, "head_n", HEAD) if "head" in have else 0
        head_crc, checked = 0, not head_n
        pos = 0
        fh.seek(0)
        for piece in inflate_pieces(fh):
            start, pos = pos, pos + len(piece)
            if not checked:                # head_n <= skip, so this ends before any send
                head_crc = zlib.crc32(piece[:max(0, head_n - start)], head_crc)
                if pos >= head_n:
                    checked = True
                    if (head_crc & 0xffffffff) != have["head"]:
                        self.reset(key, "the file was replaced while the link was down")
                        return self.send_inflated(key, fh, 0)
            if pos <= skip:
                self.ping_if_quiet()       # minutes, for a big day the copy mostly has
                continue
            if start < skip:
                piece, start = piece[skip - start:], skip
            payload = self.z.compress(piece) + self.z.flush(zlib.Z_SYNC_FLUSH)
            self.emit({"t": "data", "path": key, "off": start, "plain": len(piece)}, payload)
        if pos < skip:                     # the copy is longer than the source
            self.reset(key, "the source is shorter than the copy")
            return self.send_inflated(key, fh, 0)
        return pos

    def send_gz(self, gz_key, fh, off):
        size = os.fstat(fh.fileno()).st_size
        if off > size:
            self.reset(gz_key, "the source is shorter than the staged copy")
            off = 0
        while True:                        # at least one frame: a whole staged part
            fh.seek(off)                   # still needs one to be inflated
            raw = fh.read(min(CHUNK, size - off))
            self.emit({"t": "gz", "path": gz_key, "off": off, "size": size}, raw)
            off += len(raw)
            if off >= size or not raw:
                break

    def send_trailer(self, key, fh):
        crc, isize = gz_trailer(fh)
        self.emit({"t": "trailer", "path": key, "crc": crc, "isize": isize})

    # ---- sealed days
    def seal_plain(self, key, fh, off):
        """A sealed day's plain file still exists: send the rest, seal, await gzip."""
        size = os.fstat(fh.fileno()).st_size
        if size < off:
            self.reset(key, "the source is shorter than the copy")
            off = 0
        elif off and self.head_differs(key, fh):
            self.reset(key, "the file was replaced while the link was down")
            off = 0
        if size == 0:
            self.announce(key)
        self.send_plain(key, fh, off, size)
        self.emit({"t": "sealed", "path": key, "size": size})
        self.awaiting[key] = tuple(key.split("/", 1))
        self.done.add(key)

    def catch_up_sealed(self, day):
        dfd = open_day(self.root, day)
        if dfd is None:
            return                         # gone, or not a real folder
        try:
            for name in stream_names(dfd):
                key = "%s/%s" % (day, name)
                if key in self.done:
                    continue
                have = self.have.get(key)
                if have and have.get("verified"):
                    continue
                fh = open_file(dfd, name)  # None once gzip is done with it
                if fh is not None:
                    with fh:
                        self.seal_plain(key, fh, have_count(have, "size"))
                    continue
                fh = open_file(dfd, name + GZ)
                if fh is None:
                    continue
                with fh:
                    if have:
                        size = self.send_inflated(key, fh, have_count(have, "size"))
                        self.emit({"t": "sealed", "path": key, "size": size})
                    else:
                        self.send_gz(key + GZ, fh, have_count(self.have.get(key + GZ), "size"))
                    self.send_trailer(key, fh)
                self.done.add(key)
        finally:
            os.close(dfd)

    # ---- the live day
    def scan_live(self):
        """Open the live day's files not yet open, at the receiver's size."""
        dfd = open_day(self.root, self.live)
        if dfd is None:
            return
        try:
            names = sorted(n for n in os.listdir(dfd) if n.endswith(".jsonl"))
            for name in names:
                key = "%s/%s" % (self.live, name)
                have = self.have.get(key)
                if key in self.files or (have and have.get("verified")):
                    continue               # the receiver refuses to change a verified file
                fh = open_file(dfd, name)
                if fh is None:
                    continue
                st = os.fstat(fh.fileno())
                off = have_count(have, "size")
                if st.st_size < off:
                    self.reset(key, "the source is shorter than the copy")
                    off = 0
                elif off and self.head_differs(key, fh):
                    self.reset(key, "the file was replaced while the link was down")
                    off = 0
                self.announce(key)         # before follow_live sends any file's bytes
                self.files[key] = {"fh": fh, "ino": st.st_ino, "off": off, "name": name}
        finally:
            os.close(dfd)

    def follow_live(self):
        dfd = open_day(self.root, self.live)   # None once gone: the handles still read
        try:
            for key in sorted(self.files):
                f = self.files[key]
                # None once gzipped away: the handle still reads it
                by_name = entry(dfd, f["name"]) if dfd is not None else None
                st = os.fstat(f["fh"].fileno())
                if by_name is not None and by_name.st_ino != f["ino"]:
                    # Replaced. A link or a FIFO in its place is not opened, and the
                    # handle reads on: the collector may still be writing through its own.
                    fh = open_file(dfd, f["name"])
                    if fh is not None:
                        self.reset(key, "the file was replaced")
                        f["fh"].close()
                        f["fh"] = fh
                        st = os.fstat(fh.fileno())
                        f["ino"], f["off"] = st.st_ino, 0
                elif st.st_size < f["off"]:
                    self.reset(key, "the file shrank")
                    f["off"] = 0
                if st.st_size == 0 and f["off"] == 0:
                    self.announce(key)
                if st.st_size > f["off"]:
                    f["off"] = self.send_plain(key, f["fh"], f["off"], st.st_size)
        finally:
            if dfd is not None:
                os.close(dfd)

    def check_rollover(self, days):
        """A newer day folder means the live day is sealed: drain it, seal it, move on."""
        if not days or days[-1] <= self.live:
            return
        old, new = self.live, days[-1]
        self.follow_live()
        for key in sorted(self.files):
            f = self.files[key]
            size = os.fstat(f["fh"].fileno()).st_size
            if size > f["off"]:
                f["off"] = self.send_plain(key, f["fh"], f["off"], size)
            f["fh"].close()
            self.emit({"t": "sealed", "path": key, "size": f["off"]})
            self.awaiting[key] = (old, f["name"])
            self.done.add(key)
        self.files = {}
        for day in days:                   # late files of the old day, and skipped days
            if old <= day < new:
                self.catch_up_sealed(day)
        self.live = new
        self.scan_live()

    def check_trailers(self):
        now = time.time()
        if now < self.next_trailer:
            return
        self.next_trailer = now + self.trailer_poll_s
        for key in sorted(self.awaiting):
            day, name = self.awaiting[key]
            dfd = open_day(self.root, day)
            if dfd is None:
                continue
            try:
                if entry(dfd, name) is not None:
                    continue               # gzip is not done with it
                fh = open_file(dfd, name + GZ)
                if fh is None:
                    continue
                with fh:
                    self.send_trailer(key, fh)
                del self.awaiting[key]
            finally:
                os.close(dfd)

    # ---- the loop
    def days(self):
        """The day folders under --root that are real dates no later than this node's
        own tomorrow, oldest first; the live day is the newest. A folder dated later
        (a stray `2036-01-01`) is left alone: it would freeze today's stream as the
        live day, and the receiver refuses it. Raises OSError."""
        last = tomorrow()
        return [d for d in list_days(self.root) if day_date(d) and d <= last]

    def read_hello(self):
        """The receiver's hello line, without its newline, or None at the end of input.
        One longer than MAX_HELLO raises ProtocolError before more of it is read."""
        parts, n = [], 0
        while True:
            chunk = os.read(self.in_fd, 65536)
            if not chunk:
                return None
            end = chunk.find(b"\n")
            parts.append(chunk if end < 0 else chunk[:end])
            n += len(parts[-1])
            if n > MAX_HELLO:
                raise ProtocolError("a hello longer than %d bytes" % MAX_HELLO)
            if end >= 0:
                return b"".join(parts)

    def run(self):
        line = self.read_hello()
        if line is None:
            return 0
        try:
            hello = json.loads(line.decode("utf-8"))
        except ValueError:
            self.emit({"t": "error", "msg": "the hello is not JSON"})
            return 1
        if not isinstance(hello, dict) or hello.get("v") != PROTOCOL:
            self.emit({"t": "error", "msg": "protocol %r is not %d" % (
                hello.get("v") if isinstance(hello, dict) else None, PROTOCOL)})
            return 1
        self.have = hello.get("have") or {}
        keep = max(1, int(hello.get("days", 1)))
        try:
            days = self.days()
        except OSError as e:
            self.emit({"t": "error", "msg": "cannot read %s: %s" % (self.root, e.strerror)})
            return 1
        self.live = days[-1] if days else None
        self.emit({"t": "hello", "v": PROTOCOL, "host": socket.gethostname().split(".")[0],
                   "live": self.live})
        # The sealed days among the `keep` newest that are within `keep` days of the live
        # one: the receiver keeps days by date, so an older one (a gap in the node's
        # folders) would only be dropped there, and sent again at each connection.
        first = day_date(self.live).toordinal() - keep + 1 if self.live else 0
        for day in days[-keep:-1]:
            if day_date(day).toordinal() >= first:
                self.catch_up_sealed(day)
        if self.live:
            self.scan_live()
            self.follow_live()
        while True:
            ready, _, _ = select.select([self.in_fd], [], [], self.poll_s)
            if ready and not os.read(self.in_fd, 65536):
                return 0                   # the receiver closed its end
            try:
                days = self.days()
            except OSError as e:
                self.emit({"t": "error", "msg": "cannot read %s: %s" % (self.root, e.strerror)})
                return 1
            if self.live is None:
                if days:
                    self.live = days[-1]
            else:
                self.check_rollover(days)
            if self.live:
                self.scan_live()
                self.follow_live()
            self.check_trailers()
            self.ping_if_quiet()


def send_main(root, inp, out, poll_s=1.0, ping_s=30.0, trailer_poll_s=30.0):
    """Run one sender over `root`; returns its exit status."""
    s = Sender(root, inp, out, poll_s, ping_s, trailer_poll_s)
    try:
        return s.run()
    except BrokenPipeError:
        return 0
    except Exception as e:                 # report it, then leave
        try:
            s.emit({"t": "error", "msg": "%s: %s" % (type(e).__name__, e)})
        except Exception:
            pass
        return 1


# ------------------------------------------------------------------ receiver
#
# The receiver writes under one base folder: mirror/<host>/<day>/<file> (the copy
# the dashboard reads), staging/<host>/<day>/ (`.gz` fetches being assembled), and
# state/ (the verified ledgers, traffic, and status). With `keep_gz` a sealed day
# sits in the copy as `<file>.gz`, as on the source's own disk, and the dashboard
# reads it as it reads the source's. Nothing in it is opened by an
# absolute path after start: SafeBase checks the folder once and then reaches
# every file through that open handle, so renaming the base folder and putting
# another in its place cannot redirect a write.

KEY_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})/([A-Za-z0-9][A-Za-z0-9._-]*\.jsonl)(\.gz)?$")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


class UnsafeBase(Exception):
    pass


class DiskFull(ProtocolError):
    """A write hit ENOSPC; the torn write was cut back and the links pause."""


def _parts(rel):
    parts = rel.split("/")
    for p in parts:
        if not NAME_RE.match(p):           # also refuses "", ".", and ".."
            raise ValueError("a bad path component %r in %r" % (p, rel))
    return parts


# POSIX ACLs in their xattr form: a 4-byte version, then 8-byte entries (tag, perm, id),
# little-endian.
ACL_ACCESS, ACL_DEFAULT = "system.posix_acl_access", "system.posix_acl_default"
_ACL_USER_OBJ, _ACL_GROUP_OBJ, _ACL_MASK, _ACL_OTHER = 0x01, 0x04, 0x10, 0x20


def acl_for_new(default, mode):
    """The access ACL that an entry made with `mode` gets from its folder's default ACL
    `default`, as the kernel gives it: the owner entry keeps only the mode's owner bits,
    the mask (the owning group's entry when there is no mask) only its group bits, and
    the other entry only its other bits. Named entries are kept; the mask limits them."""
    version, = struct.unpack_from("<I", default, 0)
    entries = [struct.unpack_from("<HHI", default, off) for off in range(4, len(default), 8)]
    has_mask = any(tag == _ACL_MASK for tag, _, _ in entries)
    out = [struct.pack("<I", version)]
    for tag, perm, ident in entries:
        if tag == _ACL_USER_OBJ:
            perm &= (mode >> 6) & 7
        elif tag == _ACL_MASK or (tag == _ACL_GROUP_OBJ and not has_mask):
            perm &= (mode >> 3) & 7
        elif tag == _ACL_OTHER:
            perm &= mode & 7
        out.append(struct.pack("<HHI", tag, perm, ident))
    return b"".join(out)


class SafeBase:
    """A base folder checked at open, and every path under it reached by dir_fd.

    With `private` false (site.env's PRIVATE_TREE=false, for a host that only the team can
    log in to), the owner and the mode are not checked: an ACL that lets one more account
    read the copy changes the modes, which would otherwise stop the stream. Each folder
    must still be a real folder, and the same one (device and inode) as at start. New
    folders and files then follow their folder's default ACL, as entries made there by
    hand would: they are made 0777 and 0666 (the umask, 077 in main(), applies where
    there is no default ACL), and an entry moved in from staging/ gets the ACL it would
    have had if made in its new folder."""

    def __init__(self, path, fd, idents, private=True):
        self.path, self.fd, self.idents, self.private = path, fd, idents, private
        self.dmode, self.fmode = (0o700, 0o600) if private else (0o777, 0o666)

    @staticmethod
    def _ident(st, private=True):
        if not private:
            return (st.st_dev, st.st_ino)
        return (st.st_dev, st.st_ino, st.st_uid, stat.S_IMODE(st.st_mode))

    @classmethod
    def _private_dir(cls, path, uid, private=True):
        try:
            st = os.lstat(path)
        except OSError as e:
            raise UnsafeBase("%s: %s" % (path, e.strerror))
        if not stat.S_ISDIR(st.st_mode):
            raise UnsafeBase("%s is not a folder (a symlink is refused)" % path)
        if not private:
            return st
        if st.st_uid != uid:
            raise UnsafeBase("%s belongs to uid %d, not %d" % (path, st.st_uid, uid))
        if stat.S_IMODE(st.st_mode) != 0o700:
            raise UnsafeBase("%s has mode %o, not 700" % (path, stat.S_IMODE(st.st_mode)))
        return st

    @classmethod
    def open(cls, path, uid=None, parents=0, private=True):
        """Check `path` and its `parents` nearest ancestors: each a real folder
        (lstat, so a symlink is refused), owned by `uid`, mode 700 (the owner and
        the mode only when `private`). A parent is checked because a folder without
        the sticky bit (a shared /scratch) lets another user put a symlink where the
        parent was, and a path through it would still reach this folder until they
        point it elsewhere."""
        uid = os.getuid() if uid is None else uid
        path = os.path.abspath(path)
        chain = [path]
        for _ in range(parents):
            chain.append(os.path.dirname(chain[-1]))
        idents = [(p, cls._ident(cls._private_dir(p, uid, private), private))
                  for p in reversed(chain)]
        fd = os.open(path, _DIR_FLAGS)
        if cls._ident(os.fstat(fd), private) != idents[-1][1]:
            os.close(fd)
            raise UnsafeBase("%s changed while it was being opened" % path)
        return cls(path, fd, idents, private)

    def check(self):
        """Raise UnsafeBase unless every checked folder is still the one seen at start."""
        for p, ident in self.idents:
            try:
                st = os.lstat(p)
            except OSError as e:
                raise UnsafeBase("%s: %s" % (p, e.strerror))
            if not stat.S_ISDIR(st.st_mode) or self._ident(st, self.private) != ident:
                raise UnsafeBase("%s is no longer the folder opened at start" % p)

    def close(self):
        os.close(self.fd)

    # ---- walking
    def _dirfd(self, parts, create=False):
        fd = os.dup(self.fd)
        try:
            for p in parts:
                try:
                    nfd = os.open(p, _DIR_FLAGS, dir_fd=fd)
                except FileNotFoundError:
                    if not create:
                        raise
                    try:
                        os.mkdir(p, self.dmode, dir_fd=fd)
                    except FileExistsError:
                        pass
                    nfd = os.open(p, _DIR_FLAGS, dir_fd=fd)
                os.close(fd)
                fd = nfd
            return fd
        except BaseException:
            os.close(fd)
            raise

    def _parent(self, rel, create=False):
        parts = _parts(rel)
        return self._dirfd(parts[:-1], create), parts[-1]

    # ---- operations
    def makedirs(self, rel):
        os.close(self._dirfd(_parts(rel), create=True))

    def _open(self, rel, flags, create_dirs):
        pfd, name = self._parent(rel, create_dirs)
        try:
            return os.open(name, flags | os.O_NOFOLLOW, self.fmode, dir_fd=pfd)
        finally:
            os.close(pfd)

    def open_append(self, rel):
        return self._open(rel, os.O_WRONLY | os.O_APPEND | os.O_CREAT, True)

    def open_new(self, rel):
        return self._open(rel, os.O_WRONLY | os.O_CREAT | os.O_EXCL, True)

    def open_read(self, rel):
        return self._open(rel, os.O_RDONLY, False)

    def size(self, rel):
        try:
            pfd, name = self._parent(rel)
        except FileNotFoundError:
            return 0
        try:
            return os.stat(name, dir_fd=pfd, follow_symlinks=False).st_size
        except FileNotFoundError:
            return 0
        finally:
            os.close(pfd)

    def rename(self, src, dst):
        sfd, sname = self._parent(src)
        try:
            dfd, dname = self._parent(dst, create=True)
            try:
                os.rename(sname, dname, src_dir_fd=sfd, dst_dir_fd=dfd)
                if not self.private:
                    self._inherit(dfd, dname)
            finally:
                os.close(dfd)
        finally:
            os.close(sfd)

    def _inherit(self, dfd, name):
        """Give `name` in the folder `dfd` the ACL it would have had if it had been made
        there, from the folder's default ACL. Nothing to do where the folder has none or
        the disk has no ACLs; a failure is logged, never raised."""
        if not hasattr(os, "getxattr"):         # not Linux
            return
        try:
            default = os.getxattr(dfd, ACL_DEFAULT)
        except OSError:
            return
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dfd)
            try:
                is_dir = stat.S_ISDIR(os.fstat(fd).st_mode)
                os.setxattr(fd, ACL_ACCESS, acl_for_new(default, 0o777 if is_dir else 0o666))
                if is_dir:
                    os.setxattr(fd, ACL_DEFAULT, default)
            finally:
                os.close(fd)
        except (OSError, struct.error) as e:
            _log("could not give %s its folder's ACL: %s" % (name, e))

    def unlink(self, rel):
        try:
            pfd, name = self._parent(rel)
        except FileNotFoundError:
            return
        try:
            os.unlink(name, dir_fd=pfd)
        except FileNotFoundError:
            pass
        finally:
            os.close(pfd)

    def listdir(self, rel):
        try:
            fd = self._dirfd(_parts(rel))
        except FileNotFoundError:
            return []
        try:
            return sorted(os.listdir(fd))
        finally:
            os.close(fd)

    def rmtree(self, rel):
        parts = _parts(rel)
        try:
            pfd = self._dirfd(parts[:-1])
        except FileNotFoundError:
            return
        try:
            self._rmtree_at(pfd, parts[-1])
        finally:
            os.close(pfd)

    def _rmtree_at(self, pfd, name):
        try:
            st = os.stat(name, dir_fd=pfd, follow_symlinks=False)
        except FileNotFoundError:
            return
        if stat.S_ISDIR(st.st_mode):
            fd = os.open(name, _DIR_FLAGS, dir_fd=pfd)
            try:
                for child in os.listdir(fd):
                    self._rmtree_at(fd, child)
            finally:
                os.close(fd)
            os.rmdir(name, dir_fd=pfd)
        else:
            os.unlink(name, dir_fd=pfd)

    def read_all(self, rel):
        fd = self.open_read(rel)
        try:
            return b"".join(iter(lambda: os.read(fd, 1 << 20), b""))
        finally:
            os.close(fd)

    def write_atomic(self, rel, data):
        tmp = "%s.tmp-%d-%d" % (rel, os.getpid(), threading.get_ident())
        self.unlink(tmp)
        fd = self.open_new(tmp)
        try:
            _write_all(os.write, fd, data)
        finally:
            os.close(fd)
        self.rename(tmp, rel)

    def free_bytes(self):
        st = os.fstatvfs(self.fd)
        return st.f_bavail * st.f_frsize, st.f_blocks * st.f_frsize


def _write_all(write, fd, data):
    view, done = memoryview(data), 0
    while done < len(data):
        done += write(fd, view[done:])


def _append_at(write, fd, start, data, what):
    """Append `data` to `fd`, whose size is `start`. On ENOSPC cut the file back
    to `start`, so no torn frame stays in the copy, and raise DiskFull."""
    try:
        _write_all(write, fd, data)
    except OSError as e:
        if e.errno != errno.ENOSPC:
            raise
        os.ftruncate(fd, start)
        raise DiskFull("no space left writing %s" % what)


class Mirror:
    """One source host's copy under a SafeBase. See the module docstring for frames."""

    def __init__(self, base, host, write=os.write, keep_gz=False):
        if not NAME_RE.match(host):
            raise ValueError("a bad host name %r" % (host,))
        self.base, self.host, self.write, self.keep_gz = base, host, write, keep_gz
        self.dropped = 0                   # bytes thrown away to be sent again
        for d in ("mirror/" + host, "staging/" + host, "state"):
            base.makedirs(d)
        self.ledger = "state/%s.verified.json" % host
        self.verified = self._load_ledger()

    # ---- the verified ledger
    def _load_ledger(self):
        try:
            return set(json.loads(self.base.read_all(self.ledger).decode("utf-8")))
        except (FileNotFoundError, ValueError):
            return set()

    def _save_ledger(self):
        self.base.write_atomic(self.ledger, json.dumps(sorted(self.verified)).encode("utf-8"))

    def mark_verified(self, key):
        self.verified.add(key)
        self._save_ledger()

    def forget(self, key):
        if key in self.verified:
            self.verified.discard(key)
            self._save_ledger()

    # ---- paths
    def rel(self, day, name):
        return "mirror/%s/%s/%s" % (self.host, day, name)

    def part(self, day, name):
        return "staging/%s/%s/%s.gz.part" % (self.host, day, name)

    def tmp(self, day, name):
        return "staging/%s/%s/%s.tmp" % (self.host, day, name)

    def gz_tmp(self, day, name):
        return "staging/%s/%s/%s.gz.tmp" % (self.host, day, name)

    @staticmethod
    def parse(path):
        """(day, plain name, is_gz) of a frame's path. Its day must be a real date no
        later than this host's tomorrow: retention keeps days by date, and a day
        after that is no node's."""
        m = KEY_RE.match(path if isinstance(path, str) else "")
        if not m:
            raise ProtocolError("a bad path %r" % (path,))
        if day_date(m.group(1)) is None or m.group(1) > tomorrow():
            raise ProtocolError("%r: a day that is not a date, or is after tomorrow" % (path,))
        return m.group(1), m.group(2), bool(m.group(3))

    def _unverified(self, day, name, t):
        """Refuse a `t` frame that would change a file the copy holds verified. No
        honest sender resets, extends, or fetches one again (catch_up_sealed skips
        them), and accepting one would let a node erase or replace a checked day."""
        key = "%s/%s" % (day, name)
        if key in self.verified:
            raise ProtocolError("a %s frame for %s, which is verified" % (t, key))

    def days(self):
        return [d for d in self.base.listdir("mirror/" + self.host) if DAY_RE.match(d)]

    def inventory(self, days):
        """What this copy holds: the hello's `have`. A verified day kept as `.gz`
        counts as held whatever `keep_gz` says, so a copy seeded with `.gz` files
        works under either setting; an unlisted `.gz` is not counted."""
        have = {}
        for day in self.days()[-days:]:
            for name in self.base.listdir("mirror/%s/%s" % (self.host, day)):   # x before x.gz
                if name.endswith(".jsonl"):
                    key = "%s/%s" % (day, name)
                    have[key] = dict(self._head(day, name), verified=key in self.verified)
                elif name.endswith(".jsonl" + GZ):
                    key = "%s/%s" % (day, name[:-len(GZ)])
                    if key in self.verified and key not in have:
                        have[key] = {"verified": True}
        for day in self.base.listdir("staging/" + self.host):
            if not DAY_RE.match(day):
                continue
            for name in self.base.listdir("staging/%s/%s" % (self.host, day)):
                if name.endswith(".jsonl.gz.part"):
                    plain = name[:-len(".gz.part")]
                    have["%s/%s%s" % (day, plain, GZ)] = {
                        "size": self.base.size("staging/%s/%s/%s" % (self.host, day, name))}
        return have

    def _head(self, day, name):
        """The copy's size and a CRC32 of its first HEAD bytes, so a sender can tell
        a source file that was replaced while the link was down."""
        fd = self.base.open_read(self.rel(day, name))
        try:
            size = os.fstat(fd).st_size
            buf = os.read(fd, min(size, HEAD))
        finally:
            os.close(fd)
        return {"size": size, "head": zlib.crc32(buf) & 0xffffffff, "head_n": len(buf)}

    def take_dropped(self):
        n, self.dropped = self.dropped, 0
        return n

    # ---- frames
    def apply(self, h, payload, inflater):
        """Apply one frame; returns the plain bytes written to the copy."""
        t = h.get("t")
        if t == "data":
            return self._data(h, payload, inflater)
        if t == "gz":
            self._gz(h, payload)
        elif t == "sealed":
            self._sealed(h)
        elif t == "trailer":
            self._trailer(h)
        elif t == "reset":
            self._reset(h)
        else:
            raise ProtocolError("an unknown frame %r" % (t,))
        return 0

    def _data(self, h, payload, inflater):
        """A sender's data frame holds at most CHUNK plain bytes (send_plain, and
        inflate_pieces' pieces). The size it states is checked first, and the
        payload is inflated only up to one byte past it, so a small payload that
        would inflate to gigabytes cannot. An honest sender never ends its zlib
        stream; once one has ended, each later payload would pile up in the
        inflater's `unused_data`."""
        day, name, is_gz = self.parse(h.get("path"))
        if is_gz:
            raise ProtocolError("plain data for a .gz path")
        self._unverified(day, name, "data")
        plain = h.get("plain")
        if type(plain) is not int or not 0 <= plain <= CHUNK:
            raise ProtocolError("a data frame of %r plain bytes" % (plain,))
        data = inflater.decompress(payload, plain + 1)
        if len(data) > plain or inflater.unconsumed_tail:
            raise ProtocolError("a data frame that inflates past the %d bytes it says" % plain)
        if inflater.eof:
            raise ProtocolError("a data frame that ends the zlib stream")
        if len(data) != plain:
            raise ProtocolError("a data frame of %d bytes says %r" % (len(data), plain))
        fd = self.base.open_append(self.rel(day, name))
        try:
            local = os.fstat(fd).st_size
            if local != h.get("off"):
                raise ProtocolError("data for %s at %r, the copy has %d" % (h["path"], h.get("off"), local))
            _append_at(self.write, fd, local, data, h["path"])
        finally:
            os.close(fd)
        return len(data)

    def _gz(self, h, payload):
        day, name, is_gz = self.parse(h.get("path"))
        if not is_gz:
            raise ProtocolError("gz bytes for a plain path")
        self._unverified(day, name, "gz")
        size = h.get("size")
        fd = self.base.open_append(self.part(day, name))
        try:
            local = os.fstat(fd).st_size
            if local != h.get("off"):
                raise ProtocolError("gz for %s at %r, staging has %d" % (h["path"], h.get("off"), local))
            if local + len(payload) > size:
                raise ProtocolError("more .gz bytes than %s's size" % h["path"])
            _append_at(self.write, fd, local, payload, h["path"])
        finally:
            os.close(fd)
        if local + len(payload) == size:
            self._finish_gz(day, name)

    def _finish_gz(self, day, name):
        """A whole `.gz` is staged. Inflate it into the copy (zlib checks its CRC), or
        with `keep_gz` check it the same way and move it into the copy as it is."""
        part, key = self.part(day, name), "%s/%s" % (day, name)
        if self.keep_gz:
            self._inflate_staged(part, key)
            self.place_gz(day, name, part)
            return
        tmp = self.tmp(day, name)
        self._inflate_staged(part, key, tmp)
        self.base.rename(tmp, self.rel(day, name))
        self.base.unlink(part)
        self.mark_verified(key)

    def _inflate_staged(self, part, key, tmp=None):
        """Inflate a staged `.gz` into `tmp`, or only check it with no `tmp`. One that
        does not inflate is dropped from staging, so it is fetched again."""
        if tmp is not None:
            self.base.unlink(tmp)
        rfd = self.base.open_read(part)
        try:
            wfd = self.base.open_new(tmp) if tmp is not None else None
            try:
                with os.fdopen(os.dup(rfd), "rb") as fh:
                    for piece in inflate_pieces(fh):
                        if wfd is not None:
                            _append_at(self.write, wfd, os.fstat(wfd).st_size, piece, tmp)
            finally:
                if wfd is not None:
                    os.close(wfd)
        except DiskFull:
            self.base.unlink(tmp)          # the staged part stays; it inflates next time
            raise
        except (zlib.error, ProtocolError) as e:
            if tmp is not None:
                self.base.unlink(tmp)
            self.base.unlink(part)
            raise ProtocolError("%s%s did not inflate: %s" % (key, GZ, e))
        finally:
            os.close(rfd)

    def place_gz(self, day, name, src):
        """Move a checked `.gz` (`src`, under the base) into the copy as `<name>.gz`,
        then drop the plain copy, then list the day as verified. A stop part way
        leaves the plain copy or an unlisted `.gz`, and the next connection checks
        or fetches the day again."""
        fd = self.base.open_read(src)
        try:
            os.fsync(fd)                   # once the plain copy goes, this is the only one
        finally:
            os.close(fd)
        self.base.rename(src, self.rel(day, name) + GZ)
        self.base.unlink(self.rel(day, name))
        self.mark_verified("%s/%s" % (day, name))

    def _sealed(self, h):
        day, name, is_gz = self.parse(h.get("path"))
        local = self.base.size(self.rel(day, name))
        if is_gz or local != h.get("size"):
            raise ProtocolError("%s sealed at %r, the copy has %d" % (h["path"], h.get("size"), local))

    def _trailer(self, h):
        day, name, _ = self.parse(h.get("path"))
        key = "%s/%s" % (day, name)
        if key in self.verified:
            return
        try:
            fd = self.base.open_read(self.rel(day, name))
        except FileNotFoundError:
            return
        tmp = self.gz_tmp(day, name) if self.keep_gz else None
        try:
            crc, n = self._sum(fd, tmp)
        finally:
            os.close(fd)
        if crc == h.get("crc") and n % 2 ** 32 == h.get("isize"):
            if tmp is None:
                self.mark_verified(key)
            else:
                self.place_gz(day, name, tmp)
            return
        if tmp is not None:
            self.base.unlink(tmp)
        self.dropped += n
        self.base.unlink(self.rel(day, name))
        raise ProtocolError("%s failed its check against the source's gzip trailer" % key)

    def _sum(self, fd, tmp=None):
        """CRC32 and size of an open plain copy, read once. With `tmp`, the same pass
        gzips it there; on ENOSPC the partial `.gz` goes and DiskFull is raised."""
        crc, n, done, wfd, z = 0, 0, 0, None, None
        if tmp is not None:
            self.base.unlink(tmp)
            wfd = self.base.open_new(tmp)
            z = zlib.compressobj(GZ_LEVEL, zlib.DEFLATED, 16 + zlib.MAX_WBITS)
        try:
            while True:
                buf = os.read(fd, 1 << 20)
                if not buf:
                    break
                crc, n = zlib.crc32(buf, crc), n + len(buf)
                if wfd is not None:
                    out = z.compress(buf)
                    _append_at(self.write, wfd, done, out, tmp)
                    done += len(out)
            if wfd is not None:
                _append_at(self.write, wfd, done, z.flush(), tmp)
        except BaseException:
            if wfd is not None:
                os.close(wfd)
                self.base.unlink(tmp)
            raise
        if wfd is not None:
            os.close(wfd)
        return crc & 0xffffffff, n

    def _reset(self, h):
        day, name, is_gz = self.parse(h.get("path"))
        self._unverified(day, name, "reset")
        rel = self.part(day, name) if is_gz else self.rel(day, name)
        self.dropped += self.base.size(rel)
        self.base.unlink(rel)
        if not is_gz:
            self.forget("%s/%s" % (day, name))


class Counters:
    """Bytes per host: on the wire, plain written, `.gz` fetched, and re-sent."""
    FIELDS = ("wire", "plain", "gz", "resent")

    def __init__(self):
        self.lock = threading.Lock()
        self.pending, self.totals, self.daily = {}, {}, {}

    def add(self, host, wire=0, plain=0, gz=0, resent=0):
        hour = time.strftime("%Y-%m-%d %H:00")
        vals = {"wire": wire, "plain": plain, "gz": gz, "resent": resent}
        with self.lock:
            for table, k in ((self.pending, (host, hour)), (self.totals, host),
                             (self.daily, (host, hour[:10]))):
                row = table.setdefault(k, dict.fromkeys(self.FIELDS, 0))
                for f, v in vals.items():
                    row[f] += v

    def total(self, host, field):
        with self.lock:
            return self.totals.get(host, {}).get(field, 0)

    def today(self, host):
        with self.lock:
            return dict(self.daily.get((host, time.strftime("%Y-%m-%d")), dict.fromkeys(self.FIELDS, 0)))

    def flush(self, base):
        """Append one JSON line per host-hour counted since the last flush."""
        with self.lock:
            rows, self.pending = self.pending, {}
        if not rows:
            return
        lines = "".join(json.dumps(dict(row, host=host, hour=hour), sort_keys=True) + "\n"
                        for (host, hour), row in sorted(rows.items()))
        fd = base.open_append("state/traffic.jsonl")
        try:
            _write_all(os.write, fd, lines.encode("utf-8"))
        finally:
            os.close(fd)


class _Counting:
    """The transport's stdout, read straight from its pipe, counting the bytes. A read
    that waits more than `stall_s` for its next byte raises Stalled. Only waiting
    counts: time spent applying a frame (a long trailer check) does not."""

    def __init__(self, raw, stall_s=None):
        self.fd, self.n, self.stall_s = raw.fileno(), 0, stall_s
        self.poll = select.poll()
        self.poll.register(self.fd, select.POLLIN)

    def read(self, n):
        if self.stall_s and not self.poll.poll(max(1, int(self.stall_s * 1000))):
            raise Stalled("nothing from the sender for %g s" % self.stall_s)
        b = os.read(self.fd, min(n, 1 << 16))
        self.n += len(b)
        return b


class Link:
    """One source host: keeps one transport (ssh, or any argv speaking the
    protocol) open, applies its frames, and reconnects with backoff.

    A sender pings after `--ping` (30) seconds with nothing else to send, even in
    a long catch-up, so a read that waits `stall_s` (300) seconds for anything at
    all ends the connection (Stalled). The link counts as connected only once a
    frame after the sender's hello has been applied: a sender that says hello and
    then stops, or whose frames are refused, stays down, and its down_since keeps
    counting across reconnections."""

    def __init__(self, host, argv, mirror, days, counters, paused, stop, backoff=(5, 300),
                 stall_s=STALL_S):
        self.host, self.argv, self.mirror, self.days = host, list(argv), mirror, days
        self.counters, self.paused, self.stop, self.backoff = counters, paused, stop, backoff
        self.stall_s = stall_s
        self.proc = None
        self.connected = False
        self.down_since = time.time()      # None while connected
        self.live = None
        self.last_error = None
        self.last_frame = None
        self.attempts = []
        self.stderr_tail = collections.deque(maxlen=5)

    def close(self):
        """End the current transport, which also ends a read blocked on it."""
        p = self.proc
        if p is not None and p.poll() is None:
            try:
                p.terminate()
            except OSError:
                pass

    def status(self):
        lag = None if self.last_frame is None else round(time.time() - self.last_frame, 1)
        return {"connected": self.connected, "live": self.live, "lag_s": lag,
                "down_since": self.down_since, "last_error": self.last_error,
                "bytes_today": self.counters.today(self.host)}

    def run(self):
        delay = self.backoff[0]
        while not self.stop.is_set():
            if self.paused.is_set():
                self.stop.wait(0.5)
                continue
            started = time.time()
            self.attempts.append(started)
            try:
                self._session()
                err = "the stream ended"
            except Exception as e:         # any failure means: reconnect later
                err = "%s: %s" % (type(e).__name__, e)
                _log("%s: %s" % (self.host, err))      # in the log, as in the status
                if isinstance(e, DiskFull):
                    self.paused.set()      # every link waits until space returns
            finally:
                if self.connected or self.down_since is None:
                    self.down_since = time.time()
                self.connected = False
                self._close_transport()
            if self.stderr_tail:
                err = "%s (transport: %s)" % (err, self.stderr_tail[-1])
                dropped = self.mirror.take_dropped()
                if dropped:
                    self.counters.add(self.host, resent=dropped)
            if self.stop.is_set():
                break
            if self.paused.is_set():
                continue
            self.last_error = err
            if time.time() - started >= 60:
                delay = self.backoff[0]
            self.stop.wait(delay)
            delay = min(delay * 2, self.backoff[1])

    def _drain_stderr(self, pipe):
        """Keep the transport's last stderr lines (ssh's own errors) and log them. A
        line is read at most STDERR_LINE bytes at a time and the rest of a longer one
        is dropped, so a transport that writes without a newline cannot fill memory."""
        rest = False                       # inside the dropped rest of a long line
        for raw in iter(lambda: pipe.readline(STDERR_LINE), b""):
            cut, rest = rest, not raw.endswith(b"\n")
            if cut:
                continue
            line = raw.decode("utf-8", "replace").strip()
            if line:
                self.stderr_tail.append(line[:300])
                _log("%s: %s" % (self.host, line[:300]))
        pipe.close()

    def _session(self):
        self.stderr_tail.clear()
        self.proc = subprocess.Popen(self.argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE)
        self._stderr_thread = threading.Thread(target=self._drain_stderr, args=(self.proc.stderr,),
                                               daemon=True)
        self._stderr_thread.start()
        hello = {"v": PROTOCOL, "days": self.days, "have": self.mirror.inventory(self.days)}
        self.proc.stdin.write((json.dumps(hello) + "\n").encode("utf-8"))
        self.proc.stdin.flush()
        src = _Counting(self.proc.stdout, self.stall_s)
        inflater = zlib.decompressobj()
        while not self.stop.is_set() and not self.paused.is_set():
            before = src.n
            f = read_frame(src)
            if f is None:
                return
            h, payload = f
            self.counters.add(self.host, wire=src.n - before)
            self.last_frame = time.time()
            t = h.get("t")
            if t == "hello":
                self.live = h.get("live")
            elif t == "error":
                raise ProtocolError("the sender says: %s" % h.get("msg"))
            elif t != "ping":
                n = self.mirror.apply(h, payload, inflater)
                if t == "data":
                    self.counters.add(self.host, plain=n)
                    day = h["path"].split("/", 1)[0]
                    if self.live is None or day > self.live:
                        self.live = day
                elif t == "gz":
                    self.counters.add(self.host, gz=len(payload))
            if t != "hello" and not self.connected:   # a frame after the hello applied
                self.connected, self.down_since, self.last_error = True, None, None
            dropped = self.mirror.take_dropped()
            if dropped:
                self.counters.add(self.host, resent=dropped)

    def _close_transport(self):
        p = self.proc
        if p is None:
            return
        try:
            p.stdin.close()
        except Exception:
            pass
        if p.poll() is None:
            try:
                p.terminate()
            except OSError:
                pass
        try:
            p.wait(5)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait(5)
        try:
            p.stdout.close()
        except Exception:
            pass
        t = getattr(self, "_stderr_thread", None)
        if t is not None:
            t.join(2)


def load_config(path):
    with open(path) as fh:
        cfg = json.load(fh)
    for k in ("base", "days", "hosts"):
        if k not in cfg:
            raise ValueError("%s lacks %r" % (path, k))
    return cfg


def _log(msg):
    sys.stderr.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    sys.stderr.flush()


FREE_MIN_BYTES = 300e9
FREE_MIN_FRACTION = 0.10


def free_floor(config, total):
    """The free bytes below which every link pauses: the larger of `free_min_gb`
    (300) and `free_min_fraction` (0.10) of the volume, from the receiver's config."""
    gb = config.get("free_min_gb")
    frac = config.get("free_min_fraction")
    return max(FREE_MIN_BYTES if gb is None else float(gb) * 1e9,
               (FREE_MIN_FRACTION if frac is None else float(frac)) * total)


class Receiver:
    """Every source host's Link, plus the guards around them:

    * the watchdog: every `watchdog_s` the base path must still lead to the folder
      opened at start. If not, every link stops, `state/TRIPPED` is written into
      the real folder through its handle, the dashboard is stopped, and run()
      returns EXIT_UNSAFE, which keep-running.sh does not restart on;
    * `private` (default true): the base and its checked parents must be this
      account's 700 folders, at start and at every watchdog round. False (a host
      that only the team can log in to) leaves modes, owners, and ACLs to the user;
      a folder put in the base's place still trips;
    * the free-space guard: below max(`free_min_gb`, `free_min_fraction` of the
      volume), 300 GB and 10% unless the config says otherwise, every link pauses,
      and resumes once space returns;
    * retention: each host keeps the day folders dated within its last `days` days,
      today included, and tomorrow (a node's clock may run a little ahead), by
      date, so no folder a node names can push a day out. A frame that names a
      later day, or would reset, extend, or fetch again a verified file, is
      refused;
    * `stall_s` (default 300): a link whose sender sends nothing, not even its
      pings, for that long is closed, shows as down, and reconnects (Link);
    * status every 10 s and traffic every 60 s, into state/;
    * `keep_gz` (default false): sealed days stay in the copy as `.gz`, about 19x
      smaller on disk. A day fetched whole keeps the collector's own file; the live
      day, once sealed and checked, is gzipped here, so nothing crosses twice.
      The dashboard reads `.gz` too, but it holds an index for each one it reads.
    """

    def __init__(self, config, statvfs=None, stop_dashboard=None, watchdog_s=10.0, uid=None,
                 space_s=60.0, retention_s=3600.0):
        self.cfg = config
        self.days = int(config["days"])
        self.statvfs, self.stop_dashboard = statvfs, stop_dashboard
        self.watchdog_s, self.space_s, self.retention_s = watchdog_s, space_s, retention_s
        self.uid = uid
        self.stop, self.paused = threading.Event(), threading.Event()
        self.counters = Counters()
        self.base, self.links, self.threads = None, [], []

    def _default_stop_dashboard(self):
        unit = self.cfg.get("dashboard_unit") or "rc-dashboard.scope"
        try:
            subprocess.call(["systemctl", "--user", "stop", unit], timeout=60)
        except Exception as e:
            _log("could not stop %s: %s" % (unit, e))

    def check_space(self):
        free, total = (self.statvfs or self.base.free_bytes)()
        low = free < free_floor(self.cfg, total)
        if low and not self.paused.is_set():
            _log("pausing: %.0f GB free of %.0f GB" % (free / 1e9, total / 1e9))
            self.paused.set()
            for link in self.links:
                link.close()
        elif not low and self.paused.is_set():
            _log("resuming: %.0f GB free" % (free / 1e9))
            self.paused.clear()

    def retention(self):
        """Keep each host's day folders dated from `days` - 1 days before today to
        tomorrow, in the copy and in staging; the ledger forgets the others. By date
        relative to today, not by the order of whatever folders exist, so a node that
        names days of its own cannot push the real ones out."""
        today = datetime.date.today()
        first = (today - datetime.timedelta(days=max(1, self.days) - 1)).isoformat()
        last = tomorrow(today)

        def kept(day):
            return day_date(day) is not None and first <= day <= last
        for link in self.links:
            m = link.mirror
            for day in m.days():
                if not kept(day):
                    self.base.rmtree("mirror/%s/%s" % (m.host, day))
            for day in self.base.listdir("staging/" + m.host):
                if DAY_RE.match(day) and not kept(day):
                    self.base.rmtree("staging/%s/%s" % (m.host, day))
            gone = set(k for k in m.verified if not kept(k.split("/", 1)[0]))
            if gone:
                m.verified -= gone
                m._save_ledger()

    def write_status(self):
        self.base.write_atomic("state/status.json", json.dumps(
            {"at": time.strftime("%Y-%m-%d %H:%M:%S"), "at_epoch": time.time(),
             "paused": self.paused.is_set(),
             "hosts": {l.host: l.status() for l in self.links}}, sort_keys=True).encode("utf-8"))

    def _write_marker(self, why):
        """`trip_marker` sits in the deployment's own 700 folder, the base folder's
        parent, which the receiver checks too, so the hourly check and the start
        checks see it even after the base folder was swapped away."""
        marker = self.cfg.get("trip_marker")
        if not marker:
            return
        try:
            fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            try:
                _write_all(os.write, fd, ("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"),
                                                       why)).encode("utf-8"))
            finally:
                os.close(fd)
        except OSError as e:
            _log("could not write %s: %s" % (marker, e))

    def _trip(self, why):
        """Write the marker, stop the dashboard (it reads by path), then the links. The
        marker comes first: a dashboard start already past its start checks looks for it
        again just before it starts."""
        _log("stopping: %s" % why)
        self.stop.set()
        self._write_marker(why)
        (self.stop_dashboard or self._default_stop_dashboard)()
        for link in self.links:
            link.close()
        try:
            self.base.write_atomic("state/TRIPPED", ("%s %s\n" % (
                time.strftime("%Y-%m-%d %H:%M:%S"), why)).encode("utf-8"))
        except OSError as e:
            _log("could not write state/TRIPPED: %s" % e)
        for t in self.threads:
            t.join(10)
        return EXIT_UNSAFE

    def run(self):
        try:
            self.base = SafeBase.open(self.cfg["base"], uid=self.uid,
                                      parents=int(self.cfg.get("base_parents", 0)),
                                      private=self.cfg.get("private", True) is not False)
        except UnsafeBase as e:
            _log("refusing to start: %s" % e)
            self._write_marker("refused to start: %s" % e)
            (self.stop_dashboard or self._default_stop_dashboard)()
            return EXIT_UNSAFE
        backoff = tuple(self.cfg.get("backoff") or (5, 300))
        keep_gz = bool(self.cfg.get("keep_gz", False))
        stall_s = float(self.cfg.get("stall_s") or STALL_S)
        self.links = [Link(host, argv, Mirror(self.base, host, keep_gz=keep_gz), self.days,
                           self.counters, self.paused, self.stop, backoff=backoff,
                           stall_s=stall_s)
                      for host, argv in sorted(self.cfg["hosts"].items())]
        self.threads = [threading.Thread(target=l.run, name=l.host, daemon=True) for l in self.links]
        for t in self.threads:
            t.start()
        _log("streaming %d hosts into %s" % (len(self.links), self.cfg["base"]))
        tick = min(self.watchdog_s, self.space_s, 1.0)
        due = {"space": 0.0, "retention": 0.0, "status": 0.0, "flush": time.time() + 60}
        while not self.stop.is_set():
            try:
                self.base.check()
            except UnsafeBase as e:
                return self._trip(e)
            now = time.time()
            try:
                if now >= due["space"]:
                    self.check_space()
                    due["space"] = now + self.space_s
                if now >= due["retention"]:
                    self.retention()
                    due["retention"] = now + self.retention_s
                if now >= due["status"]:
                    self.write_status()
                    due["status"] = now + 10
                if now >= due["flush"]:
                    self.counters.flush(self.base)
                    due["flush"] = now + 60
            except OSError as e:           # a full disk must not end the watchdog
                _log("housekeeping failed: %s" % e)
            self.stop.wait(tick)
        for link in self.links:
            link.close()
        for t in self.threads:
            t.join(10)
        try:
            self.counters.flush(self.base)
            self.write_status()
        except OSError:
            pass
        return 0


def receive_main(config_path):
    """Run the Receiver until SIGTERM; returns the exit status."""
    os.umask(0o077)
    r = Receiver(load_config(config_path))
    signal.signal(signal.SIGTERM, lambda *a: r.stop.set())
    signal.signal(signal.SIGINT, lambda *a: r.stop.set())
    return r.run()


def status_main(base_path):
    """Print each host's state and today's bytes, from the receiver's state/ files."""
    state = os.path.join(base_path, "state")
    try:
        with open(os.path.join(state, "status.json")) as fh:
            st = json.load(fh)
    except (OSError, ValueError) as e:
        print("no status yet: %s" % e)
        return 1
    totals = {}
    try:
        with open(os.path.join(state, "traffic.jsonl")) as fh:
            for line in fh:
                row = json.loads(line)
                if row["hour"][:10] == time.strftime("%Y-%m-%d"):
                    t = totals.setdefault(row["host"], dict.fromkeys(Counters.FIELDS, 0))
                    for f in Counters.FIELDS:
                        t[f] += row.get(f, 0)
    except OSError:
        pass
    print("status at %s" % st.get("at"))
    for host, s in sorted(st.get("hosts", {}).items()):
        t = totals.get(host, dict.fromkeys(Counters.FIELDS, 0))
        print("%-12s %-9s live %-10s lag %6ss  today: wire %s, plain %s, gz %s, resent %s%s" % (
            host, "connected" if s.get("connected") else "DOWN", s.get("live"), s.get("lag_s"),
            t["wire"], t["plain"], t["gz"], t["resent"],
            ("  last error: %s" % s["last_error"]) if s.get("last_error") else ""))
    return 0


# ------------------------------------------------------------------------ cli

def _exit_quietly(code):
    """Leave without the interpreter's final flush of a stdout whose reader is gone."""
    try:
        sys.stdout.flush()
    except Exception:
        pass
    os._exit(code)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="fleet_stream")
    sub = ap.add_subparsers(dest="cmd")
    s = sub.add_parser("send", help="stream a node's capture to stdout")
    s.add_argument("--root", default="/scratch/ebpfm")
    s.add_argument("--poll", type=float, default=1.0)
    s.add_argument("--ping", type=float, default=30.0)
    s.add_argument("--trailer-poll", type=float, default=30.0)
    r = sub.add_parser("receive", help="keep the local copy of every source host")
    r.add_argument("--config", required=True)
    st = sub.add_parser("status", help="print the receiver's state")
    st.add_argument("--base", required=True)
    args = ap.parse_args(argv)
    if args.cmd == "send":
        _exit_quietly(send_main(args.root, sys.stdin.buffer, sys.stdout.buffer,
                                args.poll, args.ping, args.trailer_poll))
    if args.cmd == "receive":
        return receive_main(args.config)
    if args.cmd == "status":
        return status_main(args.base)
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
