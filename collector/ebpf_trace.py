#!/usr/bin/env python3
"""ebpfm — root eBPF collector for coding-agent activity on a login node.

Standalone: everything it imports lives in ./lib, so the folder can be copied to a
machine on its own. Invoke it through ./ebpfm.sh, which bootstraps dependencies,
checks the host and supervises the daemon.

WHY eBPF, precisely. The unprivileged /proc poller it replaces is *quota-bound*,
not design-bound: every process a user runs on a login node shares one cgroup v2
slice capped at cpu.max = 1.0 CPU, one full /proc scan costs ~281 ms, so the
achievable rate is ~2-5 Hz and the capture floor is about twice the interval.
Sampling density is anti-correlated with load — the busier the node, the more pids
to scan, the slower the loop, the wider the blind spot. eBPF breaks that by an
asymmetry: a probe's execution is charged to the task that triggered the event,
not to us, so exhaustive capture is drawn from nobody's poll budget and has no
sampling floor.

Two consequences shape the design:
  * Event-driven for anything that HAPPENS; sampled only for anything that
    PERSISTS. Every process exit is a kernel event. Only the residency view
    samples, which is inherent to asking what is alive right now.
  * Keep userspace syscalls off the per-event path. This is why cwd inherits from
    the parent by default instead of reading /proc on every exec.
  * The userspace drain IS cgroup-charged, and `sudo` does NOT leave the capped
    slice — `ebpfm.sh start` launches into system.slice for that reason.

Eight data points this adds over the previous collector, all of which an
unprivileged poller either cannot see or sees worse:
  1. cwd / work_dir           readlink /proc/<pid>/cwd, inherit-first
  2. D-state stall time       sum_block_runtime, or sched_stat_blocked accumulated
  3. standing connections     netlink socket-diag joined to the BPF birth map
  4. wait channel             /proc/<pid>/stack for processes in D at the tick
  5. flush at stop            event="truncated" with drained kernel counters
  6. unattributed processes   uid-based attribution (opt-in) + a dropped bucket
  7. per-agent-type totals    by_agent_type in residency_totals
  8. inbound connections      kretprobe inet_csk_accept
plus all-protocol per-process network bytes (tcp/udp send+recv retprobes), which
is what makes QUIC visible, with the endpoint each byte went to (schema 7), and
connect latency / failed-connect detection free from the existing socket
tracepoint.

Privilege: root, or CAP_BPF + CAP_PERFMON + CAP_SYS_PTRACE (CAP_SYS_ADMIN < 5.8).
Needs python3-bcc, and kernel.sched_schedstats=1 plus kernel.task_delayacct=1 or
two of the eight are inert. `ebpfm.sh bootstrap` arranges all of that.

Usage (prefer ./ebpfm.sh):
    sudo python3 ebpf_trace.py [hostname]        # loop -> daily log
    sudo EBPFM_DURATION=30 python3 ebpf_trace.py # 30 s then exit
    sudo python3 ebpf_trace.py --features        # probe, print the resolved set, exit
    sudo python3 ebpf_trace.py --dump-c          # print the BPF C actually compiled

Output: $EBPFM_OUTPUT_DIR/<YYYY-MM-DD>/<hostname>.<stream>.jsonl, one JSON object
per line, `event` in {meta, exit, truncated, conn, netio, submit, residency,
residency_totals, stop} (v6 folded the former `tcp`/`accept` kinds into `conns`
on the owning exit record; v7 added `netio`). Two files per host per day:

    <hostname>.exits.jsonl      exit, truncated          (per-process, unbounded)
    <hostname>.snapshot.jsonl   everything else          (census + run bookends)

stream_of() is the single mapping, and the split is a filing decision, not a
schema one: a reader that globs the day directory sees exactly what one file
used to give it. Schema in README.md.
"""
import bisect
import collections
import ctypes
import hashlib
import ipaddress
import itertools
import json
import os
import queue
import re
import signal
import socket
import sys
import threading
import time
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, 'lib'))
from agent_classify import (classify, actor3, is_autonomous, SANDBOX_TYPES,
                            approval_mode_of)  # noqa: E402
from procparse import (  # noqa: E402
    boot_time, list_pids, read_stat, read_cmdline, read_cgroup, read_cwd,
    tty_name, uid_of, username, start_epoch, env_num, boot_id, read_ns, read_confinement, read_io)

from slurm_args import parse_submit_args  # noqa: E402
from redact import redact_argv, redact_line, needs_argv  # noqa: E402

try:  # provider buckets for the network records (absent -> provider=null)
    from provider_cidrs import load_index as _load_cidr_index
except Exception:  # pragma: no cover
    _load_cidr_index = None
try:  # netlink socket-diag for standing connections
    import sockdiag
except Exception:  # pragma: no cover
    sockdiag = None

# This process's pid, stamped on every record (see _envelope). Read once: the
# tracer never forks itself -- ebpfm.sh daemonizes before Python starts -- and a
# getpid() per record would put a syscall on the per-event path.
COLLECTOR_PID = os.getpid()
SCHEMA_VERSION = 8      # 8 = exit counters cover every thread (thread_acc), `args` and
                        #     `captured_tail` masked (credentials, agent prompts) with
                        #     `args_redacted` saying what, kernel-socket (NFS) traffic out
                        #     of net_* and endpoints (`netpeer_kernel`), cgroups from file
                        #     handles, fork ppid = the forking process, running loss and
                        #     error counters in residency_totals, meta.counters_scope,
                        #     meta.redact, meta.unsafe_dirs
                        # 7 = `net_endpoints` on exit/truncated, the `netio` series,
                        #     `proto` on conn, net_*_bytes now include IPv6 UDP, the
                        #     envelope trades `source`/`collector` (constants:
                        #     'ebpf', 'ebpf_marthen_new') for `collector_pid`,
                        #     `command` (whole) replaces `comm` (cut at 15) and its
                        #     by_comm/_comms keys, `cwd_source` and `env_flags` are
                        #     gone (no /proc/<pid>/environ read), and `args`
                        #     is no longer capped by default
                        # 6 = per-connection `tcp`/`accept` records folded into a `conns`
                        #     block on the owning process's exit/truncated record;
                        #     those two event kinds no longer exist
                        # 5 = ts_epoch envelope, sandbox_*, approval_*, session_uuid,
                        #     args_len/args_truncated, submit request fields,
                        #     residency top_procs/state_counts/tcp_open_ext, io on truncated
                        # 4 = cwd, dstate_*, net_*, conn/accept/truncated events, attribution

HOSTNAME = (sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith('-')
            else os.uname().nodename.split('.')[0])

# ---------------------------------------------------------------------------
# configuration (EBPFM_* env)
# ---------------------------------------------------------------------------
OUTPUT_DIR    = os.environ.get('EBPFM_OUTPUT_DIR', '/var/log/ebpfm')

# Output is sensitive: the eBPF tier records argv, cwd and all-user I/O across
# every user on the node, so a day-file can contain command-line secrets and
# private paths. Default to no access for "other" (dirs 0750, files 0640), and
# allow an admin to hand read access to a specific GROUP. All three are
# configurable: 0 modes mean "leave whatever the umask produced" (opt out).
def _env_mode(name, default):
    """Parse an octal mode like '0750'/'750'.

    Unset env var           -> `default` (the secure default; we tighten).
    Explicitly empty or '0' -> None (leave whatever the umask produced; opt out).
    Unparseable             -> `default` (fail safe toward tighter)."""
    if name not in os.environ:
        return default
    v = os.environ[name].strip()
    if v == '' or v == '0':
        return None
    try:
        return int(v, 8)
    except ValueError:
        return default
DIR_MODE      = _env_mode('EBPFM_DIR_MODE', 0o750)           # output dirs; None = leave umask default
FILE_MODE     = _env_mode('EBPFM_FILE_MODE', 0o640)          # day-files; None = leave umask default
OUTPUT_GROUP  = (os.environ.get('EBPFM_GROUP') or '').strip() or None  # chgrp target (name or gid); None = no chown

def _resolve_gid(group):
    """Resolve a group NAME or numeric gid string to an int gid, or None.

    Tries grp.getgrnam first, then falls back to `getent group`, which always
    goes through NSS/SSSD -- important on hosts where lab groups come from
    LDAP/SSSD rather than /etc/group and grp.getgrnam may miss them. A
    set-but-unresolvable group is a configuration error that would otherwise
    silently leave output root-owned, so it is announced on stderr (-> journal)
    rather than swallowed."""
    if not group:
        return None
    try:
        return int(group)
    except ValueError:
        pass
    try:
        import grp
        return grp.getgrnam(group).gr_gid
    except (KeyError, ImportError):
        pass
    # Fallback: getent consults the full NSS stack (files + sss/ldap). Parse the
    # gid field of `group:x:GID:members`.
    try:
        import subprocess
        out = subprocess.run(['getent', 'group', group],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             universal_newlines=True)
        if out.returncode == 0 and out.stdout:
            fields = out.stdout.strip().split(':')
            if len(fields) >= 3 and fields[2].isdigit():
                return int(fields[2])
    except Exception:
        pass
    sys.stderr.write(
        "ebpfm: EBPFM_GROUP=%r could not be resolved to a gid (tried getgrnam "
        "and `getent group`); output will keep its default group. Use a numeric "
        "gid, or confirm the group is visible to this host (getent group %s).\n"
        % (group, group))
    return None
OUTPUT_GID = _resolve_gid(OUTPUT_GROUP)

def _secure_path(path, mode):
    """Best-effort chmod + chgrp of `path` to the configured mode/group. Never
    fatal: a collector that cannot tighten perms must still collect. chgrp needs
    privilege the eBPF tier already has (root); it is a no-op when unset."""
    try:
        if mode is not None:
            os.chmod(path, mode)
    except OSError:
        pass
    if OUTPUT_GID is not None:
        try:
            os.chown(path, -1, OUTPUT_GID)   # -1 = leave owner, set group
        except OSError:
            pass

# --- log compression -------------------------------------------------------
# The day-files are unbounded (the exits stream can exceed 1 GB) and the only
# rotation is the daily rollover -- nothing shrinks a sealed day. gzip on this
# newline-delimited JSON is ~10-20x, so sealed days are compressed in place to
# <file>.jsonl.gz. Only SEALED days are touched; the live day's open files are
# never compressed. Default on; EBPFM_COMPRESS=0 disables.
COMPRESS      = os.environ.get('EBPFM_COMPRESS', '1') != '0'

def _compress_file(path):
    """gzip `path` -> `path`.gz out of band, then apply the mode/group policy to
    the result.

    Runs in a short-lived daemon thread so a 1 GB compress never blocks the
    drain, yet we can still chmod/chgrp the `.gz` once gzip finishes -- gzip
    preserves the SOURCE's group, which may be stale (e.g. the .jsonl was
    created before EBPFM_GROUP was set), so the policy is re-applied explicitly
    rather than trusted. Best-effort and crash-safe: gzip writes `path`.gz and
    unlinks the source only on success, so an interrupted run leaves the
    readable `.jsonl` to retry next time. Skips when a `.gz` already exists."""
    gz = path + '.gz'
    if not os.path.isfile(path) or os.path.exists(gz):
        return

    def _run():
        try:
            import subprocess
            rc = subprocess.call(['nice', '-n', '19', 'gzip', '-f', path],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if rc == 0:
                _secure_path(gz, FILE_MODE)   # enforce mode+group on the .gz
        except Exception:
            pass
    try:
        import threading
        threading.Thread(target=_run, daemon=True).start()
    except Exception:
        pass

def _compress_day(root, day, host):
    """Compress a sealed day's stream files. Called at rollover for the day that
    just closed, and by the startup sweep for any older sealed day."""
    if not COMPRESS or not day:
        return
    for s in STREAMS:
        _compress_file(os.path.join(root, day, '%s.%s.jsonl' % (host, s)))

def _sweep_sealed_days(root, host, keep_day):
    """At startup, gzip any sealed day left uncompressed by a prior run or crash.
    `keep_day` (today) is skipped -- its files are about to be reopened for
    appending and must stay plain."""
    if not COMPRESS or not os.path.isdir(root):
        return
    try:
        days = [d for d in os.listdir(root)
                if d != keep_day and os.path.isdir(os.path.join(root, d))]
    except OSError:
        return
    for d in days:
        _compress_day(root, d, host)

def _redact_kinds(value):
    """EBPFM_REDACT -> the kinds of text masked in what is written. Unset means
    both; '0', 'none' or empty means neither (an explicit opt-out)."""
    if value is None:
        return frozenset(('keys', 'prompts'))
    kinds = {k.strip().lower() for k in value.split(',') if k.strip()}
    if kinds & {'0', 'none', 'off'}:
        return frozenset()
    bad = kinds - {'keys', 'prompts'}
    if bad:
        sys.stderr.write('ebpfm: EBPFM_REDACT ignores unknown kind(s) %s (know: keys, prompts)\n'
                         % ', '.join(sorted(bad)))
    return frozenset(kinds & {'keys', 'prompts'})


# What is masked in every command line written out (`args`, a submit record's
# `captured_tail`): 'keys' are credentials -- API keys, tokens, passwords,
# private keys -- and 'prompts' the text handed to a coding agent on its command
# line. Both by default. The in-memory argv stays raw, because classification,
# the approval flags and the endpoint names are read from it. lib/redact.py.
REDACT        = _redact_kinds(os.environ.get('EBPFM_REDACT'))
REDACT_KEYS   = 'keys' in REDACT
REDACT_PROMPTS = 'prompts' in REDACT

DURATION      = env_num('EBPFM_DURATION', 0, float)          # 0 = forever
ARGS_MAXLEN   = env_num('EBPFM_ARGS_MAXLEN', 0, int)         # emitted argv cap; 0 = none (classification is always uncapped)
ARGV_KMAX     = env_num('EBPFM_ARGV_KMAX', 4096, int)        # in-kernel argv read (bytes): the real limit when uncapped
WRITE_KMAX    = env_num('EBPFM_WRITE_KMAX', 1024, int)       # per write() capture (bytes)
SUBMIT_BUDGET = env_num('EBPFM_SUBMIT_BUDGET', 8192, int)   # per submit tool, then capture stops
MIN_DURATION  = env_num('EBPFM_MIN_DURATION', 0.0, float)
MIN_CPU       = env_num('EBPFM_MIN_CPU', 0.0, float)
INCLUDE_ROOTS = os.environ.get('EBPFM_INCLUDE_ROOTS', '1') != '0'
KEEP_TIMERS   = os.environ.get('EBPFM_KEEP_TIMERS', '0') != '0'
TIMER_COMMS   = {c for c in (os.environ.get('EBPFM_TIMER_COMMS') or 'sleep,usleep').split(',') if c}
GRACE_S       = env_num('EBPFM_GC_GRACE', 300.0, float)
POLL_MS       = env_num('EBPFM_POLL_MS', 100, int)
PAGE_CNT      = env_num('EBPFM_PERF_PAGES', 256, int)        # perf ring pages per CPU
RESIDENCY_S   = env_num('EBPFM_RESIDENCY_S', 60.0, float)    # 0 disables the tick entirely
SUBMIT_COMMS  = {c for c in (os.environ.get('EBPFM_SUBMIT_COMMS') or 'sbatch,salloc,srun').split(',') if c}
FEATURES_ENV  = os.environ.get('EBPFM_FEATURES', 'auto')     # 'auto' | pinned comma list
FEATURE_CACHE = os.environ.get('EBPFM_FEATURE_CACHE', '1') != '0'
PROBE_VERBOSE = os.environ.get('EBPFM_PROBE_VERBOSE', '0') != '0'

# --- item 1: cwd -----------------------------------------------------------
WANT_CWD      = os.environ.get('EBPFM_CWD', '1') != '0'
CWD_ALWAYS    = os.environ.get('EBPFM_CWD_ALWAYS', '0') != '0'   # readlink on every exec (validation)

# --- sandbox / approval ----------------------------------------------------
WANT_SANDBOX  = os.environ.get('EBPFM_SANDBOX', '1') != '0'
SANDBOX_ALWAYS = os.environ.get('EBPFM_SANDBOX_ALWAYS', '0') != '0'  # read on every exec (validation)
# Launchers that create the namespaces. The PARENT's comm matters as much as the
# process's own: bwrap execs, THEN unshares, THEN execs the payload, so reading
# namespaces at bwrap's own exec returns the HOST's and -- under inherit-first --
# would propagate "unsandboxed" down the whole sandboxed tree. Matched on the full
# command (_command_of), not comm: codex-linux-sandbox is 19 characters, and the
# kernel's comm for it is only ever 'codex-linux-san'.
RESIDENCY_TOPN = env_num('EBPFM_RESIDENCY_TOPN', 5, int)   # heaviest procs per tree
SANDBOX_COMMS = {'bwrap', 'unshare', 'nsenter', 'podman', 'docker', 'runc', 'crun',
                 'conmon', 'containerd-shim', 'apptainer', 'singularity', 'proot',
                 'firejail', 'systemd-nspawn', 'codex-linux-sandbox'}
# --- item 3/8: network -----------------------------------------------------
TCP_ALL       = os.environ.get('EBPFM_TCP_ALL', '0') != '0'   # fold conns for untracked pids too
CONNS_TOPN    = env_num('EBPFM_CONNS_TOPN', 5, int)           # peers/providers kept per record
# How long an exit record is held before it is written, so that the socket
# closes belonging to the exiting process can still be folded into it. The
# kernel runs trace_sched_process_exit() BEFORE exit_files(), so a process's own
# TCP_CLOSE events are submitted AFTER its exit event -- emitting inline would
# drop every connection the process still held open, which for an agent is most
# of them. Two poll rounds is generous; 0 restores the old inline emit.
EXIT_HOLD_MS  = env_num('EBPFM_EXIT_HOLD_MS', 250, int)
# How far behind "now" (kernel time) the event queue is applied. Each CPU has
# its own perf ring and BCC drains them one after another, so a poll round
# hands events over by ring, not by time. Anything stamped this long before the
# round's poll began was already in its ring when the poll ran, so nothing
# older can still arrive. See dispatch_events().
REORDER_MS    = env_num('EBPFM_REORDER_MS', 20, int)
CONN_ALL      = os.environ.get('EBPFM_CONN_ALL', '0') != '0'  # 1 = emit loopback/private too
CONN_UNTRACKED = os.environ.get('EBPFM_CONN_UNTRACKED', '1') != '0'  # netlink rows with no pid
NETPEER_MAX   = env_num('EBPFM_NETPEER_MAX', 65536, int)      # (pid, endpoint) entries in the kernel table (~3 MB)
NET_ENDPOINTS_MAX = env_num('EBPFM_NET_ENDPOINTS_MAX', 256, int)  # endpoints listed per record
WANT_NETIO    = os.environ.get('EBPFM_NETIO', '1') != '0'      # the per-tick `netio` series
WANT_ARGV_HOSTS = os.environ.get('EBPFM_ARGV_HOSTS', '1') != '0'  # endpoint `host` from argv names


def _eph_range():
    """net.ipv4.ip_local_port_range, which covers IPv6 as well, or the kernel's
    default. Compiled into the endpoint probe: it is how a socket the collector
    did not see open is told inbound from outbound."""
    try:
        with open('/proc/sys/net/ipv4/ip_local_port_range') as f:
            lo, hi = (int(x) for x in f.read().split()[:2])
        if 0 < lo <= hi <= 65535:
            return lo, hi
    except Exception:
        pass
    return 32768, 60999


EPH_RANGE     = _eph_range()
# --- item 4: wait channel --------------------------------------------------
DSTACK_MAX    = env_num('EBPFM_DSTACK_MAX', 300, int)         # /proc/<pid>/stack reads per tick
DSTACK_DEPTH  = env_num('EBPFM_DSTACK_DEPTH', 5, int)
# --- item 5: flush ---------------------------------------------------------
FLUSH_ON_EXIT = os.environ.get('EBPFM_FLUSH', '1') != '0'
# NB: distinct from EBPFM_FLUSH above, which is about flushing live PROCESSES at
# stop. This one is the output file's write(2) policy.
#   'poll'   -- one flush per poll round (default): syscalls are capped at
#               1000/EBPFM_POLL_MS per second no matter the record rate, and a
#               record is at most one poll round short of durable, which is
#               already the collector's own latency floor.
#   'record' -- flush every record, the schema-5 behaviour.
FLUSH_MODE    = os.environ.get('EBPFM_FLUSH_MODE', 'poll')
# --- item 6: uid attribution ----------------------------------------------
# 0 disables. Enabling this widens what `actor3="human"` means, so a capture with
# it on is NOT comparable to one with it off; filter on `attribution` downstream.
MIN_UID       = env_num('EBPFM_MIN_UID', 0, int)

PAGE_SIZE = os.sysconf('SC_PAGE_SIZE') or 4096
CLK_TCK   = os.sysconf('SC_CLK_TCK') or 100
BTIME     = boot_time()


def _ktime_now():
    """Now on bpf_ktime_get_ns()'s clock (CLOCK_MONOTONIC), in ns.

    time.monotonic_ns() is 3.7+, and EL8 runs 3.6, where the old fallback was
    time.time() -- the WALL clock, which put every conn record's age_s about 56
    years off on those hosts."""
    if hasattr(time, 'monotonic_ns'):
        return time.monotonic_ns()
    return int(time.monotonic() * 1e9)

EVT_FORK, EVT_EXEC, EVT_EXIT = 1, 2, 3
TCP_KIND_CLOSE, TCP_KIND_ACCEPT = 1, 2
# `netp` key flags. build_net_c() compiles these same values into the probe.
NETP_F_INBOUND, NETP_F_V6, NETP_F_UNKNOWN = 1, 2, 4

_KSTACK_FRAME = re.compile(r'\]\s+(\S+?)\+0x')   # same extraction snapshot.py uses

# ---------------------------------------------------------------------------
# BPF C program, assembled from independently probed blocks
# ---------------------------------------------------------------------------
# Greedy probing: base + accepted + candidate must compile AND load, else the
# candidate alone is dropped and its compiler error is recorded. A block that
# needs another lists it in `requires`.

BLOCKS = [
    # name          requires        purpose
    ('fork_raw',    (),             'fork events for new PROCESSES only, from the raw tracepoint, which has the child task; threads never reach the ring'),
    ('signal',      (),             'whole-process CPU/threads/cutime/cmaj_flt via signal_struct'),
    ('io',          (),             'task->ioac read/write/rchar/wchar (+signal->ioac when signal)'),
    ('sched',       (),             'sched_info.run_delay (run-queue wait)'),
    ('delay',       (),             'task->delays: blkio/swapin/freepages dwell (CONFIG_TASK_DELAY_ACCT)'),
    ('dstate_task', (),             'item 2: cumulative uninterruptible sleep from task->stats.sum_block_runtime (5.19+)'),
    ('dstate_stat', (),             'item 2: uninterruptible sleep accumulated from sched:sched_stat_blocked (portable)'),
    # after the per-task blocks: what it adds up per thread depends on which loaded
    ('thread_acc',  ('signal',),    'whole-process exit counters: each thread adds its own as it exits, the last one writes the record'),
    ('pids_signal', ('signal',),    'pgrp/sid via signal->pids[] (kernel >= 4.19 layout)'),
    ('pids_task',   (),             'pgrp/sid via group_leader->pids[].pid (kernel <= 4.18 layout)'),
    ('pids_kread',  ('signal',),    'pgrp/sid via explicit bpf_probe_read of signal->pids[] (bypasses the BCC rewriter)'),
    ('tty',         ('signal',),    'controlling tty name via signal->tty'),
    ('cgid',        (),             'bpf_get_current_cgroup_id (cgroup v2 id, resolved to a path in userspace)'),
    ('argv_user',   (),             'in-kernel argv at exec via bpf_probe_read_user'),
    ('argv_kernel', (),             'in-kernel argv at exec via bpf_probe_read (pre-5.5 fallback)'),
    ('submit',      (),             'sbatch/salloc/srun stdout/stderr capture -> job id'),
    ('netbytes',    (),             'all-protocol per-process net bytes: kretprobes on tcp/udp send+recv (makes QUIC visible)'),
    ('netbytes_udp6', ('netbytes',), 'the same over IPv6 UDP: udpv6_sendmsg/udpv6_recvmsg, which udp_* never see'),
    ('netbytes_page', ('netbytes',), 'sendfile/splice over TCP: tcp_sendpage, which tcp_sendmsg never sees (gone in 6.5, which routes it through sendmsg)'),
    ('tcp_btf',     (),             'TCP lifetimes + bytes; tcp_sock offsets from kernel BTF (no net headers)'),
    ('tcp_hdr',     (),             'TCP lifetimes + bytes via <linux/tcp.h> (older kernels/BCC)'),
    ('tcp_basic',   (),             'TCP lifetimes without byte counters (no headers, no BTF)'),
    ('tcp_accept',  (),             'item 8: inbound connections via kretprobe inet_csk_accept (needs BTF)'),
    # after the tcp blocks: an endpoint's direction comes from their birth map
    # when it knows the socket, and the program text differs with and without it
    ('netpeer_btf', ('netbytes',),  'the endpoint of every counted send/recv; sock_common offsets from kernel BTF'),
    ('netpeer_hdr', ('netbytes',),  'the endpoint of every counted send/recv via <net/sock.h> (older kernels/BCC)'),
]
BLOCK_NAMES = [b[0] for b in BLOCKS]
TCP_BLOCKS   = {'tcp_btf', 'tcp_hdr', 'tcp_basic'}
PIDS_BLOCKS  = {'pids_signal', 'pids_task', 'pids_kread'}
DSTATE_BLOCKS = {'dstate_task', 'dstate_stat'}
NETPEER_BLOCKS = {'netpeer_btf', 'netpeer_hdr'}
# mutually exclusive alternatives: once the first loads, skip the rest
ALTERNATIVES = {'pids_task': 'pids_signal', 'pids_kread': 'pids_signal',
                'argv_kernel': 'argv_user', 'tcp_hdr': 'tcp_btf', 'tcp_basic': 'tcp_btf',
                'netpeer_hdr': 'netpeer_btf'}
ALSO_SUPERSEDED_BY = {'pids_kread': ('pids_task',), 'tcp_basic': ('tcp_hdr',)}
# dstate_task and dstate_stat are NOT alternatives: both load where possible so a
# capture can cross-check them, and `dstate_src` says which one a record used.

# ---------------------------------------------------------------------------
# BTF offsets: CO-RE by hand
# ---------------------------------------------------------------------------
# BCC 0.29 cannot compile <net/sock.h> against 6.x headers (the kernel's
# linux/bpf.h references UAPI symbols newer than BCC's bundled copy), so instead
# of struct access we read members at offsets taken from the running kernel's BTF.
BTF_WANT = {
    'tcp_sock':    ('bytes_received', 'bytes_acked'),
    'sock_common': ('skc_family', 'skc_rcv_saddr', 'skc_daddr', 'skc_num', 'skc_dport',
                    'skc_v6_rcv_saddr', 'skc_v6_daddr'),
    # optional: a socket with no file is the kernel's own (see _netp_stash)
    'sock':        ('sk_socket',),
    'socket':      ('file',),
}
BTF_REQUIRED = {
    'tcp_btf':    {'tcp_sock': ('bytes_received', 'bytes_acked')},
    'tcp_accept': {'sock_common': ('skc_family', 'skc_rcv_saddr', 'skc_daddr',
                                   'skc_num', 'skc_dport')},
    # skc_v6_daddr is optional, as it is for tcp_accept: without it an IPv6
    # socket's peer reads as unattributed rather than failing the block
    'netpeer_btf': {'sock_common': ('skc_family', 'skc_daddr', 'skc_num', 'skc_dport')},
}
BTF_OFFS = None          # {struct_name: {member: byte_offset}}


def _btf_member_type(m):
    """bpftool spells a member's type either 'type_id' or 'type'."""
    return m.get('type_id', m.get('type'))


# bpftool renders an unnamed member as the literal string "(anon)", not as an
# empty name. Treating that as a real member is exactly how the sock_common
# offsets went missing on the first run.
_BTF_ANON = ('', '(anon)')


def _btf_is_anon(name):
    return (name or '') in _BTF_ANON


def btf_collect(by_id, tid, want, base_bits, out, depth=0):
    """Offsets of `want` members under type `tid`, descending ANONYMOUS members.

    This descent is not optional. The members `tcp_accept` needs are not direct
    members of struct sock_common: skc_daddr and skc_rcv_saddr live inside the
    anonymous union around __addrpair, and skc_dport / skc_num inside the one
    around __portpair. BTF gives those wrappers an empty name, so a flat scan of
    `members` finds only skc_family and reports the struct as unusable."""
    t = by_id.get(tid)
    if t is None or depth > 8:
        return
    kind = t.get('kind')
    if kind in ('TYPEDEF', 'VOLATILE', 'CONST', 'RESTRICT'):
        btf_collect(by_id, _btf_member_type(t), want, base_bits, out, depth + 1)
        return
    if kind not in ('STRUCT', 'UNION'):
        return
    for m in t.get('members', ()):
        bits = base_bits + m.get('bits_offset', 0)
        nm = m.get('name') or ''
        if _btf_is_anon(nm):
            btf_collect(by_id, _btf_member_type(m), want, bits, out, depth + 1)
        elif nm in want:
            out[nm] = bits // 8


def btf_offsets():
    """{struct: {member: byte_offset}} from /sys/kernel/btf/vmlinux, or {}.

    `struct sock` begins with `struct sock_common __sk_common` at offset 0, so a
    sock_common member offset is usable directly against a `struct sock *`."""
    global BTF_OFFS
    if BTF_OFFS is not None:
        return BTF_OFFS
    import subprocess
    BTF_OFFS = {}
    if not os.path.exists('/sys/kernel/btf/vmlinux'):
        return BTF_OFFS
    try:
        # stdout/stderr, not capture_output: that keyword is Python 3.7+, and on
        # EL8's 3.6 the TypeError it raises was swallowed below, so every block
        # that needs BTF (tcp_accept among them) was silently refused there.
        out = subprocess.run(['bpftool', 'btf', 'dump', 'file', '/sys/kernel/btf/vmlinux', '-j'],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=300)
        if out.returncode != 0:
            return BTF_OFFS
        types = json.loads(out.stdout).get('types', [])
    except Exception:
        return BTF_OFFS
    by_id = {t['id']: t for t in types if 'id' in t}
    for t in types:
        if t.get('kind') != 'STRUCT':
            continue
        want = BTF_WANT.get(t.get('name'))
        if not want:
            continue
        offs = {}
        btf_collect(by_id, t.get('id'), set(want), 0, offs)
        if offs:
            BTF_OFFS.setdefault(t['name'], {}).update(offs)
    return BTF_OFFS


def btf_ok(block):
    """True if BTF supplied every member `block` needs."""
    need = BTF_REQUIRED.get(block)
    if not need:
        return True
    have = btf_offsets()
    return all(m in have.get(s, {}) for s, members in need.items() for m in members)


def build_tcp_common_c():
    """Declarations every TCP block shares. The birth map is written by the
    outbound tracepoint and the inbound accept retprobe, read from userspace each
    tick to describe connections that are still open (netlink supplies their
    stats; it has no pid), and read by the endpoint probe for direction."""
    return """
#define EBPFM_IPPROTO_TCP 6
#define EBPFM_TCP_ESTABLISHED 1
#define EBPFM_TCP_SYN_SENT 2
#define EBPFM_TCP_CLOSE 7
struct tcp_meta_t {
    u64 ts;           /* SYN_SENT, or accept() return for inbound */
    u64 estab_ts;     /* 0 until ESTABLISHED: a connect that never completed stays 0 */
    u32 pid, uid;
    u32 saddr_v4, daddr_v4;
    u16 sport, dport, family;
    u8  inbound, _pad;
    unsigned __int128 saddr_v6, daddr_v6;
    char comm[TASK_COMM_LEN];
};
struct tcp_t {
    u64 ts_ns, dur_ns, connect_ns, rx_bytes, tx_bytes;
    u32 pid, uid, saddr_v4, daddr_v4;
    u16 sport, dport, family;
    u8  has_bytes, kind, inbound, established;
    unsigned __int128 saddr_v6, daddr_v6;
    char comm[TASK_COMM_LEN];
};
BPF_HASH(tcp_birth, u64, struct tcp_meta_t, 65536);
BPF_PERF_OUTPUT(tcp_events);
"""


_NETB_C = """
struct netb_t { u64 tx; u64 rx; u64 calls; };
BPF_HASH(netb, u32, struct netb_t, 65536);

static __always_inline void _netb_add(s64 ret, int is_tx) {
    if (ret <= 0)
        return;
    u32 tgid = bpf_get_current_pid_tgid() >> 32;
    struct netb_t *p = netb.lookup(&tgid);
    if (!p) {
        struct netb_t z = {};
        netb.update(&tgid, &z);
        p = netb.lookup(&tgid);
        if (!p)
            return;
    }
    if (is_tx)
        __sync_fetch_and_add(&p->tx, (u64)ret);
    else
        __sync_fetch_and_add(&p->rx, (u64)ret);
    __sync_fetch_and_add(&p->calls, 1);
}
"""

_NETB_NEST_C = """
/* A dual-stack socket sending to an IPv4 peer goes udpv6_sendmsg -> udp_sendmsg
 * (a v4-mapped destination) on the same socket and thread, so both return probes
 * would count it. The outer one counts; the inner one is recognised by carrying
 * the socket the outer one entered with, and skipped. Keyed on that socket, not
 * a bare flag: a return probe that never ran (kretprobe maxactive) must not
 * silence every later udp_sendmsg on the thread, and a stale entry only matches
 * its own IPv6 socket, whose sends always go through udpv6_sendmsg anyway.
 * udpv6_recvmsg handles v4-mapped itself and never calls udp_recvmsg. */
struct netb_nest_t { u64 v6sk; u64 skip; };
BPF_HASH(netb_nest, u64, struct netb_nest_t, 16384);

static __always_inline void _netb_nest_enter(u64 sk) {
    u64 id = bpf_get_current_pid_tgid();
    struct netb_nest_t n = {};
    n.v6sk = sk;
    netb_nest.update(&id, &n);
}

static __always_inline void _netb_nest_leave(void) {
    u64 id = bpf_get_current_pid_tgid();
    netb_nest.delete(&id);
}

/* udp_sendmsg entry: is this the inner half of a udpv6_sendmsg? */
static __always_inline int _netb_nest_inner(u64 sk) {
    u64 id = bpf_get_current_pid_tgid();
    struct netb_nest_t *n = netb_nest.lookup(&id);
    if (!n)
        return 0;
    n->skip = (n->v6sk == sk);
    return n->skip ? 1 : 0;
}

/* udp_sendmsg return: consume the decision its entry made */
static __always_inline int _netb_nest_skip(void) {
    u64 id = bpf_get_current_pid_tgid();
    struct netb_nest_t *n = netb_nest.lookup(&id);
    if (!n || !n->skip)
        return 0;
    n->skip = 0;
    return 1;
}
"""

_NETPEER_C = """
/* ---- the endpoint of every counted send/recv --------------------------------
 * A return probe has lost its arguments, so an entry probe records which socket
 * and msghdr the call is on, per THREAD; the return probe then reads the peer and
 * adds the bytes under (process, protocol, direction, address, port). Each new
 * entry is announced once on `netp_new`, so userspace can keep an index of every
 * process's entries and read or delete just those -- a process's own entries at
 * its exit, the reported processes' at a tick -- where it used to read the whole
 * table, up to ten times a second. A live process's entries are never deleted:
 * that would race the increments below, and 4.18 has no atomic read-and-delete. */
#define NETP_TCP 6
#define NETP_UDP 17
#define NETP_F_INBOUND %(f_inbound)d
#define NETP_F_V6 %(f_v6)d
#define NETP_F_UNKNOWN %(f_unknown)d
#define NETP_EPH_LO %(eph_lo)d
#define NETP_EPH_HI %(eph_hi)d
struct netp_arg_t { u64 sk; u64 msg; u64 kern; };
struct netp_key_t { u32 tgid; u16 port; u8 proto; u8 flags; u8 addr[16]; };
struct netp_val_t { u64 tx; u64 rx; u64 calls; };
BPF_HASH(netp_args, u64, struct netp_arg_t, 16384);
BPF_HASH(netp, struct netp_key_t, struct netp_val_t, %(netp_max)d);
BPF_ARRAY(netp_drop, u64, 3);          /* calls, tx bytes, rx bytes that found netp full */
BPF_ARRAY(netp_kern, u64, 3);          /* calls, tx bytes, rx bytes on kernel sockets */
BPF_PERF_OUTPUT(netp_new);             /* one struct netp_key_t per entry created */

static __always_inline void _netp_stash(u64 sk, u64 msg) {
    u64 id = bpf_get_current_pid_tgid();
    struct netp_arg_t a = {};
    a.sk = sk;
    a.msg = msg;
__NETP_SOCK_KERN__
    netp_args.update(&id, &a);
}

/* The return probe's half: this thread's stash, consumed. 0 when there is none
 * (the call was already in flight when we attached, or its entry was lost). */
static __always_inline int _netp_take(struct netp_arg_t *out) {
    u64 id = bpf_get_current_pid_tgid();
    struct netp_arg_t *a = netp_args.lookup(&id);
    if (!a)
        return 0;
    out->sk = a->sk;
    out->msg = a->msg;
    out->kern = a->kern;
    netp_args.delete(&id);
    return 1;
}

static __always_inline void _netp_count(u32 slot0, s64 ret, int is_tx, int kern) {
    u32 i = slot0;
    u64 *c = kern ? netp_kern.lookup(&i) : netp_drop.lookup(&i);
    if (c)
        __sync_fetch_and_add(c, 1);
    i = is_tx ? 1 : 2;
    c = kern ? netp_kern.lookup(&i) : netp_drop.lookup(&i);
    if (c && ret > 0)
        __sync_fetch_and_add(c, (u64)ret);
}

static __always_inline void _netp_add(struct pt_regs *ctx, struct netp_arg_t *a, s64 ret,
                                      int is_tx, u8 proto) {
    u64 skp = a->sk, msgp = a->msg;
    if (ret <= 0 || !skp)
        return;
    u64 id = bpf_get_current_pid_tgid();
    struct netp_key_t k = {};
    k.tgid = id >> 32;
    k.proto = proto;
    u16 fam = 0, lport = 0, p = 0;
    int got = 0;
__NETP_SOCK_LOCAL__
    /* sendto()/recvfrom() on UDP: the peer is in msg->msg_name (the first member
     * of struct msghdr on 4.18 and 6.x alike), which the syscall layer has
     * already copied into kernel memory. TCP ignores msg_name. */
    if (proto == NETP_UDP && msgp) {
        u64 name = 0;
        bpf_probe_read(&name, sizeof(name), (void *)msgp);
        if (name) {
            u16 sf = 0;
            bpf_probe_read(&sf, sizeof(sf), (void *)name);
            bpf_probe_read(&p, sizeof(p), (void *)(name + 2));      /* sin(6)_port */
            if (sf == 2) {
                u32 a4 = 0;
                bpf_probe_read(&a4, sizeof(a4), (void *)(name + 4));
                __builtin_memcpy(k.addr, &a4, 4);
                got = a4 != 0;
            } else if (sf == 10) {
                u64 w[2] = {};
                bpf_probe_read(w, sizeof(w), (void *)(name + 8));
                __builtin_memcpy(k.addr, w, 16);
                k.flags = NETP_F_V6;
                got = (w[0] | w[1]) != 0;
            }
        }
    }
    if (!got) {                  /* the socket's own peer: TCP, connected UDP */
        k.flags = 0;
        __builtin_memset(k.addr, 0, sizeof(k.addr));
__NETP_SOCK_PEER__
    }
    if (!got) {
        /* no address anywhere, e.g. recv() on an unconnected UDP socket: its own
         * bucket, never dropped */
        k.flags = NETP_F_UNKNOWN;
        __builtin_memset(k.addr, 0, sizeof(k.addr));
    } else {
        u16 rport = (u16)((p >> 8) | (p << 8));
        int inbound = -1;
__NETP_BIRTH__
        /* Otherwise a guess from the ports: a remote end on an ephemeral port and
         * a local end off one is a client that connected in. */
        if (inbound < 0)
            inbound = rport >= NETP_EPH_LO && rport <= NETP_EPH_HI
                      && !(lport >= NETP_EPH_LO && lport <= NETP_EPH_HI);
        /* Inbound is keyed by the LOCAL port. The client's port is random, and
         * keying on it would make one entry per connection. */
        k.port = inbound ? lport : rport;
        if (inbound)
            k.flags |= NETP_F_INBOUND;
    }
    struct netp_val_t *v = netp.lookup(&k);
    if (!v) {
        struct netp_val_t z = {};
        /* insert, not update: of two threads making one entry, exactly one
         * creates it, so it is announced once and nobody resets its counts */
        if (netp.insert(&k, &z) == 0)
            netp_new.perf_submit(ctx, &k, sizeof(k));
        v = netp.lookup(&k);
        if (!v) {
            _netp_count(0, ret, is_tx, 0);  /* full: the per-process totals still have it */
            return;
        }
    }
    if (is_tx)
        __sync_fetch_and_add(&v->tx, (u64)ret);
    else
        __sync_fetch_and_add(&v->rx, (u64)ret);
    __sync_fetch_and_add(&v->calls, 1);
}
"""

_NETPEER_BIRTH_C = """
        /* The tcp blocks saw this socket open, so they know its direction. */
        if (proto == NETP_TCP) {
            struct tcp_meta_t *bm = tcp_birth.lookup(&skp);
            if (bm)
                inbound = bm->inbound ? 1 : 0;
        }
"""


def build_net_c(f, offs=None):
    """The network section of the BPF program: per-process byte totals
    (netbytes), IPv6 UDP (netbytes_udp6) and the endpoint table (netpeer_*).

    Kept apart from build_bpf_text so the unit tests can compile and drive
    exactly this section with the host compiler (tests/bpf_mock.h). It is the one
    part whose correctness rests on probes handing state to each other -- entry
    to return, udpv6_sendmsg to the udp_sendmsg it calls -- which a text check
    cannot show."""
    if 'netbytes' not in f:
        return ''
    O = offs if offs is not None else btf_offsets()
    udp6 = 'netbytes_udp6' in f
    peer = next((b for b in ('netpeer_btf', 'netpeer_hdr') if b in f), None)
    out = [_NETB_C]
    if udp6:
        out.append(_NETB_NEST_C)
    # A socket with no file behind it was made by the kernel for itself: the NFS
    # client's RPC transport above all, which sends from the CALLING process's
    # context, so every `ls` on an NFS path became network I/O of that process
    # -- 95% of the endpoint table on the login nodes, one entry per process per
    # NFS server, the table full most of the day. Those bytes are not the
    # process's own traffic (its reads arrive in softirq and were never counted);
    # they are counted apart, per node, in `netp_kern`.
    kern = ''
    if peer == 'netpeer_btf':
        so_off = O.get('sock', {}).get('sk_socket')
        file_off = O.get('socket', {}).get('file')
        if so_off is not None and file_off is not None:
            kern = """
    {
        u64 so = 0, file = 1;
        bpf_probe_read(&so, sizeof(so), (void *)(sk + %d));
        if (so && bpf_probe_read(&file, sizeof(file), (void *)(so + %d)) == 0 && !file)
            a.kern = 1;
    }
""" % (so_off, file_off)
    elif peer == 'netpeer_hdr':
        kern = """
    {
        struct sock *s_ = (struct sock *)sk;
        struct socket *so = 0;
        u64 file = 1;
        bpf_probe_read(&so, sizeof(so), &s_->sk_socket);
        if (so && bpf_probe_read(&file, sizeof(file), &so->file) == 0 && !file)
            a.kern = 1;
    }
"""
    if peer == 'netpeer_btf':
        sc = O.get('sock_common', {})
        local = """
    bpf_probe_read(&fam, sizeof(fam), (void *)(skp + %d));
    bpf_probe_read(&lport, sizeof(lport), (void *)(skp + %d));   /* skc_num: host order */
""" % (sc.get('skc_family', 0), sc.get('skc_num', 0))
        sock_peer = """
        bpf_probe_read(&p, sizeof(p), (void *)(skp + %d));          /* skc_dport: network order */
        if (fam == 2) {
            u32 a4 = 0;
            bpf_probe_read(&a4, sizeof(a4), (void *)(skp + %d));
            __builtin_memcpy(k.addr, &a4, 4);
            got = a4 != 0;
        }
""" % (sc.get('skc_dport', 0), sc.get('skc_daddr', 0))
        if 'skc_v6_daddr' in sc:
            sock_peer += """
        if (fam == 10) {
            u64 w[2] = {};
            bpf_probe_read(w, sizeof(w), (void *)(skp + %d));
            __builtin_memcpy(k.addr, w, 16);
            k.flags = NETP_F_V6;
            got = (w[0] | w[1]) != 0;
        }
""" % sc['skc_v6_daddr']
    elif peer == 'netpeer_hdr':
        local = """
    {
        struct sock *s_ = (struct sock *)skp;
        bpf_probe_read(&fam, sizeof(fam), &s_->__sk_common.skc_family);
        bpf_probe_read(&lport, sizeof(lport), &s_->__sk_common.skc_num);
    }
"""
        sock_peer = """
        {
            struct sock *s_ = (struct sock *)skp;
            bpf_probe_read(&p, sizeof(p), &s_->__sk_common.skc_dport);
            if (fam == 2) {
                u32 a4 = 0;
                bpf_probe_read(&a4, sizeof(a4), &s_->__sk_common.skc_daddr);
                __builtin_memcpy(k.addr, &a4, 4);
                got = a4 != 0;
            }
#if IS_ENABLED(CONFIG_IPV6)
            if (fam == 10) {
                u64 w[2] = {};
                bpf_probe_read(w, sizeof(w), &s_->__sk_common.skc_v6_daddr);
                __builtin_memcpy(k.addr, w, 16);
                k.flags = NETP_F_V6;
                got = (w[0] | w[1]) != 0;
            }
#endif
        }
"""
    if peer:
        birth = _NETPEER_BIRTH_C if (TCP_BLOCKS | {'tcp_accept'}) & f else ''
        out.append((_NETPEER_C % {'f_inbound': NETP_F_INBOUND, 'f_v6': NETP_F_V6,
                                  'f_unknown': NETP_F_UNKNOWN, 'eph_lo': EPH_RANGE[0],
                                  'eph_hi': EPH_RANGE[1], 'netp_max': NETPEER_MAX})
                   .replace('__NETP_SOCK_KERN__', kern)
                   .replace('__NETP_SOCK_LOCAL__', local)
                   .replace('__NETP_SOCK_PEER__', sock_peer)
                   .replace('__NETP_BIRTH__', birth))

    # kretprobes on the protocol entry points, NOT on sock_sendmsg: tcp_sendmsg
    # and udp_sendmsg take `struct sock *` and are inet by construction, so unix
    # sockets (all the MCP traffic) are excluded without reading any struct
    # member, and the return value is the byte count actually moved. udpv6_prot
    # has its own pair, so IPv6 UDP needs udpv6_* as well; TCP shares tcp_*.
    out.append("""
/* All of these return `int`, so the ABI only defines eax — the upper 32 bits of
 * rax are whatever was there before. Truncate to s32 and sign-extend, or that
 * garbage is read as data: a 6.7 KB download first measured as 21474843382
 * bytes, which is exactly 20 GiB plus the true 6902. */
""")
    fns = [('tcp_sendmsg', 1, 'NETP_TCP'), ('tcp_recvmsg', 0, 'NETP_TCP'),
           ('udp_sendmsg', 1, 'NETP_UDP'), ('udp_recvmsg', 0, 'NETP_UDP')]
    if udp6:
        fns += [('udpv6_sendmsg', 1, 'NETP_UDP'), ('udpv6_recvmsg', 0, 'NETP_UDP')]
    if 'netbytes_page' in f:
        fns += [('tcp_sendpage', 1, 'NETP_TCP')]
    for fn, is_tx, proto in fns:
        entry, pre = [], []
        if udp6 and fn == 'udpv6_sendmsg':
            entry.append('_netb_nest_enter(PT_REGS_PARM1(ctx));')
            pre.append('_netb_nest_leave();')
        elif udp6 and fn == 'udp_sendmsg':
            entry.append('if (_netb_nest_inner(PT_REGS_PARM1(ctx)))\n'
                         '        return 0;              /* counted by udpv6_sendmsg */')
            pre.append('if (_netb_nest_skip())\n        return 0;')
        if peer:
            # tcp_sendpage's second argument is a page, not a msghdr
            entry.append('_netp_stash(PT_REGS_PARM1(ctx), %s);'
                         % ('0' if fn == 'tcp_sendpage' else 'PT_REGS_PARM2(ctx)'))
        if entry:
            out.append('int kprobe__%s(struct pt_regs *ctx) {\n    %s\n    return 0;\n}\n'
                       % (fn, '\n    '.join(entry)))
        body = pre + ['s64 ret = (s64)(s32)PT_REGS_RC(ctx);']
        if peer:
            body += ['struct netp_arg_t a = {};',
                     'if (_netp_take(&a) && a.kern) {',
                     '    _netp_count(0, ret, %d, 1);   /* a kernel socket: not the process\'s I/O */'
                     % is_tx,
                     '    return 0;',
                     '}']
        body.append('_netb_add(ret, %d);' % is_tx)
        if peer:
            body.append('_netp_add(ctx, &a, ret, %d, %s);' % (is_tx, proto))
        out.append('int kretprobe__%s(struct pt_regs *ctx) {\n    %s\n    return 0;\n}\n'
                   % (fn, '\n    '.join(body)))
    return ''.join(out)


def build_bpf_text(f, offs=None):
    """BPF C source for feature set `f` (a set of BLOCK names).
    `offs` overrides the module-level BTF offsets (for unit tests)."""
    O = offs if offs is not None else btf_offsets()

    def off(s, m, d=0):
        return O.get(s, {}).get(m, d)

    # KBUILD_MODNAME must exist before any net/ header; BCC usually predefines it.
    inc = ['#ifndef KBUILD_MODNAME', '#define KBUILD_MODNAME "ebpfm"', '#endif',
           '#include <uapi/linux/ptrace.h>',
           '#include <linux/sched.h>', '#include <linux/mm_types.h>', '#include <linux/pid.h>']
    if 'signal' in f or 'tty' in f or 'pids_signal' in f:
        inc.append('#include <linux/sched/signal.h>')
    if 'tty' in f:
        inc.append('#include <linux/tty.h>')
    if 'delay' in f:
        inc.append('#include <linux/delayacct.h>')
    if 'tcp_hdr' in f:
        inc += ['#include <linux/tcp.h>', '#include <net/sock.h>']
    elif 'netpeer_hdr' in f and 'netbytes' in f:
        inc.append('#include <net/sock.h>')
    argv_on = ('argv_user' in f) or ('argv_kernel' in f)
    read_user = 'bpf_probe_read_user' if 'argv_user' in f else 'bpf_probe_read'

    # ---- exit-time reads --------------------------------------------------
    thread_acc = 'thread_acc' in f
    # With thread_acc the event carries this thread's own counters and
    # signal_struct's apart, and userspace adds the exited threads (see
    # _exit_totals); without it, the leader's own plus signal_struct's, summed.
    cpu_own = """
    d.utime_ns   = task->utime;
    d.stime_ns   = task->stime;
""" if thread_acc else """
    d.utime_ns   = task->utime + task->signal->utime;
    d.stime_ns   = task->stime + task->signal->stime;
"""
    cpu = (cpu_own + """    d.cutime_ns  = task->signal->cutime;
    d.cstime_ns  = task->signal->cstime;
    d.nr_threads = task->signal->nr_threads;
    d.cmaj_flt   = task->signal->cmaj_flt;
    /* Peak RSS, in pages. This MUST come off signal_struct, not mm: the kernel
       runs exit_mm() -- which sets tsk->mm = NULL -- before sched_process_exit
       fires, so a `task->mm` read is NULL at 100% of exits and the old
       mm->hiwater_rss guard never passed. Measured on 6.8: mm NULL 9/9, and
       signal->maxrss populated 9/9, matching getrusage's ru_maxrss exactly. */
    d.hiwater_rss_pages = task->signal->maxrss;
""" if 'signal' in f else """
    d.utime_ns   = task->utime;
    d.stime_ns   = task->stime;
""")
    if thread_acc:
        cpu += """    d.sig_utime   = task->signal->utime;
    d.sig_stime   = task->signal->stime;
    d.sig_run     = task->signal->sum_sched_runtime;
    d.sig_min_flt = task->signal->min_flt;
    d.sig_maj_flt = task->signal->maj_flt;
    d.sig_nvcsw   = task->signal->nvcsw;
    d.sig_nivcsw  = task->signal->nivcsw;
    d.has_acc     = thr_acc.lookup(&tgid) ? 1 : 0;
"""
    if 'io' in f and thread_acc:
        io = """
    d.rd_bytes  = (u64)task->ioac.read_bytes;
    d.wr_bytes  = (u64)task->ioac.write_bytes;
    d.rchar     = (u64)task->ioac.rchar;
    d.wchar     = (u64)task->ioac.wchar;
    d.sig_rd    = (u64)task->signal->ioac.read_bytes;
    d.sig_wr    = (u64)task->signal->ioac.write_bytes;
    d.sig_rchar = (u64)task->signal->ioac.rchar;
    d.sig_wchar = (u64)task->signal->ioac.wchar;
"""
    elif 'io' in f and 'signal' in f:
        io = """
    d.rd_bytes = (u64)task->ioac.read_bytes  + (u64)task->signal->ioac.read_bytes;
    d.wr_bytes = (u64)task->ioac.write_bytes + (u64)task->signal->ioac.write_bytes;
    d.rchar    = (u64)task->ioac.rchar + (u64)task->signal->ioac.rchar;
    d.wchar    = (u64)task->ioac.wchar + (u64)task->signal->ioac.wchar;
"""
    elif 'io' in f:
        io = """
    d.rd_bytes = (u64)task->ioac.read_bytes;
    d.wr_bytes = (u64)task->ioac.write_bytes;
    d.rchar    = (u64)task->ioac.rchar;
    d.wchar    = (u64)task->ioac.wchar;
"""
    else:
        io = ''
    sched = '    d.wait_ns = task->sched_info.run_delay;\n' if 'sched' in f else ''
    delay = ("""
    if (task->delays) {
        d.blkio_ns     = task->delays->blkio_delay;
        d.swapin_ns    = task->delays->swapin_delay;
        d.freepages_ns = task->delays->freepages_delay;
    }
""" if 'delay' in f else '')

    # item 2a: the kernel's own cumulative uninterruptible-sleep total.
    dstate_task = ("""
    d.block_ns = task->stats.sum_block_runtime;   /* 5.19+: sched_statistics moved onto task_struct */
    d.has_block = 1;
""" if 'dstate_task' in f else '')

    # item 2b: accumulate sched:sched_stat_blocked per tid. That tracepoint fires
    # only when a task WAKES from uninterruptible sleep, carrying the episode's
    # length, so it costs one hash op per unblock — orders of magnitude cheaper
    # than filtering sched_switch. Needs kernel.sched_schedstats=1.
    dstate_defs = ("""
struct dstate_t { u64 total; u64 max; u64 cnt; };
BPF_HASH(dstate_acc, u32, struct dstate_t, 65536);

TRACEPOINT_PROBE(sched, sched_stat_blocked) {
    u32 tid = args->pid;
    u64 delay = args->delay;
    struct dstate_t *p = dstate_acc.lookup(&tid);
    if (p) {
        __sync_fetch_and_add(&p->total, delay);
        __sync_fetch_and_add(&p->cnt, 1);
        if (delay > p->max)
            p->max = delay;             /* racy by a hair; a max never needs to be exact */
    } else {
        struct dstate_t z = {};
        z.total = delay; z.cnt = 1; z.max = delay;
        dstate_acc.update(&tid, &z);
    }
    return 0;
}
""" if 'dstate_stat' in f else '')
    dstate_read = ("""
    {
        struct dstate_t *p = dstate_acc.lookup(&tid);
        if (p) {
            d.dstate_ns  = p->total;
            d.dstate_max = p->max;
            d.dstate_cnt = p->cnt;
            d.has_dstate = 1;
        }
        dstate_acc.delete(&tid);
    }
""" if 'dstate_stat' in f else '')
    # keep the map bounded: a thread that never leads still gets its entry dropped
    dstate_del_thread = '        dstate_acc.delete(&tid);\n' if 'dstate_stat' in f else ''

    # ---- thread_acc: whole-process exit counters ----------------------------
    # See _exit_totals() for why. A thread that is not the last to exit adds its
    # own counters to its process's entry and returns; the first to do so also
    # notes signal_struct's totals as the base. Only counters this program reads
    # anyway are summed, so the struct follows the loaded blocks.
    own_x = {'utime': 'task->utime', 'stime': 'task->stime',
             'run': 'task->se.sum_exec_runtime', 'min_flt': 'task->min_flt',
             'maj_flt': 'task->maj_flt', 'nvcsw': 'task->nvcsw', 'nivcsw': 'task->nivcsw',
             'rd': '(u64)task->ioac.read_bytes', 'wr': '(u64)task->ioac.write_bytes',
             'rchar': '(u64)task->ioac.rchar', 'wchar': '(u64)task->ioac.wchar',
             'wait': 'task->sched_info.run_delay', 'block': 'task->stats.sum_block_runtime'}
    sig_x = {'utime': 'task->signal->utime', 'stime': 'task->signal->stime',
             'run': 'task->signal->sum_sched_runtime', 'min_flt': 'task->signal->min_flt',
             'maj_flt': 'task->signal->maj_flt', 'nvcsw': 'task->signal->nvcsw',
             'nivcsw': 'task->signal->nivcsw',
             'rd': '(u64)task->signal->ioac.read_bytes',
             'wr': '(u64)task->signal->ioac.write_bytes',
             'rchar': '(u64)task->signal->ioac.rchar', 'wchar': '(u64)task->signal->ioac.wchar'}
    acc_sig = ['utime', 'stime', 'run', 'min_flt', 'maj_flt', 'nvcsw', 'nivcsw']
    if 'io' in f:
        acc_sig += ['rd', 'wr', 'rchar', 'wchar']
    acc_own = (['wait'] if 'sched' in f else []) + (['block'] if 'dstate_task' in f else [])
    acc_fields = (['nthreads'] + ['b_' + x for x in acc_sig]
                  + ['s_' + x for x in acc_sig + acc_own]
                  + (['s_blkio', 's_swapin', 's_freepages'] if 'delay' in f else [])
                  + (['s_dstate', 's_dstate_max', 's_dstate_cnt'] if 'dstate_stat' in f else []))
    acc_defs = ("""
struct thr_acc_t { u64 %s; };
BPF_HASH(thr_acc, u32, struct thr_acc_t, 16384);
BPF_ARRAY(thr_acc_miss, u64, 1);       /* thread exits that found thr_acc full */
""" % ', '.join(acc_fields)) if thread_acc else ''
    if thread_acc:
        base = ''.join('            z.b_%s = %s;\n' % (x, sig_x[x]) for x in acc_sig)
        adds = ''.join('            __sync_fetch_and_add(&a->s_%s, %s);\n' % (x, own_x[x])
                       for x in acc_sig + acc_own)
        if 'delay' in f:
            adds += """            if (task->delays) {
                __sync_fetch_and_add(&a->s_blkio, task->delays->blkio_delay);
                __sync_fetch_and_add(&a->s_swapin, task->delays->swapin_delay);
                __sync_fetch_and_add(&a->s_freepages, task->delays->freepages_delay);
            }
"""
        if 'dstate_stat' in f:
            adds += """            {
                struct dstate_t *p = dstate_acc.lookup(&tid);
                if (p) {
                    __sync_fetch_and_add(&a->s_dstate, p->total);
                    __sync_fetch_and_add(&a->s_dstate_cnt, p->cnt);
                    if (p->max > a->s_dstate_max)
                        a->s_dstate_max = p->max;
                }
            }
"""
        exit_gate = """    /* Not the last thread out: add this one's counters to its process and
     * leave the record to whichever thread is (signal->live drops to 0 before
     * this tracepoint, for the thread that took it there). */
    if (task->signal->live.counter > 0) {
        struct thr_acc_t *a = thr_acc.lookup(&tgid);
        if (!a) {
            struct thr_acc_t z = {};
%s            thr_acc.insert(&tgid, &z);
            a = thr_acc.lookup(&tgid);
        }
        if (a) {
%s            u64 nt = task->signal->nr_threads;
            if (nt > a->nthreads)
                a->nthreads = nt;
        } else {
            u32 zero = 0;
            u64 *c = thr_acc_miss.lookup(&zero);
            if (c)
                __sync_fetch_and_add(c, 1);
        }
%s        return 0;
    }
""" % (base, adds, dstate_del_thread)
        # The last thread out may not be the leader; the process's own name, start
        # and status are the leader's (wait(2) reports the group exit code when
        # there was one, else the leader's own).
        exit_leader = """    if (tgid != tid) {
        struct task_struct *leader = task->group_leader;
        bpf_probe_read_str(&d.comm, sizeof(d.comm), leader->comm);
        d.start_ns  = task->group_leader->start_time;
        d.exit_code = task->signal->group_exit_code ? task->signal->group_exit_code
                                                    : task->group_leader->exit_code;
    }
"""
        data_acc = """    u64 sig_utime, sig_stime, sig_run, sig_min_flt, sig_maj_flt, sig_nvcsw, sig_nivcsw;
    u64 sig_rd, sig_wr, sig_rchar, sig_wchar;
    u8  has_acc;          // thr_acc held an entry for the process at this exit
"""
    else:
        exit_gate = """    if (tgid != tid) {
%s        return 0;   // thread-group leader only: one record per process
    }
""" % dstate_del_thread
        exit_leader = ''
        data_acc = ''
    # A probe's entry stash is per THREAD and its return probe deletes it, but a
    # return the kernel never ran (kretprobe maxactive) leaves it behind; once
    # the map filled, every new thread's sends went unattributed. A thread's exit
    # is the last chance to drop it.
    thread_del = ''
    if 'netbytes' in f and (NETPEER_BLOCKS & f):
        thread_del += '    netp_args.delete(&id);\n'
    if 'netbytes_udp6' in f and 'netbytes' in f:
        thread_del += '    netb_nest.delete(&id);\n'

    if 'pids_signal' in f:
        pids = """
    d.pgrp = task->signal->pids[PIDTYPE_PGID]->numbers[0].nr;
    d.sid  = task->signal->pids[PIDTYPE_SID]->numbers[0].nr;
"""
    elif 'pids_task' in f:
        pids = """
    d.pgrp = task->group_leader->pids[PIDTYPE_PGID].pid->numbers[0].nr;
    d.sid  = task->group_leader->pids[PIDTYPE_SID].pid->numbers[0].nr;
"""
    elif 'pids_kread' in f:
        # Same layout as pids_signal, with explicit reads, for BCC rewriters that
        # cannot follow `->pids[i]->numbers[0].nr` through a flexible array.
        pids = """
    {
        struct signal_struct *sig = task->signal;
        struct pid *pg = 0, *ss = 0;
        if (sig) {
            bpf_probe_read(&pg, sizeof(pg), &sig->pids[PIDTYPE_PGID]);
            bpf_probe_read(&ss, sizeof(ss), &sig->pids[PIDTYPE_SID]);
        }
        if (pg) bpf_probe_read(&d.pgrp, sizeof(d.pgrp), &pg->numbers[0].nr);
        if (ss) bpf_probe_read(&d.sid,  sizeof(d.sid),  &ss->numbers[0].nr);
    }
"""
    else:
        pids = ''
    tty = ("""
    {
        struct tty_struct *t = task->signal->tty;
        if (t) {
            d.has_tty = 1;
            bpf_probe_read_str(&d.tty, sizeof(d.tty), t->name);   /* old helper name: valid on 4.18 and 6.x */
        }
    }
""" if 'tty' in f else '')
    cgid = '    d.cgid = bpf_get_current_cgroup_id();\n' if 'cgid' in f else ''

    # ---- item 3b: all-protocol per-process network bytes -------------------
    netb_defs = build_net_c(f, O)
    netb_read = ("""
    {
        struct netb_t *p = netb.lookup(&tgid);
        if (p) {
            d.net_tx = p->tx;
            d.net_rx = p->rx;
            d.net_calls = p->calls;
            d.has_net = 1;
        }
        netb.delete(&tgid);
    }
""" if 'netbytes' in f else '')

    # ---- fork --------------------------------------------------------------
    # The parent is the PROCESS that forked: the tracepoint's parent_pid is the
    # forking THREAD's id, which for a child spawned off a worker thread (a
    # tokio runtime spawns from whichever one runs the task) named a tid the
    # table never tracks, and the child lost its ancestry.
    if 'fork_raw' in f:
        fork_probe = """
RAW_TRACEPOINT_PROBE(sched_process_fork) {
    /* (struct task_struct *parent, struct task_struct *child) */
    struct task_struct *child = (struct task_struct *)ctx->args[1];
    u32 pid = 0, tgid = 0;
    bpf_probe_read(&pid, sizeof(pid), &child->pid);
    bpf_probe_read(&tgid, sizeof(tgid), &child->tgid);
    if (!pid || pid != tgid)
        return 0;              /* a new thread: userspace tracks processes only */
    struct data_t d = {};
    d.type     = EVT_FORK;
    d.pid      = pid;
    d.ppid     = bpf_get_current_pid_tgid() >> 32;
    d.ktime_ns = bpf_ktime_get_ns();
    d.uid      = (u32)bpf_get_current_uid_gid();
    events.perf_submit(ctx, &d, sizeof(d));
    return 0;
}
"""
    else:
        fork_probe = """
TRACEPOINT_PROBE(sched, sched_process_fork) {
    struct data_t d = {};
    d.type     = EVT_FORK;
    d.pid      = args->child_pid;
    d.ppid     = bpf_get_current_pid_tgid() >> 32;
    d.ktime_ns = bpf_ktime_get_ns();
    d.uid      = (u32)bpf_get_current_uid_gid();
    events.perf_submit(args, &d, sizeof(d));
    return 0;
}
"""

    # ---- exec-time argv ----------------------------------------------------
    argv_defs = ("""
#define ARGV_MAX %d
struct argv_t { u32 pid; u32 len; u32 raw_len; u32 _pad; u64 ktime_ns; char buf[ARGV_MAX]; };
/* ktime_ns pairs it with the exec event. raw_len is arg_end-arg_start BEFORE
   the clamp below -- argv only, the environment is a separate range -- so
   userspace can tell a genuinely short command line from a truncated one.
   _pad is explicit: a bare third u32 before the u64 gets 4 bytes of padding
   anyway, and naming it keeps the ctypes and BPF views unambiguous. */
BPF_PERCPU_ARRAY(argv_scratch, struct argv_t, 1);
BPF_PERF_OUTPUT(argv_events);
""" % ARGV_KMAX) if argv_on else ''
    argv_exec = ("""
    {
        struct mm_struct *mm = task->mm;
        u32 zero = 0;
        struct argv_t *a = argv_scratch.lookup(&zero);
        if (mm && a) {
            unsigned long s = mm->arg_start, e = mm->arg_end;
            u32 raw = 0;
            if (e > s) raw = e - s;
            u32 len = raw;
            /* the clamp must stay the LAST write to len before the read below */
            if (len > ARGV_MAX) len = ARGV_MAX;
            a->pid = d.pid;
            a->len = len;
            a->raw_len = raw;
            a->ktime_ns = d.ktime_ns;
            if (len > 0 && %s(a->buf, len, (void *)s) == 0) {
                /* Only what was read: the whole 4 KB buffer went out with every
                 * exec, short command line or not, into a ring every CPU's
                 * execs share. Userspace reads buf[:len] and pads the rest. */
                u64 sz = (u64)len + __builtin_offsetof(struct argv_t, buf);
                if (sz > sizeof(*a))
                    sz = sizeof(*a);
                argv_events.perf_submit(args, a, sz);
            }
        }
    }
""" % read_user) if argv_on else ''

    # ---- submit capture ----------------------------------------------------
    def _comm_test(name):
        parts = ' && '.join("d.comm[%d] == '%s'" % (i, c) for i, c in enumerate(name))
        return '(%s && d.comm[%d] == 0)' % (parts, len(name))
    # A submit tool's stdout/stderr, for the job id it prints. Capture stops at a
    # byte budget, or sooner once userspace has the id (it deletes the pid): an
    # `srun --pty` session used to send every write of the whole session up the
    # ring for the 8 KB that was kept -- 8.4M events in 6.5 days on one node.
    submit_defs = ("""
#define WRITE_MAX %d
#define SUBMIT_BUDGET %d
struct write_t { u32 pid; u32 fd; u32 len; u32 _pad; u64 ktime_ns; char buf[WRITE_MAX]; };
BPF_HASH(submit_pids, u32, u32, 4096);      /* pid -> bytes still to capture */
BPF_PERCPU_ARRAY(write_scratch, struct write_t, 1);
BPF_PERF_OUTPUT(write_events);

TRACEPOINT_PROBE(syscalls, sys_enter_write) {
    u32 pid = bpf_get_current_pid_tgid() >> 32;
    u32 *left = submit_pids.lookup(&pid);
    if (!left)
        return 0;
    if (args->fd > 2)
        return 0;
    u32 zero = 0;
    struct write_t *w = write_scratch.lookup(&zero);
    if (!w)
        return 0;
    u32 len = (u32)args->count;
    if (len > WRITE_MAX) len = WRITE_MAX;
    u32 budget = *left;
    if (len >= budget) {
        len = budget;
        submit_pids.delete(&pid);           /* this write spends what is left */
    } else {
        *left = budget - len;
    }
    /* the clamp must stay the LAST write to len before the read below */
    if (len > WRITE_MAX) len = WRITE_MAX;
    w->pid = pid; w->fd = (u32)args->fd; w->len = len;
    w->ktime_ns = bpf_ktime_get_ns();
    if (len > 0 && %s(w->buf, len, (void *)args->buf) == 0)
        write_events.perf_submit(args, w, sizeof(*w));
    return 0;
}
""" % (WRITE_KMAX, SUBMIT_BUDGET, read_user)) if 'submit' in f else ''
    # an exec into anything else clears the pid: the flag used to outlive both
    # the submit tool's image and, until the pid's exit, its owner
    submit_exec = ("""
    if (%s) {
        u32 budget = SUBMIT_BUDGET;
        submit_pids.update(&d.pid, &budget);
    } else {
        submit_pids.delete(&d.pid);
    }
""" % ' || '.join(_comm_test(c) for c in sorted(SUBMIT_COMMS))) if 'submit' in f else ''
    submit_exit = '    submit_pids.delete(&tgid);\n' if 'submit' in f else ''

    # ---- TCP: lifetimes, connect latency, inbound accepts ------------------
    tcp_on = bool(TCP_BLOCKS & f)
    accept_on = 'tcp_accept' in f
    if 'tcp_btf' in f:
        tcp_bytes = """
    bpf_probe_read(&d.rx_bytes, sizeof(d.rx_bytes), (void *)(skp + %d));
    bpf_probe_read(&d.tx_bytes, sizeof(d.tx_bytes), (void *)(skp + %d));
    d.has_bytes = 1;
""" % (off('tcp_sock', 'bytes_received'), off('tcp_sock', 'bytes_acked'))
    elif 'tcp_hdr' in f:
        tcp_bytes = """
    {
        struct tcp_sock *tp = (struct tcp_sock *)args->skaddr;
        d.rx_bytes = tp->bytes_received;
        d.tx_bytes = tp->bytes_acked;
        d.has_bytes = 1;
    }
"""
    else:
        tcp_bytes = ''

    tcp_common_defs = build_tcp_common_c() if (tcp_on or accept_on) else ''

    tcp_tp = ("""
TRACEPOINT_PROBE(sock, inet_sock_set_state) {
    if (args->protocol != EBPFM_IPPROTO_TCP)
        return 0;
    u64 skp = (u64)args->skaddr;
    /* Active open: SYN_SENT is the only transition that runs in the connecting
     * process's own context (ESTABLISHED and CLOSE often land in softirq), so it
     * is the one chance to record the pid. The tuple is stored here as a
     * best-effort starting point, but sport is still 0 at this point (see the
     * ESTABLISHED branch) and gets corrected there. */
    if (args->newstate == EBPFM_TCP_SYN_SENT) {
        struct tcp_meta_t m = {};
        m.ts  = bpf_ktime_get_ns();
        m.pid = bpf_get_current_pid_tgid() >> 32;
        m.uid = (u32)bpf_get_current_uid_gid();
        m.sport = args->sport;
        m.dport = args->dport;
        m.family = args->family;
        __builtin_memcpy(&m.saddr_v4, args->saddr, 4);
        __builtin_memcpy(&m.daddr_v4, args->daddr, 4);
        __builtin_memcpy(&m.saddr_v6, args->saddr_v6, 16);
        __builtin_memcpy(&m.daddr_v6, args->daddr_v6, 16);
        bpf_get_current_comm(&m.comm, sizeof(m.comm));
        tcp_birth.update(&skp, &m);
        return 0;
    }
    if (args->newstate == EBPFM_TCP_ESTABLISHED) {
        struct tcp_meta_t *m = tcp_birth.lookup(&skp);
        if (m) {
            if (m->estab_ts == 0)
                m->estab_ts = bpf_ktime_get_ns();   /* connect latency */
            /* Refresh the tuple here, not at SYN_SENT. tcp_v4_connect calls
             * tcp_set_state(sk, TCP_SYN_SENT) BEFORE inet_hash_connect assigns
             * the ephemeral source port, so inet_sport — and therefore the
             * tracepoint's sport — is still 0 at SYN_SENT. Keying the userspace
             * netlink join on that zero makes every standing-flow lookup miss,
             * which is how rtt/rx/tx first came back null. */
            m->sport = args->sport;
            m->dport = args->dport;
            m->family = args->family;
            __builtin_memcpy(&m->saddr_v4, args->saddr, 4);
            __builtin_memcpy(&m->daddr_v4, args->daddr, 4);
            __builtin_memcpy(&m->saddr_v6, args->saddr_v6, 16);
            __builtin_memcpy(&m->daddr_v6, args->daddr_v6, 16);
        }
        return 0;
    }
    if (args->newstate != EBPFM_TCP_CLOSE)
        return 0;
    struct tcp_meta_t *m = tcp_birth.lookup(&skp);
    if (!m)
        return 0;   /* opened before we attached, and not an accept we saw */
    struct tcp_t d = {};
    d.kind    = 1;                  /* close */
    d.ts_ns   = bpf_ktime_get_ns();
    d.dur_ns  = d.ts_ns - m->ts;
    d.connect_ns = m->estab_ts ? (m->estab_ts - m->ts) : 0;
    d.established = m->estab_ts ? 1 : 0;
    d.inbound = m->inbound;
    d.pid     = m->pid;
    d.uid     = m->uid;
    d.sport   = args->sport;
    d.dport   = args->dport;
    d.family  = args->family;
    __builtin_memcpy(&d.saddr_v4, args->saddr, 4);
    __builtin_memcpy(&d.daddr_v4, args->daddr, 4);
    __builtin_memcpy(&d.saddr_v6, args->saddr_v6, 16);
    __builtin_memcpy(&d.daddr_v6, args->daddr_v6, 16);
    __builtin_memcpy(&d.comm, m->comm, sizeof(d.comm));
__TCP_BYTES__
    tcp_events.perf_submit(args, &d, sizeof(d));
    tcp_birth.delete(&skp);
    return 0;
}
""".replace('__TCP_BYTES__', tcp_bytes)) if tcp_on else ''

    # item 8. inet_csk_accept returns the new socket in the ACCEPTING process's
    # context; the inbound SYN_RECV->ESTABLISHED transition runs in softirq where
    # the current task is meaningless, which is why the tracepoint cannot do this.
    accept_probe = ("""
int kretprobe__inet_csk_accept(struct pt_regs *ctx) {
    u64 skp = (u64)PT_REGS_RC(ctx);
    if (!skp)
        return 0;
    u16 fam = 0;
    bpf_probe_read(&fam, sizeof(fam), (void *)(skp + %(f_family)d));
    if (fam != 2 && fam != 10)
        return 0;
    struct tcp_meta_t m = {};
    m.ts = bpf_ktime_get_ns();
    m.estab_ts = m.ts;                  /* an accepted socket is already established */
    m.pid = bpf_get_current_pid_tgid() >> 32;
    m.uid = (u32)bpf_get_current_uid_gid();
    m.inbound = 1;
    m.family = fam;
    bpf_get_current_comm(&m.comm, sizeof(m.comm));
    u16 num = 0, dp = 0;
    bpf_probe_read(&num, sizeof(num), (void *)(skp + %(f_num)d));    /* host order */
    bpf_probe_read(&dp,  sizeof(dp),  (void *)(skp + %(f_dport)d));  /* network order */
    m.sport = num;
    m.dport = (u16)((dp >> 8) | (dp << 8));
    if (fam == 2) {
        bpf_probe_read(&m.saddr_v4, 4, (void *)(skp + %(f_saddr)d));
        bpf_probe_read(&m.daddr_v4, 4, (void *)(skp + %(f_daddr)d));
    }
%(v6)s    tcp_birth.update(&skp, &m);
    struct tcp_t d = {};
    d.kind = 2;                         /* accept */
    d.ts_ns = m.ts;
    d.established = 1;
    d.inbound = 1;
    d.pid = m.pid; d.uid = m.uid;
    d.family = fam; d.sport = m.sport; d.dport = m.dport;
    d.saddr_v4 = m.saddr_v4; d.daddr_v4 = m.daddr_v4;
    d.saddr_v6 = m.saddr_v6; d.daddr_v6 = m.daddr_v6;
    __builtin_memcpy(&d.comm, m.comm, sizeof(d.comm));
    tcp_events.perf_submit(ctx, &d, sizeof(d));
    return 0;
}
""" % {'f_family': off('sock_common', 'skc_family'),
       'f_num':    off('sock_common', 'skc_num'),
       'f_dport':  off('sock_common', 'skc_dport'),
       'f_saddr':  off('sock_common', 'skc_rcv_saddr'),
       'f_daddr':  off('sock_common', 'skc_daddr'),
       'v6': ('''    else {
        bpf_probe_read(&m.saddr_v6, 16, (void *)(skp + %d));
        bpf_probe_read(&m.daddr_v6, 16, (void *)(skp + %d));
    }
''' % (off('sock_common', 'skc_v6_rcv_saddr'), off('sock_common', 'skc_v6_daddr')))
       if ('skc_v6_rcv_saddr' in O.get('sock_common', {})
           and 'skc_v6_daddr' in O.get('sock_common', {})) else ''}) if accept_on else ''

    text = r"""
__INC__

#define EVT_FORK 1
#define EVT_EXEC 2
#define EVT_EXIT 3

struct data_t {
    u8  type;
    u8  has_tty, has_block, has_dstate, has_net;
    u32 pid, ppid, uid;
    u64 ktime_ns;
    char comm[TASK_COMM_LEN];
    char tty[16];
    // exit-only (zero on fork/exec)
    int exit_code;
    u64 utime_ns, stime_ns, cutime_ns, cstime_ns;
    u64 run_ns, wait_ns, start_ns, hiwater_rss_pages;
    u64 rd_bytes, wr_bytes, rchar, wchar;
    u64 maj_flt, cmaj_flt, min_flt, nvcsw, nivcsw;
    u64 blkio_ns, swapin_ns, freepages_ns;
    u64 block_ns, dstate_ns, dstate_max, dstate_cnt;
    u64 net_tx, net_rx, net_calls;
    u64 cgid;
    u32 nr_threads, pgrp, sid;
__DATA_ACC__};
BPF_PERF_OUTPUT(events);
__ARGV_DEFS____SUBMIT_DEFS____DSTATE_DEFS____ACC_DEFS____TCP_COMMON____TCP_TP____ACCEPT_PROBE____NETB_DEFS__

__FORK_PROBE__

TRACEPOINT_PROBE(sched, sched_process_exec) {
    u64 id = bpf_get_current_pid_tgid();
    struct task_struct *task = (struct task_struct *)bpf_get_current_task();
    struct data_t d = {};
    d.type     = EVT_EXEC;
    d.pid      = id >> 32;
    d.ppid     = task->real_parent->tgid;
    d.uid      = (u32)bpf_get_current_uid_gid();
    d.ktime_ns = bpf_ktime_get_ns();
    bpf_get_current_comm(&d.comm, sizeof(d.comm));
__TTY____CGID__
    events.perf_submit(args, &d, sizeof(d));
__ARGV_EXEC____SUBMIT_EXEC__
    return 0;
}

TRACEPOINT_PROBE(sched, sched_process_exit) {
    u64 id = bpf_get_current_pid_tgid();
    u32 tgid = id >> 32, tid = (u32)id;
    struct task_struct *task = (struct task_struct *)bpf_get_current_task();
__THREAD_DEL____EXIT_GATE__    struct data_t d = {};
    d.type      = EVT_EXIT;
    d.pid       = tgid;
    d.ppid      = task->real_parent->tgid;
    d.uid       = (u32)bpf_get_current_uid_gid();
    d.ktime_ns  = bpf_ktime_get_ns();
    bpf_get_current_comm(&d.comm, sizeof(d.comm));
    d.exit_code = task->exit_code;
__CPU__
    d.run_ns    = task->se.sum_exec_runtime;
    d.start_ns  = task->start_time;
    d.maj_flt   = task->maj_flt;
    d.min_flt   = task->min_flt;
    d.nvcsw     = task->nvcsw;
    d.nivcsw    = task->nivcsw;
__SCHED____IO____DELAY____DSTATE_TASK____DSTATE_READ____NETB_READ____PIDS____TTY____CGID____EXIT_LEADER__
    events.perf_submit(args, &d, sizeof(d));
__SUBMIT_EXIT__
    return 0;
}
"""
    return (text.replace('__INC__', '\n'.join(inc))
                .replace('__DATA_ACC__', data_acc)
                .replace('__FORK_PROBE__', fork_probe)
                .replace('__THREAD_DEL__', thread_del)
                .replace('__EXIT_GATE__', exit_gate)
                .replace('__EXIT_LEADER__', exit_leader)
                .replace('__ARGV_DEFS__', argv_defs)
                .replace('__SUBMIT_DEFS__', submit_defs)
                .replace('__DSTATE_DEFS__', dstate_defs)
                .replace('__ACC_DEFS__', acc_defs)
                .replace('__NETB_DEFS__', netb_defs)
                .replace('__TCP_COMMON__', tcp_common_defs)
                .replace('__TCP_TP__', tcp_tp)
                .replace('__ACCEPT_PROBE__', accept_probe)
                .replace('__CPU__', cpu).replace('__SCHED__', sched).replace('__IO__', io)
                .replace('__DELAY__', delay)
                .replace('__DSTATE_TASK__', dstate_task)
                .replace('__DSTATE_READ__', dstate_read)
                .replace('__DSTATE_DEL__', dstate_del_thread)
                .replace('__NETB_READ__', netb_read)
                .replace('__PIDS__', pids)
                .replace('__TTY__', tty).replace('__CGID__', cgid)
                .replace('__ARGV_EXEC__', argv_exec)
                .replace('__SUBMIT_EXEC__', submit_exec).replace('__SUBMIT_EXIT__', submit_exit))


# ---------------------------------------------------------------------------
# feature probing
# ---------------------------------------------------------------------------

CFLAGS = ['-Wno-macro-redefined', '-Wno-duplicate-decl-specifier']   # 6.x header noise


class _Capture:
    """Divert fd 2 (BCC's clang writes there, bypassing sys.stderr) into a temp
    file so a failed probe can report the compiler's actual error lines."""
    def __enter__(self):
        import tempfile
        sys.stderr.flush()
        self._saved = os.dup(2)
        self._tmp = tempfile.TemporaryFile(mode='w+b')
        os.dup2(self._tmp.fileno(), 2)
        return self

    def __exit__(self, *a):
        sys.stderr.flush()
        os.dup2(self._saved, 2)
        os.close(self._saved)
        self._tmp.seek(0)
        self.text = self._tmp.read().decode('utf-8', 'replace')
        self._tmp.close()
        return False


def _try_load(BPF, feats, quiet=True):
    """(bpf, None) or (None, error_text) including clang's error lines."""
    text = build_bpf_text(feats)
    if not quiet:
        try:
            return BPF(text=text, cflags=CFLAGS), None
        except Exception as e:
            return None, str(e)
    cap = _Capture()
    try:
        with cap:
            b = BPF(text=text, cflags=CFLAGS)
        return b, None
    except Exception as e:
        err = cap.text if PROBE_VERBOSE else _clang_errors(cap.text)
        return None, ('%s\n%s' % (e, err)).strip()


def _clang_errors(stderr_text, limit=4):
    lines = stderr_text.splitlines()
    out = []
    for i, line in enumerate(lines):
        if 'error:' in line or ('invalid' in line.lower() and 'R' in line[:3]):
            out.append(line.strip())
            if i + 1 < len(lines) and lines[i + 1].strip():
                out.append('    ' + lines[i + 1].strip())
        if len(out) >= limit * 2:
            break
    return '\n'.join(out)


def _short_err(err):
    s = [ln for ln in str(err).strip().splitlines() if ln.strip()]
    keep = [ln.strip() for ln in s if 'error' in ln.lower() or ln.startswith('    ')]
    if not keep:
        keep = s[-2:] if s else ['unknown']
    return ' | '.join(keep)[:600]


def _feature_cache_path():
    return os.path.join(OUTPUT_DIR, '.features_%s.json' % os.uname().release.replace('/', '_'))


def probe_features(BPF, verbose=True):
    """Greedy per-block probing -> (bpf, accepted_set, rejected{name: reason})."""
    global BTF_OFFS
    if FEATURES_ENV != 'auto':
        feats = {x.strip() for x in FEATURES_ENV.split(',') if x.strip()}
        bad = feats - set(BLOCK_NAMES)
        if bad:
            raise SystemExit('EBPFM_FEATURES has unknown block(s): %s' % ', '.join(sorted(bad)))
        for blk in feats:
            if not btf_ok(blk):
                raise SystemExit('EBPFM_FEATURES pins %s but BTF lacks its members' % blk)
        b, err = _try_load(BPF, feats, quiet=False)
        if b is None:
            raise SystemExit('pinned feature set failed to load: %s' % err)
        return b, feats, {}
    if FEATURE_CACHE:
        try:
            with open(_feature_cache_path()) as fh:
                cached = json.load(fh)
            # Keyed by kernel release, but only valid for the block list it was
            # probed against: a collector that has gained a block since must probe
            # it, or every host that ran the older collector never would.
            if cached.get('blocks') != list(BLOCK_NAMES):
                raise ValueError('feature cache predates the current block list')
            feats = set(cached.get('features', []))
            if cached.get('btf_offs'):
                BTF_OFFS = cached['btf_offs']
            b, err = _try_load(BPF, feats)
            if b is not None:
                if verbose:
                    sys.stderr.write('BPF features (cached for %s): %s\n'
                                     % (os.uname().release, ','.join(sorted(feats))))
                return b, feats, cached.get('rejected', {})
        except Exception:
            pass
    b, err = _try_load(BPF, set(), quiet=False)
    if b is None:
        raise SystemExit('error: even the base BPF program failed to load:\n%s' % err)
    accepted, rejected = set(), {}
    for name, requires, _purpose in BLOCKS:
        alt = ALTERNATIVES.get(name)
        sup = [a for a in ((alt,) if alt else ()) + ALSO_SUPERSEDED_BY.get(name, ()) if a in accepted]
        if sup:
            rejected[name] = 'superseded by %s' % ','.join(sup)
            continue
        if any(r not in accepted for r in requires):
            rejected[name] = 'requires %s' % ','.join(requires)
            continue
        if not btf_ok(name):
            rejected[name] = 'BTF lacks required members (needs bpftool + /sys/kernel/btf/vmlinux)'
            continue
        cand = accepted | {name}
        nb, err = _try_load(BPF, cand)
        if nb is None:
            rejected[name] = _short_err(err)
            continue
        try:
            b.cleanup()
        except Exception:
            pass
        b, accepted = nb, cand
    # A failed variant whose sibling loaded is not a capability gap; say so while
    # keeping the compiler line for the record.
    for group in (PIDS_BLOCKS, TCP_BLOCKS, {'argv_user', 'argv_kernel'}, DSTATE_BLOCKS,
                  NETPEER_BLOCKS):
        winner = sorted(group & accepted)
        if winner:
            for nm in group - accepted:
                if nm in rejected and not rejected[nm].startswith('superseded'):
                    rejected[nm] = 'not needed (%s loaded); %s' % (winner[0], rejected[nm])
    if FEATURE_CACHE:
        try:
            os.makedirs(OUTPUT_DIR, exist_ok=True)
            _secure_path(OUTPUT_DIR, DIR_MODE)
            cache_path = _feature_cache_path()
            with open(cache_path, 'w') as fh:
                json.dump({'kernel': os.uname().release, 'features': sorted(accepted),
                           'rejected': rejected, 'btf_offs': btf_offsets(),
                           'blocks': list(BLOCK_NAMES),
                           'probed_at': _now()}, fh, indent=1)
            _secure_path(cache_path, FILE_MODE)
        except Exception:
            pass
    if verbose:
        sys.stderr.write('BPF features: %s\n' % (','.join(sorted(accepted)) or '(base only)'))
        gaps = []
        for k, v in rejected.items():
            v1 = v.replace('\n', ' | ')
            if v1.startswith('superseded') or v1.startswith('not needed'):
                sys.stderr.write('  skipped %-12s %s\n' % (k, v1.split(';', 1)[0]))
            else:
                gaps.append(k)
                sys.stderr.write('  DROPPED %-12s %s\n' % (k, v1[:300]))
        if gaps:
            sys.stderr.write('  (%d capability gap(s): %s — EBPFM_PROBE_VERBOSE=1 for full output)\n'
                             % (len(gaps), ','.join(gaps)))
        else:
            sys.stderr.write('  (no capability gaps: every block has a loaded variant)\n')
    return b, accepted, rejected


# ---------------------------------------------------------------------------
# output
# ---------------------------------------------------------------------------

def _now():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


BOOT_ID = boot_id()


def _tz_name():
    """IANA-ish local zone name plus the UTC offset in force at startup.

    ts is local and naive; without this a reader cannot convert it, and cannot
    tell a DST shift from a clock jump."""
    try:
        off = -(time.altzone if time.daylight and time.localtime().tm_isdst else time.timezone)
        sign = '+' if off >= 0 else '-'
        off = abs(off)
        name = time.tzname[1] if (time.daylight and time.localtime().tm_isdst) else time.tzname[0]
        return '%s%s%02d:%02d' % (name, sign, off // 3600, (off % 3600) // 60)
    except Exception:
        return None


def session_uuid(agent_pid):
    """Session id that survives a collector restart, unlike session_key.

    session_key is "apid:<pid>", so a restart re-keys every live session and pid
    recycling can merge two unrelated ones. Digesting (boot_id, pid, the pid's
    start time in clock ticks) fixes both: start time is monotonic since boot, so
    the triple is unique for the life of the boot, and it is re-derivable -- a
    restarted collector seeds live pids and recomputes the identical digest.

    None when the start time is unreadable (the root already exited), rather than
    a digest over a missing component that would silently differ from the one the
    previous run emitted."""
    if agent_pid is None or BOOT_ID is None:
        return None
    m = proc.get(agent_pid)
    st = m.get('start_ticks') if m else None
    if st is None:
        raw = read_stat(agent_pid)
        if raw is None:
            return None
        st = raw['starttime']
        if m is not None:
            m['start_ticks'] = st
    return hashlib.sha1(('%s|%d|%d' % (BOOT_ID, agent_pid, st)).encode()).hexdigest()[:16]


# pid 1's namespaces and bounding capability set, read once. These are the
# baseline every process is compared against; they never change for the life of
# the boot. CapBnd is read rather than assumed because CAP_LAST_CAP is
# kernel-version dependent, so there is no safe constant for "the full set".
INIT_NS = {}
INIT_CAPBND = None


def _init_baseline():
    global INIT_NS, INIT_CAPBND
    INIT_NS = read_ns(1)
    c = read_confinement(1)
    INIT_CAPBND = (c or {}).get('cap_bnd')


_init_baseline()


def classify_sandbox(pid, m=None):
    """(sandbox, sandbox_src, sandbox_detail) for a pid.

    `sandbox` means NAMESPACE ISOLATION and nothing else. Seccomp and
    capabilities go in the detail string only, deliberately:

      * Every VS Code / Electron renderer runs seccomp-filtered, and
        actor3="human-vscode" is a first-class actor class here -- folding
        seccomp into the boolean would light up that entire class as sandboxed.
        Measured on a stock host: systemd-resolve, systemd-logind, rsyslogd,
        systemd-network and polkitd all report Seccomp=2, because systemd's
        SystemCallFilter= installs one. Seccomp is evidence of CONFINEMENT, not
        of agent SANDBOXING.
      * A capability set decides nothing. An unprivileged process has CapEff=0 by
        definition, so "reduced caps => sandboxed" would flag every ordinary
        process on a login node; and a process that is uid 0 inside a user
        namespace -- the primary case of interest -- reports a FULL set, so the
        inverse rule is equally wrong. Only CapBnd against init's says anything,
        and only as detail.

    A true verdict that may still surprise: systemd units with PrivateTmp= or
    ProtectSystem= really are in a private mount namespace, so a hardened daemon
    reports sandboxed/mntns (verified: systemd-resolve). That is correct for what
    this field MEANS -- namespace isolation -- it is simply not agent sandboxing.
    Those processes are almost never tracked anyway (no agent ancestry, no tty,
    MIN_UID off by default), so they rarely reach a record; read sandbox_src, and
    sandbox_ancestry, before treating a sandboxed verdict as an agent sandbox.

    Returns sandbox=None when nothing could be read. That is NOT the same as
    "unsandboxed": /proc/<pid>/ns/* is ptrace-gated for other users when the
    collector runs unprivileged, and reporting a false negative there would
    deflate the sandbox rate for every user except the collector's owner -- a
    systematic bias that reads like a finding."""
    ns = read_ns(pid)
    conf = read_confinement(pid)
    if not ns and conf is None:
        return None, 'denied', None

    detail = []
    verdict, src = None, None
    # user first: unprivileged sandboxing on a login node essentially requires a
    # user namespace. mnt second: it catches root-launched containers and
    # `unshare -m`. Then pid, then net.
    for kind in ('user', 'mnt', 'pid', 'net'):
        have = ns.get(kind)
        base = INIT_NS.get(kind)
        if have is None or base is None:
            continue
        if have != base:
            detail.append(kind)
            if verdict is None:
                verdict, src = 'sandboxed', kind + 'ns'

    if verdict is None and conf and conf.get('nspid_depth') and conf['nspid_depth'] > 1:
        detail.append('pid')
        verdict, src = 'sandboxed', 'pidns'

    if conf:
        if conf.get('seccomp'):
            detail.append('seccomp=%d' % conf['seccomp'])
        if conf.get('nnp'):
            detail.append('nnp')
        if (INIT_CAPBND and conf.get('cap_bnd')
                and conf['cap_bnd'] != INIT_CAPBND):
            detail.append('capbnd')

    if verdict is None:
        # Only claim "unsandboxed" if we actually compared every namespace we
        # have a baseline for; otherwise we do not know.
        checked = [k for k in ('user', 'mnt', 'pid', 'net')
                   if ns.get(k) is not None and INIT_NS.get(k) is not None]
        if checked:
            verdict, src = 'unsandboxed', 'none'
        else:
            src = 'denied'
    return verdict, src, (','.join(sorted(set(detail))) or None)


def sandbox_ancestry_of(m):
    """How a process came to be sandboxed, as opposed to whether it is.

    Free: the ancestry is already resolved. Worth emitting separately because
    ancestry and ground truth answer different questions, and their DISAGREEMENT
    is the signal -- Claude Code's bwrap use is a capability probe
    (`bwrap --ro-bind / /`), and per findings/login.md that probe is exactly what
    wedges in D-state on a dead automount. ancestry="bwrap" with
    sandbox="unsandboxed" is that probe's signature; ancestry="bwrap" with
    sandbox="sandboxed" is a real confinement.

    Note the collector could not see this before: _AGENT_PATTERNS is
    first-match-wins with bwrap LAST, and resolve() returns early when a process
    self-classifies, so a claude_code process inside bwrap reports
    agent_type="claude_code" and the bwrap fact survived only in tree_root_pid,
    which carries a pid and no type."""
    if m is None:
        return None
    if (m.get('agent_type') or None) in SANDBOX_TYPES:
        return m['agent_type']
    # Fast path, and it is the common one: no agent ancestry at all means no
    # ancestor of ANY agent type, bwrap included -- resolve() would have
    # attributed this process to one otherwise. Skipping the walk here keeps a
    # parent-chain traversal off the per-record path for every ordinary process,
    # which on an exec-heavy node is nearly all of them.
    if m.get('agent_pid') is None:
        args_ = m.get('args') or ''
        for tok in ('codex-linux-sandbox', 'CURSOR_SANDBOX'):
            if tok in args_:
                return tok
        return None
    # Walk the real parent chain, resolving as we go. Walking agent_pid alone is
    # order-dependent: resolve() returns early for a self-classifying process, so
    # an ancestor's agent_pid may still be unset unless tree_root() happened to
    # run first. tree_root has the same shape for the same reason.
    cached = m.get('sandbox_anc')
    if cached is not _UNSET:
        return cached
    cur, seen = m.get('ppid'), set()
    while cur is not None and cur not in seen and cur in proc:
        seen.add(cur)
        pm = proc[cur]
        resolve(cur)
        if (pm.get('agent_type') or None) in SANDBOX_TYPES:
            m['sandbox_anc'] = pm['agent_type']
            return pm['agent_type']
        nxt = pm.get('ppid')
        if nxt == cur:
            break
        cur = nxt
    args = m.get('args') or ''
    out = None
    for tok in ('codex-linux-sandbox', 'CURSOR_SANDBOX'):
        if tok in args:
            out = tok
            break
    m['sandbox_anc'] = out
    return out


def _envelope(event, when=None):
    """The record envelope: `ts`, `ts_epoch`, `host`, `event`, `collector_pid`,
    `schema_version`.

    `ts` is local, naive and second-granular -- fine for a human reading one
    host's file, useless for ordering or binning records from seven hosts against
    each other. `ts_epoch` is the authoritative key for both: UTC by definition,
    sub-second, and monotone enough to sort across hosts. `ts` is kept unchanged
    because every existing consumer parses it.

    `collector_pid` is this process, so its own process tree can be dropped
    downstream (`pid` or `ppid` equal to it). That tree is invisible in a normal
    deployment, where nothing admits it, but a capture started from a login
    shell inherits the tty and is tracked as a human. It also tells two runs in
    one day's file apart.

    Through schema 6 this also stamped `source` ('ebpf') and `collector`
    ('ebpf_marthen_new') on every record: constants that said nothing the feed's
    own location does not, at a cost on every line. Nothing read either one.

    Built here rather than copied per record type so a future envelope field
    lands on all of them; DailyWriter.write() backstops anything that still
    constructs one by hand.

    `when` (epoch seconds) stamps a record for a moment already past: an exit
    record is built at the end of its hold, but describes the exit."""
    if when is None:
        ts, when = _now(), time.time()
    else:
        ts = datetime.fromtimestamp(when).strftime('%Y-%m-%d %H:%M:%S')
    return {'ts': ts, 'ts_epoch': when, 'host': HOSTNAME,
            'event': event, 'collector_pid': COLLECTOR_PID,
            'schema_version': SCHEMA_VERSION}


def _top_n(d, n, or_none=False):
    """The largest `n` entries of a count map, highest first.

    Inlined five times before this existed. `or_none` returns None for an empty
    map, which the optional/root-gated maps want so an absent measurement does
    not render as an empty one."""
    if or_none and not d:
        return None
    return dict(sorted(d.items(), key=lambda kv: -kv[1])[:n])


# Every record carries `event`; these two constants decide which of the day's two
# files it lands in. `exit` and `truncated` are the per-process terminal records
# -- the unbounded stream, whose rate IS the fork rate of the node and is highest
# exactly when the node is busiest. Everything else describes the node or the run
# as it stood at a moment: the residency census and its totals, the
# connection/submit observations, and the collector's own meta/stop bookends.
# Splitting them means a consumer that wants only the census does not have to
# read (or retain) a day of exits to find it.
EXIT_EVENTS = ('exit', 'truncated')
STREAMS = ('snapshot', 'exits')


def stream_of(event):
    """Record `event` -> which of the day's two files it belongs in.

    Deliberately total: an event kind added later without a decision here lands
    in `snapshot` rather than vanishing or raising mid-capture. StreamSplitTest
    pins the known set, so the default cannot silently absorb a new high-rate
    stream without a test failing first."""
    return 'exits' if event in EXIT_EVENTS else 'snapshot'


class DailyWriter:
    """JSONL sink: two files per host per day, one per stream.

    Layout is `<root>/<YYYY-MM-DD>/<host>.<stream>.jsonl`, `stream` in
    (snapshot, exits) per stream_of(). Both handles open when the day opens,
    not on first use, so a running collector always shows the pair and an empty
    `.exits.jsonl` reads as "no exits observed" rather than "collector never
    started".

    Flush policy is the whole I/O cost of this collector. Through schema 5 every
    record was followed by fh.flush(), which is one write(2) per record -- and
    the record rate is the EVENT rate, unbounded and exactly highest when the
    node is busiest. Under 'poll' the loop flushes once per round instead, so
    syscalls are bounded by the poll rate (10/s at the default POLL_MS) however
    many records the round produced, and a burst of 400 exits costs one write
    instead of 400. Only DIRTY streams flush, so the split costs a second
    write(2) only in a round that actually wrote to both -- an idle round still
    costs nothing, and a pure-exit burst still costs one.

    What that trades: a record is durable within one poll round rather than
    immediately. SIGTERM, SIGINT and the duration stop all run through close(),
    which flushes, so the exposure is SIGKILL or power loss losing at most one
    round. EBPFM_FLUSH_MODE=record restores the old behaviour."""

    def __init__(self, root, host, flush_mode=None):
        self.root, self.host, self.day = root, host, None
        self.fh = {}              # stream -> open file
        self.dirty = {}           # stream -> bool
        self.n = 0
        self.flushes = 0          # write(2) count; reported in the stop record
        self.per_record = (flush_mode or FLUSH_MODE) == 'record'

    def path(self, stream, day=None):
        """Where `stream` lands for `day` (default: the open day). Used by the
        tests and by anything that needs to name a file without writing one."""
        return os.path.join(self.root, day or self.day,
                            '%s.%s.jsonl' % (self.host, stream))

    def _roll(self, day):
        """Open the day's pair, closing the outgoing one. _shut() flushes first,
        so a rollover cannot strand a record in the old day's buffer."""
        prev_day = self.day
        self._shut()
        daydir = os.path.join(self.root, day)
        os.makedirs(daydir, exist_ok=True)
        # Tighten the day dir and the root above it: makedirs is subject to the
        # umask, so set the configured mode explicitly. Root first so an admin
        # browsing /scratch/ebpfm sees the same policy at every level.
        _secure_path(self.root, DIR_MODE)
        _secure_path(daydir, DIR_MODE)
        self.day = day
        for s in STREAMS:
            path = self.path(s)
            self.fh[s] = open(path, 'a')
            _secure_path(path, FILE_MODE)
            self.dirty[s] = False
        # The outgoing day's files are now closed and complete -- compress them
        # out of band. prev_day is None on the first open (nothing to seal).
        if prev_day and prev_day != day:
            _compress_day(self.root, prev_day, self.host)

    def _shut(self):
        self.flush()
        for fh in self.fh.values():
            fh.close()
        self.fh, self.dirty = {}, {}

    def write(self, rec):
        # Backstop for `ts_epoch`: every record type is supposed to get it from
        # _envelope(), but this is the one funnel all of them pass through, so a
        # record built by hand somewhere still carries the key the dashboard bins
        # on. setdefault, not assignment -- the envelope's value is stamped at
        # record construction and is the more accurate of the two.
        rec.setdefault('ts_epoch', time.time())
        day = datetime.now().strftime('%Y-%m-%d')
        if day != self.day:
            self._roll(day)
        s = stream_of(rec.get('event'))
        self.fh[s].write(json.dumps(rec) + '\n')
        self.n += 1
        if self.per_record:
            self.fh[s].flush()
            self.flushes += 1
        else:
            self.dirty[s] = True

    def flush(self):
        """Called once per poll round. A no-op for any stream the round left
        alone, which on an idle node is both of them."""
        for s, fh in self.fh.items():
            if self.dirty.get(s):
                fh.flush()
                self.flushes += 1
                self.dirty[s] = False

    def close(self):
        self._shut()


# ---------------------------------------------------------------------------
# pure helpers (unit-tested; no bcc, no root)
# ---------------------------------------------------------------------------

def decode_status(raw):
    """wait(2) status -> (exit_code, signal, core_dumped). exit_code is null iff
    signal-terminated; the shell's 128+N convention is NOT applied."""
    sig = raw & 0x7f
    if sig == 0:
        return (raw >> 8) & 0xff, None, False
    return None, sig, bool(raw & 0x80)


def decode_argv(buf, cap=None, truncated=False):
    r"""Kernel arg area (NUL-separated) -> 'a b c'.

    `truncated` drops everything after the last NUL, and that is a correctness
    fix rather than tidiness. When the kernel clamp fired, the buffer ends
    MID-TOKEN with no trailing NUL, so rstrip removes nothing and half an
    argument is presented as a whole one -- and classify() runs on this string.
    agent_classify matches `(?:^|/|\s)claude(?:\s|$)`, so an argv truncated to
    '... /opt/tools/claude' matches via the end anchor when the real token was
    'claude-wrapper'. Truncation could therefore INVENT a claude_code process."""
    if not buf:
        return ''
    raw = buf.rstrip(b'\x00')
    if truncated:
        cut = raw.rfind(b'\x00')
        raw = raw[:cut] if cut > 0 else b''
    s = raw.replace(b'\x00', b' ').decode('utf-8', 'replace').strip()
    return s if cap is None else s[:cap]


def argv_tokens(buf, truncated=False):
    r"""The same arg area as its separate arguments, cut where decode_argv cuts.

    decode_argv's string cannot say where one argument ends and the next
    begins, and redaction needs to: a prompt is ONE argument, spaces and all."""
    if not buf:
        return []
    raw = buf.rstrip(b'\x00')
    if truncated:
        cut = raw.rfind(b'\x00')
        raw = raw[:cut] if cut > 0 else b''
    if not raw:
        return []
    return [t.decode('utf-8', 'replace') for t in raw.split(b'\x00')]


_JOBID_PATTERNS = (
    re.compile(r'Submitted batch job (\d+)'),                      # sbatch
    re.compile(r'salloc: Granted job allocation (\d+)'),            # salloc
    re.compile(r'salloc: Pending job allocation (\d+)'),
    re.compile(r'srun: job (\d+) (?:queued|has been allocated)'),  # srun
    re.compile(r'srun: jobid (\d+)'),
    re.compile(r'^(\d{4,})(?:;\S+)?\s*$', re.M),                   # sbatch --parsable
)


def parse_job_ids(text):
    """Slurm job ids in captured stdout/stderr, in order, de-duplicated."""
    out = []
    for rx in _JOBID_PATTERNS:
        for m in rx.finditer(text):
            jid = int(m.group(1))
            if jid not in out:
                out.append(jid)
    return out


def is_external_ip(ip):
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return not (a.is_private or a.is_loopback or a.is_link_local or a.is_multicast
                or a.is_reserved or a.is_unspecified)


def null_if_off(value, block, features):
    """null (not 0) for a field whose kernel block was dropped."""
    return value if block in features else None


def parse_kstack(text, depth=5):
    """/proc/<pid>/stack -> up to `depth` kernel frame names. Same extraction as
    snapshot.py's _proc_kstack; names the autofs/NFS/RPC call path one level
    deeper than wchan did."""
    if not text:
        return None
    frames = _KSTACK_FRAME.findall(text)
    return frames[:depth] or None


def pick_dstate(e, features):
    """(seconds, episodes, max_seconds, source) for item 2.

    Both blocks can be live at once so a capture can cross-check them; the exact
    struct read wins because it is the kernel's own total, while the accumulator
    only sees episodes that ended after we attached."""
    if 'dstate_task' in features and getattr(e, 'has_block', 0):
        total = e.block_ns / 1e9
        cnt = e.dstate_cnt if ('dstate_stat' in features and getattr(e, 'has_dstate', 0)) else None
        mx = (e.dstate_max / 1e9) if ('dstate_stat' in features and getattr(e, 'has_dstate', 0)) else None
        return round(total, 4), cnt, (round(mx, 4) if mx is not None else None), 'task'
    if 'dstate_stat' in features and getattr(e, 'has_dstate', 0):
        return (round(e.dstate_ns / 1e9, 4), e.dstate_cnt,
                round(e.dstate_max / 1e9, 4), 'stat')
    if features & DSTATE_BLOCKS:
        return 0.0, 0, 0.0, ('task' if 'dstate_task' in features else 'stat')
    return None, None, None, None


def read_fork_total():
    """The node's cumulative fork counter from /proc/stat, or None. Its delta is
    the only honest denominator for 'what share of this node's process churn is
    agent work', since the actor filter drops everything else."""
    try:
        with open('/proc/stat') as f:
            for line in f:
                if line.startswith('processes '):
                    return int(line.split()[1])
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# process table + ancestry
# ---------------------------------------------------------------------------

proc = {}          # pid -> entry
_children = {}     # ppid -> set(pid): an exec re-classifies its subtree without scanning the table
FEATURES = set()   # resolved kernel blocks (set by main)
writer = None
_cgid_map = {}
_cgid_last_walk = 0.0
_stop = False
_dropped = {'n': 0, 'commands': {}}  # item 6: what the actor filter discarded
_fork_prev = None
# (due_monotonic, m, rec, quiet) in insertion order, which is also due order
# because the hold is a constant. A deque, not a list: this is drained from the
# head once per poll round and popleft is O(1) where pop(0) is O(n). See
# _stage_exit().
_pending_exits = collections.deque()
_conn_dropped = {'n': 0, 'commands': {}}   # conns whose owner never got an exit record
# entries retired without an exit event (a lost one, or a pid recycled under
# them), and those only seen missing once so far: retired on the second miss
_proc_stats = {'vanished': 0}
_proc_missing = set()
# Exceptions a handler swallowed, by handler. They used to vanish without a
# trace; now each residency_totals and the stop record carry the counts.
_handler_errors = collections.Counter()
# perf events decoded and dropped per ring, cumulative; set up by main()
_perf_seen = {}
_perf_lost = {}


def _count_error(where):
    _handler_errors[where] += 1


# ---------------------------------------------------------------------------
# event ordering
# ---------------------------------------------------------------------------
# The queue holds (ktime_ns, rank, seq, handler, event). `rank` orders events
# that share a timestamp: an exec and its argv are stamped with the same
# ktime_ns, and the argv must come second. `seq` keeps arrival order among the
# rest, so the sort never compares a handler or an event.
RANK_CORE, RANK_ARGV, RANK_WRITE, RANK_TCP = 0, 1, 2, 3
_RANK_MAX = 9
_evq = []
_ev_seq = itertools.count()


def enqueue_event(ktime, rank, handler, ev):
    _evq.append((ktime, rank, next(_ev_seq), handler, ev))


def dispatch_events(cutoff=None):
    """Apply queued events stamped at or before kernel time `cutoff` (None: all
    of them), oldest first. Returns how many were applied.

    The handlers assume causal order -- a process's fork before its exec, its
    exec before its exit -- and the perf rings do not deliver it. Every CPU has
    its own ring and BCC drains them in turn, so one poll round can hand over a
    process's exit, read off one CPU's ring, before its own exec off another's,
    and every argv after every exec. Applied in that order, an exec after its
    exit revived the dead process: an entry nothing ever retired, which held its
    endpoint-table entries forever and lent its identity to the next process on
    the pid. And an exit record was frozen before its argv arrived."""
    global _evq
    if not _evq:
        return 0
    _evq.sort()
    if cutoff is None:
        due, _evq = _evq, []
    else:
        i = bisect.bisect_right(_evq, (cutoff, _RANK_MAX))
        if not i:
            return 0
        due, _evq = _evq[:i], _evq[i:]
    for _kt, _rank, _seq, handler, ev in due:
        try:
            handler(ev)
        except Exception:
            _count_error(handler.__name__)
    return len(due)


def _queued_exits():
    """Pids whose exit event is queued but not applied yet."""
    return {ev.pid for _kt, rank, _s, _h, ev in _evq
            if rank == RANK_CORE and getattr(ev, 'type', None) == EVT_EXIT}


def reconcile_proc(now=None):
    """Retire live entries whose process is gone; returns the set of live pids,
    or None when /proc could not be listed.

    An exit event can be lost (a full perf ring), and before this nothing ever
    retired the entry it left behind: it stayed live, kept its endpoint-table
    entries, and handed its identity to the next process on its pid. The tick
    only re-read processes it was going to report, so an untracked one never
    left -- 16-20k of them at a time on a busy login node. An entry is retired
    on its SECOND miss, a tick apart: on the first, its exit event may simply
    still be in a ring."""
    try:
        alive = list_pids()
    except Exception:
        return None
    now = time.time() if now is None else now
    held = _queued_exits()
    missing = set()
    for pid, m in list(proc.items()):
        if m['exited'] or pid in alive or pid in held:
            continue
        if pid in _proc_missing:
            _retire(m, now)
        else:
            missing.add(pid)
    _proc_missing.clear()
    _proc_missing.update(missing)
    return alive


def _on_signal(signum, frame):
    global _stop
    _stop = True


signal.signal(signal.SIGTERM, _on_signal)
signal.signal(signal.SIGINT, _on_signal)


def _new_entry(pid, ppid, comm=None, **kw):
    e = {'pid': pid, 'ppid': ppid, 'comm': comm, 'args': None, 'args_red': None,
         'argv_source': None,
         'argv_ktime': None, 'tty': None, 'user': None, 'uid': None, 'cgroup': None,
         'cwd': None, 'start_ticks': None,
     'args_raw_len': None, 'args_clamped': False, 'tick_cpu_s': None, 'sandbox_anc': _UNSET,
     'sandbox': None, 'sandbox_src': None, 'sandbox_detail': None,
         'start_ep': None, 'exec_ktime': None, 'attributed': None, 'is_agent': False,
         'agent_pid': None, 'agent_type': None, 'depth': None,
         'exited': False, 'gc_ts': None, 'is_submit': False, 'submit_buf': None,
         'emitted': False, 'conns': None,
         # kernel times of the fork that made the pid and of its exit, so an
         # event handled out of order can tell whether it is about this process
         'fork_ktime': None, 'exit_ktime': None,
         # schema 7 (see netpeer_read): the endpoint table's counters for this
         # process, what the netio series has reported of them, and its state
         'net_io': False, 'exit_pending': False, 'netp_k': None, 'netp_kobj': None,
         'netp_rep': None, 'in_series': False, 'netio_ts': None, 'netp_done': False}
    e.update(kw)
    return e


def _set_args(m, args, source, argv=None):
    """Set a process's command line. `argv` is the same line as separate
    arguments, where the source kept them apart (the kernel's arg area, /proc's
    cmdline): the emitted copy is redacted from those boundaries."""
    m['args'], m['argv_source'] = args, source
    m['args_red'] = _redact_now(args, argv)


def _redact_now(args, argv):
    """(args, as written, what was masked) for a line that needs its argument
    boundaries masked -- one that may run a coding agent, whose prompt is a
    single argument with spaces in it -- while they are in hand. None leaves
    the line to _emit_args(), which needs only the string."""
    if not (REDACT_PROMPTS and args and argv) or not needs_argv(args):
        return None
    try:
        red, what = redact_argv(argv, keys=REDACT_KEYS, prompts=True)
    except Exception:
        _count_error('redact')
        return None                       # _emit_args() tries again, and fails closed
    return (args, red, what)


def _emit_args(m):
    """(the command line as written, sorted names of what redaction masked or
    None). Cached on the entry against the very string it was made from, so a
    new argv is never written out under an old masking."""
    a = m.get('args')
    if not a:
        return None, None
    if not REDACT:
        return a, None
    c = m.get('args_red')
    if c is None or c[0] is not a:
        try:
            red, what = redact_line(a, keys=REDACT_KEYS, prompts=REDACT_PROMPTS)
        except Exception:
            # Fail closed: a line the masking could not read is not written out.
            _count_error('redact')
            red, what = '[REDACTED]', ['error']
        c = m['args_red'] = (a, red, what)
    return c[1], c[2]


def _link(pid, ppid):
    if ppid is not None:
        _children.setdefault(ppid, set()).add(pid)


def _unlink(pid, ppid):
    s = _children.get(ppid)
    if s is not None:
        s.discard(pid)
        if not s:
            del _children[ppid]


def proc_add(pid, ppid, **kw):
    """The entry for `pid`, made if new. An existing entry keeps its parent:
    attribution follows the process that forked it, while exec and exit report
    the CURRENT parent -- init or a subreaper for anything whose parent exited
    first, which is how `nohup cmd &` from an agent's shell runs."""
    m = proc.get(pid)
    if m is not None:
        if m['ppid'] is None and ppid is not None:
            proc_set_ppid(m, ppid)
        return m
    m = _new_entry(pid, ppid, **kw)
    proc[pid] = m
    _link(pid, ppid)
    return m


def proc_set_ppid(m, ppid):
    if ppid is None or m['ppid'] == ppid:
        return
    _unlink(m['pid'], m['ppid'])
    m['ppid'] = ppid
    _link(m['pid'], ppid)


def proc_del(pid):
    m = proc.pop(pid, None)
    if m is not None:
        _unlink(pid, m['ppid'])
    _children.pop(pid, None)


def resolve(pid, m=None):
    """Nearest-agent-ancestor attribution, memoized.

    A fork-without-exec child carries its parent's argv (argv_source='inherit');
    that argv must NOT make the child its own agent root — it attaches to the
    parent's tree at depth+1, as it would under the /proc poller.

    `m` is the entry itself when the caller holds it: an exit record is written
    after a hold, and by then the pid may name a newer process."""
    if m is None:
        m = proc.get(pid)
    if m is None:
        return False
    if m['attributed'] is not None:
        return m['attributed']
    own_args = m['args'] if m.get('argv_source') != 'inherit' else None
    tag = classify(own_args) if own_args else None
    if tag is not None:
        m.update(attributed=True, is_agent=True, agent_pid=pid, agent_type=tag, depth=0)
        return True
    ppid = m['ppid']
    if ppid and ppid != pid and ppid in proc and resolve(ppid):
        pm = proc[ppid]
        m.update(attributed=True, agent_pid=pm['agent_pid'], agent_type=pm['agent_type'],
                 depth=(pm['depth'] or 0) + 1)
        return True
    m['attributed'] = False
    return False


def tree_root(pid, m=None):
    """Top-most agent ancestor — the census's session root — as opposed to
    agent_pid, the nearest one (the spawner). A VS Code helper is its own nearest
    agent but belongs to the extension host's tree. `m` as for resolve()."""
    if m is None:
        m = proc.get(pid)
    if m is None or not resolve(pid, m):
        return None
    root = m['agent_pid']
    seen = set()
    while root is not None and root not in seen:
        seen.add(root)
        rm = m if root == pid else proc.get(root)
        if rm is None:
            break
        pp = rm['ppid']
        if pp is None or pp == root or pp not in proc or not resolve(pp):
            break
        root = proc[pp]['agent_pid']
    return root


def _tgid_of(pid):
    """Tgid of a task from /proc/<pid>/status, or None if it is already gone."""
    try:
        with open('/proc/%d/status' % pid) as f:
            for line in f:
                if line.startswith('Tgid:'):
                    return int(line.split()[1])
    except Exception:
        return None
    return None


def _invalidate_attribution(pid):
    """A pid's argv changed (exec): re-classify it and every descendant, via the
    children index, so the cost is the subtree size and not the table size."""
    stack = [pid]
    seen = set()
    while stack:
        p = stack.pop()
        if p in seen:
            continue
        seen.add(p)
        m = proc.get(p)
        if m is not None:
            m['attributed'] = None
        stack.extend(_children.get(p, ()))


def _ingest_proc(pid, ppid=None):
    """Populate/refresh an entry from /proc (root: any user). True if alive."""
    st = read_stat(pid)
    if st is None:
        return False
    uid = uid_of(pid)
    vals = dict(comm=st['comm'], tty=tty_name(st['tty_nr']), uid=uid,
                user=username(uid) if uid is not None else None,
                cgroup=read_cgroup(pid), start_ep=start_epoch(st['starttime']))
    m = proc_add(pid, ppid if ppid is not None else st['ppid'])
    m.update(vals)
    if m['argv_source'] != 'kernel':
        args, argv = _read_argv(pid)
        if args and not args.startswith('['):
            _set_args(m, args, 'proc', argv=argv)
            m['args_raw_len'] = len(args)      # /proc's cmdline is uncapped: exact
            m['args_clamped'] = False
            m['attributed'] = None
    return True


def _read_argv(pid):
    """(command line, its separate arguments) from /proc/<pid>/cmdline -- the
    line exactly as procparse.read_cmdline() makes it -- or (None, None)."""
    try:
        with open('/proc/%d/cmdline' % pid, 'rb') as f:
            raw = f.read()
    except Exception:
        return None, None
    argv = argv_tokens(raw)
    line = raw.replace(b'\x00', b' ').decode('utf-8', 'replace').strip()
    return (line or None), argv


def seed_existing(poll=None):
    """One-time /proc walk so pre-existing agents and parents resolve. The only
    full scan this collector ever does. It runs with the probes attached, and
    `poll` drains the perf rings into the event queue every few hundred pids,
    so a long walk on a busy node cannot overflow them."""
    for i, pid in enumerate(list_pids()):
        if poll is not None and i % 256 == 255:
            poll()
        _ingest_proc(pid)
        if WANT_CWD:
            m = proc.get(pid)
            if m is not None and m['cwd'] is None:
                c = read_cwd(pid)
                if c:
                    m['cwd'] = c
    # Only once the whole table is in: attribution needs every parent present.
    for m in list(proc.values()):
        _ask_hosts(m)


def _inherit(m, key):
    pm = proc.get(m['ppid'])
    if pm is not None and m.get(key) is None and pm.get(key) is not None:
        m[key] = pm[key]
        return True
    return False


def _inherit_tty(m, authoritative):
    """Copy the parent's controlling terminal only when we have no reading of our
    own. A forked child really does inherit its parent's tty, so inheriting is
    correct there. After an exec the kernel has already told us, and a process
    that called setsid() legitimately has NO tty — overwriting that with the
    parent's would relabel a detached process as an interactive human, which is
    exactly the misclassification the uid rung exists to catch."""
    if authoritative or m.get('tty') is not None:
        return
    _inherit(m, 'tty')


def _inherit_sandbox(m):
    """Sandbox state from the parent, so the per-event path stays syscall-free.

    Namespaces are inherited across fork and change only at unshare/setns/clone,
    so the parent's value is right for the overwhelming majority of processes --
    the same argument that keeps a readlink off every exec for cwd. The
    exceptions are handled by the exec predicate (a launcher's child) and the
    residency tick (a process that isolates itself later), because
    sched_process_fork carries no clone_flags: the collector genuinely cannot
    tell fork() from clone(CLONE_NEWNS) at the fork boundary."""
    if not WANT_SANDBOX or m.get('sandbox') is not None:
        return
    pm = proc.get(m['ppid'])
    if pm is not None and pm.get('sandbox') is not None:
        m['sandbox'] = pm['sandbox']
        m['sandbox_detail'] = pm.get('sandbox_detail')
        m['sandbox_src'] = 'inherit'


def _need_sandbox_read(m):
    """Whether this exec warrants a real namespace read rather than inheritance.

    The PARENT-comm rung is the load-bearing one and the reason this is a named
    function with a test. A launcher execs, THEN unshares, THEN execs the
    payload: reading at the launcher's own exec returns the HOST namespaces, and
    inheriting that downward marks the whole sandboxed tree "unsandboxed" --
    exactly backwards. Firing on the exec whose PARENT is a launcher measures the
    first process actually inside the sandbox; everything under it then inherits
    correctly, because inheritance within a sandbox is exact."""
    if SANDBOX_ALWAYS or m.get('is_agent'):
        return True
    # An unknown sandbox is only worth 5 /proc reads for a process that will
    # actually be EMITTED. Reading for every unknown process charged ~15k extra
    # /proc operations to a 3000-exec benchmark -- measured as a ~12% fork/exec
    # regression -- for processes the actor filter then dropped. This mirrors the
    # cwd predicate, which likewise reads only where the value is load-bearing
    # rather than for every unknown. An untracked process inherits or stays null,
    # and null is the honest answer for something we never report.
    if m.get('sandbox') is None and (m.get('tty') or m.get('agent_pid') is not None):
        return True
    if (m.get('agent_type') or None) in SANDBOX_TYPES:
        return True
    if _command_of(m) in SANDBOX_COMMS:
        return True
    pm = proc.get(m.get('ppid'))
    return pm is not None and _command_of(pm) in SANDBOX_COMMS


def _read_sandbox(m, when):
    """Authoritative read; overwrites whatever inheritance guessed."""
    v, src, detail = classify_sandbox(m['pid'], m)
    m['sandbox'], m['sandbox_detail'] = v, detail
    m['sandbox_src'] = 'denied' if src == 'denied' else '%s:%s' % (src, when)


def _inherit_cwd(m):
    """cwd from the parent. A fork inherits the directory, so this is correct
    unless the child chdir'd, which tool processes essentially never do. This is
    what keeps a readlink off the per-event path."""
    if not WANT_CWD or m.get('cwd') is not None:
        return
    pm = proc.get(m['ppid'])
    if pm is not None and pm.get('cwd'):
        m['cwd'] = pm['cwd']


class _FileHandle(ctypes.Structure):
    """struct file_handle with room for the 8-byte kernfs id."""
    _fields_ = [('handle_bytes', ctypes.c_uint), ('handle_type', ctypes.c_int),
                ('f_handle', ctypes.c_ubyte * 8)]


_libc = []


def _kernfs_id(path):
    """The id bpf_get_current_cgroup_id() returns for the cgroup at `path`, from
    its file handle (kernfs exports the node id as one), or None.

    The inode number is NOT that id everywhere: through 5.4 the id is
    generation << 32 | ino, and the generation starts at 1, so the old
    inode-keyed map never matched one id on EL8. Every exec there kept the
    cgroup its process inherited at fork -- pid 1's /init.scope for anything
    systemd starts, since it moves the child only after forking it -- and that
    spread to everything below: 22% of one node's exit records."""
    try:
        if not _libc:
            _libc.append(ctypes.CDLL(None, use_errno=True))
        fh = _FileHandle()
        fh.handle_bytes = 8
        mount_id = ctypes.c_int(0)
        if _libc[0].name_to_handle_at(-100, path.encode(), ctypes.byref(fh),
                                      ctypes.byref(mount_id), 0) != 0:
            return None
        if fh.handle_bytes != 8:
            return None
        return int.from_bytes(bytes(fh.f_handle), 'little')
    except Exception:
        return None


def _cgroup2_mount():
    """Where the unified (v2) hierarchy is mounted -- /sys/fs/cgroup on a v2
    host, /sys/fs/cgroup/unified on a hybrid one -- or None (pure v1). The id
    is meaningful only there; walking every v1 hierarchy as well, as this used
    to, let one hierarchy's inode numbers stand for another's."""
    try:
        with open('/proc/self/mountinfo') as f:
            for line in f:
                parts = line.split()
                if '-' in parts:
                    sep = parts.index('-')
                    if sep + 1 < len(parts) and parts[sep + 1] == 'cgroup2':
                        return parts[4]
    except Exception:
        pass
    return None


_CGROUP2 = []      # [mount point or None], found once


def _resolve_cgid(cgid):
    """cgroup v2 id -> path. Lazy; a miss triggers a rescan at most every 30 s
    (session scopes appear at login), and a caller that can still read /proc
    should, when this returns None."""
    global _cgid_last_walk
    if not cgid:
        return None
    p = _cgid_map.get(cgid)
    if p is not None:
        return p
    now = time.time()
    if now - _cgid_last_walk < 30:
        return None
    _cgid_last_walk = now
    if not _CGROUP2:
        _CGROUP2.append(_cgroup2_mount())
    root = _CGROUP2[0]
    if root is None:
        return None
    try:
        for dirpath, _dirnames, _files in os.walk(root):
            path = dirpath[len(root):] or '/'
            kid = _kernfs_id(dirpath)
            if kid is not None:
                _cgid_map[kid] = path
            else:
                try:
                    ino = os.stat(dirpath).st_ino
                except OSError:
                    continue
                # no file handle: the inode, and the 4.18 id with generation 1
                _cgid_map[ino] = path
                _cgid_map[(1 << 32) | ino] = path
            if len(_cgid_map) > 200000:
                break
    except Exception:
        return None
    return _cgid_map.get(cgid)


# ---------------------------------------------------------------------------
# actor classification
# ---------------------------------------------------------------------------

def actor_of(m):
    """(actor, attribution) or (None, None) when the process is dropped.

    Precedence is ancestry, then tty, then uid. The uid rung is opt-in
    (EBPFM_MIN_UID) because it widens what actor3="human" covers: a detached
    `nohup`, a cron job, or a bwrap sandbox already orphaned before we attached
    has no agent ancestry and no tty, yet still belongs to a user. The cgroup
    cannot make that call — on one test host tty-attached humans sat in
    /system.slice/ssh.service while another user's VS Code sat in a
    user-N.slice session scope, so the cgroup reflects how the session was
    established, not who owns the work."""
    if resolve(m['pid'], m):
        return 'agent', 'ancestry'
    if m.get('tty'):
        return 'human', 'tty'
    uid = m.get('uid')
    if MIN_UID and uid is not None and uid >= MIN_UID:
        return 'human', 'uid'
    return None, None


def _note_dropped(m):
    _dropped['n'] += 1
    c = _command_of(m) or '?'
    _dropped['commands'][c] = _dropped['commands'].get(c, 0) + 1


# ---------------------------------------------------------------------------
# core event handlers
# ---------------------------------------------------------------------------

def _cstr(raw):
    if raw is None:
        return ''
    if isinstance(raw, bytes):
        return raw.split(b'\x00', 1)[0].decode('utf-8', 'replace')
    return str(raw).split('\x00', 1)[0]


def _predates(m, kt):
    """True when entry `m` provably belongs to a process that existed before
    kernel time `kt`, so an event at `kt` that creates a process is not about it:
    its pid was recycled after an exit the collector never saw (a lost event).
    Seeded entries know only their /proc start time, which is whole clock ticks
    against a boot time rounded to the second, hence the slack."""
    if not kt:
        return False
    for k in ('fork_ktime', 'exec_ktime'):
        v = m.get(k)
        if v is not None and v < kt:
            return True
    se = m.get('start_ep')
    return bool(se is not None and BTIME and (BTIME + kt / 1e9) - se > 5.0)


def _retire(m, now=None):
    """Mark an entry whose process is gone without an exit event. It is then
    reaped like any exited entry; there is no record to write."""
    m['exited'] = True
    m['vanished'] = True
    m['gc_ts'] = m['gc_ts'] or (time.time() if now is None else now)
    _proc_stats['vanished'] += 1


def on_fork(e):
    child, parent = e.pid, e.ppid
    kt = getattr(e, 'ktime_ns', None)
    m = proc.get(child)
    if m is not None:
        if m['exited']:
            if kt and m.get('exit_ktime') and kt < m['exit_ktime']:
                return                        # that process's own birth, handled late
        elif _predates(m, kt):
            _retire(m)                        # its exit was lost; the pid is new now
    # sched_process_fork fires for every clone(), threads included. A thread has
    # tgid != pid, never produces a leader exit event, and /proc/<tid>/stat
    # reports the whole group's numbers — registering one creates an immortal
    # ghost that double-counts its process.
    if child in proc and not proc[child]['exited']:
        pass                                  # made by an event handled earlier
    elif 'fork_raw' in FEATURES:
        pass                                  # the kernel sends processes only
    else:
        tgid = _tgid_of(child)
        if tgid is None or tgid != child:
            return
    m = proc_add(child, parent)
    if m['exited']:
        proc_del(child)                       # pid recycled inside the GC window
        m = proc_add(child, parent)
    if m['fork_ktime'] is None:
        m['fork_ktime'] = kt
    if m['uid'] is None and e.uid != 0xffffffff:
        m['uid'], m['user'] = e.uid, username(e.uid)
    for k in ('user', 'uid', 'cgroup', 'tty'):
        _inherit(m, k)
    _inherit_cwd(m)
    _inherit_sandbox(m)
    pm = proc.get(parent)
    if pm is not None and m['args'] is None and pm.get('args'):
        m['args'], m['argv_source'] = pm['args'], 'inherit'
        m['args_red'] = pm.get('args_red')     # same string, same masking
        m['args_raw_len'] = pm.get('args_raw_len')
        m['args_clamped'] = pm.get('args_clamped', False)


def on_exec(e):
    pid = e.pid
    m = proc.get(pid)
    late = False
    if m is not None and m.get('exec_ktime') is not None and e.ktime_ns < m['exec_ktime']:
        return                            # an earlier image than the one already applied
    if m is not None and m['exited']:
        if m.get('exit_ktime') is not None and e.ktime_ns < m['exit_ktime']:
            # This process's own exec, handled after its exit. What it says still
            # belongs in the exit record, which is held; but the process is gone,
            # so it is not revived and /proc is not read (the pid may be reused).
            late = True
        else:
            proc_del(pid)                 # a recycled pid whose fork was never seen
    m = proc_add(pid, e.ppid)
    m['comm'] = _cstr(e.comm) or m['comm']
    m['exec_ktime'] = e.ktime_ns
    if not late:
        m['exited'] = False
    m['uid'] = e.uid
    m['user'] = username(e.uid)
    m['is_submit'] = m['is_submit'] or (m['comm'] in SUBMIT_COMMS)
    # exec replaces the image, so a previous argv is stale — unless the kernel
    # argv for THIS exec already arrived (it carries the same ktime_ns; the queue
    # applies it after the exec, but an event from a round already applied can
    # still come first).
    if not (m['argv_source'] == 'kernel' and m['argv_ktime'] == e.ktime_ns):
        _set_args(m, None, None)
        m['argv_ktime'] = None
        # the old image's lengths too: unknown until the new argv arrives
        m['args_raw_len'], m['args_clamped'] = None, False
    tty_known = False
    if 'tty' in FEATURES:
        m['tty'] = _cstr(e.tty) if e.has_tty else None
        tty_known = True
    if late:
        _invalidate_attribution(pid)
        return
    if 'cgid' in FEATURES and e.cgid:
        # A miss reads /proc rather than keep the cgroup inherited at fork: that
        # is the one wrong for every process a service manager starts (it forks,
        # THEN moves the child, then the child execs).
        m['cgroup'] = _resolve_cgid(e.cgid) or read_cgroup(pid) or m['cgroup']
    argv_kernel = ('argv_user' in FEATURES) or ('argv_kernel' in FEATURES)
    need_proc = (not argv_kernel) or ('tty' not in FEATURES) or (m['cgroup'] is None)
    if need_proc:
        # racy for ultra-short lives; kernel argv wins if it lands. A successful
        # read is itself an authoritative tty answer.
        tty_known = _ingest_proc(pid, ppid=e.ppid) or tty_known
    _inherit(m, 'cgroup')
    _inherit_tty(m, tty_known)
    _inherit_cwd(m)
    _inherit_sandbox(m)
    # item 1: readlink only where inheritance cannot answer or where the value is
    # load-bearing (the session work_dir, a submission's directory, a human shell).
    if WANT_CWD:
        need_cwd = (CWD_ALWAYS or m['cwd'] is None or m['is_submit']
                    or m.get('tty') is not None
                    or classify(m.get('args')) is not None)
        if need_cwd:
            c = read_cwd(pid)
            if c:
                m['cwd'] = c
    if WANT_SANDBOX:
        # The PARENT's comm is the load-bearing rung. bwrap execs, THEN unshares,
        # THEN execs the payload -- so reading at bwrap's own exec yields the
        # HOST namespaces, and inheriting that downward would mark the entire
        # sandboxed tree "unsandboxed", i.e. exactly backwards. Reading on the
        # exec whose parent is a launcher measures the first process actually
        # INSIDE the sandbox; everything below it then inherits correctly,
        # because inheritance within a sandbox is exact.
        if _need_sandbox_read(m):
            _read_sandbox(m, 'exec')
    # approval_mode: split from `autonomous`, which keeps the union so existing
    # findings stay comparable. The two dual-effect flags also assert the sandbox
    # is off -- argv is authoritative there in a way a /proc read cannot be,
    # because the flag says what the agent was TOLD to do.
    mode, sb_off = approval_mode_of(m.get('args'))
    if mode and sb_off:
        # The argv-derived mode itself is computed lazily at emit (see
        # _approval_of); only the sandbox side-effect has to be recorded now,
        # because it overrides a /proc reading.
        m['sandbox'], m['sandbox_src'] = 'unsandboxed', 'argv'
    _invalidate_attribution(pid)
    if m['argv_source'] == 'proc':        # a kernel argv asks in on_argv instead
        _ask_hosts(m)


def on_argv(a):
    pid = a.pid
    m = proc.get(pid)
    if (m is not None and m['exited'] and m.get('exit_ktime') is not None
            and a.ktime_ns > m['exit_ktime']):
        proc_del(pid)                     # the argv of a process born on a recycled pid
    m = proc_add(pid, None)
    if m.get('exec_ktime') is not None and a.ktime_ns < m['exec_ktime']:
        return                            # the argv of an image already replaced
    raw = ctypes.string_at(ctypes.addressof(a) + type(a).buf.offset, a.len)
    # raw_len is the pre-clamp length; > a.len means the kernel truncated, which
    # decode_argv must know or a half-token can classify as a whole one.
    raw_len = getattr(a, 'raw_len', 0) or a.len
    truncated = raw_len > a.len
    args = decode_argv(raw, truncated=truncated)
    if args:
        _set_args(m, args, 'kernel', argv=argv_tokens(raw, truncated))
        m['argv_ktime'] = a.ktime_ns
        m['args_raw_len'] = raw_len
        m['args_clamped'] = truncated           # the ARGV_KMAX read cut it
        if not m['comm']:
            m['comm'] = os.path.basename(args.split(' ', 1)[0])[:15]
        _invalidate_attribution(pid)
        _after_argv(m)
        _ask_hosts(m)


def _after_argv(m):
    """What on_exec decides from argv, for the kernel argv that reaches the
    process after its exec event (the queue applies them in that order). Rare by
    construction: only an agent's own exec or an approval-bypass flag does
    anything, so ordinary processes cost one regex pass."""
    if m['exited']:
        return
    args = m.get('args')
    tag = classify(args)
    if tag is not None:
        if WANT_CWD:
            c = read_cwd(m['pid'])        # the session's work_dir comes from here
            if c:
                m['cwd'] = c
        if WANT_SANDBOX:
            _read_sandbox(m, 'exec')
    mode, sb_off = approval_mode_of(args)
    if mode and sb_off:
        m['sandbox'], m['sandbox_src'] = 'unsandboxed', 'argv'


def _duration_and_start(m, e):
    exit_kt = e.ktime_ns
    if m.get('exec_ktime') is not None:
        d = (exit_kt - m['exec_ktime']) / 1e9
        return ((BTIME + m['exec_ktime'] / 1e9) if BTIME else m.get('start_ep')), d, 'exec'
    if m.get('start_ep') is not None and BTIME is not None:
        return m['start_ep'], (BTIME + exit_kt / 1e9) - m['start_ep'], 'proc'
    if e.start_ns and exit_kt >= e.start_ns:
        return ((BTIME + e.start_ns / 1e9) if BTIME else None), (exit_kt - e.start_ns) / 1e9, 'task'
    return None, None, None


def _approval_of(m):
    """(approval_mode, approval_src), derived at emit time.

    Computed here rather than only at exec so a SEEDED process gets it -- one
    alive before the collector attached, which never produced an exec event.
    That is not a corner case: it is exactly the long-running agent session a
    collector restart re-seeds, i.e. the case session_uuid exists to keep
    stable. `autonomous` is already lazy in the same way, and the two disagreeing
    on the same record would be indefensible.

    argv is the only source: the collector does not read /proc/<pid>/environ,
    so a bypass set through configuration rather than a flag is not seen."""
    if m is None:
        return None, None
    mode, _sb = approval_mode_of(m.get('args'))
    return (mode, 'argv') if mode else (None, None)


# The canonical identity key set. Derived once at import from _base_identity's
# own output against an empty entry, so adding a field there needs no edit here.
_UNSET = object()              # 'not computed yet', distinct from a cached None
_IDENTITY_KEYS = None          # filled in below, after _base_identity is defined


_COMM_MAX = 15               # TASK_COMM_LEN - 1: the kernel cuts comm here


def _command_of(m):
    """The executable's name, whole: the kernel's comm, completed from argv when
    the kernel cut it.

    comm stops at 15 characters, so a 15-character comm may be a prefix. The full
    name is then nearly always an argv token: argv[0] for a binary, the path after
    the interpreter for a script (binfmt_script puts the interpreter first). The
    first token whose basename extends the comm wins. Anything shorter is taken
    as is, so a login shell's '-bash' never replaces 'bash'; a name that exists
    nowhere longer -- a thread name set with prctl -- stays as the kernel has it."""
    comm = m.get('comm') or ''
    if len(comm) >= _COMM_MAX:
        for tok in (m.get('args') or '').split(' '):
            b = os.path.basename(tok)
            if len(b) > len(comm) and b.startswith(comm):
                return b
    return comm


def _base_identity(m, actor, attribution=None):
    agent_pid = m.get('agent_pid')
    args, masked = _emit_args(m)
    return {
        'actor': actor,
        'actor3': actor3(actor, m.get('agent_type')),
        'attribution': attribution,
        'pid': m['pid'], 'ppid': m['ppid'], 'uid': m.get('uid'), 'user': m.get('user'),
        'tty': m.get('tty'), 'command': _command_of(m),
        # masked first, then cut: a cut could split a key past recognition
        'args': ((args or '')[:ARGS_MAXLEN] if ARGS_MAXLEN > 0 else args) or None,
        # what `args` had masked ('prompt', or the credential rule that fired),
        # null when nothing was; the lengths below are still the raw argv's
        'args_redacted': masked,
        'argv_source': m.get('argv_source'),
        # An honest denominator for "unresolvable" in the tool distribution: a
        # truncated shell payload was indistinguishable from a genuinely short
        # one. null (not false) when the raw length is unknown -- false would
        # claim "measured and complete". Two things can cut it: the in-kernel
        # read (ARGV_KMAX), which is the only limit when ARGS_MAXLEN is 0, and
        # the emit cap when one is set.
        'args_len': m.get('args_raw_len'),
        'args_truncated': (None if m.get('args_raw_len') is None
                           else bool(m.get('args_clamped'))
                           or (ARGS_MAXLEN > 0 and m['args_raw_len'] > ARGS_MAXLEN)),
        'cwd': m.get('cwd'),
        # sandbox = namespace isolation (ground truth); sandbox_ancestry = how it
        # got there. Their disagreement separates a bwrap PROBE from a real one.
        'sandbox': m.get('sandbox'), 'sandbox_src': m.get('sandbox_src'),
        'sandbox_detail': m.get('sandbox_detail'),
        'sandbox_ancestry': sandbox_ancestry_of(m),
        'approval_mode': _approval_of(m)[0], 'approval_src': _approval_of(m)[1],
        'agent_type': m.get('agent_type'), 'agent_pid': agent_pid,
        'session_key': ('apid:%s' % agent_pid) if agent_pid is not None else None,
        # session_key is pid-based and dies with a collector restart; this does not.
        'session_uuid': session_uuid(agent_pid),
        'tree_root_pid': tree_root(m['pid'], m) if agent_pid is not None else None,
        'depth': m.get('depth'), 'is_agent': bool(m.get('is_agent')),
        'autonomous': is_autonomous(m.get('args')) if m.get('is_agent') else None,
        'cgroup': m.get('cgroup'),
    }


_IDENTITY_KEYS = tuple(_base_identity(
    {'pid': None, 'ppid': None}, None, None).keys())


def _stub_identity(pid=None, uid=None, comm=None):
    """Identity block for a record whose process we never tracked.

    Must carry EXACTLY the key set _base_identity() produces, with None for
    everything unknown. Before this existed, three call sites hand-built a
    7-key subset against _base_identity's 21, so `tcp` and `conn` records
    silently OMITTED fourteen keys -- absent, not null, which a JSONL consumer
    cannot tell apart from "written by an older collector". test_ebpfm.py
    asserts the two key sets are equal, so they cannot drift again.
    """
    out = dict.fromkeys(_IDENTITY_KEYS)
    out['pid'] = pid
    out['uid'] = uid
    out['user'] = username(uid) if uid is not None else None
    out['command'] = comm
    return out


def on_exit(e):
    pid = e.pid
    m = proc.get(pid)
    if m is not None and m['exited'] and m.get('exit_ktime') is not None:
        if e.ktime_ns == m['exit_ktime']:
            return                            # the same event twice
        proc_del(pid)                         # a recycled pid whose fork and exec were lost
    elif m is not None and _predates(m, getattr(e, 'start_ns', 0)):
        # The entry is an older process than the one exiting (the event names its
        # start): that one's exit was lost, and this one's fork and exec too.
        if not m['exited']:
            _retire(m)
        proc_del(pid)
    m = proc_add(pid, e.ppid, comm=_cstr(e.comm) or None)
    if not m['comm']:
        m['comm'] = _cstr(e.comm) or None
    if m['uid'] is None:
        m['uid'], m['user'] = e.uid, username(e.uid)
    tty_known = False
    if 'tty' in FEATURES:
        if e.has_tty and not m['tty']:
            m['tty'] = _cstr(e.tty)
        tty_known = True
    if 'cgid' in FEATURES and m['cgroup'] is None and e.cgid:
        m['cgroup'] = _resolve_cgid(e.cgid)
    for k in ('user', 'uid', 'cgroup'):
        _inherit(m, k)
    _inherit_tty(m, tty_known)
    _inherit_cwd(m)
    _inherit_sandbox(m)
    m['exited'] = True
    m['exit_ktime'] = e.ktime_ns
    m['gc_ts'] = time.time()
    m['net_io'] = bool('netbytes' in FEATURES and getattr(e, 'has_net', 0)
                       and (e.net_tx or e.net_rx))
    _stage_exit(m, e)


# fork, exec and exit share one ring; the queue is handed the right one
_CORE_HANDLERS = {EVT_FORK: on_fork, EVT_EXEC: on_exec, EVT_EXIT: on_exit}


# ---- whole-process counters (the thread_acc block) --------------------------
#
# sched_process_exit fires once per THREAD. The record used to be built at the
# group leader's, from the leader's own counters plus signal_struct's -- and
# signal_struct only holds threads the kernel has already released, which a
# thread is AFTER its own exit tracepoint. When a process ends, its threads die
# together and the leader is rarely the last of them, so most of a 42-thread
# process's CPU was in nobody's record (one Codex process: 48,297 s at its last
# residency tick, 79.7 s at exit). Faults, context switches, run time, waits and
# D-state were the leader's alone, and peak RSS predated the last thread.
#
# With the block, every thread adds its own counters to a per-process entry in
# `thr_acc` as it exits, and the record comes from the LAST thread to exit
# (signal->live == 0). The first thread to exit also notes what signal_struct
# held then -- threads that died before the collector attached -- as the base.
# The entry is taken after the exit hold, not at the exit event: a thread that
# dropped `live` earlier can reach its own tracepoint later (the one tearing
# down a large address space does), and the hold lets it land first.
_thr_acc_tab = None        # BPF hash `thr_acc`, when the block loaded (set by main)
_thr_acc_miss_tab = None   # BPF array `thr_acc_miss`: thread exits that found it full
# entries taken for a record; those made by a thread that reached its tracepoint
# after the last one (late); and records whose entry was gone (missing)
_thr_acc_stats = {'taken': 0, 'late': 0, 'missing': 0, 'swept': 0}

# (event field, its signal_struct twin on the event, accumulator suffix)
_ACC_SIGNAL = (('utime_ns', 'sig_utime', 'utime'), ('stime_ns', 'sig_stime', 'stime'),
               ('run_ns', 'sig_run', 'run'), ('min_flt', 'sig_min_flt', 'min_flt'),
               ('maj_flt', 'sig_maj_flt', 'maj_flt'), ('nvcsw', 'sig_nvcsw', 'nvcsw'),
               ('nivcsw', 'sig_nivcsw', 'nivcsw'))
_ACC_SIGNAL_IO = (('rd_bytes', 'sig_rd', 'rd'), ('wr_bytes', 'sig_wr', 'wr'),
                  ('rchar', 'sig_rchar', 'rchar'), ('wchar', 'sig_wchar', 'wchar'))
# per-thread counters with no signal_struct total: (event field, suffix, block)
_ACC_OWN = (('wait_ns', 'wait', 'sched'), ('blkio_ns', 'blkio', 'delay'),
            ('swapin_ns', 'swapin', 'delay'), ('freepages_ns', 'freepages', 'delay'),
            ('block_ns', 'block', 'dstate_task'))
# everything _exit_record() reads off the counters
_TOTAL_FIELDS = ('utime_ns', 'stime_ns', 'run_ns', 'min_flt', 'maj_flt', 'nvcsw', 'nivcsw',
                 'rd_bytes', 'wr_bytes', 'rchar', 'wchar', 'wait_ns', 'blkio_ns',
                 'swapin_ns', 'freepages_ns', 'block_ns', 'has_block', 'dstate_ns',
                 'dstate_max', 'dstate_cnt', 'has_dstate', 'nr_threads')


class _Totals(object):
    """A process's counters, under the exit event's own field names."""


def _may_have_acc(e):
    """An accumulator can exist only if one did at the exit (has_acc), or a
    thread was still unreleased then (nr_threads > 1) and may add one late."""
    return bool(getattr(e, 'has_acc', 0) or getattr(e, 'nr_threads', 0) > 1)


def _thread_acc_take(e):
    """The process's accumulator, removed from the kernel map, or None."""
    tab = _thr_acc_tab
    if tab is None or not _may_have_acc(e):
        return None
    k = tab.Key(e.pid)
    try:
        v = tab[k]
    except KeyError:
        if getattr(e, 'has_acc', 0):
            _thr_acc_stats['missing'] += 1
        return None
    try:
        del tab[k]
    except KeyError:
        pass
    _thr_acc_stats['taken'] += 1
    if not getattr(e, 'has_acc', 0):
        _thr_acc_stats['late'] += 1
    return v


def _thread_acc_release(e):
    """Drop the accumulator of a process that gets no record."""
    tab = _thr_acc_tab
    if tab is None or not _may_have_acc(e):
        return
    try:
        del tab[tab.Key(e.pid)]
    except Exception:
        pass


_residency_ticks = 0


def _thread_acc_sweep(alive):
    """Delete accumulators whose process is gone and has no exit pending: its
    exit event was lost, so nothing else ever would, and a full map stops
    accumulating for every process."""
    tab = _thr_acc_tab
    if tab is None:
        return 0
    held = {m['pid'] for _d, m, _e, _t in _pending_exits} | _queued_exits()
    n = 0
    try:
        keys = list(tab.keys())
    except Exception:
        return 0
    for k in keys:
        pid = getattr(k, 'value', k)
        if pid in alive or pid in held:
            continue
        try:
            del tab[k]
            n += 1
        except Exception:
            pass
    _thr_acc_stats['swept'] += n
    return n


def _thread_acc_report():
    """The thread_acc block's counters for residency_totals, or None without it."""
    if _thr_acc_tab is None:
        return None
    out = dict(_thr_acc_stats)
    try:
        out['full'] = int(_thr_acc_miss_tab[0].value)
    except Exception:
        out['full'] = None
    return out


def _exit_totals(e):
    """The whole process's counters at exit: the event itself without the
    thread_acc block (the leader's, as before), else this last thread's own plus
    the accumulator, on a base of signal_struct.

    Which base: the one the accumulator noted when its first thread exited --
    if it existed at the exit. One made LATER, by a thread that reached its
    tracepoint after the last one, noted a signal_struct that may already hold
    the last thread, so then the event's own signal_struct reading is the base
    and the late entry adds only its threads."""
    if 'thread_acc' not in FEATURES:
        return e
    acc = _thread_acc_take(e)
    t = _Totals()
    for name in _TOTAL_FIELDS:
        setattr(t, name, getattr(e, name, 0))
    acc_base = acc is not None and getattr(e, 'has_acc', 0)
    fields = _ACC_SIGNAL + (_ACC_SIGNAL_IO if 'io' in FEATURES else ())
    for own, sig, a in fields:
        base = getattr(acc, 'b_' + a) if acc_base else getattr(e, sig, 0)
        extra = getattr(acc, 's_' + a) if acc is not None else 0
        setattr(t, own, getattr(e, own) + base + extra)
    if acc is None:
        return t
    for own, a, blk in _ACC_OWN:
        if blk in FEATURES:
            setattr(t, own, getattr(e, own) + getattr(acc, 's_' + a))
    if 'dstate_stat' in FEATURES and acc.s_dstate_cnt:
        t.dstate_ns = getattr(e, 'dstate_ns', 0) + acc.s_dstate
        t.dstate_cnt = getattr(e, 'dstate_cnt', 0) + acc.s_dstate_cnt
        t.dstate_max = max(getattr(e, 'dstate_max', 0), acc.s_dstate_max)
        t.has_dstate = 1
    t.nr_threads = max(getattr(e, 'nr_threads', 0), acc.nthreads)
    return t


def _exit_record(m, e, actor, attribution, ts=None):
    """(record, quiet) for a process's exit event `e`.

    Built when the record is WRITTEN, not when the event arrives: an argv, a
    parent or an attribution that reaches the process during the hold belongs in
    its record, and every one of those used to be frozen out of it. `ts` is when
    the exit was handled, which is what the record's timestamp has always said.

    `quiet` is decided here and applied by the caller: a process that is too
    cheap to be interesting is still kept when it made a connection, and its
    socket closes arrive after its exit event."""
    F = FEATURES
    t = _exit_totals(e)
    start_ep, duration, dur_src = _duration_and_start(m, e)
    cpu_s = (t.utime_ns + t.stime_ns) / 1e9
    io_bytes = (t.rd_bytes + t.wr_bytes) if 'io' in F else 0
    comm = m.get('comm') or ''
    # These three discard a process for being too cheap to be interesting. A
    # process that talked to the network is never a no-op, over TCP or not
    # (m['net_io']); see _write_exit.
    quiet = bool(
        (not KEEP_TIMERS and not m['is_agent'] and comm in TIMER_COMMS
         and cpu_s == 0 and io_bytes == 0)
        or (duration is not None and duration < MIN_DURATION)
        or (cpu_s < MIN_CPU))

    exit_code, sig, core = decode_status(e.exit_code)
    ds_s, ds_n, ds_max, ds_src = pick_dstate(t, F)
    rec = _envelope('exit', ts)
    rec.update(_base_identity(m, actor, attribution))
    rec.update({
        'fork_only':    not bool(comm),
        'start':        datetime.fromtimestamp(start_ep).strftime('%Y-%m-%d %H:%M:%S') if start_ep else None,
        'duration_s':   round(duration, 4) if duration is not None else None,
        'duration_src': dur_src,
        'cpu_s':        round(cpu_s, 4),
        'cpu_scope':    'process' if 'signal' in F else 'leader',
        'child_cpu_s':  null_if_off(round((e.cutime_ns + e.cstime_ns) / 1e9, 4), 'signal', F),
        'cpu_pct':      (round(100.0 * cpu_s / duration, 1) if duration and duration >= 0.01 else None),
        'peak_rss_mb':  null_if_off(round(e.hiwater_rss_pages * PAGE_SIZE / 1048576.0, 2),
                                    'signal', F),
        'threads':      null_if_off(t.nr_threads, 'signal', F),
        'min_flt':      t.min_flt,
        'maj_flt':      t.maj_flt,
        'cmaj_flt':     null_if_off(e.cmaj_flt, 'signal', F),
        'nvcsw':        t.nvcsw,
        'nivcsw':       t.nivcsw,
        'sched_run_s':  round(t.run_ns / 1e9, 4),
        'sched_wait_s': null_if_off(round(t.wait_ns / 1e9, 4), 'sched', F),
        'blkio_wait_s':     null_if_off(round(t.blkio_ns / 1e9, 4), 'delay', F),
        'swapin_wait_s':    null_if_off(round(t.swapin_ns / 1e9, 4), 'delay', F),
        'freepages_wait_s': null_if_off(round(t.freepages_ns / 1e9, 4), 'delay', F),
        # item 2: uninterruptible sleep, which delay accounting does NOT cover —
        # a task parked in autofs_mount_wait shows 0 blkio_wait_s and a real
        # dstate_wait_s, and that wedge is the login nodes' known failure mode.
        'dstate_wait_s':  ds_s,
        'dstate_episodes': ds_n,
        'dstate_max_s':   ds_max,
        'dstate_src':     ds_src,
        # item 3b: all protocols, so QUIC over UDP is counted
        'net_tx_bytes': (e.net_tx if getattr(e, 'has_net', 0) else 0) if 'netbytes' in F else None,
        'net_rx_bytes': (e.net_rx if getattr(e, 'has_net', 0) else 0) if 'netbytes' in F else None,
        'net_calls':    (e.net_calls if getattr(e, 'has_net', 0) else 0) if 'netbytes' in F else None,
        'pgrp':  e.pgrp if F & PIDS_BLOCKS else None,
        'sid':   e.sid if F & PIDS_BLOCKS else None,
        'exit_code': exit_code, 'signal': sig, 'core_dumped': core,
        'io': ({'rd_mb': round(t.rd_bytes / 1e6, 3), 'wr_mb': round(t.wr_bytes / 1e6, 3),
                'rchar_mb': round(t.rchar / 1e6, 3), 'wchar_mb': round(t.wchar / 1e6, 3),
                'scope': 'process' if 'signal' in F else 'leader'} if 'io' in F else None),
        # poller-compat placeholders (this record is a strict superset of proc_trace's)
        'samples': None, 'state_last': None,
    })
    return rec, quiet


# ---------------------------------------------------------------------------
# deferred exit records (schema 6)
# ---------------------------------------------------------------------------

def _stage_exit(m, e):
    """Hold an exit for EXIT_HOLD_MS, then write its record with its `conns`.

    The hold exists for one kernel ordering fact: do_exit() runs
    trace_sched_process_exit() BEFORE exit_files(), so the TCP_CLOSE events for
    the sockets this process still held are submitted AFTER the exit event.
    Writing inline would fold in only the connections that happened to close
    during the process's life and miss every one it was still holding -- for an
    agent, nearly all of them. The record itself is built at the end of the hold
    (_exit_record), so whatever else arrived late lands in it too.

    Cost of the hold is ordering in the FILE only: `ts_epoch` is when the exit
    was handled, not when the record was written, so a consumer that sorts on it
    (which is what it is for) is unaffected. Nothing is buffered that a stop
    would lose -- drain_exits(final=1) runs before the stop record."""
    m['exit_pending'] = True
    ts = time.time()
    if EXIT_HOLD_MS <= 0:
        _netp_collect([m])
        _write_exit(m, e, ts)
        return
    _pending_exits.append((time.monotonic() + EXIT_HOLD_MS / 1000.0, m, e, ts))


def _write_exit(m, e, ts=None):
    m['exit_pending'] = False
    actor, attribution = actor_of(m)
    if actor is None:
        _note_dropped(m)
        _netp_release(m)
        _thread_acc_release(e)
        return
    if not INCLUDE_ROOTS and m['is_agent']:
        _netp_release(m)
        _thread_acc_release(e)
        return
    rec, quiet = _exit_record(m, e, actor, attribution, ts)
    if quiet and not m.get('conns') and not m['net_io']:
        _netp_release(m)
        return
    rec['conns'] = _conns_block(m)
    rec['net_endpoints'] = _net_endpoints_block(m, rec.get('net_tx_bytes'),
                                                rec.get('net_rx_bytes'))
    if m['in_series']:
        # the remainder since its last tick, so the series sums to the lifetime
        _emit_netio(m, rec['actor'], rec.get('attribution'), 'exit', time.time())
    writer.write(rec)
    m['emitted'] = True
    _netp_release(m)
    if m.get('is_submit') or m.get('submit_buf') is not None:
        _emit_submit(m, rec)


def drain_exits(final=False):
    """Write every staged exit record whose hold has elapsed. Called once per
    poll round, and with final=True at shutdown so nothing is stranded.

    _pending_exits is append-only with a constant hold, so it is already in due
    order and the first record that is not due ends the scan. A round that is
    about to write a networked process reads the endpoint table first, once for
    the whole round: its counters are final now that the process has exited."""
    if not _pending_exits:
        return 0
    now = time.monotonic()
    due = []
    while _pending_exits:
        if not final and _pending_exits[0][0] > now:
            break
        due.append(_pending_exits.popleft())
    try:
        _netp_collect([m for _d, m, _e, _ts in due])
    except Exception:
        _count_error('netpeer_read')
    for _due, m, e, ts in due:
        try:
            _write_exit(m, e, ts)
        except Exception:
            _count_error('_write_exit')
    return len(due)


# ---------------------------------------------------------------------------
# submit capture (item: exact process -> Slurm job key)
# ---------------------------------------------------------------------------

_submit_tab = None        # BPF hash `submit_pids` (submit block), set by main


def on_write(w):
    m = proc_add(w.pid, None)
    m['is_submit'] = True
    raw = ctypes.string_at(ctypes.addressof(w) + type(w).buf.offset, w.len)
    buf = m.get('submit_buf') or b''
    keep = max(SUBMIT_BUDGET, 0)
    if len(buf) < keep:
        buf = m['submit_buf'] = buf + raw[:keep - len(buf)]
    # The job id is all this capture is for; once it has been printed, stop the
    # kernel sending this process's writes at all.
    if not m.get('submit_done') and parse_job_ids(buf.decode('utf-8', 'replace')):
        m['submit_done'] = True
        _submit_stop(w.pid)


def _submit_stop(pid):
    tab = _submit_tab
    if tab is None:
        return
    try:
        del tab[tab.Key(pid)]
    except Exception:
        pass


def _interactive_submit(m):
    """An `srun --pty` session: its captured output is the user's terminal."""
    return '--pty' in (m.get('args') or '').split(' ')


def _emit_submit(m, exit_rec):
    text = (m.get('submit_buf') or b'').decode('utf-8', 'replace')
    jobs = parse_job_ids(text)
    tail = text[-300:] if text else None
    if tail and _interactive_submit(m):
        tail = None
    elif tail and REDACT_KEYS:
        try:
            tail = redact_line(tail, keys=True, prompts=False)[0]
        except Exception:
            _count_error('redact')
            tail = None
    rec = _envelope('submit')
    rec.update(_base_identity(m, exit_rec['actor'], exit_rec.get('attribution')))
    req = parse_submit_args(m.get('args'))
    rec.update(req)          # partition, gpus, array, time_limit_s, mem, cpus_per_task, req_src
    rec.update({
        'tool': exit_rec['command'],
        'job_id': jobs[0] if jobs else None,
        'job_ids': jobs or None,
        # the tool's cwd IS the job's submit directory; named for the join rather
        # than for the kernel, because downstream groups on `work_dir`
        'work_dir': m.get('cwd'),
        'exit_code': exit_rec['exit_code'], 'signal': exit_rec['signal'],
        'start': exit_rec['start'], 'duration_s': exit_rec['duration_s'],
        'captured_bytes': len(text),
        # the last 300 characters, credentials masked; null for an interactive
        # (--pty) session, whose output is the user's own terminal
        'captured_tail': tail,
    })
    writer.write(rec)


# ---------------------------------------------------------------------------
# network records
# ---------------------------------------------------------------------------

_cidr_index = None


def _ip_of(ev, v4field, v6field, family):
    """Address out of a BPF struct. The kernel memcpy'd the raw address bytes into
    a u32/u128, so on a little-endian host the first byte is least significant."""
    if family == 2:  # AF_INET
        return str(ipaddress.IPv4Address(getattr(ev, v4field).to_bytes(4, 'little')))
    raw = getattr(ev, v6field)
    try:
        b = raw.to_bytes(16, 'little')
    except AttributeError:            # ctypes may hand back a byte array
        b = bytes(raw)
    return str(ipaddress.IPv6Address(b))


def _provider(ip):
    return _cidr_index.classify(ip) if _cidr_index is not None else None


def _new_conns():
    """The per-process connection accumulator. Created lazily: a `conns` of None
    means the process opened none, and most processes open none, so this must not
    cost a dict on every entry."""
    return {'n': 0, 'accepted': 0, 'established': 0, 'failed': 0, 'external': 0,
            'rx_bytes': 0, 'tx_bytes': 0, 'rx_known': False, 'tx_known': False,
            'dur_s': 0.0, 'connect_ms': [], 'providers': {}, 'peers': {}}


def _conn_fold(m, *, daddr, dport, external, provider, inbound, established,
               dur_s=None, connect_ms=None, rx=None, tx=None):
    """Accumulate one closed/accepted connection onto its process.

    Endpoints collapse on (daddr, dport): twenty sequential requests to one API
    host were twenty records and are now one peer with n=20, which is the whole
    point of folding -- an agent's traffic is overwhelmingly repeat calls to a
    handful of hosts."""
    c = m.get('conns')
    if c is None:
        c = m['conns'] = _new_conns()
    c['n'] += 1
    if inbound:
        c['accepted'] += 1
    if established:
        c['established'] += 1
    elif not inbound:
        # SYN_SENT straight to CLOSE with no ESTABLISHED is a refused or
        # timed-out connect, which is a provider-health signal. Only meaningful
        # outbound: an accept is established by construction.
        c['failed'] += 1
    if external:
        c['external'] += 1
    # rx/tx are null-not-zero when the kernel block that reads them did not load,
    # so track whether ANY connection contributed a real figure.
    if rx is not None:
        c['rx_bytes'] += rx
        c['rx_known'] = True
    if tx is not None:
        c['tx_bytes'] += tx
        c['tx_known'] = True
    if dur_s:
        c['dur_s'] += dur_s
    if connect_ms is not None and len(c['connect_ms']) < 1024:
        c['connect_ms'].append(connect_ms)
    if provider:
        c['providers'][provider] = c['providers'].get(provider, 0) + 1
    # Unbounded peer keys are the one real width risk here (a port scan would be
    # one key per port), so the map is capped and the overflow is counted rather
    # than silently merged into a peer it does not belong to.
    key = '%s:%d' % (daddr, dport)
    peers = c['peers']
    e = peers.get(key)
    if e is None:
        if len(peers) >= _PEER_KEYS_MAX:
            c['peers_overflow'] = c.get('peers_overflow', 0) + 1
            return
        e = peers[key] = {'daddr': daddr, 'dport': dport, 'n': 0,
                          'external': external, 'provider': provider,
                          'inbound': 0, 'failed': 0,
                          'rx_bytes': 0, 'tx_bytes': 0}
    e['n'] += 1
    if inbound:
        e['inbound'] += 1
    elif not established:
        e['failed'] += 1
    if rx:
        e['rx_bytes'] += rx
    if tx:
        e['tx_bytes'] += tx


_PEER_KEYS_MAX = 256


def _conns_block(m):
    """The `conns` field of an exit/truncated record, or None if the process
    never opened a connection.

    None for "no connections" rather than a zeroed block: the block is ~200 B and
    would otherwise land on every exit record on the node, against the width rule
    the schema-5 note argues. Whether the TCP blocks loaded at all is a property
    of the capture, not of the process, and is recorded once in `meta.features`."""
    c = m.get('conns')
    if not c or not c['n']:
        return None
    peers = sorted(c['peers'].values(),
                   key=lambda e: (-(e['rx_bytes'] + e['tx_bytes']), -e['n']))
    cm = sorted(c['connect_ms'])
    out = {
        'n': c['n'],
        'accepted': c['accepted'],
        'established': c['established'],
        # outbound connects that never reached ESTABLISHED
        'failed': c['failed'],
        'external': c['external'],
        'rx_bytes': c['rx_bytes'] if c['rx_known'] else None,
        'tx_bytes': c['tx_bytes'] if c['tx_known'] else None,
        # summed connection lifetimes, NOT wall time: connections overlap, so
        # this can exceed the process's own duration_s and is not a rate base
        'conn_s_total': round(c['dur_s'], 3),
        'connect_ms_p50': cm[len(cm) // 2] if cm else None,
        'connect_ms_max': cm[-1] if cm else None,
        'providers': _top_n(c['providers'], CONNS_TOPN, or_none=True),
        'peers': peers[:CONNS_TOPN],
        'peers_total': len(c['peers']),
    }
    if c.get('peers_overflow'):
        out['peers_overflow'] = c['peers_overflow']
    return out


def on_tcp(t):
    """Fold one closed (kind=1) or accepted (kind=2) connection onto its process.

    Emits nothing. Through schema 5 this wrote one `tcp`/`accept` record per
    connection; both kinds are gone and the connection now reaches the feed only
    inside the owning process's `exit` (or `truncated`) record. See _stage_exit()
    for why that record has to wait."""
    m = proc.get(t.pid)
    if m is None:
        if not TCP_ALL:
            return
        # No fork/exec was ever seen for this pid -- it predates the collector.
        # Give it an entry so the connection has somewhere to live; whether it
        # ever reaches the feed depends on the pid becoming attributable before
        # it exits, and _note_conn_dropped() counts it when it does not.
        m = proc_add(t.pid, None, comm=_cstr(t.comm) or None)
        if m['uid'] is None:
            m['uid'], m['user'] = t.uid, username(t.uid)
    elif actor_of(m)[0] is None and not TCP_ALL:
        return
    daddr = _ip_of(t, 'daddr_v4', 'daddr_v6', t.family)
    inbound = bool(t.inbound)
    established = True if t.kind == TCP_KIND_ACCEPT else bool(t.established)
    _conn_fold(
        m, daddr=daddr, dport=t.dport, external=is_external_ip(daddr),
        provider=_provider(daddr), inbound=inbound, established=established,
        dur_s=(round(t.dur_ns / 1e9, 3) if t.kind != TCP_KIND_ACCEPT else None),
        connect_ms=(round(t.connect_ns / 1e6, 2)
                    if t.kind != TCP_KIND_ACCEPT and t.established and t.connect_ns
                    else None),
        rx=(t.rx_bytes if (t.kind != TCP_KIND_ACCEPT and t.has_bytes) else None),
        tx=(t.tx_bytes if (t.kind != TCP_KIND_ACCEPT and t.has_bytes) else None),
    )


def _note_conn_dropped(m):
    """A process with folded connections whose exit record is not emitted takes
    them with it. Counted so the loss is visible in `stop` instead of silent --
    the same contract `_dropped` and conn_tick's `unmatched_births` keep."""
    c = m.get('conns')
    if not c or not c['n']:
        return
    _conn_dropped['n'] += c['n']
    k = _command_of(m) or '?'
    _conn_dropped['commands'][k] = _conn_dropped['commands'].get(k, 0) + c['n']


def conn_tick(b):
    """Item 3a: one `conn` record per socket that is STILL OPEN.

    The BPF birth map supplies pid, agent ancestry and the true connection birth
    time; netlink socket-diag supplies what the kernel knows about a live socket
    (state, RTT, retransmits, byte totals, idle times) and has no pid at all. The
    join key is the four-tuple. Neither half is sufficient alone."""
    rows = []
    if sockdiag is not None:
        try:
            rows = sockdiag.dump()
        except Exception:
            rows = []
    by_key = {}
    for r in rows:
        # The birth map is TCP-only, and a UDP socket can share a TCP one's
        # four-tuple: a join on the tuple alone would hand one's stats to the other.
        if _is_tcp_row(r):
            by_key[sockdiag.key_of(r)] = r
    now_ktime = _ktime_now()
    matched = set()
    n_emitted = 0
    out_records = []
    by_tree, by_a3 = {}, {}
    unmatched_births = 0

    def _count(m_, external_, established_):
        """Counted ABOVE the external filter, deliberately. If counting happened
        after it, flipping EBPFM_CONN_ALL would silently change what
        tcp_open_ext MEANS rather than how much is emitted."""
        if m_ is None:
            return
        a_, _ = actor_of(m_)
        if a_ is None:
            return
        # Key on tree_root, matching the residency loop exactly (:tree accumulator).
        # Keying on agent_pid instead would file a VS Code helper's sockets under
        # a key that has no residency record, and the count would vanish.
        k = tree_root(m_['pid']) or m_.get('agent_pid')
        for d_, kk in ((by_tree, k), (by_a3, actor3(a_, m_.get('agent_type')))):
            if kk is None:
                continue
            e = d_.setdefault(kk, {'open': 0, 'ext': 0, 'estab': 0})
            e['open'] += 1
            e['ext'] += 1 if external_ else 0
            e['estab'] += 1 if established_ else 0
    if (TCP_BLOCKS | {'tcp_accept'}) & FEATURES:
        try:
            items = list(b['tcp_birth'].items())
        except Exception:
            items = []
        for _k, v in items:
            fam = v.family
            saddr = _ip_of(v, 'saddr_v4', 'saddr_v6', fam)
            daddr = _ip_of(v, 'daddr_v4', 'daddr_v6', fam)
            key = (saddr, v.sport, daddr, v.dport)
            row = by_key.get(key)
            if row is not None:
                matched.add(key)
            external = is_external_ip(daddr)
            m = proc.get(v.pid)
            if row is None and v.estab_ts:
                # Established once, but netlink has no row for it now: the socket
                # is gone and the CLOSE event never arrived. Only deletion path is
                # on TCP_CLOSE, so these leak -- a phantom conn record every tick
                # forever, and eventually a full map after which NEW births are
                # silently dropped. Counted here so the leak is observable.
                unmatched_births += 1
            _count(m, external, bool(v.estab_ts))
            if not external and not CONN_ALL:
                continue
            actor, attribution = actor_of(m) if m is not None else (None, None)
            rec = _envelope('conn')
            if m is not None and actor is not None:
                rec.update(_base_identity(m, actor, attribution))
            else:
                rec.update(_stub_identity(v.pid, v.uid, _cstr(v.comm)))
            rec.update({
                'family': 'inet6' if fam == 10 else 'inet', 'proto': 'tcp',
                'saddr': saddr, 'sport': v.sport, 'daddr': daddr, 'dport': v.dport,
                'external': external, 'provider': _provider(daddr),
                'inbound': bool(v.inbound),
                'age_s': round((now_ktime - v.ts) / 1e9, 1) if v.ts else None,
                'established': bool(v.estab_ts),
                'stats_src': 'netlink' if row else None,
            })
            rec.update(_conn_stats(row))
            out_records.append(rec)
            n_emitted += 1
    # netlink rows we never saw a connect or accept for: opened before we
    # attached, or inbound on a kernel where the accept probe did not load. They
    # carry uid but no pid, which is exactly what nettcp reports — so emitting
    # them makes this feed a superset of it. Connected UDP sockets land here too
    # (they report ESTABLISHED), which is what `proto` is for.
    if CONN_UNTRACKED:
        for r in rows:
            k = sockdiag.key_of(r)
            if _is_tcp_row(r) and k in matched:
                continue
            if r['state'] != 'ESTABLISHED':
                continue
            if not is_external_ip(r['daddr']) and not CONN_ALL:
                continue
            rec = _envelope('conn')
            rec.update(_stub_identity(None, r['uid'], None))
            rec.update({
                   'family': r['family'], 'proto': r.get('proto'),
                   'saddr': r['saddr'], 'sport': r['sport'],
                   'daddr': r['daddr'], 'dport': r['dport'],
                   'external': is_external_ip(r['daddr']), 'provider': _provider(r['daddr']),
                   'inbound': None, 'age_s': None, 'established': True,
                   'stats_src': 'netlink'})
            rec.update(_conn_stats(r))
            out_records.append(rec)
            n_emitted += 1
    return {'n': n_emitted, 'by_tree': by_tree, 'by_actor3': by_a3,
            'records': out_records, 'netlink_rows': len(rows),
            'unmatched_births': unmatched_births}


def _is_tcp_row(r):
    """A netlink row from the TCP dump. A row with no `proto` at all predates the
    stamp, and every row was treated as TCP then."""
    return r.get('proto') in ('tcp', None)


def _conn_stats(row):
    """tcp_info fields for a conn record, or nulls when netlink had nothing."""
    if not row:
        return {'state': None, 'rtt_ms': None, 'retrans': None, 'rx_bytes': None,
                'tx_bytes': None, 'idle_send_s': None, 'idle_recv_s': None,
                'segs_in': None, 'segs_out': None, 'rqueue': None, 'wqueue': None}
    i = row.get('info') or {}
    return {
        'state': row['state'],
        'rtt_ms': round(i['rtt_us'] / 1000.0, 2) if 'rtt_us' in i else None,
        'retrans': i.get('total_retrans'),
        'rx_bytes': i.get('bytes_received'),
        'tx_bytes': i.get('bytes_acked'),
        'idle_send_s': round(i['last_data_sent'] / 1000.0, 1) if 'last_data_sent' in i else None,
        'idle_recv_s': round(i['last_data_recv'] / 1000.0, 1) if 'last_data_recv' in i else None,
        'segs_in': i.get('segs_in'), 'segs_out': i.get('segs_out'),
        'rqueue': row.get('rqueue'), 'wqueue': row.get('wqueue'),
    }


# ---------------------------------------------------------------------------
# schema 7: network bytes by endpoint (the netpeer_* blocks)
# ---------------------------------------------------------------------------
#
# The kernel keeps cumulative (tx, rx, calls) per (process, protocol, direction,
# address, port) in `netp`, and announces each new entry on `netp_new`, from
# which userspace keeps an index of every process's entries. It reads a
# process's own entries, by the index, before writing its record and -- for the
# processes it reports -- at every residency tick; the whole table only every
# NETP_SWEEP_TICKS ticks, or sooner once it fills, to catch what the index missed.
# The latest figures sit on the process entry, and two things come from them:
#
#   net_endpoints  on exit/truncated: the process's complete lifetime list
#   netio          at each tick: the bytes each endpoint moved since the
#                  process's previous netio record -- the time series
#
# A process joins the series at its first tick with traffic, and from then on its
# exit (or the stop) writes one last netio record for the remainder, so for a
# series process the netio records alone sum to its lifetime. A process that
# never saw a tick has `in_series: false`, and all of its traffic happened within
# one tick of its exit record: it would otherwise have been in the series.

_NETP_PROTO = {6: 'tcp', 17: 'udp'}
_netp_tab = None          # BPF hash `netp`, when a netpeer block loaded (set by main)
_netp_drop_tab = None     # BPF array `netp_drop`: [calls, tx, rx] that found netp full
# entries deleted without reaching a record: a pid never seen, or bytes that
# arrived after the record was written (a thread outliving its group leader)
_netp_orphans = {'entries': 0, 'tx_bytes': 0, 'rx_bytes': 0, 'calls': 0}
_netp_unclaimed = {}      # (tgid, key) -> 1: seen at the last read with no known pid
_netio_stats = {'records': 0}
_netp_kern_tab = None     # BPF array `netp_kern`: [calls, tx, rx] on kernel sockets
# tgid -> {endpoint key: BPF key}: every entry each process has in `netp`, from
# the netp_new announcements and from each sweep
_netp_index = {}
NETP_SWEEP_TICKS = 10     # a full read of the table every this many ticks
_netp_sweep = {'tick': 0, 'full_calls': 0, 'fallback': 0.0}
_COLLECTOR_START = time.time()     # reset by main(); floors a process's first interval


def netp_endpoint(proto, flags, port, addr):
    """A `netp` key -> (proto, addr, port, inbound); addr, port and inbound are
    None for the unattributed bucket. `addr` is the key's 16 raw bytes, a v4
    address in the first four. A v4-mapped address reads as the v4 it maps, so a
    dual-stack socket's peer and a plain v4 socket's are one endpoint."""
    p = _NETP_PROTO.get(proto, str(proto))
    if flags & NETP_F_UNKNOWN:
        return p, None, None, None
    raw = bytes(addr)
    if flags & NETP_F_V6:
        a = ipaddress.IPv6Address(raw[:16])
        a = a.ipv4_mapped or a
    else:
        a = ipaddress.IPv4Address(raw[:4])
    return p, str(a), port, bool(flags & NETP_F_INBOUND)


def netpeer_read():
    """Fold the kernel's endpoint table onto the process table. Returns the
    number of entries read, or None when there is no table.

    The kernel's counters are cumulative, so a read REPLACES a process's figures
    rather than adding to them. An entry is deleted only where nothing can add to
    it any more -- a live process's entry never is, because a delete would race
    its increments:
      * the process's record is already written: counted as orphaned, deleted
      * the pid is unknown: left for one more read (its fork event may still be
        queued behind this one), then counted as orphaned and deleted
      * the process exited without a record of its own (untracked, filtered):
        deleted, not counted -- it was never going to be reported"""
    global _netp_unclaimed
    tab = _netp_tab
    if tab is None:
        return None
    items = None
    if hasattr(tab, 'items_lookup_batch'):
        try:
            # one syscall per batch, where items() costs two per entry; the
            # kernel has it from 5.6 (4.18 answers EINVAL)
            items = list(tab.items_lookup_batch())
        except Exception:
            items = None
    if items is None:
        try:
            items = list(tab.items())
        except Exception:
            return None
    unclaimed, drop = {}, []
    for k, v in items:
        kk = (k.proto, k.flags, k.port, bytes(k.addr))
        m = proc.get(k.tgid)
        orphan = False
        if m is None:
            if (k.tgid,) + kk in _netp_unclaimed:
                orphan = True
            else:
                unclaimed[(k.tgid,) + kk] = 1
                continue
        elif m['netp_done']:
            orphan = True
        elif m['exited'] and not m['exit_pending']:
            drop.append(k)
            continue
        if orphan:
            _netp_orphans['entries'] += 1
            _netp_orphans['tx_bytes'] += v.tx
            _netp_orphans['rx_bytes'] += v.rx
            _netp_orphans['calls'] += v.calls
            drop.append(k)
            continue
        if m['netp_k'] is None:
            m['netp_k'], m['netp_kobj'] = {}, {}
        m['netp_k'][kk] = (v.tx, v.rx, v.calls)
        m['netp_kobj'][kk] = k
        _netp_index.setdefault(k.tgid, {})[kk] = k    # an announcement may be lost
    _netp_unclaimed = unclaimed
    for k in drop:
        idx = _netp_index.get(k.tgid)
        if idx is not None:
            idx.pop((k.proto, k.flags, k.port, bytes(k.addr)), None)
            if not idx:
                del _netp_index[k.tgid]
        try:
            del tab[k]
        except Exception:
            pass
    return len(items)


def _netp_index_add(k):
    """Index one new `netp` entry under its process (a netp_new event)."""
    _netp_index.setdefault(k.tgid, {})[(k.proto, k.flags, k.port, bytes(k.addr))] = k


def _netp_read_pid(m):
    """Fold a process's own entries onto it, by the index: two syscalls per
    entry it has, against two per entry in the table for netpeer_read(). Returns
    how many were read."""
    tab = _netp_tab
    keys = _netp_index.get(m['pid'])
    if tab is None or not keys:
        return 0
    n = 0
    for kk, k in list(keys.items()):
        try:
            v = tab[k]
        except KeyError:
            del keys[kk]                         # a sweep deleted it
            continue
        if m['netp_k'] is None:
            m['netp_k'], m['netp_kobj'] = {}, {}
        m['netp_k'][kk] = (v.tx, v.rx, v.calls)
        m['netp_kobj'][kk] = k
        n += 1
    return n


def _netp_collect(ms):
    """Bring the endpoint counters of processes about to be written up to date.
    A process that moved bytes but has nothing indexed lost its announcements
    (a full ring); for those the whole table is read, at most once a second."""
    if _netp_tab is None:
        return
    full = False
    for m in ms:
        if m['pid'] in _netp_index:
            _netp_read_pid(m)
        elif m['net_io']:
            full = True
    now = time.monotonic()
    if full and now - _netp_sweep['fallback'] >= 1.0:
        _netp_sweep['fallback'] = now
        netpeer_read()


def _netp_refresh_live():
    """The tick's read: every live, reported process's own entries."""
    for pid in list(_netp_index):
        m = proc.get(pid)
        if m is not None and not m['exited'] and actor_of(m)[0] is not None:
            _netp_read_pid(m)


def _netp_sweep_due():
    """A full read on the first tick, every NETP_SWEEP_TICKS, and on any tick
    after the table was found full -- that is when entries leaked past the index
    cost the most."""
    _netp_sweep['tick'] += 1
    drop = _read_netp_drop()
    calls = drop['calls'] if drop else 0
    filled = calls > _netp_sweep['full_calls']
    _netp_sweep['full_calls'] = calls
    return filled or (_netp_sweep['tick'] - 1) % NETP_SWEEP_TICKS == 0


def _netp_release(m):
    """Delete a finished process's kernel entries, the indexed ones included.
    Safe once it has exited: nothing but its own sends and receives adds to them."""
    tab = _netp_tab
    keys = dict(m['netp_kobj'] or {})
    keys.update(_netp_index.pop(m['pid'], None) or {})
    if tab is not None:
        for k in keys.values():
            try:
                del tab[k]
            except Exception:
                pass
    m['netp_kobj'] = None
    m['netp_done'] = True


def _netp_lifetime(m):
    """{(proto, addr, port, inbound): [tx, rx, calls]} over the process's life.
    Sums, because two kernel keys can decode to one endpoint."""
    out = {}
    for (proto, flags, port, addr), (tx, rx, calls) in (m['netp_k'] or {}).items():
        a = out.setdefault(netp_endpoint(proto, flags, port, addr), [0, 0, 0])
        a[0] += tx
        a[1] += rx
        a[2] += calls
    return out


# ---- endpoint names, from the process's own command line --------------------
#
# An endpoint is an address, and the name behind it is often in the argv that
# made the connection: curl's URL, the ssh that git spawns for a remote, scp's
# host:path. Those names are resolved on a background thread, and an endpoint
# carries one as `host` only when its address is among the name's addresses.
# Never a guess: a name absent from argv, not yet resolved, or answered with
# other addresses (a CDN rotating them) leaves `host` null. Only hostnames are
# taken -- a URL's credentials never are -- only for a process that is reported,
# and nothing is read from the traffic itself.

_URL_HOST = re.compile(r'[A-Za-z][A-Za-z0-9+.-]*://(?:[^\s/@]*@)?'
                       r'(\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9._-]+)')
_SSH_COMMANDS = {'ssh', 'autossh', 'sftp', 'mosh'}     # destination: first non-option
_HOSTPATH_COMMANDS = {'scp', 'rsync'}                   # [user@]host:path arguments
_SSH_OPT_ARG = set('BbcDEeFIiJLlmOoPpQRSWw')          # ssh(1) options that take a value
_HOST_PATH = re.compile(r'^(?:[^@\s/]+@)?([A-Za-z0-9._-]+):')
_SSH_DEST = re.compile(r'^(?:[^@\s/]+@)?([A-Za-z0-9._-]+)$')
_ARGV_HOSTS_MAX = 8                                     # names taken per process


def _ssh_destination(toks):
    """ssh's destination: the first argument that is neither an option nor an
    option's value (getopt rules: `-p 22`, `-p22`, `-vp 22`)."""
    i = 0
    while i < len(toks):
        t = toks[i]
        if not t:
            i += 1
            continue
        if t == '--':
            return toks[i + 1] if i + 1 < len(toks) else None
        if t.startswith('-') and len(t) > 1:
            for j, ch in enumerate(t[1:], 1):
                if ch in _SSH_OPT_ARG:
                    if j == len(t) - 1:
                        i += 1                  # its value is the next token
                    break
            i += 1
            continue
        return t
    return None


def _argv_hostnames(args, command=None):
    """The hostnames `args` names, in order, without repeats: every URL's host,
    and the destination of an ssh/sftp/scp/rsync. Address literals and localhost
    need no name and are left out, and an ordinary argument -- a file name, an
    email address -- is never taken for one."""
    if not args:
        return []
    found = _URL_HOST.findall(args) if '://' in args else []
    if command in _SSH_COMMANDS:
        dest = _ssh_destination(args.split(' ')[1:])
        mt = dest and (_SSH_DEST.match(dest) or _HOST_PATH.match(dest))
        if mt:
            found.append(mt.group(1))
    elif command in _HOSTPATH_COMMANDS:
        for t in args.split(' ')[1:]:
            mt = _HOST_PATH.match(t)
            if mt:
                found.append(mt.group(1))
    out = []
    for h in found:
        h = h.strip('[]').rstrip('.').lower()
        if (not h or h in out or h == 'localhost' or h.endswith('.localhost')
                or not re.search('[a-z]', h)):
            continue
        try:
            ipaddress.ip_address(h)
            continue                            # already an address
        except ValueError:
            pass
        out.append(h)
        if len(out) >= _ARGV_HOSTS_MAX:
            break
    return out


HOST_TTL_S, HOST_FAIL_TTL_S = 600.0, 300.0
_HOST_CACHE_MAX = 4096
_HOST_WANT = queue.Queue(maxsize=256)   # names waiting for the lookup thread
_HOST_CACHE = {}          # name -> (frozenset of addresses, expiry on time.monotonic())
_host_pending = set()
_host_stats = {'resolved': 0, 'failed': 0, 'dropped': 0}
_host_getaddrinfo = socket.getaddrinfo     # the one blocking call, on the worker thread


def _want_host(name):
    """Queue `name` for the lookup thread, unless it is fresh in the cache or
    already queued. Never blocks: a full queue drops the name, counted in
    `_host_stats`, and it can be asked for again later."""
    hit = _HOST_CACHE.get(name)
    if (hit is not None and hit[1] > time.monotonic()) or name in _host_pending:
        return
    _host_pending.add(name)
    try:
        _HOST_WANT.put_nowait(name)
    except queue.Full:
        _host_pending.discard(name)
        _host_stats['dropped'] += 1


def _resolve_host(name):
    """One lookup, as the worker thread does it. A failure is cached too, for
    less long, so a dead name is not looked up on every exec that mentions it."""
    try:
        ips = set()
        for _f, _t, _p, _c, sa in _host_getaddrinfo(name, None, 0, socket.SOCK_STREAM):
            try:
                a = ipaddress.ip_address(sa[0].split('%', 1)[0])
            except ValueError:
                continue
            ips.add(str(getattr(a, 'ipv4_mapped', None) or a))
        ttl = HOST_TTL_S
        _host_stats['resolved'] += 1
    except Exception:
        ips, ttl = set(), HOST_FAIL_TTL_S
        _host_stats['failed'] += 1
    if name not in _HOST_CACHE and len(_HOST_CACHE) >= _HOST_CACHE_MAX:
        _HOST_CACHE.pop(next(iter(_HOST_CACHE)), None)     # the oldest
    _HOST_CACHE[name] = (frozenset(ips), time.monotonic() + ttl)
    _host_pending.discard(name)


def _host_ips(name):
    """The addresses `name` resolved to, set() if it did not, None if unknown."""
    hit = _HOST_CACHE.get(name)
    return set(hit[0]) if hit is not None else None


def _host_worker():
    while True:
        _resolve_host(_HOST_WANT.get())


def _ask_hosts(m):
    """Queue the names m's argv gives, for a process that will be reported.
    Not callable while seeding: actor_of() memoizes, and a parent not ingested
    yet would pin the child as unattributed."""
    if not WANT_ARGV_HOSTS:
        return
    names = _argv_hostnames(m.get('args'), _command_of(m))
    if names and actor_of(m)[0] is not None:
        for n in names:
            _want_host(n)


def _hosts_for(m):
    """{address: name} for the names m's argv gives, from what is resolved."""
    if not WANT_ARGV_HOSTS:
        return {}
    out = {}
    for name in _argv_hostnames(m.get('args'), _command_of(m)):
        for ip in _host_ips(name) or ():
            out.setdefault(ip, name)
    return out


def _endpoint_list(agg, hosts=None):
    """{endpoint: (tx, rx, calls)} -> (entries, unattributed, overflow, n).

    Entries are sorted by bytes and cut at NET_ENDPOINTS_MAX; whatever is past
    the cut is summed into `overflow`, so the list's totals still add up. `n` is
    the true number of distinct endpoints."""
    un = [0, 0, 0]
    rows = []
    for (proto, addr, port, inbound), (tx, rx, calls) in agg.items():
        if addr is None:
            un[0] += tx
            un[1] += rx
            un[2] += calls
            continue
        rows.append({'proto': proto, 'addr': addr, 'port': port,
                     'host': (hosts or {}).get(addr), 'inbound': inbound,
                     'external': is_external_ip(addr), 'provider': _provider(addr),
                     'tx_bytes': tx, 'rx_bytes': rx, 'calls': calls})
    rows.sort(key=lambda e: (-(e['tx_bytes'] + e['rx_bytes']), -e['calls'],
                             e['proto'], e['addr'], e['port']))
    cap = max(0, NET_ENDPOINTS_MAX)
    rest = rows[cap:]
    over = ({'n': len(rest), 'tx_bytes': sum(e['tx_bytes'] for e in rest),
             'rx_bytes': sum(e['rx_bytes'] for e in rest),
             'calls': sum(e['calls'] for e in rest)} if rest else None)
    unat = {'tx_bytes': un[0], 'rx_bytes': un[1], 'calls': un[2]} if any(un) else None
    return rows[:cap], unat, over, len(rows)


def _net_endpoints_block(m, net_tx=None, net_rx=None):
    """The `net_endpoints` field of an exit/truncated record.

    None when the netpeer blocks did not load, or for a process with no network
    I/O at all -- the rule `conns` follows, for the same width reason.
    `net_tx`/`net_rx` are the process's netbytes totals: the same probe feeds
    both tables, so any excess over this list is bytes the endpoint table missed
    (full, or the call was already in flight at attach), reported as
    `unaccounted` rather than left for a reader to discover."""
    if not (NETPEER_BLOCKS & FEATURES):
        return None
    agg = _netp_lifetime(m)
    if not agg and not (net_tx or net_rx):
        return None
    eps, unat, over, n = _endpoint_list(agg, _hosts_for(m))
    out = {'n': n, 'in_series': bool(m['in_series']), 'endpoints': eps,
           'unattributed': unat}
    if over:
        out['overflow'] = over
    miss_tx = max(0, net_tx - sum(v[0] for v in agg.values())) if net_tx is not None else 0
    miss_rx = max(0, net_rx - sum(v[1] for v in agg.values())) if net_rx is not None else 0
    if miss_tx or miss_rx:
        out['unaccounted'] = {'tx_bytes': miss_tx, 'rx_bytes': miss_rx}
    return out


def _start_epoch_of(m):
    if m.get('exec_ktime') is not None and BTIME:
        return BTIME + m['exec_ktime'] / 1e9
    return m.get('start_ep')


def _emit_netio(m, actor, attribution, reason, now):
    """One `netio` record: what each endpoint moved since this process's previous
    one. Returns False, writing nothing, when there is nothing new."""
    life = _netp_lifetime(m)
    rep = m['netp_rep'] or {}
    delta = {}
    for ek, (tx, rx, calls) in life.items():
        r = rep.get(ek, (0, 0, 0))
        d = (tx - r[0], rx - r[1], calls - r[2])
        if d[0] > 0 or d[1] > 0 or d[2] > 0:
            delta[ek] = d
    if not delta:
        return False
    since = m['netio_ts'] or max(_start_epoch_of(m) or 0.0, _COLLECTOR_START)
    eps, unat, over, _n = _endpoint_list(delta, _hosts_for(m))
    rec = _envelope('netio')
    rec.update(_base_identity(m, actor, attribution))
    rec.update({'reason': reason, 'interval_s': round(now - since, 1),
                'endpoints': eps, 'unattributed': unat})
    if over:
        rec['overflow'] = over
    writer.write(rec)
    m['netp_rep'] = {ek: tuple(v) for ek, v in life.items()}
    m['in_series'] = True
    m['netio_ts'] = now
    _netio_stats['records'] += 1
    return True


def netio_tick(now=None):
    """The series: one `netio` record per live, tracked process that moved bytes
    since its previous one. Call after netpeer_read(). Returns the number
    written, or None when the series is off or there is no endpoint table."""
    if _netp_tab is None or not WANT_NETIO or not (NETPEER_BLOCKS & FEATURES):
        return None
    now = time.time() if now is None else now
    n = 0
    for m in list(proc.values()):
        if m['exited'] or not m['netp_k']:
            continue
        actor, attribution = actor_of(m)
        if actor is not None and _emit_netio(m, actor, attribution, 'tick', now):
            n += 1
    return n


def netio_final_live(now=None):
    """At stop: the remainder for each live process already in the series, so the
    series ends where the `truncated` records do."""
    if _netp_tab is None or not WANT_NETIO:
        return 0
    now = time.time() if now is None else now
    n = 0
    for m in list(proc.values()):
        if m['exited'] or not m['in_series']:
            continue
        actor, attribution = actor_of(m)
        if actor is not None and _emit_netio(m, actor, attribution, 'stop', now):
            n += 1
    return n


def _read_netp_kern():
    """{calls, tx_bytes, rx_bytes} on kernel sockets since start, or None."""
    t = _netp_kern_tab
    if t is None:
        return None
    try:
        return {'calls': int(t[0].value), 'tx_bytes': int(t[1].value),
                'rx_bytes': int(t[2].value)}
    except Exception:
        return None


def _read_netp_drop():
    """{calls, tx_bytes, rx_bytes} that found the endpoint table full since start,
    or None without a table."""
    t = _netp_drop_tab
    if t is None:
        return None
    try:
        return {'calls': int(t[0].value), 'tx_bytes': int(t[1].value),
                'rx_bytes': int(t[2].value)}
    except Exception:
        return None


# ---------------------------------------------------------------------------
# residency sampler (census replacement)
# ---------------------------------------------------------------------------

def _read_vmrss_kb(pid):
    """Current resident set (VmRSS, kB). The sampler wants current, not peak."""
    try:
        with open('/proc/%d/status' % pid) as f:
            for line in f:
                if line.startswith('VmRSS:'):
                    return int(line.split()[1])
    except Exception:
        pass
    return None


def _read_kstack(pid):
    """Item 4: kernel frames of a process in D. Reading /proc/<pid>/stack touches
    no filesystem, so unlike the cwd link it cannot hang on the mount being
    diagnosed. Root only — this is the field limitations.md calls the headline
    ptrace-gated one."""
    try:
        with open('/proc/%d/stack' % pid) as f:
            return parse_kstack(f.read(), DSTACK_DEPTH)
    except Exception:
        return None


def residency_tick(b=None):
    """One rollup per live agent tree, plus per-actor3 and per-agent-type totals,
    the dropped bucket, the node fork rate, and the standing-connection records."""
    global _fork_prev, _residency_ticks
    now = time.time()
    # Every entry against /proc, tracked or not: before this only the processes
    # about to be reported were re-read, so an untracked one whose exit was lost
    # stayed live forever.
    alive = reconcile_proc(now)
    _residency_ticks += 1
    if alive is not None and _residency_ticks % 10 == 0:
        _thread_acc_sweep(alive)
    live = [m for m in proc.values() if not m['exited']]
    trees, totals, by_type = {}, {}, {}
    stacks_read = 0
    global_frames = {}
    for m in live:
        actor, _attr = actor_of(m)
        if actor is None:
            continue
        st = read_stat(m['pid'])
        if st is None:
            continue                          # gone; reconcile_proc() retires it
        se = start_epoch(st['starttime'])
        if m.get('start_ep') is None:
            m['start_ep'] = se
        elif se is not None and abs(se - m['start_ep']) > 5.0:
            # A different process on this pid: this one's exit event was lost and
            # the pid recycled. Retire it, and give the new one its own entry.
            pid = m['pid']
            _retire(m, now)
            proc_del(pid)
            _ingest_proc(pid)
            continue
        rss_kb = _read_vmrss_kb(m['pid']) or 0
        cpu_s = st['cpu_ticks'] / float(CLK_TCK)
        in_d = st['state'] == 'D'
        # CPU burned since the previous tick -- the only way to say which process
        # is busy NOW rather than which one was busy over its whole life.
        prev = m.get('tick_cpu_s')
        dcpu = round(cpu_s - prev, 3) if prev is not None else None
        m['tick_cpu_s'] = cpu_s
        # Confinement backstop. A seccomp filter is installed by prctl at an
        # arbitrary time, usually AFTER exec, so the exec-time read misses it;
        # and sched_process_fork carries no clone_flags, so a clone(CLONE_NEW*)
        # child that never execs is invisible to inherit-first. This closes both
        # within one tick. Seccomp is monotone -- filters can only be added and
        # they survive execve -- so a stale value is a false negative, never a
        # false positive, and max() keeps a transient EACCES from downgrading a
        # known-filtered process. Namespaces are NOT monotone (setns can move a
        # process out), so those overwrite.
        if WANT_SANDBOX and m.get('sandbox_src') in (None, 'inherit', 'denied'):
            _read_sandbox(m, 'tick')
        frames = None
        if in_d and stacks_read < DSTACK_MAX:
            frames = _read_kstack(m['pid'])
            stacks_read += 1
            if frames:
                top = frames[0]
                global_frames[top] = global_frames.get(top, 0) + 1
        a3 = actor3(actor, m.get('agent_type'))
        t = totals.setdefault(a3, {'n_procs': 0, 'threads': 0, 'rss_mb': 0.0,
                                   'd_state': 0, 'users': set(), 'states': {}})
        t['n_procs'] += 1
        t['threads'] += st['threads']
        t['rss_mb'] += rss_kb / 1024.0
        t['d_state'] += 1 if in_d else 0
        t['states'][st['state']] = t['states'].get(st['state'], 0) + 1
        if m.get('user'):
            t['users'].add(m['user'])
        if actor != 'agent' or m.get('agent_pid') is None:
            continue
        # item 7: per-agent-type totals, mirroring the census's totals_by_type
        at = m.get('agent_type') or '?'
        bt = by_type.setdefault(at, {'trees': set(), 'n_procs': 0, 'threads': 0,
                                     'rss_mb': 0.0, 'cpu_s': 0.0, 'd_state': 0,
                                     'users': set(), 'autonomous': 0})
        bt['n_procs'] += 1
        bt['threads'] += st['threads']
        bt['rss_mb'] += rss_kb / 1024.0
        bt['cpu_s'] += cpu_s
        bt['d_state'] += 1 if in_d else 0
        if m.get('user'):
            bt['users'].add(m['user'])
        key = tree_root(m['pid']) or m['agent_pid']   # top-most agent ancestor
        bt['trees'].add(key)
        if m.get('is_agent') and is_autonomous(m.get('args')):
            bt['autonomous'] += 1
        tr = trees.setdefault(key, {'n_procs': 0, 'threads': 0, 'rss_mb': 0.0, 'cpu_s': 0.0,
                                    'd_state': 0, 'max_depth': 0, 'commands': {},
                                    'agent_types': set(), 'frames': {},
                                    'states': {}, 'procs': []})
        tr['n_procs'] += 1
        tr['threads'] += st['threads']
        tr['rss_mb'] += rss_kb / 1024.0
        tr['cpu_s'] += cpu_s
        tr['d_state'] += 1 if in_d else 0
        tr['max_depth'] = max(tr['max_depth'], m.get('depth') or 0)
        tr['agent_types'].add(m.get('agent_type'))
        c = _command_of(m) or '?'
        tr['commands'][c] = tr['commands'].get(c, 0) + 1
        # item 10: zombie census. state_z_frac was lost at the poller cutover --
        # an exit-triggered collector cannot see a zombie, because a zombie has
        # not exited. The tick can, and st['state'] is already read here.
        tr['states'][st['state']] = tr['states'].get(st['state'], 0) + 1
        # item 9: the row itself, so a live view can name the process burning CPU
        # right now. Every field is already in hand -- selection only, no syscall.
        tr['procs'].append({'pid': m['pid'], 'command': c,
                            'rss_mb': round(rss_kb / 1024.0, 1),
                            'cpu_s': round(cpu_s, 2), 'cpu_delta_s': dcpu,
                            'state': st['state'], 'threads': st['threads']})
        if frames:
            tr['frames'][frames[0]] = tr['frames'].get(frames[0], 0) + 1
    # Computed before the emit loop so the per-tree counts are available to it,
    # but the records themselves are written AFTER, below, so the file's record
    # order is byte-for-byte what v4 produced. The process table is already
    # reaped at this point (the walk above), so no conn record can carry identity
    # for a process the loop is about to mark exited.
    conn = None
    if b is not None:
        try:
            conn = conn_tick(b)
        except Exception:
            conn = None
    conn_by_tree = (conn or {}).get('by_tree', {})

    for apid, tr in trees.items():
        root = proc.get(apid)
        if root is None:
            continue
        if WANT_CWD:
            c = read_cwd(apid)               # roots are long-lived and may have chdir'd
            if c:
                root['cwd'] = c
        rec = _envelope('residency')
        rec.update(_base_identity(root, 'agent', 'ancestry'))
        rec.update({
            'root_alive': not root['exited'],
            'age_s': round(now - root['start_ep'], 1) if root.get('start_ep') else None,
            'headless': root.get('tty') is None,
            'n_procs': tr['n_procs'], 'threads': tr['threads'],
            'rss_mb': round(tr['rss_mb'], 1), 'cpu_s': round(tr['cpu_s'], 2),
            'd_state': tr['d_state'], 'max_depth': tr['max_depth'],
            'agent_types': sorted(t for t in tr['agent_types'] if t),
            'by_command': _top_n(tr['commands'], 15),
            'd_stack': _top_n(tr['frames'], 5, or_none=True),
            # item 10: R/S/D/Z/T for this tree's live processes. Zombies are
            # invisible to an exit-triggered collector by construction.
            'state_counts': tr['states'],
            # item 9: the heaviest processes right now, by CPU burned since the
            # previous tick, then by RSS. Live resource usage, as opposed to the
            # per-process totals that only exist at exit.
            'top_procs': sorted(tr['procs'],
                                key=lambda r: (-(r['cpu_delta_s'] or 0), -r['rss_mb'])
                                )[:RESIDENCY_TOPN],
            # item 6: standing external connections for this tree. null, not 0,
            # when the tcp blocks are absent or the tick failed -- "not measured"
            # must not read as "none".
            # 0 when the tick ran and this tree simply has no standing sockets;
            # null ONLY when the tick did not run (tcp blocks absent, or it
            # raised). "no connections" and "not measured" must not collide.
            'tcp_open_ext': (conn_by_tree.get(apid, {}).get('ext', 0)
                             if conn is not None else None),
            'tcp_open': (conn_by_tree.get(apid, {}).get('open', 0)
                         if conn is not None else None),
        })
        writer.write(rec)

    fork_total = read_fork_total()
    fork_rate = None
    if fork_total is not None and _fork_prev is not None and RESIDENCY_S:
        fork_rate = round((fork_total - _fork_prev) / float(RESIDENCY_S), 1)
    _fork_prev = fork_total

    for _r in (conn or {}).get('records', ()):
        writer.write(_r)
    n_conn = conn['n'] if conn is not None else None

    # After the walk above, so a process it just found gone is not ticked.
    n_netp = n_netio = None
    try:
        if _netp_tab is not None:
            if _netp_sweep_due():
                n_netp = netpeer_read()
            else:
                _netp_refresh_live()
                n_netp = sum(len(v) for v in _netp_index.values())
            if n_netp is not None:
                n_netio = netio_tick(now)
    except Exception:
        _count_error('netpeer_read')

    _tot = _envelope('residency_totals')
    _tot.update({
        'interval_s': RESIDENCY_S, 'tracked_live': len(live), 'trees': len(trees),
        'conn_records': n_conn,
        'by_actor3': {k: {'n_procs': v['n_procs'], 'threads': v['threads'],
                          'rss_mb': round(v['rss_mb'], 1), 'd_state': v['d_state'],
                          'users': len(v['users']), 'states': v['states']}
                      for k, v in totals.items()},
        'by_agent_type': {k: {'trees': len(v['trees']), 'n_procs': v['n_procs'],
                              'threads': v['threads'], 'rss_mb': round(v['rss_mb'], 1),
                              'cpu_s': round(v['cpu_s'], 2), 'd_state': v['d_state'],
                              'users': len(v['users']), 'autonomous': v['autonomous']}
                          for k, v in by_type.items()},
        # item 6: what the actor filter threw away since the last tick, and the
        # node's own fork rate as the denominator for the agent share
        'dropped': {'n': _dropped['n'],
                    'by_command': _top_n(_dropped['commands'], 10)},
        # Same rule: an empty map means "measured, none found"; null means the
        # tick did not run.
        'tcp_open_ext_by_actor3': (conn['by_actor3'] if conn is not None else None),
        # Sockets whose owning tree has no residency record (owner just exited,
        # or a non-agent actor). Without this, sum(residency.tcp_open_ext) does
        # not reconcile with conn_records and there is no way to see why.
        'conn_unattributed': (None if conn is None else
                              sum(v['ext'] for k, v in conn_by_tree.items()
                                  if k not in trees)),
        # tcp_birth entries that were established but have no netlink row: the
        # CLOSE event never arrived and the map entry has leaked.
        'conn_unmatched_births': (conn or {}).get('unmatched_births'),
        'fork_total': fork_total, 'fork_rate': fork_rate,
        'd_stack_top': _top_n(global_frames, 10, or_none=True),
        'd_stacks_read': stacks_read,
        # schema 7, all null (not 0) when no netpeer block loaded: the netio
        # records this tick wrote, the endpoint table's occupancy, and the two
        # cumulative loss counters -- calls that found the table full, and
        # entries deleted without ever reaching a record
        'netio_records': n_netio,
        'netpeer_entries': n_netp,
        'netpeer_full': _read_netp_drop(),
        'netpeer_orphans': dict(_netp_orphans) if n_netp is not None else None,
        # schema 8: calls and bytes on kernel sockets (the NFS client's), which
        # are not counted as any process's network I/O; cumulative
        'netpeer_kernel': _read_netp_kern(),
        # schema 8, all cumulative since start: perf events decoded and lost per
        # ring (only the stop record had them, and a collector that runs for
        # weeks seldom writes one), exceptions the handlers swallowed, the
        # process table's size and the entries retired without an exit event
        'perf_seen': dict(_perf_seen) or None,
        'perf_lost': dict(_perf_lost) or None,
        'handler_errors': dict(_handler_errors) or None,
        'proc_entries': len(proc),
        'proc_vanished': _proc_stats['vanished'],
        'thread_acc': _thread_acc_report(),
    })
    writer.write(_tot)
    _dropped['n'] = 0
    _dropped['commands'] = {}


def gc():
    cutoff = time.time() - GRACE_S
    for pid in [p for p, m in proc.items() if m['exited'] and (m['gc_ts'] or 0) < cutoff]:
        m = proc[pid]
        # Reaped without ever being emitted -- unattributable, filtered, or a pid
        # the collector only ever saw a socket for. Whatever it had folded is
        # lost with it, and this is the one place that sees every such entry.
        if not m['emitted']:
            _note_conn_dropped(m)
        proc_del(pid)


# ---------------------------------------------------------------------------
# item 5: flush still-live processes at stop
# ---------------------------------------------------------------------------

def _io_mb(raw):
    """procparse.read_io()'s raw byte counters in the exit record's MB shape."""
    if not raw:
        return None
    return {'rd_mb': round(raw['rd'] / 1e6, 3), 'wr_mb': round(raw['wr'] / 1e6, 3),
            'rchar_mb': round(raw['rchar'] / 1e6, 3),
            'wchar_mb': round(raw['wchar'] / 1e6, 3), 'scope': 'proc'}


def flush_live(b, features):
    """`event="truncated"` for every live tracked process, mirroring the poller's
    field name. The processes have not exited, so no kernel event exists and this
    must be userspace — but we drain the D-state and network hashes first, so a
    truncated record carries the kernel's real accumulated counters rather than
    only what /proc shows at this instant. The poller could not do that."""
    ds, nb = {}, {}
    if 'dstate_stat' in features:
        try:
            for k, v in b['dstate_acc'].items():
                ds[k.value] = (v.total, v.max, v.cnt)
        except Exception:
            pass
    if 'netbytes' in features:
        try:
            for k, v in b['netb'].items():
                nb[k.value] = (v.tx, v.rx, v.calls)
        except Exception:
            pass
    n = 0
    now = time.time()
    for m in list(proc.values()):
        if m['exited']:
            continue
        actor, attribution = actor_of(m)
        if actor is None:
            continue
        st = read_stat(m['pid'])
        if st is None:
            continue
        pid = m['pid']
        d_total, d_max, d_cnt = ds.get(pid, (None, None, None))
        tx, rx, calls = nb.get(pid, (None, None, None))
        rec = _envelope('truncated')
        rec.update(_base_identity(m, actor, attribution))
        rec.update({
            'start': (datetime.fromtimestamp(m['start_ep']).strftime('%Y-%m-%d %H:%M:%S')
                      if m.get('start_ep') else None),
            'duration_s': round(now - m['start_ep'], 4) if m.get('start_ep') else None,
            'duration_src': 'proc',
            'cpu_s': round(st['cpu_ticks'] / float(CLK_TCK), 4),
            'cpu_scope': 'process',
            'child_cpu_s': round(st['child_ticks'] / float(CLK_TCK), 4),
            'rss_mb': round((_read_vmrss_kb(pid) or 0) / 1024.0, 2),
            'threads': st['threads'],
            'state_last': st['state'],
            'min_flt': st['min_flt'], 'maj_flt': st['maj_flt'],
            'dstate_wait_s': round(d_total / 1e9, 4) if d_total is not None else None,
            'dstate_episodes': d_cnt,
            'dstate_max_s': round(d_max / 1e9, 4) if d_max is not None else None,
            'dstate_src': 'stat' if d_total is not None else None,
            'net_tx_bytes': tx, 'net_rx_bytes': rx, 'net_calls': calls,
            # These are the LONGEST-LIVED, highest-I/O processes on the node --
            # exactly the ones whose I/O mattered -- and they carried none of it
            # before. scope='proc' marks the different provenance from the exit
            # record's in-kernel taskstats read: this is /proc at flush time,
            # which is own-user-only unless the collector is root.
            'io': _io_mb(read_io(pid)),
            # Same block the exit record carries. Without it a process that is
            # still alive at stop -- which is exactly the long-lived agent whose
            # traffic matters most -- would have every connection it made dropped
            # on the floor now that they no longer emit records of their own.
            'conns': _conns_block(m),
            'net_endpoints': _net_endpoints_block(m, tx, rx),
            'exit_code': None, 'signal': None, 'core_dumped': None,
            'samples': None,
        })
        writer.write(rec)
        m['emitted'] = True
        n += 1
    return n


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def _bcc_version():
    try:
        import bcc
        return getattr(bcc, '__version__', 'unknown')
    except Exception:
        return None


def _sysctl(name):
    try:
        with open('/proc/sys/' + name.replace('.', '/')) as f:
            return f.read().strip()
    except Exception:
        return None


def _self_cgroup():
    try:
        with open('/proc/self/cgroup') as f:
            for line in f:
                parts = line.rstrip('\n').split(':', 2)
                if len(parts) == 3 and parts[0] == '0':
                    return parts[2]
    except Exception:
        pass
    return None


def _take_lock(root, host):
    """Hold <root>/.<host>.lock, exclusively, for the life of the run -- or exit.
    Two collectors writing one host's files (the unit, and a manual start)
    interleave their records and report every process twice. A filesystem that
    cannot lock lets the run through."""
    import errno
    import fcntl
    try:
        os.makedirs(root, exist_ok=True)
        fd = os.open(os.path.join(root, '.%s.lock' % host),
                     os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    except OSError:
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        if e.errno not in (errno.EWOULDBLOCK, errno.EAGAIN):
            return fd
        try:
            holder = os.pread(fd, 32, 0).decode('ascii', 'replace').strip()
        except Exception:
            holder = ''
        sys.stderr.write('error: another collector%s is writing %s for %s; not starting '
                         'a second one\n' % (' (pid %s)' % holder if holder else '', root, host))
        sys.exit(1)
    try:
        os.ftruncate(fd, 0)
        os.write(fd, ('%d\n' % os.getpid()).encode())
    except OSError:
        pass
    return fd


def _unsafe_dirs(paths):
    """Directories on the way to `paths` that someone other than root can
    change: owned by a user, or group/other-writable without the sticky bit.
    A collector running as root writes through each of them (a planted
    symlink sends its appends wherever that user likes) and, for its own code
    directory, runs what is there at its next start. Both the path as given
    and the path with its symlinks resolved are walked."""
    import stat as st_
    out, seen = set(), set()
    for path in paths:
        for p in {os.path.abspath(path), os.path.realpath(path)}:
            while p not in seen:
                seen.add(p)
                try:
                    st = os.lstat(p)
                except OSError:
                    st = None
                if st is not None and st_.S_ISDIR(st.st_mode):
                    open_w = (st.st_mode & 0o002) or (st.st_mode & 0o020 and st.st_gid != 0)
                    if st.st_uid != 0 or (open_w and not st.st_mode & st_.S_ISVTX):
                        out.add(p)
                parent = os.path.dirname(p)
                if parent == p:
                    break
                p = parent
    return sorted(out)


def main():
    global FEATURES, writer, _cidr_index, _netp_tab, _netp_drop_tab, _COLLECTOR_START
    global _thr_acc_tab, _thr_acc_miss_tab, _submit_tab, _netp_kern_tab
    argv = sys.argv[1:]
    try:
        from bcc import BPF
    except ImportError:
        sys.stderr.write('error: python3-bcc not found. Run `ebpfm.sh bootstrap` '
                         '(apt install python3-bpfcc | dnf install python3-bcc).\n')
        sys.exit(2)
    if os.geteuid() != 0:
        sys.stderr.write('warning: not root — BPF attach and other-user /proc reads will fail '
                         'without CAP_BPF/CAP_PERFMON/CAP_SYS_PTRACE (CAP_SYS_ADMIN < 5.8).\n')

    if '--dump-c' in argv or '--features' in argv:
        b, feats, rejected = probe_features(BPF, verbose=True)
        if '--dump-c' in argv:
            print(build_bpf_text(feats))
        else:
            print(json.dumps({'kernel': os.uname().release, 'bcc': _bcc_version(),
                              'features': sorted(feats), 'rejected': rejected,
                              'btf_offs': btf_offsets(),
                              'sysctl': {'sched_schedstats': _sysctl('kernel.sched_schedstats'),
                                         'task_delayacct': _sysctl('kernel.task_delayacct')},
                              'cgroup': _self_cgroup()}, indent=1))
        return

    lock_fd = _take_lock(OUTPUT_DIR, HOSTNAME)     # held until exit  # noqa: F841
    unsafe = _unsafe_dirs([OUTPUT_DIR, _HERE]) if os.geteuid() == 0 else []
    if unsafe:
        sys.stderr.write('warning: running as root with output or code under directories '
                         'other users can change: %s -- use root-owned ones\n'
                         % ', '.join(unsafe))
    writer = DailyWriter(OUTPUT_DIR, HOSTNAME)
    # Catch up any sealed day left uncompressed by a prior run or a crash before
    # it could compress at rollover. Skips today, whose files writer will reopen
    # for appending. Fire-and-forget gzip subprocesses; cheap when nothing is
    # pending.
    _sweep_sealed_days(OUTPUT_DIR, HOSTNAME, datetime.now().strftime('%Y-%m-%d'))
    if _load_cidr_index is not None:
        try:
            _cidr_index = _load_cidr_index()
        except Exception:
            _cidr_index = None

    if WANT_ARGV_HOSTS:
        # before seeding, so names in the argv of processes already running resolve
        threading.Thread(target=_host_worker, name='ebpfm-hosts', daemon=True).start()
    # Attach FIRST, then seed. The other way round, a process started between
    # the /proc walk and the attach was in neither -- an agent started then was
    # never attributed, nor was anything it ran. Events that arrive while
    # seeding wait in the queue and are applied after it, in kernel-time order.
    b, FEATURES, rejected = probe_features(BPF)
    feats = FEATURES
    if NETPEER_BLOCKS & feats:
        _netp_tab, _netp_drop_tab = b['netp'], b['netp_drop']
        _netp_kern_tab = b['netp_kern']
    if 'thread_acc' in feats:
        _thr_acc_tab, _thr_acc_miss_tab = b['thr_acc'], b['thr_acc_miss']

    # Events DECODED, not records written. The drain touches every fork/exec/exit
    # to keep the ancestry table current, even for processes that are never
    # emitted, so this is the only number that makes the collector's userspace CPU
    # interpretable -- and, with the lost counts, the pair that says whether it
    # kept up. Both are cumulative and reported every residency tick.
    for ring in ('events', 'argv_events', 'write_events', 'tcp_events', 'netp_new'):
        _perf_seen[ring] = 0
        _perf_lost[ring] = 0
    resync = {'due': False}

    def _copier(table):
        """An owned copy of a perf event. BCC's event() is a view into the ring,
        valid only inside the callback, and the queue keeps events past it. A
        record can be shorter than the struct (argv sends only what it read), so
        the copy is padded with zeros rather than read past the record."""
        tab, cls = b[table], []

        def copy(data, size):
            if not cls:
                cls.append(type(tab.event(data)))
            n = ctypes.sizeof(cls[0])
            raw = ctypes.string_at(data, min(size, n))
            if len(raw) < n:
                raw += b'\x00' * (n - len(raw))
            return cls[0].from_buffer_copy(raw)
        return copy

    copy_core = _copier('events')

    def _cb_core(cpu, data, size):
        _perf_seen['events'] += 1
        try:
            e = copy_core(data, size)
            h = _CORE_HANDLERS.get(e.type)
            if h is not None:
                enqueue_event(e.ktime_ns, RANK_CORE, h, e)
        except Exception:
            _count_error('events')

    def _mk_cb(table, fn, rank, stamp):
        copy = _copier(table)

        def _cb(cpu, data, size):
            _perf_seen[table] += 1
            try:
                ev = copy(data, size)
                enqueue_event(getattr(ev, stamp), rank, fn, ev)
            except Exception:
                _count_error(table)
        return _cb

    def _mk_lost(name):
        def _l(n):
            _perf_lost[name] += n
            resync['due'] = True        # a lost exit leaves its entry live
        return _l

    b['events'].open_perf_buffer(_cb_core, page_cnt=PAGE_CNT, lost_cb=_mk_lost('events'))
    if 'argv_user' in feats or 'argv_kernel' in feats:
        b['argv_events'].open_perf_buffer(_mk_cb('argv_events', on_argv, RANK_ARGV, 'ktime_ns'),
                                          page_cnt=PAGE_CNT, lost_cb=_mk_lost('argv_events'))
    if 'submit' in feats:
        _submit_tab = b['submit_pids']
        b['write_events'].open_perf_buffer(_mk_cb('write_events', on_write, RANK_WRITE, 'ktime_ns'),
                                           page_cnt=64, lost_cb=_mk_lost('write_events'))
    if (TCP_BLOCKS | {'tcp_accept'}) & feats:
        b['tcp_events'].open_perf_buffer(_mk_cb('tcp_events', on_tcp, RANK_TCP, 'ts_ns'),
                                         page_cnt=64, lost_cb=_mk_lost('tcp_events'))
    if NETPEER_BLOCKS & feats:
        # Applied at once, not queued: indexing an entry depends on nothing else.
        key_cls = _netp_tab.Key
        key_len = ctypes.sizeof(key_cls)

        def _cb_netp_new(cpu, data, size):
            _perf_seen['netp_new'] += 1
            try:
                _netp_index_add(key_cls.from_buffer_copy(
                    ctypes.string_at(data, key_len)))
            except Exception:
                _count_error('netp_new')

        def _lost_netp_new(n):
            _perf_lost['netp_new'] += n
        b['netp_new'].open_perf_buffer(_cb_netp_new, page_cnt=64, lost_cb=_lost_netp_new)
    _COLLECTOR_START = time.time()

    def _drain_rings():
        try:
            b.perf_buffer_poll(timeout=0)
        except Exception:
            _count_error('perf_buffer_poll')
    seed_existing(poll=_drain_rings)

    sysctls = {'kernel.sched_schedstats': _sysctl('kernel.sched_schedstats'),
               'kernel.task_delayacct': _sysctl('kernel.task_delayacct')}
    meta = _envelope('meta')
    meta.update({'kernel': os.uname().release, 'bcc': _bcc_version(), 'euid': os.geteuid(),
            # Cross-host time: ts_epoch on every record is the authoritative
            # ordering/binning key; these two say how to interpret it and which
            # boot the pids in this file belong to.
            'tz': _tz_name(), 'boot_id': BOOT_ID,
            'features': sorted(feats), 'features_rejected': rejected,
            'cpu_scope': 'process' if 'signal' in feats else 'leader',
            # schema 8: whether every exit counter (run time, faults, context
            # switches, waits, D-state, threads, I/O) covers all of a process's
            # threads, or only the leader's and the threads already released
            'counters_scope': 'process' if 'thread_acc' in feats else 'leader',
            # what `args` and `captured_tail` have masked (lib/redact.py)
            'redact': sorted(REDACT),
            'argv_source_default': 'kernel' if ('argv_user' in feats or 'argv_kernel' in feats) else 'proc',
            'dstate_src_default': ('task' if 'dstate_task' in feats
                                   else ('stat' if 'dstate_stat' in feats else None)),
            'btf_offs': btf_offsets() or None,
            # two of the eight data points are inert when these read 0
            'sysctl': sysctls,
            'cgroup': _self_cgroup(),
            'netlink_sockdiag': sockdiag is not None,
            # directories other users can change on the way to the output or
            # this collector's code, which a root collector should not trust
            'unsafe_dirs': unsafe or None,
            'config': {'args_maxlen': ARGS_MAXLEN, 'argv_kmax': ARGV_KMAX,
                       'write_kmax': WRITE_KMAX, 'residency_s': RESIDENCY_S,
                       'keep_timers': KEEP_TIMERS, 'include_roots': INCLUDE_ROOTS,
                       'tcp_all': TCP_ALL, 'conn_all': CONN_ALL,
                       'conns_topn': CONNS_TOPN, 'exit_hold_ms': EXIT_HOLD_MS,
                       'reorder_ms': REORDER_MS,
                       'netpeer_max': NETPEER_MAX, 'net_endpoints_max': NET_ENDPOINTS_MAX,
                       'netio': WANT_NETIO, 'argv_hosts': WANT_ARGV_HOSTS,
                       # compiled into the endpoint probe; decides inbound for a
                       # socket the tcp blocks did not see open
                       'eph_range': list(EPH_RANGE),
                       'flush_mode': FLUSH_MODE,
                       'conn_untracked': CONN_UNTRACKED, 'cwd': WANT_CWD,
                       'cwd_always': CWD_ALWAYS, 'min_uid': MIN_UID,
                       'sandbox': WANT_SANDBOX, 'sandbox_always': SANDBOX_ALWAYS,
                       'dstack_max': DSTACK_MAX, 'flush': FLUSH_ON_EXIT,
                       'submit_comms': sorted(SUBMIT_COMMS), 'gc_grace_s': GRACE_S},
            'seeded_pids': len(proc)})
    writer.write(meta)
    writer.flush()          # `ebpfm.sh status` looks for this record immediately
    for k, v in sysctls.items():
        if v == '0':
            sys.stderr.write('warning: %s=0 — the dstate_*/blkio_* fields will read 0 '
                             '(run `ebpfm.sh bootstrap` to set it)\n' % k)
    # `ebpfm.sh overhead` waits for "running on" in this line
    sys.stderr.write('ebpfm running on %s -> %s/<date>/%s.{snapshot,exits}.jsonl '
                     '(features: %s)\n'
                     % (HOSTNAME, OUTPUT_DIR, HOSTNAME, ','.join(sorted(feats)) or 'base'))

    start = time.time()
    last_gc = last_res = last_resync = start
    while not _stop:
        # Kernel time before the poll: every event stamped REORDER_MS before it
        # was already in its ring, so this round's poll drained it, and all of
        # those can be applied in order. Newer ones wait a round.
        t0 = _ktime_now()
        try:
            b.perf_buffer_poll(timeout=POLL_MS)
        except Exception:
            _count_error('perf_buffer_poll')
        try:
            dispatch_events(t0 - REORDER_MS * 1000000)
        except Exception:
            _count_error('dispatch_events')
        try:
            drain_exits()
        except Exception:
            _count_error('drain_exits')
        # After drain_exits, so a record staged this round is covered by this
        # round's flush rather than waiting for the next one.
        try:
            writer.flush()
        except Exception:
            _count_error('flush')
        now = time.time()
        if resync['due'] and now - last_resync >= 10:
            # a lost event may have been an exit; find the entries it left live
            resync['due'] = False
            last_resync = now
            try:
                reconcile_proc(now)
            except Exception:
                _count_error('reconcile_proc')
        if now - last_gc > 60:
            gc()
            last_gc = now
        if RESIDENCY_S and now - last_res >= RESIDENCY_S:
            try:
                residency_tick(b)
            except Exception:
                _count_error('residency_tick')
            last_res = now
        if DURATION and (now - start) >= DURATION:
            break

    # Whatever the rings still hold, then everything queued, so no event is
    # stranded behind the reorder window.
    try:
        b.perf_buffer_poll(timeout=0)
        dispatch_events()
    except Exception:
        _count_error('dispatch_events')
    # Before flush_live, which skips m['exited'] entries: a staged exit record
    # that is still pending belongs to a process that HAS exited, so draining
    # first keeps it an `exit` and not a second, contradictory `truncated`.
    try:
        drain_exits(final=True)
    except Exception:
        _count_error('drain_exits')
    # The last read of the endpoint table, then the series' remainder for every
    # process still alive, so the series ends where the truncated records do.
    try:
        netpeer_read()
        netio_final_live()
    except Exception:
        _count_error('netpeer_read')
    n_trunc = 0
    if FLUSH_ON_EXIT:
        try:
            n_trunc = flush_live(b, feats)
        except Exception:
            n_trunc = 0
    # NB: `_stop` is the module-global shutdown flag read below -- do not shadow it.
    stop_rec = _envelope('stop')
    stop_rec.update({'uptime_s': round(time.time() - start, 1), 'records': writer.n,
                     'truncated': n_trunc, 'perf_lost': dict(_perf_lost),
                     'perf_seen': dict(_perf_seen),
                     # exceptions the handlers swallowed, and entries retired
                     # because their process was gone with no exit event seen
                     'handler_errors': dict(_handler_errors) or None,
                     'proc_vanished': _proc_stats['vanished'],
                     # write(2) count for the whole run, against `records` above:
                     # the pair IS the I/O amplification, measured rather than
                     # assumed. +1 because this record is built before it is
                     # written, and the flush that lands it has not happened yet
                     # -- true in both modes.
                     'flush_mode': 'record' if writer.per_record else 'poll',
                     'flushes': writer.flushes + 1,
                     # folded connections whose owning process never produced a
                     # record; the acceptance gate for the schema-6 fold
                     'conns_dropped': _conn_dropped['n'],
                     'conns_dropped_commands': _top_n(_conn_dropped['commands'], 10,
                                                      or_none=True),
                     # schema 7, null without a netpeer block: the run's netio
                     # records, and the endpoint table's two loss counters
                     'netio_records': (_netio_stats['records']
                                       if _netp_tab is not None else None),
                     'netpeer_full': _read_netp_drop(),
                     'netpeer_orphans': (dict(_netp_orphans)
                                         if _netp_tab is not None else None),
                     # argv hostname lookups: done, failed, and dropped on a full
                     # queue (each a `host` that may be missing)
                     'argv_hosts': dict(_host_stats) if WANT_ARGV_HOSTS else None,
                     'reason': 'duration' if DURATION and not _stop else 'signal'})
    writer.write(stop_rec)
    if any(_perf_lost.values()):
        sys.stderr.write('warning: perf events dropped: %s\n' % json.dumps(_perf_lost))
    writer.close()


if __name__ == '__main__':
    main()
