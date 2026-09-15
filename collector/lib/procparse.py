"""Shared /proc + small parsing primitives for the login-node collectors.

VENDORED COPY — see ../VENDORED.md. Upstream is collect/common/procparse.py; keep the two in sync.
`ebpfm.sh check` diffs them whenever it can see the repo.

A dependency-free toolkit for reading per-process state straight from /proc,
factored out of the agent collectors (`../agents/proc_trace.py` reads /proc at
~1 Hz; `../agents/agent_census.py` uses the scalar/comm helpers). Import it from
a path-invoked collector via the usual `sys.path.insert(.../common)` shim.

All readers tolerate a pid disappearing mid-read (return None / skip). The fields
exposed here — stat (utime/stime/cutime/cstime/majflt/starttime/threads/state/
ppid), schedstat (on-CPU vs run-queue-wait ns), cgroup path, status memory
(VmHWM/VmRSS) and cmdline — are world-readable, so they resolve for every user's
processes; only /proc/<pid>/io is ptrace-gated and yields None for other users
when unprivileged (see ./limitations.md).

Note: `./snapshot.py` predates this module and stays standalone on purpose — it
is the import-free node probe re-exec'd every interval by the live login/compute
monitors, and its /proc readers serve a different schema (raw IO bytes + blkio,
kstack), so they are not folded in here.
"""
import os
import pwd
from functools import lru_cache

CLK_TCK = os.sysconf('SC_CLK_TCK') or 100


# ---------------------------------------------------------------------------
# scalar / string helpers
# ---------------------------------------------------------------------------

def to_int(s):
    try:    return int(s)
    except (TypeError, ValueError): return None


def to_float(s):
    try:    return float(s)
    except (TypeError, ValueError): return None


def env_num(name, default, cast):
    """Numeric env var with a typed fallback, e.g. env_num('X', 1.0, float)."""
    try:    return cast(os.environ.get(name, default))
    except (TypeError, ValueError): return cast(default)


def comm_of(args):
    """arg0 basename of a command line ('[kthread]' style names kept verbatim)."""
    tok = args.split(None, 1)[0] if args else ''
    if not tok:
        return ''
    return tok if tok.startswith('[') else tok.rsplit('/', 1)[-1]


@lru_cache(maxsize=None)
def username(uid):
    """User name for a uid (cached), falling back to the numeric uid as a string."""
    try:    return pwd.getpwuid(uid).pw_name
    except Exception: return str(uid)


# ---------------------------------------------------------------------------
# /proc readers
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def boot_time():
    """Boot time as a unix epoch (from /proc/stat `btime`), or None."""
    try:
        with open('/proc/stat') as f:
            for line in f:
                if line.startswith('btime'):
                    return int(line.split()[1])
    except Exception:
        pass
    return None


def start_epoch(starttime_ticks):
    """Process start time as a unix epoch from stat's field-22 starttime ticks."""
    bt = boot_time()
    return None if bt is None else bt + starttime_ticks / CLK_TCK


def list_pids():
    """Set of live numeric pids under /proc."""
    return {int(d) for d in os.listdir('/proc') if d.isdigit()}


def read_stat(pid):
    """Parse /proc/<pid>/stat -> dict(ppid, pgrp, sid, state, tty_nr, min_flt,
    maj_flt, cmaj_flt, cpu_ticks, child_ticks, threads, starttime, comm), or None.
    cpu_ticks is utime+stime; child_ticks is cutime+cstime (CPU of already-reaped
    children — recovers the sub-poll blind spot of a sampling tracer); min_flt /
    maj_flt / cmaj_flt are minor / major page faults (self, and reaped children for
    cmaj); starttime is in clock ticks since boot (pair with start_epoch); tty_nr
    is the controlling terminal (0 = none -> headless; decode with tty_name).
    pgrp (field 5) and sid (field 6, the session id) let callers reconstruct a
    shell pipeline — every member of `cmd1 | cmd2 | cmd3` shares one pgrp, and an
    interactive login session shares one sid — which is otherwise invisible to a
    per-process tracer. The comm is taken from between the parentheses, so
    spaces/parens in the name don't shift the positional fields."""
    try:
        with open('/proc/%d/stat' % pid) as f:
            data = f.read()
    except Exception:
        return None
    try:
        rpar = data.rindex(')')
        comm = data[data.index('(') + 1:rpar]
        rest = data[rpar + 2:].split()
        # rest[0] = field 3 (state); field N -> rest[N-3]
        return {
            'ppid':        int(rest[1]),
            'pgrp':        int(rest[2]),                    # field 5: process group (pipeline key)
            'sid':         int(rest[3]),                    # field 6: session id (login-session key)
            'state':       rest[0],
            'tty_nr':      int(rest[4]),                    # field 7: controlling tty
            'min_flt':     int(rest[6]),                    # field 10: minflt (self)
            'maj_flt':     int(rest[9]),                    # field 12: majflt (self)
            'cmaj_flt':    int(rest[10]),                   # field 13: cmajflt (reaped children)
            'cpu_ticks':   int(rest[11]) + int(rest[12]),   # utime + stime
            'child_ticks': int(rest[13]) + int(rest[14]),   # cutime + cstime (reaped children)
            'threads':     int(rest[17]),
            'starttime':   int(rest[19]),
            'comm':        comm,
        }
    except (ValueError, IndexError):
        return None


def tty_name(tty_nr):
    """Decode a /proc stat tty_nr to a device name ('pts/3', 'tty1', ...), or None
    when the process has no controlling terminal (tty_nr == 0). A controlling tty
    is the signal of an interactive login session (vs a headless daemon/agent)."""
    if not tty_nr:
        return None
    major = (tty_nr >> 8) & 0xfff
    minor = (tty_nr & 0xff) | ((tty_nr >> 20) << 8)
    if major == 136:            # UNIX98 pty slaves -> pts/N
        return 'pts/%d' % minor
    if major == 4:              # virtual consoles / serial -> ttyN
        return 'tty%d' % minor
    return '%d:%d' % (major, minor)


def read_status_mem(pid):
    """Peak resident set (VmHWM) in kB, falling back to VmRSS; None if no mm."""
    try:
        with open('/proc/%d/status' % pid) as f:
            rss = None
            for line in f:
                if line.startswith('VmHWM:'):
                    return int(line.split()[1])
                if line.startswith('VmRSS:'):
                    rss = int(line.split()[1])
                elif line.startswith('VmSwap:'):
                    break
            return rss
    except Exception:
        return None


def read_cmdline(pid, comm=None):
    """Full command line with NULs turned to spaces; falls back to '[comm]' for
    kernel threads / zombies with an empty cmdline."""
    try:
        with open('/proc/%d/cmdline' % pid) as f:
            cmd = f.read().replace('\0', ' ').strip()
        if cmd:
            return cmd
    except Exception:
        pass
    return '[%s]' % comm if comm else ''


def read_io(pid):
    """Cumulative IO counters for a pid, or None if unreadable (other users'
    /proc/<pid>/io is ptrace-gated when unprivileged). Raw bytes; callers format
    as needed. rd/wr/ccwr are storage-layer bytes (read_bytes / write_bytes /
    cancelled_write_bytes); rchar/wchar are syscall-layer bytes and additionally
    count page-cache and pipe/socket traffic — on NFS-heavy login nodes the
    rchar−rd gap is the cache-served share."""
    try:
        out = {'rd': 0, 'wr': 0, 'ccwr': 0, 'rchar': 0, 'wchar': 0}
        with open('/proc/%d/io' % pid) as f:
            for line in f:
                if line.startswith('read_bytes:'):
                    out['rd'] = int(line.split()[1])
                elif line.startswith('write_bytes:'):
                    out['wr'] = int(line.split()[1])
                elif line.startswith('cancelled_write_bytes:'):
                    out['ccwr'] = int(line.split()[1])
                elif line.startswith('rchar:'):
                    out['rchar'] = int(line.split()[1])
                elif line.startswith('wchar:'):
                    out['wchar'] = int(line.split()[1])
        return out
    except Exception:
        return None


def read_schedstat(pid):
    """Parse /proc/<pid>/schedstat -> dict(run_ns, wait_ns, slices), or None.
    run_ns = cumulative time actually on-CPU; wait_ns = cumulative time runnable
    but waiting for a CPU (run-queue delay = contention as felt by the process);
    slices = number of timeslices run. World-readable for all users."""
    try:
        with open('/proc/%d/schedstat' % pid) as f:
            p = f.read().split()
        return {'run_ns': int(p[0]), 'wait_ns': int(p[1]), 'slices': int(p[2])}
    except Exception:
        return None


def read_cgroup(pid):
    """Cgroup path of a pid ('/user.slice/user-1234.slice/session-56.scope'), or
    None. Prefers the cgroup-v2 unified line ('0::<path>'); falls back to the
    first v1 line's path. World-readable for all users."""
    try:
        with open('/proc/%d/cgroup' % pid) as f:
            first = None
            for line in f:
                parts = line.rstrip('\n').split(':', 2)
                if len(parts) != 3:
                    continue
                if first is None:
                    first = parts[2]
                if parts[0] == '0' and parts[1] == '':
                    return parts[2]
            return first
    except Exception:
        return None


def uid_of(pid):
    """Owning uid of a pid via /proc/<pid> ownership, or None."""
    try:    return os.stat('/proc/%d' % pid).st_uid
    except Exception: return None


_NS_KINDS = ('user', 'mnt', 'pid', 'net')


def read_ns(pid, kinds=_NS_KINDS):
    """Namespace inode strings for a pid, e.g. {'mnt': 'mnt:[4026531841]', ...}.

    READLINK ONLY, for the same reason as read_cwd: /proc/<pid>/ns/* are magic
    symlinks resolved out of the task's nsproxy, so os.readlink touches no
    filesystem and cannot block. os.stat() on them for st_ino WOULD be the
    resolving form this collector forbids everywhere else -- do not "optimise"
    this into a stat.

    A kind that cannot be read is ABSENT from the result rather than None, so the
    caller can tell "not isolated" from "not readable": ptrace-gates these for
    other users when unprivileged, and mislabelling that as "same as init" would
    understate the sandbox rate for every user but the collector's owner."""
    out = {}
    for k in kinds:
        try:
            out[k] = os.readlink('/proc/%d/ns/%s' % (pid, k))
        except Exception:
            pass
    return out


def read_confinement(pid):
    """Seccomp / no_new_privs / capability state from /proc/<pid>/status.

    Returns None if the file is unreadable. `nspid_depth` comes from NStgid,
    whose field count is the pid-namespace nesting depth -- a free pid-namespace
    signal that needs no readlink.

    Note what is NOT decided here: a bare capability set is not evidence of
    confinement. An ordinary unprivileged process has CapEff=0 by definition, and
    a process that is uid 0 inside a user namespace reports a FULL set. The
    caller must compare CapBnd against init's, and even then only as detail."""
    try:
        out = {'seccomp': None, 'seccomp_filters': None, 'nnp': None,
               'cap_eff': None, 'cap_bnd': None, 'nspid_depth': None}
        with open('/proc/%d/status' % pid) as f:
            for line in f:
                if line.startswith('Seccomp:'):
                    out['seccomp'] = int(line.split()[1])
                elif line.startswith('Seccomp_filters:'):
                    out['seccomp_filters'] = int(line.split()[1])
                elif line.startswith('NoNewPrivs:'):
                    out['nnp'] = int(line.split()[1])
                elif line.startswith('CapEff:'):
                    out['cap_eff'] = line.split()[1]
                elif line.startswith('CapBnd:'):
                    out['cap_bnd'] = line.split()[1]
                elif line.startswith('NStgid:'):
                    out['nspid_depth'] = len(line.split()) - 1
        return out
    except Exception:
        return None


def read_environ_names(pid, prefixes):
    """NAMES ONLY of environment variables matching any of `prefixes`.

    Values are never returned. Agent environments hold API keys, and the whole
    point of an allow-list of NAME prefixes rather than a deny-list of values is
    that a new secret-bearing variable is excluded by default.

    Same NUL-separated format as read_cmdline. ptrace-gated like /proc/<pid>/io,
    so it resolves for other users only as root. Sorted for stable output."""
    try:
        with open('/proc/%d/environ' % pid, 'rb') as f:
            raw = f.read(65536)
    except Exception:
        return None
    names = set()
    for item in raw.split(b'\x00'):
        if not item or b'=' not in item:
            continue
        name = item.split(b'=', 1)[0].decode('utf-8', 'replace')
        if any(name.startswith(pfx) for pfx in prefixes):
            names.add(name)
    return sorted(names) or None


def boot_id():
    """The kernel's boot UUID, or None.

    Constant for the life of a boot and distinct across boots, which is what
    makes it the right prefix for an id that must be stable across a collector
    restart but must NOT collide with the same pid on a later boot."""
    try:
        with open('/proc/sys/kernel/random/boot_id') as f:
            return f.read().strip() or None
    except Exception:
        return None


def read_cwd(pid):
    """Current working directory of a pid as a STRING, or None.

    READLINK ONLY — never stat, never realpath, never list. `os.readlink` asks the
    kernel to render the path out of the task's `fs_struct` and touches no
    filesystem. Anything that *resolves* the link (`os.stat`, `os.path.exists`,
    `os.path.realpath`, listing the directory) follows it onto the target mount,
    which on a wedged autofs/NFS mount blocks in uninterruptible sleep and would
    hang a single-threaded collector during the very incident it exists to
    observe. `snapshot.py` obeys the same rule when it counts /proc/<pid>/fd with
    `listdir`, which does not dereference the entries.

    Two caveats travel with the string: a deleted directory comes back with a
    " (deleted)" suffix, and a process inside a mount namespace (a `bwrap`
    sandbox) reports the path as resolved *there*, so it need not exist outside.

    Own-user only when unprivileged (ptrace-gated); resolves for every user as
    root, which is the point of running this collector privileged."""
    try:
        return os.readlink('/proc/%d/cwd' % pid)
    except Exception:
        return None
