#!/usr/bin/env python3
"""Collect node metrics and print a single JSON line to stdout.

Shared by the login and compute monitors. The schema is uniform across both:
GPU sections are populated only when `nvidia-smi` is present (null otherwise),
and the `slurm` section only when running under Slurm (null otherwise).
"""
import sys, os, json, re, shutil, subprocess, time, pwd
from collections import Counter
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache

hostname = sys.argv[1]
top_n    = int(sys.argv[2])

try:
    CMD_TIMEOUT = float(os.environ.get('SNAPSHOT_CMD_TIMEOUT', '20'))
except ValueError:
    CMD_TIMEOUT = 20.0

# Max length kept for process command lines. Configurable so an operator can
# capture fuller argv (e.g. to read agent flags) at the cost of larger JSONL.
try:
    ARGS_MAXLEN = int(os.environ.get('SNAPSHOT_ARGS_MAXLEN', '256'))
except ValueError:
    ARGS_MAXLEN = 256

# Whole-host process tree (pid/ppid/comm for every process + agent tags), so a
# captured `grep`/`find` can be walked up to the agent that spawned it. One
# [pid, ppid, comm] triple per process; set SNAPSHOT_PROC_TREE=0 to disable on
# fork-heavy nodes where the extra per-snapshot size is unwelcome.
PROC_TREE = os.environ.get('SNAPSHOT_PROC_TREE', '1') != '0'

# Privileged mode: when the collector runs as root (or with CAP_SYS_PTRACE /
# CAP_SYS_ADMIN) the kernel un-gates /proc/<pid>/{io,wchan,syscall,stack,fd} for
# *other* users, so the per-process I/O, wait-channel and kernel-stack fields
# resolve cluster-wide instead of own-user-only. Collectors that are only useful
# (or only readable) with privilege are gated on this flag and degrade to
# null/absent otherwise, keeping the default unprivileged feed unchanged.
# See limitations.md.
IS_ROOT = (os.geteuid() == 0)
# Cap on how many D-state processes get the (privileged) per-proc stack/fd reads,
# to bound runtime on nodes with thousands of hung procs (e.g. an NFS hang).
try:
    D_STACK_MAX = int(os.environ.get('SNAPSHOT_D_STACK_MAX', '300'))
except ValueError:
    D_STACK_MAX = 300

# Schema version of this record shape. 1 == the shape snapshot.py has emitted
# since it was written, so historical files (which carry no key) read as
# "absent == 1". Bump only when a field's *meaning* changes -- the full/node
# distinction below is a runtime MODE, not a version, and lives in
# collector.mode. This counter is per-collector: it is unrelated to the eBPF
# collector's own schema_version.
SCHEMA_VERSION = 1

# Collection mode.
#   full  (default)  every section, exactly as always. Used by `compute`, and by
#                    `login` on any node where the root eBPF tier is NOT running.
#   node             the four ptrace-crippled / eBPF-superseded per-process
#                    sections are skipped and emitted as null:
#                      top_io_processes    -> ebpfm exit.io.* (all users, exact)
#                      top_sleeping_procs  -> ebpfm residency.d_stack
#                      sleeping_wchans     -> ebpfm residency_totals.d_stack_top
#                      socket_talkers      -> ebpfm tcp / accept / conn records
#                    Everything else -- including d_state_procs, proc_tree,
#                    per_user_cgroup and proc_stat.btime (the reboot-era source
#                    for EVERY dataset, analyze/split_by_reboot.py) -- is kept.
#
# The dropped keys are set to None IN PLACE. They are never deleted and the key
# order never changes: analyze/login_responsiveness.py regex-parses the raw bytes
# BEFORE the "responsiveness" key and depends on the preceding keys' order.
MODE = os.environ.get('SNAPSHOT_MODE', 'full')
if MODE not in ('full', 'node'):
    sys.exit("snapshot.py: SNAPSHOT_MODE must be 'full' or 'node', got %r" % MODE)
NODE_MODE = (MODE == 'node')

@lru_cache(maxsize=None)
def command_exists(program):
    return shutil.which(program) is not None

HAS_GPU = command_exists('nvidia-smi')

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def run(cmd, timeout=CMD_TIMEOUT):
    if '/' not in cmd[0] and not command_exists(cmd[0]):
        return ''
    try:
        r = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            env={**os.environ, 'LC_ALL': 'C'},
        )
        return r.stdout.decode('utf-8', errors='replace')
    except Exception:
        return ''

def _float(s):
    try:    return float(s)
    except: return None

def _int(s):
    try:    return int(s)
    except: return None

def _field(parts, index, *names):
    for name in names:
        i = index.get(name)
        if i is not None and i < len(parts):
            return parts[i]
    return None

def _field_float(parts, index, *names, scale=1.0):
    value = _field(parts, index, *names)
    parsed = _float(value)
    return None if parsed is None else parsed * scale

# ---------------------------------------------------------------------------
# parsers
# ---------------------------------------------------------------------------

def parse_uptime(s):
    m = re.search(r'(\d+)\s+user', s)
    users = int(m.group(1)) if m else 0
    loads = re.findall(r'load average:\s*([\d.]+),\s*([\d.]+),\s*([\d.]+)', s)
    return {
        'users':   users,
        'load_1':  _float(loads[0][0]) if loads else None,
        'load_5':  _float(loads[0][1]) if loads else None,
        'load_15': _float(loads[0][2]) if loads else None,
    }

def parse_w(s):
    users = []
    for line in s.splitlines()[2:]:
        p = line.split(None, 7)
        if len(p) >= 6:
            users.append({
                'user':  p[0],
                'tty':   p[1],
                'from':  p[2],
                'login': p[3],
                'idle':  p[4],
                'jcpu':  p[5],
                'pcpu':  p[6] if len(p) > 6 else '',
                'what':  p[7].strip()[:200] if len(p) > 7 else '',
            })
    return users

def parse_proc_states(s):
    c = {}
    for line in s.splitlines():
        k = line.strip()[:1]
        c[k] = c.get(k, 0) + 1
    return {
        'running':    c.get('R', 0),
        'blocked_io': c.get('D', 0),
        'sleeping':   c.get('S', 0),
        'zombie':     c.get('Z', 0),
        'stopped':    c.get('T', 0),
    }

def parse_memory(s):
    out = {}
    for line in s.splitlines():
        p = line.split()
        if len(p) >= 7 and p[0] in ('Mem:', 'Swap:'):
            key = p[0].rstrip(':').lower()
            out[key] = {
                'total_gb':      round(int(p[1]) / 1e9, 1),
                'used_gb':       round(int(p[2]) / 1e9, 1),
                'free_gb':       round(int(p[3]) / 1e9, 1),
            }
            if p[0] == 'Mem:':
                out[key]['shared_gb']     = round(int(p[4]) / 1e9, 1)
                out[key]['buff_cache_gb'] = round(int(p[5]) / 1e9, 1)
                out[key]['available_gb']  = round(int(p[6]) / 1e9, 1)
    return out

def parse_mpstat(s):
    cpus = []
    for line in s.splitlines():
        if not line.startswith('Average:'):
            continue
        p = line.split()
        if len(p) < 12 or p[1] == 'CPU':
            continue
        try:
            cpus.append({
                'cpu':    p[1],
                'usr':    float(p[2]),
                'sys':    float(p[4]),
                'iowait': float(p[5]),
                'irq':    float(p[6]),
                'soft':   float(p[7]),
                'idle':   float(p[11]),
            })
        except (ValueError, IndexError):
            pass
    return cpus

def parse_vmstat(s):
    lines = [l for l in s.splitlines() if l.strip()]
    if len(lines) < 3:
        return {}
    p = lines[-1].split()
    keys = ['procs_r','procs_b','swpd_kb','free_kb','buff_kb','cache_kb',
            'si','so','bi','bo','interrupts_s','ctxt_switches_s',
            'cpu_us','cpu_sy','cpu_id','cpu_wa','cpu_st']
    return {k: _int(v) for k, v in zip(keys, p)}

def parse_iostat(s):
    header, index, devices = None, None, []
    # iostat column order changes between sysstat versions; parse the last
    # device block by header name so await/util fields stay truthful.
    for line in s.splitlines():
        if not line.strip() or line.startswith('avg-cpu'):
            continue
        if line.startswith('Device'):
            header = line.split()
            index = {name: i for i, name in enumerate(header)}
            devices = []
            continue
        if header is None or index is None:
            continue
        p = line.split()
        device = _field(p, index, 'Device', 'Device:')
        if not device:
            continue
        rkb_s = _field_float(p, index, 'rkB/s')
        if rkb_s is None:
            rmb_s = _field_float(p, index, 'rMB/s')
            rkb_s = None if rmb_s is None else rmb_s * 1024
        wkb_s = _field_float(p, index, 'wkB/s')
        if wkb_s is None:
            wmb_s = _field_float(p, index, 'wMB/s')
            wkb_s = None if wmb_s is None else wmb_s * 1024
        devices.append({
            'device':   device,
            'r_s':      _field_float(p, index, 'r/s') or 0.0,
            'w_s':      _field_float(p, index, 'w/s') or 0.0,
            'rkb_s':    rkb_s or 0.0,
            'wkb_s':    wkb_s or 0.0,
            'r_await':  _field_float(p, index, 'r_await', 'await') or 0.0,
            'w_await':  _field_float(p, index, 'w_await', 'await') or 0.0,
            'util_pct': _field_float(p, index, '%util') or 0.0,
        })
    return {'devices': devices}

def parse_nfsiostat(s):
    mounts, seen, cur, in_second, state = [], {}, None, False, None
    for line in s.splitlines():
        m = re.match(r'.+ mounted on ([^:]+):', line)
        if m:
            mp = m.group(1)
            in_second = mp in seen
            seen[mp] = True
            cur = {'mountpoint': mp, 'ops_s': 0.0,
                   'rd_kb_s': 0.0, 'rd_rtt_ms': 0.0, 'rd_exe_ms': 0.0, 'rd_retrans': 0,
                   'wr_kb_s': 0.0, 'wr_rtt_ms': 0.0, 'wr_exe_ms': 0.0, 'wr_retrans': 0,
                   } if in_second else None
            state = None
            continue
        if cur is None:
            continue
        ls = line.strip()
        if re.match(r'ops/s', ls):
            state = 'ops'
        elif state == 'ops' and ls and ls[0].isdigit():
            p = ls.split()
            cur['ops_s'] = _float(p[0]) or 0.0
            state = None
        elif ls.startswith('read:'):
            state = 'read'
        elif state == 'read' and ls and (ls[0].isdigit() or ls[0] == '-'):
            # columns: ops/s  kB/s  kB/op  retrans  avg_rtt  avg_exe  avg_queue  errors
            # retrans/errors render as "0 (0.0%)" (2 tokens); drop the "(...%)"
            # tokens so the remaining fields are positional in every nfsiostat version
            p = [t for t in ls.split() if not t.startswith('(')]
            cur['rd_kb_s']    = _float(p[1]) or 0.0
            cur['rd_retrans'] = _int(p[3]) or 0
            cur['rd_rtt_ms']  = _float(p[4]) or 0.0
            cur['rd_exe_ms']  = _float(p[5]) or 0.0
            state = None
        elif ls.startswith('write:'):
            state = 'write'
        elif state == 'write' and ls and (ls[0].isdigit() or ls[0] == '-'):
            p = [t for t in ls.split() if not t.startswith('(')]
            cur['wr_kb_s']    = _float(p[1]) or 0.0
            cur['wr_retrans'] = _int(p[3]) or 0
            cur['wr_rtt_ms']  = _float(p[4]) or 0.0
            cur['wr_exe_ms']  = _float(p[5]) or 0.0
            if cur['ops_s'] > 0.005:
                mounts.append(cur)
            cur = None
            state = None
    return sorted(mounts, key=lambda x: x['ops_s'], reverse=True)


def parse_mountstats():
    """Cumulative NFS retransmits, bad_xid, slot backlog, and RTT breakdown."""
    try:
        with open('/proc/self/mountstats') as f:
            text = f.read()
    except Exception:
        return []
    mounts, cur = [], None
    for line in text.splitlines():
        m = re.match(r'device\s+\S+\s+mounted on\s+(\S+)\s+with fstype\s+(nfs\S*)', line)
        if m:
            cur = {'mountpoint': m.group(1), 'fstype': m.group(2),
                   'bad_xid': 0, 'backlog_u': 0.0}
            mounts.append(cur)
            continue
        if cur is None:
            continue
        ls = line.strip()
        # xprt: tcp srcport bind_cnt conn_cnt conn_time idle_time sends recvs bad_xids req_u backlog_u
        if ls.startswith('xprt:') and 'tcp' in ls:
            p = ls.split()
            try:
                cur['bad_xid']  = int(p[9])
                cur['backlog_u'] = float(p[11]) if len(p) > 11 else 0.0
            except (ValueError, IndexError):
                pass
        # per-op: OP: ops transmissions major_timeouts bytes_sent bytes_recv
        #         queue_ms rtt_ms exe_ms errors
        # Transmissions includes the original request, so retransmissions are
        # transmissions minus operations.
        for op_tag, op_key in (('READ:', 'read'), ('WRITE:', 'write')):
            if ls.startswith(op_tag):
                p = ls.split()
                if len(p) >= 9:
                    try:
                        ops = int(p[1])
                        if ops > 0:
                            cur[op_key] = {
                                'ops':          ops,
                                'retrans':      max(0, int(p[2]) - ops),
                                'avg_queue_ms': round(int(p[6]) / ops, 2),
                                'avg_rtt_ms':   round(int(p[7]) / ops, 2),
                                'avg_exe_ms':   round(int(p[8]) / ops, 2),
                            }
                    except (ValueError, IndexError):
                        pass
    return [m for m in mounts if m.get('fstype', '').startswith('nfs')]


def parse_nfs_rpc():
    """NFS client-level RPC call and retransmit counts from /proc/net/rpc/nfs."""
    try:
        with open('/proc/net/rpc/nfs') as f:
            text = f.read()
    except Exception:
        return {}
    for line in text.splitlines():
        if line.startswith('rpc '):
            p = line.split()
            if len(p) >= 3:
                try:
                    return {
                        'calls':           int(p[1]),
                        'retransmissions': int(p[2]),
                        'auth_refreshes':  int(p[3]) if len(p) > 3 else 0,
                    }
                except ValueError:
                    return {}
    return {}


def parse_mount_topology():
    """Census of the autofs/NFS mount layer from /proc/self/mountinfo.

    The login-node wedge originates here: a `bwrap --ro-bind / /` sandbox probe
    forces autofs to traverse every /net automount, and one unresponsive target
    blocks the bind in `autofs_mount_wait` forever, piling up D-state processes.
    A target that *fails to mount* never appears in `nfs_mountstats`, so that
    field is blind exactly at the trigger; this census tracks the surrounding
    topology instead — how many autofs trigger points exist, how many NFS mounts
    are live, and which distinct NFS servers are currently mounted. A server
    disappearing from `nfs_servers` (or `nfs_mounts` dropping) between snapshots
    is a trigger signal that precedes the D-state pileup.

    Reading /proc/self/mountinfo is a pure mount-table read: it lists what is
    already mounted and does NOT stat() or traverse automount paths, so it cannot
    itself block on a hung mount (unlike walking /net).
    """
    try:
        with open('/proc/self/mountinfo') as f:
            text = f.read()
    except Exception:
        return {}
    autofs = 0
    nfs = 0
    servers = set()
    for line in text.splitlines():
        sep = line.split(' - ', 1)
        if len(sep) != 2:
            continue
        right = sep[1].split()
        if not right:
            continue
        fstype = right[0]
        source = right[1] if len(right) > 1 else ''
        if fstype == 'autofs':
            autofs += 1
        elif fstype.startswith('nfs'):
            nfs += 1
            if ':' in source:                 # server:/export/path
                host = source.split(':', 1)[0]
                if host:
                    servers.add(host)
    return {
        'autofs_triggers':  autofs,
        'nfs_mounts':       nfs,
        'nfs_server_count': len(servers),
        'nfs_servers':      sorted(servers),
    }


# Filesystem types backed by a local block device (or RAM). statvfs() on these is
# an in-kernel read that returns immediately; NFS/autofs/fuse are deliberately
# excluded because statvfs() on a hung network mount blocks uninterruptibly and
# would stall the whole snapshot — disk-space probing never touches them.
_LOCAL_FSTYPES = ('xfs', 'ext4', 'ext3', 'ext2', 'btrfs', 'f2fs')
_TMPFS_KEEP    = {'/tmp', '/var/tmp', '/dev/shm'}

def parse_disk_space():
    """Free space + inode use for LOCAL filesystems only (a hang-proof `df`).

    A full `/` or `/tmp`, or inode exhaustion, is a classic login-node wedge that the
    iostat *rate* fields cannot reveal. Mounts are discovered from the mount table we
    already read; only block-backed filesystems (one row per device) and a small
    tmpfs allowlist are statvfs'd, so a degraded NFS/autofs mount can never block this
    probe. See _LOCAL_FSTYPES above.
    """
    try:
        with open('/proc/self/mountinfo') as f:
            text = f.read()
    except Exception:
        return []
    out, seen_dev = [], set()
    for line in text.splitlines():
        sep = line.split(' - ', 1)
        if len(sep) != 2:
            continue
        left, right = sep[0].split(), sep[1].split()
        if len(left) < 5 or not right:
            continue
        majmin, mountpoint, fstype = left[2], left[4], right[0]
        if fstype in _LOCAL_FSTYPES:
            if majmin in seen_dev:               # one row per physical device
                continue
            seen_dev.add(majmin)
        elif not (fstype == 'tmpfs' and mountpoint in _TMPFS_KEEP):
            continue                             # skip nfs/autofs/fuse + tmpfs noise
        try:
            st = os.statvfs(mountpoint)
        except OSError:
            continue
        total = st.f_blocks * st.f_frsize
        if total == 0:
            continue
        out.append({
            'mountpoint':     mountpoint,
            'fstype':         fstype,
            'total_gb':       round(total / 1e9, 1),
            'used_pct':       round(100 * (1 - st.f_bfree / st.f_blocks), 1),
            'inode_used_pct': round(100 * (1 - st.f_ffree / st.f_files), 1) if st.f_files else 0.0,
        })
    return out


def parse_d_state_procs(s):
    """Processes blocked in uninterruptible I/O wait (D state).

    `wchan` is the kernel wait channel they are parked in, so NFS/RPC waits can
    be told apart from local-disk or lock waits. The kernel gates
    /proc/<pid>/wchan behind ptrace_may_access, so `wchan` is `-` for processes
    owned by other users (the unprivileged collector can only resolve its own).
    See limitations.md ("fields scoped to the collector's own user").

    `ppid` (always captured) lets a hung sandbox be traced to the IDE/agent that
    spawned it. When the collector is privileged, the first D_STACK_MAX procs also
    get `kstack` (kernel call path, deeper than wchan) and `fd_count`.
    """
    procs = []
    detailed = 0
    for line in s.splitlines():
        p = line.split(None, 5)         # pid ppid stat user wchan args
        if len(p) >= 6 and p[2].startswith('D'):
            try:
                pid = int(p[0])
                rec = {
                    'pid':   pid,
                    'ppid':  int(p[1]),
                    'stat':  p[2],
                    'user':  p[3],
                    'wchan': p[4],
                    'args':  p[5][:ARGS_MAXLEN],
                }
                if IS_ROOT and detailed < D_STACK_MAX:
                    detailed += 1
                    kstack = _proc_kstack(pid)
                    if kstack:
                        rec['kstack'] = kstack
                    fd_count = _proc_fd_count(pid)
                    if fd_count is not None:
                        rec['fd_count'] = fd_count
                procs.append(rec)
            except (ValueError, IndexError):
                pass
    return procs


# Coding-agent / IDE / sandbox classification for the whole-host process tree.
# Deliberately small and dependency-free (the richer classifier lives in
# analyze/agent_lib.py for offline use). Order matters: an agent running inside
# an IDE host (e.g. Claude Code under .vscode-server) is tagged as the agent, and
# a Cursor-tagged bwrap is told apart from a generic sandbox probe.
_AGENT_PATTERNS = [
    ('claude_code', re.compile(r'anthropic\.claude-code|native-binary/claude'
                               r'|/\.claude/|claude-agent-sdk|(?:^|/|\s)claude(?:\s|$)', re.I)),
    ('codex',       re.compile(r'@openai/codex|openai\.chatgpt|codex[ -]app-server'
                               r'|/\.codex/|codex-linux-sandbox|codex-bin|codex-homes'
                               r'|(?:^|/|\s)codex(?:\s|$|@)', re.I)),
    ('copilot',     re.compile(r'github\.copilot|/copilot(?:\s|$)', re.I)),
    ('cursor',      re.compile(r'CURSOR_SANDBOX|\.cursor-server|\.cursor/', re.I)),
    ('windsurf',    re.compile(r'windsurf-terminal|WINDSURF_STATE|\.windsurf-server'
                               r'|\.codeium/windsurf', re.I)),
    ('vscode',      re.compile(r'\.vscode-server|\.vscode/cli', re.I)),
    ('bwrap',       re.compile(r'(?:^|/)bwrap(?:\s|$)')),
]


def classify_agent(args):
    """Tag a command line as a coding agent / IDE / sandbox, else None."""
    for name, rx in _AGENT_PATTERNS:
        if rx.search(args):
            return name
    return None


def parse_proc_tree(s):
    """Compact whole-host process tree for parent-chain attribution.

    Input is `ps -eo pid,ppid,args` for every process. We keep pid -> ppid plus
    a short comm for all user processes (kernel threads — children of pid 2 — are
    dropped), and separately tag the pids whose command line classifies as a
    coding agent / IDE / sandbox. Offline analysis can then walk any process up
    its ppid chain — e.g. a `grep`/`find` whose own argv carries no agent marker —
    to the IDE/agent that ultimately spawned it, which path-only attribution
    cannot do. comm is the arg0 basename (capped); full argv for the agent pids
    is already carried in the top-N / d_state lists. Disable with
    SNAPSHOT_PROC_TREE=0 if the per-snapshot size is a concern on fork-heavy
    nodes (one [pid, ppid, comm] triple per process).
    """
    procs = []
    agents = []
    for line in s.splitlines():
        p = line.split(None, 2)             # pid ppid args
        if len(p) < 2:
            continue
        try:
            pid, ppid = int(p[0]), int(p[1])
        except ValueError:
            continue
        if pid == 2 or ppid == 2:           # skip kernel threads (children of kthreadd)
            continue
        args = p[2] if len(p) > 2 else ''
        tok = args.split(None, 1)[0] if args else ''
        comm = tok if tok.startswith('[') else tok.rsplit('/', 1)[-1]
        procs.append([pid, ppid, comm[:24]])
        tag = classify_agent(args)
        if tag:
            agents.append([pid, tag])
    return {'procs': procs, 'agents': agents}


def parse_sar_dev(s):
    ifaces, index = [], None
    for line in s.splitlines():
        if not line.startswith('Average:'):
            continue
        p = line.split()
        if len(p) >= 2 and p[1] == 'IFACE':
            index = {name: i for i, name in enumerate(p)}
            continue
        if index is None:
            continue
        iface = _field(p, index, 'IFACE')
        rx_kb = _field_float(p, index, 'rxkB/s', 'rxKB/s') or 0.0
        tx_kb = _field_float(p, index, 'txkB/s', 'txKB/s') or 0.0
        if not iface or rx_kb + tx_kb < 0.1:
            continue
        ifaces.append({
            'iface':    iface,
            'rx_pkt_s': _field_float(p, index, 'rxpck/s') or 0.0,
            'tx_pkt_s': _field_float(p, index, 'txpck/s') or 0.0,
            'rx_kb_s':  rx_kb,
            'tx_kb_s':  tx_kb,
            'util_pct': _field_float(p, index, '%ifutil') or 0.0,
        })
    return sorted(ifaces, key=lambda x: x['rx_kb_s'] + x['tx_kb_s'], reverse=True)

def parse_ss(s):
    out = {}
    m = re.search(r'Total:\s+(\d+)', s)
    if m: out['total'] = int(m.group(1))
    m = re.search(r'TCP:\s+\d+ \(estab (\d+), closed (\d+), orphaned (\d+), timewait (\d+)\)', s)
    if m:
        out['tcp_estab']    = int(m.group(1))
        out['tcp_closed']   = int(m.group(2))
        out['tcp_orphaned'] = int(m.group(3))
        out['tcp_timewait'] = int(m.group(4))
    m = re.search(r'^UDP\s+(\d+)', s, re.MULTILINE)
    if m: out['udp_total'] = int(m.group(1))
    m = re.search(r'^TCP\s+(\d+)', s, re.MULTILINE)
    if m: out['tcp_total'] = int(m.group(1))
    return out

def parse_per_user(s, n):
    users = {}
    for line in s.splitlines():
        p = line.split()
        if len(p) < 3:
            continue
        u = p[0]
        if u not in users:
            users[u] = {'user': u, 'cpu_pct': 0.0, 'rss_mb': 0.0, 'procs': 0}
        users[u]['cpu_pct'] += _float(p[1]) or 0.0
        users[u]['rss_mb']  += ((_int(p[2]) or 0) / 1024.0)
        users[u]['procs']   += 1
    result = sorted(users.values(), key=lambda x: x['cpu_pct'], reverse=True)
    for u in result:
        u['cpu_pct'] = round(u['cpu_pct'], 1)
        u['rss_mb']  = round(u['rss_mb'],  1)
    return result[:n]

def parse_ps(s, n):
    procs = []
    for line in s.splitlines():
        p = line.split()
        if len(p) < 9:
            continue
        # columns: pid ppid user %cpu %mem rss stat start time args
        # start field is 1 token (HH:MM:SS) or 2 tokens (Mon DD / May 26)
        if len(p) > 7 and p[7][0].isdigit():
            started, time_i = p[7], 8
        elif len(p) > 8:
            started, time_i = p[7] + ' ' + p[8], 9
        else:
            continue
        if time_i >= len(p):
            continue
        try:
            procs.append({
                'pid':     int(p[0]),
                'ppid':    int(p[1]),
                'user':    p[2],
                'cpu_pct': float(p[3]),
                'mem_pct': float(p[4]),
                'rss_kb':  int(p[5]),
                'stat':    p[6],
                'started': started,
                'time':    p[time_i],
                'args':    ' '.join(p[time_i+1:])[:ARGS_MAXLEN],
            })
        except (ValueError, IndexError):
            pass
    return procs[:n]

def _read_proc_io(pid):
    """Snapshot the cumulative IO byte counters of one pid, or None if unreadable.

    /proc/<pid>/io is only readable for our own processes (others -> EACCES),
    so this silently yields None for processes the unprivileged collector can't
    see — those are dropped rather than reported as -1.
    """
    try:
        io = {}
        with open('/proc/%s/io' % pid) as f:
            for line in f:
                k, _, v = line.partition(':')
                io[k.strip()] = int(v)
        with open('/proc/%s/stat' % pid) as f:
            blkio = int(f.read().rsplit(')', 1)[1].split()[39])
        return {
            'rd':    io.get('read_bytes', 0),
            'wr':    io.get('write_bytes', 0),
            'ccwr':  io.get('cancelled_write_bytes', 0),
            'blkio': blkio,
        }
    except Exception:
        return None

def _proc_meta(pid):
    """uid, ppid and command line for one pid, mirroring pidstat's UID/Command."""
    uid = ppid = None
    try:
        with open('/proc/%s/status' % pid) as f:
            for line in f:
                if line.startswith('Uid:'):
                    uid = int(line.split()[1])
                elif line.startswith('PPid:'):
                    ppid = int(line.split()[1])
                if uid is not None and ppid is not None:
                    break
    except Exception:
        pass
    cmd = ''
    try:
        with open('/proc/%s/cmdline' % pid) as f:
            cmd = f.read().replace('\0', ' ').strip()
        if not cmd:
            with open('/proc/%s/comm' % pid) as f:
                cmd = '[%s]' % f.read().strip()
    except Exception:
        pass
    return uid, ppid, cmd


def _proc_kstack(pid, depth=5):
    """Top kernel-stack frames of a process, or None if unreadable.

    /proc/<pid>/stack requires CAP_SYS_ADMIN/root, so this yields None unless the
    collector is privileged. For a D-state process it shows the exact call path
    it is blocked in (e.g. autofs/NFS/RPC), one level deeper than `wchan`.
    """
    try:
        frames = []
        with open('/proc/%s/stack' % pid) as f:
            for line in f:
                # format: "[<0>] func_name+0x12/0x34"
                m = re.search(r'\]\s+(\S+?)\+0x', line)
                if m:
                    frames.append(m.group(1))
                if len(frames) >= depth:
                    break
        return frames or None
    except Exception:
        return None


def _proc_fd_count(pid):
    """Number of open file descriptors, or None. listdir does not deref the fd
    symlinks, so this stays safe even for processes hung on a dead NFS mount.
    Readable for other users only with root."""
    try:
        return len(os.listdir('/proc/%s/fd' % pid))
    except Exception:
        return None

def collect_top_io_processes(n, interval=1.0):
    """Per-process IO rates from /proc/<pid>/io deltas over `interval` seconds.

    Replaces `pidstat -d`, which reports -1 for any process whose /proc/<pid>/io
    the caller can't read; unprivileged on a shared node that is ~every process.
    We instead sample our own readable processes twice and report real rates,
    silently skipping the ones we can't read. Consequently this section only
    covers the collector user's own processes — other tenants' per-process I/O
    is unavailable without CAP_SYS_PTRACE/root. See limitations.md.
    """
    pids = [d for d in os.listdir('/proc') if d.isdigit()]
    first = {pid: io for pid in pids for io in (_read_proc_io(pid),) if io}
    time.sleep(interval)
    procs = []
    for pid, a in first.items():
        b = _read_proc_io(pid)
        if b is None:
            continue
        uid, ppid, cmd = _proc_meta(pid)
        procs.append({
            'uid':       uid,
            'pid':       int(pid),
            'ppid':      ppid,
            'kb_rd_s':   round((b['rd']   - a['rd'])   / 1024.0 / interval, 2),
            'kb_wr_s':   round((b['wr']   - a['wr'])   / 1024.0 / interval, 2),
            'kb_ccwr_s': round((b['ccwr'] - a['ccwr']) / 1024.0 / interval, 2),
            'iodelay':   b['blkio'] - a['blkio'],
            'command':   cmd[:ARGS_MAXLEN],
        })
    return sorted(procs, key=lambda x: x['kb_rd_s'] + x['kb_wr_s'],
                  reverse=True)[:n]

def parse_fds(s):
    p = s.split()
    if len(p) >= 3:
        try:
            return {'allocated': int(p[0]), 'free': int(p[1]), 'max': int(p[2])}
        except ValueError:
            return {}
    return {}

def parse_sleeping_wchans(s, n):
    """Histogram of (wait channel, command) for non-kernel sleeping processes.

    Surfaces what the bulk of sleeping processes are blocked on — e.g. a spike
    in a single NFS/RPC wchan points at a stuck mount. `wchan` is ptrace-gated,
    so other users' processes collapse into the `-` bucket (see limitations.md).
    """
    c = Counter()
    for line in s.splitlines():
        p = line.split(None, 3)         # ppid stat wchan comm
        if len(p) < 4:
            continue
        ppid, stat, wchan, comm = p
        if ppid == '2':                 # skip kernel threads (children of kthreadd)
            continue
        if not stat.startswith('S'):
            continue
        c[(wchan, comm.strip())] += 1
    return [{'count': cnt, 'wchan': w, 'command': cmd}
            for (w, cmd), cnt in c.most_common(n)]

def parse_top_sleeping(s, n):
    """Highest-CPU sleeping processes with their wait channel and live syscall.

    Reads /proc/<pid>/syscall for each (own processes only; others -> N/A),
    so a hung process shows exactly which syscall it is parked in. Both `wchan`
    and `syscall` are ptrace-gated and resolve only for the collector user's own
    processes (other users -> `-` / `N/A`). See limitations.md.
    """
    out = []
    for line in s.splitlines():
        p = line.split(None, 6)         # pid ppid user %cpu stat wchan args
        if len(p) < 7:
            continue
        pid, ppid, user, cpu, stat, wchan, args = p
        if ppid == '2' or not stat.startswith('S'):
            continue
        try:
            with open('/proc/%s/syscall' % pid) as fh:
                syscall = fh.read().strip()
        except Exception:
            syscall = 'N/A'
        out.append({
            'pid':     _int(pid),
            'user':    user,
            'cpu_pct': _float(cpu),
            'wchan':   wchan,
            'syscall': syscall,
            'args':    args[:ARGS_MAXLEN],
        })
        if len(out) >= n:
            break
    return out


def parse_proc_stat():
    """Node-level scheduler counters from /proc/stat.

    `processes` is the cumulative number of forks since boot; its delta between
    two snapshots gives the fork rate — a cheap fork-storm / sandbox-respawn
    signal without eBPF. `ctxt` (context switches) and the instantaneous
    procs_running / procs_blocked round it out."""
    try:
        with open('/proc/stat') as f:
            text = f.read()
    except Exception:
        return {}
    out = {}
    for line in text.splitlines():
        p = line.split()
        if len(p) >= 2 and p[0] in (
                'processes', 'ctxt', 'procs_running', 'procs_blocked', 'btime'):
            out[p[0]] = _int(p[1])
    return out


def collect_per_user_cgroup(n):
    """Exact per-user CPU/memory/pids from cgroup v2 user slices.

    Reads /sys/fs/cgroup/user.slice/user-<uid>.slice/{cpu.stat,cpu.max,
    memory.current,pids.current,pids.max}. Unlike the top-N `per_user_resources`
    sample these are exact, non-sampled totals: `memory.current`/`pids.current`
    are instantaneous, and `cpu_usage_usec` is cumulative (delta between snapshots
    = CPU time). `pids.current` vs `pids.max` flags users approaching the fork
    ceiling. `cpu_max_quota_usec`/`cpu_max_period_usec` record the CPU ceiling
    (`cpu.max`; quota None == uncapped, so the slice cannot throttle), and the CFS
    bandwidth counters `nr_periods`/`nr_throttled`/`throttled_usec` record its
    enforcement — a `throttled_usec` delta between snapshots is time the slice's
    runnable tasks were held off-CPU by the quota (the cgroup-throttling signal,
    distinct from I/O wait). On an uncapped slice they stay 0 (or absent -
    kernel-dependent), so read them against `cpu_max_quota_usec`.

    cgroup v2 stats are typically readable by the slice owner and root; other
    users' slices may be restricted without privilege, so entries we cannot read
    are skipped. Returns [] on cgroup v1 / no unified hierarchy."""
    base = '/sys/fs/cgroup/user.slice'
    try:
        entries = os.listdir(base)
    except Exception:
        return []
    users = []
    for e in entries:
        m = re.match(r'user-(\d+)\.slice$', e)
        if not m:
            continue
        uid = int(m.group(1))
        d = os.path.join(base, e)
        rec = {'uid': uid}
        try:
            rec['user'] = pwd.getpwuid(uid).pw_name
        except Exception:
            rec['user'] = str(uid)
        # cpu.stat: cumulative usage plus the CFS bandwidth throttle counters. A
        # throttled_usec delta between snapshots is the cgroup-throttling signal
        # (runnable tasks held off-CPU by the cpu.max quota), distinct from I/O
        # wait. On an uncapped slice they stay 0 (or are absent - kernel-
        # dependent), so read them against cpu_max_quota_usec (below; None ==
        # uncapped).
        try:
            with open(os.path.join(d, 'cpu.stat')) as f:
                cs = {}
                for line in f:
                    k, _, v = line.partition(' ')
                    v = v.strip()
                    if v.isdigit():
                        cs[k] = int(v)
            if 'usage_usec' in cs:
                rec['cpu_usage_usec'] = cs['usage_usec']
            for k in ('nr_periods', 'nr_throttled', 'throttled_usec'):
                if k in cs:
                    rec[k] = cs[k]
        except Exception:
            pass
        # cpu.max = "<quota_usec> <period_usec>" ("max <period>" == no limit).
        # Captured so throttle counts can be read against the actual ceiling;
        # cpu_max_quota_usec is None on an uncapped slice (cannot throttle).
        try:
            with open(os.path.join(d, 'cpu.max')) as f:
                q, _, p = f.read().strip().partition(' ')
                rec['cpu_max_quota_usec'] = None if q == 'max' else int(q)
                rec['cpu_max_period_usec'] = int(p) if p else None
        except Exception:
            pass
        for fn, key, conv in (
                ('memory.current', 'mem_current_bytes', int),
                ('pids.current',   'pids_current',      int),
                ('pids.max',       'pids_max',          lambda x: _int(x))):
            try:
                with open(os.path.join(d, fn)) as f:
                    rec[key] = conv(f.read().strip())
            except Exception:
                pass
        # keep only slices we could actually read a resource counter from -- not
        # cpu.max metadata alone, which stays readable on an empty/foreign slice
        if any(k in rec for k in ('cpu_usage_usec', 'mem_current_bytes',
                                  'pids_current', 'pids_max')):
            users.append(rec)
    users.sort(key=lambda r: (r.get('cpu_usage_usec') or 0,
                              r.get('mem_current_bytes') or 0), reverse=True)
    return users[:n]

# GPU columns queried from nvidia-smi --query-gpu (order matters, parsed by index)
GPU_QUERY = ('index,name,temperature.gpu,utilization.gpu,utilization.memory,'
             'memory.used,memory.total,power.draw,power.limit,pstate,'
             'clocks.current.sm,clocks.current.memory')

def parse_gpu(s):
    gpus = []
    for line in s.splitlines():
        p = [x.strip() for x in line.split(',')]
        if len(p) < 12:
            continue
        gpus.append({
            'index':        _int(p[0]),
            'name':         p[1],
            'temp_c':       _float(p[2]),
            'util_gpu_pct': _float(p[3]),
            'util_mem_pct': _float(p[4]),
            'mem_used_mb':  _float(p[5]),
            'mem_total_mb': _float(p[6]),
            'power_w':      _float(p[7]),
            'power_limit_w': _float(p[8]),
            'pstate':       p[9],
            'sm_clock_mhz': _float(p[10]),
            'mem_clock_mhz': _float(p[11]),
        })
    return gpus

def parse_gpu_pmon(s):
    """Per-GPU per-process utilization. Column count varies across driver
    versions, but gpu/pid/type/sm/mem (first five) and command (last) are
    stable, so parse only those."""
    rows = []
    for line in s.splitlines():
        if line.startswith('#') or not line.strip():
            continue
        p = line.split()
        if len(p) < 6 or not p[1].isdigit():   # skip idle GPUs (pid shown as '-')
            continue
        rows.append({
            'gpu':     _int(p[0]),
            'pid':     _int(p[1]),
            'type':    p[2],
            'sm_pct':  _float(p[3]),
            'mem_pct': _float(p[4]),
            'command': p[-1][:100],
        })
    return rows

def parse_gpu_apps(s):
    apps = []
    for line in s.splitlines():
        if not line.strip():
            continue
        p = [x.strip() for x in line.split(',')]
        if len(p) < 4:
            continue
        apps.append({
            'gpu_uuid':     p[0],
            'pid':          _int(p[1]),
            'process_name': p[2],
            'used_mem_mb':  _float(p[3]),
        })
    return apps

def slurm_context():
    """Slurm job/step/partition from the environment, or None outside Slurm."""
    job_id = os.environ.get('SLURM_JOB_ID')
    if not job_id:
        return None
    return {
        'job_id':    job_id,
        'step_id':   os.environ.get('SLURM_STEP_ID', ''),
        'partition': os.environ.get('SLURM_JOB_PARTITION', ''),
    }

def parse_meminfo_detail():
    """Kernel memory internals from /proc/meminfo that `free` hides.

    Dirty + Writeback + NFS_Unstable measure write-back pressure (directly relevant to
    NFS write stalls — the same layer as the autofs wedge); Slab/SReclaimable track
    kernel-object growth; Committed_AS is the total promised address space. Pure /proc
    read.
    """
    want = ('Dirty', 'Writeback', 'NFS_Unstable', 'Slab', 'SReclaimable',
            'SUnreclaim', 'KReclaimable', 'Committed_AS')
    out = {}
    try:
        with open('/proc/meminfo') as f:
            for line in f:
                k, _, rest = line.partition(':')
                if k in want:
                    try:
                        out[k.lower() + '_mb'] = round(int(rest.split()[0]) / 1024, 1)
                    except (ValueError, IndexError):
                        pass
    except Exception:
        return {}
    return out


def parse_tcp_transport():
    """TCP-layer health from /proc/net/snmp + /proc/net/netstat (cumulative counters).

    Distinct from NFS-RPC retransmits: this is the transport beneath every mount and
    SSH session, so a rising RetransSegs rate or ListenDrops flags NIC/network trouble
    before it surfaces as application slowness. The delta between snapshots is the rate.
    """
    out = {}
    def grab(path, prefix, keys):
        try:
            lines = open(path).read().splitlines()
        except Exception:
            return
        for i in range(len(lines) - 1):
            if lines[i].startswith(prefix) and lines[i + 1].startswith(prefix):
                d = dict(zip(lines[i].split()[1:], lines[i + 1].split()[1:]))
                for k in keys:
                    if k in d:
                        try:
                            out[k] = int(d[k])
                        except ValueError:
                            pass
                return
    grab('/proc/net/snmp',    'Tcp:',    ('OutSegs', 'RetransSegs', 'InErrs', 'AttemptFails'))
    grab('/proc/net/netstat', 'TcpExt:', ('ListenDrops', 'ListenOverflows', 'TCPTimeouts'))
    return out


def parse_socket_talkers(n):
    """Top processes by established TCP connection count from `ss -tnp`.

    Attributes socket churn to owning processes (whole host as root; own-user only
    otherwise — ss reads process info via ptrace-gated /proc, see limitations.md).
    This is a connection-*count* view, not bandwidth: per-process byte rates are not
    cheaply available unprivileged, so a single high-throughput rsync shows as one
    connection. Use alongside network_io (per-interface throughput) for the data-hub
    picture.
    """
    out = run(['ss', '-tnpH', 'state', 'established'])
    if not out:
        return []
    proc_re = re.compile(r'"([^"]+)",pid=(\d+)')
    by_proc = Counter()
    for line in out.splitlines():
        m = proc_re.search(line)
        by_proc[(m.group(1), int(m.group(2))) if m else ('(unknown)', 0)] += 1
    return [{'command': name, 'pid': pid, 'conns': cnt}
            for (name, pid), cnt in by_proc.most_common(n)]


def collect_responsiveness():
    """Synthetic latency probes of the user-facing login experience (milliseconds).

    Measures what users actually feel — fork/exec + scheduler latency, a local stat,
    and an SSSD/LDAP identity lookup (a classic login-node stall) — so "the node feels
    slow" becomes a number. Each probe is bounded by a short subprocess timeout, so a
    hung backend delays only this section, never the whole snapshot, and no probe
    touches a /net automount (the thing that hangs). The optional NFS read probe is
    enabled by setting SNAPSHOT_PROBE_NFS_PATH to an already-mounted file. A value of
    -1 means the probe exceeded its timeout (i.e. degraded).
    """
    DEVNULL = subprocess.DEVNULL
    out = {}
    t = time.monotonic()
    try:
        subprocess.run(['/bin/true'], timeout=5, stdout=DEVNULL, stderr=DEVNULL)
        out['fork_exec_ms'] = round((time.monotonic() - t) * 1000, 1)
    except Exception:
        out['fork_exec_ms'] = -1
    t = time.monotonic()
    try:
        os.stat('/usr/bin/env')
        out['stat_local_ms'] = round((time.monotonic() - t) * 1000, 2)
    except Exception:
        out['stat_local_ms'] = -1
    t = time.monotonic()
    try:
        r = subprocess.run(['getent', 'passwd', str(os.geteuid())],
                           timeout=5, stdout=DEVNULL, stderr=DEVNULL)
        out['getent_ms'] = round((time.monotonic() - t) * 1000, 1) if r.returncode == 0 else -1
    except Exception:
        out['getent_ms'] = -1
    probe = os.environ.get('SNAPSHOT_PROBE_NFS_PATH')
    if probe:
        t = time.monotonic()
        try:                                     # dd via subprocess so a hung read is killable
            subprocess.run(['dd', 'if=' + probe, 'of=/dev/null', 'bs=4096', 'count=1'],
                           timeout=5, stdout=DEVNULL, stderr=DEVNULL)
            out['nfs_read_ms'] = round((time.monotonic() - t) * 1000, 1)
        except Exception:
            out['nfs_read_ms'] = -1
    return out


# ---------------------------------------------------------------------------
# collect
# ---------------------------------------------------------------------------

with ThreadPoolExecutor(max_workers=8) as ex:
    # timed commands — run in parallel
    f_mpstat     = ex.submit(run, ['mpstat', '-P', 'ALL', '1', '1'])
    f_vmstat     = ex.submit(run, ['vmstat', '1', '1'])
    f_iostat     = ex.submit(run, ['iostat', '-xz', '1', '2'])
    f_sar        = ex.submit(run, ['sar', '-n', 'DEV', '1', '2'])
    f_top_io     = (None if NODE_MODE
                    else ex.submit(collect_top_io_processes, top_n))
    f_nfsiostat  = ex.submit(run, ['nfsiostat', '5', '2'])

    # GPU commands — only on nodes with nvidia-smi (pmon takes ~1s)
    if HAS_GPU:
        f_gpu  = ex.submit(run, ['nvidia-smi',
            '--query-gpu=' + GPU_QUERY, '--format=csv,noheader,nounits'])
        f_pmon = ex.submit(run, ['nvidia-smi', 'pmon', '-c', '1'])
        f_apps = ex.submit(run, ['nvidia-smi',
            '--query-compute-apps=gpu_uuid,pid,process_name,used_gpu_memory',
            '--format=csv,noheader,nounits'])

    # instant reads while timed commands run
    uptime_out    = run(['uptime'])
    w_out         = run(['w'])
    ps_state_out  = run(['ps', '-e', '--no-headers', '-o', 'stat'])
    free_out      = run(['free', '-b'])
    ss_out        = run(['ss', '-s'])
    ps_cpu_out    = run(['ps', '-eo', 'pid,ppid,user,%cpu,%mem,rss,stat,start,time,args',
                         '--sort=-%cpu', '--no-headers'])
    ps_rss_out    = run(['ps', '-eo', 'pid,ppid,user,%cpu,%mem,rss,stat,start,time,args',
                         '--sort=-rss', '--no-headers'])
    ps_user_out   = run(['ps', '-eo', 'user,%cpu,rss', '--no-headers'])
    ps_dstate_out = run(['ps', 'axo', 'pid,ppid,stat,user,wchan:32,args', '--no-headers'])
    ps_sleep_out  = '' if NODE_MODE else run(
        ['ps', 'axo', 'ppid,stat,wchan:32,comm', '--no-headers'])
    ps_sleep_top_out = '' if NODE_MODE else run(
        ['ps', 'axo', 'pid,ppid,user,%cpu,stat,wchan:32,args',
         '--no-headers', '--sort=-%cpu'])
    ps_tree_out = run(['ps', '-eo', 'pid,ppid,args', '--no-headers']) if PROC_TREE else ''
    try:
        fds_out = open('/proc/sys/fs/file-nr').read().strip()
    except Exception:
        fds_out = ''

snapshot = {
    'timestamp':          datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    'hostname':           hostname,
    'schema_version':     SCHEMA_VERSION,
    'collector':          {'uid': os.geteuid(), 'privileged': IS_ROOT,
                           'args_maxlen': ARGS_MAXLEN, 'mode': MODE},
    'uptime':             parse_uptime(uptime_out),
    'logged_in_users':    parse_w(w_out),
    'process_states':     parse_proc_states(ps_state_out),
    'memory':             parse_memory(free_out),
    'meminfo_detail':     parse_meminfo_detail(),
    'cpu':                parse_mpstat(f_mpstat.result()),
    'vmstat':             parse_vmstat(f_vmstat.result()),
    'proc_stat':          parse_proc_stat(),
    'local_disk_io':      parse_iostat(f_iostat.result()),
    'nfs_io':             parse_nfsiostat(f_nfsiostat.result()),
    'nfs_mountstats':     parse_mountstats(),
    'nfs_rpc':            parse_nfs_rpc(),
    'mount_topology':     parse_mount_topology(),
    'disk_space':         parse_disk_space(),
    'network_io':         parse_sar_dev(f_sar.result()),
    'tcp_transport':      parse_tcp_transport(),
    'socket_summary':     parse_ss(ss_out),
    'socket_talkers':     None if NODE_MODE else parse_socket_talkers(top_n),
    'responsiveness':     collect_responsiveness(),
    'd_state_procs':      parse_d_state_procs(ps_dstate_out),
    'per_user_resources': parse_per_user(ps_user_out, top_n),
    'per_user_cgroup':    collect_per_user_cgroup(top_n),
    'top_cpu_processes':  parse_ps(ps_cpu_out, top_n),
    'top_rss_processes':  parse_ps(ps_rss_out, top_n),
    'top_io_processes':   None if NODE_MODE else f_top_io.result(),
    'sleeping_wchans':    None if NODE_MODE else parse_sleeping_wchans(ps_sleep_out, top_n),
    'top_sleeping_procs': None if NODE_MODE else parse_top_sleeping(ps_sleep_top_out, top_n),
    'proc_tree':          parse_proc_tree(ps_tree_out) if PROC_TREE else None,
    'system_fds':         parse_fds(fds_out),
    'gpu':                parse_gpu(f_gpu.result()) if HAS_GPU else None,
    'gpu_pmon':           parse_gpu_pmon(f_pmon.result()) if HAS_GPU else None,
    'gpu_compute_apps':   parse_gpu_apps(f_apps.result()) if HAS_GPU else None,
    'slurm':              slurm_context(),
}

print(json.dumps(snapshot))
