#!/usr/bin/env python3
"""Minimal netlink socket-diag (NETLINK_SOCK_DIAG) client — no external binary.

Answers "what sockets are open right now, and what has each one moved?" for every
user, which is what `ss --info --tcp` prints. We call the interface directly
instead of parsing `ss` output because that output shifts between iproute2
versions, and because one syscall round trip is cheaper than a process spawn.

What this gives that the eBPF side cannot: the kernel's own `tcp_info` for a
socket that is *still open* — RTT, retransmits, byte totals, and the last-send /
last-recv idle times. What it cannot give: a pid. `inet_diag_msg` carries uid and
inode, never a process, which is why `ss -p` has to scan every /proc/*/fd to
attribute a socket. The collector joins these rows to its own BPF birth map on
the four-tuple instead.

Pure-parse entry points (`parse_messages`, `parse_tcp_info`) take bytes and are
unit-tested without a socket.
"""
import socket
import struct

NETLINK_SOCK_DIAG = 4
SOCK_DIAG_BY_FAMILY = 20

NLM_F_REQUEST = 0x001
NLM_F_ROOT    = 0x100
NLM_F_MATCH   = 0x200
NLM_F_DUMP    = NLM_F_ROOT | NLM_F_MATCH

NLMSG_DONE  = 3
NLMSG_ERROR = 2

INET_DIAG_INFO = 2                      # extension id; the ext bitmask uses 1 << (id - 1)
EXT_INFO_BIT   = 1 << (INET_DIAG_INFO - 1)

# TCP states, as the kernel numbers them. UDP sockets report 7 (CLOSE) when
# unconnected and 1 (ESTABLISHED) once connect()ed.
TCP_STATES = {1: 'ESTABLISHED', 2: 'SYN_SENT', 3: 'SYN_RECV', 4: 'FIN_WAIT1',
              5: 'FIN_WAIT2', 6: 'TIME_WAIT', 7: 'CLOSE', 8: 'CLOSE_WAIT',
              9: 'LAST_ACK', 10: 'LISTEN', 11: 'CLOSING'}
ALL_STATES = 0xFFFFFFFF

_NLMSGHDR = struct.Struct('=IHHII')            # len, type, flags, seq, pid
# inet_diag_req_v2: family, protocol, ext, pad, states, then a 48-byte sockid
_REQ = struct.Struct('=BBBBI')
_SOCKID_LEN = 48
# inet_diag_msg: family, state, timer, retrans, sockid(48), expires, rqueue,
# wqueue, uid, inode
_MSG_HEAD = struct.Struct('=BBBB')
_MSG_TAIL = struct.Struct('=IIIII')
_RTATTR = struct.Struct('=HH')

# tcp_info field offsets we read. The struct has grown over time by appending, so
# a prefix read is safe on every kernel; each read is guarded by the payload size.
_TI = {
    'retransmits':    (2,   'B'),
    'last_data_sent': (44,  'I'),
    'last_data_recv': (52,  'I'),
    'rtt_us':         (68,  'I'),
    'rttvar_us':      (72,  'I'),
    'snd_cwnd':       (80,  'I'),
    'total_retrans':  (100, 'I'),
    'bytes_acked':    (120, 'Q'),
    'bytes_received': (128, 'Q'),
    'segs_out':       (136, 'I'),
    'segs_in':        (140, 'I'),
}


def parse_tcp_info(blob):
    """INET_DIAG_INFO payload -> dict of the fields we use, size-guarded."""
    out = {}
    for name, (off, fmt) in _TI.items():
        end = off + struct.calcsize('=' + fmt)
        if end <= len(blob):
            out[name] = struct.unpack_from('=' + fmt, blob, off)[0]
    return out


def _addr(raw, family):
    if family == socket.AF_INET:
        return socket.inet_ntop(socket.AF_INET, raw[:4])
    return socket.inet_ntop(socket.AF_INET6, raw[:16])


def parse_messages(blob):
    """A netlink response buffer -> list of socket dicts. Pure; no I/O.

    Returns [] on NLMSG_DONE and raises nothing on NLMSG_ERROR (the caller sees a
    short list rather than an exception, because a partial dump is still useful)."""
    out = []
    off = 0
    n = len(blob)
    while off + _NLMSGHDR.size <= n:
        mlen, mtype, _flags, _seq, _pid = _NLMSGHDR.unpack_from(blob, off)
        if mlen < _NLMSGHDR.size or off + mlen > n:
            break
        if mtype in (NLMSG_DONE, NLMSG_ERROR):
            break
        body = off + _NLMSGHDR.size
        end = off + mlen
        if body + _MSG_HEAD.size + _SOCKID_LEN + _MSG_TAIL.size <= end:
            family, state, timer, retrans = _MSG_HEAD.unpack_from(blob, body)
            p = body + _MSG_HEAD.size
            sport, dport = struct.unpack_from('!HH', blob, p)
            src = blob[p + 4:p + 20]
            dst = blob[p + 20:p + 36]
            # p+36: idiag_if (4), p+40: idiag_cookie (8)
            t = p + _SOCKID_LEN
            _expires, rqueue, wqueue, uid, inode = _MSG_TAIL.unpack_from(blob, t)
            rec = {
                'family': 'inet6' if family == socket.AF_INET6 else 'inet',
                'state': TCP_STATES.get(state, str(state)),
                'state_num': state,
                'retrans_now': retrans,
                'timer': timer,
                'saddr': _addr(src, family), 'sport': sport,
                'daddr': _addr(dst, family), 'dport': dport,
                'rqueue': rqueue, 'wqueue': wqueue,
                'uid': uid, 'inode': inode,
                'info': None,
            }
            # optional rtattrs
            a = t + _MSG_TAIL.size
            while a + _RTATTR.size <= end:
                rta_len, rta_type = _RTATTR.unpack_from(blob, a)
                if rta_len < _RTATTR.size or a + rta_len > end:
                    break
                payload = blob[a + _RTATTR.size:a + rta_len]
                if rta_type == INET_DIAG_INFO:
                    rec['info'] = parse_tcp_info(payload)
                a += (rta_len + 3) & ~3
            out.append(rec)
        off += (mlen + 3) & ~3
    return out


def _request(family, protocol, states, ext):
    sockid = b'\x00' * _SOCKID_LEN
    body = _REQ.pack(family, protocol, ext, 0, states) + sockid
    hdr = _NLMSGHDR.pack(_NLMSGHDR.size + len(body), SOCK_DIAG_BY_FAMILY,
                         NLM_F_REQUEST | NLM_F_DUMP, 1, 0)
    return hdr + body


def dump(families=(socket.AF_INET, socket.AF_INET6),
         protocols=(socket.IPPROTO_TCP, socket.IPPROTO_UDP),
         states=ALL_STATES, want_info=True, timeout=5.0):
    """Every socket in `families` x `protocols`, as dicts from parse_messages().

    Raises OSError if the netlink socket cannot be opened, which is the caller's
    signal to fall through to the next method in the ladder."""
    ext = EXT_INFO_BIT if want_info else 0
    out = []
    s = socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, NETLINK_SOCK_DIAG)
    try:
        s.settimeout(timeout)
        for fam in families:
            for proto in protocols:
                try:
                    s.send(_request(fam, proto, states, ext))
                except OSError:
                    continue
                while True:
                    try:
                        buf = s.recv(1 << 18)
                    except (socket.timeout, OSError):
                        break
                    if not buf:
                        break
                    batch = parse_messages(buf)
                    out.extend(batch)
                    # a dump ends with NLMSG_DONE, which parse_messages stops on;
                    # detect it directly so we do not block for the timeout
                    if _has_done(buf):
                        break
    finally:
        s.close()
    return out


def _has_done(blob):
    off = 0
    n = len(blob)
    while off + _NLMSGHDR.size <= n:
        mlen, mtype, _f, _s, _p = _NLMSGHDR.unpack_from(blob, off)
        if mlen < _NLMSGHDR.size:
            return True
        if mtype in (NLMSG_DONE, NLMSG_ERROR):
            return True
        off += (mlen + 3) & ~3
    return False


def key_of(rec):
    """Four-tuple join key, matching the collector's BPF birth-map key."""
    return (rec['saddr'], rec['sport'], rec['daddr'], rec['dport'])


if __name__ == '__main__':
    import json
    rows = dump()
    print(json.dumps({'n': len(rows), 'sample': rows[:3]}, indent=1))
