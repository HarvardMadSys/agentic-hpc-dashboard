#!/usr/bin/env python3
"""eBPF_marthen_new — root eBPF collector for coding-agent activity on a login node.

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
is what makes QUIC visible, and connect latency / failed-connect detection free
from the existing socket tracepoint.

Privilege: root, or CAP_BPF + CAP_PERFMON + CAP_SYS_PTRACE (CAP_SYS_ADMIN < 5.8).
Needs python3-bcc, and kernel.sched_schedstats=1 plus kernel.task_delayacct=1 or
two of the eight are inert. `ebpfm.sh bootstrap` arranges all of that.

Usage (prefer ./ebpfm.sh):
    sudo python3 ebpf_trace.py [hostname]        # loop -> daily log
    sudo EBPFM_DURATION=30 python3 ebpf_trace.py # 30 s then exit
    sudo python3 ebpf_trace.py --features        # probe, print the resolved set, exit
    sudo python3 ebpf_trace.py --dump-c          # print the BPF C actually compiled

Output: $EBPFM_OUTPUT_DIR/<YYYY-MM-DD>/<hostname>.jsonl, one JSON object per line,
`event` in {meta, exit, truncated, tcp, accept, conn, submit, residency,
residency_totals, stop}. Schema in README.md.
"""
import ctypes
import hashlib
import ipaddress
import json
import os
import re
import signal
import sys
import time
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, 'lib'))
from agent_classify import (classify, actor3, is_autonomous, SANDBOX_TYPES,
                            approval_mode_of)  # noqa: E402
from procparse import (  # noqa: E402
    boot_time, list_pids, read_stat, read_cmdline, read_cgroup, read_cwd,
    tty_name, uid_of, username, start_epoch, env_num, boot_id, read_ns, read_confinement, read_io,
    read_environ_names)

from slurm_args import parse_submit_args  # noqa: E402

try:  # provider buckets for the network records (absent -> provider=null)
    from provider_cidrs import load_index as _load_cidr_index
except Exception:  # pragma: no cover
    _load_cidr_index = None
try:  # netlink socket-diag for standing connections
    import sockdiag
except Exception:  # pragma: no cover
    sockdiag = None

COLLECTOR = 'ebpf_marthen_new'
SCHEMA_VERSION = 5      # 5 = ts_epoch envelope, sandbox_*, approval_*, session_uuid,
                        #     args_len/args_truncated, submit request fields,
                        #     residency top_procs/state_counts/tcp_open_ext, io on truncated
                        # 4 = cwd, dstate_*, net_*, conn/accept/truncated events, attribution

HOSTNAME = (sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith('-')
            else os.uname().nodename.split('.')[0])

# ---------------------------------------------------------------------------
# configuration (EBPFM_* env)
# ---------------------------------------------------------------------------
OUTPUT_DIR    = os.environ.get('EBPFM_OUTPUT_DIR', '/var/log/ebpfm')
DURATION      = env_num('EBPFM_DURATION', 0, float)          # 0 = forever
ARGS_MAXLEN   = env_num('EBPFM_ARGS_MAXLEN', 2048, int)      # emitted argv cap (classification uncapped)
ARGV_KMAX     = env_num('EBPFM_ARGV_KMAX', 4096, int)        # in-kernel argv read (bytes)
WRITE_KMAX    = env_num('EBPFM_WRITE_KMAX', 1024, int)       # per write() capture (bytes)
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
# would propagate "unsandboxed" down the whole sandboxed tree. All <= 15 chars,
# so all survive TASK_COMM_LEN.
RESIDENCY_TOPN = env_num('EBPFM_RESIDENCY_TOPN', 5, int)   # heaviest procs per tree
SANDBOX_COMMS = {'bwrap', 'unshare', 'nsenter', 'podman', 'docker', 'runc', 'crun',
                 'conmon', 'containerd-shim', 'apptainer', 'singularity', 'proot',
                 'firejail', 'systemd-nspawn', 'codex-linux-sandbox'}
# Environment NAMES only, never values -- agent environments carry API keys.
ENV_PREFIXES  = tuple((os.environ.get('EBPFM_ENV_PREFIXES')
                       or 'CLAUDE_,CODEX_,CURSOR_,ANTHROPIC_').split(','))
# --- item 3/8: network -----------------------------------------------------
TCP_ALL       = os.environ.get('EBPFM_TCP_ALL', '0') != '0'   # keep records for untracked pids
CONN_ALL      = os.environ.get('EBPFM_CONN_ALL', '0') != '0'  # 1 = emit loopback/private too
CONN_UNTRACKED = os.environ.get('EBPFM_CONN_UNTRACKED', '1') != '0'  # netlink rows with no pid
# --- item 4: wait channel --------------------------------------------------
DSTACK_MAX    = env_num('EBPFM_DSTACK_MAX', 300, int)         # /proc/<pid>/stack reads per tick
DSTACK_DEPTH  = env_num('EBPFM_DSTACK_DEPTH', 5, int)
# --- item 5: flush ---------------------------------------------------------
FLUSH_ON_EXIT = os.environ.get('EBPFM_FLUSH', '1') != '0'
# --- item 6: uid attribution ----------------------------------------------
# 0 disables. Enabling this widens what `actor3="human"` means, so a capture with
# it on is NOT comparable to one with it off; filter on `attribution` downstream.
MIN_UID       = env_num('EBPFM_MIN_UID', 0, int)

PAGE_SIZE = os.sysconf('SC_PAGE_SIZE') or 4096
CLK_TCK   = os.sysconf('SC_CLK_TCK') or 100
BTIME     = boot_time()

EVT_FORK, EVT_EXEC, EVT_EXIT = 1, 2, 3
TCP_KIND_CLOSE, TCP_KIND_ACCEPT = 1, 2

_KSTACK_FRAME = re.compile(r'\]\s+(\S+?)\+0x')   # same extraction snapshot.py uses

# ---------------------------------------------------------------------------
# BPF C program, assembled from independently probed blocks
# ---------------------------------------------------------------------------
# Greedy probing: base + accepted + candidate must compile AND load, else the
# candidate alone is dropped and its compiler error is recorded. A block that
# needs another lists it in `requires`.

BLOCKS = [
    # name          requires        purpose
    ('signal',      (),             'whole-process CPU/threads/cutime/cmaj_flt via signal_struct'),
    ('io',          (),             'task->ioac read/write/rchar/wchar (+signal->ioac when signal)'),
    ('sched',       (),             'sched_info.run_delay (run-queue wait)'),
    ('delay',       (),             'task->delays: blkio/swapin/freepages dwell (CONFIG_TASK_DELAY_ACCT)'),
    ('dstate_task', (),             'item 2: cumulative uninterruptible sleep from task->stats.sum_block_runtime (5.19+)'),
    ('dstate_stat', (),             'item 2: uninterruptible sleep accumulated from sched:sched_stat_blocked (portable)'),
    ('pids_signal', ('signal',),    'pgrp/sid via signal->pids[] (kernel >= 4.19 layout)'),
    ('pids_task',   (),             'pgrp/sid via group_leader->pids[].pid (kernel <= 4.18 layout)'),
    ('pids_kread',  ('signal',),    'pgrp/sid via explicit bpf_probe_read of signal->pids[] (bypasses the BCC rewriter)'),
    ('tty',         ('signal',),    'controlling tty name via signal->tty'),
    ('cgid',        (),             'bpf_get_current_cgroup_id (cgroup v2 id, resolved to a path in userspace)'),
    ('argv_user',   (),             'in-kernel argv at exec via bpf_probe_read_user'),
    ('argv_kernel', (),             'in-kernel argv at exec via bpf_probe_read (pre-5.5 fallback)'),
    ('submit',      (),             'sbatch/salloc/srun stdout/stderr capture -> job id'),
    ('netbytes',    (),             'all-protocol per-process net bytes: kretprobes on tcp/udp send+recv (makes QUIC visible)'),
    ('tcp_btf',     (),             'TCP lifetimes + bytes; tcp_sock offsets from kernel BTF (no net headers)'),
    ('tcp_hdr',     (),             'TCP lifetimes + bytes via <linux/tcp.h> (older kernels/BCC)'),
    ('tcp_basic',   (),             'TCP lifetimes without byte counters (no headers, no BTF)'),
    ('tcp_accept',  (),             'item 8: inbound connections via kretprobe inet_csk_accept (needs BTF)'),
]
BLOCK_NAMES = [b[0] for b in BLOCKS]
TCP_BLOCKS   = {'tcp_btf', 'tcp_hdr', 'tcp_basic'}
PIDS_BLOCKS  = {'pids_signal', 'pids_task', 'pids_kread'}
DSTATE_BLOCKS = {'dstate_task', 'dstate_stat'}
# mutually exclusive alternatives: once the first loads, skip the rest
ALTERNATIVES = {'pids_task': 'pids_signal', 'pids_kread': 'pids_signal',
                'argv_kernel': 'argv_user', 'tcp_hdr': 'tcp_btf', 'tcp_basic': 'tcp_btf'}
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
}
BTF_REQUIRED = {
    'tcp_btf':    {'tcp_sock': ('bytes_received', 'bytes_acked')},
    'tcp_accept': {'sock_common': ('skc_family', 'skc_rcv_saddr', 'skc_daddr',
                                   'skc_num', 'skc_dport')},
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
        out = subprocess.run(['bpftool', 'btf', 'dump', 'file', '/sys/kernel/btf/vmlinux', '-j'],
                             capture_output=True, timeout=300)
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


def _off(struct_name, member, default=0):
    return btf_offsets().get(struct_name, {}).get(member, default)


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
    argv_on = ('argv_user' in f) or ('argv_kernel' in f)
    read_user = 'bpf_probe_read_user' if 'argv_user' in f else 'bpf_probe_read'

    # ---- exit-time reads --------------------------------------------------
    cpu = ("""
    d.utime_ns   = task->utime + task->signal->utime;
    d.stime_ns   = task->stime + task->signal->stime;
    d.cutime_ns  = task->signal->cutime;
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
    if 'io' in f and 'signal' in f:
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
    # kretprobes on the protocol entry points, NOT on sock_sendmsg: tcp_sendmsg
    # and udp_sendmsg take `struct sock *` and are inet by construction, so unix
    # sockets (all the MCP traffic) are excluded without reading any struct
    # member, and the return value is the byte count actually moved.
    netb_defs = ("""
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

/* All four return `int`, so the ABI only defines eax — the upper 32 bits of rax
 * are whatever was there before. Truncate to s32 and sign-extend, or that
 * garbage is read as data: a 6.7 KB download first measured as 21474843382
 * bytes, which is exactly 20 GiB plus the true 6902. */
int kretprobe__tcp_sendmsg(struct pt_regs *ctx) { _netb_add((s64)(s32)PT_REGS_RC(ctx), 1); return 0; }
int kretprobe__tcp_recvmsg(struct pt_regs *ctx) { _netb_add((s64)(s32)PT_REGS_RC(ctx), 0); return 0; }
int kretprobe__udp_sendmsg(struct pt_regs *ctx) { _netb_add((s64)(s32)PT_REGS_RC(ctx), 1); return 0; }
int kretprobe__udp_recvmsg(struct pt_regs *ctx) { _netb_add((s64)(s32)PT_REGS_RC(ctx), 0); return 0; }
""" if 'netbytes' in f else '')
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
            if (len > 0 && %s(a->buf, len, (void *)s) == 0)
                argv_events.perf_submit(args, a, sizeof(*a));
        }
    }
""" % read_user) if argv_on else ''

    # ---- submit capture ----------------------------------------------------
    def _comm_test(name):
        parts = ' && '.join("d.comm[%d] == '%s'" % (i, c) for i, c in enumerate(name))
        return '(%s && d.comm[%d] == 0)' % (parts, len(name))
    submit_defs = ("""
#define WRITE_MAX %d
struct write_t { u32 pid; u32 fd; u32 len; char buf[WRITE_MAX]; };
BPF_HASH(submit_pids, u32, u8, 4096);
BPF_PERCPU_ARRAY(write_scratch, struct write_t, 1);
BPF_PERF_OUTPUT(write_events);

TRACEPOINT_PROBE(syscalls, sys_enter_write) {
    u32 pid = bpf_get_current_pid_tgid() >> 32;
    if (!submit_pids.lookup(&pid))
        return 0;
    if (args->fd > 2)
        return 0;
    u32 zero = 0;
    struct write_t *w = write_scratch.lookup(&zero);
    if (!w)
        return 0;
    u32 len = (u32)args->count;
    if (len > WRITE_MAX) len = WRITE_MAX;
    w->pid = pid; w->fd = (u32)args->fd; w->len = len;
    if (len > 0 && %s(w->buf, len, (void *)args->buf) == 0)
        write_events.perf_submit(args, w, sizeof(*w));
    return 0;
}
""" % (WRITE_KMAX, read_user)) if 'submit' in f else ''
    submit_exec = ("""
    if (%s) {
        u8 one = 1;
        submit_pids.update(&d.pid, &one);
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

    # The birth map is shared by the outbound tracepoint and the inbound accept
    # retprobe, and is also read from userspace each tick to describe connections
    # that are still open (netlink supplies their stats; it has no pid).
    tcp_common_defs = ("""
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
""") if (tcp_on or accept_on) else ''

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
};
BPF_PERF_OUTPUT(events);
__ARGV_DEFS____SUBMIT_DEFS____DSTATE_DEFS____NETB_DEFS____TCP_COMMON____TCP_TP____ACCEPT_PROBE__

TRACEPOINT_PROBE(sched, sched_process_fork) {
    struct data_t d = {};
    d.type     = EVT_FORK;
    d.pid      = args->child_pid;
    d.ppid     = args->parent_pid;
    d.ktime_ns = bpf_ktime_get_ns();
    d.uid      = (u32)bpf_get_current_uid_gid();
    events.perf_submit(args, &d, sizeof(d));
    return 0;
}

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
    if (tgid != tid) {
__DSTATE_DEL__        return 0;   // thread-group leader only: one record per process
    }
    struct task_struct *task = (struct task_struct *)bpf_get_current_task();
    struct data_t d = {};
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
__SCHED____IO____DELAY____DSTATE_TASK____DSTATE_READ____NETB_READ____PIDS____TTY____CGID__
    events.perf_submit(args, &d, sizeof(d));
__SUBMIT_EXIT__
    return 0;
}
"""
    return (text.replace('__INC__', '\n'.join(inc))
                .replace('__ARGV_DEFS__', argv_defs)
                .replace('__SUBMIT_DEFS__', submit_defs)
                .replace('__DSTATE_DEFS__', dstate_defs)
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
    for group in (PIDS_BLOCKS, TCP_BLOCKS, {'argv_user', 'argv_kernel'}, DSTATE_BLOCKS):
        winner = sorted(group & accepted)
        if winner:
            for nm in group - accepted:
                if nm in rejected and not rejected[nm].startswith('superseded'):
                    rejected[nm] = 'not needed (%s loaded); %s' % (winner[0], rejected[nm])
    if FEATURE_CACHE:
        try:
            os.makedirs(OUTPUT_DIR, exist_ok=True)
            with open(_feature_cache_path(), 'w') as fh:
                json.dump({'kernel': os.uname().release, 'features': sorted(accepted),
                           'rejected': rejected, 'btf_offs': btf_offsets(),
                           'probed_at': _now()}, fh, indent=1)
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


def _envelope(event):
    """The six-key record envelope, plus `ts_epoch`.

    `ts` is local, naive and second-granular -- fine for a human reading one
    host's file, useless for ordering or binning records from seven hosts against
    each other. `ts_epoch` is the authoritative key for both: UTC by definition,
    sub-second, and monotone enough to sort across hosts. `ts` is kept unchanged
    because every existing consumer parses it.

    Built here rather than copied per record type so a future envelope field
    lands on all of them; DailyWriter.write() backstops anything that still
    constructs one by hand."""
    return {'ts': _now(), 'ts_epoch': time.time(), 'host': HOSTNAME,
            'event': event, 'source': 'ebpf',
            'collector': COLLECTOR, 'schema_version': SCHEMA_VERSION}


def _top_n(d, n, or_none=False):
    """The largest `n` entries of a count map, highest first.

    Inlined five times before this existed. `or_none` returns None for an empty
    map, which the optional/root-gated maps want so an absent measurement does
    not render as an empty one."""
    if or_none and not d:
        return None
    return dict(sorted(d.items(), key=lambda kv: -kv[1])[:n])


class DailyWriter:
    def __init__(self, root, host):
        self.root, self.host, self.day, self.fh = root, host, None, None
        self.n = 0

    def write(self, rec):
        # Backstop for `ts_epoch`: every record type is supposed to get it from
        # _envelope(), but this is the one funnel all of them pass through, so a
        # record built by hand somewhere still carries the key the dashboard bins
        # on. setdefault, not assignment -- the envelope's value is stamped at
        # record construction and is the more accurate of the two.
        rec.setdefault('ts_epoch', time.time())
        day = datetime.now().strftime('%Y-%m-%d')
        if day != self.day:
            if self.fh:
                self.fh.close()
            d = os.path.join(self.root, day)
            os.makedirs(d, exist_ok=True)
            self.fh = open(os.path.join(d, '%s.jsonl' % self.host), 'a')
            self.day = day
        self.fh.write(json.dumps(rec) + '\n')
        self.fh.flush()
        self.n += 1

    def close(self):
        if self.fh:
            self.fh.close()


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
_dropped = {'n': 0, 'comms': {}}     # item 6: what the actor filter discarded
_fork_prev = None


def _on_signal(signum, frame):
    global _stop
    _stop = True


signal.signal(signal.SIGTERM, _on_signal)
signal.signal(signal.SIGINT, _on_signal)


def _new_entry(pid, ppid, comm=None, **kw):
    e = {'pid': pid, 'ppid': ppid, 'comm': comm, 'args': None, 'argv_source': None,
         'argv_ktime': None, 'tty': None, 'user': None, 'uid': None, 'cgroup': None,
         'cwd': None, 'cwd_source': None, 'start_ticks': None,
     'args_raw_len': None, 'tick_cpu_s': None, 'sandbox_anc': _UNSET,
     'sandbox': None, 'sandbox_src': None, 'sandbox_detail': None,
     'env_flags': None, 'approval_mode': None, 'approval_src': None,
         'start_ep': None, 'exec_ktime': None, 'attributed': None, 'is_agent': False,
         'agent_pid': None, 'agent_type': None, 'depth': None,
         'exited': False, 'gc_ts': None, 'is_submit': False, 'submit_buf': None,
         'emitted': False}
    e.update(kw)
    return e


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
    m = proc.get(pid)
    if m is not None:
        proc_set_ppid(m, ppid if ppid is not None else m['ppid'])
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


def resolve(pid):
    """Nearest-agent-ancestor attribution, memoized.

    A fork-without-exec child carries its parent's argv (argv_source='inherit');
    that argv must NOT make the child its own agent root — it attaches to the
    parent's tree at depth+1, as it would under the /proc poller."""
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


def tree_root(pid):
    """Top-most agent ancestor — the census's session root — as opposed to
    agent_pid, the nearest one (the spawner). A VS Code helper is its own nearest
    agent but belongs to the extension host's tree."""
    m = proc.get(pid)
    if m is None or not resolve(pid):
        return None
    root = m['agent_pid']
    seen = set()
    while root is not None and root not in seen:
        seen.add(root)
        rm = proc.get(root)
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
        args = read_cmdline(pid, st['comm'])
        if args and not args.startswith('['):
            m['args'], m['argv_source'] = args, 'proc'
            m['args_raw_len'] = len(args)      # read_cmdline is uncapped: exact
            m['attributed'] = None
    return True


def seed_existing():
    """One-time /proc walk so pre-existing agents and parents resolve. The only
    full scan this collector ever does."""
    for pid in list_pids():
        _ingest_proc(pid)
        if WANT_CWD:
            m = proc.get(pid)
            if m is not None and m['cwd'] is None:
                c = read_cwd(pid)
                if c:
                    m['cwd'], m['cwd_source'] = c, 'proc'


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
    if (m.get('comm') or '') in SANDBOX_COMMS:
        return True
    pm = proc.get(m.get('ppid'))
    return pm is not None and (pm.get('comm') or '') in SANDBOX_COMMS


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
        m['cwd'], m['cwd_source'] = pm['cwd'], 'inherit'


def _resolve_cgid(cgid):
    """cgroup v2 id -> path via /sys/fs/cgroup inode numbers. Lazy; a miss
    triggers a rescan at most every 30 s (session scopes appear at login)."""
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
    root = '/sys/fs/cgroup'
    try:
        for dirpath, _dirnames, _files in os.walk(root):
            try:
                ino = os.stat(dirpath).st_ino
            except OSError:
                continue
            _cgid_map[ino] = dirpath[len(root):] or '/'
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
    if resolve(m['pid']):
        return 'agent', 'ancestry'
    if m.get('tty'):
        return 'human', 'tty'
    uid = m.get('uid')
    if MIN_UID and uid is not None and uid >= MIN_UID:
        return 'human', 'uid'
    return None, None


def _note_dropped(m):
    _dropped['n'] += 1
    c = m.get('comm') or '?'
    _dropped['comms'][c] = _dropped['comms'].get(c, 0) + 1


# ---------------------------------------------------------------------------
# core event handlers
# ---------------------------------------------------------------------------

def _cstr(raw):
    if raw is None:
        return ''
    if isinstance(raw, bytes):
        return raw.split(b'\x00', 1)[0].decode('utf-8', 'replace')
    return str(raw).split('\x00', 1)[0]


def on_fork(e):
    child, parent = e.pid, e.ppid
    # sched_process_fork fires for every clone(), threads included. A thread has
    # tgid != pid, never produces a leader exit event, and /proc/<tid>/stat
    # reports the whole group's numbers — registering one creates an immortal
    # ghost that double-counts its process.
    if child in proc and not proc[child]['exited']:
        pass                                  # known leader (its exit arrived first)
    else:
        tgid = _tgid_of(child)
        if tgid is None or tgid != child:
            return
    m = proc_add(child, parent)
    if m['exited']:
        proc_del(child)                       # pid recycled inside the GC window
        m = proc_add(child, parent)
    if m['uid'] is None and e.uid != 0xffffffff:
        m['uid'], m['user'] = e.uid, username(e.uid)
    for k in ('user', 'uid', 'cgroup', 'tty'):
        _inherit(m, k)
    _inherit_cwd(m)
    _inherit_sandbox(m)
    pm = proc.get(parent)
    if pm is not None and m['args'] is None and pm.get('args'):
        m['args'], m['argv_source'] = pm['args'], 'inherit'
        m['args_raw_len'] = pm.get('args_raw_len')


def on_exec(e):
    pid = e.pid
    m = proc_add(pid, e.ppid)
    proc_set_ppid(m, e.ppid)
    m['comm'] = _cstr(e.comm) or m['comm']
    m['exec_ktime'] = e.ktime_ns
    m['exited'] = False
    m['uid'] = e.uid
    m['user'] = username(e.uid)
    m['is_submit'] = m['is_submit'] or (m['comm'] in SUBMIT_COMMS)
    # exec replaces the image, so a previous argv is stale — unless the kernel
    # argv for THIS exec already arrived on its own ring (the rings are drained in
    # arbitrary order; the two events carry the same ktime_ns).
    if not (m['argv_source'] == 'kernel' and m['argv_ktime'] == e.ktime_ns):
        m['args'], m['argv_source'], m['argv_ktime'] = None, None, None
    tty_known = False
    if 'tty' in FEATURES:
        m['tty'] = _cstr(e.tty) if e.has_tty else None
        tty_known = True
    if 'cgid' in FEATURES and e.cgid:
        m['cgroup'] = _resolve_cgid(e.cgid) or m['cgroup']
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
                m['cwd'], m['cwd_source'] = c, 'proc'
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
    if WANT_SANDBOX and m['is_agent'] and m.get('env_flags') is None:
        # Once per agent root, never per exec. NAMES ONLY -- values can hold keys.
        names = read_environ_names(pid, ENV_PREFIXES)
        if names:
            m['env_flags'] = names
            if m['approval_mode'] is None and any('BYPASS' in n or 'DANGER' in n
                                                  for n in names):
                m['approval_mode'], m['approval_src'] = 'bypassed', 'env'
    _invalidate_attribution(pid)


def on_argv(a):
    pid = a.pid
    m = proc_add(pid, None)
    raw = ctypes.string_at(ctypes.addressof(a) + type(a).buf.offset, a.len)
    # raw_len is the pre-clamp length; > a.len means the kernel truncated, which
    # decode_argv must know or a half-token can classify as a whole one.
    raw_len = getattr(a, 'raw_len', 0) or a.len
    args = decode_argv(raw, truncated=(raw_len > a.len))
    if args:
        m['args'], m['argv_source'], m['argv_ktime'] = args, 'kernel', a.ktime_ns
        m['args_raw_len'] = raw_len
        if not m['comm']:
            m['comm'] = os.path.basename(args.split(' ', 1)[0])[:15]
        _invalidate_attribution(pid)


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

    An env-derived verdict (read once per agent root) wins, because a flag set
    through configuration is invisible in argv."""
    if m is None:
        return None, None
    if m.get('approval_mode'):
        return m['approval_mode'], m.get('approval_src') or 'env'
    mode, _sb = approval_mode_of(m.get('args'))
    return (mode, 'argv') if mode else (None, None)


# The canonical identity key set. Derived once at import from _base_identity's
# own output against an empty entry, so adding a field there needs no edit here.
_UNSET = object()              # 'not computed yet', distinct from a cached None
_IDENTITY_KEYS = None          # filled in below, after _base_identity is defined


def _base_identity(m, actor, attribution=None):
    agent_pid = m.get('agent_pid')
    return {
        'actor': actor,
        'actor3': actor3(actor, m.get('agent_type')),
        'attribution': attribution,
        'pid': m['pid'], 'ppid': m['ppid'], 'uid': m.get('uid'), 'user': m.get('user'),
        'tty': m.get('tty'), 'comm': m.get('comm') or '',
        'args': (m.get('args') or '')[:ARGS_MAXLEN] or None,
        'argv_source': m.get('argv_source'),
        # An honest denominator for "unresolvable" in the tool distribution: a
        # truncated shell payload was indistinguishable from a genuinely short
        # one. null (not false) when the raw length is unknown -- false would
        # claim "measured and complete".
        'args_len': m.get('args_raw_len'),
        'args_truncated': (None if m.get('args_raw_len') is None
                           else m['args_raw_len'] > ARGS_MAXLEN),
        'cwd': m.get('cwd'), 'cwd_source': m.get('cwd_source'),
        # sandbox = namespace isolation (ground truth); sandbox_ancestry = how it
        # got there. Their disagreement separates a bwrap PROBE from a real one.
        'sandbox': m.get('sandbox'), 'sandbox_src': m.get('sandbox_src'),
        'sandbox_detail': m.get('sandbox_detail'),
        'sandbox_ancestry': sandbox_ancestry_of(m),
        'approval_mode': _approval_of(m)[0], 'approval_src': _approval_of(m)[1],
        'env_flags': m.get('env_flags'),
        'agent_type': m.get('agent_type'), 'agent_pid': agent_pid,
        'session_key': ('apid:%s' % agent_pid) if agent_pid is not None else None,
        # session_key is pid-based and dies with a collector restart; this does not.
        'session_uuid': session_uuid(agent_pid),
        'tree_root_pid': tree_root(m['pid']) if agent_pid is not None else None,
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
    out['comm'] = comm
    return out


def on_exit(e):
    pid = e.pid
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
    m['gc_ts'] = time.time()

    actor, attribution = actor_of(m)
    if actor is None:
        _note_dropped(m)
        return
    if not INCLUDE_ROOTS and m['is_agent']:
        return

    start_ep, duration, dur_src = _duration_and_start(m, e)
    cpu_s = (e.utime_ns + e.stime_ns) / 1e9
    F = FEATURES
    io_bytes = (e.rd_bytes + e.wr_bytes) if 'io' in F else 0
    comm = m.get('comm') or ''
    if (not KEEP_TIMERS and not m['is_agent'] and comm in TIMER_COMMS
            and cpu_s == 0 and io_bytes == 0):
        return
    if duration is not None and duration < MIN_DURATION:
        return
    if cpu_s < MIN_CPU:
        return

    exit_code, sig, core = decode_status(e.exit_code)
    ds_s, ds_n, ds_max, ds_src = pick_dstate(e, F)
    rec = _envelope('exit')
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
        'threads':      null_if_off(e.nr_threads, 'signal', F),
        'min_flt':      e.min_flt,
        'maj_flt':      e.maj_flt,
        'cmaj_flt':     null_if_off(e.cmaj_flt, 'signal', F),
        'nvcsw':        e.nvcsw,
        'nivcsw':       e.nivcsw,
        'sched_run_s':  round(e.run_ns / 1e9, 4),
        'sched_wait_s': null_if_off(round(e.wait_ns / 1e9, 4), 'sched', F),
        'blkio_wait_s':     null_if_off(round(e.blkio_ns / 1e9, 4), 'delay', F),
        'swapin_wait_s':    null_if_off(round(e.swapin_ns / 1e9, 4), 'delay', F),
        'freepages_wait_s': null_if_off(round(e.freepages_ns / 1e9, 4), 'delay', F),
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
        'io': ({'rd_mb': round(e.rd_bytes / 1e6, 3), 'wr_mb': round(e.wr_bytes / 1e6, 3),
                'rchar_mb': round(e.rchar / 1e6, 3), 'wchar_mb': round(e.wchar / 1e6, 3),
                'scope': 'process' if 'signal' in F else 'leader'} if 'io' in F else None),
        # poller-compat placeholders (this record is a strict superset of proc_trace's)
        'samples': None, 'state_last': None,
    })
    writer.write(rec)
    m['emitted'] = True

    if m.get('is_submit') or m.get('submit_buf') is not None:
        _emit_submit(m, rec)


# ---------------------------------------------------------------------------
# submit capture (item: exact process -> Slurm job key)
# ---------------------------------------------------------------------------

def on_write(w):
    m = proc_add(w.pid, None)
    m['is_submit'] = True
    raw = ctypes.string_at(ctypes.addressof(w) + type(w).buf.offset, w.len)
    buf = m.get('submit_buf') or b''
    if len(buf) < 8192:
        m['submit_buf'] = buf + raw[:8192 - len(buf)]


def _emit_submit(m, exit_rec):
    text = (m.get('submit_buf') or b'').decode('utf-8', 'replace')
    jobs = parse_job_ids(text)
    rec = _envelope('submit')
    rec.update(_base_identity(m, exit_rec['actor'], exit_rec.get('attribution')))
    req = parse_submit_args(m.get('args'))
    rec.update(req)          # partition, gpus, array, time_limit_s, mem, cpus_per_task, req_src
    rec.update({
        'tool': exit_rec['comm'],
        'job_id': jobs[0] if jobs else None,
        'job_ids': jobs or None,
        # the tool's cwd IS the job's submit directory; named for the join rather
        # than for the kernel, because downstream groups on `work_dir`
        'work_dir': m.get('cwd'),
        'exit_code': exit_rec['exit_code'], 'signal': exit_rec['signal'],
        'start': exit_rec['start'], 'duration_s': exit_rec['duration_s'],
        'captured_bytes': len(text),
        'captured_tail': text[-300:] if text else None,
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


def on_tcp(t):
    """One record per closed connection (kind=1) or accepted connection (kind=2)."""
    m = proc.get(t.pid)
    actor, attribution = actor_of(m) if m is not None else (None, None)
    if actor is None and not TCP_ALL:
        return
    daddr = _ip_of(t, 'daddr_v4', 'daddr_v6', t.family)
    saddr = _ip_of(t, 'saddr_v4', 'saddr_v6', t.family)
    kind = 'accept' if t.kind == TCP_KIND_ACCEPT else 'tcp'
    rec = _envelope(kind)
    if m is not None:
        rec.update(_base_identity(m, actor, attribution))
    else:
        rec.update(_stub_identity(t.pid, t.uid, _cstr(t.comm)))
    rec.update({
        'family': 'inet6' if t.family == 10 else 'inet',
        'saddr': saddr, 'sport': t.sport, 'daddr': daddr, 'dport': t.dport,
        'external': is_external_ip(daddr), 'provider': _provider(daddr),
        'inbound': bool(t.inbound),
    })
    if kind == 'tcp':
        rec.update({
            'duration_s': round(t.dur_ns / 1e9, 3),
            # SYN_SENT straight to CLOSE with no ESTABLISHED is a refused or
            # timed-out connect, which is a provider-health signal
            'established': bool(t.established),
            'connect_ms': round(t.connect_ns / 1e6, 2) if t.established and t.connect_ns else None,
            'rx_bytes': t.rx_bytes if t.has_bytes else None,
            'tx_bytes': t.tx_bytes if t.has_bytes else None,
        })
    writer.write(rec)


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
        by_key[sockdiag.key_of(r)] = r
    now_ktime = time.monotonic_ns() if hasattr(time, 'monotonic_ns') else int(time.time() * 1e9)
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
                'family': 'inet6' if fam == 10 else 'inet',
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
    # them makes this feed a superset of it.
    if CONN_UNTRACKED:
        for r in rows:
            k = sockdiag.key_of(r)
            if k in matched:
                continue
            if r['state'] != 'ESTABLISHED':
                continue
            if not is_external_ip(r['daddr']) and not CONN_ALL:
                continue
            rec = _envelope('conn')
            rec.update(_stub_identity(None, r['uid'], None))
            rec.update({
                   'family': r['family'], 'saddr': r['saddr'], 'sport': r['sport'],
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
    global _fork_prev
    live = [m for m in proc.values() if not m['exited']]
    trees, totals, by_type = {}, {}, {}
    stacks_read = 0
    global_frames = {}
    now = time.time()
    for m in live:
        actor, _attr = actor_of(m)
        if actor is None:
            continue
        st = read_stat(m['pid'])
        if st is None:
            m['exited'] = True                # vanished without an exit event we saw
            m['gc_ts'] = m['gc_ts'] or now
            continue
        if m.get('start_ep') is None:
            m['start_ep'] = start_epoch(st['starttime'])
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
                                    'd_state': 0, 'max_depth': 0, 'comms': {},
                                    'agent_types': set(), 'frames': {},
                                    'states': {}, 'procs': []})
        tr['n_procs'] += 1
        tr['threads'] += st['threads']
        tr['rss_mb'] += rss_kb / 1024.0
        tr['cpu_s'] += cpu_s
        tr['d_state'] += 1 if in_d else 0
        tr['max_depth'] = max(tr['max_depth'], m.get('depth') or 0)
        tr['agent_types'].add(m.get('agent_type'))
        c = m.get('comm') or '?'
        tr['comms'][c] = tr['comms'].get(c, 0) + 1
        # item 10: zombie census. state_z_frac was lost at the poller cutover --
        # an exit-triggered collector cannot see a zombie, because a zombie has
        # not exited. The tick can, and st['state'] is already read here.
        tr['states'][st['state']] = tr['states'].get(st['state'], 0) + 1
        # item 9: the row itself, so a live view can name the process burning CPU
        # right now. Every field is already in hand -- selection only, no syscall.
        tr['procs'].append({'pid': m['pid'], 'comm': c,
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
                root['cwd'], root['cwd_source'] = c, 'proc'
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
            'by_comm': _top_n(tr['comms'], 15),
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
                    'by_comm': _top_n(_dropped['comms'], 10)},
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
    })
    writer.write(_tot)
    _dropped['n'] = 0
    _dropped['comms'] = {}


def gc():
    cutoff = time.time() - GRACE_S
    for pid in [p for p, m in proc.items() if m['exited'] and (m['gc_ts'] or 0) < cutoff]:
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
            'exit_code': None, 'signal': None, 'core_dumped': None,
            'samples': None,
        })
        writer.write(rec)
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


def main():
    global FEATURES, writer, _cidr_index
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

    writer = DailyWriter(OUTPUT_DIR, HOSTNAME)
    if _load_cidr_index is not None:
        try:
            _cidr_index = _load_cidr_index()
        except Exception:
            _cidr_index = None

    seed_existing()
    b, FEATURES, rejected = probe_features(BPF)
    feats = FEATURES

    lost = {'events': 0, 'argv_events': 0, 'write_events': 0, 'tcp_events': 0}
    # Events DECODED, not records written. The drain touches every fork/exec/exit
    # to keep the ancestry table current, even for processes that are never
    # emitted, so this is the only number that makes the collector's userspace CPU
    # interpretable -- and, with `lost`, the pair that says whether it kept up.
    seen = {'events': 0, 'argv_events': 0, 'write_events': 0, 'tcp_events': 0}

    def _cb_core(cpu, data, size):
        e = b['events'].event(data)
        seen['events'] += 1
        try:
            if e.type == EVT_FORK:
                on_fork(e)
            elif e.type == EVT_EXEC:
                on_exec(e)
            elif e.type == EVT_EXIT:
                on_exit(e)
        except Exception:
            pass

    def _mk_cb(table, fn):
        def _cb(cpu, data, size):
            seen[table] += 1
            try:
                fn(b[table].event(data))
            except Exception:
                pass
        return _cb

    def _mk_lost(name):
        def _l(n):
            lost[name] += n
        return _l

    b['events'].open_perf_buffer(_cb_core, page_cnt=PAGE_CNT, lost_cb=_mk_lost('events'))
    if 'argv_user' in feats or 'argv_kernel' in feats:
        b['argv_events'].open_perf_buffer(_mk_cb('argv_events', on_argv), page_cnt=PAGE_CNT,
                                          lost_cb=_mk_lost('argv_events'))
    if 'submit' in feats:
        b['write_events'].open_perf_buffer(_mk_cb('write_events', on_write), page_cnt=64,
                                           lost_cb=_mk_lost('write_events'))
    if (TCP_BLOCKS | {'tcp_accept'}) & feats:
        b['tcp_events'].open_perf_buffer(_mk_cb('tcp_events', on_tcp), page_cnt=64,
                                         lost_cb=_mk_lost('tcp_events'))

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
            'argv_source_default': 'kernel' if ('argv_user' in feats or 'argv_kernel' in feats) else 'proc',
            'dstate_src_default': ('task' if 'dstate_task' in feats
                                   else ('stat' if 'dstate_stat' in feats else None)),
            'btf_offs': btf_offsets() or None,
            # two of the eight data points are inert when these read 0
            'sysctl': sysctls,
            'cgroup': _self_cgroup(),
            'netlink_sockdiag': sockdiag is not None,
            'config': {'args_maxlen': ARGS_MAXLEN, 'argv_kmax': ARGV_KMAX,
                       'write_kmax': WRITE_KMAX, 'residency_s': RESIDENCY_S,
                       'keep_timers': KEEP_TIMERS, 'include_roots': INCLUDE_ROOTS,
                       'tcp_all': TCP_ALL, 'conn_all': CONN_ALL,
                       'conn_untracked': CONN_UNTRACKED, 'cwd': WANT_CWD,
                       'cwd_always': CWD_ALWAYS, 'min_uid': MIN_UID,
                       'sandbox': WANT_SANDBOX, 'sandbox_always': SANDBOX_ALWAYS,
                       'env_prefixes': list(ENV_PREFIXES),
                       'dstack_max': DSTACK_MAX, 'flush': FLUSH_ON_EXIT,
                       'submit_comms': sorted(SUBMIT_COMMS), 'gc_grace_s': GRACE_S},
            'seeded_pids': len(proc)})
    writer.write(meta)
    for k, v in sysctls.items():
        if v == '0':
            sys.stderr.write('warning: %s=0 — the dstate_*/blkio_* fields will read 0 '
                             '(run `ebpfm.sh bootstrap` to set it)\n' % k)
    sys.stderr.write('%s running on %s -> %s/<date>/%s.jsonl (features: %s)\n'
                     % (COLLECTOR, HOSTNAME, OUTPUT_DIR, HOSTNAME,
                        ','.join(sorted(feats)) or 'base'))

    start = time.time()
    last_gc = last_res = start
    while not _stop:
        try:
            b.perf_buffer_poll(timeout=POLL_MS)
        except Exception:
            pass
        now = time.time()
        if now - last_gc > 60:
            gc()
            last_gc = now
        if RESIDENCY_S and now - last_res >= RESIDENCY_S:
            try:
                residency_tick(b)
            except Exception:
                pass
            last_res = now
        if DURATION and (now - start) >= DURATION:
            break

    n_trunc = 0
    if FLUSH_ON_EXIT:
        try:
            n_trunc = flush_live(b, feats)
        except Exception:
            n_trunc = 0
    # NB: `_stop` is the module-global shutdown flag read below -- do not shadow it.
    stop_rec = _envelope('stop')
    stop_rec.update({'uptime_s': round(time.time() - start, 1), 'records': writer.n,
                     'truncated': n_trunc, 'perf_lost': lost, 'perf_seen': seen,
                     'reason': 'duration' if DURATION and not _stop else 'signal'})
    writer.write(stop_rec)
    if any(lost.values()):
        sys.stderr.write('warning: perf events dropped: %s\n' % json.dumps(lost))
    writer.close()


if __name__ == '__main__':
    main()
